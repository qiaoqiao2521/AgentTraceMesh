# 按需上下文恢复

`context` 在当前动作与既有 `recall` 之间增加一个轻量判断层：缺什么、为何阻塞、去哪里找、
需要什么证据、查完接哪一步。它复用案例和历史索引，不创建新的记忆数据库或常驻服务。

## 什么时候调用

调用 Agent 先判断现有上下文能否支持下一步。只有缺少会改变具体行动的信息时，才声明一个 need。
不因出现“继续”“以前”两个词就搜索全部历史；当前文件已经回答的问题直接继续。

`compress` 现在会在已有案例中提供 `context_candidates`，提示可观察到的缺口：原始请求不在窗口、
还有下一页、工具结果未知、相同可见工具输入多次失败、历史停止或等待条件。这些仍是历史候选，
调用 Agent 必须结合当前任务选择；它们不会自动触发检索、重试或恢复已停止任务。

对当前项目的状态、代码或冲突，先回到相关 SpecMesh 文件和源码；对工具能力、权限或输入不足，
先检查当前工具。确实缺少过去的要求、纠正或解决经验时，才路由到历史。

## 最小请求与命令

```json
{
  "schema_version": "history.context-request.v1",
  "task": "核对灯光插件效果",
  "next_action": "根据当前渲染结果修正插件调用",
  "project": "/project/stage",
  "state_revision": "task-state-1",
  "needs": [{
    "id": "lighting-receipt",
    "kind": "history",
    "status": "missing",
    "question": "上次灯光插件实际调用了什么，用户后来纠正了什么？",
    "reason": "只有配置变化，缺少判断当前调用是否遗漏步骤的依据。",
    "required_evidence": "原始工具输入、返回以及后续用户纠正的消息定位。",
    "query": "灯光插件 实际调用 后续纠正"
  }]
}
```

把当前任务的请求交给入口。仓库的 `demo/context-gap-request.json` 是可直接运行的合成示例：

```bash
python3 -m history_core context --request demo/context-gap-request.json
python3 -m history_core context --request request.json --retrieve \
  --directory /path/to/problem-cases --source codex --source-path /path/to/sessions --data-dir /path/to/cache
```

默认只生成路由计划，不打开案例或历史。`--retrieve` 允许先查已有案例；缺少完整命中时，
沿用 `recall` 回退到唯一显式来源。`project` 是记录中的精确项目路径，传入后不会放宽它。
查询和回退都不自动刷新；空索引、来源不可用、查询无匹配分别报告。原始记录保持只读。

`context` 和 `recall` 一样，来源参数写在子命令之后。`compress`、`handoff` 等原入口的全局来源参数
仍写在子命令之前。旧命令参数和默认行为保持兼容。

## 请求的最小约束

| 字段 | 含义 |
|---|---|
| `task` / `next_action` | 当前目标，以及补齐并核对证据后准备执行的动作；均为必填文字 |
| `project` | 可选的精确项目范围；不填意味着调用方没有限定项目，不会由工具猜测 |
| `state_revision` | 调用方维护的当前证据/来源状态标识；变化后允许重新检索 |
| `needs` | 最多 8 项；没有真实缺口时使用空数组，或直接不调用本入口 |
| `id` | 同一个缺口稳定使用同一 ID，改写查询不换 ID |
| `kind` | `history`、`project` 或 `tool` |
| `status` | `missing`、`stale`、`conflicting` 或 `available` |
| `question` / `reason` / `required_evidence` | 缺什么、为何阻塞、什么证据才足以继续；均为必填文字 |
| `evidence` | 可选定位数组；声明 `available` 时必须提供非空且未标为 partial 的定位 |
| `query` | 可选的自包含短查询；省略则使用任务和问题。沿用 recall 的 300 字符/64 词项限制，超限要求明确改写 |

`no_gap` 只说明调用方声明的需求已无未解决项，不证明模型自动识别了所有语义缺口，
也不认证调用方提供的证据。证据的相关性和当前适用性仍由调用 Agent 核对。

## 结果与动作

| 状态 | 下一步 |
|---|---|
| `no_gap` | 本入口不检索；沿当前指令继续原动作 |
| `needs_retrieval` | 使用已限定的查询和来源，或在下一次有界调用中继续未处理的缺口 |
| `needs_current_evidence` | 回到当前项目文件、源码或原始材料；过时/冲突信息不会被旧历史自动消解 |
| `needs_tool` | 核对工具实际能力与必要输入；不复制历史命令直接运行 |
| `needs_review` | 已找到候选，先展开原始证据、核对后续纠正，再判断是否适用于当前任务 |
| `blocked` | 保留缺口和失败原因，补证、明确改写或等待真实状态变化，不循环尝试 |

所有结果都是 `history.context-gap.v1`，固定 `authorization=context_only`、
`current_verification=not_performed`。一次最多调用两次共享召回，每次最多保留三个候选。
`retrieval_calls` 统计本轮召回调用次数，不等于文件读取数、模型调用数或 Token 节省量。
退出码 0 表示成功生成计划/结果，1 表示存在 blocked 缺口，2 表示请求或读取错误；0 不代表任务完成。

候选只保留少量定位和摘要。案例先 `cases show CASE_ID` 展开，搜索证据按
source/store/session/index_revision/message_index 回源；随后可以使用既有 `materials` 导出。
压缩草稿、停止措辞、历史成功状态和检索排名都不能成为当前事实或执行授权。

## 防止反复检索

由调用方把上次 JSON 结果传回 `--previous`：

```bash
python3 -m history_core context --request request.json --retrieve --directory /path/to/problem-cases > result-1.json
python3 -m history_core context --request request.json --retrieve --directory /path/to/problem-cases \
  --previous result-1.json > result-2.json
```

当前任务、下一动作、项目、状态标识和来源范围不变时，同一缺口、同一查询复用先前结果，
成功候选、无匹配与失败都不重复查。一个缺口最多接受两种查询尝试；耗尽后需要新的证据或真实状态变化。
一次尚未处理的其他缺口可以在后续调用继续。把 receipt 留在现有任务上下文或计划产物中即可，
无需另建目录或永久索引；未传回 receipt 时不会产生跨进程去重。

工具不会为了检测变化额外打开历史。调用方在索引刷新、相关文件修改、来源修复或证据变化后更新
`state_revision`；不要为了重复尝试随意换 ID/状态标识。复用不代表来源仍然新鲜，候选仍需核对。

## 接入现有交接

```bash
python3 -m history_core --source codex --source-path /path/to/sessions --data-dir /path/to/cache \
  handoff SESSION_ID --context-request request.json --context-previous result.json \
  --case-dir /path/to/problem-cases
```

只有显式添加 `--retrieve-context` 才为交接执行本地召回。来源/案例范围应与生成 receipt 时一致。
Handoff 自身仍按原逻辑读取所选会话；无缺口零检索指新增恢复层，不取消原交接读取。
JSON 的 `context_recovery` 和标准/紧凑文本同时包含缺口、证据要求、候选定位与下一步。
历史 goal/verified/status 保持历史意义，当前任务的恢复结果另列，避免将两者混成已完成。

Python 调用使用 `history_core.context_gap.recover_context`；需要本地来源适配时使用
`history_core.context_cli.recover`。`retrieve` 回调是显式依赖，不提供它则只生成计划。

## 与 SpecMesh 的边界

任务计划文件保存当前任务状态；SpecMesh 提供仓库规范、当前事实与渐进索引；TraceMesh 负责
轨迹线索、缺口路由和历史证据恢复。检索 receipt 不是可直接导入的项目事实。
仍按 [问题压缩的材料导出说明](problem-compression.md) 和
[SpecMesh 检索契约](https://github.com/qiaoqiao2521/specmesh/blob/main/docs/RETRIEVAL-CONTRACT.md)
使用原生材料、精确绑定和当前证据复核。历史查询不覆盖规范、不自动晋升记忆。
