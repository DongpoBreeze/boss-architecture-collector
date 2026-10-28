"""Pure-data editing, validation and safe snapshots for collection plans.

The original arrays are a source-identification catalog, not just the current
selection. Removing a keyword from a plan must never erase its old source ID.
This module deliberately does not import the collector, browser or any client.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


TRACKS = ("current", "target")
TRACK_LABELS = {"current": "当前可投", "target": "成长目标"}
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_RESERVED_IDS = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是对象")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空，且必须是文本")
    result = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in result):
        raise ValueError(f"{label}不能包含换行或控制字符")
    return result


def _identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not _SAFE_ID.fullmatch(value)
        or "__" in value
        or value.lower() in _RESERVED_IDS
    ):
        raise ValueError(f"{label}不安全：仅允许字母、数字、单个下划线或连字符，长度不超过64，且不能是保留名称")
    return value


def _code(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{label}必须是数字编码")
    result = str(value)
    if not re.fullmatch(r"[0-9]+", result):
        raise ValueError(f"{label}必须是数字编码")
    return result


def _catalog(value: Any, kind: str, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{label}必须是列表")
    result: list[dict[str, str]] = []
    ids: set[str] = set()
    queries: set[str] = set()
    for item in value:
        item = _mapping(item, f"{label}选项")
        identifier = _identifier(item.get("id"), f"{label}ID")
        if identifier.casefold() in ids:
            raise ValueError(f"{label}存在重复ID（大小写也不能重复）")
        ids.add(identifier.casefold())
        normalized = {"id": identifier}
        if kind == "city":
            normalized["name"] = _text(item.get("name"), "城市名称")
        elif kind == "keyword":
            query = _text(item.get("query"), f"{label}内容")
            if query in queries:
                raise ValueError(f"{label}存在重复关键词")
            queries.add(query)
            normalized["query"] = query
        else:
            normalized["label"] = _text(item.get("label", identifier), f"{label}显示名称")
            normalized["code"] = _code(item.get("code"), f"{label}编码")
        result.append(normalized)
    return result


def _selected_ids(value: Any, options: list[dict[str, Any]], label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label}必须是ID列表")
    allowed = {item["id"] for item in options}
    result: list[str] = []
    seen: set[str] = set()
    for identifier in value:
        identifier = _identifier(identifier, f"{label}ID")
        if identifier.casefold() in seen:
            raise ValueError(f"{label}存在重复ID")
        if identifier not in allowed:
            raise ValueError(f"{label}包含配置中不存在的选项")
        seen.add(identifier.casefold())
        result.append(identifier)
    return result


def _selection(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize optional selection fields; missing is all, [] is never all."""

    supplied = _mapping(config.get("collection_selection", {}), "采集条件")
    result: dict[str, Any] = {}
    for key, label in (("cities", "所选城市"), ("salaries", "所选薪资")):
        options = config[key]
        result[key] = _selected_ids(supplied.get(key, [item["id"] for item in options]), options, label)
        if not result[key]:
            raise ValueError(f"请至少选择一个{('城市' if key == 'cities' else '薪资范围')}")
    for track in TRACKS:
        track_selection = _mapping(supplied.get(track, {}), f"{TRACK_LABELS[track]}采集条件")
        result[track] = {}
        for key, label in (("keywords", "关键词"), ("experiences", "经验")):
            options = config[track][key]
            result[track][key] = _selected_ids(
                track_selection.get(key, [item["id"] for item in options]),
                options,
                f"{TRACK_LABELS[track]}所选{label}",
            )
    if not any(result[track]["keywords"] and result[track]["experiences"] for track in TRACKS):
        raise ValueError("采集方案没有可执行组合：至少一个轨道需同时选择关键词和经验")
    combinations = supplied.get("combinations")
    if combinations is None:
        result["combinations"] = None
        return result
    if not isinstance(combinations, list):
        raise ValueError("逐组选择必须是组合列表，或使用 null 表示全部组合")
    if not combinations:
        raise ValueError("请至少勾选一个采集组合")
    selected: list[str] = []
    seen_combinations: set[str] = set()
    for combination in combinations:
        if not isinstance(combination, str) or not combination:
            raise ValueError("采集组合不能为空，且必须是组合ID文本")
        if combination in seen_combinations:
            raise ValueError("逐组选择中存在重复组合")
        parts = combination.split("__")
        if len(parts) != 5:
            raise ValueError("采集组合不存在或格式不正确")
        track, city, keyword, salary, experience = parts
        if (
            track not in TRACKS
            or city not in result["cities"]
            or salary not in result["salaries"]
            or keyword not in result[track]["keywords"]
            or experience not in result[track]["experiences"]
        ):
            raise ValueError("采集组合不存在或不符合当前所选城市、薪资、关键词和经验")
        seen_combinations.add(combination)
        selected.append(combination)
    result["combinations"] = selected
    return result


def validate_plan_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a validated detached whitelist copy, never arbitrary raw fields.

    The two track catalogs may share a keyword ID only for the same query.
    A single empty track is valid, but a plan with no executable group is not.
    """

    config = _mapping(config, "采集方案")
    result: dict[str, Any] = {
        "schema_version": _text(config.get("schema_version", "1.0"), "配置版本"),
    }
    degree = _mapping(config.get("degree", {"id": "203", "name": "本科"}), "学历配置")
    degree_id = _identifier(degree.get("id", "203"), "学历ID")
    result["degree"] = {"id": _code(degree_id, "学历ID"), "name": _text(degree.get("name", degree_id), "学历名称")}
    result["cities"] = _catalog(config.get("cities"), "city", "城市目录")
    result["salaries"] = _catalog(config.get("salaries"), "salary", "薪资目录")
    if not result["cities"]:
        raise ValueError("城市目录不能为空，请至少保留一个城市")
    if not result["salaries"]:
        raise ValueError("薪资目录不能为空，请至少保留一个薪资范围")
    keyword_ids: dict[str, tuple[str, str]] = {}
    query_ids: dict[str, str] = {}
    experience_codes: dict[str, tuple[str, str]] = {}
    for track in TRACKS:
        track_config = _mapping(config.get(track), f"{TRACK_LABELS[track]}配置")
        keywords = _catalog(track_config.get("keywords"), "keyword", f"{TRACK_LABELS[track]}关键词目录")
        experiences = _catalog(track_config.get("experiences"), "experience", f"{TRACK_LABELS[track]}经验目录")
        for keyword in keywords:
            identifier, query = keyword["id"], keyword["query"]
            previous = keyword_ids.get(identifier.casefold())
            if previous is not None and previous != (identifier, query):
                raise ValueError("不同轨道的同一关键词ID必须对应同一关键词内容，不能修改旧ID的含义")
            if query in query_ids and query_ids[query] != identifier:
                raise ValueError("相同关键词内容必须复用同一旧ID")
            keyword_ids[identifier.casefold()] = (identifier, query)
            query_ids[query] = identifier
        for experience in experiences:
            identifier, code = experience["id"], experience["code"]
            previous = experience_codes.get(identifier.casefold())
            if previous is not None and previous != (identifier, code):
                raise ValueError("不同轨道的同一经验ID必须对应同一经验编码")
            experience_codes[identifier.casefold()] = (identifier, code)
        result[track] = {"keywords": keywords, "experiences": experiences}
    selection = _selection({**result, **({"collection_selection": config["collection_selection"]} if "collection_selection" in config else {})})
    if "collection_selection" in config:
        result["collection_selection"] = selection
    return result


def plan_editor_values(config: dict[str, Any]) -> dict[str, Any]:
    """Return only editor options and selected values; no paths or credentials."""

    config = validate_plan_config(config)
    selection = _selection(config)
    result: dict[str, Any] = {
        "cities": [{"id": item["id"], "label": item["name"]} for item in config["cities"]],
        "salaries": [{"id": item["id"], "label": item["label"]} for item in config["salaries"]],
        "experiences": {
            track: [{"id": item["id"], "label": item["label"]} for item in config[track]["experiences"]]
            for track in TRACKS
        },
        "city_ids": list(selection["cities"]),
        "salary_ids": list(selection["salaries"]),
    }
    for track in TRACKS:
        keyword_queries = {item["id"]: item["query"] for item in config[track]["keywords"]}
        result[track] = {
            "keywords": [keyword_queries[identifier] for identifier in selection[track]["keywords"]],
            "experience_ids": list(selection[track]["experiences"]),
        }
    return result


def _keyword_queries(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label}必须是关键词文本列表")
    result: list[str] = []
    seen: set[str] = set()
    for query in value:
        if isinstance(query, str) and not query.strip():
            continue
        query = _text(query, label)
        if query in seen:
            raise ValueError(f"{label}存在重复关键词")
        seen.add(query)
        result.append(query)
    return result


def update_plan_config(
    base: dict[str, Any],
    *,
    city_ids: list[str],
    salary_ids: list[str],
    current_keywords: list[str],
    current_experience_ids: list[str],
    target_keywords: list[str],
    target_experience_ids: list[str],
) -> dict[str, Any]:
    """Update selected dimensions, extending but never deleting old catalogs."""

    result = validate_plan_config(base)
    query_ids = {item["query"]: item["id"] for track in TRACKS for item in result[track]["keywords"]}
    id_queries = {item["id"].casefold(): item["query"] for track in TRACKS for item in result[track]["keywords"]}
    selection: dict[str, Any] = {
        "cities": _selected_ids(city_ids, result["cities"], "所选城市"),
        "salaries": _selected_ids(salary_ids, result["salaries"], "所选薪资"),
        "combinations": None,
    }
    inputs = {
        "current": (current_keywords, current_experience_ids),
        "target": (target_keywords, target_experience_ids),
    }
    for track, (keywords, experiences) in inputs.items():
        selected_keywords: list[str] = []
        track_ids = {item["id"] for item in result[track]["keywords"]}
        for query in _keyword_queries(keywords, f"{TRACK_LABELS[track]}关键词"):
            identifier = query_ids.get(query)
            if identifier is None:
                identifier = "k_" + hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
                if identifier.casefold() in id_queries and id_queries[identifier.casefold()] != query:
                    raise ValueError("新关键词ID与已有关键词冲突，无法安全保存")
                query_ids[query] = identifier
                id_queries[identifier.casefold()] = query
            if identifier not in track_ids:
                result[track]["keywords"].append({"id": identifier, "query": query})
                track_ids.add(identifier)
            selected_keywords.append(identifier)
        selection[track] = {
            "keywords": selected_keywords,
            "experiences": _selected_ids(experiences, result[track]["experiences"], f"{TRACK_LABELS[track]}所选经验"),
        }
    result["collection_selection"] = selection
    return validate_plan_config(result)


def select_plan_combinations(config: dict[str, Any], combinations: list[str] | None) -> dict[str, Any]:
    """Select exact groups (not a prefix), or reset to all selected dimensions."""

    result = validate_plan_config(config)
    selection = _selection(result)
    selection["combinations"] = copy.deepcopy(combinations)
    result["collection_selection"] = selection
    return validate_plan_config(result)


def _no_directory_links(directory: Path) -> None:
    """Inspect lexical ancestors before resolve can hide a Windows junction."""

    try:
        for ancestor in reversed((directory, *directory.parents)):
            try:
                info = ancestor.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise ValueError("方案保存目录含符号链接或目录联接（junction），已拒绝写入")
    except OSError:
        raise ValueError("无法安全检查方案保存目录，未写入采集方案") from None


def save_plan_config(config: dict[str, Any], directory: Path) -> Path:
    """Atomically publish a new unique JSON snapshot without replacing files."""

    config = validate_plan_config(config)
    directory = Path(directory).expanduser()
    if ".." in directory.parts:
        raise ValueError("方案保存目录不能包含上级路径（..），请使用普通文件夹的完整路径")
    directory = directory.absolute()
    _no_directory_links(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _no_directory_links(directory)
    directory = directory.resolve(strict=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".collection-plan-", suffix=".tmp", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for _attempt in range(100):
            path = directory / f"collection-plan-{datetime.now():%Y%m%d-%H%M%S-%f}-{uuid.uuid4().hex[:12]}.json"
            try:
                _no_directory_links(directory)
                if os.name == "nt":
                    # Windows rename refuses an existing destination atomically.
                    os.rename(temporary, path)
                else:
                    # POSIX rename replaces files; hard-link publication does not.
                    os.link(temporary, path)
                return path
            except FileExistsError:
                continue
        raise FileExistsError("无法创建唯一的采集方案文件，请重试")
    finally:
        temporary.unlink(missing_ok=True)


def filter_plan_specs(config: dict[str, Any], specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply dimensions and exact groups before external batch/city/limit."""

    config = validate_plan_config(config)
    if "collection_selection" not in config:
        return list(specs)
    selection = config["collection_selection"]
    combinations = selection["combinations"]
    exact_groups = None if combinations is None else set(combinations)
    cities, salaries = set(selection["cities"]), set(selection["salaries"])
    keywords = {track: set(selection[track]["keywords"]) for track in TRACKS}
    experiences = {track: set(selection[track]["experiences"]) for track in TRACKS}
    return [
        spec for spec in specs
        if spec.get("track") in TRACKS
        and spec.get("city_id") in cities
        and spec.get("salary") in salaries
        and spec.get("keyword_id") in keywords[spec["track"]]
        and spec.get("experience") in experiences[spec["track"]]
        and (exact_groups is None or spec.get("combination") in exact_groups)
    ]


def preview_rows(config: dict[str, Any], specs: list[dict[str, Any]], pages: int = 10) -> list[dict[str, Any]]:
    """Decorate already-expanded specs without changing their search identity."""

    config = validate_plan_config(config)
    if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= 10:
        raise ValueError("每组页数必须是1到10之间的整数")
    cities = {item["id"]: item["name"] for item in config["cities"]}
    salaries = {item["id"]: item["label"] for item in config["salaries"]}
    experiences = {track: {item["id"]: item["label"] for item in config[track]["experiences"]} for track in TRACKS}
    rows: list[dict[str, Any]] = []
    for spec in specs:
        track = spec.get("track")
        if (
            track not in TRACKS
            or spec.get("city_id") not in cities
            or spec.get("salary") not in salaries
            or spec.get("experience") not in experiences[track]
        ):
            raise ValueError("预览组合包含配置中不存在的选项")
        rows.append({
            **copy.deepcopy(spec),
            "track_label": TRACK_LABELS[track],
            "city_label": cities[spec["city_id"]],
            "salary_label": salaries[spec["salary"]],
            "experience_label": experiences[track][spec["experience"]],
            "degree_label": config["degree"]["name"],
            "pages_requested": pages,
        })
    return rows
