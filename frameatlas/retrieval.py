"""Explicit editing handoff. Never copy the whole library or expire editor dependencies."""
import errno
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid

from .native import probe_media

RESERVE = 3 * 1024 ** 3
REFERENCE_FIELDS = ("scene_key", "part_filename", "speech_key")


def requested_reference(asset, options):
    with_scene = "scene_key" in options or "part_filename" in options
    if "speech_key" in options:
        if with_scene:
            raise ValueError("Choose a scene reference or a transcript reference")
        return speech_reference(asset, options["speech_key"])
    if with_scene:
        return clip_reference(asset, options.get("scene_key"), options.get("part_filename", ""))
    return None


def clip_reference(asset, key, part_filename=""):
    """Describe the current scene in original-file seconds as well as catalogue seconds."""
    if not isinstance(key, str) or not key or len(key) > 160:
        raise ValueError("Provide a valid scene key")
    if not isinstance(part_filename, str) or len(part_filename) > 512:
        raise ValueError("Invalid citation recording part")
    matches = [scene for scene in asset["scenes"] if scene["scene_key"] == key
               and (not part_filename or (scene["details"].get("source_part") or {}).get("filename") == part_filename)]
    if len(matches) != 1:
        raise ValueError("The scene key is stale or ambiguous. Reopen the scene and use its current key and recording part.")
    scene = matches[0]
    detail = scene["details"]
    sample = detail.get("sample_time")
    return {"scene_key": key, "kind": "360_sample" if sample is not None else "scene",
            **source_reference(asset, scene["start"], scene["end"], detail.get("source_part"), sample)}


def speech_reference(asset, key):
    from .store import speech_key

    match = re.fullmatch(r"speech:([0-9]{1,6}):([0-9]{1,6}):[0-9a-f]{64}", key) if isinstance(key, str) and len(key) <= 85 else None
    if not match:
        raise ValueError("Provide the speech key from a current transcript search match")
    first, last = map(int, match.groups())
    words = asset["analysis"].get("speech", {}).get("words", [])
    if not 0 <= first <= last < min(200000, len(words)) or last-first >= 128 or speech_key(words, first, last) != key:
        raise ValueError("The transcript match is stale. Search again to use the current speech match.")
    start, end = words[first], words[last]
    part = start.get("source_part") or {}
    identity = (part.get("index"), part.get("filename"))
    for word in words[first:last+1]:
        origin = word.get("source_part") or {}
        if (origin.get("index"), origin.get("filename")) != identity:
            raise ValueError("The transcript reference crosses a camera part boundary")
    return {"speech_key": key, "kind": "speech",
            "matched_text": " ".join(str(w.get("text", "")) for w in words[first:last+1])[:500],
            **source_reference(asset, start.get("start"), end.get("end"),
                               {**part, "end": end["source_part"].get("end")} if part else None)}


def source_reference(asset, start, end, part, sample=None):
    from .multipart import seconds

    start, end = seconds(start), seconds(end)
    if not part and (asset["locator"].get("recording_parts") or asset["analysis"].get("parts")):
        raise ValueError("This reference has no recorded mapping to an original camera part")
    source_start, source_end = start, end
    source_filename = asset["filename"]
    source_path = None
    local = asset.get("local_path")
    if part:
        source_filename = part.get("filename")
        if not isinstance(source_filename, str) or not source_filename or Path(source_filename).name != source_filename:
            raise ValueError("The scene has no valid source part filename")
        source_start, source_end = seconds(part.get("start")), seconds(part.get("end"))
        if local:
            from .multipart import package
            try:
                source_path = next((p["path"] for p in package(asset, local) if p["filename"] == source_filename), None)
            except (OSError, ValueError):
                pass  # The stable filename remains usable for retrieving the full group.
    elif local and Path(local).is_file():
        source_path = local
    if source_end < source_start or end < start:
        raise ValueError("The indexed reference has an invalid time range")
    if sample is not None:
        at = seconds(sample)
        source_at = seconds(part.get("sample_time")) if part else at
        if not (start <= at <= end and source_start <= source_at <= source_end):
            raise ValueError("The indexed 360 sample is outside the scene range")
        start = end = at
        source_start = source_end = source_at
    return {"unit": "seconds", "start": start, "end": end, "source_filename": source_filename,
            "source_start": source_start, "source_end": source_end, "source_path": source_path}


def destination_folder(data_root, value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("Choose an existing editing folder")
    folder = Path(value).expanduser().resolve(strict=True)
    root = Path(data_root).resolve()
    if folder == root or root in folder.parents:
        raise ValueError("Editing media must be outside FrameAtlas's disposable data/cache directory")
    if not folder.is_dir():
        raise ValueError("Editing destination must be an existing folder")
    return folder


def media_probe(source):
    p = probe_media(source)
    if not (p.get("video") or p.get("audio_streams") or str(p.get("kind", "")).lower() in ("still", "image", "picture")):
        raise ValueError("The source is not a supported media file; it will not be copied")
    return p


def verify_legacy_properties(asset, source, actual, part_filename=None):
    """Reject known differences without claiming that matching properties identify the bytes."""
    from .fingerprint import IndexedSourceMismatch, indexed_part

    if part_filename is None:
        analysis = asset["analysis"]
        size = analysis.get("source_bytes")
    else:
        part = indexed_part(asset, part_filename)
        analysis = part.get("analysis")
        size = part.get("bytes")
    analysis = analysis if isinstance(analysis, dict) else {}
    expected = analysis.get("probe")
    expected = expected if isinstance(expected, dict) else {}

    def positive_int(value):
        return value if type(value) is int and value > 0 else None

    def kind(probe):
        value = probe.get("kind")
        return value.casefold() if isinstance(value, str) and value else None

    def dimensions(probe):
        picture = probe.get("video")
        if not isinstance(picture, dict):
            return None
        width, height = positive_int(picture.get("width")), positive_int(picture.get("height"))
        return (width, height) if width and height else None

    checks = (
        ("file size", positive_int(size) or positive_int(expected.get("file_size")), source.stat().st_size),
        ("media kind", kind(expected), kind(actual)),
        ("duration", positive_int(expected.get("duration")), positive_int(actual.get("duration"))),
        ("frame dimensions", dimensions(expected), dimensions(actual)),
    )
    checked = []
    for field, before, now in checks:
        if before is None or now is None:
            continue
        if before != now:
            raise IndexedSourceMismatch(f"The available file's {field} differs from the indexed rendition. "
                                        "Choose the indexed source or run full analysis on this file.")
        checked.append(field)
    return checked


def filename(asset):
    # Bounded UTF-8 basename; no path separators, shell text or hidden-file names.
    name = Path(asset["filename"]).name
    name = "".join(c if c.isalnum() or c in ". _-()" else "_" for c in name).strip(" .") or "media"
    suffix = Path(name).suffix[:12]
    stem = name[:-len(suffix)] if suffix else name
    stem = stem.encode("utf-8")[:150].decode("utf-8", errors="ignore")
    return asset["id"][:8] + "_" + uuid.uuid4().hex[:12] + "_" + stem + suffix


def publish(source, folder, asset, cancelled=lambda: False, target_name=None):
    """Publish only a complete new file, without overwriting any existing file."""
    source, folder = Path(source), Path(folder)
    if target_name is not None:
        if not isinstance(target_name,str) or Path(target_name).name!=target_name or target_name.startswith('.') or '\\' in target_name or len(target_name.encode('utf-8'))>240:
            raise ValueError('Invalid editor source filename')
    target = folder / (target_name or filename(asset))
    if cancelled():
        raise RuntimeError("Job cancelled")
    # Same-volume selected cloud downloads can be handed off without a second full copy.
    try:
        os.link(source, target)
        return target
    except FileExistsError:
        raise ValueError("Editing destination already contains this output; nothing was overwritten")
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise ValueError("Cannot atomically publish to this editing folder: " + str(exc))
    # Cross-volume copy: complete an owned staging file, then publish with a non-overwriting
    # hard link on that destination volume. Unsupported filesystems fail safely.
    total = source.stat().st_size
    modified = source.stat().st_mtime_ns
    if shutil.disk_usage(folder).free < total + RESERVE:
        raise ValueError("Editing destination lacks space plus the 3 GiB reserve")
    fd, name = tempfile.mkstemp(prefix=".frameatlas-part-", dir=folder)
    part = Path(name)
    try:
        deadline = time.monotonic() + 1200
        with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
            written = 0
            while True:
                if cancelled() or time.monotonic() > deadline:
                    raise RuntimeError("Job cancelled")
                block = inp.read(1024 * 1024)
                if not block:
                    break
                if written + len(block) > total:
                    raise ValueError("Source grew during retrieval; no output was published")
                if shutil.disk_usage(folder).free < len(block) + RESERVE:
                    raise ValueError("Stopped copying to preserve free disk space")
                out.write(block); written += len(block)
            out.flush(); os.fsync(out.fileno())
        if written != total or source.stat().st_mtime_ns != modified:
            raise ValueError("Source changed during retrieval; no output was published")
        if cancelled():
            raise RuntimeError("Job cancelled")
        os.link(part, target)
        return target
    finally:
        part.unlink(missing_ok=True)


def handoff(asset, source, data_root, destination="", temporary=False, cancelled=lambda: False, clip=None):
    from .fingerprint import IndexedSourceMismatch, verify_indexed_source
    source = Path(source).resolve(strict=True)
    probe = media_probe(source)
    if cancelled():
        raise RuntimeError("Job cancelled")
    from .multipart import package
    parts=package(asset,source)
    verification = None
    if clip:
        source_files = {part["filename"]: Path(part["path"]) for part in parts} if parts else {asset["filename"]: source}
        clip_filename = clip["source_filename"]
        if clip_filename not in source_files:
            raise ValueError("The selected reference does not match the retrieved recording")
        clip_source = source_files[clip_filename]
        part_filename = clip_filename if parts else None
        verification = verify_indexed_source(asset, clip_source, cancelled, part_filename=part_filename)
        if verification == "no_fingerprint":
            clip_probe = probe if clip_source == source else media_probe(clip_source)
            legacy_checks = verify_legacy_properties(asset, clip_source, clip_probe, part_filename)
    elif asset["provider"] == "google_photos":
        try:
            verification = verify_indexed_source(asset, source, cancelled)
        except IndexedSourceMismatch:
            # A whole-file request can still use the current copy. Keep the
            # indexed source link separate and disclose the difference.
            verification = "different_bytes"
    related = []
    locator=asset.get('locator',{})
    dependencies=[{k:locator.get(k) for k in ('second_lens','secondLens','proxy')},*parts]
    for entry in dependencies:
        for key in ('path','second_lens','secondLens','proxy'):
            path=entry.get(key) if key!='path' or parts else None
            if path and Path(path).is_file() and str(Path(path).resolve()) not in related and Path(path).resolve()!=source:
                related.append(str(Path(path).resolve()))
    if temporary and asset['provider']=='insta360':
        # Keep raw camera basenames together so the editor can find paired lenses/LRV.
        # Only this explicitly selected recording package persists outside the cache.
        folder=destination_folder(data_root,destination)/('FrameAtlas-'+asset['id'][:8]+'-'+uuid.uuid4().hex[:12])
        folder.mkdir(mode=0o700)
        published=[]
        try:
            source.chmod(0o600)
            output=publish(source,folder,asset,cancelled,target_name=source.name);published.append(output)
            deps=[]
            for value in related:
                dep=Path(value);media_probe(dep);dep.chmod(0o600)
                path=publish(dep,folder,asset,cancelled,target_name=dep.name);published.append(path);deps.append(str(path))
            related=deps
        except Exception:
            for path in published:path.unlink(missing_ok=True)
            try:folder.rmdir()
            except OSError:pass  # Never remove files another actor put in this folder.
            raise
    elif temporary:
        folder = destination_folder(data_root, destination)
        source.chmod(0o600)  # Only our temporary cloud file; existing originals stay untouched.
        output = publish(source, folder, asset, cancelled)
    else:
        output = source
    result={"asset_id": asset["id"], "source_id": asset["source_id"], "provider": asset["provider"],
            "filename": asset["filename"], "path": str(output), "related_files": related,
            "existing_original_reused": not temporary, "cloud_rendition_downloaded": temporary and asset['provider']=='google_photos',
            "cloud_source_downloaded":temporary,"studio_source_downloaded":temporary and asset['provider']=='insta360',
            "bytes": output.stat().st_size, "probe": probe,
            "retention": "Editing source retained. FrameAtlas never auto-deletes this file. Import this path into your editor; it is not a temporary cache path."}
    if parts:
        published={Path(p).name:str(p) for p in [str(output),*related]}
        result['recording_parts']=[{'filename':part['filename'],**{key:published[Path(part[key]).name] for key in ('path','proxy','second_lens','secondLens') if part.get(key) and Path(part[key]).name in published}} for part in parts]
        result['bytes']=sum(Path(p['path']).stat().st_size for p in result['recording_parts'])
    if clip:
        path = published[clip["source_filename"]] if parts else result["path"]
        result["clip"] = {**clip, "source_path": path}
        if verification == "no_fingerprint":
            result["indexed_source_properties_checked"] = legacy_checks
    if verification is not None:
        result["indexed_source_verification"] = verification
    return result
