from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from threading import Thread
import unittest
from unittest.mock import patch

from frameatlas.ideas import generate, local_models
from frameatlas.ollama_local import request_json


class OllamaLocalTests(unittest.TestCase):
    def test_remote_alias_is_rejected_before_sending_private_story_prompt(self):
        models = {"models": [
            {"name": "phi4-mini:latest", "remote_model": "remote-model"},
            {"name": "qwen2.5vl:7b", "remote_host": "https://example.invalid"},
            {"name": "qwen3:8b"},
            None,
        ]}
        with patch("frameatlas.ideas.request_json", return_value=models) as request:
            self.assertEqual(local_models(), ["qwen3:8b"])
            request.reset_mock()
            with self.assertRaisesRegex(ValueError, "installed local writing model"):
                generate(None, "private footage notes", model="phi4-mini:latest")
            request.assert_called_once_with("/api/tags", timeout=3)

    def test_inference_ignores_proxies_and_never_follows_redirects(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                calls.append((self.path, b""))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"models":[]}')

            def do_POST(self):
                calls.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(307)
                self.send_header("Location", "/unexpected-target")
                self.end_headers()

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def connect(host, port, *, timeout):
                    self.assertEqual((host, port), ("127.0.0.1", 11434))
                    return HTTPConnection(host, server.server_port, timeout=timeout)

                with patch("frameatlas.ollama_local.HTTPConnection", side_effect=connect), \
                        patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": "",
                                               "http_proxy": "http://127.0.0.1:1", "no_proxy": ""}):
                    self.assertEqual(request_json("/api/tags", timeout=2), {"models": []})
                    with self.assertRaisesRegex(ValueError, "HTTP 307"):
                        request_json("/api/chat", {"sample": "private"}, timeout=2)
                    with self.assertRaisesRegex(ValueError, "Unsupported"):
                        request_json("http://example.invalid/api/chat", timeout=2)
            finally:
                server.shutdown()
                thread.join(3)
        self.assertEqual(calls, [("/api/tags", b""), ("/api/chat", b'{"sample": "private"}')])
