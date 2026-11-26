# 迁移依据与边界

- 迁移基线为 ControlMesh `59014053939707b98a9fea3510400fdf02d468f5`、AgentTraceMesh
  `3d44fd37a8a49076a5e142ef8a9e88a7c8dd55d3`。原插件位于 CM 的
  `plugins/dsh-companion-memory/`，已经是独立 Node/DSH 包，可直接迁移维护归属。
- 原包名 `@controlmesh/dsh-companion-memory` 与 CLI `cm-companion-memory` 改为 TraceMesh
  命名；`import-cm` 保留旧格式含义，不作为 CM 运行依赖或自动数据迁移。
- 原默认数据位置为 `$DSH_HOME/companion-memory/<companion>/`，宿主未设置时使用
  `~/.dsh`。保留它可避免源码迁移制造新存储副本；原生会话不进入本次迁移。
- 原研发三件套从 CM 固定提交完整迁入，只更换历史出处与当前入口提示，研究正文和旧测试
  记录不改。2026-10-23 的 DSH 接线与脚本 provider 结果不是本轮已实跑证据。
- 已撤回此前让 CM 代理上下文和传递回执的方案。TraceMesh 的 `context/compress/recall/handoff`
  保留原生入口，由宿主直接选择调用；没有新增 T3 数据库或前端适配。
- Python 历史工具保持标准库运行，插件独立使用 Node/DSH 依赖；维护归属统一不要求把不同
  数据含义或运行时强制合成一个系统。本轮不改可编辑文件存储和词法召回算法。
