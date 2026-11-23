"""Full indexed recall and bounded excerpts, without personal transcript access."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from history_core import reuse
from history_core.sources import Indexer, MAX_SEARCH_CHARS


class LargeSearchSnippetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.logs = self.root / 'sessions'
        self.logs.mkdir()
        self.idx = Indexer(self.logs, self.root, 'codex')
        self.addCleanup(self.idx.conn.close)
        self.indexers = [('linux', 'codex', self.idx)]

    def add_session(self, sid, messages, blob='', title='Synthetic conversation'):
        """Seed an old bounded cache with complete, independently ordered messages."""
        with self.idx.conn:
            self.idx.conn.execute(
                'INSERT INTO sessions(id,file_path,start_ts_ms,end_ts_ms,title,cwd,'
                'message_count,search_blob) VALUES(?,?,1,2,?,?,?,?)',
                (sid, str(self.logs / (sid + '.jsonl')), title, '/synthetic', len(messages), blob))
            self.idx.conn.executemany(
                'INSERT INTO messages(session_id,ts_ms,role,kind,text) VALUES(?,?,?,?,?)',
                [(sid, ts, role, 'message', text) for ts, role, text in messages])

    def search(self, query):
        before = self.idx.conn.total_changes
        revision = reuse.index_revision(self.idx)
        page = reuse.search(self.indexers, query=query)
        self.assertEqual(self.idx.conn.total_changes, before, 'queries must not rebuild caches')
        self.assertEqual(reuse.index_revision(self.idx), revision)
        self.assertEqual(page['errors'], [])
        return page

    def test_tail_after_two_million_characters_recalls_both_languages_and_unicode_position(self):
        prefix = '🧪' * (MAX_SEARCH_CHARS + 10)
        self.add_session('large', [(1, 'assistant', prefix + ' 后置系统 TailNeedle')],
                         blob=prefix[:MAX_SEARCH_CHARS])
        for query in ('后置系统', 'tailneedle'):
            with self.subTest(query=query):
                page = self.search(query)
                self.assertEqual([item['id'] for item in page['items']], ['large'])
                item = page['items'][0]
                self.assertEqual(item['snippet_status'], 'matched')
                snippet = item['snippets'][0]
                self.assertEqual(snippet['message_index'], 0)
                self.assertIn(query.lower(), snippet['text'].lower())
                self.assertLessEqual(len(snippet['text']), 240)
                self.assertTrue(snippet['truncated'])
                self.assertFalse(page['partial'])
                self.assertFalse(page['truncated'])

    def test_match_after_message_2001_keeps_real_reader_offset(self):
        messages = [(number, 'user', 'ordinary filler') for number in range(2005)]
        messages.append((2005, 'assistant', '后置系统 found here'))
        self.add_session('late-row', messages)
        item = self.search('后置系统')['items'][0]
        snippet = item['snippets'][0]
        self.assertEqual(snippet['message_index'], 2005)
        actual = self.idx.get_session_message('late-row', snippet['message_index'])
        self.assertIn('后置系统', actual['text'])
        self.assertEqual(snippet['text'], actual['text'])

    def test_ordering_includes_null_timestamps_and_id_tiebreak_before_filtering(self):
        self.add_session('ordered', [(30, 'assistant', 'needle latest'),
                                     (10, 'user', 'not a match'),
                                     (20, 'assistant', 'needle first tie'),
                                     (20, 'assistant', 'needle second tie'),
                                     (None, 'system', 'ordinary context')])
        snippets = self.search('needle')['items'][0]['snippets']
        self.assertEqual([item['message_index'] for item in snippets], [2, 3, 4])
        for snippet in snippets:
            actual = self.idx.get_session_message('ordered', snippet['message_index'])
            self.assertEqual(snippet['text'], actual['text'])

    def test_multiple_query_terms_can_match_different_long_messages(self):
        prefix = 'x' * (reuse.MAX_MESSAGE_CHARS + 100)
        self.add_session('both', [(1, 'user', prefix + ' 系统'),
                                  (2, 'assistant', prefix + ' 中文')])
        self.add_session('one', [(1, 'user', prefix + ' 系统')])
        page = self.search('系统 中文')
        self.assertEqual([item['id'] for item in page['items']], ['both'])
        snippets = page['items'][0]['snippets']
        self.assertEqual([item['message_index'] for item in snippets], [0, 1])
        self.assertIn('系统', snippets[0]['text'])
        self.assertIn('中文', snippets[1]['text'])
        self.assertFalse(page['partial'])

    def test_phrase_is_preferred_and_response_has_at_most_three_bounded_excerpts(self):
        text = 'alpha early ' + ('x' * 9000) + ' alpha beta actual phrase ' + ('x' * 9000)
        self.add_session('many', [(number, 'assistant', text) for number in range(5)])
        page = self.search('alpha beta')
        snippets = page['items'][0]['snippets']
        self.assertEqual(len(snippets), 3)
        for snippet in snippets:
            self.assertIn('alpha beta actual phrase', snippet['text'])
            self.assertLessEqual(len(snippet['text']), 240)
        self.assertLess(len(json.dumps(page)), 4000)
        self.assertFalse(page['partial'], 'short display excerpts do not mean incomplete recall')

    def test_later_phrase_precedes_earlier_scattered_terms(self):
        self.add_session('phrase', [(0, 'assistant', 'alpha precedes scattered beta'),
                                    (1, 'assistant', 'alpha beta final explanation')])
        snippets = self.search('alpha beta')['items'][0]['snippets']
        self.assertEqual([item['message_index'] for item in snippets], [1, 0])

    def test_literal_percent_underscore_and_ascii_case_match_consistently(self):
        self.add_session('literal', [(1, 'assistant', '100%_Literal')])
        self.add_session('wildcard', [(1, 'assistant', '100XXLiteral')])
        page = self.search('100%_literal')
        self.assertEqual([item['id'] for item in page['items']], ['literal'])
        self.assertEqual(page['items'][0]['snippets'][0]['text'], '100%_Literal')

    def test_full_body_phrase_contributes_to_rank_even_when_blob_is_empty(self):
        self.add_session('a', [(1, 'assistant', 'systems ready')])
        self.add_session('z', [(1, 'assistant', 'systems'), (2, 'assistant', 'ready')])
        page = self.search('systems ready')
        self.assertEqual([item['id'] for item in page['items']], ['a', 'z'])
        self.assertEqual([item['score'] for item in page['items']], [4, 0])

    def test_harness_context_cannot_fill_excerpt_slots_before_real_hit(self):
        self.add_session('context-first', [(n, 'system', '系统 setup') for n in range(3)]
                         + [(3, 'assistant', '系统 real answer')])
        with self.idx.conn:
            self.idx.conn.execute("UPDATE messages SET kind='context' WHERE ts_ms<3")
        item = self.search('系统')['items'][0]
        self.assertEqual([snippet['message_index'] for snippet in item['snippets']], [3])
        self.assertEqual(item['snippets'][0]['text'], '系统 real answer')

    def test_cached_blob_and_nonpublic_records_cannot_recall_noise(self):
        self.add_session('noise', [(n, 'assistant', 'noise_needle') for n in range(5)],
                         blob='noise_needle')
        with self.idx.conn:
            for n, kind in enumerate(('context', 'context:memory', 'reasoning_summary',
                                      'agent_reasoning', 'raw_json:event_msg:imported')):
                self.idx.conn.execute('UPDATE messages SET kind=? WHERE session_id=? AND ts_ms=?',
                                      (kind, 'noise', n))
        self.add_session('public', [(0, 'system', 'noise_needle injected rules'),
                                    (1, 'tool', 'noise_needle actual readback')],
                         blob='noise_needle')
        with self.idx.conn:
            self.idx.conn.execute("UPDATE messages SET kind='tool_result' WHERE session_id='public' AND ts_ms=1")
        page = self.search('noise_needle')
        self.assertEqual([item['id'] for item in page['items']], ['public'])
        self.assertEqual([item['message_index'] for item in page['items'][0]['snippets']], [1])
        self.assertEqual(page['items'][0]['snippets'][0]['text'], 'noise_needle actual readback')

    def test_nonpublic_record_cannot_supply_one_term_of_session_and_query(self):
        self.add_session('planned', [(0, 'user', 'alpha visible request'),
                                     (1, 'assistant', 'beta unexecuted thought')], blob='alpha beta')
        with self.idx.conn:
            self.idx.conn.execute("UPDATE messages SET kind='reasoning_summary' WHERE session_id='planned' AND ts_ms=1")
        self.assertEqual(self.search('alpha beta')['items'], [])

    def test_snippets_prefer_term_coverage_tools_and_later_user_messages(self):
        messages = [(0, 'assistant', 'alpha beta early report'),
                    (1, 'user', 'alpha beta original request'),
                    (2, 'assistant', 'alpha beta private thought'),
                    (3, 'tool', 'alpha beta verified readback'),
                    (4, 'user', 'alpha beta later correction'),
                    (5, 'assistant', 'alpha only later report')]
        self.add_session('ranked', messages)
        with self.idx.conn:
            self.idx.conn.execute("UPDATE messages SET kind='reasoning_summary' WHERE session_id='ranked' AND ts_ms=2")
            self.idx.conn.execute("UPDATE messages SET kind='tool_result' WHERE session_id='ranked' AND ts_ms=3")
        snippets = self.search('alpha beta')['items'][0]['snippets']
        self.assertEqual([item['message_index'] for item in snippets], [3, 4, 1])
        for snippet in snippets:
            original = self.idx.get_session_message('ranked', snippet['message_index'])
            self.assertEqual(snippet['text'], original['text'])

    def test_same_message_term_coverage_precedes_partial_tool_match(self):
        self.add_session('coverage', [(0, 'tool', 'alpha command'),
                                      (1, 'assistant', 'beta report'),
                                      (2, 'assistant', 'beta and alpha final explanation')])
        with self.idx.conn:
            self.idx.conn.execute("UPDATE messages SET kind='tool_use' WHERE session_id='coverage' AND ts_ms=0")
        snippets = self.search('alpha beta')['items'][0]['snippets']
        self.assertEqual([item['message_index'] for item in snippets], [2, 0, 1])

    def test_snippet_sql_budget_interrupt_is_explicit_and_connection_recovers(self):
        self.add_session('budget', [(number, 'user', 'filler') for number in range(2005)]
                         + [(2005, 'assistant', 'needle')])
        calls = 0

        def clock():
            nonlocal calls
            calls += 1
            return calls * 3

        with patch('history_core.reuse.time.monotonic', side_effect=clock):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'interrupted'):
                reuse._snippets(self.idx, 'budget', 'needle')
        self.assertGreater(calls, 1, 'the real SQLite progress handler must run')
        self.assertEqual(self.search('needle')['items'][0]['snippets'][0]['message_index'], 2005)

    def test_search_marks_snippet_budget_failure_without_claiming_no_match(self):
        self.add_session('budget', [(1, 'assistant', 'needle')])
        budget = sqlite3.OperationalError('interrupted')
        budget.query_budget_exceeded = True
        with patch('history_core.reuse._indexed_snippets', side_effect=budget):
            page = reuse.search(self.indexers, query='needle')
        self.assertEqual([item['id'] for item in page['items']], ['budget'])
        self.assertTrue(page['partial'])
        self.assertEqual(page['items'][0]['snippet_status'], 'query_budget_exceeded')
        self.assertEqual(page['items'][0]['snippets'], [])


if __name__ == '__main__':
    unittest.main()
