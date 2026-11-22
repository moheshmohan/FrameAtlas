"""The agent contract: every tool is reachable, labelled with its risk, checks its input, returns an
object of the promised shape, and fails with a code an agent can act on."""
import json
from pathlib import Path
import re
import tempfile
import unittest

from frameatlas import contract
from frameatlas.mcp import TOOLS, handle
from frameatlas.server import READ_ACTIONS
from frameatlas.service import Service
from frameatlas.store import Store

ROOT = Path(__file__).resolve().parent.parent / "frameatlas"
# Reachable over HTTP only, by design: the browser page's own session token.
HTTP_ONLY = {"session"}
# Read tools that start a local model or the native FilmCraft CLI, so they are not run here.
NOT_RUN = {"catalog_search_related": "asks a local writing model",
           "catalog_studio_ocr": "runs local OCR on a screenshot",
           "catalog_models": "asks the FilmCraft CLI which models are installed"}


def call(service, name, arguments=None, rid=1):
    reply = handle(service, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                             "params": {"name": name, "arguments": arguments or {}}})
    return reply["result"]


def error_of(result):
    return json.loads(result["content"][0]["text"])["error"]


def confirmed(service, name, arguments=None):
    """Call a tool the way an agent does when the owner's permissions ask for confirmation."""
    first = call(service, name, arguments)
    if not first["isError"] or error_of(first)["code"] != "needs_confirmation":
        return first
    return call(service, name, {**(arguments or {}), "confirmation": error_of(first)["data"]["confirmation"]})


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data")
        self.service = Service(self.store)
        self.aid, _ = self.store.upsert({"provider": "local", "source_id": "made-up-1", "filename": "harbour walk.jpg",
                                         "kind": "photo", "captured": "2024-03-02T10:00:00Z"})
        self.other, _ = self.store.upsert({"provider": "local", "source_id": "made-up-2", "filename": "harbour walk copy.jpg",
                                           "kind": "photo", "captured": "2024-03-02T10:00:00Z"})

    def tearDown(self):
        self.tmp.cleanup()

    # ---- the tool table
    def test_every_tool_is_defined_once_with_a_risk_tier(self):
        names = [t[0] for t in TOOLS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), set(contract.TIER_OF), "every tool needs exactly one tier in contract.TIERS")
        listed = handle(self.service, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in listed], names)
        for tool in listed:
            with self.subTest(tool=tool["name"]):
                self.assertGreater(len(tool["description"]), 20)
                self.assertEqual(tool["inputSchema"]["type"], "object")
                self.assertFalse(tool["inputSchema"]["additionalProperties"])
                for key, prop in tool["inputSchema"]["properties"].items():
                    self.assertIn(prop.get("type"), contract.TYPES, f"{key} needs a JSON type")
                self.assertEqual(tool["outputSchema"]["type"], "object")
                notes, tier = tool["annotations"], tool["_meta"]["frameatlas/tier"]
                self.assertEqual(notes["readOnlyHint"], tier == "read")
                self.assertEqual(notes["destructiveHint"], tier == "destructive")
                if tier == "outside":
                    self.assertTrue(notes["openWorldHint"])

    def test_http_get_actions_are_read_only_tools(self):
        by_action = {t[2]: t[0] for t in TOOLS if t[2]}
        for action in READ_ACTIONS:
            with self.subTest(action=action):
                self.assertIn(action, by_action, "a GET action needs an MCP tool")
                self.assertEqual(contract.TIER_OF[by_action[action]], "read")

    def test_every_service_and_page_action_has_a_tool(self):
        service = (ROOT / "service.py").read_text()
        actions = set(re.findall(r'action == "([a-z_]+)"', service))
        for group in re.findall(r'action in [({]([^)}]*)[)}]', service):
            actions |= set(re.findall(r'"([a-z_]+)"', group))
        page = "".join((ROOT / "static" / name).read_text() for name in ("app.js", "ideapad.js", "people.js"))
        calls = set(re.findall(r"""\bapi\(\s*['"`]([a-z_]+)""", page)) | set(re.findall(r"""fetch\('/api/([a-z_]+)""", page))
        self.assertGreater(len(actions), 50)
        self.assertGreater(len(calls), 40)
        tools = {t[2] for t in TOOLS if t[2]}
        self.assertEqual(sorted(actions - tools - contract.OWNER_ONLY), [], "service actions without an MCP tool")
        self.assertEqual(sorted(calls - tools - HTTP_ONLY - contract.OWNER_ONLY), [], "page actions without an MCP tool")
        self.assertEqual(tools & contract.OWNER_ONLY, set(), "owner-only actions must not be tools")

    # ---- results
    def test_read_tools_return_their_promised_shape_or_a_coded_error(self):
        arguments = {"catalog_asset": {"id": self.aid}, "catalog_resolve": {"id": self.aid},
                     "catalog_compare_sources": {"first": self.aid, "second": self.other},
                     "catalog_job": {"id": 1}, "person_get": {"id": 1}, "people_suggest": {"faces": [1]},
                     "people_compare": {"faces": [1]}, "catalog_picker_queued_items": {"session": "made-up"}}
        ran = 0
        for name in sorted(contract.TIERS["read"]):
            if name in NOT_RUN:
                continue
            with self.subTest(tool=name):
                result = call(self.service, name, arguments.get(name))
                if result["isError"]:
                    self.assertNotEqual(error_of(result)["code"], "internal", error_of(result))
                    continue
                ran += 1
                data = result["structuredContent"]
                self.assertEqual(json.loads(result["content"][0]["text"]), data)
                for key in contract.output_schema(name).get("required", []):
                    self.assertIn(key, data)
        self.assertGreaterEqual(ran, 19)
        self.assertLessEqual(set(NOT_RUN), contract.TIERS["read"])

    def test_search_and_asset_results_match_their_schemas(self):
        found = call(self.service, "catalog_search", {"query": "harbour"})["structuredContent"]
        self.assertEqual(found["total"], 2)
        asset = call(self.service, "catalog_asset", {"id": self.aid})["structuredContent"]
        self.assertEqual(asset["filename"], "harbour walk.jpg")

    # ---- input checking and errors
    def test_bad_arguments_are_named_before_anything_runs(self):
        cases = [({"limit": "5"}, "arguments.limit must be an integer"),
                 ({"limit": True}, "arguments.limit must be an integer"),
                 ({"limit": 0}, "arguments.limit must be at least 1"),
                 ({"source_access": "anywhere"}, "arguments.source_access must be one of"),
                 ({"arbitrary_sql": "x"}, "arguments.arbitrary_sql is not a known argument")]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                result = call(self.service, "catalog_search", arguments)
                self.assertTrue(result["isError"])
                error = error_of(result)
                self.assertEqual(error["code"], "invalid_arguments")
                self.assertIn(expected, error["message"])
                self.assertFalse(error["retryable"])
        self.assertEqual(error_of(call(self.service, "no_such_tool"))["code"], "unknown_tool")
        reply = handle(self.service, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                      "params": {"name": "catalog_stats", "arguments": ["not", "an", "object"]}})
        self.assertEqual(error_of(reply["result"])["code"], "invalid_arguments")

    def test_errors_carry_codes_agents_can_act_on(self):
        self.assertEqual(error_of(call(self.service, "catalog_asset", {"id": "missing"}))["code"], "not_found")
        for name in ("places_forget", "people_forget"):
            with self.subTest(tool=name):
                error = error_of(confirmed(self.service, name))
                self.assertEqual(error["code"], "needs_confirmation")
                self.assertIn("confirm", error["hint"])
        self.assertEqual(confirmed(self.service, "places_forget", {"confirm": True})["structuredContent"], {"forgotten": 0})
        self.assertIn("forgotten_faces", confirmed(self.service, "people_forget", {"confirm": True})["structuredContent"])

    def test_classify(self):
        from frameatlas.worker import WorkerBusy
        from frameatlas.scene_batch import PickerSelectionUnavailable
        cases = [(WorkerBusy("Another worker owns the catalogue"), "busy", True),
                 (PickerSelectionUnavailable("Make a new selection"), "needs_user", False),
                 (RuntimeError("GPU busy with another local task. Retry after it finishes."), "busy", True),
                 (RuntimeError("Native operation timed out; retry a shorter source"), "timeout", True),
                 (RuntimeError("Local speech model is not installed: tiny"), "missing_model", False),
                 (ValueError("The scene changed since you opened it."), "stale", False),
                 (ValueError("Choose two different items"), "invalid", False),
                 (RuntimeError("FilmCraft: something broke"), "failed", True),
                 (KeyError("x"), "invalid", False),
                 (ZeroDivisionError("division by zero"), "internal", False),
                 (contract.ToolError("deferred", "Later", "Try at noon"), "deferred", True)]
        for exc, code, retryable in cases:
            with self.subTest(exc=exc):
                got = contract.classify(exc)
                self.assertEqual((got["code"], got["retryable"]), (code, retryable))
                self.assertTrue(got["hint"])
        self.assertEqual(contract.classify(contract.ToolError("deferred", "Later", "Try at noon"))["hint"], "Try at noon")

    def test_check_covers_the_schema_features_the_tools_use(self):
        schema = {"type": "object", "additionalProperties": False, "required": ["ids"], "properties": {
            "ids": {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True, "items": {"type": "string", "maxLength": 3}},
            "n": {"type": "number", "maximum": 1}, "scope": {"type": "object", "minProperties": 1}}}
        self.assertEqual(contract.check(schema, {"ids": ["a"], "n": 0.5, "scope": {"x": 1}}), [])
        problems = contract.check(schema, {"ids": ["abcd", "abcd", "x"], "n": 2, "scope": {}})
        self.assertIn("arguments.ids must have at most 2 items", problems)
        self.assertIn("arguments.ids must not repeat items", problems)
        self.assertIn("arguments.ids[0] must have at most 3 characters", problems)
        self.assertIn("arguments.n must be at most 1", problems)
        self.assertIn("arguments.scope needs at least 1 field(s)", problems)
        self.assertEqual(contract.check(schema, {}), ["arguments.ids is required"])
        deep = {"type": "array", "items": {}}
        deep["items"] = deep
        value = []
        for _ in range(40):
            value = [value]
        self.assertEqual(contract.check(deep, value), [])  # bounded, no recursion error

    # ---- the new tools
    def test_merge_and_idea_history_are_tools(self):
        merged = confirmed(self.service, "catalog_merge", {"keep": self.aid, "duplicate": self.other})
        self.assertFalse(merged["isError"], merged)
        self.assertEqual(merged["structuredContent"]["id"], self.aid)
        self.assertEqual(error_of(call(self.service, "catalog_asset", {"id": self.other}))["code"], "not_found")
        self.assertEqual(call(self.service, "catalog_idea_history")["structuredContent"], {"items": []})

    def test_batch_runs_calls_in_order_and_stops_at_an_error(self):
        result = call(self.service, "catalog_batch", {"calls": [
            {"tool": "idea_pad_add", "arguments": {"title": "Harbour walks through the year"}},
            {"tool": "idea_pad_list"},
            {"tool": "catalog_search", "arguments": {"limit": "x"}},
            {"tool": "catalog_stats"}]})
        self.assertFalse(result["isError"])
        data = result["structuredContent"]
        self.assertEqual((data["ran"], data["stopped"]), (3, True))
        self.assertEqual(data["results"][1]["result"]["total"], 1)
        self.assertEqual(data["results"][2]["error"]["code"], "invalid_arguments")
        go_on = call(self.service, "catalog_batch", {"stop_on_error": False, "calls": [
            {"tool": "no_such_tool"}, {"tool": "catalog_stats"}]})["structuredContent"]
        self.assertEqual([r["ok"] for r in go_on["results"]], [False, True])
        self.assertEqual(go_on["results"][0]["error"]["code"], "unknown_tool")
        self.assertFalse(go_on["stopped"])

    def test_batch_refuses_destructive_outside_and_nested_calls(self):
        for name in ("idea_pad_delete", "people_forget", "catalog_picker_create", "catalog_retrieve", "catalog_batch"):
            with self.subTest(tool=name):
                data = call(self.service, "catalog_batch", {"calls": [{"tool": name, "arguments": {}}]})["structuredContent"]
                self.assertFalse(data["results"][0]["ok"])
                self.assertEqual(data["results"][0]["error"]["code"], "invalid_arguments")
        self.assertEqual(error_of(call(self.service, "catalog_batch", {"calls": []}))["code"], "invalid_arguments")
        self.assertEqual(error_of(call(self.service, "catalog_batch", {"calls": [{"tool": "catalog_stats"}] * 26}))["code"],
                         "invalid_arguments")

    def test_initialize_names_the_version_and_the_rules(self):
        reply = handle(self.service, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]
        self.assertEqual(reply["serverInfo"]["version"], "0.2.0")
        self.assertIn("destructiveHint", reply["instructions"])


if __name__ == "__main__":
    unittest.main()
