import concurrent.futures
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from app import Handler
from history_core.materials import preview_material, export_material
import test_evidence_selection


class MaterialTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_evidence_selection.EvidenceSelectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.indexer, self.source = self.fixture.fixture(texts=('Fixed bug /home/private/project api_key=secret-test-value ``` <script>', 'not selected'))
        self.selections = [self.fixture.selection(self.indexer)]
        self.fields = {'title': '查找故障', 'result': '修复了搜索', 'lesson': '先复现'}
        self.root = self.fixture.root / 'inbox'

    def preview(self, **fields):
        return preview_material(self.fixture.indexers, self.selections, {**self.fields, **fields})

    def test_preview_redaction_provenance_and_no_claim_of_verification(self):
        before = self.source.read_bytes()
        material = self.preview()
        self.assertNotIn('/home/private', material['markdown'])
        self.assertNotIn('secret-test-value', material['markdown'])
        self.assertNotIn('not selected', material['markdown'])
        self.assertIn('same-session', material['markdown'])
        self.assertIn('````text', material['markdown'])
        self.assertEqual(material['historical_verification'], 'unknown')
        self.assertEqual(before, self.source.read_bytes())
        self.assertFalse(self.root.exists())

    def test_repeated_export_and_changed_fields_immutable_revision(self):
        material = self.preview()
        first = export_material(self.root, material, material['revision'])
        self.assertEqual(first['status'], 'created')
        self.assertEqual(export_material(self.root, self.preview(), material['revision'])['status'], 'already_exists')
        changed = self.preview(result='新增结果')
        self.assertEqual(changed['material_id'], material['material_id'])
        self.assertNotEqual(changed['filename'], material['filename'])
        export_material(self.root, changed, changed['revision'])
        self.assertEqual(len(list(self.root.glob('*.md'))), 2)
        self.assertEqual((self.root / material['filename']).read_text(), material['markdown'])

    def test_manual_edits_never_overwritten(self):
        material = self.preview()
        export_material(self.root, material, material['revision'])
        path = self.root / material['filename']
        path.write_text('人工修改')
        with self.assertRaisesRegex(ValueError, 'existing_file_changed'):
            export_material(self.root, material, material['revision'])
        self.assertEqual(path.read_text(), '人工修改')
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_concurrent_export_is_atomic_and_idempotent(self):
        material = self.preview()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: export_material(self.root, material, material['revision']), range(8)))
        self.assertEqual(sum(row['status'] == 'created' for row in results), 1)
        self.assertEqual(list(self.root.iterdir()), [self.root / material['filename']])

    def test_preview_revision_and_configuration_required(self):
        material = self.preview()
        for root, revision, error in [(None, material['revision'], 'not_configured'), (self.root, 'old', 'preview_changed')]:
            with self.assertRaisesRegex(ValueError, error):
                export_material(root, material, revision)
        self.assertFalse(self.root.exists())
        for fields in ({'title': ''}, {'result': 'a' * 2001}, {'title': []}):
            with self.assertRaises(ValueError):
                self.preview(**fields)

    def test_stale_source_and_unbounded_selections_refused(self):
        self.source.write_bytes(self.source.read_bytes() + b'{}\n')
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.preview()
        with self.assertRaises(ValueError):
            preview_material([], [], self.fields)

    def test_symlinks_and_special_files_refused(self):
        material = self.preview()
        actual = self.fixture.root / 'actual'
        actual.mkdir()
        self.root.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'directory_symlink'):
            export_material(self.root, material, material['revision'])
        self.root.unlink()
        self.root.mkdir()
        target = self.root / material['filename']
        target.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, 'existing_file_changed'):
            export_material(self.root, material, material['revision'])
        target.unlink()
        import os
        os.mkfifo(target)
        with self.assertRaisesRegex(ValueError, 'existing_file_changed'):
            export_material(self.root, material, material['revision'])

    def test_cross_session_and_order_deduplication(self):
        second, _ = self.fixture.fixture('claude', texts=('second source',))
        self.selections.append(self.fixture.selection(second))
        first = self.preview()
        self.selections.reverse()
        second = self.preview()
        self.assertEqual(first, second)
        self.selections.append(self.selections[0])
        with self.assertRaisesRegex(ValueError, "selection_duplicate"):
            self.preview()

    def test_handler_preview_export_and_origin_guard(self):
        handler = Handler.__new__(Handler)
        handler.client_address = ('127.0.0.1', 12345)
        handler._material_dir = self.root
        handler.headers = {'Content-Type': 'application/json', 'Host': '127.0.0.1:8895', 'Origin': 'http://127.0.0.1:8895'}
        handler._reuse_sources = lambda: (self.fixture.indexers, [])
        handler.send_json = lambda body, status=200: (body, status)
        data = {'selections': self.selections, 'fields': self.fields}
        preview, status = handler.handle_reuse_post(urlparse('/api/reuse/material-preview'), data)
        self.assertEqual(status, 200)
        data['revision'] = preview['revision']
        result, status = handler.handle_reuse_post(urlparse('/api/reuse/material-export'), data)
        self.assertEqual((status, result['status']), (200, 'created'))
        handler.headers['Origin'] = 'https://attacker.invalid'
        with patch('history_core.materials.preview_material', side_effect=AssertionError('must not read')):
            _, status = handler.handle_reuse_post(urlparse('/api/reuse/material-export'), data)
        self.assertEqual(status, 403)

    def test_handler_requires_loopback_json_and_blocks_cross_site(self):
        handler = Handler.__new__(Handler)
        handler.client_address = ('127.0.0.1', 12345)
        handler.send_json = lambda body, status=200: (body, status)
        for headers in (
            {'Host': 'rebind.invalid', 'Content-Type': 'application/json'},
            {'Host': '127.0.0.1:8895', 'Content-Type': 'text/plain'},
            {'Host': '127.0.0.1:8895', 'Content-Type': 'application/json', 'Sec-Fetch-Site': 'cross-site'},
        ):
            handler.headers = headers
            _, status = handler.handle_reuse_post(urlparse('/api/reuse/material-export'), {})
            self.assertEqual(status, 403)

    def test_material_export_rejects_remote_peer_with_forged_loopback_host(self):
        handler = Handler.__new__(Handler)
        handler.client_address = ('192.0.2.20', 12345)
        handler.headers = {'Host': 'localhost:8895', 'Content-Type': 'application/json'}
        handler.send_json = lambda body, status=200: (body, status)
        with patch('history_core.materials.export_material', side_effect=AssertionError('must not write')):
            result, status = handler.handle_reuse_post(urlparse('/api/reuse/material-export'), {})
        self.assertEqual((status, result['error']), (403, 'material_loopback_required'))
