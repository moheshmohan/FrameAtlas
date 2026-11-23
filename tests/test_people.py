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


    def test_bulk_link_unlink_and_new_person(self):
        self.scan()
        people.regroup(self.store, self.native)
        a, b = [p["id"] for p in people.list_people(self.store)["items"]]
        a_faces = [f["id"] for f in people.person(self.store, a)["faces"]]
        # move two of a's faces to b; regrouping keeps them there (confirmed) and they never return to a
        out = self.service.dispatch("faces_link", {"faces": a_faces[:2], "person": b})
        self.assertEqual((out["person"], out["linked"]), (b, 2))
        people.regroup(self.store, self.native)
        b_faces = {f["id"] for f in people.person(self.store, b)["faces"]}
        self.assertTrue(set(a_faces[:2]) <= b_faces)
        # unlink them: they belong to no one, not to a again and not to b again
        out = self.service.dispatch("faces_unlink", {"faces": a_faces[:2]})
        self.assertEqual(out["unlinked"], 2)
        people.regroup(self.store, self.native)
        for pid in (a, b):
            self.assertFalse(set(a_faces[:2]) & {f["id"] for f in people.person(self.store, pid)["faces"]})
        unsorted = self.service.dispatch("faces_unsorted", {"limit": "50", "offset": "0"})
        self.assertTrue(set(a_faces[:2]) <= {f["id"] for f in unsorted["items"]})
        self.assertEqual(unsorted["total"], len(unsorted["items"]))
        # a new, named person from them; the name is searchable
        made = self.service.dispatch("faces_link", {"faces": a_faces[:2], "new_person": True, "name": " Chitra "})
        found = self.service.dispatch("people", {"query": "chit"})["items"]
        self.assertEqual([(p["id"], p["name"]) for p in found], [(made["person"], "Chitra")])
        self.assertEqual(self.service.dispatch("people", {"query": f"#{made['person']}"})["total"], 1)
        self.assertEqual(self.service.dispatch("people", {"query": "50%_"})["items"], [])
        # a cover face that moved away is replaced
        with self.store.connect() as db:
            cover = db.execute("SELECT cover_face FROM people WHERE id=?", (a,)).fetchone()[0]
            self.assertIn(cover, {f["id"] for f in people.person(self.store, a)["faces"]})

    def test_suggest_ranks_the_right_person_first(self):
        self.scan()
        people.regroup(self.store, self.native)
        a, b = [p["id"] for p in people.list_people(self.store)["items"]]
        people.update_person(self.store, b, name="Bina")
        # a new face of b's, found in an item nobody was seen in, belongs to no one yet
        face = {"bbox": [0, 0, 0.5, 0.5], "landmarks": [[0, 0]] * 5, "score": 0.95, "side": 200.0, "frontal": 1.0,
                "embedding": b64(self.native.people["b"]), "crop": ""}
        with self.store.connect() as db:
            people.record(self.store, db, self.ids[7], "original", [{"faces": [face]}], MODEL)
            new_face = db.execute("SELECT id FROM faces WHERE asset_id=?", (self.ids[7],)).fetchone()[0]
        got = self.service.dispatch("people_suggest", {"faces": str(new_face), "limit": "3"})["items"]
        self.assertEqual([(got[0]["id"], got[0]["name"]), (got[-1]["id"],)], [(b, "Bina"), (a,)])
        self.assertGreater(got[0]["likeness"], 0.9)
        self.assertLess(got[-1]["likeness"], 0.5)
        # a face is not suggested back to its own person, nor to a person it was taken away from
        a_face = people.person(self.store, a)["faces"][0]["id"]
        self.assertNotIn(a, [p["id"] for p in people.suggest(self.store, [a_face])["items"]])
        people.link_faces(self.store, [new_face], b)
        people.unlink_faces(self.store, [new_face])
        self.assertNotIn(b, [p["id"] for p in people.suggest(self.store, [new_face])["items"]])
        # with another face that was never taken from b, b is suggested again
        both = people.suggest(self.store, [new_face, a_face])["items"]
        self.assertIn(b, [p["id"] for p in both])

    def test_hostile_face_arguments(self):
        self.scan()
        people.regroup(self.store, self.native)
        a = people.list_people(self.store)["items"][0]["id"]
        bad_links = [{"faces": [], "person": a}, {"faces": list(range(1, 502)), "person": a}, {"faces": "1", "person": a},
                     {"faces": [1, "x"], "person": a}, {"faces": [1, -2], "person": a}, {"faces": [99999], "person": a},
                     {"faces": [1], "person": a, "new_person": True}, {"faces": [1]}, {"faces": [1], "person": 99999},
                     {"faces": [1], "new_person": "yes"}, {"faces": [1], "new_person": True, "name": "x" * 121},
                     {"faces": [1], "person": a, "colour": "red"}, {"faces": [True], "person": a}]
        for bad in bad_links:
            with self.assertRaises(ValueError, msg=bad):
                self.service.dispatch("faces_link", bad)
        for bad in ({"faces": []}, {"faces": None}, {"faces": [1], "person": a}, {"faces": [99999]}):
            with self.assertRaises(ValueError, msg=bad):
                self.service.dispatch("faces_unlink", bad)
        for bad in ({"limit": "0"}, {"limit": "x"}, {"offset": "-1"}, {"limit": 10, "kind": "x"}):
            with self.assertRaises(ValueError, msg=bad):
                self.service.dispatch("faces_unsorted", bad)
        for bad in ({"faces": ""}, {"faces": "1,x"}, {"faces": "99999"}, {"faces": "1", "limit": "0"}):
            with self.assertRaises(ValueError, msg=bad):
                self.service.dispatch("people_suggest", bad)
        with self.assertRaises(ValueError):
            self.service.dispatch("people", {"query": "x" * 121})
        # duplicates are one face, and nothing changed after the refusals
        self.assertEqual(self.service.dispatch("faces_link", {"faces": [1, 1, "1"], "person": a})["faces"], [1])


    def new_face(self, who, item=7):
        """A face of `who` found in an item at full resolution, belonging to no one yet."""
        crop = Path(self.tmp.name) / f"crop-{who}-{item}.jpg"
        crop.write_bytes(b"\xff\xd8 test jpeg")
        face = {"bbox": [0, 0, 0.5, 0.5], "landmarks": [[0, 0]] * 5, "score": 0.95, "side": 200.0, "frontal": 1.0,
                "embedding": b64(self.native.people[who]), "crop": str(crop)}
        with self.store.connect() as db:
            people.record(self.store, db, self.ids[item], "original", [{"faces": [face]}], MODEL)
            return db.execute("SELECT id FROM faces WHERE asset_id=? AND source='original'", (self.ids[item],)).fetchone()[0]

    def split(self, pid, count=3):
        """Split some of a person's faces off into another person, as grouping sometimes does."""
        with self.store.connect() as db:
            other = db.execute("INSERT INTO people(created,updated) VALUES(1,1)").lastrowid
            ids = [r[0] for r in db.execute("SELECT id FROM faces WHERE person_id=? ORDER BY id LIMIT ?", (pid, count))]
            db.execute(f"UPDATE faces SET person_id=? WHERE id IN ({','.join('?' * len(ids))})", (other, *ids))
        return other

    def test_auto_sort_merges_links_logs_and_undoes(self):
        self.scan()
        people.regroup(self.store, self.native)
        a, b = [p["id"] for p in people.list_people(self.store)["items"]]
        people.update_person(self.store, b, name="Bina")
        piece = self.split(a)
        face = self.new_face("b")
        # a person split three ways is one merge suggestion per pair, never the same pair twice
        piece2 = self.split(a, 1)
        out = people.auto_sort(self.store)
        pairs = [frozenset((e["person_id"], e["other_id"])) for e in self.service.dispatch("people_review", {"action": "merge"})["items"]]
        self.assertEqual(len(pairs), len(set(pairs)))
        people.merge(self.store, a, piece2)
        # without apply nothing changes; the merge and the link are proposed
        out = people.auto_sort(self.store)
        self.assertEqual((out["merged"], out["linked"]), (0, 0))
        proposed = self.service.dispatch("people_review", {})["items"]
        self.assertEqual({(e["action"], e["person_id"], e["other_id"] or e["face_id"]) for e in proposed},
                         {("merge", a, piece), ("link", b, face)})
        # applied: the split person is merged back and the face linked to Bina, unconfirmed
        out = people.auto_sort(self.store, apply=True)
        self.assertEqual((out["merged"], out["linked"]), (1, 1))
        self.assertEqual(len(people.person(self.store, a)["faces"]), 6)
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT person_id, confirmed FROM faces WHERE id=?", (face,)).fetchone()), (b, 0))
        applied = self.service.dispatch("people_review", {"status": "applied"})["items"]
        self.assertEqual(len(applied), 2)
        # undo both: the split person and the face are back as they were, and the link is not made again
        for e in applied:
            self.service.dispatch("people_review_decide", {"id": e["id"], "decision": "undo"})
        self.assertEqual(len(people.person(self.store, piece)["faces"]), 3)
        self.assertEqual(len(people.person(self.store, a)["faces"]), 3)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT person_id FROM faces WHERE id=?", (face,)).fetchone()[0])
        again = people.auto_sort(self.store, apply=True)
        self.assertEqual((again["merged"], again["linked"]), (0, 0))
        # an undone change cannot be undone twice, nor a proposal undone
        with self.assertRaises(ValueError):
            self.service.dispatch("people_review_decide", {"id": applied[0]["id"], "decision": "undo"})

    def test_proposals_can_be_applied_or_dismissed_and_confirmed_faces_are_left_alone(self):
        self.scan()
        people.regroup(self.store, self.native)
        a, b = [p["id"] for p in people.list_people(self.store)["items"]]
        people.update_person(self.store, a, name="Aarav")
        people.update_person(self.store, b, name="Bina")
        piece = self.split(a)
        people.update_person(self.store, piece, name="Someone else")
        # two names that differ are never merged on their own, only proposed
        out = people.auto_sort(self.store, apply=True)
        self.assertEqual(out["merged"], 0)
        merge = [e for e in self.service.dispatch("people_review", {"action": "merge"})["items"]]
        self.assertEqual(len(merge), 1)
        self.assertEqual(merge[0]["detail"]["why"], "names differ")
        self.service.dispatch("people_review_decide", {"id": merge[0]["id"], "decision": "dismiss"})
        people.auto_sort(self.store, apply=True)
        self.assertEqual(self.service.dispatch("people_review", {"action": "merge"})["items"], [])
        # a confirmed face is never moved, even when it looks like someone else
        face = people.person(self.store, b)["faces"][0]["id"]
        people.link_faces(self.store, [face], a)
        before = people.auto_sort(self.store, apply=True)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT person_id FROM faces WHERE id=?", (face,)).fetchone()[0], a)
        self.assertFalse([e for e in self.service.dispatch("people_review", {})["items"] if e["face_id"] == face])
        # apply a proposal by hand
        piece2 = self.split(b, 2)
        people.update_person(self.store, piece2, name="Bina")
        prop = [e for e in self.service.dispatch("people_review", {"action": "merge"})["items"]]
        people.auto_sort(self.store)
        prop = [e for e in self.service.dispatch("people_review", {"action": "merge"})["items"] if e["other_id"] in (piece2, b)]
        self.assertTrue(prop)
        self.service.dispatch("people_review_decide", {"id": prop[0]["id"], "decision": "apply"})
        self.assertEqual(len([p for p in people.list_people(self.store, min_faces=1)["items"] if p["name"] == "Bina"]), 1)
        for bad in ({"id": 0, "decision": "apply"}, {"id": prop[0]["id"], "decision": "apply"}, {"id": 1, "decision": "maybe"},
                    {"id": 99999, "decision": "dismiss"}, {"id": 1, "decision": "apply", "x": 1}):
            with self.assertRaises(ValueError, msg=bad):
                self.service.dispatch("people_review_decide", bad)
        for bad in ({"status": "odd"}, {"actor": "someone"}, {"limit": "0"}):
            with self.assertRaises(ValueError, msg=bad):
                self.service.dispatch("people_review", bad)
        with self.assertRaises(ValueError):
            people.auto_sort(self.store, merge_at=2)
        with self.assertRaises(ValueError):
            people.auto_sort(self.store, colour=0.5)
        self.assertTrue(before)

    def test_names_from_google_photos_match_by_local_time(self):
        self.scan()
        people.regroup(self.store, self.native)
        a, b = [p["id"] for p in people.list_people(self.store)["items"]]
        with self.store.connect() as db:
            for i, aid in self.ids.items():
                db.execute("UPDATE assets SET captured=? WHERE id=?", (f"2024-05-20T10:0{i}:1{i}Z", aid))
        # Google shows local times (here UTC+5:30, and one item in UTC+4); a video never matches a photo
        local = lambda i, h=15, m=30: {"kind": "photo", "local": f"2024-05-20T{h}:{m + i:02d}:1{i}"}
        records = [{"name": "Aarav", "items": [local(i) for i in range(6)] + [{"kind": "video", "local": "2024-05-20T15:37:17"}]},
                   {"name": "Bina", "items": [local(1), local(3), local(5, 14, 0), local(6)]},
                   {"name": "Nobody here", "items": [{"kind": "photo", "local": "1999-01-01T00:00:00"}, "junk", {"local": "x"}]}]
        with self.store.connect() as db:
            matched = people.match_items(db, records[1]["items"])
        self.assertEqual(matched, [self.ids[1], self.ids[3], self.ids[5], self.ids[6]])
        people.update_person(self.store, b, name="Bina")
        out = self.service.dispatch("people_import_names", {"source": "google_photos", "people": records, "apply": True})
        self.assertEqual((out["names"], out["named"], out["unmatched_names"]), (3, 1, ["Nobody here"]))
        names = {p["id"]: p["name"] for p in people.list_people(self.store)["items"]}
        self.assertEqual(names, {a: "Aarav", b: "Bina"})
        # an owner's different name is kept; the import only proposes
        people.update_person(self.store, a, name="Aaru")
        out = people.import_names(self.store, "google_photos", records, apply=True)
        self.assertEqual(out["named"], 0)
        prop = self.service.dispatch("people_review", {"actor": "google"})["items"]
        self.assertEqual([(e["action"], e["person_id"], e["detail"]["name"]) for e in prop], [("name", a, "Aarav")])
        self.assertEqual(people.list_people(self.store)["items"][0]["name"], "Aaru")
        # a split group seen in most of a name's items is merged back when the faces agree
        people.update_person(self.store, a, name="Aarav")
        piece = self.split(a)
        out = people.import_names(self.store, "google_photos", records, apply=True)
        self.assertEqual(out["merged"], 1)
        with self.assertRaises(ValueError):
            people.person(self.store, piece)
        for bad in ({"source": "x y", "people": records}, {"people": []}, {"people": [{"items": []}]},
                    {"people": records, "apply": "yes"}, {"people": [{"name": "x", "items": "y"}]}):
            with self.assertRaises(ValueError, msg=bad):
                self.service.dispatch("people_import_names", bad)

    def test_items_at_one_second_all_count_and_a_second_or_two_off_still_matches(self):
        self.scan()
        with self.store.connect() as db:
            for i, aid in self.ids.items():
                db.execute("UPDATE assets SET captured=? WHERE id=?", (f"2024-05-20T10:0{i}:1{i}Z", aid))
            db.execute("UPDATE assets SET captured=? WHERE id=?", ("2024-05-20T10:05:15Z", self.ids[6]))  # a burst with 5
            items = [{"kind": "photo", "local": "2024-05-20T14:05:15"}, {"kind": "photo", "local": "2024-05-20T14:01:12"},
                     {"kind": "photo", "local": "2024-05-20T14:02:15"}, {"kind": "photo", "local": "2024-05-20T14:03:13"},
                     {"kind": "photo", "local": "2024-05-20T14:00:10"}]
            sets = people.match_sets(db, items)
        # the burst gives both; 14:01:12 is a second off item 1; 14:02:15 is three seconds off: no match
        self.assertEqual([sorted(s) for s in sets[:3]], [sorted([self.ids[5], self.ids[6]]), [self.ids[1]], []])
        self.assertEqual(people.unpack_items("Ps8bzlr junk Vs5vdmb"),
                         [{"kind": "photo", "local": "2024-02-04T12:20:15"}, {"kind": "video", "local": "2023-12-18T15:56:35"}])
        with self.assertRaises(ValueError):
            people.import_names(self.store, "google_photos")  # nothing read yet
        out = people.import_names(self.store, "google_photos", [{"name": "Burst", "packed": "Psdsf4r"}], min_count=1)
        self.assertEqual((out["names"], out["matched"]), (1, 2))

    def test_names_go_to_people_seen_mostly_in_that_name_and_mixed_results_are_only_proposed(self):
        self.scan()
        people.regroup(self.store, self.native)
        a, b = [p["id"] for p in people.list_people(self.store)["items"]]
        with self.store.connect() as db:
            for i, aid in self.ids.items():
                db.execute("UPDATE assets SET captured=? WHERE id=?", (f"2024-05-20T10:0{i}:1{i}Z", aid))
        items = lambda *its: [{"kind": "photo", "local": f"2024-05-20T10:0{i}:1{i}"} for i in its]
        names = lambda: {p["id"]: p["name"] for p in people.list_people(self.store)["items"]}
        # a is in most items of the library, so being in half of a guest's items says little
        guest = [{"name": "Guest", "items": items(0, 2, 6, 7)}]
        out = people.import_names(self.store, "google_photos", guest, apply=True, min_count=1)
        self.assertEqual((out["named"], out["proposed"]), (0, 0))
        out = people.import_names(self.store, "google_photos", guest, apply=True, min_count=1, min_lift=0.5)
        self.assertEqual(names()[a], "Guest")
        people.update_person(self.store, a, name="")
        # a and b are in all of Kay's items: mixed, so only proposed
        out = people.import_names(self.store, "google_photos", [{"name": "Kay", "items": items(1, 3, 5)}], apply=True)
        self.assertEqual((out["named"], out["mixed"]), (0, ["Kay"]))
        prop = self.service.dispatch("people_review", {"actor": "google"})["items"]
        self.assertEqual(len(prop), 1)
        self.assertTrue(prop[0]["detail"]["why"].startswith("mixed"))
        # the owner can give a mixed name to either candidate, and undo it
        first, second = prop[0]["person_id"], prop[0]["other_id"]
        self.assertEqual({first, second}, {a, b})
        self.service.dispatch("people_review_decide", {"id": prop[0]["id"], "decision": "apply_other"})
        self.assertEqual((names()[second], names()[first]), ("Kay", ""))
        people.decide(self.store, prop[0]["id"], "undo")
        self.assertEqual(names()[second], "")
        with self.assertRaises(ValueError):
            people.decide(self.store, prop[0]["id"], "apply_other")  # no longer proposed
        # reads are kept: a later import weighs every name read so far
        out = people.import_names(self.store, "google_photos")
        # the owner undid Kay on the second candidate, so only the first is left for Kay: no longer mixed
        self.assertEqual((out["names"], out["mixed"]), (2, []))
        prop = self.service.dispatch("people_review", {"actor": "google"})["items"]
        self.assertEqual([(e["person_id"], e["detail"]["name"]) for e in prop], [(first, "Kay")])
        # X's pictures show a alone, so X is a; Y is a in all of its pictures too, so Y does not fall
        # back to b, whom it is seen with less: mixed
        records = [{"name": "X", "items": items(0, 2, 4)}, {"name": "Y", "items": items(0, 1, 2, 3, 4)}]
        out = people.import_names(self.store, "google_photos", records, apply=True, replace=True, min_count=1, min_lift=0.5)
        self.assertEqual((out["named"], out["mixed"]), (1, ["Y"]))
        self.assertEqual((names()[a], names()[b]), ("X", ""))
        prop = self.service.dispatch("people_review", {"actor": "google"})["items"]
        self.assertEqual([(e["person_id"], e["other_id"], e["detail"]["why"]) for e in prop], [(b, a, f"mixed: seen more with #{a}")])
        # an undone name stays undone when the names are weighed again
        applied = people.review(self.store, status="applied", actor="google")["items"]
        people.decide(self.store, next(e["id"] for e in applied if e["detail"].get("name") == "X"), "undo")
        people.import_names(self.store, "google_photos", apply=True, min_count=1, min_lift=0.5)
        self.assertNotEqual(names()[a], "X")  # (with X ruled out, Y, seen with a in every picture, may take a)
        people.update_person(self.store, a, name="")
        # a person already called a name keeps it, even when another name is seen with them more
        people.update_person(self.store, b, name="Bina")
        records = [{"name": "Bina", "items": items(0, 1, 2, 3, 5, 6)}, {"name": "Other", "items": items(1, 3, 6)}]
        people.import_names(self.store, "google_photos", records, apply=True)
        self.assertEqual(names()[b], "Bina")
        prop = self.service.dispatch("people_review", {"actor": "google"})["items"]
        self.assertNotIn(b, [e["person_id"] for e in prop])

    def test_agents_can_look_at_faces(self):
        from frameatlas import mcp
        self.scan()
        people.regroup(self.store, self.native)
        a, b = [p["id"] for p in people.list_people(self.store)["items"]]
        reply = mcp.handle(self.service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": "people_compare", "arguments": {"person": a, "other": b, "count": 2}}})
        content = reply["result"]["content"]
        self.assertFalse(reply["result"]["isError"])
        images = [c for c in content if c["type"] == "image"]
        self.assertEqual(len(images), 4)
        self.assertEqual(base64.b64decode(images[0]["data"]), b"\xff\xd8 test jpeg")
        data = json.loads(content[0]["text"])
        self.assertLess(data["likeness"], 0.5)
        # crops are only read from the faces folder
        self.assertEqual(mcp.face_images(self.service, {"images": [{"crop": "../catalog.sqlite3"}, {"crop": "1.jpg/../x"}]}), [])
        with self.assertRaises(ValueError):
            self.service.dispatch("people_compare", {"person": a})
        listed = mcp.handle(self.service, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertTrue({"people_autosort", "people_review", "people_review_decide", "people_compare", "people_import_names"}
                        <= {t["name"] for t in listed})


if __name__ == "__main__":
    unittest.main()
