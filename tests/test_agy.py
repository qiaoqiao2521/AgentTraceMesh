"""Synthetic schema-backed AGY protobuf/SQLite fixtures; no private logs."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from audit.agy import decode_agy_step, protobuf_fields
from history_core.agy import AgyIndexer, parse_agy_session_file, read_agy_session


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def integer(number, value):
    return varint(number << 3) + varint(value)


def blob(number, value):
    value = value.encode() if isinstance(value, str) else value
    return varint(number << 3 | 2) + varint(len(value)) + value


def metadata(tool=None, timestamp=1790000000):
    value = blob(1, integer(1, timestamp))
    if tool:
        value += blob(4, blob(1, tool[0]) + blob(2, tool[1]) + blob(3, json.dumps(tool[2])))
    return value


def step(kind, member, *, status=3, meta=None):
    field = {14: 19, 15: 20, 17: 24, 23: 30, 101: 114, 132: 140}.get(kind, 200)
    return integer(1, kind) + integer(4, status) + blob(5, metadata() if meta is None else meta) + blob(field, member)


class AgyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'conversations').mkdir()
        self.summaries = self.root / 'conversation_summaries.db'
        with sqlite3.connect(self.summaries) as db:
            db.execute('CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY,title TEXT,step_count INTEGER,'
                       'last_modified_time TEXT,workspace_uris TEXT)')

    def write(self, rows, sid='synthetic', *, workspace='file:///synthetic/repo', title='Synthetic AGY', extension='.db'):
        path = self.root / 'conversations' / (sid + extension)
        with sqlite3.connect(self.summaries) as db:
            db.execute('INSERT INTO conversation_summaries VALUES(?,?,?,?,?)',
                       (sid, title, len(rows), '2026-09-21T12:00:00Z', json.dumps([workspace])))
        if extension == '.db':
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE steps(idx INTEGER PRIMARY KEY,step_type INTEGER,status INTEGER,step_format INTEGER,step_payload BLOB)')
                db.executemany('INSERT INTO steps VALUES(?,?,?,?,?)', [(i, kind, status, fmt, payload) for i, (kind, status, fmt, payload) in enumerate(rows)])
        else:
            path.write_bytes(b'PRIVATE-UNDECODED-LEGACY-BYTES')
        return path

    def reader(self):
        reader = AgyIndexer(self.summaries)
        self.addCleanup(reader.conn.close)
        return reader

    def fingerprints(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob('*') if p.is_file()}

    def test_six_observed_step_kinds_decode_from_explicit_fields(self):
        call = ('tool-1', 'Bash', {'command': 'pytest'})
        tool = blob(1, call[0]) + blob(2, call[1]) + blob(3, json.dumps(call[2]))
        rows = [
            (14, 3, 0, step(14, blob(3, blob(1, 'Find a historical change')) + blob(1, 'deprecated duplicate'))),
            (15, 3, 0, step(15, blob(1, 'Checking now') + blob(3, 'Thinking safely') + blob(7, tool))),
            (132, 3, 0, step(132, blob(2, blob(1, 'Exit code: 0\n2 tests passed')), meta=metadata(call))),
            (101, 3, 0, step(101, blob(1, 'System context'))),
            (23, 3, 0, step(23, blob(5, 'Historical checkpoint'))),
            (17, 3, 0, step(17, blob(3, blob(1, 'Explicit historical error')))),
        ]
        data = read_agy_session(self.write(rows))
        self.assertEqual(data['content_status'], 'decoded_text')
        self.assertEqual(data['cwd'], '/synthetic/repo')
        self.assertEqual(data['unsupported_steps'], [])
        self.assertEqual(data['message_count'], 2)
        self.assertEqual(sum(m['kind'] == 'tool_use' for m in data['messages']), 1, 'Planner and execution metadata refer to one call')
        self.assertIn('Historical checkpoint', data['search_blob'])
        self.assertIn('Explicit historical error', data['search_blob'])
        self.assertNotIn('deprecated duplicate', data['search_blob'])
        self.assertIsNone(data['usage'])

    def test_noise_summary_title_uses_decoded_user_request_outside_context(self):
        context = ('# AGENTS.md instructions for /synthetic/project\n<INSTRUCTIONS>\n'
                   '只属于环境的中文规则\n</INSTRUCTIONS>\n'
                   '<environment_context>\n中文环境信息\n</environment_context>')
        path = self.write([(15, 3, 0, step(15, blob(1, 'Assistant text is not the request'))),
                           (14, 3, 0, step(14, blob(1, context))),
                           (14, 3, 0, step(14, blob(1, context + '\n请修复会话初始化问题\n这是第二行')))],
                          title='# AGENTS.md instructions for /synthetic/project')
        before = self.fingerprints()
        self.assertEqual(read_agy_session(path)['title'], '请修复会话初始化问题')
        reader = self.reader()
        self.assertEqual(reader.get_session_metadata('synthetic')['title'], '请修复会话初始化问题')
        self.assertEqual(before, self.fingerprints())

    def test_real_summary_titles_keep_their_language(self):
        for sid, title, request in [('english', 'Fix WAL snapshot initialization', '请修复初始化'),
                                    ('chinese', '修复会话读取', 'Please fix initialization')]:
            path = self.write([(14, 3, 0, step(14, blob(1, request)))], sid=sid, title=title)
            self.assertEqual(read_agy_session(path)['title'], title)

    def test_default_summary_titles_fall_back_without_reading_undecoded_text(self):
        for index, title in enumerate(('', 'Untitled conversation', 'New chat', '123 --', 'default-4', 'AGY default-')):
            sid = 'default-%d' % index
            path = self.write([(14, 3, 0, step(14, blob(1, '真正的中文请求')))], sid=sid, title=title)
            self.assertEqual(read_agy_session(path)['title'], '真正的中文请求')
        path = self.write([(999, 3, 0, step(999, blob(1, '禁止从未知步骤猜标题')))], sid='unknown', title='New chat')
        self.assertEqual(read_agy_session(path)['title'], 'AGY unknown')

    def test_generic_done_does_not_invent_success_exit_code(self):
        call = ('tool-1', 'Bash', {'command': 'pytest'})
        decoded = decode_agy_step(step(132, blob(2, blob(1, 'Command finished')), meta=metadata(call)))
        tool_result = decoded['records'][-1]['message']['content'][0]
        self.assertNotIn('is_error', tool_result)
        self.assertNotIn('Exit code', tool_result['content'])
        failed = decode_agy_step(step(132, blob(2, blob(1, 'Permission denied')), status=7, meta=metadata(call)))
        self.assertTrue(failed['records'][-1]['message']['content'][0]['is_error'])

    def test_malformed_wire_and_envelope_mismatch_are_rejected(self):
        for payload in (b'\x12\xff', b'\x00', b'\x0e', b'\x12\x04abc'):
            with self.assertRaises(ValueError):
                protobuf_fields(payload)
        with self.assertRaisesRegex(ValueError, 'envelope_mismatch'):
            decode_agy_step(step(14, blob(1, 'hello')), step_type=15)

    def test_unknown_types_formats_and_corruption_remain_visible(self):
        rows = [(999, 3, 0, step(999, blob(1, 'MUST NOT GUESS THIS TEXT'))),
                (14, 3, 9, step(14, blob(1, 'unknown format'))), (14, 3, 0, b'\xff')]
        data = read_agy_session(self.write(rows))
        self.assertEqual(data['content_status'], 'partial_unsupported_steps')
        self.assertEqual(len(data['unsupported_steps']), 3)
        self.assertNotIn('MUST NOT GUESS', data['search_blob'])
        self.assertIn('full text is incomplete', data['search_blob'])

    def test_brief_search_keeps_partial_content_warning_without_raw_steps(self):
        from history_core import service
        self.write([(14, 3, 0, step(14, blob(1, 'needle in public text'))),
                    (999, 3, 0, step(999, blob(1, 'UNDECODED PRIVATE BYTES')))])
        before = self.fingerprints()
        reader = self.reader()
        result = service.search(reader, query='needle', brief=True)
        self.assertTrue(result['partial'])
        self.assertEqual(result['content_warnings'], [{'source': 'agy', 'incomplete_sessions': 1}])
        candidate = result['items'][0]
        self.assertEqual(candidate['content_status'], 'partial_unsupported_steps')
        self.assertEqual(candidate['unsupported_steps_count'], 1)
        self.assertTrue(candidate['snippets_partial'])
        self.assertNotIn('unsupported_steps', candidate)
        self.assertNotIn('UNDECODED PRIVATE BYTES', json.dumps(result))
        missing = service.search(reader, query='not-present', brief=True)
        self.assertEqual(missing['items'], [])
        self.assertTrue(missing['partial'], 'No match cannot imply full coverage of undecoded content')
        self.assertEqual(before, self.fingerprints())

    def test_legacy_protobuf_is_metadata_only_without_reading_contents(self):
        path = self.write([], extension='.pb')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('legacy content must not be read')):
            data = parse_agy_session_file(path)
        self.assertEqual(data['content_status'], 'unsupported_legacy_protobuf')
        self.assertNotIn('PRIVATE', data['search_blob'])
        reader = self.reader()
        self.assertEqual(reader.get_session_metadata('synthetic')['content_status'], 'unsupported_legacy_protobuf')
        self.assertIn('metadata only', reader.get_session('synthetic')['messages'][0]['text'])
        self.assertIsNone(reader.build_session_audit('synthetic'))

    def test_page_search_project_and_session_messages_use_read_only_sources(self):
        for index in range(23):
            self.write([(14, 3, 0, step(14, blob(1, 'needle in body %d' % index)))],
                       sid='session-%02d' % index, workspace='file:///synthetic/repo-%02d' % index)
        before = self.fingerprints()
        reader = self.reader()
        first = reader.list_sessions_page(q='needle', limit=20, stable_order=True)
        second = reader.list_sessions_page(q='needle', limit=20, offset=first['next_offset'], stable_order=True)
        self.assertEqual((len(first['items']), len(second['items'])), (20, 3))
        self.assertEqual(len(set(r['id'] for r in first['items'] + second['items'])), 23)
        self.assertEqual(len(reader.list_projects_page(limit=20)['items']), 20)
        self.assertEqual(len(reader.list_sessions_page(cwd='/synthetic/repo-03')['items']), 1)
        self.assertEqual(reader.search_session_messages('session-03', 'needle')['message_match_count'], 1)
        self.assertIn('needle', reader.get_session_message('session-03', 0)['text'])
        self.assertFalse(reader.rename_session('session-03', 'changed'))
        self.assertFalse(reader.pin_session('session-03', True))
        self.assertFalse(reader.query_usage()['has_usage_data'])
        self.assertEqual(before, self.fingerprints())

    def test_revision_and_refresh_observe_conversation_changes_without_summary_changes(self):
        path = self.write([(14, 3, 0, step(14, blob(1, 'before')))])
        reader = self.reader()
        before = reader.source_revision()
        with sqlite3.connect(path) as db:
            db.execute('UPDATE steps SET step_payload=? WHERE idx=0', (step(14, blob(1, 'after body edit')),))
        self.assertNotEqual(before, reader.source_revision())
        reader.maybe_update_index(max_age_seconds=0)
        self.assertIn('after body edit', reader.get_session_message('synthetic', 0)['text'])

    def test_active_wal_is_read_and_revision_bound(self):
        path = self.write([(14, 3, 0, step(14, blob(1, 'before')))])
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('UPDATE steps SET step_payload=? WHERE idx=0', (step(14, blob(1, 'committed WAL message')),))
        writer.commit()
        before = self.fingerprints()
        reader = self.reader()
        self.assertIn('committed WAL message', reader.get_session_message('synthetic', 0)['text'])
        self.assertEqual(before, self.fingerprints())

    def test_store_over_256_mib_indexes_late_small_session_and_refreshes(self):
        # SQLite permits unused trailing pages: large physical snapshots without
        # inventing hundreds of MiB of public messages or private transcript data.
        for number in range(5):
            path = self.write([(14, 3, 0, step(14, blob(1, 'early message')))],
                              sid='a-large-%d' % number)
            with path.open('r+b') as stream:
                stream.truncate(54 * 1024 * 1024)
        target = self.write([(14, 3, 0, step(14, blob(1, 'late-orchestration-proof')))],
                            sid='z-target', title='Synthetic orchestration')
        self.assertGreater(sum(p.stat().st_size for p in self.root.rglob('*.db')),
                           256 * 1024 * 1024)
        before = self.fingerprints()
        reader = self.reader()
        from history_core.reuse import search
        result = search([('linux', 'agy', reader)], query='late-orchestration-proof')
        self.assertEqual([row['id'] for row in result['items']], ['z-target'])
        self.assertEqual(reader.coverage()['total_sessions'], 6)
        self.assertEqual(reader.get_session_message('z-target', 0)['text'], 'late-orchestration-proof')
        self.assertEqual(before, self.fingerprints())
        with sqlite3.connect(target) as db:
            db.execute('UPDATE steps SET step_payload=?',
                       (step(14, blob(1, 'late-refreshed-proof')),))
        after_edit = self.fingerprints()
        reader.scan_sessions()
        result = search([('linux', 'agy', reader)], query='late-refreshed-proof')
        self.assertEqual([row['id'] for row in result['items']], ['z-target'])
        self.assertEqual(reader.coverage()['decoded_text_sessions'], 6)
        self.assertEqual(after_edit, self.fingerprints())

    def test_source_and_audit_limits_reject_without_silent_truncation(self):
        path = self.write([(14, 3, 0, step(14, blob(1, 'bounded data')))])
        with self.assertRaisesRegex(ValueError, 'source_limit_exceeded'):
            read_agy_session(path, max_bytes=3)
        with patch('history_core.agy.MAX_SESSION_STEPS', 0):
            with self.assertRaisesRegex(ValueError, 'source_limit_exceeded'):
                read_agy_session(path)
        reader = self.reader()
        with patch('history_core.agy.MAX_AUDIT_BYTES', 3):
            with self.assertRaisesRegex(ValueError, 'source_limit_exceeded'):
                reader.build_session_audit('synthetic')

    def test_oversized_db_or_wal_keeps_other_sessions_searchable(self):
        from history_core.agy import MAX_SESSION_BYTES
        from history_core import service
        for suffix in ('', '-wal'):
            with self.subTest(suffix=suffix):
                sid = 'a-oversized' + ('-wal' if suffix else '-db')
                path = self.write([(14, 3, 0, step(14, blob(1, 'unavailable body')))], sid=sid)
                oversized = Path(str(path) + suffix)
                with oversized.open('ab') as stream:
                    stream.truncate(MAX_SESSION_BYTES + 1)
        self.write([(14, 3, 0, step(14, blob(1, 'late intact message')))], sid='z-good')
        before = self.fingerprints()
        reader = self.reader()
        coverage = reader.coverage()
        self.assertEqual(coverage['total_sessions'], 3)
        self.assertEqual(coverage['decoded_text_sessions'], 1)
        self.assertEqual(coverage['metadata_only_sessions'], 2)
        self.assertEqual(coverage['partial_sessions'], 2)
        self.assertEqual(coverage['failed_sessions'], 2)
        for detail in coverage['session_errors']:
            self.assertEqual(detail['error_code'], 'agy_source_limit_exceeded')
            self.assertEqual(detail['reason'], 'physical_source_bytes')
            self.assertEqual(detail['limit_bytes'], MAX_SESSION_BYTES)
            self.assertEqual(detail['budget_remaining_bytes'], MAX_SESSION_BYTES)
            self.assertGreater(detail['size_bytes'], detail['limit_bytes'])
            self.assertEqual(detail['path'], str(self.root / 'conversations' / (detail['session_id'] + '.db')))
            metadata = reader.get_session_metadata(detail['session_id'])
            self.assertEqual(metadata['content_status'], 'partial_source_error')
            self.assertIn('Full text is unavailable', reader.get_session(detail['session_id'])['messages'][0]['text'])
        result = service.search(reader, query='late intact', brief=True)
        self.assertEqual([item['id'] for item in result['items']], ['z-good'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['content_warnings'][0]['incomplete_sessions'], 2)
        self.assertEqual(before, self.fingerprints())

    def test_corrupt_session_is_metadata_only_and_recovers_on_refresh(self):
        bad = self.write([], sid='a-bad')
        bad.write_bytes(b'not a SQLite database')
        good = self.write([(14, 3, 0, step(14, blob(1, 'healthy text')))], sid='z-good')
        before = self.fingerprints()
        reader = self.reader()
        metadata = reader.get_session_metadata('a-bad')
        self.assertEqual(metadata['content_status'], 'partial_source_error')
        self.assertEqual(metadata['source_error']['error_code'], 'source_schema_unavailable')
        self.assertEqual(metadata['source_error']['path'], str(bad))
        self.assertEqual(metadata['source_error']['size_bytes'], len(b'not a SQLite database'))
        self.assertIsNone(metadata['source_error']['limit_bytes'])
        self.assertIn('healthy text', reader.get_session_message('z-good', 0)['text'])
        self.assertEqual(before, self.fingerprints())
        bad.write_bytes(good.read_bytes())
        repaired = self.fingerprints()
        reader.scan_sessions()
        self.assertEqual(reader.coverage()['failed_sessions'], 0)
        self.assertEqual(reader.coverage()['session_errors'], [])
        self.assertEqual(reader.get_session_metadata('a-bad')['content_status'], 'decoded_text')
        self.assertIn('healthy text', reader.get_session_message('a-bad', 0)['text'])
        self.assertEqual(repaired, self.fingerprints())

    def test_step_limits_isolate_sessions_and_keep_units_explicit(self):
        self.write([(14, 3, 0, step(14, blob(1, 'first'))),
                    (14, 3, 0, step(14, blob(1, 'second')))], sid='a-many')
        self.write([(14, 3, 0, step(14, blob(1, 'x' * 300)))], sid='b-large-step')
        self.write([(14, 3, 0, step(14, blob(1, 'normal')))], sid='z-good')
        with patch('history_core.agy.MAX_SESSION_STEPS', 1), patch('history_core.agy.MAX_STEP_BYTES', 100):
            reader = self.reader()
        errors = {detail['session_id']: detail for detail in reader.coverage()['session_errors']}
        self.assertEqual(set(errors), {'a-many', 'b-large-step'})
        self.assertEqual(errors['a-many']['reason'], 'session_steps')
        self.assertEqual(errors['a-many']['step_count'], 2)
        self.assertEqual(errors['a-many']['limit_steps'], 1)
        self.assertIsNone(errors['a-many']['size_bytes'])
        self.assertIsNone(errors['a-many']['limit_bytes'])
        self.assertEqual(errors['b-large-step']['reason'], 'step_payload_bytes')
        self.assertGreater(errors['b-large-step']['size_bytes'], 300)
        self.assertEqual(errors['b-large-step']['limit_bytes'], 100)
        self.assertEqual(reader.get_session_message('z-good', 0)['text'], 'normal')

    def test_failed_refresh_keeps_previous_revision_and_coverage(self):
        path = self.write([(14, 3, 0, step(14, blob(1, 'original')))])
        reader = self.reader()
        revision, coverage = reader._revision, reader.coverage()
        path.write_bytes(b'broken during refresh')
        with patch.object(reader, 'source_revision', side_effect=['before', 'after', 'before-retry', 'after-retry']):
            with self.assertRaisesRegex(ValueError, 'source_changed_during_refresh'):
                reader.scan_sessions()
        self.assertEqual(reader._revision, revision)
        self.assertEqual(reader.coverage(), coverage)
        self.assertEqual(reader.get_session_message('synthetic', 0)['text'], 'original')

    def test_changed_during_read_and_symlinks_are_rejected(self):
        path = self.write([(14, 3, 0, step(14, blob(1, 'hello')))])
        with patch('history_core.agy._signature', side_effect=['before', 'after']):
            with self.assertRaisesRegex(ValueError, 'source_changed_during_read'):
                read_agy_session(path)
        link = path.with_name('linked.db')
        link.symlink_to(path)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            read_agy_session(link)

    def test_summary_ids_cannot_escape_conversations(self):
        with sqlite3.connect(self.summaries) as db:
            db.execute('INSERT INTO conversation_summaries VALUES(?,?,?,?,?)', ('../escape', 'bad', 0, '', '[]'))
        with self.assertRaisesRegex(ValueError, 'invalid_agy_session_id'):
            AgyIndexer(self.summaries)

    def test_source_identity_is_configurable_and_coverage_is_explicit(self):
        self.write([(14, 3, 0, step(14, blob(1, 'Hello')))])
        self.write([], sid='legacy', extension='.pb')
        reader = AgyIndexer(self.summaries, source='antigravity')
        self.addCleanup(reader.conn.close)
        self.assertEqual(reader.source, 'antigravity')
        self.assertEqual(reader.db_path, self.summaries)
        self.assertEqual(reader.build_session_audit('synthetic')['source'], 'antigravity')
        self.assertEqual(reader.coverage()['total_sessions'], 2)
        self.assertEqual(reader.coverage()['decoded_text_sessions'], 1)
        self.assertEqual(reader.coverage()['metadata_only_sessions'], 1)
        self.assertEqual(reader.coverage()['legacy_protobuf_sessions'], 1)

    def test_missing_source_is_distinct_from_unsupported_legacy_protobuf(self):
        with sqlite3.connect(self.summaries) as db:
            db.execute('INSERT INTO conversation_summaries VALUES(?,?,?,?,?)', ('missing', 'Known summary', 1, '', '[]'))
        reader = self.reader()
        self.assertEqual(reader.get_session_metadata('missing')['content_status'], 'source_missing')
        self.assertEqual(reader.coverage()['metadata_only_sessions'], 1)
        self.assertEqual(reader.coverage()['missing_source_sessions'], 1)
        self.assertEqual(reader.coverage()['legacy_protobuf_sessions'], 0)

    def test_temporary_source_snapshot_is_cleaned_after_success_and_failure(self):
        from history_core.agy import _read_only, _ReadBudget
        path = self.write([(14, 3, 0, step(14, blob(1, 'Hello')))])
        snapshot = _read_only(path, budget=_ReadBudget(1024 * 1024))
        directory = Path(snapshot._snapshot.name)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        snapshot.close()
        self.assertFalse(directory.exists())

    def test_budget_counts_physical_summary_and_session_bytes_not_only_payload(self):
        path = self.write([(14, 3, 0, step(14, blob(1, 'Small payload')))])
        physical_bytes = self.summaries.stat().st_size + path.stat().st_size
        with self.assertRaisesRegex(ValueError, 'source_limit_exceeded'):
            read_agy_session(path, max_bytes=physical_bytes - 1)
        data = read_agy_session(path, max_bytes=physical_bytes)
        self.assertEqual(data['source_bytes'], physical_bytes)
        self.assertLess(data['payload_bytes'], data['source_bytes'])

    def test_oversized_summary_is_rejected_before_opening_a_source_descriptor(self):
        path = self.write([(14, 3, 0, step(14, blob(1, 'Small payload')))])
        # A valid tiny session cannot bypass its budget through a large summary.
        with patch('history_core.agy.os.open', side_effect=AssertionError('No source file may be copied')):
            with self.assertRaisesRegex(ValueError, 'source_limit_exceeded'):
                read_agy_session(path, max_bytes=self.summaries.stat().st_size - 1)

    def test_db_plus_wal_is_preflighted_before_copying_the_database(self):
        from history_core.agy import _read_only, _ReadBudget
        path = self.write([(14, 3, 0, step(14, blob(1, 'Original')))])
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('UPDATE steps SET step_payload=?', (step(14, blob(1, 'WAL update')),))
        writer.commit()
        wal = Path(str(path) + '-wal')
        budget = _ReadBudget(path.stat().st_size + wal.stat().st_size - 1)
        with patch('history_core.agy.os.open', side_effect=AssertionError('Preflight must precede copying')):
            with self.assertRaisesRegex(ValueError, 'source_limit_exceeded'):
                _read_only(path, budget=budget)
        self.assertEqual(budget.remaining, budget.limit)

    def test_native_audit_has_step_refs_and_only_explicit_exit_codes(self):
        call = ('tool-1', 'Bash', {'command': 'pytest'})
        self.write([(132, 3, 0, step(132, blob(2, blob(1, 'Exit code: 0\n2 passed')), meta=metadata(call)))])
        audit = self.reader().build_session_audit('synthetic')
        self.assertEqual(audit['source'], 'agy')
        self.assertEqual(audit['commands'][0]['exit_code'], 0)
        self.assertEqual(audit['commands'][0]['status'], 'pass')
        for evidence in audit['evidence']:
            self.assertEqual(evidence['raw_ref']['step_index'], 0)
            self.assertNotIn('line_no', evidence['raw_ref'])


if __name__ == '__main__':
    unittest.main()
