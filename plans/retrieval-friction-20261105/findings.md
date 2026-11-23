# Findings

- 当前HEAD为87c1dfe；继承的daily-activity/设备/SpecMesh/ControlMesh材料和锁保持原样。
- AGY源码已无累计整源cap，但逐会话异常未隔离，会使初始化全失败；旧`agy-store-cap`完成不能证明此边界已覆盖。
- 文件索引来源的health已有indexed_sessions，但search未带计数/空索引信号；用户实际假阴性成立。
- service.search对非公共message-index来源直接拒绝brief；CLI默认brief=False，完整session卡导致输出过大。
- 首轮定位曾使用不存在的根parsers/indexer.py路径，已改为history_core中的实际模块，不扩大盲扫。
- 本机仅stat已知AGY两处conversations目录：CLI目录现有256个DB，DB+WAL总计1,279,758,336字节；恰有1个会话超过64MiB，大小108,265,472字节（约103.25MiB）。这支持“单条超限拖垮整源”的原因，不是整源总cap重新生效。没有读取正文；目录DB数不等于摘要库259条，差异未在本轮伪造解释。
- query_budget原计时在获取Python锁之前开始，锁等待会消耗VM预算；已改为获锁后计时。SQLite实际BUSY/LOCKED、由本handler取消的预算耗尽、原因未知的INTERRUPT分别标识；没有设置新限额或自动循环重试。
- Codex混合环境包装消息的真实请求标题已可提取，但既有normalize可能省略其公开正文。本轮不通过把整段环境/指令包装公开来修标题；此正文边界单独保留。
- 用户追加的Mobile输出已包含本轮空索引字段，说明另一个调用方正在读取同一工作树；不能把其工具“成功”标签当查询到额度证据。Service verify另报MCode刷新重复ID、ZCode预算失败、Codex全量刷新超时；新增前两项定点核实，不重复发起全库Codex扫描。
- 首轮全量815项：1可选跳过、1失败、1错误。旧full service mock不具source，新增brief判定不应在full路径触发；已修。独立CLI恢复测试需要完整卡，改显式--verbose。另有文档补丁未匹配整段长行，已拆成准确目标修订，未覆盖其它正文。
