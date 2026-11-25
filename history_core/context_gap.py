"""Bounded, caller-driven context recovery; no storage, model or execution.

The calling Agent decides which information is needed for its current action.
This module validates that declaration, routes unresolved needs, and optionally
calls the existing read-only recall adapter. Historical candidates never close
a gap: the caller must inspect their sources and provide current evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from .evidence import redact_annotation_field, redact_text
from .problem_cases import _case_id, _recall_terms

SCHEMA_VERSION = "history.context-gap.v1"
MAX_NEEDS = 8
MAX_ATTEMPTS = 2
MAX_CALLS = 2
MAX_RECEIPT_BYTES = 512 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_OUTCOMES = {"candidates", "no_matches", "source_required", "unavailable", "partial"}
_LOCATORS = ("id", "kind", "system", "source", "store_id", "session_id", "case_id",
             "message_index", "evidence_id", "index_revision", "content_revision", "context_revision",
             "text_offset", "text_offset_basis", "repository", "path", "commit", "ref",
             "url", "object_ref", "line_start", "line_end", "selected_fragment_hash")


def _text(value, *, maximum=800, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("context_text_invalid")
    return value


def _json(value, maximum=MAX_RECEIPT_BYTES):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > maximum:
            raise ValueError("context_payload_too_large")
        return json.loads(encoded)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("context_payload_invalid_or_too_large") from None


def _redact(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key, item = redact_annotation_field(key, item)
            result[key] = _redact(item)
        return result
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _locator(value):
    if isinstance(value, str):
        value = _text(value, maximum=2048)
        if value.strip().lower() in ("unknown", "none", "null", "n/a", "unverified"):
            raise ValueError("context_evidence_locator_required")
        return redact_text(value)
    if not isinstance(value, dict):
        raise ValueError("context_evidence_invalid")
    result = {}
    for key in _LOCATORS:
        item = value.get(key)
        if isinstance(item, str) and item.strip() and len(item) <= 1024:
            result[key] = redact_text(item)
        elif type(item) is int and 0 <= item < 2 ** 63:
            result[key] = item
    if not result:
        raise ValueError("context_evidence_locator_required")
    direct = any(isinstance(result.get(key), str) for key in ("path", "ref", "url", "object_ref", "case_id"))
    direct = direct or all(isinstance(result.get(key), str) for key in ("repository", "commit"))
    message = type(result.get("message_index")) is int or isinstance(result.get("evidence_id"), str)
    if not direct and not message:
        raise ValueError("context_evidence_locator_required")
    if message and not direct and (any(not isinstance(result.get(key), str) for key in ("source", "store_id", "session_id"))
                                  or not any(isinstance(result.get(key), str) for key in ("index_revision", "content_revision", "context_revision"))):
        result["partial"] = True
    if isinstance(value.get("text"), str):
        result["text"] = redact_text(value["text"])[:160]
    if value.get("partial") or value.get("truncated") or value.get("locator_partial"):
        result["partial"] = True
    return result


def _request(request):
    request = _json(request, 64 * 1024)
    if not isinstance(request, dict) or request.get("schema_version", "history.context-request.v1") != "history.context-request.v1":
        raise ValueError("context_request_version_invalid")
    task = _text(request.get("task"), maximum=1000)
    action = _text(request.get("next_action"), maximum=1000)
    project = _text(request.get("project"), maximum=4096, optional=True)
    revision = _text(request.get("state_revision"), maximum=512, optional=True)
    needs = request.get("needs")
    if not isinstance(needs, list) or len(needs) > MAX_NEEDS:
        raise ValueError("context_needs_invalid")
    clean, seen = [], set()
    for need in needs:
        if not isinstance(need, dict) or not isinstance(need.get("id"), str) or not _ID.fullmatch(need["id"]):
            raise ValueError("context_need_id_invalid")
        if need["id"] in seen:
            raise ValueError("context_need_id_duplicate")
        seen.add(need["id"])
        if need.get("kind") not in ("history", "project", "tool"):
            raise ValueError("context_need_kind_invalid")
        if need.get("status") not in ("missing", "stale", "conflicting", "available"):
            raise ValueError("context_need_status_invalid")
        evidence = need.get("evidence", [])
        if not isinstance(evidence, list) or len(evidence) > 8:
            raise ValueError("context_evidence_invalid")
        evidence = [_locator(value) for value in evidence]
        if need["status"] == "available" and not evidence:
            raise ValueError("context_available_requires_evidence")
        if need["status"] == "available" and any(isinstance(value, dict) and value.get("partial") for value in evidence):
            raise ValueError("context_available_evidence_partial")
        clean.append({"id": need["id"], "kind": need["kind"], "status": need["status"],
                      "question": _text(need.get("question"), maximum=1000),
                      "reason": _text(need.get("reason")),
                      "required_evidence": _text(need.get("required_evidence")),
                      "query": _text(need.get("query"), maximum=300, optional=True), "evidence": evidence})
    return task, action, project, revision, clean


def _candidates(items, origin, context=None):
    if not isinstance(items, list):
        raise ValueError("context_retrieval_items_invalid")
    result = []
    for item in items[:3]:
        if not isinstance(item, dict):
            raise ValueError("context_retrieval_item_invalid")
        if origin == "history" and context:
            item = {**{key: context[key] for key in ("system", "source", "store_id", "index_revision") if key in context},
                    **item, "session_id": item.get("session_id") or item.get("id")}
        candidate = {"origin": origin, "locator_partial": bool(item.get("locator_partial"))}
        for key in ("id", "title", "system", "session_id", "source", "store_id", "index_revision"):
            value = item.get(key)
            if isinstance(value, str) and value.strip() and len(value) <= 1024:
                candidate[key] = redact_text(value)
        if origin == "case":
            try:
                candidate["expand"] = {"operation": "cases show", "case_id": _case_id(candidate.get("id"))}
            except ValueError:
                candidate["locator_partial"] = True
        evidence = item.get("evidence", item.get("snippets", item.get("excerpts", [])))
        if not isinstance(evidence, list):
            raise ValueError("context_retrieval_evidence_invalid")
        candidate["evidence"] = []
        for value in evidence[:3]:
            try:
                if origin == "history" and isinstance(value, dict):
                    value = {**{key: candidate[key] for key in ("system", "source", "store_id", "session_id", "index_revision") if key in candidate}, **value}
                    if "text_offset" not in value and type(value.get("excerpt_start")) is int:
                        value["text_offset"] = value["excerpt_start"]
                locator = _locator(value)
                if origin == "history" and (not isinstance(locator, dict) or
                        any(not isinstance(locator.get(key), str) or not locator[key] for key in
                            ("system", "source", "store_id", "session_id", "index_revision")) or
                        type(locator.get("message_index")) is not int):
                    candidate["locator_partial"] = True
                candidate["evidence"].append(locator)
            except ValueError:
                candidate["locator_partial"] = True
        if origin == "history" and not candidate["evidence"]:
            candidate["locator_partial"] = True
        candidate["partial"] = bool(item.get("partial") or item.get("snippets_partial")
                                    or item.get("candidate_match_status") == "unverified" or candidate.get("locator_partial"))
        candidate["current_verification"] = "not_performed"
        # Never copy a historical command/next_action into a current action.
        if len(json.dumps(candidate, ensure_ascii=False).encode()) > 4096:
            candidate["evidence"] = []
            candidate["locator_partial"] = candidate["partial"] = True
        result.append(candidate)
    return result


def _diagnostic_items(values):
    if not isinstance(values, list):
        raise ValueError("context_diagnostics_invalid")
    result = []
    for value in values[:6]:
        if isinstance(value, str):
            value = {"code": "source_notice", "detail": value}
        if not isinstance(value, dict):
            raise ValueError("context_diagnostic_invalid")
        code = value.get("code", value.get("error_code", value.get("error", "source_notice")))
        detail = value.get("detail", value.get("message"))
        item = {"code": redact_text(code)[:80] if isinstance(code, str) else "source_notice"}
        if isinstance(detail, str):
            item["detail"] = redact_text(detail)[:240]
        if item not in result:
            result.append(item)
    return result


def _diagnostics(result, history):
    values = []
    if isinstance(result.get("history_error"), dict):
        values.append(result["history_error"])
    for source in (result, history):
        for key in ("errors", "warnings", "limitations"):
            if isinstance(source.get(key), list):
                values.extend(source[key][:6])
    if history.get("refresh_required") is True:
        values.append({"code": "history_refresh_required", "detail": "The selected index needs an explicit refresh; context recovery does not refresh or widen source scope."})
    for key in ("index_state", "coverage"):
        if isinstance(history.get(key), str):
            values.append({"code": "history_" + key, "detail": history[key]})
    coverage = history.get("source_coverage")
    if isinstance(coverage, dict):
        scalar = {key: coverage[key] for key in ("partial", "complete", "partial_sessions", "metadata_only_sessions", "indexed_sessions", "status")
                  if key in coverage and type(coverage[key]) in (bool, int, str)}
        if scalar:
            values.append({"code": "history_source_coverage", "detail": json.dumps(_redact(scalar), ensure_ascii=False)})
    diagnostics = _diagnostic_items(values)
    if len(values) > 6:
        diagnostics[-1:] = [{"code": "diagnostics_truncated", "detail": "Inspect the selected source or complete recall result for further diagnostics."}]
    return diagnostics


def _summary(result):
    result = _json(result, 256 * 1024)
    if not isinstance(result, dict) or result.get("schema_version") != "history.case-recall.v1":
        raise ValueError("context_retrieval_version_invalid")
    history = result.get("history", {})
    if not isinstance(history, dict):
        raise ValueError("context_retrieval_history_invalid")
    cases = _candidates(result.get("items", []), "case")
    historical = _candidates(history.get("items", []), "history", history)
    # A fallback was performed because cases were insufficient. Reserve space
    # for its evidence instead of letting three partial cases hide every hit.
    candidates = cases[:2] + historical[:3 - min(len(cases), 2)] if historical else cases[:3]
    partial = bool(result.get("partial") or history.get("partial") or
                   any(item["partial"] for item in cases + historical))
    if candidates:
        outcome = "candidates"
    elif result.get("fallback_status") in ("source_required", "unavailable"):
        outcome = result["fallback_status"]
    else:
        outcome = "partial" if partial else "no_matches"
    return {"outcome": outcome, "partial": partial, "candidates": candidates,
            "diagnostics": _diagnostics(result, history)}


def _previous(previous, scope_id):
    if previous is None:
        return []
    previous = _json(previous)
    if not isinstance(previous, dict) or previous.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("context_previous_version_invalid")
    attempts = previous.get("attempts")
    if not isinstance(attempts, list) or len(attempts) > MAX_NEEDS * MAX_ATTEMPTS:
        raise ValueError("context_previous_attempts_invalid")
    clean = []
    for item in attempts:
        if not isinstance(item, dict):
            raise ValueError("context_previous_attempt_invalid")
        for field in ("fingerprint", "scope_fingerprint"):
            if not isinstance(item.get(field), str) or not _HASH.fullmatch(item[field]):
                raise ValueError("context_previous_identity_invalid")
        if item.get("outcome") not in _OUTCOMES or type(item.get("partial")) is not bool:
            raise ValueError("context_previous_outcome_invalid")
        query = _text(item.get("query"), maximum=300)
        if not isinstance(item.get("candidates"), list) or len(item["candidates"]) > 3:
            raise ValueError("context_previous_candidates_invalid")
        # Re-project even an apparently valid receipt: no saved status or action
        # can grant authority or assert current verification.
        candidates = []
        for candidate in item["candidates"]:
            if not isinstance(candidate, dict) or candidate.get("origin") not in ("case", "history"):
                raise ValueError("context_previous_candidate_invalid")
            candidates.extend(_candidates([candidate], candidate["origin"]))
        if (item["outcome"] == "candidates") != bool(candidates):
            raise ValueError("context_previous_candidates_mismatch")
        if item["scope_fingerprint"] == scope_id:
            clean.append({"fingerprint": item["fingerprint"], "scope_fingerprint": scope_id,
                          "query": redact_text(query), "outcome": item["outcome"],
                          "partial": bool(item["partial"] or any(candidate["partial"] for candidate in candidates)),
                          "candidates": candidates, "diagnostics": _diagnostic_items(item.get("diagnostics", []))})
    return clean


def _query(task, need):
    query = redact_text(need["query"] or f"{task}: {need['question']}")
    try:
        _recall_terms(query)
    except ValueError:
        raise ValueError("context_query_requires_short_self_contained_restatement") from None
    return query


def _apply_attempt(gap, attempt):
    gap.update(outcome=attempt["outcome"], partial=attempt["partial"], candidates=attempt["candidates"], diagnostics=attempt["diagnostics"])
    if attempt["outcome"] == "candidates":
        gap.update(status="needs_review", next_action="Open candidate sources and check the required evidence against the current task before resuming.")
    else:
        actions = {"source_required": "Select one explicit history source or provide the missing evidence; a catalog miss does not prove historical absence.",
                   "unavailable": "Inspect the selected source failure or supply evidence before retrying; do not broaden scope automatically.",
                   "partial": "Inspect the reported source coverage and obtain the missing evidence; incomplete retrieval cannot settle this gap.",
                   "no_matches": "Supply evidence or revise the bounded query once; no match does not prove that the information never existed."}
        gap.update(status="blocked", next_action=actions[attempt["outcome"]])


def recover_context(request, *, previous=None, retrieve=None, scope=None):
    """Return a plan/receipt; ``retrieve`` is an optional bounded recall callback.

    ``state_revision`` and ``scope`` identify the caller's current context/source
    state. Change them only when relevant evidence or source scope really changes.
    Receipts are continuation hints, not authenticated history or permissions.
    """
    task, action, project, revision, needs = _request(request)
    scope = _json({} if scope is None else scope, 16 * 1024)
    if not isinstance(scope, dict) or len(scope) > 16:
        raise ValueError("context_scope_invalid")
    if any(not isinstance(key, str) or len(key) > 80 or
           not (value is None or isinstance(value, str) and len(value) <= 4096)
           for key, value in scope.items()):
        raise ValueError("context_scope_fields_invalid")
    scope_id = _digest([task, action, project, revision, scope])
    attempts = _previous(previous, scope_id)
    active = [need for need in needs if need["status"] != "available"]
    # Validate every planned query before the first callback. A late validation
    # error must not discard an already performed retrieval and its receipt.
    queries = {need["id"]: _query(task, need) for need in active
               if need["kind"] == "history" and need["status"] == "missing"}
    fingerprints = {need["id"]: _digest([scope_id, need["id"], need["kind"], need["required_evidence"]]) for need in active}
    attempts = [item for item in attempts if item["fingerprint"] in fingerprints.values()]
    if any(sum(item["fingerprint"] == fingerprint for item in attempts) > MAX_ATTEMPTS for fingerprint in fingerprints.values()):
        raise ValueError("context_previous_attempt_limit_invalid")
    gaps, calls = [], 0
    for need in active:
        gap = {key: need[key] for key in ("id", "kind", "question", "reason", "required_evidence", "evidence")}
        gap.update(fingerprint=fingerprints[need["id"]], query=None, candidates=[], partial=False, reused=False)
        if need["status"] in ("stale", "conflicting") or need["kind"] == "project":
            gap.update(route="current_source", status="needs_current_evidence",
                       next_action="Read the relevant current project/SpecMesh source and resolve missing, stale or conflicting evidence before resuming.")
        elif need["kind"] == "tool":
            gap.update(route="tool_capability", status="needs_tool",
                       next_action="Inspect the selected tool's available capability and required inputs; history cannot authorize or execute it.")
        else:
            query = queries[need["id"]]
            gap.update(route="case_recall_then_explicit_history", query=query)
            prior = [item for item in attempts if item["fingerprint"] == gap["fingerprint"]]
            same = next((item for item in reversed(prior) if item["query"] == query), None)
            if same:
                _apply_attempt(gap, same)
                gap["reused"] = True
            elif len(prior) >= MAX_ATTEMPTS:
                gap.update(status="blocked", outcome="attempt_limit",
                           next_action="Stop repeating this gap. Obtain new evidence or a changed source/context state before another retrieval.")
            elif retrieve is None or calls >= MAX_CALLS:
                gap.update(status="needs_retrieval", outcome="not_requested" if retrieve is None else "call_budget",
                           next_action="Use the stated query and exact scope with the existing recall adapter; carry this receipt into the next bounded call.")
            else:
                calls += 1
                try:
                    summary = _summary(retrieve(query=query, project=project, limit=3))
                except (ValueError, OSError, TypeError, KeyError, RecursionError, sqlite3.Error) as exc:
                    summary = {"outcome": "unavailable", "partial": True, "candidates": [],
                               "diagnostics": _diagnostic_items([{"code": type(exc).__name__, "detail": str(exc)}])}
                attempt = {"fingerprint": gap["fingerprint"], "scope_fingerprint": scope_id, "query": query, **summary}
                attempts.append(attempt)
                _apply_attempt(gap, attempt)
        gaps.append(gap)
    states = {gap["status"] for gap in gaps}
    status = next((value for value in ("blocked", "needs_current_evidence", "needs_tool", "needs_retrieval", "needs_review") if value in states), "no_gap")
    result = {"schema_version": SCHEMA_VERSION, "authorization": "context_only", "current_verification": "not_performed",
              "status": status, "task": task, "next_action": action, "project": project, "state_revision": revision,
              "scope": scope, "scope_fingerprint": scope_id, "gaps": gaps, "attempts": attempts,
              "retrieval_calls": calls, "limits": {"needs": MAX_NEEDS, "attempts_per_gap": MAX_ATTEMPTS, "calls_per_run": MAX_CALLS},
              "basis": "caller_declared_needs_not_semantic_completeness",
              "freshness": "caller_state_revision_not_source_revalidated",
              "resume_condition": "Resolve every listed need with current source evidence before resuming the requested next_action."}
    return _json(_redact(result))
