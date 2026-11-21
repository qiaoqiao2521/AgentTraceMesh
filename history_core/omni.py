"""Read-only cross-source, multi-slot omni discovery, search, accomplishment radar,
and Daily 3-Slot WeChat Studio feed for AgentTraceMesh.

Design invariants:
- Python 3.11+ stdlib only; no external dependencies.
- 100% read-only (`mode=ro`) against derived caches and native provider SQLite stores.
- Demo mode (`demo=True`) strictly isolates to packaged synthetic demo caches and
  never probes private home directories, AccountMesh slots, or local ports.
- Public messages only (`user`, `assistant`, `tool`); internal `thinking`/`reasoning`
  records are never returned or matched, and excerpts pass through `redact_text`.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import sqlite3
import sys
import time
import urllib.request

from .evidence import redact_text
from .providers import SOURCES, canonical_source
from .sources import _escape_sql_like, _indexed_public_message_sql
from .projection import PROJECTED_DB
from .omni_markers import omni_markers
from .titles import is_context_title, meaningful_user_title

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VAULT_PATH = REPO_ROOT / 'data' / 'experience_vault.json'

TOPIC_RULES = {
    'ai_agent': {
        'label': 'AI Agent 与工具链基建',
        'pattern': re.compile(
            r'tracemesh|controlmesh|accountmesh|specmesh|hcom|zcode|opencode|codex|claude|'
            r'antigravity|agy|mcp|skill|subagent|harness|cliproxy|litellm|proxy|agent|上下文|智能体|控制面|验收',
            re.I,
        ),
    },
    'media_3d': {
        'label': '3D / Blender / 多模态与视觉',
        'pattern': re.compile(
            r'blender|three\.?js|glb|gltf|pbr|isotruth|vace|sam2|vggt|megasam|render|shader|'
            r'camera|motion|skeleton|rig|dxf|slg|tif|cad|resolve|ffmpeg|tts|voice|video|动捕|骨骼|渲染|试衣|分镜|画框|打字机|眼镜',
            re.I,
        ),
    },
    'robotics': {
        'label': '具身智能 / ROS2 / 机器人仿真',
        'pattern': re.compile(
            r'ros2|fishbot|so101|lerobot|mujoco|gazebo|urdf|nav2|slam|cartographer|px4|drone|'
            r'esp32|micro-ros|sim2real|humanoid|g1|机器人|机械臂|底盘|仿真|雷达|舵机',
            re.I,
        ),
    },
    'wechat_content': {
        'label': '公众号与自媒体自动化引擎',
        'pattern': re.compile(
            r'wechat|gongzhonghao|qiao-wechat|xueyu|aihot|newsnow|newrank|gemctl|argon|'
            r'draftbox|公众号|草稿箱|选题|排版|自媒体|小红书|本草|素材库|文风',
            re.I,
        ),
    },
    'web_product': {
        'label': 'Web 工作台与应用产品实战',
        'pattern': re.compile(
            r'lifegame|novel|wuxia|gansu|meridian|workbench|react|vite|electron|apk|android|'
            r'canvas|infographic|工作台|网站|前端|小说|穴位|学校|拾像',
            re.I,
        ),
    },
    'infra_ops': {
        'label': 'Linux 运维 / 云算力 / 网络排障',
        'pattern': re.compile(
            r'colab|rustdesk|tailscale|racknerd|436|systemd|ssh|oauth|feishu|lark|sqlite|wal|'
            r'proxy|clash|wsl|ubuntu|gpu|docker|宽带|远程|服务器|死锁|性能|缓存',
            re.I,
        ),
    },
}

SMOKE_NOISE_RE = re.compile(
    r'agent-link-smoke|smoke-test|synthetic-codex-|tmp/dsh-verify|demo-isolated',
    re.I,
)

MAIN_WECHAT_CATEGORIES = ('ai_agent', 'web_product', 'infra_ops')

OFF_TRACK_MAIN_WECHAT_RE = re.compile(
    r'(?<![a-z0-9_])(?:ros2?|mujoco|isaac|omniverse|lerobot|sim2real|humanoid|locomotion|'
    r'urdf|gazebo|blender|smpl-?x?|mocap)(?![a-z0-9_])|具身智能|机械臂|步态控制|动作捕捉',
    re.I,
)

BIG_TECH_RE = re.compile(
    r'openai\.com|anthropic\.com|claude\.com|deepmind\.google|blog\.google|developers\.googleblog\.com|'
    r'ai\.google\.dev|blog\.cloudflare\.com|github\.blog|huggingface\.co|'
    r'github\.com/(?:openai|anthropics|google|google-deepmind|google-gemini|modelcontextprotocol|cloudflare)|'
    r'\b(?:openai|chatgpt|codex|gpt-?[45o]|o[134]-?(?:mini|pro)|anthropic|claude|google|gemini|deepmind|antigravity)\b',
    re.I,
)

HN_BREAKING_RE = re.compile(
    r'hacker\s*news|hnrss|news\.ycombinator\.com|github\s*trending|openrouter|v2ex|solidot|product\s*hunt|deepflood|nodeseek',
    re.I,
)


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _store_key(kind: str, source: str, db_path: Path, slot: str = '') -> str:
    digest = hashlib.sha256(f"{kind}:{source}:{slot}:{db_path.resolve()}".encode('utf-8')).hexdigest()[:14]
    return f"{source}-{slot + '-' if slot else ''}{digest}"


def _slot_from_meta(source_str: str) -> str:
    match = re.search(r'/\.(?:agy|codex)-slots/([^/]+)/', source_str or '')
    return match.group(1) if match and match.group(1) != '_shared' else ''


def _physical_marker_roots(home):
    """Map resolved origins to every slot that advertises them, including active links."""
    roots = {}
    def register(source, path, alias):
        if (path.is_dir() if source in ('codex', 'claude') else path.is_file()):
            roots.setdefault((source, str(path.resolve())), []).append(alias)
    for path in sorted((home / '.codex-slots').glob('*/codex/sessions')):
        if path.parents[1].name != '_shared':
            register('codex', path, path.parents[1].name)
    for path in sorted((home / '.agy-slots').glob('*/gemini/antigravity/conversation_summaries.db')):
        if path.parents[2].name != '_shared':
            register('antigravity', path, path.parents[2].name)
    register('codex', home / '.codex/sessions', 'active')
    register('claude', home / '.claude/projects', 'active')
    register('antigravity', home / '.gemini/antigravity/conversation_summaries.db', 'active')
    return roots


def _source_display_label(source: str, slot: str = '', is_archive: bool = False) -> str:
    names = {
        'codex': 'Codex',
        'claude': 'Claude Code',
        'codebuddy': 'CodeBuddy (cbc)',
        'mcode': 'MiniMax (mcode)',
        'zcode': 'ZCode',
        'opencode': 'OpenCode',
        'agy': 'AGY CLI',
        'antigravity': 'Antigravity',
        'gemini': 'Gemini CLI',
        'copilot': 'GitHub Copilot',
        'pi': 'pi',
        'prime': 'Prime Agent',
        'openclaw': 'OpenClaw',
        'hermes': 'Hermes',
    }
    base = names.get(source, source)
    parts = [base]
    if slot:
        parts.append(f"[{slot}]")
    if is_archive:
        parts.append("(归档索引)")
    return ' '.join(parts)


def _inspect_store_counts(db_path: Path, kind: str) -> tuple[int, int | None]:
    table = 'session' if kind == 'opencode_family' else 'sessions'
    ts_col = 'time_updated' if kind == 'opencode_family' else (
        'COALESCE(ended_at, started_at) * 1000' if kind == 'hermes' else 'COALESCE(end_ts_ms, start_ts_ms)'
    )
    with _ro_connect(db_path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if table not in names:
            return 0, None
        row = conn.execute(f"SELECT COUNT(*) AS n, MAX({ts_col}) AS latest FROM {table}").fetchone()
        return int(row['n'] or 0), (int(row['latest']) if row['latest'] is not None else None)


def discover_omni_stores(
    data_dir: Path | str | None = None,
    *,
    demo: bool = False,
    include_archives: bool = True,
    marker_stores: bool = False,
    cache_only: bool = False,
) -> list[dict]:
    """Return read-only descriptors for all discoverable local conversation stores."""
    stores: list[dict] = []
    seen_db_paths: set[Path] = set()

    def add_store(source: str, db_path: Path, kind: str, *, slot: str = '', is_archive: bool = False,
                  origin_path: str = '', can_open_history: bool = False):
        try:
            resolved = db_path.resolve(strict=True)
        except OSError:
            return
        if resolved in seen_db_paths or not resolved.is_file():
            return
        try:
            if cache_only and kind != 'indexed':
                count, latest, cache_status = 0, None, 'unsupported'
            else:
                count, latest = _inspect_store_counts(resolved, kind)
                cache_status = 'indexed'
        except (sqlite3.Error, OSError):
            if not marker_stores:
                return
            count, latest, cache_status = 0, None, 'cache_unreadable'
        if count <= 0 and is_archive and not marker_stores:
            return
        seen_db_paths.add(resolved)
        stores.append({
            'store_key': _store_key(kind, source, resolved, slot),
            'source': source,
            'slot': slot,
            'is_archive': is_archive,
            'kind': kind,
            'label': _source_display_label(source, slot, is_archive),
            'db_path': str(resolved),
            'origin_path': origin_path or str(resolved),
            'session_count': count,
            'latest_ts_ms': latest,
            'can_open_history': can_open_history,
            'slot_aliases': [slot] if slot else [],
            'cache_status': cache_status,
            'capture_mode': 'projected' if db_path.name == PROJECTED_DB else 'strict',
        })

    if demo:
        base = Path(data_dir).expanduser().resolve() if data_dir else (REPO_ROOT / 'demo' / '.data' / 'demo-isolated')
        if base.is_dir():
            for sqlite_file in sorted(base.glob('*.sqlite')):
                stem = sqlite_file.stem
                src = 'codex' if stem in ('index', 'index_linux') else stem.replace('index_linux_', '').replace('index_', '')
                if src in SOURCES:
                    add_store(src, sqlite_file, 'indexed', can_open_history=True)
        return stores

    home = Path.home()
    xdg_cache = Path(os.environ.get('XDG_CACHE_HOME') or (home / '.cache')).expanduser()
    reader_root = Path(data_dir).expanduser() if data_dir else (xdg_cache / 'agenttracemesh' / 'reader')
    physical = _physical_marker_roots(home) if data_dir is None else {}
    bound = {'machine-' + src + '-' + hashlib.sha256(origin.encode()).hexdigest(): origin
             for src, origin in physical}
    if data_dir is None:
        # Pre-slot caches bind the lexical active path. Its current symlink
        # target cannot identify the account that populated an old cache.
        legacy_root = str(home / '.codex/sessions')
        bound.setdefault('machine-codex-' + hashlib.sha256(legacy_root.encode()).hexdigest(), legacy_root)

    active_gemini_target = ''
    try:
        gemini_home = home / '.gemini'
        if gemini_home.exists():
            active_gemini_target = str(gemini_home.resolve())
    except OSError:
        active_gemini_target = ''

    # 1. Primary derived reader caches (includes Codex, Claude, CodeBuddy, mcode, Gemini, and AGY/Antigravity slots!)
    seen_agy_origins: set[tuple[str, str]] = set()
    if reader_root.is_dir():
        for sub in sorted(reader_root.iterdir()):
            if not sub.is_dir() or not sub.name.startswith('machine-'):
                continue
            parts = sub.name.split('-', 2)
            if len(parts) < 3:
                continue
            src = canonical_source(parts[1])
            if src not in SOURCES:
                continue
            sqlite_files = sorted(sub.glob('*.sqlite'))
            for db_file in sqlite_files:
                if db_file.name.startswith('index.projected-') and (not marker_stores or db_file.name != PROJECTED_DB):
                    continue
                slot = ''
                origin = ''
                try:
                    with _ro_connect(db_file) as conn:
                        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                        if 'reader_state' in tables:
                            meta = dict(conn.execute("SELECT key, value FROM reader_state").fetchall())
                            origin = meta.get('agy_source') or meta.get('source_root') or bound.get(sub.name, '')
                            slot = _slot_from_meta(origin)
                except sqlite3.Error:
                    pass
                if src in ('agy', 'antigravity') and origin:
                    # Avoid duplicate `agy` vs `antigravity` cache pointing to the exact same summary DB file
                    canonical_kind = 'agy' if 'antigravity-cli' in origin else 'antigravity'
                    if src != canonical_kind:
                        continue
                    dedup_key = (canonical_kind, origin)
                    if dedup_key in seen_agy_origins:
                        continue
                    seen_agy_origins.add(dedup_key)
                can_open = src not in ('agy', 'antigravity') or (
                    bool(active_gemini_target) and origin.startswith(active_gemini_target)
                )
                add_store(src, db_file, 'indexed', slot=slot, origin_path=origin, can_open_history=can_open)
                if (data_dir is None and src == 'codex' and origin == legacy_root
                        and (src, origin) not in physical and stores and stores[-1]['db_path'] == str(db_file.resolve())):
                    stores[-1].update(slot_aliases=['historical-active'], origin_binding='historical_active_alias')

    # 2. Native OpenCode / ZCode / Hermes databases (only in default user discovery, never when isolated data_dir is supplied)
    if data_dir is None:
        zcode_db = home / '.zcode' / 'cli' / 'db' / 'db.sqlite'
        if zcode_db.is_file():
            add_store('zcode', zcode_db, 'opencode_family', origin_path=str(zcode_db), can_open_history=True)

        xdg_data = Path(os.environ.get('XDG_DATA_HOME') or (home / '.local' / 'share')).expanduser()
        opencode_db = xdg_data / 'opencode' / 'opencode.db'
        if opencode_db.is_file():
            add_store('opencode', opencode_db, 'opencode_family', origin_path=str(opencode_db), can_open_history=True)

        hermes_db = home / '.hermes' / 'state.db'
        if hermes_db.is_file():
            add_store('hermes', hermes_db, 'hermes', origin_path=str(hermes_db), can_open_history=True)

    # 3. Optional read-only preserved historical caches (for rotated/older Codex & Claude recordings)
    if include_archives and data_dir is None:
        archive_candidates = [
            ('codex', xdg_cache / 'agenttracemesh' / 'weekly-automation' / 'machine-codex-1c51195e29edc56df27a8c2c1a7499248d023ca09204bbb7ebdd0fcbf4a1d52d' / 'index.sqlite'),
            ('codex', REPO_ROOT / 'index_linux.sqlite'),
            ('claude', REPO_ROOT / 'index_linux_claude.sqlite'),
        ]
        for src, arch_db in archive_candidates:
            if arch_db.is_file():
                add_store(src, arch_db, 'indexed', is_archive=True, origin_path=str(arch_db), can_open_history=False)

    for (source, origin), aliases in physical.items():
        matches = [s for s in stores if (s['source'], s['origin_path']) == (source, origin)]
        slot = _slot_from_meta(origin) or aliases[0]
        if marker_stores and not matches:
            directory = reader_root / ('machine-' + source + '-' + hashlib.sha256(origin.encode()).hexdigest())
            if source == 'antigravity':
                from .agy_transcripts import cache_path
                db_path = cache_path(directory, Path(origin))
            else:
                db_path = directory / 'index.sqlite'
            store = dict(store_key=_store_key('indexed', source, db_path, slot), source=source, kind='indexed',
                         slot=slot, origin_path=origin, db_path=str(db_path), cache_status='not_refreshed',
                         is_archive=False, capture_mode='strict', session_count=0, latest_ts_ms=None,
                         can_open_history=False)
            stores.append(store)
            matches = [store]
        for store in matches:
            names = list(dict.fromkeys([slot, *aliases]))
            if 'active' in names and source == 'codex':
                names.append('~/.codex/sessions')
            store.update(slot=slot, slot_aliases=names,
                         label=_source_display_label(source, slot))
    stores.sort(key=lambda item: (item['is_archive'], -item['session_count'], item['label']))
    return stores


def parse_omni_query(raw_query: str | None) -> dict:
    """Parse flexible search syntax supporting `|` / `OR` branches, space AND, `"phrases"`, and `-exclude`."""
    raw = str(raw_query or '').strip()
    if len(raw) > 400:
        raise ValueError('query_too_long')
    if not raw:
        return {'raw': '', 'branches': [], 'excludes': [], 'all_terms': []}

    excludes: list[str] = []
    branches: list[list[str]] = []
    raw_branches = re.split(r'\s*\|\s*|\s+OR\s+', raw)
    token_re = re.compile(r'(-?"[^"]+"|-?\'[^\']+\'|\S+)')

    for branch_text in raw_branches:
        branch_terms: list[str] = []
        for match in token_re.finditer(branch_text.strip()):
            tok = match.group(0)
            neg = tok.startswith('-') and len(tok) > 1
            if neg:
                tok = tok[1:]
            if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
                tok = tok[1:-1]
            tok = tok.strip()
            if not tok:
                continue
            if neg:
                if tok.lower() not in [e.lower() for e in excludes]:
                    excludes.append(tok)
            else:
                if tok.lower() not in [t.lower() for t in branch_terms]:
                    branch_terms.append(tok)
        if branch_terms:
            branches.append(branch_terms[:8])
    all_terms = list(dict.fromkeys(term for br in branches for term in br))
    return {'raw': raw, 'branches': branches[:6], 'excludes': excludes[:6], 'all_terms': all_terms[:16]}


def _extract_touched_files(raw_json: str | None, limit: int = 6) -> list[str]:
    if not raw_json:
        return []
    try:
        data = json.loads(raw_json)
    except (ValueError, TypeError):
        return []
    out: list[str] = []
    if isinstance(data, dict):
        for bucket in ('local', 'remote', 'inferred'):
            for entry in data.get(bucket) or []:
                path = entry.get('path') if isinstance(entry, dict) else (entry if isinstance(entry, str) else '')
                if path and path not in out:
                    out.append(str(path))
                    if len(out) >= limit:
                        return out
    elif isinstance(data, list):
        for item in data:
            path = item.get('path') if isinstance(item, dict) else (item if isinstance(item, str) else '')
            if path and path not in out:
                out.append(str(path))
                if len(out) >= limit:
                    return out
    return out


def _infer_topic(title: str, cwd: str, extra: str = '') -> str:
    haystack = f"{title} {cwd} {extra}"
    for key, spec in TOPIC_RULES.items():
        if spec['pattern'].search(haystack):
            return key
    return 'ai_agent'


def _deliverable_score(
    *,
    value_score: int | float | None,
    message_count: int,
    files_count: int,
    outcome: str | None,
    title: str,
    cwd: str,
) -> float:
    if SMOKE_NOISE_RE.search(f"{title} {cwd}"):
        return 1.0
    score = float(value_score or 0) * 0.65
    score += min(files_count * 7.5, 35.0)
    score += min(message_count * 0.45, 30.0)
    if outcome in ('completed', 'success', 'pass'):
        score += 12.0
    elif outcome in ('partial',):
        score += 5.0
    if cwd and cwd not in ('/', str(Path.home())):
        score += 4.0
    return round(min(score, 100.0), 1)


def _matches_parsed_query(haystack_lower: str, parsed: dict) -> tuple[bool, int]:
    for ex in parsed['excludes']:
        if ex.lower() in haystack_lower:
            return False, 0
    if not parsed['branches']:
        return True, 0
    best = 0
    matched_any = False
    for branch in parsed['branches']:
        if all(term.lower() in haystack_lower for term in branch):
            matched_any = True
            best = max(best, len(branch) * 4 + (6 if ' '.join(branch).lower() in haystack_lower else 0))
    return matched_any, best


def _clean_display_title(raw_title: str, fallback_text: str = '') -> str:
    t = str(raw_title or '').strip()
    ambient_prefixes = (
        '# Files mentioned by the user:',
        '# Files pasted by the user:',
        '# Context from my IDE setup:',
        '# AGENTS.md instructions for',
        '<environment_context>',
        '<USER_REQUEST>',
    )
    if not t or is_context_title(t) or t.startswith(ambient_prefixes):
        if fallback_text:
            # 1. Explicit `## My request:` section in Codex Desktop wrapper
            req_match = re.search(r'(?:#{1,4}\s*)?(?:My request(?: for Codex)?|用户请求|我的请求)\s*[:：]\s*\n+([^\n#][^\n]{2,120})', fallback_text, re.I)
            if req_match:
                return redact_text(req_match.group(1).strip())[:120]
            # 2. Standard harness stripper
            cleaned = meaningful_user_title(fallback_text, limit=110)
            if cleaned and not cleaned.startswith(ambient_prefixes):
                return redact_text(cleaned)
            # 3. Quoted paste preview `## "preview...": /path`
            paste_match = re.search(r'##\s+"([^"]{6,120})', fallback_text)
            if paste_match:
                preview = re.sub(r'^Worked for \d+m?\s*\d*s\s*', '', paste_match.group(1)).strip()
                if preview:
                    return redact_text(preview)[:110]
            # 4. First plain non-boilerplate line
            lines = [
                ln.strip() for ln in fallback_text.splitlines()
                if ln.strip()
                and not ln.strip().startswith((
                    '# Files ', '## ', '- /', '- ~', '<', '```',
                    'Distinguish instructions', 'Pasted text contains',
                ))
            ]
            if lines:
                return redact_text(lines[0][:110])
    return redact_text(t or '未命名会话')[:180]


def _query_indexed_store(
    store: dict,
    parsed: dict,
    *,
    project_sub: str = '',
    role: str = '',
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict]:
    db_path = Path(store['db_path'])
    public_pred = _indexed_public_message_sql('m')
    where = ['1=1']
    args: list = []

    if project_sub:
        where.append("COALESCE(s.cwd, '') LIKE ? ESCAPE '\\'")
        args.append(f"%{_escape_sql_like(project_sub)}%")
    if start_ms is not None:
        where.append('COALESCE(s.end_ts_ms, s.start_ts_ms, 0) >= ?')
        args.append(start_ms)
    if end_ms is not None:
        where.append('COALESCE(s.end_ts_ms, s.start_ts_ms, 0) <= ?')
        args.append(end_ms)

    # SQLite LIKE is ASCII case-insensitive natively; avoid LOWER(m.text) full-blob copies!
    if parsed['branches']:
        branch_sqls = []
        for branch in parsed['branches']:
            term_sqls = []
            for term in branch:
                pat = f"%{_escape_sql_like(term)}%"
                if role:
                    term_sqls.append(
                        f"((COALESCE(s.search_blob,'')='' OR s.search_blob LIKE ? ESCAPE '\\') "
                        f"AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id=s.id AND {public_pred} "
                        f"AND m.role=? AND COALESCE(m.text,'') LIKE ? ESCAPE '\\'))"
                    )
                    args.extend([pat, role, pat])
                else:
                    term_sqls.append(
                        f"(COALESCE(s.id,'') LIKE ? ESCAPE '\\' OR COALESCE(s.title,'') LIKE ? ESCAPE '\\' OR COALESCE(s.cwd,'') LIKE ? ESCAPE '\\' "
                        f"OR ((COALESCE(s.search_blob,'')='' OR s.search_blob LIKE ? ESCAPE '\\') "
                        f"AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id=s.id AND {public_pred} "
                        f"AND COALESCE(m.text,'') LIKE ? ESCAPE '\\')))"
                    )
                    args.extend([pat, pat, pat, pat, pat])
            branch_sqls.append('(' + ' AND '.join(term_sqls) + ')')
        where.append('(' + ' OR '.join(branch_sqls) + ')')
    elif role:
        where.append(f"EXISTS (SELECT 1 FROM messages m WHERE m.session_id=s.id AND {public_pred} AND m.role=?)")
        args.append(role)

    for ex in parsed['excludes']:
        pat = f"%{_escape_sql_like(ex)}%"
        where.append("COALESCE(s.title,'') NOT LIKE ? ESCAPE '\\' AND COALESCE(s.cwd,'') NOT LIKE ? ESCAPE '\\'")
        args.extend([pat, pat])

    sql = f"""
        SELECT s.id, s.title, s.cwd, s.start_ts_ms, s.end_ts_ms, s.message_count,
               s.files_touched_json, s.outcome_signal, s.value_score, s.action_density
        FROM sessions s
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(s.end_ts_ms, s.start_ts_ms, 0) DESC
        LIMIT 300
    """
    out: list[dict] = []
    with _ro_connect(db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
        for row in rows:
            files = _extract_touched_files(row['files_touched_json'])
            msg_count = int(row['message_count'] or 0)
            title = str(row['title'] or row['id'])
            cwd = str(row['cwd'] or '')
            d_score = _deliverable_score(
                value_score=row['value_score'],
                message_count=msg_count,
                files_count=len(files),
                outcome=row['outcome_signal'],
                title=title,
                cwd=cwd,
            )
            rel = 0
            if parsed['all_terms']:
                tl = f"{title} {cwd}".lower()
                for t in parsed['all_terms']:
                    if t.lower() in tl:
                        rel += 5
                rel += 3
            out.append({
                'store_key': store['store_key'],
                'source': store['source'],
                'source_label': store['label'],
                'slot': store['slot'],
                'is_archive': store['is_archive'],
                'can_open_history': store['can_open_history'],
                'id': str(row['id']),
                'title': redact_text(title)[:180],
                'project': cwd,
                'project_name': posixpath.basename(cwd.rstrip('/')) if cwd else '未绑定项目',
                'start_ts_ms': row['start_ts_ms'],
                'updated_at': row['end_ts_ms'] or row['start_ts_ms'],
                'message_count': msg_count,
                'value_score': int(row['value_score'] or 0),
                'deliverable_score': d_score,
                'outcome_signal': row['outcome_signal'] or 'unknown',
                'files_touched': files,
                'relevance': rel,
                'topic': _infer_topic(title, cwd, ' '.join(files)),
            })
    return out


def _query_opencode_family_store(
    store: dict,
    parsed: dict,
    *,
    project_sub: str = '',
    role: str = '',
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict]:
    db_path = Path(store['db_path'])
    where = ['1=1']
    args: list = []

    if project_sub:
        where.append("COALESCE(s.directory, '') LIKE ? ESCAPE '\\'")
        args.append(f"%{_escape_sql_like(project_sub)}%")
    if start_ms is not None:
        where.append('COALESCE(s.time_updated, s.time_created, 0) >= ?')
        args.append(start_ms)
    if end_ms is not None:
        where.append('COALESCE(s.time_updated, s.time_created, 0) <= ?')
        args.append(end_ms)

    if parsed['branches']:
        branch_sqls = []
        for branch in parsed['branches']:
            term_sqls = []
            for term in branch:
                pat = f"%{_escape_sql_like(term)}%"
                if role:
                    term_sqls.append(
                        "EXISTS (SELECT 1 FROM message m JOIN part p ON p.message_id=m.id "
                        "WHERE m.session_id=s.id AND json_valid(m.data) AND json_extract(m.data,'$.role')=? "
                        "AND json_valid(p.data) AND COALESCE(json_extract(p.data,'$.type'),'') IN ('text','tool') "
                        "AND p.data LIKE ? ESCAPE '\\')"
                    )
                    args.extend([role, pat])
                else:
                    term_sqls.append(
                        "(COALESCE(s.id,'') LIKE ? ESCAPE '\\' OR COALESCE(s.title,'') LIKE ? ESCAPE '\\' OR COALESCE(s.directory,'') LIKE ? ESCAPE '\\' "
                        "OR EXISTS (SELECT 1 FROM part p WHERE p.session_id=s.id AND json_valid(p.data) "
                        "AND COALESCE(json_extract(p.data,'$.type'),'') IN ('text','tool') AND p.data LIKE ? ESCAPE '\\'))"
                    )
                    args.extend([pat, pat, pat, pat])
            branch_sqls.append('(' + ' AND '.join(term_sqls) + ')')
        where.append('(' + ' OR '.join(branch_sqls) + ')')

    for ex in parsed['excludes']:
        pat = f"%{_escape_sql_like(ex)}%"
        where.append("COALESCE(s.title,'') NOT LIKE ? ESCAPE '\\' AND COALESCE(s.directory,'') NOT LIKE ? ESCAPE '\\'")
        args.extend([pat, pat])

    with _ro_connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session)").fetchall()}
        outcome_expr = 's.last_outcome' if 'last_outcome' in cols else "NULL"
        files_expr = 'COALESCE(s.summary_files, 0)' if 'summary_files' in cols else '0'
        adds_expr = 'COALESCE(s.summary_additions, 0)' if 'summary_additions' in cols else '0'
        sql = f"""
            SELECT s.id, s.title, s.directory AS cwd, s.time_created AS start_ts_ms,
                   s.time_updated AS end_ts_ms, {files_expr} AS summary_files,
                   {adds_expr} AS summary_additions, {outcome_expr} AS outcome_signal,
                   (SELECT COUNT(*) FROM message m WHERE m.session_id=s.id) AS message_count
            FROM session s
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(s.time_updated, s.time_created, 0) DESC
            LIMIT 300
        """
        rows = conn.execute(sql, args).fetchall()

    out: list[dict] = []
    for row in rows:
        title = str(row['title'] or row['id'])
        cwd = str(row['cwd'] or '')
        msg_count = int(row['message_count'] or 0)
        s_files = int(row['summary_files'] or 0)
        s_adds = int(row['summary_additions'] or 0)
        synth_val = min(95, 25 + s_files * 8 + min(msg_count * 2, 35) + (15 if s_adds > 20 else 0)) if (s_files or msg_count >= 4) else min(30, msg_count * 5)
        d_score = _deliverable_score(
            value_score=synth_val,
            message_count=msg_count,
            files_count=s_files,
            outcome=row['outcome_signal'],
            title=title,
            cwd=cwd,
        )
        rel = 0
        if parsed['all_terms']:
            tl = f"{title} {cwd}".lower()
            for t in parsed['all_terms']:
                if t.lower() in tl:
                    rel += 5
            rel += 3
        files_label = [f"变更 {s_files} 个文件 (+{s_adds} 行)"] if s_files > 0 else []
        out.append({
            'store_key': store['store_key'],
            'source': store['source'],
            'source_label': store['label'],
            'slot': store['slot'],
            'is_archive': store['is_archive'],
            'can_open_history': store['can_open_history'],
            'id': str(row['id']),
            'title': redact_text(title)[:180],
            'project': cwd,
            'project_name': posixpath.basename(cwd.rstrip('/')) if cwd else '未绑定项目',
            'start_ts_ms': row['start_ts_ms'],
            'updated_at': row['end_ts_ms'] or row['start_ts_ms'],
            'message_count': msg_count,
            'value_score': synth_val,
            'deliverable_score': d_score,
            'outcome_signal': row['outcome_signal'] or ('completed' if s_files > 0 else 'unknown'),
            'files_touched': files_label,
            'relevance': rel,
            'topic': _infer_topic(title, cwd),
        })
    return out


def _enrich_item_snippets(store_map: dict[str, dict], item: dict, terms: list[str], role: str = '') -> None:
    store = store_map.get(item['store_key'])
    if not store:
        item['snippets'] = []
        item['user_goal'] = ''
        return
    db_path = Path(store['db_path'])
    snippets: list[dict] = []
    user_goal = ''
    try:
        with _ro_connect(db_path) as conn:
            if store['kind'] == 'indexed':
                pub = _indexed_public_message_sql('m')
                mcols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
                idx_expr = 'COALESCE(m.message_index, 0)' if 'message_index' in mcols else '0'
                goal_rows = conn.execute(
                    f"SELECT substr(COALESCE(m.text,''), 1, 900) AS txt FROM messages m "
                    f"WHERE m.session_id=? AND {pub} AND m.role='user' AND length(trim(COALESCE(m.text,''))) > 6 "
                    f"ORDER BY m.ts_ms ASC, m.id ASC LIMIT 3",
                    (item['id'],),
                ).fetchall()
                for gr in goal_rows:
                    cand_txt = str(gr['txt'] or '').strip()
                    if cand_txt:
                        cleaned_title = _clean_display_title(item['title'], cand_txt)
                        if cleaned_title and cleaned_title != item['title']:
                            item['title'] = cleaned_title
                        user_goal = redact_text(cand_txt)[:240]
                        if not cand_txt.startswith(('# AGENTS.md', '<environment_context>')):
                            break

                if terms:
                    likes = ' OR '.join("COALESCE(m.text,'') LIKE ? ESCAPE '\\'" for _ in terms[:6])
                    params: list = [item['id']]
                    role_sql = ''
                    if role:
                        role_sql = ' AND m.role=?'
                        params.append(role)
                    params.extend(f"%{_escape_sql_like(t)}%" for t in terms[:6])
                    rows = conn.execute(
                        f"SELECT {idx_expr} AS msg_idx, m.role, m.kind, substr(COALESCE(m.text,''), 1, 2400) AS txt "
                        f"FROM messages m "
                        f"WHERE m.session_id=? AND {pub}{role_sql} AND ({likes}) "
                        f"ORDER BY CASE WHEN m.role='user' THEN 1 WHEN m.role='assistant' THEN 2 ELSE 3 END, m.ts_ms DESC LIMIT 3",
                        params,
                    ).fetchall()
                    for r in rows:
                        txt = str(r['txt'] or '')
                        pos = 0
                        low = txt.lower()
                        for t in terms:
                            idx = low.find(t.lower())
                            if idx >= 0:
                                pos = max(0, idx - 60)
                                break
                        snippets.append({
                            'message_index': int(r['msg_idx'] or 0),
                            'role': r['role'],
                            'text': redact_text(txt[pos:pos + 220].strip()),
                        })
                else:
                    tail_row = conn.execute(
                        f"SELECT {idx_expr} AS msg_idx, substr(COALESCE(m.text,''), 1, 320) AS txt FROM messages m "
                        f"WHERE m.session_id=? AND {pub} AND m.role='assistant' AND length(trim(COALESCE(m.text,''))) > 20 "
                        f"ORDER BY m.ts_ms DESC, m.id DESC LIMIT 1",
                        (item['id'],),
                    ).fetchone()
                    if tail_row and tail_row['txt']:
                        snippets.append({
                            'message_index': int(tail_row['msg_idx'] or max(0, item['message_count'] - 1)),
                            'role': 'assistant',
                            'text': redact_text(tail_row['txt'].strip())[:220],
                        })
            elif store['kind'] == 'opencode_family':
                rows = conn.execute(
                    "SELECT json_extract(m.data,'$.role') AS role, json_extract(p.data,'$.type') AS ptype, "
                    "COALESCE(json_extract(p.data,'$.text'), json_extract(p.data,'$.tool'), '') AS txt "
                    "FROM message m JOIN part p ON p.message_id=m.id "
                    "WHERE m.session_id=? AND json_valid(m.data) AND json_valid(p.data) "
                    "AND COALESCE(json_extract(p.data,'$.type'),'') IN ('text','tool') "
                    "ORDER BY m.time_created ASC, p.time_created ASC LIMIT 240",
                    (item['id'],),
                ).fetchall()
                for idx_num, r in enumerate(rows):
                    txt = str(r['txt'] or '').strip()
                    if not user_goal and r['role'] == 'user' and len(txt) > 6:
                        user_goal = redact_text(txt[:240])
                    if terms and len(snippets) < 3:
                        if role and r['role'] != role:
                            continue
                        low = txt.lower()
                        hit_positions = [low.find(t.lower()) for t in terms if low.find(t.lower()) >= 0]
                        if hit_positions:
                            start = max(0, min(hit_positions) - 60)
                            snippets.append({
                                'message_index': idx_num,
                                'role': r['role'] or 'assistant',
                                'text': redact_text(txt[start:start + 220]),
                            })
                if not terms and rows:
                    for idx_num in range(len(rows) - 1, -1, -1):
                        r = rows[idx_num]
                        txt = str(r['txt'] or '').strip()
                        if r['role'] == 'assistant' and len(txt) > 20:
                            snippets.append({
                                'message_index': idx_num,
                                'role': 'assistant',
                                'text': redact_text(txt[:220]),
                            })
                            break
    except sqlite3.Error:
        pass

    item['user_goal'] = user_goal
    item['snippets'] = snippets


def omni_search(
    *,
    query: str = '',
    source: str = '',
    slot: str = '',
    project: str = '',
    topic: str = '',
    role: str = '',
    sort: str = 'value',
    only_accomplished: bool = False,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 24,
    offset: int = 0,
    demo: bool = False,
    data_dir: Path | str | None = None,
    enrich: bool = True,
    include_archives: bool = False,
) -> dict:
    """Unified cross-source, cross-slot search & zero-keyword accomplishment browser."""
    started = time.monotonic()
    limit = max(1, min(int(limit or 24), 1000))
    offset = max(0, min(int(offset or 0), 10000))
    if role and role not in ('user', 'assistant', 'tool'):
        raise ValueError('invalid_role')
    if topic and topic not in TOPIC_RULES:
        raise ValueError('invalid_topic')
    if sort not in ('value', 'relevance', 'recent', 'messages'):
        sort = 'value'

    parsed = parse_omni_query(query)
    stores = discover_omni_stores(data_dir, demo=demo, include_archives=include_archives)
    store_map = {s['store_key']: s for s in stores}

    canon_src = canonical_source(source) if source else ''
    selected_stores = [
        s for s in stores
        if (not canon_src or s['source'] == canon_src or s['store_key'] == source)
        and (not slot or s['slot'] == slot)
    ]

    deduped: dict[tuple[str, str], dict] = {}
    store_errors: list[dict] = []

    for st in selected_stores:
        try:
            if st['kind'] == 'indexed':
                candidates = _query_indexed_store(
                    st, parsed, project_sub=project.strip(), role=role, start_ms=start_ms, end_ms=end_ms
                )
            elif st['kind'] == 'opencode_family':
                candidates = _query_opencode_family_store(
                    st, parsed, project_sub=project.strip(), role=role, start_ms=start_ms, end_ms=end_ms
                )
            else:
                candidates = []
            for cand in candidates:
                if topic and cand['topic'] != topic:
                    continue
                if only_accomplished:
                    if SMOKE_NOISE_RE.search(f"{cand['title']} {cand['project']}"):
                        continue
                    if cand['message_count'] < 4 and not cand['files_touched'] and cand['value_score'] < 25:
                        continue
                key = (cand['source'], cand['id'])
                prev = deduped.get(key)
                if prev is None or (prev['is_archive'] and not cand['is_archive']) or (
                    cand['message_count'] > prev['message_count'] and prev['is_archive'] == cand['is_archive']
                ):
                    deduped[key] = cand
        except (OSError, sqlite3.Error, ValueError) as exc:
            store_errors.append({'store_key': st['store_key'], 'source': st['source'], 'error': str(exc)[:120]})

    all_items = list(deduped.values())
    if sort == 'relevance' and parsed['all_terms']:
        all_items.sort(key=lambda x: (-x['relevance'], -x['deliverable_score'], -(x['updated_at'] or 0)))
    elif sort == 'recent':
        all_items.sort(key=lambda x: (-(x['updated_at'] or 0), -x['deliverable_score']))
    elif sort == 'messages':
        all_items.sort(key=lambda x: (-x['message_count'], -x['deliverable_score'], -(x['updated_at'] or 0)))
    else:
        # Default: if query supplied, blend relevance + deliverable_score; otherwise pure deliverable_score
        if parsed['all_terms']:
            all_items.sort(key=lambda x: (-(x['relevance'] * 3 + x['deliverable_score']), -(x['updated_at'] or 0)))
        else:
            all_items.sort(key=lambda x: (-x['deliverable_score'], -(x['updated_at'] or 0)))

    total = len(all_items)
    page_items = all_items[offset:offset + limit]
    if enrich:
        for item in page_items:
            _enrich_item_snippets(store_map, item, parsed['all_terms'], role=role)

    return {
        'items': page_items,
        'total': total,
        'offset': offset,
        'limit': limit,
        'has_more': offset + limit < total,
        'next_offset': offset + limit if offset + limit < total else None,
        'query_parsed': parsed,
        'stores_queried': len(selected_stores),
        'errors': store_errors,
        'partial': bool(store_errors),
        'elapsed_ms': int((time.monotonic() - started) * 1000),
    }


def omni_session_messages(
    store_key: str,
    session_id: str,
    *,
    offset: int = 0,
    limit: int = 80,
    query: str = '',
    demo: bool = False,
    data_dir: Path | str | None = None,
) -> dict:
    """Read public, redacted messages of any discovered session (including inactive AccountMesh slots)."""
    if not store_key or not session_id:
        raise ValueError('store_and_session_required')
    limit = max(1, min(int(limit or 80), 200))
    offset = max(0, min(int(offset or 0), 50000))
    stores = discover_omni_stores(data_dir, demo=demo, include_archives=True)
    store = next((s for s in stores if s['store_key'] == store_key), None)
    if store is None:
        raise ValueError('store_not_found')

    db_path = Path(store['db_path'])
    messages: list[dict] = []
    meta: dict = {'id': session_id, 'source': store['source'], 'source_label': store['label'], 'slot': store['slot']}
    total = 0

    with _ro_connect(db_path) as conn:
        if store['kind'] == 'indexed':
            srow = conn.execute(
                "SELECT id, title, cwd, start_ts_ms, end_ts_ms, message_count, files_touched_json, outcome_signal, value_score "
                "FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if srow is None:
                raise ValueError('session_not_found')
            meta.update({
                'title': redact_text(str(srow['title'] or session_id)),
                'project': str(srow['cwd'] or ''),
                'updated_at': srow['end_ts_ms'] or srow['start_ts_ms'],
                'files_touched': _extract_touched_files(srow['files_touched_json'], limit=12),
                'outcome_signal': srow['outcome_signal'] or 'unknown',
                'value_score': int(srow['value_score'] or 0),
            })
            pub = _indexed_public_message_sql('m')
            where_q = ''
            params: list = [session_id]
            if query.strip():
                where_q = " AND LOWER(COALESCE(m.text,'')) LIKE ?"
                params.append(f"%{query.strip().lower()}%")
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM messages m WHERE m.session_id=? AND {pub}{where_q}",
                params,
            ).fetchone()[0])
            rows = conn.execute(
                f"WITH ordered AS ("
                f"  SELECT id, ROW_NUMBER() OVER (ORDER BY ts_ms ASC, id ASC) - 1 AS msg_idx "
                f"  FROM messages WHERE session_id=?"
                f") "
                f"SELECT o.msg_idx, m.role, m.kind, m.ts_ms, substr(COALESCE(m.text,''), 1, 6000) AS txt "
                f"FROM ordered o JOIN messages m ON m.id=o.id "
                f"WHERE {pub}{where_q} ORDER BY o.msg_idx ASC LIMIT ? OFFSET ?",
                [session_id, *params[1:], limit, offset],
            ).fetchall()
            for r in rows:
                messages.append({
                    'message_index': int(r['msg_idx']),
                    'role': r['role'],
                    'kind': r['kind'] or 'message',
                    'ts_ms': r['ts_ms'],
                    'text': redact_text(str(r['txt'] or '')),
                })
        elif store['kind'] == 'opencode_family':
            srow = conn.execute(
                "SELECT id, title, directory AS cwd, time_created, time_updated FROM session WHERE id=?",
                (session_id,),
            ).fetchone()
            if srow is None:
                raise ValueError('session_not_found')
            meta.update({
                'title': redact_text(str(srow['title'] or session_id)),
                'project': str(srow['cwd'] or ''),
                'updated_at': srow['time_updated'] or srow['time_created'],
                'files_touched': [],
                'outcome_signal': 'unknown',
                'value_score': 0,
            })
            all_rows = conn.execute(
                "SELECT json_extract(m.data,'$.role') AS role, json_extract(p.data,'$.type') AS ptype, "
                "p.time_created AS ts_ms, "
                "COALESCE(json_extract(p.data,'$.text'), json_extract(p.data,'$.state.input'), json_extract(p.data,'$.tool'), '') AS txt "
                "FROM message m JOIN part p ON p.message_id=m.id "
                "WHERE m.session_id=? AND json_valid(m.data) AND json_valid(p.data) "
                "AND COALESCE(json_extract(p.data,'$.type'),'') IN ('text','tool') "
                "ORDER BY m.time_created ASC, p.time_created ASC LIMIT 3000",
                (session_id,),
            ).fetchall()
            filtered = []
            q_low = query.strip().lower()
            for idx_num, r in enumerate(all_rows):
                txt = str(r['txt'] or '')
                if q_low and q_low not in txt.lower():
                    continue
                filtered.append({
                    'message_index': idx_num,
                    'role': r['role'] or ('tool' if r['ptype'] == 'tool' else 'assistant'),
                    'kind': r['ptype'] or 'text',
                    'ts_ms': r['ts_ms'],
                    'text': redact_text(txt[:6000]),
                })
            total = len(filtered)
            messages = filtered[offset:offset + limit]
        else:
            raise ValueError('unsupported_store_kind')

    return {
        'session': meta,
        'items': messages,
        'total': total,
        'offset': offset,
        'limit': limit,
        'has_more': offset + limit < total,
        'next_offset': offset + limit if offset + limit < total else None,
        'privacy': 'public_messages_redacted',
    }


def omni_radar(
    *,
    topic: str = '',
    limit: int = 30,
    demo: bool = False,
    data_dir: Path | str | None = None,
    include_archives: bool = False,
) -> dict:
    """Surface real AI accomplishments grouped by project and deliverable density across all stores."""
    top_limit = max(1, min(int(limit or 30), 50))
    stores = discover_omni_stores(data_dir, demo=demo, include_archives=include_archives)
    store_map = {s['store_key']: s for s in stores}
    by_source: dict[str, int] = {}
    for st in stores:
        if not st['is_archive']:
            by_source[st['label']] = by_source.get(st['label'], 0) + st['session_count']

    # Metadata-only full scan across ALL accomplished sessions (< 50ms!), then enrich only top_limit
    full_scan = omni_search(
        query='',
        topic=topic,
        sort='value',
        only_accomplished=not demo,
        limit=1000,
        offset=0,
        demo=demo,
        data_dir=data_dir,
        enrich=False,
        include_archives=include_archives,
    )
    project_map: dict[str, dict] = {}
    topic_counts = {k: {'key': k, 'label': v['label'], 'count': 0} for k, v in TOPIC_RULES.items()}
    for item in full_scan['items']:
        tp = item.get('topic') or 'ai_agent'
        if tp in topic_counts:
            topic_counts[tp]['count'] += 1
        proj = item['project'] or '未绑定项目'
        grp = project_map.setdefault(proj, {
            'project': item['project'],
            'project_name': item['project_name'],
            'topic': tp,
            'topic_label': TOPIC_RULES.get(tp, {}).get('label', tp),
            'session_count': 0,
            'max_score': 0.0,
            'latest_at': 0,
            'sources': [],
            'highlights': [],
        })
        grp['session_count'] += 1
        grp['max_score'] = max(grp['max_score'], item['deliverable_score'])
        grp['latest_at'] = max(grp['latest_at'], item['updated_at'] or 0)
        if item['source_label'] not in grp['sources']:
            grp['sources'].append(item['source_label'])
        if len(grp['highlights']) < 4:
            grp['highlights'].append({
                'store_key': item['store_key'],
                'source': item['source'],
                'source_label': item['source_label'],
                'id': item['id'],
                'title': item['title'],
                'deliverable_score': item['deliverable_score'],
                'message_count': item['message_count'],
                'files_touched': item['files_touched'][:4],
                'updated_at': item['updated_at'],
            })

    clusters = sorted(
        project_map.values(),
        key=lambda g: (-min(g['session_count'], 8) * 4 - g['max_score'], -g['latest_at']),
    )[:30]

    top_sessions = [dict(it) for it in full_scan['items'][:top_limit]]
    for item in top_sessions:
        _enrich_item_snippets(store_map, item, [])

    return {
        'summary': {
            'stores_count': len(stores),
            'raw_sessions_across_stores': sum(s['session_count'] for s in stores),
            'accomplished_sessions': full_scan['total'],
            'active_projects': len(project_map),
            'by_source': by_source,
            'topics': list(topic_counts.values()),
        },
        'project_clusters': clusters,
        'top_sessions': top_sessions,
    }


def build_experience_gem_packet(entry: dict) -> str:
    """Build a ready-to-feed Markdown packet (`material.md`) for Gem 木乔公众号 (Slot 3: 个人经验)."""
    sop_lines = '\n'.join(f"- {step}" for step in (entry.get('reusable_sop') or []))
    ev = entry.get('evidence') or {}
    return (
        f"# 公众号每日第 3 篇 · 个人实战经验拆解素材包\n\n"
        f"- **拟定标题（≤32字硬限参考）**：{entry.get('title', '')}\n"
        f"- **赛道分类**：{TOPIC_RULES.get(entry.get('category', ''), {}).get('label', entry.get('category', ''))}\n"
        f"- **核心反差钩子**：{entry.get('hook', '')}\n"
        f"- **TraceMesh 原始会话溯源**：`{ev.get('source', '')}` | `{ev.get('session_id', '')}` | `{ev.get('date', '')}` | `{ev.get('project', '')}` ({ev.get('message_refs', '')})\n\n"
        f"---\n\n"
        f"## 槽位 1：真实卡点与起手现场（直接切入，禁 AI 套话）\n"
        f"{entry.get('problem', '')}\n\n"
        f"## 槽位 2：最终做成了什么（真实交付物与指标）\n"
        f"{entry.get('accomplishment', '')}\n\n"
        f"## 槽位 3：AI 协作架构与详细打通全过程（核心硬核干货）\n"
        f"{entry.get('process', '')}\n\n"
        f"## 槽位 4：读者拿走就能套用的可复用 SOP\n"
        f"{sop_lines}\n\n"
        f"## 槽位 5：诚实工程边界（防吹牛红线）\n"
        f"{entry.get('boundaries', '')}\n\n"
        f"---\n"
        f"## 写作指令约束（木乔文风）\n"
        f"1. 严禁出现“不仅仅是…更是…”、“范式转移”、“毋庸置疑”、“在这个…的时代”等 AI 腔词汇。\n"
        f"2. 以同行复盘视角直接切入痛点、真实命令/模块名与关键转折，保留真实版本号与误差/耗时数字。\n"
        f"3. 严格遵守“诚实工程边界”中列出的未验条件，不把局部跑通夸大为全自动通用神话。\n"
    )


def load_experience_vault(vault_path: Path | None = None, *, category: str = '', query: str = '') -> dict:
    path = Path(vault_path) if vault_path else DEFAULT_VAULT_PATH
    items: list[dict] = []
    meta: dict = {'updated_at': None, 'version': '1.0'}
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(payload, dict):
                items = list(payload.get('items') or [])
                meta = {k: v for k, v in payload.items() if k != 'items'}
            elif isinstance(payload, list):
                items = payload
        except (OSError, ValueError):
            items = []

    q_low = str(query or '').strip().lower()
    filtered: list[dict] = []
    main_focus_count = 0
    by_cat = {k: {'key': k, 'label': v['label'], 'count': 0, 'in_main_focus': k in MAIN_WECHAT_CATEGORIES} for k, v in TOPIC_RULES.items()}
    for entry in items:
        cat = entry.get('category') or 'ai_agent'
        if cat in by_cat:
            by_cat[cat]['count'] += 1
        if cat in MAIN_WECHAT_CATEGORIES:
            main_focus_count += 1
        if category == 'focus_main':
            if cat not in MAIN_WECHAT_CATEGORIES:
                continue
        elif category and cat != category:
            continue
        if q_low:
            blob = json.dumps(entry, ensure_ascii=False).lower()
            if not all(tok in blob for tok in q_low.split()):
                continue
        enriched = dict(entry)
        enriched['category_label'] = TOPIC_RULES.get(cat, {}).get('label', cat)
        enriched['in_main_focus'] = cat in MAIN_WECHAT_CATEGORIES
        enriched['gem_packet_md'] = build_experience_gem_packet(entry)
        filtered.append(enriched)

    return {
        'meta': meta,
        'total_all': len(items),
        'total_main_focus': main_focus_count,
        'total_filtered': len(filtered),
        'main_focus_categories': list(MAIN_WECHAT_CATEGORIES),
        'categories': list(by_cat.values()),
        'items': filtered,
    }


def _fetch_436_live_materials(*, demo: bool = False) -> dict:
    """Read-only loopback check against local 436 workbench tunnel (`127.0.0.1:8768`), if up."""
    if demo:
        return {'status': 'demo_isolated', 'collected_at': None, 'slot1_big_tech': [], 'slot2_hn_breaking': []}
    url = os.environ.get('TRACEMESH_WECHAT_WORKBENCH_URL', 'http://127.0.0.1:8768/api/materials')
    if not url.startswith(('http://127.0.0.1:', 'http://localhost:')):
        return {'status': 'non_loopback_disabled', 'collected_at': None, 'slot1_big_tech': [], 'slot2_hn_breaking': []}
    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read(768_000).decode('utf-8', 'replace'))
    except Exception:
        return {'status': 'tunnel_offline', 'collected_at': None, 'slot1_big_tech': [], 'slot2_hn_breaking': []}

    slot1: list[dict] = []
    slot2: list[dict] = []
    for raw in (data.get('items') or []):
        title = str(raw.get('original_title') or '').strip()
        src = str(raw.get('source_name') or '').strip()
        link = str((raw.get('link_check') or {}).get('resolved_url') or raw.get('original_url') or '').strip()
        heat = raw.get('heat_evidence') or {}
        heat_cat = str(heat.get('category') or '').strip()
        heat_info = str(heat.get('reported_reads') or '').strip()
        if not title or not link or len(title) < 12:
            continue
        if OFF_TRACK_MAIN_WECHAT_RE.search(f"{title} {link} {src}"):
            continue
        if re.search(r'/news/(?:announcements|partnerships)/?$|initial public offering|roadshow', f"{title} {link}", re.I):
            continue
        card = {
            'title': title,
            'source_name': f"{src} · {heat_info}" if heat_info and heat_info not in ('trending', '官方一手信源', 'FreebieRadar 精选信息源') else src,
            'url': link,
            'published_at': raw.get('published_at'),
        }
        if 'Slot 1' in heat_cat or (BIG_TECH_RE.search(f"{title} {link} {src}") and not HN_BREAKING_RE.search(f"{src} {link}")):
            slot1.append(card)
        elif 'Slot 2' in heat_cat or HN_BREAKING_RE.search(f"{src} {link}") or 'github.com' in link.lower():
            slot2.append(card)
        elif BIG_TECH_RE.search(f"{title} {link}"):
            slot1.append(card)
        else:
            slot2.append(card)

    return {
        'status': 'online',
        'collected_at': data.get('collected_at'),
        'slot1_big_tech': slot1[:20],
        'slot2_hn_breaking': slot2[:20],
    }


def wechat_studio(*, category: str = '', query: str = '', demo: bool = False, vault_path: Path | None = None) -> dict:
    """Unified payload for the Daily 3-Article WeChat Studio in TraceMesh."""
    vault = load_experience_vault(vault_path, category=category, query=query)
    live = _fetch_436_live_materials(demo=demo)
    return {
        'daily_plan': {
            'articles_per_day': 3,
            'slots': [
                {
                    'slot_id': 'slot1_big_tech',
                    'badge': '每日第 1 篇 · 大厂前沿更新',
                    'name': 'Google / OpenAI / Claude 一手更新',
                    'source_desc': '436 AIHOT (:8088) 官方工程博客、模型发布、SDK / CLI / 协议更新',
                    'count': len(live['slot1_big_tech']),
                },
                {
                    'slot_id': 'slot2_hn_breaking',
                    'badge': '每日第 2 篇 · HN 与爆炸新闻',
                    'name': 'Hacker News 高赞争议 / GitHub 爆款开源',
                    'source_desc': '436 NewsNow (:4444) HN 首页热帖、开发者高赞互搏、GitHub 暴涨项目拆解',
                    'count': len(live['slot2_hn_breaking']),
                },
                {
                    'slot_id': 'slot3_personal_exp',
                    'badge': '每日第 3 篇 · 我的个人经验',
                    'name': 'TraceMesh 全对话实战经验与 AI 成事库',
                    'source_desc': '本机全源 1,300+ 会话深挖：只要用 AI 做成一件事就是硬核独占弹药',
                    'count': vault['total_all'],
                },
            ],
        },
        'live_436': live,
        'experience_vault': vault,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python3 -m history_core omni',
        description='Cross-source & multi-slot omni discovery, flexible search, accomplishment radar, and experience vault.',
    )
    parser.add_argument('--data-dir', type=Path, help='Optional reader cache directory override')
    parser.add_argument('--demo', action='store_true', help='Restrict to isolated synthetic demo data')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('stores', help='List all discovered local conversation stores, AccountMesh slots, and session counts')
    markers = sub.add_parser('markers', help='Aggregate recorded events and covered active sessions across slots')
    markers.add_argument('--from', dest='start', required=True)
    markers.add_argument('--to', dest='end', required=True)
    markers.add_argument('--timezone', default='Asia/Shanghai')
    markers.add_argument('--sources', default='all', help='Comma-separated source names or all')
    markers.add_argument('--slots', default='all', help='Comma-separated slots or all')
    markers.add_argument('--kind', choices=('skill', 'output_style'), default='skill')
    markers.add_argument('--event')
    markers.add_argument('--group-by', default='skill,source,event')
    markers.add_argument('--refresh', action='store_true')
    markers.add_argument('--review-misses', nargs='?', const='studio', choices=('studio', 'all'))
    markers.add_argument('--limit', type=int, default=100)
    markers.add_argument('--offset', type=int, default=0)
    markers.add_argument('--index-revision')

    sp_search = sub.add_parser('search', help='Search across all sources & slots (supports empty query, `|` OR, `-exclude`, substring project)')
    sp_search.add_argument('--query', '-q', default='')
    sp_search.add_argument('--source', default='')
    sp_search.add_argument('--slot', default='')
    sp_search.add_argument('--project', default='', help='Case-insensitive project path substring')
    sp_search.add_argument('--topic', default='', choices=('', *TOPIC_RULES.keys()))
    sp_search.add_argument('--role', default='', choices=('', 'user', 'assistant', 'tool'))
    sp_search.add_argument('--sort', default='value', choices=('value', 'relevance', 'recent', 'messages'))
    sp_search.add_argument('--accomplished', action='store_true', help='Filter to substantive sessions with deliverables/depth')
    sp_search.add_argument('--include-archives', action='store_true', help='Also scan preserved legacy archive SQLite files')
    sp_search.add_argument('--limit', type=int, default=20)
    sp_search.add_argument('--offset', type=int, default=0)

    sp_radar = sub.add_parser('radar', help='Surface accomplished AI sessions & project clusters across all engines')
    sp_radar.add_argument('--topic', default='', choices=('', *TOPIC_RULES.keys()))
    sp_radar.add_argument('--include-archives', action='store_true', help='Also include preserved legacy archive SQLite files')
    sp_radar.add_argument('--limit', type=int, default=25)

    sp_msg = sub.add_parser('messages', help='Read public redacted messages from any store_key and session_id')
    sp_msg.add_argument('store_key')
    sp_msg.add_argument('session_id')
    sp_msg.add_argument('--query', default='')
    sp_msg.add_argument('--limit', type=int, default=50)
    sp_msg.add_argument('--offset', type=int, default=0)

    sp_vault = sub.add_parser('vault', help='Query the curated Personal Experience Vault for WeChat Slot 3')
    sp_vault.add_argument('--category', default='', choices=('', 'focus_main', *TOPIC_RULES.keys()))
    sp_vault.add_argument('--query', '-q', default='')

    args = parser.parse_args(argv)
    if args.command == 'stores':
        stores = discover_omni_stores(args.data_dir, demo=args.demo, marker_stores=True)
        out = {
            'stores': stores,
            'total_stores': len(stores),
            'total_sessions': sum(s['session_count'] for s in stores),
            'observed_at': datetime.now(timezone.utc).isoformat(),
        }
    elif args.command == 'markers':
        out = omni_markers(**{key: value for key, value in vars(args).items() if key != 'command'})
    elif args.command == 'search':
        out = omni_search(
            query=args.query,
            source=args.source,
            slot=args.slot,
            project=args.project,
            topic=args.topic,
            role=args.role,
            sort=args.sort,
            only_accomplished=args.accomplished,
            limit=args.limit,
            offset=args.offset,
            demo=args.demo,
            data_dir=args.data_dir,
            include_archives=args.include_archives,
        )
    elif args.command == 'radar':
        out = omni_radar(
            topic=args.topic, limit=args.limit, demo=args.demo,
            data_dir=args.data_dir, include_archives=args.include_archives,
        )
    elif args.command == 'messages':
        out = omni_session_messages(
            args.store_key, args.session_id, offset=args.offset, limit=args.limit,
            query=args.query, demo=args.demo, data_dir=args.data_dir,
        )
    else:
        out = load_experience_vault(category=args.category, query=args.query)

    print(json.dumps(out, ensure_ascii=False))
    return 0
