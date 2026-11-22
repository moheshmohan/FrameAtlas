"""The shared screen (ui_show, ui_select, ui_state and the page's poll) and MCP resources and prompts."""
import base64
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from frameatlas import ui_channel
from frameatlas.mcp import handle
from frameatlas.service import Service
from frameatlas.store import Store


def rpc(service, method, params=None):
    return handle(service, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def tool(service, name, arguments=None):
    return rpc(service, "tools/call", {"name": name, "arguments": arguments or {}})["result"]


class UiChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)
        self.aid, _ = self.store.upsert({"provider": "local", "source_id": "u1", "filename": "kite.jpg", "kind": "photo"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_routes_are_links_the_page_understands(self):
        self.assertEqual(ui_channel.route("item", {"id": "abc", "time": 12.5}), "item/abc@12.5")
        self.assertEqual(ui_channel.route("item", {"id": "abc", "time": 3}), "item/abc@3")
        self.assertEqual(ui_channel.route("person", {"id": "4"}), "person/4")
        self.assertEqual(ui_channel.route("place", {"id": "3, 9"}), "place/3,9")
        self.assertEqual(ui_channel.route("library", {"query": "kite beach", "since": "2024-05-25"}),
                         "library?query=kite+beach&since=2024-05-25")
        self.assertEqual(ui_channel.route("trip", {"since": "2024-05-25", "until": "2024-05-27"}),
                         "library?since=2024-05-25&until=2024-05-27")
        self.assertEqual(ui_channel.route("ideas", {}), "ideapad")
        for view, args in (("item", {}), ("trip", {"since": "2024-05-25"}), ("library", {"since": "May"}), ("nowhere", {})):
            with self.subTest(view=view), self.assertRaises(ValueError):
                ui_channel.route(view, args)

    def test_show_without_an_open_page_gives_a_link(self):
        ui_channel.remember_port(self.store, 7444)
        data = tool(self.service, "ui_show", {"view": "item", "id": self.aid, "time": 4})["structuredContent"]
        self.assertFalse(data["shown"])
        self.assertEqual(data["link"], f"http://127.0.0.1:7444/#item/{self.aid}@4")
        self.assertIn("not open", data["note"])
        missing = json.loads(tool(self.service, "ui_show", {"view": "item", "id": "missing"})["content"][0]["text"])
        self.assertEqual(missing["error"]["code"], "not_found")
        self.assertEqual(json.loads(tool(self.service, "ui_show", {"view": "person", "id": "x"})["content"][0]["text"])["error"]["code"],
                         "invalid_arguments")

    def test_the_page_receives_events_and_reports_its_state(self):
        start = self.service.dispatch("ui_poll", {"after": -1})
        self.assertEqual(start["events"], [])
        self.assertTrue(tool(self.service, "ui_state")["structuredContent"]["page_open"])
        shown = tool(self.service, "ui_show", {"view": "library", "query": "kite", "message": "Kites"})["structuredContent"]
        self.assertTrue(shown["shown"])
        got = self.service.dispatch("ui_poll", {"after": start["last"], "wait": 0})
        self.assertEqual([(e["kind"], e["hash"], e["message"]) for e in got["events"]], [("show", "library?query=kite", "Kites")])
        selected = tool(self.service, "ui_select", {"ids": [self.aid]})["structuredContent"]
        self.assertEqual(selected["selected"], 1)
        event = self.service.dispatch("ui_poll", {"after": got["last"], "wait": 0})["events"][0]
        self.assertEqual(event["items"], [[self.aid, "kite.jpg"]])
        self.service.dispatch("ui_report", {"state": {"page": "library", "selected": [{"id": self.aid}]}})
        state = tool(self.service, "ui_state")["structuredContent"]
        self.assertEqual(state["state"]["selected"], [{"id": self.aid}])
        with self.assertRaises(ValueError):
            self.service.dispatch("ui_report", {"state": {"x": "y" * 70000}})
        self.assertEqual(json.loads(tool(self.service, "ui_select", {"ids": ["nope"]})["content"][0]["text"])["error"]["code"], "not_found")

    def test_poll_waits_for_an_event(self):
        last = self.service.dispatch("ui_poll", {"after": -1})["last"]
        threading.Timer(0.6, lambda: ui_channel.show(self.store, {"view": "queue"})).start()
        began = time.time()
        got = self.service.dispatch("ui_poll", {"after": last, "wait": 5})
        self.assertEqual(got["events"][0]["hash"], "queue")
        self.assertLess(time.time() - began, 4)

    def test_old_events_are_not_replayed_and_a_closed_page_has_no_state(self):
        last = self.service.dispatch("ui_poll", {"after": -1})["last"]
        ui_channel.show(self.store, {"view": "queue"})
        with self.store.connect() as db:
            db.execute("UPDATE ui_events SET at=at-600")
            db.execute("UPDATE settings SET value=? WHERE key='ui_seen'", (json.dumps(time.time() - 600),))
        self.assertEqual(self.service.dispatch("ui_poll", {"after": last, "wait": 0})["events"], [])
        with self.store.connect() as db:
            db.execute("UPDATE settings SET value=? WHERE key='ui_seen'", (json.dumps(time.time() - 600),))
        state = tool(self.service, "ui_state")["structuredContent"]
        self.assertEqual((state["page_open"], state["state"]), (False, None))


class ResourceAndPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)
        self.aid, _ = self.store.upsert({"provider": "local", "source_id": "r1", "filename": "canal.jpg", "kind": "photo"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_initialize_offers_resources_and_prompts(self):
        caps = rpc(self.service, "initialize")["result"]["capabilities"]
        self.assertIn("resources", caps)
        self.assertIn("prompts", caps)

    def test_every_listed_resource_reads(self):
        listed = rpc(self.service, "resources/list")["result"]["resources"]
        self.assertGreaterEqual(len(listed), 6)
        for resource in listed:
            with self.subTest(uri=resource["uri"]):
                contents = rpc(self.service, "resources/read", {"uri": resource["uri"]})["result"]["contents"]
                self.assertIsInstance(json.loads(contents[0]["text"]), dict)
        templates = rpc(self.service, "resources/templates/list")["result"]["resourceTemplates"]
        self.assertIn("frameatlas://asset/{id}", [t["uriTemplate"] for t in templates])

    def test_templated_resources(self):
        asset = rpc(self.service, "resources/read", {"uri": f"frameatlas://asset/{self.aid}"})["result"]["contents"][0]
        self.assertEqual(json.loads(asset["text"])["filename"], "canal.jpg")
        missing = rpc(self.service, "resources/read", {"uri": f"frameatlas://preview/{self.aid}"})
        self.assertEqual(missing["error"]["code"], -32002)
        thumbs = self.store.root / "thumbnails"
        thumbs.mkdir(exist_ok=True)
        (thumbs / f"{self.aid}.jpg").write_bytes(b"\xff\xd8 made-up jpeg")
        with self.store.connect() as db:
            db.execute("UPDATE assets SET thumbnail=? WHERE id=?", (f"{self.aid}.jpg", self.aid))
        preview = rpc(self.service, "resources/read", {"uri": f"frameatlas://preview/{self.aid}"})["result"]["contents"][0]
        self.assertEqual((preview["mimeType"], base64.b64decode(preview["blob"])), ("image/jpeg", b"\xff\xd8 made-up jpeg"))
        idea = self.service.dispatch("idea_pad_add", {"title": "Canal boats"})
        read = rpc(self.service, "resources/read", {"uri": f"frameatlas://idea/{idea['id']}"})["result"]["contents"][0]
        self.assertEqual(json.loads(read["text"])["title"], "Canal boats")
        for uri in ("frameatlas://place/1", "frameatlas://idea/99", "frameatlas://person/1", "frameatlas://../etc", "http://x"):
            with self.subTest(uri=uri):
                self.assertIn("error", rpc(self.service, "resources/read", {"uri": uri}))

    def test_prompts(self):
        listed = {p["name"]: p for p in rpc(self.service, "prompts/list")["result"]["prompts"]}
        self.assertEqual(set(listed), {"find_footage", "sort_people", "plan_trip_video", "check_coverage"})
        got = rpc(self.service, "prompts/get", {"name": "find_footage", "arguments": {"description": "kites on the beach", "since": "2024-05-01"}})
        text = got["result"]["messages"][0]["content"]["text"]
        self.assertIn("kites on the beach", text)
        self.assertIn("From 2024-05-01.", text)
        self.assertIn("ui_show", text)
        trip = rpc(self.service, "prompts/get", {"name": "plan_trip_video", "arguments": {"since": "2024-05-25", "until": "2024-05-27"}})
        self.assertIn("short video", trip["result"]["messages"][0]["content"]["text"])
        self.assertEqual(rpc(self.service, "prompts/get", {"name": "find_footage", "arguments": {}})["error"]["code"], -32602)
        self.assertEqual(rpc(self.service, "prompts/get", {"name": "nope"})["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
