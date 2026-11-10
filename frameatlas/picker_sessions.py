"""Durable selection-batch receipts, without tokens, Picker URLs or media URLs."""
import datetime
import json
import math
import re
import uuid
import time
from collections import Counter, defaultdict

from .store import ASSET_PICKER_SESSION, JOB_SOURCE, NEEDS_CLOUD, NEEDS_SELECTION, PICKER_EXPIRED


def session_id(value):
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError('Use a valid Google Picker session ID')
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ValueError('Use a valid Google Picker session ID')
    if parsed != value:
        raise ValueError('Use a valid Google Picker session ID')
    return value


def timestamp(value):
    if value in (None, ''):
        return ''
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError('Invalid selection-batch timestamp')
    try:
        parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Timestamp needs a timezone')
        return parsed.astimezone(datetime.timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise ValueError('Invalid selection-batch timestamp')


def capture_counts(value, selected_count):
    if not isinstance(value, dict) or len(value) > 2000:
        raise ValueError('Invalid capture-month counts')
    if any(not isinstance(month, str) or not re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])', month)
           or type(count) is not int or not 1 <= count <= 2000 for month, count in value.items()):
        raise ValueError('Invalid capture-month counts')
    if sum(value.values()) > selected_count:
        raise ValueError('Capture-month counts exceed the selected item count')
    return dict(sorted(value.items()))


def record(store, sid, *, expires=None, selected_count=None, added_count=0,
           captured_min='', captured_max='', capture_months=None, closed=False, db=None):
    sid = session_id(sid)
    expiry = timestamp(expires) if expires is not None else None
    low, high = timestamp(captured_min), timestamp(captured_max)
    if low and high and datetime.datetime.fromisoformat(low) > datetime.datetime.fromisoformat(high):
        raise ValueError('Selection-batch date bounds are reversed')
    if selected_count is not None and (type(selected_count) is not int or not 0 <= selected_count <= 2000):
        raise ValueError('A Google Picker batch has at most 2,000 selected items')
    if type(added_count) is not int or not 0 <= added_count <= (selected_count or 0):
        raise ValueError('Invalid new-item count for selection batch')
    if not isinstance(closed, bool):
        raise ValueError('closed must be boolean')
    months = json.dumps(capture_counts(capture_months, selected_count or 0)) if capture_months is not None else None
    if db is None:
        with store.connect() as connection:
            return record(store, sid, expires=expiry, selected_count=selected_count, added_count=added_count,
                          captured_min=low, captured_max=high, capture_months=capture_months, closed=closed, db=connection)
    now = time.time()
    state = 'closed' if closed else ('selected' if selected_count is not None else 'pending')
    db.execute('''INSERT INTO picker_sessions
        (id,expires,state,selected_count,added_count,captured_min,captured_max,capture_months,created,updated)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        expires=COALESCE(?,picker_sessions.expires),
        state=CASE WHEN ? THEN 'closed' WHEN ? IS NOT NULL THEN 'selected' ELSE picker_sessions.state END,
        selected_count=CASE WHEN ? IS NOT NULL THEN excluded.selected_count ELSE picker_sessions.selected_count END,
        added_count=CASE WHEN ? IS NOT NULL THEN MAX(picker_sessions.added_count,excluded.added_count) ELSE picker_sessions.added_count END,
        captured_min=CASE WHEN ? IS NOT NULL THEN excluded.captured_min ELSE picker_sessions.captured_min END,
        captured_max=CASE WHEN ? IS NOT NULL THEN excluded.captured_max ELSE picker_sessions.captured_max END,
        capture_months=COALESCE(?,picker_sessions.capture_months),
        updated=excluded.updated''',
        (sid, expiry or '', state, selected_count or 0, added_count, low, high, months or '{}', now, now,
         expiry, closed, selected_count, selected_count, selected_count, selected_count, selected_count, months))


def _media_counts(total, photos, videos):
    return {'photo': photos, 'video': videos, 'other': total-photos-videos}


def list_batches(store):
    now = time.time()
    with store.connect() as db:
        db.execute('BEGIN')
        total = db.execute('SELECT COUNT(*) FROM picker_sessions').fetchone()[0]
        rows = db.execute('''WITH linked AS (
            SELECT json_extract(locator,'$.session_id') AS session_id,count(*) AS assets,
                   count(CASE WHEN status IN ('ready','partial') THEN 1 END) AS analysed
            FROM assets WHERE provider='google_photos' GROUP BY session_id)
            SELECT p.*,coalesce(linked.assets,0) AS linked_assets,
                   coalesce(linked.analysed,0) AS analysed_assets
            FROM picker_sessions p LEFT JOIN linked ON linked.session_id=p.id
            ORDER BY p.state='closed',p.created DESC LIMIT 200''').fetchall()
        jobs = db.execute(f'''WITH pending AS (
            SELECT p.id,p.state,p.expires,a.kind,j.state='queued' AND {NEEDS_CLOUD} AS cloud
            {JOB_SOURCE} WHERE p.id IS NOT NULL AND j.state IN ('queued','running','cancelling'))
            SELECT id,state,expires,count(*) AS pending,
                count(CASE WHEN cloud THEN 1 END) AS cloud_queued,
                count(CASE WHEN cloud AND kind='photo' THEN 1 END) AS photos,
                count(CASE WHEN cloud AND kind='video' THEN 1 END) AS videos
            FROM pending
            GROUP BY id''').fetchall()
        completed, picker, local, picker_photos, picker_videos = db.execute('''SELECT count(*),
            count(CASE WHEN source='picker' THEN 1 END), count(CASE WHEN source='local' THEN 1 END),
            count(CASE WHEN source='picker' AND kind='photo' THEN 1 END),
            count(CASE WHEN source='picker' AND kind='video' THEN 1 END)
            FROM (SELECT a.kind,
                CASE WHEN json_valid(j.result) THEN json_extract(j.result,'$.analysis_source') END AS source
            FROM jobs j JOIN assets a ON a.id=j.asset_id
            WHERE a.provider='google_photos' AND j.task='analysis' AND j.state='done'
            AND j.updated>? AND j.updated<=?)''', (now-3600, now)).fetchone()

    jobs_by_batch = {row['id']: row for row in jobs}
    media_by_batch = {row['id']: _media_counts(row['cloud_queued'], row['photos'], row['videos'])
                      for row in jobs}
    deadlines = {}
    deadline_counts = defaultdict(Counter)
    for row in jobs:
        if row['state'] != 'selected' or not row['cloud_queued'] or not row['expires']:
            continue
        try:
            deadline = datetime.datetime.fromisoformat(timestamp(row['expires'])).timestamp()
        except ValueError:
            continue
        if deadline > now:
            deadlines[row['id']] = deadline
            deadline_counts[deadline].update(media_by_batch[row['id']])
    cumulative = Counter()
    pace = {}
    peak_by_kind = _media_counts(0, 0, 0)
    for deadline, count in sorted(deadline_counts.items()):
        cumulative.update(count)
        total_due = sum(cumulative.values())
        remaining = deadline - now
        by_kind = {kind: math.ceil(n * 3600 / remaining) for kind, n in cumulative.items()}
        for kind, rate in by_kind.items():
            peak_by_kind[kind] = max(peak_by_kind[kind], rate)
        pace[deadline] = {'cloud_jobs_due': total_due,
                          'cloud_jobs_due_by_kind': dict(cumulative),
                          'minimum_jobs_per_hour': math.ceil(total_due * 3600 / remaining),
                          'minimum_jobs_per_hour_by_kind': by_kind}

    items = []
    for row in rows:
        item = dict(row)
        try:
            expiry = timestamp(item['expires'])
            item['expired'] = bool(expiry and datetime.datetime.fromisoformat(expiry).timestamp() <= now)
        except ValueError:
            item['expired'] = None
        try:
            item['capture_months'] = capture_counts(json.loads(item['capture_months']), item['selected_count'])
        except (TypeError, ValueError):
            item['capture_months'] = {}
        counts = jobs_by_batch.get(item['id'])
        item['pending_jobs'] = counts['pending'] if counts else 0
        item['cloud_queued_jobs'] = counts['cloud_queued'] if counts else 0
        item['cloud_queued_jobs_by_kind'] = media_by_batch.get(item['id'], _media_counts(0, 0, 0))
        item['deadline_load'] = pace.get(deadlines.get(item['id']))
        item['can_close'] = item['state'] != 'closed' and item['pending_jobs'] == 0
        items.append(item)
    return {'items': items, 'total': total, 'truncated': total > len(items),
            'recent_google_analysis_completions': completed,
            'recent_google_picker_analysis_completions': picker,
            'recent_google_picker_analysis_by_kind': _media_counts(picker, picker_photos, picker_videos),
            'peak_minimum_jobs_per_hour_by_kind': peak_by_kind,
            'recent_google_local_analysis_completions': local,
            'recent_google_unclassified_analysis_completions': completed-picker-local,
            'recent_window_seconds': 3600,
            'coverage': 'Selected batches only. Linked items may move to a newer selection; earlier receipts remain.'}


def media_page(limit, offset, max_offset):
    bounds = []
    for value, minimum, maximum in ((limit, 1, 100), (offset, 0, max_offset)):
        if isinstance(value, str) and re.fullmatch(r'[0-9]{1,8}', value):
            value = int(value)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f'Use a limit of 1–100 and an offset of 0–{max_offset}')
        bounds.append(value)
    return bounds


def queued_items(store, sid, limit=25, offset=0):
    """List distinct queued sources whose current locator depends on this selection."""
    sid = session_id(sid)
    limit, offset = media_page(limit, offset, 2000)
    where = f"{JOB_SOURCE} WHERE p.id=? AND j.state='queued' AND {NEEDS_CLOUD}"
    with store.connect() as db:
        db.execute('BEGIN')
        batch = db.execute(f"""SELECT p.state,p.expires,
            (p.state='closed' OR {PICKER_EXPIRED}) IS TRUE AS needs_reselection
            FROM picker_sessions p WHERE p.id=?""", (sid,)).fetchone()
        if batch is None:
            raise ValueError('Selection batch is not in the catalogue')
        total, jobs = db.execute(f'SELECT count(DISTINCT a.id),count(*) {where}', (sid,)).fetchone()
        items = db.execute(f"""SELECT a.id,a.filename,a.captured,a.kind,count(*) AS queued_jobs
            {where} GROUP BY a.id ORDER BY julianday(a.captured) IS NULL,julianday(a.captured),a.id
            LIMIT ? OFFSET ?""", (sid, limit, offset)).fetchall()
    return {'session': sid, 'state': batch['state'], 'expires': batch['expires'],
            'needs_reselection': bool(batch['needs_reselection']), 'total': total, 'queued_jobs': jobs,
            'offset': offset, 'limit': limit, 'has_more': offset + len(items) < total,
            'items': [dict(item) for item in items]}


def reselection_items(store, limit=25, offset=0):
    """Find waiting sources even when their selection is no longer attached."""
    limit, offset = media_page(limit, offset, 10_000_000)
    where = f"{JOB_SOURCE} WHERE j.state='queued' AND {NEEDS_SELECTION} IS TRUE"
    with store.connect() as db:
        db.execute('BEGIN')
        total, jobs = db.execute(f'SELECT count(DISTINCT a.id),count(*) {where}').fetchone()
        items = db.execute(f"""SELECT a.id,a.filename,a.captured,a.kind,count(*) AS queued_jobs,
            p.id AS session,p.expires,
            CASE WHEN COALESCE(({ASSET_PICKER_SESSION}),'')='' THEN 'missing'
                 WHEN p.state IN ('closed','pending') THEN p.state ELSE 'expired' END AS reason
            {where} GROUP BY a.id ORDER BY julianday(a.captured) IS NULL,julianday(a.captured),a.id
            LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
    return {'needs_reselection': bool(total), 'total': total, 'queued_jobs': jobs,
            'offset': offset, 'limit': limit, 'has_more': offset + len(items) < total,
            'items': [dict(item) for item in items]}
