"""Uniform compact search over synthetic public source records."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from history_core import HistoryReader, service
from history_core.sources import HermesStateIndexer, OpenCodeIndexer
from history_core.zcode import ZCodeIndexer
from test_zcode import SCHEMA, TS


class UniformBriefTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def native(self, source):
        path = self.root / (source + '.db')
        if source == 'hermes':
            with sqlite3.connect(path) as conn:
                conn.executescript('''
                    CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,user_id TEXT,model TEXT,
                        started_at REAL,ended_at REAL,message_count INTEGER,title TEXT);
                    CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,timestamp REAL,
                        role TEXT,content TEXT,reasoning TEXT,tool_calls TEXT,tool_name TEXT);
                ''')
                conn.executemany('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)', [
                    ('chosen', '/fixture', '', '', TS/1000, TS/1000+1, 4, 'Useful candidate'),
                    ('other', '/fixture', '', '', TS/1000+2, TS/1000+3, 1, 'needle in title')])
                conn.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)', [
                    (1, 'chosen', TS/1000, 'user', 'request needle artifact.png', '', '', ''),
                    (2, 'chosen', TS/1000+1, 'assistant', '', 'HIDDEN_ONLY private needle', '', ''),
                    (3, 'chosen', TS/1000+2, 'assistant', 'answer needle ' + 'x'*700, 'PRIVATE_REASONING', '', ''),
                    (4, 'chosen', TS/1000+3, 'tool', 'output needle artifact.png', '', '', 'bash'),
                    (5, 'other', TS/1000+4, 'assistant', 'unrelated answer', '', '', '')])
            idx = HermesStateIndexer(path)
        else:
            with sqlite3.connect(path) as conn:
                conn.executescript(SCHEMA)
                if source == 'opencode':
                    for column in ('agent TEXT', 'model TEXT', 'cost REAL', 'tokens_input INTEGER',
                                   'tokens_output INTEGER', 'tokens_reasoning INTEGER',
                                   'tokens_cache_read INTEGER', 'tokens_cache_write INTEGER'):
                        conn.execute('ALTER TABLE session ADD COLUMN ' + column)
                conn.executemany('INSERT INTO session(id,directory,title,time_created,time_updated) VALUES (?,?,?,?,?)', [
                    ('chosen', '/fixture', 'Useful candidate', TS, TS+1000),
                    ('other', '/fixture', 'needle in title', TS+2000, TS+3000)])
                conn.executemany('INSERT INTO message VALUES (?,?,?,?,?,?)', [
                    ('u', 'chosen', TS, TS, json.dumps({'role': 'user'}), 0),
                    ('a', 'chosen', TS+1, TS+1, json.dumps({'role': 'assistant'}), 1),
                    ('o', 'other', TS+2, TS+2, json.dumps({'role': 'assistant'}), 0)])
                parts = [
                    ('p0', 'u', 'chosen', TS, {'type': 'text', 'text': 'request needle artifact.png'}, 0),
                    ('p1', 'a', 'chosen', TS+1, {'type': 'reasoning', 'text': 'HIDDEN_ONLY private needle'}, 0),
                    ('p2', 'a', 'chosen', TS+2, {'type': 'text', 'text': 'answer needle ' + 'x'*700}, 1),
                    ('p3', 'a', 'chosen', TS+3, {'type': 'tool', 'tool': 'bash', 'state': {
                        'status': 'completed', 'input': {'command': 'create artifact.png'},
                        'output': 'output needle artifact.png', 'metadata': {'exit': 0}}}, 2),
                    ('o0', 'o', 'other', TS+4, {'type': 'text', 'text': 'unrelated answer'}, 0)]
                conn.executemany('INSERT INTO part VALUES (?,?,?,?,?,?,?)', [
                    (pid, mid, sid, ts, ts, json.dumps(data), seq) for pid, mid, sid, ts, data, seq in parts])
            idx = ZCodeIndexer(path) if source == 'zcode' else OpenCodeIndexer(path)
        self.addCleanup(idx.conn.close)
        return idx, path

    def test_native_brief_returns_small_public_evidence_with_absolute_indices(self):
        for source in ('zcode', 'opencode', 'hermes'):
            with self.subTest(source=source):
                idx, path = self.native(source)
                before = path.read_bytes()
                page = service.search(idx, query='needle', brief=True)
                item = next(row for row in page['items'] if row['id'] == 'chosen')
                self.assertEqual({entry['message_index'] for entry in item['snippets']}, {0, 2, 3})
                self.assertNotIn('PRIVATE_REASONING', json.dumps(page))
                self.assertNotIn('HIDDEN_ONLY', json.dumps(page))
                self.assertNotIn('tokens_total', item)
                self.assertNotIn('audit', item)
                self.assertLessEqual(max(map(lambda s: len(s['text']), item['snippets'])), 240)
                self.assertEqual(path.read_bytes(), before)

    def test_native_hidden_only_query_does_not_claim_a_public_match(self):
        for source in ('zcode', 'opencode', 'hermes'):
            with self.subTest(source=source):
                idx, _ = self.native(source)
                self.assertEqual(service.search(idx, query='HIDDEN_ONLY', brief=True)['items'], [])

    def test_native_scope_filters_before_pagination_and_exact_session(self):
        for source in ('zcode', 'opencode', 'hermes'):
            with self.subTest(source=source):
                idx, _ = self.native(source)
                page = service.search(idx, query='needle', role='user', brief=True, limit=1)
                self.assertEqual([item['id'] for item in page['items']], ['chosen'])
                self.assertFalse(page['has_more'])
                self.assertTrue(all(s['role'] == 'user' for s in page['items'][0]['snippets']))
                page = service.search(idx, query='needle', session_id='other', role='user', brief=True)
                self.assertEqual(page['items'], [])

    def test_native_message_expands_same_public_window_and_refuses_private_index(self):
        for source in ('zcode', 'opencode', 'hermes'):
            with self.subTest(source=source):
                idx, _ = self.native(source)
                message = service.read_native_message(idx, 'chosen', 2, text_offset=200)
                self.assertEqual(message['message_index'], 2)
                self.assertEqual(message['text_offset'], 200)
                self.assertNotIn('PRIVATE_REASONING', message['text'])
                with self.assertRaisesRegex(ValueError, 'message_not_public'):
                    service.read_native_message(idx, 'chosen', 1)

    def test_native_pages_have_no_duplicates_and_preserve_offsets(self):
        idx, _ = self.native('zcode')
        first = service.search(idx, query='needle', brief=True, limit=1, stable_order=True)
        second = service.search(idx, query='needle', brief=True, limit=1,
                                offset=first['next_offset'], stable_order=True)
        self.assertTrue(first['has_more'])
        self.assertEqual(second['offset'], 1)
        self.assertFalse(second['has_more'])
        self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])

    def test_native_file_reference_is_literal_public_message_evidence(self):
        for source in ('zcode', 'opencode', 'hermes'):
            with self.subTest(source=source):
                idx, _ = self.native(source)
                page = service.search(idx, query='artifact.png', field='files_touched', brief=True, role='user')
                self.assertEqual([item['id'] for item in page['items']], ['chosen'])
                self.assertEqual(page['evidence_authority'], 'path_reference_not_operation_verification')
                self.assertEqual(page['items'][0]['snippets'][0]['message_index'], 0)
                self.assertEqual(service.search(idx, query='artifactXpng', field='files_touched', brief=True)['items'], [])

    def codex(self):
        path = self.root / 'sessions'
        path.mkdir()
        rows = [{'type': 'session_meta', 'timestamp': TS, 'payload': {'id': 'file-session', 'cwd': '/fixture'}}]
        for n, text in enumerate(('please create /fixture/my images/artifact.png', 'artifactXpng does not match')):
            rows.append({'type': 'response_item', 'timestamp': TS+n+1, 'payload': {
                'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}})
        (path/'session.jsonl').write_text('\n'.join(map(json.dumps, rows))+'\n')
        reader = HistoryReader('codex', path, self.root/'cache')
        self.addCleanup(reader.close)
        reader.refresh()
        return reader

    def test_indexed_file_references_do_not_infer_operation_success(self):
        reader = self.codex()
        page = reader.search(query='artifact.png', field='files_touched', brief=True)
        self.assertEqual(page['evidence_authority'], 'path_reference_not_operation_verification')
        snippet = page['items'][0]['snippets'][0]
        self.assertEqual(snippet['message_index'], 0)
        self.assertEqual(snippet['role'], 'user')
        self.assertIn('please create', reader.message('file-session', 0, index_revision=page['index_revision'])['text'])

    def test_native_excerpt_failure_keeps_candidate_with_partial_reason(self):
        idx, _ = self.native('zcode')
        with patch('history_core.service._native_snippets', side_effect=ValueError('handoff_source_limit_exceeded')):
            page = service.search(idx, query='needle', brief=True)
        self.assertEqual(len(page['items']), 2)
        self.assertTrue(page['partial'])
        self.assertTrue(page['items'][0]['snippets_partial'])
        self.assertEqual(page['items'][0]['snippet_status'], 'unavailable')
        self.assertIn('handoff_source_limit_exceeded', json.dumps(page['items'][0]['limitations']))


    def test_filename_query_requires_a_nonempty_literal(self):
        idx, _ = self.native('zcode')
        for query in (None, '', '   '):
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, 'filename_query_required'):
                service.search(idx, query=query, field='files_touched', brief=True)

    def test_complete_filename_with_spaces_ignores_inferred_file_metadata(self):
        reader = self.codex()
        phrase = '/fixture/my images/artifact.png'
        page = reader.search(query=phrase, field='files_touched', brief=True)
        self.assertEqual(len(page['items']), 1)
        self.assertIn(phrase, page['items'][0]['snippets'][0]['text'])
        # Audit inference is deliberately insufficient for this evidence mode.
        idx = reader._HistoryReader__indexer
        idx.conn.execute("UPDATE sessions SET files_touched_json=?", (json.dumps({'local': ['inferred-only.png']}),))
        idx.conn.commit()
        self.assertEqual(reader.search(query='inferred-only.png', field='files_touched', brief=True)['items'], [])

    def test_native_search_failure_keeps_unverified_small_candidates_and_scope_limit(self):
        idx, _ = self.native('zcode')
        error = sqlite3.OperationalError('interrupted')
        with patch('history_core.compact_search.session_page', side_effect=error):
            page = service.search(idx, query='needle', brief=True, role='user', session_id='chosen')
        self.assertEqual([item['id'] for item in page['items']], ['chosen'])
        self.assertTrue(page['partial'])
        self.assertEqual(page['unsupported_scope'], ['role'])
        self.assertFalse(page['limitations'][0]['query_applied'])
        self.assertEqual(page['limitations'][0]['error_code'], 'query_interrupted')
        self.assertEqual(page['items'][0]['candidate_match_status'], 'unverified')

    def test_native_renderer_receives_only_projected_public_fields(self):
        idx, path = self.native('zcode')
        with sqlite3.connect(path) as conn:
            raw = json.loads(conn.execute("SELECT data FROM part WHERE id='p2'").fetchone()[0])
            raw['hidden_thinking'] = 'NEVER_MATERIALIZE'
            conn.execute("UPDATE part SET data=? WHERE id='p2'", (json.dumps(raw),))
        seen = []
        flatten = idx._flatten_part
        def inspect(*args):
            seen.append(json.dumps(args[2]))
            return flatten(*args)
        with patch.object(idx, '_flatten_part', side_effect=inspect):
            page = service.search(idx, query='needle', brief=True)
        self.assertTrue(page['items'])
        self.assertTrue(seen)
        self.assertNotIn('NEVER_MATERIALIZE', ''.join(seen))
        self.assertNotIn('HIDDEN_ONLY', ''.join(seen))

    def test_common_indexed_source_dispatch_preserves_the_compact_contract(self):
        # All file providers and decoded AGY share this derived-index read
        # model. Parser-specific correctness remains in their own fixtures.
        from history_core.providers import FILE_SOURCES
        from history_core.sources import Indexer, parse_codex_session_file
        reader = self.codex()
        for source in (*FILE_SOURCES, 'agy', 'antigravity'):
            with self.subTest(source=source):
                cache = self.root/('direct-'+source)
                cache.mkdir()
                idx = Indexer(self.root/'sessions', cache, 'codex',
                              parse_file_fn=parse_codex_session_file)
                self.addCleanup(idx.conn.close)
                idx.scan_sessions()
                idx.source = source
                page = service.search(idx, query='artifact.png', brief=True)
                self.assertEqual(len(page['items']), 1)
                self.assertEqual(page['items'][0]['snippets'][0]['message_index'], 0)
                self.assertNotIn('tokens_total', page['items'][0])
                self.assertFalse(page['partial'])

    def test_source_error_counts_and_bounded_details_survive_compaction(self):
        reader = self.codex()
        idx = reader._HistoryReader__indexer
        idx._coverage = {('file-session' if n == 0 else 'failed-%d' % n): {
            'content_status': 'partial_source_error', 'unsupported_steps': [],
            'source_error': {'error_code': 'agy_source_limit_exceeded', 'path': '/fixture/%d.db' % n,
                             'size_bytes': None, 'limit_bytes': None,
                             'budget_remaining_bytes': None, 'reason': 'step_count_limit_exceeded',
                             'step_count': 10001, 'limit_steps': 10000}}
            for n in range(7)}
        page = service.search(idx, query='artifact.png', brief=True)
        self.assertTrue(page['partial'])
        warning = page['content_warnings'][0]
        self.assertEqual(warning['failed_sessions'], 7)
        self.assertEqual(len(warning['source_errors']), 5)
        self.assertTrue(warning['source_errors_truncated'])
        self.assertIsNone(page['items'][0]['source_error']['size_bytes'])
        self.assertEqual(page['items'][0]['source_error']['step_count'], 10001)


    def test_native_unknown_project_scope_matches_existing_project_labels(self):
        for source in ('zcode', 'opencode', 'hermes'):
            with self.subTest(source=source):
                idx, path = self.native(source)
                table, column = ('sessions', 'source') if source == 'hermes' else ('session', 'directory')
                label = '(unknown source)' if source == 'hermes' else '(unknown directory)'
                with sqlite3.connect(path) as conn:
                    conn.execute(f"UPDATE {table} SET {column}='' WHERE id='chosen'")
                page = service.search(idx, query='needle', cwd=label, brief=True)
                self.assertEqual([item['id'] for item in page['items']], ['chosen'])
                self.assertEqual(page['items'][0]['cwd'], label)
                with patch('history_core.compact_search.session_page', side_effect=ValueError('public_unavailable')):
                    page = service.search(idx, query='needle', cwd=label, brief=True)
                self.assertEqual([item['id'] for item in page['items']], ['chosen'])
                self.assertEqual(page['evidence_authority'], 'unverified_session_metadata')

    def test_native_display_indices_match_renderer_at_schema_edges(self):
        for source in ('zcode', 'opencode'):
            with self.subTest(source=source):
                idx, path = self.native(source)
                values = [None, False, 0, '', [], {}, True, 12, ' ', ['text'], {'text': 'object'}, True, {}, {'text':'public'}]
                with sqlite3.connect(path) as conn:
                    conn.execute("DELETE FROM part WHERE session_id='chosen'")
                    for n, value in enumerate(values):
                        data = {'type': ' text ' if n % 2 else 'reasoning', 'text': value}
                        conn.execute('INSERT INTO part VALUES(?,?,?,?,?,?,?)',
                                     ('edge-%02d'%n, 'a', 'chosen', TS+n, TS+n, json.dumps(data), n))
                    for n, envelope in enumerate(('{}', '[]', 'null', 'false', '0', '"str"')):
                        mid = 'invalid-%d' % n
                        conn.execute('INSERT INTO message VALUES(?,?,?,?,?,?)', (mid,'chosen',TS,TS,envelope,10+n))
                        conn.execute('INSERT INTO part VALUES(?,?,?,?,?,?,?)',
                                     (mid,mid,'chosen',TS,TS,json.dumps({'type':'text','text':'INVALID_ENV'}),0))
                flat = idx._load_flat_messages('chosen')
                for n, expected in enumerate(flat):
                    if expected['kind'] == 'reasoning_summary':
                        with self.assertRaisesRegex(ValueError, 'message_not_public'):
                            service.read_native_message(idx, 'chosen', n)
                    else:
                        actual = service.read_native_message(idx, 'chosen', n)
                        self.assertEqual(actual['text'], expected['text'])
                        self.assertEqual(actual['kind'], expected['kind'])
                self.assertEqual(service.search(idx, query='INVALID_ENV', brief=True)['items'], [])

    def test_native_literal_path_allows_more_than_twelve_space_separated_parts(self):
        phrase = '/tmp/folder a b c d e f g h i j k l/file.txt'
        for source in ('zcode', 'opencode', 'hermes'):
            with self.subTest(source=source):
                idx, path = self.native(source)
                with sqlite3.connect(path) as conn:
                    if source == 'hermes':
                        conn.execute('UPDATE messages SET content=? WHERE id=1', (phrase,))
                    else:
                        conn.execute("UPDATE part SET data=? WHERE id='p0'", (json.dumps({'type':'text','text':phrase}),))
                page = service.search(idx, query=phrase, field='files_touched', brief=True, role='user')
                self.assertEqual(page['items'][0]['snippet_status'], 'matched')
                self.assertIn(phrase, page['items'][0]['snippets'][0]['text'])
                with patch('history_core.compact_search.session_page', side_effect=ValueError('public_unavailable')):
                    page = service.search(idx, query=phrase, field='files_touched', brief=True, role='user')
                self.assertEqual(page['evidence_authority'], 'unverified_session_metadata')
                self.assertEqual(page['match_semantics'], 'not_applied')
                self.assertFalse(page['limitations'][0]['field_applied'])


    def test_native_user_lookup_renders_only_matching_parts_with_large_unrelated_tools(self):
        idx, path = self.native('zcode')
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE part SET data=? WHERE id='p0'", (json.dumps({'type':'text','text':'quota targetneedle'}),))
            conn.execute("UPDATE part SET data=? WHERE id='p3'", (json.dumps({'type':'tool','tool':'bash','state':{
                'status':'completed','input':{'command':'unrelated'},'output':'targetneedle '+'x'*(3*1024*1024)}}),))
            for n in range(20):
                sid, mid = 'bulk-%d'%n, 'bulk-message-%d'%n
                conn.execute('INSERT INTO session(id,title,time_created,time_updated) VALUES(?,?,?,?)', (sid,'Unrelated',TS,TS))
                conn.execute('INSERT INTO message VALUES(?,?,?,?,?,?)',(mid,sid,TS,TS,json.dumps({'role':'assistant'}),0))
                data = json.dumps({'type':'tool','tool':'bash','state':{'status':'completed','output':'targetneedle '+'x'*1000}})
                conn.executemany('INSERT INTO part VALUES(?,?,?,?,?,?,?)',
                    ((mid+'-%d'%i,mid,sid,TS+i,TS+i,data,i) for i in range(100)))
        rendered = []
        flatten = idx._flatten_part
        def inspect(*args):
            rendered.append((args[0],args[1],args[2].get('text')))
            return flatten(*args)
        with patch.object(idx,'_flatten_part',side_effect=inspect):
            page = service.search(idx,query='targetneedle',role='user',brief=True,limit=3)
            selected = service.read_native_message(idx,'chosen',0)
        self.assertEqual([item['id'] for item in page['items']],['chosen'])
        self.assertFalse(page['partial'])
        self.assertEqual(selected['text'],'quota targetneedle')
        self.assertTrue(rendered)
        self.assertTrue(all(role=='user' and kind=='text' and text=='quota targetneedle'
                            for role,kind,text in rendered))

    def test_native_oversized_selected_public_record_is_explicit_and_never_rendered(self):
        idx, path = self.native('zcode')
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE part SET data=? WHERE id='p3'", (json.dumps({'type':'tool','tool':'bash','state':{
                'status':'completed','output':'BIG_SELECTED '+'x'*(3*1024*1024)}}),))
        rendered = []
        flatten = idx._flatten_part
        def inspect(*args):
            rendered.append(args[1])
            return flatten(*args)
        with patch.object(idx,'_flatten_part',side_effect=inspect):
            page = service.search(idx,query='BIG_SELECTED',role='tool',brief=True)
            with self.assertRaisesRegex(ValueError,'native_public_message_limit_exceeded'):
                service.read_native_message(idx,'chosen',3)
        self.assertEqual(rendered,[])
        self.assertTrue(page['partial'])
        item = page['items'][0]
        self.assertEqual(item['snippet_status'],'unavailable')
        self.assertEqual(item['candidate_match_status'],'unverified')
        self.assertIn('native_public_message_limit_exceeded',json.dumps(item['limitations']))

    def test_native_tool_coarse_filter_preserves_public_prefixes_and_decoded_json(self):
        idx,path = self.native('zcode')
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE part SET data=? WHERE id='p3'", (json.dumps({'type':'tool','tool':'bash','state':{
                'status':'completed','input':json.dumps({'command':'额度管理'}),'output':'done'}}),))
        for query in ('额度','Tool use:','Input:','"command":'):
            with self.subTest(query=query):
                page = service.search(idx,query=query,role='tool',brief=True)
                self.assertEqual([item['id'] for item in page['items']],['chosen'])
                self.assertTrue(page['items'][0]['snippets'])
                self.assertIn(query,page['items'][0]['snippets'][0]['text'])


    def test_native_tool_coarse_filter_keeps_rendered_json_null(self):
        for source in ('opencode','zcode'):
            with self.subTest(source=source):
                idx,path = self.native(source)
                with sqlite3.connect(path) as conn:
                    conn.execute("UPDATE part SET data=? WHERE id='p3'", (json.dumps({'type':'tool','tool':'bash','state':{
                        'status':'completed','input':{'value':None},'output':'done'}}),))
                page = service.search(idx,query='null',role='tool',brief=True)
                self.assertEqual([item['id'] for item in page['items']],['chosen'])
                self.assertIn('"value": null',page['items'][0]['snippets'][0]['text'])


if __name__ == '__main__':
    unittest.main()
