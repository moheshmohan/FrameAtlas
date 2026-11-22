"""From an idea to an AutoCut project in one step: the idea's checked edit sequence becomes AutoCut's
catalogue edit plan (autocut.catalogEditPlan), filmcraft-cli assembles it into a project with every shot
in order, and the footage the project used is remembered. Nothing is downloaded here: shots whose
originals are not on this Mac are listed with how to fetch them (catalog_retrieve)."""
import json
import os
from pathlib import Path
import re

from .contract import ToolError

TICKS = 254_016_000_000
POINT_SECONDS = 3.0          # a 360 sample point becomes a shot this long, centred on the sample
MIN_SECONDS = 1.0
ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}
VERTICAL = {"short", "reel"}


def _slug(title):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:60]
    return slug or "idea"


def _retrieved(db, asset_id):
    """The newest editing copy a finished retrieval job left for this item, with its recording parts."""
    for row in db.execute("""SELECT result FROM jobs WHERE asset_id=? AND task='retrieve' AND state='done'
            ORDER BY updated DESC LIMIT 5""", (asset_id,)):
        try:
            result = json.loads(row["result"] or "{}")
        except ValueError:
            continue
        if isinstance(result.get("path"), str) and os.path.isfile(result["path"]):
            parts = {p.get("filename"): p.get("path") for p in result.get("recording_parts") or [] if isinstance(p, dict)}
            return result["path"], parts
    return None, {}


def _source(db, shot, overrides):
    """(path, part start offset) of the file a shot cuts from, or (None, reason)."""
    aid = shot["asset_id"]
    part = shot.get("source_part") if isinstance(shot.get("source_part"), dict) else None
    if aid in overrides:
        path = overrides[aid]
        return (path, None) if os.path.isfile(path) else (None, "the given path is not a file")
    row = db.execute("SELECT local_path, filename, duration FROM assets WHERE id=?", (aid,)).fetchone()
    if not row:
        return None, "not in the catalogue"
    retrieved, parts = _retrieved(db, aid)
    if part:
        path = parts.get(part.get("filename"))
        if path and os.path.isfile(path):
            return path, part
        local = row["local_path"]
        if local and os.path.isfile(local) and Path(local).name == part.get("filename"):
            return local, part
        return None, f"recording part {part.get('filename')} is not on this Mac"
    for path in (row["local_path"], retrieved):
        if path and os.path.isfile(path):
            return path, None
    return None, "the original is not on this Mac"


def plan(store, idea_id, aspect="", overrides=None, name=""):
    """The edit plan for an idea's sequence, and the shots that cannot be placed yet."""
    from . import idea_pad
    overrides = overrides or {}
    if not isinstance(overrides, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in overrides.items()):
        raise ToolError("invalid_arguments", "paths maps catalogue ids to local files")
    with store.connect() as db:
        idea_pad.ensure(db)
        row = db.execute("SELECT * FROM idea_pad WHERE id=?", (idea_id,)).fetchone()
        if not row:
            raise ToolError("not_found", "Idea not found")
        idea = idea_pad._row(row)
        sources = ((idea["basis"].get("sequence") or {}).get("sources")) or []
        if not sources:
            raise ToolError("invalid", "This idea has no edit sequence yet",
                            "Build one first with idea_pad_develop, then try again.")
        if aspect and aspect not in ASPECTS:
            raise ToolError("invalid_arguments", "aspect is " + ", ".join(ASPECTS))
        width, height = ASPECTS[aspect or ("9:16" if idea["format"] in VERTICAL else "16:9")]
        entries, missing, shots = [], [], []
        for n, shot in enumerate(sources, 1):
            if not isinstance(shot, dict) or not shot.get("asset_id"):
                continue
            path, part = _source(db, shot, overrides)
            if path is None:
                missing.append({"shot": n, "asset_id": shot["asset_id"], "filename": shot.get("filename", ""), "why": part})
                continue
            start, end = float(shot.get("start", 0)), float(shot.get("end", 0))
            if part:
                start, end = float(part.get("start", start)), float(part.get("end", end))
            if end - start < MIN_SECONDS:
                middle = (start + end) / 2
                half = (POINT_SECONDS if end == start else MIN_SECONDS) / 2
                start, end = max(0.0, middle - half), middle + half
            duration = db.execute("SELECT duration FROM assets WHERE id=?", (shot["asset_id"],)).fetchone()["duration"] or 0
            if duration and not part:
                end = min(end, duration)
                start = max(0.0, min(start, end - MIN_SECONDS))
            if end <= start:
                missing.append({"shot": n, "asset_id": shot["asset_id"], "filename": shot.get("filename", ""), "why": "no usable time range"})
                continue
            entries.append({"source": {"asset_id": shot["asset_id"], "path": str(Path(path).resolve())},
                            "sourceIn": int(round(start * TICKS)), "duration": int(round((end - start) * TICKS))})
            shots.append({"asset_id": shot["asset_id"], "path": str(Path(path).resolve()), "start": round(start, 3), "end": round(end, 3)})
    edit = {"format": "autocut.catalogEditPlan", "version": 1, "name": (name or idea["title"])[:120],
            "sequence": {"width": width, "height": height, "fps": 30}, "entries": entries}
    return idea, edit, shots, missing


def handoff(store, idea_id, folder, aspect="", overrides=None, build=True, name=""):
    """Write the plan (and with build, the AutoCut project) into folder, and remember the footage used."""
    from . import footage
    from .native import CLI
    import subprocess
    import tempfile
    if not isinstance(folder, str) or not os.path.isabs(folder) or not os.path.isdir(folder):
        raise ToolError("invalid_arguments", "folder is an existing folder, as an absolute path")
    idea, edit, shots, missing = plan(store, idea_id, aspect, overrides, name)
    out = {"idea": idea["id"], "shots": len(shots), "missing": missing, "aspect": f"{edit['sequence']['width']}x{edit['sequence']['height']}"}
    if missing:
        out["next_step"] = ("Fetch the missing originals first: catalog_retrieve each asset_id with an editing destination, "
                            "wait for the jobs (catalog_wait), then call again. Or pass paths {asset_id: local file}.")
    if not shots:
        raise ToolError("invalid", "None of this idea's shots has its original on this Mac yet", out.get("next_step", ""),
                        data={"missing": missing})
    base = Path(folder) / _slug(edit["name"])
    plan_path = base.with_suffix(".edit-plan.json")
    project = base.with_suffix(".fcproj")
    for n in range(2, 100):
        if not project.exists() and not plan_path.exists():
            break
        plan_path, project = Path(f"{base}-{n}.edit-plan.json"), Path(f"{base}-{n}.fcproj")
    plan_path.write_text(json.dumps(edit, ensure_ascii=False, indent=1))
    out["plan"] = str(plan_path)
    if build:
        insv = sorted({e["source"]["path"] for e in edit["entries"] if e["source"]["path"].lower().endswith(".insv")})
        with tempfile.TemporaryDirectory(prefix="FrameAtlas-handoff-") as tmp:
            script = Path(tmp) / "build.jsonl"
            lines = ([json.dumps({"id": "file.import", "params": {"paths": insv}})] if insv else []) + \
                    [json.dumps({"id": "catalog.importEditPlan", "params": {"plan": edit}}, ensure_ascii=False)]
            script.write_text("\n".join(lines) + "\n")
            result = subprocess.run([CLI, "--project", str(project), "run", str(script), "--save"],
                                    capture_output=True, text=True, timeout=600)
        if result.returncode or not project.is_file():
            raise ToolError("failed", "AutoCut could not assemble the project: " + (result.stderr or result.stdout)[-800:],
                            f"The plan is saved at {plan_path}; run: filmcraft-cli catalog assemble <plan> --save-as <project>")
        out["project"] = str(project)
        out["open_with"] = f"filmcraft-cli --project {project} inspect sequence"
    recorded = footage.record(store, edit["name"], [{"asset_id": s["asset_id"], "start": s["start"], "end": s["end"]} for s in shots],
                              project=str(project if build else plan_path), idea_id=idea["id"])
    out["video"] = recorded["video"]
    import time
    with store.connect() as db:
        db.execute("UPDATE idea_pad SET status=CASE WHEN status IN ('idea','planned','shooting') THEN 'editing' ELSE status END, "
                   "updated=? WHERE id=?", (time.time(), idea["id"]))
    return out


def import_project(store, project, title="", idea_id=None):
    """Remember which catalogue items a finished AutoCut project used, read with filmcraft-cli."""
    from . import footage
    from .native import cli
    if not isinstance(project, str) or not os.path.isabs(project) or not os.path.isfile(project):
        raise ToolError("invalid_arguments", "project is an existing .fcproj file, as an absolute path")
    media = cli("--project", project, "exec", "media.status", "{}", timeout=180)
    seq = cli("--project", project, "inspect", "sequence", timeout=180)
    items = media.get("items", media) if isinstance(media, dict) else media
    paths = {str(m.get("item")): m.get("path") for m in items or [] if isinstance(m, dict) and m.get("path")}
    clips = []

    def walk(value, depth=0):
        if depth > 8:
            return
        if isinstance(value, dict):
            if "item" in value and ("sourceIn" in value or "source_in" in value):
                clips.append(value)
            for v in value.values():
                walk(v, depth + 1)
        elif isinstance(value, list):
            for v in value[:5000]:
                walk(v, depth + 1)
    walk(seq)
    used = [paths[str(c["item"])] for c in clips if str(c.get("item")) in paths] or list(paths.values())
    shots = [{"path": p} for p in dict.fromkeys(used)]
    if not shots:
        raise ToolError("invalid", "The project uses no media files FrameAtlas can see")
    return footage.record(store, title or Path(project).stem, shots, project=project, idea_id=idea_id)
