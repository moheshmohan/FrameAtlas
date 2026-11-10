"""Native FilmCraft inference. No hosted inference or automatic model downloads."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import tempfile

CLI = os.environ.get("FILMCRAFT_CLI", "/Users/mohesh/Desktop/dev_work/Autocut/target/agent-main/release/filmcraft-cli")
TICKS = 254016000000


def probe_timeout(path):
    # Large camera containers require more I/O than a phone clip. Still bound every probe.
    return min(1800,60+max(0,Path(path).stat().st_size)/(64*1024**2))


def cli(*args, timeout=120):
    result = subprocess.run([CLI, *map(str, args)], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("FilmCraft: " + result.stderr[-1500:])
    return json.loads(result.stdout)


def probe_media(path, *, native=None):
    """Use the native streaming reader, rather than the CLI's whole-file byte probe."""
    path=Path(path)
    timeout=probe_timeout(path)
    def read(source):
        params = {"path": str(source)}
        if native is None:
            result = cli("exec", "mediaBrowser.probe", json.dumps(params), timeout=timeout)
        else:
            result = native.run("mediaBrowser.probe", params, timeout=timeout)
        return result.get('info') if result.get('media') and isinstance(result.get('info'),dict) else None
    info=read(path)
    if info is None:
        # The browser's extension filter may reject an extensionless cached source or LRV.
        # A disposable, read-only alias lets its native reader sniff the actual container.
        # No original bytes are copied and unlinking the alias cannot remove its target.
        with tempfile.TemporaryDirectory(prefix='FrameAtlas-probe-') as directory:
            alias=Path(directory)/'source.mov';alias.symlink_to(path.resolve(strict=True))
            info=read(alias)
    if info is None:raise RuntimeError('Native streaming probe could not recognize this media; source was retained')
    info['name']=path.name
    return info


def video_preview_time(shots):
    """Prefer the first captioned shot's sampled frame to the opening frame."""
    for shot in shots:
        if not shot.get("description", {}).get("caption"):
            continue
        start, end = shot.get("start"), shot.get("end")
        if type(start) not in (int, float) or type(end) not in (int, float) or not 0 <= start < end < float("inf"):
            continue
        keyframes = shot.get("keyframes")
        if not isinstance(keyframes, list):
            continue
        for seconds in keyframes:
            if type(seconds) in (int, float) and start <= seconds < end:
                return seconds
    return 0


class Native:
    def __init__(self, work, cancelled=lambda: False):
        self.work = Path(work)
        self.cancelled = cancelled
        self.log = open(self.work / "native.log", "w")
        # Hugging Face's offline flags are defense in depth; native inference only uses
        # already installed weights. Missing models are checked before any inference.
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        self.p = subprocess.Popen([CLI, "--project", str(self.work / "analysis.fcproj"), "mcp"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=self.log, text=True, env=env, bufsize=1)
        self.lines = queue.Queue(maxsize=10000)
        self.counter = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "frameatlas", "version": "0.1"}})
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.p.stdout:
                self.lines.put(line, timeout=5)
        except (OSError, queue.Full):
            pass
        finally:
            try:
                self.lines.put(None, timeout=1)
            except queue.Full:
                pass

    def send(self, value):
        self.p.stdin.write(json.dumps(value) + "\n")
        self.p.stdin.flush()

    def rpc(self, method, params, timeout=1800):
        self.counter += 1
        rid = self.counter
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cancelled():
                raise RuntimeError("Job cancelled")
            try:
                line = self.lines.get(timeout=0.25)
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError("Native worker stopped; see local diagnostic log")
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") == rid:
                if "error" in response:
                    raise RuntimeError(str(response["error"])[:1500])
                return response["result"]
        raise RuntimeError("Native operation timed out; retry a shorter source or proxy")

    def tool(self, name, args, *, timeout=1800):
        r = self.rpc("tools/call", {"name": name, "arguments": args}, timeout=timeout)
        text = "".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text")
        if r.get("isError"):
            raise RuntimeError(text[:1500])
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError("Native worker returned an unexpected response")

    def run(self, command, params=None, *, timeout=1800):
        return self.tool("command_run", {"id": command, "params": params or {}}, timeout=timeout)

    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(timeout=10)
        self.log.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def check_speech_model(self, model):
        models = self.run("transcript.models")
        if not any(x.get("id") == model and x.get("installed") for x in models.get("models", [])):
            raise RuntimeError("Local speech model is not installed: " + str(model))

    def import_source(self, path):
        imported = self.tool("media_import", {"paths": [str(path)]})
        items = imported.get("items", [])
        if not isinstance(items, list) or not items:
            raise RuntimeError("FilmCraft did not import the source")
        item = items[0]
        if isinstance(item, dict):
            item = item.get("id", item.get("item"))
        if type(item) is not int or item <= 0:
            raise RuntimeError("FilmCraft returned an invalid source media ID")
        return item, imported

    def analysis_settings(self, kind, options):
        detail = options.get("detail", "fast")
        if detail not in ("fast", "full"):
            raise ValueError("detail must be fast or full")
        if kind != "audio":
            status = self.run("scenes.status")
            if not status.get("available") or status.get(detail, {}).get("missingBytes", 1):
                raise RuntimeError("Install FilmCraft fast scene models first; this app never downloads weights automatically")
        # The native speech command validates the selected local model when it
        # loads it. Scanning every installed model here delays scene publication.
        return detail, options.get("model", "whisper-base")

    def describe(self, items, detail, fresh):
        return self.run("media.analyzeScenes", {"items": items, "detail": detail, "describe": True,
                                               "embed": False, "aesthetic": False, "wait": True, "fresh": fresh})

    def read_speech(self, item, probe, model, language, stage):
        from .speech import speech_absence, transcribe
        stage("Transcribing speech locally · " + model)
        try:
            return transcribe(self, item, probe, model, language, stage), "complete", ""
        except (RuntimeError, ValueError, OSError) as exc:
            if self.cancelled():
                raise RuntimeError("Job cancelled") from exc
            message = str(exc)
            absence = speech_absence(message)
            if absence:
                return {}, absence, ""
            return {}, "failed", message

    def analyze(self, path, kind, options, stage, *, indexed_media=None, speech_reader=None):
        if indexed_media is None:
            detail, model = self.analysis_settings(kind, options)
            item, imported = self.import_source(path)
            probe = probe_media(path)
            vr_report = self.run('media.vrProperties',{'items':[item]}) if kind!='audio' else {}
            vr = next((x.get('vr') for x in vr_report.get('items',[]) if x.get('item')==item),None)
            preview_work = self.work
            scene_report = {}
        else:
            item, imported, probe, scene_report, preview_work, detail, model = indexed_media
            vr = None
        scenes = []
        index_info = {}
        directional = {}
        directional_error = ''
        preview_file = ''
        if kind != "audio":
            if indexed_media is None:
                stage("Finding cuts and describing scenes locally")
                scene_report = self.describe([item], detail, bool(options.get("fresh")))
            index = self.run("media.scenes", {"item": item})
            scenes = index.get("shots", [])
            index_info = {key: value for key, value in index.items() if key != "shots"}
            if not scenes or not any(s.get("description", {}).get("caption") for s in scenes):
                raise RuntimeError("No local scene descriptions were produced; analysis was not marked complete")
            # Read media through native VR properties, not an uncalibrated LRV file.
            # Preserve native shot boundaries and append timecoded direction samples.
            if vr:
                try:
                    from .spherical import describe_directions
                    extra,directional,preview_file=describe_directions(self,item,probe,kind,detail,stage)
                    scenes.extend(extra)
                except Exception as exc:
                    if self.cancelled():raise RuntimeError('Job cancelled')
                    directional_error=str(exc)[:1500]
            if not preview_file and not vr:
                try:
                    preview_file='native-preview.png'
                    seconds = video_preview_time(scenes) if kind == "video" else 0
                    self.run('media.frame',{'item':item,'seconds':seconds,'maxSide':512,'path':str(preview_work/preview_file)})
                except Exception:
                    if self.cancelled():raise RuntimeError('Job cancelled')
                    preview_file=''
        speech = {}
        speech_status = "not_applicable"
        speech_error = ""
        if kind != "photo":
            read = speech_reader or self.read_speech
            speech, speech_status, speech_error = read(item, probe, model, options.get("language", "auto"), stage)
        provenance = {"engine": "FilmCraft native", "offline_inference": True,
                      "vision_model": ("qwen3-vl-4b" if detail == "full" else "qwen3-vl-2b") if scenes else None,
                      "cut_model": "transnetv2" if scenes else None, "speech_model": model,
                      "speech_status": speech_status, "speech_error": speech_error,
                      "scene_report": scene_report, "scene_index": index_info, "analyzed_at": time.time(), "media_kind": kind, "probe": probe,
                      "is_360":bool(vr),"vr_properties":vr,"directional_analysis":directional,"directional_error":directional_error,
                      "camera_proxy_attached":bool(imported.get('proxies')),
                      "claims": "Model descriptions are suggestions. Visible text, identities and landmarks require verification."}
        if preview_file:provenance['_preview_file']=preview_file
        if kind == "photo":
            from .geography import coordinates
            try:
                capture = self.run("media.captureDate", {"items": [item]})
                info = next((entry for entry in capture.get("items", []) if entry.get("item") == item), {})
                gps = coordinates(info.get("gps"))
                provenance["photo_gps_status"] = "recorded" if gps else "no_embedded_position_read"
                if gps:
                    provenance["embedded_gps"] = gps
            except (RuntimeError, ValueError, OSError):
                if self.cancelled():
                    raise RuntimeError("Job cancelled")
                provenance["photo_gps_status"] = "unavailable"
        elif kind == "video":
            from .geography import video_position
            status, gps = video_position(Path(path))
            provenance["video_gps_status"] = status
            if gps:
                provenance["embedded_gps"] = gps
        if vr:
            provenance['coverage_warning']=('360 directional analysis needs attention: '+directional_error) if directional_error else (
                '360 coverage uses six calibrated directions at sampled times, not continuous all-frame verification. '
                +str(directional.get('frames_missing',0))+' views lack descriptions. Events between samples may be missed.')
        return scenes, speech, provenance
