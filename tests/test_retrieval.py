import errno
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from frameatlas.store import LOCAL_ACQUISITION, Store
from frameatlas.retrieval import destination_folder, handoff, publish
from frameatlas.service import Service
from frameatlas.worker import Worker


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')
        self.dest = self.root / 'editing'; self.dest.mkdir()
        self.original = self.root / 'original.insv'; self.original.write_bytes(b'original')
        self.proxy = self.root / 'camera.lrv'; self.proxy.write_bytes(b'proxy')
        self.aid = self.store.upsert({'provider':'insta360','source_id':'recording','filename':'original.insv','kind':'video','local_path':str(self.original),'locator':{'proxy':str(self.proxy)},'metadata':{'is_360':True}})[0]
        self.probe = {'kind':'Movie','video':{'width':1920,'height':1080}}

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_360_retrieval_returns_original_and_dependencies_not_proxy(self):
        analysis = self.store.enqueue(self.aid)
        fetch = self.store.enqueue_retrieval(self.aid)
        self.assertNotEqual(fetch['id'], analysis['id'])
        with patch('frameatlas.retrieval.media_probe', return_value=self.probe):
            worker = Worker(self.store)
            try:
                worker.run_job(self.store.job(fetch['id']))
            finally:
                worker.close()
        job = self.store.job(fetch['id'])
        self.assertEqual(job['state'], 'done', job['error'])
        self.assertEqual(job['result']['path'], str(self.original.resolve()))
        self.assertEqual(job['result']['related_files'], [str(self.proxy.resolve())])
        self.assertEqual(self.original.read_bytes(), b'original')
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertEqual(self.store.job(analysis['id'])['state'], 'queued')

    def test_selected_cloud_file_survives_disposable_cache_cleanup(self):
        worker = Worker(self.store)
        try:
            with worker.cache.job() as work:
                source = work / 'download.mov'; source.write_bytes(b'owned temporary cloud source')
                with patch('frameatlas.retrieval.media_probe', return_value=self.probe):
                    result = handoff(self.store.get(self.aid), source, self.store.root, str(self.dest), temporary=True)
                output = Path(result['path'])
                self.assertTrue(output.is_file())
            self.assertEqual(worker.cache.usage(), 0)
            self.assertEqual(output.read_bytes(), b'owned temporary cloud source')
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        finally:
            worker.close()

    def test_no_overwrite_or_path_traversal(self):
        asset = self.store.get(self.aid)
        target = self.dest / 'known.mov'; target.write_bytes(b'important existing media')
        with patch('frameatlas.retrieval.filename', return_value='known.mov'):
            with self.assertRaisesRegex(ValueError, 'nothing was overwritten'):
                publish(self.original, self.dest, asset)
        self.assertEqual(target.read_bytes(), b'important existing media')
        (self.store.root / 'cache').mkdir(exist_ok=True)
        with self.assertRaises(ValueError):
            destination_folder(self.store.root, str(self.store.root / 'cache'))

    def test_reselection_during_download_keeps_new_locator(self):
        item = {'provider': 'google_photos', 'source_id': 'cloud-photo',
                'filename': 'clip.mov', 'kind': 'video'}
        aid = self.store.upsert({**item, 'locator': {'session_id': 'earlier'}})[0]
        job = self.store.enqueue_retrieval(aid, str(self.dest))
        worker = Worker(self.store)

        def source(asset, work, jid, for_analysis):
            self.store.upsert({**item, 'locator': {'session_id': 'refreshed'}})
            path = work / 'source.mov'
            path.write_bytes(b'cloud rendition')
            return path, 'temporary'

        try:
            with patch.object(worker, 'source', side_effect=source), \
                    patch('frameatlas.retrieval.media_probe', return_value=self.probe):
                worker.run_job(self.store.job(job['id']))
        finally:
            worker.close()
        receipt = self.store.job(job['id'])
        asset = self.store.get(aid)
        self.assertEqual(receipt['state'], 'done', receipt['error'])
        self.assertEqual(asset['locator']['session_id'], 'refreshed')
        self.assertEqual(asset['local_path'], receipt['result']['path'])
        self.assertEqual(Path(asset['local_path']).read_bytes(), b'cloud rendition')

    def test_retrieval_revalidates_spoken_reference_before_acquiring_source(self):
        service = Service(self.store)
        speech = {'text': 'hotel', 'words': [{'text': 'hotel', 'start': 3, 'end': 4}]}
        self.store.analysis(self.aid, [], speech, {'speech_status': 'complete'})
        hit = next(m for m in self.store.search(query='hotel')['items'][0]['matches'] if m['kind'] == 'speech')
        request = {'id': self.aid, 'speech_key': hit['speech_key']}
        job = service.dispatch('retrieve', request)
        self.assertEqual(service.dispatch('retrieve', request)['id'], job['id'])
        with self.assertRaisesRegex(ValueError, 'different clip reference'):
            service.dispatch('retrieve', {'id': self.aid})
        self.store.analysis(self.aid, [], {'text': 'hotel', 'words': [{'text': 'hotel', 'start': 3, 'end': 5}]},
                            {'speech_status': 'complete'})
        worker = Worker(self.store)
        try:
            with patch.object(worker, 'source') as source:
                worker.run_job(self.store.job(job['id']))
                source.assert_not_called()
            receipt = self.store.job(job['id'])
            self.assertEqual(receipt['state'], 'failed')
            self.assertIn('transcript match is stale', receipt['error'])
            plain = service.dispatch('retrieve', {'id': self.aid})
            with patch('frameatlas.retrieval.media_probe', return_value=self.probe):
                worker.run_job(self.store.job(plain['id']))
            self.assertEqual(self.store.job(plain['id'])['state'], 'done')
            self.assertIsNone(self.store.job(job['id'])['recovered_by'])
        finally:
            worker.close()

    def test_saved_handoff_remains_ready_if_temporary_cleanup_fails(self):
        job = self.store.enqueue_retrieval(self.aid)
        worker = Worker(self.store)
        try:
            with patch.object(worker.cache, 'remove', side_effect=ValueError('Temporary volume is busy')), \
                    patch('frameatlas.retrieval.media_probe', return_value=self.probe):
                worker.run_job(self.store.job(job['id']))
        finally:
            worker.close()
        receipt = self.store.job(job['id'])
        self.assertEqual(receipt['state'], 'done')
        self.assertIn('cleanup needs attention', receipt['stage'])
        self.assertIn('Temporary volume is busy', receipt['error'])
        self.assertEqual(receipt['result']['path'], str(self.original.resolve()))

    def test_timed_retrieval_rejects_same_size_different_bytes_before_publish(self):
        from frameatlas.fingerprint import digest_source
        aid = self.store.upsert({'provider': 'google_photos', 'source_id': 'clip',
                                'filename': 'clip.mov', 'kind': 'video', 'locator': {'session_id': 'selected'}})[0]
        evidence = digest_source(self.original, lambda: False)
        self.store.analysis(aid, [{'start': 1, 'end': 2, 'description': {'caption': 'A street'}}], {},
                            {'file_fingerprint': evidence})
        request = {'id': aid, 'scene_key': self.store.get(aid)['scenes'][0]['scene_key'],
                   'destination': str(self.dest)}
        service = Service(self.store)
        worker = Worker(self.store)
        data = b'modified'

        def source(asset, work, jid, for_analysis):
            path = work / 'source.mov'
            path.write_bytes(data)
            return path, 'temporary'

        try:
            with patch.object(worker, 'source', side_effect=source), \
                    patch('frameatlas.retrieval.media_probe', return_value=self.probe):
                failed = service.dispatch('retrieve', request)['id']
                worker.run_job(self.store.job(failed))
                self.assertEqual(self.store.job(failed)['state'], 'failed')
                self.assertIn('file bytes differ', self.store.job(failed)['error'])
                self.assertEqual(list(self.dest.iterdir()), [])
                self.assertFalse(self.store.get(aid)['local_path'])
                self.assertEqual(list(worker.cache.root.iterdir()), [])

                whole = service.dispatch('retrieve', {'id': aid, 'destination': str(self.dest)})['id']
                worker.run_job(self.store.job(whole))
                receipt = self.store.job(whole)
                self.assertEqual(receipt['state'], 'done', receipt['error'])
                self.assertEqual(receipt['result']['indexed_source_verification'], 'different_bytes')
                self.assertEqual(Path(receipt['result']['path']).read_bytes(), data)
                self.assertNotIn('clip', receipt['result'])
                self.assertFalse(self.store.get(aid)['local_path'])
                self.assertEqual(list(worker.cache.root.iterdir()), [])

                data = self.original.read_bytes()
                succeeded = service.dispatch('retrieve', request)['id']
                worker.run_job(self.store.job(succeeded))
            receipt = self.store.job(succeeded)
            self.assertEqual(receipt['state'], 'done', receipt['error'])
            self.assertEqual(receipt['result']['indexed_source_verification'], 'matched')
            self.assertEqual(Path(receipt['result']['clip']['source_path']).read_bytes(), data)
        finally:
            worker.close()

    def test_legacy_timed_retrieval_checks_known_properties_before_publishing(self):
        aid = self.store.upsert({'provider': 'google_photos', 'source_id': 'legacy',
                                 'filename': 'legacy.mov', 'kind': 'video',
                                 'locator': {'session_id': 'selected'}})[0]
        data = b'original'
        recorded = {**self.probe, 'duration': 2540160000000}
        self.store.analysis(aid, [{'start': 1, 'end': 2, 'description': {'caption': 'A street'}}], {},
                            {'source_bytes': len(data), 'probe': recorded})
        request = {'id': aid, 'scene_key': self.store.get(aid)['scenes'][0]['scene_key'],
                   'destination': str(self.dest)}
        service = Service(self.store)
        worker = Worker(self.store)

        def source(_asset, work, _jid, for_analysis):
            path = work / 'source.mov'
            path.write_bytes(data)
            return path, 'temporary'

        try:
            with patch.object(worker, 'source', side_effect=source), \
                    patch('frameatlas.retrieval.media_probe') as probe:
                for mismatch, content, media in (
                        ('file size', b'truncated', recorded),
                        ('duration', b'original', {**recorded, 'duration': 1000000000000}),
                        ('frame dimensions', b'original', {**recorded, 'video': {'width': 1280, 'height': 720}})):
                    with self.subTest(mismatch=mismatch):
                        data, probe.return_value = content, media
                        jid = service.dispatch('retrieve', request)['id']
                        worker.run_job(self.store.job(jid))
                        self.assertEqual(self.store.job(jid)['state'], 'failed')
                        self.assertIn(mismatch + ' differs', self.store.job(jid)['error'])
                        self.assertEqual(list(self.dest.iterdir()), [])
                        self.assertEqual(list(worker.cache.root.iterdir()), [])
                data, probe.return_value = b'original', recorded
                jid = service.dispatch('retrieve', request)['id']
                worker.run_job(self.store.job(jid))
            receipt = self.store.job(jid)
            self.assertEqual(receipt['state'], 'done', receipt['error'])
            self.assertEqual(receipt['result']['indexed_source_verification'], 'no_fingerprint')
            self.assertEqual(receipt['result']['indexed_source_properties_checked'],
                             ['file size', 'media kind', 'duration', 'frame dimensions'])
            self.assertEqual(Path(receipt['result']['clip']['source_path']).read_bytes(), b'original')
            self.assertNotIn('file_fingerprint', self.store.get(aid)['analysis'])
        finally:
            worker.close()

    def test_timed_google_handoff_recovers_a_mismatched_local_link(self):
        from frameatlas.fingerprint import digest_source

        aid = self.store.upsert({'provider': 'google_photos', 'source_id': 'stale-link',
                                 'filename': 'edit.mov', 'kind': 'video',
                                 'local_path': str(self.original),
                                 'locator': {'session_id': 'selected'}})[0]
        evidence = digest_source(self.original, lambda: False)
        self.original.write_bytes(b'modified')
        self.store.analysis(aid, [{'start': 1, 'end': 2, 'description': {'caption': 'A street'}}], {},
                            {'file_fingerprint': evidence})
        request = {'id': aid, 'scene_key': self.store.get(aid)['scenes'][0]['scene_key']}
        service = Service(self.store)
        worker = Worker(self.store)
        cloud_data = b'modified'

        def source(_asset, work, _jid, prefer_cloud=False, for_analysis=True):
            self.assertFalse(for_analysis)
            if not prefer_cloud:
                return self.original, LOCAL_ACQUISITION
            path = work / 'source.mov'
            path.write_bytes(cloud_data)
            return path, 'temporary'

        def retrieve(args):
            jid = service.dispatch('retrieve', args)['id']
            worker.run_job(self.store.job(jid))
            return self.store.job(jid)

        try:
            with patch.object(worker, 'source', side_effect=source) as acquire, \
                    patch('frameatlas.retrieval.media_probe', return_value=self.probe):
                self.assertIn('Supply an editing folder', retrieve(request)['error'])
                self.assertEqual(acquire.call_count, 1)
                request['destination'] = str(self.dest)
                with patch.object(self.store, 'picker_access', return_value={'state': 'expired'}):
                    self.assertIn('Refresh the Google Photos selection', retrieve(request)['error'])
                self.assertEqual(acquire.call_count, 2)
                self.assertIn('Google Photos copy also differs', retrieve(request)['error'])
                self.assertEqual(list(self.dest.iterdir()), [])
                self.assertEqual(self.store.get(aid)['local_path'], str(self.original))
                self.assertEqual(list(worker.cache.root.iterdir()), [])
                cloud_data = b'original'
                receipt = retrieve(request)
            self.assertEqual(receipt['state'], 'done', receipt['error'])
            result = receipt['result']
            self.assertTrue(result['local_source_mismatch_recovered'])
            self.assertEqual(result['indexed_source_verification'], 'matched')
            self.assertEqual(result['path'], self.store.get(aid)['local_path'])
            self.assertEqual(result['clip']['source_path'], result['path'])
            self.assertEqual(Path(result['path']).read_bytes(), b'original')
            self.assertEqual(self.original.read_bytes(), b'modified')
            self.assertEqual(list(worker.cache.root.iterdir()), [])
        finally:
            worker.close()

    def test_editor_handoff_publishes_cloud_file_if_local_link_disappears_mid_request(self):
        aid, _ = self.store.upsert({'provider': 'google_photos', 'source_id': 'vanishing-link',
                                   'filename': 'edit.mov', 'kind': 'video', 'local_path': str(self.original),
                                   'locator': {'session_id': 'selected'}})
        worker = Worker(self.store)
        actual_source = worker.source

        def remove_then_resolve(*args, **kwargs):
            self.original.unlink()
            return actual_source(*args, **kwargs)

        def download(_root, _action, _session, _source_id, destination, *_args, **_kwargs):
            destination.write_bytes(b'cloud original')

        try:
            jid = self.store.enqueue_retrieval(aid, str(self.dest))['id']
            with patch.object(worker, 'source', side_effect=remove_then_resolve), \
                 patch('frameatlas.worker.broker', side_effect=download), \
                 patch('frameatlas.retrieval.media_probe', return_value=self.probe):
                worker.run_job(self.store.job(jid))
            job = self.store.job(jid)
            self.assertEqual(job['state'], 'done', job['error'])
            path = Path(job['result']['path'])
            self.assertEqual(path.parent, self.dest.resolve())
            self.assertEqual(path.read_bytes(), b'cloud original')
            self.assertFalse(job['result']['existing_original_reused'])
            self.assertEqual(list(worker.cache.root.iterdir()), [])
        finally:
            worker.close()

    def test_cancelled_retrieval_never_publishes(self):
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            publish(self.original, self.dest, self.store.get(self.aid), lambda: True)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_cross_volume_publish_failure_cleans_only_owned_staging_file(self):
        target = self.dest / 'important.mov'; target.write_bytes(b'keep me')
        with patch('frameatlas.retrieval.os.link', side_effect=[OSError(errno.EXDEV, 'cross device'), OSError(errno.EPERM, 'unsupported')]):
            with self.assertRaises(OSError):
                publish(self.original, self.dest, self.store.get(self.aid))
        self.assertEqual([p.name for p in self.dest.iterdir()], ['important.mov'])
        self.assertEqual(target.read_bytes(), b'keep me')

    def test_retrieval_is_idempotent_and_does_not_displace_analysis(self):
        first = self.store.enqueue_retrieval(self.aid, str(self.dest))
        again = self.store.enqueue_retrieval(self.aid, str(self.dest))
        self.assertEqual(first['id'], again['id'])
        with self.assertRaises(ValueError):
            self.store.enqueue_retrieval(self.aid, '')
        self.store.enqueue(self.aid)
        self.assertEqual(self.store.queue_state()['counts']['queued'], 2)
        self.assertEqual(self.store.next_job()['id'], first['id'])

    def test_cancelled_queued_job_cannot_be_claimed_again(self):
        worker = Worker(self.store)
        try:
            job = self.store.enqueue_retrieval(self.aid)
            snapshot = self.store.job(job['id'])
            self.store.cancel(job['id'])
            worker.run_job(snapshot)
            self.assertEqual(self.store.job(job['id'])['state'], 'cancelled')
        finally:
            worker.close()

    def test_malformed_job_reports_failure_instead_of_stalling_queue(self):
        worker = Worker(self.store)
        try:
            job = self.store.enqueue_retrieval(self.aid)
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET options='invalid json' WHERE id=?", (job['id'],))
            worker.run_job(self.store.job(job['id']))
            self.assertEqual(self.store.job(job['id'])['state'], 'failed')
        finally:
            worker.close()

    def test_old_database_migrates_without_losing_queued_work(self):
        import sqlite3
        folder = self.root / 'old'; folder.mkdir()
        with sqlite3.connect(folder / 'catalog.sqlite3') as db:
            db.executescript("CREATE TABLE jobs(id INTEGER PRIMARY KEY,asset_id TEXT,state TEXT,stage TEXT,error TEXT,options TEXT,created REAL,updated REAL); INSERT INTO jobs VALUES(1,'a','queued','','','{}',0,0); CREATE UNIQUE INDEX one_active_job ON jobs(asset_id) WHERE state IN ('queued','running','cancelling');")
        migrated = Store(folder)
        with migrated.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=1').fetchone()
            self.assertEqual(row['state'], 'queued')
            self.assertEqual(row['task'], 'analysis')
            self.assertEqual(json.loads(row['result']), {})


if __name__ == '__main__':
    unittest.main()
