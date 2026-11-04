# Progress

## 当前

源码修复与有界现场验收完成。根Codex串行整合；三个内置worker分工实施并交叉复核。保留既有来源路径、修订分页和落盘读取约定；不新建数据库、自动全库刷新或外呼模型。

## 已交付

1. search/health同时给indexed_sessions；文件缓存为空时标index_empty_refresh_required/partial并附可执行刷新参数。Codex另给selected_refresh_argv_template，支持保持原来源根/data-dir的--file定点捕获。已填索引零命中、空原生库、查询失败分别表达。
2. AGY单会话超限/解析失败保留partial_source_error元数据，正常会话继续。source_error包含实际path/size/limit/reason，未知为null；错误列表前5项与总失败数分开，不恢复整库cap。
3. CLI search默认brief，--verbose显式选旧完整卡；Python默认和旧Web full入口保持兼容。OpenCode/ZCode/Hermes支持公开摘录与同修订message展开；能力不足时明确partial/limitations及未验证元数据。
4. --field files_touched按公开正文中的字面文件名/路径在分页前筛选，支持空格。结果带role/kind/消息定位，不用推断files_touched当操作证明。
5. Codex派生标题跳过ambient，parser版本9在显式refresh迁移；AGY空/默认/包装摘要回退公开用户请求。真实英文摘要和人工标题（含合法HTML文本）保留，native-reference协议不改。
6. 等待Python连接锁不再消耗SQL VM预算。真实handler取消、BUSY/LOCKED和未知INTERRUPT分别报告query_budget_exceeded/source_busy/query_interrupted；CLI含failure_domain/retryable，不自动重试或新增配额。
7. 追加MCode问题：移除重复manifest ID的专用拒绝，复用既有recording alias保留不同录制；刷新审计、selected review和旧GUI ordinary audit都支持精确别名。源路径/原生ID/元数据不匹配会拒绝，源读取期间变化仍回滚或明确报错，review原大小限制不变。

## 验证

- 首轮全量815项发现旧full service依赖被brief判断扩大、以及完整卡恢复测试未显式--verbose；已修复这两项真实兼容问题。
- 整合后`python3 -B -m unittest discover -s tests`：831项，830通过、1可选跨仓原生reader未配置跳过，用时37.914秒。随后独立复核补工具JSON null的粗筛漏命中；最终uniform/MCode identity/receipt三组37项全部通过（含该新增回归），未把831写成832全量复跑。
- 9套tests/test_*.js全部通过；git diff --check及受影响文档相对链接检查通过。
- 真正子进程CLI完成：空缓存信号→定点refresh→含空格mp4路径筛选→按原修订展开tool_use/tool_result/assistant原文。精简候选不含完整files_touched；小会话不保证JSON字节数总比verbose少。
- AGY合成单64MiB+1、坏DB、steps及payload超限、全部失败与错误列表截断、坏条修复后refresh、源变动回滚均通过。全部失败时索引会话数仍代表元数据数量，另报正文0与失败数，不伪装完整覆盖。
- native公开投影回归覆盖私有reasoning排除、空/无效envelope、空白type、ZCode sequence、unknown cwd、跨part AND、literal路径>12空白词、工具固定词/格式化JSON/null，以及巨大消息明确限制。2000个无关tool和同会话3MiB tool不进入user query/展开的renderer。
- MCode重复ID、精确副本、alias稳定、源owner消失、元数据篡改/伪造alias、manifest变化与audit期间源变化均覆盖；普通audit的大文件能力保留，selected-review仍有原限制。

## 实际来源检查

仅stat两处已知AGY目录：CLI当前256个DB、物理总量1,279,758,336字节，有1个108,265,472字节（约103.25MiB）会话超过64MiB。支持单条限制触发原因，未读取全库正文。目录DB数与用户摘要259条不同，未擅自解释差异或宣称已恢复所有会话。

ZCode实际有界查询“额度”、role=user、limit=3：修复前2.053秒预算耗尽，3项仅未验证metadata、0摘录；查询下推后最终一次0.237秒，3候选/6公开摘录、partial=false、native_public_excerpts、无error。仅输出计数/耗时，未输出私有ID或正文；不是任意查询的性能承诺。使用既有native只读接口，未复制库或写源；ZCode原有SHM读标记边界仍见local-cli-sources说明。

用户追加Mobile输出已读到新空索引字段，表明另一调用方使用了本工作树；其工具成功标签不等于已找到额度记录。没有重复全量刷新Codex。MCode仅有定点manifest抽查和合成触发复现，没有真库全量刷新验收。

## 知识与交付

updated：已在Obsidian原页Wiki/自动化开发范式与智能体协作.md的“历史工具检索”补空索引/失败区别、定点刷新、路径引用与操作证明、实际调用位置和争用分类；Wiki/开发知识入口.md同步触发词。两处内容已实际读回，diff检查通过。知识库Git发布pending：两页继承约600行未提交重写与其它协作修改，接续owner根Codex，最短入口为两页scoped git diff；未把本地回写冒充远端发布。

当前仓库源码用main普通提交推送；既有cchv启动器仍指向独立安装目录，本轮不替换安装包或运行服务。831全量基线与最终37项是源码证据，不是安装/私有全库完成证据。

## 剩余边界与清理

混合环境/指令包装后用户请求的标题已修复，但原正文normalize仍可能省略尾部请求。本轮没有通过公开整段隐藏包装来改变该边界。元数据fallback不代表应用过关键词、角色或文件条件，路径引用也不证明落盘或删除。真实Windows/WSL与可选跨仓reader未现场复验。

临时合成来源/缓存均由TemporaryDirectory清理；本轮没有新建常驻服务或浏览器。两份测试临时输出在核对后删除，约8KiB；保留代码、测试和任务记录。此前daily-activity、SpecMesh/设备、ControlMesh未跟踪材料和锁用途/证据仍待审查，原样保留；接续owner根Codex，入口沿用plans/evidence-reliability-20261031/progress.md的交接段落，不混入本次提交。
