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
