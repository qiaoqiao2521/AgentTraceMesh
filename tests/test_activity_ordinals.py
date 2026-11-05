import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from history_core.activity import _activity_message_ids, read_store, window
from history_core.message_ordinals import ordered_message_ids_sql
from history_core.read_view import indexed_read_view
from history_core.sources import Indexer


class ActivityOrdinalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.recordings = self.root / 'recordings'
        self.recordings.mkdir()
        self.path = self.recordings / 'session.jsonl'
        self.path.write_text('')
        self.indexer = Indexer(self.recordings, self.root, 'codex')
        self.addCleanup(self.indexer.conn.close)
        self.start, self.end = window('2026-11-05', 'Asia/Shanghai')
        self.messages = [
            dict(ts_ms=self.start + 300, role='assistant', kind='message', text='needle late'),
            dict(ts_ms=self.start + 200, role='user', kind='message', text='needle first tie'),
            dict(ts_ms=self.start + 100, role='system', kind='context', text='context occupies slot'),
            dict(ts_ms=self.start + 200, role='assistant', kind='message', text='needle second tie'),
            dict(ts_ms=None, role='tool', kind='tool_result', text='unknown verified time'),
        ]
        self.store()

    def store(self):
        session = dict(id='session', file_path=str(self.path), start_ts_ms=self.start + 100,
                       end_ts_ms=self.start + 300, title='Fixture', cwd='/fixture',
                       message_count=len(self.messages), search_blob='', messages=self.messages)
        with self.indexer.conn:
            row = self.indexer.conn.execute('SELECT * FROM sessions WHERE id=?', ('session',)).fetchone()
            self.indexer._store_parsed_session(row, session, 1, 'unchanged-signature', self.path,
                                               {self.path}, build_audits=False)
            self.indexer.conn.execute("UPDATE reader_state SET value=lower(hex(randomblob(16))) WHERE key='revision'")

    def read(self, **kwargs):
        return read_store(self.indexer, 'codex', self.recordings,
                          date='2026-11-05', timezone='Asia/Shanghai', **kwargs)

    def ids(self, conn=None, **kwargs):
        return _activity_message_ids(conn or self.indexer.conn, 'session', self.start,
                                     self.end, kwargs.get('message_index'),
                                     kwargs.get('limit', 100), kwargs.get('offset', 0))

    def test_unordered_ties_context_and_unknown_time_share_reader_slots(self):
        stored = self.indexer.conn.execute(
            'SELECT message_index FROM messages ORDER BY id').fetchall()
        self.assertEqual([row[0] for row in stored], [4, 2, 0, 3, 1])
        page = self.indexer.get_session_messages_page('session')
        self.assertEqual([row['message_index'] for row in page['messages']], list(range(5)))
        evidence = self.read()['items'][0]['evidence']
        self.assertEqual([row['message_index'] for row in evidence], [2, 3, 4])
        matches = self.indexer.search_session_messages('session', 'needle')['matches']
        self.assertEqual([row['message_index'] for row in matches], [2, 3, 4])
        for row in evidence:
            message = self.indexer.get_session_message('session', row['message_index'])
            self.assertEqual(message['text'], row['text'])
        self.assertEqual(self.read()['coverage']['messages_without_verified_time'], 1)

    def test_reparse_reassigns_absolute_slots_and_rejects_stale_pagination(self):
        first = self.read(evidence_limit=1)
        second = self.read(evidence_limit=1, message_offset=1,
                           index_revision=first['index_revision'])
        self.assertEqual(second['items'][0]['evidence'][0]['message_index'], 3)
        self.messages.append(dict(ts_ms=self.start + 50, role='system', kind='context', text='earlier'))
        self.store()
        self.assertEqual([row['message_index'] for row in self.read()['items'][0]['evidence']], [3, 4, 5])
        with self.assertRaisesRegex(ValueError, 'revision_changed'):
            self.read(message_offset=1, index_revision=first['index_revision'])

    def test_exact_slot_uses_full_session_before_window_and_public_filter(self):
        self.assertEqual(self.ids(message_index=0), [])
        self.assertEqual(self.ids(message_index=1), [])
        exact = self.read(session_id='session', message_index=3)['items'][0]['evidence']
        self.assertEqual([(row['message_index'], row['text']) for row in exact], [(3, 'needle second tie')])

    def test_old_schema_numbers_ids_without_accessing_text(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.executescript('''CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,
            ts_ms INTEGER,role TEXT,kind TEXT,text TEXT,activity_ts_ms INTEGER);
            CREATE INDEX idx_messages_session_ts ON messages(session_id,ts_ms);''')
        connection.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', [
            (1, 'session', self.start + 300, 'user', 'message', 'X' * 1024 * 1024, self.start + 300),
            (2, 'session', self.start + 100, 'system', 'context', 'context', self.start + 100),
            (3, 'session', self.start + 200, 'assistant', 'message', 'older', self.start + 200),
        ])
        def no_body(action, table, column, database, trigger):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'messages' and column == 'text' else sqlite3.SQLITE_OK
        connection.set_authorizer(no_body)
        with self.assertRaises(sqlite3.DatabaseError):
            connection.execute('''WITH numbered AS (SELECT *,ROW_NUMBER() OVER (ORDER BY ts_ms,id)-1 message_index
                FROM messages WHERE session_id=?) SELECT * FROM numbered ORDER BY activity_ts_ms DESC LIMIT 1''', ('session',)).fetchall()
        result = self.ids(connection, limit=1)
        self.assertEqual([(row['id'], row['message_index']) for row in result], [(1, 2)])
        ordered, params = ordered_message_ids_sql(connection, 'session')
        plan = list(connection.execute('EXPLAIN QUERY PLAN ' + ordered, params))
        self.assertTrue(any('COVERING INDEX idx_messages_session_ts' in row[3] for row in plan))
        self.assertFalse(any('TEMP B-TREE' in row[3] for row in plan))

    def test_large_bodies_are_fetched_only_after_scalar_paging(self):
        self.messages[0]['text'] = 'X' * (8 * 1024 * 1024)
        self.messages[1]['text'] = 'Y' * (8 * 1024 * 1024)
        self.store()
        connection = self.indexer.conn
        connection.set_authorizer(lambda action, table, column, database, trigger:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'messages' and column == 'text' else sqlite3.SQLITE_OK)
        try:
            result = self.ids(limit=1)
        finally:
            connection.set_authorizer(None)
        self.assertEqual([row['message_index'] for row in result], [4])
        ordered, params = ordered_message_ids_sql(connection, 'session')
        self.assertNotIn('ROW_NUMBER', ordered)
        query = f'SELECT id,message_index FROM ({ordered}) WHERE id IN (?)'
        plan = list(connection.execute('EXPLAIN QUERY PLAN ' + query, (*params, result[0]['id'])))
        self.assertTrue(any('INTEGER PRIMARY KEY' in row[3] for row in plan))
        statements = []
        connection.set_trace_callback(statements.append)
        try:
            report = self.read(evidence_limit=1)
        finally:
            connection.set_trace_callback(None)
        self.assertEqual(report['coverage']['evidence_messages_read'], 1)
        body_queries = [sql for sql in statements if 'SELECT id,activity_ts_ms,role,kind,text' in sql]
        self.assertEqual(len(body_queries), 1)
        self.assertIn('WHERE id IN (', body_queries[0])
        self.assertNotIn('ORDER BY', body_queries[0])
        candidate = next(sql for sql in statements if sql.startswith('SELECT m.id,m.activity_ts_ms'))
        candidate_plan = list(connection.execute('EXPLAIN QUERY PLAN ' + candidate))
        self.assertTrue(any('idx_messages_activity (activity_ts_ms>?' in row[3] for row in candidate_plan))

    def test_session_metadata_is_read_after_window_aggregation(self):
        # The metadata column follows a potentially large session search_blob.
        # Its VM read must occur after aggregating messages, rather than once
        # for every input row copied into the grouping sorter.
        statements = []
        connection = self.indexer.conn
        connection.set_trace_callback(statements.append)
        try:
            self.read()
        finally:
            connection.set_trace_callback(None)
        grouping = next(sql for sql in statements if 'GROUP BY' in sql and 'message_count' in sql)
        program = list(connection.execute('EXPLAIN ' + grouping))
        rootpage = connection.execute("SELECT rootpage FROM sqlite_schema WHERE name='sessions'").fetchone()[0]
        cursors = {row[2] for row in program if row[1] == 'OpenRead' and row[3] == rootpage}
        metadata_column = next(row[0] for row in connection.execute('PRAGMA table_info(sessions)')
                               if row[1] == 'activity_metadata_json')
        metadata_reads = [row[0] for row in program
                          if row[1] == 'Column' and row[2] in cursors and row[3] == metadata_column]
        aggregate_end = max(row[0] for row in program if row[1] == 'AggFinal')
        self.assertTrue(metadata_reads)
        self.assertGreater(min(metadata_reads), aggregate_end)

    def test_column_migration_does_not_reparse_or_invalidate_old_cache(self):
        connection = self.indexer.conn
        with connection:
            connection.execute('ALTER TABLE messages DROP COLUMN message_index')
            connection.execute('ALTER TABLE sessions DROP COLUMN message_index_count')
            connection.execute("UPDATE sessions SET activity_metadata_json='{}'")
        statements = []
        connection.set_trace_callback(statements.append)
        try:
            with patch.object(self.indexer, '_parse_file_fn', side_effect=AssertionError('unexpected parse')):
                self.indexer._init_db()
        finally:
            connection.set_trace_callback(None)
        self.assertEqual(connection.execute('SELECT file_signature FROM sessions').fetchone()[0], 'unchanged-signature')
        self.assertEqual(connection.execute('SELECT COUNT(message_index) FROM messages').fetchone()[0], 0)
        self.assertFalse(any('UPDATE messages' in sql or 'UPDATE sessions SET file_signature' in sql for sql in statements))
        self.assertEqual([row['message_index'] for row in self.read()['items'][0]['evidence']], [2, 3, 4])
        coverage = self.read()['coverage']
        self.assertIsNone(coverage['messages_without_verified_time'])
        self.assertIsNone(coverage['unparsed_indexed_records'])
        self.assertEqual(coverage['record_count_sessions_unknown'], 1)

    def test_old_writer_append_uses_fallback_instead_of_stale_persisted_slots(self):
        writer = sqlite3.connect(self.indexer.db_path)
        self.addCleanup(writer.close)
        with indexed_read_view(self.indexer) as view:
            with writer:
                writer.execute('''INSERT INTO messages(session_id,ts_ms,role,kind,text,activity_ts_ms)
                    VALUES(?,?,?,?,?,?)''', ('session', self.start + 50, 'system', 'context', 'new context', self.start + 50))
                writer.execute("UPDATE reader_state SET value='writer-append' WHERE key='revision'")
            self.assertEqual([row['message_index'] for row in self.ids(view.conn)], [4, 3, 2])
        self.assertEqual([row['message_index'] for row in self.ids()], [5, 4, 3])

    def test_old_writer_replaces_session_without_new_columns(self):
        writer = sqlite3.connect(self.indexer.db_path)
        self.addCleanup(writer.close)
        with writer:
            writer.execute('DELETE FROM messages WHERE session_id=?', ('session',))
            writer.execute('''INSERT OR REPLACE INTO sessions(id,file_path,title,activity_metadata_json)
                VALUES(?,?,?,?)''', ('session', str(self.path), 'old writer', '{}'))
            writer.executemany('''INSERT INTO messages(session_id,ts_ms,role,kind,text,activity_ts_ms)
                VALUES(?,?,?,?,?,?)''', [('session', self.start + value, 'user', 'message', str(value), self.start + value)
                                        for value in (300, 100, 200)])
            writer.execute("UPDATE reader_state SET value='writer-replace' WHERE key='revision'")
        self.assertIsNone(self.indexer.conn.execute('SELECT message_index_count FROM sessions').fetchone()[0])
        self.assertEqual([row['message_index'] for row in self.ids()], [2, 1, 0])


if __name__ == '__main__':
    unittest.main()
