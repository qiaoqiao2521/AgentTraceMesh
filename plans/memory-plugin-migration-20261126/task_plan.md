# 记忆插件归属迁移

## 目标与用户纠正

2026-11-26 用户明确要求记忆直接迁到 TraceMesh，CM 此后不负责，并采用插件形式。
本轮迁移既有 DSH Companion Memory，不在 CM 增加上下文代理或回执传递职责，
不重建旧通用记忆运行时。

## 范围

- 将插件源码、工具、CLI、配置、许可与测试迁入 `plugins/dsh-companion-memory/`。
- 包名改为 `@tracemesh/dsh-companion-memory`，CLI 改为 `tracemesh-memory`；
  保留旧 DSH 数据位置、companion 身份与数据格式。
- `import-cm` 仅为显式旧格式导入，默认预览，不读取或搬运私人存储。
- 同步 TraceMesh 与 CM 当前入口和 CI，迁入原插件研究记录并保留来源。
- 当前任务归宿主，项目事实归 SpecMesh，历史与个人记忆插件归 TraceMesh。

## 验收

源码不依赖 CM；包内容与命令名正确；既有存储行为与显式导入保持兼容；插件数据不入 Git；
当前文档链接可用，CM 活跃入口不再承担记忆职责。使用云端合成数据验证，分别记录无需 SDK
的存储检查与 DSH SDK 接线覆盖。不启动真实模型、不宣称部署或 Token 收益。

## 进度

实现、检查和交付状态见 [progress.md](progress.md)。历史研发事实单独保留在
[`../dsh-companion-memory/`](../dsh-companion-memory/task_plan.md)，不倒填成本轮结果。
