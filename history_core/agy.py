"""AGY native histories: read-only sources and a disposable derived index.

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
AGY_CACHE_VERSION = 1
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


def _title_candidates(summary, messages=()):
    candidates = []
    if isinstance(summary.get('title'), str) and summary['title'].strip():
        candidates.append({'source': 'summary', 'title': summary['title']})
    for message in messages:
        if message['role'] == 'user' and message['kind'] == 'message':
            title = meaningful_user_title(message['text'])
            if title:
                candidates.append({'source': 'first_user_message', 'title': title})
                break
    return candidates


def _session_data(path, summary):
    session_id = path.stem
    return {'id': session_id, 'file_path': str(path), 'cwd': _workspace(summary.get('workspace_uris')),
            'title': _session_title(summary, session_id), 'title_candidates': _title_candidates(summary),
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
    result['title_candidates'] = _title_candidates(summary, messages)
    result['search_blob'] = '\n'.join(message['text'] for message in messages)
    return result


def parse_agy_session_file(path, display_root=None):
    del display_root
    result = read_agy_session(path)
    result.pop('records', None)
    return result


class AgyIndexer(Indexer):
    """Reuse the indexed reader; an explicit data_dir enables cross-process reuse.

    Every SQLite open uses a private mode=ro snapshot. Mutation APIs remain disabled. Freshness
    observes both summary metadata and per-session DB/WAL changes.
    """
    def __init__(self, db_path, source='agy', data_dir=None):
        self.db_path = Path(db_path).expanduser().absolute()
        self.root = self.db_path.parent
        if self.db_path.is_symlink() or self.root.is_symlink():
            raise ValueError('source_symlink_not_allowed')
        with self.db_path.open('rb'):
            pass
        self.source = source
        self.lock = threading.Lock()
        self.parser_version = AGY_CACHE_VERSION
        self.recall_titles = None
        self._session_preview_cache = OrderedDict()
        self._local_title_backfill_done = True
        self.last_scan = 0
        self.scan_interval = 5
        self._revision = None
        self._coverage = {}
        self._cache_valid = False
        self.cache_path = None
        self._cache_status = 'memory'
        self._cache_warning = None
        if data_dir is not None:
            directory = Path(data_dir).expanduser().absolute()
            if any(path.is_symlink() for path in (directory, *directory.parents)):
                raise ValueError('cache_symlink_not_allowed')
            resolved_source = self.root.resolve()
            resolved_cache = directory.resolve()
            if resolved_cache == resolved_source or resolved_cache in resolved_source.parents or resolved_source in resolved_cache.parents:
                raise ValueError('source_and_cache_must_be_disjoint')
            identity = hashlib.sha256((str(self.db_path) + '\0' + self.source).encode()).hexdigest()[:24]
            self.cache_path = directory / ('agy-' + identity + '.sqlite')
            if any(path.is_symlink() for path in (self.cache_path, Path(str(self.cache_path) + '-wal'),
                                                  Path(str(self.cache_path) + '-shm'), Path(str(self.cache_path) + '-journal'))):
                raise ValueError('cache_symlink_not_allowed')
            try:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.conn = sqlite3.connect(self.cache_path, check_same_thread=False)
                self.conn.row_factory = sqlite3.Row
                for attempt in range(2):
                    try:
                        self._init_cache()
                        break
                    except sqlite3.OperationalError as error:
                        raced = (getattr(error, 'sqlite_errorcode', None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                                 or str(error).startswith('duplicate column name:'))
                        if attempt or not raced:
                            raise
                        # Two first-use processes can collide when enabling WAL;
                        # its PRAGMA can return BUSY without honoring busy_timeout,
                        # or both can observe a missing column before migrating it.
                        self.conn.rollback()
                        time.sleep(0.05)
                self.cache_path.chmod(0o600)
                self._load_cache()
            except (OSError, sqlite3.Error) as error:
                if hasattr(self, 'conn'):
                    self.conn.close()
                self._cache_warning = 'agy_cache_unavailable: ' + str(error)
                self._cache_status = 'fallback_memory'
                self._memory_cache()
        else:
            self._memory_cache()
        try:
            self.scan_sessions()
        except Exception:
            self.conn.close()
            raise

    def _memory_cache(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_cache()

    def _init_cache(self):
        # A warm reader must not acquire a writer lock merely to repeat schema
        # initialization while another process refreshes the derived database.
        if self._cache_schema_ready():
            self.conn.execute('PRAGMA synchronous=NORMAL')
            return
        self._init_db()
        with self.conn:
            self.conn.execute('CREATE TABLE IF NOT EXISTS agy_cache_sessions '
                              '(session_id TEXT PRIMARY KEY, signature TEXT NOT NULL, coverage_json TEXT NOT NULL)')
            self.conn.execute('INSERT OR REPLACE INTO reader_state VALUES(?,?)',
                              ('agy_schema_version', str(AGY_CACHE_VERSION)))

    def _cache_schema_ready(self):
        try:
            version = self.conn.execute("SELECT value FROM reader_state WHERE key='agy_schema_version'").fetchone()
            if not version or version[0] != str(AGY_CACHE_VERSION):
                return False
            required = {'sessions': {'id', 'file_path', 'message_index_count', 'activity_metadata_json'},
                        'messages': {'session_id', 'message_index', 'activity_ts_ms', 'activity_meta_json'},
                        'agy_cache_sessions': {'session_id', 'signature', 'coverage_json'}}
            return (self.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal' and
                    all(columns <= {row[1] for row in self.conn.execute('PRAGMA table_info(' + table + ')')}
                        for table, columns in required.items()) and
                    any(row[1] == 'idx_messages_search_scope'
                        for row in self.conn.execute('PRAGMA index_list(messages)')))
        except sqlite3.OperationalError:
            return False  # New or older derived schema: reuse the existing migrations.

    def _load_cache(self):
        self.conn.execute('BEGIN')
        try:
            self._load_cache_state()
        finally:
            self.conn.rollback()

    def _load_cache_state(self):
        state = dict(self.conn.execute("SELECT key,value FROM reader_state WHERE key LIKE 'agy_%'"))
        if (state.get('agy_version') != str(AGY_CACHE_VERSION) or state.get('agy_source') != str(self.db_path)
                or state.get('agy_identity') != self.source):
            return
        try:
            coverage = {row['session_id']: json.loads(row['coverage_json'])
                        for row in self.conn.execute('SELECT session_id,coverage_json FROM agy_cache_sessions')}
            sessions = self.conn.execute('SELECT id,file_path FROM sessions').fetchall()
            ids = {row['id'] for row in sessions}
            if ids != set(coverage) or any(not isinstance(item, dict) or
                    not isinstance(item.get('content_status'), str) or
                    not isinstance(item.get('unsupported_steps'), list) for item in coverage.values()):
                raise ValueError('incomplete_agy_cache_metadata')
            for row in sessions:
                if not isinstance(row['id'], str) or not _ID.fullmatch(row['id']) or row['file_path'] not in (
                        str(self.root / 'conversations' / (row['id'] + '.db')),
                        str(self.root / 'conversations' / (row['id'] + '.pb'))):
                    raise ValueError('invalid_agy_cached_file_path')
            self._revision = state['agy_revision']
        except (ValueError, KeyError, TypeError):
            self._cache_warning = 'agy_cache_metadata_invalid: rebuilding derived index'
            return
        self._coverage = coverage
        self._cache_valid = True

    def cache_info(self):
        return {'mode': 'persistent' if self.cache_path is not None and self._cache_status != 'fallback_memory' else 'memory',
                'path': str(self.cache_path) if self.cache_path is not None else None,
                'status': self._cache_status, 'warning': self._cache_warning, 'version': AGY_CACHE_VERSION}

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
        for attempt in range(2):
            revision = self.source_revision()
            if revision == self._revision:
                if self.cache_info()['mode'] == 'persistent':
                    self._cache_status = 'warm'
                self.last_scan = time.time()
                return
            try:
                self._scan_sessions_once(revision)
                return
            except (OSError, ValueError, sqlite3.Error) as error:
                changed = str(error).startswith(('source_changed_during_read', 'source_changed_during_refresh'))
                if isinstance(error, OSError):
                    try:
                        changed = self.source_revision() != revision
                    except OSError:
                        changed = False  # Cannot attest a changed source; preserve the original failure.
                if isinstance(error, _AgySourceLimitError) and error.source_error['path'] == str(self.db_path):
                    # The summary budget came from a preceding stat observation.
                    # A static oversized conversation never enters this retry.
                    changed = self.source_revision() != revision
                if not changed:
                    raise
                if attempt:
                    context = {'snapshot_attempts': 2, 'path': str(self.db_path),
                               'observed_revision': revision, 'cause': str(error)}
                    if isinstance(error, _AgySourceLimitError):
                        error.source_error.update(snapshot_attempts=2, observed_revision=revision)
                        error.args = ('agy_source_limit_exceeded: ' + json.dumps(error.source_error),)
                        raise
                    raise ValueError('source_changed_during_refresh: ' + json.dumps(context)) from error

    def _scan_sessions_once(self, revision):
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
        was_valid = self._cache_valid
        with self.lock, self.conn:
            # Serialize writers, while WAL readers keep the preceding complete index.
            self.conn.execute('BEGIN IMMEDIATE')
            cached = {row['session_id']: row for row in self.conn.execute('SELECT * FROM agy_cache_sessions')} if was_valid else {}
            session_files = set()
            for row in summaries:
                sid = row['conversation_id']
                if not isinstance(sid, str) or not _ID.fullmatch(sid):
                    raise ValueError('invalid_agy_session_id')
                path = self.root / 'conversations' / (sid + '.db')
                if not path.exists() and path.with_suffix('.pb').exists():
                    path = path.with_suffix('.pb')
                session_files.add(path)
                signature = _signature(_database_paths(path)) + ':' + json.dumps(dict(row), sort_keys=True)
                if sid in cached and cached[sid]['signature'] == signature:
                    coverage[sid] = json.loads(cached[sid]['coverage_json'])
                    continue
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
                coverage[sid] = {key: data[key] for key in ('content_status', 'unsupported_steps', 'usage_status', 'title_candidates')}
                if 'source_error' in data:
                    coverage[sid]['source_error'] = data['source_error']
                previous = self.conn.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
                self._store_parsed_session(previous, data, 0, signature, path, session_files, build_audits=False)
                self.conn.execute('INSERT OR REPLACE INTO agy_cache_sessions VALUES(?,?,?)',
                                  (sid, signature, json.dumps(coverage[sid])))
            for sid, in self.conn.execute('SELECT id FROM sessions').fetchall():
                if sid not in coverage:
                    self.conn.execute('DELETE FROM messages WHERE session_id=?', (sid,))
                    self.conn.execute('DELETE FROM sessions WHERE id=?', (sid,))
                    self.conn.execute('DELETE FROM agy_cache_sessions WHERE session_id=?', (sid,))
            after_revision = self.source_revision()
            if after_revision != revision:
                raise ValueError('source_changed_during_refresh: ' + json.dumps(
                    {'source_revision_before': revision, 'source_revision_after': after_revision}))
            self.conn.execute("UPDATE reader_state SET value=? WHERE key='revision'", (revision,))
            self.conn.executemany('INSERT OR REPLACE INTO reader_state VALUES(?,?)',
                                  [('agy_version', str(AGY_CACHE_VERSION)), ('agy_source', str(self.db_path)), ('agy_identity', self.source),
                                   ('agy_revision', revision)])
        self._coverage = coverage
        self._revision = revision
        self._cache_valid = True
        if self.cache_info()['mode'] == 'persistent':
            self._cache_status = 'updated' if was_valid else 'rebuilt'
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
