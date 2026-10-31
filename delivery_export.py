"""Offline delivery JSON from the current, confirmed four review stages.

No report, browser, provider settings, model request, or source collection is
read here. Preview is optimistic and read-only; publication shares the task
writer's lease and checks the preview token immediately before committing.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict, defaultdict
from collections.abc import Iterable
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any
from urllib.parse import parse_qsl, urlsplit
import uuid

from desktop_data import _redact_text, company_name_source, experience_source
from desktop_tasks import (REVIEW_STAGES, ReviewTask, TaskStateError, _absolute,
                           _file_version, _no_links, _read_json, _task_location,
                           _validated_jobs, job_id)
from project_resources import ResourceError, resource_path


SCHEMA_VERSION = "1.0"
_SCHEMA_NAME = "delivery_jobs.v1.schema.json"
_BOSS_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_COMPANY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_~-]{0,127}\Z")
_COMPANY_URL = re.compile(r"https?://(?:[a-z0-9-]+\.)*zhipin\.com(?::(?:80|443))?"
                          r"/gongsi/[A-Za-z0-9][A-Za-z0-9_~-]{0,127}\.html\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_WINDOWS_PATH = re.compile(r"(?i)(?:file:/+|(?<![\w/])[a-z]:[\\/]|\\\\)[^\s<>\"|]*")
_UNIX_PATH = re.compile(r"(?<![\w:/])/(?:Users|home|root|tmp|var|etc|opt|mnt|media|srv|run|private)/(?:[^\s<>\"|]+)")
_ID_FIELDS = ("boss_job_id", "encrypt_job_id", "encryptJobId")
_URL_FIELDS = ("job_link", "job_url", "link", "url")
_NAVIGATION_FIELDS = {
    "lid": ("lid", "list_id", "listId"),
    "ka": ("ka",),
    "from": ("from", "from_source", "fromSource"),
    # BOSS uses securityId as a detail-page navigation hint.  It is copied
    # only when it is a simple opaque value; cookies, headers and arbitrary
    # query parameters are never carried into the export.
    "security_id": ("security_id", "securityId", "securityID"),
    "encrypt_boss_id": ("encrypt_boss_id", "encryptBossId"),
}
BLOCK_REASONS = (
    "missing_boss_identifier", "invalid_boss_identifier", "invalid_job_url",
    "conflicting_identifiers", "duplicate_target",
)
FIELD_LABELS = {
    "schema_version": "格式版本", "export_id": "导出 ID", "generated_at": "导出时间",
    "source_task": "来源任务", "task_id": "任务 ID", "name": "任务名称",
    "track": "方向范围", "created_at": "任务创建时间", "review_revision": "审核版本",
    "review_stages": "各步确认版本", "import": "导入与去重确认版本", "shortlist": "列表初筛确认版本",
    "fetch-jd": "JD 补抓确认版本", "llm": "AI 语义复核确认版本", "summary": "数量摘要",
    "selected_count": "最终保留数", "ready_count": "可导出数", "blocked_count": "阻止导出数",
    "jobs": "可导出岗位", "blocked_jobs": "阻止导出岗位", "job_key": "稳定记录键",
    "job_id": "任务内岗位 ID", "boss_job_id": "BOSS 岗位 ID", "job_url": "岗位链接",
    "title": "岗位名称", "company": "公司", "city": "城市", "location": "工作地点",
    "encrypt_brand_id": "公司 ID", "company_link": "公司链接", "company_industry": "公司行业",
    "company_scale": "公司规模", "company_stage": "融资阶段",
    "district": "行政区", "business_district": "商圈", "address": "详细地址",
    "salary": "薪资", "experience": "经验要求", "education": "学历要求",
    "skills": "技能", "job_labels": "岗位标签", "jd": "JD 正文",
    "ai_review": "AI 复核", "format": "复核格式", "status": "运行状态",
    "judgment": "判断", "reason": "理由", "validation": "验证状态",
    "selection": "审核取舍", "stage": "审核阶段", "confirmed": "已确认",
    "keep": "最终保留", "source": "决定来源", "manual_keep": "人工保留决定",
    "navigation": "页面导航信息", "original_job_url": "来源岗位链接",
    "lid": "列表定位值", "ka": "页面定位值", "from": "来源标记",
    "security_id": "详情页定位值", "encrypt_boss_id": "招聘者定位值",
    "blocked_reasons": "阻止原因",
}
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f\ud800-\udfff]')
_RESERVED_FILENAME = re.compile(r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)")
_MAX_FILENAME_LENGTH = 120
_EMPTY = object()


class DeliveryExportError(ValueError):
    """A fixed, safe message suitable for both the desktop and CLI."""


class EmptyExportError(DeliveryExportError):
    """The chosen leaves currently have no nonempty values to publish."""


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = _redact_text(value.strip())
    text = _WINDOWS_PATH.sub("[REDACTED PATH]", text)
    return _UNIX_PATH.sub("[REDACTED PATH]", text)


def _first_text(job: dict, *fields: str) -> str | None:
    return next((text for field in fields if (text := _safe_text(job.get(field))) is not None), None)


def _company_identifier(value: Any) -> str | None:
    # Encrypted company IDs can end in ~ or ~~. They are metadata and never
    # participate in _identifiers(), which only resolves a BOSS job target.
    return value if (isinstance(value, str) and _COMPANY_ID.fullmatch(value)
                     and _redact_text(value) == value) else None


def _company_link(value: Any) -> str | None:
    # Keep safe source links exactly as recorded. Query strings, fragments,
    # credentials, local paths and unrelated URLs are not company metadata.
    return value if (isinstance(value, str) and _COMPANY_URL.fullmatch(value)
                     and _redact_text(value) == value) else None


def _string_list(value: Any) -> list[str] | None:
    if isinstance(value, str):
        # Upstream versions have emitted both arrays and a compact human
        # readable string.  Normalize the latter without dropping optional
        # values from an otherwise valid whitelist record.
        values = re.split(r"\s*(?:\||｜|、|,|，|;|；)\s*", value.strip())
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        return None
    return list(dict.fromkeys(text for item in values if (text := _safe_text(item)) is not None)) or None


def _navigation_text(value: Any) -> str | None:
    """Return a bounded navigation hint, excluding credentials and paths."""
    if type(value) in {int, str}:
        text = str(value).strip()
        if (1 <= len(text) <= 512
                and re.fullmatch(r"[A-Za-z0-9._~:/+=@-]+", text)
                and _WINDOWS_PATH.search(text) is None and _UNIX_PATH.search(text) is None
                and _redact_text(text) == text):
            return text
    return None


def _navigation_values(rows: list[dict]) -> dict[str, str | None]:
    values: dict[str, str | None] = {key: None for key in _NAVIGATION_FIELDS}
    for row in reversed(rows):
        for output_name, aliases in _NAVIGATION_FIELDS.items():
            if values[output_name] is not None:
                continue
            for alias in aliases:
                value = _navigation_text(row.get(alias))
                if value is not None:
                    values[output_name] = value
                    break
        for field in _URL_FIELDS:
            parsed = _job_url(row.get(field))
            if parsed is None:
                continue
            for key, value in parse_qsl(urlsplit(parsed[2]).query):
                output_name = "security_id" if key == "securityId" else key
                if output_name in values and values[output_name] is None:
                    values[output_name] = value
    return values


def _json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise DeliveryExportError("导出内容无法安全保存为 JSON。") from None


class ExportCache:
    """Bounded, version-aware local reads; returned previews are isolated copies."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._files: OrderedDict[tuple, Any] = OrderedDict()
        self._previews: OrderedDict[tuple, dict] = OrderedDict()

    def clear(self) -> None:
        with self._lock:
            self._files.clear()
            self._previews.clear()

    def _read(self, path: Path, *, expected: tuple | None = None) -> Any:
        version = _file_version(path)
        if expected is not None and version != expected:
            raise DeliveryExportError("审核文件已变化，请刷新预览后重试。")
        key = version
        with self._lock:
            if key in self._files:
                self._files.move_to_end(key)
                return self._files[key]
        try:
            value = _read_json(path)
        except (OSError, UnicodeError):
            raise DeliveryExportError("导出所需的本地文件不可读取。") from None
        if _file_version(path) != version:
            raise DeliveryExportError("审核文件在读取时发生变化，请刷新后重试。")
        with self._lock:
            self._files[key] = value
            while len(self._files) > 64:
                self._files.popitem(last=False)
        return value

    def _get_preview(self, key: tuple) -> dict | None:
        with self._lock:
            value = self._previews.get(key)
            if value is not None:
                self._previews.move_to_end(key)
                return copy.deepcopy(value)
        return None

    def _save_preview(self, key: tuple, value: dict) -> None:
        with self._lock:
            self._previews[key] = copy.deepcopy(value)
            while len(self._previews) > 8:
                self._previews.popitem(last=False)


def _checkpoint(task: ReviewTask, snapshot: dict, cache: ExportCache,
                stage: str, reference: str | None) -> list[dict]:
    if reference is None:
        raise DeliveryExportError("审核步骤缺少当前输入或结果检查点，请重新运行并确认。")
    value = cache._read(task._artifact_path(stage, reference),
                        expected=snapshot["file_versions"][reference])
    if not isinstance(value, dict) or value.get("stage") != stage or not isinstance(value.get("jobs"), list):
        raise DeliveryExportError("审核岗位检查点结构无效。")
    return _validated_jobs(value["jobs"])


def _current_rows(task: ReviewTask, snapshot: dict, cache: ExportCache) -> tuple[list[dict], dict]:
    manifest = snapshot["manifest"]
    stages = manifest["stages"]
    # Validate readiness before reading checkpoint bodies. Pending/excluded
    # rows remain outside the final keep set, even in a confirmed AI stage.
    for stage in REVIEW_STAGES:
        info = stages[stage]
        if (info["status"] != "completed" or info["confirmed"] is not True
                or info["confirmed_revision"] != info["revision"]
                or info["result_file"] is None or info.get("invalidated_by") is not None
                or info["result_input_ids"] != info["input_ids"]
                or info["result_input_revision"] != info["input_revision"]
                or info["result_input_file"] != info["input_file"]):
            raise DeliveryExportError("请先完成并确认前四步；未运行、未确认或失效结果不能导出。")
    baseline = _checkpoint(task, snapshot, cache, "import", manifest["import_baseline_file"])
    if [job_id(row) for row in baseline] != manifest["baseline_ids"]:
        raise DeliveryExportError("固定导入基线与审核名单不一致。")
    results, previous_rows, identity_rows = {}, [], defaultdict(list)
    for index, stage in enumerate(REVIEW_STAGES):
        info = stages[stage]
        inputs = _checkpoint(task, snapshot, cache, stage, info["input_file"])
        rows = _checkpoint(task, snapshot, cache, stage, info["result_file"])
        if ([job_id(row) for row in inputs] != info["input_ids"]
                or [job_id(row) for row in rows] != info["result_ids"]):
            raise DeliveryExportError("当前审核输入或结果与固定岗位清单不一致。")
        if index == 0:
            if info["input_ids"] or info["result_ids"] != manifest["baseline_ids"]:
                raise DeliveryExportError("当前导入结果不匹配固定基线。")
        else:
            previous = stages[REVIEW_STAGES[index - 1]]
            keep = set(previous["keep_ids"])
            expected = [row for row in previous_rows if job_id(row) in keep]
            if (info["input_revision"] != previous["revision"]
                    or info["input_ids"] != previous["keep_ids"]
                    or info["result_ids"] != info["input_ids"] or inputs != expected):
                raise DeliveryExportError("审核输入已不匹配前置确认结果，请重跑相关步骤。")
        for row in rows:
            identity_rows[job_id(row)].append(row)
        results[stage] = rows
        previous_rows = rows
    keep = set(stages["llm"]["keep_ids"])
    return [row for row in results["llm"] if job_id(row) in keep], identity_rows


def _job_url(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname or ""
        match = re.fullmatch(r"/job_detail/([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.html", parsed.path)
        if (parsed.scheme not in {"https", "http"} or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 80, 443}
                or re.fullmatch(r"(?:[a-z0-9-]+\.)*zhipin\.com", host) is None or match is None):
            return None
        identifier = match[1]
        if _redact_text(identifier) != identifier:
            return None
        canonical = f"https://www.zhipin.com/job_detail/{identifier}.html"
        # Navigation hints are optional.  Keep only fields the downstream BOSS
        # navigator understands; unknown query names never leave the task.
        query = []
        for segment in parsed.query.split("&"):
            pair = parse_qsl(segment, keep_blank_values=True)
            if (len(pair) == 1 and pair[0][0] in {"lid", "ka", "from", "securityId"}
                    and _navigation_text(pair[0][1]) is not None):
                query.append(segment)
        # Preserve the navigable source URL (including its approved hints) in
        # the nested navigation object; the public job_url remains canonical.
        port = f":{parsed.port}" if parsed.port is not None else ""
        original = (f"{parsed.scheme.lower()}://{host}{port}{parsed.path}"
                    + ("?" + "&".join(query) if query else ""))
        return identifier, canonical, original
    except (ValueError, UnicodeError):
        return None


def _identifiers(rows: list[dict]) -> tuple[str | None, str | None, str | None, list[str]]:
    ids, urls, reasons = set(), set(), set()
    originals = []
    identity_rows = []
    for row in reversed(rows):
        identity_rows.append(row)
        # Import keeps every original source target when deduplication merges
        # rows. A stable task job_id is deliberately outside this whitelist.
        candidates = row.get("delivery_identity_candidates")
        if isinstance(candidates, list):
            identity_rows.extend(candidate for candidate in candidates if isinstance(candidate, dict))
    for row in identity_rows:
        for field in _ID_FIELDS:
            value = row.get(field)
            if value is None or value == "":
                continue
            value = str(value).strip() if type(value) in {str, int} else ""
            if not _BOSS_ID.fullmatch(value) or _redact_text(value) != value:
                reasons.add("invalid_boss_identifier")
            else:
                ids.add(value)
        for field in _URL_FIELDS:
            value = row.get(field)
            if value is None or value == "":
                continue
            parsed = _job_url(value)
            if parsed is None:
                reasons.add("invalid_job_url")
            else:
                ids.add(parsed[0])
                urls.add(parsed[1])
                if parsed[2] not in originals:
                    originals.append(parsed[2])
    if len(ids) > 1:
        reasons.add("conflicting_identifiers")
    identifier = next(iter(ids)) if len(ids) == 1 else None
    if not ids:
        reasons.add("missing_boss_identifier")
    url = next(iter(urls)) if len(urls) == 1 else None
    original = originals[0] if len(urls) == 1 and originals else None
    return identifier, url, original, [code for code in BLOCK_REASONS if code in reasons]


def _ai_review(job: dict) -> dict:
    """Copy readable review fields without revalidating the confirmed selection."""
    review = job.get("llm_review")
    fields = review if isinstance(review, dict) else {}
    # Unknown textual statuses and old format names are records, not verdicts.
    # Missing or unreadable statuses must never be upgraded to success.
    status = _safe_text(fields.get("status")) or ("missing" if review is None else "invalid")
    return {
        "format": _safe_text(fields.get("review_format")), "status": status,
        "judgment": _first_text(fields, "判断", "judgment"),
        "reason": _first_text(fields, "理由", "reason"), "validation": "not_checked",
    }


def _preview(task: ReviewTask, snapshot: dict, cache: ExportCache) -> dict:
    manifest = snapshot["manifest"]
    selected, identities = _current_rows(task, snapshot, cache)
    candidates, by_target = [], defaultdict(list)
    manual = manifest["stages"]["llm"]["manual_choices"]
    for row in selected:
        identity = job_id(row)
        boss_id, url, original, reasons = _identifiers(identities[identity])
        ai = _ai_review(row)
        manual_keep = manual.get(identity) is True
        city = _first_text(row, "city", "city_name", "cityName")
        location = _first_text(row, "location", "job_location")
        # The row key belongs to the durable task identity, never an export or
        # directory name. Synthetic IDs are hashed, not mislabeled as BOSS IDs.
        stable = hashlib.sha256((str(manifest["task_id"]) + "\0" + identity).encode("utf-8")).hexdigest()
        navigation = _navigation_values(identities[identity])
        # The source URL is itself a navigation hint.  Keep the approved
        # scalar fields beside it so external scripts do not need to parse a
        # query string and so their absence is represented by null.
        navigation = {"original_job_url": original, **navigation}
        item = {
            "job_key": "job_" + stable, "job_id": _safe_text(identity),
            "boss_job_id": boss_id, "job_url": url,
            "title": _first_text(row, "title", "job_name", "jobName"),
            "company": _safe_text(company_name_source(row)[0]),
            "encrypt_brand_id": _company_identifier(row.get("encrypt_brand_id")),
            "company_link": _company_link(row.get("company_link")),
            "company_industry": _first_text(row, "company_industry"),
            "company_scale": _first_text(row, "company_scale"),
            "company_stage": _first_text(row, "company_stage"),
            "city": city, "location": location,
            "district": _first_text(row, "district", "area_district", "areaDistrict"),
            "business_district": _first_text(row, "business_district", "businessDistrict"),
            "address": _first_text(row, "address", "job_address"),
            "salary": _first_text(row, "salary", "salary_desc", "salaryDesc"),
            "experience": _safe_text(experience_source(row)[0]),
            "education": _first_text(row, "education", "job_degree", "jobDegree"),
            "skills": _string_list(row.get("skills")), "job_labels": _string_list(row.get("job_labels")),
            "jd": _first_text(row, "jd"), "ai_review": ai,
            "selection": {"stage": "llm", "confirmed": True, "keep": True,
                          "source": "manual" if manual_keep else "ai", "manual_keep": True if manual_keep else None},
            "navigation": navigation,
        }
        candidates.append((item, reasons))
        if boss_id is not None:
            by_target[boss_id].append(reasons)
    for group in by_target.values():
        if len(group) > 1:
            for reasons in group:
                reasons.append("duplicate_target")
    jobs, blocked = [], []
    for item, reasons in candidates:
        if reasons:
            blocked.append({**item, "blocked_reasons": [code for code in BLOCK_REASONS if code in reasons]})
        else:
            jobs.append(item)
    return {
        "source_task": {
            "task_id": _safe_text(manifest["task_id"]), "name": _safe_text(manifest["name"]),
            "city": _safe_text(manifest.get("city")), "track": manifest["track"],
            "created_at": _safe_text(manifest["created_at"]),
            "review_revision": snapshot["snapshot_token"],
            "review_stages": {stage: manifest["stages"][stage]["revision"] for stage in REVIEW_STAGES},
        },
        "summary": {"selected_count": len(selected), "ready_count": len(jobs), "blocked_count": len(blocked)},
        "jobs": jobs, "blocked_jobs": blocked, "snapshot_token": snapshot["snapshot_token"],
    }


def preview_task(task: ReviewTask, *, cache: ExportCache | None = None) -> dict:
    """Return an isolated, read-only view of the current confirmed final keep set."""
    cache = cache if cache is not None else ExportCache()
    try:
        snapshot = task.review_snapshot(preserve_selection=True)
        key = (str(task.path), snapshot["snapshot_token"])
        value = cache._get_preview(key)
        if value is None:
            value = _preview(task, snapshot, cache)
        if task.review_snapshot(preserve_selection=True)["snapshot_token"] != snapshot["snapshot_token"]:
            raise DeliveryExportError("审核名单或结果在预览时发生变化，请刷新后重试。")
        cache._save_preview(key, value)
        return copy.deepcopy(value)
    except TaskStateError:
        raise DeliveryExportError("审核任务或检查点无效，请刷新并检查前四步。") from None


def _schema_check(value: Any, schema: dict, root: dict, *, require_all: bool = False) -> None:
    """Validate the JSON Schema subset used by our checked-in contract."""
    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            raise DeliveryExportError("导出 JSON Schema 引用无效。")
        return _schema_check(value, root["$defs"][reference.removeprefix("#/$defs/")], root,
                             require_all=require_all)
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            try:
                _schema_check(value, branch, root, require_all=require_all)
                break
            except DeliveryExportError:
                pass
        else:
            raise DeliveryExportError("导出 JSON 不满足字段约束。")
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    checks = {"object": type(value) is dict, "array": type(value) is list,
              "string": type(value) is str, "integer": type(value) is int,
              "boolean": type(value) is bool, "null": value is None}
    if types and not any(checks.get(kind, False) for kind in types):
        raise DeliveryExportError("导出 JSON 字段类型无效。")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise DeliveryExportError("导出 JSON 固定字段无效。")
    if "enum" in schema and value not in schema["enum"]:
        raise DeliveryExportError("导出 JSON 枚举字段无效。")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = set(schema.get("required", [])) | (set(properties) if require_all else set())
        if (any(name not in value for name in required)
                or schema.get("additionalProperties") is False and any(name not in properties for name in value)):
            raise DeliveryExportError("导出 JSON 存在缺失或未允许的字段。")
        for name, child in properties.items():
            if name in value:
                _schema_check(value[name], child, root, require_all=require_all)
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise DeliveryExportError("导出 JSON 列表数量无效。")
        if schema.get("uniqueItems") and len({_json_bytes(item) for item in value}) != len(value):
            raise DeliveryExportError("导出 JSON 列表含重复项。")
        for item in value:
            if "items" in schema:
                _schema_check(item, schema["items"], root, require_all=require_all)
    elif isinstance(value, str):
        if (len(value) < schema.get("minLength", 0)
                or len(value) > schema.get("maxLength", len(value))
                or "pattern" in schema and re.search(schema["pattern"], value) is None):
            raise DeliveryExportError("导出 JSON 文本字段无效。")
        if schema.get("format") == "date-time":
            try:
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if timestamp.utcoffset() is None:
                    raise ValueError
            except ValueError:
                raise DeliveryExportError("导出 JSON 时间字段无效。") from None
    elif type(value) is int and value < schema.get("minimum", value):
        raise DeliveryExportError("导出 JSON 数量不能为负数。")


def _export_schema(cache: ExportCache) -> dict:
    schema = cache._read(resource_path(_SCHEMA_NAME))
    if (not isinstance(schema, dict) or schema.get("type") != "object"
            or schema.get("additionalProperties") is not False
            or schema.get("properties", {}).get("schema_version", {}).get("const") != SCHEMA_VERSION):
        raise DeliveryExportError("导出 JSON Schema 无效。")
    return schema


def export_field_paths(*, cache: ExportCache | None = None) -> tuple[str, ...]:
    """List schema-ordered leaf paths; object-array items share their array path."""
    cache = cache if cache is not None else ExportCache()
    try:
        root = _export_schema(cache)

        def leaves(schema: dict, path: tuple[str, ...], references: frozenset[str]) -> list[str]:
            if not isinstance(schema, dict):
                raise DeliveryExportError("导出 JSON Schema 字段无效。")
            if "$ref" in schema:
                reference = schema["$ref"]
                if (not isinstance(reference, str) or not reference.startswith("#/")
                        or reference in references):
                    raise DeliveryExportError("导出 JSON Schema 引用无效。")
                target = root
                for part in reference[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
                return leaves(target, path, references | {reference})
            if "properties" in schema:
                properties = schema["properties"]
                if not isinstance(properties, dict):
                    raise DeliveryExportError("导出 JSON Schema 字段无效。")
                paths = []
                for name, child in properties.items():
                    if not isinstance(name, str) or not name or "." in name:
                        raise DeliveryExportError("导出 JSON Schema 字段名无效。")
                    paths.extend(leaves(child, (*path, name), references))
                return paths
            if "items" in schema:
                # A scalar array is one selectable value; an object array
                # exposes the fields of each item without numeric indices.
                return leaves(schema["items"], path, references)
            return [".".join(path)] if path else []

        return tuple(leaves(root, (), frozenset()))
    except (ResourceError, TaskStateError, OSError, KeyError, TypeError, RecursionError):
        raise DeliveryExportError("导出字段读取失败，请检查安装资源。") from None


def _has_export_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_has_export_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_export_value(item) for item in value)
    return True


def available_export_fields(preview: dict, *, cache: ExportCache | None = None) -> tuple[str, ...]:
    """List populated whitelist leaves in this preview, plus generated metadata."""
    if not isinstance(preview, dict):
        raise DeliveryExportError("导出预览无效，请刷新后重新选择字段。")
    fields = export_field_paths(cache=cache)
    present = {"schema_version", "export_id", "generated_at"}

    def visit(value: Any, tree: dict, path: str) -> None:
        if not tree:
            # Once a leaf is present, later records need no repeated text scan
            # or copying of large JD bodies. Scalar arrays stay single leaves.
            if path in present:
                return
            if _has_export_value(value):
                present.add(path)
        elif isinstance(value, list):
            for item in value:
                visit(item, tree, path)
        elif isinstance(value, dict):
            for name, child in tree.items():
                if name in value:
                    visit(value[name], child, f"{path}.{name}" if path else name)

    try:
        visit(preview, _selected_field_tree(fields), "")
    except (TypeError, RecursionError):
        raise DeliveryExportError("导出预览无效，请刷新后重新选择字段。") from None
    return tuple(field for field in fields if field in present)


def _field_values(fields: Iterable[str]) -> tuple[str, ...]:
    try:
        if isinstance(fields, (str, bytes, bytearray)):
            raise TypeError
        values = tuple(fields)
    except (TypeError, ValueError):
        raise DeliveryExportError("请选择有效的导出字段列表。") from None
    if not values or any(not isinstance(field, str) for field in values):
        raise DeliveryExportError("请至少选择一个有效的导出字段。")
    return values


def _ordered_fields(selected_fields: Iterable[str] | None, cache: ExportCache) -> tuple[str, ...]:
    fields = None if selected_fields is None else _field_values(selected_fields)
    available = export_field_paths(cache=cache)
    if fields is None:
        return available
    selected = set(fields)
    if not selected.issubset(available):
        raise DeliveryExportError("所选导出字段无效，请重新选择字段。")
    return tuple(field for field in available if field in selected)


def _filename_label(label: str) -> str:
    label = _UNSAFE_FILENAME.sub("_", re.sub(r"\s+", "", label)).strip("._")
    label = label[:36].rstrip("._") or "字段"
    return "字段" + label if _RESERVED_FILENAME.match(label) else label


def is_export_filename(name: str) -> bool:
    """Accept safe JSON basenames, including legacy delivery_jobs.json files."""
    if (not isinstance(name, str) or not name.endswith(".json")
            or len(name) > _MAX_FILENAME_LENGTH or name.startswith(".")
            or _UNSAFE_FILENAME.search(name) or any(character.isspace() for character in name)):
        return False
    stem = name[:-5]
    return bool(stem and stem == stem.rstrip(" .") and not _RESERVED_FILENAME.match(name))


def field_filename(fields: Iterable[str]) -> str:
    """Build a basename from already validated, schema-ordered leaves without I/O."""
    first = tuple(dict.fromkeys(_field_values(fields)))[:3]
    try:
        labels = [_filename_label(FIELD_LABELS[field.rsplit(".", 1)[-1]]) for field in first]
    except (KeyError, TypeError):
        raise DeliveryExportError("所选导出字段无效，请重新选择字段。") from None
    name = "_".join(labels) + ".json"
    if not is_export_filename(name):
        raise DeliveryExportError("导出文件名无效，请重新选择字段。")
    return name


def export_filename(selected_fields: Iterable[str] | None = None, *,
                    cache: ExportCache | None = None, preview: dict | None = None) -> str:
    """Name selected labels in schema order, optionally excluding absent values."""
    cache = cache if cache is not None else ExportCache()
    fields = _ordered_fields(selected_fields, cache)
    if preview is not None:
        available = set(available_export_fields(preview, cache=cache))
        fields = tuple(field for field in fields if field in available)
        if not fields:
            raise EmptyExportError("所选字段当前均无值，请重新选择需要导出的字段。")
    return field_filename(fields)


def _selected_field_tree(fields: tuple[str, ...]) -> dict:
    tree: dict = {}
    for field in fields:
        branch = tree
        for name in field.split("."):
            branch = branch.setdefault(name, {})
    return tree


def _project_fields(value: Any, tree: dict) -> Any:
    """Copy selected leaves, retaining their ancestor objects and ordered lists."""
    if not tree or value is None:
        return copy.deepcopy(value)
    if isinstance(value, list):
        return [_project_fields(item, tree) for item in value]
    return {name: _project_fields(value[name], child) for name, child in tree.items()}


def _prune_empty(value: Any) -> Any:
    """Copy nonempty JSON values recursively, preserving false and zero."""
    if value is None or isinstance(value, str) and not value.strip():
        return _EMPTY
    if isinstance(value, dict):
        cleaned = {name: child for name, item in value.items() if (child := _prune_empty(item)) is not _EMPTY}
        return cleaned if cleaned else _EMPTY
    if isinstance(value, list):
        cleaned = [child for item in value if (child := _prune_empty(item)) is not _EMPTY]
        return cleaned if cleaned else _EMPTY
    return copy.deepcopy(value)


def _compact_fields(document: dict, tree: dict | None) -> dict:
    value = _prune_empty(document if tree is None else _project_fields(document, tree))
    return {} if value is _EMPTY else value


def compact_export_record(record: dict) -> dict:
    """Return an isolated record with recursively empty values removed."""
    if not isinstance(record, dict):
        raise DeliveryExportError("导出记录无效，请刷新预览后重试。")
    return _compact_fields(record, None)


def _require_canonical_shape(document: dict, cache: ExportCache) -> None:
    """Require every canonical whitelist slot before any optional values are pruned."""
    try:
        schema = _export_schema(cache)
        _schema_check(document, schema, schema, require_all=True)
    except (ResourceError, TaskStateError, OSError, KeyError, TypeError, RecursionError):
        raise DeliveryExportError("导出结构校验失败，请检查安装资源和审核检查点。") from None


def validate_export(document: Any, *, cache: ExportCache | None = None) -> None:
    """Apply the packaged schema and count/target/selection invariants locally."""
    cache = cache if cache is not None else ExportCache()
    try:
        schema = _export_schema(cache)
        _schema_check(document, schema, schema)
        summary, jobs, blocked = document["summary"], document.get("jobs", []), document.get("blocked_jobs", [])
        if (summary["ready_count"] != len(jobs) or summary["blocked_count"] != len(blocked)
                or summary["selected_count"] != len(jobs) + len(blocked)):
            raise DeliveryExportError("导出计数与岗位列表不一致。")
        keys, targets = set(), set()
        for item in [*jobs, *blocked]:
            if item["job_key"] in keys:
                raise DeliveryExportError("导出岗位键重复。")
            keys.add(item["job_key"])
            company_id, company_link = item.get("encrypt_brand_id"), item.get("company_link")
            if ((company_id is not None and _company_identifier(company_id) is None)
                    or (company_link is not None and _company_link(company_link) is None)):
                raise DeliveryExportError("导出公司 ID 或公司详情链接无效。")
            navigation = item.get("navigation", {})
            original = navigation.get("original_job_url")
            parsed_original = _job_url(original) if original is not None else None
            if original is not None and (parsed_original is None or parsed_original[2] != original):
                raise DeliveryExportError("导出导航链接包含无效或未允许的字段。")
            if any(value is not None and _navigation_text(value) != value
                   for name, value in navigation.items() if name != "original_job_url"):
                raise DeliveryExportError("导出导航字段无效。")
            selection = item["selection"]
            if (selection["source"] == "manual" and selection.get("manual_keep") is not True
                    or selection["source"] == "ai" and selection.get("manual_keep") is not None):
                raise DeliveryExportError("可交付岗位的确认来源无效。")
        for item in jobs:
            identifier, url = item.get("boss_job_id"), item.get("job_url")
            parsed = _job_url(url) if url is not None else None
            if ((identifier is None and parsed is None)
                    or url is not None and parsed is None
                    or parsed is not None and identifier is not None and identifier != parsed[0]):
                raise DeliveryExportError("可交付岗位缺少有效或一致的 BOSS 定位信息。")
            target = identifier or parsed[0]
            if target in targets:
                raise DeliveryExportError("可交付岗位包含重复 BOSS 目标。")
            targets.add(target)
    except (ResourceError, TaskStateError, OSError, KeyError, TypeError, RecursionError):
        raise DeliveryExportError("导出结构校验失败，请检查安装资源和审核检查点。") from None


def _publish(temporary: Path, destination: Path) -> None:
    # A hard link is an atomic, no-overwrite publication on Windows and POSIX.
    # Both files live in the same newly-created export directory.
    os.link(temporary, destination, follow_symlinks=False)


def export_task(task: ReviewTask, output_dir: Path | None = None, *,
                expected_snapshot: str | None = None, cache: ExportCache | None = None,
                selected_fields: Iterable[str] | None = None) -> Path:
    """Publish full or selected JSON after validating the full confirmed document."""
    cache = cache if cache is not None else ExportCache()
    fields = _ordered_fields(selected_fields, cache)
    if expected_snapshot is not None and (not isinstance(expected_snapshot, str) or not _DIGEST.fullmatch(expected_snapshot)):
        raise DeliveryExportError("预览版本无效，请刷新后重新导出。")
    directory = temporary = None
    published = False
    try:
        preview = preview_task(task, cache=cache)
        token = preview["snapshot_token"]
        if expected_snapshot is not None and expected_snapshot != token:
            raise DeliveryExportError("审核名单或结果已变化，请刷新预览后重新导出。")
        export_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        document = {"schema_version": SCHEMA_VERSION, "export_id": export_id,
                    "generated_at": now.isoformat(timespec="milliseconds"),
                    **{field: preview[field] for field in ("source_task", "summary", "jobs", "blocked_jobs")}}
        _require_canonical_shape(document, cache)
        validate_export(document, cache=cache)
        available = set(available_export_fields(preview, cache=cache))
        fields = tuple(field for field in fields if field in available)
        if not fields:
            raise EmptyExportError("所选字段当前均无值，请重新选择需要导出的字段。")
        field_tree = _selected_field_tree(fields)
        filename = field_filename(fields)
        output = _compact_fields(document, field_tree)
        if not output:
            raise EmptyExportError("所选字段当前均无值，请重新选择需要导出的字段。")
        parent = _absolute(output_dir if output_dir is not None else task.path / "delivery-exports")
        _task_location(parent)
        for reserved in (task.path / "stages", task.path / "runtime"):
            if parent == reserved or reserved in parent.parents:
                raise DeliveryExportError("请选择审核检查点和运行目录以外的导出目录。")
        parent.mkdir(parents=True, exist_ok=True)
        _no_links(parent)
        candidate = parent / (now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + export_id)
        candidate.mkdir(exist_ok=False)
        directory = candidate  # Own cleanup only after exclusive creation succeeds.
        destination = directory / filename
        temporary = directory / ".delivery_jobs.json.tmp"
        _no_links(temporary)
        with temporary.open("xb") as handle:
            handle.write(_json_bytes(output))
            handle.flush()
            os.fsync(handle.fileno())
        saved = _read_json(temporary)
        _require_canonical_shape(document, cache)
        if selected_fields is None:
            validate_export(saved, cache=cache)
        else:
            # A projection intentionally omits schema-required fields. Recheck
            # the canonical document and compare the saved JSON with its exact
            # selected, compact projection, including JSON scalar types.
            validate_export(document, cache=cache)
        if _json_bytes(saved) != _json_bytes(_compact_fields(document, field_tree)):
            raise DeliveryExportError("导出字段或内容在保存时发生变化，请重试。")
        with task.review_read_lease(expected_snapshot=token, preserve_selection=True):
            _no_links(directory)
            if task.review_snapshot(preserve_selection=True)["snapshot_token"] != token:
                raise DeliveryExportError("审核文件已变化，请刷新预览后重新导出。")
            _publish(temporary, destination)
            published = True
        return destination
    except TaskStateError:
        raise DeliveryExportError("审核任务已变化或正在更新，请刷新预览后重新导出。") from None
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, DeliveryExportError):
            raise
        raise DeliveryExportError("JSON 导出失败；未覆盖已有导出，请检查目录后重试。") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if directory is not None and not published:
            try:
                directory.rmdir()
            except OSError:
                pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, "参数无效，请使用 --help 查看用法。\n")


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(description="将前四步已确认的最终保留岗位导出为独立投递 JSON（完全离线）。")
    parser.add_argument("--task", type=Path, required=True, help="已有审核任务目录或 task_manifest.json")
    parser.add_argument("--output-dir", type=Path, help="导出父目录；默认任务目录内 delivery-exports")
    parser.add_argument("--fields", nargs="+", metavar="FIELD",
                        help="只导出指定叶字段，如 jobs.title jobs.ai_review.reason；省略时导出完整 JSON；无值字段自动省略")
    args = parser.parse_args(argv)
    try:
        task = ReviewTask.open_readonly(args.task, preserve_selection=True)
        path = export_task(task, args.output_dir, selected_fields=args.fields)
    except EmptyExportError:
        print("导出失败：所选字段当前均无值，请重新选择需要导出的字段。", file=sys.stderr)
        return 1
    except (DeliveryExportError, TaskStateError):
        print("导出失败：请检查任务是否有效、前四步是否已确认、所选字段是否有效以及导出目录是否可写。", file=sys.stderr)
        return 1
    print(str(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
