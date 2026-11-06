# Findings

- 当前HEAD 7b19bb6；用户未提交reuse.py预算手柄默认2秒、显式环境变量可调，保留其意图与代码。
- activity.read_store的ROW_NUMBER原SELECT *带消息TEXT进入临时结构。只改--limit不减少已发生的编号和排序成本。
- AgyIndexer当前内存SQLite，构造必scan_sessions；因此每个新CLI进程都重解码。已有持久Indexer/schema可直接复用，不另造缓存服务。
- AGY summary先stat作为_read_only预算，随后WAL变化可能使刚观察的预算失效；需要定点一次重试与可定位的最终错误，不能拿静态会话超限当竞态。
- 本轮将“已有修复但调用路径未覆盖”和“新增缺口”分别验收；不以旧测试通过替代用户4.3GB活动场景。

## 真实瓶颈与修正

先只移除TEXT编号排序仍不够。既有主缓存3,932,028,928字节，所选2026-11-02 UTC有效日窗在默认2秒下仍约2.303秒失败。只读profile发现聚合前重复访问sessions的大段metadata/search_blob布局；物化窄列聚合结果、分页后再join会话，将同一完整read_store降至worker约0.458秒，根Agent独立复测0.3961秒，返回10会话、20证据。两次均未设预算环境变量、未刷新源、未更改此真库schema。计时包含暖文件缓存，不能作为冷启动或所有日期性能保证。

最初在此旧缓存查2026-11-05返回0，后确认缓存最后有效活动为11月2日，已排除该次空窗作为性能证据。旧库249个会话缺解析期覆盖计数，因此仍返回未知；没有将新查询成功写成完整覆盖。

## 接口边界

- CLI/Reader已有空索引语义，但聚合reuse/search未带；本轮补source_indexes和结构化refresh_request。新timeline也复用空索引及partial捕获回执。
- AGY持久cache以现有SQLite实现，无新服务。新进程禁止快照/解码仍可warm搜索；实际单会话变化只解码该会话。完整1.2GB私人库未重新扫描，合成跨进程证据不冒充真实大库冷热耗时。
- AGY summary/WAL大小观察竞态和exists→stat删除竞态，仅观察到源变化才重试一次；静态缺失/单条超限保持原限制，持续变化带两次尝试与路径。
- AGY保留摘要及首条真实用户请求title_candidates，不把“中文”当作原摘要无效的理由。
- timeline复用现有原生公开投影/消息读取，按显示时间筛选、按原绝对槽位展开。隐藏槽位不公开、不删号；显示时间可能推定，不扩称activity认证范围。

## 独立审查纠正

AGY worker只读复查补出WAL删除竞态；timeline worker补出新入口空索引/partial capture遗漏和负时间参数后置初始化。根Agent或原owner修复后，最终128项相关回归覆盖这三处，不以先前全套绿灯覆盖后续修改。

## 追加现场

启动器~/.local/bin/cchv仍执行~/.local/share/cchv/current/app.py，默认data-dir为~/.cache/cchv；current目前指向1.3.0-rc.3-c47bbc8。用户要求升级该实际入口；不能只在仓库运行测试。现有build_release.py/verify_release.py与current软链可直接复用。

真实AGY：源DB/WAL合计1,280,094,208字节，259摘要。首个默认派生缓存冷建10.4479秒，259次解码、257次源快照；另一次空派生目录冷建8.8102秒，/proc VmHWM约100.05MiB（没有清系统page cache）。新进程warm health0.0198秒、search0.2298秒、timeline0.0551–0.0597秒，均0解码/0源快照，同index_revision；warm实际VmHWM约31.2MiB。getrusage的ru_maxrss受exec前高水位干扰，弃用其2.2GiB显示，不据此误报真实占用。默认派生库252,571,648字节；251条decoded_text，4条metadata-only（1 legacy+2缺源+1超限），另4条部分解码，326个未支持step，仍partial。

空间：19个cchv目录allocated合计20,367,929,344字节。主cchv3,410,037,512字节WAL经SQLite checkpoint(TRUNCATE)回执[0,0,0]回收，不直接删除。保留main；fresh Codex缓存无复制移动至canonical根。17个退役目录经tar+zstd流式归档并完整列出134成员，确认无占用/变化后移除展开副本：原allocated9,243,955,200字节，归档2,374,773,250字节，净回收6,869,176,320字节。原文件缺失的旧cchv-codex历史保存在归档内，主库的recall人工meta保留。

## 2026-11-07：普通搜索另有开销

已安装fbd1ec7在229会话的真实Codex缓存上，“思维 升华”“摘要 原文”加role=user/limit3均约2.08/2.11秒以search阶段query_budget_exceeded退出；problem-cases同轮2.03秒成功，说明并非稳定的全库不可用。当前主文件4,484,833,280字节，消息只有session_ts/activity索引。热只读候选SQL约0.30秒、245万VM；仅缩投影仍约0.31秒，不能照搬上次activity的单一解释。

scope查找使用session_ts，先逐行回表检查所有role/kind；新增不含TEXT的(session_id,role,kind)索引后，实际EQP按session_id+role定位，tool排名额外按kind范围定位。真实窄索引约45,420,544字节，首次schema补齐约0.81秒，不使文件签名或解析缓存失效。角色搜索热读总耗时约0.024/0.050/0.013秒，返回1/3/1个候选。

无role搜索还会把files/tool_summary/command_intents/remote_context等大字段带入候选排序，改为物化窄键排序分页后再join会话字段。合成50候选的可观测metadata读取由50次降至最多limit+1次；另一个回归证明增加5000条其它角色消息不再线性增加user查询VM工作，同时工具词不能补足用户查询AND。

排名字符串实验：LIKE+NUL回退并未加速（1.73→1.82秒），已放弃，未进入产品代码。SQLite lower只折叠ASCII，因此无ASCII字母的中文/数字term直接instr，不再复制整段tool正文；同页顺序一致，实测候选约1.72→1.47秒。ASCII仍原逻辑，NUL尾部/字面%下划线反斜线/非ASCII大小写有语义回归。

以上计时有页缓存、查询顺序与机器负载影响，单次数据不作冷IO或所有查询的性能保证；确定性的回归用VM工作与metadata访问次数证明开销消除。默认预算仍2秒，错误/partial契约不变，不重建全文索引或添加正文副本。
