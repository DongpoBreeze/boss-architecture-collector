"""Fixed-source, single-task, explicitly gated desktop review workbench.

All task/file operations and stage executors run on a worker.  Tk only reads
the immutable view delivered by the queue; neither navigation nor confirmation
can fall through to an executor. Collection and review have separate pages,
inputs and execution controls; switching pages never starts either workflow.
"""

from __future__ import annotations

import argparse
import copy
import inspect
import json
import os
from pathlib import Path
import queue
import stat
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable
from urllib.parse import urlsplit

import desktop_data
from collection_sources import review_config_for_source
from desktop_collection import CollectionPage
from desktop_delivery import DeliveryPage
from desktop_tasks import (ReviewTask, STAGES, STAGE_LABELS, TaskStateError, job_id,
                           stage_selection_version, _file_version)
from desktop_preferences import load_preferences, save_preferences
from desktop_theme import configure_combobox_popdown, configure_styles, normalize_theme, palette_for
from desktop_tk_lifecycle import BooleanVar, StringVar, destroy_tk_widget, install_tk_lifecycle
from process_architecture import detail_completeness, report_decision, report_pool_eligible
import project_llm_settings
from llm_client import (CodexResponsesClient, JsonSchemaValidationError,
                        SIMPLE_REVIEW_FORMAT, simple_review_payload)
from project_resources import DEFAULT_CONFIG, PROJECT_ROOT, default_state_root
from review_pipeline import (
    StageControl, _analyze_ai_stage, _read_json, _safe_path, execute_stage,
    prepare_browser, reset_stage as reset_review_stage,
)
from review_rules import default_review_rules
from screening_policy import (
    AI_DEFAULT_MODEL, AI_DEFAULT_REASONING_EFFORT, AI_DEFAULT_WORKERS,
    JD_DEFAULT_WORKERS, JD_SCROLL_COUNT, LIST_QUEUE_LABELS, MAX_WORKERS,
)


REVIEW_SOURCE_CONFIGS = default_state_root() / "config" / "review-source-catalogs"
CITY_CHOICES = ("全部城市", "上海", "深圳", "杭州", "广州", "成都", "北京")
TRACK_CHOICES = {"当前＋成长": "all", "当前岗位": "current", "成长目标": "target"}
SAMPLE_CHOICES = ("全部岗位（不限）", "自定义数量")
BROWSER_MODE_CHOICES = ("单浏览器（Chrome）", "Chrome + Edge 各半")
BROWSER_MODE_VALUES = {
    BROWSER_MODE_CHOICES[0]: "single",
    BROWSER_MODE_CHOICES[1]: "dual",
}
BROWSER_MODE_LABELS = {value: label for label, value in BROWSER_MODE_VALUES.items()}
DEFAULT_JD_BROWSER_MODE = "dual"
DEFAULT_CHROME_PROFILE = Path.home() / ".boss-zhipin-scraper" / "chrome-profile"
DEFAULT_EDGE_PROFILE = Path.home() / ".boss-zhipin-scraper" / "edge-profile"
BROWSER_STATUS_LABELS = {
    "pending": "未开始", "running": "运行中", "completed": "完成",
    "success": "完成", "paused": "已暂停", "stopped": "已停止",
    "waiting": "等待登录 / 验证", "failed": "失败", "partial": "部分完成",
    "missing": "未提供",
}
STATUS_LABELS = {
    "pending": "尚未运行", "running": "正在运行", "completed": "完成 · 等待确认",
    "paused": "已暂停，可继续", "stopped": "已停止，可继续", "waiting": "等待人工处理",
    "failed": "运行失败，可重试", "stale": "上游已修改 · 待更新",
}
STAGE_DESCRIPTIONS = {
    "import": "从唯一绑定的 raw/current 与 raw/target 导入、去重、抽样，保留全部检索来源。",
    "shortlist": "列表方向队列不等于最终可投；默认保留明确相关＋待核实，仍需人工确认后补 JD。",
    "fetch-jd": "只为上一步确认的岗位补抓 JD；复用本任务检查点，登录或安全验证时停住。",
    "llm": "只复核已确认名单；AI明确判断符合才建议保留、明确不符合才建议剔除。失败、未运行、JD不完整或信息不足均进入待核实，不自动归入保留或剔除。模型设置仅作用于本项目。",
    "report": "生成全部导入岗位的追踪报告；人工确认剔除项明确排除出可投池。完成后可点击“打开报告”查看 Markdown，无需先确认报告完成。",
}
PAGE_SIZE = 20
REVIEW_STAGES = STAGES[:-1]
MAX_LOG_CHARS = 250_000
POLL_MS = 75
ALL_LIST_QUEUES = "全部队列"
LEGACY_LIST_QUEUE_NOTICE = "旧版本，需重跑初筛更新队列"
API_MODE_CHOICES = {
    project_llm_settings.API_MODE_LABELS[project_llm_settings.API_MODE_RESPONSES]: project_llm_settings.API_MODE_RESPONSES,
    project_llm_settings.API_MODE_LABELS[project_llm_settings.API_MODE_CHAT_COMPLETIONS]: project_llm_settings.API_MODE_CHAT_COMPLETIONS,
}
API_MODE_LABELS = {value: label for label, value in API_MODE_CHOICES.items()}


def _safe(value: Any) -> str:
    return desktop_data._redact_text(str(value))


def _display(value: Any) -> str:
    if value is None or value == "":
        return "未提供"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(desktop_data._redact(value), ensure_ascii=False, indent=2)
    return _safe(value)


def _positive(value: str, label: str, *, maximum: int | None = None, optional: bool = False) -> int | None:
    if optional and not value.strip():
        return None
    try:
        result = int(value.strip())
    except (ValueError, TypeError):
        raise ValueError(f"{label}须为正整数。") from None
    if result < 1 or (maximum is not None and result > maximum):
        suffix = f" 1–{maximum}" if maximum else "正整数"
        raise ValueError(f"{label}须为{suffix}。")
    return result


def _supported_kwargs(function: Callable[..., Any], options: dict[str, Any]) -> dict[str, Any]:
    """Pass new optional UI settings only when the injected backend supports them.

    Review UI tests and older installations inject callables with the original
    signatures.  Filtering here keeps those callables working while allowing
    the dual-browser pipeline to receive its complete configuration.
    """
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return dict(options)
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return dict(options)
    accepted = {parameter.name for parameter in parameters}
    return {key: value for key, value in options.items() if key in accepted}


def _browser_status(value: Any, *, fallback: str = "pending") -> str:
    """Normalize backend/browser result values to the labels shown by the UI."""
    if not isinstance(value, (dict, str, bool, type(None))) and hasattr(value, "status"):
        value = getattr(value, "status")
    if isinstance(value, dict):
        if isinstance(value.get("status"), str):
            value = value["status"]
        elif value.get("success") is True or value.get("ok") is True:
            value = "completed"
        elif value.get("success") is False or value.get("ok") is False or value.get("error"):
            value = "failed"
    if isinstance(value, bool):
        value = "completed" if value else "failed"
    normalized = str(value or fallback).strip().lower()
    aliases = {"done": "completed", "success": "completed", "ok": "completed",
               "error": "failed", "failure": "failed", "in_progress": "running",
               "未开始": "pending", "完成": "completed", "失败": "failed",
               "暂停": "paused", "停止": "stopped"}
    return aliases.get(normalized, normalized if normalized in BROWSER_STATUS_LABELS else fallback)


def _browser_result_map(value: Any) -> dict[str, str]:
    """Extract per-browser statuses from a pipeline/preparer result if present."""
    if not isinstance(value, dict):
        return {}
    if any(browser in value for browser in ("chrome", "edge")):
        return {browser: _browser_status(value[browser]) for browser in ("chrome", "edge") if browser in value}
    direct_browser = str(value.get("browser") or "").strip().lower()
    if direct_browser in {"chrome", "edge"} and ("status" in value or "success" in value or "ok" in value):
        return {direct_browser: _browser_status(value)}
    candidates = (value.get("browser_results") or value.get("browser_statuses")
                  or value.get("browsers") or value.get("browser_status"))
    result: dict[str, str] = {}
    if not isinstance(candidates, dict):
        for browser in ("chrome", "edge"):
            for key in (f"{browser}_status", f"{browser}_state"):
                if key in value:
                    result[browser] = _browser_status(value[key])
                    break
        return result
    for browser in ("chrome", "edge"):
        if browser in candidates:
            result[browser] = _browser_status(candidates[browser])
    return result


def _saved_list_queue(job: dict[str, Any]) -> tuple[str | None, bool]:
    """Read a saved queue, or visibly map old saved decisions without scoring.

    The second value marks compatibility-only labels. A legacy pass without
    an old priority is uncertain, never upgraded to an explicit direction.
    """
    saved = job.get("list_queue")
    if isinstance(saved, str) and saved in LIST_QUEUE_LABELS:
        return saved, False
    priority = job.get("list_priority")
    if job.get("list_pass") is False:
        return "excluded", True
    if job.get("list_pass") is True:
        return ("relevant" if priority == "优先补JD" else "verify"), True
    old = {"优先补JD": "relevant", "低优先待JD": "verify", "暂不优先": "excluded"}
    return (old.get(priority) if isinstance(priority, str) else None), True


def _list_queue_label(job: dict[str, Any]) -> str:
    key, legacy = _saved_list_queue(job)
    label = LIST_QUEUE_LABELS.get(key, "未标队列")
    return label + ("（旧版兼容）" if legacy else "")


def _list_queue_reason(job: dict[str, Any]) -> str:
    _key, legacy = _saved_list_queue(job)
    if legacy:
        return (LEGACY_LIST_QUEUE_NOTICE + "；仅按已保存的 list_pass / list_priority 兼容显示，未应用新规则。"
                + f" 原优先级：{_display(job.get('list_priority'))}")
    return _display(job.get("list_queue_reason") or "未保存队列原因，请核实结果。")


def _list_queue_summary(jobs: list[dict[str, Any]]) -> tuple[dict[str, int], int]:
    """Count persisted row fields, never rerun a policy in the desktop view."""
    counts = {key: 0 for key in LIST_QUEUE_LABELS}
    legacy_count = 0
    for job in jobs:
        key, legacy = _saved_list_queue(job)
        if key in counts:
            counts[key] += 1
        legacy_count += legacy
    return counts, legacy_count


def _completed_chain(manifest: dict[str, Any], *, through_report: bool = False) -> bool:
    stages = manifest.get("stages", {})
    keys = STAGES if through_report else STAGES[:-1]
    for stage in keys:
        info = stages.get(stage, {})
        if info.get("status") != "completed":
            return False
        if stage != "report" and not info.get("confirmed"):
            return False
    return True


def _report_ready(manifest: dict[str, Any]) -> bool:
    return _completed_chain(manifest, through_report=True)


def _metric_parts(stage: str, stats: dict[str, Any]) -> list[str]:
    """Use the adapter's durable, stage-specific counters; never infer them."""
    specific = {
        "import": (("原始记录", ("raw_record_count",)), ("去重岗位", ("deduplicated_job_count",)),
                   ("本任务", ("selected_job_count", "sampled_job_count"))),
        "shortlist": (("已处理", ("processed_count", "completed_count")), ("建议保留", ("recommended_count",))),
        "fetch-jd": (("JD 成功", ("jd_success_count", "success_count")),
                     ("部分可用", ("jd_partial_count", "partial_count")),
                     ("失败", ("jd_failed_count", "failed_count")),
                     ("已暂停", ("jd_paused_count", "paused_count")),
                     ("待处理", ("jd_pending_count", "pending_count"))),
        "llm": (("AI 成功", ("llm_success_count", "success_count")),
                ("未完成AI判断", ("llm_fallback_count", "fallback_count")),
                ("待人工核实", ("llm_verification_count",)),
                ("失败", ("llm_failed_count", "failed_count")),
                ("待处理", ("llm_pending_count", "pending_count"))),
    }
    result = []
    for label, fields in specific.get(stage, ()):
        value = next((stats[key] for key in fields if type(stats.get(key)) is int), None)
        if value is not None:
            if stage == "llm" and label == "AI 成功" and stats.get("review_format") != SIMPLE_REVIEW_FORMAT:
                label = "旧版成功（只读，需重跑）"
            result.append(f"{label} {value}")
    if stage == "shortlist" and isinstance(stats.get("list_queue_counts"), dict):
        counts = stats["list_queue_counts"]
        for key, label in LIST_QUEUE_LABELS.items():
            if type(counts.get(key)) is int:
                result.append(f"{'队列剔除' if key == 'excluded' else label} {counts[key]}")
    if result:
        return result
    return [f"{label} {stats[key]}" for key, label in (
        ("success_count", "成功"), ("completed_count", "已完成"),
        ("fallback_count", "规则回退"), ("failed_count", "失败"), ("pending_count", "待处理"),
    ) if type(stats.get(key)) is int]


def _confined_path(task: ReviewTask, value: Any, *, allow_missing: bool = False) -> Path | None:
    """Never locate an artifact in another task, including through a junction."""
    if not isinstance(value, (str, Path)) or not str(value).strip():
        return None
    base = task.path.absolute()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.absolute()
    if ".." in path.parts or not path.is_relative_to(base):
        return None
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if allow_missing and component == path:
                continue
            return None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return None
    path = path.resolve()
    if not path.is_relative_to(base.resolve()):
        return None
    return path


def _confined_file(task: ReviewTask, value: Any) -> Path | None:
    path = _confined_path(task, value)
    return path if path is not None and path.is_file() else None


def _artifact_paths(task: ReviewTask, manifest: dict[str, Any]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    log = _confined_file(task, "desktop.log") or _confined_file(task, "task.log")
    if log:
        result["log"] = log
    if not _report_ready(manifest):
        return result
    stats = manifest.get("stages", {}).get("report", {}).get("stats", {})
    artifacts = stats.get("artifacts", {}) if isinstance(stats.get("artifacts"), dict) else {}
    runtime = task.run_directory("report")
    fallbacks = {
        "json": runtime / "prepared" / "merged_jobs_index.json",
        "markdown": runtime / "prepared" / "海投分层清单.md",
        "pool": runtime / "prepared" / "hai_tou_pool.json",
    }
    for kind, fallback in fallbacks.items():
        path = _confined_file(task, stats.get(f"{kind}_path") or artifacts.get(f"{kind}_path") or fallback)
        if path:
            result[kind] = path
    return result


def _build_search(jobs: list[dict[str, Any]]) -> dict[str, str]:
    return {job_id(job): json.dumps(desktop_data._redact(job), ensure_ascii=False, default=str).casefold()
            for job in jobs}


class TaskViewCache:
    """One task's latest view only, owned by its serial worker, never by Tk.

    Published rows/search are read-only by convention: replace cache entries,
    never mutate an earlier view that Tk may still be displaying.
    """

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self.identity: tuple | None = None
        self.files: dict[tuple[str, str], tuple[tuple, list[dict]]] = {}
        self.versions: dict[tuple[str, str], tuple] = {}
        self.stages: dict[str, tuple[tuple, list[dict], dict[str, str], bool]] = {}
        self.trace: tuple[tuple, list[dict]] | None = None
        self.report: tuple[tuple, list[dict], dict[str, str]] | None = None
        self.live_ai: dict | None = None
        self.row_views: dict[str, tuple] = {}

    def prepare(self, task: ReviewTask, manifest: dict, *, include_report: bool = True) -> None:
        identity = (str(task.path.absolute()), manifest["task_id"], manifest["scope_signature"], include_report)
        if identity != self.identity:
            self.clear()
            self.identity = identity
        references = {(stage, info[field]) for stage, info in manifest["stages"].items() if include_report or stage in REVIEW_STAGES
                      for field in ("input_file", "result_file", "result_input_file") if info[field] is not None}
        if manifest["import_baseline_file"] is not None:
            references.add(("import", manifest["import_baseline_file"]))
        # Keep canonical path/junction validation even on cache hits. Only file
        # contents and their derived search strings are skipped, not validation.
        self.versions = {key: _file_version(task._artifact_path(*key)) for key in references}
        self.files = {key: entry for key, entry in self.files.items() if key in references}
        projection = task._ai_projection_cache
        if projection is not None:
            relative = manifest["stages"]["llm"]["result_file"]
            key = ("llm", relative)
            if self.versions.get(key) == projection[0]:
                self.files[key] = (projection[0], projection[1])

    def result_key(self, info: dict, stage: str) -> tuple:
        relative = info["result_file"]
        return (relative, tuple(info["result_ids"]), self.versions.get((stage, relative)))


class _CapturedTaskRows(ReviewTask):
    """Use canonical task ID/merge/audit rules against one validated snapshot.

    This private reader is never edited or passed to an executor. Overriding
    only the two reads avoids duplicating ReviewTask's selection/merge rules.
    """

    def __init__(self, task: ReviewTask, manifest: dict, cache: TaskViewCache, *, include_report: bool = True):
        self._capture_cache = cache
        # The ReviewTask constructor may inspect an older AI result while it
        # upgrades an in-memory snapshot, so initialize the read cache first.
        super().__init__(task.path, manifest, read_only=True, review_only=not include_report)

    def _upgrade_pending_selection(self, value: dict) -> None:
        # task.refresh already projected and validated this snapshot. Repeating
        # compatibility work here would read the same AI artifact a second time.
        pass

    def _load(self, *, review_only: bool = False) -> dict:
        return self._manifest

    def _read_jobs(self, stage: str, relative: str | None) -> list[dict]:
        if relative is None:
            return []
        key = (stage, relative)
        version = self._capture_cache.versions[key]
        entry = self._capture_cache.files.get(key)
        if entry is None or entry[0] != version:
            jobs = super()._read_jobs(stage, relative)
            if _file_version(self._artifact_path(stage, relative)) != version:
                raise TaskStateError("任务文件在读取时发生变化，请刷新后重试。")
            entry = (version, jobs)
            self._capture_cache.files[key] = entry
        return entry[1]


def _trace_key(manifest: dict, cache: TaskViewCache) -> tuple:
    stages = manifest["stages"]
    return (manifest["import_baseline_file"], tuple(manifest["baseline_ids"] or ()),
            cache.versions.get(("import", manifest["import_baseline_file"])), tuple(
                (stage, cache.result_key(info, stage), info["status"], info["confirmed"],
                 info["revision"], info["confirmed_revision"], info["input_revision"], info["result_input_revision"],
                 *(tuple(info[field]) for field in (
                     "input_ids", "result_input_ids", "keep_ids", "excluded_ids", "pending_ids",
                     "recommended_keep_ids", "recommended_exclude_ids")),
                 tuple(sorted(info["manual_choices"].items())))
                for stage, info in stages.items()))


def _live_ai_rows(task: ReviewTask, reader: _CapturedTaskRows, info: dict,
                  cache: TaskViewCache, path: Path, version: tuple | None) -> dict:
    """Replace only jobs whose durable model record changed."""
    input_key = (info.get("input_file"), tuple(info["input_ids"]),
                 cache.versions.get(("llm", info.get("input_file"))))
    previous = cache.live_ai
    if previous is not None and previous["input_key"] == input_key and previous["version"] == version:
        return previous
    inputs = reader._read_jobs("llm", info.get("input_file"))
    if [job_id(job) for job in inputs] != info["input_ids"]:
        raise TaskStateError("任务输入 ID 与检查点清单不一致。")
    checkpoint = _read_json(task, path, {}) if version is not None else {}
    if version is not None and _file_version(path) != version:
        raise TaskStateError("AI 检查点在读取时发生变化，请刷新后重试。")
    checkpoint_rows = checkpoint.get("reviews", []) if isinstance(checkpoint, dict) else []
    reviews = {row["job_id"]: row for row in checkpoint_rows
               if isinstance(row, dict) and isinstance(row.get("job_id"), str)}
    reusable = previous is not None and previous["input_key"] == input_key
    changed = []
    jobs = []
    for job in inputs:
        identity = job_id(job)
        if reusable and previous["reviews"].get(identity) == reviews.get(identity):
            projected = previous["by_id"][identity]
        else:
            projected = _analyze_ai_stage(job, reviews.get(identity))
            changed.append(projected)
        jobs.append(projected)
    if reusable and not changed:
        jobs, search = previous["jobs"], previous["search"]
    else:
        search = dict(previous["search"]) if reusable else {}
        search.update(_build_search(changed))
    related = [job_id(job) for job in jobs if job.get("llm_stage_decision") == "related"]
    unrelated = [job_id(job) for job in jobs if job.get("llm_stage_decision") == "unrelated"]
    pending = [job_id(job) for job in jobs if job.get("llm_stage_decision") not in {"related", "unrelated"}]
    cache.live_ai = {"input_key": input_key, "version": version, "reviews": reviews,
                     "jobs": jobs, "by_id": {job_id(job): job for job in jobs}, "search": search,
                     "selection": {"result_ids": list(info["input_ids"]),
                                   "recommended_keep_ids": related, "recommended_exclude_ids": unrelated,
                                   "pending_ids": pending},
                     "stats": {"review_format": SIMPLE_REVIEW_FORMAT,
                               "llm_success_count": sum(job.get("model_status") == "success" for job in jobs),
                               "llm_failed_count": sum(job.get("model_status") == "failed" for job in jobs),
                               "llm_related_count": len(related), "llm_recommended_count": len(related),
                               "llm_unrelated_count": len(unrelated), "llm_recommended_exclude_count": len(unrelated),
                               "llm_verification_count": len(pending), "llm_triage_total": len(jobs)}}
    return cache.live_ai


def _row_view(jobs: list[dict], info: dict, stage: str, cache: TaskViewCache) -> dict:
    """Prepare selection groups off Tk; the table renders only its current page."""
    token = (id(jobs), *(tuple(info[field]) for field in ("keep_ids", "excluded_ids", "pending_ids")))
    previous = cache.row_views.get(stage)
    if previous is not None and previous[0] == token:
        return previous[1]
    by_id = {job_id(job): job for job in jobs}
    if stage == "report":
        kept, excluded, pending = [], [], []
        for job in jobs:
            decision = report_decision(job)
            if decision.get("classification") == "暂不投":
                excluded.append(job)
            elif decision.get("classification") == "待核实":
                pending.append(job)
            elif report_pool_eligible(decision):
                kept.append(job)
    else:
        kept = [by_id[identity] for identity in info["keep_ids"] if identity in by_id]
        excluded = [by_id[identity] for identity in info["excluded_ids"] if identity in by_id]
        pending = [by_id[identity] for identity in info["pending_ids"] if identity in by_id]
    result = {"by_id": by_id, "groups": {"全部": jobs, "保留": kept, "剔除": excluded, "待核实": pending},
              "kept_ids": frozenset(info["keep_ids"]), "pending_ids": frozenset(info["pending_ids"])}
    cache.row_views[stage] = (token, result)
    return result


def _capture_task(task: ReviewTask, *, read_log: bool = False, cache: TaskViewCache | None = None,
                  include_report: bool = True) -> dict[str, Any]:
    """Worker-only durable view, reusing unchanged immutable rows and search."""
    cache = cache if cache is not None else TaskViewCache()
    manifest = task.refresh() if include_report else task.review_snapshot()["manifest"]
    cache.prepare(task, manifest, include_report=include_report)
    reader = _CapturedTaskRows(task, manifest, cache, include_report=include_report)
    rows: dict[str, list[dict[str, Any]]] = {}
    search: dict[str, dict[str, str]] = {}
    previews: set[str] = set()
    live_ai_checkpoint_version: tuple | None = None
    display_manifest = manifest
    for index, stage in enumerate(STAGES[:-1]):
        info = manifest["stages"][stage]
        preview = not info["result_ids"] and info["status"] == "pending"
        key = (cache.result_key(info, stage), preview)
        live_ai = stage == "llm" and info["status"] == "running"
        if live_ai:
            runtime = task._runtime_path("llm", info.get("runtime_directory", "runtime/llm"))
            checkpoint_path = _safe_path(task, runtime / "prepared" / "llm_reviews.json")
            try:
                checkpoint_path.lstat()
            except FileNotFoundError:
                live_ai_checkpoint_version = None
            else:
                live_ai_checkpoint_version = _file_version(checkpoint_path)
            live = _live_ai_rows(task, reader, info, cache, checkpoint_path, live_ai_checkpoint_version)
            rows[stage], search[stage] = live["jobs"], live["search"]
            live_info = {**info, **live["selection"], "manual_choices": dict(info["manual_choices"]),
                         "stats": {**info["stats"], **live["stats"]}}
            reader._select(live_info, "llm")
            display_manifest = {**manifest, "stages": {**manifest["stages"], "llm": live_info}}
            continue
        if stage == "llm":
            cache.live_ai = None
        if preview:
            ready = index > 0 and all(manifest["stages"][previous]["status"] == "completed"
                                      and manifest["stages"][previous]["confirmed"]
                                      for previous in STAGES[:index])
            previous = manifest["stages"][STAGES[index - 1]] if ready else None
            key += ((cache.result_key(previous, STAGES[index - 1]), tuple(previous["keep_ids"]))
                    if previous is not None else None,)
        entry = cache.stages.get(stage)
        if entry is None or entry[0] != key:
            jobs = reader.input_jobs(stage) if preview else reader.jobs(stage)
            entry = (key, jobs, _build_search(jobs), preview)
            cache.stages[stage] = entry
        rows[stage], search[stage] = entry[1], entry[2]
        if entry[3]:
            previews.add(stage)
    artifacts = {}
    log = _confined_file(task, "desktop.log") or _confined_file(task, "task.log")
    if log is not None:
        artifacts["log"] = log
    if include_report:
        _capture_report(reader, manifest, cache, rows, search, previews, artifacts)
    view = {"manifest": display_manifest, "rows": rows, "previews": previews, "search": search,
            "row_views": {stage: _row_view(rows[stage], display_manifest["stages"][stage], stage, cache)
                          for stage in rows},
            "artifacts": artifacts, "review_rules": reader.get_review_rules(),
            "default_review_rules": default_review_rules()}
    if display_manifest is not manifest:
        view["live_ai_checkpoint_version"] = live_ai_checkpoint_version
    if read_log:
        try:
            log_path = artifacts.get("log")
            if log_path is None:
                raise FileNotFoundError()
            with log_path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - MAX_LOG_CHARS * 4))
                content = stream.read().decode("utf-8", errors="replace")
            view["log"] = _safe(content)[-MAX_LOG_CHARS:]
        except FileNotFoundError:
            view["log"] = ""
    return view


def _capture_report(reader: ReviewTask, manifest: dict, cache: TaskViewCache, rows: dict, search: dict,
                    previews: set, artifacts: dict) -> None:
    """Explicit legacy report capture; the default review workbench never calls it."""
    trace_key = _trace_key(manifest, cache)
    if cache.trace is None or cache.trace[0] != trace_key:
        cache.trace = (trace_key, reader.all_jobs())
    if not manifest["stages"]["report"]["result_ids"] and manifest["stages"]["report"]["status"] == "pending":
        previews.add("report")
    artifacts.update(_artifact_paths(reader, manifest))
    report_path = artifacts.get("json") if _report_ready(manifest) else None
    report_key = (trace_key, _file_version(report_path) if report_path is not None else None)
    if cache.report is None or cache.report[0] != report_key:
        records = cache.trace[1]
        if report_path is not None:
            try:
                report = json.loads(report_path.read_text(encoding="utf-8-sig"))
                records = report.get("jobs") if isinstance(report, dict) else report
                if not isinstance(records, list) or not all(isinstance(job, dict) for job in records):
                    raise ValueError("report jobs must be an array")
                identities = [job_id(job) for job in records]
                if (len(set(identities)) != len(identities)
                        or set(identities) != {job_id(job) for job in cache.trace[1]}
                        or _file_version(report_path) != report_key[1]):
                    raise ValueError("report IDs or file version do not match")
            except (ValueError, OSError) as error:
                cache.report = None
                raise TaskStateError("报告 JSON 与当前任务全量岗位不一致或无法读取，未作为有效结果显示。") from error
        cache.report = (report_key, records, _build_search(records))
    rows["report"], search["report"] = cache.report[1], cache.report[2]


def _sources_text(job: dict[str, Any]) -> str:
    clean = desktop_data._redact(job)
    sources = clean.get("observed_in")
    lines = ["全部来源（原始数据只读，不因剔除而删除）", ""]
    if isinstance(sources, list) and sources:
        for index, source in enumerate(sources, 1):
            lines.extend((f"来源 {index}", _display(source), ""))
    else:
        lines.append("未提供 observed_in，以下为已有来源字段：")
    for key in ("sources", "source_files", "source_file", "source", "desktop_detail_source"):
        if clean.get(key):
            lines.extend((key, _display(clean[key]), ""))
    if len(lines) <= 4:
        lines.append("暂无来源信息；不能据此认为来源完整。")
    return "\n".join(lines)


def _jd_completeness_label(job: dict[str, Any]) -> str:
    state = detail_completeness({**job, "jd": desktop_data._jd_text(job)})
    return {"complete": "正文完整", "partial": "正文部分可用", "missing": "正文缺失"}[state]


def _evidence_text(job: dict[str, Any], *, stage: str | None = None) -> str:
    if stage == "llm":
        review = job.get("llm_review") if isinstance(job.get("llm_review"), dict) else {}
        current = job.get("review_format") == SIMPLE_REVIEW_FORMAT
        payload = None
        if (current and review.get("review_format") == SIMPLE_REVIEW_FORMAT
                and job.get("model_status") == "success" and review.get("status") == "success"
                and str(review.get("job_id", "")) == job_id(job)):
            try:
                payload = simple_review_payload(review)
            except JsonSchemaValidationError:
                pass
        lines = [
            f"岗位：{_display(job.get('title'))}",
            f"公司：{_display(job.get('company') or job.get('company_name') or job.get('brand_name'))}",
            f"岗位 ID：{_display(job_id(job))}",
            f"正文完整度：{_jd_completeness_label(job)}",
        ]
        if current:
            suggestion = ("待核实" if job.get("llm_stage_decision") == "pending_verification"
                          else _display(job.get("reason")))
            lines.extend((f"AI判断：{_display(payload.get('判断')) if payload else '未完成有效判断（待人工核实）'}",
                          f"AI理由：{_display(payload.get('理由')) if payload else '未产生模型理由'}",
                          f"AI默认建议（不含人工决定）：{suggestion}",
                          f"模型状态：{_display(job.get('model_status'))}"))
            if job.get("model_error") or review.get("error"):
                lines.append("技术问题（不是AI理由）：" + _display(job.get("model_error") or review.get("error")))
            metadata = {key: review[key] for key in (
                "review_format", "status", "review_model", "requested_model", "model", "reasoning_effort",
                "api_mode", "reviewed_at", "cache_signature", "cache_input_signature", "cache_version",
            ) if key in review}
            lines.extend(("运行元数据：", _display(metadata)))
        else:
            lines.extend(("旧版结果（只读历史，不代表新版AI成功；需重跑AI）。",
                          "识别到旧版六维规则时，仅重置 AI 阶段后再点击运行才会切换为简化默认；其他自定义规则保留，打开任务或单独重置均不会替换规则。",
                          "重置会清除本阶段结果；需查看的旧记录请在重置前查看。",
                          "旧模型记录（只读）：", _display(review)))
        if job.get("review_selection"):
            lines.extend(("人工选择审计：", _display(job["review_selection"])))
        lines.append("完整正文见“JD 原文”，检索来源见“全部来源”。")
        return "\n".join(lines)
    elif (job.get("review_format") == SIMPLE_REVIEW_FORMAT
          or isinstance(job.get("llm_review"), dict) and job["llm_review"].get("review_format") == SIMPLE_REVIEW_FORMAT):
        return desktop_data.format_job_detail(job)
    elif stage == "fetch-jd":
        lines = [
            "本步仅补正文与完整度，不进行职责关键词筛选或岗位分层。",
            f"岗位：{_display(job.get('title'))}",
            f"公司：{_display(job.get('company') or job.get('company_name') or job.get('brand_name'))}",
            f"岗位 ID：{_display(job_id(job))}",
            f"薪资：{_display(job.get('salary'))}",
            f"正文完整度：{_jd_completeness_label(job)}",
            f"正文长度：{len(desktop_data._jd_text(job))} 字符",
            f"抓取状态：{_display(job.get('jd_status'))}",
            f"抓取问题：{_display(job.get('jd_fetch_issue'))}",
            f"岗位链接：{_display(job.get('job_link') or job.get('link'))}",
            "完整正文见“JD 原文”，检索来源见“全部来源”。",
        ]
        record = job.get("jd_record")
        if isinstance(record, dict):
            metadata = {key: value for key, value in record.items() if key not in ("jd", "job_description", "description")}
            if metadata:
                lines.extend(("", "抓取记录（正文另见原文页）：", _display(metadata)))
    else:
        lines = []
        if stage == "shortlist":
            lines.extend((f"列表方向队列：{_list_queue_label(job)}",
                          f"队列原因：{_list_queue_reason(job)}",
                          "明确相关仅表示列表方向相关，仍须 JD 核实；队列不是人工保留状态，也不是最终重点 / 海投 / 暂不投分层。",
                          ""))
        lines.append(desktop_data.format_job_detail(job))
    for key, label in (
        ("excluded_at", "已确认剔除于"), ("review_selection", "人工选择审计"),
        ("salary_flags", "薪资风险"), ("salary_risks", "薪资风险"),
        ("salary_status", "薪资规则结论"),
        ("experience_flags", "经验风险"), ("experience_risks", "经验风险"),
        ("experience_gap", "经验差距 / 项目争取提示"),
        ("experience_labels", "已有经验标签"), ("jd_minimum_years", "JD 最低经验年限"),
        ("risk_flags", "风险提示"), ("hard_rule_reasons", "代码硬规则"),
        ("llm_review", "模型已有完整结论（只读）"),
    ):
        if job.get(key) is not None:
            lines.extend(("", label + "：", _display(job[key])))
    return "\n".join(lines)


class ReviewDesktopApp:
    """The default review UI; external work requires an explicit button click."""

    def __init__(self, root: tk.Tk, args: argparse.Namespace, *,
                 executor: Callable[..., dict] = execute_stage,
                 browser_preparer: Callable[..., bool] = prepare_browser,
                 collector_opener: Callable[[], None] | None = None):
        self.root, self.args = root, args
        install_tk_lifecycle(root)
        self.executor, self.browser_preparer = executor, browser_preparer
        self.collector_opener = collector_opener
        self.ui_thread_id = threading.get_ident()
        self.task: ReviewTask | None = None
        self.manifest: dict[str, Any] = {}
        self._rows: dict[str, list[dict[str, Any]]] = {stage: [] for stage in REVIEW_STAGES}
        self._shortlist_result_file: str | None = None
        self._search_index: dict[str, dict[str, str]] = {}
        self._view_cache = TaskViewCache()
        self._view_lock = threading.Lock()
        self._capture_sequence = 0
        self._applied_capture_sequence = 0
        self._row_views: dict[str, dict] = {}
        self._filtered_key: tuple | None = None
        self._rendered_rows_key: tuple | None = None
        self._table_values: dict[str, tuple] = {}
        self._detail_key: tuple | None = None
        self._view_filter: tuple | None = None
        self._prepared_filter: tuple | None = None
        self._filter_cache: tuple | None = None
        self._live_ai_checkpoint_version: tuple | None = None
        self._previews: set[str] = set()
        self._artifacts: dict[str, Path] = {}
        self._page = 0
        self._page_ids: list[str] = []
        self._jobs_by_id: dict[str, dict[str, Any]] = {}
        self._filtered_jobs: list[dict[str, Any]] = []
        self._focus_id: str | None = None
        self._selected_ids: tuple[str, ...] = ()
        self._rendering = False
        self._busy = False
        self._operation = ""
        self._operation_id = 0
        self._control: StageControl | None = None
        self.worker: threading.Thread | None = None
        self.events: queue.Queue[tuple[str, int, Any]] = queue.Queue()
        self._log_lock = threading.Lock()
        self._preference_lock = threading.Lock()
        self._preference_workers: list[threading.Thread] = []
        self._model_config_worker: threading.Thread | None = None
        self._model_config_request_busy = False
        self._saved_review_rules = ""
        self._default_review_rules = ""
        self._review_rules_dialog: tk.Toplevel | None = None
        self._review_rules_editor: tk.Text | None = None
        self._review_rules_status: StringVar | None = None
        self._review_rules_buttons: dict[str, ttk.Button] = {}
        self._review_rules_task: Path | None = None
        self._after_ids: set[str] = set()
        self._poll_after_id: str | None = None
        # Progress callbacks normally publish a live AI view.  Keep a
        # separate, low-frequency checkpoint poll as a fallback: a child
        # process can update its JSON checkpoint without producing a callback
        # (or the callback can be coalesced while the UI is busy).  The poll
        # always uses a private view cache and never mutates the Tk thread's
        # cache from a worker.
        self._live_refresh_after_id: str | None = None
        self._live_refresh_busy = False
        self._live_refresh_worker: threading.Thread | None = None
        self._filter_after_id: str | None = None
        self._closing = False
        self._closed = False
        self._notice_kind = "note"
        self._preferences = load_preferences()
        p = self._preferences
        self.theme = StringVar(root, normalize_theme(p.get("theme")))
        self.colors = palette_for(self.theme.get())
        self.theme_button_text = StringVar(root, "夜间模式")
        self.workspace = StringVar(root, "review")
        self.workspace_description = StringVar(root, "四步审核并确认最终保留；投递数据在 03 显式导出。")
        self.source_root = StringVar(root, str(getattr(args, "root", None) or p.get("data_root", "")))
        self.task_name = StringVar(root, "架构方向-上海")
        self.task_parent = StringVar(root, str(getattr(args, "task_parent", None) or default_state_root() / "job-result" / "review-tasks"))
        self.city = StringVar(root, p.get("city") if p.get("city") in CITY_CHOICES else "上海")
        self.track = StringVar(root, "当前＋成长")
        self.sample_mode = StringVar(root, SAMPLE_CHOICES[0])
        self.sample_size = StringVar(root, "")
        self.workers = StringVar(root, p.get("workers", str(AI_DEFAULT_WORKERS)))
        self.jd_workers = StringVar(root, p.get("jd_workers", str(JD_DEFAULT_WORKERS)))
        self.batch_size = StringVar(root, p.get("batch_size", "30"))
        self.scraper_root = StringVar(root, str(getattr(args, "scraper_root", None) or os.environ.get("BOSS_SCRAPER_ROOT", "") or p.get("scraper_root", "")))
        # Browser settings are deliberately plain strings.  The UI only passes
        # these values to the selected backend; it never opens a browser or
        # reads a profile/cookie directory itself.
        self.browser_mode = StringVar(
            root,
            BROWSER_MODE_LABELS.get(
                str(getattr(args, "browser_mode", None) or p.get("browser_mode", DEFAULT_JD_BROWSER_MODE)),
                BROWSER_MODE_LABELS[DEFAULT_JD_BROWSER_MODE],
            ),
        )
        self.chrome_port = StringVar(root, str(getattr(args, "chrome_port", None) or p.get("chrome_port", "9222")))
        self.edge_port = StringVar(root, str(getattr(args, "edge_port", None) or p.get("edge_port", "9223")))
        self.chrome_profile = StringVar(root, str(getattr(args, "chrome_profile", None) or p.get("chrome_profile", DEFAULT_CHROME_PROFILE)))
        self.edge_profile = StringVar(root, str(getattr(args, "edge_profile", None) or p.get("edge_profile", DEFAULT_EDGE_PROFILE)))
        self.chrome_status_text = StringVar(root, "未开始")
        self.edge_status_text = StringVar(root, "未开始")
        self._browser_statuses: dict[str, str] = {"chrome": "pending", "edge": "pending"}
        self.config_path = Path(getattr(args, "config", None) or DEFAULT_CONFIG).expanduser()
        self.stage = StringVar(root, "import")
        self.search = StringVar(root, "")
        self.filter = StringVar(root, "全部")
        self.list_queue_filter = StringVar(root, ALL_LIST_QUEUES)
        self.list_queue_hint = StringVar(root, "")
        self.notice = StringVar(root, "选择包含 raw/current、raw/target 的完整数据根并新建任务，或显式打开已有任务。启动和切换步骤不会执行任务。")
        self.status_text = StringVar(root, "尚未绑定任务")
        self.stage_title = StringVar(root)
        self.stage_description = StringVar(root)
        self.progress_text = StringVar(root, "未运行")
        self.count_text = StringVar(root, "")
        self.page_text = StringVar(root, "0 个岗位")
        self.empty_text = StringVar(root, "尚未导入；请先创建或打开一个任务。")
        self.confirm_text = StringVar(root, "确认 0 个进入下一步")
        self.binding_title = StringVar(root)
        self.binding_source = StringVar(root)
        self.binding_output = StringVar(root)
        self.binding_scope = StringVar(root)
        self.detail_title = StringVar(root, "选择一个岗位查看详情")
        self._text_widgets: list[tuple[tk.Text, bool]] = []
        self._comboboxes: list[ttk.Combobox] = []
        self._setting_controls: list[tuple[Any, str]] = []

        root.title("BOSS 岗位工作台 · 采集 / 后处理 / 投递导出")
        width = min(1380, max(1024, root.winfo_screenwidth() - 100))
        height = min(900, max(700, root.winfo_screenheight() - 110))
        root.geometry(f"{width}x{height}")
        root.minsize(860, 720)
        self._build_ui()
        self._set_theme(self.theme.get(), persist=False)
        self.search.trace_add("write", self._debounce_filters)
        self.filter.trace_add("write", self._debounce_filters)
        self.list_queue_filter.trace_add("write", self._debounce_filters)
        root.bind("<Control-f>", self._focus_search)
        root.bind("<F5>", self._refresh_workspace)
        root.protocol("WM_DELETE_WINDOW", self._close)
        self._render()
        self._schedule_poll()
        self._schedule_live_refresh_tick()

    @property
    def workflow_running(self) -> bool:
        return self._busy and self._operation in ("execute", "browser")

    @property
    def _collection_busy(self) -> bool:
        page = getattr(self, "collection_page", None)
        return page is not None and page.busy

    @property
    def _execution_blocked(self) -> bool:
        return self._busy or self._collection_busy or self._delivery_busy or self._closing

    @property
    def _delivery_busy(self) -> bool:
        page = getattr(self, "delivery_page", None)
        return page is not None and page.busy

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(4, weight=1)
        header = ttk.Frame(self.root, padding=(18, 6, 18, 3))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="BOSS 岗位工作台", style="Review.Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.workspace_description, style="Muted.TLabel").grid(row=1, column=0, sticky="w")
        actions = ttk.Frame(header)
        actions.grid(row=0, column=1, rowspan=2, sticky="e")
        self.collector_button = ttk.Button(actions, text="01  岗位采集", style="Small.TButton", command=self._open_collector)
        self.collector_button.pack(side="left", padx=(0, 8))
        self.review_button = ttk.Button(actions, text="02  后处理审核", style="Theme.TButton", command=lambda: self._show_workspace("review"))
        self.review_button.pack(side="left", padx=(0, 8))
        self.delivery_button = ttk.Button(actions, text="03  投递数据导出", style="Small.TButton", command=lambda: self._show_workspace("delivery"))
        self.delivery_button.pack(side="left", padx=(0, 8))
        self.theme_button = ttk.Button(actions, textvariable=self.theme_button_text, style="Theme.TButton", command=self._toggle_theme)
        self.theme_button.pack(side="left")

        self.setup_frame = ttk.Frame(self.root, padding=(18, 8, 18, 5))
        self.setup_frame.grid(row=1, column=0, sticky="ew")
        self.setup_frame.columnconfigure(1, weight=1)
        ttk.Label(self.setup_frame, text="完整数据根").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.source_entry = ttk.Entry(self.setup_frame, textvariable=self.source_root)
        self.source_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.choose_source_button = ttk.Button(self.setup_frame, text="选择文件夹", style="Small.TButton", command=self._choose_source)
        self.choose_source_button.grid(row=0, column=2)
        options = ttk.Frame(self.setup_frame)
        options.grid(row=1, column=0, columnspan=3, sticky="ew", pady=5)
        ttk.Label(options, text="任务名").pack(side="left")
        self.name_entry = ttk.Entry(options, textvariable=self.task_name, width=22)
        self.name_entry.pack(side="left", padx=(8, 16))
        ttk.Label(options, text="城市").pack(side="left")
        self.city_box = ttk.Combobox(options, textvariable=self.city, values=CITY_CHOICES, state="readonly", width=8)
        self.city_box.pack(side="left", padx=(5, 12))
        self.track_box = ttk.Combobox(options, textvariable=self.track, values=tuple(TRACK_CHOICES), state="readonly", width=11)
        self.track_box.pack(side="left", padx=(0, 12))
        quantity = ttk.Frame(self.setup_frame)
        quantity.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        ttk.Label(quantity, text="处理数量").pack(side="left")
        self.sample_mode_box = ttk.Combobox(quantity, textvariable=self.sample_mode, values=SAMPLE_CHOICES,
                                          state="readonly", width=17)
        self.sample_mode_box.pack(side="left", padx=(8, 10))
        self.sample_mode_box.bind("<<ComboboxSelected>>", lambda _event: self._sample_mode_changed(focus=True))
        self.sample_entry = ttk.Entry(quantity, textvariable=self.sample_size, width=9)
        self.sample_entry.pack(side="left", padx=5)
        ttk.Label(quantity, text="个去重岗位；自定义请输入正整数，创建后范围固定", style="Muted.TLabel").pack(side="left", padx=5)
        ttk.Label(self.setup_frame, text="任务保存位置").grid(row=3, column=0, sticky="w", padx=(0, 10))
        self.parent_entry = ttk.Entry(self.setup_frame, textvariable=self.task_parent)
        self.parent_entry.grid(row=3, column=1, sticky="ew", padx=(0, 8))
        self.choose_parent_button = ttk.Button(self.setup_frame, text="选择位置", style="Small.TButton", command=self._choose_parent)
        self.choose_parent_button.grid(row=3, column=2)
        setup_actions = ttk.Frame(self.setup_frame)
        setup_actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.create_button = ttk.Button(setup_actions, text="创建具名任务", style="Primary.TButton", command=self._create_task)
        self.create_button.pack(side="left", padx=(0, 8))
        self.open_button = ttk.Button(setup_actions, text="打开已有任务…", command=self._choose_task)
        self.open_button.pack(side="left")
        ttk.Label(setup_actions, text="只读原始文件 · 不按日期另找数据 · 不自动开始", style="Muted.TLabel").pack(side="left", padx=15)
        self._setup_controls = [self.source_entry, self.choose_source_button, self.name_entry,
                                self.sample_mode_box, self.sample_entry, self.parent_entry, self.choose_parent_button,
                                self.city_box, self.track_box, self.create_button, self.open_button]
        self._comboboxes.extend((self.city_box, self.track_box, self.sample_mode_box))

        self.binding_frame = ttk.Frame(self.root, padding=(18, 4, 18, 2))
        self.binding_frame.columnconfigure(0, weight=1)
        ttk.Label(self.binding_frame, textvariable=self.binding_title, style="Review.Binding.TLabel").grid(row=0, column=0, sticky="w")
        bound_actions = ttk.Frame(self.binding_frame)
        bound_actions.grid(row=0, column=1, rowspan=2, sticky="ne")
        self.new_task_button = ttk.Button(bound_actions, text="新建 / 改数量", style="Small.TButton", command=self._new_task)
        self.new_task_button.pack(side="left", padx=4)
        self.open_bound_button = ttk.Button(bound_actions, text="打开任务…", style="Small.TButton", command=self._choose_task)
        self.open_bound_button.pack(side="left", padx=4)
        self.task_folder_button = ttk.Button(bound_actions, text="任务目录", style="Small.TButton", command=lambda: self._open_location("task"))
        self.task_folder_button.pack(side="left", padx=4)
        ttk.Label(self.binding_frame, textvariable=self.binding_scope, style="Review.Context.TLabel").grid(row=1, column=0, sticky="w")
        self.binding_source_label = ttk.Label(self.binding_frame, textvariable=self.binding_source, style="Review.Context.TLabel", wraplength=940)
        self.binding_output_label = ttk.Label(self.binding_frame, textvariable=self.binding_output, style="Review.Context.TLabel", wraplength=940)
        self.binding_details_button = ttk.Button(bound_actions, text="输入 / 输出绑定", style="Small.TButton", command=self._toggle_bindings)
        self.binding_details_button.pack(side="left", padx=4)

        nav = self.stage_navigation = ttk.Frame(self.root, padding=(18, 4, 18, 5))
        nav.grid(row=2, column=0, sticky="ew")
        self.stage_buttons: dict[str, ttk.Button] = {}
        for index, stage in enumerate(REVIEW_STAGES):
            nav.columnconfigure(index, weight=1, uniform="stages")
            button = ttk.Button(nav, style="Review.Step.TButton", command=lambda s=stage: self._select_stage(s))
            button.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 3, 0 if index == len(REVIEW_STAGES) - 1 else 3))
            self.stage_buttons[stage] = button
        self.notice_label = tk.Label(self.root, textvariable=self.notice, anchor="w", justify="left",
                                     padx=12, pady=5, wraplength=940, font=("Microsoft YaHei UI", 9))
        self.notice_label.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 5))
        self.root.bind("<Configure>", self._resize_notice, add="+")

        self.main_pane = ttk.Panedwindow(self.root, orient="vertical")
        self.main_pane.grid(row=4, column=0, sticky="nsew", padx=18, pady=(0, 12))
        self.main_pane.bind("<Configure>", self._keep_log_reachable, add="+")
        work = ttk.Frame(self.main_pane, style="Card.TFrame", padding=(10, 8))
        self.main_pane.add(work, weight=5)
        work.columnconfigure(0, weight=1)
        work.rowconfigure(4, weight=1)
        work_heading = ttk.Frame(work, style="Card.TFrame")
        work_heading.grid(row=0, column=0, sticky="ew")
        work_heading.columnconfigure(0, weight=1)
        ttk.Label(work_heading, textvariable=self.stage_title, style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.status_badge = tk.Label(work_heading, textvariable=self.status_text, padx=8, pady=3, font=("Microsoft YaHei UI", 9))
        self.status_badge.grid(row=0, column=1, sticky="e")
        description = ttk.Frame(work, style="Card.TFrame")
        description.grid(row=1, column=0, sticky="ew", pady=(4, 5))
        description.columnconfigure(0, weight=1)
        self.description_label = ttk.Label(description, textvariable=self.stage_description, style="Hint.TLabel", wraplength=820)
        self.description_label.grid(row=0, column=0, sticky="ew")
        self.reset_button = ttk.Button(description, text="重置本阶段", style="Small.TButton", command=self._reset_stage)
        self.reset_button.grid(row=0, column=1, sticky="ne", padx=(8, 0))
        controls = ttk.Frame(work, style="Card.TFrame")
        controls.grid(row=2, column=0, sticky="ew", pady=(0, 5))
        self.run_button = ttk.Button(controls, text="运行本步", style="Review.Primary.TButton", command=self._run_stage)
        self.run_button.pack(side="left", padx=(0, 6))
        self.pause_button = ttk.Button(controls, text="暂停", style="Small.TButton", command=self._pause_stage)
        self.pause_button.pack(side="left", padx=3)
        self.stop_button = ttk.Button(controls, text="停止", style="Small.TButton", command=self._stop_stage)
        self.stop_button.pack(side="left", padx=3)
        self.continue_button = ttk.Button(controls, text="人工处理完成，继续", style="Small.TButton", command=self._continue_stage)
        self.continue_button.pack(side="left", padx=3)
        self.settings_button = ttk.Button(controls, text="执行设置", style="Small.TButton", command=self._toggle_settings)
        self.settings_button.pack(side="right")
        self.refresh_button = ttk.Button(controls, text="刷新", style="Small.TButton", command=self._refresh_task)
        self.refresh_button.pack(side="right", padx=5)
        self.edit_rules_button = ttk.Button(controls, text="修改复核规则", style="Small.TButton",
                                            command=self._edit_review_rules)

        progress_area = ttk.Frame(work, style="Card.TFrame")
        progress_area.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        progress_area.columnconfigure(0, weight=1)
        self.jd_settings_frame = ttk.Frame(progress_area, style="Card.TFrame")
        jd_options_line = ttk.Frame(self.jd_settings_frame, style="Card.TFrame")
        jd_options_line.pack(fill="x")
        ttk.Label(jd_options_line, text="每个浏览器页面并发", style="Hint.TLabel").pack(side="left", padx=(0, 6))
        self.jd_workers_spin = ttk.Spinbox(jd_options_line, textvariable=self.jd_workers,
                                          from_=1, to=MAX_WORKERS, width=4, command=self._remember_execution_settings)
        self.jd_workers_spin.pack(side="left")
        ttk.Label(jd_options_line, text="浏览器模式", style="Hint.TLabel").pack(side="left", padx=(12, 6))
        self.browser_mode_box = ttk.Combobox(
            jd_options_line, textvariable=self.browser_mode, values=BROWSER_MODE_CHOICES,
            state="readonly", width=18, style="Review.TCombobox",
        )
        self.browser_mode_box.pack(side="left")
        self.browser_mode_box.bind("<<ComboboxSelected>>", self._browser_mode_selected)
        self.prepare_browser_button = ttk.Button(jd_options_line, text="准备 Chrome + Edge / 登录", style="Small.TButton", command=self._prepare_browser)
        self.prepare_browser_button.pack(side="left", padx=(12, 0))
        jd_browser_line = ttk.Frame(self.jd_settings_frame, style="Card.TFrame")
        jd_browser_line.pack(fill="x", pady=(1, 0))
        self.jd_mode_label = ttk.Label(jd_browser_line,
                                      text=f"每页滚动{JD_SCROLL_COUNT}次 / 本步仅补正文不筛选", style="Hint.TLabel")
        self.jd_mode_label.pack(side="left", padx=(0, 12))
        self.chrome_status_label = ttk.Label(jd_browser_line, textvariable=self.chrome_status_text, style="Hint.TLabel")
        self.chrome_status_label.pack(side="left", padx=(0, 20))
        self.edge_status_label = ttk.Label(jd_browser_line, textvariable=self.edge_status_text, style="Hint.TLabel")
        self.edge_status_label.pack(side="left")
        self.llm_settings_frame = ttk.Frame(progress_area, style="Card.TFrame")
        ttk.Label(self.llm_settings_frame, text="AI 并发（每岗独立）", style="Hint.TLabel").pack(side="left", padx=(0, 6))
        self.workers_spin = ttk.Spinbox(self.llm_settings_frame, textvariable=self.workers,
                                       from_=1, to=MAX_WORKERS, width=4, command=self._remember_execution_settings)
        self.workers_spin.pack(side="left")
        self.model_config_label = ttk.Label(self.llm_settings_frame,
                                           text="", style="Hint.TLabel", wraplength=620, justify="left")
        self.model_config_label.pack(side="left", padx=(12, 0), anchor="w", fill="x", expand=True)
        self.model_config_button = ttk.Button(
            self.llm_settings_frame, text="修改模型配置", style="Small.TButton", command=self._edit_model_configuration,
        )
        self.model_config_button.pack(side="right", padx=(8, 0))
        self._refresh_model_config_label()
        self.progress_label = ttk.Label(progress_area, textvariable=self.progress_text, style="Hint.TLabel", wraplength=940)
        self.progress_label.grid(row=1, column=0, sticky="ew")
        self.progress_bar = ttk.Progressbar(progress_area, maximum=1, value=0)
        self.progress_bar.grid(row=2, column=0, sticky="ew", pady=(3, 0))
        self.settings_frame = ttk.Frame(progress_area, style="Card.TFrame")
        self.settings_frame.columnconfigure(1, weight=1)
        settings_line = ttk.Frame(self.settings_frame, style="Card.TFrame")
        settings_line.grid(row=0, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Label(settings_line, text="JD 每批岗位数", style="Hint.TLabel").pack(side="left", padx=(0, 6))
        self.batch_spin = ttk.Spinbox(settings_line, textvariable=self.batch_size, from_=1, to=100, width=4,
                                     command=self._remember_execution_settings)
        self.batch_spin.pack(side="left")
        ttk.Label(settings_line, text="仅用于检查点分组，与页面 / AI 请求并发独立", style="Hint.TLabel").pack(side="left", padx=(12, 0))
        ttk.Label(self.settings_frame, text="上游采集工具", style="Hint.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.scraper_entry = ttk.Entry(self.settings_frame, textvariable=self.scraper_root)
        self.scraper_entry.grid(row=1, column=1, sticky="ew")
        scraper_actions = ttk.Frame(self.settings_frame, style="Card.TFrame")
        scraper_actions.grid(row=1, column=2)
        self.scraper_choose_button = ttk.Button(scraper_actions, text="选择", style="Small.TButton", command=self._choose_scraper)
        self.scraper_choose_button.pack(side="left", padx=4)
        browser_line = ttk.Frame(self.settings_frame, style="Card.TFrame")
        browser_line.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        ttk.Label(browser_line, text="Chrome CDP 端口", style="Hint.TLabel").pack(side="left", padx=(0, 4))
        self.chrome_port_entry = ttk.Entry(browser_line, textvariable=self.chrome_port, width=7)
        self.chrome_port_entry.pack(side="left", padx=(0, 10))
        ttk.Label(browser_line, text="Edge CDP 端口", style="Hint.TLabel").pack(side="left", padx=(0, 4))
        self.edge_port_entry = ttk.Entry(browser_line, textvariable=self.edge_port, width=7)
        self.edge_port_entry.pack(side="left", padx=(0, 10))
        self.browser_status_hint = ttk.Label(browser_line, text="端口仅用于连接已准备的 CDP；不会读取 Cookie", style="Hint.TLabel")
        self.browser_status_hint.pack(side="left", padx=(4, 0))

        profile_line = ttk.Frame(self.settings_frame, style="Card.TFrame")
        profile_line.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Label(profile_line, text="Chrome profile", style="Hint.TLabel").pack(side="left", padx=(0, 6))
        self.chrome_profile_entry = ttk.Entry(profile_line, textvariable=self.chrome_profile, width=29)
        self.chrome_profile_entry.pack(side="left", fill="x", expand=True, padx=(0, 12))
        ttk.Label(profile_line, text="Edge profile", style="Hint.TLabel").pack(side="left", padx=(0, 6))
        self.edge_profile_entry = ttk.Entry(profile_line, textvariable=self.edge_profile, width=29)
        self.edge_profile_entry.pack(side="left", fill="x", expand=True)

        self._browser_controls = [
            (self.browser_mode_box, "readonly"), (self.chrome_port_entry, "normal"),
            (self.edge_port_entry, "normal"), (self.chrome_profile_entry, "normal"),
            (self.edge_profile_entry, "normal"),
        ]
        self._comboboxes.append(self.browser_mode_box)
        self._setting_controls.extend((widget, "normal") for widget in (
            self.workers_spin, self.model_config_button, self.jd_workers_spin, self.batch_spin,
            self.scraper_entry, self.scraper_choose_button,
        ))
        for widget in (self.workers_spin, self.jd_workers_spin, self.batch_spin):
            widget.bind("<FocusOut>", self._remember_execution_settings, add="+")
            widget.bind("<Return>", self._remember_execution_settings, add="+")
        for widget in (self.chrome_port_entry, self.edge_port_entry, self.chrome_profile_entry, self.edge_profile_entry):
            widget.bind("<FocusOut>", self._remember_browser_settings, add="+")
            widget.bind("<Return>", self._remember_browser_settings, add="+")
        self._browser_mode_changed()

        self.review_pane = ttk.Panedwindow(work, orient="horizontal")
        self.review_pane.grid(row=4, column=0, sticky="nsew")
        listing = ttk.Frame(self.review_pane, style="Card.TFrame")
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(3, weight=1)
        self.review_pane.add(listing, weight=3)
        self.list_queue_frame = ttk.Frame(listing, style="Card.TFrame")
        self.list_queue_frame.columnconfigure(0, weight=1)
        queue_tools = ttk.Frame(self.list_queue_frame, style="Card.TFrame")
        queue_tools.grid(row=0, column=0, sticky="ew")
        ttk.Label(queue_tools, text="方向队列", style="Hint.TLabel").pack(side="left", padx=(0, 5))
        self.list_queue_box = ttk.Combobox(queue_tools, textvariable=self.list_queue_filter,
                                          values=(ALL_LIST_QUEUES, *LIST_QUEUE_LABELS.values()),
                                          width=10, state="readonly", style="Review.TCombobox")
        self.list_queue_box.pack(side="left", padx=(0, 6))
        self.relevant_only_button = ttk.Button(queue_tools, text="仅保留明确相关", style="Small.TButton",
                                               command=self._keep_relevant_only)
        self.relevant_only_button.pack(side="left", padx=(0, 4))
        self.add_verify_button = ttk.Button(queue_tools, text="加入待核实", style="Small.TButton",
                                            command=self._add_verify_queue)
        self.add_verify_button.pack(side="left")
        self.list_queue_hint_label = ttk.Label(self.list_queue_frame, textvariable=self.list_queue_hint,
                                               style="Hint.TLabel", wraplength=530)
        self.list_queue_hint_label.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.list_queue_frame.bind("<Configure>", lambda event: self.list_queue_hint_label.configure(wraplength=max(240, event.width - 4)))
        self._comboboxes.append(self.list_queue_box)
        tools = ttk.Frame(listing, style="Card.TFrame")
        tools.grid(row=1, column=0, sticky="ew", pady=(0, 3))
        ttk.Label(tools, text="人工名单", style="Hint.TLabel").pack(side="left", padx=(0, 4))
        self.filter_buttons: dict[str, ttk.Button] = {}
        for label in ("全部", "保留", "剔除", "待核实"):
            button = ttk.Button(tools, text=label, style="Small.TButton", command=lambda value=label: self.filter.set(value))
            button.pack(side="left", padx=(0, 3))
            self.filter_buttons[label] = button
        self.search_entry = ttk.Entry(tools, textvariable=self.search, width=17, style="Review.TEntry")
        self.search_entry.pack(side="right", fill="x", expand=True, padx=(8, 0))
        ttk.Label(tools, text="搜索", style="Hint.TLabel").pack(side="right", padx=(4, 0))
        bulk = ttk.Frame(listing, style="Card.TFrame")
        bulk.grid(row=2, column=0, sticky="ew", pady=(0, 3))
        self.select_page_button = ttk.Button(bulk, text="全选本页", style="Small.TButton", command=self._select_page)
        self.select_page_button.pack(side="left", padx=(0, 3))
        self.exclude_button = ttk.Button(bulk, text="剔除所选", style="Small.TButton", command=lambda: self._set_selected_keep(False))
        self.exclude_button.pack(side="left", padx=3)
        self.restore_button = ttk.Button(bulk, text="恢复所选", style="Small.TButton", command=lambda: self._set_selected_keep(True))
        self.restore_button.pack(side="left", padx=3)
        self.recommend_button = ttk.Button(bulk, text="采用建议", style="Small.TButton", command=self._apply_recommendations)
        self.recommend_button.pack(side="left", padx=3)

        table_frame = ttk.Frame(listing, style="Card.TFrame")
        table_frame.grid(row=3, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.results_table = ttk.Treeview(table_frame, columns=("keep", "job", "salary", "reason"), show="headings", selectmode="extended", height=5, style="Review.Treeview")
        for key, label, width, minimum, stretch in (
            ("keep", "保留", 46, 44, False), ("job", "岗位 · 公司", 245, 150, True),
            ("salary", "薪资", 85, 75, False), ("reason", "本步依据", 165, 115, True),
        ):
            self.results_table.heading(key, text=label)
            self.results_table.column(key, width=width, minwidth=minimum, stretch=stretch, anchor="center" if key == "keep" else "w")
        self.results_table.grid(row=0, column=0, sticky="nsew")
        scroll_y = ttk.Scrollbar(table_frame, orient="vertical", command=self.results_table.yview)
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x = ttk.Scrollbar(table_frame, orient="horizontal", command=self.results_table.xview)
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.results_table.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.results_table.bind("<<TreeviewSelect>>", self._show_selected_job)
        self.results_table.bind("<Button-1>", self._table_click, add="+")
        self.results_table.bind("<Double-1>", self._table_double_click)
        self.results_table.bind("<space>", self._table_space)
        self.empty_label = ttk.Label(table_frame, textvariable=self.empty_text, style="Hint.TLabel", anchor="center", wraplength=430)
        self.empty_label.place(relx=.5, rely=.5, anchor="center")
        pagination = ttk.Frame(listing, style="Card.TFrame")
        pagination.grid(row=4, column=0, sticky="ew", pady=(5, 0))
        self.previous_page_button = ttk.Button(pagination, text="上一页", style="Small.TButton", command=lambda: self._change_page(-1))
        self.previous_page_button.pack(side="left")
        ttk.Label(pagination, textvariable=self.page_text, style="Hint.TLabel").pack(side="left", padx=7)
        self.next_page_button = ttk.Button(pagination, text="下一页", style="Small.TButton", command=lambda: self._change_page(1))
        self.next_page_button.pack(side="left")

        detail = ttk.Frame(self.review_pane, style="Card.TFrame", padding=(10, 0, 0, 0))
        self.review_pane.add(detail, weight=2)
        detail.columnconfigure(0, weight=1)
        detail.rowconfigure(1, weight=1)
        ttk.Label(detail, textvariable=self.detail_title, style="Section.TLabel", wraplength=340).grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.detail_notebook = ttk.Notebook(detail, style="Review.Detail.TNotebook")
        self.detail_notebook.grid(row=1, column=0, sticky="nsew")
        self.detail_text = self._text_tab(self.detail_notebook, "依据")
        self.jd_text = self._text_tab(self.detail_notebook, "JD 原文")
        self.sources_text = self._text_tab(self.detail_notebook, "全部来源")
        self.detail_toggle_button = ttk.Button(detail, text="剔除 / 恢复当前岗位", style="Small.TButton", command=self._toggle_focused_keep)
        self.detail_toggle_button.grid(row=2, column=0, sticky="w", pady=(5, 0))

        footer = ttk.Frame(work, style="Card.TFrame")
        footer.grid(row=5, column=0, sticky="ew", pady=(7, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.count_text, style="Hint.TLabel").grid(row=0, column=0, sticky="w")
        self.open_report_button = ttk.Button(footer, text="打开报告", style="Review.Primary.TButton",
                                             command=lambda: self._open_location("markdown"))
        self.go_delivery_button = ttk.Button(footer, text="前往 03 投递数据导出", style="Review.Primary.TButton",
                                             command=lambda: self._show_workspace("delivery"))
        self.go_delivery_button.grid(row=0, column=1, sticky="e", padx=(10, 6))
        self.confirm_button = ttk.Button(footer, textvariable=self.confirm_text, style="Review.Primary.TButton", command=self._confirm_stage)
        self.confirm_button.grid(row=0, column=2, sticky="e")

        log_panel = ttk.Frame(self.main_pane, style="Card.TFrame", padding=(10, 5))
        self.main_pane.add(log_panel, weight=1)
        log_panel.columnconfigure(0, weight=1)
        log_panel.rowconfigure(1, weight=1)
        log_tools = ttk.Frame(log_panel, style="Card.TFrame")
        log_tools.grid(row=0, column=0, sticky="ew", pady=(0, 3))
        ttk.Label(log_tools, text="真实运行日志", style="Section.TLabel").pack(side="left")
        self.log_button = ttk.Button(log_tools, text="日志文件", style="Small.TButton", command=lambda: self._open_location("log"))
        self.log_button.pack(side="right", padx=(4, 0))
        self.markdown_button = ttk.Button(log_tools, text="报告 Markdown", style="Small.TButton", command=lambda: self._open_location("markdown"))
        self.json_button = ttk.Button(log_tools, text="报告 JSON", style="Small.TButton", command=lambda: self._open_location("json"))
        log_frame = ttk.Frame(log_panel)
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_view = tk.Text(log_frame, wrap="word", height=3, state="disabled", relief="flat", borderwidth=0,
                                padx=8, pady=5, font=("Consolas", 9), exportselection=False)
        self.log_view.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_view.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_view.configure(yscrollcommand=log_scroll.set)
        self._text_widgets.append((self.log_view, True))

        self.collection_page = CollectionPage(
            self.root, config_path=self.config_path,
            initial_root=self.source_root.get(), initial_scraper_root=self.scraper_root.get(),
            on_use_source=self._use_collection_source,
            can_start=self._can_start_collection,
            on_busy_change=self._collection_state_changed,
        )
        self.delivery_page = DeliveryPage(self.root, can_start=self._can_start_delivery,
                                          on_busy_change=self._delivery_state_changed, open_path=self._open_path)

    def _show_workspace(self, workspace: str) -> None:
        if self._closing or workspace not in ("collection", "review", "delivery"):
            return
        self.workspace.set(workspace)
        self._render(render_rows=False)
        if workspace == "delivery" and self.delivery_page._preview is None and self.delivery_page._source_path is not None:
            self.delivery_page.refresh_preview()

    def _apply_workspace_visibility(self) -> None:
        workspace = self.workspace.get()
        collecting, delivering = workspace == "collection", workspace == "delivery"
        if collecting or delivering:
            for widget in (self.setup_frame, self.binding_frame, self.stage_navigation,
                           self.notice_label, self.main_pane):
                widget.grid_remove()
            if collecting:
                self.delivery_page.grid_remove()
                self.collection_page.grid(row=1, column=0, rowspan=4, sticky="nsew", padx=18, pady=(4, 12))
                self.workspace_description.set("其他页面正在处理，采集暂不可启动；可切回查看进度。" if self._busy or self._delivery_busy else
                                               "只采集原始列表；完成后由你决定是否新建后处理任务。")
            else:
                self.collection_page.grid_remove()
                self.delivery_page.grid(row=1, column=0, rowspan=4, sticky="nsew", padx=18, pady=(4, 12))
                self.workspace_description.set("导出最终确认保留的岗位；预览、切换与打开任务均不自动发布文件。")
        else:
            self.collection_page.grid_remove()
            self.delivery_page.grid_remove()
            for widget in (self.stage_navigation, self.notice_label, self.main_pane):
                widget.grid()
            self.workspace_description.set("其他页面正在处理，后处理暂不可启动；可切回查看进度。" if self._collection_busy or self._delivery_busy else
                                           "四步审核并确认最终保留；投递数据在 03 显式导出。")
        self.collector_button.configure(style="Theme.TButton" if collecting else "Small.TButton")
        self.review_button.configure(style="Theme.TButton" if workspace == "review" else "Small.TButton")
        self.delivery_button.configure(style="Theme.TButton" if delivering else "Small.TButton")
        self.delivery_page.render_controls()

    def _collection_state_changed(self, _busy: bool) -> None:
        if not self._closed and hasattr(self, "collection_page") and hasattr(self, "results_table"):
            self._render(render_rows=False)

    def _can_start_collection(self) -> bool:
        return not self._busy and not self._delivery_busy and not self._closing

    def _can_start_delivery(self) -> bool:
        return not self._busy and not self._collection_busy and not self._closing

    def _delivery_state_changed(self, _busy: bool) -> None:
        if not self._closed:
            self._render(render_rows=False)

    def _use_collection_source(self, path: Path) -> None:
        if self._execution_blocked:
            return
        if self.task is not None:
            self._new_task()
            if self.task is not None:  # The user declined to leave the bound task.
                return
        self.source_root.set(str(Path(path).expanduser()))
        self.config_path = Path(self.collection_page.config_path)
        self._show_workspace("review")
        self._notify("已将采集目录填入新任务表单；尚未创建任务、导入或筛选。已有任务和原始数据均保留。")

    def _text_tab(self, notebook: ttk.Notebook, label: str) -> tk.Text:
        frame = ttk.Frame(notebook)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = tk.Text(frame, wrap="word", state="disabled", height=5, width=27, relief="flat", borderwidth=0,
                       padx=8, pady=7, font=("Microsoft YaHei UI", 9), exportselection=False)
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        notebook.add(frame, text=label)
        self._text_widgets.append((text, False))
        return text

    def _resize_notice(self, event: tk.Event) -> None:
        if event.widget is self.root and event.width > 100:
            width = max(280, event.width - 80)
            self.notice_label.configure(wraplength=width)
            self.description_label.configure(wraplength=max(260, width - 100))
            self.progress_label.configure(wraplength=width)
            self.binding_source_label.configure(wraplength=width)
            self.binding_output_label.configure(wraplength=width)

    def _keep_log_reachable(self, event: tk.Event) -> None:
        if len(self.main_pane.panes()) > 1 and event.height > 250:
            # ttk panes have no minsize option.  Reserve a couple of real log
            # lines at first layout / resize instead of shrinking them away.
            maximum = event.height - 92
            if self.main_pane.sashpos(0) > maximum:
                self.main_pane.sashpos(0, maximum)

    def _toggle_bindings(self) -> None:
        if self.binding_source_label.winfo_manager():
            self.binding_source_label.grid_remove()
            self.binding_output_label.grid_remove()
        else:
            self.binding_source_label.grid(row=2, column=0, columnspan=2, sticky="ew")
            self.binding_output_label.grid(row=3, column=0, columnspan=2, sticky="ew")

    def _persist_preferences(self, **updates: str) -> None:
        self._preferences = dict(self._preferences, **updates)

        def save() -> None:
            try:
                with self._preference_lock:
                    # Always save the latest main-thread snapshot, even if an
                    # earlier theme/settings worker obtained the lock last.
                    save_preferences(dict(self._preferences))
            except OSError as error:
                self.events.put(("preference_error", -1, _safe(error)))

        worker = threading.Thread(target=save, name="review-preferences-save", daemon=True)
        self._preference_workers = [thread for thread in self._preference_workers if thread.is_alive()]
        self._preference_workers.append(worker)
        worker.start()

    def _remember_execution_settings(self, _event: tk.Event | None = None) -> None:
        if self._busy or self._closing:
            return
        try:
            values = {
                "workers": str(_positive(self.workers.get(), "AI 请求并发", maximum=MAX_WORKERS)),
                "jd_workers": str(_positive(self.jd_workers.get(), "JD 页面并发", maximum=MAX_WORKERS)),
                "batch_size": str(_positive(self.batch_size.get(), "JD 每批岗位数", maximum=100)),
            }
        except ValueError:
            # Editable spinboxes can temporarily contain incomplete input.
            # Running a stage performs the same validation with a clear error.
            return
        if any(self._preferences.get(key) != value for key, value in values.items()):
            self._persist_preferences(**values)

    def _refresh_model_config_label(self) -> None:
        """Show the effective project model settings without reading its secret."""
        try:
            settings = project_llm_settings.load_settings() or {}
        except project_llm_settings.ProjectProviderError:
            self.model_config_label.configure(text="本机模型配置无效 · 点击“修改模型配置”修复")
            return
        base_url = settings.get("base_url", project_llm_settings.DEFAULT_BASE_URL)
        model = settings.get("model", AI_DEFAULT_MODEL)
        effort = settings.get("reasoning_effort", AI_DEFAULT_REASONING_EFFORT)
        api_mode = settings.get("api_mode", project_llm_settings.API_MODE_RESPONSES)
        api_mode_label = API_MODE_LABELS.get(api_mode, API_MODE_LABELS[project_llm_settings.API_MODE_RESPONSES])
        verified = project_llm_settings.is_chat_reasoning_verified(
            base_url, model, effort, settings.get("chat_reasoning_verified_for", ""),
            chat_stream=settings.get("chat_stream", False),
        )
        effort_label = (f"思考强度 {effort}" if api_mode == project_llm_settings.API_MODE_RESPONSES
                        else f"思考强度 {effort}（已验证，将发送）" if verified
                        else f"思考强度 {effort}（未验证，不发送）")
        effort_label += " · 流式" if settings.get("chat_stream", False) else " · 非流式"
        self.model_config_label.configure(
            text=f"协议：{api_mode_label} · 模型：{model} · {effort_label}\nURL：{base_url} · API Key：Windows Credential Manager"
        )

    def _edit_model_configuration(self) -> None:
        if self._busy or self._closing:
            return
        try:
            settings = project_llm_settings.load_settings() or {}
            read_error = ""
        except project_llm_settings.ProjectProviderError:
            # Let users repair a malformed file by loading known-safe defaults.
            settings = {}
            read_error = "现有配置无法读取；保存时将以此处的新值替换。"

        dialog = tk.Toplevel(self.root)
        dialog.title("AI 语义复核 · 模型配置")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=18)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)

        base_url = StringVar(dialog, settings.get("base_url", project_llm_settings.DEFAULT_BASE_URL))
        model = StringVar(dialog, settings.get("model", AI_DEFAULT_MODEL))
        effort = StringVar(dialog, settings.get("reasoning_effort", AI_DEFAULT_REASONING_EFFORT))
        api_mode_value = settings.get("api_mode", project_llm_settings.API_MODE_RESPONSES)
        api_mode = StringVar(dialog, API_MODE_LABELS.get(
            api_mode_value, API_MODE_LABELS[project_llm_settings.API_MODE_RESPONSES]
        ))
        chat_stream = BooleanVar(dialog, settings.get("chat_stream", False))
        api_key = StringVar(dialog, "")
        status = StringVar(dialog, read_error)
        mode_hint = StringVar(dialog, "")
        self._model_config_verified_for = settings.get("chat_reasoning_verified_for", "")
        self._model_config_revision = 0

        ttk.Label(body, text="服务地址", style="Hint.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=5)
        base_url_entry = ttk.Entry(body, textvariable=base_url, width=56)
        base_url_entry.grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Label(body, text="Base URL，例如 https://provider.example/v1", style="Hint.TLabel").grid(
            row=1, column=1, sticky="w", pady=(0, 5)
        )

        ttk.Label(body, text="API 协议", style="Hint.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=5)
        api_mode_box = ttk.Combobox(
            body, textvariable=api_mode, values=tuple(API_MODE_CHOICES),
            state="readonly", width=24, style="Review.TCombobox",
        )
        api_mode_box.grid(row=2, column=1, sticky="w", pady=5)
        stream_check = ttk.Checkbutton(body, text="使用流式响应", variable=chat_stream)
        stream_check.grid(row=2, column=2, sticky="w", padx=(8, 0), pady=5)
        ttk.Label(
            body, textvariable=mode_hint,
            style="Hint.TLabel", wraplength=480, justify="left",
        ).grid(row=3, column=1, columnspan=2, sticky="w", pady=(0, 5))

        ttk.Label(body, text="模型名称", style="Hint.TLabel").grid(row=4, column=0, sticky="w", padx=(0, 12), pady=5)
        model_box = ttk.Combobox(body, textvariable=model, values=(model.get(),) if model.get() else (),
                                 state="normal", width=44, style="Review.TCombobox")
        model_box.grid(row=4, column=1, sticky="ew", pady=5)
        read_models_button = ttk.Button(
            body, text="读取模型", style="Small.TButton",
            command=lambda: self._run_model_config_check(dialog, "models"),
        )
        read_models_button.grid(row=4, column=2, sticky="e", padx=(8, 0), pady=5)

        ttk.Label(body, text="思考强度", style="Hint.TLabel").grid(row=5, column=0, sticky="w", padx=(0, 12), pady=5)
        effort_box = ttk.Combobox(
            body, textvariable=effort, values=project_llm_settings.REASONING_EFFORTS,
            state="readonly", width=16, style="Review.TCombobox",
        )
        effort_box.grid(row=5, column=1, sticky="w", pady=5)
        reasoning_button = ttk.Button(
            body, text="验证思考强度", style="Small.TButton",
            command=lambda: self._run_model_config_check(dialog, "reasoning"),
        )
        reasoning_button.grid(row=5, column=2, sticky="e", padx=(8, 0), pady=5)

        def update_api_mode() -> None:
            is_chat = API_MODE_CHOICES.get(api_mode.get()) == "chat_completions"
            stream_check.configure(state="normal")
            reasoning_button.configure(state="normal" if is_chat and not self._model_config_request_busy else "disabled")
            verified = project_llm_settings.is_chat_reasoning_verified(
                base_url.get().strip(), model.get().strip(), effort.get().strip(),
                self._model_config_verified_for, chat_stream=chat_stream.get(),
            )
            if not is_chat:
                mode_hint.set("Responses 使用 /responses，直接发送所选思考强度；支持流式响应，完整接收后校验结果。")
            elif verified:
                mode_hint.set("思考强度已验证，保存后随 Chat 请求发送；修改地址、模型、强度、流式或密钥后需重新验证。")
            else:
                mode_hint.set("Chat 思考强度未验证，不发送。可先选择强度与流式选项，再点击“验证思考强度”。")

        def invalidate_verification(*_args: str) -> None:
            self._model_config_revision += 1
            self._model_config_verified_for = ""
            update_api_mode()
            if not self._model_config_request_busy:
                status.set("")

        ttk.Label(body, text="API Key", style="Hint.TLabel").grid(row=6, column=0, sticky="w", padx=(0, 12), pady=5)
        api_key_entry = ttk.Entry(body, textvariable=api_key, show="•", width=48)
        api_key_entry.grid(row=6, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Label(
            body, text="留空会保留现有密钥；输入新密钥将覆盖。已保存密钥不会被读取或回显。",
            style="Hint.TLabel", wraplength=480, justify="left",
        ).grid(row=7, column=1, columnspan=2, sticky="w", pady=(0, 7))
        ttk.Label(
            body, text="配置仅保存在本机，API Key 使用 Windows Credential Manager。保存后用于后续 AI 复核。",
            style="Hint.TLabel", wraplength=480, justify="left",
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(4, 7))
        ttk.Label(
            body, text="连接与强度验证各发送一次短请求，可能产生少量 API 用量。验证成功仅表示服务接受参数并完整返回，不能证明内部思考预算。读取模型只查询列表。",
            style="Hint.TLabel", wraplength=540, justify="left",
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(0, 7))
        ttk.Label(body, textvariable=status, style="Warning.TLabel", wraplength=480, justify="left").grid(
            row=10, column=0, columnspan=3, sticky="w", pady=(0, 5)
        )

        actions = ttk.Frame(body)
        actions.grid(row=11, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(actions, text="取消", style="Small.TButton",
                   command=lambda: self._close_model_configuration(dialog)).pack(side="right")
        save_button = ttk.Button(
            actions, text="保存配置", style="Review.Primary.TButton",
            command=lambda: self._save_model_configuration(
                dialog, base_url.get(), model.get(), effort.get(), api_key.get(), status,
                API_MODE_CHOICES.get(api_mode.get(), project_llm_settings.API_MODE_RESPONSES),
                chat_stream.get(),
            ),
        )
        save_button.pack(side="right", padx=(0, 8))
        test_connection_button = ttk.Button(
            actions, text="测试连接", style="Small.TButton",
            command=lambda: self._run_model_config_check(dialog, "connection"),
        )
        test_connection_button.pack(side="right", padx=(0, 8))
        dialog.bind("<Escape>", lambda _event: self._close_model_configuration(dialog))
        dialog.bind("<Return>", lambda _event: save_button.invoke())
        dialog.protocol("WM_DELETE_WINDOW", lambda: self._close_model_configuration(dialog))
        dialog.update_idletasks()
        width, height = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.grab_set()
        base_url_entry.focus_set()
        self._model_config_dialog = dialog
        self._model_config_vars = {
            "base_url": base_url, "model": model, "reasoning_effort": effort,
            "api_mode": api_mode, "api_key": api_key, "chat_stream": chat_stream,
        }
        self._model_config_status = status
        self._model_config_model_box = model_box
        self._model_config_api_mode_box = api_mode_box
        self._model_config_effort_box = effort_box
        self._model_config_mode_hint = mode_hint
        self._model_config_refresh_mode = update_api_mode
        self._model_config_buttons = {
            "models": read_models_button,
            "connection": test_connection_button,
            "reasoning": reasoning_button,
            "save": save_button,
        }
        self._model_config_credential_target = settings.get(
            "credential_target", project_llm_settings.CREDENTIAL_TARGET
        )
        for variable in (base_url, model, effort, api_mode, api_key, chat_stream):
            variable.trace_add("write", invalidate_verification)
        update_api_mode()

    def _run_model_config_check(self, dialog: tk.Toplevel, operation: str) -> None:
        """Run provider discovery or a minimal model request without blocking Tk."""
        if (self._model_config_request_busy or self._model_config_dialog is not dialog
                or operation not in {"models", "connection", "reasoning"}):
            return
        variables = self._model_config_vars
        values = {
            "base_url": variables["base_url"].get().strip(),
            "model": variables["model"].get().strip(),
            "reasoning_effort": variables["reasoning_effort"].get().strip(),
            "api_mode": API_MODE_CHOICES.get(
                variables["api_mode"].get().strip(), project_llm_settings.API_MODE_RESPONSES
            ),
            "api_key": variables["api_key"].get().strip(),
            "chat_stream": variables["chat_stream"].get(),
            "chat_reasoning_verified_for": self._model_config_verified_for,
            "credential_target": getattr(
                self, "_model_config_credential_target", project_llm_settings.CREDENTIAL_TARGET
            ),
        }
        if not values["base_url"]:
            self._model_config_status.set("请先填写服务地址。")
            return
        try:
            parsed_url = urlsplit(values["base_url"])
            valid_url = (
                parsed_url.scheme == "https" and bool(parsed_url.hostname)
                and parsed_url.username is None and parsed_url.password is None
                and not parsed_url.query and not parsed_url.fragment
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            self._model_config_status.set("服务地址须为不含凭据、查询参数或片段的 HTTPS Base URL。")
            return
        if operation != "models" and not values["model"]:
            self._model_config_status.set("请先填写模型名称，或先读取并选择一个可用模型。")
            return
        if operation == "reasoning" and values["api_mode"] != "chat_completions":
            self._model_config_status.set("强度验证用于 Chat Completions；Responses 直接使用所选强度。")
            return
        if operation == "reasoning":
            self._model_config_verified_for = ""
            self._model_config_refresh_mode()

        self._model_config_request_busy = True
        revision = self._model_config_revision
        self._model_config_status.set(
            {"models": "正在读取服务模型列表…", "connection": "正在测试连接…",
             "reasoning": "正在验证思考强度；仅完整返回文本才算成功…"}[operation]
        )
        for button in getattr(self, "_model_config_buttons", {}).values():
            button.configure(state="disabled")
        results: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)

        def work() -> None:
            api_key: str | None = None
            try:
                api_key = values["api_key"] or project_llm_settings.load_api_key(values["credential_target"])
                config = {
                    "base_url": values["base_url"],
                    "model": values["model"] or AI_DEFAULT_MODEL,
                    "reasoning_effort": values["reasoning_effort"] or AI_DEFAULT_REASONING_EFFORT,
                    "api_mode": values["api_mode"],
                    "chat_stream": values["chat_stream"],
                    "chat_reasoning_verified_for": values["chat_reasoning_verified_for"],
                    "api_key": api_key,
                    "requires_auth": True,
                    "disable_response_storage": True,
                    # High reasoning effort can take longer than the former
                    # 25-second connection limit even with streaming enabled.
                    "timeout": 25 if operation == "models" else 120,
                    "max_retries": 0,
                }
                client = CodexResponsesClient(config, response_schema=None)
                if operation == "models":
                    result = {"models": client.list_models()}
                elif operation == "reasoning":
                    verified = client.test_chat_reasoning_effort(
                        model=values["model"], reasoning_effort=values["reasoning_effort"]
                    )
                    if verified is not True:
                        raise ValueError("未取得完整的模型验证结果。")
                    result = {"reasoning_verified": True}
                else:
                    client.test_connection(
                        model=values["model"], reasoning_effort=values["reasoning_effort"]
                    )
                    result = {"connected": True}
            except Exception as error:
                # Do not trust provider diagnostics to omit a caller's token.
                # The client already uses safe errors; also guard other errors
                # raised by injected or platform transports before showing them.
                message = str(error)
                for secret in (values["api_key"], api_key):
                    if isinstance(secret, str) and secret:
                        message = message.replace(secret, "[REDACTED]")
                result = {"error": _safe(message)}
            results.put(result)

        self._model_config_worker = threading.Thread(
            target=work, name=f"model-config-{operation}", daemon=True
        )
        self._model_config_worker.start()

        def poll() -> None:
            if self._model_config_dialog is not dialog:
                return
            try:
                if not dialog.winfo_exists():
                    return
                result = results.get_nowait()
            except queue.Empty:
                try:
                    dialog.after(100, poll)
                except tk.TclError:
                    pass
                return
            except tk.TclError:
                return

            self._model_config_request_busy = False
            for button in getattr(self, "_model_config_buttons", {}).values():
                try:
                    button.configure(state="normal")
                except tk.TclError:
                    pass
            self._model_config_refresh_mode()
            if result.get("error"):
                if operation == "models":
                    self._model_config_status.set(
                        f"读取模型失败：{result['error']}。仍可手动填写模型名称并测试连接。"
                    )
                elif operation == "reasoning":
                    self._model_config_status.set(f"思考强度验证失败，未启用：{result['error']}")
                else:
                    self._model_config_status.set(f"连接测试失败：{result['error']}")
            elif operation == "models":
                models = result.get("models", [])
                model_box = getattr(self, "_model_config_model_box", None)
                if model_box is not None:
                    model_box.configure(values=tuple(models))
                self._model_config_status.set(
                    f"读取到 {len(models)} 个可用模型；当前选择未更改。"
                    if models else "接口可访问，但没有返回模型列表；仍可手动填写模型名称。"
                )
            elif operation == "reasoning":
                if self._model_config_revision != revision:
                    self._model_config_status.set("原配置验证已完成，但表单已修改；未启用参数，请验证当前配置。")
                    return
                self._model_config_verified_for = project_llm_settings.chat_reasoning_signature(
                    values["base_url"], values["model"], values["reasoning_effort"],
                    chat_stream=values["chat_stream"],
                )
                self._model_config_refresh_mode()
                self._model_config_status.set(
                    f"验证成功：服务接受 reasoning_effort={values['reasoning_effort']} 并完整返回。保存后启用。"
                )
            else:
                mode_label = API_MODE_LABELS[values["api_mode"]]
                verified = project_llm_settings.is_chat_reasoning_verified(
                    values["base_url"], values["model"], values["reasoning_effort"],
                    values["chat_reasoning_verified_for"], chat_stream=values["chat_stream"],
                )
                effort_label = (f"思考强度 {values['reasoning_effort']}"
                                if values["api_mode"] == "responses" or verified else "未发送思考强度")
                self._model_config_status.set(
                    f"连接成功：{mode_label} 已返回模型 {values['model']} 的测试结果（{effort_label}）。"
                )

        dialog.after(100, poll)

    def _close_model_configuration(self, dialog: tk.Toplevel) -> None:
        try:
            dialog.grab_release()
        except tk.TclError:
            pass
        try:
            destroy_tk_widget(dialog)
        except tk.TclError:
            pass
        if getattr(self, "_model_config_dialog", None) is dialog:
            self._model_config_dialog = None
            self._model_config_vars = {}
            self._model_config_status = None
            self._model_config_buttons = {}
            self._model_config_model_box = None
            self._model_config_api_mode_box = None
            self._model_config_effort_box = None
            self._model_config_mode_hint = None
            self._model_config_refresh_mode = None
            self._model_config_verified_for = ""
            self._model_config_request_busy = False

    def _save_model_configuration(self, dialog: tk.Toplevel, base_url: str, model: str,
                                  reasoning_effort: str, api_key: str, status: StringVar,
                                  api_mode: str = project_llm_settings.API_MODE_RESPONSES,
                                  chat_stream: bool = False) -> None:
        if self._busy or self._closing or self._model_config_request_busy:
            return
        proof = getattr(self, "_model_config_verified_for", "")
        if api_mode != "chat_completions" or not project_llm_settings.is_chat_reasoning_verified(
            base_url.strip(), model.strip(), reasoning_effort.strip(), proof, chat_stream=chat_stream,
        ):
            proof = ""
        new_key = api_key.strip()
        settings_options = {
            "base_url": base_url, "model": model, "reasoning_effort": reasoning_effort,
            "api_mode": api_mode, "chat_stream": chat_stream,
            "credential_target": getattr(self, "_model_config_credential_target", project_llm_settings.CREDENTIAL_TARGET),
        }
        try:
            settings_path = project_llm_settings.save_settings(
                **settings_options, chat_reasoning_verified_for="" if new_key else proof,
            )
        except (project_llm_settings.ProjectProviderError, OSError) as error:
            status.set(str(error) if isinstance(error, project_llm_settings.ProjectProviderError)
                       else "无法写入本机模型配置；请检查本机配置目录的权限。")
            return

        if new_key:
            try:
                settings = project_llm_settings.load_settings(settings_path) or {}
                project_llm_settings.store_api_key(
                    new_key, settings.get("credential_target", project_llm_settings.CREDENTIAL_TARGET),
                )
            except project_llm_settings.ProjectProviderError:
                self._refresh_model_config_label()
                status.set("模型参数已保存，但 API Key 未能保存到 Windows Credential Manager；请检查系统凭据权限后重试。")
                return
            # A probe made with a new key cannot enable the previously stored
            # credential if Credential Manager rejects the replacement.
            if proof:
                try:
                    project_llm_settings.save_settings(**settings_options, chat_reasoning_verified_for=proof)
                except (project_llm_settings.ProjectProviderError, OSError):
                    self._refresh_model_config_label()
                    status.set("模型和密钥已保存，但验证记录无法保存；思考强度未启用，请重试保存。")
                    return

        self._refresh_model_config_label()
        self.notice.set("模型配置已保存；后续 AI 语义复核将使用新配置，历史报告不会自动重写。")
        self._close_model_configuration(dialog)

    def _browser_mode_value(self) -> str:
        value = self.browser_mode.get().strip()
        return BROWSER_MODE_VALUES.get(value, value if value in {"single", "dual"} else DEFAULT_JD_BROWSER_MODE)

    def _browser_mode_selected(self, _event: tk.Event | None = None) -> None:
        self._browser_mode_changed()
        self._remember_browser_settings()

    def _browser_mode_changed(self) -> None:
        """Keep the Edge controls visibly dormant in the single-browser mode."""
        dual = self._browser_mode_value() == "dual"
        blocked = self._execution_blocked
        self._enabled(self.browser_mode_box, not blocked, "readonly")
        self._enabled(self.chrome_port_entry, not blocked)
        self._enabled(self.chrome_profile_entry, not blocked)
        self._enabled(self.edge_port_entry, dual and not blocked)
        self._enabled(self.edge_profile_entry, dual and not blocked)
        if not dual:
            self.edge_status_text.set("Edge：未使用")
        elif self._browser_statuses.get("edge") == "disabled":
            self._set_browser_status("edge", "pending")
        if hasattr(self, "prepare_browser_button"):
            self.prepare_browser_button.configure(text="准备 Chrome + Edge / 登录" if dual else "准备 Chrome / 登录")

    def _remember_browser_settings(self, _event: tk.Event | None = None) -> None:
        if self._busy or self._closing:
            return
        # desktop_preferences intentionally has a narrow shared allowlist.  A
        # newer installation may add these fields; older ones simply ignore
        # them without losing existing preferences.
        values = {
            "browser_mode": self._browser_mode_value(), "chrome_port": self.chrome_port.get().strip(),
            "edge_port": self.edge_port.get().strip(), "chrome_profile": self.chrome_profile.get().strip(),
            "edge_profile": self.edge_profile.get().strip(),
        }
        if any(self._preferences.get(key) != value for key, value in values.items()):
            self._persist_preferences(**values)

    def _browser_options(self, *, validate: bool = True) -> dict[str, Any]:
        mode = self._browser_mode_value()
        if validate:
            chrome_port = _positive(self.chrome_port.get(), "Chrome CDP 端口", maximum=65535)
            edge_port = (_positive(self.edge_port.get(), "Edge CDP 端口", maximum=65535)
                         if mode == "dual" else self.edge_port.get().strip())
        else:
            chrome_port = self.chrome_port.get().strip()
            edge_port = self.edge_port.get().strip()
        return {
            "browser_mode": mode,
            "chrome_port": chrome_port,
            "edge_port": edge_port,
            "chrome_profile": self.chrome_profile.get().strip() or None,
            "edge_profile": self.edge_profile.get().strip() or None,
            # review_pipeline names the profile paths explicitly as *_dir;
            # retain the shorter aliases for older injected backends.
            "chrome_profile_dir": self.chrome_profile.get().strip() or None,
            "edge_profile_dir": self.edge_profile.get().strip() or None,
        }

    def _set_browser_status(self, browser: str, status: Any) -> None:
        normalized = _browser_status(status)
        self._browser_statuses[browser] = normalized
        label = "Chrome" if browser == "chrome" else "Edge"
        text = BROWSER_STATUS_LABELS.get(normalized, normalized)
        variable = self.chrome_status_text if browser == "chrome" else self.edge_status_text
        variable.set(f"{label}：{text}")
        status_label = getattr(self, f"{browser}_status_label", None)
        if status_label is not None and hasattr(self, "colors"):
            role = ("green" if normalized == "completed" else
                    "red" if normalized == "failed" else
                    "accent" if normalized == "running" else
                    "amber" if normalized in {"paused", "stopped", "waiting", "partial"} else "muted")
            status_label.configure(foreground=self.colors[role])

    def _set_browser_statuses(self, statuses: dict[str, Any] | None = None, *, fallback: str | None = None) -> None:
        statuses = statuses or {}
        mode = self._browser_mode_value()
        for browser in ("chrome", "edge"):
            if browser == "edge" and mode != "dual":
                self._browser_statuses[browser] = "disabled"
                self.edge_status_text.set("Edge：未使用")
                continue
            value = statuses.get(browser, fallback if fallback is not None else self._browser_statuses.get(browser, "pending"))
            self._set_browser_status(browser, value)

    def _apply_browser_status_result(self, value: Any, *, fallback: str | None = None) -> None:
        self._set_browser_statuses(_browser_result_map(value), fallback=fallback)

    def _set_theme(self, value: str, *, persist: bool = True) -> None:
        theme = normalize_theme(value)
        self.theme.set(theme)
        self.colors = palette_for(theme)
        p = self.colors
        configure_styles(self.root, p)
        style = ttk.Style(self.root)
        style.configure("Review.Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Review.Binding.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Review.Context.TLabel", font=("Microsoft YaHei UI", 9), foreground=p["muted"])
        style.configure("Small.TButton", padding=(6, 3))
        # Highlight a filter without increasing the whole toolbar's height.
        style.configure("Review.Theme.TButton", padding=(6, 3))
        style.configure("Review.Primary.TButton", padding=(10, 5))
        style.configure("Review.TEntry", padding=4, font=("Microsoft YaHei UI", 9))
        style.configure("Review.TCombobox", padding=3, font=("Microsoft YaHei UI", 9))
        style.configure("Review.Treeview", rowheight=31)
        style.configure("Review.Treeview.Heading", padding=(7, 4))
        style.configure("Review.Step.TButton", padding=(6, 4), font=("Microsoft YaHei UI", 9))
        style.configure("Review.ActiveStep.TButton", padding=(6, 4), font=("Microsoft YaHei UI", 9, "bold"),
                        background=p["accent_soft"], foreground=p["accent"])
        style.map("Review.ActiveStep.TButton", background=[("disabled", p["disabled"]), ("active", p["button_hover"]), ("!disabled", p["accent_soft"])],
                  foreground=[("disabled", p["disabled_text"]), ("!disabled", p["accent"])])
        style.configure("Review.StaleStep.TButton", padding=(6, 4), font=("Microsoft YaHei UI", 9), foreground=p["amber"])
        style.map("Review.StaleStep.TButton", foreground=[("disabled", p["disabled_text"]), ("!disabled", p["amber"])])
        style.configure("Review.Detail.TNotebook.Tab", padding=(9, 6), font=("Microsoft YaHei UI", 9))
        self.theme_button_text.set("日间模式" if theme == "dark" else "夜间模式")
        for text, log in self._text_widgets:
            text.configure(background=p["log" if log else "surface"], foreground=p["log_text" if log else "text"],
                           selectbackground=p["log_selection" if log else "selection"],
                           selectforeground=p["log_selected_text" if log else "selected_text"],
                           insertbackground=p["text"], highlightbackground=p["border"], highlightcolor=p["focus"])
        self._style_review_rules_editor()
        self.results_table.tag_configure("excluded", background=p["table_excluded"], foreground=p["muted"])
        self.results_table.tag_configure("kept", background=p["surface"], foreground=p["text"])
        self.results_table.tag_configure("suggest_keep", background=p["surface"], foreground=p["text"])
        self.results_table.tag_configure("suggest_exclude", background=p["table_suggest_exclude"], foreground=p["red"])
        self.results_table.tag_configure("pending_review", background=p["amber_soft"], foreground=p["amber"])
        self.results_table.tag_configure("stale", background=p["amber_soft"], foreground=p["amber"])
        for combo in self._comboboxes:
            configure_combobox_popdown(combo, p)
        self.collection_page.apply_theme(p)
        self.delivery_page.apply_theme(p)
        self._paint_notice()
        self._paint_badge()
        if persist:
            self._persist_preferences(theme=theme)

    def _toggle_theme(self) -> None:
        self._set_theme("light" if self.theme.get() == "dark" else "dark")

    def _paint_notice(self) -> None:
        role = "amber" if self._notice_kind == "warning" else "accent"
        self.notice_label.configure(background=self.colors[f"{role}_soft"], foreground=self.colors[role])

    def _paint_badge(self) -> None:
        info = self._stage_info()
        status = info.get("status", "pending")
        role = "green" if info.get("confirmed") and status == "completed" else "amber" if status in ("stale", "waiting", "paused", "stopped") else "red" if status == "failed" else "accent" if status == "running" else "muted"
        background = self.colors["neutral_soft" if role == "muted" else f"{role}_soft"]
        self.status_badge.configure(background=background, foreground=self.colors[role])

    def _notify(self, value: str, *, warning: bool = False) -> None:
        self.notice.set(_safe(value))
        self._notice_kind = "warning" if warning else "note"
        self._paint_notice()

    def _toggle_settings(self) -> None:
        if self.settings_frame.winfo_manager():
            self.settings_frame.grid_remove()
        else:
            self.settings_frame.grid(row=3, column=0, sticky="ew", pady=(4, 0))

    def _edit_review_rules(self) -> None:
        if self.task is None or self._execution_blocked:
            return
        if self._review_rules_dialog is not None:
            self._review_rules_dialog.lift()
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("AI 语义复核 · 编辑本任务规则")
        dialog.transient(self.root)
        dialog.minsize(620, 460)
        width = min(900, max(620, self.root.winfo_screenwidth() - 100))
        height = min(760, max(460, self.root.winfo_screenheight() - 140))
        dialog.geometry(f"{width}x{height}")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        body = ttk.Frame(dialog, padding=16)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        ttk.Label(body, text="修改 AI 对岗位工作内容的判断规则", style="Section.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(body, text=("AI 接收完整职位职责与任职要求；模型只返回判断和理由，报告采用同一判断。"
                              "规则只保存到当前任务，保存不会改变已发起请求。重置 AI 阶段不会改规则；"
                              "重置后真正点击运行时会重新读取已保存规则。"
                              "重置会清除本阶段旧结果，旧结果请在重置前查看。可恢复默认后保存。"
                              "识别出的旧版六维规则仅在重置后再次运行 AI 时切换为简化默认；其他自定义规则保留。"),
                  style="Hint.TLabel", wraplength=740, justify="left").grid(
            row=1, column=0, sticky="ew", pady=(6, 10))
        area = ttk.Frame(body)
        area.grid(row=2, column=0, sticky="nsew")
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        editor = tk.Text(area, wrap="word", undo=True, maxundo=60, relief="flat", borderwidth=1,
                         padx=12, pady=10, font=("Microsoft YaHei UI", 10), exportselection=False)
        editor.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(area, orient="vertical", command=editor.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        editor.configure(yscrollcommand=scroll.set)
        editor.insert("1.0", self._saved_review_rules)
        editor.edit_modified(False)
        status = StringVar(dialog, "修改后点击“保存本任务规则”；保存不会自动请求模型或改变正在运行的请求。")
        ttk.Label(body, textvariable=status, style="Hint.TLabel", wraplength=740,
                  justify="left").grid(row=3, column=0, sticky="ew", pady=(8, 4))
        actions = ttk.Frame(body)
        actions.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        reset = ttk.Button(actions, text="恢复默认", style="Small.TButton",
                           command=self._reset_review_rules_draft)
        reset.pack(side="left")
        cancel = ttk.Button(actions, text="取消", style="Small.TButton", command=self._close_review_rules_editor)
        cancel.pack(side="right")
        save = ttk.Button(actions, text="保存本任务规则", style="Review.Primary.TButton",
                          command=self._save_review_rules)
        save.pack(side="right", padx=(0, 8))
        self._review_rules_dialog, self._review_rules_editor = dialog, editor
        self._review_rules_status = status
        self._review_rules_task = self.task.path
        self._review_rules_buttons = {"save": save, "reset": reset, "cancel": cancel}
        self._style_review_rules_editor()
        dialog.protocol("WM_DELETE_WINDOW", self._close_review_rules_editor)
        dialog.bind("<Escape>", lambda _event: self._close_review_rules_editor())
        dialog.bind("<Control-s>", lambda _event: self._save_review_rules())
        dialog.grab_set()
        editor.focus_set()

    def _style_review_rules_editor(self) -> None:
        editor = self._review_rules_editor
        if editor is not None:
            p = self.colors
            editor.configure(background=p["surface"], foreground=p["text"], insertbackground=p["text"],
                             selectbackground=p["selection"], selectforeground=p["selected_text"],
                             highlightbackground=p["border"], highlightcolor=p["focus"])

    def _reset_review_rules_draft(self) -> None:
        if self._execution_blocked or self._review_rules_editor is None:
            return
        editor = self._review_rules_editor
        editor.delete("1.0", "end")
        editor.insert("1.0", self._default_review_rules)
        if self._review_rules_status is not None:
            self._review_rules_status.set("默认规则已载入编辑框，点击保存后才会应用到本任务。")

    def _save_review_rules(self) -> None:
        if self._execution_blocked or self.task is None or self._review_rules_editor is None:
            return
        if self.task.path != self._review_rules_task:
            if self._review_rules_status is not None:
                self._review_rules_status.set("当前任务已改变，请关闭编辑器后重新打开。")
            return
        draft = self._review_rules_editor.get("1.0", "end-1c").strip()
        if not draft:
            if self._review_rules_status is not None:
                self._review_rules_status.set("复核规则不能为空；可点击“恢复默认”后再保存。")
            return
        task = self.task
        rules = None if draft == self._default_review_rules.strip() else draft

        def save() -> dict[str, Any]:
            task.set_review_rules(rules)
            return {"task": task, "view": self._capture_view(task), "close_rules_editor": True,
                    "notice": "本任务复核规则已保存。已有 AI 与报告结果如受影响会标记失效；请手动运行 AI 复核，再生成报告。"}

        self._submit("save-review-rules", save, status="正在保存本任务复核规则…")

    def _close_review_rules_editor(self) -> None:
        if self._busy and self._operation == "save-review-rules":
            return
        dialog = self._review_rules_dialog
        if dialog is not None:
            dialog.grab_release()
            dialog.destroy()
        self._review_rules_dialog, self._review_rules_editor = None, None
        self._review_rules_status = None
        self._review_rules_task = None
        self._review_rules_buttons = {}

    def _stage_info(self, stage: str | None = None) -> dict[str, Any]:
        return self.manifest.get("stages", {}).get(stage or self.stage.get(), {})

    def _can_run(self, stage: str | None = None) -> bool:
        if self.task is None or self._execution_blocked:
            return False
        if any(info.get("status") == "running" for info in self.manifest.get("stages", {}).values()):
            return False
        key = stage or self.stage.get()
        if key not in REVIEW_STAGES:
            return False
        for upstream in STAGES[:STAGES.index(key)]:
            info = self._stage_info(upstream)
            if info.get("status") != "completed" or not info.get("confirmed"):
                return False
        return True

    def _can_edit(self) -> bool:
        return (self.task is not None and not self._execution_blocked and self.stage.get() != "report"
                and not any(info.get("status") == "running" for info in self.manifest.get("stages", {}).values())
                and self._stage_info().get("status") not in (None, "pending", "running", "stale")
                and bool(self._rows.get(self.stage.get()))
                and (self.stage.get() != "llm" or all(job.get("review_format") == SIMPLE_REVIEW_FORMAT
                                                     for job in self._rows["llm"])))

    def _can_confirm(self) -> bool:
        return self._can_run() and self._stage_info().get("status") == "completed"

    def _can_reset_stage(self) -> bool:
        info = self._stage_info()
        return (self._can_run() and info.get("attempt", 0) > 0
                and info.get("status") not in (None, "pending", "running"))

    def _can_edit_list_queues(self) -> bool:
        return (self.stage.get() == "shortlist" and self._can_edit() and self._can_confirm()
                and "shortlist" not in self._previews
                and all(not _saved_list_queue(job)[1] for job in self._rows["shortlist"]))

    def _render_list_queues(self) -> None:
        if self.stage.get() != "shortlist":
            self.list_queue_frame.grid_remove()
            self._enabled(self.list_queue_box, False)
            self._enabled(self.relevant_only_button, False)
            self._enabled(self.add_verify_button, False)
            return
        self.list_queue_frame.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        jobs = self._rows.get("shortlist", [])
        preview = "shortlist" in self._previews
        counts, legacy_count = _list_queue_summary(jobs) if not preview else ({key: 0 for key in LIST_QUEUE_LABELS}, 0)
        if preview or not jobs:
            hint = "尚无本步队列；初筛后默认保留明确相关＋待核实，仍须人工确认后补 JD。"
        elif legacy_count:
            hint = LEGACY_LIST_QUEUE_NOTICE + "；当前仅旧版兼容，队列动作禁用。"
        else:
            hint = "默认保留明确相关＋待核实；仅列表方向，仍须 JD，不等于最终可投。"
        self.list_queue_hint.set(hint)
        self._enabled(self.list_queue_box, bool(jobs) and not preview and not self._closing, "readonly")
        editable = self._can_edit_list_queues()
        self._enabled(self.relevant_only_button, editable)
        self._enabled(self.add_verify_button, editable and counts["verify"] > 0)

    @staticmethod
    def _enabled(widget: Any, enabled: bool, normal: str = "normal") -> None:
        widget.configure(state=normal if enabled else "disabled")

    def _sample_mode_changed(self, *, focus: bool = False) -> None:
        editable = (self.sample_mode.get() == SAMPLE_CHOICES[1] and self.task is None
                    and not self._execution_blocked)
        self._enabled(self.sample_entry, editable)
        if focus and editable:
            self.sample_entry.focus_set()

    def _render(self, *, render_rows: bool = True) -> None:
        blocked = self._execution_blocked
        key = self.stage.get()
        info = self._stage_info()
        status = info.get("status", "pending")
        bound = self.task is not None
        if bound:
            self.setup_frame.grid_remove()
            self.binding_frame.grid(row=1, column=0, sticky="ew")
            name = self.manifest.get("name", "未命名任务")
            self.binding_title.set(_safe(name))
            self.binding_source.set("固定输入（只读）：" + _safe(self.manifest.get("source_root", "")))
            self.binding_output.set("同一任务输出：" + str(self.task.path))
            city = self.manifest.get("city") or "全部城市"
            track = next((label for label, value in TRACK_CHOICES.items() if value == self.manifest.get("track")), self.manifest.get("track", "all"))
            sample = self.manifest.get("sample_size")
            imported = self._stage_info("import")
            count = len(imported.get("keep_ids", [])) + len(imported.get("excluded_ids", []))
            actual = str(count) if imported.get("status") not in ("pending", "running") else "未完成"
            self.binding_scope.set(f"固定范围：{city} · {track} · 去重岗位数 {sample if sample is not None else '全部（不限）'}  |  已导入 {actual} 个")
        else:
            self.binding_frame.grid_remove()
            self.setup_frame.grid(row=1, column=0, sticky="ew")
        for widget in self._setup_controls:
            self._enabled(widget, not blocked, "readonly" if widget in self._comboboxes else "normal")
        self._sample_mode_changed()
        for widget in (self.new_task_button, self.open_bound_button, self.task_folder_button, self.refresh_button):
            self._enabled(widget, bound and not blocked)
        # Page navigation stays available while work is running; execution is
        # guarded separately so the other page can be inspected safely.
        self._enabled(self.collector_button, not self._closing)
        self._enabled(self.review_button, not self._closing)
        self._enabled(self.delivery_button, not self._closing)
        for index, stage in enumerate(REVIEW_STAGES):
            stage_info = self._stage_info(stage)
            stage_status = stage_info.get("status", "pending")
            label = "已确认 · " + str(len(stage_info.get("keep_ids", []))) if stage_info.get("confirmed") and stage_status == "completed" else STATUS_LABELS.get(stage_status, stage_status)
            button = self.stage_buttons[stage]
            button.configure(text=f"{index + 1}  {STAGE_LABELS[stage]}\n{label}", style="Review.ActiveStep.TButton" if stage == key else "Review.StaleStep.TButton" if stage_status == "stale" else "Review.Step.TButton")
            locked = bound and index > 0 and not self._stage_info(STAGES[index - 1]).get("confirmed") and stage_status == "pending"
            self._enabled(button, not self._busy and not self._closing and not locked)
        self.stage_title.set(f"{STAGES.index(key) + 1}. {STAGE_LABELS[key]}")
        self.stage_description.set(STAGE_DESCRIPTIONS[key])
        if key == "llm":
            self.edit_rules_button.pack(side="right", padx=5)
        else:
            self.edit_rules_button.pack_forget()
        self._enabled(self.edit_rules_button, key == "llm" and bound and not blocked)
        if self._review_rules_editor is not None:
            self._review_rules_editor.configure(state="disabled" if blocked else "normal")
            for button in self._review_rules_buttons.values():
                self._enabled(button, not blocked)
        for stage_key, frame in (("fetch-jd", self.jd_settings_frame), ("llm", self.llm_settings_frame)):
            if key == stage_key:
                frame.grid(row=0, column=0, sticky="ew", pady=(0, 4))
            else:
                frame.grid_remove()
        if not self._busy:
            self.status_text.set("名单已确认" if info.get("confirmed") and status == "completed" else STATUS_LABELS.get(status, status) if bound else "尚未绑定任务")
        self._paint_badge()
        label = "更新本步" if status == "stale" else "继续本步" if status in ("paused", "stopped", "waiting", "failed") else "复用检查点运行" if status == "completed" else "运行本步"
        self.run_button.configure(text=label)
        self._enabled(self.run_button, self._can_run())
        self._enabled(self.reset_button, self._can_reset_stage())
        active = self.workflow_running and self._control is not None
        self._enabled(self.pause_button, active)
        self._enabled(self.stop_button, active)
        self._enabled(self.continue_button, not self._busy and status == "waiting" and self._can_run())
        for widget, normal in self._setting_controls:
            self._enabled(widget, not blocked, normal)
        self._browser_mode_changed()
        self._enabled(self.prepare_browser_button, bound and not blocked)
        if key == "fetch-jd" and not self._busy:
            self._apply_browser_status_result(info.get("stats"))
        progress = info.get("progress") or {}
        done, total = progress.get("done"), progress.get("total")
        known = type(done) is int and type(total) is int and done >= 0 and total >= 0
        self.progress_bar.configure(maximum=max(1, total) if known else 1, value=min(done, total) if known else 0)
        progress_caption = f"落盘进度 {done} / {total}" if known else "进度未提供"
        stats = info.get("stats") or {}
        stats_parts = _metric_parts(key, stats)
        self.progress_text.set(progress_caption + (" · " + " / ".join(stats_parts) if stats_parts else ""))
        keep, excluded = len(info.get("keep_ids", [])), len(info.get("excluded_ids", []))
        pending = len(info.get("pending_ids", [])) if key == "llm" else 0
        inputs = len(info.get("input_ids", []))
        if key == "report" and status == "completed" and stats.get("classification_counts"):
            counts = stats.get("actionable_classification_counts") or stats["classification_counts"]
            pending = counts.get("待核实", stats.get("pending_verification_count", stats.get("llm_verification_count", 0)))
            self.count_text.set(f"全量追踪 {stats.get('total_jobs', stats.get('unique_job_count', '—'))}  ·  重点 {counts.get('重点投递', '—')}  /  海投 {counts.get('海投池', '—')}  /  暂不投 {counts.get('暂不投', '—')}  /  待核实 {pending}")
        else:
            if key == "llm":
                self.count_text.set(f"本步输入 {inputs}  ·  明确保留 {keep}  ·  明确剔除 {excluded}  ·  待核实 {pending}" + ("  ·  旧结果仅供查看" if status == "stale" else ""))
            else:
                self.count_text.set(f"本步输入 {inputs}  ·  保留 {keep}  ·  剔除 {excluded}" + ("  ·  旧结果仅供查看" if status == "stale" else ""))
        self.confirm_text.set(f"确认最终保留 {keep} 个" if key == "llm" else f"确认 {keep} 个进入下一步")
        self._enabled(self.confirm_button, self._can_confirm())
        editable = self._can_edit()
        for widget in (self.select_page_button, self.exclude_button, self.restore_button, self.recommend_button):
            self._enabled(widget, editable)
        self.restore_button.configure(text="保留所选" if key == "llm" else "恢复所选")
        self._enabled(self.detail_toggle_button, editable and self._focus_id is not None)
        self._render_list_queues()
        if key == "llm":
            self.go_delivery_button.grid()
        else:
            self.go_delivery_button.grid_remove()
        self._enabled(self.go_delivery_button, bound and not self._closing)
        for legacy in (self.open_report_button, self.json_button, self.markdown_button):
            self._enabled(legacy, False)
        self._enabled(self.log_button, bound and not self._busy)
        if render_rows:
            self._render_rows()
        self._apply_workspace_visibility()

    def _kept(self, job: dict[str, Any]) -> bool:
        if self.stage.get() == "report":
            return report_pool_eligible(report_decision(job))
        return job_id(job) in self._stage_info().get("keep_ids", [])

    def _row_reason(self, job: dict[str, Any]) -> str:
        if self.stage.get() == "report":
            decision = report_decision(job)
            classification = decision.get("classification") or "待生成报告"
            if decision.get("report_decision_source") == "manual_llm_confirmed":
                return str(classification) + " · AI复核已确认人工" + ("保留" if decision["report_manual_keep"] else "剔除")
            if decision.get("excluded_at"):
                return "暂不投 · 人工剔除于" + STAGE_LABELS.get(decision["excluded_at"], decision["excluded_at"])
            if classification == "待核实":
                return "待核实 · 尚无有效AI判断或已确认人工决定"
            return str(classification)
        if self.stage.get() in self._previews:
            return "输入预览 · 尚未运行"
        if self.stage.get() == "import":
            sources = job.get("observed_in")
            return f"去重完成 · {len(sources)} 个来源" if isinstance(sources, list) else "去重完成 · 来源待核实"
        if self.stage.get() == "fetch-jd":
            issue = job.get("jd_fetch_issue")
            return _jd_completeness_label(job) + " · 本步不筛选" + (" · " + _display(issue).replace("\n", " ") if issue else "")
        if self.stage.get() == "shortlist":
            return _list_queue_label(job) + " · " + _list_queue_reason(job).replace("\n", " ")
        if self.stage.get() == "llm" and job.get("review_format") != SIMPLE_REVIEW_FORMAT:
            return "旧版结果 · 只读历史 · 需重跑AI采用新版判断"
        if self.stage.get() == "llm":
            info = self._stage_info() if hasattr(self, "_stage_info") else {}
            manual = info.get("manual_choices", {}).get(job_id(job))
            if type(manual) is bool:
                label = "人工保留" if manual else "人工剔除"
                label += "（已确认）" if info.get("confirmed") else "（待确认）"
                judgment = job.get("ai_judgment") or "未完成有效判断"
                reason = job.get("ai_reason") or job.get("model_error")
                return label + " · AI判断：" + str(judgment) + (" · " + _display(reason).replace("\n", " ") if reason else "")
        if self.stage.get() == "llm" and job.get("llm_stage_decision") == "pending_verification":
            message = _display(job.get("ai_reason") or job.get("reason")).replace("\n", " ")
            if job.get("model_status") == "failed" and job.get("model_error"):
                message += " · " + _display(job["model_error"]).replace("\n", " ")[:200]
            return "待人工核实 · 不在保留或剔除名单 · " + message
        recommend = job.get("recommended_keep")
        if recommend is None and self.stage.get() == "shortlist":
            recommend = job.get("list_pass")
        label = "建议剔除" if recommend is False else "建议保留" if recommend is True else "待人工核实"
        reason = (job.get("ai_reason") if self.stage.get() == "llm" else None) or job.get("reason") or job.get("reasons") or job.get("list_exclusion_evidence")
        if not reason:
            reason = job.get("jd_status") if self.stage.get() == "fetch-jd" else job.get("model_status") if self.stage.get() == "llm" else None
        return label + (" · " + _display(reason).replace("\n", " ") if reason else "")

    def _render_rows(self) -> None:
        stage = self.stage.get()
        jobs = self._rows.get(stage, [])
        info = self._stage_info()
        row_view = self._row_views.get(stage)
        # Published worker indexes are immutable. The fallback also supports
        # locally injected/test views through the historic _rows interface.
        if (row_view is None or row_view["groups"]["全部"] is not jobs
                or row_view["kept_ids"] != frozenset(info.get("keep_ids", []))
                or row_view["pending_ids"] != frozenset(info.get("pending_ids", []))):
            info = {"keep_ids": [], "excluded_ids": [], "pending_ids": [], **info}
            row_view = _row_view(jobs, info, stage, TaskViewCache())
            self._row_views[stage] = row_view
        query = self.search.get().strip().casefold()
        selected_filter = self.filter.get()
        selected_queue = next((key for key, label in LIST_QUEUE_LABELS.items()
                               if label == self.list_queue_filter.get()), None)
        preview = stage in self._previews
        use_queue = stage == "shortlist" and not preview and selected_queue is not None
        filter_key = (stage, id(row_view), id(self._search_index.get(stage)), query, selected_filter,
                      selected_queue if use_queue else None)
        self._view_filter = (stage, query, selected_filter, selected_queue if use_queue else None)
        if filter_key != self._filtered_key:
            candidates = row_view["groups"].get(selected_filter, jobs)
            index = self._search_index.get(stage, {})
            if self._prepared_filter is not None and self._prepared_filter[0] == filter_key:
                self._filtered_jobs = self._prepared_filter[1]
            else:
                self._filtered_jobs = (candidates if not query and not use_queue else
                                       [job for job in candidates if (not query or query in index.get(job_id(job), ""))
                                        and (not use_queue or _saved_list_queue(job)[0] == selected_queue)])
            self._filtered_key = filter_key
        filtered = self._filtered_jobs
        pages = max(1, (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._page = max(0, min(self._page, pages - 1))
        render_key = (filter_key, self._page, info.get("status"), preview, self._can_edit(), self.task is not None)
        # Inspect the visible page's values before deciding whether Tk needs
        # item mutations. A fresh durable view is immutable, but this also
        # keeps explicit caller edits and selection-only updates responsive.
        self._rendering = True
        try:
            old_selection = self.results_table.selection() or self._selected_ids
            current_focus = self.results_table.focus() or self._focus_id
            self._jobs_by_id = row_view["by_id"]
            kept_ids, pending_ids = row_view["kept_ids"], row_view["pending_ids"]
            manual_choices = info.get("manual_choices", {})
            shown = filtered[self._page * PAGE_SIZE:(self._page + 1) * PAGE_SIZE]
            self._page_ids = [job_id(job) for job in shown]
            children = self.results_table.get_children()
            wanted = set(self._page_ids)
            removed = [identity for identity in children if identity not in wanted]
            if removed:
                self.results_table.delete(*removed)
            stale = info.get("status") == "stale"
            new_values = {}
            for position, job in enumerate(shown):
                identity = job_id(job)
                kept = self._kept(job) if stage == "report" else identity in kept_ids
                company = desktop_data._company(job) or "公司未提供"
                title = _safe(job.get("title") or "未提供标题")
                if stale:
                    row_tag = "stale"
                elif stage == "report" and report_decision(job).get("classification") == "待核实":
                    row_tag = "pending_review"
                elif preview or stage in {"import", "fetch-jd", "report"}:
                    row_tag = "kept" if kept or preview else "excluded"
                elif stage == "llm" and identity in pending_ids:
                    row_tag = "pending_review"
                elif stage == "llm" and type(manual_choices.get(identity)) is bool:
                    row_tag = "kept" if kept else "excluded"
                else:
                    recommendation = job.get("recommended_keep")
                    if recommendation is None and stage == "shortlist":
                        recommendation = job.get("list_pass")
                    row_tag = ("suggest_keep" if recommendation is True else
                               "suggest_exclude" if recommendation is False else
                               "kept" if kept else "excluded")
                values = (("—" if preview or stage == "llm" and identity in pending_ids else "☑" if kept else "☐",
                           f"{title} · {_safe(company)}", _safe(job.get("salary") or "待核实"),
                           _safe(self._row_reason(job))), (row_tag,))
                if identity not in children:
                    self.results_table.insert("", position, iid=identity, values=values[0], tags=values[1])
                else:
                    if self._table_values.get(identity) != values:
                        self.results_table.item(identity, values=values[0], tags=values[1])
                    if position >= len(children) or children[position] != identity:
                        self.results_table.move(identity, "", position)
                new_values[identity] = values
            self._table_values = new_values
            selection = [identity for identity in old_selection if identity in wanted]
            self._focus_id = current_focus if current_focus in wanted else self._page_ids[0] if shown else None
            if not selection and self._focus_id:
                selection = [self._focus_id]
            self._selected_ids = tuple(selection)
            if tuple(self.results_table.selection()) != tuple(selection):
                self.results_table.selection_set(selection)
            if self._focus_id and self.results_table.focus() != self._focus_id:
                self.results_table.focus(self._focus_id)
            self.empty_label.place_forget() if shown else self.empty_label.place(relx=.5, rely=.5, anchor="center")
            self.empty_text.set("没有符合搜索或筛选的岗位。" if jobs else "尚未运行本步，或本步确认为空名单。" if self.task else "尚未导入；请先创建或打开一个任务。")
            self.page_text.set(f"{self._page * PAGE_SIZE + 1}–{self._page * PAGE_SIZE + len(shown)} / {len(filtered)} 岗" if shown else "0 个岗位")
            self._enabled(self.previous_page_button, self._page > 0)
            self._enabled(self.next_page_button, self._page < pages - 1)
            for label, button in self.filter_buttons.items():
                count = len(row_view["groups"].get(label, []))
                if label == "待核实":
                    if stage in {"llm", "report"}:
                        if not button.winfo_manager():
                            button.pack(side="left", padx=(0, 3), after=self.filter_buttons["剔除"])
                    else:
                        button.pack_forget()
                button.configure(text=f"{label} {count}", style="Review.Theme.TButton" if selected_filter == label else "Small.TButton")
            self._rendered_rows_key = render_key
            self._show_detail()
        finally:
            self._rendering = False

    @staticmethod
    def _replace_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", value)
        widget.configure(state="disabled")

    def _show_detail(self) -> None:
        job = self._jobs_by_id.get(self._focus_id or "")
        detail_key = (self.stage.get(), id(job), self.stage.get() in self._previews,
                      bool(job and self._kept(job)), self._can_edit(),
                      bool(job and job_id(job) in self._stage_info().get("pending_ids", [])),
                      self._stage_info().get("manual_choices", {}).get(job_id(job)) if job else None,
                      self._stage_info().get("confirmed"),
                      self._row_reason(job) if job else None,
                      job.get("ai_judgment") if job else None,
                      job.get("ai_reason") if job else None,
                      job.get("classification") if job else None)
        # Rendering the toolbar can temporarily disable this button before
        # the table restores its focus. Restore its state even when the
        # unchanged evidence text is already cached.
        self._enabled(self.detail_toggle_button, self._can_edit() and job is not None)
        if detail_key == self._detail_key:
            return
        self._detail_key = detail_key
        self.detail_title.set(_safe(job.get("title") or "未提供标题") if job else "选择一个岗位查看详情")
        if job and self.stage.get() == "shortlist" and "shortlist" in self._previews:
            evidence = "输入预览：尚未运行列表初筛，没有本步方向队列。\n\n" + _evidence_text(job)
        else:
            detail_job = report_decision(job) if job and self.stage.get() == "report" else job
            evidence = _evidence_text(detail_job, stage=self.stage.get()) if job else "依据、风险与人工审计将在这里显示。"
        if job and self.stage.get() == "shortlist" and "shortlist" not in self._previews:
            evidence = f"人工名单：{'保留' if self._kept(job) else '剔除'}（仅本任务后续输入）\n\n" + evidence
        if job and self.stage.get() == "llm" and "llm" not in self._previews:
            info = self._stage_info()
            identity = job_id(job)
            manual = info.get("manual_choices", {}).get(identity)
            selection = ("待核实" if identity in info.get("pending_ids", []) else
                         "保留" if self._kept(job) else "剔除")
            origin = "人工决定" if type(manual) is bool else "默认建议" if selection != "待核实" else "尚未人工处理"
            confirmation = "已确认" if info.get("confirmed") else "待确认"
            evidence = f"当前名单：{selection} · {origin} · {confirmation}\n人工决定只改变本任务名单；原始 AI 判断、理由与技术状态保留。\n\n" + evidence
        if job and self.stage.get() == "report":
            evidence = self._row_reason(job) + "\n\n" + evidence
        self._replace_text(self.detail_text, evidence)
        self._replace_text(self.jd_text, _safe(desktop_data._jd_text(job)) or "暂无 JD 原文；请运行 JD 补抓或人工核实。" if job else "未选择岗位。")
        self._replace_text(self.sources_text, _sources_text(job) if job else "未选择岗位。")
        if job:
            pending_ai = (self.stage.get() == "llm"
                          and job_id(job) in self._stage_info().get("pending_ids", []))
            self.detail_toggle_button.configure(text=("人工保留当前岗位" if pending_ai else
                                                       "剔除当前岗位" if self._kept(job) else "恢复当前岗位"))

    def _show_selected_job(self, _event: tk.Event | None = None) -> None:
        if self._rendering:
            return
        selection = self.results_table.selection()
        self._selected_ids = selection
        focus = self.results_table.focus()
        if focus in self._jobs_by_id:
            self._focus_id = focus
        elif selection:
            self._focus_id = selection[0]
        self._show_detail()

    def _table_click(self, event: tk.Event) -> str | None:
        row = self.results_table.identify_row(event.y)
        if row and self.results_table.identify_column(event.x) == "#1" and self.results_table.identify_region(event.x, event.y) == "cell":
            self._focus_id = row
            self.results_table.focus(row)
            self.results_table.selection_set(row)
            self._toggle_focused_keep()
            return "break"
        return None

    def _table_space(self, _event: tk.Event | None = None) -> str:
        self._focus_id = self.results_table.focus() or self._focus_id
        self._toggle_focused_keep()
        return "break"

    def _table_double_click(self, event: tk.Event) -> str | None:
        row = self.results_table.identify_row(event.y)
        if (row and self.results_table.identify_column(event.x) != "#1"
                and self.results_table.identify_region(event.x, event.y) == "cell"):
            self._focus_id = row
            self.results_table.focus(row)
            self.results_table.selection_set(row)
            self._toggle_focused_keep()
            return "break"
        return None

    def _debounce_filters(self, *_args: Any) -> None:
        self._cancel_after(self._filter_after_id)
        self._filter_after_id = self._after(120, self._apply_filters)

    def _apply_filters(self) -> None:
        self._filter_after_id = None
        self._page = 0
        self._render_rows()

    def _change_page(self, amount: int) -> None:
        self._page = max(0, self._page + amount)
        self._render_rows()

    def _focus_search(self, _event: tk.Event | None = None) -> str:
        if self.workspace.get() != "review":
            return "break"
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, "end")
        return "break"

    def _select_stage(self, stage: str) -> None:
        if stage not in REVIEW_STAGES or self._busy or self._closing:
            return
        index = STAGES.index(stage)
        if self.task and index and self._stage_info(stage).get("status") == "pending" and not self._stage_info(STAGES[index - 1]).get("confirmed"):
            self._notify("请先运行并确认上一步的名单。", warning=True)
            return
        self.stage.set(stage)
        self._page, self._focus_id, self._selected_ids = 0, None, ()
        self.search.set("")
        self.filter.set("全部")
        self._cancel_after(self._filter_after_id)
        self._filter_after_id = None
        stale = self._stage_info(stage).get("status") == "stale"
        if stale:
            notice = "上游名单已修改，旧结果仅供查看，不能确认或作为新报告。请重新确认上游并逐步更新。"
        elif stage == "report" and _report_ready(self.manifest) and "markdown" in self._artifacts:
            notice = "报告已生成，可点击下方“打开报告”查看 Markdown；查看不改变确认状态，也不会重新执行处理。"
        else:
            notice = f"正在查看「{STAGE_LABELS[stage]}」。尚未自动执行；点击运行本步才开始。"
        self._notify(notice, warning=stale)
        self._render()

    def _choose_source(self) -> None:
        path = filedialog.askdirectory(title="选择完整数据根（包含 raw/current、raw/target）", parent=self.root)
        if path:
            self.source_root.set(path)

    def _choose_parent(self) -> None:
        path = filedialog.askdirectory(title="选择任务保存位置（不选择原始 raw 目录）", parent=self.root)
        if path:
            self.task_parent.set(path)

    def _choose_scraper(self) -> None:
        path = filedialog.askdirectory(title="选择 boss-zhipin-scraper 工具目录", parent=self.root)
        if path:
            self.scraper_root.set(path)

    def _choose_task(self) -> None:
        if self._execution_blocked:
            return
        path = filedialog.askdirectory(title="显式选择已有具名任务目录", parent=self.root)
        if path:
            self._open_task(Path(path))

    def _create_task(self) -> None:
        if self._execution_blocked:
            return
        try:
            if not self.source_root.get().strip():
                raise ValueError("请先选择完整数据根。")
            name = self.task_name.get().strip()
            if not name:
                raise ValueError("请填写任务名。")
            if not self.task_parent.get().strip():
                raise ValueError("请选择任务保存位置。")
            mode = self.sample_mode.get()
            if mode not in SAMPLE_CHOICES:
                raise ValueError("请选择全部岗位（不限）或自定义数量。")
            size = None if mode == SAMPLE_CHOICES[0] else _positive(self.sample_size.get(), "自定义去重岗位数")
            source, parent = Path(self.source_root.get()).expanduser(), Path(self.task_parent.get()).expanduser()
            city = None if self.city.get() == "全部城市" else self.city.get()
            track = TRACK_CHOICES[self.track.get()]
            config = self.config_path
        except (ValueError, KeyError) as error:
            self._error("无法创建任务", error)
            return

        def create() -> dict[str, Any]:
            source_config = review_config_for_source(source, config, output_dir=REVIEW_SOURCE_CONFIGS)
            task = ReviewTask.create(source, parent, name, city=city, track=track, sample_size=size, config_path=source_config)
            self._write_log(task, f"创建任务：{name}；固定输入：{task.manifest.get('source_root')}\n")
            if source_config != config:
                self._write_log(task, "已合并该数据源保存的采集方案目录，保留旧词及新增词的来源映射；未重跑采集。\n")
            return {"task": task, "view": self._capture_view(task, read_log=True), "stage": "import",
                    "notice": "任务已创建，固定输入与范围已绑定。先运行导入，审核后再确认；现在尚未开始。"}

        self._submit("create", create, status="正在后台创建任务…")

    def _open_task(self, path: Path) -> None:
        def open_task() -> dict[str, Any]:
            task = ReviewTask.open(path)
            view = self._capture_view(task, read_log=True)
            stages = view["manifest"].get("stages", {})
            stage = next((key for key in REVIEW_STAGES if not stages.get(key, {}).get("confirmed") or stages.get(key, {}).get("status") != "completed"), "llm")
            return {"task": task, "view": view, "stage": stage,
                    "notice": "已打开原任务，未触发任何阶段执行。固定输入、范围和人工选择均沿用此任务。"}

        self._submit("open", open_task, status="正在后台打开任务…")

    def _new_task(self) -> None:
        if self._execution_blocked:
            return
        if self.task is not None and not messagebox.askyesno("新建任务 / 更改数量", "当前任务的范围和数量已固定。更改数量会另建任务；原任务和审核结果不会被覆盖。继续填写新任务吗？", parent=self.root):
            return
        self._close_review_rules_editor()
        self._saved_review_rules, self._default_review_rules = "", ""
        self.task, self.manifest = None, {}
        self._view_cache = TaskViewCache()
        self._rows = {key: [] for key in REVIEW_STAGES}
        self._shortlist_result_file = None
        self._search_index, self._artifacts, self._previews = {}, {}, set()
        self.stage.set("import")
        self.sample_mode.set(SAMPLE_CHOICES[0])
        self.sample_size.set("")
        self.search.set("")
        self.filter.set("全部")
        self.list_queue_filter.set(ALL_LIST_QUEUES)
        self._page, self._focus_id, self._selected_ids = 0, None, ()
        self._replace_text(self.log_view, "")
        self.delivery_page.set_current_task(None)
        self._notify("新任务默认处理全部岗位（不限），也可选择自定义数量。旧任务已保留，创建后仍需手动运行。")
        self._render()

    def _refresh_task(self) -> None:
        task = self.task
        if task is not None:
            self._submit("refresh", lambda: {"task": task, "view": self._capture_view(task)}, status="正在后台刷新落盘状态…")

    def _refresh_workspace(self, _event: tk.Event | None = None) -> str:
        if not self._closing:
            if self.workspace.get() == "collection":
                self.collection_page.refresh_progress()
            elif self.workspace.get() == "delivery":
                self.delivery_page.refresh_preview()
            else:
                self._refresh_task()
        return "break"

    def _set_keep(self, identities: list[str], keep: bool) -> None:
        if not self._can_edit() or not identities:
            return
        task, stage = self.task, self.stage.get()
        assert task is not None
        identities = list(dict.fromkeys(identities))
        expected_stage = copy.deepcopy(self._stage_info())

        def update() -> dict[str, Any]:
            task.set_keep(stage, identities, keep, expected_stage=expected_stage)
            self._write_log(task, f"人工{'恢复/保留' if keep else '剔除'}：{STAGE_LABELS[stage]}，{len(identities)} 个岗位；不修改原始数据。\n")
            view = self._capture_view(task)
            stale = [STAGE_LABELS[key] for key in STAGES if view["manifest"].get("stages", {}).get(key, {}).get("status") == "stale"]
            notice = "人工选择已保存。" + ("下游「" + "、".join(stale) + "」已失效，旧输出保留；请重新确认并逐步更新。" if stale else "只改变本任务名单，原始文件保持不变。")
            return {"task": task, "view": view, "notice": notice, "warning": bool(stale)}

        self._submit("selection", update, status="正在保存人工选择…")

    def _select_page(self) -> None:
        if self._can_edit() and self._page_ids:
            identities = list(self._page_ids)
            self.results_table.selection_set(identities)
            self._selected_ids = tuple(identities)
            self._set_keep(identities, True)

    def _set_selected_keep(self, keep: bool) -> None:
        identities = list(self.results_table.selection())
        if not identities and self._focus_id:
            identities = [self._focus_id]
        self._set_keep(identities, keep)

    def _toggle_focused_keep(self) -> None:
        job = self._jobs_by_id.get(self._focus_id or "")
        if job:
            self._set_keep([job_id(job)], not self._kept(job))

    def _apply_recommendations(self) -> None:
        if not self._can_edit():
            return
        task, stage = self.task, self.stage.get()
        assert task is not None

        def apply() -> dict[str, Any]:
            task.apply_recommendations(stage)
            self._write_log(task, f"人工明确采用本步建议：{STAGE_LABELS[stage]}。\n")
            view = self._capture_view(task)
            stale = any(info.get("status") == "stale" for info in view["manifest"].get("stages", {}).values())
            return {"task": task, "view": view, "notice": "已明确采用建议并保存名单。" + ("下游旧结果已失效，需要重新确认、逐步更新。" if stale else "请审核后确认。"), "warning": stale}

        self._submit("recommendations", apply, status="正在保存建议选择…")

    def _keep_relevant_only(self) -> None:
        self._choose_list_queues(relevant_only=True)

    def _add_verify_queue(self) -> None:
        self._choose_list_queues(relevant_only=False)

    def _choose_list_queues(self, *, relevant_only: bool) -> None:
        """An explicit, full-result manual selection; never execute or confirm."""
        if not self._can_edit_list_queues():
            return
        task = self.task
        assert task is not None
        jobs = self._rows["shortlist"]
        counts, _legacy = _list_queue_summary(jobs)
        if not relevant_only and not counts["verify"]:
            return
        expected_stage = copy.deepcopy(self._stage_info("shortlist"))
        version = stage_selection_version(expected_stage)
        expected_queues = tuple((job_id(job), job.get("list_queue")) for job in jobs)
        if relevant_only:
            title = "替换为仅保留明确相关"
            question = (f"将按本步完整结果（{len(jobs)} 岗）替换人工选择范围：\n"
                        f"明确相关 {counts['relevant']} 岗全部保留；待核实 {counts['verify']} 岗和剔除 {counts['excluded']} 岗全部移出本次后续输入。\n\n"
                        "这会覆盖上述岗位此前的人工保留 / 剔除，不受当前搜索、筛选或分页影响。"
                        "明确相关仍须 JD 核实，不代表最终可投。\n"
                        "本操作只保存名单，不确认、不补抓；已有下游结果可能失效。继续吗？")
        else:
            title = "将完整待核实队列加入保留"
            question = (f"将本步完整待核实队列 {counts['verify']} 岗加入保留，不受当前搜索、筛选或分页影响。\n\n"
                        "这会将该队列中人工剔除的岗位也恢复保留；其他队列的人工取舍保持不变。"
                        "待核实不代表已经相关或可投。\n"
                        "本操作只保存名单，不确认、不补抓；已有下游结果可能失效。继续吗？")
        if not messagebox.askyesno(title, question, parent=self.root):
            return

        def update() -> dict[str, Any]:
            before = self._capture_view(task)
            info = before["manifest"]["stages"]["shortlist"]
            full = before["rows"]["shortlist"]
            if (stage_selection_version(info) != version or info.get("status") != "completed"
                    or "shortlist" in before["previews"]
                    or any(_saved_list_queue(job)[1] for job in full)
                    or tuple((job_id(job), job.get("list_queue")) for job in full) != expected_queues):
                raise TaskStateError("列表结果或人工选择已变化，请刷新并重新审核后再执行队列选择。")
            if relevant_only:
                keep = [job_id(job) for job in full if _saved_list_queue(job)[0] == "relevant"]
                task.set_keep("shortlist", keep, True, replace_selection=True, expected_stage=expected_stage)
                action = f"仅保留完整明确相关队列 {len(keep)} 岗"
            else:
                keep = [job_id(job) for job in full if _saved_list_queue(job)[0] == "verify"]
                task.set_keep("shortlist", keep, True, expected_stage=expected_stage)
                action = f"将完整待核实队列 {len(keep)} 岗加入保留，其他队列取舍不变"
            self._write_log(task, "人工队列选择：" + action + "；未确认或补抓，不修改原始数据。\n")
            view = self._capture_view(task)
            # A different window can legitimately edit again after our one
            # atomic commit. Display that later choice, never retry over it or
            # advertise the queue preset as the still-current saved scope.
            projected = copy.deepcopy(expected_stage)
            selected = set(keep) if relevant_only else set(expected_stage["keep_ids"]) | set(keep)
            projected["keep_ids"] = [key for key in projected["result_ids"] if key in selected]
            projected["excluded_ids"] = [key for key in projected["result_ids"] if key not in selected]
            if relevant_only:
                projected["manual_choices"].update({key: key in selected for key in projected["result_ids"]})
            else:
                projected["manual_choices"].update({key: True for key in keep})
            if projected["keep_ids"] != expected_stage["keep_ids"]:
                projected.update(confirmed=False, confirmed_revision=None, revision=expected_stage["revision"] + 1)
            if stage_selection_version(view["manifest"]["stages"]["shortlist"]) != stage_selection_version(projected):
                notice = "名单在保存后又被其他窗口修改；已刷新实际结果，请重新审核。未覆盖后续修改，也未确认或补抓 JD。"
                self._write_log(task, notice + "\n")
                return {"task": task, "view": view, "notice": notice, "warning": True}
            stale = any(item.get("status") == "stale" for item in view["manifest"]["stages"].values())
            notice = action + "，已保存。仍需审核并人工确认，尚未补抓 JD。"
            if stale:
                notice += "下游旧结果已失效；请重新确认并逐步更新。"
            return {"task": task, "view": view, "notice": notice, "warning": stale}

        self._submit("queue-selection", update, status="正在保存完整队列的人工选择…")

    def _confirm_stage(self) -> None:
        if not self._can_confirm():
            self._notify("本步须完成且上游确认有效，才能确认名单；失效结果请先更新。", warning=True)
            return
        task, stage = self.task, self.stage.get()
        assert task is not None
        expected_stage = copy.deepcopy(self._stage_info())
        keep = expected_stage.get("keep_ids", [])
        if not keep and not messagebox.askyesno("明确确认空名单", "本步保留 0 个岗位。后续不会发起 JD 或模型请求；投递导出将没有可投岗位。确认继续空名单吗？", parent=self.root):
            return

        def confirm() -> dict[str, Any]:
            retained = task.confirm(stage, expected_stage=expected_stage)
            self._write_log(task, f"确认名单：{STAGE_LABELS[stage]}，保留 {len(retained)} 个；未调用下一步执行器。\n")
            index = REVIEW_STAGES.index(stage)
            next_stage = REVIEW_STAGES[index + 1] if index < len(REVIEW_STAGES) - 1 else stage
            notice = (f"已确认最终保留 {len(retained)} 个。点击“前往 03 投递数据导出”预览，再显式导出 JSON。"
                      if stage == "llm" else
                      f"已确认 {len(retained)} 个，载入「{STAGE_LABELS[next_stage]}」。尚未运行；点击运行本步才执行。")
            return {"task": task, "view": self._capture_view(task), "stage": next_stage, "notice": notice}

        # This path intentionally has no reference to self.executor.
        self._submit("confirm", confirm, status="正在冻结本步名单…")

    def _reset_stage(self) -> None:
        if not self._can_reset_stage():
            return
        stage = self.stage.get()
        message = (f"重置「{STAGE_LABELS[stage]}」？\n\n"
                   "确认后只清除本阶段已生成的结果、检查点、进度与阶段状态，并将已执行的下游阶段标为失效；"
                   "上游结果与确认保持不变。需要保留的结果请在重置前先查看。\n"
                   "重置不会立即重新筛选、抓取 JD、请求 AI 或生成报告，也不会确认或启动下一阶段。"
                   "完成后本步回到待运行状态；需要重做时，请再单独点击“运行本步”。")
        if not messagebox.askyesno("重置本阶段", message, parent=self.root):
            return
        task = self.task
        assert task is not None

        def reset() -> dict[str, Any]:
            reset_review_stage(task, stage)
            self._write_log(task, f"已重置本阶段：{STAGE_LABELS[stage]}；未运行阶段执行器。\n")
            return {"task": task, "view": self._capture_view(task), "stage": stage,
                    "notice": f"「{STAGE_LABELS[stage]}」已重置，未开始执行。需要时点击“运行本步”重新运行。"}

        self._submit("reset-stage", reset, status=f"正在重置：{STAGE_LABELS[stage]}…")

    def _run_stage(self) -> None:
        if self._delivery_busy:
            self._notify("投递导出页正在处理，请完成后再执行后处理；可切换页面查看进度。", warning=True)
            return
        if self._collection_busy:
            self._notify("岗位采集正在运行，请先在采集页暂停或停止，再执行后处理；两者不会同时操作数据源。", warning=True)
            return
        if not self._can_run():
            self._notify("请先运行并确认所有前置步骤；上游已修改时须重新确认。", warning=True)
            return
        try:
            workers = _positive(self.workers.get(), "AI 请求并发", maximum=MAX_WORKERS)
            jd_workers = _positive(self.jd_workers.get(), "JD 页面并发", maximum=MAX_WORKERS)
            batch_size = _positive(self.batch_size.get(), "JD 每批岗位数", maximum=100)
            # Validate browser settings when JD is actually being fetched;
            # file-only/report stages keep their historical input behavior.
            browser_options = self._browser_options(validate=self.stage.get() == "fetch-jd")
            scraper = Path(self.scraper_root.get()).expanduser() if self.scraper_root.get().strip() else None
        except ValueError as error:
            self._error("执行设置无效", error)
            return
        task, stage = self.task, self.stage.get()
        assert task is not None
        self._remember_execution_settings()
        if stage == "llm":
            self._live_ai_checkpoint_version = None
        control = StageControl()
        self._control = control
        if stage == "fetch-jd":
            self._set_browser_statuses(fallback="running")
        self._remember_browser_settings()
        token = self._operation_id + 1

        def run() -> dict[str, Any]:
            self._write_log(task, f"\n开始本步（续用有效检查点）：{STAGE_LABELS[stage]}；仅执行此步，不自动确认或串行运行下步。\n")
            if stage == "fetch-jd":
                mode = browser_options["browser_mode"]
                self._write_log(task, (f"本次浏览器：Chrome + Edge 各半（dual）；每侧最多 {jd_workers} 页，总计最多 {jd_workers * 2} 页。\n"
                                      if mode == "dual" else
                                      f"本次浏览器：仅 Chrome（single）；最多 {jd_workers} 页；如需两侧采集请暂停后切换浏览器模式。\n"))
            last_refresh = 0.0

            def on_event(kind: str, value: Any) -> None:
                nonlocal last_refresh
                if kind == "log":
                    self._write_log(task, _safe(value))
                    return
                browser_statuses = _browser_result_map(value)
                if browser_statuses:
                    self.events.put(("browser-status", token, browser_statuses))
                if kind in ("progress", "waiting", "finished"):
                    now = time.monotonic()
                    if kind != "progress" or now - last_refresh >= .15:
                        if stage == "llm":
                            # The callback never performs a view read. Tk
                            # coalesces requests with its checkpoint fallback.
                            self.events.put(("refresh-request", token, None))
                        else:
                            self.events.put(("manifest", token, task.snapshot()))
                        last_refresh = now
                if kind == "waiting":
                    self.events.put(("waiting", token, _safe(value)))

            execution_options = {
                "scraper_root": scraper, "workers": workers, "jd_workers": jd_workers,
                "batch_size": batch_size, "control": control, "on_event": on_event,
                **browser_options,
            }
            self.executor(task, stage, **_supported_kwargs(self.executor, execution_options))
            view = self._capture_view(task)
            info = view["manifest"].get("stages", {}).get(stage, {})
            status = info.get("status")
            if status == "completed":
                notice = ("报告已生成，点击下方“打开报告”即可查看 Markdown，无需先确认；查看后可点击“确认报告完成”。"
                          if stage == "report" and "markdown" in view["artifacts"] else
                          "本步已完成，已停在审核处。请检查依据与名单，再点击确认；下一步不会自动执行。")
            elif status == "waiting":
                notice = "已保存检查点并停住。请在专用浏览器完成人工登录或安全验证，再点击“人工处理完成，继续”；不会确认或跳步。"
            elif status in ("paused", "stopped"):
                notice = "本步已暂停 / 停止并保留检查点。点击“继续本步”只恢复当前步骤。"
            else:
                notice = "本步未完成，不能确认。请查看真实日志与问题后重试。"
            browser_statuses = _browser_result_map(info.get("stats"))
            if stage == "fetch-jd" and not browser_statuses:
                browser_statuses = {
                    browser: ("completed" if status == "completed" else status or "failed")
                    for browser in ("chrome", "edge")
                    if browser == "chrome" or browser_options.get("browser_mode") == "dual"
                }
            return {"task": task, "view": view, "notice": notice, "warning": status != "completed",
                    "browser_statuses": browser_statuses}

        self._submit("execute", run, status=f"正在运行：{STAGE_LABELS[stage]}…")

    def _pause_stage(self) -> None:
        if self.workflow_running and self._control is not None:
            self._control.pause()
            self.status_text.set("正在安全暂停，保留检查点…")
            self._notify("已请求暂停；等待当前操作安全退出后，可继续本步。")
            self._enabled(self.pause_button, False)

    def _stop_stage(self) -> None:
        if self.workflow_running and self._control is not None:
            self._control.stop()
            self.status_text.set("正在停止并保存检查点…")
            self._notify("已请求停止，不删除原始数据或本任务检查点。")
            self._enabled(self.pause_button, False)
            self._enabled(self.stop_button, False)

    def _continue_stage(self) -> None:
        if self._stage_info().get("status") == "waiting" and not self._busy:
            self._run_stage()

    def _prepare_browser(self) -> None:
        if self.task is None or self._execution_blocked:
            return
        if not self.scraper_root.get().strip():
            self._toggle_settings() if not self.settings_frame.winfo_manager() else None
            self._notify("请在执行设置中选择上游 boss-zhipin-scraper 目录，再主动准备浏览器。", warning=True)
            return
        task, scraper = self.task, Path(self.scraper_root.get()).expanduser()
        try:
            browser_options = self._browser_options()
        except ValueError as error:
            self._error("浏览器设置无效", error)
            return
        self._remember_browser_settings()
        self._set_browser_statuses(fallback="running")
        control = StageControl()
        self._control = control

        def prepare() -> dict[str, Any]:
            self._write_log(task, "用户主动准备登录浏览器；不执行任何审核步骤。\n")
            options: dict[str, Any] = {"on_event": lambda kind, value: self._write_log(task, _safe(value)) if kind == "log" else None}
            options.update(browser_options, control=control)
            ready = self.browser_preparer(scraper, **_supported_kwargs(self.browser_preparer, options))
            statuses = _browser_result_map(ready)
            ready_flag = (ready if isinstance(ready, bool) else
                          (all(status == "completed" for status in statuses.values()) if statuses
                           else _browser_status(ready) == "completed"))
            if not statuses:
                statuses = {browser: ("completed" if ready_flag else "failed")
                             for browser in ("chrome", "edge")
                             if browser == "chrome" or browser_options.get("browser_mode") == "dual"}
            return {"task": task, "view": self._capture_view(task), "notice": "专用浏览器与 CDP 已准备；请在打开的窗口确认 BOSS 登录，再主动运行当前步骤。" if ready_flag else "浏览器窗口或 CDP 未准备完成；请查看日志中的具体错误，任务名单没有改变。", "warning": not ready_flag,
                    "browser_statuses": statuses}

        self._submit("browser", prepare, status="正在后台准备登录浏览器…")

    def _open_collector(self) -> None:
        if self._closing:
            return
        self._show_workspace("collection")
        if self.collector_opener is not None:
            self.collector_opener()

    def _write_log(self, task: ReviewTask, value: str) -> None:
        safe = _safe(value)
        if not safe.endswith("\n"):
            safe += "\n"
        try:
            with self._log_lock:
                path = _confined_path(task, "desktop.log", allow_missing=True)
                if path is None:
                    raise OSError("任务日志路径不安全")
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(safe)
        except OSError as error:
            safe += "[日志文件暂无法写入：" + _safe(type(error).__name__) + "]\n"
        self.events.put(("log", self._operation_id, safe))

    def _append_log(self, value: str) -> None:
        # Keep the user's scroll position while inspecting earlier real logs.
        at_end = self.log_view.yview()[1] >= .98
        self.log_view.configure(state="normal")
        self.log_view.insert("end", _safe(value))
        size = int(self.log_view.count("1.0", "end-1c", "chars")[0] or 0)
        if size > MAX_LOG_CHARS:
            self.log_view.delete("1.0", f"1.0+{size - MAX_LOG_CHARS}c")
        self.log_view.configure(state="disabled")
        if at_end:
            self.log_view.see("end")

    def _submit(self, operation: str, function: Callable[[], dict[str, Any]], *, status: str) -> None:
        if self._execution_blocked:
            return
        self._busy, self._operation = True, operation
        self._operation_id += 1
        token = self._operation_id
        task = self.task
        self.status_text.set(status)
        self._render(render_rows=False)

        def work() -> None:
            try:
                result = function()
            except Exception as error:
                result = {"error": _safe(error), "operation": operation}
                if task is not None:
                    self._write_log(task, f"{operation} 未完成：{type(error).__name__}：{_safe(error)}\n")
                    try:
                        result.update(task=task, view=self._capture_view(task))
                    except Exception:
                        pass
            self.events.put(("done", token, result))

        self.worker = threading.Thread(target=work, name=f"review-{operation}", daemon=True)
        self.worker.start()

    def _capture_view(self, task: ReviewTask, *, read_log: bool = False) -> dict[str, Any]:
        # Completion, explicit refresh, callbacks and checkpoint polling share
        # one serial cache. No independent cold reader can overtake this one.
        with self._view_lock:
            view = _capture_task(task, read_log=read_log, cache=self._view_cache, include_report=False)
            criteria = self._view_filter
            if criteria is not None:
                stage, query, selection, selected_queue = criteria
                row_view, search = view["row_views"][stage], view["search"][stage]
                key = (stage, id(row_view), id(search), query, selection, selected_queue)
                if self._filter_cache is None or self._filter_cache[0] != key:
                    candidates = row_view["groups"].get(selection, row_view["groups"]["全部"])
                    filtered = (candidates if not query and selected_queue is None else
                                [job for job in candidates if (not query or query in search.get(job_id(job), ""))
                                 and (selected_queue is None or _saved_list_queue(job)[0] == selected_queue)])
                    self._filter_cache = (key, filtered)
                view["filtered_view"] = self._filter_cache
            self._capture_sequence += 1
            view["capture_sequence"] = self._capture_sequence
            return view

    def _apply_view(self, task: ReviewTask, view: dict[str, Any]) -> bool:
        incoming_manifest = view.get("manifest", {})
        sequence = view.get("capture_sequence")
        same_task = (self.task is not None and self.task.path == task.path
                     and self.manifest.get("task_id") == incoming_manifest.get("task_id"))
        if same_task:
            if type(sequence) is int and sequence < self._applied_capture_sequence:
                return False
            if incoming_manifest.get("revision", 0) < self.manifest.get("revision", 0):
                return False
        if type(sequence) is int:
            self._applied_capture_sequence = sequence
        self._live_ai_checkpoint_version = view.get("live_ai_checkpoint_version")
        task_changed = self.task is None or (self.task.path != task.path
                                            or self.manifest.get("task_id") != view["manifest"].get("task_id"))
        old_shortlist = self._shortlist_result_file
        if task_changed:
            self._close_review_rules_editor()
        new_shortlist = view["manifest"].get("stages", {}).get("shortlist", {}).get("result_file")
        if task_changed or (new_shortlist is not None and old_shortlist != new_shortlist):
            self.list_queue_filter.set(ALL_LIST_QUEUES)
            self._cancel_after(self._filter_after_id)
            self._filter_after_id = None
            self._page, self._focus_id, self._selected_ids = 0, None, ()
        self.task, self.manifest = task, view["manifest"]
        self.delivery_page.set_current_task(task.path, self.manifest.get("revision"))
        self._saved_review_rules = view.get("review_rules", "")
        self._default_review_rules = view.get("default_review_rules", "")
        self._shortlist_result_file = new_shortlist
        self._rows = view["rows"]
        self._row_views = view.get("row_views", {})
        self._prepared_filter = view.get("filtered_view")
        self._search_index, self._previews = view["search"], view["previews"]
        self._artifacts = view["artifacts"]
        self.source_root.set(str(self.manifest.get("source_root", "")))
        self.task_name.set(str(self.manifest.get("name", "")))
        self.task_parent.set(str(task.path.parent))
        self.city.set(self.manifest.get("city") or "全部城市")
        self.track.set(next((label for label, value in TRACK_CHOICES.items() if value == self.manifest.get("track")), "当前＋成长"))
        self.sample_mode.set(SAMPLE_CHOICES[1] if self.manifest.get("sample_size") is not None else SAMPLE_CHOICES[0])
        self.sample_size.set(str(self.manifest["sample_size"]) if self.manifest.get("sample_size") is not None else "")
        if self.manifest.get("config_path"):
            self.config_path = Path(self.manifest["config_path"])
        if "log" in view:
            self._replace_text(self.log_view, view["log"])
            self.log_view.see("end")
        return True

    def _poll_events(self) -> None:
        self._poll_after_id = None
        if self._closing or self._closed:
            return
        needs_render = False
        needs_rows = False
        needs_refresh = False
        log_parts = []
        try:
            for _ in range(120):
                kind, token, value = self.events.get_nowait()
                if kind == "preference_error":
                    self._append_log("主题已切换，但偏好无法保存：" + value + "\n")
                    continue
                if token != self._operation_id:
                    continue
                if kind == "log":
                    log_parts.append(value)
                elif kind == "refresh-request":
                    needs_refresh = True
                elif kind == "manifest":
                    if value.get("revision", 0) >= self.manifest.get("revision", 0):
                        self.manifest = value
                        needs_render = True
                elif kind == "live-view":
                    # A final ``done`` view is authoritative.  A queued
                    # fallback refresh that arrives after it must not put the
                    # old running snapshot back on screen.
                    task, view = value
                    if self.task is None or self.task.path != task.path:
                        continue
                    if self._apply_view(task, view):
                        needs_render = needs_rows = True
                elif kind == "live-refresh-error":
                    # Checkpoint replacement is intentionally atomic; a
                    # reader can briefly observe the old file between the
                    # replace operations.  Keep the progress text and retry
                    # on the next tick without surfacing a false failure.
                    continue
                elif kind == "waiting":
                    self._notify(value + "；本步退出后可人工处理并继续。", warning=True)
                elif kind == "browser-status":
                    self._set_browser_statuses(value)
                elif kind == "done":
                    self._busy, self._operation = False, ""
                    self._control = None
                    if value.get("error"):
                        # A failed read must not leave a cached report looking
                        # newly valid.  A successful refresh can re-enable it.
                        self._artifacts = {key: path for key, path in self._artifacts.items() if key == "log"}
                    if value.get("view") is not None:
                        self._apply_view(value["task"], value["view"])
                    if value.get("close_rules_editor"):
                        self._close_review_rules_editor()
                    if value.get("browser_statuses"):
                        self._set_browser_statuses(value["browser_statuses"])
                    if value.get("stage"):
                        self.stage.set(value["stage"])
                        self.search.set("")
                        self.filter.set("全部")
                        self._cancel_after(self._filter_after_id)
                        self._filter_after_id = None
                        self._page, self._focus_id, self._selected_ids = 0, None, ()
                    needs_render = needs_rows = True
                    if value.get("error"):
                        if self._review_rules_status is not None:
                            self._review_rules_status.set("保存未完成，编辑内容已保留。" + value["error"])
                        self._notify("操作未完成：" + value["error"], warning=True)
                        self._error("操作未完成", value["error"])
                    elif value.get("notice"):
                        self._notify(value["notice"], warning=value.get("warning", False))
                    if value.get("open_path"):
                        self._open_path(value["open_path"])
        except queue.Empty:
            pass
        if log_parts:
            self._append_log("".join(log_parts))
        if needs_render:
            self._render(render_rows=needs_rows)
        if needs_refresh:
            self._schedule_live_refresh()
        self._schedule_poll()

    def _schedule_live_refresh(self) -> None:
        """Refresh a running AI queue from its durable checkpoint.

        The executor emits progress events, but those events are best-effort
        UI notifications.  The model worker writes ``llm_reviews.json``
        independently for every completed job, so polling that file gives the
        UI a reliable source of truth even when no progress callback arrives.
        This is deliberately limited to the AI stage and one in-flight read.
        """
        observing_ai = self._stage_info("llm").get("status") == "running"
        executing_ai = self.workflow_running and self.stage.get() == "llm"
        if (self._closing or self._closed or not (observing_ai or executing_ai)
                or self._live_refresh_busy or self._busy and not executing_ai):
            return
        token = self._operation_id
        task = self.task
        if task is None:
            return
        self._live_refresh_busy = True

        def read_view() -> None:
            try:
                view = self._capture_view(task)
                self.events.put(("live-view", token, (task, view)))
            except Exception as error:
                # A transient half-written checkpoint is expected while the
                # child is replacing the file.  The next poll retries it; the
                # durable progress event remains visible meanwhile.
                self.events.put(("live-refresh-error", token, type(error).__name__))
            finally:
                self._live_refresh_busy = False

        self._live_refresh_worker = threading.Thread(target=read_view, name="review-live-checkpoint", daemon=True)
        self._live_refresh_worker.start()

    def _schedule_live_refresh_tick(self) -> None:
        if self._closing or self._closed:
            return
        self._live_refresh_after_id = self._after(500, self._run_live_refresh_tick)

    def _run_live_refresh_tick(self) -> None:
        self._live_refresh_after_id = None
        self._schedule_live_refresh()
        if not self._closing and not self._closed:
            self._schedule_live_refresh_tick()

    def _error(self, title: str, error: Any) -> None:
        messagebox.showerror(title, _safe(error), parent=self.root)

    def _open_location(self, kind: str) -> None:
        task = self.task
        if task is None or self._busy:
            return
        if kind in ("json", "markdown", "pool") and not _report_ready(self.manifest):
            self._notify("报告尚未完成或已失效；旧报告不能当作本次新结果。请逐步更新后重新生成。", warning=True)
            return

        def locate() -> dict[str, Any]:
            view = self._capture_view(task)
            if kind == "task":
                path = task.path
            elif kind == "source":
                path = Path(view["manifest"]["source_root"])
            else:
                if kind in ("json", "markdown", "pool") and not _report_ready(view["manifest"]):
                    raise TaskStateError("报告已失效，不能当作本次新报告打开。")
                path = view["artifacts"].get(kind)
                if path is None:
                    raise TaskStateError("该文件尚未生成，或路径不属于当前任务。")
            return {"task": task, "view": view, "open_path": path}

        self._submit("locate", locate, status="正在定位本任务文件…")

    def _open_path(self, path: Path) -> None:
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as error:
            self._error("无法打开文件", error)

    def _after(self, milliseconds: int, callback: Callable[[], None]) -> str | None:
        if self._closed:
            return None
        identifier: str

        def invoke() -> None:
            self._after_ids.discard(identifier)
            # Remove the Tcl command before invoking a callback that may
            # destroy/detach the root. Tk.after normally does this afterwards,
            # when the lifecycle manager has already released root.tk.
            self.root.deletecommand(command)
            if not self._closed:
                callback()

        command = self.root.register(invoke)
        identifier = str(self.root.tk.call("after", milliseconds, command))
        self._after_ids.add(identifier)
        return identifier

    def _cancel_after(self, identifier: str | None) -> None:
        if identifier is not None:
            try:
                self.root.after_cancel(identifier)
            except tk.TclError:
                pass
            self._after_ids.discard(identifier)

    def _schedule_poll(self) -> None:
        if not self._closing and not self._closed:
            self._poll_after_id = self._after(POLL_MS, self._poll_events)

    def _close(self) -> None:
        if self._closing or self._closed:
            return
        if (self._busy or self._collection_busy or self._delivery_busy) and not messagebox.askyesno("正在处理任务", "等待后台安全结束并关闭？采集与审核将保存检查点，已开始的导出会完成当前操作。", parent=self.root):
            return
        self._closing = True
        if self._control is not None:
            self._control.stop()
        self.collection_page.request_close()
        self.delivery_page.request_close()
        for identifier in tuple(self._after_ids):
            self._cancel_after(identifier)
        self._poll_after_id, self._live_refresh_after_id, self._filter_after_id = None, None, None
        self.status_text.set("正在等待后台安全退出并关闭…")
        self._render(render_rows=False)
        self._finish_close()

    def _finish_close(self) -> None:
        active = self.worker is not None and self.worker.is_alive()
        preferences = any(thread.is_alive() for thread in self._preference_workers)
        live_refresh = self._live_refresh_worker is not None and self._live_refresh_worker.is_alive()
        if active or live_refresh or preferences or not self.collection_page.shutdown_complete or not self.delivery_page.shutdown_complete:
            self._after(50, self._finish_close)
            return
        for identifier in tuple(self._after_ids):
            self._cancel_after(identifier)
        self._closed = True
        self.root.destroy()
