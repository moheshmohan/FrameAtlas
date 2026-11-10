from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave

from frameatlas.native import Native, TICKS
from frameatlas.speech import transcribe
from frameatlas.store import Store
from frameatlas.worker import Worker


class SpeechWindowTests(unittest.TestCase):
    def test_missing_speech_model_is_a_failure_even_if_its_name_mentions_audio(self):
        native = object.__new__(Native)
        native.cancelled = lambda: False
        for message, status in (
            ("unknown speech model `no audio`", "failed"),
            ("the speech model `selected` is not downloaded", "failed"),
            ("media.understandSpeech: none of the clips has audio", "no_audio"),
            ("Speech window: no speech", "no_speech"),
            ("invalid parameters for `transcript.inspect`: item 21 has no transcript (transcript.generate)", "failed"),
        ):
            with self.subTest(message=message), patch(
                "frameatlas.speech.transcribe", side_effect=RuntimeError(message)
            ):
                speech, actual, error = native.read_speech(1, {}, "selected", "auto", lambda _: None)
                self.assertEqual(speech, {})
                self.assertEqual(actual, status)
                self.assertEqual(error, message if status == "failed" else "")

    def test_large_audio_uses_bounded_windows_and_source_word_times(self):
        with TemporaryDirectory() as folder:
            work = Path(folder)

            class Native:
                cancelled = staticmethod(lambda: False)

                def __init__(self):
                    self.work = work
                    self.bounds = []
                    self.removed = []

                def run(self, command, params):
                    if command == 'clip.makeSubclip':
                        self.bounds.append((params['start'], params['end']))
                        return {'item': 2}
                    if command == 'clip.extractAudio':
                        self.assert_previous_window_removed(params)
                        path = Path(params['dir']) / 'test.wav'
                        start, end = self.bounds[-1]
                        with wave.open(str(path), 'wb') as pcm:
                            pcm.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                            pcm.writeframes(bytes((end - start) * 16000 // TICKS * 2))
                        return {'extracted': [{'item': 3, 'path': str(path)}]}
                    if command == 'media.understandSpeech':
                        if params['items'] != [3]:
                            raise AssertionError('Oversized original passed directly to the recognizer')
                        return {}
                    if command == 'transcript.inspect':
                        return {'language': 'en', 'words': [{'text': 'test', 'start': 0, 'end': TICKS // 2}]}
                    if command == 'project.select':
                        return {}
                    if command == 'project.delete':
                        self.removed.append(params['items'])
                        return {'items': 2, 'offline': 0}
                    raise AssertionError(command)

                def assert_previous_window_removed(self, params):
                    if list(work.glob('speech-*')) != [Path(params['dir'])]:
                        raise AssertionError('Previous speech window is still on disk')

            native = Native()
            probe = {'duration': 5 * TICKS, 'audio_streams': [{'sample_rate': 16000, 'channels': 1}]}
            with patch('frameatlas.speech.DIRECT_BYTES', 64_000), patch('frameatlas.speech.WINDOW_BYTES', 128_000):
                result = transcribe(native, 1, probe, 'whisper-base', 'auto', lambda _: None)
            self.assertEqual(native.bounds, [(0, 2*TICKS), (2*TICKS, 4*TICKS), (4*TICKS, 5*TICKS)])
            self.assertEqual(native.removed, [[2, 3], [2, 3], [2, 3]])
            self.assertEqual([w['start'] for w in result['words']], [0., 2., 4.])
            self.assertEqual([w['end'] for w in result['words']], [0.5, 2.5, 4.5])
            self.assertEqual(list(work.iterdir()), [])

    def test_multipart_retry_indexes_only_the_failed_part_and_retains_successful_words(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / "catalogue")
            prefix = "VID_20250820_114517_00_"
            sources = [root / (prefix + suffix + ".insv") for suffix in ("049", "050")]
            for path in sources:
                path.write_bytes(b"camera fixture")
            probe = {"duration": 8*TICKS, "video": {"width": 32}, "audio_streams": [{"sample_rate": 16000}]}
            aid, _ = store.upsert({"provider": "insta360", "source_id": "split",
                                   "filename": prefix + "049_050.insv", "kind": "video",
                                   "local_path": str(sources[0]), "locator": {
                                       "recording_parts": [{"path": str(path)} for path in sources]}})
            parts = [{"index": index, "filename": path.name, "offset": 8 * index, "duration": 8,
                      "bytes": path.stat().st_size, "analysis": {"probe": probe, "speech_status": status,
                      "speech_model": "whisper-base", "speech_error": "decode cap" if index == 0 else ""}}
                     for index, (path, status) in enumerate(zip(sources, ("failed", "complete")))]
            retained = {"start": 9, "end": 10, "text": "market", "source_part": {
                "index": 1, "filename": sources[1].name, "start": 1, "end": 2, "offset": 8}}
            old_speech = {"text": "market", "words": [retained], "language": "en", "time_unit": "seconds"}
            store.analysis(aid, [{"start": 0, "end": 8, "description": {"caption": "Street"}}], old_speech,
                           {"source_bytes": 2*sources[0].stat().st_size,
                            "probe": {"duration": 16*TICKS}, "parts": parts, "speech_status": "failed"})
            before = store.get(aid)
            worker = Worker(store)
            try:
                job = store.enqueue_speech(aid, "en")["id"]
                with patch("frameatlas.worker.Native") as native, patch("frameatlas.native.probe_media", return_value=probe), \
                        patch("frameatlas.speech.transcribe", side_effect=RuntimeError("decoder failed")):
                    native.return_value.__enter__.return_value.import_source.return_value = (1, {})
                    worker.run_job(store.next_job())
                self.assertEqual(store.job(job)["state"], "failed")
                self.assertEqual(store.get(aid), before)
                job = store.enqueue_speech(aid, "en")["id"]
                new = {"text": "harbour", "words": [{"text": "harbour", "start": 1, "end": 2}], "language": "en"}
                with patch("frameatlas.worker.Native") as native, patch("frameatlas.native.probe_media", return_value=probe), \
                        patch("frameatlas.speech.transcribe", return_value=new) as recognize:
                    session = native.return_value.__enter__.return_value
                    session.import_source.return_value = (1, {})
                    worker.run_job(store.next_job())
                    session.import_source.assert_called_once_with(sources[0].resolve())
                    self.assertEqual(recognize.call_count, 1)
                self.assertEqual(store.job(job)["state"], "done")
                after = store.get(aid)
                self.assertEqual(after["analysis"]["speech_status"], "complete")
                self.assertEqual(after["analysis"]["speech"]["words"][1], retained)
                self.assertEqual(after["analysis"]["parts"][1], parts[1])
                self.assertEqual(after["scenes"], before["scenes"])
                match = store.search(query="harbour", speech_only=True)["items"][0]["matches"][0]
                self.assertEqual(match["source_part"]["filename"], sources[0].name)
                self.assertEqual(match["start"], 1)
                self.assertEqual(list((store.root / "cache").iterdir()), [])
            finally:
                worker.close()

    def test_retry_transcript_indexes_words_without_replacing_edited_scenes(self):
        from frameatlas.fingerprint import digest_source
        with TemporaryDirectory() as folder:
            store = Store(Path(folder))
            source = Path(folder) / "clip.mp4"
            source.write_bytes(b"media fixture")
            aid, _ = store.upsert({"provider": "local", "source_id": "clip", "filename": source.name,
                                   "kind": "video", "local_path": str(source)})
            probe = {"duration": 8*TICKS, "video": {"width": 32}, "audio_streams": [{"sample_rate": 16000}]}
            store.analysis(aid, [{"start": 0, "end": 8, "description": {"caption": "Original scene"}}], {},
                           {"media_kind": "video", "speech_status": "failed", "speech_error": "Buffer limit",
                            "source_bytes": source.stat().st_size, "probe": probe,
                            "file_fingerprint": digest_source(source, lambda: False)}, "preview.jpg")
            store.correct_scene(aid, 0, "Edited scene")
            store.annotate(aid, tags=["manual"], summary="Edited overview")
            before = store.get(aid)
            ordinary = store.enqueue(aid, {"priority": True})
            retry = store.enqueue_speech(aid, "ml")
            self.assertNotEqual(ordinary["id"], retry["id"])
            self.assertEqual(store.enqueue_speech(aid, "ml")["id"], retry["id"])
            self.assertEqual(store.next_job()["id"], retry["id"])
            worker = Worker(store)
            word = {"text": "seaside", "start": 1.0, "end": 1.5}
            try:
                with patch("frameatlas.worker.Native") as native, patch(
                        "frameatlas.native.probe_media", return_value={**probe, "duration": 9*TICKS}):
                    worker.run_job(store.next_job())
                    native.return_value.__enter__.return_value.import_source.assert_not_called()
                failed = store.job(retry["id"])
                self.assertEqual(failed["state"], "failed")
                self.assertIn("differs from the indexed rendition", failed["error"])
                self.assertEqual(store.get(aid), before)
                source.write_bytes(b"media altered")
                retry = store.enqueue_speech(aid, "ml")
                with patch("frameatlas.worker.Native") as native, patch("frameatlas.native.probe_media", return_value=probe):
                    worker.run_job(store.next_job())
                    native.return_value.__enter__.return_value.import_source.assert_not_called()
                self.assertIn("file bytes differ", store.job(retry["id"])["error"])
                self.assertEqual(store.get(aid), before)
                source.write_bytes(b"media fixture")
                retry = store.enqueue_speech(aid, "ml")
                with patch("frameatlas.worker.Native") as native, patch("frameatlas.native.probe_media", return_value=probe), \
                        patch("frameatlas.speech.transcribe", return_value={"text": "seaside", "words": [word]}):
                    native.return_value.__enter__.return_value.import_source.return_value = (1, {})
                    worker.run_job(store.next_job())
            finally:
                worker.close()
            after = store.get(aid)
            self.assertEqual(store.job(retry["id"])["state"], "done")
            self.assertEqual(after["status"], "ready")
            self.assertEqual(after["analysis"]["speech_retry"]["indexed_source_verification"], "matched")
            self.assertEqual(after["scenes"], before["scenes"])
            self.assertEqual(after["summary"], before["summary"])
            self.assertEqual(after["thumbnail"], before["thumbnail"])
            self.assertEqual(after["tags"], ["manual"])
            matches = store.search(query="seaside")["items"][0]["matches"]
            self.assertEqual(matches[0]["start"], 1.0)
            self.assertEqual(list((Path(folder) / "cache").iterdir()), [])

            with self.assertRaisesRegex(ValueError, "requires a failed or empty transcript"):
                store.enqueue_speech(aid, "ml")
            with self.assertRaisesRegex(ValueError, "boolean"):
                store.enqueue_speech(aid, "ml", replace="true")
            replacement = store.enqueue_speech(aid, "ml", "whisper-medium-ml", replace=True)["id"]
            worker = Worker(store)
            try:
                with patch("frameatlas.worker.Native") as native, patch("frameatlas.native.probe_media", return_value=probe), \
                        patch("frameatlas.speech.transcribe", return_value={"text": "…", "words": [{**word, "text": "…"}]}) as recognize:
                    native.return_value.__enter__.return_value.import_source.return_value = (1, {})
                    worker.run_job(store.next_job())
                    self.assertIn("existing transcript was kept", store.job(replacement)["error"])
                    self.assertEqual(store.get(aid), after)
                    replacement = store.enqueue_speech(aid, "ml", "whisper-medium-ml", replace=True)["id"]
                    recognize.return_value = {"text": "harbour", "words": [{**word, "text": "harbour"}]}
                    worker.run_job(store.next_job())
            finally:
                worker.close()
            replaced = store.get(aid)
            self.assertEqual(store.job(replacement)["state"], "done")
            self.assertEqual(replaced["analysis"]["speech_model"], "whisper-medium-ml")
            self.assertEqual(replaced["scenes"], before["scenes"])
            self.assertEqual(replaced["summary"], before["summary"])
            self.assertEqual(replaced["thumbnail"], before["thumbnail"])
            self.assertEqual(replaced["tags"], ["manual"])
            self.assertEqual(store.search(query="seaside")["total"], 0)
            self.assertEqual(store.search(query="harbour")["items"][0]["matches"][0]["start"], 1.0)


if __name__ == '__main__':
    unittest.main()
