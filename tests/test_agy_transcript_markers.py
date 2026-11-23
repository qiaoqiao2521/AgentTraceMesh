import json
import sqlite3
from unittest.mock import patch

from history_core import HistoryReader
from history_core.capture import JsonlCapture
from history_core.trace_markers import agy_skill_file_reads


def ide_store(root,sid='cid'):
    root.mkdir(parents=True,exist_ok=True)
    db = root/'conversation_summaries.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS conversation_summaries(conversation_id TEXT PRIMARY KEY,title,step_count,last_modified_time,workspace_uris)')
        conn.execute('INSERT INTO conversation_summaries VALUES(?,?,1,?,?)',(sid,sid,'2026-11-20T00:00:00Z','["file:///project"]'))
    log = root/'brain'/sid/'.system_generated/logs/transcript.jsonl'
    log.parent.mkdir(parents=True)
    return db,log


def planner(step, ts, *calls):
    return dict(step_index=step,source='MODEL',type='PLANNER_RESPONSE',status='DONE',created_at=ts,tool_calls=list(calls))


def call(name='read_file',path='/project/.agents/skills/lens/SKILL.md',key='Path'):
    return {'name':name,'args':{key:path}}


def test_whitelist_path_grammar_and_individual_pointers():
    valid = call('view_file', '  "/folder with spaces/skills/one/SKILL.md"  ')
    wrapped = call('default_api:read_file','./skills/two/SKILL.md','AbsolutePath')
    wrapped['args'] = json.dumps(wrapped['args'])
    calls = [valid,call('read_file','/skills/three/SKILL.md','path'),wrapped]
    for path in ('https://site/skills/x/SKILL.md','/a/../skills/x/SKILL.md','/skills/x/SKILL.md.bak',
                 'cat /skills/x/SKILL.md','/bin/cat /skills/x/SKILL.md','/a\n/skills/x/SKILL.md','/skills/x/SKILL.md\n',
                 'see the file /skills/x/SKILL.md','""/skills/x/SKILL.md""','/wrongskills/x/SKILL.md'):
        calls.append(call(path=path))
    calls.extend([call('READ_FILE'),call('run_command'),{'name':'read_file','args':{'Path':'/skills/x/SKILL.md','path':'/skills/y/SKILL.md'}}])
    row = planner(53,'2026-11-20T00:00:00Z',*calls)
    got = [m for _,m in agy_skill_file_reads(row,54,'/project')]
    assert [m['name'] for m in got] == ['one','three','two']
    assert [m['raw_ref'] for m in got] == [
        {'record_id':'53','line_no':54,'json_pointer':f'/tool_calls/{i}/args/{key}'}
        for i,key in enumerate(['Path','path','AbsolutePath'])]
    assert got[2]['path'] == '/project/skills/two/SKILL.md'
    assert len(list(agy_skill_file_reads(row,54))) == 2
    assert not list(agy_skill_file_reads({**row,'source':'USER_EXPLICIT'},54))
    assert not list(agy_skill_file_reads({**row,'type':'TOOL_RESULT'},54))
    assert not list(agy_skill_file_reads({**row,'tool_calls':[],'content':str(calls)},54))


def test_cache_only_refresh_missing_and_transcript_only_revision(tmp_path):
    root = tmp_path/'ide'
    db,log = ide_store(root)
    cache = tmp_path/'cache'
    def query():
        with HistoryReader('antigravity',db,cache) as reader:
            return reader.markers(event='skill_file_read')
    assert query()['transcript_coverage']['status'] == 'not_refreshed'
    assert not cache.exists()
    with HistoryReader('antigravity',db,cache) as reader:
        reader.refresh()
        assert reader.markers()['transcript_coverage']['sessions'] == {'missing_transcript':1}
        row = planner(53,'2026-11-20T00:00:00Z',call(),call('view_file'))
        log.write_text(json.dumps(row)+'\n{"incomplete":')
        first = reader.refresh()
        got = reader.markers(event='skill_file_read')
        assert len(got['items']) == 2 and got['index_revision'] == first['index_revision']
        assert all(it['version']=='unknown' and it['message_index'] is None and it['request_status']=='requested' for it in got['items'])
        assert got['items'][1]['raw_ref']['json_pointer'] == '/tool_calls/1/args/Path'
        assert reader.markers(event='agent_declared')['count'] == 0
    with patch.object(JsonlCapture,'__enter__',side_effect=AssertionError('query scanned transcript')):
        assert query() == got
    log.write_text(json.dumps(row)+'\n'+json.dumps(planner(61,'2026-11-20T00:00:03Z',call()))+'\n')
    assert query() == got
    with HistoryReader('antigravity',db,cache) as reader:
        new = reader.refresh()
        assert new['index_revision'] != first['index_revision']
        assert reader.markers()['count'] == 3
        assert reader.refresh()['index_revision'] == new['index_revision']


def test_native_tool_pairing_failed_result_and_symlink_isolation(tmp_path):
    from test_agy import blob, step, metadata
    root = tmp_path/'ide'
    db,log = ide_store(root)
    (root/'conversations').mkdir()
    requested = call()
    native = ('id','read_file',requested['args'])
    tool = blob(1,'id') + blob(2,'read_file') + blob(3,json.dumps(requested['args']))
    with sqlite3.connect(root/'conversations/cid.db') as conn:
        conn.execute('CREATE TABLE steps(idx INTEGER PRIMARY KEY,step_type,status,step_format,step_payload)')
        conn.execute('INSERT INTO steps VALUES(53,15,3,0,?)',(step(15,blob(7,tool)),))
        conn.execute('INSERT INTO steps VALUES(54,132,7,0,?)',(step(132,blob(2,blob(1,'Permission denied')),status=7,meta=metadata(native)),))
    log.write_text(json.dumps(planner(53,'2026-11-20T01:02:03Z',requested))+'\n')
    cache=tmp_path/'reader'
    with HistoryReader('antigravity',db,cache) as reader:
        reader.refresh()
        got=reader.markers()['items']
        assert len(got)==1 and got[0]['message_index']==0 and got[0]['request_status']=='requested'
        log.parent.rename(tmp_path/'original')
        log.parent.symlink_to(tmp_path/'original',target_is_directory=True)
        assert reader.refresh()['transcript_coverage']=={'capture_error':1}
        assert reader.markers()['items']==got
