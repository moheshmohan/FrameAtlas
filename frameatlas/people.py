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
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='face_log'").fetchone():
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
        CREATE TABLE IF NOT EXISTS face_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
          status TEXT NOT NULL, face_id INTEGER, person_id INTEGER, other_id INTEGER, likeness REAL,
          detail TEXT NOT NULL DEFAULT '{}', previous TEXT NOT NULL DEFAULT '{}');
        CREATE INDEX IF NOT EXISTS face_log_status ON face_log(status, id);
        CREATE TABLE IF NOT EXISTS external_people (
          source TEXT NOT NULL, name TEXT NOT NULL, asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
          PRIMARY KEY(source, name, asset_id));
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
                    # one vote per face: a few faces the owner moved must not carry the rest of their group with them
                    votes[r["person_id"]] = votes.get(r["person_id"], 0) + 1
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

def list_people(store, *, include_hidden=False, named_only=False, limit=200, offset=0, min_faces=MIN_GROUP, query=""):
    limit, offset = _int(limit, "limit") if limit else 200, int(offset or 0)
    if limit > 500 or offset < 0:
        raise ValueError("Use a limit up to 500")
    if not isinstance(query, str) or len(query) > 120:
        raise ValueError("Search names with up to 120 characters")
    clauses, params = ([] if include_hidden else ["p.hidden=0"]), []
    query = query.strip()
    if query.lstrip("#").isdigit() and len(query) <= 12:
        clauses.append("p.id=?")
        params.append(int(query.lstrip("#")))
    elif query:
        clauses.append("p.name LIKE ? ESCAPE '\\'")
        params.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
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
            ORDER BY p.name='' , assets DESC, p.id LIMIT ? OFFSET ?""", (*params, min_faces, limit, offset)).fetchall()
        total = db.execute(f"SELECT count(*) FROM people p {where}", params).fetchone()[0]
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


MAX_FACES = 500


def _face_ids(faces):
    if not isinstance(faces, list) or not faces or len(faces) > MAX_FACES:
        raise ValueError(f"Choose 1 to {MAX_FACES} faces")
    ids = [_int(f, "face") for f in faces]
    return list(dict.fromkeys(ids))


def link_faces(store, faces, person_id=None, *, new_person=False, name=""):
    """Link faces to a person (confirmed, so regrouping keeps them there) or to a new person.
    A face moved away from another person never goes back to that person."""
    ids = _face_ids(faces)
    if not isinstance(name, str) or len(name) > 120:
        raise ValueError("A name of up to 120 characters")
    if new_person == (person_id is not None):
        raise ValueError("Choose a person or a new person")
    with store.connect() as db:
        ensure(db)
        db.execute("BEGIN IMMEDIATE")
        found = db.execute(f"SELECT id, person_id FROM faces WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
        if len(found) != len(ids):
            raise ValueError("Face not found")
        now = time.time()
        if new_person:
            target = db.execute("INSERT INTO people(name,created,updated) VALUES(?,?,?)", (name.strip(), now, now)).lastrowid
        else:
            target = _int(person_id, "person")
            _person(db, target)
        for face in found:
            if face["person_id"] is not None and face["person_id"] != target:
                db.execute("INSERT OR IGNORE INTO face_rejects(face_id,person_id) VALUES(?,?)", (face["id"], face["person_id"]))
            db.execute("DELETE FROM face_rejects WHERE face_id=? AND person_id=?", (face["id"], target))
            db.execute("UPDATE faces SET person_id=?, confirmed=1 WHERE id=?", (target, face["id"]))
        _refresh(db, {target} | {f["person_id"] for f in found}, now)
    return {"faces": ids, "person": target, "linked": len(ids)}


def unlink_faces(store, faces):
    """Take faces away from their people; regrouping never puts them back with those people."""
    ids = _face_ids(faces)
    with store.connect() as db:
        ensure(db)
        db.execute("BEGIN IMMEDIATE")
        found = db.execute(f"SELECT id, person_id FROM faces WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
        if len(found) != len(ids):
            raise ValueError("Face not found")
        for face in found:
            if face["person_id"] is not None:
                db.execute("INSERT OR IGNORE INTO face_rejects(face_id,person_id) VALUES(?,?)", (face["id"], face["person_id"]))
        db.execute(f"UPDATE faces SET person_id=NULL, confirmed=0 WHERE id IN ({','.join('?' * len(ids))})", ids)
        _refresh(db, {f["person_id"] for f in found}, time.time())
    return {"faces": ids, "unlinked": sum(f["person_id"] is not None for f in found)}


def _refresh(db, pids, now):
    """New covers for people whose faces changed (a cover that left them is replaced)."""
    for pid in pids - {None}:
        cover = db.execute("""SELECT id FROM faces WHERE person_id=? AND crop<>''
            ORDER BY confirmed DESC, score * min(side,160) * (0.5 + frontal/2) DESC LIMIT 1""", (pid,)).fetchone()
        db.execute("""UPDATE people SET cover_face=CASE WHEN cover_face IN (SELECT id FROM faces WHERE person_id=?)
            THEN cover_face ELSE ? END, updated=? WHERE id=?""", (pid, cover["id"] if cover else None, now, pid))


def assign_face(store, face_id, person_id=None, *, new_person=False):
    """One face: put it with a person, take it away from its person, or start a new person."""
    if new_person or person_id is not None:
        out = link_faces(store, [face_id], person_id, new_person=new_person)
    else:
        out = unlink_faces(store, [face_id])
    return {"face": out["faces"][0], "person": out.get("person")}


def unsorted_faces(store, *, limit=120, offset=0, min_side=40):
    """Faces with no person (passers-by and faces the grouping was unsure of), clearest first."""
    limit = min(_int(limit, "limit"), 300)
    offset = _int(offset, "offset") if offset not in (0, "0", None, "") else 0
    with store.connect() as db:
        ensure(db)
        rows = db.execute("""SELECT f.id, f.asset_id, f.crop, f.score, f.side, f.frontal, a.filename, a.captured
            FROM faces f JOIN assets a ON a.id=f.asset_id WHERE f.person_id IS NULL AND f.crop<>'' AND f.side>=?
            ORDER BY score * min(side,160) * (0.5 + frontal/2) DESC, f.id LIMIT ? OFFSET ?""", (min_side, limit, offset)).fetchall()
        total = db.execute("SELECT count(*) FROM faces WHERE person_id IS NULL AND crop<>'' AND side>=?", (min_side,)).fetchone()[0]
    return {"items": [dict(r) for r in rows], "total": total}


def suggest(store, faces, *, limit=8, sample=8):
    """The people these faces most look like: each person's clearest faces compared with the
    chosen ones (mean of the best three likenesses)."""
    ids = _face_ids(faces)[:64]
    limit = min(_int(limit, "limit"), 30)
    with store.connect() as db:
        ensure(db)
        chosen = db.execute(f"SELECT id, person_id, embedding FROM faces WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
        if not chosen:
            raise ValueError("Face not found")
        current = {r["person_id"] for r in chosen}
        # people every chosen face was taken away from are not suggested again
        rejected = None
        for r in chosen:
            mine = {pid for (pid,) in db.execute("SELECT person_id FROM face_rejects WHERE face_id=?", (r["id"],))}
            rejected = mine if rejected is None else rejected & mine
        rows = db.execute("""SELECT person_id, embedding FROM (
                SELECT f.person_id, f.embedding, ROW_NUMBER() OVER (PARTITION BY f.person_id
                    ORDER BY f.confirmed DESC, f.score * min(f.side,160) * (0.5 + f.frontal/2) DESC) n
                FROM faces f JOIN people p ON p.id=f.person_id WHERE p.hidden=0) WHERE n<=?""", (sample,)).fetchall()
        info = {r["id"]: dict(r) for r in db.execute("""SELECT p.id, p.name, (SELECT crop FROM faces WHERE id=p.cover_face) cover,
                (SELECT count(*) FROM faces WHERE person_id=p.id) faces FROM people p WHERE p.hidden=0""")}
    query = [unpack(r["embedding"]) for r in chosen]
    scores = {}
    for r in rows:
        if (len(current) == 1 and r["person_id"] in current) or r["person_id"] in rejected:
            continue
        v = unpack(r["embedding"])
        best = max(math.fsum(map(float.__mul__, q, v)) for q in query)
        scores.setdefault(r["person_id"], []).append(best)
    ranked = sorted(((sum(sorted(s, reverse=True)[:3]) / min(len(s), 3), pid) for pid, s in scores.items()), reverse=True)
    return {"items": [{**info[pid], "likeness": round(score, 3)} for score, pid in ranked[:limit] if pid in info]}


# ---- sorting on its own: confident links and merges, the rest queued for review

AUTO = {"link_at": 0.5, "link_margin": 0.1, "review_link_at": 0.45, "review_link_margin": 0.05,
        "merge_at": 0.8, "review_merge_at": 0.62, "outlier_below": 0.15}
ACTORS = ("auto", "agent", "google")
ACTIONS = ("link", "move", "unlink", "merge", "name")


def _centroids(db, sample=24):
    """Each visible person's average fingerprint (of their clearest faces), unit length."""
    acc, counts = {}, {}
    for pid, blob in db.execute("""SELECT person_id, embedding FROM (SELECT f.person_id, f.embedding,
            ROW_NUMBER() OVER (PARTITION BY f.person_id ORDER BY f.confirmed DESC,
                f.score * min(f.side,160) * (0.5 + f.frontal/2) DESC) n
            FROM faces f JOIN people p ON p.id=f.person_id WHERE p.hidden=0) WHERE n<=?""", (sample,)):
        v = unpack(blob)
        a = acc.get(pid)
        acc[pid] = list(v) if a is None else [x + y for x, y in zip(a, v)]
        counts[pid] = counts.get(pid, 0) + 1
    out = {}
    for pid, a in acc.items():
        n = math.sqrt(math.fsum(x * x for x in a))
        if n > 0:
            out[pid] = [x / n for x in a]
    return out


def _dot(a, b):
    return sum(map(float.__mul__, a, b))


def _log(db, actor, action, status, *, face=None, person=None, other=None, likeness=None, detail=None, previous=None):
    return db.execute("""INSERT INTO face_log(at,actor,action,status,face_id,person_id,other_id,likeness,detail,previous)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", (time.time(), actor, action, status, face, person, other,
                                         None if likeness is None else round(float(likeness), 4),
                                         json.dumps(detail or {}), json.dumps(previous or {}))).lastrowid


def _dismissed(db):
    """Pairs the owner or an agent said no to, so they are not proposed again."""
    out = set()
    for action, face, person, other in db.execute(
            "SELECT action, face_id, person_id, other_id FROM face_log WHERE status IN ('dismissed','undone')"):
        out.add((action, face, person, other))
        if action == "merge":
            out.add((action, face, other, person))
    return out


def _do_link(db, face_id, person_id, actor, likeness, detail=None):
    """An automatic link: only a face that still belongs to no one, to a person who still exists."""
    row = db.execute("SELECT person_id, confirmed FROM faces WHERE id=?", (face_id,)).fetchone()
    if row is None or row["confirmed"] or row["person_id"] is not None:
        return None
    if db.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone() is None:
        return None
    db.execute("UPDATE faces SET person_id=? WHERE id=?", (person_id, face_id))
    return {"person_id": row["person_id"], "confirmed": row["confirmed"]}


def _do_merge(db, into, other):
    """Merge like `merge`, but remember enough to undo it."""
    keep, gone = _person(db, into), _person(db, other)
    faces = [(r[0], r[1]) for r in db.execute("SELECT id, confirmed FROM faces WHERE person_id=?", (other,))]
    rejects = [r[0] for r in db.execute("SELECT face_id FROM face_rejects WHERE person_id=?", (other,))]
    previous = {"other": dict(gone), "faces": faces, "rejects": rejects, "into_name": keep["name"]}
    db.execute("UPDATE faces SET person_id=? WHERE person_id=?", (into, other))
    db.execute("UPDATE OR IGNORE face_rejects SET person_id=? WHERE person_id=?", (into, other))
    db.execute("DELETE FROM face_rejects WHERE person_id=?", (other,))
    if not keep["name"] and gone["name"]:
        db.execute("UPDATE people SET name=? WHERE id=?", (gone["name"], into))
    db.execute("DELETE FROM people WHERE id=?", (other,))
    _refresh(db, {into}, time.time())
    return previous


def auto_sort(store, *, apply=False, actor="auto", progress=None, **limits):
    """Sort faces on their own: merge people whose faces are clearly one person, link unsorted
    faces that clearly belong to someone, and queue everything less certain for review. Faces the
    owner confirmed and pairs already turned down are never touched. Every change is logged and
    can be undone."""
    if actor not in ACTORS:
        raise ValueError("actor must be one of " + ", ".join(ACTORS))
    bad = set(limits) - set(AUTO)
    if bad:
        raise ValueError("Unknown setting: " + sorted(bad)[0])
    t = dict(AUTO)
    for k, v in limits.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not -1 <= v <= 1:
            raise ValueError(f"{k} must be a number from -1 to 1")
        t[k] = float(v)
    out = {"merged": 0, "linked": 0, "proposed": 0, "apply": bool(apply)}
    say = progress or (lambda **kw: None)
    # people: work out the merges without holding the write lock, then write them quickly
    say(state="sorting", step="people")
    with store.connect() as db:
        ensure(db)
        dismissed = _dismissed(db)
        cents = _centroids(db)
        info = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM people")}
        size = dict(db.execute("SELECT person_id, count(*) FROM faces WHERE person_id IS NOT NULL GROUP BY person_id").fetchall())
    pids = sorted(cents)
    pairs = []
    for i, a in enumerate(pids):
        ca = cents[a]
        for b in pids[i + 1:]:
            sim = _dot(ca, cents[b])
            if sim >= t["review_merge_at"]:
                pairs.append((sim, a, b))
    pairs.sort(reverse=True)
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE face_log SET status='superseded' WHERE status='proposed' AND actor=?", (actor,))
        seen = set()
        for sim, a, b in pairs:
            a, b = find(a), find(b)
            if a == b or ("merge", None, a, b) in dismissed or frozenset((a, b)) in seen:
                continue
            seen.add(frozenset((a, b)))
            na, nb = info.get(a, ""), info.get(b, "")
            into, other = (a, b) if (na and not nb) or (bool(na) == bool(nb) and size.get(a, 0) >= size.get(b, 0)) else (b, a)
            if sim >= t["merge_at"] and not (na and nb and na != nb) and apply:
                previous = _do_merge(db, into, other)
                parent[other] = into
                size[into] = size.get(into, 0) + size.pop(other, 0)
                info[into] = info.get(into) or info.get(other, "")
                _log(db, actor, "merge", "applied", person=into, other=other, likeness=sim, previous=previous)
                out["merged"] += 1
            else:
                _log(db, actor, "merge", "proposed", person=into, other=other, likeness=sim,
                     detail={"why": "names differ" if na and nb and na != nb else "alike"})
                out["proposed"] += 1
    # faces: unsorted ones that clearly belong to someone, and grouped ones that look out of place
    say(state="sorting", step="faces")
    with store.connect() as db:
        cents = _centroids(db) if out["merged"] else cents
        rejects = {}
        for fid, pid in db.execute("SELECT face_id, person_id FROM face_rejects"):
            rejects.setdefault(fid, set()).add(pid)
        unsorted = db.execute("""SELECT id, embedding FROM faces WHERE person_id IS NULL AND confirmed=0
            AND crop<>'' AND side>=40""").fetchall()
        grouped = db.execute("SELECT id, person_id, embedding FROM faces WHERE person_id IS NOT NULL AND confirmed=0").fetchall()
    ids, vecs = list(cents), [cents[p] for p in cents]
    decisions = []
    for n, r in enumerate(unsorted):
        v = unpack(r["embedding"])
        sims = sorted(((_dot(v, c), pid) for pid, c in zip(ids, vecs) if pid not in rejects.get(r["id"], ())), reverse=True)
        if not sims or ("link", r["id"], sims[0][1], None) in dismissed:
            continue
        best, pid = sims[0]
        margin = best - (sims[1][0] if len(sims) > 1 else 0.0)
        if best >= t["link_at"] and margin >= t["link_margin"] and apply:
            decisions.append(("link", "applied", r["id"], pid, None, best, {"margin": round(margin, 4)}))
        elif best >= t["review_link_at"] and margin >= t["review_link_margin"]:
            decisions.append(("link", "proposed", r["id"], pid, None, best, {"margin": round(margin, 4)}))
        if n % 500 == 0:
            say(state="sorting", step="faces", done=n, total=len(unsorted))
    say(state="sorting", step="outliers")
    for r in grouped:
        own = cents.get(r["person_id"])
        if own is None:
            continue
        v = unpack(r["embedding"])
        mine = _dot(v, own)
        if mine >= t["outlier_below"]:
            continue
        others = sorted(((_dot(v, c), pid) for pid, c in zip(ids, vecs)
                         if pid != r["person_id"] and pid not in rejects.get(r["id"], ())), reverse=True)
        if others and others[0][0] >= t["link_at"] and others[0][0] - mine >= t["link_margin"]:
            if ("move", r["id"], others[0][1], None) not in dismissed:
                decisions.append(("move", "proposed", r["id"], others[0][1], r["person_id"], others[0][0], {"own": round(mine, 4)}))
        elif ("unlink", r["id"], r["person_id"], None) not in dismissed:
            decisions.append(("unlink", "proposed", r["id"], r["person_id"], None, mine, {}))
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        touched = set()
        for action, status, face, person, other, likeness, detail in decisions:
            if status == "applied":
                previous = _do_link(db, face, person, actor, likeness)
                if previous is None:
                    continue
                touched.add(person)
                _log(db, actor, action, status, face=face, person=person, likeness=likeness, previous=previous, detail=detail)
                out["linked"] += 1
            else:
                _log(db, actor, action, status, face=face, person=person, other=other, likeness=likeness, detail=detail)
                out["proposed"] += 1
        _refresh(db, touched, time.time())
    return out


def review(store, *, status="proposed", actor="", action="", limit=50, offset=0):
    """The sorter's log: what it changed (applied, undoable) and what it suggests (proposed)."""
    if status not in ("proposed", "applied", "undone", "dismissed", ""):
        raise ValueError("status is proposed, applied, undone, dismissed or empty")
    if actor not in ("",) + ACTORS or action not in ("",) + ACTIONS:
        raise ValueError("Unknown actor or action")
    limit = min(_int(limit, "limit"), 200)
    offset = _int(offset, "offset") if offset not in (0, "0", None, "") else 0
    clauses, params = ["l.status<>'superseded'"], []
    for col, val in (("status", status), ("actor", actor), ("action", action)):
        if val:
            clauses.append(f"l.{col}=?")
            params.append(val)
    where = " AND ".join(clauses)
    with store.connect() as db:
        ensure(db)
        rows = db.execute(f"""SELECT l.*, f.crop face_crop, f.asset_id, a.filename, a.captured,
                p.name person_name, (SELECT crop FROM faces WHERE id=p.cover_face) person_cover,
                o.name other_name, (SELECT crop FROM faces WHERE id=o.cover_face) other_cover
            FROM face_log l LEFT JOIN faces f ON f.id=l.face_id LEFT JOIN assets a ON a.id=f.asset_id
            LEFT JOIN people p ON p.id=l.person_id LEFT JOIN people o ON o.id=l.other_id
            WHERE {where} ORDER BY CASE l.action WHEN 'merge' THEN 0 WHEN 'name' THEN 1 ELSE 2 END,
                l.likeness DESC, l.id LIMIT ? OFFSET ?""", (*params, limit, offset)).fetchall()
        total = db.execute(f"SELECT count(*) FROM face_log l WHERE {where}", params).fetchone()[0]
        counts = dict(db.execute("SELECT status, count(*) FROM face_log WHERE status<>'superseded' GROUP BY status").fetchall())
    items = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail"])
        d.pop("previous", None)
        items.append(d)
    return {"items": items, "total": total, "counts": counts}


def decide(store, log_id, decision, *, actor="owner"):
    """Apply a proposal, turn it down, or undo a change the sorter made."""
    lid = _int(log_id, "log entry")
    if decision not in ("apply", "dismiss", "undo"):
        raise ValueError("decision is apply, dismiss or undo")
    with store.connect() as db:
        ensure(db)
        db.execute("BEGIN IMMEDIATE")
        e = db.execute("SELECT * FROM face_log WHERE id=?", (lid,)).fetchone()
        if e is None:
            raise ValueError("Log entry not found")
        want = {"apply": "proposed", "dismiss": "proposed", "undo": "applied"}[decision]
        if e["status"] != want:
            raise ValueError(f"Only a {want} entry can be {decision}ed" if decision != "apply" else "Only a proposed entry can be applied")
        if decision == "dismiss":
            db.execute("UPDATE face_log SET status='dismissed', at=? WHERE id=?", (time.time(), lid))
            return {"id": lid, "status": "dismissed"}
        if decision == "apply":
            previous = _apply(db, e)
            db.execute("UPDATE face_log SET status='applied', previous=?, at=?, detail=json_set(detail,'$.by',?) WHERE id=?",
                       (json.dumps(previous), time.time(), actor, lid))
            return {"id": lid, "status": "applied"}
        _undo(db, e)
        db.execute("UPDATE face_log SET status='undone', at=? WHERE id=?", (time.time(), lid))
        return {"id": lid, "status": "undone"}


def _apply(db, e):
    action, face, person, other = e["action"], e["face_id"], e["person_id"], e["other_id"]
    if action in ("link", "move"):
        _person(db, person)
        row = db.execute("SELECT person_id, confirmed FROM faces WHERE id=?", (face,)).fetchone()
        if row is None:
            raise ValueError("Face not found")
        if row["person_id"] is not None:
            db.execute("INSERT OR IGNORE INTO face_rejects(face_id,person_id) VALUES(?,?)", (face, row["person_id"]))
        db.execute("DELETE FROM face_rejects WHERE face_id=? AND person_id=?", (face, person))
        db.execute("UPDATE faces SET person_id=?, confirmed=1 WHERE id=?", (person, face))
        _refresh(db, {person, row["person_id"]}, time.time())
        return {"person_id": row["person_id"], "confirmed": row["confirmed"]}
    if action == "unlink":
        row = db.execute("SELECT person_id, confirmed FROM faces WHERE id=?", (face,)).fetchone()
        if row is None:
            raise ValueError("Face not found")
        if row["person_id"] is not None:
            db.execute("INSERT OR IGNORE INTO face_rejects(face_id,person_id) VALUES(?,?)", (face, row["person_id"]))
        db.execute("UPDATE faces SET person_id=NULL, confirmed=0 WHERE id=?", (face,))
        _refresh(db, {row["person_id"]}, time.time())
        return {"person_id": row["person_id"], "confirmed": row["confirmed"]}
    if action == "merge":
        return _do_merge(db, person, other)
    if action == "name":
        p = _person(db, person)
        name = json.loads(e["detail"]).get("name", "")
        db.execute("UPDATE people SET name=?, updated=? WHERE id=?", (name, time.time(), person))
        return {"name": p["name"]}
    raise ValueError("Unknown action")


def _undo(db, e):
    prev = json.loads(e["previous"] or "{}")
    action, face, person, other = e["action"], e["face_id"], e["person_id"], e["other_id"]
    if action in ("link", "move", "unlink"):
        row = db.execute("SELECT person_id FROM faces WHERE id=?", (face,)).fetchone()
        if row is None:
            raise ValueError("Face not found")
        before = prev.get("person_id")
        if before is not None and db.execute("SELECT 1 FROM people WHERE id=?", (before,)).fetchone() is None:
            before = None
        db.execute("UPDATE faces SET person_id=?, confirmed=? WHERE id=?", (before, int(prev.get("confirmed", 0)), face))
        if before is not None:
            db.execute("DELETE FROM face_rejects WHERE face_id=? AND person_id=?", (face, before))
        if action != "unlink" and person is not None:
            db.execute("INSERT OR IGNORE INTO face_rejects(face_id,person_id) VALUES(?,?)", (face, person))
        _refresh(db, {person, row["person_id"], before}, time.time())
        return
    if action == "merge":
        gone = prev.get("other") or {}
        if db.execute("SELECT 1 FROM people WHERE id=?", (other,)).fetchone():
            raise ValueError("That person exists again; nothing to undo")
        db.execute("""INSERT INTO people(id,name,hidden,cover_face,notes,created,updated) VALUES(?,?,?,?,?,?,?)""",
                   (other, gone.get("name", ""), int(gone.get("hidden", 0)), gone.get("cover_face"), gone.get("notes", ""),
                    gone.get("created", time.time()), time.time()))
        for fid, confirmed in prev.get("faces", []):
            db.execute("UPDATE faces SET person_id=?, confirmed=? WHERE id=? AND person_id=?", (other, confirmed, fid, person))
        for fid in prev.get("rejects", []):
            db.execute("INSERT OR IGNORE INTO face_rejects(face_id,person_id) VALUES(?,?)", (fid, other))
        if "into_name" in prev:
            db.execute("UPDATE people SET name=? WHERE id=?", (prev["into_name"], person))
        _refresh(db, {person, other}, time.time())
        return
    if action == "name":
        db.execute("UPDATE people SET name=?, updated=? WHERE id=?", (prev.get("name", ""), time.time(), person))
        return
    raise ValueError("Unknown action")


def compare(store, person, other=None, faces=None, *, n=6):
    """Face crops to look at side by side: a person's clearest faces next to another person's, or
    next to some faces, with how alike their fingerprints are. For agents that can see images."""
    pid = _int(person, "person")
    n = min(_int(n, "count"), 12)
    face_ids = _face_ids(faces)[:12] if faces is not None else []
    oid = _int(other, "person") if other is not None else None
    if oid is None and not face_ids:
        raise ValueError("Compare with another person or with some faces")
    with store.connect() as db:
        ensure(db)
        cents = _centroids(db)

        def side(x):
            p = dict(_person(db, x))
            crops = [r[0] for r in db.execute("""SELECT crop FROM faces WHERE person_id=? AND crop<>''
                ORDER BY confirmed DESC, score * min(side,160) * (0.5 + frontal/2) DESC LIMIT ?""", (x, n))]
            count = db.execute("SELECT count(*) FROM faces WHERE person_id=?", (x,)).fetchone()[0]
            return {"id": x, "name": p["name"], "faces": count, "crops": crops}
        out = {"person": side(pid)}
        images = [{"label": f"person {pid} ({out['person']['name'] or 'unnamed'})", "crop": c} for c in out["person"]["crops"]]
        if oid is not None:
            out["other"] = side(oid)
            out["likeness"] = round(_dot(cents[pid], cents[oid]), 3) if pid in cents and oid in cents else None
            images += [{"label": f"person {oid} ({out['other']['name'] or 'unnamed'})", "crop": c} for c in out["other"]["crops"]]
        if face_ids:
            q = ",".join("?" * len(face_ids))
            rows = db.execute(f"SELECT id, crop, embedding, person_id FROM faces WHERE id IN ({q})", face_ids).fetchall()
            out["faces"] = [{"id": r["id"], "crop": r["crop"], "person": r["person_id"],
                             "likeness": round(_dot(unpack(r["embedding"]), cents[pid]), 3) if pid in cents else None} for r in rows]
            images += [{"label": f"face {f['id']} (likeness {f['likeness']})", "crop": f["crop"]} for f in out["faces"] if f["crop"]]
    out["images"] = images
    return out


# ---- names from another library (Google Photos' people)

def _utc_index(db):
    index = {}
    for aid, captured, kind in db.execute("SELECT id, captured, kind FROM assets WHERE captured<>''"):
        key = captured[:19]
        if len(key) == 19:
            index.setdefault(key, []).append((aid, kind))
    return index


def match_items(db, items):
    """Catalogue items for another library's items given by local capture time to the second
    (no time zone) and kind: the time zone is found by trying every quarter-hour offset and
    preferring the offset most items agree on."""
    from datetime import datetime, timedelta
    index = _utc_index(db)
    hits, votes = [], {}
    for item in items:
        if not isinstance(item, dict):
            hits.append({})
            continue
        try:
            local = datetime.strptime(str(item.get("local", ""))[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            hits.append({})
            continue
        kind = item.get("kind")
        found = {}
        for q in range(-48, 57):
            key = (local - timedelta(minutes=15 * q)).strftime("%Y-%m-%dT%H:%M:%S")
            ids = [aid for aid, k in index.get(key, ()) if not kind or k == kind]
            if len(ids) == 1:
                found[q] = ids[0]
        hits.append(found)
        if len(found) == 1:
            q = next(iter(found))
            votes[q] = votes.get(q, 0) + 1
    order = sorted(votes, key=lambda q: -votes[q])
    out = []
    for found in hits:
        if len(found) == 1:
            out.append(next(iter(found.values())))
        else:
            q = next((q for q in order if q in found), None)
            out.append(found[q] if q is not None else None)
    return out


def import_names(store, source, people_list, *, apply=False, actor="google", min_share=0.35, min_count=3,
                 min_lift=5.0, margin=0.15):
    """Name our people after another library's named people: each name's items are matched to the
    catalogue, and the person seen in the largest share of them gets the name (an owner's name is
    never replaced; a different existing name becomes a proposal). Other people seen in most of a
    name's items are proposed as the same person (merges, applied when their faces agree too).

    People seen everywhere (the family behind the camera) are in most of everyone's items, so a
    person only counts for a name when seen there `min_lift` times more often than across the
    whole catalogue, or in most of the name's items. A person already called a name keeps it.
    When another person comes close for the same name, or another name comes close for the same
    person, the result is mixed and only proposed."""
    if not isinstance(source, str) or not source or len(source) > 40 or not source.replace("_", "").isalnum():
        raise ValueError("source is a short name like google_photos")
    if not isinstance(people_list, list) or not people_list or len(people_list) > 2000:
        raise ValueError("people is a list of {name, items}")
    if actor not in ACTORS:
        raise ValueError("Unknown actor")
    out = {"names": 0, "matched": 0, "named": 0, "proposed": 0, "merged": 0, "unmatched_names": []}
    with store.connect() as db:
        ensure(db)
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE face_log SET status='superseded' WHERE status='proposed' AND actor=?", (actor,))
        dismissed = _dismissed(db)
        with_faces = db.execute("SELECT count(DISTINCT asset_id) FROM faces").fetchone()[0] or 1
        seen_in = dict(db.execute("""SELECT person_id, count(DISTINCT asset_id) FROM faces
            WHERE person_id IS NOT NULL GROUP BY person_id""").fetchall())
        shares, pieces, near = [], [], []
        for entry in people_list:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"].strip():
                raise ValueError("Every person needs a name")
            name = entry["name"].strip()[:120]
            items = entry.get("items") or []
            if not isinstance(items, list) or len(items) > 20000:
                raise ValueError("items is a list of up to 20000 {kind, local}")
            assets = {a for a in match_items(db, items) if a}
            out["names"] += 1
            out["matched"] += len(assets)
            for aid in assets:
                db.execute("INSERT OR IGNORE INTO external_people(source,name,asset_id) VALUES(?,?,?)", (source, name, aid))
            if len(assets) < min_count:
                out["unmatched_names"].append(name)
                continue
            q = ",".join("?" * len(assets))
            for pid, n in db.execute(f"""SELECT person_id, count(DISTINCT asset_id) FROM faces
                    WHERE person_id IS NOT NULL AND asset_id IN ({q}) GROUP BY person_id""", list(assets)):
                share = n / len(assets)
                lift = share * with_faces / max(seen_in.get(pid, 1), 1)
                if n >= min_count and (lift >= min_lift or share >= 0.6):
                    near.append((share, n, name, pid, len(assets)))  # rivals, even below min_share
                    if share >= min_share:
                        shares.append(near[-1])
                if n >= min_count and share >= 0.5:  # split groups; merged only when the faces agree
                    pieces.append((share, n, name, pid, len(assets)))
        info = {r["id"]: dict(r) for r in db.execute("SELECT id, name, hidden FROM people")}
        size = dict(db.execute("SELECT person_id, count(*) FROM faces WHERE person_id IS NOT NULL GROUP BY person_id").fetchall())
        # the largest share first; on a tie, the person already called that, then the larger group
        shares.sort(key=lambda x: (x[0], info.get(x[3], {}).get("name") == x[2], size.get(x[3], 0)), reverse=True)
        taken_names, taken_people, primary = set(), set(), {}
        wanted = {e["name"].strip()[:120] for e in people_list}
        for pid, row in info.items():  # a person already called a name keeps it
            if row["name"] in wanted and row["name"] not in taken_names:
                taken_names.add(row["name"])
                taken_people.add(pid)
                primary[row["name"]] = pid
        out["mixed"] = []
        for share, n, name, pid, total in shares:
            if name in taken_names or pid in taken_people:
                continue
            taken_names.add(name)
            taken_people.add(pid)
            primary[name] = pid
            current = info[pid]["name"]
            detail = {"name": name, "source": source, "share": round(share, 3), "items": n, "of": total}
            rival = next((x for x in near if x[0] >= share - margin and (x[2] == name) != (x[3] == pid)
                          and x[3] not in taken_people - {pid} and x[2] not in taken_names - {name}), None)
            if rival is not None and not current:
                out["mixed"].append(name)
                del primary[name]  # nothing to merge into either
                if ("name", None, pid, None) not in dismissed:
                    _log(db, actor, "name", "proposed", person=pid, likeness=share,
                         detail={**detail, "why": f"mixed: also #{rival[3]} for {rival[2]}" if rival[2] != name
                                 else f"mixed: #{rival[3]} is seen almost as often"})
                    out["proposed"] += 1
                continue
            if not current and apply:
                db.execute("UPDATE people SET name=?, updated=? WHERE id=?", (name, time.time(), pid))
                _log(db, actor, "name", "applied", person=pid, likeness=share, detail=detail, previous={"name": ""})
                out["named"] += 1
            elif ("name", None, pid, None) not in dismissed:
                _log(db, actor, "name", "proposed", person=pid, likeness=share,
                     detail={**detail, "why": f"already named {current}" if current else "proposed"})
                out["proposed"] += 1
        cents = _centroids(db)
        for share, n, name, pid, total in sorted(pieces, reverse=True):
            into = primary.get(name)
            if into is None or pid == into or pid in taken_people or share < 0.5:
                continue
            if db.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone() is None or ("merge", None, into, pid) in dismissed:
                continue
            alike = _dot(cents[into], cents[pid]) if into in cents and pid in cents else 0.0
            if alike < AUTO["review_link_at"]:  # two people often in the same photos, not one person
                continue
            other_name = info[pid]["name"]
            if apply and alike >= AUTO["review_merge_at"] and (not other_name or other_name == name):
                previous = _do_merge(db, into, pid)
                _log(db, actor, "merge", "applied", person=into, other=pid, likeness=alike, previous=previous,
                     detail={"name": name, "source": source, "share": round(share, 3)})
                out["merged"] += 1
                cents = _centroids(db)
            else:
                _log(db, actor, "merge", "proposed", person=into, other=pid, likeness=alike,
                     detail={"name": name, "source": source, "share": round(share, 3)})
                out["proposed"] += 1
    return out


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
    p.add_argument("action", choices=["scan", "group", "sort", "status"])
    p.add_argument("--limit", type=int)
    p.add_argument("--apply", action="store_true", help="sort: make the confident changes (otherwise only propose)")
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
    if args.action == "sort":
        try:
            set_progress(store, state="sorting", started=time.time(), error="")
            out = auto_sort(store, apply=args.apply, progress=lambda **kw: set_progress(store, **kw))
            set_progress(store, state="done", sorted=out)
            print(json.dumps(out))
        except Exception as exc:
            set_progress(store, state="failed", error=str(exc)[:500])
            raise
        finally:
            lock.close()
        return
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
