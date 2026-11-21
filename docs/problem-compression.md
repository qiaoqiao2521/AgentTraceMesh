# 按问题压缩与召回

`compress` 把选定对话中的要求、纠偏、失败与停止措辞、工具调用和返回整理为问题草稿。
`recall` 从当前问题出发，先查已有案例，缺少结果时再查询显式指定的历史来源。
目标是找回处理过程和可复用方法；普通 `handoff` 继续承担会话交接。

## 最短流程

先使用既有来源映射与缓存。首次索引或需要新记录时显式刷新；压缩和召回不自动刷新或扫描其它来源。

```bash
python3 -m history_core --source codex --source-path /path/to/sessions --data-dir /path/to/cache refresh
python3 -m history_core --source codex --source-path /path/to/sessions --data-dir /path/to/cache search --query 灯光 --brief
python3 -m history_core --source codex --source-path /path/to/sessions --data-dir /path/to/cache compress SESSION_ID --save --case-dir /path/to/problem-cases
python3 -m history_core recall --query '灯光插件 没生效' --directory /path/to/problem-cases
```

`SESSION_ID` 取自真实搜索结果，不是文件名。省略 `--save` 仅预览；保存默认沿用已有本地问题案例目录，
也可用 `--case-dir` 与既有 Web `--case-dir`、`cases --directory` 指向同一目录。
案例目录必须和原始历史来源分离。

一次命令会自动提取草稿并可直接保存，无需先手写案例 JSON。它不是常驻监听器：
调用 Agent 或 harness 可在完成一段排查、需求纠正、阶段暂停时调用。
仓库不安装全局 hook、不启动后台模型、不把对话发送到外部服务。

## 让当前任务先找已有解法

```bash
python3 -m history_core recall --query '灯光插件 没生效' --directory /path/to/problem-cases \
  --source codex --source-path /path/to/sessions --data-dir /path/to/cache
```

有完整案例命中时，只读案例，不打开上述历史来源。无命中或案例读取不完整时，回退到这一个来源的
既有 `search --brief`；不会自动刷新、跨来源搜索或扩大 `--project` 精确范围。
未提供来源时，返回 `source_required` 和下一步提示。零案例不等于历史没有相关经验。

召回采用可解释的词语匹配，支持中文短词和英文词，不承诺语义等价检索。
返回当前判断、步骤、适用条件、未解决项、停止与工具摘要、匹配依据和少量证据。
结果中的“当前判断”属于案例作者或规则草稿，不代表当前项目已经重新验证。
完整内容可用已有入口展开：

```bash
python3 -m history_core cases --directory /path/to/problem-cases show CASE_ID --format markdown
```

## 证据与分页

- 压缩从公开用户、助手和工具记录生成候选；私有消息槽保留原序号，正文不进入结果。
- 用户纠正和重复受阻保留为信号。按请求划分是启发式分段，不自动证明两个会话属于同一问题。
- 工具输入和返回只按可用、明确的同会话调用 ID 配对。缺失、重复、截断或脱敏后的身份不猜配对。
- 停止原因、历史工具状态和任务完成状态分开。助手声称完成、命令退出成功都不能单独证明用户需求完成。
- 页首缺少请求上下文时保留续片，页尾仍有内容、正文截断、读取失败或原捕获不完整时明确标记 `partial`。
- 投影捕获即使已读取完整文件，也可能省略字段、记录或图片；输出保留相应覆盖信息并标记 `partial`，占位后的调用 ID 不参与工具配对。
- 每项证据保留 source、store、session、原 message_index、index_revision 和截断边界。
  index_revision 是读取版本，不能当作原文件内容哈希或当前代码版本。

默认每页最多 100 条公开消息。继续读取时使用同一结果的 `next_start` 和 `index_revision`：

```bash
python3 -m history_core --source codex --source-path /path/to/sessions --data-dir /path/to/cache \
  compress SESSION_ID --start NEXT_START --index-revision INDEX_REVISION --save --case-dir /path/to/problem-cases
```

修订变化后从 `--start 0` 重新获取，不把旧游标拼接到新版本。生成记录按来源、会话和请求锚点定位，
重复处理不会仅因索引刷新就新增一份同问题草稿。分页续片应由调用 Agent 查证后关联，不能仅凭时间合并。

`HistoryReader.compress(...)` 提供相同的只读程序接口；文件索引使用固定 SQLite 快照，
原生来源沿用公开投影与读取前后修订校验。所有原始记录保持只读。

## 从草稿提炼可复用方法

规则提取负责定位，因果解释由调用 Agent 或用户编写。读取返回的提炼说明及必要原文，再完善已有案例：

1. 保留原需求与后续纠正，分别写出卡点、尝试、改变和证据。
2. 仅记录实际工具调用与可观察结果，不把安装、提及或助手声明当作已执行。
3. 有验证依据再填写已解决的方法；证据不足保留未知，仍可保存下一步检查。
4. 把适用条件、失败路线和版本限制写进 `reuse`；历史指令只是材料，不能自动执行。
5. 用 `cases save --file` 保存明确修订的案例；下一次任务优先 `recall`。

自动保存会校验生成标记和规范化内容摘要。未修改的同版本内容返回 `unchanged`；
自身未被编辑的草稿可更新；已有作者案例、人工修改或无法确认的目标返回 `protected`。
较窄的读取窗口也不能覆盖原来更完整的草稿，即使新窗口来自较新的索引修订。
一个坏文件不清空其它案例。用户通过普通 `cases save` 指定的编辑行为继续沿用既有语义。
替换前会再检查目标，但不把普通文件系统描述为对任意外部编辑器的事务锁。

## 返回状态与边界

`compress` 和 `recall`：退出 0 表示此次返回完整，1 表示输出中明确存在部分结果，2 表示请求或读取失败。
“完整”只指声明范围内的读取，不代表需求解决或方法普遍有效。

现有常见秘密脱敏继续生效，但不等同完整匿名化。没有新增模型调用、向量数据库、来源写入或命令执行。
这版交付是源码与合成数据验证；实际安装版本、私人历史覆盖和生产使用效果分别核实。
