"""Search by meaning: every stored preview as a SigLIP 2 vector (made by FilmCraft on this Mac, on the GPU),
kept in one file of 768-number rows, and a text query compared with all of them by FilmCraft
(`media.nearest`). "Kids laughing on a boat at sunset" then finds footage whose captions use other words.
The vectors stay in the data folder; nothing leaves the Mac and nothing is downloaded."""
import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .contract import ToolError

DIMS = 768
ROW_BYTES = DIMS * 2          # little-endian f16
BATCH = 256                   # pictures a media.embed call takes
CALLS_PER_RUN = 16            # media.embed calls one filmcraft-cli process makes (the model loads once)
MAX_RESULTS = 200


def folder(store):
    return Path(store.root) / "semantic"


def index_path(store):
    return folder(store) / "index.f16"


def ensure(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS embeddings (
          asset_id TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE, row INTEGER NOT NULL UNIQUE,
          model TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS embedding_failures (
          asset_id TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE, error TEXT NOT NULL, at REAL NOT NULL);""")


def status(store):
    with store.connect() as db:
        ensure(db)
        done = db.execute("SELECT count(*) FROM embeddings").fetchone()[0]
        failed = db.execute("SELECT count(*) FROM embedding_failures").fetchone()[0]
        previews = db.execute("SELECT count(*) FROM assets WHERE thumbnail<>''").fetchone()[0]
        model = db.execute("SELECT model FROM embeddings LIMIT 1").fetchone()
    size = index_path(store).stat().st_size if index_path(store).is_file() else 0
    return {"embedded": done, "previews": previews, "left": max(0, previews - done - failed), "failed": failed,
            "model": model[0] if model else None, "index_bytes": size,
            "consistent": size == done * ROW_BYTES}


def _pending(store, limit):
    with store.connect() as db:
        ensure(db)
        rows = db.execute("""SELECT a.id, a.thumbnail FROM assets a WHERE a.thumbnail<>''
            AND a.id NOT IN (SELECT asset_id FROM embeddings) AND a.id NOT IN (SELECT asset_id FROM embedding_failures)
            ORDER BY a.captured DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    thumbs = Path(store.root) / "thumbnails"
    for r in rows:
        name = r["thumbnail"]
        path = thumbs / name
        if re.fullmatch(r"[a-f0-9]{24}\.jpg", name or "") and path.is_file() and not path.is_symlink():
            out.append((r["id"], str(path)))
    return out


def _run(cli, calls, timeout):
    """One filmcraft-cli process for several media.embed calls; yields each call's result or error."""
    script = "\n".join(json.dumps({"id": "media.embed", "params": {"paths": paths, "precision": "f16"}}) for paths in calls) + "\n"
    with tempfile.TemporaryDirectory(prefix="FrameAtlas-embed-") as tmp:
        path = Path(tmp) / "embed.jsonl"
        path.write_text(script)
        done = subprocess.run([cli, "run", str(path), "--keep-going"], capture_output=True, text=True, timeout=timeout)
    results = []
    for line in done.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("id") == "media.embed":
            results.append(row)
    if not results and done.returncode:
        raise ToolError("failed", "FilmCraft could not embed the previews: " + (done.stderr or done.stdout)[-600:])
    return results


def build(store, limit=5000, cli=None, progress=None):
    """Embed up to limit previews not embedded yet, appending their rows to the index."""
    from .native import CLI
    cli = cli or CLI
    limit = max(1, min(int(limit), 100000))
    folder(store).mkdir(exist_ok=True)
    info = status(store)
    if not info["consistent"]:
        raise ToolError("invalid", "The vector index does not match its row list; run semantic_reset, then build again")
    todo = _pending(store, limit)
    added = failed = 0
    started = time.time()
    model = None
    for start in range(0, len(todo), BATCH * CALLS_PER_RUN):
        group = todo[start:start + BATCH * CALLS_PER_RUN]
        calls = [group[i:i + BATCH] for i in range(0, len(group), BATCH)]
        results = _run(cli, [[p for _, p in c] for c in calls], timeout=1800)
        for call, result in zip(calls, results):
            if not result.get("ok"):
                raise ToolError("failed", "FilmCraft: " + str(result.get("error", ""))[:600])
            out = result.get("result") or {}
            model = out.get("model") or model
            if out.get("dimensions") not in (DIMS, None, 0):
                raise ToolError("failed", f"The embedding model gave {out.get('dimensions')} numbers, not {DIMS}")
            pictures = out.get("pictures") or []
            with store.connect() as db:
                ensure(db)
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT COALESCE(max(row), -1) + 1 FROM embeddings").fetchone()[0]
                chunk, now = bytearray(), time.time()
                for (aid, _), pic in zip(call, pictures):
                    raw = None
                    if isinstance(pic, dict) and isinstance(pic.get("embedding"), str):
                        try:
                            raw = base64.b64decode(pic["embedding"], validate=True)
                        except ValueError:
                            raw = None
                    if raw is None or len(raw) != ROW_BYTES:
                        error = str((pic or {}).get("error", "no embedding"))[:300] if isinstance(pic, dict) else "no embedding"
                        db.execute("INSERT OR REPLACE INTO embedding_failures(asset_id, error, at) VALUES(?,?,?)", (aid, error, now))
                        failed += 1
                        continue
                    db.execute("INSERT INTO embeddings(asset_id, row, model, updated) VALUES(?,?,?,?)", (aid, row, model or "", now))
                    chunk += raw
                    row += 1
                    added += 1
                # the rows go to the file inside the same transaction: a failed write rolls the rows back
                with open(index_path(store), "ab") as f:
                    f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
            if progress:
                progress(added + failed, len(todo))
    return {"added": added, "failed": failed, "seconds": round(time.time() - started, 1), **status(store)}


def reset(store):
    """Forget every vector (the previews stay); build again afterwards."""
    with store.connect() as db:
        ensure(db)
        db.execute("DELETE FROM embeddings")
        db.execute("DELETE FROM embedding_failures")
    if index_path(store).is_file():
        index_path(store).unlink()
    return status(store)


def search(store, query, limit=48, cli=None, **filters):
    """Items whose previews look most like the words, best first, then narrowed by catalogue filters."""
    from .native import CLI
    cli = cli or CLI
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        raise ToolError("invalid_arguments", "query is up to 300 characters")
    limit = max(1, min(int(limit), MAX_RESULTS))
    info = status(store)
    if not info["embedded"]:
        raise ToolError("invalid", "No previews have vectors yet", "Run semantic_build first (it runs on the GPU, a few minutes per 10,000 previews).")
    if not info["consistent"]:
        raise ToolError("invalid", "The vector index does not match its row list; run semantic_reset, then build again")
    # provider and kind are read off each hit; the other filters go through the catalogue search
    direct = {k: filters.pop(k) for k in ("provider", "kind") if filters.get(k) not in (None, "")}
    want = min(1000, max(limit * 10, 200)) if (filters or direct) else limit
    done = subprocess.run([cli, "exec", "media.nearest", json.dumps({"index": str(index_path(store)), "query": query.strip(),
                                                                       "dims": DIMS, "limit": want})],
                          capture_output=True, text=True, timeout=180)
    if done.returncode:
        raise ToolError("failed", "FilmCraft could not search the vectors: " + (done.stderr or done.stdout)[-600:])
    try:
        hits = json.loads(done.stdout)["hits"]
    except (ValueError, KeyError, TypeError):
        raise ToolError("failed", "FilmCraft gave an unreadable answer") from None
    rows = {h["row"]: h for h in hits if isinstance(h, dict) and isinstance(h.get("row"), int)}
    if not rows:
        return {"items": [], "total": 0, "query": query}
    with store.connect() as db:
        ensure(db)
        where = "".join(f" AND a.{k}=?" for k in direct)
        by_row = {r["row"]: r["asset_id"] for r in db.execute(
            f"SELECT e.asset_id, e.row FROM embeddings e JOIN assets a ON a.id=e.asset_id"
            f" WHERE e.row IN ({','.join('?' * len(rows))}){where}", [*rows, *direct.values()])}
    allowed = None
    if filters:
        allowed = set()
        page = store.search(**filters, limit=200, offset=0)
        total, offset = page["total"], 0
        while True:
            allowed.update(a["id"] for a in page["items"])
            offset += 200
            if offset >= min(total, 20000):
                break
            page = store.search(**filters, limit=200, offset=offset)
    items = []
    for row in rows:
        aid = by_row.get(row)
        if not aid or (allowed is not None and aid not in allowed):
            continue
        try:
            asset = store.get(aid, include_scenes=False)
        except ValueError:
            continue
        hit = rows[row]
        asset.pop("analysis", None)
        asset.pop("locator", None)
        items.append({**asset, "cosine": hit.get("cosine"), "match": hit.get("match"), "matched_query": f"looks like: {query.strip()}"})
        if len(items) >= limit:
            break
    return {"items": items, "total": len(items), "query": query, "searched": info["embedded"],
            "note": "Ranked by how the preview looks against the words (SigLIP 2); captions and speech are not used here."}
