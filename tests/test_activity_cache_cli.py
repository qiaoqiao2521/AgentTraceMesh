"""Public facade and real subprocess coverage for cache/timeline integration."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from app import Handler
from history_core import HistoryReader, reuse
from history_core.__main__ import main
from history_core.sources import Indexer
from test_agy import blob, metadata, step


class ActivityCacheCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.source = self.folder / 'source'
        self.source.mkdir()
        (self.source / 'conversations').mkdir()
        self.summary = self.source / 'conversation_summaries.db'
        self.cache = self.folder / 'cache'
        stamp = int(datetime(2026, 11, 5, 8, tzinfo=timezone.utc).timestamp())
        with sqlite3.connect(self.summary) as conn:
            conn.execute('CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY,title TEXT,step_count INTEGER,last_modified_time TEXT,workspace_uris TEXT)')
            conn.execute('INSERT INTO conversation_summaries VALUES(?,?,?,?,?)',
                         ('one', '中文模板名', 3, '2026-11-05T08:00:02Z', '[]'))
        with sqlite3.connect(self.source / 'conversations/one.db') as conn:
            conn.execute('CREATE TABLE steps(idx INTEGER PRIMARY KEY,step_type INTEGER,status INTEGER,step_format INTEGER,step_payload BLOB)')
            payloads = [step(14, blob(1, '请解决真实缓存需求'), meta=metadata(timestamp=stamp)),
                        step(15, blob(3, 'HIDDEN_REASONING'), meta=metadata(timestamp=stamp+1)),
                        step(15, blob(1, '缓存需求已经处理'), meta=metadata(timestamp=stamp+2))]
            conn.executemany('INSERT INTO steps VALUES(?,?,3,0,?)',
                             [(n, 14 if n == 0 else 15, body) for n, body in enumerate(payloads)])

    def cli(self, *args, explicit_cache=True, environment=None):
        base = [sys.executable, '-B', '-m', 'history_core', '--source', 'agy',
                '--source-path', str(self.summary)]
        if explicit_cache:
            base.extend(['--data-dir', str(self.cache)])
        result = subprocess.run(base+list(args), capture_output=True, text=True, timeout=10,
                                cwd=Path(__file__).resolve().parents[1], env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_facade_and_second_process_use_persisted_cache(self):
        cold = self.cli('health')
        self.assertEqual(cold['cache']['mode'], 'persistent')
        program = ('import sys\nfrom unittest.mock import patch\nfrom history_core.__main__ import main\n'
                   'with patch("history_core.agy.read_agy_session",side_effect=AssertionError("decode")), '
                   'patch("history_core.agy._read_only",side_effect=AssertionError("snapshot")):\n'
                   ' raise SystemExit(main(sys.argv[1:]))')
        result = subprocess.run([sys.executable, '-B', '-c', program, '--source', 'agy',
            '--source-path', str(self.summary), '--data-dir', str(self.cache), 'search', '--query', '缓存'],
            capture_output=True, text=True, timeout=10, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        page = json.loads(result.stdout)
        self.assertEqual(page['cache']['status'], 'warm')
        self.assertEqual(len(page['items']), 1)
        self.assertIn('请解决真实缓存需求', json.dumps(page['items'][0]['title_candidates'], ensure_ascii=False))
        self.assertNotIn('HIDDEN_REASONING', result.stdout)

    def test_cli_default_cache_is_reused_without_explicit_data_dir(self):
        env = {**os.environ, 'XDG_CACHE_HOME': str(self.folder / 'xdg')}
        first = self.cli('health', explicit_cache=False, environment=env)
        second = self.cli('health', explicit_cache=False, environment=env)
        self.assertEqual(second['cache']['status'], 'warm')
        self.assertEqual(first['cache']['path'], second['cache']['path'])
        self.assertTrue(Path(second['cache']['path']).is_relative_to(self.folder / 'xdg'))

    def test_timeline_pages_reopen_at_same_revision_and_expand_exact_event(self):
        first = self.cli('timeline', '--date', '2026-11-05', '--timezone', 'Asia/Shanghai', '--limit', '1')
        self.assertEqual(first['items'][0]['message_index'], 2)
        self.assertEqual(first['next_offset'], 1)
        second = self.cli('timeline', '--date', '2026-11-05', '--timezone', 'Asia/Shanghai',
                          '--limit', '1', '--offset', '1', '--index-revision', first['index_revision'])
        self.assertEqual(second['items'][0]['message_index'], 0)
        self.assertEqual(first['index_revision'], second['index_revision'])
        body = self.cli('message', 'one', '0', '--index-revision', first['index_revision'])
        self.assertIn('请解决真实缓存需求', body['text'])
        self.assertNotIn('HIDDEN_REASONING', json.dumps([first, second, body]))

    def test_bad_timeline_window_is_rejected_before_source_initialization(self):
        out, err = io.StringIO(), io.StringIO()
        with patch('history_core.__main__.HistoryReader') as factory, redirect_stdout(out), redirect_stderr(err):
            code = main(['--source', 'agy', '--source-path', str(self.summary),
                         'timeline', '--date', '2026-11-05'])
        self.assertEqual(code, 2)
        factory.assert_not_called()
        self.assertEqual(json.loads(err.getvalue())['phase'], 'timeline_parameters')

    def test_negative_timeline_timestamp_is_rejected_before_cold_load(self):
        out, err = io.StringIO(), io.StringIO()
        with patch('history_core.__main__.HistoryReader') as factory, redirect_stdout(out), redirect_stderr(err):
            code = main(['--source', 'agy', '--source-path', str(self.summary),
                         'timeline', '--start-ms', '-1'])
        self.assertEqual(code, 2)
        factory.assert_not_called()
        self.assertEqual(json.loads(err.getvalue())['phase'], 'timeline_parameters')

    def test_empty_file_cache_timeline_warns_and_supplies_refresh_command(self):
        source = self.folder / 'codex'; source.mkdir()
        with HistoryReader('codex', source, self.cache) as reader:
            page = reader.timeline()
        self.assertEqual(page['items'], [])
        self.assertEqual(page['indexed_sessions'], 0)
        self.assertEqual(page['index_state'], 'empty')
        self.assertTrue(page['partial'])
        self.assertTrue(page['refresh_required'])
        self.assertEqual(page['warnings'][0]['code'], 'index_empty_refresh_required')
        self.assertEqual(page['refresh_argv'][-1], 'refresh')

    def test_initialization_size_error_keeps_structured_path_and_budget(self):
        from history_core.agy import _AgySourceLimitError
        failure = _AgySourceLimitError(self.summary, reason='physical_source_bytes',
                                      size_bytes=4096, limit_bytes=2048, budget_remaining_bytes=0)
        out, err = io.StringIO(), io.StringIO()
        with patch('history_core.__main__.HistoryReader', side_effect=failure), redirect_stdout(out), redirect_stderr(err):
            code = main(['--source', 'agy', '--source-path', str(self.summary), 'health'])
        self.assertEqual(code, 2)
        receipt = json.loads(err.getvalue())
        self.assertEqual(receipt['phase'], 'initialization')
        self.assertEqual(receipt['source_error']['path'], str(self.summary))
        self.assertEqual(receipt['source_error']['size_bytes'], 4096)
        self.assertEqual(receipt['source_error']['budget_remaining_bytes'], 0)

    def test_corrupt_cache_warning_survives_search_and_timeline(self):
        cold = self.cli('health')
        Path(cold['cache']['path']).write_bytes(b'broken synthetic cache')
        for command in (('search', '--query', '缓存'), ('timeline', '--limit', '2')):
            page = self.cli(*command)
            self.assertEqual(page['cache']['status'], 'fallback_memory')
            self.assertIn('agy_cache_unavailable', json.dumps(page['warnings']))
            self.assertTrue(page['items'])


class FileSourceDefaultCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.source = self.folder / 'sessions'
        self.source.mkdir()
        self.fixture = self.source / 'one.jsonl'
        events = [
            {'timestamp': '2026-11-05T08:00:00Z', 'type': 'session_meta',
             'payload': {'id': 'shared-cache-fixture', 'cwd': '/synthetic/shared-cache'}},
            {'timestamp': '2026-11-05T08:00:01Z', 'type': 'response_item',
             'payload': {'type': 'message', 'role': 'user',
                         'content': [{'type': 'input_text', 'text': '复用已有派生缓存'}]}},
        ]
        self.fixture.write_text('\n'.join(json.dumps(event) for event in events) + '\n')
        self.xdg = self.folder / 'xdg'
        self.environment = {**os.environ, 'XDG_CACHE_HOME': str(self.xdg)}

    def cli(self, *args, data_dir=None):
        command = [sys.executable, '-B', '-m', 'history_core', '--source', 'codex',
                   '--source-path', str(self.source)]
        if data_dir is not None:
            command.extend(['--data-dir', str(data_dir)])
        result = subprocess.run(command + list(args), capture_output=True, text=True, timeout=10,
                                cwd=Path(__file__).resolve().parents[1], env=self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_default_cache_survives_a_new_command_without_source_rebuild(self):
        self.cli('refresh')
        cache = self.xdg / 'agenttracemesh' / 'reader'
        databases = list(cache.glob('machine-codex-*/index.sqlite'))
        self.assertEqual(len(databases), 1)
        # A new process must use the prior index, even with the recording gone.
        # This also distinguishes reuse from an implicit per-command refresh.
        self.fixture.unlink()
        page = self.cli('search', '--query', '复用', '--limit', '1')
        self.assertEqual(page['items'][0]['id'], 'shared-cache-fixture')
        self.assertEqual(page['indexed_sessions'], 1)
        self.assertEqual(list(cache.glob('machine-codex-*/index.sqlite')), databases)

    def test_explicit_cache_overrides_default_and_python_api_stays_explicit(self):
        chosen = self.folder / 'chosen-cache'
        self.cli('refresh', data_dir=chosen)
        page = self.cli('search', '--query', '复用', data_dir=chosen)
        self.assertEqual(page['indexed_sessions'], 1)
        self.assertEqual(len(list(chosen.glob('machine-codex-*/index.sqlite'))), 1)
        self.assertFalse(self.xdg.exists())
        with self.assertRaisesRegex(ValueError, 'sessions_directory_and_data_dir_required'):
            HistoryReader('codex', self.source)


class AggregateIndexReceiptTests(unittest.TestCase):
    def test_empty_api_search_has_count_warning_and_existing_refresh_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, cache = root / 'source', root / 'cache'
            source.mkdir(); cache.mkdir()
            indexer = Indexer(source, cache, 'codex')
            try:
                handler = Handler.__new__(Handler)
                handler._reuse_sources = lambda *args, **kwargs: ([('linux', 'codex', indexer)], [])
                handler.send_json = lambda data, status=200: data
                page = handler.handle_reuse_get(urlparse('/api/reuse/search?q=not-present'))
                self.assertEqual(page['items'], [])
                self.assertTrue(page['partial'])
                self.assertEqual(page['source_indexes'][0]['indexed_sessions'], 0)
                warning = page['warnings'][0]
                self.assertEqual(warning['code'], 'index_empty_refresh_required')
                self.assertEqual(warning['refresh_request']['path'], '/api/reuse/refresh')
                self.assertEqual(warning['refresh_request']['json'], {'system': 'linux', 'source': 'codex'})
            finally:
                indexer.conn.close()

    def test_budget_handle_is_reported_and_nonfinite_settings_are_safe(self):
        for setting, expected in [('60', 60), ('0.5', 0.5), ('nan', 2), ('inf', 2), ('bad', 2)]:
            with self.subTest(setting=setting), patch.dict(os.environ, {'HISTORY_QUERY_BUDGET_SECONDS': setting}):
                self.assertEqual(reuse.query_budget_seconds(), expected)
