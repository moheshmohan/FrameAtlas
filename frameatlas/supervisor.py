"""One queue for what the local models were unsure of, for the owner or a capable model to settle: today the
people sorter's proposals (merges, links, moves and unlinks in the uncertain band, and Google names that
matched more than one person). Each item is a plain question with its evidence (face crops as images) and
the answers it takes; every decision is logged and can be undone."""
from . import people

KINDS = ("", "merge", "link", "move", "unlink", "name")


def _who(name, pid):
    return f"'{name}'" if name else f"person #{pid}"


def _question(e):
    person, other = _who(e.get("person_name"), e.get("person_id")), _who(e.get("other_name"), e.get("other_id"))
    likeness = e.get("likeness") or 0
    detail = e.get("detail") or {}
    if e["action"] == "merge":
        return f"Are {person} and {other} the same person? Their faces are {likeness:.2f} alike. Apply merges {other} into {person}."
    if e["action"] == "name":
        why = f" ({detail['why']})" if detail.get("why") else ""
        either = (f" Or is it {other}? apply_other gives the name to {other} instead." if e.get("other_id") else "")
        return (f"Should {person} be named '{detail.get('name', '')}' after Google Photos? {detail.get('items', 0)} of "
                f"{detail.get('of', 0)} of that name's photos show them{why}.{either}")
    if e["action"] == "move":
        return (f"Does this face belong to {person} rather than {other}? It is {likeness:.2f} like {person} and "
                f"{detail.get('own', 0):.2f} like {other}.")
    if e["action"] == "link":
        return f"Is this unsorted face {person}? It is {likeness:.2f} like them."
    return f"Is this face really {person}? It is only {likeness:.2f} like their other faces. Apply takes it away from them."


def _images(e):
    out = []
    if e.get("face_crop"):
        out.append({"crop": e["face_crop"], "label": f"The face in question (from {e.get('filename') or 'an item'})"})
    if e.get("person_cover"):
        out.append({"crop": e["person_cover"], "label": f"{_who(e.get('person_name'), e.get('person_id'))}"})
    if e.get("other_cover"):
        out.append({"crop": e["other_cover"], "label": f"{_who(e.get('other_name'), e.get('other_id'))}"})
    return out


def queue(store, kind="", limit=10, offset=0):
    if kind not in KINDS:
        raise ValueError("kind is merge, link, move, unlink or name")
    limit = max(1, min(int(limit), 20))
    r = people.review(store, status="proposed", action=kind, limit=limit, offset=max(0, int(offset)))
    items, images = [], []
    for e in r["items"]:
        crops = _images(e)
        items.append({"id": f"people:{e['id']}", "kind": e["action"], "from": e["actor"], "likeness": e.get("likeness"),
                      "question": _question(e),
                      "answers": ["apply", "apply_other", "dismiss"] if e["action"] == "name" and e.get("other_id") else ["apply", "dismiss"], "item": e.get("asset_id"),
                      "person": e.get("person_id"), "other": e.get("other_id"),
                      "images": [c["label"] for c in crops]})
        for c in crops:
            if len(images) < 30:
                images.append({"crop": c["crop"], "label": f"{items[-1]['id']}: {c['label']}"})
    return {"items": items, "total": r["total"], "images": images,
            "note": "Look at the face images (labelled by item id) before deciding; dismiss anything unclear."}


def decide(store, item_id, decision):
    if not isinstance(item_id, str) or not item_id.startswith("people:") or not item_id[7:].isdigit():
        raise ValueError("Use an id from supervisor_queue, such as people:17")
    if decision not in ("apply", "apply_other", "dismiss"):
        raise ValueError("decision is apply, apply_other or dismiss")
    result = people.decide(store, int(item_id[7:]), decision, actor="agent")
    return {"id": item_id, "decision": decision, "result": result}
