from pathlib import Path
from itertools import product
import runpy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from frameatlas.native import Native, TICKS
from frameatlas.scene_batch import (MAX_SOURCE_BYTES, VIDEO_PAIR_BYTES, compatible, read_clip_speech,
                                    read_group_speech, run_batch, short_flat_candidate, stage_parallel)
from frameatlas.store import PICKER_ACQUISITION, Store
from frameatlas.worker import ANALYSIS_RESERVE, Worker


class SceneBatchTests(unittest.TestCase):
    def test_flat_video_preview_uses_a_captioned_scene_keyframe(self):
        native = Native.__new__(Native)
        shots = [
            {"start": 0, "end": 1, "keyframes": [0.5]},
            {"start": 1, "end": 5, "keyframes": [0, float("nan"), 2.5],
             "description": {"caption": "A lounge"}},
        ]
        with TemporaryDirectory() as directory:
            work = Path(directory)
            with patch.object(native, "run", return_value={"shots": shots}) as command:
                _, _, provenance = native.analyze(
                    work / "source.mp4", "video", {}, lambda _: None,
                    indexed_media=(1, {}, {}, {}, work, "fast", "whisper-base"),
                    speech_reader=lambda *_: ({}, "no_audio", ""),
                )
            command.assert_any_call("media.frame", {
                "item": 1, "seconds": 2.5, "maxSide": 512, "path": str(work / "native-preview.png"),
            })
            self.assertEqual(provenance["_preview_file"], "native-preview.png")

    def test_grouped_speech_keeps_source_identity_and_individual_fallback(self):
        with TemporaryDirectory() as directory:
            cache = SimpleNamespace(job=lambda: TemporaryDirectory(dir=directory))
            worker = SimpleNamespace(cache=cache, cancelled=lambda _: False, state=lambda *_: None)
            first = SimpleNamespace(jid=1, source=Path("first.mov"), work=directory)
            second = SimpleNamespace(jid=2, source=Path("second.mov"), work=directory)
            long = SimpleNamespace(jid=3, source=Path("long.mov"), work=directory)
            probe = {"duration": 10 * TICKS, "audio_streams": [{"sample_rate": 48000, "channels": 2}]}
            wide = {"duration": 10 * TICKS, "audio_streams": [{"sample_rate": 384000, "channels": 64}]}
            calls = []

            class LocalSpeech:
                def __init__(self, _work, cancelled):
                    self.cancelled = cancelled

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    pass

                def import_source(self, path):
                    return {first.source: 21, second.source: 22}[path], {}

                def run(self, name, args):
                    calls.append((name, args))
                    if name == "media.understandSpeech":
                        return {}
                    return {"item": 21, "words": [{"text": "first", "start": TICKS, "end": 2 * TICKS}]}

                def read_speech(self, item, *_):
                    return {"item": item, "words": []}, "complete", ""

            ready = [(first, probe, 101, {}), (second, probe, 102, {}), (long, wide, 103, {})]
            grouped = read_group_speech(worker, ready, LocalSpeech, "whisper-base", "auto")
            self.assertEqual(calls[0][1]["items"], [21, 22])
            self.assertEqual(set(grouped), {first.jid})
            self.assertEqual(grouped[first.jid]["words"][0]["start"], 1)
            recovered = read_clip_speech(worker, second, LocalSpeech, grouped, 102, probe,
                                         "whisper-base", "auto", lambda _: None)
            self.assertEqual(recovered[0]["item"], 22)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_studio_wait_photo_defers_if_broker_has_no_size_gate(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory))
            aid, _ = store.upsert({"provider": "google_photos", "source_id": "still",
                                   "filename": "still.jpg", "kind": "photo",
                                   "locator": {"session_id": "selected"}})
            jid = store.enqueue(aid)["id"]
            worker = Worker(store)
            try:
                worker.analysis_downloads.supported = False
                with patch("frameatlas.worker.broker") as broker, patch.object(worker, "analyze_source") as analyze:
                    run_batch(worker, [store.next_job()], Native, during_studio_wait=True)
                    broker.assert_not_called()
                    analyze.assert_not_called()
                self.assertEqual(store.job(jid)["state"], "queued")
                self.assertEqual(list(worker.cache.root.iterdir()), [])
            finally:
                worker.close()

    def test_parallel_staging_reserves_every_source_and_analysis(self):
        for kind, width, limit in (("photo", 2, MAX_SOURCE_BYTES), ("photo", 4, MAX_SOURCE_BYTES),
                                   ("photo", 8, MAX_SOURCE_BYTES),
                                   ("video", 2, VIDEO_PAIR_BYTES), ("video", 3, VIDEO_PAIR_BYTES)):
            with self.subTest(kind=kind, width=width):
                budget = ANALYSIS_RESERVE + width * limit - 1
                worker = SimpleNamespace(cache=SimpleNamespace(budget=lambda: budget))
                self.assertIsNone(stage_parallel(worker, [{}] * width, kind, {}, None, [], limit=limit))

    def test_photo_pair_keeps_successful_peer_and_requeues_size_deferral(self):
        self.check_pair_deferral("photo")

    def test_video_pair_probes_successful_peer_and_requeues_size_deferral(self):
        self.check_pair_deferral("video")

    def test_size_deferred_video_keeps_ordinary_allowance(self):
        self.check_video_individual_retry({"deferred": True})

    def test_slow_video_headers_keep_successful_peer_and_retry_individually(self):
        for deferred_index in (0, 1):
            with self.subTest(deferred_index=deferred_index):
                self.check_video_individual_retry(
                    {"deferred": True, "reason": "slow_video_headers"}, deferred_index)

    def check_video_individual_retry(self, deferral, deferred_index=1):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory))
            ids = []
            for i in range(3):
                aid, _ = store.upsert({"provider": "google_photos", "source_id": str(i),
                                       "filename": f"{i}.mov", "kind": "video",
                                       "locator": {"session_id": "selected"}})
                ids.append(store.enqueue(aid)["id"])
            worker = Worker(store)
            worker.analysis_downloads.paired_videos = True
            worker.analysis_downloads.maximum_paired_video_bytes = VIDEO_PAIR_BYTES

            def pair(session, files, limit, cancelled):
                self.assertEqual([identity for identity, _ in files], ["0", "1"])
                files[1 - deferred_index][1].write_bytes(b"video")
                outcomes = [{"bytes": 5}, {"bytes": 5}]
                outcomes[deferred_index] = deferral
                return outcomes

            def source(asset, work, jid, prefer_cloud, *, batch_limit):
                self.assertEqual(jid, ids[deferred_index])
                self.assertIsNone(batch_limit)
                path = work / "source"
                with path.open("xb") as media:
                    media.truncate(VIDEO_PAIR_BYTES + 1)
                return path, PICKER_ACQUISITION

            try:
                with patch.object(worker.analysis_downloads, "fetch_parallel", side_effect=pair) as fetch, \
                        patch.object(worker, "source", side_effect=source) as single, \
                        patch("frameatlas.scene_batch.prepare_short_video", return_value=False), \
                        patch.object(worker, "analyze_source"):
                    run_batch(worker, compatible(store.next_jobs()), Native)
                    self.assertEqual(store.job(ids[deferred_index])["result"]["rejected_pair_limit"], VIDEO_PAIR_BYTES)
                    self.assertEqual(store.job(ids[deferred_index])["state"], "queued")
                    self.assertEqual(store.job(ids[1 - deferred_index])["state"], "done")
                    run_batch(worker, compatible(Store(Path(directory)).next_jobs()), Native)
                fetch.assert_called_once()
                single.assert_called_once()
                self.assertEqual([store.job(jid)["state"] for jid in ids], ["done", "done", "queued"])
                self.assertEqual(list(worker.cache.root.iterdir()), [])
            finally:
                worker.close()

    def test_video_pair_uses_broker_allowance_but_analyzes_individually(self):
        for advertised, expected in ((96 * 1024**2, 96 * 1024**2), (VIDEO_PAIR_BYTES * 2, VIDEO_PAIR_BYTES)):
            with self.subTest(advertised=advertised):
                self.check_video_pair_allowance(advertised, expected)

    def test_three_video_staging_keeps_individual_analysis_and_falls_back_to_pair(self):
        for third_bytes, expected_width in ((0, 3), (VIDEO_PAIR_BYTES + 1, 2)):
            with self.subTest(third_bytes=third_bytes):
                self.check_video_pair_allowance(VIDEO_PAIR_BYTES, VIDEO_PAIR_BYTES,
                                                width=3, third_bytes=third_bytes,
                                                expected_width=expected_width)

    def check_video_pair_allowance(self, advertised, expected, *, width=2, third_bytes=0, expected_width=2):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory))
            ids = []
            for i in range(3):
                aid, _ = store.upsert({"provider": "google_photos", "source_id": str(i),
                                       "filename": f"{i}.mov", "kind": "video",
                                       "bytes": third_bytes if i == 2 else 0,
                                       "locator": {"session_id": "selected"}})
                ids.append(store.enqueue(aid)["id"])
            worker = Worker(store)
            worker.analysis_downloads.paired_videos = True
            worker.analysis_downloads.maximum_parallel_videos = width
            worker.analysis_downloads.maximum_paired_video_bytes = advertised

            def pair(session, files, limit, cancelled):
                self.assertEqual(limit, expected)
                self.assertEqual([identity for identity, _ in files], [str(i) for i in range(expected_width)])
                size = expected - 1
                for _, path in files:
                    with path.open("xb") as source:
                        source.truncate(size)
                return [{"bytes": size} for _ in files]

            try:
                with patch.object(worker.analysis_downloads, "fetch_parallel", side_effect=pair) as fetch, \
                        patch.object(worker, "source") as single, patch.object(worker, "analyze_source") as analyze:
                    run_batch(worker, compatible(store.next_jobs()), Native)
                fetch.assert_called_once()
                single.assert_not_called()
                self.assertEqual([call.args[2] for call in analyze.call_args_list], ids[:expected_width])
                self.assertEqual([store.job(jid)["state"] for jid in ids],
                                 ["done"] * expected_width + ["queued"] * (len(ids) - expected_width))
                self.assertEqual(list(worker.cache.root.iterdir()), [])
            finally:
                worker.close()

    def test_photo_batch_stages_parallel_head_and_falls_back_if_too_large(self):
        for (width, budget_width), (head_deferred, needs_peer_space) in product(
                ((2, 2), (4, 4), (8, 8), (8, 4)), ((False, False), (True, False), (True, True))):
            with self.subTest(width=width, budget_width=budget_width, head_deferred=head_deferred,
                              needs_peer_space=needs_peer_space), TemporaryDirectory() as directory:
                store = Store(Path(directory))
                ids = []
                count = max(4, width)
                for i in range(count):
                    aid, _ = store.upsert({"provider": "google_photos", "source_id": str(i),
                                           "filename": f"{i}.jpg", "kind": "photo",
                                           "locator": {"session_id": "selected"}})
                    ids.append(store.enqueue(aid)["id"])
                worker = Worker(store)
                worker.analysis_downloads.paired_photos = True
                worker.analysis_downloads.maximum_parallel_photos = width
                attempts = []

                def pair(session, files, limit, cancelled):
                    attempts.append([int(identity) for identity, _ in files])
                    outcomes = []
                    for identity, path in files:
                        if head_deferred and identity == "0":
                            outcomes.append({"deferred": True})
                        else:
                            path.write_bytes(b"test")
                            outcomes.append({"bytes": 4})
                    return outcomes

                def source(asset, work, jid, prefer_cloud, *, batch_limit):
                    self.assertEqual(jid, ids[0])
                    self.assertIsNone(batch_limit)
                    peer_bytes = sum(path.stat().st_size for path in worker.cache.root.glob("job-*/source.jpg"))
                    if needs_peer_space and peer_bytes:
                        raise ValueError("Source exceeds the temporary storage budget")
                    self.assertEqual(peer_bytes, 0 if needs_peer_space else 4 * (budget_width - 1))
                    path = work / "source"
                    path.write_bytes(b"head")
                    return path, PICKER_ACQUISITION

                try:
                    with patch.object(worker.cache, "budget",
                                      return_value=ANALYSIS_RESERVE + budget_width * MAX_SOURCE_BYTES), \
                            patch.object(worker.analysis_downloads, "fetch_parallel", side_effect=pair), \
                            patch.object(worker, "source", side_effect=source) as single, \
                            patch("frameatlas.scene_batch.describe"), patch.object(worker, "analyze_source") as analyze:
                        run_batch(worker, compatible(store.next_jobs()), Native)
                    expected = [list(range(start, start + budget_width)) for start in range(0, count, budget_width)]
                    self.assertEqual(attempts, expected[:1] if head_deferred else expected)
                    self.assertEqual(single.call_count, int(head_deferred) + int(needs_peer_space))
                    finished = 1 if needs_peer_space else budget_width if head_deferred else count
                    self.assertEqual([call.args[2] for call in analyze.call_args_list], ids[:finished])
                    self.assertEqual([store.job(jid)["state"] for jid in ids],
                                     ["done"] * finished + ["queued"] * (count - finished))
                    self.assertEqual(list(worker.cache.root.iterdir()), [])
                finally:
                    worker.close()

    def check_pair_deferral(self, kind):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory))
            ids = []
            for i in range(3):
                aid, _ = store.upsert({"provider": "google_photos", "source_id": str(i),
                                       "filename": f"{kind}-{i}", "kind": kind,
                                       "locator": {"session_id": "selected"}})
                ids.append(store.enqueue(aid)["id"])
            worker = Worker(store)
            worker.analysis_downloads.paired_photos = kind == "photo"
            worker.analysis_downloads.paired_videos = kind == "video"
            try:
                def source(asset, work, jid, prefer_cloud, *, batch_limit):
                    self.assertEqual(jid, ids[0])
                    path = work / "source"
                    path.write_bytes(b"head")
                    return path, PICKER_ACQUISITION

                def pair(session, files, limit, cancelled):
                    self.assertEqual(session, "selected")
                    self.assertEqual(limit, MAX_SOURCE_BYTES)
                    self.assertEqual([identity for identity, _ in files], ["1", "2"])
                    self.assertNotEqual(files[0][1].parent, files[1][1].parent)
                    self.assertFalse(cancelled())
                    files[1][1].write_bytes(b"peer")
                    return [{"deferred": True}, {"bytes": 4}]

                probe = {"kind": "Movie", "duration": TICKS, "video": {"width": 1920, "height": 1080}}
                with patch.object(worker.cache, "budget", return_value=ANALYSIS_RESERVE + 2 * MAX_SOURCE_BYTES), \
                        patch.object(worker, "source", side_effect=source), \
                        patch.object(worker.analysis_downloads, "fetch_parallel", side_effect=pair) as fetch, \
                        patch("frameatlas.scene_batch.probe_media", return_value=probe), \
                        patch("frameatlas.scene_batch.describe") as describe, \
                        patch.object(worker, "analyze_source"):
                    run_batch(worker, compatible(store.next_jobs()), Native)
                    fetch.assert_called_once()
                    self.assertEqual([p.jid for p in describe.call_args.args[1]], [ids[0], ids[2]])
                    if kind == "video":
                        self.assertEqual([p.probe for p in describe.call_args.args[1]], [probe, probe])
                self.assertEqual([store.job(jid)["state"] for jid in ids], ["done", "queued", "done"])
                self.assertEqual(store.next_job()["id"], ids[1])
                self.assertEqual(list(worker.cache.root.iterdir()), [])
            finally:
                worker.close()

    def test_analysis_settings_do_not_scan_unrelated_models(self):
        with patch.object(Native, "run", autospec=True) as run:
            native = object.__new__(Native)
            run.side_effect = lambda _, command: (
                {"available": True, "fast": {"missingBytes": 0}}
                if command == "scenes.status" else {"models": []}
            )
            self.assertEqual(native.analysis_settings("photo", {}), ("fast", "whisper-base"))
            self.assertEqual(native.analysis_settings("video", {"model": "selected"}), ("fast", "selected"))
            self.assertEqual(native.analysis_settings("audio", {"model": "selected"}), ("fast", "selected"))
            self.assertEqual([call.args[1] for call in run.call_args_list], ["scenes.status", "scenes.status"])

    def test_google_job_receipt_preserves_elapsed_phases_and_acquisition(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory))
            aid, _ = store.upsert({"provider": "google_photos", "source_id": "photo",
                                   "filename": "photo.jpg", "kind": "photo",
                                   "locator": {"session_id": "selected"}})
            jid = store.enqueue(aid)["id"]
            worker = Worker(store)
            try:
                with patch("frameatlas.store.time.time", return_value=100):
                    self.assertTrue(store.claim_job(jid))
                with patch("frameatlas.worker.time.time", return_value=102):
                    worker.state(jid, "Preparing native analysis")
                with patch("frameatlas.worker.time.time", return_value=103):
                    worker.state(jid, "Preparing native analysis")
                with patch("frameatlas.worker.time.time", return_value=105):
                    worker.state(jid, "Describing 8 photos locally")
                with patch("frameatlas.worker.time.time", return_value=113):
                    worker.state(jid, "Saving a small local preview")
                    store.analysis(aid, [{"start": 0, "end": 0, "description": {"caption": "Tree"}}],
                                   {}, {"acquisition": PICKER_ACQUISITION}, job_id=jid)
                with patch("frameatlas.worker.time.time", return_value=114):
                    worker.state(jid, "Finished · temporary media removed", "done")
                self.assertEqual(store.job(jid)["result"], {
                    "analysis_source": "picker",
                    "phase_seconds": {"source": 2, "prepare": 3, "scenes": 8, "preview_and_cleanup": 1},
                })
            finally:
                worker.close()

    def test_lookahead_fills_batch_without_crossing_work_boundaries(self):
        jobs = [{"id": n, "task": "analysis", "asset_kind": kind,
                 "asset_provider": "google_photos", "options": "{}",
                 "picker_session": "selection", "analysis_deadline": 2450000.0}
                for n, kind in enumerate(("photo", "video", "photo", "video", "video",
                                          "photo", "video", "photo", "photo"))]
        self.assertEqual([j["id"] for j in compatible(jobs)], [0, 2, 5, 7, 8])
        self.assertEqual([j["id"] for j in compatible(jobs[1:])], [1, 3, 4])
        self.assertEqual([j["id"] for j in compatible([jobs[1], *jobs[3:]])], [1, 3, 4])
        extra = [{**jobs[0], "id": i} for i in range(len(jobs), 40)]
        self.assertEqual([j["id"] for j in compatible(jobs + extra)], [0, 2, 5, 7, 8, *range(9, 32)])
        only_photos = [{**jobs[0], "id": n} for n in range(40)]
        self.assertEqual(compatible(only_photos), only_photos[:32])
        mostly_videos = [jobs[0], *[{**jobs[1], "id": i} for i in range(1, 33)]]
        mostly_videos[32]["asset_kind"] = "photo"
        self.assertEqual(compatible(mostly_videos), [jobs[0]])
        mostly_photos = [jobs[1], *[{**jobs[0], "id": i} for i in range(2, 18)]]
        mostly_photos[16]["asset_kind"] = "video"
        self.assertEqual(compatible(mostly_photos), [jobs[1]])
        for field, value in (("task", "retrieve"), ("asset_provider", "insta360"),
                             ("options", '{"priority":true}'), ("picker_session", "new"),
                             ("analysis_deadline", None)):
            with self.subTest(field=field):
                changed = [jobs[0], {**jobs[1], field: value}, *jobs[2:]]
                self.assertEqual(compatible(changed), [jobs[0]])
                changed = [jobs[1], {**jobs[0], field: value}, *jobs[3:]]
                self.assertEqual(compatible(changed), [jobs[1]])

    def test_cancelled_member_and_non_still_do_not_lose_other_results(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "catalog")
            ids = []
            for n in range(4):
                source = root / f"source-{n}.jpg"
                source.write_bytes(b"source")
                aid, _ = store.upsert({"provider": "google_photos", "source_id": str(n),
                                       "filename": source.name, "kind": "photo", "local_path": str(source)})
                ids.append(store.enqueue(aid)["id"])
            described = []
            fallback = []

            class Native:
                def __init__(self, work, cancelled):
                    self.next_item = 0

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def analysis_settings(self, kind, opts):
                    return "fast", "whisper-base"

                def import_source(self, source):
                    self.next_item += 1
                    return self.next_item, {}

                def run(self, command, params):
                    return {"items": [{"item": item, "vr": None} for item in params["items"]]}

                def describe(self, items, detail, fresh):
                    described.extend(items)
                    store.cancel(ids[1])
                    return {"items": len(items), "job": 1, "scenes": {"items": [{"item": item} for item in items]}}

                def analyze(self, path, kind, opts, stage, *, indexed_media, speech_reader=None):
                    return ([{"start": 0, "end": 1, "description": {"caption": path.stem}}], {},
                            {"speech_status": "not_applicable", "media_kind": "photo"})

            worker = Worker(store)
            try:
                def save(asset, work, jid, source, acquisition, scenes, speech, provenance):
                    store.analysis(asset["id"], scenes, speech, provenance, job_id=jid)

                def single(asset, work, jid, opts, source, acquisition):
                    fallback.append(jid)
                    store.analysis(asset["id"], [{"start": 0, "end": 1, "description": {"caption": "Fallback"}}],
                                   {}, {"speech_status": "not_applicable"}, job_id=jid)

                with patch("frameatlas.scene_batch.probe_media",
                           side_effect=lambda p, *, native: {"kind": "Video" if p.stem.endswith("-2") else "Still"}) as probe, \
                        patch.object(worker, "save_analysis", side_effect=save), \
                        patch.object(worker, "analyze_source", side_effect=single):
                    run_batch(worker, compatible(store.next_jobs(4)), Native)
                    self.assertEqual(probe.call_count, 4)
                    self.assertEqual(len({id(call.kwargs["native"]) for call in probe.call_args_list}), 1)
            finally:
                worker.close()
            self.assertEqual(described, [1, 2, 3])
            self.assertEqual(fallback, [ids[2]])
            self.assertEqual([store.job(j)["state"] for j in ids], ["done", "cancelled", "done", "done"])
            self.assertEqual(store.stats()["scenes"], 3)
            self.assertEqual(list(worker.cache.root.iterdir()), [])

    def test_short_video_transcript_cancelled_in_its_own_session(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "catalog")
            ids = []
            assets = []
            for n in range(3):
                source = root / f"clip-{n}.mov"
                source.write_bytes(b"source")
                aid, _ = store.upsert({"provider": "google_photos", "source_id": str(n),
                                       "filename": source.name, "kind": "video", "local_path": str(source)})
                assets.append(aid)
                ids.append(store.enqueue(aid)["id"])
            described, transcribed = [], []

            class VideoNative(Native):
                def __init__(self, work, cancelled):
                    self.work, self.cancelled = work, cancelled
                    self.paths = {}

                def close(self):
                    pass

                def analysis_settings(self, kind, opts):
                    return "fast", "whisper-base"

                def import_source(self, source):
                    item = len(self.paths) + 1
                    self.paths[item] = source
                    return item, {}

                def describe(self, items, detail, fresh):
                    described.extend(items)
                    return {"items": len(items), "job": 1, "scenes": {"items": [{"item": item} for item in items]}}

                def run(self, command, params):
                    if command == "media.vrProperties":
                        return {"items": [{"item": item, "vr": None} for item in params["items"]]}
                    if command == "media.scenes":
                        return {"shots": [{"start": 0, "end": 1,
                                           "description": {"caption": self.paths[params["item"]].stem}}]}
                    return {}

                def read_speech(self, item, probe, model, language, stage):
                    self_path = self.paths[item]
                    transcribed.append((len(self.paths), self_path.stem))
                    if self_path.stem == "clip-1":
                        store.cancel(ids[1])
                        if self.cancelled():
                            raise RuntimeError("Job cancelled")
                    return {"text": self_path.stem}, "complete", ""

            worker = Worker(store)
            try:
                def save(asset, work, jid, source, acquisition, scenes, speech, provenance):
                    store.analysis(asset["id"], scenes, speech, provenance, job_id=jid)

                probe = {"kind": "Movie", "duration": 12 * TICKS, "video": {"width": 1920, "height": 1080}}
                with patch("frameatlas.scene_batch.probe_media", return_value=probe), \
                        patch.object(worker, "save_analysis", side_effect=save), \
                        patch.object(worker, "analyze_source") as fallback:
                    run_batch(worker, compatible(store.next_jobs()), VideoNative)
                    fallback.assert_not_called()
            finally:
                worker.close()
            self.assertEqual(described, [1, 2, 3])
            self.assertEqual(transcribed, [(1, "clip-0"), (1, "clip-1"), (1, "clip-2")])
            self.assertEqual([store.job(j)["state"] for j in ids], ["done", "cancelled", "done"])
            self.assertEqual([store.get(a)["transcript"] for a in assets], ["clip-0", "", "clip-2"])
            self.assertEqual(list(worker.cache.root.iterdir()), [])

    def test_oversized_speculative_video_leaves_queue_after_short_peers_finish(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory))
            ids = []
            for n in range(3):
                aid, _ = store.upsert({"provider": "google_photos", "source_id": str(n),
                                       "filename": f"clip-{n}.mov", "kind": "video",
                                       "locator": {"session_id": "selected"}})
                ids.append(store.enqueue(aid)["id"])
            # The worker launched with -m has its own module namespace.
            from warnings import catch_warnings, filterwarnings
            with catch_warnings():
                filterwarnings("ignore", message="'frameatlas.worker' found in sys.modules", category=RuntimeWarning)
                entry = runpy.run_module("frameatlas.worker", run_name="__worker_entry_test__")
            worker = entry["Worker"](store)
            seen = []
            try:
                def source(asset, work, jid, prefer_cloud, *, batch_limit):
                    seen.append((jid, batch_limit))
                    if jid == ids[2]:
                        raise entry["BatchSizeDeferred"]("Cloud rendition is too large")
                    path = work / "source.mov"
                    path.write_bytes(b"short source")
                    return path, PICKER_ACQUISITION

                probe = {"kind": "Movie", "duration": 12*TICKS, "video": {"width": 1920, "height": 1080}}
                with patch.object(worker, "source", side_effect=source), \
                        patch("frameatlas.scene_batch.probe_media", return_value=probe), \
                        patch("frameatlas.scene_batch.describe") as describe, \
                        patch.object(worker, "analyze_source") as analyze:
                    run_batch(worker, compatible(store.next_jobs()), Native)
                    self.assertEqual([p.jid for p in describe.call_args.args[1]], ids[:2])
                    self.assertEqual([call.args[2] for call in analyze.call_args_list], ids[:2])
                self.assertEqual(seen, [(ids[0], None), (ids[1], MAX_SOURCE_BYTES), (ids[2], MAX_SOURCE_BYTES)])
                self.assertEqual([store.job(jid)["state"] for jid in ids], ["done", "done", "queued"])
                self.assertEqual(store.next_job()["id"], ids[2])
                self.assertEqual(list(worker.cache.root.iterdir()), [])
            finally:
                worker.close()

    def test_video_cap_rejects_invalid_or_oversized_native_probes(self):
        clip = {"kind": "Movie", "duration": 20 * TICKS, "video": {"width": 2160, "height": 3840}}
        self.assertTrue(short_flat_candidate(clip))
        for changes in ({"duration": 20 * TICKS + 1}, {"duration": True}, {"kind": "Still"},
                        {"video": {"width": 3840, "height": 3840}}, {"video": {"width": -1, "height": 3840}}):
            self.assertFalse(short_flat_candidate({**clip, **changes}), changes)
