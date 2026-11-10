import subprocess
import tempfile
from pathlib import Path
import unittest
from frameatlas.studio_inventory import VISION_HELPER


@unittest.skipUnless(VISION_HELPER.is_file(), 'Optional macOS OCR helper is not built')
class VisionHelperTests(unittest.TestCase):
    def test_bad_arguments_corrupt_image_and_oversized_file_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            corrupt=Path(d)/'broken.png';corrupt.write_bytes(b'not an image')
            large=Path(d)/'large.jpg'
            with large.open('wb') as output:output.truncate(6*1024**2+1)
            for args in ([],[str(corrupt)],[str(large)]):
                with self.subTest(args=args):
                    proc=subprocess.run([str(VISION_HELPER),*args],capture_output=True,timeout=10)
                    self.assertEqual(proc.returncode,1)
                    self.assertIn(b'Local OCR failed:',proc.stderr)
                    self.assertEqual(proc.stdout,b'')


if __name__=='__main__':unittest.main()
