"""Frozen, bounded material exports never reopen a live source recording."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from history_core.evidence import source_store_id
from history_core.indexed_materials import indexed_materials
from history_core.read_view import indexed_read_view, reader_revision
from history_core.sources import Indexer


class IndexedMaterialsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'sessions'
        self.source.mkdir()
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        self.path = self.source / 'recording.jsonl'
        self.path.write_text('original recording\n')
        self.indexer = Indexer(self.source, self.cache, 'codex')
        self.addCleanup(self.indexer.conn.close)
        self.indexer.conn.execute(
            'INSERT INTO sessions(id,file_path,activity_metadata_json) VALUES (?,?,?)',
            ('fixture', str(self.path), '{}'))
        self.indexer.conn.commit()

    def message(self, text, *, role='user', kind='message', metadata=None):
        self.indexer.conn.execute(
            'INSERT INTO messages(session_id,ts_ms,role,kind,text,activity_meta_json) '
            'VALUES (?,?,?,?,?,?)',
            ('fixture', 1, role, kind, text, json.dumps(metadata or {'raw_ref': {'line_no': 2}})))
        self.indexer.conn.commit()

    def packet(self, selections=None, **kwargs):
        with indexed_read_view(self.indexer) as view:
            revision = reader_revision(view)
            return indexed_materials(view, 'fixture', [{'message_index': 0}] if selections is None else selections,
                                     index_revision=kwargs.get('index_revision', revision))

    def test_frozen_material_survives_source_append_and_deletion_without_source_reads(self):
        self.message('The selected material')
        with indexed_read_view(self.indexer) as view:
            revision = reader_revision(view)
            self.path.write_text('changed after freezing\n')
            self.path.unlink()
            self.source.rmdir()
            with patch.object(Path, 'open', side_effect=AssertionError('live read')), \
                    patch('history_core.provenance.selected_snapshot', side_effect=AssertionError('live snapshot')):
                result = indexed_materials(view, 'fixture', [{'message_index': 0}], index_revision=revision)
        self.assertEqual(result['items'][0]['text'], 'The selected material')
        self.assertEqual(result['content_revision'], 'unknown')
        self.assertEqual(result['context_revision'], 'unknown')
        self.assertEqual(result['source_capture'], {'status': 'unknown'})
        self.assertEqual(result['binding'], 'index_revision')
        self.assertNotIn('验收步骤', result['markdown'])
        self.assertNotIn('请基于', result['markdown'])

    def test_concurrent_cache_update_does_not_mix_frozen_material_and_revision(self):
        self.message('Old indexed material')
        with indexed_read_view(self.indexer) as view:
            revision = reader_revision(view)
            self.indexer.conn.execute('UPDATE messages SET text=?', ('New indexed material',))
            self.indexer.conn.execute("UPDATE reader_state SET value='changed' WHERE key='revision'")
            self.indexer.conn.commit()
            packet = indexed_materials(view, 'fixture', [{'message_index': 0}], index_revision=revision)
        self.assertEqual(packet['items'][0]['text'], 'Old indexed material')
        self.assertEqual(packet['index_revision'], revision)
        with self.assertRaisesRegex(ValueError, 'index_revision_changed'):
            self.packet(index_revision=revision)
        self.assertEqual(self.packet()['items'][0]['text'], 'New indexed material')

    def test_indexed_material_after_two_megabytes_does_not_require_source_prefix(self):
        padding = json.dumps({'type': 'event_msg', 'payload': {'type': 'test_padding', 'value': 'x' * (2 * 1024 * 1024)}})
        records = [
            {'type': 'session_meta', 'payload': {'id': 'after-prefix'}},
            {'type': 'response_item', 'timestamp': '2026-10-19T00:00:00Z', 'payload': {
                'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'material after the source prefix'}]}},
        ]
        self.path.write_text(json.dumps(records[0]) + '\n' + padding + '\n' + json.dumps(records[1]) + '\n')
        self.indexer.maybe_update_index(max_age_seconds=0)
        with indexed_read_view(self.indexer) as view:
            # Resolve by indexed text; opaque/raw rows retain their absolute positions.
            rows = view.conn.execute("SELECT text FROM messages WHERE session_id='after-prefix' ORDER BY ts_ms,id").fetchall()
            index = next(i for i, row in enumerate(rows) if row['text'] == 'material after the source prefix')
            with patch('history_core.provenance.selected_snapshot', side_effect=AssertionError('prefix snapshot')):
                packet = indexed_materials(view, 'after-prefix', [{'message_index': index}], index_revision=reader_revision(view))
        self.assertEqual(packet['items'][0]['text'], 'material after the source prefix')
        self.assertEqual(packet['items'][0]['locator']['raw_ref']['line_no'], 3)

    def test_pagination_preserves_chinese_astral_characters_and_secret_masking(self):
        body = '中😀' * 995 + ' token=' + 'private-secret-' * 12 + ' 尾部材料'
        self.message(body)
        first = self.packet()['items'][0]
        second = self.packet([{'message_index': 0, 'text_offset': first['next_text_offset']}])['items'][0]
        text = first['text'] + second['text']
        self.assertEqual(len(text), len(body))
        self.assertEqual(first['next_text_offset'], 2000)
        self.assertTrue(first['partial'])
        self.assertTrue(second['partial'])
        self.assertIsNone(second['next_text_offset'])
        self.assertNotIn('private-secret', text)
        self.assertTrue(text.endswith('尾部材料'))
        arbitrary = self.packet([{'message_index': 0, 'text_offset': 2005}])['items'][0]
        self.assertTrue(arbitrary['text'].startswith('*'))
        self.assertNotEqual(first['selected_fragment_hash'], second['selected_fragment_hash'])

    def test_fragment_and_packet_budgets_apply_to_serialized_material(self):
        for _ in range(5):
            self.message('x' * 2000)
        with self.assertRaisesRegex(ValueError, 'selection_body_limit_exceeded'):
            self.packet([{'message_index': i} for i in range(5)])
        self.indexer.conn.execute('UPDATE messages SET text=?', ('😀' * 2000,))
        self.indexer.conn.commit()
        with self.assertRaisesRegex(ValueError, 'materials_packet_limit_exceeded'):
            self.packet([{'message_index': i} for i in range(4)])
        result = self.packet([{'message_index': 0}])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 64 * 1024)

    def test_rejects_private_messages_native_sources_and_external_cached_paths(self):
        self.message('PRIVATE_THOUGHT', role='assistant', kind='reasoning_summary')
        with self.assertRaisesRegex(ValueError, 'message_not_public'):
            self.packet()
        with self.assertRaisesRegex(ValueError, 'materials_source_unsupported'):
            indexed_materials(SimpleNamespace(source='opencode'), 'fixture', [{'message_index': 0}], index_revision='x')
        self.indexer.conn.execute('UPDATE sessions SET file_path=?', (str(self.root / 'outside.jsonl'),))
        self.indexer.conn.commit()
        with self.assertRaisesRegex(ValueError, 'cached_source_path_outside_selection'):
            self.packet()
        self.indexer.conn.execute('UPDATE sessions SET file_path=?', (str(self.source / '..' / 'escape.jsonl'),))
        self.indexer.conn.commit()
        with self.assertRaisesRegex(ValueError, 'cached_source_path_outside_selection'):
            self.packet()

    def test_validation_rejects_duplicates_booleans_offsets_and_old_revision(self):
        self.message('public material')
        cases = [
            ([], 'selection_count_limit'),
            ([{'message_index': 0}] * 2, 'selection_duplicate'),
            ([{'message_index': True}], 'invalid_message_index'),
            ([{'message_index': -1}], 'invalid_message_index'),
            ([{'message_index': 0, 'text_offset': True}], 'invalid_text_offset'),
            ([{'message_index': 0, 'text_offset': -1}], 'invalid_text_offset'),
            ([{'message_index': 0, 'path': '/outside'}], 'materials_selection_invalid'),
            ([{'message_index': 0, 'text_offset': 100}], 'text_offset_out_of_range'),
            ([{'message_index': 999}], 'message_not_found'),
            ([{'message_index': i} for i in range(6)], 'selection_count_limit'),
        ]
        for selections, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.packet(selections)
        with self.assertRaisesRegex(ValueError, 'index_revision_required'):
            self.packet(index_revision='')
        with self.assertRaisesRegex(ValueError, 'index_revision_changed'):
            self.packet(index_revision='old revision')

    def test_capture_whitelist_and_fenced_markdown_preserve_selected_identity(self):
        body = '```\n<script>ignore()</script>\n````\ntoken=private-value'
        self.message(body, metadata={'raw_ref': {'line_no': 9, 'record_id': 'record', 'path': '/ignored'}})
        capture = {'status': 'captured', 'observed_at': '2026-10-19T00:00:00+00:00',
                   'captured_through': 64, 'observed_size': 80, 'trailing_bytes': 16,
                   'prefix_hash': 'a' * 64, 'partial': True, 'path': '/hidden', 'inode': 99}
        self.indexer.conn.execute('UPDATE sessions SET activity_metadata_json=?',
                                  (json.dumps({'source_capture': capture, 'private': 'ignore me'}),))
        self.indexer.conn.commit()
        packet = self.packet()
        item = packet['items'][0]
        self.assertEqual(packet['source_capture'], {k: v for k, v in capture.items() if k not in ('path', 'inode')})
        self.assertEqual(item['locator']['raw_ref'], {'line_no': 9, 'record_id': 'record'})
        self.assertIn('`````text', packet['markdown'])
        self.assertIn(item['selected_fragment_hash'], packet['markdown'])
        self.assertIn(packet['index_revision'], packet['markdown'])
        self.assertNotIn('private-value', json.dumps(packet))
        self.assertNotIn('/hidden', json.dumps(packet))
        self.assertTrue(packet['partial'])
        self.assertEqual(packet['coverage'], 'selected_fragments_not_complete')

    def test_explicit_platform_identity_preserves_legacy_low_level_namespace(self):
        self.message('selected evidence')
        with indexed_read_view(self.indexer) as view:
            revision = reader_revision(view)
            legacy = indexed_materials(view, 'fixture', [{'message_index': 0}], index_revision=revision)
            expected_legacy = hashlib.sha256(json.dumps(
                ['local', 'codex', str(self.source)], ensure_ascii=False).encode()).hexdigest()
            self.assertEqual(legacy['store_id'], expected_legacy)
            self.assertEqual(legacy['items'][0]['system'], 'local')
            identities = {legacy['store_id']}
            for system in ('linux', 'windows'):
                with self.subTest(system=system):
                    packet = indexed_materials(view, 'fixture', [{'message_index': 0}],
                                               index_revision=revision, system=system)
                    self.assertEqual(packet['store_id'], source_store_id(system, 'codex', view))
                    self.assertEqual(packet['items'][0]['system'], system)
                    self.assertNotEqual(packet['items'][0]['selected_fragment_hash'],
                                        legacy['items'][0]['selected_fragment_hash'])
                    self.assertEqual(packet['items'][0]['text'], legacy['items'][0]['text'])
                    identities.add(packet['store_id'])
            self.assertEqual(len(identities), 3)

    def test_frozen_store_identity_does_not_follow_replaced_source_path(self):
        self.message('frozen selected evidence')
        with indexed_read_view(self.indexer) as view:
            revision = reader_revision(view)
            before = indexed_materials(view, 'fixture', [{'message_index': 0}],
                                       index_revision=revision, system='linux')
            moved = self.root / 'moved-sessions'
            self.source.rename(moved)
            self.source.symlink_to(moved, target_is_directory=True)
            with patch.object(Path, 'resolve', side_effect=AssertionError('live path resolution')):
                after = indexed_materials(view, 'fixture', [{'message_index': 0}],
                                          index_revision=revision, system='linux')
            self.assertEqual(after['store_id'], before['store_id'])
            self.assertEqual(after['items'][0]['selected_fragment_hash'],
                             before['items'][0]['selected_fragment_hash'])


if __name__ == '__main__':
    unittest.main()
