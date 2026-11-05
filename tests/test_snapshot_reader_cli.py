"""Public reader and CLI integration on synthetic live recordings."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from history_core import HistoryReader
from history_core.__main__ import main
from history_core import reuse
from history_core.messages import read_message


class SnapshotReaderCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.path = self.source / 'selected.jsonl'
        self.cache = self.root / 'cache'
        self.write('old material')
        self.reader = HistoryReader('codex', self.source, self.cache)
        self.addCleanup(self.reader.close)
        self.reader.refresh()

    def write(self, text):
        rows = [
            {'type': 'session_meta', 'timestamp': '2026-10-19T00:00:00Z',
             'payload': {'id': 'selected', 'cwd': '/synthetic/snapshots'}},
            {'type': 'response_item', 'timestamp': '2026-10-19T00:00:01Z',
             'payload': {'type': 'message', 'role': 'user',
                         'content': [{'type': 'input_text', 'text': text}]}}
        ]
        self.path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')

    def publish(self, text):
        self.write(text)
        with HistoryReader('codex', self.source, self.cache) as writer:
            writer.refresh()

    def cli(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(['--source', 'codex', '--source-path', str(self.source),
                         '--data-dir', str(self.cache), *args])
        return code, output.getvalue(), error.getvalue()

    def test_brief_is_one_snapshot_when_writer_commits_between_sql_queries(self):
        original = reuse._snippets
        def publish_then_read(indexer, *args):
            self.publish('new material')
            return original(indexer, *args)
        with patch('history_core.reuse._snippets', side_effect=publish_then_read):
            page = self.reader.search(query='material', brief=True)
        self.assertIn('old material', page['items'][0]['snippets'][0]['text'])
        self.assertEqual(page['read_consistency'], 'sqlite_snapshot')
        fresh = self.reader.search(query='material', brief=True)
        self.assertIn('new material', fresh['items'][0]['snippets'][0]['text'])
        self.assertNotEqual(page['index_revision'], fresh['index_revision'])

    def test_message_stays_on_selected_snapshot_and_next_call_rejects_old_revision(self):
        revision = self.reader.search()['index_revision']
        def publish_then_read(indexer, *args):
            self.publish('new material')
            return read_message(indexer, *args)
        with patch('history_core.messages.read_message', side_effect=publish_then_read):
            page = self.reader.message('selected', 0, index_revision=revision)
        self.assertEqual(page['text'], 'old material')
        with self.assertRaisesRegex(ValueError, 'index_revision_changed'):
            self.reader.message('selected', 0, index_revision=revision)

    def test_cli_materials_exports_indexed_windows_after_source_append(self):
        revision = self.reader.search()['index_revision']
        with self.path.open('a') as stream:
            stream.write('{"incomplete":')
        code, output, error = self.cli('materials', 'selected', '--index-revision', revision, '--message', '0')
        self.assertEqual(code, 0, error)
        bundle = json.loads(output)
        self.assertEqual(bundle['schema_version'], 'history.indexed-materials.v1')
        self.assertIn('old material', bundle['markdown'])
        self.assertEqual(bundle['items'][0]['content_revision'], 'unknown')

    def test_cli_query_error_identifies_stage_and_budget(self):
        budget = sqlite3.OperationalError('interrupted')
        budget.query_budget_exceeded = True
        with patch('history_core.reuse._snippets', side_effect=budget):
            code, output, error = self.cli('search', '--query', 'material', '--brief')
        self.assertEqual(code, 2)
        self.assertEqual(output, '')
        receipt = json.loads(error)
        self.assertEqual(receipt['phase'], 'snippets')
        self.assertEqual(receipt['error_code'], 'query_budget_exceeded')
        self.assertGreaterEqual(receipt['elapsed_ms'], 0)
        code, output, error = self.cli('search', '--query', 'material', '--brief')
        self.assertEqual(code, 0, error)
        self.assertEqual(len(json.loads(output)['items']), 1)

    def test_cli_partial_refresh_keeps_existing_index_and_reports_failure(self):
        (self.source / 'broken.jsonl').write_text('{invalid}\n')
        code, output, error = self.cli('refresh', '--file', 'broken.jsonl')
        self.assertEqual(code, 1, error)
        result = json.loads(output)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['failed'], 1)
        self.assertTrue(result['partial'])
        self.assertEqual(self.reader.search()['items'][0]['id'], 'selected')

    def test_cli_reads_do_not_walk_the_source_tree(self):
        with patch.object(HistoryReader, '_HistoryReader__validate_source',
                          side_effect=AssertionError('unexpected source walk')):
            code, output, error = self.cli('search', '--query', 'material')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['items'][0]['id'], 'selected')

    def test_search_discloses_the_same_capture_watermark_and_uncovered_tail(self):
        indexer = self.reader._HistoryReader__indexer
        parse = indexer._parse_file_fn
        initial_size = self.path.stat().st_size
        def append_then_parse(path, *, content):
            with path.open('a') as stream:
                stream.write('{"incomplete":')
            return parse(path, content=content)
        with patch.object(indexer, '_parse_file_fn', side_effect=append_then_parse):
            receipt = self.reader.refresh(paths=['selected.jsonl'])
        self.assertTrue(receipt['partial'])
        result = self.reader.search(query='material', brief=True)
        self.assertTrue(result['partial'])
        self.assertEqual(result['items'][0]['source_capture']['captured_through'], initial_size)
        self.assertGreater(result['items'][0]['source_capture']['trailing_bytes'], 0)
        self.assertEqual(result['coverage'], 'indexed_records_not_live_completeness')
        events = self.reader.timeline()
        self.assertTrue(events['partial'])
        self.assertEqual(events['items'][0]['source_capture']['captured_through'], initial_size)
        self.assertGreater(events['items'][0]['source_capture']['trailing_bytes'], 0)
        self.assertEqual(events['coverage'], 'public_message_timestamps_not_full_activity')

    def test_health_distinguishes_empty_cache_from_indexed_sessions(self):
        with HistoryReader('codex', self.source, self.root / 'empty-cache') as empty:
            self.assertFalse(empty.health()['has_index'])
            self.assertEqual(empty.health()['indexed_sessions'], 0)
        self.assertEqual(self.reader.health()['indexed_sessions'], 1)
        self.assertEqual(self.reader.health()['freshness'], 'unknown')


if __name__ == '__main__':
    unittest.main()
