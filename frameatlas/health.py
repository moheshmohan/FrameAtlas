"""What needs attention, in one place: failed jobs grouped by cause with a fix, Google selections about to
expire, items never analysed, Google page reads still to do, faces and sorting proposals waiting, and
agent calls waiting for the owner. Read by the Sources page and by agents (catalog_health) at the start
of a session."""
import re
import time
from datetime import datetime, timezone

from .contract import classify

# (pattern in the error, cause, what to do)
FIXES = [
    (r"not ready to download", "Google is still processing these videos",
     "Retry them later (catalog_jobs, then retry); Google needs time after an upload."),
    (r"studio batch timed out|studio transfer", "An Insta360 Studio transfer did not finish",
     "Finish or cancel it in Studio, clean it (catalog_cleanup_studio_download), then retry."),
    (r"cloud download status 4\d\d|selection (expired|is missing)|picker", "Google refused the download",
     "Make a fresh Google Photos selection of these items (catalog_picker_reselection lists them), then retry."),
    (r"cloud download status 5\d\d", "Google had a server error",
     "Retry later; nothing is wrong on this Mac."),
    (r"exceeds the batch allowance|exceeds|allowance|quota", "The file is larger than the temporary storage allows",
     "Raise the storage limit (catalog_storage_limit) and retry, or analyse the file from a local copy."),
    (r"filmcraft-cli|native worker|native operation", "The FilmCraft engine failed on these files",
     "Check FilmCraft is installed and up to date, then retry; the worker log has details."),
    (r"not installed", "A local model is missing",
     "Install the named model; FrameAtlas never downloads models itself."),
]


def _cause(error):
    low = (error or "").lower()
    for pattern, cause, fix in FIXES:
        if re.search(pattern, low):
            return cause, fix
    info = classify(RuntimeError(error or "Unknown error"))
    short = re.sub(r"/[^\s'\"]+", "…", (error or "Unknown error"))[:90]
    return short, info["hint"]


def failed_jobs(store):
    from .store import RECOVERED_BY
    with store.connect() as db:
        rows = db.execute(f"""SELECT j.id, j.error FROM jobs j WHERE j.state='failed' AND ({RECOVERED_BY}) IS NULL
            ORDER BY j.updated DESC LIMIT 2000""").fetchall()
    groups = {}
    for r in rows:
        cause, fix = _cause(r["error"])
        g = groups.setdefault(cause, {"cause": cause, "fix": fix, "count": 0, "jobs": []})
        g["count"] += 1
        if len(g["jobs"]) < 20:
            g["jobs"].append(r["id"])
    return sorted(groups.values(), key=lambda g: -g["count"])


def _expires_soon(service, hours=48):
    out = []
    now = datetime.now(timezone.utc)
    for s in service.dispatch("picker_sessions", {}).get("items", []):
        if s.get("expired") or not s.get("expires") or not s.get("pending_jobs"):
            continue
        try:
            left = (datetime.fromisoformat(str(s["expires"]).replace("Z", "+00:00")) - now).total_seconds() / 3600
        except ValueError:
            continue
        if left < hours:
            out.append({"session": s["id"], "hours_left": round(left, 1), "pending_jobs": s["pending_jobs"]})
    return out


def report(service):
    from . import agents, google_reads
    store = service.store
    items = []

    def add(kind, level, title, detail, count, next_step):
        items.append({"kind": kind, "level": level, "title": title, "detail": detail, "count": count, "next": next_step})

    for g in failed_jobs(store):
        add("failed_jobs", "action", f"{g['count']} failed job{'s' if g['count'] != 1 else ''}: {g['cause']}", g["fix"], g["count"],
            {"tool": "catalog_jobs", "jobs": g["jobs"]})
    for s in _expires_soon(service):
        add("selection_expiring", "action", f"A Google selection expires in {s['hours_left']} h with {s['pending_jobs']} job(s) waiting",
            "Let the jobs finish before then, or make a fresh selection of the same items.", s["pending_jobs"],
            {"tool": "catalog_picker_queued_items", "session": s["session"]})
    with store.connect() as db:
        never = {r[0]: r[1] for r in db.execute("""SELECT provider, count(*) FROM assets
            WHERE status='inventory' AND kind IN ('photo','video') GROUP BY provider""")}
        jobs = db.execute("SELECT state, count(*) FROM jobs WHERE state IN ('queued','running') GROUP BY state").fetchall()
    for provider, n in sorted(never.items()):
        add("not_analysed", "info", f"{n} {provider.replace('_', ' ')} item{'s' if n != 1 else ''} never analysed",
            "They are listed but not described; search finds them only by name and date.", n,
            {"tool": "catalog_search", "provider": provider, "status": "metadata_only"})
    queued = {r[0]: r[1] for r in jobs}
    paused = store.queue_state()["paused"]
    if paused and queued.get("queued"):
        add("queue_paused", "action", f"The analysis queue is paused with {queued['queued']} job(s) waiting",
            "Resume it when the Mac is free.", queued["queued"], {"tool": "catalog_queue_control", "paused": False})
    for kind, counts in google_reads.summary(store).items():
        left = counts.get("pending", 0) + counts.get("incomplete", 0)
        if left:
            add("google_reads", "info", f"{left} Google {kind} still to read", "Read a few at a time; Google throttles.",
                left, {"tool": "google_read_next", "kind": kind})
    try:
        review = service.dispatch("people_review", {"limit": 1})["counts"].get("proposed", 0)
        unsorted = service.dispatch("faces_unsorted", {"limit": 1})["total"]
    except Exception:
        review = unsorted = 0
    if review:
        add("people_review", "info", f"{review} people-sorting proposal{'s' if review != 1 else ''} waiting",
            "Apply or dismiss them under People › To review.", review, {"tool": "people_review"})
    if unsorted:
        add("faces_unsorted", "info", f"{unsorted} face{'s' if unsorted != 1 else ''} not linked to anyone",
            "Most are passers-by; link the ones you know.", unsorted, {"tool": "faces_unsorted"})
    pending = agents.confirmations(store)
    if pending:
        add("agent_waiting", "action", f"{len(pending)} agent call{'s' if len(pending) != 1 else ''} waiting for your OK",
            "Approve or decline them on the Activity page.", len(pending), {"page": "activity"})
    order = {"action": 0, "info": 1}
    items.sort(key=lambda i: (order[i["level"]], -i["count"]))
    return {"items": items, "needs_action": sum(1 for i in items if i["level"] == "action"), "checked": round(time.time())}
