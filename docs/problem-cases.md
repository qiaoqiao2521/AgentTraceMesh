# 问题案例：找回纠偏，带入下一次任务

案例围绕一个问题组织跨会话解释。它同时回答“现在怎样判断”“为什么改变”“下次怎样做”，并保留要求、尝试、用户或 Agent 的纠正、取舍及证据。

解释和关联由调用 Agent 或用户编写。TraceMesh 提供本地保存、文字检索、读取与导出，不自动判断因果、替代、知识价值或需求是否完成。项目文件仍保存现行要求和决定，已有知识页保存可复用解释。

## Agent 最短入口

在仓库根目录执行，无需来源参数，不启动历史扫描或模型：

```bash
python3 -m history_core cases list --query 分页
python3 -m history_core cases show triple-review-attribution --format markdown
```

查询匹配标题、问题、标签和解释正文；结果同时给出案例中记录的当前判断及下一步，不把早期错误片段单独当作建议。排序不使用会话 value_score、工具调用量或测试次数。这是文字检索；未找到已编写案例不代表历史里没有经验，继续用原有 `search --brief` 和 `message` 定点查证。

案例不需要刷新历史才能阅读。若需要重新核实消息，按该条证据自己的来源、存储、会话、消息序号及修订进入已有接口；index_revision 不能冒充 content_revision/context_revision。修订变化不阻止修改案例，但应记录依据需重新定位。

## 保存和人工二改

每项案例是一份 JSON。默认目录在本机用户数据目录下的 `agenttracemesh/problem-cases`；Windows 使用本机应用数据目录。CLI `--directory` 和 Web 启动参数 `--case-dir` 可指定同一个目录。它保存人工标注，不是可随意清空的派生索引。

`--demo` 默认改用演示数据目录下的案例目录，不发现私人案例；只有显式 `--case-dir` 才使用指定目录。

```bash
python3 -m history_core cases --directory /your/notes/problem-cases save --file /your/draft.json
python3 -m history_core cases --directory /your/notes/problem-cases show your-case-id
python3 app.py --case-dir /your/notes/problem-cases
```

先参考[现有真实案例](../history_core/case_examples/triple-review-attribution.json)编写 JSON；`id` 和 `title` 用于定位，其余内容允许暂缺。保存会覆盖调用者明确指定的同 ID 本地案例，不需要内容哈希、前序完成状态或模型审批。直接编辑 JSON 后重新读取即可；内置示例可用同 ID 本地文件覆盖。

Web 工作台的“问题案例”与 CLI 使用同一读取结果。查询、刷新、复制和 Markdown 下载不依赖已选历史片段。页面显示本地可编辑路径；不把它放进面向公众号的成果素材表单。`/?view=cases&q=分页&case=triple-review-attribution` 可直接打开。

## 一项解释怎样写才有用

- `now`：当前判断、采用什么、任务结果、知识价值、适用范围和未解决项分别写。取消、替代、实现、验证和未知不混为一类。
- `requirements`：保留原要求与后续改变，说明每项状态的依据。历史“verified”是作者的历史判断，不是 TraceMesh 对当前项目的验收。
- `evolution`：按必要转折写 actor、text、evidence；`replaces` 只记录有明确依据的关系，不根据时间自动推断。技术解释与目标、授权的变更分别说明；不确定由谁贡献时用 unknown。
- `reuse`：写适用条件、下一次具体步骤、已否决路线和未知。下次使用前仍要核当前任务，不把旧指令自动执行。
- `evidence`：引用各自绑定的公开消息材料或版本化项目文件。保存定位和证据包原有的 partial、截断、未知边界；不复制隐藏推理、原样 raw record 或全部历史。

证据原文不足时不要补写因果或贡献归属。AGY 正文出现 Boost 字样不证明父子任务、派发、回传或结果采纳关系；需要对应版本的明确记录。Skill 标记只证明记录过触发，效果分析仍由 Agent 从动作、结果与纠正中判断。

## 接口

| 操作 | 入口 |
| --- | --- |
| 查询已保存解释 | `GET /api/reuse/cases?q=&project=&offset=0&limit=20` |
| 读取 JSON 和同源 Markdown | `GET /api/reuse/case?id=CASE_ID` |
| 显式保存一项 JSON | `POST /api/reuse/case-save`，请求体为案例对象 |

查询这些入口不会初始化或扫描历史来源。保存沿用 JSON／来源检查，并要求实际连接来自本机；不向模型或外部知识库发送数据。

列表的 `partial/errors` 保留坏文件和不可读项；一个坏案例不清空其它结果。坏的本地覆盖不会偷偷回退到内置示例。正文脱敏复用现有常见秘密规则，不保证完整匿名化；来源身份与修订保持原值。

所有结果明确标注 `interpretation=authored_annotation`、`source_verification=references_not_revalidated`、`current_verification=not_performed`。正常读写成功只证明标注已保存或可读，不证明它的专业解释正确、原始历史完整、当前任务已完成或方法普遍有效。

## 首版案例与验收范围

三路反馈案例来自 `plans/review-feedback-attribution` 的公开 Git 版本 `1a4ee95d55fbc4cba3c6e9af4b6d1e47770fbe02`。它说明有限材料派遣不能证明产品缺少分页，并保留实际采纳的可读时间改进。历史测试数字是当时记录；本轮不以此声称复跑了原任务，也没有公开私有反馈和原会话定位。

本轮实现与一次独立 Agent 迁移试验记录在[任务进度](../plans/problem-cases-20261031/progress.md)。它不代替技术栈淘汰、真实 AGY 多代理和跨项目生产场景的后续验证。
