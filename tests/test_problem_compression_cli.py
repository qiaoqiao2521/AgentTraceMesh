"""End-to-end local problem extraction, storage and question-first retrieval."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]


class ProblemCompressionCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.cache = self.root / 'cache'
        self.cases = self.root / 'cases'
        self.recording = self.source / 'lighting.jsonl'
        self.events = [
            {'type': 'session_meta', 'payload': {'id': 'lighting', 'cwd': '/synthetic/lighting'}},
            self.message('user', '灯光插件没有生效，请找出实际调用和渲染结果。'),
            {'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': [
                {'type': 'summary_text', 'text': 'PRIVATE_REASONING_DO_NOT_EXPORT'}]}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'shell_command',
                'call_id': 'render-check', 'arguments': json.dumps({'cmd': 'renderer --preset studio'})}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output',
                'call_id': 'render-check', 'output': 'Exit code: 0\nPreset configured. api_key=synthetic-hidden-key'}},
            self.message('assistant', '已经设置完成。'),
            self.message('user', '不对，我要的是画面生效，不能只看配置；请核对实际渲染。'),
            self.message('assistant', '还没有渲染结果，效果尚未验证。'),
            self.message('user', '先暂停，等我提供输出图。'),
        ]
        self.write_recording()
        self.prefix = ['--source', 'codex', '--source-path', str(self.source), '--data-dir', str(self.cache)]

    @staticmethod
    def message(role, text):
        return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]}}

    def write_recording(self):
        events = [dict(event, timestamp=f'2026-11-20T00:00:{number:02d}Z')
                  for number, event in enumerate(self.events)]
        self.recording.write_text('\n'.join(json.dumps(event, ensure_ascii=False) for event in events) + '\n', encoding='utf-8')

    def invoke(self, arguments, codes=(0,)):
        result = subprocess.run([sys.executable, '-B', '-m', 'history_core', *arguments],
                                cwd=REPO, capture_output=True, text=True, timeout=30)
        self.assertIn(result.returncode, codes, result.stderr or result.stdout)
        return json.loads(result.stdout if result.stdout else result.stderr)

    def refresh(self):
        return self.invoke([*self.prefix, 'refresh'])

    def test_extract_save_recall_without_raw_mutation_or_manual_json(self):
        before = self.recording.read_bytes()
        self.refresh()
        packet = self.invoke([*self.prefix, 'compress', 'lighting', '--save', '--case-dir', str(self.cases)], (0, 1))
        self.assertEqual(packet['schema_version'], 'history.problem-compression.v1')
        self.assertEqual(packet['authorization'], 'context_only')
        self.assertTrue(packet['cases'])
        self.assertTrue(list(self.cases.glob('*.json')))
        exported = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn('PRIVATE_REASONING_DO_NOT_EXPORT', exported)
        self.assertNotIn('synthetic-hidden-key', exported)
        self.assertIn('不能只看配置', exported)
        self.assertIn('先暂停', exported)
        for case in packet['cases']:
            self.assertNotEqual(case['now']['task_status'], 'completed')
        saved_before = {path.name: path.read_bytes() for path in self.cases.glob('*.json')}
        self.invoke([*self.prefix, 'compress', 'lighting', '--save', '--case-dir', str(self.cases)], (0, 1))
        self.assertEqual(saved_before, {path.name: path.read_bytes() for path in self.cases.glob('*.json')})
        recalled = self.invoke(['recall', '--query', '灯光插件', '--directory', str(self.cases)], (0, 1))
        self.assertTrue(recalled['items'])
        self.assertEqual(recalled['fallback_status'], 'not_needed')
        self.assertEqual(self.recording.read_bytes(), before)

    def test_case_hit_does_not_open_configured_history_or_create_cache(self):
        from history_core.problem_cases import save_case
        save_case(self.cases, {'id': 'known', 'title': '灯光插件效果核对',
                              'question': '灯光插件为什么没有生效',
                              'now': {'judgment': '先查实际操作回执'},
                              'reuse': {'steps': ['核对渲染对应的工具返回']}})
        unopened_cache = self.root / 'unopened-cache'
        recalled = self.invoke(['recall', '--query', '灯光插件', '--directory', str(self.cases),
                               '--source', 'codex', '--source-path', str(self.root / 'absent'),
                               '--data-dir', str(unopened_cache)])
        self.assertEqual(recalled['fallback_status'], 'not_needed')
        self.assertEqual(recalled['items'][0]['id'], 'known')
        self.assertFalse(unopened_cache.exists())

    def test_recalled_case_identity_matches_native_materials_without_rewriting_cases(self):
        self.refresh()
        packet = self.invoke([*self.prefix, 'compress', 'lighting', '--save',
                              '--case-dir', str(self.cases)], (0, 1))
        recalled = self.invoke(['recall', '--query', '灯光插件', '--directory', str(self.cases)], (0, 1))
        case = self.invoke(['cases', '--directory', str(self.cases), 'show',
                            recalled['items'][0]['id']])['case']
        evidence = {item['message_index']: item for item in case['evidence']}
        self.assertNotIn(1, evidence)  # Private reasoning keeps its original slot.
        self.assertIn('不能只看配置', evidence[5]['text'])
        saved_before = {path.name: path.read_bytes() for path in self.cases.glob('*.json')}
        arguments = [*self.prefix, 'materials', 'lighting', '--index-revision', packet['index_revision'],
                     '--message', '0:2', '--message', '5:0']
        materials = self.invoke(arguments)
        for key in ('source', 'store_id', 'session_id', 'index_revision'):
            self.assertEqual(materials[key], packet[key], key)
            for item in materials['items']:
                self.assertEqual(item[key], evidence[item['message_index']][key], key)
        self.assertEqual([item['message_index'] for item in materials['items']], [0, 5])
        self.assertEqual(materials['items'][0]['text_offset'], 2)
        self.assertEqual(materials['items'][0]['text'], evidence[0]['text'][2:])
        self.assertTrue(all(item['system'] == packet['system'] for item in materials['items']))
        self.assertTrue(materials['context_only'])
        repeated = self.invoke(arguments)
        self.assertEqual([item['selected_fragment_hash'] for item in materials['items']],
                         [item['selected_fragment_hash'] for item in repeated['items']])
        self.assertEqual(saved_before, {path.name: path.read_bytes() for path in self.cases.glob('*.json')})

    def test_miss_falls_back_to_the_existing_explicit_index_without_refresh(self):
        self.refresh()
        before = self.recording.read_bytes()
        recalled = self.invoke(['recall', '--query', '灯光插件', '--directory', str(self.cases), *self.prefix])
        self.assertFalse(recalled['items'])
        self.assertEqual(recalled['fallback_status'], 'searched')
        self.assertTrue(recalled['history']['items'])
        self.assertEqual(recalled['history']['mode'], 'brief')
        self.assertTrue(recalled['history']['index_revision'])
        self.assertFalse(self.cases.exists())
        self.assertEqual(self.recording.read_bytes(), before)

    def test_pagination_requires_matching_revision_and_retains_original_slots(self):
        self.refresh()
        packet = self.invoke([*self.prefix, 'compress', 'lighting', '--limit', '2'], (0, 1))
        self.assertTrue(packet['has_more'])
        self.assertTrue(packet['partial'])
        next_start = packet['next_start']
        self.assertGreaterEqual(next_start, 3)  # private reasoning keeps its original slot
        error = self.invoke([*self.prefix, 'compress', 'lighting', '--start', str(next_start)], (2,))
        self.assertIn('index_revision_required', error['detail'])
        second = self.invoke([*self.prefix, 'compress', 'lighting', '--start', str(next_start),
                              '--index-revision', packet['index_revision']], (0, 1))
        self.assertEqual(second['index_revision'], packet['index_revision'])
        self.events.append(self.message('user', '补充：只核对最终输出，继续保持未验证。'))
        self.write_recording()
        self.refresh()
        error = self.invoke([*self.prefix, 'compress', 'lighting', '--start', str(next_start),
                             '--index-revision', packet['index_revision']], (2,))
        self.assertIn('index_revision_changed', error['detail'])

    def test_bad_fallback_and_case_source_overlap_are_explicit(self):
        no_source = self.invoke(['recall', '--query', 'missing', '--directory', str(self.cases)])
        self.assertEqual(no_source['fallback_status'], 'source_required')
        unavailable = self.invoke(['recall', '--query', 'missing', '--directory', str(self.cases),
                                   '--source', 'codex', '--source-path', str(self.root / 'absent')], (1,))
        self.assertEqual(unavailable['fallback_status'], 'unavailable')
        self.assertTrue(unavailable['partial'])
        self.refresh()
        error = self.invoke([*self.prefix, 'compress', 'lighting', '--save', '--case-dir', str(self.source)], (2,))
        self.assertEqual(error['detail'], 'case_and_source_must_be_disjoint')
        self.assertEqual(list(self.source.iterdir()), [self.recording])


if __name__ == '__main__':
    unittest.main()
