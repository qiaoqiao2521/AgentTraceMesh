"""Synthetic public timeline windows; never reads personal source databases."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from history_core.agy import AgyIndexer
from history_core.native_timeline import timeline
from history_core.sources import Indexer
import test_uniform_brief as native_fixtures
from test_agy import blob, metadata, step
from test_zcode import TS


class NativeTimelineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def native(self, source):
        return native_fixtures.UniformBriefTests.native(self, source)

    def indexed(self):
        sessions = self.root/'sessions'; sessions.mkdir()
        cache = self.root/'cache'; cache.mkdir()
        idx = Indexer(sessions,cache,'codex')
        self.addCleanup(idx.conn.close)
        with idx.conn:
            idx.conn.execute("INSERT INTO sessions(id,title,cwd,start_ts_ms,end_ts_ms) VALUES('chosen','Indexed fixture','/fixture',?,?)", (TS,TS+3))
            idx.conn.executemany('INSERT INTO messages(session_id,ts_ms,role,kind,text) VALUES(?,?,?,?,?)', [
                ('chosen',TS,'user','message','request artifact.png'),
                ('chosen',TS+1,'assistant','reasoning_summary','HIDDEN_REASONING'),
                ('chosen',TS+2,'assistant','message','answer '+'x'*700),
                ('chosen',TS+3,'tool','tool_result','token=synthetic-secret-value\nfinished')])
        return idx

    def agy(self, source='agy'):
        conversations = self.root/'conversations'; conversations.mkdir()
        summary = self.root/'conversation_summaries.db'
        with sqlite3.connect(summary) as conn:
            conn.execute('''CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY,title TEXT,
                step_count INTEGER,last_modified_time TEXT,workspace_uris TEXT)''')
            conn.execute('INSERT INTO conversation_summaries VALUES(?,?,?,?,?)',
                ('chosen','AGY fixture',3,'2026-09-21T12:00:00Z','["file:///fixture"]'))
        path = conversations/'chosen.db'
        payloads = [step(14,blob(1,'请查找今天的修改'),meta=metadata(timestamp=TS//1000)),
                    step(15,blob(3,'HIDDEN_REASONING'),meta=metadata(timestamp=TS//1000+1)),
                    step(15,blob(1,'已经整理好了'),meta=metadata(timestamp=TS//1000+2))]
        with sqlite3.connect(path) as conn:
            conn.execute('''CREATE TABLE steps(idx INTEGER PRIMARY KEY,step_type INTEGER,status INTEGER,
                step_format INTEGER,step_payload BLOB)''')
            conn.executemany('INSERT INTO steps VALUES(?,?,3,0,?)',
                [(n,14 if n==0 else 15,payload) for n,payload in enumerate(payloads)])
        idx = AgyIndexer(summary,source=source)
        self.addCleanup(idx.conn.close)
        return idx,path

    def test_native_time_window_filters_public_events_before_pagination(self):
        for source in ('opencode','zcode','hermes'):
            with self.subTest(source=source):
                idx,path = self.native(source)
                before = path.read_bytes()
                page = timeline(idx,start_ms=TS+1,end_ms=TS+4000,session_id='chosen',limit=1)
                self.assertEqual(page['schema_version'],'history.timeline.v1')
                self.assertEqual(page['items'][0]['message_index'],3)
                self.assertTrue(page['has_more'])
                second = timeline(idx,start_ms=TS+1,end_ms=TS+4000,session_id='chosen',
                    limit=1,offset=page['next_offset'])
                self.assertEqual(second['items'][0]['message_index'],2)
                self.assertFalse(second['has_more'])
                self.assertNotIn('HIDDEN_ONLY',json.dumps([page,second]))
                self.assertFalse(page['partial'])
                self.assertEqual(path.read_bytes(),before)
                self.assertFalse(Path(str(path)+'-wal').exists())

    def test_half_open_boundaries_match_message_not_session_time(self):
        for source in ('opencode','zcode','hermes'):
            with self.subTest(source=source):
                idx,_ = self.native(source)
                page = timeline(idx,start_ms=TS,end_ms=TS+1,session_id='chosen')
                self.assertEqual([item['message_index'] for item in page['items']],[0])
                factor = 1000 if source=='hermes' else 1
                start = TS+3*factor
                self.assertEqual([item['message_index'] for item in timeline(idx,
                    start_ms=start,end_ms=start+1,session_id='chosen')['items']],[3])

    def test_indexed_public_events_keep_private_slots_and_mask_secrets(self):
        idx = self.indexed()
        page = timeline(idx,start_ms=TS,end_ms=TS+4)
        self.assertEqual([item['message_index'] for item in page['items']],[3,2,0])
        self.assertNotIn('synthetic-secret-value',json.dumps(page))
        self.assertNotIn('HIDDEN_REASONING',json.dumps(page))
        self.assertTrue(page['items'][1]['truncated'])
        self.assertLessEqual(len(page['items'][1]['text']),240)
        self.assertEqual(page['coverage'],'public_message_timestamps_not_full_activity')
        self.assertEqual(timeline(idx,start_ms=TS+4,end_ms=TS+5)['items'],[])

    def test_agy_aliases_reuse_indexed_projection_and_disclose_partial(self):
        for source in ('agy','antigravity'):
            with self.subTest(source=source):
                location = self.root/source; location.mkdir()
                original,self.root = self.root,location
                try:
                    idx,path = self.agy(source)
                    before = path.read_bytes()
                    page = timeline(idx,start_ms=TS,end_ms=TS+3000)
                    self.assertEqual([item['message_index'] for item in page['items']],[2,0])
                    self.assertIn('请查找今天的修改',json.dumps(page,ensure_ascii=False))
                    self.assertNotIn('HIDDEN_REASONING',json.dumps(page))
                    self.assertEqual(path.read_bytes(),before)
                    idx._coverage['missing'] = {'content_status':'source_missing'}
                    self.assertTrue(timeline(idx)['partial'])
                    self.assertEqual(timeline(idx)['source_coverage']['incomplete_sessions'],1)
                finally:
                    self.root = original

    def test_only_selected_native_public_record_reaches_renderer(self):
        idx,path = self.native('zcode')
        giant = {'type':'tool','tool':'giant','state':{'status':'completed','output':'x'*(3*1024*1024)}}
        with sqlite3.connect(path) as writer:
            writer.execute('INSERT INTO part VALUES(?,?,?,?,?,?,?)',
                ('giant','u','chosen',TS-1,TS-1,json.dumps(giant),-1))
        rendered=[]
        real = idx._flatten_part
        def render(role,kind,data,stamp):
            rendered.append(data.get('tool') or data.get('text'))
            self.assertNotEqual(data.get('tool'),'giant')
            return real(role,kind,data,stamp)
        with patch.object(idx,'_flatten_part',side_effect=render):
            page = timeline(idx,session_id='chosen',limit=1)
        self.assertEqual(len(page['items']),1)
        self.assertEqual(rendered,['bash'])
        self.assertEqual(page['items'][0]['message_index'],4)

    def test_oversized_selected_native_event_is_an_explicit_partial_candidate(self):
        idx,path = self.native('zcode')
        with sqlite3.connect(path) as writer:
            writer.execute("UPDATE part SET data=? WHERE id='p3'",(json.dumps({
                'type':'tool','tool':'huge','state':{'output':'x'*(3*1024*1024)}}),))
        with patch.object(idx,'_flatten_part',side_effect=AssertionError('oversized body rendered')):
            page = timeline(idx,session_id='chosen',limit=1)
        self.assertTrue(page['partial'])
        self.assertEqual(page['items'][0]['message_index'],3)
        self.assertEqual(page['items'][0]['evidence_status'],'unavailable')
        self.assertEqual(page['errors'][0]['error'],'native_public_message_limit_exceeded')

    def test_ties_are_stable_and_invalid_windows_are_rejected(self):
        idx = self.indexed()
        with idx.conn:
            idx.conn.execute('UPDATE messages SET ts_ms=?',(TS,))
        pages = [timeline(idx,limit=1,offset=n)['items'][0]['message_index'] for n in range(3)]
        self.assertEqual(pages,[0,2,3])
        for arguments in ({'start_ms':TS,'end_ms':TS},{'end_ms':True},{'limit':101},{'offset':-1}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                timeline(idx,**arguments)


if __name__=='__main__':
    unittest.main()
