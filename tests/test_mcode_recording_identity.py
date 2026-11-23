"""MCode recording identity regressions; all source files are synthetic."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from history_core import HistoryReader
from history_core.mcode_review import mcode_review_snapshot


class McodeRecordingIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'sessions'
        self.source.mkdir()
        self.cache = self.root / 'cache'

    def write(self, relative, text, *, parent=None):
        folder = self.source / relative
        folder.mkdir(parents=True, exist_ok=True)
        header = {'schemaVersion': 1, 'sessionId': 'shared-native',
                  'layout': 'v2-final-dated-session', 'parentSessionId': parent}
        (folder / 'manifest.json').write_text(json.dumps(header))
        row = {'message_id': 'user-1', 'message': {'role': 'user', 'timestamp': 1790526000000,
                                                'content': [{'type': 'text', 'text': text}]}}
        path = folder / 'messages.jsonl'
        path.write_text(json.dumps(row) + '\n')
        return path

    def reader(self):
        reader = HistoryReader('mcode', self.source, self.cache)
        self.addCleanup(reader.close)
        return reader

    @staticmethod
    def indexer(reader):
        return reader._HistoryReader__indexer

    def identities(self, reader):
        return {row['file_path']: row['id'] for row in self.indexer(reader).conn.execute(
            'SELECT id,file_path FROM sessions')}

    def test_same_manifest_id_preserves_parent_and_child_recordings(self):
        first = self.write('2026/11/03/parent', 'parent unique request')
        child = self.write('2026/11/03/parent/child', 'child unique request', parent='shared-native')
        before = {path: path.read_bytes() for path in self.source.rglob('*') if path.is_file()}
        reader = self.reader()
        receipt = reader.refresh()
        self.assertEqual(receipt['indexed_sessions'], 2)
        self.assertFalse(receipt.get('partial', False))
        identities = self.identities(reader)
        self.assertEqual(set(identities), {str(first), str(child)})
        self.assertEqual(len(set(identities.values())), 2)
        for path, text in ((first, 'parent unique request'), (child, 'child unique request')):
            identity = identities[str(path)]
            result = reader.search(query=text, brief=True)
            self.assertEqual([item['id'] for item in result['items']], [identity])
            snapshot = mcode_review_snapshot(self.indexer(reader), identity)
            self.assertEqual(snapshot['audit']['session_id'], identity)
            self.assertEqual(snapshot['provenance']['session_id'], identity)
            self.assertEqual(snapshot['provenance']['locator']['path'], str(path))
            self.assertEqual(snapshot['messages'][0]['text'], text)
            if identity != 'shared-native':
                self.assertTrue(snapshot['provenance']['identity_conflict'])
                self.assertEqual(snapshot['provenance']['native_session_id'], 'shared-native')
                metadata = self.indexer(reader).get_session_metadata(identity)
                self.assertTrue(metadata['identity_conflict'])
                self.assertEqual(metadata['native_session_id'], 'shared-native')
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_exact_copy_is_a_distinct_recording_without_automatic_deduplication(self):
        self.write('first', 'same copied public text')
        self.write('copy', 'same copied public text')
        reader = self.reader()
        reader.refresh()
        result = reader.search(query='same copied public text', brief=True)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(len({item['id'] for item in result['items']}), 2)
        self.assertTrue(all(item['snippets'] for item in result['items']))

    def test_alias_survives_refresh_edit_and_native_owner_removal(self):
        self.write('first', 'first request')
        self.write('second', 'second request')
        reader = self.reader()
        reader.refresh()
        identities = self.identities(reader)
        native_path = next(Path(path) for path, sid in identities.items() if sid == 'shared-native')
        alias_path, alias = next((Path(path), sid) for path, sid in identities.items() if sid != 'shared-native')
        native_path.unlink()
        self.write(alias_path.parent.relative_to(self.source), 'surviving edited request')
        reader.refresh()
        self.assertEqual(self.identities(reader), {str(alias_path): alias})
        self.assertEqual(mcode_review_snapshot(self.indexer(reader), alias)['messages'][0]['text'],
                         'surviving edited request')
        reader.refresh()
        self.assertEqual(self.identities(reader), {str(alias_path): alias})

    def test_tampered_alias_native_metadata_is_rejected(self):
        self.write('first', 'first request')
        self.write('second', 'second request')
        reader = self.reader()
        reader.refresh()
        alias = next(sid for sid in self.identities(reader).values() if sid != 'shared-native')
        indexer = self.indexer(reader)
        for metadata in ('{bad json', '[]', '{}',
                         json.dumps({'identity_conflict': True, 'native_session_id': 'wrong-native'}),
                         json.dumps({'identity_conflict': False, 'native_session_id': 'shared-native'})):
            with self.subTest(metadata=metadata), indexer.conn:
                indexer.conn.execute('UPDATE sessions SET activity_metadata_json=? WHERE id=?', (metadata, alias))
                with self.assertRaisesRegex(ValueError, 'recording identity mismatch'):
                    mcode_review_snapshot(indexer, alias)
                with self.assertRaisesRegex(ValueError, 'recording identity mismatch'):
                    indexer.build_session_audit(alias)

    def test_forged_alias_with_correct_native_metadata_is_rejected(self):
        self.write('first', 'first request')
        self.write('second', 'second request')
        reader = self.reader()
        reader.refresh()
        alias = next(sid for sid in self.identities(reader).values() if sid != 'shared-native')
        indexer = self.indexer(reader)
        forged = 'shared-native@recording-not-the-selected-path'
        with indexer.conn:
            indexer.conn.execute('UPDATE sessions SET id=? WHERE id=?', (forged, alias))
            indexer.conn.execute('UPDATE messages SET session_id=? WHERE session_id=?', (forged, alias))
        with self.assertRaisesRegex(ValueError, 'recording identity mismatch'):
            mcode_review_snapshot(indexer, forged)
        with self.assertRaisesRegex(ValueError, 'recording identity mismatch'):
            indexer.build_session_audit(forged)

    def test_alias_review_still_rejects_changed_manifest_revision(self):
        self.write('first', 'first request')
        self.write('second', 'second request')
        reader = self.reader()
        reader.refresh()
        alias_path, alias = next((Path(path), sid) for path, sid in self.identities(reader).items()
                                if sid != 'shared-native')
        manifest = alias_path.parent / 'manifest.json'
        header = json.loads(manifest.read_text())
        header['sessionId'] = 'changed-native'
        manifest.write_text(json.dumps(header))
        with self.assertRaisesRegex(ValueError, 'source_changed_since_index'):
            mcode_review_snapshot(self.indexer(reader), alias)

    def test_source_change_during_audit_rolls_back_refresh(self):
        from history_core import mcode_review
        self.write('first', 'original first request')
        self.write('second', 'original second request')
        reader = self.reader()
        reader.refresh()
        identities = self.identities(reader)
        alias_path, alias = next((Path(path), sid) for path, sid in identities.items()
                                if sid != 'shared-native')
        original_text = self.indexer(reader).get_session_message(alias, 0)['text']
        original_revision = reader.health()['index_revision']
        self.write(alias_path.parent.relative_to(self.source), 'attempted update')
        build_audit = mcode_review.build_mcode_audit

        def change_manifest_after_parse(session):
            audit = build_audit(session)
            manifest = alias_path.parent / 'manifest.json'
            header = json.loads(manifest.read_text())
            header['changed_during_audit'] = True
            manifest.write_text(json.dumps(header))
            return audit

        with patch.object(mcode_review, 'build_mcode_audit', side_effect=change_manifest_after_parse):
            with self.assertRaisesRegex(ValueError, 'source_changed_during_refresh'):
                reader.refresh()
        self.assertEqual(self.identities(reader), identities)
        self.assertEqual(reader.health()['index_revision'], original_revision)
        self.assertEqual(self.indexer(reader).get_session_message(alias, 0)['text'], original_text)

    def test_legacy_native_and_alias_audits_keep_full_source_size_support(self):
        from history_core.mcode_review import MAX_MESSAGES_BYTES
        text = 'large public request ' + 'x' * MAX_MESSAGES_BYTES
        first = self.write('first', text)
        self.write('second', text)
        self.assertGreater(first.stat().st_size, MAX_MESSAGES_BYTES)
        reader = self.reader()
        reader.refresh()
        identities = self.identities(reader)
        self.assertEqual(len(identities), 2)
        for identity in identities.values():
            with self.subTest(identity=identity):
                audit = self.indexer(reader).build_session_audit(identity)
                self.assertEqual(audit['session_id'], identity)
                self.assertTrue(audit['first_user_prompt'].startswith('large public request '))
                with self.assertRaisesRegex(ValueError, 'handoff_source_limit_exceeded'):
                    mcode_review_snapshot(self.indexer(reader), identity)

    def test_legacy_audit_rejects_source_change_during_parse(self):
        from history_core import mcode
        self.write('first', 'first request')
        self.write('second', 'second request')
        reader = self.reader()
        reader.refresh()
        alias_path, alias = next((Path(path), sid) for path, sid in self.identities(reader).items()
                                if sid != 'shared-native')
        parse = mcode.parse_mcode_session_file

        def change_manifest_after_parse(path):
            parsed = parse(path)
            manifest = alias_path.parent / 'manifest.json'
            header = json.loads(manifest.read_text())
            header['changed_during_parse'] = True
            manifest.write_text(json.dumps(header))
            return parsed

        with patch.object(mcode, 'parse_mcode_session_file', side_effect=change_manifest_after_parse):
            with self.assertRaisesRegex(ValueError, 'source_changed_during_read'):
                self.indexer(reader).build_session_audit(alias)


if __name__ == '__main__':
    unittest.main()
