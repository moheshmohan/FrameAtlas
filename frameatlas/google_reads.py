"""Reading Google Photos' Places (and People & pets) in the owner's own browser, as a resumable plan
that FrameAtlas keeps: which names are still to read, which reads stopped early, how many searches
ran in the last hour, and when Google's throttle should have lifted. An agent asks for the next names
and the script to run, runs it in the owner's browser, and hands the result straight back; nothing is
scraped from a transcript, and any agent can carry on where the last one stopped."""
import re
import time
from pathlib import Path

from .contract import ToolError

KINDS = ("places", "people")
STATES = ("pending", "done", "incomplete", "failed")
HOUR = 3600
SEARCH_BUDGET = 25          # searches per hour before Google is likely to throttle (it varies, 25 to 55)
THROTTLE_ITEMS = 60         # an incomplete read with at most this many items looks like Google's throttle
MAX_NAMES = 5000
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "google_photos_places.js"
KEY = re.compile(r"[0-9a-f]{1,32}")


def ensure(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS google_reads (
          kind TEXT NOT NULL, name TEXT NOT NULL, key TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
          items INTEGER NOT NULL DEFAULT 0, days INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
          last_read REAL, note TEXT NOT NULL DEFAULT '', PRIMARY KEY(kind, name));
        CREATE TABLE IF NOT EXISTS google_searches (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
          outcome TEXT NOT NULL);""")


def _kind(kind):
    if kind not in KINDS:
        raise ToolError("invalid_arguments", "kind is places or people")
    return kind


def _throttle(db, kind, now):
    """Searches in the last hour, and when the throttle should lift if Google showed it."""
    recent = db.execute("SELECT count(*) FROM google_searches WHERE kind=? AND at>?", (kind, now - HOUR)).fetchone()[0]
    hit = db.execute("""SELECT max(at) FROM google_searches WHERE kind=? AND outcome='throttled' AND at>?""",
                     (kind, now - HOUR)).fetchone()[0]
    first = db.execute("SELECT min(at) FROM google_searches WHERE kind=? AND at>?", (kind, now - HOUR)).fetchone()[0]
    wait_until = hit + HOUR if hit else (first + HOUR if recent >= SEARCH_BUDGET and first else None)
    return recent, wait_until


def register(store, kind, entries):
    """Add the names (with Google's place key for places) from the list page; known names keep their state."""
    kind = _kind(kind)
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_NAMES:
        raise ToolError("invalid_arguments", f"entries is a list of 1–{MAX_NAMES} names")
    added = 0
    with store.connect() as db:
        ensure(db)
        for e in entries:
            name, key = (e.get("name"), e.get("key", "")) if isinstance(e, dict) else (e, "")
            if not isinstance(name, str) or not name.strip() or len(name) > 120:
                raise ToolError("invalid_arguments", "Every entry needs a name of up to 120 characters")
            if kind == "places" and not (isinstance(key, str) and KEY.fullmatch(key)):
                raise ToolError("invalid_arguments", f"{name}: places need Google's place key (hex)")
            added += db.execute("INSERT OR IGNORE INTO google_reads(kind,name,key) VALUES(?,?,?)",
                                (kind, name.strip(), key or "")).rowcount
            if key:
                db.execute("UPDATE google_reads SET key=? WHERE kind=? AND name=?", (key, kind, name.strip()))
    return {"added": added, **plan(store, kind)}


def plan(store, kind):
    kind = _kind(kind)
    now = time.time()
    with store.connect() as db:
        ensure(db)
        counts = {s: 0 for s in STATES}
        counts.update({r[0]: r[1] for r in db.execute("SELECT state, count(*) FROM google_reads WHERE kind=? GROUP BY state", (kind,))})
        recent, wait_until = _throttle(db, kind, now)
        incomplete = [dict(r) for r in db.execute("""SELECT name, items, attempts, note FROM google_reads
            WHERE kind=? AND state IN ('incomplete','failed') ORDER BY last_read LIMIT 50""", (kind,))]
    waiting = bool(wait_until and wait_until > now)
    return {"kind": kind, "counts": counts, "total": sum(counts.values()),
            "searches_last_hour": recent, "budget_per_hour": SEARCH_BUDGET,
            "wait_until": round(wait_until) if waiting else None,
            "wait_minutes": round((wait_until - now) / 60) if waiting else 0,
            "needs_again": incomplete,
            "next_step": ("Wait for Google's throttle to lift" if waiting else
                          "Read the list page first (google_read_script step 1, then google_read_register)" if not sum(counts.values()) else
                          "Call google_read_next" if counts["pending"] or counts["incomplete"] else "Everything is read")}


def next_names(store, kind, count=5):
    kind = _kind(kind)
    count = max(1, min(int(count), 10))
    info = plan(store, kind)
    if info["wait_until"]:
        return {"names": [], "wait_until": info["wait_until"], "wait_minutes": info["wait_minutes"],
                "note": "Google is likely throttling; read again after wait_until."}
    left = max(0, SEARCH_BUDGET - info["searches_last_hour"])
    with store.connect() as db:
        ensure(db)
        rows = db.execute("""SELECT name, key, state, attempts FROM google_reads WHERE kind=? AND state IN ('pending','incomplete','failed')
            AND attempts < 5 ORDER BY CASE state WHEN 'pending' THEN 0 ELSE 1 END, attempts, name LIMIT ?""",
                          (kind, min(count, left))).fetchall()
    return {"names": [dict(r) for r in rows], "budget_left": left,
            "note": "" if rows else ("This hour's search budget is used; wait." if not left else "Nothing left to read.")}


def script(step):
    """The browser script's step (1 list, 2 Maps positions, 3 one place's days)."""
    if step not in (1, 2, 3):
        raise ToolError("invalid_arguments", "step is 1, 2 or 3")
    try:
        text = SCRIPT.read_text()
    except OSError:
        raise ToolError("failed", "The places script is missing from this FrameAtlas checkout") from None
    header = text.split("// ---- step 1", 1)[0]
    parts = re.split(r"^// ---- step (\d)[^\n]*\n", text, flags=re.M)
    steps = {int(parts[i]): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if step not in steps:
        raise ToolError("failed", "The places script does not have that step")
    how = {1: "Run on https://photos.google.com/places. Pass its result to google_read_register as entries [{name, key}].",
           2: "Run on any https://www.google.com/maps page with KEYS set; read window.frameatlasMaps when done and pass "
              "positions with google_read_result or places_import.",
           3: "Set NAME, run on https://photos.google.com/places (it opens the place with a full page load), run again on "
              "the place's page, then pass {name, key, items, complete, packed} to google_read_result."}[step]
    return {"step": step, "how": how, "script": steps[step], "notes": header.strip()}


def result(store, args):
    """Take one read's result: import it when it reached the end, else mark it to read again."""
    from . import people, places
    kind = _kind(args.get("kind", "places"))
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ToolError("invalid_arguments", "name is the place or person as listed")
    name = name.strip()
    now = time.time()
    outcome = args.get("outcome", "read")
    if outcome not in ("read", "no_results", "not_found"):
        raise ToolError("invalid_arguments", "outcome is read, no_results or not_found")
    items = args.get("items", 0)
    if type(items) is not int or items < 0:
        raise ToolError("invalid_arguments", "items is how many photos and videos the read saw")
    complete = args.get("complete") is True
    with store.connect() as db:
        ensure(db)
        row = db.execute("SELECT * FROM google_reads WHERE kind=? AND name=?", (kind, name)).fetchone()
    if not row:
        raise ToolError("not_found", f"{name} is not in the read plan; register the list first")
    imported, state, note = None, "done", ""
    if outcome == "no_results":
        state, note, search = "incomplete", "Google returned no results (throttling)", "throttled"
    elif outcome == "not_found":
        state, note, search = "failed", "Not on the list page", "not_found"
    elif not complete:
        throttled = items <= THROTTLE_ITEMS
        state, search = "incomplete", "throttled" if throttled else "incomplete"
        note = (f"Stopped at {items} items: Google is likely throttling" if throttled else
                f"Stopped at {items} items before the end")
    else:
        search = "ok"
        if kind == "places":
            packed = args.get("packed", "")
            if not isinstance(packed, str) or (items and not packed):
                raise ToolError("invalid_arguments", "packed is the days text step 3 returned")
            place = {"key": args.get("key") or row["key"], "name": name, "packed": packed}
            for k in ("address", "latitude", "longitude", "plus_code"):
                if args.get(k) not in (None, ""):
                    place[k] = args[k]
            if packed:
                imported = places.import_places(store, "google_photos", [place])
        else:
            found = args.get("people_items")
            if not isinstance(found, list) or not found:
                raise ToolError("invalid_arguments", "people_items is [{kind, local}] for this person's photos")
            imported = people.import_names(store, "google_photos", [{"name": name, "items": found}], apply=False)
    days = len(str(args.get("packed", "")).split()) if kind == "places" and complete else 0
    with store.connect() as db:
        ensure(db)
        db.execute("""UPDATE google_reads SET state=?, items=?, days=?, attempts=attempts+1, last_read=?, note=?
            WHERE kind=? AND name=?""", (state, items, days, now, note, kind, name))
        db.execute("INSERT INTO google_searches(at,kind,name,outcome) VALUES(?,?,?,?)", (now, kind, name, search))
        db.execute("DELETE FROM google_searches WHERE at < ?", (now - 7 * 86400,))
    out = {"name": name, "state": state, "note": note, "imported": imported, **{k: v for k, v in plan(store, kind).items()
                                                                                if k in ("counts", "searches_last_hour", "wait_until", "wait_minutes")}}
    if state == "incomplete" and search == "throttled":
        out["next_step"] = "Stop reading for now; Google is throttling. Try again after wait_until."
    return out


def reset(store, kind, names=None):
    """Read some names (or every name) again."""
    kind = _kind(kind)
    with store.connect() as db:
        ensure(db)
        if names:
            n = sum(db.execute("UPDATE google_reads SET state='pending', attempts=0, note='' WHERE kind=? AND name=?",
                               (kind, str(x))).rowcount for x in names[:MAX_NAMES])
        else:
            n = db.execute("UPDATE google_reads SET state='pending', attempts=0, note='' WHERE kind=?", (kind,)).rowcount
    return {"reset": n}


def summary(store):
    """For the health report: how far each kind of read has got."""
    out = {}
    with store.connect() as db:
        ensure(db)
        for kind in KINDS:
            counts = {r[0]: r[1] for r in db.execute("SELECT state, count(*) FROM google_reads WHERE kind=? GROUP BY state", (kind,))}
            if counts:
                out[kind] = counts
    return out
