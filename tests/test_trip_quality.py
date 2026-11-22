"""Trips that are really someone else's photos, and items inside two places' windows on one day."""
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from frameatlas import places
from frameatlas.store import Store

# Made-up places: home, a far coast, and a small stop inside the home town.
HOME = {"key": "h1", "name": "Dune City", "address": "Dune City, Sandland", "latitude": 25.20, "longitude": 55.27}
STOP = {"key": "s1", "name": "Old Souk", "address": "Old Souk, Dune City, Sandland", "latitude": 25.26, "longitude": 55.30}
COAST = {"key": "c1", "name": "Palm Coast", "address": "Palm Coast, Greenstate, Farland", "latitude": 9.93, "longitude": 76.27}


class TripQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.n = 0
        self.days = {}

    def tearDown(self):
        self.tmp.cleanup()

    def shoot(self, place, day, times, zone=240, name="IMG_{n:04d}.HEIC"):
        local = [datetime.fromisoformat(f"{day}T{t}") for t in times]
        ids = []
        for t in local:
            self.n += 1
            aid, _ = self.store.upsert({"provider": "google_photos", "source_id": f"q{self.n}", "kind": "photo",
                                        "filename": name.format(n=self.n), "captured": (t - timedelta(minutes=zone)).strftime("%Y-%m-%dT%H:%M:%SZ")})
            ids.append(aid)
        self.days.setdefault(place["key"], []).append({"first": min(local).isoformat(), "last": max(local).isoformat(), "items": len(local)})
        return ids

    def home_life(self, skip=()):
        d = date(2024, 1, 1)
        k = 0
        while d <= date(2024, 6, 30):
            if d.isoformat() not in skip:
                k += 1
                self.shoot(HOME, d.isoformat(), [f"09:{k % 50 + 10:02d}:{k % 47 + 10:02d}", f"18:{k % 41 + 10:02d}:{k % 43 + 10:02d}"])
            d += timedelta(days=2)

    def load(self, *chosen):
        places.import_places(self.store, "google_photos", [{**p, "days": self.days.get(p["key"], [])} for p in chosen])
        return places.trips(self.store, limit=100)

    def test_family_photos_from_far_away_on_a_day_at_home_are_not_a_trip(self):
        self.home_life()
        # the same hours as the photos at home, 2,800 km away: someone else's phone
        self.shoot(COAST, "2024-03-01", ["12:11:07", "13:05:31"], zone=330)  # a day with photos at home
        result = self.load(HOME, COAST)
        self.assertEqual([t for t in result["trips"] if t["kind"] == "trip"], [])
        self.assertEqual(result["runs_left_out_as_someone_elses"], 1)

    def test_a_real_trip_stays_even_if_someone_at_home_sent_photos(self):
        away = {(date(2024, 4, 1) + timedelta(days=k)).isoformat() for k in range(6)}
        self.home_life(skip=away)
        for k, day in enumerate(sorted(away)):
            self.shoot(COAST, day, [f"08:{10 + k}:13", f"20:{10 + k}:41"], zone=330)
        self.shoot(HOME, "2024-04-03", ["11:00:19", "12:00:23"])  # sent from home while away
        trips = [t for t in self.load(HOME, COAST)["trips"] if t["kind"] == "trip"]
        self.assertEqual([(t["start"], t["end"]) for t in trips], [("2024-04-01", "2024-04-06")])

    def test_a_far_day_made_only_of_sent_photos_is_left_out(self):
        self.home_life(skip={"2024-03-03", "2024-03-05"})
        self.shoot(COAST, "2024-03-04", ["10:11:07", "16:05:31"], zone=330, name="IMG-20240304-WA{n:04d}.jpg")
        result = self.load(HOME, COAST)
        self.assertEqual([t for t in result["trips"] if t["kind"] == "trip"], [])
        self.assertEqual(result["shared_days_left_out"], 1)

    def test_an_item_inside_two_places_keeps_the_narrower_one(self):
        whole_day = self.shoot(HOME, "2024-02-10", ["09:00:11", "20:00:47"])
        stop = self.shoot(STOP, "2024-02-10", ["12:00:13", "13:00:29"])
        middle, _ = self.store.upsert({"provider": "google_photos", "source_id": "mid", "kind": "photo", "filename": "IMG_MID.HEIC",
                                       "captured": "2024-02-10T08:30:00Z"})
        out = places.import_places(self.store, "google_photos", [{**p, "days": self.days[p["key"]]} for p in (HOME, STOP)])
        self.assertGreater(out["overlaps_tidied"], 0)
        with self.store.connect() as db:
            labels = {aid: sorted(r[0] for r in db.execute("SELECT place_id FROM asset_places WHERE asset_id=?", (aid,)))
                      for aid in (middle, whole_day[0], *stop)}
            ids = {r["key"]: r["id"] for r in db.execute("SELECT id, key FROM places")}
        self.assertEqual(labels[middle], [ids["s1"]], "inside both windows: the stop, not the whole town")
        self.assertEqual(labels[whole_day[0]], [ids["h1"]], "outside the stop's window: the town")
        self.assertEqual(places.tidy(self.store), {"labels_removed": 0}, "tidying again changes nothing")


if __name__ == "__main__":
    unittest.main()
