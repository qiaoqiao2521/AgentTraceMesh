"""Synthetic source mutations only; never inspect native user sessions."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from history_core.capture import MAX_CAPTURE_LINE_BYTES, stat_signature
from history_core.sources import Indexer, parse_codex_session_file
from history_core import sources


def message(text):
    return (json.dumps({'timestamp': '2026-10-19T00:00:01Z', 'type': 'response_item',
        'payload': {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': text}]}}, ensure_ascii=False) + '\n').encode()


def recording(identity='fixture', text='BEFORE'):
    return (json.dumps({'timestamp': '2026-10-19T00:00:00Z', 'type': 'session_meta',
                       'payload': {'id': identity, 'cwd': '/synthetic/project'}}) + '\n').encode() + message(text)


class ActiveCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'source'
        self.root.mkdir()
        cache = self.base / 'cache'
        cache.mkdir()
        self.indexer = Indexer(self.root, cache, 'codex')
        self.addCleanup(self.indexer.conn.close)
        self.path = self.root / 'active.jsonl'
        self.original = recording()
        self.path.write_bytes(self.original)

    def texts(self, identity='fixture'):
        return [row[0] for row in self.indexer.conn.execute(
            'SELECT text FROM messages WHERE session_id=? ORDER BY id', (identity,))]

    def revision(self):
        return self.indexer.conn.execute("SELECT value FROM reader_state WHERE key='revision'").fetchone()[0]

    def test_stable_capture_keeps_whole_file_signature_and_fast_path(self):
        result = self.indexer.refresh_capture(['active.jsonl'])
        self.assertFalse(result['partial'])
        self.assertEqual(result['updated'], 1)
        row = self.indexer.conn.execute('SELECT * FROM sessions').fetchone()
        self.assertEqual(row['file_signature'], stat_signature(self.path.stat()))
        metadata = json.loads(row['activity_metadata_json'])['source_capture']
        self.assertEqual(metadata['captured_through'], len(self.original))
        self.assertEqual(metadata['prefix_hash'], hashlib.sha256(self.original).hexdigest())
        before = self.revision()
        with patch.object(self.indexer, '_parse_file_fn', side_effect=AssertionError('unexpected parse')):
            self.assertEqual(self.indexer.refresh_capture()['sessions'][0]['status'], 'unchanged')
            self.indexer.scan_sessions()  # Existing strict scan accepts the same signature.
        self.assertEqual(self.revision(), before)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_append_during_parse_is_excluded_from_parser_and_audit_until_next_capture(self):
        observed = []
        builder = sources.build_audit_for_file
        def parse(path, *, content):
            parsed = parse_codex_session_file(path, content=content)
            with path.open('ab') as stream:
                stream.write(message('LATER'))
            return parsed
        def audit(path, source, **kwargs):
            observed.append(kwargs['content'])
            return builder(path, source, **kwargs)
        with patch.object(self.indexer, '_parse_file_fn', side_effect=parse), patch.object(sources, 'build_audit_for_file', side_effect=audit):
            result = self.indexer.refresh_capture(['active.jsonl'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['failed'], 0)
        self.assertEqual(observed, [self.original])
        self.assertFalse(any('LATER' in text for text in self.texts()))
        capture = result['sessions'][0]['source_capture']
        self.assertEqual(capture['captured_through'], len(self.original))
        self.assertEqual(capture['trailing_bytes'], len(message('LATER')))
        signature = self.indexer.conn.execute('SELECT file_signature FROM sessions').fetchone()[0]
        self.assertNotEqual(signature, stat_signature(self.path.stat()))
        self.assertFalse(self.indexer.refresh_capture(['active.jsonl'])['partial'])
        self.assertTrue(any('LATER' in text for text in self.texts()))

    def test_partial_utf8_line_is_deferred_and_later_completion_is_indexed(self):
        tail = message('你好')
        split = tail.index('你'.encode()) + 1
        self.path.write_bytes(self.original + tail[:split])
        result = self.indexer.refresh_capture(['active.jsonl'])
        self.assertEqual(result['sessions'][0]['source_capture']['trailing_bytes'], split)
        self.assertEqual(self.texts(), ['BEFORE'])
        with self.path.open('ab') as stream:
            stream.write(tail[split:])
        self.assertFalse(self.indexer.refresh_capture(['active.jsonl'])['partial'])
        self.assertEqual(self.texts(), ['BEFORE', '你好'])

    def test_rewrite_failure_preserves_old_rows_and_commits_other_file_once(self):
        self.indexer.scan_sessions()
        other = self.root / 'other.jsonl'
        other.write_bytes(recording('other', 'INDEPENDENT'))
        before = self.revision()
        statements = []
        self.indexer.conn.set_trace_callback(statements.append)
        self.addCleanup(self.indexer.conn.set_trace_callback, None)
        def parse(path, *, content):
            result = parse_codex_session_file(path, content=content)
            if path == self.path:
                path.write_bytes(content.replace(b'BEFORE', b'AFTER!'))
            return result
        with patch.object(self.indexer, '_parse_file_fn', side_effect=parse):
            receipt = self.indexer.refresh_capture(['active.jsonl', 'other.jsonl'])
        self.assertEqual((receipt['updated'], receipt['failed']), (1, 1))
        self.assertIn('capture_prefix_changed', receipt['sessions'][0]['error'])
        self.assertEqual(self.texts(), ['BEFORE'])
        self.assertEqual(self.texts('other'), ['INDEPENDENT'])
        self.assertNotEqual(self.revision(), before)
        self.assertEqual(sum(sql.startswith('UPDATE reader_state SET value=') for sql in statements), 1)

    def test_truncate_or_replace_preserves_previous_revision_and_rows(self):
        self.indexer.scan_sessions()
        before = self.revision()
        for mutation in ('truncate', 'replace'):
            with self.subTest(mutation=mutation):
                self.path.write_bytes(self.original)
                def parse(path, *, content):
                    result = parse_codex_session_file(path, content=content)
                    if mutation == 'truncate':
                        path.write_bytes(b'{')
                    else:
                        path.rename(path.with_suffix('.old'))
                        path.write_bytes(content)
                    return result
                with patch.object(self.indexer, '_parse_file_fn', side_effect=parse):
                    receipt = self.indexer.refresh_capture(['active.jsonl'])
                self.assertEqual(receipt['failed'], 1)
                self.assertEqual(self.texts(), ['BEFORE'])
                self.assertEqual(self.revision(), before)

    def test_budget_and_line_limit_fail_without_parsing_or_erasing_cache(self):
        self.indexer.scan_sessions()
        before = self.revision()
        with patch.object(self.indexer, '_parse_file_fn', side_effect=AssertionError('must not parse')):
            receipt = self.indexer.refresh_capture(['active.jsonl'], max_bytes=len(self.original) - 1)
            self.assertIn('capture_budget_exceeded', receipt['sessions'][0]['error'])
            self.path.write_bytes(b'x' * (MAX_CAPTURE_LINE_BYTES + 1) + b'\n')
            receipt = self.indexer.refresh_capture(['active.jsonl'])
            self.assertIn('capture_line_budget_exceeded', receipt['sessions'][0]['error'])
        self.assertEqual(self.texts(), ['BEFORE'])
        self.assertEqual(self.revision(), before)

    def test_targeted_capture_never_prunes_or_replaces_unselected_recording(self):
        self.indexer.scan_sessions()
        duplicate = self.root / 'copied.jsonl'
        duplicate.write_bytes(recording(text='COPIED'))
        receipt = self.indexer.refresh_capture(['copied.jsonl'])
        self.assertEqual(receipt['failed'], 0)
        self.assertEqual(self.texts(), ['BEFORE'])
        copied = receipt['sessions'][0]['session_id']
        self.assertNotEqual(copied, 'fixture')
        self.assertEqual(self.texts(copied), ['COPIED'])
        repeated = self.indexer.refresh_capture(['copied.jsonl'])
        self.assertEqual(repeated['failed'], 0)
        self.assertEqual(repeated['sessions'][0]['session_id'], copied)
        self.assertEqual(self.texts(), ['BEFORE'])
        self.assertEqual(self.texts(copied), ['COPIED'])
        duplicate.unlink()
        self.indexer.refresh_capture(['active.jsonl'])
        self.assertEqual(self.indexer.conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 2)

    def test_generated_recording_alias_cannot_replace_another_native_session(self):
        suffix = hashlib.sha256(b'copied.jsonl').hexdigest()[:16]
        owned_id = 'fixture@recording-' + suffix
        owned = self.root / 'owned.jsonl'
        owned.write_bytes(recording(owned_id, 'MUST SURVIVE'))
        self.indexer.scan_sessions()
        previous = [tuple(row) for row in self.indexer.conn.execute('SELECT * FROM sessions ORDER BY id')]
        before = self.revision()
        duplicate = self.root / 'copied.jsonl'
        duplicate.write_bytes(recording(text='COPIED'))
        receipt = self.indexer.refresh_capture(['copied.jsonl'])
        self.assertEqual(receipt['failed'], 1)
        self.assertIn('duplicate_session_identity', receipt['sessions'][0]['error'])
        self.assertEqual(self.texts(), ['BEFORE'])
        self.assertEqual(self.texts(owned_id), ['MUST SURVIVE'])
        self.assertEqual([tuple(row) for row in self.indexer.conn.execute('SELECT * FROM sessions ORDER BY id')], previous)
        self.assertEqual(self.revision(), before)

    def test_escape_is_rejected_and_symlink_is_not_followed(self):
        before = self.revision()
        outside = self.base / 'outside.jsonl'
        outside.write_bytes(recording('outside'))
        with self.assertRaisesRegex(ValueError, 'outside_source'):
            self.indexer.refresh_capture(['../outside.jsonl'])
        link = self.root / 'link.jsonl'
        link.symlink_to(outside)
        receipt = self.indexer.refresh_capture(['link.jsonl'])
        self.assertEqual(receipt['failed'], 1)
        self.assertEqual(self.indexer.conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
        self.assertEqual(self.revision(), before)

    def test_complete_malformed_line_is_preserved_as_parser_warning(self):
        self.path.write_bytes(self.original + b'not json\n')
        receipt = self.indexer.refresh_capture(['active.jsonl'])
        self.assertFalse(receipt['partial'])
        self.assertTrue(self.indexer.conn.execute("SELECT 1 FROM messages WHERE kind='raw_json:malformed_line'").fetchone())

    @unittest.skipUnless(hasattr(os, 'mkfifo') and hasattr(os, 'O_NONBLOCK'), 'requires POSIX FIFO')
    def test_fifo_open_is_nonblocking_and_rejected(self):
        fifo = self.root / 'fifo.jsonl'
        os.mkfifo(fifo)
        real_open = os.open
        def checked_open(path, flags, *args, **kwargs):
            if Path(path) == fifo:
                self.assertTrue(flags & os.O_NONBLOCK, 'FIFO would block before regular-file validation')
            return real_open(path, flags, *args, **kwargs)
        with patch('history_core.capture.os.open', side_effect=checked_open):
            receipt = self.indexer.refresh_capture(['fifo.jsonl'])
        self.assertEqual(receipt['failed'], 1)
        self.assertIn('regular_file_required', receipt['sessions'][0]['error'])

    def test_full_selection_prunes_missing_rows_and_changes_revision(self):
        self.indexer.refresh_capture()
        before = self.revision()
        self.path.unlink()
        receipt = self.indexer.refresh_capture()
        self.assertEqual(receipt['deleted'], 1)
        self.assertFalse(receipt['partial'])
        self.assertEqual(self.texts(), [])
        self.assertNotEqual(self.revision(), before)

    def test_partial_full_selection_retains_missing_rows(self):
        other = self.root / 'other.jsonl'
        other.write_bytes(recording('other', 'RETAINED'))
        self.indexer.refresh_capture()
        other.unlink()
        self.path.write_bytes(self.original + b'{')
        receipt = self.indexer.refresh_capture()
        self.assertTrue(receipt['partial'])
        self.assertEqual(receipt['deleted'], 0)
        self.assertEqual(self.texts('other'), ['RETAINED'])

    def test_root_replacement_rolls_back_previously_staged_success(self):
        other = self.root / 'last.jsonl'
        other.write_bytes(recording('other'))
        before = self.revision()
        def parse(path, *, content):
            result = parse_codex_session_file(path, content=content)
            if path == other:
                self.root.rename(self.base / 'previous-source')
                self.root.mkdir()
            return result
        with patch.object(self.indexer, '_parse_file_fn', side_effect=parse):
            with self.assertRaisesRegex(ValueError, 'root_changed'):
                self.indexer.refresh_capture(['active.jsonl', 'last.jsonl'])
        self.assertEqual(self.indexer.conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
        self.assertEqual(self.revision(), before)


if __name__ == '__main__':
    unittest.main()
