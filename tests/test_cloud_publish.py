import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.cloud_publish import digest, publish


class CloudPublishTests(unittest.TestCase):
    def test_atomic_publication_keeps_an_already_open_image_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, out, native = root/'source', root/'broker', root/'native'
            source.write_bytes(b'complete new executable');source.chmod(0o755)
            out.write_bytes(b'old executable');native.write_bytes(b'native fixture')
            with out.open('rb') as old:
                pin = publish(source, out, native, 'rustc fixture')
                self.assertEqual(old.read(), b'old executable')
            self.assertEqual(out.read_bytes(), b'complete new executable')
            self.assertEqual(out.stat().st_mode & 0o777, 0o755)
            self.assertEqual(pin['broker_sha256'], digest(out))
            self.assertEqual(json.loads((root/'build-provenance.json').read_text()), pin)
            self.assertEqual(list(root.glob('.frameatlas-*-next-*')), [])

    def test_failed_copy_cleans_only_staging_and_retains_the_live_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);source, out, native = root/'source', root/'broker', root/'native'
            source.write_bytes(b'new');out.write_bytes(b'old');native.write_bytes(b'native')
            receipt = root/'build-provenance.json';receipt.write_text('old receipt')
            def fail_copy(source, destination):
                Path(destination).write_bytes(b'partial')
                raise OSError('No space left')
            with patch('scripts.cloud_publish.shutil.copy2', side_effect=fail_copy):
                with self.assertRaisesRegex(OSError, 'No space'):
                    publish(source, out, native, 'rustc fixture')
            self.assertEqual(out.read_bytes(), b'old')
            self.assertEqual(receipt.read_text(), 'old receipt')
            self.assertEqual(list(root.glob('.frameatlas-*-next-*')), [])

    def test_failed_provenance_check_never_replaces_the_live_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);source, out = root/'source', root/'broker'
            source.write_bytes(b'new');out.write_bytes(b'old')
            with self.assertRaises(FileNotFoundError):
                publish(source, out, root/'missing-native', 'rustc fixture')
            self.assertEqual(out.read_bytes(), b'old')
            self.assertEqual(list(root.glob('.frameatlas-*-next-*')), [])


if __name__ == '__main__':
    unittest.main()
