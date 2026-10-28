#!/usr/bin/env python3
"""Desktop launcher and the explicit ``--legacy-workbench`` workflow.

The default entry opens ReviewDesktopApp; the legacy runner remains available
for existing CLI flags, routed checkpoints and helper-function consumers.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk  # ttk is also a legacy public re-export.

import collect_architecture as collector
import process_architecture as processor
# Preserve the legacy public helper names while sharing the desktop sanitizer.
from desktop_data import _redact as redact_json, _redact_text
from desktop_routing import (
    AUTO_RUN_CHOICE, NEW_RUN_CHOICE, RoutePlan, canonical_city, canonical_track,
    copy_route_inputs, load_fixed_sample, resolve_daily_root, resolve_route, same_path,
)
from desktop_ui import WorkspaceMixin, format_count, load_preferences
from desktop_tk_lifecycle import StringVar, install_tk_lifecycle
from project_resources import DEFAULT_CONFIG, PROJECT_ROOT, default_state_root
from screening_policy import AI_DEFAULT_MODEL, AI_DEFAULT_REASONING_EFFORT


CITY_CHOICES = ("全部城市", "上海", "深圳", "杭州", "广州", "成都")
TRACK_CHOICES = ("当前+成长", "当前岗位", "成长目标")
STAGE_CHOICES = (
    "完整流程（采集+后处理）",
    "仅采集",
    "列表前处理",
    "JD补抓",
    "Codex语义复核",
    "生成报告",
    "后处理全流程（已有数据）",
)
MAX_LOG_CHARS = 250_000
STOP_GRACE_MS = 5_000
MAX_CHILD_LINE_CHARS = 1_000_000
LOG_BATCH_CHARS = 64 * 1024
LOG_FLUSH_SECONDS = .2
LOG_WRITE_WARNING = "日志文件写入失败；部分日志可能未保存，请检查磁盘空间与目录权限。\n"
POST_PROCESS_STAGES = (
    ("列表前处理", "shortlist"),
    ("JD补抓", "fetch-jd"),
    ("Codex语义复核", "llm"),
    ("生成报告", "report"),
)

_SECRET_OPTION_RE = re.compile(r"(?i)^--?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|authorization|password|passwd|client[_-]?secret|secret)$")


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def safe_component(value: str) -> str:
    normalized = re.sub(r"[^\w-]+", "-", value.strip()).strip("-")
    return normalized or "all"


def redact_secrets(value: str) -> str:
    """Share sanitization while retaining non-secret context in legacy logs."""
    return _redact_text(value, whole_header_lines=False)


def redact_command(command: list[str]) -> list[str]:
    output: list[str] = []
    redact_next = False
    for raw_part in command:
        part = str(raw_part)
        if redact_next and not part.startswith("-"):
            output.append("[REDACTED]")
            redact_next = False
            continue
        output.append(redact_secrets(part))
        redact_next = bool(_SECRET_OPTION_RE.fullmatch(part))
    return output


def parse_optional_positive_integer(value: str, label: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError as error:
        raise ValueError(f"{label}必须为正整数或留空。") from error
    if parsed < 1:
        raise ValueError(f"{label}必须为正整数或留空。")
    return parsed


def parse_progress(line: str) -> str | None:
    """Recognize collector totals and the processor's JD/batch progress formats."""

    patterns = (
        r"进度\s*[:：]\s*(\d+)\s*/\s*(\d+)",
        r"\[JD\s*总进度\]\s*(\d+)\s*/\s*(\d+)",
        r"\[批次\s*(\d+)\s*/\s*(\d+)\]",
    )
    for pattern in patterns:
        match = re.search(pattern, line, re.IGNORECASE)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return None


def load_run_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def stage_artifacts_exist(label: str, source_root: Path, output_root: Path) -> bool:
    """Only skip a successful stage when its durable checkpoint still exists."""

    artifacts = {
        "岗位采集": (source_root / "collection_manifest.json",),
        "列表前处理": (
            output_root / "prepared" / "merged_jobs_index.json",
            output_root / "prepared" / "jd_shortlists" / "jd_input.json",
        ),
        "JD补抓": (output_root / "prepared" / "jd_shortlists" / "details_manifest.json",),
        "Codex语义复核": (
            output_root / "prepared" / "llm_manifest.json",
            output_root / "prepared" / "llm_reviews.json",
        ),
        "生成报告": (
            output_root / "prepared" / "hai_tou_pool.json",
            output_root / "prepared" / "海投分层清单.md",
        ),
    }
    required = artifacts.get(label)
    return bool(required and all(path.is_file() for path in required))


def planned_stages(stage_name: str) -> list[str]:
    """Return workflow stages in the order used by the desktop runner."""

    mapping = {
        "仅采集": ["岗位采集"],
        "列表前处理": ["列表前处理"],
        "JD补抓": ["JD补抓"],
        "Codex语义复核": ["Codex语义复核"],
        "生成报告": ["生成报告"],
        "后处理全流程（已有数据）": [label for label, _ in POST_PROCESS_STAGES],
        "完整流程（采集+后处理）": ["岗位采集", *(label for label, _ in POST_PROCESS_STAGES)],
    }
    return list(mapping.get(stage_name, []))


def invalidate_downstream_stages(state: dict[str, Any], upstream: str) -> None:
    """Keep job checkpoints, but stop treating older derived stages as current."""
    labels = ["岗位采集", *(name for name, _ in POST_PROCESS_STAGES)]
    if upstream not in labels:
        return
    stages = state.setdefault("stages", {})
    for label in labels[labels.index(upstream) + 1:]:
        entry = stages.get(label)
        if isinstance(entry, dict) and entry.get("status") in ("success", "partial", "stale"):
            entry.update(status="stale", invalidated_by=upstream)


def run_settings_match(manifest: dict[str, Any], options: dict[str, Any]) -> bool:
    """Avoid skipping checkpoints when the selected scope changed on resume."""

    comparisons = {
        "combination_limit": options["limit"],
        "pages_per_combination": options["pages"],
        "sample_size": options["sample"],
        "batch_size": options["batch_size"],
    }
    return (
        same_path(manifest.get("source_root"), options["root"])
        and "city" in manifest and canonical_city(manifest["city"]) == canonical_city(options["city"])
        and "track" in manifest and canonical_track(manifest["track"]) == canonical_track(options["track"])
        and all(key in manifest and manifest.get(key) == value for key, value in comparisons.items())
    )


def jd_stage_checkpoint_complete(output_root: Path) -> bool:
    """Return true only when every expected JD batch has a successful checkpoint."""

    shortlist_path = output_root / "prepared" / "jd_shortlists" / "jd_input.json"
    manifest_path = output_root / "prepared" / "jd_shortlists" / "details_manifest.json"
    shortlist = load_run_manifest(shortlist_path)
    manifest = load_run_manifest(manifest_path)
    jobs = shortlist.get("jobs")
    records = manifest.get("records")
    if not isinstance(jobs, list) or not isinstance(records, list):
        return False
    total_jobs = len(jobs)
    if manifest.get("total_jobs") != total_jobs:
        return False
    try:
        batch_size = max(1, int(manifest.get("batch_size", 1)))
    except (TypeError, ValueError):
        return False
    total_batches = (total_jobs + batch_size - 1) // batch_size
    by_number = {
        record.get("batch"): record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("batch"), int)
    }
    return all(by_number.get(number, {}).get("status") == "success" for number in range(1, total_batches + 1))


def jd_stage_checkpoint_outcome(output_root: Path) -> tuple[str, str]:
    """Distinguish usable partial JD results from gates and broken checkpoints."""

    shortlist_dir = output_root / "prepared" / "jd_shortlists"
    jobs = load_run_manifest(shortlist_dir / "jd_input.json").get("jobs")
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        return "failed", "JD 初筛输入缺失或无效；已停止后续阶段，请检查 jd_input.json。"
    if not jobs:
        return "success", "JD 候选为 0，跳过补抓并继续后续阶段。"

    manifest = load_run_manifest(shortlist_dir / "details_manifest.json")
    records = manifest.get("records")
    batch_size = manifest.get("batch_size")
    if (
        type(manifest.get("total_jobs")) is not int
        or manifest["total_jobs"] != len(jobs)
        or type(batch_size) is not int
        or batch_size < 1
        or not isinstance(records, list)
    ):
        return "failed", "JD 检查点缺失或无效；已停止后续阶段，请检查 details_manifest.json。"

    total_batches = (len(jobs) + batch_size - 1) // batch_size
    by_number: dict[int, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            return "failed", "JD 批次检查点格式无效；已停止后续阶段。"
        number = record.get("batch")
        if type(number) is not int or not 1 <= number <= total_batches or number in by_number:
            return "failed", "JD 批次检查点编号无效或重复；已停止后续阶段。"
        by_number[number] = record

    if any(
        record.get("status") == "paused"
        or record.get("security_or_login_gate")
        or record.get("pause_reason") == "cdp_setup_failed"
        for record in by_number.values()
    ):
        return "paused", "JD 批次因登录、安全验证或 CDP 未就绪而暂停；已保留检查点，处理后可从该阶段续跑。"
    if len(by_number) != total_batches:
        return "failed", "JD 检查点缺少预期批次，尚未全部处理；已停止后续阶段。"
    for number, record in by_number.items():
        if record.get("status") not in ("success", "partial", "failed"):
            return "failed", f"JD 批次 {number} 尚未处理完成或状态无效；已停止后续阶段。"
        if type(record.get("exit_code")) is not int or record["exit_code"] != 0:
            return "failed", f"JD 批次 {number} 存在非零或无效退出码；已停止后续阶段，不能按普通缺失继续。"
    if jd_stage_checkpoint_complete(output_root):
        return "success", ""
    return "partial", "JD 批次均已处理，但仍有缺失或失败项；按现有可用 JD 继续后续阶段，缺失内容待核实，可稍后续抓。"


def can_skip_completed_stage(
    label: str,
    state: dict[str, Any],
    source_root: Path,
    output_root: Path,
    *,
    settings_match: bool,
) -> bool:
    stages = state.get("stages", {})
    entry = stages.get(label, {}) if isinstance(stages, dict) else {}
    if not settings_match or not isinstance(entry, dict) or entry.get("status") != "success":
        return False
    if not stage_artifacts_exist(label, source_root, output_root):
        return False
    if label == "JD补抓":
        return jd_stage_checkpoint_complete(output_root)
    if label == "Codex语义复核":
        return llm_stage_checkpoint_outcome(output_root)[0] == "success"
    return True


def resume_scope_conflicts(manifest: dict[str, Any], options: dict[str, Any]) -> list[str]:
    """A resume must not silently replace an old sample with a different one."""
    conflicts = []
    for key, option, label in (("city", "city", "城市"), ("track", "track", "岗位轨道"), ("sample_size", "sample", "岗位数量")):
        normalize = canonical_city if key == "city" else canonical_track if key == "track" else lambda value: value
        if key in manifest and normalize(manifest[key]) != normalize(options[option]):
            conflicts.append(label)
    saved_root = manifest.get("source_root")
    if saved_root and not same_path(saved_root, options["root"]):
        conflicts.append("采集根目录")
    return conflicts


def llm_stage_checkpoint_outcome(output_root: Path) -> tuple[str, str]:
    """An exit code of zero does not mean that semantic requests succeeded."""
    manifest = load_run_manifest(output_root / "prepared" / "llm_manifest.json")
    if not manifest:
        return "failed", "模型检查点缺失或无效，不能将退出码 0 当作复核成功。"
    status = manifest.get("status")
    if status == "configuration_failed":
        return "partial", "本机模型配置或访问未就绪；保留规则结果生成报告，语义复核待补。"
    if status == "skipped_no_candidates":
        return "success", "没有可复核的完整 JD，跳过模型阶段。"
    if status != "completed":
        return "failed", "模型检查点尚未完成，不能标记为复核成功。"
    failed = manifest.get("failed_count")
    pending = manifest.get("pending_count")
    fallback = manifest.get("fallback_count")
    if any(type(value) is not int or value < 0 for value in (failed, pending, fallback)):
        return "failed", "模型检查点统计无效，请检查 llm_manifest.json。"
    if failed or pending or fallback:
        return "partial", f"模型阶段：失败 {failed}、待完成 {pending}、整岗规则回退 {fallback}；报告将保留状态与核实事项。"
    return "success", ""


def make_run_directory(root: Path, city: str, sample_size: int | None) -> Path:
    # A runs junction must not redirect automatic output outside this day.
    runs = root / "runs"
    if runs.is_symlink() or getattr(runs, "is_junction", lambda: False)():
        raise ValueError("runs 目录使用了链接；请使用日目录内的普通运行目录。")
    try:
        runs.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise ValueError("运行输出目录不能越过所选采集日目录。") from error
    city_part = safe_component(city if city != "全部城市" else "all")
    quantity_part = str(sample_size) if sample_size else "all"
    base = root / "runs" / f"{timestamp()}-{city_part}-{quantity_part}"
    candidate = base
    suffix = 2
    while True:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            candidate = base.with_name(f"{base.name}-{suffix}")
            suffix += 1


def discover_default_root() -> Path:
    user_profile = Path(os.environ.get("USERPROFILE", Path.home()))
    candidates = [
        user_profile / ".boss-zhipin-scraper" / "job-result" / "2026-10-16-architecture-screen-full",
        user_profile / ".boss-zhipin-scraper" / "job-result",
        default_state_root() / "job-result",
    ]
    for candidate in candidates:
        if (candidate / "collection_manifest.json").is_file():
            return candidate
        if candidate.is_dir():
            existing = [path for path in candidate.iterdir() if path.is_dir() and (path / "collection_manifest.json").is_file()]
            if existing:
                return max(existing, key=lambda path: path.stat().st_mtime)
    return collector.default_root()


def selected_city_argument(city: str) -> str | None:
    return None if city == "全部城市" else city


def selected_track_argument(track: str) -> str:
    return {"当前+成长": "all", "当前岗位": "current", "成长目标": "target"}[track]


def make_collect_command(
    python: str,
    root: Path,
    scraper_root: Path,
    city: str,
    track: str,
    pages: int,
    combination_limit: int | None,
    *,
    prepare_browser: bool,
    setup_only: bool = False,
    config_path: Path = DEFAULT_CONFIG,
) -> list[str]:
    command = [
        python,
        "-u",
        str(PROJECT_ROOT / "collect_architecture.py"),
        "--root",
        str(root),
        "--config",
        str(config_path),
        "--batch",
        selected_track_argument(track),
        "--pages",
        str(pages),
        "--scraper-root",
        str(scraper_root),
        "--gui-interactive",
    ]
    if city != "全部城市":
        command.extend(("--cities", city))
    if combination_limit:
        command.extend(("--limit", str(combination_limit)))
    if prepare_browser:
        command.append("--setup-browser")
    if setup_only:
        command.append("--setup-only")
    return command


def make_process_command(
    python: str,
    source_root: Path,
    output_root: Path,
    stage: str,
    city: str,
    track: str,
    sample_size: int | None,
    workers: int,
    batch_size: int,
    scraper_root: Path | None,
    config_path: Path = DEFAULT_CONFIG,
) -> list[str]:
    command = [
        python,
        "-u",
        str(PROJECT_ROOT / "process_architecture.py"),
        "--root",
        str(source_root),
        "--config",
        str(config_path),
        "--output-root",
        str(output_root),
        "--stage",
        stage,
        "--workers",
        str(workers),
        "--batch-size",
        str(batch_size),
        "--sample-strategy",
        "balanced-dedup",
        "--gui-interactive",
        "--model",
        AI_DEFAULT_MODEL,
        "--reasoning-effort",
        AI_DEFAULT_REASONING_EFFORT,
    ]
    if scraper_root is not None:
        command.extend(("--scraper-root", str(scraper_root)))
    if city != "全部城市":
        command.extend(("--city", city))
    if track != "当前+成长":
        command.extend(("--tracks", selected_track_argument(track)))
    if sample_size:
        command.extend(("--sample-size", str(sample_size)))
    return command


class _OrderedLogWriter:
    """One FIFO writer per window. No filesystem operations run on Tk."""

    def __init__(self, events: queue.Queue, *, batch_chars: int = LOG_BATCH_CHARS,
                 flush_interval: float = LOG_FLUSH_SECONDS):
        self.events = events
        self.batch_chars = batch_chars
        self.flush_interval = flush_interval
        self.pending: queue.Queue = queue.Queue()
        self.done = threading.Event()
        self._closing = False
        self.thread = threading.Thread(target=self._run, name="desktop-log-writer", daemon=True)
        self.thread.start()

    def append(self, path: Path, text: str) -> bool:
        if self._closing or self.done.is_set():
            return False
        self.pending.put(("append", (path, text)))
        return True

    def flush(self) -> threading.Event:
        completed = threading.Event()
        if self.done.is_set():
            completed.set()
        elif self._closing:
            return self.done
        else:
            self.pending.put(("flush", completed))
        return completed

    def close(self) -> threading.Event:
        if not self._closing:
            self._closing = True
            self.pending.put(("close", None))
        return self.done

    def _run(self) -> None:
        stream = None
        stream_path = None
        path = None
        parts: list[str] = []
        size = 0
        deadline = 0.0
        warned = False

        def warn() -> None:
            nonlocal warned
            if not warned:
                # Never publish exception strings, filesystem paths or content.
                self.events.put(("log_write_error", LOG_WRITE_WARNING))
                warned = True

        def close_stream() -> None:
            nonlocal stream, stream_path
            if stream is not None:
                try:
                    stream.close()
                except (OSError, UnicodeError):
                    warn()
            stream, stream_path = None, None

        def persist() -> None:
            nonlocal stream, stream_path, parts, size, deadline, warned
            if not parts:
                return
            try:
                if stream_path != path:
                    close_stream()
                    assert path is not None
                    path.parent.mkdir(parents=True, exist_ok=True)
                    stream = path.open("a", encoding="utf-8")
                    stream_path = path
                stream.write("".join(parts))
                stream.flush()
                warned = False
            except (OSError, UnicodeError):
                warn()
                close_stream()
            finally:
                parts, size, deadline = [], 0, 0.0

        try:
            while True:
                try:
                    kind, value = self.pending.get(timeout=max(0.0, deadline - time.monotonic()) if parts else None)
                except queue.Empty:
                    persist()
                    continue
                if kind == "append":
                    next_path, text = value
                    if parts and next_path != path:
                        persist()
                    path = next_path
                    if not parts:
                        deadline = time.monotonic() + self.flush_interval
                    parts.append(text)
                    size += len(text)
                    if size >= self.batch_chars or time.monotonic() >= deadline:
                        persist()
                else:
                    persist()
                    if kind == "flush":
                        value.set()
                    elif kind == "close":
                        break
        except Exception:
            warn()
        finally:
            close_stream()
            self.done.set()


class CollectorDesktopApp(WorkspaceMixin):
    def __init__(self, root: tk.Tk, args: argparse.Namespace):
        self.root = root
        install_tk_lifecycle(root)
        self.root.title("BOSS 岗位工作台 · 采集与智能筛选")
        width = min(1440, max(1080, self.root.winfo_screenwidth() - 80))
        height = min(920, max(700, self.root.winfo_screenheight() - 100))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(1080, 700)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.ui_thread_id = threading.get_ident()
        self.process: subprocess.Popen[str] | None = None
        self._process_lock = threading.Lock()
        self._stopping_process: subprocess.Popen[str] | None = None
        self.worker: threading.Thread | None = None
        self._stats_request_id = 0
        self._route_request_id = 0
        self._route_after_id: str | None = None
        self._route_previous_context: tuple[Path | None, Path | None, str] | None = None
        self.active_run: Path | None = None
        self.current_log: Path | None = None
        self._log_writer: _OrderedLogWriter | None = None
        self.waiting_for_human = False
        self.stop_requested = False
        self._pause_requested = False
        self._closing = False
        self._poll_after_id: str | None = None
        self._browser_prepared = False
        self._workflow_kind = "workflow"
        self.workflow_running = False
        self._collection_only = False
        self.config_path = Path(args.config).expanduser().resolve()
        self.config = collector.load_config(self.config_path)

        preferences = load_preferences()
        default_root = Path(args.root).expanduser() if args.root else Path(preferences["data_root"]).expanduser() if preferences.get("data_root") else discover_default_root()
        default_scraper = args.scraper_root or os.environ.get("BOSS_SCRAPER_ROOT", "") or preferences.get("scraper_root", "")
        if not default_scraper:
            candidates = (Path.home() / ".codex" / "tools" / "boss-zhipin-scraper", PROJECT_ROOT.parent / "boss-zhipin-scraper")
            default_scraper = next((str(path) for path in candidates if (path / "scripts" / "boss_cdp_raw.py").is_file()), "")
        self.data_root = StringVar(master=self.root, value=str(default_root))
        self.scraper_root = StringVar(master=self.root, value=str(Path(default_scraper).expanduser()) if default_scraper else "")
        self.city = StringVar(master=self.root, value=preferences.get("city") if preferences.get("city") in CITY_CHOICES else "上海")
        self.track = StringVar(master=self.root, value=preferences.get("track") if preferences.get("track") in TRACK_CHOICES else "当前+成长")
        self.stage = StringVar(master=self.root, value=preferences.get("stage") if preferences.get("stage") in STAGE_CHOICES else "列表前处理")
        self.combination_limit = StringVar(master=self.root, value=preferences.get("combination_limit", ""))
        self.pages = StringVar(master=self.root, value=preferences.get("pages", "10"))
        self.sample_size = StringVar(master=self.root, value=preferences.get("sample_size", "50"))
        self.workers = StringVar(master=self.root, value=preferences.get("workers", str(processor.AI_DEFAULT_WORKERS)))
        self.batch_size = StringVar(master=self.root, value=preferences.get("batch_size", "30"))
        self.output_directory = StringVar(master=self.root, value="尚未创建运行目录")
        self.stats_text = StringVar(master=self.root, value="请选择结果目录并刷新统计。")
        self.run_summary = StringVar(master=self.root, value="")
        self.routing_summary = StringVar(master=self.root, value="正在识别采集日目录与阶段输入…")
        self.status_text = StringVar(master=self.root, value="就绪")
        self.progress_text = StringVar(master=self.root, value="")
        self.resume_run = StringVar(master=self.root, value=AUTO_RUN_CHOICE)
        self.history_values: list[str] = []
        self.prompt_button_text = StringVar(master=self.root, value="人工处理完成，继续")

        self._build_ui()
        for variable in (self.data_root, self.stage, self.city, self.track, self.sample_size, self.resume_run):
            variable.trace_add("write", self._request_route_preview)
        self._refresh_history()
        self._poll_after_id = self.root.after(100, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh_stats()
        self._request_route_preview()

    def _build_ui(self) -> None:
        self._build_workspace(CITY_CHOICES, TRACK_CHOICES, STAGE_CHOICES)

    def _append_log(self, text: str) -> None:
        if not text:
            return
        if threading.get_ident() != self.ui_thread_id:
            self.events.put(("log", redact_secrets(text)))
            return
        text = redact_secrets(text)
        self.log_view.configure(state="normal")
        if getattr(self, "_workspace_ready", False):
            lower = text.lower()
            tag = "error" if any(word in lower for word in ("traceback", "error", "失败")) else "warning" if any(word in lower for word in ("暂停", "验证", "warning")) else "success" if any(word in lower for word in ("success", "完成", "成功")) else ""
            self.log_view.insert("end", text, tag)
        else:
            self.log_view.insert("end", text)
        if not hasattr(self, "auto_scroll") or self.auto_scroll.get():
            self.log_view.see("end")
        try:
            end = self.log_view.index("end-1c")
            count = int(self.log_view.count("1.0", end, "chars")[0])
        except (AttributeError, TypeError, IndexError, tk.TclError):
            count = 0
        if count > MAX_LOG_CHARS:
            self.log_view.delete("1.0", f"1.0+{count - MAX_LOG_CHARS} chars")
        self.log_view.configure(state="disabled")

    def _record_log(self, text: str) -> None:
        """Display on Tk, then enqueue an already-redacted FIFO disk write."""

        if not text:
            return
        if threading.get_ident() != self.ui_thread_id:
            self._queue_log(text)
            return
        safe_text = redact_secrets(text)
        self._append_log(safe_text)
        log_path = self.current_log
        if log_path is not None:
            writer = getattr(self, "_log_writer", None)
            if writer is None:
                writer = self._log_writer = _OrderedLogWriter(self.events)
            if not writer.append(log_path, safe_text):
                self._append_log(LOG_WRITE_WARNING)

    def _flush_logs(self) -> None:
        writer = getattr(self, "_log_writer", None)
        if writer is not None:
            writer.flush()

    def _queue_log(self, text: str) -> None:
        if text:
            self.events.put(("log", redact_secrets(text)))

    def _choose_root(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.data_root.get() or str(PROJECT_ROOT))
        if chosen:
            self.data_root.set(chosen)
            self._refresh_history()
            self._refresh_stats()

    def _choose_scraper(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.scraper_root.get() or str(PROJECT_ROOT.parent))
        if chosen:
            self.scraper_root.set(chosen)

    def _refresh_history(self) -> None:
        if getattr(self, "_workspace_ready", False):
            self._refresh_workspace_history()
            return
        root_path = Path(self.data_root.get()).expanduser()
        runs = sorted((path for path in (root_path / "runs").glob("*") if path.is_dir()), reverse=True) if root_path.exists() else []
        self.history_values = [AUTO_RUN_CHOICE, NEW_RUN_CHOICE, *(str(path) for path in runs[:100])]
        if hasattr(self, "history_box"):
            self.history_box.configure(values=self.history_values)
            if self.resume_run.get() not in self.history_values:
                self.resume_run.set(AUTO_RUN_CHOICE)

    def _request_route_preview(self, *_args: Any) -> None:
        """Debounce Tk traces; only the Tk thread reads selection variables."""
        if getattr(self, "_closing", False):
            return
        self._route_request_id = getattr(self, "_route_request_id", 0) + 1
        if getattr(self, "_route_after_id", None):
            try:
                self.root.after_cancel(self._route_after_id)
            except tk.TclError:
                pass
        self._route_after_id = self.root.after(250, self._begin_route_preview)

    def _begin_route_preview(self) -> None:
        self._route_after_id = None
        if self._closing:
            return
        request_id = self._route_request_id
        try:
            sample = parse_optional_positive_integer(self.sample_size.get(), "后处理岗位数")
            snapshot = (self.data_root.get(), self.stage.get(), self.city.get(),
                        self.track.get(), sample, self.resume_run.get())
        except (ValueError, OSError) as error:
            self.routing_summary.set(f"暂不能预览：{redact_secrets(str(error))}")
            return
        # The closure contains ordinary Python values, never Tk variables.
        events = self.events

        def work() -> None:
            try:
                plan = resolve_route(*snapshot)
                events.put(("route_preview", (request_id, plan.summary)))
            except Exception as error:
                events.put(("route_preview_error", (request_id, redact_secrets(str(error)))))

        threading.Thread(target=work, name="desktop-route-preview", daemon=True).start()

    def _refresh_stats(self) -> None:
        self._stats_request_id += 1
        request_id = self._stats_request_id
        if self.worker and self.worker.is_alive():
            return
        root_path = Path(self.data_root.get()).expanduser()
        city = selected_city_argument(self.city.get())
        tracks = selected_track_argument(self.track.get())
        self.stats_text.set("正在统计…")

        def work() -> None:
            try:
                daily_root, _warnings = resolve_daily_root(root_path)
                info = processor.inspect_city(daily_root, self.config, city, tracks=tracks)
                self.events.put(("stats", (request_id, info)))
            except Exception as error:  # keep the UI usable for incomplete datasets
                self.events.put(("stats_error", (request_id, str(error))))

        threading.Thread(target=work, daemon=True).start()

    def _validate(self) -> dict[str, Any]:
        root_text = self.data_root.get().strip()
        if not root_text:
            raise ValueError("请先选择某天的采集总文件夹，结果目录不能为空。")
        root_path = Path(root_text).expanduser().resolve()
        if self.city.get() not in CITY_CHOICES:
            raise ValueError("请选择有效城市。")
        if self.track.get() not in TRACK_CHOICES:
            raise ValueError("请选择有效岗位轨道。")
        if self.stage.get() not in STAGE_CHOICES:
            raise ValueError("请选择有效处理阶段。")
        if root_path.exists() and not root_path.is_dir():
            raise ValueError("采集结果根目录必须是文件夹。")
        if not root_path.exists() and self.stage.get() not in ("仅采集", "完整流程（采集+后处理）"):
            raise ValueError("采集结果根目录不存在；请先选择已有数据目录或运行采集。")
        scraper_text = self.scraper_root.get().strip() or os.environ.get("BOSS_SCRAPER_ROOT", "")
        scraper_path = Path(scraper_text).expanduser().resolve() if scraper_text else None
        if self.stage.get() in ("仅采集", "JD补抓", "后处理全流程（已有数据）", "完整流程（采集+后处理）"):
            if not scraper_path or not (scraper_path / "scripts" / "boss_cdp_raw.py").is_file():
                raise ValueError("未找到上游 boss-zhipin-scraper，请选择正确的上游目录。")
        try:
            pages = int(self.pages.get())
            workers = int(self.workers.get())
            batch_size = int(self.batch_size.get())
        except ValueError as error:
            raise ValueError("页数、并发数和JD批次必须是整数。") from error
        if not 1 <= pages <= 10:
            raise ValueError("每个检索组合的页数必须在 1–10 之间。")
        if not 1 <= workers <= 32 or not 1 <= batch_size <= 30:
            raise ValueError("AI并发必须在1–32之间，JD批次必须在1–30之间。")
        limit = parse_optional_positive_integer(self.combination_limit.get(), "采集组合数")
        sample = parse_optional_positive_integer(self.sample_size.get(), "后处理岗位数")
        return {
            "root": root_path,
            "scraper": scraper_path,
            "city": self.city.get(),
            "track": self.track.get(),
            "pages": pages,
            "limit": limit,
            "sample": sample,
            "workers": workers,
            "batch_size": batch_size,
        }

    def _prepare_browser(self) -> None:
        """Launch the isolated browser profile and wait for the user to log in."""

        if self.worker and self.worker.is_alive():
            return
        try:
            scraper_text = self.scraper_root.get().strip() or os.environ.get("BOSS_SCRAPER_ROOT", "")
            scraper_path = Path(scraper_text).expanduser().resolve() if scraper_text else None
            if not scraper_path or not (scraper_path / "scripts" / "boss_cdp_raw.py").is_file():
                raise ValueError("未找到上游 boss-zhipin-scraper，请选择正确的上游目录。")
            pages = int(self.pages.get())
            if not 1 <= pages <= 10:
                raise ValueError("每个检索组合的页数必须在 1–10 之间。")
            limit = parse_optional_positive_integer(self.combination_limit.get(), "采集组合数")
            options = {
                "root": Path(self.data_root.get()).expanduser().resolve(),
                "scraper": scraper_path,
                "city": self.city.get(),
                "track": self.track.get(),
                "pages": pages,
                "limit": limit,
            }
            if options["city"] not in CITY_CHOICES or options["track"] not in TRACK_CHOICES:
                raise ValueError("请选择有效城市和岗位轨道。")
        except (ValueError, OSError) as error:
            messagebox.showerror("无法准备浏览器", str(error), parent=self.root)
            return
        try:
            setup_root = make_run_directory(Path(tempfile.gettempdir()) / "boss-architecture-collector", "browser-setup", None)
        except OSError as error:
            messagebox.showerror("无法准备浏览器", f"无法创建临时日志目录：{error}", parent=self.root)
            return
        self.active_run = setup_root
        self._collection_only = False
        self.current_log = setup_root / "desktop.log"
        self._workflow_kind = "browser_setup"
        self._browser_prepared = False
        self.stop_requested = False
        self._pause_requested = False
        self.workflow_running = True
        self.output_directory.set(f"浏览器准备日志目录：{setup_root}")
        self._record_log(f"\n=== 专用浏览器准备 {datetime.now().astimezone().isoformat(timespec='seconds')} ===\n")
        self.status_text.set("正在启动专用浏览器…")
        self.progress_text.set("")
        self._set_busy(True)
        if getattr(self, "_workspace_ready", False):
            self._on_workspace_start("准备浏览器 / 人工登录")
        self.worker = threading.Thread(target=self._run_browser_setup_safely, args=(options, setup_root), daemon=True)
        self.worker.start()

    def _run_browser_setup_safely(self, options: dict[str, Any], setup_root: Path) -> None:
        try:
            self._run_browser_setup(options, setup_root)
        except Exception as error:
            self._queue_log(f"浏览器准备失败：{redact_secrets(str(error))}\n")
            self.events.put(("browser_ready", {"exit_code": 1}))

    def _run_browser_setup(self, options: dict[str, Any], setup_root: Path) -> None:
        command = make_collect_command(
            sys.executable,
            setup_root,
            options["scraper"],
            options["city"],
            options["track"],
            options["pages"],
            options["limit"],
            prepare_browser=True,
            setup_only=True,
            config_path=self.config_path,
        )
        self._queue_log("\n--- 准备专用浏览器并等待登录 ---\n")
        self._queue_log("命令：" + " ".join(redact_command(command)) + "\n")
        code = self._run_child(command)
        self.events.put(("browser_ready", {"exit_code": 130 if self.stop_requested else code}))

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            options = self._validate()
        except (ValueError, OSError) as error:
            messagebox.showerror("无法开始", str(error), parent=self.root)
            return
        stage_name = self.stage.get()
        collect_stage = stage_name in ("仅采集", "完整流程（采集+后处理）")
        if collect_stage and not self._browser_prepared:
            if not messagebox.askyesno(
                "确认浏览器登录",
                "尚未通过“准备专用浏览器/登录”确认专用浏览器已登录。\n\n"
                "如果已有可用的专用 CDP 浏览器，可以继续；否则请先准备并登录。仍要开始吗？",
                parent=self.root,
            ):
                return
        options["prepare_browser"] = collect_stage and not self._browser_prepared
        options["write_run_manifest"] = stage_name != "仅采集"
        selected_run = self.resume_run.get()
        if stage_name in ("JD补抓", "Codex语义复核", "生成报告") and selected_run == NEW_RUN_CHOICE:
            if (
                self.active_run
                and self.active_run.resolve() != options["root"].resolve()
                and (self.active_run / "prepared").is_dir()
                and load_run_manifest(self.active_run / "run_manifest.json")
            ):
                selected_run = str(self.active_run)
            else:
                messagebox.showerror("请选择已有运行", "该阶段需要列表前处理生成的检查点，请在“已有运行续跑”中选择对应运行目录。", parent=self.root)
                return
        if stage_name != "仅采集" and selected_run not in (AUTO_RUN_CHOICE, NEW_RUN_CHOICE):
            # Keep fast, explicit-resume errors synchronous.  Daily-root
            # normalization and every candidate scan/import happen below on
            # the worker, not on the Tk input/redraw thread.
            output = Path(selected_run).expanduser()
            if not output.is_dir():
                messagebox.showerror("运行目录不可用", "所选的历史运行目录已不存在。", parent=self.root)
                return
            saved = load_run_manifest(output / "run_manifest.json")
            if not saved:
                messagebox.showerror("运行清单不可用", "所选目录的 run_manifest.json 缺失或无效。\n请只读查看该目录，或新建运行；不会覆盖未知范围的旧结果。", parent=self.root)
                return
            scope_options = dict(options, root=saved.get("source_root") or options["root"])
            conflicts = resume_scope_conflicts(saved, scope_options)
            if conflicts:
                messagebox.showerror("续跑范围不一致", "所选运行的" + "、".join(conflicts) + "与当前设置不同。\n请选择“新建运行目录”，或从运行历史载入原设置；不会覆盖旧样本。", parent=self.root)
                return
        self._route_previous_context = (self.active_run, self.current_log, self.output_directory.get())
        self.active_run = None
        self._collection_only = stage_name == "仅采集"
        self._workflow_kind = "workflow"
        self.current_log = None
        self.output_directory.set("正在识别日目录、选择输入与准备独立输出…")
        self.status_text.set("正在解析阶段输入…")
        self.progress_text.set("")
        self.stop_requested = False
        self._pause_requested = False
        self.workflow_running = True
        self.waiting_for_human = False
        self._set_busy(True)
        if getattr(self, "_workspace_ready", False):
            self._save_workspace_preferences()
            self._on_workspace_start(stage_name)
        self.worker = threading.Thread(target=self._run_routed_workflow_safely, args=(options, stage_name, selected_run), name="desktop-routed-workflow", daemon=True)
        self.worker.start()

    def _run_routed_workflow_safely(self, options: dict[str, Any], stage_name: str, run_choice: str) -> None:
        output: Path | None = None
        plan: RoutePlan | None = None
        activated = False
        prepared = False
        try:
            plan = resolve_route(options["root"], stage_name, options["city"], options["track"], options["sample"], run_choice)
            if plan.errors:
                raise ValueError("；".join(plan.errors))
            if self.stop_requested:
                self.events.put(("route_failed", {"message": "已停止，尚未创建或修改运行目录。", "cancelled": True}))
                return
            routed = dict(options, root=plan.source_root, route_plan=plan,
                          execution_stages=plan.stages, routing_quality_incomplete=plan.quality_incomplete)
            if plan.mode == "collection":
                output = plan.source_root
                output.mkdir(parents=True, exist_ok=True)
            elif plan.mode == "resume":
                output = plan.input_run
            else:
                output = make_run_directory(plan.source_root, options["city"], options["sample"])
            assert output is not None
            prepared = True
            # Finish the small prerequisite chain before honoring pause/stop.
            # A normal pause must never publish half-imported inputs as a
            # resumable run; the workflow checks stop_requested immediately
            # after its complete, durable routing manifest is written.
            imported = copy_route_inputs(plan, output)
            # The Tk consumer starts desktop.log as soon as it sees this
            # event.  Publish only after the exclusive empty-run import has
            # finished, otherwise that log races the import's safety check.
            self.events.put(("route_started", {"output": str(output), "source_root": str(plan.source_root),
                                               "summary": plan.summary, "stage": stage_name,
                                               "city": options["city"], "track": options["track"]}))
            activated = True
            routed["imported_files"] = [str(path) for path in imported]
            self._queue_log("\n--- 阶段输入路由 ---\n" + plan.summary + "\n")
            for relative, destination in zip(plan.copy_files, imported):
                self._queue_log(f"导入前置：{plan.input_run / relative} -> {destination}\n")
            self._run_workflow_safely(routed, stage_name, output)
        except Exception as error:
            safe_error = redact_secrets(str(error))
            if output is None or not prepared:
                self.events.put(("route_failed", {"message": safe_error, "cancelled": False}))
                return
            if not activated and plan is not None:
                self.events.put(("route_started", {"output": str(output), "source_root": str(plan.source_root),
                                                   "summary": plan.summary, "stage": stage_name,
                                                   "city": options["city"], "track": options["track"]}))
            paused = self.stop_requested and getattr(self, "_pause_requested", False)
            state = {"status": "paused" if paused else "stopped_or_failed" if self.stop_requested else "failed",
                     "source_root": str(plan.source_root) if plan else str(options["root"]),
                     "output_root": str(output), "city": options["city"], "track": options["track"],
                     "sample_size": options["sample"], "stage_selection": stage_name, "stages": {},
                     "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"), "error": safe_error}
            if paused:
                state["pause_reason"] = "user_requested"
            if plan:
                state["routing"] = plan.to_manifest()
                state["routing"]["preparation_complete"] = activated
            if stage_name != "仅采集":
                try:
                    self._write_run_manifest(output, state)
                except OSError as manifest_error:
                    self._queue_log(f"保存运行清单失败：{redact_secrets(str(manifest_error))}\n")
            self._queue_log(f"阶段输入准备失败：{safe_error}\n")
            self.events.put(("finished", {"exit_code": 130 if self.stop_requested else 1, "output": str(output), "manifest": state}))

    def _run_workflow_safely(self, options: dict[str, Any], stage_name: str, output: Path) -> None:
        try:
            self._run_workflow(options, stage_name, output)
        except Exception as error:
            safe_error = redact_secrets(str(error))
            self._queue_log(f"工作流失败：{safe_error}\n")
            write_manifest = stage_name != "仅采集" and bool(options.get("write_run_manifest", True))
            state = load_run_manifest(output / "run_manifest.json") if write_manifest else {}
            state["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            state["status"] = "failed"
            state["error"] = safe_error
            for entry in (state.get("stages") or {}).values():
                if isinstance(entry, dict) and entry.get("status") == "running":
                    entry.update({"status": "failed", "error": safe_error, "finished_at": state["finished_at"]})
            if write_manifest:
                try:
                    self._write_run_manifest(output, state)
                except OSError as manifest_error:
                    self._queue_log(f"保存运行清单失败：{redact_secrets(str(manifest_error))}\n")
            self.events.put(("finished", {"exit_code": 1, "output": str(output), "manifest": state}))

    def _set_busy(self, busy: bool) -> None:
        self.start_button.configure(state="disabled" if busy else "normal")
        self.prepare_browser_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        if getattr(self, "_workspace_ready", False):
            self.pause_button.configure(state="normal" if busy and self._workflow_kind == "workflow" else "disabled")
            self.resume_history_button.configure(state="disabled" if busy else "normal")
            for widget, normal_state in self._settings_controls:
                widget.configure(state="disabled" if busy else normal_state)
        if not busy:
            can_resume = getattr(self, "_pause_requested", False) and getattr(self, "_workflow_kind", "workflow") == "workflow"
            self.continue_button.configure(state="normal" if can_resume else "disabled")
            if hasattr(self, "prompt_button_text"):
                self.prompt_button_text.set("继续此任务" if can_resume else "人工处理完成，继续")

    def _write_run_manifest(self, output: Path, state: dict[str, Any]) -> None:
        path = output / "run_manifest.json"
        payload: dict[str, Any] = {}
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    payload = value
            except (OSError, json.JSONDecodeError):
                payload = {}
        payload.update(redact_json(state))
        payload = redact_json(payload)
        payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        output.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _run_workflow(self, options: dict[str, Any], stage_name: str, output: Path) -> None:
        stages: list[tuple[str, list[str]]] = []
        collect_stage = stage_name in ("仅采集", "完整流程（采集+后处理）")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        write_manifest = stage_name != "仅采集" and bool(options.get("write_run_manifest", True))
        state = load_run_manifest(output / "run_manifest.json") if write_manifest else {}
        settings_match = run_settings_match(state, options) if state else False
        if not state:
            state = {"schema_version": "1.0", "started_at": now, "stages": {}}
        state.update({
            "source_root": str(options["root"]),
            "output_root": str(output),
            "city": options["city"],
            "track": options["track"],
            "combination_limit": options["limit"],
            "pages_per_combination": options["pages"],
            "sample_size": options["sample"],
            "workers": options["workers"],
            "stage_selection": stage_name,
            "batch_size": options["batch_size"],
            "last_started_at": now,
            "status": "running",
            "error": None,
            "pause_reason": None,
        })
        route_plan = options.get("route_plan")
        if isinstance(route_plan, RoutePlan):
            state["routing"] = route_plan.to_manifest()
            state["routing"]["output_root"] = str(output)
            state["routing"]["imported_files"] = list(options.get("imported_files", []))
            if route_plan.input_run and route_plan.mode == "auto":
                state["imported_from"] = str(route_plan.input_run)
        if not isinstance(state.get("stages"), dict):
            state["stages"] = {}
        if isinstance(route_plan, RoutePlan):
            for label in route_plan.recovered_stages:
                entry = state["stages"].get(label)
                entry = dict(entry) if isinstance(entry, dict) else {}
                entry.update(status="success", recovered_from_durable_checkpoint=True, recovered_at=now)
                state["stages"][label] = entry
        if not settings_match:
            for key in ("source_stats", "sample_stats", "classification_counts", "jd_manifest", "llm_manifest"):
                state.pop(key, None)
        if write_manifest:
            self._write_run_manifest(output, state)
        if collect_stage:
            collect_command = make_collect_command(
                sys.executable,
                options["root"],
                options["scraper"],
                options["city"],
                options["track"],
                options["pages"],
                options["limit"],
                prepare_browser=bool(options.get("prepare_browser", True)),
                config_path=self.config_path,
            )
            stages.append(("岗位采集", collect_command))
        command_for_stage = dict(POST_PROCESS_STAGES)
        for item in options.get("execution_stages", planned_stages(stage_name)):
            if item == "岗位采集":
                continue
            command = make_process_command(
                sys.executable,
                options["root"],
                output,
                command_for_stage[item],
                options["city"],
                options["track"],
                options["sample"],
                options["workers"],
                options["batch_size"],
                options["scraper"],
                self.config_path,
            )
            stages.append((item, command))

        exit_code = 0
        workflow_paused = False
        for stage_label, command in stages:
            if self.stop_requested:
                exit_code = 130
                break
            if can_skip_completed_stage(
                stage_label,
                state,
                options["root"],
                output,
                settings_match=settings_match,
            ):
                self._queue_log(f"\n--- {stage_label}：复用已完成检查点，跳过 ---\n")
                continue
            self.events.put(("status", f"正在运行：{stage_label}"))
            self.events.put(("progress", ""))
            self._queue_log(f"\n--- {stage_label} ---\n")
            self._queue_log("命令：" + " ".join(redact_command(command)) + "\n")
            stage_start = datetime.now().astimezone().isoformat(timespec="seconds")
            invalidate_downstream_stages(state, stage_label)
            state["stages"][stage_label] = {
                "started_at": stage_start,
                "status": "running",
                "command": redact_command(command),
            }
            if write_manifest:
                self._write_run_manifest(output, state)
            no_jd_candidates = False
            if stage_label == "JD补抓":
                jobs = load_run_manifest(output / "prepared" / "jd_shortlists" / "jd_input.json").get("jobs")
                no_jd_candidates = isinstance(jobs, list) and not jobs
            if stage_label == "列表前处理" and isinstance(route_plan, RoutePlan) and route_plan.fixed_sample:
                frozen = load_fixed_sample(output, options["root"], options["city"], options["track"], options["sample"])
                if frozen is None:
                    raise ValueError("已固定的 raw_sample.json 缺失、无效或范围改变；为避免重新抽样，已停止恢复。")
                self._queue_log(f"从已固定样本恢复列表索引：{route_plan.fixed_sample}；不重新读取 raw 或抽样。\n")
                processor.write_indexes(output, copy.deepcopy(frozen["jobs"]), copy.deepcopy(frozen["metadata"]), copy.deepcopy(frozen["jobs"]))
                code = 0
            else:
                code = 0 if no_jd_candidates else self._run_child(command)
            if self.stop_requested:
                code = 130
            stage_status = "success" if code == 0 else "failed"
            notice = ""
            if stage_label == "JD补抓" and code == 0:
                stage_status, notice = jd_stage_checkpoint_outcome(output)
                if stage_status == "paused":
                    code = 20
                    workflow_paused = True
                elif stage_status == "failed":
                    code = 1
            elif stage_label == "Codex语义复核" and code == 0:
                stage_status, notice = llm_stage_checkpoint_outcome(output)
                if stage_status == "failed":
                    code = 1
            if self.stop_requested and getattr(self, "_pause_requested", False):
                stage_status = "paused"
                workflow_paused = True
                notice = "用户主动暂停，已保留现有检查点；可继续同一运行。"
                state["pause_reason"] = "user_requested"
            if notice:
                self._queue_log(notice + "\n")
            state["stages"][stage_label] = {
                "started_at": stage_start,
                "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "exit_code": code,
                "status": stage_status,
                "command": redact_command(command),
            }
            if notice:
                state["stages"][stage_label]["message"] = notice
            if stage_status == "partial":
                state["stages"][stage_label]["warning"] = notice
            if no_jd_candidates and stage_status == "success":
                state["stages"][stage_label]["skipped_reason"] = "no_candidates"
            if write_manifest:
                self._write_run_manifest(output, state)
            if code != 0:
                exit_code = code
                break
        if self.stop_requested and getattr(self, "_pause_requested", False):
            workflow_paused = True
            state["pause_reason"] = "user_requested"
        state["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        stage_statuses = [state["stages"].get(label, {}).get("status") for label, _ in stages]
        state["warnings"] = [
            state["stages"][label]["warning"]
            for label, _ in stages
            if state["stages"].get(label, {}).get("status") == "partial"
        ]
        if isinstance(route_plan, RoutePlan):
            state["warnings"].extend(route_plan.warnings)
            if route_plan.quality_incomplete:
                state["warnings"].append("仍待核实：" + "；".join(route_plan.missing))
        stale_stages = [name for name, entry in state["stages"].items() if isinstance(entry, dict) and entry.get("status") == "stale"]
        if stale_stages:
            state["warnings"].append("上游数据已更新，以下阶段尚未重新核对：" + "、".join(stale_stages) + "；旧岗位分层与报告待更新。")
        if exit_code == 0 and all(status in ("success", "partial") for status in stage_statuses):
            state["status"] = "partial" if "partial" in stage_statuses or stale_stages or options.get("routing_quality_incomplete") else "success"
        else:
            state["status"] = "paused" if workflow_paused else "stopped_or_failed"
        try:
            stats = processor.inspect_city(options["root"], self.config, selected_city_argument(options["city"]), tracks=selected_track_argument(options["track"]))
            state["source_stats"] = stats
            if write_manifest and output.joinpath("prepared", "hai_tou_pool.json").exists():
                pool = processor.read_json(output / "prepared" / "hai_tou_pool.json")
                state["classification_counts"] = pool.get("summary", {}).get("classification_counts", {})
            details_path = output / "prepared" / "jd_shortlists" / "details_manifest.json"
            if write_manifest and details_path.exists():
                state["jd_manifest"] = processor.read_json(details_path)
            llm_path = output / "prepared" / "llm_manifest.json"
            if write_manifest and llm_path.exists():
                state["llm_manifest"] = processor.read_json(llm_path)
        except Exception as error:
            state["stats_error"] = redact_secrets(str(error))
        sample_path = output / "raw_sample.json"
        if write_manifest and sample_path.is_file():
            try:
                raw_sample = processor.read_json(sample_path)
                state["sample_stats"] = raw_sample.get("metadata", {}) if isinstance(raw_sample, dict) else {}
            except Exception as error:
                state["sample_stats_error"] = redact_secrets(str(error))
        state["output_files"] = {
            name: str(path)
            for name, path in {
                "raw_sample": output / "raw_sample.json",
                "merged_jobs": output / "prepared" / "merged_jobs_index.json",
                "jd_input": output / "prepared" / "jd_shortlists" / "jd_input.json",
                "jd_details": output / "prepared" / "jd_shortlists" / "details.json",
                "llm_reviews": output / "prepared" / "llm_reviews.json",
                "hai_tou_pool": output / "prepared" / "hai_tou_pool.json",
                "report": output / "prepared" / "海投分层清单.md",
                "desktop_log": output / "desktop.log",
            }.items()
            if path.is_file()
        }
        if not write_manifest:
            state["output_files"] = {name: str(path) for name, path in {
                "collection_manifest": options["root"] / "collection_manifest.json",
                "desktop_log": output / "desktop.log",
            }.items() if path.is_file()}
        if write_manifest:
            self._write_run_manifest(output, state)
        self.events.put(("finished", {"exit_code": exit_code, "output": str(output), "manifest": state}))

    def _run_child(self, command: list[str]) -> int:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        # Match the pipe decoder, including Python grandchildren, without
        # changing the desktop's environment or dropping proxy settings.
        child_env = os.environ.copy()
        child_env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        try:
            child = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=child_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                startupinfo=startupinfo,
                start_new_session=os.name != "nt",
            )
        except OSError as error:
            self.events.put(("log", f"启动子进程失败：{error}\n"))
            return 1
        with self._process_lock:
            self.process = child
            should_stop = self.stop_requested
        if should_stop:
            self._request_child_stop(child)
        assert child.stdout is not None
        buffer: list[str] = []
        line_overflow = False
        prompt_tail = ""
        last_progress = ""
        try:
            while True:
                char = child.stdout.read(1)
                if char == "" and child.poll() is not None:
                    break
                if char == "" and child.poll() is None:
                    time.sleep(0.03)
                    continue
                # Never split an assignment before redaction: a key and its
                # value can straddle an arbitrary display-sized chunk.
                if not line_overflow:
                    buffer.append(char)
                    if len(buffer) > MAX_CHILD_LINE_CHARS:
                        buffer.clear()
                        line_overflow = True
                prompt_tail = (prompt_tail + char)[-128:]
                prompt = any(prompt_tail.endswith(token) for token in ("按 Enter", "按回车", "输入 q 退出"))
                if prompt and not self.waiting_for_human:
                    self.waiting_for_human = True
                    self.events.put(("human_wait", True))
                if char in ("\n", "\r"):
                    line = "[日志行过长，已隐藏整行以保护敏感信息]\n" if line_overflow else "".join(buffer)
                    buffer.clear()
                    line_overflow = False
                    prompt_tail = ""
                    self._queue_log(line)
                    progress = parse_progress(redact_secrets(line))
                    if progress:
                        last_progress = progress
                        self.events.put(("progress", last_progress))
                    if any(token in line for token in ("PAUSE", "[暂停]", "登录状态异常", "触发安全验证", "需要人工验证")):
                        self.events.put(("status", "等待人工登录或验证"))
        finally:
            if buffer or line_overflow:
                line = "[日志行过长，已隐藏整行以保护敏感信息]\n" if line_overflow else "".join(buffer)
                self._queue_log(line)
            code = child.wait()
            child.stdout.close()
            if child.stdin:
                child.stdin.close()
            with self._process_lock:
                self.process = None
                if self._stopping_process is child:
                    self._stopping_process = None
            self.waiting_for_human = False
            self.events.put(("human_wait", False))
        return code

    def _continue_child(self) -> None:
        with self._process_lock:
            child = self.process
        if child is None or child.stdin is None:
            if not self.workflow_running and getattr(self, "_pause_requested", False) and self.active_run:
                self.resume_run.set(str(self.active_run))
                self._start()
            return
        try:
            child.stdin.write("\n")
            child.stdin.flush()
            self.waiting_for_human = False
            self.continue_button.configure(state="disabled")
            self.status_text.set("已发送继续信号")
            if getattr(self, "_workspace_ready", False):
                self._workspace_event("human_wait", False)
        except OSError as error:
            messagebox.showerror("无法继续", str(error), parent=self.root)

    def _stop(self, pause: bool = False) -> None:
        self._flush_logs()
        self._pause_requested = pause
        self.stop_requested = True
        with self._process_lock:
            child = self.process
        if child is None:
            self.status_text.set("将在当前阶段完成后停止")
            return
        self.status_text.set("正在安全停止并保留检查点…")
        self._request_child_stop(child)

    def _request_child_stop(self, child: subprocess.Popen[str]) -> None:
        with self._process_lock:
            if self._stopping_process is child:
                return
            self._stopping_process = child
        try:
            if os.name == "nt":
                child.send_signal(getattr(__import__("signal"), "CTRL_BREAK_EVENT"))
            else:
                os.killpg(child.pid, signal.SIGTERM)
        except (OSError, ValueError):
            try:
                child.terminate()
            except OSError:
                pass

        def force_stop() -> None:
            try:
                child.wait(timeout=STOP_GRACE_MS / 1000)
            except subprocess.TimeoutExpired:
                if os.name == "nt" and child.pid:
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                        return
                    except OSError:
                        pass
                elif os.name != "nt":
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                        return
                    except OSError:
                        pass
                try:
                    child.kill()
                except OSError:
                    pass

        threading.Thread(target=force_stop, name="desktop-child-stop", daemon=True).start()

    def _open_run_directory(self) -> None:
        target = self._artifact_run()
        if not target or not target.exists():
            messagebox.showinfo("运行目录", "当前还没有创建运行目录。", parent=self.root)
            return
        self._open_path(target)

    def _open_report(self) -> None:
        target = self._artifact_run()
        report = (target / "prepared" / "海投分层清单.md") if target else None
        if not report or not report.exists():
            messagebox.showinfo("报告", "当前运行还没有生成分层报告。", parent=self.root)
            return
        self._open_path(report)

    def _open_run_json(self) -> None:
        target = self._artifact_run()
        json_path = target / "run_manifest.json" if target else None
        if json_path and not json_path.exists():
            collection_manifest = target / "collection_manifest.json"
            json_path = collection_manifest if collection_manifest.exists() else json_path
        if not json_path or not json_path.exists():
            messagebox.showinfo("运行 JSON", "当前运行还没有生成运行清单 JSON。", parent=self.root)
            return
        self._open_path(json_path)

    def _open_path(self, path: Path) -> None:
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as error:
            messagebox.showerror("无法打开文件", str(error), parent=self.root)

    def _poll_events(self) -> None:
        try:
            # A verbose child must not starve Tk's redraw and input queue.
            for _ in range(200):
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._record_log(value)
                elif kind == "log_write_error":
                    # Do not write this warning back into the failing sink.
                    self._append_log(LOG_WRITE_WARNING)
                elif kind == "status":
                    self.status_text.set(value)
                elif kind == "progress":
                    self.progress_text.set(value)
                elif kind in ("route_preview", "route_preview_error"):
                    request_id, description = value
                    if request_id != getattr(self, "_route_request_id", None) or self._closing:
                        continue
                    self.routing_summary.set(description if kind == "route_preview" else f"暂不能预览：{description}")
                elif kind == "route_started":
                    self._route_previous_context = None
                    self.active_run = Path(value["output"])
                    self.current_log = self.active_run / "desktop.log"
                    self.output_directory.set(f"当前运行目录：{self.active_run}")
                    if self.data_root.get() != value["source_root"]:
                        self.data_root.set(value["source_root"])
                    self.routing_summary.set(value["summary"])
                    self._record_log(f"\n=== 运行开始 {datetime.now().astimezone().isoformat(timespec='seconds')} ===\n")
                    self._record_log(f"城市={value['city']} | 轨道={value['track']} | 阶段={value['stage']}\n")
                    self._record_log(f"采集日目录：{value['source_root']}\n当前输出：{self.active_run}\n")
                elif kind == "route_failed":
                    self.workflow_running = False
                    previous = getattr(self, "_route_previous_context", None)
                    if previous is not None:
                        self.active_run, self.current_log, previous_text = previous
                        self.output_directory.set(previous_text)
                    self._route_previous_context = None
                    self._pause_requested = False
                    description = redact_secrets(str(value["message"]))
                    self.routing_summary.set(description)
                    self.status_text.set("已停止（未开始运行）" if value.get("cancelled") else "阶段输入不可用，请检查下方提示")
                    self.run_summary.set(description + "；未覆盖原任务、检查点或报告。")
                    self.progress_text.set("")
                    self._append_log(f"阶段输入未就绪：{description}\n")
                    self._set_busy(False)
                    if getattr(self, "_workspace_ready", False):
                        self.progress_bar.stop()
                        self.progress_bar.configure(mode="determinate", value=0)
                        self.live_detail.set(description)
                    if self._closing:
                        self._finalize_close()
                        return
                    if not value.get("cancelled"):
                        messagebox.showerror("阶段输入不可用", description, parent=self.root)
                elif kind == "human_wait":
                    enabled = bool(value)
                    self.continue_button.configure(state="normal" if enabled else "disabled")
                    self.status_text.set("请在专用 Chrome 中完成登录或验证，然后点击“人工处理完成，继续”。" if enabled else "正在运行")
                elif kind == "stats":
                    request_id, info = value
                    if request_id != self._stats_request_id:
                        continue
                    self.stats_text.set(
                        f"原始记录 {info.get('raw_record_count', 0):,} 条 | 去重岗位 {info.get('deduplicated_job_count', 0):,} 个 | "
                        f"有效列表组合 {info.get('raw_combination_count', info.get('combination_count', 0))}/"
                        f"预期 {info.get('expected_combination_count', 0)} 组 | 已完成 {info.get('completed_combination_count', 0)} | "
                        f"未完成 {info.get('pending_combination_count', 0)} | 状态：{info.get('manifest_status_counts', {})}"
                    )
                elif kind == "stats_error":
                    request_id, error = value
                    if request_id != self._stats_request_id:
                        continue
                    self.stats_text.set(f"统计暂不可用：{redact_secrets(str(error))}")
                elif kind == "finished":
                    self._flush_logs()
                    self.workflow_running = False
                    self.active_run = Path(value["output"])
                    self.output_directory.set(f"当前运行目录：{self.active_run}")
                    manifest = value.get("manifest", {})
                    if value["exit_code"] == 0 and manifest.get("status") == "partial":
                        self.status_text.set("部分完成（见下方待核实事项）")
                    elif manifest.get("status") == "paused":
                        self.status_text.set("已暂停，检查点已保存；可点击“继续此任务”" if manifest.get("pause_reason") == "user_requested" else "已暂停：请处理登录、安全验证或 CDP 后续跑")
                    else:
                        self.status_text.set("运行完成" if value["exit_code"] == 0 else f"运行结束（退出码 {value['exit_code']}），可续跑")
                    source = manifest.get("source_stats", {})
                    classes = manifest.get("classification_counts", {})
                    jd = manifest.get("jd_manifest", {}).get("records", [])
                    llm = manifest.get("llm_manifest", {})
                    jd_status = {}
                    for record in jd:
                        status = record.get("status", "unknown")
                        jd_status[status] = jd_status.get(status, 0) + 1
                    if manifest.get("error"):
                        self.run_summary.set(f"工作流错误：{redact_secrets(str(manifest['error']))}")
                    else:
                        self.run_summary.set(
                            f"城市原始 {format_count(source.get('raw_record_count'))} 条 / 去重 {format_count(source.get('deduplicated_job_count'))} 个；"
                            f"JD 批次 {jd_status if jd else '未生成检查点'}；LLM 成功 {format_count(llm.get('completed_count'))}、回退 {format_count(llm.get('fallback_count'))}、失败 {format_count(llm.get('failed_count'))}；"
                            f"重点 {format_count(classes.get('重点投递'))}、海投 {format_count(classes.get('海投池'))}、暂不投 {format_count(classes.get('暂不投'))}。"
                        )
                        warnings = manifest.get("warnings") or []
                        if warnings:
                            self.run_summary.set(self.run_summary.get() + "；待核实：" + "；".join(redact_secrets(str(item)) for item in warnings))
                    self.progress_text.set("")
                    self._set_busy(False)
                    self._refresh_history()
                    self._refresh_stats()
                    if hasattr(self, "routing_summary"):
                        self._request_route_preview()
                    if self._closing:
                        self._finalize_close()
                        return
                elif kind == "browser_ready":
                    self._flush_logs()
                    self.workflow_running = False
                    code = value["exit_code"]
                    self._browser_prepared = code == 0
                    self.status_text.set("专用浏览器已准备并确认登录" if code == 0 else f"浏览器准备未完成（退出码 {code}）")
                    self.run_summary.set("准备流程只写入系统临时目录，不修改所选采集结果目录。")
                    self._set_busy(False)
                    if self._closing:
                        self._finalize_close()
                        return
                if getattr(self, "_workspace_ready", False):
                    self._workspace_event(kind, value)
        except queue.Empty:
            pass
        if not self._destroyed():
            self._poll_after_id = self.root.after(100, self._poll_events)

    def _destroyed(self) -> bool:
        try:
            return not bool(self.root.winfo_exists())
        except (AttributeError, tk.TclError):
            return False

    def _finalize_close(self) -> None:
        if self._destroyed():
            return
        if self._poll_after_id:
            try:
                self.root.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        if self.worker and self.worker.is_alive():
            self._poll_after_id = self.root.after(100, self._finalize_close)
            return
        # The worker is finished. Drain queued final lines in bounded Tk turns
        # before placing the writer's final FIFO close barrier.
        for _ in range(200):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._record_log(value)
            elif kind == "log_write_error":
                self._append_log(LOG_WRITE_WARNING)
        if not self.events.empty():
            self._poll_after_id = self.root.after(50, self._finalize_close)
            return
        writer = getattr(self, "_log_writer", None)
        if writer is not None and not writer.close().is_set():
            self._poll_after_id = self.root.after(50, self._finalize_close)
            return
        if getattr(self, "_route_after_id", None):
            try:
                self.root.after_cancel(self._route_after_id)
            except tk.TclError:
                pass
            self._route_after_id = None
        if getattr(self, "_workspace_ready", False):
            self._close_workspace()
        self.root.destroy()

    def _close(self) -> None:
        if self._closing:
            return
        if self.workflow_running:
            if not messagebox.askyesno("正在运行", "程序仍在执行。停止并关闭窗口吗？", parent=self.root):
                return
            self._closing = True
            self._stop()
            self.status_text.set("正在停止任务；窗口将在子进程退出后关闭…")
            return
        self._closing = True
        self._finalize_close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BOSS 岗位采集与处理桌面程序")
    parser.add_argument("--root", type=Path, default=None, help="采集结果根目录")
    parser.add_argument("--scraper-root", default=None, help="上游 boss-zhipin-scraper 目录")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="岗位检索配置")
    parser.add_argument("--legacy-workbench", action="store_true", help="独立打开旧版采集工作台")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = tk.Tk()
    if args.legacy_workbench:
        CollectorDesktopApp(root, args)
    else:
        from desktop_review_ui import ReviewDesktopApp
        ReviewDesktopApp(root, args)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
