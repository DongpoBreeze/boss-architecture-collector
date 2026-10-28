#!/usr/bin/env python3
"""Post-process BOSS job lists and optionally review JD text with local Codex.

The collector itself only writes raw search results.  This module owns the
read-only transformation after collection: sampling, merging, deduplication,
list screening, detail fetching, concurrent semantic review, and reports.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import queue
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

import browser_setup
from jd_parallel import detail_completeness, detail_quality as _detail_quality
from project_resources import DEFAULT_CONFIG, DEFAULT_PROMPT as PROMPT_PATH, PROJECT_ROOT
import project_llm_settings
from screening_policy import (
    AI_DEFAULT_MODEL, AI_DEFAULT_REASONING_EFFORT, AI_DEFAULT_WORKERS,
    JD_DEFAULT_WORKERS, JD_SCROLL_COUNT, MAX_WORKERS, LIST_QUEUE_LABELS,
    SALARY_TARGET_K, SALARY_CEILING_REASON, detect_pure_development, mixed_duty_evidence,
)


CITY_NAMES = {
    "shanghai": "上海",
    "shenzhen": "深圳",
    "hangzhou": "杭州",
    "guangzhou": "广州",
    "chengdu": "成都",
}

DIMENSIONS = (
    "方案设计",
    "系统边界与集成",
    "Agent/RAG/Workflow",
    "知识与数据",
    "PoC/交付",
    "迭代与评测",
)

DIMENSION_TERMS = {
    "方案设计": ("方案设计", "解决方案", "需求分析", "需求调研", "业务场景", "场景拆解", "流程设计", "规划", "架构设计", "技术咨询", "需求洞察"),
    "系统边界与集成": ("系统集成", "系统对接", "接口", "api", "数据流", "权限", "私有化", "部署拓扑", "身份", "租户", "连接器", "集成"),
    "Agent/RAG/Workflow": ("agent", "智能体", "rag", "检索增强", "工作流", "workflow", "工具调用", "tool calling", "mcp", "多智能体", "编排"),
    "知识与数据": ("知识库", "知识工程", "文档解析", "文档", "数据治理", "数据源", "向量", "embedding", "检索", "切片", "重排", "溯源"),
    "PoC/交付": ("poc", "demo", "交付", "实施", "上线", "验收", "部署", "生产化", "落地", "试点", "上线切换"),
    "迭代与评测": ("迭代", "优化", "评测", "复盘", "反馈", "持续", "效果", "质量闭环", "最佳实践", "回归验证", "监控"),
}

# The semantic stage evaluates duties, not keyword presence in job labels,
# requirements, company introductions, or benefit copy.  These section
# headings are intentionally conservative: when a JD has explicit sections,
# only text in a responsibility section can prove a responsibility.
_DUTY_HEADINGS = (
    "岗位职责", "工作职责", "主要职责", "核心职责", "职位职责", "岗位内容",
    "工作内容", "主要工作", "职位描述", "工作描述", "岗位描述", "工作介绍",
    "岗位介绍", "responsibilities", "what you will do", "what you’ll do",
)
_REQUIREMENT_HEADINGS = (
    "任职要求", "任职资格", "职位要求", "岗位要求", "任职条件", "岗位条件",
    "基本要求", "必备条件", "硬性要求", "技能要求", "专业要求", "技术要求",
    "经验要求", "职位资格", "要求", "加分项", "优先条件", "优先资格",
    "requirements", "qualifications", "preferred qualifications", "benefits",
)
_OTHER_HEADINGS = (
    "我们提供", "福利待遇", "薪酬福利", "公司介绍", "关于我们", "公司简介",
    "团队介绍", "企业文化", "about us", "who we are", "what we offer",
)
_SECTION_HEADING_RE = re.compile(
    r"(?:^|(?<=[\r\n])|(?<=[。；;]))[ \t]*(?:#{1,6}[ \t]*|[•*▪-][ \t]*|\d+[、.)）][ \t]*)?"
    r"(?P<heading>" + "|".join(
        re.escape(value) for value in sorted((*_DUTY_HEADINGS, *_REQUIREMENT_HEADINGS, *_OTHER_HEADINGS),
                                             key=len, reverse=True)
    ) + r")[ \t]*(?:[:：][ \t]*|[ \t]+|(?=\r?$))",
    re.I | re.M,
)
_DUTY_ACTION_RE = re.compile(
    r"负责|主导|承担|参与|设计|构建|搭建|开发|研发|实施|交付|部署|落地|完成|制定|规划|定义|"
    r"对接|集成|推进|评估|识别|梳理|拆解|输出|建立|维护|更新|优化|运维|验证|管理|确保|"
    r"协助|支持|协同|开展|实现|处理|监控|复盘|跟进|转化|迭代|沉淀|适配|调试|升级|编排|提供|明确|串联|整理|"
    r"\b(?:own|lead|design|build|develop|implement|deliver|deploy|integrate|conduct|"
    r"define|create|manage|operate|evaluate|assess|analyze|analyse|maintain|optimize|"
    r"coordinate|support|drive|orchestrate|validate|test|monitor|iterate|provide|\w+ing)\b",
    re.I,
)
_IMPLICIT_REQUIREMENT_RE = re.compile(
    r"熟悉|了解|掌握|精通|具备|具有|要求|优先|能够|可以|善于|擅长|至少|本科|硕士|博士|"
    r"\b(?:familiar|proficient|knowledge|experience|qualification|requirement|preferred|able\s+to)\b",
    re.I,
)

RELEVANT_TERMS = (
    ("架构", 5), ("解决方案", 5), ("方案架构", 5), ("FDE", 5),
    ("前线部署", 5), ("交付", 4), ("实施", 4), ("落地", 3),
    ("Agent", 3), ("智能体", 3), ("RAG", 3), ("工作流", 3),
    ("Workflow", 3), ("知识库", 3), ("知识工程", 3), ("大模型应用", 3),
    ("LLM", 2), ("系统集成", 3), ("集成", 2), ("客户成功", 2),
    ("技术咨询", 2), ("售前", 2), ("产品经理", 2), ("应用开发", 2),
    ("应用工程", 2), ("PoC", 2), ("Demo", 2), ("企业AI", 3),
    ("企业级AI", 3),
)
REPORTED_EXCLUDED_TERMS = ("销售", "招商", "渠道", "商务拓展", "BD", "回款", "拓客", "编导", "视频", "剪辑", "标注", "客服", "培训师", "讲师", "算法工程师", "算法研发", "高并发", "底层架构", "基础架构")
SALES_KPI_TERMS = ("销售额", "销售业绩", "业绩指标", "收入指标", "市场份额", "回款", "签约", "签单", "商机转化", "拓展客户", "开拓客户", "陌拜", "电话销售", "提成", "续约", "增购", "销售目标", "sales quota", "sales target", "revenue target", "commission")
SALES_ROLE_TERMS = ("销售", "商务拓展", "bd", "渠道", "招商", "客户经理", "业务拓展", "sales", "account executive", "business development")
ALGORITHM_TERMS = ("算法研发", "算法工程", "模型训练", "深度学习", "机器学习", "pytorch", "tensorflow", "模型调优", "算法优化", "推荐算法", "计算机视觉")
INFRA_TERMS = ("高并发", "高吞吐", "微服务", "分布式", "kubernetes", "k8s", "服务治理", "推理服务", "算力", "模型训练", "底层架构", "基础架构", "向量数据库", "性能优化")
CLASSIFICATIONS = ("重点投递", "海投池", "暂不投")
SCREENING_VERSION = "title-skills-three-queues-v5-20261023"

# These are context signals, not standalone positive/negative word lists.
# In particular, AI may mean Illustrator, and sales/algorithm/infra titles
# need their actual work object before they can be excluded.
AI_CONTEXT_TERMS = ("AI", "人工智能", "大模型", "LLM", "智能体", "Agent", "AIGC", "智能应用", "artificial intelligence", "large language model", "intelligent application")
PRODUCT_ROLE_TERMS = ("产品经理", "产品助理", "产品专员", "产品架构师", "产品策划", "产品负责人", "产品孵化", "产品内部孵化", "产品运营", "product manager", "product owner", "product assistant", "product specialist", "product architect")
AGENT_TOOL_TERMS = ("Agent", "Agentic", "智能体", "RAG", "检索增强", "知识工程", "知识库", "Workflow", "工作流", "工具调用", "LangChain", "LangGraph", "LlamaIndex", "Dify", "Coze", "n8n", "MCP")
APPLICATION_COUNTER_TERMS = (
    "大模型应用", "AI应用", "AI 应用", "人工智能应用", "Agent", "智能体", "RAG", "检索增强",
    "业务自动化", "流程自动化", "系统集成", "系统对接", "应用落地", "应用开发", "应用研发",
    "客户技术方案", "解决方案", "技术方案", "售前", "技术销售", "FDE", "PoC", "Demo",
    "软件交付", "系统交付", "应用交付", "项目交付", "软件实施", "模型工程化", "大模型工程化", "AI框架", "AI SDK",
)
SYSTEM_BUILD_TERMS = (
    "大模型应用", "AI应用", "AI 应用", "人工智能应用", "Agent开发", "Agent研发", "智能体开发", "智能体研发",
    "系统集成", "系统对接", "软件交付", "系统交付", "应用交付", "软件实施", "业务自动化", "流程自动化",
)
ACQUISITION_TERMS = ("电话销售", "电销", "电话邀约", "陌拜", "纯销售", "纯获客", "会展销售", "招商销售", "招商拓展", "招商加盟", "销售获客", "拓客", "开发新客户", "开发客户", "客户开发", "开拓新客户", "开拓客户", "新客开发", "商户拓展", "地推", "渠道拓展", "渠道开发", "cold call", "cold calling", "prospecting", "customer acquisition", "client acquisition", "customer development", "channel development", "new business acquisition")
CONTENT_ROLE_TERMS = ("主播", "编导", "编剧", "原画师", "原画美术", "美术", "插画师", "平面设计", "广告设计", "视频剪辑", "剪辑师", "短视频拍摄", "摄影师", "抽卡师")
HARDWARE_ROLE_TERMS = ("芯片设计", "芯片开发", "芯片研发", "芯片测试", "芯片验证", "封装工艺", "晶圆工艺", "集成电路设计", "电路设计", "硬件工程", "硬件开发", "硬件研发", "硬件设计", "工艺工程", "机械设计", "电控工程", "嵌入式软件", "规划控制算法", "hardware engineer", "hardware developer", "circuit design", "chip design")
HARDWARE_OBJECT_TERMS = ("MCU", "STM32", "PCB", "FPGA", "Verilog", "RTOS", "AUTOSAR", "晶圆制造", "封装工艺", "嵌入式软件开发", "电机控制", "轨迹规划")
BASE_MODEL_TERMS = ("底模", "基座模型", "基础大模型", "模型预训练", "大模型预训练", "基础模型研究", "大模型基础研究")
INFRA_SPECIALIST_TERMS = ("CUDA", "算子", "GPU内核", "编译器", "集合通信", "通信优化", "分布式训练", "训练集群", "集群调度", "推理加速", "高性能计算", "模型并行", "算力调度", "存储引擎", "网络协议栈")
SALES_TECH_COUNTER_TERMS = ("客户技术方案", "技术方案", "解决方案设计", "方案设计", "售前支持", "技术咨询", "方案交付", "系统集成", "业务集成", "系统对接", "应用开发", "应用研发", "Agent开发", "Agent研发", "智能体开发", "智能体研发", "PoC验证", "PoC实施", "PoC交付", "项目交付", "实施交付", "软件实施", "应用部署", "私有化部署", "solution design", "technical solution", "system integration", "business integration", "project delivery", "implementation delivery", "PoC validation", "PoC delivery", "customer deployment")
BUSINESS_APPLICATION_TERMS = ("B端产品", "B 端产品", "B端业务", "ToB产品", "To B产品", "企业软件", "业务系统", "业务应用", "企业应用", "软件产品", "ERP产品", "ERP系统", "CRM系统", "SaaS产品", "SaaS平台", "enterprise application", "business application", "enterprise software", "ERP product", "software product", "SaaS product")
ARCHITECTURE_ROLE_TERMS = ("架构师", "架构专家", "架构工程师", "系统架构", "应用架构", "软件架构", "产品架构", "业务架构", "企业架构", "技术架构", "architect", "architecture")
INTEGRATION_TERMS = ("系统集成", "业务集成", "应用集成", "企业集成", "API集成", "系统对接", "业务对接", "集成实施", "集成交付", "system integration", "business integration", "application integration", "integration engineer", "integration consultant", "integration architect")
HARDWARE_ARCHITECTURE_TERMS = ("芯片架构", "SoC架构", "CPU架构", "GPU架构", "整车架构", "车辆架构", "电子电气架构", "硬件架构", "嵌入式架构", "底层硬件", "chip architect", "hardware architect", "vehicle architect", "SoC architect")
NONTECH_ROLE_TERMS = ("财务BP", "财务经理", "财务专员", "财务主管", "会计", "审计师", "猎头", "招聘顾问", "招聘专员", "招聘经理", "人力资源", "人事专员", "行政专员", "行政助理", "法务", "律师", "finance business partner", "financial analyst", "accountant", "recruiter", "headhunter", "recruitment consultant", "human resources")
FINANCIAL_SERVICE_ROLE_TERMS = ("财税咨询师", "财税顾问", "税务咨询师", "税务顾问", "财务咨询师", "财务顾问", "税务师", "tax consultant", "tax advisor", "tax adviser", "accounting consultant", "financial consultant")
FINANCIAL_SERVICE_OBJECT_TERMS = ("CPA", "CTA", "注册会计师", "会计职称", "财税工作", "税务代理", "税务筹划", "税务咨询", "财务诊断", "财务分析", "tax planning", "tax compliance", "accounting advisory")
FINANCIAL_SOFTWARE_TERMS = ("AI产品", "AI 产品", "大模型产品", "人工智能产品", "财税软件", "财务软件", "会计软件", "税务软件", "税务系统", "财税系统", "财务系统", "tax software", "accounting software")
_DUTY_NEGATION_RE = re.compile(
    r"(?:无需|无须|不需要|不需|不必|不用|不)(?:再|直接|亲自)?"
    r"(?:承担|负责|参与|涉及|开展|进行|要求|从事|处理|做|开拓|开发|获客|陌拜|电话销售)"
    r"|\b(?:not\s+(?:responsible\s+for|involved\s+in|required\s+to)|"
    r"no\s+(?:responsibility\s+for|hands[ -]on)|without\s+responsibility\s+for|"
    r"do(?:es)?\s+not|don['’]t|doesn['’]t)\b", re.I,
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " | ".join(text(x) for x in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def contains(value: Any, term: str) -> bool:
    return term.lower() in text(value).lower()


def _is_requirement_heading(heading: str) -> bool:
    normalized = heading.casefold()
    return any(normalized == value.casefold() for value in _REQUIREMENT_HEADINGS)


def _is_other_heading(heading: str) -> bool:
    normalized = heading.casefold()
    return any(normalized == value.casefold() for value in _OTHER_HEADINGS)


def _active_duty_statements(value: str, *, implicit: bool = True) -> str:
    """Keep only action-bearing statements from an identified duty section.

    Commas are deliberately retained: Chinese JD bullets often state the
    action once and then enumerate its objects ("负责方案设计、系统集成、
    Agent应用交付"). A bare list of tools or skills has no action and is not
    promoted to duty evidence.
    """

    statements = re.split(r"(?<=[。；;！？!?])\s*|\r?\n+", value)
    kept = []
    for statement in statements:
        if implicit and _IMPLICIT_REQUIREMENT_RE.search(statement):
            continue
        cleaned = _responsibility_text(statement).strip()
        if cleaned and _DUTY_ACTION_RE.search(cleaned):
            kept.append(cleaned)
    return "\n".join(dict.fromkeys(kept))


def extract_jd_sections(jd: str, *, preserve_responsibilities: bool = False) -> dict[str, str]:
    """Separate actual job duties from requirements and non-duty copy.

    When a JD has explicit section headings, responsibility evidence comes
    only from duty sections (plus an action-bearing preamble before the first
    heading). Without headings, only action-bearing statements are retained;
    requirement, skill, company and benefits sections are never fallback duty
    evidence. ``preserve_responsibilities`` keeps the full identified duty
    body for semantic review instead of requiring a code-recognized verb.
    """

    jd = text(jd)
    result: dict[str, list[str]] = {"responsibilities": [], "requirements": [], "other": []}

    def preserve_unheaded(body: str) -> None:
        for statement in re.split(r"(?<=[。；;！？!?])\s*|\r?\n+", body):
            # Recognize an explicit qualification at the start of a bullet;
            # words such as "要求" inside a described duty are not a filter.
            start = re.sub(r"^\s*(?:[•*▪-]\s*|\d+[、.)）]\s*)?", "", statement)
            key = "requirements" if _IMPLICIT_REQUIREMENT_RE.match(start) else "responsibilities"
            result[key].append(statement.strip())

    matches = list(_SECTION_HEADING_RE.finditer(jd))
    if not matches:
        if preserve_responsibilities:
            preserve_unheaded(jd)
        else:
            result["responsibilities"].append(_active_duty_statements(jd, implicit=True))
        return {key: value for key, chunks in result.items() if (value := "\n".join(x for x in chunks if x.strip()))}

    # Preserve the role overview for semantic review; deterministic rules
    # retain their existing action-only treatment. Explicit company/skills
    # sections are already bounded by the section headings above.
    preamble = jd[:matches[0].start()]
    if preamble.strip():
        if preserve_responsibilities:
            preserve_unheaded(preamble)
        else:
            result["responsibilities"].append(_active_duty_statements(preamble, implicit=True))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(jd)
        heading = match.group("heading")
        body = jd[match.end():end].strip()
        if _is_requirement_heading(heading):
            key = "requirements"
        elif _is_other_heading(heading):
            key = "other"
        else:
            key = "responsibilities"
        result[key].append(_active_duty_statements(body, implicit=True)
                           if key == "responsibilities" and not preserve_responsibilities else body)
    return {key: value for key, chunks in result.items() if (value := "\n".join(x for x in chunks if x.strip()))}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def load_config(path: Path) -> dict[str, Any]:
    return read_json(path)


def config_maps(config: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], set[str], set[str]]:
    current = {str(x["id"]): str(x["query"]) for x in config.get("current", {}).get("keywords", [])}
    target = {str(x["id"]): str(x["query"]) for x in config.get("target", {}).get("keywords", [])}
    current_exp = {str(x["id"]) for x in config.get("current", {}).get("experiences", [])}
    target_exp = {str(x["id"]) for x in config.get("target", {}).get("experiences", [])}
    return current, target, current_exp, target_exp


def _ordinary_source_path(path: Path, root: Path) -> bool:
    """Raw inputs must stay within this root and not traverse reparse links."""
    try:
        relative = path.absolute().relative_to(root.absolute())
        path.resolve().relative_to(root.resolve())
        current = root.absolute()
        for part in relative.parts:
            current = current / part
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                return False
    except (OSError, ValueError, RuntimeError):
        return False
    return True


def _raw_source_files(root: Path) -> list[Path]:
    """Discover ordinary raw files without following an external city folder."""
    files = []
    raw = root / "raw"
    if not _ordinary_source_path(raw, root):
        return files
    for track in ("current", "target"):
        track_dir = raw / track
        if not _ordinary_source_path(track_dir, root):
            continue
        try:
            cities = list(track_dir.iterdir())
        except OSError:
            continue
        for city_dir in cities:
            if not _ordinary_source_path(city_dir, root) or not city_dir.is_dir():
                continue
            files.extend(path for path in city_dir.glob("*.json") if _ordinary_source_path(path, root))
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _source_path_in_scope(path: Path, root: Path, selected_tracks: set[str], city: str | None) -> bool:
    """Filter known path scope before opening JSON, not after reading payloads."""
    parts = path.relative_to(root).parts
    if len(parts) != 4 or parts[1] not in selected_tracks:
        return False
    if city and parts[2] in CITY_NAMES:
        wanted = text(city).strip().casefold()
        if wanted not in {parts[2].casefold(), CITY_NAMES[parts[2]].casefold()}:
            return False
    return True


def parse_source(path: Path, root: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return None
    parts = relative.split("/")
    if len(parts) != 4 or parts[0] != "raw" or parts[1] not in ("current", "target"):
        return None
    if not _ordinary_source_path(path, root):
        return None
    track, city_id = parts[1], parts[2]
    stem = path.stem.split("__")
    if len(stem) != 5:
        return None
    _, file_city, keyword_id, salary_id, experience_id = stem
    current, target, current_exp, target_exp = config_maps(config)
    keywords = current if track == "current" else target
    if file_city != city_id or keyword_id not in keywords:
        return None
    if experience_id not in (current_exp if track == "current" else target_exp):
        return None
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    filters = payload.get("filters") if isinstance(payload, dict) else None
    if not isinstance(filters, dict) or not isinstance(payload.get("jobs"), list):
        return None
    return {
        "source_file": relative,
        "track": track,
        "city_id": city_id,
        "city": CITY_NAMES.get(city_id, payload.get("city", city_id)),
        "keyword_id": keyword_id,
        "keyword": keywords[keyword_id],
        "salary_id": salary_id,
        "salary_filter": str(filters.get("salary", "")),
        "experience_id": experience_id,
        "experience_filter": str(filters.get("experience", "")),
        "degree": str(filters.get("degree", "203")),
        "combination": path.stem,
        "payload": payload,
    }


def _source_rows(source: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Attach provenance without mutating payloads or renumbering skipped rows."""

    for row_index, row in enumerate(source["payload"].get("jobs", [])):
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["source_file"] = source["source_file"]
        item["source_row"] = row_index
        item["source_line_number"] = row_index + 1
        item["source_context"] = {
            **{key: value for key, value in source.items() if key != "payload"},
            "source_line_number": row_index + 1,
        }
        yield item


def _selected_tracks(tracks: str) -> tuple[str, ...]:
    if tracks == "all":
        return ("current", "target")
    if tracks in ("current", "target"):
        return (tracks,)
    raise ValueError("tracks 必须是 'current'、'target' 或 'all'")


def _city_matches(source: dict[str, Any], city: str | None) -> bool:
    if not city:
        return True
    wanted = text(city).strip().casefold()
    return wanted in {
        text(source.get("city")).strip().casefold(),
        text(source.get("city_id")).strip().casefold(),
    }


def _configured_combinations(
    config: dict[str, Any],
    tracks: Iterable[str],
    city: str | None = None,
) -> list[str]:
    """Expand the configured search space without requiring collection state."""

    wanted_city = text(city).strip().casefold() if city else ""
    cities = config.get("cities", [])
    salaries = config.get("salaries", [])
    if not isinstance(cities, list) or not isinstance(salaries, list):
        return []

    result: list[str] = []
    for track in tracks:
        track_config = config.get(track, {})
        if not isinstance(track_config, dict):
            continue
        keywords = track_config.get("keywords", [])
        experiences = track_config.get("experiences", [])
        if not isinstance(keywords, list) or not isinstance(experiences, list):
            continue
        for city_spec in cities:
            if not isinstance(city_spec, dict):
                continue
            city_id = text(city_spec.get("id")).strip()
            city_name = text(city_spec.get("name") or CITY_NAMES.get(city_id, city_id)).strip()
            if not city_id or (wanted_city and wanted_city not in {city_id.casefold(), city_name.casefold()}):
                continue
            for keyword in keywords:
                if not isinstance(keyword, dict) or not text(keyword.get("id")):
                    continue
                for salary in salaries:
                    if not isinstance(salary, dict) or not text(salary.get("id")):
                        continue
                    for experience in experiences:
                        if not isinstance(experience, dict) or not text(experience.get("id")):
                            continue
                        result.append("__".join((
                            track,
                            city_id,
                            text(keyword["id"]),
                            text(salary["id"]),
                            text(experience["id"]),
                        )))
    return sorted(set(result))


def _balanced_unique_sample(jobs: list[dict[str, Any]], sample_size: int | None) -> list[dict[str, Any]]:
    """Round-robin unique jobs by city, search combination, and track."""

    if sample_size is None:
        return jobs
    if sample_size <= 0:
        return []

    by_group: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        raw_contexts = job.get("observed_in") or []
        contexts = [context for context in raw_contexts if isinstance(context, dict)] if isinstance(raw_contexts, list) else []
        groups = {
            (
                text(context.get("city_id") or context.get("city")) or "~city",
                "__".join(text(context.get("combination")).split("__")[1:]) if context.get("combination") else "~unassigned",
                text(context.get("track")) or "unknown",
            )
            for context in contexts
        } or {("~city", "~unassigned", "unknown")}
        for group in sorted(groups):
            by_group[group].append(job)

    def stable_group_key(job: dict[str, Any], group: tuple[str, str, str]) -> tuple[str, str, str]:
        contexts = [
            context
            for context in (job.get("observed_in") or [])
            if isinstance(context, dict)
            and (
                text(context.get("city_id") or context.get("city")) or "~city",
                "__".join(text(context.get("combination")).split("__")[1:]) if context.get("combination") else "~unassigned",
                text(context.get("track")) or "unknown",
            ) == group
        ]
        relative_path = min((text(context.get("source_file")) for context in contexts), default="~unassigned")
        job_id = text(job.get("job_id") or job.get("dedupe_key"))
        combination = min((text(context.get("combination")) for context in contexts), default="~unassigned")
        return relative_path, job_id, combination

    for group, queue in by_group.items():
        queue.sort(key=lambda job: stable_group_key(job, group))

    cities = sorted({group[0] for group in by_group})
    query_combinations = {
        city: sorted({group[1] for group in by_group if group[0] == city})
        for city in cities
    }
    tracks = ("current", "target", "unknown")
    offsets = {group: 0 for group in by_group}
    chosen: list[dict[str, Any]] = []
    chosen_keys: set[str] = set()
    while len(chosen) < sample_size:
        made_progress = False
        for city in cities:
            for query_combination in query_combinations[city]:
                for track in tracks:
                    group = (city, query_combination, track)
                    queue = by_group.get(group, [])
                    while offsets.get(group, 0) < len(queue):
                        job = queue[offsets[group]]
                        offsets[group] += 1
                        key = text(job.get("dedupe_key") or detail_key(job))
                        if key in chosen_keys:
                            continue
                        chosen.append(job)
                        chosen_keys.add(key)
                        made_progress = True
                        break
                    if len(chosen) >= sample_size:
                        break
                if len(chosen) >= sample_size:
                    break
            if len(chosen) >= sample_size:
                break
        if not made_progress:
            break
    return chosen


def collect_rows(
    root: Path,
    config: dict[str, Any],
    city: str | None = None,
    sample_size: int | None = None,
    sample_strategy: str = "sequential",
    tracks: str = "all",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load list rows, optionally selecting a balanced sample of unique jobs.

    ``sequential`` preserves the historical path-ordered row sampling behavior.
    ``balanced-dedup`` filters tracks/city first, deduplicates across all source
    combinations, then round-robins unique jobs by their observed combinations.
    """

    if sample_strategy not in ("sequential", "balanced-dedup"):
        raise ValueError("sample_strategy 必须是 'sequential' 或 'balanced-dedup'")
    selected_tracks = set(_selected_tracks(tracks))
    # Sort the combined set by relative path.  This makes the sample contract
    # deterministic across current/target tracks instead of implicitly giving
    # one track precedence over the other.
    files = _raw_source_files(root)
    rows: list[dict[str, Any]] = []
    invalid: list[str] = []
    filtered_files = 0
    for path in files:
        if not _source_path_in_scope(path, root, selected_tracks, city):
            continue
        source = parse_source(path, root, config)
        if source is None:
            invalid.append(path.relative_to(root).as_posix())
            continue
        if source["track"] not in selected_tracks or not _city_matches(source, city):
            continue
        filtered_files += 1
        rows.extend(_source_rows(source))

    if sample_strategy == "balanced-dedup":
        unique_jobs, dedupe_meta = merge_rows(rows)
        sampled = _balanced_unique_sample(unique_jobs, sample_size)
        metadata = {
            "raw_files_seen": len(files),
            "filtered_raw_files": filtered_files,
            "invalid_files": invalid,
            "raw_record_count": len(rows),
            "raw_sample_count": len(sampled),
            "unique_job_count": len(unique_jobs),
            "dedupe_removed": dedupe_meta["dedupe_removed"],
            "city": city,
            "tracks": tracks,
            "sample_size": sample_size,
            "sample_strategy": sample_strategy,
        }
        return sampled, metadata

    sampled = rows if sample_size is None else rows[:max(0, sample_size)]
    metadata = {
        "raw_files_seen": len(files),
        "filtered_raw_files": filtered_files,
        "invalid_files": invalid,
        "raw_sample_count": len(sampled),
        "city": city,
        "tracks": tracks,
        "sample_size": sample_size,
        "sample_strategy": sample_strategy,
    }
    return sampled, metadata


def inspect_city(
    root: Path,
    config: dict[str, Any],
    city: str | None = None,
    tracks: str = "all",
) -> dict[str, Any]:
    """Summarize valid raw data and collection-manifest states for a city."""

    selected_tracks = set(_selected_tracks(tracks))
    files = _raw_source_files(root)
    rows: list[dict[str, Any]] = []
    combinations: set[str] = set()
    valid_files = 0
    invalid_files = 0
    cities: dict[str, str] = {}
    expected_combinations = _configured_combinations(config, selected_tracks, city)
    expected_set = set(expected_combinations)
    completed_combinations: set[str] = set()
    for path in files:
        if not _source_path_in_scope(path, root, selected_tracks, city):
            continue
        source = parse_source(path, root, config)
        if source is None:
            invalid_files += 1
            continue
        if source["track"] not in selected_tracks or not _city_matches(source, city):
            continue
        valid_files += 1
        combinations.add(source["combination"])
        cities[source["city_id"]] = source["city"]
        rows.extend(_source_rows(source))
    _, merge_meta = merge_rows(rows)

    manifest_path = root / "collection_manifest.json"
    try:
        manifest = read_json(manifest_path) if _ordinary_source_path(manifest_path, root) and manifest_path.is_file() else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        manifest = {}
    records = manifest.get("records", []) if isinstance(manifest, dict) else []
    status_counts: Counter[str] = Counter({
        "success": 0,
        "no_data": 0,
        "paused": 0,
        "failed": 0,
    })
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        combination = text(record.get("combination"))
        combination_parts = combination.split("__")
        record_track = text(record.get("track") or (combination_parts[0] if combination_parts else ""))
        record_city_id = text(record.get("city_id") or (combination_parts[1] if len(combination_parts) > 1 else ""))
        record_city = text(record.get("city")) or CITY_NAMES.get(record_city_id, record_city_id)
        if record_track not in selected_tracks:
            continue
        if city:
            normalized_city = text(city).strip().casefold()
            if normalized_city not in {record_city.strip().casefold(), record_city_id.strip().casefold()}:
                continue
        record_status = text(record.get("status") or "unknown").strip().casefold() or "unknown"
        status_counts[record_status] += 1
        if combination in expected_set and record_status in {"success", "no_data"}:
            completed_combinations.add(combination)

    return {
        "city": city,
        "city_id": next(iter(cities)) if len(cities) == 1 else None,
        "cities": [{"city_id": city_id, "city": name} for city_id, name in sorted(cities.items())],
        "tracks": tracks,
        "raw_record_count": len(rows),
        "deduplicated_job_count": merge_meta["unique_job_count"],
        # ``combination_count`` is retained as the count of valid raw files for
        # existing callers.  Explicit names distinguish that from the full
        # configured search space.
        "combination_count": len(combinations),
        "raw_combination_count": len(combinations),
        "raw_combinations": sorted(combinations),
        "expected_combination_count": len(expected_combinations),
        "expected_combinations": expected_combinations,
        "manifest_record_count": sum(status_counts.values()),
        "manifest_status_counts": dict(sorted(status_counts.items())),
        "completed_combination_count": len(completed_combinations),
        "pending_combination_count": max(0, len(expected_combinations) - len(completed_combinations)),
        "valid_raw_file_count": valid_files,
        "invalid_raw_file_count": invalid_files,
    }


def identity_values(row: dict[str, Any]) -> list[tuple[str, str]]:
    values = [(text(row.get("job_id")), "job_id"), (text(row.get("encrypt_job_id")), "encrypt_job_id"), (text(row.get("job_link") or row.get("link")), "job_link")]
    return [(value, kind) for value, kind in values if value]


_SOURCE_FIELDS = ("title", "salary", "skills", "skill_tags", "job_labels")
_SOURCE_METADATA = ("source_file", "source_row", "source_line_number", "source_context",
                    "field_candidates", "field_conflicts")


def _stable_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=text)


def _field_source_reference(source: dict[str, Any]) -> dict[str, Any]:
    """Reference the full observed_in context without embedding it per field."""
    reference = {key: source[key] for key in ("source_file", "source_row") if key in source}
    if not reference.get("source_file"):
        reference.update({key: source[key] for key in ("track", "combination") if key in source})
    return reference


def _merge_field_candidates(item: dict[str, Any], row: dict[str, Any], sources: list[dict]) -> None:
    """Keep each observed value attached to its original source contexts."""
    destination = item.setdefault("field_candidates", {})
    prior = row.get("field_candidates")
    prior = prior if isinstance(prior, dict) else {}
    for field in _SOURCE_FIELDS:
        incoming = prior.get(field)
        if not isinstance(incoming, list) or not incoming:
            value = row.get(field)
            incoming = [{"value": value, "sources": sources}] if value not in (None, "", [], {}) else []
        known = {_stable_value(entry["value"]): entry for entry in destination.get(field, [])}
        for entry in incoming:
            if not isinstance(entry, dict) or entry.get("value") in (None, "", [], {}):
                continue
            key = _stable_value(entry["value"])
            candidate = known.setdefault(key, {"value": entry["value"], "sources": []})
            for source in entry.get("sources", sources):
                if isinstance(source, dict):
                    reference = _field_source_reference(source)
                    if not _has_observed_context(candidate["sources"], reference):
                        candidate["sources"].append(reference)
            candidate["sources"].sort(key=_stable_value)
        if known:
            destination[field] = [known[key] for key in sorted(known)]


def _field_values(job: dict[str, Any], field: str) -> list[Any]:
    candidates = job.get("field_candidates")
    entries = candidates.get(field) if isinstance(candidates, dict) else None
    values = [entry["value"] for entry in entries or [] if isinstance(entry, dict) and "value" in entry]
    return values or ([job[field]] if job.get(field) not in (None, "", [], {}) else [])


def _source_conflict_notes(job: dict[str, Any]) -> list[str]:
    labels = {"title": "标题", "salary": "薪资", "skills": "技能", "skill_tags": "技能标签", "job_labels": "岗位标签"}
    return [f"多来源{labels[field]}冲突，需核实原始记录：" + " / ".join(text(value) for value in _field_values(job, field))
            for field in _SOURCE_FIELDS if len(_field_values(job, field)) > 1]


def merge_rows(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}
    raw_count = 0
    for row in rows:
        raw_count += 1
        identities = identity_values(row)
        if not identities:
            key = f"unkeyed:{raw_count}"
            existing_keys: list[str] = []
        else:
            existing_keys = list(dict.fromkeys(aliases[value] for value, _ in identities if value in aliases))
            key = existing_keys[0] if existing_keys else identities[0][0]
        item = by_key.get(key)
        if item is None:
            item = {k: v for k, v in row.items() if k not in _SOURCE_METADATA}
            item["dedupe_key"] = key
            item["dedupe_key_type"] = identities[0][1] if identities else "synthetic_source_row"
            item["observed_in"] = []
            by_key[key] = item
        # A later row can connect two previously separate records (for
        # example, one record has only job_id and another only encrypt_job_id,
        # while a third exposes the shared job_link).  Collapse those aliases
        # before adding the current row so the final index remains unique.
        for other_key in existing_keys[1:]:
            other = by_key.pop(other_key, None)
            if other is None:
                continue
            for field, value in other.items():
                if field in (*_SOURCE_METADATA, "observed_in"):
                    continue
                if not item.get(field) and value not in (None, "", [], {}):
                    item[field] = value
            for context in other.get("observed_in", []):
                if context not in item["observed_in"]:
                    item["observed_in"].append(context)
            _merge_field_candidates(item, other, other.get("observed_in", []))
            for alias, alias_key in list(aliases.items()):
                if alias_key == other_key:
                    aliases[alias] = key
        for field, value in row.items():
            if field not in _SOURCE_METADATA and not item.get(field) and value not in (None, "", [], {}):
                item[field] = value
        for value, _ in identities:
            aliases[value] = key
        existing_contexts = row.get("observed_in")
        if not isinstance(existing_contexts, list):
            existing_contexts = []
        row_contexts = [dict(context) for context in existing_contexts if isinstance(context, dict)]
        for prior_context in existing_contexts:
            if not isinstance(prior_context, dict):
                continue
            context = dict(prior_context)
            if not _has_observed_context(item["observed_in"], context):
                item["observed_in"].append(context)

        # Merged rows already carry their original contexts.  Do not append a
        # synthetic context when such a row is merged again; otherwise retain
        # one source-row context even for context-free callers so provenance is
        # never silently discarded.
        source_context = row.get("source_context")
        has_flat_source = any(key in row for key in ("source_file", "source_row", "source_line_number"))
        if isinstance(source_context, dict) or has_flat_source or not existing_contexts:
            context = dict(source_context) if isinstance(source_context, dict) else {}
            if "source_file" in row:
                context["source_file"] = row["source_file"]
            elif "source_file" not in context:
                context["source_file"] = ""
            context["source_row"] = row.get("source_row", context.get("source_row", raw_count - 1))
            context["source_line_number"] = row.get("source_line_number", context.get("source_line_number", raw_count))
            if not _has_observed_context(item["observed_in"], context):
                item["observed_in"].append(context)
            if not _has_observed_context(row_contexts, context):
                row_contexts.append(context)
        _merge_field_candidates(item, row, row_contexts)
    jobs = list(by_key.values())
    for item in jobs:
        candidates = item.get("field_candidates", {})
        item["field_conflicts"] = [field for field in _SOURCE_FIELDS if len(candidates.get(field, [])) > 1]
        for field, entries in candidates.items():
            # Display fields are stable without inventing a preferred source.
            # Screening below inspects all candidates and their original context.
            item[field] = entries[0]["value"]
        if not item.get("job_id"):
            item["job_id"] = item.get("dedupe_key")
    return jobs, {"raw_list_rows": raw_count, "unique_job_count": len(jobs), "dedupe_removed": raw_count - len(jobs)}


def _has_observed_context(contexts: list[dict[str, Any]], candidate: dict[str, Any]) -> bool:
    """Check full context equality so distinct provenance is not collapsed."""

    candidate_key = json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=text)
    return any(
        json.dumps(context, ensure_ascii=False, sort_keys=True, default=text) == candidate_key
        for context in contexts
        if isinstance(context, dict)
    )


def parse_salary(value: Any) -> dict[str, Any]:
    """Parse explicit monthly amounts; ambiguous ceilings remain unknown.

    Hard list exclusions must not mistake a 万 amount, daily rate, annual
    package, open-ended floor, or multi-part pay for a low monthly ceiling.
    Extra salary months are retained, never averaged into fixed monthly pay.
    """
    raw = re.sub(r"\s+", "", text(value))
    lower = upper = None
    months = re.search(r"(\d+)\s*薪", raw)
    nonmonthly = re.search(r"年薪|日薪|时薪|周薪|每(?:年|天|日|小时|周)|[/／](?:年|天|日|小时|时|周)|per(?:year|day|hour|week)", raw, re.I)
    amount = r"(\d+(?:\.\d+)?)([kK千万]|元)?"
    match = re.search(amount + r"(?:-|－|–|—|~|～|至|到)" + amount, raw)
    units = {"k": 1.0, "千": 1.0, "万": 10.0, "元": 0.001}
    open_ended_suffix = r"(?:元)?(?:(?:[/／]|每)月)?(?:以上|起|\+|及以上|起步)"
    if not nonmonthly and match and (match.group(2) or match.group(4)):
        low_unit = (match.group(2) or match.group(4)).lower()
        high_unit = (match.group(4) or match.group(2)).lower()
        lower = float(match.group(1)) * units[low_unit]
        upper = float(match.group(3)) * units[high_unit]
        extra_amount = re.search(r"\d+(?:\.\d+)?[kK千万元]", raw[:match.start()] + "|" + raw[match.end():])
        if extra_amount or lower > upper:
            lower = upper = None
        elif re.match(open_ended_suffix, raw[match.end():]):
            upper = None
    elif not nonmonthly and not match:
        amounts = list(re.finditer(r"(\d+(?:\.\d+)?)([kK千万]|元)", raw))
        if len(amounts) == 1:
            one = amounts[0]
            lower = upper = float(one.group(1)) * units[one.group(2).lower()]
            if re.match(open_ended_suffix, raw[one.end():]) or re.search(r"(?:至少|最低|起薪|不低于)$", raw[:one.start()]):
                upper = None
    if lower is not None and lower <= 0:
        lower = upper = None
    return {"raw": value or "", "lower_k": lower, "upper_k": upper, "months": int(months.group(1)) if months else None}


def salary_status(parsed: dict[str, Any]) -> str:
    low, high = parsed.get("lower_k"), parsed.get("upper_k")
    if low is None or high is None:
        return "未知"
    if low >= 17:
        return "达标（下限>=17K）"
    if high < 17:
        return "低于目标（上限<17K）"
    return "跨17K（下限<17K、上限>=17K）"


def _screen_pattern(term: str) -> str:
    """Match abbreviations without finding AI in unrelated English words."""

    pattern = re.escape(term).replace(r"\ ", r"\s+")
    if re.match(r"[A-Za-z0-9]", term):
        pattern = r"(?<![A-Za-z0-9_])" + pattern
    if re.search(r"[A-Za-z0-9]$", term):
        pattern += r"(?![A-Za-z0-9_])"
    return pattern


@lru_cache(maxsize=2048)
def _screen_regex(term: str) -> re.Pattern[str]:
    # The expanded bilingual families exceed re's small global cache. Compile
    # once per bounded literal so large offline shortlists stay inexpensive.
    return re.compile(_screen_pattern(term), re.I)


def _screen_terms(value: Any, terms: Iterable[str]) -> list[str]:
    value = text(value)
    return [term for term in terms if _screen_regex(term).search(value)]


def _screen_evidence(fields: dict[str, str], terms: Iterable[str], family: str) -> list[dict[str, str]]:
    """Retain literal, field-addressable quotes, never inferred JD duties."""

    result = []
    terms = tuple(dict.fromkeys(terms))
    for field, value in fields.items():
        for term in terms:
            match = _screen_regex(term).search(value)
            if match:
                evidence = {"family": family, "field": field, "quote": match.group(0)}
                if evidence not in result:
                    result.append(evidence)
    return result


def _has_ai_context(fields: dict[str, str]) -> bool:
    """Disambiguate Adobe AI and AI Coding from AI as the work object."""

    if any(_screen_terms(value, AI_CONTEXT_TERMS[1:]) for value in fields.values()):
        return True
    for value in fields.values():
        if not _screen_terms(value, ("AI",)):
            continue
        if _screen_terms(value, ("AI产品", "AI 产品", "AI应用", "AI 应用", "AI业务", "AI 业务", "AI全栈", "AI 全栈", "AI框架", "AI SDK", "AI原生")):
            return True
        if _screen_terms(value, ("PS", "Photoshop", "AE", "Illustrator", "Adobe")):
            continue
        without_coding = re.sub(r"(?<![A-Za-z0-9_])AI\s*Coding(?![A-Za-z0-9_])", "", value, flags=re.I)
        if _screen_terms(without_coding, ("AI",)):
            return True
    return False


def _responsibility_text(value: str, *, filter_product_sales: bool = True) -> str:
    """Drop narrow, explicit negations/other-team duties, not whole JDs.

    This is deliberately clause-scoped: '负责方案设计，不负责销售' keeps
    the first clause. Selling or introducing an Agent product is also not
    evidence of personally building an Agent application.
    """

    clauses = re.split(r"[，,。；;|\r\n！？!?]|(?<=[A-Za-z0-9])\.(?=\s+[A-Z])", value)
    kept, changed = [], False
    for clause in clauses:
        positive = clause
        negative = _DUTY_NEGATION_RE.search(positive)
        if negative:
            positive = positive[:negative.start()]
        other_team = re.search(
            r"(?:由|交由).{0,20}(?:技术|研发|售前|交付|实施|开发|其他|专门|解决方案).{0,8}(?:团队|部门|同事|人员|工程师|架构师|顾问|专家).{0,10}(?:负责|承担|完成)"
            r"|^\s*(?:相关)?(?:技术|研发|售前|交付|实施|开发|其他|专门)(?:团队|部门|同事|人员|工程师|架构师|顾问|专家)(?:全权|主要|独立)?(?:负责|承担|完成)"
            r"|\b(?:handled|owned|performed|delivered|implemented)\s+by\s+(?:the\s+)?(?:engineering|technical|other|delivery|presales|implementation)\s+(?:team|engineers?|consultants?|architects?|specialists?)\b"
            r"|^\s*(?:the\s+)?(?:engineering|technical|other|delivery|presales|implementation)\s+(?:team|engineers?|consultants?|architects?|specialists?)\s+(?:owns?|handles?|is\s+responsible\s+for)\b",
            positive, re.I,
        )
        if other_team:
            positive = ""
        object_only = re.search(r"(?:介绍|推销|售卖|销售|推广).{0,24}(?:Agent|智能体|AI|大模型).{0,12}(?:产品|服务|课程)", positive, re.I)
        if filter_product_sales and object_only and not _screen_terms(positive, SALES_TECH_COUNTER_TERMS):
            positive = positive[:object_only.start()]
        changed = changed or positive != clause
        if positive.strip():
            kept.append(positive.strip())
    return "\n".join(kept) if changed else value


def _has_technical_disclaimer(value: str) -> bool:
    for clause in re.split(r"[，,。；;|\r\n！？!?]", value):
        match = _DUTY_NEGATION_RE.search(clause)
        if match and _screen_terms(clause[match.end():], (*SALES_TECH_COUNTER_TERMS, "研发", "PoC", "交付", "integration", "delivery", "deployment")):
            return True
    return False


def _has_sales_title(title: str) -> bool:
    # 商务 in 电子商务/商务智能 describes an application domain, not sales.
    return bool(_screen_terms(title, SALES_ROLE_TERMS)) or bool(
        re.search(r"(?<!电子)商务(?!智能|数据|系统|产品)", title)
    )


def _semantic_role_exclusions(title: str, fields: dict[str, str], *, title_as_counter: bool = True) -> dict[str, Any]:
    """Exclude explicit main-work/object combinations, with counterevidence.

    Shared by list screening and JD fallback: a sales KPI, algorithm title,
    K8S tag, training/operations/customer-service word is not an exclusion.
    """

    raw_fields = fields
    fields = {key: _responsibility_text(value) for key, value in fields.items()}
    source = " | ".join(fields.values())
    has = lambda terms: bool(_screen_terms(source, terms))
    title_has = lambda terms: bool(_screen_terms(title, terms))
    application_counter = has(APPLICATION_COUNTER_TERMS)
    product_counter = _has_ai_context(fields) and title_has(PRODUCT_ROLE_TERMS)
    system_counter = has(SYSTEM_BUILD_TERMS) or product_counter or title_has(("软件开发", "软件工程师", "全栈开发", "解决方案工程师", "解决方案架构师", "FDE"))
    sales_role = _has_sales_title(title)
    disclaimer = any(_has_technical_disclaimer(value) for value in raw_fields.values())
    # Agent/AI may only be the product being sold. Actual technical work,
    # unlike a bare product/tool name or negated PoC mention, can reopen it.
    technical_title = title_as_counter and title_has(("售前", "技术销售", "销售工程师", "FDE", "前沿部署", "前线部署", "方案架构师", "方案设计师", "pre-sales", "presales", "technical sales", "sales engineer", "solutions architect", "solution architect"))
    sales_work = " | ".join(value for key, value in fields.items() if key != "title")
    sales_counter = (technical_title and not disclaimer) or bool(_screen_terms(sales_work, SALES_TECH_COUNTER_TERMS))
    if re.search(r"(?:负责|参与|开展|推进|完成).{0,16}(?:PoC|Demo|交付|部署)|\b(?:own|build|implement|deliver|conduct|perform)\w*\s+(?:customer\s+)?(?:PoC|integration|deployment|technical\s+solution)", sales_work, re.I):
        sales_counter = True
    sales_kpis = _screen_terms(source, SALES_KPI_TERMS)
    pure_sales = sales_role and not sales_counter and (has(ACQUISITION_TERMS) or len(sales_kpis) >= 2)
    content = title_has(CONTENT_ROLE_TERMS) and not system_counter
    traditional_terms = ("课程顾问", "招生顾问", "课程销售", "律师", "临床监查", "财务尽调", "口腔医师", "土建工程", "施工管理")
    physical_objects = ("牙膏", "口腔护理", "炭黑", "日化产品", "服装产品")
    traditional = (title_has(traditional_terms) or (title_has(("产品经理", "销售", "顾问")) and has(physical_objects))) and not system_counter
    hardware_object = title_has((*HARDWARE_ROLE_TERMS, *HARDWARE_ARCHITECTURE_TERMS)) or (title_has(("工程", "开发", "研发", "算法")) and has(HARDWARE_OBJECT_TERMS))
    business_counter = has((*INTEGRATION_TERMS, "客户技术方案", "业务应用", "企业应用", "业务自动化", "流程自动化", "大模型应用", "AI应用"))
    hardware = hardware_object and not (business_counter or product_counter or title_has(("FDE", "前沿部署", "前线部署")))
    # 财税售前/售后咨询 may concern tax planning and accounting services.
    # Those words alone do not establish a software/technical consulting job.
    # Keep actual AI product work or enterprise-software design/integration;
    # plain AI/解决方案 nouns are not counterevidence for a service profession.
    financial_software_counter = product_counter or (
        has((*BUSINESS_APPLICATION_TERMS, *FINANCIAL_SOFTWARE_TERMS))
        and has((*INTEGRATION_TERMS, "方案设计", "产品规划", "产品设计", "软件实施", "实施交付", "PoC交付", "solution design", "product planning", "product design", "software implementation", "PoC delivery"))
    )
    financial_service = title_has(FINANCIAL_SERVICE_ROLE_TERMS) and not financial_software_counter
    nontechnical = title_has(NONTECH_ROLE_TERMS) and not (
        product_counter or title_has(("解决方案", "系统集成", "软件产品", "技术顾问", "solution architect", "solution consultant", "product manager"))
    )
    research_role = title_has(("算法", "研发", "研究", "开发", "工程师"))
    training_direction = title_has(("训练研发", "模型训练", "预训练", "训练方向", "（训练）", "(训练)"))
    research_skills = _screen_terms(source, ("强化学习", "自然语言处理算法", "多模态算法", "模型训练", "预训练", "模型并行"))
    base_research = has(BASE_MODEL_TERMS) and (title_has(("研究", "训练", "预训练")) or has(("底模训练", "基座模型训练", "基座模型研发", "基础模型预训练", "从零训练")))
    pure_algorithm = research_role and (base_research or (training_direction and len(research_skills) >= 2)) and not application_counter
    infra_role = title_has(("Infra", "基础设施", "基础架构", "底层架构", "平台架构", "后端架构", "训练与推理研发", "推理引擎"))
    infra_specialist = _screen_terms(source, INFRA_SPECIALIST_TERMS)
    explicit_infra_object = bool(re.search(
        r"(?:算子|编译器|GPU\s*内核|CUDA\s*内核).{0,12}(?:开发|研发|工程师|优化)"
        r"|(?:operator|compiler|GPU\s+kernel|CUDA\s+kernel)\s+(?:development\s+)?(?:engineer|developer)"
        r"|高性能计算.{0,8}(?:引擎|开发|研发)", title, re.I,
    ))
    pure_infra = (infra_role and len(infra_specialist) >= 2 or title_has(("训练集群调度", "GPU内核研发", "CUDA算子研发")) or explicit_infra_object) and not business_counter and not product_counter
    annotation_terms = ("标注员", "数据标注", "文本标注", "caption标注", "caption文本")
    annotation = title_has(annotation_terms) and not has(("评测", "质量标准", "项目经理", "项目管理", "数据治理", "系统集成", "Agent", "RAG"))
    marketing_terms = ("产品营销", "品牌传播", "舆情公关", "社群运营", "用户增长", "市场推广")
    marketing = title_has(marketing_terms) and not application_counter and not product_counter
    groups = [
        ("pure_sales", pure_sales, "主业为纯销售/获客，且无技术方案反向证据", (*SALES_ROLE_TERMS, *ACQUISITION_TERMS, *SALES_KPI_TERMS)),
        ("irrelevant", content, "主业为主播/内容美术制作，并非应用系统建设", CONTENT_ROLE_TERMS),
        ("irrelevant", traditional, "主业与对象明确属于传统非软件岗位", (*traditional_terms, *physical_objects)),
        ("irrelevant", hardware, "主业与对象明确属于硬件/工艺/专用控制", (*HARDWARE_ROLE_TERMS, *HARDWARE_ARCHITECTURE_TERMS, *HARDWARE_OBJECT_TERMS)),
        ("irrelevant", nontechnical, "主业为财务/招聘/人事等非技术职能，技术名词仅为服务领域", NONTECH_ROLE_TERMS),
        ("irrelevant", financial_service, "主业为财税/会计专业咨询服务，未见AI产品或企业软件建设职责", (*FINANCIAL_SERVICE_ROLE_TERMS, *FINANCIAL_SERVICE_OBJECT_TERMS)),
        ("pure_algorithm", pure_algorithm, "主方向明确为底模/预训练研究，而非应用工程", (*BASE_MODEL_TERMS, "算法", "研究", "训练", *research_skills)),
        ("pure_infrastructure", pure_infra, "主业与专项技能明确指向底层基础设施", ("Infra", "基础设施", "基础架构", "底层架构", "平台架构", "后端架构", *INFRA_SPECIALIST_TERMS, "operator", "compiler", "GPU kernel", "CUDA kernel")),
        ("irrelevant", annotation, "主业为基础标注执行，未见评测/质量/工程闭环", annotation_terms),
        ("irrelevant", marketing, "主业为营销/公关/增长，未见技术建设证据", marketing_terms),
    ]
    reasons, evidence = [], []
    flags = {name: False for name in ("pure_sales", "pure_algorithm", "pure_infrastructure", "irrelevant")}
    for category, present, reason, terms in groups:
        if present:
            flags[category] = True
            reasons.append(reason)
            evidence.extend(_screen_evidence(fields, terms, reason))
    return {**flags, "reasons": reasons, "evidence": evidence, "application_counter": application_counter, "sales_counter": sales_counter, "system_counter": system_counter}


def score_job(job: dict[str, Any]) -> dict[str, Any]:
    """Assign one list queue from positive title/skill clues, never JD duties."""

    raw_fields = {key: " | ".join(text(value).strip() for value in _field_values(job, key))
                  for key in ("title", "skills", "skill_tags", "job_labels") if _field_values(job, key)}
    fields = {key: _responsibility_text(value) for key, value in raw_fields.items()}
    title = fields.get("title", "")
    source = " | ".join(fields.values())
    has = lambda terms: bool(_screen_terms(source, terms))
    title_has = lambda terms: bool(_screen_terms(title, terms))
    score, hits = 0, []
    for term, weight in RELEVANT_TERMS:
        if has((term,)):
            score += weight
            hits.append(term)
    if _screen_terms(title, ("架构", "解决方案", "FDE", "交付", "实施", "顾问", "智能体")):
        score += 4
        hits.append("岗位头衔方向加权")
    ai = _has_ai_context(fields)
    families, evidence, priority_families = [], [], []

    def add_family(name: str, present: bool, terms: Iterable[str], priority: bool = False) -> None:
        if present:
            families.append(name)
            evidence.extend(_screen_evidence(fields, terms, name))
            if priority:
                priority_families.append(name)

    application_roles = ("应用工程", "应用开发", "应用研发", "应用技术", "应用经理", "应用主管", "应用专员", "全栈", "原生软件", "软件开发", "软件工程师", "应用架构", "application engineer", "application architect")
    enablement_roles = ("业务赋能", "业务自动化", "流程自动化", "应用落地", "落地顾问", "应用顾问", "赋能顾问", "业务应用", "业务运营", "业务创新", "自动化运营", "智能办公", "提效专家", "提效顾问", "效能顾问", "BI平台建设", "adoption consultant", "enablement consultant", "transformation consultant", "productivity consultant", "productivity specialist")
    engineering_terms = ("大模型工程化", "模型工程化", "SDK", "Framework", "框架开发", "框架工程", "LLMOps")
    technical_roles = ("开发", "研发", "工程", "架构", "产品", "方案", "交付", "实施", "业务", "技术", "顾问")
    solution_roles = ("FDE", "前沿部署", "前线部署", "部署工程师", "客户技术方案", "解决方案", "方案设计", "售前", "技术咨询", "技术销售", "销售工程师", "实施工程师", "交付工程师", "solution architect", "solutions architect", "solution engineer", "solutions engineer", "solution consultant", "solutions consultant", "pre-sales", "presales", "technical consultant", "technical sales", "sales engineer", "implementation engineer", "delivery engineer", "forward deployed engineer")
    mixed = mixed_duty_evidence(fields)
    business_application = has(BUSINESS_APPLICATION_TERMS)
    architecture_title = title_has(ARCHITECTURE_ROLE_TERMS)
    architecture_duty = any(re.search(r"架构|architect", item["quote"], re.I) for item in mixed)
    add_family("AI/应用/业务架构", architecture_title or architecture_duty,
               (*ARCHITECTURE_ROLE_TERMS, "总体架构", "整体架构", "架构设计"),
               (architecture_title and ai) or (architecture_title and business_application))
    add_family("AI产品/孵化", ai and has(PRODUCT_ROLE_TERMS), (*AI_CONTEXT_TERMS, *PRODUCT_ROLE_TERMS), True)
    add_family("业务软件产品/架构", title_has(PRODUCT_ROLE_TERMS) and business_application,
               (*PRODUCT_ROLE_TERMS, *BUSINESS_APPLICATION_TERMS), True)
    add_family("AI售前/业务分析", ai and has(("售前", "业务分析", "技术咨询")), (*AI_CONTEXT_TERMS, "售前", "业务分析", "技术咨询"), True)
    add_family("FDE/部署/客户技术方案", has(solution_roles), solution_roles,
               has(("FDE", "前沿部署", "前线部署", "forward deployed engineer"))
               or ((ai or business_application) and title_has(solution_roles)))
    add_family("AI业务赋能/自动化/运营落地", ai and has(enablement_roles), (*AI_CONTEXT_TERMS, *enablement_roles), True)
    knowledge_roles = ("知识管理专家", "知识管理经理", "知识管理顾问", "knowledge management specialist", "knowledge management expert", "knowledge management manager", "knowledge manager")
    add_family("知识管理/AI知识运营", has(knowledge_roles), knowledge_roles, ai)
    add_family("系统/业务集成过渡", has(INTEGRATION_TERMS), INTEGRATION_TERMS, ai or business_application)
    customer_success_roles = ("客户成功", "customer success")
    add_family("客户成功/应用交付过渡", title_has(customer_success_roles),
               (*customer_success_roles, "SaaS", "软件", "交付", "实施"),
               (ai or business_application) and has((*INTEGRATION_TERMS, "技术方案", "PoC交付", "实施交付")))
    add_family("AI应用/全栈", ai and (has(application_roles) or has(("AI应用", "AI 应用", "人工智能应用", "大模型应用"))), (*AI_CONTEXT_TERMS, *application_roles, "AI应用", "AI 应用", "人工智能应用", "大模型应用"), True)
    # 客户开发/渠道开发 is acquisition, not software development. A bare
    # Agent product sold by a salesperson supplies no personal technical duty.
    add_family("Agent/RAG/Workflow应用", has(AGENT_TOOL_TERMS) and (title_has(technical_roles) or bool(mixed)), (*AGENT_TOOL_TERMS, *technical_roles), True)
    add_family("大模型工程化/SDK/框架", ai and has(engineering_terms), (*AI_CONTEXT_TERMS, *engineering_terms), True)
    add_family("方案/架构/集成交付混合职责", bool(mixed), (item["quote"] for item in mixed))
    quality_terms = ("评测", "提示词", "训练师", "AI测试", "AI 测试", "人工智能测试", "模型测试", "Prompt Engineer")
    add_family("评测/提示词/AI测试过渡", has(quality_terms), quality_terms)
    software_terms = ("软件开发", "软件工程", "全栈", "后端开发", "前端开发", "开发工程师", "研发工程师", "开发岗位", "研发岗位", "系统工程师", "产品应用工程师", "现场应用工程师", "FAE", "软件测试", "测试工程师", "应用软件", "应用层软件", "software engineer", "software developer", "system engineer")
    add_family("通用软件/系统工程过渡", has(software_terms), software_terms)
    product_terms = (*PRODUCT_ROLE_TERMS, "产品管培", "产品设计", "项目经理", "项目管培", "项目管理", "版本经理", "需求管理", "需求分析师", "B端交互", "用户研究", "Product Designer")
    ambiguous_pm = bool(_screen_terms(title, ("PM",))) and has(("游戏", "软件", "产品", "项目"))
    add_family("通用产品/项目/体验过渡", has(product_terms) or ambiguous_pm, (*product_terms, *(("PM", "游戏", "软件", "产品", "项目") if ambiguous_pm else ())))
    data_terms = ("数据分析", "数据开发", "数据工程", "数据仓库", "ETL", "数据BP", "BI开发", "数据治理", "数据采集")
    add_family("数据链路过渡", has(data_terms), data_terms)
    uncertain_terms = ("算法", "NLP", "计算机视觉", "Infra", "SRE", "基础设施", "基础架构", "底层架构", "平台架构", "后端架构", "平台工程师", "模型训练", "推理服务", "云原生", "K8S", "Kubernetes", "算子", "编译器", "高性能计算", *BASE_MODEL_TERMS)
    add_family("算法/基础设施方向待核", has(uncertain_terms), uncertain_terms)
    adjacent_roles = ("工程师", "专员", "优化师", "培训师", "教练", "运营", "商务", "销售", "业务拓展", "客户经理", "技术支持", "管培生", "engineer", "specialist", "consultant", "operations", "sales")
    add_family("AI相关相邻职能待核", ai and has(adjacent_roles), (*AI_CONTEXT_TERMS, *adjacent_roles))
    add_family("自动化/RPA过渡", has(("RPA", "流程自动化", "业务自动化")), ("RPA", "流程自动化", "业务自动化"))
    exclusions = _semantic_role_exclusions(title, raw_fields)
    development = detect_pure_development(fields)
    if development["excluded"]:
        exclusions["reasons"].extend(development["reasons"])
        exclusions["evidence"].extend(development["evidence"])
    conflict_notes = _source_conflict_notes(job)
    if conflict_notes:
        # A positive value from another source must not erase an explicit role
        # exclusion supported by one original title/skills observation.
        projections: dict[str, dict[str, str]] = {}
        for field in ("title", "skills", "skill_tags", "job_labels"):
            for entry in (job.get("field_candidates") or {}).get(field, []):
                for context in entry.get("sources", []):
                    projections.setdefault(_stable_value(context), {})[field] = text(entry["value"])
        for source_fields in projections.values():
            source_exclusions = _semantic_role_exclusions(source_fields.get("title", ""), source_fields)
            source_development = detect_pure_development(source_fields)
            exclusions["reasons"].extend(source_exclusions["reasons"] + source_development["reasons"])
            exclusions["evidence"].extend(source_exclusions["evidence"] + source_development["evidence"])
        exclusions["reasons"] = list(dict.fromkeys(exclusions["reasons"]))
    salaries = [parse_salary(value) for value in _field_values(job, "salary")]
    salary_below_target = bool(salaries) and all(value["upper_k"] is not None and value["upper_k"] < SALARY_TARGET_K for value in salaries)
    if salary_below_target:
        exclusions["reasons"].append(SALARY_CEILING_REASON)
        exclusions["evidence"].append({"family": SALARY_CEILING_REASON, "field": "salary", "quote": " / ".join(text(value) for value in _field_values(job, "salary"))})
    list_pass = bool(families) and not exclusions["reasons"]
    # Strong title evidence can be relevant even with no skill tags. Only
    # substantive role/object ambiguity demotes it; neither queue is JD proof.
    actual_technical_work = has(SALES_TECH_COUNTER_TERMS)
    explicit_application_title = title_has(("大模型应用", "AI应用", "AI 应用", "人工智能应用", "智能应用", "业务应用", "应用架构", "FDE", "前沿部署", "前线部署", "forward deployed engineer"))
    weak_specialist = title_has(("算法", "Infra", "基础设施", "基础架构", "底层架构", "平台架构", "后端架构", "模型训练", "推理", "算子", "编译器")) and not (actual_technical_work or explicit_application_title)
    unclear_domain = has((*HARDWARE_OBJECT_TERMS, "芯片", "半导体", "整车", "硬件", "数据中心", "IDC", "CUDA", "算子", "编译器", "基础设施", "底层架构", "平台架构")) and not has((*INTEGRATION_TERMS, "客户技术方案", "方案设计", "业务场景", "业务流程", "业务自动化", "企业知识库"))
    unclear_sales = _has_sales_title(title) and not exclusions["sales_counter"]
    ambiguity = list(conflict_notes)
    if unclear_sales:
        ambiguity.append("销售/商务标题尚无本人技术方案、集成或交付证据")
    if weak_specialist:
        ambiguity.append("算法/基础设施仅为相邻方向，应用与底层研发边界待核实")
    if unclear_domain:
        ambiguity.append("行业/平台工作对象存在歧义，需核对业务应用与底层/硬件职责")
    if not list_pass:
        queue = "excluded"
        queue_reason = "；".join(exclusions["reasons"]) or "标题与技能未见目标方向的可核对线索"
    elif priority_families and not ambiguity:
        queue = "relevant"
        queue_reason = "列表明确命中" + "、".join(priority_families) + "；仍需完整JD核实职责与要求"
    else:
        queue = "verify"
        queue_reason = "；".join(ambiguity) or "仅通用/过渡方向线索，应用对象与本人职责仍待完整JD核实"
    verification = []
    if not any(fields.get(key) for key in ("skills", "skill_tags", "job_labels")):
        verification.append("技能字段缺失，不能据此判断无相关能力，需补JD核实")
    if has(("算法", "模型训练", "训练师", "NLP", "计算机视觉")):
        verification.append("核实应用研发/效果迭代与底模训练研究的主业边界")
    if has(("Infra", "基础架构", "基础设施", "K8S", "Kubernetes", "云原生")):
        verification.append("核实应用部署/模型服务与底层基础设施的主业边界")
    if has((*SALES_ROLE_TERMS, "营销")):
        verification.append("核实技术方案/交付与销售获客/KPI的责任占比")
    if has(("高级", "资深", "专家", "主管")):
        verification.append("核实实际职级和经验门槛，不按资深头衔排除")
    if has(("校招", "校园招聘", "应届", "实习", "届")):
        verification.append("核实毕业届别、在校/实习及留任资格")
    if ambiguous_pm:
        verification.append("核实PM指产品/项目管理还是其他职能，以及实际软件项目责任")
    if queue == "verify":
        verification.append("低优先过渡/方向待核候选，不据此认定AI相关或已经胜任")
        verification.extend(ambiguity)
    if list_pass:
        verification.append("列表仅用于补JD召回，六项职责及岗位硬要求仍待核实")
    return {
        "score": score,
        "hit_terms": hits,
        "matched_direction_terms": hits,
        "excluded_terms": _screen_terms(source, REPORTED_EXCLUDED_TERMS),
        "list_excluded": bool(exclusions["reasons"]),
        "list_pure_development": development["excluded"],
        "list_salary_below_target": salary_below_target,
        "list_field_conflicts": [field for field in _SOURCE_FIELDS if len(_field_values(job, field)) > 1],
        "list_exclusion_reasons": exclusions["reasons"],
        "screening_version": SCREENING_VERSION,
        "list_pass": list_pass,
        "list_queue": queue,
        "list_queue_reason": queue_reason,
        "list_priority": {"relevant": "优先补JD", "verify": "低优先待JD", "excluded": "暂不优先"}[queue],
        "list_families": families,
        "list_match_evidence": evidence,
        "list_exclusion_evidence": exclusions["evidence"],
        "list_verification": verification,
    }


def evidence_snippets(jd: str, terms: Iterable[str], limit: int = 3) -> list[str]:
    lines = [line.strip() for line in re.split(r"[\r\n]+", jd) if line.strip()]
    result = []
    for line in lines:
        if any(contains(line, term) for term in terms):
            result.append(line[:300])
            if len(result) >= limit:
                break
    return result


def parse_jd_experience(jd: str) -> dict[str, Any]:
    """Extract explicit work-experience floors, not unrelated year amounts.

    A range contributes its lower end. Multiple mandatory requirements must
    all be met, so the largest of their lower bounds is the effective floor.
    Preferences and upper-bound-only statements are not hard minimums.
    """

    year_pattern = re.compile(
        r"(?<![\d.])(?P<lower>\d{1,2}(?:\.\d+)?)\s*"
        r"(?:(?:年\s*)?[-~～至到—–]\s*(?P<upper>\d{1,2}(?:\.\d+)?)\s*)?年"
    )
    requirements: list[tuple[int | float, str]] = []
    optional_section = False
    for line in jd.splitlines():
        if re.match(r"\s*(?:加分项|优先条件|优先资格|加分条件)", line):
            optional_section = True
        elif re.match(r"\s*(?:任职要求|任职资格|职位要求|岗位要求|必备条件|基本要求|硬性要求|岗位职责|工作职责)", line):
            optional_section = False
        if optional_section:
            continue
        for clause in re.split(r"[，,。；;！？!?]", line):
            if re.search(r"优先(?!级)|加分|更佳|尤佳|为佳", clause):
                continue
            for match in year_pattern.finditer(clause):
                before, after = clause[:match.start()], clause[match.end():]
                # Company history, compensation and contract durations do not
                # describe the applicant's work experience, even nearby.
                if re.search(r"成立|创立|创办|注册|司龄|质保|保修|合同|服务期|项目周期|交付周期|年薪|薪酬|薪资|工资|营收|收入", before[-24:]):
                    continue
                if not match.group("upper") and (
                    re.match(r"\s*(?:及)?(?:以内|以下|内)", after)
                    or re.search(r"(?:不超过|不多于|最多|(?<!不)少于|不满)\s*$", before)
                ):
                    continue
                experience_after = re.search(r"经验|经历", after[:60])
                experience_before = re.search(r"(?:经验|经历|年限)[^，,。；;\r\n]{0,12}$", before)
                if not experience_after and not experience_before:
                    continue
                lower = float(match.group("lower"))
                upper = float(match.group("upper")) if match.group("upper") else lower
                if not 0 <= lower <= upper <= 60:
                    continue
                years = int(lower) if lower.is_integer() else lower
                requirements.append((years, clause.strip()))
    return {
        "minimum_years": max((years for years, _ in requirements), default=None),
        "evidence": list(dict.fromkeys(evidence for _, evidence in requirements)),
    }


def rule_analyze_jd(jd: str, title: str = "") -> dict[str, Any]:
    sections = extract_jd_sections(jd)
    responsibility_jd = sections.get("responsibilities", "")
    dimensions = {}
    for name, terms in DIMENSION_TERMS.items():
        matched = [term for term in terms if contains(responsibility_jd, term)]
        dimensions[name] = {"present": bool(matched) if responsibility_jd.strip() else "unknown", "matched_terms": matched, "evidence": evidence_snippets(responsibility_jd, matched)}
    low = jd.lower()
    sales = list(dict.fromkeys([term for term in SALES_KPI_TERMS + SALES_ROLE_TERMS if term.lower() in low]))
    algorithm = list(dict.fromkeys([term for term in ALGORITHM_TERMS if term.lower() in low]))
    infra = list(dict.fromkeys([term for term in INFRA_TERMS if term.lower() in low]))
    count = sum(1 for item in dimensions.values() if item["present"] is True)
    role = _semantic_role_exclusions(title, {"jd": responsibility_jd}, title_as_counter=False) if jd.strip() else {}
    experience = parse_jd_experience(jd)
    return {"dimensions": dimensions, "dimension_names": [x for x, value in dimensions.items() if value["present"] is True], "dimension_count": count, "responsibilities": responsibility_jd, "requirements": sections.get("requirements", ""), "sales_terms": sales, "algorithm_terms": algorithm, "infrastructure_terms": infra, "pure_sales": role.get("pure_sales", False), "pure_algorithm": role.get("pure_algorithm", False), "pure_infrastructure": role.get("pure_infrastructure", False), "irrelevant": role.get("irrelevant", False), "exclusion_reasons": role.get("reasons", []), "exclusion_evidence": role.get("evidence", []), "application_counter": role.get("application_counter", False), "sales_counter": role.get("sales_counter", False), "system_counter": role.get("system_counter", False), "minimum_years": experience["minimum_years"], "experience_evidence": experience["evidence"]}


def extract_experience_labels(job: dict[str, Any]) -> list[str]:
    values = []
    for key in ("tags", "job_labels"):
        values.extend(re.findall(r"(?:经验)?\s*(?:不限|应届生?|在校|无经验|\d+\s*[-至~～]\s*\d+\s*年|\d+\s*年以上|\d+年以内)", text(job.get(key))))
    for source in job.get("observed_in", []):
        values.extend([text(source.get("experience_id")), text(source.get("experience_filter"))])
    return list(dict.fromkeys(re.sub(r"\s+", "", x) for x in values if x))


def detail_key(item: dict[str, Any]) -> str:
    return text(item.get("job_id") or item.get("encrypt_job_id") or item.get("dedupe_key") or item.get("job_link") or item.get("link"))


def load_details(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    value = read_json(path)
    records = value if isinstance(value, list) else value.get("jobs") or value.get("details") or value.get("records") or [] if isinstance(value, dict) else []
    return merge_detail_records({}, (item for item in records if isinstance(item, dict)))


def find_detail(job: dict[str, Any], details: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for key in (job.get("job_id"), job.get("encrypt_job_id"), job.get("job_link"), job.get("dedupe_key")):
        if key and text(key) in details:
            return details[text(key)]
    return None


def shortlist_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for job in jobs:
        info = score_job(job)
        job.update(info)
        if info["list_pass"]:
            result.append(job)
    return result


def write_indexes(output_root: Path, jobs: list[dict[str, Any]], metadata: dict[str, Any], raw_rows: list[dict[str, Any]]) -> None:
    prepared = output_root / "prepared"
    write_json(output_root / "raw_sample.json", {"metadata": metadata, "jobs": raw_rows})
    candidates = shortlist_jobs(jobs)
    queue_counter = Counter(job["list_queue"] for job in jobs)
    queue_counts = {queue: queue_counter[queue] for queue in LIST_QUEUE_LABELS}
    metadata.update({"screening_version": SCREENING_VERSION, "list_pass_count": len(candidates), "list_priority_counts": dict(Counter(job["list_priority"] for job in jobs)), "list_queue_counts": queue_counts})
    current = [job for job in jobs if "current" in {x.get("track") for x in job.get("observed_in", [])}]
    target = [job for job in jobs if "target" in {x.get("track") for x in job.get("observed_in", [])}]
    write_json(prepared / "merged_jobs_index.json", {"metadata": metadata, "jobs": jobs})
    write_json(prepared / "current_candidates.json", {"total": len(current), "jobs": current})
    write_json(prepared / "target_candidates.json", {"total": len(target), "jobs": target})
    shortlist_metadata = {"screening_version": SCREENING_VERSION, "list_queue_counts": queue_counts}
    write_json(prepared / "jd_shortlists" / "jd_input.json", {**shortlist_metadata, "total": len(candidates), "jobs": candidates})
    for queue in ("relevant", "verify"):
        queued = [job for job in candidates if job["list_queue"] == queue]
        write_json(prepared / "jd_shortlists" / f"jd_{queue}.json",
                   {**shortlist_metadata, "list_queue": queue, "total": len(queued), "jobs": queued})


def merge_stage(
    source_root: Path,
    output_root: Path,
    config_path: Path,
    city: str | None,
    sample_size: int | None,
    sample_strategy: str = "sequential",
    tracks: str = "all",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = load_config(config_path)
    rows, sample_meta = collect_rows(source_root, config, city, sample_size, sample_strategy, tracks)
    if sample_strategy == "balanced-dedup":
        jobs = rows
        sampled_raw_count = sum(len(job.get("observed_in") or []) for job in jobs)
        merge_meta = {
            "raw_list_rows": sampled_raw_count,
            "unique_job_count": len(jobs),
            "dedupe_removed": max(0, sampled_raw_count - len(jobs)),
        }
    else:
        jobs, merge_meta = merge_rows(rows)
    metadata = {**sample_meta, **merge_meta, "generated_at": now_iso(), "source_root": str(source_root.resolve())}
    write_indexes(output_root, jobs, metadata, rows)
    return jobs, metadata


def resolve_python(scraper_root: Path, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    candidate = scraper_root / ".venv" / "Scripts" / "python.exe"
    return candidate if candidate.exists() else Path(sys.executable)


_DETAIL_GATE_RE = re.compile(
    r"security_check|安全验证|人工验证|登录状态异常|验证码|detail login expired|登录查看完整内容",
    re.I,
)
_DETAIL_LOG_SECRET_RE = re.compile(
    r"(?ix)(?P<prefix>"
    r"\b(?:authorization|proxy-authorization)\s*:\s*bearer\s+"
    r"|\bbearer\s+"
    r"|\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|password|passwd|secret|token)\b[\"']?\s*[:=]\s*[\"']?"
    r")(?P<secret>[^\"'\s,;}]+)"
)
_DETAIL_LOG_COOKIE_RE = re.compile(
    r"(?im)(?P<prefix>\b(?:cookie|set-cookie)\s*:\s*)[^\r\n]+"
)


def redact_detail_log(text_value: str) -> str:
    """Keep upstream diagnostics useful without persisting credential values."""

    def replace(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}[REDACTED]"

    redacted = _DETAIL_LOG_SECRET_RE.sub(replace, text_value)
    return _DETAIL_LOG_COOKIE_RE.sub(replace, redacted)


def local_cdp_ready(port: int = 9222, timeout: float = 2.0) -> bool:
    """Check the local Chrome DevTools endpoint without using configured proxies."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return isinstance(payload, dict) and bool(payload.get("webSocketDebuggerUrl"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError):
        return False


def subprocess_env_with_local_bypass() -> dict[str, str]:
    """Preserve proxy settings while excluding loopback CDP traffic from them."""
    env = os.environ.copy()
    for key in ("NO_PROXY", "no_proxy"):
        entries = [entry.strip() for entry in env.get(key, "").split(",") if entry.strip()]
        for host in ("127.0.0.1", "localhost"):
            if host not in entries:
                entries.append(host)
        env[key] = ",".join(entries)
    return env


def _default_browser_profile(browser: str) -> Path:
    """Resolve an isolated profile path even in stripped test environments."""
    return browser_setup.default_profile_dir(browser)


def ensure_detail_cdp_ready(
    entry: Path,
    python: Path,
    port: int = 9222,
    *,
    browser: str = "chrome",
    profile_dir: Path | str | None = None,
) -> tuple[bool, str | None]:
    """Open our isolated browser and use only supported upstream check flags."""
    env = subprocess_env_with_local_bypass()
    browser = str(browser).strip().lower()
    if browser not in {"chrome", "edge"}:
        raise ValueError("浏览器必须为 chrome 或 edge")
    label = "Chrome" if browser == "chrome" else "Edge"
    check_command = [str(python), "-u", str(entry), "--check", "--cdp-port", str(port)]

    def check_login() -> tuple[bool, str]:
        try:
            check = subprocess.run(
                check_command, capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=False, env=env,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return False, "BOSS 登录检查超时。"
        except OSError as error:
            return False, f"无法运行 BOSS 登录检查：{redact_detail_log(str(error))}"
        if check.returncode == 0 and local_cdp_ready(port):
            return True, ""
        diagnostic = redact_detail_log((check.stdout or "") + (check.stderr or ""))
        if diagnostic.strip():
            print(diagnostic, flush=True)
        return False, f"BOSS 登录或环境检查未通过（退出码 {check.returncode}）。"

    was_running = local_cdp_ready(port)
    if was_running:
        ready, error = check_login()
        if ready:
            print(f"专用 {label}/CDP 与 BOSS 登录态检查通过。", flush=True)
            return True, None
    try:
        browser_setup.open_dedicated_browser(browser, port, profile_dir, show_window=True)
    except (browser_setup.BrowserPreparationError, ValueError) as error:
        return False, redact_detail_log(str(error))
    # A running session that failed validation needs human action. Opening a
    # visible BOSS window is enough; do not repeatedly probe a security gate.
    if not was_running:
        ready, error = check_login()
        if ready:
            print(f"专用 {label}/CDP 已就绪，继续抓取 JD。", flush=True)
            return True, None
    return False, (f"专用 {label} 已打开并连接 CDP {port}，{error}"
                   f"请在该专用 {label} 中登录或完成人工验证，再继续本步；已抓 JD 保留。")


def run_detail_subprocess(
    command: list[str],
    *,
    env: dict[str, str],
    on_line: Any = None,
) -> subprocess.CompletedProcess[str]:
    """Run the detail scraper while teeing its live progress to the terminal."""
    output: list[str] = []
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    try:
        if process.stdout is not None:
            for line in process.stdout:
                output.append(line)
                sys.stdout.write(redact_detail_log(line))
                sys.stdout.flush()
                if on_line:
                    on_line(line)
        return_code = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
    return subprocess.CompletedProcess(command, return_code, "".join(output), "")


def detail_records_from_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        value = read_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    records = value if isinstance(value, list) else value.get("jobs") or value.get("details") or value.get("records") or [] if isinstance(value, dict) else []
    return [item for item in records if isinstance(item, dict) and detail_key(item)]


def browser_detail_checkpoint_status(output_path: Path, expected: set[str]) -> dict[str, dict[str, Any]]:
    """Summarize dual-browser side checkpoints without reading browser state."""

    result: dict[str, dict[str, Any]] = {}
    for browser in ("chrome", "edge"):
        path = output_path.with_name(f"{output_path.stem}.{browser}{output_path.suffix}")
        records = detail_records_from_file(path)
        owned = [record for record in records if detail_key(record) in expected]
        owned_keys = {detail_key(record) for record in owned}
        complete = {detail_key(record) for record in owned
                    if detail_completeness(record) == "complete"}
        paused = any(record.get("status") == "paused" or record.get("security_or_login_gate") for record in owned)
        # Each side owns only its half of the batch. Compare against that
        # side's checkpoint keys rather than the full batch, otherwise both
        # browsers would remain incorrectly labelled "partial" forever.
        status = "paused" if paused else "completed" if complete == owned_keys and owned_keys else "partial" if owned else "pending"
        result[browser] = {"status": status, "count": len(owned),
                           "completed_count": len(complete), "job_ids": sorted(detail_key(record) for record in owned)}
    return result


def merge_detail_records(
    details: dict[str, dict[str, Any]], records: Iterable[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    for item in records:
        key = detail_key(item)
        if key and (key not in details or _detail_quality(item) > _detail_quality(details[key])):
            details[key] = item
    return details


def wait_for_detail_gate_retry(wait_on_gate: bool, allow_non_tty: bool = False) -> bool:
    """Return whether the paused batch should be retried after manual verification."""

    message = (
        "检测到登录或安全验证。请在专用 Chrome 中完成人工验证（不会自动处理验证码）；"
        "完成后按 Enter 重试当前批次，输入 q 退出。"
    )
    if not wait_on_gate:
        print(f"{message} 当前已禁用等待，保留 paused 并返回。", flush=True)
        return False

    stdin = getattr(sys, "stdin", None)
    is_interactive = bool(stdin is not None and getattr(stdin, "isatty", lambda: False)())
    if not is_interactive and not allow_non_tty:
        print(f"{message} 当前为非交互模式，保留 paused 并返回。", flush=True)
        return False
    try:
        answer = input(f"{message} ")
    except (EOFError, OSError, KeyboardInterrupt):
        print("未收到重试确认，保留 paused 并安全返回。", flush=True)
        return False
    normalized = answer.strip().lower()
    if normalized == "q":
        return False
    return normalized == ""


def fetch_jd(
    output_root: Path,
    scraper_root: Path,
    batch_size: int = 10,
    python_path: str | None = None,
    wait_on_gate: bool = True,
    detail_speed_profile: str = "fast",
    gui_interactive: bool = False,
    jd_workers: int = JD_DEFAULT_WORKERS,
    browser_mode: str = "single",
    chrome_port: int = 9222,
    edge_port: int = 9223,
    chrome_profile_dir: Path | str | None = None,
    edge_profile_dir: Path | str | None = None,
) -> dict[str, Any]:
    if type(jd_workers) is not int or not 1 <= jd_workers <= MAX_WORKERS:
        raise ValueError("JD 页面并发数须为 1–32。")
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError("JD 每批数量须为 1–100。")
    if browser_mode not in {"single", "dual"}:
        raise ValueError("browser_mode 必须为 single 或 dual。")
    for name, port in (("Chrome", chrome_port), ("Edge", edge_port)):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f"{name} CDP 端口必须为 1–65535 的整数。")
    # Our launcher owns browser/profile flags; upstream only receives its CDP
    # port for login checks. This module never reads cookies.
    chrome_profile_dir = chrome_profile_dir or _default_browser_profile("chrome")
    edge_profile_dir = edge_profile_dir or _default_browser_profile("edge")
    if browser_mode == "dual":
        if chrome_port == edge_port:
            raise ValueError("双浏览器必须使用独立 CDP 端口。")
        if os.path.normcase(os.path.abspath(chrome_profile_dir)) == os.path.normcase(os.path.abspath(edge_profile_dir)):
            raise ValueError("双浏览器必须使用独立 profile 目录。")
    shortlist_path = output_root / "prepared" / "jd_shortlists" / "jd_input.json"
    if not shortlist_path.exists():
        raise FileNotFoundError(f"找不到 JD 初筛输入：{shortlist_path}")
    jobs = (read_json(shortlist_path) or {}).get("jobs", [])
    batch_dir = output_root / "prepared" / "jd_shortlists" / "detail_batches"
    details_path = output_root / "prepared" / "jd_shortlists" / "details.json"
    manifest_path = output_root / "prepared" / "jd_shortlists" / "details_manifest.json"
    batch_dir.mkdir(parents=True, exist_ok=True)
    manifest = read_json(manifest_path) if manifest_path.exists() else {"schema_version": "1.0", "records": []}
    details = load_details(details_path)
    python = resolve_python(scraper_root, python_path)
    entry = scraper_root / "scripts" / "boss_cdp_raw.py"
    if not entry.exists():
        raise FileNotFoundError(f"找不到上游采集器：{entry}")
    batch_count = (len(jobs) + batch_size - 1) // batch_size
    job_keys = {detail_key(job) for job in jobs if detail_key(job)}
    expected_batches = {
        number: {
            detail_key(job)
            for job in jobs[(number - 1) * batch_size:number * batch_size]
            if detail_key(job)
        }
        for number in range(1, batch_count + 1)
    }
    # Manifest state belongs to the current input grouping, unlike reusable JD
    # checkpoints. Old batch numbers or memberships must not leave stale gates.
    current_records = {}
    for record in manifest.get("records", []):
        number = record.get("batch")
        if number in expected_batches and set(record.get("job_ids") or []) == expected_batches[number]:
            current_records[number] = record
    manifest["records"] = [current_records[number] for number in sorted(current_records)]
    manifest.update({"total_jobs": len(jobs), "batch_size": batch_size, "jd_workers": jd_workers,
                     "scroll_count": JD_SCROLL_COUNT, "browser_mode": browser_mode,
                     "cdp_ports": {"chrome": chrome_port, "edge": edge_port},
                     "profile_dirs": {"chrome": str(chrome_profile_dir) if chrome_profile_dir else None,
                                      "edge": str(edge_profile_dir) if edge_profile_dir else None},
                     "updated_at": now_iso()})
    write_json(manifest_path, manifest)

    # Recover per-batch checkpoints before scheduling work. The upstream scraper
    # writes after every completed JD, so an interrupted batch can resume without
    # downloading those descriptions again.
    # A new batch size/input order may leave complete records in an old batch
    # number or only in a browser sidecar. Recover all of them before any
    # current batch input or checkpoint is rewritten.
    for checkpoint in sorted(batch_dir.glob("details_*.json")):
        merge_detail_records(details, detail_records_from_file(checkpoint))
    if details or details_path.exists():
        write_json(details_path, sorted(details.values(), key=lambda x: detail_key(x)))

    def is_complete(key: str) -> bool:
        return detail_completeness(details.get(key)) == "complete"

    completed_total = sum(1 for key in job_keys if is_complete(key))
    pending_total = len(job_keys) - completed_total
    print(
        f"JD 详情进度：候选 {len(job_keys)} 条，已完成 {completed_total} 条，待抓 {pending_total} 条；"
        f"批次 {batch_count} 个，页面并发 {jd_workers}，每页滚动 {JD_SCROLL_COUNT} 次，速度档位 {detail_speed_profile}。",
        flush=True,
    )
    def check_browser_ready(browser: str, port: int, profile: Path | str | None) -> tuple[bool, str | None]:
        """Call readiness probes while retaining compatibility with old test hooks."""

        try:
            return ensure_detail_cdp_ready(
                entry, python, port, browser=browser, profile_dir=profile,
            )
        except TypeError as error:
            # Older injected probes accepted only (entry, python, port). Only
            # retry for that exact signature mismatch; real probe errors pass
            # through unchanged.
            if "unexpected keyword argument" not in str(error):
                raise
            return ensure_detail_cdp_ready(entry, python, port)

    browser_readiness = {name: {"ready": True, "error": None} for name in ("chrome", "edge")}
    if pending_total == 0:
        cdp_ready, cdp_error = True, None
    elif browser_mode == "dual":
        readiness = {}
        for browser, port, profile in (("chrome", chrome_port, chrome_profile_dir),
                                       ("edge", edge_port, edge_profile_dir)):
            if browser == "edge" and pending_total == 1:
                readiness[browser] = (False, None)
                continue
            try:
                readiness[browser] = check_browser_ready(browser, port, profile)
            except Exception as exc:
                readiness[browser] = (False, f"{browser} CDP 检查失败：{type(exc).__name__}")
        ready = [name for name, (ok, _error) in readiness.items() if ok]
        errors = [f"{name}: {error}" for name, (ok, error) in readiness.items() if not ok and error]
        # A healthy side can continue while the other side is paused/failed;
        # the adapter keeps side checkpoints separate for a later resume.
        cdp_ready = bool(ready)
        cdp_error = "；".join(errors) if errors else None
        browser_readiness = {
            name: {"ready": ok, "error": error} for name, (ok, error) in readiness.items()
        }
        manifest["browser_readiness"] = browser_readiness
        write_json(manifest_path, manifest)
    else:
        cdp_ready, cdp_error = check_browser_ready("chrome", chrome_port, chrome_profile_dir)
    if not cdp_ready:
        print(
            f"{cdp_error} JD 批次已暂停；处理完成后可继续本步。",
            flush=True,
        )
    for number in range(1, batch_count + 1):
        old = next((x for x in manifest.get("records", []) if x.get("batch") == number), None)
        start = (number - 1) * batch_size
        batch = jobs[start:start + batch_size]
        expected = expected_batches[number]
        output_path = batch_dir / f"details_{number:04d}.json"
        input_path = batch_dir / f"jd_input_{number:04d}.json"
        log_path = batch_dir / f"details_{number:04d}.log"
        batch_complete = sum(1 for key in expected if is_complete(key))
        if old and old.get("status") == "success" and set(old.get("job_ids") or []) == expected and batch_complete == len(expected):
            print(f"[批次 {number}/{batch_count}] 已完成，跳过（{batch_complete}/{len(expected)} 条）。", flush=True)
            continue

        if batch_complete == len(expected):
            record = {
                "batch": number,
                "browser_mode": browser_mode,
                "cdp_ports": {"chrome": chrome_port, "edge": edge_port},
                "count": len(batch),
                "job_ids": sorted(expected),
                "fetched_count": batch_complete,
                "missing_job_ids": [],
                "input": str(input_path),
                "output": str(output_path),
                "log": str(log_path),
                "status": "success",
                "exit_code": 0,
                "recovered_from_checkpoint": True,
                "started_at": now_iso(),
                "finished_at": now_iso(),
                "security_or_login_gate": False,
            }
            manifest["records"] = [x for x in manifest.get("records", []) if x.get("batch") != number] + [record]
            manifest.update({"total_jobs": len(jobs), "batch_size": batch_size, "updated_at": now_iso()})
            write_json(manifest_path, manifest)
            write_json(details_path, sorted(details.values(), key=lambda x: detail_key(x)))
            print(f"[批次 {number}/{batch_count}] 从检查点恢复完成（{batch_complete}/{len(expected)} 条）。", flush=True)
            continue

        pending_batch = [job for job in batch if not is_complete(detail_key(job))]
        print(
            f"\n[批次 {number}/{batch_count}] 本批 {len(batch)} 条，已完成 {batch_complete} 条，"
            f"本次待抓 {len(pending_batch)} 条；全局 {completed_total}/{len(job_keys)}。",
            flush=True,
        )
        if not cdp_ready:
            message = cdp_error or "本机 Chrome CDP 端口未就绪。"
            log_path.write_text(message + "\n", encoding="utf-8")
            started = now_iso()
            record = {
                "batch": number,
                "browser_mode": browser_mode,
                "cdp_ports": {"chrome": chrome_port, "edge": edge_port},
                "count": len(batch),
                "job_ids": sorted(expected),
                "fetched_count": batch_complete,
                "missing_job_ids": sorted(key for key in expected if not is_complete(key)),
                "input": str(input_path),
                "output": str(output_path),
                "log": str(log_path),
                "status": "paused",
                "pause_reason": "cdp_setup_failed",
                "error": message,
                "exit_code": None,
                "started_at": started,
                "finished_at": now_iso(),
                "security_or_login_gate": False,
            }
            manifest["records"] = [x for x in manifest.get("records", []) if x.get("batch") != number] + [record]
            manifest.update({"total_jobs": len(jobs), "batch_size": batch_size, "updated_at": now_iso()})
            write_json(manifest_path, manifest)
            write_json(details_path, sorted(details.values(), key=lambda x: detail_key(x)))
            break
        while True:
            started = now_iso()
            batch_started = time.monotonic()
            pending_batch = [job for job in batch if not is_complete(detail_key(job))]
            if not pending_batch:
                break
            write_json(input_path, {"keyword": "architecture-screen-jd-batch", "city": "多城市", "filters": {"degree": "203"}, "total": len(pending_batch), "jobs": pending_batch})
            command = [
                str(python), "-u", str(PROJECT_ROOT / "jd_parallel.py"),
                "--scraper-root", str(scraper_root), "--input", str(input_path),
                "--detail-output", str(output_path), "--workers", str(jd_workers),
                "--detail-speed-profile", detail_speed_profile,
            ]
            command.extend(("--browser-mode", browser_mode, "--chrome-port", str(chrome_port),
                            "--edge-port", str(edge_port)))
            if browser_mode == "dual":
                readiness_path = batch_dir / "browser_readiness.json"
                write_json(readiness_path, browser_readiness)
                command.extend(("--browser-readiness-file", str(readiness_path)))
            if chrome_profile_dir:
                command.extend(("--chrome-profile-dir", str(chrome_profile_dir)))
            if edge_profile_dir:
                command.extend(("--edge-profile-dir", str(edge_profile_dir)))
            def report_live_progress(line: str) -> None:
                if re.search(r"JD:\s*\d+\s*字", line):
                    # A nonempty scraper result can still be truncated. Only
                    # persisted, complete checkpoints count as completed work.
                    live_details = dict(details)
                    merge_detail_records(live_details, detail_records_from_file(output_path))
                    live_completed = sum(detail_completeness(live_details.get(key)) == "complete" for key in job_keys)
                    live_batch_completed = sum(detail_completeness(live_details.get(key)) == "complete" for key in expected)
                    print(
                        f"[JD 总进度] {live_completed}/{len(job_keys)} 已完成，"
                        f"当前批次 {live_batch_completed}/{len(expected)}。",
                        flush=True,
                    )

            try:
                completed = run_detail_subprocess(
                    command,
                    env=subprocess_env_with_local_bypass(),
                    on_line=report_live_progress,
                )
                log_text = completed.stdout or ""
            except OSError as exc:
                completed = subprocess.CompletedProcess(command, 1, "", str(exc))
                log_text = str(exc)
            # Gate detection uses the raw upstream text; persisted logs redact
            # common credential fields so a verbose scraper cannot leak secrets.
            log_path.write_text(redact_detail_log(log_text), encoding="utf-8")
            batch_details = detail_records_from_file(output_path)
            merge_detail_records(details, batch_details)
            write_json(details_path, sorted(details.values(), key=lambda x: detail_key(x)))
            fetched = {key for key in expected if is_complete(key)}
            completed_total = sum(1 for key in job_keys if is_complete(key))
            gate = bool(_DETAIL_GATE_RE.search(log_text))
            browser_results = browser_detail_checkpoint_status(output_path, expected) if browser_mode == "dual" else {"chrome": {"status": "pending", "count": 0, "completed_count": 0}}
            adapter_paused = completed.returncode == 2 and any(
                value.get("status") == "paused" for value in browser_results.values())
            status = "paused" if gate or adapter_paused else "failed" if completed.returncode != 0 or not output_path.exists() else "partial" if expected - fetched else "success"
            if browser_mode != "dual":
                browser_results = {"chrome": {"status": status, "count": len(fetched), "completed_count": len(fetched)}}
            record = {"batch": number, "browser_mode": browser_mode,
                      "cdp_ports": {"chrome": chrome_port, "edge": edge_port},
                      "browser_results": browser_results,
                      "count": len(batch), "job_ids": sorted(expected), "fetched_count": len(fetched), "missing_job_ids": sorted(expected - fetched), "input": str(input_path), "output": str(output_path), "log": str(log_path), "status": status, "exit_code": completed.returncode, "started_at": started, "finished_at": now_iso(), "elapsed_seconds": round(time.monotonic() - batch_started, 1), "progress_completed": completed_total, "progress_total": len(job_keys), "security_or_login_gate": gate}
            manifest["records"] = [x for x in manifest.get("records", []) if x.get("batch") != number] + [record]
            manifest.update({"total_jobs": len(jobs), "batch_size": batch_size, "updated_at": now_iso()})
            write_json(manifest_path, manifest)
            print(
                f"[批次 {number}/{batch_count}] {status}：本批 {len(fetched)}/{len(expected)}，"
                f"全局 {completed_total}/{len(job_keys)}，用时 {record['elapsed_seconds']:.1f}s。",
                flush=True,
            )
            if status == "failed" and completed.returncode != 0:
                # Adapter-level failures differ from durable per-job failures
                # (which return zero). Do not launch more batches after a broken
                # adapter or an interrupted child, and let the CLI expose it.
                return {"total_jobs": len(jobs), "details": len(details), "manifest": manifest}
            if status != "paused":
                break
            if not wait_for_detail_gate_retry(wait_on_gate, allow_non_tty=gui_interactive):
                # A gate applies to the browser session, not just this batch.
                # The checkpoint above is durable; do not request later jobs
                # until the user has completed verification and resumed.
                return {"total_jobs": len(jobs), "details": len(details), "manifest": manifest}
            if browser_mode == "dual":
                for browser, port, profile in (("chrome", chrome_port, chrome_profile_dir),
                                               ("edge", edge_port, edge_profile_dir)):
                    if not browser_readiness[browser]["ready"]:
                        ok, error = check_browser_ready(browser, port, profile)
                        browser_readiness[browser] = {"ready": ok, "error": error}
                manifest["browser_readiness"] = browser_readiness
    return {"total_jobs": len(jobs), "details": len(details), "manifest": manifest}


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def model_input(job: dict[str, Any]) -> dict[str, Any]:
    sections = extract_jd_sections(text(job.get("jd", "")), preserve_responsibilities=True)
    # Do not pass title, skills, labels, company copy, salary, or search
    # keywords to the semantic reviewer. They are useful elsewhere in the
    # workflow, but must not become evidence for role fit or JD duties.
    return {
        "job_id": job.get("job_id") or job.get("dedupe_key"),
        "responsibilities": sections.get("responsibilities", ""),
        "requirements": sections.get("requirements", ""),
    }


def review_dimensions(review: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Normalize successful model dimensions while preserving raw evidence."""

    if not isinstance(review, dict):
        return {}
    raw = review.get("dimensions")
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for name in DIMENSIONS:
        value = raw.get(name)
        if not isinstance(value, dict) or "present" not in value:
            continue
        present = value.get("present")
        if isinstance(present, str):
            lowered = present.lower()
            if lowered == "true":
                present = True
            elif lowered == "false":
                present = False
            elif lowered == "unknown":
                present = "unknown"
            else:
                present = False
        if present not in (True, False, "unknown"):
            present = bool(present)
        normalized[name] = {
            "present": present,
            "matched_terms": list(value.get("matched_terms") or []),
            "evidence": list(value.get("evidence") or []),
            "confidence": value.get("confidence"),
        }
    return normalized


def review_exclusion_present(review: dict[str, Any] | None, name: str) -> bool:
    """Read exclusion flags from the current object schema or legacy shapes."""

    if not isinstance(review, dict):
        return False
    exclusions = review.get("exclusions")
    if isinstance(exclusions, dict):
        value = exclusions.get(name)
        if isinstance(value, dict):
            present = value.get("present", value.get("value"))
        else:
            present = value
        return present is True or (isinstance(present, str) and present.lower() == "true")
    if isinstance(exclusions, list):
        for item in exclusions:
            if isinstance(item, dict) and item.get("category") == name:
                present = item.get("present", item.get("status"))
                return present is True or present == "present"
    legacy = review.get(name)
    return isinstance(legacy, dict) and legacy.get("value") is True


def review_experience_assessment(review: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(review, dict):
        return {}
    value = review.get("experience_assessment")
    if isinstance(value, dict):
        return value
    value = review.get("experience")
    return value if isinstance(value, dict) else {}


def run_llm(output_root: Path, workers: int = AI_DEFAULT_WORKERS, timeout: int = 120, max_retries: int = 3, codex_home: str | None = None, base_url: str | None = None, api_key: str | None = None, model: str | None = None, reasoning_effort: str | None = None, *, prompt_file: Path | None = None, api_mode: str | None = None, conversation_size: int = 1) -> dict[str, Any]:
    from llm_client import (CodexResponsesClient, InvalidJsonResponseError, JsonSchemaValidationError,
                            LLMRequestError, SIMPLE_REVIEW_FORMAT, REVIEW_RESPONSE_SCHEMA,
                            load_codex_config, validate_json_schema)
    from review_cache import (CACHE_INPUT_SIGNATURE_FIELD, CACHE_SIGNATURE_FIELD, REVIEW_CACHE_VERSION,
                              provider_signature, review_input_signature, review_signature, select_cached_reviews)

    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError("AI 并发数须为 1–32。")
    if type(conversation_size) is not int or not 1 <= conversation_size <= 100:
        raise ValueError("AI 会话轮数须为 1–100。")
    merged_path = output_root / "prepared" / "merged_jobs_index.json"
    details = load_details(output_root / "prepared" / "jd_shortlists" / "details.json")
    jobs = (read_json(merged_path) or {}).get("jobs", [])
    shortlist_path = output_root / "prepared" / "jd_shortlists" / "jd_input.json"
    shortlist_jobs_by_key: dict[str, dict[str, Any]] = {}
    if shortlist_path.exists():
        shortlist_data = read_json(shortlist_path) or {}
        for item in shortlist_data.get("jobs", []):
            if isinstance(item, dict):
                key = detail_key(item)
                if key:
                    shortlist_jobs_by_key[key] = item
    eligible = shortlist_jobs_by_key.values() if shortlist_path.exists() else []
    candidates = []
    for job in eligible:
        detail = find_detail(job, details)
        if detail_completeness(detail) == "complete":
            item = dict(job)
            item["jd"] = text(detail.get("jd"))
            candidates.append(item)
    reviews_path = output_root / "prepared" / "llm_reviews.json"
    jsonl_path = output_root / "prepared" / "llm_reviews.jsonl"
    manifest_path = output_root / "prepared" / "llm_manifest.json"
    old_payload = {}
    if reviews_path.exists():
        try:
            old_payload = read_json(reviews_path)
        except (OSError, UnicodeError, ValueError):
            pass
    candidate_keys = {text(job.get("job_id") or job.get("dedupe_key")) for job in candidates}

    def save_reviews(selection: Any) -> None:
        write_json(reviews_path, selection.document(now_iso()))
        write_jsonl(jsonl_path, selection.reviews.values())

    def cache_counts(selection: Any, request_count: int) -> dict[str, Any]:
        return {
            "cache_version": REVIEW_CACHE_VERSION,
            "review_format": SIMPLE_REVIEW_FORMAT,
            "cache_hit_count": selection.hit_count,
            "cache_miss_count": len(candidates) - selection.hit_count,
            "cache_invalidated_count": selection.invalidated_count,
            "cache_legacy_count": selection.legacy_count,
            "cache_excluded_count": selection.excluded_count,
            "request_count": request_count,
        }

    if not candidates:
        selection = select_cached_reviews(old_payload, {}, input_signatures={})
        save_reviews(selection)
        manifest = {
            "generated_at": now_iso(),
            "candidate_count": 0,
            "completed_count": 0,
            "pending_count": 0,
            "failed_count": 0,
            "fallback_count": 0,
            "workers": workers,
            "status": "skipped_no_candidates",
            **cache_counts(selection, 0),
        }
        write_json(manifest_path, manifest)
        return manifest
    # ``load_codex_config`` accepts concrete config/auth paths.  Resolve an
    # optional home directory here so the processing CLI never leaks or logs
    # credentials while still honoring the user's local Codex installation.
    env_home = os.environ.get("CODEX_HOME")
    home = Path(codex_home).expanduser() if codex_home else Path(env_home).expanduser() if env_home else None
    config_path = home / "config.toml" if home else None
    auth_path = home / "auth.json" if home else None
    try:
        project_settings_path = None if codex_home else project_llm_settings.default_settings_path()
        project_settings = (
            project_llm_settings.load_settings(project_settings_path)
            if project_settings_path is not None else None
        )
        cfg = load_codex_config(
            config_path=config_path,
            auth_path=auth_path,
            project_settings_path=project_settings_path,
            base_url=base_url,
            api_key=api_key,
            model=model or (project_settings or {}).get("model") or AI_DEFAULT_MODEL,
            reasoning_effort=reasoning_effort or (project_settings or {}).get("reasoning_effort") or AI_DEFAULT_REASONING_EFFORT,
            api_mode=(api_mode or (project_settings or {}).get("api_mode")
                      or (None if codex_home else project_llm_settings.API_MODE_RESPONSES)),
            timeout=timeout,
            max_retries=max_retries,
        )
        provider = provider_signature(cfg)
        # The same prompt snapshot is used for every signature and request.
        prompt = Path(prompt_file).read_text(encoding="utf-8") if prompt_file is not None else load_prompt()
        signatures = {text(job.get("job_id") or job.get("dedupe_key")):
                      review_signature(model_input(job), prompt, provider) for job in candidates}
        input_signatures = {text(job.get("job_id") or job.get("dedupe_key")):
                            review_input_signature(model_input(job), prompt) for job in candidates}
        client = CodexResponsesClient(cfg)
    except Exception as exc:
        # Semantic review is an enrichment step.  Preserve a machine-readable
        # manifest and let report_stage apply deterministic rules when the
        # local provider is unavailable or not configured yet.
        selection = select_cached_reviews(old_payload, {key: None for key in candidate_keys}, input_signatures={})
        save_reviews(selection)
        manifest = {
            "generated_at": now_iso(),
            "candidate_count": len(candidates),
            "completed_count": 0,
            "pending_count": len(candidates),
            "failed_count": 0,
            "fallback_count": len(candidates),
            "workers": workers,
            "status": "configuration_failed",
            "error": f"模型配置或提示词暂不可用（{type(exc).__name__}）",
            "error_type": type(exc).__name__,
            **cache_counts(selection, 0),
        }
        write_json(manifest_path, manifest)
        return manifest
    selection = select_cached_reviews(old_payload, signatures, input_signatures=input_signatures)
    pending = [job for job in candidates if text(job.get("job_id") or job.get("dedupe_key")) not in selection.reviews]
    # These describe this invocation, not the growing number of new successes.
    counts = cache_counts(selection, len(pending))
    results = selection.reviews

    # Test doubles and older third-party adapters may expose only the original
    # review_job(job, prompt=...) contract. The built-in client is the only
    # implementation that can safely carry a conversation state.
    conversation_supported = getattr(client, "supports_conversations", False) is True
    session_reuse = conversation_size > 1 and conversation_supported
    conversation_stats = {
        "conversation_mode": "parallel_reused_sessions" if session_reuse else "stateless",
        "conversation_session_size": conversation_size,
        "conversation_sessions_started": 0,
        "conversation_followup_count": 0,
        "conversation_fallback_count": 0,
        "conversation_validated_request_count": 0,
        "conversation_client_history_job_count": 0,
        "response_storage_requested": bool(session_reuse and cfg.api_mode == "responses"),
    }

    def save_checkpoint(status: str) -> dict[str, Any]:
        save_reviews(selection)
        successful = sum(row.get("status") == "success" for row in results.values())
        failures = sum(row.get("status") == "failed" for row in results.values())
        manifest = {
            "generated_at": now_iso(), "candidate_count": len(candidates), "completed_count": successful,
            "pending_count": len(candidates) - successful - failures, "failed_count": failures,
            "fallback_count": failures, "workers": workers, "model": cfg.model,
            "reasoning_effort": cfg.effective_reasoning_effort,
            "api_mode": cfg.api_mode, "chat_stream": cfg.chat_stream if cfg.api_mode == "chat_completions" else False,
            "status": status, **conversation_stats, **counts,
        }
        write_json(manifest_path, manifest)
        return manifest

    # Displaced successes are history, never current results while requests are
    # in flight.  Each per-job checkpoint is independently safe to resume even
    # if its sidecar or final manifest was not written before interruption.
    save_checkpoint("running" if pending else "completed")
    outcome_queue: queue.Queue = queue.Queue()

    def review_lane(lane_jobs: list[dict[str, Any]]) -> None:
        conversation: dict[str, Any] = {}
        turns = 0
        conversation_enabled = session_reuse
        responses_history_only = False
        for job in lane_jobs:
            started = False
            continued = False
            fallback = False
            if conversation_enabled and turns >= conversation_size:
                conversation.clear()
                turns = 0
                if responses_history_only:
                    conversation["responses_history_only"] = True
            if conversation_enabled:
                started = not bool(conversation.get("previous_response_id") or conversation.get("messages"))
                continued = not started
            try:
                kwargs: dict[str, Any] = {"prompt": prompt}
                if conversation_enabled:
                    kwargs["conversation"] = conversation
                try:
                    result = dict(client.review_job(model_input(job), **kwargs))
                except Exception as first_exc:
                    status_code = getattr(first_exc, "status_code", None)
                    transport_rejected_history = (isinstance(first_exc, LLMRequestError)
                                                  and status_code in (400, 404, 422))
                    invalid_server_continuation = (
                        cfg.api_mode == "responses" and not responses_history_only
                        and bool(conversation.get("previous_response_id") or conversation.get("messages"))
                        and isinstance(first_exc, (InvalidJsonResponseError, JsonSchemaValidationError))
                    )
                    if conversation_enabled and (transport_rejected_history or invalid_server_continuation):
                        fallback = True
                        if cfg.api_mode == "responses" and not responses_history_only:
                            # Keep a real logical conversation when server-side
                            # chaining is unavailable or returns unusable output:
                            # replay validated text
                            # history with the current job, without provider IDs.
                            responses_history_only = True
                            conversation["responses_history_only"] = True
                            conversation.pop("previous_response_id", None)
                            result = dict(client.review_job(model_input(job), prompt=prompt,
                                                           conversation=conversation))
                        elif cfg.api_mode == "chat_completions":
                            # A Chat provider may reject a history-bearing
                            # request. Try this job independently once.
                            conversation.clear()
                            conversation_enabled = False
                            result = dict(client.review_job(model_input(job), prompt=prompt))
                        else:
                            raise
                    else:
                        raise
                if conversation_enabled:
                    if cfg.api_mode == "responses" and not conversation.get("previous_response_id"):
                        if conversation.get("responses_history_only") is True:
                            fallback = fallback or not responses_history_only
                            responses_history_only = True
                        else:
                            # Older adapters without local history cannot
                            # continue a successful response lacking an ID.
                            conversation_enabled = False
                            fallback = True
                    turns += 1
                outcome_queue.put((job, result, None, started, continued, fallback))
            except Exception as exc:
                # A failed turn may not have a usable response ID. Start a clean
                # session on the next job rather than risking cross-job drift.
                conversation.clear()
                turns = 0
                if responses_history_only:
                    conversation["responses_history_only"] = True
                outcome_queue.put((job, None, exc, started, continued, fallback))
            except BaseException as exc:
                # A worker interruption must reach the main thread; otherwise
                # it would wait indefinitely for an outcome that never arrives.
                outcome_queue.put((job, None, exc, started, continued, fallback))
                return

    lanes = [pending[index::max(1, workers)] for index in range(max(1, workers))]
    lanes = [lane for lane in lanes if lane]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        lane_futures = [pool.submit(review_lane, lane) for lane in lanes]
        for _ in range(len(pending)):
            job, value, error_exc, session_started, session_continued, session_fallback = outcome_queue.get()
            key = text(job.get("job_id") or job.get("dedupe_key"))
            if session_started:
                conversation_stats["conversation_sessions_started"] += 1
            if session_continued:
                conversation_stats["conversation_followup_count"] += 1
            if session_fallback:
                conversation_stats["conversation_fallback_count"] += 1
            try:
                if error_exc is not None:
                    raise error_exc
                result = dict(value)
                # Even third-party adapters must honor the model contract.
                # The application, never the model, binds identity/provenance.
                validate_json_schema({key: val for key, val in result.items()
                                      if key not in {"_conversation", "_format_repair_attempts", "_schema_repair_fields"}},
                                     REVIEW_RESPONSE_SCHEMA)
                # Provider response IDs and full history remain in lane-local
                # memory only. Persist only bounded, client-produced counters.
                transport_metadata = result.pop("_conversation", {})
                if isinstance(transport_metadata, dict):
                    used_requests = transport_metadata.get("request_count")
                    if type(used_requests) is int and used_requests >= 0:
                        conversation_stats["conversation_validated_request_count"] += used_requests
                    result["conversation_reused"] = transport_metadata.get("reused") is True
                    result["conversation_client_history"] = transport_metadata.get("client_history") is True
                    if result["conversation_client_history"]:
                        conversation_stats["conversation_client_history_job_count"] += 1
                result["conversation_session_size"] = conversation_size
                # The input index is authoritative; never let a hallucinated
                # model id break checkpoint/report joins.
                result["job_id"] = key
                result["status"] = "success"
                result["review_format"] = SIMPLE_REVIEW_FORMAT
                schema_repairs = result.pop("_schema_repair_fields", [])
                if isinstance(schema_repairs, list) and schema_repairs:
                    result["schema_repair_fields"] = [str(field) for field in schema_repairs]
                    result["needs_human_review"] = True
                # Provenance belongs to the actual request configuration, not
                # to fields a model might include in its generated JSON.
                result["review_model"] = cfg.model
                result["review_reasoning_effort"] = cfg.effective_reasoning_effort
                result["review_api_mode"] = cfg.api_mode
                result["review_chat_stream"] = cfg.chat_stream if cfg.api_mode == "chat_completions" else False
                result["format_repair_attempts"] = int(result.pop("_format_repair_attempts", 0))
                result.pop("_response_id", None)
                if session_fallback:
                    result["conversation_fallback"] = True
                result["source"] = ("configured_chat_completions_api" if cfg.api_mode == "chat_completions"
                                    else "configured_responses_api")
            except Exception as exc:
                # Avoid forwarding exception strings that might contain provider
                # diagnostics or request material into result files.
                http_status = getattr(exc, "status_code", None)
                http_status = http_status if type(http_status) is int else None
                operation = "Chat Completions API" if cfg.api_mode == "chat_completions" else "Responses API"
                validation_error = str(exc) if isinstance(exc, JsonSchemaValidationError) else None
                if isinstance(exc, JsonSchemaValidationError):
                    error = "模型输出未通过本地 JSON 结构校验"
                elif isinstance(exc, InvalidJsonResponseError):
                    error = "模型响应不是可解析的 JSON"
                else:
                    error = (f"{operation} 请求失败（HTTP {http_status}）" if http_status is not None
                             else f"{operation} 请求失败" if isinstance(exc, LLMRequestError) else type(exc).__name__)
                result = {
                    "job_id": key,
                    "status": "failed",
                    "review_format": SIMPLE_REVIEW_FORMAT,
                    "error": error,
                    "error_type": type(exc).__name__,
                    "validation_error": validation_error,
                    "format_repair_attempts": int(getattr(exc, "format_repair_attempts", 0)),
                    "http_status": http_status,
                    "requested_model": cfg.model,
                    "requested_reasoning_effort": cfg.effective_reasoning_effort,
                    "requested_api_mode": cfg.api_mode,
                    "requested_chat_stream": cfg.chat_stream if cfg.api_mode == "chat_completions" else False,
                    "needs_human_review": True,
                }
            result[CACHE_SIGNATURE_FIELD] = signatures[key]
            result[CACHE_INPUT_SIGNATURE_FIELD] = input_signatures[key]
            results[key] = result
            save_checkpoint("running")
        for future in lane_futures:
            future.result()
    return save_checkpoint("completed")


def _grounded_quotes(evidence: Any, jd: str) -> list[str]:
    """Use supplied JD quotes only; whitespace formatting may differ."""

    if not isinstance(evidence, list):
        return []
    normalized_jd = re.sub(r"\s+", "", jd).casefold()
    return list(dict.fromkeys(
        quote for quote in evidence
        if isinstance(quote, str) and quote.strip()
        and re.sub(r"\s+", "", quote).casefold() in normalized_jd
    ))


def _grounded_duty_quotes(evidence: Any, responsibilities: str) -> list[str]:
    """Ground model evidence in complete duties without a verb allowlist.

    Keep the narrow safeguards for explicitly negated work and work assigned
    to another team. Understanding other phrasing belongs to the reviewer.
    """

    return _grounded_quotes(evidence, _responsibility_text(responsibilities, filter_product_sales=False))


def _grounded_dimension_quotes(name: str, evidence: Any, responsibilities: str) -> list[str]:
    """Ground semantic dimension decisions in actual duties, not term overlap.

    The model is asked to map work described in natural language to a review
    dimension. Requiring a fixed keyword from ``DIMENSION_TERMS`` here would
    silently turn semantic review back into keyword matching. Exact JD quotes
    from the responsibility section are the evidence boundary instead.
    """

    del name  # The meaning-to-dimension mapping belongs to the semantic review.
    return _grounded_duty_quotes(evidence, responsibilities)


def _target_relevance(review: dict[str, Any] | None, responsibilities: str, *,
                      fallback_responsibilities: str | None = None) -> dict[str, Any]:
    """Validate a model's target-direction assessment against actual duties."""

    raw = review.get("target_relevance") if isinstance(review, dict) else None
    if not isinstance(raw, dict):
        inferred = _rule_target_relevance(responsibilities if fallback_responsibilities is None
                                          else fallback_responsibilities)
        inferred["source"] = "rule_fallback_missing_model_assessment"
        return inferred
    assessment = text(raw.get("assessment")).strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "direct": "direct", "directly_relevant": "direct", "直接相关": "direct",
        "adjacent": "adjacent", "transferable_adjacent": "adjacent", "可迁移相邻": "adjacent",
        "unrelated": "unrelated", "not_relevant": "unrelated", "不相关": "unrelated",
        "insufficient": "insufficient", "unknown": "insufficient", "信息不足": "insufficient",
    }
    assessment = aliases.get(assessment, "insufficient")
    quotes = _grounded_duty_quotes(raw.get("evidence"), responsibilities)
    if assessment in ("direct", "adjacent", "unrelated") and not quotes:
        return {"assessment": "insufficient", "evidence": [], "validation": "缺少可核对JD原文（职责段实际工作证据）"}
    # Do not reclassify the model's semantic judgement through fixed positive
    # keyword lists. It must quote a grounded duty above; the prompt
    # distinguishes AI application work from sales, pure development and
    # infrastructure work using the task's actual meaning.
    return {"assessment": assessment, "evidence": quotes}


def _rule_target_relevance(responsibilities: str) -> dict[str, Any]:
    """Conservative non-model fallback based only on performed duties."""

    duties = [line.strip() for line in responsibilities.splitlines() if line.strip()]
    adjacent_terms = (
        *ARCHITECTURE_ROLE_TERMS, *INTEGRATION_TERMS, "解决方案设计", "技术方案设计",
        "客户技术方案", "业务系统", "企业软件", "企业应用", "业务应用",
        "PoC", "项目交付", "软件实施", "应用部署", "产品规划", "产品设计",
    )
    duty_text = "\n".join(duties)
    matched_dimensions = sum(
        any(contains(duty_text, term) for term in terms)
        for terms in DIMENSION_TERMS.values()
    )
    # In code fallback only, a generic AI/Agent term is a context signal,
    # never a verdict. Require multiple distinct action/dimension signals
    # before a technical-AI direction can be inferred.
    direct = (duties if matched_dimensions >= 2 and _screen_terms(duty_text, AI_CONTEXT_TERMS)
              else [])
    if direct:
        return {"assessment": "direct", "evidence": direct, "source": "rule_fallback"}
    adjacent = [line for line in duties if _screen_terms(line, adjacent_terms)]
    # A description can express transferable application work without using
    # a canonical role label. Two or more action-bearing core responsibilities
    # are enough for the fallback to retain it as adjacent; one isolated term
    # is not enough to decide direction.
    if matched_dimensions >= 2 and not adjacent:
        adjacent = duties
    if adjacent:
        return {"assessment": "adjacent", "evidence": adjacent, "source": "rule_fallback"}
    return {"assessment": "insufficient", "evidence": [], "source": "rule_fallback"}


def _review_exclusion_evidence(review: dict[str, Any], name: str) -> list[str]:
    raw = review.get("exclusions")
    if isinstance(raw, dict):
        value = raw.get(name)
        return value.get("evidence", []) if isinstance(value, dict) else []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("category") == name:
                return item.get("evidence", [])
    legacy = review.get(name)
    return legacy.get("evidence", []) if isinstance(legacy, dict) else []


def _merge_simple_review(job: dict[str, Any], review: dict[str, Any], *, pending: bool = False) -> dict[str, Any]:
    """Use the AI judgment directly; salary/experience are annotations only."""
    from llm_client import SIMPLE_REVIEW_FORMAT, simple_review_payload

    payload = None if pending else simple_review_payload(review)
    judgment = payload["判断"] if payload is not None else None
    reason = payload["理由"] if payload is not None else "AI复核未成功，待人工核实"
    item = {**job, **score_job(job)}
    for field in ("jd_dimensions", "jd_dimension_names", "jd_dimension_count", "target_relevance",
                  "jd_evidence", "model_evidence", "jd_exclusion_evidence", "ability_gaps", "gaps",
                  "confidence", "verification_items"):
        item.pop(field, None)
    jd = text(job.get("jd")).strip()
    quality = detail_completeness(job)
    salary = parse_salary(job.get("salary"))
    salary_label = salary_status(salary)
    experience = parse_jd_experience(extract_jd_sections(jd, preserve_responsibilities=True).get("requirements", ""))
    minimum_years = experience.get("minimum_years")
    verification: list[str] = _source_conflict_notes(job)
    if pending:
        verification.append("AI复核未成功，判断与理由待重新复核")
    elif judgment == "信息不足":
        verification.append("AI判断信息不足，待人工核实")
    if quality != "complete":
        verification.append("JD正文缺失或部分可用，待补齐核实")
    if salary_label == "低于目标（上限<17K）":
        verification.append("薪资低于17K目标")
    elif salary_label.startswith("跨17K"):
        verification.append("核实固定薪资和绩效占比")
    if minimum_years is not None and minimum_years >= 3:
        verification.append(f"JD要求至少{minimum_years:g}年相关经验，需核实是否为硬性门槛")
    return {
        **item, "review_format": SIMPLE_REVIEW_FORMAT,
        "ai_judgment": judgment, "ai_reason": payload["理由"] if payload is not None else None,
        "analysis_pending": pending or judgment == "信息不足",
        "classification": "暂不投" if judgment == "不符合" else "海投池",
        "reason": reason, "reasons": [reason],
        "salary_parsed": salary, "salary_status": salary_label,
        "jd_status": "部分可用" if quality == "partial" else "已抓取" if jd else "待核实",
        "jd_fetched": bool(jd), "jd_minimum_years": minimum_years,
        "jd_experience_evidence": experience.get("evidence", []),
        "verification": verification, "verify": verification,
        "llm_review": review, "model_status": review.get("status", "not_run") if pending else "success",
        "model_error": review.get("error") if pending else None,
    }


def merge_model_review(job: dict[str, Any], review: dict[str, Any] | None) -> dict[str, Any]:
    from llm_client import SIMPLE_REVIEW_FORMAT, JsonSchemaValidationError

    is_simple = job.get("review_format") == SIMPLE_REVIEW_FORMAT or bool(review and (
        review.get("review_format") == SIMPLE_REVIEW_FORMAT or "判断" in review or "理由" in review
    ))
    if is_simple:
        if review and review.get("status") == "success":
            try:
                return _merge_simple_review(job, review)
            except JsonSchemaValidationError:
                review = {"status": "failed", "review_format": SIMPLE_REVIEW_FORMAT,
                          "error": "模型两字段结果无效，待重新复核"}
        return _merge_simple_review(job, review or {"status": "not_run", "review_format": SIMPLE_REVIEW_FORMAT}, pending=True)
    # Recompute even for direct callers and legacy reports. Cached scores or
    # exclusion flags must not silently retain an older screening policy.
    job = {**job, **score_job(job)}
    jd = text(job.get("jd")).strip()
    jd_quality = detail_completeness(job)
    rule = rule_analyze_jd(jd, text(job.get("title")))
    responsibilities = rule.get("responsibilities", "")
    model_responsibilities = model_input(job)["responsibilities"]
    dimensions = {name: dict(value) for name, value in rule["dimensions"].items()}
    model_dims, model_verification = {}, []
    successful_review = bool(review and review.get("status") == "success")
    target_relevance = (
        _target_relevance(review, model_responsibilities, fallback_responsibilities=responsibilities) if successful_review
        else _rule_target_relevance(responsibilities)
    )
    if not successful_review and jd:
        model_verification.append("AI语义复核未成功；目标方向仅按职责段规则兜底，职位名/技能/通用词不参与判断")
    if jd and successful_review:
        for name, value in review_dimensions(review).items():
            quotes = _grounded_dimension_quotes(name, value.get("evidence"), model_responsibilities)
            if value.get("present") is True and not quotes:
                model_verification.append(f"模型{name}缺少职责段中带实际工作动作的可核对原文，使用规则回退")
                continue
            model_dims[name] = {**value, "evidence": quotes}
        dimensions.update(model_dims)
        if target_relevance["assessment"] == "insufficient":
            reason = target_relevance.get("validation") or "模型未提供可核对的目标方向判断"
            model_verification.append(f"目标方向复核证据不足：{reason}")
    elif successful_review:
        model_verification.append("缓存模型结论缺少JD正文，不用于职责或排除判断")
    count = sum(1 for value in dimensions.values() if value.get("present") is True)
    exclusions = []
    if job["list_salary_below_target"]:
        exclusions.append(SALARY_CEILING_REASON)
    role_exclusion_reasons = [reason for reason in job["list_exclusion_reasons"] if reason != SALARY_CEILING_REASON]
    if job["list_pure_development"]:
        # A coding title or Agent/AI stack is not new JD counterevidence for
        # its own exclusion. Require actual mixed duties in the saved JD.
        list_counter = bool(mixed_duty_evidence({"jd": _responsibility_text(jd)}))
    else:
        list_counter = rule["sales_counter"] if any("纯销售" in reason for reason in role_exclusion_reasons) else (rule["application_counter"] or rule["system_counter"])
    if role_exclusion_reasons and (not jd or not list_counter):
        exclusions.extend(role_exclusion_reasons)
    elif role_exclusion_reasons:
        model_verification.append("列表排除线索与JD应用/方案建设证据冲突，已按JD重新判断")
    model_exclusions = set()
    for category in ("pure_sales", "pure_algorithm", "pure_infrastructure", "irrelevant"):
        if not jd or not successful_review or not review_exclusion_present(review, category):
            continue
        quotes = _grounded_duty_quotes(_review_exclusion_evidence(review, category), model_responsibilities)
        counter = rule["system_counter"] if category == "irrelevant" else rule["application_counter"]
        if category == "pure_sales":
            counter = rule["sales_counter"]
        if quotes and not counter:
            model_exclusions.add(category)
        else:
            model_verification.append(f"模型{category}排除缺少职责段原文或存在应用/技术方案反向证据，未作硬排")
    if rule["irrelevant"]: exclusions.append("JD明显属于无关岗位")
    if rule["pure_sales"] or "pure_sales" in model_exclusions: exclusions.append("JD以纯销售KPI为主")
    if rule["pure_algorithm"] or "pure_algorithm" in model_exclusions: exclusions.append("JD以纯算法研发为主")
    if rule["pure_infrastructure"] or "pure_infrastructure" in model_exclusions: exclusions.append("JD以纯底层基础设施为主")
    if "irrelevant" in model_exclusions: exclusions.append("JD明显属于无关岗位")
    tracks = {x.get("track") for x in job.get("observed_in", [])}
    salary = parse_salary(job.get("salary"))
    status = salary_status(salary)
    labels = extract_experience_labels(job)
    list_high_experience = any(
        any(token in label for token in ("3-5", "5-10", "3_5", "5_10", "年以上", "105", "106"))
        for label in labels
    )
    jd_minimum_years = rule.get("minimum_years")
    jd_high_experience = jd_minimum_years is not None and jd_minimum_years >= 3
    high_experience = list_high_experience or jd_high_experience
    experience_assessment = review_experience_assessment(review)
    hard_conflict = bool(experience_assessment.get("hard_conflict") is True) or (jd_high_experience and "current" in tracks)
    if exclusions:
        classification = "暂不投"
        reason = "；".join(dict.fromkeys(exclusions))
    elif not jd:
        classification = "海投池" if job["list_pass"] else "暂不投"
        reason = "列表初筛通过，JD待核实" if classification == "海投池" else "列表相关性不足"
    elif target_relevance["assessment"] == "unrelated":
        classification, reason = "暂不投", "岗位职责与AI应用/解决方案方向不相关"
    elif target_relevance["assessment"] == "insufficient":
        classification, reason = "暂不投", "目标方向语义证据不足，不能仅凭通用关键词放行"
    elif target_relevance["assessment"] == "direct" and jd_quality == "complete" and count >= 4 and salary.get("lower_k") is not None and salary["lower_k"] >= 17 and not hard_conflict and not high_experience:
        classification, reason = "重点投递", f"JD命中{count}/6项核心职责，薪资下限达到17K"
    elif count >= 2 and target_relevance["assessment"] in ("direct", "adjacent"):
        classification, reason = "海投池", f"JD命中{count}/6项核心职责"
    else:
        classification, reason = "暂不投", "JD核心职责命中不足2项"
    verification = list(review.get("verification_items", []) if review and isinstance(review.get("verification_items"), list) else [])
    verification.extend(job["list_verification"])
    verification.extend(model_verification)
    if target_relevance["assessment"] == "adjacent":
        verification.append("岗位属于可迁移相邻方向，不等同于直接AI应用架构岗位")
    elif target_relevance["assessment"] in ("insufficient", "unverified"):
        verification.append("未从岗位职责确认与目标方向的语义关联，需人工复核")
    if not jd:
        verification.append("JD正文未提供，六项职责及岗位要求待核实")
    elif jd_quality == "partial":
        verification.append("JD正文部分可用，待补齐完整正文；当前职责证据和分层仅基于已保存片段")
    if status == "低于目标（上限<17K）": verification.append("薪资低于17K目标")
    elif status.startswith("跨17K"): verification.append("核实固定薪资和绩效占比")
    if high_experience: verification.append("经验要求偏高，用项目案例争取")
    if jd_high_experience:
        verification.append(f"JD要求至少{jd_minimum_years:g}年相关经验，需核实是否为硬性门槛")
        if labels and not list_high_experience:
            verification.append("JD/列表或检索经验标签可能冲突，需核实实际最低年限")
        elif not labels:
            verification.append("列表经验标签缺失，需核实JD经验门槛")
    rule_evidence = [evidence for value in rule["dimensions"].values() for evidence in value.get("evidence", [])]
    model_evidence = [evidence for value in model_dims.values() for evidence in value.get("evidence", [])]
    gaps = list(review.get("ability_gaps", []) if jd and successful_review and isinstance(review.get("ability_gaps"), list) else [])
    llm_status = review.get("status", "not_run") if review else "not_run"
    return {
        **job,
        "salary_parsed": salary,
        "salary_status": status,
        "jd_status": "部分可用" if jd_quality == "partial" else "已抓取" if jd else "待核实",
        "jd_fetched": bool(jd),
        "jd_dimensions": dimensions,
        "jd_dimension_names": [x for x, value in dimensions.items() if value.get("present") is True],
        "jd_dimension_count": count,
        "target_relevance": target_relevance,
        "jd_evidence": list(dict.fromkeys(rule_evidence)),
        "jd_minimum_years": jd_minimum_years,
        "jd_experience_evidence": rule.get("experience_evidence", []),
        "jd_exclusion_evidence": rule.get("exclusion_evidence", []),
        "model_evidence": list(dict.fromkeys(model_evidence)),
        "classification": classification,
        "reason": reason,
        "reasons": [reason],
        "verification": list(dict.fromkeys(verification)),
        "verify": list(dict.fromkeys(verification)),
        "ability_gaps": gaps,
        "gaps": gaps,
        "llm_review": review or {"status": "not_run"},
        "model_status": llm_status,
        "model_error": review.get("error") if review and llm_status != "success" else None,
        "confidence": "low" if not jd else "high" if llm_status == "success" else "medium",
    }


def _report_markdown(value: Any) -> str:
    """Keep untrusted job text on one Markdown line without changing JSON."""

    value = re.sub(r"\s+", " ", text(value)).strip()
    if not value:
        return "未提供（待核实）"
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_\[\]|])", r"\\\1", value)


def _report_job_link(job: dict[str, Any]) -> str:
    from urllib.parse import quote

    # Parentheses, brackets, whitespace and pipes must not end a link target.
    return quote(text(job.get("job_link")).strip(), safe=":/?#@!$&'*+,;=%-._~")


def _report_jd_completeness(job: dict[str, Any]) -> str:
    """Present recorded JD quality without certifying unmarked legacy text."""

    completeness = detail_completeness(job)
    if completeness != "complete":
        return completeness
    if (
        text(job.get("jd_completeness")).strip().casefold() == "complete"
        or job.get("complete") is True
        or job.get("jd_complete") is True
    ):
        return "complete"
    return "unknown"


def _report_overview_lines(jobs: list[dict[str, Any]], pool: list[dict[str, Any]]) -> list[str]:
    """Give the actionable pool a compact view, leaving one detail entry per job."""

    counts = Counter(job.get("classification") for job in jobs)
    lines = [
        "## 可投总览", "",
        f"全部岗位：{len(jobs)}；重点投递：{counts.get('重点投递', 0)}；海投池：{counts.get('海投池', 0)}；暂不投：{counts.get('暂不投', 0)}；待核实：{counts.get('待核实', 0)}。",
        f"可投清单：{len(pool)} 岗（与 [hai_tou_pool.json](hai_tou_pool.json) 一致；未执行投递）。", "",
    ]
    if not pool:
        return lines + ["本次没有可投岗位；全部岗位及原因仍保留在下方详细证据中。", ""]
    lines.extend([
        "| 分层 | 公司 | 岗位 | 薪资 | 必要标记 |",
        "| --- | --- | --- | --- | --- |",
    ])
    for classification in ("重点投递", "海投池"):
        for job in pool:
            if job.get("classification") != classification:
                continue
            flags = [{
                "complete": "JD完整",
                "partial": "JD部分（待补齐）",
                "missing": "JD缺失（待核实）",
                "unknown": "JD完整性待核实（旧数据未记录完整度）",
            }[_report_jd_completeness(job)]]
            if report_manual_choice(job) is True:
                flags.append("已确认人工保留（AI复核阶段）")
            salary_flags = job.get("salary_flags")
            if isinstance(salary_flags, list):
                flags.extend(text(flag) for flag in salary_flags if text(flag))
            else:
                salary = text(job.get("salary_status"))
                if salary.startswith("低于目标"):
                    flags.append("薪资低于目标")
                elif salary.startswith("跨17K"):
                    flags.append("需核实固定薪资")
                elif salary == "未知":
                    flags.append("薪资待核实")
            if job.get("experience_gap") or any(
                "经验要求偏高" in text(item) for item in (job.get("verification") or job.get("verify") or [])
            ):
                flags.append("经验要求偏高")
            model_status = text(job.get("model_status")) or "not_run"
            if job.get("analysis_pending"):
                flags.append("AI信息不足（待核实）" if job.get("ai_judgment") == "信息不足" else "AI待核实")
            if job.get("model_cache_validation") == "legacy_unverified":
                flags.append("历史模型缓存未验证")
            if model_status != "success":
                if job.get("review_format") == "simple-v1":
                    flags.append("模型失败，待重跑" if model_status == "failed" else "模型未完成，待核实")
                else:
                    flags.append({
                        "failed": "模型失败，规则回退",
                        "not_run": "模型未运行，规则回退",
                    }.get(model_status, "规则回退"))
            lines.append("| " + " | ".join([
                _report_markdown(classification),
                _report_markdown(job.get("company") or job.get("boss_name")),
                f"[{_report_markdown(job.get('title'))}]({_report_job_link(job)})",
                _report_markdown(job.get("salary")),
                _report_markdown("；".join(dict.fromkeys(flags))),
            ]) + " |")
    return lines + [""]


def _report_job_lines(job: dict[str, Any]) -> list[str]:
    """Render one job, retaining provenance and explicit evidence gaps."""

    from llm_client import SIMPLE_REVIEW_FORMAT
    if job.get("review_format") == SIMPLE_REVIEW_FORMAT:
        display = _report_markdown
        review = job.get("llm_review") or {}
        lines = [
            f"- [{display(job.get('title'))}]({_report_job_link(job)})｜{display(job.get('company') or job.get('boss_name'))}｜{display(job.get('salary'))}",
            f"  - 岗位ID：{display(job.get('job_id') or job.get('dedupe_key'))}；最终分层：{display(job.get('classification'))}",
            f"  - AI判断：{display(job.get('ai_judgment'))}；理由：{display(job.get('ai_reason'))}",
            f"  - JD完整度：{display(_report_jd_completeness(job))}；模型状态：{display(job.get('model_status'))}；复核格式：{SIMPLE_REVIEW_FORMAT}",
            f"  - 薪资状态：{display(job.get('salary_status'))}；经验标签：{display(extract_experience_labels(job))}",
            f"  - 模型：{display(review.get('review_model'))}；协议：{display(review.get('review_api_mode'))}；缓存：{display(job.get('model_cache_validation'))}",
            f"  - 待核实：{display(job.get('verification') or job.get('verify') or [])}",
        ]
        manual = report_manual_choice(job)
        if manual is not None:
            lines.append("  - 报告决定来源：AI 语义复核已确认人工" + ("保留" if manual else "剔除")
                         + "；原始 AI 判断与技术状态保留追溯。")
        for source in job.get("observed_in", []):
            if isinstance(source, dict):
                lines.append("  - 检索来源：" + display(source))
        return lines
    link = _report_job_link(job)
    display = _report_markdown
    jd = text(job.get("jd")).strip()
    count = job.get("jd_dimension_count", 0)
    list_evidence = [f"{item.get('family')} / {item.get('field')}：{item.get('quote')}" for item in job.get("list_match_evidence", [])]
    list_exclusions = [f"{item.get('family')} / {item.get('field')}：{item.get('quote')}" for item in job.get("list_exclusion_evidence", [])]
    lines = [
        f"- [{display(job.get('title'))}]({link})｜{display(job.get('company') or job.get('boss_name'))}｜{display(job.get('salary'))}｜命中 {count}/6｜{display(job.get('reason'))}",
        f"  - 岗位ID：{display(job.get('job_id') or job.get('dedupe_key'))}；最终分层：{display(job.get('classification'))}",
        f"  - 列表初筛：{'通过' if job.get('list_pass') else '未通过'}；优先级：{display(job.get('list_priority'))}；排序分：{display(job.get('score'))}；规则版本：{display(job.get('screening_version'))}",
        f"  - 列表方向：{display(job.get('list_families'))}",
        f"  - 列表方向依据：{display(list_evidence)}",
        f"  - 列表排除依据：{display(list_exclusions)}",
    ]
    sources = [source for source in job.get("observed_in", []) if isinstance(source, dict)]
    if sources:
        lines.append("  - 检索来源：")
        for source in sources:
            lines.append(
                "    - " + "；".join([
                    f"轨道：{display(source.get('track'))}",
                    f"城市：{display(source.get('city') or source.get('city_id'))}",
                    f"关键词：{display(source.get('keyword'))}（ID：{display(source.get('keyword_id'))}）",
                    f"薪资筛选：{display(source.get('salary_filter'))}（ID：{display(source.get('salary_id'))}）",
                    f"经验筛选：{display(source.get('experience_filter'))}（ID：{display(source.get('experience_id'))}）",
                    f"学历筛选：{display(source.get('degree'))}",
                    f"检索组合：{display(source.get('combination'))}",
                    f"文件：{display(source.get('source_file'))}",
                    f"源行号：{display(source.get('source_line_number'))}",
                ])
            )
    else:
        lines.append("  - 检索来源：未提供（待核实）")

    dimensions = job.get("jd_dimensions") or {}
    dimension_labels = []
    for name in DIMENSIONS:
        dimension = dimensions.get(name) or {}
        present = dimension.get("present")
        status = "命中" if present is True else "未命中" if present is False and jd else "待核实"
        dimension_labels.append(f"{name}：{status}")
    lines.extend([
        f"  - 六项命中（{count}/6）：{'；'.join(dimension_labels)}",
        f"  - 目标方向判断：{display((job.get('target_relevance') or {}).get('assessment'))}；职责依据：{display((job.get('target_relevance') or {}).get('evidence'))}",
        f"  - JD证据：{display(job.get('jd_evidence'))}",
        f"  - 模型证据：{display(job.get('model_evidence'))}",
        f"  - 能力差距：{display(job.get('ability_gaps') or job.get('gaps'))}",
        f"  - 薪资状态：{display(job.get('salary_status'))}；经验标签（列表/检索）：{display(extract_experience_labels(job))}",
        f"  - 代码经验评估：JD最低年限：{display(job.get('jd_minimum_years'))}；经验依据：{display(job.get('jd_experience_evidence'))}",
    ])

    review = job.get("llm_review") or {}
    experience = review_experience_assessment(review)
    hard_conflict = experience.get("hard_conflict")
    conflict_label = "是" if hard_conflict is True else "否" if hard_conflict is False else "待核实"
    lines.append(
        f"  - 模型经验评估：最低年限：{display(experience.get('minimum_years'))}；硬性冲突：{conflict_label}；经验依据：{display(experience.get('evidence'))}"
    )
    model_status = text(job.get("model_status")) or "not_run"
    model_label = {
        "success": "success（语义复核成功）",
        "failed": "failed（规则回退）",
        "not_run": "not_run（未运行，规则回退）",
    }.get(model_status, model_status)
    if model_status == "success" and job.get("model_cache_validation") == "legacy_unverified":
        model_label = "success（历史语义结论，缓存未验证）"
    completeness = {
        "complete": f"完整（正文 {len(jd)} 字符）",
        "partial": f"部分（正文 {len(jd)} 字符；待补齐完整正文）",
        "missing": "缺失（JD正文未提供，待核实）",
        "unknown": f"待核实（正文 {len(jd)} 字符；旧数据未记录完整度）",
    }[_report_jd_completeness(job)]
    lines.append(f"  - JD完整度：{completeness}；模型状态：{display(model_label)}；模型证据质量：{display(review.get('evidence_quality'))}")
    if review.get("summary"):
        lines.append(f"  - 模型摘要：{display(review.get('summary'))}")
    verification = list(job.get("verification") or job.get("verify") or [])
    if not jd:
        verification.append("JD正文未提供，六项职责及岗位要求待核实")
    if model_status != "success":
        verification.append("模型未完成，能力差距与语义结论待核实")
    lines.append(f"  - 待核实：{display(list(dict.fromkeys(verification)))}")
    return lines


def report_prompt(prompt_file: Path | None = None) -> str | None:
    """Read one local prompt snapshot without resolving a model or credentials."""
    try:
        return Path(prompt_file).read_text(encoding="utf-8") if prompt_file is not None else load_prompt()
    except (OSError, UnicodeError):
        return None


def report_manual_choice(item: dict[str, Any]) -> bool | None:
    """Return only a current, explicitly confirmed AI-stage manual decision.

    ``ReviewTask.all_jobs`` supplies this per-job audit from the validated
    manifest. A saved intent outside the current input/result, a stale result,
    or an unconfirmed selection must never authorize the actionable pool.
    """
    audit = item.get("review_selection")
    selection = audit.get("llm") if isinstance(audit, dict) else None
    if (isinstance(selection, dict)
            and selection.get("status") == "completed"
            and selection.get("in_input") is True and selection.get("has_result") is True
            and selection.get("confirmed") is True and selection.get("pending") is False
            and type(selection.get("manual_keep")) is bool
            and selection.get("keep") is selection["manual_keep"]):
        return selection["manual_keep"]
    return None


def report_decision(item: dict[str, Any]) -> dict[str, Any]:
    """Project report eligibility while preserving the original audit fields."""
    result = dict(item)
    manual = report_manual_choice(result)
    # Recompute derived provenance instead of trusting a previous report's label.
    result.pop("report_manual_keep", None)
    result.pop("report_decision_source", None)
    if result.get("excluded_at"):
        classification = "暂不投"
        result["report_decision_source"] = "confirmed_exclusion"
    elif manual is not None:
        classification = "海投池" if manual else "暂不投"
    elif result.get("analysis_pending") or result.get("ai_judgment") == "信息不足":
        classification = "待核实"
    elif result.get("review_format") == "simple-v1":
        # A previous manual classification is derived output, not authority.
        # Once its confirmation is gone, recover only the actual AI decision.
        classification = {"符合": "海投池", "不符合": "暂不投"}.get(result.get("ai_judgment"), "待核实")
    else:
        classification = result.get("classification")
    if manual is not None:
        result["report_manual_keep"] = manual
        if not result.get("excluded_at") or result.get("excluded_at") == "llm":
            result["report_decision_source"] = "manual_llm_confirmed"
    if classification != result.get("classification"):
        result.setdefault("rule_classification", result.get("classification"))
        result["classification"] = classification
    return result


def report_pool_eligible(item: dict[str, Any]) -> bool:
    """Use the same actionable-pool boundary for CLI and desktop reports."""
    decision = report_decision(item)
    return (decision.get("classification") in ("重点投递", "海投池")
            and (report_manual_choice(decision) is True or (
                not decision.get("analysis_pending") and decision.get("ai_judgment") != "信息不足"))
            and not decision.get("excluded_at"))


def analyze_report_job(job: dict[str, Any], review: dict[str, Any] | None, *,
                       prompt: str | None,
                       analyzer: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Share offline cache validation across CLI and confirmed desktop reports."""
    from review_cache import review_for_report

    effective, validation = review_for_report(review, model_input(job), prompt)
    if review and review.get("status") == "success" and detail_completeness(job) != "complete":
        effective, validation = None, "incomplete_jd"
    notes = {
        "legacy_unverified": "历史模型记录未按当前两字段协议验证；仅兼容展示，需重跑AI复核",
        "missing_signature": "模型缓存签名不完整，未采用旧缓存；请重跑AI复核",
        "prompt_unavailable": "当前提示词不可读取，无法验证模型缓存，未采用旧缓存；请检查后重跑AI复核",
        "input_changed": "模型缓存与当前JD、岗位输入或提示词/schema不一致，未采用旧缓存；请重跑AI复核",
        "incomplete_jd": "JD正文缺失或部分可用，不使用历史模型结论；补齐正文后重跑AI复核",
        "invalid_review_format": "模型记录不符合当前两字段协议，待重新复核",
    }
    rejected = validation not in {"not_applicable", "input_match", "legacy_unverified"}
    if rejected:
        effective = {"status": "failed", "error": notes[validation], "needs_human_review": True}
        from llm_client import SIMPLE_REVIEW_FORMAT
        if review and (review.get("review_format") == SIMPLE_REVIEW_FORMAT or "判断" in review or "理由" in review):
            effective["review_format"] = SIMPLE_REVIEW_FORMAT
    item = (analyzer or merge_model_review)(job, effective)
    item["model_cache_validation"] = validation
    item["model_cache_input_verified"] = validation == "input_match"
    if validation in notes:
        verification = list(dict.fromkeys([*(item.get("verification") or []), notes[validation]]))
        item["verification"] = item["verify"] = verification
        item["verification_items"] = list(dict.fromkeys([*(item.get("verification_items") or []), notes[validation]]))
    return report_decision(item)


def report_cache_statistics(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(job.get("model_cache_validation", "not_applicable") for job in jobs)
    rejected = {"missing_signature", "prompt_unavailable", "input_changed", "incomplete_jd", "invalid_review_format"}
    return {"model_cache_validation_counts": dict(counts),
            "model_cache_input_validated_count": counts["input_match"],
            "model_cache_legacy_unverified_count": counts["legacy_unverified"],
            "model_cache_rejected_count": sum(counts[state] for state in rejected)}


def report_stage(output_root: Path, *, prompt_file: Path | None = None) -> dict[str, Any]:
    from llm_client import SIMPLE_REVIEW_FORMAT

    merged_path = output_root / "prepared" / "merged_jobs_index.json"
    data = read_json(merged_path)
    jobs = data.get("jobs", [])
    reviews = {}
    review_path = output_root / "prepared" / "llm_reviews.json"
    if review_path.exists():
        try: reviews = {text(x.get("job_id")): x for x in read_json(review_path).get("reviews", []) if isinstance(x, dict)}
        except Exception: reviews = {}
    # A current run may have no per-job records (configuration failed, no
    # complete JD, or interrupted before the first result). Its local manifest
    # establishes only a pending two-field context, never an AI success.
    simple_run = False
    manifest_path = output_root / "prepared" / "llm_manifest.json"
    if manifest_path.exists():
        try:
            manifest = read_json(manifest_path)
            simple_run = isinstance(manifest, dict) and manifest.get("review_format") == SIMPLE_REVIEW_FORMAT
        except (OSError, UnicodeError, ValueError):
            pass
    details = load_details(output_root / "prepared" / "jd_shortlists" / "details.json")
    prompt = (report_prompt(prompt_file) if prompt_file is not None else report_prompt()) \
        if any(review.get("status") == "success" for review in reviews.values()) else None
    final = []
    for job in jobs:
        item = dict(job)
        if simple_run:
            item["review_format"] = SIMPLE_REVIEW_FORMAT
        detail = find_detail(item, details)
        if detail:
            item["jd"] = text(detail.get("jd"))
            item["jd_completeness"] = detail_completeness(detail)
        elif detail_completeness(item) == "partial":
            # merge_model_review refreshes the fetch label, so retain older
            # partial-status evidence in the explicit quality field first.
            item["jd_completeness"] = "partial"
        key = text(item.get("job_id") or item.get("dedupe_key"))
        final.append(analyze_report_job(item, reviews.get(key), prompt=prompt))
    counts = Counter(item.get("classification") for item in final)
    metadata = dict(data.get("metadata") or {})
    queue_counter = Counter(job["list_queue"] for job in final)
    classifications = (*CLASSIFICATIONS, "待核实")
    all_counts = {name: counts.get(name, 0) for name in classifications}
    pending_count = counts.get("待核实", 0)
    metadata.update({"generated_at": now_iso(), "screening_version": SCREENING_VERSION, "list_pass_count": sum(job["list_pass"] for job in final), "list_priority_counts": dict(Counter(job["list_priority"] for job in final)), "list_queue_counts": {queue: queue_counter[queue] for queue in LIST_QUEUE_LABELS}, "unique_job_count": len(final), "classification_counts": all_counts, "pending_verification_count": pending_count, "analysis_pending_count": sum(bool(job.get("analysis_pending")) for job in final), "jd_fetched_count": sum(bool(x.get("jd_fetched")) for x in final)})
    metadata.update(report_cache_statistics(final))
    write_json(merged_path, {"metadata": metadata, "jobs": final})
    pool = [x for x in final if report_pool_eligible(x)]
    pool_counts = {name: sum(job.get("classification") == name for job in pool) for name in CLASSIFICATIONS}
    write_json(output_root / "prepared" / "hai_tou_pool.json", {"metadata": {**metadata, "unique_job_count": len(pool), "total_imported_jobs": len(final), "classification_counts": pool_counts, "all_classification_counts": all_counts}, "summary": {"total": len(pool), "classification_counts": pool_counts}, "jobs": pool})
    write_json(output_root / "prepared" / "current_candidates.json", {"total": sum("current" in {x.get("track") for x in job.get("observed_in", [])} for job in final), "jobs": [x for x in final if "current" in {y.get("track") for y in x.get("observed_in", [])}]})
    write_json(output_root / "prepared" / "target_candidates.json", {"total": sum("target" in {x.get("track") for x in job.get("observed_in", [])} for job in final), "jobs": [x for x in final if "target" in {y.get("track") for y in x.get("observed_in", [])}]})
    def city_name(job: dict[str, Any]) -> str:
        value = text(job.get("city") or job.get("location"))
        if not value:
            observed = job.get("observed_in") or []
            value = text(observed[0].get("city")) if observed and isinstance(observed[0], dict) else ""
        return value.split("·", 1)[0] if value else "未知城市"

    lines = ["# BOSS 岗位海投分层清单", "", f"生成时间：{metadata['generated_at']}", ""]
    lines.extend(_report_overview_lines(final, pool))
    lines.extend([f"待核实：{pending_count} 岗（AI信息不足或AI未完成，不计入可投清单）。", ""])
    lines.extend([f"列表初筛版本：{SCREENING_VERSION}；通过 {metadata['list_pass_count']} 岗。分值仅用于排序；低优先过渡候选不代表AI相关或已经胜任。缺JD的海投池岗位均待核实，不计作已确认职责匹配。", "", "以下为全部岗位详细证据。", ""])
    for classification in classifications:
        lines.extend([f"## {classification}（{counts.get(classification, 0)}）", ""])
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for job in final:
            if job.get("classification") == classification:
                grouped[city_name(job)].append(job)
        for city in sorted(grouped):
            lines.extend([f"### {_report_markdown(city)}（{len(grouped[city])}）", ""])
            for job in sorted(grouped[city], key=lambda x: (-int(x.get("score") or 0), text(x.get("title")))):
                lines.extend(_report_job_lines(job))
            lines.append("")
        lines.append("")
    (output_root / "prepared" / "海投分层清单.md").write_text("\n".join(lines), encoding="utf-8")
    return {"unique_job_count": len(final), "classification_counts": all_counts, "pending_verification_count": pending_count, "analysis_pending_count": metadata["analysis_pending_count"], "pool_count": len(pool), "jd_fetched_count": metadata["jd_fetched_count"], "screening_version": SCREENING_VERSION, "list_pass_count": metadata["list_pass_count"], "list_queue_counts": metadata["list_queue_counts"]}


def output_root_for(
    source_root: Path,
    city: str | None,
    sample_size: int | None,
    run_timestamp: str | None = None,
) -> Path:
    if run_timestamp is not None:
        if not city or sample_size is None:
            raise ValueError("独立运行目录需要同时提供 city 和 sample_size")
        if not re.fullmatch(r"\d{8}-\d{6}", run_timestamp):
            raise ValueError("run_timestamp 必须使用 YYYYMMDD-HHMMSS 格式")
        normalized = text(city).strip().lower()
        reverse_city = {value.lower(): key for key, value in CITY_NAMES.items()}
        safe = CITY_NAMES.get(reverse_city.get(normalized, ""), text(city).strip())
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", safe).strip(" -.") or "sample"
        return source_root / "runs" / f"{run_timestamp}-{safe}-{sample_size}"
    if city and sample_size:
        normalized = text(city).strip().lower()
        reverse_city = {value.lower(): key for key, value in CITY_NAMES.items()}
        safe = reverse_city.get(normalized, normalized)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", safe).strip("-") or "sample"
        return source_root / "test-runs" / f"{safe}-{sample_size}"
    return source_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BOSS 岗位后处理与本地 Codex 语义筛选")
    parser.add_argument("--root", type=Path, required=True, help="原始采集根目录")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--city", default=None)
    parser.add_argument("--sample-size", type=int, default=None)
    parser.add_argument("--sample-strategy", choices=("sequential", "balanced-dedup"), default="sequential")
    parser.add_argument("--tracks", choices=("current", "target", "all"), default="all")
    parser.add_argument("--output-root", type=Path, default=None, help="后处理输出目录；指定时覆盖默认 test-runs 路径")
    parser.add_argument("--run-timestamp", default=None, help="使用 runs/YYYYMMDD-HHMMSS-城市-数量 独立输出目录")
    parser.add_argument("--stage", choices=("merge", "shortlist", "fetch-jd", "llm", "report", "all"), default="all")
    parser.add_argument("--workers", type=int, default=AI_DEFAULT_WORKERS, help="AI 请求并发，默认 10")
    parser.add_argument("--conversation-size", type=int, default=1,
                        help="每个并发 AI 会话最多连续复核岗位数；默认 1，每岗独立，不复用历史")
    parser.add_argument("--jd-workers", type=int, default=JD_DEFAULT_WORKERS, help="JD 同时打开的页面数，1–32，默认 3")
    parser.add_argument("--browser-mode", choices=("single", "dual"), default="single",
                        help="JD 使用单浏览器或 Chrome+Edge 双浏览器（默认 single）")
    parser.add_argument("--chrome-port", type=int, default=9222)
    parser.add_argument("--edge-port", type=int, default=9223)
    parser.add_argument("--chrome-profile-dir", type=Path, default=None)
    parser.add_argument("--edge-profile-dir", type=Path, default=None)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--detail-speed-profile", choices=("safe", "fast"), default="fast")
    parser.add_argument("--scraper-root", type=Path, default=None)
    parser.add_argument("--python", dest="python_path", default=None)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--codex-home", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--api-mode", choices=project_llm_settings.API_MODES, default=None,
                        help="API 协议；默认读取项目本机配置，旧配置为 responses；chat_completions 不发送思考强度")
    parser.add_argument("--prompt-file", type=Path, default=None,
                        help="使用已保存的完整复核提示词快照；默认使用项目提示词")
    parser.add_argument("--model", default=None,
                        help=f"覆盖 AI 语义复核模型；默认读取项目本机配置，无配置时为 {AI_DEFAULT_MODEL}")
    parser.add_argument("--reasoning-effort", default=None,
                        help=f"覆盖 AI 思考强度；默认读取项目本机配置，无配置时为 {AI_DEFAULT_REASONING_EFFORT}")
    gate_wait = parser.add_mutually_exclusive_group()
    gate_wait.add_argument("--wait-on-gate", dest="wait_on_gate", action="store_true", help="遇到登录或安全验证时等待人工完成后按 Enter 重试（默认启用）")
    gate_wait.add_argument("--no-wait-on-gate", dest="wait_on_gate", action="store_false", help="遇到登录或安全验证时保存 paused 并立即返回")
    parser.add_argument("--gui-interactive", action="store_true", help="允许桌面 GUI 通过 stdin 管道发送安全验证后的继续信号")
    parser.set_defaults(wait_on_gate=True)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error("--workers 须为 1–32")
    if not 1 <= args.conversation_size <= 100:
        parser.error("--conversation-size 须为 1–100")
    if not 1 <= args.jd_workers <= MAX_WORKERS:
        parser.error("--jd-workers 须为 1–32")
    if not 1 <= args.batch_size <= 100:
        parser.error("--batch-size 须为 1–100")
    source_root = args.root.expanduser().resolve()
    out = args.output_root.expanduser().resolve() if args.output_root else output_root_for(source_root, args.city, args.sample_size, args.run_timestamp)
    out.mkdir(parents=True, exist_ok=True)
    if args.stage in ("merge", "shortlist", "all"):
        merge_stage(
            source_root,
            out,
            args.config.expanduser().resolve(),
            args.city,
            args.sample_size,
            sample_strategy=args.sample_strategy,
            tracks=args.tracks,
        )
    if args.stage in ("fetch-jd", "all"):
        scraper = args.scraper_root.expanduser().resolve() if args.scraper_root else Path(os.environ.get("BOSS_SCRAPER_ROOT", ""))
        if not scraper:
            raise SystemExit("fetch-jd 需要 --scraper-root 或 BOSS_SCRAPER_ROOT")
        detail_result = fetch_jd(
            out,
            scraper,
            args.batch_size,
            args.python_path,
            wait_on_gate=args.wait_on_gate,
            detail_speed_profile=args.detail_speed_profile,
            gui_interactive=args.gui_interactive,
            jd_workers=args.jd_workers,
            browser_mode=args.browser_mode,
            chrome_port=args.chrome_port,
            edge_port=args.edge_port,
            chrome_profile_dir=args.chrome_profile_dir,
            edge_profile_dir=args.edge_profile_dir,
        )
        records = detail_result.get("manifest", {}).get("records", [])
        paused = any(record.get("status") == "paused" for record in records)
        fatal = any(record.get("status") == "failed" and record.get("exit_code") not in (None, 0)
                    for record in records)
        if fatal or (paused and args.stage == "all"):
            print(json.dumps({"output_root": str(out), "stage": args.stage,
                              "status": "paused" if paused else "failed"}, ensure_ascii=False))
            return 1 if fatal and not paused else 0
    if args.stage in ("llm", "all"):
        prompt_options = {"prompt_file": args.prompt_file} if args.prompt_file is not None else {}
        if args.api_mode is not None:
            prompt_options["api_mode"] = args.api_mode
        run_llm(out, args.workers, args.timeout, args.max_retries, args.codex_home, args.base_url, args.api_key, args.model, args.reasoning_effort, conversation_size=args.conversation_size, **prompt_options)
    if args.stage in ("report", "all"):
        report_stage(out, prompt_file=args.prompt_file) if args.prompt_file is not None else report_stage(out)
    print(json.dumps({"output_root": str(out), "stage": args.stage}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
