"""Shared case-first recall; only an explicit fallback opens a history source."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3

from .evidence import redact_text
from .problem_cases import recall_cases
from .providers import FILE_SOURCES
from .reader import HistoryReader


def recall(query, *, directory=None, project=None, limit=5, source=None,
           source_path=None, data_dir=None):
    """Reuse saved cases and one selected index, without refreshing or execution."""
    if bool(source) != bool(source_path):
        raise ValueError('recall_fallback_requires_source_and_source_path')
    result = recall_cases(directory, query=query, project=project, limit=limit)
    result.update(authorization='context_only', current_verification='not_performed')
    if result['items'] and not result.get('partial'):
        result['fallback_status'] = 'not_needed'
    elif not source:
        result['fallback_status'] = 'source_required'
        result['next_action'] = (
            'No complete case match. Supply --source and --source-path '
            'to search the existing index, or use search --brief; '
            'a case miss does not establish absence in history.')
    else:
        reader = None
        try:
            cache_dir = data_dir
            if cache_dir is None and source in (*FILE_SOURCES, 'agy', 'antigravity'):
                cache_dir = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'agenttracemesh' / 'reader'
            reader = HistoryReader(source, source_path, cache_dir, validate_tree=False)
            result['history'] = reader.search(query=query, cwd=project, limit=limit, brief=True)
            result['fallback_status'] = 'searched'
            result['partial'] = bool(result.get('partial') or result['history'].get('partial'))
        except (ValueError, OSError, sqlite3.Error) as exc:
            result['fallback_status'] = 'unavailable'
            result['partial'] = True
            result['history_error'] = {'error': type(exc).__name__, 'detail': redact_text(str(exc))}
        finally:
            if reader is not None:
                reader.close()
    return result
