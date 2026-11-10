from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch

from frameatlas.lookahead import Lookahead
from frameatlas.native import Native
from frameatlas.scene_batch import MAX_SOURCE_BYTES
from frameatlas.store import PICKER_ACQUISITION, Store
from frameatlas.worker import ANALYSIS_RESERVE, Worker


class LookaheadTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "catalogue")
        self.worker = Worker(self.store)
        self.addCleanup(self.worker.close)
        self.worker.analysis_downloads.paired_photos = True
        self.worker.analysis_downloads.maximum_parallel_photos = 8

    def enqueue(self, count, *, kind="photo", options=None, start=0):
        jobs = []
        for index in range(start, start + count):
            asset, _ = self.store.upsert({
                "provider": "google_photos", "source_id": f"{kind}-{index}",
                "filename": f"{index}." + ("jpg" if kind == "photo" else "mov"),
                "kind": kind, "locator": {"session_id": "selected"},
            })
            jobs.append(self.store.enqueue(asset, options)["id"])
        return jobs

    @staticmethod
    def files_ready(_session, files, _limit, cancelled, *, timeout):
        if cancelled():
            raise RuntimeError("Speculative transfer interrupted")
        for _, target in files:
            target.write_bytes(b"sample")
        return [{"bytes": 6} for _ in files]

    def test_real_loop_overlaps_and_keeps_full_still_width(self):
        foreground = self.enqueue(1, kind="video", options={"interactive": True})[0]
        photos = self.enqueue(32, options={"priority": True})
        trailing = self.enqueue(1, start=32)[0]
        last_wave = Event()

        def fetch(session, files, limit, cancelled, *, timeout):
            result = self.files_ready(session, files, limit, cancelled, timeout=timeout)
            if files[-1][0] == "photo-31":
                last_wave.set()
            return result

        def source(_asset, work, jid, _prefer_cloud):
            self.assertEqual(jid, foreground)
            path = work / "source"
            path.write_bytes(b"foreground")
            return path, PICKER_ACQUISITION

        def analyze(_asset, _work, jid, _opts, _source, _acquisition):
            if jid == foreground:
                self.assertTrue(last_wave.wait(4), "Final transfer must start during foreground analysis")
            if jid == photos[-1]:
                self.worker.stop.set()

        with patch.object(self.worker, "source", side_effect=source), \
                patch.object(self.worker.analysis_downloads, "fetch_parallel", side_effect=fetch) as transfer, \
                patch.object(self.worker, "analyze_source", side_effect=analyze), \
                patch("frameatlas.scene_batch.describe") as describe:
            self.worker.start()
            self.worker.thread.join(timeout=6)
            self.assertFalse(self.worker.thread.is_alive())
            self.assertEqual([len(call.args[1]) for call in transfer.call_args_list], [8, 8, 8, 8])
            self.assertEqual([member.jid for member in describe.call_args.args[1]], photos)
        self.assertTrue(all(self.store.job(j)["state"] == "done" for j in [foreground, *photos]))
        self.assertEqual(self.store.job(trailing)["state"], "queued")
        self.assertEqual(list(self.worker.cache.root.iterdir()), [])

    def test_pause_or_priority_interrupts_live_transfer_and_requeues(self):
        for interrupt in ("pause", "retrieval"):
            with self.subTest(interrupt=interrupt):
                photos = self.enqueue(2, options={"priority": True})
                started = Event()
                interrupted = Event()

                def fetch(_session, files, _limit, cancelled, *, timeout):
                    files[0][1].write_bytes(b"partial")
                    started.set()
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline:
                        if cancelled():
                            interrupted.set()
                            raise RuntimeError("Speculative transfer interrupted")
                        time.sleep(0.05)
                    self.fail("Priority change did not interrupt the live transfer")

                with patch.object(self.worker.analysis_downloads, "fetch_parallel", side_effect=fetch), \
                        Lookahead(self.worker, Native) as lookahead:
                    lookahead.start()
                    self.assertTrue(started.wait(3))
                    if interrupt == "pause":
                        self.store.queue_state(True)
                    else:
                        asset = self.store.job(photos[0])["asset_id"]
                        retrieval = self.store.enqueue_retrieval(asset, self.directory.name)["id"]
                    self.assertFalse(lookahead.consume())
                    self.assertTrue(interrupted.is_set())
                self.assertTrue(all(self.store.job(j)["state"] == "queued" for j in photos))
                self.assertEqual(list(self.worker.cache.root.iterdir()), [])
                if interrupt == "retrieval":
                    self.assertEqual(self.store.next_job()["id"], retrieval)
                self.store.queue_state(False)

    def test_drain_finishes_active_and_prefetched_work_without_claiming_more(self):
        foreground = self.enqueue(1, kind="video", options={"interactive": True})[0]
        staged = self.enqueue(2, options={"priority": True})
        later = self.enqueue(2, start=2)
        transferring = Event()

        def fetch(session, files, limit, cancelled, *, timeout):
            transferring.set()
            self.assertTrue(self.worker.draining.wait(3))
            return self.files_ready(session, files, limit, cancelled, timeout=timeout)

        def source(_asset, work, jid, _prefer_cloud):
            self.assertEqual(jid, foreground)
            path = work / "source"
            path.write_bytes(b"foreground")
            return path, PICKER_ACQUISITION

        def analyze(_asset, _work, jid, _opts, _source, _acquisition):
            if jid == foreground:
                self.assertTrue(transferring.wait(3))
                self.worker.draining.set()
            self.assertFalse(self.worker.cancelled(jid))

        with patch.object(self.worker, "source", side_effect=source), \
                patch.object(self.worker.analysis_downloads, "fetch_parallel", side_effect=fetch) as transfer, \
                patch.object(self.worker, "analyze_source", side_effect=analyze), \
                patch("frameatlas.scene_batch.describe"):
            self.worker.start()
            self.worker.thread.join(timeout=6)
            self.assertFalse(self.worker.thread.is_alive())
            transfer.assert_called_once()
        self.assertTrue(all(self.store.job(j)["state"] == "done" for j in [foreground, *staged]))
        self.assertTrue(all(self.store.job(j)["state"] == "queued" for j in later))
        self.assertFalse(self.store.queue_state()["paused"])
        self.assertEqual(list(self.worker.cache.root.iterdir()), [])

    def test_full_batch_budget_reserves_active_analysis_space(self):
        photos = self.enqueue(32)
        budget = ANALYSIS_RESERVE + 32 * MAX_SOURCE_BYTES - 1
        with patch.object(self.worker.cache, "budget", return_value=budget), \
                patch.object(self.worker.analysis_downloads, "fetch_parallel") as transfer, \
                Lookahead(self.worker, Native) as lookahead:
            lookahead.start()
            self.assertFalse(lookahead.consume())
            transfer.assert_not_called()
        self.assertTrue(all(self.store.job(j)["state"] == "queued" for j in photos))
        self.assertEqual(list(self.worker.cache.root.iterdir()), [])

    def test_failed_still_transfer_keeps_verified_peers_across_waves(self):
        photos = self.enqueue(16)

        def fetch(session, files, limit, cancelled, *, timeout):
            result = self.files_ready(session, files, limit, cancelled, timeout=timeout)
            if files[0][0] == "photo-8":
                result[0] = {"error": "Temporary transfer failure"}
            return result

        with patch.object(self.worker.analysis_downloads, "fetch_parallel", side_effect=fetch) as transfer, \
                patch.object(self.worker, "analyze_source") as analyze, \
                patch("frameatlas.scene_batch.describe"), \
                Lookahead(self.worker, Native) as lookahead:
            lookahead.start()
            self.assertTrue(lookahead.consume())
            self.assertEqual(transfer.call_count, 2)
            self.assertEqual([call.args[2] for call in analyze.call_args_list], photos[:8] + photos[9:])
        self.assertEqual([row["id"] for row in self.store.next_jobs()], photos[8:9])
        self.assertEqual(list(self.worker.cache.root.iterdir()), [])

    def test_lookahead_deadline_preserves_only_already_downloaded_wave(self):
        photos = self.enqueue(16)
        clock = [0]

        def fetch(session, files, limit, cancelled, *, timeout):
            results = self.files_ready(session, files, limit, cancelled, timeout=timeout)
            clock[0] = 76
            return results

        with patch("frameatlas.lookahead.time") as time_source, \
                patch.object(self.worker.analysis_downloads, "fetch_parallel", side_effect=fetch) as transfer, \
                patch.object(self.worker, "analyze_source") as analyze, \
                patch("frameatlas.scene_batch.describe"), \
                Lookahead(self.worker, Native) as lookahead:
            time_source.monotonic.side_effect = lambda: clock[0]
            lookahead.start()
            self.assertTrue(lookahead.consume())
            transfer.assert_called_once()
            self.assertEqual([call.args[2] for call in analyze.call_args_list], photos[:8])
        self.assertEqual([row["id"] for row in self.store.next_jobs()], photos[8:])
        self.assertEqual(list(self.worker.cache.root.iterdir()), [])

    def test_interrupted_wave_keeps_only_receipted_complete_file(self):
        photos = self.enqueue(4)

        def interrupted(_session, files, _limit, _cancelled, *, timeout):
            first, old_broker, bad_receipt, partial = [target for _, target in files]
            for target in (first, old_broker, bad_receipt):
                target.write_bytes(b"sample")
            first.with_name(".frameatlas-transfer-complete").write_bytes(b"6\n")
            bad_receipt.with_name(".frameatlas-transfer-complete").write_bytes(b"4\n")
            partial.with_name(".frameatlas-transfer").write_bytes(b"incomplete")
            raise RuntimeError("Cloud operation timed out")

        with patch.object(self.worker.analysis_downloads, "fetch_parallel", side_effect=interrupted), \
                patch.object(self.worker, "analyze_source") as analyze, \
                Lookahead(self.worker, Native) as lookahead:
            lookahead.start()
            self.assertTrue(lookahead.consume())
            self.assertEqual([call.args[2] for call in analyze.call_args_list], photos[:1])
        self.assertEqual([row["id"] for row in self.store.next_jobs()], photos[1:])
        self.assertEqual(list(self.worker.cache.root.iterdir()), [])

    def test_pause_after_transfer_discards_staged_group_without_analyzing(self):
        jobs = self.enqueue(2)
        with patch.object(self.worker.analysis_downloads, "fetch_parallel", side_effect=self.files_ready), \
                patch.object(self.worker, "analyze_source") as analyze, \
                Lookahead(self.worker, Native) as lookahead:
            lookahead.start()
            self.assertIsNotNone(lookahead.future.result(timeout=3))
            self.store.queue_state(True)
            self.assertFalse(lookahead.consume())
            analyze.assert_not_called()
        self.assertEqual([self.store.job(j)["state"] for j in jobs], ["queued", "queued"])
        self.assertEqual(list(self.worker.cache.root.iterdir()), [])

    def test_higher_priority_google_batch_preempts_staged_default_jobs(self):
        jobs = self.enqueue(2)
        with patch.object(self.worker.analysis_downloads, "fetch_parallel", side_effect=self.files_ready), \
                patch.object(self.worker, "analyze_source") as analyze, \
                Lookahead(self.worker, Native) as lookahead:
            lookahead.start()
            self.assertIsNotNone(lookahead.future.result(timeout=3))
            prioritized = self.enqueue(1, start=2, options={"priority": True})[0]
            self.assertFalse(lookahead.consume())
            analyze.assert_not_called()
        self.assertEqual(self.store.next_job()["id"], prioritized)
        self.assertEqual([self.store.job(j)["state"] for j in jobs], ["queued", "queued"])
        self.assertEqual(list(self.worker.cache.root.iterdir()), [])

    def test_long_prefetched_videos_keep_individual_native_analysis(self):
        jobs = self.enqueue(3, kind="video")
        downloads = self.worker.analysis_downloads
        downloads.paired_videos = True
        downloads.maximum_parallel_videos = 3
        downloads.maximum_paired_video_bytes = 256 * 1024**2
        with patch.object(downloads, "fetch_parallel", side_effect=self.files_ready), \
                patch("frameatlas.lookahead.prepare_short_video", return_value=False) as probe, \
                patch.object(self.worker, "analyze_source") as analyze, \
                Lookahead(self.worker, Native) as lookahead:
            lookahead.start()
            self.assertTrue(lookahead.consume())
            self.assertEqual(probe.call_count, 3)
            self.assertEqual([call.args[2] for call in analyze.call_args_list], jobs)
        self.assertTrue(all(self.store.job(j)["state"] == "done" for j in jobs))
        self.assertEqual(list(self.worker.cache.root.iterdir()), [])

    def test_single_video_and_rejected_pair_head_use_bounded_standalone_prefetch(self):
        downloads = self.worker.analysis_downloads
        downloads.paired_videos = downloads.known_size_batch = True
        downloads.maximum_parallel_videos = 3
        limit = downloads.maximum_paired_video_bytes = 256 * 1024**2
        for start, count in enumerate((1, 3)):
            jobs = self.enqueue(count, kind="video", start=start)
            head = jobs[0]
            if count > 1:
                self.assertTrue(self.store.claim_job(head))
                self.store.defer_job(head, rejected_pair_limit=limit)

            def fetch(session, source, target, budget, cancelled, **options):
                self.assertEqual((session, source, budget), ("selected", f"video-{start}", limit))
                self.assertEqual(options["batch_limit"], limit)
                self.assertTrue(options["require_size"])
                self.assertFalse(cancelled())
                target.write_bytes(b"video")
                return {"bytes": 5}

            with self.subTest(count=count), patch.object(downloads, "fetch", side_effect=fetch) as transfer, \
                    patch.object(downloads, "fetch_parallel") as pair, \
                    patch.object(self.worker, "analyze_source") as analyze, \
                    patch("frameatlas.lookahead.prepare_short_video", return_value=False), \
                    Lookahead(self.worker, Native) as lookahead:
                lookahead.start()
                self.assertIsNotNone(lookahead.future.result(timeout=3))
                self.worker.draining.set()
                self.assertTrue(lookahead.consume())
                transfer.assert_called_once()
                pair.assert_not_called()
                self.assertEqual(analyze.call_args.args[2], head)
            self.assertEqual(self.store.job(head)["state"], "done")
            self.assertTrue(all(self.store.job(j)["state"] == "queued" for j in jobs[1:]))
            self.assertEqual(list(self.worker.cache.root.iterdir()), [])
            self.worker.draining.clear()

    def test_interrupted_standalone_video_requeues_and_removes_partial_file(self):
        job = self.enqueue(1, kind="video")[0]
        downloads = self.worker.analysis_downloads
        downloads.paired_videos = downloads.known_size_batch = True
        downloads.maximum_paired_video_bytes = 256 * 1024**2

        def interrupted(_session, _source, target, _budget, _cancelled, **_options):
            target.with_name(".frameatlas-transfer").write_bytes(b"partial")
            raise RuntimeError("Speculative transfer interrupted")

        with patch.object(downloads, "fetch", side_effect=interrupted) as transfer, \
                Lookahead(self.worker, Native) as lookahead:
            lookahead.start()
            self.assertFalse(lookahead.consume())
            transfer.assert_called_once()
        self.assertEqual(self.store.job(job)["state"], "queued")
        self.assertEqual(list(self.worker.cache.root.iterdir()), [])

    def test_deferred_video_keeps_verified_peers_unless_preempted(self):
        downloads = self.worker.analysis_downloads
        downloads.paired_videos = True
        downloads.maximum_parallel_videos = 3
        downloads.maximum_paired_video_bytes = 256 * 1024**2
        for group, preempt in enumerate((False, True)):
            jobs = self.enqueue(3, kind="video", start=3 * group)

            def fetch(_session, files, _limit, _cancelled, *, timeout):
                for _, target in files[1:]:
                    target.write_bytes(b"sample")
                return [{"deferred": True, "reason": "slow_video_headers"}, {"bytes": 6}, {"bytes": 6}]

            with self.subTest(preempt=preempt), \
                    patch.object(downloads, "fetch_parallel", side_effect=fetch) as transfer, \
                    patch("frameatlas.lookahead.prepare_short_video", return_value=False), \
                    patch.object(self.worker, "analyze_source") as analyze, \
                    Lookahead(self.worker, Native) as lookahead:
                lookahead.start()
                self.assertIsNotNone(lookahead.future.result(timeout=3))
                if preempt:
                    self.store.enqueue_retrieval(self.store.job(jobs[0])["asset_id"], self.directory.name)
                self.assertEqual(lookahead.consume(), not preempt)
                transfer.assert_called_once()
                self.assertEqual([call.args[2] for call in analyze.call_args_list], [] if preempt else jobs[1:])
            self.assertEqual(self.store.job(jobs[0])["result"]["rejected_pair_limit"], 256 * 1024**2)
            self.assertEqual([self.store.job(j)["state"] for j in jobs],
                             ["queued"] * 3 if preempt else ["queued", "done", "done"])
            self.assertEqual(list(self.worker.cache.root.iterdir()), [])
            self.store.cancel(jobs[0])

    def test_wider_video_transfer_preserves_native_width_and_cache_budget(self):
        downloads = self.worker.analysis_downloads
        downloads.paired_videos = True
        downloads.maximum_parallel_videos = 6
        downloads.maximum_paired_video_bytes = 256 * 1024**2
        for start, width in enumerate((6, 4)):
            jobs = self.enqueue(6, kind="video", start=start * 6)
            with self.subTest(width=width), \
                    patch.object(self.worker.cache, "budget",
                                 return_value=ANALYSIS_RESERVE + width * downloads.maximum_paired_video_bytes), \
                    patch.object(downloads, "fetch_parallel", side_effect=self.files_ready) as fetch, \
                    patch("frameatlas.lookahead.prepare_short_video", return_value=True), \
                    patch("frameatlas.scene_batch.describe") as describe, \
                    patch.object(self.worker, "analyze_source"), \
                    Lookahead(self.worker, Native) as lookahead:
                lookahead.start()
                self.assertTrue(lookahead.consume())
                self.assertEqual(len(fetch.call_args_list[0].args[1]), width)
                self.assertEqual([[m.jid for m in call.args[1]] for call in describe.call_args_list],
                                 [jobs[:3], jobs[3:width]])
            self.assertTrue(all(self.store.job(j)["state"] == "done" for j in jobs[:width]))
            self.assertTrue(all(self.store.job(j)["state"] == "queued" for j in jobs[width:]))
            self.assertEqual(list(self.worker.cache.root.iterdir()), [])
