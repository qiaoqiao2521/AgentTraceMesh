"""Legacy API regressions through a real server and synthetic source only."""
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from app import Handler
from history_core.sources import Indexer


class RequestBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        logs = root / 'sessions'
        logs.mkdir()
        self.path = logs / 's.jsonl'
        records = [
            {'type': 'session_meta', 'payload': {'id': 's', 'cwd': '/synthetic/project'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': 'synthetic original request'}]}}]
        self.original = '\n'.join(json.dumps(row) for row in records).encode() + b'\n'
        self.path.write_bytes(self.original)
        self.indexer = Indexer(logs, root, 'codex')
        self.addCleanup(self.indexer.conn.close)
        self.indexer.scan_sessions()
        self.backend = SimpleNamespace(
            indexer=self.indexer, ensure_ready=lambda: None,
            root_dir=logs, sessions_dir=logs,
            _refresh_index_once=self.indexer.scan_sessions,
            archived_dir=root / 'archived', deleted_dir=root / 'deleted', read_only=False)

        class Quiet(Handler):
            def log_message(self, *args):
                pass

        def handler(*args, **kwargs):
            return Quiet(*args, source_backends={('linux', 'codex'): self.backend},
                         runtime_system='linux', **kwargs)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, path, *, data=None, headers=None):
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_legacy_writes_and_network_calls_reject_untrusted_requests(self):
        guards = [
            {'Content-Type': 'text/plain'},
            {'Origin': 'https://attacker.invalid'},
            {'Sec-Fetch-Site': 'cross-site'},
            {'Origin': 'null'},
            {'Host': 'attacker.invalid'}]
        routes = [('/api/linux/codex/session/s/archive', {}),
                  ('/api/linux/codex/project/delete', {'project': '/synthetic/project'}),
                  ('/api/linux/codex/briefing', {'mode': 'llm'}),
                  ('/api/reuse/refresh', {'system': 'linux', 'source': 'codex'})]
        with patch.object(Handler, 'handle_briefing_generate',
                          side_effect=AssertionError('network handler must not run')):
            for path, data in routes:
                for headers in guards:
                    with self.subTest(path=path, headers=headers):
                        self.path.write_bytes(self.original)
                        self.indexer.scan_sessions()
                        status, _ = self.request(path, data=data, headers=headers)
                        self.assertEqual(status, 403)
                        self.assertEqual(self.path.read_bytes(), self.original)

    def test_valid_json_cli_and_same_origin_writes_remain_available(self):
        for headers in ({}, {'Origin': self.url, 'Sec-Fetch-Site': 'same-origin'}):
            status, result = self.request('/api/linux/codex/session/s/pin', data={'pinned': True}, headers=headers)
            self.assertEqual((status, result['ok']), (200, True))
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_all_read_api_routes_validate_host(self):
        for route in ('/api/version', '/api/sessions', '/api/reuse/health', '/api/linux/codex/session/s'):
            for host in ('attacker.invalid', '127.0.0.1:1', 'user@127.0.0.1'):
                with self.subTest(route=route, host=host):
                    self.assertEqual(self.request(route, headers={'Host': host})[0], 403)
        self.assertEqual(self.request('/api/linux/codex/session/s')[0], 200)

    def test_explicit_and_wildcard_lan_bind_keep_valid_host_access(self):
        handler = Handler.__new__(Handler)
        handler.send_json = lambda body, status=200: (status, body)
        cases = [('0.0.0.0', '192.0.2.10', '192.0.2.10:8787'),
                 ('192.0.2.10', '192.0.2.10', '192.0.2.10:8787'),
                 ('::', '::1', '[::1]:8787')]
        for bound, reached, host in cases:
            with self.subTest(bound=bound, host=host):
                handler.server = SimpleNamespace(server_address=(bound, 8787))
                handler.connection = SimpleNamespace(getsockname=lambda: (reached, 8787))
                handler.headers = {'Host': host, 'Content-Type': 'application/json'}
                self.assertTrue(handler._check_api_host())
                self.assertTrue(handler._same_origin_json())

    def test_reindex_requires_post_and_accepts_read_only_source(self):
        with patch.object(self.indexer, 'scan_sessions', wraps=self.indexer.scan_sessions) as scan:
            for route in ('/api/reindex', '/api/linux/codex/reindex'):
                self.assertEqual(self.request(route)[0], 405)
            scan.assert_not_called()
            self.backend.read_only = True  # derived index refresh is not a source mutation
            status, response = self.request('/api/linux/codex/reindex', data={})
            self.assertEqual((status, response['ok']), (200, True))
            self.assertEqual(scan.call_count, 1)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_backup_failures_preserve_actionable_api_receipts(self):
        with patch.object(self.indexer, 'archive_session', return_value=(False, 'backup_failed')):
            status, result = self.request('/api/linux/codex/session/s/archive', data={})
        self.assertEqual((status, result['error']), (500, 'backup_failed'))
        with patch.object(self.indexer, 'delete_project_sessions',
                          return_value=(False, 'partial', 2, '/synthetic/backup')):
            status, result = self.request('/api/linux/codex/project/delete', data={'project': '/synthetic/project'})
        self.assertEqual(status, 500)
        self.assertEqual(result, {'error': 'partial', 'partial': True,
                                  'deleted_count': 2, 'backup_dir': '/synthetic/backup'})
        self.assertEqual(self.path.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
