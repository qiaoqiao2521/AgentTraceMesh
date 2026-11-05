# Work index

- **源码及有界现场验收：大库活动与 AGY 缓存**：[计划](activity-cache-20261105/task_plan.md) / [状态](activity-cache-20261105/progress.md)。保留默认 2 秒预算，持久消息编号、窄列活动查询、AGY 跨进程缓存与一次活库重试、统一公开时间线；未声称全库覆盖或已更新安装。

- **源码及有界现场验收完成：检索障碍**：[计划](retrieval-friction-20261105/task_plan.md) / [状态](retrieval-friction-20261105/progress.md)。空索引回执、AGY失败隔离、跨来源精简/文件名检索、标题与查询错误；MCode录制身份和ZCode实际查询已定点验证，未冒称全库恢复。

- **源码验收完成：问题案例与专业纠偏首版**：[计划](problem-cases-20261031/task_plan.md) / [状态](problem-cases-20261031/progress.md)。Agent 编写三视图解释，TraceMesh 提供同源读查存取；一次独立受控迁移避开了已否决路线，生产及其它案例效果仍待验证。

- **源码验收完成：历史证据可靠性**：[计划](evidence-reliability-20261031/task_plan.md) / [状态](evidence-reliability-20261031/progress.md)。备份保留、旧出口脱敏、请求边界、共享索引规则及长会话交互；安装、发行与真实私有长会话未复测。

- **已完成：工具借鉴与知识收尾**：[计划](tool-knowledge-reuse-20261008/task_plan.md) / [状态](tool-knowledge-reuse-20261008/progress.md)。结构化结果、工具证据候选、直接消息展开、技能/插件日志标记与知识原页读回。2026-10-10 复查交付进度见状态页。

- **已完成：AGY 整库累计上限修复**：[计划](agy-store-cap/task_plan.md) / [验证](agy-store-cap/progress.md)。不再因整库超过 256 MiB 拒绝初始化，保留逐会话限制和不完整覆盖披露。

当前执行方向以此索引和 PROJECT 为准；旧计划保存有日期的证据，不竞争当前优先级。

- **验收交接**：[model-review-handoff](model-review-handoff/task_plan.md)：原需求与实现证据交给高级模型，对照当前代码逐项验收；不自动判完成或调模型。

- **工程完成：相关会话归组与关键消息导航**：[计划](conversation-reading/task_plan.md) / [状态](conversation-reading/progress.md)。Agent 对话阅读为主，完整记录保留，展示名 AgentTraceMesh；不发 rc.4。

- **已收口：Agent 对话记录定位与定名**：[trace-positioning](trace-positioning/task_plan.md)。比较归档；阅读功能已提交，AgentTraceMesh 定名落地。

- **已完成：检索加固补证**：[计划](search-hardening/task_plan.md) / [最终补证](search-hardening/closure/report.md)。新版独立探针、冻结源码真实点击/翻页/故障恢复；旧实验不倒填，产品实现不变。

- **检索修复**：[超大会话检索](large-session-search/task_plan.md) / [证据](large-session-search/findings.md)。旧缓存全文补查、命中后截取摘录，含 J1900 真实只读复验。

- **当前：项目阅读与设置**：[计划](project-reader-ux/task_plan.md) / [状态](project-reader-ux/progress.md)。4/4工程交付，rc.3已安装并启动；保留人工查阅，U1 已取消；安装/运行是该记录时点事实。

- **已交付：本机 CLI 来源扩展，1.3.0-rc.2 本地候选**：[计划](local-cli-sources/task_plan.md) / [状态](local-cli-sources/progress.md) / [盘点](local-cli-sources/inventory-extra.md)。包括用户指定的 OpenCode、cbc；Linux 只读接入，别名按存储归并。

- **当前：历史找回与复用工程完成，Agent 对接优先**：[计划](history-reuse-product/task_plan.md) / [状态](history-reuse-product/progress.md) / [依据](history-reuse-product/findings.md)。HV-reuse-linux-v1 实施 5/5、工程验收/候选包完成；U1/M3 于 2026-10-01 按用户要求取消；与旧独立交付分开计数。
- **已完成：Linux v1.2.0 完整交付**：[独立、有界交付](bounded-delivery/task_plan.md) / [状态](bounded-delivery/progress.md)。HV-independent-linux-v2 全部 5/5；发布、安装、回滚与验收证据已归档。
- 既有实现与局部验收：[Agent handoff service](agent-handoff-service/task_plan.md)。HV-H0/H1/H2/H4/H5 映射到独立计划；HV-H3 经 CM 的受监督原生验收归 CM，保留旧 partial 证据和用户直接执行显式续接命令的能力。
- 历史发布对齐：[plan](release-alignment/task_plan.md) / [status](release-alignment/progress.md)。不能据旧记录声称当前进程已对齐。
- 历史产品验证：[Product validation](history-viewer-product-validation/task_plan.md)。M3 未执行且已由用户取消，历史材料保留，不再是门槛。
- 已交付实现：[Insight review](review-insight-upgrade/task_plan.md)、[native OpenCode command](native-opencode-resume/task_plan.md)。
- 历史：[SpecMesh adoption](specmesh-adoption/task_plan.md)、[v1.0.0](release-v1.0.0/task_plan.md)。
- 本地 harness/device 记录和无关 migration notes 原样保留，不定义本计划范围。

- [outcome-materials](outcome-materials/task_plan.md)：成果素材预览、不可覆盖导出、公众号内容仓接入。

- [daily-activity](daily-activity/task_plan.md)：明确日期/时区的跨来源无头活动证据与 mcode v2（源码与验收完成，未安装/部署）。

- [web-briefing-window](web-briefing-window/task_plan.md)：网页日简报改用消息日期/时区窗口，移除跨日审计与累计用量污染。
