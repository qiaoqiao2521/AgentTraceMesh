# Skill 与 output-style 日志标记

TraceMesh 从 Codex、Claude 录制和 Antigravity IDE transcript 提取有原文定位的标记。查询读取本机派生缓存。历史版本、请求结果和效果各自需要对应证据。

## 标记的证据范围

| kind / event | 原记录 | 能确认什么 |
| --- | --- | --- |
| `output_style / context_injected` | Codex system/developer 中的已知 Learning 指令；Claude `SessionStart` 的 `hook_additional_context` 或 system/developer 指令 | Learning 指令出现在记录中；提示正文哈希可比较版本。不能据此证明插件执行成功或学习有效 |
| `skill / invocation_requested` | 原生 `Skill` 工具调用与 `input.skill` | 对该技能发起了调用；用 Call ID 定位对应结果。不能把请求直接当加载成功 |
| `skill / agent_declared` | Assistant 独立输出的显式标记 | Agent 声明采用该技能；原记录位置与版本可追溯。不是工具执行证明 |
| `skill / skill_file_read` | Antigravity IDE 规划步骤中的文件读取工具调用 | 模型请求读取对应的 Skill 文件。读取是否成功需要配对工具回执 |

`learning-output-style` 是插件。它的上下文注入单列。安装清单、普通名称提及、教学 Insight、压缩转述、工具返回和内部推理都不能构成独立的 assistant 声明。

同一记录保留原生行号或 JSON pointer、绝对 `message_index`、时间、Call ID 和录制中的工作目录。工作目录来自当时的 context，不探测当前磁盘或推断 Git 根目录。Claude 原生 `Skill` 保留调用块编号；没有版本、路径或哈希时保留 unknown，不读取当前技能文件冒充历史版本。

## Agent 怎样留下标记

Claude 的原生 `Skill` 调用已经提供标记，不再重复声明。Codex 等没有原生调用记录时，实际采用技能后，在现有对话中输出一条独立 Assistant 消息：

```text
[[tracemesh:skill-trigger]] {"name":"example-skill","path":"/repo/skills/example/SKILL.md","version":"unknown"}
```

可选 `content_hash` 是当时所读技能正文的 SHA-256，格式为 64 位十六进制。字段只接收 `name` 或同义 `skill`、`path`、`version` 与 `content_hash`；正文上限 2,048 字符。动态字段先做最小脱敏，再限为 128 字并标记截断。不要输出原始参数、秘密或整份技能正文。

该声明只在完整独立消息中识别。代码块示例、用户消息、工具返回和带说明的混合正文不会误记为触发。跨 SessionStart 或压缩后重复注入保留各自原生定位，不合并成一次虚构调用。

## 查询与原文定位

从仓库根目录执行，来源与缓存使用已确认的本机映射：

```bash
python3 -m history_core --source codex --source-path ~/.codex/sessions --data-dir ~/.cache/cchv-reader markers --name learning-output-style --kind output_style --limit 3
python3 -m history_core --source claude --source-path ~/.claude/projects --data-dir ~/.cache/cchv-reader markers --name example-skill --kind skill --limit 3
```

首次使用或需要新录制时，先对该来源显式 `refresh`。Codex v8 和 Claude v6 起的缓存支持原有录制标记。旧缓存可按来源重解析。

`HistoryReader.markers(...)` 可按 `name`、`kind`、`event`、`session_id` 和 `project` 过滤。`project` 精确匹配录制中的工作目录，CLI 会话过滤用 `--session`。翻页传回同一缓存的 `index_revision` 和 `next_offset`。修订变化后从第一页重查。

输出只含白名单标记与定位，不导出隐藏上下文、推理或原始工具参数。`coverage=recorded_markers_only`、`partial=true`、`uncaptured_triggers_possible=true` 明确说明捕获边界；`effect_assessment=not_performed` 明确说明未评价作用。其他来源不支持时显式报错。

后续 Agent 用标记定位会话，再通过 [message](search-semantics.md#从搜索直接展开消息) 阅读所需公开调用、返回及纠正。它可以分析前因后果，判断是否值得在原技能仓库调优；TraceMesh 本次接口不提取结论、不写反馈库、不自动调优。

## Codex 大文件投影

默认 `strict` 单文件上限为 128 MiB，单行上限为 8 MiB。对含大图的录制，可指定源根下的相对文件名并单独查询投影修订。

```bash
recording=2026/11/20/rollout-2026-11-20T19-30-59-01a1f8b9-5c54-7260-aef6-cd116a79e967.jsonl
python3 -m history_core --source codex --source-path ~/.codex/sessions refresh --capture-mode projected --max-bytes 268435456 --file "$recording"
python3 -m history_core --source codex --source-path ~/.codex/sessions markers --capture-mode projected --kind skill --event agent_declared
```

`source_capture.captured_through` 是最后一个完整 LF 的原始结束偏移。`prefix_hash` 覆盖此前的全部原始字节，包括省略的图片。发布前在同一个只读文件描述符上复算。失败回执保留错误码和上次验证成功的索引。

`partial` 描述尾部字节。`projection_version` 和 `index_coverage` 单独描述索引内容。覆盖值为 `complete`、`images_omitted` 或 `partial`。用 `image_fields`、`lossy_fields`、`omitted_bytes` 和 `skipped_lines` 查看省略规模。解析和审计重放有界记录，超长文本或过深记录会降低覆盖。严格缓存和投影缓存各有修订。

## Antigravity IDE 读取记录

```bash
ide_db=~/.agy-slots/darring/gemini/antigravity/conversation_summaries.db
python3 -m history_core --source antigravity --source-path "$ide_db" refresh
python3 -m history_core --source antigravity --source-path "$ide_db" markers --event skill_file_read
```

显式刷新只处理该 IDE 库中已发现会话的 `brain/<cid>/.system_generated/logs/transcript.jsonl`。查询只读缓存。缺少 transcript、尚未刷新及采集错误分别见 `transcript_coverage`。transcript 前缀或解析版本变化会更新修订。AGY CLI 的来源名为 `agy`，此证据来自 `antigravity`。

只收录 `MODEL / PLANNER_RESPONSE` 中的 `read_file`、`view_file` 或 `default_api:read_file`。参数对象或其 JSON 字符串必须给出结尾为 `skills/<名称>/SKILL.md` 的路径。`Path`、`AbsolutePath`、`path` 可以带一层成对引号。相对路径需要录制中的 cwd。冲突键、URL、遍历、命令文本和备份后缀均跳过。

每条请求保留 `raw_ref.line_no`（从 1 开始）、`record_id`（原生步号字符串）和 `json_pointer`（含调用序号及原参数键）。只有能匹配到原生消息时才给出 `message_index`。历史版本为 `unknown`。跨槽分母和迟调用的查询方法见 [Omni 工作台](omni-studio.md#跨槽-markers-与漏调复盘)。
