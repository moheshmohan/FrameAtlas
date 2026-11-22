"""Who writes: the local Ollama model (the default), the calling agent's own model, or the connected
client's model through MCP sampling. Idea, story and edit-sequence writing all make the same chat call;
this module answers it. Whoever writes, the reply goes through the same checks: every shot must cite an
indexed item and time range from the evidence that was sent."""
import contextlib
import hashlib
import json
import re
import threading

from .contract import ToolError

MODES = ("local", "agent", "client")
MAX_DRAFT = 200_000
_state = threading.local()


class NeedsWriting(Exception):
    """The agent is asked to write this reply itself and call again with it as a draft."""

    def __init__(self, request):
        super().__init__("needs writing")
        self.request = request


@contextlib.contextmanager
def use(mode, *, label="", drafts=None, sampler=None, allow_images=False, preview=None):
    if mode not in MODES:
        raise ToolError("invalid_arguments", "writer is local, agent or client")
    previous = getattr(_state, "current", None)
    _state.current = {"mode": mode, "label": label, "drafts": dict(drafts or {}), "sampler": sampler,
                      "allow_images": allow_images, "preview": preview}
    try:
        yield
    finally:
        _state.current = previous


def active():
    """The writer's name when someone other than the local model writes, else None."""
    cur = getattr(_state, "current", None)
    if not cur or cur["mode"] == "local":
        return None
    return f"{cur['mode']}:{cur['label'] or 'agent'}"


def prompt_id(payload):
    messages = [(m.get("role"), m.get("content")) for m in payload.get("messages", [])]
    text = json.dumps({"messages": messages, "format": payload.get("format")}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def json_text(text):
    """The JSON object in a model's reply, without code fences or words around it."""
    if not isinstance(text, str):
        raise ValueError("The reply was not text")
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("The reply has no JSON object")
        text = text[start:end + 1]
    json.loads(text)
    return text


ASSET_ID = re.compile(r"\b[a-f0-9]{24}\b")


def chat(payload, timeout, note="FrameAtlas writing", local=None):
    """Answer one chat request with {"message": {"content": text}}, from whoever writes. local is the
    caller's own way to ask the local model (request_json("/api/chat", ...) by default)."""
    cur = getattr(_state, "current", None)
    if not cur or cur["mode"] == "local":
        from .resources import gpu_lease
        if local is None:
            from .ollama_local import request_json
            local = lambda p, t: request_json("/api/chat", p, timeout=t)
        with gpu_lease(note=note):
            return local(payload, timeout)
    pid = prompt_id(payload)
    messages = payload.get("messages", [])
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    user = "\n\n".join(m["content"] for m in messages if m.get("role") == "user")
    schema = payload.get("format")
    ids = list(dict.fromkeys(ASSET_ID.findall(user)))[:12]
    if cur["mode"] == "agent":
        if pid in cur["drafts"]:
            draft = cur["drafts"][pid]
            if not isinstance(draft, str) or len(draft) > MAX_DRAFT:
                raise ToolError("invalid_arguments", f"The draft for {pid} must be text of up to {MAX_DRAFT} characters")
            try:
                return {"message": {"content": json_text(draft)}}
            except ValueError:
                raise ToolError("invalid_arguments", f"The draft for {pid} is not JSON matching the schema",
                                "Reply with only a JSON object that matches the schema.") from None
        request = {"prompt_id": pid, "system": system, "user": user, "schema": schema}
        if cur["allow_images"]:
            request["previews"] = [f"frameatlas://preview/{i}" for i in ids]
        raise NeedsWriting(request)
    sampler = cur["sampler"]
    if sampler is None:
        raise ToolError("invalid_arguments", "This client cannot write through MCP sampling; use writer agent instead")
    images = []
    if cur["allow_images"] and cur["preview"]:
        for aid in ids[:6]:
            data = cur["preview"](aid)
            if data:
                images.append((aid, data))
    instruction = system + "\n\nReply with only a JSON object that matches this JSON schema:\n" + json.dumps(schema, ensure_ascii=False)
    text = sampler(instruction, user, images, int(payload.get("options", {}).get("num_predict", 3000)))
    try:
        return {"message": {"content": json_text(text)}}
    except ValueError:
        raise ToolError("failed", "The connected model's reply was not JSON; nothing was saved. Try again.") from None
