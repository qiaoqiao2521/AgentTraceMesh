"""Opt-in local context recovery. A plan alone never opens historical sources."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys

from .evidence import redact_text
from .providers import SOURCES, canonical_source


def read_packet(path, *, limit=1024 * 1024):
    """Read an explicit regular JSON file with a byte bound; never interpret code."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('context_packet_regular_file_required')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('context_packet_too_large')
    packet = json.loads(raw.decode('utf-8'))
    if not isinstance(packet, dict):
        raise ValueError('context_packet_object_required')
    return packet


def recover(request, *, previous=None, retrieve=False, directory=None,
            source=None, source_path=None, data_dir=None):
    """Adapt the same recall implementation used by the existing recall command."""
    from .context_gap import recover_context
    from .problem_cases import default_case_directory
    if bool(source) != bool(source_path):
        raise ValueError('recall_fallback_requires_source_and_source_path')
    # absolute() is lexical. Planning, no-gap and repeated receipts do not stat,
    # resolve, refresh or open the selected historical source or case directory.
    scope = {'case_directory': str(Path(directory or default_case_directory()).expanduser().absolute()),
             'source': source or '',
             'source_path': str(Path(source_path).expanduser().absolute()) if source_path else '',
             'data_dir': str(Path(data_dir).expanduser().absolute()) if data_dir else ''}

    def lookup(*, query, project, limit):
        from .recall import recall
        return recall(query, directory=directory, project=project, limit=limit,
                      source=source, source_path=source_path, data_dir=data_dir)

    return recover_context(request, previous=previous, scope=scope,
                           retrieve=lookup if retrieve else None)


def attach_handoff(bundle, recovery):
    """Add current recovery context without replacing historical session facts."""
    from audit.handoff import render_handoff
    bundle['context_recovery'] = recovery
    bundle['payload']['context_recovery'] = recovery
    bundle['compact'] = render_handoff(bundle['payload'], 'compact')
    bundle['standard'] = render_handoff(bundle['payload'], 'standard')
    return bundle


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python3 -m history_core context',
        description='Route explicit current-task gaps; optionally retrieve bounded local evidence.')
    parser.add_argument('--request', type=Path, required=True, help='Current task and evidence needs, not a historical instruction')
    parser.add_argument('--previous', type=Path, help='Prior context result for duplicate/retry suppression')
    parser.add_argument('--retrieve', action='store_true', help='Allow case-first lookup and one explicitly selected history source')
    parser.add_argument('--directory', type=Path, help='Same case directory as recall and compress --save')
    parser.add_argument('--source', type=canonical_source, choices=SOURCES)
    parser.add_argument('--source-path', type=Path)
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args(argv)
    try:
        request = read_packet(args.request, limit=128 * 1024)
        previous = read_packet(args.previous) if args.previous else None
        result = recover(request, previous=previous, retrieve=args.retrieve,
                         directory=args.directory, source=args.source,
                         source_path=args.source_path, data_dir=args.data_dir)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 1 if result.get('status') == 'blocked' else 0
    except (ValueError, OSError, TypeError, KeyError, RecursionError, sqlite3.Error) as exc:
        print(json.dumps({'error': type(exc).__name__, 'detail': redact_text(str(exc))},
                         ensure_ascii=False), file=sys.stderr)
        return 2
