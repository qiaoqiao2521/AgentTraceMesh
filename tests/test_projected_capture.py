import hashlib
import json
import os
from unittest.mock import patch

import pytest

from history_core import HistoryReader
from history_core.capture import JsonlCapture, MAX_CAPTURE_LINE_BYTES
from history_core.projection import FIELD_BYTES
from history_core.trace_markers import DECLARED_MARKER_PREFIX


def record(kind, payload):
    return (json.dumps(dict(type=kind, timestamp='2026-11-20T01:00:00Z', payload=payload),
                       ensure_ascii=False) + '\r\n').encode()


def test_projection_keeps_outer_fields_and_raw_prefix_across_blocks(tmp_path):
    path = tmp_path / 'large.jsonl'
    name = DECLARED_MARKER_PREFIX + '{"name":"actual"}'
    img = 'A' * (MAX_CAPTURE_LINE_BYTES + 123)
    raw = record('response_item', {'image_url':'data:image/png;base64,' + img,
                 'type':'message', 'role':'assistant', 'content':[{'type':'output_text','text':name}]})
    raw += record('response_item', dict(type='custom_tool_call_output',call_id='pair',
                  output='quoted " ' + 'data:image/jpeg;base64,' + img + ' !suffix 你好'))
    raw += record('compacted', dict(replacement_history=[{'role':'assistant','text':name}]))
    raw += record('response_item', dict(type='function_call_output',output='''\x60\x60\x60\n''' + name + '''\n\x60\x60\x60'''))
    raw += record('response_item', {'data':img,'mime_type':'image/webp','type':'function_call',
                                    'name':'Skill','call_id':'pair','arguments':{'skill':'native'}})
    path.write_bytes(raw + b'\xe4')
    with JsonlCapture(path, capture_mode='projected') as capture:
        rows = [json.loads(line) for line in capture.projection.lines()]
        meta = capture.verify()
        assert meta['prefix_hash'] == hashlib.sha256(raw).hexdigest()
        assert (meta['captured_through'],meta['trailing_bytes'],meta['image_fields']) == (len(raw),1,3)
        assert meta['omitted_bytes'] == 3 * len(img)
        assert capture.projection.ref['byte_end'] == len(raw)
        assert name in str(rows[0]) and 'image/png' in str(rows[0])
        assert rows[1]['payload']['output'].endswith('!suffix 你好')
        assert rows[-1]['payload']['name'] == 'Skill'
        with path.open('r+b') as writer:
            writer.seek(raw.index(b'A' * 100) + 1000)
            writer.write(b'B')
        with pytest.raises(ValueError, match='capture_prefix_changed'):
            capture.verify()
    with pytest.raises(ValueError, match='capture_line_budget_exceeded'):
        with JsonlCapture(path):
            pass
    root = tmp_path/'source'
    root.mkdir()
    path.rename(root/path.name)
    with HistoryReader('codex', root, tmp_path/'cache') as reader:
        receipt = reader.refresh(capture_mode='projected')
        assert receipt['sessions'][0]['status'] == 'captured'
        rows = reader.markers(capture_mode='projected')['items']
        assert [(r['name'], r['raw_ref']['line_no']) for r in rows] == [('native',5),('actual',1)]
        assert rows[0]['call_id'] == 'pair'
        assert reader.markers()['items'] == []


def test_cache_isolation_idempotence_and_rollback(tmp_path):
    root = tmp_path/'source'
    root.mkdir()
    path = root/'small.jsonl'
    raw = record('session_meta',{'id':'sid'}) + record('response_item',dict(type='function_call',name='Skill',arguments={'skill':'before'}))
    path.write_bytes(raw)
    with HistoryReader('codex', root, tmp_path/'cache') as reader:
        reader.refresh()
        before = reader.markers()
        got = reader.refresh(capture_mode='projected')
        projected = reader.markers(capture_mode='projected')
        assert got['index_revision'] == projected['index_revision'] != before['index_revision']
        assert projected['items'] == before['items']
        assert reader.refresh(capture_mode='projected')['updated'] == 0
        assert reader.markers(capture_mode='projected')['index_revision'] == projected['index_revision']
        path.write_bytes(raw.replace(b'before', b'after!') + record('compacted',{'image':'A'*10000}))
        verify = JsonlCapture.verify
        def changed(capture):
            with path.open('r+b') as f:
                f.seek(-30,2)
                f.write(b'B')
            return verify(capture)
        with patch.object(JsonlCapture,'verify',changed):
            result = reader.refresh(capture_mode='projected')
        assert result['sessions'][0]['error'] == 'capture_prefix_changed'
        assert reader.markers(capture_mode='projected')['items'] == before['items']
        assert reader.markers()['index_revision'] == before['index_revision']
        assert reader.refresh(capture_mode='projected',max_bytes=42)['failed'] == 1


def test_projected_append_and_failed_neighbor_preserve_verified_prefix(tmp_path):
    root = tmp_path/'source'
    root.mkdir()
    path = root/'good.jsonl'
    raw = record('session_meta', {'id': 'valid'})
    path.write_bytes(raw)
    with JsonlCapture(path) as strict, JsonlCapture(path, capture_mode='projected') as projected:
        with path.open('ab') as writer:
            writer.write(record('response_item', dict(type='function_call', name='Skill', arguments={'skill':'next'})))
        for capture in (strict, projected):
            meta = capture.verify()
            assert meta['partial'] and meta['captured_through'] == len(raw)
            assert meta['prefix_hash'] == hashlib.sha256(raw).hexdigest()
    (root/'broken.jsonl').write_bytes(b'no LF')
    with HistoryReader('codex', root, tmp_path/'cache', capture_mode='projected') as reader:
        result = reader.refresh()
        assert result['updated'] == result['failed'] == 1
        assert next(x for x in result['sessions'] if x['path']=='broken.jsonl')['error'] == 'capture_no_complete_lines'
        assert reader.markers()['items'][0]['name'] == 'next'


def test_split_escapes_uri_headers_long_text_and_damage_are_local(tmp_path):
    path = tmp_path/'split.jsonl'
    uri = 'data:image/png;base64,' + 'Q/'*4000
    line = json.dumps({'embedded':'开头\\"' + uri + ' middle ' + uri + '"尾',
                       'long':'x'*(FIELD_BYTES+15),'sibling':'kept'},ensure_ascii=False).replace('/', r'\/')
    path.write_bytes((line+'\n'+'['*135+'0'+']'*135+'\n'+json.dumps({'id':'next'})+'\n').encode())
    with patch('history_core.projection.BLOCK_BYTES',17), JsonlCapture(path,capture_mode='projected') as cap:
        lines = list(cap.projection.lines())
        obj = json.loads(lines[0])
        assert obj['embedded'].startswith('开头\\"') and obj['embedded'].endswith('"尾')
        assert obj['sibling'] == 'kept' and 'middle' in obj['embedded']
        meta = cap.verify()
        assert (meta['image_fields'],meta['lossy_fields'],meta['skipped_lines']) == (2,1,1)
        assert meta['index_coverage'] == 'partial' and json.loads(lines[2]) == {'id':'next'}
    path.write_bytes(b'no lf')
    with pytest.raises(ValueError,match='capture_no_complete_lines'):
        with JsonlCapture(path,capture_mode='projected'):
            pass


def test_generic_data_requires_its_own_image_discriminator(tmp_path):
    path = tmp_path/'data.jsonl'
    large = 'A' * (FIELD_BYTES + 10)
    short = 'x' * 5000
    fields = [dict(data=large, media_type='image/jpeg'), dict(data=large, type='text'), dict(data=short)]
    path.write_text(json.dumps({'content': fields}) + '\n')
    with JsonlCapture(path, capture_mode='projected') as cap:
        meta = cap.verify()
        row = json.loads(next(cap.projection.lines()))
        assert (meta['image_fields'], meta['lossy_fields'], meta['index_coverage']) == (1, 1, 'partial')
        assert row['content'][-1]['data'] == short


@pytest.mark.parametrize('change,code', [('replace','source_replaced'),('truncate','source_truncated'),('touch','nonappend_change')])
def test_verify_identity_and_stat(tmp_path, change, code):
    path = tmp_path/'one.jsonl'
    path.write_bytes(b'{}\n')
    with JsonlCapture(path,capture_mode='projected') as cap:
        if change == 'replace':
            replacement = tmp_path/'copy'
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
        elif change == 'truncate':
            path.write_bytes(b'')
        else:
            stamp = path.stat().st_mtime_ns - 1_000_000_000
            os.utime(path,ns=(stamp,stamp))
        with pytest.raises(ValueError,match=code):
            cap.verify()


def test_bad_escape_in_omitted_field_does_not_publish_marker(tmp_path):
    root = tmp_path/'source'
    root.mkdir()
    marker = DECLARED_MARKER_PREFIX + '{"name":"sample"}'
    valid = record('response_item',dict(type='message',role='assistant',content=[{'text':marker}]))
    invalid = valid.replace(b'"type": "message"', b'"bad":"' + b'A'*(FIELD_BYTES+4096) + b'\\q","type":"message"')
    (root/'file.jsonl').write_bytes(invalid + valid)
    with HistoryReader('codex',root,tmp_path/'cache',capture_mode='projected') as reader:
        capture = reader.refresh()['sessions'][0]['source_capture']
        assert capture['skipped_lines'] == 1
        assert [i['raw_ref']['line_no'] for i in reader.markers()['items']] == [2]


def test_audit_folds_late_commands_after_large_projected_messages(tmp_path):
    root = tmp_path/'source'
    root.mkdir()
    msg = record('response_item',dict(type='message',role='user',content=[{'text':'y'*16000}]))
    last = record('response_item',dict(type='function_call',name='Bash',call_id='test',arguments={'command':'pytest'}))
    (root/'audit.jsonl').write_bytes(msg*320 + last)
    import sqlite3
    with HistoryReader('codex',root,tmp_path/'cache',capture_mode='projected') as reader:
        receipt = reader.refresh()
        assert receipt['sessions'][0]['source_capture']['audit_coverage'] == 'projected_records'
        with sqlite3.connect(receipt['cache_path']) as conn:
            assert json.loads(conn.execute('SELECT tool_summary_json FROM sessions').fetchone()[0]) == {'bash':1}
