"""Cross-store marker counts and revision-bound evidence from existing derived caches."""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone as dt_timezone
import hashlib
from itertools import groupby
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .evidence import redact_text
from .markers import MARKER_EVENTS, MARKER_SOURCES, MAX_METADATA_CHARS, indexed_time, marker_rows, time_window_sql
from .marker_review import assess, route_requests
from .sources import _indexed_public_message_sql


def _window(start, end, timezone):
    zone = ZoneInfo(timezone)
    def epoch(value):
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zone)
        delta = dt - datetime(1970, 1, 1, tzinfo=dt_timezone.utc)
        return (delta.days * 86400 + delta.seconds) * 1000 + (delta.microseconds + 999) // 1000
    lo, hi = epoch(start), epoch(end)
    if lo >= hi:
        raise ValueError('invalid_marker_window')
    return lo, hi


def _filters(value):
    return {s.strip() for s in value.split(',') if s.strip()} if isinstance(value, str) else set(value)


def _supported(source, kind, event):
    if source not in MARKER_SOURCES or (event is not None and event not in MARKER_EVENTS):
        return False
    if event == 'context_injected' and kind == 'skill':
        return False
    if event and event != 'context_injected' and kind == 'output_style':
        return False
    if source == 'antigravity':
        return kind != 'output_style' and event in (None, 'skill_file_read')
    return event != 'skill_file_read'


def _family(source, sid):
    return (source, sid.split('@recording-', 1)[0] if source == 'codex' else sid)


def _event_key(item):
    ref = item['raw_ref']
    if item['call_id']:
        position = ('call', item['call_id'], ref.get('json_pointer'))
    elif item['source'] == 'antigravity':
        position = ('step', ref.get('record_id'), ref.get('json_pointer'), item['activity_ts_ms'])
    else:
        position = ('message', ref.get('record_id'), ref.get('line_no', item['message_index']),
                    ref.get('json_pointer'), item['activity_ts_ms'], item['marker_index'])
    return (*_family(item['source'], item['session_id']), item['kind'], item['event'], item['name'],
            item['path'], item['version'], item['content_hash'], position)


def _counts(active, observable, seen, total, supported=True):
    denominator = len(observable) if supported else None
    numerator = len(observable & seen) if supported else None
    return dict(known_active_sessions=len(active), observable_active_sessions=denominator,
                sessions_with_event=numerator, sessions_with_observed_evidence=len(seen),
                rate=numerator / denominator if denominator else None, total_events=total if supported else None,
                coverage_unknown_active_sessions=len(active - observable))


def _active(conn, store, tables, window):
    sql = f"SELECT DISTINCT m.session_id FROM messages m WHERE {time_window_sql(store['source'])} AND {_indexed_public_message_sql()}"
    active = {r[0] for r in conn.execute(sql, dict(lo=window[0], hi=window[1]))}
    if 'agy_transcript_rows' in tables:
        active.update(r[0] for r in conn.execute('SELECT DISTINCT session_id FROM agy_transcript_rows WHERE ts_ms>=? AND ts_ms<?', window))
    return active


def _requests(conn, store, tables, window, preset):
    """Use the shared public-role predicate. Only explicit review reads user text."""
    conn.create_function('tm_redact_user', 1, redact_text, deterministic=True)
    body = 'CASE WHEN length(m.text)<=65536 THEN tm_redact_user(m.text) END'
    stmt = f'''SELECT m.session_id,{indexed_time(store['source'])} AS ts,{body} AS body,
        (SELECT COUNT(*) FROM messages p WHERE p.session_id=m.session_id AND (p.ts_ms,p.id)<(m.ts_ms,m.id)) AS ordinal,
        CASE WHEN length(activity_meta_json)<=65536 AND json_valid(activity_meta_json)
             THEN json_extract(activity_meta_json,'$.raw_ref') END AS raw_ref
        FROM messages m WHERE {time_window_sql(store['source'])} AND m.role='user' AND {_indexed_public_message_sql()}'''
    args = dict(lo=window[0], hi=window[1])
    if 'agy_transcript_rows' in tables:
        # Prefer transcript step locators for IDE requests when available.
        stmt += """ AND NOT EXISTS (SELECT 1 FROM agy_transcript_rows t WHERE t.session_id=m.session_id
                                    AND t.role='user' AND t.ts_ms=m.activity_ts_ms)
                   UNION ALL SELECT m.session_id,m.ts_ms,""" + body + """,m.message_index,
                       json_extract(m.activity_meta_json,'$.raw_ref') FROM agy_transcript_rows m
                   WHERE m.ts_ms>=:lo AND m.ts_ms<:hi AND m.role='user'"""
    result, omitted = {}, set()
    for row in conn.execute(stmt + ' ORDER BY ts,ordinal', args):
        sid = row['session_id']
        if row['body'] is None:
            omitted.add(sid)
            continue
        for name, hit in route_requests(row['body'], preset):
            raw = json.loads(row['raw_ref'] or '{}')
            ref = {}
            if isinstance(raw, dict):
                if type(raw.get('line_no')) is int and raw['line_no'] > 0:
                    ref['line_no'] = raw['line_no']
                for field in ('record_id', 'json_pointer'):
                    if isinstance(raw.get(field), str):
                        ref[field] = redact_text(raw[field])[:512]
            loc = dict(activity_ts_ms=row['ts'], message_index=row['ordinal'], raw_ref=ref, **hit)
            entry = result.setdefault((sid, name), dict(first=loc, explicit=False))
            entry['explicit'] |= hit['explicit']
            if hit['reminder']:
                entry.setdefault('reminder', loc)
    return result, omitted


def _read_store(conn, scratch, store, window, preset, kind, event, report):
    source = store['source']
    conn.execute('BEGIN')
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    state = dict(conn.execute('SELECT key,value FROM reader_state')) if 'reader_state' in tables else {}
    report['revision'] = state.get('revision')
    errors = None
    if 'capture_errors' in tables:
        errors = {r[0]: redact_text(r[1])[:300] for r in conn.execute('SELECT path,error FROM capture_errors ORDER BY path')}
    if state.get('activity_refresh_error'):
        report['previous_refresh_error'] = redact_text(state['activity_refresh_error'])[:300]
    report.update(capture_errors=errors, capture_error_status='recorded' if errors is not None else 'not_recorded')
    columns = {r[1] for r in conn.execute('PRAGMA table_info(messages)')}
    if not {'activity_ts_ms', 'activity_meta_json'} <= columns:
        report['coverage'] = 'legacy_time_or_metadata_unavailable'
        return set(), {}, []
    active = _active(conn, store, tables, window)
    supports = source in MARKER_SOURCES
    if not supports:
        report['coverage'] = 'unsupported'
        return active, {}, []
    transcripts = {}
    if 'agy_transcript_state' in tables:
        transcripts = {r[0]: json.loads(r[1]) for r in conn.execute('SELECT session_id,coverage_json FROM agy_transcript_state')}
        errors = {sid: v.get('error', 'capture_error') for sid, v in transcripts.items() if v['status'] == 'capture_error'}
        report.update(capture_errors=errors, capture_error_status='recorded')
        report['transcript_coverage'] = dict(Counter(v['status'] for v in transcripts.values()))
    needed = active.copy()
    uncertain, uncertain_skills = set(), set()
    unknown_times = 0
    for item in marker_rows(conn, source, unknown_time=True):
        unknown_times += 1
        if item['kind'] == kind and (event is None or item['event'] == event):
            uncertain.add(item['session_id'])
        if item['kind'] == 'skill':
            uncertain_skills.add((item['session_id'], item['name']))
    report['unknown_time_events'] = (None if source == 'antigravity' and 'agy_transcript_rows' not in tables
                                    else unknown_times)
    for item in marker_rows(conn, source, window=window):
        if _supported(source, item['kind'], item['event']):
            needed.add(item['session_id'])
            item.update(store_key=store['store_key'], slot=store['slot'])
            key = json.dumps(_event_key(item), ensure_ascii=False)
            scratch.execute('INSERT INTO occurrences(event_key,store_key,slot,session_id,native_id,kind,event,item) '
                            'VALUES(?,?,?,?,?,?,?,?)', (key, store['store_key'], store['slot'], item['session_id'],
                            _family(source, item['session_id'])[1], item['kind'], item['event'], json.dumps(item)))
    bad_meta = dict(conn.execute(
        f'SELECT session_id,count(*) FROM messages m WHERE {time_window_sql(source)} '
        'AND (length(activity_meta_json)>:cap OR (activity_meta_json IS NOT NULL AND NOT json_valid(activity_meta_json))) '
        'GROUP BY session_id', dict(lo=window[0], hi=window[1], cap=MAX_METADATA_CHARS)))
    report['omitted_metadata_rows'] = sum(bad_meta.values())
    copies = {}
    for r in conn.execute('SELECT id,cwd,file_path,parser_version,activity_metadata_json FROM sessions'):
        sid = r['id']
        if sid not in needed:
            continue
        capture = json.loads(r['activity_metadata_json'] or '{}').get('source_capture') or {}
        if source == 'antigravity':
            meta = transcripts.get(sid, {})
            status = meta.get('status', 'transcript_not_refreshed')
        else:
            meta = capture
            status = 'captured'
            if (r['parser_version'] or 0) < (8 if source == 'codex' else 6):
                status = 'legacy_parser'
            elif capture.get('partial') or capture.get('index_coverage') == 'partial':
                status = 'partial'
            if sid in bad_meta:
                status = 'incomplete_metadata'
            if errors and str(Path(r['file_path']).relative_to(store['origin_path'])
                              if Path(r['file_path']).is_relative_to(store['origin_path']) else r['file_path']) in errors:
                status = 'capture_error'
        if status == 'captured' and sid in uncertain:
            status = 'unknown_event_time'
        copies[sid] = dict(store_key=store['store_key'], session_id=sid, slot=store['slot'],
                           revision=report['revision'], prefix_hash=meta.get('prefix_hash'),
                           transcript_mtime_ns=meta.get('transcript_mtime_ns'),
                           captured_through=meta.get('captured_through'), coverage=status, active=sid in active)
    scratch.executemany('UPDATE occurrences SET covered=?,eligible=?,revision_ns=?,through=? WHERE store_key=? AND session_id=?',
                        ((c['coverage'] == 'captured', c['coverage'] == 'captured' and c['active'],
                          c['transcript_mtime_ns'] or 0, c['captured_through'] or 0, store['store_key'], sid)
                         for sid, c in copies.items()))
    report['coverage'] = ('transcript_not_refreshed' if source == 'antigravity' and 'agy_transcript_state' not in tables
                          else 'recorded_prefixes' if state else 'legacy_index')
    report['active_coverage'] = dict(Counter(c['coverage'] for c in copies.values() if c['active']))
    reviews = []
    if preset:
        requests, omitted = _requests(conn, store, tables, window, preset)
        report['review_oversized_user_sessions'] = len(omitted)
        prior = {}
        # Include prior evidence as context, so a repeated call after a reminder
        # cannot impersonate a first call. No post-window events enter the review.
        for sid in {sid for sid, _ in requests}:
            for e in marker_rows(conn, source, session_id=sid, kind='skill', recorded_time=True):
                if e['activity_ts_ms'] is not None and e['activity_ts_ms'] < window[1]:
                    key = (sid, e['name'])
                    old = prior.get(key)
                    if key in requests and (old is None or _event_order(e) < _event_order(old)):
                        prior[key] = e
        for (sid, name), request in requests.items():
            copy = copies.get(sid, {})
            visible = (copy.get('coverage') in ('captured', 'unknown_event_time') and sid not in omitted
                       and (sid, name) not in uncertain_skills)
            review = assess(name, request, prior.get((sid, name)), visible)
            reviews.append(dict(source=source, session_id=sid, store_key=store['store_key'], slot=store['slot'],
                                **review))
    return active, copies, reviews


def _event_order(item):
    return item['activity_ts_ms'], item['raw_ref'].get('line_no', 0), item['marker_index']


def _refresh(store, data_dir):
    from .reader import HistoryReader
    source, origin = store['source'], Path(store['origin_path'])
    if source not in MARKER_SOURCES or store['is_archive']:
        return 'unsupported'
    cache = Path(store['db_path']).parent
    expected = 'machine-' + source + '-' + hashlib.sha256(str(origin.resolve()).encode()).hexdigest()
    if cache.name != expected or not origin.exists():
        return 'origin_unavailable'
    mode = store['capture_mode']
    with HistoryReader(source, origin, data_dir or cache.parent, validate_tree=False, capture_mode=mode) as reader:
        receipt = reader.refresh(max_bytes=256 * 1024 * 1024) if mode == 'projected' else reader.refresh()
    return receipt['status']


def omni_markers(*, start, end, timezone='Asia/Shanghai', sources='all', slots='all', kind='skill', event=None,
                 group_by='skill,source,event', refresh=False, review_misses=None, limit=100, offset=0,
                 index_revision=None, data_dir=None, demo=False):
    # An empty SQLite filename creates a private, auto-deleted temporary DB.
    # Stores remain read-only. Only the selected page is materialized for output.
    with closing(sqlite3.connect('')) as scratch:
        scratch.row_factory = sqlite3.Row
        scratch.executescript('''
            CREATE TABLE occurrences(event_key,store_key,slot,session_id,native_id,kind,event,item,
                                     covered INTEGER DEFAULT 0,eligible INTEGER DEFAULT 0,revision_ns INTEGER DEFAULT 0,
                                     through INTEGER DEFAULT 0);
            CREATE INDEX by_store_session ON occurrences(store_key,session_id);
            CREATE INDEX by_event ON occurrences(event_key,covered DESC,revision_ns DESC,through DESC);
            CREATE TABLE pages(chosen INTEGER,ts INTEGER,source,native_id,store_key,line INTEGER,pointer,idx INTEGER);
        ''')
        return _aggregate(scratch, start, end, timezone, sources, slots, kind, event, group_by, refresh,
                          review_misses, limit, offset, index_revision, data_dir, demo)


def _aggregate(scratch, start, end, timezone, sources, slots, kind, event, group_by, refresh,
               review_misses, limit, offset, index_revision, data_dir, demo):
    from .omni import _ro_connect, discover_omni_stores
    window = _window(start, end, timezone)
    dims = [s.strip() for s in group_by.split(',')]
    if not dims or len(set(dims)) != len(dims) or not set(dims) <= {'skill','source','slot','event','kind'}:
        raise ValueError('invalid_marker_group')
    if kind not in ('skill','output_style') or (event and event not in MARKER_EVENTS):
        raise ValueError('invalid_marker_filter')
    if review_misses not in (None, 'all', 'studio') or not 1 <= limit <= 1000 or offset < 0:
        raise ValueError('invalid_marker_options')
    if offset and not index_revision:
        raise ValueError('index_revision_required: restart from offset 0')
    wanted, selected = _filters(sources), _filters(slots)
    stores = [s for s in discover_omni_stores(data_dir, demo=demo, include_archives=True, marker_stores=True, cache_only=True)
              if ('all' in wanted or s['source'] in wanted)
              and ('all' in selected or selected.intersection([s['slot'], *s['slot_aliases']]))]
    reports, families, reviewed = [], defaultdict(list), {}
    known, observable = set(), set()
    scope_active, scope_observable = defaultdict(set), defaultdict(set)
    for store in stores:
        source, slot = store['source'], store['slot']
        report = {k: store[k] for k in ('store_key', 'source', 'slot', 'slot_aliases', 'origin_path', 'capture_mode', 'is_archive')}
        report.update(revision=None, freshness='archived' if store['is_archive'] else 'unknown', coverage=store['cache_status'], capture_errors=None,
                      capture_error_status='not_recorded', known_active_sessions=None, observable_active_sessions=None,
                      sessions_with_event=None, total_events=None, rate=None)
        reports.append(report)
        if store['kind'] != 'indexed':
            report['coverage'] = 'unsupported'
            continue
        try:
            if refresh:
                report['refresh_status'] = _refresh(store, data_dir)
                if report['refresh_status'] in ('refreshed', 'partial'):
                    report['freshness'] = report['refresh_status']
            if not Path(store['db_path']).is_file():
                continue
            with scratch, closing(_ro_connect(Path(store['db_path']))) as conn:
                active, copies, reviews = _read_store(conn, scratch, store, window, review_misses, kind, event, report)
        except (ValueError, OSError, sqlite3.Error) as exc:
            report.update(coverage='store_error', error=redact_text(str(exc))[:300])
            continue
        active_ids = {_family(source, sid) for sid in active}
        eligible = _supported(source, kind, event)
        ok = {_family(source, sid) for sid, c in copies.items() if c['active'] and c['coverage'] == 'captured' and eligible}
        known.update(active_ids)
        observable.update(ok)
        for sid, copy in copies.items():
            families[_family(source, sid)].append(copy)
        for scope in ((source,slot),(source,None),(None,slot),(None,None)):
            scope_active[scope].update(active_ids)
            scope_observable[scope].update(ok)
        if report['coverage'] == 'legacy_time_or_metadata_unavailable':
            continue
        rows = scratch.execute('SELECT native_id,COUNT(DISTINCT event_key),MAX(eligible) FROM occurrences '
                               'WHERE store_key=? AND kind=? AND (? IS NULL OR event=?) GROUP BY native_id',
                               (store['store_key'], kind, event, event))
        local_total = local_evidence = 0
        local_eligible = set()
        for native_id, count, covered in rows:
            local_total += count
            local_evidence += 1
            if covered:
                local_eligible.add((source, native_id))
        report.update(_counts(active_ids, ok, local_eligible, local_total, eligible))
        report['sessions_with_observed_evidence'] = local_evidence
        for item in reviews:
            sid = _family(source, item['session_id'])
            def location(loc):
                if not loc:
                    return None
                return (loc['activity_ts_ms'], loc['raw_ref'].get('record_id') or loc['raw_ref'].get('line_no') or loc['message_index'])
            key = (sid, item['skill'], location(item['first_request']), location(item['reminder']),
                   item['status'], location(item['first_event']))
            reviewed.setdefault(key, {**item, 'copies': []})['copies'].append(store['store_key'])

    groups = {}
    scope_events, scope_seen, scope_evidence = Counter(), defaultdict(set), defaultdict(set)
    by_event, styles = Counter(), Counter()
    seen, observed_seen = set(), set()
    event_sessions, event_evidence = defaultdict(set), defaultdict(set)
    rows = scratch.execute('SELECT rowid,* FROM occurrences ORDER BY event_key,covered DESC,revision_ns DESC,through DESC,rowid')
    for _, occurrences in groupby(rows, key=lambda r: r['event_key']):
        chosen = next(occurrences)
        e = json.loads(chosen['item'])
        family = (e['source'], chosen['native_id'])
        slots_here = {chosen['slot']}
        eligible_slots = {chosen['slot']} if chosen['eligible'] else set()
        for copy in occurrences:
            slots_here.add(copy['slot'])
            if copy['eligible']:
                eligible_slots.add(copy['slot'])
        if e['kind'] == 'output_style':
            styles[e['event']] += 1
        if e['kind'] != kind or (event and e['event'] != event):
            continue
        observed_seen.add(family)
        event_evidence[e['event']].add(family)
        if eligible_slots:
            seen.add(family)
            event_sessions[e['event']].add(family)
        by_event[e['event']] += 1
        ref = e['raw_ref']
        scratch.execute('INSERT INTO pages VALUES(?,?,?,?,?,?,?,?)',
                        (chosen['rowid'], e['activity_ts_ms'], e['source'], family[1], e['store_key'],
                         ref.get('line_no', 0), ref.get('json_pointer', ''), e['marker_index']))
        for slot in slots_here | {None}:
            for scope in ((e['source'],slot),(None,slot)):
                scope_events[scope] += 1
                scope_evidence[scope].add(family)
                if eligible_slots and (slot is None or slot in eligible_slots):
                    scope_seen[scope].add(family)
        group_slots = slots_here if 'slot' in dims else {None}
        for slot in group_slots:
            values = {**e, 'skill':e['name'], 'slot':slot}
            bucket = groups.setdefault(tuple(values[d] for d in dims), dict(count=0, observed=set(), seen=set()))
            bucket['count'] += 1
            bucket['observed'].add(family)
            if eligible_slots and (slot is None or slot in eligible_slots):
                bucket['seen'].add(family)
    group_list = []
    for key, members in sorted(groups.items()):
        values = dict(zip(dims, key))
        scope = (values.get('source'), values.get('slot'))
        eligible_sources = {s['source'] for s in stores if _supported(s['source'], values.get('kind', kind), values.get('event', event))}
        eligible_scope = {f for f in scope_observable[scope] if f[0] in eligible_sources}
        counts = _counts(scope_active[scope], eligible_scope, members['seen'], members['count'], bool(eligible_sources))
        counts['sessions_with_observed_evidence'] = len(members['observed'])
        group_list.append({**values, **counts})
    revisions = {r['store_key']: r['revision'] for r in reports}
    revision = hashlib.sha256(json.dumps([window, sorted(wanted), sorted(selected), kind, event, dims, revisions,
                                          [(r['coverage'],r['capture_errors']) for r in reports]], sort_keys=True).encode()).hexdigest()
    if index_revision is not None and index_revision != revision:
        raise ValueError('index_revision_changed: restart from offset 0')
    details = []
    for row in scratch.execute('SELECT o.item,o.event_key,p.native_id FROM pages p JOIN occurrences o ON o.rowid=p.chosen '
                               'ORDER BY ts DESC,source,p.native_id,p.store_key,line,pointer,idx,o.event_key LIMIT ? OFFSET ?',
                               (limit, offset)):
        item = json.loads(row['item'])
        copies = scratch.execute('SELECT store_key,slot,session_id FROM occurrences WHERE event_key=? ORDER BY rowid',
                                 (row['event_key'],))
        details.append({**item, 'native_session_id': row['native_id'], 'copies': [dict(c) for c in copies]})
    total = sum(by_event.values())
    summary = _counts(known, observable, seen, total, any(_supported(s['source'], kind,event) for s in stores))
    summary['sessions_with_observed_evidence'] = len(observed_seen)
    if not any(r['known_active_sessions'] is not None for r in reports):
        summary.update(known_active_sessions=None, coverage_unknown_active_sessions=None)
    event_coverage = {}
    for label in MARKER_EVENTS:
        if (label == 'context_injected') != (kind == 'output_style') or (event and label != event):
            continue
        capable = {s['source'] for s in stores if _supported(s['source'], kind, label)}
        denominator = {f for f in observable if f[0] in capable}
        counts = _counts({f for f in known if f[0] in capable}, denominator, event_sessions[label], by_event[label], bool(capable))
        counts['sessions_with_observed_evidence'] = len(event_evidence[label])
        event_coverage[label] = counts
    branches = []
    for (src, sid), copies in sorted(families.items()):
        if len(copies) < 2:
            continue
        hashes = defaultdict(set)
        for copy in copies:
            if copy['prefix_hash']:
                hashes[copy['captured_through']].add(copy['prefix_hash'])
        status = ('divergent' if any(len(v)>1 for v in hashes.values()) else
                  'same_prefix' if len(hashes)==1 and all(c['prefix_hash'] for c in copies) else 'unverified_relationship')
        representative = max(copies, key=lambda c: (c['coverage'] == 'captured', c['transcript_mtime_ns'] or 0,
                                                     c['captured_through'] or 0))
        branches.append(dict(source=src, native_session_id=sid, relationship=status, recordings=copies,
                             representative_store=representative['store_key'],
                             representative_session=representative['session_id']))
    def scope_stats(scope, supported):
        stats = _counts(scope_active[scope], scope_observable[scope], scope_seen[scope], scope_events[scope], supported)
        stats['sessions_with_observed_evidence'] = len(scope_evidence[scope])
        if not any(r['known_active_sessions'] is not None for r in reports
                   if (scope[0] is None or r['source'] == scope[0]) and (scope[1] is None or r['slot'] == scope[1])):
            stats.update(known_active_sessions=None, coverage_unknown_active_sessions=None)
        return stats
    source_stats = {s: scope_stats((s,None), _supported(s, kind, event)) for s in sorted({s['source'] for s in stores})}
    slot_stats = {slot: scope_stats((None,slot), any(_supported(s['source'], kind, event) for s in stores if s['slot'] == slot))
                  for slot in sorted({s['slot'] for s in stores})}
    out = dict(schema_version='history.omni.markers.v1', window=dict(start=start,end=end,timezone=timezone,
               start_ms=window[0],end_ms=window[1],bounds='[from,to)'), summary=summary, by_event=dict(by_event),
               event_coverage=event_coverage, output_style=dict(styles), by_source=source_stats, by_slot=slot_stats, groups=group_list,
               conflicting_session_families=sum(b['relationship'] == 'divergent' for b in branches), stores=reports, recordings=branches, revisions=revisions,
               index_revision=revision, items=details,
               next_offset=offset+limit if offset+limit < total else None,
               unmatched_sources=sorted(wanted - {'all'} - {s['source'] for s in stores}),
               unmatched_slots=sorted(selected - {'all'} - {a for s in stores for a in [s['slot'], *s['slot_aliases']]}),
               denominator_basis='unique covered sessions with timed public activity; slot counts can overlap',
               partial=True, uncaptured_triggers_possible=True, effect_assessment='not_performed')
    if review_misses:
        items = list(reviewed.values())
        opportunities = defaultdict(set)
        for it in items:
            first = it['first_request']
            key = (*_family(it['source'], it['session_id']), it['skill'], first['activity_ts_ms'],
                   first['raw_ref'].get('record_id') or first['raw_ref'].get('line_no', first['message_index']))
            opportunities[key].add(it['status'])
        # Preserve branch-local judgments in items. A disputed logical
        # opportunity cannot count twice in confirmed rates.
        counts = Counter(next(iter(statuses)) if len(statuses) == 1 else 'unknown'
                         for statuses in opportunities.values())
        confirmed = sum(counts[k] for k in ('timely','late','expected_unseen'))
        out['review_misses'] = dict(preset=review_misses, items=items, counts=dict(counts), confirmed_opportunities=confirmed,
                                    disputed_opportunities=sum(len(s) > 1 for s in opportunities.values()),
                                    rates={k: counts[k]/confirmed if confirmed else None for k in ('timely','late','expected_unseen')},
                                    basis='redacted_public_user_requests_and_recorded_events')
    return out
