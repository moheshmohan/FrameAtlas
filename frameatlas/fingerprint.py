"""Exact evidence for files read during local analysis."""
import hashlib
import os
import re
import stat


class IndexedSourceMismatch(ValueError):
    """The available source contradicts recorded analysis evidence."""


def digest_source(path, cancelled):
    digest = hashlib.sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as source:
        initial = os.fstat(source.fileno())
        if not stat.S_ISREG(initial.st_mode) or not 0 < initial.st_size <= 32 * 1024**3:
            return None
        remaining = initial.st_size
        while remaining:
            if cancelled():
                raise RuntimeError("Job cancelled")
            block = source.read(min(8 * 1024**2, remaining))
            if not block:
                return None
            digest.update(block)
            remaining -= len(block)
        for current in (os.fstat(source.fileno()), path.stat()):
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                    initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns):
                return None
    return {"sha256": digest.hexdigest(), "bytes": initial.st_size}


def indexed_fingerprint(asset):
    analysis = asset.get("analysis", {})
    locator = asset.get("locator", {})
    if analysis.get("parts") or any(locator.get(key) for key in ("recording_parts", "second_lens", "secondLens")):
        return None
    return valid_fingerprint(analysis.get("file_fingerprint"))


def valid_fingerprint(evidence):
    if (not isinstance(evidence, dict) or type(evidence.get("bytes")) is not int
            or not 0 < evidence["bytes"] <= 32 * 1024**3
            or not isinstance(evidence.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"])):
        return None
    return {key: evidence[key] for key in ("sha256", "bytes")}


def indexed_part(asset, filename):
    parts = asset.get("analysis", {}).get("parts")
    if not isinstance(parts, list):
        return {}
    matches = [part for part in parts if isinstance(part, dict) and part.get("filename") == filename]
    return matches[0] if len(matches) == 1 else {}


def verify_indexed_source(asset, path, cancelled, *, part_filename=None):
    if part_filename is None:
        expected = indexed_fingerprint(asset)
    else:
        analysis = indexed_part(asset, part_filename).get("analysis")
        expected = valid_fingerprint(analysis.get("file_fingerprint")) if isinstance(analysis, dict) else None
    if expected is None:
        return "no_fingerprint"
    actual = digest_source(path, cancelled)
    if actual is None:
        raise ValueError("Could not verify the indexed source bytes. Check that the source is stable and retry.")
    if actual != expected:
        raise IndexedSourceMismatch("The available file bytes differ from the indexed rendition. "
                                    "Choose the indexed source or run full analysis on this file.")
    return "matched"


def compare_sources(store, first, second):
    if first == second:
        raise ValueError("Choose two different catalogue records")
    items = []
    for aid in (first, second):
        asset = store.get(aid, include_scenes=False)
        items.append({**{key: asset[key] for key in ("id", "provider", "filename", "captured")},
                      "file_fingerprint": indexed_fingerprint(asset)})
    left, right = (item["file_fingerprint"] for item in items)
    result = "unknown" if not (left and right) else ("same_bytes" if left == right else "different_bytes")
    return {"items": items, "result": result, "scope": "file_bytes_observed_during_analysis"}
