"""Bounded, revision-bound cross-source history read models.

Queries use the existing derived message index or query-only native connections.
No provider execution, implicit refresh, project filesystem probe, or new store.
"""
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import posixpath
import sqlite3
import time

from . import service
from .evidence import source_store_id
from .sources import (_escape_sql_like, _indexed_body_match_sql,
                      _indexed_session_match_sql, _indexed_public_message_sql,
                      deserialize_audit_summary)
from .providers import SOURCES, canonical_source, uses_message_index

MAX_CANDIDATES = 500
MAX_PROJECTS = 1000
MAX_MESSAGE_ROWS = 2000
MAX_MESSAGE_CHARS = 8192
MAX_RELATED_TOOL_MESSAGES = 3
MAX_DISPLAY_CALL_ID_CHARS = 128


def store_id(indexer):
    root = getattr(indexer, 'sessions_dir', None) or indexer.db_path
    return hashlib.sha256((indexer.source + ':' + str(Path(root).absolute())).encode()).hexdigest()[:24]


def index_revision(indexer):
    root = Path(getattr(indexer, 'sessions_dir', None) or indexer.db_path)
    root.stat()
    if root.is_symlink():
        raise ValueError('source_symlink_not_allowed')
    with indexer.lock:
        if callable(getattr(indexer, 'source_revision', None)):
            value = indexer.source_revision()
            if getattr(indexer, '_revision', value) != value:
                raise ValueError('source_changed_since_index: refresh required')
        elif getattr(indexer, 'sessions_dir', None) is not None:
            value = indexer.conn.execute("SELECT value FROM reader_state WHERE key='revision'").fetchone()[0]
            value = [value, indexer.conn.execute('PRAGMA data_version').fetchone()[0], indexer.conn.total_changes]
        else:
            value = []
            for path in (root, Path(str(root) + '-wal')):
                try:
                    st = path.stat()
                    value.append([st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino])
                except FileNotFoundError:
                    value.append(None)
    return hashlib.sha256(json.dumps([store_id(indexer), value]).encode()).hexdigest()


@contextmanager
def query_budget(indexer):
    """Bound VM work/time on each shared connection, always clear the handler."""
    with indexer.lock:
        # Waiting for another reader is contention, not time spent running SQL.
        deadline = time.monotonic() + 2
        remaining, expired = [2000], [False]
        def stop():
            remaining[0] -= 1
            expired[0] = remaining[0] <= 0 or time.monotonic() > deadline
            return expired[0]
        indexer.conn.set_progress_handler(stop, 10000)
        try:
            yield indexer.conn
        except sqlite3.Error as error:
            if expired[0]:
                error.query_budget_exceeded = True
            raise
        finally:
            indexer.conn.set_progress_handler(None, 0)


def error_code(exc):
    if isinstance(exc, PermissionError): return 'permission_denied'
    if isinstance(exc, FileNotFoundError): return 'source_not_found'
    if isinstance(exc, sqlite3.Error):
        if getattr(exc, 'query_budget_exceeded', False):
            return 'query_budget_exceeded'
        code = getattr(exc, 'sqlite_errorcode', 0) or 0
        message = str(exc).lower()
        if code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or 'database is locked' in message or 'database table is locked' in message:
            return 'source_busy'
        if code & 0xff == sqlite3.SQLITE_INTERRUPT or 'interrupt' in message:
            return 'query_interrupted'
        return 'source_schema_unavailable'
    return str(exc).split(':', 1)[0] if isinstance(exc, ValueError) else 'source_unavailable'


def _capture(indexers, errors):
    ready, versions = [], []
    for system, source, idx in indexers:
        try:
            rev = index_revision(idx)
            ready.append((system, source, idx))
            versions.append([system, source, store_id(idx), rev])
        except (OSError, ValueError, sqlite3.Error) as exc:
            errors.append({'system': system, 'source': source, 'error': error_code(exc)})
    versions += [['error', e.get('system'), e.get('source'), e.get('error')] for e in errors]
    return ready, hashlib.sha256(json.dumps(sorted(versions, key=str)).encode()).hexdigest()


def _page_start(cursor, revision, spec):
    query_hash = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    if not cursor: return 0, query_hash
    if len(cursor) > 2048: raise ValueError('invalid_cursor')
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor))
        offset = data['offset']
        if type(offset) is not int or offset < 0 or offset > 100000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError('invalid_cursor') from None
    if data.get('revision') != revision: raise ValueError('index_revision_changed: restart search')
    if data.get('query') != query_hash: raise ValueError('cursor_query_changed: restart search')
    return offset, query_hash


def _finish(items, offset, limit, revision, query_hash, errors, truncated=False):
    if errors and offset:
        raise ValueError('index_revision_changed: source query failed; restart search')
    end = offset + limit
    token = base64.urlsafe_b64encode(json.dumps({'offset': end, 'revision': revision, 'query': query_hash}).encode()).decode() if end < len(items) and not errors else None
    return {'items': items[offset:end], 'next_cursor': token, 'revision': revision,
            'pagination_status': 'restart_after_source_error' if errors else 'available',
            'errors': errors, 'partial': bool(errors or truncated), 'truncated': bool(truncated),
            'freshness': 'unknown', 'candidate_limit_per_source': MAX_CANDIDATES,
            'observed_at': datetime.now(timezone.utc).isoformat()}


def _check_revision(indexers, original, initial_errors):
    _, now = _capture(indexers, list(initial_errors))
    if now != original: raise ValueError('index_revision_changed: restart search')


def _content_coverage(result, indexers):
    warnings = []
    for system, source, idx in indexers:
        incomplete = sum(item.get('content_status') != 'decoded_text'
                         for item in getattr(idx, '_coverage', {}).values())
        if incomplete:
            warnings.append({'system': system, 'source': source, 'incomplete_sessions': incomplete})
    if warnings:
        result['partial'] = True
        result['content_warnings'] = warnings
    return result


def _terms(query):
    query = str(query or '').strip()
    if len(query) > 300: raise ValueError('query_too_long')
    terms = list(dict.fromkeys(query.split()))
    if len(terms) > 12: raise ValueError('too_many_query_terms')
    return query, terms


def _limit(value, maximum=20):
    try: value = int(value)
    except (ValueError, TypeError): raise ValueError('invalid_limit') from None
    if not 1 <= value <= maximum: raise ValueError('invalid_limit')
    return value


def _path_match(raw, target, cwd):
    try: groups = json.loads(raw or '{}')
    except (ValueError, TypeError): return 0
    if not isinstance(groups, dict): return 0
    def norm(value):
        value = str(value or '')
        return posixpath.normpath(posixpath.join(cwd, value)) if cwd and not value.startswith('/') else posixpath.normpath(value)
    for bucket in ('local', 'remote', 'inferred'):
        for entry in groups.get(bucket) or []:
            if isinstance(entry, dict) and entry.get('path') and norm(entry['path']) == norm(target):
                return 1
    return 0


def _candidates(idx, query='', project=None, start_ms=None, end_ms=None, file_path=None):
    _, terms = _terms(query)
    native = not uses_message_index(idx)
    args = []
    if not native:
        title = "COALESCE(s.title,'')"
        cwd = "COALESCE(s.cwd,'')"
        timestamp = 's.end_ts_ms'
        table = 'sessions s'
        fields = 's.id,s.title,s.cwd,s.start_ts_ms,s.end_ts_ms,s.message_count,s.files_touched_json,s.outcome_signal,s.tool_summary_json,s.command_intents_json,s.remote_context_json,s.value_score,s.friction_score,s.action_density'
        def match(term):
            like = '%' + _escape_sql_like(term) + '%'
            args.extend([like]*3)
            return _indexed_session_match_sql('s')
    elif idx.source == 'hermes':
        title, cwd, timestamp, table = "COALESCE(s.title,'')", "''", 'COALESCE(s.ended_at,s.started_at)*1000', 'sessions s'
        fields = f's.id,s.title,{cwd} AS cwd,s.started_at*1000 AS start_ts_ms,COALESCE(s.ended_at,s.started_at)*1000 AS end_ts_ms,s.message_count'
        def match(term):
            like = '%' + _escape_sql_like(term) + '%';args.extend([like]*4)
            return f"({title} LIKE ? ESCAPE '\\' OR {cwd} LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM messages m WHERE m.session_id=s.id AND (COALESCE(m.content,'') LIKE ? ESCAPE '\\' OR COALESCE(m.reasoning,'') LIKE ? ESCAPE '\\')))"
    else:
        title, cwd, timestamp, table = "COALESCE(s.title,'')", "COALESCE(s.directory,'')", 's.time_updated', 'session s'
        fields = 's.id,s.title,s.directory AS cwd,s.time_created AS start_ts_ms,s.time_updated AS end_ts_ms'
        def match(term):
            like = '%' + _escape_sql_like(term) + '%';args.extend([like]*4)
            return f"({title} LIKE ? ESCAPE '\\' OR {cwd} LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM message m WHERE m.session_id=s.id AND m.data LIKE ? ESCAPE '\\') OR EXISTS (SELECT 1 FROM part p WHERE p.session_id=s.id AND p.data LIKE ? ESCAPE '\\'))"
    # Score is transparent: exact title phrase, title terms, then body phrase.
    rank_args = []
    rank = '0'
    if terms:
        rank = f'(CASE WHEN instr(lower({title}),lower(?))>0 THEN 8 ELSE 0 END'
        rank_args.append(query)
        for term in terms:
            rank += f' + CASE WHEN instr(lower({title}),lower(?))>0 THEN 2 ELSE 0 END'
            rank_args.append(term)
        if not native:
            rank += f' + CASE WHEN {_indexed_body_match_sql("s")} THEN 4 ELSE 0 END'
            rank_args.append('%' + _escape_sql_like(query) + '%')
        rank += ')'
    predicates = [match(term) for term in terms]
    if project is not None:
        predicates.append(f"COALESCE({cwd},'') = ?");args.append(project)
    if start_ms is not None: predicates.append(f'{timestamp} >= ?');args.append(start_ms)
    if end_ms is not None: predicates.append(f'{timestamp} <= ?');args.append(end_ms)
    if file_path and not native:
        predicates.append('hv_reuse_file_match(s.files_touched_json, ?, COALESCE(s.cwd,\'\')) = 1');args.append(file_path)
    sql = f'SELECT {fields},{rank} AS relevance FROM {table}'
    if predicates: sql += ' WHERE ' + ' AND '.join(predicates)
    sql += f' ORDER BY relevance DESC,{timestamp} DESC,s.id ASC LIMIT ?'
    with query_budget(idx) as conn:
        if not native: conn.create_function('hv_reuse_file_match', 3, _path_match, deterministic=True)
        rows = [dict(row) for row in conn.execute(sql, [*rank_args, *args, MAX_CANDIDATES+1])]
    capped = len(rows) > MAX_CANDIDATES
    return rows[:MAX_CANDIDATES], capped


def _indexed_excerpt(row):
    """Keep tool identity/status separate from a possibly clipped text window."""
    from .evidence import redact_text, _redact
    def object_value(raw):
        try:
            value = json.loads(raw or '{}')
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
    meta = object_value(row['activity_meta_json'])
    summary = object_value(row['tool_summary_json'])
    call_id = meta.get('tool_call_id')
    call_id = redact_text(call_id) if isinstance(call_id, str) and call_id else None
    call_id_truncated = bool(call_id and len(call_id) > MAX_DISPLAY_CALL_ID_CHARS)
    if call_id_truncated:
        call_id = call_id[:MAX_DISPLAY_CALL_ID_CHARS]
    tool_summary = None
    if row['kind'] in ('tool_use', 'tool_result'):
        status = summary.get('exit_status')
        tool_summary = _redact({key: summary.get(key) for key in ('name', 'category', 'headline', 'exit_code', 'is_error')})
        tool_summary['exit_status'] = status if status in ('ok', 'error') else 'unknown'
        for key, maximum in (('name', 80), ('category', 32), ('headline', 120)):
            if isinstance(tool_summary[key], str):
                tool_summary[key] = tool_summary[key][:maximum]
    start, size = row['excerpt_start'], row['text_chars']
    end = min(size, start + 240)
    return {'message_index': row['message_index'], 'role': row['role'], 'kind': row['kind'],
            'ts_ms': row['ts_ms'], 'call_id': call_id, 'call_id_truncated': call_id_truncated,
            'tool_summary': tool_summary,
            'text': row['text'], 'truncated': start > 0 or end < size,
            'has_more_before': start > 0, 'has_more_after': end < size,
            'excerpt_start': start, 'excerpt_end': end, 'text_chars': size,
            'excerpt_offset_basis': 'indexed_text'}


def _indexed_snippets(idx, sid, query, terms, role=None):
    """Rank public message evidence, then return three short excerpts.

    Number before filtering so deep links keep the reader's timestamp/id order.
    The window contains IDs only, not copies of arbitrarily long message bodies.
    Query work remains bounded by query_budget, rather than a silent text prefix.
    """
    if not terms:
        return [], False
    from .evidence import redact_indexed_window
    text = "COALESCE(m.text,'')"
    occurrence = f'instr(lower({text}),lower(?))'
    hits = [f'hit_{n}' for n in range(len(terms))]
    term_columns = ','.join(f'{occurrence} AS {hit}' for hit in hits)
    coverage = ' + '.join(f'({hit}>0)' for hit in hits)
    term_positions = [f'CASE WHEN {hit}>0 THEN {hit} ELSE body_chars+1 END' for hit in hits]
    first_term = term_positions[0] if len(hits) == 1 else 'min(' + ','.join(term_positions) + ')'
    position = f'CASE WHEN phrase_position>0 THEN phrase_position ELSE {first_term} END'
    predicate = ' OR '.join(f'{hit}>0' for hit in hits)
    sql = f'''
        WITH ordered AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY ts_ms ASC,id ASC)-1 AS message_index
            FROM messages WHERE session_id=?
        ), located AS MATERIALIZED (
            SELECT o.id,o.message_index,m.role,m.kind,hv_reuse_text_chars({text}) AS body_chars,
                   {occurrence} AS phrase_position,{term_columns}
            FROM ordered o JOIN messages m ON m.id=o.id
            WHERE {_indexed_public_message_sql('m')}{' AND m.role=?' if role else ''}
        ), matches AS (
            SELECT *,({coverage}) AS matched_terms,{position} AS match_position
            FROM located WHERE {predicate}
        ), windows AS (
            SELECT *,CASE WHEN body_chars<=240 THEN 0 ELSE max(0,match_position-71) END AS excerpt_start
            FROM matches
        ), selected AS MATERIALIZED (
            SELECT * FROM windows
            ORDER BY matched_terms DESC,(phrase_position>0) DESC,
                     CASE WHEN role='tool' AND kind IN ('tool_use','tool_result') THEN 2
                          WHEN role='user' THEN 1 ELSE 0 END DESC,
                     CASE WHEN role='user' THEN message_index ELSE 0 END DESC,
                     message_index ASC LIMIT 3
        )
        SELECT r.message_index,r.role,r.kind,m.ts_ms,m.tool_summary_json,m.activity_meta_json,
               hv_reuse_indexed_window({text},r.excerpt_start,240) AS text,
               r.excerpt_start,r.body_chars AS text_chars
        FROM selected r JOIN messages m ON m.id=r.id
        ORDER BY r.matched_terms DESC,(r.phrase_position>0) DESC,
                 CASE WHEN r.role='tool' AND r.kind IN ('tool_use','tool_result') THEN 2
                      WHEN r.role='user' THEN 1 ELSE 0 END DESC,
                 CASE WHEN r.role='user' THEN r.message_index ELSE 0 END DESC,
                 r.message_index ASC
    '''
    args = [sid, query, *terms]
    if role:
        args.append(role)
    with query_budget(idx) as conn:
        conn.create_function('hv_reuse_text_chars', 1, len, deterministic=True)
        conn.create_function('hv_reuse_indexed_window', 3, redact_indexed_window, deterministic=True)
        rows = conn.execute(sql, args).fetchall()
    return [_indexed_excerpt(row) for row in rows], False


def _related_tool_messages(idx, sid, snippets):
    """Same-session Call ID counterparts only; bounded independently of hits."""
    indices = [item['message_index'] for item in snippets
               if item.get('kind') in ('tool_use', 'tool_result') and item.get('call_id')]
    if not indices:
        return [], False
    from .evidence import redact_indexed_window
    placeholders = ','.join('?' for _ in indices)
    call_id = "CASE WHEN json_valid(m.activity_meta_json) THEN json_extract(m.activity_meta_json,'$.tool_call_id') END"
    # Keep the small ordered selection outermost; an IN-list can otherwise make
    # SQLite scan every session's messages before looking up these few calls.
    sql = f'''
        WITH ordered AS (
            SELECT id,ROW_NUMBER() OVER (ORDER BY ts_ms,id)-1 AS message_index
            FROM messages WHERE session_id=?
        ), selected_calls AS MATERIALIZED (
            SELECT o.message_index,{call_id} AS call_id,
                   CASE WHEN m.kind='tool_use' THEN 'tool_result' ELSE 'tool_use' END AS counterpart
            FROM ordered o CROSS JOIN messages m ON m.id=o.id
            WHERE o.message_index IN ({placeholders}) AND m.role='tool'
              AND m.kind IN ('tool_use','tool_result')
        ), related AS MATERIALIZED (
        SELECT o.id,o.message_index,group_concat(s.message_index) AS related_to
        FROM ordered o JOIN messages m ON m.id=o.id
        JOIN selected_calls s ON {call_id}=s.call_id AND m.kind=s.counterpart
        WHERE {_indexed_public_message_sql('m')} AND m.role='tool'
          AND o.message_index NOT IN ({placeholders})
        GROUP BY o.id ORDER BY o.message_index LIMIT ?
        )
        SELECT r.message_index,m.role,m.kind,m.ts_ms,m.tool_summary_json,m.activity_meta_json,
               hv_reuse_indexed_window(COALESCE(m.text,''),0,240) AS text,0 AS excerpt_start,
               hv_reuse_text_chars(COALESCE(m.text,'')) AS text_chars,r.related_to
        FROM related r JOIN messages m ON m.id=r.id ORDER BY r.message_index
    '''
    with query_budget(idx) as conn:
        conn.create_function('hv_reuse_text_chars', 1, len, deterministic=True)
        conn.create_function('hv_reuse_indexed_window', 3, redact_indexed_window, deterministic=True)
        rows = conn.execute(sql, [sid, *indices, *indices, MAX_RELATED_TOOL_MESSAGES + 1]).fetchall()
    related = []
    for row in rows[:MAX_RELATED_TOOL_MESSAGES]:
        item = _indexed_excerpt(row)
        item['related_to_message_indexes'] = sorted({int(value) for value in row['related_to'].split(',')})
        related.append(item)
    return related, len(rows) > MAX_RELATED_TOOL_MESSAGES


def _snippets(idx, sid, query, role=None):
    query, terms = _terms(query)
    matches = []
    native = not uses_message_index(idx)
    if not native:
        return _indexed_snippets(idx, sid, query, terms, role=role)
    if native and idx.source in ('opencode', 'zcode'):
        from .provenance import validate_native_size
        validate_native_size(idx, sid)
        # Native flattener has a byte/row guard before invocation.
        rows = idx._load_flat_messages(sid)
        truncated = len(rows) > MAX_MESSAGE_ROWS
        messages = ((n, row.get('role',''), str(row.get('text') or '')[:MAX_MESSAGE_CHARS]) for n,row in enumerate(rows[:MAX_MESSAGE_ROWS]))
    else:
        sql = 'SELECT COALESCE(role,\'\') AS role, substr((COALESCE(content,\'\') || char(10) || COALESCE(reasoning,\'\')),1,?) AS text FROM messages WHERE session_id=? ORDER BY timestamp ASC,id ASC LIMIT ?'
        with query_budget(idx) as conn:
            rows = conn.execute(sql, (MAX_MESSAGE_CHARS, sid, MAX_MESSAGE_ROWS+1)).fetchall()
        truncated = len(rows) > MAX_MESSAGE_ROWS
        messages = ((n, row['role'], row['text'] or '') for n,row in enumerate(rows[:MAX_MESSAGE_ROWS]))
    for n,role,text in messages:
        truncated |= len(text) >= MAX_MESSAGE_CHARS
        lower = text.lower()
        hits = [lower.find(term.lower()) for term in terms]
        hits = [pos for pos in hits if pos >= 0]
        if not hits: continue
        pos = lower.find(query.lower())
        if pos < 0: pos = min(hits)
        start = max(0, pos-70)
        excerpt = text[start:start+240]
        matches.append({'message_index': n, 'role': role, 'text': excerpt, 'truncated': len(text)>240})
        if len(matches) == 3: break
    return matches, truncated


def _item(system, source, idx, row):
    return {'system': system, 'source': source, 'store_id': source_store_id(system, source, idx),
            'content_status': getattr(idx, '_coverage', {}).get(str(row['id']), {}).get('content_status'),
            'id': str(row['id']), 'title': row.get('title') or str(row['id']),
            'project': '' if source == 'hermes' else (row.get('cwd') or ''), 'updated_at': row.get('end_ts_ms') or row.get('start_ts_ms'),
            'score': row.get('relevance', 0)}


def search(indexers, *, query, project=None, source=None, start_ms=None, end_ms=None, cursor=None, limit=20, errors=None):
    limit = _limit(limit);query,_ = _terms(query)
    errors = list(errors or [])
    if not query: raise ValueError('query_required')
    source = canonical_source(source)
    if source and source not in SOURCES:
        raise ValueError('invalid_source')
    selected = [x for x in indexers if not source or x[1] == source]
    errors = [e for e in errors if not source or e.get('source') == source]
    before_errors = list(errors)
    ready, revision = _capture(selected, errors)
    offset, qhash = _page_start(cursor, revision, ['search',query,project,source,start_ms,end_ms,limit])
    items, truncated = [], False
    owners = {}
    for system, src, idx in ready:
        try:
            rows, capped = _candidates(idx, query, project, start_ms, end_ms)
            truncated |= capped
            for row in rows:
                item = _item(system, src, idx, row)
                owners[(system,src,item['store_id'])] = idx
                item['match_reason'] = '标题、正文或项目匹配；标题短语优先，再按时间排序'
                items.append(item)
        except (OSError, ValueError, sqlite3.Error) as exc:
            errors.append({'system':system,'source':src,'error':error_code(exc)})
    items.sort(key=lambda x:(-x['score'],-(x['updated_at'] or 0),x['source'],x['store_id'],x['id']))
    result = _finish(items,offset,limit,revision,qhash,errors,truncated)
    for item in result['items']:
        idx = owners[(item['system'],item['source'],item['store_id'])]
        try:
            item['source_revision'] = index_revision(idx)
            item['snippets'], capped = _snippets(idx,item['id'],query)
            item['snippet_status'] = 'bounded' if capped else ('matched' if item['snippets'] else 'metadata_match_or_excerpt_unavailable')
            if capped: result['partial'] = result['truncated'] = True
        except (ValueError, OSError, sqlite3.Error) as exc:
            item['snippets'] = [];item['snippet_status'] = error_code(exc)
            result['partial'] = True
    _check_revision(selected,revision,before_errors)
    return _content_coverage(result, ready)


def evidence_item(system, source, idx, sid):
    metadata = idx.get_session_metadata(sid)
    if metadata is None: raise ValueError('session_not_found')
    result = _item(system,source,idx,metadata)
    try:
        payload = service.handoff(idx,sid)['payload']
        for key in ('goal','status','changed','verified','remaining','blockers','decisions','evidence','provenance','next_action','failures'):
            result[key] = payload.get(key)
        result['evidence_status'] = 'available'
    except (ValueError, OSError, sqlite3.Error) as exc:
        result.update(status='unknown', evidence_status=error_code(exc), evidence=[], changed=[], verified=[], remaining=[])
    result['current_verification'] = 'unknown'
    return result


def projects(indexers, *, cursor=None, limit=20, errors=None):
    limit=_limit(limit);errors=list(errors or []);before_errors=list(errors)
    ready,revision=_capture(indexers,errors)
    offset,qhash=_page_start(cursor,revision,['projects',limit])
    groups={};truncated=False
    for system,src,idx in ready:
        try:
            native=not uses_message_index(idx)
            if not native: table,cwd,ts='sessions',"COALESCE(cwd,'')",'end_ts_ms'
            elif src in ('opencode','zcode'): table,cwd,ts='session',"COALESCE(directory,'')",'time_updated'
            else: table,cwd,ts='sessions',"''",'COALESCE(ended_at,started_at)*1000'
            with query_budget(idx) as conn:
                rows=conn.execute(f'SELECT {cwd} AS project,COUNT(*) AS n,MAX({ts}) AS latest FROM {table} GROUP BY {cwd} ORDER BY latest DESC,project ASC LIMIT ?', (MAX_PROJECTS+1,)).fetchall()
            truncated |= len(rows)>MAX_PROJECTS
            for row in rows[:MAX_PROJECTS]:
                project=row['project'] or ''
                group=groups.setdefault(project,{'project':project,'label':posixpath.basename(project.rstrip('/')) if project else '未绑定项目','session_count':0,'last_activity':0,'sources':[]})
                group['session_count']+=row['n'];group['last_activity']=max(group['last_activity'],row['latest'] or 0)
                group['sources'].append({'system':system,'source':src,'store_id':source_store_id(system,src,idx)})
        except (ValueError,OSError,sqlite3.Error) as exc:
            errors.append({'system':system,'source':src,'error':error_code(exc)})
    items=sorted(groups.values(),key=lambda x:(-x['last_activity'],x['project']))
    _check_revision(indexers,revision,before_errors)
    return _finish(items,offset,limit,revision,qhash,errors,truncated)


def sessions(indexers, *, project, source=None, cursor=None, limit=50, errors=None):
    """List one project's sessions without extracting messages or audit evidence."""
    if project is None: raise ValueError('project_required')
    if len(project) > 4096: raise ValueError('path_too_long')
    limit = _limit(limit, maximum=100)
    source = canonical_source(source)
    if source and source not in SOURCES: raise ValueError('invalid_source')
    selected = [entry for entry in indexers if not source or entry[1] == source]
    errors = [error for error in (errors or []) if not source or error.get('source') == source]
    before_errors = list(errors)
    ready, revision = _capture(selected, errors)
    offset, qhash = _page_start(cursor, revision, ['sessions', project, source, limit])
    items, truncated = [], False
    for system, src, idx in ready:
        try:
            rows, capped = _candidates(idx, project=project)
            source_revision = index_revision(idx)
            truncated |= capped
            for row in rows:
                item = _item(system, src, idx, row)
                item['source_revision'] = source_revision
                items.append(item)
        except (ValueError, OSError, sqlite3.Error) as exc:
            errors.append({'system': system, 'source': src, 'error': error_code(exc)})
    items.sort(key=lambda item: (-(item['updated_at'] or 0), item['source'], item['store_id'], item['id']))
    # Annotate the complete bounded candidate set before slicing pages. Grouping
    # never changes membership, order, cursor semantics or source identities.
    from .related_sessions import annotate_related_sessions
    items = annotate_related_sessions(items)
    result = _finish(items, offset, limit, revision, qhash, errors, truncated)
    result.update(project=project, source=source, has_more=bool(result['next_cursor']))
    _check_revision(selected, revision, before_errors)
    return _content_coverage(result, ready)


def timeline(indexers, *, project, file_path=None, cursor=None, limit=20, errors=None):
    if project is None: raise ValueError('project_required')
    if len(project)>4096 or len(file_path or '')>4096: raise ValueError('path_too_long')
    limit=_limit(limit);errors=list(errors or []);before_errors=list(errors)
    ready,revision=_capture(indexers,errors)
    offset,qhash=_page_start(cursor,revision,['timeline',project,file_path,limit])
    items=[];owners={};truncated=False
    for system,src,idx in ready:
        try:
            if file_path and getattr(idx, 'sessions_dir', None) is None:
                errors.append({'system':system,'source':src,'error':'file_history_not_indexed'})
                continue
            rows,capped=_candidates(idx,project=project,file_path=file_path)
            truncated |= capped
            for row in rows:
                item=_item(system,src,idx,row)
                items.append(item);owners[(system,src,item['store_id'])]=idx
        except (ValueError,OSError,sqlite3.Error) as exc:
            errors.append({'system':system,'source':src,'error':error_code(exc)})
    items.sort(key=lambda x:(-(x['updated_at'] or 0),x['source'],x['store_id'],x['id']))
    result=_finish(items,offset,limit,revision,qhash,errors,truncated)
    result['project']=project;result['file']=file_path
    for n,item in enumerate(result['items']):
        idx=owners[(item['system'],item['source'],item['store_id'])]
        result['items'][n]=evidence_item(item['system'],item['source'],idx,item['id'])
        if file_path:
            from .file_history import file_changes
            current = result['items'][n]
            try:
                changes = file_changes(idx, item['id'], project, file_path, current.get('provenance') or {})
                current.update(file_changes=changes, file_status='available' if changes else 'unknown',
                               diff_status='available' if any(change.get('diff') for change in changes) else 'unavailable')
            except (ValueError, OSError, sqlite3.Error) as exc:
                current.update(file_changes=[], file_status='unavailable', diff_status='unavailable', file_error=error_code(exc))
                result['partial'] = True
                result['errors'].append({'system': item['system'], 'source': item['source'], 'error': error_code(exc)})
    _check_revision(indexers,revision,before_errors)
    return _content_coverage(result, ready)
