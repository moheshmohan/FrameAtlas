"""Places from another library (Google Photos' Places), and the trips, days out and return visits
they show.

Google Photos groups located photos by place (a city, a district, a landmark) but neither its
Picker nor its downloads say where an item was taken. An agent reads the Places page in the
owner's own browser: for each place its name, Google's place key and, from Google Maps, its
position and address; and for each day there the local capture time of the first and last item
and how many there were. `import_places` keeps that, finds each place's time zone by matching
those items to the catalogue to the second, and labels the catalogue items taken there between
the first and last item of each day.

`trips` reads the days back: places close together form an area, home is the area seen on the
most days around each day (it moves when the owner moves), and runs of days away from home are
trips (or a day out when it is one day close by). Areas visited again in other years are return
visits. Nothing leaves the Mac: the positions are Google's own, read in the owner's browser.
"""
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import math
import re
import time

from .geography import coordinates

SOURCES = ("google_photos",)
MAX_PLACES, MAX_DAYS = 2000, 20000
AREA_KM = 30           # places this close to an area's first place belong to that area
NEAR_KM = 25           # closer than this to home is home
DAY_OUT_KM = 250       # a single day away and closer than this is a day out, not a trip
RUN_GAP_DAYS = 2       # days without a place inside a run of days away
HOME_WINDOW_DAYS = 120 # home is the area seen on most days within this many days either side
HOME_PRIOR = 0.02      # and an area's days overall break ties where the owner took few photos
KINDS = ("trip", "day_out", "return")
LOCAL = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
KEY = re.compile(r"[0-9A-Za-z:_-]{1,80}")
PACKED = re.compile(r"(\d{4})(\d{2})(\d{2})\.([PV])(\d{2})(\d{2})(\d{2})\.([PV])(\d{2})(\d{2})(\d{2})\.(\d{1,6})")
MAX_PACKED = 1_000_000


def unpack(packed):
    """Days from the browser script's packed form: "YYYYMMDD.<P|V>HHMMSS.<P|V>HHMMSS.N" per day."""
    if not isinstance(packed, str) or len(packed) > MAX_PACKED:
        raise ValueError("packed is text of up to a million characters")
    days, kinds = [], {"P": "photo", "V": "video"}
    for token in packed.split():
        m = PACKED.fullmatch(token)
        if not m:
            raise ValueError(f"Unreadable day {token[:40]!r}: use YYYYMMDD.PHHMMSS.VHHMMSS.N")
        y, mo, d, fk, fh, fm, fs, lk, lh, lm, ls, n = m.groups()
        days.append({"first": f"{y}-{mo}-{d}T{fh}:{fm}:{fs}", "last": f"{y}-{mo}-{d}T{lh}:{lm}:{ls}", "items": int(n),
                     "first_kind": kinds[fk], "last_kind": kinds[lk]})
    return days


def _text(value, limit, name):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return " ".join(value.split())[:limit]


def _local(value):
    if not isinstance(value, str) or not LOCAL.fullmatch(value[:19]):
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _kind(value):
    return value if value in ("photo", "video") else None


class _Index:
    """The catalogue's capture times (UTC, to the second) for matching and for time windows."""

    def __init__(self, db):
        self.exact = defaultdict(list)
        rows = []
        for aid, captured, kind in db.execute("SELECT id, captured, kind FROM assets WHERE captured<>''"):
            # only times with a zone: a camera clock without one cannot be placed in UTC
            try:
                moment = datetime.fromisoformat(captured.strip()[:40].replace("Z", "+00:00"))
            except ValueError:
                continue
            if moment.tzinfo is None or len(captured) < 19:
                continue
            key = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            self.exact[key].append((aid, kind))
            rows.append((key, aid))
        rows.sort()
        self.keys = [k for k, _ in rows]
        self.ids = [a for _, a in rows]

    def offsets(self, local, kind):
        """Quarter-hour offsets (local minus UTC) at which exactly one catalogue item matches."""
        found = set()
        for q in range(-48, 57):
            key = (local - timedelta(minutes=15 * q)).strftime("%Y-%m-%dT%H:%M:%S")
            if len([a for a, k in self.exact.get(key, ()) if not kind or k == kind]) == 1:
                found.add(q)
        return found

    def between(self, first, last):
        return self.ids[bisect_left(self.keys, first):bisect_right(self.keys, last)]


OLC = "23456789CFGHJMPQRVWX"


def plus_code(code):
    """The centre of a full Open Location Code ("plus code", for example 8Q7F53FC+6HV), which
    Google Maps gives for a place when its address has no position; None when it is not one."""
    if not isinstance(code, str) or len(code) > 20:
        return None
    text = code.strip().upper()
    if text.count("+") != 1 or text.index("+") != 8:
        return None
    text = text.replace("+", "")
    if len(text) < 10 or any(c not in OLC for c in text) or OLC.index(text[0]) > 8 or OLC.index(text[1]) > 17:
        return None
    lat, lng, step = -90.0, -180.0, 20.0
    for i in range(0, 10, 2):
        lat += OLC.index(text[i]) * step
        lng += OLC.index(text[i + 1]) * step
        size_lat = size_lng = step
        step /= 20
    for c in text[10:]:
        size_lat, size_lng = size_lat / 5, size_lng / 4
        lat += (OLC.index(c) // 4) * size_lat
        lng += (OLC.index(c) % 4) * size_lng
    return coordinates((round(lat + size_lat / 2, 6), round(lng + size_lng / 2, 6)))


def _country(address):
    """The last part of a Google address ("Old Town, Some State, Farland" → Farland); none for one part."""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    return parts[-1][:80] if len(parts) > 1 else ""


def import_places(store, source, places):
    """Keep another library's places and their days, and label the catalogue items taken there.

    places: [{key, name, address?, latitude?, longitude?, days: [{first, last, items, first_kind?,
    last_kind?}]}] with first and last the local capture times (YYYY-MM-DDTHH:MM:SS) of the day's
    first and last item at that place (or packed: the browser script's text form of the same days). Days already known are replaced; others are kept, so a place
    can be read in parts."""
    if source not in SOURCES:
        raise ValueError("source is google_photos")
    if not isinstance(places, list) or not places or len(places) > MAX_PLACES:
        raise ValueError(f"places is a list of 1–{MAX_PLACES} places")
    clean = []
    for p in places:
        if not isinstance(p, dict):
            raise ValueError("Every place is an object")
        key, name = p.get("key"), _text(p.get("name"), 120, "Name")
        if not isinstance(key, str) or not KEY.fullmatch(key) or not name:
            raise ValueError("Every place needs a key (Google's place id) and a name")
        lat, lon = p.get("latitude"), p.get("longitude")
        position = coordinates((lat, lon)) if lat is not None or lon is not None else None
        if (lat is not None or lon is not None) and position is None:
            raise ValueError(f"{name}: latitude and longitude must be a real position")
        if position is None and p.get("plus_code"):
            position = plus_code(p["plus_code"])
            if position is None:
                raise ValueError(f"{name}: plus_code must be a full plus code like 8Q7F53FC+6HV")
        address = _text(p.get("address"), 200, "Address")
        days = p.get("days") or []
        if p.get("packed") and isinstance(days, list):
            days = days + unpack(p["packed"])
        if not isinstance(days, list) or len(days) > MAX_DAYS:
            raise ValueError(f"{name}: days is a list of up to {MAX_DAYS} days")
        parsed = []
        for d in days:
            first, last = (_local(d.get("first")), _local(d.get("last"))) if isinstance(d, dict) else (None, None)
            items = d.get("items") if isinstance(d, dict) else None
            if first is None or last is None or type(items) is not int or not 1 <= items <= 100000:
                raise ValueError(f"{name}: every day needs first and last (YYYY-MM-DDTHH:MM:SS) and items (1–100000)")
            if last < first:
                first, last = last, first
            parsed.append((first, last, items, _kind(d.get("first_kind")), _kind(d.get("last_kind"))))
        clean.append((key, name, address, position, parsed))
    out = {"places": 0, "days": 0, "matched": 0, "labelled": 0, "without_zone": 0, "without_position": 0, "zone_from_nearby": 0}
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _zone_column(db)
        index = _Index(db)
        now = time.time()
        zones, pending = Counter(), []
        for key, name, address, position, days in clean:
            row = db.execute("SELECT id, address, latitude FROM places WHERE source=? AND key=?", (source, key)).fetchone()
            if row is None:
                pid = db.execute("""INSERT INTO places(source,key,name,address,country,latitude,longitude,updated)
                    VALUES(?,?,?,?,?,?,?,?)""", (source, key, name, address, _country(address),
                                                position and position["latitude"], position and position["longitude"], now)).lastrowid
            else:
                pid = row["id"]
                db.execute("""UPDATE places SET name=?, address=COALESCE(NULLIF(?,''),address),
                    country=COALESCE(NULLIF(?,''),country), latitude=COALESCE(?,latitude),
                    longitude=COALESCE(?,longitude), updated=? WHERE id=?""",
                           (name, address, _country(address), position and position["latitude"],
                            position and position["longitude"], now, pid))
            if position is None and (row is None or row["latitude"] is None):
                out["without_position"] += 1
            votes = Counter()
            for first, last, _, fk, lk in days:
                for local, kind in ((first, fk), (last, lk)):
                    found = index.offsets(local, kind)
                    if len(found) == 1:
                        votes[next(iter(found))] += 1
                        out["matched"] += 1
            zones.update(votes)
            pending.append((pid, days, votes))
            out["places"] += 1
        common = zones.most_common(1)[0][0] if zones else None
        for pid, days, votes in pending:
            # A place's own matches set its zone (kept for later reads). A place with none keeps the zone
            # it had, else takes the zone of the nearest place within 500 km that has one, else the zone
            # most places in this import had.
            if votes:
                q = votes.most_common(1)[0][0]
                db.execute("UPDATE places SET zone_q=? WHERE id=?", (q, pid))
            else:
                q = _known_zone(db, pid)
                if q is None:
                    q = _nearby_zone(db, pid)
                    if q is not None:
                        out["zone_from_nearby"] += 1
                if q is None:
                    q = common
            for first, last, items, _, _ in days:
                if q is None:
                    utc_first = utc_last = ""
                    out["without_zone"] += 1
                else:
                    shift = timedelta(minutes=15 * q)
                    utc_first = (first - shift).strftime("%Y-%m-%dT%H:%M:%S")
                    utc_last = (last - shift).strftime("%Y-%m-%dT%H:%M:%S")
                    for aid in index.between(utc_first, utc_last):
                        out["labelled"] += db.execute("INSERT OR IGNORE INTO asset_places(asset_id,place_id) VALUES(?,?)",
                                                      (aid, pid)).rowcount
                db.execute("INSERT OR REPLACE INTO place_days(place_id,day,items,first,last) VALUES(?,?,?,?,?)",
                           (pid, first.date().isoformat(), items, utc_first, utc_last))
                out["days"] += 1
    return out


def _zone_column(db):
    if "zone_q" not in [r[1] for r in db.execute("PRAGMA table_info(places)")]:
        db.execute("ALTER TABLE places ADD COLUMN zone_q INTEGER")


def _known_zone(db, pid):
    row = db.execute("SELECT zone_q FROM places WHERE id=?", (pid,)).fetchone()
    return row["zone_q"] if row and row["zone_q"] is not None else None


NEARBY_KM = 500


def _nearby_zone(db, pid):
    """The time zone (in quarter hours) of the nearest place within 500 km whose zone is known."""
    me = db.execute("SELECT latitude, longitude FROM places WHERE id=?", (pid,)).fetchone()
    if not me or me["latitude"] is None or me["longitude"] is None:
        return None
    best = None
    for r in db.execute("""SELECT latitude, longitude, zone_q FROM places WHERE id<>? AND zone_q IS NOT NULL
            AND latitude IS NOT NULL AND longitude IS NOT NULL""", (pid,)):
        d = km((me["latitude"], me["longitude"]), (r["latitude"], r["longitude"]))
        if d <= NEARBY_KM and (best is None or d < best[0]):
            best = (d, r["zone_q"])
    return best[1] if best else None


def list_places(store, query="", limit=200, offset=0):
    if type(limit) is not int or not 1 <= limit <= 2000 or type(offset) is not int or offset < 0:
        raise ValueError("Use a limit of 1–2000 and an offset of 0 or more")
    query = _text(query, 120, "Query")
    with store.connect() as db:
        where, args = ("WHERE p.name LIKE ? OR p.address LIKE ?", [f"%{query}%"] * 2) if query else ("", [])
        total = db.execute(f"SELECT count(*) FROM places p {where}", args).fetchone()[0]
        rows = db.execute(f"""SELECT p.id, p.source, p.name, p.address, p.country, p.latitude, p.longitude,
            (SELECT count(*) FROM place_days d WHERE d.place_id=p.id) AS days,
            (SELECT COALESCE(sum(items),0) FROM place_days d WHERE d.place_id=p.id) AS items,
            (SELECT min(day) FROM place_days d WHERE d.place_id=p.id) AS first_day,
            (SELECT max(day) FROM place_days d WHERE d.place_id=p.id) AS last_day,
            (SELECT count(*) FROM asset_places a WHERE a.place_id=p.id) AS catalogued
            FROM places p {where} ORDER BY items DESC, p.name LIMIT ? OFFSET ?""", args + [limit, offset]).fetchall()
    return {"places": [dict(r) for r in rows], "total": total}


def names_by_asset(db, asset_ids=None):
    """Place names for catalogue items (all of them, or the given ones)."""
    out = defaultdict(list)
    if asset_ids is not None:
        asset_ids = list(asset_ids)
        for i in range(0, len(asset_ids), 500):
            chunk = asset_ids[i:i + 500]
            for aid, name in db.execute(f"""SELECT a.asset_id, p.name FROM asset_places a JOIN places p ON p.id=a.place_id
                    WHERE a.asset_id IN ({','.join('?' * len(chunk))})""", chunk):
                out[aid].append(name)
        return out
    for aid, name in db.execute("SELECT a.asset_id, p.name FROM asset_places a JOIN places p ON p.id=a.place_id"):
        out[aid].append(name)
    return out


def forget(store, source="google_photos"):
    if source not in SOURCES:
        raise ValueError("source is google_photos")
    with store.connect() as db:
        n = db.execute("DELETE FROM places WHERE source=?", (source,)).rowcount
    return {"forgotten": n}


# ---- trips

def km(a, b):
    """Great-circle distance between two (latitude, longitude) pairs."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _areas(places):
    """Group places into areas: each place joins the first area (by size) within AREA_KM."""
    areas, of = [], {}
    for p in sorted(places, key=lambda p: -p["items"]):
        centre = (p["latitude"], p["longitude"])
        area = next((a for a in areas if km(a["centre"], centre) <= AREA_KM), None)
        if area is None:
            area = {"id": len(areas), "centre": centre, "name": p["name"], "places": [], "countries": Counter()}
            areas.append(area)
        area["places"].append(p)
        if p["country"]:
            area["countries"][p["country"]] += p["items"]
        of[p["id"]] = area
    for a in areas:
        a["country"] = a["countries"].most_common(1)[0][0] if a["countries"] else ""
    return areas, of


def _load(store):
    with store.connect() as db:
        places = [dict(r) for r in db.execute("""SELECT p.id, p.name, p.country, p.latitude, p.longitude,
            COALESCE((SELECT sum(items) FROM place_days d WHERE d.place_id=p.id),0) AS items
            FROM places p WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL""")]
        days = db.execute("""SELECT d.place_id, d.day, d.items FROM place_days d JOIN places p ON p.id=d.place_id
            WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL""").fetchall()
    return places, days


def trips(store, since="", until="", kind="", limit=200):
    """Trips away from home, days out near it, and areas visited again in other years."""
    if kind not in ("", *KINDS):
        raise ValueError("kind is trip, day_out or return")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Use a limit of 1–1000")
    start = date.fromisoformat(since) if since else None
    end = date.fromisoformat(until) if until else None
    places, rows = _load(store)
    if not places or not rows:
        return {"trips": [], "located_days": 0, "areas": 0}
    areas, of = _areas(places)
    by_id = {p["id"]: p for p in places}
    per_day = defaultdict(Counter)   # day -> area id -> items
    place_day = defaultdict(Counter) # day -> place id -> items
    for pid, day, items in rows:
        try:
            d = date.fromisoformat(day)
        except ValueError:
            continue
        per_day[d][of[pid]["id"]] += items
        place_day[d][pid] += items
    located = sorted(per_day)
    main = {d: per_day[d].most_common(1)[0][0] for d in located}
    total_days = Counter(main.values())
    # home on each day: the area seen on most days nearby, with a small pull towards the area
    # seen most overall (so a year of only holiday photos does not make the holiday home)
    home, lo, hi, window = {}, 0, 0, Counter()
    for d in located:
        while hi < len(located) and (located[hi] - d).days <= HOME_WINDOW_DAYS:
            window[main[located[hi]]] += 1
            hi += 1
        while (d - located[lo]).days > HOME_WINDOW_DAYS:
            window[main[located[lo]]] -= 1
            lo += 1
        home[d] = max(window, key=lambda a: (window[a] + HOME_PRIOR * total_days[a], -a))

    def away_from(d):
        h = areas[home[d]]
        far = [(km(h["centre"], areas[a]["centre"]), a) for a in per_day[d] if a != h["id"]]
        far = [(k, a) for k, a in far if k >= NEAR_KM]
        return max(far) if far else None

    runs, current = [], []
    for d in located:
        far = away_from(d)
        if far is None:
            if current:
                runs.append(current); current = []
            continue
        if current and (d - current[-1][0]).days > RUN_GAP_DAYS + 1:
            runs.append(current); current = []
        current.append((d, far))
    if current:
        runs.append(current)
    out = []
    for run in runs:
        first, last = run[0][0], run[-1][0]
        h = areas[home[first]]
        span = (last - first).days + 1
        distance = max(k for _, (k, _) in run)
        names, countries, items, visited = Counter(), Counter(), 0, set()
        for d, _ in run:
            for pid, n in place_day[d].items():
                if of[pid]["id"] == h["id"]:
                    continue
                names[by_id[pid]["name"]] += n
                visited.add(of[pid]["id"])
                if by_id[pid]["country"]:
                    countries[by_id[pid]["country"]] += n
                items += n
        # Google's addresses for some places carry no country ("Old Town - Harbour City"), so a
        # trip is abroad wherever its country differs from home's, known or not
        abroad = [c for c, _ in countries.most_common() if c != h["country"]]
        what = "day_out" if span == 1 and distance < DAY_OUT_KM else "trip"
        main_area = Counter({a: sum(per_day[d][a] for d, _ in run) for a in visited}).most_common(1)[0][0]
        out.append({"id": f"{'d' if what == 'day_out' else 't'}:{first.isoformat()}" + (f"..{last.isoformat()}" if last != first else ""),
                    "kind": what, "start": first.isoformat(), "end": last.isoformat(), "days": span,
                    "located_days": len(run), "home": h["name"], "distance_km": round(distance),
                    "countries": [c for c, _ in countries.most_common(6)], "abroad": abroad,
                    "places": [f"{k} ×{v}" for k, v in names.most_common(8)], "items": items,
                    "area": areas[main_area]["name"], "area_id": main_area,
                    "place_ids": sorted({pid for d, _ in run for pid in place_day[d] if of[pid]["id"] != h["id"]})})
    # return visits: an area away from home seen on runs in two or more different years
    visits = defaultdict(list)
    for t in out:
        visits[t["area_id"]].append(t)
    returns = []
    for aid, ts in visits.items():
        years = sorted({t["start"][:4] for t in ts})
        if len(years) < 2:
            continue
        a = areas[aid]
        returns.append({"id": f"r:{a['name']}:{years[0]}..{years[-1]}"[:120], "kind": "return",
                        "start": ts[0]["start"], "end": ts[-1]["end"], "days": sum(t["days"] for t in ts),
                        "home": ts[-1]["home"], "distance_km": max(t["distance_km"] for t in ts),
                        "countries": [a["country"]] if a["country"] else [], "abroad": [],
                        "places": [f"{p['name']} ×{p['items']}" for p in sorted(a["places"], key=lambda p: -p["items"])[:8]],
                        "items": sum(t["items"] for t in ts), "area": a["name"], "area_id": aid,
                        "visits": [{"start": t["start"], "end": t["end"], "kind": t["kind"]} for t in ts],
                        "years": years, "place_ids": sorted(p["id"] for p in a["places"])})
    every = out + returns
    if start or end:
        every = [t for t in every if (not start or t["end"] >= start.isoformat()) and (not end or t["start"] <= end.isoformat())]
    if kind:
        every = [t for t in every if t["kind"] == kind]
    every.sort(key=lambda t: t["start"], reverse=True)
    return {"trips": every[:limit], "total": len(every), "located_days": len(located), "areas": len(areas)}
