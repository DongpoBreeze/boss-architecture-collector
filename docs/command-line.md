# 命令行参考

隔离安装后的入口与源码参数保持一致：

| 安装入口 | 源码入口 | 用途 |
| --- | --- | --- |
| `boss-architecture-desktop` | `python desktop_app.py` | 同窗采集、四步审核、投递导出 |
| `boss-architecture-export` | `python delivery_export.py` | 读取已确认审核任务，导出 JSON |
| `boss-architecture-collect` | `python collect_architecture.py` | 列表采集 |
| `boss-architecture-process` | `python process_architecture.py` | 显式兼容后处理与报告 |

日常使用推荐[三个模块的桌面程序](../README.md)。导出 CLI 直接读取已有 `task_manifest.json`；采集与兼容后处理 CLI 不建立人工确认任务，不能用它们的运行目录替代审核任务。所有示例的路径需按实际环境替换。源码的 PowerShell / BAT 启动器继续可用，不包含在 wheel 中；安装态默认可写目录见 [README](../README.md)。

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

默认桌面的 01 页面直接管理 Chrome + Edge 的后台采集进程，分别连接 9222 / 9223。按勾选与数量上限确定本次组合后，再按浏览器和并发槽分为互不重叠的分片；共享输出根、方案快照和续采清单。页面提供两侧的状态、日志、登录准备及验证恢复，用户无需另开命令行双采集窗口。下文 `collect_architecture.py` 仍是单个浏览器 / 分片的入口，双终端启动器继续可用。

01 的“每浏览器采集并发”默认 3，可设 1–8；每浏览器仅准备一次后，再为各槽启动独立页面和分片。翻页等待最短 / 最长秒数默认 12 / 22，可设有限小数范围 0–300，相同时使用固定间隔；所有设置对下次开始 / 续采生效。CLI 入口仍串行处理自己的分片，桌面并发由多个互不重叠的分片组成。

## 投递 JSON 导出

只读取已经完成并确认 `import / shortlist / fetch-jd / llm` 的具名任务。不会运行报告、补抓 JD、请求模型或执行投递。导出以当前最终保留名单为准，不再次验证 AI 判断、记录或签名；已有 AI 信息仅作记录，`ai_review.validation="not_checked"`。目标 ID / 链接缺失、无效、冲突或重复仍列入 `blocked_jobs`。

```powershell
python delivery_export.py --task "D:\岗位数据\审核任务\task-…"

# 安装态等效入口；--output-dir 指定保存父目录
boss-architecture-export --task "D:\岗位数据\审核任务\task-…" --output-dir "D:\岗位数据\delivery-exports"

# 仅导出指定字段；不传 --fields 时导出完整 JSON
boss-architecture-export --task "D:\岗位数据\审核任务\task-…" --fields jobs.title jobs.company jobs.ai_review.reason
```

| 参数 | 行为 |
| --- | --- |
| `--task PATH` | 必填，具名审核任务目录；不能传原始源、CLI 运行或旧报告目录 |
| `--output-dir PATH` | 可选，保存父目录；默认 `<任务>/delivery-exports`，每次在父目录下创建唯一子目录 |
| `--fields FIELD [FIELD ...]` | 可选，仅导出指定叶子字段，使用点分层级（如 `jobs.title`、`jobs.ai_review.reason`、`summary.ready_count`）；已知但当前无值的字段自动省略，省略参数时导出全部有值字段 |
| `--help` | 显示帮助，不读取岗位或请求服务 |

输出为 `<父目录>/<时间戳>-<uuid>/<前三个字段中文名>.json`，CLI 成功时打印实际路径。文件名取字段列表顺序中前三个已选且有值叶子字段的中文名称，用下划线拼接；不足三项使用实际项数。例如选择 `jobs.title jobs.company jobs.city jobs.salary`，当前城市无值时文件名为 `岗位名称_公司_薪资.json`。完整 JSON 名为 `格式版本_导出ID_导出时间.json`。文件采用白名单与标准库校验后原子发布，发布时必须仍为所捕获的确认名单；冲突时重新预览 / 重试。既有导出不覆盖，也不修改任务状态或人工决定。JSON 是审核快照，不能据此认定已经投递。

完整 JSON 根字段包含 `schema_version`（字符串 `"1.0"`）、`export_id`、`generated_at`、`source_task`、`summary`，`jobs` / `blocked_jobs` 仅在非空时写入。`summary.selected_count` 等于 `ready_count + blocked_count`，也分别对应两份数组长度（缺省数组按零项处理）。两种导出均递归省略 `null`、空白文本、空数组、空对象和清理后为空的父容器，保留有效的 `0`、`false` 与非空文本；不包含原始模型响应、凭据、Cookie、日志或本机路径。完整字段契约见 [JSON Schema](../schemas/delivery_jobs.v1.schema.json)，仍兼容历史稠密完整文件。安装后可通过 `project_resources.resource_path("delivery_jobs.v1.schema.json")` 获取经过包清单和 SHA-256 校验的同一 Schema。

指定字段导出先校验完整数据，再与当前实际有值字段取交集，保留必要层级；不强制补入版本、ID 或摘要。例如 `--fields jobs.title jobs.company` 输出 `{"jobs":[{"title":"…","company":"…"}]}`。未知字段、仅有父级名（如 `jobs`）和空选择会被拒绝；已知但无值的字段自动略过，若最终没有任何可导出内容则不生成文件。桌面“选择导出字段…”只展示当前 02 数据中有值的选项，不添加不存在或全部为空的字段和空岗位组；至少一条记录有值即可出现，`0` / `false` 也有效。刷新和切换任务同步更新目录，父级复选框批量选择当前可用叶子。下方完整格式读取示例适用于完整 JSON；读取指定信息时按实际选择的字段处理。

每条记录的 `job_id` 是任务内身份，`boss_job_id` 是经真实岗位 ID / BOSS 详情链接核对的目标身份，二者不能互换。`job_key` 在同一任务的历次导出中稳定，可供外部脚本保存进度和去重；跨任务的同一目标可进一步按 `boss_job_id` 去重。地区缺失时省略，不会阻止有有效目标身份的岗位导出。

公司名称输出为 `company`，可通过 `--fields jobs.company` 单独导出。01 新采集独立保存公司名称；旧采集器曾将 API 的 `brandName` 写入 `boss_name`，已知 API 记录可在本地恢复名称，无需重跑采集或 AI。DOM 或未知来源的招聘者姓名不会当作公司名，确实未保存名称时保持缺失。

招聘经验输出为 `experience`，例如 `"1-3年"`、`"经验不限"`、`"在校/应届"`。使用 `--fields jobs.experience` 可生成仅含经验要求的 `经验要求.json`。显式岗位经验字段优先；旧数据只从完整、明确且无冲突的经验标签恢复，不使用检索经验筛选、学历标签或 JD 推算。缺失时省略，不填零年；旧任务读取和导出不会改写检查点或确认名单。

已有公司信息使用原字段导出：`encrypt_brand_id`（公司 ID）、`company_link`（公司详情链接）、`company_industry`（行业）、`company_scale`（规模）、`company_stage`（融资阶段）。完整 JSON 包含其中有值的字段，也可通过 `--fields jobs.encrypt_brand_id jobs.company_link jobs.company_industry jobs.company_scale jobs.company_stage` 单独导出。公司 ID 和公司链接仅作公司信息，不能替代岗位定位。

`navigation` 保留已存在的 `lid`、`ka`、`from`、`security_id`、`encrypt_boss_id` 导航值和带允许查询参数的来源链接 `original_job_url`。BOSS 的 `securityId` 是详情页定位提示，`encrypt_boss_id` 是招聘者导航提示，均不充当岗位 ID；未知查询参数、登录凭据和 Cookie 不透传。机器可读阻止原因位于每个 `blocked_jobs` 记录的 `blocked_reasons`。

下面的 Python 示例只读取可导出岗位，不发送任何内容：

```python
import json
from pathlib import Path

data = json.loads(Path("格式版本_导出ID_导出时间.json").read_text(encoding="utf-8"))
if data["schema_version"] != "1.0":
    raise ValueError("不支持的导出版本")
for job in data.get("jobs", []):
    print(json.dumps(job, ensure_ascii=False))
print("待修正：", len(data.get("blocked_jobs", [])))
```

下游只消费 `jobs`，不得把 `blocked_jobs` 自动当可投岗位。修正任务取舍或证据后，应重新完成受影响审核并确认，生成新快照；历史文件保留原内容。

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
| `--page-wait-min SECONDS`、`--page-wait-max SECONDS` | 列表翻页等待，默认 12 / 22 秒，有限小数，`0 ≤ min ≤ max ≤ 300`；相同值为固定等待 |
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

CLI 的登录准备仍会初始化所指定根目录与清单；若不想触碰既有采集源，使用独立准备目录或 GUI“准备浏览器 / 登录”（使用临时根）。Chrome 与 Edge 使用各自端口和 profile；桌面同一浏览器的并发槽复用该侧端口，各自创建独立页面。翻页等待由项目内适配器应用到上游列表采集，兼容性检查失败会明确报错，不静默沿用旧等待值。

### PowerShell 包装与双账号

`run_collect.ps1` / `run_collect.bat` 提供 `-Batch`、`-Pages`、`-PageWaitMin`、`-PageWaitMax`、`-Root`、`-ScraperRoot`、`-Browser`、`-CdpPort`、`-TotalShards`、`-Shard`、`-ProfileDir`、`-SetupBrowser`、`-SetupOnly`、`-DryRun`、`-ContinueOnError`、`-NoWaitOnGate` 和日志开关。`-SetupChrome` 是旧别名，`-SetupEdge` 选择 Edge 并准备登录。双终端启动器也接受 `-PageWaitMin` / `-PageWaitMax`。

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

正常模式有意打开两个可见交互终端：Chrome 端口 9222 / 分片 0/2，Edge 端口 9223 / 分片 1/2。默认全量 672 组时各 336 组，使用同一固定结果根；不传根则创建当天默认根。`-SetupBrowsers` 在采集前准备两个浏览器，`-SetupOnly` 只准备；`-ChromeProfileDir`、`-EdgeProfileDir` 可分别覆盖 profile。中断后重复原命令续跑；只需人工处理受影响的分片。

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
| `--detail-speed-profile fast\|safe` | `fast`，控制页面加载 / 滚动等待 |
| `--jd-scroll-count N` | JD 每页滚动次数，默认 3，范围 1–20；已有完整 JD 不因修改次数而自动重抓 |
| `--jd-page-wait-min N` / `--jd-page-wait-max N` | 每个 JD 页面打开前额外随机等待的上下限，均为 0–30 秒且下限不大于上限；默认 0 / 0，相同值表示固定等待，支持暂停和停止 |
| `--jd-page-wait-min SECONDS`、`--jd-page-wait-max SECONDS` | JD 每页打开前的额外随机等待；整数，`0 ≤ min ≤ max ≤ 30`，默认 0 / 0；相同值为固定等待，已完整 JD 不再等待 |
| `--scraper-root PATH`、`--python PATH` | JD 上游目录及其解释器；目录也可用 `BOSS_SCRAPER_ROOT` |
| `--timeout N`、`--max-retries N` | 请求超时默认 120 秒，有限重试默认 3 |
| `--codex-home PATH` | 显式指定后改用该目录的 Codex `config.toml` / `auth.json`，不读取项目 URL 或 Credential Manager 密钥；不指定时 AI 使用本项目模型配置 |
| `--base-url`、`--api-key`、`--model`、`--reasoning-effort` | 仅覆盖本次请求；默认读取项目本机配置（初始模型 `gpt-5.6-luna`、强度 `max`），URL 从项目配置读取，API Key 从 Windows Credential Manager 读取 |
| `--api-mode responses\|chat_completions` | 仅覆盖本次 API 协议；不传时沿用项目 / 显式 Codex 配置，旧配置缺省为 `responses`。Chat 模式使用 `/chat/completions`，强度仅在匹配已保存验证时发送 |
| `--prompt-file PATH` | 可选 UTF-8 完整提示词快照，仅供 AI / 报告步骤使用；同一轮请求和报告应使用同一文件。不传时沿用项目默认提示词；GUI 自动传入本任务保存的快照 |
| `--wait-on-gate` / `--no-wait-on-gate` | 默认交互等待；后者保存 paused 并返回 |

CLI 默认输出规则：同时给城市和数量时是 `<源根>/test-runs/<城市ID>-<数量>/`，例如 `shanghai-50`；否则直接用源根，可能更新原有 `prepared`。`--output-root` 优先于所有默认规则，不要对旧报告试运行。输出常见文件包括 `raw_sample.json`、`prepared/merged_jobs_index.json`、`prepared/jd_shortlists/details.json`、`prepared/llm_reviews.json`、`prepared/hai_tou_pool.json` 和 `prepared/海投分层清单.md`。

列表初筛为每岗保存互斥的 `list_queue`（`relevant` 明确相关、`verify` 待核实、`excluded` 剔除）及原因；`list_pass=true` 包含前两类，不设最低评分。`prepared/jd_shortlists/jd_input.json` 保留两类合并建议，同时输出 `jd_relevant.json`、`jd_verify.json`，并在元数据 / manifest 保存三类 `list_queue_counts`（含零值）。这些是方向队列，不是最终投递分层；CLI 默认沿用合并名单补 JD，需人工逐步选择和确认时使用桌面审核任务。

报告与桌面共用 `report_decision / report_pool_eligible`：有效已确认的 AI 人工保留 / 剔除优先归入海投池 / 暂不投，前置已确认剔除仍优先。没有有效人工决定的 AI 信息不足、失败和未运行项归待核实，不写入 `hai_tou_pool.json`。全量 JSON 和 Markdown 保留原始判断、技术状态、来源及人工审计信息，数量按相同分类统计。普通 CLI 没有人工确认记录时仍按 AI 默认三类处理。

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

模型初始默认使用 `gpt-5.6-luna` / `max` 和 Responses API；AI 页面保存的项目配置也会由 CLI 默认读取。项目 URL 读取本机项目配置，API Key 读取 Windows Credential Manager；两者均不写入项目结果或全局 Codex 配置。CLI 显式 `--base-url`、`--api-key`、`--model`、`--reasoning-effort`、`--api-mode` 只覆盖本次请求，不保存配置。GUI 的“使用流式响应”对两种协议均有效；兼容沿用本机 `chat_stream` 配置键，CLI 同样读取，缺省为非流式。Chat 默认不发送强度；可在 GUI 选择流式选项、验证强度并保存，CLI 读取同一份验证记录。覆盖后的地址、模型、强度、流式选项必须匹配验证记录，否则不发送参数；`--reasoning-effort` 本身不能绕过验证。更换有效请求配置后显式重跑会使不匹配的缓存失效；Chat 仅实际发送的强度参与缓存签名。默认 Responses 沿用原签名，仅切换流式不使成功缓存失效，历史报告不会自动改写。GUI 的协议选择、读取模型和极短连接测试见[工作流契约](desktop-review-workflow.md)。

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
python delivery_export.py --help
python rescreen_saved_run.py --help
python collect_architecture.py --dry-run --cities 上海 --limit 2
.\run_collect_dual.ps1 -DryRun
```

这些帮助 / dry-run 不执行真实采集或模型请求；后处理没有 dry-run 参数。测试命令与验证边界统一见[开发文档](development.md)。
