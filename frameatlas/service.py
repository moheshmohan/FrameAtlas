import json
import os
from collections import Counter
from pathlib import Path
import time
from urllib.parse import quote

from .contract import ToolError
from . import importers, ideas
from . import idea_pad, metadata as capture_info, people, places
from .metadata import picker_camera
from .worker import broker, resolve, Cache, ANALYSIS_RESERVE


def scan_running(store):
    import fcntl
    try:
        with open(Path(store.root) / "face-scan.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def start_scan(store, action, extra=()):
    """Start the face scan (or a regroup, or a sort) as its own process; it holds the scan lock."""
    import subprocess, sys
    if scan_running(store):
        return {"started": False, "running": True}
    log = open(Path(store.root) / "face-scan.log", "a")
    process = subprocess.Popen([sys.executable, "-m", "frameatlas.people", action, *extra], cwd=str(Path(__file__).resolve().parent.parent),
                               stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
                               env={**os.environ, "FRAMEATLAS_DATA": str(store.root)})
    log.close()
    from . import agents
    task = agents.start_process(store, {"scan": "people_scan", "group": "people_regroup", "sort": "people_autosort"}.get(action, action), process.pid)
    return {"started": True, "running": True, "task": task}


def whole(value):
    """A count from a query string ("25") or JSON (25); anything else is passed on to be refused."""
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 9:
        return int(value)
    return value


AGENT_ACTIONS = {"activity", "activity_undo", "permissions", "permissions_set", "confirmation_decide", "tasks", "task", "wait",
                 "ui_show", "ui_select", "ui_state", "ui_report", "ui_poll"}


class Service:
    def __init__(self, store):
        self.store = store

    def agent_action(self, action, args):
        from . import agents
        if action == "activity":
            flag = lambda k: args.get(k) in (True, "true")
            return agents.activity(self.store, include_reads=flag("include_reads"), tool=args.get("tool", ""),
                                   client=args.get("client", ""), limit=whole(args.get("limit", 100)), offset=whole(args.get("offset", 0)))
        if action == "activity_undo":
            return agents.undo(self.store, whole(args.get("id")), client=args.get("client", "owner"))
        if action == "permissions":
            return {"permissions": agents.permissions(self.store), "policies": list(agents.POLICIES)}
        if action == "permissions_set":  # owner only: no MCP tool reaches it
            return agents.set_permissions(self.store, args.get("permissions"))
        if action == "confirmation_decide":  # owner only: no MCP tool reaches it
            return agents.decide_confirmation(self.store, args.get("token", ""), args.get("decision", ""))
        if action == "tasks":
            return agents.tasks(self.store, limit=whole(args.get("limit", 50)), state=args.get("state", ""))
        if action == "task":
            return agents.task(self.store, whole(args.get("id")))
        if action == "wait":
            return agents.wait(self, task_id=args.get("task"), job_id=args.get("job"), timeout=args.get("timeout", 30))
        from . import ui_channel
        if action == "ui_show":
            return ui_channel.show(self.store, args)
        if action == "ui_select":
            return ui_channel.select(self.store, args)
        if action == "ui_state":
            return ui_channel.state(self.store)
        if action == "ui_report":  # the open page only
            return ui_channel.report(self.store, args.get("state"))
        if action == "ui_poll":  # the open page only
            return ui_channel.poll(self.store, whole(args.get("after", -1)), args.get("wait", ui_channel.POLL_SECONDS))
        raise ValueError("Unknown action")

    def dispatch(self, action, args):
        if not isinstance(args, dict):
            raise ValueError("Arguments must be a JSON object")
        if action in AGENT_ACTIONS:
            return self.agent_action(action, args)
        if action == "search":
            allowed = {"query", "provider", "kind", "status", "category", "since", "until", "limit", "offset", "bounds", "capture_month", "speech_only", "scene_only", "filename", "source_access", "speech_state", "person", "place"}
            return self.store.search(**{k: v for k, v in args.items() if k in allowed})
        if action == "search_related":
            filters = {k: args[k] for k in ("provider", "kind", "status", "category", "since", "until", "bounds", "capture_month", "speech_only", "scene_only", "source_access", "speech_state")
                       if k in args}
            return ideas.search_related(self.store, args.get("query", ""), args.get("model", ""), **filters)
        if action == "asset":
            asset = self.store.get(args.get("id", ""))
            citation_fields = {"start", "end", "sample_time", "scene_key", "part_filename"}
            if citation_fields.intersection(args):
                from .store import resolve_citation
                asset["citation_match"] = resolve_citation(
                    asset["scenes"], args.get("start"), args.get("end"), args.get("sample_time"),
                    args.get("scene_key", ""), args.get("part_filename", ""))
            asset["facts"] = capture_info.facts(asset)
            asset["people"] = people.asset_people(self.store, asset["id"])
            with self.store.connect() as db:
                asset["places"] = [dict(r) for r in db.execute("""SELECT p.id, p.name, p.address, p.latitude, p.longitude
                    FROM asset_places a JOIN places p ON p.id=a.place_id WHERE a.asset_id=? ORDER BY p.name""", (asset["id"],))]
            return asset
        if action == "people_status":
            return {**people.progress(self.store), "running": scan_running(self.store)}
        if action in ("people_scan", "people_regroup"):
            return start_scan(self.store, "scan" if action == "people_scan" else "group")
        if action == "people":
            flag = lambda k: args.get(k) in (True, "true")
            return people.list_people(self.store, include_hidden=flag("include_hidden"), named_only=flag("named_only"),
                                      limit=args.get("limit", 200), offset=args.get("offset", 0), query=args.get("query", ""))
        if action == "person":
            return people.person(self.store, args.get("id"), limit=args.get("limit", 60), offset=args.get("offset", 0))
        if action == "person_update":
            known = {"id", "name", "hidden", "notes"}
            if set(args) - known:
                raise ValueError("Change any of: name, hidden, notes")
            return people.update_person(self.store, args.get("id"), name=args.get("name"), hidden=args.get("hidden"), notes=args.get("notes"))
        if action == "person_merge":
            return people.merge(self.store, args.get("into"), args.get("other"))
        if action == "face_assign":
            if not isinstance(args.get("new_person", False), bool):
                raise ValueError("new_person must be true or false")
            return people.assign_face(self.store, args.get("face"), args.get("person"), new_person=args.get("new_person", False))
        if action == "faces_link":
            if set(args) - {"faces", "person", "new_person", "name"}:
                raise ValueError("Link with: faces, person or new_person, name")
            if not isinstance(args.get("new_person", False), bool):
                raise ValueError("new_person must be true or false")
            return people.link_faces(self.store, args.get("faces"), args.get("person"),
                                     new_person=args.get("new_person", False), name=args.get("name", ""))
        if action == "faces_unlink":
            if set(args) - {"faces"}:
                raise ValueError("Unlink with: faces")
            return people.unlink_faces(self.store, args.get("faces"))
        if action == "faces_unsorted":
            if set(args) - {"limit", "offset"}:
                raise ValueError("List with: limit, offset")
            return people.unsorted_faces(self.store, limit=args.get("limit", 120), offset=whole(args.get("offset", 0)))
        if action == "people_suggest":
            if set(args) - {"faces", "limit"}:
                raise ValueError("Suggest with: faces, limit")
            faces = args.get("faces")
            if isinstance(faces, str):
                faces = [f for f in faces.split(",") if f.strip()]
            return people.suggest(self.store, faces, limit=args.get("limit", 8))
        if action == "people_autosort":
            if set(args) - {"apply"} or not isinstance(args.get("apply", False), bool):
                raise ValueError("Sort with: apply (true or false)")
            return start_scan(self.store, "sort", ["--apply"] if args.get("apply") else [])
        if action == "people_review":
            if set(args) - {"status", "actor", "action", "limit", "offset"}:
                raise ValueError("Review with: status, actor, action, limit, offset")
            return people.review(self.store, status=args.get("status", "proposed"), actor=args.get("actor", ""),
                                 action=args.get("action", ""), limit=args.get("limit", 50), offset=args.get("offset", 0))
        if action == "people_review_decide":
            if set(args) - {"id", "decision"}:
                raise ValueError("Decide with: id, decision")
            return people.decide(self.store, args.get("id"), args.get("decision"))
        if action == "people_compare":
            if set(args) - {"person", "other", "faces", "count"}:
                raise ValueError("Compare with: person, other or faces, count")
            faces = args.get("faces")
            if isinstance(faces, str):
                faces = [f for f in faces.split(",") if f.strip()]
            return people.compare(self.store, args.get("person"), args.get("other"), faces, n=args.get("count", 6))
        if action == "people_import_names":
            if set(args) - {"source", "people", "apply"} or not isinstance(args.get("apply", False), bool):
                raise ValueError("Import with: source, people, apply")
            return people.import_names(self.store, args.get("source", "google_photos"), args.get("people"), apply=args.get("apply", False))
        if action == "places_import":
            if set(args) - {"source", "places"}:
                raise ValueError("Import with: source, places")
            return places.import_places(self.store, args.get("source", "google_photos"), args.get("places"))
        if action == "places":
            return places.list_places(self.store, args.get("query", ""), whole(args.get("limit", 200)), whole(args.get("offset", 0)))
        if action == "places_trips":
            for key in ("since", "until"):
                if args.get(key):
                    idea_pad._date(args[key], key.title())
            return places.trips(self.store, args.get("since", ""), args.get("until", ""), args.get("kind", ""), whole(args.get("limit", 200)))
        if action == "places_forget":
            if args.get("confirm") is not True:
                raise ToolError("needs_confirmation", "Pass confirm: true to delete every imported place")
            return places.forget(self.store, args.get("source", "google_photos"))
        if action == "people_forget":
            if args.get("confirm") is not True:
                raise ToolError("needs_confirmation", "Pass confirm: true to delete every face and person")
            return people.forget(self.store)
        if action == "metadata_coverage":
            return capture_info.coverage(self.store)
        if action == "metadata_backfill":
            return capture_info.backfill(self.store)
        if action == "idea_pad":
            return idea_pad.list_ideas(self.store, args.get("status", ""), args.get("origin", ""),
                                       whole(args.get("limit", 200)), whole(args.get("offset", 0)))
        if action == "idea_pad_add":
            known = {"title", "logline", "notes", "format", "tags", "status", "asset_ids"}
            if set(args) - known:
                raise ValueError("Unknown idea field: " + sorted(set(args) - known)[0])
            return idea_pad.add(self.store, args.get("title"), **{k: args[k] for k in known - {"title"} if k in args})
        if action == "idea_pad_update":
            return idea_pad.update(self.store, args.get("id"), **{k: v for k, v in args.items() if k != "id"})
        if action == "idea_pad_delete":
            return idea_pad.delete(self.store, args.get("id"))
        if action == "idea_pad_develop":
            if set(args) - {"id", "model"}:
                raise ValueError("Develop with: id, model")
            return idea_pad.develop(self.store, args.get("id"), args.get("model", ""))
        if action == "idea_pad_moments":
            return idea_pad.moments(self.store, args.get("since", ""), args.get("until", ""), whole(args.get("limit", 40)),
                                    kind=args.get("kind", ""))
        if action == "idea_pad_generate":
            return idea_pad.generate(self.store, args.get("model", ""), args.get("count", 5),
                                     args.get("since", ""), args.get("until", ""), args.get("focus", ""),
                                     args.get("include_used", False), kind=args.get("kind", ""))
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
            idea_pad.adopt_story_runs(self.store)
            result = ideas.generate(self.store, args.get("prompt", ""), args.get("model", ""),
                                    args.get("expand_search", False), asset_ids=args.get("asset_ids"),
                                    search_scope=args.get("search_scope"))
            with self.store.connect() as db:
                result["saved_ids"] = idea_pad.save_story(db, result, args.get("prompt", ""), result.get("run"))
            return result
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
                        "metadata": {"inventory": "Google Picker selection", "gps_status": "not_provided_by_picker", "mime_type": p.get("mimeType"),
                                     **picker_camera(p.get("capture"))}}, db)
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
