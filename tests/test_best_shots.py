"""Best shots from measured picture quality, and quality in the story writer's evidence."""
import json
from pathlib import Path
import tempfile
import unittest

from frameatlas.mcp import handle
from frameatlas.service import Service
from frameatlas.store import Store


class BestShotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)
        self.ids = []
        for i, (day, scores) in enumerate([("2024-05-25", (0.91, 0.95, 0.5)), ("2024-05-25", (0.97,)), ("2024-05-26", (0.6, 0.99)),
                                          ("2024-07-01", (0.98,))]):
            aid, _ = self.store.upsert({"provider": "local", "source_id": f"b{i}", "filename": f"VID_{i}.mp4", "kind": "video",
                                        "captured": f"{day}T10:0{i}:00Z"})
            self.store.analysis(aid, [{"start": 2.0 * k, "end": 2.0 * k + 1.5, "description": {"caption": f"Waves {i}.{k}"},
                                       "quality": {"score": sc, "sharpness": sc, "exposure": 0.8, "shake": 0.1}}
                                      for k, sc in enumerate(scores)], {}, {"speech_status": "no_audio"})
            self.ids.append(aid)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name, arguments=None):
        result = handle(self.service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}})["result"]
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def test_best_first_spread_over_items_and_days(self):
        with self.store.connect() as db:
            self.assertIsNotNone(db.execute("SELECT json_extract(details,'$.quality.score') FROM scenes LIMIT 1").fetchone()[0],
                                 "analysis keeps the measured quality")
        got = self.call("catalog_best_shots", {"since": "2024-05-25", "until": "2024-05-26", "per_item": 1})
        self.assertEqual(got["items_considered"], 3)
        self.assertEqual([(s["asset_id"], s["score"]) for s in got["shots"]],
                         [(self.ids[2], 0.99), (self.ids[1], 0.97), (self.ids[0], 0.95)])
        two = self.call("catalog_best_shots", {"since": "2024-05-25", "until": "2024-05-26"})
        self.assertEqual(sum(s["asset_id"] == self.ids[0] for s in two["shots"]), 2)
        one_a_day = self.call("catalog_best_shots", {"per_day": 1})
        self.assertEqual([s["captured"][:10] for s in one_a_day["shots"]], ["2024-05-26", "2024-07-01", "2024-05-25"])
        self.assertTrue(all(s["score"] >= 0.9 for s in self.call("catalog_best_shots", {"min_score": 0.9})["shots"]))
        self.assertEqual(self.call("catalog_best_shots", {"query": "nothing-like-this"})["shots"], [])
        self.assertEqual(self.call("catalog_best_shots", {"min_score": 2})["error"]["code"], "invalid_arguments")

    def test_the_story_writer_sees_picture_quality(self):
        reply = self.call("catalog_ideas", {"prompt": "Waves", "asset_ids": [self.ids[0]], "writer": "agent"})["needs_writing"]
        scenes = json.loads(reply["user"])["evidence"][0]["scenes"]
        self.assertEqual(sorted(s["picture_quality"] for s in scenes), [0.5, 0.91, 0.95])
        self.assertIn("picture_quality", reply["system"])


if __name__ == "__main__":
    unittest.main()
