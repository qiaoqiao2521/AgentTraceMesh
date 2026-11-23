"""Bounded queries of recorded trace markers, never message bodies or effects."""
import json

from .evidence import redact_text
from .reuse import query_budget

MARKER_SOURCES = ('codex', 'claude', 'antigravity')
MARKER_EVENTS = ('invocation_requested', 'agent_declared', 'context_injected', 'skill_file_read')
MAX_LIMIT = 100
MAX_OFFSET = 100000
MAX_METADATA_CHARS = 65536
FIELD_CHARS = 128
PATH_CHARS = 512
CWD_CHARS = 2048
_MARKER_FIELDS = ('kind', 'event', 'name', 'path', 'version', 'content_hash',
                  'evidence_origin', 'hook_event')
_RAW_REF_FIELDS = ('line_no', 'record_id', 'parent_message_id', 'json_pointer')


def _bounded_text(value, limit, field, truncated):
    if not isinstance(value, str):
        return None
    # Mask complete selected fields before clipping across secret boundaries.
    safe = redact_text(value)
    if len(safe) > limit:
        truncated.append(field)
    return safe[:limit]


def _display_metadata(raw):
    """The SQL caller supplies only whitelisted fields, not raw metadata."""
    value = json.loads(raw)
    truncated = []
    result = {}
    for field in _MARKER_FIELDS:
        result[field] = _bounded_text(value.get(field),
            PATH_CHARS if field == 'path' else FIELD_CHARS, field, truncated)
        if value.get(field + '_truncated') in (True, 1) and field not in truncated:
            truncated.append(field)
    for field, limit in (('call_id', FIELD_CHARS), ('cwd', CWD_CHARS),
                         ('turn_id', FIELD_CHARS), ('context_basis', FIELD_CHARS)):
        result[field] = _bounded_text(value.get(field), limit, field, truncated)
    if value.get('event') == 'skill_file_read':
        for field in ('tool_name', 'request_status'):
            result[field] = _bounded_text(value.get(field), FIELD_CHARS, field, truncated)
    raw_ref = {}
    for field in _RAW_REF_FIELDS:
        ref = value.get('raw_ref_' + field)
        if field == 'line_no':
            if type(ref) is int and ref > 0:
                raw_ref[field] = ref
        elif isinstance(ref, str):
            raw_ref[field] = _bounded_text(ref, PATH_CHARS,
                                           'raw_ref.' + field, truncated)
    result.update(raw_ref=raw_ref, metadata_truncated=bool(truncated),
                  truncated_fields=truncated)
    return json.dumps(result, ensure_ascii=False)


def read_markers(indexer, *, name=None, kind=None, session_id=None, project=None, limit=20, offset=0, event=None):
    if getattr(indexer, 'source', None) not in MARKER_SOURCES:
        raise ValueError('markers_source_unsupported')
    validate_filters(name=name, kind=kind, event=event, session_id=session_id, project=project, limit=limit, offset=offset)
    with query_budget(indexer) as conn:
        rows = list(marker_rows(conn, indexer.source, name=name, kind=kind, event=event,
                                session_id=session_id, project=project, limit=limit + 1, offset=offset))
        coverage = None
        if indexer.source == 'antigravity':
            coverage = transcript_coverage(conn, session_id)
    result = marker_page(rows[:limit], limit, offset, len(rows) > limit)
    if coverage is not None:
        result['transcript_coverage'] = coverage
    return result


def validate_filters(*, name, kind, event, session_id, project, limit, offset):
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError('invalid_marker_limit')
    if type(offset) is not int or not 0 <= offset <= MAX_OFFSET:
        raise ValueError('invalid_marker_offset')
    if name is not None and (not isinstance(name, str) or not name or len(name) > FIELD_CHARS):
        raise ValueError('invalid_marker_name')
    if kind is not None and kind not in ('skill', 'output_style'):
        raise ValueError('invalid_marker_kind')
    if event is not None and event not in MARKER_EVENTS:
        raise ValueError('invalid_marker_event')
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id or len(session_id) > 1024
    ):
        raise ValueError('invalid_marker_session')
    if project is not None and (not isinstance(project, str) or not project or len(project) > CWD_CHARS):
        raise ValueError('invalid_marker_project')


def transcript_coverage(conn, session_id=None):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='agy_transcript_state'").fetchone():
        return {'status': 'not_refreshed'}
    sql = "SELECT json_extract(coverage_json,'$.status'),COUNT(*) FROM agy_transcript_state"
    params = (session_id,) if session_id is not None else ()
    if params:
        sql += ' WHERE session_id=?'
    return {'status': 'indexed_prefixes', 'sessions': dict(conn.execute(sql + ' GROUP BY 1', params))}


def indexed_time(source):
    # The file writer substitutes start_ts_ms for missing message time. Only a
    # distinct, positive ts_ms on a parsed native row rules out that substitution.
    if source not in ('codex', 'claude'):
        return 'm.activity_ts_ms'
    return """COALESCE(m.activity_ts_ms, CASE WHEN m.ts_ms>0 AND m.kind NOT LIKE 'raw_json%'
        AND m.ts_ms<>(SELECT s.start_ts_ms FROM sessions s WHERE s.id=m.session_id) THEN m.ts_ms END)"""


def time_window_sql(source):
    timed = 'm.activity_ts_ms>=:lo AND m.activity_ts_ms<:hi'
    if source in ('codex', 'claude'):
        return f"(({timed}) OR (m.activity_ts_ms IS NULL AND m.ts_ms>=:lo AND m.ts_ms<:hi AND {indexed_time(source)} IS NOT NULL))"
    return '(' + timed + ')'


def marker_rows(conn, source, *, name=None, kind=None, event=None, session_id=None, project=None,
                limit=None, offset=0, window=None, recorded_time=False, unknown_time=False):
    # Only marker candidates pay for the ordinal lookup in a window query.
    # Counting preceding IDs includes hidden rows and resolves timestamp ties.
    meta = "CASE WHEN length(m.activity_meta_json)<=? AND json_valid(m.activity_meta_json) THEN m.activity_meta_json ELSE '{}' END"
    params = [MAX_METADATA_CHARS]
    scope = []
    if session_id is not None:
        scope.append('m.session_id=?')
        params.append(session_id)
    if source == 'antigravity':
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='agy_transcript_rows'").fetchone():
            return
        scope.append('m.call_index>=0')
        table = 'agy_transcript_rows'
        verified_time = 'm.ts_ms'
        basis = "'created_at'"
        ordinal = 'm.message_index'
        locator = 'm.line_no AS line_no,m.call_index'
    else:
        table = 'messages'
        verified_time = indexed_time(source) if window or recorded_time or unknown_time else 'm.activity_ts_ms'
        basis = "CASE WHEN m.activity_ts_ms IS NOT NULL THEN 'activity_ts_ms' ELSE 'recorded_ts_ms' END"
        ordinal = ('(SELECT COUNT(*) FROM messages prior WHERE prior.session_id=m.session_id '
                   'AND (prior.ts_ms,prior.id)<(m.ts_ms,m.id))')
        locator = 'm.id AS line_no,0 AS call_index'
    if window is not None:
        if source in ('codex', 'claude'):
            scope.append(time_window_sql(source).replace(':lo', '?').replace(':hi', '?'))
            params.extend(window)
        else:
            scope.extend([verified_time + '>=?', verified_time + '<?'])
        params.extend(window)
    if unknown_time:
        if source != 'antigravity':
            scope.append("m.activity_ts_ms IS NULL AND m.activity_meta_json LIKE '%trace_markers%'")
        scope.append(verified_time + ' IS NULL')
    where = 'WHERE ' + ' AND '.join(scope) if scope else ''
    filters = ["json_extract(marker,'$.kind') IN ('skill','output_style')",
               "json_type(marker,'$.event')='text'",
               "length(json_extract(marker,'$.event'))>0",
               "json_type(marker,'$.name')='text'",
               "length(json_extract(marker,'$.name'))>0"]
    for field, selected in (('name', name), ('kind', kind), ('event', event)):
        if selected is not None:
            filters.append("json_extract(marker,'$.%s')=?" % field)
            params.append(selected)
    if project is not None:
        filters.append("COALESCE(json_extract(meta,'$.trace_context.cwd'),session_cwd)=?")
        params.append(project)
    fields = []
    extra = ('tool_name', 'request_status') if source == 'antigravity' else ()
    for field in (*_MARKER_FIELDS, *extra):
        fields.extend(("'%s'" % field, "json_extract(marker,'$.%s')" % field))
        fields.extend(("'%s_truncated'" % field,
                       "json_extract(marker,'$.%s_truncated')" % field))
    for field in _RAW_REF_FIELDS:
        fields.extend(("'raw_ref_%s'" % field,
                       "json_extract(meta,'$.raw_ref.%s')" % field))
    fields.extend(("'call_id'", "json_extract(meta,'$.tool_call_id')",
                   "'cwd'", "COALESCE(json_extract(meta,'$.trace_context.cwd'),session_cwd)",
                   "'turn_id'", "COALESCE(json_extract(meta,'$.trace_context.turn_id'),json_extract(meta,'$.turn_id'))",
                   "'context_basis'", "json_extract(meta,'$.trace_context.basis')"))
    pagination = ''
    if limit is not None:
        pagination = 'LIMIT ? OFFSET ?'
        params.extend((limit, offset))
    conn.create_function('hv_marker_metadata', 1, _display_metadata, deterministic=True)
    marker_index = 'o.call_index' if source == 'antigravity' else 'j.key'
    rows = conn.execute(f'''
            WITH ordered AS (
                SELECT m.session_id,m.ts_ms,{verified_time} AS activity_ts_ms,{meta} AS meta,
                       {ordinal} AS message_index,{locator},{basis} AS time_basis
                FROM {table} m {where}
            ), candidates AS (
                SELECT o.*,s.cwd AS session_cwd,{marker_index} AS marker_index,
                       CASE WHEN j.type='object' THEN j.value ELSE '{{}}' END AS marker
                FROM ordered o JOIN sessions s ON s.id=o.session_id
                JOIN json_each(CASE WHEN json_type(o.meta,'$.trace_markers')='array'
                                    THEN json_extract(o.meta,'$.trace_markers') ELSE '[]' END) j
            )
            SELECT session_id,message_index,marker_index,ts_ms,activity_ts_ms,time_basis,
                   hv_marker_metadata(json_object({','.join(fields)})) AS display_json
            FROM candidates WHERE {' AND '.join(filters)}
            ORDER BY ts_ms DESC,session_id ASC,message_index DESC,line_no DESC,call_index ASC,marker_index ASC
            {pagination}
        ''', params)
    for row in rows:
        item = {'source': source, 'session_id': row['session_id'],
                'message_index': row['message_index'], 'marker_index': row['marker_index'],
                'ts_ms': row['ts_ms'], 'activity_ts_ms': row['activity_ts_ms']}
        item.update(json.loads(row['display_json']))
        if window or recorded_time:
            item['time_basis'] = row['time_basis']
        yield item


def marker_page(items, limit, offset, has_more):
    return {'schema_version': 'history.markers.v1', 'items': items,
            'limit': limit, 'offset': offset, 'count': len(items),
            'count_basis': 'returned_recorded_markers', 'has_more': has_more,
            'next_offset': offset + len(items) if has_more else None,
            'coverage': 'recorded_markers_only', 'partial': True,
            'uncaptured_triggers_possible': True, 'effect_assessment': 'not_performed',
            'metadata_budget_chars': MAX_METADATA_CHARS, 'oversized_metadata_policy': 'omitted',
            'redacted': True,
            'redaction_notice': 'Common secret spans are masked; this is not complete anonymisation.'}
