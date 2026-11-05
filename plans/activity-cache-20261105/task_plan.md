# 大库活动与AGY缓存

## 用户目标与范围

承接第二轮现场清单。优先修活动日窗对大TEXT逐会话重编号的结构性成本、AGY CLI跨命令全重扫、活库快照竞态；提供AGY/ZCode公开事件时间线，保留标题原摘要与首条真实用户候选。核对空索引回执在CLI与聚合API的实际覆盖。前轮默认compact、verbose、跨来源brief及错误分类不回退。

采用已有SQLite派生缓存、消息读取/修订和原生公开投影，不增加数据库服务或向量索引。保留用户已加HISTORY_QUERY_BUDGET_SECONDS手柄和默认2秒，性能改进从查询结构取得。source DB/WAL只读，静态108MB会话继续按原64MiB降级，不因重试变更限额。

## 分工

- ordinal worker：messages.message_index新写入持久化、旧缓存窄键兼容与activity查询；不重解析全库或启动时更新全部大正文。
- AGY worker：data_dir可选持久缓存、跨进程复用、单会话增量、coverage持久化和活库一次定点重试。
- timeline worker：公开事件分页与原生轻量时间筛选；标题候选事实核对。
- 根Codex：reader/providers/CLI/Web接线、聚合搜索空索引回执、用户手柄保留与预算披露、交叉审查、实际验证和Git交付。

## 验收

乱序/同时间戳/context占位下，search/message/activity/timeline绝对定位一致；旧大索引不把TEXT带入排序，不复制或整库重解析。AGY冷进程后另一个进程读同cache不解码，源变化只重读变化会话，失败保留诚实partial。竞态最多重试一次，永久大小超限不重试。统一时间线带来源/修订、[start,end)与partial，不将其冒称全部activity来源已覆盖。

## 当前/下一步

源码实施和有界现场验收完成。根Agent已串行集成三个切片，保留用户预算手柄。默认2秒下既有大缓存实际日窗复测通过；最终边界回归通过。既有未跟踪材料和锁保留，旧安装及全量私人AGY冷/热性能未复测，详见progress。
