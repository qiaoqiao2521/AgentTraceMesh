"""AGY native histories: read-only source databases, disposable memory index.

Register AgyIndexer(<root>/conversation_summaries.db) as a native source. The
CLI root (~/.gemini/antigravity-cli) and desktop root (~/.gemini/antigravity)
are distinct stores. source_revision() includes conversation databases/WALs;
callers must use it when binding multi-page results to a source revision.
"""
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit

from audit.agy import MAX_STEP_BYTES, decode_agy_step
from .sources import (Indexer, _claude_format_tool_result, _claude_format_tool_use,
                      _claude_summarize_tool_use, parse_ts)
from .titles import is_context_title, meaningful_user_title

MAX_SESSION_BYTES = 64 * 1024 * 1024
MAX_SESSION_STEPS = 20000
MAX_STORE_SESSIONS = 10000
MAX_AUDIT_BYTES = 2 * 1024 * 1024
_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


class _ReadBudget:
    def __init__(self, limit):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError('invalid_agy_read_budget')
        self.limit = self.remaining = limit


class _AgySourceLimitError(ValueError):
    def __init__(self, path, *, reason, size_bytes=None, limit_bytes=None,
                 budget_remaining_bytes=None, **details):
        self.source_error = {'error_code': 'agy_source_limit_exceeded', 'path': str(path),
                             'size_bytes': size_bytes, 'limit_bytes': limit_bytes,
                             'budget_remaining_bytes': budget_remaining_bytes,
                             'reason': reason, **details}
        super().__init__('agy_source_limit_exceeded: ' + json.dumps(self.source_error, ensure_ascii=False))


def _session_source_error(path, error):
    if isinstance(error, _AgySourceLimitError):
        return dict(error.source_error)
    from .reuse import error_code
    try:
        size_bytes = sum(source.stat().st_size for source in _database_paths(path) if source.exists())
    except OSError:
        size_bytes = None
    return {'error_code': error_code(error), 'path': str(path), 'size_bytes': size_bytes,
            'limit_bytes': None, 'budget_remaining_bytes': None, 'reason': 'session_read_failed'}


def _signature(paths):
    parts = []
    for path in paths:
        if path.is_symlink():
            raise ValueError('source_symlink_not_allowed')
        try:
            st = path.stat()
            parts.append((str(path), st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_dev, st.st_ino))
        except FileNotFoundError:
            parts.append((str(path), None))
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def _database_paths(path):
    return (path, Path(str(path) + '-wal'))


class _SnapshotConnection(sqlite3.Connection):
    def close(self):
        try:
            super().close()
        finally:
            snapshot = getattr(self, '_snapshot', None)
            if snapshot:
                snapshot.cleanup()
                self._snapshot = None


def _read_only(path, *, budget):
    """SQLite WAL readers write shm read marks, even with mode=ro.

    Copy bounded DB/WAL bytes into a private, auto-cleaned directory first.
    SQLite validates/replays committed WAL frames itself, without opening any
    source file for writing. No persistent transcript copy is retained.
    """
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('source_symlink_not_allowed')
    sources = _database_paths(path)
    before = _signature(sources)
    # Check the combined physical DB + WAL size before copying either file.
    # The caller shares this budget across summary and conversation snapshots.
    sizes = {}
    for original in sources:
        if not original.exists() and original != path:
            continue
        source_stat = original.stat()
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError('source_regular_file_required')
        sizes[original] = source_stat.st_size
    if sum(sizes.values()) > budget.remaining:
        raise _AgySourceLimitError(path, reason='physical_source_bytes', size_bytes=sum(sizes.values()),
                                   limit_bytes=budget.limit, budget_remaining_bytes=budget.remaining)
    snapshot = tempfile.TemporaryDirectory(prefix='history-agy-')
    copied = Path(snapshot.name) / 'snapshot.db'
    initial_remaining = budget.remaining
    try:
        for original, target in zip(sources, _database_paths(copied)):
            if original not in sizes:
                continue
            descriptor = os.open(original, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(descriptor, 'rb') as stream, target.open('wb') as output:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError('source_regular_file_required')
                remaining = sizes[original]
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('source_changed_during_read')
                    output.write(chunk)
                    remaining -= len(chunk)
                    budget.remaining -= len(chunk)
        if _signature(sources) != before:
            raise ValueError('source_changed_during_read')
        db = sqlite3.connect(copied.as_uri() + '?mode=ro', uri=True, factory=_SnapshotConnection)
        db._snapshot = snapshot
        db._source_bytes = initial_remaining - budget.remaining
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return db
    except Exception:
        snapshot.cleanup()
        raise


def _workspace(value):
    try:
        values = json.loads(value or '[]')
    except (ValueError, TypeError):
        return ''
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        return ''  # Do not collapse a multi-workspace session into a guessed cwd.
    uri = urlsplit(values[0])
    if uri.scheme == 'file' and uri.netloc in ('', 'localhost'):
        return unquote(uri.path)
    return values[0] if not uri.scheme and Path(values[0]).is_absolute() else ''


def _summary(path, session_id, budget):
    summary_path = path.parent.parent / 'conversation_summaries.db'
    if not summary_path.is_file():
        return {}
    db = _read_only(summary_path, budget=budget)
    try:
        row = db.execute('SELECT conversation_id,title,step_count,last_modified_time,workspace_uris '
                         'FROM conversation_summaries WHERE conversation_id=?', (session_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        db.close()


def _session_title(summary, session_id, messages=()):
    title = summary.get('title')
    fallback = 'AGY ' + session_id[:8]
    if not is_context_title(title) and title not in (session_id, fallback):
        return title
    for message in messages:
        if message['role'] == 'user' and message['kind'] == 'message':
            candidate = meaningful_user_title(message['text'])
            if candidate:
                return candidate
    return fallback


def _session_data(path, summary):
    session_id = path.stem
    return {'id': session_id, 'file_path': str(path), 'cwd': _workspace(summary.get('workspace_uris')),
            'title': _session_title(summary, session_id),
            'start_ts_ms': 0, 'end_ts_ms': parse_ts(summary.get('last_modified_time')) or 0,
            'message_count': 0, 'messages': [], 'search_blob': '', 'usage': None,
            'usage_status': 'unsupported_token_accounting', 'content_status': 'decoded_text',
            'unsupported_steps': [], 'records': [], 'source_bytes': 0, 'payload_bytes': 0}


def read_agy_session(path, *, summary=None, max_bytes=MAX_SESSION_BYTES):
    """Decode one bounded SQLite snapshot; legacy .pb stays metadata-only.

    No brain directory, current project files, logs, or credentials are read.
    Unsupported formats/types are surfaced, never scanned for printable text.
    max_bytes caps combined physical source I/O: summary DB/WAL, when needed,
    plus conversation DB/WAL. payload_bytes separately reports decoded BLOB
    bytes. SQLite page overhead and other tables also consume the I/O budget.
    """
    path = Path(path)
    session_id = path.stem
    if not _ID.fullmatch(session_id):
        raise ValueError('invalid_agy_session_id')
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('source_symlink_not_allowed')
    budget = _ReadBudget(max_bytes)
    summary = _summary(path, session_id, budget) if summary is None else summary
    result = _session_data(path, summary)
    result['source_bytes'] = budget.limit - budget.remaining
    if path.suffix != '.db' or not path.is_file():
        result['content_status'] = 'unsupported_legacy_protobuf' if path.suffix == '.pb' else 'source_missing'
        result['messages'] = [{'ts_ms': result['end_ts_ms'], 'role': 'other', 'kind': 'status',
                               'text': 'AGY metadata only: ' + result['content_status'] + '. Full text is unavailable.'}]
        result['search_blob'] = str(result['title'])
        return result
    before = _signature(_database_paths(path))
    db = _read_only(path, budget=budget)
    result['source_bytes'] = budget.limit - budget.remaining
    messages = result['messages']
    seen_tools = set()
    try:
        db.execute('BEGIN')
        sizes = db.execute('SELECT COUNT(*),COALESCE(SUM(length(step_payload)),0),'
                           'COALESCE(MAX(length(step_payload)),0) FROM steps').fetchone()
        if sizes[0] > MAX_SESSION_STEPS:
            raise _AgySourceLimitError(path, reason='session_steps', step_count=sizes[0],
                                       limit_steps=MAX_SESSION_STEPS)
        if sizes[1] > max_bytes:
            raise _AgySourceLimitError(path, reason='session_payload_bytes', size_bytes=sizes[1],
                                       limit_bytes=max_bytes)
        if sizes[2] > MAX_STEP_BYTES:
            raise _AgySourceLimitError(path, reason='step_payload_bytes', size_bytes=sizes[2],
                                       limit_bytes=MAX_STEP_BYTES)
        result['payload_bytes'] = sizes[1]
        for row in db.execute('SELECT idx,step_type,status,step_format,step_payload FROM steps ORDER BY idx'):
            if row['step_format'] != 0:
                result['unsupported_steps'].append({'step_index': row['idx'], 'reason': 'unsupported_step_format'})
                continue
            try:
                step = decode_agy_step(row['step_payload'], step_type=row['step_type'], status=row['status'])
            except (ValueError, UnicodeError):
                result['unsupported_steps'].append({'step_index': row['idx'], 'reason': 'invalid_step_payload'})
                continue
            if not step['supported']:
                result['unsupported_steps'].append({'step_index': row['idx'], 'reason': 'unsupported_step_type',
                                                   'step_type': row['step_type']})
                continue
            timestamp = step['timestamp']
            if timestamp:
                result['start_ts_ms'] = min(result['start_ts_ms'] or timestamp, timestamp)
                result['end_ts_ms'] = max(result['end_ts_ms'], timestamp)
            for record in step['records']:
                record.update(sessionId=session_id, cwd=result['cwd'], step_index=row['idx'])
                if record['type'] == 'summary':
                    messages.append({'ts_ms': timestamp, 'role': 'other', 'kind': 'summary', 'text': record['summary']})
                    result['records'].append(record)
                    continue
                content = []
                role = record['message']['role']
                for item in record['message']['content']:
                    kind = item['type']
                    tool_summary = None
                    mapped_role = role if role in ('assistant', 'user') else 'other'
                    if kind == 'tool_use':
                        if item.get('id') and item['id'] in seen_tools:
                            continue
                        if item.get('id'):
                            seen_tools.add(item['id'])
                        text = _claude_format_tool_use(item)
                        tool_summary = _claude_summarize_tool_use(item)
                        mapped_role = 'tool'
                    elif kind == 'tool_result':
                        text = _claude_format_tool_result(item)
                        mapped_role = 'tool'
                    elif kind == 'thinking':
                        text = item['thinking']
                        mapped_role = 'other'
                    else:
                        text = item['text']
                        kind = 'message'
                        if role in ('assistant', 'user'):
                            result['message_count'] += 1
                    content.append(item)
                    message = {'ts_ms': timestamp, 'role': mapped_role, 'kind': kind, 'text': text,
                               'step_index': row['idx'], 'native_status': step['status']}
                    if tool_summary:
                        message['tool_summary'] = tool_summary
                    messages.append(message)
                record['message']['content'] = content
                if content:
                    result['records'].append(record)
    finally:
        db.close()
    if _signature(_database_paths(path)) != before:
        raise ValueError('source_changed_during_read')
    if result['unsupported_steps']:
        result['content_status'] = 'partial_unsupported_steps'
        messages.append({'ts_ms': result['end_ts_ms'], 'role': 'other', 'kind': 'status',
                         'text': 'AGY partial history: %d unsupported or invalid steps; full text is incomplete.' % len(result['unsupported_steps'])})
    result['title'] = _session_title(summary, session_id, messages)
    result['search_blob'] = '\n'.join(message['text'] for message in messages)
    return result


def parse_agy_session_file(path, display_root=None):
    del display_root
    result = read_agy_session(path)
    result.pop('records', None)
    return result


class AgyIndexer(Indexer):
    """Reuse the mature indexed reader, with an in-memory derived database.

    Every SQLite open uses a private mode=ro snapshot. Mutation APIs remain disabled. Freshness
    observes both summary metadata and per-session DB/WAL changes.
    """
    def __init__(self, db_path, source='agy'):
        self.db_path = Path(db_path).expanduser().absolute()
        self.root = self.db_path.parent
        if self.db_path.is_symlink() or self.root.is_symlink():
            raise ValueError('source_symlink_not_allowed')
        with self.db_path.open('rb'):
            pass
        self.source = source
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.recall_titles = None
        self._session_preview_cache = OrderedDict()
        self._local_title_backfill_done = True
        self.last_scan = 0
        self.scan_interval = 5
        self._revision = None
        self._coverage = {}
        self._init_db()
        try:
            self.scan_sessions()
        except Exception:
            self.conn.close()
            raise

    def source_revision(self):
        directory = self.root / 'conversations'
        if directory.is_symlink():
            raise ValueError('source_symlink_not_allowed')
        paths = list(_database_paths(self.db_path))
        if directory.is_dir():
            for path in directory.iterdir():
                if path.suffix in ('.db', '.pb') or path.name.endswith('.db-wal'):
                    paths.append(path)
                    if len(paths) > MAX_STORE_SESSIONS * 3 + 2:
                        raise ValueError('agy_store_limit_exceeded')
        return _signature(sorted(paths))

    def scan_sessions(self):
        revision = self.source_revision()
        if revision == self._revision:
            return
        # Bound this snapshot to the observed summary size, not an arbitrary
        # cumulative store cap. Each conversation is copied and cleaned alone.
        summary_bytes = sum(path.stat().st_size for path in _database_paths(self.db_path) if path.exists())
        store_budget = _ReadBudget(summary_bytes)
        source = _read_only(self.db_path, budget=store_budget)
        try:
            if source.execute('SELECT COUNT(*) FROM conversation_summaries').fetchone()[0] > MAX_STORE_SESSIONS:
                raise ValueError('agy_store_limit_exceeded')
            summaries = source.execute('SELECT conversation_id,title,step_count,last_modified_time,workspace_uris '
                                       'FROM conversation_summaries ORDER BY conversation_id').fetchall()
        finally:
            source.close()
        coverage = {}
        with self.lock, self.conn:
            self.conn.execute('DELETE FROM messages')
            self.conn.execute('DELETE FROM sessions')
            for row in summaries:
                sid = row['conversation_id']
                if not isinstance(sid, str) or not _ID.fullmatch(sid):
                    raise ValueError('invalid_agy_session_id')
                path = self.root / 'conversations' / (sid + '.db')
                if not path.exists() and path.with_suffix('.pb').exists():
                    path = path.with_suffix('.pb')
                try:
                    data = read_agy_session(path, summary=dict(row), max_bytes=MAX_SESSION_BYTES)
                except (OSError, ValueError, sqlite3.Error) as error:
                    data = _session_data(path, dict(row))
                    data['content_status'] = 'partial_source_error'
                    data['source_error'] = _session_source_error(path, error)
                    data['messages'] = [{'ts_ms': data['end_ts_ms'], 'role': 'other', 'kind': 'status',
                                         'text': 'AGY metadata only: ' + data['source_error']['error_code']
                                                 + '. Full text is unavailable.'}]
                    data['search_blob'] = str(data['title'])
                coverage[sid] = {key: data[key] for key in ('content_status', 'unsupported_steps', 'usage_status')}
                if 'source_error' in data:
                    coverage[sid]['source_error'] = data['source_error']
                self.conn.execute('INSERT INTO sessions(id,file_path,start_ts_ms,end_ts_ms,cwd,title,message_count,search_blob,pinned) '
                                  'VALUES(?,?,?,?,?,?,?,?,0)', (sid, str(path), data['start_ts_ms'], data['end_ts_ms'], data['cwd'],
                                                               data['title'], data['message_count'], data['search_blob']))
                self.conn.executemany('INSERT INTO messages(session_id,ts_ms,role,kind,text,tool_summary_json) VALUES(?,?,?,?,?,?)',
                                      [(sid, item['ts_ms'], item['role'], item['kind'], item['text'],
                                        json.dumps(item['tool_summary']) if item.get('tool_summary') else None) for item in data['messages']])
            if self.source_revision() != revision:
                raise ValueError('source_changed_during_refresh')
            self.conn.execute("UPDATE reader_state SET value=? WHERE key='revision'", (revision,))
        self._coverage = coverage
        self._revision = revision
        self._clear_session_preview_cache()
        self.last_scan = time.time()

    def get_session_metadata(self, session_id):
        metadata = super().get_session_metadata(session_id)
        if metadata:
            metadata.update(self._coverage.get(str(session_id), {}))
        return metadata

    def coverage(self):
        statuses = [entry['content_status'] for entry in self._coverage.values()]
        errors = [{'session_id': sid, **entry['source_error']} for sid, entry in self._coverage.items()
                  if 'source_error' in entry]
        return {'total_sessions': len(statuses), 'decoded_text_sessions': statuses.count('decoded_text'),
                'metadata_only_sessions': sum(s in ('unsupported_legacy_protobuf', 'source_missing', 'partial_source_error')
                                              for s in statuses),
                'legacy_protobuf_sessions': statuses.count('unsupported_legacy_protobuf'),
                'missing_source_sessions': statuses.count('source_missing'),
                'partial_sessions': sum(s.startswith('partial_') for s in statuses),
                'failed_sessions': len(errors), 'session_errors': errors,
                'unsupported_steps': sum(len(entry['unsupported_steps']) for entry in self._coverage.values()),
                'usage_status': 'unsupported_token_accounting', 'decoded_scope': 'text_thinking_tool_fields'}

    def list_sessions_page(self, *args, **kwargs):
        page = super().list_sessions_page(*args, **kwargs)
        for row in page['items']:
            row.update(self._coverage.get(row['id'], {}))
        return page

    def build_session_audit(self, session_id):
        from audit.extractor import _claude_events, build_audit_from_events
        if not _ID.fullmatch(str(session_id)):
            return None
        path = self.root / 'conversations' / (str(session_id) + '.db')
        data = read_agy_session(path, max_bytes=MAX_AUDIT_BYTES)
        if data['content_status'] != 'decoded_text':
            return None
        events = []
        for record in data['records']:
            # line_no is an internal ordering input only. Native evidence refs
            # below expose the exact SQLite step index, never a fake JSONL line.
            events.extend(_claude_events(record, record['step_index'] + 1))
        audit = build_audit_from_events(events, session_id=str(session_id), source=self.source,
                                        started_at=data['start_ts_ms'], ended_at=data['end_ts_ms']).to_dict()
        for evidence in audit.get('evidence', []):
            ref = evidence.get('raw_ref') or {}
            if 'line_no' in ref:
                evidence['raw_ref'] = {'step_index': ref['line_no'] - 1, 'table': 'steps'}
        return audit

    def query_usage(self, *args, **kwargs):
        return {'totals': {'session_count': 0, 'input': 0, 'output': 0, 'cached': 0, 'reasoning': 0, 'total': 0},
                'has_usage_data': False, 'status': 'unsupported_token_accounting',
                'by_day': [], 'by_project': [], 'top_sessions': []}

    def rename_session(self, *args, **kwargs):
        return False

    def pin_session(self, *args, **kwargs):
        return False

    def archive_session(self, *args, **kwargs):
        return False, 'unsupported'

    def delete_project_sessions(self, *args, **kwargs):
        return False, 'unsupported', 0, None

    def cleanup_weak_sessions(self, *args, **kwargs):
        return False, 'unsupported', 0, None
