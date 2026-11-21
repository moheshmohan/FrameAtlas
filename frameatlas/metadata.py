"""What the catalogue knows about each item's capture: when, where, and with what.

Recorded facts (a camera's own make and model from Google's Picker, an embedded or Takeout
position, a manifest location) are kept apart from inferences (a device guessed from the
filename pattern). Google strips positions from Picker downloads and re-encodes videos without
their camera metadata, so for Google items a location needs a linked Takeout record.
"""
from collections import Counter
from datetime import datetime
import json
import re
import time

from .geography import coordinates

# (pattern on the filename, device label, confidence). First match wins; checked after
# recorded camera fields. Labels describe a likely source, never a verified device.
DEVICE_PATTERNS = (
    (r"\.(insv|insp)$", "Insta360 360° camera", "high"),
    (r"^(vid|img)_\d{8}_\d{6}_\d{2}_\d{3}\.", "Insta360 360° camera", "high"),
    (r"^lrv_", "Insta360 360° camera (proxy)", "high"),
    (r"^flow_(vid|img)_", "Phone on an Insta360 Flow gimbal", "medium"),
    (r"^(img|vid|aud|ptt)-\d{8}-wa\d+", "WhatsApp (sent or received)", "high"),
    (r"^pxl_\d{8}", "Google Pixel phone", "high"),
    (r"^(screenshot|screen shot|screen_shot)", "Screenshot", "high"),
    (r"^dji_\d", "DJI drone or camera", "high"),
    (r"^(gopr|gp\d{6}|gh\d{6}|gx\d{6})", "GoPro", "high"),
    (r"-(collage|animation|effects)\.|_top\.|^color_pop", "Google Photos creation", "high"),
    (r"^img_\d{4}\.(heic|mov)$", "iPhone", "high"),
    (r"^img_e?\d{4}\.(jpe?g|png)$", "iPhone or Canon camera", "low"),
    (r"^(img|vid)_\d{8}_\d{6}", "Android phone", "medium"),
    (r"^\d{8}_\d{6}(_\d+)?\.(jpe?g|mp4)$", "Samsung phone", "medium"),
    (r"^dscn\d", "Nikon Coolpix camera", "medium"),
    (r"^dsc_\d", "Nikon camera", "medium"),
    (r"^_?dsc\d{5}", "Sony camera", "medium"),
    (r"^cimg\d", "Casio camera", "medium"),
    (r"^p[0-9a-c]\d{6}\.", "Olympus or Panasonic camera", "low"),
    (r"^received_\d", "Facebook Messenger", "medium"),
    (r"^\d+_\d+_\d+_[no]\.jpe?g$", "Facebook download", "medium"),
)
_PATTERNS = tuple((re.compile(p, re.I), label, confidence) for p, label, confidence in DEVICE_PATTERNS)
STATUS_TEXT = {
    "not_provided_by_picker": "Google Picker does not report positions",
    "unavailable_from_picker": "Google Picker does not report positions",
    "no_embedded_position_read": "No position in the analysed file (Google removes it from downloads)",
}


def device_hint(filename):
    """A likely source device from the filename pattern, or None."""
    if not isinstance(filename, str) or not filename:
        return None
    name = re.sub(r"^(copy of )+", "", filename.strip()[:512], flags=re.I)
    for pattern, label, confidence in _PATTERNS:
        if pattern.search(name):
            return {"label": label, "source": "filename pattern", "confidence": confidence}
    return None


def camera(metadata):
    """The recorded camera, from Picker camera fields or an Insta360 inventory, or None."""
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("camera")
    if isinstance(value, dict):
        make = str(value.get("make") or "").strip()[:120]
        model = str(value.get("model") or "").strip()[:120]
        if not make and not model:
            return None
        label = model if make and model.casefold().startswith(make.casefold()) else " ".join(x for x in (make, model) if x)
        return {"label": label, "make": make, "model": model, "source": str(value.get("source") or "recorded")[:80]}
    if isinstance(value, str) and value.strip():
        return {"label": value.strip()[:120], "make": "", "model": value.strip()[:120], "source": "camera inventory"}
    return None


def picker_camera(capture):
    """Metadata fields for an item's Picker capture report: {"camera": {...}} or nothing."""
    if not isinstance(capture, dict):
        return {}
    out = {}
    for key in ("make", "model", "exposure"):
        if isinstance(capture.get(key), str) and capture[key].strip():
            out[key] = capture[key].strip()[:120]
    for key in ("width", "height", "fps", "focal_length_mm", "aperture_f", "iso"):
        value = capture.get(key)
        if type(value) in (int, float) and 0 < value < 10_000_000:
            out[key] = value
    if not out:
        return {}
    return {"camera": {**out, "source": "Google Picker camera fields"}}


def place(metadata, position):
    """A recorded position, or a location label supplied by the owner, or None."""
    if isinstance(position, dict) and coordinates((position.get("latitude"), position.get("longitude"))):
        return {"latitude": position["latitude"], "longitude": position["longitude"], "source": position.get("source", "recorded")}
    label = metadata.get("location") if isinstance(metadata, dict) else None
    if isinstance(label, str) and label.strip():
        return {"label": label.strip()[:200], "source": "owner metadata"}
    if isinstance(label, dict):
        pair = coordinates((label.get("latitude"), label.get("longitude")))
        name = str(label.get("name") or label.get("label") or "").strip()[:200]
        if pair or name:
            return {**(pair or {}), **({"label": name} if name else {}), "source": "owner metadata"}
    return None


def when(captured, zone=None):
    """Parse a capture time. Times without a zone are taken as written (camera clock)."""
    if not isinstance(captured, str) or not re.match(r"^\d{4}-\d{2}-\d{2}", captured):
        return None
    text = captured.strip()[:40].replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        try:
            moment = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    if not 1990 <= moment.year <= 2100:
        return None
    if moment.tzinfo is not None:
        local = moment.astimezone(zone) if zone is not None else moment.astimezone()
        basis = "this Mac's time zone"
    else:
        local, basis = moment, "camera clock (zone unknown)"
    has_time = len(text) > 10
    return {"date": local.date().isoformat(), "weekday": local.strftime("%A"),
            "time": local.strftime("%H:%M") if has_time else None, "basis": basis}


def facts(asset):
    """One item's capture facts, recorded and inferred kept apart."""
    metadata = asset.get("metadata") or {}
    analysis = asset.get("analysis") or {}
    probe = analysis.get("probe") if isinstance(analysis.get("probe"), dict) else {}
    video = probe.get("video") if isinstance(probe.get("video"), dict) else {}
    recorded = camera(metadata)
    status = analysis.get("photo_gps_status") or analysis.get("video_gps_status") or metadata.get("gps_status")
    picture = {k: video.get(k) for k in ("width", "height", "codec") if video.get(k) not in (None, "")}
    # Stills carry the probe's default rate; a frame rate means something only for moving pictures.
    rate = video.get("frame_rate") if asset.get("kind") == "video" else None
    if isinstance(rate, dict) and type(rate.get("num")) is int and type(rate.get("den")) is int and rate["den"] > 0 < rate["num"]:
        picture["fps"] = round(rate["num"] / rate["den"], 3)
    elif type(rate) in (int, float) and 0 < rate < 2000:
        picture["fps"] = rate
    capture = metadata.get("camera") if isinstance(metadata.get("camera"), dict) else {}
    for key in ("width", "height", "fps") if asset.get("kind") == "video" else ("width", "height"):
        if key in capture and key not in picture:
            picture[key] = capture[key]
    return {"captured": asset.get("captured") or "", "when": when(asset.get("captured")),
            "date_source": metadata.get("date_source") or "",
            "place": place(metadata, asset.get("position")),
            "place_status": None if asset.get("position") else STATUS_TEXT.get(status, status),
            "camera": recorded, "device": None if recorded else device_hint(asset.get("filename")),
            "exposure": {k: capture[k] for k in ("focal_length_mm", "aperture_f", "iso", "exposure") if k in capture} or None,
            "picture": picture or None, "is_360": bool(metadata.get("is_360") or analysis.get("is_360"))}


POSITION_SQL = """(json_type(a.analysis,'$.embedded_gps.latitude') IN ('real','integer')
    OR json_type(a.metadata,'$.gps.latitude') IN ('real','integer')
    OR json_type(a.metadata,'$.takeout_location.gps.latitude') IN ('real','integer')
    OR COALESCE(json_extract(a.metadata,'$.location'),'')<>''
    OR a.id IN (SELECT asset_id FROM asset_places))"""
CAMERA_SQL = """(json_type(a.metadata,'$.camera')='text' AND json_extract(a.metadata,'$.camera')<>''
    OR COALESCE(json_extract(a.metadata,'$.camera.make'),'')<>''
    OR COALESCE(json_extract(a.metadata,'$.camera.model'),'')<>'')"""


def coverage(store):
    """How much of the catalogue has a date, a place, a camera, a device hint and captions."""
    rows = {}
    with store.connect() as db:
        for r in db.execute(f"""SELECT a.provider, a.filename,
                (a.captured GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*') AS dated,
                {POSITION_SQL} AS placed, {CAMERA_SQL} AS cam,
                (a.status IN ('ready','partial')) AS analysed
                FROM assets a"""):
            p = rows.setdefault(r["provider"], Counter())
            p["total"] += 1
            p["date"] += bool(r["dated"])
            p["location"] += bool(r["placed"])
            p["camera"] += bool(r["cam"])
            p["analysed"] += bool(r["analysed"])
            if not bool(r["cam"]) and device_hint(r["filename"]):
                p["device_hint"] += 1
    keys = ("total", "date", "location", "camera", "device_hint", "analysed")
    total = Counter()
    for counts in rows.values():
        total.update(counts)
    notes = []
    google = rows.get("google_photos", Counter())
    if google["total"] and google["location"] < google["total"]:
        notes.append("Google Photos: the Picker does not report positions and Google removes them from downloads. "
                     "Import Google Photos' places (an agent reads them in your browser: places_import) to label items with "
                     "their place, or a Google Takeout export (its JSON sidecars hold the positions) and link records.")
    if google["total"] and google["camera"] < google["total"]:
        notes.append("Google Photos: camera make and model come from the Picker. Items picked from now on keep them; "
                     "earlier picks get them when they are selected again.")
    if total["device_hint"]:
        notes.append("Device hints come from filename patterns (for example IMG_1234.HEIC → iPhone). They are guesses, not recorded facts.")
    return {"total": {k: total[k] for k in keys},
            "providers": {name: {k: counts[k] for k in keys} for name, counts in sorted(rows.items())},
            "notes": notes}


def backfill(store, *, read_video_gps=None, limit=5000):
    """Read positions from linked local video originals that were never checked.

    Only existing local files are opened, read-only; nothing is downloaded. Returns counts.
    """
    from pathlib import Path
    from .geography import video_position
    from .store import local_file_available
    reader = read_video_gps or video_position
    checked = recorded = 0
    with store.connect() as db:
        rows = db.execute("""SELECT id, local_path, analysis FROM assets a
            WHERE kind='video' AND local_path<>'' AND json_extract(a.analysis,'$.video_gps_status') IS NULL
            AND json_type(a.analysis,'$.embedded_gps') IS NULL LIMIT ?""", (int(limit),)).fetchall()
    for row in rows:
        if not local_file_available(row["local_path"]):
            continue
        status, gps = reader(Path(row["local_path"]))
        checked += 1
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT analysis FROM assets WHERE id=?", (row["id"],)).fetchone()
            if current is None:
                continue
            analysis = json.loads(current["analysis"] or "{}")
            analysis["video_gps_status"] = status
            if gps:
                analysis["embedded_gps"] = gps
                recorded += 1
            db.execute("UPDATE assets SET analysis=?,updated=? WHERE id=?",
                       (json.dumps(analysis, ensure_ascii=False), time.time(), row["id"]))
    return {"checked": checked, "recorded": recorded, "candidates": len(rows)}
