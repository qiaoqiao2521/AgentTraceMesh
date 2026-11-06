# 检索词法与摘录边界

本页说明当前文件索引来源（如 Codex、Claude）的行为，不把结果外推到所有原生数据库来源。

- 多个查询词以 AND 寻找会话，可以出现在不同消息中；标题、正文与项目路径都可匹配。
- 消息索引正文只匹配公开的用户、助手和工具记录。系统/上下文、内部思考、`thinking` 与未识别的 `raw_json` 记录不参与正文召回或摘录；它们仍保留在原会话阅读器中。旧 `search_blob` 混有这些类型，不单独授权命中；查询直接核对完整消息，不要求重建旧缓存。
- `%`、`_`、反斜杠按字面文字搜索，不作为用户输入的通配符。
- 保证 ASCII 大小写不敏感；不承诺非 ASCII 字母的大小写折叠。例如 `Café` 与 `CAFÉ` 不等价。
- 每个会话最多显示三条摘录，依次按同条消息覆盖查询词数、完整查询短语、工具输入/结果、用户消息排序。同级用户消息优先较晚记录，其他同级记录按原消息顺序；较晚用户消息是纠正候选，不表示已自动判定纠正或最终决策。每条消息内部优先截取完整短语，否则定位最早查询词。
- 摘录保留筛选前的绝对 `message_index`，排序不会重编消息编号。会话 AND 不变，不要求每条摘录都含全部词。
- 摘录不是命中总数。会话召回与摘录选取分开，短摘录不意味着全文先被截断再搜索。完整原文通过带修订的消息深链核对。
- 来源异常与零命中分开披露；索引变化后旧分页/旧消息深链可能失效，应重新检索。原始文件已改但尚未刷新索引，不等于索引修订已经变化。

以上公开消息过滤和摘录排序适用于使用公用消息索引的来源。未使用此索引的原生来源沿用各自检索/摘录能力，不能外推为同等公开证据覆盖。这些规则没有引入 Unicode 搜索引擎。历史合成回归与浏览器补证见 [search-hardening](../plans/search-hardening/task_plan.md)。

## Agent 的简短候选

CLI `search` 默认返回小候选；`--brief` 保留为显式同义参数。`--verbose` 才返回旧的完整会话卡。Python `HistoryReader.search` 的默认参数和既有 Web 会话卡保持兼容，需要精简结果时传 `brief=True`。

所有注册来源共用候选中的 `id/title/cwd/start_ts_ms/end_ts_ms`、`snippets` 及修订回执。OpenCode、ZCode、Hermes 从已有原生公开消息/part投影短摘录，不再因缺少公共消息索引而拒绝整个 brief；私有 reasoning 不作为摘要正文。缺少工具配对、未知格式或正文读取失败明确披露 `partial/limitations`，不能把空摘录当成没有记录。只能读取元数据的降级结果标为 `unverified_session_metadata`、`query_applied=false`，不是关键词或角色筛选的有效命中。

简短候选在分页前优先匹配的实际工具证据，再比较正文匹配、标题与最近活动；普通搜索的默认排序不变。每会话最多三条命中摘录，另外最多三条 `related_tool_messages`；后者仅关联同会话中已索引且相等的 Call ID，并列出 `related_to_message_indexes`。没有 ID 时不按相邻位置配对。当前 Codex 和 Claude 索引记录该 ID；其他来源未保存 ID 时关联为空。

每条摘录保留 `kind`、`ts_ms`、调用 ID 及 `tool_summary.exit_status`（`ok/error/unknown`）。短正文不超过 240 字时完整保留；长正文给出 `has_more_before/after`、`excerpt_start/end` 与 `text_chars`，位置基于 `indexed_text`。失败状态与正文窗口独立，显示窗口缺少错误头不消除已知失败。调用 ID 展示上限 128 字，`call_id_truncated` 明确披露；关联仍比较索引中的完整 ID。

已知内容缺失仍通过 `partial`、`content_warnings` 和候选的 `content_status` 披露；未知 AGY step 仅保留数量。零命中不消除来源的覆盖警告。

简短候选用于选择要读的记录。定点展开相同修订的原文和必要上下文，再核对工具结果与当前项目验证；摘录、工具输入或历史退出码不能直接证明本次工作已完成。

### 缩小查询与找回用户粘贴的解法

已知目标会话时，给 `search --brief` 加 `--session SESSION_ID`，在正文查询和排序前限定会话。使用返回的索引 ID；多录制的 ID 可能带 `@recording-…`，不按前缀扩大范围。`--limit` 只限制返回条数，不能代替查询范围。

找用户后来提供的方案、纠正或长段粘贴时，加 `--role user`；也支持 `assistant`、`tool`。角色条件在候选分页和摘录选择前生效。多个词仍以 AND 匹配同会话中该角色的公开消息，标题、目录或其他角色不能补足命中。角色搜索按会话最近活动排序，不计算工具优先分数；不传角色时沿用原排序。两个参数用于 CLI/Python 的精简入口；原生公开投影也在分页前应用条件，降级未应用的条件单独披露。

```bash
python3 -m history_core --source codex --source-path ~/.codex/sessions --data-dir ~/.cache/agenttracemesh/reader search --brief --session SESSION_ID --role user --query '报错或解法关键词' --limit 3
```

长消息搜索完整索引正文；摘录仍最多三条、每条 240 字，绝对 `message_index` 不变。用返回的修订和 `excerpt_start` 展开命中段，再按需读取前后公开消息。用户粘贴的方案是候选材料，不自动认定为原创、已采用或已验证；前面的失败、后续动作和验证须分别回读。没有关键词时不提供正文摘录；会话分页不等于会话内所有命中的消息分页。

工具配对按会话内的少量选中消息查询，避免为选中 Call ID 扫描全表。可选配对遭遇 `query_budget_exceeded` 时保留已经取得的命中，设置 `partial`、`snippets_partial` 和 `related_tool_messages_error`；空配对此时不表示没有对应记录。候选搜索或摘录本身失败仍显式报错，不转换为零命中。预算是本地 SQLite 查询成本限制，不是模型账户额度；全库 activity/markers 的预算失败须按各自阶段诊断。

## 从搜索直接展开消息

使用搜索返回的 `session_id`、绝对 `message_index` 和 `index_revision`，无需取得 activity 日期或转换修订：

```bash
python3 -m history_core --source codex --source-path ~/.codex/sessions --data-dir ~/.cache/agenttracemesh/reader message SESSION_ID MESSAGE_INDEX --index-revision INDEX_REVISION
```

`HistoryReader.message(..., index_revision=..., text_offset=0)` 与 CLI 使用同一路径。每页最多 2,000 个正文字符；有 `next_text_offset` 时加 `--text-offset` 继续，位置基于索引正文。已知秘密范围先在完整的选中消息中识别，再对窗口交集使用等长 `*` 遮罩，避免跨页或任意起点绕过；这仍是有限模式的最小脱敏。元数据超限明确标记，不声称已展示全文。

内部思考与非公开消息拒绝展开；OpenCode/ZCode/Hermes使用同一公开投影展开，保留原显示序号和public_rendered_text偏移口径。消息保持 `partial` 与覆盖状态；`freshness=unknown`、`current_verification=not_performed` 不代表当前应用已验证。此入口不隐式刷新或修改历史源；修订变化后重新搜索。search 与 activity 的修订契约各自独立。

## 按日期找活动

自然日复盘优先走 [无头 activity](daily-activity.md)，不是按会话创建日期的旧 briefing，也不需要临时遍历家目录。指定日期、IANA 时区和来源；先检查 coverage，再按返回身份/修订展开。未支持来源与过期缓存不代表“没有工作”。

## 空索引与零命中

`health` 和单来源 `search` 都返回本次同一读取视图中的 `indexed_sessions`。计数针对所选来源根，独立于关键词和项目筛选；不等于正文已完整覆盖。

- 新建文件来源缓存但未刷新：`index_state=empty`、`refresh_required=true`、`partial=true`，并附 `index_empty_refresh_required`、`refresh_argv` 和正确引用路径的 `refresh_command`。执行返回的命令后重查；不会暗中全库刷新。
- 已有索引而关键词无命中：`index_state=populated`、`indexed_sessions>0`；仍要结合来源 coverage、partial、时间窗和修订判断，不能外推成全历史不存在。
- 原生数据库本身无会话：`index_state=source_empty`；不会把它伪装成待刷新的文件缓存。AGY 元数据已入索引但正文全部失败时，计数可能大于零，`source_coverage` 同时披露解码数、失败数和 partial。

`health.reader_module` 与 `parser_version` 用于识别实际调用的代码，避免把仓库修复当成旧安装包已更新。来源错误详情最多展示5项，另给完整失败数与截断标记；不因列表短就声称只失败5项。

Codex 库较大或全量刷新曾超时，使用空索引回执中的 `selected_refresh_argv_template`，把 `SOURCE_RELATIVE_JSONL` 换成已知录制的相对路径，沿现有 `refresh --file` 定点捕获。保持来源根和data-dir不变；定点读取成功不表示其它录制都已索引，不以缩小source-path另建孤立缓存来伪装全源覆盖。

聚合 `/api/reuse/search` 也按来源返回 `source_indexes`、`indexed_sessions` 与状态。空派生索引附 `index_empty_refresh_required` 和现有 `/api/reuse/refresh` 的结构化请求，整体为 partial。原生空库仍是 `source_empty`；查询或计数失败保持错误，不冒充零命中。

## 按文件名或路径找原文

```bash
python3 -m history_core --source codex --source-path ~/.codex/sessions --data-dir ~/.cache/agenttracemesh/reader search --field files_touched --query 'clip.mp4' --limit 3
```

这一路径复用已有消息/part数据，在候选分页前按完整查询字符串做字面匹配，支持含空格路径；不另建文件索引，也不采用会话卡推断的 `files_touched` 作为操作证明。它是字面子串查询，不是路径解析或文件系统存在性检查。

结果提供公开消息的角色、类型、短摘录及绝对消息定位。`path_reference_not_operation_verification` 只表示原文引用了此路径；需要展开后核对创建、交付、删除及返回状态。用户提过文件名、助手声称交付，均不自动证明文件实际落盘或已删除。无关键词报 `filename_query_required`，不把全会话列表当作文件命中。

## 预算、争用与中断

`query_budget_exceeded` 仅用于当前 SQLite progress handler 确实取消的查询。等待共享连接锁的时间不消耗该查询的 VM 时间预算。SQLite BUSY/LOCKED 返回 `source_busy`、`failure_domain=storage_contention`，CLI标记可重试；原因未知的 INTERRUPT 返回 `query_interrupted`，不猜测为额度不足。原有 query budget 不变，不新增账号额度、总库限额或自动重试循环。

查询失败保持错误回执和阶段；不会转换为 `items:[]` 的正常零命中。已有命中但可选配对失败时仍保留命中和 partial。

普通brief搜索先通过不含正文的会话/角色/类型索引限定消息，再对轻量候选键排序分页，最后读取该页会话JSON。旧缓存只补SQLite窄索引，不全量重解析或改写来源；角色AND、工具排名、全文尾部匹配和消息定位保持原契约。无ASCII字母的排名和定位关键词省去整段正文的lower副本；ASCII大小写和NUL尾部仍按原规则处理。1个词的工具最大覆盖用EXISTS判定，2个词用同一公开工具消息的any/all精确得到0/1/2分，3个词以上保留原MAX；不是把多条工具消息的词数合并。摘录先按原规则选三条，再计算正文长度与窗口，全文仍参与匹配。

`HISTORY_QUERY_BUDGET_SECONDS` 保留为显式手柄，默认 2 秒，activity 回执披露实际值；非有限或无效值回到默认。大库日窗已改用轻量键筛选/聚合和持久消息编号，不靠扩大默认预算掩盖宽列排序。具体见[活动查询](daily-activity.md#大索引查询与旧缓存)。

## 派生标题与旧缓存

Codex 标题跳过纯ambient/环境包装，从首个可提取的真实用户请求首行截断；AGY摘要为空、默认或纯包装时，从已解码用户请求回退。英文摘要不因缺少中文就被丢弃；用户自定义标题保留。Codex parser版本9只在显式refresh后迁移旧派生标题，不自动重写源会话。

AGY 同时返回原摘要和首条真实用户请求的 `title_candidates`，不因模板摘要看起来有效就藏掉用户候选。跨命令重扫通过[持久缓存](local-cli-sources.md#agy-derived-cache)复用，缓存失败有独立警告；按日期取公开消息可用同页 `timeline` 入口，不能外推为全部 activity 已支持。

标题提取不等于正文normalize修复。混合环境包装后的请求可能仍被原公开正文过滤省略；本轮没有通过公开整段环境/指令来改变此边界。工作范围与验证见[现场修复](../plans/retrieval-friction-20261105/progress.md)。
