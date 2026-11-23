"""Search by meaning: previews made into vectors by FilmCraft (here a stand-in script), one index file,
and the nearest previews to a query, narrowed by catalogue filters."""
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest

from frameatlas import semantic
from frameatlas.mcp import handle
from frameatlas.service import Service
from frameatlas.store import Store

# A stand-in for filmcraft-cli: a picture's vector is chosen by a word in its bytes (red, blue or
# green), a text's by the word it contains; media.nearest compares f16 rows like the real one.
FAKE_CLI = r'''#!/usr/bin/env python3
import base64, json, math, struct, sys
DIMS = 768
def vec(word):
    v = [0.0] * DIMS
    v[{"red": 0, "blue": 1, "green": 2}.get(word, 3)] = 1.0
    return v
def word_of(text):
    return next((w for w in ("red", "blue", "green") if w in text), "other")
def f16(v):
    return base64.b64encode(struct.pack("<%de" % len(v), *v)).decode()
def embed(params):
    pics = []
    for p in params.get("paths", []):
        try:
            data = open(p, "rb").read().decode("latin-1")
        except OSError as e:
            pics.append({"path": p, "error": str(e)}); continue
        if "broken" in data:
            pics.append({"path": p, "error": "cannot decode"}); continue
        pics.append({"path": p, "width": 8, "height": 8, "embedding": f16(vec(word_of(data)))})
    return {"pictures": pics, "texts": [], "model": "fake-siglip", "dimensions": DIMS, "precision": "f16"}
args = sys.argv[1:]
if args[0] == "run":
    for n, line in enumerate(open(args[1])):
        cmd = json.loads(line)
        print(json.dumps({"line": n + 1, "id": cmd["id"], "ok": True, "result": embed(cmd["params"])}))
elif args[:2] == ["exec", "media.nearest"]:
    p = json.loads(args[2])
    q = vec(word_of(p["query"]))
    raw = open(p["index"], "rb").read()
    hits = []
    for row in range(len(raw) // (DIMS * 2)):
        v = struct.unpack_from("<%de" % DIMS, raw, row * DIMS * 2)
        hits.append({"row": row, "cosine": sum(a * b for a, b in zip(v, q)), "match": 0.5})
    hits.sort(key=lambda h: -h["cosine"])
    print(json.dumps({"hits": hits[:p["limit"]], "rows": len(raw) // (DIMS * 2)}))
'''


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "data")
        self.service = Service(self.store)
        self.cli = self.root / "filmcraft-cli"
        self.cli.write_text(FAKE_CLI.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IEXEC)
        thumbs = self.store.root / "thumbnails"
        thumbs.mkdir(exist_ok=True)
        self.ids = {}
        for i, (word, kind) in enumerate([("red", "photo"), ("blue", "photo"), ("red", "video"), ("broken", "photo"), ("green", "photo")]):
            aid, _ = self.store.upsert({"provider": "local", "source_id": f"s{i}", "filename": f"IMG_{i}.jpg", "kind": kind,
                                        "captured": f"2024-0{i + 1}-01T10:00:00Z"})
            (thumbs / f"{aid}.jpg").write_bytes(f"made-up preview, {word}".encode())
            with self.store.connect() as db:
                db.execute("UPDATE assets SET thumbnail=? WHERE id=?", (f"{aid}.jpg", aid))
            self.ids.setdefault(word, []).append(aid)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_then_search(self):
        built = semantic.build(self.store, cli=str(self.cli))
        self.assertEqual((built["added"], built["failed"], built["left"], built["consistent"]), (4, 1, 0, True))
        self.assertEqual(semantic.index_path(self.store).stat().st_size, 4 * semantic.ROW_BYTES)
        again = semantic.build(self.store, cli=str(self.cli))
        self.assertEqual(again["added"], 0, "a second build only embeds new previews")
        found = semantic.search(self.store, "a red sunset", limit=2, cli=str(self.cli))
        self.assertEqual({i["id"] for i in found["items"]}, set(self.ids["red"]))
        self.assertAlmostEqual(found["items"][0]["cosine"], 1.0, places=3)
        only_photos = semantic.search(self.store, "red", limit=5, cli=str(self.cli), kind="photo")
        self.assertEqual(only_photos["items"][0]["id"], self.ids["red"][0])
        self.assertNotIn(self.ids["red"][1], [i["id"] for i in only_photos["items"]])
        videos = semantic.search(self.store, "red", limit=5, cli=str(self.cli), kind="video", provider="local")
        self.assertEqual([i["id"] for i in videos["items"]], [self.ids["red"][1]])
        self.assertEqual(semantic.search(self.store, "red", cli=str(self.cli), provider="google_photos")["items"], [])

    def test_errors_are_explained(self):
        with self.assertRaises(ValueError) as caught:
            semantic.search(self.store, "red", cli=str(self.cli))
        self.assertIn("semantic_build", caught.exception.hint)
        with self.assertRaises(ValueError):
            semantic.search(self.store, " ", cli=str(self.cli))
        semantic.build(self.store, cli=str(self.cli))
        with open(semantic.index_path(self.store), "ab") as f:
            f.write(b"\0" * 10)
        self.assertFalse(semantic.status(self.store)["consistent"])
        with self.assertRaises(ValueError):
            semantic.search(self.store, "red", cli=str(self.cli))
        self.assertEqual(semantic.reset(self.store)["embedded"], 0)
        self.assertFalse(semantic.index_path(self.store).exists())
        bad = self.root / "bad-cli"
        bad.write_text("#!/bin/sh\necho nope >&2\nexit 3\n")
        bad.chmod(0o755)
        with self.assertRaises(ValueError) as caught:
            semantic.build(self.store, cli=str(bad))
        self.assertIn("nope", str(caught.exception))

    def test_tools(self):
        def call(name, arguments=None):
            r = handle(self.service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}})["result"]
            return r.get("structuredContent") or json.loads(r["content"][0]["text"])
        self.assertEqual(call("semantic_status")["previews"], 5)
        self.assertEqual(call("catalog_search_meaning", {"query": "red"})["error"]["code"], "invalid")
        token = call("semantic_reset")["error"]["data"]["confirmation"]
        self.assertEqual(call("semantic_reset", {"confirmation": token})["embedded"], 0)


if __name__ == "__main__":
    unittest.main()
