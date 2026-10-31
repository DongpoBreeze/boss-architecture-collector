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


def assert_source_specs_compatible(source_root: Path, specs: list[dict[str, Any]], *,
                                   config: dict[str, Any] | None = None) -> None:
    """Refuse a changed meaning for an ID already recorded in this source.

    Normal UI keyword edits get a new content ID. This extra check also protects
    imported, hand-edited plans from reusing an old success for another query.
    It is read-only and must run before collection snapshots or raw are written.
    """
    source = _absolute(source_root)
    _no_links(source)
    catalog = _catalog(validate_plan_config(config)) if config is not None else None
    for plan_path in _source_plan_files(source):
        incoming = _catalog(_read_plan(plan_path))
        if catalog is None:
            catalog = incoming
        else:
            _merge_catalog(catalog, incoming)
    path = source / "collection_manifest.json"
    _no_links(path)
    manifest: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    if path.exists():
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

    identities: dict[tuple[str, str], tuple[str, str | None]] = {}

    def remember(kind: str, identifier: Any, meaning: Any) -> None:
        if identifier is None:
            return
        spelling = str(identifier)
        key, value = (kind, spelling.casefold()), str(meaning) if meaning is not None else None
        prior = identities.get(key)
        if prior is not None:
            if prior[0] != spelling:
                raise ValueError("来源方案复用了仅大小写不同的同一 ID；请保留历史 ID 的原拼写或使用新的 ID，未写入新方案。")
            if prior[1] is not None and value is not None and prior[1] != value:
                raise ValueError("来源方案复用了同一 ID，但对应不同条件；请使用新的 ID 或新的采集目录，未写入新方案。")
            if value is None:
                return  # Keep known meaning when a historical record omits it.
        # Even when meaning is unknown, the spelling is part of the raw path.
        identities[key] = (spelling, value)

    if catalog is not None:
        for item in catalog["cities"]:
            remember("city", item["id"], item["name"])
        for item in catalog["salaries"]:
            remember("salary", item["id"], item["code"])
        remember("degree", "source", catalog["degree"]["id"])
        for track in ("current", "target"):
            for item in catalog[track]["keywords"]:
                remember("keyword", item["id"], item["query"])
            for item in catalog[track]["experiences"]:
                remember("experience", item["id"], item["code"])
    expected = manifest.get("expected_combination_specs", [])
    historical = records + ([item for item in expected if isinstance(item, dict)] if isinstance(expected, list) else [])
    for spec in [*historical, *specs]:
        parts = str(spec.get("combination", "")).split("__")
        fallback = dict(zip(("track", "city_id", "keyword_id", "salary", "experience"), parts)) if len(parts) == 5 else {}
        for kind, id_field, value_field in (("city", "city_id", "city"), ("keyword", "keyword_id", "keyword"),
                                             ("salary", "salary", "salary_code"), ("experience", "experience", "experience_code")):
            remember(kind, spec.get(id_field), spec.get(value_field))
            remember(kind, fallback.get(id_field), spec.get(value_field))
        remember("degree", "source", spec.get("degree"))


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
    existing = {item["id"].casefold(): item for item in destination}
    for item in incoming:
        prior = existing.get(item["id"].casefold())
        if prior is not None:
            if prior["id"] != item["id"]:
                raise ValueError("不同采集方案复用了仅大小写不同的同一 ID；请保留历史 ID 的原拼写，未覆盖旧映射。")
            if any(prior.get(key) != item.get(key) for key in identity_fields):
                raise ValueError("不同采集方案的同一 ID 对应不同条件；未覆盖旧映射，请检查来源方案。")
        else:
            copied = copy.deepcopy(item)
            destination.append(copied)
            existing[copied["id"].casefold()] = copied


def _source_plan_files(source: Path) -> list[Path]:
    directory = source / "collection-plans"
    _no_links(directory)
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValueError("collection-plans 不是目录；请检查采集源，未写入新方案或创建后处理任务。")
    try:
        return sorted((path for path in directory.iterdir() if path.suffix.casefold() == ".json"),
                      key=lambda path: path.name.casefold())
    except OSError:
        raise ValueError("无法读取来源方案目录，未写入新方案或创建后处理任务。") from None


def _merge_catalog(destination: dict[str, Any], incoming: dict[str, Any]) -> None:
    if incoming.get("degree") != destination.get("degree"):
        raise ValueError("来源方案的学历编码不一致；未覆盖旧映射，请检查来源方案。")
    _merge_items(destination["cities"], incoming["cities"], ("name",))
    _merge_items(destination["salaries"], incoming["salaries"], ("code",))
    for track in ("current", "target"):
        _merge_items(destination[track]["keywords"], incoming[track]["keywords"], ("query",))
        _merge_items(destination[track]["experiences"], incoming[track]["experiences"], ("code",))
    # This also enforces the plan's global keyword/experience identities across
    # tracks, exactly as the final handoff configuration must do.
    validate_plan_config(destination)


def review_config_for_source(source_root: Path, fallback_config: Path, *, output_dir: Path) -> Path:
    """Return a stable catalog path; write only a new config outside the source.

    Called on the GUI's worker only when the user explicitly creates a task.
    Sources without recorded plans retain their original configuration path.
    Corrupt or conflicting recorded plans fail visibly instead of dropping jobs.
    """
    source = _absolute(source_root)
    fallback = _absolute(fallback_config)
    _no_links(source)
    files = _source_plan_files(source)
    if not files:
        return fallback
    base = _catalog(_read_plan(fallback))
    merged = copy.deepcopy(base)
    for path in files:
        incoming = _catalog(_read_plan(path))
        _merge_catalog(merged, incoming)
    if merged == base:
        return fallback
    output = _absolute(output_dir)
    if output == source or source in output.parents:
        raise ValueError("来源识别配置不能写进原始采集源目录，请选择独立保存位置。")
    # The saving helper whitelists config fields and creates an immutable,
    # uniquely named file. Future plan edits cannot invalidate this task.
    return save_plan_config(merged, output)
