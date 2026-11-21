import json
import os
import subprocess
import sys

import pytest

from history_core import HistoryReader
from history_core.omni import omni_markers, discover_omni_stores
from history_core.trace_markers import DECLARED_MARKER_PREFIX, LEARNING_OUTPUT_STYLE_PREFIX
from test_agy_transcript_markers import ide_store, planner, call

START = '2026-11-20T00:00:00Z'
MID = '2026-11-20T00:01:00Z'
LATER = '2026-11-20T00:02:00Z'
END = '2026-11-21T00:00:00Z'
WINDOW = dict(start=START,end=END)


def response(role, text, ts):
    return dict(type='response_item',timestamp=ts,payload=dict(type='message',role=role,content=[{'type':'output_text','text':text}]))


def codex(root, name, sid, payloads):
    root.mkdir(exist_ok=True,parents=True)
    rows = [dict(type='session_meta',timestamp='2026-09-01T00:00:00Z',payload={'id':sid,'cwd':'/fixture'}),*payloads]
    path = root/name
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    return path


def agy_user(step,ts,text):
    return dict(step_index=step,source='USER_EXPLICIT',type='USER_INPUT',created_at=ts,
                content='<USER_REQUEST>'+text+'</USER_REQUEST><ADDITIONAL_METADATA>请使用 agy-slot</ADDITIONAL_METADATA>')


def test_physical_roots_zero_misses_branches_windows_and_pages(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path))
    monkeypatch.setenv('XDG_CACHE_HOME',str(tmp_path/'cache'))
    monkeypatch.setenv('XDG_DATA_HOME',str(tmp_path/'data'))
    cache = tmp_path/'cache/agenttracemesh/reader'
    roots = [tmp_path/'.codex-slots'/slot/'codex/sessions' for slot in ('main','second')]
    (tmp_path/'.codex').mkdir()
    (tmp_path/'.codex/sessions').symlink_to(roots[1])
    (tmp_path/'.codex-slots/_shared/codex/sessions').mkdir(parents=True)
    old = '2026-11-19T23:59:59Z'
    declared = DECLARED_MARKER_PREFIX + '{"name":"demo"}'
    chat = [response('assistant',declared,old),response('user','start',START),
            response('developer',LEARNING_OUTPUT_STYLE_PREFIX,MID),response('assistant',declared,MID),
            response('assistant',declared,END)]
    for root in roots:
        codex(root,'a.jsonl','family',chat)
        if root == roots[0]:
            codex(root,'b.jsonl','family',chat)
            codex(root,'zero.jsonl','zero',[response('user','请使用 blender-mesh-xray 检查改模',MID)])
            p = codex(root,'partial.jsonl','partial',[response('assistant',declared,MID)])
            with p.open('ab') as f:
                f.write(b'{')
        with HistoryReader('codex',root,cache) as reader:
            reader.refresh()
    for slot in ('a','b'):
        ide = tmp_path/'.agy-slots'/slot/'gemini/antigravity'
        db,log = ide_store(ide)
        rows = [agy_user(0,START,'请检查改模'),agy_user(52,MID,'为什么没有用 blender-mesh-xray？'),
                planner(53,MID,call(path='/skills/blender-mesh-xray/SKILL.md')),
                planner(54,LATER,call(path='/skills/blender-mesh-xray/SKILL.md')),
                planner(55,END,call())]
        log.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        revision_ns = 1_700_000_000_000_000_000 + (100_000_000 if slot == 'b' else 0)
        os.utime(log, ns=(revision_ns, revision_ns))
        with HistoryReader('antigravity',db,cache) as reader:
            reader.refresh()
    fresh_root = tmp_path/'.codex-slots/new/codex/sessions'
    codex(fresh_root,'unread.jsonl','new',[response('user','hello',MID)])
    stores = discover_omni_stores(marker_stores=True)
    assert not any(s['slot']=='_shared' for s in stores)
    assert next(s for s in stores if s['slot']=='new')['cache_status']=='not_refreshed'
    assert 'active' in next(s for s in stores if s['slot']=='second')['slot_aliases']
    res = omni_markers(**WINDOW,review_misses='studio',limit=1)
    summary = res['summary']
    assert (summary['known_active_sessions'],summary['sessions_with_event'],summary['observable_active_sessions']) == (4,2,3)
    assert res['by_event'] == {'agent_declared':2,'skill_file_read':2}
    assert res['event_coverage']['invocation_requested']['sessions_with_event'] == 0
    assert res['event_coverage']['skill_file_read']['observable_active_sessions'] == 1
    assert res['output_style'] == {'context_injected':1}
    assert res['next_offset'] == 1 and len(res['items']) == 1
    agy = [r for r in res['review_misses']['items'] if r['skill']=='blender-mesh-xray' and r['source']=='antigravity']
    assert len(agy)==1 and agy[0]['status']=='late' and len(agy[0]['copies'])==2
    assert agy[0]['reminder']['raw_ref']['record_id']=='52' and agy[0]['first_event']['raw_ref']['record_id']=='53'
    assert any(r['status']=='expected_unseen' and r['session_id']=='zero' for r in res['review_misses']['items'])
    family = next(f for f in res['recordings'] if f['native_session_id']=='family')
    assert len(family['recordings'])==3 and family['relationship']=='same_prefix'
    assert any('@recording-' in r['session_id'] for r in family['recordings'])
    ide_family = next(f for f in res['recordings'] if f['source']=='antigravity')
    assert ide_family['representative_store'] == next(r['store_key'] for r in ide_family['recordings'] if r['slot']=='b')
    assert omni_markers(**WINDOW,slots='active')['summary']['total_events']==1
    assert omni_markers(**WINDOW,event='skill_file_read')['summary']['observable_active_sessions']==1
    assert omni_markers(**WINDOW,kind='output_style')['summary']['total_events']==1
    assert omni_markers(**WINDOW,offset=1,limit=1,index_revision=res['index_revision'])['items'] != res['items']
    updated = omni_markers(**WINDOW,sources='codex',slots='new',refresh=True)
    assert updated['summary']['observable_active_sessions']==1
    with pytest.raises(ValueError,match='index_revision_changed'):
        omni_markers(**WINDOW,index_revision=res['index_revision'],offset=1)
    cli = subprocess.run([sys.executable,'-m','history_core','omni','markers','--from',START,'--to',END,
                          '--sources','antigravity','--group-by','skill,source','--event','skill_file_read'],
                         check=True,capture_output=True,text=True)
    assert json.loads(cli.stdout)['groups'][0]['total_events']==2


def test_counts_beyond_first_page_and_prior_marker_prevents_false_late(tmp_path):
    cache = tmp_path/'cache'
    name = 'blender-mesh-xray'
    marker = DECLARED_MARKER_PREFIX + json.dumps({'name':name})
    rows = [response('assistant',marker,'2026-11-19T01:00:00Z'),response('user','为什么没有用 blender-mesh-xray',START)]
    rows += [response('assistant',marker,MID) for _ in range(111)]
    root=tmp_path/'source'
    codex(root,'many.jsonl','many',rows)
    with HistoryReader('codex',root,cache) as reader:
        reader.refresh()
    r = omni_markers(**WINDOW,data_dir=cache,review_misses='studio',group_by='skill,source',limit=4)
    assert len(r['items']) == 4 and r['groups'][0]['total_events']==111
    assert r['review_misses']['counts']=={'timely':1}


def test_divergent_copies_keep_review_evidence_on_its_own_branch(tmp_path):
    cache=tmp_path/'reader'
    text='请使用 blender-mesh-xray'
    for slot in ('left','right'):
        root=tmp_path/'.codex-slots'/slot/'codex/sessions'
        marker = DECLARED_MARKER_PREFIX + json.dumps({'name':'blender-mesh-xray' if slot == 'left' else 'x'*17})
        codex(root,'fork.jsonl','same',[response('user',text,START),response('assistant',marker,MID)])
        with HistoryReader('codex',root,cache) as r:
            r.refresh()
    res=omni_markers(**WINDOW,data_dir=cache,review_misses='studio')
    assert res['summary']['observable_active_sessions']==1
    assert res['recordings'][0]['relationship']=='divergent'
    assert {i['status'] for i in res['review_misses']['items']} == {'timely','expected_unseen'}
    assert res['review_misses']['counts'] == {'unknown':1}
    assert res['review_misses']['disputed_opportunities'] == 1


def test_proven_timestamp_fallback_and_failed_capture_status(tmp_path):
    root,cache = tmp_path/'source',tmp_path/'reader'
    marker=DECLARED_MARKER_PREFIX + '{"name":"clock"}'
    codex(root,'clock.jsonl','clock',[response('assistant',marker,MID)])
    with HistoryReader('codex',root,cache) as r:
        r.refresh()
        db=r._HistoryReader__indexer.conn
        db.execute('UPDATE messages SET activity_ts_ms=NULL')
        db.commit()
        r.refresh(paths=['clock.jsonl'],max_bytes=1)
    result=omni_markers(**WINDOW,data_dir=cache)
    assert result['items'][0]['time_basis']=='recorded_ts_ms'
    assert result['stores'][0]['capture_errors']=={'clock.jsonl':'capture_budget_exceeded'}
    assert result['summary']['sessions_with_observed_evidence']==1
    assert result['summary']['observable_active_sessions']==0 and result['summary']['rate'] is None


def test_untimed_skill_cannot_turn_a_request_into_a_miss(tmp_path):
    root, cache = tmp_path/'ide', tmp_path/'reader'
    db, log = ide_store(root)
    rows = [agy_user(1, START, '请使用 blender-mesh-xray'),
            planner(2, None, call(path='/skills/blender-mesh-xray/SKILL.md'))]
    log.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    with HistoryReader('antigravity', db, cache) as reader:
        reader.refresh()
    r = omni_markers(**WINDOW, data_dir=cache, review_misses='studio')
    assert r['stores'][0]['unknown_time_events'] == 1
    assert r['summary']['total_events'] == 0 and r['summary']['rate'] is None
    assert r['review_misses']['counts'] == {'unknown': 1}
