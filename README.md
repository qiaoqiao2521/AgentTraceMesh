# AgentTraceMesh

Repository: [qiaoqiao2521/AgentTraceMesh](https://github.com/qiaoqiao2521/AgentTraceMesh).

Formerly **Codex & Claude History Viewer**. AgentTraceMesh focuses on local Agent conversation records: find conversations, trace original evidence, and carry useful context into the next task. It is independent of [OpsPAI/TraceMesh](https://github.com/OpsPAI/TraceMesh). Existing `cchv` commands, repository paths, data directories and preference keys remain compatible.

**Agent integration comes first:** retrieve work evidence, recover original requirements and later corrections, then hand selected context to an Agent for independent code review. The independent human trial (U1/M3) was cancelled by the user on 2026-10-01; it is no longer a release gate. Web reading and export previews remain available.

**先实现，再独立验收。** 可以先用一般/低价模型完成主要代码，再用 AgentTraceMesh 找回你的需求原文、后续纠正和实现线索，交给高级模型在当前仓库逐项审查、提出必要优化。减少换模型时重讲背景的负担，让验收有原始需求可查。

点击 **准备验收交接** → 搜索并选择用户完整消息及相关证据 → 预览、复制验收包 → 交给高级模型。CBC、AGY、mcode 和 ZCode 均可在支持的本地记录范围内选择原始需求与后续修订。验收包附带逐项核对提示词；AgentTraceMesh 不自动调用模型或判定完成。历史“测试通过”仍需结合当前代码核对。[使用步骤与边界](docs/model-review-handoff.md)

Local-first, dependency-free web viewer for **Codex CLI**, **Claude Code**, **CodeBuddy (cbc)**, **Gemini CLI**, **pi / Prime Agent**, **GitHub Copilot**, **ZCode**, **AGY CLI / Antigravity**, **OpenCode**, **OpenClaw**, and **Hermes** session history. Format-specific limits are shown explicitly.

- Start with a project, then read conversations across Agent sources in one place
- Retrieve editable [problem cases](docs/problem-cases.md): current judgment, corrections and next-task methods, with attributed evidence and explicit unknowns
- Expand related-title groups without losing conversations; open revision-bound key-message hints for requests, proposals, failures and verification mentions
- Adjust themes, text size, message visibility and source refresh in Settings
- Search across sources by keyword, project and date; open matching messages with revision checks
- Follow cross-source project/file history, inspect recorded patches and retain earlier failures
- Select up to 5 evidence fragments for a revision-bound, masked Markdown/JSON handoff
- Prepare a requirements-first review handoff for another model, keeping original user requests separate from historical completion claims
- Inspect local source status and preview a sanitized diagnostic export
- Filter messages by role, and search within a session (highlight + next/prev)
- Sort sidebar by **start time**, **last activity**, or **value signal**
- Audit badges per session: files touched, tool count, remote/test/deploy/debug activity, friction, outcome, value score
- **⚡ Usage** panel: provider-reported token totals per day / project / session where available
- **📰 Briefing** panel: daily work summary with highlights, blocked sessions, deliverables, and an optional LLM narrative
- **🗒 Plans** panel: planning files (`task_plan.md` / `progress.md` / `findings.md`) near the session, read-only
- Copy compact or standard agent handoffs with intent, constraints, changes, verification, remaining work, and evidence references — plus a themed markdown preview, rich-text copy, and `.md` / `.html` export
- Highlight interruptions and common error outputs

> Not affiliated with OpenAI or Anthropic. “Codex” and “Claude” are trademarks of their respective owners.

![Codex UI screenshot](docs/codex.png)

<details>
<summary>More screenshots</summary>

![Claude Code UI screenshot](docs/claudecode.png)

</details>

## Why

Codex CLI and Claude Code both generate local, machine-readable transcripts (JSONL). This project turns those logs into a fast, searchable “history inbox” you can use to:

- find past commands/patches/discussions quickly
- audit tool failures and interruptions
- compare sessions across days/projects
- keep everything **local** (no uploads by default)

Current checkout: **1.3.0-rc.3 candidate** with [project-first reading and settings](plans/project-reader-ux/progress.md). [Local CLI source expansion](plans/local-cli-sources/progress.md) remains included. Reuse engineering acceptance and current Agent-first policy: [history reuse plan](plans/history-reuse-product/progress.md). No independent human trial is pending; its cancellation does not claim human usability validation or publish a new release. Stable v1.2.0 remains available.

## Quick start

Requirements:

- Python 3.11+ (standard library only)
- A modern browser

Run:

```bash
python3 app.py
```

Open:

- http://127.0.0.1:8787

By default it reads:

- Codex logs: `~/.codex/sessions`
- Claude logs: `~/.claude/projects`
- OpenClaw logs: `~/.openclaw/agents`
- OpenCode state DB: auto-detected from `~/.local/share/opencode/opencode.db`
- Hermes state DB: auto-detected from `~/.hermes/state.db` or `../hermes-agent/.hermes-home/state.db`

Linux also discovers CodeBuddy, Gemini, pi/Prime, Copilot (including Snap), ZCode and AGY/Antigravity history. See [local CLI paths, configuration and capability limits](docs/local-cli-sources.md).

Indexes (SQLite) are stored next to `app.py` (the repo folder) unless you set `--data-dir`.

## Runtime systems

- **Windows** runtime exposes `Windows` and `WSL`
- **Linux / Ubuntu** runtime exposes `Linux`

The UI follows the runtime platform instead of assuming Windows-only tabs.

## Configuration

Show all options:

```bash
python3 app.py --help
```

## Documentation

- [Project context](PROJECT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Decisions](docs/DECISIONS.md)
- [Active work](plans/)

## Demo data (included)

This repo includes a small set of **synthetic** Codex/Claude logs under `demo/` so you can try the UI without using your own transcripts.

```bash
python3 app.py --demo --data-dir ./demo/.data
```

Common examples:

```bash
# Store indexes outside the repo (recommended)
python3 app.py --data-dir ~/.cache/cchv

# Bind to LAN (be careful: your logs may contain secrets)
python3 app.py --host 0.0.0.0 --port 8787

# Custom log locations (these are the *base dirs* that contain `sessions/` and `projects/`)
python3 app.py --codex-dir ~/.codex --claude-dir ~/.claude --openclaw-dir ~/.openclaw

# Explicit OpenCode state DB (optional; otherwise auto-detected)
python3 app.py --opencode-state-db ~/.local/share/opencode/opencode.db

# Explicit Hermes DB path (optional; otherwise auto-detected on Linux)
python3 app.py --hermes-state-db ~/.hermes/state.db

# Faster/slower auto-rescan (seconds)
python3 app.py --scan-interval 2
```

### Ubuntu / Linux

Run directly:

```bash
python3 app.py \
  --codex-dir ~/.codex \
  --claude-dir ~/.claude \
  --openclaw-dir ~/.openclaw \
  --opencode-state-db ~/.local/share/opencode/opencode.db \
  --data-dir ~/.cache/cchv \
  --host 127.0.0.1 \
  --port 8787
```

Or use the launcher:

```bash
chmod +x ./scripts/start-cchv.sh
./scripts/start-cchv.sh
```

The launcher now probes the repo path automatically in this order:

- explicit `REPO_DIR` / `CCHV_REPO_DIR`
- repo-local launch from `scripts/`
- `CCHV_WORKSPACE_ROOT` with the repo under `repos/AgentTraceMesh`
- nearby workspace paths such as `./repos/AgentTraceMesh`
- user-level fallbacks such as `~/.ductor/workspace/repos/AgentTraceMesh`

Legacy directory names `TraceMesh`, `tracemesh`, and `Codex-Claude-History-Viewer` remain supported.

Install a desktop entry on Ubuntu:

```bash
chmod +x ./scripts/install-linux-desktop-entry.sh
./scripts/install-linux-desktop-entry.sh
```

That writes:

- `~/.local/share/applications/codex-claude-history-viewer.desktop`

## Using the UI

- **System**: switch between the runtime systems that are actually available.
- **Source**: switch between Codex / Claude Code / OpenClaw / OpenCode / Hermes history. OpenCode and Hermes are read-only sources backed by their own SQLite state DBs.
- **Browse**:
  - **Sessions**: list individual sessions.
  - **Projects**: group sessions by working directory (cwd), or by Hermes/OpenCode source for those sources.
- **Sidebar sort**: top-right dropdown in the sessions list:
  - `Start time` (default)
  - `Last activity`
  - `Value signal` — sessions with higher audit value score first (only meaningful for JSONL-backed sources: Codex / Claude Code / OpenClaw)
- **Audit badges** (JSONL sources only): each session row shows compact chips summarising what the agent actually did — files touched, tool calls, remote/test/deploy/debug activity, friction (errors + retries + interrupts), outcome (✓ completed / ✗ errored / ⏸ interrupted / ? unknown), and a 0–100 value score. Hover any chip for details.
- **Roles**: toggle user/assistant/system/developer/tool/other.
- **Search**
  - Left panel: keyword search across sessions + optional date range.
  - Right panel: search within the opened session (highlight + ▲/▼ navigation).
- **Error highlighting**
  - User interruptions (e.g. `turn_aborted`) are highlighted.
  - Common tool failures (HTTP 4xx/5xx, Traceback/Exception, `Status: error`, etc.) are highlighted.
- **Insights row** (below the audit actions): **⚡ Usage** opens a token-usage
  dashboard for the whole source with a range selector (7 days / 30 days /
  all time); **📰 Briefing** opens a date-scoped work summary (highlights,
  blocked sessions, deliverables, optional 🤖 LLM narrative); **🗒 Plans**
  lists planning files (`task_plan.md` / `progress.md` / `findings.md`,
  `docs/session-plans/*.md`) found near the current session's working
  directory. Clicking a usage/briefing row opens that session.
- **Handoff extras**: **👁 Preview** renders the handoff as themed markdown
  (plain / card / Feishu card — theme persists), **📋 Rich** copies it as
  rich text, **⬇ .md** / **⬇ .html** download standalone files.

> Note: token usage for Codex/Claude is extracted during indexing (parser
> v5/v4). On the first start after upgrading, existing sessions are re-parsed
> once to backfill the usage columns.

## Privacy & safety notes

- Your local transcripts may contain sensitive info (API keys, file paths, proprietary code).
- If you bind `--host 0.0.0.0`, anyone on your network may be able to access the UI. Prefer `127.0.0.1`.
- Index files are local SQLite databases; this repo’s `.gitignore` excludes them.

## Status

`v1.1.0` adds the shared history core, explicit headless handoff CLI and human work overview alongside the prior candidate features. Real multi-device continuation and resident service milestones remain planned. Run `python3 app.py --version` to identify the checkout. The project is actively maintained; Agent history formats remain external contracts and may require parser updates as their producers evolve.

## License

MIT (see `LICENSE`).

## Workflow integration (v1.1.0)

See [implementation boundaries and commands](docs/CODEKIT-INTEGRATION.md).


## 明确日期的无头复盘

问“某一天做了什么”时，使用消息时间窗的 `python3 -m history_core activity --date 2026-09-27 --timezone Asia/Shanghai --sources codex,claude,mcode --data-dir ~/.cache/agenttracemesh/reader`。默认读缓存，显式加 `--refresh` 才增量解析；先看来源覆盖，再定点展开。参见[命令、覆盖与回退契约](docs/daily-activity.md)。网页日简报也按消息时间窗展示当天证据；完整复盘优先使用 activity 的刷新、分页与正文展开。

## Linux independent delivery (v1.2.0)

The default page shows progress, outcomes, blockers, decisions, next steps and evidence.
Use **Search history and settings** for the detailed history tools. Historical completion
and command results are never presented as verification of the current code.

```bash
python3 app.py --demo --data-dir ~/.cache/cchv
python3 -m history_core --source codex --source-path /path/to/sessions --data-dir /path/to/cache refresh
python3 -m history_core --source codex --source-path /path/to/sessions --data-dir /path/to/cache search --limit 20
```

Demo uses packaged synthetic sources and an isolated `demo-isolated` cache child; it does
not discover private native databases. Machine pagination returns `index_revision`: pass
it as `--index-revision` with subsequent `--offset` requests. A changed revision requires
starting from offset 0. Search freshness remains explicitly unknown until a new refresh.

`handoff SESSION_ID [--include-plans]` adds source identity, bounded source content revision,
historical project claims, evidence baseline/unknowns and optional unverified plan-file
candidates. JSONL source reads are capped at 2 MiB; truncated sources have unknown full
revision. Native databases remain read-only, with provider-specific audit limits. There
is no execution or new authorization in a handoff. Weak-session bulk cleanup is disabled.

This release is validated on **Linux, Python 3.11/3.12**. Windows/WSL compatibility code
remains present but is not newly certified. See [release validation and rollback](docs/release-v1.2.0.md).

公众号写作素材：从所选证据整理成果卡，预览后下载或导出到本地内容仓，详见[使用说明](docs/materials.md)。

[检索词法与摘录边界](docs/search-semantics.md)：大小写、字面字符、摘录顺序及修订变化说明。

当前源码新增[活跃对话固定读取与纯素材](docs/active-read-snapshots.md)：目标 Codex 文件捕获、逐会话失败隔离和无提示词 `materials` 导出。既有安装包与 GUI 未因此自动升级。
