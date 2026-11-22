"""Footage memory: which catalogue items went into which finished video, and where that video was
published. Search can then find footage never used, an item's detail says where it was used, ideas can
prefer fresh shots, and a published idea keeps its links."""
import json
import re
import time
from pathlib import Path

from .contract import ToolError

MAX_SHOTS = 2000
WHERE = ("youtube", "youtube_short", "instagram", "google_photos", "blog", "other")


def ensure(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS videos (
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
          idea_id INTEGER, published TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL, updated REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS videos_project ON videos(project) WHERE project<>'';
        CREATE TABLE IF NOT EXISTS footage_use (
          video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
          asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
          start REAL, end REAL, PRIMARY KEY(video_id, asset_id, start));
        CREATE INDEX IF NOT EXISTS footage_use_asset ON footage_use(asset_id);""")


def _resolve(db, shot):
    """A catalogue item for a shot given by asset_id, by a local path, or by filename (and capture time)."""
    if shot.get("asset_id"):
        row = db.execute("SELECT id FROM assets WHERE id=?", (str(shot["asset_id"]),)).fetchone()
        return row["id"] if row else None
    path = shot.get("path")
    if isinstance(path, str) and path:
        row = db.execute("SELECT id FROM assets WHERE local_path=? LIMIT 2", (path,)).fetchall()
        if len(row) == 1:
            return row[0]["id"]
        path = Path(path).name
    name = shot.get("filename") or path
    if not isinstance(name, str) or not name:
        return None
    rows = db.execute("SELECT id, captured FROM assets WHERE filename=?", (name,)).fetchall()
    if len(rows) > 1 and shot.get("captured"):
        rows = [r for r in rows if str(r["captured"]).startswith(str(shot["captured"])[:19])]
    return rows[0]["id"] if len(rows) == 1 else None


def _published(value):
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ToolError("invalid_arguments", "published is a list of up to 20 {where, url, date}")
    out = []
    for p in value:
        if not isinstance(p, dict) or p.get("where") not in WHERE:
            raise ToolError("invalid_arguments", "Each published entry needs where: " + ", ".join(WHERE))
        url = str(p.get("url", ""))[:500]
        if url and not re.match(r"https?://", url):
            raise ToolError("invalid_arguments", "url must start with http:// or https://")
        out.append({"where": p["where"], "url": url, "date": str(p.get("date", ""))[:10], "note": str(p.get("note", ""))[:200]})
    return out


def record(store, title, shots, project="", idea_id=None, published=None):
    """Remember that a video used these shots (asset_id, path or filename, with start and end seconds when
    known). Recording the same project again replaces its shots; published links are added to the idea."""
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ToolError("invalid_arguments", "title is the video's name (up to 200 characters)")
    if not isinstance(shots, list) or not 1 <= len(shots) <= MAX_SHOTS:
        raise ToolError("invalid_arguments", f"shots is a list of 1–{MAX_SHOTS} shots")
    if not isinstance(project, str) or len(project) > 1000:
        raise ToolError("invalid_arguments", "project is the project file's path")
    links = _published(published)
    now = time.time()
    with store.connect() as db:
        ensure(db)
        db.execute("BEGIN IMMEDIATE")
        if idea_id is not None and not db.execute("SELECT 1 FROM sqlite_master WHERE name='idea_pad'").fetchone():
            raise ToolError("not_found", "Idea not found")
        if idea_id is not None and not db.execute("SELECT 1 FROM idea_pad WHERE id=?", (idea_id,)).fetchone():
            raise ToolError("not_found", "Idea not found")
        row = db.execute("SELECT id, published FROM videos WHERE project=? AND project<>''", (project,)).fetchone() if project else None
        if row:
            vid = row["id"]
            merged = json.loads(row["published"]) + [p for p in links if p not in json.loads(row["published"])]
            db.execute("UPDATE videos SET title=?, idea_id=COALESCE(?,idea_id), published=?, updated=? WHERE id=?",
                       (title.strip(), idea_id, json.dumps(merged), now, vid))
            db.execute("DELETE FROM footage_use WHERE video_id=?", (vid,))
        else:
            vid = db.execute("INSERT INTO videos(title,project,idea_id,published,created,updated) VALUES(?,?,?,?,?,?)",
                             (title.strip(), project, idea_id, json.dumps(links), now, now)).lastrowid
        matched, missing = set(), []
        for shot in shots:
            if not isinstance(shot, dict):
                raise ToolError("invalid_arguments", "Every shot is an object")
            aid = _resolve(db, shot)
            if not aid:
                missing.append(str(shot.get("asset_id") or shot.get("filename") or Path(str(shot.get("path", ""))).name)[:120])
                continue
            start, end = shot.get("start"), shot.get("end")
            start = float(start) if isinstance(start, (int, float)) and not isinstance(start, bool) else None
            end = float(end) if isinstance(end, (int, float)) and not isinstance(end, bool) else None
            db.execute("INSERT OR REPLACE INTO footage_use(video_id,asset_id,start,end) VALUES(?,?,?,?)", (vid, aid, start, end))
            matched.add(aid)
        if idea_id is not None and links:
            idea = db.execute("SELECT basis FROM idea_pad WHERE id=?", (idea_id,)).fetchone()
            basis = json.loads(idea["basis"])
            basis["published"] = basis.get("published", []) + [p for p in links if p not in basis.get("published", [])]
            db.execute("UPDATE idea_pad SET basis=?, status='published', updated=? WHERE id=?",
                       (json.dumps(basis, ensure_ascii=False), now, idea_id))
    return {"video": vid, "items": len(matched), "not_found": missing[:50], "not_found_count": len(missing)}


def used_in(store, asset_id):
    with store.connect() as db:
        ensure(db)
        rows = db.execute("""SELECT v.id, v.title, v.idea_id, v.published, v.updated, u.start, u.end
            FROM footage_use u JOIN videos v ON v.id=u.video_id WHERE u.asset_id=? ORDER BY v.updated DESC LIMIT 20""",
                          (asset_id,)).fetchall()
    out = []
    for r in rows:
        out.append({"video": r["id"], "title": r["title"], "idea_id": r["idea_id"], "start": r["start"], "end": r["end"],
                    "published": json.loads(r["published"])})
    return out


def videos(store, limit=50, offset=0):
    with store.connect() as db:
        ensure(db)
        total = db.execute("SELECT count(*) FROM videos").fetchone()[0]
        rows = db.execute("""SELECT v.*, (SELECT count(DISTINCT asset_id) FROM footage_use u WHERE u.video_id=v.id) AS items
            FROM videos v ORDER BY v.updated DESC LIMIT ? OFFSET ?""", (max(1, min(int(limit), 200)), max(0, int(offset)))).fetchall()
    return {"items": [{**{k: r[k] for k in ("id", "title", "project", "idea_id", "items", "created", "updated")},
                       "published": json.loads(r["published"])} for r in rows], "total": total}


def forget_video(store, video_id):
    with store.connect() as db:
        ensure(db)
        if not db.execute("DELETE FROM videos WHERE id=?", (int(video_id),)).rowcount:
            raise ToolError("not_found", "Video not found")
    return {"forgotten": int(video_id)}
