"""Public tool-reuse behavior against synthetic recordings, never private logs."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from history_core import HistoryReader, service
from history_core.sources import Indexer


REPO = Path(__file__).resolve().parents[1]


class ToolReuseBriefTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / 'sessions'
        self.source.mkdir()
        self.cache = self.root / 'cache'

    def record(self, sid, events, stamp=1791590400000):
        rows = [{'type': 'session_meta', 'timestamp': stamp,
                 'payload': {'id': sid, 'cwd': '/synthetic'}}]
        rows.extend({'type': 'response_item', 'timestamp': stamp + n,
                     'payload': event} for n, event in enumerate(events, 1))
        path = self.source / (sid + '.jsonl')
        path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
        return path

    @staticmethod
    def message(text, role='user'):
        return {'type': 'message', 'role': role,
                'content': [{'type': 'input_text', 'text': text}]}

    @staticmethod
    def call(command, cid='call-one'):
        return {'type': 'function_call', 'name': 'shell_command', 'call_id': cid,
                'arguments': json.dumps({'command': command})}

    @staticmethod
    def result(output, cid='call-one', code=0):
        return {'type': 'function_call_output', 'call_id': cid,
                'output': {'output': output, 'metadata': {'exit_code': code}}}

    def reader(self):
        reader = HistoryReader('codex', self.source, self.cache)
        self.addCleanup(reader.close)
        reader.refresh()
        return reader

    def test_old_tool_beats_recent_title_mentions_without_changing_default_search(self):
        self.record('old-tool', [self.message('Check artifact'),
                                self.call('readback verifier artifact.bin'),
                                self.result('READBACK_OK')], stamp=1790816400000)
        for n in range(3):
            self.record('new-chat-%d' % n, [self.message('readback verifier question')],
                        stamp=1791590400000 + n * 10000)
        reader = self.reader()
        normal = reader.search(query='readback verifier', limit=3)
        brief = reader.search(query='readback verifier', limit=3, brief=True)
        self.assertEqual([item['id'] for item in normal['items']],
                         ['new-chat-2', 'new-chat-1', 'new-chat-0'])
        self.assertEqual(brief['items'][0]['id'], 'old-tool')
        self.assertEqual(len(brief['items']), 3)
        self.assertTrue(brief['has_more'])
        self.assertGreater(brief['items'][0]['tool_match_terms'], 0)

    def test_tool_relevance_pages_stay_complete_without_a_500_candidate_cap(self):
        for n in range(503):
            events = [self.message('Check artifact')]
            if n % 2:
                events += [self.call('readback verifier'), self.result('checked')]
            else:
                events += [self.message('readback verifier mention', role='assistant')]
            self.record('s%03d' % n, events)
        reader = self.reader()
        ids, offset, revision = [], 0, None
        while True:
            page = reader.search(query='readback verifier', brief=True, limit=100,
                                 offset=offset, index_revision=revision)
            revision = page['index_revision']
            self.assertLessEqual(len(page['items']), 100)
            ids.extend(item['id'] for item in page['items'])
            if not page['has_more']:
                break
            offset = page['next_offset']
        self.assertEqual(len(ids), 503)
        self.assertEqual(len(set(ids)), 503)
        self.assertEqual(ids[:251], ['s%03d' % n for n in range(1, 503, 2)])

    def test_short_result_keeps_error_header_and_explicit_status(self):
        self.record('short', [self.message('Check artifact'), self.call('check_artifact'),
                              self.result('x' * 55 + ' READBACK_OK', code=7)])
        snippet = self.reader().search(query='READBACK_OK', brief=True)['items'][0]['snippets'][0]
        self.assertIn('Status: error', snippet['text'])
        self.assertIn('Exit code: 7', snippet['text'])
        self.assertFalse(snippet['truncated'])
        self.assertFalse(snippet['has_more_before'])
        self.assertFalse(snippet['has_more_after'])
        self.assertEqual(snippet['kind'], 'tool_result')
        self.assertEqual(snippet['call_id'], 'call-one')
        self.assertIsInstance(snippet['ts_ms'], int)
        self.assertEqual(snippet['tool_summary']['exit_status'], 'error')
        self.assertEqual(snippet['tool_summary']['exit_code'], 7)

    def test_long_result_discloses_real_window_and_keeps_status(self):
        self.record('long', [self.message('Check artifact'), self.call('check_artifact'),
                             self.result('x' * 900 + ' READBACK_OK', code=7)])
        item = self.reader().search(query='READBACK_OK', brief=True)['items'][0]
        snippet = item['snippets'][0]
        self.assertTrue(snippet['truncated'])
        self.assertTrue(snippet['has_more_before'])
        self.assertFalse(snippet['has_more_after'])
        self.assertGreater(snippet['excerpt_start'], 0)
        self.assertEqual(snippet['excerpt_end'], snippet['text_chars'])
        self.assertEqual(snippet['excerpt_offset_basis'], 'indexed_text')
        self.assertLessEqual(len(snippet['text']), 240)
        self.assertEqual(snippet['tool_summary']['exit_status'], 'error')
        self.assertEqual(snippet['tool_summary']['exit_code'], 7)

    def test_marker_without_recorded_exit_status_stays_unknown(self):
        result = self.result('READBACK_OK')
        result['output'].pop('metadata')
        self.record('unknown', [self.message('Check artifact'), self.call('check_artifact'), result])
        snippet = self.reader().search(query='READBACK_OK', brief=True)['items'][0]['snippets'][0]
        self.assertEqual(snippet['tool_summary']['exit_status'], 'unknown')
        self.assertIsNone(snippet['tool_summary']['exit_code'])

    def test_matched_window_masks_a_secret_that_starts_before_the_window(self):
        self.record('secret-window', [self.message('Check artifact'), self.call('check_artifact'),
                                     self.result('token=' + 'A' * 400 + 'needle' + 'B' * 400)])
        item = self.reader().search(query='needle', brief=True)['items'][0]
        snippet = item['snippets'][0]
        self.assertTrue(snippet['has_more_before'])
        self.assertTrue(snippet['has_more_after'])
        self.assertEqual(snippet['text'], '*' * 240)
        self.assertNotIn('AAAA', json.dumps(item))
        self.assertNotIn('BBBB', json.dumps(item))

    def test_related_window_and_call_id_are_masked_before_display(self):
        cid = 'ghp_' + 'F' * 40
        self.record('secret-related', [self.message('Check artifact'),
                                      self.call('sha256sum artifact.bin', cid=cid),
                                      self.result('password=' + 'A' * 600, cid=cid)])
        item = self.reader().search(query='sha256sum', brief=True)['items'][0]
        self.assertEqual(item['snippets'][0]['call_id'], '[REDACTED]')
        self.assertEqual(item['related_tool_messages'][0]['call_id'], '[REDACTED]')
        self.assertNotIn(cid, json.dumps(item))
        self.assertNotIn('AAAA', json.dumps(item))
        self.assertTrue(item['related_tool_messages'][0]['has_more_after'])

    def test_nul_inside_a_long_message_keeps_true_source_offsets(self):
        self.record('nul', [self.message('Check artifact'), self.call('check_artifact'),
                            self.result('needle\x00' + 'x' * 600)])
        snippet = self.reader().search(query='needle', brief=True)['items'][0]['snippets'][0]
        self.assertIn('needle', snippet['text'])
        self.assertTrue(snippet['has_more_after'])
        self.assertGreater(snippet['text_chars'], 600)
        self.assertLess(snippet['excerpt_end'], snippet['text_chars'])

    def test_oversized_call_id_is_bounded_for_display_but_still_pairs(self):
        cid = 'c' * 100000
        self.record('large-id', [self.message('Check artifact'),
                                self.call('PUBLIC_LOOKUP', cid=cid), self.result('done', cid=cid)])
        item = self.reader().search(query='PUBLIC_LOOKUP', brief=True)['items'][0]
        for message in [item['snippets'][0], item['related_tool_messages'][0]]:
            self.assertEqual(len(message['call_id']), 128)
            self.assertTrue(message['call_id_truncated'])
        self.assertLess(len(json.dumps(item)), 3000)

    def test_input_and_result_queries_include_only_the_same_call_counterpart(self):
        self.record('pairs', [self.message('Check artifact'),
                              self.call('sha256sum artifact.bin', cid='wanted'),
                              self.call('unrelated_operation', cid='other'),
                              self.result('unrelated output', cid='other'),
                              self.result('12ab artifact.bin', cid='wanted')])
        reader = self.reader()
        for query, related_kind, related_text in (
                ('sha256sum', 'tool_result', '12ab artifact.bin'),
                ('12ab', 'tool_use', 'sha256sum artifact.bin')):
            with self.subTest(query=query):
                item = reader.search(query=query, brief=True)['items'][0]
                self.assertEqual(len(item['snippets']), 1)
                self.assertEqual(len(item['related_tool_messages']), 1)
                related = item['related_tool_messages'][0]
                self.assertEqual(related['call_id'], 'wanted')
                self.assertEqual(related['kind'], related_kind)
                self.assertIn(related_text, related['text'])
                self.assertEqual(related['related_to_message_indexes'],
                                 [item['snippets'][0]['message_index']])
                self.assertFalse(item['related_tool_messages_truncated'])

    def test_missing_call_id_does_not_guess_adjacent_messages(self):
        call = self.call('sha256sum artifact.bin')
        result = self.result('12ab artifact.bin')
        call.pop('call_id'); result.pop('call_id')
        self.record('missing-id', [self.message('Check artifact'), call, result])
        item = self.reader().search(query='sha256sum', brief=True)['items'][0]
        self.assertIsNone(item['snippets'][0]['call_id'])
        self.assertEqual(item['related_tool_messages'], [])

    def test_related_messages_are_bounded_and_do_not_displace_matching_snippets(self):
        self.record('chunks', [self.message('Check artifact'), self.call('sha256sum artifact.bin')]
                    + [self.result('chunk-%d' % n) for n in range(5)])
        item = self.reader().search(query='sha256sum', brief=True)['items'][0]
        self.assertEqual(len(item['snippets']), 1)
        self.assertEqual(len(item['related_tool_messages']), 3)
        self.assertTrue(item['related_tool_messages_truncated'])
        self.assertEqual([entry['message_index'] for entry in item['related_tool_messages']], [2, 3, 4])

    def test_short_chinese_and_cross_word_queries_keep_literal_and_semantics(self):
        self.record('chinese', [self.message('检查文件'), self.call('回读 ' + 'x' * 650 + ' 校验'),
                                self.result('完成')])
        reader = self.reader()
        for query in ('回读', '回读 校验'):
            with self.subTest(query=query):
                item = reader.search(query=query, brief=True)['items'][0]
                self.assertIn('回读', item['snippets'][0]['text'])
                self.assertTrue(item['snippets'][0]['has_more_after'])
                self.assertEqual(item['related_tool_messages'][0]['kind'], 'tool_result')

    def test_brief_candidate_budget_failure_is_explicit(self):
        path = self.record('budget', [self.message('Check artifact'), self.call('readback verifier')])
        cache = self.root / 'direct-cache'; cache.mkdir()
        idx = Indexer(self.source, cache, 'codex')
        self.addCleanup(idx.conn.close)
        idx.scan_sessions()
        with patch('history_core.reuse.query_budget', side_effect=sqlite3.OperationalError('interrupted')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'interrupted'):
                service.search(idx, query='readback verifier', brief=True)
        self.assertTrue(path.exists())

    def test_scoped_user_search_keeps_long_solution_and_absolute_message_index(self):
        pasted = '说明🧪' * 4000 + ' 可替换解法 use_existing_adapter'
        self.record('chosen', [self.message('initial request'),
                               self.call('use_existing_adapter'), self.result('use_existing_adapter failed'),
                               self.call('use_existing_adapter retry'),
                               self.message(pasted)])
        self.record('other', [self.message('use_existing_adapter elsewhere')])
        reader = self.reader()
        page = reader.search(query='use_existing_adapter', brief=True, session_id='chosen', role='user')
        self.assertEqual([row['id'] for row in page['items']], ['chosen'])
        snippet = page['items'][0]['snippets'][0]
        self.assertEqual(snippet['role'], 'user')
        self.assertEqual(snippet['message_index'], 4)
        self.assertGreater(snippet['excerpt_start'], 8192)
        self.assertEqual(page['items'][0]['related_tool_messages'], [])
        original = reader.message('chosen', 4, index_revision=page['index_revision'],
                                  text_offset=snippet['excerpt_start'])
        self.assertIn('可替换解法 use_existing_adapter', original['text'])
        self.assertEqual(page['scope'], {'session_id': 'chosen', 'role': 'user'})

    def test_role_filters_before_pagination_and_does_not_match_title_or_other_roles(self):
        self.record('actual', [self.message('initial'), self.message('needle remedy')])
        for n in range(4):
            self.record('noise-%d' % n, [self.message('ordinary'), self.call('needle remedy')],
                        stamp=1791590401000 + n)
        reader = self.reader()
        page = reader.search(query='needle remedy', brief=True, role='user', limit=1)
        self.assertEqual([row['id'] for row in page['items']], ['actual'])
        self.assertFalse(page['has_more'])
        self.assertEqual(reader.search(query='needle remedy', brief=True, role='user',
                                       session_id='noise-0')['items'], [])
        self.assertEqual(reader.search(query='needle', brief=True, session_id='missing')['items'], [])
        with self.assertRaisesRegex(ValueError, 'search_scope_requires_brief'):
            reader.search(query='needle', role='user')
        with self.assertRaisesRegex(ValueError, 'invalid_search_role'):
            reader.search(query='needle', brief=True, role='system')

    def test_role_search_keeps_public_filter_and_session_and_semantics(self):
        self.record('scoped', [self.message('alpha'), self.message('beta'),
                               self.message('hidden', role='assistant')])
        self.record('wrong-role', [self.message('alpha'), self.result('beta')])
        reader = self.reader()
        page = reader.search(query='alpha beta', brief=True, role='user')
        self.assertEqual([row['id'] for row in page['items']], ['scoped'])
        self.assertEqual([s['message_index'] for s in page['items'][0]['snippets']], [1, 0])

    def test_role_search_excludes_context_and_title_only_hits(self):
        self.record('hidden', [self.message('hidden_needle')])
        cache = self.root / 'hidden-cache'; cache.mkdir()
        idx = Indexer(self.source, cache, 'codex')
        self.addCleanup(idx.conn.close)
        idx.scan_sessions()
        with idx.conn:
            idx.conn.execute("UPDATE messages SET kind='context' WHERE session_id='hidden'")
        page = service.search(idx, query='hidden_needle', brief=True, role='user')
        self.assertEqual(page['items'], [])

    def test_role_search_pagination_and_revision_checks(self):
        for n in range(3):
            self.record('user-%d' % n, [self.message('pasted solution')], stamp=1791590400000 + n)
        reader = self.reader()
        first = reader.search(query='solution', role='user', brief=True, limit=1)
        second = reader.search(query='solution', role='user', brief=True, limit=1,
                               offset=first['next_offset'], index_revision=first['index_revision'])
        self.assertEqual([first['items'][0]['id'], second['items'][0]['id']], ['user-2', 'user-1'])
        self.record('user-new', [self.message('new solution')])
        reader.refresh()
        with self.assertRaisesRegex(ValueError, 'index_revision_changed'):
            reader.search(query='solution', role='user', brief=True, limit=1,
                          offset=second['next_offset'], index_revision=first['index_revision'])

    def test_scoped_cli_search_reuses_revision_for_expansion(self):
        self.record('cli-user', [self.message('initial'), self.call('user_solution'),
                                 self.message('user_solution pasted')])
        self.reader()
        prefix = [sys.executable, '-B', '-m', 'history_core', '--source', 'codex',
                  '--source-path', str(self.source), '--data-dir', str(self.cache)]
        command = ['search', '--query', 'user_solution', '--brief', '--session', 'cli-user', '--role', 'user']
        process = subprocess.run(prefix + command, cwd=REPO, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        response = json.loads(process.stdout)
        snippet = response['items'][0]['snippets'][0]
        self.assertEqual(snippet['message_index'], 2)
        process = subprocess.run(prefix + ['message', 'cli-user', '2', '--index-revision', response['index_revision']],
                                 cwd=REPO, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)['text'], 'user_solution pasted')

    def test_real_candidate_budget_interrupt_recovers_for_the_next_read(self):
        self.record('budget', [self.message('Check artifact'), self.call('readback verifier')])
        cache = self.root / 'direct-cache'; cache.mkdir()
        idx = Indexer(self.source, cache, 'codex')
        self.addCleanup(idx.conn.close)
        idx.scan_sessions()
        with idx.conn:
            idx.conn.executemany('INSERT INTO messages(session_id,ts_ms,role,kind,text) VALUES(?,?,?,?,?)',
                                 [('budget', n + 1791590401000, 'assistant', 'reasoning_summary',
                                   'readback verifier unexecuted thought') for n in range(4000)])
        calls = 0

        def clock():
            nonlocal calls
            calls += 1
            return calls * 3

        with patch('history_core.reuse.time.monotonic', side_effect=clock):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'interrupted'):
                service.search(idx, query='readback verifier', brief=True)
        self.assertGreater(calls, 1)
        page = service.search(idx, query='readback verifier', brief=True)
        self.assertEqual(page['items'][0]['id'], 'budget')
        self.assertEqual(len(page['items'][0]['snippets']), 1)

    def test_fresh_cli_fixture_returns_paired_evidence_without_source_writes(self):
        path = self.record('cli', [self.message('检查文件'), self.call('sha256sum artifact.bin'),
                                  self.result('12ab artifact.bin', code=7)])
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        prefix = [sys.executable, '-B', '-m', 'history_core', '--source', 'codex',
                  '--source-path', str(self.source), '--data-dir', str(self.cache)]
        for command in (['refresh'], ['search', '--query', 'sha256sum', '--brief', '--limit', '3']):
            process = subprocess.run(prefix + command, cwd=REPO, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            response = json.loads(process.stdout)
        self.assertEqual(response['source'], 'codex')
        self.assertTrue(response['index_revision'])
        related = response['items'][0]['related_tool_messages'][0]
        self.assertIn('12ab artifact.bin', related['text'])
        self.assertEqual(related['tool_summary']['exit_status'], 'error')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_related_pairing_work_does_not_grow_with_other_sessions(self):
        from contextlib import contextmanager
        from history_core.reuse import _related_tool_messages

        self.record('pair-target', [self.message('Check three calls')]
                    + [event for n in range(3)
                       for event in (self.call('check-%d' % n, cid='pair-%d' % n),
                                     self.result('result-%d' % n, cid='pair-%d' % n))])
        cache = self.root / 'pair-cache'; cache.mkdir()
        idx = Indexer(self.source, cache, 'codex')
        self.addCleanup(idx.conn.close)
        idx.scan_sessions()
        insert = ('INSERT INTO messages(session_id,ts_ms,role,kind,text,activity_meta_json) '
                  'VALUES(?,?,?,?,?,?)')
        with idx.conn:
            idx.conn.executemany(insert,
                (('pair-target', 1791590401000 + n, 'tool', 'tool_use', 'unrelated',
                  json.dumps({'tool_call_id': 'filler-%d' % n})) for n in range(4993)))
        snippets = [{'message_index': 1 + n * 2, 'kind': 'tool_use',
                     'call_id': 'pair-%d' % n} for n in range(3)]

        def measured_pairing():
            ticks = [0]

            def progress():
                ticks[0] += 1
                return 0

            @contextmanager
            def counting_budget(indexer):
                with indexer.lock:
                    indexer.conn.set_progress_handler(progress, 100)
                    try:
                        yield indexer.conn
                    finally:
                        indexer.conn.set_progress_handler(None, 0)

            with patch('history_core.reuse.query_budget', counting_budget):
                related, truncated = _related_tool_messages(idx, 'pair-target', snippets)
            self.assertEqual([row['message_index'] for row in related], [2, 4, 6])
            self.assertEqual([row['related_to_message_indexes'] for row in related], [[1], [3], [5]])
            self.assertFalse(truncated)
            return ticks[0]

        baseline = measured_pairing()
        with idx.conn:
            idx.conn.executemany(insert,
                (('other-session', n, 'tool', 'tool_result', 'unrelated',
                  json.dumps({'tool_call_id': 'pair-%d' % (n % 3)})) for n in range(50000)))
        expanded = measured_pairing()
        # Count SQLite work, not wall time; unrelated matching Call IDs stay out.
        self.assertGreater(baseline, 0)
        self.assertLessEqual(expanded, baseline + max(20, baseline // 10))

    def test_related_budget_interrupt_keeps_matching_evidence_and_marks_partial(self):
        self.record('pair-budget', [self.message('Check artifact'),
                                    self.call('matched_operation'), self.result('done')])
        reader = self.reader()
        budget = sqlite3.OperationalError('interrupted')
        budget.query_budget_exceeded = True
        with patch('history_core.reuse._related_tool_messages', side_effect=budget):
            page = reader.search(query='matched_operation', brief=True,
                                 session_id='pair-budget', role='tool')
        self.assertEqual([item['id'] for item in page['items']], ['pair-budget'])
        self.assertTrue(page['partial'])
        item = page['items'][0]
        self.assertEqual(len(item['snippets']), 1)
        self.assertIn('matched_operation', item['snippets'][0]['text'])
        self.assertEqual(item['snippets'][0]['message_index'], 1)
        self.assertEqual(item['related_tool_messages'], [])
        self.assertEqual(item['related_tool_messages_error'],
                         {'error_code': 'query_budget_exceeded', 'phase': 'related_tool_messages'})

    def test_related_schema_failure_is_not_reported_as_a_budget_fallback(self):
        self.record('pair-schema', [self.message('Check artifact'),
                                    self.call('matched_operation'), self.result('done')])
        reader = self.reader()
        with patch('history_core.reuse._related_tool_messages',
                   side_effect=sqlite3.OperationalError('no such table')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'no such table'):
                reader.search(query='matched_operation', brief=True,
                              session_id='pair-schema', role='tool')


if __name__ == '__main__':
    unittest.main()
