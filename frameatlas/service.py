import json
from collections import Counter
from pathlib import Path
import time
from urllib.parse import quote

from . import importers, ideas
from .worker import broker, resolve, Cache, ANALYSIS_RESERVE


class Service:
    def __init__(self, store):
        self.store = store

    def dispatch(self, action, args):
        if not isinstance(args, dict):
            raise ValueError("Arguments must be a JSON object")
        if action == "search":
            allowed = {"query", "provider", "kind", "status", "category", "since", "until", "limit", "offset", "bounds", "capture_month", "speech_only", "scene_only", "filename", "source_access", "speech_state"}
            return self.store.search(**{k: v for k, v in args.items() if k in allowed})
        if action == "search_related":
            filters = {k: args[k] for k in ("provider", "kind", "status", "category", "since", "until", "bounds", "capture_month", "speech_only", "scene_only", "source_access", "speech_state")
                       if k in args}
            return ideas.search_related(self.store, args.get("query", ""), args.get("model", "phi4-mini:latest"), **filters)
        if action == "asset":
            asset = self.store.get(args.get("id", ""))
            citation_fields = {"start", "end", "sample_time", "scene_key", "part_filename"}
            if citation_fields.intersection(args):
                from .store import resolve_citation
                asset["citation_match"] = resolve_citation(
                    asset["scenes"], args.get("start"), args.get("end"), args.get("sample_time"),
                    args.get("scene_key", ""), args.get("part_filename", ""))
            return asset
        if action == "coverage":
            return self.store.coverage(args.get("provider", ""), args.get("limit", 24), args.get("offset", 0),
                                       kind=args.get("kind", ""))
        if action == "compare_sources":
            from .fingerprint import compare_sources
            return compare_sources(self.store, args.get("first", ""), args.get("second", ""))
        if action == "stats":
            result = self.store.stats()
            cache = Cache(self.store.root,cap=self.store.storage_limit())
            result.update({"temporary_bytes": cache.usage(), "temporary_cap": cache.cap,
                           "available_budget": max(0,cache.budget()-ANALYSIS_RESERVE),
                           "available_editing_budget":cache.budget(),"analysis_working_reserve":ANALYSIS_RESERVE,
                           "full_originals_mirrored_in_catalogue": False,
                           "original_retention_policy": "Catalogue stores metadata and small previews. User-selected editing downloads are retained in explicitly chosen editing folders, never expired automatically.",
                           "inference": "Local FilmCraft + loopback Ollama only",
                           "coverage": "Partial inventories. No provider has been fully scanned."})
            return result
        if action == 'storage_limit':
            from .store import STORAGE_LIMITS_GIB
            gib=args.get('gib')
            if not isinstance(gib,int) or isinstance(gib,bool) or gib not in STORAGE_LIMITS_GIB:
                raise ValueError('Choose a temporary storage limit of 2, 4, 8, 16 or 32 GiB')
            if Cache(self.store.root).usage()>gib*1024**3:
                raise ValueError('Finish or clean the current temporary source before choosing a smaller limit')
            cap=self.store.storage_limit(gib)
            return {'temporary_cap':cap,'applies_to':'New jobs; current work keeps its existing allowance','free_space_reserve':3*1024**3}
        if action == "jobs":
            return {"items": self.store.jobs(), **self.store.queue_state(include_waiting=True)}
        if action == "queue_control":
            if "paused" not in args:
                raise ValueError("Supply paused: true or false")
            return self.store.queue_state(args["paused"])
        if action == "enqueue":
            from .store import INTERACTIVE_ANALYSIS_LIMIT
            ids = args.get("ids") or [args.get("id", "")]
            if not isinstance(ids, list) or len(ids) > 200 or any(not isinstance(aid, str) for aid in ids):
                raise ValueError("Queue at most 200 items at once")
            ids = list(dict.fromkeys(ids))
            options = self.store.analysis_options(args.get("options"))
            if options.get("interactive") and len(ids) > INTERACTIVE_ANALYSIS_LIMIT:
                raise ValueError(f"Queue at most {INTERACTIVE_ANALYSIS_LIMIT} items per interactive analysis request")
            # Validate everything first, avoiding partially queued malformed requests.
            for aid in ids:
                self.store.get(aid, include_scenes=False)
            return {"jobs": [self.store.enqueue(aid, options) for aid in ids]}
        if action == "studio_batch":
            return self.store.enqueue_studio_batch(args.get("ids"), args.get("options"))
        if action == "cancel":
            return self.store.cancel(args.get("id"))
        if action == "retrieve":
            from .retrieval import REFERENCE_FIELDS
            reference = {key: args[key] for key in REFERENCE_FIELDS if key in args}
            return self.store.enqueue_retrieval(args.get("id", ""), args.get("destination", ""), reference)
        if action == "retry_speech":
            return self.store.enqueue_speech(args.get("id", ""), args.get("language", "auto"),
                                             args.get("model", "whisper-base"), args.get("prefer_cloud", False),
                                             args.get("replace", False))
        if action == "job":
            return self.store.job(args.get("id"))
        if action == 'studio_confirm':
            from .studio_stage import confirm
            return confirm(self.store, args.get('id'), args.get('individual', False))
        if action == 'studio_cleanup':
            from .studio_stage import cleanup
            return cleanup(self.store,args.get('id'))
        if action == "resolve":
            from .retrieval import requested_reference
            with_scene = "scene_key" in args or "part_filename" in args
            asset = self.store.get(args.get("id", ""), include_scenes=with_scene)
            clip = requested_reference(asset, args)
            selection = self.store.picker_access(asset["locator"].get("session_id")) if asset["provider"] == "google_photos" else None
            plan = resolve(asset, selection)
            if clip:
                plan["asset_id"] = asset["id"]
                plan["clip"] = clip
            return plan
        if action == "annotate":
            return self.store.annotate(args.get("id", ""), args.get("tags"), args.get("summary"))
        if action == "correct_scene":
            return self.store.correct_scene(args.get("id", ""), args.get("ordinal"), args.get("caption", ""),
                                            args.get("scene_key"))
        if action == "import_native":
            return importers.import_native(self.store)
        if action == "studio_ocr":
            from .studio_inventory import extract
            return extract(args)
        if action == "studio_import":
            from .studio_inventory import import_rows
            return import_rows(self.store, args)
        if action == "studio_import_batch":
            from .studio_inventory import import_captures
            return import_captures(self.store, args.get('captures'))
        if action == "import_manifest":
            return importers.import_manifest(self.store, args)
        if action == "import_takeout":
            return importers.import_takeout(self.store, args.get("path", ""))
        if action == "link_takeout_location":
            from .geography import link_takeout_location
            return link_takeout_location(self.store, args.get("picker_id"), args.get("takeout_id"))
        if action == "models":
            from .native import cli
            try:
                writing = ideas.local_models()
            except Exception:
                writing = []
            return {"scenes": cli("scenes", "status"), "speech": cli("exec", "transcript.models"), "writing": writing}
        if action == "ideas":
            if "asset_ids" in args and args["asset_ids"] is None:
                raise ValueError("Choose distinct catalogue asset IDs or omit asset_ids")
            if "search_scope" in args and args["search_scope"] is None:
                raise ValueError("Provide Library search filters or omit search_scope")
            return ideas.generate(self.store, args.get("prompt", ""), args.get("model", "qwen3:8b"),
                                  args.get("expand_search", False), asset_ids=args.get("asset_ids"),
                                  search_scope=args.get("search_scope"))
        if action == "idea_history":
            with self.store.connect() as db:
                return {"items": [{"id": x["id"], "prompt": x["prompt"], "result": json.loads(x["result"]), "created": x["created"]} for x in db.execute("SELECT * FROM ideas ORDER BY id DESC LIMIT 12")]}
        if action == "picker_create":
            from .picker_sessions import record
            result=broker(self.store.root, "create")
            record(self.store,result.get('id'),expires=result.get('expires'))
            return result
        if action == 'picker_sessions':
            from .picker_sessions import list_batches
            return list_batches(self.store)
        if action == 'picker_queued_items':
            from .picker_sessions import queued_items
            return queued_items(self.store, args.get('session'), args.get('limit', 25), args.get('offset', 0))
        if action == 'picker_reselection':
            from .picker_sessions import reselection_items
            return reselection_items(self.store, args.get('limit', 25), args.get('offset', 0))
        if action == "picker_import":
            from .picker_sessions import record,session_id,timestamp
            session = session_id(args.get("session", ""))
            queue_analysis = args.get("queue_analysis", False)
            if not isinstance(queue_analysis, bool):
                raise ValueError("queue_analysis must be boolean")
            result = broker(self.store.root, "list", session)
            if not result.get("ready"):
                return result
            added = 0
            queued = 0
            ids = []
            with self.store.connect() as db:
                for p in result.get("items", []):
                    aid, new = self.store.upsert({"provider": "google_photos", "source_id": p["id"], "filename": p["filename"],
                        "kind": p["kind"], "captured": p.get("created", ""),
                        "locator": {"picker_id": p["id"], "session_id": session, "filename": p["filename"]},
                        "metadata": {"inventory": "Google Picker selection", "gps_status": "not_provided_by_picker", "mime_type": p.get("mimeType")}}, db)
                    added += int(new); ids.append(aid)
                self.store.imported("google_photos", "Picker selection", "Only items deliberately selected in this Picker session; no library-wide scan.", len(ids), db)
                dates=[]
                for item in result.get('items',[]):
                    try:
                        if item.get('created'):dates.append(timestamp(item['created']))
                    except ValueError:
                        pass
                record(self.store,session,selected_count=len(ids),added_count=added,
                       captured_min=min(dates) if dates else '',captured_max=max(dates) if dates else '',
                       capture_months=dict(Counter(date[:7] for date in dates)),db=db)
                if queue_analysis:
                    now = time.time()
                    queued = db.execute("""INSERT INTO jobs(asset_id,state,created,updated)
                        SELECT a.id,'queued',?,? FROM json_each(?) picked
                        JOIN assets a ON a.id=picked.value
                        WHERE a.status='inventory' AND a.kind IN ('photo','video')
                          AND a.id NOT IN (SELECT asset_id FROM jobs WHERE task='analysis')
                        ORDER BY picked.key""", (now, now, json.dumps(ids))).rowcount
            return {"ready": True, "seen": len(ids), "added": added, "ids": ids, "queued": queued,
                    "note": "Download access expires. Reselect expired items. The import itself downloads no originals."}
        if action == "picker_delete":
            from .picker_sessions import record,session_id
            session = session_id(args.get("session", ""))
            with self.store.connect() as db:
                if db.execute("SELECT 1 FROM jobs j JOIN assets a ON a.id=j.asset_id WHERE json_extract(a.locator,'$.session_id')=? AND j.state IN ('queued','running','cancelling')", (session,)).fetchone():
                    raise ValueError("Finish or cancel this session's analysis jobs before closing it")
            try:
                result = broker(self.store.root, "delete", session)
            except ValueError as exc:
                if "404" not in str(exc):
                    raise
                result = {"ok": True, "already_closed": True}
            with self.store.connect() as db:
                db.execute("UPDATE assets SET locator=json_remove(locator,'$.session_id') WHERE json_extract(locator,'$.session_id')=?", (session,))
                record(self.store,session,closed=True,db=db)
            return result
        if action == "attach_local":
            aid = args.get("id", "")
            a = self.store.get(aid)
            p = Path(str(args.get("path", ""))).expanduser().resolve(strict=True)
            if not p.is_file():
                raise ValueError("Choose an existing original file")
            # Explicit association only; filenames alone never prove asset identity.
            with self.store.connect() as db:
                db.execute("UPDATE assets SET local_path=?,updated=? WHERE id=?", (str(p), time.time(), aid))
            return {"id": a["id"], "filename": a["filename"], "linked_read_only": True}
        if action == "merge":
            # Explicitly bind Takeout metadata to a user-verified Picker item.
            keep, duplicate = self.store.get(args.get("keep", "")), self.store.get(args.get("duplicate", ""))
            if keep["id"] == duplicate["id"] or keep["provider"] != duplicate["provider"]:
                raise ValueError("Choose two different items from the same provider")
            if keep["status"] != "inventory" or duplicate["status"] != "inventory":
                raise ValueError("Merge inventory records before analysis; existing scene/transcript records are never discarded")
            with self.store.connect() as db:
                if db.execute("SELECT 1 FROM jobs WHERE asset_id IN (?,?)", (keep["id"], duplicate["id"])).fetchone():
                    raise ValueError("Cannot merge items with job history")
                metadata = {**keep["metadata"], **duplicate["metadata"], "merged_source_id": duplicate["source_id"]}
                db.execute("UPDATE assets SET metadata=?,cloud_url=?,local_path=?,updated=? WHERE id=?", (json.dumps(metadata), keep["cloud_url"] or duplicate["cloud_url"], keep["local_path"] or duplicate["local_path"], time.time(), keep["id"]))
                db.execute("DELETE FROM search WHERE asset_id=?", (duplicate["id"],))
                db.execute("DELETE FROM assets WHERE id=?", (duplicate["id"],))
            return self.store.get(keep["id"])
        raise ValueError("Unknown action")
