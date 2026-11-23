# 问题案例首版 QA

2026-10-31，基线 `9e4fab3`。根 Codex 做隔离浏览器和实际 HTTP 验收；两个内置子 Agent 分别检查存储/CLI、网页/API，并实施指定的局部修复。没有读取私人历史或替换原有运行服务。

## 已复现并修复

| 问题 | 修复前 | 修复后与验证 |
| --- | --- | --- |
| 结构化秘密字段未脱敏 | `password` 为对象、`token` 为数组或秘密字段为数字时，原值进入保存文件、case JSON 和 Markdown | 复用既有秘密字段词汇，命中后整值掩码；save/get/list/磁盘和实际 HTTP 回归均不含合成秘密。`authorization=context_only`、来源身份及修订不变 |
| 案例深链切到历史搜索缺少来源筛选 | `?view=cases` 启动后，点“搜索历史”只有“全部来源” | 首次进入历史搜索或项目页才初始化来源；独立案例启动仍不查来源。实际 demo 页面出现配置的具体来源选项；回归覆盖 pending 时切换去重和较新回执优先 |
| 下一页失败清空已读结果 | 首批20项后断网，点“加载更多案例”会变成0项且按钮消失 | 保留20项、原 offset/筛选及可用重试入口；实际浏览器离线失败后仍20项，`set offline off` 并重试变24项；同页重试无须刷新 |
| 深层 JSON 缺少稳定错误回执 | CLI save 打印 traceback、退出1；HTTP `now.judgment` 1500层数组使连接断开 | CLI 输出结构化错误、退出2；HTTP 返回400 `case_nesting_invalid`，不建案例文件，随后正常保存和读取仍可用 |

每项均有修复前失败复现。HTTP 复现保留实际 loopback 服务，网页分页使用真实浏览器离线开关；没有用独立 mock 的成功代替现场重走。

## 本轮检查

- `python3 -B -m unittest discover -s tests -p 'test_problem_cases*.py'`：44项通过。
- `python3 -B -m unittest discover -s tests -p test_evidence_selection.py`：13项通过。
- 9套 `tests/test_*.js` 全部退出0；`node --check static/workspace.js`、`git diff --check` 通过。未机械复跑前轮776项全量基线。
- 用“分页不足”检索命中1个真实种子，三视图、后续纠正、贡献未知均在；页面正文与 API Markdown 完全一致，仍标注 `current_verification=not_performed`。
- 390×844 页面文档宽度390，无水平溢出，截图已查看；浏览器没有捕获的 JS 异常。离线产生的网络失败是本轮主动注入条件，demo favicon 404 不作为新功能阻塞。

## 现场与证据边界

本机选定截图与现场记录位于 `/home/muqiao/.codex/visualizations/2026/10/31/tracemesh-cases-qa/`。`issue-002-*` 对比来源筛选，`issue-003-*` 对比失败、保留与恢复，`case-mobile-final.png` 为手机最终页面。只含公开种子和合成案例。browser-bg 不支持 record，未录制视频；不把截图当未记录的完整操作视频。

曾确认服务已提供新脚本、页面仍持旧脚本；刷新脚本缓存并重载后，页面才包含本轮修复，再执行最终操作。早期 `offline true/false` 调用未能可靠恢复网络；最终复测按 CLI 文档使用 `on/off`，并用 `navigator.onLine` 确认。上述尝试不作为修复通过证据。

真实私有长会话、真实 Windows/WSL、生产运行服务和安装包未纳入本轮。显式 `--case-dir` 在 demo 下仍使用指定目录，属于已文档化的主动覆盖，没有将其误报为默认私人目录泄露。

## 收尾

仅保留本轮选定 QA 证据，临时24项案例、demo缓存及启动元数据已删除（清理前约2.1 MiB），隔离服务和命名浏览器均已关闭。既有未跟踪任务材料、锁和运行服务继续保留，接续 owner 与入口沿用 [progress](progress.md)。

知识状态 `already_covered`：已读回 Obsidian `Wiki/自动化开发范式与智能体协作.md` 的“验证入口跟随实际调用路径”和“问题案例：保留纠偏，再检查迁移”。实际入口、当前版本和解释/证据分离已覆盖本次认识；不重复增写知识页。前轮知识库 Git 发布 pending 仍由根 Codex 接续。
