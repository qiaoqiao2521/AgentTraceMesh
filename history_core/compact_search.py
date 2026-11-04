"""Small public-record projections for native and filename-oriented reads.

The native projection reuses the existing visible-part renderer. Private parts
occupy their original display index, but their payloads never reach Python.
"""
import json
import re

from .evidence import redact_indexed_window
from .reuse import _indexed_excerpt, _terms, query_budget
from .sources import _indexed_public_message_sql, parse_ts

NATIVE_PUBLIC_SOURCES = frozenset(('opencode', 'zcode', 'hermes'))
MAX_NATIVE_PUBLIC_BYTES = 2 * 1024 * 1024


def _native_projection(indexer, conn, session_id=None, *, role=None, terms=None,
                       numbered=True, selected_index=None):
    """Render matching public parts only; number slots only in a chosen session.

    Candidate lookup has no window over the source. Selected-session numbering
    keeps private slots, then applies scope/query before invoking the renderer.
    """
    terms = terms or []
    args = []
    if indexer.source in ('opencode', 'zcode'):
        def render(message_role, raw, timestamp):
            data = json.loads(raw)
            flat = indexer._flatten_part(message_role, data.get('type'), data, parse_ts(timestamp))
            if flat is None:
                return None
            message_role, kind, text, stamp, summary = flat
            return json.dumps(dict(role=message_role, kind=kind, text=text, ts_ms=stamp, tool_summary=summary))
        conn.create_function('hv_brief_part', 3, render)
        part = "CASE WHEN json_valid(p.data) THEN p.data ELSE '{}' END"
        message = "CASE WHEN json_valid(m.data) THEN m.data ELSE '{}' END"
        kind = f"trim(COALESCE(json_extract({part},'$.type'),''))"
        body = f"COALESCE(json_extract({part},'$.text'),'')"
        body_type = f"json_type({part},'$.text')"
        has_body = f"""CASE COALESCE({body_type},'null') WHEN 'null' THEN 0 WHEN 'false' THEN 0
            WHEN 'integer' THEN {body}!=0 WHEN 'real' THEN {body}!=0
            WHEN 'text' THEN {body}!='' WHEN 'array' THEN json_array_length({body})>0
            WHEN 'object' THEN {body}!='{{}}' ELSE 1 END"""
        public_body = f"CASE {body_type} WHEN 'true' THEN json('true') WHEN 'false' THEN json('false') ELSE json_extract({part},'$.text') END"
        public_part = f"""json_object('type',{kind},'text',{public_body},
            'tool',json_extract({part},'$.tool'),'state',json_object(
                'status',json_extract({part},'$.state.status'),
                'input',json_extract({part},'$.state.input'),
                'output',json_extract({part},'$.state.output'),
                'error',json_extract({part},'$.state.error'),
                'metadata',json_object(
                    'error',json_extract({part},'$.state.metadata.error'),
                    'exit',json_extract({part},'$.state.metadata.exit'),
                    'exit_code',json_extract({part},'$.state.metadata.exit_code'),
                    'is_error',json_extract({part},'$.state.metadata.is_error'))))"""
        native_role = f"COALESCE(NULLIF(trim(json_extract({message},'$.role')),''),'assistant')"
        if role == 'tool':
            scope = f"{kind}='tool'"
        elif role:
            scope = f"{kind}='text' AND {native_role}='{role}'"
        else:
            scope = f"({kind}='tool' OR ({kind}='text' AND {native_role} IN ('user','assistant','tool')))"
        coarse_args = []
        if terms:
            text_hits = []
            tool_hits = []
            # A text field is already the visible body. Tool JSON is only a
            # conservative prefilter: actual matching still uses its renderer.
            input_value = f"json_extract({part},'$.state.input')"
            decoded_input = f"CASE WHEN json_type({part},'$.state.input')='text' AND json_valid({input_value}) THEN json({input_value}) ELSE {input_value} END"
            search_part = f"json_set({public_part},'$.state.input',{decoded_input})"
            for term in terms:
                text_hits.append(f"({body_type}!='text' OR instr(lower({body}),lower(?))>0)")
                coarse_args.append(term)
            tool_args = []
            for term in terms:
                chunks = list(dict.fromkeys(re.findall(r'\w+', term)))
                pieces = []
                for chunk in chunks:
                    pieces.append(f"""(instr(lower('Tool use Input Output Error json true false none null'),lower(?))>0
                        OR EXISTS (SELECT 1 FROM json_tree({search_part}) j WHERE
                            instr(lower(CAST(j.atom AS TEXT)),lower(?))>0
                            OR instr(lower(CAST(j.key AS TEXT)),lower(?))>0))""")
                    tool_args.extend([chunk, chunk, chunk])
                tool_hits.append('('+' AND '.join(pieces)+')' if pieces else '1=1')
            coarse_args.extend(tool_args)
            coarse = f"(({kind}='text' AND ({' OR '.join(text_hits)})) OR ({kind}='tool' AND ({' OR '.join(tool_hits)})))"
            scope = f'({scope}) AND ({coarse})'
        index = f"ROW_NUMBER() OVER (PARTITION BY p.session_id ORDER BY {indexer._part_order_sql()})-1" if numbered else 'NULL'
        if selected_index is not None:
            scope = f'({scope}) AND ({index})={selected_index}'
        size = f'length(CAST({public_part} AS BLOB))'
        call = f"hv_brief_part({native_role},{public_part},COALESCE(NULLIF(p.time_created,0),m.time_created))"
        eligible = f"json_type({message})='object' AND json({message})!='{{}}' AND json_valid(p.data) AND ({kind}='tool' OR ({kind} IN ('text','reasoning') AND ({has_body})))"
        selected = ' AND p.session_id=?' if session_id is not None else ''
        if numbered:
            projection = f"""SELECT p.session_id,{index} AS message_index,
                CASE WHEN {scope} THEN {size} END AS public_size,
                CASE WHEN {scope} AND {size}<={MAX_NATIVE_PUBLIC_BYTES} THEN {call} END AS payload
                FROM part p JOIN message m ON m.id=p.message_id WHERE {eligible}{selected}"""
            args = [*coarse_args,*coarse_args,*([session_id] if session_id is not None else [])]
        else:
            projection = f"""SELECT p.session_id,NULL AS message_index,{size} AS public_size,
                CASE WHEN {size}<={MAX_NATIVE_PUBLIC_BYTES} THEN {call} END AS payload
                FROM part p JOIN message m ON m.id=p.message_id WHERE {eligible}{selected} AND ({scope})"""
            args = [*([session_id] if session_id is not None else []),*coarse_args]
    else:
        def render(message_role, content, calls, name, timestamp):
            message_role = str(message_role or '').strip() or 'assistant'
            content, calls = str(content or ''), str(calls or '').strip()
            if message_role == 'tool':
                kind, text = 'tool_result', content
            elif calls:
                message_role, kind = 'assistant', 'tool_use'
                text = indexer._tool_use_text({'tool_calls': calls, 'tool_name': name, 'reasoning': ''})
            else:
                kind, text = 'message', content
            if not text:
                return None
            return json.dumps(dict(role=message_role, kind=kind, text=text, ts_ms=parse_ts(timestamp)))
        conn.create_function('hv_brief_hermes', 5, render)
        calls = "trim(COALESCE(tool_calls,''))"
        mapped = "COALESCE(NULLIF(trim(role),''),'assistant')"
        if role == 'tool':
            scope = f"({mapped}='tool' OR {calls}!='')"
        elif role:
            scope = f"{mapped}='{role}' AND {calls}=''"
        else:
            scope = f"{mapped} IN ('user','assistant','tool')"
        coarse_args = []
        if terms:
            coarse = ' OR '.join("instr(lower(COALESCE(content,'')),lower(?))>0" for _ in terms)
            scope = f"({scope}) AND ({calls}!='' OR {coarse})"
            coarse_args.extend(terms)
        index = 'ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY timestamp,id)-1' if numbered else 'NULL'
        if selected_index is not None:
            scope = f'({scope}) AND ({index})={selected_index}'
        size = "length(CAST(COALESCE(content,'') AS BLOB))+length(CAST(COALESCE(tool_calls,'') AS BLOB))"
        call = 'hv_brief_hermes(role,content,tool_calls,tool_name,timestamp)'
        selected = 'session_id=?' if session_id is not None else '1=1'
        if numbered:
            projection = f"""SELECT session_id,{index} AS message_index,
                CASE WHEN {scope} THEN {size} END AS public_size,
                CASE WHEN {scope} AND {size}<={MAX_NATIVE_PUBLIC_BYTES} THEN {call} END AS payload
                FROM messages WHERE {selected}"""
            args = [*coarse_args,*coarse_args,*([session_id] if session_id is not None else [])]
        else:
            projection = f"""SELECT session_id,NULL AS message_index,{size} AS public_size,
                CASE WHEN {size}<={MAX_NATIVE_PUBLIC_BYTES} THEN {call} END AS payload
                FROM messages WHERE {selected} AND ({scope})"""
            args = [*([session_id] if session_id is not None else []),*coarse_args]
    return f"""WITH ordered_native AS MATERIALIZED ({projection}),
        public_messages AS MATERIALIZED (
            SELECT session_id,message_index,json_extract(payload,'$.role') AS role,
                   json_extract(payload,'$.kind') AS kind,json_extract(payload,'$.text') AS text,
                   json_extract(payload,'$.ts_ms') AS ts_ms,
                   json_extract(payload,'$.tool_summary') AS tool_summary_json,'{{}}' AS activity_meta_json
            FROM ordered_native WHERE payload IS NOT NULL
              AND json_extract(payload,'$.role') IN ('user','assistant','tool')
              AND json_extract(payload,'$.kind') IN ('message','tool_use','tool_result')
        )""", args


def _projection(indexer, conn, *, session_id=None, role=None, terms=None):
    from .providers import uses_message_index
    if not uses_message_index(indexer):
        return _native_projection(indexer, conn, session_id, role=role, terms=terms, numbered=False)
    return f"""WITH public_messages AS (
        SELECT m.* FROM messages m WHERE {_indexed_public_message_sql('m')}
    )""", []


def _role_sql(role):
    if role == 'tool':
        return "(m.role='tool' OR m.kind IN ('tool_use','tool_result'))"
    if role:
        return "m.role=? AND m.kind NOT IN ('tool_use','tool_result')"
    return '1=1'


def session_page(indexer, *, query, limit, offset, cwd=None, session_id=None, role=None, literal=False):
    """Filter public record references before pagination; never derive an audit."""
    from .providers import uses_message_index
    native = not uses_message_index(indexer)
    if native and indexer.source not in NATIVE_PUBLIC_SOURCES:
        raise ValueError('public_search_source_unsupported')
    if native and indexer.source in ('opencode', 'zcode'):
        table, project, start, end = 'session', indexer._project_value(), 's.time_created', 's.time_updated'
    elif native:
        table, project, start, end = 'sessions', indexer._project_value(), 's.started_at', 's.ended_at'
    else:
        table, project, start, end = 'sessions', 's.cwd', 's.start_ts_ms', 's.end_ts_ms'
    title = "COALESCE(NULLIF(s.title,''),s.id)"
    terms = [str(query)] if literal and query else _terms(query)[1]
    if native and not terms and role is None:
        return metadata_page(indexer, limit=limit, offset=offset, cwd=cwd, session_id=session_id)
    args, predicates, ranks, rank_args = [], [], [], []
    if session_id is not None:
        predicates.append('s.id=?'); args.append(session_id)
    if cwd:
        predicates.append(f'{project}=?'); args.append(cwd)
    role_predicate = _role_sql(role)
    role_args = [role] if role in ('user', 'assistant') else []
    for term in terms:
        public_hit = f'EXISTS (SELECT 1 FROM public_messages m WHERE m.session_id=s.id AND {role_predicate} AND instr(lower(m.text),lower(?))>0)'
        if native:
            public_hit = f'({public_hit} OR EXISTS (SELECT 1 FROM ordered_native n WHERE n.session_id=s.id AND n.public_size>{MAX_NATIVE_PUBLIC_BYTES}))'
        if role or literal:
            predicates.append(public_hit); args.extend([*role_args, term])
        else:
            predicates.append(f'(instr(lower({title}),lower(?))>0 OR instr(lower(COALESCE({project},\'\')),lower(?))>0 OR instr(lower(s.id),lower(?))>0 OR {public_hit})')
            args.extend([term, term, term, term])
        ranks.append("EXISTS (SELECT 1 FROM public_messages m WHERE m.session_id=s.id AND m.kind IN ('tool_use','tool_result') AND instr(lower(m.text),lower(?))>0)")
        rank_args.append(term)
    if role and not terms:
        role_hit = f'EXISTS (SELECT 1 FROM public_messages m WHERE m.session_id=s.id AND {role_predicate})'
        if native:
            role_hit = f'({role_hit} OR EXISTS (SELECT 1 FROM ordered_native n WHERE n.session_id=s.id AND n.public_size>{MAX_NATIVE_PUBLIC_BYTES}))'
        predicates.append(role_hit)
        args.extend(role_args)
    rank = '+'.join(ranks) if ranks else '0'
    oversized = f'(SELECT COUNT(*) FROM ordered_native n WHERE n.session_id=s.id AND n.public_size>{MAX_NATIVE_PUBLIC_BYTES})' if native else '0'
    with query_budget(indexer) as conn:
        cte, prefix_args = _projection(indexer, conn, session_id=session_id, role=role, terms=terms)
        rows = conn.execute(f'''{cte}
            SELECT s.id,{title} AS title,{project} AS cwd,{start} AS start_ts_ms,{end} AS end_ts_ms,
                   ({rank}) AS tool_match_terms,{oversized} AS native_oversized_records
            FROM {table} s WHERE {' AND '.join(predicates) if predicates else '1=1'}
            ORDER BY tool_match_terms DESC,COALESCE({end},{start}) DESC,{start} DESC,s.id ASC LIMIT ? OFFSET ?
        ''', [*prefix_args, *rank_args, *args, limit+1, offset]).fetchall()
    items = [dict(row) for row in rows[:limit]]
    if native:
        for item in items:
            for key in ('start_ts_ms', 'end_ts_ms'):
                item[key] = parse_ts(item[key])
    more = len(rows) > limit
    return dict(items=items, limit=limit, offset=offset, has_more=more,
                next_offset=offset+len(items) if more else None)


def metadata_page(indexer, *, limit, offset, cwd=None, session_id=None):
    """Keep small, explicitly unverified candidates when public lookup fails."""
    if indexer.source in ('opencode', 'zcode'):
        table, project, start, end = 'session', indexer._project_value(), 'time_created', 'time_updated'
    elif indexer.source == 'hermes':
        table, project, start, end = 'sessions', indexer._project_value(), 'started_at', 'ended_at'
    else:
        raise ValueError('public_search_source_unsupported')
    conditions, args = [], []
    if session_id is not None:
        conditions.append('id=?'); args.append(session_id)
    if cwd:
        conditions.append(project+'=?'); args.append(cwd)
    with query_budget(indexer) as conn:
        rows = conn.execute(f"""SELECT id,COALESCE(NULLIF(title,''),id) AS title,{project} AS cwd,
            {start} AS start_ts_ms,{end} AS end_ts_ms FROM {table} s
            WHERE {' AND '.join(conditions) if conditions else '1=1'}
            ORDER BY COALESCE({end},{start}) DESC,{start} DESC,id ASC LIMIT ? OFFSET ?""",
            [*args,limit+1,offset]).fetchall()
    items = [dict(row) for row in rows[:limit]]
    for item in items:
        for key in ('start_ts_ms','end_ts_ms'):
            item[key] = parse_ts(item[key])
    more = len(rows)>limit
    return dict(items=items,limit=limit,offset=offset,has_more=more,
                next_offset=offset+len(items) if more else None)


def native_snippets(indexer, session_id, query, role=None, literal=False):
    if literal:
        query, terms = str(query or ''), [str(query)] if query else []
    else:
        query, terms = _terms(query)
    if not terms:
        return [], False
    with query_budget(indexer) as conn:
        cte, prefix_args = _native_projection(indexer, conn, session_id, role=role, terms=terms)
        oversized = conn.execute(cte+f' SELECT COUNT(*) FROM ordered_native WHERE public_size>{MAX_NATIVE_PUBLIC_BYTES}',prefix_args).fetchone()[0]
        conn.create_function('hv_brief_chars', 1, len, deterministic=True)
        positions = ','.join('instr(lower(m.text),lower(?)) AS hit_%d' % n for n in range(len(terms)))
        hit_names = ['hit_%d' % n for n in range(len(terms))]
        coverage = '+'.join('(%s>0)' % name for name in hit_names)
        first = ','.join('CASE WHEN %s>0 THEN %s ELSE hv_brief_chars(text)+1 END' % (name,name) for name in hit_names)
        first = 'min('+first+')' if len(terms)>1 else first
        rows = conn.execute(f'''{cte}, located AS (
            SELECT m.*,{positions},instr(lower(m.text),lower(?)) AS phrase_position
            FROM public_messages m WHERE {_role_sql(role)}
        ), matched AS (
            SELECT *,({coverage}) AS matched_terms,
                   CASE WHEN hv_brief_chars(text)<=240 THEN 0 ELSE max(0,(CASE WHEN phrase_position>0 THEN phrase_position ELSE {first} END)-71) END AS excerpt_start
            FROM located WHERE {' OR '.join(name+'>0' for name in hit_names)}
        ) SELECT * FROM matched ORDER BY matched_terms DESC,(phrase_position>0) DESC,
            CASE WHEN kind IN ('tool_use','tool_result') THEN 2 WHEN role='user' THEN 1 ELSE 0 END DESC,
            CASE WHEN role='user' THEN message_index ELSE 0 END DESC,message_index LIMIT 3
        ''', [*prefix_args,*terms,query,*([role] if role in ('user','assistant') else [])]).fetchall()
    excerpts = []
    for record in rows:
        row = dict(record)
        row['text_chars'] = len(row['text'])
        row['text'] = redact_indexed_window(row['text'], row['excerpt_start'], 240)
        excerpt = _indexed_excerpt(row)
        excerpt['excerpt_offset_basis'] = 'public_rendered_text'
        excerpts.append(excerpt)
    return excerpts, bool(oversized)


def read_native_message(indexer, session_id, message_index, text_offset=0):
    from .evidence import _redact
    from .messages import BODY_PAGE_CHARS
    if indexer.source not in NATIVE_PUBLIC_SOURCES:
        raise ValueError('message_evidence_source_unsupported')
    if not isinstance(session_id, str) or not session_id:
        raise ValueError('session_id_required')
    if type(message_index) is not int or message_index < 0:
        raise ValueError('invalid_message_index')
    if type(text_offset) is not int or text_offset < 0:
        raise ValueError('invalid_text_offset')
    with query_budget(indexer) as conn:
        table = 'session' if indexer.source in ('opencode','zcode') else 'sessions'
        if conn.execute(f'SELECT 1 FROM {table} WHERE id=?', (session_id,)).fetchone() is None:
            raise ValueError('session_not_found')
        cte, args = _native_projection(indexer, conn, session_id, selected_index=message_index)
        row = conn.execute(cte+' SELECT * FROM public_messages WHERE message_index=?', [*args,message_index]).fetchone()
        if row is None:
            present = conn.execute(cte+' SELECT public_size FROM ordered_native WHERE message_index=?', [*args,message_index]).fetchone()
            if present and present['public_size'] and present['public_size']>MAX_NATIVE_PUBLIC_BYTES:
                raise ValueError('native_public_message_limit_exceeded')
            raise ValueError('message_not_public' if present else 'message_not_found')
    text, size = row['text'], len(row['text'])
    if text_offset > size:
        raise ValueError('text_offset_out_of_range')
    end = min(size,text_offset+BODY_PAGE_CHARS)
    result = dict(schema_version='history.message.v1',session_id=session_id,message_index=message_index,
        role=row['role'],kind=row['kind'],ts_ms=row['ts_ms'],text=redact_indexed_window(text,text_offset,BODY_PAGE_CHARS),
        char_count=size,text_offset=text_offset,text_offset_basis='public_rendered_text',body_page_chars=BODY_PAGE_CHARS,
        next_text_offset=end if end<size else None,has_more_before=text_offset>0,has_more_after=end<size,
        truncated=text_offset>0 or end<size,call_id=None,call_id_truncated=False,tool_summary_truncated=False,
        redacted=True,partial=False,redaction_notice='Common secret spans use equal-length masking; this is not complete anonymisation.')
    if row['tool_summary_json']:
        result['tool_summary'] = _redact(json.loads(row['tool_summary_json']))
    return result
