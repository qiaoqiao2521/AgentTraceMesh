"""Problem cases are local authored notes, independent from provider startup."""
import copy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import quote
from unittest.mock import patch

from app import Handler


class ProblemCasesHttpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / 'authored-cases'
        self.note = {
            'id': 'follow-evidence', 'title': 'Follow the correction',
            'question': 'Does a limited packet prove missing pagination?',
            'now': {'judgment': 'Check the packet and actual API separately.',
                    'task_status': 'unknown', 'applicability': 'limited input only'},
            'reuse': {'steps': ['Inspect what the worker could read before changing the product.']}}

        class Quiet(Handler):
            def log_message(self, *args):
                pass

            def _reuse_sources(self, *args, **kwargs):
                raise AssertionError('case retrieval must not initialize history sources')

        def handler(*args, **kwargs):
            return Quiet(*args, case_dir=self.directory, **kwargs)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, path, data=None, headers=None):
        request = urllib.request.Request(self.url + '/api/reuse/' + quote(path, safe='/?=&%'),
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_list_example_without_creating_directory_or_reading_sources(self):
        status, page = self.request('cases?q=分页')
        self.assertEqual(status, 200)
        self.assertTrue(page['items'])
        self.assertFalse(self.directory.exists())
        self.assertEqual(page['current_verification'], 'not_performed')
        self.assertIn('派遣边界', json.dumps(page, ensure_ascii=False))

    def test_save_show_and_direct_edit_share_canonical_markdown(self):
        from history_core.problem_cases import get_case
        status, saved = self.request('case-save', self.note)
        self.assertEqual(status, 200)
        self.assertEqual(saved['case']['id'], self.note['id'])
        status, shown = self.request('case?id=follow-evidence')
        self.assertEqual(status, 200)
        self.assertEqual(shown['markdown'], get_case(self.directory, 'follow-evidence')['markdown'])
        self.assertEqual(shown['current_verification'], 'not_performed')
        path = self.directory / 'follow-evidence.json'
        edited = json.loads(path.read_text())
        edited['now']['judgment'] = 'A human changed this explanation.'
        path.write_text(json.dumps(edited))
        status, refreshed = self.request('case?id=follow-evidence')
        self.assertEqual(status, 200)
        self.assertIn('A human changed this explanation.', refreshed['markdown'])
        self.assertEqual(refreshed['case']['now']['judgment'], edited['now']['judgment'])

    def test_invalid_local_item_is_partial_without_hiding_good_results(self):
        self.request('case-save', self.note)
        (self.directory / 'bad.json').write_text('{broken')
        status, page = self.request('cases')
        self.assertEqual(status, 200)
        self.assertTrue(page['partial'])
        self.assertTrue(page['errors'])
        self.assertIn('follow-evidence', [item['id'] for item in page['items']])

    def test_write_origin_host_and_content_type_are_protected(self):
        for headers in ({'Origin': 'https://attacker.invalid'},
                        {'Host': 'attacker.invalid'}, {'Content-Type': 'text/plain'},
                        {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('case-save', self.note, headers)[0], 403)
                self.assertFalse(self.directory.exists())

    def test_case_save_masks_authored_secrets_without_claiming_verification(self):
        note = copy.deepcopy(self.note)
        note['now']['judgment'] = 'password=SYNTHETIC_ONLY_NOT_A_SECRET'
        status, result = self.request('case-save', note)
        self.assertEqual(status, 200)
        self.assertNotIn('SYNTHETIC_ONLY_NOT_A_SECRET', json.dumps(result))
        self.assertNotIn('SYNTHETIC_ONLY_NOT_A_SECRET',
                         (self.directory / 'follow-evidence.json').read_text())
        self.assertEqual(result['source_verification'], 'references_not_revalidated')

    def test_deep_case_returns_json_error_and_server_keeps_serving(self):
        # Parsing can succeed while the authored-value traversal exceeds depth.
        body = ('{"id":"deep","title":"Depth","now":{"judgment":'
                + '[' * 1500 + '"synthetic"' + ']' * 1500 + '}}').encode()
        request = urllib.request.Request(self.url + '/api/reuse/case-save', data=body,
                                         headers={'Content-Type': 'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as failed:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(failed.exception.code, 400)
        self.assertEqual(json.load(failed.exception)['error'], 'case_nesting_invalid')
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.request('case-save', self.note)[0], 200)
        self.assertEqual(self.request('case?id=follow-evidence')[0], 200)

    def test_invalid_id_and_unknown_case_do_not_read_arbitrary_files(self):
        self.assertEqual(self.request('case?id=..%2Foutside')[0], 400)
        self.assertEqual(self.request('case?id=no-such-case')[0], 404)
        bad = {**self.note, 'id': '../outside'}
        self.assertEqual(self.request('case-save', bad)[0], 400)

    def test_spoofed_loopback_host_from_remote_peer_cannot_save(self):
        from urllib.parse import urlparse
        handler = Handler.__new__(Handler)
        handler.headers = {'Host': 'localhost:8787', 'Content-Type': 'application/json'}
        handler.client_address = ('192.0.2.20', 4321)
        handler.send_json = lambda data, status=200: (status, data)
        with patch('history_core.problem_cases.save_case', side_effect=AssertionError('must not write')):
            status, _ = handler.handle_reuse_post(urlparse('/api/reuse/case-save'), self.note)
        self.assertEqual(status, 403)

    def test_demo_case_reads_never_fall_back_to_private_default_directory(self):
        from http.server import SimpleHTTPRequestHandler
        from urllib.parse import urlparse
        with patch('app.__file__', str(self.directory.parent / 'app.py')):
            with patch.object(SimpleHTTPRequestHandler, '__init__', return_value=None):
                handler = Handler(demo=True)
        handler.send_json = lambda data, status=200: (status, data)
        with patch('history_core.problem_cases.default_case_directory',
                   side_effect=AssertionError('demo must not discover private cases')):
            status, page = handler.handle_reuse_get(urlparse('/api/reuse/cases'))
        self.assertEqual(status, 200)
        self.assertTrue(page['items'])
        self.assertFalse(self.directory.parent.joinpath('demo-isolated').exists())


if __name__ == '__main__':
    unittest.main()
