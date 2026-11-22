"""From an idea's edit sequence to an AutoCut edit plan and project, and reading a project back."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from frameatlas import autocut_handoff
from frameatlas.service import Service
from frameatlas.store import Store

TICKS = 254_016_000_000


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "data")
        self.service = Service(self.store)
        self.out = self.root / "out"
        self.out.mkdir()
        self.files = {}
        self.ids = {}
        for name, duration in (("CLIP_A.mp4", 20.0), ("VID_B_00_001.insv", 60.0), ("CLIP_C.mov", 5.0)):
            path = self.root / name
            path.write_bytes(b"made-up media")
            aid, _ = self.store.upsert({"provider": "local", "source_id": name, "filename": name, "kind": "video",
                                        "local_path": str(path), "duration": duration})
            self.files[name], self.ids[name] = str(path.resolve()), aid
        self.cloud, _ = self.store.upsert({"provider": "google_photos", "source_id": "cloud", "filename": "IMG_CLOUD.MOV", "kind": "video"})

    def tearDown(self):
        self.tmp.cleanup()

    def idea(self, sources, fmt="reel"):
        idea = self.service.dispatch("idea_pad_add", {"title": "Rooftop evening!", "format": fmt})
        with self.store.connect() as db:
            db.execute("UPDATE idea_pad SET basis=? WHERE id=?", (json.dumps({"sequence": {"sources": sources}}), idea["id"]))
        return idea["id"]

    def test_the_plan_cuts_each_shot_in_order(self):
        iid = self.idea([{"asset_id": self.ids["CLIP_A.mp4"], "start": 2.0, "end": 6.5},
                         {"asset_id": self.ids["VID_B_00_001.insv"], "start": 30.0, "end": 30.0, "sample_time": 30.0},
                         {"asset_id": self.ids["CLIP_C.mov"], "start": 4.6, "end": 4.8},
                         {"asset_id": self.cloud, "start": 1.0, "end": 2.0, "filename": "IMG_CLOUD.MOV"}])
        idea, edit, shots, missing = autocut_handoff.plan(self.store, iid)
        self.assertEqual(edit["format"], "autocut.catalogEditPlan")
        self.assertEqual((edit["sequence"]["width"], edit["sequence"]["height"]), (1080, 1920), "reels are vertical")
        self.assertEqual([e["source"]["path"] for e in edit["entries"]],
                         [self.files["CLIP_A.mp4"], self.files["VID_B_00_001.insv"], self.files["CLIP_C.mov"]])
        self.assertEqual((edit["entries"][0]["sourceIn"], edit["entries"][0]["duration"]), (2 * TICKS, int(4.5 * TICKS)))
        self.assertEqual((shots[1]["start"], shots[1]["end"]), (28.5, 31.5), "a 360 sample point becomes 3 seconds")
        self.assertEqual((shots[2]["start"], shots[2]["end"]), (4.0, 5.0), "a short shot grows to a second, inside the file")
        self.assertEqual([m["asset_id"] for m in missing], [self.cloud])
        self.assertEqual(autocut_handoff.plan(self.store, iid, aspect="16:9")[1]["sequence"]["width"], 1920)

    def test_a_retrieved_copy_or_a_given_path_fills_a_missing_shot(self):
        iid = self.idea([{"asset_id": self.cloud, "start": 1.0, "end": 3.0}], fmt="vlog")
        copy = self.root / "IMG_CLOUD.MOV"
        copy.write_bytes(b"made-up")
        given = autocut_handoff.plan(self.store, iid, overrides={self.cloud: str(copy)})
        self.assertEqual(given[3], [])
        self.assertEqual(given[1]["sequence"]["width"], 1920)
        job = self.store.enqueue(self.cloud)["id"]
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET task='retrieve', state='done', result=? WHERE id=?", (json.dumps({"path": str(copy)}), job))
        self.assertEqual(autocut_handoff.plan(self.store, iid)[2][0]["path"], str(copy.resolve()))

    def test_handoff_builds_the_project_records_the_footage_and_moves_the_idea(self):
        iid = self.idea([{"asset_id": self.ids["CLIP_A.mp4"], "start": 2.0, "end": 6.5},
                         {"asset_id": self.ids["VID_B_00_001.insv"], "start": 30.0, "end": 30.0}])
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"], seen["script"] = cmd, Path(cmd[4]).read_text().splitlines()
            Path(cmd[2]).write_text("{}")
            return subprocess.CompletedProcess(cmd, 0, "{}", "")
        with patch("subprocess.run", side_effect=fake_run):
            out = self.service.dispatch("idea_to_autocut", {"id": iid, "folder": str(self.out)})
        self.assertEqual(out["project"], str(self.out / "rooftop-evening.fcproj"))
        self.assertTrue(Path(out["plan"]).is_file())
        self.assertEqual(seen["cmd"][1:4], ["--project", out["project"], "run"])
        self.assertEqual(json.loads(seen["script"][0])["id"], "file.import", "360 originals are imported first for their proxies")
        self.assertEqual(json.loads(seen["script"][1])["id"], "catalog.importEditPlan")
        self.assertEqual(self.service.dispatch("search", {"used": "used"})["total"], 2)
        self.assertEqual(self.service.dispatch("idea_pad", {})["items"][0]["status"], "editing")
        with patch("subprocess.run", side_effect=fake_run):
            again = self.service.dispatch("idea_to_autocut", {"id": iid, "folder": str(self.out), "build": False})
        self.assertTrue(again["plan"].endswith("rooftop-evening-2.edit-plan.json"), "an earlier plan is never overwritten")
        self.assertNotIn("project", again)

    def test_failures_are_explained(self):
        with self.assertRaises(ValueError):
            self.service.dispatch("idea_to_autocut", {"id": 99, "folder": str(self.out)})
        empty = self.service.dispatch("idea_pad_add", {"title": "No sequence"})["id"]
        with self.assertRaises(ValueError):
            self.service.dispatch("idea_to_autocut", {"id": empty, "folder": str(self.out)})
        only_cloud = self.idea([{"asset_id": self.cloud, "start": 1.0, "end": 2.0}])
        with self.assertRaises(ValueError) as caught:
            self.service.dispatch("idea_to_autocut", {"id": only_cloud, "folder": str(self.out)})
        self.assertEqual(caught.exception.data["missing"][0]["asset_id"], self.cloud)
        with self.assertRaises(ValueError):
            self.service.dispatch("idea_to_autocut", {"id": only_cloud, "folder": "relative/folder"})
        iid = self.idea([{"asset_id": self.ids["CLIP_A.mp4"], "start": 2.0, "end": 6.5}])
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "boom")):
            with self.assertRaises(ValueError) as caught:
                self.service.dispatch("idea_to_autocut", {"id": iid, "folder": str(self.out)})
        self.assertIn("boom", str(caught.exception))
        self.assertIn("catalog assemble", caught.exception.hint)

    def test_reading_a_finished_project_back(self):
        project = self.out / "done.fcproj"
        project.write_text("{}")
        media = [{"item": 4, "path": self.files["CLIP_A.mp4"]}, {"item": 5, "path": self.files["CLIP_C.mov"]}]
        sequence = {"video": [{"items": [{"item": 4, "sourceIn": 0, "duration": TICKS}]}]}
        with patch("frameatlas.native.cli", side_effect=lambda *a, **k: media if "media.status" in a else sequence):
            got = self.service.dispatch("footage_import_project", {"project": str(project), "title": "Done"})
        self.assertEqual(got["items"], 1, "only media placed on the timeline counts as used")
        self.assertEqual(self.service.dispatch("search", {"used": "used"})["items"][0]["id"], self.ids["CLIP_A.mp4"])


if __name__ == "__main__":
    unittest.main()
