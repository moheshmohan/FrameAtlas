"""Other AI apps on this Mac connecting to FrameAtlas over MCP's HTTP transport (POST /mcp on the local web
server). Off until the owner turns it on; each app gets its own bearer token with a scope (read, write or
all), shown once and kept only as a hash. Only the owner, in the browser, can turn it on, make tokens or
revoke them: no MCP tool can."""
import hashlib
import json
import secrets
import time

from .contract import ToolError

SCOPES = ("read", "write", "all")
SETTING = "mcp_http_enabled"
MAX_TOKENS = 20


def ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS mcp_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, scope TEXT NOT NULL, hash TEXT NOT NULL UNIQUE,
        created REAL NOT NULL, last_used REAL)""")


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def enabled(store):
    with store.connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (SETTING,)).fetchone()
    return bool(row) and row[0] == "true"


def set_enabled(store, on):
    if not isinstance(on, bool):
        raise ToolError("invalid_arguments", "enabled is true or false")
    with store.connect() as db:
        db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SETTING, json.dumps(on)))
    return status(store)


def status(store, port=None):
    with store.connect() as db:
        ensure(db)
        tokens = [dict(r) for r in db.execute("SELECT id, label, scope, created, last_used FROM mcp_tokens ORDER BY id")]
    from .ui_channel import base_url
    url = base_url(store) + "mcp"
    return {"enabled": enabled(store), "url": url, "tokens": tokens, "scopes": list(SCOPES)}


def create(store, label, scope):
    if not isinstance(label, str) or not label.strip() or len(label) > 60:
        raise ToolError("invalid_arguments", "label names the app, up to 60 characters")
    if scope not in SCOPES:
        raise ToolError("invalid_arguments", "scope is read, write or all")
    token = "fa_" + secrets.token_urlsafe(32)
    with store.connect() as db:
        ensure(db)
        if db.execute("SELECT count(*) FROM mcp_tokens").fetchone()[0] >= MAX_TOKENS:
            raise ToolError("invalid", f"Revoke a token first; at most {MAX_TOKENS} apps can connect")
        tid = db.execute("INSERT INTO mcp_tokens(label, scope, hash, created) VALUES(?,?,?,?)",
                         (label.strip(), scope, _hash(token), time.time())).lastrowid
    out = status(store)
    config = {"mcpServers": {"frameatlas": {"type": "http", "url": out["url"], "headers": {"Authorization": f"Bearer {token}"}}}}
    return {"id": tid, "label": label.strip(), "scope": scope, "token": token, "url": out["url"], "config": config,
            "note": "Copy the token now: FrameAtlas keeps only its hash."}


def revoke(store, token_id):
    with store.connect() as db:
        ensure(db)
        if not db.execute("DELETE FROM mcp_tokens WHERE id=?", (int(token_id),)).rowcount:
            raise ToolError("not_found", "Token not found")
    return status(store)


def authenticate(store, header):
    """(label, scope) for a valid Authorization: Bearer header, else None."""
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    if not token.startswith("fa_") or len(token) > 100:
        return None
    digest = _hash(token)
    with store.connect() as db:
        ensure(db)
        row = db.execute("SELECT id, label, scope, hash FROM mcp_tokens WHERE hash=?", (digest,)).fetchone()
        if not row or not secrets.compare_digest(row["hash"], digest):
            return None
        db.execute("UPDATE mcp_tokens SET last_used=? WHERE id=?", (time.time(), row["id"]))
    return row["label"], row["scope"]
