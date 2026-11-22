"""How people are related to the owner, their birthdays and years, and occasions as moments."""
from datetime import date
import json
from pathlib import Path
import tempfile
import time
import unittest

from frameatlas import family, idea_pad, people
from frameatlas.mcp import handle
from frameatlas.service import Service
from frameatlas.store import Store


class FamilyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)
        now = time.time()
        with self.store.connect() as db:
            people.ensure(db)
            db.execute("INSERT INTO people(id,name,created,updated) VALUES(1,'Kiran',?,?),(2,'Mira',?,?)", (now, now, now, now))
        self.items = []
        for i, (day, who) in enumerate([("2021-04-20T10:00:00Z", (1,)), ("2021-04-21T10:00:00Z", (1, 2)), ("2021-04-21T11:00:00Z", (1,)),
                                        ("2023-04-21T09:00:00Z", (1, 2)), ("2023-04-21T09:30:00Z", (1,)), ("2023-12-25T09:30:00Z", (2,)),
                                        ("2023-12-25T10:30:00Z", (2,))]):
            aid, _ = self.store.upsert({"provider": "local", "source_id": f"k{i}", "filename": f"IMG_{i}.HEIC", "kind": "photo", "captured": day})
            with self.store.connect() as db:
                db.execute("UPDATE assets SET status='ready', summary=? WHERE id=?", (f"A garden party {i}", aid))
                for pid in who:
                    db.execute("""INSERT INTO faces(asset_id, source, bbox, landmarks, score, side, frontal, embedding, model,
                        person_id, created) VALUES(?, 'preview', '[]', '[]', 0.9, 100, 1, x'00', 'made-up', ?, ?)""", (aid, pid, now))
            self.items.append(aid)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name, arguments=None):
        result = handle(self.service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}})["result"]
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def test_details_and_timeline(self):
        self.assertEqual(self.call("person_details", {"id": 1, "relation": "our son", "born": "2019-04-21"}),
                         {"id": 1, "relation": "our son", "born": "2019-04-21"})
        self.assertEqual(self.call("person_details", {"id": 1, "relation": "our son"})["born"], "2019-04-21", "an update keeps the other field")
        for bad in ({"id": 1, "born": "April"}, {"id": 1, "born": "2019-02-30"}, {"id": 99, "relation": "x"}):
            with self.subTest(bad=bad):
                self.assertIn("error", self.call("person_details", bad))
        t = self.call("person_timeline", {"id": 1})
        self.assertEqual([(y["year"], y["items"], y["age"]) for y in t["years"]], [("2021", 3, 2), ("2023", 2, 4)])
        self.assertEqual(t["together"][0]["name"], "Mira")
        self.assertEqual(t["relation"], "our son")
        self.assertEqual(len(t["years"][0]["examples"]), 3)

    def test_occasions_and_birthdays_become_moments(self):
        self.assertTrue(self.call("calendar")["default"])
        self.call("calendar_set", {"occasions": [{"name": "Christmas", "month": 12, "day": 25},
                                                 {"name": "Spring fair", "dates": ["2021-04-20"]}]})
        self.assertIn("error", self.call("calendar_set", {"occasions": [{"name": "Bad", "month": 2, "day": 30}]}))
        self.assertIn("error", self.call("calendar_set", {"occasions": [{"name": "Bad", "dates": ["soon"]}]}))
        family.set_details(self.store, 1, born="2019-04-21")
        found = idea_pad.moments(self.store, kind="occasion", today=date(2024, 1, 1))["moments"]
        got = {(m["start"], tuple(m["occasion"])) for m in found}
        self.assertIn(("2023-12-25", ("Christmas",)), got)
        self.assertIn(("2021-04-21", ("Kiran's birthday (2)",)), got)
        self.assertIn(("2023-04-21", ("Kiran's birthday (4)",)), got)
        self.assertNotIn("2021-04-20", {m["start"] for m in found}, "one photo is not enough for a moment")

    def test_idea_writing_sees_relations_as_given(self):
        family.set_details(self.store, 1, relation="our son", born="2019-04-21")
        reply = handle(self.service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "idea_pad_generate", "arguments": {"count": 1, "kind": "occasion", "writer": "agent"}}})["result"]["structuredContent"]
        moment = next(m for m in json.loads(reply["needs_writing"]["user"])["moments"] if "Kiran's birthday (4)" in m["occasion"])
        self.assertEqual(moment["relations"], {"Kiran": "our son"})
        self.assertIn("use it exactly as given", reply["needs_writing"]["system"])


if __name__ == "__main__":
    unittest.main()
