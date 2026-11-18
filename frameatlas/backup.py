"""Portable, verified catalogue backups. Standard library only; no cloud credentials."""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile

FORMAT = "frameatlas-backup"
VERSION = 1
CHUNK = 1024 * 1024
MAX_FILES = 200000
MAX_TOTAL = 64 * 1024 ** 3
MAX_MANIFEST = 32 * 1024 ** 2
TABLES = ("assets", "scenes", "jobs", "imports", "ideas", "settings", "picker_sessions", "search")
INSTRUCTIONS = """FrameAtlas portable catalogue backup, version 1

Contains the SQLite catalogue and, unless omitted at creation, referenced small
JPEG previews. Original media, model weights, OAuth tokens, passwords, app caches
and temporary downloads are excluded. Keep this archive private.

Restore with a FrameAtlas checkout (Python 3.9+; no extra packages):
  ./run.sh restore /absolute/path/backup.zip --to /absolute/path/NEW-data-folder

Or extract ONLY restore_frameatlas.py from this archive, then run:
  python3 restore_frameatlas.py restore /absolute/path/backup.zip --to /absolute/path/NEW-data-folder

The destination must not exist. Restore validates every payload checksum and the
SQLite database before publishing the folder. It never replaces an existing app.
The restored queue starts PAUSED. Computer-specific local source associations
are cleared by default; historical provenance, stable IDs and filenames remain.
Use --keep-local-paths only for a deliberate recovery on the same computer.

Start the new instance from its FrameAtlas code checkout:
  FRAMEATLAS_DATA=/absolute/path/NEW-data-folder ./run.sh serve --port 7319

Search, descriptions, transcripts, tags, corrections and saved ideas work offline.
Reconnect Google Photos/Insta360 and install local models separately to retrieve
cloud originals or analyse new files. Google Picker access expires; this backup
does not extend it. Relink original files if their paths changed.

Inspect without restoring:
  python3 restore_frameatlas.py inspect /absolute/path/backup.zip

Checksums detect corruption, not a maliciously replaced archive. Use a trusted copy.
"""


def default_root():
    return Path(os.environ.get("FRAMEATLAS_DATA", str(Path.home() / "Library/Application Support/FrameAtlas")))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def checked_database(path):
    with contextlib.closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(TABLES).issubset(tables):
            raise ValueError("Database is not a supported FrameAtlas catalogue")
        if db.execute("PRAGMA integrity_check(20)").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Database has broken relationships")
        return {table: db.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in TABLES}


def payload_name(name):
    return name in ("catalog.sqlite3", "RESTORE.txt", "restore_frameatlas.py") or bool(
        re.fullmatch(r"thumbnails/[a-f0-9]{24}\.jpg", name))


def create_backup(output, root=None, include_previews=True):
    root = Path(root) if root is not None else default_root()
    source = root / "catalog.sqlite3"
    if source.is_symlink() or not source.is_file():
        raise ValueError("Source catalogue database does not exist or is a symlink")
    output = Path(output).expanduser().absolute()
    if os.path.lexists(output):
        raise ValueError("Backup output already exists; choose a new filename")
    if not output.parent.is_dir():
        raise ValueError("Create the backup output's parent folder first")
    with tempfile.TemporaryDirectory(prefix=".frameatlas-backup-", dir=output.parent) as folder:
        work = Path(folder)
        snapshot = work / "catalog.sqlite3"
        started = time.monotonic()
        def progress(*unused):
            if time.monotonic() - started > 600:
                raise ValueError("Snapshot timed out; retry when catalogue writes are quieter")
        with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with contextlib.closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst, pages=256, progress=progress, sleep=0.05)
                dst.execute("PRAGMA journal_mode=DELETE")
        counts = checked_database(snapshot)
        with contextlib.closing(sqlite3.connect(snapshot)) as db:
            names = [r[0] for r in db.execute("SELECT DISTINCT thumbnail FROM assets WHERE thumbnail<>''")]
        if len(names) + 3 > MAX_FILES:
            raise ValueError("Too many previews for this backup format")
        members = [("catalog.sqlite3", snapshot)]
        missing = []
        if include_previews:
            if (root / "thumbnails").is_symlink():
                raise ValueError("Thumbnail directory symlinks are not backed up")
            for name in names:
                if not isinstance(name, str) or not payload_name("thumbnails/" + name):
                    raise ValueError("Catalogue contains an invalid thumbnail filename")
                path = root / "thumbnails" / name
                if path.is_symlink():
                    raise ValueError("Thumbnail symlinks are not backed up")
                if not path.is_file():
                    missing.append(name)
                    continue
                if path.stat().st_size > 2 * CHUNK:
                    raise ValueError("A thumbnail exceeds the supported 2 MiB limit")
                members.append(("thumbnails/" + name, path))
        guide = work / "RESTORE.txt"
        guide.write_text(INSTRUCTIONS, encoding="utf-8")
        members.extend((("RESTORE.txt", guide), ("restore_frameatlas.py", Path(__file__))))
        needed = sum(path.stat().st_size for _, path in members)
        if needed > MAX_TOTAL or shutil.disk_usage(work).free < needed + CHUNK:
            raise ValueError("Insufficient disk space or backup exceeds the 64 GiB limit")
        manifest = {"format": FORMAT, "version": VERSION,
                    "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "counts": counts, "previews_included": include_previews,
                    "missing_previews": missing, "files": []}
        archive = work / "backup.zip"
        with zipfile.ZipFile(archive, "w", allowZip64=True) as z:
            total_written = 0
            for name, path in members:
                h, size = hashlib.sha256(), 0
                method = zipfile.ZIP_STORED if name.startswith("thumbnails/") else zipfile.ZIP_DEFLATED
                info = zipfile.ZipInfo(name)
                info.compress_type = method
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                with path.open("rb") as src, z.open(info, "w", force_zip64=True) as dest:
                    for block in iter(lambda: src.read(CHUNK), b""):
                        size += len(block)
                        total_written += len(block)
                        if total_written > MAX_TOTAL:
                            raise ValueError("Source grew beyond the backup limit")
                        h.update(block)
                        dest.write(block)
                manifest["files"].append({"path": name, "bytes": size, "sha256": h.hexdigest()})
            body = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            if len(body) > MAX_MANIFEST:
                raise ValueError("Backup manifest exceeds its limit")
            z.writestr("manifest.json", body, compress_type=zipfile.ZIP_DEFLATED)
        archive.chmod(0o600)
        with archive.open("rb") as f:
            os.fsync(f.fileno())
        # Link publishes atomically without replacing a concurrently-created backup.
        os.link(archive, output)
    return {"path": str(output), "bytes": output.stat().st_size, "sha256": digest(output),
            "counts": counts, "previews": len(members) - 3, "missing_previews": len(missing)}


def read_manifest(z):
    entries = z.infolist()
    if len(entries) > MAX_FILES + 1 or len({e.filename for e in entries}) != len(entries):
        raise ValueError("Archive contains too many or duplicate files")
    info = z.getinfo("manifest.json")
    if info.file_size > MAX_MANIFEST:
        raise ValueError("Backup manifest exceeds its limit")
    manifest = json.loads(z.read(info))
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or type(manifest.get("version")) is not int or manifest["version"] != VERSION:
        raise ValueError("Unsupported FrameAtlas backup format/version")
    files = manifest.get("files")
    if not isinstance(files, list) or not 3 <= len(files) <= MAX_FILES:
        raise ValueError("Invalid payload manifest")
    expected, total = {}, 0
    for member in files:
        if not isinstance(member, dict):
            raise ValueError("Invalid payload entry")
        name, size, sha = member.get("path"), member.get("bytes"), member.get("sha256")
        if not isinstance(name, str) or not payload_name(name) or name in expected:
            raise ValueError("Unsafe or duplicate payload path")
        if type(size) is not int or not 0 <= size <= MAX_TOTAL or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("Invalid payload size or checksum")
        if name.startswith("thumbnails/") and size > 2 * CHUNK:
            raise ValueError("Oversized thumbnail")
        total += size
        expected[name] = member
    if not {"catalog.sqlite3", "RESTORE.txt", "restore_frameatlas.py"}.issubset(expected) or total > MAX_TOTAL:
        raise ValueError("Missing required files or archive exceeds the 64 GiB limit")
    if {e.filename for e in entries} != set(expected) | {"manifest.json"}:
        raise ValueError("Archive contains undeclared files")
    for entry in entries:
        if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
            raise ValueError("Symlink/encrypted archive members are unsupported")
        if entry.filename in expected and entry.file_size != expected[entry.filename]["bytes"]:
            raise ValueError("Payload size differs from manifest")
    return manifest


def restore_backup(archive, destination, keep_local_paths=False):
    destination = Path(destination).expanduser().absolute()
    if os.path.lexists(destination):
        raise ValueError("Restore destination already exists; choose a new data folder")
    if not destination.parent.is_dir():
        raise ValueError("Create the restore destination's parent folder first")
    with zipfile.ZipFile(archive) as z:
        manifest = read_manifest(z)
        needed = sum(x["bytes"] for x in manifest["files"])
        if shutil.disk_usage(destination.parent).free < needed + 64 * CHUNK:
            raise ValueError("Insufficient free space to restore this catalogue")
        with tempfile.TemporaryDirectory(prefix=".frameatlas-restore-", dir=destination.parent) as folder:
            work = Path(folder)
            for entry in manifest["files"]:
                name = entry["path"]
                target = work / name
                target.parent.mkdir(exist_ok=True, mode=0o700)
                h, size = hashlib.sha256(), 0
                with z.open(name) as src, target.open("xb") as out:
                    for block in iter(lambda: src.read(CHUNK), b""):
                        size += len(block)
                        if size > entry["bytes"]:
                            raise ValueError("Payload exceeds declared size")
                        h.update(block)
                        out.write(block)
                target.chmod(0o600)
                if size != entry["bytes"] or h.hexdigest() != entry["sha256"]:
                    raise ValueError("Backup checksum mismatch: " + name)
            dbpath = work / "catalog.sqlite3"
            counts = checked_database(dbpath)
            if counts != manifest.get("counts"):
                raise ValueError("Restored row counts differ from manifest")
            with contextlib.closing(sqlite3.connect(dbpath)) as db, db:
                db.execute("INSERT INTO settings VALUES('paused','true') ON CONFLICT(key) DO UPDATE SET value='true'")
                if not keep_local_paths:
                    db.execute("UPDATE assets SET local_path='' WHERE local_path<>''")
                db.execute("UPDATE jobs SET state='failed',error='Interrupted before backup. Retry explicitly on the new instance.' WHERE state IN ('running','cancelling')")
                db.execute("UPDATE jobs SET state='cancelled',stage='Restored: choose a new editing destination' WHERE task='retrieve' AND state='queued'")
                db.execute("UPDATE assets SET status='inventory' WHERE status='analyzing'")
            checked_database(dbpath)
            # Reserve the destination exclusively. An existing directory is never replaced.
            destination.mkdir(mode=0o700)
            try:
                for child in work.iterdir():
                    os.rename(child, destination / child.name)
            except BaseException:
                shutil.rmtree(destination)
                raise
    return {"root": str(destination), "counts": counts, "queue_paused": True,
            "local_paths_kept": keep_local_paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("create")
    create.add_argument("output")
    create.add_argument("--root", default=str(default_root()))
    create.add_argument("--database-only", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("archive")
    restore.add_argument("--to", required=True)
    restore.add_argument("--keep-local-paths", action="store_true")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("archive")
    args = parser.parse_args()
    try:
        if args.action == "create":
            result = create_backup(args.output, args.root, not args.database_only)
        elif args.action == "restore":
            result = restore_backup(args.archive, args.to, args.keep_local_paths)
        else:
            with zipfile.ZipFile(args.archive) as z:
                result = read_manifest(z)
            result = {k: v for k, v in result.items() if k != "files"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        parser.exit(1, "Error: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
