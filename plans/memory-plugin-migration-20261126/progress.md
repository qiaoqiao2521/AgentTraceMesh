# 记忆插件迁移进度

## 已完成的文档与归属工作

- 记录用户纠正：记忆能力由 TraceMesh 以可选插件维护，CM 保留执行与验收。
- 新增 `docs/memory-plugin.md`，同步根 README、PROJECT、AGENTS、架构、决策和计划入口。
- 将原 CM `plans/dsh-companion-memory/` 三件套迁入，保留原研究和验证正文，添加固定来源
  提交及本轮入口。没有把历史安装状态改写为当前状态。
- 明确原数据目录兼容、显式导入、宿主直接调用历史工具、DSH 与 T3 适配边界。
- 逐一比对固定 CM 提交，三份历史正文仅更新顶部出处提示。最终核对的 16 份文档、158 个
  相对链接全部通过；当前 README、PROJECT、AGENTS 与 docs 未残留已撤回的 CM 上下文接口。
  `git diff --check` 通过。
- CM 的 15 个原始跟踪文件与研究材料已先核对迁入副本，再从旧位置删除。插件 `index.js`、
  `store.js` 和 `LICENSE` 原样保留；CM 活跃代码、测试、脚本及 CI 已无记忆调用。

## 实现与验证

以下均为本轮云端源码与合成数据验证，独立于原研发记录。

### TraceMesh 插件与历史工具

- 插件 `npm ci` 成功安装 25 个包。在独立空临时目录再次执行
  `npm ci --offline --ignore-scripts` 成功，锁文件未变化。
- `npm run check` 通过；`npm run test:store` 为 16/16；`npm test` 为 22/22，
  包括存储 12 项、DSH 接线 6 项、迁移 CLI 4 项。DSH 接线使用脚本 provider，未调用真实模型。
- `npm pack --dry-run` 确认 9 个发布文件，包名与 bin 正确，没有捆绑依赖目录。
- `import-cm` 写入有条目失败时保留结构化 `errors` 并返回非零；选择性跳过不算写入失败。
- TraceMesh 前端 9 套测试通过。Python 全量在 `TZ=UTC` 下为 984 passed、1 skipped、
  470 subtests passed，用时 55.02 秒；唯一跳过为未配置的可选跨仓 Codex reader。
- 首轮 Python 验证遇到云端 PST/PDT 时区：既有 HTTP 日期筛选测试的 UTC 午夜样例落入
  本地前一天，出现 1 项失败。生产代码与该测试均和基线逐字节一致；按 UTC 单测及全量复验
  通过。现已在 Python CI 显式设置 `TZ: UTC`，没有修改日期筛选语义或跳过该测试。
- `python3 -B -S -m history_core context --request demo/context-gap-request.json`
  正常生成路由，`retrieval_calls=0`，没有读取私人历史或调用模型。

### CM 迁出回归与 CI

- CM 全量 pytest 为 111 passed、20 subtests passed，用时 21.02 秒；
  `python3 -B -S` 下执行入口 28 项、workflow 10 项通过，Ruff 通过。
- 重新构建的 0.44 wheel 为 19,564 字节；隔离安装核对 4 个命令、零生产依赖，真实正确
  产物被接受、错误产物被拒绝。插件迁出没有替代原执行验收行为。
- 两仓修改后的 workflow 均通过 actionlint；zizmor 离线 medium 门禁无报告。
- CM 当前改动文档的 46 个本地链接有效；已撤回的适配器源码、测试、计划与其生成缓存、
  0.45 构建产物均已移除，未进入本轮提交。

## 交付与知识记录

本轮交付内容为两个仓库的源码迁移，目标是普通 fast-forward 推送到各自 main；不创建额外
发布标签。迁移本地提交为 TraceMesh `c823ebb0d2aae7c63b8e96aae70227eace8693bd`、
CM `e84789f034a73ca18809315aa7cf9e80885c7264`。

首次 TraceMesh `git push origin main` 被自动审批拒绝：审查认为当时可信用户消息缺少对该
远端 main 分支推送的明确授权，并指出共享默认分支变更的影响。没有改用其他工具或通道
继续写入，也没有再尝试同类 CM 推送。随后只读核对：当时 TraceMesh 远端为 `3d44fd3`，
CM 远端为 `5901405`。用户随后在同一对话中明确回复 `ok`，批准把本轮迁移推送到
`qiaoqiao2521/AgentTraceMesh` 和 `qiaoqiao2521/ControlMesh` 的 main；授权阻断已解除。
继续推送前已重新 fetch，两个远端没有新增分歧。以上测试为已执行的云端本地验收；
远端提交与在线 CI 的最终状态以完成回复和相应 GitHub 检查为准。
包内容核对不代表已发布到 npm，也不代表已更新用户机器中的旧安装。

知识回写：`no_reusable_delta`。本轮沿用已有职责分离、显式导入及分层验收原则，
未查询或写入用户私有知识库。

## 未覆盖

没有搬运或导入用户私人记忆，没有替换用户本机安装、重启 DSH、调用真实模型或修改 T3。
实际模型的记忆选择、长期使用质量与 Token 收益未验收。
