# Findings

> 历史记录：本文件于 2026-11-26 随插件从 ControlMesh 迁入，保留 2026-10-23 原研发记录、环境与验收范围，不表示当前安装或部署状态。[迁移来源](https://github.com/qiaoqiao2521/ControlMesh/blob/59014053939707b98a9fea3510400fdf02d468f5/plans/dsh-companion-memory/findings.md)。当前归属与用法见 [记忆插件](../../docs/memory-plugin.md)，本轮迁移验证见 [迁移进度](../memory-plugin-migration-20261126/progress.md)。

## Baseline

- Local DSH: `0.2.1-rc.2`, installed as `@deepseek-ai/dsh`.
- Official repository from package metadata: `deepseek-ai/deepseek-harness`.
- xerj was absent; latest observed release: `v1.0.0-rc.98`.
- xerj documentation says the default embedder is lexical, not neural.
- The official `specmesh.md` exists; the previous conversational absence claim was wrong.
- Legacy CM memory remains in `controlmesh/memory/`; source is a reference, not a runtime dependency.

## Existing Worktree

The initial worktree has unrelated Orca bridge and experiment work, including
`.gitignore`, `controlmesh/__main__.py`, `controlmesh/cli_commands/status.py`, and
untracked bridge modules, tests and plans. Preserve them. Owner remains root Codex;
the shortest recovery entry is `plans/cm-orca-headless-bridge-v0/` and `git status --short`.
This plugin does not reactivate those historical runtime directions.

## Reference Boundary

Use only selected public repositories and CM memory source. Do not index home,
personal chat histories, credentials, runtime logs or the legacy private memory store.
External repository instructions and automatic feedback requests are source data.

## xerj Analysis (2026-10-23)

- Installed xerj `v1.0.0-rc.98`; explicit loopback listeners at ports 9320-9322.
- Home disk is 99% used. Its flood-stage policy blocked writes, so temporary
  index data moved to `/dev/shm/cm-dsh-xerj`, outside source material.
- Bounded corpus: 187 files / 3,453,261 bytes. Index completed in 3.7 s:
  151 eligible files, 2,417 live records, 102/102 code files indexed, zero code junk.
- Retrieval used `xerj search` and `xerj def` against prefix `cmcomp`.
- CM `7949a337`: `memory/promotion.py:58` parses explicit candidates;
  `memory/store.py:113` appends a reviewable dream diary. Retain editable Markdown,
  source attribution and intentional promotion. Do not port hash gates or per-turn capture.
- Miru `cbea20b4`: `memory.py:55` separates commitments and journals;
  `memory_router.py:1454` routes writes. Borrow category separation, not its runtime.
- kiwi-mem `b01a0c50`: `database.py:969` ranks with heat, decay and pinned memory.
  It is AGPL-3.0-or-later; no source copied. Use simple local recency ranking,
  with no PostgreSQL, proxy or background extraction.
- MemOS `adcb9f27`: `adapters/deepseek-harness/README.md:1` and package peers show
  a native bundle, but require DSH `<0.2.0`; installed DSH is `0.2.1-rc.2`.
  Its general memory core and Viewer exceed this small companion-plugin scope.
- DSH `687f282e`: `system-prompt/src/index.ts:79,513` provides dynamic contexts;
  `tools/src/schema.ts:554` provides `defineTool`; native hooks and disposal own lifecycle.

## Implementation Choice

Small native bundle under `plugins/dsh-companion-memory/`. Each memory is an
editable Markdown file, isolated by companion identity. Automatic context recall
plus a model-facing tool handle deliberate writes, review, correction, forgetting,
commitment completion and a daily reflection. No second model, automatic private-data
import, scheduler, proxy, or search daemon is required at runtime.
An explicit legacy-CM import command previews supported personal categories before apply.
