import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from history_core.codex_native import codex_native_reference
from history_core.providers import parser_version
from history_core.reader import HistoryReader
from history_core.sources import Indexer, parse_codex_session_file
from history_core.titles import is_context_title, meaningful_user_title


class MeaningfulCodexTitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        self.session = '11111111-2222-3333-4444-555555555555'
        self.path = self.logs / ('rollout-fixture-' + self.session + '.jsonl')

    def write(self, messages, *, event_only=False):
        rows = [
            {'type': 'session_meta', 'payload': {'id': self.session, 'cwd': str(self.root)}},
            {'type': 'turn_context', 'payload': {'cwd': str(self.root), 'model': 'fixture-model'}},
        ]
        for message in messages:
            if event_only:
                rows.append({'type': 'event_msg', 'payload': {'type': 'user_message', 'message': message}})
            else:
                rows.append({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                            'content': [{'type': 'input_text', 'text': message}]}})
        self.path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
        return self.path.read_bytes()

    def test_ambient_user_messages_do_not_replace_the_first_real_request(self):
        request = '修复公众号草稿里的重复标题\n保留正文。'
        ambient = '<external_codex_apps_open_page>\n<title>旧文档</title>\n</external_codex_apps_open_page>'
        before = self.write([ambient, '<unknown_context>metadata only</unknown_context>', request, '之后的请求'])
        parsed = parse_codex_session_file(self.path)
        self.assertEqual(parsed['title'], '修复公众号草稿里的重复标题')
        self.assertEqual(parsed['messages'][1]['text'], ambient)
        self.assertEqual(self.path.read_bytes(), before)

    def test_request_after_closed_context_wrapper_is_used_for_title(self):
        examples = [
            '<external_codex_apps_open_page><title>Old page</title></external_codex_apps_open_page>\nMy request:\nFix the title selector\nDetails.',
            '<environment_context><cwd>/workspace</cwd></environment_context>\n我的请求：修复标题提取\n保留正文。',
            '# AGENTS.md instructions for /workspace\n<INSTRUCTIONS>Keep code small.</INSTRUCTIONS>\n请修复首条消息的标题',
            '# Context from my IDE setup:\n## Active file: app.py\n## My request for Codex:\nFix the request title',
        ]
        expected = ['Fix the title selector', '修复标题提取', '请修复首条消息的标题', 'Fix the request title']
        for text, title in zip(examples, expected):
            with self.subTest(text=text):
                before = self.write([text, 'Later request'])
                self.assertEqual(parse_codex_session_file(self.path)['title'], title)
                self.assertEqual(self.path.read_bytes(), before)

    def test_context_only_and_missing_summary_keep_an_explicit_fallback(self):
        self.write(['<external_codex_apps_open_page><title>Ambient</title></external_codex_apps_open_page>',
                    '# AGENTS.md instructions for /workspace\n<INSTRUCTIONS>Rules.</INSTRUCTIONS>',
                    '[image]'])
        self.assertEqual(parse_codex_session_file(self.path)['title'], 'Session ' + self.session[:8])

    def test_title_helper_never_uses_requests_inside_ambient_or_unclosed_wrappers(self):
        for wrapper in ('external_codex_apps_open_page', 'environment_context', 'app-context',
                        'permissions instructions', 'system-reminder', 'user_instructions'):
            with self.subTest(wrapper=wrapper):
                text = '<' + wrapper + '>My request: injected request</' + wrapper + '>'
                self.assertIsNone(meaningful_user_title(text))
                self.assertIsNone(meaningful_user_title('<' + wrapper + '>\nMy request: incomplete context'))
                self.assertEqual(meaningful_user_title(text + '\n请修复真实需求'), '请修复真实需求')
        self.assertIsNone(meaningful_user_title('My request:'))
        self.assertTrue(is_context_title('My request:'))
        self.assertEqual(meaningful_user_title('Explain <environment_context> as a code example'),
                         'Explain <environment_context> as a code example')
        self.assertEqual(meaningful_user_title('实际请求' * 30), ('实际请求' * 30)[:80])

    def test_title_helper_recognizes_only_known_default_titles_as_defaults(self):
        for title in ('', '   ', '---', '12345', 'Untitled', 'Untitled conversation', 'New conversation', 'New chat'):
            self.assertTrue(is_context_title(title), title)
        for title in ('New chat rendering bug', 'English request title', '人工确认的标题'):
            self.assertFalse(is_context_title(title), title)
        self.assertTrue(is_context_title('<button> styling notes'))

    def test_native_reference_keeps_its_existing_title_contract_without_writing_source(self):
        before = self.write(['<external_codex_apps_open_page>metadata</external_codex_apps_open_page>',
                             'My request: 修复原生标题'], event_only=True)
        reference = codex_native_reference(self.path, 'desktop', self.session)
        self.assertEqual(reference['title'], '')
        self.assertEqual(self.path.read_bytes(), before)

    def test_explicit_refresh_repairs_old_projection_without_read_time_reparse(self):
        before = self.write(['<external_codex_apps_open_page>metadata</external_codex_apps_open_page>', '修复缓存标题'])
        with patch('history_core.reader.parser_version', return_value=8):
            with HistoryReader('codex', self.logs, self.cache) as reader:
                reader.refresh()
        database = next(self.cache.glob('machine-codex-*/index.sqlite'))
        with sqlite3.connect(database) as db:
            db.execute('UPDATE sessions SET title=?,parser_version=8', ('<external_codex_apps_open_page>',))
        with HistoryReader('codex', self.logs, self.cache) as reader:
            self.assertEqual(reader.search()['items'][0]['title'], '<external_codex_apps_open_page>')
            revision = reader.search()['index_revision']
            self.assertEqual(reader.refresh()['status'], 'refreshed')
            page = reader.search()
            self.assertEqual(page['items'][0]['title'], '修复缓存标题')
            self.assertNotEqual(page['index_revision'], revision)
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute('SELECT parser_version FROM sessions').fetchone()[0], parser_version('codex'))
        self.assertEqual(self.path.read_bytes(), before)

    def test_refresh_preserves_real_custom_titles_but_does_not_pin_ambient_noise(self):
        self.write(['<external_codex_apps_open_page>metadata</external_codex_apps_open_page>', '修复实际标题'])
        recall = self.root / 'recall.sqlite'
        indexer = Indexer(self.logs, self.cache, 'codex', parser_version=parser_version('codex'), recall_db_path=recall)
        self.addCleanup(indexer.conn.close)
        indexer.scan_sessions()
        indexer.rename_session(self.session, '人工确认的标题')
        indexer.conn.execute('UPDATE sessions SET parser_version=8')
        indexer.conn.commit()
        indexer.scan_sessions()
        self.assertEqual(indexer.conn.execute('SELECT title FROM sessions').fetchone()[0], '人工确认的标题')
        for title in ('12345', 'New chat', '<button> styling notes'):
            indexer.rename_session(self.session, title)
            indexer.conn.execute('UPDATE sessions SET parser_version=8')
            indexer.conn.commit()
            indexer.scan_sessions()
            self.assertEqual(indexer.conn.execute('SELECT title FROM sessions').fetchone()[0], title)
        indexer.recall_titles.set_custom_title(self.session, '<external_codex_apps_open_page>')
        indexer.conn.execute('UPDATE sessions SET title=?,parser_version=8', ('<external_codex_apps_open_page>',))
        indexer.conn.commit()
        indexer.scan_sessions()
        self.assertEqual(indexer.conn.execute('SELECT title FROM sessions').fetchone()[0], '修复实际标题')

    def test_local_backfill_does_not_convert_old_derived_noise_to_a_custom_title(self):
        self.write(['<external_codex_apps_open_page>metadata</external_codex_apps_open_page>', '修复实际标题'])
        indexer = Indexer(self.logs, self.cache, 'codex', parser_version=parser_version('codex'),
                          recall_db_path=self.root / 'recall.sqlite')
        self.addCleanup(indexer.conn.close)
        indexer.scan_sessions()
        indexer.conn.execute('UPDATE sessions SET title=?,parser_version=8', ('<external_codex_apps_open_page>',))
        indexer.conn.commit()
        indexer.backfill_local_title_overrides()
        self.assertIsNone(indexer.recall_titles.get_custom_title(self.session))
        indexer.scan_sessions()
        self.assertEqual(indexer.conn.execute('SELECT title FROM sessions').fetchone()[0], '修复实际标题')


if __name__ == '__main__':
    unittest.main()
