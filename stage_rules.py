"""Local, typed configuration for the three deterministic review stages.

Rules only influence recommendations and browser rendering parameters. They
never change a task's source/range, job identity, or a durable manual choice.
"""

from __future__ import annotations

import copy
from typing import Any


RULE_STAGES = ("import", "shortlist", "fetch-jd")
SHORTLIST_MATCH_FIELDS = ("title", "skills", "skill_tags", "job_labels")
IMPORT_COMPANY_FIELDS = (
    "company", "company_name", "companyName", "brand_name", "brandName",
    "company_full_name", "companyFullName", "company_short_name", "companyShortName",
    "brand_full_name", "brandFullName", "brand_short_name", "brandShortName",
    "company_info", "companyInfo", "brand_info", "brandInfo",
)
IMPORT_COMPANY_NAME_FIELDS = (
    "name", "company_name", "companyName", "brand_name", "brandName",
    "full_name", "fullName", "short_name", "shortName",
    "company_full_name", "companyFullName", "company_short_name", "companyShortName",
    "brand_full_name", "brandFullName", "brand_short_name", "brandShortName",
)


def stage_rule_fields(stage: str) -> list[dict]:
    """Return fresh, UI-independent field descriptions with strict types."""
    if stage == "import":
        return [
            {"key": "require_title", "label": "要求职位名称", "type": "bool",
             "help": "职位名称为空时建议剔除；记录和岗位 ID 仍保留。"},
            {"key": "exclude_title_keywords", "label": "职位排除词", "type": "keywords",
             "help": "在职位 title（含同名来源候选值）中按字面匹配，不区分大小写。"},
            {"key": "exclude_company_keywords", "label": "公司排除词", "type": "keywords",
             "help": "在 company、company_name、brand_name 等公司名称字段（含来源候选值）中按字面匹配，不区分大小写；不匹配招聘者姓名。"},
        ]
    if stage == "shortlist":
        return [
            {"key": "use_default_direction_rules", "label": "使用默认方向规则", "type": "bool",
             "help": "启用项目内置方向、岗位类型和薪资规则；关闭后仅用自定义词表及纯开发开关。"},
            {"key": "exclude_pure_development", "label": "排除纯开发岗位", "type": "bool",
             "help": "按职位与技能判断纯开发；关闭后允许进入待核实队列。"},
            {"key": "keep_verification", "label": "建议保留待核实岗位", "type": "bool",
             "help": "关闭后待核实队列保持不变，但不建议保留；人工选择优先。"},
            {"key": "include_keywords", "label": "包含词", "type": "keywords",
             "help": "至少命中一词。仅匹配 title、skills、skill_tags、job_labels 及同名来源候选值，不读取 JD；为空时沿用已启用的默认方向，关闭默认规则则不限包含词。"},
            {"key": "exclude_keywords", "label": "排除词", "type": "keywords",
             "help": "命中任一词优先剔除。与包含词使用相同列表字段，按字面匹配，不区分大小写。"},
        ]
    if stage == "fetch-jd":
        return [
            {"key": "scroll_count", "label": "每页滚动次数", "type": "int", "min": 1, "max": 20,
             "help": "实际详情页滚动次数，1–20，默认 3。"},
            {"key": "detail_speed_profile", "label": "详情渲染速度", "type": "choice",
             "choices": ["safe", "fast"], "help": "fast 使用较短渲染等待，safe 使用较长等待。"},
            {"key": "page_wait_min", "label": "每页等待最短秒数", "type": "int", "min": 0, "max": 30,
             "help": "每个 JD 页面打开前的额外随机等待下限，0–30 秒，默认 0。"},
            {"key": "page_wait_max", "label": "每页等待最长秒数", "type": "int", "min": 0, "max": 30,
             "help": "额外等待上限，0–30 秒且不小于下限；两者相同为固定等待，0 / 0 不额外等待。等待中可暂停或停止，原有页面渲染等待仍保留。"},
        ]
    raise ValueError("此步骤没有可编辑的阶段参数。")


def default_stage_rules(stage: str) -> dict:
    """Return independent defaults; older tasks have exactly this behavior."""
    if stage == "import":
        return {"require_title": False, "exclude_title_keywords": [], "exclude_company_keywords": []}
    if stage == "shortlist":
        return {"use_default_direction_rules": True, "exclude_pure_development": True,
                "keep_verification": True, "include_keywords": [], "exclude_keywords": []}
    if stage == "fetch-jd":
        return {"scroll_count": 3, "detail_speed_profile": "fast", "page_wait_min": 0, "page_wait_max": 0}
    raise ValueError("此步骤没有可编辑的阶段参数。")


def validate_stage_rules(stage: str, value: dict) -> dict:
    """Normalize a copy, filling omitted fields and rejecting unknown data.

    Keyword lists are literal strings: whitespace-only entries are ignored,
    and duplicates are removed in first-occurrence order, ignoring case.
    ``None`` is reserved for ReviewTask.set_stage_rules' restore operation.
    """
    fields = stage_rule_fields(stage)
    if not isinstance(value, dict):
        raise ValueError("阶段规则必须是对象。")
    known = {field["key"] for field in fields}
    if any(key not in known for key in value):
        raise ValueError("阶段规则含未知字段。")
    normalized = default_stage_rules(stage)
    for field in fields:
        key, kind = field["key"], field["type"]
        if key not in value:
            continue
        item = value[key]
        if kind == "bool":
            if type(item) is not bool:
                raise ValueError(f"{field['label']}必须为布尔值。")
        elif kind == "int":
            if type(item) is not int or not field["min"] <= item <= field["max"]:
                raise ValueError(f"{field['label']}必须为 {field['min']}–{field['max']} 的整数。")
        elif kind == "choice":
            if not isinstance(item, str) or item not in field["choices"]:
                raise ValueError(f"{field['label']}选项无效。")
        elif kind == "keywords":
            if not isinstance(item, list) or any(not isinstance(term, str) for term in item):
                raise ValueError(f"{field['label']}必须为字符串词表。")
            words, seen = [], set()
            for term in item:
                word = term.strip()
                if word and word.casefold() not in seen:
                    words.append(word)
                    seen.add(word.casefold())
            item = words
        normalized[key] = copy.deepcopy(item)
    if stage == "fetch-jd" and normalized["page_wait_min"] > normalized["page_wait_max"]:
        raise ValueError("每页等待最短秒数不能大于最长秒数。")
    return normalized


def _literal_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " | ".join(_literal_text(item) for item in value)
    return ""


def _import_values(job: dict, fields: tuple[str, ...]) -> list[str]:
    values = []
    candidates = job.get("field_candidates")
    for field in fields:
        values.append(_literal_text(job.get(field)))
        entries = candidates.get(field) if isinstance(candidates, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict):
                values.append(_literal_text(entry.get("value")))
    return values


def _company_text(value: Any) -> str:
    if isinstance(value, dict):
        return " | ".join(_literal_text(value.get(key)) for key in IMPORT_COMPANY_NAME_FIELDS)
    return _literal_text(value)


def _import_companies(job: dict) -> list[str]:
    values = []
    candidates = job.get("field_candidates")
    for field in IMPORT_COMPANY_FIELDS:
        values.append(_company_text(job.get(field)))
        entries = candidates.get(field) if isinstance(candidates, dict) else None
        if isinstance(entries, list):
            values.extend(_company_text(entry.get("value")) for entry in entries if isinstance(entry, dict))
    return values


def apply_import_rules(job: dict, rules: dict) -> dict:
    """Annotate a copied row, retaining every identity and source field."""
    config = validate_stage_rules("import", rules)
    if not isinstance(job, dict):
        raise ValueError("岗位记录必须是对象。")
    result = copy.deepcopy(job)
    titles = _import_values(job, ("title",))
    companies = _import_companies(job)
    reasons = []
    if config["require_title"] and not any(value.strip() for value in titles):
        reasons.append("职位名称为空")
    for key, values, label in (("exclude_title_keywords", titles, "职位排除词"),
                               ("exclude_company_keywords", companies, "公司排除词")):
        hits = [term for term in config[key] if any(term.casefold() in value.casefold() for value in values)]
        if hits:
            reasons.append(f"命中{label}：" + "、".join(hits))
    result["import_rule_keep"] = not reasons
    result["import_rule_reason"] = "；".join(reasons) or "符合导入规则，建议保留"
    return result
