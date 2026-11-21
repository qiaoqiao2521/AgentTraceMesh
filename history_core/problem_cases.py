"""Editable, locally authored problem cases; never execute or revalidate history."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat

from .evidence import redact_annotation_field, redact_text
from .materials import _block


EXAMPLE_DIRECTORY = Path(__file__).with_name("case_examples")
_LABELS = {
    "interpretation": "authored_annotation",
    "source_verification": "references_not_revalidated",
    "current_verification": "not_performed",
}
_NOW_FIELDS = ("judgment", "adopted", "task_status", "knowledge_value", "applicability", "remaining")
_REUSE_FIELDS = ("conditions", "steps", "avoid", "unknowns")
_GENERATED_PRODUCER = "tracemesh.compression"
_ANY_REVISION = object()
_MAX_GENERATED_CASES = 100
_MAX_RECALL_CASES = 1000
_MAX_CASE_BYTES = 2 * 1024 * 1024
_MAX_RECALL_BYTES = 16 * 1024 * 1024
_IDENTITY_FIELDS = frozenset({
    "id", "kind", "system", "source", "store_id", "session_id", "message_index",
    "audit_event_index", "evidence_id", "index_revision", "content_revision",
    "context_revision", "selected_fragment_hash", "binding", "representation",
    "repository", "path", "commit", "line_start", "line_end", "raw_ref", "native_ref",
    "prefix_hash", "observed_at", "timestamp_ms", "text_offset", "text_offset_basis",
})


def default_case_directory():
    """Return the user-data location without creating it."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "agenttracemesh" / "problem-cases"


def _case_id(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)
            or value.endswith(".") or value.split(".")[0].upper() in {
                "CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
                *[f"LPT{i}" for i in range(1, 10)]}):
        raise ValueError("case_id_invalid")
    return value


def _directory(directory):
    root = Path(directory if directory is not None else default_case_directory()).expanduser().absolute()
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError("case_directory_symlink")
    return root


def _supports_directory_fd():
    supported = getattr(os, "supports_dir_fd", ())
    return hasattr(os, "O_DIRECTORY") and all(function in supported for function in (os.open, os.rename, os.unlink))


def _redact(value, *, evidence=False, key=""):
    if evidence and key in _IDENTITY_FIELDS:
        return value
    if isinstance(value, str):
        return redact_annotation_field(key, value)[1]
    if isinstance(value, list):
        return [_redact(item, evidence=evidence, key=key) for item in value]
    if isinstance(value, dict):
        cleaned = {}
        for name, item in value.items():
            if evidence and name in _IDENTITY_FIELDS:
                cleaned[name] = item
            else:
                clean_name, clean_item = redact_annotation_field(name, item)
                cleaned[clean_name] = _redact(clean_item, evidence=evidence, key=name)
        return cleaned
    return value


def _normalize(data, expected_id=None):
    if not isinstance(data, dict):
        raise ValueError("case_object_required")
    identity = _case_id(data.get("id"))
    if expected_id is not None and identity != expected_id:
        raise ValueError("case_filename_id_mismatch")
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ValueError("case_title_required")
    # A detached JSON value prevents normalization from changing caller data.
    case = json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
    evidence = case.pop("evidence", [])
    case = _redact(case)
    case["id"] = identity
    case.setdefault("schema_version", "history.problem-case.v1")
    case.setdefault("project", "unknown")
    case.setdefault("question", "unknown")
    warnings = []
    if not isinstance(case.get("tags", []), list):
        warnings.append("invalid_field_defaulted: tags")
        case["tags"] = []
    case.setdefault("tags", [])
    for name, fields in (("now", _NOW_FIELDS), ("reuse", _REUSE_FIELDS)):
        if not isinstance(case.get(name), dict):
            if name in case:
                warnings.append(f"invalid_field_defaulted: {name}")
            case[name] = {}
        for field in fields:
            case[name].setdefault(field, "unknown")
            if not isinstance(case[name][field], (str, list)):
                warnings.append(f"annotation_shape_unknown: {name}.{field}")
    for name in ("requirements", "evolution"):
        if not isinstance(case.get(name, []), list):
            warnings.append(f"invalid_field_defaulted: {name}")
            case[name] = []
        case.setdefault(name, [])
    if not isinstance(evidence, list):
        warnings.append("invalid_field_defaulted: evidence")
        evidence = []
    case["evidence"] = _redact(evidence, evidence=True)
    warnings.extend(_reference_warnings(case))
    return case, warnings


def _refs(value):
    return [value] if isinstance(value, str) else value if isinstance(value, list) else []


def _reference_warnings(case):
    warnings, identities = [], set()
    for evidence in case["evidence"]:
        if not isinstance(evidence, dict) or not isinstance(evidence.get("id"), str):
            warnings.append("evidence_identity_missing")
            continue
        identity = evidence["id"]
        if identity in identities:
            warnings.append(f"duplicate_evidence_id: {identity}")
        identities.add(identity)
        if evidence.get("kind") == "artifact":
            if any(not evidence.get(key) or evidence[key] == "unknown"
                   for key in ("repository", "path", "commit")):
                warnings.append(f"artifact_reference_incomplete: {identity}")
            start, end = evidence.get("line_start"), evidence.get("line_end")
            if type(start) is not int or type(end) is not int or start < 1 or end < start:
                warnings.append(f"artifact_line_range_unknown: {identity}")
        elif evidence.get("kind") == "message":
            message_index = evidence.get("message_index")
            if (not evidence.get("source") or not evidence.get("session_id") or not evidence.get("store_id")
                    or not (type(message_index) is int and message_index >= 0 or evidence.get("evidence_id"))
                    or not any(evidence.get(key) not in (None, "", "unknown") for key in
                               ("index_revision", "content_revision", "context_revision"))):
                warnings.append(f"message_reference_incomplete: {identity}")
        else:
            warnings.append(f"evidence_kind_unknown: {identity}")
    if not identities:
        warnings.append("case_evidence_missing")
    events = {event.get("id") for event in case["evolution"]
              if isinstance(event, dict) and isinstance(event.get("id"), str)}
    for group in ("requirements", "evolution"):
        for position, entry in enumerate(case[group], 1):
            if not isinstance(entry, dict):
                warnings.append(f"annotation_shape_unknown: {group}[{position}]")
                continue
            refs = _refs(entry.get("evidence"))
            label = entry.get("id") or f"{group}[{position}]"
            if not refs:
                warnings.append(f"annotation_evidence_missing: {label}")
            for reference in refs:
                if not isinstance(reference, str) or reference not in identities:
                    warnings.append(f"missing_evidence_reference: {label} -> {reference}")
            if group == "evolution":
                for replaced in _refs(entry.get("replaces")):
                    if not isinstance(replaced, str) or replaced not in events:
                        warnings.append(f"unknown_replacement_reference: {label} -> {replaced}")
    return warnings


def _generation_digest(case):
    """Hash saved, normalized content, including metadata except its own hash."""
    value = dict(case)
    generation = dict(value.get("generation") or {})
    generation.pop("content_digest", None)
    value["generation"] = generation
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _untouched_generated(case):
    generation = case.get("generation")
    return (isinstance(generation, dict)
            and generation.get("producer") == _GENERATED_PRODUCER
            and type(generation.get("schema_version")) is int
            and generation["schema_version"] == 1
            and case.get("interpretation") == "rule_derived_draft"
            and generation.get("content_digest") == _generation_digest(case))


def _labels(case, warnings=()):
    return dict(_LABELS, interpretation="rule_derived_draft" if case and "generated_case_modified" not in warnings
                and _untouched_generated(case)
                else "authored_annotation")


def _display(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join("- " + item for item in value) if value else "unknown"
    return json.dumps(value, ensure_ascii=False, indent=2)


def _reference_text(value):
    references = _refs(value)
    return " / ".join(_display(reference) for reference in references) if references else "unknown"


def _evidence_text(evidence):
    if not isinstance(evidence, dict):
        return _display(evidence)
    lines = ["证据 ID：" + _display(evidence.get("id", "unknown")),
             "类型：" + _display(evidence.get("kind", "unknown"))]
    if evidence.get("kind") == "artifact":
        repository = _display(evidence.get("repository", "unknown"))
        path = _display(evidence.get("path", "unknown"))
        commit = _display(evidence.get("commit", "unknown"))
        start, end = evidence.get("line_start", "unknown"), evidence.get("line_end", "unknown")
        lines.append(f"文件引用：{repository} / {path}@{commit}；行 {_display(start)}–{_display(end)}")
        omitted = {"id", "kind", "repository", "path", "commit", "line_start", "line_end"}
    elif evidence.get("kind") == "message":
        keys = {"system", "source", "store_id", "session_id", "message_index", "evidence_id",
                "index_revision", "content_revision", "context_revision", "binding", "locator",
                "text_offset", "text_offset_basis", "raw_ref"}
        locator = {key: value for key, value in evidence.items() if key in keys}
        lines.append("消息定位：\n" + json.dumps(locator, ensure_ascii=False, indent=2))
        omitted = {"id", "kind", *keys}
    else:
        omitted = {"id", "kind"}
    # Preserve authored notes and quoted text, without inventing a summary.
    for key, value in evidence.items():
        if key not in omitted:
            lines.append(key + "：\n" + _display(value))
    return "\n\n".join(lines)


def _markdown(case, warnings):
    interpretation = _labels(case, warnings)["interpretation"]
    description = ("规则提炼的待核对草稿；不证明因果、解决或完成。" if interpretation == "rule_derived_draft"
                   else "Agent 或用户编写的解释与关联。")
    sections = ["# 问题案例", _block(case["title"]),
                "> " + description + "来源引用未重新核对；当前验证未执行。",
                _block("解释属性：" + interpretation + "\n来源状态：references_not_revalidated\n当前验证：not_performed"),
                "## 现在怎样判断"]
    for label, value in (("问题", case["question"]), ("项目", case["project"]),
                         ("当前判断", case["now"]["judgment"]), ("采用方案", case["now"]["adopted"]),
                         ("任务状态", case["now"]["task_status"]), ("知识价值", case["now"]["knowledge_value"]),
                         ("适用边界", case["now"]["applicability"]), ("仍待核对", case["now"]["remaining"])):
        sections.extend(["### " + label, _block(_display(value))])
    for number, requirement in enumerate(case["requirements"], 1):
        if isinstance(requirement, dict):
            text = (_display(requirement.get("text", "unknown")) + "\n\n状态："
                    + _display(requirement.get("state", "unknown")) + "\n证据："
                    + _reference_text(requirement.get("evidence")))
        else:
            text = _display(requirement)
        sections.extend([f"### 要求 {number}", _block(text)])
    if not case["requirements"]:
        sections.extend(["### 要求与证据", _block("unknown")])
    sections.append("## 为什么改变")
    for number, event in enumerate(case["evolution"], 1):
        if isinstance(event, dict):
            text = (_display(event.get("text", "unknown")) + "\n\n节点 ID："
                    + _display(event.get("id", "unknown")) + "\n记录角色："
                    + _display(event.get("actor", "unknown")) + "\n类型："
                    + _display(event.get("kind", "unknown")) + "\n证据："
                    + _reference_text(event.get("evidence")) + "\n显式替代："
                    + _reference_text(event.get("replaces")))
        else:
            text = _display(event)
        sections.extend([f"### 变化 {number}", _block(text)])
    if not case["evolution"]:
        sections.append(_block("unknown"))
    sections.append("## 下次怎样做")
    for field, label in (("conditions", "适用条件"), ("steps", "下一步"),
                         ("avoid", "避免重走"), ("unknowns", "仍未知")):
        sections.extend(["### " + label, _block(_display(case["reuse"][field]))])
    sections.append("## 来源证据")
    for number, evidence in enumerate(case["evidence"], 1):
        sections.extend([f"### 证据 {number}", _block(_evidence_text(evidence))])
    if not case["evidence"]:
        sections.append(_block("unknown"))
    if warnings:
        sections.extend(["## 引用提示", _block("\n".join(warnings))])
    return "\n\n".join(sections) + "\n"


def _read_case_raw(path, identity, *, max_bytes=None):
    """Read without normalization so edits cannot disappear behind defaults/redaction."""
    if path.is_symlink():
        raise ValueError("case_file_symlink")
    directory_fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        if _supports_directory_fd():
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
            fd = os.open(path.name, flags, dir_fd=directory_fd)
        else:
            fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("case_regular_file_required")
            if max_bytes is not None and before.st_size > max_bytes:
                raise ValueError("case_read_limit_exceeded")
            raw = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
            if max_bytes is not None and len(raw) > max_bytes:
                raise ValueError("case_read_limit_exceeded")
            after = os.fstat(stream.fileno())
            identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            version = tuple(getattr(before, field) for field in identity_fields)
            if version != tuple(getattr(after, field) for field in identity_fields):
                raise ValueError("case_changed_during_read")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("id") != identity:
                raise ValueError("case_filename_id_mismatch")
            return data, (*version, hashlib.sha256(raw).hexdigest())
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _read_case(path, identity):
    data, _ = _read_case_raw(path, identity)
    case, warnings = _normalize(data, identity)
    if isinstance(data.get("generation"), dict) and not _untouched_generated(data):
        warnings.append("generated_case_modified")
    return case, warnings


def _error(identity, origin, exc):
    return {"id": identity, "origin": origin, "error": type(exc).__name__,
            "detail": redact_text(str(exc))}


def _envelope(case, origin, root, warnings=(), errors=()):
    identity = case["id"] if case else None
    return {**_labels(case, warnings), "case": case, "origin": origin,
            "editable_path": str(root / (identity + ".json")) if identity else None,
            "markdown": _markdown(case, warnings) if case else "",
            "warnings": list(warnings), "partial": bool(errors), "errors": list(errors)}


def _paths(root):
    if not root.exists():
        return []
    return sorted((path for path in root.iterdir() if path.suffix == ".json"), key=lambda path: path.name)


def _first(value):
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, str) and item and item != "unknown"), None)
    return value if isinstance(value, str) and value and value != "unknown" else None


def list_cases(directory=None, *, query="", project=None, offset=0, limit=20):
    if type(offset) is not int or offset < 0 or type(limit) is not int or limit <= 0:
        raise ValueError("case_pagination_invalid")
    if not isinstance(query, str) or project is not None and not isinstance(project, str):
        raise ValueError("case_query_invalid")
    root = Path(directory if directory is not None else default_case_directory()).expanduser().absolute()
    records, errors = {}, []
    for directory_path, origin in ((EXAMPLE_DIRECTORY, "example"), (root, "local")):
        try:
            paths = _paths(_directory(directory_path))
        except (OSError, ValueError) as exc:
            errors.append(_error(None, origin, exc))
            continue
        for path in paths:
            # An explicit local file shadows the example, including a broken
            # local edit. Do not silently show the example as that saved edit.
            if origin == "local":
                records.pop(path.stem, None)
            try:
                identity = _case_id(path.stem)
                case, warnings = _read_case(path, identity)
                records[identity] = (case, origin, warnings)
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                errors.append(_error(path.stem, origin, exc))
    terms = query.casefold().split()
    items = []
    for identity in sorted(records):
        case, origin, warnings = records[identity]
        if project is not None and case["project"] != project:
            continue
        haystack = json.dumps(case, ensure_ascii=False).casefold()
        if any(term not in haystack for term in terms):
            continue
        item = {key: case[key] for key in ("id", "title", "project", "question", "tags", "now")}
        item.update(**_labels(case, warnings), origin=origin, warnings=warnings,
                    next_action=_first(case["reuse"]["steps"]) or _first(case["now"]["remaining"]) or "unknown")
        items.append(item)
    total = len(items)
    page = items[offset:offset + limit]
    return {**_LABELS, "items": page, "total": total,
            "next_offset": offset + len(page) if offset + len(page) < total else None,
            "partial": bool(errors), "errors": errors}


def get_case(directory, case_id):
    identity = _case_id(case_id)
    root = _directory(directory)
    for directory_path, origin in ((root, "local"), (EXAMPLE_DIRECTORY, "example")):
        path = _directory(directory_path) / (identity + ".json")
        # lexists semantics: a dangling symlink is a failed local override.
        if not path.exists() and not path.is_symlink():
            continue
        try:
            case, warnings = _read_case(path, identity)
            return _envelope(case, origin, root, warnings)
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            result = _envelope(None, origin, root, errors=[_error(identity, origin, exc)])
            result["editable_path"] = str(root / (identity + ".json"))
            return result
    raise ValueError("case_not_found")


def save_case(directory, data, *, _expected_revision=_ANY_REVISION):
    case, warnings = _normalize(data)
    root = _directory(directory)
    root.mkdir(parents=True, exist_ok=True)
    directory_fd = (os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
                    if _supports_directory_fd() else None)
    temporary = ".case-" + secrets.token_hex(16) + ".tmp"
    target = case["id"] + ".json"
    temporary_created = False
    try:
        if (root / target).is_symlink():
            raise ValueError("case_file_symlink")
        if (root / target).exists() and not (root / target).is_file():
            raise ValueError("case_regular_file_required")
        if directory_fd is not None:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
        else:
            fd = os.open(root / temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        temporary_created = True
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(case, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if _expected_revision is not _ANY_REVISION:
            try:
                _, observed = _read_case_raw(root / target, case["id"], max_bytes=_MAX_CASE_BYTES)
            except FileNotFoundError:
                observed = None
            if observed != _expected_revision:
                raise ValueError("case_changed_during_save")
        if _expected_revision is None:
            # Linking the finished temporary file creates a new name without
            # replacing a case that appeared after our missing-file check.
            try:
                os.link(root / temporary, root / target, follow_symlinks=False)
            except FileExistsError:
                raise ValueError("case_changed_during_save") from None
            if directory_fd is not None:
                os.fsync(directory_fd)
        elif directory_fd is not None:
            os.replace(temporary, target, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        else:
            os.replace(root / temporary, root / target)
    finally:
        try:
            if temporary_created:
                try:
                    if directory_fd is not None:
                        os.unlink(temporary, dir_fd=directory_fd)
                    else:
                        os.unlink(root / temporary)
                except FileNotFoundError:
                    pass
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
    return _envelope(case, "local", root, warnings)


def _source_identity(case):
    """A stable id must not silently adopt another recording's evidence."""
    return sorted({tuple(str(item.get(key, "")) for key in ("system", "source", "store_id", "session_id"))
                   for item in case.get("evidence", []) if isinstance(item, dict) and item.get("kind") == "message"})


def _narrower_window(previous, incoming):
    def boundary(case):
        compression = case.get("compression")
        value = compression.get("boundary") if isinstance(compression, dict) else None
        if not isinstance(value, dict):
            return None
        first, last = value.get("first_message_index"), value.get("last_message_index")
        if type(first) is not int or type(last) is not int or first < 0 or last < first:
            return None
        return first, last, value.get("request_anchor")
    old, new = boundary(previous), boundary(incoming)
    if old is None:
        return False
    return (new is None or new[0] > old[0] or new[1] < old[1]
            or old[2] is not None and new[2] != old[2])


def save_generated_cases(directory, packet):
    """Persist only new or unedited generated drafts; never overwrite authored cases.

    The final revision check detects concurrent edits before replacement. It is
    not a filesystem compare-and-swap against non-cooperating external writers.
    """
    if not isinstance(packet, dict) or not isinstance(packet.get("cases"), list):
        raise ValueError("generated_cases_packet_required")
    if len(packet["cases"]) > _MAX_GENERATED_CASES:
        raise ValueError("generated_case_limit_exceeded")
    root = _directory(directory)
    report = {"schema_version": "history.case-generation.v1", "items": [], "errors": [],
              "saved": 0, "updated": 0, "unchanged": 0, "protected": 0, "error": 0,
              "partial": bool(packet.get("partial")), "current_verification": "not_performed"}
    seen = set()
    for data in packet["cases"]:
        identity = None
        try:
            case, warnings = _normalize(data)
            identity = case["id"]
            if identity in seen:
                raise ValueError("duplicate_generated_case_id")
            seen.add(identity)
            case["interpretation"] = "rule_derived_draft"
            case["generation"] = {"producer": _GENERATED_PRODUCER, "schema_version": 1}
            case["generation"]["content_digest"] = _generation_digest(case)
            if len(json.dumps(case, ensure_ascii=False).encode()) > _MAX_CASE_BYTES:
                raise ValueError("case_read_limit_exceeded")
            target = root / (identity + ".json")
            expected = None
            status, reason = "saved", None
            if target.exists() or target.is_symlink():
                previous, expected = _read_case_raw(target, identity, max_bytes=_MAX_CASE_BYTES)
                if not _untouched_generated(previous):
                    status, reason = "protected", "authored_or_modified_case"
                elif _source_identity(previous) != _source_identity(case):
                    status, reason = "protected", "source_identity_changed"
                elif _narrower_window(previous, case):
                    status, reason = "protected", "narrower_evidence_window"
                else:
                    status = ("unchanged" if previous["generation"]["content_digest"] == case["generation"]["content_digest"]
                              else "updated")
            elif (EXAMPLE_DIRECTORY / (identity + ".json")).exists():
                status, reason = "protected", "existing_example_case"
            if status in ("saved", "updated"):
                save_case(root, case, _expected_revision=expected)
            item = {"id": identity, "status": status, "warnings": warnings}
            if reason:
                item["reason"] = reason
            report[status] += 1
            report["items"].append(item)
            report["partial"] |= status == "protected"
        except (OSError, ValueError, TypeError, RecursionError) as error:
            status = "protected" if str(error) == "case_changed_during_save" else "error"
            report[status] += 1
            report["partial"] = True
            detail = _error(identity, "generated", error)
            report["items"].append({"id": identity, "status": status, "reason": detail["detail"]})
            report["errors"].append(detail)
    return report


def _recall_terms(query):
    if not isinstance(query, str) or not query.strip():
        raise ValueError("case_query_required")
    if len(query) > 300:
        raise ValueError("case_query_too_long")
    # Mask while assignment context is intact, before a credential becomes an
    # isolated matched/unmatched term that can no longer be recognized.
    phrase = " ".join(redact_text(query).casefold().split())
    terms = []
    for part in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_./-]+", phrase):
        if re.fullmatch(r"[\u3400-\u9fff]{3,}", part):
            terms.extend(part[index:index + 2] for index in range(len(part) - 1))
        else:
            terms.append(part)
    terms = list(dict.fromkeys(terms)) or [phrase]
    if len(terms) > 64:
        raise ValueError("case_query_too_many_terms")
    return phrase, terms


def _text_values(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_text_values(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_text_values(item) for item in value.values())
    return ""


def _short(value, *, chars=280, entries=3):
    """Display-only bounds; full cases remain available by id."""
    if isinstance(value, str):
        return value[:chars]
    if isinstance(value, list):
        return [_short(item, chars=chars, entries=entries) for item in value[:entries]]
    if isinstance(value, dict):
        return {str(key)[:80]: _short(item, chars=chars, entries=entries) for key, item in list(value.items())[:10]}
    return value


def _recall_evidence(evidence):
    items = []
    fields = ("id", "kind", "system", "source", "store_id", "session_id", "message_index",
              "index_revision", "content_revision", "context_revision", "text_offset", "text_offset_basis",
              "repository", "path", "commit", "line_start", "line_end", "next_text_offset", "char_count",
              "text_chars", "excerpt_start", "excerpt_end", "binding", "representation")
    for value in evidence[:3]:
        if not isinstance(value, dict):
            continue
        item = {}
        for key in fields:
            entry = value.get(key)
            if isinstance(entry, (str, int)) and not isinstance(entry, bool):
                if isinstance(entry, str) and len(entry) > 1024:
                    item["locator_partial"] = True
                else:
                    item[key] = entry
        if "text" in value:
            item["text"] = _short(value["text"])
            item["display_excerpt_truncated"] = item["text"] != value["text"]
        for key in ("partial", "truncated", "analysis_window_truncated", "has_more_before", "has_more_after"):
            if key in value:
                item[key] = value[key] if isinstance(value[key], bool) else "unknown"
        items.append(item)
    return items


def recall_cases(directory, *, query, project=None, limit=5):
    """Rank existing cases with literal words/CJK bigrams, never semantic inference."""
    phrase, terms = _recall_terms(query)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("case_recall_limit_invalid")
    if project is not None and not isinstance(project, str):
        raise ValueError("case_query_invalid")
    root = _directory(directory)
    paths, errors = {}, []
    for candidate_root, origin in ((EXAMPLE_DIRECTORY, "example"), (root, "local")):
        try:
            for path in _paths(_directory(candidate_root)):
                paths[path.stem] = (path, origin)
        except (OSError, ValueError) as error:
            errors.append(_error(None, origin, error))
    candidates, scanned = [], 0
    remaining_bytes, byte_limited = _MAX_RECALL_BYTES, False
    for identity in sorted(paths)[:_MAX_RECALL_CASES]:
        if remaining_bytes <= 0:
            byte_limited = True
            break
        path, origin = paths[identity]
        scanned += 1
        try:
            _case_id(identity)
            data, revision = _read_case_raw(path, identity, max_bytes=min(_MAX_CASE_BYTES, remaining_bytes))
            remaining_bytes -= revision[2]
            case, warnings = _normalize(data, identity)
            if isinstance(data.get("generation"), dict) and not _untouched_generated(data):
                warnings.append("generated_case_modified")
            if project is not None and case["project"] != project:
                continue
            fields = {key: _text_values(case.get(key)).casefold() for key in
                      ("title", "question", "tags", "now", "requirements", "evolution", "reuse",
                       "signals", "attempts", "stopping", "evidence")}
            matched = [term for term in terms if any(term in value for value in fields.values())]
            if not matched:
                continue
            primary = " ".join(fields[key] for key in ("title", "question", "tags"))
            primary_matches = [term for term in matched if term in primary]
            phrase_match = phrase in fields["title"] or phrase in fields["question"]
            generated = _untouched_generated(data)
            item = {key: _short(case[key]) for key in ("id", "title", "question", "project", "now", "reuse")}
            item.update(**_labels(case, warnings), origin=origin,
                        next_action=_short(_first(case["reuse"]["steps"]) or _first(case["now"]["remaining"]) or "unknown"),
                        stopping=_short(case.get("stopping", {"reason": "unknown"})),
                        signals=_short(case.get("signals", [])), attempts=_short(case.get("attempts", [])),
                        evidence=_recall_evidence(case["evidence"]), warnings=_short(warnings, entries=5),
                        partial=bool((case.get("compression") or {}).get("partial"))
                        if isinstance(case.get("compression"), dict) else False,
                        match={"method": "literal_terms_and_cjk_bigrams", "phrase_in_problem": phrase_match,
                               "matched_terms": matched, "unmatched_terms": [term for term in terms if term not in matched],
                               "fields": [key for key, value in fields.items() if any(term in value for term in matched)]},
                        display_bounded=True, expand={"operation": "cases show", "case_id": identity})
            candidates.append(((-int(phrase_match), -len(primary_matches), -len(matched), int(generated), identity), item))
        except (OSError, ValueError, TypeError, RecursionError) as error:
            errors.append(_error(identity, origin, error))
    candidates.sort(key=lambda pair: pair[0])
    items = [item for _, item in candidates[:limit]]
    truncated = len(paths) > _MAX_RECALL_CASES or byte_limited
    return {"schema_version": "history.case-recall.v1", "query": redact_text(query),
            "project": redact_text(project) if project is not None else None,
            "items": items, "total_matches": len(candidates), "has_more": len(candidates) > limit,
            "partial": bool(errors or truncated or any(item["partial"] for item in items)),
            "errors": errors, "truncated": truncated, "scanned_cases": scanned,
            "case_limit": _MAX_RECALL_CASES, "match_semantics": "literal_terms_and_cjk_bigrams",
            "read_byte_limit": _MAX_RECALL_BYTES,
            "display_limits": {"text_chars": 280, "list_items": 3, "evidence_items": 3},
            "source_verification": "references_not_revalidated", "current_verification": "not_performed",
            "coverage": "saved_case_catalog_only", "authorization": "context_only"}
