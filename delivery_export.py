"""Offline delivery JSON from the current, confirmed four review stages.

No report, browser, provider settings, model request, or source collection is
read here. Preview is optimistic and read-only; publication shares the task
writer's lease and checks the preview token immediately before committing.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict, defaultdict
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import threading
from typing import Any
from urllib.parse import parse_qsl, urlsplit
import uuid

from desktop_data import _redact_text
from desktop_tasks import (REVIEW_STAGES, ReviewTask, TaskStateError, _absolute,
                           _file_version, _no_links, _read_json, _task_location,
                           _validated_jobs, job_id)
from llm_client import JsonSchemaValidationError, SIMPLE_REVIEW_FORMAT, simple_review_payload
from process_architecture import model_input
from project_resources import ResourceError, resource_path
from review_cache import review_for_report


SCHEMA_VERSION = "1.0"
_SCHEMA_NAME = "delivery_jobs.v1.schema.json"
_BOSS_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
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
    "conflicting_identifiers", "ai_review_invalid", "duplicate_target",
)


class DeliveryExportError(ValueError):
    """A fixed, safe message suitable for both the desktop and CLI."""


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = _redact_text(value.strip())
    text = _WINDOWS_PATH.sub("[REDACTED PATH]", text)
    return _UNIX_PATH.sub("[REDACTED PATH]", text)


def _first_text(job: dict, *fields: str) -> str | None:
    return next((text for field in fields if (text := _safe_text(job.get(field))) is not None), None)


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

    def _read(self, path: Path, *, text: bool = False, expected: tuple | None = None) -> Any:
        version = _file_version(path)
        if expected is not None and version != expected:
            raise DeliveryExportError("审核文件已变化，请刷新预览后重试。")
        key = (text, version)
        with self._lock:
            if key in self._files:
                self._files.move_to_end(key)
                return self._files[key]
        try:
            value = path.read_text(encoding="utf-8") if text else _read_json(path)
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


def _current_rows(task: ReviewTask, snapshot: dict, cache: ExportCache) -> tuple[list[dict], dict, dict]:
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
    inputs = _checkpoint(task, snapshot, cache, "llm", stages["llm"]["input_file"])
    return ([row for row in results["llm"] if job_id(row) in keep],
            {job_id(row): row for row in inputs}, identity_rows)


def _prompt(task: ReviewTask, manifest: dict, cache: ExportCache) -> str | None:
    info = manifest["stages"]["llm"]
    runtime = info.get("runtime_directory", "runtime/llm")
    reference = info["stats"].get("review_prompt_file")
    # A project default cannot stand in for the prompt actually used by a task.
    if (not isinstance(reference, str)
            or not re.fullmatch(re.escape(runtime) + r"/review_prompts/[a-f0-9]{64}\.md", reference)):
        return None
    try:
        path = task.path.joinpath(*PurePosixPath(reference).parts)
        _no_links(path)
        prompt = cache._read(path, text=True)
        return prompt if hashlib.sha256(prompt.encode("utf-8")).hexdigest() == path.stem else None
    except (TaskStateError, DeliveryExportError):
        return None


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
    for row in reversed(rows):
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


def _ai_review(job: dict, original_input: dict, prompt: str | None) -> dict:
    review = job.get("llm_review")
    payload = {}
    if isinstance(review, dict) and review.get("review_format") == SIMPLE_REVIEW_FORMAT:
        try:
            payload = simple_review_payload(review)
        except JsonSchemaValidationError:
            pass
    status = (review.get("status") if isinstance(review, dict) else "missing")
    status = status if isinstance(status, str) and status in {"success", "failed"} else "missing" if review is None else "invalid"
    validation = "missing_review"
    if isinstance(review, dict):
        if status == "failed":
            validation = "review_failed"
        elif status != "success":
            validation = "invalid_status"
        elif str(review.get("job_id", "")) != job_id(job):
            validation = "job_identity_changed"
        elif (job.get("jd_completeness") != "complete"
              or not isinstance(job.get("jd"), str) or not job["jd"].strip()):
            validation = "jd_incomplete"
        elif model_input(job) != model_input(original_input):
            validation = "input_changed"
        else:
            valid, validation = review_for_report(review, model_input(original_input), prompt)
            if valid is None or validation != "input_match":
                validation = validation if validation != "legacy_unverified" else "invalid_review_format"
    return {
        "format": SIMPLE_REVIEW_FORMAT if isinstance(review, dict) and review.get("review_format") == SIMPLE_REVIEW_FORMAT else None,
        "status": status, "judgment": payload.get("判断"), "reason": _safe_text(payload.get("理由")),
        "validation": validation,
    }


def _preview(task: ReviewTask, snapshot: dict, cache: ExportCache) -> dict:
    manifest = snapshot["manifest"]
    selected, inputs, identities = _current_rows(task, snapshot, cache)
    prompt = _prompt(task, manifest, cache)
    candidates, by_target = [], defaultdict(list)
    manual = manifest["stages"]["llm"]["manual_choices"]
    for row in selected:
        identity = job_id(row)
        boss_id, url, original, reasons = _identifiers(identities[identity])
        ai = _ai_review(row, inputs[identity], prompt)
        manual_keep = manual.get(identity) is True
        if (ai["validation"] in {"invalid_status", "job_identity_changed"}
                or not manual_keep and not (ai["validation"] == "input_match" and ai["judgment"] == "符合")):
            reasons.append("ai_review_invalid")
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
            "company": _first_text(row, "company", "company_name", "brand_name", "brandName"),
            "city": city, "location": location,
            "district": _first_text(row, "district", "area_district", "areaDistrict"),
            "business_district": _first_text(row, "business_district", "businessDistrict"),
            "address": _first_text(row, "address", "job_address"),
            "salary": _first_text(row, "salary", "salary_desc", "salaryDesc"),
            "experience": _first_text(row, "experience", "job_experience", "jobExperience"),
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
        snapshot = task.review_snapshot()
        key = (str(task.path), snapshot["snapshot_token"])
        value = cache._get_preview(key)
        if value is None:
            value = _preview(task, snapshot, cache)
        if task.review_snapshot()["snapshot_token"] != snapshot["snapshot_token"]:
            raise DeliveryExportError("审核名单或结果在预览时发生变化，请刷新后重试。")
        cache._save_preview(key, value)
        return copy.deepcopy(value)
    except TaskStateError:
        raise DeliveryExportError("审核任务或检查点无效，请刷新并检查前四步。") from None


def _schema_check(value: Any, schema: dict, root: dict) -> None:
    """Validate the JSON Schema subset used by our checked-in contract."""
    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            raise DeliveryExportError("导出 JSON Schema 引用无效。")
        return _schema_check(value, root["$defs"][reference.removeprefix("#/$defs/")], root)
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            try:
                _schema_check(value, branch, root)
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
        if (any(name not in value for name in schema.get("required", []))
                or schema.get("additionalProperties") is False and any(name not in properties for name in value)):
            raise DeliveryExportError("导出 JSON 存在缺失或未允许的字段。")
        for name, child in properties.items():
            if name in value:
                _schema_check(value[name], child, root)
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise DeliveryExportError("导出 JSON 列表数量无效。")
        if schema.get("uniqueItems") and len({_json_bytes(item) for item in value}) != len(value):
            raise DeliveryExportError("导出 JSON 列表含重复项。")
        for item in value:
            if "items" in schema:
                _schema_check(item, schema["items"], root)
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


def validate_export(document: Any, *, cache: ExportCache | None = None) -> None:
    """Apply the packaged schema and count/target/selection invariants locally."""
    cache = cache if cache is not None else ExportCache()
    try:
        schema = cache._read(resource_path(_SCHEMA_NAME))
        if (not isinstance(schema, dict) or schema.get("type") != "object"
                or schema.get("additionalProperties") is not False
                or schema.get("properties", {}).get("schema_version", {}).get("const") != SCHEMA_VERSION):
            raise DeliveryExportError("导出 JSON Schema 无效。")
        _schema_check(document, schema, schema)
        summary, jobs, blocked = document["summary"], document["jobs"], document["blocked_jobs"]
        if (summary["ready_count"] != len(jobs) or summary["blocked_count"] != len(blocked)
                or summary["selected_count"] != len(jobs) + len(blocked)):
            raise DeliveryExportError("导出计数与岗位列表不一致。")
        keys, targets = set(), set()
        for item in [*jobs, *blocked]:
            if item["job_key"] in keys:
                raise DeliveryExportError("导出岗位键重复。")
            keys.add(item["job_key"])
            navigation = item["navigation"]
            original = navigation["original_job_url"]
            parsed_original = _job_url(original) if original is not None else None
            if original is not None and (parsed_original is None or parsed_original[2] != original):
                raise DeliveryExportError("导出导航链接包含无效或未允许的字段。")
            if any(value is not None and _navigation_text(value) != value
                   for name, value in navigation.items() if name != "original_job_url"):
                raise DeliveryExportError("导出导航字段无效。")
        for item in jobs:
            identifier, url = item["boss_job_id"], item["job_url"]
            parsed = _job_url(url) if url is not None else None
            if ((identifier is None and parsed is None)
                    or url is not None and parsed is None
                    or parsed is not None and identifier is not None and identifier != parsed[0]):
                raise DeliveryExportError("可交付岗位缺少有效或一致的 BOSS 定位信息。")
            target = identifier or parsed[0]
            if target in targets:
                raise DeliveryExportError("可交付岗位包含重复 BOSS 目标。")
            targets.add(target)
            ai, selection = item["ai_review"], item["selection"]
            if (selection["source"] == "manual" and selection["manual_keep"] is not True
                    or selection["source"] == "ai" and (selection["manual_keep"] is not None
                        or ai["validation"] != "input_match" or ai["judgment"] != "符合")):
                raise DeliveryExportError("可交付岗位的确认来源无效。")
    except (ResourceError, TaskStateError, OSError, KeyError, TypeError, RecursionError):
        raise DeliveryExportError("导出结构校验失败，请检查安装资源和审核检查点。") from None


def _publish(temporary: Path, destination: Path) -> None:
    # A hard link is an atomic, no-overwrite publication on Windows and POSIX.
    # Both files live in the same newly-created export directory.
    os.link(temporary, destination, follow_symlinks=False)


def export_task(task: ReviewTask, output_dir: Path | None = None, *,
                expected_snapshot: str | None = None, cache: ExportCache | None = None) -> Path:
    """Publish a unique validated JSON file, guarding the preview's selection."""
    cache = cache if cache is not None else ExportCache()
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
        validate_export(document, cache=cache)
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
        destination = directory / "delivery_jobs.json"
        temporary = directory / ".delivery_jobs.json.tmp"
        _no_links(temporary)
        with temporary.open("xb") as handle:
            handle.write(_json_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        validate_export(_read_json(temporary), cache=cache)
        with task.review_read_lease(expected_snapshot=token):
            _no_links(directory)
            if task.review_snapshot()["snapshot_token"] != token:
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
    args = parser.parse_args(argv)
    try:
        task = ReviewTask.open_readonly(args.task)
        path = export_task(task, args.output_dir)
    except (DeliveryExportError, TaskStateError):
        print("导出失败：请检查任务是否有效、前四步是否已确认以及导出目录是否可写。", file=sys.stderr)
        return 1
    print(str(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
