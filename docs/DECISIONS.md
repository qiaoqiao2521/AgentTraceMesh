# Decisions

## 2026-11-26 — Route context gaps before retrieving history

Decision: keep semantic gap selection with the calling Agent, expose observable
historical candidates from compression, and validate/route current needs through a
small deterministic entry. Share recall, preserve original evidence and add optional
recovery to handoff. Use bounded caller-carried receipts instead of another database.

Why: more stored history does not identify what blocks the next action. The useful
unit is missing information plus evidence requirements and a conditional next step.
SpecMesh retains current repository authority; historical candidates stay derived.

Boundaries: no always-on search, implicit refresh, automatic command retry, new model
call or transcript upload. Hits require review; missing/conflicting/stale evidence
remains unresolved. Revisit semantic automation only with measured recurring gaps;
do not infer real Token savings from synthetic call-count tests.

## 2026-11-22 — Compress problem evidence into protected drafts

Decision:

Add bounded `compress` and case-first `recall` capabilities on the existing reader
and problem-case store. Generate local drafts from observable messages, keep
stopping signals separate from task completion, and preserve authored changes.
The calling Agent may refine explanations using the referenced evidence.

Why:

Session handoff is useful for continuation, while recurring questions need the
specific correction, attempted tool and result. Existing cases already provide
storage; the missing workflow is extraction and question-first retrieval.

Boundaries:

No model call, vector service, global hook or automatic transcript upload. Lexical
matches and wording signals do not prove causal relationships. Raw history stays
read-only, and incomplete or stale records remain explicit.

## 2026-02-10 — Keep the required runtime dependency-free

Decision:

Use the Python standard library and a static browser frontend without a build step.

Why:

The viewer should start directly against local logs with minimal setup on personal machines.

Rejected:

A framework-heavy backend or mandatory frontend toolchain.

Revisit when:

A required feature cannot be delivered safely or maintainably with the current runtime.

## 2026-06-27 — Separate deterministic evidence from AI interpretation

Decision:

Parse tools, files, commands, errors, remote activity, tests, and outcomes deterministically. AI may interpret a compact payload, but every completion claim must remain traceable to evidence.

Why:

Operational facts must be reproducible and inspectable; model output alone is not a trustworthy audit trail.

Rejected:

Letting an LLM read complete transcripts and infer the factual action record.

Revisit when:

Never for factual provenance; only the compact semantic interpretation layer may evolve.

## 2026-06-27 — Keep processing local and AI audit opt-in

Decision:

Index and analyze histories locally. Only send a compacted audit payload to a configured model after an explicit user action.

Why:

Development transcripts can contain secrets, private paths, proprietary code, and operational details.

Rejected:

Automatic uploads, background AI auditing, and bulk remote processing of history.

Revisit when:

The user explicitly chooses a different privacy model with clear controls.

## 2026-06-27 — Use per-source adapters behind one UI contract

Decision:

Normalize Codex, Claude, and OpenClaw JSONL while adapting OpenCode and Hermes SQLite sources into the same browsing API. Treat tool-owned databases as read-only.

Why:

The histories differ in storage and schema, but users need one coherent browsing experience without risking source data.

Rejected:

Mutating upstream databases or forcing all tools into one physical storage format.

Revisit when:

A source exposes a stable official API that is safer or richer than its current local representation.

## 2026-07-22 — Make handoff deterministic and separate from AI Audit

Decision:

Build Agent continuation capsules from normalized evidence, user corrections, verification, and Git state. Keep them independent from optional AI audit.

Why:

Continuation context must work offline, remain compact, and avoid invented completion claims.

Rejected:

Copying full transcripts or requiring an LLM to summarize every handoff.

Revisit when:

A new source lacks enough deterministic structure to produce a useful capsule.

## 2026-09-06 — Treat token_count telemetry as data, not transcript

Decision:

Fold cumulative `event_msg/token_count` payloads into per-session `tokens_*` columns during indexing and stop surfacing them as raw JSON messages; parse Claude's per-message `usage` the same way. Store usage denormalized on `sessions` so listing, briefing, and aggregation never re-parse transcripts.

Why:

Usage was already on disk but invisible, and the previous fallback flooded transcripts with one raw JSON block per token-count event (thousands per session).

Rejected:

Currency cost estimates (prices change; tokens are the honest unit) and a separate usage table (no query needs message-level granularity yet).

Revisit when:

A source exposes model-level pricing or per-turn cost data worth showing next to token counts.

## 2026-09-06 — Keep plan-aware scanning stateless and read-only

Decision:

`/plans` endpoints scan the session's working directory on request (capped: 80 files, 120 KB bodies, well-known sections only) instead of persisting plan files in the index. Briefing generation is likewise computed on demand with no storage, and its POST route is allowed for read-only sources because it writes nothing.

Why:

Plans change on disk constantly; a second copy in SQLite would go stale and add migration surface for near-zero query benefit. Read-only scanning respects the "upstream data is never mutated" invariant.

Rejected:

A `plan_files` SQLite table with mtime-sync, and indexing plan content into `search_blob` (would pollute keyword search across unrelated projects).

Revisit when:

Cross-project plan search or plan-history diffing becomes a real workflow.


## 2026-09-06 — Validate the existing product before expanding scope

历史优先级；当前执行方向由下方 2026-09-14 独立交付决定承接。M3 仍未完成，不再作为活动主线。

Decision:

Prioritize independent review of the insight-upgrade fixes, real browser acceptance,
and a first-time user completing search → evidence → handoff. Keep SpecMesh as lightweight
continuity documentation. The active plan is `plans/history-viewer-product-validation/`.

Why:

The user explicitly focused this next plan on History Viewer. The existing feature set
already supports a useful independent product; correctness and successful first use are
the next evidence needed. Green unit tests do not establish either on their own.

Rejected:

Parallel rewrites, new data sources, extra platform features, or expanding SpecMesh Map
before validating the current workflow.

Revisit when:

The current workflow has passed independent acceptance and a concrete user need justifies
an extension. Code cleanup needed to fix an observed defect remains within scope.

## 2026-09-11 — Integrate against existing repository authority

Reuse the existing storage/parsers and keep SpecMesh independently callable. Do not install a second TaskHub from a proposal or equate historical handoff with live completion. The human overview and headless retrieval have separate entry points. See [scope and remaining limits](CODEKIT-INTEGRATION.md). Status: implemented for v1.1.0; broader roadmap gates remain planned.

## 2026-09-14 — History 独立交付与可选原生续接

决定：复用现有 headless CLI/parser/indexer，将只读能力边界、增量可靠性、带来源的交接候选、人的结果优先界面和发布分别验收。机器检索不依赖 Web、CM 或模型；SpecMesh 文件仍保存审查后的项目事实。History 继续提供显式原生 resume command；实际执行由用户选定原生 CLI 或可选 CM 承担，History 不执行 provider 会话。经 CM 的受监督接管、授权和多设备协调归 CM，TaskHub 不成为直接续接或独立发布前置条件。CM 实时状态是可选适配，未连接时 unknown。

原因：用户明确选择三个独立项目和有限任务卡，SpecMesh 第一、History 第二、CM 第三。旧总交接附件的 9 月 11 日表格不是最新事实，已有接口和局部真实续接结果应复用，不能为重排范围全部重做，也不能将局部执行推广为全矩阵完成。

执行边界：本项目 [bounded-delivery](../plans/bounded-delivery/task_plan.md) 当前 queued，只详写 HV-H0.1；一实现者加按需一审查者，最多两轮修正，收口退出，不用 Goal/loop/自动续卡。保留本机主工作区；已授权直接 push 不因本决定增加 worktree/PR 门槛，同仓确需并行才隔离。本次仅规划，不提交/推送/发布。

拒绝：重写已有 parser、同时扩展多种传输/服务、把原生执行迁入 History、建立第二份项目决定库，以及未测量就承诺大日志性能。旧 M3 保留历史未完状态；旧 HV-H3 局部验收保留，但退出本项目五阶段独立分母。

重访条件：已选任务卡的实测证明显式 CLI/refresh 不足，或稳定接口有具体适配缺口时，另选范围明确的后续卡；不能以“将来可能需要”为由启动常驻调度或无限研究。


## 2026-09-15 — 显式刷新与原子缓存更新

机器热查返回缓存并标记 freshness unknown；整树可读性检查放在构造/refresh，交接只验证所选文件。逐文件解析写入一个事务，成功才清理消失路径，失败保留原索引。拒绝为了控制内存而分批提交半份结果，也不以后台刷新掩盖时效边界。固定合成基准证明当前方式达到门槛；若跨刷新分页或超大单文件出现明确需求，再单独建立修订/资源边界协议。

## 2026-09-22 — 历史证据复用使用现有索引和显式修订

决定：统一检索、项目/文件时间线和选择性交接复用既有索引及来源快照。完整 cwd 才代表项目；Hermes 入口类型归未绑定。消息深链绑定索引修订，审计证据按原始行与内容修订定位，不能混用事件序号。来源查询失败时停止续页。文件工具调用、结果、历史验证与当前文件状态分别呈现。

原因：固定题集的词法检索达到 80% 证据 Hit@5，尚有四道自然语言题未命中；先交付可核对的路径，不在缺乏收益证据时引入语义引擎。选择性导出在读取和结果层都设上限，最小密钥遮盖后仍需预览。拒绝隐式云处理、自动清除历史失败、自动写回项目记忆或执行原生会话。

验收边界：工程候选与独立试用分开。用户明确 U1 稍后安排，候选包可准备，U1 仍 pending，不能称本轮产品体验全部完成或据此发布稳定版。重访条件为固定题集缺口的收益实测或 U1 观察到的具体问题。

## 2026-09-22 — 本机 CLI 按原生存储接入，共享检索契约

决定：按用户新授权扩展 Linux 本机来源，Web 与机器 Reader 使用同一来源注册、解析器和修订检查。命令别名不重复建来源；ZCode、Prime、AGY CLI 与桌面 Antigravity 即使共享协议也保留不同存储身份。AGY 只按已安装实现的 protobuf 描述符解码已确认字段；旧格式和未知步骤显示能力限制。读取 AGY 前创建有界临时 DB/WAL 快照，避免只读 SQLite 连接仍更新源 SHM。

原因：可执行命令数量、存储数量和可恢复正文数量是三个不同事实。仅解析摘要或扫描二进制字符串会伪造完整支持；直接打开原始 WAL 数据库也不足以证明源文件未变。拒绝猜测 token 口径、将包装器或编排事件重复计为 provider 会话，以及为新来源建立另一套检索服务。重访条件为已核实的新格式或明确的编排层历史需求。U1 继续待试用，候选保持本地。

## 2026-09-22 — 项目优先阅读，设置与检索分层

用户本人验收指出重复搜索、来源标签拥挤和设置缺失。决定默认项目目录 → 跨来源会话 → 对话，保留一个全局检索入口；阅读页仅按需展开会话内查找、续接信息与审计交接。主题/字号/消息显示及来源状态放统一设置，沿用本地偏好键。项目目录复用已有索引，单独提供不生成审计的轻量会话列表。拒绝把所有能力持续铺在侧栏，也不为这次交互调整替换渲染器或引入前端框架。用户反馈或实际导航边界再决定后续变化。

## 2026-09-27 — 成果素材以本地 Markdown 交接

采用既有修订绑定的证据选择与人工表单，产出待核实素材，不把 AI 自报完成作为已验证成果。通过显式配置的 inbox 与独立内容仓交接，不改 qiao-wechat MediaAsset 或自动创建文章/远程草稿。身份来自所选证据集合，修订来自字段及来源修订；以原子新建保护人工改稿，相同内容幂等。新修订保留旧稿，归并由人负责。

## 2026-09-27 — 会话归组与原文导航保持可解释、无损

专注 Agent 对话阅读：相关组仅以同项目且规范化后标题相同的明确线索生成，泛化标题不组，不做会话采样或删除，不将同组认定为同一任务。关键消息复用全文消息索引和原有修订深链；“提及失败/验证”只表示措辞，“最后回复”不表示完成。首尾锚点独立于 80 条线索上限，超限/正文不完整/来源不支持均披露。拒绝新聚类运行时、隐式 LLM 分析和把历史陈述写成项目当前事实。名称暂不迁移；后续范围以真实阅读障碍为依据。

## 2026-09-28 — AgentTraceMesh 定名与 U1 暂存

用户要求在 TraceMesh 基础上加 Agent 词缀，决定产品名为 **AgentTraceMesh**，专注本地 Agent 对话记录。README 与两个页面显示新名，明确原名 Codex & Claude History Viewer，区别于 OpsPAI/TraceMesh；不宣称名称全球唯一或完成商标检索。仓库路径、cchv 命令、数据目录、偏好键作为兼容标识保留，不再列作命名待决事项。

conversation-reading 随本提交收口，选择“暂存待 U1”，不发 1.3.0-rc.4，不删除功能、不替换已安装候选。U1 保持用户安排的独立 Linux 用户试用；收到试用反馈后另行决定发行，未发生的用户验收不记通过。检索修复/补证与阅读实现按两笔独立提交保存，其他工作树任务保留且已有外部完整备份。

## 2026-09-29 — Web Briefing uses message windows

Date-specific Web review uses verified public message timestamps and an explicit IANA timezone, not session creation dates or whole-session scores. Bounded evidence is traceable, with unknown freshness and truncation disclosed. CLI activity remains the refresh/pagination/full-text route. This does not change standalone audit scoring.

## 2026-09-29 — Separate operation signals from review value

Keep the legacy value_score field and formula for compatibility, but describe it as an operation-record signal, not delivery or importance. Heuristic review is always available regardless of score; auto uses it when the external-call cost policy blocks a model call. Explicit llm mode retains the configured cost gate. Current project indexes point to accepted/current records rather than carrying stale next-step lists.

## 2026-10-01 — Agent 对接为主，取消独立人类试用门槛

用户明确“真实用户接入的计划其实不需要了，agent 对接为主”。取消 history-reuse-product 的 U1 及旧 product-validation 的 M3，不再作为待办或发布门槛；覆盖此前“稍后安排”和“暂存待 U1”的决定。原试用未执行，历史材料与冻结验收快照保留，不改写为通过。

优先既有无头检索/日活动、原需求与后续纠正定位、修订绑定的选取与验收交接；Web 保留人工查阅和预览。上下文交接不代表当前代码已验证，不自动调用模型或执行会话。仍保留接口正确性、来源只读、隐私、有界读取、失效与部分失败披露，以及发布产物/安装/回滚检查。只有明确提出新的独立用户研究需求才重开试用，不自动恢复旧门槛。

## 2026-10-01 — 取消 AGY 全库累计字节限制

用户报告正常历史超过 256 MiB 后整库初始化失败，并要求先不设上限。取消刷新累计字节门槛，沿用逐会话快照与清理。读过的累计字节不是峰值资源占用，不据此阻止整库搜索；单会话、步骤与审计/导出限制独立保留。来源稳定性、只读和不完整覆盖披露仍须验证。此决定不承诺无限资源或固定响应时间，后续按实际单会话/索引成本问题再调整。
