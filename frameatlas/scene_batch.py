"""Share native scene analysis while retaining per-job ownership and publication."""
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
import json
from pathlib import Path
from typing import Optional

from .native import TICKS, probe_media
from .speech import in_seconds, speech_window_parameters, understand
from .store import PICKER_ACQUISITION, QUEUE_LOOKAHEAD, local_file_available

MAX_SOURCE_BYTES = 32 * 1024 ** 2
VIDEO_PAIR_BYTES = 256 * 1024 ** 2
MAX_CLIP_TICKS = 20 * TICKS
PHOTO_BATCH_LIMIT = 32
VIDEO_QUEUE_LOOKAHEAD = 16
VIDEO_SCENE_LIMIT = 3
MAX_VIDEO_TRANSFERS = 6


class BatchSizeDeferred(ValueError):
    """A speculative download could not establish that it fits the batch."""


class PickerSelectionUnavailable(ValueError):
    def __init__(self, session):
        super().__init__("Google Photos selection changed or expired during acquisition; refresh access to retry")
        self.session = session


def compatible(jobs, *, video_limit=VIDEO_SCENE_LIMIT):
    if not jobs or jobs[0]["asset_kind"] not in ("photo", "video"):
        return []
    first = jobs[0]
    kind = first["asset_kind"]
    limit = PHOTO_BATCH_LIMIT if kind == "photo" else max(VIDEO_SCENE_LIMIT, video_limit)
    matches = []
    # Mixed media can share a selection and priority. Leave the other kind queued
    # and look only inside this bounded window for compatible peers.
    window = QUEUE_LOOKAHEAD if kind == "photo" else VIDEO_QUEUE_LOOKAHEAD
    for job in jobs[:window]:
        if (job["task"] != "analysis" or job["asset_provider"] != "google_photos"
                or job["asset_kind"] not in ("photo", "video")
                or job["options"] != first["options"]
                or job["picker_session"] != first["picker_session"]
                or job["analysis_deadline"] != first["analysis_deadline"]):
            break
        if job["asset_kind"] != kind:
            continue
        matches.append(job)
        if len(matches) == limit:
            break
    return matches


@dataclass
class Member:
    jid: int
    asset: dict
    work: Path
    source: Path
    acquisition: str
    probe: Optional[dict] = None
    saved: bool = False
    error: str = ""


def short_flat_candidate(probe):
    duration = probe.get("duration")
    video = probe.get("video")
    if (str(probe.get("kind", "")).lower() != "movie" or type(duration) is not int
            or not 0 < duration <= MAX_CLIP_TICKS or not isinstance(video, dict)):
        return False
    width, height = video.get("width"), video.get("height")
    return (type(width) is int and type(height) is int and 0 < width <= 3840
            and 0 < height <= 3840 and width * height <= 3840 * 2160)


def prepare_short_video(member):
    if member.asset["metadata"].get("is_360") or member.asset["locator"].get("recording_parts"):
        return False
    try:
        probe = probe_media(member.source)
    except (OSError, RuntimeError, ValueError):
        return False  # The ordinary path can retry and report a source-specific failure.
    if not short_flat_candidate(probe):
        return False
    member.probe = probe
    return True


def read_group_speech(worker, ready, native_factory, model, language):
    members = []
    for member, probe, _, _ in ready:
        try:
            if probe.get("audio_streams") and speech_window_parameters(probe) is None:
                members.append(member)
        except (ValueError, KeyError, TypeError):
            continue  # The individual path reports invalid audio dimensions.
    if len(members) < 2:
        return {}
    cancelled = lambda: any(worker.cancelled(member.jid) for member in members)
    if cancelled():
        return {}
    results = {}
    try:
        # Keep speech cancellation separate from the session holding the scene index.
        with worker.cache.job() as work, native_factory(work, cancelled) as native:
            sources = {native.import_source(member.source)[0]: member for member in members}
            if len(sources) != len(members):
                return {}
            for member in members:
                worker.state(member.jid, "Transcribing speech locally · " + model)
            understand(native, list(sources), model, language)
            for item, member in sources.items():
                try:
                    speech = native.run("transcript.inspect", {"item": item})
                    if isinstance(speech, dict) and speech.get("item") == item and isinstance(speech.get("words"), list):
                        results[member.jid] = in_seconds(speech)
                except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                    continue  # Missing or invalid results use the ordinary retry.
    except (OSError, RuntimeError, ValueError):
        return {}
    return results


def read_clip_speech(worker, member, native_factory, grouped, _scene_item, probe, model, language, stage):
    if member.jid in grouped:
        return grouped[member.jid], "complete", ""
    # An interrupted transcript can close its own process without losing the shared scene index.
    with native_factory(member.work, lambda: worker.cancelled(member.jid)) as native:
        item, _ = native.import_source(member.source)
        return native.read_speech(item, probe, model, language, stage)


def describe(worker, members, kind, opts, native_factory):
    candidates = [p for p in members if not worker.cancelled(p.jid)
                  and not p.asset["metadata"].get("is_360") and not p.asset["locator"].get("recording_parts")
                  and p.source.stat().st_size <= MAX_SOURCE_BYTES
                  and (kind == "photo" or p.probe is not None)]
    if len(candidates) < 2:
        return
    with worker.cache.job() as session_work, native_factory(
            session_work, lambda: all(worker.cancelled(p.jid) for p in candidates)) as native:
        detail, model = native.analysis_settings(kind, opts)
        prepared = []
        for p in candidates:
            if worker.cancelled(p.jid):
                continue
            try:
                probe = p.probe if kind == "video" else probe_media(p.source, native=native)
                if kind == "video" or str(probe.get("kind", "")).lower() == "still":
                    item, imported = native.import_source(p.source)
                    prepared.append((p, probe, item, imported))
            except (OSError, RuntimeError, ValueError):
                continue  # The ordinary path reports a per-source error or succeeds.
        if len(prepared) < 2:
            return
        vr = native.run("media.vrProperties", {"items": [entry[2] for entry in prepared]})
        flat = {item["item"] for item in vr.get("items", [])
                if isinstance(item, dict) and "item" in item and "vr" in item and item["vr"] is None}
        ready = [entry for entry in prepared if entry[2] in flat and not worker.cancelled(entry[0].jid)]
        if len(ready) < 2:
            return
        label = "photos" if kind == "photo" else "short videos"
        for p, _, _, _ in ready:
            worker.state(p.jid, f"Describing {len(ready)} {label} locally")
        report = native.describe([entry[2] for entry in ready], detail, bool(opts.get("fresh")))
        results = report.get("scenes", {})
        grouped = read_group_speech(worker, ready, native_factory, model, opts.get("language", "auto")) if kind == "video" else {}
        for p, probe, item, imported in ready:
            if worker.cancelled(p.jid):
                continue
            receipt = {"items": 1, "job": report.get("job"), "batch_size": len(ready),
                       "scenes": {"items": [r for r in results.get("items", []) if r.get("item") == item]},
                       "batch_seconds": results.get("seconds")}
            try:
                speech_reader = partial(read_clip_speech, worker, p, native_factory, grouped) if kind == "video" else None
                scenes, speech, provenance = native.analyze(
                    p.source, kind, opts, lambda text: worker.state(p.jid, text),
                    indexed_media=(item, imported, probe, receipt, p.work, detail, model),
                    speech_reader=speech_reader)
                if p.jid in grouped:
                    provenance["speech_group_results"] = len(grouped)
            except (OSError, RuntimeError, ValueError):
                continue
            try:
                worker.save_analysis(p.asset, p.work, p.jid, p.source, p.acquisition, scenes, speech, provenance)
                p.saved = True
            except Exception as exc:
                p.error = str(exc)


def finish(worker, jid, owner, saved=False, error="", *, picker_session=None):
    try:
        owner.close()
    except Exception as exc:
        detail = f"Temporary cleanup needs attention: {exc}"
        error = f"{error}. {detail}" if error else detail
    else:
        if picker_session is not None and not worker.cancelled(jid) and worker.store.defer_picker_acquisition(jid, picker_session):
            return
    if saved:
        stage = "Finished · temporary cleanup needs attention" if error else "Finished · temporary media removed"
        worker.state(jid, stage, "done", error)
    elif worker.cancelled(jid):
        worker.state(jid, "Cancelled", "cancelled", error)
    else:
        worker.state(jid, "Needs attention", "failed", error)


def defer(worker, jid, owner, error, *, rejected_pair_limit=0):
    try:
        owner.close()
    except Exception as cleanup_error:
        finish(worker, jid, owner, error=f"{error}. Temporary cleanup needs attention: {cleanup_error}")
    else:
        worker.store.defer_job(jid, rejected_pair_limit=rejected_pair_limit)


def pair_limit_rejected(job, limit):
    previous = job.get("rejected_pair_limit")
    return type(previous) is int and previous >= limit


def storage_limited(error):
    return "storage budget" in error or "exceeded its cap" in error


def interrupted_transfer_outcome(source, limit, error):
    """Use only completed files marked by the broker in this owned job."""
    receipt = source.with_name(".frameatlas-transfer-complete")
    try:
        if source.is_symlink() or receipt.is_symlink() or not source.is_file() or not receipt.is_file():
            return {"error": error}
        with receipt.open("rb") as handle:
            size = source.stat().st_size
            if 0 < size <= limit and handle.read(24) == f"{size}\n".encode():
                return {"bytes": size}
    except OSError:
        pass  # Incomplete or unavailable proof takes the ordinary retry path.
    return {"error": error}


def stage_parallel(worker, jobs, kind, opts, cleanup, staged, *, limit=MAX_SOURCE_BYTES,
                   prefetch_cancelled=None, timeout=1900):
    """Return None for serial fallback, or whether the batch should stop staging."""
    from .worker import ANALYSIS_RESERVE, staging_destination
    if any(pair_limit_rejected(job, limit) for job in jobs):
        return None
    try:
        if worker.cache.budget() < ANALYSIS_RESERVE + len(jobs) * limit:
            return None
        assets = [worker.store.get(job["asset_id"], include_scenes=False) for job in jobs]
    except (OSError, ValueError):
        # The normal per-source path can report the problem after claiming the job.
        return None
    session = assets[0]["locator"].get("session_id")
    if not session or len({asset["source_id"] for asset in assets}) != len(assets) or any(
            asset["locator"].get("session_id") != session or asset["bytes"] > limit
            or (local_file_available(asset.get("local_path")) and not opts.get("prefer_cloud")) for asset in assets):
        return None
    claimed = []
    members = []
    try:
        for job, asset in zip(jobs, assets):
            jid = job["id"]
            if worker.stop.is_set() or not worker.store.claim_job(jid, respect_pause=True):
                raise ValueError("Parallel staging interrupted before every job could be claimed")
            owner = cleanup.enter_context(ExitStack())
            claimed.append((jid, owner))
        for (jid, owner), asset in zip(claimed, assets):
            work = owner.enter_context(worker.cache.job())
            members.append(Member(jid, asset, work, staging_destination(asset, work), PICKER_ACQUISITION))
        transfer_options = {"timeout": timeout} if prefetch_cancelled is not None else {}
        outcomes = worker.analysis_downloads.fetch_parallel(
            session, [(p.asset["source_id"], p.source) for p in members], limit,
            lambda: ((prefetch_cancelled is not None and prefetch_cancelled())
                     or all(worker.cancelled(p.jid) for p in members)), **transfer_options)
    except Exception as exc:
        if prefetch_cancelled is None or len(members) != len(jobs):
            for jid, owner in claimed:
                defer(worker, jid, owner, str(exc))
            return True
        outcomes = [interrupted_transfer_outcome(p.source, limit, str(exc)) for p in members]
    deferred = False
    for p, (_, owner), outcome in zip(members, claimed, outcomes):
        try:
            if worker.cancelled(p.jid):
                if prefetch_cancelled is None:
                    finish(worker, p.jid, owner)
                else:
                    defer(worker, p.jid, owner, "")
                continue
            if outcome.get("deferred") is True:
                reason = ("Google did not send video headers within the parallel wait"
                          if outcome.get("reason") == "slow_video_headers"
                          else "Source has no reported size within the batch allowance")
                defer(worker, p.jid, owner, reason, rejected_pair_limit=limit)
                deferred = True
                continue
            if outcome.get("error"):
                raise ValueError(outcome["error"])
            size = p.source.stat().st_size
            if size != outcome.get("bytes") or not 0 < size <= limit:
                raise ValueError("Parallel transfer size could not be verified")
            worker.state(p.jid, "Preparing native analysis")
            if prefetch_cancelled is None and kind == "video" and size <= MAX_SOURCE_BYTES:
                prepare_short_video(p)
            staged.append((p, owner))
        except Exception as exc:
            defer(worker, p.jid, owner, str(exc))
            deferred = True
    return deferred


def run_batch(worker, jobs, native_factory, *, during_studio_wait=False, before_analysis=None):
    try:
        opts = json.loads(jobs[0]["options"])
        if not isinstance(opts, dict):
            raise ValueError("Invalid job options")
    except (ValueError, TypeError):
        if not during_studio_wait:
            worker.run_job(jobs[0])
        return
    worker.cache.cap = worker.store.storage_limit()
    kind = jobs[0]["asset_kind"]
    staged = []
    pending = deque(jobs)
    with ExitStack() as cleanup:
        while pending:
            job = pending.popleft()
            jid = job["id"]
            recovering_head = False
            paired = worker.analysis_downloads.paired_photos if kind == "photo" else worker.analysis_downloads.paired_videos
            width_cap = (worker.analysis_downloads.maximum_parallel_photos if kind == "photo"
                         else worker.analysis_downloads.maximum_parallel_videos)
            width = min(width_cap, len(pending) + 1)
            photo_head = not staged and kind == "photo" and not during_studio_wait
            parallel_head = photo_head and (width > 2 or len(pending) % 2 == 1)
            video_pair_limit = min(VIDEO_PAIR_BYTES, worker.analysis_downloads.maximum_paired_video_bytes)
            wide_video_head = (not staged and kind == "video" and not during_studio_wait
                               and video_pair_limit > MAX_SOURCE_BYTES)
            parallel_head = parallel_head or wide_video_head
            if pending and paired and (staged or parallel_head):
                limit = video_pair_limit if wide_video_head else MAX_SOURCE_BYTES
                group = [job, *list(pending)[:width - 1]]
                deferred = stage_parallel(worker, group, kind, opts, cleanup, staged, limit=limit)
                if deferred is None and width > 4:
                    width = 4
                    deferred = stage_parallel(worker, group[:width], kind, opts, cleanup, staged, limit=limit)
                if deferred is None and width > 2 and (kind == "video" or staged or len(pending) % 2 == 1):
                    width = 2
                    deferred = stage_parallel(worker, group[:width], kind, opts, cleanup, staged, limit=limit)
                if deferred is not None:
                    for _ in range(width - 1):
                        pending.popleft()
                    if not deferred:
                        if wide_video_head and any(member.probe is None for member, _ in staged):
                            break  # Long or large clips retain individual native analysis.
                        continue
                    if not parallel_head or any(member.jid == jid for member, _ in staged):
                        break
                    if kind == "video" and staged:
                        head = worker.store.job(jid)
                        if head and pair_limit_rejected(head["result"], limit):
                            # Publish the completed peers before the head's individual retry.
                            break
                    # Retry the head normally, counting any staged peer against the cache.
                    recovering_head = True
                    pending.clear()
            batch_limit = MAX_SOURCE_BYTES if during_studio_wait or (staged and kind == "video" and not recovering_head) else None
            if batch_limit is not None and pair_limit_rejected(job, batch_limit):
                break
            if worker.stop.is_set() or not worker.store.claim_job(jid, respect_pause=True):
                continue
            owner = cleanup.enter_context(ExitStack())
            try:
                asset = worker.store.get(job["asset_id"])
                work = owner.enter_context(worker.cache.job())
                source_options = {"during_studio_wait": True} if during_studio_wait else {}
                try:
                    source, acquisition = worker.source(asset, work, jid, opts.get("prefer_cloud", False),
                                                        batch_limit=batch_limit, **source_options)
                except ValueError as exc:
                    if not recovering_head or not staged or not storage_limited(str(exc)) or worker.cancelled(jid):
                        raise
                    for peer, peer_owner in staged:
                        defer(worker, peer.jid, peer_owner, "")
                    staged.clear()
                    source, acquisition = worker.source(asset, work, jid, opts.get("prefer_cloud", False),
                                                        batch_limit=None)
                worker.state(jid, "Preparing native analysis")
                member = Member(jid, asset, work, source, acquisition)
                large = source.stat().st_size > MAX_SOURCE_BYTES
                if recovering_head:
                    staged.insert(0, (member, owner))
                else:
                    staged.append((member, owner))
                if large:
                    break
                if kind == "video" and not prepare_short_video(member):
                    break
            except Exception as exc:
                error = str(exc)
                if (during_studio_wait or (staged and (isinstance(exc, BatchSizeDeferred)
                        or storage_limited(error)))) and not worker.cancelled(jid):
                    defer(worker, jid, owner, error)
                    break
                finish(worker, jid, owner, error=error,
                       picker_session=exc.session if isinstance(exc, PickerSelectionUnavailable) else None)
        if before_analysis and any(p.acquisition == PICKER_ACQUISITION for p, _ in staged):
            before_analysis()
        analyze_staged(worker, staged, kind, opts, native_factory, during_studio_wait=during_studio_wait)


def analyze_staged(worker, staged, kind, opts, native_factory, *, during_studio_wait=False):
    if not staged:
        return
    width = VIDEO_SCENE_LIMIT if kind == "video" else len(staged)
    for start in range(0, len(staged), width):
        group = staged[start:start + width]
        try:
            describe(worker, [member for member, _ in group], kind, opts, native_factory)
        except Exception:
            pass  # An unsuccessful shared native call falls back to each staged source.
        for p, owner in group:
            if not p.saved and not p.error and not worker.cancelled(p.jid):
                try:
                    worker.analyze_source(p.asset, p.work, p.jid, opts, p.source, p.acquisition)
                    p.saved = True
                except Exception as exc:
                    p.error = str(exc)
            if during_studio_wait and p.error and not worker.cancelled(p.jid):
                defer(worker, p.jid, owner, p.error)
            else:
                finish(worker, p.jid, owner, p.saved, p.error)
