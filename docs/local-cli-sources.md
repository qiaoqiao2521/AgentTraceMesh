# Linux local CLI sources

The Web and `python3 -m history_core` share provider identities, filters and parsers in `history_core/providers.py`. CLI aliases do not create duplicate sources. All new provider stores are read-only; derived file indexes stay in the Viewer data directory. AGY CLI/Web now persist derived indexes; direct Python callers without a cache directory retain in-memory compatibility.

| Source | Default input | Explicit Web option | Notes |
|---|---|---|---|
| CodeBuddy / `cbc` | `~/.codebuddy/projects/**/*.jsonl` | `--codebuddy-dir` / `--cbc-dir` | Native CodeBuddy envelopes; excludes parent-ID `agent-*` sidecars; message usage dedup |
| Gemini CLI | `~/.gemini/tmp/*/chats/` | `--gemini-dir` | JSONL and legacy JSON, including child-agent recordings; prefer migrated JSONL over same-name JSON |
| pi | `~/.pi/agent/sessions/` | `--pi-dir`, `--pi-sessions-dir` | v1–v3 tree entries, branch annotations; inherited fork usage remains unknown |
| Prime Agent | `~/.prime/agent/sessions/` | `--prime-dir`, `--prime-sessions-dir` | pi-family parser, separate identity and cache |
| GitHub Copilot | `~/.copilot/session-state/` | `--copilot-dir` | Falls back to `~/snap/copilot-cli/common/.copilot`; only `events.jsonl` transcripts |
| mcode / MiniMax Code | `~/.minimax/v2/sessions/**/messages.jsonl` + sibling manifest | `--mcode-dir`, `--mcode-sessions-dir` | Public blocks; dual-file revision-bound review |
| ZCode | `~/.zcode/cli/db/db.sqlite` | `--zcode-state-db` | Shared Desktop/CLI/TUI/ACP store; OpenCode-family SQLite with sequence ordering |
| OpenCode | `$XDG_DATA_HOME/opencode/opencode.db` | `--opencode-state-db` | XDG defaults to `~/.local/share`; existing native reader |
| AGY CLI | `~/.gemini/antigravity-cli/conversation_summaries.db` | `--agy-state-db` | Summary plus neighboring `conversations/*.db`; verified protobuf text/thinking/tool fields |
| Antigravity desktop | `~/.gemini/antigravity/conversation_summaries.db` | `--antigravity-state-db` | Separate store, same decoder; legacy `.pb` is metadata-only |

Existing Codex, Claude, OpenClaw and Hermes sources remain available. Environment overrides supported for new stores: `CODEBUDDY_CONFIG_DIR`, `GEMINI_CLI_HOME`, `PI_CODING_AGENT_DIR`, `PI_CODING_AGENT_SESSION_DIR`, `PRIME_AGENT_CODING_AGENT_DIR`, `PRIME_AGENT_SESSION_DIR`. Explicit arguments win; `--demo` overrides all private discovery and environment paths.

Machine input is the **sessions directory** from the table (or the exact native SQLite file), whereas Web `--*-dir` is the parent tool home:

The installed `cchv reader` entry uses the same module without changing cwd. File-source CLI commands now share the AGY default XDG cache when `--data-dir` is omitted. Existing explicit directories still work; the Python file-source API remains explicit. See [cache reuse and recovery](local-cache.md).

```bash
python3 -m history_core --source cbc --source-path ~/.codebuddy/projects --data-dir ~/.cache/agenttracemesh/reader refresh
python3 -m history_core --source codebuddy --source-path ~/.codebuddy/projects --data-dir ~/.cache/agenttracemesh/reader search --query error
python3 -m history_core --source zcode --source-path ~/.zcode/cli/db/db.sqlite search --query error
```

For tool reuse, indexed sources support `search --brief --limit 3`. Expand selected public messages with the returned index revision:

```bash
python3 -m history_core --source codex --source-path ~/.codex/sessions --data-dir ~/.cache/agenttracemesh/reader message SESSION_ID MESSAGE_INDEX --index-revision INDEX_REVISION
```

Continue only needed text pages with `--text-offset NEXT_TEXT_OFFSET`. The [search contract](search-semantics.md) defines bounds, redaction, pairing, and coverage warnings.

Codex and Claude expose recorded skill/output-style tags through `markers`. See [marker evidence and CLI](trace-markers.md); tags do not assess effectiveness.

## Evidence boundaries

CodeBuddy supports native tool audit and explicit file-change records. Gemini, pi/Prime and Copilot provide normalized conversation/tool audit and revision-bound handoff; their file diff reconstruction is not yet supported. JSON evidence uses a message JSON pointer; JSONL uses the original line. Gemini/pi/Prime/Copilot materialization requires a complete document within the 2 MiB selected-evidence limit; larger histories stay searchable but bounded handoff explicitly rejects them.

OpenCode native audit is available, but selected-fragment export remains unsupported. ZCode and fully decoded AGY sessions now support revision-bound public-message selection and review handoffs; audit refs retain native locations, without fabricated JSONL lines. AGY partial/legacy sessions retain limited browsing, without complete-message export. AGY reads private, automatically cleaned DB/WAL snapshots to avoid SQLite updating the original SHM read marks. Source revisions include conversation files and WAL, not just the summary DB. Unsupported steps and legacy `.pb` remain explicitly partial/metadata-only; media is not decoded. No cumulative byte cap is applied to an AGY store refresh (the former 256 MiB cap was removed on 2026-10-01). Snapshots are copied and cleaned one conversation at a time. Remaining limits: 64 MiB physical DB+WAL per conversation, 20,000 steps per conversation, 8 MiB per step and 2 MiB for audit snapshots. A failed conversation is retained as `partial_source_error` metadata, with `source_error` identifying the observed path, size, limit and reason; other conversations remain readable. Unknown byte measurements are null, and step counts use separate units. Coverage reports the complete failed count and a bounded error list; a refresh can be partial without making the whole store unavailable.

CLI search now defaults to compact candidates; `--brief` works across registered sources with explicit content limits, while `--verbose` retains legacy full cards. OpenCode/ZCode/Hermes public snippets and revision-bound `message` expansion reuse native text/tool records; this does not expand their selected-fragment-export support. See [empty-index receipts, filename lookup and search semantics](search-semantics.md).

A displayed resume command never executes the provider. CodeBuddy uses `cbc --resume <id>`; unverified resume syntaxes are not guessed.

## AGY derived cache

AGY/Antigravity CLI defaults to `$XDG_CACHE_HOME/agenttracemesh/reader` or `~/.cache/agenttracemesh/reader`. Set `--data-dir` to choose a separate cache. The cache is bound to source path, provider and decoder version. Warm commands reuse unchanged sessions without copying or decoding their bodies; changed sessions alone are decoded. Source revision checks still inspect file metadata. This is not a claim of complete content coverage.

Web stores each provider cache under its configured data directory. Python `HistoryReader(..., data_dir=...)` enables persistence; omitting it retains memory mode. `cache.mode/status/path/warning` disclose reuse or fallback. An unavailable/corrupt cache remains preserved and falls back to memory with `agy_cache_unavailable`; that fallback may rescan the source.

Summary/WAL changes retry once. Persistent races retain path and attempt context; initialization size failures also expose structured `source_error` with observed size and remaining budget. Static oversized conversations are not retried or granted a larger limit. AGY candidates retain the summary and first meaningful user request in `title_candidates`; the user candidate does not overwrite a useful original title.

## Public message timeline

```bash
python3 -m history_core --source agy \
  --source-path ~/.gemini/antigravity-cli/conversation_summaries.db \
  timeline --date 2026-11-05 --timezone Asia/Shanghai --limit 20
```

The same command supports ZCode, OpenCode, Hermes and message-index sources using their existing paths/cache rules. Use `--start-ms`/`--end-ms` instead of a date for an epoch `[start,end)` window. Results sort newest first and contain short public events, source, timestamp and absolute `message_index`. Expand a selected event with `message` and the returned `index_revision`; pass that revision when using nonzero `--offset`.

`coverage=public_message_timestamps_not_full_activity` distinguishes this entry from certified activity. `timestamp_basis=normalized_provider_display_time` may include inferred timestamp fallbacks. Untimestamped/private records are excluded; unknown formats and source failures remain partial. Empty file caches carry the same refresh receipt as search. Date filtering does not prove complete history or an action's success.

Usage is provider-reported historical accounting, not a cost estimate. CodeBuddy, Gemini and ZCode input already includes cached tokens. No verified counters means unknown accounting (legacy usage totals may display zero); it does not mean a free session. Copilot counters are intentionally not inferred from shutdown telemetry; AGY/Antigravity token accounting remains unsupported.

Installed configuration/orchestration wrappers such as TeamAI and CM are not additional native provider histories. CLI names sharing a store are grouped. The dated local inventory and format exceptions are in [the source plan](../plans/local-cli-sources/findings.md).

## mcode v2

MiniMax Code 0.5.5 的 `~/.minimax/v2/sessions` 已作为 `mcode` 注册到公共 reader。固定 manifest/messages 文件、公开块与工具关联，来源只读；不假定旧 `~/.minimax/sessions` 包含 v2。Web 也自动发现同一根目录，支持来源设置诊断、检索、需求分页和验收包。使用 [activity 时间窗入口](daily-activity.md)；旧单来源 `--source mcode --source-path ... --data-dir ... refresh/search` 也可用。

不同录制共享 manifest ID 时，复用现有 `@recording-…` 索引别名保留两份，不再以重复身份拒绝整源刷新。别名不是新的原生恢复ID。刷新审计、普通audit和selected review均校验精确来源路径、原生ID和录制身份；旧审计的大文件能力与review的限额分别保留。

CBC、AGY、mcode、ZCode 的具体操作和限制见[模型验收交接](model-review-handoff.md)。ZCode 新增的验收快照只在临时 DB/WAL 副本上读取；既有 ZCode 浏览/搜索连接使用 SQLite `mode=ro`，可能更新原 `-shm` 的读标记，不修改历史主库或 WAL 内容。快照 helper 的 SHM 字节不变验证不能外推到整个旧浏览链路。
