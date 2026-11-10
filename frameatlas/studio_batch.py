"""Acquire an explicit Studio selection on one owned volume and index it item by item."""
from collections import Counter
from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import time

from .camera_parts import names, source_paths
from .native import Native
from .scene_batch import (MAX_SOURCE_BYTES, analyze_staged, compatible, defer, pair_limit_rejected,
                          run_batch, stage_parallel)
from .studio_stage import (CONFIRMED, OVERHEAD, confirmed, create_for, mark_confirmed,
                           member_confirmation, needs_trailer, retire_recording, retirement_plan,
                           scan_recordings, validated_source, work_removed)

MAX_WAIT_PHOTOS = 24
WAIT_PHOTO_BATCH = 4


def index_waiting_photo(worker, attempted, quota, confirmation_pending=lambda: False):
    from .worker import ANALYSIS_RESERVE, MIN_FREE
    def should_yield():
        return worker.stop.is_set() or worker.store.queue_state()["paused"] or confirmation_pending()

    if len(attempted) >= MAX_WAIT_PHOTOS or should_yield():
        return False
    jobs = worker.store.next_jobs()
    if not jobs or jobs[0]["task"] != "analysis" or jobs[0].get("studio_batch"):
        return False
    # Keep enough physical space even if Studio consumes its full quota.
    free = shutil.disk_usage(worker.cache.root).free - MIN_FREE - quota - OVERHEAD
    fitting = (min(worker.cache.budget(), free) - ANALYSIS_RESERVE) // MAX_SOURCE_BYTES
    if fitting < 1:
        return False
    for index, job in enumerate(jobs):
        if (job["id"] not in attempted and job["task"] == "analysis"
                and job["asset_provider"] == "google_photos" and job["asset_kind"] == "photo"
                and job["picker_session"] and not pair_limit_rejected(job, MAX_SOURCE_BYTES)):
            width = min(WAIT_PHOTO_BATCH, fitting, MAX_WAIT_PHOTOS - len(attempted),
                        worker.analysis_downloads.maximum_parallel_photos)
            if width > 1 and worker.analysis_downloads.paired_photos:
                peers = [peer for peer in compatible(jobs[index:]) if peer["id"] not in attempted
                         and not pair_limit_rejected(peer, MAX_SOURCE_BYTES)][:width]
                if len(peers) > 1:
                    options = json.loads(job["options"])
                    if isinstance(options, dict):
                        with ExitStack() as cleanup:
                            staged = []
                            outcome = stage_parallel(worker, peers, "photo", options, cleanup, staged,
                                                     prefetch_cancelled=should_yield, timeout=20)
                            if outcome is not None:
                                attempted.update(peer["id"] for peer in peers)
                                settled = False
                                try:
                                    if should_yield():
                                        return False
                                    analyze_staged(worker, staged, "photo", options, Native, during_studio_wait=True)
                                    settled = True
                                    return True
                                finally:
                                    if not settled:
                                        for member, owner in staged:
                                            defer(worker, member.jid, owner, "")
            if should_yield():
                return False
            attempted.add(job["id"])
            run_batch(worker, [job], Native, during_studio_wait=True)
            return True
    return False


def acquire(worker, entries, work, stages):
    assets = list(entries.values())
    mount, quota = create_for(worker, assets, work, True)
    members = [{"id": jid, "filename": asset["filename"]} for jid, asset in entries.items()]
    for jid, asset in entries.items():
        stage = {"work_name": work.name, "destination": str(mount), "filename": asset["filename"],
                 "quota_bytes": quota, "state": "waiting", "requires_confirmation": True,
                 "expected_parts": len(names(asset["filename"])[1]), "batch": members,
                 "instructions": "In Studio, select these exact recordings together and choose Download → Browse. "
                                 "Leave Set as default download path off. Individual confirmation can free space after local analysis. "
                                 "Stop failed transfers in Studio before retrying or cancelling their FrameAtlas jobs. "
                                 "Confirm each completed recording, or confirm the batch once all remaining transfers are complete."}
        stages[jid] = stage
        worker.store.job_result(jid, {"studio_download": stage})
        worker.state(jid, "Waiting for Studio batch download")
    deadline = time.monotonic() + 1200
    pending = dict(entries)
    attempted = set()
    can_index_while_waiting = True
    observed, used_paths = {}, set()

    def confirmation_pending():
        return (confirmed(work / CONFIRMED)
                or any(confirmed(member_confirmation(work, jid)) for jid in pending))

    while pending:
        now = time.monotonic()
        if now >= deadline:
            raise ValueError("Studio batch timed out. Finish or cancel any active transfer in Studio before cleaning the source.")
        ready, paths, errors = {}, {}, {}
        batch_confirmed = confirmed(work / CONFIRMED)
        for jid, asset in list(pending.items()):
            if worker.cancelled(jid):
                pending.pop(jid)
                continue
            if not (batch_confirmed or confirmed(member_confirmation(work, jid))):
                continue
            try:
                selected = source_paths(mount, asset["filename"])
                signature = tuple((p.resolve(), st.st_size, st.st_mtime_ns)
                                  for p in selected for st in (p.stat(),))
                if not signature or any(size <= 0 for _, size, _ in signature):
                    observed.pop(jid, None)
                    continue
                previous, since = observed.get(jid, (None, now))
                if previous != signature:
                    observed[jid] = signature, now
                    continue
                if now - since < 2:
                    continue
                selected_paths = {p for p, _, _ in signature}
                if used_paths.intersection(selected_paths):
                    raise ValueError("Selected Studio items overlap and resolve to the same original")
                used_paths.update(selected_paths)
                ready[jid], paths[jid] = asset, selected
            except FileNotFoundError:
                observed.pop(jid, None)
            except (OSError, ValueError) as exc:
                errors[jid] = str(exc)
        if ready or errors:
            for jid in ready.keys() | errors.keys():
                pending.pop(jid)
            sources, validation_errors = validate(mount, quota, ready, paths)
            started = time.monotonic()
            yield sources, {**errors, **validation_errors}
            deadline += time.monotonic() - started
        elif pending:
            if can_index_while_waiting and not confirmation_pending() and deadline - now >= 45:
                try:
                    if index_waiting_photo(worker, attempted, quota, confirmation_pending):
                        continue
                except Exception as exc:
                    can_index_while_waiting = False
                    print("Waiting-photo work stopped for this Studio batch: " + str(exc)[:300], flush=True)
            worker.stop.wait(0.5)
    if all(confirmed(member_confirmation(work, jid)) for jid in entries):
        mark_confirmed(work / CONFIRMED)


def validate(mount, quota, entries, paths):
    records = scan_recordings(mount) if any(needs_trailer(asset) for asset in entries.values()) else []
    sources, errors = {}, {}
    for jid, asset in entries.items():
        try:
            parts = validated_source(mount, paths[jid], asset, quota, records)
            if not parts:
                raise ValueError("The original did not pass the native camera validation")
            sources[jid] = paths[jid][0], [part["filename"] for part in parts], retirement_plan(mount, asset["filename"], parts)
        except Exception as exc:
            errors[jid] = str(exc)
    return sources, errors


def run(worker, batch):
    jobs = worker.store.claim_studio_batch(batch)
    if not jobs:
        return
    saved, errors, stages = set(), {}, {}
    batch_error = ""
    try:
        worker.cache.cap = worker.store.storage_limit()
        entries = {job["id"]: worker.store.get(job["asset_id"]) for job in jobs}
        folder_counts = Counter(Path(asset["filename"]).stem.casefold() for asset in entries.values())
        with worker.cache.job() as shared:
            options = {job["id"]: json.loads(job["options"]) for job in jobs}
            for sources, validation_errors in acquire(worker, entries, shared, stages):
                errors.update(validation_errors)
                for jid, (_, filenames, _) in sources.items():
                    stages[jid].update(state="ready", part_filenames=filenames)
                    worker.store.job_result(jid, {"studio_download": stages[jid]})
                    worker.state(jid, "Source validated · waiting for local analysis")
                for jid, (source, _, plan) in sources.items():
                    if worker.cancelled(jid):
                        continue
                    try:
                        with worker.cache.job() as work:
                            worker.analyze_source(entries[jid], work, jid, options[jid], source,
                                                  "Temporary Studio batch download in an owned quota-limited volume")
                            saved.add(jid)
                        stem = Path(entries[jid]["filename"]).stem.casefold()
                        released = retire_recording(shared, jid, plan) if folder_counts[stem] == 1 else 0
                        if released:
                            stages[jid].update(state="released", released_bytes=released)
                            worker.store.job_result(jid, {"studio_download": stages[jid]})
                        worker.state(jid, "Analysis saved · source released" if released
                                     else "Analysis saved · waiting for shared source cleanup")
                    except Exception as exc:
                        errors[jid] = str(exc)
    except Exception as exc:
        batch_error = str(exc)

    for job in jobs:
        jid = job["id"]
        if jid in stages and work_removed(worker.cache.root / stages[jid]["work_name"]):
            stages[jid]["state"] = "removed"
            worker.store.job_result(jid, {"studio_download": stages[jid]})
        error = errors.get(jid) or batch_error
        if jid in saved:
            stage = "Finished · temporary cleanup needs attention" if error else "Finished · temporary media removed"
            worker.state(jid, stage, "done", error)
        elif worker.cancelled(jid):
            worker.state(jid, "Cancelled", "cancelled", error)
        else:
            worker.state(jid, "Needs attention", "failed", error or "Studio source did not validate")
