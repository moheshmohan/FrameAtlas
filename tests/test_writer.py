"""Who writes (the local model, the calling agent, or the client's model through MCP sampling), asking the
owner through the client (MCP elicitation), the stdin reader, and the supervisor queue."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from frameatlas import mcp, people, writer
from frameatlas.mcp import SESSION, handle
from frameatlas.service import Service
from frameatlas.store import Store


def story(asset_id):
    return {"ideas": [{"title": "Lantern walk", "hook": "Lanterns along the river at dusk.", "pickups": [],
                       "sources": [{"asset_id": asset_id, "start": 2.0, "end": 7.0, "reason": "The lanterns"}]}]}


class FakeClient:
    """The client's side of requests FrameAtlas sends: sampling and elicitation answers."""

    def __init__(self, reply_text="", allow=True):
        self.sent, self.reply_text, self.allow = [], reply_text, allow

    def send(self, message):
        self.sent.append(message)

    def receive(self, timeout):
        request = self.sent[-1]
        if request.get("method") == "sampling/createMessage":
            return {"jsonrpc": "2.0", "id": request["id"], "result": {"role": "assistant", "model": "made-up",
                                                                      "content": {"type": "text", "text": self.reply_text}}}
        if request.get("method") == "elicitation/create":
            action = "accept" if self.allow is not None else "cancel"
            return {"jsonrpc": "2.0", "id": request["id"], "result": {"action": action, "content": {"allow": bool(self.allow)}}}
        return None


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)
        self.aid, _ = self.store.upsert({"provider": "local", "source_id": "w1", "filename": "lanterns.mov", "kind": "video"})
        self.store.analysis(self.aid, [{"start": 2.0, "end": 7.0, "description": {"caption": "Paper lanterns float down a river at dusk"}}],
                            {}, {"speech_status": "no_audio"})

    def tearDown(self):
        SESSION.update({"client": "mcp", "sampling": False, "elicitation": False})
        self.tmp.cleanup()

    def init(self, client, capabilities=None):
        handle(self.service, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"clientInfo": {"name": "made-up-client"}, "capabilities": capabilities or {}}})

    def call(self, name, arguments, client=None):
        result = handle(self.service, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
                        client.send if client else None, client.receive if client else None)["result"]
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def test_the_agent_writes_and_the_citations_are_checked(self):
        self.init(None)
        args = {"prompt": "A calm evening by the river", "asset_ids": [self.aid], "writer": "agent"}
        asked = self.call("catalog_ideas", args)["needs_writing"]
        self.assertEqual(set(asked) >= {"prompt_id", "system", "user", "schema"}, True)
        self.assertIn(self.aid, asked["user"])
        self.assertNotIn("previews", asked)
        bad = self.call("catalog_ideas", {**args, "drafts": {asked["prompt_id"]: "not json"}})
        self.assertEqual(bad["error"]["code"], "invalid_arguments")
        done = self.call("catalog_ideas", {**args, "drafts": {asked["prompt_id"]: "```json\n" + json.dumps(story(self.aid)) + "\n```"}})
        self.assertEqual(done["ideas"][0]["title"], "Lantern walk")
        self.assertEqual(done["model"], "agent:made-up-client")
        self.assertFalse(done["offline"])
        saved = self.service.dispatch("idea_pad", {})["items"][0]
        self.assertEqual(saved["model"], "agent:made-up-client")
        with_images = self.call("catalog_ideas", {**args, "allow_images": True})["needs_writing"]
        self.assertEqual(with_images["previews"], [f"frameatlas://preview/{self.aid}"])

    def test_a_draft_citing_footage_outside_the_evidence_is_not_saved(self):
        self.init(None)
        args = {"prompt": "A calm evening", "asset_ids": [self.aid], "writer": "agent"}
        pid = self.call("catalog_ideas", args)["needs_writing"]["prompt_id"]
        made_up = story("0" * 24)
        result = self.call("catalog_ideas", {**args, "drafts": {pid: json.dumps(made_up)}})
        self.assertIn("error", result)
        self.assertEqual(self.service.dispatch("idea_pad", {})["total"], 0)

    def test_the_clients_model_writes_through_sampling(self):
        client = FakeClient("Here it is:\n" + json.dumps(story(self.aid)))
        self.init(client, {"sampling": {}})
        thumbs = self.store.root / "thumbnails"
        thumbs.mkdir(exist_ok=True)
        (thumbs / f"{self.aid}.jpg").write_bytes(b"\xff\xd8 made-up")
        with self.store.connect() as db:
            db.execute("UPDATE assets SET thumbnail=? WHERE id=?", (f"{self.aid}.jpg", self.aid))
        done = self.call("catalog_ideas", {"prompt": "A calm evening", "asset_ids": [self.aid], "writer": "client"}, client)
        self.assertEqual(done["model"], "client:made-up-client")
        request = next(m for m in client.sent if m.get("method") == "sampling/createMessage")
        self.assertIn("JSON schema", request["params"]["systemPrompt"])
        self.assertEqual([m["content"]["type"] for m in request["params"]["messages"]], ["text"], "no images unless allowed")
        client.sent.clear()
        self.call("catalog_ideas", {"prompt": "Another evening", "asset_ids": [self.aid], "writer": "client", "allow_images": True}, client)
        request = next(m for m in client.sent if m.get("method") == "sampling/createMessage")
        self.assertEqual([m["content"]["type"] for m in request["params"]["messages"]], ["text", "image"])

    def test_client_writing_needs_a_client_that_samples(self):
        self.init(None)
        result = self.call("catalog_ideas", {"prompt": "x", "asset_ids": [self.aid], "writer": "client"})
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertIn("writer agent", result["error"]["message"])
        result = self.call("catalog_ideas", {"prompt": "x", "asset_ids": [self.aid], "writer": "agent", "background": True})
        self.assertEqual(result["error"]["code"], "invalid_arguments")

    def test_json_text(self):
        self.assertEqual(writer.json_text('```json\n{"a": 1}\n```'), '{"a": 1}')
        self.assertEqual(writer.json_text('Sure! {"a": {"b": 2}} Hope it helps'), '{"a": {"b": 2}}')
        for bad in ("no json", "{broken", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                writer.json_text(bad)


class ElicitationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)
        self.a, _ = self.store.upsert({"provider": "local", "source_id": "e1", "filename": "gate.jpg", "kind": "photo"})
        self.b, _ = self.store.upsert({"provider": "local", "source_id": "e2", "filename": "gate copy.jpg", "kind": "photo"})

    def tearDown(self):
        SESSION.update({"client": "mcp", "sampling": False, "elicitation": False})
        self.tmp.cleanup()

    def run_merge(self, client):
        handle(self.service, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {"elicitation": {}}}})
        return handle(self.service, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "catalog_merge", "arguments": {"keep": self.a, "duplicate": self.b}}}, client.send, client.receive)["result"]

    def test_the_owner_says_yes_in_the_client(self):
        client = FakeClient(allow=True)
        result = self.run_merge(client)
        self.assertFalse(result["isError"], result)
        ask = next(m for m in client.sent if m.get("method") == "elicitation/create")
        self.assertIn("catalog_merge", ask["params"]["message"])

    def test_the_owner_says_no_in_the_client(self):
        for allow in (False, None):
            with self.subTest(allow=allow):
                result = self.run_merge(FakeClient(allow=allow))
                self.assertEqual(json.loads(result["content"][0]["text"])["error"]["code"], "forbidden")


class LineReaderTests(unittest.TestCase):
    def test_messages_partial_lines_bad_lines_and_timeouts(self):
        r, w = os.pipe()
        try:
            reader = mcp.LineReader(r)
            os.write(w, b'{"id": 1}\n{"id": 2}\nnot json\n{"id"')
            self.assertEqual(reader.message(1), {"id": 1})
            self.assertEqual(reader.message(1), {"id": 2}, "a second line already read is not lost")
            self.assertIs(reader.message(1), mcp.INVALID)
            began = time.time()
            self.assertIsNone(reader.message(0.3), "a partial line waits")
            self.assertLess(time.time() - began, 2)
            os.write(w, b': 3}\n')
            self.assertEqual(reader.message(1), {"id": 3})
            os.close(w)
            w = None
            self.assertIsNone(reader.message())
        finally:
            os.close(r)
            if w is not None:
                os.close(w)

    def test_messages_that_arrive_while_waiting_are_kept(self):
        client = FakeClient()
        client.receive = lambda timeout: {"jsonrpc": "2.0", "id": 99, "method": "ping"} if not mcp.DEFERRED else \
            {"jsonrpc": "2.0", "id": client.sent[-1]["id"], "result": {"ok": True}}
        self.assertEqual(mcp.client_request("ping", {}, client.send, client.receive), {"ok": True})
        self.assertEqual(mcp.DEFERRED.pop(0)["id"], 99)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)
        now = time.time()
        with self.store.connect() as db:
            people.ensure(db)
            db.execute("INSERT INTO people(id,name,created,updated) VALUES(1,'Ana',?,?),(2,'',?,?)", (now, now, now, now))
            db.execute("""INSERT INTO face_log(id,at,actor,action,status,person_id,other_id,likeness,detail)
                VALUES(7,?,'auto','merge','proposed',1,2,0.71,'{"why": "alike"}'),
                      (8,?,'google','name','proposed',2,NULL,1.0,'{"name": "Bo", "items": 3, "of": 4}')""", (now, now))

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name, arguments=None):
        result = handle(self.service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}})["result"]
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def test_questions_answers_and_undo(self):
        q = self.call("supervisor_queue")
        self.assertEqual(q["total"], 2)
        merge = next(i for i in q["items"] if i["kind"] == "merge")
        self.assertEqual(merge["id"], "people:7")
        self.assertIn("'Ana' and person #2 the same person", merge["question"])
        name = next(i for i in q["items"] if i["kind"] == "name")
        self.assertIn("named 'Bo'", name["question"])
        self.assertEqual(self.call("supervisor_decide", {"id": "people:7", "decision": "dismiss"})["decision"], "dismiss")
        self.call("supervisor_decide", {"id": "people:8", "decision": "apply"})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT name FROM people WHERE id=2").fetchone()[0], "Bo")
        entry = next(i for i in self.service.dispatch("activity", {})["items"] if i["tool"] == "supervisor_decide" and i["can_undo"])
        self.call("agent_undo", {"id": entry["id"]})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT name FROM people WHERE id=2").fetchone()[0], "")
        self.assertEqual(self.call("supervisor_queue")["total"], 0, "neither the dismissed merge nor the undone name is asked again")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM face_log WHERE id=8").fetchone()[0], "undone")
        self.assertEqual(self.call("supervisor_decide", {"id": "7", "decision": "apply"})["error"]["code"], "invalid")


if __name__ == "__main__":
    unittest.main()
