# Code origin and references

## Migration into TraceMesh

This optional plugin was moved from ControlMesh into AgentTraceMesh on
2026-11-26. Its source baseline is
[ControlMesh `59014053939707b98a9fea3510400fdf02d468f5`](https://github.com/qiaoqiao2521/ControlMesh/tree/59014053939707b98a9fea3510400fdf02d468f5/plugins/dsh-companion-memory).
The package is now `@tracemesh/dsh-companion-memory`, with the
`tracemesh-memory` CLI. No ControlMesh runtime or package is required.

The native DSH plugin/tool identities, existing memory directory layout,
Markdown record format and MIT copyright notices are preserved. `import-cm`
remains an explicit legacy file importer, not a runtime integration.

## Reference implementations

The implementation is a native DSH extension, not a fork of a companion platform.
xerj v1.0.0-rc.98 retrieved the following pinned source passages before implementation.

| Source | Revision | Adopted idea | License / boundary |
| --- | --- | --- | --- |
| [ControlMesh](https://github.com/qiaoqiao2521/ControlMesh) | 7949a337 | Editable Markdown, source references, explicit promotion and lifecycle | MIT; Python runtime and private data remain separate |
| [Miru](https://github.com/kiyotakali/Miru) | cbea20b4 | Separate commitments, episodes and daily journals | Apache-2.0 application; no source or character assets copied |
| [kiwi-mem](https://github.com/LucieEveille/kiwi-mem) | b01a0c50 | Pinned memories and recency affect recall | AGPL-3.0-or-later; architecture reference only, no code copied |
| [MemOS local plugin](https://github.com/MemTensor/MemOS/tree/adcb9f270c982f2b9ff8db9714792829d3bd6ab8/apps/memos-local-plugin) | adcb9f27 | Native DSH bundle and accepted-user-turn hooks | Package declares MIT; existing adapter targets DSH below 0.2.0 |
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) | 687f282e | Native bundle, tools, context and real-loop test pattern | MIT, Copyright (c) 2026 DeepSeek |
| [xerj](https://github.com/xerj-org/xerj) | v1.0.0-rc.98 | Local reference-code indexing and symbol/passage retrieval | Apache-2.0; analysis tool only |

DSH plugin registration and scripted test stream shapes follow its public examples.
The accompanying LICENSE preserves the applicable MIT notices.
No proprietary models, third-party character assets, vector service or companion
application runtime are bundled.
