"""Read-only data adapters for the desktop result browser.

This module deliberately does not import the collector, processor, or UI.  It
reads their durable JSON checkpoints without running any screening rules or
network requests.  Unknown progress is represented by ``None``, not by zero.
JD failure/pause counts refer to affected jobs, not batches.
"""

from __future__ import annotations

import copy
import csv
import json
import math
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


CLASSIFICATIONS = ("重点投递", "海投池", "暂不投")
DIMENSIONS = (
    "方案设计", "系统边界与集成", "Agent/RAG/Workflow",
    "知识与数据", "PoC/交付", "迭代与评测",
)

_ARTIFACT_PATHS = {
    "run_manifest": "run_manifest.json",
    "raw_sample": "raw_sample.json",
    "merged_jobs": "prepared/merged_jobs_index.json",
    "hai_tou_pool": "prepared/hai_tou_pool.json",
    "jd_input": "prepared/jd_shortlists/jd_input.json",
    "jd_details": "prepared/jd_shortlists/details.json",
    "jd_manifest": "prepared/jd_shortlists/details_manifest.json",
    "llm_reviews": "prepared/llm_reviews.json",
    "llm_manifest": "prepared/llm_manifest.json",
    "report": "prepared/海投分层清单.md",
    "comparison": "prepared/语义复核前后对照.md",
    "desktop_log": "desktop.log",
}
_SOURCE_JSON_NAMES = {
    Path(relative).name for relative in _ARTIFACT_PATHS.values()
    if relative.endswith(".json")
} | {"collection_manifest.json", "current_candidates.json", "target_candidates.json"}

_SECRET_NAMES = {
    "apikey", "openaiapikey", "accesstoken", "refreshtoken", "token",
    "authorization", "proxyauthorization", "auth", "password", "passwd",
    "secret", "clientsecret", "bearer", "bearertoken", "experimentalbearertoken",
    "securityid", "sessionid", "sessiontoken", "csrftoken", "privatekey",
}
_SECRET_LABEL = (
    r"(?:[\w-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|bearer[_-]?token|"
    r"auth[_-]?token|api[_-]?token|csrf[_-]?token|"
    r"credential(?:s)?|cookie|authorization|password|passwd|client[_-]?secret)|"
    r"token|secret|security[_-]?id|session[_-]?(?:id|token))"
)
_COOKIE_LINE_RE = re.compile(r"(?im)(\b(?:set[-_]?cookies?|cookies?)\s*[:=]\s*)[^\r\n]*")
_AUTH_LINE_RE = re.compile(r"(?im)(\b(?:proxy-)?authorization\s*[:=]\s*)[^\r\n]*")
_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b" + _SECRET_LABEL + r"\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|(?:bearer\s+)?[^\s,;}&]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+(?!\[REDACTED\])[A-Za-z0-9._~+/=-]+")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&]" + _SECRET_LABEL + r"=)[^&\s#]+"
)
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_URL_AUTH_RE = re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "；".join(_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _secret_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
    return (
        normalized in _SECRET_NAMES
        or "credential" in normalized
        or "cookie" in normalized
        or normalized.endswith(("apikey", "authorization", "bearertoken", "authtoken", "apitoken",
                                "accesstoken", "refreshtoken", "csrftoken", "sessiontoken", "securitytoken",
                                "password", "clientsecret"))
    )


def _redact_text(value: str, *, whole_header_lines: bool = True) -> str:
    if whole_header_lines:
        value = _AUTH_LINE_RE.sub(lambda match: match[1] + "[REDACTED]", value)
    value = _QUERY_SECRET_RE.sub(lambda match: match[1] + "[REDACTED]", value)
    value = _ASSIGNMENT_RE.sub(lambda match: match[1] + '"[REDACTED]"', value)
    # Redact the complete cookie last, including attributes after semicolons;
    # generic assignment redaction must not quote its already-redacted marker.
    value = _COOKIE_LINE_RE.sub(lambda match: match[1] + "[REDACTED]", value)
    value = _BEARER_RE.sub("Bearer [REDACTED]", value)
    value = _URL_AUTH_RE.sub(lambda match: match[1] + "[REDACTED]@", value)
    return _OPENAI_KEY_RE.sub("[REDACTED]", value)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _secret_key(key) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return _redact_text(value) if isinstance(value, str) else value


def _unique(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _first_count(*values: Any) -> int | None:
    return next((value for item in values if (value := _integer(item)) is not None), None)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass
class _JsonFile:
    value: Any = None
    state: str = "missing"


def _read_json(path: Path, warnings: list[str], *, required: bool = False) -> _JsonFile:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        if required:
            warnings.append(f"缺失文件：{path.name}。")
        return _JsonFile()
    except (OSError, UnicodeError) as error:
        warnings.append(f"无法读取文件：{path.name}（{type(error).__name__}）。")
        return _JsonFile(state="unreadable")
    if not raw.strip():
        warnings.append(f"空文件：{path.name}，可能正在写入，请稍后刷新。")
        return _JsonFile(state="empty")
    try:
        def invalid_constant(value: str) -> None:
            raise ValueError(f"Not a JSON number: {value}")

        return _JsonFile(json.loads(raw, parse_constant=invalid_constant), "valid")
    except (ValueError, RecursionError):
        warnings.append(f"JSON 无效：{path.name}，可能尚未写完，请稍后刷新。")
        return _JsonFile(state="invalid")


def _rows(file: _JsonFile, keys: tuple[str, ...], label: str,
          warnings: list[str]) -> list[dict[str, Any]] | None:
    if file.state != "valid":
        return None
    value = file.value
    if isinstance(value, dict):
        value = next((value[key] for key in keys if key in value), None)
    if not isinstance(value, list):
        warnings.append(f"JSON 结构无效：{label} 应包含岗位/记录数组。")
        return None
    valid = [row for row in value if isinstance(row, dict)]
    if len(valid) != len(value):
        warnings.append(f"{label} 中 {len(value) - len(valid)} 条非对象记录已忽略。")
        if not valid:
            return None
    return valid


def _identifier(job: dict[str, Any]) -> str:
    return _text(job.get("job_id") or job.get("dedupe_key") or
                 job.get("encrypt_job_id") or job.get("job_link") or job.get("link")).strip()


def _aliases(job: dict[str, Any]) -> set[str]:
    return {
        value for key in ("job_id", "dedupe_key", "encrypt_job_id", "job_link", "link")
        if (value := _text(job.get(key)).strip())
    }


def _merge_values(first: Any, second: Any) -> Any:
    if isinstance(first, dict) and isinstance(second, dict):
        merged = copy.deepcopy(first)
        for key, value in second.items():
            merged[key] = _merge_values(merged[key], value) if key in merged else copy.deepcopy(value)
        return merged
    if isinstance(first, list) and isinstance(second, list):
        return copy.deepcopy(_unique([*first, *second]))
    return copy.deepcopy(second if first is None or first == "" else first)


def _merge_jobs(rows: list[dict[str, Any]], warnings: list[str]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for row in rows:
        key = _identifier(row)
        if not key or key not in positions:
            if key:
                positions[key] = len(jobs)
            jobs.append(copy.deepcopy(row))
            continue
        previous = jobs[positions[key]]
        if (previous.get("classification") and row.get("classification")
                and previous["classification"] != row["classification"]):
            warnings.append(f"岗位 {key} 有重复且分层冲突，保留索引中首次已有分层，未重新判断。")
        merged = _merge_values(previous, row)
        for field, plural in (("source_file", "source_files"), ("source", "sources")):
            if previous.get(field) and row.get(field) and previous[field] != row[field]:
                merged[plural] = _unique([
                    *(previous.get(plural, []) if isinstance(previous.get(plural), list) else []),
                    previous[field], row[field],
                    *(row.get(plural, []) if isinstance(row.get(plural), list) else []),
                ])
        jobs[positions[key]] = merged
    return jobs


def _jd_text(job: dict[str, Any]) -> str:
    return _text(job.get("jd") or job.get("job_description") or job.get("description")).strip()


def _company(job: dict[str, Any]) -> str:
    for key in ("company", "company_name", "brand_name"):
        value = job.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("full_name") or value.get("company_name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _city(job: dict[str, Any]) -> str:
    value = _text(job.get("city") or job.get("location")).strip()
    if not value:
        for source in job.get("observed_in", []) if isinstance(job.get("observed_in"), list) else []:
            if isinstance(source, dict) and source.get("city"):
                value = _text(source["city"])
                break
    return re.split(r"[·・]", value, maxsplit=1)[0].strip()


def _model_state(job: dict[str, Any]) -> str | None:
    value = _text(job.get("model_status") or job.get("llm_status") or
                  _mapping(job.get("llm_review")).get("status")).strip().casefold()
    aliases = {"成功": "success", "已完成": "success", "completed": "success",
               "失败": "failed", "error": "failed", "未运行": "not_run", "未执行": "not_run"}
    return aliases.get(value, value) or None


def _jd_state(job: dict[str, Any]) -> str | None:
    if _jd_text(job) or job.get("jd_fetched") is True:
        return "success"
    status = _text(job.get("jd_status")).strip().casefold()
    if status in ("已抓取", "已获取", "success", "成功"):
        return "success"
    if status in ("failed", "失败"):
        return "failed"
    if status in ("paused", "暂停", "已暂停"):
        return "paused"
    if status or "jd" in job or job.get("jd_fetched") is False:
        return "missing"
    return None


def _classification_counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    """Return observed classifications without turning unfinished work into 0.

    With any unclassified rows, an absent category is unknown: one of those
    rows may eventually belong to it.  Only complete coverage (including a
    valid empty result array) establishes genuine zero-valued categories.
    """
    counts = Counter(
        value.strip() for job in jobs
        if isinstance(value := job.get("classification"), str) and value.strip() in CLASSIFICATIONS
    )
    complete = sum(counts.values()) == len(jobs)
    return {name: counts.get(name, 0) for name in CLASSIFICATIONS if complete or name in counts}


def _classification_summary(jobs: list[dict[str, Any]], known: bool) -> dict[str, Any]:
    """Describe classification coverage, not workflow/report completion."""
    if not known:
        return {"classification_counts": {}, "classification_status": "unavailable",
                "classified_count": None, "unclassified_count": None}
    counts = _classification_counts(jobs)
    classified = sum(counts.values())
    unclassified = len(jobs) - classified
    status = "complete" if not unclassified else "partial" if classified else "unclassified"
    return {"classification_counts": counts, "classification_status": status,
            "classified_count": classified, "unclassified_count": unclassified}


def _detail_lookup(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in rows:
        for key in _aliases(row):
            if key not in lookup or len(_jd_text(row)) > len(_jd_text(lookup[key])):
                lookup[key] = row
    return lookup


def _find_detail(job: dict[str, Any], lookup: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # The identifier order is deterministic even when several aliases exist.
    for key in ("job_id", "dedupe_key", "encrypt_job_id", "job_link", "link"):
        record = lookup.get(_text(job.get(key)).strip())
        if record is not None:
            return record
    return {}


def _load_result_bundle(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any], bool]:
    run_dir = Path(run_dir).expanduser().resolve()
    warnings: list[str] = []
    manifest_file = _read_json(run_dir / "run_manifest.json", warnings, required=True)
    manifest = _mapping(manifest_file.value)
    if manifest_file.state == "valid" and not isinstance(manifest_file.value, dict):
        warnings.append("JSON 结构无效：run_manifest.json 应为对象。")
    if isinstance(manifest.get("warnings"), list):
        warnings.extend(_text(value) for value in manifest["warnings"])
    source = run_dir / "prepared" / "merged_jobs_index.json"
    index = _read_json(source, warnings, required=True)
    subset = index.state == "missing"
    if subset:
        source = run_dir / "prepared" / "hai_tou_pool.json"
        index = _read_json(source, warnings, required=True)
        if index.state == "valid":
            warnings.append("仅有 hai_tou_pool.json：这是重点/海投岗位子集，不代表完整岗位总数；暂不投岗位可能未包含。")
    rows = _rows(index, ("jobs",), source.name, warnings)
    jobs = _merge_jobs(rows, warnings) if rows is not None else []
    metadata = _mapping(_mapping(index.value).get("metadata"))

    detail_path = run_dir / "prepared" / "jd_shortlists" / "details.json"
    detail_file = _read_json(detail_path, warnings)
    details = _rows(detail_file, ("jobs", "details", "records"), detail_path.name, warnings)
    if details is not None:
        lookup = _detail_lookup(details)
        for job in jobs:
            detail = _find_detail(job, lookup)
            enriched = False
            if not _company(job) and _company(detail):
                job["company"] = _company(detail)
                enriched = True
            if not _jd_text(job) and _jd_text(detail):
                job["jd"] = _jd_text(detail)
                enriched = True
            if enriched:
                job.setdefault("desktop_detail_source", str(detail_path))

    known = rows is not None
    jd_states = [_jd_state(job) for job in jobs]
    model_states = [_model_state(job) for job in jobs]
    jd_known = known and (not jobs or any(state is not None for state in jd_states))
    model_known = known and (not jobs or any(state is not None for state in model_states))
    stats = metadata if not subset else {}
    classification_summary = _classification_summary(jobs, known)
    if classification_summary["classification_status"] == "unclassified":
        warnings.append(f"当前 {len(jobs)} 岗尚无有效分层；三类数量未知，不能作为已生成的分层报告。")
    elif classification_summary["classification_status"] == "partial":
        warnings.append(
            f"分层尚未完整：当前 {classification_summary['classified_count']}/{len(jobs)} 岗已有有效分层，"
            f"{classification_summary['unclassified_count']} 岗未分层；分类计数仅覆盖已知岗位，未出现类别仍未知。"
        )
    summary = {
        "total": len(jobs) if known else None,
        **classification_summary,
        "jd_success": jd_states.count("success") if jd_known else _integer(stats.get("jd_fetched_count")),
        "llm_success": model_states.count("success") if model_known else _integer(stats.get("llm_success_count")),
        "llm_failed": model_states.count("failed") if model_known else _integer(stats.get("llm_failed_count")),
        "llm_not_run": (model_states.count("not_run") if known and all(state is not None for state in model_states)
                        else _integer(stats.get("llm_not_run_count"))),
        "is_subset": subset and known,
        "source": str(source) if known else None,
    }
    expected = _integer(metadata.get("unique_job_count"))
    if not subset and known and expected is not None and expected != len(jobs):
        warnings.append(f"索引元数据去重数 {expected} 与实际可读岗位 {len(jobs)} 不一致，以实际岗位数组为准。")
    artifacts: dict[str, str] = {}
    for name, relative in _ARTIFACT_PATHS.items():
        path = run_dir / relative
        try:
            if path.is_file():
                artifacts[name] = str(path)
        except OSError:
            pass
    return ({"jobs": jobs, "summary": summary, "manifest": manifest,
             "warnings": _unique(_redact_text(value) for value in warnings),
             "artifacts": artifacts, "run_dir": str(run_dir)}, metadata, not subset and known)


def load_run_results(run_dir: Path) -> dict[str, Any]:
    """Load the complete index, or a clearly marked pool-only compatibility view.

    A corrupt/empty existing index is never silently replaced by the smaller
    pool.  Duplicate IDs retain their first existing values and all provenance.
    Detail checkpoints may supplement missing company/JD on returned copies;
    ``desktop_detail_source`` records that provenance.  No files are modified.
    ``classification_status`` describes coverage of readable rows, not whether
    a report stage ran: unavailable/unclassified/partial/complete.  Missing
    category counts stay absent until coverage is complete; unclassified rows
    are recorded separately in ``unclassified_count``.
    """
    return _load_result_bundle(run_dir)[0]


def _date_value(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _history_date(path: Path, manifest: dict[str, Any]) -> tuple[Any, float]:
    started = manifest.get("started_at") or manifest.get("last_started_at")
    parsed = _date_value(started)
    if parsed is not None:
        return started, parsed
    match = re.match(r"^(\d{8}-\d{6}(?:-\d{6})?)", path.name)
    if match:
        stamp = match[1]
        try:
            date = datetime.strptime(stamp, "%Y%m%d-%H%M%S-%f" if len(stamp) > 15 else "%Y%m%d-%H%M%S")
            return date.isoformat(timespec="seconds"), date.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    try:
        return started or None, path.stat().st_mtime
    except OSError:
        return started or None, float("-inf")


def list_run_history(root: Path, limit: int = 100) -> list[dict[str, Any]]:
    """List even empty/broken run directories, stably newest first."""
    if limit <= 0:
        return []
    runs = Path(root).expanduser() / "runs"
    try:
        paths = [path for path in runs.iterdir() if path.is_dir()]
    except OSError:
        return []
    history: list[tuple[float, str, dict[str, Any]]] = []
    for path in paths:
        result, metadata, _ = _load_result_bundle(path)
        manifest, summary = result["manifest"], result["summary"]
        started, sort_time = _history_date(path, manifest)
        status = _text(manifest.get("status")).strip() or "unknown"
        entry = {
            "path": result["run_dir"], "name": path.name, "started_at": started,
            "status": status, "city": manifest.get("city") or metadata.get("city") or None,
            "sample_size": manifest.get("sample_size", metadata.get("sample_size")),
            "classification_counts": summary["classification_counts"],
            "classification_status": summary["classification_status"],
            "classified_count": summary["classified_count"],
            "unclassified_count": summary["unclassified_count"],
            "unique_job_count": summary["total"], "warnings": result["warnings"],
        }
        history.append((sort_time, path.name.casefold(), entry))
    history.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [entry for _, _, entry in history[:limit]]


def _checkpoint_rows(path: Path, keys: tuple[str, ...], warnings: list[str]) -> list[dict[str, Any]] | None:
    return _rows(_read_json(path, warnings), keys, path.name, warnings)


def _latest_records(rows: list[dict[str, Any]], identity: str = "job_id") -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for number, row in enumerate(rows):
        key = _text(row.get(identity)) or (_identifier(row) if identity == "job_id" else "")
        records[key or f"__anonymous_{number}"] = row
    return list(records.values())


def _affected_jobs(records: list[dict[str, Any]] | None, state: str,
                   completed: set[str]) -> int | None:
    if records is None:
        return None
    total = 0
    counted: set[str] = set()
    for record in _latest_records(records, "batch"):
        paused = (record.get("status") == "paused" or record.get("security_or_login_gate") is True
                  or record.get("pause_reason") == "cdp_setup_failed")
        matches = paused if state == "paused" else record.get("status") == "failed" and not paused
        if not matches:
            continue
        missing = record.get("missing_job_ids")
        if isinstance(missing, list):
            keys = {_text(key) for key in missing if _text(key)} - completed - counted
            counted.update(keys)
            total += len(keys)
        else:
            count, fetched = _integer(record.get("count")), _integer(record.get("fetched_count"))
            if count is None or fetched is None:
                return None
            total += max(0, count - fetched)
    return total


def _stage_info(manifest: dict[str, Any]) -> tuple[str | None, str, dict[str, str]]:
    stages = _mapping(manifest.get("stages"))
    statuses = {
        str(label): _text(entry.get("status")) if isinstance(entry, dict) else _text(entry)
        for label, entry in stages.items()
        if (isinstance(entry, dict) and entry.get("status")) or isinstance(entry, str)
    }
    active = [label for label, state in statuses.items() if state == "running"]
    stage = _text(manifest.get("current_stage") or manifest.get("stage")).strip() or None
    if not stage:
        if active:
            stage = active[-1]
        elif statuses:
            dated = [(date, index, label) for index, label in enumerate(statuses)
                     if (date := _date_value(_mapping(stages.get(label)).get("finished_at") or
                                             _mapping(stages.get(label)).get("started_at"))) is not None]
            stage = max(dated)[2] if dated else next(reversed(statuses))
            # A sparse resumed checkpoint may have no timestamps but an older
            # completed report can still occupy the last dictionary position.
            state = _text(manifest.get("status"))
            terminal = "paused" if state == "paused" else "failed" if state in ("failed", "stopped_or_failed") else None
            matching = [label for label, value in statuses.items() if value == terminal] if terminal else []
            if matching:
                stage = matching[-1]
    return stage, _text(manifest.get("status")).strip() or "unknown", statuses


def read_live_snapshot(run_dir: Path, collection_root: Path | None = None) -> dict[str, Any]:
    """Read current JSON checkpoints, including in-flight per-job progress.

    Counts are observations, not estimates based on log text or requested
    sample sizes.  A failed/half-written optional file produces a warning and
    leaves unavailable counts unknown.  Failure/pause counts use jobs as units.
    """
    result, metadata, full_index = _load_result_bundle(run_dir)
    run = Path(result["run_dir"])
    manifest, summary, jobs = result["manifest"], result["summary"], result["jobs"]
    warnings = list(result["warnings"])
    stage, status, stage_statuses = _stage_info(manifest)
    snapshot: dict[str, Any] = {
        "stage": stage, "status": status,
        **{key: None for key in (
            "raw_record_count", "unique_job_count", "jd_success", "jd_total", "jd_missing",
            "jd_failed", "jd_paused", "llm_success", "llm_total", "llm_failed", "llm_fallback",
        )},
        "classification_counts": summary["classification_counts"],
        "classification_status": summary["classification_status"],
        "classified_count": summary["classified_count"],
        "unclassified_count": summary["unclassified_count"],
        "stage_statuses": stage_statuses, "warnings": warnings,
    }
    raw_file = _read_json(run / "raw_sample.json", warnings)
    raw_metadata = _mapping(_mapping(raw_file.value).get("metadata"))
    raw_jobs = _rows(raw_file, ("jobs",), "raw_sample.json", warnings)
    source_stats = _mapping(manifest.get("source_stats"))
    sample_stats = _mapping(manifest.get("sample_stats"))
    snapshot["raw_record_count"] = _first_count(
        raw_metadata.get("raw_record_count"), metadata.get("raw_record_count"),
        source_stats.get("raw_record_count"), sample_stats.get("raw_record_count"),
    )
    source = Path(collection_root).expanduser() if collection_root is not None else None
    if source is None and isinstance(manifest.get("source_root"), str) and manifest["source_root"]:
        source = Path(manifest["source_root"]).expanduser()
    if source is not None:
        collection_file = _read_json(source / "collection_manifest.json", warnings)
        collection = _mapping(collection_file.value)
        records = collection.get("records")
        if isinstance(records, list) and all(isinstance(row, dict) for row in records):
            counts = [_integer(row.get("job_count")) for row in records]
            if all(count is not None for count in counts):
                collected = sum(counts)
                if snapshot["raw_record_count"] is None or stage == "岗位采集":
                    snapshot["raw_record_count"] = collected
        if snapshot["raw_record_count"] is None:
            snapshot["raw_record_count"] = _integer(collection.get("raw_record_count"))
    if snapshot["raw_record_count"] is None and raw_jobs is not None:
        snapshot["raw_record_count"] = len(raw_jobs)
        warnings.append("原始记录数仅来自 raw_sample.json 样本，不代表完整采集总量。")
    snapshot["unique_job_count"] = _first_count(
        summary["total"] if full_index else None, raw_metadata.get("unique_job_count"),
        metadata.get("unique_job_count"), sample_stats.get("unique_job_count"),
    )
    if snapshot["unique_job_count"] is None and raw_jobs is not None:
        snapshot["unique_job_count"] = len(_merge_jobs(raw_jobs, warnings))

    prepared = run / "prepared"
    jd_dir = prepared / "jd_shortlists"
    shortlist = _checkpoint_rows(jd_dir / "jd_input.json", ("jobs",), warnings)
    shortlist = _merge_jobs(shortlist, warnings) if shortlist is not None else None
    jd_file = _read_json(jd_dir / "details_manifest.json", warnings)
    if jd_file.state == "valid" and not isinstance(jd_file.value, dict):
        warnings.append("JSON 结构无效：details_manifest.json 应为对象。")
    jd_manifest = _mapping(jd_file.value) if jd_file.state == "valid" else _mapping(manifest.get("jd_manifest"))
    jd_records = jd_manifest.get("records")
    if not isinstance(jd_records, list) or any(not isinstance(row, dict) for row in jd_records):
        jd_records = None
    snapshot["jd_total"] = len(shortlist) if shortlist is not None else _integer(jd_manifest.get("total_jobs"))
    details = _checkpoint_rows(jd_dir / "details.json", ("jobs", "details", "records"), warnings)
    details_known = details is not None
    detail_rows = list(details or [])
    try:
        batch_paths = sorted((jd_dir / "detail_batches").glob("details_*.json"))
    except OSError:
        batch_paths = []
        warnings.append("无法读取 JD 批次检查点目录。")
    for batch_path in batch_paths:
        batch = _checkpoint_rows(batch_path, ("jobs", "details", "records"), warnings)
        if batch is not None:
            details_known = True
            detail_rows.extend(batch)
    # Index fields can also be the only surviving durable JD evidence.
    detail_rows.extend(job for job in jobs if _jd_state(job) == "success")
    lookup = _detail_lookup(detail_rows)
    completed: set[str] = set()
    if shortlist is not None:
        successful = 0
        for job in shortlist:
            detail = _find_detail(job, lookup)
            if detail and (_jd_text(detail) or _jd_state(detail) == "success"):
                successful += 1
                completed.update(_aliases(job))
        if details_known or summary["jd_success"] is not None or not shortlist:
            snapshot["jd_success"] = successful
    elif details_known:
        snapshot["jd_success"] = len(_merge_jobs([row for row in detail_rows if _jd_text(row)], warnings))
        completed = set().union(*(_aliases(row) for row in detail_rows if _jd_text(row))) if detail_rows else set()
    if snapshot["jd_success"] is None and jd_records is not None:
        fetched = [_integer(row.get("fetched_count")) for row in _latest_records(jd_records, "batch")]
        if all(value is not None for value in fetched):
            snapshot["jd_success"] = sum(fetched)
    if snapshot["jd_success"] is None:
        snapshot["jd_success"] = summary["jd_success"] if full_index else None
    if snapshot["jd_total"] is not None and snapshot["jd_success"] is not None:
        snapshot["jd_missing"] = max(0, snapshot["jd_total"] - snapshot["jd_success"])
    snapshot["jd_failed"] = _affected_jobs(jd_records, "failed", completed)
    snapshot["jd_paused"] = _affected_jobs(jd_records, "paused", completed)
    if snapshot["jd_total"] == 0:
        snapshot.update(jd_success=0, jd_missing=0, jd_failed=0, jd_paused=0)

    llm_file = _read_json(prepared / "llm_manifest.json", warnings)
    if llm_file.state == "valid" and not isinstance(llm_file.value, dict):
        warnings.append("JSON 结构无效：llm_manifest.json 应为对象。")
    llm_manifest = _mapping(llm_file.value) if llm_file.state == "valid" else _mapping(manifest.get("llm_manifest"))
    reviews = _checkpoint_rows(prepared / "llm_reviews.json", ("reviews",), warnings)
    candidates: list[dict[str, Any]] | None = None
    jd_text_available = shortlist is not None and any(_jd_text(_find_detail(job, lookup)) for job in shortlist)
    if shortlist is not None and (details_known or jd_text_available or not shortlist):
        candidates = [job for job in shortlist if _jd_text(_find_detail(job, lookup))]
    candidate_count = len(candidates) if candidates is not None else None
    reported_candidates = _integer(llm_manifest.get("candidate_count"))
    snapshot["llm_total"] = _first_count(candidate_count, reported_candidates)
    if candidate_count is not None and reported_candidates is not None and candidate_count != reported_candidates:
        warnings.append(f"模型清单候选数 {reported_candidates} 与当前 JD 候选 {candidate_count} 不一致，以当前候选为准。")
    if reviews is not None:
        reviews = _latest_records(reviews)
        # A temporarily unavailable JD file must not hide saved model results.
        # Restrict foreign/stale IDs to the known run scope, not to only those
        # descriptions that happened to be readable during this poll.
        scope = shortlist if shortlist is not None else jobs if full_index else None
        if scope is not None:
            eligible = set().union(*(_aliases(job) for job in scope)) if scope else set()
            reviews = [review for review in reviews if _aliases(review) & eligible]
        states = [_model_state({"llm_review": review}) for review in reviews]
        snapshot["llm_success"] = states.count("success")
        snapshot["llm_failed"] = states.count("failed")
    else:
        snapshot["llm_success"] = _first_count(llm_manifest.get("completed_count"),
                                               summary["llm_success"] if full_index else None)
        snapshot["llm_failed"] = _first_count(llm_manifest.get("failed_count"),
                                              summary["llm_failed"] if full_index else None)
    snapshot["llm_fallback"] = _first_count(llm_manifest.get("fallback_count"),
                                            snapshot["llm_failed"] if reviews is not None else None)
    if snapshot["llm_total"] == 0:
        snapshot.update(llm_success=0, llm_failed=0, llm_fallback=0)
    if not stage:
        if full_index and "report" in result["artifacts"]:
            snapshot["stage"] = "生成报告"
        elif reviews is not None or llm_file.state == "valid":
            snapshot["stage"] = "Codex语义复核"
        elif details_known or jd_records is not None:
            snapshot["stage"] = "JD补抓"
        elif full_index:
            snapshot["stage"] = "列表前处理"
    snapshot["warnings"] = _unique(_redact_text(value) for value in warnings)
    return snapshot


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _salary_key(job: dict[str, Any]) -> tuple[bool, float, float]:
    parsed = _mapping(job.get("salary_parsed"))
    lower = parsed.get("lower_k")
    upper = parsed.get("upper_k")
    if lower is not None:
        return False, _number(lower), _number(upper, _number(lower))
    salary = _text(job.get("salary")).replace(",", "").casefold()
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:[-~—–至]\s*(\d+(?:\.\d+)?))?\s*(k|千|万|元)", salary)
    if not match:
        return True, 0.0, 0.0
    low, high = float(match[1]), float(match[2] or match[1])
    unit = {"k": 1, "千": 1, "万": 10, "元": 0.001}[match[3]]
    if re.search(r"/\s*年|每年|年薪|万/年|万元/年", salary):
        unit /= 12
    if re.search(r"/\s*[天日]|每天|每日", salary):
        unit *= 21.75
    return False, low * unit, high * unit


def filter_jobs(jobs: Iterable[dict[str, Any]], search: str = "", classification: str = "全部",
                city: str = "全部", jd_status: str = "全部", model_status: str = "全部",
                sort_by: str = "priority", reverse: bool = False) -> list[dict[str, Any]]:
    """Filter/sort supplied facts only; input objects are never changed.

    Priority defaults to classification order, descending score, title and ID.
    Other columns sort ascending unless ``reverse`` is requested; ties retain
    priority order.  Salary is normalized to approximate monthly K for sorting.
    """
    keywords = search.casefold().split()
    class_aliases = {"重点": "重点投递", "海投": "海投池"}
    classification = class_aliases.get(classification, classification)
    jd_aliases = {"已抓取": "success", "已获取": "success", "成功": "success",
                  "待核实": "missing", "缺失": "missing", "失败": "failed", "暂停": "paused"}
    model_aliases = {"成功": "success", "失败": "failed", "未运行": "not_run", "规则回退": "fallback"}
    jd_filter = jd_aliases.get(jd_status, jd_status)
    model_filter = model_aliases.get(model_status, model_status)
    selected: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if classification not in ("全部", "", None) and job.get("classification") != classification:
            continue
        if city not in ("全部", "全部城市", "", None) and _city(job) != city:
            continue
        jd_state = _jd_state(job)
        if jd_filter not in ("全部", "", None):
            if jd_filter == "missing" and jd_state == "success":
                continue
            if jd_filter != "missing" and jd_state != jd_filter:
                continue
        model_state = _model_state(job)
        if model_filter not in ("全部", "", None):
            if model_filter == "fallback" and model_state not in ("failed", "not_run"):
                continue
            if model_filter != "fallback" and model_state != model_filter:
                continue
        searchable = " ".join(_text(job.get(key)) for key in (
            "title", "company", "company_name", "brand_name", "skills", "skill_tags", "city", "location",
        )) + " " + _city(job)
        if all(keyword in searchable.casefold() for keyword in keywords):
            selected.append(job)
    priorities = {name: index for index, name in enumerate(CLASSIFICATIONS)}
    selected.sort(key=lambda job: (
        priorities.get(job.get("classification") if isinstance(job.get("classification"), str) else None,
                       len(priorities)), -_number(job.get("score")),
        _text(job.get("title")).casefold(), _identifier(job),
    ))
    column_keys = {
        "title": lambda job: _text(job.get("title")).casefold(),
        "company": lambda job: _company(job).casefold(),
        "city": lambda job: _city(job).casefold(),
        "salary": _salary_key,
        "dimensions": lambda job: _number(job.get("jd_dimension_count"), -1),
    }
    if sort_by == "priority":
        if reverse:
            selected.reverse()
    elif sort_by in column_keys:
        selected.sort(key=column_keys[sort_by], reverse=reverse)
    else:
        raise ValueError(f"不支持的排序字段：{sort_by}")
    return selected


def _items(*values: Any) -> list[str]:
    items: list[str] = []
    for value in values:
        values_to_add = value if isinstance(value, (list, tuple)) else [value] if value else []
        items.extend(_text(item).strip() for item in values_to_add if _text(item).strip())
    return _unique(items)


def format_job_detail(job: dict[str, Any]) -> str:
    """Render evidence and provenance without treating a recruiter as a company."""
    job = _redact(job)
    review = _mapping(job.get("llm_review"))
    display = lambda value: _text(value).strip() or "未提供（待核实）"
    if job.get("review_format") == "simple-v1" or review.get("review_format") == "simple-v1":
        from llm_client import JsonSchemaValidationError, simple_review_payload
        payload = None
        if (review.get("review_format") == "simple-v1" and job.get("model_status") == "success" and review.get("status") == "success"
                and str(review.get("job_id", "")) == _identifier(job)):
            try:
                payload = simple_review_payload(review)
            except JsonSchemaValidationError:
                pass
        lines = [f"岗位：{display(job.get('title'))}", f"公司：{_company(job) or '未知公司'}",
                 f"岗位 ID：{display(_identifier(job))}", f"已有分层：{display(job.get('classification'))}",
                 f"分层依据：{display(job.get('reason'))}",
                 f"AI判断：{display(payload['判断']) if payload else '未完成有效判断（待人工核实）'}",
                 f"AI理由：{display(payload['理由']) if payload else '未产生模型理由'}"]
        if job.get("model_error") or review.get("error"):
            lines.append("技术问题（不是AI理由）：" + display(job.get("model_error") or review.get("error")))
        metadata = {key: review[key] for key in ("review_format", "status", "review_model", "requested_model", "model",
                    "reasoning_effort", "api_mode", "reviewed_at", "cache_signature", "cache_input_signature", "cache_version")
                    if key in review}
        lines.extend(("运行元数据：" + display(metadata), "人工选择审计：" + display(job.get("review_selection")),
                      "人工确认剔除于：" + display(job.get("excluded_at")),
                      "检索来源：" + display(job.get("observed_in") or job.get("sources") or job.get("source_files")),
                      "完整 JD 请查看原文页。", f"岗位链接：{display(job.get('job_link') or job.get('link'))}"))
        return "\n".join(lines)
    model_state = _model_state(job)
    model_labels = {"success": "success（语义复核成功）", "failed": "failed（规则回退）",
                    "not_run": "not_run（未运行）"}
    lines = [
        f"岗位：{display(job.get('title'))}",
        f"公司：{_company(job) or '未知公司（未提供公司字段）'}",
        f"招聘者：{display(job.get('boss_name'))}  {display(job.get('boss_title'))}",
        f"城市/地点：{display(job.get('location') or job.get('city') or _city(job))}",
        f"薪资：{display(job.get('salary'))}",
        f"经验/学历：{display(_items(job.get('experience'), job.get('experience_tags'), job.get('tags'), job.get('job_labels'), job.get('tags_list')))}",
        f"技能：{display(_items(job.get('skills'), job.get('skill_tags')))}",
        f"岗位 ID：{display(_identifier(job))}",
        f"已有分层：{display(job.get('classification'))}  排序分：{display(job.get('score'))}",
        f"分层依据：{display(_items(job.get('reason'), job.get('reasons')))}",
        "", "检索来源：",
    ]
    sources = job.get("observed_in")
    if isinstance(sources, list) and sources:
        for index, source in enumerate(sources, 1):
            if isinstance(source, dict):
                labels = (("轨道", "track"), ("城市", "city"), ("关键词", "keyword"),
                          ("薪资筛选", "salary_filter"), ("经验筛选", "experience_filter"),
                          ("学历筛选", "degree"), ("组合", "combination"),
                          ("文件", "source_file"), ("源行", "source_line_number"))
                lines.append(f"  {index}. " + "；".join(
                    f"{label}：{display(source.get(key))}" for label, key in labels
                ))
            else:
                lines.append(f"  {index}. {display(source)}")
    else:
        lines.append("  " + display(job.get("sources") or job.get("source_files") or job.get("source_file") or job.get("source")))
    if job.get("desktop_detail_source"):
        lines.append(f"  补充公司/JD来源：{display(job['desktop_detail_source'])}")
    dimensions = _mapping(job.get("jd_dimensions") or job.get("dimensions"))
    lines.extend(["", f"六维证据（已有命中数：{display(job.get('jd_dimension_count'))}/6）："])
    for name in _unique([*DIMENSIONS, *dimensions]):
        dimension = _mapping(dimensions.get(name))
        present = dimension.get("present")
        label = "命中" if present is True else "未命中" if present is False else "待核实"
        lines.append(f"  {name}：{label}")
        lines.append(f"    JD原文证据：{display(dimension.get('evidence'))}")
        if dimension.get("matched_terms"):
            lines.append(f"    已有匹配词：{display(dimension['matched_terms'])}")
    lines.extend([
        f"JD证据：{display(job.get('jd_evidence'))}",
        f"模型证据：{display(job.get('model_evidence'))}",
        f"列表召回证据：{display(job.get('list_match_evidence'))}",
        f"列表排除线索：{display(job.get('list_exclusion_evidence'))}",
        "", f"JD状态：{display(job.get('jd_status'))}",
        f"模型状态：{model_labels.get(model_state, model_state or '未知（未提供状态）')}",
        f"模型：{display(review.get('review_model') or review.get('requested_model'))}",
        f"模型摘要：{display(review.get('summary'))}",
        f"模型错误：{display(job.get('model_error') or review.get('error'))}",
        "", "能力差距：",
    ])
    gaps = _items(job.get("ability_gaps"), job.get("gaps"), review.get("ability_gaps"), review.get("gaps"))
    lines.extend(f"  - {item}" for item in gaps or ["未提供（不能据此认为没有差距）"])
    verification = _items(job.get("verification"), job.get("verify"), job.get("list_verification"),
                          review.get("verification_items"), review.get("verification"), review.get("verify"))
    if not _jd_text(job):
        verification.append("JD正文未提供，六项职责及岗位要求待核实")
    if model_state != "success":
        verification.append("模型未完成，语义结论与能力差距待核实")
    lines.extend(["", "需核实："])
    lines.extend(f"  - {item}" for item in _unique(verification) or ["未提供（待人工核实）"])
    lines.extend(["", "完整 JD：", _jd_text(job) or "未提供 JD 正文（待补抓/核实）",
                  "", f"岗位链接：{display(job.get('job_link') or job.get('link'))}"])
    return "\n".join(lines)


def _csv_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    else:
        text = _text(value)
    stripped = text.lstrip(" \t\r\n\v\f\ufeff")
    if (stripped and stripped[0] in "=+-@") or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def export_jobs(path: Path, jobs: list[dict[str, Any]]) -> Path:
    """Export selected rows only, sanitizing credentials and spreadsheet formulas.

    JSON uses a ``{total, jobs}`` envelope.  CSV retains original fields, JSON
    encodes nested evidence, and uses UTF-8 with a BOM for Windows Excel.
    Existing canonical source JSON artifacts cannot be overwritten as exports.
    """
    path = Path(path).expanduser()
    suffix = path.suffix.casefold()
    if suffix not in (".json", ".csv"):
        raise ValueError("导出格式仅支持 .json 或 .csv。")
    if path.exists() and path.name.casefold() in {name.casefold() for name in _SOURCE_JSON_NAMES}:
        raise ValueError("不能覆盖已有源结果 JSON；请另选导出文件名。")
    if any(not isinstance(job, dict) for job in jobs):
        raise ValueError("导出的每个岗位必须是 JSON 对象。")
    rows = _redact(jobs)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig" if suffix == ".csv" else "utf-8",
            newline="", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            if suffix == ".json":
                json.dump({"total": len(rows), "jobs": rows}, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
            else:
                fields = [
                    "job_id", "title", "company", "city", "location", "salary", "tags", "skills",
                    "classification", "reason", "score", "jd_status", "model_status", "jd_dimension_count",
                    "jd_dimensions", "jd_evidence", "model_evidence", "ability_gaps", "gaps",
                    "verification", "verify", "observed_in", "job_link", "jd", "llm_review",
                ]
                fields.extend(sorted({key for row in rows for key in row} - set(fields)))
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writerow({key: _csv_value(key) for key in fields})
                for original in rows:
                    row = dict(original)
                    row["company"] = _company(row) or "未知公司"
                    row.setdefault("city", _city(row))
                    row.setdefault("job_link", row.get("link", ""))
                    row.setdefault("model_status", _model_state(row) or "unknown")
                    writer.writerow({key: _csv_value(row.get(key)) for key in fields})
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path
