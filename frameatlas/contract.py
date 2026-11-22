"""What every agent-facing tool promises: how risky it is, the shape of its input and output, and
typed errors an agent can act on. Shared by the MCP server and the HTTP API."""
import json

# ---- risk tiers
# read: changes nothing. write: an ordinary change the owner can see and correct. destructive: deletes
# or merges records. outside: reaches Google Photos, Insta360 or downloads media.
TIERS = {
    "read": {
        "catalog_search", "catalog_search_related", "catalog_asset", "catalog_stats", "catalog_compare_sources",
        "catalog_coverage", "catalog_resolve", "catalog_job", "catalog_jobs", "catalog_metadata_coverage",
        "catalog_picker_sessions", "catalog_picker_queued_items", "catalog_picker_reselection", "catalog_models",
        "catalog_studio_ocr", "catalog_idea_history", "idea_pad_list", "idea_pad_moments", "people_status",
        "people_list", "person_get", "faces_unsorted", "people_suggest", "people_review", "people_compare",
        "places_list", "places_trips", "agent_activity", "agent_permissions", "catalog_tasks", "catalog_task",
        "catalog_wait", "ui_state", "catalog_health", "google_read_plan", "google_read_next", "google_read_script",
        "supervisor_queue"},
    "write": {
        "catalog_storage_limit", "catalog_retry_speech", "catalog_confirm_studio_download",
        "catalog_cleanup_studio_download", "catalog_cancel", "catalog_queue_control", "catalog_ideas",
        "catalog_metadata_backfill", "catalog_annotate", "catalog_correct_scene", "catalog_import_native",
        "catalog_import_studio_inventory", "catalog_import_studio_captures", "catalog_import_manifest",
        "catalog_import_takeout", "catalog_link_takeout_location", "catalog_attach_local", "catalog_batch",
        "idea_pad_add", "idea_pad_update", "idea_pad_develop", "idea_pad_generate", "people_scan",
        "people_regroup", "person_update", "face_assign", "faces_link", "faces_unlink", "people_autosort",
        "people_review_decide", "people_import_names", "places_import", "agent_undo", "ui_show", "ui_select", "google_read_register",
        "google_read_result", "google_read_reset", "supervisor_decide"},
    "destructive": {
        "idea_pad_delete", "person_merge", "people_forget", "places_forget", "catalog_merge", "catalog_picker_close"},
    "outside": {
        "catalog_analyze", "catalog_retrieve", "catalog_stage_studio_batch", "catalog_picker_create",
        "catalog_picker_import"},
}
TIER_OF = {name: tier for tier, names in TIERS.items() for name in names}
# Tools that reach beyond this Mac (Google, Insta360 Studio, a download), whatever their tier.
OPEN_WORLD = TIERS["outside"] | {"catalog_picker_close"}
# Calling these twice with the same arguments leaves the same state as calling once.
IDEMPOTENT = TIERS["read"] | {
    "catalog_storage_limit", "catalog_queue_control", "catalog_annotate", "catalog_correct_scene",
    "catalog_link_takeout_location", "catalog_attach_local", "catalog_cancel", "idea_pad_update",
    "person_update", "faces_link", "faces_unlink", "face_assign", "places_import", "people_forget",
    "places_forget", "idea_pad_delete", "catalog_picker_close", "catalog_import_native"}
# Tools that can take a while: background: true returns a task to wait on with catalog_wait.
LONG = {"catalog_ideas", "idea_pad_generate", "idea_pad_develop", "places_import", "people_import_names",
        "catalog_metadata_backfill", "catalog_import_takeout", "catalog_import_native", "catalog_import_manifest"}
# Service actions only the owner reaches, through the browser UI: an agent cannot widen its own
# permissions or approve its own calls.
OWNER_ONLY = {"permissions_set", "confirmation_decide"}
# The open page's own channel (its session token, reporting its state, polling for what to show).
PAGE_ONLY = {"session", "ui_report", "ui_poll"}
# Tools a catalog_batch may run: everything except destructive and outside calls, which need their own call.
BATCHABLE = (TIERS["read"] | TIERS["write"]) - {"catalog_batch"}


def annotations(name, title):
    tier = TIER_OF.get(name, "destructive")
    return {"title": title, "readOnlyHint": tier == "read", "destructiveHint": tier == "destructive",
            "idempotentHint": name in IDEMPOTENT, "openWorldHint": name in OPEN_WORLD}


# ---- output shapes: every result is a JSON object; these keys are always present
OUTPUT_REQUIRED = {
    "catalog_search": ["items", "total", "limit", "offset"],
    "catalog_search_related": ["items"],
    "catalog_asset": ["id", "filename", "kind", "scenes"],
    "catalog_stats": ["providers", "kinds", "coverage"],
    "catalog_coverage": ["items", "totals", "has_more"],
    "catalog_jobs": ["counts", "items", "paused"],
    "catalog_metadata_coverage": ["providers", "total"],
    "catalog_idea_history": ["items"],
    "catalog_batch": ["results", "ran", "stopped"],
    "agent_activity": ["items", "total", "pending", "permissions"],
    "agent_permissions": ["permissions"],
    "agent_undo": ["undone"],
    "catalog_tasks": ["items"],
    "catalog_task": ["id", "tool", "state"],
    "catalog_wait": ["state", "waited", "finished_waiting"],
    "ui_show": ["shown", "link"],
    "ui_select": ["selected", "shown"],
    "ui_state": ["page_open", "link"],
    "catalog_health": ["items", "needs_action"],
    "google_read_plan": ["kind", "counts", "searches_last_hour", "next_step"],
    "google_read_next": ["names"],
    "google_read_script": ["step", "script", "how"],
    "google_read_result": ["name", "state"],
    "google_read_register": ["added", "counts"],
    "google_read_reset": ["reset"],
    "supervisor_queue": ["items", "total"],
    "supervisor_decide": ["id", "decision"],
    "idea_pad_list": ["items", "total"],
    "idea_pad_moments": ["moments"],
    "people_status": ["faces", "people", "named", "running"],
    "people_list": ["items", "total"],
    "faces_unsorted": ["items", "total"],
    "people_review": ["items", "total"],
    "places_list": ["places", "total"],
    "places_trips": ["trips"],
    "catalog_picker_sessions": ["items", "total"],
    "catalog_picker_reselection": ["items", "total", "has_more"],
    "catalog_models": ["scenes", "speech", "writing"],
    "places_forget": ["forgotten"],
    "people_forget": ["forgotten_faces"],
}


def output_schema(name):
    schema = {"type": "object"}
    if OUTPUT_REQUIRED.get(name):
        schema["required"] = list(OUTPUT_REQUIRED[name])
    return schema


# ---- input checking: the subset of JSON Schema the tool table uses
TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def check(schema, value, path="arguments"):
    """Problems with value against schema, as short sentences (empty when it fits)."""
    problems = []
    _check(schema, value, path, problems, 0)
    return problems[:20]


def _check(schema, value, path, problems, depth):
    if depth > 16 or len(problems) >= 20 or not isinstance(schema, dict):
        return
    kind = schema.get("type")
    if kind in TYPES and not TYPES[kind](value):
        problems.append(f"{path} must be {'an' if kind[0] in 'aeiou' else 'a'} {kind}")
        return
    if "enum" in schema and value not in schema["enum"]:
        shown = ", ".join(repr(x) for x in schema["enum"][:12])
        problems.append(f"{path} must be one of {shown}")
    if TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path} must be at most {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{path} must have at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{path} must have at most {schema['maxLength']} characters")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{path} must have at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path} must have at most {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True, default=str) for v in value]
            if len(set(seen)) != len(seen):
                problems.append(f"{path} must not repeat items")
        for i, item in enumerate(value[:2000]):
            _check(schema.get("items"), item, f"{path}[{i}]", problems, depth + 1)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}.{key} is required")
        if schema.get("additionalProperties") is False:
            for key in sorted(set(value) - set(props))[:5]:
                allowed = ", ".join(sorted(props)) or "none"
                problems.append(f"{path}.{key} is not a known argument (known: {allowed})")
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            problems.append(f"{path} needs at least {schema['minProperties']} field(s)")
        for key, sub in props.items():
            if key in value:
                _check(sub, value[key], f"{path}.{key}", problems, depth + 1)


# ---- typed errors
class ToolError(ValueError):
    """A ValueError that names its error code, so agents need not parse the message."""

    def __init__(self, code, message, hint="", data=None):
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.data = data


# code: (worth retrying unchanged later, what to do next)
CODES = {
    "invalid_arguments": (False, "Check the arguments against the tool's inputSchema (tools/list)."),
    "unknown_tool": (False, "Call tools/list for the current tool names."),
    "not_found": (False, "Look the ID up again (catalog_search, catalog_jobs, people_list, places_list or idea_pad_list)."),
    "invalid": (False, "The request does not fit the catalogue's current state; read the message."),
    "needs_confirmation": (False, "Ask the owner, then repeat the call with confirm: true."),
    "forbidden": (False, "The owner's permissions do not allow this; ask the owner."),
    "needs_user": (False, "The owner has to act first (for example a fresh Google Photos selection); pass the message on."),
    "busy": (True, "Another local job holds the worker or GPU; retry after it finishes (catalog_jobs, people_status)."),
    "deferred": (True, "The work was put off, not lost; retry later."),
    "timeout": (True, "Retry; for a long source try a shorter range or its proxy."),
    "missing_model": (False, "Install the named local model; FrameAtlas never downloads models itself."),
    "mismatch": (False, "The source no longer matches the indexed file; reanalyse it or use the indexed rendition."),
    "stale": (False, "Read the item again (catalog_asset or catalog_search) for current keys, then retry."),
    "failed": (True, "A local step failed; read the message, then retry or check the worker log."),
    "internal": (False, "Unexpected error inside FrameAtlas; report it with the message."),
}
CLASS_CODES = {"WorkerBusy": "busy", "StageBusy": "busy", "PickerSelectionUnavailable": "needs_user",
               "BatchSizeDeferred": "deferred", "IndexedSourceMismatch": "mismatch"}
MESSAGE_CODES = [  # checked in order against the lower-case message
    (("not found",), "not_found"),
    (("pass confirm",), "needs_confirmation"),
    (("stale", "changed since", "reopen this item", "analysis changed", "source changed"), "stale"),
    (("gpu busy", "could not acquire the local gpu", " is busy", "already running"), "busy"),
    (("timed out", "did not respond"), "timeout"),
    (("not installed", "install filmcraft", "install the"), "missing_model"),
    (("fresh selection", "fresh google photos selection", "sign-in", "sign in"), "needs_user"),
]


def classify(exc):
    """An agent-readable error: code, message, whether a plain retry may work, and the next step."""
    message = str(exc)[:1500] or type(exc).__name__
    code = getattr(exc, "code", None) if isinstance(exc, ToolError) else None
    if code not in CODES:
        code = next((c for cls in type(exc).__mro__ for name, c in CLASS_CODES.items() if cls.__name__ == name), None)
    if code is None:
        low = message.lower()
        code = next((c for needles, c in MESSAGE_CODES if any(n in low for n in needles)), None)
    if code is None:
        code = "invalid" if isinstance(exc, (ValueError, TypeError, LookupError)) else \
            "failed" if isinstance(exc, (RuntimeError, OSError)) else "internal"
    retryable, hint = CODES[code]
    out = {"code": code, "message": message, "retryable": retryable, "hint": getattr(exc, "hint", "") or hint}
    if isinstance(exc, ToolError) and isinstance(exc.data, dict):
        out["data"] = exc.data
    return out
