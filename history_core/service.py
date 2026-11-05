"""Shared read model for machine callers and the optional human workspace."""
from datetime import datetime, timezone
import sqlite3

from audit.handoff import build_handoff_bundle
from .provenance import selected_audit, validate_native_size


def _stage(name, action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except Exception as error:
        error.phase = name
        raise


def _native_snippets(*args, **kwargs):
    from .compact_search import native_snippets
    return native_snippets(*args, **kwargs)


def read_native_message(*args, **kwargs):
    from .compact_search import read_native_message as read
    return read(*args, **kwargs)


def _small_source_error(error):
    from .evidence import _redact
    keys = ('error_code', 'path', 'size_bytes', 'limit_bytes', 'budget_remaining_bytes',
            'reason', 'step_count', 'limit_steps')
    return {key: _redact(value)[:240] if isinstance(value, str) else value
            for key in keys if key in error for value in [error[key]]}


def search(indexer, *, query=None, limit=20, offset=0, cwd=None, stable_order=False, brief=False,
           session_id=None, role=None, field=None):
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("invalid_pagination")
    if role is not None and role not in ('user', 'assistant', 'tool'):
        raise ValueError('invalid_search_role')
    if field not in (None, 'files_touched'):
        raise ValueError('invalid_search_field')
    if (session_id is not None or role is not None or field is not None) and not brief:
        raise ValueError('search_scope_requires_brief')
    from .providers import uses_message_index
    indexed = uses_message_index(indexer) if brief else True
    literal = field == 'files_touched'
    if literal and not str(query or '').strip():
        raise ValueError('filename_query_required')
    search_error = None
    if brief and (not indexed or literal):
        from .compact_search import session_page
        try:
            result = _stage('search', session_page, indexer, query=query, limit=limit, offset=offset,
                            cwd=cwd, session_id=session_id, role=role, literal=literal)
        except (OSError, ValueError, sqlite3.Error) as error:
            if indexed:
                raise
            from .compact_search import metadata_page
            from .reuse import error_code
            search_error = {'error_code': error_code(error), 'phase': 'search',
                            'query_applied': False, 'role_applied': role is None,
                            'field_applied': field is None}
            result = _stage('search_metadata', metadata_page, indexer, limit=limit, offset=offset,
                            cwd=cwd, session_id=session_id)
            result.update(partial=True, limitations=[search_error])
            if role is not None:
                result['unsupported_scope'] = ['role']
    else:
        kwargs = {"stable_order": True} if stable_order else {}
        if session_id is not None or role is not None:
            kwargs.update(session_id=session_id, role=role)
        result = _stage('search', indexer.list_sessions_page, q=query, limit=limit, offset=offset, cwd=cwd,
                        sort='tool_evidence' if brief else 'last', **kwargs)
    if not brief:
        return result
    from .reuse import _snippets, _indexed_snippets, _related_tool_messages, MAX_RELATED_TOOL_MESSAGES, error_code
    from .evidence import redact_text, _redact
    items = []
    coverage = getattr(indexer, '_coverage', {})
    for row in result['items']:
        limitations = []
        snippet_error = None
        if indexed:
            if literal:
                snippets, partial = _stage('snippets', _indexed_snippets, indexer, row['id'],
                                          str(query or ''), [str(query)] if query else [], role=role)
            else:
                snippets, partial = _stage('snippets', _snippets, indexer, row['id'], query or '',
                                          **({'role': role} if role is not None else {}))
        else:
            try:
                snippets, partial = _stage('snippets', _native_snippets, indexer, row['id'], query or '',
                                          role=role, literal=literal)
            except (OSError, ValueError, sqlite3.Error) as error:
                snippet_error = error_code(error)
                snippets, partial = [], True
                limitations.append({'error_code': snippet_error, 'phase': 'snippets'})
        related_error = None
        if indexed:
            try:
                related, related_truncated = _stage('related_tool_messages', _related_tool_messages, indexer, row['id'], snippets)
            except sqlite3.OperationalError as error:
                if error_code(error) != 'query_budget_exceeded':
                    raise
                related, related_truncated, partial = [], False, True
                related_error = {'error_code': 'query_budget_exceeded', 'phase': 'related_tool_messages'}
        else:
            related, related_truncated = [], False
            if any(item.get('kind') in ('tool_use', 'tool_result') for item in snippets):
                limitations.append({'error_code': 'native_tool_pairing_unavailable'})
                partial = True
        candidate = {key: row.get(key) for key in ('id', 'title', 'cwd', 'start_ts_ms', 'end_ts_ms')}
        candidate['tool_match_terms'] = row.get('tool_match_terms', 0)
        if search_error:
            candidate['candidate_match_status'] = 'unverified'
        content = coverage.get(str(row['id']), {})
        title_candidates = content.get('title_candidates') or row.get('title_candidates')
        if title_candidates:
            candidate['title_candidates'] = _redact(title_candidates[:2])
        status = row.get('content_status') or content.get('content_status')
        if status:
            candidate['content_status'] = status
            candidate['unsupported_steps_count'] = len(row.get('unsupported_steps') or content.get('unsupported_steps') or [])
            partial = partial or status != 'decoded_text'
        if row.get('native_oversized_records'):
            from .compact_search import MAX_NATIVE_PUBLIC_BYTES
            limitations.append({'error_code': 'native_public_message_limit_exceeded',
                                'messages': row['native_oversized_records'], 'limit_bytes': MAX_NATIVE_PUBLIC_BYTES})
            partial = True
            if not snippets:
                snippet_error = 'native_public_message_limit_exceeded'
                candidate['candidate_match_status'] = 'unverified'
        source_error = row.get('source_error') or content.get('source_error')
        if source_error:
            candidate['source_error'] = _small_source_error(source_error)
            partial = True
        for key, maximum in (('title', 160), ('cwd', 240)):
            if isinstance(candidate[key], str):
                candidate[key] = redact_text(candidate[key])[:maximum]
        candidate.update(snippets=_redact(snippets), snippets_partial=partial,
                         snippet_status='unavailable' if snippet_error or (status and status != 'decoded_text' and not snippets) else 'matched' if snippets else
                                        'query_not_provided' if not query else 'no_public_match',
                         related_tool_messages=_redact(related), related_tool_messages_truncated=related_truncated)
        if limitations:
            candidate['limitations'] = limitations
        if related_error:
            candidate['related_tool_messages_error'] = related_error
        items.append(candidate)
    incomplete = sum(entry.get('content_status') != 'decoded_text' for entry in coverage.values())
    compact = dict(result, items=items, mode='brief', related_tool_messages_limit=MAX_RELATED_TOOL_MESSAGES,
                   partial=bool(result.get('partial') or incomplete or any(item['snippets_partial'] for item in items)))
    if session_id is not None or role is not None:
        compact['scope'] = {'session_id': session_id, 'role': role}
    if literal:
        compact.update(field=field, match_semantics='literal_public_record_reference',
                       evidence_authority='path_reference_not_operation_verification')
    elif not indexed:
        compact['evidence_authority'] = 'native_public_excerpts'
    if search_error:
        compact.update(evidence_authority='unverified_session_metadata', match_semantics='not_applied')
    if incomplete:
        failures = [(sid, entry['source_error']) for sid, entry in coverage.items() if entry.get('source_error')]
        warning = {'source': indexer.source, 'incomplete_sessions': incomplete}
        if failures:
            warning.update(failed_sessions=len(failures),
                           source_errors=[dict(session_id=sid, **_small_source_error(error)) for sid, error in failures[:5]],
                           source_errors_truncated=len(failures)>5)
        compact['content_warnings'] = [warning]
    return compact


def audit_handoff(indexer, session_id):
    validate_native_size(indexer, session_id)
    metadata = indexer.get_session_metadata(session_id)
    if metadata is None:
        raise ValueError("session_not_found")
    audit, provenance = selected_audit(indexer, session_id)
    if audit is None:
        raise ValueError("audit_not_supported_or_unavailable")
    result = build_handoff_bundle(audit, metadata=metadata, provenance=provenance)
    result["schema_version"] = "history.handoff.v1"
    result["authorization"] = "context_only"
    return audit, result


def handoff(indexer, session_id, *, include_plans=False):
    audit, result = audit_handoff(indexer, session_id)
    if include_plans:
        metadata = indexer.get_session_metadata(session_id)
        from pathlib import Path
        from .sources import scan_plan_files
        from .evidence import redact_text
        from audit.handoff import render_handoff
        cwd = metadata.get("cwd") or ""
        plans = scan_plan_files(cwd) if cwd and Path(cwd).is_absolute() else []
        result["payload"]["specmesh"] = {
            "status": "candidate_context" if plans else "missing", "authority": "unverified_project_files",
            "files": [{"path": redact_text(p.get("rel_path") or ""), "mtime_ms": p.get("mtime_ms"),
                       "sections": {redact_text(str(k)): redact_text(str(v))[:600]
                                    for k, v in (p.get("sections") or {}).items()}}
                      for p in plans[:3]],
            "max_files": 3, "max_section_chars": 600,
        }
        result["compact"] = render_handoff(result["payload"], "compact")
        result["standard"] = render_handoff(result["payload"], "standard")
    result["schema_version"] = "history.handoff.v1"
    result["authorization"] = "context_only"
    return result


def workspace(indexers, limit=12):
    """History-derived work, never claimed to be live CM task state."""
    candidates, errors = [], []
    for system, source, indexer in indexers:
        try:
            page = search(indexer, limit=limit)
            for row in page["items"]:
                candidates.append((row.get("end_ts_ms") or row.get("start_ts_ms") or 0,
                                   system, source, indexer, row))
        except Exception:
            errors.append({"system": system, "source": source, "error": "source_unavailable"})
    candidates.sort(key=lambda item: item[0], reverse=True)
    work = []
    for updated, system, source, indexer, row in candidates[:limit]:
        item = {"id": row["id"], "system": system, "source": source,
                "title": row.get("title") or row["id"], "project": row.get("cwd") or None,
                "updated_at": updated, "status": "unknown", "goal": None,
                "next_action": "核对当前项目状态后继续", "verified": [], "changed": [], "remaining": [], "blockers": [], "decisions": [], "evidence": [],
                "origin": "historical_evidence", "live_task_state": "unknown"}
        try:
            payload = handoff(indexer, row["id"])["payload"]
            for key in ("goal", "status", "verified", "changed", "remaining", "blockers", "decisions", "evidence"):
                item[key] = payload.get(key, item[key])
            item["next_action"] = payload.get("next_action") or item["next_action"]
        except (ValueError, OSError):
            item["evidence_status"] = "unavailable"
        work.append(item)
    return {"schema_version": "history.workspace.v1", "mode": "history_snapshot",
            "observed_at": datetime.now(timezone.utc).isoformat(), "work": work, "errors": errors}
