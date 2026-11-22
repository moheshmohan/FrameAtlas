"""The shared screen: an agent shows the owner something in the open FrameAtlas page (ui_show), selects
media there (ui_select) and reads what the owner is looking at (ui_state). The MCP server and the web
server are separate processes, so they meet in the catalogue: agents add events, and the page
long-polls for them and reports its own state."""
import json
import re
import time
from urllib.parse import quote, urlencode

from .contract import ToolError

KEEP_EVENTS = 200
EVENT_SECONDS = 120          # an event older than this is not shown to a page that opens later
PAGE_SECONDS = 45            # a page that polled within this many seconds is open
POLL_SECONDS = 20
MAX_STATE_BYTES = 64 * 1024
VIEWS = ("library", "item", "person", "place", "trip", "idea", "people", "ideas", "queue", "sources", "activity")
DEFAULT_PORT = 7318
ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS ui_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, event TEXT NOT NULL)""")


def _setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    try:
        return json.loads(row[0]) if row else None
    except ValueError:
        return None


def _save(db, key, value):
    db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def remember_port(store, port):
    with store.connect() as db:
        _save(db, "ui_port", int(port))


def base_url(store):
    with store.connect() as db:
        port = _setting(db, "ui_port")
    return f"http://127.0.0.1:{port if isinstance(port, int) and 0 < port < 65536 else DEFAULT_PORT}/"


def page_open(store):
    with store.connect() as db:
        seen = _setting(db, "ui_seen")
    return isinstance(seen, (int, float)) and time.time() - seen < PAGE_SECONDS, seen


# ---- routes

def route(view, args):
    """The page's hash route for a view: also a link the owner can open or bookmark."""
    if view not in VIEWS:
        raise ToolError("invalid_arguments", "view is one of: " + ", ".join(VIEWS))
    ident = args.get("id")
    if view in ("item", "person", "idea", "place") and not ident:
        raise ToolError("invalid_arguments", f"Showing a {view} needs its id")
    if view == "item":
        hash_ = f"item/{quote(str(ident), safe='')}"
        if args.get("time") is not None:
            hash_ += f"@{float(args['time']):.3f}".rstrip("0").rstrip(".")
        return hash_
    if view in ("person", "idea"):
        return f"{view}/{int(ident)}"
    if view == "place":
        return "place/" + ",".join(str(int(x)) for x in str(ident).split(",") if x.strip())
    if view in ("library", "trip"):
        keys = ("query", "person", "place", "since", "until", "kind", "provider")
        params = {k: str(args[k]) for k in keys if args.get(k) not in (None, "")}
        for key in ("since", "until"):
            if key in params and not DAY.fullmatch(params[key]):
                raise ToolError("invalid_arguments", f"{key} is a date like 2024-05-25")
        if view == "trip" and not (params.get("since") and params.get("until")):
            raise ToolError("invalid_arguments", "A trip needs since and until")
        return "library" + ("?" + urlencode(params) if params else "")
    return {"ideas": "ideapad"}.get(view, view)


def _check_exists(db, view, ident):
    if view == "item":
        if not db.execute("SELECT 1 FROM assets WHERE id=?", (str(ident),)).fetchone():
            raise ToolError("not_found", "Media item not found")
    if view == "person":
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='people'").fetchone() or \
                not db.execute("SELECT 1 FROM people WHERE id=?", (int(ident),)).fetchone():
            raise ToolError("not_found", "Person not found")
    if view == "idea":
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='idea_pad'").fetchone() or \
                not db.execute("SELECT 1 FROM idea_pad WHERE id=?", (int(ident),)).fetchone():
            raise ToolError("not_found", "Idea not found")


def _add_event(db, event):
    ensure(db)
    eid = db.execute("INSERT INTO ui_events(at,event) VALUES(?,?)", (time.time(), json.dumps(event, ensure_ascii=False))).lastrowid
    db.execute("DELETE FROM ui_events WHERE id <= ?", (eid - KEEP_EVENTS,))
    return eid


def show(store, args):
    view = args.get("view", "library")
    try:
        hash_ = route(view, args)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ToolError):
            raise
        raise ToolError("invalid_arguments", "Use a numeric id for people, ideas and places, and seconds for time") from None
    message = str(args.get("message", ""))[:200]
    with store.connect() as db:
        _check_exists(db, view, args.get("id"))
        eid = _add_event(db, {"kind": "show", "hash": hash_, "message": message})
    opened, _ = page_open(store)
    link = base_url(store) + "#" + hash_
    return {"shown": opened, "event": eid, "link": link,
            "note": "" if opened else "FrameAtlas is not open in a browser; give the owner the link."}


def select(store, args):
    mode = args.get("mode", "replace")
    if mode not in ("replace", "add", "clear"):
        raise ToolError("invalid_arguments", "mode is replace, add or clear")
    ids = args.get("ids") or []
    if mode != "clear" and not ids:
        raise ToolError("invalid_arguments", "Give ids to select, or mode clear")
    with store.connect() as db:
        rows = {r["id"]: r["filename"] for r in db.execute(
            f"SELECT id, filename FROM assets WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
        missing = [i for i in ids if i not in rows]
        if missing:
            raise ToolError("not_found", f"{len(missing)} item(s) not found, for example {missing[0]}")
        eid = _add_event(db, {"kind": "select", "mode": mode, "items": [[i, rows[i]] for i in ids],
                              "message": str(args.get("message", ""))[:200]})
    opened, _ = page_open(store)
    return {"selected": len(ids), "mode": mode, "event": eid, "shown": opened,
            "note": "" if opened else "FrameAtlas is not open in a browser; the selection waits two minutes for it."}


def state(store):
    opened, seen = page_open(store)
    with store.connect() as db:
        saved = _setting(db, "ui_state") or {}
    out = {"page_open": opened, "seconds_since_seen": round(time.time() - seen, 1) if isinstance(seen, (int, float)) else None,
           "link": base_url(store), "state": saved.get("state") if opened else None}
    if not opened:
        out["note"] = "FrameAtlas is not open in a browser, so there is no current view or selection."
    return out


# ---- the page's side (HTTP only)

def report(store, page_state):
    if not isinstance(page_state, dict):
        raise ValueError("state must be an object")
    text = json.dumps(page_state, ensure_ascii=False)
    if len(text) > MAX_STATE_BYTES:
        raise ValueError("Page state is too large")
    with store.connect() as db:
        _save(db, "ui_state", {"state": page_state, "at": time.time()})
        _save(db, "ui_seen", time.time())
    return {"ok": True}


def poll(store, after, wait=POLL_SECONDS):
    """Events newer than after, waiting up to wait seconds for one. after -1 starts at the newest."""
    after = int(after)
    deadline = time.time() + max(0, min(float(wait), POLL_SECONDS))
    with store.connect() as db:
        _save(db, "ui_seen", time.time())
    while True:
        with store.connect() as db:
            ensure(db)
            last = db.execute("SELECT COALESCE(max(id), 0) FROM ui_events").fetchone()[0]
            if after < 0:
                return {"events": [], "last": last}
            rows = db.execute("SELECT id, at, event FROM ui_events WHERE id>? AND at>? ORDER BY id LIMIT 20",
                              (after, time.time() - EVENT_SECONDS)).fetchall()
        if rows or time.time() >= deadline:
            return {"events": [{"id": r["id"], **json.loads(r["event"])} for r in rows],
                    "last": rows[-1]["id"] if rows else max(after, last)}
        time.sleep(0.5)
