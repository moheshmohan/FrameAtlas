from collections import deque
import contextlib
import hashlib
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
import uuid

DATA = Path(os.environ.get("FRAMEATLAS_DATA", str(Path.home() / "Library/Application Support/FrameAtlas")))
PROVIDERS = {"google_photos", "insta360", "local"}
STORAGE_LIMITS_GIB = (2, 4, 8, 16, 32)
QUEUE_LOOKAHEAD = 32
STUDIO_BATCH_LIMIT = 8
INTERACTIVE_ANALYSIS_LIMIT = 8
PICKER_ACQUISITION = "Temporary Google Picker download; deleted after analysis"
LOCAL_ACQUISITION = "Existing original opened read-only; native engine manages any available camera proxy"
CAPTURE_MONTH = """CASE WHEN a.captured GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-*'
    AND substr(a.captured,6,2) BETWEEN '01' AND '12' THEN substr(a.captured,1,7) END"""
METADATA_ONLY = "a.status IS NULL OR a.status NOT IN ('ready','partial')"
SPEECH_PASS_STATES = ("complete", "no_speech", "no_audio", "failed")


def local_file_available(path):
    return isinstance(path, str) and bool(path) and os.path.isfile(path)


def search_terms(text):
    # Mirror the index's word boundaries and common Latin accent folding.
    text = unicodedata.normalize("NFD", text.casefold())
    text = re.sub(r"([a-z])[\u0300-\u036f]+", r"\1", text)
    return tuple(re.findall(r"[^\W_]+", unicodedata.normalize("NFC", text)))


def matches_groups(text, groups):
    terms = search_terms(text)
    unique = set(terms)
    return all(group[0] in unique and (
        len(group) == 1 or any(terms[i:i+len(group)] == group for i in range(len(terms)))
    ) for group in groups)


def searchable_scene(caption, details):
    return " ".join((caption, json.dumps(details.get("description", {}), ensure_ascii=False),
                     str(details.get("source_part", {}).get("filename", ""))))


def speech_key(words, first, last):
    content = json.dumps(words[first:last+1], sort_keys=True)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"speech:{first}:{last}:{digest}"


def speech_hits(words, phrase):
    recent = deque(maxlen=len(phrase))
    previous = None
    for i, word in enumerate(words[:200000]):
        for term in search_terms(str(word.get("text", ""))):
            recent.append((term, i))
            if len(recent) != len(phrase) or term != phrase[-1]:
                continue
            if not all(actual == expected for (actual, _), expected in zip(recent, phrase)):
                continue
            first = recent[0][1]
            if previous == (first, i) or i-first >= 128:
                continue
            start = words[first]
            first_part = start.get("source_part", {})
            last_part = word.get("source_part", {})
            if first_part.get("index") != last_part.get("index"):
                continue
            previous = first, i
            context = " ".join(str(w.get("text", "")) for w in words[max(0, first-4):i+9])
            match = {"kind": "speech", "start": number(start.get("start")),
                     "end": number(word.get("end")), "caption": "Spoken: " + context,
                     "word_start": first, "word_end": i,
                     "speech_key": speech_key(words, first, i),
                     "matched_text": " ".join(str(w.get("text", "")) for w in words[first:i+1])[:500]}
            if first_part:
                match["source_part"] = {**first_part, "end": last_part.get("end")}
            yield match

# Resolve against the asset's current selection. Reimporting the same provider ID
# makes its existing job eligible again without changing the job or its options.
ASSET_PICKER_SESSION = "CASE WHEN json_valid(a.locator) THEN json_extract(a.locator,'$.session_id') END"
JOB_SOURCE = f"""FROM jobs j JOIN assets a ON a.id=j.asset_id
    LEFT JOIN picker_sessions p ON a.provider='google_photos'
        AND p.id=({ASSET_PICKER_SESSION})"""
# A successful retry does not clean up the source volume owned by an earlier job.
RECOVERED_BY = """CASE WHEN j.state='failed' AND CASE WHEN json_valid(j.result)
    THEN json_extract(j.result,'$.studio_download') IS NULL
      OR json_extract(j.result,'$.studio_download.state')='removed' END THEN (
    SELECT later.id FROM jobs later
    WHERE later.asset_id=j.asset_id AND later.task=j.task AND later.state='done' AND later.id>j.id
      AND (j.task<>'retrieve' OR
        (CASE WHEN json_valid(j.options) THEN COALESCE(json_extract(j.options,'$.destination'),'') END =
        CASE WHEN json_valid(later.options) THEN COALESCE(json_extract(later.options,'$.destination'),'') END
        AND CASE WHEN json_valid(j.options) THEN json_extract(j.options,'$.reference') END IS
        CASE WHEN json_valid(later.options) THEN json_extract(later.options,'$.reference') END))
    ORDER BY later.id LIMIT 1) END"""
NEEDS_CLOUD = """(NOT local_file_available(a.local_path) OR
     (j.task IN ('analysis','speech') AND CASE WHEN json_valid(j.options)
       THEN COALESCE(json_extract(j.options,'$.prefer_cloud'),0) ELSE 0 END))"""
PICKER_EXPIRED = "julianday(p.expires)<=julianday('now')"
NEEDS_SELECTION = f"""(CASE WHEN a.provider='google_photos' AND
    (COALESCE(({ASSET_PICKER_SESSION}),'')='' OR p.state IN ('pending','closed') OR {PICKER_EXPIRED})
    THEN {NEEDS_CLOUD} ELSE 0 END)"""
IS_PRIORITY = """CASE WHEN j.state='queued' AND json_valid(j.options)
    THEN COALESCE(json_extract(j.options,'$.priority'),0) ELSE 0 END"""
IS_INTERACTIVE = """CASE WHEN j.state='queued' AND j.task='analysis' AND json_valid(j.options)
    THEN COALESCE(json_extract(j.options,'$.interactive'),0) ELSE 0 END"""
ANALYSIS_DEADLINE = f"""CASE WHEN j.state='queued' AND j.task='analysis' AND p.state='selected'
    AND p.expires<>'' AND {NEEDS_CLOUD} THEN julianday(p.expires) END"""
JOB_PRIORITY = f"""CASE
    WHEN j.task='retrieve' THEN 0
    WHEN j.task='speech' THEN 1
    WHEN j.task='analysis' AND a.provider='insta360' AND {IS_PRIORITY}=1 AND {NEEDS_CLOUD}
        THEN 2
    ELSE 3 END, {IS_INTERACTIVE} DESC, {IS_PRIORITY} DESC,
    {ANALYSIS_DEADLINE} IS NULL, {ANALYSIS_DEADLINE}"""


def bounded(value, limit=4000):
    return str(value or "")[:limit]


def number(value, default=0):
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else default
    except (ValueError, TypeError):
        return default


def scene_key(scene):
    key=scene.get('sample_key')
    if isinstance(key,str) and key and len(key)<=128:
        return 'sample:'+key
    return "%.6f:%.6f"%(number(scene.get('start')),number(scene.get('end')))


def resolve_citation(scenes, start, end, sample_time=None, expected_key="", part_filename=""):
    def seconds(value):
        if isinstance(value, bool):
            raise ValueError("Citation times must be finite and nonnegative")
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Citation times must be finite and nonnegative") from None
        if not math.isfinite(value) or value < 0:
            raise ValueError("Citation times must be finite and nonnegative")
        return value

    sample = seconds(sample_time) if sample_time is not None else None
    if sample is not None and start is None and end is None:
        start = end = sample
    else:
        start, end = seconds(start), seconds(end)
    if end < start or (sample is not None and (abs(start-sample) > .001 or abs(end-sample) > .001)):
        raise ValueError("Use an ordered citation range or a single 360 sample time")
    if not isinstance(expected_key, str) or len(expected_key) > 160:
        raise ValueError("Invalid citation scene key")
    if not isinstance(part_filename, str) or len(part_filename) > 512:
        raise ValueError("Invalid citation recording part")

    for scene in scenes:
        if expected_key and scene["scene_key"] != expected_key:
            continue
        detail = scene["details"]
        if part_filename and (detail.get("source_part") or {}).get("filename") != part_filename:
            continue
        at = detail.get("sample_time")
        if sample is not None:
            if type(at) not in (int, float) or not scene["start"] <= at <= scene["end"] or abs(at-sample) > .001:
                continue
        elif at is not None or not scene["start"] <= start <= end <= scene["end"]:
            continue
        return {"ordinal": scene["ordinal"], "scene_key": scene["scene_key"]}
    return None


def analysis_status(scenes, captions, provenance):
    complete = (bool(scenes) and all(captions)) or provenance.get("media_kind") == "audio"
    if complete and provenance.get("speech_status") in ("complete", "no_speech", "no_audio", "not_applicable") and not provenance.get("coverage_warning"):
        return "ready"
    return "partial"


def has_transcript_words(text):
    return isinstance(text, str) and any(char.isalnum() for char in text)


def normalize_speech(speech, provenance):
    if has_transcript_words(speech.get("text")):
        return speech
    if provenance.get("speech_status") == "complete":
        provenance["speech_status"] = "no_speech"
    return {**speech, "text": "", "words": []} if speech else speech


def calibrated_preview(provenance):
    if provenance.get("preview_projection") == "calibrated_direction":
        return True
    parts = provenance.get("parts")
    if isinstance(parts, list) and parts and isinstance(parts[0], dict):
        provenance = parts[0].get("analysis", {})
    return bool(provenance.get("vr_properties") and provenance.get("directional_analysis", {}).get("windows"))


def require_speech_retry(asset, replace=False):
    provenance = asset["analysis"]
    allowed = ("complete", "no_speech") if replace else ("failed", "no_speech")
    if asset["kind"] not in ("video", "audio") or provenance.get("speech_status") not in allowed:
        status = "completed or empty" if replace else "failed or empty"
        raise ValueError(f"Speech retry requires a {status} transcript on indexed video or audio")
    parts = provenance.get("parts")
    if parts or asset["locator"].get("recording_parts"):
        if replace or not isinstance(parts, list) or not 2 <= len(parts) <= 16 or not any(
            isinstance(part, dict) and isinstance(part.get("analysis"), dict)
            and part["analysis"].get("speech_status") == "failed"
            for part in parts
        ):
            raise ValueError("Multipart speech retry requires a failed part and preserves successful parts")
    if not provenance.get("source_bytes") or not provenance.get("probe", {}).get("duration"):
        raise ValueError("Run full analysis to verify the source before retrying speech")



class Store:
    def __init__(self, root=DATA):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "catalog.sqlite3"
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS assets (
              id TEXT PRIMARY KEY, provider TEXT NOT NULL, source_id TEXT NOT NULL,
              filename TEXT NOT NULL, kind TEXT NOT NULL, captured TEXT DEFAULT '',
              bytes INTEGER DEFAULT 0, duration REAL DEFAULT 0, cloud_url TEXT DEFAULT '',
              locator TEXT DEFAULT '{}', metadata TEXT DEFAULT '{}', local_path TEXT DEFAULT '',
              status TEXT DEFAULT 'inventory', summary TEXT DEFAULT '', tags TEXT DEFAULT '[]',
              categories TEXT DEFAULT '[]', transcript TEXT DEFAULT '', analysis TEXT DEFAULT '{}',
              thumbnail TEXT DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
              UNIQUE(provider, source_id));
            CREATE TABLE IF NOT EXISTS scenes (
              asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
              ordinal INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
              caption TEXT NOT NULL, details TEXT NOT NULL, PRIMARY KEY(asset_id, ordinal));
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id TEXT NOT NULL REFERENCES assets(id),
              state TEXT NOT NULL, stage TEXT DEFAULT '', error TEXT DEFAULT '',
              options TEXT DEFAULT '{}', created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS imports (
              id INTEGER PRIMARY KEY, provider TEXT NOT NULL, label TEXT NOT NULL,
              coverage TEXT NOT NULL, count INTEGER NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS ideas (id INTEGER PRIMARY KEY, prompt TEXT, result TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS people (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0,
              cover_face INTEGER, notes TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS faces (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
              source TEXT NOT NULL, at REAL, bbox TEXT NOT NULL, landmarks TEXT NOT NULL,
              score REAL NOT NULL, side REAL NOT NULL, frontal REAL NOT NULL,
              embedding BLOB NOT NULL, model TEXT NOT NULL,
              person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
              confirmed INTEGER NOT NULL DEFAULT 0, crop TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS faces_asset ON faces(asset_id);
            CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id);
            CREATE TABLE IF NOT EXISTS face_rejects (
              face_id INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
              person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE, PRIMARY KEY(face_id, person_id));
            CREATE TABLE IF NOT EXISTS face_scans (
              asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE, source TEXT NOT NULL,
              model TEXT NOT NULL, faces INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '', scanned REAL NOT NULL,
              PRIMARY KEY(asset_id, source));
            CREATE TABLE IF NOT EXISTS places (
              id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, key TEXT NOT NULL, name TEXT NOT NULL,
              address TEXT NOT NULL DEFAULT '', country TEXT NOT NULL DEFAULT '', latitude REAL, longitude REAL,
              updated REAL NOT NULL, UNIQUE(source, key));
            CREATE TABLE IF NOT EXISTS place_days (
              place_id INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE, day TEXT NOT NULL,
              items INTEGER NOT NULL, first TEXT NOT NULL DEFAULT '', last TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(place_id, day));
            CREATE TABLE IF NOT EXISTS asset_places (
              asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
              place_id INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE, PRIMARY KEY(asset_id, place_id));
            CREATE INDEX IF NOT EXISTS asset_places_place ON asset_places(place_id);
            CREATE TABLE IF NOT EXISTS idea_pad (
              id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, logline TEXT NOT NULL DEFAULT '',
              notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'idea', format TEXT NOT NULL DEFAULT 'other',
              tags TEXT NOT NULL DEFAULT '[]', origin TEXT NOT NULL DEFAULT 'manual', basis TEXT NOT NULL DEFAULT '{}',
              model TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS picker_sessions (
              id TEXT PRIMARY KEY, expires TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
              selected_count INTEGER NOT NULL DEFAULT 0, added_count INTEGER NOT NULL DEFAULT 0,
              captured_min TEXT NOT NULL DEFAULT '', captured_max TEXT NOT NULL DEFAULT '',
              capture_months TEXT NOT NULL DEFAULT '{}',
              created REAL NOT NULL, updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS assets_picker_session
              ON assets(json_extract(locator,'$.session_id')) WHERE provider='google_photos';
            CREATE INDEX IF NOT EXISTS assets_exact_filename ON assets(filename COLLATE NOCASE);
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
              asset_id UNINDEXED, filename, summary, tags, transcript, scenes,
              tokenize='unicode61 remove_diacritics 2');
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            if "task" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN task TEXT NOT NULL DEFAULT 'analysis'")
            if "result" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN result TEXT NOT NULL DEFAULT '{}'")
            if "capture_months" not in {r[1] for r in db.execute("PRAGMA table_info(picker_sessions)")}:
                db.execute("ALTER TABLE picker_sessions ADD COLUMN capture_months TEXT NOT NULL DEFAULT '{}'")
            db.execute("DROP INDEX IF EXISTS one_active_job")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_job_per_task ON jobs(asset_id,task) WHERE state IN ('queued','running','cancelling')")
            db.execute("CREATE INDEX IF NOT EXISTS completed_jobs_by_asset_task ON jobs(asset_id,task,id) WHERE state='done'")
            self.normalize_saved_transcripts(db)
        os.chmod(self.path, 0o600)

    def normalize_saved_transcripts(self, db):
        key = "normalized_transcript_words_v1"
        if db.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone():
            return
        rows = db.execute("""SELECT id,transcript,analysis FROM assets
            WHERE kind IN ('audio','video') AND json_valid(analysis) AND (transcript<>''
                OR CASE WHEN json_valid(analysis) THEN json_extract(analysis,'$.speech_status')='complete' END)
            """).fetchall()
        for row in rows:
            if has_transcript_words(row["transcript"]):
                continue
            provenance = json.loads(row["analysis"])
            if not isinstance(provenance, dict):
                continue
            speech = provenance.get("speech")
            provenance["speech"] = normalize_speech(speech if isinstance(speech, dict) else {}, provenance)
            db.execute("UPDATE assets SET transcript='',analysis=? WHERE id=?",
                       (json.dumps(provenance, ensure_ascii=False), row["id"]))
            if row["transcript"]:
                self.reindex(row["id"], db)
        db.execute("INSERT INTO settings VALUES(?, 'true')", (key,))

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=20)
        db.row_factory = sqlite3.Row
        db.create_function("local_file_available", 1, local_file_available)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='failed',error='Worker interrupted. Retry to resume safely.',updated=? WHERE state IN ('running','cancelling')", (time.time(),))
            db.execute("UPDATE assets SET status='inventory' WHERE status='analyzing'")

    def upsert(self, item, db=None):
        if db is None:
            with self.connect() as conn:
                return self.upsert(item, conn)
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        provider = item.get("provider")
        if provider not in PROVIDERS:
            raise ValueError("Unknown source provider")
        source_id = bounded(item.get("source_id"), 1024)
        filename = bounded(item.get("filename"), 512)
        if not source_id or not filename:
            raise ValueError("Each item needs a stable source_id and filename")
        aid = hashlib.sha256((provider + ":" + source_id).encode()).hexdigest()[:24]
        old = db.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
        # Imports are idempotent and never erase analysis or user annotations.
        now = time.time()
        metadata = {**(json.loads(old["metadata"]) if old else {}), **item.get("metadata", {})}
        previous_locator = json.loads(old["locator"]) if old else {}
        locator = {**previous_locator, **item.get("locator", {})}
        old_session, new_session = previous_locator.get("session_id"), locator.get("session_id")
        if provider == "google_photos" and old_session and new_session and old_session != new_session:
            later_access = db.execute("""SELECT 1 FROM picker_sessions current, picker_sessions incoming
                WHERE current.id=? AND incoming.id=? AND current.state='selected'
                AND julianday(current.expires)>julianday('now')
                AND julianday(current.expires)>julianday(incoming.expires)""",
                (old_session, new_session)).fetchone()
            if later_access:
                locator["session_id"] = old_session
        values = (aid, provider, source_id, filename, item.get("kind", "unknown"),
                  bounded(item.get("captured"), 80), int(number(item.get("bytes"))),
                  number(item.get("duration")), bounded(item.get("cloud_url"), 2048),
                  json.dumps(locator, ensure_ascii=False),
                  json.dumps(metadata, ensure_ascii=False),
                  bounded(item.get("local_path"), 4096), now, now)
        db.execute("""INSERT INTO assets(id,provider,source_id,filename,kind,captured,bytes,duration,cloud_url,locator,metadata,local_path,created,updated)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
          filename=excluded.filename,captured=CASE WHEN excluded.captured<>'' THEN excluded.captured ELSE assets.captured END,
          bytes=MAX(assets.bytes,excluded.bytes),duration=MAX(assets.duration,excluded.duration),
          cloud_url=CASE WHEN excluded.cloud_url<>'' THEN excluded.cloud_url ELSE assets.cloud_url END,
          local_path=CASE WHEN excluded.local_path<>'' THEN excluded.local_path ELSE assets.local_path END,
          metadata=excluded.metadata,locator=excluded.locator,updated=excluded.updated""", values)
        self.reindex(aid, db)
        return aid, old is None

    def reindex(self, aid, db):
        a = db.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
        text = " ".join(searchable_scene(x[0], json.loads(x[1])) for x in db.execute(
            "SELECT caption,details FROM scenes WHERE asset_id=?", (aid,)))
        db.execute("DELETE FROM search WHERE asset_id=?", (aid,))
        db.execute("INSERT INTO search(asset_id,filename,summary,tags,transcript,scenes) VALUES(?,?,?,?,?,?)", (aid, a["filename"], a["summary"], a["tags"] + " " + a["categories"], a["transcript"], text))

    def decode(self, row):
        a = dict(row)
        for key in ("locator", "metadata", "tags", "categories", "analysis"):
            a[key] = json.loads(a[key])
        from .geography import position
        a["position"] = position(a)
        if (a["metadata"].get("is_360") or a["analysis"].get("is_360")) and not calibrated_preview(a["analysis"]):
            a["thumbnail"] = ""
        return a

    def get(self, aid, *, include_scenes=True):
        with self.connect() as db:
            if include_scenes:
                db.execute("BEGIN")
            row = db.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if not row:
                raise ValueError("Media item not found")
            a = self.decode(row)
            if include_scenes:
                a["scenes"] = []
                for scene in db.execute("SELECT * FROM scenes WHERE asset_id=? ORDER BY ordinal", (aid,)):
                    detail = json.loads(scene["details"])
                    a["scenes"].append(dict(scene, details=detail, scene_key=scene_key(detail)))
            return a

    def picker_access(self, session):
        if not session:
            return {"state": "missing", "expires": ""}
        with self.connect() as db:
            row = db.execute(f"""SELECT CASE
                WHEN p.state='closed' THEN 'closed'
                WHEN {PICKER_EXPIRED} THEN 'expired'
                WHEN p.state='pending' THEN 'pending'
                WHEN julianday(p.expires) IS NOT NULL THEN 'selected'
                ELSE 'unknown' END AS state, p.expires
                FROM picker_sessions p WHERE p.id=?""", (session,)).fetchone()
        return dict(row) if row else {"state": "unknown", "expires": ""}

    def search(self, query="", provider="", kind="", status="", category="", since="", until="", limit=48, offset=0,
               bounds="", capture_month="", speech_only=False, filename="", scene_only=False, source_access="",
               speech_state="", person="", place="", *, require_caption=False):
        if speech_only in ("true", "false"):
            speech_only = speech_only == "true"
        if not isinstance(speech_only, bool):
            raise ValueError("Speech-only search must be true or false")
        if scene_only in ("true", "false"):
            scene_only = scene_only == "true"
        if not isinstance(scene_only, bool):
            raise ValueError("Scene-only search must be true or false")
        if speech_only and scene_only:
            raise ValueError("Choose either scene captions or spoken words")
        limit = max(1, min(200, int(limit)))
        offset = max(0, min(10000000, int(offset)))
        query = bounded(query, 500).strip()
        clauses, args = [], []
        if source_access == "local":
            clauses.append("a.local_path<>'' AND local_file_available(a.local_path)")
        elif source_access == "picker":
            clauses.append(f"""a.provider='google_photos' AND EXISTS(
                SELECT 1 FROM picker_sessions p WHERE p.id=({ASSET_PICKER_SESSION})
                AND p.state='selected' AND julianday(p.expires)>julianday(?,'unixepoch'))""")
            args.append(time.time())
        elif source_access != "":
            raise ValueError("Source access must be local or picker")
        if speech_state != "":
            recorded = "CASE WHEN json_valid(a.analysis) THEN json_extract(a.analysis,'$.speech_status') END"
            clauses.append("a.kind IN ('video','audio')")
            if speech_state == "pending":
                clauses.append(f"COALESCE(({recorded}), '') NOT IN (?, ?, ?, ?)")
                args.extend(SPEECH_PASS_STATES)
            elif speech_state in SPEECH_PASS_STATES:
                clauses.append(f"({recorded})=?")
                args.append(speech_state)
            else:
                raise ValueError("Speech state must be complete, no_speech, no_audio, failed or pending")
        if filename != "":
            if not isinstance(filename, str) or not filename.strip() or len(filename) > 512:
                raise ValueError("Provide a filename of 1–512 characters")
            clauses.append("a.filename=? COLLATE NOCASE")
            args.append(filename)
        # Plain-language literal tokens; never accept raw FTS syntax from callers.
        terms = [(token, group) for token in query.split()[:24] if (group := search_terms(token))]
        groups = [group for _, group in terms]
        phrase = tuple(term for group in groups for term in group)
        source = " FROM assets a"
        order = "CASE WHEN a.status IN ('ready','partial') THEN 0 ELSE 1 END,a.captured DESC,a.filename,a.id"
        order_args = []
        if terms:
            source = " FROM search JOIN assets a ON a.id=search.asset_id"
            clauses.append("search MATCH ?")
            order = "CASE WHEN a.filename=? COLLATE NOCASE THEN 0 ELSE 1 END,bm25(search,0,8,3,2,2,4)," + order
            order_args.append(query)
            expression = " AND ".join('"' + token.replace('"', '""') + '"' for token, _ in terms)
            if speech_only:
                expression = 'transcript : "' + " ".join(phrase) + '"'
            elif scene_only:
                expression = "scenes : (" + expression + ")"
            args.append(expression)
        elif query:
            clauses.append("0")
        elif speech_only:
            clauses.append("a.transcript<>''")
        if scene_only:
            caption_test = "caption_matches(sc.caption)" if groups else "sc.caption<>''"
            clauses.append(f"EXISTS(SELECT 1 FROM scenes sc WHERE sc.asset_id=a.id AND {caption_test})")
        elif require_caption:
            clauses.append("EXISTS(SELECT 1 FROM scenes sc WHERE sc.asset_id=a.id AND sc.caption<>'')")
        for key, val in (("provider", provider), ("kind", kind), ("status", "" if status == "metadata_only" else status)):
            if val:
                clauses.append("a." + key + "=?")
                args.append(bounded(val, 80))
        if status == "metadata_only":
            clauses.append(f"({METADATA_ONLY})")
        if capture_month == "unknown":
            clauses.append(f"({CAPTURE_MONTH}) IS NULL")
        elif capture_month != "":
            if (not isinstance(capture_month, str)
                    or not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", capture_month)):
                raise ValueError("Capture month must be YYYY-MM or unknown")
            clauses.append(f"({CAPTURE_MONTH})=?")
            args.append(capture_month)
        if category:
            clauses.append("EXISTS(SELECT 1 FROM json_each(a.categories) WHERE value=?)")
            args.append(bounded(category, 80))
        if since:
            clauses.append("a.captured>=?"); args.append(bounded(since, 32))
        if until:
            clauses.append("a.captured<?"); args.append(bounded(until, 32) + "T99")
        if person not in ("", None):
            # one or more people (comma-separated ids): every one of them is in the item
            ids = str(person).split(",") if isinstance(person, (str, int)) and not isinstance(person, bool) else None
            if not ids or len(ids) > 8 or any(not i.strip().isdigit() or len(i.strip()) > 12 for i in ids):
                raise ValueError("Person must be up to eight person IDs")
            for pid in ids:
                clauses.append("a.id IN (SELECT asset_id FROM faces WHERE person_id=?)")
                args.append(int(pid))
        if place not in ("", None):
            # one or more places (comma-separated ids): the item was taken at any of them
            ids = str(place).split(",") if isinstance(place, (str, int)) and not isinstance(place, bool) else None
            if not ids or len(ids) > 50 or any(not i.strip().isdigit() or len(i.strip()) > 12 for i in ids):
                raise ValueError("Place must be up to 50 place IDs")
            clauses.append(f"a.id IN (SELECT asset_id FROM asset_places WHERE place_id IN ({','.join('?' * len(ids))}))")
            args.extend(int(i) for i in ids)
        if bounds != "":
            from .geography import inside_bounds, search_bounds
            location_clause, location_args = inside_bounds(search_bounds(bounds))
            clauses.append(location_clause)
            args.extend(location_args)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            if scene_only and groups:
                db.create_function("caption_matches", 1, lambda caption: matches_groups(caption, groups), deterministic=True)
            db.execute("BEGIN")
            total = db.execute("SELECT count(*)" + source + where, args).fetchone()[0]
            rows = db.execute("SELECT a.*" + source + where + " ORDER BY " + order + " LIMIT ? OFFSET ?",
                              args + order_args + [limit, offset]).fetchall()
            result = [self.decode(x) for x in rows]
            for a in result:
                a["matches"] = []
                if groups:
                    scene_rows = () if speech_only else db.execute(
                        "SELECT ordinal,start,end,caption,details FROM scenes WHERE asset_id=? ORDER BY ordinal", (a["id"],))
                    for sc in scene_rows:
                        details=json.loads(sc['details'])
                        text = sc["caption"] if scene_only else searchable_scene(sc["caption"], details)
                        if matches_groups(text, groups):
                            match={k:sc[k] for k in ('ordinal','start','end','caption')}
                            match['scene_key'] = scene_key(details)
                            if details.get('source_part'):match['source_part']=details['source_part']
                            if details.get('sample_key'):
                                at=number(details.get('sample_time'),number(sc['start']))
                                match.update({'kind':'directional','start':at,'end':at,'sample_time':at,
                                              'window_start':sc['start'],'window_end':sc['end']})
                                if not details.get('description',{}).get('editor_corrected'):
                                    views=[v for v in details.get('views',[])[:6]
                                           if matches_groups(json.dumps(v.get('description',{}), ensure_ascii=False), groups)]
                                    if views:
                                        views.sort(key=lambda v:not matches_groups(str(v.get('description',{}).get('caption','')), groups))
                                        match['directions']=[{k:v.get(k) for k in ('label','yaw','pitch','at')} for v in views]
                                        match['caption']=' | '.join(str(v.get('label',''))+': '+str(v.get('description',{}).get('caption','')) for v in views)[:4000]
                            a['matches'].append(match)
                    words = [] if scene_only else a["analysis"].get("speech", {}).get("words", [])
                    speech = list(islice(speech_hits(words, phrase), 8))
                    by_time = lambda m: (m['start'], m['end'])
                    visual = sorted(a['matches'], key=by_time)[:20-len(speech)]
                    a["matches"] = sorted(visual + speech, key=by_time)
                a["analysis"] = {k: a["analysis"].get(k) for k in ("speech_status", "review_status", "is_360")}
                a.pop("transcript", None)
            return {"items": result, "total": total, "offset": offset, "limit": limit}

    def stats(self):
        with self.connect() as db:
            group = lambda key: {x[0]: x[1] for x in db.execute("SELECT " + key + ",count(*) FROM assets GROUP BY " + key)}
            return {"total": db.execute("SELECT count(*) FROM assets").fetchone()[0],
                    "providers": group("provider"), "statuses": group("status"), "kinds": group("kind"),
                    "scenes": db.execute("SELECT count(*) FROM scenes").fetchone()[0],
                    "transcribed": db.execute("SELECT count(*) FROM assets WHERE transcript<>''").fetchone()[0],
                    "categories": [x[0] for x in db.execute("SELECT DISTINCT value FROM assets,json_each(categories) ORDER BY value")],
                    "imports": [dict(x) for x in db.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 12")],
                    "db_bytes": self.path.stat().st_size}

    def coverage(self, provider="", limit=24, offset=0, *, kind=""):
        if not isinstance(provider, str) or (provider and provider not in PROVIDERS):
            raise ValueError("Choose Google Photos, Insta360 or local coverage")
        if not isinstance(kind, str) or kind not in ("", "photo", "video", "audio"):
            raise ValueError("Choose photos, videos, audio or all media")
        limit = max(1, min(100, int(limit)))
        offset = max(0, min(100000, int(offset)))
        counts = f"""count(*) AS catalogued,
            count(CASE WHEN status='ready' THEN 1 END) AS ready,
            count(CASE WHEN status='partial' THEN 1 END) AS partial,
            count(CASE WHEN {METADATA_ONLY} THEN 1 END) AS metadata_only,
            count(CASE WHEN transcript<>'' THEN 1 END) AS transcribed"""
        kind_where = " WHERE kind=?" if kind else ""
        kind_args = (kind,) if kind else ()
        where = kind_where
        args = kind_args
        if provider:
            where += (" AND " if kind else " WHERE ") + "provider=?"
            args += (provider,)
        with self.connect() as db:
            db.execute("BEGIN")
            totals = [dict(row) for row in db.execute(
                f"SELECT provider,{counts} FROM assets a{kind_where} GROUP BY provider ORDER BY provider", kind_args)]
            rows = [dict(row) for row in db.execute(
                f"""SELECT provider,{CAPTURE_MONTH} AS month,{counts} FROM assets a{where}
                    GROUP BY provider,month ORDER BY month DESC,provider LIMIT ? OFFSET ?""",
                [*args, limit+1, offset])]
        return {"totals": totals, "items": rows[:limit], "offset": offset, "limit": limit, "kind": kind,
                "has_more": len(rows) > limit, "scope": "catalogued_records_only",
                "month_basis": "Capture calendar as stored. Missing or invalid month is null."}

    def imported(self, provider, label, coverage, count, db):
        db.execute("INSERT INTO imports(provider,label,coverage,count,created) VALUES(?,?,?,?,?)", (provider, bounded(label, 300), bounded(coverage, 2000), count, time.time()))

    def analysis(self, aid, scenes, speech, provenance, thumbnail="", *, job_id=None):
        from .taxonomy import classify
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if job_id is not None:
                job = db.execute(
                    "SELECT task,result FROM jobs WHERE id=? AND asset_id=? AND state='running'",
                    (job_id, aid)).fetchone()
                if job is None:
                    raise ValueError("Analysis job is no longer active")
            row = db.execute("SELECT metadata,provider FROM assets WHERE id=?", (aid,)).fetchone()
            if not row:
                raise ValueError("Media item not found")
            annotations = json.loads(row[0]).get("annotations", {})
            keys={scene_key(scene) for scene in scenes[:10000]}
            retained=[]
            for old in db.execute('SELECT details FROM scenes WHERE asset_id=? LIMIT 10000',(aid,)):
                previous=json.loads(old[0]);key=scene_key(previous)
                if key not in keys and annotations.get('scenes',{}).get(key):
                    previous['retained_editor_scene']=True
                    retained.append(previous)
            if retained:
                provenance['retained_editor_scenes']=len(retained)
                scenes=retained+scenes[:max(0,10000-len(retained))]
            if len(scenes) > 10000:
                provenance["coverage_warning"] = "Scene storage cap reached; split this long source before continuing."
            for scene in scenes[:10000]:
                key = scene_key(scene)
                corrected = annotations.get("scenes", {}).get(key)
                if corrected:
                    scene["generated_description"] = scene.get("generated_description", scene.get("description", {}))
                    scene["description"] = {"caption": corrected, "editor_corrected": True}
            captions = [s.get("description", {}).get("caption", "") for s in scenes[:10000]]
            speech = normalize_speech(speech, provenance)
            tags, categories = classify(scenes[:10000], " ".join(captions) + " " + speech.get("text", ""))
            status = analysis_status(scenes, captions, provenance)
            tags = annotations.get("tags", tags)
            overview=[s.get('description',{}).get('caption','') for s in scenes[:10000] if not s.get('sample_key')]
            summary = annotations.get("summary", " ".join((overview or captions)[:3])[:2000])
            db.execute("DELETE FROM scenes WHERE asset_id=?", (aid,))
            for i, scene in enumerate(scenes[:10000]):
                start, end = number(scene.get("start")), number(scene.get("end"))
                db.execute("INSERT INTO scenes VALUES(?,?,?,?,?,?)", (aid, i, start, max(start, end), bounded(scene.get("description", {}).get("caption")), json.dumps(scene, ensure_ascii=False)))
            provenance["speech"] = speech
            duration = number(provenance.get("probe", {}).get("duration")) / 254016000000
            if duration > 0:
                db.execute("UPDATE assets SET duration=? WHERE id=?", (duration, aid))
            source_bytes=provenance.get('source_bytes')
            if row['provider']=='insta360' and isinstance(source_bytes,int) and not isinstance(source_bytes,bool) and 0<source_bytes<2**63:
                db.execute('UPDATE assets SET bytes=? WHERE id=?',(source_bytes,aid))
            db.execute("UPDATE assets SET status=?,summary=?,tags=?,categories=?,transcript=?,analysis=?,thumbnail=?,updated=? WHERE id=?",
                       (status, summary, json.dumps(tags, ensure_ascii=False), json.dumps(categories), speech.get("text", ""), json.dumps(provenance, ensure_ascii=False), thumbnail, time.time(), aid))
            if job_id is not None and job["task"] == "analysis" and row["provider"] == "google_photos":
                source = {PICKER_ACQUISITION: "picker", LOCAL_ACQUISITION: "local"}.get(provenance.get("acquisition"))
                if source:
                    receipt = json.loads(job["result"])
                    receipt["analysis_source"] = source
                    db.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps(receipt), job_id))
            self.reindex(aid, db)

    def record_speech_retry(self, jid, original, speech, report):
        from .taxonomy import classify
        aid = original["id"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT state,options FROM jobs WHERE id=? AND asset_id=? AND task='speech'",
                             (jid, aid)).fetchone()
            if job is None or job["state"] != "running":
                raise ValueError("Speech retry is no longer active")
            row = db.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if row is None:
                raise ValueError("Media item no longer exists")
            asset = self.decode(row)
            replace = json.loads(job["options"]).get("replace", False)
            require_speech_retry(asset, replace)
            provenance = asset["analysis"]
            if provenance != original["analysis"]:
                raise ValueError("The underlying analysis changed. Retry speech on the updated item.")
            rows = db.execute("SELECT caption,details FROM scenes WHERE asset_id=? ORDER BY ordinal", (aid,)).fetchall()
            scenes = [json.loads(r["details"]) for r in rows]
            captions = [r["caption"] for r in rows]
            text = speech.get("text", "")
            if replace and (not has_transcript_words(text) or
                            not any(has_transcript_words(word.get("text")) for word in speech.get("words", []))):
                raise ValueError("The replacement returned no timed words. The existing transcript was kept.")
            speech = normalize_speech(speech, report)
            text = speech.get("text", "")
            tags, categories = classify(scenes, " ".join(captions) + " " + text)
            tags = asset["metadata"].get("annotations", {}).get("tags", tags)
            provenance.update(report, speech=speech)
            status = analysis_status(scenes, captions, provenance)
            db.execute("UPDATE assets SET status=?,tags=?,categories=?,transcript=?,analysis=?,updated=? WHERE id=?",
                       (status, json.dumps(tags, ensure_ascii=False), json.dumps(categories), text,
                        json.dumps(provenance, ensure_ascii=False), time.time(), aid))
            self.reindex(aid, db)

    def annotate(self, aid, tags=None, summary=None):
        if tags is not None:
            if not isinstance(tags, list) or len(tags) > 80:
                raise ValueError("tags must be a list of at most 80 strings")
            tags = [bounded(tag, 100) for tag in tags]
        if summary is not None:
            summary = bounded(summary)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT metadata FROM assets WHERE id=?", (aid,)).fetchone()
            if row is None:
                raise ValueError("Media item not found")
            metadata = json.loads(row["metadata"])
            annotations = metadata.setdefault("annotations", {})
            now = time.time()
            if tags is not None:
                db.execute("UPDATE assets SET tags=?,updated=? WHERE id=?", (json.dumps(tags), now, aid))
                annotations["tags"] = tags
            if summary is not None:
                db.execute("UPDATE assets SET summary=?,updated=? WHERE id=?", (summary, now, aid))
                annotations["summary"] = summary
            db.execute("UPDATE assets SET metadata=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), aid))
            self.reindex(aid, db)
        return self.get(aid)

    def correct_scene(self, aid, ordinal, caption, expected_key=None):
        caption = bounded(caption, 4000).strip()
        if not caption:
            raise ValueError("A corrected scene caption cannot be empty")
        ordinal = int(ordinal)
        if expected_key is not None and (not isinstance(expected_key, str) or not 0 < len(expected_key) <= 160):
            raise ValueError("Use the scene_key returned with the scene")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT metadata FROM assets WHERE id=?", (aid,)).fetchone()
            if row is None:
                raise ValueError("Media item not found")
            scene = db.execute("SELECT details FROM scenes WHERE asset_id=? AND ordinal=?", (aid, ordinal)).fetchone()
            if scene is None:
                raise ValueError("Scene not found. Reopen this item to see its current scenes.")
            detail = json.loads(scene["details"])
            key = scene_key(detail)
            if expected_key is not None and expected_key != key:
                raise ValueError("The scene changed since you opened it. Reopen this item to choose the current scene.")
            detail["generated_description"] = detail.get("generated_description", detail.get("description", {}))
            detail["description"] = {"caption": caption, "editor_corrected": True}
            metadata = json.loads(row["metadata"])
            metadata.setdefault("annotations", {}).setdefault("scenes", {})[key] = caption
            db.execute("UPDATE scenes SET caption=?,details=? WHERE asset_id=? AND ordinal=?", (caption, json.dumps(detail, ensure_ascii=False), aid, ordinal))
            db.execute("UPDATE assets SET metadata=?,updated=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), time.time(), aid))
            self.reindex(aid, db)
        return self.get(aid)

    @staticmethod
    def analysis_options(options):
        options = options or {}
        if not isinstance(options, dict) or set(options) - {"language", "detail", "model", "fresh", "prefer_cloud", "priority", "interactive"}:
            raise ValueError("Unknown analysis options")
        for key in ("fresh", "prefer_cloud", "priority", "interactive"):
            if key in options and not isinstance(options[key], bool):
                raise ValueError(key + " must be boolean")
        if options.get("language", "auto") not in ("auto", "en", "ml", "hi", "ta", "te", "kn", "ko"):
            raise ValueError("Unsupported language")
        return options

    def enqueue(self, aid, options=None):
        self.get(aid, include_scenes=False)
        options = self.analysis_options(options)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id,state,options FROM jobs WHERE asset_id=? AND task='analysis' AND state IN ('queued','running','cancelling')", (aid,)).fetchone()
            if existing:
                promotions = {key: True for key in ("priority", "interactive") if options.get(key)}
                if promotions and existing['state']=='queued':
                    prior=json.loads(existing['options'])
                    if not isinstance(prior,dict):raise ValueError('Queued job has invalid options; cancel and retry it')
                    prior.update(promotions)
                    db.execute('UPDATE jobs SET options=?,updated=? WHERE id=?',(json.dumps(prior),time.time(),existing['id']))
                return {"id": existing[0], "existing": True}
            now = time.time()
            cur = db.execute("INSERT INTO jobs(asset_id,state,options,created,updated) VALUES(?,'queued',?,?,?)", (aid, json.dumps(options), now, now))
            return {"id": cur.lastrowid}

    def enqueue_studio_batch(self, ids, options=None):
        from .camera_parts import names
        if (not isinstance(ids, list) or not 2 <= len(ids) <= STUDIO_BATCH_LIMIT
                or any(not isinstance(aid, str) for aid in ids) or len(set(ids)) != len(ids)):
            raise ValueError(f"Select 2–{STUDIO_BATCH_LIMIT} distinct cloud-only Insta360 items")
        opts = {"priority": True, **self.analysis_options(options), "studio_batch": uuid.uuid4().hex}
        queued = []
        filenames = set()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for aid in ids:
                asset = db.execute("SELECT provider,kind,filename,local_path FROM assets WHERE id=?", (aid,)).fetchone()
                if (asset is None or asset["provider"] != "insta360" or asset["kind"] not in ("video", "photo")
                        or asset["local_path"]):
                    raise ValueError("A Studio batch requires cloud-only Insta360 photos or videos")
                names(asset["filename"])
                filename = asset["filename"].casefold()
                if filename in filenames:
                    raise ValueError("Two selected items have the same Studio filename")
                filenames.add(filename)
                if db.execute("""SELECT 1 FROM jobs WHERE asset_id=? AND task='analysis'
                    AND state IN ('queued','running','cancelling')""", (aid,)).fetchone():
                    raise ValueError("A selected item already has an active analysis job")
            now = time.time()
            for aid in ids:
                job = db.execute("INSERT INTO jobs(asset_id,state,options,created,updated) VALUES(?,'queued',?,?,?)",
                                 (aid, json.dumps(opts), now, now))
                queued.append({"id": job.lastrowid})
        return {"jobs": queued}

    def claim_studio_batch(self, batch):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM settings WHERE key='paused' AND value='true'").fetchone():
                return []
            rows = db.execute("""SELECT j.* FROM jobs j JOIN assets a ON a.id=j.asset_id
                WHERE j.state='queued' AND j.task='analysis' AND a.provider='insta360'
                  AND CASE WHEN json_valid(j.options) THEN json_extract(j.options,'$.studio_batch') END=?
                ORDER BY j.id LIMIT ?""", (batch, STUDIO_BATCH_LIMIT + 1)).fetchall()
            if len(rows) > STUDIO_BATCH_LIMIT:
                raise ValueError("Studio batch exceeds its job limit")
            db.executemany("UPDATE jobs SET state='running',stage='Preparing Studio batch',updated=? WHERE id=?",
                           [(time.time(), row["id"]) for row in rows])
            return [dict(row) for row in rows]

    def enqueue_retrieval(self, aid, destination="", reference=None):
        from .retrieval import destination_folder, requested_reference
        reference = reference or {}
        asset = self.get(aid, include_scenes="scene_key" in reference or "part_filename" in reference)
        requested_reference(asset, reference)
        folder = str(destination_folder(self.root, destination)) if destination else ""
        options = {"destination": folder}
        if reference:
            options["reference"] = reference
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id,options FROM jobs WHERE asset_id=? AND task='retrieve' AND state IN ('queued','running','cancelling')", (aid,)).fetchone()
            if existing:
                active = json.loads(existing["options"])
                if active.get("destination", "") != folder:
                    raise ValueError("A retrieval for this source is already active with a different destination")
                if active.get("reference", {}) != reference:
                    raise ValueError("A retrieval for this source is already active with a different clip reference")
                return {"id": existing["id"], "existing": True}
            now = time.time()
            job = db.execute("INSERT INTO jobs(asset_id,state,options,created,updated,task) VALUES(?,'queued',?,?,?,'retrieve')", (aid, json.dumps(options), now, now))
            return {"id": job.lastrowid, "task": "retrieve"}

    def enqueue_speech(self, aid, language="auto", model="whisper-base", prefer_cloud=False, replace=False):
        if language not in ("auto", "en", "ml", "hi", "ta", "te", "kn", "ko"):
            raise ValueError("Unsupported language")
        if (not isinstance(model, str) or not model or len(model) > 80
                or not isinstance(prefer_cloud, bool) or not isinstance(replace, bool)):
            raise ValueError("Supply a local speech model and boolean cloud and replacement preferences")
        opts = {"language": language, "model": model, "prefer_cloud": prefer_cloud}
        if replace:
            opts["replace"] = True
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if row is None:
                raise ValueError("Media item not found")
            require_speech_retry(self.decode(row), replace)
            existing = db.execute("SELECT id,options FROM jobs WHERE asset_id=? AND task='speech' "
                                  "AND state IN ('queued','running','cancelling')", (aid,)).fetchone()
            if existing:
                if json.loads(existing["options"]) != opts:
                    raise ValueError("A speech retry is already active with different options")
                return {"id": existing["id"], "existing": True, "task": "speech"}
            now = time.time()
            cur = db.execute("INSERT INTO jobs(asset_id,state,options,created,updated,task) VALUES(?,'queued',?,?,?,'speech')",
                             (aid, json.dumps(opts), now, now))
            return {"id": cur.lastrowid, "task": "speech"}

    def job(self, jid):
        with self.connect() as db:
            row = db.execute(f"""SELECT j.*,a.filename,
                (j.state='queued' AND {NEEDS_SELECTION}) IS TRUE AS waiting_for_selection,
                {RECOVERED_BY} AS recovered_by
                {JOB_SOURCE} WHERE j.id=?""", (int(jid),)).fetchone()
            if not row:
                raise ValueError("Job not found")
            return dict(row, result=json.loads(row["result"]),
                        waiting_for_selection=bool(row["waiting_for_selection"]))

    def job_result(self, jid, result):
        with self.connect() as db:
            db.execute("UPDATE jobs SET result=?,updated=? WHERE id=?", (json.dumps(result, ensure_ascii=False), time.time(), int(jid)))

    def record_handoff(self, jid, asset, result, locator_patch):
        """Record the published editor file and its job receipt in one transaction."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT local_path,locator FROM assets WHERE id=?", (asset["id"],)).fetchone()
            if row is None:
                raise ValueError("Media item no longer exists")
            now = time.time()
            link_matches_index = result.get("indexed_source_verification") != "different_bytes"
            if not result["existing_original_reused"] and link_matches_index and row["local_path"] == asset["local_path"]:
                locator = json.loads(row["locator"])
                for key, path in locator_patch.items():
                    if path is None:
                        locator.pop(key, None)
                    else:
                        locator[key] = path
                db.execute("UPDATE assets SET local_path=?,locator=?,updated=? WHERE id=?",
                           (result["path"], json.dumps(locator), now, asset["id"]))
            receipt = db.execute(
                "UPDATE jobs SET result=?,updated=? WHERE id=? AND asset_id=? AND task='retrieve' "
                "AND state IN ('running','cancelling')",
                (json.dumps(result, ensure_ascii=False), now, jid, asset["id"]),
            )
            if receipt.rowcount != 1:
                raise ValueError("Editing retrieval is no longer active")

    def next_job(self):
        rows = self.next_jobs(1)
        return rows[0] if rows else None

    def next_jobs(self, limit=QUEUE_LOOKAHEAD):
        with self.connect() as db:
            rows = db.execute(f"""SELECT j.*,a.kind AS asset_kind,a.provider AS asset_provider,
                    {ASSET_PICKER_SESSION} AS picker_session,
                    CASE WHEN json_valid(j.options) THEN json_extract(j.options,'$.studio_batch') END AS studio_batch,
                    CASE WHEN json_valid(j.result) THEN json_extract(j.result,'$.rejected_pair_limit') END AS rejected_pair_limit,
                    {ANALYSIS_DEADLINE} AS analysis_deadline
                {JOB_SOURCE}
                WHERE j.state='queued' AND {NEEDS_SELECTION} IS NOT TRUE
                ORDER BY {JOB_PRIORITY},j.id LIMIT ?""", (max(1, min(QUEUE_LOOKAHEAD, int(limit))),))
            return [dict(row) for row in rows]

    def claim_job(self, jid, *, respect_pause=False):
        with self.connect() as db:
            return bool(db.execute(f"""UPDATE jobs SET state='running',stage='Resolving source',updated=?
                WHERE id=(SELECT j.id {JOB_SOURCE}
                    WHERE j.id=? AND j.state='queued' AND {NEEDS_SELECTION} IS NOT TRUE)
                AND (NOT ? OR COALESCE((SELECT value FROM settings WHERE key='paused'),'false')<>'true')""",
                (time.time(), int(jid), respect_pause)).rowcount)

    def defer_job(self, jid, *, rejected_pair_limit=0):
        with self.connect() as db:
            db.execute("""UPDATE jobs SET state=CASE state WHEN 'cancelling' THEN 'cancelled' ELSE 'queued' END,
                result=CASE WHEN ?>0 AND json_valid(result) THEN json_set(result,'$.rejected_pair_limit',
                    max(?,coalesce(json_extract(result,'$.rejected_pair_limit'),0))) ELSE result END,
                stage='',updated=? WHERE id=? AND state IN ('running','cancelling')""",
                (rejected_pair_limit, rejected_pair_limit, time.time(), int(jid)))

    def defer_picker_acquisition(self, jid, failed_session):
        """Keep a failed transfer queued when access is lost or already replaced."""
        with self.connect() as db:
            return bool(db.execute(f"""UPDATE jobs SET state='queued',stage='',error='',updated=?
                WHERE id=(SELECT j.id {JOB_SOURCE} WHERE j.id=? AND j.state='running'
                    AND a.provider='google_photos' AND ({NEEDS_SELECTION} IS TRUE
                    OR COALESCE(({ASSET_PICKER_SESSION}),'')<>?))""",
                (time.time(), jid, failed_session)).rowcount)

    def jobs(self):
        with self.connect() as db:
            rows = db.execute(f"""SELECT j.*,a.filename,
                (j.state='queued' AND {NEEDS_SELECTION}) IS TRUE AS waiting_for_selection,
                {RECOVERED_BY} AS recovered_by
                {JOB_SOURCE} ORDER BY CASE
                    WHEN j.state='running' THEN 0
                    WHEN j.task='retrieve' AND j.state='done' OR j.state='cancelling' THEN 1
                    WHEN j.state='failed' AND recovered_by IS NULL THEN 2
                    WHEN j.state='queued' AND NOT waiting_for_selection THEN 3
                    WHEN j.state='queued' THEN 4 ELSE 5 END,
                    {JOB_PRIORITY},CASE WHEN j.state='queued' THEN j.id ELSE -j.id END LIMIT 100""")
            return [dict(x, result=json.loads(x["result"]),
                         waiting_for_selection=bool(x["waiting_for_selection"])) for x in rows]

    def queue_state(self, paused=None, *, include_waiting=False):
        with self.connect() as db:
            db.execute("BEGIN")
            if paused is not None:
                if not isinstance(paused, bool):
                    raise ValueError("paused must be boolean")
                db.execute("INSERT INTO settings VALUES('paused',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(paused),))
            row = db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
            counts = {x[0]: x[1] for x in db.execute("SELECT state,count(*) FROM jobs GROUP BY state")}
            result = {"paused": json.loads(row[0]) if row else False, "counts": counts}
            if include_waiting:
                result["unresolved_failures"] = db.execute(f"""SELECT count(*) FROM jobs j
                    WHERE j.state='failed' AND ({RECOVERED_BY}) IS NULL""").fetchone()[0]
                result["waiting_for_selection"] = db.execute(f"""SELECT count(*) {JOB_SOURCE}
                    WHERE j.state='queued' AND {NEEDS_SELECTION} IS TRUE""").fetchone()[0]
            return result

    def storage_limit(self, gib=None):
        if gib is not None and (not isinstance(gib,int) or isinstance(gib,bool) or gib not in STORAGE_LIMITS_GIB):
            raise ValueError('Choose a temporary storage limit of 2, 4, 8, 16 or 32 GiB')
        with self.connect() as db:
            if gib is not None:
                db.execute("INSERT INTO settings VALUES('storage_limit_gib',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(gib),))
            row=db.execute("SELECT value FROM settings WHERE key='storage_limit_gib'").fetchone()
            value=json.loads(row[0]) if row else 2
        if not isinstance(value,int) or isinstance(value,bool) or value not in STORAGE_LIMITS_GIB:
            raise ValueError('Saved storage limit is invalid; choose 2, 4, 8, 16 or 32 GiB again')
        return value*1024**3

    def cancel(self, job_id):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'cancelling' END,updated=? WHERE id=? AND state IN ('queued','running')", (time.time(), int(job_id)))
        return {"ok": True}
