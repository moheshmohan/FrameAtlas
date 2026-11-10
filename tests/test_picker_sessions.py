import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from frameatlas.store import LOCAL_ACQUISITION, PICKER_ACQUISITION, Store
from frameatlas.service import Service
from frameatlas.mcp import handle
from frameatlas.picker_sessions import record, list_batches, session_id
from frameatlas.scene_batch import run_batch
from frameatlas.worker import Native, Worker

SID = 'a6c4f5d6-ffe1-4e31-9245-4220149baee3'
NEW = 'd1e1e926-5d7f-4adc-8612-89a50e3b4d50'


class PickerSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.service = Service(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_receipt_survives_restart_without_storing_picker_or_media_urls(self):
        response = {'id': SID, 'expires': '2030-01-01T00:00:00Z',
                    'picker_uri': 'https://photos.google.com/integration/picker/auth/private-handoff'}
        with patch('frameatlas.service.broker', return_value=response):
            self.assertEqual(self.service.dispatch('picker_create', {}), response)
        batch = list_batches(Store(self.store.root))['items'][0]
        self.assertEqual(batch['state'], 'pending')
        self.assertFalse(batch['expired'])
        self.assertTrue(batch['can_close'])
        self.assertNotIn('picker_uri', batch)
        with self.store.connect() as db:
            self.assertNotIn('private-handoff', '\n'.join(db.iterdump()))

    def test_reselection_moves_dependency_counts_and_protects_active_jobs(self):
        response = {'ready': True, 'items': [{'id': 'one', 'filename': 'one.jpg', 'kind': 'photo',
                                            'created': '2026-08-01T11:36:01.430Z'}]}
        record(self.store, SID, expires='2030-01-01T00:00:00Z')
        with patch('frameatlas.service.broker', return_value=response):
            imported = self.service.dispatch('picker_import', {'session': SID})
        aid = imported['ids'][0]
        self.store.enqueue(aid, {'language': 'ml', 'priority': True})
        batch = list_batches(self.store)['items'][0]
        self.assertEqual((batch['selected_count'], batch['added_count'], batch['pending_jobs']), (1, 1, 1))
        self.assertTrue(batch['captured_min'].startswith('2026-08-01T11:36:01.430'))
        self.assertEqual(batch['capture_months'], {'2026-08': 1})
        self.assertFalse(batch['can_close'])
        with patch('frameatlas.service.broker') as broker:
            with self.assertRaisesRegex(ValueError, 'Finish or cancel'):
                self.service.dispatch('picker_delete', {'session': SID})
            broker.assert_not_called()
        # A newer selection refreshes access without duplicating the job or asset.
        record(self.store, NEW, expires='2030-01-02T00:00:00Z')
        with patch('frameatlas.service.broker', return_value=response):
            newer = self.service.dispatch('picker_import', {'session': NEW})
        self.assertEqual(newer['ids'], [aid])
        batches = {b['id']: b for b in list_batches(self.store)['items']}
        self.assertEqual(batches[SID]['linked_assets'], 0)
        self.assertTrue(batches[SID]['can_close'])
        self.assertEqual(batches[NEW]['pending_jobs'], 1)
        # An older receipt can be checked again without shortening access.
        with patch('frameatlas.service.broker', return_value=response):
            older = self.service.dispatch('picker_import', {'session': SID, 'queue_analysis': True})
        self.assertEqual((older['ids'], older['added'], older['queued']), ([aid], 0, 0))
        self.assertEqual(self.store.get(aid)['locator']['session_id'], NEW)
        self.assertEqual(self.store.next_job()['picker_session'], NEW)
        with patch('frameatlas.service.broker', return_value={'ok': True}):
            self.service.dispatch('picker_delete', {'session': SID})
        self.assertEqual(self.store.get(aid)['locator']['session_id'], NEW)
        batches = {b['id']: b for b in list_batches(self.store)['items']}
        self.assertEqual(batches[SID]['state'], 'closed')
        self.assertEqual(batches[SID]['selected_count'], 1)
        self.assertEqual(batches[SID]['capture_months'], {'2026-08': 1})

    def test_queued_media_lists_distinct_cloud_sources_and_follows_reselection(self):
        record(self.store, SID, expires='2020-01-01T00:00:00Z', selected_count=4)
        record(self.store, NEW, expires='2030-01-01T00:00:00Z', selected_count=1)
        local = self.store.root / 'existing.jpg'
        local.write_bytes(b'existing')
        media = []
        for i, name in enumerate(('video', 'photo', 'local', 'cancelled')):
            item = {'provider': 'google_photos', 'source_id': name, 'filename': name,
                    'kind': 'video' if name == 'video' else 'photo',
                    'captured': f'2019-11-0{i+1}T05:00:00Z', 'locator': {'session_id': SID},
                    'local_path': str(local) if name == 'local' else ''}
            aid, _ = self.store.upsert(item)
            jid = self.store.enqueue(aid)['id']
            media.append((item, aid, jid))
        self.store.enqueue_retrieval(media[0][1])
        self.store.cancel(media[3][2])
        args = {'session': SID, 'limit': 1}
        response = handle(self.service, {'id': 1, 'method': 'tools/call',
                            'params': {'name': 'catalog_picker_queued_items', 'arguments': args}})
        self.assertFalse(response['result']['isError'])
        first = json.loads(response['result']['content'][0]['text'])
        self.assertEqual((first['total'], first['queued_jobs']), (2, 3))
        self.assertTrue(first['needs_reselection'])
        self.assertTrue(first['has_more'])
        self.assertEqual((first['items'][0]['id'], first['items'][0]['queued_jobs']), (media[0][1], 2))
        second = self.service.dispatch('picker_queued_items', {**args, 'offset': '1'})
        self.assertEqual([item['id'] for item in second['items']], [media[1][1]])
        self.assertFalse(second['has_more'])
        self.store.upsert({**media[0][0], 'locator': {'session_id': NEW}})
        current = self.service.dispatch('picker_queued_items', {**args, 'session': NEW})
        self.assertFalse(current['needs_reselection'])
        self.assertEqual([item['id'] for item in current['items']], [media[0][1]])
        self.assertEqual(self.service.dispatch('picker_queued_items', args)['total'], 1)
        for bad in ({'limit': True}, {'limit': 101}, {'offset': -1}, {'offset': '0; DROP'}, {'session': 'invalid'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.service.dispatch('picker_queued_items', {**args, **bad})

    def test_reselection_worklist_includes_missing_and_expired_sources(self):
        record(self.store, SID, expires='2020-01-01T00:00:00Z', selected_count=1)
        record(self.store, NEW, expires='2030-01-01T00:00:00Z', selected_count=1)
        def queue(name, locator=None):
            item = {'provider': 'google_photos', 'source_id': name, 'filename': name + '.mp4',
                    'kind': 'video', 'locator': locator or {}}
            aid = self.store.upsert(item)[0]
            return item, aid, self.store.enqueue(aid)['id']
        missing, aid, original = queue('missing')
        _, expired, _ = queue('expired', {'session_id': SID})
        _, available, _ = queue('available', {'session_id': NEW})
        self.store.enqueue_retrieval(expired)
        response = handle(self.service, {'id': 1, 'method': 'tools/call',
                          'params': {'name': 'catalog_picker_reselection', 'arguments': {'limit': 1}}})
        self.assertFalse(response['result']['isError'])
        first = json.loads(response['result']['content'][0]['text'])
        self.assertEqual((first['total'], first['queued_jobs']), (2, 3))
        self.assertTrue(first['has_more'])
        second = self.service.dispatch('picker_reselection', {'limit': '1', 'offset': '1'})
        found = {row['id']: row for row in first['items'] + second['items']}
        self.assertEqual((found[aid]['reason'], found[aid]['session']), ('missing', None))
        self.assertEqual((found[expired]['reason'], found[expired]['queued_jobs']), ('expired', 2))
        self.assertNotIn(available, found)
        self.assertEqual(self.store.queue_state(include_waiting=True)['waiting_for_selection'], first['queued_jobs'])
        self.store.upsert({**missing, 'locator': {'session_id': NEW}})
        self.assertEqual(self.store.job(original)['state'], 'queued')
        self.assertFalse(self.store.job(original)['waiting_for_selection'])
        self.assertEqual(self.service.dispatch('picker_reselection', {})['total'], 1)
        with self.assertRaises(ValueError):
            self.service.dispatch('picker_reselection', {'offset': '10000001'})

    def test_import_can_queue_only_unanalysed_selected_items_without_job_history(self):
        response = {'ready': True, 'items': [
            {'id': name, 'filename': name + '.jpg', 'kind': 'photo'}
            for name in ('new', 'priority', 'cancelled', 'ready')
        ]}
        with patch('frameatlas.service.broker', return_value=response) as cloud:
            imported = self.service.dispatch('picker_import', {'session': SID})
            self.assertEqual(imported['queued'], 0)
            self.assertEqual(self.store.jobs(), [])
            new, priority, cancelled, ready = imported['ids']
            existing = self.store.enqueue(priority, {'language': 'ml', 'priority': True})['id']
            cancelled_job = self.store.enqueue(cancelled)['id']
            self.store.cancel(cancelled_job)
            with self.store.connect() as db:
                db.execute("UPDATE assets SET status='ready' WHERE id=?", (ready,))

            def call(option):
                result = handle(self.service, {'id': 1, 'method': 'tools/call',
                                'params': {'name': 'catalog_picker_import',
                                           'arguments': {'session': NEW, 'queue_analysis': option}}})['result']
                return result

            self.assertTrue(call('true')['isError'])
            self.assertEqual(cloud.call_count, 1)
            result = call(True)
            self.assertFalse(result['isError'], result)
            self.assertEqual(json.loads(result['content'][0]['text'])['queued'], 1)
            self.assertEqual(json.loads(call(True)['content'][0]['text'])['queued'], 0)
        jobs = {job['asset_id']: job for job in self.store.jobs()}
        self.assertEqual(set(jobs), {new, priority, cancelled})
        self.assertEqual(jobs[new]['state'], 'queued')
        self.assertEqual(jobs[priority]['id'], existing)
        self.assertEqual(json.loads(jobs[priority]['options']), {'language': 'ml', 'priority': True})
        self.assertEqual(jobs[cancelled]['state'], 'cancelled')
        self.assertEqual(self.store.get(priority)['locator']['session_id'], NEW)

    def test_reimport_keeps_original_added_count_and_expiry(self):
        record(self.store, SID, expires='2000-01-01T00:00:00Z', selected_count=2, added_count=2,
               capture_months={'1999-03': 1, '1999-01': 1})
        record(self.store, SID, selected_count=2, added_count=0)
        result = list_batches(self.store)['items'][0]
        self.assertEqual(result['added_count'], 2)
        self.assertTrue(result['expired'])
        self.assertTrue(result['expires'].startswith('2000-01-01'))
        self.assertEqual(result['capture_months'], {'1999-01': 1, '1999-03': 1})

    def test_deadline_load_counts_only_queued_cloud_work_and_groups_equal_expiry(self):
        same = str(uuid.uuid4())
        expired = str(uuid.uuid4())
        for sid, expiry in ((SID, '2030-01-01T01:00:00Z'), (same, '2030-01-01T04:00:00+03:00'),
                            (NEW, '2030-01-01T02:00:00Z'), (expired, '2029-12-31T23:00:00Z')):
            record(self.store, sid, expires=expiry, selected_count=1)

        local = self.store.root / 'linked.jpg'
        local.touch()
        def enqueue(source, sid, path='', options=None, kind='photo'):
            aid, _ = self.store.upsert({'provider': 'google_photos', 'source_id': source,
                                       'filename': source, 'kind': kind,
                                       'locator': {'session_id': sid}, 'local_path': path})
            return self.store.enqueue(aid, options)['id']

        enqueue('first', SID)
        enqueue('second', SID)
        enqueue('third', SID)
        enqueue('linked', SID, str(local))
        enqueue('cloud-override', same, str(local), {'prefer_cloud': True}, kind='video')
        enqueue('later', NEW, kind='audio')
        enqueue('expired', expired, kind='video')
        completed = enqueue('completed', SID)
        older = enqueue('old-completion', SID)
        now = 1893456000  # 2030-01-01T00:00:00Z
        with self.store.connect() as db:
            db.executemany("UPDATE jobs SET state='done',updated=? WHERE id=?",
                           [(now-120, completed), (now-3600, older)])
        with patch('frameatlas.picker_sessions.time.time', return_value=now):
            result = list_batches(self.store)
        batches = {b['id']: b for b in result['items']}
        self.assertEqual(result['recent_google_analysis_completions'], 1)
        self.assertEqual(result['recent_google_unclassified_analysis_completions'], 1)
        self.assertEqual(result['recent_google_picker_analysis_by_kind'], {'photo': 0, 'video': 0, 'other': 0})
        self.assertEqual(batches[SID]['pending_jobs'], 4)
        self.assertEqual(batches[SID]['cloud_queued_jobs'], 3)
        self.assertEqual(batches[SID]['cloud_queued_jobs_by_kind'], {'photo': 3, 'video': 0, 'other': 0})
        self.assertEqual(batches[same]['cloud_queued_jobs_by_kind'], {'photo': 0, 'video': 1, 'other': 0})
        self.assertEqual(batches[SID]['deadline_load'], {
            'cloud_jobs_due': 4, 'cloud_jobs_due_by_kind': {'photo': 3, 'video': 1, 'other': 0},
            'minimum_jobs_per_hour': 4, 'minimum_jobs_per_hour_by_kind': {'photo': 3, 'video': 1, 'other': 0}})
        self.assertEqual(batches[same]['deadline_load'], batches[SID]['deadline_load'])
        self.assertEqual(batches[NEW]['deadline_load'], {
            'cloud_jobs_due': 5, 'cloud_jobs_due_by_kind': {'photo': 3, 'video': 1, 'other': 1},
            'minimum_jobs_per_hour': 3, 'minimum_jobs_per_hour_by_kind': {'photo': 2, 'video': 1, 'other': 1}})
        self.assertEqual(result['peak_minimum_jobs_per_hour_by_kind'], {'photo': 3, 'video': 1, 'other': 1})
        self.assertTrue(batches[expired]['expired'])
        self.assertIsNone(batches[expired]['deadline_load'])

    def test_completion_source_survives_reanalysis_and_local_path_changes(self):
        record(self.store, SID, expires='2099-01-01T00:00:00Z', selected_count=1)
        aid, _ = self.store.upsert({'provider': 'google_photos', 'source_id': 'one',
                                   'filename': 'one.jpg', 'kind': 'photo', 'locator': {'session_id': SID}})
        scenes = [{'start': 0, 'end': 0, 'description': {'caption': 'A tree'}}]

        def finish(acquisition):
            jid = self.store.enqueue(aid)['id']
            self.assertTrue(self.store.claim_job(jid))
            self.store.analysis(aid, scenes, {}, {'acquisition': acquisition}, job_id=jid)
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET state='done' WHERE id=?", (jid,))
            return jid

        cloud = finish(PICKER_ACQUISITION)
        local_path = self.store.root / 'one.jpg'
        local_path.touch()
        self.store.upsert({'provider': 'google_photos', 'source_id': 'one',
                           'filename': 'one.jpg', 'kind': 'photo', 'local_path': str(local_path)})
        local = finish(LOCAL_ACQUISITION)
        self.assertEqual(self.store.get(aid)['analysis']['acquisition'], LOCAL_ACQUISITION)
        self.assertEqual(self.store.job(cloud)['result']['analysis_source'], 'picker')
        self.assertEqual(self.store.job(local)['result']['analysis_source'], 'local')

        counts = list_batches(self.store)
        self.assertEqual(counts['recent_google_analysis_completions'], 2)
        self.assertEqual(counts['recent_google_picker_analysis_completions'], 1)
        self.assertEqual(counts['recent_google_picker_analysis_by_kind'], {'photo': 1, 'video': 0, 'other': 0})
        self.assertEqual(counts['recent_google_local_analysis_completions'], 1)
        self.assertEqual(counts['recent_google_unclassified_analysis_completions'], 0)

    def test_expired_jobs_keep_options_and_resume_after_reselection(self):
        item = {'id': 'one', 'filename': 'one.jpg', 'kind': 'photo'}
        response = {'ready': True, 'items': [item]}
        record(self.store, SID, expires='2000-01-01T00:00:00Z')
        with patch('frameatlas.service.broker', return_value=response):
            aid = self.service.dispatch('picker_import', {'session': SID})['ids'][0]
        plan = self.service.dispatch('resolve', {'id': aid})
        self.assertTrue(plan['needs_selection'])
        self.assertEqual(plan['google_selection']['state'], 'expired')
        opts = {'language': 'ml', 'priority': True}
        jid = self.store.enqueue(aid, opts)['id']
        retrieval = self.store.enqueue_retrieval(aid)['id']
        local, _ = self.store.upsert({'provider': 'local', 'source_id': 'other',
                                       'filename': 'other.jpg', 'kind': 'photo'})
        other = self.store.enqueue(local)['id']
        self.assertEqual(self.store.next_job()['id'], other)
        self.assertFalse(self.store.claim_job(jid))
        self.assertFalse(self.store.claim_job(retrieval))
        queue = self.service.dispatch('jobs', {})
        self.assertEqual(queue['counts']['queued'], 3)
        self.assertEqual(queue['waiting_for_selection'], 2)
        self.assertEqual(queue['items'][0]['id'], other)
        self.assertTrue(self.store.job(jid)['waiting_for_selection'])

        record(self.store, NEW, expires='2099-01-01T00:00:00Z')
        with patch('frameatlas.service.broker', return_value=response):
            self.assertEqual(self.service.dispatch('picker_import', {'session': NEW})['ids'], [aid])
        plan = self.service.dispatch('resolve', {'id': aid})
        self.assertFalse(plan['needs_selection'])
        self.assertEqual(plan['google_selection']['state'], 'selected')
        self.assertEqual(self.store.next_job()['id'], retrieval)
        self.assertTrue(self.store.claim_job(retrieval))
        self.assertEqual(self.store.next_job()['id'], jid)
        self.assertEqual(json.loads(self.store.job(jid)['options']), opts)
        self.assertEqual(self.service.dispatch('jobs', {})['waiting_for_selection'], 0)

    def test_transfer_failure_at_expiry_waits_with_same_job_and_cleans_partial_file(self):
        editing = tempfile.TemporaryDirectory()
        self.addCleanup(editing.cleanup)
        for mode in ('single', 'batch', 'retrieve', 'refreshed', 'unrelated'):
            with self.subTest(mode=mode):
                record(self.store, SID, expires='2099-01-01T00:00:00Z', selected_count=1)
                aid, _ = self.store.upsert({'provider': 'google_photos', 'source_id': mode,
                                             'filename': mode + '.jpg', 'kind': 'photo',
                                             'locator': {'session_id': SID}})
                opts = {'priority': True, 'fresh': True}
                jid = (self.store.enqueue_retrieval(aid, editing.name) if mode == 'retrieve'
                       else self.store.enqueue(aid, opts))['id']
                original = self.store.job(jid)['options']
                job = self.store.next_job()
                self.assertEqual(job['id'], jid)

                def transfer(_session, _source, path, *_args, **_kwargs):
                    path.write_bytes(b'partial source')
                    if mode != 'unrelated':
                        record(self.store, SID, expires='2000-01-01T00:00:00Z')
                    if mode == 'refreshed':
                        record(self.store, NEW, expires='2099-01-01T00:00:00Z', selected_count=1)
                        self.store.upsert({'provider': 'google_photos', 'source_id': mode,
                                           'filename': mode + '.jpg', 'kind': 'photo',
                                           'locator': {'session_id': NEW}})
                    raise ValueError('Transfer failed')

                worker = Worker(self.store)
                try:
                    with patch.object(worker.analysis_downloads, 'fetch', side_effect=transfer), \
                         patch('frameatlas.worker.broker', side_effect=lambda _root, _action, *args, **kw: transfer(*args, **kw)):
                        if mode == 'batch':
                            run_batch(worker, [job], Native)
                        else:
                            worker.run_job(job)
                    actual = self.store.job(jid)
                    self.assertEqual(actual['options'], original)
                    self.assertEqual(actual['state'], 'failed' if mode == 'unrelated' else 'queued')
                    self.assertEqual(actual['waiting_for_selection'], mode in ('single', 'batch', 'retrieve'))
                    self.assertEqual(list(worker.cache.root.iterdir()), [])
                    if mode == 'refreshed':
                        self.assertEqual(self.store.next_job()['id'], jid)
                    elif actual['waiting_for_selection']:
                        self.assertIsNone(self.store.next_job())
                        self.assertEqual(self.service.dispatch('picker_reselection', {})['items'][0]['id'], aid)
                    if actual['state'] == 'queued':
                        self.store.cancel(jid)
                finally:
                    worker.close()

    def test_closed_selection_allows_local_reuse_and_defers_explicit_cloud(self):
        record(self.store, SID, closed=True)
        local = self.store.root / 'attached.jpg'
        local.touch()
        aid, _ = self.store.upsert({'provider': 'google_photos', 'source_id': 'attached',
                                    'filename': local.name, 'kind': 'photo', 'local_path': str(local),
                                    'locator': {'session_id': SID}})
        cloud = self.store.enqueue(aid, {'prefer_cloud': True})['id']
        retrieval = self.store.enqueue_retrieval(aid)['id']
        self.assertTrue(self.store.job(cloud)['waiting_for_selection'])
        self.assertEqual(self.store.next_job()['id'], retrieval)
        self.assertTrue(self.store.claim_job(retrieval))
        self.store.cancel(cloud)
        analysis = self.store.enqueue(aid)['id']
        self.assertEqual(self.store.next_job()['id'], analysis)
        self.assertTrue(self.store.claim_job(analysis))
        plan = self.service.dispatch('resolve', {'id': aid})
        self.assertTrue(plan['existing_local_available'])
        self.assertFalse(plan['needs_selection'])
        self.assertEqual(plan['google_selection']['state'], 'closed')
        local.unlink()
        plan = self.service.dispatch('resolve', {'id': aid})
        self.assertFalse(plan['existing_local_available'])
        self.assertTrue(plan['needs_selection'])

    def test_queued_jobs_follow_local_file_availability_without_erasing_link(self):
        local = self.store.root / 'reattachable.jpg'
        local.write_bytes(b'local')
        record(self.store, SID, expires='2000-01-01T00:00:00Z', selected_count=1)
        aid, _ = self.store.upsert({'provider': 'google_photos', 'source_id': 'reattachable',
                                   'filename': local.name, 'kind': 'photo', 'local_path': str(local),
                                   'locator': {'session_id': SID}})
        jid = self.store.enqueue(aid, {'priority': True})['id']
        self.assertEqual(self.store.next_job()['id'], jid)
        local.unlink()
        self.assertIsNone(self.store.next_job())
        self.assertTrue(self.store.job(jid)['waiting_for_selection'])
        self.assertEqual(self.service.dispatch('picker_reselection', {})['items'][0]['id'], aid)
        self.assertEqual(self.store.get(aid)['local_path'], str(local))
        local.write_bytes(b'local')
        self.assertEqual(self.store.next_job()['id'], jid)
        self.assertFalse(self.store.job(jid)['waiting_for_selection'])

    def test_resolve_distinguishes_missing_pending_and_legacy_google_access(self):
        item = {'provider': 'google_photos', 'source_id': 'legacy', 'filename': 'old clip.mov', 'kind': 'video'}
        aid, _ = self.store.upsert(item)
        plan = self.service.dispatch('resolve', {'id': aid})
        self.assertTrue(plan['needs_selection'])
        self.assertEqual(plan['google_selection']['state'], 'missing')
        self.assertTrue(plan['search_url'].endswith('/old%20clip.mov'))
        self.store.upsert({**item, 'locator': {'session_id': SID}})
        plan = self.service.dispatch('resolve', {'id': aid})
        self.assertFalse(plan['needs_selection'])
        self.assertEqual(plan['google_selection']['state'], 'unknown')
        record(self.store, SID, expires='2099-01-01T00:00:00Z')
        plan = self.service.dispatch('resolve', {'id': aid})
        self.assertTrue(plan['needs_selection'])
        self.assertEqual(plan['google_selection']['state'], 'pending')

    def test_missing_and_pending_selections_keep_queued_jobs_until_import(self):
        item = {'provider': 'google_photos', 'source_id': 'archived', 'filename': 'archived.mov', 'kind': 'video'}
        aid, _ = self.store.upsert(item)
        options = {'language': 'ml', 'interactive': True}
        analysis = self.store.enqueue(aid, options)['id']
        editor = tempfile.TemporaryDirectory()
        self.addCleanup(editor.cleanup)
        retrieval = self.store.enqueue_retrieval(aid, editor.name)['id']

        self.assertEqual(self.service.dispatch('jobs', {})['waiting_for_selection'], 2)
        self.assertFalse(self.store.claim_job(analysis))
        self.assertFalse(self.store.claim_job(retrieval))
        self.assertIsNone(self.store.next_job())

        self.store.upsert({**item, 'locator': {'session_id': SID}})
        self.assertFalse(self.store.job(analysis)['waiting_for_selection'])
        record(self.store, SID, expires='2099-01-01T00:00:00Z')
        self.assertTrue(self.store.job(analysis)['waiting_for_selection'])
        self.assertTrue(self.store.job(retrieval)['waiting_for_selection'])
        self.assertIsNone(self.store.next_job())

        response = {'ready': True, 'items': [{'id': item['source_id'], 'filename': item['filename'], 'kind': item['kind']}]}
        with patch('frameatlas.service.broker', return_value=response):
            imported = self.service.dispatch('picker_import', {'session': SID, 'queue_analysis': True})
        self.assertEqual(imported['ids'], [aid])
        self.assertEqual(imported['queued'], 0)
        self.assertEqual(self.service.dispatch('jobs', {})['waiting_for_selection'], 0)
        self.assertEqual(self.store.next_job()['id'], retrieval)
        self.assertTrue(self.store.claim_job(retrieval))
        self.assertEqual(self.store.next_job()['id'], analysis)
        self.assertEqual(json.loads(self.store.job(analysis)['options']), options)

    def test_soonest_cloud_deadline_runs_first_within_the_same_analysis_priority(self):
        record(self.store, SID, expires='2099-01-03T00:00:00Z', selected_count=1)
        record(self.store, NEW, expires='2099-01-02T00:00:00Z', selected_count=1)
        local, _ = self.store.upsert({'provider': 'local', 'source_id': 'local',
                                       'filename': 'local.jpg', 'kind': 'photo'})
        later, _ = self.store.upsert({'provider': 'google_photos', 'source_id': 'later',
                                       'filename': 'later.jpg', 'kind': 'photo', 'locator': {'session_id': SID}})
        earlier, _ = self.store.upsert({'provider': 'google_photos', 'source_id': 'earlier',
                                         'filename': 'earlier.jpg', 'kind': 'photo', 'locator': {'session_id': NEW}})
        jobs = [self.store.enqueue(aid)['id'] for aid in (local, later, earlier)]
        self.assertEqual(self.store.next_job()['id'], jobs[2])
        self.assertEqual([j['id'] for j in self.store.jobs()], jobs[::-1])
        self.store.enqueue(local, {'priority': True})
        self.assertEqual(self.store.next_job()['id'], jobs[0])
        self.store.enqueue(later, {'priority': True})
        self.assertEqual(self.store.next_job()['id'], jobs[1])
        self.store.enqueue(earlier, {'priority': True})
        self.assertEqual(self.store.next_job()['id'], jobs[2])
        self.assertEqual(self.store.enqueue(later, {'interactive': True}), {'id': jobs[1], 'existing': True})
        self.assertEqual(self.store.next_job()['id'], jobs[1])
        self.assertEqual([job['id'] for job in self.store.jobs()], [jobs[1], jobs[2], jobs[0]])
        self.assertEqual(json.loads(self.store.job(jobs[1])['options']), {'priority': True, 'interactive': True})
        retrieval = self.store.enqueue_retrieval(later)
        self.assertEqual(self.store.next_job()['id'], retrieval['id'])

    def test_interactive_request_limit_rejects_bulk_and_preserves_existing_settings(self):
        ids = [self.store.upsert({'provider': 'google_photos', 'source_id': f'photo-{i}',
                                 'filename': f'photo-{i}.jpg', 'kind': 'photo'})[0] for i in range(9)]
        with self.assertRaisesRegex(ValueError, 'at most 8'):
            self.service.dispatch('enqueue', {'ids': ids, 'options': {'interactive': True}})
        self.assertEqual(self.store.jobs(), [])
        job = self.store.enqueue(ids[0], {'language': 'ml', 'detail': 'full'})
        result = self.service.dispatch('enqueue', {'ids': [ids[0]], 'options': {'interactive': True}})
        self.assertEqual(result, {'jobs': [{**job, 'existing': True}]})
        self.assertEqual(json.loads(self.store.job(job['id'])['options']),
                         {'language': 'ml', 'detail': 'full', 'interactive': True})

    def test_hostile_ids_counts_and_dates_leave_receipts_unchanged(self):
        for bad in ('', 'x', SID.upper(), 1, None, 'https://example.com/' + SID):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                session_id(bad)
        for kwargs in ({'selected_count': True}, {'selected_count': 2001}, {'added_count': 1},
                       {'expires': 'https://example.com/token'}, {'expires': '2026-01-01'},
                       {'captured_min': '2030-01-01T00:00:00Z', 'captured_max': '2020-01-01T00:00:00Z'},
                       {'selected_count': 1, 'capture_months': {'2024-02': 2}},
                       {'selected_count': 1, 'capture_months': {'2024-13': 1}},
                       {'selected_count': 1, 'capture_months': {'2024-01': True}},
                       {'closed': 'yes'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                record(self.store, SID, **kwargs)
        self.assertEqual(list_batches(self.store)['total'], 0)

    def test_closed_provider_session_is_still_recorded_and_mcp_lists_it(self):
        record(self.store, SID)
        with patch('frameatlas.service.broker', side_effect=ValueError('HTTP 404')):
            result = self.service.dispatch('picker_delete', {'session': SID})
        self.assertTrue(result['already_closed'])
        reply = handle(self.service, {'id': 1, 'method': 'tools/call',
                                     'params': {'name': 'catalog_picker_sessions', 'arguments': {}}})
        self.assertFalse(reply['result']['isError'])
        batch = json.loads(reply['result']['content'][0]['text'])['items'][0]
        self.assertEqual(batch['state'], 'closed')
        self.assertFalse(batch['can_close'])

    def test_bounded_history_keeps_active_rows_first_and_reports_truncation(self):
        for index in range(201):
            record(self.store, str(uuid.uuid5(uuid.NAMESPACE_URL, str(index))), closed=True)
        record(self.store, SID)
        result = list_batches(self.store)
        self.assertEqual(result['total'], 202)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['items']), 200)
        self.assertEqual(result['items'][0]['id'], SID)


if __name__ == '__main__':
    unittest.main()
