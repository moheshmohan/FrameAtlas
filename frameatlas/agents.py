"""What agents did and may do: the activity log (with undo where a change can be reversed), the owner's
permission tiers with confirmation tokens, and background tasks an agent can wait on."""
import contextlib
import hashlib
import json
import os
import secrets
import threading
import time

from .contract import TIER_OF, ToolError, classify

POLICIES = ("allow", "confirm", "owner", "deny")
DEFAULT_PERMISSIONS = {"write": "allow", "destructive": "confirm", "outside": "confirm"}
PERMISSIONS_KEY = "agent_permissions"
TOKEN_SECONDS = 15 * 60
KEEP_ACTIVITY, KEEP_TASKS, KEEP_TOKENS = 5000, 500, 500
MAX_RESULT_BYTES = 2 * 1024 ** 2
PRIVATE_ARGS = {"confirmation", "background"}


def ensure(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS agent_activity (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, client TEXT NOT NULL, tool TEXT NOT NULL,
          tier TEXT NOT NULL, args TEXT NOT NULL, ok INTEGER NOT NULL, code TEXT NOT NULL DEFAULT '',
          summary TEXT NOT NULL DEFAULT '', task INTEGER, undo TEXT NOT NULL DEFAULT '', undone REAL);
        CREATE INDEX IF NOT EXISTS agent_activity_tier ON agent_activity(tier, id);
        CREATE TABLE IF NOT EXISTS agent_confirmations (
          token TEXT PRIMARY KEY, tool TEXT NOT NULL, digest TEXT NOT NULL, summary TEXT NOT NULL,
          client TEXT NOT NULL, tier TEXT NOT NULL, policy TEXT NOT NULL, created REAL NOT NULL,
          expires REAL NOT NULL, decision TEXT NOT NULL DEFAULT '', used REAL);
        CREATE TABLE IF NOT EXISTS agent_tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT NOT NULL, kind TEXT NOT NULL, pid INTEGER NOT NULL,
          state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
          started REAL NOT NULL, finished REAL);""")


@contextlib.contextmanager
def _connect(store):
    with store.connect() as db:
        ensure(db)
        yield db


def short_args(args, limit=4000):
    """Arguments as stored in the log: long lists are counted, not copied."""
    def trim(value, depth=0):
        if isinstance(value, list):
            return value if len(value) <= 20 and depth < 3 else f"[{len(value)} items]"
        if isinstance(value, dict) and depth < 3:
            return {k: trim(v, depth + 1) for k, v in list(value.items())[:40]}
        if isinstance(value, str) and len(value) > 300:
            return value[:300] + "…"
        return value
    text = json.dumps(trim({k: v for k, v in args.items() if k not in PRIVATE_ARGS}), ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


# ---- permissions

def permissions(store):
    with _connect(store) as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (PERMISSIONS_KEY,)).fetchone()
    saved = {}
    if row:
        try:
            saved = json.loads(row[0])
        except ValueError:
            saved = {}
    return {tier: saved.get(tier) if saved.get(tier) in POLICIES else policy for tier, policy in DEFAULT_PERMISSIONS.items()}


def set_permissions(store, values):
    """Owner only (the browser UI): an agent cannot widen its own permissions."""
    if not isinstance(values, dict) or not values or set(values) - set(DEFAULT_PERMISSIONS):
        raise ValueError("Set any of: " + ", ".join(DEFAULT_PERMISSIONS))
    for tier, policy in values.items():
        if policy not in POLICIES:
            raise ValueError(f"{tier} must be one of: " + ", ".join(POLICIES))
    merged = {**permissions(store), **values}
    with _connect(store) as db:
        db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (PERMISSIONS_KEY, json.dumps(merged)))
    return {"permissions": merged}


def digest(tool, args):
    clean = {k: v for k, v in args.items() if k not in PRIVATE_ARGS}
    return hashlib.sha256((tool + "\n" + json.dumps(clean, sort_keys=True, default=str)).encode()).hexdigest()


def gate(store, tool, args, client):
    """Raise unless the owner's permissions let this call run now. A confirmation token is bound to the
    exact tool and arguments, single use, and expires after 15 minutes."""
    tier = TIER_OF.get(tool, "destructive")
    if tier == "read":
        return
    policy = permissions(store).get(tier, "confirm")
    if policy == "allow":
        return
    if policy == "deny":
        raise ToolError("forbidden", f"The owner does not let agents use {tier} tools such as {tool}",
                        "Ask the owner; they can change it on FrameAtlas's Activity page.")
    now, token, want = time.time(), args.get("confirmation"), digest(tool, args)
    with _connect(store) as db:
        db.execute("BEGIN IMMEDIATE")
        if token:
            row = db.execute("SELECT * FROM agent_confirmations WHERE token=?", (str(token)[:80],)).fetchone()
            if not row or row["tool"] != tool or row["digest"] != want:
                raise ToolError("invalid_arguments", "This confirmation is for a different call",
                                "Repeat the call without confirmation to get a token for these exact arguments.")
            if row["used"]:
                raise ToolError("invalid_arguments", "This confirmation was already used",
                                "Repeat the call without confirmation to get a new token.")
            if row["decision"] == "declined":
                raise ToolError("forbidden", "The owner declined this call", "Do not repeat it unless the owner asks.")
            if row["expires"] < now:
                raise ToolError("invalid_arguments", "This confirmation expired",
                                "Repeat the call without confirmation to get a new token.")
            if row["policy"] == "owner" and row["decision"] != "approved":
                raise ToolError("needs_user", "Waiting for the owner to approve this on FrameAtlas's Activity page",
                                "Ask the owner to press Approve, then repeat the call with the same confirmation.",
                                data={"confirmation": token})
            db.execute("UPDATE agent_confirmations SET used=? WHERE token=?", (now, token))
            return
        token = "fa-" + secrets.token_urlsafe(12)
        summary = f"{tool} {short_args(args, 300)}"
        db.execute("""INSERT INTO agent_confirmations(token,tool,digest,summary,client,tier,policy,created,expires)
            VALUES(?,?,?,?,?,?,?,?,?)""", (token, tool, want, summary, client, tier, policy, now, now + TOKEN_SECONDS))
        db.execute("""DELETE FROM agent_confirmations WHERE token NOT IN
            (SELECT token FROM agent_confirmations ORDER BY created DESC LIMIT ?)""", (KEEP_TOKENS,))
    if policy == "owner":
        raise ToolError("needs_user", f"{tool} needs the owner's approval on FrameAtlas's Activity page",
                        "Tell the owner what you want to do and why; once they press Approve, repeat the call "
                        "with the same arguments and this confirmation.",
                        data={"confirmation": token, "summary": summary, "expires_in": TOKEN_SECONDS})
    raise ToolError("needs_confirmation", f"{tool} is {'an' if tier[0] in 'aeiou' else 'a'} {tier} tool: confirm it first",
                    "Tell the owner exactly what this call will do and get their yes, then repeat it with the same "
                    "arguments and this confirmation.",
                    data={"confirmation": token, "summary": summary, "expires_in": TOKEN_SECONDS})


def confirmations(store, pending_only=True):
    with _connect(store) as db:
        rows = db.execute("""SELECT token,tool,summary,client,tier,policy,created,expires,decision,used
            FROM agent_confirmations WHERE ? = 0 OR (used IS NULL AND decision='' AND expires>?)
            ORDER BY created DESC LIMIT 100""", (int(pending_only), time.time())).fetchall()
    return [dict(r) for r in rows]


def decide_confirmation(store, token, decision):
    """Owner only: approve or decline a pending call an agent asked for."""
    if decision not in ("approved", "declined"):
        raise ValueError("decision is approved or declined")
    with _connect(store) as db:
        n = db.execute("UPDATE agent_confirmations SET decision=? WHERE token=? AND used IS NULL AND expires>?",
                       (decision, str(token)[:80], time.time())).rowcount
    if not n:
        raise ValueError("Confirmation not found, used or expired")
    return {"token": token, "decision": decision}


# ---- undo: what to save before a change so the change can be reversed

def before(store, tool, args):
    """Capture what this call will change, when the change can be reversed afterwards."""
    try:
        with store.connect() as db:
            if tool == "idea_pad_update":
                row = db.execute("SELECT * FROM idea_pad WHERE id=?", (args.get("id"),)).fetchone()
                if row:
                    old = {"title": row["title"], "logline": row["logline"], "notes": row["notes"], "status": row["status"],
                           "format": row["format"], "tags": json.loads(row["tags"]),
                           "asset_ids": json.loads(row["basis"]).get("asset_ids", [])}
                    return {"kind": "idea_fields", "id": row["id"], "fields": {k: old[k] for k in args if k in old}}
            if tool == "idea_pad_delete":
                row = db.execute("SELECT * FROM idea_pad WHERE id=?", (args.get("id"),)).fetchone()
                if row:
                    return {"kind": "idea_reinsert", "row": dict(row)}
            if tool == "person_update":
                row = db.execute("SELECT id,name,hidden,notes FROM people WHERE id=?", (args.get("id"),)).fetchone()
                if row:
                    old = {"name": row["name"], "hidden": bool(row["hidden"]), "notes": row["notes"]}
                    return {"kind": "person_fields", "id": row["id"], "fields": {k: old[k] for k in args if k in old}}
            if tool == "catalog_annotate":
                row = db.execute("SELECT id,summary,tags,metadata FROM assets WHERE id=?", (args.get("id"),)).fetchone()
                if row:
                    return {"kind": "annotations", "id": row["id"], "summary": row["summary"], "tags": json.loads(row["tags"] or "[]"),
                            "annotations": json.loads(row["metadata"] or "{}").get("annotations")}
            if tool == "catalog_queue_control":
                return {"kind": "queue_paused", "paused": store.queue_state()["paused"]}
            if tool == "catalog_storage_limit":
                return {"kind": "storage_limit", "gib": store.storage_limit() // 1024 ** 3}
    except Exception:
        return None
    if tool == "people_review_decide" and args.get("decision") == "apply":
        return {"kind": "review_undo", "id": args.get("id")}
    ident = str(args.get("id", ""))
    if tool == "supervisor_decide" and args.get("decision") == "apply" and ident.startswith("people:") and ident[7:].isdigit():
        return {"kind": "review_undo", "id": int(ident[7:])}
    return None


def after(tool, plan, result):
    if tool == "idea_pad_add" and isinstance(result, dict) and isinstance(result.get("id"), int):
        return {"kind": "idea_delete", "id": result["id"]}
    return plan


def undo(store, activity_id, client="owner"):
    from . import idea_pad, people
    with _connect(store) as db:
        row = db.execute("SELECT * FROM agent_activity WHERE id=?", (activity_id,)).fetchone()
    if not row:
        raise ValueError("Activity entry not found")
    if row["undone"]:
        raise ToolError("invalid", "This change was already undone")
    plan = json.loads(row["undo"] or "null")
    if not plan:
        raise ToolError("invalid", f"{row['tool']} cannot be undone from the activity log")
    kind = plan["kind"]
    if kind == "idea_delete":
        idea_pad.delete(store, plan["id"])
    elif kind == "idea_fields":
        idea_pad.update(store, plan["id"], **plan["fields"])
    elif kind == "idea_reinsert":
        r = plan["row"]
        with store.connect() as db:
            idea_pad.ensure(db)
            if db.execute("SELECT 1 FROM idea_pad WHERE id=?", (r["id"],)).fetchone():
                raise ToolError("invalid", "An idea with this number exists again")
            db.execute(f"INSERT INTO idea_pad({','.join(r)}) VALUES({','.join('?' * len(r))})", tuple(r.values()))
    elif kind == "person_fields":
        people.update_person(store, plan["id"], **plan["fields"])
    elif kind == "annotations":
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT metadata FROM assets WHERE id=?", (plan["id"],)).fetchone()
            if not current:
                raise ValueError("Media item not found")
            metadata = json.loads(current["metadata"] or "{}")
            if plan["annotations"] is None:
                metadata.pop("annotations", None)
            else:
                metadata["annotations"] = plan["annotations"]
            db.execute("UPDATE assets SET summary=?,tags=?,metadata=?,updated=? WHERE id=?",
                       (plan["summary"], json.dumps(plan["tags"]), json.dumps(metadata, ensure_ascii=False), time.time(), plan["id"]))
            store.reindex(plan["id"], db)
    elif kind == "queue_paused":
        store.queue_state(plan["paused"])
    elif kind == "storage_limit":
        store.storage_limit(plan["gib"])
    elif kind == "review_undo":
        people.decide(store, plan["id"], "undo")
    else:
        raise ToolError("invalid", "Unknown undo")
    with _connect(store) as db:
        db.execute("UPDATE agent_activity SET undone=? WHERE id=?", (time.time(), activity_id))
    record(store, client, "agent_undo", {"id": activity_id}, result={"undone": activity_id})
    return {"undone": activity_id, "tool": row["tool"]}


# ---- the log

def record(store, client, tool, args, result=None, error=None, plan=None, task=None):
    summary = ""
    if error is not None:
        summary = error.get("message", "")[:300]
    elif isinstance(result, dict):
        summary = ", ".join(f"{k}: {result[k]}" for k in ("total", "id", "started", "forgotten", "ran", "task", "state")
                            if k in result and not isinstance(result[k], (dict, list)))[:300]
    with _connect(store) as db:
        cur = db.execute("""INSERT INTO agent_activity(at,client,tool,tier,args,ok,code,summary,task,undo)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (time.time(), str(client)[:80], tool, TIER_OF.get(tool, "destructive"),
                                             short_args(args), int(error is None), (error or {}).get("code", ""), summary,
                                             task, json.dumps(plan) if plan else ""))
        if cur.lastrowid % 100 == 0:
            db.execute("DELETE FROM agent_activity WHERE id <= ?", (cur.lastrowid - KEEP_ACTIVITY,))
        return cur.lastrowid


def activity(store, include_reads=False, tool="", client="", limit=100, offset=0):
    clauses, params = [], []
    if not include_reads:
        clauses.append("tier<>'read'")
    if tool:
        clauses.append("tool=?"); params.append(tool)
    if client:
        clauses.append("client=?"); params.append(client)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
    with _connect(store) as db:
        total = db.execute(f"SELECT count(*) FROM agent_activity {where}", params).fetchone()[0]
        rows = db.execute(f"SELECT * FROM agent_activity {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    items = []
    for r in rows:
        item = {k: r[k] for k in ("id", "at", "client", "tool", "tier", "ok", "code", "summary", "task", "undone")}
        item["args"] = r["args"]
        item["can_undo"] = bool(r["undo"]) and not r["undone"]
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset,
            "pending": confirmations(store), "permissions": permissions(store)}


# ---- background tasks

def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _finish(store, task_id, state, result=None, error=None):
    text = json.dumps(result, ensure_ascii=False, default=str) if result is not None else ""
    if len(text) > MAX_RESULT_BYTES:
        text = json.dumps({"truncated": True, "note": "The result was too large to keep; read it with the matching list tool."})
    with _connect(store) as db:
        db.execute("UPDATE agent_tasks SET state=?,result=?,error=?,finished=? WHERE id=?",
                   (state, text, json.dumps(error) if error else "", time.time(), task_id))


def start_thread(store, tool, work):
    """Run work() in this process's background and return a task an agent can wait on."""
    with _connect(store) as db:
        task_id = db.execute("INSERT INTO agent_tasks(tool,kind,pid,state,started) VALUES(?,?,?,?,?)",
                             (tool, "thread", os.getpid(), "running", time.time())).lastrowid
        db.execute("DELETE FROM agent_tasks WHERE id <= ?", (task_id - KEEP_TASKS,))

    def run():
        try:
            _finish(store, task_id, "done", result=work())
        except BaseException as exc:  # a background failure is recorded, never raised into the server
            _finish(store, task_id, "failed", error=classify(exc))
    threading.Thread(target=run, daemon=True, name=f"frameatlas-task-{task_id}").start()
    return {"task": task_id, "state": "running", "tool": tool}


def start_process(store, tool, pid):
    """A task for work running in its own process (the face scan), finished when its lock is free."""
    with _connect(store) as db:
        return db.execute("INSERT INTO agent_tasks(tool,kind,pid,state,started) VALUES(?,?,?,?,?)",
                          (tool, "process", int(pid), "running", time.time())).lastrowid


def _threads():
    return {t.name for t in threading.enumerate()}


def task(store, task_id):
    with _connect(store) as db:
        row = db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise ValueError("Task not found")
    if row["state"] == "running":
        if row["kind"] == "process":
            from .service import scan_running
            if not scan_running(store):
                from . import people
                _finish(store, row["id"], "done", result=people.progress(store))
                return task(store, task_id)
        elif row["pid"] == os.getpid():
            if f"frameatlas-task-{row['id']}" not in _threads():
                _finish(store, row["id"], "interrupted", error={"code": "failed", "message": "The task stopped without a result"})
                return task(store, task_id)
        elif not _alive(row["pid"]):
            _finish(store, row["id"], "interrupted",
                    error={"code": "failed", "message": "The process running this task ended; start it again"})
            return task(store, task_id)
    out = {k: row[k] for k in ("id", "tool", "kind", "state", "started", "finished")}
    out["result"] = json.loads(row["result"]) if row["result"] else None
    out["error"] = json.loads(row["error"]) if row["error"] else None
    out["seconds"] = round((row["finished"] or time.time()) - row["started"], 1)
    return out


def tasks(store, limit=50, state=""):
    with _connect(store) as db:
        ids = [r[0] for r in db.execute("SELECT id FROM agent_tasks WHERE ?='' OR state=? ORDER BY id DESC LIMIT ?",
                                        (state, state, max(1, min(int(limit), 200))))]
    items = [task(store, i) for i in ids]
    for item in items:
        item.pop("result", None)
    return {"items": items}


TERMINAL_JOB = {"done", "failed", "cancelled"}


def wait(service, task_id=None, job_id=None, timeout=30, notify=None):
    """Block until a task finishes or an analysis job changes state, up to timeout seconds (at most 60)."""
    if (task_id is None) == (job_id is None):
        raise ValueError("Wait for one task or one job")
    timeout = max(1, min(float(timeout), 60))
    read = (lambda: task(service.store, task_id)) if task_id is not None else (lambda: service.dispatch("job", {"id": job_id}))
    first = read()
    start, last, said, current = time.time(), object(), 0.0, first
    while True:
        state = current.get("state")
        if task_id is not None and state != "running":
            break
        if job_id is not None and (state in TERMINAL_JOB or state != first.get("state")):
            break
        elapsed = time.time() - start
        if elapsed >= timeout:
            break
        if notify and (current.get("stage") != last or elapsed - said >= 5):
            last, said = current.get("stage"), elapsed
            notify(elapsed, timeout, f"{state} {last or ''}".strip())
        time.sleep(0.5)
        current = read()
    current = dict(current)
    current["waited"] = round(time.time() - start, 1)
    current["finished_waiting"] = (current.get("state") != "running") if task_id is not None else \
        (current.get("state") in TERMINAL_JOB or current.get("state") != first.get("state"))
    return current
