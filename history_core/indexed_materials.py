"""Pure, bounded historical material from an already frozen derived index.

The caller supplies an indexed_read_view. No live source is opened or validated;
the binding covers selected cached records, never the complete source recording.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .evidence import MAX_BODY_CHARS, MAX_SELECTIONS, _markdown, _redact, _store_id
from .messages import BODY_PAGE_CHARS, METADATA_CHARS, read_message
from .providers import FILE_SOURCES
from .reuse import query_budget

MAX_JSON_BYTES = 64 * 1024
_HEX_REVISION = re.compile(r'[0-9a-f]{64}\Z')


def _object(raw):
    try:
        value = json.loads(raw or '{}')
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _source_capture(raw):
    value = _object(raw).get('source_capture')
    if not isinstance(value, dict):
        return {'status': 'unknown'}
    result = {}
    for key in ('captured_through', 'observed_size', 'trailing_bytes'):
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    for key in ('status', 'observed_at'):
        if isinstance(value.get(key), str) and len(value[key]) <= 128:
            result[key] = _redact(value[key])
    if isinstance(value.get('prefix_hash'), str) and _HEX_REVISION.fullmatch(value['prefix_hash']):
        result['prefix_hash'] = value['prefix_hash']
    if type(value.get('partial')) is bool:
        result['partial'] = value['partial']
    result.setdefault('status', 'unknown')
    return result


def _raw_ref(raw):
    value = _object(raw).get('raw_ref')
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ('line_no', 'byte_offset', 'byte_start', 'byte_end'):
        if type(value.get(key)) is int and value[key] >= (1 if key == 'line_no' else 0):
            result[key] = value[key]
    for key, maximum in (('record_id', 128), ('json_pointer', 512)):
        if isinstance(value.get(key), str) and len(value[key]) <= maximum:
            result[key] = _redact(value[key])
    return result


def _session(indexer, session_id):
    with query_budget(indexer) as conn:
        row = conn.execute(
            'SELECT file_path,SUBSTR(activity_metadata_json,1,?) AS metadata '
            'FROM sessions WHERE id=?', (METADATA_CHARS, session_id)).fetchone()
    if row is None:
        raise ValueError('session_not_found')
    root, path = Path(indexer.sessions_dir), Path(row['file_path'])
    # A frozen cache path is data, not permission to read outside its store.
    # Do not resolve/stat it: the live recording may have since disappeared.
    if (not root.is_absolute() or not path.is_absolute() or '..' in path.parts
            or root not in path.parents):
        raise ValueError('cached_source_path_outside_selection')
    return path, _source_capture(row['metadata'])


def _message_raw_ref(indexer, session_id, message_index):
    with query_budget(indexer) as conn:
        row = conn.execute(
            'SELECT SUBSTR(activity_meta_json,1,?) FROM messages '
            'WHERE session_id=? ORDER BY ts_ms,id LIMIT 1 OFFSET ?',
            (METADATA_CHARS, session_id, message_index)).fetchone()
    return _raw_ref(row[0]) if row else {}


def _fragment_hash(item):
    identity = {key: item[key] for key in (
        'source', 'store_id', 'session_id', 'index_revision', 'message_index',
        'text_offset', 'text')}
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':')).encode('utf-8')
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def indexed_materials(indexer, session_id, selections, *, index_revision, system='local'):
    """Export indexed windows; keep the legacy namespace for direct low-level callers."""
    from .read_view import reader_revision
    if getattr(indexer, 'sessions_dir', None) is None or getattr(indexer, 'source', '') not in FILE_SOURCES:
        raise ValueError('materials_source_unsupported')
    if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
        raise ValueError('session_id_required')
    if not isinstance(index_revision, str) or not index_revision:
        raise ValueError('index_revision_required')
    if not isinstance(selections, list) or not 1 <= len(selections) <= MAX_SELECTIONS:
        raise ValueError('selection_count_limit: choose 1 to 5 fragments')
    requested, seen = [], set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) - {'message_index', 'text_offset'}:
            raise ValueError('materials_selection_invalid')
        message_index, offset = selection.get('message_index'), selection.get('text_offset', 0)
        if type(message_index) is not int or message_index < 0:
            raise ValueError('invalid_message_index')
        if type(offset) is not int or offset < 0:
            raise ValueError('invalid_text_offset')
        if (message_index, offset) in seen:
            raise ValueError('selection_duplicate')
        seen.add((message_index, offset))
        requested.append((message_index, offset))
    revision = reader_revision(indexer)
    if index_revision != revision:
        raise ValueError('index_revision_changed: restart search')
    path, capture = _session(indexer, session_id)
    source = indexer.source
    # Reader roots are canonical at selection; frozen exports must not resolve
    # a changed or missing live root again. The legacy local default stays exact.
    store = _store_id(system, source, indexer.sessions_dir)
    observed_at = datetime.now(timezone.utc).isoformat()
    items, body_chars = [], 0
    for message_index, offset in requested:
        message = read_message(indexer, session_id, message_index, offset)
        body_chars += len(message['text'])
        if body_chars > MAX_BODY_CHARS:
            raise ValueError('selection_body_limit_exceeded')
        locator = {'path': _redact(str(path)), 'message_index': message_index,
                   'role': message['role'], 'kind': message['kind'],
                   'timestamp_ms': message['ts_ms'],
                   'raw_ref': _message_raw_ref(indexer, session_id, message_index)}
        item = {key: message[key] for key in (
            'message_index', 'text', 'char_count', 'text_offset', 'text_offset_basis',
            'next_text_offset', 'has_more_before', 'has_more_after', 'call_id',
            'call_id_truncated')}
        item.update(system=system, source=source, store_id=store, session_id=session_id,
                    locator=locator, audit_event_index=None, evidence_id=None,
                    content_revision='unknown', context_revision='unknown',
                    observed_at=observed_at, index_revision=revision, binding='index_revision',
                    representation='indexed_message_fragment', redacted=True,
                    context_only=True, partial=bool(message['truncated'] or message['partial']))
        item['selected_fragment_hash'] = _fragment_hash(item)
        # Reuse the existing adaptive-fence renderer without changing its API.
        item['locator'].update(index_revision=revision,
                               selected_fragment_hash=item['selected_fragment_hash'],
                               text_offset=offset, next_text_offset=item['next_text_offset'],
                               partial=item['partial'], source_capture=capture)
        items.append(item)
    if reader_revision(indexer) != revision:
        raise ValueError('index_revision_changed: restart search')
    result = {'schema_version': 'history.indexed-materials.v1', 'authorization': 'context_only',
              'context_only': True, 'source': source, 'store_id': store, 'session_id': session_id,
              'index_revision': revision, 'binding': 'index_revision',
              'content_revision': 'unknown', 'context_revision': 'unknown',
              'selected_fragment_hash_basis': 'redacted_indexed_fragment',
              'coverage': 'selected_fragments_not_complete', 'partial': True,
              'source_capture': capture, 'freshness': 'unknown', 'observed_at': observed_at,
              'items': items, 'body_chars': body_chars, 'redacted': True,
              'limits': {'max_fragments': MAX_SELECTIONS, 'max_body_chars': MAX_BODY_CHARS,
                         'body_page_chars': BODY_PAGE_CHARS, 'max_json_bytes': MAX_JSON_BYTES},
              'markdown': ('Binding: index_revision. These are selected cached fragments; '
                           'complete-message and whole-session coverage are not claimed.\n\n'
                           + _markdown(items)),
              'redaction_notice': 'Common secret spans are masked; this is not complete anonymisation.'}
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > MAX_JSON_BYTES:
        raise ValueError('materials_packet_limit_exceeded')
    return result
