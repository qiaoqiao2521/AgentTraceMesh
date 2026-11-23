import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from audit.handoff import build_handoff_bundle, build_handoff_payload, render_handoff
from history_core import service
from history_core.evidence import redact_text


REPO = Path(__file__).resolve().parents[1]


class HandoffRedactionTests(unittest.TestCase):
    def setUp(self):
        self.password = 'synthetic-password-value'
        self.token = 'sk-proj-synthetic_token_1234567890'
        self.audit = {
            'source': 'codex', 'session_id': 'fixture',
            'first_user_prompt': 'Continue with password=' + self.password,
            'last_user_prompt': 'Use token=' + self.token,
            'important_user_prompts': ['password="' + self.password + '"'],
            'commands': [{'command': 'pytest --password=' + self.password,
                          'status': 'pass', 'exit_code': 0, 'evidence_id': 'fixture:tool:2'}],
            'files_touched': {'local': [{'path': 'password=' + self.password}]},
            'evidence': [{'id': 'fixture:user:1', 'type': 'user_prompt',
                          'message_index': 1, 'summary': 'token=' + self.token,
                          'raw_ref': {'line_no': 2, 'json_pointer': '/payload/content/0'}}],
        }
        self.provenance = {
            'status': 'captured', 'content_revision': 'sha256:' + 'a' * 64,
            'context_revision': 'sha256:' + 'b' * 64,
            'locator': {'path': '/selected/session.jsonl'}, 'truncated': False,
        }

    def assert_masked(self, value):
        text = json.dumps(value, ensure_ascii=False)
        self.assertNotIn(self.password, text)
        self.assertNotIn(self.token, text)
        self.assertIn('[REDACTED]', text)

    def test_payload_and_both_renderings_mask_all_content_without_mutating_inputs(self):
        before = copy.deepcopy(self.audit)
        ai_audit = {'user_intent': 'password=' + self.password,
                    'gaps': ['token=' + self.token],
                    'next_action': 'password=' + self.password}
        bundle = build_handoff_bundle(self.audit, metadata={'cwd': '/tmp/password=' + self.password},
                                      ai_audit=ai_audit, provenance=self.provenance)
        self.assert_masked(bundle['payload'])
        self.assert_masked(bundle['compact'])
        self.assert_masked(bundle['standard'])
        payload = bundle['payload']
        self.assertEqual(payload['provenance']['content_revision'], self.provenance['content_revision'])
        self.assertEqual(payload['provenance']['locator'], self.provenance['locator'])
        self.assertEqual(payload['evidence'][0]['raw_ref'], self.audit['evidence'][0]['raw_ref'])
        self.assertEqual(payload['evidence'][0]['id'], 'fixture:user:1')
        self.assertEqual(payload['verified'][0]['evidence_id'], 'fixture:tool:2')
        self.assertEqual(payload['authorization'], 'context_only')
        self.assertEqual(self.audit, before)

    def test_complete_quoted_and_private_key_fields_are_redacted_before_shortening(self):
        private_key = '-----BEGIN PRIVATE KEY-----\n' + 'PRIVATE_BODY_' * 80 + '\n-----END PRIVATE KEY-----'
        original = 'x' * 570 + ' password="' + self.password * 40 + '" after'
        self.audit['first_user_prompt'] = original
        self.audit['important_user_prompts'] = [private_key]
        payload = build_handoff_payload(self.audit, provenance=self.provenance)
        self.assertEqual(payload['goal'], redact_text(original))
        self.assertLessEqual(len(payload['goal']), 600)
        self.assertIn('[REDACTED]', payload['goal'])
        self.assertIn('[REDACTED PRIVATE KEY]', payload['constraints'][0])
        self.assertNotIn('PRIVATE_BODY', json.dumps(payload))

    def test_raw_payload_renderer_masks_short_tokens_and_nested_git_fields(self):
        payload = {'goal': 'password=x', 'cwd': 'token=' + self.token,
                   'git': {'available': True, 'commit': 'abc', 'workspace': 'password=' + self.password},
                   'specmesh': {'status': 'candidate_context', 'files': [
                       {'path': 'password=' + self.password,
                        'sections': {'token=' + self.token: 'password=' + self.password}}]}}
        before = copy.deepcopy(payload)
        for detail in ('compact', 'standard'):
            self.assert_masked(render_handoff(payload, detail))
        self.assertEqual(payload, before)

    def test_nested_git_payload_is_sanitized(self):
        git = {'available': True, 'commit': 'abc', 'workspace': 'clean',
               'nested': [{'note': 'password=' + self.password, 'detail': {'token': self.token}}]}
        with patch('audit.handoff._git_state', return_value=git):
            bundle = build_handoff_bundle(self.audit)
        self.assert_masked(bundle)
        self.assertNotIn(self.password, json.dumps(bundle['payload']['git']))
        self.assertNotIn(self.token, json.dumps(bundle['payload']['git']))

    def test_legacy_web_audit_handoff_bundle_is_the_sanitized_source_for_copy_exports(self):
        import app
        indexer = SimpleNamespace(source='hermes',
                                  get_session_metadata=Mock(return_value={'id': 'fixture', 'cwd': '/project'}),
                                  build_session_audit=Mock(return_value=self.audit))
        handler = SimpleNamespace(send_json=Mock(side_effect=lambda result: result),
                                  _audit_llm_configured=lambda: False)
        response = app.Handler.handle_session_audit(handler, 'fixture', None, SimpleNamespace(indexer=indexer))
        bundle = response['handoff']
        self.assert_masked(bundle)
        # Reading the audit in the local UI remains distinct from exporting it.
        self.assertEqual(response['audit'], self.audit)
        self.assertEqual(bundle['authorization'], 'context_only')

    def test_explicit_plan_fields_are_masked_before_the_600_character_bound(self):
        plan_text = 'x' * 570 + ' password="' + self.password * 40 + '"'
        plans = [{'rel_path': 'plans/password=' + self.password,
                  'mtime_ms': 123, 'sections': {'token=' + self.token: plan_text}}]
        indexer = SimpleNamespace(source='hermes',
                                  get_session_metadata=Mock(return_value={'id': 'fixture', 'cwd': '/project'}),
                                  build_session_audit=Mock(return_value=self.audit))
        with patch('history_core.sources.scan_plan_files', return_value=plans):
            bundle = service.handoff(indexer, 'fixture', include_plans=True)
        self.assert_masked(bundle)
        exported = bundle['payload']['specmesh']['files'][0]
        self.assertEqual(next(iter(exported['sections'].values())), redact_text(plan_text)[:600])
        self.assertIn('[REDACTED]', next(iter(exported['sections'])))
        self.assertEqual(bundle['payload']['specmesh']['authority'], 'unverified_project_files')

    @unittest.skipUnless(shutil.which('node'), 'Node is required for isolated frontend export checks')
    def test_frontend_copy_markdown_and_html_consume_only_the_redacted_bundle(self):
        bundle = build_handoff_bundle(self.audit, provenance=self.provenance)
        script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('static/app.js', 'utf8');
const bundle = JSON.parse(fs.readFileSync(0, 'utf8'));
const names = ['currentHandoffText', 'copyCurrentHandoff', 'copyHandoffRich',
               'buildHandoffHtmlDoc', 'handoffFilename', 'downloadHandoff'];
const functions = names.map(name => {
  const pattern = new RegExp('(?:async )?function ' + name + '\\(');
  const start = source.search(pattern);
  if (start < 0) throw new Error('missing export function: ' + name);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}).join('\n');
const results = [];
const context = {
  currentHandoff: bundle, currentSession: {id: 'fixture'},
  handoffDetailEl: {value: 'standard'}, handoffThemeEl: {value: 'plain'},
  copyHandoffBtn: null, copyHandoffRichBtn: null, navigator: {},
  copyText: async text => results.push(text),
  downloadText: (_name, text) => results.push(text),
  setHandoffTheme: theme => theme,
  renderMarkdown: text => '<pre>' + text.replaceAll('&', '&amp;').replaceAll('<', '&lt;') + '</pre>',
};
vm.runInNewContext(functions + '\n(async () => {' +
  'for (const detail of ["compact", "standard"]) {' +
  'handoffDetailEl.value = detail; await copyCurrentHandoff(); await copyHandoffRich();' +
  'downloadHandoff("md"); downloadHandoff("html"); }} )()', context)
  .then(() => process.stdout.write(JSON.stringify(results)))
  .catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(['node', '-e', script], input=json.dumps(bundle),
                                cwd=REPO, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = json.loads(result.stdout)
        self.assertEqual(len(outputs), 8)
        for output in outputs:
            self.assert_masked(output)
        self.assertEqual(outputs[0], bundle['compact'])
        self.assertEqual(outputs[4], bundle['standard'])
        self.assertIn('<!doctype html>', outputs[3])

    def test_cli_exports_json_compact_and_standard_without_plaintext_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'sessions'
            source.mkdir()
            path = source / 'fixture.jsonl'
            rows = [
                {'type': 'session_meta', 'payload': {'id': 'fixture', 'cwd': str(root)}},
                {'timestamp': '2026-10-31T00:00:00Z', 'type': 'response_item',
                 'payload': {'type': 'message', 'role': 'user', 'content': [
                     {'type': 'input_text', 'text': 'Continue password=' + self.password + ' token=' + self.token}]}}
            ]
            path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
            before = path.read_bytes()
            prefix = [sys.executable, '-B', '-m', 'history_core', '--source', 'codex',
                      '--source-path', str(source), '--data-dir', str(root / 'cache')]
            refresh = subprocess.run(prefix + ['refresh'], cwd=REPO, capture_output=True, text=True)
            self.assertEqual(refresh.returncode, 0, refresh.stderr)
            result = subprocess.run(prefix + ['handoff', 'fixture'], cwd=REPO, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = json.loads(result.stdout)
            self.assert_masked(bundle)
            for field in ('compact', 'standard'):
                self.assert_masked(bundle[field])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(bundle['payload']['authorization'], 'context_only')


if __name__ == '__main__':
    unittest.main()
