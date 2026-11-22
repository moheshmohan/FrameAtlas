"""People as the owner knows them: how each named person is related to the owner and when they were born,
a person's years in the media, who they appear with, and the owner's calendar of occasions (festivals,
holidays and birthdays) that become moments for video ideas."""
from collections import defaultdict
from datetime import date
import json
import re
import time

from .contract import ToolError

KEY = "calendar_occasions"
DEFAULT_OCCASIONS = [{"name": "New Year's Day", "month": 1, "day": 1}, {"name": "Christmas", "month": 12, "day": 25}]
MAX_OCCASIONS = 200


def ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS person_details (
        person_id INTEGER PRIMARY KEY, relation TEXT NOT NULL DEFAULT '', born TEXT NOT NULL DEFAULT '', updated REAL NOT NULL)""")


def _born(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}(-\d{2}-\d{2})?", value):
        raise ToolError("invalid_arguments", "born is a year (2019) or a date (2019-04-21)")
    if len(value) == 10:
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ToolError("invalid_arguments", "born is not a real date") from None
    return value


def set_details(store, person_id, relation=None, born=None):
    """How a person is related to the owner ("our son", "my wife", "a friend from school") and when
    they were born. Agents and idea writing use the relation as given; nothing is guessed."""
    if not isinstance(person_id, int) or person_id < 1:
        raise ToolError("invalid_arguments", "id is a person id from people_list")
    if relation is not None and (not isinstance(relation, str) or len(relation) > 60):
        raise ToolError("invalid_arguments", "relation is up to 60 characters, such as 'our son'")
    born = _born(born) if born is not None else None
    with store.connect() as db:
        ensure(db)
        if not db.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
            raise ToolError("not_found", "Person not found")
        row = db.execute("SELECT relation, born FROM person_details WHERE person_id=?", (person_id,)).fetchone()
        rel = (relation.strip() if relation is not None else (row["relation"] if row else ""))
        b = born if born is not None else (row["born"] if row else "")
        db.execute("INSERT OR REPLACE INTO person_details(person_id, relation, born, updated) VALUES(?,?,?,?)",
                   (person_id, rel, b, time.time()))
    return {"id": person_id, "relation": rel, "born": b}


def details(db):
    ensure(db)
    return {r["person_id"]: {"relation": r["relation"], "born": r["born"]} for r in db.execute("SELECT * FROM person_details")}


def relations_by_name(store):
    with store.connect() as db:
        ensure(db)
        return {r["name"]: r["relation"] for r in db.execute("""SELECT p.name, d.relation FROM person_details d
            JOIN people p ON p.id=d.person_id WHERE p.name<>'' AND d.relation<>''""")}


def timeline(store, person_id):
    """A person's years in the media: items each year (with their age when born is known), a few
    analysed examples, and who they appear with most."""
    if not isinstance(person_id, int) or person_id < 1:
        raise ToolError("invalid_arguments", "id is a person id from people_list")
    with store.connect() as db:
        ensure(db)
        person = db.execute("SELECT id, name FROM people WHERE id=?", (person_id,)).fetchone()
        if not person:
            raise ToolError("not_found", "Person not found")
        info = details(db).get(person_id, {"relation": "", "born": ""})
        rows = db.execute("""SELECT DISTINCT a.id, a.captured, a.summary, a.kind, a.status, a.filename FROM faces f
            JOIN assets a ON a.id=f.asset_id WHERE f.person_id=? AND a.captured<>'' ORDER BY a.captured""", (person_id,)).fetchall()
        together = db.execute("""SELECT f2.person_id AS id, p.name, count(DISTINCT f2.asset_id) AS items FROM faces f1
            JOIN faces f2 ON f2.asset_id=f1.asset_id AND f2.person_id IS NOT NULL AND f2.person_id<>f1.person_id
            JOIN people p ON p.id=f2.person_id WHERE f1.person_id=? AND p.hidden=0
            GROUP BY f2.person_id ORDER BY items DESC LIMIT 10""", (person_id,)).fetchall()
    years = defaultdict(list)
    for r in rows:
        years[r["captured"][:4]].append(r)
    born_year = int(info["born"][:4]) if info["born"] else None
    out = []
    for year in sorted(years):
        items = years[year]
        examples = [{"id": r["id"], "filename": r["filename"], "captured": r["captured"], "summary": (r["summary"] or "")[:160]}
                    for r in items if r["status"] in ("ready", "partial") and r["summary"]][:3]
        entry = {"year": year, "items": len(items), "videos": sum(r["kind"] == "video" for r in items),
                 "first": items[0]["captured"][:10], "last": items[-1]["captured"][:10], "examples": examples}
        if born_year and year.isdigit():
            entry["age"] = max(0, int(year) - born_year)
        out.append(entry)
    return {"id": person_id, "name": person["name"], **info, "years": out, "items": len(rows),
            "together": [dict(r) for r in together]}


# ---- the owner's calendar

def occasions(store):
    with store.connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
    try:
        saved = json.loads(row[0]) if row else None
    except ValueError:
        saved = None
    listed = saved if isinstance(saved, list) else DEFAULT_OCCASIONS
    return {"occasions": listed, "birthdays": "Birthdays come from people's born dates (person_details).",
            "default": saved is None}


def set_occasions(store, items):
    """Replace the owner's occasions: each {name, month, day} for a fixed date every year, or {name, dates}
    for festivals whose date moves (Onam, Vishu, Eid, Diwali), listed year by year."""
    if not isinstance(items, list) or len(items) > MAX_OCCASIONS:
        raise ToolError("invalid_arguments", f"occasions is a list of up to {MAX_OCCASIONS}")
    clean = []
    for o in items:
        if not isinstance(o, dict) or not isinstance(o.get("name"), str) or not o["name"].strip() or len(o["name"]) > 60:
            raise ToolError("invalid_arguments", "Each occasion needs a name of up to 60 characters")
        if "dates" in o:
            dates = o["dates"]
            if not isinstance(dates, list) or not 1 <= len(dates) <= 100:
                raise ToolError("invalid_arguments", f"{o['name']}: dates is a list of 1–100 dates")
            for d in dates:
                try:
                    date.fromisoformat(d)
                except (TypeError, ValueError):
                    raise ToolError("invalid_arguments", f"{o['name']}: dates look like 2025-09-05") from None
            clean.append({"name": o["name"].strip(), "dates": sorted(set(dates))})
        else:
            month, day = o.get("month"), o.get("day")
            try:
                date(2024, month, day)
            except (TypeError, ValueError):
                raise ToolError("invalid_arguments", f"{o['name']}: give month and day, or dates") from None
            clean.append({"name": o["name"].strip(), "month": month, "day": day})
    with store.connect() as db:
        db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY, json.dumps(clean)))
    return {"occasions": clean}


def occasion_days(store, years):
    """{date: [occasion names]} for the given years: the owner's occasions and named people's birthdays."""
    listed = occasions(store)["occasions"]
    out = defaultdict(list)
    for o in listed:
        if "dates" in o:
            for d in o["dates"]:
                day = date.fromisoformat(d)
                if day.year in years:
                    out[day].append(o["name"])
        else:
            for y in years:
                try:
                    out[date(y, o["month"], o["day"])].append(o["name"])
                except ValueError:
                    continue
    with store.connect() as db:
        ensure(db)
        for r in db.execute("""SELECT p.name, d.born, d.relation FROM person_details d JOIN people p ON p.id=d.person_id
                WHERE length(d.born)=10 AND p.name<>''"""):
            born = date.fromisoformat(r["born"])
            for y in years:
                if y < born.year:
                    continue
                try:
                    day = date(y, born.month, born.day)
                except ValueError:
                    day = date(y, 2, 28)
                age = y - born.year
                out[day].append(f"{r['name']}'s birthday" + (f" ({age})" if age else " (born)"))
    return out
