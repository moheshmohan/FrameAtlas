import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from frameatlas.store import Store
from frameatlas.importers import import_native
from frameatlas.studio_inventory import validate_rows, import_rows, import_captures, vision_rows


class StudioInventoryTests(unittest.TestCase):
    def preview(self):
        return {"cloud_media_selected":True,"list_view":True,"evidence_sha256":"a"*64,"records":[
            {"filename":"VID_20250720_210019_00_031.insv","created_label":"07/20/2025 21:00"},
            {"filename":"VID_20250720_210019_00_031.insv","created_label":"07/20/2025 21:00"}]}

    def test_visible_rows_are_date_checked_and_deduplicated(self):
        p = self.preview()
        p['records'].extend([{'filename':'../../tokens.json','created_label':''},
            {'filename':'VID_20250231_120000_00_001.insv','created_label':'02/31/2025 12:00'},
            {'filename':'VID_20250720_210019_00_032.insv','created_label':'07/21/2025 21:00'}])
        result = validate_rows(p)
        self.assertEqual(len(result['records']),1)
        self.assertEqual(len(result['rejected']),3)
        self.assertEqual(result['records'][0]['captured'],'2025-07-20T21:00:19')

    def test_noncloud_or_missing_evidence_is_rejected(self):
        p = self.preview(); p['cloud_media_selected'] = False
        with self.assertRaises(ValueError): validate_rows(p)
        with tempfile.TemporaryDirectory() as d:
            p = self.preview(); p.pop('evidence_sha256')
            with self.assertRaises(ValueError): import_rows(Store(Path(d)),p)

    def test_split_recording_preserves_all_parts_and_bounds_input(self):
        p = self.preview()
        name = 'VID_20250820_114517_00_049_050.insv'
        p['records'] = [{'filename':name, 'created_label':'08/20/2025 11:45'},
            {'filename':name.replace('2025','２０２５'), 'created_label':'08/20/2025 11:45'},
            {'filename':'VID_20250820_114517_00_' + '_'.join(['049']*17) + '.insv',
             'created_label':'08/20/2025 11:45'}]
        result = validate_rows(p)
        self.assertEqual([row['filename'] for row in result['records']], [name])
        self.assertEqual(len(result['rejected']), 2)
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            self.assertEqual(import_rows(store, p)['added'], 1)
            asset = store.search('049_050')['items'][0]
            self.assertEqual(asset['locator']['recording_name'], name.rsplit('.',1)[0])

    def test_display_zone_offset_is_retained_without_rewriting_camera_time(self):
        p=self.preview();p['records']=[{'filename':'VID_20251024_060634_00_073.insv','created_label':'10/24/2025 03:06'}]
        row=validate_rows(p)['records'][0]
        self.assertEqual(row['captured'],'2025-10-24T06:06:34')
        self.assertEqual(row['display_offset_minutes'],-180)
        p['records'][0]['created_label']='10/24/2025 03:07'
        self.assertEqual(validate_rows(p)['records'],[])

    def test_native_ocr_pairs_only_unambiguous_rows_with_required_headers(self):
        lines=[{'text':text,'x':0.1,'y':0.9} for text in ('Cloud Media','File Name','Date Created')]
        lines.extend([{'text':'VID_20250720_210019_00_031.insv','x':0.14,'y':0.5},
                      {'text':'07/20/2025 21:00','x':0.85,'y':0.503},
                      {'text':'VID_20250720_210019_00_032.insv','x':0.14,'y':0.3}])
        result=validate_rows(vision_rows({'lines':lines}))
        self.assertEqual(len(result['records']),1)
        with self.assertRaises(ValueError):vision_rows({'lines':lines[1:]})
        lines.append({'text':'07/20/2025 21:00','x':0.85,'y':0.5})
        ambiguous=validate_rows(vision_rows({'lines':lines}))
        self.assertEqual(ambiguous['records'],[])
        self.assertEqual(len(ambiguous['rejected']),2)

    def test_import_is_metadata_only_and_preserves_existing_source(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'data');p=self.preview();original=Path(d)/'original.insv';original.write_bytes(b'user original')
            aid=store.upsert({'provider':'insta360','source_id':'VID_20250720_210019_00_031','filename':p['records'][0]['filename'],'kind':'video','local_path':str(original)})[0]
            result=import_rows(store,p)
            self.assertEqual(result['added'],0)
            self.assertEqual(result['originals_downloaded'],0)
            self.assertEqual(store.get(aid)['local_path'],str(original))
            self.assertTrue(store.get(aid)['metadata']['ocr_unverified'])
            self.assertEqual(original.read_bytes(),b'user original')

    def test_mp4_and_insv_with_the_same_stem_are_distinct_files(self):
        p=self.preview();p['records'].append({'filename':p['records'][0]['filename'].replace('.insv','.mp4'),'created_label':'07/20/2025 21:00'})
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d));self.assertEqual(import_rows(store,p)['added'],2)
            self.assertEqual(import_rows(store,p)['added'],0)
            rows=store.search('')['items'];self.assertEqual(len(rows),2)
            self.assertIsNone(next(r for r in rows if r['filename'].endswith('.mp4'))['metadata']['is_360'])

    def test_capture_batch_is_atomic_and_deduplicates_overlapping_pages(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d));p=self.preview();bad=self.preview();bad.pop('evidence_sha256')
            with self.assertRaises(ValueError):import_captures(store,[p,bad])
            self.assertEqual(store.search('')['total'],0)
            self.assertEqual(import_captures(store,[p,p])['seen'],1)
            with store.connect() as db:self.assertEqual(db.execute('select count(*) from imports').fetchone()[0],1)

    def test_later_studio_download_attaches_to_the_existing_cloud_file(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d));p=self.preview();import_rows(store,p)
            asset=store.search('')['items'][0];store.annotate(asset['id'],['keep this'],'My corrected description')
            name=p['records'][0]['filename'];original=Path(d)/name;original.write_bytes(b'user original')
            recording={'stem':name.rsplit('.',1)[0],'path':str(original),'kind':'video'}
            with patch('frameatlas.importers.cli',side_effect=[[],{'recordings':[recording]}]),patch('frameatlas.importers.recover_photos_metadata',return_value={'seen':0,'added':0}):
                import_native(store)
            self.assertEqual(store.search('')['total'],1)
            updated=store.get(asset['id']);self.assertEqual(updated['local_path'],str(original))
            self.assertEqual(updated['summary'],'My corrected description')
            self.assertEqual(updated['tags'],['keep this'])


if __name__=='__main__':unittest.main()
