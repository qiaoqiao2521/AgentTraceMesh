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

实现提交 `df26471b09c53ca249f09693ae970f7705263a99` 已推送至既有qiaoqiao2521/AgentTraceMesh main，并经ls-remote读回一致。账号qiaoqiao2521（238822818）与公开仓库身份已核对，未改可见性。仅交付本轮28个代码/测试/项目文档路径，既有未跟踪遗留均保留。本段交付回执以独立文档提交收尾，不改写实现提交。

## 追加任务进行中

2026-11-05用户授权本机安装更新、真实AGY大库冷建/跨命令复用及缓存轻量化。根Agent负责安装、实测与串行整合；内置cache_footprint只读盘点~/.cache/cchv*，不访问正文或删除。当前尚未承诺19G均可清理，先核占用与独有数据。

### 实测与轻量化已执行

- 旧current已切到0569dd4；干净包verify_release通过，实际cchv --version可用，保留rollback-before-activity-cache-20261105。当前补交CLI默认缓存与cchv reader入口，最终包安装后再核修订。
- 真实AGY源DB/WAL 1,280,094,208字节，259条摘要。初次默认cache冷建10.4479秒；独立空派生目录复测8.8102秒，VmHWM100.05MiB。这里“冷”仅指未建派生缓存，没有清系统page cache。热health0.0198秒、search0.2298秒、timeline0.0551–0.0597秒，分别新Python进程，均0解码/0源快照、相同修订。热进程VmHWM约31.2MiB。
- 默认AGY派生库252,571,648字节。251会话decoded_text、4条metadata-only（1 legacy、2缺失、1逐会话超限）、4条部分解码；326未支持step，结果仍partial。没有提高64MiB会话限制。
- SQLite checkpoint(TRUNCATE)回执[0,0,0]，回收主库3.410GB WAL；主目录和1条recall人工meta保留。
- 17个不活跃缓存流式压成2,374,773,250字节tar.zst，完整读取134个成员验证；归档时/删除前均无占用或变化，再移除9,243,955,200字节展开副本，净回收6,869,176,320字节。缺失原录制的旧cchv-codex完整库在归档中保留，不声称能refresh恢复。
- fresh Codex缓存通过同文件系统rename迁入默认reader根，未复制、未重建。加新AGY缓存后，主Web+统一reader+旧库归档allocated合计10,340,302,848字节（9.63GiB）；原20,367,929,344字节（18.97GiB），净释放10,027,626,496字节（9.34GiB）。Web与CLI仍有不同历史/元数据，暂不强并。
- 新CLI默认cache回归40项通过；根Agent重跑12项通过，sh -n scripts/cchv与diff检查通过。不重新跑不相关全套。具体复用、退役目录及恢复入口见docs/local-cache.md。

### 最终安装与交付

本机current现为~/.local/share/cchv/releases/1.3.0-rc.3-fbd1ec7，BUILD_INFO源码fbd1ec7f7d358c6d3e59d73b0ed97262f1206f60。沿用1.3.0-rc.3兼容版本号，不冒称发布新稳定版。最终包verify_release通过；实际~/.local/bin/cchv启动隔离demo的/api/version也返回此源码修订，验收进程已停止。

新增cchv reader入口不改变调用cwd；在/tmp实际调用health定位到安装目录，默认Codex缓存199会话直接复用，未refresh。真实AGY在最终安装下仍warm、259会话；探针再次证实0解码/0源快照。通过实际启动器测完整新进程（含shell/Python启动）health0.2718秒、search1.4747秒、timeline0.2081秒；内部main耗时则约0.0163/0.2444秒，口径不同，不把内部时间写成完整进程墙钟保证。

回滚原安装：将current原子切回rollback-before-activity-cache-20261105指向的c47bbc8，并恢复~/.local/share/cchv/launcher-before-activity-cache-20261105.sh至~/.local/bin/cchv。旧release与旧启动器均保留。回滚入口已保留，未在真实历史上执行回滚或重复刷新。本轮verify_release未请求previous-root，不以旧合成记录冒充本轮回滚验证。

知识状态updated：Wiki/自动化开发范式与智能体协作.md“历史工具检索”增加统一入口、WAL与唯一历史归档的处理；Wiki/开发知识入口.md增加缓存膨胀/WAL/cchv reader触发词，均已读回。沿用已有coverage限制；知识仓大段既有修改未全审，仍仅本地回写、发布pending，owner根Codex。

本轮仅新增CLI默认目录与入口，没有重写索引存储或调度。AGY不再每条命令重建，但部分格式/缺失/超限仍partial；主Web与CLI因历史和元数据不同，尚未强行合并成单DB；压缩归档需恢复后才能搜索。既有未跟踪验收/锁原样保留。

本轮临时zip/校验旁账/只含汇总的测试回执/探针脚本与中间0569dd4安装已清理，逻辑共9178021字节；独立冷测252,571,648字节临时cache已先行删除。最终release、原安装回滚、活跃cache和2.375GB可恢复归档保留。原会话录制未删除，未遗留本轮测试服务。

最后核对发现README、活动/搜索示例和知识入口仍显式指定旧cchv-reader/cchv-activity路径，会绕过新默认并重建已退役副本；已同步为统一reader根。日常活动示例移除默认refresh，首次空缓存或有新录制时按回执显式处理。知识入口两条最短命令已读回，避免规则与示例相互冲突。最终安装源码仍为fbd1ec7，后续提交仅为文档与实测回执。

## 2026-11-07普通搜索修复进行中

上轮检索中Codex的“思维 升华”“摘要 原文”“problem-cases”均在search阶段query_budget_exceeded。当前安装fbd1ec7、229个已索引会话。没有把失败当零命中，也不因上次activity优化通过就宣称普通搜索已修。既有未跟踪材料与锁保留。知识采用状态already_covered：Wiki“历史工具检索”已有轻量键与实际代码核对原则；本轮按用户要求只改TraceMesh，不改知识库或原矿方案。

### 普通搜索源码修复

- 窄索引(session_id,role,kind)过滤其它角色及私有kind后再读正文；现有真缓存只补索引约45.4MB，schema补齐约0.81秒，未更改源码录制或重解析全库。
- 排名/匹配在分页前完成，物化的是会话键、时间与数字排名；会话大JSON在limit+1之后读取。合成50候选metadata访问从50次降至最多4次；5000条其它角色噪音不再线性增加user查询VM。
- 仅无ASCII字母的排名词省lower副本；保留原instr/NUL、大小写、字面转义语义。尝试LIKE+NUL回退无收益，未采纳。
- 已独立复核CTE内外排序、跨消息AND、role过滤、limit+1/offset及修订/绝对定位；代码无新增账号限额或自动刷新。旧AGY暖缓存缺窄索引时沿既有schema迁移补齐，新增跨进程回归禁止decode/snapshot仍warm。
- 最后源码改动后，相关92项通过（5.019秒）；AGY focused46项通过（3.383秒）。最终合并回归与实际安装还在执行，不以工作树通过替代本机入口验证。

与本轮无关的既有未跟踪验收/锁/迁移材料继续按上文owner与入口保留。用户明确原矿包和知识沉淀自己处理，本轮未写Obsidian、未新增skill或导出功能。

最终合并相关回归159项全部通过，8.880秒；包含新查询成本、原搜索/长正文/角色/快照、activity编号、CLI默认缓存、AGY持久化及native brief。6份相关Markdown相对目标无缺失，git diff检查通过。

### 2026-11-07本机安装与真实回归完成

实现提交46265ec1445102ff277e1f44e48caa6dd2780977已推送至qiaoqiao2521/AgentTraceMesh main。当前实际安装为~/.local/share/cchv/releases/1.3.0-rc.3-46265ec；已原子切换current，原fbd1ec7保留为rollback-before-search-query-20261107。当前cchv启动器未改。干净包verify_release通过，API runtime_identity为46265ec；未请求previous-root，不宣称已执行真实回滚。

从/tmp调用实际cchv reader（移除环境预算覆盖）在同一229会话Codex缓存上：

| 查询 | role | limit | 完整新进程墙钟 | 返回会话 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 思维 升华 | user | 3 | 0.1504秒 | 1 | partial=false、无错误 |
| 摘要 原文 | user | 3 | 0.1798秒 | 3 | partial=false、无错误 |
| problem-cases | user | 3 | 0.1450秒 | 1 | partial=false、无错误 |
| 摘要 原文 | user | 10 | 0.1827秒 | 4 | partial=false、无错误 |
| 摘要 原文 | 不限定 | 3 | 1.9400秒 | 3 | partial=false、无错误 |

health.reader_module明确指向46265ec安装路径；index_revision仍与修复前相同，未刷新源录制或重建派生正文。默认2秒为SQL阶段预算，完整进程包含解释器、候选、摘录与脱敏，不能混用两种时间。上述为当前有页缓存/当前负载的实测，不承诺所有词或冷IO都低于2秒，也不承诺229会话覆盖全部原始历史。

8787现有Web不可达，未重启或占用其它服务。静态UI未改，不以包级隔离HTTP检查冒称用户浏览器验收。知识状态仍already_covered，按用户要求没有改Obsidian或原矿方案。既有未跟踪材料/锁继续保留并沿原owner根Codex接续。

本轮profile脚本、临时zip/校验旁账和只含汇总的测试回执已清理，共2911865逻辑字节，临时文件由2911865降为0。合成fixture自动回收、只读查询连接已关闭；最终release与fbd1ec7回滚、必要的45.4MB窄索引保留。没有启动常驻测试进程，没有删除源会话或其它任务缓存。

## 继续优化不限定角色搜索

开始本轮稳定性优化，当前安装46265ec、229个已索引会话。默认SQL预算仍2秒，首轮真实全进程基线与只读SQL实验进行中。采用此前已覆盖的“实际启动器、完整进程与SQL阶段计时分开、复用缓存”原则；知识状态already_covered，本轮只改TraceMesh。

### 稳定性优化源码已落地

两种无收益候选未采纳。实际改动为1/2词工具分数的EXISTS早停，以及摘录先选三条再计算长度/窗口，复用ASCII-only位置表达式。76项搜索相关回归通过（3.075秒），真实旧/新完整items相等、revision一致。综合回归和最终安装后稳定样本仍在进行，不以单次最好值完成验收。

最终综合回归164项全部通过，9.474秒；新增性能回归以instr/长度回调次数与结果语义验证，不依赖固定毫秒数。独立审查确认参数顺序、CASE早停、同一tool分数、跨消息AND、snippet排序和NUL/Unicode偏移保持；Python3.11语法检查通过。
