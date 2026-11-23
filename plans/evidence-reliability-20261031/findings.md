# Findings

- 基准：`17b4786`，与用户审查一致。
- 已静态确认：归档及项目删除只检查一次备用名，随后 shutil.move；旧 POST 通用入口未检查 Origin／Content-Type；GET reindex 会写派生索引；Web 有独立来源注册。
- 开始时已有未跟踪项：daily-activity 验收材料、specmesh-r001-validation、ubuntu-keyboard-recovery、agents 中用户迁移计划与 search-hardening 锁；均保留，未读原始会话或凭据。
- 用户报告是调查依据，不把其测试统计或浏览器风险当成本轮验收结果。

## 已修复与复核

- 备份：同名与相同 ID 前缀连续三版均保留。最初复制方案在最终 stat 与 unlink 间仍漏追加，独立复现后改为同文件系统排他硬链接保留原 inode；跨盘/不支持链接时保留源并返回 backup_failed，批量部分成功返回真实数量。相对源 symlink 备份真实目标；不声称 Windows 已验收。
- 请求：API Host 校验覆盖 GET/POST；所有 API POST 共用 JSON/Origin/Sec-Fetch-Site 检查。无 Origin 的合法 CLI 保留。GET reindex 不再写索引，POST 可重建只读来源的派生缓存。素材还检查实际 peer 为 loopback，不能只靠 Host。
- 交接：旧 payload、compact、standard、可选计划及前端复制/Markdown/HTML 沿用同一常见秘密脱敏，完整字段先脱敏再截断；原始本地 audit 与修订定位保留。
- 解析与索引：Web 所有系统注册从 providers 获取版本；真实 Linux main 启动旧缓存迁移通过，旧 app 在同一合成测试中触发六处失败；Windows/WSL 只验证隔离注册参数。旧 test_usage 强制版本 5 的源码匹配测试被真实启动测试替代。同 ID 不同录制普通扫描可见且更新保持身份，冲突标记仅透出必要字段；冲突别名禁止生成伪原生 resume。
- 阅读：两条 session search 路由在查询前后验证修订；前端携带修订。失效后清浏览缓存，Clear 不恢复旧消息。时间线绝对消息身份与相对布局位置分开，长消息按 message_index 找对象。

## 独立审查

agent-link Qoder 在 quota probe 通过后派发只读审查，180 秒未返回有效结论；改用宿主内置子 Agent。内置跨文件复核实际复现了备份最后追加、伪 loopback Host、409→Clear、别名 resume 四个遗漏，全部补修并复测。没有宣称外部审查成功或恢复回调。
