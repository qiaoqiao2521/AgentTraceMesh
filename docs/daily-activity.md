# 明确日期的本地活动证据（无头）

当前产品和仓库名称为 [AgentTraceMesh](https://github.com/qiaoqiao2521/AgentTraceMesh)，
原仓库名为 Codex-Claude-History-Viewer。启动命令仍为 `cchv`，本机已有目录仍为 TraceMesh。
从本仓库运行，Python 3.11+，无需 Web、DISPLAY、模型或后台进程。

## 先查候选，再定点展开

```bash
cd /home/muqiao/桌面/TraceMesh
python3 -m history_core activity \
  --date 2026-09-27 --timezone Asia/Shanghai \
  --sources codex,claude,mcode \
  --data-dir ~/.cache/cchv-activity --refresh
```

`--refresh` 显式扫描所选来源的全部录制目录，复用 Indexer 的文件签名增量更新。
活动入口不计算整段会话审计分数。删除也更新缓存；最多三次尝试，持续变化的文件
保留已有缓存并披露 `partial_refresh_unstable_recordings`。不自动定时刷新。
早期 activity 迁移曾令旧文件签名失效，需显式刷新补齐原始时间/定位字段。本次消息编号扩展不使签名失效，也不自动重解析全库。

省略 `--refresh` 读取缓存，并对所选录制做文件元数据及可读性观察；不会解析原始正文。
没有刷新过的缓存、来源缺失、错误或未支持的来源均有覆盖说明，不能据空列表说“没工作”。
缓存只读模式仍可能创建/迁移派生缓存 schema；从不修改来源录制。

默认选择：Codex `~/.codex/sessions`、Claude `~/.claude/projects`、mcode
`~/.minimax/v2/sessions`。只遍历这些明确选中的存储，可用
`--store mcode=/absolute/selected/session-directory` 等覆盖。mcode 只打开固定兄弟文件
`manifest.json` 和 `messages.jsonl`；不跟随 manifest 路径，不读取 auth/配置/请求缓存。
来源路径和缓存必须互不包含；链接被拒绝。

当前日活动认证范围为 **codex、claude、mcode**。其他 provider 的既有 search/handoff
不变；activity 返回 `supported:false`，不要拿未支持来源换取“完整”结论。
AGY、ZCode 等来源可用[公开消息 timeline](local-cli-sources.md#public-message-timeline)按日期查看；其显示时间语义与 activity 不同。
Claude 现有发现规则排除 `agent-*` 子任务文件，此限制会在 coverage 中列出。

每个来源各有 `items`、`index_revision`、`next_offset` 和 `coverage`。
默认每源最多 100 个候选、每会话最近 6 条公开消息（显示时按时间正序），正文每条最多
2,000 字符；标题和项目是会话背景，可能早于请求日期，不能当作本日成果。
`window_message_count` 仅是记录数量，不是成果数或个人生产力。

拿返回的 `session_id` 和 `index_revision` 定点展开。下面占位符须替换为实际返回值：

```bash
python3 -m history_core activity --date 2026-09-27 --timezone Asia/Shanghai \
  --sources codex --data-dir ~/.cache/cchv-activity \
  --session-id RETURNED_SESSION_ID --evidence-limit 30 \
  --revisions '{"codex":"RETURNED_INDEX_REVISION"}'
```

`--offset` 按各来源候选翻页；`--message-offset` 按所选会话的窗内消息从新到旧翻页，
每页内部正序。非零 offset 必须传 `--revisions`；内容修订变化时丢弃旧分页，重查第一页。
多来源共享同一个 offset，但各自独立分页，不是全局排序页。建议对有下一页的来源单独翻页。
`next_message_offset` 可展开较早消息。文本截断有 `text_truncated`，本接口不宣称返回全文。
需要完整长正文时，在相同日期/来源/session/revisions 参数上追加 `--message-index 返回的消息序号 --text-offset 2000`，按 `next_text_offset` 继续；偏移针对脱敏后的文字。无需临时解析器，也不扫描家目录。
旧 `handoff` 有 2 MiB 限制，不能把它当成超大会话完整正文的可靠回退。

Python 公共接口是 `history_core.query_activity(stores, date=..., timezone=..., data_dir=...)`
或已选定来源的 `HistoryReader.activity(...)`。旧的 `--source ... search/refresh/handoff`
命令及 `cchv` 启动方式保留。mcode 本轮支持 refresh/search/activity，尚无传统 audit-handoff/原生 resume 契约，调用会明确返回不支持；请用 activity 定点展开。

## 时间、身份与覆盖契约

- 调用者明确指定 `YYYY-MM-DD` 和 IANA 时区；不把午夜后的“今天”悄悄解释成昨天。
  当天窗口是 `[本地零点, 次日零点)`，DST 日可能不是 24 小时；与主机 TZ 无关。
- 只按有记录时间的消息/事件取证。创建时间、目录日期、mtime 不代替发生时间。
  缺少原始时间的记录排除并披露。上下文、系统消息、内部思考不作活动证据。
- 每条证据保留 `timestamp_ms`，同时提供请求时区的 `timestamp_local`（ISO 8601、毫秒精度、含偏移）；窗口提供 `start_local/end_local`。调用者直接引用，不让模型口算时间。重复的夏令时本地时刻以偏移区分；日期筛选仍使用原来的 epoch 半开区间。
- 每条证据含 `system/source/store_id/session_id/source_revision/message_index`。
  原始行号与 mcode 消息 ID/turn ID/工具 call ID 保留；记录中有明确父会话关系时保留。
  `source_revision` 是**索引快照**修订，不能冒充当前文件内容哈希或 Web 深链修订。
- 同项目不自动合并。activity 显式刷新对 Codex/Claude 同一原生 ID 的多个录制保留独立缓存身份（不更改旧 Web 默认扫描策略），附
  `native_session_id` 和冲突说明；带 `@recording-...` 的缓存 ID 不能直接传原生 CLI resume。
  mcode 同存储内冲突 manifest 身份及冲突消息 ID 则刷新失败，不静默覆盖。
- 用户文本表示意图，助手文本表示报告，工具记录提供进一步依据。任何历史“通过”
  都不自动证明今天的项目状态；没有外呼叙事管线。
- `last_successful_refresh` / `last_observed_at` 是观察时点，`checked_through:null`
  表示不承诺“已完整检查到此时”。`observed_unchanged` 只说明观察时元数据与快照相符；
  `stale` 是已知变化/刷新失败；`unknown` 不推导无活动。
- 顶层 `partial` 包含来源失败、过期、候选分页、缺时间和解析/身份警告。证据条数及正文
  截断另见每个 item/evidence，不能仅凭 `partial:false` 认定全文已读。
- `query_activity` 同时返回 `receipt`。它集中列出请求来源、本次可读到的来源、失败来源、
  各来源检查水位、失败原因和 `partial`，避免调用方分别读取 `coverage` 后漏掉预算失败。
  `checked_through` 只有全部来源有相同且可证明的完整水位时才取时间；当前契约保留为
  `null`。`receipt.partial:true` 表示不能据此得出“没有记录”或“覆盖完整”的结论。
- SQL 预算默认每来源 2 秒/有限 VM 指令；`HISTORY_QUERY_BUDGET_SECONDS` 可显式调整，回执返回实际值。它不是模型账户额度，不包括扫描、锁等待、Python 格式化的总墙钟。
  记录 candidate/read/body counts 和 wall_seconds，命令墙钟须另测。

## 大索引查询与旧缓存

新解析的消息持久化 `message_index`，包含过滤掉的 context/思考占位；筛选后不重编编号。会话记录编号总数，失配时回退轻量键编号。旧缓存仅按消息 ID、时间与插入顺序编号，不将 TEXT 放进排序，也不要求一次性回填大库。

日窗先沿已有活动索引筛选，聚合并分页会话键，再读取选中的会话元数据与正文。解析时保存缺时间/未解析计数，避免每次活动查询扫描全表正文。旧缓存缺少计数时返回 `null` 与 `record_count_sessions_unknown`，整体汇总仍为 partial；显式刷新后才可补齐，不以零冒充未知。

Web `/briefing` 已改为同一日期/时区半开消息时间窗，返回 `history.web-briefing.v2`。
浏览器传入本地 IANA 时区；直接 GET/POST 未传 `timezone` 时按 UTC。
Web 读取现有索引，不调用整段审计，不展示整会话累计 tokens、价值分或推断当天交付。
每会话展示最近 6 条窗内公开记录，每条 600 字符；候选上限 2000，截断明确披露。
证据链接使用 Web 索引修订与绝对消息序号。错误不会伪装为空：修订变化 409、查询失败 503。
Web 不观察源文件变化，新鲜度明确为 unknown、checked_through 为 null、partial 为 true。
不支持的来源和空缓存不能推导“无工作”。mcode 尚未加入 Web 自动发现。

**完整的明确日期复盘仍优先使用 activity：它支持显式刷新、分页及正文展开。**
Web 是有界的当天证据预览，不承诺全面覆盖。GET 与 POST 使用同一窗口；可选叙事仅接收
日期、计数及覆盖说明，不外传正文片段，也不据操作数量判断成果价值。

## 面向人的回答

先读覆盖，再挑少量证据；不要将所有路径、测试计数和哈希堆在主回答里。例如：

> 按北京时间 9 月 27 日的记录，你推进了 PLC 验证门禁，也继续处理 LifeGame 的同步与部署。
> PLC 会话报告门禁修补已通过离线验证；这不代表 TIA 或实机验收。
> LifeGame 的具体手机端状态要按最后一条验证记录确认，不能只凭“部署完成”推断。
> 明天我建议先补最影响你使用的一项真实设备验证；这是建议，不是新增承诺。

示例是组织方式，不是对所有来源的完整结论；真实定点证据及覆盖限制见本轮验收记录。
