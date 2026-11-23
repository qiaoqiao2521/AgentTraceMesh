import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from history_core import query_activity, HistoryReader
from history_core.activity import window


def codex(path, sid, pairs):
    events=[dict(type='session_meta', timestamp=pairs[0][0], payload=dict(id=sid,cwd='/fixture/project',timestamp=pairs[0][0]))]
    events += [dict(type='response_item',timestamp=ts,payload=dict(type='message',role='user',content=[dict(type='input_text',text=text)])) for ts,text in pairs]
    path.write_text('\n'.join(json.dumps(x) for x in events)+'\n')


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.src=self.root/'sessions';self.src.mkdir();self.cache=self.root/'cache'
        codex(self.src/'old.jsonl','old',[('2026-09-25T20:00:00Z','OUTSIDE OLD'),('2026-09-27T04:00:00Z','CONTINUED TODAY')])
        codex(self.src/'new.jsonl','new',[('2026-09-27T03:00:00Z','STARTED TODAY'),('2026-09-27T16:00:00Z','TOMORROW')])
        codex(self.src/'edge.jsonl','edge',[('2026-09-27T15:59:59.500Z','LAST HALF SECOND')])
    def query(self,**kw):
        return query_activity({'codex':self.src},date='2026-09-27',timezone='Asia/Shanghai',data_dir=self.cache,**kw)
    def test_actual_web_handler_excludes_outside_activity(self):
        import app
        from history_core.sources import Indexer,parse_codex_session_file
        with (self.src/'new.jsonl').open('a') as f:
            f.write(json.dumps(dict(type='event_msg',timestamp='2026-09-27T16:00:01Z',
                payload=dict(type='token_count',info=dict(total_token_usage=dict(input_tokens=123,output_tokens=0,total_tokens=123)))))+'\n')
        cache=self.root/'legacy';cache.mkdir()
        idx=Indexer(self.src,cache,'codex',parse_file_fn=parse_codex_session_file);self.addCleanup(idx.conn.close);idx.scan_sessions()
        previous=os.environ.get('TZ');os.environ['TZ']='Asia/Shanghai';time.tzset()
        try:
            handler=object.__new__(app.Handler)
            report,md,error=handler._build_briefing_for_range(SimpleNamespace(indexer=idx,source='codex'),'2026-09-27',None,'Asia/Shanghai')
            self.assertIsNone(error)
            # Fixed product includes old continuation and fractional end-of-day; no whole-session token usage.
            self.assertEqual(report['overview']['session_count'],3)
            self.assertNotIn('tokens_total',report['overview'])
            self.assertIn('CONTINUED TODAY',md)
            self.assertNotIn('TOMORROW',md)
            self.assertNotIn('OUTSIDE OLD',md)
            page=idx.list_sessions_page(start_ms=app.parse_date_param('2026-09-27',False),end_ms=app.parse_date_param('2026-09-27',True),limit=100,offset=0)
            self.assertEqual({r['id'] for r in page['items']},{'new'})
            self.assertEqual(next(r for r in page['items'] if r['id']=='new')['message_count'],2)
        finally:
            if previous is None:os.environ.pop('TZ',None)
            else:os.environ['TZ']=previous
            time.tzset()
    def test_window_and_host_timezone(self):
        result=self.query(refresh=True);items=result['sources'][0]['items']
        self.assertEqual({x['session_id'] for x in items},{'old','new','edge'})
        texts={e['text'] for x in items for e in x['evidence']}
        self.assertEqual(texts,{'CONTINUED TODAY','STARTED TODAY','LAST HALF SECOND'})
        self.assertEqual(result['receipt']['covered_sources'],['codex'])
        self.assertEqual(result['receipt']['failed_sources'],[])
        self.assertTrue(result['receipt']['partial'])
        self.assertIsNone(result['receipt']['checked_through'])
        self.assertEqual(result['receipt']['watermarks']['codex']['freshness'],'observed_unchanged')
        previous=os.environ.get('TZ');os.environ['TZ']='UTC';time.tzset()
        try:self.assertEqual(self.query()['sources'][0]['items'],items)
        finally:
            if previous is None:os.environ.pop('TZ',None)
            else:os.environ['TZ']=previous
            time.tzset()
    def test_unindexed_append_delete_and_revision(self):
        self.assertTrue(self.query()['partial'])
        first=self.query(refresh=True,limit=1)['sources'][0]
        first_receipt=self.query(refresh=True,limit=1)['receipt']
        self.assertIn({'source':'codex','limitation':'truncated'},first_receipt['limitations'])
        codex(self.src/'extra.jsonl','extra',[('2026-09-27T08:00:00Z','APPEND')])
        self.assertEqual(self.query()['sources'][0]['coverage']['freshness'],'stale')
        new=self.query(refresh=True)
        self.assertEqual(len(new['sources'][0]['items']),4)
        changed=self.query(offset=1,revisions={'codex':first['index_revision']})
        self.assertIn('revision',changed['sources'][0]['coverage']['detail'])
        (self.src/'extra.jsonl').unlink()
        self.assertEqual(len(self.query(refresh=True)['sources'][0]['items']),3)
    def test_missing_unsupported_and_shared_id(self):
        claude=self.root/'claude';claude.mkdir()
        (claude/'old.jsonl').write_text(json.dumps(dict(sessionId='old',timestamp='2026-09-27T08:00:00Z',cwd='/fixture/project',type='user',message=dict(role='user',content='CLAUDE TODAY')))+'\n')
        r=query_activity({'codex':self.src,'claude':claude,'opencode':None,'mcode':self.root/'missing'},date='2026-09-27',timezone='Asia/Shanghai',data_dir=self.cache,refresh=True)
        self.assertEqual([s['source'] for s in r['sources']],['codex','claude','opencode','mcode'])
        ids=[x for s in r['sources'] for x in s['items'] if x['session_id']=='old']
        self.assertEqual(len(ids),2);self.assertNotEqual(ids[0]['store_id'],ids[1]['store_id'])
        self.assertTrue(r['partial']);self.assertFalse(r['sources'][2]['coverage']['supported'])
        receipt=r['receipt']
        self.assertEqual(receipt['requested_sources'],['codex','claude','opencode','mcode'])
        self.assertEqual(receipt['covered_sources'],['codex','claude'])
        self.assertEqual(receipt['failed_sources'],['opencode','mcode'])
        self.assertTrue(receipt['partial'])
        self.assertIn({'source':'opencode','reason':'activity_source_unsupported'},receipt['reasons'])
        self.assertEqual(receipt['window'],r['window'])
    def test_dst_window(self):
        a,b=window('2026-03-08','America/New_York');self.assertEqual(b-a,23*3600000)

if __name__=='__main__':unittest.main()

class ActivityBoundaryTests(unittest.TestCase):
    setUp = ActivityTests.setUp
    query = ActivityTests.query
    def test_message_pagination_and_time_unknown(self):
        path=self.src/'many.jsonl'
        codex(path,'many',[(f'2026-09-27T04:00:0{i}Z',f'ROW {i}') for i in range(5)])
        first=self.query(refresh=True,session_id='many',evidence_limit=2)['sources'][0]
        self.assertEqual([e['text'] for e in first['items'][0]['evidence']],['ROW 3','ROW 4'])
        second=self.query(session_id='many',evidence_limit=2,message_offset=2,revisions={'codex':first['index_revision']})
        self.assertEqual([e['text'] for e in second['sources'][0]['items'][0]['evidence']],['ROW 1','ROW 2'])
        with path.open('a') as f:
            f.write(json.dumps(dict(type='response_item',payload=dict(type='message',role='user',content=[dict(type='input_text',text='UNKNOWN TIME')]))))
        result=self.query(refresh=True,session_id='many')
        self.assertNotIn('UNKNOWN TIME',json.dumps(result));self.assertTrue(result['partial'])
        self.assertEqual(result['sources'][0]['coverage']['messages_without_verified_time'],1)
        self.assertIn({'source':'codex','limitation':'messages_without_verified_time'},result['receipt']['limitations'])
    def test_changing_file_keeps_stable_records_and_discloses(self):
        from unittest.mock import patch
        from history_core.sources import parse_codex_session_file
        path=self.src/'old.jsonl'
        def changing(p):
            result=parse_codex_session_file(p)
            if p==path:
                with p.open('a') as f:f.write('\n')
            return result
        with patch('history_core.reader.parser_for',return_value=changing):
            result=self.query(refresh=True)
        self.assertTrue(result['partial'])
        self.assertIn('partial_refresh_unstable',result['sources'][0]['coverage']['refresh_error'])
        self.assertEqual(result['receipt']['failed_sources'],['codex'])
        self.assertEqual(result['receipt']['reasons'],[{'source':'codex','reason':'refresh_error'}])
        self.assertIn({'source':'codex','limitation':'refresh_error'},result['receipt']['limitations'])
        self.assertEqual({i['session_id'] for i in result['sources'][0]['items']},{'new','edge'})
    def test_headless_cli_and_invalid_timezone(self):
        import subprocess,sys
        cmd=[sys.executable,'-m','history_core','activity','--date','2026-09-27','--timezone','Asia/Shanghai','--sources','codex','--store','codex='+str(self.src),'--data-dir',str(self.cache),'--refresh']
        env=dict(os.environ);env.pop('DISPLAY',None)
        result=subprocess.run(cmd,capture_output=True,text=True,env=env)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(len(json.loads(result.stdout)['sources'][0]['items']),3)
        cmd[cmd.index('Asia/Shanghai')]='invalid/timezone'
        result=subprocess.run(cmd,capture_output=True,text=True,env=env)
        self.assertEqual(result.returncode,2);self.assertNotIn('Traceback',result.stderr)
    def test_duplicate_recordings_do_not_erase_today(self):
        codex(self.src/'copy.jsonl','old',[('2026-09-25T20:00:00Z','STALE COPY')])
        first=self.query(refresh=True)
        self.assertIn('CONTINUED TODAY',json.dumps(first))
        self.assertEqual(first['sources'][0]['coverage']['identity_conflicts'],1)
        self.assertIn('CONTINUED TODAY',json.dumps(self.query(refresh=True)))
    def test_exact_message_text_pages_stay_in_day_and_redacted(self):
        codex(self.src/'long.jsonl','long',[('2026-09-26T01:00:00Z','WINDOW EXCLUDED'),('2026-09-27T04:00:00Z','A'*1990+' password=TOPSECRET '+ 'B'*2500)])
        first=self.query(refresh=True,session_id='long')['sources'][0]
        ev=first['items'][0]['evidence'][0]
        self.assertNotIn('TOPSECRET',ev['text'])
        next_page=self.query(session_id='long',message_index=ev['message_index'],text_offset=2000,revisions={'codex':first['index_revision']})
        self.assertNotIn('TOPSECRET',json.dumps(next_page))
        self.assertEqual(next_page['sources'][0]['items'][0]['evidence'][0]['text_offset'],2000)
        excluded=self.query(session_id='long',message_index=0)
        self.assertEqual(excluded['sources'][0]['items'][0]['evidence'],[])
    def test_unreadable_and_query_budget_are_not_empty_work(self):
        from unittest.mock import patch
        import sqlite3
        self.query(refresh=True)
        original=Path.open
        def denied(path,*args,**kwargs):
            if path.parent==self.src:raise PermissionError('fixture denial')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',denied):
            r=self.query()
        self.assertTrue(r['partial']);self.assertFalse(r['sources'][0]['coverage']['readable'])
        budget = sqlite3.OperationalError('interrupted')
        budget.query_budget_exceeded = True
        with patch('history_core.activity.query_budget',side_effect=budget):
            r=self.query()
        self.assertTrue(r['partial']);self.assertEqual(r['sources'][0]['coverage']['reason'],'query_budget_exceeded')
        self.assertTrue(r['sources'][0]['coverage']['indexed'])
        self.assertEqual(r['receipt']['covered_sources'],[])
        self.assertEqual(r['receipt']['failed_sources'],['codex'])
        self.assertEqual(r['receipt']['reasons'], [{'source':'codex','reason':'query_budget_exceeded'}])
        self.assertTrue(r['receipt']['partial'])
    def test_revision_change_during_formatting_rejects_whole_source_page(self):
        from unittest.mock import patch
        import sqlite3
        from history_core.evidence import redact_text
        self.query(refresh=True);changed=[]
        db=next(self.cache.glob('machine-codex-*/index.sqlite'))
        def mutate(text):
            if not changed:
                conn=sqlite3.connect(db)
                conn.execute("UPDATE reader_state SET value='concurrent-change' WHERE key='revision'")
                conn.commit();conn.close();changed.append(True)
            return redact_text(text)
        with patch('history_core.activity.redact_text',side_effect=mutate):r=self.query()
        self.assertTrue(r['partial']);self.assertEqual(r['sources'][0]['items'],[])
        self.assertIn('revision_changed',r['sources'][0]['coverage']['reason'])


class ActivityReadableTimeTests(unittest.TestCase):
    setUp = ActivityTests.setUp
    query = ActivityTests.query

    def test_activity_exposes_window_and_message_local_times(self):
        report=self.query(refresh=True)
        self.assertEqual(report['window']['start_local'],'2026-09-27T00:00:00.000+08:00')
        self.assertEqual(report['window']['end_local'],'2026-09-28T00:00:00.000+08:00')
        edge=next(x for x in report['sources'][0]['items'] if x['session_id']=='edge')['evidence'][0]
        self.assertEqual(edge['timestamp_local'],'2026-09-27T23:59:59.500+08:00')
        from datetime import datetime
        self.assertEqual(int(datetime.fromisoformat(edge['timestamp_local']).timestamp()*1000),edge['timestamp_ms'])

    def test_known_report_timestamps_do_not_move_to_next_day(self):
        from history_core.activity import local_timestamp
        self.assertEqual(local_timestamp(1790691974272,'Asia/Shanghai'),'2026-09-29T22:26:14.272+08:00')
        self.assertEqual(local_timestamp(1790694630633,'Asia/Shanghai'),'2026-09-29T23:10:30.633+08:00')
        self.assertEqual(local_timestamp(1790691974272,'UTC'),'2026-09-29T14:26:14.272+00:00')

    def test_dst_repeated_hour_is_distinguished_by_offset(self):
        from history_core.activity import local_timestamp, window_metadata
        from datetime import datetime
        times=['2026-11-01T05:30:00+00:00','2026-11-01T06:30:00+00:00']
        local=[local_timestamp(int(datetime.fromisoformat(t).timestamp()*1000),'America/New_York') for t in times]
        self.assertEqual(local,['2026-11-01T01:30:00.000-04:00','2026-11-01T01:30:00.000-05:00'])
        a,b=window('2026-03-08','America/New_York');metadata=window_metadata(a,b,'America/New_York')
        self.assertEqual(metadata['start_local'],'2026-03-08T00:00:00.000-05:00')
        self.assertEqual(metadata['end_local'],'2026-03-09T00:00:00.000-04:00')
