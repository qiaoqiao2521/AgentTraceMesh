# 进度

- [x] 记录 HEAD/脏工作树基线；保留已有修改。
- [x] 人工 oracle 冻结；实际 Handler + Indexer 复现遗漏/污染/末秒边界。
- [x] 切片一无头日期/时区/跨来源入口及独立验证。
- [x] 切片二 mcode v2 解析/注册/读取及独立验证。
- [x] 追加/删除/修订/失败/重复身份加固；保持旧入口兼容。
- [x] PLC/LifeGame/课程/mcode 定点真实冒烟。
- [x] 同来源/日期/预算冷热测量；明确 stale/partial。
- [x] README、机器说明、检索指引、完整验收记录。

完成范围：源码与本轮验证。未提交/安装/部署/推送；未将开发自测等同独立用户验收。

## 2026-10-22 统一活动回执补强

- 依据现有验收：activity 可用，但曾遇 `query_budget_exceeded`，索引刷新也为
  `stale/partial`；不能承诺完整覆盖。
- `query_activity` 顶层新增向后兼容的 `receipt`，集中披露窗口、请求/可读/失败来源、
  各来源水位、失败原因和 `limitations`。`checked_through` 没有可证明的统一水位时
  保持 `null`，`receipt.partial` 不会把未知改写为完整。
- 来源失败时保留缓存是否存在的状态；预算中断明确为 `query_budget_exceeded`，不转成
  “零命中”。旧 `sources[].coverage` 字段保留。
- 新增测试覆盖正常但 partial、混合来源、候选截断、缺时间、刷新失败和预算失败。
  `tests.test_activity tests.test_mcode tests.test_web_briefing tests.test_briefing
  tests.test_codex_tool_results` 共 70 项通过，`py_compile` 与 `git diff --check` 通过。
- 知识回写：`updated`，原页
  `Wiki/自动化开发范式与智能体协作.md#历史工具检索：定位原文再借鉴实现`；已写入统一
  活动回执、未知水位、预算失败和刷新 partial 的适用范围。真实长会话仍待现场复测。
- 交付：提交 `5bb0eeb` 已普通推送并读回 `origin/main`；作者为 `qiaoqiao2521`。
  以上“未提交/推送”仅描述 2026-10-01 原验收时点。
