"""Other AI apps connecting over MCP's HTTP transport: off by default, a bearer token each with a scope,
and the same loopback and origin checks as the browser page."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

from frameatlas import connections
from frameatlas.store import Store


class ConnectionTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_tokens_are_kept_as_hashes_and_can_be_revoked(self):
        self.assertFalse(connections.enabled(self.store))
        made = connections.create(self.store, "Desk agent", "read")
        self.assertTrue(made["token"].startswith("fa_"))
        self.assertEqual(made["config"]["mcpServers"]["frameatlas"]["headers"]["Authorization"], "Bearer " + made["token"])
        with self.store.connect() as db:
            stored = db.execute("SELECT hash FROM mcp_tokens").fetchone()[0]
        self.assertNotIn(made["token"], stored)
        self.assertEqual(connections.authenticate(self.store, "Bearer " + made["token"]), ("Desk agent", "read"))
        for bad in ("Bearer fa_wrong", "Basic x", "", None, "Bearer " + made["token"][:-1]):
            with self.subTest(bad=bad):
                self.assertIsNone(connections.authenticate(self.store, bad))
        self.assertIsNotNone(connections.status(self.store)["tokens"][0]["last_used"])
        connections.revoke(self.store, made["id"])
        self.assertIsNone(connections.authenticate(self.store, "Bearer " + made["token"]))
        for label, scope in (("", "read"), ("x", "admin")):
            with self.subTest(label=label, scope=scope), self.assertRaises(ValueError):
                connections.create(self.store, label, scope)


class HttpMcpTests(unittest.TestCase):
    def test_the_endpoint_end_to_end(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            write = connections.create(store, "Writer", "write")["token"]
            read = connections.create(store, "Reader", "read")["token"]
            p = subprocess.Popen([sys.executable, "-m", "frameatlas.server", "--data", folder, "--port", "0"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                url = p.stdout.readline().strip().split()[-1].rstrip("/")

                def post(message, token=write, headers=None, raw=None):
                    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})}
                    if token:
                        h["Authorization"] = "Bearer " + token
                    body = raw if raw is not None else json.dumps(message).encode()
                    with urllib.request.urlopen(urllib.request.Request(url + "/mcp", body, h), timeout=15) as r:
                        text = r.read()
                        return r.status, (json.loads(text) if text else None)

                def status_of(**kw):
                    try:
                        return post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, **kw)[0]
                    except urllib.error.HTTPError as e:
                        return e.code

                self.assertEqual(status_of(), 404, "off until the owner turns it on")
                connections.set_enabled(store, True)
                self.assertEqual(status_of(), 200)
                self.assertEqual(status_of(token=None), 401)
                self.assertEqual(status_of(token="fa_made-up"), 401)
                self.assertEqual(status_of(headers={"Origin": "https://attacker.test"}), 403)
                self.assertEqual(status_of(headers={"Host": "attacker.test"}), 403)
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(url + "/mcp", timeout=10)
                self.assertEqual(e.exception.code, 405)
                _, init = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "x"}}})
                self.assertEqual(init["result"]["serverInfo"]["name"], "frameatlas")
                self.assertEqual(post({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
                _, tools = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                self.assertGreater(len(tools["result"]["tools"]), 80)
                _, added = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "idea_pad_add", "arguments": {"title": "From another app"}}})
                self.assertFalse(added["result"]["isError"], added)
                _, denied = post({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "idea_pad_add", "arguments": {"title": "Not allowed"}}}, token=read)
                self.assertEqual(json.loads(denied["result"]["content"][0]["text"])["error"]["code"], "forbidden")
                _, found = post({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                 "params": {"name": "idea_pad_list", "arguments": {}}}, token=read)
                self.assertEqual(found["result"]["structuredContent"]["total"], 1)
                _, log = post({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "agent_activity", "arguments": {}}})
                self.assertEqual(log["result"]["structuredContent"]["items"][0]["client"], "http:Writer")
                with self.assertRaises(urllib.error.HTTPError) as e:
                    post(None, raw=b"[1, 2]")
                self.assertEqual((e.exception.code, json.loads(e.exception.read())["error"]["code"]), (400, -32600))
            except urllib.error.HTTPError as e:
                self.fail(f"HTTP {e.code}: {e.read()[:300]}")
            finally:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                p.stdout.close()
                p.stderr.close()


if __name__ == "__main__":
    unittest.main()
