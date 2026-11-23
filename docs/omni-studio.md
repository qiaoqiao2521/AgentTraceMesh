# Omni Search & Studio 产品工作台

`history_core.omni` 与 Web `/studio` 在不改动原有只读历史源与既有 `/workspace` 契约的前提下，提供跨来源、穿透多账号槽位（如 `AccountMesh` 的 `~/.agy-slots/*`）的统一检索、成事雷达与公众号每日三篇选题工作台。

## 为什么新增这一层

1. **穿透多账号槽位失忆**：切换 `~/.gemini` 符号链接槽位（如从 `darring` 切到 `576`）后，单路径发现会漏掉非当前槽位的数百场历史对话。`omni` 自动扫描 `~/.cache/agenttracemesh/reader/machine-*` 中的各槽位派生缓存并标注 `[darring]` / `[576]`。
2. **消除单源逐个搜与严格关键字门槛**：支持**留空直接按成事价值浏览**、`|` 或搜索（`blender | glb | threejs`）、空格与搜索、`-排除词`、会话 ID 直查，以及**项目路径子串模糊匹配**（直接填 `blender` 即可，无需完整绝对路径）。
3. **跨槽位内联抽屉阅读**：非当前激活槽位或归档索引中的会话，可直接在 `/studio` 右侧抽屉按公开脱敏消息阅读全文并一键提取公众号草稿包。
4. **每日三篇公众号选题台**：
   - **第 1 篇 · 大厂前沿更新**：Google / OpenAI / Claude 等一手发布（当本机 `127.0.0.1:8768` 隧道在线时自动分流展示）。
   - **第 2 篇 · HN 与爆炸新闻**：Hacker News 高赞争议与 GitHub Trending 爆款开源拆解。
   - **第 3 篇 · 我的个人经验全景库**：读取 `data/experience_vault.json` 中从本机全量对话提炼的详细成事案例，支持按 6 大工程领域筛选、查看完整 SOP 与边界，并一键复制喂给「木乔公众号」Gem 的 `material.md` 指令包。

## 无头 CLI 用法

```bash
cd /home/muqiao/桌面/TraceMesh

# 1. 盘点本机所有来源、AccountMesh 槽位与会话总量
python3 -m history_core omni stores

# 2. 跨源全槽位舒服搜索（支持 | 或语法、子串项目筛选、成事过滤）
python3 -m history_core omni search -q "blender | glb | threejs" --project media --accomplished --limit 15

# 3. 留空关键词：按成事价值盘点全机高价值对话与项目簇
python3 -m history_core omni radar --topic robotics --limit 20

# 4. 查询个人实战经验精编库
python3 -m history_core omni vault --category ai_agent -q "SQLite"
```

## Web 启动入口

```bash
python3 app.py --host 127.0.0.1 --port 8787
# 浏览器打开 http://127.0.0.1:8787/studio （或从 /workspace 顶栏一键进入）
```

## 跨槽 markers 与漏调复盘

```bash
python3 -m history_core omni markers --from 2026-11-15T00:00:00+08:00 --to 2026-11-22T05:00:00+08:00 --timezone Asia/Shanghai --sources all --slots all --kind skill --group-by skill,source
python3 -m history_core omni markers --from 2026-11-15T00:00:00+08:00 --to 2026-11-22T05:00:00+08:00 --review-misses studio
```

时间窗口为左闭右开。无时区输入按 `Asia/Shanghai` 解释。计数采用事件或公开消息的发生时间，时间依据在 `time_basis` 中。来源及槽位默认 `all`，也可传逗号分隔的名称。`--event skill_file_read` 可单查 IDE 的读取请求。`--kind output_style` 可查学习风格。默认 Skill 统计把风格事件另列在 `output_style`。

发现器将 `~/.codex-slots/*/codex/sessions` 和已知 IDE 物理库与缓存对应。活动软链归入 `slot_aliases`，`_shared` 不占账号名额。Codex 按原生会话 ID 合并 `@recording-` 录制，IDE 按 cid 合并。同一次事件保留各槽定位。检查 `recordings` 可看到已证实的同前缀、分叉和前缀关系待核的副本。按槽位查看时，同一会话可出现在多列。

同一 cid 有多份完整副本时，`representative_store` 优先选刷新时记录的较新 transcript 修订，时间相同时取较长前缀。事件时间仍取规划步骤的 `created_at`。

用 `summary.sessions_with_event / observable_active_sessions` 读取会话率。分母包括窗口内有公开活动且具有对应记录能力的零事件会话。部分覆盖中的可见事件计入 `total_events`。覆盖未知的会话单列，分母为零时 `rate` 为 `null`。`event_coverage` 分别给出原生请求、独立声明和 IDE 读取各自的分母。

每个 store 给出修订、覆盖及 `capture_errors`。旧库缺少错误记录时，该字段为 `null`。无法定时的事件见 `unknown_time_events`。全量计数与明细页大小无关。翻页使用 `--offset`、`--limit`、`--index-revision`。

`--review-misses studio` 检查工作室常见路由，`all` 加入通用路由。输出公开用户消息中的匹配短句、会话 ID、首次请求、提醒及首次记录的定位。统计区分已见、提醒后首次记录和明确要求后仍未见证据。窗口前的已有记录也参与判断。示例、盘点和技能创建等候选另列。覆盖、分叉或历史可用性存疑时保留 `unknown`。

默认查询读取现有缓存。加 `--refresh` 会刷新所选且已绑定本地来源的派生缓存，包括 IDE transcript。大录制可先按文件执行 Codex 投影刷新，命令及覆盖字段见 [标记与大文件投影](trace-markers.md#codex-大文件投影)。独立缓存可用 `python3 -m history_core omni --data-dir <缓存目录> markers …` 查询。

## 安全与边界

- 查询使用只读 SQLite 连接。显式刷新写入派生索引，原始会话按只读方式打开。
- 仅检索并展示公开记录（`user`、`assistant`、`tool`），内部 `thinking` / `reasoning` 块不参与召回与抽屉渲染，输出统一经过 `redact_text` 常见密钥脱敏。
- `--demo` 模式严格隔离在合成演示目录内，不扫描用户家目录、槽位或本地端口。
