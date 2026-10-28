# 开发与维护

本文说明代码边界、验证方法和当前技术改进项。日常操作见 [README](../README.md)，业务与审核状态约定见 [工作流契约](desktop-review-workflow.md)，高级参数见 [命令行参考](command-line.md)。文档保留当前约定，不累积每轮调试日志和重复测试次数。

## 代码地图

| 职责 | 主要文件 | 边界 |
| --- | --- | --- |
| 桌面入口 | `desktop_app.py`、`run_desktop.*` | 默认打开新双页面；旧工作台仅通过 `--legacy-workbench` 启用 |
| 独立采集页面 | `desktop_collection.py` | 编辑与预览方案、显式开始、人工验证与进度；不自动执行后处理 |
| 采集与方案 | `collect_architecture.py`、`collection_plan.py`、`collection_sources.py` | 组合展开、原始采集续跑、不可变方案及历史来源映射 |
| 手动审核界面 | `desktop_review_ui.py` | 运行与确认分开，后台执行，界面从持久化结果展示统计 |
| 审核存储与执行 | `desktop_tasks.py`、`review_pipeline.py` | 固定源、五步状态机、版本化检查点、输入集合和进程所有权 |
| 处理与规则 | `process_architecture.py`、`screening_policy.py` | 去重、初筛、证据、薪资和经验硬规则、最终分层与报告 |
| 模型缓存 | `review_cache.py` | CLI / 桌面共用签名，区分当前有效记录与历史结果；报告只做离线输入校验 |
| 任务复核规则 | `review_rules.py` | 分离可编辑语义规则与固定输出协议，为任务请求、缓存和报告提供一致提示词 |
| 外部执行适配 | `browser_setup.py`、`jd_parallel.py`、`llm_client.py` | 专用浏览器启动、有限并发、检查点、请求失败和人工验证；不投递、不绕过验证 |
| 只读展示与兼容 | `desktop_data.py`、`desktop_theme.py`、`desktop_preferences.py`、`desktop_ui.py`、`desktop_routing.py` | 共享数据、主题与白名单偏好；旧工作台保留偏好接口兼容 |
| Tk 生命周期 | `desktop_tk_lifecycle.py` | 在创建窗口的线程释放变量、图标及已销毁控件的 Tcl 引用，避免后台垃圾回收跨线程释放解释器 |
| 资源与发布 | `project_resources.py`、`boss_architecture_resources/`、`pyproject.toml` | 源码资源与已安装只读资源定位、构建精确白名单、默认可写状态目录 |
| 离线历史回测 | `rescreen_saved_run.py` | 复用已保存岗位/JD/模型记录，不请求服务，不覆盖原运行 |

根目录的 Python 文件采用平铺模块布局。不要只因模块名包含旧界面或兼容逻辑就删除：正常界面仍会复用部分公共偏好、主题和数据适配器。后续拆分需要同步更新启动器、导入和测试。

## 本地验证

推荐 Python 3.11+，桌面测试需要 Tcl/Tk。Python 3.10 的模型配置读取需要安装元数据声明的条件依赖 `tomli`；3.11+ 使用内置 `tomllib`。源码及 wheel 均可启动，不需要额外 GUI 或测试框架依赖。

```powershell
python -B -m unittest discover -s tests -v
python -B collect_architecture.py --dry-run
python -B process_architecture.py --help
```

- `-B` 只避免当前 Python 进程生成字节码；验证子进程时可在当前终端设置 `$env:PYTHONDONTWRITEBYTECODE = '1'`，不修改全局环境。
- 自动测试使用临时目录、模拟服务或无害本机子进程。不要用真实采集、登录或模型请求替代单元测试。
- Windows 文件锁、Job Object、路径联接、暂停/停止及 Tk 后台线程属于回归重点；缺少符号链接权限的测试可能跳过，应报告原因。
- 修改规则时验证人工确认、旧缓存失效、全部来源、薪资边界与 JSON/Markdown 数量一致；列表队列需验证三类互斥、合并与分列文件一致、缺字段的历史结果兼容，以及人工选队列后实际确认输入。修改并发或进程代码时验证取消和所有权。
- 离线测试通过不代表真实 BOSS 登录、网站并发稳定性、模型授权或桌面截图视觉效果已经验收。
- JD 自动准备与审核界面“准备浏览器”共用 `browser_setup.py`，直接启动本机 Chrome / Edge 并打开 BOSS 窗口，不将本项目的 `--profile-dir` / `--setup-browser` 参数传给上游。准备按钮只确认窗口启动命令与 CDP 连接，不表示 BOSS 已登录；自动补抓仅用上游支持的 `--check --cdp-port` 检查登录，失败后显示窗口并暂停，保留 JD 检查点，不循环探测验证页。

## 可以清理与必须保留的内容

- 可重新生成：`__pycache__/`、`.pytest_cache/`、构建目录和覆盖率输出。这些由 `.gitignore` 排除。
- 必须保留：采集根的 `raw/`、manifest、续跑检查点、原始日志、具名审核任务、版本化阶段文件、已保存 JD/模型记录和报告。
- `config/collection-plans/`、`config/review-source-catalogs/` 虽然不提交 Git，但可能被任务按路径及摘要固定引用，不是普通缓存。
- 浏览器 profile、环境文件及本机 Codex 配置/凭据不属于源码清理对象。不要把它们放入开源包或诊断日志。
- 删除或移动前先确认真实引用与路径边界；历史版本管理尚未实现自动回收，不手工按“看起来旧”删除检查点。

## 当前优化契约

- 模型缓存：CLI 与桌面使用同一签名规则，覆盖完整模型输入、提示词、模型、服务地址、API 协议与实际生效的思考强度；缺少签名或签名不匹配的成功记录不得作为本次成功复用，失败记录仍可重试。签名及诊断不保存密钥或原始服务地址，不修改历史报告。
- 发布：保留源码启动及现有平铺导入兼容，显式声明可安装模块和只读配置/提示词资源；在项目外的临时目录验证 wheel/sdist 及已安装入口，不将岗位数据、本机配置和凭据打包。
- 桌面公共层：小范围提取仍被新旧界面共同依赖的偏好保存等基础逻辑，使用唯一临时文件避免竞争；不搬迁整个项目或删除仍有引用的兼容代码。
- 刷新性能：未变化的任务阶段复用已读取岗位与搜索索引，变更时可靠失效；日志持久化不阻塞 Tk 主线程，并在关闭时处理尚未写完的记录。性能测试使用确定性读次数/索引构建次数，不依赖机器时间阈值。
- 验收边界：保留人工确认、阶段 stale、只读数据源和列表初筛独立规则；新版 AI / 报告不得再被代码关键词、职责证据或六维改判。全部验证使用临时数据与模拟服务，不运行真实采集、模型或读取个人凭据。

### 模型缓存与离线报告

AI 默认逐岗独立请求：`run_llm`、CLI 与 PowerShell 的 `conversation_size` 均为 1，桌面显式传入 1。worker 不向客户端传递跨岗位会话状态，Responses 默认 `store=false`，Chat 仅提交当前岗位消息；主线程逐岗写检查点。显式多岗参数仍保留兼容实现与测试，但不作为默认工作流。相关测试核对两个协议的实际请求体、默认参数、桌面子命令、成功缓存续跑和并发控制；不改变模型、强度、服务商或列表初筛的独立规则。

缓存签名仍表示当前岗位的逻辑输入及配置；恢复独立请求不强制重跑已有有效成功结果。旧会话计数字段只描述当前执行，不能当服务账单。单元测试不访问真实服务；用户授权的真实小样本验证单独放在 job-result/validation-runs，不改原审核任务，并记录实际配置、失败、格式纠正及归一化数量。

`review_cache.py` 是签名和当前记录选择的唯一入口。每条新复核记录保存完整请求签名与不含提供方的输入签名，不能用更新过的旁路 context 文件为旧结果补证明。完整签名覆盖实际消息模板、岗位输入、提示词、响应 schema、模型、服务地址摘要、API 协议和实际生效的思考强度；API 密钥不进入签名。Responses 默认保留旧指纹格式，避免仅因版本升级重复请求全部成功记录；Chat 加入独立协议标识，未验证强度按 `None`，已验证且实际发送时按真实值签名。相同成功记录可续跑，失败或缺少签名的历史记录重新请求，失效记录保留在 `previous_reviews`，不计当前成功数。配置不可用时 AI 步骤默认保留待核实，不以旧成功伪装新复核。

当前模型协议为 `simple-v1`：模型只返回 `判断`（符合 / 不符合 / 信息不足）、`理由`（一两句话）；本地追加岗位 ID、状态、模型、签名与格式标记。桌面 `review_pipeline._analyze_ai_stage` 仅接受格式标记、两字段结构、当前岗位身份和成功状态一致的记录。符合建议保留、不符合建议剔除，其余默认进入独立待核实集合；有效结果中的全部岗位均可逐条或批量人工保留 / 剔除，人工决定优先，加载、刷新与续跑保留 `manual_choices`，“采用建议”清除人工决定并恢复默认三类。本地 `ai_judgment / ai_reason / analysis_pending` 是 UI 派生字段，不属于模型输出，人工选择不改写原始 AI 证据。AI 与新版报告不调用关键词 / 引用 / 六维分类来推翻结论；报告优先采用当前有效且已确认的人工决定，分别映射海投池 / 暂不投，没有人工决定时按 AI 默认三类映射海投池 / 暂不投 / 待核实。前置阶段已确认剔除仍优先；未确认、失效或不在当前输入和结果中的人工选择不能授权入池。

沿用 `AI_STAGE_PROJECTION="model_relevance_v1"` 的显式执行投影，`AI_STAGE_RESULT_FIELDS` 同时清理新旧派生字段，合并时不能复活旧分层、证据、置信度或能力差距。打开旧任务不迁移结果、不覆写自定义规则；旧 rich 显示只读历史，不能冒充新版成功。规则编辑器“恢复默认”仅改草稿，“保存本任务规则”才应用；“重置本阶段”只清除该阶段数据并待用户另行运行。只有重置后的 AI 阶段真正开始时才识别并迁移旧复杂规则到简化默认。随后用户手动运行 AI、确认名单、生成报告；已抓 JD、来源、上游结果和隔离目录边界不变。`llm_success_count` 只统计两字段有效的 `simple-v1` 成功；兼容字段 `llm_fallback_count` 在 UI / 日志显示“未完成AI判断”；人工选择不改变这些技术计数，`llm_verification_count` 则随尚未人工处理的待核实名单更新。

语义工作负载的初始默认模型与强度统一定义在 `screening_policy.py`：`gpt-5.6-luna` / `max`。项目 AI 使用 `project_llm_settings.py` 的本机项目配置，默认 URL 为 `https://nikoapi.xyz/v1`；AI 页面允许编辑服务地址、API 协议、模型、强度和 API Key，Windows API Key 仅存 Credential Manager。`api_mode` 使用稳定值 `responses` / `chat_completions`，缺省为 `responses`，旧配置无需迁移。`CodexConfig`、客户端 Mapping、项目设置和显式 CLI 覆盖传递同一模式；`load_codex_config(api_mode=...)` 及客户端显式参数优先于保存值。`llm_client.load_codex_config()` 只有显式传入 `project_settings_path` 时才读取该项目配置，避免改变通用 Codex 客户端语义。项目配置和密钥不写入岗位结果、日志或发行包。

`CodexResponsesClient` 保留公共名称，同时支持两种协议。本机 `chat_stream` 布尔键兼容保留，现对两种协议共用；缺省仍为非流式。Responses 保留原 `input`、`reasoning`、`store` 请求，开启流式时追加 `stream=true`，完整复核、连接测试与格式纠正使用同一公共请求入口；Chat 发送 `model`、`messages`、`stream=chat_stream`，只有 `chat_reasoning_verified_for` 与地址、模型、强度及流式选项的 SHA-256 相符时才发送顶层 `reasoning_effort`。`test_chat_reasoning_effort()` 不依赖既有证明，单次发送候选参数，严格要求完成且最终文本非空；不自行保存记录。GUI 显式验证并保存才启用，普通连接和模型发现不生成证明。未验证 Chat 普通连接测试用 `max_tokens=16`，强度验证和已验证 Chat 测试用 `1024`。两种模式共用 `/models`，Base URL 的端点后缀会先归一化。

Responses SSE 以 `response.completed` 内的 `status=completed` 对象为完成依据，之后继续走原输出提取、JSON 和 schema 校验；不要求 Chat 的 `[DONE]`，不将 delta 或正文结束事件补成成功。Chat 保留完成片段及 `[DONE]` 校验。错误、空包、仅思考、截断、过滤、工具调用或中断均不算成功；不回显提供方原文或推理正文。流式是传输选项，不改变 Responses 语义缓存身份；旧配置缺少证明或流式字段时按未验证、非流式处理。相关离线契约由 `tests/test_llm_client.py`、`tests/test_project_llm_settings.py`、`tests/test_review_cache.py` 及 GUI/流水线集成测试验证，不读取真实密钥或访问模型服务。

2026-10-28 经用户授权，以本机项目 Responses 流式配置进行了小样本真实验证。针对实际网关增加定向兼容：`response.metadata` 仅作为附加事件，不能代替完成；最终包包含多个 message 时优先 `phase=final_answer`，排除 reasoning/commentary，并以结构化 output 优先于网关拼接的 output_text。过滤前仍检查全部输出的错误、拒绝及工具调用。连接测试必须取得非空实际文本；界面模型请求等待上限为 120 秒，模型列表查询仍为 25 秒，均在后台且不自动重试。沿用的 `chat_stream`、`review_chat_stream` 和 `requested_chat_stream` 诊断字段现在对两种协议均记录实际设置。真实测试只使用自造岗位，验证文件放在 Git 忽略的独立目录，不接触现有任务；这不替代长期稳定性和真实断流测试。

默认 HTTP transport 使用独立 opener，在处理 Location 前拒绝所有 API 3xx，包括同域、跨域及 HTTPS 降级；正式复核、模型发现、连接测试和强度验证共用此边界。错误只含固定提示与状态，不重试、不读取重定向正文；服务地址须配置最终端点。自定义 transport 返回的 3xx 同样拒绝，但其内部是否自行跟随重定向由注入方负责。

报告仅校验已有记录与当前输入，不读取配置、凭据或请求模型。新版签名无效、失败或缺失默认归为待核实；当前有效且已确认的人工决定可改变报告分类和可投池，原始 AI 判断、技术状态与问题仍保留，不能触发旧关键词 / 证据分层。旧 rich 只按兼容路径追溯。提供方是否改变需通过显式重跑 AI 验证，不在报告步猜测，历史已生成文件不自动改写。新默认 schema 与完整请求签名使旧 rich 缓存失效并保留 `previous_reviews`，不以旧 `status=success` 计当前成功。

### 安装产物

`pyproject.toml` 显式列出平铺模块，`BuildPy` 从 `config/architecture_collection.json` 与 `prompts/jd_semantic_review.md` 精确生成包资源和 SHA-256 清单；不存在两份手工维护的默认配置。wheel 不带构建钩子、源码启动器、岗位数据、个人方案或凭据；sdist 以白名单纳入源码、测试、四份文档和两处资源源文件，可离线再构建 wheel。安装态发现资源缺失、篡改或版本错配时明确失败，不从工作目录猜测回退。

源码态 `default_state_root()` 返回项目目录。安装态 Windows 使用 `%LOCALAPPDATA%/boss-architecture-collector`，缺失时回退 `~/.boss-architecture-collector`；其他平台使用 `$XDG_STATE_HOME/boss-architecture-collector` 或 `~/.local/state/boss-architecture-collector`。该函数只计算路径，不创建目录。原有桌面偏好位置保持兼容，不迁移用户数据。

`tests/test_packaging.py` 在临时源码副本中实际构建 wheel / sdist、从 sdist 再构建、在独立 venv 安装，并从源码目录外检查三个入口、模块和资源。离线测试需要已安装的 `setuptools>=70.1`、pip 与 ensurepip，不会联网安装构建工具。当前实际验证环境为 Windows Python 3.11；Python 3.10 条件依赖有元数据测试，尚未以该解释器实测。

### 刷新、日志与偏好

`TaskViewCache` 仅保留当前任务和仍被引用的阶段文件版本；缓存文件内容及派生搜索索引，不缓存或绕过路径、ID 与人工确认校验。每次仍读取有效 manifest，并检查文件身份、大小和变更时间；Windows 使用系统变更时间检测同大小且恢复 mtime 的外部改写。人工选择变更重建报告审计，未变化的正文可复用。关闭 / 切换任务清理引用，不积累历史 revision。

确定性 fixture 中，1000 个岗位首次捕获读取 1 个检查点、构建 3000 条跨视图搜索文本，随后三次无变化刷新新增检查点读取与搜索构建均为 0；每次仍保留一次 manifest 读取。四阶段结果均完成时，冷刷新读取 4 个检查点并构建 5 组搜索索引，热刷新为 0 / 0。这里验证的是工作量减少，不代表所有机器上的耗时比例。

旧工作台 `_OrderedLogWriter` 按队列顺序后台批量写盘；停止请求异步 flush，关闭通过 Tk 定时检查排空，磁盘失败只给固定安全提示。1000 条短日志的单批测试中 open / write / flush 各 1 次。新旧界面共用 `desktop_data` 的递归与文本脱敏，旧日志入口保留非敏感上下文，Cookie 内容和常见凭据统一遮盖。`desktop_preferences.py` 统一白名单和持久化，使用唯一临时文件与原子替换，旧 `desktop_ui` 保留导入兼容。多窗口仍为最后一次完整保存生效，不做字段级自动合并。

窗口关闭还须处理 Python 对象的生命周期：仅调用 `destroy()` 不保证变量和控件循环已释放 Tcl 解释器。新旧界面共用 `desktop_tk_lifecycle.py`，在所属 Tk 线程正常销毁后释放引用；后台线程只处理数据，不操作 Tk。新增应用持有的变量和图片须使用其受管类型，独立页与编辑器用 `destroy_tk_widget()` 销毁。不要通过禁用垃圾回收或跳过跨窗口测试掩盖线程释放错误。

## 本次优化（2026-10-28）

本次工作仅在 `boss-architecture-collector-optimized-20261028` 副本中实施。副本保留原项目的采集数据、历史结果和配置，复制时排除可重建的 Python / pytest 缓存；原项目不修改。副本数据是复制时快照，不会同步原目录后续运行结果。已有任务的固定源路径和签名不自动改写；需要完全独立的新审核时，在副本中选择复制后的采集根新建任务。

- AI 语义复核每份已落盘结果能在运行中更新保留、剔除及待核实名单和数量；不要求用户切换步骤或手动刷新。后台更新不覆盖较新结果或尚未保存的人工操作；未完成或信息不足仍进入独立待核实。
- 无变化刷新不重复读取完整 AI 检查点或重建搜索索引；新结果只触发必要的投影、筛选与控件更新。通过大样本和真实 Tk 事件循环验证持续输入、选择、滚动及更新可响应，并用确定性工作量验证缓存与重复渲染减少。
- 修复本次审查 F1–F10：双侧检查点恢复、CLI 待核实投池、HTTP 重定向凭据边界、执行租约异常收尾、来源字段冲突、CLI 组合校验、双侧预检调度、持久化失败停止、确认快照核对及兼容投影缓存。
- 清理实际无引用的代码和可重建缓存；重复实现通过稳定公共入口合并。保留仍有调用者的兼容入口、发布资源、历史数据、个人配置和任务检查点，不以文件名或“旧版”标签代替引用核对。
- 与现行 simple-v1 / 待核实约定不一致的测试使用有效当前 fixture，旧 rich 兼容另测，保留原有安全和恢复覆盖。Windows CI 在 Python 3.10 / 3.11 / 3.12 上执行离线回归、真实临时 wheel / sdist 安装及启动入口检查；本机实际验证以 Python 3.11 为准，不访问真实 BOSS、模型服务或个人凭据。

报告分类和可投资格共用 `report_decision / report_pool_eligible`；JD 完整度共用 `jd_parallel.detail_completeness`，`process_architecture` 保留导出兼容；文本与递归脱敏统一到 `desktop_data`。没有删除仍被调用的旧工作台、历史模型验证器或显式多岗会话入口。

## 后续改进

1. Windows CI 已配置 Python 3.10 / 3.11 / 3.12 的离线回归、安装产物和入口冒烟；首次远端执行后核对结果。本机实测环境仍为 Python 3.11，不将矩阵配置当成其他解释器已经通过。
2. 继续按稳定边界统一重复的脱敏、原子 JSON 和进程管理；保留旧界面兼容，不把一次清理变成全目录搬迁。
3. 大任务继续测量 manifest 校验、JSON 合并、深复制和内存占用，再决定分页或进一步增量处理，不盲目提高网络并发。
4. 历史数据回收另做显式工具：先显示占用及引用关系、预览可回收项、用户确认后操作，不手工删除固定源或任务引用。

对于会改变目录结构、检查点复用、业务规则或用户操作的优化，先更新工作流契约并单独验收；不要与无行为变化的日常清理混在一起。
