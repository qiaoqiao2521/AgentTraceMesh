# AgentTraceMesh — Project

Repository: [qiaoqiao2521/AgentTraceMesh](https://github.com/qiaoqiao2521/AgentTraceMesh). Renamed from Codex-Claude-History-Viewer on 2026-10-31; existing `cchv` commands and local directory aliases remain compatible.

## Why

Local Agent CLIs and desktop clients leave useful session history, but raw transcripts are slow to search and poor at answering what an Agent actually delivered. This project turns those records into a fast local history inbox and an evidence-backed engineering ledger.

## User Intent

- Preserve how requirements, professional corrections and technical approaches evolve across sessions. Agents author explanations and explicit relationships; TraceMesh provides evidence-linked case storage/retrieval/export. Separate task outcome, reusable knowledge and current applicability, and help a later Agent avoid a rejected route. See [problem cases](docs/problem-cases.md).

- Find past Agent sessions, commands, patches, decisions, and discussions quickly.
- Provide explicit resume commands for a selected native provider session when available. The user can execute them through their selected native CLI or choose CM for supervised adoption; TaskHub is not a prerequisite. Native conversation context and verified SpecMesh project files are complementary layers.
- See the practical value of a session: what changed, what was tested, where it failed, and whether the request converged.
- Continue work from compact, evidence-backed handoff context instead of replaying an entire transcript.
- Support general/lower-cost models doing implementation and a user-selected stronger model reviewing it: carry selected original requirements, later corrections and historical evidence into a requirement-by-requirement check against the current code. Exporting context itself is not approval or verification.
- Keep private development history on the user's machine by default.
- Deliver history retrieval, evidence and explicit resume commands independently of CM. History does not execute provider sessions; the user-selected native CLI or optional CM owns execution, and live CM facts are an optional adapter.

## Non-goals

- An enterprise observability or multi-tenant analytics platform.
- A real-time Agent gateway, MCP interceptor, or event-sourcing system.
- Mandatory vector search, embeddings, or bulk LLM analysis of historical sessions.
- Treating AI interpretation as authoritative evidence.
- Uploading transcripts or credentials by default.

## Success

A user can locate and inspect a relevant session quickly, understand its intent, actions, deliverables, friction, and outcome without reading the full transcript, trace claims back to evidence, and copy enough context for another Agent to continue safely.

## Constraints

- Python 3.11+ standard library only for the application runtime.
- Modern browser frontend without a build step.
- Local-first storage and processing; network AI audit is optional and explicit.
- Must tolerate evolving and partially malformed transcript formats.
- Platform-aware behavior for Linux, Windows, and WSL sources.
- Source databases owned by other tools, such as OpenCode and Hermes, are read-only inputs.

## Current State

The stable `v1.2.0` Linux release delivers the public read-only reader, revision-bound pagination, atomic incremental indexing, bounded source-aware handoff and the six-section human workspace. Weak-session cleanup is disabled and demo caches are isolated. Five sources have synthetic read-only coverage; Hermes ordinary audit remains explicitly unsupported.

Linux delivery is complete (5/5). Release source is `eb99792dadc46ca2b352be8b56ea16daa2138c6c`; the published archive, local launcher and actual test process identities were verified on 2026-09-15. The process is now stopped; this dated observation is not continuous runtime monitoring. See [release evidence](plans/bounded-delivery/progress.md). Windows/WSL and live model/CM execution are outside this release acceptance. The historical M3 independent-user trial was not performed; the user cancelled it on 2026-10-01. It is no longer a pending task or release gate.

## Current Priority

2026-11-07继续：[不限定角色搜索余量](plans/activity-cache-20261105/progress.md)。保持2秒预算，优化一/两词工具排名与摘录长度计算；全文、公开块、排名、分页与定位语义不变。已安装20ac3dd；目标搜索20个新进程P95 1.21秒、最大1.23秒，32次混合查询零失败/partial；164项相关回归通过，计时边界见同一进度。

2026-11-07：[普通搜索查询成本修复](plans/activity-cache-20261105/progress.md)。本轮只修TraceMesh自身：角色/类型窄索引、候选分页后取会话JSON、中文工具词排名免整段lower复制；保持默认2秒。已安装46265ec；真实用户角色查询完整进程约0.15–0.18秒，不限定角色同词约1.94秒；159项相关回归通过，边界见进度。原矿包与知识沉淀由用户另行处理。

2026-11-05追加：[本机缓存与安装](docs/local-cache.md)。本机已安装fbd1ec7，实际cchv与reader入口均已验证。真实AGY空派生缓存冷建8.8–10.4秒，跨进程复用无重新解码。默认CLI统一来源绑定缓存，避免按任务新建副本；主WAL正常checkpoint，退役实验缓存压缩保留，缺失原件的旧索引不按可重建垃圾处理。实际时间、空间和安装修订回源[进度](plans/activity-cache-20261105/progress.md)。

2026-11-05：[大库活动与 AGY 缓存](plans/activity-cache-20261105/task_plan.md)。消息编号在新索引中持久化，旧缓存仅排序轻量键；日窗先聚合再读取会话元数据。既有约 3.93GB 主缓存同日窗由默认预算失败变为约 0.40 秒返回 10 会话、20 证据，未刷新源或提高预算。AGY CLI/Web 接入持久增量缓存，并提供各来源公开消息 `timeline`；时间线不等于完整 activity。验收和安装边界见[进度](plans/activity-cache-20261105/progress.md)。

2026-11-05：[现场检索障碍修复](plans/retrieval-friction-20261105/task_plan.md) 源码与有界现场验收完成。已区分空索引/零命中/失败、隔离AGY单条超限、默认精简/统一native brief与文件名引用检索、修正标题和争用分类；追加MCode多录制与审计别名。全量831项（1可选跳过）、最后补丁37项定点及9套JS通过。ZCode“额度”单次实际查询由2.053秒预算失败改善为0.237秒、3候选6摘录partial=false；不是全库覆盖或普遍性能保证。安装、正文normalize与真库边界见[进度](plans/retrieval-friction-20261105/progress.md)。

2026-10-31：[问题案例首版](plans/problem-cases-20261031/task_plan.md) 源码验收完成。三路反馈归因案例提供“现在怎样判断／为什么改变／下次怎样做”的共享 CLI/HTTP/Web 入口；一次未继承本对话的 Agent 受控迁移找到了后续纠正，并先核材料与检索权限。776 项 Python（775 通过、1 可选测试跳过）、9 套 JavaScript 及隔离浏览器通过；详见[范围与证据](plans/problem-cases-20261031/progress.md)。旧历史检索 5/5 不代表整个知识产品目标完成；没有自动语义裁决、全量知识抽取或完整 AGY Boost 模型，现有运行服务和安装包未替换。

2026-10-31：[历史证据可靠性](plans/evidence-reliability-20261031/task_plan.md) 源码验收完成。针对用户提供的 `17b4786` 审查，修复备份保留、跨出口脱敏与请求边界，统一 Web 解析版本、录制身份和搜索修订，并验证长会话定位／全文／Clear。732 项 Python（731 通过、1 可选跨仓测试跳过）、全部 JavaScript 套件和 1000 条合成消息的隔离浏览器通过；详见[状态与边界](plans/evidence-reliability-20261031/progress.md)。未替换安装或发布发行版，真实私有长会话未现场复测。

2026-10-19：已交付[活跃会话固定读取与纯素材](plans/active-read-snapshot-20261019/task_plan.md)。复用派生 SQLite 短读事务、原 Codex parser/audit 与素材格式，补充目标捕获和失败隔离。验证及源码/知识发布见该计划；不暂停对话，不新增索引系统或自动升级安装包。

2026-10-10：已完成[工具借鉴复查与精准日志接口](plans/tool-knowledge-reuse-20261008/task_plan.md)：修复跨会话候选、工具输入/结果关联和直接证据展开；区分 learning-output-style 注入、原生 Skill 请求与 Agent 声明。源码及4处原知识页已普通推送，远端修订与逐页哈希已核对，见[交付记录](plans/tool-knowledge-reuse-20261008/progress.md)。作用分析与技能调优归后续 Agent；本轮未替换既有 GUI 安装。

2026-10-08：已完成[工具借鉴与知识收尾](plans/tool-knowledge-reuse-20261008/task_plan.md)。结构化工具结果、公开证据排序与无头精简候选已验证；知识入口和原页已修订、读回并交付。验证及普通推送见[进度](plans/tool-knowledge-reuse-20261008/progress.md)。源码、安装和历史覆盖分别验收。

2026-10-01：**Agent 对接为主**。先检索与定位证据，再把原需求、后续纠正和选定片段交给接手模型核对当前代码；Web 保留人工查阅与预览。U1/M3 独立人类试用已取消，不再邀请、等待或阻塞交付；自动验收不冒充人类试用。

2026-09-27 [相关会话归组与关键消息导航](plans/conversation-reading/task_plan.md)：专注 Agent 对话阅读，相关性仅作可解释线索，全部原会话/原文保留，关键节点绑定来源修订。源码及隔离浏览器验收完成；2026-09-28 随本提交归档，当时状态为“暂存待 U1”；2026-10-01 用户取消该试用门槛，现为工程完成，不发 rc.4、不替换现有安装。产品展示名定为 AgentTraceMesh（原 Codex & Claude History Viewer），技术兼容标识保留；不引入采样检索或第三方聚类运行时。

新增用户授权：[成果素材接入](plans/outcome-materials/task_plan.md)。从所选证据整理一项 AI 辅助成果，预览后写入显式配置的本地公众号素材目录；历史来源保持只读。不自动写成文章或创建微信草稿，人工核实后沿用现有 qiao-wechat 流程。

用户本人验收后要求产品化：默认选项目直接看对话，去掉阅读页重复全局搜索和拥挤来源标签，增加统一设置。[project-reader-ux](plans/project-reader-ux/task_plan.md) 已完成；候选 1.3.0-rc.3（源码 c47bbc8）已离线验包、安装并在本机8787运行供用户继续验收，保留证据与修订校验。候选交付事实以该计划 progress 的日期为准，不代表当前进程状态。U1 已取消。

新增用户授权：盘点本机 Linux Agent CLI 并接入历史，明确包括 OpenCode 与 CodeBuddy/cbc。[local-cli-sources](plans/local-cli-sources/task_plan.md) 的 13 来源注册、解析、工程回归与浏览器验收已完成；候选 1.3.0-rc.2（源码 eb40004）的离线包与安装目录保留，当前 `cchv` 已更新为上述 rc.3。包装器按独立历史存储归并，逐来源记录已验证能力。

用户指定专注 History Viewer、以 Linux 为主，并授权一次性完成 [历史找回与复用计划](plans/history-reuse-product/task_plan.md)。五项增量已实现，候选 `1.3.0-rc.1` 的自动验收、浏览器验证、离线包与干净安装/回滚已完成（源码 0bb034b，本地交付）。2026-10-01 用户改为 Agent 对接优先，取消 U1，不再等待独立用户；保留既有验收证据，不宣称未发生的用户体验验收通过。v1.2.0 仍是既有稳定发行，不使用 Goal 或定时续跑。

## Knowledge Map

- AGY 累计 256 MiB 限制已取消，真实全库检索验证 → [agy-store-cap](plans/agy-store-cap/task_plan.md)

- CBC/AGY/mcode/ZCode support original-request selection and revision-bound model-review handoff on supported bounded recordings; mcode also joins Linux Web discovery. No provider execution.
- Implementation-to-review model handoff → [usage](docs/model-review-handoff.md), [plan](plans/model-review-handoff/task_plan.md)

- Development experience lookup and closeout → [AGENTS.md](AGENTS.md), using the locally configured Obsidian question index. History remains the source-evidence reader; this convention does not change runtime export destinations or automatically publish transcript content.

- Local CLI source expansion → [plans/local-cli-sources/](plans/local-cli-sources/)
- Completed reuse engineering and Agent acceptance → [plans/history-reuse-product/](plans/history-reuse-product/)
- Completed Linux delivery plan → [plans/bounded-delivery/](plans/bounded-delivery/)
- Earlier Agent handoff implementation and partial evidence → [plans/agent-handoff-service/](plans/agent-handoff-service/)
- Dated release alignment → [plans/release-alignment/](plans/release-alignment/)
- Historical product validation / cancelled M3 → [plans/history-viewer-product-validation/](plans/history-viewer-product-validation/)

- How the system works → [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Why durable choices were made → [docs/DECISIONS.md](docs/DECISIONS.md)
- Completed SpecMesh adoption → [plans/specmesh-adoption/](plans/specmesh-adoption/)
- First stable release record → [plans/release-v1.0.0/](plans/release-v1.0.0/)
- Feature delivery history → [docs/session-plans/](docs/session-plans/)

- Plan status index → [plans/README.md](plans/README.md)

## Approved next direction

已交付 v1.2.0 的证据以 [bounded-delivery](plans/bounded-delivery/task_plan.md) 为准，5/5 complete。history-reuse-product 工程已完成，U1/M3 已由用户取消。后续优先无头检索、日活动证据、原需求与后续纠正的验收交接；依照实际 Agent 使用障碍安排改动。发布仍核验源码、产物、安装与回滚，不因取消试用自动升级版本或发布稳定版。既有主工作区和直接 push 约定保留，不强制另建 worktree/PR。
