import copy
import contextlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from frameatlas.camera_parts import names,source_paths,validated_parts
from frameatlas.multipart import analyze,package
from frameatlas.native import TICKS,probe_timeout,probe_media
from frameatlas.retrieval import handoff
from frameatlas.store import Store
from frameatlas.studio_stage import acquire,confirm,DEFAULT_QUOTA,MOUNT
from frameatlas.worker import Cache

GROUP='VID_20250820_114517_00_049_050.insv'
FIRST='VID_20250820_114517_00_049.insv'
SECOND='VID_20250820_121517_00_050.insv'


class PartsTests(unittest.TestCase):
    def test_large_file_probes_have_a_size_aware_bounded_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'large.insv'
            with p.open('wb') as f:f.truncate(32*1024**3)
            self.assertGreater(probe_timeout(p),500)
            with patch('frameatlas.native.Path.stat',return_value=SimpleNamespace(st_size=1024**4)):
                self.assertEqual(probe_timeout(p),1800)

    def test_streaming_probe_uses_a_disposable_alias_for_extensionless_sources(self):
        with tempfile.TemporaryDirectory() as d:
            original=Path(d)/'original';original.write_bytes(b'user original');seen=[]
            def native(*args,**kwargs):
                self.assertEqual(args[:2],('exec','mediaBrowser.probe'))
                p=Path(json.loads(args[2])['path']);seen.append(p)
                if p==original:return {'media':False,'info':None}
                self.assertTrue(p.is_symlink());self.assertEqual(p.resolve(),original.resolve())
                return {'media':True,'info':{'kind':'Movie','duration':10*TICKS,'file_size':13}}
            with patch('frameatlas.native.cli',side_effect=native):result=probe_media(original)
            self.assertEqual(result['name'],'original');self.assertFalse(seen[-1].exists())
            self.assertEqual(original.read_bytes(),b'user original')

    def test_unrecognized_streaming_source_returns_an_error_without_whole_file_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            original=Path(d)/'corrupt.insv';original.write_bytes(b'corrupt')
            with patch('frameatlas.native.cli',return_value={'media':False,'info':None}) as native:
                with self.assertRaises(RuntimeError):probe_media(original)
                self.assertTrue(all(call.args[:2]==('exec','mediaBrowser.probe') for call in native.call_args_list))
            self.assertEqual(original.read_bytes(),b'corrupt')

    def fixture(self,root):
        folder=root/(Path(GROUP).stem+'-Original');folder.mkdir()
        paths=[folder/FIRST,folder/SECOND]
        for p in paths:p.write_bytes(b'original camera recording')
        asset={'id':'a'*24,'source_id':'group','provider':'insta360','filename':GROUP,'kind':'video',
               'locator':{'recording_parts':[{'path':str(p),'filename':p.name} for p in paths]}}
        return paths,asset

    def test_group_requires_every_unique_part_and_preserves_real_camera_names(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);paths,asset=self.fixture(root)
            self.assertEqual(source_paths(root,GROUP),paths)
            saved=paths[1].read_bytes();paths[1].unlink()
            self.assertEqual(source_paths(root,GROUP),[])
            paths[1].write_bytes(saved)
            self.assertEqual(source_paths(root,GROUP),paths)
            with self.assertRaises(ValueError):names(GROUP.replace('049_050','049_049'))
            extra=paths[0].parent/'copy';extra.mkdir();(extra/FIRST).write_bytes(b'duplicate')
            with self.assertRaises(ValueError):source_paths(root,GROUP)

    def test_group_rejects_symlinks_wrong_initial_time_and_foreign_mounts(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);paths,asset=self.fixture(root);outside=root/'user.insv';outside.write_bytes(b'user')
            paths[1].unlink();paths[1].symlink_to(outside)
            self.assertEqual(source_paths(root,GROUP),[])
            paths[1].unlink();paths[1].write_bytes(b'part')
            with patch('frameatlas.camera_parts.os.path.ismount',return_value=True):
                with self.assertRaises(ValueError):source_paths(root,GROUP)
            paths[0].rename(paths[0].with_name(FIRST.replace('114517','114518')))
            with self.assertRaises(ValueError):source_paths(root,GROUP)
            self.assertEqual(outside.read_bytes(),b'user')

    def test_every_camera_trailer_and_dependency_must_be_valid(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);paths,asset=self.fixture(root)
            records=[{'path':str(p),'complete':True} for p in paths]
            self.assertEqual(len(validated_parts(root,paths,records)),2)
            records[1]['complete']=False
            self.assertEqual(validated_parts(root,paths,records),[])
            records[1].update({'complete':True,'proxy':str(root.parent/'outside.lrv')})
            with self.assertRaises(ValueError):validated_parts(root,paths,records)

    def test_group_confirmation_cannot_accept_just_the_first_file(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);store=Store(root);work=root/'cache/job-group123';work.mkdir(parents=True);(work/'.frameatlas-owned').write_text('owned')
            mount=work/MOUNT;mount.mkdir();paths,asset=self.fixture(mount)
            aid=store.upsert(asset)[0];job=store.enqueue(aid);store.claim_job(job['id'])
            store.job_result(job['id'],{'studio_download':{'work_name':work.name,'filename':GROUP}})
            content=paths[1].read_bytes();paths[1].unlink()
            with patch('frameatlas.studio_stage.matching_mount',return_value=True):
                with self.assertRaises(ValueError):confirm(store,job['id'])
                self.assertFalse((work/'.studio-confirmed').exists())
                paths[1].write_bytes(content);self.assertTrue(confirm(store,job['id'])['confirmed'])

    def test_acquisition_validates_all_raw_parts_before_returning_the_package(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);store=Store(root);work=root/'cache/job-group123';work.mkdir(parents=True);(work/'.frameatlas-owned').write_text('owned')
            mount=work/MOUNT;mount.mkdir();paths,asset=self.fixture(mount);(work/'.studio-confirmed').write_text('Operator confirmed complete')
            aid=store.upsert(asset)[0];job=store.enqueue(aid);worker=SimpleNamespace(cache=Cache(root),store=store,cancelled=lambda j:False,state=lambda *a:None,stop=SimpleNamespace(wait=lambda t:None))
            probed=[]
            def native(*args,**kwargs):
                if args[:2]==('exec','insta360.list'):
                    self.assertEqual(json.loads(args[2]),{'folders':[str(mount)]})
                    return {'recordings':[{'path':str(p),'complete':True} for p in paths]}
                self.assertEqual(args[0],'probe');probed.append(args[1]);return {'kind':'Movie'}
            with patch('frameatlas.studio_stage.create',return_value=(mount,DEFAULT_QUOTA)),patch('frameatlas.studio_stage.cli',side_effect=native),patch('frameatlas.studio_stage.probe_media',side_effect=lambda path:native('probe',str(path))),patch('frameatlas.studio_stage.time.monotonic',side_effect=[0,1,2,3,4,6,7,9]):
                result,note=acquire(worker,asset,work,job['id'])
            self.assertEqual(result,paths[0]);self.assertEqual(probed,[str(p) for p in paths])
            self.assertEqual([p['path'] for p in asset['locator']['recording_parts']],[str(p) for p in paths])
            self.assertEqual(store.job(job['id'])['result']['studio_download']['part_filenames'],[FIRST,SECOND])

    def native(self,fail=False):
        class Fake:
            def __init__(self,work,cancelled):self.work=Path(work)
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def analyze(self,path,kind,options,stage):
                second=path.name==SECOND
                if fail and second:raise RuntimeError('Second part failed')
                scene={'start':0.,'end':10. if not second else 20.,'description':{'caption':'Hotel room'}}
                sample={'start':0.,'end':scene['end'],'sample_key':'window-0','sample_time':5.,'views':[{'label':'Up','at':5.,'description':{'caption':'Ceiling lamp'}}], 'description':{'caption':'Ceiling lamp'}}
                (self.work/'native-preview.png').write_bytes(b'preview')
                return [scene,sample],{'text':'hotel','language':'en','words':[{'text':'hotel','start':1.,'end':2.}]}, {'probe':{'duration':(20 if second else 10)*TICKS},'speech_status':'complete','is_360':True,'camera_proxy_attached':True,'directional_analysis':{'windows':1,'frames_described':6,'frames_missing':0},'_preview_file':'native-preview.png','offline_inference':True,
                                                                                                                   'video_gps_status':'recorded','embedded_gps':{'latitude':10 if second else 11,'longitude':20}}
        return Fake

    def test_analysis_offsets_scenes_speech_and_views_without_losing_source_time(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);paths,asset=self.fixture(root);work=root/'job';work.mkdir()
            scenes,speech,p=analyze(asset,paths[0],work,{},lambda s:None,lambda:False,self.native())
            self.assertEqual(len(scenes),4);self.assertEqual(scenes[2]['start'],10.)
            self.assertEqual(scenes[3]['sample_time'],15.);self.assertEqual(scenes[3]['views'][0]['at'],15.)
            self.assertEqual(scenes[3]['source_part']['sample_time'],5.)
            self.assertEqual(scenes[3]['source_part']['filename'],SECOND)
            self.assertNotEqual(scenes[1]['sample_key'],scenes[3]['sample_key'])
            self.assertEqual(speech['words'][1]['start'],11.);self.assertEqual(speech['words'][1]['source_part']['start'],1.)
            self.assertEqual(p['probe']['duration'],30*TICKS);self.assertEqual(p['directional_analysis']['frames_described'],12)
            self.assertEqual([r['analysis']['file_fingerprint']['bytes'] for r in p['parts']],
                             [path.stat().st_size for path in paths])
            self.assertEqual(p['video_gps_status'], 'part_specific')
            self.assertNotIn('embedded_gps', p)
            self.assertEqual([part['analysis']['embedded_gps']['latitude'] for part in p['parts']], [11, 10])
            self.assertTrue((work/p['_preview_file']).is_file());self.assertEqual(list((work/'cache').iterdir()),[])
            store=Store(root/'catalog')
            aid=store.upsert({**asset, 'local_path': str(paths[0])})[0]
            store.analysis(aid,scenes,speech,p)
            self.assertEqual(store.get(aid)['bytes'],sum(path.stat().st_size for path in paths))
            matches=store.search(query='ceiling')['items'][0]['matches']
            self.assertEqual(matches[1]['start'],15.);self.assertEqual(matches[1]['source_part']['filename'],SECOND)
            self.assertEqual(store.search(query=SECOND)['total'],1)
            from frameatlas.service import Service
            service = Service(store)
            request = {'id': aid, 'scene_key': matches[1]['scene_key'], 'part_filename': SECOND}
            ref = service.dispatch('resolve', request)['clip']
            self.assertEqual(ref['kind'], '360_sample')
            self.assertEqual((ref['start'], ref['end']), (15., 15.))
            self.assertEqual((ref['source_start'], ref['source_end']), (5., 5.))
            self.assertEqual(ref['source_filename'], SECOND)
            self.assertEqual(ref['source_path'], str(paths[1].resolve()))
            with self.assertRaisesRegex(ValueError, 'stale or ambiguous'):
                service.dispatch('resolve', {**request, 'part_filename': FIRST})
            hit = next(m for m in store.search(query='hotel')['items'][0]['matches']
                       if m.get('kind') == 'speech' and m['source_part']['filename'] == SECOND)
            spoken = service.dispatch('resolve', {'id': aid, 'speech_key': hit['speech_key']})['clip']
            self.assertEqual((spoken['start'], spoken['end']), (11., 12.))
            self.assertEqual((spoken['source_start'], spoken['source_end']), (1., 2.))
            self.assertEqual(spoken['source_path'], str(paths[1].resolve()))
            from frameatlas.store import speech_key
            crossing = speech_key(speech['words'], 0, 1)
            with self.assertRaisesRegex(ValueError, 'crosses a camera part'):
                service.dispatch('resolve', {'id': aid, 'speech_key': crossing})
            paths[1].unlink()
            self.assertIsNone(service.dispatch('resolve', request)['clip']['source_path'])

    def test_part_failure_cleans_work_and_missing_original_cannot_be_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);paths,asset=self.fixture(root);work=root/'job';work.mkdir()
            with self.assertRaisesRegex(RuntimeError,'Second part failed'):analyze(asset,paths[0],work,{},lambda s:None,lambda:False,self.native(fail=True))
            self.assertEqual(list((work/'cache').iterdir()),[])
            paths[1].unlink()
            with self.assertRaises(ValueError):package(asset,paths[0])

    def test_editor_handoff_retains_all_parts_and_proxies_after_source_removal(self):
        from frameatlas.fingerprint import digest_source
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);paths,asset=self.fixture(root);dest=root/'editing';dest.mkdir();data=root/'data';data.mkdir()
            first_bytes=paths[0].read_bytes()
            second_bytes=bytes(reversed(first_bytes))
            paths[1].write_bytes(second_bytes)
            asset['analysis']={'parts':[{'filename':p.name,'analysis':{'file_fingerprint':digest_source(p,lambda:False)}} for p in paths]}
            for part in asset['locator']['recording_parts']:
                original=Path(part['path']);proxy=original.with_name(original.name.replace('VID_','LRV_').replace('_00_','_01_')).with_suffix('.lrv')
                proxy.write_bytes(b'proxy');part['proxy']=str(proxy)
            clip={'source_filename':SECOND,'source_path':str(paths[1]),'source_start':5.0,'source_end':5.0}
            with patch('frameatlas.retrieval.media_probe',return_value={'kind':'Movie','video':{'width':512}}):
                with self.assertRaisesRegex(ValueError, 'does not match'):
                    handoff(asset,paths[0],data,str(dest),temporary=True,clip={**clip,'source_filename':'missing.insv'})
                paths[1].write_bytes(first_bytes)
                with self.assertRaisesRegex(ValueError, 'file bytes differ'):
                    handoff(asset,paths[0],data,str(dest),temporary=True,clip=clip)
                self.assertEqual(list(dest.iterdir()),[])
                paths[1].write_bytes(second_bytes)
                result=handoff(asset,paths[0],data,str(dest),temporary=True,clip=clip)
            for p in paths[0].parent.iterdir():p.unlink()
            self.assertEqual(len(result['recording_parts']),2)
            self.assertEqual([p['filename'] for p in result['recording_parts']],[FIRST,SECOND])
            self.assertEqual(result['clip'],{**clip,'source_path':result['recording_parts'][1]['path']})
            self.assertEqual(result['indexed_source_verification'],'matched')
            for part in result['recording_parts']:
                self.assertTrue(Path(part['path']).is_file());self.assertTrue(Path(part['proxy']).is_file())
                self.assertIn(dest.resolve(),Path(part['path']).parents)

    def test_legacy_split_handoff_checks_the_referenced_part(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            paths, asset = self.fixture(root)
            first = {'kind': 'Movie', 'video': {'width': 1920, 'height': 1080}, 'duration': 100}
            second = {**first, 'duration': 200}
            asset['analysis'] = {'source_bytes': sum(p.stat().st_size for p in paths), 'probe': first,
                                 'parts': [{'filename': p.name, 'bytes': p.stat().st_size, 'analysis': {'probe': probe}}
                                           for p, probe in zip(paths, (first, second))]}
            clip = {'source_filename': SECOND, 'source_start': 0.0, 'source_end': 0.0}
            with patch('frameatlas.retrieval.media_probe', side_effect=[first, first]):
                with self.assertRaisesRegex(ValueError, 'duration differs'):
                    handoff(asset, paths[0], root, clip=clip)
            with patch('frameatlas.retrieval.media_probe', side_effect=[first, second]):
                result = handoff(asset, paths[0], root, clip=clip)
            self.assertEqual(result['indexed_source_verification'], 'no_fingerprint')
            self.assertIn('duration', result['indexed_source_properties_checked'])
            self.assertEqual(result['clip']['source_path'], str(paths[1].resolve()))

    def test_story_citations_retain_exact_part_and_local_seconds(self):
        from frameatlas.ideas import generate
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);paths,asset=self.fixture(root);work=root/'job';work.mkdir()
            scenes,speech,p=analyze(asset,paths[0],work,{},lambda s:None,lambda:False,self.native())
            store=Store(root/'catalog');aid=store.upsert(asset)[0];store.analysis(aid,scenes,speech,p)
            result={'ideas':[{'title':'Hotel sequence','hook':'Arriving','outline':['Available hotel shot'],'sources':[{'asset_id':aid,'start':11.,'end':12.,'reason':'Available hotel footage'}]}]}
            response={'message':{'content':json.dumps(result)}}
            with patch('frameatlas.ideas.local_models',return_value=['qwen3:8b']),patch('frameatlas.resources.gpu_lease',return_value=contextlib.nullcontext()),patch('frameatlas.ideas.request_json',return_value=response):
                r=generate(store,'hotel')
            ref=r['ideas'][0]['sources'][0]
            self.assertEqual(ref['source_part']['filename'],SECOND)
            self.assertEqual((ref['source_part']['start'],ref['source_part']['end']),(1.,2.))

    def test_named_only_request_uses_the_named_recording_and_constrained_reply(self):
        from frameatlas.ideas import generate
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);paths,asset=self.fixture(root);work=root/'job';work.mkdir()
            scenes,speech,p=analyze(asset,paths[0],work,{},lambda s:None,lambda:False,self.native())
            store=Store(root/'catalog');aid=store.upsert(asset)[0];store.analysis(aid,scenes,speech,p)
            other=store.upsert({'provider':'local','source_id':'other','filename':'unrelated.mov','kind':'video'})[0]
            store.analysis(other,[{'start':0,'end':10,'description':{'caption':'One restaurant sequence'}}],{'text':'one restaurant'},{'speech_status':'complete'})
            payloads=[]
            def response(path,payload,**kwargs):
                self.assertEqual(path,"/api/chat");payloads.append(payload)
                evidence=json.loads(payload['messages'][1]['content'])['evidence']
                self.assertEqual([a['id'] for a in evidence],[aid])
                self.assertIsInstance(payload['format'],dict);self.assertEqual(payload['format']['properties']['ideas']['maxItems'],3)
                return {'message':{'content':json.dumps({'ideas':[{'title':'Available dinner','hook':'Dinner','outline':['Available scene'],'sources':[{'asset_id':aid,'start':1,'end':2,'reason':'Visible hotel room'}]}]})}}
            with patch('frameatlas.ideas.local_models',return_value=['qwen3:8b']),patch('frameatlas.resources.gpu_lease',return_value=contextlib.nullcontext()),patch('frameatlas.ideas.request_json',side_effect=response):
                r=generate(store,'Suggest one brief restaurant sequence using only '+GROUP+'.')
            self.assertEqual(r['ideas'][0]['sources'][0]['asset_id'],aid)

    def test_explicit_story_sources_exclude_other_files_with_the_same_filename(self):
        from frameatlas.ideas import generate
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            ids = []
            for provider in ("google_photos", "insta360", "local"):
                aid, _ = store.upsert({"provider": provider, "source_id": provider,
                                       "filename": "shared.mp4", "kind": "video"})
                store.analysis(aid, [{"start": 0, "end": 10, "description": {"caption": provider}}],
                               {}, {"speech_status": "complete"})
                ids.append(aid)

            def response(path, payload, **kwargs):
                evidence = json.loads(payload["messages"][1]["content"])["evidence"]
                self.assertEqual([a["id"] for a in evidence], ids[:2])
                return {"message": {"content": json.dumps({"ideas": [
                    {"title": "Scene", "hook": "Open here", "outline": ["shared.mp4"],
                     "sources": [{"asset_id": aid, "start": 0, "end": 1, "reason": "Visible"} for aid in ids]}
                ]})}}

            with patch("frameatlas.ideas.local_models", return_value=["qwen3:8b"]), \
                    patch("frameatlas.resources.gpu_lease", return_value=contextlib.nullcontext()), \
                    patch("frameatlas.ideas.request_json", side_effect=response):
                result = generate(store, "Use shared.mp4", asset_ids=ids[:2])
                self.assertEqual(result["selected_asset_ids"], ids[:2])
                self.assertEqual([s["asset_id"] for s in result["ideas"][0]["sources"]], ids[:2])
                with self.assertRaisesRegex(ValueError, "distinct"):
                    generate(store, "Use shared.mp4", asset_ids=[ids[0], ids[0]])
                with self.assertRaisesRegex(ValueError, "related wording"):
                    generate(store, "Use shared.mp4", expand_search=True, asset_ids=ids[:2])

    def test_truncated_local_reply_is_actionable_and_does_not_save_a_partial_idea(self):
        from frameatlas.ideas import generate
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d));aid=store.upsert({'provider':'local','source_id':'a','filename':'room.mov','kind':'video'})[0]
            store.analysis(aid,[{'start':0,'end':10,'description':{'caption':'A hotel room'}}],{}, {'speech_status':'complete'})
            with patch('frameatlas.ideas.local_models',return_value=['qwen3:8b']),patch('frameatlas.resources.gpu_lease',return_value=contextlib.nullcontext()),patch('frameatlas.ideas.request_json',return_value={'message':{'content':'{"ideas":['}}):
                with self.assertRaisesRegex(ValueError,'No idea was saved'):generate(store,'hotel')
            with store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM ideas').fetchone()[0],0)

    def test_ideas_align_words_to_half_open_scene_ranges(self):
        from frameatlas.ideas import generate
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            aid, _ = store.upsert({'provider':'local','source_id':'trail','filename':'trail.mov','kind':'video'})
            scenes = [{'start':i,'end':i+1,'description':{'caption':'A trail'}} for i in range(2)]
            words = [{'text':text,'start':start,'end':start+.1}
                     for text,start in [('river',1),('walk',0),('trees',.7),('after',2)]]
            store.analysis(aid,scenes,{'text':'walk trees river after','words':words},{'speech_status':'complete'})

            def response(path, payload, **kwargs):
                self.assertEqual(path, "/api/chat")
                evidence = json.loads(payload['messages'][1]['content'])['evidence']
                self.assertEqual([s['speech'] for s in evidence[0]['scenes']], ['walk trees','river'])
                idea = {'title':'Trail','hook':'Follow the trail','outline':['trail.mov shows a trail'],
                        'sources':[{'asset_id':aid,'start':0,'end':1,'reason':'First trail scene'}]}
                return {'message':{'content':json.dumps({'ideas':[idea]})}}

            with patch('frameatlas.ideas.local_models',return_value=['qwen3:8b']), \
                    patch('frameatlas.resources.gpu_lease',return_value=contextlib.nullcontext()), \
                    patch('frameatlas.ideas.request_json',side_effect=response):
                self.assertEqual(generate(store,'Use only trail.mov')['ideas'][0]['sources'][0]['asset_id'],aid)

    def test_ideas_cite_directional_evidence_only_at_its_sample_time(self):
        from frameatlas.ideas import generate
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            aid, _ = store.upsert({'provider': 'insta360', 'source_id': 'sample',
                                   'filename': 'pano.insv', 'kind': 'video'})
            store.analysis(aid, [{'start': 0, 'end': 20, 'sample_time': 10, 'sample_key': 'sample',
                                 'description': {'caption': 'A red bicycle'}}],
                           {'text': 'nearby', 'words': [{'start': 1, 'end': 2, 'text': 'nearby'}]},
                           {'speech_status': 'complete', 'coverage_warning': 'Directional sample'})

            def response(path, payload, **kwargs):
                evidence = json.loads(payload['messages'][1]['content'])['evidence'][0]['scenes'][0]
                self.assertEqual((evidence['start'], evidence['end'], evidence['speech']), (10, 10, ''))
                idea = {'title': 'Bicycle', 'hook': 'Open on the bicycle', 'outline': ['pano.insv at 10s'],
                        'sources': [{'asset_id': aid, 'start': a, 'end': b, 'reason': 'Red bicycle'}
                                    for a, b in ((4, 12), (10.0004, 10.0004))]}
                return {'message': {'content': json.dumps({'ideas': [idea]})}}

            with patch('frameatlas.ideas.local_models', return_value=['qwen3:8b']), \
                    patch('frameatlas.resources.gpu_lease', return_value=contextlib.nullcontext()), \
                    patch('frameatlas.ideas.request_json', side_effect=response):
                refs = generate(store, 'Use only pano.insv')['ideas'][0]['sources']
            self.assertEqual([(r['start'], r['end'], r['sample_time']) for r in refs], [(10, 10, 10)])
            self.assertEqual(refs[0]['scene_key'], 'sample:sample')


if __name__=='__main__':unittest.main()
