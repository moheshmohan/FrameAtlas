from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from frameatlas import idea_pad, places
from frameatlas.mcp import TOOLS
from frameatlas.service import Service
from frameatlas.store import Store

# Made-up places and positions: a home city, a day out nearby, a country abroad visited twice,
# and a city in another time zone.
HOME = {"key": "home1", "name": "Harbour City", "address": "Harbour City, Sandland", "latitude": 25.20, "longitude": 55.27}
NEAR = {"key": "near1", "name": "Hill Village", "address": "Hill Village, Sandland", "latitude": 24.81, "longitude": 56.13}
SKI = {"key": "ski1", "name": "Snow Pass", "address": "Snow Pass, Mountainia", "latitude": 42.48, "longitude": 44.48}
CAPITAL = {"key": "cap1", "name": "Old Capital", "address": "Old Capital, Mountainia", "latitude": 41.71, "longitude": 44.79}
COAST = {"key": "coast1", "name": "Spice Coast", "address": "Spice Coast, Greenstate, Farland", "latitude": 9.93, "longitude": 76.27}


class PlacesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)
        self.n = 0
        self.days = {}

    def tearDown(self):
        self.tmp.cleanup()

    def shoot(self, place, day, hours, zone_minutes=240, kind="photo", summary=""):
        """Catalogue items (UTC, as Google gives them) at local times on a day, and the place-day
        Google Photos would list."""
        local = [datetime.fromisoformat(f"{day}T{h:02d}:{m:02d}:{s:02d}") for h, m, s in hours]
        ids = []
        for t in local:
            self.n += 1
            utc = t - timedelta(minutes=zone_minutes)
            aid, _ = self.store.upsert({"provider": "google_photos", "source_id": f"g{self.n}", "kind": kind,
                                        "filename": f"IMG_{self.n:04d}.HEIC", "captured": utc.strftime("%Y-%m-%dT%H:%M:%SZ")})
            if summary:
                with self.store.connect() as db:
                    db.execute("UPDATE assets SET status='ready', summary=? WHERE id=?", (f"{summary} {self.n}", aid))
            ids.append(aid)
        self.days.setdefault(place["key"], []).append(
            {"first": min(local).isoformat(), "last": max(local).isoformat(), "items": len(local)})
        return ids

    def payload(self, *chosen):
        return [{**p, "days": self.days.get(p["key"], [])} for p in chosen]

    def library(self):
        """Two years at home (every third day), a day out, two trips abroad in different years and
        a long stay in another time zone."""
        away = {"2024-03-02", "2024-05-10", "2024-05-11", "2024-05-12", "2024-05-13", "2023-02-02", "2023-02-03",
                *((date(2023, 12, 22) + timedelta(days=k)).isoformat() for k in range(6))}
        d = date(2023, 1, 1)
        while d <= date(2024, 12, 31):
            if d.isoformat() not in away:
                self.shoot(HOME, d.isoformat(), [(18, 0, 0)])
            d += timedelta(days=3)
        self.near = self.shoot(NEAR, "2024-03-02", [(9, 0, 0), (12, 30, 0), (16, 0, 5)], summary="wadi pools")
        self.ski = []
        for day in ("2024-05-10", "2024-05-11", "2024-05-12"):
            self.ski += self.shoot(SKI, day, [(10, 0, 0), (15, 0, 0)], summary="snow slope")
        self.shoot(CAPITAL, "2024-05-13", [(11, 0, 0), (19, 0, 0)])
        self.shoot(SKI, "2023-02-02", [(10, 0, 0), (14, 0, 0)])
        self.shoot(SKI, "2023-02-03", [(10, 0, 0)])
        self.coast = []
        for k in range(6):
            day = (date(2023, 12, 22) + timedelta(days=k)).isoformat()
            self.coast += self.shoot(COAST, day, [(8, 15, 0), (20, 45, 30)], zone_minutes=330)

    def test_import_finds_each_places_zone_and_labels_items_between_first_and_last(self):
        self.library()
        between, _ = self.store.upsert({"provider": "google_photos", "source_id": "mid", "kind": "photo", "filename": "m.jpg",
                                        "captured": "2023-12-23T09:00:00Z"})  # 14:30 at the coast
        outside, _ = self.store.upsert({"provider": "google_photos", "source_id": "late", "kind": "photo", "filename": "l.jpg",
                                        "captured": "2023-12-23T17:00:00Z"})  # 22:30 there, after the last item
        camera, _ = self.store.upsert({"provider": "insta360", "source_id": "cam", "kind": "video", "filename": "VID.insv",
                                       "captured": "2023-12-23T09:00:00"})  # a camera clock without a zone
        out = places.import_places(self.store, "google_photos", self.payload(HOME, NEAR, SKI, CAPITAL, COAST))
        self.assertEqual(out["places"], 5)
        self.assertEqual(out["without_zone"], 0)
        with self.store.connect() as db:
            coast = db.execute("SELECT id FROM places WHERE key='coast1'").fetchone()[0]
            labelled = {r[0] for r in db.execute("SELECT asset_id FROM asset_places WHERE place_id=?", (coast,))}
            first = db.execute("SELECT first FROM place_days WHERE place_id=? AND day='2023-12-23'", (coast,)).fetchone()[0]
        self.assertEqual(first, "2023-12-23T02:45:00")  # 08:15 at UTC+5:30
        self.assertTrue(set(self.coast) <= labelled)
        self.assertIn(between, labelled)
        self.assertNotIn(outside, labelled)
        self.assertNotIn(camera, labelled)
        listed = places.list_places(self.store)["places"]
        self.assertEqual(listed[0]["name"], "Harbour City")
        self.assertEqual({p["name"]: p["country"] for p in listed}["Spice Coast"], "Farland")

    def test_trips_days_out_and_return_visits(self):
        self.library()
        places.import_places(self.store, "google_photos", self.payload(HOME, NEAR, SKI, CAPITAL, COAST))
        found = places.trips(self.store)["trips"]
        by = {(t["kind"], t["start"]): t for t in found}
        day_out = by[("day_out", "2024-03-02")]
        self.assertEqual(day_out["home"], "Harbour City")
        self.assertTrue(80 < day_out["distance_km"] < 120)
        trip = by[("trip", "2024-05-10")]
        self.assertEqual(trip["end"], "2024-05-13")
        self.assertEqual(trip["abroad"], ["Mountainia"])
        self.assertEqual(trip["places"][0], "Snow Pass ×6")
        self.assertIn("Old Capital ×2", trip["places"])
        coast = by[("trip", "2023-12-22")]
        self.assertEqual((coast["days"], coast["abroad"]), (6, ["Farland"]))
        back = [t for t in found if t["kind"] == "return"]
        self.assertEqual(len(back), 1)
        self.assertEqual((back[0]["area"], back[0]["years"]), ("Snow Pass", ["2023", "2024"]))
        self.assertEqual([v["start"] for v in back[0]["visits"]], ["2023-02-02", "2024-05-10"])
        # nothing at home is a trip, and the filters work
        self.assertFalse([t for t in found if t["area"] == "Harbour City"])
        self.assertEqual([t["kind"] for t in places.trips(self.store, kind="day_out")["trips"]], ["day_out"])
        self.assertEqual(places.trips(self.store, since="2024-05-01", until="2024-05-31", kind="trip")["total"], 1)

    def test_home_moves_with_the_owner(self):
        # a year living at the coast, then two years in the harbour city: the coast year has no trips
        d = date(2021, 1, 1)
        while d < date(2022, 1, 1):
            self.shoot(COAST, d.isoformat(), [(9, 0, 0)], zone_minutes=330)
            d += timedelta(days=3)
        self.library()
        places.import_places(self.store, "google_photos", self.payload(HOME, NEAR, SKI, CAPITAL, COAST))
        found = places.trips(self.store, until="2022-06-30")["trips"]
        self.assertEqual(found, [])
        self.assertTrue([t for t in places.trips(self.store, kind="trip")["trips"] if t["area"] == "Spice Coast"])

    def test_moments_hold_trips_and_drop_busy_days_inside_them(self):
        self.library()
        places.import_places(self.store, "google_photos", self.payload(HOME, NEAR, SKI, CAPITAL, COAST))
        found = idea_pad.moments(self.store, limit=200, today=date(2025, 1, 15))["moments"]
        kinds = {m["id"]: m for m in found}
        self.assertIn("t:2024-05-10..2024-05-13", kinds)
        trip = kinds["t:2024-05-10..2024-05-13"]
        self.assertEqual(trip["kind"], "trip")
        self.assertEqual(trip["trip"]["abroad"], ["Mountainia"])
        self.assertEqual({s["asset_id"] for s in trip["samples"]}, set(self.ski))
        self.assertTrue(trip["places"][0].startswith("Snow Pass"))
        day_out = kinds["d:2024-03-02"]
        self.assertEqual(day_out["counts"]["total"], 3)
        self.assertFalse([m for m in found if m["kind"] == "busy" and "2024-05-10" <= m["start"] <= "2024-05-13"])
        only = idea_pad.moments(self.store, limit=200, kind="return")["moments"]
        self.assertEqual([m["kind"] for m in only], ["return"])
        self.assertEqual(only[0]["trip"]["years"], ["2023", "2024"])
        with self.assertRaises(ValueError):
            idea_pad.moments(self.store, kind="holiday")

    def test_search_by_place_and_by_bounds_through_places(self):
        self.library()
        places.import_places(self.store, "google_photos", self.payload(HOME, NEAR, SKI, CAPITAL, COAST))
        listed = {p["name"]: p["id"] for p in places.list_places(self.store)["places"]}
        hits = self.service.dispatch("search", {"place": str(listed["Hill Village"]), "limit": 50})
        self.assertEqual({a["id"] for a in hits["items"]} if "items" in hits else {a["id"] for a in hits["results"]}, set(self.near))
        both = self.service.dispatch("search", {"place": f"{listed['Snow Pass']},{listed['Old Capital']}", "limit": 200})
        self.assertEqual(both["total"], 11)
        boxed = self.service.dispatch("search", {"bounds": "41,44,43,45", "limit": 200})
        self.assertEqual(boxed["total"], 11)
        with self.assertRaises(ValueError):
            self.service.dispatch("search", {"place": "1;DROP"})
        asset = self.service.dispatch("asset", {"id": self.near[0]})
        self.assertEqual([p["name"] for p in asset["places"]], ["Hill Village"])
        coverage = self.service.dispatch("metadata_coverage", {})
        self.assertEqual(coverage["total"]["location"], coverage["total"]["total"])

    def test_reimport_replaces_known_days_and_keeps_the_rest(self):
        self.shoot(SKI, "2024-05-10", [(10, 0, 0), (15, 0, 0)])
        places.import_places(self.store, "google_photos", self.payload(SKI))
        self.days = {}
        self.shoot(SKI, "2024-05-11", [(9, 0, 0)])
        out = places.import_places(self.store, "google_photos", [{"key": "ski1", "name": "Snow Pass",
                                                                   "days": self.days["ski1"] + [{"first": "2024-05-10T10:00:00", "last": "2024-05-10T15:00:00", "items": 5}]}])
        self.assertEqual(out["places"], 1)
        with self.store.connect() as db:
            rows = dict(db.execute("SELECT day, items FROM place_days").fetchall())
            kept = db.execute("SELECT latitude, address FROM places").fetchone()
        self.assertEqual(rows, {"2024-05-10": 5, "2024-05-11": 1})
        self.assertEqual(tuple(kept), (42.48, "Snow Pass, Mountainia"))  # not wiped by a re-read without them

    def test_bad_input_is_refused(self):
        good = {"key": "k1", "name": "A", "days": [{"first": "2024-01-01T10:00:00", "last": "2024-01-01T11:00:00", "items": 2}]}
        for bad in ([], "x", [{"name": "A"}], [{**good, "key": "bad key!"}], [{**good, "latitude": 95, "longitude": 1}],
                    [{**good, "latitude": 10}], [{**good, "days": [{"first": "2024-01-01", "last": "2024-01-01T11:00:00", "items": 2}]}],
                    [{**good, "days": [{"first": "2024-01-01T10:00:00", "last": "2024-01-01T11:00:00", "items": 0}]}],
                    [{**good, "days": "x"}], [good] * 2001):
            with self.assertRaises(ValueError):
                places.import_places(self.store, "google_photos", bad)
        with self.assertRaises(ValueError):
            places.import_places(self.store, "other", [good])
        with self.assertRaises(ValueError):
            self.service.dispatch("places_import", {"places": [good], "apply": True})
        with self.assertRaises(ValueError):
            self.service.dispatch("places_trips", {"since": "May"})
        with self.assertRaises(ValueError):
            self.service.dispatch("places_trips", {"kind": "cruise"})
        with self.assertRaises(ValueError):
            self.service.dispatch("places_forget", {})
        self.assertEqual(self.service.dispatch("places_trips", {})["trips"], [])
        # a place without a position is kept, but cannot place a trip
        out = self.service.dispatch("places_import", {"places": [good]})
        self.assertEqual(out["without_position"], 1)
        self.assertEqual(self.service.dispatch("places_trips", {})["trips"], [])
        self.assertEqual(self.service.dispatch("places_forget", {"confirm": True})["forgotten"], 1)

    def test_packed_days_from_the_browser_script(self):
        ids = self.shoot(SKI, "2024-05-10", [(10, 0, 0), (15, 0, 7)])
        out = places.import_places(self.store, "google_photos", [{**SKI, "packed": "20240510.P100000.V150007.2"}])
        self.assertEqual((out["days"], out["labelled"]), (1, 2))
        self.assertEqual(places.unpack("20240510.P100000.V150007.2 20240511.V090000.V090000.1")[1],
                         {"first": "2024-05-11T09:00:00", "last": "2024-05-11T09:00:00", "items": 1,
                          "first_kind": "video", "last_kind": "video"})
        for bad in ("2024-05-10.P100000.V150007.2", "20240510.X100000.V150007.2", "20240510.P100000.V150007", 5, "x" * 1_000_001):
            with self.assertRaises(ValueError):
                places.unpack(bad)
        self.assertTrue(ids)
        script = (Path(__file__).resolve().parents[1] / "scripts/google_photos_places.js").read_text()
        self.assertIn("${day}.${d.first}.${d.last}.${d.n}", script)  # the form unpack reads

    def test_plus_codes_stand_in_for_a_missing_position(self):
        self.assertEqual(places.plus_code("8Q7F53FC+6HV"), {"latitude": 35.173113, "longitude": 129.071422})
        for bad in ("8Q7F+53FC6HV", "XX7F53FC+6HV", "8Q7F53FC+", "8Q7F53FC6HV", "", None, 5, "8" * 30):
            self.assertIsNone(places.plus_code(bad))
        places.import_places(self.store, "google_photos", [{"key": "p1", "name": "Port", "plus_code": "8Q7F53FC+6HV"}])
        self.assertEqual(places.list_places(self.store)["places"][0]["latitude"], 35.173113)
        with self.assertRaises(ValueError):
            places.import_places(self.store, "google_photos", [{"key": "p2", "name": "Nowhere", "plus_code": "nope"}])

    def test_agent_tools(self):
        names = {t[0] for t in TOOLS}
        self.assertTrue({"places_import", "places_list", "places_trips"} <= names)
        search = next(t for t in TOOLS if t[0] == "catalog_search")
        self.assertIn("place", search[3])
        moments = next(t for t in TOOLS if t[0] == "idea_pad_moments")
        self.assertIn("return", moments[3]["kind"]["enum"])


if __name__ == "__main__":
    unittest.main()
