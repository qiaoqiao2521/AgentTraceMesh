"""Empty caches, compact CLI output and SQLite failure provenance."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import shlex
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from history_core import HistoryReader
from history_core.__main__ import main
from history_core import reuse


class RetrievalReceiptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'source with spaces'
        self.source.mkdir()
        self.cache = self.root / 'new cache'
        rows = [
            {'type': 'session_meta', 'payload': {'id': 'receipt', 'cwd': '/synthetic'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': '镜面记录 clip.mp4'}]}}]
        (self.source / 'record.jsonl').write_text('\n'.join(json.dumps(row) for row in rows) + '\n')

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['--source', 'codex', '--source-path', str(self.source),
                         '--data-dir', str(self.cache), *args])
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def test_empty_search_is_distinct_from_a_refreshed_no_match(self):
        with HistoryReader('codex', self.source, self.cache) as reader:
            page = reader.search(query='镜面', brief=True)
            self.assertEqual(page['indexed_sessions'], 0)
            self.assertTrue(page['partial'])
            self.assertEqual(page['index_state'], 'empty')
            self.assertIn('index_empty_refresh_required', json.dumps(page))
            self.assertEqual(shlex.split(page['refresh_command']), page['refresh_argv'])
            self.assertEqual(page['items'], [])
            self.assertEqual(reader.health()['index_state'], 'empty')
            self.assertTrue(reader.health()['refresh_required'])
            reader.refresh()
            miss = reader.search(query='does-not-exist', brief=True)
            self.assertEqual(miss['indexed_sessions'], 1)
            self.assertEqual(miss['index_state'], 'populated')
            self.assertFalse(miss['refresh_required'])
            self.assertNotIn('index_empty_refresh_required', json.dumps(miss))
            self.assertEqual(miss['items'], [])

    def test_suggested_refresh_arguments_work_then_default_search_is_compact(self):
        code, empty, error = self.call('search', '--query', '镜面')
        self.assertEqual(code, 0, error)
        out, err = io.StringIO(), io.StringIO()
        selected = [argument if argument != 'SOURCE_RELATIVE_JSONL' else 'record.jsonl'
                    for argument in empty['selected_refresh_argv_template']]
        with redirect_stdout(out), redirect_stderr(err):
            code = main(selected[3:])
        self.assertEqual(code, 0, err.getvalue())
        code, compact, error = self.call('search', '--query', '镜面')
        self.assertEqual(code, 0, error)
        self.assertEqual(compact['mode'], 'brief')
        self.assertNotIn('files_touched', compact['items'][0])
        self.assertTrue(compact['items'][0]['snippets'])
        code, verbose, error = self.call('search', '--query', '镜面', '--verbose')
        self.assertEqual(code, 0, error)
        self.assertIn('files_touched', verbose['items'][0])

    def test_file_field_is_forwarded_without_a_new_index(self):
        self.assertEqual(self.call('refresh')[0], 0)
        code, result, error = self.call('search', '--query', 'clip.mp4', '--field', 'files_touched')
        self.assertEqual(code, 0, error)
        self.assertEqual(result['items'][0]['id'], 'receipt')
        self.assertEqual(result['evidence_authority'], 'path_reference_not_operation_verification')

    def test_cli_distinguishes_contention_and_unattributed_interruption(self):
        for message, code, domain, retryable in (
            ('database is locked', 'source_busy', 'storage_contention', True),
            ('interrupted', 'query_interrupted', 'query_interruption', False),
        ):
            with self.subTest(message=message), patch.object(
                    HistoryReader, 'search', side_effect=sqlite3.OperationalError(message)):
                status, output, error = self.call('search', '--query', '镜面')
            self.assertEqual(status, 2)
            self.assertIsNone(output)
            receipt = json.loads(error)
            self.assertEqual(receipt['error_code'], code)
            self.assertEqual(receipt['failure_domain'], domain)
            self.assertEqual(receipt['retryable'], retryable)
            self.assertEqual(receipt['phase'], 'search')


class QueryFailureProvenanceTests(unittest.TestCase):
    def test_waiting_for_connection_lock_does_not_spend_vm_query_time(self):
        now = [0]
        class DelayedLock:
            def __enter__(self):
                now[0] = 100
            def __exit__(self, *args):
                pass
        class Connection:
            handler = None
            def set_progress_handler(self, handler, steps):
                self.handler = handler
        conn = Connection()
        with patch('history_core.reuse.time.monotonic', side_effect=lambda: now[0]):
            with reuse.query_budget(SimpleNamespace(conn=conn, lock=DelayedLock())):
                self.assertFalse(conn.handler(), 'waiting for another reader is not VM work')
        self.assertIsNone(conn.handler)

    def test_real_budget_interrupt_is_tagged_but_unknown_interrupt_is_not(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        idx = SimpleNamespace(conn=conn, lock=threading.RLock())
        now = [0]
        with patch('history_core.reuse.time.monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(sqlite3.OperationalError) as stopped:
                with reuse.query_budget(idx):
                    now[0] = 3
                    conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT sum(x) FROM n').fetchone()
        self.assertEqual(reuse.error_code(stopped.exception), 'query_budget_exceeded')
        self.assertEqual(reuse.error_code(sqlite3.OperationalError('interrupted')), 'query_interrupted')
        self.assertEqual(conn.execute('SELECT 1').fetchone()[0], 1)

    def test_real_database_contention_has_a_retryable_distinct_code(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'index.sqlite')
            writer, reader = sqlite3.connect(path), sqlite3.connect(path, timeout=0)
            try:
                writer.execute('CREATE TABLE records(value)')
                writer.commit()
                writer.execute('BEGIN EXCLUSIVE')
                with self.assertRaises(sqlite3.OperationalError) as blocked:
                    reader.execute('SELECT * FROM records').fetchall()
                self.assertEqual(reuse.error_code(blocked.exception), 'source_busy')
            finally:
                writer.rollback()
                writer.close()
                reader.close()
