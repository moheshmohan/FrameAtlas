"""Resumable Google page reads, a place's time zone from a nearby place, and the health report."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import time
import unittest

from frameatlas import google_reads, places
from frameatlas.mcp import handle
from frameatlas.service import Service
from frameatlas.store import Store

# Made-up places: two neighbouring towns and one far away.
TOWN = {"key": "a1", "name": "Reed Town", "address": "Reed Town, Lakeland", "latitude": 41.70, "longitude": 44.80}
VALLEY = {"key": "b2", "name": "Pine Valley", "address": "Pine Valley, Lakeland", "latitude": 42.40, "longitude": 44.50}
FAR = {"key": "c3", "name": "Far Bay", "address": "Far Bay, Oceania", "latitude": -33.80, "longitude": 151.20}


def tool(service, name, arguments=None):
    result = handle(service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}})["result"]
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


class GoogleReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def shoot(self, day, times, zone_minutes=240):
        for t in times:
            self.n += 1
            local = datetime.fromisoformat(f"{day}T{t}")
            self.store.upsert({"provider": "google_photos", "source_id": f"r{self.n}", "kind": "photo", "filename": f"IMG_{self.n}.HEIC",
                               "captured": (local - timedelta(minutes=zone_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")})

    def register(self):
        return tool(self.service, "google_read_register", {"kind": "places", "entries": [
            {"name": p["name"], "key": p["key"]} for p in (TOWN, VALLEY, FAR)]})

    def test_the_plan_moves_from_pending_to_done(self):
        plan = tool(self.service, "google_read_plan", {"kind": "places"})
        self.assertIn("list page", plan["next_step"])
        self.assertEqual(self.register()["counts"]["pending"], 3)
        self.assertEqual(self.register()["added"], 0, "registering again only adds new names")
        names = tool(self.service, "google_read_next", {"kind": "places", "count": 2})["names"]
        self.assertEqual([n["name"] for n in names], ["Far Bay", "Pine Valley"])
        self.shoot("2024-05-11", ["10:00:07", "15:21:43"])
        done = tool(self.service, "google_read_result", {"kind": "places", "name": "Reed Town", "key": "a1", "items": 2,
                                                          "complete": True, "packed": "20240511.P100007.P152143.2",
                                                          "latitude": TOWN["latitude"], "longitude": TOWN["longitude"]})
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["imported"]["labelled"], 2)
        self.assertEqual(done["counts"]["done"], 1)
        self.assertEqual(tool(self.service, "places_list")["places"][0]["catalogued"], 2)

    def test_incomplete_and_throttled_reads_wait_and_count(self):
        self.register()
        stopped = tool(self.service, "google_read_result", {"kind": "places", "name": "Far Bay", "items": 480, "complete": False})
        self.assertEqual((stopped["state"], stopped["wait_until"]), ("incomplete", None))
        throttled = tool(self.service, "google_read_result", {"kind": "places", "name": "Pine Valley", "items": 50, "complete": False})
        self.assertIn("throttling", throttled["note"])
        self.assertIsNotNone(throttled["wait_until"])
        waiting = tool(self.service, "google_read_next", {"kind": "places"})
        self.assertEqual(waiting["names"], [])
        self.assertGreater(waiting["wait_minutes"], 50)
        plan = tool(self.service, "google_read_plan", {"kind": "places"})
        self.assertEqual({n["name"] for n in plan["needs_again"]}, {"Far Bay", "Pine Valley"})
        with self.store.connect() as db:
            db.execute("UPDATE google_searches SET at=at-4000")
        nxt = tool(self.service, "google_read_next", {"kind": "places", "count": 5})["names"]
        self.assertEqual([n["name"] for n in nxt][0], "Reed Town", "never-read names come before re-reads")
        no = tool(self.service, "google_read_result", {"kind": "places", "name": "Reed Town", "outcome": "no_results"})
        self.assertEqual(no["state"], "incomplete")
        self.assertEqual(tool(self.service, "google_read_reset", {"kind": "places", "names": ["Reed Town"]})["reset"], 1)

    def test_the_hourly_budget_limits_the_next_names(self):
        self.register()
        now = time.time()
        with self.store.connect() as db:
            for i in range(google_reads.SEARCH_BUDGET - 1):
                db.execute("INSERT INTO google_searches(at,kind,name,outcome) VALUES(?,?,?,?)", (now - 60, "places", f"x{i}", "ok"))
        self.assertEqual(len(tool(self.service, "google_read_next", {"kind": "places", "count": 5})["names"]), 1)

    def test_bad_input(self):
        self.assertEqual(tool(self.service, "google_read_result", {"kind": "places", "name": "Nowhere", "items": 1})["error"]["code"], "not_found")
        bad = tool(self.service, "google_read_register", {"kind": "places", "entries": [{"name": "No Key"}]})
        self.assertEqual(bad["error"]["code"], "invalid_arguments")
        self.register()
        missing = tool(self.service, "google_read_result", {"kind": "places", "name": "Reed Town", "items": 3, "complete": True})
        self.assertEqual(missing["error"]["code"], "invalid_arguments")

    def test_the_script_steps_come_from_the_shipped_script(self):
        for step in (1, 2, 3):
            got = tool(self.service, "google_read_script", {"step": step})
            self.assertIn("(", got["script"])
            self.assertTrue(got["how"])
        self.assertIn("__NAME__", tool(self.service, "google_read_script", {"step": 3})["script"])
        self.assertIn("throttles", tool(self.service, "google_read_script", {"step": 1})["notes"])

    def test_a_place_read_alone_takes_a_nearby_places_zone(self):
        self.shoot("2024-05-13", ["11:00:07", "19:23:41"])
        first = places.import_places(self.store, "google_photos", [{**TOWN, "days": [
            {"first": "2024-05-13T11:00:07", "last": "2024-05-13T19:23:41", "items": 2}]}])
        self.assertEqual(first["labelled"], 2)
        self.shoot("2024-05-11", ["11:00:13", "12:00:29"])
        # The place's own day starts and ends a minute away from any item, so no item matches to the second.
        alone = places.import_places(self.store, "google_photos", [{**VALLEY, "days": [
            {"first": "2024-05-11T10:59:00", "last": "2024-05-11T12:01:00", "items": 2}]}])
        self.assertEqual((alone["matched"], alone["zone_from_nearby"], alone["labelled"]), (0, 1, 2))
        far = places.import_places(self.store, "google_photos", [{**FAR, "days": [
            {"first": "2024-05-12T10:59:00", "last": "2024-05-12T12:01:00", "items": 2}]}])
        self.assertEqual((far["zone_from_nearby"], far["without_zone"]), (0, 1))


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_jobs_are_grouped_by_cause_with_a_fix(self):
        for i, error in enumerate(["Google video is not ready to download", "Google video is not ready to download",
                                   "Unexpected cloud download status 500", "Something odd at /Users/someone/x.mov"]):
            aid, _ = self.store.upsert({"provider": "local", "source_id": f"h{i}", "filename": f"h{i}.mov", "kind": "video"})
            job = self.store.enqueue(aid)["id"]
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET state='failed', error=? WHERE id=?", (error, job))
        aid, _ = self.store.upsert({"provider": "insta360", "source_id": "never", "filename": "VID_1.insv", "kind": "video"})
        report = tool(self.service, "catalog_health")
        titles = [i["title"] for i in report["items"]]
        self.assertEqual(titles[0], "2 failed jobs: Google is still processing these videos")
        self.assertIn("1 failed job: Google had a server error", titles)
        odd = next(i for i in report["items"] if "Something odd" in i["title"])
        self.assertNotIn("/Users", odd["title"])
        self.assertIn("insta360", " ".join(titles))
        self.assertEqual(report["needs_action"], 3)

    def test_a_paused_queue_and_waiting_agent_calls_need_action(self):
        aid, _ = self.store.upsert({"provider": "local", "source_id": "q", "filename": "q.mov", "kind": "video"})
        self.store.enqueue(aid)
        self.store.queue_state(True)
        tool(self.service, "idea_pad_delete", {"id": 1})  # asks for a confirmation
        kinds = {i["kind"] for i in tool(self.service, "catalog_health")["items"] if i["level"] == "action"}
        self.assertEqual(kinds, {"queue_paused", "agent_waiting"})


if __name__ == "__main__":
    unittest.main()
