# 本机缓存：复用优先，按需刷新

安装入口 `cchv` 启动 Web；`cchv reader` 使用同一已安装版本读取历史，不依赖当前仓库目录。

```bash
cchv reader --source agy \
  --source-path ~/.gemini/antigravity-cli/conversation_summaries.db health
cchv reader --source codex --source-path ~/.codex/sessions \
  search --query '要找的关键词' --limit 3
```

文件来源和 AGY 的 CLI 默认缓存根统一为 `$XDG_CACHE_HOME/agenttracemesh/reader`，未设置 XDG 时用 `~/.cache/agenttracemesh/reader`。来源和原始根路径仍分别隔离；不是把不同来源的会话混在一张表。显式 `--data-dir` 保留为实验或独立快照入口，日常查询不再为每次任务新建 `cchv-某选题` 目录。Python `HistoryReader` 的文件来源仍须显式给目录。

普通搜索只读已有索引，不自动全量刷新。先看 `indexed_sessions/index_state` 和覆盖回执；空索引才按提示初始化。需要最新 Codex 录制时，用返回的定点 `refresh --file`，保持来源根与缓存不变。AGY 会检查文件元数据，复用未变化会话，仅解码变化的会话；缓存失败的内存回退有独立 warning。

activity 仍要求显式 `--data-dir`。日常可传同一个 `~/.cache/agenttracemesh/reader`，复用已有来源索引；没有最新覆盖计数时保留未知，不必为了填齐数字重建全部历史。

Web 的 `~/.cache/cchv` 含旧历史、人工元数据及主索引，暂时单独保留。不能把 Web、CLI 与早期实验的任意 SQLite 文件直接互换。当前复用规则不会强行合并它们或自动清理用户选定的实验目录。

## 体积大时先区分原因

先看目录数量、SQLite 主库与 WAL，再看是否有正在使用的进程。WAL 是数据库事务的一部分，使用 SQLite `wal_checkpoint(TRUNCATE)` 回收，不直接删文件。若返回 busy，保留 WAL；不要杀其它任务。大量空闲页可在空闲时用原生 VACUUM 回收；不用为了即将退役的副本先重写整库。

原录制已不存在时，派生库可能保留唯一历史。此时先保留或压缩归档，不能宣称“refresh 一次就能恢复”。归档前关闭本轮连接，核对占用；检查压缩流和目录可读取后，再移除展开副本。缓存正在变化时保留原目录。

本机 2026-11-05 清理记录与实际性能见[验收进度](../plans/activity-cache-20261105/progress.md)。本机归档位置为 `~/.local/share/cchv/cache-archives/cchv-retired-20261105.tar.zst`，包含17个退役缓存目录。恢复旧 Codex 缓存：

```bash
tar -I zstd -xf ~/.local/share/cchv/cache-archives/cchv-retired-20261105.tar.zst \
  -C ~/.cache cchv-codex
cchv reader --source codex --source-path ~/.codex/sessions \
  --data-dir ~/.cache/cchv-codex search --query '关键词' --limit 3
```

仅在恢复目标不存在时解包；已有目标先检查，不覆盖正在使用的索引。查询归档恢复的旧库时先读结果，不自动 refresh：原录制缺失时刷新会移除对应派生条目。压缩归档不是活跃搜索索引，恢复后才可查询。
