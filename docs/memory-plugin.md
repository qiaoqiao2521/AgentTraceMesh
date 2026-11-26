# 记忆插件归属与使用

TraceMesh 独立维护记忆能力。现有 DSH Companion Memory 已从 CM 迁入
[`plugins/dsh-companion-memory/`](../plugins/dsh-companion-memory/README.md)，包名为
`@tracemesh/dsh-companion-memory`，CLI 为 `tracemesh-memory`。它保存可编辑的个人记忆、
偏好、共同经历和承诺，使用 DSH 原生工具、动态上下文与插件生命周期。

当前任务状态由 T3、DSH 等宿主维护，项目已确认事实按 SpecMesh 保存；历史压缩、召回、
缺口路由和交接仍由 TraceMesh 的现有入口承担。CM 保留执行与验收职责，不存储记忆，
不转交恢复回执，也不是使用插件的前置依赖。

## 安装与宿主边界

按 [插件 README](../plugins/dsh-companion-memory/README.md) 从本仓源码安装到选定的 DSH
profile。插件单独要求 Node.js 与相应 DSH peer packages，复用当前 Harness；不替换模型、
登录和代理。普通 `python3 app.py` 与 `python3 -m history_core` 不加载它，也不增加 Python
运行依赖。包名变更不代表 npm 已发布，源码迁移也不代表用户机器已经安装或重启。

当前实现是 DSH 原生适配。T3 或其他宿主可直接使用 TraceMesh 的历史 CLI，例如在本仓运行：

```sh
python3 -B -S -m history_core context --request demo/context-gap-request.json
```

该合成例子只生成缺口路由，不调用模型、不读取私人历史。宿主核对返回证据后按当前任务与
已有授权继续；需要检索时沿用 [`context`](context-gap.md) 的显式来源与回执复用规则。
这不表示 DSH 插件已能原生装进 T3，本轮也没有修改 T3 数据库、前端或会话机制。

## 数据兼容与旧 CM 导入

默认数据目录继续使用 `$DSH_HOME/companion-memory/<companion>/`；未设置 `DSH_HOME` 时
根目录为 `~/.dsh`。已配置的 `dataDir`、companion 身份、Markdown 记忆和每日总结格式保持
原意。已有 DSH 插件数据无需重导；更换源码或包名不会自动移动私人文件。

`import-cm` 只处理明确选中的旧 `MEMORY.md` 格式，默认预览：

```sh
node plugins/dsh-companion-memory/cli.js import-cm /path/to/selected/MEMORY.md
```

明确执行导入时才加 `--apply`。分类、重复导入、跳过项和来源保留规则见插件 README；
不扫描旧 CM 配置、每日笔记或原生聊天，不启动旧 CM 运行时。卸载插件后的数据保留与
选定条目删除也按插件自身规则处理。

## 维护与验证

历史读取、问题案例和个人记忆各有自己的数据含义，候选或模型总结不能自动晋升为当前
项目事实。本轮保留既有可编辑文件存储与检索算法，完成的是归属、包接口和维护入口迁移；
存储效率、召回质量和真实模型收益需要另用相应证据评估。

本轮源码、打包和测试结果见 [迁移进度](../plans/memory-plugin-migration-20261126/progress.md)。
[原研发研究](../plans/dsh-companion-memory/findings.md) 与
[原验证](../plans/dsh-companion-memory/progress.md) 从 CM 随代码迁入，保留当时版本与日期。
原来的脚本 provider 验证不代表本轮真实模型或用户安装已经验收。
