"""Validate recorded positions and optional catalogue search bounds."""
import math
from pathlib import Path
import json
import re
import subprocess
import tempfile
import time

VIDEO_GPS = Path(__file__).resolve().parents[1] / "native-helper/bin/video-gps"
QUICKTIME_COORDINATES = re.compile(r"([+-][0-9]{1,2}(?:\.[0-9]+)?)([+-][0-9]{1,3}(?:\.[0-9]+)?)(?:[+-][0-9]+(?:\.[0-9]+)?)?/?")


def coordinates(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lat, lon = value
    if (type(lat) not in (float, int) or type(lon) not in (float, int)
            or not -90 <= lat <= 90 or not -180 <= lon <= 180 or (lat, lon) == (0, 0)):
        return None
    return {"latitude": lat, "longitude": lon}


def search_bounds(value):
    error = "Enter south, west, north, east in decimal degrees"
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError(error)
    try:
        south, west, north, east = map(float, value.split(","))
    except ValueError:
        raise ValueError(error) from None
    if (not all(math.isfinite(v) for v in (south, west, north, east))
            or not -90 <= south <= north <= 90 or not -180 <= west <= 180 or not -180 <= east <= 180):
        raise ValueError(error)
    return south, west, north, east


def position(asset):
    embedded = asset["analysis"].get("embedded_gps")
    linked = asset["metadata"].get("takeout_location") or {}
    label = "Embedded video GPS" if asset["kind"] == "video" else "Embedded photo GPS"
    for info, label in ((embedded, label), (asset["metadata"].get("gps"), "Catalogue metadata"),
                        (linked.get("gps"), "Linked Google Takeout GPS")):
        if isinstance(info, dict) and (pair := coordinates((info.get("latitude"), info.get("longitude")))):
            return {**pair, "source": label}
    return None


def link_takeout_location(store, picker_id, takeout_id):
    if (not isinstance(picker_id, str) or not re.fullmatch(r"[0-9a-f]{24}", picker_id)
            or not isinstance(takeout_id, str) or (takeout_id and not re.fullmatch(r"[0-9a-f]{24}", takeout_id))):
        raise ValueError("Use catalogue IDs, or an empty Takeout ID to remove the link")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        picker = db.execute("SELECT * FROM assets WHERE id=? AND provider='google_photos'", (picker_id,)).fetchone()
        if (picker is None or picker["source_id"].startswith("takeout:")
                or json.loads(picker["locator"]).get("picker_id") != picker["source_id"]
                or picker["kind"] not in ("photo", "video")):
            raise ValueError("Choose a Google Photos Picker photo or video")
        metadata = json.loads(picker["metadata"])
        if takeout_id:
            source = db.execute("SELECT * FROM assets WHERE id=? AND provider='google_photos'", (takeout_id,)).fetchone()
            if source is None or not source["source_id"].startswith("takeout:") or source["kind"] != picker["kind"]:
                raise ValueError("Choose a Takeout record with the same media type")
            info = json.loads(source["metadata"]).get("gps")
            gps = coordinates((info.get("latitude"), info.get("longitude"))) if isinstance(info, dict) else None
            if gps is None:
                raise ValueError("This Takeout record has no valid recorded GPS")
            metadata["takeout_location"] = {"asset_id": takeout_id, "gps": gps}
        else:
            metadata.pop("takeout_location", None)
        db.execute("UPDATE assets SET metadata=?,updated=? WHERE id=?",
                   (json.dumps(metadata, ensure_ascii=False), time.time(), picker_id))
    return {"id": picker_id, "takeout_id": takeout_id or None,
            "position": store.get(picker_id, include_scenes=False)["position"]}


def video_position(path):
    suffix = path.suffix.lower()
    if suffix not in {".mov", ".mp4", ".m4v", ".insv"}:
        return "unsupported", None
    def read(source):
        result = subprocess.run([str(VIDEO_GPS), str(source)], capture_output=True, timeout=8, check=True)
        return json.loads(result.stdout)
    try:
        if suffix == ".insv":
            # AVFoundation accepts the camera's container through a read-only MP4 alias.
            with tempfile.TemporaryDirectory(prefix="FrameAtlas-video-gps-") as directory:
                alias = Path(directory) / "camera.mp4"
                alias.symlink_to(path.resolve(strict=True))
                values = read(alias)
        else:
            values = read(path)
        if not isinstance(values, list) or len(values) > 16:
            return "unavailable", None
    except (OSError, subprocess.SubprocessError, ValueError):
        return "unavailable", None
    unique = set()
    for value in values:
        match = QUICKTIME_COORDINATES.fullmatch(value) if isinstance(value, str) and len(value) <= 128 else None
        if match:
            pair = tuple(map(float, match.groups()))
            if coordinates(pair):
                unique.add(pair)
    if len(unique) == 1:
        return "recorded", coordinates(next(iter(unique)))
    if unique:
        return "conflicting_embedded_positions", None
    return "no_embedded_position_read", None


def inside_bounds(bounds):
    south, west, north, east = bounds
    def pair(column, path):
        lat_path, lon_path = f"{column}, '$.{path}.latitude'", f"{column}, '$.{path}.longitude'"
        lat, lon = f"json_extract({lat_path})", f"json_extract({lon_path})"
        valid = (f"json_type({lat_path}) IN ('real','integer') AND json_type({lon_path}) IN ('real','integer')"
                 f" AND {lat} BETWEEN -90 AND 90 AND {lon} BETWEEN -180 AND 180 AND ({lat}<>0 OR {lon}<>0)")
        longitude = f"{lon} BETWEEN ? AND ?" if west <= east else f"({lon} >= ? OR {lon} <= ?)"
        return valid, f"{lat} BETWEEN ? AND ? AND {longitude}"
    embedded, embedded_bounds = pair("a.analysis", "embedded_gps")
    supplied, supplied_bounds = pair("a.metadata", "gps")
    linked, linked_bounds = pair("a.metadata", "takeout_location.gps")
    # an item with no recorded position matches by the position of its imported place
    place_longitude = "p.longitude BETWEEN ? AND ?" if west <= east else "(p.longitude >= ? OR p.longitude <= ?)"
    imported = (f"a.id IN (SELECT ap.asset_id FROM asset_places ap JOIN places p ON p.id=ap.place_id "
                f"WHERE p.latitude BETWEEN ? AND ? AND {place_longitude})")
    return (f"(CASE WHEN {embedded} THEN {embedded_bounds} WHEN {supplied} THEN {supplied_bounds} "
            f"WHEN {linked} THEN {linked_bounds} ELSE {imported} END)", [south, north, west, east] * 4)
