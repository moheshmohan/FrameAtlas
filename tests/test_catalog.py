import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from frameatlas.store import Store
from frameatlas.importers import import_manifest, import_takeout, cloud_url, recover_photos_metadata
from frameatlas.worker import Cache
from frameatlas.service import Service
from frameatlas.mcp import handle


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'data')
        self.item = {'provider': 'google_photos', 'source_id': 'stable-id', 'filename': 'hotel.mov', 'kind': 'video', 'captured': '2023-10-12T10:00:00Z'}
        self.aid, _ = self.store.upsert(self.item)

    def tearDown(self):
        self.tmp.cleanup()

    def test_search_source_access_checks_real_files_and_current_picker_receipts(self):
        from frameatlas.picker_sessions import record

        local = self.root / "local.mov"
        local.write_bytes(b"local test media")
        self.store.upsert({**self.item, "local_path": str(local)})
        ids = {}
        for index, label in enumerate(("active", "expired", "closed", "pending", "unverified"), start=1):
            sid = f"00000000-0000-4000-8000-{index:012d}"
            ids[label], _ = self.store.upsert({**self.item, "source_id": label,
                                               "locator": {"session_id": sid}})
            if label != "unverified":
                expiry = "2000-01-01T00:00:00Z" if label == "expired" else "2099-01-01T00:00:00Z"
                record(self.store, sid, expires=expiry, selected_count=None if label == "pending" else 1,
                       closed=label == "closed")

        service = Service(self.store)
        search = {"query": "hotel", "source_access": "local", "limit": 1}
        self.assertEqual([x["id"] for x in service.dispatch("search", search)["items"]], [self.aid])
        local.unlink()
        self.assertEqual(service.dispatch("search", search)["total"], 0)
        search["source_access"] = "picker"
        result = service.dispatch("search", search)
        self.assertEqual(result["total"], 1)
        self.assertEqual([x["id"] for x in result["items"]], [ids["active"]])
        with self.assertRaisesRegex(ValueError, "Source access"):
            service.dispatch("search", {"source_access": ["local"]})

    def test_speech_state_filters_recorded_outcomes_independently_of_transcript_text(self):
        self.store.upsert({**self.item, "source_id": "still", "kind": "photo"})
        ids = {}
        words = [{"text": "harbour", "start": 0, "end": 1}]
        for state in ("complete", "no_speech", "no_audio", "failed"):
            aid, _ = self.store.upsert({**self.item, "source_id": state})
            ids[state] = aid
            speech = {"text": "harbour", "words": words} if state in ("complete", "failed") else {}
            self.store.analysis(aid, [], speech, {"speech_status": state})

        service = Service(self.store)
        for state, aid in {**ids, "pending": self.aid}.items():
            with self.subTest(state=state):
                result = service.dispatch("search", {"speech_state": state})
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["items"][0]["id"], aid)
        result = service.dispatch("search", {
            "query": "harbour", "speech_only": True, "speech_state": "failed",
        })
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], ids["failed"])
        self.assertEqual(service.dispatch("search", {"speech_state": "pending", "kind": "photo"})["total"], 0)
        with self.assertRaisesRegex(ValueError, "Speech state"):
            service.dispatch("search", {"speech_state": ["no_speech"]})

    def test_storage_limit_is_persistent_bounded_and_repairable(self):
        service=Service(self.store)
        self.assertEqual(service.dispatch('stats',{})['temporary_cap'],2*1024**3)
        for invalid in (True,False,0,-2,3,17,'8',8.0,None,float('nan')):
            with self.assertRaises(ValueError):service.dispatch('storage_limit',{'gib':invalid})
        service.dispatch('storage_limit',{'gib':8})
        self.assertEqual(Store(self.store.root).storage_limit(),8*1024**3)
        self.assertEqual(service.dispatch('stats',{})['temporary_cap'],8*1024**3)
        self.assertEqual(service.dispatch('storage_limit',{'gib':32})['temporary_cap'],32*1024**3)
        with self.store.connect() as db:db.execute("UPDATE settings SET value='999999' WHERE key='storage_limit_gib'")
        with self.assertRaises(ValueError):self.store.storage_limit()
        service.dispatch('storage_limit',{'gib':4})
        self.assertEqual(self.store.storage_limit(),4*1024**3)

    def test_lowering_storage_cannot_break_an_active_source_reservation(self):
        service=Service(self.store);service.dispatch('storage_limit',{'gib':8})
        with patch('frameatlas.service.Cache.usage',return_value=4*1024**3+1):
            with self.assertRaisesRegex(ValueError,'Finish or clean'):service.dispatch('storage_limit',{'gib':4})
        self.assertEqual(self.store.storage_limit(),8*1024**3)

    def test_corrupt_storage_setting_does_not_consume_the_queue(self):
        from frameatlas.worker import Worker
        job=self.store.enqueue(self.aid)
        worker=Worker(self.store)
        try:
            with self.store.connect() as db:db.execute("INSERT INTO settings VALUES('storage_limit_gib','999999')")
            with patch.object(worker.stop,'wait',side_effect=lambda t:worker.stop.set()),contextlib.redirect_stdout(io.StringIO()):worker.loop()
            self.assertEqual(self.store.job(job['id'])['state'],'queued')
        finally:worker.close()

    def analyze(self):
        self.store.analysis(self.aid, [{'start': 2.0, 'end': 7.0, 'description': {'caption': 'A hotel dining room with a meal', 'subjects': [{'name': 'table'}]}}], {'text': 'കൊറിയയിലെ യാത്ര hotel', 'words': [{'text': 'hotel', 'start': 3, 'end': 4}]}, {'speech_status': 'complete'})

    def test_nonword_transcripts_are_excluded_from_coverage_and_remain_retryable(self):
        self.analyze()
        aid, _ = self.store.upsert({**self.item, 'source_id': 'punctuation'})
        speech = {'text': '… !', 'words': [{'text': '…', 'start': 0, 'end': 1}]}
        report = {'speech_status': 'complete', 'source_bytes': 10, 'probe': {'duration': 254016000000}}
        self.store.analysis(aid, [{'start': 0, 'end': 1, 'description': {'caption': 'Clouds'}}], speech, report.copy())
        before = self.store.get(aid)
        self.assertEqual(before['analysis']['speech_status'], 'no_speech')
        self.assertEqual(before['status'], 'ready')
        self.assertEqual(before['transcript'], '')
        with self.store.connect() as db:
            db.execute("DELETE FROM settings WHERE key='normalized_transcript_words_v1'")
            db.execute("UPDATE assets SET transcript=?,analysis=? WHERE id=?",
                       (speech['text'], json.dumps({**report, 'speech': speech}), aid))
            self.store.reindex(aid, db)
        reopened = Store(self.store.root)
        self.assertEqual(reopened.get(aid), before)
        self.assertEqual(reopened.stats()['transcribed'], 1)
        self.assertEqual(reopened.coverage('google_photos')['items'][0]['transcribed'], 1)
        self.assertEqual([item['id'] for item in reopened.search(speech_only=True)['items']], [self.aid])
        self.assertEqual(reopened.enqueue_speech(aid)['task'], 'speech')

    def test_search_has_timecoded_scenes_and_unicode_speech(self):
        self.analyze()
        for q in ('hotel', 'dining', 'കൊറിയയിലെ'):
            self.assertEqual(self.store.search(query=q)['total'], 1)
        result = self.store.search(query='meal')['items'][0]
        self.assertEqual(result['matches'][0]['start'], 2)
        self.assertIn('Food & dining', result['categories'])
        self.assertTrue(any(m.get('kind') == 'speech' and m['start'] == 3 for m in self.store.search(query='hotel')['items'][0]['matches']))

    def test_related_library_search_preserves_filters_and_edit_references(self):
        self.analyze()
        other_id, _ = self.store.upsert({**self.item, 'provider': 'local', 'source_id': 'other'})
        self.assertNotEqual(other_id, self.aid)
        expected = self.store.search(query='hotel dining room')['items'][0]['matches'][0]
        service = Service(self.store)
        with patch('frameatlas.ideas.local_models', return_value=['phi4-mini:latest']), \
                patch('frameatlas.ideas.related_phrases', return_value=['lodging', 'hotel dining room', 'hotel']):
            response = handle(service, {'id': 1, 'method': 'tools/call', 'params': {
                'name': 'catalog_search_related',
                'arguments': {'query': 'lodging', 'provider': 'google_photos', 'kind': 'video',
                              'since': '2023-10-01', 'until': '2023-10-31'}
            }})
            self.assertFalse(response['result']['isError'])
            result = json.loads(response['result']['content'][0]['text'])
            self.assertEqual(result['phrases'], ['hotel dining room', 'hotel'])
            self.assertEqual([asset['id'] for asset in result['items']], [self.aid])
            self.assertEqual(result['items'][0]['matched_query'], 'hotel dining room')
            self.assertEqual(result['items'][0]['matches'][0], expected)
            self.assertEqual(service.dispatch('search_related', {'query': 'lodging', 'kind': 'photo'})['items'], [])
            with self.assertRaisesRegex(ValueError, 'installed local writing model'):
                service.dispatch('search_related', {'query': 'lodging', 'model': 'remote-model'})
        for query in ('', '!!!', 'x' * 501, None):
            with self.assertRaisesRegex(ValueError, 'Enter a search query'):
                service.dispatch('search_related', {'query': query})

    def test_editor_clip_reference_resolves_search_hit_without_retrieval(self):
        self.analyze()
        hit = self.store.search(query='meal')['items'][0]['matches'][0]
        service = Service(self.store)
        args = {'id': self.aid, 'scene_key': hit['scene_key']}
        plan = service.dispatch('resolve', args)
        self.assertEqual(plan['source_id'], self.item['source_id'])
        self.assertEqual(plan['asset_id'], self.aid)
        self.assertEqual(plan['clip'], {
            'scene_key': hit['scene_key'], 'kind': 'scene', 'unit': 'seconds',
            'start': 2.0, 'end': 7.0, 'source_filename': 'hotel.mov',
            'source_start': 2.0, 'source_end': 7.0, 'source_path': None,
        })
        self.assertNotIn('clip', service.dispatch('resolve', {'id': self.aid}))
        with self.assertRaisesRegex(ValueError, 'stale or ambiguous'):
            service.dispatch('resolve', {**args, 'scene_key': '0.000000:1.000000'})
        self.assertEqual(self.store.jobs(), [])

    def test_spoken_clip_reference_rejects_changed_words_and_mixed_key_types(self):
        self.analyze()
        hit = next(m for m in self.store.search(query='hotel')['items'][0]['matches'] if m.get('kind') == 'speech')
        service = Service(self.store)
        request = {'id': self.aid, 'speech_key': hit['speech_key']}
        clip = service.dispatch('resolve', request)['clip']
        self.assertEqual((clip['kind'], clip['matched_text'], clip['source_filename']), ('speech', 'hotel', 'hotel.mov'))
        self.assertEqual((clip['source_start'], clip['source_end']), (3, 4))
        with self.assertRaisesRegex(ValueError, 'Choose a scene reference or'):
            service.dispatch('resolve', {**request, 'scene_key': '2.000000:7.000000'})
        self.store.analysis(self.aid, [], {'text': 'hotel', 'words': [{'text': 'hotel', 'start': 3, 'end': 4.2}]},
                            {'speech_status': 'complete'})
        with self.assertRaisesRegex(ValueError, 'transcript match is stale'):
            service.dispatch('resolve', request)
        self.assertEqual(self.store.jobs(), [])

    def test_filters_and_literal_fts_are_safe(self):
        self.analyze()
        self.assertEqual(self.store.search(provider='insta360')['total'], 0)
        self.assertEqual(self.store.search(since='2024-01-01')['total'], 0)
        self.assertEqual(self.store.search(until='2023-10-12')['total'], 1)
        for q in ['"', 'OR * NOT', 'x\" OR filename:*', "'; DROP TABLE assets;--"]:
            self.store.search(query=q)
        self.assertEqual(self.store.stats()['total'], 1)

    def test_gps_bounds_require_recorded_coordinates_and_support_wrapping(self):
        self.store.upsert({**self.item, 'metadata': {'gps': {'latitude': 25, 'longitude': 55}}})
        self.store.analysis(self.aid, [], {}, {'embedded_gps': {'latitude': -12, 'longitude': 178}})
        other, _ = self.store.upsert({'provider': 'local', 'source_id': 'other', 'filename': 'other.jpg',
                                      'kind': 'photo', 'metadata': {'gps': {'latitude': -11, 'longitude': -179}}})
        for name, geo in [('placeholder', (0, 0)), ('boolean', (True, 179)), ('partial', (-12, None))]:
            self.store.upsert({'provider': 'local', 'source_id': name, 'filename': name+'.jpg',
                               'kind': 'photo', 'metadata': {'gps': dict(zip(('latitude','longitude'),geo))}})
        result = Service(self.store).dispatch('search', {'bounds': '-15,175,-10,-175'})
        self.assertEqual({a['id'] for a in result['items']}, {self.aid, other})
        self.assertEqual(self.store.get(self.aid)['position']['source'], 'Embedded video GPS')
        self.assertEqual(self.store.search(query='hotel', bounds='24,54,26,56')['total'], 0)
        self.assertEqual(self.store.search(bounds='-90,-180,90,180')['total'], 2)
        for bounds in (None, 'nan,0,1,2', '1,0,-1,0', '1,2,3', '0,-181,2,3'):
            with self.subTest(bounds=bounds), self.assertRaisesRegex(ValueError, 'decimal degrees'):
                self.store.search(bounds=bounds)

    def test_video_gps_checks_signed_metadata_and_leaves_conflicts_unknown(self):
        from frameatlas.geography import video_position
        self.assertEqual(video_position(Path('clip.webm')), ('unsupported', None))
        with patch('frameatlas.geography.subprocess.run') as run:
            run.return_value.stdout = b'["+12.3400-056.7800+15.0/","+12.34-56.78/","+00.0+000.0/"]'
            self.assertEqual(video_position(Path('clip.mov')), ('recorded', {'latitude':12.34, 'longitude':-56.78}))
            run.return_value.stdout = b'["+12.34-56.78/","+23.45-56.78/"]'
            self.assertEqual(video_position(Path('clip.mov')), ('conflicting_embedded_positions', None))
            run.return_value.stdout = b'["+91.00-056.7/","+12.00+181.0/","12.34,56.78"]'
            self.assertEqual(video_position(Path('clip.mov')), ('no_embedded_position_read', None))
            original = self.root / 'camera.insv'
            original.touch()
            def read_alias(args, **kwargs):
                alias = Path(args[1])
                self.assertEqual(alias.suffix, '.mp4')
                self.assertTrue(alias.is_symlink())
                self.assertEqual(alias.resolve(), original.resolve())
                return type('Result', (), {'stdout': b'[]'})()
            run.side_effect = read_alias
            self.assertEqual(video_position(original), ('no_embedded_position_read', None))
            self.assertFalse(Path(run.call_args.args[0][1]).is_symlink())
            self.assertTrue(original.is_file())
            run.side_effect = FileNotFoundError()
            self.assertEqual(video_position(Path('clip.mov')), ('unavailable', None))

    def test_local_related_wording_only_supplies_indexed_evidence(self):
        self.analyze()
        self.assertEqual(self.store.search('lodging')['total'], 0)
        def local_reply(path, payload, *, timeout):
            self.assertEqual(path, '/api/chat')
            if 'phrases' in payload['format']['properties']:
                return {'message': {'content': json.dumps({
                    'phrases': ['Hotel dining room', 'hotel dining room', 'https://example.test/private']
                })}}
            evidence = json.loads(payload['messages'][1]['content'])['evidence']
            self.assertEqual([a['id'] for a in evidence], [self.aid])
            self.assertIn('hotel dining room', evidence[0]['scenes'][0]['caption'])
            return {'message': {'content': json.dumps({'ideas': [{
                'title': 'A stop for dinner', 'hook': 'A meal away from home',
                'outline': ['PICKUP NEEDED: Shows the room'],
                'sources': [{'asset_id': self.aid, 'start': 2, 'end': 7, 'reason': 'PICKUP NEEDED: Shows the room'}],
                'pickups': ['Shows the room.', evidence[0]['scenes'][0]['caption'], 'Exterior sign at dusk']
            }]})}}

        with patch('frameatlas.ideas.local_models', return_value=['qwen3:8b']), \
                patch('frameatlas.resources.gpu_lease', return_value=contextlib.nullcontext()), \
                patch('frameatlas.ideas.request_json', side_effect=local_reply) as local:
            result = Service(self.store).dispatch('ideas', {'prompt': 'lodging', 'expand_search': True})
        self.assertEqual(local.call_count, 2)
        self.assertEqual(result['related_search_phrases'], ['hotel dining room'])
        self.assertEqual(result['ideas'][0]['sources'][0]['asset_id'], self.aid)
        self.assertEqual(result['ideas'][0]['sources'][0]['reason'], 'Shows the room')
        self.assertEqual(result['ideas'][0]['pickups'], ['Exterior sign at dusk'])
        self.assertEqual(result['ideas'][0]['outline'], ['hotel.mov — Shows the room', 'PICKUP NEEDED: Exterior sign at dusk'])
        # the story is on the Idea Pad too, with its cited edit sequence
        pad = Service(self.store).dispatch('idea_pad', {'origin': 'story'})
        self.assertEqual(pad['total'], 1)
        story = pad['items'][0]
        self.assertEqual(result['saved_ids'], [story['id']])
        self.assertEqual((story['title'], story['logline'], story['format']), ('A stop for dinner', 'A meal away from home', 'story'))
        self.assertEqual(story['basis']['sequence']['sources'][0]['start'], 2)
        self.assertEqual(story['basis']['sequence']['pickups'], ['Exterior sign at dusk'])
        self.assertEqual(story['basis']['sequence']['related_search_phrases'], ['hotel dining room'])
        self.assertEqual([a['id'] for a in story['linked']], [self.aid])

    def test_scoped_story_uses_filtered_results_and_prioritizes_a_late_scene_match(self):
        local = self.root / "source.mov"
        local.write_bytes(b"fixture")
        self.store.upsert({**self.item, "local_path": str(local)})
        late = {"start": 70, "end": 74, "description": {"caption": "A train station"}}
        early = [{"start": i * 5, "end": i * 5 + 4, "description": {"caption": "Sunrise clouds"}}
                 for i in range(14)]
        self.store.analysis(self.aid, early + [late], {}, {})
        for provider, path in (("insta360", str(local)), ("google_photos", "")):
            aid, _ = self.store.upsert({**self.item, "provider": provider, "source_id": "other",
                                       "local_path": path})
            self.store.analysis(aid, [late], {}, {})
        self.store.upsert({**self.item, "source_id": "metadata-only", "filename": "train station.mov",
                           "local_path": str(local)})
        scope = {"query": "train station", "provider": "google_photos", "source_access": "local"}

        def reply(_path, payload, *, timeout):
            evidence = json.loads(payload["messages"][1]["content"])["evidence"]
            self.assertEqual([asset["id"] for asset in evidence], [self.aid])
            self.assertIn(70, [scene["start"] for scene in evidence[0]["scenes"]])
            return {"message": {"content": json.dumps({"ideas": [{
                "title": "Arrival", "hook": "Train footage", "sources": [
                    {"asset_id": self.aid, "start": 70, "end": 74, "reason": "Show the station"}],
                "pickups": [],
            }]})}}

        service = Service(self.store)
        with patch("frameatlas.ideas.local_models", return_value=["qwen3:8b"]), \
                patch("frameatlas.resources.gpu_lease", return_value=contextlib.nullcontext()), \
                patch("frameatlas.ideas.request_json", side_effect=reply):
            result = service.dispatch("ideas", {"prompt": "Use the sunrise clouds", "search_scope": scope})
            # the Library's filters always carry person (empty when no one is chosen)
            with_person = service.dispatch("ideas", {"prompt": "Use the sunrise clouds", "search_scope": {**scope, "person": ""}})
            self.assertEqual(with_person["ideas"][0]["sources"][0]["start"], 70)
        self.assertEqual(result["search_scope"], scope)
        self.assertEqual(result["ideas"][0]["sources"][0]["start"], 70)
        with self.assertRaisesRegex(ValueError, "No matching media"):
            service.dispatch("ideas", {"search_scope": {**scope, "query": "nonexistent"}})
        with self.assertRaisesRegex(ValueError, "either exact media or Library filters"):
            service.dispatch("ideas", {"search_scope": scope, "asset_ids": [self.aid]})
        self.assertTrue(result['offline'])

    def test_coverage_counts_known_records_by_source_month_and_analysis_state(self):
        self.analyze()
        camera = {'provider': 'insta360', 'source_id': 'camera', 'filename': 'view.insv',
                  'kind': 'video', 'captured': '2023-10-31T23:30:00-04:00'}
        aid, _ = self.store.upsert(camera)
        self.store.analysis(aid, [{'start': 0, 'end': 1, 'description': {'caption': 'A street'}}],
                            {}, {'speech_status': 'no_audio', 'coverage_warning': 'Sampled'})
        self.store.upsert({**self.item, 'source_id': 'unknown-date', 'captured': '2023-19-01'})
        self.store.upsert({**self.item, 'source_id': 'next-month', 'captured': '2023-11-01T00:00:00Z'})
        service = Service(self.store)
        reply = handle(service, {'id': 1, 'method': 'tools/call', 'params': {
            'name': 'catalog_coverage', 'arguments': {'provider': 'google_photos', 'limit': 2}}})
        data = json.loads(reply['result']['content'][0]['text'])
        self.assertEqual(data['scope'], 'catalogued_records_only')
        self.assertEqual([row['month'] for row in data['items']], ['2023-11', '2023-10'])
        self.assertEqual(data['items'][1]['transcribed'], 1)
        self.assertTrue(data['has_more'])
        self.assertEqual(data['totals'], [
            {'provider': 'google_photos', 'catalogued': 3, 'ready': 1, 'partial': 0,
             'metadata_only': 2, 'transcribed': 1},
            {'provider': 'insta360', 'catalogued': 1, 'ready': 0, 'partial': 1,
             'metadata_only': 0, 'transcribed': 0},
        ])
        next_page = service.dispatch('coverage', {'provider': 'google_photos', 'offset': '2'})
        self.assertIsNone(next_page['items'][0]['month'])
        self.assertFalse(next_page['has_more'])
        studio = service.dispatch('coverage', {'provider': 'insta360'})
        self.assertEqual(studio['items'][0]['month'], '2023-10')
        self.assertEqual(studio['items'][0]['partial'], 1)
        for row in [*data['items'], *next_page['items'], *studio['items']]:
            for key, status in (('catalogued', ''), ('ready', 'ready'), ('partial', 'partial'),
                                ('metadata_only', 'metadata_only')):
                with self.subTest(month=row['month'], provider=row['provider'], status=status):
                    response = handle(service, {'id': 2, 'method': 'tools/call', 'params': {
                        'name': 'catalog_search', 'arguments': {'provider': row['provider'],
                        'capture_month': row['month'] or 'unknown', 'status': status, 'limit': 1}}})
                    self.assertFalse(response['result']['isError'])
                    self.assertEqual(json.loads(response['result']['content'][0]['text'])['total'], row[key])
        self.assertEqual(service.dispatch('search', {'query': 'street', 'capture_month': '2023-10',
                                                     'status': 'partial'})['items'][0]['id'], aid)
        for bad in ('2023-00', '2023-13', '2023-10-01', True, None):
            with self.subTest(capture_month=bad), self.assertRaisesRegex(ValueError, 'Capture month'):
                service.dispatch('search', {'capture_month': bad})
        with self.assertRaisesRegex(ValueError, 'Choose Google Photos'):
            service.dispatch('coverage', {'provider': 'unrecognized'})

    def test_coverage_media_kind_filters_totals_and_months_for_library_links(self):
        self.analyze()
        self.store.upsert({**self.item, 'source_id': 'photo', 'filename': 'hotel.jpg', 'kind': 'photo'})
        self.store.upsert({**self.item, 'provider': 'insta360', 'source_id': 'camera', 'filename': 'street.insv'})
        service = Service(self.store)
        args = {'provider': 'google_photos', 'kind': 'video', 'limit': 1}
        result = handle(service, {'id': 1, 'method': 'tools/call', 'params': {
            'name': 'catalog_coverage', 'arguments': args}})['result']
        self.assertFalse(result['isError'])
        data = json.loads(result['content'][0]['text'])
        self.assertEqual(data['kind'], 'video')
        self.assertEqual([(r['provider'], r['catalogued']) for r in data['totals']],
                         [('google_photos', 1), ('insta360', 1)])
        self.assertEqual(len(data['items']), 1)
        self.assertFalse(data['has_more'])
        row = data['items'][0]
        for field, status in (('catalogued', ''), ('ready', 'ready'), ('partial', 'partial'),
                              ('metadata_only', 'metadata_only')):
            found = service.dispatch('search', {'provider': row['provider'], 'kind': data['kind'],
                                                'capture_month': row['month'], 'status': status})
            self.assertEqual(found['total'], row[field])
        self.assertEqual(service.dispatch('coverage', {'kind': 'photo'})['totals'][0]['catalogued'], 1)
        self.assertEqual(service.dispatch('coverage', {'kind': 'audio'})['items'], [])
        with self.assertRaisesRegex(ValueError, 'Choose photos'):
            service.dispatch('coverage', {'kind': 'all'})

    def test_timecodes_require_whole_terms_and_contiguous_speech(self):
        captions = ('The Korean terminal', 'Airplane overhead', 'A Korean Air logo', 'A café')
        scenes = [{'start': i*5, 'end': i*5+4, 'description': {'caption': caption}}
                  for i, caption in enumerate(captions)]
        tokens = ('Korean', 'food', 'hair', 'Korean', 'Air.', 'കൊറിയയിലെ')
        words = [{'text': token, 'start': i+1, 'end': i+1.5} for i, token in enumerate(tokens)]
        self.store.analysis(self.aid, scenes, {'text': ' '.join(tokens), 'words': words}, {})

        matches = self.store.search('Korean Air')['items'][0]['matches']
        self.assertEqual([(m.get('kind'), m['start'], m['end']) for m in matches],
                         [('speech', 4, 5.5), (None, 10, 14)])
        self.assertEqual((matches[0]['word_start'], matches[0]['word_end'], matches[0]['matched_text']),
                         (3, 4, 'Korean Air.'))
        self.assertEqual(self.store.search('cafe')['items'][0]['matches'][0]['start'], 15)
        self.assertEqual(self.store.search('കൊറിയയിലെ')['items'][0]['matches'][0]['start'], 6)
        self.assertEqual(self.store.search('terminal cafe')['items'][0]['matches'], [])

        scene = self.store.search('Korean Air', scene_only='true', limit=1)
        self.assertEqual(scene['total'], 1)
        self.assertEqual([(m['start'], m['end']) for m in scene['items'][0]['matches']], [(10, 14)])
        self.assertEqual(self.store.search('Korean Air', scene_only=True, offset=1)['items'], [])
        self.assertEqual(self.store.search('terminal cafe', scene_only=True)['total'], 0)
        self.assertEqual(self.store.search('hair', scene_only=True)['total'], 0)
        self.store.upsert({**self.item, 'source_id': 'metadata-only', 'filename': 'Korean Air.mov'})
        self.assertEqual(self.store.search(scene_only=True)['total'], 1)
        with self.assertRaisesRegex(ValueError, 'either scene captions or spoken words'):
            self.store.search('Korean Air', scene_only=True, speech_only=True)

    def test_speech_only_search_ignores_visual_matches_and_requires_a_phrase(self):
        self.analyze()
        items = self.store.search('hotel', speech_only=True)['items']
        self.assertEqual([a['id'] for a in items], [self.aid])
        self.assertEqual([(m['kind'], m['start']) for m in items[0]['matches']], [('speech', 3)])
        for query in ('dining room', 'hotel meal', '!!!', 'hotel OR meal'):
            self.assertEqual(self.store.search(query, speech_only=True)['total'], 0)
        self.assertEqual(self.store.search('കൊറിയയിലെ', speech_only='true')['total'], 1)
        self.store.upsert({**self.item, 'source_id': 'silent', 'filename': 'silent.mov'})
        self.assertEqual(self.store.search(speech_only=True)['total'], 1)
        self.assertEqual(self.store.search(speech_only='false')['total'], 2)
        with self.assertRaisesRegex(ValueError, 'true or false'):
            self.store.search(speech_only='yes')

    def test_many_visual_hits_do_not_hide_a_later_spoken_hit(self):
        scenes = [{'start': i, 'end': i+1, 'description': {'caption': 'A terminal'}} for i in range(30)]
        speech = {'text': 'terminal', 'words': [{'text': 'terminal', 'start': 34, 'end': 34.5}]}
        self.store.analysis(self.aid, scenes, speech, {'speech_status': 'complete'})
        matches = self.store.search('terminal')['items'][0]['matches']
        self.assertEqual(len(matches), 20)
        self.assertEqual(matches[-1]['kind'], 'speech')
        self.assertEqual(matches[-1]['start'], 34)

    def test_exact_inventory_filename_precedes_analyzed_keyword_match(self):
        other, _ = self.store.upsert(dict(self.item, source_id='another', filename='airport.mov',
                                          captured='2025-10-01T10:00:00Z'))
        self.store.analysis(other, [{'start': 0, 'end': 2, 'description': {'caption': 'hotel.mov'}}],
                            {}, {})

        first = self.store.search('HOTEL.MOV', limit=1)
        second = self.store.search('HOTEL.MOV', limit=1, offset=1)
        self.assertEqual(first['total'], 2)
        self.assertEqual(first['items'][0]['id'], self.aid)
        self.assertEqual(second['items'][0]['id'], other)

    def test_filename_with_standalone_punctuation_and_exact_name_filters(self):
        name = 'South Korea - Travel Short (1080x1920).mp4'
        first, _ = self.store.upsert({**self.item, 'source_id': 'first', 'filename': name})
        second, _ = self.store.upsert({**self.item, 'source_id': 'second', 'filename': name,
                                       'captured': '2025-11-01T00:00:00Z'})
        unrelated, _ = self.store.upsert({**self.item, 'source_id': 'unrelated', 'filename': 'another.mov'})
        self.store.analysis(unrelated, [{'start': 0, 'end': 2, 'description': {'caption': name}}], {}, {})

        self.assertEqual(self.store.search(name)['items'][0]['id'], second)
        self.assertEqual(self.store.search('South Korea - Travel Short')['total'], 3)
        self.assertEqual(self.store.search('- !')['total'], 0)
        self.assertEqual(self.store.search(filename=name)['total'], 2)
        result = Service(self.store).dispatch('search', {
            'filename': name.upper(), 'provider': 'google_photos', 'capture_month': '2023-10',
        })
        self.assertEqual([item['id'] for item in result['items']], [first])
        self.assertEqual(result['items'][0]['matches'], [])

    def test_compare_sources_requires_exact_evidence_and_keeps_records(self):
        from frameatlas.fingerprint import digest_source
        other, _ = self.store.upsert({**self.item, 'source_id': 'separate-upload'})
        service = Service(self.store)
        args = {'first': self.aid, 'second': other}
        path = self.root / 'source.mov'
        path.write_bytes(b'video')
        original = digest_source(path, lambda: False)
        self.store.analysis(self.aid, [], {}, {'file_fingerprint': original})
        self.assertEqual(service.dispatch('compare_sources', args)['result'], 'unknown')
        path.write_bytes(b'audio')  # Same byte count and catalogue filename, different data.
        self.store.analysis(other, [], {}, {'file_fingerprint': digest_source(path, lambda: False)})
        self.assertEqual(service.dispatch('compare_sources', args)['result'], 'different_bytes')
        self.store.analysis(other, [], {}, {'file_fingerprint': original})
        self.assertEqual(service.dispatch('compare_sources', args)['result'], 'same_bytes')
        self.assertEqual(self.store.stats()['total'], 2)
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            digest_source(path, lambda: True)
        with self.assertRaisesRegex(ValueError, 'two different'):
            service.dispatch('compare_sources', {'first': other, 'second': other})

    def test_reimport_and_reanalysis_preserve_corrections(self):
        self.analyze()
        self.store.annotate(self.aid, ['corrected'], 'Verified by owner')
        aid, new = self.store.upsert(self.item)
        self.assertEqual(aid, self.aid)
        self.assertFalse(new)
        self.analyze()
        asset = self.store.get(aid)
        self.assertEqual(asset['summary'], 'Verified by owner')
        self.assertEqual(asset['tags'], ['corrected'])
        self.assertEqual(len(asset['scenes']), 1)

    def test_annotation_reads_metadata_after_a_concurrent_refresh(self):
        self.store.upsert({**self.item, 'metadata': {'inventory': 'old', 'gps': {'latitude': 25}}})
        connect = self.store.connect
        begin = threading.Event()
        owner = threading.current_thread()

        @contextlib.contextmanager
        def traced_connect():
            with connect() as db:
                if threading.current_thread() is not owner:
                    db.set_trace_callback(lambda sql: begin.set() if sql.startswith('BEGIN') else None)
                yield db

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(self.store, 'connect', traced_connect):
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                edit = pool.submit(self.store.annotate, self.aid, ['owner tag'], 'Owner summary')
                self.assertTrue(begin.wait(timeout=3))
                self.store.upsert({**self.item, 'metadata': {'inventory': 'new'},
                                   'locator': {'session_id': 'refreshed'}}, db)
            asset = edit.result(timeout=3)
        self.assertEqual(asset['metadata']['inventory'], 'new')
        self.assertEqual(asset['metadata']['gps'], {'latitude': 25})
        self.assertEqual(asset['locator']['session_id'], 'refreshed')
        self.assertEqual(asset['summary'], 'Owner summary')
        self.assertEqual(asset['metadata']['annotations']['tags'], ['owner tag'])

    def test_stale_scene_key_cannot_correct_a_reordered_scene(self):
        first = {'start': 2, 'end': 4, 'description': {'caption': 'A window'}}
        second = {'start': 4, 'end': 6, 'description': {'caption': 'A doorway'}}
        self.store.analysis(self.aid, [first, second], {}, {'speech_status': 'no_audio'})
        original = self.store.get(self.aid)['scenes'][0]
        self.store.analysis(self.aid, [second, first], {}, {'speech_status': 'no_audio'})
        service = Service(self.store)
        args = {'id': self.aid, 'ordinal': 0, 'scene_key': original['scene_key'], 'caption': 'Owner correction'}
        with self.assertRaisesRegex(ValueError, 'scene changed'):
            service.dispatch('correct_scene', args)
        self.assertEqual(self.store.search('Owner correction')['total'], 0)
        result = service.dispatch('correct_scene', {**args, 'ordinal': 1})
        self.assertEqual([row['caption'] for row in result['scenes']], ['A doorway', 'Owner correction'])
        self.store.upsert({**self.item, 'locator': {'session_id': 'refreshed'}})
        self.store.analysis(self.aid, [first, second], {}, {'speech_status': 'no_audio'})
        self.assertEqual(self.store.get(self.aid)['scenes'][0]['caption'], 'Owner correction')

    def test_saved_citation_resolves_current_scene_and_preserves_directional_identity(self):
        original = {'start': 0, 'end': 20, 'description': {'caption': 'Wide angle'}}
        sample = {'start': 0, 'end': 20, 'description': {'caption': 'Ceiling'},
                  'sample_time': 12, 'sample_key': 'ceiling', 'source_part': {'filename': 'first.insv'}}
        self.store.analysis(self.aid, [original, sample], {}, {'speech_status': 'no_audio'})
        service = Service(self.store)
        args = {'id': self.aid, 'start': '12', 'end': '12', 'sample_time': '12',
                'scene_key': 'sample:ceiling', 'part_filename': 'first.insv'}
        match = service.dispatch('asset', args)['citation_match']
        self.assertEqual(match, {'ordinal': 1, 'scene_key': 'sample:ceiling'})
        hit = self.store.search('Ceiling')['items'][0]['matches'][0]
        self.assertEqual(hit['sample_time'], 12)
        point = {'id': self.aid, 'sample_time': hit['sample_time'], 'scene_key': hit['scene_key'],
                 'part_filename': hit['source_part']['filename']}
        self.assertEqual(service.dispatch('asset', point)['citation_match'], match)
        self.store.analysis(self.aid, [sample, original], {}, {'speech_status': 'no_audio'})
        self.assertEqual(service.dispatch('asset', args)['citation_match']['ordinal'], 0)
        self.assertEqual(service.dispatch('asset', point)['citation_match']['ordinal'], 0)
        self.assertIsNone(service.dispatch('asset', {**args, 'part_filename': 'second.insv'})['citation_match'])
        self.assertEqual(service.dispatch('asset', {'id': self.aid, 'start': 2, 'end': 4})['citation_match']['ordinal'], 1)
        with self.assertRaisesRegex(ValueError, 'finite'):
            service.dispatch('asset', {**args, 'start': 'nan'})
        self.store.analysis(self.aid, [original], {}, {'speech_status': 'no_audio'})
        self.assertIsNone(service.dispatch('asset', args)['citation_match'])
        self.assertIsNone(service.dispatch('asset', point)['citation_match'])

    def test_manifest_is_atomic_and_does_not_store_secrets(self):
        with self.assertRaises(ValueError):
            import_manifest(self.store, {'items': [{'provider': 'local', 'source_id': 'second', 'filename': 'a.jpg'}, {'provider': 'bogus'}]})
        self.assertEqual(self.store.stats()['total'], 1)
        import_manifest(self.store, {'items': [dict(self.item, token='secret', baseUrl='https://lh3.googleusercontent.com/signed', cloud_url='https://evil.test')]})
        a = self.store.get(self.aid)
        self.assertNotIn('secret', json.dumps(a))
        self.assertEqual(a['cloud_url'], '')
        self.assertEqual(cloud_url('https://photos.google.com.evil.test/x'), '')

    def test_verified_takeout_location_preserves_picker_identity_and_existing_jobs(self):
        self.store.upsert({**self.item, 'locator': {'picker_id': self.item['source_id'], 'session_id': 'selected'}})
        self.analyze()
        old = self.store.get(self.aid)
        job = self.store.enqueue(self.aid)['id']
        takeout = self.store.upsert({**self.item, 'source_id': 'takeout:distinct', 'metadata': {
            'gps': {'latitude': 35.1, 'longitude': 129.1}}})[0]
        args = {'picker_id': self.aid, 'takeout_id': takeout}
        service = Service(self.store)
        result = service.dispatch('link_takeout_location', args)
        self.assertEqual(result['position']['source'], 'Linked Google Takeout GPS')
        linked = self.store.get(self.aid)
        for field in ('locator', 'scenes', 'analysis', 'source_id', 'transcript'):
            self.assertEqual(linked[field], old[field])
        self.assertEqual(self.store.job(job)['state'], 'queued')
        self.assertEqual(self.store.stats()['total'], 2)
        self.assertEqual(self.store.search(query='meal', bounds='34,128,36,130')['items'][0]['id'], self.aid)
        self.store.upsert({**self.item, 'metadata': {'gps': {'latitude': 25, 'longitude': 55}}})
        self.assertEqual(self.store.get(self.aid)['position']['source'], 'Catalogue metadata')
        self.assertEqual(self.store.search(query='meal', bounds='34,128,36,130')['total'], 0)
        self.assertEqual(self.store.search(query='meal', bounds='24,54,26,56')['total'], 1)
        self.assertEqual(service.dispatch('link_takeout_location', {**args, 'takeout_id': ''})['takeout_id'], None)
        self.assertNotIn('takeout_location', self.store.get(self.aid)['metadata'])
        with self.assertRaisesRegex(ValueError, 'same media type'):
            service.dispatch('link_takeout_location', {**args, 'takeout_id': self.aid})

    def test_takeout_only_reads_sidecars_and_deduplicates(self):
        arc = self.root / 'takeout.zip'
        metadata = {'title': 'walk.jpg', 'photoTakenTime': {'timestamp': '1697000000'},
                    'geoDataExif': {'latitude': 0, 'longitude': 0}, 'geoData': {'latitude': 35.1, 'longitude': 129.1},
                    'url': 'https://photos.google.com/photo/example'}
        with zipfile.ZipFile(arc, 'w') as z:
            z.writestr('Takeout/Photos/walk.jpg', b'NOT AN IMAGE')
            z.writestr('../../walk.jpg.json', json.dumps(metadata))
            z.writestr('album/walk.jpg.json', json.dumps(metadata))
        result = import_takeout(self.store, arc)
        self.assertEqual(result['seen'], 2)
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['originals_extracted'], 0)
        self.assertFalse((self.root / 'walk.jpg').exists())
        a = self.store.search(query='walk.jpg')['items'][0]
        self.assertEqual(a['metadata']['gps']['latitude'], 35.1)

    def test_takeout_identity_keeps_distinct_sidecar_occurrences_without_photo_links(self):
        archive = self.root / 'takeout.zip'
        metadata = {'title': 'walk.jpg', 'photoTakenTime': {'timestamp': '1697000000'},
                    'url': 'https://photos.google.com/album/same-album'}
        direct = {**metadata, 'url': 'https://photos.google.com/photo/stable-id'}
        with zipfile.ZipFile(archive, 'w') as arc:
            for folder in ('First', 'Second'):
                arc.writestr(f'{folder}/walk.jpg.json', json.dumps(metadata))
            arc.writestr('First/direct.jpg.json', json.dumps(direct))
            arc.writestr('Second/direct.jpg.json', json.dumps({**direct, 'url': direct['url'] + '/?example=1'}))
        result = import_takeout(self.store, archive)
        self.assertEqual((result['seen'], result['added']), (4, 3))
        results = self.store.search('walk.jpg')['items']
        occurrences = [a for a in results if a['locator']['takeout_identity'] == 'sidecar_occurrence']
        self.assertEqual(len(occurrences), 2)
        self.assertEqual(len({a['source_id'] for a in occurrences}), 2)
        self.assertEqual({a['locator']['takeout_sidecar'] for a in occurrences},
                         {'First/walk.jpg.json', 'Second/walk.jpg.json'})
        moved = self.root / 'moved'
        moved.mkdir()
        archive.rename(moved / archive.name)
        self.assertEqual(import_takeout(self.store, moved / archive.name)['added'], 0)

    def test_takeout_folder_import_keeps_successful_shards_and_rolls_back_corrupt_archive(self):
        folder = self.root / 'export'
        folder.mkdir()
        metadata = {'title': 'walk.jpg', 'photoTakenTime': {'timestamp': '1697000000'},
                    'url': 'https://photos.google.com/photo/walk'}
        with zipfile.ZipFile(folder / '01.zip', 'w') as arc:
            arc.writestr('Takeout/walk.jpg', b'original should stay in archive')
            arc.writestr('Takeout/walk.jpg.json', json.dumps(metadata))
        bad = folder / '02.zip'
        corrupted = json.dumps({**metadata, 'title': 'broken.jpg'}).encode()
        with zipfile.ZipFile(bad, 'w') as arc:
            arc.writestr('Takeout/rollback.json', json.dumps({**metadata, 'url': 'https://photos.google.com/photo/rollback'}))
            arc.writestr('Takeout/broken.json', corrupted)
        contents = bad.read_bytes()
        bad.write_bytes(contents.replace(corrupted, b'X' + corrupted[1:]))
        with zipfile.ZipFile(folder / '03.ZIP', 'w') as arc:
            arc.writestr('Album/walk.jpg.json', json.dumps(metadata))
            arc.writestr('Album/train.mov.json', json.dumps({**metadata, 'title': 'train.mov',
                                                             'url': 'https://photos.google.com/photo/train'}))
        (folder / 'ignored.zip').symlink_to(bad)
        result = import_takeout(self.store, folder)
        self.assertEqual((result['seen'], result['added'], result['failed_archives']), (3, 2, 1))
        self.assertEqual([entry['filename'] for entry in result['archives']], ['01.zip', '02.zip', '03.ZIP'])
        self.assertIn('CRC', result['archives'][1]['error'])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM assets WHERE cloud_url LIKE '%/rollback'").fetchone()[0], 0)
        self.assertFalse((folder / 'Takeout').exists())
        self.assertEqual(import_takeout(self.store, folder)['added'], 0)
        with self.assertRaisesRegex(ValueError, 'Enter a local Takeout'):
            import_takeout(self.store, '')

    def test_removed_original_metadata_is_recoverable_without_media_or_credentials(self):
        cache = self.root / 'photos-cache'; item = cache / 'native-id'; item.mkdir(parents=True)
        (item / 'missing.mov.json').write_text(json.dumps({'title':'missing.mov','googlePhotosId':'native-id','created':'2021-08-09T10:00:00Z','mimeType':'video/mp4','access_token':'must not be imported'}))
        (item / 'wrong.mov.json').write_text(json.dumps({'title':'wrong.mov','googlePhotosId':'wrong-id'}))
        (item / 'tokens.json').write_text('not a media sidecar')
        outside = self.root / 'private.json'; outside.write_text('private')
        (item / 'symlink.mov.json').symlink_to(outside)
        a = recover_photos_metadata(self.store, cache)
        b = recover_photos_metadata(self.store, cache)
        self.assertEqual((a['seen'],a['added'],b['added']), (1,1,0))
        recovered = self.store.search(query='missing.mov')['items'][0]
        self.assertEqual(recovered['local_path'], '')
        self.assertEqual(recovered['kind'], 'video')
        self.assertNotIn('must not be imported', json.dumps(recovered))
        self.assertFalse((item / 'missing.mov').exists())
        self.assertEqual(outside.read_text(), 'private')

    def test_cache_cleans_failure_but_never_originals_or_symlinks(self):
        cache = Cache(self.store.root)
        original = self.root / 'original.mov'
        original.write_bytes(b'original')
        with self.assertRaisesRegex(RuntimeError, 'failure'):
            with cache.job() as work:
                (work / 'temporary.mov').write_bytes(b'temporary')
                (work / 'original-link').symlink_to(original)
                raise RuntimeError('failure')
        self.assertEqual(original.read_bytes(), b'original')
        self.assertEqual(cache.usage(), 0)
        with self.assertRaises(ValueError):
            cache.remove(self.root)

    def test_queue_is_idempotent_and_recovers_interrupted_jobs(self):
        job = self.store.enqueue(self.aid)
        self.assertEqual(self.store.enqueue(self.aid)['id'], job['id'])
        self.store.cancel(job['id'])
        self.assertEqual(self.store.jobs()[0]['state'], 'cancelled')
        nextjob = self.store.enqueue(self.aid)
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (nextjob['id'],))
        self.store.recover()
        self.assertEqual(self.store.jobs()[0]['state'], 'failed')

    def test_queue_pause_is_persistent_and_counts_all_work(self):
        self.store.enqueue(self.aid)
        self.assertEqual(self.store.queue_state(True)['counts']['queued'], 1)
        self.assertTrue(Store(self.store.root).queue_state()['paused'])
        self.assertFalse(self.store.queue_state(False)['paused'])

    def test_queue_distinguishes_recovered_errors_from_open_failures(self):
        def finish(job, state):
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET state=?,error=? WHERE id=?",
                           (state, 'Download failed' if state == 'failed' else '', job))

        old = self.store.enqueue(self.aid)['id']
        finish(old, 'failed')
        retry = self.store.enqueue(self.aid)['id']
        self.assertIsNone(self.store.job(old)['recovered_by'])
        self.assertEqual(self.store.queue_state(include_waiting=True)['unresolved_failures'], 1)
        finish(retry, 'done')
        self.assertEqual(self.store.job(old)['recovered_by'], retry)
        self.assertEqual(self.store.job(old)['error'], 'Download failed')
        self.assertEqual(self.store.queue_state(include_waiting=True)['counts']['failed'], 1)
        self.assertEqual(self.store.queue_state(include_waiting=True)['unresolved_failures'], 0)

        first = self.root / 'first-edit'
        second = self.root / 'second-edit'
        first.mkdir()
        second.mkdir()
        failed = self.store.enqueue_retrieval(self.aid, str(first))['id']
        finish(failed, 'failed')
        finish(self.store.enqueue_retrieval(self.aid, str(second))['id'], 'done')
        self.assertIsNone(self.store.job(failed)['recovered_by'])
        success = self.store.enqueue_retrieval(self.aid, str(first))['id']
        finish(success, 'done')
        self.store.job_result(failed, {'studio_download': {'state': 'waiting'}})
        self.assertEqual(self.store.queue_state(include_waiting=True)['unresolved_failures'], 1)
        self.assertIsNone(self.store.job(failed)['recovered_by'])
        self.store.job_result(failed, {'studio_download': {'state': 'removed'}})
        self.assertEqual(self.store.job(failed)['recovered_by'], success)
        self.assertEqual(self.store.queue_state(include_waiting=True)['unresolved_failures'], 0)

        self.store.enqueue(self.aid)
        rows = self.store.jobs()
        self.assertLess(next(i for i,j in enumerate(rows) if j['state']=='queued'),
                        next(i for i,j in enumerate(rows) if j['recovered_by']))

    def test_mcp_uses_same_search_and_reports_bad_arguments(self):
        self.analyze()
        svc = Service(self.store)
        r = handle(svc, {'id': 1, 'method': 'tools/call', 'params': {'name': 'catalog_search', 'arguments': {'query': 'hotel'}}})
        self.assertFalse(r['result']['isError'])
        self.assertEqual(json.loads(r['result']['content'][0]['text'])['total'], 1)
        r = handle(svc, {'id': 2, 'method': 'tools/call', 'params': {'name': 'catalog_search', 'arguments': {'arbitrary_sql': 'x'}}})
        self.assertTrue(r['result']['isError'])

    def test_360_coverage_is_not_marked_complete(self):
        self.store.analysis(self.aid, [{'start': 0, 'end': 1, 'description': {'caption': 'A road'}}], {}, {'speech_status': 'no_audio', 'coverage_warning': 'One direction only'})
        self.assertEqual(self.store.get(self.aid)['status'], 'partial')

    def test_mcp_search_exposes_pagination_for_the_full_inventory(self):
        for i in range(2):self.store.upsert({'provider':'local','source_id':str(i),'filename':str(i)+'.jpg','kind':'photo'})
        reply=handle(Service(self.store),{'id':1,'method':'tools/call','params':{'name':'catalog_search','arguments':{'limit':1,'offset':1}}})
        self.assertFalse(reply['result']['isError'])
        page=json.loads(reply['result']['content'][0]['text'])
        self.assertEqual((page['total'],page['limit'],page['offset'],len(page['items'])),(3,1,1,1))

    def test_scene_corrections_preserve_provenance_without_searching_old_guesses(self):
        self.analyze()
        self.store.correct_scene(self.aid, 0, 'A Flipper Zero on a desk')
        self.assertEqual(self.store.search(query='Flipper')['total'], 1)
        detail = self.store.get(self.aid)['scenes'][0]['details']
        self.assertEqual(detail['generated_description']['caption'], 'A hotel dining room with a meal')
        self.analyze()
        self.assertEqual(self.store.get(self.aid)['scenes'][0]['caption'], 'A Flipper Zero on a desk')


if __name__ == '__main__':
    unittest.main()
