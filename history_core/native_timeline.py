"""Small public message-time evidence, using the existing source projections.

This is a conversation timeline, not an activity ledger or an outcome audit.
Candidates contain record IDs only; bodies are expanded after pagination.
"""
import json
import sqlite3

from .compact_search import (NATIVE_PUBLIC_SOURCES, _native_projection,
                             read_native_message)
from .evidence import redact_indexed_window, redact_text
from .providers import uses_message_index
from .reuse import _indexed_excerpt, error_code, query_budget
from .sources import _indexed_public_message_sql


def _window(stamp, start_ms, end_ms):
    clauses, args = [f'{stamp} IS NOT NULL'], []
    if start_ms is not None:
        clauses.append(f'{stamp}>=?'); args.append(start_ms)
    if end_ms is not None:
        clauses.append(f'{stamp}<?'); args.append(end_ms)
    return clauses, args


def _indexed_candidates(indexer, *, start_ms, end_ms, session_id, limit, offset):
    conditions, args = _window('m.ts_ms', start_ms, end_ms)
    conditions.append(_indexed_public_message_sql('m'))
    if session_id is not None:
        conditions.append('m.session_id=?'); args.append(session_id)
    with query_budget(indexer) as conn:
        rows = conn.execute(f"""SELECT m.id AS record_id,m.session_id,m.ts_ms,
            SUBSTR(COALESCE(NULLIF(s.title,''),s.id),1,160) AS title,
            SUBSTR(COALESCE(s.cwd,''),1,4096) AS cwd
            FROM messages m JOIN sessions s ON s.id=m.session_id
            WHERE {' AND '.join(conditions)}
            ORDER BY m.ts_ms DESC,m.session_id ASC,m.id ASC LIMIT ? OFFSET ?""",
            [*args,limit+1,offset]).fetchall()
    return [dict(row) for row in rows]


def _native_candidates(indexer, *, start_ms, end_ms, session_id, limit, offset):
    table = 'session' if indexer.source in ('opencode', 'zcode') else 'sessions'
    cwd = "COALESCE(s.directory,'')" if table == 'session' else "''"
    with query_budget(indexer) as conn:
        cte, args = _native_projection(indexer, conn, session_id, numbered=False,
            records_only=True, start_ms=start_ms, end_ms=end_ms)
        rows = conn.execute(cte+f""" SELECT n.*,
            SUBSTR(COALESCE(NULLIF(s.title,''),s.id),1,160) AS title,
            SUBSTR({cwd},1,4096) AS cwd FROM native_records n
            JOIN {table} s ON s.id=n.session_id
            ORDER BY n.ts_ms DESC,n.session_id ASC,n.record_id ASC LIMIT ? OFFSET ?""",
            [*args,limit+1,offset]).fetchall()
    return [dict(row) for row in rows]


def _ordinals(indexer, rows, native):
    """Only selected sessions participate in display-slot numbering."""
    result = {}
    for sid in dict.fromkeys(row['session_id'] for row in rows):
        ids = [row['record_id'] for row in rows if row['session_id']==sid]
        placeholders = ','.join('?' for _ in ids)
        with query_budget(indexer) as conn:
            if native:
                cte, args = _native_projection(indexer, conn, sid, records_only=True)
                records = conn.execute(cte+f' SELECT record_id,message_index FROM native_records WHERE record_id IN ({placeholders})',
                                       [*args,*ids])
            else:
                from .message_ordinals import ordered_message_ids_sql
                ordered, args = ordered_message_ids_sql(conn,sid)
                records = conn.execute(f'''SELECT id AS record_id,message_index FROM ({ordered})
                    WHERE id IN ({placeholders})''', [*args,*ids])
            result.update({(sid, record['record_id']): record['message_index'] for record in records})
    return result


def _indexed_event(indexer, record_id, message_index):
    with query_budget(indexer) as conn:
        conn.create_function('hv_timeline_window', 3, redact_indexed_window, deterministic=True)
        conn.create_function('hv_timeline_chars', 1, lambda value: len(value or ''), deterministic=True)
        row = conn.execute('''SELECT role,kind,ts_ms,
            hv_timeline_window(text,0,240) AS text,hv_timeline_chars(text) AS text_chars,
            SUBSTR(tool_summary_json,1,8192) AS tool_summary_json,
            SUBSTR(activity_meta_json,1,8192) AS activity_meta_json
            FROM messages WHERE id=?''', (record_id,)).fetchone()
    if row is None:
        raise ValueError('message_not_found')
    event = dict(row, message_index=message_index, excerpt_start=0)
    return _indexed_excerpt(event)


def _native_event(indexer, sid, message_index):
    message = read_native_message(indexer, sid, message_index)
    event = _indexed_excerpt(dict(message, text=message['text'][:240],
        text_chars=message['char_count'], excerpt_start=0, activity_meta_json='{}',
        tool_summary_json=json.dumps(message.get('tool_summary'))))
    event['excerpt_offset_basis'] = 'public_rendered_text'
    return event


def timeline(indexer, *, start_ms=None, end_ms=None, session_id=None, limit=20, offset=0):
    """Page timestamped public events in ``[start_ms, end_ms)`` without refresh.

    The reader binds these offsets to its existing source/index revision. A
    missing time bound means an open end, and does not expand transcript bodies.
    """
    native = not uses_message_index(indexer)
    if native and indexer.source not in NATIVE_PUBLIC_SOURCES:
        raise ValueError('timeline_source_unsupported')
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError('invalid_limit')
    if type(offset) is not int or not 0<=offset<=100000:
        raise ValueError('invalid_offset')
    for value in (start_ms, end_ms):
        if value is not None and (type(value) is not int or value<0):
            raise ValueError('invalid_time_window')
    if start_ms is not None and end_ms is not None and start_ms>=end_ms:
        raise ValueError('invalid_time_window')
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        raise ValueError('session_id_required')
    candidates = _native_candidates if native else _indexed_candidates
    rows = candidates(indexer, start_ms=start_ms, end_ms=end_ms, session_id=session_id,
                      limit=limit, offset=offset)
    more, selected = len(rows)>limit, rows[:limit]
    ordinals = _ordinals(indexer, selected, native)
    items, errors = [], []
    for row in selected:
        sid = row['session_id']
        number = ordinals.get((sid,row['record_id']))
        item = {key: row[key] for key in ('session_id','title','cwd','ts_ms')}
        item.update(id=sid, source=indexer.source, message_index=number,
                    title=redact_text(item['title']), cwd=redact_text(item['cwd']))
        try:
            if number is None:
                raise ValueError('message_not_found')
            evidence = (_native_event(indexer,sid,number) if native
                        else _indexed_event(indexer,row['record_id'],number))
            item.update(evidence, evidence_status='available')
        except (ValueError, OSError, sqlite3.Error) as exc:
            code = error_code(exc)
            errors.append({'session_id':sid, 'message_index':number, 'error':code})
            item.update(evidence_status='unavailable', text='', limitations=[code])
        items.append(item)
    coverage = getattr(indexer, '_coverage', {})
    incomplete = sum(entry.get('content_status')!='decoded_text' for entry in coverage.values())
    return dict(schema_version='history.timeline.v1', source=indexer.source,
        start_ms=start_ms, end_ms=end_ms, time_window='[start_ms,end_ms)',
        session_id=session_id, items=items, limit=limit, offset=offset,
        has_more=more, next_offset=offset+len(selected) if more else None,
        order='timestamp_desc_session_record_asc', errors=errors,
        partial=bool(errors or incomplete), redacted=True,
        source_coverage={'incomplete_sessions':incomplete},
        coverage='public_message_timestamps_not_full_activity',
        timestamp_basis='normalized_provider_display_time',
        limitations=['public_user_assistant_tool_only', 'untimestamped_records_excluded',
                     'provider_timestamps_may_include_inferred_fallbacks', 'not_full_activity_coverage'],
        current_verification='not_performed')
