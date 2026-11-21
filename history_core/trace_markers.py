"""Narrow recorded markers, without inferring successful loading or effects.

Callers must pass the original native role, prompt and tool input. A request or
agent declaration is evidence of that event only; it does not establish that a
skill ran, a plugin was installed, or the completed work benefited from it.
"""

import hashlib
import json
import re

from .evidence import redact_text

LEARNING_OUTPUT_STYLE_PREFIX = (
    "You are in 'learning' output style mode, which combines interactive "
    "learning with educational explanations."
)
DECLARED_MARKER_PREFIX = '[[tracemesh:skill-trigger]] '
DECLARATION_CHARS = 2048
FIELD_CHARS = 128
_DECLARED_FIELDS = {'name', 'skill', 'path', 'version', 'content_hash'}
_SHA256 = re.compile(r'[0-9a-fA-F]{64}\Z')


def _text_field(marker, key, value):
    """Redact complete values before clipping, so clipping cannot split secrets."""
    safe = redact_text(value)
    marker[key] = safe[:FIELD_CHARS]
    marker[key + '_truncated'] = len(safe) > FIELD_CHARS


def _skill_marker(name, event, origin, *, path='unknown', version='unknown'):
    marker = {'kind': 'skill', 'event': event, 'evidence_origin': origin}
    for key, value in (('name', name), ('path', path), ('version', version)):
        _text_field(marker, key, value)
    return marker


def _nonempty_text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def style_markers(role, text):
    """Recognise the installed learning prompt in native system/developer text."""
    if role not in ('system', 'developer') or not isinstance(text, str):
        return []
    if not text.strip().startswith(LEARNING_OUTPUT_STYLE_PREFIX):
        return []
    return [{'kind': 'output_style', 'name': 'learning-output-style',
             'event': 'context_injected', 'evidence_origin': 'recorded_prompt',
             'content_hash': hashlib.sha256(text.encode('utf-8')).hexdigest()}]


def skill_tool_markers(tool_name, raw_input):
    """Record native Skill requests; ordinary reads and mentions are not calls."""
    if not isinstance(tool_name, str) or tool_name.casefold() != 'skill':
        return []
    if isinstance(raw_input, str):
        try:
            raw_input = json.loads(raw_input)
        except (TypeError, ValueError, RecursionError):
            return []
    if not isinstance(raw_input, dict):
        return []
    name = _nonempty_text(raw_input.get('skill'))
    if not name:
        return []
    return [_skill_marker(name, 'invocation_requested', 'native_tool_call')]


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_marker_field')
        result[key] = value
    return result


def declared_skill_markers(role, text):
    """Accept only a complete standalone assistant declaration, never examples."""
    if role != 'assistant' or not isinstance(text, str) or len(text) > DECLARATION_CHARS:
        return []
    body = text.strip()
    if not body.startswith(DECLARED_MARKER_PREFIX):
        return []
    try:
        value = json.loads(body[len(DECLARED_MARKER_PREFIX):], object_pairs_hook=_unique_fields)
    except (TypeError, ValueError, RecursionError):
        return []
    if not isinstance(value, dict) or not value.keys() <= _DECLARED_FIELDS:
        return []
    name = _nonempty_text(value.get('name'))
    skill = _nonempty_text(value.get('skill'))
    if ('name' in value and not name) or ('skill' in value and not skill):
        return []
    if not (name or skill) or (name and skill and name != skill):
        return []
    fields = {}
    for key in ('path', 'version'):
        fields[key] = _nonempty_text(value[key]) if key in value else 'unknown'
        if not fields[key]:
            return []
    content_hash = value.get('content_hash')
    if 'content_hash' in value and (
        not isinstance(content_hash, str) or not _SHA256.fullmatch(content_hash)
    ):
        return []
    marker = _skill_marker(name or skill, 'agent_declared', 'agent_marker', **fields)
    if content_hash is not None:
        marker['content_hash'] = content_hash.lower()
    return [marker]


def _skill_path(value, cwd):
    if not isinstance(value, str) or len(value) > 4096 or len(value.splitlines()) != 1 or any(c in value for c in "\r\n"):
        return None
    path = value.strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
        path = path[1:-1]
    # Lexical validation only. Never resolve a recorded path against today's disk.
    if not path or any(ord(c) < 32 or c in "\"'\\:;|&\x60$<>()*?{}!#" for c in path):
        return None
    parts = path.split('/')
    if '..' in parts or '//' in path or len(parts) < 3 or parts[-3] != 'skills' or parts[-1] != 'SKILL.md':
        return None
    if not re.fullmatch(r'[\w.+-]+', parts[-2]) or parts[-2] in ('.', '..'):
        return None
    # An absolute path may contain spaces. A relative path needs a recorded
    # working directory and must not be a command followed by an argument.
    if not path.startswith('/'):
        if not cwd or not cwd.startswith('/') or any(c.isspace() for c in path) or path.startswith(('~', '-')):
            return None
        path = cwd.rstrip('/') + '/' + path.removeprefix('./')
    elif re.search(r'\s+(?:/|--?|\./|[~$])', path) or re.match(r'^/(?:usr/)?bin/[^/ ]+\s', path):
        return None
    return path


def agy_skill_file_reads(record, line_no, cwd=''):
    """Yield indexed call numbers and whitelisted, transcript-located requests."""
    if record.get('source') != 'MODEL' or record.get('type') != 'PLANNER_RESPONSE':
        return
    calls = record.get('tool_calls')
    step = record.get('step_index')
    if not isinstance(calls, list) or type(step) is not int or step < 0:
        return
    for index, call in enumerate(calls):
        if not isinstance(call, dict) or call.get('name') not in ('read_file', 'view_file', 'default_api:read_file'):
            continue
        args = call.get('args')
        if isinstance(args, str) and len(args) <= 16384:
            try:
                args = json.loads(args, object_pairs_hook=_unique_fields)
            except (ValueError, RecursionError):
                continue
        if not isinstance(args, dict):
            continue
        paths = {key: _skill_path(args[key], cwd) for key in ('Path', 'AbsolutePath', 'path') if key in args}
        if not paths or None in paths.values() or len(set(paths.values())) != 1:
            continue
        key, path = next(iter(paths.items()))
        marker = _skill_marker(path.split('/')[-2], 'skill_file_read', 'transcript_tool_call', path=path)
        marker.update(raw_ref={'line_no': line_no, 'record_id': str(step),
                               'json_pointer': f'/tool_calls/{index}/args/{key}'},
                      tool_name=call['name'], request_status='requested')
        yield index, marker
