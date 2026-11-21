import base64
import json
import math
from pathlib import Path
import random
import struct
import tempfile
import unittest
from datetime import date

from frameatlas import idea_pad, people
from frameatlas.service import Service
from frameatlas.store import Store

MODEL = "glintr100.onnx=test"


def unit(vec):
    n = math.sqrt(sum(v * v for v in vec))
    return [v / n for v in vec]


def direction(seed):
    r = random.Random(seed)
    return unit([r.uniform(-1, 1) for _ in range(512)])


def b64(vec):
    return base64.b64encode(struct.pack("<512f", *vec)).decode()


class FakeNative:
    """FilmCraft's face commands, with a picture's people read from its file name
    (`a-b.jpg` shows people a and b) and a simple grouping by direction."""

    def __init__(self):
        self.calls = []
        self.people = {name: direction(i) for i, name in enumerate("abcdefgh")}

    def run(self, command, params=None, timeout=0):
        self.calls.append(command)
        if command == "faces.status":
            return {"installed": True, "fingerprints": {"id": MODEL}}
        if command == "media.faces":
            out = []
            for i, p in enumerate(params["paths"]):
                faces = []
                who = Path(p).stem.split("-")[0].split("_")
                for j, name in enumerate(w for w in who if w in self.people):
                    r = random.Random(f"{p}{j}")
                    vec = unit([v + r.uniform(-0.02, 0.02) for v in self.people[name]])
                    crop = Path(params["cropsDir"]) / f"face-{i}-{j}.jpg"
                    crop.write_bytes(b"\xff\xd8 test jpeg")
                    faces.append({"bbox": [0.1 * j, 0.1, 0.2, 0.2], "landmarks": [[0.1, 0.1]] * 5, "score": 0.9,
                                  "side": 60.0, "frontal": 0.8, "embedding": b64(vec), "crop": str(crop)})
                out.append({"path": p, "faces": faces})
            return {"pictures": out, "model": MODEL}
        if command == "faces.group":
            raw = Path(params["path"]).read_bytes()
            vecs = [struct.unpack("<512f", raw[i:i + 2048]) for i in range(0, len(raw), 2048)]
            labels, centres = [], []
            for v in vecs:
                for k, c in enumerate(centres):
                    if sum(a * b for a, b in zip(v, c)) > params["threshold"]:
                        labels.append(k)
                        break
                else:
                    centres.append(v)
                    labels.append(len(centres) - 1)
            return {"labels": labels, "groups": len(centres), "seconds": 0.01}
        raise AssertionError(command)


class PeopleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.native = FakeNative()
        self.service = Service(self.store)
        thumbs = self.store.root / "thumbnails"
        thumbs.mkdir()
        self.ids = {}
        # a appears in 6 items, b in 4, c once (a passer-by)
        for i, who in enumerate(["a", "a_b", "a", "a_b", "a_c", "a_b", "b", ""]):
            aid, _ = self.store.upsert({"provider": "google_photos", "source_id": f"s{i}", "filename": f"IMG_{i:04d}.HEIC",
                                        "kind": "photo", "captured": f"2024-05-{20 + i % 3:02d}T10:00:00Z"})
            name = f"{who or 'nobody'}-{i}.jpg"
            (thumbs / name).write_bytes(b"jpeg")
            with self.store.connect() as db:
                db.execute("UPDATE assets SET thumbnail=?, status='ready', summary=? WHERE id=?", (name, f"A beach {i}", aid))
            self.ids[i] = aid

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        return people.scan_previews(self.store, self.native, batch=3)

    def test_scan_stores_faces_and_crops_and_is_resumable(self):
        out = self.scan()
        self.assertEqual((out["scanned"], out["faces"]), (8, 11))
        self.assertEqual(self.native.calls.count("media.faces"), 3)
        with self.store.connect() as db:
            crops = [r[0] for r in db.execute("SELECT crop FROM faces")]
        self.assertTrue(all((self.store.root / "faces" / c).is_file() for c in crops))
        again = self.scan()
        self.assertEqual(again["scanned"], 0)
        st = people.progress(self.store)
        self.assertEqual((st["scanned"], st["faces"]), (8, 11))

    def test_grouping_names_survive_regrouping_and_corrections_stick(self):
        self.scan()
        out = people.regroup(self.store, self.native)
        self.assertEqual((out["faces"], out["new_people"]), (11, 2))
        listed = people.list_people(self.store)["items"]
        self.assertEqual([p["assets"] for p in listed], [6, 4])
        a, b = listed[0]["id"], listed[1]["id"]
        self.assertTrue(listed[0]["cover"].endswith(".jpg"))
        people.update_person(self.store, a, name="Aarav")
        people.regroup(self.store, self.native)
        names = {p["name"]: p["id"] for p in people.list_people(self.store)["items"]}
        self.assertEqual(names.get("Aarav"), a)
        # take one face away from Aarav: it never goes back on regrouping
        face = people.person(self.store, a)["faces"][0]["id"]
        people.assign_face(self.store, face)
        people.regroup(self.store, self.native)
        self.assertNotIn(face, [f["id"] for f in people.person(self.store, a)["faces"]])
        # confirm it to b instead: it stays there
        people.assign_face(self.store, face, b)
        people.regroup(self.store, self.native)
        self.assertIn(face, [f["id"] for f in people.person(self.store, b)["faces"]])
        # search by person, and by both together
        hits = self.store.search(person=str(a), limit=50)["items"]
        self.assertEqual(len(hits), 5)
        both = self.store.search(person=f"{a},{b}", limit=50)["items"]
        moved_from = people.asset_people(self.store, people.person(self.store, b)["faces"][0]["asset_id"])
        with self.store.connect() as db:
            moved_asset = db.execute("SELECT asset_id FROM faces WHERE id=?", (face,)).fetchone()[0]
        self.assertEqual({h["id"] for h in both}, {self.ids[1], self.ids[3], self.ids[5]} - {moved_asset})
        self.assertTrue(moved_from)
        for bad in ("x", "1,2,3,4,5,6,7,8,9", "-1", "1;DROP"):
            with self.assertRaises(ValueError):
                self.store.search(person=bad)

    def test_merge_hide_and_forget(self):
        self.scan()
        people.regroup(self.store, self.native)
        a, b = [p["id"] for p in people.list_people(self.store)["items"]]
        people.update_person(self.store, b, name="Bina")
        moved = people.merge(self.store, a, b)
        self.assertEqual(moved["moved_faces"], 4)
        listed = people.list_people(self.store)["items"]
        self.assertEqual((len(listed), listed[0]["name"]), (1, "Bina"))
        people.update_person(self.store, a, hidden=True)
        self.assertEqual(people.list_people(self.store)["items"], [])
        self.assertEqual(len(people.list_people(self.store, include_hidden=True)["items"]), 1)
        for bad in ((a, a), (a, 999), ("x", b)):
            with self.assertRaises(ValueError):
                people.merge(self.store, *bad)
        out = self.service.dispatch("people_forget", {"confirm": True})
        self.assertEqual(out["forgotten_faces"], 11)
        self.assertFalse((self.store.root / "faces").exists())
        with self.assertRaises(ValueError):
            self.service.dispatch("people_forget", {})

    def test_full_resolution_faces_replace_the_preview_and_hostile_faces_are_dropped(self):
        self.scan()
        aid = self.ids[1]
        good = {"bbox": [0, 0, 0.5, 0.5], "landmarks": [[0, 0]] * 5, "score": 0.95, "side": 400.0, "frontal": 1.0,
                "embedding": b64(direction(0))}
        bad = [{**good, "embedding": "not base64!"}, {**good, "embedding": b64([0.0] * 512)},
               {**good, "embedding": base64.b64encode(b"\0" * 100).decode()}, {**good, "score": float("nan")},
               {**good, "bbox": [0, 0, 1]}, {**good, "embedding": b64([float("inf")] + [0.0] * 511)}, "junk"]
        with self.store.connect() as db:
            kept = people.record(self.store, db, aid, "original", [{"faces": [good] + bad}], MODEL)
        self.assertEqual(kept, 1)
        sources = [f["source"] for f in people.asset_people(self.store, aid)]
        self.assertEqual(sources, ["original"])
        with self.store.connect() as db:
            with self.assertRaises(ValueError):
                people.record(self.store, db, aid, "elsewhere", [], MODEL)
        detail = self.service.dispatch("asset", {"id": aid})
        self.assertEqual(len(detail["people"]), 1)

    def test_idea_pad_moments_carry_named_people(self):
        self.scan()
        people.regroup(self.store, self.native)
        a = people.list_people(self.store)["items"][0]["id"]
        people.update_person(self.store, a, name="Aarav")
        found = idea_pad.moments(self.store, today=date(2026, 1, 1))["moments"]
        self.assertTrue(found and found[0]["people"][0].startswith("Aarav ×"), found)

    def test_service_and_http_shaped_arguments(self):
        self.scan()
        people.regroup(self.store, self.native)
        listed = self.service.dispatch("people", {"limit": "10", "include_hidden": "false"})
        pid = listed["items"][0]["id"]
        self.assertEqual(self.service.dispatch("person", {"id": str(pid), "limit": "5"})["id"], pid)
        for bad in ({"id": pid, "name": "x" * 121}, {"id": pid, "hidden": "yes"}, {"id": pid, "colour": "red"}, {"id": 0, "name": "x"}):
            with self.assertRaises(ValueError):
                self.service.dispatch("person_update", bad)
        with self.assertRaises(ValueError):
            self.service.dispatch("face_assign", {"face": 1, "new_person": "yes"})
        made = self.service.dispatch("face_assign", {"face": 1, "new_person": True})
        self.assertIsNotNone(made["person"])
        self.assertEqual(self.service.dispatch("people_status", {})["running"], False)


if __name__ == "__main__":
    unittest.main()
