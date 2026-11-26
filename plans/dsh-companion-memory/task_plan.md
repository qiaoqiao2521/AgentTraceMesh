# DSH Companion Memory

> 历史记录：本文件于 2026-11-26 随插件从 ControlMesh 迁入，保留 2026-10-23 原研发记录、环境与验收范围，不表示当前安装或部署状态。[迁移来源](https://github.com/qiaoqiao2521/ControlMesh/blob/59014053939707b98a9fea3510400fdf02d468f5/plans/dsh-companion-memory/task_plan.md)。当前归属与用法见 [记忆插件](../../docs/memory-plugin.md)，本轮迁移验证见 [迁移进度](../memory-plugin-migration-20261126/progress.md)。

## Goal

Use xerj to compare the legacy CM memory implementation and selected open-source
companions, then deliver a small native DSH plugin for personal companion memory.

## User Intent

- Keep DSH as the single harness and reuse its tools, hooks and session lifecycle.
- Reuse existing implementations before adding code.
- Preserve editable personal memories, shared experiences and commitments across sessions.
- Keep project truth in SpecMesh and capability decisions in CapMesh.
- Preserve the old CM implementation and personal data without automatic migration.

## Scope

Implementation lives in `plugins/dsh-companion-memory/` unless reference analysis
reveals a directly reusable native plugin requiring only configuration.
Reference checkouts and xerj data stay outside Git and outside personal memory.
No new runtime, scheduler, authentication layer, avatar, vector service or cross-harness bridge.

## Phases

1. [complete] Install xerj locally, index bounded reference sources and verify DSH hooks.
2. [complete] Record reuse decisions and implement the smallest companion-memory slice.
3. [complete] Verify native DSH loading, cross-session recall, correction and forgetting.
4. [complete] Document setup and evidence, review changes, commit and push the accepted scope.

## Success

- xerj queries return actual source passages from CM and community references.
- DSH loads the plugin using its documented native entrypoint.
- Explicit memories and commitments survive a new session without importing private histories.
- Users can inspect, edit, correct and forget stored material.
- Missing optional input or a broken memory record does not break a chat or a batch.
- Tests distinguish a real local DSH runtime from mocked provider behavior.

## Adopted Knowledge

- CapMesh D024 and PIT-002: reuse one harness; no replacement orchestration or model/login layer.
- Obsidian `Wiki/开发协作接入.md`: keep task state here; promote only reusable lessons.

## Next Step

Use the installed companion-memory profile for real-model user acceptance when desired.

## Errors

- Guessed raw GitHub URLs for xerj `llms.txt` and uppercase DSH repository returned 404.
  Use the official xerj site and the repository identity from the installed DSH package.
- Direct xerj site/release downloads timed out. GitHub's release-asset API succeeded.
- xerj rejected writes on the home disk's flood-stage watermark; moved only temporary
  index data to the available memory filesystem without deleting user files.
- Native DSH tool DSL rejects JSON-Schema minimum/maximum keys. Removed those keys;
  argument bounds are handled locally and the actual DSH registration now passes.
- The native empty profile patch includes comments before its empty array. The initial
  literal-empty check refused it; inspected the generated file and preserved its meaning.
