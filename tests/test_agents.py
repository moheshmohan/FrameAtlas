"""Agents under the owner's control: permission tiers with confirmation tokens, the activity log with
undo, and background tasks an agent can wait on."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from frameatlas import agents
from frameatlas.mcp import SESSION, handle
from frameatlas.service import Service
from frameatlas.store import Store


def call(service, name, arguments=None, send=None, meta=None):
    params = {"name": name, "arguments": arguments or {}}
    if meta:
        params["_meta"] = meta
    return handle(service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}, send)["result"]


def error_of(result):
    return json.loads(result["content"][0]["text"])["error"]


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)
        self.a, _ = self.store.upsert({"provider": "local", "source_id": "p1", "filename": "pier.jpg", "kind": "photo"})
        self.b, _ = self.store.upsert({"provider": "local", "source_id": "p2", "filename": "pier copy.jpg", "kind": "photo"})
        SESSION["client"] = "mcp"

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_and_only_valid_settings(self):
        self.assertEqual(agents.permissions(self.store), {"write": "allow", "destructive": "confirm", "outside": "confirm"})
        with self.assertRaises(ValueError):
            agents.set_permissions(self.store, {"read": "deny"})
        with self.assertRaises(ValueError):
            agents.set_permissions(self.store, {"write": "sometimes"})
        self.assertEqual(agents.set_permissions(self.store, {"write": "confirm"})["permissions"]["write"], "confirm")
        self.assertEqual(call(self.service, "agent_permissions")["structuredContent"]["permissions"]["write"], "confirm")

    def test_a_confirmation_is_bound_to_one_call_and_used_once(self):
        args = {"keep": self.a, "duplicate": self.b}
        first = error_of(call(self.service, "catalog_merge", args))
        self.assertEqual(first["code"], "needs_confirmation")
        token = first["data"]["confirmation"]
        self.assertIn("catalog_merge", first["data"]["summary"])
        other = error_of(call(self.service, "catalog_merge", {"keep": self.b, "duplicate": self.a, "confirmation": token}))
        self.assertEqual((other["code"], other["message"]), ("invalid_arguments", "This confirmation is for a different call"))
        done = call(self.service, "catalog_merge", {**args, "confirmation": token})
        self.assertFalse(done["isError"], done)
        again = error_of(call(self.service, "catalog_merge", {**args, "confirmation": token}))
        self.assertIn("already used", again["message"])

    def test_expired_and_declined_confirmations(self):
        token = error_of(call(self.service, "idea_pad_delete", {"id": 1}))["data"]["confirmation"]
        with self.store.connect() as db:
            agents.ensure(db)
            db.execute("UPDATE agent_confirmations SET expires=0 WHERE token=?", (token,))
        self.assertIn("expired", error_of(call(self.service, "idea_pad_delete", {"id": 1, "confirmation": token}))["message"])
        token = error_of(call(self.service, "idea_pad_delete", {"id": 1}))["data"]["confirmation"]
        self.assertEqual(len(agents.confirmations(self.store)), 1)
        agents.decide_confirmation(self.store, token, "declined")
        self.assertEqual(error_of(call(self.service, "idea_pad_delete", {"id": 1, "confirmation": token}))["code"], "forbidden")
        with self.assertRaises(ValueError):
            agents.decide_confirmation(self.store, "fa-missing", "approved")

    def test_owner_approval_and_deny(self):
        agents.set_permissions(self.store, {"destructive": "owner"})
        args = {"keep": self.a, "duplicate": self.b}
        first = error_of(call(self.service, "catalog_merge", args))
        self.assertEqual(first["code"], "needs_user")
        token = first["data"]["confirmation"]
        waiting = error_of(call(self.service, "catalog_merge", {**args, "confirmation": token}))
        self.assertEqual(waiting["code"], "needs_user")
        self.assertEqual(self.service.dispatch("activity", {})["pending"][0]["token"], token)
        self.service.dispatch("confirmation_decide", {"token": token, "decision": "approved"})
        self.assertFalse(call(self.service, "catalog_merge", {**args, "confirmation": token})["isError"])
        agents.set_permissions(self.store, {"write": "deny"})
        self.assertEqual(error_of(call(self.service, "idea_pad_add", {"title": "Pier at dawn"}))["code"], "forbidden")
        self.assertFalse(call(self.service, "catalog_search", {"query": "pier"})["isError"], "reads are never gated")

    def test_batches_respect_the_permissions_of_each_call(self):
        agents.set_permissions(self.store, {"write": "confirm"})
        data = call(self.service, "catalog_batch", {"calls": [{"tool": "catalog_stats"},
                                                              {"tool": "idea_pad_add", "arguments": {"title": "x"}}]})["structuredContent"]
        self.assertEqual([r["ok"] for r in data["results"]], [True, False])
        self.assertEqual(data["results"][1]["error"]["code"], "needs_confirmation")


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)
        self.aid, _ = self.store.upsert({"provider": "local", "source_id": "a1", "filename": "lighthouse.jpg", "kind": "photo"})
        handle(self.service, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"clientInfo": {"name": "test-agent"}}})

    def tearDown(self):
        SESSION["client"] = "mcp"
        self.tmp.cleanup()

    def log(self, **kw):
        return self.service.dispatch("activity", kw)["items"]

    def test_calls_are_logged_with_client_and_outcome(self):
        call(self.service, "catalog_search", {"query": "lighthouse"})
        call(self.service, "idea_pad_add", {"title": "The lighthouse in every season"})
        call(self.service, "idea_pad_update", {"id": 99, "title": "x"})
        items = self.log()
        self.assertEqual([i["tool"] for i in items], ["idea_pad_update", "idea_pad_add"])
        self.assertEqual(items[0]["client"], "mcp:test-agent")
        self.assertEqual((items[0]["ok"], items[0]["code"]), (0, "not_found"))
        self.assertEqual(items[1]["ok"], 1)
        self.assertEqual(len(self.log(include_reads=True)), 3)
        self.assertEqual(call(self.service, "agent_activity")["structuredContent"]["total"], 2)

    def test_long_arguments_are_counted_not_copied(self):
        stored = json.loads(agents.short_args({"places": list(range(500)), "note": "y" * 1000, "confirmation": "fa-x"}))
        self.assertEqual(stored["places"], "[500 items]")
        self.assertEqual(len(stored["note"]), 301)
        self.assertNotIn("confirmation", stored)

    def undo_last(self, tool):
        entry = next(i for i in self.log() if i["tool"] == tool)
        self.assertTrue(entry["can_undo"], entry)
        return call(self.service, "agent_undo", {"id": entry["id"]})

    def test_idea_changes_can_be_undone(self):
        idea = call(self.service, "idea_pad_add", {"title": "Lighthouse at night"})["structuredContent"]
        call(self.service, "idea_pad_update", {"id": idea["id"], "title": "Lighthouse at dusk", "status": "planned"})
        self.assertFalse(self.undo_last("idea_pad_update")["isError"])
        restored = self.service.dispatch("idea_pad", {})["items"][0]
        self.assertEqual((restored["title"], restored["status"]), ("Lighthouse at night", "idea"))
        token = error_of(call(self.service, "idea_pad_delete", {"id": idea["id"]}))["data"]["confirmation"]
        call(self.service, "idea_pad_delete", {"id": idea["id"], "confirmation": token})
        self.assertEqual(self.service.dispatch("idea_pad", {})["total"], 0)
        self.undo_last("idea_pad_delete")
        self.assertEqual(self.service.dispatch("idea_pad", {})["items"][0]["id"], idea["id"])
        self.undo_last("idea_pad_add")
        self.assertEqual(self.service.dispatch("idea_pad", {})["total"], 0)
        twice = next(i for i in self.log() if i["tool"] == "idea_pad_add")
        self.assertFalse(twice["can_undo"])
        self.assertEqual(error_of(call(self.service, "agent_undo", {"id": twice["id"]}))["code"], "invalid")
        self.assertIn("agent_undo", [i["tool"] for i in self.log()])

    def test_annotations_queue_and_storage_can_be_undone(self):
        before = self.store.get(self.aid)
        call(self.service, "catalog_annotate", {"id": self.aid, "summary": "A lighthouse on a cliff", "tags": ["coast"]})
        self.assertEqual(self.store.search(query="cliff")["total"], 1)
        self.undo_last("catalog_annotate")
        after = self.store.get(self.aid)
        self.assertEqual((after["summary"], after["tags"]), (before["summary"], before["tags"]))
        self.assertEqual(self.store.search(query="cliff")["total"], 0)
        call(self.service, "catalog_queue_control", {"paused": True})
        self.undo_last("catalog_queue_control")
        self.assertFalse(self.store.queue_state()["paused"])
        call(self.service, "catalog_storage_limit", {"gib": 8})
        self.undo_last("catalog_storage_limit")
        self.assertEqual(self.store.storage_limit(), 2 * 1024 ** 3)

    def test_changes_without_an_undo_say_so(self):
        call(self.service, "catalog_correct_scene", {"id": self.aid, "ordinal": 0, "caption": "x"})
        entry = self.log()[0]
        self.assertFalse(entry["can_undo"])


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_long_tool_runs_in_the_background_and_can_be_waited_for(self):
        started = call(self.service, "catalog_import_manifest", {"label": "made-up", "background": True, "items": [
            {"provider": "local", "source_id": "m1", "filename": "made-up.jpg", "kind": "photo"}]})
        self.assertFalse(started["isError"], started)
        task = started["structuredContent"]["task"]
        done = call(self.service, "catalog_wait", {"task": task, "timeout": 10})["structuredContent"]
        self.assertEqual(done["state"], "done")
        self.assertTrue(done["finished_waiting"])
        self.assertEqual(done["result"], {"seen": 1, "added": 1})
        listed = call(self.service, "catalog_tasks")["structuredContent"]["items"]
        self.assertEqual(listed[0]["id"], task)
        self.assertNotIn("result", listed[0])
        self.assertEqual(self.service.dispatch("activity", {})["items"][0]["task"], task)

    def test_a_failed_background_task_keeps_its_error(self):
        started = call(self.service, "catalog_import_takeout", {"path": "/nonexistent/made-up.zip", "background": True})
        task = started["structuredContent"]["task"]
        done = call(self.service, "catalog_wait", {"task": task, "timeout": 10})["structuredContent"]
        self.assertEqual(done["state"], "failed")
        self.assertIn(done["error"]["code"], {"invalid", "not_found", "failed"})

    def test_background_is_only_for_long_tools(self):
        error = error_of(call(self.service, "catalog_stats", {"background": True}))
        self.assertEqual(error["code"], "invalid_arguments")

    def test_tasks_from_a_process_that_ended_are_interrupted(self):
        with self.store.connect() as db:
            agents.ensure(db)
            gone = db.execute("INSERT INTO agent_tasks(tool,kind,pid,state,started) VALUES('places_import','thread',999999,'running',?)",
                              (time.time(),)).lastrowid
            here = db.execute("INSERT INTO agent_tasks(tool,kind,pid,state,started) VALUES('places_import','thread',?,'running',?)",
                              (__import__("os").getpid(), time.time())).lastrowid
        self.assertEqual(agents.task(self.store, gone)["state"], "interrupted")
        self.assertEqual(agents.task(self.store, here)["state"], "interrupted")

    def test_a_face_scan_task_finishes_when_its_lock_is_free(self):
        task = agents.start_process(self.store, "people_scan", 999999)
        done = agents.task(self.store, task)
        self.assertEqual(done["state"], "done")
        self.assertIn("faces", done["result"])

    def test_wait_reports_progress_and_returns_when_done(self):
        gate = threading.Event()
        task = agents.start_thread(self.store, "places_import", lambda: (gate.wait(5), {"imported": 1})[1])["task"]
        sent = []
        threading.Timer(1.2, gate.set).start()
        result = call(self.service, "catalog_wait", {"task": task, "timeout": 10}, send=sent.append, meta={"progressToken": "w1"})
        data = result["structuredContent"]
        self.assertEqual((data["state"], data["result"]), ("done", {"imported": 1}))
        self.assertTrue(sent)
        self.assertEqual(sent[0]["method"], "notifications/progress")
        self.assertEqual(sent[0]["params"]["progressToken"], "w1")

    def test_wait_for_a_job_times_out_without_error(self):
        aid, _ = self.store.upsert({"provider": "local", "source_id": "j1", "filename": "dune.jpg", "kind": "photo"})
        job = self.store.enqueue(aid)["id"]
        data = call(self.service, "catalog_wait", {"job": job, "timeout": 1})["structuredContent"]
        self.assertEqual(data["state"], "queued")
        self.assertFalse(data["finished_waiting"])
        self.assertEqual(error_of(call(self.service, "catalog_wait", {}))["code"], "invalid")


if __name__ == "__main__":
    unittest.main()
