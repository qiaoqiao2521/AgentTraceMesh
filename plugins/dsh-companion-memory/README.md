# TraceMesh DSH Companion Memory

TraceMesh 的可选个人记忆插件，保存人物、偏好、共同经历和承诺，跨会话召回。
包名为 `@tracemesh/dsh-companion-memory`，命令为 `tracemesh-memory`。
它直接接入 DSH，不依赖 ControlMesh，也不随 TraceMesh 的 Python 历史读取核心自动加载。
记忆以 Markdown 保存在本机，可直接编辑；需要 DSH 0.2.1-rc.2 和 Node.js 22 或更新版本。

## 安装

从 AgentTraceMesh 仓库根目录安装本地源码：

    dsh plugin --profile web add ./plugins/dsh-companion-memory --config.auto-install-peers=false

插件复用当前 DSH 安装提供的 peer packages，不另装一套 Harness。
重启该 profile 后生效。也可以给 headless profile 安装同一插件。
相同 companion 身份共享记忆；安装插件不会改模型、登录和代理。
这里使用本地源码包，不假设已将该命名空间发布到 npm。

### 从原 CM 插件切换

停止正在使用旧插件的 DSH profile，在它的插件配置中移除或停用
`@controlmesh/dsh-companion-memory` 的旧注册，再从上面的 TraceMesh 路径安装。
保留原来的 `companion`、`dataDir`、`DSH_HOME`、`timeZone` 和上下文预算；重启该 profile。
不要同时保留两个有效的插件注册，以免重复贡献上下文或注册同名工具。

这次迁移改变代码归属和包/CLI 名称，现有数据无需搬家、导出或重新导入。
Cordis row id 和插件名仍为 `companion-memory`，模型工具仍为 `companion_memory`；
默认数据路径和文件格式保持兼容。原来显式设置了 `dataDir` 的 profile 继续使用该目录。
源码迁移不会修改用户已有 DSH 配置，也不证明用户机器已经完成安装切换。

安装后的 CLI 名称由 `cm-companion-memory` 改为 `tracemesh-memory`；
直接使用源码时，仍可运行 `node plugins/dsh-companion-memory/cli.js`。

可先用临时覆盖层试用，无需安装：

    dsh headless --patch ./plugins/dsh-companion-memory/cordis.patch.yml "请记住，我喜欢桂花茶。"
    dsh headless --patch ./plugins/dsh-companion-memory/cordis.patch.yml "我平常喜欢喝什么？"

这两次是真实模型调用，沿用 DSH 本身的模型配置。
在 Web 对话里也可以说“纠正这条记忆”“这件事完成了”“忘掉这件事”。

## 能做什么

| 操作 | 行为 |
| --- | --- |
| remember | 选择性保存事实、偏好、人物、经历、承诺；相同活动内容不重复保存 |
| search / review | 检索或分页查看；review 包含已完成与有争议的条目 |
| update | 原条目内纠正内容、类别、固定状态和生命周期 |
| forget | 删除选定条目的物理文件 |
| resolve | 将承诺标为已完成，不再自动提醒 |
| reflect | 有 content 时保存当天总结；无 content 时读最近一天，可指定 date |

每轮自动召回固定记忆、未完成承诺、相关条目与近期经历。
模型在正常对话中调用工具选择值得保存的内容；不逐轮归档聊天或工具输出。
来源包含真实 DSH session/message ID；可附一小段原话，不复制整轮消息。
后台子 Agent 默认不接收或写入这份私人记忆。

每日总结由用户请求触发，由当前对话模型整理，保存在独立文件中。
它是可修订叙述，不会自动晋升成事实。没有后台“做梦”模型或主动联系服务。
承诺日期只是上下文；需要定时通知时，使用 DSH 现有调度能力。

## 配置与数据

修改插件的 Cordis row 配置；整个 config 替换时重述需要的字段：

    - id: companion-memory
      config:
        companion: companion
        maxContextChars: 6000
        timeZone: Asia/Shanghai

可选 dataDir 指定记忆基目录。
默认位置为 $DSH_HOME/companion-memory/<companion>/；
DSH_HOME 未设置时使用 ~/.dsh。不要把该目录设在源码仓库内。

| 相对该 companion 目录的路径 | 内容 |
| --- | --- |
| `memories/<id>.md` | 个人记忆与来源、类别、生命周期元数据 |
| `reflections/YYYY-MM-DD.md` | 用户请求生成的可修订日总结 |

条目头部是两个 --- 之间的 JSON 元数据，正文是普通 Markdown。
直接编辑正文，下次请求立即读取；没有哈希锁，也不需要重建索引。
坏条目发出 WARNING 并跳过，保留原文件；其余记忆和对话继续运行。
多进程同时改同一个条目采用最后写入版本，适合个人单 Harness 使用。

检索采用本地词语与中文二字片段匹配，再结合固定状态、重要度和时间排序。
它不包含神经语义搜索，不承诺任意同义改写均可命中。
默认 6000 字符是上下文预算；更长条目仍可通过工具查看。
动态记忆通过 DSH context 接口注入，固定工具说明保持稳定。

forget 只删除选定记忆。DSH 原聊天和已经写过的每日总结仍是独立文件。
需要完整删除某段经历时，同时处理这些来源；插件不擅自清理原生聊天。
停用或卸载插件会撤销工具和上下文贡献，保留本机记忆文件。

## 显式导入更早的 CM MEMORY.md

已有 DSH 插件记录无需导入。以下入口只用于更早的、明确选定的 CM `MEMORY.md`，
保留兼容子命令 `import-cm`，不读取或运行旧 CM 代码：

    node plugins/dsh-companion-memory/cli.js import-cm /path/to/MEMORY.md
    node plugins/dsh-companion-memory/cli.js import-cm /path/to/MEMORY.md --apply

默认预览；加 --apply 才写入。支持 Fact、Preference、Person 三类。
Project、Decision 留在项目系统；失效、被取代和争议条目跳过。
原来源元数据保留，重复导入不会增加相同的活动条目。
不读取 daily notes、DREAMS.md、私有历史或旧 CM 配置。
发生条目写入错误时，结果的 `errors` 保留原因，CLI 返回非零；不把部分失败报告为成功。
跳过 Project、Decision 或非活动条目属于上述选择性边界，不算写入错误。

## 验证与来源

    cd plugins/dsh-companion-memory
    npm ci --ignore-scripts
    npm run check
    npm test

行为测试使用真实 DSH 工具注册器、Agent Loop、会话和上下文组装器。
模型响应为本地脚本，不使用网络、登录或真实个人数据。
这些测试证明插件接线和持久化行为，不证明真实模型的记忆选择质量。
`npm run test:store` 只运行 Node 标准库即可完成的存储和迁移测试，无需 DSH peer packages。
迁移测试使用临时合成目录验证旧数据路径、已有 Markdown/日总结读回、导入预览、
重复导入幂等及显式 `dataDir`；不会访问用户已有记忆或修改 DSH 安装。

原实现的来源分析曾使用 xerj 索引 CM、Miru、kiwi-mem、MemOS 和 DSH。
历史研究见
[原研究与决策](https://github.com/qiaoqiao2521/ControlMesh/blob/59014053939707b98a9fea3510400fdf02d468f5/plans/dsh-companion-memory/findings.md)
和 [原验证记录](https://github.com/qiaoqiao2521/ControlMesh/blob/59014053939707b98a9fea3510400fdf02d468f5/plans/dsh-companion-memory/progress.md)；
迁移来源及许可见 [NOTICE.md](NOTICE.md)。这些是可追溯历史链接，CM 和 xerj 都不是运行依赖。
