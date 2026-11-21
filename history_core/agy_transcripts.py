"""Explicit IDE transcript refresh. Source paths come exclusively from the bound summary store."""
from collections import Counter
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os

from .capture import JsonlCapture, stat_signature
from .evidence import redact_text
from .projection import PROJECTION_VERSION
from .sources import parse_ts
from .trace_markers import agy_skill_file_reads

TRANSCRIPT_VERSION = 1


def cache_path(directory, source_path, source='antigravity'):
    identity = hashlib.sha256((str(source_path) + '\0' + source).encode()).hexdigest()[:24]
    return directory / ('agy-' + identity + '.sqlite')


def _transcript(root, sid):
    path = root
    for part in ('brain', sid, '.system_generated', 'logs', 'transcript.jsonl'):
        path /= part
        if path.is_symlink():
            raise ValueError('transcript_symlink')
    return path


@contextmanager
def _capture_transcript(root, path):
    # Pin each directory on systems with openat. The capture still checks the
    # complete live pathname against the pinned file before publishing.
    with ExitStack() as stack:
        parent = None
        if os.open in os.supports_dir_fd:
            flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            parent = os.open(root, flags)
            stack.callback(os.close, parent)
            for part in path.parent.relative_to(root).parts:
                parent = os.open(part, flags, dir_fd=parent)
                stack.callback(os.close, parent)
        yield stack.enter_context(JsonlCapture(path, capture_mode='projected', directory_fd=parent))


def _store_rows(conn, root, path, sid, cwd, native_calls):
    with _capture_transcript(root, path) as capture:
        for number, line in enumerate(capture.projection.lines(), 1):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            step = record.get('step_index')
            if type(step) is not int or step < 0:
                continue
            ts = parse_ts(record.get('created_at'))
            ref = {'line_no': number, 'record_id': str(step)}
            def insert(idx, role, text, meta, native=None):
                conn.execute('INSERT INTO agy_transcript_rows VALUES(?,?,?,?,?,?,?,?)',
                             (sid, number, idx, ts, role, text, json.dumps(meta), native))

            if record.get('source') == 'USER_EXPLICIT' and record.get('type') == 'USER_INPUT':
                content = record.get('content')
                if isinstance(content, str) and content.lstrip().startswith('<USER_REQUEST>'):
                    body, end_tag, _ = content.lstrip()[len('<USER_REQUEST>'):].partition('</USER_REQUEST>')
                    content = body if end_tag else ''
                if isinstance(content, str) and content.strip():
                    insert(-1, 'user', redact_text(content), {'raw_ref': ref})
            elif record.get('source') == 'MODEL' and record.get('type') == 'PLANNER_RESPONSE':
                if record.get('content') or record.get('tool_calls'):
                    insert(-1, 'assistant', None, {'raw_ref': ref})
                for idx, marker in agy_skill_file_reads(record, number, cwd):
                    call = record['tool_calls'][idx]
                    meta = {'trace_markers': [marker], 'raw_ref': marker['raw_ref'],
                            'trace_context': {'cwd': cwd, 'basis': 'transcript_tool_call'}}
                    call_id = call.get('id') or call.get('call_id')
                    if isinstance(call_id, str):
                        meta['tool_call_id'] = redact_text(call_id)[:128]
                    native = native_calls.get((step, idx, call['name']))
                    insert(idx, 'tool', None, meta, native)
        metadata = capture.verify()
    return dict(status='partial' if metadata['partial'] or metadata['index_coverage'] == 'partial' else 'captured',
                prefix_hash=metadata['prefix_hash'], captured_through=metadata['captured_through'],
                trailing_bytes=metadata['trailing_bytes'], projection=metadata['index_coverage'],
                transcript_mtime_ns=capture.initial.st_mtime_ns)


def refresh_transcripts(indexer):
    from .agy import _ID
    conn = indexer.conn
    with indexer.lock, conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE IF NOT EXISTS agy_transcript_state('
                     'session_id TEXT PRIMARY KEY, signature TEXT, coverage_json TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS agy_transcript_rows('
                     'session_id TEXT,line_no INTEGER,call_index INTEGER,ts_ms INTEGER,role TEXT,text TEXT,'
                     'activity_meta_json TEXT,message_index INTEGER,PRIMARY KEY(session_id,line_no,call_index))')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_transcript_time ON agy_transcript_rows(ts_ms,session_id)')
        state = {}
        for row in conn.execute('SELECT id,cwd,file_signature FROM sessions ORDER BY id'):
            sid = row['id']
            if not _ID.fullmatch(sid):
                raise ValueError('invalid_agy_session_id')
            signature = None
            previous = conn.execute('SELECT signature,coverage_json FROM agy_transcript_state WHERE session_id=?',
                                    (sid,)).fetchone()
            try:
                path = _transcript(indexer.root, sid)
                signature = json.dumps([TRANSCRIPT_VERSION, PROJECTION_VERSION, stat_signature(path.stat()), row['file_signature']])
                if previous and previous['signature'] == signature:
                    state[sid] = json.loads(previous['coverage_json'])
                    continue
                conn.execute('SAVEPOINT transcript_rows')
                try:
                    conn.execute('DELETE FROM agy_transcript_rows WHERE session_id=?', (sid,))
                    native = {}
                    for msg in conn.execute("SELECT message_index,activity_meta_json FROM messages "
                                            "WHERE session_id=? AND kind='tool_use'", (sid,)):
                        loc = json.loads(msg['activity_meta_json'] or '{}').get('raw_ref') or {}
                        if 'call_index' in loc:
                            key = (loc['step_index'], loc['call_index'], loc['tool_name'])
                            native[key] = None if key in native else msg['message_index']
                    coverage = _store_rows(conn, indexer.root, path, sid, row['cwd'] or '', native)
                    _transcript(indexer.root, sid)
                except Exception:
                    conn.execute('ROLLBACK TO transcript_rows')
                    raise
                finally:
                    conn.execute('RELEASE transcript_rows')
            except (OSError, ValueError) as exc:
                coverage = dict(status='missing_transcript' if isinstance(exc, FileNotFoundError)
                                else 'capture_error', error=str(exc)[:300])
                if previous:
                    last = json.loads(previous['coverage_json'])
                    if last.get('prefix_hash'):
                        coverage.update(prefix_hash=last['prefix_hash'], captured_through=last['captured_through'],
                                        retained_previous_prefix=True)
                signature = None
            coverage['parser_version'] = TRANSCRIPT_VERSION
            coverage['projection_version'] = PROJECTION_VERSION
            conn.execute('INSERT OR REPLACE INTO agy_transcript_state VALUES(?,?,?)',
                         (sid, signature, json.dumps(coverage)))
            state[sid] = coverage
        for table in ('agy_transcript_state', 'agy_transcript_rows'):
            conn.execute('DELETE FROM ' + table + ' WHERE session_id NOT IN (SELECT id FROM sessions)')
        native_revision = conn.execute("SELECT value FROM reader_state WHERE key='agy_revision'").fetchone()[0]
        revision = hashlib.sha256(json.dumps([native_revision, TRANSCRIPT_VERSION, state],
                                             sort_keys=True).encode()).hexdigest()
        conn.execute("UPDATE reader_state SET value=? WHERE key='revision'", (revision,))
    return dict(Counter(item['status'] for item in state.values()))
