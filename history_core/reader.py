"""Public machine capabilities; legacy Web adapters stay private to this facade.

This is an application API boundary, not a sandbox against hostile Python code.
Only explicitly selected sources and a disjoint derived cache are used.
"""
import hashlib
import os
import shlex
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from . import service
from .providers import SOURCES, NATIVE_SOURCES, canonical_source, parser_for, parser_version, include_file, file_suffixes, native_indexer
from .sources import (Indexer, OpenCodeIndexer, HermesStateIndexer,
                      parse_codex_session_file, parse_claude_session_file,
                      parse_openclaw_session_file)


class HistoryReader:
    """Explicit refresh and reads, with no forwarded legacy mutation methods."""

    def __init__(self, source, source_path, data_dir=None, *, validate_tree=True):
        source = canonical_source(source)
        if source not in SOURCES:
            raise ValueError('unsupported_source')
        self.__source = source
        self.__root = Path(source_path).resolve(strict=True)
        self.__native = source in NATIVE_SOURCES
        self.__indexer = None
        self.__cache = None
        self.__page_revision = None
        if validate_tree:
            self.__validate_source()
        else:
            self.__check_root()
        if self.__native:
            self.__indexer = native_indexer(source, self.__root)
        else:
            if data_dir is None:
                raise ValueError('sessions_directory_and_data_dir_required')
            data = Path(data_dir).resolve()
            if data == self.__root or data in self.__root.parents or self.__root in data.parents:
                raise ValueError('source_and_cache_must_be_disjoint')
            # A source-bound subdirectory cannot accidentally reuse another
            # source's cached file paths or the Web's mutable/pinned index.
            key = hashlib.sha256(str(self.__root).encode('utf-8')).hexdigest()
            self.__cache = data / ('machine-%s-%s' % (source, key))
            self.__validate_cache()
            self.__cache.mkdir(parents=True, exist_ok=True)
            self.__indexer = Indexer(self.__root, self.__cache, source,
                parse_file_fn=parser_for(source), file_filter_fn=self.__include,
                parser_version=parser_version(source))

    def __include(self, path):
        return include_file(self.__source, path)

    def __validate_cache(self):
        if self.__cache is None:
            return
        # SQLite may write journal/WAL sidecars: reject linked artifacts too.
        if self.__cache.is_symlink():
            raise ValueError('cache_symlink_not_allowed')
        if self.__cache.exists():
            for p in self.__cache.rglob('*'):
                if p.is_symlink():
                    raise ValueError('cache_symlink_not_allowed')

    def __validate_source(self):
        if self.__root.is_symlink():
            raise ValueError('source_symlink_not_allowed')
        self.__root.stat()
        if self.__native:
            if not self.__root.is_file():
                raise ValueError('native_database_file_required')
            with self.__root.open('rb'):
                pass
            return
        if not self.__root.is_dir():
            raise ValueError('sessions_directory_required')
        def fail(error):
            raise error
        for directory, dirs, files in os.walk(self.__root, followlinks=False, onerror=fail):
            for name in dirs + files:
                p = Path(directory) / name
                if p.is_symlink():
                    raise ValueError('source_symlink_not_allowed')
            for name in files:
                p = Path(directory) / name
                if p.suffix in file_suffixes(self.__source) and self.__include(p):
                    with p.open('rb'):
                        pass

    def __ready(self):
        if self.__indexer is None:
            raise ValueError('reader_closed')
        self.__validate_source()
        self.__validate_cache()

    def refresh(self, *, paths=None, max_bytes=128 * 1024 * 1024):
        if self.__source == 'codex':
            if self.__indexer is None:
                raise ValueError('reader_closed')
            self.__root.stat()
            self.__check_root()
            self.__validate_cache()
            result = self.__indexer.refresh_capture(paths, max_bytes=max_bytes)
            result.update(status='partial' if result.get('partial') else 'refreshed',
                          native_source_read_only=True, index_revision=self.__revision())
            return result
        if paths is not None or max_bytes != 128 * 1024 * 1024:
            raise ValueError('capture_source_unsupported')
        self.__ready()
        self.__indexer.maybe_update_index(max_age_seconds=0)
        with self.__read_view() as view:
            result = self.__index_receipt(view)
            result.update(source=self.__source, status='partial' if result.get('partial') else 'refreshed',
                          native_source_read_only=True, index_revision=self.__view_revision(view))
        return result

    def __check_root(self):
        if self.__root.is_symlink():
            raise ValueError('source_symlink_not_allowed')
        if not (self.__root.is_file() if self.__native else self.__root.is_dir()):
            raise ValueError('native_database_file_required' if self.__native else 'sessions_directory_required')

    def health(self):
        if self.__indexer is None:
            raise ValueError('reader_closed')
        self.__check_root()
        if self.__native:
            if not self.__root.is_file():
                raise ValueError('native_database_file_required')
        else:
            if not self.__root.is_dir():
                raise ValueError('sessions_directory_required')
        self.__validate_cache()
        self.__indexer.conn.execute('SELECT 1').fetchone()
        result = {'source': self.__source, 'status': 'readable', 'freshness': 'unknown',
                  'background_refresh': False, 'refresh_policy': 'explicit',
                  'reader_module': str(Path(__file__).resolve()), 'parser_version': parser_version(self.__source)}
        with self.__read_view() as view:
            result.update(self.__index_receipt(view), index_revision=self.__view_revision(view))
        return result

    def __index_receipt(self, view):
        table = 'session' if self.__source in ('opencode', 'zcode') else 'sessions'
        with view.lock:
            count = view.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
        result = {'indexed_sessions': count, 'has_index': bool(count),
                  'index_state': 'populated' if count else 'source_empty' if self.__native else 'empty',
                  'refresh_required': not count and not self.__native}
        if not count and not self.__native:
            argv = [sys.executable, '-m', 'history_core', '--source', self.__source,
                    '--source-path', str(self.__root), '--data-dir', str(self.__cache.parent), 'refresh']
            result.update(partial=True, refresh_argv=argv,
                          refresh_command=subprocess.list2cmdline(argv) if os.name == 'nt' else shlex.join(argv),
                          warnings=[{'code': 'index_empty_refresh_required',
                                     'message': 'No sessions have been indexed; empty results do not establish absence in the source.'}])
            if self.__source == 'codex':
                result['selected_refresh_argv_template'] = argv + ['--file', 'SOURCE_RELATIVE_JSONL']
                result['refresh_scope_hint'] = 'For a large store, select known recordings with --file; keep this source root and data directory.'
        coverage = getattr(view, 'coverage', None)
        if callable(coverage):
            source_coverage = dict(coverage())
            failures = source_coverage.get('session_errors', [])
            if failures:
                source_coverage['session_errors'] = failures[:5]
                source_coverage['session_errors_truncated'] = len(failures) > 5
            result['source_coverage'] = source_coverage
            if source_coverage.get('partial_sessions') or source_coverage.get('metadata_only_sessions'):
                result['partial'] = True
        return result

    def __revision(self):
        if callable(getattr(self.__indexer, 'source_revision', None)):
            revision = self.__indexer.source_revision()
            if getattr(self.__indexer, '_revision', revision) != revision:
                raise ValueError('source_changed_since_index: refresh required')
        elif self.__native:
            parts = []
            for path in (self.__root, Path(str(self.__root) + '-wal')):
                try:
                    st = path.stat()
                    parts.append((st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino))
                except FileNotFoundError:
                    parts.append(None)
            revision = repr(parts)
        else:
            with self.__indexer.lock:
                revision = self.__indexer.conn.execute("SELECT value FROM reader_state WHERE key = 'revision'").fetchone()[0]
        return hashlib.sha256((self.__source + str(self.__root) + revision).encode()).hexdigest()

    @contextmanager
    def __read_view(self):
        if self.__native:
            yield self.__indexer
        else:
            from .read_view import indexed_read_view
            with indexed_read_view(self.__indexer) as view:
                yield view

    def __view_revision(self, view):
        if self.__native:
            return self.__revision()
        from .read_view import reader_revision
        return reader_revision(view)

    def __capture_coverage(self, view, result):
        from .indexed_materials import _source_capture
        from .reuse import query_budget
        with query_budget(view) as conn:
            for item in result['items']:
                row = conn.execute(
                    'SELECT SUBSTR(activity_metadata_json,1,8192) FROM sessions WHERE id=?',
                    (item['id'],)).fetchone()
                capture = _source_capture(row[0] if row else None)
                item['source_capture'] = capture
                if capture.get('partial'):
                    result['partial'] = True
        result['coverage'] = 'indexed_records_not_live_completeness'

    def search(self, *, query=None, limit=20, offset=0, cwd=None, index_revision=None, brief=False,
               session_id=None, role=None, field=None):
        if self.__indexer is None:
            raise ValueError('reader_closed')
        self.__check_root()
        self.__validate_cache()
        with self.__read_view() as view:
            before = self.__view_revision(view)
            expected = index_revision or (self.__page_revision if offset else None)
            if offset and not expected:
                raise ValueError('index_revision_required: restart from offset 0')
            if expected is not None and expected != before:
                raise ValueError('index_revision_changed: restart from offset 0')
            res = service.search(view, query=query, limit=limit, offset=offset, cwd=cwd, stable_order=True,
                                 brief=brief, session_id=session_id, role=role, field=field)
            if not self.__native:
                self.__capture_coverage(view, res)
            receipt = self.__index_receipt(view)
            if res.get('partial'):
                receipt['partial'] = True
            res.update(receipt)
            if self.__view_revision(view) != before:
                raise ValueError('index_revision_changed: restart from offset 0')
        self.__page_revision = before
        res.update(freshness='unknown', index_revision=before, pagination_consistency='revision_bound',
                   read_consistency='native_revision_checked' if self.__native else 'sqlite_snapshot')
        if brief:
            res.update(source=self.__source, current_verification='not_performed')
            res.setdefault('evidence_authority', 'indexed_excerpts')
        return res

    def handoff(self, session_id, *, include_plans=False):
        if self.__indexer is None:
            raise ValueError('reader_closed')
        self.__check_root()
        self.__validate_cache()
        if not self.__native:
            # Persisted file paths are data, never authority to read elsewhere.
            row = self.__indexer.conn.execute('SELECT file_path FROM sessions WHERE id = ?',
                                             (str(session_id),)).fetchone()
            if row:
                path = Path(row['file_path'])
                if self.__root not in path.parents or not self.__include(path):
                    raise ValueError('cached_source_path_outside_selection')
                curr = path
                while curr != self.__root:
                    if curr.is_symlink():
                        raise ValueError('source_symlink_not_allowed')
                    curr = curr.parent
                if self.__root not in path.resolve(strict=True).parents:
                    raise ValueError('cached_source_path_outside_selection')
                with path.open('rb'):
                    pass
        return service.handoff(self.__indexer, session_id, include_plans=include_plans)

    def message(self, session_id, message_index, *, index_revision, text_offset=0):
        """Expand a search locator with the same revision, without guessing a date."""
        if self.__indexer is None:
            raise ValueError('reader_closed')
        self.__check_root()
        self.__validate_cache()
        if not index_revision:
            raise ValueError('index_revision_required')
        from .messages import read_message
        from .providers import uses_message_index
        with self.__read_view() as view:
            before = self.__view_revision(view)
            if index_revision != before:
                raise ValueError('index_revision_changed: restart search')
            result = (read_message(view, session_id, message_index, text_offset)
                      if uses_message_index(view) else
                      service.read_native_message(view, session_id, message_index, text_offset))
            if self.__view_revision(view) != before:
                raise ValueError('index_revision_changed: restart search')
        result.update(source=self.__source, index_revision=before, freshness='unknown',
                      evidence_authority='indexed_message', current_verification='not_performed')
        return result

    def materials(self, session_id, selections, *, index_revision):
        """Export selected indexed windows, without rereading live transcripts."""
        if self.__indexer is None:
            raise ValueError('reader_closed')
        if self.__native:
            raise ValueError('indexed_snapshot_unsupported')
        self.__check_root()
        self.__validate_cache()
        from .indexed_materials import indexed_materials
        with self.__read_view() as view:
            return indexed_materials(view, session_id, selections, index_revision=index_revision)

    def markers(self, name=None, kind=None, session_id=None, project=None, limit=20, offset=0, index_revision=None):
        """Read recorded marker metadata without refresh or message bodies."""
        if self.__indexer is None:
            raise ValueError('reader_closed')
        if self.__source not in ('codex', 'claude'):
            raise ValueError('markers_source_unsupported')
        from .markers import MAX_OFFSET, read_markers
        if type(offset) is not int or not 0 <= offset <= MAX_OFFSET:
            raise ValueError('invalid_marker_offset')
        self.__check_root()
        self.__validate_cache()
        if offset and not index_revision:
            raise ValueError('index_revision_required: restart from offset 0')
        before = self.__revision()
        if index_revision is not None and index_revision != before:
            raise ValueError('index_revision_changed: restart from offset 0')
        result = read_markers(self.__indexer, name=name, kind=kind,
                              session_id=session_id, project=project, limit=limit, offset=offset)
        if self.__revision() != before:
            raise ValueError('index_revision_changed: restart from offset 0')
        result.update(source=self.__source, index_revision=before, freshness='unknown',
                      pagination_consistency='revision_bound', evidence_authority='recorded_markers',
                      current_verification='not_performed')
        return result

    def activity(self, *, date, timezone, refresh=False, limit=100, offset=0,
                 index_revision=None, session_id=None, evidence_limit=6, message_offset=0, message_index=None, text_offset=0):
        """Message-time evidence from the selected store; never an outcome score."""
        from .activity import read_store
        self.__check_root()
        self.__validate_cache()
        return read_store(self.__indexer, self.__source, self.__root,
                          date=date, timezone=timezone, refresh=refresh,
                          limit=limit, offset=offset, index_revision=index_revision,
                          session_id=session_id, evidence_limit=evidence_limit, message_offset=message_offset, message_index=message_index, text_offset=text_offset)

    def close(self):
        if self.__indexer is not None:
            self.__indexer.conn.close()
            self.__indexer = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
