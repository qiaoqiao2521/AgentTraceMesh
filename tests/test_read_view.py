"""Read transactions isolate derived queries without freezing source writers."""
from collections import OrderedDict
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from history_core.read_view import indexed_read_view, reader_revision
from history_core.sources import Indexer


class ReadViewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'sessions'
        self.source.mkdir()
        self.recording = self.source / 'fixture.jsonl'
        self.original = b'{"synthetic": "source remains unchanged"}\n'
        self.recording.write_bytes(self.original)
        self.database = self.root / 'index ?# fixture.sqlite'
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        writer.row_factory = sqlite3.Row
        writer.execute('PRAGMA journal_mode=WAL')
        writer.executescript('''
            CREATE TABLE reader_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO reader_state VALUES ('revision', 'old-revision');
            CREATE TABLE sessions(id TEXT PRIMARY KEY, start_ts_ms INTEGER,
                end_ts_ms INTEGER, title TEXT, message_count INTEGER,
                cwd TEXT, pinned INTEGER);
            INSERT INTO sessions VALUES ('fixture', 1, 2, 'Old title', 1, '/fixture', 0);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, text TEXT);
            INSERT INTO messages VALUES (1, 'fixture', 'Old body');
        ''')
        # Reuse real query methods without invoking Indexer's schema migration.
        self.indexer = Indexer.__new__(Indexer)
        self.indexer.source = 'codex'
        self.indexer.sessions_dir = self.source
        self.indexer.db_path = self.database
        self.indexer.conn = writer
        self.indexer.lock = threading.Lock()
        self.indexer._session_preview_cache = OrderedDict([('parent', {'title': 'kept'})])

    def tearDown(self):
        self.assertEqual(self.recording.read_bytes(), self.original)

    def update_index(self):
        with self.indexer.conn:
            self.indexer.conn.execute("UPDATE sessions SET title='New title', message_count=2")
            self.indexer.conn.execute("INSERT INTO messages VALUES (2, 'fixture', 'New body')")
            self.indexer.conn.execute("UPDATE reader_state SET value='new-revision' WHERE key='revision'")

    def test_existing_view_stays_old_and_new_view_observes_commit(self):
        expected = hashlib.sha256(('codex' + str(self.source) + 'old-revision').encode()).hexdigest()
        with indexed_read_view(self.indexer) as view:
            self.assertEqual(reader_revision(view), expected)
            self.update_index()
            self.assertEqual(view.get_session_metadata('fixture')['title'], 'Old title')
            self.assertEqual(view.get_session_metadata('fixture')['message_total'], 1)
            self.assertEqual(view.conn.execute('SELECT text FROM messages ORDER BY id').fetchall()[0]['text'], 'Old body')
            self.assertEqual(reader_revision(view), expected)
            with indexed_read_view(self.indexer) as newer:
                self.assertNotEqual(reader_revision(newer), expected)
                self.assertEqual(newer.get_session_metadata('fixture')['title'], 'New title')
                self.assertEqual(newer.get_session_metadata('fixture')['message_total'], 2)

    def test_does_not_hold_parent_lock_or_initialize_indexer(self):
        self.indexer.lock.acquire()
        try:
            with patch.object(Indexer, '__init__', side_effect=AssertionError('no migrations')):
                with indexed_read_view(self.indexer) as view:
                    with view.lock:
                        self.assertEqual(view.get_session_metadata('fixture')['title'], 'Old title')
                    self.assertIsNot(view.conn, self.indexer.conn)
                    self.assertIsNot(view.lock, self.indexer.lock)
                    self.assertEqual(view._session_preview_cache, {})
                    view._remember_session_preview_cache('child', {'title': 'isolated'})
        finally:
            self.indexer.lock.release()
        self.assertEqual(list(self.indexer._session_preview_cache), ['parent'])

    def test_view_cannot_write_and_parent_stays_writable(self):
        with indexed_read_view(self.indexer) as view:
            self.assertEqual(view.conn.execute('PRAGMA query_only').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                view.conn.execute("UPDATE sessions SET title='Forbidden'")
        self.update_index()
        self.assertEqual(self.indexer.get_session_metadata('fixture')['title'], 'New title')

    def test_query_failure_closes_view_and_leaves_parent_usable(self):
        with self.assertRaises(sqlite3.OperationalError):
            with indexed_read_view(self.indexer) as view:
                view.conn.set_progress_handler(lambda: 1, 1)
                view.conn.execute('SELECT * FROM messages').fetchall()
        with self.assertRaises(sqlite3.ProgrammingError):
            view.conn.execute('SELECT 1')
        with indexed_read_view(self.indexer) as recovered:
            self.assertEqual(recovered.get_session_metadata('fixture')['message_total'], 1)
        self.update_index()

    def test_native_or_missing_reader_state_is_unsupported(self):
        for native in ('opencode', 'hermes', 'agy'):
            with self.subTest(source=native), self.assertRaisesRegex(ValueError, 'indexed_snapshot_unsupported'):
                with indexed_read_view(SimpleNamespace(source=native, db_path=self.database)):
                    self.fail('native store must not become a derived view')
        with self.indexer.conn:
            self.indexer.conn.execute('DROP TABLE reader_state')
        with self.assertRaisesRegex(ValueError, 'indexed_snapshot_unsupported'):
            with indexed_read_view(self.indexer):
                self.fail('missing revision must not yield a view')

    def test_rejects_linked_database_and_linked_parent(self):
        for path in (self.root / 'linked.sqlite', self.root / 'linked-cache' / self.database.name):
            with self.subTest(path=path):
                if path.parent == self.root:
                    path.symlink_to(self.database)
                else:
                    path.parent.symlink_to(self.root, target_is_directory=True)
                self.indexer.db_path = path
                with self.assertRaisesRegex(ValueError, 'cache_symlink_not_allowed'):
                    with indexed_read_view(self.indexer):
                        self.fail('linked cache must not be opened')

    def test_rejects_nonregular_target_and_does_not_create_missing_database(self):
        self.indexer.db_path = self.root
        with self.assertRaisesRegex(ValueError, 'index_database_regular_file_required'):
            with indexed_read_view(self.indexer):
                self.fail('directory must not be opened')
        self.indexer.db_path = self.root / 'missing.sqlite'
        with self.assertRaises(FileNotFoundError):
            with indexed_read_view(self.indexer):
                self.fail('missing cache must not be initialized')
        self.assertFalse(self.indexer.db_path.exists())


if __name__ == '__main__':
    unittest.main()
