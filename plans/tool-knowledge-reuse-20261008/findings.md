# Findings

## 2026-10-25 查询预算与用户方案检索

- 实际自身调用的旧证据包含 activity 全表 raw_json 计数与 markers 的 `OperationalError: interrupted`。当前按项目的 brief 查询再次在 search 阶段约 2067ms 报 `query_budget_exceeded`。这是本地 SQLite 预算，不能据此推断模型账户耗尽。
- 同一真实长会话定点捕获约 52MB、5874 条索引消息；完整行尾部仍未覆盖，`partial` 保留。复用知识页“先固定读取、再按修订展开”，没有扫原始全库或新建索引系统。
- 工具配对在三个选中消息时，实际执行计划先 `SCAN m` 全表，再关联会话内编号。合成数据独立复现；仅物化 selected_calls 不够，固定 `ordered → messages 主键` 的连接顺序后，同一查询从约 2 秒中断降为 139ms，配对返回正常。测量仅说明该缓存/所选查询，不是全库性能承诺。
- 工具优先的三条摘要可能挤掉用户补充。新增 brief 的会话/角色预过滤；用户长正文按全文匹配，绝对编号和分页脱敏沿用既有接口。真实用户方案检索约 30–64ms；长尾全文、角色隔离、旧修订与 CLI 展开另由合成回归验证。
- 原 activity/markers 全库预算问题未在本轮统一优化；继续按日期、项目或会话缩小范围，失败保留缺口。归属：根 Codex；入口为 activity 的 raw_json 统计与 markers 查询的实际计划，不以调高预算替代定位。
- agent-link 的 AGY 子调用因 `--print`/`--output-format` 参数兼容失败；未反复重试或改认证，改用内置子 Agent 完成独立 SQL 计划分析和回归。

- 采用现有知识页关于按实际调用入口验证的经验：归一化纯函数、公开 reader 和缓存迁移分别核对。来源：Obsidian `Wiki/自动化开发范式与智能体协作.md`。
- Codex 工具结果渲染及摘要只处理字符串；同一人工对象直接输入会丢正文，序列化为 JSON 字符串后可读。审计提取器已有对象/列表支持，阅读器能力不一致。
- 会话级 AND 与最早三条任一词摘录不是同一条有效工具经验；内部推理参与旧全文召回。
- 原知识入口只有宽主题；旧协作规则仅要求新页补入口，旧页新增经验缺少发现性维护。
- 知识库已有近期经验，不能将索引缺口概括成完全未更新；索引刷新不代表原页已写入。

## 2026-10-10 Recheck

- 前轮会话内摘录相关性没有改变 brief 的会话级最近排序。合成 CLI 搜索表明，旧的真实工具调用会被较新的随口讨论挤出少量候选。
- 命中窗口不能代替工具状态：短结果也可能从中间裁剪，原 `length>240` 标记遗漏前部省略；错误头与返回体要分别保留。
- 输入和结果关联必须使用同会话已保存的 Call ID；相邻记录或同名工具不是可靠关联依据。
- search 和 activity 修订不同。采用原知识页“验证入口跟随实际调用路径”：直接消息展开绑定 search 修订，保留 activity 独立契约，不要求用户猜跨日日期。
- 正式 v6 缓存会保留解析回归。正文/错误解释先由 v7 刷新迁移恢复，最终 v8 另保存日志标记；两阶段均核对正式上一版缓存。新缓存通过不能冒充旧缓存恢复。
- 最新浏览器规则已由用户明确改为 daily-browser plugin first；知识原页仍直接引导 CDP 排查，需要同步工具次序，而不为知识整理实际登录账号。

## Skill marker scope and evidence

- 用户于 2026-10-10 明确：目标是 `learning-output-style` 插件触发与所有 skill 采用的精准日志标记。TraceMesh 只提供接口；后续 Agent 才提取结论、分析作用并在原技能仓库优化。
- 已撤回误扩的 skill-learning 草稿；没有安装、helper、链接或反馈数据库。tracemesh-recall 正文恢复到本轮修改前的 0.4.1；共享 AGENTS 仅增加用户确认的原对话标记约定。
- Codex 的实际所选录制已有 4 处 developer Learning 指令注入。Claude 有界观察到 SessionStart attachment 的 list[str] 原生格式；该真实样本为 Explanatory，不冒称 Learning 已观察。Learning 分支另用当前插件公开提示和合成录制验证。
- 注入、原生 Skill 请求、Agent 声明是三种证据，不推断加载成功或正向作用。读文件、配置启用、目录列表和 Insight 都不产生标记。
- Claude 混合附件按元素识别并保留 JSON pointer；Codex 新 context 缺少 turn ID 时清空旧值，避免错误归因。工作目录来自录制，不探测 Git 根目录。
- Codex parser v8、Claude v6 在既有索引保存 marker 元数据；公开 refresh 验证正式上一缓存恢复且不新增表。markers 查询不取正文，不刷新，不写历史源。
- 独立审查与纯代码验证均使用最小合成记录。实际当前录制活跃，第一次前后哈希比较不稳定；随后的有界读回确认 4 个注入与 1 个声明、该次源字节不变。这不是全库覆盖。
- `markers` 结果始终披露 recorded_markers_only、未捕获可能与 effect_assessment=not_performed，防止后续 Agent 将日志数量误当效果判定。

## Protected prior work

- `plans/daily-activity/` 未跟踪测量、基线和原始定位：已有计划明确仅本地保留；不上传。源码及公开验收文件已在 Git。
- `plans/search-hardening/*.lock`：本地实验锁，保留，不入 Git。
- `plans/specmesh-r001-validation/`：已读当前计划，验证完成但明确“不提交实验 fixture 或 Harness 产物”；保留其本地证据和嵌套测试 Git。无需为知识维护重测设备。
- `plans/ubuntu-keyboard-recovery/`：进度中混有两个日期的恢复段，真实输入验收与 USB I/O 收尾没有当前确认；保留原文件。接续 owner：根 Codex；从 `task_plan.md` 的输入验证和现有 `progress.md` 判断对应日期，不自动重做系统或磁盘操作。
- `agents/controlmesh_typescript_migration_plan(1)..md`：已读迁移结论和证据边界，仍引用旧账户的公开仓库与未完成的本地审计；不能作为已验收实现交付。接续 owner：根 Codex；在对应 ControlMesh 当前计划核对本地源码和适用范围后整理。
- Obsidian 的嵌套 `编程/SpecMesh/`、公众号目录保持独立 Git 边界；其他已有主题修改先审阅，不盲目暂存。
- Obsidian 的既有视频路线与 ROS 2 项目地图已查目录、来源和验收边界；涉及更广项目历史与同时进行的机器人任务，保留当前主工作区。接续 owner：根 Codex；先核对对应媒体/ROS 2 项目已有计划及这四页的引用，不为整理笔记重跑实体环境。当前任务仅隔离交付已核实的开发知识、入口与后台浏览器经验。
