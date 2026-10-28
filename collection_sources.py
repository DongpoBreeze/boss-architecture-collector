"""Read collection-plan catalogs for a newly created review task.

Selections describe future searches, not which existing raw files may be read.
Keep every recorded keyword/experience ID when handing a source to review, so
adding a keyword in the collection UI cannot silently hide its collected jobs.
No source files or existing tasks are ever changed here.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from collection_plan import save_plan_config, validate_plan_config
from desktop_tasks import _absolute, _no_links


def assert_source_specs_compatible(source_root: Path, specs: list[dict[str, Any]]) -> None:
    """Refuse a changed meaning for an ID already recorded in this source.

    Normal UI keyword edits get a new content ID. This extra check also protects
    imported, hand-edited plans from reusing an old success for another query.
    It is read-only and must run before collection snapshots or raw are written.
    """
    source = _absolute(source_root)
    path = source / "collection_manifest.json"
    _no_links(path)
    if not path.exists():
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        records = manifest.get("records") if isinstance(manifest, dict) else None
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ValueError("invalid manifest")
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise ValueError("已有采集清单无法安全读取；未启动采集，也未覆盖旧结果。") from None
    old = {record.get("combination"): record for record in records if isinstance(record.get("combination"), str)}
    for spec in specs:
        prior = old.get(spec.get("combination"))
        if prior is None:
            continue
        for field in ("city_id", "city", "keyword_id", "keyword", "salary_code", "experience_code", "degree"):
            if field in prior and prior[field] is not None and str(prior[field]) != str(spec.get(field, "")):
                raise ValueError("方案复用了旧组合 ID，但检索条件不同；请使用新的关键词 ID 或新的采集目录，未覆盖旧结果。")


def _read_plan(path: Path) -> dict[str, Any]:
    _no_links(path)
    if not path.is_file():
        raise ValueError("来源采集方案不是普通 JSON 文件，未创建后处理任务。")
    try:
        def invalid_constant(_value: str) -> None:
            raise ValueError("Invalid JSON number")

        value = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=invalid_constant)
        return validate_plan_config(value)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise ValueError("来源采集方案损坏或无效；请检查 collection-plans，未忽略这些来源。") from None


def _catalog(config: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    result.pop("collection_selection", None)
    return result


def _merge_items(destination: list[dict[str, Any]], incoming: list[dict[str, Any]],
                 identity_fields: tuple[str, ...]) -> None:
    existing = {item["id"]: item for item in destination}
    for item in incoming:
        prior = existing.get(item["id"])
        if prior is not None:
            if any(prior.get(key) != item.get(key) for key in identity_fields):
                raise ValueError("不同采集方案的同一 ID 对应不同条件；未覆盖旧映射，请检查来源方案。")
        else:
            copied = copy.deepcopy(item)
            destination.append(copied)
            existing[copied["id"]] = copied


def review_config_for_source(source_root: Path, fallback_config: Path, *, output_dir: Path) -> Path:
    """Return a stable catalog path; write only a new config outside the source.

    Called on the GUI's worker only when the user explicitly creates a task.
    Sources without recorded plans retain their original configuration path.
    Corrupt or conflicting recorded plans fail visibly instead of dropping jobs.
    """
    source = _absolute(source_root)
    fallback = _absolute(fallback_config)
    _no_links(source)
    directory = source / "collection-plans"
    _no_links(directory)
    if not directory.exists():
        return fallback
    if not directory.is_dir():
        raise ValueError("collection-plans 不是目录；请检查采集源，未创建后处理任务。")
    try:
        files = sorted((path for path in directory.iterdir() if path.suffix.casefold() == ".json"),
                       key=lambda path: path.name.casefold())
    except OSError:
        raise ValueError("无法读取来源方案目录，未创建后处理任务。") from None
    if not files:
        return fallback
    base = _catalog(_read_plan(fallback))
    merged = copy.deepcopy(base)
    for path in files:
        incoming = _catalog(_read_plan(path))
        if incoming.get("degree") != merged.get("degree"):
            raise ValueError("来源方案的学历编码不一致；未覆盖旧映射，请检查来源方案。")
        _merge_items(merged["cities"], incoming["cities"], ("name",))
        _merge_items(merged["salaries"], incoming["salaries"], ("code",))
        for track in ("current", "target"):
            _merge_items(merged[track]["keywords"], incoming[track]["keywords"], ("query",))
            _merge_items(merged[track]["experiences"], incoming[track]["experiences"], ("code",))
    if merged == base:
        return fallback
    output = _absolute(output_dir)
    if output == source or source in output.parents:
        raise ValueError("来源识别配置不能写进原始采集源目录，请选择独立保存位置。")
    # The saving helper whitelists config fields and creates an immutable,
    # uniquely named file. Future plan edits cannot invalidate this task.
    return save_plan_config(merged, output)
