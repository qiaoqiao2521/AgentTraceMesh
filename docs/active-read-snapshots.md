# 活跃对话：固定读取与纯素材

这是当前源码新增的无头能力，不代表既有安装包或 GUI 已更新。历史来源只读；对话可以继续写入。

## 先读现有索引

保持同一 `--source-path` 和 `--data-dir`。改成日期子目录会进入另一套缓存。`health` 的 `readable` 只表示入口可读；`has_index` 和 `indexed_sessions` 才说明该缓存是否已有会话，新鲜度仍为 unknown。

`search`、`message` 和 `materials` 不隐式刷新。派生消息索引使用独立只读连接与短 SQLite 事务，一次调用内固定候选、正文与 revision。另一个进程可以继续提交更新。

跨独立调用仍须传同一 `index_revision`。索引已更新时旧定位明确失效，重新搜索；不会后台保留每个历史版本。需留存时保存已导出的少量素材包，不长期保持读事务等待 Agent 思考。

## 只捕获目标 Codex 文件

在仓库根目录执行。下面的路径和会话 ID 是占位符，替换为已定位的目标。

```bash
python3 -m history_core --source codex --source-path /path/to/sessions --data-dir /path/to/cache refresh --file 2026/10/13/rollout-target.jsonl
```

`--file` 可重复，来源根保持不变，未选会话不删除。未传 `--file` 时，显式 Codex refresh 按会话捕获；稳定且版本相同的记录复用缓存。其他来源保留原刷新契约。

捕获固定文件对象与初始长度，只收完整 JSONL 行。parser 与 audit 使用同一份捕获内容，结束时复核前缀和文件身份。正常追加属于下次覆盖；半行保留为未覆盖尾部。检测到前缀改写、截断或文件替换时，该会话失败并保留旧行，其他成功项仍可提交。

每会话默认最多捕获 128 MiB，可用 `--max-bytes` 显式设置；单行另有上限。达到限制、未收尾部与失败均要看回执，不表示全会话已覆盖。保存的 `source_capture` 包含捕获字节水位、观察大小、前缀哈希和时间。文件 stat 不是内容证明，复核也不是对任意恶意并发写的原子文件系统快照。

首次读取仍需解析目标范围。此版复用原 parser，不宣称已实现任意长会话的常量内存或按尾部游标增量解析。

## 导出纯素材

```bash
python3 -m history_core --source codex --source-path /path/to/sessions --data-dir /path/to/cache search --query '关键词' --brief --limit 3
python3 -m history_core --source codex --source-path /path/to/sessions --data-dir /path/to/cache materials SESSION_ID --index-revision REVISION --message 12 --message 19:2000
```

从搜索结果取得真实 revision 与绝对消息编号。`--message INDEX[:OFFSET]` 的 OFFSET 是索引正文字符位置。每片最多 2000 字符，每包最多五片、正文合计 8000 字符，最终 JSON 不超过 64 KiB；超限明确报错，按返回的 `next_text_offset` 分包。

素材沿用公开消息过滤、脱敏与 Markdown 围栏，不含验收提示词。完整选中消息先识别秘密范围，再遮罩窗口，避免跨页泄漏；模式脱敏不等于完全匿名。

绑定字段是 `index_revision` 和 `selected_fragment_hash`。完整源的 `content_revision`、`context_revision` 均为 unknown。`source_capture` 是索引当时的覆盖观察，`partial` 明示这里只选取部分内容，不将所选原文认定为事实或实际验收。

普通 `handoff` 和 model-review 导出继续保留原源签名、2 MiB 前缀及完整消息绑定要求。纯素材入口不绕过它们，也不把它们的提示词混入素材。

## 读回错误

上述单源 CLI 的异常回执增加 `phase`、`error_code` 和 `elapsed_ms`。摘录或关联工具查询失败可定位到 `snippets`、`related_tool_messages`；`interrupted` 映射为 `query_budget_exceeded`，不会被当成零命中。

brief 的可选工具配对预算超限会保留已取命中，并在候选的 `related_tool_messages_error` 和 `partial` 中披露。已知目标时可用 `search --brief --session ID` 缩小范围；用户粘贴的解法另用 `--role user` 检索。详见[检索边界](search-semantics.md#缩小查询与找回用户粘贴的解法)。

SQLite VM 预算只约束相应查询，不是文件读取、完整消息脱敏或整条流程的硬超时。先看阶段，不单凭总耗时调大预算。

Codex refresh 的退出码：0 表示本次选择范围没有已报告缺口；1 表示 partial，查看各会话回执；2 表示调用失败。刷新成功不证明实时全库完整或业务已验收。
