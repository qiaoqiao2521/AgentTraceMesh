"""Evidence-linked problem drafts from bounded public conversation windows.

The caller pins the index revision/read view. This module neither opens raw
recordings nor calls a model, executes history, or decides that a task is done.
User-message boundaries and wording are retrieval hints, not semantic verdicts.
"""
from __future__ import annotations

import hashlib
import json
import re

from .compact_search import NATIVE_PUBLIC_SOURCES, _native_projection, read_native_message
from .evidence import _redact, redact_indexed_window, redact_text
from .indexed_materials import _source_capture
from .message_ordinals import ordered_message_ids_sql
from .messages import BODY_PAGE_CHARS, METADATA_CHARS, _display_metadata, _object
from .projection import PLACEHOLDER
from .providers import uses_message_index
from .reuse import query_budget
from .sources import _indexed_public_message_sql


SCHEMA_VERSION = "history.problem-compression.v1"
MAX_MESSAGES = 100
EXCERPT_CHARS = 360
_NEW_REQUEST = re.compile(r"^(?:另外|另一个|新问题|还有一个|第二个|next question\b|another (?:question|task)\b|separately\b)", re.I)
_FOLLOWUP = re.compile(r"^(?:不对|不是|不行|还是|仍然|刚才|之前|其实|改成|应该|你(?:没有|没|又|理解)|继续|接着|再试|按这个|就这个|对[，。！!\s]|好[，。！!\s]|可以[，。！!\s]|continue\b|proceed\b|actually\b|instead\b|still\b|no[,!\s]|yes[,!\s])", re.I)
_FRICTION = re.compile(r"不对|不行|卡住|报错|失败|没用|未解决|反复|重复|\b(?:failed|failure|error|stuck|retry|broken)\b", re.I)
_STOP = re.compile(r"^(?:(?:先|现在|请)\s*)?(?:停止|暂停|取消|别再|不要继续|等一下)|^(?:stop|pause|cancel|hold on|wait)\b", re.I)
_WAIT = re.compile(r"等待(?:你|用户|权限|批准|输入|确认)|需要(?:你|用户).{0,12}(?:提供|确认|授权)|\b(?:waiting for|awaiting)\b", re.I)
_COMPLETION = re.compile(r"已(?:经)?完成|完成了|改好了|修好了|测试通过|全部通过|\b(?:done|fixed|completed|tests? passed)\b", re.I)
_SOLUTION = re.compile(r"解决|原因|修复|改用|换成|方案|```|\b(?:solution|because|fix|workaround|instead)\b", re.I)


def _short(text, limit=EXCERPT_CHARS):
    clean = str(text or "")
    return clean if len(clean) <= limit else clean[:limit - 1] + "…"


def _visible_summary(value):
    """Keep compact structured result facts; detailed tool text has a locator."""
    result = {}
    for key in ("name", "category", "headline", "file_path", "change_kind", "exit_status", "exit_code", "is_error"):
        item = value.get(key)
        if isinstance(item, str):
            result[key] = _short(item, 160)
        elif item is None or type(item) in (bool, int):
            result[key] = item
    return result


def _signal(kind, message, pattern, *, basis="wording"):
    """Quote the actual match in the already-masked window, with source offsets."""
    text = message["text"]
    match = pattern.search(text)
    start = max(0, match.start() - 120) if match else 0
    excerpt = text[start:start + EXCERPT_CHARS]
    return {"kind": kind, "basis": basis, "actor": message["role"],
            "text": excerpt, "text_offset": start,
            "text_offset_basis": message["text_offset_basis"],
            "evidence": [f"m{message['message_index']}"]}


def _capture_metadata(raw):
    """Keep projection loss separate from the physical captured-prefix status."""
    result = _source_capture(raw)
    value = _object(raw).get("source_capture")
    if not isinstance(value, dict):
        return result
    for key in ("projection_version", "image_fields", "lossy_fields", "omitted_bytes",
                "omitted_lines", "skipped_lines", "complete_lines"):
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    for key in ("capture_mode", "index_coverage", "audit_coverage"):
        if isinstance(value.get(key), str) and len(value[key]) <= 128:
            result[key] = redact_text(value[key])
    return result


def _indexed_page(indexer, session_id, start, limit):
    """Number lightweight keys first, then redact only selected public bodies."""
    with query_budget(indexer) as conn:
        session = conn.execute("SELECT SUBSTR(cwd,1,4096) AS cwd, "
                               "SUBSTR(activity_metadata_json,1,?) AS metadata "
                               "FROM sessions WHERE id=?", (METADATA_CHARS, session_id)).fetchone()
        if session is None:
            raise ValueError("session_not_found")
        ordered, args = ordered_message_ids_sql(conn, session_id)
        selected = conn.execute(f"""WITH ordered AS ({ordered})
            SELECT m.id,o.message_index FROM ordered o JOIN messages m ON m.id=o.id
            WHERE m.session_id=? AND o.message_index>=? AND {_indexed_public_message_sql('m')}
            ORDER BY o.message_index LIMIT ?""", [*args, session_id, start, limit + 1]).fetchall()
        more, selected = len(selected) > limit, selected[:limit]
        positions = {row["id"]: row["message_index"] for row in selected}
        messages = []
        if selected:
            conn.create_function("hv_compress_chars", 1, lambda value: len(value or ""), deterministic=True)
            conn.create_function("hv_compress_window", 3, redact_indexed_window, deterministic=True)
            conn.create_function("hv_compress_metadata", 1, _display_metadata, deterministic=True)
            placeholders = ",".join("?" for _ in selected)
            rows = conn.execute(f"""SELECT m.id,m.role,m.kind,m.ts_ms,
                hv_compress_chars(m.text) AS char_count,
                hv_compress_window(m.text,0,?) AS text,
                SUBSTR(m.tool_summary_json,1,?) AS tool_summary_json,
                hv_compress_chars(m.tool_summary_json)>? AS tool_summary_truncated,
                hv_compress_metadata(m.activity_meta_json) AS metadata
                FROM messages m WHERE m.session_id=? AND m.id IN ({placeholders})
                AND {_indexed_public_message_sql('m')}""",
                [BODY_PAGE_CHARS, METADATA_CHARS, METADATA_CHARS, session_id, *positions]).fetchall()
            for row in rows:
                meta = _object(row["metadata"])
                size = row["char_count"]
                messages.append({
                    "message_index": positions[row["id"]], "role": row["role"], "kind": row["kind"],
                    "ts_ms": row["ts_ms"], "text": row["text"], "char_count": size,
                    "text_offset": 0, "text_offset_basis": "indexed_text",
                    "next_text_offset": BODY_PAGE_CHARS if size > BODY_PAGE_CHARS else None,
                    "truncated": size > BODY_PAGE_CHARS,
                    "call_id": meta.get("tool_call_id"),
                    "call_id_truncated": bool(meta.get("call_id_truncated")),
                    "tool_summary": _redact(_object(row["tool_summary_json"])),
                    "tool_summary_truncated": bool(row["tool_summary_truncated"]),
                })
    messages.sort(key=lambda item: item["message_index"])
    provider = getattr(indexer, "_coverage", {}).get(session_id, {})
    coverage = {"content_status": provider.get("content_status", "unknown"),
                "unsupported_steps_count": len(provider.get("unsupported_steps") or [])}
    errors = []
    if provider and provider.get("content_status") != "decoded_text":
        errors.append("source_content_partial")
    return messages, more, selected[-1]["message_index"] + 1 if more else None, {
        "project": redact_text(str(session["cwd"] or "unknown")),
        "source_capture": _capture_metadata(session["metadata"]),
        "source_coverage": coverage, "reasons": errors,
    }


def _native_page(indexer, session_id, start, limit):
    """Reuse native public renderers; never fall back to private/raw records."""
    if indexer.source not in NATIVE_PUBLIC_SOURCES:
        raise ValueError("problem_compression_source_unsupported")
    table = "session" if indexer.source in ("opencode", "zcode") else "sessions"
    with query_budget(indexer) as conn:
        project = "directory" if table == "session" else "''"
        row = conn.execute(f"SELECT SUBSTR({project},1,4096) AS cwd FROM {table} WHERE id=?",
                           (session_id,)).fetchone()
        if row is None:
            raise ValueError("session_not_found")
        cte, args = _native_projection(indexer, conn, session_id, records_only=True)
        selected = conn.execute(cte + " SELECT message_index FROM native_records "
                                "WHERE is_public=1 AND message_index>=? "
                                "ORDER BY message_index LIMIT ?", [*args, start, limit + 1]).fetchall()
        missing_time = conn.execute(cte + " SELECT 1 FROM native_records WHERE ts_ms IS NULL LIMIT 1", args).fetchone()
    more, selected = len(selected) > limit, selected[:limit]
    messages, errors = [], []
    if missing_time:
        errors.append("native_untimestamped_slots_not_projected")
    for item in selected:
        try:
            messages.append(read_native_message(indexer, session_id, item["message_index"]))
        except ValueError as exc:
            # One oversized/unsupported public part does not erase other cases.
            errors.append({"message_index": item["message_index"], "code": str(exc)[:160]})
    return messages, more, selected[-1]["message_index"] + 1 if more else None, {
        "project": redact_text(str(row["cwd"] or "unknown")),
        "source_capture": {"status": "not_applicable_native_projection"},
        "source_coverage": {"public_projection": "timestamped_native_public_records",
                            "tool_call_identity": "not_exposed_by_existing_renderer"},
        "reasons": errors,
    }


def _followup(text):
    text = text.strip()
    if _NEW_REQUEST.search(text):
        return False
    if text.lower() in {"继续", "继续吧", "好", "好的", "可以", "对", "不对", "continue", "yes", "no", "ok"}:
        return True
    return bool(_FOLLOWUP.search(text) or _STOP.search(text))


def _safe_call_id(message):
    identity = message.get("call_id")
    if not isinstance(identity, str) or not identity:
        return None, "missing_call_id"
    if PLACEHOLDER in identity:
        return None, "projected_call_id_unavailable"
    # Redaction/truncation can make unrelated IDs equal. Never pair those IDs.
    if (message.get("call_id_truncated") or "[REDACTED]" in identity
            or "***" in identity or redact_text(identity) != identity):
        return None, "redacted_or_truncated_call_id"
    return identity, None


def _tool_status(message):
    if message.get("kind") != "tool_result":
        return "unknown"
    summary = message.get("tool_summary") or {}
    status, code = summary.get("exit_status"), summary.get("exit_code")
    if summary.get("is_error") is True or status in ("error", "failed", "failure"):
        return "error"
    if type(code) is int:
        return "ok" if code == 0 else "error"
    return "ok" if status in ("ok", "success", "succeeded", "pass") else "unknown"


def _attempts(messages, all_messages):
    """Only unique complete IDs in this page and segment establish a pair."""
    by_call = {}
    for item in all_messages:
        if item["kind"] not in ("tool_use", "tool_result"):
            continue
        identity, _ = _safe_call_id(item)
        if identity is not None:
            by_call.setdefault(identity, {"tool_use": [], "tool_result": []})[item["kind"]].append(item)
    positions = {item["message_index"] for item in messages}
    consumed, result = set(), []
    for item in messages:
        index = item["message_index"]
        if index in consumed or item["kind"] not in ("tool_use", "tool_result"):
            continue
        identity, issue = _safe_call_id(item)
        pairing, call, output = issue, None, None
        if identity is not None:
            group = by_call[identity]
            if len(group["tool_use"]) > 1 or len(group["tool_result"]) > 1:
                pairing = "ambiguous_call_id"
            elif group["tool_use"] and group["tool_result"]:
                candidate_call, candidate_output = group["tool_use"][0], group["tool_result"][0]
                if any(candidate["message_index"] not in positions for candidate in (candidate_call, candidate_output)):
                    pairing = "outside_problem_segment"
                elif candidate_call["message_index"] >= candidate_output["message_index"]:
                    pairing = "result_precedes_call"
                else:
                    call, output, pairing = candidate_call, candidate_output, "matched_call_id"
            else:
                pairing = "result_not_in_page" if item["kind"] == "tool_use" else "call_not_in_page"
        if call is None and output is None:
            call, output = (item, None) if item["kind"] == "tool_use" else (None, item)
        items = [entry for entry in (call, output) if entry is not None]
        consumed.update(entry["message_index"] for entry in items)
        summary = (call or output).get("tool_summary") or {}
        result.append({
            "tool": _short(summary.get("name") or "unknown", 80), "call_id": item.get("call_id"),
            "call_message_index": call["message_index"] if call else None,
            "result_message_indexes": [output["message_index"]] if output else [],
            "pairing": pairing, "status": _tool_status(output) if output else "unknown",
            "input_excerpt": _short(call["text"]) if call else "unknown",
            "result_excerpts": [_short(output["text"])] if output else [],
            "evidence": [f"m{entry['message_index']}" for entry in items],
            "current_verification": "not_performed", "task_success": "unknown",
        })
    return result


def _context_candidates(boundary, attempts, stopping):
    """Expose observable historical gaps, never infer a current task or retry."""
    candidates = []

    def add(identity, question, reason, required, evidence, *, kind='history'):
        candidates.append({'id': identity, 'kind': kind, 'status': 'missing',
                           'question': question, 'reason': reason,
                           'required_evidence': required, 'evidence': list(dict.fromkeys(evidence))[:6],
                           'basis': 'historical_observation', 'current_applicability': 'unknown'})

    if boundary['starts_mid_problem']:
        add('original-request', '这段历史对应的原始要求和后续纠正是什么？',
            '当前公开窗口没有可确认的请求锚点，无法还原问题边界。',
            '同一来源、会话与修订中的原始用户消息及相关纠正。', [])
    if boundary['continues_after_page']:
        add('continuation', '下一页是否有改变本段判断的结果或纠正？',
            '当前页并未覆盖这一段问题的后续记录。',
            '沿同一 index_revision 和 next_start 展开的后续原消息。', [])
    unmatched = [item for item in attempts if item['status'] == 'unknown']
    if unmatched:
        add('tool-result', '这些工具尝试实际返回了什么？',
            '选定窗口缺少可明确配对或判定状态的工具返回，不能按成功继续。',
            '具有可核对调用身份的原始工具输入、返回和结果状态。',
            [ref for item in unmatched for ref in item['evidence']])
    failures = {}
    for attempt in attempts:
        if attempt['status'] == 'error' and attempt['pairing'] == 'matched_call_id':
            key = (attempt['tool'], attempt['input_excerpt'])
            failures.setdefault(key, []).append(attempt)
    repeated = [group for group in failures.values() if len(group) >= 2]
    if repeated:
        add('repeated-failure', '同一工具输入重复失败后，缺少哪项原因或修正证据？',
            '观察到至少两次相同可见输入的结构化失败；片段相同不证明完整命令相同。',
            '完整调用及返回、当前环境状态、后续修正证据；原样重试本身不是新证据。',
            [ref for group in repeated for item in group for ref in item['evidence']], kind='tool')
    if stopping:
        add('stop-condition', '历史停止或等待条件在当前任务中是否仍然成立？',
            '记录含停止或等待措辞；历史指令不能授权当前继续执行。',
            '当前用户指令和所等待材料的当前状态；不得仅凭历史完成声明解除停止。',
            [ref for item in stopping for ref in item['evidence']], kind='project')
    return candidates


def _case(messages, all_messages, *, identity, project, start, page_more, last_group, source_info):
    first, last = messages[0], messages[-1]
    request = first if (first["role"] == "user" and first["kind"] not in ("tool_use", "tool_result")
                        and not _followup(first["text"])) else None
    anchor = request["message_index"] if request else None
    segment_identity = [identity[key] for key in ("source", "store_id", "session_id")]
    segment_identity.extend(["request" if request else "continuation", anchor if request else first["message_index"]])
    digest = hashlib.sha256(json.dumps(segment_identity, ensure_ascii=False).encode()).hexdigest()[:32]
    evidence, requirements, evolution, signals, stop_candidates = [], [], [], [], []
    omitted = []
    for item in messages:
        index, role, kind, text = item["message_index"], item["role"], item["kind"], item["text"]
        if (role == "assistant" and kind not in ("tool_use", "tool_result")
                and item is not last and not any(pattern.search(text) for pattern in (_FRICTION, _COMPLETION, _WAIT, _SOLUTION))):
            omitted.append(index)
            continue
        ref = f"m{index}"
        excerpt = text[:EXCERPT_CHARS]
        entry = {**identity, "id": ref, "kind": "message", "message_index": index,
                 "role": role, "message_kind": kind, "timestamp_ms": item.get("ts_ms"),
                 "text": excerpt, "char_count": item["char_count"], "text_offset": 0,
                 "text_offset_basis": item["text_offset_basis"],
                 "next_text_offset": len(excerpt) if len(excerpt) < item["char_count"] else None,
                 "truncated": len(excerpt) < item["char_count"],
                 "analysis_window_truncated": item["truncated"], "analysis_window_chars": len(text),
                 "binding": "index_revision", "representation": "public_message_window",
                 "call_id": item.get("call_id"), "call_id_truncated": item.get("call_id_truncated", False),
                 "tool_summary_truncated": item.get("tool_summary_truncated", False),
                 "redacted": True, "current_verification": "not_performed"}
        if item.get("tool_summary"):
            entry["tool_summary"] = _visible_summary(item["tool_summary"])
        evidence.append(entry)
        event_kind = kind
        focus = None
        if role == "user" and kind not in ("tool_use", "tool_result"):
            event_kind = "request" if index == anchor else "user_followup_candidate"
            requirements.append({"id": f"r{index}", "text": _short(text), "state": "recorded_wording",
                                 "evidence": [ref], "completion": "unknown"})
            if _followup(text):
                focus = _signal("user_correction_or_continuation_candidate", item, _FOLLOWUP)
                signals.append(focus)
            if _STOP.search(text.strip()):
                focus = _signal("user_stop_request", item, _STOP)
                stop_candidates.append(focus)
        if _FRICTION.search(text):
            focus = _signal("friction_candidate", item, _FRICTION)
            signals.append(focus)
        if role == "assistant" and kind not in ("tool_use", "tool_result"):
            if _SOLUTION.search(text):
                event_kind = "proposed_solution_or_explanation"
                focus = _signal(event_kind, item, _SOLUTION)
                signals.append(focus)
            if _COMPLETION.search(text):
                event_kind = "assistant_completion_claim"
                focus = _signal(event_kind, item, _COMPLETION)
                signals.append(focus)
            if _WAIT.search(text):
                focus = _signal("waiting_wording", item, _WAIT)
                stop_candidates.append(focus)
        if _tool_status(item) == "error":
            focus = _signal("tool_error", item, _FRICTION, basis="structured_tool_result")
            signals.append(focus)
        evolution.append({"id": f"event{index}", "actor": role, "kind": event_kind,
                          "text": focus["text"] if focus else _short(text),
                          "text_offset": focus["text_offset"] if focus else 0,
                          "text_offset_basis": item["text_offset_basis"],
                          "focus_signal": focus["kind"] if focus else None,
                          "evidence": [ref], "replaces": []})
    attempts = _attempts(messages, all_messages)
    reasons = list(source_info["reasons"])
    if request is None:
        reasons.append("request_anchor_outside_window_or_unclear")
    if last_group and page_more:
        reasons.append("problem_may_continue_on_next_page")
    if any(item["truncated"] for item in messages):
        reasons.append("message_bodies_truncated")
    if any(item.get("tool_summary_truncated") for item in messages):
        reasons.append("tool_metadata_truncated")
    boundary = {"request_anchor": anchor, "first_message_index": first["message_index"],
                "last_message_index": last["message_index"], "starts_mid_problem": request is None,
                "continues_after_page": bool(last_group and page_more),
                "segmentation": "user_message_heuristic", "may_need_merge": request is None or start > 0}
    question = _short(request["text"]) if request else "原问题不在此窗口或指代未解；需结合前页核对。"
    title = _short(" ".join(request["text"].split()), 100) if request else f"问题续段：消息 {first['message_index']}（原问题待定位）"
    source_capture = source_info["source_capture"]
    return {
        "schema_version": "history.problem-case.v1", "id": "compressed-" + digest,
        "title": title or f"问题候选：消息 {first['message_index']}", "question": question, "project": project,
        "tags": ["自动提炼", "待核对", *sorted({signal["kind"] for signal in signals})],
        "interpretation": "rule_derived_draft", "source_verification": "indexed_public_windows",
        "current_verification": "not_performed",
        "now": {"judgment": "已定位问题候选及公开证据；因果、采用方案和解决状态尚未判断。",
                "adopted": "unknown", "task_status": "unknown", "knowledge_value": "unassessed",
                "applicability": "unknown", "remaining": ["核对原始需求、后续纠正、实际效果与当前适用条件。", *[
                    reason if isinstance(reason, str) else json.dumps(reason, ensure_ascii=False) for reason in reasons]]},
        "requirements": requirements, "evolution": evolution,
        "reuse": {"conditions": "与所列原始请求及环境逐项核对；历史动作仅供参考。",
                  "steps": ["先核对本案例的原始需求、后续纠正与当前任务是否一致。",
                            "按 attempts.evidence 打开工具输入、返回与效果证据，核清失败、成功和未知各指什么。",
                            "找到明确且仍适用的解决证据后，再决定当前动作；历史尝试不是默认执行步骤。"],
                  "avoid": ["不要把助手完成声明、工具成功状态或时间先后当作任务完成与因果证明。"],
                  "unknowns": ["根因", "已证实的解决办法", "当前环境适用性"]},
        "evidence": evidence, "attempts": attempts, "signals": signals,
        "context_candidates": _context_candidates(boundary, attempts, stop_candidates),
        "stopping": {"reason": "unknown", "candidates": stop_candidates,
                     "boundary": "page_end" if last_group and page_more else "indexed_end" if last_group else "next_request"},
        "compression": {"partial": bool(reasons), "coverage": "public_message_windows_not_complete_problem",
                        "source_capture": source_capture, "source_coverage": source_info["source_coverage"],
                        "boundary": boundary, "reasons": reasons,
                        "omitted_non_signal_message_indexes": omitted,
                        "inspected_message_count": len(messages), "retained_message_count": len(evidence)},
    }


def compress(indexer, session_id, *, source, store_id, index_revision, start=0, limit=100, system="unknown"):
    """Return candidate cases; ``start`` is an absolute message-slot lower bound.

    Paging never consumes prior summaries as facts. Boundary fragments remain
    separate candidates with explicit continuation markers until reviewed.
    """
    if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
        raise ValueError("session_id_required")
    if not isinstance(source, str) or source != indexer.source:
        raise ValueError("source_identity_mismatch")
    if not isinstance(store_id, str) or not store_id:
        raise ValueError("store_id_required")
    if not isinstance(index_revision, str) or not index_revision:
        raise ValueError("index_revision_required")
    if type(start) is not int or start < 0:
        raise ValueError("invalid_start")
    if type(limit) is not int or not 1 <= limit <= MAX_MESSAGES:
        raise ValueError("invalid_limit")
    if not isinstance(system, str) or not system:
        raise ValueError("system_identity_required")
    loader = _indexed_page if uses_message_index(indexer) else _native_page
    messages, more, next_start, info = loader(indexer, session_id, start, limit)
    capture = info["source_capture"]
    if capture.get("partial") or capture.get("trailing_bytes", 0) > 0:
        info["reasons"].append("source_capture_partial")
    if (capture.get("index_coverage") == "partial" or capture.get("lossy_fields", 0)
            or capture.get("skipped_lines", 0)):
        info["reasons"].append("source_projection_partial")
    if capture.get("index_coverage") == "images_omitted" or capture.get("image_fields", 0):
        info["reasons"].append("source_projection_images_omitted")
    identity = dict(system=system, source=source, store_id=store_id,
                    session_id=session_id, index_revision=index_revision)
    groups = []
    for message in messages:
        new_request = message["role"] == "user" and message["kind"] not in ("tool_use", "tool_result") and not _followup(message["text"])
        if not groups or new_request:
            groups.append([])
        groups[-1].append(message)
    cases = [_case(group, messages, identity=identity, project=info["project"], start=start,
                   page_more=more, last_group=n == len(groups) - 1, source_info=info)
             for n, group in enumerate(groups)]
    return {
        "schema_version": SCHEMA_VERSION, **identity, "authorization": "context_only",
        "interpretation": "rule_derived_draft", "current_verification": "not_performed",
        "cases": cases, "start": start, "has_more": more, "next_start": next_start,
        "partial": bool(start or more or info["reasons"] or any(case["compression"]["partial"] for case in cases)),
        "coverage": {"scope": "public_message_windows", "complete_problem": False,
                     "first_message_index": messages[0]["message_index"] if messages else None,
                     "last_message_index": messages[-1]["message_index"] if messages else None,
                     "message_count": len(messages), "source_coverage": info["source_coverage"],
                     "reasons": info["reasons"]},
        "source_capture": capture, "freshness": "unknown", "redacted": True,
        "limits": {"max_messages": MAX_MESSAGES, "requested_messages": limit,
                   "body_page_chars": BODY_PAGE_CHARS, "excerpt_chars": EXCERPT_CHARS,
                   "tool_metadata_chars": METADATA_CHARS},
        "extraction_guidance": [
            "这是带原始定位的自动候选；核对需求、纠偏、尝试、停止与效果，再写可复用解释。",
            "顺序相邻与词语命中只提供线索；没有证据时保留根因、解决状态和贡献归属为 unknown。",
            "使用同一 index_revision 和 next_start 继续读取；分页续段需回源合并，不能直接当作独立结论。",
            "消息被截断时按 evidence.next_text_offset 展开；隐藏块和未支持的来源内容不在覆盖内。",
            "无明确信号的中间助手解释可省略；省略序号保留在 compression.omitted_non_signal_message_indexes。",
            "工具调用仅按同页同问题的唯一完整 call ID 配对；结果成功不等于任务成功。",
            "查询和生成不会执行历史命令、调用模型或写入案例文件。",
        ],
        "redaction_notice": "Common secret spans are masked; this is not complete anonymisation.",
    }
