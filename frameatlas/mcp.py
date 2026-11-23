"""MCP over stdio. Uses the same service and database as the human UI."""
import json
import sys
import threading
from . import contract
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
    ("catalog_search", "Search filenames, local scene descriptions, tags and transcripts. Set filename for an exact full-name filter and use provider or dates to distinguish duplicates. Optional bounds is south,west,north,east in decimal degrees and matches recorded coordinates only. Use capture_month (YYYY-MM or unknown) with status (ready, partial or metadata_only) to browse coverage. Page with limit (1–200) and offset. Set speech_only to search for a contiguous phrase only in transcripts, or omit the query to list items with transcript text. Set scene_only to require all terms in one scene caption, or omit the query to browse assets with captions. Set source_access to local for an existing linked file, or picker for a current recorded Google selection. Use speech_state to filter the recorded transcription outcome. Pending excludes still photos. Visual matches include a scene_key; timed speech matches include a speech_key. Directional matches also include sample_time and matched camera angles. Set person to a person ID (or several, comma-separated, for items with all of them) from people_list. Set place to a place ID (or several, comma-separated, for items at any of them) from places_list; bounds also match items by their imported place's position. Set used to unused for footage no recorded video has used yet (footage_record), or used for footage already in one.", "search", {**{k: STRING for k in ("query", "provider", "kind", "status", "category", "since", "until", "bounds", "capture_month", "filename")},"limit":{"type":"integer","minimum":1,"maximum":200},"offset":{"type":"integer","minimum":0,"maximum":10000000},"source_access":{"type":"string","enum":["","local","picker"]},"speech_state":SPEECH_STATE,"speech_only":{"type":"boolean"},"scene_only":{"type":"boolean"},"person":STRING,"place":STRING,"used":{"type":"string","enum":["","used","unused"]}}),
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
    ("catalog_ideas", "Generate story ideas with valid asset/time citations using an installed local Ollama model. Set asset_ids to restrict evidence to exact analysed files. Set search_scope to use Library filters and up to 18 matching files with scene captions. With an unrestricted search, expand_search can look up related phrases suggested by the same model. Every idea is also saved to the Idea Pad (origin story, with its edit sequence in basis.sequence); saved_ids lists them.", "ideas", {"prompt": STRING, "model": STRING, "expand_search": {"type": "boolean"}, "search_scope":STORY_SEARCH_SCOPE,"asset_ids":{"type":"array","items":STRING,"minItems":1,"maxItems":18,"uniqueItems":True}}),
    ("catalog_metadata_coverage", "Read how many records have a capture date, a recorded place, a recorded camera, a filename device guess and analysis, per source, with notes on why fields are missing (Google strips positions; camera fields arrive with new Picker selections).", "metadata_coverage", {}),
    ("catalog_metadata_backfill", "Read positions from linked local video originals that were never checked, read-only. Nothing is downloaded.", "metadata_backfill", {}),
    ("idea_pad_list", "Read the Idea Pad: saved video ideas with title, logline, notes, status (idea, planned, shooting, editing, published, dropped), format, tags, origin (manual, generated from a moment, or story from footage) and basis (the moment, why, linked asset_ids, and sequence: cited shots in editing order with pickups).", "idea_pad", {"status": STRING, "origin": {"type": "string", "enum": ["", "manual", "generated", "story"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}, "offset": {"type": "integer", "minimum": 0}}),
    ("idea_pad_add", "Save a video idea to the Idea Pad. Optional asset_ids link catalogue items to it.", "idea_pad_add", {"title": STRING, "logline": STRING, "notes": STRING, "format": {"type": "string", "enum": list(IDEA_FORMATS)}, "status": {"type": "string", "enum": list(IDEA_STATUSES)}, "tags": {"type": "array", "items": STRING, "maxItems": 12}, "asset_ids": {"type": "array", "items": STRING, "maxItems": 24}}),
    ("idea_pad_update", "Change an Idea Pad entry: any of title, logline, notes, status, format, tags, asset_ids.", "idea_pad_update", {"id": {"type": "integer", "minimum": 1}, "title": STRING, "logline": STRING, "notes": STRING, "format": {"type": "string", "enum": list(IDEA_FORMATS)}, "status": {"type": "string", "enum": list(IDEA_STATUSES)}, "tags": {"type": "array", "items": STRING, "maxItems": 12}, "asset_ids": {"type": "array", "items": STRING, "maxItems": 24}}),
    ("idea_pad_delete", "Delete an Idea Pad entry.", "idea_pad_delete", {"id": {"type": "integer", "minimum": 1}}),
    ("idea_pad_moments", "List notable moments in the catalogue's metadata, each with a kind: trip (days away from home, from imported places, with home, area, distance and countries), day_out (one day out near home), return (an area visited again in other years) occasion (a festival, holiday or birthday from the owner's calendar, on a day with footage) or busy (days or runs of days with unusual amounts of footage, 360 recordings or several videos); with counts, devices, places, tags, an upcoming anniversary and sample analysed items. Dates are YYYY-MM-DD.", "idea_pad_moments", {"since": STRING, "until": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "kind": {"type": "string", "enum": ["", "busy", "trip", "day_out", "return", "occasion"]}}),
    ("idea_pad_develop", "Build or rebuild an idea's edit sequence from indexed footage with an installed local model: its linked analysed items, else the days of its moment, else a catalogue search for its title and line. Stores cited shots (asset_id, times, reason), pickups and other takes in basis.sequence.", "idea_pad_develop", {"id": {"type": "integer", "minimum": 1}, "model": {"type": "string", "enum": ["", *WRITING_MODELS]}}),
    ("idea_pad_generate", "Generate 1–10 video ideas from notable moments with an installed local Ollama model (the best installed one when model is empty) and save them to the Idea Pad. Each idea names its moment and links sample assets. Moments that already have a generated idea are skipped unless include_used. Optional focus steers the choice (for example 'beach' or '360'); kind keeps to one kind of moment (trip, day_out, return or busy).", "idea_pad_generate", {"kind": {"type": "string", "enum": ["", "busy", "trip", "day_out", "return", "occasion"]}, "model": {"type": "string", "enum": ["", *WRITING_MODELS]}, "count": {"type": "integer", "minimum": 1, "maximum": 10}, "since": STRING, "until": STRING, "focus": STRING, "include_used": {"type": "boolean"}}),
    ("people_status", "Read the face index: previews scanned, faces found and grouped, people and named people, and the running scan's progress.", "people_status", {}),
    ("people_scan", "Start the local face scan of stored previews (YuNet + AuraFace on the GPU), then group faces into people. Runs in the background; read people_status.", "people_scan", {}),
    ("people_regroup", "Regroup every face into people, keeping names, confirmed faces and removals.", "people_regroup", {}),
    ("people_list", "List people found in the media: id, name, how many faces and items, first and last seen, cover crop. Hidden people (passers-by) only with include_hidden; named_only for named people; query searches names (or #id).", "people", {"include_hidden": {"type": "boolean"}, "named_only": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}, "offset": {"type": "integer", "minimum": 0}, "query": STRING}),
    ("person_get", "Read one person and their faces (with item IDs, filenames, capture times). Use catalog_search with person=<id> for their media.", "person", {"id": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "offset": {"type": "integer", "minimum": 0}}),
    ("person_update", "Name a person, hide or show them, or keep notes about them.", "person_update", {"id": {"type": "integer", "minimum": 1}, "name": STRING, "hidden": {"type": "boolean"}, "notes": STRING}),
    ("person_merge", "Merge two people who are the same person: every face of other moves to into.", "person_merge", {"into": {"type": "integer", "minimum": 1}, "other": {"type": "integer", "minimum": 1}}),
    ("face_assign", "Correct one face: give person to put it with that person, omit person to take it away from its person (it will not go back), or new_person to start a new person with it.", "face_assign", {"face": {"type": "integer", "minimum": 1}, "person": {"type": "integer", "minimum": 1}, "new_person": {"type": "boolean"}}),
    ("faces_link", "Link faces to a person (confirmed: regrouping keeps them there), or with new_person (and an optional name) to a new person. Faces moved away from another person never go back to them.", "faces_link", {"faces": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 500}, "person": {"type": "integer", "minimum": 1}, "new_person": {"type": "boolean"}, "name": STRING}),
    ("faces_unlink", "Take faces away from their people; regrouping never puts them back with those people.", "faces_unlink", {"faces": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 500}}),
    ("faces_unsorted", "List faces with no person (passers-by and faces grouping was unsure of), clearest first, with item IDs and crops.", "faces_unsorted", {"limit": {"type": "integer", "minimum": 1, "maximum": 300}, "offset": {"type": "integer", "minimum": 0}}),
    ("people_suggest", "The people some faces most look like (likeness 0-1), to link them to the right person.", "people_suggest", {"faces": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 64}, "limit": {"type": "integer", "minimum": 1, "maximum": 30}}),
    ("people_autosort", "Sort faces on their own in the background (read people_status): merge people whose faces are clearly one person (likeness >= 0.8), link unsorted faces that clearly belong to someone, and queue less certain merges, links, moves and unlinks for review. Without apply nothing changes; everything is proposed. Never touches faces the owner confirmed or pairs already turned down. To sort as an agent: run it, read people_review, look at each proposal with people_compare (it returns the face images), then people_review_decide apply or dismiss.", "people_autosort", {"apply": {"type": "boolean"}}),
    ("people_review", "Read the sorting log: proposed changes waiting for a decision (default) or applied ones (which can be undone), with likeness, face and person crops. actor is auto, agent or google; action is link, move, unlink, merge or name.", "people_review", {"status": {"type": "string", "enum": ["proposed", "applied", "undone", "dismissed", ""]}, "actor": {"type": "string", "enum": ["", "auto", "agent", "google"]}, "action": {"type": "string", "enum": ["", "link", "move", "unlink", "merge", "name"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "offset": {"type": "integer", "minimum": 0}}),
    ("people_review_decide", "Decide on one sorting log entry: apply a proposal, dismiss it (it is not proposed again), or undo an applied change. For a mixed name with another candidate (other_id), apply_other gives the name to the other person instead.", "people_review_decide", {"id": {"type": "integer", "minimum": 1}, "decision": {"type": "string", "enum": ["apply", "apply_other", "dismiss", "undo"]}}),
    ("people_compare", "Look at faces: returns a person's clearest face crops as images next to another person's (with their fingerprint likeness), or next to given faces. Use it to judge merges and links by eye.", "people_compare", {"person": {"type": "integer", "minimum": 1}, "other": {"type": "integer", "minimum": 1}, "faces": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 12}, "count": {"type": "integer", "minimum": 1, "maximum": 12}}),
    ("places_import", "Label catalogue items with another library's places (Google Photos' Places page, read in the owner's browser). Give each place its key (Google's place id), name, and from Google Maps its address, latitude and longitude (or plus_code when Maps gives only that), with days: for each day there the local capture time (YYYY-MM-DDTHH:MM:SS, no time zone) of the first and last item and how many items (or packed, the text that scripts/google_photos_places.js returns). Each place's time zone is found by matching those items to the catalogue; items taken between a day's first and last item are labelled. Known days are replaced, others kept, so a place can be read in parts.", "places_import", {"source": {"type": "string", "enum": ["google_photos"]}, "places": {"type": "array", "maxItems": 2000, "items": {"type": "object"}}}),
    ("places_list", "List imported places: id, name, address, position, days and items there, first and last day, and how many catalogue items carry the place. query searches names and addresses.", "places", {"query": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 2000}, "offset": {"type": "integer", "minimum": 0}}),
    ("places_trips", "Trips away from home, days out near it and areas visited again in other years, read from imported places: dates, home, main area, distance in km, countries (or regions) and places. Home is the area seen on most days around each date, so it moves when the owner moves. kind is trip, day_out or return.", "places_trips", {"since": STRING, "until": STRING, "kind": {"type": "string", "enum": ["", "trip", "day_out", "return"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}),
    ("people_import_names", "Name people after another library's named people (for example Google Photos' People & pets). Give each name with its items' local capture times (items [{kind: photo or video, local: YYYY-MM-DDTHH:MM:SS, no time zone}], or packed: the people script's text). Every read is kept, so call it with only the new names, or with no people to weigh again all the names read so far. Items are matched to the catalogue (all items at one second, as bursts show the same people), and names and people are paired together, a group photo counting for less: a name goes to the person seen far more in its items than elsewhere, two names always seen together go to two people, and anything unclear is only proposed with the other candidate (People › To review). Owner names are never replaced; split groups of one person are merged when their faces agree. Without apply, everything is proposed. replace forgets earlier reads first.", "people_import_names", {"source": STRING, "people": {"type": "array", "maxItems": 2000, "items": {"type": "object"}}, "apply": {"type": "boolean"}, "replace": {"type": "boolean"}}),
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
    ("catalog_idea_history", "Read the last 12 story-idea runs (catalog_ideas): prompt, result and when.", "idea_history", {}),
    ("catalog_merge", "Merge two inventory records of the same provider that the owner confirmed are one item: keep gains the other's metadata and links, and duplicate is deleted. Only for records never analysed or queued. Cannot be undone.", "merge", {"keep": STRING, "duplicate": STRING}),
    ("places_forget", "Delete every imported place and its days and item labels for a source. Items and analysis stay. Ask the owner first, then call with confirm: true. Cannot be undone; re-import the places to restore them.", "places_forget", {"source": {"type": "string", "enum": ["google_photos"]}, "confirm": {"type": "boolean"}}),
    ("people_forget", "Delete every face, person, name and correction (the face crops too). Items and analysis stay. Ask the owner first, then call with confirm: true. Cannot be undone; a new scan finds faces again but names are lost.", "people_forget", {"confirm": {"type": "boolean"}}),
    ("catalog_batch", "Run up to 25 read and write tool calls in order, in one round trip. Stops at the first error unless stop_on_error is false. Destructive tools and tools that reach Google or Insta360 are not allowed here; call them on their own. Calls are not one transaction: earlier changes stay when a later call fails. Face images are not returned; call people_compare on its own to see them.", None, {"calls": {"type": "array", "minItems": 1, "maxItems": 25, "items": {"type": "object", "required": ["tool"], "additionalProperties": False, "properties": {"tool": STRING, "arguments": {"type": "object"}}}}, "stop_on_error": {"type": "boolean"}}),
    ("catalog_tasks", "List recent background tasks (from background: true, and face scans, regroups and sorts): tool, state (running, done, failed, interrupted), when, and how long.", "tasks", {"limit": {"type": "integer", "minimum": 1, "maximum": 200}, "state": {"type": "string", "enum": ["", "running", "done", "failed", "interrupted"]}}),
    ("catalog_task", "Read one background task with its result or error.", "task", {"id": {"type": "integer", "minimum": 1}}),
    ("catalog_wait", "Wait up to timeout seconds (default 30, at most 60) for a background task to finish (task) or an analysis job to change state (job, from catalog_analyze or catalog_jobs), then return it. finished_waiting says whether it got there; if not, call again. Sends progress notifications when the call has a progressToken.", "wait", {"task": {"type": "integer", "minimum": 1}, "job": {"type": "integer", "minimum": 1}, "timeout": {"type": "number", "minimum": 1, "maximum": 60}}),
    ("agent_activity", "Read what agents did: each logged call with when, which client, tool, risk tier, result or error code, and whether it can be undone. Also the owner's permissions and the calls waiting for the owner's approval. Reads are left out unless include_reads.", "activity", {"include_reads": {"type": "boolean"}, "tool": STRING, "client": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 500}, "offset": {"type": "integer", "minimum": 0}}),
    ("agent_permissions", "Read the owner's permissions for agents: for write, destructive and outside tools, allow, confirm (repeat the call with the token it returns, after telling the owner), owner (the owner approves it on the Activity page) or deny. Only the owner can change them, in the browser UI.", "permissions", {}),
    ("ui_show", "Show the owner something in the open FrameAtlas page: view library (with query, person, place, since, until, kind, provider), item (id, optional time in seconds to open at that scene), person (id), place (id, or several comma-separated), trip (since and until), idea (id), or the people, ideas, queue, sources or activity page. message is shown with it. Returns a link the owner can open when no page is open (shown false).", "ui_show", {"view": {"type": "string", "enum": ["library", "item", "person", "place", "trip", "idea", "people", "ideas", "queue", "sources", "activity"]}, "id": {"type": "string", "maxLength": 200}, "time": {"type": "number", "minimum": 0}, "query": STRING, "person": STRING, "place": STRING, "since": STRING, "until": STRING, "kind": {"type": "string", "enum": ["", "photo", "video", "audio"]}, "provider": STRING, "message": {"type": "string", "maxLength": 200}}),
    ("ui_select", "Select media in the open page's Library, so the owner can act on them (analyse, build a story) or check them: replace the selection, add to it, or clear it. Up to 200 catalogue IDs.", "ui_select", {"ids": {"type": "array", "items": STRING, "maxItems": 200, "uniqueItems": True}, "mode": {"type": "string", "enum": ["replace", "add", "clear"]}, "message": {"type": "string", "maxLength": 200}}),
    ("ui_state", "Read what the owner is looking at in the open FrameAtlas page: the page, the Library's query and filters, the results on screen, the media they selected (IDs and filenames), and the open item or person. page_open is false when no page is open.", "ui_state", {}),
    ("catalog_health", "What needs attention, most urgent first: failed jobs grouped by cause with the fix, Google selections about to expire with jobs waiting, items never analysed, a paused queue, Google page reads still to do, people-sorting proposals and unlinked faces, and agent calls waiting for the owner. Each item names the tool to look further. Read it at the start of a session.", "health", {}),
    ("google_read_plan", "The resumable plan for reading Google Photos' Places (or People & pets) in the owner's browser: how many names are pending, done, incomplete or failed, searches in the last hour against the budget, when Google's throttle should lift, and the next step. Google throttles after roughly 25–55 searches for about an hour.", "google_read_plan", {"kind": {"type": "string", "enum": ["places", "people"]}}),
    ("google_read_register", "Register the names from Google Photos' list page (Places: step 1's [[name, key]] as entries [{name, key}]; People: [{name}]). Known names keep their state, so registering again only adds new ones.", "google_read_register", {"kind": {"type": "string", "enum": ["places", "people"]}, "entries": {"type": "array", "minItems": 1, "maxItems": 5000, "items": {"type": "object", "required": ["name"], "additionalProperties": False, "properties": {"name": STRING, "key": STRING}}}}),
    ("google_read_next", "The next names to read (pending first, then ones that stopped early), up to count and within this hour's search budget. Returns no names and wait_until while Google is likely throttling.", "google_read_next", {"kind": {"type": "string", "enum": ["places", "people"]}, "count": {"type": "integer", "minimum": 1, "maximum": 10}}),
    ("google_read_script", "The browser script to run for a step of reading Google Photos' Places (kind places: 1 lists the places with their keys, 2 finds each place's position on Google Maps, 3 reads one place's days) or People & pets (kind people: 1 lists the named people, 3 reads one person's photos). Run it in the owner's signed-in browser and pass its result back with google_read_register or google_read_result.", "google_read_script", {"kind": {"type": "string", "enum": ["places", "people"]}, "step": {"type": "integer", "enum": [1, 2, 3]}}),
    ("google_read_result", "Hand back one read: for a place, step 3's name, key, items, complete and packed (plus address, latitude, longitude or plus_code from step 2 when known); for a person, step 3's name, items, complete and packed (or people_items [{kind, local}]). A complete read is imported at once (places_import, or people_import_names as proposals over every person read so far). An incomplete read, or outcome no_results, is kept to read again and counts towards Google's throttle.", "google_read_result", {"kind": {"type": "string", "enum": ["places", "people"]}, "name": STRING, "key": STRING, "items": {"type": "integer", "minimum": 0}, "complete": {"type": "boolean"}, "packed": {"type": "string", "maxLength": 2000000}, "address": STRING, "latitude": {"type": "number"}, "longitude": {"type": "number"}, "plus_code": STRING, "people_items": {"type": "array", "maxItems": 20000, "items": {"type": "object"}}, "outcome": {"type": "string", "enum": ["read", "no_results", "not_found"]}}),
    ("google_read_reset", "Mark names to read again (or every name of that kind when names is omitted).", "google_read_reset", {"kind": {"type": "string", "enum": ["places", "people"]}, "names": {"type": "array", "items": STRING, "maxItems": 5000}}),
    ("supervisor_queue", "The questions the local models were unsure of, one by one, for you (when you can see images) or the owner to settle: whether two people are the same, whether a face belongs to someone, whether a Google name fits. Each item has a plain question, its likeness, the answers it takes (apply or dismiss) and face images labelled with its id. Look at the images before deciding; dismiss anything unclear. kind keeps to one kind of question.", "supervisor_queue", {"kind": {"type": "string", "enum": ["", "merge", "link", "move", "unlink", "name"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}, "offset": {"type": "integer", "minimum": 0}}),
    ("supervisor_decide", "Answer one supervisor_queue item with one of its answers: apply, dismiss, or for a name with two candidates apply_other (the name goes to the other person). A dismissed question is not asked again; an applied one can be undone (agent_undo or People › To review).", "supervisor_decide", {"id": {"type": "string", "maxLength": 40}, "decision": {"type": "string", "enum": ["apply", "apply_other", "dismiss"]}}),
    ("footage_record", "Remember which catalogue items a finished video used, so search can find unused footage (catalog_search used: unused), an item's detail says where it was used, and the idea keeps its links. Give the video's title, its shots (each asset_id, or a local path, or a filename with captured, plus start and end seconds when known), optionally the project file, the idea it came from, and where it was published [{where: youtube, youtube_short, instagram, google_photos, blog or other, url, date}]. Recording the same project again replaces its shots; with published and an idea, the idea moves to published.", "footage_record", {"title": {"type": "string", "maxLength": 200}, "shots": {"type": "array", "minItems": 1, "maxItems": 2000, "items": {"type": "object"}}, "project": {"type": "string", "maxLength": 1000}, "idea_id": {"type": "integer", "minimum": 1}, "published": {"type": "array", "maxItems": 20, "items": {"type": "object"}}}),
    ("footage_videos", "List the finished videos FrameAtlas knows about: title, project, idea, how many catalogue items they used, and where they were published.", "footage_videos", {"limit": {"type": "integer", "minimum": 1, "maximum": 200}, "offset": {"type": "integer", "minimum": 0}}),
    ("footage_forget", "Forget one recorded video and which footage it used (the footage itself stays).", "footage_forget", {"id": {"type": "integer", "minimum": 1}}),
    ("idea_to_autocut", "Turn an idea's edit sequence into an AutoCut project: every cited shot, in order, with its in and out points (a 360 sample point becomes a 3-second shot), on a 9:16 sequence for shorts and reels or 16:9 otherwise (or aspect). Writes <folder>/<title>.edit-plan.json and, unless build is false, assembles <folder>/<title>.fcproj with filmcraft-cli. The footage is remembered as used and the idea moves to editing. Nothing is downloaded: shots whose originals are not on this Mac are listed in missing; fetch them with catalog_retrieve, or pass paths {asset_id: local file}.", "idea_to_autocut", {"id": {"type": "integer", "minimum": 1}, "folder": {"type": "string", "maxLength": 1000}, "aspect": {"type": "string", "enum": ["", "9:16", "16:9", "1:1", "4:5"]}, "paths": {"type": "object"}, "build": {"type": "boolean"}, "name": {"type": "string", "maxLength": 120}}),
    ("footage_import_project", "Remember which catalogue items a finished AutoCut project (.fcproj) used, read with filmcraft-cli, so they show as used (catalog_search used) and in each item's detail. Optional title and idea_id.", "footage_import_project", {"project": {"type": "string", "maxLength": 1000}, "title": {"type": "string", "maxLength": 200}, "idea_id": {"type": "integer", "minimum": 1}}),
    ("places_tidy", "Give every item that falls inside two places' windows on the same day only the place with the narrower window (the more specific stop); equally narrow places both stay. Imports do this on their own; run it once for places imported before.", "places_tidy", {}),
    ("person_details", "Say how a person is related to the owner (relation, such as 'our son' or 'my wife') and when they were born (born: a year or a date). Idea writing uses the relation exactly as given, and a born date adds their birthdays to the calendar's moments.", "person_details", {"id": {"type": "integer", "minimum": 1}, "relation": {"type": "string", "maxLength": 60}, "born": {"type": "string", "maxLength": 10}}),
    ("person_timeline", "A person's years in the media: items and videos each year (with their age when born is known), a few analysed examples, and who they appear with most. Use catalog_search with person set to two ids for items with both.", "person_timeline", {"id": {"type": "integer", "minimum": 1}}),
    ("calendar", "Read the owner's occasions (festivals, holidays and anniversaries) that become moments for video ideas, alongside named people's birthdays.", "calendar", {}),
    ("calendar_set", "Replace the owner's occasions: each {name, month, day} for a fixed date every year, or {name, dates: [YYYY-MM-DD...]} for a festival whose date moves (Onam, Vishu, Eid, Diwali), listed year by year. Only add dates you are sure of or the owner gave.", "calendar_set", {"occasions": {"type": "array", "maxItems": 200, "items": {"type": "object"}}}),
    ("catalog_best_shots", "The best-looking shots for a trip or day (since, until), a person, a place, a search (query) or any catalog_search filter, from the picture quality measured for every analysed scene: sharpness, exposure and steadiness in one score (0–1). Spread out: at most per_item shots from one item (default 2) and per_day from one day (default 6). Each shot has its item, scene time range, caption and scores; set used to unused for fresh footage. Scores say how good the picture is, not what it shows.", "best_shots", {**{k: STRING for k in ("query", "provider", "kind", "since", "until", "person", "place", "category", "bounds", "capture_month")}, "used": {"type": "string", "enum": ["", "used", "unused"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "per_item": {"type": "integer", "minimum": 1, "maximum": 10}, "per_day": {"type": "integer", "minimum": 1, "maximum": 50}, "min_score": {"type": "number", "minimum": 0, "maximum": 1}}),
    ("catalog_search_meaning", "Search by meaning: items whose previews look most like the words (SigLIP 2 on this Mac), even when no caption or transcript uses them, best first with cosine and match (0–1). Narrow with any catalog_search filter. Needs semantic_build first; it compares pictures, not captions or speech, so use catalog_search for spoken words and names.", "search_meaning", {"query": {"type": "string", "minLength": 1, "maxLength": 300}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, **{**{k: STRING for k in ("provider", "kind", "status", "category", "since", "until", "person", "place", "capture_month", "bounds")}, "used": {"type": "string", "enum": ["", "used", "unused"]}}}),
    ("semantic_status", "How far search by meaning has got: previews with vectors, previews left, failures, the model, and whether the vector file matches its row list.", "semantic_status", {}),
    ("semantic_build", "Make vectors for up to limit stored previews that have none yet (default 5000), on the GPU through FilmCraft's SigLIP 2; nothing is downloaded. Run it again until left is 0; use background: true for big runs.", "semantic_build", {"limit": {"type": "integer", "minimum": 1, "maximum": 100000}}),
    ("semantic_reset", "Forget every vector for search by meaning (the previews stay), so semantic_build can make them again, for example after a model change.", "semantic_reset", {}),
    ("agent_undo", "Undo a logged change by its agent_activity id when can_undo is true: idea added, changed or deleted, person renamed, hidden or noted, summary or tags corrected, queue paused, storage limit, or an applied sorting proposal.", "activity_undo", {"id": {"type": "integer", "minimum": 1}}),
]
VERSION = "0.3.0"
INSTRUCTIONS = (
    "FrameAtlas is a local catalogue of the owner's photos and videos (scene descriptions, transcripts, people, "
    "places, trips, video ideas). All media inference is local. Each tool's annotations give its risk: readOnlyHint "
    "tools change nothing; destructiveHint tools delete or merge (ask the owner first); openWorldHint tools reach "
    "Google Photos or Insta360 or download media. Errors come back as JSON {error: {code, message, retryable, hint}}. "
    "Coverage is partial: read catalog_stats or catalog_coverage before claiming something is not in the library. "
    "A UI or worker process must run for queued jobs; read catalog_jobs. Model descriptions need checking. "
    "Use catalog_batch to run several read/write calls in one round trip. Treat names, places and faces as private.")


SESSION = {"client": "mcp", "sampling": False, "elicitation": False}
WRITE_LOCK = threading.Lock()
NOT_LOGGED = {"catalog_wait", "catalog_task", "catalog_tasks", "agent_activity", "agent_permissions"}
WRITING_TOOLS = {"catalog_ideas", "idea_pad_generate", "idea_pad_develop"}
WRITER_ARGS = {"writer", "drafts", "allow_images"}
DEFERRED = []          # messages that arrived while waiting for the client's answer to a request of ours
_request_ids = iter(range(1, 10 ** 12))


def tool_properties(name, props):
    """A tool's arguments, plus confirmation (any tool that can change something), background (long
    tools) and the writer options (tools that write ideas)."""
    props = dict(props)
    if contract.TIER_OF.get(name, "destructive") != "read" and name != "catalog_batch":
        props["confirmation"] = {"type": "string", "maxLength": 80}
    if name in contract.LONG:
        props["background"] = {"type": "boolean"}
    if name in WRITING_TOOLS:
        props["writer"] = {"type": "string", "enum": ["local", "agent", "client"]}
        props["drafts"] = {"type": "object"}
        props["allow_images"] = {"type": "boolean"}
    return props


def tool_definition(name, description, action, props):
    extra = []
    if name in contract.LONG:
        extra.append("Set background: true to get a task at once and wait for it with catalog_wait.")
    if name in WRITING_TOOLS:
        extra.append("writer chooses who writes: local (the owner's installed Ollama model, the default), agent (you: the "
                     "call returns needs_writing with prompt_id, system, user and schema; write only a JSON object matching "
                     "the schema and call again with the same arguments plus drafts {prompt_id: your JSON}, keeping earlier "
                     "drafts), or client (the connected client's model through MCP sampling). Every cited shot is checked "
                     "against the evidence whoever writes. Images (previews) go to an outside model only with allow_images.")
    tier = contract.TIER_OF.get(name, "destructive")
    if tier != "read" and name != "catalog_batch":
        extra.append(f"Tier {tier}: when the owner's permissions ask for confirmation the call fails with code "
                     "needs_confirmation (or needs_user) and a token; repeat it with confirmation set to that token.")
    return {"name": name, "title": name.replace("_", " ").capitalize(), "description": " ".join([description, *extra]),
            "inputSchema": {"type": "object", "properties": tool_properties(name, props), "additionalProperties": False},
            "outputSchema": contract.output_schema(name),
            "annotations": contract.annotations(name, name.replace("_", " ").capitalize()),
            "_meta": {"frameatlas/tier": tier}}


def client_request(method, params, send, receive, timeout=180):
    """Ask the client something (sampling, elicitation) and wait for its answer. Other messages that
    arrive meanwhile are kept for the main loop."""
    import time as _time
    if not send or not receive:
        raise contract.ToolError("invalid_arguments", "This connection cannot carry requests to the client")
    rid = f"fa-{next(_request_ids)}"
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    deadline = _time.time() + timeout
    while True:
        left = deadline - _time.time()
        message = receive(left) if left > 0 else None
        if message is None:
            raise contract.ToolError("timeout", f"The client did not answer {method} in time")
        if message.get("id") == rid and ("result" in message or "error" in message):
            if "error" in message:
                err = message["error"] if isinstance(message["error"], dict) else {}
                raise contract.ToolError("failed", f"The client refused {method}: {str(err.get('message', ''))[:200]}")
            return message["result"] if isinstance(message["result"], dict) else {}
        DEFERRED.append(message)


def make_sampler(send, receive):
    import base64

    def sample(system, user, images, max_tokens):
        messages = [{"role": "user", "content": {"type": "text", "text": user}}]
        for aid, data in images:
            messages.append({"role": "user", "content": {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/jpeg"}})
        result = client_request("sampling/createMessage", {"messages": messages, "systemPrompt": system,
                                                           "maxTokens": max(256, min(int(max_tokens), 8000)),
                                                           "includeContext": "none",
                                                           "modelPreferences": {"intelligencePriority": 0.8}}, send, receive)
        content = result.get("content") or {}
        if isinstance(content, list):
            content = next((c for c in content if isinstance(c, dict) and c.get("type") == "text"), {})
        return content.get("text", "") if isinstance(content, dict) else ""
    return sample


def preview_reader(store):
    import re as _re

    def read(aid):
        try:
            asset = store.get(aid, include_scenes=False)
        except ValueError:
            return None
        name = asset.get("thumbnail") or ""
        path = store.root / "thumbnails" / name
        if not _re.fullmatch(r"[a-f0-9]{24}\.jpg", name) or path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 ** 2:
            return None
        return path.read_bytes()
    return read


def ask_owner(store, error, send, receive):
    """When the client can ask its user (MCP elicitation), ask the owner there for a confirm-policy call.
    Returns the token when they allowed it; raises forbidden when they did not."""
    from . import agents
    data = getattr(error, "data", None) or {}
    token = data.get("confirmation")
    if not token:
        return None
    answer = client_request("elicitation/create", {
        "message": f"An AI agent asks to run this in FrameAtlas: {data.get('summary', '')[:300]}. Allow it?",
        "requestedSchema": {"type": "object", "required": ["allow"],
                            "properties": {"allow": {"type": "boolean", "title": "Allow this call"}}}}, send, receive)
    allowed = answer.get("action") == "accept" and isinstance(answer.get("content"), dict) and answer["content"].get("allow") is True
    if not allowed:
        try:
            agents.decide_confirmation(store, token, "declined")
        except ValueError:
            pass
        raise contract.ToolError("forbidden", "The owner did not allow this call", "Do not repeat it unless the owner asks.")
    return token


def call_tool(service, name, args, depth=0, notify=None, send=None, receive=None, session=None):
    """Run one tool by name: check its arguments, apply the owner's permissions, run it (or start it in
    the background), and log it. Returns the result object or raises."""
    from . import agents, writer
    tool = next((t for t in TOOLS if t[0] == name), None)
    if not tool:
        raise contract.ToolError("unknown_tool", f"Unknown tool: {str(name)[:80]}")
    problems = contract.check({"type": "object", "properties": tool_properties(name, tool[3]), "additionalProperties": False}, args)
    if problems:
        raise contract.ToolError("invalid_arguments", "Invalid arguments: " + "; ".join(problems))
    session = SESSION if session is None else session
    scope = session.get("scope", "all")
    tier = contract.TIER_OF.get(name, "destructive")
    if scope == "read" and tier != "read" or scope == "write" and tier not in ("read", "write"):
        raise contract.ToolError("forbidden", f"This connection may only use {'read' if scope == 'read' else 'read and write'} tools",
                                 "Ask the owner for a token with a wider scope on FrameAtlas's Activity page.")
    if name == "catalog_batch":
        if depth:
            raise contract.ToolError("invalid_arguments", "catalog_batch cannot run inside catalog_batch")
        return run_batch(service, args, session)
    if name == "catalog_wait":
        return agents.wait(service, task_id=args.get("task"), job_id=args.get("job"), timeout=args.get("timeout", 30), notify=notify)
    client, store = session["client"], service.store
    clean = {k: v for k, v in args.items() if k not in agents.PRIVATE_ARGS and k not in WRITER_ARGS}
    logged_args = {k: v for k, v in args.items() if k not in agents.PRIVATE_ARGS and k != "drafts"}
    mode = args.get("writer", "local")
    logged = name not in NOT_LOGGED
    try:
        try:
            agents.gate(store, name, args, client)
        except contract.ToolError as gated:
            if gated.code == "needs_confirmation" and session.get("elicitation") and receive and not depth:
                token = ask_owner(store, gated, send, receive)
                return call_tool(service, name, {**args, "confirmation": token}, depth, notify, send, receive, session)
            raise
        if mode != "local" and args.get("background"):
            raise contract.ToolError("invalid_arguments", "Only the local writer can run in the background")
        if args.get("background") and name in contract.LONG:
            result = agents.start_thread(store, name, lambda: service.dispatch(tool[2], clean))
            if logged:
                agents.record(store, client, name, logged_args, result=result, task=result["task"])
            return result
        sampler = None
        if mode == "client":
            if not session.get("sampling") or not receive:
                raise contract.ToolError("invalid_arguments", "This client does not offer MCP sampling; use writer agent instead")
            sampler = make_sampler(send, receive)
        plan = agents.before(store, name, clean)
        try:
            with writer.use(mode, label=client.split(":", 1)[-1], drafts=args.get("drafts"), sampler=sampler,
                            allow_images=args.get("allow_images") is True, preview=preview_reader(store)):
                result = service.dispatch(tool[2], clean)
        except writer.NeedsWriting as needed:
            result = {"needs_writing": needed.request,
                      "how": "Write only a JSON object that matches schema, from the user content and system rules. Call this tool "
                             "again with the same arguments plus drafts: {prompt_id: your JSON text} (keep any earlier drafts). "
                             "FrameAtlas checks every cited item and time against the evidence before saving."}
            if logged:
                agents.record(store, client, name, logged_args, result={"state": "needs writing"})
            return result
        result = result if isinstance(result, dict) else {"result": result}
    except Exception as exc:
        if logged:
            agents.record(store, client, name, logged_args, error=contract.classify(exc))
        raise
    if logged:
        agents.record(store, client, name, logged_args, result=result, plan=agents.after(name, plan, result),
                      task=result.get("task") if isinstance(result.get("task"), int) else None)
    return result


def run_batch(service, args, session=None):
    results, stop = [], args.get("stop_on_error", True)
    for call in args["calls"]:
        name, arguments = call["tool"], call.get("arguments", {})
        try:
            if name not in contract.BATCHABLE and any(t[0] == name for t in TOOLS):
                raise contract.ToolError("invalid_arguments", f"{name} cannot run in a batch; call it on its own",
                                         "Destructive tools and tools that reach Google or Insta360 need their own call.")
            results.append({"tool": name, "ok": True, "result": call_tool(service, name, arguments, depth=1, session=session)})
        except Exception as exc:
            results.append({"tool": name, "ok": False, "error": contract.classify(exc)})
            if stop:
                break
    return {"results": results, "ran": len(results), "stopped": len(results) < len(args["calls"])}


RESOURCES = [
    ("frameatlas://stats", "Catalogue coverage", "What is in the catalogue: sources, media kinds, analysis coverage and storage.", "stats", {}),
    ("frameatlas://people", "Named people", "The people the owner has named, with how often they appear.", "people", {"named_only": True, "limit": 500}),
    ("frameatlas://ideas", "Idea Pad", "Saved video ideas with status, format and edit sequences.", "idea_pad", {"limit": 200}),
    ("frameatlas://trips", "Trips and days out", "Trips away from home, days out and return visits from imported places.", "places_trips", {"limit": 300}),
    ("frameatlas://places", "Places", "Imported places with positions, days and item counts.", "places", {"limit": 2000}),
    ("frameatlas://activity", "Agent activity", "What agents did recently, the calls waiting for the owner, and the owner's permissions.", "activity", {"limit": 50}),
]
TEMPLATES = [
    ("frameatlas://asset/{id}", "Media item", "One catalogue item with its scenes, transcript, people and places.", "application/json"),
    ("frameatlas://preview/{id}", "Preview image", "The stored small JPEG preview of a catalogue item.", "image/jpeg"),
    ("frameatlas://person/{id}", "Person", "One person and their clearest faces.", "application/json"),
    ("frameatlas://place/{id}", "Place", "One imported place with its address, position and days.", "application/json"),
    ("frameatlas://idea/{id}", "Idea", "One Idea Pad entry with its edit sequence.", "application/json"),
]
PROMPTS = {
    "find_footage": ("Find footage for a video idea and show it in FrameAtlas.",
                     [("description", "What the video is about or what shots are needed", True),
                      ("since", "Earliest date, YYYY-MM-DD", False), ("until", "Latest date, YYYY-MM-DD", False)],
                     "Find footage in FrameAtlas for: {description}.{dates} Search with catalog_search (and "
                     "catalog_search_related when the words in captions may differ). If people or places are mentioned, "
                     "look them up with people_list and places_list and search with person or place. Open the best matches "
                     "with catalog_asset to check the scenes really show it. Then show them to me with ui_show (the library "
                     "with your query, or an item at the right time) and list each shot: filename, time range, and why it fits."),
    "sort_people": ("Sort the faces FrameAtlas found into the right people, with the owner's yes for unclear cases.", [],
                    "Help me sort the people in FrameAtlas. Read people_status. Run people_autosort without apply and wait "
                    "for its task with catalog_wait. Then read people_review: for each proposal look at the faces with "
                    "people_compare and decide with people_review_decide (apply only when the faces clearly match; dismiss "
                    "otherwise). Ask me about anything you are unsure of, and show me the person with ui_show first."),
    "plan_trip_video": ("Plan a video about one trip or day out from its places, people and footage.",
                        [("since", "First day of the trip, YYYY-MM-DD", True), ("until", "Last day, YYYY-MM-DD", True),
                         ("format", "short, vlog, montage, documentary or other", False)],
                        "Plan a {format} video about the trip from {since} to {until}. Read places_trips and "
                        "idea_pad_moments for those dates, and catalog_search for the footage (since/until). Note who is in "
                        "it (person filter, people_list) and where. Save the idea with idea_pad_add (linking the best items), "
                        "build its edit sequence with idea_pad_develop (background: true, then catalog_wait), and show me "
                        "the idea with ui_show. Tell me which shots are missing."),
    "check_coverage": ("Report what FrameAtlas knows and what is missing or waiting.", [],
                       "Tell me how complete FrameAtlas is. Read catalog_stats, catalog_coverage, catalog_metadata_coverage, "
                       "catalog_jobs and people_status. Say what is analysed, what is only listed, what failed and why, "
                       "which Google selections expire soon, and the three most useful next steps."),
}


def read_resource(service, uri):
    import base64
    import re as _re
    from . import idea_pad as _ideas
    for fixed, _name, _desc, action, args in RESOURCES:
        if uri == fixed:
            return [{"uri": uri, "mimeType": "application/json", "text": json.dumps(service.dispatch(action, dict(args)), ensure_ascii=False, default=str)}]
    m = _re.fullmatch(r"frameatlas://(asset|preview|person|place|idea)/([A-Za-z0-9_-]{1,64})", str(uri))
    if not m:
        raise contract.ToolError("not_found", "Unknown resource")
    kind, ident = m.groups()
    if kind == "asset":
        data = service.dispatch("asset", {"id": ident})
    elif kind == "preview":
        asset = service.store.get(ident, include_scenes=False)
        name = asset.get("thumbnail") or ""
        path = service.store.root / "thumbnails" / name
        if not _re.fullmatch(r"[a-f0-9]{24}\.jpg", name) or path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 ** 2:
            raise contract.ToolError("not_found", "This item has no stored preview")
        return [{"uri": uri, "mimeType": "image/jpeg", "blob": base64.b64encode(path.read_bytes()).decode()}]
    elif kind == "person":
        data = service.dispatch("person", {"id": int(ident) if ident.isdigit() else -1, "limit": 24})
    elif kind == "place":
        if not ident.isdigit():
            raise contract.ToolError("not_found", "Place not found")
        with service.store.connect() as db:
            row = db.execute("SELECT id, source, key, name, address, country, latitude, longitude FROM places WHERE id=?", (int(ident),)).fetchone()
            if not row:
                raise contract.ToolError("not_found", "Place not found")
            data = dict(row)
            data["days"] = [dict(r) for r in db.execute("SELECT day, items, first, last FROM place_days WHERE place_id=? ORDER BY day", (row["id"],))]
    else:
        with service.store.connect() as db:
            _ideas.ensure(db)
            row = db.execute("SELECT * FROM idea_pad WHERE id=?", (int(ident) if ident.isdigit() else -1,)).fetchone()
        if not row:
            raise contract.ToolError("not_found", "Idea not found")
        data = _ideas._row(row)
    return [{"uri": uri, "mimeType": "application/json", "text": json.dumps(data, ensure_ascii=False, default=str)}]


def get_prompt(name, arguments):
    if name not in PROMPTS:
        raise contract.ToolError("not_found", "Unknown prompt")
    description, params, text = PROMPTS[name]
    arguments = arguments if isinstance(arguments, dict) else {}
    values = {}
    for pname, _desc, required in params:
        value = arguments.get(pname, "")
        if not isinstance(value, str) or len(value) > 2000:
            raise contract.ToolError("invalid_arguments", f"{pname} must be text of up to 2000 characters")
        if required and not value.strip():
            raise contract.ToolError("invalid_arguments", f"{pname} is required")
        values[pname] = value.strip()
    if name == "find_footage":
        dates = " ".join(x for x in (f"From {values['since']}." if values.get("since") else "",
                                     f"Until {values['until']}." if values.get("until") else "") if x)
        values["dates"] = (" " + dates) if dates else ""
    if name == "plan_trip_video":
        values["format"] = values.get("format") or "short"
    return {"description": description, "messages": [{"role": "user", "content": {"type": "text", "text": text.format(**values)}}]}


def rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle(service, message, send=None, receive=None, session=None):
    """Answer one JSON-RPC message. send, when given, writes notifications (progress) to the client."""
    rid = message.get("id")
    method = message.get("method")
    if rid is None:
        return None
    if method == "initialize":
        if session is None:  # stdio: one client for the life of this process
            info = (message.get("params") or {}).get("clientInfo") if isinstance(message.get("params"), dict) else None
            if isinstance(info, dict) and isinstance(info.get("name"), str) and info["name"].strip():
                SESSION["client"] = "mcp:" + info["name"].strip()[:60]
            caps = (message.get("params") or {}).get("capabilities") if isinstance(message.get("params"), dict) else None
            caps = caps if isinstance(caps, dict) else {}
            SESSION["sampling"] = isinstance(caps.get("sampling"), dict)
            SESSION["elicitation"] = isinstance(caps.get("elicitation"), dict)
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}, "prompts": {"listChanged": False}}, "serverInfo": {"name": "frameatlas", "title": "FrameAtlas", "version": VERSION}, "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": [
            {"uri": u, "name": n, "description": d, "mimeType": "application/json"} for u, n, d, _a, _x in RESOURCES]}}
    if method == "resources/templates/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resourceTemplates": [
            {"uriTemplate": u, "name": n, "description": d, "mimeType": m} for u, n, d, m in TEMPLATES]}}
    if method == "resources/read":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        try:
            return {"jsonrpc": "2.0", "id": rid, "result": {"contents": read_resource(service, params.get("uri"))}}
        except Exception as exc:
            error = contract.classify(exc)
            return rpc_error(rid, -32002 if error["code"] == "not_found" else -32603, error["message"])
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"prompts": [
            {"name": n, "description": d, "arguments": [{"name": a, "description": ad, "required": r} for a, ad, r in params]}
            for n, (d, params, _t) in PROMPTS.items()]}}
    if method == "prompts/get":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        try:
            return {"jsonrpc": "2.0", "id": rid, "result": get_prompt(params.get("name"), params.get("arguments"))}
        except Exception as exc:
            return rpc_error(rid, -32602, contract.classify(exc)["message"])
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [tool_definition(*t) for t in TOOLS]}}
    if method == "tools/call":
        try:
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise contract.ToolError("invalid_arguments", "params must be an object")
            args = params.get("arguments")
            args = {} if args is None else args
            if not isinstance(args, dict):
                raise contract.ToolError("invalid_arguments", "Invalid arguments: arguments must be an object")
            token = (params.get("_meta") or {}).get("progressToken") if isinstance(params.get("_meta"), dict) else None
            notify = None
            if send and isinstance(token, (str, int)) and not isinstance(token, bool):
                notify = lambda done, total, text: send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {
                    "progressToken": token, "progress": round(done, 1), "total": total, "message": text[:200]}})
            result = call_tool(service, params.get("name"), args, notify=notify, send=send, receive=receive, session=session)
            text = json.dumps(result, ensure_ascii=False, default=str)
            content = {"content": [{"type": "text", "text": text}] + face_images(service, result),
                       "structuredContent": json.loads(text), "isError": False}
        except Exception as exc:
            error = contract.classify(exc)
            content = {"content": [{"type": "text", "text": json.dumps({"error": error}, ensure_ascii=False)}], "isError": True}
        return {"jsonrpc": "2.0", "id": rid, "result": content}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}


def face_images(service, result):
    """Face crops a tool named in its result, as images an agent can look at (labelled in order)."""
    import base64, re
    from .people import crops_dir
    out = []
    if not isinstance(result, dict) or not isinstance(result.get("images"), list):
        return out
    root = crops_dir(service.store)
    for item in result["images"][:30]:
        crop = item.get("crop", "") if isinstance(item, dict) else ""
        path = root / crop
        if not re.fullmatch(r"[0-9]{1,12}\.jpg", crop) or path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        out.append({"type": "text", "text": str(item.get("label", ""))[:120]})
        out.append({"type": "image", "data": base64.b64encode(path.read_bytes()).decode(), "mimeType": "image/jpeg"})
    return out


def send(message):
    with WRITE_LOCK:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


MAX_LINE = 8 * 1024 ** 2
INVALID = {"__invalid__": True}


class LineReader:
    """JSON-RPC messages from a file descriptor, one per line, with an optional wait. Reads the descriptor
    itself so no line hides in a buffer that select() cannot see."""

    def __init__(self, fd):
        self.fd, self.buffer, self.closed = fd, b"", False

    def message(self, timeout=None):
        import os
        import select
        import time as _time
        deadline = None if timeout is None else _time.time() + max(0.0, timeout)
        while True:
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    if len(line) > MAX_LINE:
                        raise ValueError("Message too large")
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("Expected JSON-RPC object")
                    return message
                except (ValueError, TypeError):
                    return INVALID
            if self.closed:
                if self.buffer.strip():
                    self.buffer += b"\n"
                    continue
                return None
            if len(self.buffer) > MAX_LINE:
                self.buffer = b""
                return INVALID
            if deadline is not None:
                left = deadline - _time.time()
                if left <= 0 or not select.select([self.fd], [], [], left)[0]:
                    return None
            chunk = os.read(self.fd, 65536)
            if not chunk:
                self.closed = True
            self.buffer += chunk


def main():
    service = Service(Store())
    reader = LineReader(sys.stdin.fileno())
    receive = lambda timeout: reader.message(timeout)
    while True:
        message = DEFERRED.pop(0) if DEFERRED else reader.message()
        if message is None:
            break
        if message is INVALID:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC input"}})
            continue
        try:
            response = handle(service, message, send, receive)
        except (ValueError, TypeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC input"}}
        if response:
            send(response)


if __name__ == "__main__":
    main()
