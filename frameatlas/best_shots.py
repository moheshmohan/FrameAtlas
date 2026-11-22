"""The best shots for a trip, a day, a person, a place or a search, from the picture quality FilmCraft
measured for every analysed scene: sharpness, exposure and steadiness, combined into one score. Highlights
are spread out: at most two shots from one item and a few from one day, best first."""
import json

from .contract import ToolError

FILTERS = ("query", "provider", "kind", "since", "until", "person", "place", "category", "used", "bounds", "capture_month")
MAX_ITEMS = 2000


def best(store, limit=24, per_item=2, per_day=6, min_score=0.0, **filters):
    if set(filters) - set(FILTERS):
        raise ToolError("invalid_arguments", "Filter with: " + ", ".join(FILTERS))
    limit = max(1, min(int(limit), 200))
    per_item, per_day = max(1, min(int(per_item), 10)), max(1, min(int(per_day), 50))
    if not isinstance(min_score, (int, float)) or isinstance(min_score, bool) or not 0 <= min_score <= 1:
        raise ToolError("invalid_arguments", "min_score is between 0 and 1")
    found = store.search(**{k: v for k, v in filters.items() if v not in (None, "")}, limit=200, offset=0)
    ids = [a["id"] for a in found["items"]]
    offset = 200
    while len(ids) < min(found["total"], MAX_ITEMS):
        more = store.search(**{k: v for k, v in filters.items() if v not in (None, "")}, limit=200, offset=offset)["items"]
        if not more:
            break
        ids += [a["id"] for a in more]
        offset += 200
    if not ids:
        return {"shots": [], "items_considered": 0, "scored_scenes": 0}
    shots = []
    with store.connect() as db:
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            rows = db.execute(f"""SELECT s.asset_id, s.ordinal, s.start, s.end, s.caption, s.details, a.filename, a.captured, a.kind
                FROM scenes s JOIN assets a ON a.id=s.asset_id
                WHERE s.asset_id IN ({','.join('?' * len(part))}) AND json_extract(s.details, '$.quality.score') IS NOT NULL""",
                              part).fetchall()
            for r in rows:
                try:
                    details = json.loads(r["details"] or "{}")
                except ValueError:
                    continue
                q = details.get("quality") or {}
                score = q.get("score")
                if not isinstance(score, (int, float)) or score < min_score:
                    continue
                shots.append({"asset_id": r["asset_id"], "filename": r["filename"], "captured": r["captured"], "kind": r["kind"],
                              "ordinal": r["ordinal"], "start": r["start"], "end": r["end"], "caption": r["caption"],
                              "score": round(score, 3), "sharpness": round(q.get("sharpness", 0), 3),
                              "exposure": round(q.get("exposure", 0), 3), "shake": round(q.get("shake", 0), 3),
                              "faces_prominent": bool((details.get("description") or {}).get("facesProminent"))})
    shots.sort(key=lambda s: (-s["score"], s["captured"] or "", s["start"] or 0))
    chosen, by_item, by_day = [], {}, {}
    for s in shots:
        day = (s["captured"] or "")[:10]
        if by_item.get(s["asset_id"], 0) >= per_item or by_day.get(day, 0) >= per_day:
            continue
        by_item[s["asset_id"]] = by_item.get(s["asset_id"], 0) + 1
        by_day[day] = by_day.get(day, 0) + 1
        chosen.append(s)
        if len(chosen) >= limit:
            break
    return {"shots": chosen, "items_considered": len(ids), "scored_scenes": len(shots),
            "note": "Scores measure the picture (sharp, well exposed, steady), not what it shows; check the captions."}
