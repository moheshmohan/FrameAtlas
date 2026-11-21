import contextlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from .native import Native, CLI
from .scene_batch import BatchSizeDeferred, MAX_VIDEO_TRANSFERS, PickerSelectionUnavailable, compatible, run_batch
from .store import LOCAL_ACQUISITION, PICKER_ACQUISITION, local_file_available

PROJECT = Path(__file__).resolve().parent.parent
BRIDGE = Path(os.environ.get("FRAMEATLAS_CLOUD_BRIDGE", str(PROJECT / "cloud-bridge/bin/frameatlas-cloud")))
MAX_BYTES = 2 * 1024 ** 3
MIN_FREE = 3 * 1024 ** 3
# Leave room for bounded native perspective frames, logs and project metadata.
ANALYSIS_RESERVE = 256 * 1024 ** 2


def analysis_phase(stage):
    if stage == "Resolving source":
        return "source"
    if stage == "Preparing native analysis":
        return "prepare"
    if stage == "Finding cuts and describing scenes locally" or stage.startswith("Describing "):
        return "scenes"
    if stage.startswith("Transcribing speech locally"):
        return "speech"
    if stage == "Saving a small local preview":
        return "preview_and_cleanup"
    return None


def staging_destination(asset, work):
    suffix = Path(asset["filename"]).suffix[:12] or (".mp4" if asset["kind"] == "video" else ".jpg")
    return work / ("source" + suffix)


def broker(root, action, *args, timeout=120, cancelled=None):
    if not BRIDGE.is_file():
        raise ValueError("Build the optional cloud broker first: ./run.sh build-cloud")
    p = subprocess.Popen([str(BRIDGE), action, *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         env=dict(os.environ, FRAMEATLAS_DATA=str(root)))
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancelled and cancelled():
                raise RuntimeError("Job cancelled")
            if action == "download" and shutil.disk_usage(root).free < MIN_FREE:
                raise ValueError("Stopped cloud download to preserve 3 GiB free disk space")
            if time.monotonic() >= deadline:
                raise RuntimeError("Cloud operation timed out; retry the selection")
            try:
                stdout, stderr = p.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait(timeout=5)
    try:
        data = json.loads(stdout)
    except ValueError:
        raise RuntimeError("Cloud broker did not respond; check FilmCraft Photos sign-in")
    if p.returncode or data.get("error"):
        raise ValueError(str(data.get("error", "Cloud broker failed"))[:1000])
    return data


class AnalysisDownloads:
    """Single-request pipe to the private broker, with one-shot compatibility."""

    def __init__(self, root):
        self.root = root
        self.supported = None
        self.known_size_batch = False
        self.paired_photos = False
        self.maximum_parallel_photos = 2
        self.paired_videos = False
        self.maximum_parallel_videos = 2
        self.maximum_paired_video_bytes = 0
        self.process = None
        self.responses = None

    def start(self):
        self.process = subprocess.Popen([str(BRIDGE), "serve-downloads"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        env=dict(os.environ, FRAMEATLAS_DATA=str(self.root)))
        self.responses = queue.Queue(maxsize=1)
        process, responses = self.process, self.responses

        def read():
            try:
                while True:
                    line = process.stdout.readline(16385)
                    responses.put(line, timeout=1)
                    if not line or len(line) > 16384 or not line.endswith(b"\n"):
                        return
            except (OSError, ValueError, queue.Full):
                return

        threading.Thread(target=read, daemon=True).start()

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()

    def fetch(self, session, source_id, dest, budget, cancelled, timeout=1900, *, batch_limit=None,
              require_size=False):
        if self.supported is None:
            capabilities = broker(self.root, "capabilities", timeout=15, cancelled=cancelled)
            self.supported = capabilities.get("persistent_downloads", False)
            self.known_size_batch = capabilities.get("known_size_batch_downloads", False)
            self.paired_photos = self.supported and capabilities.get("paired_photo_downloads") is True
            width = capabilities.get("maximum_parallel_photos")
            if self.paired_photos and type(width) is int and width >= 2:
                self.maximum_parallel_photos = min(width, 8)
            self.paired_videos = self.supported and capabilities.get("paired_video_downloads") is True
            video_width = capabilities.get("maximum_parallel_videos")
            if self.paired_videos and type(video_width) is int and video_width >= 2:
                self.maximum_parallel_videos = min(video_width, MAX_VIDEO_TRANSFERS)
            limit = capabilities.get("maximum_paired_video_bytes")
            if self.paired_videos and type(limit) is int:
                self.maximum_paired_video_bytes = limit
        size_gated = batch_limit is not None and self.known_size_batch
        if require_size and (not self.supported or not size_gated):
            raise BatchSizeDeferred("This broker cannot safely stage a size-limited source")
        if not self.supported:
            return broker(self.root, "download", session, source_id, dest, budget,
                          timeout=timeout, cancelled=cancelled)
        request = [session, source_id, str(dest), budget]
        if size_gated:
            request = [session, source_id, str(dest), min(batch_limit, budget), True]
        data = self.exchange(request, cancelled, timeout)
        if size_gated and data.get("deferred") is True:
            raise BatchSizeDeferred("The cloud rendition exceeds the batch allowance or has no reported size")
        return data

    def fetch_parallel(self, session, files, limit, cancelled, timeout=1900):
        request = [[session, asset_id, str(dest), limit, True] for asset_id, dest in files]
        data = self.exchange({"pair": request}, cancelled, timeout)
        pair = data.get("pair")
        if not isinstance(pair, list) or len(pair) != len(files) or any(not isinstance(row, dict) for row in pair):
            self.close()
            raise RuntimeError("Cloud broker returned an invalid parallel response")
        return pair

    def exchange(self, request, cancelled, timeout):
        if cancelled():
            raise RuntimeError("Job cancelled")
        if self.process is None or self.process.poll() is not None:
            self.close()
            self.start()
        frame = (json.dumps(request) + "\n").encode()
        if len(frame) > 16384:
            raise ValueError("Cloud download request exceeded its size limit")
        deadline = time.monotonic() + timeout
        try:
            self.process.stdin.write(frame)
            self.process.stdin.flush()
            while True:
                if cancelled():
                    raise RuntimeError("Job cancelled")
                if shutil.disk_usage(self.root).free < MIN_FREE:
                    raise ValueError("Stopped cloud download to preserve 3 GiB free disk space")
                if time.monotonic() >= deadline:
                    raise RuntimeError("Cloud operation timed out; retry the selection")
                try:
                    line = self.responses.get(timeout=0.25)
                except queue.Empty:
                    if self.process.poll() is not None:
                        raise RuntimeError("Cloud broker stopped; retry the selection")
                    continue
                if len(line) > 16384 or not line.endswith(b"\n"):
                    raise RuntimeError("Cloud broker returned an invalid response")
                data = json.loads(line)
                if not isinstance(data, dict):
                    raise RuntimeError("Cloud broker returned an invalid response")
                break
        except (OSError, ValueError, RuntimeError):
            self.close()
            raise
        if data.get("error"):
            raise ValueError(str(data["error"])[:1000])
        return data


class Cache:
    """Only disposable, app-owned job directories. Existing source files are never deleted."""
    def __init__(self, root, cap=MAX_BYTES):
        self.root = Path(root) / "cache"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError("Cache root cannot be a symlink")
        self.cap = cap

    def usage(self):
        from .studio_stage import reservation,IMAGE
        size = 0
        for root, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink() and not os.path.ismount(Path(root)/d)]
            reserved=reservation(Path(root))
            size+=reserved
            for f in files:
                if reserved and f==IMAGE:continue
                p = Path(root) / f
                if not p.is_symlink():
                    try:
                        size += p.stat().st_size
                    except FileNotFoundError:
                        # A completed job can disappear while the UI reads usage.
                        continue
        return size

    def budget(self):
        return max(0, min(self.cap - self.usage(), shutil.disk_usage(self.root).free - MIN_FREE))

    def remove(self, path, studio_finished=False):
        path = Path(path)
        if path.is_symlink() or path.parent.resolve() != self.root.resolve() or not path.name.startswith("job-"):
            raise ValueError("Refusing to clean a path outside owned job cache")
        if (path / ".frameatlas-owned").is_file() and not (path / ".frameatlas-owned").is_symlink():
            from .studio_stage import release, retire_work
            studio = release(path,external_finished=studio_finished)
            for root,dirs,files in os.walk(path,followlinks=False):
                if any(os.path.ismount(Path(root)/d) for d in dirs):
                    raise ValueError('Refusing to remove a job containing an unverified mounted volume')
                dirs[:]=[d for d in dirs if not (Path(root)/d).is_symlink()]
            shutil.rmtree(path)
            if studio:
                retire_work(path)

    def janitor(self):
        for p in self.root.glob("job-*"):
            if p.is_dir() and not p.is_symlink():
                # Only when worker singleton lock is held (no live worker can own these).
                try:self.remove(p)
                except ValueError as exc:
                    # Keep a busy external transfer intact; other read-only work can proceed.
                    print('Owned cache needs cleanup: '+p.name+': '+str(exc)[:300],flush=True)

    @contextlib.contextmanager
    def job(self):
        p = Path(tempfile.mkdtemp(prefix="job-", dir=self.root))
        (p / ".frameatlas-owned").write_text("Disposable FrameAtlas working directory\n")
        try:
            yield p
        except BaseException as original:
            try:
                self.remove(p)
            except (OSError, ValueError, subprocess.TimeoutExpired) as cleanup:
                if isinstance(original, Exception):
                    raise RuntimeError(f"{original}. Temporary cleanup also needs attention: {cleanup}") from original
                print("Temporary cleanup needs attention: " + str(cleanup)[:300], flush=True)
            raise
        else:
            self.remove(p)


def resolve(asset, google_selection=None):
    from urllib.parse import quote
    existing = local_file_available(asset.get("local_path"))
    result = {"provider": asset["provider"], "source_id": asset["source_id"], "filename": asset["filename"],
              "cloud_url": asset["cloud_url"], "existing_local_available": existing,
              "local_path": asset["local_path"] if existing else None}
    if asset["provider"] == "google_photos":
        result["search_url"] = "https://photos.google.com/search/" + quote(asset["filename"], safe="")
        selection = google_selection or {
            "state": "unknown" if asset["locator"].get("session_id") else "missing", "expires": ""}
        result["google_selection"] = selection
        result["needs_selection"] = not existing and selection["state"] in {"missing", "pending", "expired", "closed"}
        if existing:
            result["download_method"] = "Reuse the existing original read-only."
        elif result["needs_selection"]:
            result["download_method"] = "Select this file in Google Picker and import the selection before retrieval."
        elif selection["state"] == "unknown":
            result["download_method"] = "Try the saved Picker selection. If access has ended, select this file again."
        else:
            result["download_method"] = "Download this selected Google Photos rendition into your editing folder."
    elif asset["provider"] == "insta360":
        name = asset["locator"].get("recording_name", asset["source_id"])
        result["studio_recording"] = name
        result["download_method"] = (
            "Reuse the existing original read-only." if existing else
            "Queue analysis or editing retrieval. The job provides a quota-limited temporary destination. In signed-in Studio → Cloud Media, download this exact recording there and leave Set as default OFF. Existing Studio-owned downloads remain untouched."
        )
    return result


class WorkerBusy(ValueError):
    pass


class Worker:
    def __init__(self, store):
        import fcntl
        self.store = store
        self.lock = open(store.root / "worker.lock", "a+")
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise WorkerBusy("Another FrameAtlas worker is already processing this catalogue")
        self.cache = Cache(store.root)
        self.analysis_downloads = AnalysisDownloads(store.root)
        self.stop = threading.Event()
        self.draining = threading.Event()
        self.thread = None
        store.recover()
        self.cache.janitor()
        from .studio_stage import guard_previous_destinations
        try:
            guard_previous_destinations(store, self.cache.root)
        except (OSError, ValueError) as exc:
            print('Could not protect all retired Studio paths: '+str(exc)[:300],flush=True)

    def start(self):
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def state(self, jid, stage="", state=None, error=""):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            previous = db.execute("""
                SELECT j.stage,j.updated FROM jobs j JOIN assets a ON a.id=j.asset_id
                WHERE j.id=? AND j.state='running' AND j.task='analysis' AND a.provider='google_photos'
                """, (jid,)).fetchone()
            if previous is not None:
                if state is None and previous["stage"] == stage:
                    return
                phase = analysis_phase(previous["stage"] or "")
                if phase:
                    key = "$.phase_seconds." + phase
                    elapsed = max(0, now - previous["updated"])
                    db.execute("""UPDATE jobs SET result=json_set(result,?,
                        round(coalesce(json_extract(result,?),0)+?,3)) WHERE id=? AND json_valid(result)""",
                        (key, key, elapsed, jid))
            if state:
                db.execute("UPDATE jobs SET state=?,stage=?,error=?,updated=? WHERE id=?", (state, stage, error[:2000], now, jid))
            else:
                db.execute("UPDATE jobs SET stage=?,updated=? WHERE id=?", (stage, now, jid))

    def cancelled(self, jid):
        if self.stop.is_set():
            return True
        with self.store.connect() as db:
            r = db.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()
        return not r or r[0] == "cancelling"

    def source(self, asset, work, jid, prefer_cloud=False, for_analysis=True, *, batch_limit=None,
               during_studio_wait=False):
        local = asset.get("local_path")
        local_available = local_file_available(local)
        if local_available and not prefer_cloud:
            return Path(local), LOCAL_ACQUISITION
        session = asset["locator"].get("session_id")
        if asset["provider"] == "google_photos" and local and not local_available:
            if self.store.picker_access(session)["state"] in ("expired", "closed", "pending", "missing"):
                raise PickerSelectionUnavailable(session or "")
        if asset["provider"] == "google_photos" and session:
            budget = self.cache.budget()
            if for_analysis:
                budget=max(0,budget-ANALYSIS_RESERVE)
            if budget < 1024 ** 2 or asset["bytes"] > budget:
                raise ValueError("Insufficient temporary storage budget; use a smaller source or a local read-only original")
            dest = staging_destination(asset, work)
            cancelled = lambda: self.cancelled(jid)
            try:
                if for_analysis:
                    transfer_options = {"timeout": 20, "require_size": True} if during_studio_wait else {}
                    self.analysis_downloads.fetch(session, asset["source_id"], dest, budget, cancelled,
                                                  batch_limit=batch_limit, **transfer_options)
                else:
                    broker(self.store.root, "download", session, asset["source_id"], dest, budget,
                           timeout=1900, cancelled=cancelled)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                if self.store.picker_access(session)["state"] in ("expired", "closed", "pending"):
                    raise PickerSelectionUnavailable(session) from exc
                raise
            return dest, PICKER_ACQUISITION
        if asset['provider']=='insta360':
            from .studio_stage import acquire
            return acquire(self,asset,work,jid,for_analysis)
        raise ValueError("Original is not available. " + resolve(asset).get("download_method", "Attach a local original or select cloud media."))

    def prepare_editor_source(self, asset, work, jid, clip, destination):
        from .fingerprint import IndexedSourceMismatch
        from .retrieval import handoff

        local = asset.get("local_path")
        existing = local_file_available(local)
        if not existing and not destination:
            raise ValueError("Choose an editing destination before retrieving a cloud source")
        source, acquisition = self.source(asset, work, jid, for_analysis=False)
        existing = acquisition == LOCAL_ACQUISITION
        self.state(jid, "Preparing full source for the editor")
        cancelled = lambda: self.cancelled(jid)
        try:
            return handoff(asset, source, self.store.root, destination,
                           temporary=not existing, cancelled=cancelled, clip=clip)
        except IndexedSourceMismatch as mismatch:
            if not existing or asset["provider"] != "google_photos":
                raise
            state = self.store.picker_access(asset["locator"].get("session_id"))["state"]
            if state not in ("selected", "unknown"):
                raise IndexedSourceMismatch(f"{mismatch} Refresh the Google Photos selection to try its cloud copy.") from mismatch
            if not destination:
                raise IndexedSourceMismatch(f"{mismatch} Supply an editing folder to try the selected cloud copy.") from mismatch

        self.state(jid, "Linked file differs · downloading selected Google Photos source")
        source, _ = self.source(asset, work, jid, prefer_cloud=True, for_analysis=False)
        self.state(jid, "Checking selected Google Photos source for editing")
        try:
            result = handoff(asset, source, self.store.root, destination,
                             temporary=True, cancelled=cancelled, clip=clip)
        except IndexedSourceMismatch as mismatch:
            raise IndexedSourceMismatch(f"The selected Google Photos copy also differs. {mismatch}") from mismatch
        result["local_source_mismatch_recovered"] = True
        return result

    def run_job(self, job, *, before_analysis=None):
        jid = job["id"]
        if not self.store.claim_job(jid, respect_pause=True):
            return
        saved_handoff = None
        saved_speech = False
        try:
            asset = self.store.get(job["asset_id"])
            opts = json.loads(job["options"])
            # Snapshot once per job. A UI change never expands an active transfer's allowance.
            self.cache.cap = self.store.storage_limit()
            with self.cache.job() as work:
                if self.cancelled(jid):
                    raise RuntimeError("Job cancelled")
                if job.get("task") == "retrieve":
                    from .retrieval import requested_reference
                    clip = requested_reference(asset, opts.get("reference", {}))
                    result = self.prepare_editor_source(asset, work, jid, clip, opts.get("destination", ""))
                    stage=self.store.job(jid)['result'].get('studio_download')
                    if stage:result['studio_download']=stage
                    locator_patch = {}
                    if not result["existing_original_reused"] and asset["provider"] == "insta360":
                        if result.get("recording_parts"):
                            locator_patch["recording_parts"] = result["recording_parts"]
                        published = {Path(path).name: path for path in result["related_files"]}
                        for key in ("proxy", "second_lens", "secondLens"):
                            previous = asset["locator"].get(key)
                            if previous:
                                locator_patch[key] = published.get(Path(previous).name)
                    self.store.record_handoff(jid, asset, result, locator_patch)
                    saved_handoff = result
                elif job.get("task") == "speech":
                    self.retry_speech(asset, work, jid, opts)
                    saved_speech = True
                else:
                    self.analyze_job(asset, work, jid, opts, before_analysis=before_analysis)
            result = self.store.job(jid)["result"]
            if result.get("studio_download"):
                result["studio_download"]["state"] = "removed"
                self.store.job_result(jid, result)
            stage = "Ready to import into your editor" if saved_handoff else "Finished · temporary media removed"
            self.state(jid, stage, "done")
        except Exception as exc:
            if saved_handoff is not None:
                self.state(jid, "Ready to import · temporary cleanup needs attention", "done", str(exc))
                return
            if saved_speech:
                self.state(jid, "Transcript saved · temporary cleanup needs attention", "done", str(exc))
                return
            cancelled = self.cancelled(jid)
            if not cancelled and isinstance(exc, PickerSelectionUnavailable):
                if self.store.defer_picker_acquisition(jid, exc.session):
                    return
            self.state(jid, "Cancelled" if cancelled else "Needs attention", "cancelled" if cancelled else "failed", str(exc))

    def retry_speech(self, asset, work, jid, opts):
        from .fingerprint import verify_indexed_source
        from .multipart import repair_speech
        from .native import probe_media
        from .speech import speech_absence, transcribe
        from .store import require_speech_retry

        require_speech_retry(asset, opts.get("replace", False))
        model = opts["model"]
        saved = asset["analysis"]
        cancelled = lambda: self.cancelled(jid)

        def read(native, source, expected, stage, part_filename=None):
            probe = probe_media(source)
            if (source.stat().st_size != expected["source_bytes"]
                    or any(probe.get(key) != expected["probe"].get(key) for key in ("duration", "video", "audio_streams"))):
                raise ValueError("The available source differs from the indexed rendition. Run full analysis or choose the indexed source.")
            verification = verify_indexed_source(asset, source, cancelled, part_filename=part_filename)
            item, _ = native.import_source(source)
            stage("Transcribing speech locally · " + model)
            try:
                speech = transcribe(native, item, probe, model, opts["language"], stage)
            except RuntimeError as exc:
                absence = speech_absence(str(exc))
                if cancelled() or not absence:
                    raise
                return {}, absence, verification
            return speech, "complete", verification

        with Native(work, cancelled) as native:
            native.check_speech_model(model)
            source, acquisition = self.source(asset, work, jid, opts["prefer_cloud"])
            if saved.get("parts"):
                def read_part(path, expected, filename, index):
                    prefix = f"Part {index+1}/{len(saved['parts'])} · "
                    with Cache(work).job() as part_work, Native(part_work, cancelled) as session:
                        return read(session, path, expected, lambda label: self.state(jid, prefix+label), filename)
                speech, report, evidence = repair_speech(asset, source, model, read_part)
                verification = "matched" if all(part["verification"] == "matched" for part in evidence) else "no_fingerprint"
            else:
                speech, status, verification = read(native, source, saved, lambda label: self.state(jid, label))
                report = {"speech_status": status, "speech_model": model, "speech_error": ""}
                evidence = []
        if cancelled():
            raise RuntimeError("Job cancelled")
        retry = {"acquisition": acquisition, "previous_error": saved.get("speech_error", ""),
                 "previous_model": saved.get("speech_model", ""), "indexed_source_verification": verification,
                 "completed_at": time.time(), "offline_inference": True}
        if evidence:
            retry["parts"] = evidence
        self.store.record_speech_retry(jid, asset, speech, {**report, "speech_retry": retry})

    def analyze_job(self, asset, work, jid, opts, *, before_analysis=None):
        source, acquisition = self.source(asset, work, jid, opts.get("prefer_cloud", False))
        self.state(jid, "Preparing native analysis")
        if before_analysis and acquisition == PICKER_ACQUISITION:
            before_analysis()
        self.analyze_source(asset, work, jid, opts, source, acquisition)

    def analyze_source(self, asset, work, jid, opts, source, acquisition):
        from .multipart import analyze
        scenes,speech,provenance=analyze(asset,source,work,opts,lambda text:self.state(jid,text),lambda:self.cancelled(jid),Native)
        self.save_analysis(asset, work, jid, source, acquisition, scenes, speech, provenance)

    def save_analysis(self, asset, work, jid, source, acquisition, scenes, speech, provenance):
        from .store import calibrated_preview
        if self.cancelled(jid):
            raise RuntimeError("Job cancelled")
        detected_360=bool(provenance.get('is_360'))
        provenance.update({"acquisition": acquisition, "source_bytes": provenance.get('source_bytes',source.stat().st_size),
                           "is_360": bool(asset["metadata"].get("is_360")) or detected_360, "review_status": "unverified"})
        if not provenance.get("parts") and not any(asset["locator"].get(k) for k in ("recording_parts", "second_lens", "secondLens")):
            from .fingerprint import digest_source
            try:
                evidence = digest_source(source, lambda: self.cancelled(jid))
            except OSError:
                evidence = None
            if evidence and evidence["bytes"] == provenance["source_bytes"]:
                provenance["file_fingerprint"] = evidence
        if provenance['is_360'] and not detected_360:
            provenance['coverage_warning']='Source was flagged 360°, but native calibrated VR properties could not be validated. Directional analysis is required.'
        preview_file=provenance.pop('_preview_file','')
        found_faces=provenance.pop('_faces',None)
        thumb = asset.get('thumbnail','')
        spherical = provenance['is_360']
        if spherical and not calibrated_preview(asset['analysis']):
            thumb = ''
        if spherical and (not preview_file or not calibrated_preview(provenance)):
            provenance['preview_error'] = 'No calibrated 360 preview available'
        elif asset["kind"] != "audio":
            self.state(jid, "Saving a small local preview")
            thumbs = self.store.root / "thumbnails"
            thumbs.mkdir(exist_ok=True)
            out = thumbs / (asset["id"] + ".jpg")
            staged=thumbs/(asset['id']+'.next-'+uuid.uuid4().hex+'.jpg')
            try:
                preview=work/preview_file if preview_file and Path(preview_file).name==preview_file else source
                if spherical and preview==source:
                    raise ValueError('Invalid calibrated 360 preview path')
                probe = provenance.get("probe", {}) if preview==source else {}
                dims = probe.get("video") or probe.get("still") or probe.get("image") or {}
                longest = max(float(dims.get("width", 0)), float(dims.get("height", 0)))
                scale = min(1.0, 512 / longest) if longest > 0 else (1.0 if preview!=source else 0.08)
                p = subprocess.run([CLI, "frames", str(preview), "--at", "0", "--scale", str(scale), "--out", str(staged)], capture_output=True, timeout=90)
                if p.returncode == 0 and staged.is_file() and staged.stat().st_size < 2 * 1024 ** 2:
                    os.replace(staged,out)
                    thumb = out.name
                else:
                    provenance['preview_error']='Native preview conversion failed; prior preview retained'
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                provenance['preview_error']=str(exc)[:1000]
            finally:
                if staged.exists():staged.unlink()
        if spherical and thumb:
            provenance['preview_projection'] = 'calibrated_direction'
        self.store.analysis(asset["id"], scenes, speech, provenance, thumb, job_id=jid)
        if found_faces:
            # full-resolution faces replace the preview's; a failure here never fails the analysis
            try:
                from . import people
                with self.store.connect() as db:
                    people.record(self.store, db, asset["id"], found_faces["source"], found_faces["pictures"], found_faces["model"])
            except Exception as exc:
                print("FrameAtlas: faces not stored for", asset["id"], str(exc)[:300], flush=True)

    def loop(self):
        from .lookahead import Lookahead
        with Lookahead(self, Native) as lookahead:
            while not self.stop.is_set():
                try:
                    if lookahead.consume():
                        continue
                    if self.draining.is_set():
                        break
                    if self.store.queue_state()["paused"]:
                        self.stop.wait(1)
                        continue
                    # A corrupt global setting must not fail every pending media job.
                    self.store.storage_limit()
                    jobs = self.store.next_jobs()
                    if self.draining.is_set():
                        break
                    if jobs:
                        if jobs[0].get("studio_batch"):
                            from .studio_batch import run
                            run(self, jobs[0]["studio_batch"])
                        else:
                            batch = compatible(jobs, video_limit=self.analysis_downloads.maximum_parallel_videos)
                            if len(batch) > 1:
                                run_batch(self, batch, Native, before_analysis=lookahead.start)
                            else:
                                self.run_job(jobs[0], before_analysis=lookahead.start)
                    else:
                        self.stop.wait(1)
                except Exception as exc:
                    print("Worker error (will retry safely): " + str(exc)[:200], flush=True)
                    self.stop.wait(3)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=15)
        if not self.thread or not self.thread.is_alive():
            self.analysis_downloads.close()
            self.lock.close()


def main():
    import signal
    from .store import Store
    worker = Worker(Store())
    def stop(*unused):
        worker.stop.set()
    def drain(*unused):
        worker.draining.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGUSR1, drain)
    print("FrameAtlas local worker running. SIGUSR1 drains active work and exits; Ctrl-C stops safely.", flush=True)
    try:
        worker.loop()
    finally:
        worker.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        raise SystemExit("FrameAtlas: " + str(exc))
