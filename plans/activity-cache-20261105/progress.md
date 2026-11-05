# Progress

源码与有界现场验收完成。三个内置worker负责ordinal、AGY缓存与timeline，根Codex串行接线、复查与复测。没有新增外部调度或数据库服务，用户预算手柄保留。

## 清单落实

| 项 | 本轮结果 |
| --- | --- |
| 空派生索引 | 在已有Reader搜索回执外补聚合API的来源计数、警告及refresh请求；新timeline同样带刷新命令和partial捕获说明。 |
| 活动预算 | 新消息持久绝对编号，旧缓存窄键兼容；聚合和分页后再读会话metadata与正文，不扩大默认2秒。 |
| AGY竞态 | summary/WAL观察变化重试一次，含WAL检查期间删除；初始化错误保留结构化路径/大小/预算，静态单条超限继续降级。 |
| 默认输出/brief/争用 | 保留前轮compact、verbose、跨源最小字段和source_busy/query_budget_exceeded/query_interrupted区分；128项相关回归覆盖。 |
| 标题 | AGY保留有意义的原摘要，并返回真实用户请求title_candidates，均经过公开投影及脱敏。 |
| AGY跨命令 | CLI默认XDG派生缓存，Web按provider保存；Python显式data_dir启用。warm不解码，变化会话增量更新；缓存故障明确warning并降级内存。 |
| AGY/ZCode按日期 | 新增统一timeline，修订绑定、短公开事件、[start,end)筛选与原message定位；activity认证范围仍为Codex/Claude/mcode。 |

## 验证

- 全套 `python3 -B -m unittest discover -s tests`：871项，870通过、1项可选跳过，38.267秒。随后独立审查发现边界并补测，不拿这次全套结果覆盖后续修改。
- 最终源码相关回归：128项全部通过，8.609秒。入口：在 `tests/` 运行 `PYTHONPATH=.. python3 -B -m unittest test_activity_cache_cli test_snapshot_reader_cli test_native_timeline test_activity_ordinals test_activity test_agy_persistence test_agy test_history_core test_reuse_contract`。包含最终WAL竞态、timeline空索引/部分捕获和参数前置拒绝。
- AGY focused 45项全部通过，4.000秒。跨进程测试使用真实新Python进程，禁止快照/解码仍可warm搜索；覆盖单会话变化、损坏cache、并发首次使用、失败回滚和源只读。
- 既有3,932,028,928字节主缓存，以SQLite `mode=ro`、`query_only=ON`及读事务测试2026-11-02 UTC有效日窗。默认预算2秒：原结构约2.303秒失败，修后worker约0.458秒；根Agent独立复测0.3961秒，10会话、20证据。未刷新源、未迁移真库schema、未输出私人正文或身份。暖缓存结果不代表冷IO或所有窗口。
- 旧缓存249会话缺解析期覆盖计数，返回未知；freshness未知，汇总仍partial，不能宣称完整覆盖。完整1.2GB AGY冷热耗时、真实长期追加混合负载没有本轮现场复测。
- `git diff --check`通过；8份相关Markdown相对目标检查无缺失；CLI timeline help检查通过。未修改前端静态资源，因此未重新跑浏览器/UI验收。

## 知识、安装与遗留

知识状态 `updated`：Obsidian `Wiki/自动化开发范式与智能体协作.md` 的“历史工具检索：定位原文再借鉴实现”，补充窄列分页/持久编号、AGY缓存及timeline适用边界；`Wiki/开发知识入口.md` 增加对应检索行。根Agent已读回正文和索引。仅本地写回；知识仓既有大段改写尚未整体审阅，发布 `pending`，owner根Codex，入口为这两页的git diff，不把局部更新冒称整库已交付。

本轮验证运行工作树CLI；未替换既有cchv安装包或长期Web进程。此前8787健康探测不可达，未启动替代服务；5196是另一个项目，不作为本项目验收。安装接续owner根Codex，先核 `health.reader_module`、安装路径与启动器，再按本计划有界命令复验，不重复全库刷新。

此前未跟踪的daily-activity验收/JSON/verify_real.py、specmesh-r001-validation、ubuntu-keyboard-recovery、agents迁移笔记和search-hardening两份锁保留。它们的审阅和恢复入口沿用 `plans/evidence-reliability-20261031/progress.md`，owner根Codex；缺完整审阅/部分可能含本地证据，不混入本轮提交，不将锁猜为废弃。没有丢弃进行中工作。

## 清理与Git

临时fixture由TemporaryDirectory自动回收；只读真库连接已关闭。没有启动浏览器、Web或后台Agent进程。测试临时日志在核对结果后删除；未复制真实大数据库、未创建录制/渲染副本，不清理其它任务缓存。本轮测试日志清理前984字节，清理后0字节；源、既有缓存和遗留锁保留。

Git交付进行中；仅暂存本轮代码、测试与项目文档。有效账号qiaoqiao2521（238822818），目标既有qiaoqiao2521/AgentTraceMesh，main；远端结果在完成后追加，不把本地commit写成已推送。
