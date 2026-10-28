"""Shared, local screening rules and the text shown in the review workbench.

Keep this module independent of processors, task storage and UI modules so
all three can import the same policy without a circular dependency.
"""

from __future__ import annotations

import re
from typing import Any


AI_DEFAULT_WORKERS = 10
AI_DEFAULT_MODEL = "gpt-5.6-luna"
AI_DEFAULT_REASONING_EFFORT = "max"
JD_DEFAULT_WORKERS = 3
JD_SCROLL_COUNT = 3
MAX_WORKERS = 32
SALARY_TARGET_K = 17
SALARY_CEILING_REASON = f"薪资上限低于{SALARY_TARGET_K}K目标"
LIST_QUEUE_LABELS = {"relevant": "明确相关", "verify": "待核实", "excluded": "剔除"}

# A JD-fetch result has no semantic judgement. Remove these fields from old
# cached inputs as well as new output when constructing that stage's result.
SEMANTIC_RESULT_FIELDS = (
    "classification", "rule_classification", "reason", "reasons",
    "jd_dimensions", "jd_dimension_names", "jd_dimension_count", "jd_evidence",
    "jd_minimum_years", "jd_experience_evidence", "jd_exclusion_evidence",
    "model_evidence", "ability_gaps", "gaps", "verification", "verify",
    "llm_review", "model_status", "model_error", "confidence",
)

# The AI step projects the model's decision, not the report classifier.  Its
# explicit marker also lets task merges replace stale/empty derived fields
# without changing historical tasks merely when they are opened.
AI_STAGE_PROJECTION = "model_relevance_v1"
AI_STAGE_RESULT_FIELDS = (*SEMANTIC_RESULT_FIELDS, "target_relevance", "dimensions", "exclusions",
                          "experience_assessment", "risk_flags", "hard_rule_reasons",
                          "model_cache_validation", "model_cache_input_verified",
                          "llm_stage_projection", "llm_stage_decision",
                          "ai_judgment", "ai_reason", "analysis_pending", "review_format",
                          "summary", "evidence_quality", "verification_items", "needs_human_review")

_LIST_FIELDS = ("title", "skills", "skill_tags", "job_labels")
_PURE_DEVELOPMENT_REASON = (
    "主业为纯开发/研发编码，未见方案、架构、产品、业务集成或实施交付职责证据"
)
_LEFT = r"(?<![A-Za-z0-9_])"
_RIGHT = r"(?![A-Za-z0-9_])"
_LANGUAGE = (
    r"(?:JavaScript|TypeScript|Java|Python|Golang|Go|C\+\+|C\#|C|PHP|Ruby|"
    r"Rust|Kotlin|Scala|Swift|Dart|\.NET)"
)
_DEV_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    # Explicit software work objects, rather than any occurrence of 开发:
    # 开发新客户 and 市场开发 are not evidence of writing software.
    r"(?:软件|程序|应用(?:层软件)?|系统|平台|框架|数据|数据库|游戏|嵌入式|"
    r"大模型(?:应用)?|人工智能(?:应用)?|智能体|工作流|算法|前端|后端|后台|服务端|客户端|移动端)"
    r"(?:开发|研发|编程|编码)(?:工程师|人员|岗|实习生)?",
    r"(?:软件|编程|编码)(?:工程师|程序员)",
    r"(?:前端|后端|后台|服务端|客户端|移动端)(?:软件)?(?:工程师|程序员)",
    r"全栈(?:开发|研发|工程师|程序员)?",
    _LEFT + _LANGUAGE + _RIGHT + r"\s*(?:软件)?(?:开发|研发|工程师|程序员)",
    # In a title, a programming language alone is a conventional coding-role
    # label. In skills it remains only a tool, never a recall/exclusion signal.
    r"^\s*(?:(?:高级|中级|初级|资深|senior|junior)\s*)?" + _LANGUAGE
    + r"\s*(?:[（(](?:后端|开发|研发|backend)[）)])?\s*$",
    _LEFT + r"(?:AI|LLM|AIGC|Agent(?:ic)?|RAG|Workflow|SDK|Framework|Web|iOS|Android|App|H5)"
    + _RIGHT + r"\s*(?:应用|软件|框架|技术)?(?:开发|研发)(?:工程师)?",
    # A generic development title must form a role, not a suffix inside an
    # unrelated Chinese title such as 客户开发工程师.
    r"(?:^|[\s/|（(【\-–—])(?:纯|初级|中级|高级|资深|高级资深)?"
    r"(?:开发|研发|编码|编程)(?:工程师|人员|岗位|岗|实习生)"
    r"(?=$|[\s/|）)】\-–—])",
    _LEFT + r"(?:software|application|back[ -]?end|front[ -]?end|full[ -]?stack|"
    r"web|mobile|SDK|framework)\s+(?:development\s+)?(?:engineer|developer|programmer)"
    + _RIGHT,
    _LEFT + _LANGUAGE + _RIGHT + r"\s+(?:engineer|developer|programmer)" + _RIGHT,
    _LEFT + r"(?:AI|LLM|AIGC|Agent(?:ic)?|RAG)\s+(?:application\s+)?"
    r"(?:developer|development\s+engineer|programmer)" + _RIGHT,
    r"^\s*(?:(?:senior|junior|lead|staff|principal)\s+)?(?:developer|programmer)\s*$",
    r"程序员",
))

_MIXED_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r"解决方案|客户技术方案|技术方案|方案设计|方案架构|方案咨询|售前|技术咨询|"
    r"技术顾问|业务分析|需求分析|需求调研|需求洞察|业务场景设计|流程设计",
    # Framework/SDK, MVC/MVVM, Spring and other framework names alone do not
    # establish responsibility for the architecture of a whole system.
    r"(?:总体|整体|系统|应用|软件|业务|企业|技术|解决方案)架构|"
    r"架构(?:师|设计|规划|治理|评审|咨询|演进|决策)",
    r"产品(?:经理|负责人|策划|规划|管理|设计|定义|孵化|运营|需求|原型)|需求管理",
    # CI/CD, IDEs and integration tests are ordinary development practices,
    # not sufficient evidence of business/system integration or delivery.
    r"(?:系统|业务|企业|客户|应用|接口|API|数据|服务)集成|"
    r"(?:系统|业务|客户|应用|接口|数据)对接|集成(?:方案|实施|交付)",
    r"(?<!持续)(?:交付|实施)(?!例)|私有化部署|客户部署|应用部署|前沿部署|前线部署",
    _LEFT + r"(?:PoC|FDE)" + _RIGHT,
    _LEFT + r"(?:solution(?:s)?\s+(?:design|engineer|architect|consultant)|"
    r"pre[ -]?sales|technical\s+consultant|business\s+analys(?:is|t)|"
    r"requirements?\s+analys(?:is|t))" + _RIGHT,
    _LEFT + r"(?:(?:system|software|application|enterprise|business|solution(?:s)?)\s+"
    r"architect(?:ure)?|architectur(?:e|al)\s+(?:design|planning|governance))" + _RIGHT,
    _LEFT + r"product\s+(?:manager|owner|management|design(?:er)?|planning|strategy)" + _RIGHT,
    _LEFT + r"(?:(?:system|business|enterprise|customer|application|API|data|service)\s+"
    r"integration|integration\s+(?:engineer|consultant|architect)|"
    r"(?:customer|project|solution|software|application)\s+delivery|"
    r"implementation\s+(?:engineer|consultant|delivery)|customer\s+deployment)" + _RIGHT,
))
_OTHER_ROLE_TITLES = re.compile(
    r"项目(?:经理|负责人|主管|管理)|顾问|讲师|培训师|教师|销售|招聘|客户成功|"
    r"(?:测试|技术支持)工程师|"
    + _LEFT + r"(?:project\s+manager|program\s+manager|consultant|trainer|teacher|"
    r"advocate|developer\s+relations|sales|technical\s+support)" + _RIGHT,
    re.I,
)

_ARCHITECTURE_CAPABILITY_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r"(?:(?:总体|整体|系统|应用|软件|业务|企业|技术|微服务|分布式|解决方案)架构|架构)"
    r"(?:设计|规划|治理|评审|咨询|演进|决策)?(?:相关|方面的|的)?(?:经验|能力|知识|基础|技能)",
    r"(?:熟悉|了解|掌握|精通)\s*(?:(?:总体|整体|系统|应用|软件|业务|企业|技术|微服务|分布式)架构|架构设计)",
    _LEFT + r"(?:experience|knowledge|familiarity|proficiency)\s+(?:in|of|with)\s+"
    r"(?:(?:system|software|application|enterprise|business|solution(?:s)?)\s+)?"
    r"architectur(?:e|al)(?:\s+design)?" + _RIGHT,
    _LEFT + r"(?:(?:system|software|application|enterprise|business|solution(?:s)?)\s+)?"
    r"architectur(?:e|al)(?:\s+design)?\s+(?:experience|knowledge|skills?)" + _RIGHT,
))


def mixed_duty_evidence(fields: dict[str, str]) -> list[dict[str, str]]:
    """Find positive mixed-work clues, not architecture-experience labels.

    The caller removes negated and other-team clauses first. Matches always
    quote the original field verbatim; masking capability-only phrases avoids
    treating a software developer's architecture skill as a second job duty.
    """

    evidence = []
    for field, value in fields.items():
        if not isinstance(value, str) or not value.strip():
            continue
        positive = value
        for pattern in _ARCHITECTURE_CAPABILITY_PATTERNS:
            positive = pattern.sub(lambda match: " " * len(match.group(0)), positive)
        for pattern in _MIXED_PATTERNS:
            for match in pattern.finditer(positive):
                item = {"field": field, "quote": value[match.start():match.end()]}
                if item not in evidence:
                    evidence.append(item)
    return evidence


def detect_pure_development(fields: dict[str, str]) -> dict[str, Any]:
    """Exclude only clear coding titles without mixed-duty counterevidence.

    ``fields`` contains list-title/skill text with negated or other-team
    responsibility clauses already removed by the caller. Skills alone
    cannot turn an ambiguous title into a pure development role, while an
    explicit coding title needs no skills to supply its own evidence.
    """

    values = {key: value for key in _LIST_FIELDS
              if isinstance(value := fields.get(key), str) and value.strip()}
    title = values.get("title", "")
    matches = [match for pattern in _DEV_PATTERNS for match in pattern.finditer(title)]
    if not matches or _OTHER_ROLE_TITLES.search(title) or mixed_duty_evidence(values):
        return {"excluded": False, "reasons": [], "evidence": []}
    evidence = []
    for match in sorted(matches, key=lambda value: (value.start(), -len(value.group(0)))):
        quote = match.group(0).strip()
        item = {"family": _PURE_DEVELOPMENT_REASON, "field": "title", "quote": quote}
        if item not in evidence:
            evidence.append(item)
    return {"excluded": True, "reasons": [_PURE_DEVELOPMENT_REASON], "evidence": evidence}


_MANUAL_RULES = (
    "人工保留/剔除优先于本步建议，重跑保留人工选择；点击“采用建议”会重置人工选择。"
    "确认只冻结名单并进入下一步，不自动运行；人工确认剔除项不进入最终可投池。"
    "人工保留用于继续核实，不直接改写AI判断；人工确认剔除优先于报告可投池。"
)

_STAGE_RULES = {
    "import": (
        "从绑定的固定数据源导入并去重，按本任务范围保留来源，不自动抓取。\n"
        "默认全部岗位（不限）；自定义数量以可用去重岗位数为上限。\n" + _MANUAL_RULES
    ),
    "shortlist": (
        f"列表按标题与技能宽召回，薪资上限<{SALARY_TARGET_K}K或明确纯开发等岗位剔除；分数仅排序，不设5分门槛。\n"
        "明确相关、待核实、剔除三个队列互斥；默认保留前两类，队列不代表JD职责命中或最终分层。"
        "可仅保留明确相关或加入待核实，人工确认后才进入JD补抓，不自动执行。\n"
        "命中AI应用、方案/架构、产品/产品助理、Agent/RAG/Workflow、系统集成/交付、"
        "AI落地赋能、知识管理及相关过渡方向即可召回。"
        "强方向标题缺技能仍召回并标核实；通用架构/泛项目、算法/基础设施或行业对象不明进入待核实。"
        "排除纯开发、纯销售、明确纯算法/底模、纯底层基础设施及明显无关岗位。\n"
        "纯开发须有明确开发/研发编码标题，且无方案设计、总体架构、产品职责、业务/系统集成、"
        "实施交付或PoC等反向证据；仅AI/Agent/RAG/Workflow、编程语言、开发框架或架构设计经验能力标签不能豁免。"
        "方案/产品岗位会编程不因此剔除，混合方案、集成与交付岗位保留；信息不足不猜测纯开发。\n"
        "AI/Agent/解决方案销售名词不等于本人技术职责；明确获客且无技术工作反证剔除，"
        "开发新客户/客户开发不是技术开发；技术售前/FDE不一票排除，否定或他人承担的职责不作反证。\n"
        f"明确月薪上限<{SALARY_TARGET_K}K直接剔除；上限={SALARY_TARGET_K}K不因薪资剔除。"
        "跨17K、面议、缺失或无法确定月薪上限的保留待核实；不把年终奖折算成固定月薪。"
        "经验偏高只标注；实际工作内容留到完整JD后由AI核实。\n" + _MANUAL_RULES
    ),
    "fetch-jd": (
        "仅补充JD正文与完整度，不作职责关键词筛选，也不自动剔除本步输入岗位。\n"
        f"页面并发默认{JD_DEFAULT_WORKERS}，范围1–{MAX_WORKERS}，可设N页；"
        "实际同时页数不超过页面并发与当前批次待抓数的较小值，批大小仅用于检查点分组。"
        f"每页固定模拟滚动{JD_SCROLL_COUNT}次，复用专用Chrome登录态并使用独立页面。\n"
        "记录完整/部分/缺失正文、失败与验证状态；遇登录或安全验证暂停调度并保存检查点，"
        "人工处理后继续本步。只有完整JD进入AI，部分与缺失JD进入待核实，不进入保留或剔除名单。\n" + _MANUAL_RULES
    ),
    "llm": (
        "AI接收完整职位职责与任职要求，按实际工作内容复核；可直接编辑本任务规则。\n"
        "目标为企业AI解决方案架构师/AI应用架构师、AI产品经理（应用/Agent/知识库），"
        "以及能积累业务需求分析、方案设计、系统集成、PoC、交付与迭代经验的相关岗位；不强求同名。"
        "排除主业代码开发、算法研究、模型训练、硬件、纯销售/渠道招商及底层基础设施。"
        "仅需懂技术或配合开发的产品、方案岗位不可误判为纯开发。\n"
        "完整JD逐岗位独立请求，发送前不按通用关键词或固定词表删职责句；"
        "不接收职位名称、技能标签、公司介绍、薪资或检索关键词。"
        "模型严格只返回“判断”（符合/不符合/信息不足）和“理由”（一两句话），"
        "不输出岗位ID、正文依据、六维、置信度或能力差距；运行元数据由本地程序追加。"
        "可点击“修改复核规则”编辑并保存本任务的AI判断规则；恢复默认后仍须保存。"
        "打开旧任务不静默替换其自定义规则，旧结果只读追溯，不能冒充新版成功。"
        "保存不执行复核；规则改变后须手动重跑AI与报告，已抓JD保留。\n"
        "符合建议保留，不符合建议剔除。"
        "信息不足、缺失/部分JD、配置不可用、请求失败或缓存无效均不进入保留或剔除名单，待人工核实；不走代码语义兜底剔除。"
        "不由代码关键词、证据门槛或六维推翻AI判断。"
        "新版报告按同一判断：符合→海投池，不符合→暂不投，信息不足/失败/缺失→独立待核实；人工确认剔除优先。"
        f"AI请求并发默认{AI_DEFAULT_WORKERS}（1–{MAX_WORKERS}），独立于JD页面并发；"
        f"模型{AI_DEFAULT_MODEL}，思考强度{AI_DEFAULT_REASONING_EFFORT}；"
        "服务地址与密钥使用本项目的本机私密配置，不修改全局Codex设置。\n" + _MANUAL_RULES
    ),
    "report": (
        "报告追踪全部导入岗位；新版AI符合→海投池，不符合→暂不投，信息不足/失败/缺失→独立待核实（不计入可投池）。\n"
        "不再用关键词、证据门槛或六维推翻新版AI判断。人工确认剔除优先，不进入可投池；人工保留不直接改写AI判断。"
        "报告只读，修改取舍须回到前面审核步骤重新确认并生成报告；不执行投递。"
    ),
}


def get_stage_rules(stage: str) -> str:
    """Return display-ready rules; the first line is a standalone summary."""

    return _STAGE_RULES.get(stage, "请选择导入、列表初筛、JD补抓、AI语义复核或生成报告阶段查看当前规则。")
