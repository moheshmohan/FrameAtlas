"""Stage one bounded Picker batch while the foreground runs native analysis."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import shutil
from threading import Event
import time

from .scene_batch import (MAX_SOURCE_BYTES, VIDEO_PAIR_BYTES, Member, analyze_staged, compatible,
                          defer, interrupted_transfer_outcome, pair_limit_rejected,
                          prepare_short_video, stage_parallel)
from .store import PICKER_ACQUISITION, local_file_available

PREFETCH_SECONDS = 75


def bulk_order(job, options):
    deadline = job["analysis_deadline"]
    return (not options.get("interactive"), not options.get("priority"),
            deadline is None, deadline, job["id"])


class Lookahead:
    def __init__(self, worker, native_factory):
        self.worker = worker
        self.native_factory = native_factory
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="picker-lookahead")
        self.future = None
        self.abort = Event()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.abort.set()
        try:
            self.consume()
        except Exception as exc:
            print("Picker lookahead could not finish cleanup: " + str(exc)[:200], flush=True)
        finally:
            self.executor.shutdown()

    def start(self):
        from .worker import ANALYSIS_RESERVE

        worker = self.worker
        if self.future is not None or worker.stop.is_set() or worker.draining.is_set():
            return
        try:
            downloads = worker.analysis_downloads
            jobs = compatible(worker.store.next_jobs(), video_limit=downloads.maximum_parallel_videos)
            if not jobs or worker.store.queue_state()["paused"]:
                return
            head = jobs[0]
            opts = json.loads(head["options"])
            if not isinstance(opts, dict) or opts.get("interactive"):
                return
            kind = head["asset_kind"]
            if kind == "photo" and downloads.paired_photos:
                width, limit = downloads.maximum_parallel_photos, MAX_SOURCE_BYTES
            elif kind == "video" and downloads.paired_videos:
                limit = min(VIDEO_PAIR_BYTES, downloads.maximum_paired_video_bytes)
                if limit <= 0:
                    return
                fitting = max(0, (worker.cache.budget() - ANALYSIS_RESERVE) // limit)
                width = min(downloads.maximum_parallel_videos, fitting)
                jobs = jobs[:width]
                for index, job in enumerate(jobs):
                    if pair_limit_rejected(job, limit):
                        jobs = jobs[:max(1, index)]
                        break
                if len(jobs) == 1 and downloads.known_size_batch:
                    self.future = self.executor.submit(self.acquire, jobs, kind, opts, 1, limit)
                    return
            else:
                return
            if len(jobs) < 2 or width < 2 or limit <= 0 or (width == 2 and len(jobs) % 2):
                return
            self.future = self.executor.submit(self.acquire, jobs, kind, opts, width, limit)
        except Exception as exc:
            print("Picker lookahead skipped: " + str(exc)[:200], flush=True)

    def superseded(self, head, options):
        from .worker import ANALYSIS_RESERVE, MIN_FREE

        worker = self.worker
        store = worker.store
        if (self.abort.is_set() or worker.stop.is_set() or store.queue_state()["paused"]
                or store.storage_limit() < worker.cache.cap
                or shutil.disk_usage(worker.cache.root).free < MIN_FREE + ANALYSIS_RESERVE):
            return True
        following = store.next_jobs(1)
        if not following:
            return False
        first = following[0]
        if first["task"] != "analysis" or first.get("studio_batch"):
            return True
        opts = json.loads(first["options"])
        return (not isinstance(opts, dict)
                or (first["asset_provider"] == "insta360" and opts.get("priority"))
                or bulk_order(first, opts) < bulk_order(head, options))

    def acquire(self, jobs, kind, opts, width, limit):
        worker = self.worker
        from .worker import ANALYSIS_RESERVE

        staged = []
        deadline = time.monotonic() + PREFETCH_SECONDS
        next_check = 0
        superseded = False

        def cancelled():
            nonlocal next_check, superseded
            now = time.monotonic()
            if self.abort.is_set() or worker.stop.is_set() or now >= deadline:
                return True
            if now >= next_check and not superseded:
                superseded = self.superseded(jobs[0], opts)
                next_check = now + 1
            return superseded

        with ExitStack() as cleanup:
            transferred_ownership = False
            try:
                # Hold room for every possible transfer byte and the active native pass.
                if worker.cache.budget() < ANALYSIS_RESERVE + len(jobs) * limit:
                    return None
                if width == 1 and not cancelled():
                    self.stage_video(jobs[0], opts, cleanup, staged, limit, cancelled,
                                     max(0.1, deadline - time.monotonic()))
                start = len(jobs) if width == 1 else 0
                while start < len(jobs):
                    size = min(width, len(jobs) - start)
                    if len(jobs) - start - size == 1:
                        size -= 1  # Avoid a singleton without reducing the native batch width.
                    if cancelled():
                        break
                    group = jobs[start:start + size]
                    deferred = stage_parallel(worker, group, kind, opts, cleanup, staged, limit=limit,
                                              prefetch_cancelled=cancelled,
                                              timeout=max(0.1, deadline - time.monotonic()))
                    if deferred is None or (deferred and kind == "video"):
                        break
                    start += size
                complete = len(staged) == len(jobs)
                # The acquisition deadline bounds downloads. Completed files
                # can still be used unless priority or storage rules preempt them.
                if not staged or self.superseded(jobs[0], opts):
                    return None
                for member, _ in staged:
                    worker.state(member.jid, "Source staged · waiting for local analysis")
                owned = cleanup.pop_all()
                transferred_ownership = True
                return owned, jobs[0], staged, kind, opts, complete
            finally:
                # An unsuccessful or interrupted speculative fetch uses the ordinary queue.
                if not transferred_ownership:
                    for member, owner in staged:
                        defer(worker, member.jid, owner, "")

    def stage_video(self, job, opts, cleanup, staged, limit, cancelled, timeout):
        from .worker import staging_destination

        worker = self.worker
        asset = worker.store.get(job["asset_id"], include_scenes=False)
        session = asset["locator"].get("session_id")
        if (not session or asset["bytes"] > limit
                or (local_file_available(asset.get("local_path")) and not opts.get("prefer_cloud"))):
            return
        jid = job["id"]
        if not worker.store.claim_job(jid, respect_pause=True):
            return
        owner = cleanup.enter_context(ExitStack())
        try:
            work = owner.enter_context(worker.cache.job())
            source = staging_destination(asset, work)
            try:
                response = worker.analysis_downloads.fetch(
                    session, asset["source_id"], source, limit,
                    lambda: cancelled() or worker.cancelled(jid),
                    timeout=timeout, batch_limit=limit, require_size=True)
            except (OSError, RuntimeError, ValueError) as exc:
                response = interrupted_transfer_outcome(source, limit, str(exc))
                if response.get("error"):
                    raise
            size = source.stat().st_size
            if size != response.get("bytes") or not 0 < size <= limit:
                raise ValueError("Speculative video size could not be verified")
            staged.append((Member(jid, asset, work, source, PICKER_ACQUISITION), owner))
        except Exception as exc:
            defer(worker, jid, owner, str(exc))

    def consume(self):
        if self.future is None:
            return False
        future, self.future = self.future, None
        obtained = future.result()
        if obtained is None:
            return False
        cleanup, head, staged, kind, opts, complete = obtained
        worker = self.worker
        with cleanup:
            published = False
            try:
                if self.superseded(head, opts):
                    return False
                for member, _ in staged:
                    worker.state(member.jid, "Preparing native analysis")
                    if kind == "video" and len(staged) > 1 and member.source.stat().st_size <= MAX_SOURCE_BYTES:
                        prepare_short_video(member)
                if self.superseded(head, opts):
                    return False
                # Deferred members should return through the ordinary queue.
                if complete:
                    self.start()
                analyze_staged(worker, staged, kind, opts, self.native_factory)
                published = True
                return True
            finally:
                if not published:
                    for member, owner in staged:
                        defer(worker, member.jid, owner, "")
