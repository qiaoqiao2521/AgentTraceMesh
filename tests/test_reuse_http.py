"""Real loopback API checks, with isolated synthetic histories only."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlencode
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from app import Handler, SourceBackend
from history_core import service
from history_core.sources import Indexer
from history_core.diagnostics import health


class ReuseHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.logs=self.root/'source';self.logs.mkdir()
        self.path=self.logs/'s.jsonl'
        lines=[{'type':'session_meta','timestamp':'2026-09-01T00:00:00Z','payload':{'id':'s','cwd':'/work/demo'}},
               {'type':'response_item','timestamp':'2026-09-01T00:00:01Z','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'修复 SQLite locked'}]}},
               {'type':'response_item','timestamp':'2026-09-01T00:00:02Z','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'SQLite locked resolved'}]}}]
        self.path.write_text('\n'.join(json.dumps(x,ensure_ascii=False) for x in lines)+'\n')
        self.original=self.path.read_bytes()
        self.idx=Indexer(self.logs,self.root,'codex');self.addCleanup(self.idx.conn.close);self.idx.scan_sessions()
        self.backend=SimpleNamespace(indexer=self.idx,root_dir=self.logs.parent,sessions_dir=self.logs,
            last_refresh_error=None,last_refreshed_at='2026-09-22T00:00:00Z',refreshing=False,
            ensure_ready=lambda:None,_refresh_index_once=lambda:self.idx.scan_sessions())
        self.backends={('linux','codex'):self.backend}
        class Quiet(Handler):
            def log_message(self,*a): pass
        def handler(*args,**kwargs): return Quiet(*args,source_backends=self.backends,runtime_system='linux',demo=True,**kwargs)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
        self.url='http://127.0.0.1:'+str(self.server.server_port)

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(5)

    def get(self,path,data=None):
        req=urllib.request.Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=5) as resp: return resp.status,json.load(resp)
        except urllib.error.HTTPError as exc: return exc.code,json.load(exc)

    def test_search_filters_deeplocators_and_validation(self):
        status,data=self.get('/api/reuse/search?q=SQLite&start=2026-09-01&end=2026-09-01')
        self.assertEqual(status,200);self.assertEqual(data['items'][0]['id'],'s')
        self.assertEqual(data['items'][0]['snippets'][0]['message_index'],0)
        self.assertEqual(self.get('/api/reuse/search?q=SQLite&start=bad')[0],400)
        self.assertEqual(self.get('/api/reuse/search?q=SQLite&source=bad')[0],400)
        self.assertEqual(self.get('/api/reuse/search?q=never')[1]['items'],[])
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_web_machine_provenance_selection_and_stale(self):
        status,response=self.get('/api/linux/codex/session/s/audit')
        self.assertEqual(status,200)
        web=response['handoff']['payload']['provenance'];machine=service.handoff(self.idx,'s')['payload']['provenance']
        self.assertEqual(web['content_revision'],machine['content_revision'])
        self.assertEqual(web['locator'],machine['locator'])
        status,item=self.get('/api/reuse/evidence?system=linux&source=codex&session=s')
        self.assertEqual(status,200)
        selection={'system':'linux','source':'codex','store_id':item['store_id'],'session_id':'s','message_index':0,'content_revision':item['provenance']['content_revision']}
        status,bundle=self.get('/api/reuse/selection',{'selections':[selection]})
        self.assertEqual(status,200);self.assertEqual(bundle['authorization'],'context_only')
        self.assertIn('SQLite locked',bundle['markdown'])
        self.path.write_bytes(self.original+b'\n')
        self.assertEqual(self.get('/api/reuse/selection',{'selections':[selection]})[0],409)
        self.assertEqual(self.get('/api/linux/codex/session/s/audit')[0],409)

    def test_review_preview_requires_original_user_message_and_preserves_binding(self):
        _, item = self.get('/api/reuse/evidence?system=linux&source=codex&session=s')
        selection = {'system':'linux', 'source':'codex', 'session_id':'s',
                     'content_revision':item['provenance']['content_revision']}
        status, error = self.get('/api/reuse/review-preview', {'selections':[{**selection, 'message_index':1}]})
        self.assertEqual(status, 400)
        self.assertEqual(error['error'], 'review_requirement_required')
        selected = [{**selection, 'message_index':i} for i in (0, 1)]
        status, packet = self.get('/api/reuse/review-preview', {'selections':selected})
        self.assertEqual(status, 200)
        self.assertEqual(packet['schema_version'], 'history.review-packet.v1')
        self.assertEqual(packet['code_verification'], 'not_performed')
        self.assertEqual([x['locator']['role'] for x in packet['items']], ['user', 'assistant'])
        self.assertIn('修复 SQLite locked', packet['markdown'])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.path.write_bytes(self.original+b'\n')
        self.assertEqual(self.get('/api/reuse/review-preview', {'selections':selected})[0], 409)

    def test_review_request_page_is_revision_bound(self):
        _, page = self.get('/api/reuse/search?q=SQLite')
        _, item = self.get('/api/reuse/evidence?system=linux&source=codex&session=s')
        params = {'system':'linux', 'source':'codex', 'session':'s', 'store_id':item['store_id'],
                  'source_revision':page['items'][0]['source_revision'],
                  'content_revision':item['provenance']['content_revision'], 'limit':1}
        status, result = self.get('/api/reuse/review-requests?' + urlencode(params))
        self.assertEqual(status, 200)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['message_index'], 0)
        self.assertEqual(result['items'][0]['role'], 'user')
        self.assertIsNone(result['next_offset'])
        self.assertEqual(self.get('/api/reuse/review-requests?' + urlencode({**params, 'limit':100}))[0], 400)
        self.path.write_bytes(self.original+b'\n')
        self.idx.scan_sessions()
        self.assertEqual(self.get('/api/reuse/review-requests?' + urlencode(params))[0], 409)

    def test_exact_raw_record_uses_line_reference_and_refuses_stale(self):
        _, item = self.get('/api/reuse/evidence?system=linux&source=codex&session=s')
        ref = next(x for x in item['evidence'] if x.get('line_no'))
        params = {'system': 'linux', 'source': 'codex', 'session': 's',
                  'evidence_id': ref['id'], 'content_revision': item['provenance']['content_revision']}
        path = '/api/reuse/raw?' + urlencode(params)
        status, data = self.get(path)
        self.assertEqual(status, 200)
        expected = self.original.decode().splitlines()[data['line_no'] - 1]
        self.assertEqual(data['text'], expected)
        self.assertNotIn('session_meta', data['text'])
        self.assertEqual(data['representation'], 'raw_jsonl_record')
        self.path.write_bytes(self.original + b'\n')
        self.assertIn(self.get(path)[0], (400, 409))

    def test_old_deep_link_cannot_show_shifted_message_after_refresh(self):
        _, data = self.get('/api/reuse/search?q=SQLite')
        revision = data['items'][0]['source_revision']
        suffix = '?source_revision=' + revision
        self.assertEqual(self.get('/api/linux/codex/session/s/messages' + suffix)[0], 200)
        self.path.write_bytes(self.original + b'\n')
        self.idx.scan_sessions()
        self.assertEqual(self.get('/api/reuse/evidence?system=linux&source=codex&session=s&source_revision=' + revision)[0], 409)
        for route in ('', '/messages', '/message/0', '/messages/search', '/search'):
            status, value = self.get('/api/linux/codex/session/s' + route + suffix)
            self.assertEqual(status, 409)
            self.assertEqual(value['error'], 'index_revision_changed')

    def test_session_search_rechecks_revision_after_query(self):
        _, page = self.get('/api/reuse/search?q=SQLite')
        revision = page['items'][0]['source_revision']
        search = self.idx.search_session_messages

        def changed(*args, **kwargs):
            result = search(*args, **kwargs)
            self.path.write_bytes(self.original + b'\n')
            self.idx.scan_sessions()
            return result

        with patch.object(self.idx, 'search_session_messages', side_effect=changed):
            status, result = self.get('/api/linux/codex/session/s/messages/search?q=SQLite&source_revision=' + revision)
        self.assertEqual(status, 409)
        self.assertEqual(result['error'], 'index_revision_changed')

    def test_indexing_health_does_not_wait_for_sqlite_lock(self):
        self.backend.refreshing = True
        with patch('history_core.diagnostics.query_budget', side_effect=AssertionError('index lock acquired')):
            result = health(self.backends)
        self.assertEqual(result['sources'][0]['status'], 'indexing')
        self.assertIsNone(result['sources'][0]['count'])

    def test_health_diagnostics_are_allowlisted_and_refresh(self):
        self.backend.root_dir=Path('/home/sensitive-user/token=secret')
        status,result=self.get('/api/reuse/health');self.assertEqual(status,200)
        self.assertEqual(result['sources'][0]['status'],'ready')
        self.assertEqual(result['sources'][0]['count'],1)
        exported=json.dumps(result['diagnostic'])
        for secret in ('sensitive-user','secret',str(self.root),'SQLite','locked'):
            self.assertNotIn(secret,exported)
        self.assertEqual(self.get('/api/reuse/refresh',{'system':'linux','source':'codex'})[0],200)
        self.backend.last_refresh_error='PermissionError: source refresh failed'
        self.assertEqual(self.get('/api/reuse/health')[1]['sources'][0]['status'],'stale')
        self.backend.last_refreshed_at=None
        self.assertEqual(self.get('/api/reuse/health')[1]['sources'][0]['status'],'unreadable')
        self.backend.last_refresh_error='ValueError: invalid data'
        self.assertEqual(self.get('/api/reuse/health')[1]['sources'][0]['status'],'unsupported')
        self.backend.last_refresh_error=None;self.backend.refreshing=True
        self.assertEqual(self.get('/api/reuse/health')[1]['sources'][0]['status'],'indexing')

    def test_empty_missing_malformed_and_recoverable_sources(self):
        self.path.unlink();self.idx.scan_sessions()
        self.assertEqual(self.get('/api/reuse/health')[1]['sources'][0]['status'],'empty')
        self.logs.rmdir()
        self.assertEqual(self.get('/api/reuse/health')[1]['sources'][0]['status'],'stale')
        self.backend.last_refreshed_at=None
        self.assertEqual(self.get('/api/reuse/health')[1]['sources'][0]['status'],'not_found')
        self.logs.mkdir();self.path.write_bytes(self.original+b'{bad tail')
        self.idx.scan_sessions();self.assertEqual(self.idx.list_sessions_page()['items'][0]['id'],'s')
        self.path.write_text('not-json\n')
        with self.assertRaises(ValueError): self.idx.scan_sessions()

    def test_missing_native_database_does_not_crash_source_catalog(self):
        with patch.object(SourceBackend,'_start_background_refresh'):
            from history_core.sources import OpenCodeIndexer
            db=self.root/'missing.db'
            backend=SourceBackend(system='linux',source='opencode',root_dir=self.root,sessions_dir=db,
                data_dir=self.root,db_filename='ignore.sqlite',parse_file_fn=None,parser_version=1,
                scan_interval=5,archived_dir=self.root/'archive',deleted_dir=self.root/'deleted',recall_db_path=None,
                indexer_factory=lambda:OpenCodeIndexer(db),read_only=True)
            self.assertIsNone(backend.indexer)
            report=health({('linux','opencode'):backend})
            self.assertEqual(report['sources'][0]['status'],'not_found')
            self.assertFalse(db.exists())

if __name__=='__main__': unittest.main()
