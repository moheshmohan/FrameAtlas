import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import urllib.error


class HttpBoundaryTests(unittest.TestCase):
    def test_loopback_origin_csrf_and_error_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            p = subprocess.Popen([sys.executable, '-m', 'frameatlas.server', '--data', folder, '--port', '0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                line = p.stdout.readline()
                url = line.strip().split()[-1]
                self.assertTrue(url.startswith('http://127.0.0.1:'))
                def req(path, data=None, headers=None):
                    payload = json.dumps(data).encode() if data is not None else None
                    h = {'Content-Type': 'application/json', **(headers or {})}
                    r = urllib.request.urlopen(urllib.request.Request(url + path, payload, h), timeout=10)
                    with r:
                        return json.loads(r.read())
                token = req('/api/session')['csrf']
                with self.assertRaises(urllib.error.HTTPError) as e:
                    req('/api/stats', headers={'Host': 'attacker.test'})
                self.assertEqual(e.exception.code, 403)
                manifest = {'items': [{'provider': 'local', 'source_id': 'test', 'filename': 'a.jpg'}]}
                with self.assertRaises(urllib.error.HTTPError) as e:
                    req('/api/import_manifest', manifest)
                self.assertEqual(e.exception.code, 403)
                with self.assertRaises(urllib.error.HTTPError) as e:
                    req('/api/import_manifest', manifest, {'X-FrameAtlas-Token': token, 'Origin': 'https://attacker.test'})
                self.assertEqual(e.exception.code, 403)
                result = req('/api/import_manifest', manifest, {'X-FrameAtlas-Token': token})
                self.assertEqual(result['added'], 1)
                with self.assertRaises(urllib.error.HTTPError):
                    req('/api/enqueue', {'ids': ['missing']}, {'X-FrameAtlas-Token': token})
                self.assertEqual(req('/api/stats')['total'], 1)
                with self.assertRaises(urllib.error.HTTPError):
                    req('/thumb/../../private')
            finally:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill(); p.wait(timeout=5)
                p.stdout.close(); p.stderr.close()

    def test_ui_can_attach_without_stopping_existing_worker(self):
        from frameatlas.worker import Worker
        from frameatlas.store import Store
        with tempfile.TemporaryDirectory() as folder:
            worker = Worker(Store(folder))
            p = subprocess.Popen([sys.executable, '-m', 'frameatlas.server', '--data', folder, '--port', '0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                url = p.stdout.readline().strip().split()[-1]
                with urllib.request.urlopen(url + '/api/stats', timeout=10) as response:
                    self.assertEqual(json.load(response)['total'], 0)
                p.terminate(); p.wait(timeout=10)
                self.assertFalse(worker.lock.closed)
            finally:
                if p.poll() is None:
                    p.kill(); p.wait(timeout=5)
                p.stdout.close(); p.stderr.close()
                worker.close()

    def test_real_stdio_mcp_protocol_and_invalid_input_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            messages = '\n'.join(['not json', json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}), json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}), json.dumps({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'catalog_stats', 'arguments': {}}})]) + '\n'
            p = subprocess.run([sys.executable, '-m', 'frameatlas.mcp'], input=messages, text=True, capture_output=True, timeout=15, env=dict(os.environ, FRAMEATLAS_DATA=folder))
            self.assertEqual(p.returncode, 0, p.stderr)
            rows = [json.loads(x) for x in p.stdout.splitlines()]
            self.assertEqual(rows[0]['error']['code'], -32700)
            self.assertEqual(rows[1]['result']['serverInfo']['name'], 'frameatlas')
            self.assertGreaterEqual(len(rows[2]['result']['tools']), 18)
            self.assertEqual(json.loads(rows[3]['result']['content'][0]['text'])['total'], 0)


if __name__ == '__main__':
    unittest.main()
