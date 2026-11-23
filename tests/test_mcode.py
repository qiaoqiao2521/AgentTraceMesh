import json
import unittest
from pathlib import Path
from tests import test_activity
from history_core import query_activity, HistoryReader
from history_core.mcode import parse_mcode_session_file


class McodeTests(unittest.TestCase):
    def setUp(self):
        test_activity.ActivityTests.setUp(self)
        self.mc=self.root/'mcode';self.folder=self.mc/'2026/09/27/session';self.folder.mkdir(parents=True)
        (self.folder/'manifest.json').write_text(json.dumps(dict(schemaVersion=1,layout='v2-final-dated-session',sessionId='m1',paths={'messages':'/DO/NOT/READ'},parentSessionId='parent')))
        self.log=self.folder/'messages.jsonl'
        self.rows=[
            dict(message_id='u1',turn_id='turn1',message=dict(role='user',timestamp=1790526000000,content=[dict(type='text',text='PUBLIC USER <system-reminder>HIDDEN REMINDER</system-reminder>')])),
            dict(message_id='a1',turn_id='turn1',message=dict(role='assistant',timestamp=1790526001000,content=[dict(type='thinking',thinking='PRIVATE THOUGHT'),dict(type='text',text='PUBLIC REPORT'),dict(type='toolCall',id='call1',name='test',arguments={'password':'hide-me'})])),
            dict(message_id='r1',turn_id='turn1',message=dict(role='toolResult',timestamp=1790526002000,toolCallId='call1',isError=True,content=[dict(type='text',text='PUBLIC FAILURE')])),
        ]
        # Explicit UTC: 2026-09-27 16:20 is Beijing 9/28 00:20.
        from datetime import datetime
        base=int(datetime.fromisoformat('2026-09-27T16:20:00+00:00').timestamp()*1000)
        for i,row in enumerate(self.rows):row['message']['timestamp']=base+i*1000
        self.write()
    def write(self):self.log.write_text('\n'.join(json.dumps(x) for x in self.rows)+'\n')
    def mcquery(self,**kwargs):
        return query_activity({'mcode':self.mc},date=kwargs.pop('date','2026-09-28'),timezone='Asia/Shanghai',data_dir=self.cache,**kwargs)
    def test_v2_public_contract_and_day(self):
        before = (self.log.read_bytes(), (self.folder/'manifest.json').read_bytes())
        result=self.mcquery(refresh=True);items=result['sources'][0]['items'];self.assertEqual(len(items),1)
        item=items[0];self.assertEqual(item['session_id'],'m1')
        ev=item['evidence'];self.assertEqual(len(ev),4)
        self.assertEqual([e['locator']['raw_ref']['line_no'] for e in ev],[1,2,2,3])
        self.assertEqual(ev[2]['locator']['tool_call_id'],ev[3]['locator']['tool_call_id'])
        self.assertTrue(ev[3]['locator']['tool_result_error'])
        text=json.dumps(result);self.assertNotIn('PRIVATE THOUGHT',text);self.assertNotIn('HIDDEN REMINDER',text);self.assertNotIn('hide-me',text)
        self.assertEqual(item['relation']['parent_session_id'],'parent')
        self.assertEqual(self.mcquery(date='2026-09-27')['sources'][0]['items'],[])
        with HistoryReader('mcode',self.mc,self.cache) as reader:
            self.assertEqual(reader.search(query='PUBLIC FAILURE')['items'][0]['id'],'m1')
        self.assertEqual(before, (self.log.read_bytes(), (self.folder/'manifest.json').read_bytes()))
    def test_bad_unknown_duplicate_and_append(self):
        self.rows.append(self.rows[0].copy());self.rows.append(dict(message_id='unknown',message=dict(role='assistant',timestamp=self.rows[0]['message']['timestamp'],content=[dict(type='future',data='SECRET')])));self.write()
        with self.log.open('a') as f:f.write('{bad\n')
        r=self.mcquery(refresh=True);self.assertTrue(r['partial'])
        w=r['sources'][0]['items'][0]['relation']['warnings'];self.assertEqual(w['malformed_lines'],1);self.assertEqual(w['unknown_blocks'],1);self.assertEqual(w['duplicate_messages'],1)
        self.rows.append(dict(message_id='u2',message=dict(role='user',timestamp=self.rows[0]['message']['timestamp']+5000,content=[dict(type='text',text='APPENDED')])));self.write()
        self.assertEqual(self.mcquery()['sources'][0]['coverage']['freshness'],'stale')
        r=self.mcquery(refresh=True);self.assertIn('APPENDED',json.dumps(r))
    def test_duplicate_identity_fails_closed(self):
        self.mcquery(refresh=True)
        self.rows.append(dict(self.rows[0],message=dict(self.rows[0]['message'],content=[dict(type='text',text='CONFLICT')])));self.write()
        r=self.mcquery(refresh=True);self.assertTrue(r['partial']);self.assertIn('conflicting_mcode',r['sources'][0]['coverage']['refresh_error'])
        self.assertNotIn('CONFLICT',json.dumps(r))
    def test_manifest_revision_and_link_rejected(self):
        first=self.mcquery(refresh=True)['sources'][0]['index_revision']
        manifest=self.folder/'manifest.json';obj=json.loads(manifest.read_text());obj['sessionId']='m2';manifest.write_text(json.dumps(obj))
        r=self.mcquery(refresh=True);self.assertNotEqual(first,r['sources'][0]['index_revision']);self.assertEqual(r['sources'][0]['items'][0]['session_id'],'m2')
        manifest.unlink();manifest.symlink_to(self.root/'outside')
        self.assertTrue(self.mcquery(refresh=True)['partial'])
    def test_two_recordings_same_manifest_identity(self):
        other=self.mc/'other';other.mkdir()
        (other/'manifest.json').write_text((self.folder/'manifest.json').read_text())
        (other/'messages.jsonl').write_text(self.log.read_text())
        r=self.mcquery(refresh=True)
        items=r['sources'][0]['items']
        self.assertTrue(r['partial']);self.assertEqual(len(items),2)
        self.assertNotIn('refresh_error',r['sources'][0]['coverage'])
        self.assertEqual(r['sources'][0]['coverage']['identity_conflicts'],1)
        identities={item['session_id'] for item in items}
        self.assertIn('m1',identities)
        self.assertEqual(len(identities),2)
