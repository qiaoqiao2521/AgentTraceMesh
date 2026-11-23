"""Derive display titles from user requests without changing transcript text."""
import re


_AMBIENT_TAG = re.compile(
    r'<(?P<tag>external_[\w.-]+|environment_context|app[-_]context|INSTRUCTIONS|'
    r'permissions instructions|skills_instructions|user_instructions|developer_instructions|'
    r'system_instructions|system[-_]reminder|collaboration_mode|turn_aborted|memory)\b[^>]*>', re.I)
_REQUEST = re.compile(
    r'^(?:#{1,6}\s*)?(?:My request(?: for Codex)?|我的请求|我的需求|用户请求|用户需求|请求|需求)\s*[:：]\s*',
    re.I | re.M)
_DEFAULT_TITLES = frozenset(('untitled', 'untitled conversation', 'new conversation', 'new chat'))


def is_ambient_title(title):
    """Recognize injected context prefixes, including truncated XML titles."""
    text = str(title or '').strip()
    return (bool(_AMBIENT_TAG.match(text))
            or bool(_REQUEST.fullmatch(text))
            or text.startswith(('# AGENTS.md instructions for ', '# Context from my IDE setup:',
                                'Environment context:', '[image]')))


def is_context_title(title):
    """Also recognize empty/default metadata, without rejecting normal prose."""
    text = str(title or '').strip()
    return (not text or text.casefold() in _DEFAULT_TITLES
            or not any(char.isalpha() for char in text)
            or bool(re.match(r'^</?[\w.-]+(?:[\s>/]|$)', text))
            or is_ambient_title(text))


def meaningful_user_title(text, limit=80):
    """Use the first request line outside known, fully closed harness wrappers.

    Unknown or incomplete XML is not interpreted as a request. Only a leading
    wrapper is removed, so quoted examples later in a real request stay intact.
    """
    if not isinstance(text, str):
        return None
    remaining = text.strip()
    while remaining:
        if remaining.startswith('# AGENTS.md instructions for '):
            _, separator, remaining = remaining.partition('\n')
            remaining = remaining.lstrip()
            if not separator or not re.match(r'<INSTRUCTIONS\b', remaining, re.I):
                return None
        if remaining.startswith('# Context from my IDE setup:'):
            request = _REQUEST.search(remaining)
            if not request:
                return None
            remaining = remaining[request.end():].lstrip()
            continue
        wrapper = _AMBIENT_TAG.match(remaining)
        if wrapper:
            if wrapper.group().endswith('/>'):
                remaining = remaining[wrapper.end():].lstrip()
                continue
            closing = re.search(r'</' + re.escape(wrapper.group('tag')) + r'\s*>',
                                remaining[wrapper.end():], re.I)
            if not closing:
                return None
            remaining = remaining[wrapper.end() + closing.end():].lstrip()
            continue
        request = _REQUEST.match(remaining)
        if request:
            remaining = remaining[request.end():].lstrip()
            continue
        first = remaining.splitlines()[0].strip()
        return None if is_context_title(first) else first[:limit]
    return None
