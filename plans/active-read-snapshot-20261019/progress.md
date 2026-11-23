# Progress

## Current

2026-10-19：固定读取、目标捕获、失败隔离与纯素材入口已交付。源码实现 dfc0b07、知识原页 ddb71d7 已推送；两个 main 与知识页字节经官方 API 读回一致。原 GUI/安装包未升级，真实长会话未全量刷新。

## Done

- 已只读确认源码与症状吻合的失败机制，区分刷新漂移、未入库与查询预算中断。
- 已确认源码基线和未跟踪资产，未刷新真实全源、暂停对话或读取真实长会话正文。
- `read_view` 复用原查询方法，以独立只读连接和短事务固定 revision；search 返回捕获覆盖，跨请求旧 revision 失效契约保留。
- Codex capture 固定 FD 与完整行水位，同一 bytes 供 parser/audit 使用；逐会话 savepoint，成功项一次提交。None 完整且无 partial 时清理确已消失路径；targeted/partial 不清理。
- 新 CLI `refresh --file`、`materials` 和阶段错误回执；partial refresh 退出 1，fatal 退出 2。普通 handoff 的原源签名与 2 MiB 限制保留。
- 独立审查发现并修复副本最终别名占用覆盖，保留红→绿证据；同路径重复刷新不影响其他记录。
- 119 MiB 合成文件在捕获中追加半行后，仍查到末条素材并导出；优化后单次观察 0.996 秒、峰值 RSS 292500 KiB，不外推真实会话性能。
- 最终 Python 全量 687 项（686 pass、1 skip，22.183 秒）通过。JS `node --test tests/*.js` 九套通过；AST、45 个本地 Markdown 目标及 diff 检查通过。唯一 skip 为跨仓 Codex native reader 未配置，不属于本轮素材路径。
- 知识回写 `updated`：原 Wiki/自动化开发范式与智能体协作.md 的历史工具检索段补充固定读取与纯素材，原 Wiki/开发知识入口.md 增加活跃源触发。两工作树选定段落 SHA256=8fc85f4774ffd84c92a26cb3e1fda725648a31647110904b64a7b94d936d009a 一致，新增 Wiki 锚点读回通过；发布采用远端基线。

## Remaining

本轮无待提交代码或知识补充。此交付记录的提交身份从 Git 历史读取。

原有本地资料继续保留，owner 为根 Codex：daily-activity 私有定位/性能记录按原 public-acceptance.md 排除发布；SpecMesh R001 含嵌套 Git fixture，先从其 progress.md 读已验证范围，不把整个目录打包提交；键盘恢复 progress.md 保留旧现场待验，不重跑设备；ControlMesh 迁移建议稿保留待核对现架构；search-hardening 的两个 lock 文件属于运行状态，不提交。最短接续分别为上述原计划和稿件，不能将它们认作本轮已交付成果。

## Issues

历史 17.2 秒中断阶段仍未知；本轮补可定位回执，不将合成测试冒充现场复现。跨调用不保留旧 generation；首次捕获仍受 128 MiB 文件/8 MiB 单行默认预算约束。

## Next

下一项真实任务按 docs/active-read-snapshots.md 定点捕获与导出；不发布新版本或启动真实全源刷新。
