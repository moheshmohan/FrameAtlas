"""Footage memory: which items a finished video used, unused-footage search, and published ideas."""
import json
from pathlib import Path
import tempfile
import unittest

from frameatlas.mcp import handle
from frameatlas.service import Service
from frameatlas.store import Store


class FootageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)
        self.local = Path(self.tmp.name) / "VID_0001.mp4"
        self.local.write_bytes(b"made-up")
        self.a, _ = self.store.upsert({"provider": "local", "source_id": "f1", "filename": "VID_0001.mp4", "kind": "video",
                                       "local_path": str(self.local), "captured": "2024-06-01T10:00:00Z"})
        self.b, _ = self.store.upsert({"provider": "google_photos", "source_id": "f2", "filename": "IMG_0002.HEIC", "kind": "photo",
                                       "captured": "2024-06-01T11:00:00Z"})
        self.c, _ = self.store.upsert({"provider": "google_photos", "source_id": "f3", "filename": "IMG_0003.HEIC", "kind": "photo",
                                       "captured": "2024-06-02T09:00:00Z"})
        self.idea = self.service.dispatch("idea_pad_add", {"title": "A day at the lake"})["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name, arguments=None):
        result = handle(self.service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}})["result"]
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def test_record_by_id_path_and_filename(self):
        got = self.call("footage_record", {"title": "Lake day", "idea_id": self.idea, "project": "/projects/lake.fcproj", "shots": [
            {"asset_id": self.a, "start": 1.5, "end": 4.0}, {"path": "/elsewhere/IMG_0002.HEIC"},
            {"filename": "IMG_9999.HEIC"}],
            "published": [{"where": "youtube", "url": "https://www.youtube.com/watch?v=made-up", "date": "2024-06-10"}]})
        self.assertEqual((got["items"], got["not_found"]), (2, ["IMG_9999.HEIC"]))
        unused = self.call("catalog_search", {"used": "unused"})
        self.assertEqual([i["id"] for i in unused["items"]], [self.c])
        self.assertEqual(self.call("catalog_search", {"used": "used"})["total"], 2)
        detail = self.call("catalog_asset", {"id": self.a})
        self.assertEqual(detail["used_in"][0]["title"], "Lake day")
        self.assertEqual((detail["used_in"][0]["start"], detail["used_in"][0]["end"]), (1.5, 4.0))
        idea = self.service.dispatch("idea_pad", {})["items"][0]
        self.assertEqual(idea["status"], "published")
        self.assertEqual(idea["basis"]["published"][0]["url"], "https://www.youtube.com/watch?v=made-up")

    def test_recording_a_project_again_replaces_its_shots(self):
        args = {"title": "Lake day", "project": "/projects/lake.fcproj", "shots": [{"asset_id": self.a}, {"asset_id": self.b}]}
        first = self.call("footage_record", args)
        again = self.call("footage_record", {**args, "title": "Lake day (final)", "shots": [{"asset_id": self.c}]})
        self.assertEqual(first["video"], again["video"])
        videos = self.call("footage_videos")
        self.assertEqual((videos["total"], videos["items"][0]["title"], videos["items"][0]["items"]), (1, "Lake day (final)", 1))
        self.assertEqual(self.call("catalog_search", {"used": "used"})["items"][0]["id"], self.c)

    def test_bad_input_and_forget(self):
        self.assertEqual(self.call("footage_record", {"title": "x", "shots": []})["error"]["code"], "invalid_arguments")
        bad_url = self.call("footage_record", {"title": "x", "shots": [{"asset_id": self.a}],
                                               "published": [{"where": "youtube", "url": "javascript:alert(1)"}]})
        self.assertEqual(bad_url["error"]["code"], "invalid_arguments")
        self.assertEqual(self.call("footage_record", {"title": "x", "idea_id": 999, "shots": [{"asset_id": self.a}]})["error"]["code"], "not_found")
        video = self.call("footage_record", {"title": "x", "shots": [{"asset_id": self.a}]})["video"]
        token = self.call("footage_forget", {"id": video})["error"]["data"]["confirmation"]
        self.assertEqual(self.call("footage_forget", {"id": video, "confirmation": token}), {"forgotten": video})
        self.assertEqual(self.call("catalog_search", {"used": "used"})["total"], 0)
        with self.assertRaises(ValueError):
            self.store.search(used="sometimes")


if __name__ == "__main__":
    unittest.main()
