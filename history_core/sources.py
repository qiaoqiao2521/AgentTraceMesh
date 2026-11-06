"""Shared native parsers/indexers, mechanically extracted from app.py."""
from __future__ import annotations
import json
import io
import logging
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone, time as dt_time
from pathlib import Path
from audit import AUDIT_VERSION, build_audit_from_events, build_audit_for_file, deserialize_audit_summary, patch_db_for_audit, serialize_audit_fields
from audit.schema import AuditEvent
from .providers import file_suffixes
from .titles import is_ambient_title, meaningful_user_title
from .message_ordinals import message_indexes

MAX_SEARCH_CHARS = 2_000_000
DEFAULT_LIMIT = 200
MESSAGE_INLINE_FULL_THRESHOLD = 12_000
MESSAGE_PREVIEW_CHARS = 4_000
MESSAGE_PREVIEW_FETCH_CHARS = MESSAGE_PREVIEW_CHARS + 2_048
SEARCH_MATCH_CONTEXT_CHARS = 1_600
SEARCH_MATCH_MAX_CHARS = 3_600
DEFAULT_PAGE_LIMIT = 50
SESSION_PREVIEW_CACHE_SIZE = 12


def _move_to_backup(source, directory, session_id):
    """Preserve the source inode under an exclusive name before unlinking.

    Open writers continue appending to that inode, including after unlink.
    Unsupported/cross-device hard links leave the source in place: a copy
    cannot safely replace this move while another process can append.
    """
    source, directory = Path(source), Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # Link the target inode, not a relative symlink that would break on move.
    original = source.resolve(strict=True)
    attempt = 0
    while True:
        if attempt == 0:
            name = source.name
        else:
            suffix = f"-{session_id[:8]}" + (f"-{attempt}" if attempt > 1 else "")
            name = source.stem + suffix + source.suffix
        destination = directory / name
        try:
            os.link(original, destination)
            break
        except FileExistsError:
            attempt += 1
        except NotImplementedError as exc:
            raise OSError("backup_hardlink_unsupported_source_preserved") from exc
    try:
        with destination.open("rb") as backup:
            os.fsync(backup.fileno())
        if os.name != "nt":
            directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    source.unlink()
    return destination


def _neutral_audit_summary():
    return {
        "files_touched": [],
        "tools_used": [],
        "command_intents": [],
        "remote_context": [],
        "outcome_signal": "unknown",
        "value_score": 0,
        "friction_score": 0,
        "action_density": 0.0,
    }


_AUDIT_RAW_JSON_KEYS = frozenset({
    "files_touched_json",
    "tool_summary_json",
    "command_intents_json",
    "remote_context_json",
})


def _strip_audit_raw_json(item):
    for key in _AUDIT_RAW_JSON_KEYS:
        item.pop(key, None)
    return item


def _match_files_touched(files_touched_json, target_path):
    # ALGO: two-stage filter for ?file= cross-session drill-down (002 §M4).
    # Stage 1 (SQL LIKE) narrows candidate rows; stage 2 (this) parses the JSON
    # and matches the exact path field, eliminating prefix-collisions that
    # coarse LIKE would admit (e.g. /a/b.py vs /a/b_backup.py).
    if not files_touched_json or not target_path:
        return False
    try:
        data = json.loads(files_touched_json) if isinstance(files_touched_json, str) else files_touched_json
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    needle = str(target_path)
    for bucket in ("local", "remote", "inferred"):
        entries = data.get(bucket) or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("path") or "") == needle:
                return True
    return False


def _escape_sql_like(value):
    # SECURITY: user-supplied ?file= paths flow into a LIKE clause; escape the
    # pattern metacharacters so %/_ in a path can't broaden the match.
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _indexed_public_message_sql(alias="m"):
    """Eligible indexed evidence; aliases are owned SQL identifiers, not input."""
    kind = f"COALESCE({alias}.kind,'')"
    return (
        f"{alias}.role IN ('user','assistant','tool') "
        f"AND {kind} NOT LIKE 'context%' AND {kind} NOT LIKE 'raw_json%' "
        f"AND {kind} NOT LIKE '%reasoning%' AND {kind} <> 'thinking'"
    )


def _indexed_text_position_sql(text, needle):
    """One bound needle, preserving SQLite's ASCII-only case folding and NULs."""
    if any('a' <= char <= 'z' or 'A' <= char <= 'Z' for char in needle):
        return f'instr(lower({text}),lower(?))'
    return f'instr({text},?)'


def _indexed_body_match_sql(alias="sessions"):
    """One literal-LIKE parameter over complete public indexed messages.

    Existing search blobs mix public text, reasoning and raw fallbacks. They
    cannot authorize a hit without checking the message's role and kind.
    """
    return (
        "EXISTS ("
        f"SELECT 1 FROM messages search_message WHERE search_message.session_id={alias}.id "
        f"AND {_indexed_public_message_sql('search_message')} "
        "AND search_message.text LIKE ? ESCAPE '\\')"
    )


def _indexed_session_match_sql(alias="sessions"):
    """Shared title, project and public full-message matching (three parameters)."""
    return (
        f"(COALESCE({alias}.title,'') LIKE ? ESCAPE '\\' "
        f"OR COALESCE({alias}.cwd,'') LIKE ? ESCAPE '\\' "
        f"OR {_indexed_body_match_sql(alias)})"
    )


def detect_runtime_system(os_name=None):
    return "windows" if str(os_name or os.name).lower() == "nt" else "linux"


def build_truncated_message_preview(text, preview_chars=MESSAGE_PREVIEW_CHARS):
    value = str(text or "")
    preview = value[:preview_chars]
    last_newline = preview.rfind("\n")
    if last_newline >= int(preview_chars * 0.6):
        preview = preview[:last_newline]
    return (
        preview.rstrip()
        + "\n\n---\nPreview truncated for performance. Expand to render the full message."
    )


def normalize_message_payload(text, *, include_full_text=False, char_count=None, is_truncated=None):
    value = str(text or "")
    total_chars = len(value) if char_count is None else int(char_count)
    truncated = bool(is_truncated) if is_truncated is not None else (
        (not include_full_text) and total_chars > MESSAGE_INLINE_FULL_THRESHOLD
    )
    rendered = value
    if not include_full_text and truncated:
        rendered = build_truncated_message_preview(value)
    return rendered, total_chars, truncated


class RecallTitleStore:
    def __init__(self, db_path: Path, source: str):
        self.db_path = Path(db_path)
        self.source = str(source or "").strip()
        self._lock = threading.Lock()

    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_meta (
                session_id TEXT PRIMARY KEY,
                source TEXT DEFAULT '',
                custom_title TEXT,
                updated_at INTEGER
            )
            """
        )
        conn.commit()
        return conn

    def get_custom_title(self, session_id):
        if not session_id:
            return None
        try:
            with self._lock:
                conn = self._connect()
                try:
                    row = conn.execute(
                        """
                        SELECT custom_title
                        FROM session_meta
                        WHERE session_id = ? AND (source = ? OR source = '')
                        """,
                        (session_id, self.source),
                    ).fetchone()
                    if not row:
                        return None
                    title = row["custom_title"]
                    if isinstance(title, str) and title.strip():
                        return title.strip()
                    return None
                finally:
                    conn.close()
        except (OSError, PermissionError, sqlite3.Error):
            return None

    def set_custom_title(self, session_id, title):
        if not session_id:
            return False
        clean_title = str(title or "").strip()
        if not clean_title:
            return False
        try:
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute(
                        """
                        INSERT INTO session_meta (session_id, source, custom_title, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(session_id) DO UPDATE SET
                            source = excluded.source,
                            custom_title = excluded.custom_title,
                            updated_at = excluded.updated_at
                        """,
                        (session_id, self.source, clean_title, int(time.time() * 1000)),
                    )
                    conn.commit()
                    return True
                finally:
                    conn.close()
        except (OSError, PermissionError, sqlite3.Error):
            return False


def parse_ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value > 1e12:
            return int(value)
        return int(value * 1000)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    return None


def build_message_preview_text(text, preview_chars=MESSAGE_PREVIEW_CHARS):
    value = str(text or "")
    if len(value) <= preview_chars:
        return value

    preview = value[:preview_chars]
    last_newline = preview.rfind("\n")
    if last_newline >= int(preview_chars * 0.6):
        preview = preview[:last_newline]
    return preview.rstrip()


def build_search_excerpt_text(
    text,
    match_start,
    match_end,
    context_chars=SEARCH_MATCH_CONTEXT_CHARS,
    max_chars=SEARCH_MATCH_MAX_CHARS,
):
    value = str(text or "")
    if not value:
        return {
            "text": "",
            "start": 0,
            "end": 0,
            "has_more_before": False,
            "has_more_after": False,
        }

    clean_start = max(0, int(match_start or 0))
    clean_end = max(clean_start, int(match_end or clean_start))
    clean_context = max(0, int(context_chars or 0))
    clean_max = max(1, int(max_chars or 1))

    excerpt_start = max(0, clean_start - clean_context)
    excerpt_end = min(len(value), clean_end + clean_context)

    if excerpt_end - excerpt_start > clean_max:
        match_width = max(1, clean_end - clean_start)
        remaining = max(0, clean_max - match_width)
        left_room = remaining // 2
        right_room = remaining - left_room
        excerpt_start = max(0, clean_start - left_room)
        excerpt_end = min(len(value), clean_end + right_room)

        window = excerpt_end - excerpt_start
        if window < clean_max:
            missing = clean_max - window
            if excerpt_start == 0:
                excerpt_end = min(len(value), excerpt_end + missing)
            elif excerpt_end == len(value):
                excerpt_start = max(0, excerpt_start - missing)

    excerpt = value[excerpt_start:excerpt_end]
    prefix = "...\n" if excerpt_start > 0 else ""
    suffix = "\n..." if excerpt_end < len(value) else ""
    return {
        "text": f"{prefix}{excerpt}{suffix}",
        "start": excerpt_start,
        "end": excerpt_end,
        "has_more_before": excerpt_start > 0,
        "has_more_after": excerpt_end < len(value),
    }


def normalize_page_args(limit, offset, default_limit=DEFAULT_PAGE_LIMIT, max_limit=DEFAULT_LIMIT):
    try:
        clean_limit = int(limit)
    except (TypeError, ValueError):
        clean_limit = int(default_limit)
    try:
        clean_offset = int(offset)
    except (TypeError, ValueError):
        clean_offset = 0

    clean_limit = max(1, min(int(max_limit), clean_limit))
    clean_offset = max(0, clean_offset)
    return clean_limit, clean_offset


def add_raw_message(messages, ts_ms, role, obj, reason="unhandled"):
    try:
        raw = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        raw = repr(obj)
    messages.append({
        "ts_ms": ts_ms,
        "role": role or "other",
        "kind": f"raw_json:{reason}",
        "text": f"```json\n{raw}\n```",
    })


def parse_date_param(value, end=False):
    if not value:
        return None
    try:
        date = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    local_tz = datetime.now().astimezone().tzinfo
    if end:
        dt = datetime.combine(date, dt_time(23, 59, 59))
    else:
        dt = datetime.combine(date, dt_time(0, 0, 0))
    dt = dt.replace(tzinfo=local_tz)
    return int(dt.timestamp() * 1000)


def slugify_path_label(value):
    text = str(value or "").strip()
    text = re.sub(r'[\\/:*?"<>|]+', "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    text = text.strip("-.")
    return text or "project"


# ---------------------------------------------------------------------------
# Plan-aware history (session-plans 004 §P1-4): recognise planning artifacts
# (planning-with-files / SpecMesh conventions) under a session's cwd. The scan
# is stateless, strictly read-only, and capped so a huge repo cannot stall the
# request.
# ---------------------------------------------------------------------------

PLAN_FILE_NAMES = ("task_plan.md", "progress.md", "findings.md")
PLAN_SCAN_MAX_FILES = 80
PLAN_FILE_MAX_CHARS = 120_000
PLAN_SECTION_PREVIEW_CHARS = 700
_PLAN_SECTION_KEYS = ("task", "goal", "plan", "status", "next step", "next action")


def extract_plan_sections(text):
    """Pull short excerpts of the well-known planning sections from a plan file.

    Returns ``{section_key: excerpt}`` for ``## `` headings whose title matches
    ``_PLAN_SECTION_KEYS`` (case-insensitive). Unknown sections are ignored so
    a long plan file cannot bloat the API payload.
    """
    sections = {}
    current_key = None
    current_lines = []

    def _flush():
        nonlocal current_key, current_lines
        if current_key and current_lines:
            body = "\n".join(current_lines).strip()
            if body:
                if len(body) > PLAN_SECTION_PREVIEW_CHARS:
                    body = body[: PLAN_SECTION_PREVIEW_CHARS - 1].rstrip() + "…"
                sections.setdefault(current_key, body)
        current_key = None
        current_lines = []

    for line in str(text or "").splitlines():
        heading = re.match(r"^#{1,3}\s+(.+?)\s*$", line)
        if heading:
            _flush()
            title = " ".join(heading.group(1).split()).lower()
            if title in _PLAN_SECTION_KEYS:
                current_key = title.replace(" ", "_")
            continue
        if current_key:
            current_lines.append(line)
    _flush()
    return sections


def scan_plan_files(cwd):
    """Collect planning artifacts under ``cwd`` (read-only).

    Recognises ``task_plan.md`` / ``progress.md`` / ``findings.md`` at the
    project root and under ``plans/*``, plus ``docs/session-plans/*.md``.
    Returns entries sorted by mtime descending; each carries short section
    excerpts instead of the full file body.
    """
    root = Path(str(cwd or "")).expanduser()
    # NOTE: Path("") silently resolves to the process CWD; reject empty input
    # so a blank project string never scans an unintended directory.
    if not str(cwd or "").strip() or not root.is_dir():
        return []

    candidates = []
    seen = set()

    def _add(path):
        try:
            resolved = str(path.resolve())
        except OSError:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(path)

    for name in PLAN_FILE_NAMES:
        _add(root / name)
    plans_dir = root / "plans"
    if plans_dir.is_dir():
        try:
            # Bound enumeration itself (not just the returned slice) so a
            # directory full of plan folders cannot make the request do
            # unbounded work.
            children = sorted(p for p in plans_dir.iterdir() if p.is_dir())[:PLAN_SCAN_MAX_FILES]
        except OSError:
            children = []
        for child in children:
            for name in PLAN_FILE_NAMES:
                _add(child / name)
    session_plans_dir = root / "docs" / "session-plans"
    if session_plans_dir.is_dir():
        try:
            for child in sorted(session_plans_dir.glob("*.md"))[:PLAN_SCAN_MAX_FILES]:
                _add(child)
        except OSError:
            pass

    items = []
    for path in candidates:
        if len(items) >= PLAN_SCAN_MAX_FILES:
            break
        try:
            if not path.is_file():
                continue
            stat = path.stat()
            # Bounded read: never pull the whole file into memory just to
            # truncate it afterwards.
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(PLAN_FILE_MAX_CHARS)
        except OSError:
            continue
        try:
            rel_path = str(path.relative_to(root))
        except ValueError:
            rel_path = str(path)
        items.append({
            "name": path.name,
            "rel_path": rel_path,
            "mtime_ms": int(stat.st_mtime * 1000),
            "size": int(stat.st_size),
            "sections": extract_plan_sections(text[:PLAN_FILE_MAX_CHARS]),
        })
    items.sort(key=lambda item: item["mtime_ms"], reverse=True)
    return items


def filter_plan_files_for_window(items, start_ms=None, end_ms=None, margin_ms=7 * 86_400_000):
    """Keep plan files whose mtime falls near a session's activity window."""
    if start_ms is None or end_ms is None:
        return list(items or [])
    low = int(start_ms) - int(margin_ms)
    high = int(end_ms) + int(margin_ms)
    return [item for item in (items or []) if low <= int(item["mtime_ms"]) <= high]


def extract_text(content_items):
    texts = []
    if not content_items:
        return ""
    for item in content_items:
        if not isinstance(item, dict):
            continue
        if "text" in item and isinstance(item["text"], str):
            texts.append(item["text"])
            continue
        item_type = item.get("type")
        if item_type in ("input_text", "output_text"):
            text = item.get("text")
            if isinstance(text, str):
                texts.append(text)
            continue
        if item_type in ("image_url", "input_image", "output_image"):
            texts.append("[image]")
            continue
    return "\n".join(t for t in texts if t)


def normalize_codex_context_message(role, kind, text):
    if not text:
        return role, kind, text, False
    s = text.strip()

    if "<permissions instructions>" in s and "</permissions instructions>" in s:
        m = re.search(r"<permissions instructions>\s*(.*?)\s*</permissions instructions>", s, re.S)
        cleaned = (m.group(1).strip() if m else s)
        # Usually developer-scoped, but keep original role if present.
        return role, "context", cleaned, True

    if "<environment_context>" in s and "</environment_context>" in s:
        m = re.search(r"<environment_context>\s*(.*?)\s*</environment_context>", s, re.S)
        inner = m.group(1) if m else ""
        pairs = re.findall(r"<([a-zA-Z0-9_]+)>(.*?)</\\1>", inner, re.S)
        lines = ["Environment context:"]
        for k, v in pairs:
            val = v.strip()
            if val:
                lines.append(f"- {k}: {val}")
        if len(lines) == 1:
            lines.append("(empty)")
        # This is harness metadata; treat as system by default.
        return "system", "context", "\n".join(lines), True

    if s.startswith("# AGENTS.md instructions for ") or "<INSTRUCTIONS>" in s:
        first = s.splitlines()[0].strip()
        skills = re.findall(r"^-\\s*([a-zA-Z0-9_-]+):", s, re.M)
        skills = sorted({x for x in skills if x})
        lines = [first]
        if skills:
            lines.append(f"Skills: {', '.join(skills)}")
        lines.append("(omitted)")
        return "system", "context", "\n".join(lines), True

    return role, kind, text, False


def _codex_try_parse_json(text):
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    if not (s.startswith("{") or s.startswith("[")):
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _codex_format_tool_use(name, call_id=None, raw_input=None, *, is_custom=False):
    tool_name = str(name or "tool").strip() or "tool"
    tool_key = tool_name.lower()
    lines = [f"Tool use: {tool_name}"]
    if call_id:
        lines.append(f"Call ID: {call_id}")

    if tool_key == "apply_patch" and isinstance(raw_input, str) and raw_input.strip():
        lines.append("Patch:")
        lines.append(f"```patch\n{raw_input.rstrip()}\n```")
        return "\n".join(lines).strip()

    parsed = _codex_try_parse_json(raw_input) if isinstance(raw_input, str) else None

    if tool_key == "shell_command" and isinstance(parsed, dict):
        command = parsed.get("command")
        workdir = parsed.get("workdir")
        if isinstance(workdir, str) and workdir.strip():
            lines.append(f"Workdir: `{workdir.strip()}`")
        if isinstance(command, str) and command.strip():
            lines.append("Command:")
            lines.append(f"```bash\n{command.rstrip()}\n```")
        else:
            lines.append("Input:")
            lines.append(f"```json\n{json.dumps(parsed, ensure_ascii=False, indent=2)}\n```")
        return "\n".join(lines).strip()

    if parsed is not None:
        lines.append("Input:")
        lines.append(f"```json\n{json.dumps(parsed, ensure_ascii=False, indent=2)}\n```")
        return "\n".join(lines).strip()

    if isinstance(raw_input, str) and raw_input.strip():
        label = "Input:" if not is_custom else "Input:"
        lines.append(label)
        lines.append(f"```\n{raw_input.rstrip()}\n```")

    return "\n".join(lines).strip()


def _codex_normalize_tool_result(raw_output, *, result_metadata=None):
    """Read known text envelopes once for both expanded and collapsed results.

    Binary content is represented by a marker. Unknown shapes stay explicit;
    the source line remains available for inspection without guessing a schema.
    """
    parts, exit_codes, error_flags, wall_times = [], [], [], []
    text_keys = {"output", "text", "content", "metadata", "exit_code", "status", "is_error", "isError", "error"}
    media_kinds = {"image", "image_url", "input_image", "output_image", "audio", "input_audio", "output_audio", "video"}
    text_kinds = {"text", "input_text", "output_text"}
    envelope_keys = text_keys | {"type", "duration_seconds", "wall_time_seconds", "chunk_id",
                                 "original_token_count", "session_id", "_meta", "structuredContent"}

    def is_envelope(value, depth=0):
        if depth > 20:
            return True  # collect() will expose the bounded fallback.
        if isinstance(value, dict):
            kind = value.get("type")
            if isinstance(kind, str) and kind in media_kinds | {"resource"}:
                return True
            if isinstance(kind, str) and kind in text_kinds and isinstance(value.get("text"), str):
                return True
            keys = set(value)
            # A familiar field alone does not make application JSON a transport
            # envelope. Preserve extra business fields instead of dropping them.
            return keys <= envelope_keys and bool(keys & {"output", "text", "content", "metadata"})
        return (isinstance(value, list) and bool(value)
                and all(is_envelope(item, depth + 1) for item in value))

    def media_safe(value, depth=0):
        """Preserve application JSON fields while omitting binary media values."""
        if depth > 20:
            return "[unsupported tool output: nesting limit]", True
        if isinstance(value, dict):
            kind = value.get("type")
            if isinstance(kind, str) and kind in media_kinds:
                return f"[{kind} content omitted]", True
            resource = value.get("resource")
            if kind == "resource" and isinstance(resource, dict) and "blob" in resource:
                return "[resource content omitted]", True
            result, changed = {}, False
            for key, item in value.items():
                result[key], omitted = media_safe(item, depth + 1)
                changed = changed or omitted
            return result, changed
        if isinstance(value, list):
            result, changed = [], False
            for item in value:
                safe, omitted = media_safe(item, depth + 1)
                result.append(safe)
                changed = changed or omitted
            return result, changed
        if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
            return "[encoded media content omitted]", True
        return value, False

    def media_safe_text(value, depth):
        text = value.strip("\n")
        parsed = _codex_try_parse_json(text)
        safe, omitted = media_safe(parsed if parsed is not None else text, depth)
        if omitted:
            return json.dumps(safe, ensure_ascii=False, indent=2) if parsed is not None else safe
        return text

    def metadata(value):
        code = value.get("exit_code")
        if isinstance(code, (int, float)) and not isinstance(code, bool):
            try:
                exit_codes.append(int(code))
            except (ValueError, OverflowError):
                pass
        for key in ("is_error", "isError"):
            if isinstance(value.get(key), bool):
                error_flags.append(value[key])
        error = value.get("error")
        if error:
            error_flags.append(True)
        status = value.get("status")
        if status in ("error", "failed", "failure"):
            error_flags.append(True)
        elif status in ("ok", "success", "succeeded"):
            error_flags.append(False)
        duration = value.get("duration_seconds", value.get("wall_time_seconds"))
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            wall_times.append(f"{duration:.3f}s")

    def collect(value, depth=0, *, body=False):
        if value is None or value == "":
            return
        if depth > 20:
            parts.append("[unsupported tool output: nesting limit]")
            return
        if isinstance(value, str):
            # A textual body is opaque application output, even when it looks
            # like a JSON result envelope. Only redact recognized media bytes.
            if body:
                text = media_safe_text(value, depth + 1)
                if text.strip():
                    parts.append(text)
                return
            parsed = _codex_try_parse_json(value)
            if is_envelope(parsed):
                collect(parsed, depth + 1)
                return
            header, separator, output = value.partition("\nOutput:\n")
            metadata_text = header if separator else value
            exit_codes.extend(int(code) for code in re.findall(r"^Exit code:\s*(-?\d+)\s*$", metadata_text, re.M))
            match = re.search(r"^Wall time:\s*(.+?)\s*$", metadata_text, re.M)
            if match:
                wall_times.append(match.group(1).strip())
            text = media_safe_text(output if separator else value, depth + 1)
            if text.strip():
                parts.append(text)
            return
        if isinstance(value, list):
            for item in value:
                collect(item, depth + 1, body=body)
            return
        if isinstance(value, dict):
            kind = value.get("type")
            if not body and set(value) & {"output", "text", "content", "metadata"} and not is_envelope(value):
                # Native application objects follow the same opaque-JSON path
                # as their string representation, including media omission.
                body = True
            if not body:
                metadata(value)
                meta = value.get("metadata")
                if isinstance(meta, dict):
                    metadata(meta)
            if isinstance(kind, str) and kind in media_kinds:
                parts.append(f"[{kind} content omitted]")
                return
            if kind == "resource" and isinstance(value.get("resource"), dict):
                resource = value["resource"]
                if isinstance(resource.get("text"), str):
                    collect(resource["text"], depth + 1, body=True)
                else:
                    parts.append("[resource content omitted]")
                return
            if body:
                if isinstance(kind, str) and kind in text_kinds and isinstance(value.get("text"), str):
                    collect(value["text"], depth + 1, body=True)
                else:
                    safe, _ = media_safe(value, depth + 1)
                    parts.append(json.dumps(safe, ensure_ascii=False, indent=2))
                return
            known = bool(set(value) & text_keys)
            for key in ("output", "text", "content"):
                if value.get(key) is not None and value[key] != "" and value[key] != []:
                    collect(value[key], depth + 1, body=True)
                    break
            error = value.get("error")
            error_text = error.get("message") if isinstance(error, dict) else error
            if isinstance(error_text, str) and error_text and error_text not in parts:
                collect(error_text, depth + 1, body=True)
            if not known:
                parts.append("[unsupported tool output: object]")
            return
        parts.append(f"[unsupported tool output: {type(value).__name__}]")

    collect(raw_output)
    # Native response metadata annotates an already normalized tool result. It
    # must not turn that result into opaque stdout or hide its explicit failure.
    if isinstance(result_metadata, dict):
        metadata(result_metadata)
        meta = result_metadata.get("metadata")
        if isinstance(meta, dict):
            metadata(meta)
        error = result_metadata.get("error")
        error_text = error.get("message") if isinstance(error, dict) else error
        if isinstance(error_text, str) and error_text and error_text not in parts:
            collect(error_text, body=True)
    # A wrapper may contain several command results. Preserve any explicit
    # failure rather than allowing a later successful result to hide it.
    exit_code = next((code for code in exit_codes if code != 0), exit_codes[-1] if exit_codes else None)
    is_error = any(error_flags) or any(code != 0 for code in exit_codes)
    status = "error" if is_error else "ok" if exit_codes or error_flags else None
    return {"body": "\n".join(parts), "exit_code": exit_code,
            "wall_time": wall_times[0] if wall_times else None, "is_error": is_error,
            "exit_status": status}


def _codex_format_tool_result(tool_name=None, call_id=None, raw_output=None, *, result_metadata=None):
    tool_label = str(tool_name).strip() if tool_name else ""
    header = f"Tool result: {tool_label}" if tool_label else "Tool result:"
    lines = [header]
    if call_id:
        lines.append(f"Call ID: {call_id}")

    result = _codex_normalize_tool_result(raw_output, result_metadata=result_metadata)
    exit_code, wall_time, body = result["exit_code"], result["wall_time"], result["body"]
    if result["exit_status"]:
        lines.append(f"Status: {result['exit_status']}")
    if exit_code is not None:
        lines.append(f"Exit code: {exit_code}")
    if wall_time:
        lines.append(f"Wall time: {wall_time}")
    if isinstance(body, str) and body.strip():
        lines.append("Output:")
        lines.append(f"````\n{body.rstrip()}\n````")
    else:
        lines.append("Output: [empty tool output]")

    return "\n".join(lines).strip()


# BDD: spec docs/session-plans/002 §M5 — every tool_use/tool_result message
# carries a structured `tool_summary` so the transcript UI can render collapsed
# one-line rows without re-parsing the raw text.
_TOOL_CATEGORY_MAP = {
    "shell_command": "shell", "bash": "shell", "powershell": "shell", "cmd": "shell", "sh": "shell",
    "apply_patch": "edit", "str_replace_editor": "edit", "write": "edit", "edit": "edit",
    "multi_edit": "edit", "create_file": "edit", "delete_file": "edit",
    "read_file": "read", "read": "read", "get_file_contents": "read", "view": "read",
    "grep": "search", "glob": "search", "search": "search", "find": "search",
    "webfetch": "deploy", "web_search": "deploy", "curl": "deploy",
    "todo_write": "deploy", "update_plan": "deploy", "task": "deploy",
    "askuserquestion": "deploy",
}


def _classify_tool_category(name):
    key = str(name or "").lower().strip()
    if not key:
        return "other"
    if key in _TOOL_CATEGORY_MAP:
        return _TOOL_CATEGORY_MAP[key]
    if key.startswith("text_editor"):
        return "edit"
    return "other"


def _truncate_str(text, max_len):
    if text is None:
        return None
    s = " ".join(str(text).split())
    if not s:
        return None
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


def _empty_tool_summary(name):
    return {
        "name": str(name or "tool")[:80],
        "category": _classify_tool_category(name),
        "headline": None,
        "file_path": None,
        "change_kind": None,
        "lines_added": None,
        "lines_removed": None,
        "exit_status": None,
        "exit_code": None,
        "output_preview": None,
        "is_error": False,
    }


def _count_diff_lines(patch_text):
    # ALGO: count +/- lines inside @@ hunks, skipping +++/---/*** file markers.
    added = 0
    removed = 0
    in_hunk = False
    for line in patch_text.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith(("+++", "---", "***")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _codex_summarize_tool_use(name, raw_input=None, *, is_custom=False):
    s = _empty_tool_summary(name)
    parsed = _codex_try_parse_json(raw_input) if isinstance(raw_input, str) else None
    cat = s["category"]
    if cat == "shell" and isinstance(parsed, dict):
        cmd = parsed.get("command")
        if isinstance(cmd, str) and cmd.strip():
            s["headline"] = _truncate_str(cmd, 80)
        wd = parsed.get("workdir")
        if isinstance(wd, str) and wd.strip():
            s["file_path"] = wd.strip()
    elif cat == "edit":
        if str(name or "").lower() == "apply_patch" and isinstance(raw_input, str):
            body = raw_input.strip()
            current_path = None
            change_kind = None
            for line in body.splitlines():
                if not line.startswith("*** "):
                    continue
                marker = line[4:].strip()
                for prefix, kind in (
                    ("Add File:", "create"),
                    ("Delete File:", "delete"),
                    ("Update File:", "modify"),
                    ("Modified File:", "modify"),
                ):
                    if marker.startswith(prefix):
                        change_kind = kind
                        current_path = marker[len(prefix):].strip()
                        break
                if change_kind:
                    break
            if current_path:
                s["file_path"] = current_path
            if change_kind:
                s["change_kind"] = change_kind
            added, removed = _count_diff_lines(body)
            s["lines_added"] = added
            s["lines_removed"] = removed
            if current_path and change_kind:
                s["headline"] = _truncate_str(f"{change_kind} {current_path}", 80)
        elif isinstance(parsed, dict):
            path = parsed.get("path") or parsed.get("file_path")
            if isinstance(path, str) and path.strip():
                s["file_path"] = path.strip()
            cmd = parsed.get("command")
            if isinstance(cmd, str) and cmd.strip():
                key = cmd.lower().strip()
                s["change_kind"] = "create" if key == "create" else "delete" if key == "delete" else "modify"
            if s["file_path"] and s["change_kind"]:
                s["headline"] = _truncate_str(f"{s['change_kind']} {s['file_path']}", 80)
    elif cat == "read" and isinstance(parsed, dict):
        path = parsed.get("path") or parsed.get("file_path")
        if isinstance(path, str) and path.strip():
            s["file_path"] = path.strip()
            s["headline"] = _truncate_str(path.strip(), 80)
    elif cat == "search" and isinstance(parsed, dict):
        pattern = parsed.get("pattern")
        path = parsed.get("path")
        parts = []
        if isinstance(pattern, str) and pattern.strip():
            parts.append(pattern.strip())
        if isinstance(path, str) and path.strip():
            parts.append(f"in {path.strip()}")
        if parts:
            s["headline"] = _truncate_str(" ".join(parts), 80)
    elif cat == "deploy" and isinstance(parsed, dict):
        url = parsed.get("url") or parsed.get("query") or parsed.get("prompt")
        if isinstance(url, str) and url.strip():
            s["headline"] = _truncate_str(url.strip(), 80)
        elif str(name or "").lower() in ("todo_write", "update_plan"):
            todos = parsed.get("todos")
            if isinstance(todos, list):
                s["headline"] = f"plan: {len(todos)} items"
    if s["headline"] is None and isinstance(parsed, dict) and parsed:
        for k, v in list(parsed.items())[:1]:
            if v is not None and v != "":
                s["headline"] = _truncate_str(f"{k}: {v}", 80)
                break
    return s


def _codex_summarize_tool_result(tool_name=None, raw_output=None, *, result_metadata=None):
    s = _empty_tool_summary(tool_name or "tool")
    result = _codex_normalize_tool_result(raw_output, result_metadata=result_metadata)
    s.update({key: result[key] for key in ("exit_code", "exit_status", "is_error")})
    if result["body"]:
        s["output_preview"] = _truncate_str(result["body"], 200)
    return s


def _claude_summarize_tool_use(item):
    name = item.get("name") if isinstance(item, dict) else None
    s = _empty_tool_summary(name)
    tool_input = item.get("input") if isinstance(item, dict) and isinstance(item.get("input"), dict) else None
    if not tool_input:
        return s
    cat = s["category"]
    if cat == "shell":
        cmd = tool_input.get("command")
        if isinstance(cmd, str) and cmd.strip():
            s["headline"] = _truncate_str(cmd, 80)
    elif cat == "edit":
        path = tool_input.get("file_path") or tool_input.get("path")
        if isinstance(path, str) and path.strip():
            s["file_path"] = path.strip()
        cmd = tool_input.get("command")
        if isinstance(cmd, str) and cmd.strip():
            key = cmd.lower().strip()
            s["change_kind"] = "create" if key == "create" else "delete" if key == "delete" else "modify"
        new_s = tool_input.get("new_str") or tool_input.get("new_string") or tool_input.get("file_text")
        old_s = tool_input.get("old_str") or tool_input.get("old_string")
        if isinstance(new_s, str) or isinstance(old_s, str):
            s["lines_added"] = len([ln for ln in (new_s or "").splitlines() if ln.strip()])
            s["lines_removed"] = len([ln for ln in (old_s or "").splitlines() if ln.strip()])
        if s["file_path"] and s["change_kind"]:
            s["headline"] = _truncate_str(f"{s['change_kind']} {s['file_path']}", 80)
    elif cat == "read":
        path = tool_input.get("file_path") or tool_input.get("path")
        if isinstance(path, str) and path.strip():
            s["file_path"] = path.strip()
            s["headline"] = _truncate_str(path.strip(), 80)
    elif cat == "search":
        pattern = tool_input.get("pattern")
        path = tool_input.get("path")
        parts = []
        if isinstance(pattern, str) and pattern.strip():
            parts.append(pattern.strip())
        if isinstance(path, str) and path.strip():
            parts.append(f"in {path.strip()}")
        if parts:
            s["headline"] = _truncate_str(" ".join(parts), 80)
    elif cat == "deploy":
        url = tool_input.get("url") or tool_input.get("query") or tool_input.get("prompt")
        if isinstance(url, str) and url.strip():
            s["headline"] = _truncate_str(url.strip(), 80)
        elif str(name or "").lower() == "todo_write":
            todos = tool_input.get("todos")
            if isinstance(todos, list):
                s["headline"] = f"plan: {len(todos)} items"
    if s["headline"] is None:
        for k, v in list(tool_input.items())[:1]:
            if v is not None and v != "":
                s["headline"] = _truncate_str(f"{k}: {v}", 80)
                break
    return s


def _claude_summarize_tool_result(tool_result_item, tool_use_result=None):
    s = _empty_tool_summary("tool")
    is_error = None
    content = None
    if isinstance(tool_result_item, dict):
        is_error = tool_result_item.get("is_error")
        content = tool_result_item.get("content")
    stdout = None
    stderr = None
    if isinstance(tool_use_result, dict):
        stdout = tool_use_result.get("stdout")
        stderr = tool_use_result.get("stderr")
    if is_error is True:
        s["exit_status"] = "error"
        s["is_error"] = True
    elif is_error is False:
        s["exit_status"] = "ok"
        s["is_error"] = False
    body = None
    if isinstance(content, str) and content.strip():
        body = content.strip()
    elif isinstance(stdout, str) or isinstance(stderr, str):
        combined = ""
        if isinstance(stdout, str) and stdout:
            combined += stdout
        if isinstance(stderr, str) and stderr:
            if combined and not combined.endswith("\n"):
                combined += "\n"
            combined += stderr
        body = combined.strip() or None
    elif content is not None:
        body = json.dumps(content, ensure_ascii=False)
    if body:
        s["output_preview"] = _truncate_str(body, 200)
    return s


def _codex_usage_from_token_count(info):
    # ALGO: `total_token_usage` is cumulative across the session; keep the
    # largest total seen so mid-session zeros / retries never lower it.
    if not isinstance(info, dict):
        return None
    usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        return None

    def _int(key):
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    total = _int("total_tokens")
    return {
        "input": _int("input_tokens"),
        "output": _int("output_tokens"),
        "cached": _int("cached_input_tokens"),
        "reasoning": _int("reasoning_output_tokens"),
        "total": total,
    }


def _usage_greater(candidate, current):
    if not isinstance(candidate, dict):
        return False
    return candidate.get("total", 0) > (current or {}).get("total", 0)


def parse_codex_session_file(path: Path, *, content: bytes | None = None):
    from .trace_markers import style_markers, declared_skill_markers, skill_tool_markers
    session_id = None
    start_ts_ms = None
    end_ts_ms = None
    cwd = None
    title = None
    message_count = 0
    messages = []
    search_parts = []
    search_len = 0
    tool_names = {}
    usage_totals = None
    relations = {}
    marker_cwd = None
    marker_turn_id = None

    def marker_fields(markers):
        if not markers:
            return {}
        return {'trace_markers': markers, 'trace_context': {
            'cwd': marker_cwd or cwd, 'turn_id': marker_turn_id,
            'basis': 'recorded_context'}}

    def add_search(text):
        nonlocal search_len
        if not text:
            return
        if search_len >= MAX_SEARCH_CHARS:
            return
        remaining = MAX_SEARCH_CHARS - search_len
        if len(text) > remaining:
            text = text[:remaining]
        search_parts.append(text)
        search_len += len(text)

    valid_json_count = 0
    try:
        stream = io.TextIOWrapper(io.BytesIO(content), encoding='utf-8') if content is not None else path.open("r", encoding="utf-8")
        with stream as f:
            for line_no, line in enumerate(f, 1):
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    obj = json.loads(line)
                    valid_json_count += 1
                except Exception:
                    messages.append({
                        "ts_ms": start_ts_ms or end_ts_ms or 0,
                        "role": "other",
                        "kind": "raw_json:malformed_line",
                        "text": f"```\n{line.rstrip()}\n```",
                    })
                    continue

                ts_ms = parse_ts(obj.get("timestamp"))
                if ts_ms is not None:
                    if start_ts_ms is None or ts_ms < start_ts_ms:
                        start_ts_ms = ts_ms
                    if end_ts_ms is None or ts_ms > end_ts_ms:
                        end_ts_ms = ts_ms

                obj_type = obj.get("type")
                if obj_type == "session_meta":
                    payload = obj.get("payload", {})
                    session_id = payload.get("id", session_id)
                    cwd = payload.get("cwd", cwd)
                    parent = payload.get('forked_from_id') or payload.get('parent_session_id')
                    origin = payload.get('source')
                    if isinstance(origin, dict):
                        agent = origin.get('subagent')
                        spawn = agent.get('thread_spawn') if isinstance(agent, dict) else None
                        if isinstance(spawn, dict):
                            parent = parent or spawn.get('parent_thread_id')
                    if isinstance(parent, str):
                        relations['parent_session_id'] = parent
                    meta_ts = parse_ts(payload.get("timestamp"))
                    if meta_ts is not None:
                        if start_ts_ms is None or meta_ts < start_ts_ms:
                            start_ts_ms = meta_ts
                        if end_ts_ms is None or meta_ts > end_ts_ms:
                            end_ts_ms = meta_ts
                elif obj_type == 'turn_context':
                    payload = obj.get('payload')
                    marker_turn_id = None
                    if isinstance(payload, dict):
                        if isinstance(payload.get('cwd'), str):
                            marker_cwd = payload['cwd']
                        if isinstance(payload.get('turn_id'), str):
                            marker_turn_id = payload['turn_id']
                    add_raw_message(messages, ts_ms, 'other', obj, reason='type:turn_context')
                elif obj_type == "response_item":
                    payload = obj.get("payload", {})
                    payload_type = payload.get("type")
                    if payload_type == "message":
                        role = payload.get("role", "unknown")
                        text = extract_text(payload.get("content", []))
                        if title is None and role == "user":
                            title = meaningful_user_title(text)
                        markers = style_markers(role, text) + declared_skill_markers(role, text)
                        role, kind, text, is_context = normalize_codex_context_message(role, "message", text)
                        if text:
                            messages.append({
                                "ts_ms": ts_ms,
                                "raw_ref": {"line_no": line_no},
                                "role": role,
                                "kind": kind,
                                "text": text,
                                **marker_fields(markers),
                            })
                            if not is_context:
                                message_count += 1
                                add_search(text)
                    elif payload_type == "reasoning":
                        summary = payload.get("summary")
                        if summary:
                            parts = []
                            for item in summary:
                                if not isinstance(item, dict):
                                    continue
                                if item.get("type") == "summary_text":
                                    txt = item.get("text", "")
                                    if txt:
                                        parts.append(txt)
                                elif "text" in item:
                                    txt = item.get("text", "")
                                    if isinstance(txt, str) and txt:
                                        parts.append(txt)
                            text = "\n".join(parts).strip()
                            if text:
                                messages.append({
                                    "ts_ms": ts_ms,
                                    "raw_ref": {"line_no": line_no},
                                    "role": "assistant",
                                    "kind": "reasoning_summary",
                                    "text": text,
                                })
                                add_search(text)
                    elif payload_type in ("function_call", "custom_tool_call"):
                        name = payload.get("name") or "tool"
                        call_id = payload.get("call_id")
                        if not isinstance(call_id, str) or not call_id.strip():
                            call_id = None
                        if name == "update_plan":
                            if call_id:
                                tool_names[call_id] = str(name)
                            continue
                        raw_input = payload.get("arguments") if payload_type == "function_call" else payload.get("input")
                        if call_id:
                            tool_names[call_id] = str(name)
                        text = _codex_format_tool_use(name, call_id=call_id, raw_input=raw_input, is_custom=(payload_type == "custom_tool_call"))
                        if text:
                            messages.append({
                                "ts_ms": ts_ms,
                                "raw_ref": {"line_no": line_no},
                                "role": "tool",
                                "kind": "tool_use",
                                "tool_call_id": call_id,
                                "text": text,
                                "tool_summary": _codex_summarize_tool_use(name, raw_input, is_custom=(payload_type == "custom_tool_call")),
                                **marker_fields(skill_tool_markers(name, raw_input)),
                            })
                            add_search(text)
                    elif payload_type in ("function_call_output", "custom_tool_call_output"):
                        call_id = payload.get("call_id")
                        if not isinstance(call_id, str) or not call_id.strip():
                            call_id = None
                        name = tool_names.get(call_id)
                        if name == "update_plan":
                            continue
                        raw_output = payload.get("output") if "output" in payload else payload.get("content")
                        # Some recordings put status beside output/content.
                        result_metadata = {key: payload[key] for key in (
                            "metadata", "exit_code", "status", "is_error", "isError", "error",
                            "duration_seconds", "wall_time_seconds") if key in payload}
                        text = _codex_format_tool_result(name, call_id=call_id, raw_output=raw_output,
                                                         result_metadata=result_metadata)
                        summary = _codex_summarize_tool_result(name, raw_output=raw_output,
                                                               result_metadata=result_metadata)
                        if text:
                            messages.append({
                                "ts_ms": ts_ms,
                                "raw_ref": {"line_no": line_no},
                                "role": "tool",
                                "kind": "tool_result",
                                "tool_call_id": call_id,
                                "text": text,
                                "tool_summary": summary,
                                **({"tool_result_error": summary["is_error"]} if summary["exit_status"] else {}),
                            })
                            add_search(text)
                    else:
                        add_raw_message(messages, ts_ms, "other", obj, reason=f"response_item:{payload_type or 'unknown'}")
                elif obj_type == "event_msg":
                    payload = obj.get("payload", {})
                    payload_type = payload.get("type")
                    if payload_type == "token_count":
                        # Telemetry, not transcript: aggregate usage and skip
                        # so token_count events don't flood the raw feed.
                        candidate = _codex_usage_from_token_count(payload.get("info"))
                        if _usage_greater(candidate, usage_totals):
                            usage_totals = candidate
                    elif payload_type == "agent_reasoning":
                        text = payload.get("text", "")
                        if isinstance(text, str) and text:
                            messages.append({
                                "ts_ms": ts_ms,
                                "raw_ref": {"line_no": line_no},
                                "role": "assistant",
                                "kind": "agent_reasoning",
                                "text": text,
                            })
                            add_search(text)
                    else:
                        add_raw_message(messages, ts_ms, "other", obj, reason=f"event_msg:{payload.get('type') or 'unknown'}")
                else:
                    add_raw_message(messages, ts_ms, "other", obj, reason=f"type:{obj_type or 'unknown'}")
    except FileNotFoundError:
        return None

    if valid_json_count == 0:
        raise ValueError("invalid_session_file: %s" % path)

    if not session_id:
        session_id = f"file-{path.stem}"

    if start_ts_ms is None:
        start_ts_ms = end_ts_ms or 0
    if end_ts_ms is None:
        end_ts_ms = start_ts_ms

    if not title:
        title = f"Session {session_id[:8]}"

    search_blob = "\n".join(search_parts)

    return {
        "id": session_id,
        "file_path": str(path),
        "start_ts_ms": int(start_ts_ms),
        "end_ts_ms": int(end_ts_ms),
        "cwd": cwd,
        "title": title,
        "message_count": message_count,
        "messages": messages,
        "search_blob": search_blob,
        "usage": usage_totals,
        "activity_metadata": relations,
    }


def _claude_extract_text_item(item):
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type == "text":
        text = item.get("text")
        return text if isinstance(text, str) and text else None
    if "text" in item and isinstance(item["text"], str) and item["text"]:
        return item["text"]
    return None


def _claude_format_tool_use(item):
    name = item.get("name") or "tool"
    tool_id = item.get("id")
    tool_input = item.get("input")
    lines = [f"Tool use: {name}"]
    if tool_id:
        lines.append(f"Tool ID: {tool_id}")
    if isinstance(tool_input, dict):
        desc = tool_input.get("description")
        if isinstance(desc, str) and desc.strip():
            lines.append(f"Description: {desc.strip()}")

        tool_name = str(name or "").strip()
        tool_key = tool_name.lower()

        if "command" in tool_input and isinstance(tool_input["command"], str):
            lines.append("Command:")
            lines.append(f"```bash\n{tool_input['command']}\n```")
        elif "file_path" in tool_input and isinstance(tool_input["file_path"], str):
            lines.append(f"File: {tool_input['file_path']}")
        elif tool_key == "grep":
            pattern = tool_input.get("pattern")
            path = tool_input.get("path")
            output_mode = tool_input.get("output_mode")
            head_limit = tool_input.get("head_limit")
            if isinstance(pattern, str) and pattern:
                lines.append(f"Pattern: `{pattern}`")
            if isinstance(path, str) and path:
                lines.append(f"Path: `{path}`")
            if isinstance(output_mode, str) and output_mode:
                lines.append(f"Mode: `{output_mode}`")
            if isinstance(head_limit, int):
                lines.append(f"Limit: `{head_limit}`")
        elif tool_key == "glob":
            pattern = tool_input.get("pattern")
            path = tool_input.get("path")
            if isinstance(pattern, str) and pattern:
                lines.append(f"Pattern: `{pattern}`")
            if isinstance(path, str) and path:
                lines.append(f"Path: `{path}`")
        elif tool_key == "askuserquestion":
            questions = tool_input.get("questions")
            if isinstance(questions, list) and questions:
                for q in questions:
                    if not isinstance(q, dict):
                        continue
                    header = q.get("header")
                    question = q.get("question")
                    if isinstance(header, str) and header.strip():
                        lines.append(f"Question ({header.strip()}):")
                    else:
                        lines.append("Question:")
                    if isinstance(question, str) and question.strip():
                        lines.append(question.strip())
                    options = q.get("options")
                    if isinstance(options, list) and options:
                        lines.append("Options:")
                        for opt in options:
                            if not isinstance(opt, dict):
                                continue
                            label = opt.get("label")
                            desc = opt.get("description")
                            if isinstance(label, str) and label.strip():
                                if isinstance(desc, str) and desc.strip():
                                    lines.append(f"- {label.strip()} — {desc.strip()}")
                                else:
                                    lines.append(f"- {label.strip()}")
                    multi = q.get("multiSelect")
                    if isinstance(multi, bool):
                        lines.append(f"Multi-select: `{str(multi).lower()}`")
        else:
            # Keep other tool inputs visible but compact.
            other = {k: v for k, v in tool_input.items() if k not in ("description", "command")}
            if other:
                lines.append("Input:")
                for k, v in other.items():
                    if isinstance(v, str):
                        value = v.strip()
                    else:
                        value = json.dumps(v, ensure_ascii=False)
                    if value:
                        lines.append(f"- {k}: {value}")
    elif tool_input is not None:
        lines.append("Input:")
        lines.append(f"```json\n{json.dumps(tool_input, ensure_ascii=False, indent=2)}\n```")
    return "\n".join(lines).strip()


def _claude_format_tool_result(tool_result_item, tool_use_result=None):
    tool_use_id = None
    is_error = None
    content = None
    if isinstance(tool_result_item, dict):
        tool_use_id = tool_result_item.get("tool_use_id")
        is_error = tool_result_item.get("is_error")
        content = tool_result_item.get("content")

    lines = ["Tool result:"]
    if tool_use_id:
        lines.append(f"Tool use ID: {tool_use_id}")
    if is_error is True:
        lines.append("Status: error")
    elif is_error is False:
        lines.append("Status: ok")

    stdout = stderr = None
    if isinstance(tool_use_result, dict):
        stdout = tool_use_result.get("stdout")
        stderr = tool_use_result.get("stderr")

    if isinstance(content, str) and content.strip():
        lines.append("Output:")
        lines.append(f"````\n{content}\n````")
    elif isinstance(stdout, str) or isinstance(stderr, str):
        combined = ""
        if isinstance(stdout, str) and stdout:
            combined += stdout
        if isinstance(stderr, str) and stderr:
            if combined and not combined.endswith("\n"):
                combined += "\n"
            combined += stderr
        lines.append("Output:")
        lines.append(f"````\n{combined}\n````")
    elif content is not None:
        lines.append("Output:")
        lines.append(f"````\n{json.dumps(content, ensure_ascii=False, indent=2)}\n````")

    return "\n".join(lines).strip()


def _claude_usage_from_message(msg):
    # ALGO: Claude Code reports per-message usage on assistant records; the
    # session total is the sum over *unique* messages. Cache reads/creation
    # are reported separately from billed input tokens.
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None

    def _int(key):
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    return {
        "input": _int("input_tokens"),
        "output": _int("output_tokens"),
        "cached": _int("cache_creation_input_tokens") + _int("cache_read_input_tokens"),
        "reasoning": 0,
        "total": _int("input_tokens") + _int("output_tokens")
        + _int("cache_creation_input_tokens") + _int("cache_read_input_tokens"),
    }


_EMPTY_USAGE = {"input": 0, "output": 0, "cached": 0, "reasoning": 0, "total": 0}


def _accumulate_claude_usage(usage_by_id, usage_unkeyed, msg):
    """Fold one assistant record's usage into the dedup accumulators.

    Claude Code splits one assistant message (same ``message.id``) across
    several JSONL records — text / tool_use blocks each re-carry the same
    usage. Counting every record would multiply the totals, so per-id we keep
    the variant with the largest total (records for one id report identical
    or monotonically updated counts). Records without an id (legacy format)
    cannot be deduped and are summed as-is.
    """
    candidate = _claude_usage_from_message(msg)
    if not candidate:
        return
    mid = msg.get("id")
    if isinstance(mid, str) and mid:
        prev = usage_by_id.get(mid)
        if prev is None or candidate["total"] > prev["total"]:
            usage_by_id[mid] = candidate
    else:
        for key in usage_unkeyed:
            usage_unkeyed[key] += candidate.get(key, 0)


def _finalize_claude_usage(usage_by_id, usage_unkeyed):
    usage_totals = dict(_EMPTY_USAGE)
    for per in usage_by_id.values():
        for key in usage_totals:
            usage_totals[key] += per.get(key, 0)
    for key in usage_totals:
        usage_totals[key] += usage_unkeyed[key]
    return usage_totals


def parse_claude_session_file(path: Path):
    from .trace_markers import style_markers, declared_skill_markers, skill_tool_markers
    session_id = None
    start_ts_ms = None
    end_ts_ms = None
    cwd = None
    title = None
    message_count = 0
    messages = []
    search_parts = []
    search_len = 0
    usage_by_id = {}
    usage_unkeyed = dict(_EMPTY_USAGE)

    def add_search(text):
        nonlocal search_len
        if not text:
            return
        if search_len >= MAX_SEARCH_CHARS:
            return
        remaining = MAX_SEARCH_CHARS - search_len
        if len(text) > remaining:
            text = text[:remaining]
        search_parts.append(text)
        search_len += len(text)

    def add_message(ts_ms, role, kind, text, count_for_stats=False, tool_summary=None,
                    trace_markers=None, tool_call_id=None, block_index=None):
        nonlocal message_count, title
        if not text:
            return
        msg = {
            "ts_ms": ts_ms,
            "raw_ref": {"line_no": line_no, "record_id": obj.get("uuid") if isinstance(obj, dict) else None,
                        "parent_message_id": obj.get("parentUuid") if isinstance(obj, dict) else None},
            "role": role,
            "kind": kind,
            "text": text,
        }
        if isinstance(tool_summary, dict):
            msg["tool_summary"] = tool_summary
        if isinstance(tool_call_id, str) and tool_call_id.strip():
            msg['tool_call_id'] = tool_call_id
        if block_index is not None:
            msg['raw_ref']['json_pointer'] = f'/message/content/{block_index}'
        markers = trace_markers if trace_markers is not None else (
            style_markers(role, text) + declared_skill_markers(role, text))
        if markers:
            msg['trace_markers'] = markers
            msg['trace_context'] = {'cwd': obj.get('cwd') or cwd, 'basis': 'recorded_context'}
        messages.append(msg)
        add_search(text)
        if count_for_stats:
            message_count += 1
            if title is None and role == "user":
                first_line = text.strip().splitlines()[0] if text.strip() else ""
                if first_line and first_line.strip().lower() != "warmup":
                    title = first_line[:80]

    valid_json_count = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                obj = None
                try:
                    obj = json.loads(line)
                    valid_json_count += 1
                except Exception:
                    add_message(start_ts_ms or end_ts_ms or 0, "other", "raw_json:malformed_line", f"```\n{line.rstrip()}\n```", count_for_stats=False)
                    continue

                if not session_id and isinstance(obj, dict) and isinstance(obj.get("sessionId"), str):
                    session_id = obj.get("sessionId")

                ts_ms = parse_ts(obj.get("timestamp")) if isinstance(obj, dict) else None
                if ts_ms is not None:
                    if start_ts_ms is None or ts_ms < start_ts_ms:
                        start_ts_ms = ts_ms
                    if end_ts_ms is None or ts_ms > end_ts_ms:
                        end_ts_ms = ts_ms

                if isinstance(obj, dict) and not cwd and isinstance(obj.get("cwd"), str) and obj.get("cwd"):
                    cwd = obj.get("cwd")

                if not isinstance(obj, dict):
                    continue

                obj_type = obj.get("type")
                if obj_type == "summary":
                    summary = obj.get("summary")
                    if isinstance(summary, str) and summary.strip() and not title:
                        title = summary.strip()[:80]
                    continue
                if obj_type == "file-history-snapshot":
                    continue

                attachment = obj.get('attachment')
                if (obj_type == 'attachment' and isinstance(attachment, dict)
                        and attachment.get('type') == 'hook_additional_context'
                        and attachment.get('hookEvent') == 'SessionStart'):
                    content = attachment.get('content')
                    texts = [content] if isinstance(content, str) else content if isinstance(content, list) else []
                    found = False
                    for block_index, text in enumerate(texts):
                        markers = style_markers('system', text)
                        if not markers:
                            continue
                        for marker in markers:
                            marker.update(evidence_origin='recorded_hook_context', hook_event='SessionStart')
                        add_message(ts_ms, 'system', 'context:output_style', text, trace_markers=markers)
                        messages[-1]['raw_ref']['json_pointer'] = (
                            '/attachment/content' if isinstance(content, str)
                            else f'/attachment/content/{block_index}')
                        found = True
                    if found:
                        continue

                msg = obj.get("message")
                if not isinstance(msg, dict):
                    add_raw_message(messages, ts_ms, "other", obj, reason=f"type:{obj_type or 'missing_message'}")
                    continue

                role = msg.get("role") or obj_type
                content = msg.get("content")

                if role in ('system', 'developer'):
                    text = content if isinstance(content, str) else extract_text(content if isinstance(content, list) else [])
                    markers = style_markers(role, text)
                    if markers:
                        add_message(ts_ms, role, 'context:output_style', text, trace_markers=markers)
                        continue

                if role == "user":
                    tool_use_result = obj.get("toolUseResult") if isinstance(obj.get("toolUseResult"), dict) else None
                    if isinstance(content, str):
                        text = content.strip()
                        add_message(ts_ms, "user", "message", text, count_for_stats=True)
                    elif isinstance(content, list):
                        for block_index, item in enumerate(content):
                            if not isinstance(item, dict):
                                continue
                            item_type = item.get("type")
                            if item_type == "tool_result":
                                text = _claude_format_tool_result(item, tool_use_result=tool_use_result)
                                add_message(ts_ms, "tool", "tool_result", text, count_for_stats=False,
                                            tool_summary=_claude_summarize_tool_result(item, tool_use_result=tool_use_result),
                                            tool_call_id=item.get('tool_use_id'), block_index=block_index)
                                continue
                            text = _claude_extract_text_item(item)
                            if text:
                                add_message(ts_ms, "user", "message", text.strip(), count_for_stats=True)
                            else:
                                add_raw_message(messages, ts_ms, "other", item, reason=f"claude_user_item:{item_type or 'unknown'}")
                    continue

                if role == "assistant":
                    _accumulate_claude_usage(usage_by_id, usage_unkeyed, msg)
                    if isinstance(content, str):
                        add_message(ts_ms, "assistant", "message", content.strip(), count_for_stats=True)
                    elif isinstance(content, list):
                        for block_index, item in enumerate(content):
                            if not isinstance(item, dict):
                                continue
                            item_type = item.get("type")
                            if item_type == "text":
                                text = item.get("text")
                                if isinstance(text, str) and text.strip():
                                    add_message(ts_ms, "assistant", "message", text.strip(), count_for_stats=True, block_index=block_index)
                                continue
                            if item_type == "thinking":
                                thinking = item.get("thinking")
                                if isinstance(thinking, str) and thinking.strip():
                                    # Hide by default via the "other" role filter.
                                    add_message(ts_ms, "other", "thinking", thinking.strip(), count_for_stats=False)
                                continue
                            if item_type == "tool_use":
                                text = _claude_format_tool_use(item)
                                add_message(ts_ms, "tool", "tool_use", text, count_for_stats=False,
                                            tool_summary=_claude_summarize_tool_use(item), tool_call_id=item.get('id'),
                                            trace_markers=skill_tool_markers(item.get('name'), item.get('input')),
                                            block_index=block_index)
                                continue

                            text = _claude_extract_text_item(item)
                            if text:
                                add_message(ts_ms, "assistant", "message", text.strip(), count_for_stats=True)
                            else:
                                add_raw_message(messages, ts_ms, "other", item, reason=f"claude_assistant_item:{item_type or 'unknown'}")
                    continue
                add_raw_message(messages, ts_ms, role or "other", obj, reason=f"claude_role:{role or 'unknown'}")
    except FileNotFoundError:
        return None

    if valid_json_count == 0:
        raise ValueError("invalid_session_file: %s" % path)

    if not session_id:
        session_id = f"file-{path.stem}"

    if start_ts_ms is None:
        start_ts_ms = end_ts_ms or 0
    if end_ts_ms is None:
        end_ts_ms = start_ts_ms

    if not title:
        title = f"Session {session_id[:8]}"

    search_blob = "\n".join(search_parts)

    return {
        "id": session_id,
        "file_path": str(path),
        "start_ts_ms": int(start_ts_ms),
        "end_ts_ms": int(end_ts_ms),
        "cwd": cwd,
        "title": title,
        "message_count": message_count,
        "messages": messages,
        "search_blob": search_blob,
        "usage": _finalize_claude_usage(usage_by_id, usage_unkeyed),
    }


def _openclaw_format_tool_use(item):
    if not isinstance(item, dict):
        return None
    name = item.get("name") or "tool"
    call_id = item.get("id")
    arguments = item.get("arguments")
    if arguments is None:
        raw_input = None
    elif isinstance(arguments, str):
        raw_input = arguments
    else:
        raw_input = json.dumps(arguments, ensure_ascii=False)
    return _codex_format_tool_use(name, call_id=call_id, raw_input=raw_input)


def _openclaw_format_tool_result(message):
    if not isinstance(message, dict):
        return None

    tool_name = message.get("toolName")
    tool_call_id = message.get("toolCallId")
    is_error = message.get("isError")
    details = message.get("details") if isinstance(message.get("details"), dict) else None
    content = message.get("content")

    header = f"Tool result: {tool_name}" if tool_name else "Tool result:"
    lines = [header]
    if tool_call_id:
        lines.append(f"Tool call ID: {tool_call_id}")

    status = None
    exit_code = None
    duration_ms = None
    if is_error is True:
        status = "error"
    elif is_error is False:
        status = "ok"

    if details:
        detail_status = str(details.get("status") or "").strip().lower()
        if detail_status in ("completed", "ok", "success") and status is None:
            status = "ok"
        elif detail_status in ("error", "failed", "failure") and status is None:
            status = "error"
        if isinstance(details.get("exitCode"), (int, float)):
            exit_code = int(details["exitCode"])
        if isinstance(details.get("durationMs"), (int, float)):
            duration_ms = int(details["durationMs"])

    if status:
        lines.append(f"Status: {status}")
    if exit_code is not None:
        lines.append(f"Exit code: {exit_code}")
    if duration_ms is not None:
        lines.append(f"Wall time: {duration_ms}ms")

    text = ""
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        text = extract_text(content).strip()

    if text:
        lines.append("Output:")
        lines.append(f"````\n{text}\n````")
    elif details:
        lines.append("Output:")
        lines.append(f"````\n{json.dumps(details, ensure_ascii=False, indent=2)}\n````")

    return "\n".join(lines).strip()


def _openclaw_summarize_tool_use(item):
    if not isinstance(item, dict):
        return _empty_tool_summary("tool")
    name = item.get("name") or "tool"
    arguments = item.get("arguments")
    if arguments is None:
        raw_input = None
    elif isinstance(arguments, str):
        raw_input = arguments
    else:
        raw_input = json.dumps(arguments, ensure_ascii=False)
    return _codex_summarize_tool_use(name, raw_input)


def _openclaw_summarize_tool_result(message):
    s = _empty_tool_summary("tool")
    if not isinstance(message, dict):
        return s
    tool_name = message.get("toolName")
    if isinstance(tool_name, str) and tool_name.strip():
        s["name"] = tool_name.strip()[:80]
        s["category"] = _classify_tool_category(tool_name)
    is_error = message.get("isError")
    details = message.get("details") if isinstance(message.get("details"), dict) else None
    content = message.get("content")
    status = None
    exit_code = None
    if is_error is True:
        status = "error"
    elif is_error is False:
        status = "ok"
    if details:
        detail_status = str(details.get("status") or "").strip().lower()
        if detail_status in ("completed", "ok", "success") and status is None:
            status = "ok"
        elif detail_status in ("error", "failed", "failure") and status is None:
            status = "error"
        if isinstance(details.get("exitCode"), (int, float)):
            exit_code = int(details["exitCode"])
    if exit_code is not None:
        s["exit_code"] = exit_code
        if status is None:
            status = "ok" if exit_code == 0 else "error"
    if status == "error":
        s["exit_status"] = "error"
        s["is_error"] = True
    elif status == "ok":
        s["exit_status"] = "ok"
        s["is_error"] = False
    body = None
    if isinstance(content, str):
        body = content.strip()
    elif isinstance(content, list):
        body = extract_text(content).strip()
    elif details:
        body = json.dumps(details, ensure_ascii=False)
    if body:
        s["output_preview"] = _truncate_str(body, 200)
    return s


def parse_openclaw_session_file(path: Path):
    session_id = None
    start_ts_ms = None
    end_ts_ms = None
    cwd = None
    title = None
    message_count = 0
    messages = []
    search_parts = []
    search_len = 0

    def add_search(text):
        nonlocal search_len
        if not text:
            return
        if search_len >= MAX_SEARCH_CHARS:
            return
        remaining = MAX_SEARCH_CHARS - search_len
        if len(text) > remaining:
            text = text[:remaining]
        search_parts.append(text)
        search_len += len(text)

    def add_message(ts_ms, role, kind, text, count_for_stats=False, tool_summary=None):
        nonlocal message_count, title
        if not text:
            return
        cleaned = text.strip()
        if not cleaned:
            return
        msg = {
            "ts_ms": ts_ms,
            "role": role,
            "kind": kind,
            "text": cleaned,
        }
        if isinstance(tool_summary, dict):
            msg["tool_summary"] = tool_summary
        messages.append(msg)
        add_search(cleaned)
        if count_for_stats:
            message_count += 1
            if title is None and role == "user":
                first_line = cleaned.splitlines()[0] if cleaned else ""
                if first_line:
                    title = first_line[:80]

    valid_json_count = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    valid_json_count += 1
                except Exception:
                    add_message(start_ts_ms or end_ts_ms or 0, "other", "raw_json:malformed_line", f"```\n{line.rstrip()}\n```", count_for_stats=False)
                    continue

                if not isinstance(obj, dict):
                    add_raw_message(messages, start_ts_ms or end_ts_ms or 0, "other", obj, reason="openclaw_non_object")
                    continue

                ts_ms = parse_ts(obj.get("timestamp"))
                if ts_ms is not None:
                    if start_ts_ms is None or ts_ms < start_ts_ms:
                        start_ts_ms = ts_ms
                    if end_ts_ms is None or ts_ms > end_ts_ms:
                        end_ts_ms = ts_ms

                obj_type = obj.get("type")
                if obj_type == "session":
                    session_id = obj.get("id") or session_id
                    if isinstance(obj.get("cwd"), str) and obj.get("cwd"):
                        cwd = obj["cwd"]
                    continue

                if obj_type != "message":
                    add_raw_message(messages, ts_ms, "other", obj, reason=f"openclaw_type:{obj_type or 'unknown'}")
                    continue

                message = obj.get("message")
                if not isinstance(message, dict):
                    add_raw_message(messages, ts_ms, "other", obj, reason="openclaw_missing_message")
                    continue

                role = message.get("role") or "unknown"
                content = message.get("content")

                if role == "user":
                    if isinstance(content, str):
                        text = content.strip()
                        if text.startswith("A new session was started via /new or /reset."):
                            add_message(ts_ms, "system", "context", text, count_for_stats=False)
                        else:
                            add_message(ts_ms, "user", "message", text, count_for_stats=True)
                    elif isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict):
                                continue
                            text = _claude_extract_text_item(item)
                            if not text:
                                add_raw_message(messages, ts_ms, "other", item, reason=f"openclaw_user_item:{item.get('type') or 'unknown'}")
                                continue
                            if text.startswith("A new session was started via /new or /reset."):
                                add_message(ts_ms, "system", "context", text, count_for_stats=False)
                            else:
                                add_message(ts_ms, "user", "message", text, count_for_stats=True)
                    continue

                if role == "assistant":
                    error_message = message.get("errorMessage")
                    if isinstance(error_message, str) and error_message.strip():
                        add_message(ts_ms, "assistant", "message", f"[error] {error_message.strip()}", count_for_stats=False)
                    if isinstance(content, str):
                        add_message(ts_ms, "assistant", "message", content, count_for_stats=True)
                    elif isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict):
                                continue
                            item_type = item.get("type")
                            if item_type == "text":
                                add_message(ts_ms, "assistant", "message", item.get("text"), count_for_stats=True)
                                continue
                            if item_type == "thinking":
                                add_message(ts_ms, "other", "thinking", item.get("thinking"), count_for_stats=False)
                                continue
                            if item_type in ("toolCall", "tool_use"):
                                add_message(ts_ms, "tool", "tool_use", _openclaw_format_tool_use(item), count_for_stats=False, tool_summary=_openclaw_summarize_tool_use(item))
                                continue
                            text = _claude_extract_text_item(item)
                            if text:
                                add_message(ts_ms, "assistant", "message", text, count_for_stats=True)
                            else:
                                add_raw_message(messages, ts_ms, "other", item, reason=f"openclaw_assistant_item:{item_type or 'unknown'}")
                    continue

                if role in ("toolResult", "tool_result", "tool"):
                    add_message(ts_ms, "tool", "tool_result", _openclaw_format_tool_result(message), count_for_stats=False, tool_summary=_openclaw_summarize_tool_result(message))
                    continue

                if isinstance(content, str):
                    add_message(ts_ms, role, "message", content, count_for_stats=False)
                elif isinstance(content, list):
                    text = extract_text(content)
                    if text:
                        add_message(ts_ms, role, "message", text, count_for_stats=False)
                    else:
                        add_raw_message(messages, ts_ms, role, message, reason=f"openclaw_role:{role or 'unknown'}")
                else:
                    add_raw_message(messages, ts_ms, role, message, reason=f"openclaw_role:{role or 'unknown'}")
    except FileNotFoundError:
        return None

    if valid_json_count == 0:
        raise ValueError("invalid_session_file: %s" % path)

    if not session_id:
        session_id = f"file-{path.stem}"

    if start_ts_ms is None:
        start_ts_ms = end_ts_ms or 0
    if end_ts_ms is None:
        end_ts_ms = start_ts_ms

    if not title:
        title = f"Session {session_id[:8]}"

    search_blob = "\n".join(search_parts)

    return {
        "id": session_id,
        "file_path": str(path),
        "start_ts_ms": int(start_ts_ms),
        "end_ts_ms": int(end_ts_ms),
        "cwd": cwd,
        "title": title,
        "message_count": message_count,
        "messages": messages,
        "search_blob": search_blob,
        "usage": None,
    }


class Indexer:
    def __init__(
        self,
        sessions_dir: Path,
        data_dir: Path,
        source: str,
        db_filename: str = "index.sqlite",
        scan_interval: int = 5,
        parse_file_fn=None,
        file_filter_fn=None,
        parser_version: int = 1,
        recall_db_path: Path | None = None,
    ):
        self.sessions_dir = sessions_dir
        self.source = str(source or "").strip()
        self.db_path = data_dir / db_filename
        self._parse_file_fn = parse_file_fn or parse_codex_session_file
        self._file_filter_fn = file_filter_fn or (lambda p: True)
        self.parser_version = int(parser_version)
        self.recall_titles = RecallTitleStore(recall_db_path, self.source) if recall_db_path else None
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.last_scan = 0
        self.scan_interval = max(1, int(scan_interval))
        self._local_title_backfill_done = False
        self._session_preview_cache = OrderedDict()
        self._init_db()

    def _clear_session_preview_cache(self, session_id=None):
        if session_id is None:
            self._session_preview_cache.clear()
            return
        self._session_preview_cache.pop(str(session_id), None)

    def _remember_session_preview_cache(self, session_id, payload):
        key = str(session_id or "")
        if not key:
            return
        self._session_preview_cache[key] = payload
        self._session_preview_cache.move_to_end(key)
        while len(self._session_preview_cache) > SESSION_PREVIEW_CACHE_SIZE:
            self._session_preview_cache.popitem(last=False)

    def _init_db(self):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    file_path TEXT UNIQUE,
                    start_ts_ms INTEGER,
                    end_ts_ms INTEGER,
                    cwd TEXT,
                    title TEXT,
                    message_count INTEGER,
                    mtime REAL,
                    search_blob TEXT,
                    parser_version INTEGER
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    ts_ms INTEGER,
                    role TEXT,
                    kind TEXT,
                    text TEXT
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_ts ON messages(session_id, ts_ms)")
            # Scope queries before visiting body-bearing rows. This small index
            # also covers public-kind checks; it never duplicates message TEXT.
            cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_search_scope ON messages(session_id, role, kind)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_start_ts ON sessions(start_ts_ms)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_end_ts ON sessions(end_ts_ms)")
            try:
                cur.execute("ALTER TABLE sessions ADD COLUMN parser_version INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                cur.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                cur.execute("ALTER TABLE sessions ADD COLUMN file_signature TEXT")
            except sqlite3.OperationalError:
                pass
            # Match the paged reader's pinned predicate and timestamp/id ordering.
            # In particular, an empty pinned set must not scan every search blob.
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_pinned_last ON sessions(COALESCE(pinned,0), end_ts_ms DESC, start_ts_ms DESC, id ASC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project_last ON sessions(cwd, COALESCE(pinned,0), end_ts_ms DESC, start_ts_ms DESC, id ASC)")
            cur.execute("CREATE TABLE IF NOT EXISTS reader_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            cur.execute("INSERT OR IGNORE INTO reader_state VALUES ('revision', lower(hex(randomblob(16))))")
            patch_db_for_audit(self.conn)
            try:
                cur.execute("ALTER TABLE messages ADD COLUMN tool_summary_json TEXT")
            except sqlite3.OperationalError:
                pass
            for column in ("tokens_input", "tokens_output", "tokens_cached", "tokens_reasoning", "tokens_total"):
                try:
                    cur.execute(f"ALTER TABLE sessions ADD COLUMN {column} INTEGER DEFAULT 0")
                except sqlite3.OperationalError:
                    pass
            session_columns = {row[1] for row in cur.execute("PRAGMA table_info(sessions)")}
            if "activity_metadata_json" not in session_columns:
                cur.execute("ALTER TABLE sessions ADD COLUMN activity_metadata_json TEXT")
            message_columns = {row[1] for row in cur.execute("PRAGMA table_info(messages)")}
            for name, kind in (("activity_ts_ms", "INTEGER"), ("activity_meta_json", "TEXT")):
                if name not in message_columns:
                    cur.execute(f"ALTER TABLE messages ADD COLUMN {name} {kind}")
                    # Existing caches cannot attest whether timestamps were inferred.
                    cur.execute("UPDATE sessions SET file_signature = NULL")
            # Metadata-only migration: old caches use the ID-only ordinal
            # fallback until a recording is naturally reparsed. Do not invalidate
            # signatures or rebuild the existing timestamp/rowid covering index.
            if "message_index" not in message_columns:
                cur.execute("ALTER TABLE messages ADD COLUMN message_index INTEGER")
            if "message_index_count" not in session_columns:
                cur.execute("ALTER TABLE sessions ADD COLUMN message_index_count INTEGER")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_activity ON messages(activity_ts_ms, session_id)")
            self.conn.commit()

    def maybe_update_index(self, max_age_seconds=None):
        if max_age_seconds is None:
            max_age_seconds = self.scan_interval
        now = time.time()
        if now - self.last_scan < max_age_seconds:
            self.backfill_local_title_overrides()
            return
        self.scan_sessions()
        self.last_scan = now
        self.backfill_local_title_overrides()

    def _recording_alias(self, native_id, path):
        import hashlib
        suffix = hashlib.sha256(str(path.relative_to(self.sessions_dir)).encode()).hexdigest()[:16]
        return native_id + '@recording-' + suffix

    def _store_parsed_session(self, row, session, mtime, signature, path, session_files, *,
                              build_audits=True, preserve_recordings=False, content=None):
        collision = self.conn.execute("SELECT file_path FROM sessions WHERE id=?", (session['id'],)).fetchone()
        conflict = collision and collision[0] != str(path) and Path(collision[0]) in session_files
        if conflict or (row and row['id'] != session['id']):
            # A copied/forked recording must not erase another recording's
            # messages, regardless of the caller's preserve_recordings flag.
            # Reuse an existing recording alias even if its native-ID owner
            # has disappeared, so edits do not change this recording's key.
            native_id = session['id']
            recording_id = self._recording_alias(native_id, path)
            if conflict or (row and row['id'] == recording_id):
                session['id'] = recording_id
                alias_owner = self.conn.execute('SELECT file_path FROM sessions WHERE id=?', (session['id'],)).fetchone()
                if alias_owner and alias_owner[0] != str(path):
                    raise ValueError('duplicate_session_identity')
                session.setdefault('activity_metadata', {}).update(
                    identity_conflict=True, native_session_id=native_id,
                    identity_policy='distinct recording; no automatic work deduplication')
        if row and row["id"] != session["id"]:
            self.conn.execute("DELETE FROM messages WHERE session_id = ?", (row["id"],))
            self.conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))

        self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session["id"],))
        pinned = int(row["pinned"] or 0) if row else 0
        usage = session.get("usage") or {}

        def _usage_int(key):
            value = usage.get(key)
            return int(value) if isinstance(value, (int, float)) else 0

        self.conn.execute(
            """
            INSERT OR REPLACE INTO sessions
            (id, file_path, start_ts_ms, end_ts_ms, cwd, title, message_count, mtime, search_blob, parser_version, pinned,
             tokens_input, tokens_output, tokens_cached, tokens_reasoning, tokens_total, file_signature)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["id"],
                session["file_path"],
                session["start_ts_ms"],
                session["end_ts_ms"],
                session["cwd"],
                session["title"],
                session["message_count"],
                mtime,
                session["search_blob"],
                self.parser_version,
                pinned,
                _usage_int("input"),
                _usage_int("output"),
                _usage_int("cached"),
                _usage_int("reasoning"),
                _usage_int("total"),
                signature,
            ),
        )

        messages = session["messages"]
        metadata = dict(session.get('activity_metadata', {}))
        metadata['activity_index_counts'] = dict(
            messages_without_verified_time=sum(
                m['role'] in ('user', 'assistant', 'tool')
                and not str(m['kind']).startswith('context')
                and (not m.get('ts_ms') or str(m['kind']).startswith('raw_json'))
                for m in messages),
            unparsed_indexed_records=sum(str(m['kind']).startswith('raw_json:') for m in messages))
        self.conn.execute("UPDATE sessions SET activity_metadata_json=?, message_index_count=? WHERE id=?",
            (json.dumps(metadata), len(messages), session['id']))
        if messages:
            slots = message_indexes(messages, session["start_ts_ms"])
            self.conn.executemany(
                """
                INSERT INTO messages (session_id, ts_ms, role, kind, text, tool_summary_json, activity_ts_ms, activity_meta_json, message_index)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        session["id"],
                        int(m["ts_ms"] or session["start_ts_ms"]),
                        m["role"],
                        m["kind"],
                        m["text"],
                        json.dumps(m["tool_summary"]) if isinstance(m.get("tool_summary"), dict) else None,
                        m.get("ts_ms") if m.get("ts_ms") and not str(m.get("kind", "")).startswith("raw_json") else None,
                        json.dumps({key: m[key] for key in ("raw_ref", "tool_call_id", "turn_id", "parent_id", "tool_result_error", "trace_markers", "trace_context") if key in m}),
                        slot,
                    )
                    for m, slot in zip(messages, slots)
                ],
            )

        audit_payload = None
        if build_audits:
            if self.source == 'mcode':
                from .mcode_review import build_mcode_audit
                audit_payload = build_mcode_audit(session)
            elif content is None:
                audit_payload = build_audit_for_file(Path(session["file_path"]), self.source, session_id_hint=session["id"])
            else:
                audit_payload = build_audit_for_file(Path(session["file_path"]), self.source,
                    session_id_hint=session["id"], content=content)
        if audit_payload is None and self.source == 'mcode':
            self.conn.execute("UPDATE sessions SET audit_version=?, audit_status='unsupported' WHERE id=?", (AUDIT_VERSION, session['id']))
        if audit_payload is not None:
            fields = serialize_audit_fields(audit_payload)
            fields["audit_updated_at"] = int(time.time() * 1000)
            self.conn.execute(
                """
                UPDATE sessions
                   SET files_touched_json = ?,
                       tool_summary_json = ?,
                       command_intents_json = ?,
                       remote_context_json = ?,
                       outcome_signal = ?,
                       value_score = ?,
                       friction_score = ?,
                       action_density = ?,
                       audit_status = ?,
                       audit_updated_at = ?,
                       audit_version = ?
                 WHERE id = ?
                """,
                (
                    fields["files_touched_json"],
                    fields["tool_summary_json"],
                    fields["command_intents_json"],
                    fields["remote_context_json"],
                    fields["outcome_signal"],
                    fields["value_score"],
                    fields["friction_score"],
                    fields["action_density"],
                    fields["audit_status"],
                    fields["audit_updated_at"],
                    fields["audit_version"],
                    session["id"],
                ),
            )


    def scan_sessions(self, *, skipped_paths=(), build_audits=True, preserve_recordings=False):
        # Enumeration must succeed before a missing path can authorize cache removal.
        root_stat = self.sessions_dir.stat()
        if not self.sessions_dir.is_dir():
            raise ValueError('sessions_directory_required')
        def fail(error):
            raise error
        session_files = []
        for directory, dirs, files in os.walk(self.sessions_dir, onerror=fail):
            for name in files:
                path = Path(directory) / name
                if path.suffix in file_suffixes(self.source) and self._file_filter_fn(path):
                    session_files.append(path)

        def fingerprint(path):
            st = path.stat()
            if self.source == 'mcode':
                manifest = path.parent / 'manifest.json'
                if manifest.is_symlink():
                    raise ValueError('source_symlink_not_allowed')
                ms = manifest.stat()
                from .mcode import mcode_file_signature
                return mcode_file_signature(st, ms), st.st_mtime
            return json.dumps([st.st_mtime_ns, st.st_ctime_ns, st.st_size,
                               st.st_dev, st.st_ino]), st.st_mtime

        with self.lock, self.conn:
            existing_rows = self.conn.execute(
                "SELECT id, file_path, mtime, parser_version, title, pinned, audit_version, file_signature FROM sessions"
            ).fetchall()
            existing_by_path = {str(row["file_path"]): row for row in existing_rows}

            def updates():
                for path in session_files:
                    if path in skipped_paths:
                        continue
                    signature, mtime = fingerprint(path)
                    row = existing_by_path.get(str(path))
                    if (row and row["file_signature"] == signature
                            and row["parser_version"] == self.parser_version
                            and (not build_audits or row["audit_version"] == AUDIT_VERSION)):
                        continue
                    session = self._parse_file_fn(path)
                    if session is None:
                        raise ValueError("invalid_session_file: %s" % path)
                    if self.recall_titles:
                        custom_title = self.recall_titles.get_custom_title(session["id"])
                        if self.source == 'codex' and is_ambient_title(custom_title):
                            custom_title = None
                        if (
                            not custom_title
                            and row
                            and row["title"]
                            and str(row["title"]).strip()
                            and (self.source != 'codex' or not is_ambient_title(row["title"]))
                            and str(row["title"]).strip() != str(session["title"]).strip()
                        ):
                            custom_title = str(row["title"]).strip()
                            self.recall_titles.set_custom_title(session["id"], custom_title)
                        if custom_title:
                            session["title"] = custom_title

                    yield row, session, mtime, signature, path

            self._clear_session_preview_cache()
            changed = False
            for row, session, mtime, signature, path in updates():
                changed = True
                self._store_parsed_session(row, session, mtime, signature, path, session_files,
                    build_audits=build_audits, preserve_recordings=preserve_recordings)

                if fingerprint(path)[0] != signature:
                    error = ValueError("source_changed_during_refresh: %s" % path)
                    error.changed_path = path
                    raise error

            final_root_stat = self.sessions_dir.stat()
            if (root_stat.st_dev, root_stat.st_ino) != (final_root_stat.st_dev, final_root_stat.st_ino):
                raise ValueError('source_changed_during_refresh')
            present = {str(path) for path in session_files}
            for old_path in existing_by_path.keys() - present:
                changed = True
                self.conn.execute("DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE file_path = ?)", (old_path,))
                self.conn.execute("DELETE FROM sessions WHERE file_path = ?", (old_path,))

            if changed:
                self.conn.execute("UPDATE reader_state SET value = lower(hex(randomblob(16))) WHERE key = 'revision'")

    def refresh_capture(self, paths=None, *, max_bytes=128 * 1024 * 1024):
        """Capture selected Codex recordings without pruning unselected rows.

        Each file has an independent savepoint. A single outer transaction
        publishes the successful captures and one new reader revision.
        """
        from .capture import JsonlCapture, stat_signature
        if self.source != 'codex':
            raise ValueError('capture_source_unsupported')
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError('invalid_capture_budget')
        root = Path(os.path.abspath(self.sessions_dir))
        root_identity = root.stat()
        if root.is_symlink() or not root.is_dir():
            raise ValueError('capture_source_directory_required')
        all_files = paths is None
        if all_files:
            selected = []
            def fail(error):
                raise error
            for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
                if any((Path(directory) / name).is_symlink() for name in dirs):
                    raise ValueError('source_symlink_not_allowed')
                selected.extend(Path(directory) / name for name in files
                    if (Path(directory) / name).suffix in file_suffixes(self.source)
                    and self._file_filter_fn(Path(directory) / name))
        else:
            if isinstance(paths, (str, bytes, Path)):
                raise ValueError('capture_paths_list_required')
            selected = []
            for value in paths:
                path = Path(value)
                candidate = Path(os.path.abspath(path if path.is_absolute() else root / path))
                try:
                    candidate.relative_to(root)
                except ValueError:
                    raise ValueError('capture_path_outside_source') from None
                if candidate.suffix not in file_suffixes(self.source) or not self._file_filter_fn(candidate):
                    raise ValueError('capture_selection_not_recording')
                selected.append(candidate)
        selected = list(dict.fromkeys(selected))
        receipt = dict(source='codex', mode='capture', selected=len(selected), updated=0,
                       failed=0, deleted=0, partial=False, sessions=[], max_bytes_per_session=max_bytes)
        with self.lock, self.conn:
            rows = self.conn.execute('SELECT * FROM sessions').fetchall()
            existing = {str(row['file_path']): row for row in rows}
            # Include unselected recordings in collision protection. Targeted
            # refresh must not replace another recording sharing a native ID.
            collision_paths = {Path(path) for path in existing}
            collision_paths.update(self.sessions_dir / path.relative_to(root) for path in selected)
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN')
            self._clear_session_preview_cache()
            for absolute in selected:
                relative = absolute.relative_to(root)
                path = self.sessions_dir / relative
                item = {'path': str(relative)}
                savepoint = False
                try:
                    ancestor = root
                    for component in relative.parts:
                        ancestor /= component
                        if ancestor.is_symlink():
                            raise ValueError('source_symlink_not_allowed')
                    row = existing.get(str(path))
                    st = path.stat()
                    if (all_files and row and row['file_signature'] == stat_signature(st)
                            and row['parser_version'] == self.parser_version
                            and row['audit_version'] == AUDIT_VERSION):
                        item.update(status='unchanged', session_id=row['id'])
                        previous = json.loads(row['activity_metadata_json'] or '{}').get('source_capture')
                        if isinstance(previous, dict):
                            item['source_capture'] = previous
                            receipt['partial'] |= bool(previous.get('partial'))
                        receipt['sessions'].append(item)
                        continue
                    with JsonlCapture(path, max_bytes=max_bytes) as capture:
                        session = self._parse_file_fn(path, content=capture.content)
                        if session is None:
                            raise ValueError('invalid_session_file')
                        if self.recall_titles:
                            title = self.recall_titles.get_custom_title(session['id'])
                            if is_ambient_title(title):
                                title = None
                            if not title and row and row['title'] and not is_ambient_title(row['title']) and row['title'] != session['title']:
                                title = row['title']
                            if title:
                                session['title'] = title
                        self.conn.execute('SAVEPOINT source_capture')
                        savepoint = True
                        self._store_parsed_session(row, session, capture.initial.st_mtime,
                            stat_signature(capture.initial), path, collision_paths,
                            preserve_recordings=True, content=capture.content)
                        metadata = capture.verify()
                        session.setdefault('activity_metadata', {})['source_capture'] = metadata
                        self.conn.execute('UPDATE sessions SET activity_metadata_json=?, file_signature=? WHERE id=?',
                            (json.dumps(session['activity_metadata']), capture.signature, session['id']))
                        self.conn.execute('RELEASE source_capture')
                        savepoint = False
                    item.update(status='captured', session_id=session['id'], source_capture=metadata)
                    receipt['updated'] += 1
                    receipt['partial'] |= metadata['partial']
                except Exception as exc:
                    if savepoint:
                        self.conn.execute('ROLLBACK TO source_capture')
                        self.conn.execute('RELEASE source_capture')
                    item.update(status='failed', error=str(exc)[:300])
                    receipt['failed'] += 1
                    receipt['partial'] = True
                receipt['sessions'].append(item)
            final_root = root.stat()
            if (final_root.st_dev, final_root.st_ino) != (root_identity.st_dev, root_identity.st_ino):
                raise ValueError('capture_source_root_changed')
            if all_files and not receipt['partial']:
                selected_keys = {str(self.sessions_dir / path.relative_to(root)) for path in selected}
                missing = []
                for old_path in existing.keys() - selected_keys:
                    try:
                        Path(old_path).lstat()
                    except FileNotFoundError:
                        missing.append(old_path)
                    except OSError as exc:
                        receipt['partial'] = True
                        receipt['prune_error'] = str(exc)[:300]
                        break
                if not receipt['partial']:
                    for old_path in missing:
                        self.conn.execute('DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE file_path=?)', (old_path,))
                        self.conn.execute('DELETE FROM sessions WHERE file_path=?', (old_path,))
                    receipt['deleted'] = len(missing)
            if receipt['updated'] or receipt['deleted']:
                self.conn.execute("UPDATE reader_state SET value=lower(hex(randomblob(16))) WHERE key='revision'")
            receipt['index_revision'] = self.conn.execute("SELECT value FROM reader_state WHERE key='revision'").fetchone()[0]
        return receipt

    def backfill_local_title_overrides(self):
        if self._local_title_backfill_done or not self.recall_titles:
            return

        with self.lock:
            rows = self.conn.execute(
                "SELECT id, file_path, title FROM sessions WHERE title IS NOT NULL AND title <> ''"
            ).fetchall()

        for row in rows:
            session_id = row["id"]
            file_path = row["file_path"]
            stored_title = str(row["title"] or "").strip()
            if not session_id or not file_path or not stored_title:
                continue
            if self.source == 'codex' and is_ambient_title(stored_title):
                continue
            if self.recall_titles.get_custom_title(session_id):
                continue
            try:
                parsed = self._parse_file_fn(Path(file_path))
            except Exception:
                continue
            if not parsed:
                continue
            parsed_title = str(parsed.get("title") or "").strip()
            if parsed_title and parsed_title != stored_title:
                self.recall_titles.set_custom_title(session_id, stored_title)

        self._local_title_backfill_done = True

    def query_sessions(self, q=None, start_ms=None, end_ms=None, limit=DEFAULT_LIMIT, cwd=None, sort=None, file_path=None):
        terms = []
        if q:
            terms = [t for t in q.split() if t]

        sort_key = str(sort or "start").strip().lower()
        if sort_key == "value":
            order_clause = " ORDER BY COALESCE(pinned,0) DESC, value_score DESC, end_ts_ms DESC"
        elif sort_key == "friction":
            order_clause = " ORDER BY COALESCE(pinned,0) DESC, friction_score DESC, end_ts_ms DESC"
        elif sort_key in ("last", "end", "updated", "update"):
            order_clause = " ORDER BY COALESCE(pinned,0) DESC, end_ts_ms DESC, start_ts_ms DESC"
        else:
            order_clause = " ORDER BY COALESCE(pinned,0) DESC, start_ts_ms DESC, end_ts_ms DESC"

        sql = (
            "SELECT id, start_ts_ms, end_ts_ms, title, message_count, cwd, pinned, "
            "files_touched_json, tool_summary_json, command_intents_json, remote_context_json, "
            "outcome_signal, value_score, friction_score, action_density, tokens_total "
            "FROM sessions WHERE 1=1"
        )
        args = []
        if start_ms is not None:
            sql += " AND start_ts_ms >= ?"
            args.append(int(start_ms))
        if end_ms is not None:
            sql += " AND start_ts_ms <= ?"
            args.append(int(end_ms))
        if cwd:
            sql += " AND cwd = ?"
            args.append(cwd)
        if file_path:
            sql += " AND files_touched_json LIKE ? ESCAPE '\\'"
            args.append("%" + _escape_sql_like(file_path) + "%")
        for term in terms:
            sql += " AND " + _indexed_session_match_sql()
            like = "%" + _escape_sql_like(term) + "%"
            args.extend([like] * 3)
        sql += order_clause + " LIMIT ?"
        args.append(int(limit))

        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        items = [dict(row) for row in rows]
        if file_path:
            items = [it for it in items if _match_files_touched(it.get("files_touched_json"), file_path)]
        for item in items:
            item.update(deserialize_audit_summary(item))
            _strip_audit_raw_json(item)
        return items

    def _list_tool_evidence_page(self, q, start_ms, end_ms, limit, offset, cwd, file_path,
                                 session_id=None, role=None):
        """Rank actual tool wording before SQL pagination; never infer success."""
        from .reuse import _terms, _path_match, query_budget
        query, terms = _terms(q)
        clean_limit, clean_offset = normalize_page_args(limit, offset)
        where, args = ['1=1'], []
        if session_id is not None:
            where.append('s.id=?')
            args.append(session_id)
        if role is not None:
            role_scope = ("m.session_id=s.id AND m.role=? "
                          f"AND {_indexed_public_message_sql('m')}")
            where.append(f'EXISTS(SELECT 1 FROM messages m WHERE {role_scope})')
            args.append(role)
        for column, operator, value in (('start_ts_ms', '>=', start_ms),
                                        ('start_ts_ms', '<=', end_ms), ('cwd', '=', cwd)):
            if value is not None:
                where.append(f's.{column} {operator} ?')
                args.append(value)
        if file_path:
            where.append("hv_reuse_file_match(s.files_touched_json,?,COALESCE(s.cwd,''))=1")
            args.append(file_path)
        for term in terms:
            like = '%' + _escape_sql_like(term) + '%'
            if role is not None:
                where.append(f"EXISTS(SELECT 1 FROM messages m WHERE {role_scope} AND m.text LIKE ? ESCAPE '\\')")
                args.extend([role, like])
            else:
                where.append(_indexed_session_match_sql('s'))
                args.extend([like] * 3)
        tool_scope = ("m.session_id=s.id AND m.role='tool' "
                      "AND m.kind IN ('tool_use','tool_result') "
                      f"AND {_indexed_public_message_sql('m')}")
        rank_args = []
        tool_terms = tool_phrase = title_phrase = '0'
        if terms and role is None:
            text = "COALESCE(m.text,'')"
            hits = [f'({_indexed_text_position_sql(text, term)}>0)' for term in terms]
            any_hit = f"EXISTS(SELECT 1 FROM messages m WHERE {tool_scope} AND ({' OR '.join(hits)}))"
            if len(terms) == 1:
                tool_terms = any_hit
            elif len(terms) == 2:
                all_hit = f"EXISTS(SELECT 1 FROM messages m WHERE {tool_scope} AND ({' AND '.join(hits)}))"
                # Exact 0/1/2 maximum over a single tool message. EXISTS can
                # stop early; a zero hit never needs a second body scan.
                tool_terms = f'CASE WHEN {any_hit} THEN CASE WHEN {all_hit} THEN 2 ELSE 1 END ELSE 0 END'
                rank_args.extend(terms)
            else:
                coverage = ' + '.join(hits)
                tool_terms = f'COALESCE((SELECT MAX({coverage}) FROM messages m WHERE {tool_scope}),0)'
            rank_args.extend(terms)
            tool_phrase = f"EXISTS(SELECT 1 FROM messages m WHERE {tool_scope} AND m.text LIKE ? ESCAPE '\\')"
            title_phrase = "(COALESCE(s.title,'') LIKE ? ESCAPE '\\')"
            rank_args.extend(['%' + _escape_sql_like(query) + '%'] * 2)
        fields = ('s.id,s.start_ts_ms,s.end_ts_ms,s.title,s.message_count,s.cwd,s.pinned,'
                  's.files_touched_json,s.tool_summary_json,s.command_intents_json,s.remote_context_json,'
                  's.outcome_signal,s.value_score,s.friction_score,s.action_density,s.tokens_total')
        sql = f'''WITH selected AS MATERIALIZED (
                    SELECT s.id,s.end_ts_ms,s.start_ts_ms,{tool_terms} AS tool_match_terms,
                           {tool_phrase} AS tool_phrase_match,{title_phrase} AS title_phrase_match
                    FROM sessions s WHERE {' AND '.join(where)}
                    ORDER BY tool_match_terms DESC,tool_phrase_match DESC,title_phrase_match DESC,
                             s.end_ts_ms DESC,s.start_ts_ms DESC,s.id ASC LIMIT ? OFFSET ?)
                  SELECT {fields},r.tool_match_terms,r.tool_phrase_match,r.title_phrase_match
                  FROM selected r JOIN sessions s ON s.id=r.id
                  ORDER BY r.tool_match_terms DESC,r.tool_phrase_match DESC,r.title_phrase_match DESC,
                           r.end_ts_ms DESC,r.start_ts_ms DESC,r.id ASC'''
        with query_budget(self) as conn:
            if file_path:
                conn.create_function('hv_reuse_file_match', 3, _path_match, deterministic=True)
            rows = conn.execute(sql, [*rank_args, *args, clean_limit + 1, clean_offset]).fetchall()
        items = []
        for row in rows[:clean_limit]:
            item = dict(row)
            item.update(deserialize_audit_summary(item))
            items.append(_strip_audit_raw_json(item))
        has_more = len(rows) > clean_limit
        return {'items': items, 'limit': clean_limit, 'offset': clean_offset,
                'has_more': has_more, 'next_offset': clean_offset + len(items) if has_more else None,
                'ranking': 'matching_role_then_time' if role else 'matching_tool_terms_then_phrase_then_title_and_time'}

    def list_sessions_page(self, q=None, start_ms=None, end_ms=None, limit=DEFAULT_PAGE_LIMIT, offset=0, cwd=None, sort=None, file_path=None, stable_order=False,
                           session_id=None, role=None):
        if str(sort or '').strip().lower() == 'tool_evidence':
            return self._list_tool_evidence_page(q, start_ms, end_ms, limit, offset, cwd, file_path,
                                                 session_id=session_id, role=role)
        clean_limit, clean_offset = normalize_page_args(limit, offset)
        terms = [t for t in str(q or "").split() if t]

        sort_key = str(sort or "start").strip().lower()
        if sort_key == "value":
            order_clause = " ORDER BY value_score DESC, end_ts_ms DESC"
        elif sort_key == "friction":
            order_clause = " ORDER BY friction_score DESC, end_ts_ms DESC"
        elif sort_key in ("last", "end", "updated", "update"):
            order_clause = " ORDER BY end_ts_ms DESC, start_ts_ms DESC"
        else:
            order_clause = " ORDER BY start_ts_ms DESC, end_ts_ms DESC"

        if stable_order:
            order_clause += ", id ASC"

        where_sql = " WHERE 1=1"
        args = []
        if start_ms is not None:
            where_sql += " AND start_ts_ms >= ?"
            args.append(int(start_ms))
        if end_ms is not None:
            where_sql += " AND start_ts_ms <= ?"
            args.append(int(end_ms))
        if cwd:
            where_sql += " AND cwd = ?"
            args.append(cwd)
        if file_path:
            where_sql += " AND files_touched_json LIKE ? ESCAPE '\\'"
            args.append("%" + _escape_sql_like(file_path) + "%")
        for term in terms:
            where_sql += " AND " + _indexed_session_match_sql()
            like = "%" + _escape_sql_like(term) + "%"
            args.extend([like] * 3)

        select_sql = (
            "SELECT id, start_ts_ms, end_ts_ms, title, message_count, cwd, pinned, "
            "files_touched_json, tool_summary_json, command_intents_json, remote_context_json, "
            "outcome_signal, value_score, friction_score, action_density, tokens_total FROM sessions"
        )
        pinned_rows = []
        with self.lock:
            if clean_offset == 0:
                pinned_rows = self.conn.execute(
                    f"{select_sql}{where_sql} AND COALESCE(pinned,0) = 1{order_clause}",
                    args,
                ).fetchall()

            rows = self.conn.execute(
                f"{select_sql}{where_sql} AND COALESCE(pinned,0) = 0{order_clause} LIMIT ? OFFSET ?",
                [*args, clean_limit + 1, clean_offset],
            ).fetchall()

        def _with_audit(row):
            item = dict(row)
            item.update(deserialize_audit_summary(item))
            return _strip_audit_raw_json(item)

        if file_path:
            matched_pinned = [r for r in pinned_rows if _match_files_touched(r["files_touched_json"], file_path)]
            matched_unpinned = [r for r in rows if _match_files_touched(r["files_touched_json"], file_path)]
            unpinned_items = [_with_audit(r) for r in matched_unpinned[:clean_limit]]
            items = [_with_audit(r) for r in matched_pinned] + unpinned_items
            has_more = len(matched_unpinned) > clean_limit
        else:
            unpinned_items = [_with_audit(row) for row in rows[:clean_limit]]
            items = [_with_audit(row) for row in pinned_rows] + unpinned_items
            has_more = len(rows) > clean_limit
        next_offset = clean_offset + len(unpinned_items) if has_more else None
        return {
            "items": items,
            "limit": clean_limit,
            "offset": clean_offset,
            "has_more": has_more,
            "next_offset": next_offset,
        }

    def query_projects(self, q=None, limit=DEFAULT_LIMIT):
        sql = (
            "SELECT cwd AS project, COUNT(*) AS session_count, MAX(start_ts_ms) AS last_ts_ms "
            "FROM sessions WHERE cwd IS NOT NULL AND cwd <> ''"
        )
        args = []
        if q:
            sql += " AND cwd LIKE ?"
            args.append(f"%{q}%")
        sql += " GROUP BY cwd ORDER BY last_ts_ms DESC LIMIT ?"
        args.append(int(limit))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def list_projects_page(self, q=None, limit=DEFAULT_PAGE_LIMIT, offset=0):
        clean_limit, clean_offset = normalize_page_args(limit, offset)
        sql = (
            "SELECT cwd AS project, COUNT(*) AS session_count, MAX(start_ts_ms) AS last_ts_ms "
            "FROM sessions WHERE cwd IS NOT NULL AND cwd <> ''"
        )
        args = []
        if q:
            sql += " AND cwd LIKE ?"
            args.append(f"%{q}%")
        sql += " GROUP BY cwd ORDER BY last_ts_ms DESC LIMIT ? OFFSET ?"
        args.extend([clean_limit + 1, clean_offset])
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()

        items = [dict(row) for row in rows[:clean_limit]]
        has_more = len(rows) > clean_limit
        next_offset = clean_offset + len(items) if has_more else None
        return {
            "items": items,
            "limit": clean_limit,
            "offset": clean_offset,
            "has_more": has_more,
            "next_offset": next_offset,
        }

    def query_usage(self, start_ms=None, end_ms=None, cwd=None):
        # Aggregated token usage for the Usage panel. Day buckets use the
        # viewer's local timezone to match the sidebar date dividers.
        where_sql = " WHERE 1=1"
        args = []
        if start_ms is not None:
            where_sql += " AND start_ts_ms >= ?"
            args.append(int(start_ms))
        if end_ms is not None:
            where_sql += " AND start_ts_ms <= ?"
            args.append(int(end_ms))
        if cwd:
            where_sql += " AND cwd = ?"
            args.append(cwd)

        def _sum_columns():
            return (
                "COUNT(*) AS session_count, "
                "COALESCE(SUM(tokens_input), 0) AS input, "
                "COALESCE(SUM(tokens_output), 0) AS output, "
                "COALESCE(SUM(tokens_cached), 0) AS cached, "
                "COALESCE(SUM(tokens_reasoning), 0) AS reasoning, "
                "COALESCE(SUM(tokens_total), 0) AS total"
            )

        def _fetch(sql, extra_args=None):
            with self.lock:
                rows = self.conn.execute(sql, [*args, *(extra_args or [])]).fetchall()
            return [dict(row) for row in rows]

        totals_rows = _fetch(f"SELECT {_sum_columns()} FROM sessions{where_sql}")
        totals = totals_rows[0] if totals_rows else {}
        by_day = _fetch(
            "SELECT date(start_ts_ms / 1000, 'unixepoch', 'localtime') AS day, "
            f"{_sum_columns()} FROM sessions{where_sql} "
            "GROUP BY day ORDER BY day DESC LIMIT 60"
        )
        by_project = _fetch(
            "SELECT COALESCE(NULLIF(cwd, ''), '(unknown)') AS project, "
            f"{_sum_columns()} FROM sessions{where_sql} "
            "GROUP BY project ORDER BY total DESC LIMIT 12"
        )
        top_sessions = _fetch(
            "SELECT id, title, cwd, start_ts_ms, tokens_input, tokens_output, "
            "tokens_cached, tokens_reasoning, tokens_total "
            f"FROM sessions{where_sql} ORDER BY tokens_total DESC LIMIT 10"
        )
        return {
            "totals": totals,
            "has_usage_data": int(totals.get("total") or 0) > 0,
            "by_day": by_day,
            "by_project": by_project,
            "top_sessions": top_sessions,
        }

    def _serialize_message_row(self, row, message_index, include_full_text=False):
        text, char_count, is_truncated = normalize_message_payload(
            row["text"],
            include_full_text=include_full_text,
            char_count=row["char_count"] if "char_count" in row.keys() else None,
            is_truncated=row["is_truncated"] if "is_truncated" in row.keys() else None,
        )
        tool_summary = None
        raw_ts = row["tool_summary_json"] if "tool_summary_json" in row.keys() else None
        if raw_ts:
            try:
                tool_summary = json.loads(raw_ts)
            except (ValueError, TypeError):
                tool_summary = None
        result = {
            "message_index": int(message_index),
            "ts_ms": row["ts_ms"],
            "role": row["role"],
            "kind": row["kind"],
            "text": text,
            "char_count": char_count,
            "is_truncated": bool(is_truncated),
        }
        if tool_summary is not None:
            result["tool_summary"] = tool_summary
        return result

    def get_session_metadata(self, session_id):
        with self.lock:
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(sessions)")}
            metadata_column = ", activity_metadata_json" if "activity_metadata_json" in columns else ""
            session = self.conn.execute(
                "SELECT id, start_ts_ms, end_ts_ms, title, message_count, cwd, pinned"
                + metadata_column + " FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                return None
            payload = dict(session)
            try:
                metadata = json.loads(payload.pop("activity_metadata_json", None) or "{}")
            except (ValueError, TypeError):
                metadata = {}
            if isinstance(metadata, dict):
                for field in ("identity_conflict", "native_session_id"):
                    if field in metadata:
                        payload[field] = metadata[field]
            total_row = self.conn.execute(
                "SELECT COUNT(*) AS total FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            payload["message_total"] = int(total_row["total"] or 0)
            return payload

    def get_session_messages_page(self, session_id, offset=0, limit=DEFAULT_LIMIT):
        clean_limit, clean_offset = normalize_page_args(limit, offset, default_limit=DEFAULT_LIMIT, max_limit=DEFAULT_LIMIT)
        with self.lock:
            session = self.conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not session:
                return None
            total_row = self.conn.execute(
                "SELECT COUNT(*) AS total FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            total = int(total_row["total"] or 0)
            messages = self.conn.execute(
                """
                SELECT
                    ts_ms,
                    role,
                    kind,
                    CASE
                        WHEN LENGTH(text) > ? THEN SUBSTR(text, 1, ?)
                        ELSE text
                    END AS text,
                    LENGTH(text) AS char_count,
                    CASE
                        WHEN LENGTH(text) > ? THEN 1
                        ELSE 0
                    END AS is_truncated,
                    tool_summary_json
                FROM messages
                WHERE session_id = ?
                ORDER BY ts_ms ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (
                    MESSAGE_INLINE_FULL_THRESHOLD,
                    MESSAGE_PREVIEW_FETCH_CHARS,
                    MESSAGE_INLINE_FULL_THRESHOLD,
                    session_id,
                    clean_limit,
                    clean_offset,
                ),
            ).fetchall()
        return {
            "messages": [
                self._serialize_message_row(row, clean_offset + idx)
                for idx, row in enumerate(messages)
            ],
            "offset": clean_offset,
            "limit": clean_limit,
            "total": total,
        }

    def get_session(self, session_id, include_messages=True):
        session = self.get_session_metadata(session_id)
        if not session:
            return None
        payload = {"session": session}
        if include_messages:
            cached = self._session_preview_cache.get(str(session_id))
            if cached is not None:
                self._session_preview_cache.move_to_end(str(session_id))
                return cached
            page = self.get_session_messages_page(session_id, offset=0, limit=DEFAULT_LIMIT)
            if page is None:
                return None
            payload["messages"] = page["messages"]
            self._remember_session_preview_cache(session_id, payload)
        return payload

    def build_session_audit(self, session_id):
        # BDD: 002 §M4 — `GET /api/sessions/{id}/audit` returns full payload.
        with self.lock:
            row = self.conn.execute(
                "SELECT file_path,activity_metadata_json FROM sessions WHERE id = ?",
                (str(session_id),),
            ).fetchone()
        if not row or not row["file_path"]:
            return None
        file_path = Path(row["file_path"])
        if not file_path.is_file():
            return None
        if self.source == 'mcode':
            from .mcode import mcode_file_signature, parse_mcode_session_file
            from .mcode_review import _selected_path, bind_mcode_recording_identity, build_mcode_audit
            file_path = _selected_path(self.sessions_dir, file_path)
            manifest = file_path.parent / 'manifest.json'
            before = mcode_file_signature(file_path.stat(), manifest.stat())
            parsed = parse_mcode_session_file(file_path)
            bind_mcode_recording_identity(self, session_id, parsed, file_path, row['activity_metadata_json'])
            payload = build_mcode_audit(parsed).to_dict()
            if mcode_file_signature(file_path.stat(), manifest.stat()) != before:
                raise ValueError('source_changed_during_read: refresh required')
            _selected_path(self.sessions_dir, file_path)
            return payload
        payload = build_audit_for_file(file_path, self.source, session_id_hint=str(session_id))
        if payload is None:
            return None
        return payload.to_dict()

    def get_stored_ai_audit(self, session_id):
        # BDD: 002 §M6 — stored AI/heuristic audit persisted in audit_json column.
        with self.lock:
            row = self.conn.execute(
                "SELECT audit_json FROM sessions WHERE id = ?",
                (str(session_id),),
            ).fetchone()
        if not row or not row["audit_json"]:
            return None
        try:
            return json.loads(row["audit_json"])
        except (ValueError, TypeError):
            return None

    def store_ai_audit(self, session_id, audit_json):
        with self.lock:
            self.conn.execute(
                "UPDATE sessions SET audit_json = ?, audit_updated_at = ? WHERE id = ?",
                (json.dumps(audit_json, ensure_ascii=False), int(time.time() * 1000), str(session_id)),
            )
            self.conn.commit()

    def clear_ai_audit(self, session_id):
        with self.lock:
            self.conn.execute(
                "UPDATE sessions SET audit_json = NULL, audit_updated_at = NULL WHERE id = ?",
                (str(session_id),),
            )
            self.conn.commit()

    def get_session_message(self, session_id, message_index):
        try:
            offset = int(message_index)
        except (TypeError, ValueError):
            return None
        if offset < 0:
            return None

        with self.lock:
            session = self.conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                return None
            row = self.conn.execute(
                """
                SELECT ts_ms, role, kind, text, tool_summary_json
                FROM messages
                WHERE session_id = ?
                ORDER BY ts_ms ASC, id ASC
                LIMIT 1 OFFSET ?
                """,
                (session_id, offset),
            ).fetchone()
        if not row:
            return None
        return self._serialize_message_row(row, offset, include_full_text=True)

    def search_session_messages(self, session_id, query, limit=None):
        term = str(query or "").strip()
        result = {
            "query": term,
            "match_count": 0,
            "message_match_count": 0,
            "matches": [],
        }
        if not term:
            return result

        pattern = re.compile(re.escape(term), re.IGNORECASE)

        with self.lock:
            session = self.conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                return None
            rows = self.conn.execute(
                """
                SELECT ts_ms, role, kind, text, tool_summary_json
                FROM messages
                WHERE session_id = ?
                ORDER BY ts_ms ASC, id ASC
                """,
                (session_id,),
            ).fetchall()

        try:
            clean_limit = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            clean_limit = None
        if clean_limit is not None and clean_limit <= 0:
            clean_limit = None

        matches = []
        total_hits = 0
        total_message_matches = 0
        for idx, row in enumerate(rows):
            text = str(row["text"] or "")
            found = list(pattern.finditer(text))
            hit_count = len(found)
            if hit_count <= 0:
                continue
            total_hits += hit_count
            total_message_matches += 1
            if clean_limit is None or len(matches) < clean_limit:
                serialized = self._serialize_message_row(row, idx)
                excerpt = build_search_excerpt_text(text, found[0].start(), found[0].end())
                matches.append({
                    "message_index": idx,
                    "ts_ms": row["ts_ms"],
                    "role": row["role"],
                    "kind": row["kind"],
                    "hit_count": hit_count,
                    "char_count": serialized["char_count"],
                    "is_truncated": serialized["is_truncated"],
                    "excerpt_text": excerpt["text"],
                    "excerpt_start": excerpt["start"],
                    "excerpt_end": excerpt["end"],
                    "excerpt_has_more_before": excerpt["has_more_before"],
                    "excerpt_has_more_after": excerpt["has_more_after"],
                })

        result["match_count"] = total_hits
        result["message_match_count"] = total_message_matches
        result["matches"] = matches
        return result

    def rename_session(self, session_id, title):
        title = str(title or "").strip()
        if not title:
            return False
        with self.lock:
            cur = self.conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ?", (title, session_id)
            )
            self.conn.commit()
            updated = cur.rowcount > 0

        if updated and self.recall_titles:
            self.recall_titles.set_custom_title(session_id, title)
        if updated:
            with self.lock:
                self._clear_session_preview_cache(session_id)

        return updated

    def archive_session(self, session_id, archived_dir):
        with self.lock:
            row = self.conn.execute(
                "SELECT file_path FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return False, "not_found"
            file_path = Path(row["file_path"])
            try:
                _move_to_backup(file_path, archived_dir, session_id)
            except OSError as exc:
                logging.getLogger(__name__).warning("Archive backup failed for %s: %s", session_id, exc)
                return False, "backup_failed"
            self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self.conn.commit()
            self._clear_session_preview_cache(session_id)
            return True, "archived"

    def _delete_sessions_with_backup(self, rows, deleted_dir, backup_label):
        deleted_dir = Path(deleted_dir)
        backup_dir = deleted_dir / (
            f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{backup_label}"
        )
        session_ids = []
        for row in rows:
            session_id = row["id"]
            file_path = Path(row["file_path"])
            try:
                _move_to_backup(file_path, backup_dir, session_id)
            except OSError as exc:
                logging.getLogger(__name__).warning("Deletion backup failed for %s: %s", session_id, exc)
                continue
            session_ids.append(session_id)

        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            self.conn.execute(
                f"DELETE FROM messages WHERE session_id IN ({placeholders})",
                session_ids,
            )
            self.conn.execute(
                f"DELETE FROM sessions WHERE id IN ({placeholders})",
                session_ids,
            )
            self.conn.commit()
            for session_id in session_ids:
                self._clear_session_preview_cache(session_id)
        return len(session_ids), str(backup_dir)

    def delete_project_sessions(self, project, deleted_dir):
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, file_path FROM sessions WHERE cwd = ? ORDER BY start_ts_ms DESC, id DESC",
                (project,),
            ).fetchall()
            if not rows:
                return False, "not_found", 0, None

            deleted_count, backup_dir = self._delete_sessions_with_backup(
                rows,
                deleted_dir,
                slugify_path_label(project),
            )
            complete = deleted_count == len(rows)
            detail = "deleted" if complete else "partial" if deleted_count else "backup_failed"
            return complete, detail, deleted_count, backup_dir

    def cleanup_weak_sessions(self, deleted_dir, min_user_messages=5, project=None):
        with self.lock:
            sql = (
                "SELECT s.id, s.file_path, s.title, s.cwd, "
                "COALESCE(SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END), 0) AS user_count "
                "FROM sessions s "
                "LEFT JOIN messages m ON m.session_id = s.id "
            )
            args = []
            if project:
                sql += "WHERE s.cwd = ? "
                args.append(project)
            sql += "GROUP BY s.id, s.file_path, s.title, s.cwd ORDER BY s.start_ts_ms DESC, s.id DESC"
            rows = self.conn.execute(sql, args).fetchall()

            weak_rows = []
            for row in rows:
                user_count = int(row["user_count"] or 0)
                if user_count < int(min_user_messages):
                    weak_rows.append(row)

            if not weak_rows:
                return True, "none", 0, None

            label = "weak-sessions"
            if project:
                label = f"{slugify_path_label(project)}-weak-sessions"
            deleted_count, backup_dir = self._delete_sessions_with_backup(
                weak_rows,
                deleted_dir,
                label,
            )
            complete = deleted_count == len(weak_rows)
            detail = "deleted" if complete else "partial" if deleted_count else "backup_failed"
            return complete, detail, deleted_count, backup_dir

    def pin_session(self, session_id, pinned):
        with self.lock:
            cur = self.conn.execute(
                "UPDATE sessions SET pinned = ? WHERE id = ?", (1 if pinned else 0, session_id)
            )
            self.conn.commit()
            if cur.rowcount > 0:
                self._clear_session_preview_cache(session_id)
            return cur.rowcount > 0


class HermesStateIndexer:
    def __init__(self, db_path: Path):
        self.source = "hermes"
        self.db_path = Path(db_path).expanduser()
        self.conn = sqlite3.connect(
            f"file:{self.db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self._session_preview_cache = OrderedDict()

    def _remember_session_preview_cache(self, session_id, payload):
        key = str(session_id or "")
        if not key:
            return
        self._session_preview_cache[key] = payload
        self._session_preview_cache.move_to_end(key)
        while len(self._session_preview_cache) > SESSION_PREVIEW_CACHE_SIZE:
            self._session_preview_cache.popitem(last=False)

    def maybe_update_index(self, max_age_seconds=None):
        return

    def scan_sessions(self):
        return

    def _project_value(self):
        return "COALESCE(NULLIF(s.source, ''), '(unknown source)')"

    def _project_value_no_alias(self):
        return "COALESCE(NULLIF(source, ''), '(unknown source)')"

    def _session_title(self, row):
        title = str(row["title"] or "").strip()
        if title:
            return title
        model = str(row["model"] or "").strip() or "Hermes"
        source = str(row["source"] or "").strip() or "session"
        return f"{model} - {source} - {str(row['id'])[:8]}"

    def _serialize_session_row(self, row):
        return {
            "id": row["id"],
            "start_ts_ms": parse_ts(row["started_at"]),
            "end_ts_ms": parse_ts(row["ended_at"]),
            "title": self._session_title(row),
            "message_count": int(row["message_count"] or 0),
            "cwd": str(row["source"] or "").strip() or "(unknown source)",
            "pinned": 0,
            **_neutral_audit_summary(),
        }

    def _tool_use_text(self, row):
        raw = str(row["tool_calls"] or "").strip()
        tool_name_fallback = str(row["tool_name"] or "").strip() or "tool"
        reasoning = str(row["reasoning"] or "").strip()
        blocks = []
        calls = None
        if raw:
            try:
                calls = json.loads(raw)
            except json.JSONDecodeError:
                calls = None
        if not isinstance(calls, list):
            calls = [calls] if calls else []

        for idx, call in enumerate(calls):
            function = call.get("function") if isinstance(call, dict) else None
            name = ""
            arguments = None
            if isinstance(function, dict):
                name = str(function.get("name") or "").strip()
                arguments = function.get("arguments")
            if not name and isinstance(call, dict):
                name = str(call.get("name") or call.get("type") or "").strip()
            name = name or tool_name_fallback
            lines = [f"Tool use: {name}"]
            if idx == 0 and reasoning:
                lines.append(f"Description: {reasoning}")
            if arguments not in (None, ""):
                lines.append("Input:")
                if isinstance(arguments, str):
                    formatted = arguments
                    try:
                        parsed = json.loads(arguments)
                    except json.JSONDecodeError:
                        parsed = None
                    if parsed is not None:
                        formatted = json.dumps(parsed, ensure_ascii=False, indent=2)
                        lines.append("```json")
                        lines.append(formatted)
                        lines.append("```")
                    else:
                        lines.append(arguments)
                else:
                    lines.append("```json")
                    lines.append(json.dumps(arguments, ensure_ascii=False, indent=2))
                    lines.append("```")
            blocks.append("\n".join(lines).strip())

        if blocks:
            return "\n\n".join(blocks)
        if reasoning:
            return f"Tool use: {tool_name_fallback}\nDescription: {reasoning}"
        return f"Tool use: {tool_name_fallback}"

    def _serialize_message_row(self, row, message_index, include_full_text=False):
        role = str(row["role"] or "").strip() or "assistant"
        content = str(row["content"] or "")
        reasoning = str(row["reasoning"] or "")
        tool_calls = str(row["tool_calls"] or "").strip()
        char_count_hint = None
        is_truncated_hint = None

        if role == "tool":
            mapped_role = "tool"
            kind = "tool_result"
            text = content
            if "content_char_count" in row.keys():
                char_count_hint = row["content_char_count"]
                is_truncated_hint = row["content_is_truncated"]
        elif tool_calls:
            mapped_role = "assistant"
            kind = "tool_use"
            text = self._tool_use_text(row)
        elif content:
            mapped_role = role
            kind = "message"
            text = content
            if "content_char_count" in row.keys():
                char_count_hint = row["content_char_count"]
                is_truncated_hint = row["content_is_truncated"]
        elif reasoning:
            mapped_role = role
            kind = "reasoning_summary"
            text = reasoning
            if "reasoning_char_count" in row.keys():
                char_count_hint = row["reasoning_char_count"]
                is_truncated_hint = row["reasoning_is_truncated"]
        else:
            mapped_role = role
            kind = "message"
            text = ""

        if content and reasoning and not tool_calls:
            # Hermes stores reasoning beside the answer in the same row. Keep
            # that row's message index while making both fields reachable from
            # a search deep link. Preview queries may have shortened each field
            # independently, so use their original lengths for the combined cap.
            separator = "\n\nReasoning:\n"
            text = content + separator + reasoning
            content_chars = row["content_char_count"] if "content_char_count" in row.keys() else len(content)
            reasoning_chars = row["reasoning_char_count"] if "reasoning_char_count" in row.keys() else len(reasoning)
            char_count_hint = int(content_chars or 0) + len(separator) + int(reasoning_chars or 0)
            is_truncated_hint = (not include_full_text) and (
                char_count_hint > MESSAGE_INLINE_FULL_THRESHOLD
                or bool(row["content_is_truncated"] if "content_is_truncated" in row.keys() else False)
                or bool(row["reasoning_is_truncated"] if "reasoning_is_truncated" in row.keys() else False)
            )

        text, char_count, is_truncated = normalize_message_payload(
            text,
            include_full_text=include_full_text,
            char_count=char_count_hint,
            is_truncated=is_truncated_hint,
        )
        return {
            "message_index": int(message_index),
            "ts_ms": parse_ts(row["timestamp"]),
            "role": mapped_role,
            "kind": kind,
            "text": text,
            "char_count": char_count,
            "is_truncated": bool(is_truncated),
        }

    def _session_lookup(self, session_id):
        return self.conn.execute(
            """
            SELECT id, source, user_id, model, started_at, ended_at, message_count, title
            FROM sessions
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()

    def list_sessions_page(self, q=None, start_ms=None, end_ms=None, limit=DEFAULT_PAGE_LIMIT, offset=0, cwd=None, sort=None, file_path=None, stable_order=False):
        clean_limit, clean_offset = normalize_page_args(limit, offset)
        terms = [t for t in str(q or "").split() if t]

        sql = (
            "SELECT s.id, s.source, s.user_id, s.model, s.started_at, s.ended_at, s.message_count, s.title "
            "FROM sessions s WHERE 1=1"
        )
        args = []
        if start_ms is not None:
            sql += " AND s.started_at >= ?"
            args.append(int(start_ms) / 1000)
        if end_ms is not None:
            sql += " AND s.started_at <= ?"
            args.append(int(end_ms) / 1000)
        if cwd:
            sql += f" AND {self._project_value()} = ?"
            args.append(str(cwd))
        for term in terms:
            like = f"%{term}%"
            sql += (
                " AND ("
                "COALESCE(s.title, '') LIKE ? OR COALESCE(s.source, '') LIKE ? OR COALESCE(s.user_id, '') LIKE ? "
                "OR COALESCE(s.model, '') LIKE ? OR s.id LIKE ? "
                "OR EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id AND COALESCE(m.content, '') LIKE ?) "
                "OR EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id AND COALESCE(m.reasoning, '') LIKE ?)"
                ")"
            )
            args.extend([like, like, like, like, like, like, like])

        sort_key = str(sort or "start").strip().lower()
        if sort_key in ("last", "end", "updated", "update"):
            sql += " ORDER BY COALESCE(s.ended_at, s.started_at) DESC, s.started_at DESC"
        else:
            sql += " ORDER BY s.started_at DESC, COALESCE(s.ended_at, s.started_at) DESC"
        if stable_order:
            sql += ", s.id ASC"
        sql += " LIMIT ? OFFSET ?"
        args.extend([clean_limit + 1, clean_offset])

        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()

        items = [self._serialize_session_row(row) for row in rows[:clean_limit]]
        has_more = len(rows) > clean_limit
        next_offset = clean_offset + len(items) if has_more else None
        return {
            "items": items,
            "limit": clean_limit,
            "offset": clean_offset,
            "has_more": has_more,
            "next_offset": next_offset,
        }

    def list_projects_page(self, q=None, limit=DEFAULT_PAGE_LIMIT, offset=0):
        clean_limit, clean_offset = normalize_page_args(limit, offset)
        project_expr = self._project_value_no_alias()
        sql = (
            f"SELECT {project_expr} AS project, COUNT(*) AS session_count, MAX(started_at) AS last_started_at "
            "FROM sessions WHERE 1=1"
        )
        args = []
        if q:
            sql += f" AND {project_expr} LIKE ?"
            args.append(f"%{q}%")
        sql += " GROUP BY project ORDER BY last_started_at DESC LIMIT ? OFFSET ?"
        args.extend([clean_limit + 1, clean_offset])

        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()

        items = [
            {
                "project": row["project"],
                "session_count": int(row["session_count"] or 0),
                "last_ts_ms": parse_ts(row["last_started_at"]),
            }
            for row in rows[:clean_limit]
        ]
        has_more = len(rows) > clean_limit
        next_offset = clean_offset + len(items) if has_more else None
        return {
            "items": items,
            "limit": clean_limit,
            "offset": clean_offset,
            "has_more": has_more,
            "next_offset": next_offset,
        }

    def get_session_metadata(self, session_id):
        with self.lock:
            session = self._session_lookup(session_id)
            if not session:
                return None
            payload = self._serialize_session_row(session)
            total_row = self.conn.execute(
                "SELECT COUNT(*) AS total FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            payload["message_total"] = int(total_row["total"] or 0)
            return payload

    def get_session_messages_page(self, session_id, offset=0, limit=DEFAULT_LIMIT):
        clean_limit, clean_offset = normalize_page_args(limit, offset, default_limit=DEFAULT_LIMIT, max_limit=DEFAULT_LIMIT)
        with self.lock:
            session = self._session_lookup(session_id)
            if not session:
                return None
            total_row = self.conn.execute(
                "SELECT COUNT(*) AS total FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            total = int(total_row["total"] or 0)
            messages = self.conn.execute(
                """
                SELECT
                    id,
                    timestamp,
                    role,
                    CASE
                        WHEN LENGTH(content) > ? THEN SUBSTR(content, 1, ?)
                        ELSE content
                    END AS content,
                    LENGTH(content) AS content_char_count,
                    CASE
                        WHEN LENGTH(content) > ? THEN 1
                        ELSE 0
                    END AS content_is_truncated,
                    tool_calls,
                    tool_name,
                    CASE
                        WHEN LENGTH(reasoning) > ? THEN SUBSTR(reasoning, 1, ?)
                        ELSE reasoning
                    END AS reasoning,
                    LENGTH(reasoning) AS reasoning_char_count,
                    CASE
                        WHEN LENGTH(reasoning) > ? THEN 1
                        ELSE 0
                    END AS reasoning_is_truncated
                FROM messages
                WHERE session_id = ?
                ORDER BY timestamp ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (
                    MESSAGE_INLINE_FULL_THRESHOLD,
                    MESSAGE_PREVIEW_FETCH_CHARS,
                    MESSAGE_INLINE_FULL_THRESHOLD,
                    MESSAGE_INLINE_FULL_THRESHOLD,
                    MESSAGE_PREVIEW_FETCH_CHARS,
                    MESSAGE_INLINE_FULL_THRESHOLD,
                    session_id,
                    clean_limit,
                    clean_offset,
                ),
            ).fetchall()
        return {
            "messages": [
                self._serialize_message_row(row, clean_offset + idx)
                for idx, row in enumerate(messages)
            ],
            "offset": clean_offset,
            "limit": clean_limit,
            "total": total,
        }

    def get_session(self, session_id, include_messages=True):
        session = self.get_session_metadata(session_id)
        if not session:
            return None
        payload = {"session": session}
        if include_messages:
            cached = self._session_preview_cache.get(str(session_id))
            if cached is not None:
                self._session_preview_cache.move_to_end(str(session_id))
                return cached
            page = self.get_session_messages_page(session_id, offset=0, limit=DEFAULT_LIMIT)
            if page is None:
                return None
            payload["messages"] = page["messages"]
            self._remember_session_preview_cache(session_id, payload)
        return payload

    def get_session_message(self, session_id, message_index):
        try:
            offset = int(message_index)
        except (TypeError, ValueError):
            return None
        if offset < 0:
            return None

        with self.lock:
            session = self._session_lookup(session_id)
            if not session:
                return None
            row = self.conn.execute(
                """
                SELECT id, timestamp, role, content, tool_calls, tool_name, reasoning
                FROM messages
                WHERE session_id = ?
                ORDER BY timestamp ASC, id ASC
                LIMIT 1 OFFSET ?
                """,
                (session_id, offset),
            ).fetchone()
        if not row:
            return None
        return self._serialize_message_row(row, offset, include_full_text=True)

    def search_session_messages(self, session_id, query, limit=None):
        term = str(query or "").strip()
        result = {
            "query": term,
            "match_count": 0,
            "message_match_count": 0,
            "matches": [],
        }
        if not term:
            return result

        pattern = re.compile(re.escape(term), re.IGNORECASE)

        with self.lock:
            session = self._session_lookup(session_id)
            if not session:
                return None
            rows = self.conn.execute(
                """
                SELECT id, timestamp, role, content, tool_calls, tool_name, reasoning
                FROM messages
                WHERE session_id = ?
                ORDER BY timestamp ASC, id ASC
                """,
                (session_id,),
            ).fetchall()

        try:
            clean_limit = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            clean_limit = None
        if clean_limit is not None and clean_limit <= 0:
            clean_limit = None

        matches = []
        total_hits = 0
        total_message_matches = 0
        for idx, row in enumerate(rows):
            serialized = self._serialize_message_row(row, idx, include_full_text=True)
            text = str(serialized["text"] or "")
            found = list(pattern.finditer(text))
            if not found:
                continue
            total_hits += len(found)
            total_message_matches += 1
            if clean_limit is None or len(matches) < clean_limit:
                excerpt = build_search_excerpt_text(text, found[0].start(), found[0].end())
                matches.append({
                    "message_index": idx,
                    "ts_ms": serialized["ts_ms"],
                    "role": serialized["role"],
                    "kind": serialized["kind"],
                    "hit_count": len(found),
                    "char_count": serialized["char_count"],
                    "is_truncated": serialized["char_count"] > MESSAGE_INLINE_FULL_THRESHOLD,
                    "excerpt_text": excerpt["text"],
                    "excerpt_start": excerpt["start"],
                    "excerpt_end": excerpt["end"],
                    "excerpt_has_more_before": excerpt["has_more_before"],
                    "excerpt_has_more_after": excerpt["has_more_after"],
                })

        result["match_count"] = total_hits
        result["message_match_count"] = total_message_matches
        result["matches"] = matches
        return result

    def rename_session(self, session_id, title):
        return False

    def archive_session(self, session_id, archived_dir):
        return False, "unsupported"

    def delete_project_sessions(self, project, deleted_dir):
        return False, "unsupported", 0, None

    def cleanup_weak_sessions(self, deleted_dir, min_user_messages=5, project=None):
        return False, "unsupported", 0, None

    def pin_session(self, session_id, pinned):
        return False

    def query_usage(self, start_ms=None, end_ms=None, cwd=None):
        # Hermes state DB exposes no per-session token columns; report zeros
        # so the Usage panel degrades gracefully for this read-only source.
        del start_ms, end_ms, cwd
        return {
            "totals": {"session_count": 0, "input": 0, "output": 0, "cached": 0, "reasoning": 0, "total": 0},
            "has_usage_data": False,
            "by_day": [],
            "by_project": [],
            "top_sessions": [],
        }


class OpenCodeIndexer:
    """Read-only indexer for the OpenCode local SQLite state DB.

    Schema (relevant tables in ``~/.local/share/opencode/opencode.db``):
      - ``session``: id, project_id, directory (cwd), title, time_created (ms),
        time_updated, agent, model (JSON), cost, tokens_*
      - ``message``: id, session_id, time_created, data (JSON envelope)
      - ``part``:    id, message_id, session_id, time_created,
        data (JSON; type ∈ text|reasoning|tool|step-start|step-finish|patch)
      - ``project``: id, worktree, name, ...
    """

    GLOBAL_PROJECT_ID = "global"

    def __init__(self, db_path: Path):
        self.source = "opencode"
        self.db_path = Path(db_path).expanduser()
        self.conn = sqlite3.connect(
            f"file:{self.db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
            timeout=30.0,
        )
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._session_preview_cache = OrderedDict()
        self._audit_cache = OrderedDict()

    def _remember_session_preview_cache(self, session_id, payload):
        key = str(session_id or "")
        if not key:
            return
        self._session_preview_cache[key] = payload
        self._session_preview_cache.move_to_end(key)
        while len(self._session_preview_cache) > SESSION_PREVIEW_CACHE_SIZE:
            self._session_preview_cache.popitem(last=False)

    def maybe_update_index(self, max_age_seconds=None):
        return

    def scan_sessions(self):
        return

    def _project_value(self):
        return "COALESCE(NULLIF(s.directory, ''), '(unknown directory)')"

    def _project_value_no_alias(self):
        return "COALESCE(NULLIF(directory, ''), '(unknown directory)')"

    def _session_title(self, row):
        title = str(row["title"] or "").strip()
        if title:
            return title[:80]
        model = ""
        raw_model = row["model"]
        if raw_model:
            try:
                model_obj = json.loads(raw_model) if isinstance(raw_model, str) else raw_model
                if isinstance(model_obj, dict):
                    model = str(model_obj.get("id") or model_obj.get("modelID") or "").strip()
            except (json.JSONDecodeError, TypeError):
                model = ""
        model = model or "OpenCode"
        return f"{model} - {str(row['id'])[:8]}"

    def _serialize_session_row(self, row, audit=None):
        cwd = str(row["directory"] or "").strip() or "(unknown directory)"
        item = {
            "id": row["id"],
            "start_ts_ms": parse_ts(row["time_created"]),
            "end_ts_ms": parse_ts(row["time_updated"]),
            "title": self._session_title(row),
            "message_count": int(row["message_count"] or 0),
            "cwd": cwd,
            "pinned": 0,
            "tokens_total": int(row["tokens_total"] or 0) if "tokens_total" in row.keys() else 0,
            **_neutral_audit_summary(),
        }
        if audit:
            item.update({
                "files_touched": audit.get("files_touched") or {"local": [], "remote": [], "inferred": []},
                "tools_used": audit.get("tools_used") or {},
                "command_intents": audit.get("command_intents") or {},
                "remote_context": audit.get("remote_context") or {},
                "outcome_signal": audit.get("outcome_signal") or "unknown",
                "value_score": int(audit.get("value_score") or 0),
                "friction_score": int(audit.get("friction_score") or 0),
                "action_density": float(audit.get("action_density") or 0.0),
            })
        return item

    @staticmethod
    def _model_id(raw_model):
        try:
            model = json.loads(raw_model) if isinstance(raw_model, str) else raw_model
        except (json.JSONDecodeError, TypeError):
            model = None
        if isinstance(model, dict):
            return str(model.get("id") or model.get("modelID") or model.get("model") or "")
        return str(model or "")

    @staticmethod
    def _parse_data(raw):
        if raw is None:
            return None
        if isinstance(raw, (dict, list)):
            return raw
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8", errors="replace")
            except Exception:
                return None
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _tool_input_paths(tool_input):
        if not isinstance(tool_input, dict):
            return []
        paths = []
        for key in ("file_path", "filePath", "path", "filename", "target_file", "file"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in paths:
                paths.append(value.strip())
        return paths

    @staticmethod
    def _format_tool_text(tool_name, state):
        name = str(tool_name or "").strip() or "tool"
        lines = [f"Tool use: {name}"]
        if isinstance(state, dict):
            arguments = state.get("input")
            if arguments not in (None, ""):
                lines.append("Input:")
                if isinstance(arguments, str):
                    try:
                        parsed = json.loads(arguments)
                    except json.JSONDecodeError:
                        parsed = None
                    if parsed is not None:
                        lines.append("```json")
                        lines.append(json.dumps(parsed, ensure_ascii=False, indent=2))
                        lines.append("```")
                    else:
                        lines.append(arguments)
                else:
                    lines.append("```json")
                    lines.append(json.dumps(arguments, ensure_ascii=False, indent=2))
                    lines.append("```")
            output = state.get("output")
            if output not in (None, ""):
                lines.append("Output:")
                if isinstance(output, str):
                    lines.append(output)
                else:
                    try:
                        lines.append(json.dumps(output, ensure_ascii=False, indent=2))
                    except (TypeError, ValueError):
                        lines.append(str(output))
            metadata = state.get("metadata")
            if isinstance(metadata, dict) and metadata:
                error = metadata.get("error")
                if error:
                    lines.append(f"Error: {error}")
        return "\n".join(lines).strip()

    @staticmethod
    def _flatten_part(message_role, part_type, part_data, part_time_ms):
        """Flatten one part into (role, kind, text, ts_ms); return None to skip."""
        role = str(message_role or "").strip() or "assistant"
        text = ""
        kind = "message"

        if part_type == "text":
            text = str(part_data.get("text") or "")
            kind = "message"
        elif part_type == "reasoning":
            text = str(part_data.get("text") or "")
            kind = "reasoning_summary"
        elif part_type == "tool":
            tool_name = part_data.get("tool") or ""
            state = part_data.get("state") or {}
            status = str(state.get("status") or "").strip().lower() if isinstance(state, dict) else ""
            text = OpenCodeIndexer._format_tool_text(tool_name, state)
            use_summary = _codex_summarize_tool_use(tool_name, json.dumps(state.get("input"), ensure_ascii=False) if isinstance(state, dict) else None)
            if status in ("pending", "running"):
                return ("assistant", "tool_use", text, part_time_ms, use_summary)
            result_summary = _codex_summarize_tool_result(tool_name, json.dumps(state, ensure_ascii=False))
            for key in ("name", "category", "headline", "file_path", "change_kind", "lines_added", "lines_removed"):
                if use_summary.get(key) not in (None, ""):
                    result_summary[key] = use_summary[key]
            return ("tool", "tool_result", text, part_time_ms, result_summary)
        else:
            return None  # step-start / step-finish / patch / unknown

        if not text:
            return None
        return (role, kind, text, part_time_ms, None)

    def _part_order_sql(self):
        return 'm.time_created ASC, p.time_created ASC, p.id ASC'

    def _load_flat_messages(self, session_id):
        """Load + flatten all parts for a session, in time order."""
        with self.lock:
            rows = self.conn.execute(
                f"""
                SELECT
                    p.id            AS part_id,
                    p.message_id    AS message_id,
                    p.time_created  AS part_ts_ms,
                    p.data          AS part_data,
                    m.data          AS message_data,
                    m.time_created  AS message_ts_ms
                FROM part p
                JOIN message m ON m.id = p.message_id
                WHERE p.session_id = ?
                ORDER BY {self._part_order_sql()}
                """,
                (session_id,),
            ).fetchall()

        flat = []
        for row in rows:
            message_data = self._parse_data(row["message_data"])
            if not message_data:
                continue
            message_role = message_data.get("role") or "assistant"
            part_data = self._parse_data(row["part_data"]) or {}
            part_type = str(part_data.get("type") or "").strip()
            if not part_type:
                continue
            part_time_ms = parse_ts(row["part_ts_ms"]) or parse_ts(row["message_ts_ms"])
            flat_part = self._flatten_part(message_role, part_type, part_data, part_time_ms)
            if flat_part is None:
                continue
            role, kind, text, ts_ms, tool_summary = flat_part
            item = {"ts_ms": ts_ms, "role": role, "kind": kind, "text": text}
            if tool_summary:
                item["tool_summary"] = tool_summary
            flat.append(item)
        return flat

    def _load_audit_events(self, session_id):
        with self.lock:
            rows = self.conn.execute(
                f"""
                SELECT p.id AS part_id, p.time_created AS part_ts_ms,
                       p.data AS part_data, m.data AS message_data,
                       m.time_created AS message_ts_ms
                FROM part p
                JOIN message m ON m.id = p.message_id
                WHERE p.session_id = ?
                ORDER BY {self._part_order_sql()}
                """,
                (session_id,),
            ).fetchall()

        events = []
        display_index = 0
        known_mutation_paths = set()
        for row in rows:
            message_data = self._parse_data(row["message_data"]) or {}
            role = str(message_data.get("role") or "assistant").strip().lower()
            role = role if role in ("user", "assistant", "system", "developer", "tool") else "other"
            part_data = self._parse_data(row["part_data"]) or {}
            part_type = str(part_data.get("type") or "").strip()
            ts_ms = parse_ts(row["part_ts_ms"]) or parse_ts(row["message_ts_ms"]) or 0
            flat_part = self._flatten_part(role, part_type, part_data, ts_ms)
            part_message_index = display_index if flat_part is not None else max(0, display_index - 1)

            if part_type == "text":
                text = str(part_data.get("text") or "")
                if text:
                    events.append(AuditEvent(
                        ts_ms=ts_ms, role=role, kind="message", text=text,
                        message_index=part_message_index,
                    ))
            elif part_type == "reasoning":
                text = str(part_data.get("text") or "")
                if text:
                    events.append(AuditEvent(
                        ts_ms=ts_ms, role="assistant", kind="reasoning", text=text,
                        message_index=part_message_index,
                    ))
            elif part_type == "tool":
                tool_name = str(part_data.get("tool") or "tool")
                state = part_data.get("state") if isinstance(part_data.get("state"), dict) else {}
                tool_input = state.get("input")
                events.append(AuditEvent(
                    ts_ms=ts_ms, role="tool", kind="tool_use",
                    tool_name=tool_name, tool_args=tool_input,
                    message_index=part_message_index,
                ))
                for path in self._tool_input_paths(tool_input):
                    known_mutation_paths.add(path)
                status = str(state.get("status") or "").strip().lower()
                if status not in ("pending", "running"):
                    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
                    exit_code = metadata.get("exit")
                    exit_codes = [exit_code] if isinstance(exit_code, int) else []
                    output = state.get("output")
                    error_text = state.get("error") or metadata.get("error")
                    result_text = str(output if output not in (None, "") else error_text or "")[:20_000]
                    events.append(AuditEvent(
                        ts_ms=ts_ms, role="tool", kind="tool_result",
                        tool_result_text=result_text,
                        tool_result_error=(status == "error" or (isinstance(exit_code, int) and exit_code != 0)),
                        tool_result_exit_codes=exit_codes,
                        tool_result_items=[result_text] if result_text else [],
                        message_index=part_message_index,
                    ))
            elif part_type == "patch":
                files = [str(path) for path in (part_data.get("files") or []) if str(path).strip()]
                files = [path for path in files if path not in known_mutation_paths]
                if files:
                    events.append(AuditEvent(
                        ts_ms=ts_ms, role="tool", kind="tool_use", tool_name="apply_patch",
                        tool_args={
                            "file_path": files[0],
                            "edits": [{"file_path": path} for path in files[1:]],
                        },
                        message_index=part_message_index,
                    ))
                    known_mutation_paths.update(files)

            if flat_part is not None:
                display_index += 1
        return events

    def build_session_audit(self, session_id):
        with self.lock:
            session = self._session_lookup(session_id)
        if not session:
            return None
        cache_key = (parse_ts(session["time_updated"]) or 0, AUDIT_VERSION)
        cached = self._audit_cache.get(str(session_id))
        if cached and cached[0] == cache_key:
            self._audit_cache.move_to_end(str(session_id))
            return cached[1]

        payload = build_audit_from_events(
            self._load_audit_events(session_id),
            session_id=str(session_id),
            source=self.source,
            model=self._model_id(session["model"]),
            started_at=parse_ts(session["time_created"]) or 0,
            ended_at=parse_ts(session["time_updated"]) or 0,
        ).to_dict()
        self._audit_cache[str(session_id)] = (cache_key, payload)
        self._audit_cache.move_to_end(str(session_id))
        while len(self._audit_cache) > 256:
            self._audit_cache.popitem(last=False)
        return payload

    def _serialize_flat_message(self, flat_msg, message_index, include_full_text=False):
        text = str(flat_msg.get("text") or "")
        text, char_count, is_truncated = normalize_message_payload(
            text,
            include_full_text=include_full_text,
        )
        result = {
            "message_index": int(message_index),
            "ts_ms": flat_msg.get("ts_ms"),
            "role": str(flat_msg.get("role") or "assistant"),
            "kind": str(flat_msg.get("kind") or "message"),
            "text": text,
            "char_count": char_count,
            "is_truncated": bool(is_truncated),
        }
        if flat_msg.get("tool_summary"):
            result["tool_summary"] = dict(flat_msg["tool_summary"])
        return result

    def _session_lookup(self, session_id):
        return self.conn.execute(
            """
            SELECT id, project_id, parent_id, slug, directory, title, version,
                   share_url, summary_additions, summary_deletions, summary_files,
                   summary_diffs, time_created, time_updated, agent, model,
                   cost, tokens_input, tokens_output, tokens_reasoning,
                   tokens_cache_read, tokens_cache_write,
                   (SELECT COUNT(*) FROM message m WHERE m.session_id = session.id) AS message_count
            FROM session
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()

    def _session_message_count(self, session_id):
        """Count of flat (rendered) messages for a session."""
        flat = self._load_flat_messages(session_id)
        return len(flat)

    def list_sessions_page(self, q=None, start_ms=None, end_ms=None, limit=DEFAULT_PAGE_LIMIT, offset=0, cwd=None, sort=None, file_path=None, stable_order=False):
        clean_limit, clean_offset = normalize_page_args(limit, offset)
        terms = [t for t in str(q or "").split() if t]

        sql = (
            "SELECT s.id, s.project_id, s.directory, s.title, s.time_created, "
            "s.time_updated, s.model, s.agent, "
            "(COALESCE(s.tokens_input, 0) + COALESCE(s.tokens_output, 0)) AS tokens_total, "
            "(SELECT COUNT(*) FROM message m WHERE m.session_id = s.id) AS message_count "
            "FROM session s WHERE 1=1"
        )
        args = []
        if start_ms is not None:
            sql += " AND s.time_created >= ?"
            args.append(int(start_ms))
        if end_ms is not None:
            sql += " AND s.time_created <= ?"
            args.append(int(end_ms))
        if cwd:
            sql += f" AND {self._project_value()} = ?"
            args.append(str(cwd))
        for term in terms:
            like = f"%{term}%"
            sql += (
                " AND ("
                "COALESCE(s.title, '') LIKE ? OR COALESCE(s.directory, '') LIKE ? "
                "OR COALESCE(s.agent, '') LIKE ? OR COALESCE(s.model, '') LIKE ? "
                "OR s.id LIKE ? "
                "OR EXISTS (SELECT 1 FROM message m WHERE m.session_id = s.id AND m.data LIKE ?) "
                "OR EXISTS (SELECT 1 FROM part p WHERE p.session_id = s.id AND p.data LIKE ?)"
                ")"
            )
            args.extend([like, like, like, like, like, like, like])

        sort_key = str(sort or "start").strip().lower()
        needs_audit_scan = bool(file_path) or sort_key == "value"
        if sort_key in ("last", "end", "updated", "update"):
            sql += " ORDER BY s.time_updated DESC, s.time_created DESC"
        else:
            sql += " ORDER BY s.time_created DESC, s.time_updated DESC"
        if stable_order:
            sql += ", s.id ASC"
        if not needs_audit_scan:
            sql += " LIMIT ? OFFSET ?"
            args.extend([clean_limit + 1, clean_offset])

        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()

        audited_items = [
            self._serialize_session_row(row, self.build_session_audit(row["id"]))
            for row in rows
        ]
        if file_path:
            audited_items = [item for item in audited_items if _match_files_touched(item.get("files_touched"), file_path)]
        if sort_key == "value":
            audited_items.sort(
                key=lambda item: (int(item.get("value_score") or 0), int(item.get("start_ts_ms") or 0)),
                reverse=True,
            )
        if needs_audit_scan:
            page_items = audited_items[clean_offset:clean_offset + clean_limit + 1]
        else:
            page_items = audited_items
        items = page_items[:clean_limit]
        has_more = len(page_items) > clean_limit
        next_offset = clean_offset + len(items) if has_more else None
        return {
            "items": items,
            "limit": clean_limit,
            "offset": clean_offset,
            "has_more": has_more,
            "next_offset": next_offset,
        }

    def list_projects_page(self, q=None, limit=DEFAULT_PAGE_LIMIT, offset=0):
        clean_limit, clean_offset = normalize_page_args(limit, offset)
        project_expr = self._project_value_no_alias()
        sql = (
            f"SELECT {project_expr} AS project, COUNT(*) AS session_count, "
            "MAX(time_created) AS last_started_at "
            "FROM session WHERE 1=1 "
            f"AND (project_id IS NULL OR project_id != '{self.GLOBAL_PROJECT_ID}')"
        )
        args = []
        if q:
            sql += f" AND {project_expr} LIKE ?"
            args.append(f"%{q}%")
        sql += " GROUP BY project ORDER BY last_started_at DESC LIMIT ? OFFSET ?"
        args.extend([clean_limit + 1, clean_offset])

        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()

        items = [
            {
                "project": row["project"],
                "session_count": int(row["session_count"] or 0),
                "last_ts_ms": parse_ts(row["last_started_at"]),
            }
            for row in rows[:clean_limit]
        ]
        has_more = len(rows) > clean_limit
        next_offset = clean_offset + len(items) if has_more else None
        return {
            "items": items,
            "limit": clean_limit,
            "offset": clean_offset,
            "has_more": has_more,
            "next_offset": next_offset,
        }

    def get_session_metadata(self, session_id):
        with self.lock:
            session = self._session_lookup(session_id)
            if not session:
                return None
            payload = self._serialize_session_row(session, self.build_session_audit(session_id))
            payload["message_total"] = self._session_message_count(session_id)
            return payload

    def get_session_messages_page(self, session_id, offset=0, limit=DEFAULT_LIMIT):
        clean_limit, clean_offset = normalize_page_args(
            limit, offset, default_limit=DEFAULT_LIMIT, max_limit=DEFAULT_LIMIT
        )
        with self.lock:
            session = self._session_lookup(session_id)
            if not session:
                return None
            flat = self._load_flat_messages(session_id)
        total = len(flat)
        window = flat[clean_offset:clean_offset + clean_limit]
        return {
            "messages": [
                self._serialize_flat_message(row, clean_offset + idx)
                for idx, row in enumerate(window)
            ],
            "offset": clean_offset,
            "limit": clean_limit,
            "total": total,
        }

    def get_session(self, session_id, include_messages=True):
        session = self.get_session_metadata(session_id)
        if not session:
            return None
        payload = {"session": session}
        if include_messages:
            cached = self._session_preview_cache.get(str(session_id))
            if cached is not None:
                self._session_preview_cache.move_to_end(str(session_id))
                return cached
            page = self.get_session_messages_page(session_id, offset=0, limit=DEFAULT_LIMIT)
            if page is None:
                return None
            payload["messages"] = page["messages"]
            self._remember_session_preview_cache(session_id, payload)
        return payload

    def get_session_message(self, session_id, message_index):
        try:
            offset = int(message_index)
        except (TypeError, ValueError):
            return None
        if offset < 0:
            return None
        with self.lock:
            session = self._session_lookup(session_id)
            if not session:
                return None
            flat = self._load_flat_messages(session_id)
        if offset >= len(flat):
            return None
        return self._serialize_flat_message(flat[offset], offset, include_full_text=True)

    def search_session_messages(self, session_id, query, limit=None):
        term = str(query or "").strip()
        result = {
            "query": term,
            "match_count": 0,
            "message_match_count": 0,
            "matches": [],
        }
        if not term:
            return result

        pattern = re.compile(re.escape(term), re.IGNORECASE)
        with self.lock:
            session = self._session_lookup(session_id)
            if not session:
                return None
            flat = self._load_flat_messages(session_id)

        try:
            clean_limit = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            clean_limit = None
        if clean_limit is not None and clean_limit <= 0:
            clean_limit = None

        matches = []
        total_hits = 0
        total_message_matches = 0
        for idx, msg in enumerate(flat):
            text = str(msg.get("text") or "")
            found = list(pattern.finditer(text))
            if not found:
                continue
            total_hits += len(found)
            total_message_matches += 1
            if clean_limit is None or len(matches) < clean_limit:
                excerpt = build_search_excerpt_text(text, found[0].start(), found[0].end())
                matches.append({
                    "message_index": idx,
                    "ts_ms": msg.get("ts_ms"),
                    "role": msg.get("role"),
                    "kind": msg.get("kind"),
                    "hit_count": len(found),
                    "char_count": len(text),
                    "is_truncated": len(text) > MESSAGE_INLINE_FULL_THRESHOLD,
                    "excerpt_text": excerpt["text"],
                    "excerpt_start": excerpt["start"],
                    "excerpt_end": excerpt["end"],
                    "excerpt_has_more_before": excerpt["has_more_before"],
                    "excerpt_has_more_after": excerpt["has_more_after"],
                })

        result["match_count"] = total_hits
        result["message_match_count"] = total_message_matches
        result["matches"] = matches
        return result

    def rename_session(self, session_id, title):
        return False

    def archive_session(self, session_id, archived_dir):
        return False, "unsupported"

    def delete_project_sessions(self, project, deleted_dir):
        return False, "unsupported", 0, None

    def cleanup_weak_sessions(self, deleted_dir, min_user_messages=5, project=None):
        return False, "unsupported", 0, None

    def pin_session(self, session_id, pinned):
        return False

    def query_usage(self, start_ms=None, end_ms=None, cwd=None):
        # ALGO: OpenCode tracks tokens on its own session rows (see class
        # docstring); aggregate them directly instead of re-deriving.
        where_sql = " WHERE 1=1"
        args = []
        if start_ms is not None:
            where_sql += " AND time_created >= ?"
            args.append(int(start_ms))
        if end_ms is not None:
            where_sql += " AND time_created <= ?"
            args.append(int(end_ms))
        if cwd:
            where_sql += f" AND {self._project_value_no_alias()} = ?"
            args.append(cwd)

        def _sum_columns():
            return (
                "COUNT(*) AS session_count, "
                "COALESCE(SUM(tokens_input), 0) AS input, "
                "COALESCE(SUM(tokens_output), 0) AS output, "
                "COALESCE(SUM(COALESCE(tokens_cache_read, 0) + COALESCE(tokens_cache_write, 0)), 0) AS cached, "
                "COALESCE(SUM(tokens_reasoning), 0) AS reasoning, "
                "COALESCE(SUM(tokens_input), 0) + COALESCE(SUM(tokens_output), 0) AS total"
            )

        def _fetch(sql, extra_args=None):
            with self.lock:
                rows = self.conn.execute(sql, [*args, *(extra_args or [])]).fetchall()
            return [dict(row) for row in rows]

        totals_rows = _fetch(f"SELECT {_sum_columns()} FROM session{where_sql}")
        totals = totals_rows[0] if totals_rows else {}
        by_day = _fetch(
            "SELECT date(time_created / 1000, 'unixepoch', 'localtime') AS day, "
            f"{_sum_columns()} FROM session{where_sql} "
            "GROUP BY day ORDER BY day DESC LIMIT 60"
        )
        by_project = _fetch(
            f"SELECT {self._project_value_no_alias()} AS project, "
            f"{_sum_columns()} FROM session{where_sql} "
            "GROUP BY project ORDER BY total DESC LIMIT 12"
        )
        top_sessions = _fetch(
            "SELECT id, title, directory AS cwd, time_created AS start_ts_ms, "
            "tokens_input, tokens_output, "
            "COALESCE(tokens_cache_read, 0) + COALESCE(tokens_cache_write, 0) AS tokens_cached, "
            "COALESCE(tokens_reasoning, 0) AS tokens_reasoning, "
            "COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0) AS tokens_total "
            f"FROM session{where_sql} ORDER BY tokens_total DESC LIMIT 10"
        )
        return {
            "totals": totals,
            "has_usage_data": int(totals.get("total") or 0) > 0,
            "by_day": by_day,
            "by_project": by_project,
            "top_sessions": top_sessions,
        }
