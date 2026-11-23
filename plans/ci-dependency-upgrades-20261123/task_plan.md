# CI 依赖升级审查

## 授权与范围

2026-11-23，用户明确要求检查四个尚未合并的 Dependabot PR，确认没问题后直接合并。
审查对象是 AgentTraceMesh 的 #1、#2、#3、#4；只调整 CI Action 固定版本，保留应用运行时、测试版本、权限和当前测试入口。

## 完成项

- [x] 核对精确 diff、官方标签 SHA、发布说明和目标 Action 源码。
- [x] 将四项升级叠加到当前主线，检查工作流语法、安全扫描与既有回归。
- [x] 保留当前 pytest、并发和已升级 Action 配置，解决旧 PR 的相邻配置冲突。
- [x] 逐个按已审查的 head SHA 合并，并读回 PR、主线内容和分支状态。
- [x] 将结论与验证范围保存到 findings / progress。

四个依赖 PR 的审查与合并已完成。依据见 [findings](findings.md)，回执见 [progress](progress.md)。

## 剩余单分支整理待办

- [ ] 删除远端 `codex/problem-compression-20261122`。
- [ ] 删除远端 `workflow/codekit-integration-20260911`。

这两条分支的提交已确认均包含在 main 中，没有独有提交。四条 Dependabot 分支在合并后的远端列表中已不存在。
当前连接器没有删除远端分支接口；此前云端网页登录已取消，本轮没有再次请求登录。删除以上两条分支后，远端仅保留 main。
