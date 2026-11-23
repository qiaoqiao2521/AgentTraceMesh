"""Source identities and file discovery shared by Web and machine readers."""
import os
from pathlib import Path

FILE_SOURCES = ('codex', 'claude', 'openclaw', 'codebuddy', 'gemini', 'pi', 'copilot', 'prime', 'mcode')
NATIVE_SOURCES = ('opencode', 'hermes', 'zcode', 'agy', 'antigravity')
SOURCES = FILE_SOURCES + NATIVE_SOURCES


def canonical_source(source):
    return {'cbc': 'codebuddy', 'codebuddy-code': 'codebuddy', 'prime-agent': 'prime'}.get(source, source)


def file_suffixes(source):
    return ('.json', '.jsonl') if source == 'gemini' else ('.jsonl',)


def include_file(source, path):
    if source == 'mcode':
        return path.name == 'messages.jsonl'
    if source in ('claude', 'codebuddy'):
        return not path.name.startswith('agent-')
    if source == 'openclaw':
        return path.parent.name == 'sessions'
    if source == 'gemini':
        if path.suffix == '.json' and path.with_suffix('.jsonl').is_file():
            return False  # Gemini retains the pre-migration JSON snapshot.
        return ((path.parent.name == 'chats' and path.name.startswith('session-'))
                or path.parent.parent.name == 'chats')
    if source == 'copilot':
        return path.name == 'events.jsonl'
    return True


def parser_for(source):
    # Local imports avoid a cycle: the indexer also consumes discovery metadata.
    from .sources import parse_codex_session_file, parse_claude_session_file, parse_openclaw_session_file
    parsers = {'codex': parse_codex_session_file, 'claude': parse_claude_session_file,
               'openclaw': parse_openclaw_session_file}
    if source == 'mcode':
        from .mcode import parse_mcode_session_file
        return parse_mcode_session_file
    if source == 'codebuddy':
        from .codebuddy import parse_codebuddy_session_file
        return parse_codebuddy_session_file
    if source in ('gemini', 'pi', 'prime'):
        from .extra_parsers import parse_gemini_session_file, parse_pi_session_file
        return parse_gemini_session_file if source == 'gemini' else parse_pi_session_file
    if source == 'copilot':
        from .copilot import parse_copilot_session_file
        return parse_copilot_session_file
    return parsers[source]


def parser_version(source):
    return 9 if source == 'codex' else 6 if source == 'claude' else 2 if source == 'mcode' else 1


def opencode_database(explicit=None):
    if explicit:
        return Path(explicit).expanduser()
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share').expanduser()
    return data / 'opencode/opencode.db'


def copilot_root():
    ordinary = Path.home() / '.copilot'
    snap = Path.home() / 'snap/copilot-cli/common/.copilot'
    return ordinary if (ordinary / 'session-state').is_dir() or not (snap / 'session-state').is_dir() else snap


def native_indexer(source, path, *, data_dir=None):
    from .sources import OpenCodeIndexer, HermesStateIndexer
    from .zcode import ZCodeIndexer
    if source in ('agy', 'antigravity'):
        from .agy import AgyIndexer
        return (AgyIndexer(path, source=source, data_dir=data_dir) if data_dir is not None
                else AgyIndexer(path, source=source))
    return {'opencode': OpenCodeIndexer, 'hermes': HermesStateIndexer, 'zcode': ZCodeIndexer}[source](path)


def uses_message_index(indexer):
    return getattr(indexer, 'sessions_dir', None) is not None or indexer.source in ('agy', 'antigravity')
