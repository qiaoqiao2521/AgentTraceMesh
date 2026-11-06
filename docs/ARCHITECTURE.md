# Architecture

## Overview

A dependency-free Python HTTP server indexes local Agent histories into per-source SQLite caches and serves a static browser UI. Deterministic audit code derives evidence and value signals; optional AI code interprets a compact audit payload only on demand.

## Repository Map

- `history_core/` — existing transcript parsers, source indexers, machine service functions and explicit headless CLI.
- `app.py` — compatibility reexports, API routes, platform source bootstrap and HTTP/static serving.
- `audit/` — normalized audit schema, evidence extraction, command classification, scoring, optional AI audit, deterministic handoff generation, and daily-briefing aggregation (`briefing.py`).
- `static/` — browser application, markup, and styling; no build pipeline. The session header hosts the audit panel plus the `.insight-panel` family (usage / briefing / plans / handoff preview).
- `tests/` — Python unit/integration tests and Node-based frontend behavior tests.
- `scripts/` — Linux/Windows launchers, desktop integration, and repository resolution.
- `demo/` — synthetic transcript fixtures safe for demonstration.
- `docs/session-plans/` — historical feature plans and delivery records.
- `DESIGN.md` — visual-system contract (tokens, components, rules) for agent-driven UI changes.

## Entry Points

- `python3 app.py` starts the server, registers available source backends, begins indexing, and serves `static/` on port 8787 by default.
- `scripts/start-cchv.sh` and `scripts/start-cchv.ps1` resolve the repository and provide platform launch paths.
- `static/index.html` loads the browser UI implemented by `static/app.js` and `static/styles.css`.

## Components

### Transcript adapters and indexes

`history_core/sources.py` parses Codex, Claude, and OpenClaw JSONL into a shared session/message shape. `Indexer` persists derived records in local SQLite caches. `OpenCodeIndexer` and `HermesStateIndexer` read their tools' existing SQLite state through source-specific adapters.

Codex expanded results and collapsed previews share one normalizer. It reads plain text, native/JSON `output`/`text`/`content` envelopes, and text-block lists. Explicit errors and nonzero exits survive multi-result wrappers. Image/audio/binary resource bodies use omission markers. Empty and unsupported native shapes remain visible, with original line references retained.

Web and machine readers obtain parser versions from `providers.parser_version`; refresh reparses older derived caches. Codex is version 9 and Claude version 6. Derived Codex titles skip ambient wrappers; this does not change the source or authorize hidden context as public text. Ordinary scans preserve distinct same-ID recordings using the existing recording aliases; ambiguous aliases do not promise exact native resume. Source recordings stay read-only during indexing. This rendering does not replace the separate deterministic audit extractor or establish application acceptance.

Explicit archive and project deletion retain the source inode through an exclusively created backup link before removing its original path. Name conflicts cannot overwrite older backups, and an active writer can continue appending to the retained inode. Cross-filesystem or unsupported-link failures keep the original and its index entry; batch receipts disclose partial success.

Linux source expansion uses `providers.py` for shared discovery/identity, `codebuddy.py` for native CBC envelopes, `extra_parsers.py` for Gemini recording updates and pi/Prime trees, and `copilot.py` for event logs. Gemini `.json` and `.jsonl` share one materializer; migrated copies are filtered. `zcode.py` reuses the OpenCode part/audit renderer with sequence ordering and a connection-local TEMP VIEW to project message metrics; it never changes the native schema. See [paths and capability matrix](local-cli-sources.md).

`agy.py` decodes verified protobuf fields from AGY/Antigravity SQLite steps into a derived session/message index. With `data_dir`, the index persists across processes; Python callers without it retain in-memory compatibility. CLI and Web supply persistent cache directories.

Warm processes reuse unchanged sessions without protobuf decoding; changed sessions are updated transactionally. Cache failures fall back to memory with an explicit warning. Source-change races retry once; static per-session limits remain unchanged.

The two roots retain separate identities. Bounded DB/WAL copies are opened in private temporary directories because even SQLite `mode=ro` can update source SHM read marks. Each conversation has an independent physical read budget; there is no cumulative store byte cap. Per-conversation read failures retain partial metadata and structured size/path/limit diagnostics while other conversations remain readable; a changed overall revision still rejects a mixed snapshot.

Partial or unsupported legacy content is visible in coverage diagnostics, search warnings and session headers. Audit refs use native step indices, not fabricated JSONL lines; fully decoded bounded sessions support selected public-message exports; partial/legacy exports and token accounting remain unsupported.

`reader.py` attaches source-wide indexed-session counts to health and search. Empty derived caches carry an explicit refresh receipt, separately from native empty stores or populated indexes with no matches. CLI search defaults to the compact service; `--verbose` selects legacy session cards. `compact_search.py` adapts native public text/tool records and literal file-reference searches without another database. Evidence retains absolute message positions and source revisions; unsupported rendering or query fallback remains partial metadata. See [search semantics](search-semantics.md).

### Source routing and HTTP API

Brief indexed searches use a narrow `(session_id, role, kind)` index to filter roles and public kinds before reading bodies. Candidate ranking and pagination materialize scalar keys before fetching session JSON. One- and two-term tool coverage uses exact EXISTS predicates with early exit; larger queries retain the single-message MAX score. Excerpts select their three records before calculating body lengths and display windows. The index contains no message text and does not invalidate recording signatures. Existing persistent AGY caches acquire it through the existing schema migration, without re-decoding unchanged sessions.

Newly parsed messages persist their absolute `message_index`; session counts verify that the ordinal set is complete. Legacy caches retain an ID-only numbering fallback without rewriting large TEXT rows. Activity queries first aggregate and page narrow message keys, then read selected session metadata and bodies. Missing legacy coverage counters stay unknown. See [activity query and coverage](daily-activity.md).

`native_timeline.py` pages public message events through the shared indexed/native projections. Selected records retain absolute display slots and revision-bound message expansion. Provider display timestamps may include inferred fallbacks; this endpoint does not certify complete activity. See [timeline usage](local-cli-sources.md#public-message-timeline).

`SourceBackend` binds a runtime system and source to an indexer. `Handler` routes `/api/{system}/{source}/...` requests and serves the static application. The runtime exposes Windows/WSL or Linux according to the host rather than presenting unavailable systems.

API requests validate Host against the listening port and local destination. State-changing or network-capable requests require JSON and reject foreign Origin/cross-site requests; CLI JSON calls without Origin remain supported. Reindex uses POST. Material writes additionally require an actual loopback peer. Session search checks the displayed source revision before and after reading, just like message retrieval; the browser discards stale windows until the session is reopened.

### Recorded skill and output-style markers

Codex and Claude parsers tag explicit native Skill requests, Learning context injection and standalone Agent declarations. Tags stay in existing message metadata. The [marker reader](trace-markers.md) returns bounded revision-bound locators without hidden bodies or a new database. Trigger evidence does not evaluate benefit; calling Agents own later interpretation and skill changes in their repositories.

### Authored problem cases

`history_core/problem_cases.py` reads and saves editable JSON annotations in a local case directory, with a versioned public example under `history_core/case_examples`. It reuses the existing secret masker and evidence locators; Markdown is rendered from the same object for CLI, HTTP and Web. Case queries do not initialize history providers. Authors supply interpretations, contribution attribution, applicability and explicit replacement relations; the service does not infer them or establish current verification. No additional database or model service is involved. See [workflow and API](problem-cases.md).

### Audit layer

`audit/extractor.py` normalizes transcript events and produces evidence-backed `AuditPayload` objects. Classification and scoring remain deterministic. `audit/ai_audit.py` and `audit/llm_client.py` provide opt-in semantic interpretation from compact payloads; the raw transcript is not the default AI input.

Audit schema version 4 revokes a previous final answer when later meaningful user, tool or reasoning activity exists. A trailing failed tool result cannot inherit an earlier completed outcome; historical failures remain visible after a successful retry.

### Handoff layer

`audit/handoff.py` builds compact or standard continuation capsules from deterministic audit data, selected user constraints, verification results, evidence locations, and explicit unknown current Git state unless separately requested. It intentionally excludes raw transcripts, hidden prompts, reasoning, and full tool output. The frontend renders the selected capsule as themed markdown (`.handoff-theme-*`), copies it as rich text, and exports `.md` / standalone themed `.html`.

Legacy capsules and optional plan excerpts reuse the selection export's common-pattern secret masking before truncation and rendering. Source locators and revision identities remain intact; this is not full anonymisation. Raw local audit viewing retains its original evidence.

`history_core/review.py` adds a requirements-first review export on top of `evidence.selection_bundle`. `GET /api/reuse/review-requests` pages ordinary user messages with absolute indices and source/index revision checks; `POST /api/reuse/review-preview` requires a selected original user message and adds an independent current-code review prompt. The workspace previews/copies/downloads this packet without executing a model or probing the historical project path. Summaries cannot substitute for original requirements; code verification remains `not_performed`. See [workflow and limits](model-review-handoff.md).

### Usage aggregation

Codex parsers fold cumulative `token_count` telemetry into per-session `tokens_*` columns (kept in the `sessions` table alongside audit scores); Claude sums per-message `message.usage`. OpenCode aggregates its own native token columns; Hermes reports zeros. `query_usage` on each indexer powers `GET /api/{system}/{source}/usage` with totals, per-day (viewer-local timezone), per-project, and top-session views. No currency costs are computed.

### Briefing layer

`audit/briefing.py` aggregates one day of session summaries into a deterministic briefing: near-duplicate sessions (same cwd, touched-file Jaccard ≥ 0.5) merge into the higher-value one; highlights, blocked sessions, and deliverables are enriched from at most 12 deterministic audits and any stored AI audits. `GET /briefing` returns the payload plus `render_briefing_markdown` output; `POST /briefing` adds a narrative — LLM (strict JSON) when a provider is configured, a composed heuristic fallback otherwise. The raw transcript is never an input.

### Plan-aware scanning

`scan_plan_files(cwd)` is a stateless, read-only filesystem scan of planning artifacts (`task_plan.md` / `progress.md` / `findings.md` at the root and under `plans/*`, plus `docs/session-plans/*.md`), capped at 80 files / 120 KB bodies, excerpting only well-known `## ` sections. `/plans?project=` and `/session/{id}/plans` (mtime window ±7 days around the session) serve it; nothing is persisted and nothing is written.

### Browser UI

`static/app.js` manages source/session navigation, pagination, search, transcript rendering, audit panels, evidence jumps, tool collapsing, and handoff copy actions. State that improves continuity across reloads is stored in browser local storage.

## Data Flow

```text
local JSONL or source SQLite
        ↓
source parser / adapter
        ↓
normalized sessions and messages
        ↓
local SQLite cache + deterministic audit evidence
        ↓
HTTP JSON API
        ↓
static browser UI
        ↓ optional explicit action
compact AI audit input or deterministic handoff
```

## Important Invariants

- Transcript facts come from deterministic parsing; AI output may explain facts but cannot replace evidence.
- Evidence identifiers must remain traceable to transcript locations.
- Local transcripts and caches stay local unless the user explicitly invokes configured network AI auditing.
- OpenCode and Hermes source databases are treated as read-only.
- Parsers tolerate unknown or malformed records and surface recoverable raw content instead of crashing the whole session.
- Static assets work without a frontend build or package installation.

## External Dependencies

- Python 3.11+ standard library and a modern browser are the only required runtime dependencies.
- Optional AI audit can use an OpenAI-compatible endpoint or a reachable local Ollama service.
- Agent history formats and OpenCode/Hermes schemas are external contracts that may evolve.

## Fragile Areas

- `app.py` combines several responsibilities; broad edits can affect unrelated sources or API behavior.
- Transcript formats differ by tool and evolve over time. Parser changes require fixtures for each affected source. Usage extraction bumps parser versions, forcing a one-time full re-parse.
- SQLite schema migrations must remain idempotent for existing user caches (usage columns included).
- Evidence jumps depend on stable message/evidence identifiers across backend and frontend.
- Frontend tests use lightweight DOM shims, so browser-only APIs need explicit compatibility handling.
- `scan_plan_files` reads the filesystem on request; its caps (file count, body size, blank-cwd rejection) must not be relaxed, or a request could scan an unintended directory.

## Read Next

- Session parsing or index behavior → `app.py` and `tests/test_session_previews.py`
- Deterministic evidence or scoring → `audit/` and `tests/test_audit_extractor.py`
- AI audit behavior → `audit/ai_audit.py`, `audit/llm_client.py`, and `tests/test_ai_audit.py`
- Agent handoffs → `audit/handoff.py`, `tests/test_handoff.py`, and `docs/session-plans/003-agent-handoff.md`
- Token usage → `parse_codex_session_file` / `parse_claude_session_file` in `app.py`, `query_usage` on the indexers, and `tests/test_usage.py`
- Daily briefings → `audit/briefing.py`, `tests/test_briefing.py`, and `docs/session-plans/004-usage-briefing-plans.md`
- Plan-aware scanning → `scan_plan_files` / `extract_plan_sections` in `app.py` and `tests/test_plan_aware.py`
- UI interactions → `static/app.js` and the matching `tests/*.js` (insight panels: `tests/test_insight_panels.js`)
- Durable tradeoffs → `docs/DECISIONS.md`

## Native session continuation

The existing resume header generates native commands for Codex, Claude and OpenCode. OpenCode uses `opencode --session <id>` with the selected source cwd and validates the native ID before emitting a shell command. The viewer only displays/copies the command; OpenCode owns session loading and mutation. TaskHub adoption is optional orchestration above this path. Native conversation continuity and evidence-based handoff/SpecMesh files serve distinct purposes.

`history_core/native.py` and `python -m history_core ... native-reference` provide the
independent machine path. The query-only v2 reference includes configured device/store
identity and the complete selected native content revision. CM consumes this CLI without
Web and independently validates the source before its own execution admission. See the
[byte contract and limits](native-session-contract.md); Viewer's output is context-only.

## Workflow boundary (v1.1.0)

See [machine interface and human workspace](CODEKIT-INTEGRATION.md). `/` is now the workspace overview; `/history` retains the full history interface.

Claude native references use `history_core/claude_native.py`, a strict reader of one explicit
canonical main-session JSONL file. Full raw bytes determine its revision; the tolerant display
parser remains separate. CM independently validates the same provider-specific byte contract
through its ClaudeSessionStore/ClaudeHistoryClient. Neither reader invokes a model or grants
execution authority; actual Claude runtime continuation remains a separate acceptance gate.

Codex native references use `history_core/codex_native.py` for one explicit canonical
rollout JSONL file. The bounded reader binds device, file identity, complete bytes,
workspace and model; it rejects incomplete or mixed-identity records. The existing
headless `native-reference` command returns only context authority. Its reference is
cross-checked against CM's independent CodexSessionStore; task adoption, idle/turn
validation and execution permission remain CM responsibilities.


### Machine read capability boundary

`history_core.HistoryReader` is the public machine object used by the headless CLI for
explicit refresh, health, search and handoff. It composes private legacy indexers without
forwarding mutation methods or connections. The Web continues using legacy adapters.
JSONL caches live under a source-path-bound machine subdirectory, disjoint from the source;
old CLI caches are retained but not reused, so first use requires explicit refresh.
Linked source/cache entries and cached handoff paths outside the selected source fail
explicitly. This is an application capability boundary, not a Python process sandbox.
Machine pagination binds an index revision and adds an ID tie-breaker; changed revisions require restarting from the first page. Search and health report freshness unknown.
Construction/refresh validate the source tree; cached search validates only the selected
root and cache. Handoff checks the selected path lexically and after resolution, rejects
linked components, and explicitly opens that file so read errors cannot become empty audits.

JSONL refresh enumerates before reconciliation, streams one parsed session at a time into
one SQLite transaction, and rolls back on parse/read/observed-change errors. Only successful
scans delete derived records for absent paths. A stat signature (mtime/ctime nanoseconds,
size/device/inode) detects the tested replacements; old rows without a signature are reparsed.
It is not a cryptographic content revision or a guarantee against concurrent adversarial edits.
Indexes match the pinned predicate and timestamp/ID order, with a separate project prefix;
they preserve existing LIKE matching while avoiding duplicate scans and unnecessary sorting.


### Linux independent delivery v1.2

Machine pages include a cache revision, changed atomically with successful index updates.
Subsequent processes supply that revision; a mismatch rejects the page and requires a
restart. Native-store page revisions conservatively include database/WAL stat identities.
Ordinary handoff audits an in-memory bounded source snapshot; unrequested project Git
state is not inspected, and historical code baseline remains unknown. Optional plan-file
context requires an explicit include-plans flag. The workspace displays historical evidence
as such and surfaces background refresh failures. Demo caches always use a separate child
directory. Release archives embed commit/file hashes; the runtime exposes its actual build
identity through /api/version.

## Cross-source evidence workspace (1.3 candidate)

`/` serves `static/workspace.*` and defaults to the cross-source project directory; `/?view=search` is the global retrieval view. `/history?project=<exact cwd>` opens a cross-source conversation reader and automatically selects the latest session. Existing source/message deep links remain valid.
`history_core/reuse.py` provides bounded search, exact-cwd projects and timelines over existing indexes. `/api/reuse/{search,projects,timeline}` returns a cursor tied to all participating source revisions. Query failures stop continuation; native SQLite stores retain explicit capability limits. Hermes transport labels are not project directories.

Search snippets carry cached message offsets plus a source revision, checked before and after deep-link message reads. Audit event offsets are different: `/api/reuse/raw` reads an exact JSONL line from the same bounded source snapshot and validates content/context revision. `provenance.selected_snapshot` is shared with Web/Reader audit and selected exports; no second transcript parser or source writes are introduced.

For message-index sources, the bounded `search_blob` is only a fast path: shared and legacy session search also match complete cached `messages.text`, including raw-format fallback records but excluding harness context from that fallback. Terms use literal `LIKE` escaping and AND across a session's messages. Existing caches need no parser-version migration. Indexed excerpts locate matches in full text inside SQLite before returning at most three 240-character snippets; offsets are numbered before matching. The query time/VM budget still reports an explicit partial/error result when exceeded. Native-store excerpt limits remain unchanged.

Gemini legacy JSON uses `/messages/<index>` pointers instead of invented line numbers. Its JSONL updates, pi branches and Copilot event dedup require complete bounded materialization. Optional default stores that never existed and have no cache are excluded from cross-source query errors; explicit configuration errors and lost history still prevent misleading pagination.

`file_history.py` extracts only explicit file tool inputs/results from a revision-bound snapshot. Explicit patches or old/new replacements can be shown; unrelated tests and current disk state are not inferred. Native databases without indexed file sets report unsupported instead of silently returning complete-looking empty history.

`evidence.py` assembles at most 5 selected summaries or indexed messages / 8000 body characters, rejects stale selections, masks common secret patterns and returns previewable Markdown/JSON. Bounded-prefix summaries and complete-message exports have distinct bindings. Masking is not full anonymization. `diagnostics.py` returns richer local-only source status and a separate allowlisted export, never paths or transcripts in that export. Indexing status avoids waiting on the index lock.

Validation and source capability matrix: [reuse findings](../plans/history-reuse-product/findings.md). Fixed synthetic quality/performance tests do not establish native database performance or real-project Agent acceptance. U1 was cancelled by the user on 2026-10-01; current acceptance prioritizes headless retrieval and evidence-bound handoff.

## Project reader and settings

`GET /api/reuse/sessions` is a lightweight, revision-bound project conversation directory. It reuses candidate/index contracts without extracting per-session audits; optional source filtering happens before unrelated backend initialization. Full cwd, system/source/store and native session ID retain their distinct roles. Project pagination remains capped at 20 per page; reader conversation pages may request up to 100.

`static/settings.js` synchronously creates one native dialog shared by both pages and owns its preference controls. It reuses existing theme/role storage keys, adds local reader preferences for text size, tool collapse and initial audit expansion, and notifies the reader through `hv-preferences-change`. Legacy listeners skip settings-owned controls. Source cards show configured paths and refresh existing derived indexes; changing source roots remains a launch-argument operation. The reader updates URL/history and selection identity across project, provider and insight links; stale responses cannot replace a newer navigation.

## Conversation reading aids

`related_sessions.py` annotates the bounded conversation candidate set before pagination. Only identical nonempty project paths and informative normalized titles qualify; groups preserve every original source/store/session identity and indicate possible relatedness, not task equivalence. Group counts describe the bounded candidate set and inherit its partial disclosure.

`GET /api/reuse/key-messages` uses `conversation_navigation.py` over the existing message index. Full text is matched before cutting 240-character excerpts; absolute message numbering precedes context/tool-input exclusion. Up to 80 wording hints plus the first request and last ordinary assistant reply are returned, with explicit truncation/coverage limits. Source revision is required and checked before/after reading; native adapters without a compatible message index report unsupported. Hints are navigation, not verification of successful delivery. Reader deep links expand and rerender target tool results before scrolling.

## Outcome materials

`history_core/materials.py` builds deterministic draft Markdown from revision-checked `evidence.selection_bundle` and bounded user fields. `POST /api/reuse/material-preview` returns a preview revision; `/material-export` rechecks sources and requires that same revision. Optional `--material-dir` enables local exports. These endpoints accept loopback Host + same-origin JSON only. Atomic link publication never replaces an existing file: identical content is a no-op, human edits conflict, new revisions get distinct filenames. Selection identities define the material ID; field/evidence revisions define the immutable revision. Timestamps are excluded from the digest. Source locators containing filesystem paths are omitted, while session/evidence IDs and revision hashes remain. Common secrets and home-directory prefixes are masked, not guaranteed anonymized.

The Markdown inbox is outside qiao-wechat's draft scanning workflow. It requires human fact/privacy review and article preparation before the existing publisher consumes it. No models or WeChat APIs are called. See [usage](materials.md).

当前文件索引的大小写、字面匹配与前三条摘录顺序见 [检索词法与摘录边界](search-semantics.md)；Unicode 大小写和跨消息短语排序不超出该声明。

## Web daily evidence

`history_core/web_briefing.py` reads verified message timestamps from the existing Web index using the activity IANA-local-day contract. GET/POST `/briefing` share schema `history.web-briefing.v2`; old whole-session audit ranking and cumulative tokens are excluded. Absolute message numbering precedes window filtering. Coverage stays unknown/partial because this path does not observe live recordings. See [daily activity](daily-activity.md).

`native_review.py` captures AGY/ZCode public messages and fresh deterministic audit together, retaining reader indices across filtered private blocks. ZCode export reads a bounded private DB/WAL snapshot, with independent physical and selected-row limits. `mcode_review.py` captures and hashes both fixed authority files, verifies the cache projection/signature, and reuses the byte parser. `provenance.selected_review_snapshot` feeds selection and review-request pagination; ordinary `selected_snapshot` returns no raw row for these sources. mcode normal audit uses the same public-block events instead of the Codex fallback.
