"""Revision-bound, deliberately selected context; no source writes or execution.

``selection_bundle(indexers, selections)`` accepts registered
``(system, source, indexer)`` triples. A selection identifies a session and either
an indexed ``message_index`` or an audit ``evidence_id``, plus the content (or
bounded context) revision that was displayed to the user. Missing revisions are
never filled in during export. Unsupported native snapshots fail explicitly.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .provenance import PUBLIC_SNAPSHOT_SOURCES, selected_audit, selected_snapshot, selected_review_snapshot

MAX_SELECTIONS = 5
MAX_BODY_CHARS = 8000
_REVISION = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?"
    r"(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)", re.S)
_AUTH = re.compile(r"(?i)(\bauthorization\b[\"']?\s*[:=]\s*[\"']?)(?:bearer|basic)\s+[^\s\"'`,;]+")
_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|"
    r"secret|client[_-]?secret|authorization)\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;}`]+)")
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{16,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16})\b")


def _store_id(system, source, root):
    """Hash an already selected root without resolving live filesystem paths."""
    identity = [str(system), str(source), str(root)]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def source_store_id(system, source, indexer):
    """Stable local store identity shared by result locators and selection UI."""
    root = getattr(indexer, "sessions_dir", None) or getattr(indexer, "db_path", None)
    if root is None:
        raise ValueError("source_store_identity_unavailable")
    return _store_id(system, source, Path(root).resolve())


def redact_text(text):
    """Minimum local secret masking, not a guarantee of anonymisation."""
    result = str(text)
    result = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", result)
    result = _AUTH.sub(lambda match: match.group(1) + "[REDACTED]", result)
    result = _ASSIGNMENT.sub(lambda match: match.group(1) + "[REDACTED]", result)
    return _KNOWN_TOKEN.sub("[REDACTED]", result)


def redact_annotation_field(key, value):
    """Mask an authored mapping field using the existing secret vocabulary.

    This opt-in helper does not change historical locator/export semantics.
    ``authorization=context_only`` is an evidence policy, not a credential.
    """
    clean_key = redact_text(key) if isinstance(key, str) else key
    field = key.strip() if isinstance(key, str) else ""
    assignment = _ASSIGNMENT.fullmatch(field + "=value")
    secret_field = assignment and assignment.end(1) == len(field) + 1
    if secret_field and not (field.lower() == "authorization" and value == "context_only"):
        return clean_key, "[REDACTED]"
    if isinstance(value, str):
        value = redact_text(value)
    return clean_key, value


def redact_indexed_window(text, start, limit):
    """Mask complete-message secret spans, preserving indexed character offsets.

    A window can start inside a secret or split its assignment across pages.
    Match on the selected full message, then mask only the window intersection.
    Equal-length masking keeps both pagination and the rendered budget stable.
    """
    text = str(text or '')
    end = min(len(text), start + limit)
    window = list(text[start:end])
    for pattern, group in ((_PRIVATE_KEY, 0), (_AUTH, 1),
                           (_ASSIGNMENT, 1), (_KNOWN_TOKEN, 0)):
        for match in pattern.finditer(text):
            left = max(start, match.end(group) if group else match.start())
            right = min(end, match.end())
            if left < right:
                window[left-start:right-start] = '*' * (right-left)
    return ''.join(window)


def _redact(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


def _resolve(indexers, selection):
    matches = [(system, source, indexer) for system, source, indexer in indexers
               if str(system) == selection.get("system") and str(source) == selection.get("source")]
    store = selection.get("store_id")
    if store:
        matches = [entry for entry in matches if source_store_id(*entry) == store]
    if len(matches) != 1:
        raise ValueError("selection_source_ambiguous" if matches else "selection_source_unavailable")
    return matches[0]


def _binding(selection, provenance):
    if provenance.get("status") != "captured":
        raise ValueError("selection_revision_unsupported")
    content = selection.get("content_revision")
    context = selection.get("context_revision")
    if content and content != "unknown":
        if not isinstance(content, str) or not _REVISION.fullmatch(content):
            raise ValueError("selection_revision_invalid")
        if content != provenance.get("content_revision"):
            raise ValueError("selection_stale: content revision changed")
        return "content_revision"
    if context and context != "unknown":
        if not isinstance(context, str) or not _REVISION.fullmatch(context):
            raise ValueError("selection_revision_invalid")
        if context != provenance.get("context_revision"):
            raise ValueError("selection_stale: context revision changed")
        return "context_revision"
    raise ValueError("selection_revision_required")


def _indexed_message(indexer, session_id, message_index):
    """Read a bounded cache row, never a full transcript or native adapter."""
    with indexer.lock:
        session = indexer.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if session is None:
            raise ValueError("selection_session_unavailable")
        signature = session["file_signature"] if "file_signature" in session.keys() else None
        if not signature:
            raise ValueError("selection_message_binding_unsupported: refresh required")
        row = indexer.conn.execute(
            "SELECT role, kind, ts_ms, LENGTH(text) AS chars, SUBSTR(text, 1, ?) AS text "
            "FROM messages WHERE session_id = ? ORDER BY ts_ms ASC, id ASC LIMIT 1 OFFSET ?",
            (MAX_BODY_CHARS + 1, session_id, message_index)).fetchone()
    if row is None:
        raise ValueError("selection_message_unavailable")
    if int(row["chars"] or 0) > MAX_BODY_CHARS:
        raise ValueError("selection_body_limit_exceeded")
    return dict(row), signature


def _message_signature(indexer, session_id):
    with indexer.lock:
        row = indexer.conn.execute("SELECT file_signature FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row[0] if row else None


def _markdown(items):
    # All source-controlled values live in fenced text blocks. An adaptive fence
    # prevents history containing backticks, HTML or shell snippets escaping it.
    sections = ["# Selected historical evidence", "Context only. No execution or recipient acceptance is implied."]
    for number, item in enumerate(items, 1):
        labels = {key: item[key] for key in (
            "system", "source", "store_id", "session_id", "message_index", "audit_event_index", "evidence_id",
            "locator", "content_revision", "context_revision", "observed_at", "binding",
            "representation", "redacted")}
        text = json.dumps(labels, ensure_ascii=False, indent=2) + "\n\n" + item["text"]
        longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
        fence = "`" * max(3, longest + 1)
        sections.extend(["## Evidence " + str(number), fence + "text\n" + text + "\n" + fence])
    return "\n\n".join(sections) + "\n"


def selection_bundle(indexers, selections):
    """Return an all-or-nothing, redacted Markdown/JSON selection bundle.

    Public errors are stable ``selection_*`` codes (with optional detail).
    Message indices refer to the browser's cached display order. Audit indices
    may differ, so evidence IDs export the selected deterministic evidence
    summary directly, never an unrelated cached message at that audit index.
    """
    if not isinstance(selections, list) or not 1 <= len(selections) <= MAX_SELECTIONS:
        raise ValueError("selection_count_limit: choose 1 to 5 fragments")
    items, seen, body_chars = [], set(), 0
    indexers = list(indexers)
    for selection in selections:
        if not isinstance(selection, dict):
            raise ValueError("selection_invalid")
        session_id = selection.get("session_id")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
            raise ValueError("selection_session_invalid")
        evidence_id = selection.get("evidence_id")
        message_index = selection.get("message_index")
        if evidence_id is not None:
            if not isinstance(evidence_id, str) or not evidence_id or len(evidence_id) > 512:
                raise ValueError("selection_evidence_invalid")
        elif type(message_index) is not int or message_index < 0:
            raise ValueError("selection_message_index_required")
        # Reject missing bindings before any source read, including native DBs.
        if not any(isinstance(selection.get(key), str) and _REVISION.fullmatch(selection[key])
                   for key in ("content_revision", "context_revision")):
            raise ValueError("selection_revision_required")
        system, source, indexer = _resolve(indexers, selection)
        public_snapshot = getattr(indexer, 'source', '') in PUBLIC_SNAPSHOT_SOURCES
        if getattr(indexer, "sessions_dir", None) is None and not public_snapshot:
            raise ValueError("selection_revision_unsupported: native database snapshot unavailable")
        store = source_store_id(system, source, indexer)
        identity = (system, source, store, session_id, evidence_id or message_index)
        if identity in seen:
            raise ValueError("selection_duplicate")
        seen.add(identity)
        message, signature = (None, None) if evidence_id or public_snapshot else _indexed_message(indexer, session_id, message_index)
        try:
            if public_snapshot:
                snapshot = selected_review_snapshot(indexer, session_id)
                audit, provenance = snapshot['audit'], snapshot['provenance']
                if not evidence_id:
                    message = next((row for row in snapshot['messages'] if row['message_index'] == message_index), None)
                    if message is None:
                        raise ValueError('selection_message_unavailable')
            else:
                audit, provenance = selected_audit(indexer, session_id)
        except (OSError, ValueError) as error:
            if "changed" in str(error):
                raise ValueError("selection_stale: refresh and select again") from error
            if str(error).startswith('selection_') or 'limit_exceeded' in str(error):
                raise
            raise ValueError("selection_source_unavailable") from error
        if audit is None or not provenance:
            raise ValueError("selection_session_unavailable")
        binding = _binding(selection, provenance)
        locator = dict(provenance.get("locator") or {})
        representation = "indexed_message"
        audit_event_index = None
        if evidence_id:
            evidence = next((item for item in audit.get("evidence", []) if item.get("id") == evidence_id), None)
            if evidence is None:
                raise ValueError("selection_evidence_unavailable")
            text = evidence.get("summary") or ""
            locator["raw_ref"] = dict(evidence.get("raw_ref") or {})
            locator["evidence_type"] = evidence.get("type", "unknown")
            locator["confidence"] = evidence.get("confidence", "unknown")
            audit_event_index = evidence.get("message_index")
            message_index = None
            representation = "deterministic_evidence_summary"
        else:
            if provenance.get("truncated"):
                raise ValueError("selection_message_outside_verified_scope: use bounded audit evidence")
            if not public_snapshot and signature != _message_signature(indexer, session_id):
                raise ValueError("selection_stale: index changed during read")
            text = message["text"] or ""
            locator["message_index"] = message_index
            locator["role"] = message["role"]
            locator["kind"] = message["kind"]
            locator["timestamp_ms"] = message["ts_ms"]
            for key in ('raw_ref', 'native_ref'):
                if message.get(key):
                    locator[key] = message[key]
        body_chars += len(text)
        if body_chars > MAX_BODY_CHARS:
            raise ValueError("selection_body_limit_exceeded")
        item = {"system": str(system), "source": str(source), "store_id": store,
                "session_id": session_id, "message_index": message_index, "audit_event_index": audit_event_index, "evidence_id": evidence_id,
                "locator": locator, "text": text, "representation": representation,
                "content_revision": provenance.get("content_revision", "unknown"),
                "context_revision": provenance.get("context_revision", "unknown"),
                "observed_at": provenance.get("observed_at"), "binding": binding,
                "context_only": True, "historical_verification": "unknown",
                "truncated_source": bool(provenance.get("truncated"))}
        clean = _redact(item)
        clean["redacted"] = clean != item
        items.append(clean)
    exported_chars = sum(len(item["text"]) for item in items)
    if exported_chars > MAX_BODY_CHARS:
        # Masking a short token can increase text length; the same final-output
        # limit applies after redaction, rather than clipping the replacement.
        raise ValueError("selection_body_limit_exceeded")
    return {"schema_version": "history.evidence-selection.v1", "context_only": True,
            "authorization": "context_only", "observed_at": datetime.now(timezone.utc).isoformat(),
            "items": items, "markdown": _markdown(items),
            "body_chars": exported_chars,
            "limits": {"max_fragments": MAX_SELECTIONS, "max_body_chars": MAX_BODY_CHARS},
            "redacted": any(item["redacted"] for item in items),
            "redaction_notice": "Common secret patterns are masked; review before sharing. This is not complete anonymisation."}


def raw_record(indexer, session_id, binding, *, evidence_id=None, line_no=None):
    """Show a bounded exact JSONL record; audit event offsets are not UI offsets."""
    audit, provenance, raw = selected_snapshot(indexer, session_id)
    _binding(binding, provenance)
    if raw is None or audit is None:
        raise ValueError("raw_record_unsupported")
    if evidence_id:
        evidence = next((entry for entry in audit.get("evidence", []) if entry.get("id") == evidence_id), None)
        if evidence is None:
            raise ValueError("raw_evidence_unavailable")
        line_no = (evidence.get("raw_ref") or {}).get("line_no")
        pointer = (evidence.get("raw_ref") or {}).get("json_pointer")
        if provenance.get('locator', {}).get('format') == 'json' and pointer:
            match = re.fullmatch(r'/messages/(\d+)', pointer)
            if not match:
                raise ValueError('raw_pointer_unavailable')
            try:
                record = json.loads(raw)['messages'][int(match[1])]
            except (ValueError, KeyError, IndexError, TypeError):
                raise ValueError('raw_pointer_unavailable') from None
            value = json.dumps(record, ensure_ascii=False, indent=2)
            return {'text': value[:16000], 'truncated': len(value) > 16000, 'json_pointer': pointer,
                    'line_no': None, 'provenance': provenance, 'representation': 'raw_json_record', 'context_only': True}
    if provenance.get('locator', {}).get('format') == 'json':
        raise ValueError('raw_pointer_unavailable')
    try:
        line_no = int(line_no)
    except (ValueError, TypeError):
        raise ValueError("raw_line_unavailable") from None
    if not 1 <= line_no <= 1000000:
        raise ValueError("raw_line_unavailable")
    for number, line in enumerate(raw.splitlines(), 1):
        if number == line_no:
            value = line.decode("utf-8", errors="replace")
            return {"text": value[:16000], "truncated": len(value) > 16000,
                    "line_no": line_no, "provenance": provenance,
                    "representation": "raw_jsonl_record", "context_only": True}
    raise ValueError("raw_line_unavailable")
