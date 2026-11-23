# Progress

## Current

2026-10-25 follow-up：自身长会话预算排查与用户方案检索的实现、59 项针对性测试和定点实测已完成。知识增量已普通推送 `e72a25e`，源码随本次提交交付。详情见下节；2026-10-10 记录保留为当时事实。

## 2026-10-25 Scoped retrieval

- `search --brief --session ID --role user|assistant|tool` 在候选分页与摘录前筛选。完整索引正文参与检索；用户方案保留绝对消息编号、字符窗口、修订与公开过滤。
- 修复工具配对的全消息表扫描。可选配对预算失败保留命中，并返回明确错误和 partial；其他数据库错误继续披露。
- 验证：59 项相关 Python 测试通过；独立子 Agent 的 SQLite 指令计数回归能检出旧 JOIN 的性能退化；新增 50000 条旁会话不会明显增加目标配对工作。
- 真实复测：当前约52MB、5874条索引消息的录制，固定捕获后查实际旧失败及用户补充。工具查询约2秒中断→139ms返回配对，用户查询30–64ms；录制仍追加，尾部缺口与 partial 保留。不是全库覆盖、全部 activity/markers 性能或已安装 GUI 的验收。
- 知识收尾：`updated`。原 `Wiki/自动化开发范式与智能体协作.md` 的“历史工具检索”新增预算分类与用户粘贴方案检索；`Wiki/开发知识入口.md` 同步触发词。两页已在 canonical 与远端基线隔离工作树实际读回，连同此前本地 receipt 段落普通推送至 `qiaoqiao2521/my-programming-world`（`e72a25e`）；无原始会话入库。
- 原遗留材料归属、保留原因与最短接续入口沿用 [findings](findings.md) 的既有工作区段：每日活动私有测量/实验锁不入库；SpecMesh 实验 fixture 保留；键盘恢复与 CM 迁移待根 Codex 在原任务核验。
- 知识主工作区仍有此前内容编辑和与远端的分叉。本次只从远端基线交付已读回的历史检索增量；媒体投递、队列章节及媒体入口变动保留。接续 owner：根 Codex；先对照对应项目已有计划与当前远端原页，不覆盖两边独有内容。
- 剩余边界：全库 activity 原始记录统计、未限定 markers 的查询预算另按其 SQL 计划定位；既有源码入口可用，本轮未更换安装包。agent-link AGY 参数失败转为内置子 Agent，未修改外部 CLI 配置。

2026-10-10：代码、公共 CLI/Python 接口、4处原知识页及共享标记约定已交付。源码与知识库均普通推送，GitHub API 读回修订及4页哈希一致。下文 2026-10-08 的验证保留为前轮事实。

随后用户要求查看 Learning 插件可保存的观点：从所选近期录制核对两条工程经验，另保留一条插件设计原则。3条已写入两处原知识页并同步入口，远端修订与内容哈希已核对；详见本页 Learning knowledge follow-up。

## 2026-10-10 Recheck

- 修复 brief 候选排序、完整短正文、有界 Call ID 输入/结果关联及独立错误状态；默认 search 排序保持兼容。
- `message` 用 search 的同一修订直接分页展开；完整选中消息先识别秘密范围再输出等长遮罩窗口，空字符与长 Call ID 边界已验证。
- 修复内层失败被外层元数据覆盖、stdout 误判退出码、原生业务 JSON 丢字段与畸形 Call ID 崩溃；正式旧缓存公开刷新恢复正文及状态。
- 按用户最终确认新增 `markers`：Learning 指令注入、原生 Skill 请求、Agent 声明分别标记；按名称、会话与录制工作目录查定位，不取隐藏正文、不新建表或反馈数据库、不评价作用。
- Codex parser v8、Claude v6；公开刷新恢复旧缓存标记。Claude 混合 hook 内容逐元素定位，Codex 缺失新 turn ID 不沿用旧值。
- 误扩的新 skill 草稿已从默认库撤出；没有安装、helper 或反馈数据库。现有 tracemesh-recall 正文恢复 0.4.1。共享 AGENTS 只增加用户确认的日志标记约定，Claude/Codex 原有入口均使用它。

## 2026-10-10 Verification

- `PYTHONPATH=tests:. python3 -B -m unittest discover -s tests -p 'test_*.py' -q`：649 项通过，1 项既有跨仓 reader 配置跳过。
- workspace JS、tool-collapse 18/18 与 diff 检查通过。76 个受影响项目 Markdown 相对链接、28 个知识页 wikilink/锚点通过。
- 独立审查报告的解析、分页脱敏、巨大 Call ID 和两处 marker 归因缺陷均先复现后修复，公共 Reader/CLI 回归通过；源录制字节保持只读。
- 所选当前 Codex 录制读回 4 个 Learning 注入标记与 1 个 Agent 声明。第一次活跃文件前后哈希不稳定；后续有界核对源字节不变，不推广为全库捕获。

## 2026-10-10 Knowledge closeout

状态：`updated`。根 Codex 串行修订 canonical 原页，并将本轮 delta 应用到 fresh origin/main 的隔离工作树。4 页已实际读回，28 个链接/锚点已核对，交付哈希留在本机缓存。

- `Wiki/开发知识入口.md`：工具配对、直接消息展开及技能/插件日志标记路由；浏览器入口同步插件优先。
- `Wiki/开发协作接入.md`：“规划时”一次有界查询四项；“执行中”现有对话标记与后续 Agent 的职责。
- `Wiki/自动化开发范式与智能体协作.md`：“历史工具检索”“技能触发日志”“解析、索引与知识回写分别验收”；保留原有主题。
- `Wiki/浏览器自动化与登录态.md`：“后台运行与桌面占用”仅修订入口次序，没有执行账号操作。
- 四态、pending owner 与原页读回约定为 `already_covered`，没有重复改写。

交付状态：已完成。根 Codex 普通推送代码及4页，GitHub API 读回 main 修订与交付提交一致，4页 SHA-256 与交付快照全部相同。原知识主工作区分叉及其它进行中主题保留，详见 findings。

## 2026-10-10 Delivery

- 源码提交：`ba20943c1fe0de26775bc479f57bff43cad533a6`，已普通推送至 `qiaoqiao2521/Codex-Claude-History-Viewer` 的 main，GitHub API 修订读回一致。26个已审阅文件单独暂存；原始历史、运行日志、本地测量及禁提交 fixture 未入库。
- 知识提交：`9175559639553e30d4f741b22589156cad91774b`，从当时 fresh origin/main `a0da9f2f18b3df4a65c0691f9d3f87846408f9a3` 应用本轮 delta 后普通推送至 `qiaoqiao2521/my-programming-world`。保留该基线的 dots 等近期内容，没有覆盖 canonical 主工作区的既有分叉及修改。
- 共享 AGENTS 标记段已实际读回；Codex 链接、Claude 引用入口核对一致。tracemesh-recall 与本轮修改前备份逐字节相同，未交付新 skill 或反馈数据库。
- 独立只读复核确认文档与接口的能力边界一致；架构文档旧解析版本已同步。此次收尾只更新交付记录和文档，不改变已验证代码。

| 远端原页 | 读回 SHA-256 |
| --- | --- |
| `Wiki/开发知识入口.md` | `d6f6fc3dcf393be781301dc9f919117dae9fb3b1dec8bbe94b18819e67f03862` |
| `Wiki/开发协作接入.md` | `0f08679d04d917c7bc2bdf87f0bbaac1721a87f4e346c61c3b044627546d245d` |
| `Wiki/自动化开发范式与智能体协作.md` | `5c123648522ecc0d0d6a8ac0cc3b3a6f92f4948439da20109a81105bc1b78042` |
| `Wiki/浏览器自动化与登录态.md` | `a53082d840c594ba1d445c53498ceb6a0cac8d0f5064a85cc2070611a84a651e` |

## 2026-10-10 Learning knowledge follow-up

知识收尾：`updated`。根 Codex 从 Learning 标记定位所选近期录制，定点展开 Insight、替身测试、持久化状态及故障分支；原始录制哈希与所选证据修订一致。只保存可复用解释和最短定位，没有将原始日志入库，也没有修改插件或技能。

- `Wiki/自动化开发范式与智能体协作.md`：新增“远端任务恢复：先确认提交边界”“诊断信息：保留代码位置和必要计数”“教学参与：围绕真实决策解释取舍”。前两条绑定当时工具证据，第三条注明插件设计原则，未评价学习效果。
- `Wiki/开发知识入口.md`：同步上述3个问题入口和锚点。此前准确覆盖的工具检索、配对、状态、缓存迁移、分页脱敏及标记边界等7项为 `already_covered`，没有重复扩写。
- 两处原页实际读回，24个 wikilink/锚点与 diff 检查通过；独立只读复核确认没有将具体 adapter 协议推广为通用协议、替身测试提升为真实交付或插件触发提升为因果效果。
- 从 fresh origin/main 的隔离工作树普通推送至 `qiaoqiao2521/my-programming-world`，提交 `d910d321e8984e7d6ba9bfd11d7c7e4e2a2ac839`。GitHub API 读回 main 修订一致，两页 SHA-256 均匹配交付快照；canonical 分叉和其他进行中修改保留。

本次仅限定日期刷新 Codex 派生缓存，查询始终披露 partial 和未捕获可能；没有推广为全库或 Claude Learning 已验证。上文4页哈希保留为此前交付快照，最新两页内容以本次追加提交为准。

## 2026-10-08 Done

- 修复原生对象、JSON 封套、文本块和业务 JSON 正文；展开与折叠共用归一化。显式失败优先，深层结构有界，媒体检查基于最终显示正文。
- 正文检索排除上下文、内部思考与未分类记录；工具输入/结果和相关用户消息优先摘录，绝对消息编号保持不变。
- 增加 `search --brief`；保留来源、索引修订、展开定位及内容覆盖警告。零命中不消除未知 AGY step 的覆盖警告。
- Codex 解析版本 5 → 6；公开 reader 验证刷新旧派生缓存后恢复正文，原录制保持只读。
- 已读所有本次所涉 Git 状态和既有任务约束；保留本地原始测量、禁提交 fixture、未验收的系统恢复和独立嵌套仓库，接续入口见 findings。

## 2026-10-08 Verification

- `PYTHONPATH=tests:. python3 -B -m unittest discover -s tests -p 'test_*.py' -q`：586 项通过，1 项既有跨仓 oracle 配置跳过。
- `node tests/test_workspace.js` 通过；`node tests/test_tool_collapse.js` 18/18 通过。
- 独立审查的 JSON 正文、覆盖提示与媒体包装旁路均先复现后修复；公开 reader 合成检查与源哈希核对通过。
- 最终代码使用新派生缓存定点展开一份旧录制：12 条工具结果均保留正文，无空值或 unsupported；所选日期的源哈希不变。日期覆盖仍为 partial，不推广为全库已重新解析。
- 67 个项目 Markdown 相对链接有效；23 个知识页 wikilink 与标题锚点有效；skill `quick_validate.py` 通过；diff 检查通过。

## 2026-10-08 Knowledge closeout

状态：`updated`。根 Codex 汇总、读回并通过隔离工作树交付：

- Obsidian `Wiki/开发知识入口.md`：13 类问题路由及“历史工具检索的最短入口”，同步旧页新增触发条件与源码定位。
- `Wiki/开发协作接入.md`：“规划时”与“在已有任务进度中记录回写结果”，定义四种结果、原页读回和 pending owner。
- `Wiki/自动化开发范式与智能体协作.md`：“历史工具检索”与“解析、索引与知识回写分别验收”；保留并同步入口依赖的近期经验。
- `Wiki/浏览器自动化与登录态.md`：“后台运行与桌面占用”，整合已完成并行任务的有效经验，保留原页其他内容。

知识库普通推送：`qiaoqiao2521/my-programming-world` → `e375896fb92d652391dea226e14009b76640fbdd`。GitHub API 读回远端 main 与 4 页内容，逐页哈希匹配交付快照；主工作区历史分叉、未提交文件和嵌套 Git 原样保留。

共享 `~/.agents/AGENTS.md` 已加入有界工具查询与知识收尾规则；`tracemesh-recall` 0.4.1 已改为 `--brief --limit 3` 并读回验证。两者保留修改前备份。当前机制是 Agent 执行约定，不宣称存在运行时自动回写 Hook。

## Remaining

本轮授权范围已完成，没有待交付代码或知识页。既有本地材料的保留原因、接续 owner 及恢复入口见 findings。

## 2026-10-08 Delivery

源码成果提交：`2f8328630459495399ce481eb38629d935ca318e`，已普通推送到 `qiaoqiao2521/Codex-Claude-History-Viewer` 的 main。GitHub API 读回 main SHA 与本地提交一致。仅暂存本轮 19 个已审阅文件；原始历史、运行日志、本地测量与禁提交 fixture 没有入库。

知识库远端提交和 4 页内容哈希已在 Knowledge closeout 分别核对。共享规则与 skill 属于本机文件，保留修改前备份。此后仅追加本任务的完成记录，不改变已验证代码。

## Issues

活跃录制会导致全库刷新时源变化；本次使用合成录制检验缓存迁移，并限定日期与会话进行真实读回。此前实现过程产生的临时 v6 派生缓存不是正式旧版迁移依据，最终真实检查使用新缓存。

源码与共享标记约定已更新，原 recall 技能正文保留；未替换既有打包 GUI 安装，未将本轮声明为全库历史覆盖或外部应用验收。

后续待复查：旧全库派生缓存的未限定会话 `markers --name learning-output-style --kind output_style --limit 3` 返回 `OperationalError: interrupted`，不能当作零命中。限定会话查询可返回，限定日期显式刷新后标记与公开消息查询通过。根因尚未核验，本次未改查询实现。接续 owner：根 Codex；最短入口为同一旧全库缓存的该查询与限定会话对照，先核对规模、缓存版本和 SQL 计划，再决定修复范围。

## Next

以后从 `search --brief` 或 `markers` 进入；显式 refresh 后读取新解析标记。标记记录事件，后续 Agent 再判断效果并在原技能仓库调优；日志本身不授权或执行调优。
