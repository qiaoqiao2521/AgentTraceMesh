# Findings

- `scan_sessions` 的整批事务会因任一文件指纹变化整体回滚；parser 与 audit 还分别打开源文件。必须保留一致性保护，改变捕获边界与失败隔离。
- `HistoryReader.search/message` 原本读取派生库；revision 检查只能发现变化，不能保留旧版本。源根路径 SHA 绑定缓存，缩日期目录会进入另一缓存。
- 普通 handoff 重新读取源文件前 2 MiB，截断时完整 content_revision 未知；现有 selection_bundle 也依赖源绑定。本轮另设明确的索引素材绑定，不降低原保护。
- `query_budget` 的 2 秒/约两千万 VM 指令限制不是整条工作流硬超时；单条消息脱敏仍处理完整选中正文。17.2 秒历史中断没有阶段回执，不能归因于某一步。
- 复用比较：SQLite WAL 短读事务已满足单次读取；全库 backup/长期 generation 存储会增加空间与生命周期，当前不采用。现有 `activity.skipped_paths` 是隔离参考，其整源 manifest 契约保留。
- 当前未跟踪项包括旧 daily-activity 验证资料、search-hardening 锁文件、specmesh-r001 与键盘恢复计划。未把它们认作本轮成果或删除，收尾核对归属和接续入口。
- 首轮全量回归 671 项发现 8 fail/1 error/1 skip：None 刷新尚未保留原全目录删除清理、缺源异常类型变化；两个旧 parser spy 不支持新 content 参数；public API 清单缺 materials；旧整批失败测试需与新增逐会话隔离契约分开。保留 strict scan 的整批回滚断言，另验证 public refresh 的 partial 与旧失败行保留，不把红灯改称通过。
- 原 daily-activity/public-acceptance.md 明确原始定位、性能与真实冒烟材料只留本地。SpecMesh R001 是含嵌套测试 Git 的上下文验证；键盘恢复仍有用户现场验证与旧设备状态。上述资料不随本轮公开提交，接续 owner 为根 Codex，具体入口在原计划；不因收口重新操作设备或执行旧命令。
- 独立审查复现生成副本别名被另一真实 native ID 占用时覆盖旧索引的缺陷。已在任何 DELETE 前再次核对最终别名，冲突由 savepoint 回滚；回归先红后绿，并核对原记录、占用者和 revision 不变、同路径重复捕获保持别名与原记录。
- 已安全快进远端两项既有提交到 1347eed，仅涉及账户文档链接与 CI，不改变产品运行代码；原本地工作保留。主知识库 14 个本地独有/29 个远端独有提交，且目标页已脏；本轮在 origin/main 的独立工作树只发布原页两段和问题入口一行，同时保留本地主工作区内容。
- 初次 119 MiB 合成 JSONL 验证通过捕获中追加半行、后部消息查找与纯素材导出，输入 124780544 字节；1.811 秒、进程峰值 RSS 772940 KiB。该数据主要为每行不超过 7 MiB 的元数据填充，不代表真实会话消息复杂度或现场性能。观察到 StringIO 全文 Unicode 缓冲开销后，复用 BytesIO/TextIOWrapper 消费同一捕获 bytes；不宣称常量内存。
- 同一 119 MiB 合成边界在流式解码后再次独立通过：0.996 秒、峰值 RSS 292500 KiB，captured_through=124780544，追加尾部仍为 partial。两次均为临时文件与派生缓存，未读取用户长会话。耗时与 RSS 仅是本机单次观察。
