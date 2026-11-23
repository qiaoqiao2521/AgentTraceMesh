"""Problem-case CLI integration using explicit temporary directories only."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / 'history_core/case_examples/triple-review-attribution.json'


class ProblemCasesCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / 'cases'
        self.environment = {**os.environ, 'XDG_DATA_HOME': str(self.root / 'user-data')}

    def call(self, *arguments, expected=0):
        result = subprocess.run(
            [sys.executable, '-B', '-m', 'history_core', 'cases',
             '--directory', str(self.directory), *arguments],
            cwd=REPO, env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def load_example(self):
        return json.loads(EXAMPLE.read_text(encoding='utf-8'))

    def test_root_help_exposes_the_source_independent_case_entry(self):
        result = subprocess.run([sys.executable, '-B', '-m', 'history_core', '--help'],
                                cwd=REPO, env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cases --help', result.stdout)
        self.assertIn('do not require a source', result.stdout)

    def save_input(self, case):
        incoming = self.root / 'input.json'
        incoming.write_text(json.dumps(case, ensure_ascii=False), encoding='utf-8')
        return json.loads(self.call('save', '--file', str(incoming)).stdout)

    def test_querying_early_pagination_proposal_reaches_corrections_and_reuse(self):
        results = json.loads(self.call('list', '--query', '分页').stdout)
        item = next(item for item in results['items'] if item['id'] == 'triple-review-attribution')
        self.assertIn('派遣', item['now']['judgment'])
        self.assertTrue(item['next_action'])
        shown = json.loads(self.call('show', item['id']).stdout)
        evolution = shown['case']['evolution']
        self.assertTrue(any(event['kind'] == 'correction' and 'paging-proposal' in event.get('replaces', [])
                            for event in evolution))
        text = json.dumps(shown['case'], ensure_ascii=False)
        for term in ('隐藏', '可读时间', '未重新执行', '具体提出者未知'):
            self.assertIn(term, text)
        self.assertTrue(shown['case']['reuse']['conditions'])
        self.assertTrue(shown['case']['reuse']['unknowns'])
        self.assertEqual(shown['interpretation'], 'authored_annotation')
        self.assertEqual(shown['source_verification'], 'references_not_revalidated')
        self.assertEqual(shown['current_verification'], 'not_performed')
        self.assertFalse(self.directory.exists(), 'read-only example discovery must not create the local store')

    def test_markdown_is_exactly_the_shared_getter_export(self):
        from history_core.problem_cases import get_case
        shown = json.loads(self.call('show', 'triple-review-attribution').stdout)
        markdown = self.call('show', 'triple-review-attribution', '--format', 'markdown').stdout
        expected = get_case(self.directory, 'triple-review-attribution')['markdown']
        self.assertEqual(markdown, expected)
        self.assertEqual(markdown, shown['markdown'])
        self.assertIn('1a4ee95d55fbc4cba3c6e9af4b6d1e47770fbe02', markdown)

    def test_saved_case_is_editable_and_same_id_overrides_the_example(self):
        case = self.load_example()
        case['now']['judgment'] = '本地修订：先查派遣材料。'
        saved = self.save_input(case)
        self.assertEqual(saved['origin'], 'local')
        self.assertEqual(saved['case']['now']['judgment'], case['now']['judgment'])
        case_file = self.directory / 'triple-review-attribution.json'
        edited = json.loads(case_file.read_text(encoding='utf-8'))
        edited['now']['judgment'] = '人工二改立刻生效，不需要更新内容哈希。'
        case_file.write_text(json.dumps(edited, ensure_ascii=False), encoding='utf-8')
        shown = json.loads(self.call('show', case['id']).stdout)
        self.assertEqual(shown['origin'], 'local')
        self.assertEqual(shown['case']['now']['judgment'], edited['now']['judgment'])
        self.assertIn(edited['now']['judgment'], shown['markdown'])

    def test_case_commands_never_initialize_history_reader(self):
        from history_core.__main__ import main
        with patch('history_core.__main__.HistoryReader', side_effect=AssertionError('history must stay unopened')):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = main(['cases', '--directory', str(self.directory), 'list', '--query', '分页'])
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(output.getvalue())['items'])

    def test_list_pagination_and_project_filter_do_not_hide_partial_results(self):
        for identifier in ('synthetic-case-a', 'synthetic-case-b'):
            case = {'id': identifier, 'title': 'isolated pagination test',
                    'project': 'synthetic-project', 'now': {'judgment': identifier}}
            self.save_input(case)
        first = json.loads(self.call('list', '--project', 'synthetic-project', '--limit', '1').stdout)
        self.assertEqual(first['total'], 2)
        self.assertEqual(len(first['items']), 1)
        self.assertEqual(first['next_offset'], 1)
        second = json.loads(self.call('list', '--project', 'synthetic-project', '--limit', '1', '--offset', '1').stdout)
        self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])
        self.assertIsNone(second['next_offset'])
        (self.directory / 'broken.json').write_text('{broken', encoding='utf-8')
        partial = json.loads(self.call('list', '--project', 'synthetic-project').stdout)
        self.assertTrue(partial['partial'])
        self.assertTrue(partial['errors'])
        self.assertEqual(len(partial['items']), 2)

    def test_save_masks_synthetic_secrets_without_reading_referenced_sources(self):
        case = {'id': 'redacted-fixture', 'title': 'Synthetic redaction',
                'now': {'judgment': 'password=synthetic-password-value'},
                'evidence': [{'id': 'unavailable-artifact', 'kind': 'artifact',
                              'repository': 'example/synthetic', 'path': 'missing.md',
                              'commit': 'a' * 40, 'line_start': 1, 'line_end': 3}]}
        saved = self.save_input(case)
        self.assertNotIn('synthetic-password-value', json.dumps(saved))
        shown = json.loads(self.call('show', case['id']).stdout)
        self.assertNotIn('synthetic-password-value', json.dumps(shown))
        self.assertEqual(shown['case']['evidence'], case['evidence'])
        self.assertEqual(shown['source_verification'], 'references_not_revalidated')

    def test_invalid_input_and_missing_case_fail_with_structured_errors(self):
        incoming = self.root / 'invalid.json'
        incoming.write_text('{invalid', encoding='utf-8')
        failed = self.call('save', '--file', str(incoming), expected=2)
        self.assertTrue(json.loads(failed.stderr)['error'])
        missing = self.call('show', 'missing-fixture', expected=2)
        self.assertTrue(json.loads(missing.stderr)['error'])
        self.assertFalse(self.directory.exists())

    def test_deep_save_input_fails_with_a_structured_error(self):
        incoming = self.root / 'deep.json'
        incoming.write_text(
            '{"id":"deep-fixture","title":"Deep optional field","custom":'
            + '[' * 1200 + '0' + ']' * 1200 + '}', encoding='utf-8')
        failed = self.call('save', '--file', str(incoming), expected=2)
        self.assertEqual(failed.stdout, '')
        self.assertEqual(json.loads(failed.stderr)['error'], 'RecursionError')
        self.assertFalse(self.directory.exists())

    def test_corrupt_local_override_is_partial_json_and_a_failed_markdown_export(self):
        self.directory.mkdir()
        (self.directory / 'triple-review-attribution.json').write_text('{broken', encoding='utf-8')
        shown = json.loads(self.call('show', 'triple-review-attribution').stdout)
        self.assertIsNone(shown['case'])
        self.assertTrue(shown['partial'])
        self.assertTrue(shown['errors'])
        markdown = self.call('show', 'triple-review-attribution', '--format', 'markdown', expected=2)
        self.assertEqual(markdown.stdout, '')
        receipt = json.loads(markdown.stderr)
        self.assertEqual(receipt['error'], 'case_unavailable')
        self.assertTrue(receipt['partial'])
        self.assertTrue(receipt['errors'])


if __name__ == '__main__':
    unittest.main()
