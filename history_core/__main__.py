"""Explicit local source access; no Web, background thread or model startup."""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .reader import HistoryReader
from .providers import SOURCES, canonical_source


def _message_window(value):
    parts = value.split(':')
    if not 1 <= len(parts) <= 2 or not all(part.isascii() and part.isdigit() for part in parts):
        raise argparse.ArgumentTypeError('expected MESSAGE_INDEX[:TEXT_OFFSET], both nonnegative integers')
    return {'message_index': int(parts[0]), 'text_offset': int(parts[1]) if len(parts) == 2 else 0}


def _cases_main(argv):
    parser = argparse.ArgumentParser(
        prog='python3 -m history_core cases',
        description='Read and save authored local problem cases without opening conversation sources.')
    parser.add_argument('--directory', type=Path, help='Editable local case directory')
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--query', default='')
    listing.add_argument('--project', default=None)
    listing.add_argument('--offset', type=int, default=0)
    listing.add_argument('--limit', type=int, default=20)
    showing = commands.add_parser('show')
    showing.add_argument('case_id')
    showing.add_argument('--format', choices=('json', 'markdown'), default='json')
    saving = commands.add_parser('save')
    saving.add_argument('--file', type=Path, required=True, help='Explicit JSON case input')
    args = parser.parse_args(argv)
    from .problem_cases import get_case, list_cases, save_case
    try:
        if args.command == 'list':
            result = list_cases(args.directory, query=args.query, project=args.project,
                                offset=args.offset, limit=args.limit)
        elif args.command == 'show':
            result = get_case(args.directory, args.case_id)
        else:
            data = json.loads(args.file.read_text(encoding='utf-8'))
            result = save_case(args.directory, data)
        if args.command == 'show' and args.format == 'markdown':
            if result.get('case') is None:
                print(json.dumps({**result, 'error': 'case_unavailable'}, ensure_ascii=False),
                      file=sys.stderr)
                return 2
            sys.stdout.write(result['markdown'])
        else:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError, KeyError, RecursionError) as exc:
        print(json.dumps({'error': type(exc).__name__, 'detail': str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'cases':
        return _cases_main(argv[1:])
    if argv and argv[0] == "activity":
        from .activity import main as activity_main
        try:
            return activity_main(argv[1:])
        except Exception as exc:
            print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog='Local cases do not require a source: python3 -m history_core cases --help')
    parser.add_argument("--source", type=canonical_source, choices=SOURCES, required=True)
    parser.add_argument("--source-path", type=Path, required=True, help="Explicit sessions directory or native SQLite file")
    parser.add_argument("--data-dir", type=Path, help="Separate derived cache: required for JSONL; AGY defaults to XDG cache")
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("refresh")
    refresh.add_argument('--file', action='append', type=Path, dest='paths',
                         help='Capture a selected Codex JSONL under the unchanged source root; repeatable')
    refresh.add_argument('--max-bytes', type=int, default=128 * 1024 * 1024,
                         help='Maximum captured bytes per Codex recording; excess is reported explicitly')
    commands.add_parser("health")
    query = commands.add_parser("search")
    query.add_argument("--query", default=None)
    query.add_argument("--limit", type=int, default=20)
    query.add_argument("--offset", type=int, default=0)
    query.add_argument("--project", default=None)
    query.add_argument("--index-revision", default=None, help="Previous page revision; required for offset > 0 in a new process")
    output = query.add_mutually_exclusive_group()
    output.add_argument("--brief", action="store_true", help="Return compact public candidates (the default)")
    output.add_argument('--verbose', action='store_true', help='Return the full legacy session cards')
    query.add_argument('--field', choices=('files_touched',), help='Find literal filenames/paths in public message evidence')
    query.add_argument('--session', dest='session_id', help='Exact indexed session ID; requires compact output')
    query.add_argument('--role', choices=('user', 'assistant', 'tool'),
                       help='Match only public messages with this role; requires compact output')
    events = commands.add_parser('timeline', help='Page public message events by timestamp; coverage is explicit')
    events.add_argument('--start-ms', type=int, help='Inclusive timestamp in milliseconds')
    events.add_argument('--end-ms', type=int, help='Exclusive timestamp in milliseconds')
    events.add_argument('--date', help='Local date YYYY-MM-DD; requires --timezone')
    events.add_argument('--timezone', help='IANA timezone for --date')
    events.add_argument('--session', dest='session_id')
    events.add_argument('--limit', type=int, default=20)
    events.add_argument('--offset', type=int, default=0)
    events.add_argument('--index-revision')
    message = commands.add_parser("message", help="Expand a public search message using its index revision")
    message.add_argument("session_id")
    message.add_argument("message_index", type=int)
    message.add_argument("--index-revision", required=True, help="Exact revision returned by search")
    message.add_argument("--text-offset", type=int, default=0, help="Indexed-text character offset for bounded body pages")
    materials = commands.add_parser('materials', help='Export selected indexed message windows without review prompts or live source reads')
    materials.add_argument('session_id')
    materials.add_argument('--index-revision', required=True)
    materials.add_argument('--message', action='append', required=True, type=_message_window, dest='selections',
                           help='MESSAGE_INDEX[:TEXT_OFFSET]; repeat for up to five selected windows')
    markers = commands.add_parser("markers", help="Query recorded skill/output-style metadata without body reads or effect claims")
    markers.add_argument("--name", default=None)
    markers.add_argument("--kind", choices=("skill", "output_style"), default=None)
    markers.add_argument("--session", default=None, dest="session_id")
    markers.add_argument("--project", default=None, help="Exact recorded working directory")
    markers.add_argument("--limit", type=int, default=20)
    markers.add_argument("--offset", type=int, default=0)
    markers.add_argument("--index-revision", default=None, help="Exact previous marker-page revision; required for offset > 0")
    transfer = commands.add_parser("handoff")
    transfer.add_argument("session_id")
    transfer.add_argument("--include-plans", action="store_true", help="Explicitly include bounded project-file candidates; not verified decisions")
    native = commands.add_parser("native-reference", help="Read a content-bound reference for explicit native continuation")
    native.add_argument("session_id")
    native.add_argument("--device-id", required=True, help="Stable local device identity from the coordinator configuration")
    args = parser.parse_args(argv)
    reader = None
    started = time.monotonic()
    phase = 'initialization'
    try:
        source = args.source_path.resolve(strict=True)
        if args.command == "native-reference":
            if args.source not in ("opencode", "claude", "codex"):
                raise ValueError("native_reference_provider_unsupported")
            from .native import native_reference
            from .claude_native import claude_native_reference
            from .codex_native import codex_native_reference
            reference = {"opencode": native_reference, "claude": claude_native_reference, "codex": codex_native_reference}[args.source]
            result = {"schema_version": "history.native_candidate.v2", "authorization": "context_only",
                      "observed_at": datetime.now(timezone.utc).isoformat(),
                      "reference": reference(args.source_path, args.device_id, args.session_id)}
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == 'timeline':
            phase = 'timeline_parameters'
            start_ms, end_ms = args.start_ms, args.end_ms
            if args.date:
                if not args.timezone or start_ms is not None or end_ms is not None:
                    raise ValueError('timeline_date_requires_timezone_without_epoch_window')
                from .activity import window
                start_ms, end_ms = window(args.date, args.timezone)
            elif args.timezone:
                raise ValueError('timeline_timezone_requires_date')
            if any(value is not None and value < 0 for value in (start_ms, end_ms)):
                raise ValueError('invalid_timeline_window')
            if start_ms is not None and end_ms is not None and start_ms >= end_ms:
                raise ValueError('invalid_timeline_window')
            if not 1 <= args.limit <= 100 or not 0 <= args.offset <= 100000:
                raise ValueError('invalid_timeline_pagination')
            if args.offset and not args.index_revision:
                raise ValueError('index_revision_required: restart from offset 0')
            phase = 'initialization'
        cache_dir = args.data_dir
        if args.source in ('agy', 'antigravity') and cache_dir is None:
            cache_dir = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'agenttracemesh' / 'reader'
        reader = HistoryReader(args.source, source, cache_dir,
                               validate_tree=args.command == 'handoff')
        phase = args.command
        if args.command == "refresh":
            result = reader.refresh(paths=args.paths, max_bytes=args.max_bytes)
        elif args.command == "search":
            result = reader.search(query=args.query, limit=args.limit, offset=args.offset, cwd=args.project,
                                   index_revision=args.index_revision, brief=not args.verbose,
                                   session_id=args.session_id, role=args.role, field=args.field)
        elif args.command == 'timeline':
            result = reader.timeline(start_ms=start_ms, end_ms=end_ms, session_id=args.session_id,
                                     limit=args.limit, offset=args.offset, index_revision=args.index_revision)
            if args.date:
                result.update(date=args.date, timezone=args.timezone)
        elif args.command == "handoff":
            result = reader.handoff(args.session_id, include_plans=args.include_plans)
        elif args.command == "message":
            result = reader.message(args.session_id, args.message_index,
                                    index_revision=args.index_revision, text_offset=args.text_offset)
        elif args.command == 'materials':
            result = reader.materials(args.session_id, args.selections, index_revision=args.index_revision)
        elif args.command == "markers":
            result = reader.markers(name=args.name, kind=args.kind, session_id=args.session_id,
                                    project=args.project,
                                    limit=args.limit, offset=args.offset, index_revision=args.index_revision)
        else:
            result = reader.health()
        if args.command == 'refresh':
            result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if args.command == 'refresh' and result.get('partial') else 0
    except Exception as exc:
        from .reuse import error_code
        code = error_code(exc)
        domain = {'source_busy': 'storage_contention', 'query_budget_exceeded': 'query_budget',
                  'query_interrupted': 'query_interruption'}.get(code, 'source_or_request')
        failure = {"error": type(exc).__name__, "detail": str(exc),
                   'error_code': code, 'failure_domain': domain,
                   'retryable': code == 'source_busy',
                   'phase': getattr(exc, 'phase', phase),
                   'elapsed_ms': round((time.monotonic() - started) * 1000)}
        if isinstance(getattr(exc, 'source_error', None), dict):
            failure['source_error'] = dict(exc.source_error)
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if reader is not None:
            reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
