"""People: the faces in the catalogue's media, fingerprinted locally and grouped into people you
can name, correct and search for.

FilmCraft finds the faces (YuNet), aligns them by their landmarks and fingerprints them
(AuraFace-v1, 512 numbers, on the GPU); `faces.group` groups the fingerprints. Nothing leaves the
Mac. Faces come from the stored previews (every analysed item has one) and, when an original is
read during analysis, from the full-resolution picture or video frames, which replace the
preview's faces for that item.

Grouping keeps what the owner said: a face the owner confirmed stays with its person, a face the
owner removed from a person never goes back to them, and a new group takes the person most of
its faces already belonged to, so names survive regrouping. Groups of fewer than three faces get
no person (passers-by), and the owner can hide a person without deleting anything.
"""
import base64
import contextlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import tempfile
import time

DIM = 512
BATCH = 64
MIN_GROUP = 3
THRESHOLD = 0.45
NEIGHBOURS = 50
SOURCES = ("preview", "original", "frame")
PROGRESS_KEY = "face_scan"


def ensure(db):
    """The people tables (the catalogue creates them on open; this covers a connection to an
    older file). Runs its script only when they are missing, so it never ends a transaction."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='face_scans'").fetchone():
        return
    db.executescript(SCHEMA)


SCHEMA = """
        CREATE TABLE IF NOT EXISTS people (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0,
          cover_face INTEGER, notes TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS faces (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
          source TEXT NOT NULL, at REAL, bbox TEXT NOT NULL, landmarks TEXT NOT NULL,
          score REAL NOT NULL, side REAL NOT NULL, frontal REAL NOT NULL,
          embedding BLOB NOT NULL, model TEXT NOT NULL,
          person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
          confirmed INTEGER NOT NULL DEFAULT 0, crop TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS faces_asset ON faces(asset_id);
        CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id);
        CREATE TABLE IF NOT EXISTS face_rejects (
          face_id INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
          person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE, PRIMARY KEY(face_id, person_id));
        CREATE TABLE IF NOT EXISTS face_scans (
          asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE, source TEXT NOT NULL,
          model TEXT NOT NULL, faces INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '', scanned REAL NOT NULL,
          PRIMARY KEY(asset_id, source));
"""


def crops_dir(store):
    d = Path(store.root) / "faces"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def pack(b64):
    """A base64 fingerprint (512 little-endian f32) as 512 half floats; None when malformed."""
    if not isinstance(b64, str) or len(b64) > 4000:
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except ValueError:
        return None
    if len(raw) != DIM * 4:
        return None
    vals = struct.unpack("<%df" % DIM, raw)
    norm = math.sqrt(sum(v * v for v in vals))
    if not math.isfinite(norm) or not 0.5 < norm < 1.5:
        return None
    return struct.pack("<%de" % DIM, *vals)


def unpack(blob):
    return struct.unpack("<%de" % DIM, blob)


def _int(value, name):
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 12:
        value = int(value)
    if type(value) is not int or value < 1:
        raise ValueError(f"Choose a {name}")
    return value


def record(store, db, asset_id, source, pictures, model):
    """Store an item's faces from `media.faces` pictures (a list; each may carry `at`, seconds into
    a video), replacing that source's earlier faces; returns how many were kept. The crops move
    into the catalogue's faces folder."""
    if isinstance(pictures, dict):
        pictures = [pictures]
    if source not in SOURCES:
        raise ValueError("Unknown face source")
    old = [r[0] for r in db.execute("SELECT crop FROM faces WHERE asset_id=? AND source=?", (asset_id, source))]
    db.execute("DELETE FROM faces WHERE asset_id=? AND source=?", (asset_id, source))
    for name in old:
        with contextlib.suppress(OSError):
            if name:
                (crops_dir(store) / name).unlink()
    kept = 0
    now = time.time()
    errors = [str(p.get("error")) for p in pictures if isinstance(p, dict) and p.get("error")]
    faces = [(f, p.get("at")) for p in pictures[:16] if isinstance(p, dict) for f in (p.get("faces") or [])[:64] if isinstance(f, dict)]
    for face, at in faces[:256]:
        at = float(at) if type(at) in (int, float) and math.isfinite(at) and at >= 0 else None
        blob = pack(face.get("embedding"))
        bbox, landmarks = face.get("bbox"), face.get("landmarks")
        nums = [face.get("score"), face.get("side"), face.get("frontal")]
        if (blob is None or not isinstance(bbox, list) or len(bbox) != 4 or not isinstance(landmarks, list)
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in nums + bbox)):
            continue
        cur = db.execute("""INSERT INTO faces(asset_id,source,at,bbox,landmarks,score,side,frontal,embedding,model,created)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (asset_id, source, at, json.dumps(bbox), json.dumps(landmarks)[:2000],
                                              float(nums[0]), float(nums[1]), float(nums[2]), blob, model, now))
        fid = cur.lastrowid
        crop = face.get("crop")
        if isinstance(crop, str) and crop:
            src = Path(crop)
            if src.is_file() and not src.is_symlink() and src.stat().st_size < 512 * 1024:
                dest = f"{fid}.jpg"
                shutil.move(str(src), crops_dir(store) / dest)
                db.execute("UPDATE faces SET crop=? WHERE id=?", (dest, fid))
        kept += 1
    db.execute("""INSERT INTO face_scans(asset_id,source,model,faces,error,scanned) VALUES(?,?,?,?,?,?)
        ON CONFLICT(asset_id,source) DO UPDATE SET model=excluded.model,faces=excluded.faces,error=excluded.error,scanned=excluded.scanned""",
               (asset_id, source, model, kept, "; ".join(errors)[:500], now))
    # a full-resolution read replaces the preview's faces
    if source != "preview" and kept:
        for (name,) in db.execute("SELECT crop FROM faces WHERE asset_id=? AND source='preview'", (asset_id,)).fetchall():
            with contextlib.suppress(OSError):
                if name:
                    (crops_dir(store) / name).unlink()
        db.execute("DELETE FROM faces WHERE asset_id=? AND source='preview'", (asset_id,))
    return kept


def model_id(native):
    status = native.run("faces.status")
    if not status.get("installed"):
        raise RuntimeError("The face models are not installed. Run faces.downloadModel (and redact.downloadModels) in FilmCraft first.")
    return status["fingerprints"]["id"]


def set_progress(store, **values):
    with store.connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (PROGRESS_KEY,)).fetchone()
        current = json.loads(row[0]) if row else {}
        current.update(values, updated=time.time())
        db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (PROGRESS_KEY, json.dumps(current)))


def progress(store):
    with store.connect() as db:
        ensure(db)
        row = db.execute("SELECT value FROM settings WHERE key=?", (PROGRESS_KEY,)).fetchone()
        state = json.loads(row[0]) if row else {}
        counts = db.execute("""SELECT (SELECT count(*) FROM assets WHERE thumbnail<>'') previews,
            (SELECT count(DISTINCT asset_id) FROM face_scans) scanned, (SELECT count(*) FROM faces) faces,
            (SELECT count(*) FROM faces WHERE person_id IS NOT NULL) grouped,
            (SELECT count(*) FROM people WHERE hidden=0) people, (SELECT count(*) FROM people WHERE name<>'') named""").fetchone()
    return {**dict(counts), "scan": state}


def scan_previews(store, native, *, limit=None, batch=BATCH, stop=lambda: False, lease=None):
    """Find and fingerprint the faces in stored previews not yet scanned with this model."""
    model = model_id(native)
    thumbs = Path(store.root) / "thumbnails"
    done = found = 0
    t0 = time.time()
    while not stop():
        with store.connect() as db:
            ensure(db)
            rows = db.execute("""SELECT a.id, a.thumbnail FROM assets a WHERE a.thumbnail<>''
                AND NOT EXISTS(SELECT 1 FROM face_scans s WHERE s.asset_id=a.id AND s.model=?)
                ORDER BY a.captured DESC, a.id LIMIT ?""", (model, batch)).fetchall()
        if limit is not None:
            rows = rows[:max(0, limit - done)]
        if not rows:
            break
        paths, ids = [], []
        with store.connect() as db:
            for aid, name in rows:
                p = thumbs / name
                if "/" in name or not p.is_file() or p.is_symlink():
                    record(store, db, aid, "preview", [{"faces": [], "error": "Preview file missing"}], model)
                    continue
                paths.append(str(p)); ids.append(aid)
        if not paths:
            continue
        with tempfile.TemporaryDirectory(prefix="faces-", dir=store.root) as tmp:
            with (lease() if lease else contextlib.nullcontext()):
                result = native.run("media.faces", {"paths": paths, "cropsDir": tmp}, timeout=600)
            pictures = result.get("pictures") or []
            with store.connect() as db:
                for aid, pic in zip(ids, pictures):
                    found += record(store, db, aid, "preview", [pic] if isinstance(pic, dict) else [], model)
        done += len(rows)
        set_progress(store, state="scanning", done=done, faces=found, seconds=round(time.time() - t0),
                     rate=round(done / max(1, time.time() - t0), 1))
        if limit is not None and done >= limit:
            break
    return {"scanned": done, "faces": found, "seconds": round(time.time() - t0, 1), "model": model}


def quality(row):
    return float(row["score"]) * min(float(row["side"]), 160.0) / 160.0 * (0.5 + float(row["frontal"]) / 2)


def regroup(store, native, *, threshold=THRESHOLD, min_group=MIN_GROUP):
    """Group every face of the current model into people, keeping names and corrections."""
    model = model_id(native)
    with store.connect() as db:
        ensure(db)
        rows = db.execute("SELECT id, person_id, confirmed, embedding FROM faces WHERE model=? ORDER BY id", (model,)).fetchall()
        rejects = {}
        for fid, pid in db.execute("SELECT face_id, person_id FROM face_rejects"):
            rejects.setdefault(fid, set()).add(pid)
    if not rows:
        return {"faces": 0, "people": 0}
    with tempfile.NamedTemporaryFile(prefix="prints-", suffix=".f32", dir=store.root, delete=False) as f:
        for r in rows:
            f.write(struct.pack("<%df" % DIM, *unpack(r["embedding"])))
        path = f.name
    try:
        result = native.run("faces.group", {"path": path, "threshold": threshold, "k": NEIGHBOURS}, timeout=1800)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    labels = result.get("labels")
    if not isinstance(labels, list) or len(labels) != len(rows):
        raise RuntimeError("Face grouping returned an unexpected answer")
    groups = {}
    for r, label in zip(rows, labels):
        groups.setdefault(label, []).append(r)
    now = time.time()
    assigned = created = 0
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for members in groups.values():
            votes = {}
            for r in members:
                if r["person_id"] is not None:
                    votes[r["person_id"]] = votes.get(r["person_id"], 0) + (3 if r["confirmed"] else 1)
            best = max(votes.items(), key=lambda kv: (kv[1], -kv[0]), default=(None, 0))
            pid = best[0] if best[0] is not None and best[1] >= max(1, 0.3 * len(members)) else None
            if pid is None and len(members) >= min_group:
                pid = db.execute("INSERT INTO people(created,updated) VALUES(?,?)", (now, now)).lastrowid
                created += 1
            for r in members:
                if r["confirmed"]:
                    continue
                target = pid if pid is not None and pid not in rejects.get(r["id"], ()) else None
                if target != r["person_id"]:
                    db.execute("UPDATE faces SET person_id=? WHERE id=?", (target, r["id"]))
                if target is not None:
                    assigned += 1
        # unnamed people left without faces go; named and hidden ones stay
        db.execute("""DELETE FROM people WHERE name='' AND hidden=0 AND notes=''
            AND NOT EXISTS(SELECT 1 FROM faces f WHERE f.person_id=people.id)""")
        for (pid,) in db.execute("SELECT id FROM people").fetchall():
            cover = db.execute("""SELECT id, score, side, frontal, confirmed FROM faces WHERE person_id=? AND crop<>''
                ORDER BY confirmed DESC, score * min(side,160) * (0.5 + frontal/2) DESC LIMIT 1""", (pid,)).fetchone()
            db.execute("UPDATE people SET cover_face=?, updated=? WHERE id=?", (cover["id"] if cover else None, now, pid))
        total = db.execute("SELECT count(*) FROM people WHERE hidden=0").fetchone()[0]
    set_progress(store, grouped_at=now, groups=result.get("groups"), group_seconds=result.get("seconds"))
    return {"faces": len(rows), "assigned": assigned, "new_people": created, "people": total,
            "groups": result.get("groups"), "seconds": result.get("seconds")}


# ---- the owner's view and corrections

def list_people(store, *, include_hidden=False, named_only=False, limit=200, offset=0, min_faces=MIN_GROUP):
    limit, offset = _int(limit, "limit") if limit else 200, int(offset or 0)
    if limit > 500 or offset < 0:
        raise ValueError("Use a limit up to 500")
    clauses = [] if include_hidden else ["p.hidden=0"]
    if named_only:
        clauses.append("p.name<>''")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with store.connect() as db:
        ensure(db)
        rows = db.execute(f"""SELECT p.*, count(f.id) faces, count(DISTINCT f.asset_id) assets,
                min(a.captured) first_seen, max(a.captured) last_seen,
                (SELECT crop FROM faces WHERE id=p.cover_face) cover
            FROM people p LEFT JOIN faces f ON f.person_id=p.id LEFT JOIN assets a ON a.id=f.asset_id
            {where} GROUP BY p.id HAVING faces>=? OR p.name<>''
            ORDER BY p.name='' , assets DESC, p.id LIMIT ? OFFSET ?""", (min_faces, limit, offset)).fetchall()
        total = db.execute(f"SELECT count(*) FROM people p {where}").fetchone()[0]
    return {"items": [dict(r) for r in rows], "total": total}


def _person(db, pid):
    row = db.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise ValueError("Person not found")
    return row


def person(store, pid, *, limit=60, offset=0):
    pid = _int(pid, "person")
    limit = min(_int(limit, "limit"), 200)
    with store.connect() as db:
        ensure(db)
        p = dict(_person(db, pid))
        faces = [dict(r) for r in db.execute("""SELECT f.id, f.asset_id, f.source, f.at, f.bbox, f.score, f.side, f.frontal,
                f.confirmed, f.crop, a.filename, a.captured, a.kind, a.thumbnail
            FROM faces f JOIN assets a ON a.id=f.asset_id WHERE f.person_id=?
            ORDER BY f.confirmed DESC, a.captured DESC LIMIT ? OFFSET ?""", (pid, limit, int(offset or 0)))]
        counts = db.execute("SELECT count(*), count(DISTINCT asset_id) FROM faces WHERE person_id=?", (pid,)).fetchone()
    for f in faces:
        f["bbox"] = json.loads(f["bbox"])
    return {**p, "faces": faces, "face_count": counts[0], "asset_count": counts[1]}


def update_person(store, pid, *, name=None, hidden=None, notes=None):
    pid = _int(pid, "person")
    with store.connect() as db:
        ensure(db)
        _person(db, pid)
        if name is not None:
            if not isinstance(name, str) or len(name) > 120:
                raise ValueError("A name of up to 120 characters")
            db.execute("UPDATE people SET name=?, updated=? WHERE id=?", (name.strip(), time.time(), pid))
        if hidden is not None:
            if not isinstance(hidden, bool):
                raise ValueError("hidden must be true or false")
            db.execute("UPDATE people SET hidden=?, updated=? WHERE id=?", (int(hidden), time.time(), pid))
        if notes is not None:
            if not isinstance(notes, str) or len(notes) > 4000:
                raise ValueError("Notes of up to 4000 characters")
            db.execute("UPDATE people SET notes=?, updated=? WHERE id=?", (notes, time.time(), pid))
        return dict(_person(db, pid))


def merge(store, into, other):
    into, other = _int(into, "person"), _int(other, "person")
    if into == other:
        raise ValueError("Choose two different people")
    with store.connect() as db:
        ensure(db)
        db.execute("BEGIN IMMEDIATE")
        keep, gone = _person(db, into), _person(db, other)
        moved = db.execute("UPDATE faces SET person_id=?, confirmed=1 WHERE person_id=?", (into, other)).rowcount
        db.execute("UPDATE OR IGNORE face_rejects SET person_id=? WHERE person_id=?", (into, other))
        if not keep["name"] and gone["name"]:
            db.execute("UPDATE people SET name=? WHERE id=?", (gone["name"], into))
        db.execute("DELETE FROM people WHERE id=?", (other,))
        db.execute("UPDATE people SET updated=? WHERE id=?", (time.time(), into))
    return {"person": into, "moved_faces": moved}


def assign_face(store, face_id, person_id=None, *, new_person=False):
    """Put a face with a person (confirmed), take it away from its person (never to return), or
    start a new person with it."""
    fid = _int(face_id, "face")
    with store.connect() as db:
        ensure(db)
        db.execute("BEGIN IMMEDIATE")
        face = db.execute("SELECT id, person_id FROM faces WHERE id=?", (fid,)).fetchone()
        if face is None:
            raise ValueError("Face not found")
        if new_person:
            now = time.time()
            target = db.execute("INSERT INTO people(created,updated) VALUES(?,?)", (now, now)).lastrowid
        elif person_id is None:
            target = None
        else:
            target = _int(person_id, "person")
            _person(db, target)
        if face["person_id"] is not None and face["person_id"] != target:
            db.execute("INSERT OR IGNORE INTO face_rejects(face_id,person_id) VALUES(?,?)", (fid, face["person_id"]))
        if target is not None:
            db.execute("DELETE FROM face_rejects WHERE face_id=? AND person_id=?", (fid, target))
        db.execute("UPDATE faces SET person_id=?, confirmed=? WHERE id=?", (target, int(target is not None), fid))
    return {"face": fid, "person": target}


def asset_people(store, asset_id):
    with store.connect() as db:
        ensure(db)
        rows = db.execute("""SELECT f.id face_id, f.person_id, f.bbox, f.source, f.at, f.crop, p.name, p.hidden
            FROM faces f LEFT JOIN people p ON p.id=f.person_id WHERE f.asset_id=? ORDER BY f.id""", (asset_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["bbox"] = json.loads(d["bbox"])
        out.append(d)
    return out


def names_by_asset(db, asset_ids=None):
    """Named, visible people per asset."""
    ensure(db)
    out = {}
    for aid, name in db.execute("""SELECT DISTINCT f.asset_id, p.name FROM faces f JOIN people p ON p.id=f.person_id
            WHERE p.name<>'' AND p.hidden=0"""):
        if asset_ids is None or aid in asset_ids:
            out.setdefault(aid, set()).add(name)
    return out


def forget(store):
    """Delete every face, person and correction (the media and its analysis stay)."""
    with store.connect() as db:
        ensure(db)
        n = db.execute("SELECT count(*) FROM faces").fetchone()[0]
        db.execute("DELETE FROM face_rejects")
        db.execute("DELETE FROM faces")
        db.execute("DELETE FROM people")
        db.execute("DELETE FROM face_scans")
        db.execute("DELETE FROM settings WHERE key=?", (PROGRESS_KEY,))
    shutil.rmtree(crops_dir(store), ignore_errors=True)
    return {"forgotten_faces": n}


def main():
    import argparse
    import fcntl
    from .native import Native
    from .store import Store
    from .resources import gpu_lease
    p = argparse.ArgumentParser(description="Find faces in stored previews and group them into people")
    p.add_argument("action", choices=["scan", "group", "status"])
    p.add_argument("--limit", type=int)
    args = p.parse_args()
    store = Store()
    if args.action == "status":
        print(json.dumps(progress(store), indent=2))
        return
    lock = open(Path(store.root) / "face-scan.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("A face scan is already running")
    work = Path(tempfile.mkdtemp(prefix="face-work-", dir=store.root))
    try:
        with Native(work) as native:
            if args.action == "scan":
                set_progress(store, state="scanning", started=time.time(), error="")
                out = scan_previews(store, native, limit=args.limit, lease=lambda: gpu_lease(note="FrameAtlas face scan", timeout=600))
                set_progress(store, state="grouping", **out)
            out = regroup(store, native)
            set_progress(store, state="done", **{k: v for k, v in out.items() if k != "seconds"})
            print(json.dumps(out))
    except Exception as exc:
        set_progress(store, state="failed", error=str(exc)[:500])
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
        lock.close()


if __name__ == "__main__":
    main()
