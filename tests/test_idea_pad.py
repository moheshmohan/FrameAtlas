import contextlib
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from frameatlas import idea_pad, metadata
from frameatlas.mcp import TOOLS, handle
from frameatlas.service import Service
from frameatlas.store import Store


def analysed(store, aid, summary, tags=(), is_360=False):
    with store.connect() as db:
        db.execute("UPDATE assets SET status='ready',summary=?,tags=?,analysis=? WHERE id=?",
                   (summary, json.dumps(list(tags)), json.dumps({"is_360": is_360}), aid))


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_device_hints_are_labelled_guesses_from_filename_patterns(self):
        cases = {"IMG_1234.HEIC": "iPhone", "IMG_20240501_101010.jpg": "Android phone", "PXL_20240701_1.mp4": "Google Pixel phone",
                 "IMG-20240501-WA0003.jpg": "WhatsApp (sent or received)", "VID_20250423_110907_00_043.insv": "Insta360 360° camera",
                 "DSC_0042.JPG": "Nikon camera", "DSC01234.JPG": "Sony camera", "Copy of DJI_0001.MP4": "DJI drone or camera",
                 "IMG_1234-COLLAGE.jpg": "Google Photos creation", "Screenshot 2024-01-01.png": "Screenshot"}
        for name, label in cases.items():
            hint = metadata.device_hint(name)
            self.assertEqual(hint["label"], label, name)
            self.assertEqual(hint["source"], "filename pattern")
        for name in ("holiday.jpg", "", None, 42, "x" * 5000):
            self.assertIsNone(metadata.device_hint(name))

    def test_picker_camera_fields_are_bounded_and_recorded_cameras_win_over_hints(self):
        self.assertEqual(metadata.picker_camera({"make": " Apple ", "model": "iPhone 15 Pro", "width": 4032, "iso": -5, "fps": "x"}),
                         {"camera": {"make": "Apple", "model": "iPhone 15 Pro", "width": 4032, "source": "Google Picker camera fields"}})
        for hostile in (None, [], "Apple", {"make": 7}, {"width": float("inf")}):
            self.assertEqual(metadata.picker_camera(hostile), {})
        self.assertEqual(metadata.camera({"camera": {"make": "Apple", "model": "Apple iPhone 15"}})["label"], "Apple iPhone 15")
        self.assertEqual(metadata.camera({"camera": "Insta360 X5"})["label"], "Insta360 X5")
        self.assertIsNone(metadata.camera({"camera": {"width": 10}}))
        still = metadata.facts({"kind": "photo", "analysis": {"probe": {"video": {"width": 4032, "frame_rate": {"num": 30000, "den": 1001}}}}})
        self.assertEqual(still["picture"], {"width": 4032})
        facts = metadata.facts({"kind": "video", "filename": "IMG_1.HEIC", "captured": "2024-07-01T18:30:00Z",
                                "metadata": {"camera": {"make": "Apple", "model": "iPhone 15"}, "gps_status": "not_provided_by_picker"},
                                "analysis": {"probe": {"video": {"width": 1920, "height": 1080, "frame_rate": {"num": 30000, "den": 1001}}}}, "position": None})
        self.assertEqual(facts["camera"]["label"], "Apple iPhone 15")
        self.assertIsNone(facts["device"])
        self.assertEqual(facts["picture"], {"width": 1920, "height": 1080, "fps": 29.97})
        self.assertNotIn("fps", metadata.facts({"analysis": {"probe": {"video": {"frame_rate": {"num": 30, "den": 0}}}}})["picture"] or {})
        self.assertIn("Picker", facts["place_status"])
        self.assertEqual(facts["when"]["date"][:4], "2024")
        guessed = metadata.facts({"filename": "IMG_0001.HEIC", "captured": "2024-07-01 18:30", "metadata": {}, "analysis": {}})
        self.assertEqual(guessed["device"]["label"], "iPhone")
        self.assertEqual(guessed["when"], {"date": "2024-07-01", "weekday": "Monday", "time": "18:30", "basis": "camera clock (zone unknown)"})
        self.assertIsNone(metadata.when("1970-01-01T00:00:00Z"))
        self.assertIsNone(metadata.when("not a date"))

    def test_picker_import_keeps_google_camera_fields(self):
        session = "00000000-0000-4000-8000-000000000001"
        listed = {"ready": True, "items": [{"id": "p1", "filename": "IMG_1.HEIC", "kind": "photo", "created": "2024-07-01T18:30:00Z",
                                            "mimeType": "image/heic", "capture": {"make": "Apple", "model": "iPhone 15 Pro", "iso": 50}},
                                           {"id": "p2", "filename": "v.mp4", "kind": "video", "created": "2024-07-01T18:31:00Z",
                                            "mimeType": "video/mp4", "capture": "hostile"}]}
        with patch("frameatlas.service.broker", return_value=listed):
            Service(self.store).dispatch("picker_import", {"session": session})
        rows = {a["filename"]: a for a in self.store.search(limit=10)["items"]}
        self.assertEqual(rows["IMG_1.HEIC"]["metadata"]["camera"],
                         {"make": "Apple", "model": "iPhone 15 Pro", "iso": 50, "source": "Google Picker camera fields"})
        self.assertNotIn("camera", rows["v.mp4"]["metadata"])
        coverage = Service(self.store).dispatch("metadata_coverage", {})
        self.assertEqual(coverage["providers"]["google_photos"]["camera"], 1)
        self.assertEqual(coverage["total"]["location"], 0)
        self.assertTrue(any("Takeout" in note for note in coverage["notes"]))
        facts = Service(self.store).dispatch("asset", {"id": rows["IMG_1.HEIC"]["id"]})["facts"]
        self.assertEqual(facts["camera"]["label"], "Apple iPhone 15 Pro")

    def test_backfill_reads_only_existing_unchecked_local_videos(self):
        here = Path(self.tmp.name) / "clip.mov"
        here.write_bytes(b"x")
        a, _ = self.store.upsert({"provider": "local", "source_id": "a", "filename": "clip.mov", "kind": "video", "local_path": str(here)})
        self.store.upsert({"provider": "local", "source_id": "b", "filename": "gone.mov", "kind": "video", "local_path": str(here) + ".missing"})
        calls = []
        def reader(path):
            calls.append(path)
            return "recorded", {"latitude": 25.2, "longitude": 55.3}
        self.assertEqual(metadata.backfill(self.store, read_video_gps=reader), {"checked": 1, "recorded": 1, "candidates": 2})
        self.assertEqual(calls, [here])
        self.assertEqual(self.store.get(a, include_scenes=False)["position"]["source"], "Embedded video GPS")
        self.assertEqual(metadata.backfill(self.store, read_video_gps=reader)["checked"], 0)


class IdeaPadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def add_day(self, day, n, prefix, kind="photo", summary="", is_360=False, filename="IMG_{i:04d}.HEIC"):
        ids = []
        for i in range(n):
            aid, _ = self.store.upsert({"provider": "google_photos", "source_id": f"{prefix}{i}", "kind": kind,
                                        "filename": filename.format(i=i), "captured": f"{day}T10:{i % 60:02d}:00"})
            if summary:
                analysed(self.store, aid, f"{summary} {i}", ["beach", "wide"], is_360)
            ids.append(aid)
        return ids

    def test_manual_ideas_are_validated_edited_listed_and_deleted(self):
        aid, _ = self.store.upsert({"provider": "local", "source_id": "x", "filename": "x.mov", "kind": "video"})
        idea = self.service.dispatch("idea_pad_add", {"title": " Robot dog at the mall ", "tags": ["robots", "robots", " "],
                                                       "format": "short", "asset_ids": [aid]})
        self.assertEqual((idea["title"], idea["origin"], idea["status"], idea["tags"]), ("Robot dog at the mall", "manual", "idea", ["robots"]))
        for bad in ({"title": ""}, {"title": "x" * 201}, {"title": "a", "status": "done"}, {"title": "a", "format": "film"},
                    {"title": "a", "tags": "x"}, {"title": "a", "asset_ids": ["f" * 24]}, {"title": "a", "asset_ids": ["../x"]}):
            with self.assertRaises(ValueError):
                self.service.dispatch("idea_pad_add", bad)
        changed = self.service.dispatch("idea_pad_update", {"id": idea["id"], "status": "planned", "notes": "Shoot on Friday"})
        self.assertEqual((changed["status"], changed["notes"]), ("planned", "Shoot on Friday"))
        for bad in ({"id": idea["id"]}, {"id": idea["id"], "origin": "generated"}, {"id": "1", "status": "idea"}, {"id": 999, "status": "idea"}):
            with self.assertRaises(ValueError):
                self.service.dispatch("idea_pad_update", bad)
        listed = self.service.dispatch("idea_pad", {"status": "planned"})
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"][0]["linked"][0]["id"], aid)
        self.assertEqual(self.service.dispatch("idea_pad", {})["counts"], {"planned": 1})
        self.service.dispatch("idea_pad_delete", {"id": idea["id"]})
        self.assertEqual(self.service.dispatch("idea_pad", {})["total"], 0)
        with self.assertRaises(ValueError):
            self.service.dispatch("idea_pad_delete", {"id": idea["id"]})

    def test_moments_find_busy_days_360_days_and_upcoming_anniversaries(self):
        for d in range(1, 21):
            self.add_day(f"2024-03-{d:02d}", 1, f"quiet{d}-")
        trip = self.add_day("2024-05-25", 8, "trip1-", summary="A beach at sunset") + self.add_day("2024-05-26", 8, "trip2-", summary="Waves")
        sphere = self.add_day("2025-10-11", 1, "s-", kind="video", summary="A panoramic airport", is_360=True,
                              filename="VID_20251011_120000_00_{i:03d}.insv")
        found = idea_pad.moments(self.store, today=date(2026, 5, 10))
        ids = [m["id"] for m in found["moments"]]
        self.assertEqual(set(ids), {"m:2024-05-25..2024-05-26", "m:2025-10-11"})
        trip_moment = next(m for m in found["moments"] if m["id"].startswith("m:2024"))
        self.assertEqual(trip_moment["counts"], {"photos": 16, "videos": 0, "360": 0, "total": 16})
        self.assertEqual(trip_moment["anniversary"], "2 years ago this month")
        self.assertEqual(trip_moment["tags"], ["beach"])
        self.assertTrue(trip_moment["devices"][0].startswith("iPhone (filename guess)"))
        self.assertLessEqual({s["asset_id"] for s in trip_moment["samples"]}, set(trip))
        sphere_moment = next(m for m in found["moments"] if m["id"] == "m:2025-10-11")
        self.assertEqual(sphere_moment["samples"][0], {"asset_id": sphere[0], "kind": "360 video",
                                                       "filename": "VID_20251011_120000_00_000.insv", "summary": "A panoramic airport 0"})
        self.assertEqual(idea_pad.moments(self.store, since="2025-01-01", today=date(2026, 5, 10))["moments"][0]["id"], "m:2025-10-11")
        for bad in ({"since": "2025-13-01"}, {"until": "yesterday"}, {"limit": 0}):
            with self.assertRaises(ValueError):
                idea_pad.moments(self.store, **bad)

    def test_generation_keeps_only_valid_moments_and_shots_and_skips_used_moments(self):
        trip = self.add_day("2024-05-25", 8, "trip-", summary="A beach at sunset")
        self.add_day("2025-10-11", 1, "s-", kind="video", summary="A panoramic airport", is_360=True)
        other = self.add_day("2020-01-01", 1, "lonely-", summary="Unrelated")
        sent = []
        def reply(path, payload, timeout):
            sent.append(payload)
            trip_id = "m:2024-05-25"
            ideas = [{"moment_id": trip_id, "title": "Sunset beach day", "logline": "Waves and light.", "format": "reel",
                      "why": "It was a beautiful day. " * 30, "shots": [trip[0], other[0], trip[0]], "tags": ["beach"]},
                     {"moment_id": "m:1999-01-01", "title": "Invented", "logline": "", "format": "vlog", "why": "", "shots": [], "tags": []},
                     {"moment_id": trip_id, "title": "Same moment again", "logline": "", "format": "short", "why": "", "shots": [], "tags": []},
                     "junk"]
            return {"message": {"content": json.dumps({"ideas": ideas})}}
        with patch("frameatlas.ideas.local_models", return_value=["phi4-mini:latest"]), \
                patch("frameatlas.resources.gpu_lease", return_value=contextlib.nullcontext()), \
                patch("frameatlas.ollama_local.request_json", side_effect=reply):
            result = self.service.dispatch("idea_pad_generate", {"count": 4, "focus": "beach"})
            self.assertEqual(result["model"], "phi4-mini:latest")
            self.assertEqual([i["title"] for i in result["items"]], ["Sunset beach day", "Same moment again"])
            saved = result["items"][0]
            self.assertEqual((saved["origin"], saved["format"], saved["basis"]["asset_ids"]), ("generated", "reel", [trip[0]]))
            self.assertLessEqual(len(saved["basis"]["why"]), 240)
            self.assertTrue(saved["basis"]["why"].endswith("."))
            schema = sent[0]["format"]["properties"]["ideas"]["items"]["properties"]
            self.assertNotIn("m:1999-01-01", schema["moment_id"]["enum"])
            self.assertTrue(sent[0]["options"]["num_ctx"] <= 16384 and sent[0]["keep_alive"] == 0)
            # The trip now has an idea, so the next request leaves it out and the model cites nothing valid.
            with self.assertRaisesRegex(ValueError, "no usable ideas"):
                self.service.dispatch("idea_pad_generate", {"count": 2})
            self.assertNotIn("m:2024-05-25", [m["id"] for m in json.loads(sent[1]["messages"][1]["content"])["moments"]])
        with patch("frameatlas.ideas.local_models", return_value=[]):
            with self.assertRaisesRegex(ValueError, "Install a local writing model"):
                self.service.dispatch("idea_pad_generate", {})
        with patch("frameatlas.ideas.local_models", return_value=["phi4-mini:latest"]):
            for bad in ({"count": 0}, {"count": "3"}, {"include_used": "yes"}, {"model": "llama:70b"}):
                with self.assertRaises(ValueError):
                    self.service.dispatch("idea_pad_generate", bad)

    def test_generated_text_is_checked_against_the_moment(self):
        long_summary = "A close-up of a computer screen displaying flight information for a flight to Amsterdam " + "x" * 300
        trip = self.add_day("2024-05-25", 8, "trip-", summary=long_summary)
        self.service.dispatch("idea_pad_add", {"title": "Existing Idea"})
        sent = []
        def reply(path, payload, timeout):
            sent.append(json.loads(payload["messages"][1]["content"]))
            ideas = [{"moment_id": "m:2024-05-25", "title": "Departures: A 360° Tour", "logline": "A 360-degree video of the gate.",
                      "format": "reel", "why": "Rare 360 footage.", "shots": [trip[0]], "tags": []},
                     {"moment_id": "m:2024-05-25", "title": "existing idea", "logline": "", "format": "short", "why": "", "shots": [], "tags": []}]
            return {"message": {"content": json.dumps({"ideas": ideas})}}
        with patch("frameatlas.ideas.local_models", return_value=["qwen3:8b"]), \
                patch("frameatlas.resources.gpu_lease", return_value=contextlib.nullcontext()), \
                patch("frameatlas.ollama_local.request_json", side_effect=reply):
            items = self.service.dispatch("idea_pad_generate", {"count": 2})["items"]
        self.assertEqual([(i["title"], i["logline"], i["basis"]["why"]) for i in items],
                         [("Departures: A Tour", "A video of the gate.", "Rare footage.")])
        moment = sent[0]["moments"][0]
        self.assertFalse(moment["has_360_footage"])
        summary = moment["samples"][0]["summary"]
        self.assertTrue(summary.endswith(" …") and len(summary) <= 222 and "Amsterdam" in summary)
        self.assertNotIn("xxxx x", summary)
        self.assertTrue(all(len(w) == len(w.strip()) for w in summary.split()))

    def test_a_small_library_still_has_moments(self):
        self.add_day("2024-05-25", 3, "a-", summary="A park")
        self.add_day("2024-06-01", 1, "b-", summary="A single photo")
        self.assertEqual([m["id"] for m in idea_pad.moments(self.store)["moments"]], ["m:2024-05-25"])
        self.assertEqual(idea_pad.moments(Store(Path(self.tmp.name) / "empty"))["moments"], [])

    def test_leap_day_anniversary_falls_on_feb_28(self):
        self.add_day("2024-02-29", 3, "leap-", summary="Leap day")
        self.add_day("2024-06-01", 1, "b-", summary="Other")
        got = idea_pad.moments(self.store, today=date(2027, 2, 20))["moments"][0]
        self.assertEqual((got["id"], got["anniversary"]), ("m:2024-02-29", "3 years ago this month"))
        self.assertEqual(idea_pad.moments(self.store, today=date(2028, 2, 20))["moments"][0]["anniversary"], "4 years ago this month")

    def test_missing_model_message_names_what_is_installed(self):
        with patch("frameatlas.ideas.local_models", return_value=["phi4-mini:latest"]):
            with self.assertRaisesRegex(ValueError, r"qwen3:8b is not installed in Ollama\. Installed: phi4-mini:latest\."):
                self.service.dispatch("idea_pad_generate", {"model": "qwen3:8b"})

    def test_list_counts_may_come_from_a_query_string(self):
        for k in range(3):
            self.service.dispatch("idea_pad_add", {"title": f"Idea {k}"})
        page = self.service.dispatch("idea_pad", {"limit": "2", "offset": "1"})
        self.assertEqual((len(page["items"]), page["total"]), (2, 3))
        for bad in ("-1", "2.5", "１", "99999999999", "x", True):
            with self.assertRaises(ValueError):
                self.service.dispatch("idea_pad", {"limit": bad})

    def test_add_refuses_unknown_fields(self):
        for extra in ({"origin": "generated"}, {"basis": {}}, {"model": "x"}, {"id": 3}):
            with self.assertRaisesRegex(ValueError, "Unknown idea field"):
                self.service.dispatch("idea_pad_add", {"title": "a", **extra})

    def test_malformed_model_reply_saves_nothing(self):
        self.add_day("2024-05-25", 8, "trip-", summary="A beach")
        for content in ("not json", json.dumps({"ideas": "x"}), json.dumps({})):
            with patch("frameatlas.ideas.local_models", return_value=["qwen3:8b"]), \
                    patch("frameatlas.resources.gpu_lease", return_value=contextlib.nullcontext()), \
                    patch("frameatlas.ollama_local.request_json", return_value={"message": {"content": content}}):
                with self.assertRaises(ValueError):
                    self.service.dispatch("idea_pad_generate", {"model": "qwen3:8b"})
        self.assertEqual(self.service.dispatch("idea_pad", {})["total"], 0)

    def test_earlier_story_explorations_move_onto_the_pad_once(self):
        aid = self.add_day("2024-05-25", 1, "s-", summary="A beach")[0]
        old_story = {"ideas": [{"title": "Beach walk", "hook": "Waves.", "outline": ["IMG_0000.HEIC — the sea"],
                                "sources": [{"asset_id": aid, "filename": "IMG_0000.HEIC", "start": 0, "end": 2, "reason": "the sea"}]}],
                     "model": "qwen3:8b"}
        with self.store.connect() as db:
            db.execute("INSERT INTO ideas(prompt,result,created) VALUES(?,?,?)", ("the beach", json.dumps(old_story), 1700000000))
            db.execute("INSERT INTO ideas(prompt,result,created) VALUES('broken','not json',1)")
            db.execute("INSERT INTO ideas(prompt,result,created) VALUES('odd',?,'x')", (json.dumps({"ideas": [{"title": "Odd"}]}),))
        listed = self.service.dispatch("idea_pad", {})
        self.assertEqual(listed["origins"], {"story": 2})
        beach = next(i for i in listed["items"] if i["title"] == "Beach walk")
        self.assertEqual((beach["origin"], beach["created"], beach["basis"]["prompt"]), ("story", 1700000000, "the beach"))
        self.assertEqual(beach["basis"]["sequence"]["outline"], ["IMG_0000.HEIC — the sea"])
        self.assertEqual(beach["basis"]["asset_ids"], [aid])
        # deleting an adopted idea keeps it deleted: adoption happens only once
        self.service.dispatch("idea_pad_delete", {"id": beach["id"]})
        self.assertEqual(self.service.dispatch("idea_pad", {})["total"], 1)
        self.assertEqual(self.service.dispatch("idea_pad", {"origin": "story"})["total"], 1)
        with self.assertRaises(ValueError):
            self.service.dispatch("idea_pad", {"origin": "elsewhere"})

    def test_any_idea_can_grow_an_edit_sequence(self):
        trip = self.add_day("2024-05-25", 3, "trip-", summary="A beach at sunset")
        unanalysed = self.add_day("2024-06-01", 1, "raw-")
        mine = self.service.dispatch("idea_pad_add", {"title": "Sunset", "logline": "Light on water", "asset_ids": [trip[1], unanalysed[0]]})
        bare = self.service.dispatch("idea_pad_add", {"title": "Anything with robots"})
        calls = []
        def story(store, prompt, model="", expand_search=False, *, asset_ids=None, search_scope=None):
            calls.append({"prompt": prompt, "model": model, "expand": expand_search, "ids": asset_ids, "scope": search_scope})
            take = lambda t, at: {"title": t, "hook": "h", "pickups": ["A wide of the bay"],
                                  "sources": [{"asset_id": trip[0], "filename": "IMG_0000.HEIC", "start": at, "end": at + 2, "reason": "open"}]}
            return {"ideas": [take("Best", 1), take("Other", 5)], "model": "gemma3:12b", "run": 7}
        with patch("frameatlas.ideas.generate", side_effect=story):
            built = self.service.dispatch("idea_pad_develop", {"id": mine["id"], "model": "gemma3:12b"})
            self.service.dispatch("idea_pad_develop", {"id": bare["id"]})
        # its analysed linked items only, and the idea's own words as the request
        self.assertEqual((calls[0]["ids"], calls[0]["scope"], calls[0]["model"]), ([trip[1]], None, "gemma3:12b"))
        self.assertIn("Sunset. Light on water", calls[0]["prompt"])
        self.assertTrue(calls[1]["expand"] and calls[1]["ids"] is None)
        seq = built["basis"]["sequence"]
        self.assertEqual((seq["sources"][0]["start"], seq["pickups"], seq["run"]), (1, ["A wide of the bay"], 7))
        self.assertEqual([a["title"] for a in seq["alternatives"]], ["Other"])
        self.assertEqual(built["basis"]["asset_ids"], [trip[1], unanalysed[0], trip[0]])
        self.assertEqual((built["title"], built["origin"]), ("Sunset", "manual"))
        for bad in ({"id": "1"}, {"id": 999}, {"id": mine["id"], "colour": "red"}):
            with self.assertRaises(ValueError):
                self.service.dispatch("idea_pad_develop", bad)

    def test_a_moment_idea_develops_from_its_days_and_falls_back_to_a_search(self):
        idea = {"title": "Beach day", "logline": "", "basis": {"moment": {"start": "2024-05-25", "end": "2024-05-26"}}}
        with self.store.connect() as db:
            idea_pad.ensure(db)
            iid = db.execute("INSERT INTO idea_pad(title,origin,basis,created,updated) VALUES(?,?,?,1,1)",
                             (idea["title"], "generated", json.dumps(idea["basis"]))).lastrowid
        calls = []
        def story(store, prompt, model="", expand_search=False, *, asset_ids=None, search_scope=None):
            calls.append(search_scope or ("expand" if expand_search else None))
            if search_scope:
                raise ValueError("No matching media has scene captions. Analyse matching media or adjust the Library filters.")
            return {"ideas": [{"title": "t", "hook": "h", "pickups": [], "sources": []}], "model": "m", "run": 1}
        with patch("frameatlas.ideas.generate", side_effect=story):
            self.service.dispatch("idea_pad_develop", {"id": iid})
        self.assertEqual(calls, [{"since": "2024-05-25", "until": "2024-05-26"}, "expand"])

    def test_mcp_exposes_the_idea_pad(self):
        names = {t[0] for t in TOOLS}
        self.assertLessEqual({"idea_pad_list", "idea_pad_add", "idea_pad_update", "idea_pad_delete", "idea_pad_moments",
                              "idea_pad_generate", "idea_pad_develop", "catalog_metadata_coverage", "catalog_metadata_backfill"}, names)
        reply = handle(self.service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "idea_pad_add", "arguments": {"title": "From an agent"}}})
        self.assertIn("From an agent", json.dumps(reply))
        self.assertEqual(self.service.dispatch("idea_pad", {})["total"], 1)


if __name__ == "__main__":
    unittest.main()
