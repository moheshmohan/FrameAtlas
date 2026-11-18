import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile

from frameatlas.backup import create_backup, restore_backup, read_manifest
from frameatlas.store import Store


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'live')
        self.aid, _ = self.store.upsert({'provider': 'google_photos', 'source_id': 'id',
                                       'filename': 'family.mov', 'kind': 'video',
                                       'local_path': '/old-machine/original.mov'})
        self.store.analysis(self.aid, [{'start': 0, 'end': 1, 'caption': 'Family by the sea',
                                       'details': {}}], {}, {'speech_status': 'no_speech'})
        self.preview = self.aid + '.jpg'
        (self.store.root / 'thumbnails').mkdir(exist_ok=True)
        (self.store.root / 'thumbnails' / self.preview).write_bytes(b'original synthetic preview')
        with self.store.connect() as db:
            db.execute('UPDATE assets SET thumbnail=? WHERE id=?', (self.preview, self.aid))
            db.execute("INSERT INTO settings VALUES('paused','false')")
        (self.store.root / 'credentials.json').write_text('must not be copied')
        self.archive = self.root / 'backup.zip'

    def tearDown(self):
        self.tmp.cleanup()

    def rewrite(self, mutate):
        with zipfile.ZipFile(self.archive) as z:
            entries = {name: z.read(name) for name in z.namelist()}
        mutate(entries)
        changed = self.root / 'changed.zip'
        with zipfile.ZipFile(changed, 'w') as z:
            for name, body in entries.items():
                z.writestr(name, body)
        return changed

    def test_live_wal_roundtrip_search_and_previews_without_credentials(self):
        with self.store.connect() as live:
            live.execute("INSERT INTO ideas(prompt,result,created) VALUES('travel','saved idea',1)")
            live.commit()
            receipt = create_backup(self.archive, self.store.root)
        self.assertEqual(receipt['counts']['assets'], 1)
        with zipfile.ZipFile(self.archive) as z:
            manifest = read_manifest(z)
            self.assertNotIn('credentials.json', z.namelist())
            self.assertIn('restore_frameatlas.py', z.namelist())
        restored = self.root / 'restored'
        result = restore_backup(self.archive, restored)
        self.assertTrue(result['queue_paused'])
        fresh = Store(restored)
        self.assertEqual(fresh.search(query='Family')['total'], 1)
        self.assertTrue(fresh.queue_state()['paused'])
        self.assertEqual(fresh.get(self.aid)['local_path'], '')
        self.assertEqual((restored / 'thumbnails' / self.preview).read_bytes(), b'original synthetic preview')
        self.assertEqual(manifest['counts']['ideas'], 1)
        self.assertFalse((restored / 'credentials.json').exists())
        self.assertFalse(self.store.queue_state()['paused'])

    def test_existing_backup_and_destination_never_overwritten(self):
        create_backup(self.archive, self.store.root)
        before = self.archive.read_bytes()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            create_backup(self.archive, self.store.root)
        self.assertEqual(self.archive.read_bytes(), before)
        dest = self.root / 'existing'
        dest.mkdir()
        (dest / 'work').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            restore_backup(self.archive, dest)
        self.assertEqual((dest / 'work').read_text(), 'keep')

    def test_tampered_payload_fails_without_publishing_destination(self):
        create_backup(self.archive, self.store.root)
        name = 'thumbnails/' + self.preview
        bad = self.rewrite(lambda entries: entries.__setitem__(name, b'x' * len(entries[name])))
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            restore_backup(bad, self.root / 'bad')
        self.assertFalse((self.root / 'bad').exists())

    def test_path_traversal_undeclared_and_future_version_rejected(self):
        create_backup(self.archive, self.store.root)
        def traversal(entries):
            m = json.loads(entries['manifest.json'])
            m['files'][0]['path'] = '../escape'
            entries['manifest.json'] = json.dumps(m).encode()
        for mutation in (traversal, lambda e: e.__setitem__('../escape', b'x'),
                         lambda e: e.__setitem__('manifest.json', json.dumps({'format': 'frameatlas-backup', 'version': 999}).encode())):
            with self.subTest(mutation=mutation):
                bad = self.rewrite(mutation)
                with self.assertRaises(ValueError):
                    restore_backup(bad, self.root / 'bad')
                self.assertFalse((self.root / 'bad').exists())
                self.assertFalse((self.root / 'escape').exists())

    def test_invalid_sqlite_even_with_matching_checksum_rejected(self):
        create_backup(self.archive, self.store.root)
        def corrupt(entries):
            entries['catalog.sqlite3'] = b'not a database'
            m = json.loads(entries['manifest.json'])
            item = next(x for x in m['files'] if x['path'] == 'catalog.sqlite3')
            item.update(bytes=len(entries['catalog.sqlite3']), sha256=hashlib.sha256(entries['catalog.sqlite3']).hexdigest())
            entries['manifest.json'] = json.dumps(m).encode()
        bad = self.rewrite(corrupt)
        with self.assertRaises(sqlite3.Error):
            restore_backup(bad, self.root / 'bad')
        self.assertFalse((self.root / 'bad').exists())

    def test_database_only_and_explicit_same_machine_paths(self):
        create_backup(self.archive, self.store.root, include_previews=False)
        restore_backup(self.archive, self.root / 'restored', keep_local_paths=True)
        self.assertEqual(Store(self.root / 'restored').get(self.aid)['local_path'], '/old-machine/original.mov')
        self.assertFalse((self.root / 'restored' / 'thumbnails').exists())

    def test_restore_does_not_resume_old_retrieval_or_running_job(self):
        with self.store.connect() as d:
            d.execute("INSERT INTO jobs(asset_id,state,task,created,updated) VALUES(?,'running','analysis',1,1)", (self.aid,))
            d.execute("INSERT INTO jobs(asset_id,state,task,created,updated) VALUES(?,'queued','retrieve',1,1)", (self.aid,))
        create_backup(self.archive, self.store.root)
        restore_backup(self.archive, self.root / 'restored')
        with sqlite3.connect(self.root / 'restored/catalog.sqlite3') as d:
            self.assertEqual(dict(d.execute('SELECT task,state FROM jobs')), {'analysis': 'failed', 'retrieve': 'cancelled'})

    def test_embedded_restore_runs_without_a_frameatlas_install(self):
        create_backup(self.archive, self.store.root)
        script = self.root / 'standalone.py'
        with zipfile.ZipFile(self.archive) as z:
            script.write_bytes(z.read('restore_frameatlas.py'))
        result = subprocess.run([sys.executable, '-I', str(script), 'restore', str(self.archive),
                                 '--to', str(self.root / 'portable')],
                                cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['queue_paused'])
        self.assertTrue((self.root / 'portable/catalog.sqlite3').is_file())


if __name__ == '__main__':
    unittest.main()
