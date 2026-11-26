# Project Instructions

This repository follows [SpecMesh v1.1](https://github.com/qiaoqiao2521/specmesh).

## Start Here

First read:

1. `PROJECT.md`

Then read only what the task requires:

- Architecture → `docs/ARCHITECTURE.md`
- Historical decisions → `docs/DECISIONS.md`
- Personal memory plugin → `docs/memory-plugin.md`, then the selected plugin README
- Active work → `plans/<task>/`

Do not load unrelated documentation by default.

For a missing prior decision, repeated failure or interrupted continuation, first
identify what evidence the current action actually lacks. Use the current project
and source before history; only then use [context recovery](docs/context-gap.md).
Carry the bounded receipt forward to avoid repeating unchanged queries. A case hit
is a candidate for review, never execution authority or verified project memory.

Memory plugins are maintained here, independently of CM. Use the selected host's
native plugin lifecycle; do not route memory storage or context receipts through
CM. Keep current task state with the host and current project facts in SpecMesh.

## Project Memory

Conversation history is not project memory.

Update `PROJECT.md` when confirmed user intent, priorities, constraints, or rejected directions change.

Update `docs/ARCHITECTURE.md` when durable knowledge about how the system works changes.

Update `docs/DECISIONS.md` when an important decision is made that future agents may otherwise revisit.

For substantial work, maintain `task_plan.md`, `findings.md`, and `progress.md` under `plans/<task>/`.

## Development

- Preserve the local-first, dependency-free Python runtime. Optional host plugins
  keep their dependencies and tests separate and are never required by the reader.
- Preserve existing plugin data paths on code/package moves. Import only explicitly
  selected legacy material; preview first and apply only when requested. Do not
  scan, relocate or rewrite private stores as part of a source migration.
- Treat user transcripts as sensitive data; do not add uploads or remote processing by default.
- Keep deterministic evidence extraction separate from optional AI interpretation.
- Use the repository's existing Python and JavaScript tests and verify changes before completion.
- Do not introduce process or infrastructure unless the task requires it.
- Do not silently reinterpret or weaken user requirements.

## Documentation

Keep documentation concise and prefer links over duplicated explanations.

Code explains implementation. Documentation explains intent, structure, decisions, and current work.

## 开发知识按需接入

先读本项目上下文。遇到方案取舍、重复问题或跨项目经验时，读取 `KNOWLEDGE_WORKFLOW_CONFIG` 指定的路径说明；未设置时查 `~/.config/knowledge-workflow/paths.md`，再从映射的 Obsidian `Wiki/开发知识入口.md` 选相关页，读写规则见 `Wiki/开发协作接入.md`。配置或来源不可用时跳过，不阻塞开发；不默认扫全库。

History 提供按需定位的历史证据，项目事实回源项目；Obsidian 保存可复用解释。消费历史片段时保留原有来源定位、修订检查和脱敏，不把模型解释或旧对话提升为当前事实，不全量导入历史或改变现有成果素材的导出目的地。收尾有新认识才由规划或收尾 Agent 修订对应知识页，普通任务不强制建卡或复测实体环境。

工具借鉴先返回适用工具、下一步动作、限制与少量公开证据；使用 `search --brief` 减少无关统计，再按需展开。已有计划或采用知识的任务，收尾在 progress 记录 `updated`、`already_covered`、`no_reusable_delta` 或 `pending`。更新时核对原页和段落读回；新增触发条件时同步入口，即使仍是旧页。待处理时注明原因与 owner，不以历史索引刷新冒充知识回写。
