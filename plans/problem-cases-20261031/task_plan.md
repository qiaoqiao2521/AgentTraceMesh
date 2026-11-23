# 问题案例：专业纠偏与方案演变首版

## 用户目标

依据本次用户提供的产品复盘，围绕一个问题保存“现在怎样判断／为什么改变／下次怎样做”，先用既有三路反馈归因案例验证是否能帮助下一次任务。Agent 编写因果解释与关系，TraceMesh 只保存、检索、展示和导出。现行要求仍由项目文件负责；不把历史案例变成自动执行指令或当前验收。

## 复用与最小缺口

复用 `evidence.redact_text`、现有公开消息材料与修订定位、工作台和 CLI 的提前分派。`materials` 面向公众号、要求成果、采用快照写出，不改作本功能。仅补本地案例 JSON 的 save/list/show 与三视图 Markdown；不建数据库、向量索引、常驻同步或模型调用。首版不实现五项完整产品、不解析未知 AGY Boost 协议。

## 跨执行者契约

- 模块 `history_core/problem_cases.py`：`default_case_directory()`；`list_cases(directory=None, *, query='', project=None, offset=0, limit=20)`；`get_case(directory, case_id)`；`save_case(directory, data)`。
- 每项本地 JSON：`id`, `title`, `project`, `tags`, `question`；`now`（`judgment`, `adopted`, `task_status`, `knowledge_value`, `applicability`, `remaining`）；`requirements`（text/state/evidence）；`evolution`（id/kind/actor/text/evidence/replaces）；`reuse`（conditions/steps/avoid/unknowns）；`evidence`（id/kind，artifact 的 repository/path/commit/line_start/line_end，或 message 的原有完整定位与各自修订）。除 id/title 外允许缺项，缺证据保留未知；不按日期自动推断替代。
- `get_case` 与 `save_case` 返回 `{case, markdown, origin, ...}`；`case` 保持上述可编辑结构。`list_cases` 返回 `{items, next_offset, total, partial, errors}`，item 包含 id/title/project/question/tags/now 和 next_action。所有结果标注 `interpretation=authored_annotation`, `source_verification=references_not_revalidated`, `current_verification=not_performed`。保存/读取解释调用现有脱敏，保留 evidence 的身份与修订，不重新读取原始日志。
- 默认目录：共享 helper 解析本地用户数据目录下 `agenttracemesh/problem-cases`；读请求不创建目录。`history_core/case_examples/*.json` 提供公开 Git 文档引用的种子，用户同 ID 的本地 JSON 显式覆盖示例；损坏项单独报错，结果披露 partial，不伪装零命中。
- HTTP：`GET /api/reuse/cases?q=&project=&offset=&limit=`；`GET /api/reuse/case?id=`；`POST /api/reuse/case-save`，body 为案例 JSON，显式本地写入，沿用可信请求和 loopback 边界。新增可选 `--case-dir`。查询这些入口不初始化会话来源。
- CLI：`python3 -m history_core cases [--directory DIR] list [--query ... --project ... --offset ... --limit ...]`；`show ID [--format json|markdown]`；`save --file FILE`。无需 --source/--source-path。JSON 是人工可改主件，Markdown 从同一 getter 导出。
- 工作台新增“问题案例”标签：查询、读三视图、复制/下载同一 Markdown、刷新人工修改，URL `?view=cases&q=...&case=ID`。不依赖当前会话或已选交接片段，不新增网页编辑器。

## 验收与边界

1. 原案例引用公开版本化文件，明确私有原会话未核对、历史测试为当时记载，贡献不能凭空归到用户或任意 CLI。
2. CLI/HTTP/Web 共享读取结果；按早期“分页”建议也能读到否决原因和下一步；人工编辑无哈希门禁，个别坏文件不影响其它案例。
3. 来源引用缺失、未知关系、导出脱敏、HTML 注入、目录穿越、HTTP 来源、异步切换均必要回归。
4. 未参与历史任务的 Agent 通过新 CLI 查询案例，用在一项新的类似场景，检查是否找到纠正、适用条件、贡献与未知，并选出合理下一步。记录为一次迁移试验，不声称普遍效果或真实生产验收。

owner：根 Codex。模块/CLI/界面分开实施，根 Agent 负责接口、文档、串行整合与交付。采用知识库“历史工具检索”“解析、索引与知识回写分别验收”的原有分工。
