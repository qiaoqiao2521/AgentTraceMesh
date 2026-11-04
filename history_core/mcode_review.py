"""Bounded, revision-bound public mcode messages for review handoffs."""
import hashlib
import json
import os
import stat
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from audit import build_audit_from_events
from audit.schema import AuditEvent
from .mcode import mcode_file_signature, parse_mcode_session_bytes


MAX_MESSAGES_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024


def _ordered_messages(parsed):
    # The cache uses inferred start time only for reader order; raw missing
    # timestamps are still absent from activity evidence.
    messages = sorted(parsed['messages'],
                      key=lambda msg: int(msg['ts_ms'] or parsed['start_ts_ms']))
    return [dict(msg, ts_ms=int(msg['ts_ms'] or parsed['start_ts_ms']), message_index=index)
            for index, msg in enumerate(messages)]


def build_mcode_audit(parsed):
    """Use the parser's public blocks, never a guessed Codex envelope."""
    events = []
    for message in _ordered_messages(parsed):
        kind = message['kind']
        ref = dict(message.get('raw_ref') or {})
        events.append(AuditEvent(
            ts_ms=message['ts_ms'], role=message['role'], kind=kind,
            text=message['text'] if kind == 'message' else '',
            tool_name=message.get('tool_name'), tool_args=message.get('tool_args'),
            tool_result_text=message['text'] if kind == 'tool_result' else '',
            tool_result_error=message.get('tool_result_error'),
            line_no=ref.get('line_no'), message_index=message['message_index'], raw_ref=ref,
        ))
    warnings = parsed.get('activity_metadata', {}).get('warnings', {})
    return build_audit_from_events(
        events, session_id=parsed['id'], source='mcode',
        started_at=parsed['start_ts_ms'], ended_at=parsed['end_ts_ms'],
        parse_errors=warnings.get('malformed_lines', 0) + warnings.get('unknown_blocks', 0),
    )


def _selected_path(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    if path.name != 'messages.jsonl' or root not in path.parents:
        raise ValueError('cached_source_path_outside_selection')
    for selected in (path, path.parent / 'manifest.json'):
        current = selected
        while True:
            if current.is_symlink():
                raise ValueError('source_symlink_not_allowed')
            if current == root:
                break
            current = current.parent
        if root.resolve(strict=True) not in selected.resolve(strict=True).parents:
            raise ValueError('cached_source_path_outside_selection')
    return path


def _stat_key(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _open_selected(stack, path):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0))
    stream = stack.enter_context(os.fdopen(descriptor, 'rb'))
    before = os.fstat(stream.fileno())
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('source_regular_file_required')
    return stream, before


def _read_bounded(stream, before, limit):
    if before.st_size > limit:
        raise ValueError('handoff_source_limit_exceeded')
    content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError('handoff_source_limit_exceeded')
    return content


def bind_mcode_recording_identity(indexer, session_id, parsed, path, identity_json):
    """Bind a parsed native ID to the selected path's exact recording alias."""
    native_id = parsed['id']
    if native_id == str(session_id):
        return native_id
    try:
        identity = json.loads(identity_json or '{}')
    except (TypeError, ValueError):
        identity = {}
    if (not isinstance(identity, dict) or identity.get('identity_conflict') is not True
            or identity.get('native_session_id') != native_id
            or str(session_id) != indexer._recording_alias(native_id, path)):
        raise ValueError('source_changed_since_index: recording identity mismatch')
    parsed['id'] = str(session_id)
    return native_id


def mcode_review_snapshot(indexer, session_id):
    """Capture two authority files and verify their public cache projection.

    Raw bytes are only hashed and parsed locally; they never leave this API.
    A complete source snapshot is required, so no prefix can masquerade as all
    original requirements. Source files are opened read-only.
    """
    if getattr(indexer, 'source', None) != 'mcode' or getattr(indexer, 'sessions_dir', None) is None:
        raise ValueError('mcode_source_required')
    with indexer.lock:
        row = indexer.conn.execute('SELECT * FROM sessions WHERE id=?', (str(session_id),)).fetchone()
    if row is None:
        raise ValueError('selection_session_unavailable')
    signature = row['file_signature']
    if not signature:
        raise ValueError('selection_message_binding_unsupported: refresh required')
    path = _selected_path(indexer.sessions_dir, row['file_path'])
    manifest = path.parent / 'manifest.json'
    with ExitStack() as stack:
        messages_stream, before = _open_selected(stack, path)
        manifest_stream, manifest_before = _open_selected(stack, manifest)
        if mcode_file_signature(before, manifest_before) != signature:
            raise ValueError('source_changed_since_index: refresh required')
        content = _read_bounded(messages_stream, before, MAX_MESSAGES_BYTES)
        manifest_content = _read_bounded(manifest_stream, manifest_before, MAX_MANIFEST_BYTES)
        parsed = parse_mcode_session_bytes(content, manifest_content, path)
        native_id = bind_mcode_recording_identity(indexer, session_id, parsed, path,
                                                 row['activity_metadata_json'])
        warnings = parsed.get('activity_metadata', {}).get('warnings', {})
        if warnings.get('malformed_lines') or warnings.get('unknown_blocks'):
            # A skipped record may contain a later requirement correction.
            # Activity can disclose partial coverage; review selection must not
            # silently present that projection as the full request candidate set.
            raise ValueError('selection_source_content_unsupported: incomplete mcode public records')
        messages = _ordered_messages(parsed)
        with indexer.lock:
            current = indexer.conn.execute('SELECT file_signature FROM sessions WHERE id=?',
                                          (str(session_id),)).fetchone()
            totals = indexer.conn.execute(
                'SELECT COUNT(*),COALESCE(SUM(LENGTH(text)),0) FROM messages WHERE session_id=?',
                (str(session_id),)).fetchone()
            if totals[0] != len(messages) or totals[1] > MAX_MESSAGES_BYTES:
                raise ValueError('selection_stale: cached messages differ from captured source')
            cached = indexer.conn.execute(
                'SELECT role,kind,ts_ms,text,SUBSTR(activity_meta_json,1,65537) AS activity_meta_json '
                'FROM messages WHERE session_id=? '
                'ORDER BY ts_ms ASC,id ASC', (str(session_id),)).fetchall()
        if current is None or current[0] != signature or len(cached) != len(messages):
            raise ValueError('selection_stale: index changed during read')
        for actual, public in zip(cached, messages):
            if any(actual[key] != public[key] for key in ('role', 'kind', 'ts_ms', 'text')):
                raise ValueError('selection_stale: cached messages differ from captured source')
            metadata = json.loads(actual['activity_meta_json'] or '{}')
            if metadata.get('raw_ref') != public.get('raw_ref'):
                raise ValueError('selection_stale: cached locator differs from captured source')
        for selected, stream, initial in ((path, messages_stream, before),
                                          (manifest, manifest_stream, manifest_before)):
            after = os.fstat(stream.fileno())
            current_stat = selected.stat(follow_symlinks=False)
            if (_stat_key(initial) != _stat_key(after)
                    or _stat_key(initial) != _stat_key(current_stat)):
                raise ValueError('source_changed_during_read: refresh required')
        _selected_path(indexer.sessions_dir, path)
    digest = hashlib.sha256(b'mcode-public-review-v1\0')
    for captured in (manifest_content, content):
        digest.update(len(captured).to_bytes(8, 'big'))
        digest.update(captured)
    revision = 'sha256:' + digest.hexdigest()
    provenance = {
        'status': 'captured', 'reason': None, 'source': 'mcode', 'session_id': str(session_id),
        'locator': {'path': str(path), 'manifest_path': str(manifest), 'format': 'jsonl',
                    'line_numbering': 'one_based', 'projection': 'public_blocks_only'},
        'content_revision': revision, 'context_revision': revision,
        'bytes_captured': len(content), 'manifest_bytes_captured': len(manifest_content),
        'byte_limit': MAX_MESSAGES_BYTES, 'manifest_byte_limit': MAX_MANIFEST_BYTES,
        'truncated': False, 'observed_at': datetime.now(timezone.utc).isoformat(),
        'parser_warnings': parsed.get('activity_metadata', {}).get('warnings', {}),
    }
    if native_id != str(session_id):
        provenance.update(native_session_id=native_id, identity_conflict=True)
    return {'messages': messages, 'audit': build_mcode_audit(parsed).to_dict(),
            'provenance': provenance}
