"""Context recovery through the real CLI, shared recall and existing handoff."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from history_core.context_cli import read_packet

REPO = Path(__file__).resolve().parents[1]


class ContextCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.cache = self.root / 'cache'
        self.cases = self.root / 'cases'
        self.request_path = self.root / 'request.json'
        self.previous_path = self.root / 'previous.json'
        self.request = {'schema_version': 'history.context-request.v1',
                        'task': 'Inspect the lighting plugin',
                        'next_action': 'Check the current render against the requested appearance',
                        'project': '/synthetic/lighting', 'state_revision': 'fixture-state-one',
                        'needs': [{'id': 'receipt', 'kind': 'history', 'status': 'missing',
                                   'question': 'What did the lighting plugin actually return?',
                                   'reason': 'The configuration alone cannot prove the requested effect',
                                   'required_evidence': 'Original tool call, result and later user correction',
                                   'query': 'lighting plugin'}]}
        self.source_args = ['--source', 'codex', '--source-path', str(self.source), '--data-dir', str(self.cache)]

    def invoke(self, arguments, codes=(0,)):
        result = subprocess.run([sys.executable, '-B', '-m', 'history_core', *arguments],
                                cwd=REPO, capture_output=True, text=True, timeout=30)
        self.assertIn(result.returncode, codes, result.stderr or result.stdout)
        return json.loads(result.stdout or result.stderr)

    def context(self, *extra, codes=(0,)):
        self.request_path.write_text(json.dumps(self.request), encoding='utf-8')
        return self.invoke(['context', '--request', str(self.request_path),
                            '--directory', str(self.cases), *self.source_args, *extra], codes)

    def fixture(self):
        self.source.mkdir()
        events = [
            {'type': 'session_meta', 'payload': {'id': 'lighting', 'cwd': '/synthetic/lighting'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': 'Make the lighting plugin change the render.'}]}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'shell_command',
             'call_id': 'light', 'arguments': json.dumps({'cmd': 'check-lighting'})}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'light',
             'output': 'Exit code: 1\nlighting plugin failed. api_key=synthetic-secret'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': 'Instead, verify the actual rendered output.'}]}},
        ]
        recording = self.source / 'lighting.jsonl'
        recording.write_text('\n'.join(json.dumps(dict(event, timestamp=f'2026-11-20T00:00:{i:02d}Z'))
                                       for i, event in enumerate(events)) + '\n', encoding='utf-8')
        self.invoke([*self.source_args, 'refresh'])
        return recording

    def test_no_gap_opens_no_sources_or_catalog_even_with_retrieval_enabled(self):
        self.request['needs'] = []
        result = self.context('--retrieve')
        self.assertEqual(result['status'], 'no_gap')
        self.assertEqual(result['retrieval_calls'], 0)
        self.assertEqual(result['attempts'], [])
        self.assertFalse(self.source.exists())
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.cases.exists())

    def test_plan_default_never_reads_a_selected_nonexistent_source(self):
        result = self.context()
        self.assertEqual(result['status'], 'needs_retrieval')
        self.assertEqual(result['retrieval_calls'], 0)
        self.assertEqual(result['gaps'][0]['query'], 'lighting plugin')
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.cases.exists())

    def test_cases_and_handoff_share_current_recovery_without_promoting_history(self):
        recording = self.fixture()
        before = recording.read_bytes()
        self.invoke([*self.source_args, 'compress', 'lighting', '--save', '--case-dir', str(self.cases)], (0, 1))
        result = self.context('--retrieve')
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['retrieval_calls'], 1)
        candidate = result['gaps'][0]['candidates'][0]
        self.assertEqual(candidate['origin'], 'case')
        locator = candidate['evidence'][0]
        for key in ('source', 'store_id', 'session_id', 'message_index', 'index_revision'):
            self.assertIn(key, locator)
        self.previous_path.write_text(json.dumps(result), encoding='utf-8')
        repeated = self.context('--retrieve', '--previous', str(self.previous_path))
        self.assertEqual(repeated['retrieval_calls'], 0)
        self.assertTrue(repeated['gaps'][0]['reused'])
        plain = self.invoke([*self.source_args, 'handoff', 'lighting'])
        linked = self.invoke([*self.source_args, 'handoff', 'lighting',
                             '--context-request', str(self.request_path),
                             '--context-previous', str(self.previous_path), '--case-dir', str(self.cases)])
        self.assertEqual(linked['context_recovery']['retrieval_calls'], 0)
        self.assertEqual(linked['context_recovery']['status'], 'needs_review')
        self.assertEqual(plain['payload']['goal'], linked['payload']['goal'])
        self.assertEqual(plain['payload']['verified'], linked['payload']['verified'])
        self.assertIn('current context recovery: needs_review', linked['standard'])
        self.assertIn('query: lighting plugin', linked['standard'])
        self.assertIn(self.request['needs'][0]['required_evidence'], linked['standard'])
        self.assertIn(candidate['id'], linked['standard'])
        self.assertIn(locator['store_id'], linked['standard'])
        self.assertIn(locator['index_revision'], linked['compact'])
        self.assertEqual(linked['authorization'], 'context_only')
        self.assertNotIn('synthetic-secret', json.dumps(linked))
        self.assertEqual(before, recording.read_bytes())

    def test_history_fallback_keeps_locators_that_can_be_expanded(self):
        recording = self.fixture()
        before = recording.read_bytes()
        result = self.context('--retrieve')
        self.assertEqual(result['status'], 'needs_review')
        candidate = result['gaps'][0]['candidates'][0]
        self.assertEqual(candidate['origin'], 'history')
        self.assertTrue(candidate['evidence'])
        locator = candidate['evidence'][0]
        for key in ('system', 'source', 'store_id', 'session_id', 'message_index', 'index_revision'):
            self.assertIn(key, locator)
        expanded = self.invoke([*self.source_args, 'message', locator['session_id'],
                                str(locator['message_index']), '--index-revision', locator['index_revision'],
                                '--text-offset', str(locator.get('text_offset', 0))])
        self.assertEqual(expanded['session_id'], locator['session_id'])
        materials = self.invoke([*self.source_args, 'materials', locator['session_id'],
                                 '--index-revision', locator['index_revision'],
                                 '--message', f"{locator['message_index']}:{locator.get('text_offset', 0)}"])
        self.assertEqual(locator['system'], materials['items'][0]['system'])
        for key in ('source', 'store_id', 'session_id', 'index_revision'):
            self.assertEqual(locator[key], materials[key], key)
        self.assertEqual(before, recording.read_bytes())
        self.assertFalse(self.cases.exists())

    def test_invalid_available_evidence_is_rejected_and_inputs_are_bounded(self):
        self.request['needs'][0]['status'] = 'available'
        result = self.context('--retrieve', codes=(2,))
        self.assertIn('requires_evidence', result['detail'])
        self.assertFalse(self.cache.exists())
        self.request_path.write_bytes(b'x' * (128 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, 'too_large'):
            read_packet(self.request_path, limit=128 * 1024)

    def test_handoff_requires_current_request_for_context_options(self):
        result = self.invoke([*self.source_args, 'handoff', 'lighting', '--retrieve-context'], (2,))
        self.assertIn('handoff_context_request_required', result['detail'])


if __name__ == '__main__':
    unittest.main()
