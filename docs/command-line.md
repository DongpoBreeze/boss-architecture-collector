# 命令行参考

隔离安装后，可分别用 `boss-architecture-collect`、`boss-architecture-process`、`boss-architecture-desktop` 替代 `python collect_architecture.py`、`python process_architecture.py`、`python desktop_app.py`；参数保持一致。源码的 PowerShell / BAT 启动器继续可用，不包含在 wheel 中。源码默认输出根不变；安装态的默认可写目录见 [README](../README.md)。

日常使用推荐[双页面桌面程序](../README.md)。本文集中记录兼容 CLI：它们不建立 GUI 的 `task_manifest.json`，也没有“每步运行后人工确认”的任务状态机。不要将 CLI 运行目录、原始采集源和具名审核任务目录混用。所有示例均在项目目录执行，示例路径需替换。

推荐 Python 3.11+；最低 3.10，读取 AI 的 TOML 配置需额外安装 `tomli`，3.11+ 使用标准库 `tomllib`。实际采集 / JD 需要安装 [boss-zhipin-scraper](https://github.com/eatmoreduck/boss-zhipin-scraper) 及其依赖，并使用独立浏览器 CDP profile；只读本地审核和 dry-run 不需要登录。模型凭据与服务授权约束见[工作流契约](desktop-review-workflow.md)。

## 桌面入口

```powershell
.\run_desktop.bat --root "D:\岗位数据\采集源" --scraper-root "D:\工具\boss-zhipin-scraper"
.\run_desktop.ps1 -Root "D:\岗位数据\采集源" -Config ".\config\architecture_collection.json" -Python "C:\Python311\python.exe"
```

`run_desktop.bat` 与 `run_desktop.ps1` 支持以下启动器参数，相对路径按调用时当前目录解析：

| 参数 | PowerShell 写法 | 作用 |
| --- | --- | --- |
| `--root` | `-Root` | 预填完整采集根，可在 GUI 内选择 |
| `--scraper-root` | `-ScraperRoot` | 预填上游目录，可在 GUI 内选择 |
| `--config` | `-Config` | 检索配置，默认项目 `config/architecture_collection.json` |
| `--python` | `-Python` | 指定本次 GUI 解释器，不切换系统 Python |
| `--help` | `-Help` | 只显示启动器帮助 |

解释器发现顺序为项目 `.venv` / `venv`、已激活 virtualenv / Conda、`py -3`、`python`；跳过版本不足或缺少 Tk 的候选。启动器不自动安装依赖、不改系统执行策略；`.bat` 的策略设置只影响本次启动进程。正常运行不保留额外终端，错误提示隐藏异常正文和密钥。

前台排查可用 `python desktop_app.py --root "D:\岗位数据\采集源"`。仅 Python 入口接受旧工作台开关，启动器不透传它：

```powershell
python desktop_app.py --legacy-workbench --root "D:\岗位数据\采集源"
```

## 列表采集

先检查计划，不连接浏览器或创建结果文件：

```powershell
python collect_architecture.py --dry-run --cities 上海 --limit 2
```

显式开始 / 续采：

```powershell
python collect_architecture.py --root "D:\岗位数据\采集源" --scraper-root "..\boss-zhipin-scraper" --pages 10 --batch all --setup-browser
```

| Python 参数 | 默认值 / 行为 |
| --- | --- |
| `--config PATH` | 项目 `config/architecture_collection.json`；可传 GUI 保存的新方案 |
| `--root PATH` | 项目 `job-result/<当天日期>-architecture-screen-full`；续跑必须显式传同一根 |
| `--scraper-root PATH` | 依次检查显式路径、`BOSS_SCRAPER_ROOT`、项目内 / 旁边 / 当前目录的上游 |
| `--python PATH` | 上游采集器解释器；否则检查 `BOSS_SCRAPER_PYTHON`、上游 `.venv`，再用当前解释器 |
| `--batch all\|current\|target` | `all` |
| `--cities NAME_OR_ID ...` | 全部城市，可传“上海”或 `shanghai` 等 |
| `--pages 1..10` | 每组合最多 10 页，默认 10 |
| `--limit N` | 所选组合顺序前 N 组，应用于分片之前；不是岗位数或新增组数 |
| `--total-shards N`、`--shard I` | 默认 1、0；全局序号轮流分配，I 从 0 开始 |
| `--browser chrome\|edge` | `chrome` |
| `--cdp-port PORT` | Python 入口始终默认 9222；Edge 并行必须显式设为 9223 |
| `--profile-dir PATH` | 所选浏览器独立 profile；默认 `%USERPROFILE%\.boss-zhipin-scraper\chrome-profile` / `edge-profile` |
| `--setup-browser` | 先准备所选浏览器并等待人工登录；兼容别名 `--setup-chrome` |
| `--setup-only` | 只准备浏览器并退出，不采集组合 |
| `--wait-on-gate` / `--no-wait-on-gate` | 默认等待人工验证；后者保存 `paused` 后返回，不等待输入 |
| `--continue-on-error` | 默认关闭；开启后失败 / 验证退出当前组合后继续后续组合，不绕过验证 |
| `--verbose-upstream` / `--quiet-upstream` | 默认显示逐条岗位明细；二者互斥 |
| `--dry-run` | 只打印组合，不连接 CDP，不落盘结果 |

`--gui-interactive` 是桌面 stdin 协作参数，普通终端无需使用。登录准备示例：

```powershell
python collect_architecture.py --setup-only --root "D:\岗位数据\浏览器准备" --scraper-root "..\boss-zhipin-scraper"
```

CLI 的登录准备仍会初始化所指定根目录与清单；若不想触碰既有采集源，使用独立准备目录或 GUI“准备浏览器 / 登录”（使用临时根）。不要让并行实例共用 CDP 端口或 profile。

### PowerShell 包装与双账号

`run_collect.ps1` / `run_collect.bat` 提供 `-Batch`、`-Pages`、`-Root`、`-ScraperRoot`、`-Browser`、`-CdpPort`、`-TotalShards`、`-Shard`、`-ProfileDir`、`-SetupBrowser`、`-SetupOnly`、`-DryRun`、`-ContinueOnError`、`-NoWaitOnGate` 和日志开关。`-SetupChrome` 是旧别名，`-SetupEdge` 选择 Edge 并准备登录。

与 Python 入口的差异：

- 包装器按浏览器自动选端口：Chrome 9222、Edge 9223；`-CdpPort` 可覆盖。
- `-Python` 选择运行包装器下 CLI 的解释器，不等于传给上游的 `--python`。
- 包装器固定默认配置，没有 `-Config`、`-Cities` 或 `-Limit`；自定义方案或精细筛选用 Python CLI 或 GUI。
- 单 / 双账号采集包装器的 `Root / ScraperRoot / ProfileDir` 相对路径按项目目录解析。

双账号准备与采集使用同一个显式根目录：

```powershell
.\run_collect_dual.ps1 -SetupOnly -Root "D:\岗位数据\采集源" -ScraperRoot "..\boss-zhipin-scraper"
.\run_collect_dual.ps1 -Root "D:\岗位数据\采集源" -ScraperRoot "..\boss-zhipin-scraper"

# 只检查分片，不打开窗口、浏览器或改写结果
.\run_collect_dual.ps1 -DryRun
```

正常模式有意打开两个可见交互终端：Chrome 端口 9222 / 分片 0/2，Edge 端口 9223 / 分片 1/2。默认全量 560 组时各 280 组，使用同一固定结果根；不传根则创建当天默认根。`-SetupBrowsers` 在采集前准备两个浏览器，`-SetupOnly` 只准备；`-ChromeProfileDir`、`-EdgeProfileDir` 可分别覆盖 profile。中断后重复原命令续跑；只需人工处理受影响的分片。

JD 补抓也支持在同一运行中使用两个浏览器：使用 Python 后处理 CLI 的 `--browser-mode dual` 并指定 `--chrome-port` / `--edge-port` 与 `--chrome-profile-dir` / `--edge-profile-dir`（默认 9222/9223）。JD 名单按输入稳定地连续切分，Chrome 获得前 `ceil(N/2)` 条，Edge 获得余下记录；两侧输出先写入 `details.chrome.json` / `details.edge.json`，再按原输入顺序合并到 `details.json`。合并输出保证 `job_id` 唯一；即使输入重复，也不输出重复行，但仍建议先修正上游输入。

任一侧遇到登录 / 安全验证时，该侧保存 `paused`；另一侧已完成的 JD 仍会保留。修复验证后重复原命令续跑会读取两侧旁车文件，跳过已经是 `success` 的 `job_id`，只重试暂停或失败条目。不要让两个实例共用同一个 CDP 端口或 profile。

续跑在重新划分 Chrome / Edge 名单前先合并主检查点与两侧检查点，优先保留完整 JD，避免侧文件已落盘而主文件尚未更新时丢失正文。预检失败的一侧不会继续导航详情；合并持久化失败会通知两侧停止。内部适配器 `jd_parallel.py --browser-readiness-file PATH` 接受按 `chrome / edge` 保存的预检状态 JSON，由后处理自动传递；正常使用无需手工构造。

### 输出与断点续跑

源根包含 `raw/current`、`raw/target` 中的列表 JSON / 日志，`checkpoints` 及 `collection_manifest.json`；GUI 另保存实际方案快照和桌面日志。清单更新使用文件锁合并，分片结果文件名唯一。

`success` 或正常 `no_data` 且文件有效的组合可跳过；`paused` 要人工检查，`failed` 下次重试。有效空响应结束当前组合，保留已抓列表后进入下一组；零岗位伴随响应未捕获 / JSON 解析异常，或无有效空响应证据时记可重试 `failed`，不证明“没有岗位”。非空结果不因后页同类告警被判为空失败，登录 / 验证优先暂停。

CLI 和 GUI 都在写入采集清单、跳过已完成组合前核对组合语义。同一组合 ID 的关键词或其他已知检索条件发生冲突会阻断续写，不把旧成功结果当作新查询结果。

旧 `no_data` 的对应日志若有异常空结果证据，会重新采集；日志缺失 / 不可读保留兼容跳过，旧累计告警可能保守重采一次。新记录按最后一次尝试诊断，原因字段保存固定提示和 `retryable=true`，不保存原始响应或凭据。本次不会无限自动重试。

## 兼容后处理 CLI

`process_architecture.py` 从原始采集根读取数据，在指定输出根执行阶段；不是 GUI 的人工确认任务。默认 `--stage all` 会自动串联，若需逐步核对须显式指定阶段。推荐先用新输出目录执行本地初筛：

```powershell
python process_architecture.py --root "D:\岗位数据\采集源" --output-root "D:\岗位数据\处理运行-01" --city 上海 --sample-size 50 --sample-strategy balanced-dedup --stage shortlist
```

后续沿用同一 `--root`、`--output-root` 和范围，分别运行 `--stage fetch-jd`（需 `--scraper-root`）、`--stage llm`、`--stage report`。JD 仅补正文，不做职责筛选；模型和最终规则遵守[工作流契约](desktop-review-workflow.md)。纯报告生成不请求浏览器或模型。

| 参数 | 默认值 / 注意事项 |
| --- | --- |
| `--root PATH` | 必填，原始采集根，不是任务目录或 `prepared` |
| `--config PATH` | 默认检索配置；须能识别源中实际用过的城市、关键词与经验 catalog |
| `--city NAME_OR_ID`、`--tracks current\|target\|all` | 默认全部城市、`all` |
| `--sample-size N` | 默认不限，按去重岗位数计 |
| `--sample-strategy sequential\|balanced-dedup` | CLI 默认 `sequential`；GUI 任务使用 `balanced-dedup` |
| `--output-root PATH` | 覆盖默认路径；首次选未用目录，续跑才复用，CLI 不保证唯一新建 |
| `--run-timestamp YYYYMMDD-HHMMSS` | 同时提供城市和数量时，使用源内 `runs/<时间戳>-<城市>-<数量>`；被 `--output-root` 覆盖 |
| `--stage merge\|shortlist\|fetch-jd\|llm\|report\|all` | `all`；merge / shortlist 都从 raw 重建索引与初筛输入 |
| `--workers N`、`--jd-workers N` | AI 默认 10，JD 默认 3，均 1–32 |
| `--conversation-size N` | 每个并行 AI 会话最多判断的不同岗位数，默认 1，逐岗独立；范围 1–100，>1 仅作显式兼容选项。纠正格式与网络重试不算新岗位 |
| `--browser-mode single\|dual` | JD 默认使用 Chrome；`dual` 同时使用 Chrome / Edge，按输入顺序连续均衡分片 |
| `--chrome-port PORT` / `--edge-port PORT` | 双浏览器 CDP 端口，默认 9222 / 9223，不得共用 |
| `--chrome-profile-dir PATH` / `--edge-profile-dir PATH` | 分别指定两个浏览器 profile；不传时使用已准备的默认 profile |
| `--batch-size N` | CLI 默认 10，范围 1–100；GUI 默认 30，不是并发数 |
| `--detail-speed-profile fast\|safe` | `fast`，控制页面加载 / 滚动等待；每页仍滚动 3 次 |
| `--scraper-root PATH`、`--python PATH` | JD 上游目录及其解释器；目录也可用 `BOSS_SCRAPER_ROOT` |
| `--timeout N`、`--max-retries N` | 请求超时默认 120 秒，有限重试默认 3 |
| `--codex-home PATH` | 显式指定后改用该目录的 Codex `config.toml` / `auth.json`，不读取项目 URL 或 Credential Manager 密钥；不指定时 AI 使用本项目模型配置 |
| `--base-url`、`--api-key`、`--model`、`--reasoning-effort` | 仅覆盖本次请求；默认读取项目本机配置（初始模型 `gpt-5.6-luna`、强度 `max`），URL 从项目配置读取，API Key 从 Windows Credential Manager 读取 |
| `--api-mode responses\|chat_completions` | 仅覆盖本次 API 协议；不传时沿用项目 / 显式 Codex 配置，旧配置缺省为 `responses`。Chat 模式使用 `/chat/completions`，强度仅在匹配已保存验证时发送 |
| `--prompt-file PATH` | 可选 UTF-8 完整提示词快照，仅供 AI / 报告步骤使用；同一轮请求和报告应使用同一文件。不传时沿用项目默认提示词；GUI 自动传入本任务保存的快照 |
| `--wait-on-gate` / `--no-wait-on-gate` | 默认交互等待；后者保存 paused 并返回 |

CLI 默认输出规则：同时给城市和数量时是 `<源根>/test-runs/<城市ID>-<数量>/`，例如 `shanghai-50`；否则直接用源根，可能更新原有 `prepared`。`--output-root` 优先于所有默认规则，不要对旧报告试运行。输出常见文件包括 `raw_sample.json`、`prepared/merged_jobs_index.json`、`prepared/jd_shortlists/details.json`、`prepared/llm_reviews.json`、`prepared/hai_tou_pool.json` 和 `prepared/海投分层清单.md`。

列表初筛为每岗保存互斥的 `list_queue`（`relevant` 明确相关、`verify` 待核实、`excluded` 剔除）及原因；`list_pass=true` 包含前两类，不设最低评分。`prepared/jd_shortlists/jd_input.json` 保留两类合并建议，同时输出 `jd_relevant.json`、`jd_verify.json`，并在元数据 / manifest 保存三类 `list_queue_counts`（含零值）。这些是方向队列，不是最终投递分层；CLI 默认沿用合并名单补 JD，需人工逐步选择和确认时使用桌面审核任务。

报告与桌面共用 `report_decision / report_pool_eligible`：人工已确认剔除优先，AI 信息不足、失败和未运行项归待核实，不写入 `hai_tou_pool.json`。全量 JSON 和 Markdown 保留对应判断、来源及审计信息，数量按相同分类统计。

自定义采集方案若含新增关键词，CLI `--config` 需包含其历史 catalog；GUI 新建任务会自动合并源内方案，CLI 不做该交接。未来检索选择不能用于删除既有 raw 来源。

`run_process.ps1` / `run_process.bat` 可使用对应 `-Root`、`-Stage`、`-City`、`-SampleSize`、`-Workers`、`-ConversationSize`、`-JdWorkers`、`-BatchSize`、`-ScraperRoot`、`-DetailSpeedProfile`、`-Timeout`、`-MaxRetries`、`-CodexHome`、`-BaseUrl`、`-ApiKey`、`-Model`、`-ReasoningEffort`、`-ApiMode responses|chat_completions`、`-NoWaitOnGate`。它没有 `OutputRoot / Tracks / SampleStrategy / Config / RunTimestamp` 参数；需要这些控制时用 Python CLI。`-Python` 只选 CLI 解释器，相对数据路径按当前目录传递。

AI 默认逐岗独立请求，只提交完整规则与当前岗位 JD，不跨岗位续接或重放历史；同一已配置模型按 `--workers` 并发处理。Responses 默认 `store=false`，Chat 的 `messages` 只含当前岗位。各岗位独立校验、落盘和重试；继续时跳过有效成功检查点，不因恢复独立请求整体重跑。

`--conversation-size` / 包装器 `-ConversationSize` 默认 1，桌面固定传入 1。大于 1 的多岗会话仅保留为显式兼容选项；可用 Responses 续接或有界本地历史，历史仍可能参与计费。旧 manifest 会话计数字段保持兼容；独立模式记录 `conversation_mode=stateless`、`conversation_session_size=1`，续接与历史重放计数为 0。模型缓存签名仍描述当前岗位的逻辑输入，改变会话模式不清空有效结果。

```powershell
.\run_process.bat -Root "D:\岗位数据\采集源" -City 上海 -SampleSize 50 -Stage shortlist
# 模型阶段沿用 GUI 保存的配置；仅本次切换为 Chat 协议：
python process_architecture.py --root "D:\岗位数据\采集源" --output-root "D:\岗位数据\处理运行-01" --city 上海 --sample-size 50 --sample-strategy balanced-dedup --stage llm --api-mode chat_completions
```

JD 补抓会检查专用 Chrome CDP / 登录，必要时尝试准备浏览器并等待，最长登录检查 300 秒；本机 CDP 地址加入子进程代理绕行列表。遇验证保存并等待，终端按 Enter 重试当前批次，输入 q 可退出后续跑。使用 `all` 遇验证不会继续 AI / 报告；子进程致命失败不会被当作完成。`--no-wait-on-gate` 不代表验证通过，不能只凭退出码判定采集完整。

模型初始默认使用 `gpt-5.6-luna` / `max` 和 Responses API；AI 页面保存的项目配置也会由 CLI 默认读取。项目 URL 读取本机项目配置，API Key 读取 Windows Credential Manager；两者均不写入项目结果或全局 Codex 配置。CLI 显式 `--base-url`、`--api-key`、`--model`、`--reasoning-effort`、`--api-mode` 只覆盖本次请求，不保存配置。Chat 默认不发送强度；可在 GUI 选择流式选项、验证强度并保存，CLI 读取同一份验证记录。覆盖后的地址、模型、强度、流式选项必须匹配验证记录，否则不发送参数；`--reasoning-effort` 本身不能绕过验证。更换有效请求配置后显式重跑会使不匹配的缓存失效；Chat 仅实际发送的强度参与缓存签名。默认 Responses 沿用原签名，旧成功缓存仍可复用，历史报告不会自动改写。GUI 的协议选择、读取模型和极短连接测试见[工作流契约](desktop-review-workflow.md)。

## 同样本离线重筛

仅使用兼容 CLI / 旧工作流中已保存的同批岗位、JD 和成功模型记录，不重新抽样、不启动浏览器或请求模型，不改原运行：

```powershell
python rescreen_saved_run.py --source-run "D:\岗位数据\采集源\test-runs\shanghai-50" --expected-size 50
```

`--source-run` 必须是包含 `raw_sample.json` 和 `prepared/merged_jobs_index.json` 的已分类运行，岗位 ID 必须唯一且完全一致。可用 `--expected-size` 核对数量。默认在旧运行旁建立唯一新目录，也可用 `--output-root` 指定尚不存在、且不在旧运行内的新目录。

新增输出包括新三层报告、`screening_comparison.json` 和 `规则放宽前后对照.md`；缺 JD 的新增保留项仍是待核候选，不代表资格或时效已核实。新 GUI 任务的输出分布在 `stages` / `runtime`，不能直接把任务根当成该脚本的 `--source-run`。

## 只读检查入口

```powershell
python desktop_app.py --help
python collect_architecture.py --help
python process_architecture.py --help
python rescreen_saved_run.py --help
python collect_architecture.py --dry-run --cities 上海 --limit 2
.\run_collect_dual.ps1 -DryRun
```

这些帮助 / dry-run 不执行真实采集或模型请求；后处理没有 dry-run 参数。测试命令与验证边界统一见[开发文档](development.md)。
