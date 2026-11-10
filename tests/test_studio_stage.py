from itertools import count
import json
import os
from pathlib import Path
import plistlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from frameatlas.store import Store
from frameatlas.studio_stage import (create,release,record,reservation,owned_work,files_named,matching_mount,
                                    acquire,confirm,cleanup,guard_previous_destinations,work_removed,
                                    StageBusy,DEFAULT_QUOTA,OVERHEAD,IMAGE,MOUNT,RECORD,MIB)
from frameatlas.worker import Cache, Worker
from frameatlas.retrieval import handoff


class StudioStageTests(unittest.TestCase):
    def test_early_release_checks_confirmation_and_package_identity(self):
        from frameatlas.studio_stage import member_confirmation, retire_recording, retirement_plan

        with tempfile.TemporaryDirectory() as directory:
            work = self.work(Path(directory))
            mount = work / MOUNT
            name = 'VID_20251018_152507_00_043.insv'
            folder = mount / (Path(name).stem + '-Original')
            folder.mkdir(parents=True)
            original = folder / name
            proxy = folder / 'LRV_20251018_152507_01_043.lrv'
            original.write_bytes(b'original')
            proxy.write_bytes(b'proxy')
            parts = [{'path': str(original), 'proxy': str(proxy)}]
            unrelated = folder / 'extra.txt'
            unrelated.touch()
            self.assertIsNone(retirement_plan(mount, name, parts))
            unrelated.unlink()
            plan = retirement_plan(mount, name, parts)
            with patch('frameatlas.studio_stage.matching_mount', return_value=True):
                self.assertEqual(retire_recording(work, 1, plan), 0)
                member_confirmation(work, 1).touch()
                proxy.unlink()
                proxy.write_bytes(b'other')
                self.assertEqual(retire_recording(work, 1, plan), 0)
                self.assertTrue(original.is_file())
                current = retirement_plan(mount, name, parts)
                self.assertEqual(retire_recording(work, 1, current), len(b'originalother'))
                self.assertEqual(list(folder.iterdir()), [])

    def test_interactive_studio_batch_defaults_to_priority_and_respects_opt_out(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            google, _ = store.upsert({'provider': 'google_photos', 'source_id': 'google',
                                      'filename': 'google.mov', 'kind': 'video',
                                      'locator': {'session_id': 'selected'}})
            google_job = store.enqueue(google, {'priority': True})['id']
            camera = []
            for counter in range(10, 14):
                name = f'VID_20251018_152507_00_{counter:03d}.insv'
                aid, _ = store.upsert({'provider': 'insta360', 'source_id': name,
                                       'filename': name, 'kind': 'video'})
                camera.append(aid)
            deferred = store.enqueue_studio_batch(camera[:2], {'priority': False})['jobs']
            interactive = store.enqueue_studio_batch(camera[2:])['jobs']
            retrieval = store.enqueue_retrieval(google)['id']
            self.assertEqual([job['id'] for job in store.next_jobs()],
                             [retrieval, *(job['id'] for job in interactive),
                              google_job, *(job['id'] for job in deferred)])

    def test_batch_confirm_requires_all_names_and_peer_cleanup_waits(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            s = Store(root)
            filenames = ['IMG_20251018_152507_00_043.insp', 'IMG_20251018_152627_10_044.insp']
            aids = [s.upsert({'provider': 'insta360', 'source_id': name, 'filename': name, 'kind': 'photo'})[0]
                    for name in filenames]
            with self.assertRaises(ValueError):
                s.enqueue_studio_batch([aids[0], 'missing'])
            self.assertEqual(s.jobs(), [])
            ids = [j['id'] for j in s.enqueue_studio_batch(aids, {'priority': True})['jobs']]
            batch = s.next_job()['studio_batch']
            s.queue_state(True)
            self.assertEqual(s.claim_studio_batch(batch), [])
            s.queue_state(False)
            self.assertEqual([j['id'] for j in s.claim_studio_batch(batch)], ids)
            work = self.work(root)
            mount = work / MOUNT
            mount.mkdir()
            members = [{'id': jid, 'filename': filename} for jid, filename in zip(ids, filenames)]
            for member in members:
                s.job_result(member['id'], {'studio_download': {
                    'work_name': work.name, 'filename': member['filename'], 'batch': members}})
            with patch('frameatlas.studio_stage.matching_mount', return_value=True):
                (mount / filenames[0]).write_bytes(b'first')
                with self.assertRaisesRegex(ValueError, 'Every expected'):
                    confirm(s, ids[0])
                self.assertFalse((work / '.studio-confirmed').exists())
                (mount / filenames[1]).touch()
                with self.assertRaisesRegex(ValueError, 'nonempty'):
                    confirm(s, ids[0])
                self.assertFalse((work / '.studio-confirmed').exists())
                (mount / filenames[1]).write_bytes(b'second')
                self.assertTrue(confirm(s, ids[0])['confirmed'])
            worker = SimpleNamespace(store=s)
            with s.connect() as db:
                db.execute("UPDATE jobs SET state='failed' WHERE id=?", (ids[0],))
            with self.assertRaisesRegex(ValueError, 'every job'):
                cleanup(worker.store, ids[0])
            self.assertTrue(work.exists())
            with s.connect() as db:
                db.execute("UPDATE jobs SET state='failed' WHERE id=?", (ids[1],))
            self.assertTrue(cleanup(s, ids[0])['cleaned'])
            self.assertFalse(work.exists())
            self.assertTrue(all(s.job(jid)['result']['studio_download']['state']=='removed' for jid in ids))

    def test_studio_batch_preserves_valid_item_when_peer_fails(self):
        from frameatlas.studio_batch import run
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d))
            names = ['IMG_20251018_152507_00_043.insp', 'IMG_20251018_152627_10_044.insp',
                     'IMG_20251018_152740_00_045.insp']
            aids = [s.upsert({'provider': 'insta360', 'source_id': n, 'filename': n, 'kind': 'photo'})[0]
                    for n in names]
            ids = [j['id'] for j in s.enqueue_studio_batch(aids)['jobs']]
            w = Worker(s)
            try:
                def transfer(worker, entries, work, stages):
                    for jid in entries:
                        stages[jid] = {'work_name': work.name, 'state': 'waiting'}
                    sources = {}
                    for jid, name in zip(ids[:2], names):
                        p = work / name
                        p.write_bytes(b'original')
                        sources[jid] = (p, [p.name], None)
                    yield sources, {ids[2]: 'Invalid camera trailer'}

                def analyze(asset, work, jid, opts, source, acquisition):
                    self.assertEqual(source.read_bytes(), b'original')
                    if jid == ids[0]:
                        peer = s.job(ids[1])
                        self.assertEqual(peer['stage'], 'Source validated · waiting for local analysis')
                        self.assertEqual(peer['result']['studio_download']['state'], 'ready')
                    s.analysis(asset['id'], [{'start': 0, 'end': 1, 'description': {'caption': 'Park'}}],
                               {}, {'speech_status': 'not_applicable'}, job_id=jid)

                with patch('frameatlas.studio_batch.acquire', side_effect=transfer), \
                        patch.object(w, 'analyze_source', side_effect=analyze):
                    run(w, s.next_job()['studio_batch'])
                self.assertEqual([s.job(jid)['state'] for jid in ids], ['done', 'done', 'failed'])
                self.assertIn('Invalid camera trailer', s.job(ids[2])['error'])
                self.assertEqual(s.get(aids[0])['scenes'][0]['caption'], 'Park')
                self.assertEqual(s.get(aids[1])['scenes'][0]['caption'], 'Park')
                self.assertFalse(s.get(aids[2])['scenes'])
                self.assertEqual(list(w.cache.root.iterdir()), [])
            finally:
                w.close()

    def test_confirm_and_acquire_completed_peer_after_failed_transfer_is_cancelled(self):
        from frameatlas.studio_batch import acquire as acquire_batch
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d))
            names = ['IMG_20251018_152507_00_043.insp', 'IMG_20251018_152627_10_044.insp']
            aids = [s.upsert({'provider': 'insta360', 'source_id': n, 'filename': n, 'kind': 'photo'})[0]
                    for n in names]
            ids = [j['id'] for j in s.enqueue_studio_batch(aids)['jobs']]
            s.claim_studio_batch(s.next_job()['studio_batch'])
            work = self.work(s.root)
            mount = work / MOUNT
            mount.mkdir()
            (mount / names[0]).write_bytes(b'completed')
            entries = {jid: s.get(aid) for jid, aid in zip(ids, aids)}
            worker = SimpleNamespace(
                store=s, cancelled=lambda jid: s.job(jid)['state'] == 'cancelling',
                state=lambda *args: None, stop=SimpleNamespace(wait=lambda _: None),
            )

            def finish_studio(_):
                s.cancel(ids[1])
                with patch('frameatlas.studio_stage.matching_mount', return_value=True):
                    confirm(s, ids[0])
                return 0

            with patch('frameatlas.studio_batch.create_for', return_value=(mount, DEFAULT_QUOTA)), \
                    patch('frameatlas.studio_batch.time.monotonic', side_effect=count()), \
                    patch('frameatlas.studio_batch.validate', return_value=({}, {})) as validate:
                worker.stop.wait = finish_studio
                list(acquire_batch(worker, entries, work, {}))
            remaining, paths = validate.call_args.args[2:]
            self.assertEqual(list(remaining), [ids[0]])
            self.assertEqual(paths, {ids[0]: [mount / names[0]]})

    def test_waiting_studio_only_runs_distinct_google_stills_with_reserved_space(self):
        from frameatlas.studio_batch import index_waiting_photo
        from frameatlas.scene_batch import MAX_SOURCE_BYTES
        from frameatlas.worker import ANALYSIS_RESERVE, MIN_FREE
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d))
            ids = []
            for kind in ("video", "photo"):
                aid, _ = s.upsert({"provider": "google_photos", "source_id": kind, "kind": kind,
                                   "filename": kind, "locator": {"session_id": "selected"}})
                ids.append(s.enqueue(aid)["id"])
            w = Worker(s)
            try:
                with patch.object(w.cache, "budget", return_value=ANALYSIS_RESERVE + MAX_SOURCE_BYTES), \
                        patch("frameatlas.studio_batch.shutil.disk_usage") as disk, \
                        patch("frameatlas.studio_batch.run_batch") as batch:
                    threshold = MIN_FREE + DEFAULT_QUOTA + OVERHEAD + ANALYSIS_RESERVE + MAX_SOURCE_BYTES
                    disk.return_value = SimpleNamespace(free=threshold - 1)
                    attempted = set()
                    self.assertFalse(index_waiting_photo(w, attempted, DEFAULT_QUOTA))
                    batch.assert_not_called()
                    disk.return_value = SimpleNamespace(free=threshold)
                    self.assertTrue(index_waiting_photo(w, attempted, DEFAULT_QUOTA))
                    self.assertFalse(index_waiting_photo(w, attempted, DEFAULT_QUOTA))
                    self.assertEqual(attempted, {ids[1]})
                    self.assertEqual(batch.call_args.args[1][0]["id"], ids[1])
                    self.assertTrue(batch.call_args.kwargs["during_studio_wait"])
            finally:
                w.close()

    def test_waiting_studio_groups_stills_and_yields_to_confirmation(self):
        from frameatlas.studio_batch import index_waiting_photo
        from frameatlas.scene_batch import MAX_SOURCE_BYTES
        from frameatlas.worker import ANALYSIS_RESERVE, MIN_FREE

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            jobs = []
            for i in range(4):
                aid, _ = store.upsert({"provider": "google_photos", "source_id": str(i), "kind": "photo",
                                       "filename": f"{i}.jpg", "locator": {"session_id": "selected"}})
                jobs.append(store.enqueue(aid)["id"])
            worker = Worker(store)
            worker.analysis_downloads.paired_photos = True
            worker.analysis_downloads.maximum_parallel_photos = 4
            fitting_budget = ANALYSIS_RESERVE + 3 * MAX_SOURCE_BYTES
            confirmation = False

            def fetch(_session, files, limit, cancelled, *, timeout):
                nonlocal confirmation
                self.assertEqual((len(files), limit, timeout), (3, MAX_SOURCE_BYTES, 20))
                self.assertFalse(cancelled())
                for _, target in files:
                    target.write_bytes(b"sample")
                confirmation = True
                return [{"bytes": 6} for _ in files]

            try:
                with patch.object(worker.cache, "budget", return_value=fitting_budget), \
                        patch("frameatlas.studio_batch.shutil.disk_usage",
                              return_value=SimpleNamespace(free=MIN_FREE + DEFAULT_QUOTA + OVERHEAD + fitting_budget)), \
                        patch.object(worker.analysis_downloads, "fetch_parallel", side_effect=fetch) as transfer, \
                        patch("frameatlas.studio_batch.analyze_staged") as analyze:
                    attempted = set()
                    self.assertFalse(index_waiting_photo(worker, attempted, DEFAULT_QUOTA, lambda: confirmation))
                    self.assertEqual(attempted, set(jobs[:3]))
                    transfer.assert_called_once()
                    analyze.assert_not_called()
                self.assertTrue(all(store.job(j)["state"] == "queued" for j in jobs))
                self.assertEqual(list(worker.cache.root.iterdir()), [])
            finally:
                worker.close()

    def test_completed_member_indexes_before_peer_and_shared_cleanup_requires_confirmation(self):
        from frameatlas.studio_batch import run
        from frameatlas.studio_stage import CONFIRMED

        for confirm_peer in (True, False):
            with self.subTest(confirm_peer=confirm_peer), tempfile.TemporaryDirectory() as d:
                s = Store(Path(d))
                names = ['IMG_20251018_152507_00_043.insp', 'IMG_20251018_152627_10_044.insp']
                aids = [s.upsert({'provider': 'insta360', 'source_id': n, 'filename': n, 'kind': 'photo'})[0]
                        for n in names]
                first, peer = [j['id'] for j in s.enqueue_studio_batch(aids)['jobs']]
                w = Worker(s)
                mounted, analysed = [], []
                try:
                    def create(worker, assets, work, for_analysis):
                        self.metadata(work)
                        mounted.append(work)
                        mount = work / MOUNT
                        mount.mkdir()
                        (mount / names[0]).write_bytes(b'complete first')
                        (mount / names[1]).touch()
                        return mount, DEFAULT_QUOTA

                    def wait(_):
                        if not analysed:
                            self.assertEqual(confirm(s, first, individual=True)['scope'], 'recording')
                            with self.assertRaisesRegex(ValueError, 'nonempty'):
                                confirm(s, peer, individual=True)

                    def validate(mount, quota, entries, paths):
                        return {jid: (items[0], [items[0].name], None) for jid, items in paths.items()}, {}

                    def analyze(asset, work, jid, opts, source, acquisition):
                        analysed.append(jid)
                        if jid == first:
                            self.assertEqual((source.parent / names[1]).stat().st_size, 0)
                            self.assertFalse((mounted[0] / CONFIRMED).exists())
                            if confirm_peer:
                                (source.parent / names[1]).write_bytes(b'complete second')
                                confirm(s, peer, individual=True)
                            else:
                                s.cancel(peer)
                        s.analysis(asset['id'], [{'start': 0, 'end': 0, 'description': {'caption': 'Park'}}],
                                   {}, {'speech_status': 'not_applicable'}, job_id=jid)

                    with patch('frameatlas.studio_batch.create_for', side_effect=create), \
                            patch('frameatlas.studio_batch.validate', side_effect=validate), \
                            patch('frameatlas.studio_batch.time.monotonic', side_effect=count()), \
                            patch('frameatlas.studio_stage.matching_mount', return_value=True), \
                            patch('frameatlas.studio_stage.command') as command, \
                            patch.object(w.stop, 'wait', side_effect=wait), \
                            patch.object(w, 'analyze_source', side_effect=analyze):
                        run(w, s.next_job()['studio_batch'])
                        self.assertEqual(s.job(first)['state'], 'done')
                        self.assertEqual(s.get(aids[0])['scenes'][0]['caption'], 'Park')
                        if confirm_peer:
                            self.assertEqual(analysed, [first, peer])
                            self.assertEqual(s.job(peer)['state'], 'done')
                            self.assertTrue(work_removed(mounted[0]))
                            self.assertTrue(mounted[0].is_file())
                            command.assert_called_once()
                        else:
                            self.assertEqual(analysed, [first])
                            self.assertEqual(s.job(peer)['state'], 'cancelled')
                            self.assertTrue(mounted[0].exists())
                            command.assert_not_called()
                            self.assertTrue(cleanup(s, first)['cleaned'])
                finally:
                    w.close()

    def work(self,root):
        p=root/'cache/job-fixture123';p.mkdir(parents=True);(p/'.frameatlas-owned').write_text('Owned test directory')
        return p
    def metadata(self,work):
        (work/RECORD).write_text(json.dumps({'image':IMAGE,'mount':MOUNT,'quota':DEFAULT_QUOTA,'phase':'ready','created':0}))

    def test_volume_creation_is_bounded_and_records_ownership(self):
        with tempfile.TemporaryDirectory() as d:
            work=self.work(Path(d));calls=[]
            def command(*args,**kwargs):
                calls.append(args)
                if args[0]=='attach':return plistlib.dumps({'system-entities':[{'mount-point':str(work/MOUNT)}]})
                return b''
            with patch('frameatlas.studio_stage.command',side_effect=command):
                mount,quota=create(work,2*1024**3)
            self.assertEqual(quota,DEFAULT_QUOTA);self.assertEqual(mount,(work/MOUNT).resolve())
            self.assertEqual(record(work)['phase'],'ready')
            self.assertEqual(calls[0][:3],('create','-size','1536m'))
            self.assertEqual(reservation(work),DEFAULT_QUOTA+OVERHEAD)

    def test_larger_volume_is_still_bounded_by_capacity_and_host_free_space(self):
        with tempfile.TemporaryDirectory() as d:
            work=self.work(Path(d));calls=[]
            def command(*args,**kwargs):
                calls.append(args)
                return plistlib.dumps({'system-entities':[{'mount-point':str(work/MOUNT)}]}) if args[0]=='attach' else b''
            with patch('frameatlas.studio_stage.command',side_effect=command),patch('frameatlas.studio_stage.shutil.disk_usage',return_value=SimpleNamespace(free=30*1024**3)):
                _,quota=create(work,8*1024**3,limit=7680*MIB)
            self.assertEqual(quota,7680*MIB);self.assertEqual(record(work)['quota'],quota)
            self.assertEqual(calls[0][:3],('create','-size','7680m'))
        with tempfile.TemporaryDirectory() as d:
            work=self.work(Path(d))
            with patch('frameatlas.studio_stage.command') as native,patch('frameatlas.studio_stage.shutil.disk_usage',return_value=SimpleNamespace(free=8*1024**3)):
                with self.assertRaisesRegex(ValueError,'free-space reserve'):create(work,8*1024**3,limit=7680*MIB)
                native.assert_not_called()
                with self.assertRaises(ValueError):create(work,64*1024**3,limit=64*1024**3)

    def test_invalid_record_paths_quotas_and_work_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);work=self.work(root)
            for name in ('../outside','job-x/../../outside','job-'):
                with self.assertRaises(ValueError):owned_work(root,name)
            self.assertEqual(owned_work(root,work.name),work.resolve())
            for quota in (True,-1,1,DEFAULT_QUOTA+1,float('inf')):
                self.metadata(work);r=json.loads((work/RECORD).read_text());r['quota']=quota;(work/RECORD).write_text(json.dumps(r))
                with self.assertRaises(ValueError):record(work)
            self.metadata(work);r=json.loads((work/RECORD).read_text());r['image']='../../original';(work/RECORD).write_text(json.dumps(r))
            with self.assertRaises(ValueError):record(work)

    def test_unrelated_mount_is_never_detached(self):
        with tempfile.TemporaryDirectory() as d:
            work=self.work(Path(d));self.metadata(work);(work/MOUNT).mkdir()
            info=[{'image-path':str(Path(d)/'user-original.sparseimage'),'system-entities':[{'mount-point':str(work/MOUNT)}]}]
            with patch('frameatlas.studio_stage.mounted_images',return_value=info),patch('frameatlas.studio_stage.command') as command:
                with self.assertRaises(StageBusy):release(work)
                command.assert_not_called()
            self.assertTrue(work.exists())

    def test_native_mount_accepts_alias_for_same_verified_image(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            work = self.work(root)
            alias = root / 'cache-alias'
            alias.symlink_to(work.parent.resolve(), target_is_directory=True)
            aliased_work = alias / work.name
            aliased_mount = aliased_work / MOUNT

            def native(*args, **kwargs):
                if args[0] != 'attach':
                    return b''
                self.assertEqual(args[args.index('-mountpoint') + 1], str((work/MOUNT).resolve()))
                return plistlib.dumps({'system-entities':[{'mount-point':str(aliased_mount)}]})

            with patch('frameatlas.studio_stage.command', side_effect=native):
                mount, _ = create(aliased_work, 2 * 1024**3)
            self.assertEqual(mount, (work/MOUNT).resolve())
            info = [{'image-path':str(aliased_work/IMAGE), 'system-entities':[{'mount-point':str(mount)}]}]
            with patch('frameatlas.studio_stage.mounted_images', return_value=info):
                self.assertTrue(matching_mount(aliased_work))

    def test_busy_volume_is_retained_and_never_force_detached(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);work=self.work(root);self.metadata(work);(work/MOUNT).mkdir();(work/'.studio-confirmed').write_text('confirmed')
            def busy(*args,**kwargs):
                self.assertNotIn('-force',args);raise StageBusy('External writer is still active')
            with patch('frameatlas.studio_stage.matching_mount',return_value=True),patch('frameatlas.studio_stage.command',side_effect=busy):
                with self.assertRaises(StageBusy):Cache(root).remove(work)
                Cache(root).janitor()
            self.assertTrue(work.exists())

    def test_cleanup_error_preserves_original_failure(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Cache(Path(d))
            with patch.object(cache, 'remove', side_effect=StageBusy('Disk is busy')):
                with self.assertRaisesRegex(RuntimeError, 'Native mount returned the wrong destination.*Disk is busy'):
                    with cache.job():
                        raise ValueError('Native mount returned the wrong destination')

    def test_unconfirmed_volume_survives_cancellation_and_restart_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);work=self.work(root);self.metadata(work);(work/MOUNT).mkdir()
            with patch('frameatlas.studio_stage.matching_mount',return_value=True),patch('frameatlas.studio_stage.command') as command:
                with self.assertRaisesRegex(StageBusy,'unconfirmed'):Cache(root).remove(work)
                Cache(root).janitor();command.assert_not_called();self.assertTrue(work.exists())
                Cache(root).remove(work,studio_finished=True)
                self.assertEqual(command.call_args.args[0],'detach');self.assertNotIn('-force',command.call_args.args)
            self.assertTrue(work_removed(work))
            self.assertTrue(work.is_file())
            self.assertEqual(Cache(root).usage(), 0)
            with self.assertRaises(NotADirectoryError):
                (work / MOUNT / 'stale-Original').mkdir(parents=True)

    def test_cache_reserves_the_volume_and_does_not_double_count_its_files(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);work=self.work(root);self.metadata(work);mount=work/MOUNT;mount.mkdir()
            (work/IMAGE).write_bytes(b'sparse backing');(mount/'video.insv').write_bytes(b'payload')
            with patch('frameatlas.worker.os.path.ismount',side_effect=lambda p:Path(p)==mount):size=Cache(root).usage()
            self.assertGreaterEqual(size,DEFAULT_QUOTA+OVERHEAD)
            self.assertLess(size,DEFAULT_QUOTA+OVERHEAD+4096)

    def test_name_matching_ignores_symlinks_and_rejects_ambiguity(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);outside=root/'user.mov';outside.write_bytes(b'user original')
            mount=root/'mounted';mount.mkdir();(mount/'target.insv').symlink_to(outside)
            self.assertIsNone(files_named(mount,'target.insv'))
            (mount/'target.insv').unlink();(mount/'one').mkdir();(mount/'two').mkdir()
            for p in (mount/'one/target.insv',mount/'two/target.insv'):p.write_bytes(b'original')
            with self.assertRaises(ValueError):files_named(mount,'target.insv')
            self.assertEqual(outside.read_bytes(),b'user original')

    def test_confirmation_requires_active_owned_job_and_positive_id(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);s=Store(root);work=self.work(root);self.metadata(work);mount=work/MOUNT;mount.mkdir();name='VID_20260919_113243_00_097.insv';(mount/name).write_bytes(b'finished recording')
            aid=s.upsert({'provider':'insta360','source_id':'cloud','filename':name,'kind':'video'})[0];j=s.enqueue(aid)
            s.job_result(j['id'],{'studio_download':{'work_name':work.name,'filename':name,'state':'waiting'}})
            with self.assertRaises(ValueError):confirm(s,j['id'])
            s.claim_job(j['id'])
            for bad in (True,0,-1,'1',None):
                with self.assertRaises(ValueError):confirm(s,bad)
            with patch('frameatlas.studio_stage.matching_mount',return_value=True):self.assertTrue(confirm(s,j['id'])['confirmed'])
            self.assertTrue((work/'.studio-confirmed').is_file())
            with self.assertRaises(ValueError):cleanup(s,j['id'])

    def test_confirmed_camera_uses_structured_native_folder_scan(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);s=Store(root);work=self.work(root);mount=work/MOUNT;mount.mkdir()
            name='VID_20260919_113243_00_097.insv';source=mount/name;source.write_bytes(b'camera')
            proxy=mount/'LRV_20260919_113243_01_097.lrv';proxy.write_bytes(b'proxy')
            (work/'.studio-confirmed').write_text('Studio reports complete')
            aid=s.upsert({'provider':'insta360','source_id':'cloud','filename':name,'kind':'video'})[0];job=s.enqueue(aid)
            worker=SimpleNamespace(cache=Cache(root),store=s,cancelled=lambda j:False,state=lambda *a:None,stop=SimpleNamespace(wait=lambda t:None))
            def native(*args,**kwargs):
                if args[:2]==('exec','insta360.list'):
                    self.assertEqual(json.loads(args[2]),{'folders':[str(mount)]})
                    return {'recordings':[{'path':str(source),'complete':True,'proxy':str(proxy)}]}
                self.assertEqual(args,('probe',str(source)))
                return {'kind':'Movie'}
            with patch('frameatlas.studio_stage.create',return_value=(mount,DEFAULT_QUOTA)),patch('frameatlas.studio_stage.cli',side_effect=native),patch('frameatlas.studio_stage.probe_media',side_effect=lambda path:native('probe',str(path))),patch('frameatlas.studio_stage.time.monotonic',side_effect=[0,1,2,3,4,6,7,9]):
                asset=s.get(aid);path,note=acquire(worker,asset,work,job['id'])
            self.assertEqual(path,source);self.assertEqual(asset['locator']['proxy'],str(proxy))
            self.assertEqual(s.job(job['id'])['result']['studio_download']['state'],'ready')

    def test_single_studio_wait_indexes_stills_then_yields_to_confirmation(self):
        from frameatlas.studio_stage import CONFIRMED

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = Store(root)
            name = "VID_20260919_113243_00_097.mp4"
            aid, _ = store.upsert({"provider": "insta360", "source_id": "cloud", "filename": name, "kind": "video"})
            job = store.enqueue(aid)["id"]
            worker = Worker(store)
            try:
                work = self.work(root)
                mount = work / MOUNT
                mount.mkdir()

                def index_stills(current, attempted, quota, should_yield):
                    self.assertIs(current, worker)
                    self.assertEqual(quota, DEFAULT_QUOTA)
                    self.assertEqual(attempted, set())
                    self.assertFalse(should_yield())
                    current.cache.cap = 32 * 1024**3
                    (mount / name).write_bytes(b"finished")
                    (work / CONFIRMED).touch()
                    self.assertTrue(should_yield())
                    return True

                original_cap = worker.cache.cap
                with patch("frameatlas.studio_stage.create_for", return_value=(mount, DEFAULT_QUOTA)), \
                        patch("frameatlas.studio_batch.index_waiting_photo", side_effect=index_stills) as index, \
                        patch("frameatlas.studio_stage.probe_media") as probe, \
                        patch.object(worker.stop, "wait"), patch("frameatlas.studio_stage.time.monotonic", side_effect=count()):
                    source, _ = acquire(worker, store.get(aid), work, job)
                    index.assert_called_once()
                    probe.assert_called_once_with(mount / name)
                self.assertEqual(source, mount / name)
                self.assertEqual(worker.cache.cap, original_cap)
                self.assertEqual(store.job(job)["result"]["studio_download"]["state"], "ready")
            finally:
                worker.close()

    def test_inactive_cleanup_is_idempotent_and_only_removes_owned_files(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);s=Store(root);work=self.work(root);self.metadata(work)
            original=root/'user.mov';original.write_bytes(b'user original');(work/'link').symlink_to(original)
            aid=s.upsert({'provider':'insta360','source_id':'cloud','filename':'camera.insv','kind':'video'})[0];j=s.enqueue(aid)
            s.job_result(j['id'],{'studio_download':{'work_name':work.name,'state':'waiting'}});s.cancel(j['id'])
            with patch('frameatlas.studio_stage.mounted_images',return_value=[]):self.assertTrue(cleanup(s,j['id'])['cleaned'])
            self.assertTrue(cleanup(s,j['id'])['already_removed']);self.assertEqual(original.read_bytes(),b'user original')
            self.assertEqual(s.job(j['id'])['result']['studio_download']['state'],'removed')

    def test_retired_studio_paths_are_guarded_without_touching_existing_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            s = Store(root)
            cache = Cache(root)
            vacant = cache.root / 'job-vacant'
            empty = cache.root / 'job-emptyfolder'
            occupied = cache.root / 'job-occupied'
            active = cache.root / 'job-active'
            mismatch = cache.root / 'job-mismatch'
            occupied.mkdir()
            (empty / MOUNT).mkdir(parents=True)
            original = occupied / 'user.mov'
            original.write_bytes(b'unchanged')
            for work, destination, state in (
                    (vacant, vacant / MOUNT, 'removed'),
                    (empty, empty / MOUNT, 'removed'),
                    (occupied, occupied / MOUNT, 'removed'),
                    (active, active / MOUNT, 'waiting'),
                    (mismatch, root / MOUNT, 'removed')):
                aid, _ = s.upsert({'provider': 'insta360', 'source_id': work.name,
                                   'filename': work.name + '.insv', 'kind': 'video'})
                jid = s.enqueue(aid)['id']
                s.job_result(jid, {'studio_download': {'work_name': work.name,
                              'destination': str(destination.resolve()), 'state': state}})
                s.cancel(jid)
            guard_previous_destinations(s, cache.root)
            guard_previous_destinations(s, cache.root)
            self.assertTrue(work_removed(vacant))
            self.assertTrue(vacant.is_file())
            self.assertTrue(empty.is_file())
            self.assertEqual(original.read_bytes(), b'unchanged')
            self.assertFalse(work_removed(occupied))
            self.assertFalse(active.exists())
            self.assertFalse(mismatch.exists())

    def test_camera_edit_handoff_keeps_paired_filenames_and_survives_source_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cache=root/'data';cache.mkdir();source_dir=root/'temporary-source';source_dir.mkdir();dest=root/'editing';dest.mkdir()
            names=['VID_20250101_120000_00_001.insv','VID_20250101_120000_10_001.insv','LRV_20250101_120000_01_001.lrv']
            paths=[source_dir/n for n in names]
            for i,p in enumerate(paths):p.write_bytes(('media '+str(i)).encode())
            asset={'id':'a'*24,'provider':'insta360','source_id':'cloud','filename':names[0],'locator':{'second_lens':str(paths[1]),'proxy':str(paths[2])}}
            with patch('frameatlas.retrieval.media_probe',return_value={'kind':'Movie','video':{'width':1664}}):r=handoff(asset,paths[0],cache,str(dest),temporary=True)
            for p in paths:p.unlink()
            out=[Path(r['path']),*[Path(p) for p in r['related_files']]]
            self.assertEqual([p.name for p in out],names);self.assertTrue(all(p.exists() and dest.resolve() in p.parents for p in out))


if __name__=='__main__':unittest.main()
