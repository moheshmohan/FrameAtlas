import base64
import json
from pathlib import Path
import struct
import tempfile
import unittest
from types import SimpleNamespace
from frameatlas.spherical import windows,render_png,describe_directions,DIRECTIONS,MAX_WINDOWS,BATCH_WINDOWS,MAX_FRAME_BYTES,MAX_RENDER_BYTES
from frameatlas.store import Store
from frameatlas.worker import Worker,Cache,ANALYSIS_RESERVE
from unittest.mock import patch


PNG=b'\x89PNG\r\n\x1a\n'+struct.pack('>I',13)+b'IHDR'+struct.pack('>II',512,512)+b'fixture'


class NativeFixture:
    def __init__(self,path):self.work=path;self.calls=[];self.payload=PNG;self.cancel=False;self.live=[];self.peak=0;self.item_id=10
    def cancelled(self):return self.cancel
    def rpc(self,method,args):
        return {'content':[{'type':'image','mimeType':'image/png','data':base64.b64encode(self.payload).decode()}]}
    def tool(self,name,args):
        self.peak=max(self.peak,len(list(self.work.glob('*.png'))))
        ids=list(range(self.item_id,self.item_id+len(args['paths'])));self.item_id+=len(ids);self.live+=ids
        return {'items':ids,'errors':[]}
    def run(self,name,args=None):
        self.calls.append((name,args))
        if name=='timeline.place':return {'clips':[4],'reframed360':[4]}
        if name=='project.delete':
            self.live=[i for i in self.live if i not in args['items']]
            return {'items':len(args['items']),'offline':0}
        if name=='media.scenes':return {'shots':[{'description':{'caption':'Observed view '+str(args['item'])}}]}
        return {}


class SphericalTests(unittest.TestCase):
    def test_window_plan_covers_source_and_bounds_long_or_hostile_input(self):
        for duration in (0.01,20,31.2,3600,24*3600):
            plan=windows(duration,'video')
            self.assertLessEqual(len(plan),MAX_WINDOWS);self.assertEqual(plan[0]['start'],0)
            self.assertAlmostEqual(plan[-1]['end'],duration)
            for a,b in zip(plan,plan[1:]):self.assertAlmostEqual(a['end'],b['start'])
            self.assertTrue(all(p['start']<=p['at']<p['end'] for p in plan))
        self.assertEqual(windows(0,'photo'),[{'start':0,'end':0,'at':0}])
        for duration in (0,-1,float('nan'),float('inf'),'bad',None,1e100):
            with self.subTest(duration=duration),self.assertRaises(ValueError):windows(duration,'video')

    def test_native_camera_samples_six_distinct_angles_without_copying_original(self):
        with tempfile.TemporaryDirectory() as d:
            n=NativeFixture(Path(d));scenes,report,preview=describe_directions(n,1,{'duration':31.2*254016000000},'video','fast',lambda s:None)
            self.assertEqual(len(scenes),2);self.assertEqual(report['frames_described'],12)
            self.assertEqual(len({(v['yaw'],v['pitch']) for v in scenes[0]['views']}),6)
            self.assertEqual(len(list(Path(d).glob('*.png'))),1);self.assertEqual(n.live,[])
            self.assertTrue((Path(d)/preview).is_file())
            self.assertTrue(any(name=='media.toggleProxies' and args=={'enabled':True} for name,args in n.calls))
            self.assertTrue(all('sample_key' in s for s in scenes))

    def test_long_source_streams_batches_and_keeps_only_one_preview(self):
        with tempfile.TemporaryDirectory() as d:
            n=NativeFixture(Path(d))
            scenes,report,preview=describe_directions(n,1,{'duration':1799.798*254016000000},'video','fast',lambda s:None)
            self.assertEqual(len(scenes),90);self.assertEqual(report['frames_described'],540)
            self.assertEqual(report['batches'],6);self.assertLessEqual(report['sample_spacing_seconds'],20)
            self.assertEqual(len({s['sample_key'] for s in scenes}),90)
            self.assertLessEqual(n.peak,BATCH_WINDOWS*6+1);self.assertEqual(n.live,[])
            self.assertEqual(list(Path(d).glob('*.png')),[Path(d)/preview])
            self.assertLessEqual(report['peak_temporary_frame_bytes'],MAX_RENDER_BYTES)
            self.assertGreater(report['temporary_frame_bytes'],report['peak_temporary_frame_bytes'])
            self.assertLessEqual(MAX_RENDER_BYTES,ANALYSIS_RESERVE)

    def test_failed_second_batch_removes_frames_and_retains_no_partial_preview(self):
        with tempfile.TemporaryDirectory() as d:
            n=NativeFixture(Path(d));run=n.run;calls=0
            def fail(name,args=None):
                nonlocal calls
                if name=='media.analyzeScenes':
                    calls+=1
                    if calls==2:raise RuntimeError('local vision failure')
                return run(name,args)
            n.run=fail
            with self.assertRaisesRegex(RuntimeError,'local vision failure'):
                describe_directions(n,1,{'duration':400*254016000000},'video','fast',lambda s:None)
            self.assertEqual(list(Path(d).glob('*.png')),[])

    def test_failed_native_release_stops_before_rendering_another_batch(self):
        with tempfile.TemporaryDirectory() as d:
            n=NativeFixture(Path(d));run=n.run
            n.run=lambda name,args=None: {'items':0} if name=='project.delete' else run(name,args)
            with self.assertRaisesRegex(RuntimeError,'could not be released'):
                describe_directions(n,1,{'duration':400*254016000000},'video','fast',lambda s:None)
            self.assertEqual(len(n.live),BATCH_WINDOWS*6)
            self.assertEqual(list(Path(d).glob('*.png')),[])

    def test_bad_image_dimensions_and_cancellation_do_not_publish_frames(self):
        with tempfile.TemporaryDirectory() as d:
            n=NativeFixture(Path(d));out=Path(d)/'sample.png'
            for data in (b'not png',PNG[:16]+struct.pack('>II',100000,100000)):
                n.payload=data
                with self.assertRaises(ValueError):render_png(n,0,out)
                self.assertFalse(out.exists())
            n.cancel=True
            with self.assertRaisesRegex(RuntimeError,'cancelled'):render_png(n,0,out)

    def test_original_is_imported_instead_of_uncalibrated_proxy(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'camera.insv';source.write_bytes(b'original');proxy=root/'camera.lrv';proxy.write_bytes(b'proxy')
            worker=object.__new__(Worker);worker.cache=Cache(root)
            asset={'local_path':str(source),'metadata':{'is_360':True},'locator':{'proxy':str(proxy)}}
            result,note=worker.source(asset,root,1)
            self.assertEqual(result,source);self.assertIn('native engine',note)
            self.assertEqual(source.read_bytes(),b'original')

    def test_cloud_analysis_reserves_frame_budget_but_edit_retrieval_can_use_it(self):
        with tempfile.TemporaryDirectory() as d:
            worker=object.__new__(Worker);worker.cache=Cache(Path(d));worker.store=type('S',(),{'root':Path(d)})();worker.cancelled=lambda _:False
            asset={'provider':'google_photos','source_id':'id','kind':'video','filename':'v.mp4','bytes':0,'metadata':{},'locator':{'session_id':'s'}}
            budgets=[]
            worker.analysis_downloads=type('D',(),{'fetch':lambda _,session,source,dest,budget,cancelled,*,batch_limit=None:budgets.append(budget)})()
            with patch.object(worker.cache,'budget',return_value=1024**3),patch('frameatlas.worker.broker',side_effect=lambda *args,**kwargs:budgets.append(args[-1])):
                worker.source(asset,Path(d),1)
                worker.source(asset,Path(d),1,for_analysis=False)
            self.assertEqual(budgets,[1024**3-ANALYSIS_RESERVE,1024**3])

    def test_directional_search_and_corrections_have_independent_identities(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d));aid=store.upsert({'provider':'local','source_id':'sphere','filename':'sphere.insv','kind':'video'})[0]
            base={'start':0,'end':10,'description':{'caption':'Native panorama'}}
            sample={'start':0,'end':10,'sample_time':5,'sample_key':'360-v1:0:10','views':[
                {'label':'Up','yaw':0,'pitch':90,'at':5,'description':{'caption':'A red bicycle'}},
                {'label':'Right','yaw':90,'pitch':0,'at':5,'description':{'caption':'A blue bicycle'}},
            ],'description':{'caption':'A red bicycle and a blue bicycle'}}
            provenance={'speech_status':'no_audio','coverage_warning':'Sampled directions'}
            store.analysis(aid,[base,sample],{},dict(provenance))
            self.assertEqual(store.search('bicycle')['items'][0]['matches'][0]['start'],5)
            self.assertEqual(store.search('bicycle')['items'][0]['matches'][0]['directions'][0]['pitch'],90)
            self.assertTrue(store.search('bicycle')['items'][0]['matches'][0]['caption'].startswith('Up:'))
            match=store.search('red bicycle')['items'][0]['matches'][0]
            self.assertEqual([view['label'] for view in match['directions']], ['Up'])
            store.correct_scene(aid,0,'Owner panorama correction')
            store.analysis(aid,[dict(base),dict(sample)],{},dict(provenance))
            result=store.get(aid)['scenes'];self.assertEqual(result[0]['caption'],'Owner panorama correction')
            self.assertIn('bicycle',result[1]['caption'])
            store.correct_scene(aid,1,'A corrected side view')
            store.analysis(aid,[{'start':0,'end':11,'description':{'caption':'Changed native boundaries'}},dict(sample)],{},dict(provenance))
            result=store.get(aid)
            self.assertTrue(any(s['caption']=='Owner panorama correction' and s['details'].get('retained_editor_scene') for s in result['scenes']))
            self.assertTrue(any(s['caption']=='A corrected side view' for s in result['scenes']))

    def test_promoted_analysis_preserves_queue_options_and_retrieval_precedence(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d));aids=[store.upsert({'provider':'local','source_id':str(i),'filename':str(i)+'.mp4','kind':'video'})[0] for i in range(3)]
            store.enqueue(aids[0]);second=store.enqueue(aids[1],{'language':'ml','model':'whisper-medium-ml'})
            self.assertEqual(store.enqueue(aids[1],{'priority':True})['id'],second['id'])
            self.assertEqual(json.loads(store.job(second['id'])['options'])['language'],'ml')
            self.assertEqual(store.next_job()['id'],second['id'])
            retrieval=store.enqueue_retrieval(aids[2]);self.assertEqual(store.next_job()['id'],retrieval['id'])
            with self.assertRaises(ValueError):store.enqueue(aids[2],{'priority':'yes'})

    def test_priority_studio_cloud_job_can_be_staged_before_promoted_bulk_analysis(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            google, _ = store.upsert({'provider': 'google_photos', 'source_id': 'bulk',
                                      'filename': 'bulk.mp4', 'kind': 'video',
                                      'locator': {'session_id': 'selected'}})
            studio, _ = store.upsert({'provider': 'insta360', 'source_id': 'studio',
                                      'filename': 'studio.insv', 'kind': 'video'})
            bulk = store.enqueue(google, {'priority': True})
            manual = store.enqueue(studio)
            self.assertEqual(store.next_job()['id'], bulk['id'])
            store.enqueue(studio, {'priority': True})
            self.assertEqual(store.next_job()['id'], manual['id'])
            retrieval = store.enqueue_retrieval(google)
            self.assertEqual(store.next_job()['id'], retrieval['id'])

    def test_failed_preview_conversion_keeps_the_prior_preview(self):
        class NativeStub:
            def __init__(self,*args):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def analyze(self,*args):return ([{'start':0,'end':1,'description':{'caption':'New description'}}],{},
                {'speech_status':'no_audio','media_kind':'video','is_360':False,'_preview_file':'native-preview.png'})
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source.mp4';source.write_bytes(b'original')
            store=Store(root/'data');aid=store.upsert({'provider':'local','source_id':'id','filename':'source.mp4','kind':'video','local_path':str(source)})[0]
            thumbs=store.root/'thumbnails';thumbs.mkdir();old=thumbs/(aid+'.jpg');old.write_bytes(b'existing preview')
            store.analysis(aid,[{'start':0,'end':1,'description':{'caption':'Old'}}],{}, {'speech_status':'no_audio'},old.name)
            worker=Worker(store)
            def failed(command,**kwargs):Path(command[-1]).write_bytes(b'incomplete replacement');return SimpleNamespace(returncode=1)
            try:
                job=store.enqueue(aid)
                with patch('frameatlas.worker.Native',NativeStub),patch('frameatlas.worker.subprocess.run',side_effect=failed):worker.run_job(store.job(job['id']))
                self.assertEqual(store.job(job['id'])['state'],'done')
                self.assertEqual(old.read_bytes(),b'existing preview')
                self.assertEqual(store.get(aid)['thumbnail'],old.name)
                self.assertEqual(list(thumbs.glob('*.next-*')),[])
            finally:worker.close()

    def test_unverified_360_source_does_not_publish_a_raw_preview(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / 'source.insp'
            source.write_bytes(b'original')
            store = Store(root / 'data')
            aid, _ = store.upsert({'provider': 'insta360', 'source_id': 'photo',
                                   'filename': source.name, 'kind': 'photo',
                                   'local_path': str(source), 'metadata': {'is_360': True}})
            scenes = [{'start': 0, 'end': 1, 'description': {'caption': 'Park'}}]
            raw = aid + '.jpg'
            store.analysis(aid, scenes, {}, {'speech_status': 'not_applicable', 'is_360': True}, raw)
            self.assertEqual(store.get(aid)['thumbnail'], '')
            self.assertEqual(store.search('Park')['items'][0]['thumbnail'], '')
            worker = Worker(store)
            try:
                jid = store.enqueue(aid)['id']
                self.assertTrue(store.claim_job(jid))
                with worker.cache.job() as work, patch('frameatlas.worker.subprocess.run') as render:
                    (work / 'native-preview.png').write_bytes(b'raw')
                    worker.save_analysis(store.get(aid), work, jid, source, 'local', scenes, {},
                                         {'speech_status': 'not_applicable',
                                          'media_kind': 'photo', '_preview_file': 'native-preview.png'})
                    render.assert_not_called()
                asset = store.get(aid)
                self.assertEqual(asset['status'], 'partial')
                self.assertEqual(asset['thumbnail'], '')
                self.assertIn('No calibrated 360 preview', asset['analysis']['preview_error'])
                self.assertEqual(asset['scenes'][0]['caption'], 'Park')
                self.assertEqual(source.read_bytes(), b'original')
            finally:
                worker.close()

    def test_cache_usage_tolerates_a_job_finishing_during_stats(self):
        with tempfile.TemporaryDirectory() as d:
            cache=Cache(Path(d));p=cache.root/'gone.png';p.write_bytes(b'disposable')
            with patch.object(Path,'stat',side_effect=FileNotFoundError):self.assertEqual(cache.usage(),0)


if __name__=='__main__':unittest.main()
