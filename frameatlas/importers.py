"""Metadata-only inventory importers. Do not copy, delete, or recursively scan originals."""
import datetime
import hashlib
import json
import mimetypes
import itertools
from pathlib import Path
import re
import tarfile
import zipfile
from urllib.parse import urlparse

from .native import cli

MAX_JSON = 4 * 1024 * 1024
MAX_RECORDS = 100000
TAKEOUT_ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar")
MAX_TAKEOUT_ARCHIVES = 32


def kind_for(name, mime=""):
    mime = mime or mimetypes.guess_type(name)[0] or ""
    if mime.startswith("video/") or Path(name).suffix.lower() in (".insv", ".lrv"):
        return "video"
    if mime.startswith("image/") or Path(name).suffix.lower() == ".insp":
        return "photo"
    if mime.startswith("audio/"):
        return "audio"
    return "unknown"


def cloud_url(value):
    value = str(value or "")
    u = urlparse(value)
    # Only stable human-facing links, never expiring bearer/signed download URLs.
    if u.scheme == "https" and u.hostname in ("photos.google.com", "photos.app.goo.gl", "cloud.insta360.com", "www.insta360.com") and not u.username:
        return value[:2048]
    return ""


def insta_source_id(db, filename, legacy_stem=None):
    """Reuse deterministic file IDs, including legacy recording-stem identities."""
    stem = legacy_stem or filename.rsplit('.',1)[0]
    for source_id in (filename, stem):
        old = db.execute("SELECT filename FROM assets WHERE provider='insta360' AND source_id=?",(source_id,)).fetchone()
        if old and old['filename'].casefold()==filename.casefold():
            return source_id
    return filename


def import_native(store):
    photos = cli("photos", "cached", "--json")
    insta = cli("insta360", "list", "--json").get("recordings", [])
    counts = {"google_photos": 0, "insta360": 0}
    with store.connect() as db:
        for p in photos[:MAX_RECORDS]:
            store.upsert({"provider": "google_photos", "source_id": p["id"], "filename": p["filename"],
                          "kind": kind_for(p["filename"]), "captured": p.get("created", ""), "bytes": p.get("bytes", 0),
                          "local_path": p.get("path", ""), "locator": {"picker_id": p["id"], "filename": p["filename"]},
                          "metadata": {"inventory": "FilmCraft selected-item cache", "date_source": "Google Picker createTime", "gps_status": "unavailable_from_picker"}}, db)
            counts["google_photos"] += 1
        for p in insta[:MAX_RECORDS]:
            filename = Path(p['path']).name
            store.upsert({"provider": "insta360", "source_id": insta_source_id(db,filename,p['stem']), "filename": filename,
                          "kind": p["kind"], "captured": p.get("shot", ""), "bytes": p.get("bytes", 0),
                          "local_path": p.get("path", ""), "locator": {"recording_name": p["stem"], "proxy": p.get("proxy"), "second_lens": p.get("secondLens") or p.get("second_lens")},
                          "metadata": {"camera": p.get("camera"), "is_360": True, "complete": p.get("complete"), "inventory": "Studio local downloads only", "date_source": "Camera filename (timezone unknown)"}}, db)
            counts["insta360"] += 1
        store.imported("google_photos", "AutoCut selected items", "Selected items already known to AutoCut; NOT the full Google Photos library.", counts["google_photos"], db)
        store.imported("insta360", "Studio download inventory", "Local Studio recordings only; NOT the full Insta360 Cloud library.", counts["insta360"], db)
    recovered = recover_photos_metadata(store)
    counts["metadata_only"] = recovered["seen"]
    counts["metadata_only_added"] = recovered["added"]
    return counts


def recover_photos_metadata(store, root=None):
    """Recover saved selection records even after their cached media were removed.

    Only Native Picker sidecars inside its known media cache are considered. Auth files,
    symlinks, wrong-ID records and arbitrary JSON payloads are never imported.
    """
    root = Path(root) if root is not None else Path.home() / "Library/Application Support/FilmCraft/google-photos/cache"
    if root.is_symlink():
        raise ValueError("Photos metadata cache cannot be a symlink")
    if not root.is_dir():
        return {"seen": 0, "added": 0, "skipped": 0}
    items, skipped = {}, 0
    forbidden = {"tokens.json", "client_secret.json", "signin.json", "control-token.json"}
    for directory in itertools.islice(root.iterdir(), MAX_RECORDS):
        if directory.is_symlink() or not directory.is_dir():
            continue
        for p in itertools.islice(directory.iterdir(), 128):
            if p.is_symlink() or not p.is_file() or not p.name.endswith(".json") or p.name.startswith('.') or p.name in forbidden:
                continue
            original = p.with_name(p.name[:-5])
            if original.is_file():
                continue
            try:
                if p.stat().st_size > 65536:
                    skipped += 1; continue
                meta = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(meta, dict) or meta.get("googlePhotosId") != directory.name or not isinstance(meta.get("title"), str):
                    skipped += 1; continue
                title = meta["title"][:512]
                if not title:
                    skipped += 1; continue
                created = str(meta.get("created") or "")[:80]
                mime = str(meta.get("mimeType") or "")[:150]
                items[directory.name] = {"provider": "google_photos", "source_id": directory.name,
                    "filename": title, "kind": kind_for(title, mime), "captured": created,
                    "locator": {"picker_id": directory.name, "filename": title},
                    "metadata": {"inventory": "FilmCraft saved selection sidecar", "date_source": "Saved Google Picker createTime",
                                 "gps_status": "not_provided_by_picker", "mime_type": mime,
                                 "sidecar_original_available": False}}
                if len(items) >= MAX_RECORDS:
                    break
            except (OSError, ValueError, UnicodeError):
                skipped += 1
    added = 0
    values = list(items.values())
    for i in range(0, len(values), 250):
        with store.connect() as db:
            for item in values[i:i+250]:
                _, new = store.upsert(item, db); added += int(new)
    with store.connect() as db:
        store.imported("google_photos", "Saved metadata without cached originals",
                       "Previously selected native sidecars only. No originals downloaded or copied; full cloud library not scanned.", len(values), db)
    return {"seen": len(values), "added": added, "skipped": skipped}


def import_manifest(store, payload):
    items = payload.get("items")
    if not isinstance(items, list) or not items or len(items) > MAX_RECORDS:
        raise ValueError("Manifest needs 1–100,000 items")
    added = 0
    with store.connect() as db:
        for raw in items:
            if not isinstance(raw, dict):
                raise ValueError("Each manifest item must be an object")
            # Strict allow-list: secrets, signed URLs and arbitrary internal fields are discarded.
            item = {k: raw[k] for k in ("provider", "source_id", "filename", "kind", "captured", "bytes", "duration") if k in raw}
            item["cloud_url"] = cloud_url(raw.get("cloud_url"))
            item["kind"] = item.get("kind") or kind_for(item.get("filename", ""))
            item["metadata"] = {"inventory": "User metadata manifest", "is_360": bool(raw.get("is_360")), "location": raw.get("location"), "date_source": "manifest"}
            if raw.get("local_path"):
                item["local_path"] = str(Path(raw["local_path"]).expanduser().absolute())
            item["locator"] = {k: raw[k] for k in ("recording_name", "picker_id") if k in raw}
            _, new = store.upsert(item, db)
            added += int(new)
        store.imported("mixed", str(payload.get("label", "Metadata manifest")), "Only the items in this manifest. Media remain at their source.", len(items), db)
    return {"seen": len(items), "added": added}


def takeout_item(meta, source, occurrence):
    from .geography import coordinates

    title = meta.get("title")
    if not title or not ("photoTakenTime" in meta or "creationTime" in meta):
        return None
    timestamp = (meta.get("photoTakenTime") or meta.get("creationTime") or {}).get("timestamp", "")
    try:
        captured = datetime.datetime.fromtimestamp(int(timestamp), datetime.timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        captured = ""
    url = cloud_url(meta.get("url"))
    parsed = urlparse(url)
    photo_link = parsed.hostname == "photos.google.com" and re.fullmatch(r"/photo/[A-Za-z0-9_-]+/?", parsed.path)
    # Only direct photo permalinks identify the same item across sidecar occurrences.
    identity_key = "https://photos.google.com" + parsed.path.rstrip("/") if photo_link else occurrence
    identity = "takeout:" + hashlib.sha256(identity_key.encode()).hexdigest()
    geo = next((info for info in (meta.get("geoDataExif"), meta.get("geoData"))
                if isinstance(info, dict) and coordinates((info.get("latitude"), info.get("longitude")))), None)
    return {"provider": "google_photos", "source_id": identity, "filename": str(title), "kind": kind_for(str(title)),
            "captured": captured, "cloud_url": url, "locator": {"filename": str(title), "takeout_sidecar": source,
                          "takeout_identity": "photo_permalink" if photo_link else "sidecar_occurrence"},
            "metadata": {"inventory": "Google Takeout metadata", "gps": geo, "date_source": "Takeout photoTakenTime" if meta.get("photoTakenTime") else "Takeout creationTime", "original_description": str(meta.get("description", ""))[:4000]}}


def import_takeout(store, path):
    if not isinstance(path, (str, Path)) or not str(path).strip() or len(str(path)) > 4096:
        raise ValueError("Enter a local Takeout archive or folder path")
    path = Path(path).expanduser().resolve(strict=True)
    if path.is_dir():
        archives = []
        for i, entry in enumerate(path.iterdir()):
            if i >= 4096:
                raise ValueError("Choose a folder with at most 4,096 entries")
            if (entry.name.lower().endswith(TAKEOUT_ARCHIVE_SUFFIXES)
                    and not entry.is_symlink() and entry.is_file()):
                archives.append(entry)
                if len(archives) > MAX_TAKEOUT_ARCHIVES:
                    raise ValueError(f"Choose a folder with at most {MAX_TAKEOUT_ARCHIVES} Takeout archives")
        if not archives:
            raise ValueError("No Takeout archives found directly inside this folder")
        results = []
        for archive in sorted(archives):
            try:
                results.append({"filename": archive.name, **import_takeout_file(store, archive)})
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as exc:
                results.append({"filename": archive.name, "error": str(exc)[:500]})
        successful = [result for result in results if "error" not in result]
        return {"seen": sum(result["seen"] for result in successful),
                "added": sum(result["added"] for result in successful), "originals_extracted": 0,
                "archives": results, "failed_archives": len(results) - len(successful)}
    return import_takeout_file(store, path)


def import_takeout_file(store, path):
    if not path.is_file():
        raise ValueError("Choose a Takeout archive, folder of archives, or individual JSON sidecar")
    count = added = 0
    # Read sidecars in place. A streaming tar may traverse media payloads, but none are extracted.
    def consume(name, offset, data, db):
        nonlocal count, added
        if count >= MAX_RECORDS:
            raise ValueError("Import cap reached; split the export into smaller archives")
        if len(data) > MAX_JSON:
            return
        try:
            origin = str(path) if offset is None else path.name
            occurrence = json.dumps([origin, name, offset, hashlib.sha256(data).hexdigest()])
            item = takeout_item(json.loads(data), name, occurrence)
        except (ValueError, TypeError, AttributeError):
            return
        if item:
            if offset is not None:
                item["locator"].update(takeout_archive=path.name, takeout_offset=offset)
            _, new = store.upsert(item, db)
            count += 1; added += int(new)
    with store.connect() as db:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as arc:
                if len(arc.infolist()) > 500000:
                    raise ValueError("Archive has too many members")
                for entry in arc.infolist():
                    if entry.filename.endswith(".json") and 0 < entry.file_size <= MAX_JSON:
                        with arc.open(entry) as f:
                            consume(entry.filename, entry.header_offset, f.read(MAX_JSON + 1), db)
        elif path.name.lower().endswith(TAKEOUT_ARCHIVE_SUFFIXES[1:]):
            with tarfile.open(path, "r|*") as arc:
                for i, entry in enumerate(arc):
                    if i > 500000:
                        raise ValueError("Archive has too many members")
                    if entry.isfile() and entry.name.endswith(".json") and 0 < entry.size <= MAX_JSON:
                        f = arc.extractfile(entry)
                        if f:
                            with f:
                                consume(entry.name, entry.offset_data, f.read(MAX_JSON + 1), db)
        elif path.suffix.lower() == ".json":
            if path.stat().st_size > MAX_JSON:
                raise ValueError("Sidecar is too large")
            consume(path.name, None, path.read_bytes(), db)
        else:
            raise ValueError("Supported: .zip, .tar.gz, .tgz, .tar or JSON sidecar")
        store.imported("google_photos", path.name, "Takeout sidecars in this archive only. No original media extracted. Match Picker items explicitly before analysis.", count, db)
    return {"seen": count, "added": added, "originals_extracted": 0}
