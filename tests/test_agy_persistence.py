"""Disposable synthetic AGY stores exercise the persisted derived index."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from history_core.agy import AgyIndexer, MAX_SESSION_BYTES
from test_agy import blob, step


class AgyPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'source'
        self.root.mkdir()
        (self.root / 'conversations').mkdir()
        self.cache = Path(self.tmp.name) / 'cache'
        self.summaries = self.root / 'conversation_summaries.db'
        with sqlite3.connect(self.summaries) as db:
            db.execute('CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY,title TEXT,step_count INTEGER,'
                       'last_modified_time TEXT,workspace_uris TEXT)')

    def write(self, sid, text):
        path = self.root / 'conversations' / (sid + '.db')
        with sqlite3.connect(self.summaries) as db:
            db.execute('INSERT INTO conversation_summaries VALUES(?,?,?,?,?)',
                       (sid, 'Synthetic ' + sid, 1, '2026-11-05T12:00:00Z', '[]'))
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE steps(idx INTEGER PRIMARY KEY,step_type INTEGER,status INTEGER,step_format INTEGER,step_payload BLOB)')
            db.execute('INSERT INTO steps VALUES(0,14,3,0,?)', (step(14, blob(1, text)),))
        return path

    def reader(self):
        reader = AgyIndexer(self.summaries, data_dir=self.cache)
        self.addCleanup(reader.conn.close)
        return reader

    def child(self, body):
        script = ('import json,sys\nfrom pathlib import Path\nfrom unittest.mock import patch\n'
                  'from history_core.agy import AgyIndexer\n' + body)
        result = subprocess.run([sys.executable, '-c', script, str(self.summaries), str(self.cache)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                                env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_new_process_warm_cache_does_not_snapshot_or_decode_source(self):
        self.write('one', 'kept across processes')
        cold = self.child('r=AgyIndexer(sys.argv[1],data_dir=sys.argv[2])\n'
                          'print(json.dumps({"coverage":r.coverage(),"cache":r.cache_info()}))\nr.conn.close()')
        warm = self.child('with patch("history_core.agy.read_agy_session", side_effect=AssertionError("decoded")), '
                          'patch("history_core.agy._read_only", side_effect=AssertionError("snapshot")):\n'
                          ' r=AgyIndexer(sys.argv[1],data_dir=sys.argv[2])\n'
                          ' print(json.dumps({"coverage":r.coverage(),"cache":r.cache_info(),'
                          '"text":r.get_session_message("one",0)["text"]}))\n r.conn.close()')
        self.assertEqual(warm['coverage'], cold['coverage'])
        self.assertEqual(warm['cache']['status'], 'warm')
        self.assertEqual(warm['text'], 'kept across processes')

    def test_new_process_adds_search_index_without_redecoding_warm_cache(self):
        self.write('one', 'retained while adding a derived index')
        reader = self.reader()
        coverage, revision = reader.coverage(), reader._revision
        version = reader.cache_info()['version']
        cache_path = reader.cache_path
        reader.conn.close()
        with sqlite3.connect(cache_path) as db:
            db.execute('DROP INDEX idx_messages_search_scope')
        warm = self.child('with patch("history_core.agy.read_agy_session", side_effect=AssertionError("decoded")), '
                          'patch("history_core.agy._read_only", side_effect=AssertionError("snapshot")):\n'
                          ' r=AgyIndexer(sys.argv[1],data_dir=sys.argv[2])\n'
                          ' print(json.dumps({"coverage":r.coverage(),"cache":r.cache_info(),'
                          '"revision":r._revision,"text":r.get_session_message("one",0)["text"],'
                          '"index_columns":[row[2] for row in r.conn.execute('
                          '\"PRAGMA index_info(idx_messages_search_scope)\")]}))\n r.conn.close()')
        self.assertEqual(warm['index_columns'], ['session_id', 'role', 'kind'])
        self.assertEqual(warm['coverage'], coverage)
        self.assertEqual(warm['revision'], revision)
        self.assertEqual(warm['cache']['version'], version)
        self.assertEqual(warm['cache']['status'], 'warm')
        self.assertEqual(warm['cache']['mode'], 'persistent')
        self.assertIsNone(warm['cache']['warning'])
        self.assertEqual(warm['text'], 'retained while adding a derived index')

    def test_new_process_decodes_only_changed_session(self):
        self.write('one', 'unchanged')
        changed = self.write('two', 'before')
        reader = self.reader()
        reader.conn.close()
        with sqlite3.connect(changed) as db:
            db.execute('UPDATE steps SET step_payload=?', (step(14, blob(1, 'after')),))
        result = self.child('import history_core.agy as agy\nseen=[]\noriginal=agy.read_agy_session\n'
                            'def read(path,**kwargs):\n seen.append(Path(path).stem)\n return original(path,**kwargs)\n'
                            'with patch.object(agy,"read_agy_session",side_effect=read):\n'
                            ' r=AgyIndexer(sys.argv[1],data_dir=sys.argv[2])\n'
                            ' print(json.dumps({"seen":seen,"text":r.get_session_message("two",0)["text"],'
                            '"count":r.coverage()["total_sessions"],"cache":r.cache_info()}))\n r.conn.close()')
        self.assertEqual(result['seen'], ['two'])
        self.assertEqual(result['text'], 'after')
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['cache']['status'], 'updated')

    def test_summary_row_edit_and_removal_reuse_other_session(self):
        self.write('one', 'kept')
        self.write('two', 'changed title')
        self.write('removed', 'old')
        reader = self.reader()
        with sqlite3.connect(self.summaries) as db:
            db.execute('UPDATE conversation_summaries SET title=? WHERE conversation_id=?', ('Revised title', 'two'))
            db.execute('DELETE FROM conversation_summaries WHERE conversation_id=?', ('removed',))
        from history_core.agy import read_agy_session
        with patch('history_core.agy.read_agy_session', wraps=read_agy_session) as decode:
            reader.scan_sessions()
        self.assertEqual([call.args[0].stem for call in decode.call_args_list], ['two'])
        self.assertEqual(reader.get_session_metadata('two')['title'], 'Revised title')
        self.assertIsNone(reader.get_session_metadata('removed'))
        self.assertEqual(reader.coverage()['total_sessions'], 2)

    def test_corrupt_cache_falls_back_with_disclosed_warning_and_preserves_file(self):
        self.write('one', 'source survives bad cache')
        reader = self.reader()
        cache_path = reader.cache_path
        reader.conn.close()
        cache_path.write_bytes(b'bad derived sqlite file')
        fallback = self.reader()
        self.assertEqual(fallback.cache_info()['status'], 'fallback_memory')
        self.assertEqual(fallback.cache_info()['mode'], 'memory')
        self.assertIn('agy_cache_unavailable', fallback.cache_info()['warning'])
        self.assertEqual(fallback.get_session_message('one', 0)['text'], 'source survives bad cache')
        self.assertEqual(cache_path.read_bytes(), b'bad derived sqlite file')

    def test_failed_refresh_rolls_back_persisted_messages_revision_and_coverage(self):
        path = self.write('one', 'old committed index')
        reader = self.reader()
        revision, coverage = reader._revision, reader.coverage()
        with sqlite3.connect(path) as db:
            db.execute('UPDATE steps SET step_payload=?', (step(14, blob(1, 'uncommitted index')),))
        with patch.object(reader, 'source_revision', side_effect=['before', 'after', 'retry-before', 'retry-after']):
            with self.assertRaisesRegex(ValueError, 'snapshot_attempts.*2'):
                reader.scan_sessions()
        self.assertEqual(reader._revision, revision)
        self.assertEqual(reader.coverage(), coverage)
        with sqlite3.connect(reader.cache_path) as db:
            self.assertEqual(db.execute('SELECT text FROM messages').fetchone()[0], 'old committed index')
            self.assertEqual(db.execute("SELECT value FROM reader_state WHERE key='agy_revision'").fetchone()[0], revision)
        reader.scan_sessions()
        self.assertEqual(reader.get_session_message('one', 0)['text'], 'uncommitted index')

    def test_process_writer_commits_while_existing_reader_holds_old_snapshot(self):
        path = self.write('one', 'before writer')
        reader = self.reader()
        reader.conn.execute('BEGIN')
        self.assertEqual(reader.conn.execute('SELECT text FROM messages').fetchone()[0], 'before writer')
        with sqlite3.connect(path) as db:
            db.execute('UPDATE steps SET step_payload=?', (step(14, blob(1, 'after writer')),))
        result = self.child('r=AgyIndexer(sys.argv[1],data_dir=sys.argv[2])\n'
                            'print(json.dumps({"text":r.get_session_message("one",0)["text"],"cache":r.cache_info()}))\nr.conn.close()')
        self.assertEqual(result['cache']['mode'], 'persistent')
        self.assertEqual(result['text'], 'after writer')
        self.assertEqual(reader.conn.execute('SELECT text FROM messages').fetchone()[0], 'before writer')
        reader.conn.rollback()
        self.assertEqual(reader.conn.execute('SELECT text FROM messages').fetchone()[0], 'after writer')

    def test_new_warm_process_reads_cache_while_another_writer_has_transaction(self):
        self.write('one', 'complete preceding index')
        reader = self.reader()
        reader.conn.execute('BEGIN IMMEDIATE')
        self.addCleanup(reader.conn.rollback)
        result = self.child('import sqlite3\noriginal_connect=sqlite3.connect\n'
                            'def short_connect(*args,**kwargs):\n kwargs["timeout"]=0.05\n return original_connect(*args,**kwargs)\n'
                            'with patch("sqlite3.connect",side_effect=short_connect), '
                            'patch("history_core.agy.read_agy_session",side_effect=AssertionError("warm reader decoded source")):\n'
                            ' r=AgyIndexer(sys.argv[1],data_dir=sys.argv[2])\n'
                            ' print(json.dumps({"cache":r.cache_info(),"text":r.get_session_message("one",0)["text"]}))\n'
                            ' r.conn.close()')
        self.assertEqual(result['cache']['mode'], 'persistent')
        self.assertEqual(result['cache']['status'], 'warm')
        self.assertEqual(result['text'], 'complete preceding index')

    def test_concurrent_process_initialization_keeps_one_complete_cache(self):
        self.write('one', 'one')
        self.write('two', 'two')
        body = ('r=AgyIndexer(sys.argv[1],data_dir=sys.argv[2])\n'
                'print(json.dumps({"count":r.coverage()["total_sessions"],"cache":r.cache_info()}))\nr.conn.close()')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.child(body), range(2)))
        self.assertTrue(all(item['count'] == 2 for item in results))
        self.assertTrue(all(item['cache']['mode'] == 'persistent' for item in results), results)
        with sqlite3.connect(self.reader().cache_path) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)

    def test_summary_size_observation_race_retries_once(self):
        self.write('one', 'available after summary growth')
        from history_core.agy import _read_only
        observations = []
        def grow_before_snapshot(path, *, budget):
            if path == self.summaries:
                observations.append(budget.limit)
                if len(observations) == 1:
                    with sqlite3.connect(self.summaries) as db:
                        db.execute('CREATE TABLE unrelated(value BLOB)')
                        db.execute('INSERT INTO unrelated VALUES(?)', (b'x' * 20000,))
            return _read_only(path, budget=budget)
        with patch('history_core.agy._read_only', side_effect=grow_before_snapshot):
            reader = self.reader()
        self.assertEqual(len(observations), 2)
        self.assertGreater(observations[1], observations[0])
        self.assertEqual(reader.get_session_message('one', 0)['text'], 'available after summary growth')

    def test_wal_disappearing_between_exists_and_stat_retries_once(self):
        self.write('one', 'available after WAL checkpoint race')
        wal = Path(str(self.summaries) + '-wal')
        wal.write_bytes(b'')
        original_exists = Path.exists
        vanished = []
        def checkpoint_after_exists(path):
            exists = original_exists(path)
            if path == wal and exists and not vanished:
                vanished.append(str(path))
                path.unlink()
            return exists
        with patch.object(Path, 'exists', checkpoint_after_exists):
            reader = self.reader()
        self.assertEqual(vanished, [str(wal)])
        self.assertEqual(reader.get_session_message('one', 0)['text'], 'available after WAL checkpoint race')

    def test_repeated_source_oserror_with_changed_revision_stops_after_two(self):
        self.write('one', 'source keeps changing')
        wal = Path(str(self.summaries) + '-wal')
        attempts = []
        def changing_source(path, *, budget):
            attempts.append(str(path))
            with sqlite3.connect(self.summaries) as db:
                db.execute("UPDATE conversation_summaries SET title=title || '!' WHERE conversation_id='one'")
            raise FileNotFoundError(2, 'WAL changed before stat', str(wal))
        with patch('history_core.agy._read_only', side_effect=changing_source):
            with self.assertRaisesRegex(ValueError, 'source_changed_during_refresh.*snapshot_attempts.*2') as raised:
                self.reader()
        self.assertEqual(attempts, [str(self.summaries), str(self.summaries)])
        self.assertIn(str(wal), str(raised.exception))

    def test_static_source_oserror_is_not_retried(self):
        self.write('one', 'source has not changed')
        missing = str(self.summaries) + '-wal'
        with patch('history_core.agy._read_only', side_effect=FileNotFoundError(2, 'No such file', missing)) as snapshot:
            with self.assertRaises(FileNotFoundError) as raised:
                self.reader()
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(raised.exception.filename, missing)

    def test_repeated_summary_race_stops_after_two_and_keeps_context(self):
        self.write('one', 'not indexed from mixed snapshots')
        from history_core.agy import _read_only
        observations = []
        def grow_before_snapshot(path, *, budget):
            if path == self.summaries:
                observations.append(budget.limit)
                with sqlite3.connect(self.summaries) as db:
                    db.execute('CREATE TABLE IF NOT EXISTS unrelated(value BLOB)')
                    db.execute('INSERT INTO unrelated VALUES(?)', (b'x' * 20000,))
            return _read_only(path, budget=budget)
        with patch('history_core.agy._read_only', side_effect=grow_before_snapshot):
            with self.assertRaisesRegex(ValueError, 'agy_source_limit_exceeded.*snapshot_attempts.*2') as raised:
                self.reader()
        self.assertEqual(len(observations), 2)
        self.assertEqual(raised.exception.source_error['path'], str(self.summaries))
        self.assertEqual(raised.exception.source_error['reason'], 'physical_source_bytes')

    def test_partial_coverage_and_static_oversized_session_are_persisted(self):
        path = self.write('large', 'not read')
        with path.open('r+b') as stream:
            stream.truncate(MAX_SESSION_BYTES + 1)
        self.write('one', 'ordinary')
        from history_core.agy import read_agy_session
        with patch('history_core.agy.read_agy_session', wraps=read_agy_session) as decode:
            reader = self.reader()
        self.assertEqual([call.args[0].stem for call in decode.call_args_list], ['large', 'one'])
        coverage = reader.coverage()
        self.assertEqual(coverage['partial_sessions'], 1)
        error = coverage['session_errors'][0]
        self.assertEqual(error['limit_bytes'], MAX_SESSION_BYTES)
        self.assertGreater(error['size_bytes'], MAX_SESSION_BYTES)
        reader.conn.close()
        with patch('history_core.agy.read_agy_session', side_effect=AssertionError('unchanged partial source must not reread')):
            warm = self.reader()
        self.assertEqual(warm.coverage(), coverage)

    def test_version_or_cached_file_path_mismatch_rebuilds_from_selected_source(self):
        self.write('one', 'trusted selected source')
        for mutate in ("UPDATE reader_state SET value='old' WHERE key='agy_version'",
                       "UPDATE sessions SET file_path='/outside/untrusted.db'"):
            with self.subTest(mutate=mutate):
                reader = self.reader()
                cache_path = reader.cache_path
                reader.conn.close()
                with sqlite3.connect(cache_path) as db:
                    db.execute(mutate)
                from history_core.agy import read_agy_session
                with patch('history_core.agy.read_agy_session', wraps=read_agy_session) as decode:
                    rebuilt = self.reader()
                self.assertEqual(decode.call_count, 1)
                self.assertEqual(rebuilt.get_session_message('one', 0)['text'], 'trusted selected source')
                self.assertEqual(rebuilt.cache_info()['status'], 'rebuilt')
                rebuilt.conn.close()

    def test_source_stays_read_only_with_live_wal_and_cache_is_private(self):
        path = self.write('one', 'initial')
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('UPDATE steps SET step_payload=?', (step(14, blob(1, 'committed WAL text')),))
        writer.commit()
        before = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.root.rglob('*') if p.is_file()}
        reader = self.reader()
        after = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(reader.get_session_message('one', 0)['text'], 'committed WAL text')
        self.assertEqual(reader.cache_path.stat().st_mode & 0o777, 0o600)

    def test_cache_symlinks_and_source_overlap_are_rejected(self):
        self.write('one', 'ordinary')
        with self.assertRaisesRegex(ValueError, 'source_and_cache_must_be_disjoint'):
            AgyIndexer(self.summaries, data_dir=self.root / 'cache')
        self.cache.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'cache_symlink_not_allowed'):
            self.reader()
        self.cache.unlink()
        reader = self.reader()
        cache_path = reader.cache_path
        reader.conn.close()
        cache_path.unlink()
        cache_path.symlink_to(self.summaries)
        with self.assertRaisesRegex(ValueError, 'cache_symlink_not_allowed'):
            self.reader()

    def test_title_candidates_survive_warm_cache_with_explicit_sources(self):
        self.write('one', 'The actual user request')
        reader = self.reader()
        expected = [{'source': 'summary', 'title': 'Synthetic one'},
                    {'source': 'first_user_message', 'title': 'The actual user request'}]
        self.assertEqual(reader.get_session_metadata('one')['title_candidates'], expected)
        reader.conn.close()
        with patch('history_core.agy.read_agy_session', side_effect=AssertionError('warm cache')):
            warm = self.reader()
        self.assertEqual(warm.get_session_metadata('one')['title_candidates'], expected)


if __name__ == '__main__':
    unittest.main()
