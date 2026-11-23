#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone, time as dt_time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from audit import (
    AUDIT_VERSION,
    build_audit_from_events,
    build_audit_for_file,
    deserialize_audit_summary,
    patch_db_for_audit,
    serialize_audit_fields,
)
from audit.schema import AuditEvent
from audit.ai_audit import (
    VALUE_SCORE_THRESHOLD,
    build_llm_messages,
    generate_heuristic_audit,
    meets_cost_guard,
    parse_llm_json_response,
)
from audit.llm_client import LLMError, call_chat_completions, detect_provider
from audit.handoff import build_handoff_bundle
from audit.briefing import (
    BRIEFING_MAX_SESSIONS,
    build_briefing,
    build_briefing_llm_messages,
    generate_heuristic_briefing_narrative,
    parse_briefing_llm_response,
    render_briefing_markdown,
)
from audit.schema import LLM_AUDIT_INPUT_FIELDS
from history_core.providers import parser_for, parser_version, include_file, opencode_database, copilot_root, native_indexer

# Compatibility exports: existing callers import these names from app.
from history_core.sources import (
    MAX_SEARCH_CHARS,
    DEFAULT_LIMIT,
    MESSAGE_INLINE_FULL_THRESHOLD,
    MESSAGE_PREVIEW_CHARS,
    MESSAGE_PREVIEW_FETCH_CHARS,
    SEARCH_MATCH_CONTEXT_CHARS,
    SEARCH_MATCH_MAX_CHARS,
    DEFAULT_PAGE_LIMIT,
    SESSION_PREVIEW_CACHE_SIZE,
    _neutral_audit_summary,
    _AUDIT_RAW_JSON_KEYS,
    _strip_audit_raw_json,
    _match_files_touched,
    _escape_sql_like,
    detect_runtime_system,
    build_truncated_message_preview,
    normalize_message_payload,
    RecallTitleStore,
    parse_ts,
    build_message_preview_text,
    build_search_excerpt_text,
    normalize_page_args,
    add_raw_message,
    parse_date_param,
    slugify_path_label,
    PLAN_FILE_NAMES,
    PLAN_SCAN_MAX_FILES,
    PLAN_FILE_MAX_CHARS,
    PLAN_SECTION_PREVIEW_CHARS,
    _PLAN_SECTION_KEYS,
    extract_plan_sections,
    scan_plan_files,
    filter_plan_files_for_window,
    extract_text,
    normalize_codex_context_message,
    _codex_try_parse_json,
    _codex_format_tool_use,
    _codex_format_tool_result,
    _TOOL_CATEGORY_MAP,
    _classify_tool_category,
    _truncate_str,
    _empty_tool_summary,
    _count_diff_lines,
    _codex_summarize_tool_use,
    _codex_summarize_tool_result,
    _claude_summarize_tool_use,
    _claude_summarize_tool_result,
    _codex_usage_from_token_count,
    _usage_greater,
    parse_codex_session_file,
    _claude_extract_text_item,
    _claude_format_tool_use,
    _claude_format_tool_result,
    _claude_usage_from_message,
    _EMPTY_USAGE,
    _accumulate_claude_usage,
    _finalize_claude_usage,
    parse_claude_session_file,
    _openclaw_format_tool_use,
    _openclaw_format_tool_result,
    _openclaw_summarize_tool_use,
    _openclaw_summarize_tool_result,
    parse_openclaw_session_file,
    Indexer,
    HermesStateIndexer,
    OpenCodeIndexer,
)

class WslBootstrapper:
    def __init__(self, distro):
        self.distro = str(distro or "").strip()
        self.lock = threading.Lock()
        self.last_ok = 0.0

    def ensure(self, max_age_seconds=15):
        if not self.distro:
            return
        now = time.time()
        if now - self.last_ok < max_age_seconds:
            return
        with self.lock:
            now = time.time()
            if now - self.last_ok < max_age_seconds:
                return
            try:
                subprocess.run(
                    ["wsl.exe", "-d", self.distro, "--", "true"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=True,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError(f"Failed to start WSL distro {self.distro}: {exc}") from exc
            self.last_ok = time.time()


class SourceBackend:
    def __init__(
        self,
        *,
        system,
        source,
        root_dir,
        sessions_dir,
        data_dir,
        db_filename,
        parse_file_fn,
        parser_version,
        scan_interval,
        archived_dir,
        deleted_dir,
        recall_db_path,
        file_filter_fn=None,
        ensure_fn=None,
        indexer_factory=None,
        read_only=False,
    ):
        self.system = system
        self.source = source
        self.root_dir = Path(root_dir)
        self.sessions_dir = Path(sessions_dir)
        self.archived_dir = Path(archived_dir)
        self.deleted_dir = Path(deleted_dir)
        self.ensure_fn = ensure_fn
        self.read_only = bool(read_only)
        self.scan_interval = max(1, int(scan_interval))
        self.last_refresh_error = None
        self.last_refreshed_at = None
        self.refreshing = False
        self._refresh_lock = threading.Lock()
        self._indexer_factory = indexer_factory
        if indexer_factory is not None:
            try:
                self.indexer = indexer_factory()
            except (OSError, ValueError, sqlite3.Error) as exc:
                self.indexer = None
                self.last_refresh_error = type(exc).__name__ + ': source initialization failed'
        else:
            self.indexer = Indexer(
                sessions_dir=self.sessions_dir,
                data_dir=Path(data_dir),
                source=source,
                db_filename=db_filename,
                scan_interval=scan_interval,
                parse_file_fn=parse_file_fn,
                file_filter_fn=file_filter_fn,
                parser_version=parser_version,
                recall_db_path=recall_db_path,
            )
        if self.ensure_fn is None:
            try:
                self._refresh_index_once()
            except (OSError, ValueError, sqlite3.Error):
                pass
        self._start_background_refresh(run_immediately=self.ensure_fn is not None)

    def ensure_ready(self):
        if self.ensure_fn:
            self.ensure_fn()
        if self.last_refresh_error:
            raise RuntimeError(self.last_refresh_error)

    def _refresh_index_once(self):
        with self._refresh_lock:
            self.refreshing = True
            try:
                return self._perform_refresh()
            finally:
                self.refreshing = False

    def _perform_refresh(self):
        try:
            if self.ensure_fn:
                self.ensure_fn()
            if self.indexer is None and self._indexer_factory:
                self.indexer = self._indexer_factory()
            self.indexer.maybe_update_index(max_age_seconds=0)
            self.last_refreshed_at = datetime.now(timezone.utc).isoformat()
            self.last_refresh_error = None
        except Exception as exc:
            self.last_refresh_error = type(exc).__name__ + ': source refresh failed'
            raise

    def _background_refresh_loop(self, run_immediately=False):
        if not run_immediately:
            time.sleep(self.scan_interval)
        while True:
            try:
                self._refresh_index_once()
            except Exception:
                pass
            time.sleep(self.scan_interval)

    def _start_background_refresh(self, run_immediately=False):
        thread = threading.Thread(
            target=self._background_refresh_loop,
            kwargs={"run_immediately": run_immediately},
            name=f"history-viewer-scan-{self.system}-{self.source}",
            daemon=True,
        )
        thread.start()
        self._refresh_thread = thread


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, source_backends=None, wsl_distro=None, runtime_system="windows", audit_config=None, demo=False, material_dir=None, case_dir=None, **kwargs):
        self._source_backends = source_backends or {}
        self._wsl_distro = wsl_distro
        self._runtime_system = str(runtime_system or "windows").strip() or "windows"
        self._audit_config = audit_config or {}
        self._material_dir = material_dir
        self._case_dir = case_dir if case_dir is not None or not demo else Path(__file__).resolve().parent / 'demo-isolated' / 'problem-cases'
        self._demo = bool(demo)
        super().__init__(*args, directory=directory, **kwargs)

    def _resolve_source_request(self, path):
        if path == "/api/claude" or path.startswith("/api/claude/"):
            backend = self._source_backends.get((self._runtime_system, "claude"))
            subpath = path[len("/api/claude"):] or "/"
            return backend, subpath

        if path == "/api/openclaw" or path.startswith("/api/openclaw/"):
            backend = self._source_backends.get((self._runtime_system, "openclaw"))
            subpath = path[len("/api/openclaw"):] or "/"
            return backend, subpath

        match = re.match(r"^/api/([^/]+)/([^/]+)(/.*)?$", path)
        if match:
            system = match.group(1)
            source = match.group(2)
            backend = self._source_backends.get((system, source))
            subpath = match.group(3) or "/"
            return backend, subpath

        if path == "/api" or path.startswith("/api/"):
            backend = self._source_backends.get((self._runtime_system, "codex"))
            subpath = path[len("/api"):] or "/"
            return backend, subpath

        return None, None

    def _ensure_backend_ready(self, backend):
        if backend is None:
            self.send_json({"error": "not found"}, status=404)
            return False
        try:
            backend.ensure_ready()
        except RuntimeError as exc:
            self.send_json({"error": "source_unavailable", "detail": str(exc)}, status=503)
            return False
        return True

    def _extract_session_id(self, source_path, suffix=""):
        if not source_path or not source_path.startswith("/session/"):
            return None
        end = -len(suffix) if suffix else None
        return unquote(source_path[len("/session/"):end])

    def _extract_session_message_request(self, source_path):
        if not source_path or not source_path.startswith("/session/"):
            return None, None
        match = re.match(r"^/session/([^/]+)/message/(\d+)$", source_path)
        if not match:
            return None, None
        return unquote(match.group(1)), int(match.group(2))

    def _extract_session_messages_request(self, source_path, suffix="/messages"):
        if not source_path or not source_path.startswith("/session/"):
            return None
        if not source_path.endswith(suffix):
            return None
        start = len("/session/")
        end = -len(suffix)
        return unquote(source_path[start:end])

    def _extract_session_audit_request(self, source_path):
        suffix = "/audit"
        if not source_path or not source_path.startswith("/session/"):
            return None
        if not source_path.endswith(suffix):
            return None
        start = len("/session/")
        end = -len(suffix)
        return unquote(source_path[start:end])

    def _check_api_host(self):
        host = self.headers.get('Host', '')
        try:
            parsed = urlparse('http://' + host)
            hostname = parsed.hostname
            port = parsed.port or 80
            if (not hostname or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment or host != host.strip()):
                raise ValueError('invalid_host')
            allowed = {'localhost', '127.0.0.1', '::1'}
            bound_host, bound_port = self.server.server_address[:2]
            if bound_host not in ('', '0.0.0.0', '::'):
                allowed.add(str(bound_host).lower())
            # A wildcard bind still permits the local address actually reached,
            # without trusting an arbitrary DNS name supplied by the request.
            allowed.add(str(self.connection.getsockname()[0]).lower())
            if hostname.lower() not in allowed or port != bound_port:
                raise ValueError('untrusted_host')
        except (ValueError, AttributeError, OSError, IndexError):
            self.send_json({'error': 'untrusted_host'}, status=403)
            return False
        return True

    def _same_origin_json(self):
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        return (self.headers.get('Content-Type', '').split(';')[0].strip().lower() == 'application/json'
                and (not origin or origin in ('http://' + host, 'https://' + host))
                and self.headers.get('Sec-Fetch-Site', '').lower() != 'cross-site')

    def _loopback_request(self):
        try:
            peer = ip_address(self.client_address[0])
            peer = getattr(peer, 'ipv4_mapped', None) or peer
            host = urlparse('http://' + self.headers.get('Host', '')).hostname
            return peer.is_loopback and host in ('localhost', '127.0.0.1', '::1')
        except (AttributeError, ValueError, IndexError):
            return False

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if (path == '/api' or path.startswith('/api/')) and not self._check_api_host():
            return

        if path == '/api/version':
            root = Path(__file__).resolve().parent
            info_path = root / 'BUILD_INFO.json'
            info = json.loads(info_path.read_text()) if info_path.exists() else {'source_commit': 'unknown'}
            return self.send_json({'version': (root / 'VERSION').read_text().strip(), 'source_commit': info.get('source_commit', 'unknown')})

        if path.startswith('/api/reuse/'):
            return self.handle_reuse_get(parsed)

        if path == "/api/sources":
            return self.handle_sources()

        if path == "/api/workspace":
            from history_core.service import workspace
            sources = []
            errors = []
            for (system, source), backend in self._source_backends.items():
                try:
                    backend.ensure_ready()
                    sources.append((system, source, backend.indexer))
                except Exception:
                    errors.append({"system": system, "source": source, "error": "source_unavailable"})
            result = workspace(sources)
            result['demo'] = self._demo
            result['freshness'] = 'unknown'
            result['live_task_state'] = 'unknown'
            result['sources'] = [{'system': system, 'source': source,
                'status': 'unavailable' if any(e['system'] == system and e['source'] == source for e in errors) else 'readable',
                'last_refreshed_at': getattr(backend, 'last_refreshed_at', None),
                'error': getattr(backend, 'last_refresh_error', None)}
                for (system, source), backend in self._source_backends.items()]
            result["errors"].extend(errors)
            return self.send_json(result)

        backend, source_path = self._resolve_source_request(path)
        if backend or path.startswith("/api/"):
            if not self._ensure_backend_ready(backend):
                return
            if source_path == "/sessions":
                return self.handle_sessions(parsed, backend)
            if source_path == "/projects":
                return self.handle_projects(parsed, backend)
            if source_path == "/usage":
                return self.handle_usage(parsed, backend)
            if source_path == "/briefing":
                return self.handle_briefing_get(parsed, backend)
            if source_path == "/plans":
                return self.handle_plans(parsed, backend)
            if source_path and source_path.startswith("/session/") and source_path.endswith("/plans"):
                session_id = self._extract_session_messages_request(source_path, "/plans")
                return self.handle_plans(parsed, backend, session_id=session_id)
            if source_path and source_path.endswith("/messages/search"):
                session_id = self._extract_session_messages_request(source_path, "/messages/search")
                return self.handle_session_messages_search(session_id, parsed, backend)
            if source_path and source_path.endswith("/messages"):
                session_id = self._extract_session_messages_request(source_path, "/messages")
                return self.handle_session_messages(session_id, parsed, backend)
            if source_path and "/message/" in source_path:
                session_id, message_index = self._extract_session_message_request(source_path)
                return self.handle_session_message(session_id, message_index, backend, parsed)
            if source_path and source_path.endswith("/search"):
                session_id = self._extract_session_id(source_path, "/search")
                return self.handle_session_search(session_id, parsed, backend)
            if source_path and source_path.endswith("/audit"):
                session_id = self._extract_session_audit_request(source_path)
                return self.handle_session_audit(session_id, parsed, backend)
            if source_path and source_path.startswith("/session/"):
                session_id = self._extract_session_id(source_path)
                return self.handle_session(session_id, backend, parsed)
            if source_path == "/reindex":
                return self.send_json({'error': 'post_required'}, status=405)
            return self.send_json({"error": "not found"}, status=404)

        if parsed.path == "/history":
            self.path = "/index.html"
            return super().do_GET()
        if parsed.path in ("/", "/workspace"):
            self.path = "/workspace.html"
            return super().do_GET()
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/api' or path.startswith('/api/'):
            if not self._check_api_host():
                return
            if not self._same_origin_json():
                return self.send_json({'error': 'untrusted_request_origin'}, status=403)
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self.send_json({'error':'invalid_content_length'},status=400)
        if length < 0 or (path.startswith('/api/reuse/') and length > 65536):
            return self.send_json({'error':'request_too_large'},status=413)
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        if path.startswith('/api/reuse/'):
            return self.handle_reuse_post(parsed, data)

        backend, source_path = self._resolve_source_request(path)
        if not backend or not source_path:
            return self.send_json({"error": "not found"}, status=404)
        if source_path == '/cleanup/weak-sessions':
            return self.send_json({'error': 'automatic_weak_cleanup_disabled',
                                   'detail': 'Short history is not evidence that a session can be removed.'}, status=405)
        if not self._ensure_backend_ready(backend):
            return
        if source_path == "/briefing":
            # Briefing generation reads deterministic summaries and writes
            # nothing to the source, so it is allowed for read-only sources.
            return self.handle_briefing_generate(data, backend)
        if source_path == '/reindex':
            backend.indexer.scan_sessions()
            return self.send_json({'ok': True})
        if getattr(backend, "read_only", False):
            return self.send_json({"error": "read_only_source"}, status=405)

        def _rename():
            title = data.get("title", "").strip()
            if not title:
                return self.send_json({"error": "title required"}, status=400)
            session_id = self._extract_session_id(source_path, "/rename")
            ok = backend.indexer.rename_session(session_id, title)
            return self.send_json({"ok": ok})

        def _archive():
            session_id = self._extract_session_id(source_path, "/archive")
            ok, detail = backend.indexer.archive_session(session_id, backend.archived_dir)
            if not ok:
                return self.send_json({"error": detail}, status=404 if detail == 'not_found' else 500)
            return self.send_json({"ok": True})

        def _pin():
            pinned = data.get("pinned", True)
            session_id = self._extract_session_id(source_path, "/pin")
            ok = backend.indexer.pin_session(session_id, pinned)
            return self.send_json({"ok": ok})

        def _delete_project():
            project = data.get("project", "").strip()
            if not project:
                return self.send_json({"error": "project required"}, status=400)
            ok, detail, deleted_count, backup_dir = backend.indexer.delete_project_sessions(project, backend.deleted_dir)
            if not ok:
                return self.send_json({"error": detail, "partial": detail == 'partial',
                                       "deleted_count": deleted_count, "backup_dir": backup_dir},
                                      status=404 if detail == 'not_found' else 500)
            return self.send_json({
                "ok": True,
                "deleted_count": deleted_count,
                "backup_dir": backup_dir,
            })

        if source_path == "/project/delete":
            return _delete_project()
        if source_path.startswith("/session/"):
            if source_path.endswith("/audit/delete"):
                session_id = self._extract_session_id(source_path, "/audit/delete")
                return self.handle_audit_delete(session_id, backend)
            if source_path.endswith("/audit"):
                session_id = self._extract_session_id(source_path, "/audit")
                return self.handle_audit_generate(session_id, data, backend)
            if source_path.endswith("/rename"):
                return _rename()
            if source_path.endswith("/archive"):
                return _archive()
            if source_path.endswith("/pin"):
                return _pin()

        return self.send_json({"error": "not found"}, status=404)

    def _reuse_sources(self, requested_source=None):
        from history_core.reuse import error_code
        sources, errors = [], []
        for (system, source), backend in self._source_backends.items():
            if requested_source and source != requested_source:
                continue
            try:
                from history_core.diagnostics import optional_missing_source
                if optional_missing_source(backend):
                    continue
                backend.ensure_ready()
                if backend.indexer is None:
                    raise ValueError('source_unavailable')
                sources.append((system, source, backend.indexer))
            except Exception as exc:
                errors.append({'system': system, 'source': source, 'error': error_code(exc)})
        return sources, errors

    def handle_reuse_get(self, parsed):
        from history_core import reuse
        from history_core.diagnostics import health
        params = parse_qs(parsed.query, keep_blank_values=True)
        value = lambda key, default=None: params.get(key, [default])[0]
        endpoint = parsed.path.rsplit('/', 1)[-1]
        if endpoint in ('cases', 'case'):
            from history_core.problem_cases import list_cases, get_case
            try:
                directory = getattr(self, '_case_dir', None)
                if endpoint == 'case':
                    result = get_case(directory, value('id', ''))
                else:
                    result = list_cases(directory, query=value('q', ''), project=value('project'),
                                        offset=int(value('offset', 0)), limit=int(value('limit', 20)))
                return self.send_json(result)
            except (ValueError, OSError) as exc:
                code = reuse.error_code(exc)
                return self.send_json({'error': code},
                    status=404 if code in ('case_not_found', 'source_not_found') else 400)
        if endpoint == 'health':
            return self.send_json(health(self._source_backends, demo=self._demo,
                system=self._runtime_system, version=Path(__file__).with_name('VERSION').read_text().strip()))
        selected_source = reuse.canonical_source(value('source') or None) if endpoint in ('sessions', 'key-messages') else None
        sources, errors = self._reuse_sources(requested_source=selected_source)
        try:
            page = {'cursor':value('cursor'), 'limit':value('limit',20), 'errors':errors}
            if endpoint == 'search':
                start = parse_date_param(value('start'), end=False)
                end = parse_date_param(value('end'), end=True)
                if value('start') and start is None or value('end') and end is None:
                    raise ValueError('invalid_date')
                result = reuse.search(sources, query=value('q',''), source=value('source') or None,
                    project=value('project'), start_ms=start, end_ms=end, **page)
            elif endpoint == 'projects':
                result = reuse.projects(sources, **page)
            elif endpoint == 'sessions':
                page['limit'] = value('limit', 50)
                result = reuse.sessions(sources, project=value('project'), source=selected_source, **page)
            elif endpoint == 'key-messages':
                from history_core.conversation_navigation import key_messages
                result = key_messages(sources, system=value('system'), source=selected_source,
                    session_id=value('session'), store_id=value('store_id') or None,
                    source_revision=value('source_revision') or None)
            elif endpoint == 'timeline':
                result = reuse.timeline(sources, project=value('project'), file_path=value('file') or None, **page)
            elif endpoint == 'review-requests':
                from history_core.evidence import _resolve
                from history_core.review import list_review_requests
                target = _resolve(sources, {'system':value('system'), 'source':value('source'), 'store_id':value('store_id')})
                result = list_review_requests(target[2], value('session'),
                    source_revision=value('source_revision'), content_revision=value('content_revision'),
                    context_revision=value('context_revision'), offset=int(value('offset', 0)), limit=int(value('limit', 10)))
            elif endpoint == 'raw':
                from history_core.evidence import raw_record
                target = next((x for x in sources if x[0] == value('system') and x[1] == value('source')), None)
                if target is None: raise ValueError('source_unavailable')
                result = raw_record(target[2], value('session'),
                    {'content_revision': value('content_revision'), 'context_revision': value('context_revision')},
                    evidence_id=value('evidence_id'), line_no=value('line_no'))
            elif endpoint == 'evidence':
                target = next((x for x in sources if x[0] == value('system') and x[1] == value('source')),None)
                if target is None: raise ValueError('source_unavailable')
                expected = value('source_revision')
                if expected and reuse.index_revision(target[2]) != expected:
                    raise ValueError('index_revision_changed')
                result = reuse.evidence_item(*target, value('session'))
                if expected and reuse.index_revision(target[2]) != expected:
                    raise ValueError('index_revision_changed')
            else:
                return self.send_json({'error':'not_found'},status=404)
            return self.send_json(result)
        except (ValueError,OSError,sqlite3.Error) as exc:
            code = reuse.error_code(exc)
            conflict = code != 'source_revision_required' and ('revision' in code or 'stale' in code)
            return self.send_json({'error':code},status=409 if conflict else 400)

    def handle_reuse_post(self, parsed, data):
        from history_core import reuse
        if not isinstance(data,dict): return self.send_json({'error':'invalid_request'},status=400)
        endpoint=parsed.path.rsplit('/',1)[-1]
        try:
            if endpoint == 'case-save':
                if not self._loopback_request() or not self._same_origin_json():
                    return self.send_json({'error': 'case_local_request_required'}, status=403)
                from history_core.problem_cases import save_case
                try:
                    return self.send_json(save_case(getattr(self, '_case_dir', None), data))
                except RecursionError:
                    return self.send_json({'error': 'case_nesting_invalid'}, status=400)
            if endpoint == 'refresh':
                backend=self._source_backends.get((data.get('system'),data.get('source')))
                if backend is None: raise ValueError('source_unavailable')
                backend._refresh_index_once()
                return self.send_json({'status':'refreshed'})
            if endpoint in ('material-preview', 'material-export'):
                from history_core.materials import preview_material, export_material
                # JSON + same-origin checks prevent drive-by local filesystem writes.
                if not self._loopback_request():
                    return self.send_json({'error': 'material_loopback_required'}, status=403)
                if not self._same_origin_json():
                    return self.send_json({'error': 'material_same_origin_required'}, status=403)
                sources, errors = self._reuse_sources()
                material = preview_material(sources, data.get('selections'), data.get('fields'))
                root = getattr(self, '_material_dir', None)
                if endpoint == 'material-export':
                    return self.send_json(export_material(root, material, data.get('revision')))
                return self.send_json({**material, 'export_enabled': bool(root), 'destination': str(root) if root else None})
            if endpoint in ('selection', 'review-preview'):
                from history_core.evidence import selection_bundle
                sources,errors=self._reuse_sources()
                if endpoint == 'review-preview':
                    from history_core.review import build_review_packet
                    return self.send_json(build_review_packet(sources, data.get('selections')))
                return self.send_json(selection_bundle(sources,data.get('selections')))
            return self.send_json({'error':'not_found'},status=404)
        except (ValueError,OSError,sqlite3.Error) as exc:
            code=reuse.error_code(exc)
            return self.send_json({'error':code},status=409 if 'revision' in code or 'stale' in code or 'changed' in code else 400)

    def handle_sources(self):
        items = []
        for (system, source), backend in sorted(self._source_backends.items()):
            items.append({
                "system": system,
                "source": source,
                "root_dir": str(backend.root_dir),
                "sessions_dir": str(backend.sessions_dir),
                "archived_dir": str(backend.archived_dir),
                "deleted_dir": str(backend.deleted_dir),
                "read_only": bool(getattr(backend, "read_only", False)),
            })
        return self.send_json({
            "sources": items,
            "wsl_distro": self._wsl_distro,
            "runtime_system": self._runtime_system,
        })

    def handle_sessions(self, parsed, backend):
        params = parse_qs(parsed.query)
        q = params.get("q", [""])[0].strip() or None
        start = params.get("start", [None])[0]
        end = params.get("end", [None])[0]
        project = params.get("project", [""])[0].strip() or None
        sort = params.get("sort", [""])[0].strip() or None
        limit = params.get("limit", [DEFAULT_PAGE_LIMIT])[0]
        offset = params.get("offset", [0])[0]
        file_path = (params.get("file", [""])[0].strip() or None)

        start_ms = parse_date_param(start, end=False)
        end_ms = parse_date_param(end, end=True)

        page = backend.indexer.list_sessions_page(
            q=q,
            start_ms=start_ms,
            end_ms=end_ms,
            limit=limit,
            offset=offset,
            cwd=project,
            sort=sort,
            file_path=file_path,
        )
        return self.send_json({
            "sessions": page["items"],
            "limit": page["limit"],
            "offset": page["offset"],
            "has_more": page["has_more"],
            "next_offset": page["next_offset"],
        })

    def handle_projects(self, parsed, backend):
        params = parse_qs(parsed.query)
        q = params.get("q", [""])[0].strip() or None
        limit = params.get("limit", [DEFAULT_PAGE_LIMIT])[0]
        offset = params.get("offset", [0])[0]
        page = backend.indexer.list_projects_page(q=q, limit=limit, offset=offset)
        return self.send_json({
            "projects": page["items"],
            "limit": page["limit"],
            "offset": page["offset"],
            "has_more": page["has_more"],
            "next_offset": page["next_offset"],
        })

    def handle_usage(self, parsed, backend):
        params = parse_qs(parsed.query)
        start = params.get("start", [None])[0]
        end = params.get("end", [None])[0]
        project = params.get("project", [""])[0].strip() or None
        start_ms = parse_date_param(start, end=False)
        end_ms = parse_date_param(end, end=True)
        fn = getattr(backend.indexer, "query_usage", None)
        if fn is None:
            return self.send_json({
                "totals": {},
                "has_usage_data": False,
                "by_day": [],
                "by_project": [],
                "top_sessions": [],
            })
        return self.send_json(fn(start_ms=start_ms, end_ms=end_ms, cwd=project))

    def handle_plans(self, parsed, backend, session_id=None):
        if session_id is not None:
            meta = backend.indexer.get_session_metadata(session_id)
            if not meta:
                return self.send_json({"error": "not_found"}, status=404)
            cwd = str(meta.get("cwd") or "")
            items = filter_plan_files_for_window(
                scan_plan_files(cwd), meta.get("start_ts_ms"), meta.get("end_ts_ms")
            )
            return self.send_json({"cwd": cwd, "items": items})
        params = parse_qs(parsed.query)
        project = params.get("project", [""])[0].strip()
        if not project:
            return self.send_json({"error": "project required"}, status=400)
        return self.send_json({"cwd": project, "items": scan_plan_files(project)})

    def _build_briefing_for_range(self, backend, date_value, project, timezone_value="UTC"):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        from history_core.web_briefing import build_window_briefing
        try:
            zone = ZoneInfo(timezone_value)
            date_value = date_value or datetime.now(zone).strftime("%Y-%m-%d")
            briefing = build_window_briefing(backend, date_value, timezone_value, project,
                                              limit=BRIEFING_MAX_SESSIONS)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            return None, None, "index_revision_changed" if "revision" in str(exc) else "invalid_date_or_timezone"
        except (OSError, sqlite3.Error):
            return None, None, "briefing_query_failed"
        return briefing, render_briefing_markdown(briefing), None

    def handle_briefing_get(self, parsed, backend):
        params = parse_qs(parsed.query)
        date_value = params.get("date", [""])[0].strip()
        project = params.get("project", [""])[0].strip() or None
        briefing, markdown, error = self._build_briefing_for_range(backend, date_value, project, params.get("timezone", ["UTC"])[0])
        if error:
            return self.send_json({"error": error}, status={"index_revision_changed": 409, "briefing_query_failed": 503}.get(error, 400))
        return self.send_json({
            "briefing": briefing,
            "markdown": markdown,
            "ai_configured": self._audit_llm_configured(),
        })

    def handle_briefing_generate(self, data, backend):
        date_value = str(data.get("date") or "").strip()
        project = str(data.get("project") or "").strip() or None
        briefing, markdown, error = self._build_briefing_for_range(backend, date_value, project, str(data.get("timezone") or "UTC"))
        if error:
            return self.send_json({"error": error}, status={"index_revision_changed": 409, "briefing_query_failed": 503}.get(error, 400))
        mode = str(data.get("mode") or "auto").strip().lower()
        config = self._audit_llm_config()
        if mode == "llm" and not config:
            return self.send_json({"error": "no_llm_configured", "detail": "Configure --audit-llm-base-url/model or set OPENAI_API_KEY."}, status=400)
        narrative = None
        if mode in ("llm", "auto") and config:
            try:
                raw = call_chat_completions(config, build_briefing_llm_messages(briefing))
                narrative = parse_briefing_llm_response(raw, model=config.get("model"))
            except (LLMError, ValueError) as exc:
                if mode == "llm":
                    return self.send_json({"error": "llm_failed", "detail": str(exc)}, status=502)
        if narrative is None:
            narrative = generate_heuristic_briefing_narrative(briefing)
        return self.send_json({
            "briefing": briefing,
            "markdown": markdown,
            "narrative": narrative,
        })

    def _check_source_revision(self, backend, parsed):
        from history_core.reuse import index_revision
        expected = parse_qs(parsed.query).get('source_revision', [None])[0] if parsed else None
        if not expected:
            return True
        try:
            matches = index_revision(backend.indexer) == expected
        except (OSError, ValueError, sqlite3.Error):
            matches = False
        if not matches:
            self.send_json({'error': 'index_revision_changed', 'detail': '重新搜索后打开消息。'}, status=409)
        return matches

    def handle_session(self, session_id, backend, parsed=None):
        if not self._check_source_revision(backend, parsed): return
        include_messages = False
        if parsed is not None:
            params = parse_qs(parsed.query)
            include_messages = params.get("include_messages", ["0"])[0] in ("1", "true", "yes")
        data = backend.indexer.get_session(session_id, include_messages=include_messages)
        if not self._check_source_revision(backend, parsed): return
        if not data:
            return self.send_json({"error": "not_found"}, status=404)
        return self.send_json(data)

    def handle_session_audit(self, session_id, parsed, backend):
        if session_id is None:
            return self.send_json({"error": "not_found"}, status=404)
        from history_core.service import audit_handoff
        from history_core.reuse import error_code
        try:
            audit, bundle = audit_handoff(backend.indexer, session_id)
        except (ValueError, OSError, sqlite3.Error) as exc:
            code = error_code(exc)
            return self.send_json({'error':code},status=404 if code in ('session_not_found','audit_not_supported_or_unavailable') else 409)
        getter = getattr(backend.indexer, "get_stored_ai_audit", None)
        ai_audit = getter(session_id) if getter else None
        return self.send_json({"audit":audit,"ai_audit":ai_audit,
                              "ai_configured":self._audit_llm_configured(),"handoff":bundle})

    def handle_audit_generate(self, session_id, data, backend):
        if session_id is None:
            return self.send_json({"error": "not_found"}, status=404)
        builder = getattr(backend.indexer, "build_session_audit", None)
        if not builder:
            return self.send_json({"error": "audit_unavailable"}, status=404)
        audit = builder(session_id)
        if audit is None:
            return self.send_json({"error": "audit_unavailable"}, status=404)
        mode = str(data.get("mode") or "auto").strip().lower()
        llm_input = {k: audit.get(k) for k in LLM_AUDIT_INPUT_FIELDS}
        config = self._audit_llm_config()
        if mode == "llm" and not config:
            return self.send_json({"error": "no_llm_configured", "detail": "Configure --audit-llm-base-url/model or set OPENAI_API_KEY."}, status=400)
        result = None
        use_llm = mode in ("llm", "auto") and bool(config)
        if use_llm:
            threshold = int((self._audit_config or {}).get("value_threshold") or VALUE_SCORE_THRESHOLD)
            ok, reason = meets_cost_guard(int(audit.get("value_score") or 0), threshold)
            if not ok and mode == "llm":
                return self.send_json({"error": "below_cost_guard", "detail": reason}, status=400)
            use_llm = ok
        if use_llm:
            try:
                messages = build_llm_messages(llm_input)
                raw = call_chat_completions(config, messages)
                result = parse_llm_json_response(raw, model=config.get("model"))
            except (LLMError, ValueError) as exc:
                if mode == "llm":
                    return self.send_json({"error": "llm_failed", "detail": str(exc)}, status=502)
        if result is None:
            result = generate_heuristic_audit(llm_input)
        store_fn = getattr(backend.indexer, "store_ai_audit", None)
        if store_fn:
            store_fn(session_id, result)
        return self.send_json({"ai_audit": result})

    def handle_audit_delete(self, session_id, backend):
        if session_id is None:
            return self.send_json({"error": "not_found"}, status=404)
        clear_fn = getattr(backend.indexer, "clear_ai_audit", None)
        if clear_fn:
            clear_fn(session_id)
        return self.send_json({"ok": True})

    def _audit_llm_configured(self):
        cfg = self._audit_config or {}
        if cfg.get("llm_base_url") and cfg.get("llm_model"):
            return True
        for var in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            if os.environ.get(var, "").strip():
                return True
        return False

    def _audit_llm_config(self):
        cfg = self._audit_config or {}
        return detect_provider(
            base_url=cfg.get("llm_base_url"),
            model=cfg.get("llm_model"),
            api_key=cfg.get("llm_api_key"),
        )

    def handle_session_messages(self, session_id, parsed, backend):
        if not self._check_source_revision(backend, parsed): return
        if session_id is None:
            return self.send_json({"error": "not_found"}, status=404)
        params = parse_qs(parsed.query)
        offset = params.get("offset", [0])[0]
        limit = params.get("limit", [DEFAULT_LIMIT])[0]
        data = backend.indexer.get_session_messages_page(session_id, offset=offset, limit=limit)
        if not self._check_source_revision(backend, parsed): return
        if data is None:
            return self.send_json({"error": "not_found"}, status=404)
        return self.send_json(data)

    def handle_session_message(self, session_id, message_index, backend, parsed=None):
        if not self._check_source_revision(backend, parsed): return
        if session_id is None or message_index is None:
            return self.send_json({"error": "not_found"}, status=404)
        data = backend.indexer.get_session_message(session_id, message_index)
        if not self._check_source_revision(backend, parsed): return
        if not data:
            return self.send_json({"error": "not_found"}, status=404)
        return self.send_json({"message": data})

    def handle_session_search(self, session_id, parsed, backend):
        if not self._check_source_revision(backend, parsed): return
        if session_id is None:
            return self.send_json({"error": "not_found"}, status=404)
        params = parse_qs(parsed.query)
        query = params.get("q", [""])[0]
        limit = params.get("limit", [None])[0]
        data = backend.indexer.search_session_messages(session_id, query, limit=limit)
        if not self._check_source_revision(backend, parsed): return
        if data is None:
            return self.send_json({"error": "not_found"}, status=404)
        return self.send_json(data)

    def handle_session_messages_search(self, session_id, parsed, backend):
        return self.handle_session_search(session_id, parsed, backend)

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description="Local Agent CLI history viewer")
    parser.add_argument("--version", action="version", version=Path(__file__).with_name("VERSION").read_text().strip())
    parser.add_argument("--material-dir", help="Explicit local Markdown material inbox; disabled by default")
    parser.add_argument("--case-dir", help="Editable local problem-case JSON directory; defaults to the user data directory")
    parser.add_argument("--demo", action="store_true", help="Use packaged synthetic data only; disable private source discovery")
    parser.add_argument("--no-wsl", action="store_true", help="Do not discover or start WSL sources")
    parser.add_argument("--codex-dir", default=os.path.expanduser("~/.codex"))
    parser.add_argument("--claude-dir", default=os.path.expanduser("~/.claude"))
    parser.add_argument("--openclaw-dir", default=os.path.expanduser("~/.openclaw"))
    parser.add_argument("--codebuddy-dir", "--cbc-dir", dest="codebuddy_dir", default=os.environ.get('CODEBUDDY_CONFIG_DIR') or os.path.expanduser("~/.codebuddy"))
    parser.add_argument("--mcode-dir", default=os.path.expanduser("~/.minimax"))
    parser.add_argument("--mcode-sessions-dir", default=None, help="Explicit mcode v2 sessions directory")
    parser.add_argument("--gemini-dir", default=os.environ.get('GEMINI_CLI_HOME') or os.path.expanduser("~/.gemini"))
    parser.add_argument("--pi-dir", default=os.environ.get('PI_CODING_AGENT_DIR') or os.path.expanduser("~/.pi/agent"))
    parser.add_argument("--pi-sessions-dir", default=os.environ.get('PI_CODING_AGENT_SESSION_DIR'))
    parser.add_argument("--prime-dir", default=os.environ.get('PRIME_AGENT_CODING_AGENT_DIR') or os.path.expanduser("~/.prime/agent"))
    parser.add_argument("--prime-sessions-dir", default=os.environ.get('PRIME_AGENT_SESSION_DIR'))
    parser.add_argument("--copilot-dir", default=str(copilot_root()))
    parser.add_argument("--zcode-state-db", default=os.path.expanduser("~/.zcode/cli/db/db.sqlite"))
    parser.add_argument("--agy-state-db", default=os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db"))
    parser.add_argument("--antigravity-state-db", default=os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db"))
    parser.add_argument("--hermes-state-db", default=None)
    parser.add_argument("--opencode-state-db", default=None)
    parser.add_argument("--wsl-distro", default="Ubuntu-22.04")
    parser.add_argument("--wsl-user", default="muqiao")
    parser.add_argument("--wsl-codex-dir", default=None)
    parser.add_argument("--wsl-claude-dir", default=None)
    parser.add_argument("--wsl-openclaw-dir", default=None)
    parser.add_argument("--data-dir", default=None, help="Directory for index.sqlite")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--scan-interval", type=int, default=5)
    parser.add_argument("--audit-llm-base-url", default=None, help="OpenAI-compatible base URL for AI audit (e.g. http://localhost:11434/v1)")
    parser.add_argument("--audit-llm-model", default=None, help="Model name for AI audit (e.g. gpt-4o-mini, qwen2.5:7b)")
    parser.add_argument("--audit-llm-api-key", default=None, help="API key for AI audit (defaults to OPENAI_API_KEY/DEEPSEEK_API_KEY env)")
    parser.add_argument("--audit-value-threshold", type=int, default=VALUE_SCORE_THRESHOLD, help="Minimum value_score to allow AI audit generation")
    args = parser.parse_args()
    supplied_options = {arg.split('=', 1)[0] for arg in sys.argv[1:] if arg.startswith('--')}

    if args.demo:
        demo_root = Path(__file__).resolve().parent / 'demo'
        args.codex_dir = str(demo_root / 'codex')
        args.claude_dir = str(demo_root / 'claude')
        args.openclaw_dir = str(demo_root / 'openclaw')
        for source in ('codebuddy', 'gemini', 'pi', 'prime', 'copilot', 'mcode'):
            setattr(args, source + '_dir', str(demo_root / source))
        args.pi_sessions_dir = args.prime_sessions_dir = None
        args.mcode_sessions_dir = None
        args.no_wsl = True
    codex_dir = Path(args.codex_dir).expanduser()
    claude_dir = Path(args.claude_dir).expanduser()
    openclaw_dir = Path(args.openclaw_dir).expanduser()
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else Path(__file__).resolve().parent
    if args.demo:
        data_dir = data_dir / 'demo-isolated'
        if args.case_dir is None:
            args.case_dir = str(data_dir / 'problem-cases')
    data_dir.mkdir(parents=True, exist_ok=True)
    runtime_system = detect_runtime_system()
    source_backends = {}

    def detect_hermes_state_db(explicit_path):
        if args.demo:
            return None
        candidates = []
        if explicit_path:
            return Path(explicit_path).expanduser()
        if runtime_system != "windows":
            candidates.extend(
                [
                    Path(__file__).resolve().parent.parent / "hermes-agent" / ".hermes-home" / "state.db",
                    Path("~/.hermes/state.db").expanduser(),
                ]
            )
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def detect_opencode_state_db(explicit_path):
        if args.demo:
            return None
        return opencode_database(explicit_path)

    def register_source(
        system,
        source,
        root_dir,
        sessions_dir,
        db_filename,
        parse_file_fn,
        *,
        file_filter_fn=None,
        ensure_fn=None,
        indexer_factory=None,
        read_only=False,
    ):
        recall_db_path = None
        if system in ("windows", "linux") and source in ("codex", "claude"):
            recall_db_path = data_dir / ("recall_%s_%s.sqlite" % (system, source))
        source_backends[(system, source)] = SourceBackend(
            system=system,
            source=source,
            root_dir=root_dir,
            sessions_dir=sessions_dir,
            data_dir=data_dir,
            db_filename=db_filename,
            parse_file_fn=parse_file_fn,
            parser_version=parser_version(source),
            scan_interval=args.scan_interval,
            archived_dir=Path(root_dir) / "archived_sessions",
            deleted_dir=Path(root_dir) / "deleted_projects",
            recall_db_path=recall_db_path,
            file_filter_fn=file_filter_fn,
            ensure_fn=ensure_fn,
            indexer_factory=indexer_factory,
            read_only=read_only,
        )
        flags = {'--' + source + '-dir', '--' + source + '-state-db', '--' + source + '-sessions-dir'}
        if source == 'codebuddy': flags.add('--cbc-dir')
        configured_env = {'codebuddy': ('CODEBUDDY_CONFIG_DIR',), 'gemini': ('GEMINI_CLI_HOME',),
                          'pi': ('PI_CODING_AGENT_DIR', 'PI_CODING_AGENT_SESSION_DIR'),
                          'prime': ('PRIME_AGENT_CODING_AGENT_DIR', 'PRIME_AGENT_SESSION_DIR')}.get(source, ())
        source_backends[(system, source)].optional_discovery = args.demo or (
            not flags.intersection(supplied_options) and not any(os.environ.get(key) for key in configured_env))

    openclaw_filter = lambda p: p.parent.name == "sessions"
    claude_filter = lambda p: not p.name.startswith("agent-")
    if runtime_system == "windows":
        wsl_home = Path(f"\\\\wsl$\\{args.wsl_distro}\\home\\{args.wsl_user}")
        wsl_codex_dir = Path(args.wsl_codex_dir) if args.wsl_codex_dir else (wsl_home / ".codex")
        wsl_claude_dir = Path(args.wsl_claude_dir) if args.wsl_claude_dir else (wsl_home / ".claude")
        wsl_openclaw_dir = Path(args.wsl_openclaw_dir) if args.wsl_openclaw_dir else (wsl_home / ".openclaw")
        wsl_bootstrapper = WslBootstrapper(args.wsl_distro)

        register_source("windows", "codex", codex_dir, codex_dir / "sessions", "index.sqlite", parse_codex_session_file)
        register_source("windows", "claude", claude_dir, claude_dir / "projects", "index_claude.sqlite", parse_claude_session_file, file_filter_fn=claude_filter)
        register_source("windows", "openclaw", openclaw_dir, openclaw_dir / "agents", "index_openclaw.sqlite", parse_openclaw_session_file, file_filter_fn=openclaw_filter)
        if not args.no_wsl:
            register_source("wsl", "codex", wsl_codex_dir, wsl_codex_dir / "sessions", "index_wsl_codex.sqlite", parse_codex_session_file, ensure_fn=wsl_bootstrapper.ensure)
            register_source("wsl", "claude", wsl_claude_dir, wsl_claude_dir / "projects", "index_wsl_claude.sqlite", parse_claude_session_file, file_filter_fn=claude_filter, ensure_fn=wsl_bootstrapper.ensure)
            register_source("wsl", "openclaw", wsl_openclaw_dir, wsl_openclaw_dir / "agents", "index_wsl_openclaw.sqlite", parse_openclaw_session_file, file_filter_fn=openclaw_filter, ensure_fn=wsl_bootstrapper.ensure)
    else:
        register_source("linux", "codex", codex_dir, codex_dir / "sessions", "index_linux.sqlite", parse_codex_session_file)
        register_source("linux", "claude", claude_dir, claude_dir / "projects", "index_linux_claude.sqlite", parse_claude_session_file, file_filter_fn=claude_filter)
        register_source("linux", "openclaw", openclaw_dir, openclaw_dir / "agents", "index_linux_openclaw.sqlite", parse_openclaw_session_file, file_filter_fn=openclaw_filter)

        for source, subdir in (('codebuddy', 'projects'), ('gemini', 'tmp'), ('pi', 'sessions'), ('prime', 'sessions'), ('copilot', 'session-state'), ('mcode', 'v2/sessions')):
            root = Path(getattr(args, source + '_dir')).expanduser()
            override = getattr(args, source + '_sessions_dir', None)
            sessions = Path(override).expanduser() if override else root / subdir
            register_source('linux', source, root, sessions,
                            'index_linux_' + source + '.sqlite', parser_for(source),
                            file_filter_fn=lambda path, src=source: include_file(src, path),
                            read_only=True)
            if override:
                source_backends[('linux', source)].source_flag = '--' + source + '-sessions-dir'
                source_backends[('linux', source)].source_argument = str(sessions)

        if not args.demo:
            for source in ('zcode', 'agy', 'antigravity'):
                native_db = Path(getattr(args, source + '_state_db')).expanduser()
                register_source('linux', source, native_db.parent, native_db, 'index_' + source + '.sqlite',
                                None, indexer_factory=lambda src=source, path=native_db: (
                                    native_indexer(src, path, data_dir=data_dir / ('native-' + src))
                                    if src in ('agy', 'antigravity') else native_indexer(src, path)), read_only=True)

    hermes_state_db = detect_hermes_state_db(args.hermes_state_db)
    if hermes_state_db:
        hermes_root = hermes_state_db.parent
        register_source(
            runtime_system,
            "hermes",
            hermes_root,
            hermes_state_db,
            "index_hermes.sqlite",
            parse_codex_session_file,
            indexer_factory=lambda: HermesStateIndexer(hermes_state_db),
            read_only=True,
        )

    opencode_state_db = detect_opencode_state_db(args.opencode_state_db)
    if opencode_state_db:
        opencode_root = opencode_state_db.parent
        register_source(
            runtime_system,
            "opencode",
            opencode_root,
            opencode_state_db,
            "index_opencode.sqlite",
            parse_codex_session_file,
            indexer_factory=lambda: OpenCodeIndexer(opencode_state_db),
            read_only=True,
        )

    static_dir = Path(__file__).resolve().parent / "static"
    audit_config = {
        "llm_base_url": args.audit_llm_base_url,
        "llm_model": args.audit_llm_model,
        "llm_api_key": args.audit_llm_api_key,
        "value_threshold": args.audit_value_threshold,
    }

    def handler(*inner_args, **inner_kwargs):
        return Handler(
            *inner_args,
            directory=str(static_dir),
            source_backends=source_backends,
            wsl_distro=args.wsl_distro if runtime_system == "windows" else None,
            runtime_system=runtime_system,
            audit_config=audit_config,
            demo=args.demo,
            material_dir=args.material_dir,
            case_dir=args.case_dir,
            **inner_kwargs,
        )

    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"History viewer running on http://{args.host}:{args.port}")
    for key in sorted(source_backends):
        backend = source_backends[key]
        print(f"{backend.system}/{backend.source}: {backend.root_dir}")
        print(f"  sessions: {backend.sessions_dir}")
        print(f"  index:    {getattr(backend.indexer, 'db_path', 'unavailable')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
