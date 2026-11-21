"""MCP over stdio. Uses the same service and database as the human UI."""
import json
import sys
from .ideas import STORY_SEARCH_KEYS
from .idea_pad import FORMATS as IDEA_FORMATS, STATUSES as IDEA_STATUSES, WRITING_MODELS
from .store import SPEECH_PASS_STATES, Store
from .service import Service

STRING = {"type": "string"}
SPEECH_STATE = {"type": "string", "enum": ["", *SPEECH_PASS_STATES, "pending"]}
STORY_SEARCH_SCOPE = {"type": "object", "minProperties": 1, "additionalProperties": False,
                      "properties": {
                          **{k: STRING for k in STORY_SEARCH_KEYS},
                          "speech_only": {"type": "boolean"}, "scene_only": {"type": "boolean"},
                          "speech_state": SPEECH_STATE}}
ANALYSIS_OPTIONS = {"type": "object", "properties": {
    "language": STRING, "model": STRING, "detail": STRING, "fresh": {"type": "boolean"},
    "prefer_cloud": {"type": "boolean"}, "priority": {"type": "boolean"}, "interactive": {"type": "boolean"}}, "additionalProperties": False}
TOOLS = [
    ("catalog_storage_limit", "Choose a 2, 4, 8, 16 or 32 GiB temporary storage cap for new jobs, including larger cloud sources. Current jobs keep their existing allowance; lowering below active reservations is rejected. Keeps the 3 GiB host free-space reserve and automatic owned-source cleanup.", "storage_limit", {"gib":{"type":"integer","enum":[2,4,8,16,32]}}),
    ("catalog_search", "Search filenames, local scene descriptions, tags and transcripts. Set filename for an exact full-name filter and use provider or dates to distinguish duplicates. Optional bounds is south,west,north,east in decimal degrees and matches recorded coordinates only. Use capture_month (YYYY-MM or unknown) with status (ready, partial or metadata_only) to browse coverage. Page with limit (1–200) and offset. Set speech_only to search for a contiguous phrase only in transcripts, or omit the query to list items with transcript text. Set scene_only to require all terms in one scene caption, or omit the query to browse assets with captions. Set source_access to local for an existing linked file, or picker for a current recorded Google selection. Use speech_state to filter the recorded transcription outcome. Pending excludes still photos. Visual matches include a scene_key; timed speech matches include a speech_key. Directional matches also include sample_time and matched camera angles. Set person to a person ID (or several, comma-separated, for items with all of them) from people_list.", "search", {**{k: STRING for k in ("query", "provider", "kind", "status", "category", "since", "until", "bounds", "capture_month", "filename")},"limit":{"type":"integer","minimum":1,"maximum":200},"offset":{"type":"integer","minimum":0,"maximum":10000000},"source_access":{"type":"string","enum":["","local","picker"]},"speech_state":SPEECH_STATE,"speech_only":{"type":"boolean"},"scene_only":{"type":"boolean"},"person":STRING}),
    ("catalog_search_related", "Use an installed local model to suggest alternate search wording. Returns up to 24 indexed assets with matched_query and the same edit references as catalog_search. Use the same source, media, status, capture-month, date, GPS, source_access, speech_state and scene_only or speech_only filters.", "search_related", {**{k: STRING for k in ("provider", "kind", "status", "category", "since", "until", "bounds", "capture_month")}, "query":{"type":"string","minLength":1,"maxLength":500}, "model":{"type":"string","enum":["","maternion/spark-x2.6:4b-q6_k","phi4-mini:latest","qwen3:8b","gemma3:12b"]},"source_access":{"type":"string","enum":["","local","picker"]},"speech_state":SPEECH_STATE,"speech_only":{"type":"boolean"},"scene_only":{"type":"boolean"}}),
    ("catalog_asset", "Read one asset, scenes and transcript. Resolve a scene from search or a story draft using start/end for a range or sample_time for a 360 point. Include scene_key and the source part filename as part_filename when available. A null citation_match means it cannot be confirmed.", "asset", {"id": STRING, "start":{"type":"number"},"end":{"type":"number"},"sample_time":{"type":"number"},"scene_key":STRING,"part_filename":STRING}),
    ("catalog_stats", "Read exact inventory/analysis coverage and temporary storage use.", "stats", {}),
    ("catalog_compare_sources", "Compare exact file bytes observed during analysis for two distinct catalogue asset IDs. Returns same_bytes, different_bytes or unknown. Uses recorded single-file fingerprints only. Never downloads, merges or changes records.", "compare_sources", {"first":STRING,"second":STRING}),
    ("catalog_coverage", "Read per-source and paginated capture-month counts for known records, including ready, partial, metadata-only and transcripts with searchable words. Filter all counts by kind (photo, video or audio) and monthly rows by provider. Counts describe the local catalogue. Months with no matching records are omitted.", "coverage", {"provider":STRING,"kind":{"type":"string","enum":["","photo","video","audio"]},"limit":{"type":"integer","minimum":1,"maximum":100},"offset":{"type":"integer","minimum":0,"maximum":100000}}),
    ("catalog_resolve", "Get stable source IDs, filename, cloud/search link and a retrieval plan. Pass a current scene_key with optional part_filename, or a current speech_key, for exact source-file times for editing. Reports local availability and Google selection state and expiry. No bearer URLs.", "resolve", {"id": STRING, "scene_key": STRING, "part_filename": STRING, "speech_key": STRING}),
    ("catalog_retrieve", "Priority editing handoff. Reuse an existing full original read-only, download a selected Google Photos rendition, or stage a cloud recording through Insta360 Studio. Cloud retrieval requires an existing editing destination. Pass a current scene_key with optional part_filename, or a speech_key, to include the exact source path and time range in catalog_job. Timed retrieval verifies recorded source bytes when a checksum is available, otherwise it checks known indexed media properties. A mismatch blocks timed publication. With an editing destination, a mismatched linked Google Photos file can fall back to the selected cloud source after the same checks. Whole-file Google Photos retrieval can deliver a different rendition and reports different_bytes without changing the indexed source link. The entire source is retained for editing.", "retrieve", {"id": STRING, "destination": STRING, "scene_key": STRING, "part_filename": STRING, "speech_key": STRING}),
    ("catalog_job", "Read one job's state, error and completed retrieval result, including the full original path and related files for editor import. A failed job with recovered_by has a later successful attempt.", "job", {"id": {"type": "integer"}}),
    ("catalog_retry_speech", "Retry a failed or empty local transcript from the same indexed rendition without replacing scenes. Set replace=true to replace a completed transcript only after a successful pass with timed words. For multipart recordings it retries only failed parts and retains successful words. Full replacement is supported for single files.", "retry_speech", {"id": STRING, "language": STRING, "model": STRING, "prefer_cloud": {"type": "boolean"}, "replace": {"type": "boolean"}}),
    ("catalog_confirm_studio_download", "Confirm after Studio visibly reports completion. For a batch, individual=true confirms only this recording and lets its analysis start while peers download. The default confirms all remaining recordings in the batch. The worker validates exact files, stability and media before analysis.", "studio_confirm", {"id":{"type":"integer","minimum":1},"individual":{"type":"boolean"}}),
    ("catalog_cleanup_studio_download", "Clean an inactive job's owned temporary Studio source after its external transfer has finished or been cancelled in Studio. Never force-unmounts a busy volume or touches existing Studio downloads.", "studio_cleanup", {"id":{"type":"integer","minimum":1}}),
    ("catalog_analyze", "Queue local offline analysis. A UI or background worker must run. Existing originals open read-only; temporary downloads are deleted. Recognized 360 media adds six calibrated directions at sampled times. Use options.interactive=true for up to eight items ahead of ordinary priority analysis. Cloud-only priority Insta360 transfers, speech retries and editing retrieval retain precedence.", "enqueue", {"ids": {"type": "array", "items": STRING, "maxItems": 200}, "options": ANALYSIS_OPTIONS}),
    ("catalog_stage_studio_batch", "Queue 2–8 distinct cloud-only Insta360 items for one bounded Studio transfer and separate local analysis. Stages ahead of background analysis by default; set options.priority=false to wait. Read any running job for the exact list and shared destination. In Studio, select the first listed recording alone, then add the others. Cancel the Download dialog and reselect if its count differs from the batch length. Confirm each completed recording or confirm the whole batch. Stop failed transfers in Studio before cancelling their FrameAtlas jobs.", "studio_batch", {"ids": {"type": "array", "items": STRING, "minItems": 2, "maxItems": 8, "uniqueItems": True}, "options": ANALYSIS_OPTIONS}),
    ("catalog_jobs", "Read queue progress, including jobs waiting for a fresh Google Photos selection and unresolved_failures. Historical failure attempts remain in counts.failed.", "jobs", {}),
    ("catalog_cancel", "Cancel a queued or running local analysis job.", "cancel", {"id": {"type": "integer"}}),
    ("catalog_queue_control", "Pause or resume new analysis jobs. The current job finishes unless separately cancelled. The pause persists across restarts.", "queue_control", {"paused": {"type": "boolean"}}),
    ("catalog_ideas", "Generate story ideas with valid asset/time citations using an installed local Ollama model. Set asset_ids to restrict evidence to exact analysed files. Set search_scope to use Library filters and up to 18 matching files with scene captions. With an unrestricted search, expand_search can look up related phrases suggested by the same model.", "ideas", {"prompt": STRING, "model": STRING, "expand_search": {"type": "boolean"}, "search_scope":STORY_SEARCH_SCOPE,"asset_ids":{"type":"array","items":STRING,"minItems":1,"maxItems":18,"uniqueItems":True}}),
    ("catalog_metadata_coverage", "Read how many records have a capture date, a recorded place, a recorded camera, a filename device guess and analysis, per source, with notes on why fields are missing (Google strips positions; camera fields arrive with new Picker selections).", "metadata_coverage", {}),
    ("catalog_metadata_backfill", "Read positions from linked local video originals that were never checked, read-only. Nothing is downloaded.", "metadata_backfill", {}),
    ("idea_pad_list", "Read the Idea Pad: saved video ideas with title, logline, notes, status (idea, planned, shooting, editing, published, dropped), format, tags, origin (manual or generated) and basis (the moment, why, linked asset_ids).", "idea_pad", {"status": STRING, "origin": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 500}, "offset": {"type": "integer", "minimum": 0}}),
    ("idea_pad_add", "Save a video idea to the Idea Pad. Optional asset_ids link catalogue items to it.", "idea_pad_add", {"title": STRING, "logline": STRING, "notes": STRING, "format": {"type": "string", "enum": list(IDEA_FORMATS)}, "status": {"type": "string", "enum": list(IDEA_STATUSES)}, "tags": {"type": "array", "items": STRING, "maxItems": 12}, "asset_ids": {"type": "array", "items": STRING, "maxItems": 24}}),
    ("idea_pad_update", "Change an Idea Pad entry: any of title, logline, notes, status, format, tags, asset_ids.", "idea_pad_update", {"id": {"type": "integer", "minimum": 1}, "title": STRING, "logline": STRING, "notes": STRING, "format": {"type": "string", "enum": list(IDEA_FORMATS)}, "status": {"type": "string", "enum": list(IDEA_STATUSES)}, "tags": {"type": "array", "items": STRING, "maxItems": 12}, "asset_ids": {"type": "array", "items": STRING, "maxItems": 24}}),
    ("idea_pad_delete", "Delete an Idea Pad entry.", "idea_pad_delete", {"id": {"type": "integer", "minimum": 1}}),
    ("idea_pad_moments", "List notable moments in the catalogue's metadata: days or runs of days with unusual amounts of footage, 360 recordings or several videos, with counts, devices, places, tags, an upcoming anniversary and sample analysed items. Dates are YYYY-MM-DD.", "idea_pad_moments", {"since": STRING, "until": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}),
    ("idea_pad_generate", "Generate 1–10 video ideas from notable moments with an installed local Ollama model (the best installed one when model is empty) and save them to the Idea Pad. Each idea names its moment and links sample assets. Moments that already have a generated idea are skipped unless include_used. Optional focus steers the choice (for example 'beach' or '360').", "idea_pad_generate", {"model": {"type": "string", "enum": ["", *WRITING_MODELS]}, "count": {"type": "integer", "minimum": 1, "maximum": 10}, "since": STRING, "until": STRING, "focus": STRING, "include_used": {"type": "boolean"}}),
    ("people_status", "Read the face index: previews scanned, faces found and grouped, people and named people, and the running scan's progress.", "people_status", {}),
    ("people_scan", "Start the local face scan of stored previews (YuNet + AuraFace on the GPU), then group faces into people. Runs in the background; read people_status.", "people_scan", {}),
    ("people_regroup", "Regroup every face into people, keeping names, confirmed faces and removals.", "people_regroup", {}),
    ("people_list", "List people found in the media: id, name, how many faces and items, first and last seen, cover crop. Hidden people (passers-by) only with include_hidden; named_only for named people.", "people", {"include_hidden": {"type": "boolean"}, "named_only": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}, "offset": {"type": "integer", "minimum": 0}}),
    ("person_get", "Read one person and their faces (with item IDs, filenames, capture times). Use catalog_search with person=<id> for their media.", "person", {"id": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "offset": {"type": "integer", "minimum": 0}}),
    ("person_update", "Name a person, hide or show them, or keep notes about them.", "person_update", {"id": {"type": "integer", "minimum": 1}, "name": STRING, "hidden": {"type": "boolean"}, "notes": STRING}),
    ("person_merge", "Merge two people who are the same person: every face of other moves to into.", "person_merge", {"into": {"type": "integer", "minimum": 1}, "other": {"type": "integer", "minimum": 1}}),
    ("face_assign", "Correct one face: give person to put it with that person, omit person to take it away from its person (it will not go back), or new_person to start a new person with it.", "face_assign", {"face": {"type": "integer", "minimum": 1}, "person": {"type": "integer", "minimum": 1}, "new_person": {"type": "boolean"}}),
    ("catalog_annotate", "Correct generated summary or tags; changes are local to the catalogue.", "annotate", {"id": STRING, "summary": STRING, "tags": {"type": "array", "items": STRING}}),
    ("catalog_correct_scene", "Correct a scene caption while preserving model output. Send the scene_key from catalog_asset with the ordinal to reject a stale edit if reanalysis changed the scene.", "correct_scene", {"id": STRING, "ordinal": {"type": "integer"}, "caption": STRING, "scene_key": STRING}),
    ("catalog_import_native", "Import metadata only from existing FilmCraft Photos selections and local Studio inventory. Does not scan either entire cloud.", "import_native", {}),
    ("catalog_studio_ocr", "Read a local PNG/JPEG screenshot of signed-in Studio Cloud Media detailed list. Default qwen uses local Qwen2.5-VL; optional apple_vision uses the installed local CPU helper. Return candidate rows for review; no media download or database mutation. Verify Cloud Media is selected and complete names/Date Created are visible.", "studio_ocr", {"path": STRING, "engine":{"type":"string","enum":["qwen","apple_vision"]}}),
    ("catalog_import_studio_inventory", "Import reviewed visible Studio Cloud rows from catalog_studio_ocr, preserving evidence and marking OCR unverified. No footage download and no whole-cloud coverage claim.", "studio_import", {"cloud_media_selected": {"type": "boolean"}, "list_view": {"type": "boolean"}, "records": {"type": "array", "items": {"type": "object"}}, "evidence_sha256": STRING, "model":STRING}),
    ("catalog_import_studio_captures", "Atomically import a reviewed batch of up to 150 local Studio OCR previews. Overlapping filenames are deduplicated, each retains its screenshot hash and OCR provenance. Visible inventory only; no footage download or claim of full-cloud coverage.", "studio_import_batch", {"captures":{"type":"array","maxItems":150,"items":{"type":"object"}}}),
    ("catalog_import_manifest", "Import an explicit cloud inventory manifest. Original files are never copied.", "import_manifest", {"label": STRING, "items": {"type": "array", "items": {"type": "object"}}}),
    ("catalog_import_takeout", "Import JSON sidecars from a local Google Takeout archive, a sidecar file, or a folder containing up to 32 archives. Folder imports report successes and errors per archive. No original media are extracted.", "import_takeout", {"path": STRING}),
    ("catalog_link_takeout_location", "After confirming two records refer to the same media, copy recorded Takeout GPS to a Google Picker catalogue item as a removable link. Supply catalogue IDs. An empty takeout_id removes the link. Preserves source records, Picker access, existing analysis and queued jobs.", "link_takeout_location", {"picker_id": STRING, "takeout_id": STRING}),
    ("catalog_picker_create", "Create a Google Photos user-selection session via FilmCraft secure credentials. Open returned Picker URL for user selection.", "picker_create", {}),
    ("catalog_picker_import", "Import selected metadata and optionally queue local analysis for previously unqueued photos and videos.", "picker_import", {"session": STRING, "queue_analysis": {"type": "boolean"}}),
    ("catalog_picker_sessions", "Read Google selection receipts, expiry, linked assets, cloud-dependent queued jobs and cumulative minimum pace by deadline. Compare recent Picker completions with the required hourly pace for each media type. Pace describes the queue, with no completion guarantee. A batch can close only after dependent jobs finish or move to another selection.", "picker_sessions", {}),
    ("catalog_picker_queued_items", "Page through the exact filenames, capture timestamps, and catalogue IDs of distinct queued sources that still depend on one Google Picker selection. Includes expiry and whether reselection is already required. Read-only.", "picker_queued_items", {"session": STRING, "limit":{"type":"integer","minimum":1,"maximum":100}, "offset":{"type":"integer","minimum":0,"maximum":2000}}),
    ("catalog_picker_reselection", "Page through distinct Google Photos sources whose queued jobs are waiting for Picker access, including missing or expired selections. Returns filenames, capture timestamps, reasons and queued-job counts. Read-only.", "picker_reselection", {"limit":{"type":"integer","minimum":1,"maximum":100}, "offset":{"type":"integer","minimum":0,"maximum":10000000}}),
    ("catalog_picker_close", "Close a finished Picker session after its analysis jobs finish.", "picker_delete", {"session": STRING}),
    ("catalog_attach_local", "Explicitly associate an existing original with an asset, read-only; never copy or delete that file.", "attach_local", {"id": STRING, "path": STRING}),
    ("catalog_models", "Report installed local scene/speech/writing models without downloading anything.", "models", {}),
]


def handle(service, message):
    rid = message.get("id")
    method = message.get("method")
    if rid is None:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "frameatlas", "version": "0.1.0"}, "instructions": "All inference is local. Start the local app worker to process queued jobs. Inventory coverage is partial; consult catalog_stats. Model descriptions require verification."}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "additionalProperties": False}} for n, d, a, p in TOOLS]}}
    if method == "tools/call":
        try:
            params = message.get("params", {})
            name = params.get("name")
            tool = next((t for t in TOOLS if t[0] == name), None)
            if not tool:
                raise ValueError("Unknown tool")
            args = params.get("arguments", {})
            if not isinstance(args, dict) or set(args) - set(tool[3]):
                raise ValueError("Invalid arguments")
            result = service.dispatch(tool[2], args)
            content = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
        except Exception as exc:
            content = {"content": [{"type": "text", "text": str(exc)[:1500]}], "isError": True}
        return {"jsonrpc": "2.0", "id": rid, "result": content}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}


def main():
    service = Service(Store())
    while True:
        line = sys.stdin.buffer.readline(8 * 1024 ** 2 + 1)
        if not line:
            break
        try:
            if len(line) > 8 * 1024 ** 2:
                raise ValueError("Message too large")
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("Expected JSON-RPC object")
            response = handle(service, message)
        except (ValueError, TypeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC input"}}
        if response:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
