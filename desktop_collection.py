"""Embeddable raw-list collection page; deliberately contains no review steps.

Only explicit Start / Prepare actions can launch ``collect_architecture.py``.
Read-only previews, disk statistics and child I/O run outside the Tk thread;
the queue is the only route back to widgets or host callbacks.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any, Callable, Mapping

import collect_architecture as collector
import collection_plan as plans
from collection_sources import assert_source_specs_compatible
from desktop_data import _redact_text
from desktop_theme import LIGHT_PALETTE, configure_combobox_popdown
from desktop_tk_lifecycle import StringVar as _PageStringVar, destroy_tk_widget, install_tk_lifecycle
from project_resources import PROJECT_ROOT, default_state_root
# These helpers start an OS-suspended Windows child, bind its descendants to
# an anonymous kill-on-close job, and never target an existing browser / port.
# Keep ownership and the final cancellation/normal-detach decision together.
from review_pipeline import StageControl, _child_options, _pid_alive, _stop_owned_child, _WindowsProcessTree


CITY_CHOICES = ("全部城市", "上海", "深圳", "杭州", "广州", "成都", "北京")
TRACK_CHOICES = ("all", "current", "target")
TRACK_LABELS = {"all": "全部分组", "current": "当前求职", "target": "成长参考"}
TRACK_KEYS = {value: key for key, value in TRACK_LABELS.items()}
BROWSER_MODES = {"dual": "Chrome + Edge", "chrome": "仅 Chrome"}
BROWSER_NAMES = {"chrome": "Chrome", "edge": "Edge"}
DEFAULT_BROWSER_WORKERS = 3
MAX_BROWSER_WORKERS = 8
GATE_STOP_FILE_ENV = "BOSS_COLLECTION_GATE_STOP_FILE"
PLAN_DIRECTORY = default_state_root() / "config" / "collection-plans"
STATUS_LABELS = {
    "success": "成功", "no_data": "确认无数据", "failed": "失败 / 可重试",
    "paused": "暂停 / 待处理", "running": "运行中", "pending": "未记录", "unknown": "未知状态",
}
MAX_LOG_LINE = 64 * 1024
UI_LOG_LINES = 1800
CHILD_DRAIN_TIMEOUT = 2.0
_RESERVED_PARTS = frozenset(("raw", "prepared", "runtime", "checkpoints", "runs"))
_PAGE_RE = re.compile(r"\[(\d+)\s*/\s*(\d+)\s*页")
_RUN_RE = re.compile(r"^RUN\s+(\S+)")
_DONE_RE = re.compile(r"^(?:DONE|SKIP)\s+(\S+)")
_PROMPT_RE = re.compile(r"(?:按\s*(?:Enter|回车)|Enter\s*(?:继续|重试))[^\r\n]{0,100}[:：]\s*$", re.I)


def redact_log(value: object) -> str:
    """Use both existing log policies, including whole Cookie/Auth headers."""
    return _redact_text(collector.redact_log(str(value)))


def _integer(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _positive(text: str, label: str, *, optional: bool = False, maximum: int | None = None) -> int | None:
    text = text.strip()
    if optional and not text:
        return None
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError(f"{label}必须是正整数" + ("，留空表示全部。" if optional else "。"))
    number = int(text)
    if number < 1 or (maximum is not None and number > maximum):
        raise ValueError(f"{label}必须在 1–{maximum} 之间。" if maximum else f"{label}必须大于 0。")
    return number


@dataclass(frozen=True)
class CollectionSelection:
    city: str = "全部城市"
    track: str = "all"
    limit: int | None = None
    pages: int = 10

    @classmethod
    def parse(cls, city: str, track: str, limit: str, pages: str) -> CollectionSelection:
        city = "全部城市" if city.strip() == "全部" else city.strip()
        track = TRACK_KEYS.get(track.strip(), track.strip())
        if city not in CITY_CHOICES:
            raise ValueError("城市请选择全部、上海、深圳、杭州、广州、成都或北京。")
        if track not in TRACK_CHOICES:
            raise ValueError("检索分组请选择全部分组、当前求职或成长参考。")
        return cls(city, track, _positive(limit, "本次最多检索组数", optional=True), int(_positive(pages, "每组页数", maximum=10)))

    @property
    def cities(self) -> list[str] | None:
        return None if self.city == "全部城市" else [self.city]


@dataclass(frozen=True)
class CollectionRequest:
    root_text: str
    scraper_text: str
    config_path: Path
    selection: CollectionSelection
    config_snapshot: dict[str, Any] | None = field(default=None, repr=False)
    combination_ids: tuple[str, ...] | None = None
    browser_mode: str = "dual"
    page_wait_min: float = collector.DEFAULT_PAGE_WAIT_MIN
    page_wait_max: float = collector.DEFAULT_PAGE_WAIT_MAX
    browser_workers: int = DEFAULT_BROWSER_WORKERS


def _validated_page_config(config: dict[str, Any]) -> dict[str, Any]:
    config = plans.validate_plan_config(config)
    if config["degree"]["id"] != "203":
        raise ValueError("本采集页仅支持本科（学历编码 203）；请加载本科方案，未更改原配置。")
    # Education is fixed by its real upstream code, not an imported label.
    config["degree"]["name"] = "本科"
    return config


def build_collection_preview(config: dict[str, Any], selection: CollectionSelection,
                             combination_ids: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Expand candidates once; checkbox selection precedes the existing CLI limit.

    The catalog remains intact. Clearing only the explicit combination mask
    lets users see and re-enable candidates excluded by a loaded plan.
    """
    config = _validated_page_config(config)
    candidates = plans.select_plan_combinations(config, None)
    all_specs = collector.build_specs(candidates)
    known = {spec["combination"] for spec in all_specs}
    checked = ({spec["combination"] for spec in collector.build_specs(config)}
               if combination_ids is None else set(combination_ids))
    if checked - known:
        raise ValueError("勾选组合与当前方案不匹配；请重新加载方案后确认选择。")
    visible = collector.build_specs(candidates, selection.track, cities=selection.cities)
    chosen = [spec for spec in visible if spec["combination"] in checked]
    execute = chosen[:selection.limit] if selection.limit is not None else chosen
    execute_ids = {spec["combination"] for spec in execute}
    rows = plans.preview_rows(candidates, visible, pages=selection.pages)
    for index, row in enumerate(rows, 1):
        combination = row["combination"]
        row.update(index=index, checked=combination in checked,
                   track_label=TRACK_LABELS.get(row["track"], row["track"]),
                   execution_status=("本次执行" if combination in execute_ids else
                                     "超出本次上限" if combination in checked else "未勾选"))
    return {"config": config, "plan_rows": rows, "all_checked": checked,
            "execution_specs": execute, "estimate": len(execute), "selected_count": len(chosen),
            "full": len(visible), "editor_values": plans.plan_editor_values(config)}


def _reparse(path: Path) -> bool:
    try:
        return (path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                or bool(getattr(path.lstat(), "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))
    except FileNotFoundError:
        return False


def validate_collection_root(value: str | Path) -> Path:
    """Accept a new or existing collection root, never an output/task subfolder.

    This is read-only. Do not call the CLI's load_manifest here: it writes a
    .corrupt backup, which is inappropriate for an initial preview.
    """
    if not str(value).strip():
        raise ValueError("请填写独立的采集输出根目录（可为尚不存在的新目录）。")
    candidate = Path(str(value).strip()).expanduser().absolute()
    if candidate.exists() and not candidate.is_dir():
        raise ValueError("采集输出根必须是文件夹，不能选择文件。")
    if candidate == Path(candidate.anchor):
        raise ValueError("请使用专用的采集文件夹，不能直接写入磁盘根目录。")
    if any(part.casefold() in _RESERVED_PARTS for part in candidate.parts):
        raise ValueError("请选择采集根目录，不要选择 raw/current、raw/target、prepared、runs、checkpoints 或任务 runtime 子目录。")
    for ancestor in (candidate, *candidate.parents):
        if _reparse(ancestor):
            raise ValueError("采集路径含符号链接或目录联接；请使用普通文件夹以免写入其他数据。")
        if (ancestor / "task_manifest.json").exists():
            raise ValueError("不能将后处理任务或其 runtime 作为采集输出；请另选独立采集根目录。")
        if ancestor != candidate and (ancestor / "collection_manifest.json").exists():
            raise ValueError("所选位置是已有采集根的子目录；请直接选择含 collection_manifest.json 的根目录。")
    return candidate.resolve()


def _guard_output_paths(root: Path, specs: list[dict[str, Any]]) -> None:
    """Reject pre-existing links/file collisions in exactly the CLI write set."""
    directories = {root, root / "raw", root / "checkpoints", root / "collection-plans"}
    files = {root / name for name in (
        "collection_manifest.json", "collection_manifest.json.tmp", "collection_manifest.json.lock",
        "collection_manifest.json.corrupt", "collection-desktop.log",
    )}
    for spec in specs:
        track, city, combination = (str(spec[key]) for key in ("track", "city_id", "combination"))
        # Config is local, but it must not turn a combination into a path.
        if track not in ("current", "target") or any(
                not part or part in (".", "..") or any(char in part for char in '/\\:')
                for part in (city, combination)):
            raise ValueError("采集配置含不安全的组合或城市路径，未启动采集。")
        folder = root / "raw" / track / city
        directories.update((folder.parent, folder))
        files.update((folder / f"{combination}.json", folder / f"{combination}.log",
                      root / "checkpoints" / f"{combination}.partial.json"))
    for path in directories | files:
        if _reparse(path):
            raise ValueError("采集输出含符号链接或目录联接；未启动，以保护原有数据。")
        if path.exists() and ((path in directories and not path.is_dir()) or (path in files and not path.is_file())):
            raise ValueError("采集输出存在文件/目录冲突；请检查路径或选择新的采集根。")


def read_collection_progress(root: Path) -> dict[str, Any]:
    """Read recorded progress only; missing/corrupt totals remain unknown."""
    empty: dict[str, Any] = {
        "root": str(root), "manifest_state": "missing", "total": None, "done": 0,
        "counts": {key: 0 for key in STATUS_LABELS if key != "pending"},
        "jobs": 0, "pages_completed": 0, "pages_requested": None, "rows": [],
        "message": "尚无 collection_manifest.json；未开始或尚未落盘，总量未知。",
    }
    path = root / "collection_manifest.json"
    if not path.exists():
        return empty
    try:
        if _reparse(path):
            raise ValueError("linked manifest")
        value = collector.read_json(path)
        if not isinstance(value, dict) or not isinstance(value.get("records"), list):
            raise ValueError("invalid manifest structure")
        if "expected_combinations" in value and (
                not isinstance(value["expected_combinations"], list)
                or any(not isinstance(item, str) or not item for item in value["expected_combinations"])):
            raise ValueError("invalid expected combinations")
        if any(not isinstance(record, dict) or not isinstance(record.get("combination"), str)
               or not record["combination"] for record in value["records"]):
            raise ValueError("invalid records")
    except (OSError, UnicodeError, ValueError, TypeError):
        return {**empty, "manifest_state": "invalid",
                "message": "collection_manifest.json 无效或不可读；进度未知，未改写原清单，请检查或选择新根目录。"}
    expected = list(dict.fromkeys(value["expected_combinations"])) if "expected_combinations" in value else None
    records = {record["combination"]: record for record in value["records"]}
    selected = expected if expected is not None else list(records)
    specs = {str(spec["combination"]): spec for spec in value.get("expected_combination_specs", [])
             if isinstance(spec, dict) and isinstance(spec.get("combination"), str)} if isinstance(value.get("expected_combination_specs", []), list) else {}
    counts = dict(empty["counts"])
    rows = []
    jobs = pages_completed = 0
    requested_pages: list[int | None] = []
    global_pages = _integer(value.get("pages"))
    for combination in selected:
        record = records.get(combination)
        spec = specs.get(combination, {})
        status = str(record.get("status", "unknown")) if record else "pending"
        if record:
            counts[status if status in counts else "unknown"] += 1
            jobs += _integer(record.get("job_count")) or 0
            pages_completed += _integer(record.get("pages_completed")) or 0
        requested = next((number for number in (
            _integer((record or {}).get("pages_requested")), _integer(spec.get("pages_requested")), global_pages,
        ) if number is not None and number > 0), None)
        requested_pages.append(requested)
        row = {**spec, **(record or {}), "combination": combination, "status": status, "pages_requested": requested}
        rows.append(row)
    total = len(expected) if expected is not None else None
    # A resume may change the CLI's global pages while old successful groups
    # are skipped unchanged. Their recorded request remains authoritative.
    page_total = sum(number for number in requested_pages if number is not None) if (
        total is not None and all(number is not None for number in requested_pages)) else None
    done = counts["success"] + counts["no_data"]
    message = (f"以落盘清单为准：已完成 {done}/{total} 组。" if total is not None
               else "清单未记录预计组合总量；仅展示已落盘记录，总量未知。")
    return {**empty, "manifest_state": "ok", "total": total, "done": done, "counts": counts,
            "jobs": jobs, "pages_completed": pages_completed, "pages_requested": page_total,
            "rows": rows, "message": message}


def make_collect_command(request: CollectionRequest, root: Path, scraper_root: Path, *, setup_only: bool = False,
                         browser: str = "chrome", shard: int = 0, total_shards: int = 1,
                         setup_browser: bool = True) -> list[str]:
    """The sole launched entry point is the resumable raw-list CLI."""
    selection = request.selection
    command = [sys.executable, "-u", str(PROJECT_ROOT / "collect_architecture.py"),
               "--config", str(request.config_path), "--root", str(root),
               "--scraper-root", str(scraper_root), "--batch", selection.track,
               "--pages", str(selection.pages), "--gui-interactive", "--quiet-upstream",
               "--wait-on-gate", "--browser", browser,
               "--cdp-port", str(9223 if browser == "edge" else 9222),
               "--profile-dir", str(collector.default_profile_dir(browser)),
               "--shard", str(shard), "--total-shards", str(total_shards),
               "--page-wait-min", str(request.page_wait_min), "--page-wait-max", str(request.page_wait_max)]
    if setup_browser or setup_only:
        command.append("--setup-browser")
    if selection.cities:
        command.extend(("--cities", *selection.cities))
    if selection.limit is not None:
        command.extend(("--limit", str(selection.limit)))
    if setup_only:
        command.append("--setup-only")
    return command


class CollectionControl(StageControl):
    """One cancellation decision shared by both owned browser lanes."""


@dataclass
class CommandResult:
    code: int
    pid: int | None = None
    combination: str | None = None
    pages_completed: int = 0
    page_markers: int = 0
    completed: set[str] = field(default_factory=set)
    collector_pid: int | None = None


class _OwnedCommand:
    """One interactive owned tree, including output without a trailing newline."""
    def __init__(self) -> None:
        self.child: subprocess.Popen[str] | None = None
        self.reader: threading.Thread | None = None
        self.tree: _WindowsProcessTree | None = None
        self.result: CommandResult | None = None
        self.continue_requested = threading.Event()
        self._gate_lock = threading.Lock()
        self._waiting = False
        self._can_preserve_tree = False

    def request_continue(self) -> bool:
        """Accept confirmation only for this currently waiting process."""
        with self._gate_lock:
            if (not self._waiting or self.continue_requested.is_set()
                    or self.child is None or self.child.poll() is not None):
                return False
            self.continue_requested.set()
            return True

    @property
    def command_complete(self) -> bool:
        return ((self.child is None or self.child.poll() is not None)
                and (self.reader is None or not self.reader.is_alive()))

    @property
    def shutdown_complete(self) -> bool:
        return self.tree is None and self.command_complete

    def release_tree(self, *, preserve_detached: bool) -> None:
        """Caller serializes the group's final detach with cancellation."""
        if self.tree:
            self.tree.close(preserve_detached=preserve_detached and self._can_preserve_tree)
            self.tree = None

    def run(self, command: list[str], control: CollectionControl,
            emit: Callable[[str, Any], None], monitor: Callable[[], None] | None = None, *,
            hold_tree: bool = False, extra_env: Mapping[str, str] | None = None) -> CommandResult:
        if control.requested:
            return CommandResult(130)
        environment = os.environ.copy()
        environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        environment.pop(GATE_STOP_FILE_ENV, None)
        if extra_env:
            environment.update(extra_env)
        owner_token = secrets.token_hex(16)
        environment[collector.DESKTOP_OWNER_TOKEN_ENV] = owner_token
        for key in ("NO_PROXY", "no_proxy"):
            values = [value for value in environment.get(key, "").split(",") if value]
            environment[key] = ",".join(dict.fromkeys((*values, "localhost", "127.0.0.1", "::1")))
        child = self.child = subprocess.Popen(
            command, cwd=PROJECT_ROOT, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1, **_child_options())
        output: queue.Queue[tuple[str, Any] | None] = queue.Queue(maxsize=512)
        reader_stop = threading.Event()
        result = self.result = CommandResult(1, child.pid)
        eof = False
        waiting = False
        stopping = False
        last_output = time.monotonic()
        exited_at: float | None = None
        drain_failed = False
        drain_started: float | None = None
        last_monitor = 0.0

        def deliver(item: tuple[str, Any] | None) -> None:
            while not reader_stop.is_set():
                try:
                    output.put(item, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def read_output() -> None:
            assert child.stdout is not None
            parts: list[str] = []
            overflow = prompted = False

            def publish_line() -> None:
                if overflow:
                    deliver(("line", "[日志行过长，已隐藏整行以保护敏感信息]"))
                    return
                line = "".join(parts)
                if line.startswith(collector.DESKTOP_OWNER_PREFIX):
                    owner = re.fullmatch(rf"{collector.DESKTOP_OWNER_PREFIX} token={owner_token} pid=([1-9][0-9]*)", line)
                    if owner:
                        deliver(("owner", int(owner[1])))
                    else:
                        deliver(("line", "[忽略不匹配的采集器进程身份消息]"))
                else:
                    deliver(("line", redact_log(line)))
            try:
                while not reader_stop.is_set():
                    char = child.stdout.read(1)
                    if not char:
                        break
                    if char in "\r\n":
                        if parts or overflow:
                            publish_line()
                        parts, overflow, prompted = [], False, False
                    elif not overflow:
                        parts.append(char)
                        if len(parts) > MAX_LOG_LINE:
                            parts, overflow = [], True
                        elif char in ":：" and not prompted and _PROMPT_RE.search("".join(parts[-512:])):
                            # No partial text is published: a split credential
                            # must never leak while a prompt is still arriving.
                            deliver(("waiting", True))
                            prompted = True
                if parts or overflow:
                    publish_line()
            except (OSError, ValueError):
                if not reader_stop.is_set():
                    deliver(("line", "[本次采集输出已关闭；状态以落盘清单为准]"))
            finally:
                try:
                    child.stdout.close()
                except (OSError, ValueError):
                    pass
                deliver(None)

        preserve_detached = False
        try:
            if os.name == "nt":
                self.tree = _WindowsProcessTree(child)
            self.reader = threading.Thread(target=read_output, name="collection-output", daemon=True)
            self.reader.start()
            if control.requested:
                _stop_owned_child(child, self.tree)
                stopping = True
            elif self.tree:
                self.tree.resume()
            while not eof or child.poll() is None:
                if control.requested and not stopping:
                    _stop_owned_child(child, self.tree)
                    stopping = True
                if self.continue_requested.is_set():
                    self.continue_requested.clear()
                    if waiting and not control.requested and child.poll() is None:
                        try:
                            assert child.stdin is not None
                            child.stdin.write("\n")
                            child.stdin.flush()
                            waiting = False
                            with self._gate_lock:
                                self._waiting = False
                            emit("waiting", False)
                            emit("log", "已由用户确认人工处理完成，向本次采集器发送 Enter。")
                        except (OSError, ValueError):
                            emit("log", "采集器已结束或关闭输入；请按落盘清单使用开始 / 续采。")
                try:
                    item = output.get(timeout=0.05)
                    if item is None:
                        eof = True
                    else:
                        kind, value = item
                        last_output = time.monotonic()
                        if kind == "owner":
                            if result.collector_pid is None:
                                result.collector_pid = int(value)
                                emit("log", f"已确认本次采集器 PID {value}（启动根 {result.pid}）。")
                        elif kind == "waiting":
                            waiting = True
                            with self._gate_lock:
                                self._waiting = True
                            emit("waiting", True)
                        else:
                            line = str(value)
                            run = _RUN_RE.match(line)
                            if run:
                                result.combination = run[1]
                                result.pages_completed = result.page_markers = 0
                                emit("position", line)
                            done = _DONE_RE.match(line)
                            if done:
                                result.completed.add(done[1])
                            pages = _PAGE_RE.findall(line)
                            if pages:
                                result.pages_completed = max(result.pages_completed, *(int(page) for page, _ in pages))
                                result.page_markers += len(pages)
                                emit("position", f"{result.combination} · {line}" if result.combination else line)
                            emit("log", line)
                except queue.Empty:
                    pass
                now = time.monotonic()
                if child.poll() is not None and not eof:
                    exited_at = exited_at or now
                    if not drain_failed and (now - max(exited_at, last_output) >= CHILD_DRAIN_TIMEOUT or now - exited_at >= 10):
                        drain_failed, drain_started = True, now
                        emit("log", "采集器已退出，但本次后代仍占用输出；已结束自有进程组，保留落盘检查点。")
                        _stop_owned_child(child, self.tree)
                    elif drain_failed and drain_started is not None and now - drain_started >= CHILD_DRAIN_TIMEOUT:
                        break
                if monitor and now - last_monitor >= 0.7:
                    monitor()
                    last_monitor = now
            result.code = child.wait()
            if drain_failed and result.code == 0:
                result.code = 1
            if monitor:
                monitor()
            preserve_detached = result.code == 0 and not control.requested and not drain_failed and eof
            return result
        except BaseException:
            _stop_owned_child(child, self.tree)
            raise
        finally:
            with self._gate_lock:
                self._waiting = False
            self.continue_requested.clear()
            self._can_preserve_tree = preserve_detached
            if self.tree:
                # Same lock as Stop/Pause: a late cancellation cannot detach a
                # newly launched browser after the user has requested Close.
                if not hold_tree:
                    with control._lock:
                        self.release_tree(preserve_detached=control._requested is None)
            elif control.requested or not preserve_detached:
                _stop_owned_child(child)
            if child.stdin:
                try:
                    child.stdin.close()
                except (OSError, ValueError):
                    pass
            reader_stop.set()
            if self.reader and self.reader.ident is not None:
                self.reader.join(timeout=0.8)
            elif child.stdout:
                child.stdout.close()


@dataclass
class _CollectionSlot:
    number: int
    shard: int
    total_shards: int
    runner: _OwnedCommand = field(default_factory=_OwnedCommand)


@dataclass
class _CollectionLane:
    browser: str
    shard: int
    total_shards: int
    runner: _OwnedCommand = field(default_factory=_OwnedCommand)
    slots: tuple[_CollectionSlot, ...] = ()
    gate_file: Path | None = None

    @property
    def all_runners(self) -> tuple[_OwnedCommand, ...]:
        return tuple(dict.fromkeys((self.runner, *(slot.runner for slot in self.slots))))

    @property
    def label(self) -> str:
        return BROWSER_NAMES[self.browser]

    @property
    def port(self) -> int:
        return 9223 if self.browser == "edge" else 9222


def _clear_owned_lock(root: Path, result: CommandResult, log: Callable[[str], None]) -> None:
    """A forced exit may leave the CLI's short manifest lock behind.

    Accept the Popen root or its token-confirmed interpreter (venv launcher).
    Both known processes must have exited before their lock can be removed.
    A lock owned by another collector is never touched or waited on.
    """
    path = root / "collection_manifest.json.lock"
    if result.pid is None or not path.is_file() or _reparse(path):
        return
    try:
        owners = re.findall(r"(?m)^pid=([1-9][0-9]*)\s*$", path.read_text(encoding="utf-8"))
        owned_pids = {pid for pid in (result.pid, result.collector_pid) if pid is not None}
        if len(owners) == 1 and int(owners[0]) in owned_pids and not any(_pid_alive(pid) for pid in owned_pids):
            path.unlink()
            log("已清理本次退出的采集器留下的清单锁；其他进程的锁不会改动。")
    except (OSError, UnicodeError):
        log("本次采集锁暂不可清理；已保留原文件，请检查后续采。")


def preserve_interrupted_combination(root: Path, specs: list[dict[str, Any]], pages: int,
                                     result: CommandResult, requested: str, started_at: str,
                                     log: Callable[[str], None]) -> None:
    """Persist a real interruption and retain the CLI's existing checkpoint.

    Completed records are never demoted. If a raw partial file was published
    before the owned tree stopped, copy it to the standard merge checkpoint
    only when that checkpoint does not already exist, matching the CLI.
    """
    _clear_owned_lock(root, result, log)
    if not result.combination or result.combination in result.completed:
        return
    spec = next((item for item in specs if item["combination"] == result.combination), None)
    if spec is None:
        return
    _guard_output_paths(root, [spec])
    combination = spec["combination"]
    output = root / "raw" / spec["track"] / spec["city_id"] / f"{combination}.json"
    checkpoint = root / "checkpoints" / f"{combination}.partial.json"
    count = collector.payload_job_count(output)
    if count is not None and not checkpoint.exists():
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output, checkpoint)
    manifest_path = root / "collection_manifest.json"
    lock_path = manifest_path.with_name(manifest_path.name + ".lock")
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        log("另一采集器仍持有清单锁；未覆盖清单，现有 raw 与检查点均已保留。")
        return
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\ncreated_at={collector.now_iso()}\n")
        descriptor = None
        if manifest_path.exists():
            manifest = collector.read_json(manifest_path)
            if not isinstance(manifest, dict) or not isinstance(manifest.get("records"), list):
                log("清单不可读；未替换原清单，现有 raw 与检查点均已保留。")
                return
        else:
            manifest = {"schema_version": "1.0", "created_at": collector.now_iso(), "records": []}
        old = collector.record_for(manifest, combination)
        if old and old.get("status") in ("success", "no_data") and str(old.get("finished_at", "")) >= started_at:
            return
        collector.prepare_manifest(manifest, specs, pages, root)
        collector.save_record(manifest, {
            **spec, "pages_requested": pages, "pages_completed": result.pages_completed,
            "page_markers": result.page_markers, "status": "paused", "exit_code": result.code,
            "job_count": count, "json": str(output), "log": str(output.with_suffix(".log")),
            "checkpoint": str(checkpoint) if checkpoint.is_file() else None,
            "started_at": started_at, "finished_at": collector.now_iso(),
            "security_or_login_gate": bool(old and old.get("security_or_login_gate")),
            "desktop_interruption": requested, "retryable": True,
            "gate_wait_count": (old or {}).get("gate_wait_count", 0),
            "last_gate_at": (old or {}).get("last_gate_at"),
        })
        collector.write_json_atomic(manifest_path, manifest)
        log("当前组合已记为 paused；已完成组合及已有检查点保留，开始 / 续采会重试未完成组合。")
    except (OSError, UnicodeError, ValueError, TypeError):
        log("暂停状态暂未写入清单；未删除任何 raw 或检查点，续采会重试未完成组合。")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


class CollectionPage(ttk.Frame):
    """Independent collection workspace embedded in an existing Tk window."""

    def __init__(self, parent: tk.Misc, *, config_path: Path, initial_root: str = "",
                 initial_scraper_root: str = "", on_use_source: Callable[[Path], None] | None = None,
                 can_start: Callable[[], bool] | None = None,
                 on_busy_change: Callable[[bool], None] | None = None):
        super().__init__(parent)
        self.config_path = Path(config_path)
        self.on_use_source, self.can_start, self.on_busy_change = on_use_source, can_start, on_busy_change
        self._tk_thread = threading.get_ident()
        self._busy = self._closing = False
        self._waiting = False
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._worker_lock = threading.Lock()
        self._after_ids: set[str] = set()
        self._preview_after: str | None = None
        self._preview_id = 0
        self._preview_active = False
        self._preview_pending = False
        self._aux_pending = False
        self._plan_action_id = 0
        self._operation = ""
        self._operation_thread: threading.Thread | None = None
        self._operation_result: dict[str, Any] | None = None
        self._runner: _OwnedCommand | None = None
        self._lanes: dict[str, _CollectionLane] = {}
        self._lane_states: dict[str, dict[str, Any]] = {}
        self._control: CollectionControl | None = None
        self._traces: list[tuple[tk.Variable, str]] = []
        self._layout_signature: tuple[int, int, int, int] | None = None
        self._track_syncing = False
        self._plan_config: dict[str, Any] | None = None
        self._cached_config_path: Path | None = None
        self._checked_combinations: set[str] | None = None
        self._plan_rows: list[dict[str, Any]] = []
        self._visible_plan_rows: list[dict[str, Any]] = []
        self._plan_row_ids: dict[str, str] = {}
        self._editor: _CollectionPlanEditor | None = None
        self.palette = dict(LIGHT_PALETTE)
        self.collection_root = _PageStringVar(self, initial_root or str(collector.default_root()))
        self.scraper_root = _PageStringVar(self, initial_scraper_root)
        self.city = _PageStringVar(self, CITY_CHOICES[0])
        self.track = _PageStringVar(self, TRACK_CHOICES[0])
        self.track_label = _PageStringVar(self, TRACK_LABELS["all"])
        self.limit = _PageStringVar(self, "")
        self.pages = _PageStringVar(self, "10")
        self.browser_mode = _PageStringVar(self, BROWSER_MODES["dual"])
        self.page_wait_min = _PageStringVar(self, f"{collector.DEFAULT_PAGE_WAIT_MIN:g}")
        self.page_wait_max = _PageStringVar(self, f"{collector.DEFAULT_PAGE_WAIT_MAX:g}")
        self.browser_workers = _PageStringVar(self, str(DEFAULT_BROWSER_WORKERS))
        self.config_text = _PageStringVar(self, f"方案：{self.config_path}")
        self.scope_help = _PageStringVar(self, "检索分组是两套关键词与经验配置，不是岗位最终分层；正在读取当前方案的实际范围。")
        self.plan_search = _PageStringVar(self, "")
        self.plan_detail = _PageStringVar(self, "点击勾选列或选中后按空格；下表是检索计划，不是已采集的岗位。")
        self.estimate_text = _PageStringVar(self, "预计组合：正在只读计算（不代表已完成进度）…")
        self.status_text = _PageStringVar(self, "待开始；打开本页不会启动浏览器、联网或执行后处理。")
        self.progress_text = _PageStringVar(self, "落盘进度：未知；尚未读取采集清单。")
        self.stats_text = _PageStringVar(self, "成功 —　无数据 —　失败 —　暂停 —　原始条目（可重复）—")
        self.position_text = _PageStringVar(self, "当前组合 / 页：未运行")
        self._variables = (self.collection_root, self.scraper_root, self.city, self.track, self.track_label, self.limit, self.pages, self.browser_mode,
                           self.page_wait_min, self.page_wait_max, self.browser_workers,
                           self.config_text, self.estimate_text, self.status_text, self.progress_text,
                           self.stats_text, self.position_text, self.scope_help, self.plan_search, self.plan_detail)
        self._snapshot: dict[str, Any] | None = None
        self._build_ui()
        self.apply_theme(self.palette)
        for variable in (self.collection_root, self.city, self.limit, self.pages):
            self._traces.append((variable, variable.trace_add("write", self._inputs_changed)))
        self._traces.append((self.track, self.track.trace_add("write", self._track_changed)))
        self._traces.append((self.track_label, self.track_label.trace_add("write", self._track_label_changed)))
        self._traces.append((self.plan_search, self.plan_search.trace_add("write", lambda *_: self._render_plan_rows())))
        self.bind("<Destroy>", self._destroyed, add="+")
        self._render_controls()
        self._schedule(70, self._drain_events)
        self._schedule_preview(80)

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def shutdown_complete(self) -> bool:
        with self._worker_lock:
            active = any(worker.is_alive() for worker in self._workers)
        complete = not active and all(runner.shutdown_complete for lane in self._lanes.values() for runner in lane.all_runners)
        if complete and self._closing and threading.get_ident() == self._tk_thread and self._busy:
            self._set_busy(False)
        return complete

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.content = ttk.Frame(self.canvas, padding=(0, 0, 6, 0))
        self._canvas_window = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(4, weight=1, minsize=165)
        self.canvas.bind("<Configure>", self._resize_content, add="+")
        self.content.bind("<Configure>", self._resize_content, add="+")
        ttk.Label(self.content,
                  text="独立采集原始岗位列表；不会自动筛选、补 JD、调用模型或生成报告。登录 / 安全验证须在各侧专用浏览器中人工完成。",
                  style="Muted.TLabel", wraplength=940).grid(row=0, column=0, sticky="ew", pady=(0, 4))
        settings = ttk.Frame(self.content, style="Card.TFrame", padding=(10, 3))
        settings.grid(row=1, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="采集输出根", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.root_entry = ttk.Entry(settings, textvariable=self.collection_root)
        self.root_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6), pady=2)
        self.choose_root_button = ttk.Button(settings, text="选择 / 新建目录", style="Small.TButton", command=self._choose_root)
        self.choose_root_button.grid(row=0, column=2)
        ttk.Label(settings, text="上游 scraper", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10))
        self.scraper_entry = ttk.Entry(settings, textvariable=self.scraper_root)
        self.scraper_entry.grid(row=1, column=1, sticky="ew", padx=(0, 6), pady=2)
        self.choose_scraper_button = ttk.Button(settings, text="选择（留空自动找）", style="Small.TButton", command=self._choose_scraper)
        self.choose_scraper_button.grid(row=1, column=2)
        filters = ttk.Frame(settings, style="Card.TFrame")
        filters.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 3))
        ttk.Label(filters, text="城市", style="Card.TLabel").pack(side="left")
        self.city_box = ttk.Combobox(filters, textvariable=self.city, values=CITY_CHOICES, state="readonly", width=8)
        self.city_box.pack(side="left", padx=(6, 14))
        ttk.Label(filters, text="检索分组", style="Card.TLabel").pack(side="left")
        self.track_box = ttk.Combobox(filters, textvariable=self.track_label, values=tuple(TRACK_LABELS.values()), state="readonly", width=9)
        self.track_box.pack(side="left", padx=(6, 14))
        ttk.Label(filters, text="本次最多检索", style="Card.TLabel").pack(side="left")
        self.limit_entry = ttk.Entry(filters, textvariable=self.limit, width=7)
        self.limit_entry.pack(side="left", padx=(6, 4))
        ttk.Label(filters, text="组，空 = 全部", style="Hint.TLabel").pack(side="left", padx=(0, 14))
        ttk.Label(filters, text="每组页数", style="Card.TLabel").pack(side="left")
        self.pages_spin = ttk.Spinbox(filters, textvariable=self.pages, from_=1, to=10, width=5)
        self.pages_spin.pack(side="left", padx=(6, 5))
        ttk.Label(filters, text="1–10 页，仅列表", style="Hint.TLabel").pack(side="left")
        self.scope_help_label = ttk.Label(settings, textvariable=self.scope_help, style="Hint.TLabel", wraplength=900)
        self.scope_help_label.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        runtime = ttk.Frame(settings, style="Card.TFrame")
        runtime.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        ttk.Label(runtime, text="翻页等待（秒）最短", style="Card.TLabel").pack(side="left")
        self.page_wait_min_entry = ttk.Entry(runtime, textvariable=self.page_wait_min, width=6)
        self.page_wait_min_entry.pack(side="left", padx=(5, 8))
        ttk.Label(runtime, text="最长", style="Card.TLabel").pack(side="left")
        self.page_wait_max_entry = ttk.Entry(runtime, textvariable=self.page_wait_max, width=6)
        self.page_wait_max_entry.pack(side="left", padx=(5, 15))
        ttk.Label(runtime, text="每浏览器采集并发数", style="Card.TLabel").pack(side="left")
        self.browser_workers_spin = ttk.Spinbox(runtime, textvariable=self.browser_workers, from_=1, to=MAX_BROWSER_WORKERS, width=4)
        self.browser_workers_spin.pack(side="left", padx=(5, 8))
        self.limit_help_label = ttk.Label(runtime, text="1–8 槽；上限先于分片，完成组跳过", style="Hint.TLabel")
        self.limit_help_label.pack(side="left")
        plan_actions = ttk.Frame(settings, style="Card.TFrame")
        plan_actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(4, 2))
        self.edit_plan_button = ttk.Button(plan_actions, text="编辑检索条件…", style="Small.TButton", command=self.open_plan_editor)
        self.edit_plan_button.pack(side="left", padx=(0, 6))
        self.load_plan_button = ttk.Button(plan_actions, text="加载方案…", style="Small.TButton", command=self.load_plan)
        self.load_plan_button.pack(side="left", padx=(0, 8))
        # Long snapshot paths remain available in the operation log; this
        # single-line label must not consume space needed by runtime controls.
        self.config_label = ttk.Label(plan_actions, textvariable=self.config_text, style="Hint.TLabel", width=1)
        self.config_label.pack(side="left", fill="x", expand=True)
        self.estimate_label = ttk.Label(settings, textvariable=self.estimate_text, style="Hint.TLabel", wraplength=900)
        self.estimate_label.grid(row=6, column=0, columnspan=3, sticky="ew")
        actions = ttk.Frame(self.content)
        actions.grid(row=2, column=0, sticky="ew", pady=2)
        first = ttk.Frame(actions)
        first.pack(fill="x")
        self.start_button = ttk.Button(first, text="开始 / 续采", style="Primary.TButton", command=self.start)
        self.prepare_browser_button = ttk.Button(first, text="准备浏览器 / 登录", style="Small.TButton", command=self.prepare_browser)
        self.pause_button = ttk.Button(first, text="暂停", style="Small.TButton", command=self.pause)
        self.stop_button = ttk.Button(first, text="停止", style="Small.TButton", command=self.stop)
        self.continue_button = ttk.Button(first, text="Chrome 人工完成，继续", style="Small.TButton", command=self.continue_after_gate)
        self.edge_continue_button = ttk.Button(first, text="Edge 人工完成，继续", style="Small.TButton",
                                              command=lambda: self.continue_after_gate("edge"))
        for widget in (self.start_button, self.prepare_browser_button, self.pause_button, self.stop_button,
                       self.continue_button, self.edge_continue_button):
            widget.pack(side="left", padx=(0, 6))
        second = ttk.Frame(actions)
        second.pack(fill="x", pady=(4, 0))
        self.refresh_button = ttk.Button(second, text="刷新进度", style="Small.TButton", command=self.refresh_progress)
        self.open_root_button = ttk.Button(second, text="打开采集目录", style="Small.TButton", command=self.open_collection_directory)
        self.use_source_button = ttk.Button(second, text="用此数据新建后处理任务", style="Small.TButton", command=self.use_source)
        for widget in (self.refresh_button, self.open_root_button, self.use_source_button):
            widget.pack(side="left", padx=(0, 6))
        self.browser_mode_box = ttk.Combobox(second, textvariable=self.browser_mode, values=tuple(BROWSER_MODES.values()),
                                            state="readonly", width=16)
        self.browser_mode_box.pack(side="right")
        ttk.Label(second, text="采集浏览器").pack(side="right", padx=(8, 6))
        progress = ttk.Frame(self.content)
        progress.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        progress.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(progress, textvariable=self.status_text, wraplength=940)
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.progress_label = ttk.Label(progress, textvariable=self.progress_text, style="Muted.TLabel", wraplength=940)
        self.progress_label.grid(row=1, column=0, sticky="ew")
        self.progress_bar = ttk.Progressbar(progress, maximum=1, value=0)
        self.progress_bar.grid(row=2, column=0, sticky="ew", pady=3)
        self.stats_label = ttk.Label(progress, textvariable=self.stats_text, style="Muted.TLabel", wraplength=940)
        self.stats_label.grid(row=3, column=0, sticky="ew")
        self.position_label = ttk.Label(progress, textvariable=self.position_text, style="Muted.TLabel", wraplength=940)
        self.position_label.grid(row=4, column=0, sticky="ew")
        self.notebook = ttk.Notebook(self.content)
        self.notebook.grid(row=4, column=0, sticky="nsew")
        self.plan_frame = ttk.Frame(self.notebook)
        self.plan_frame.columnconfigure(0, weight=1)
        self.plan_frame.rowconfigure(1, weight=1)
        plan_toolbar = ttk.Frame(self.plan_frame)
        plan_toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(3, 3))
        self.select_all_button = ttk.Button(plan_toolbar, text="全选可见范围", style="Small.TButton", command=lambda: self.select_visible_combinations(True))
        self.select_all_button.pack(side="left", padx=(0, 5))
        self.clear_selection_button = ttk.Button(plan_toolbar, text="清空可见范围", style="Small.TButton", command=lambda: self.select_visible_combinations(False))
        self.clear_selection_button.pack(side="left", padx=(0, 12))
        ttk.Label(plan_toolbar, text="查找关键词 / 城市").pack(side="left")
        self.plan_search_entry = ttk.Entry(plan_toolbar, textvariable=self.plan_search, width=22)
        self.plan_search_entry.pack(side="left", padx=6)
        ttk.Label(plan_toolbar, text="勾选后再应用本次上限", style="Muted.TLabel").pack(side="left")
        plan_columns = ("checked", "index", "group", "city", "keyword", "salary", "experience", "degree", "pages", "execution")
        self.plan_table = ttk.Treeview(self.plan_frame, columns=plan_columns, show="headings", height=3, selectmode="extended")
        for key, title, width in zip(plan_columns,
                ("勾选", "序号", "检索分组", "城市", "关键词", "薪资", "经验", "学历", "页数", "本次计划"),
                (48, 48, 85, 65, 225, 78, 85, 62, 48, 110)):
            self.plan_table.heading(key, text=title)
            self.plan_table.column(key, width=width, minwidth=40, stretch=key == "keyword")
        self.plan_table.grid(row=1, column=0, sticky="nsew")
        plan_scroll = ttk.Scrollbar(self.plan_frame, orient="vertical", command=self.plan_table.yview)
        plan_scroll.grid(row=1, column=1, sticky="ns")
        plan_xscroll = ttk.Scrollbar(self.plan_frame, orient="horizontal", command=self.plan_table.xview)
        plan_xscroll.grid(row=2, column=0, sticky="ew")
        self.plan_table.configure(yscrollcommand=plan_scroll.set, xscrollcommand=plan_xscroll.set)
        self.plan_table.bind("<Button-1>", self._plan_click, add="+")
        self.plan_table.bind("<space>", self._plan_space, add="+")
        self.plan_table.bind("<<TreeviewSelect>>", self._plan_selected, add="+")
        ttk.Label(self.plan_frame, textvariable=self.plan_detail, style="Muted.TLabel", wraplength=930).grid(
            row=3, column=0, columnspan=2, sticky="ew")
        self.notebook.add(self.plan_frame, text="检索组合预览")
        log_frame = ttk.Frame(self.notebook)
        self.log_frame = log_frame
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_view = tk.Text(log_frame, height=5, width=1, wrap="word", state="disabled", relief="flat", borderwidth=0,
                                padx=8, pady=6, font=("Consolas", 9))
        self.log_view.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_view.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_view.configure(yscrollcommand=log_scroll.set)
        self.notebook.add(log_frame, text="采集日志")
        table_frame = ttk.Frame(self.notebook)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("combination", "scope", "status", "pages", "jobs")
        self.progress_table = ttk.Treeview(table_frame, columns=columns, show="headings", height=4)
        for key, title, width in zip(columns, ("组合", "城市 / 分组", "落盘状态", "已落盘页", "条目"), (380, 145, 130, 95, 65)):
            self.progress_table.heading(key, text=title)
            self.progress_table.column(key, width=width, minwidth=55, stretch=key == "combination")
        self.progress_table.grid(row=0, column=0, sticky="nsew")
        table_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.progress_table.yview)
        table_scroll.grid(row=0, column=1, sticky="ns")
        table_xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.progress_table.xview)
        table_xscroll.grid(row=1, column=0, sticky="ew")
        self.progress_table.configure(yscrollcommand=table_scroll.set, xscrollcommand=table_xscroll.set)
        self.notebook.add(table_frame, text="组合进度（落盘清单）")
        slot_frame = ttk.Frame(self.notebook)
        slot_frame.columnconfigure(0, weight=1)
        slot_frame.rowconfigure(0, weight=1)
        self.slot_table = ttk.Treeview(slot_frame, columns=("browser", "slot", "state", "position"), show="headings", height=4)
        for key, title, width in (("browser", "浏览器 / 端口", 130), ("slot", "并发槽", 65),
                                  ("state", "状态", 175), ("position", "当前组合 / 页", 540)):
            self.slot_table.heading(key, text=title)
            self.slot_table.column(key, width=width, minwidth=50, stretch=key == "position")
        self.slot_table.grid(row=0, column=0, sticky="nsew")
        slot_scroll = ttk.Scrollbar(slot_frame, orient="vertical", command=self.slot_table.yview)
        slot_scroll.grid(row=0, column=1, sticky="ns")
        slot_xscroll = ttk.Scrollbar(slot_frame, orient="horizontal", command=self.slot_table.xview)
        slot_xscroll.grid(row=1, column=0, sticky="ew")
        self.slot_table.configure(yscrollcommand=slot_scroll.set, xscrollcommand=slot_xscroll.set)
        self.slot_frame = slot_frame
        self.notebook.add(slot_frame, text="浏览器槽位")
        self.notebook.select(self.plan_frame)
        self._edit_controls = (self.root_entry, self.scraper_entry, self.choose_root_button,
                               self.choose_scraper_button, self.limit_entry, self.pages_spin, self.edit_plan_button,
                               self.page_wait_min_entry, self.page_wait_max_entry, self.browser_workers_spin,
                               self.load_plan_button, self.select_all_button, self.clear_selection_button, self.plan_search_entry)
        for section in (settings, actions, progress, self.notebook):
            # Their requested heights can shrink without resizing the canvas
            # window itself (e.g. after wrapping a long plan path).
            section.bind("<Configure>", self._resize_content, add="+")

    def _resize_content(self, _event: tk.Event | None = None) -> None:
        if self._closing:
            return
        try:
            width = max(1, self.canvas.winfo_width())
            # The notebook requests the height of its larger, hidden tree tab.
            # It can safely shrink to the explicit row minimum while the log
            # stays usable; do not force that hidden tab's full request on a
            # 1024x760 host window (or retain an old, narrow-wrap request).
            signature = (width, self.content.winfo_reqheight(), self.notebook.winfo_reqheight(), self.canvas.winfo_height())
            minimum = max(1, signature[1] - signature[2] + 165)
            height = max(minimum, self.canvas.winfo_height())
            self.canvas.itemconfigure(self._canvas_window, width=width, height=height)
            self.canvas.configure(scrollregion=(0, 0, width, height))
            wrap = max(280, width - 25)
            for label in (self.status_label, self.progress_label, self.stats_label, self.position_label,
                          self.estimate_label, self.scope_help_label, self.limit_help_label):
                label.configure(wraplength=wrap)
            if signature != self._layout_signature:
                self._layout_signature = signature
                # A wrap/tab change can settle a later idle pass at the same
                # width. Refit until that geometry is stable, without a timer.
                self._schedule(-1, self._resize_content)
        except tk.TclError:
            pass

    def apply_theme(self, palette: Mapping[str, str]) -> None:
        """Repaint local Tk widgets only; host owns ttk styles/options/theme."""
        if self._closing:
            return
        self.palette = {**LIGHT_PALETTE, **dict(palette)}
        p = self.palette
        self.canvas.configure(background=p["background"])
        self.log_view.configure(background=p["log"], foreground=p["log_text"], insertbackground=p["log_text"],
                                selectbackground=p["log_selection"], selectforeground=p["log_selected_text"])
        for box in (self.city_box, self.track_box, self.browser_mode_box):
            configure_combobox_popdown(box, p)
        for status, color in (("success", p["green"]), ("no_data", p["muted"]),
                              ("paused", p["amber"]), ("failed", p["red"])):
            self.progress_table.tag_configure(status, foreground=color)
        self.plan_table.tag_configure("unchecked", foreground=p["muted"])
        self.plan_table.tag_configure("limited", foreground=p["amber"])
        if self._editor is not None:
            self._editor.apply_theme(p)

    def _schedule(self, milliseconds: int, function: Callable[[], None]) -> str | None:
        if self._closing:
            return None
        holder: list[str] = []

        def invoke() -> None:
            if holder:
                self._after_ids.discard(holder[0])
            if not self._closing:
                function()
        try:
            identifier = self.after_idle(invoke) if milliseconds < 0 else self.after(milliseconds, invoke)
        except tk.TclError:
            return None
        holder.append(identifier)
        self._after_ids.add(identifier)
        return identifier

    def _spawn(self, name: str, work: Callable[[], None]) -> threading.Thread:
        worker = threading.Thread(target=work, name=name, daemon=True)
        with self._worker_lock:
            self._workers = [item for item in self._workers if item.is_alive()]
            self._workers.append(worker)
        worker.start()
        return worker

    def _emit(self, kind: str, value: Any) -> None:
        if not self._closing:
            self._events.put((kind, value))

    def _capture_request(self) -> CollectionRequest:
        selection = CollectionSelection.parse(self.city.get(), self.track.get(), self.limit.get(), self.pages.get())
        page_wait_min, page_wait_max = collector.validate_page_wait(self.page_wait_min.get(), self.page_wait_max.get())
        browser_workers = int(_positive(self.browser_workers.get(), "每浏览器采集并发数", maximum=MAX_BROWSER_WORKERS))
        mode_text = self.browser_mode.get()
        mode = next((key for key, label in BROWSER_MODES.items() if mode_text in (key, label)), None)
        if mode is None:
            raise ValueError("采集浏览器请选择 Chrome + Edge 或仅 Chrome。")
        cached = deepcopy(self._plan_config) if self._cached_config_path == self.config_path else None
        combinations = tuple(sorted(self._checked_combinations)) if self._checked_combinations is not None else None
        return CollectionRequest(self.collection_root.get().strip(), self.scraper_root.get().strip(), self.config_path,
                                 selection, cached, combinations, mode, page_wait_min, page_wait_max, browser_workers)

    def _track_changed(self, *_args: object) -> None:
        if self._track_syncing or self._closing:
            return
        value = TRACK_KEYS.get(self.track.get(), self.track.get())
        self._track_syncing = True
        try:
            if self.track.get() != value:
                self.track.set(value)
            self.track_label.set(TRACK_LABELS.get(value, value))
        finally:
            self._track_syncing = False
        self._inputs_changed()

    def _track_label_changed(self, *_args: object) -> None:
        if self._track_syncing or self._closing:
            return
        self.track.set(TRACK_KEYS.get(self.track_label.get(), self.track_label.get()))

    def _inputs_changed(self, *_args: object) -> None:
        self._preview_id += 1
        self._snapshot = None
        self.progress_text.set("输入已变化，正在只读刷新；下方旧记录不代表当前选择。")
        self.estimate_text.set("检索计划正在只读刷新；不会启动采集。")
        self._schedule_preview(220)

    def _schedule_preview(self, milliseconds: int) -> None:
        if self._preview_after:
            try:
                self.after_cancel(self._preview_after)
            except tk.TclError:
                pass
            self._after_ids.discard(self._preview_after)
        self._preview_after = self._schedule(milliseconds, self._begin_preview)

    def refresh_progress(self) -> None:
        if not self._closing:
            self._preview_id += 1
            self._schedule_preview(0)

    def _begin_preview(self) -> None:
        self._preview_after = None
        if self._closing:
            return
        if self._preview_active:
            self._preview_pending = True
            return
        try:
            request = self._capture_request()
        except ValueError as error:
            self.estimate_text.set(redact_log(error))
            return
        generation = self._preview_id
        self._preview_active = True

        def inspect() -> None:
            value: dict[str, Any] = {"generation": generation, "error": None}
            try:
                config = request.config_snapshot if request.config_snapshot is not None else collector.load_config(request.config_path.expanduser().resolve())
                value.update(build_collection_preview(config, request.selection, request.combination_ids))
            except Exception as error:
                value["error"] = redact_log(error)
            try:
                root = validate_collection_root(request.root_text)
                value["progress"] = read_collection_progress(root)
            except Exception as error:
                value["path_error"] = redact_log(error)
            self._emit("preview", value)
        self._spawn("collection-preview", inspect)

    def _apply_plan_preview(self, value: dict[str, Any]) -> None:
        self._plan_config = deepcopy(value["config"])
        self._cached_config_path = self.config_path
        if self._checked_combinations is None:
            self._checked_combinations = set(value["all_checked"])
        self._plan_rows = value["plan_rows"]
        self.estimate_text.set(f"当前范围 {value['full']} 组 · 已勾选 {value['selected_count']} 组 · 本次执行 {value['estimate']} 组"
                               "（按配置顺序，规划数不是已完成数）。" + (" 未勾选组合，不能开始采集。" if not value["selected_count"] else ""))
        editor_values = value["editor_values"]
        descriptions = []
        for track in ("current", "target"):
            selected = set(editor_values[track]["experience_ids"])
            names = [item["label"] for item in editor_values["experiences"][track] if item["id"] in selected]
            descriptions.append(f"{TRACK_LABELS[track]}：{len(editor_values[track]['keywords'])} 个关键词，经验 {' / '.join(names) if names else '未启用'}")
        self.scope_help.set("两套关键词与经验配置，不是岗位最终分层。" + "；".join(descriptions) + "。")
        self._render_plan_rows()

    def _render_plan_rows(self) -> None:
        if self._closing or not hasattr(self, "plan_table"):
            return
        search = self.plan_search.get().strip().casefold()
        rows = [row for row in self._plan_rows if not search or search in " ".join(str(row.get(key, "")) for key in (
            "keyword", "city_label", "track_label", "salary_label", "experience_label", "degree_label")).casefold()]
        self._visible_plan_rows = rows
        old_y = self.plan_table.yview()
        selected_ids = {self._plan_row_ids.get(item) for item in self.plan_table.selection()}
        self.plan_table.delete(*self.plan_table.get_children())
        self._plan_row_ids = {}
        selected = []
        for index, row in enumerate(rows):
            iid = str(index)
            combination = row["combination"]
            self._plan_row_ids[iid] = combination
            tag = "unchecked" if not row["checked"] else "limited" if row["execution_status"] != "本次执行" else ""
            self.plan_table.insert("", "end", iid=iid, tags=(tag,), values=(
                "☑" if row["checked"] else "☐", row["index"], row["track_label"], row["city_label"],
                redact_log(row["keyword"]), row["salary_label"], row["experience_label"], row["degree_label"],
                row["pages_requested"], row["execution_status"]))
            if combination in selected_ids:
                selected.append(iid)
        if selected:
            self.plan_table.selection_set(selected)
        if old_y:
            self.plan_table.yview_moveto(old_y[0])

    def _plan_selected(self, _event: tk.Event | None = None) -> None:
        if self._closing:
            return
        chosen = self.plan_table.selection()
        if chosen and chosen[0] in self._plan_row_ids:
            combination = self._plan_row_ids[chosen[0]]
            row = next((item for item in self._plan_rows if item["combination"] == combination), None)
            if row:
                self.plan_detail.set(redact_log(f"{row['city_label']} · {row['track_label']} · 关键词：{row['keyword']}"
                                               f" · {row['salary_label']} · {row['experience_label']} · {row['degree_label']} · {row['pages_requested']}页"))

    def _plan_click(self, event: tk.Event) -> str | None:
        if self.plan_table.identify_region(event.x, event.y) == "cell" and self.plan_table.identify_column(event.x) == "#1":
            iid = self.plan_table.identify_row(event.y)
            if iid:
                self.plan_table.selection_set(iid)
                self.toggle_combination(self._plan_row_ids.get(iid, ""))
                return "break"
        return None

    def _plan_space(self, _event: tk.Event) -> str:
        if not self._busy and not self._aux_pending:
            for iid in self.plan_table.selection():
                self.toggle_combination(self._plan_row_ids.get(iid, ""), refresh=False)
            self._schedule_preview(0)
        return "break"

    def toggle_combination(self, combination: str, *, refresh: bool = True) -> None:
        if self._closing or self._busy or self._aux_pending or self._checked_combinations is None:
            return
        if not any(row["combination"] == combination for row in self._plan_rows):
            return
        if combination in self._checked_combinations:
            self._checked_combinations.remove(combination)
        else:
            self._checked_combinations.add(combination)
        self._preview_id += 1
        for row in self._plan_rows:
            if row["combination"] == combination:
                row["checked"] = combination in self._checked_combinations
                if not row["checked"]:
                    row["execution_status"] = "未勾选"
        self._render_plan_rows()
        if refresh:
            self._schedule_preview(0)

    def select_visible_combinations(self, selected: bool) -> None:
        if self._closing or self._busy or self._aux_pending or self._checked_combinations is None:
            return
        visible = {row["combination"] for row in self._visible_plan_rows}
        if selected:
            self._checked_combinations.update(visible)
        else:
            self._checked_combinations.difference_update(visible)
        self._preview_id += 1
        self._schedule_preview(0)

    def _apply_loaded_plan(self, path: Path, config: dict[str, Any], *, preserve_checks: bool = False) -> None:
        """Publish an already validated plan on Tk's thread, without starting it."""
        self.config_path = Path(path)
        self._plan_config = deepcopy(config)
        self._cached_config_path = self.config_path
        self.config_text.set(f"方案：{self.config_path}")
        if not preserve_checks:
            self._checked_combinations = None
            self._plan_rows = []
            self._render_plan_rows()
        self._preview_id += 1
        self.estimate_text.set("新方案正在只读展开；尚未开始采集。")
        self._schedule_preview(0)

    def open_plan_editor(self) -> None:
        if self._closing or self._busy:
            return
        if self._editor is not None:
            self._editor.lift()
            return
        if self._aux_pending:
            return
        path = self.config_path
        cached = deepcopy(self._plan_config) if self._cached_config_path == path else None
        self._aux_pending = True
        self._plan_action_id += 1
        token = self._plan_action_id
        self.status_text.set("正在只读加载编辑选项；不会启动浏览器或采集。")
        self._render_controls()

        def work() -> None:
            value: dict[str, Any] = {"token": token, "path": path, "error": None}
            try:
                config = cached if cached is not None else collector.load_config(path.expanduser().resolve())
                value["config"] = _validated_page_config(config)
                value["editor_values"] = plans.plan_editor_values(value["config"])
            except Exception as error:
                value["error"] = redact_log(error)
            self._emit("plan_editor_open", value)
        self._spawn("collection-plan-editor-open", work)

    def load_plan(self, path: str | Path | None = None) -> None:
        if self._closing or self._busy or self._aux_pending:
            return
        if path is None:
            path = filedialog.askopenfilename(parent=self, title="加载检索方案（只读验证，不启动采集）",
                                              filetypes=(("JSON 采集方案", "*.json"), ("所有文件", "*.*")))
        if not path:
            return
        selected_path = Path(path).expanduser().absolute()
        self._aux_pending = True
        self._plan_action_id += 1
        token = self._plan_action_id
        self.status_text.set("正在后台只读验证方案；验证成功后才替换当前计划。")
        self._render_controls()

        def work() -> None:
            value: dict[str, Any] = {"token": token, "path": selected_path, "error": None}
            try:
                config = collector.load_config(selected_path)
                value["config"] = _validated_page_config(config)
                if not collector.build_specs(value["config"]):
                    raise ValueError("该方案没有可执行的检索组合，未替换当前方案。")
            except Exception as error:
                value["error"] = redact_log(error)
            self._emit("plan_loaded", value)
        self._spawn("collection-plan-load", work)

    def _editor_closed(self, editor: _CollectionPlanEditor) -> None:
        if self._editor is not editor:
            return
        self._editor = None
        self._aux_pending = False
        if not self._closing:
            if not editor.applied:
                self.status_text.set("已取消编辑；当前方案和组合勾选未改变，也未启动采集。")
            self._render_controls()

    def _host_allows_start(self) -> bool:
        try:
            return self.can_start is None or bool(self.can_start())
        except Exception:
            return False

    def _set_busy(self, value: bool) -> None:
        if self._busy == value:
            return
        self._busy = value
        if self.on_busy_change:
            try:
                self.on_busy_change(value)
            except Exception:
                pass

    def _render_controls(self) -> None:
        if self._closing:
            return
        editable = not self._busy and not self._aux_pending
        for widget in (*self._edit_controls, self.start_button, self.prepare_browser_button, self.use_source_button):
            widget.state(["!disabled"] if editable else ["disabled"])
        if self.on_use_source is None:
            self.use_source_button.state(["disabled"])
        for box in (self.city_box, self.track_box, self.browser_mode_box):
            box.configure(state="readonly" if editable else "disabled")
        self.plan_table.state(["!disabled"] if editable else ["disabled"])
        self.pause_button.state(["!disabled"] if self._busy and self._control and not self._control.requested else ["disabled"])
        self.stop_button.state(["!disabled"] if self._busy and self._control and self._control.requested != "stopped" else ["disabled"])
        for browser, button in (("chrome", self.continue_button), ("edge", self.edge_continue_button)):
            waiting = self._lane_states.get(browser, {}).get("waiting", False)
            button.state(["!disabled"] if self._busy and waiting and self._control and not self._control.requested else ["disabled"])

    def start(self) -> None:
        self._start_operation(setup_only=False)

    def prepare_browser(self) -> None:
        self._start_operation(setup_only=True)

    def _start_operation(self, *, setup_only: bool) -> None:
        if self._closing or self._busy or self._aux_pending:
            return
        if not self._host_allows_start():
            self.status_text.set("后处理正在工作或窗口正在关闭；请等待完成后再开始采集 / 准备浏览器。")
            return
        try:
            request = self._capture_request()
        except ValueError as error:
            self.status_text.set(redact_log(error))
            return
        self._control = control = CollectionControl()
        browsers = ("chrome", "edge") if request.browser_mode == "dual" else ("chrome",)
        lanes = tuple(_CollectionLane(browser, index, len(browsers)) for index, browser in enumerate(browsers))
        for lane in lanes:
            lane.slots = tuple(_CollectionSlot(index + 1, lane.shard + len(browsers) * index,
                                               len(browsers) * request.browser_workers,
                                               lane.runner if request.browser_workers == 1 else _OwnedCommand())
                               for index in range(request.browser_workers))
        self._lanes = {lane.browser: lane for lane in lanes}
        self._runner = lanes[0].runner
        self._lane_states = {lane.browser: {"state": "等待检查", "position": "尚未开始", "waiting": False,
                                           "slots": {str(slot.number): {"state": "等待检查", "position": "尚未开始", "waiting": False}
                                                     for slot in lane.slots}}
                             for lane in lanes}
        if setup_only or request.browser_workers > 1:
            for state in self._lane_states.values():
                state["slots"]["setup"] = {"state": "等待检查", "position": "浏览器登录准备", "waiting": False}
        self._operation = "browser" if setup_only else "collect"
        self._operation_result = None
        self._preview_id += 1  # An old, slow disk preview cannot replace live progress.
        self._waiting = False
        self.status_text.set("正在后台检查配置、输出路径与上游采集器…")
        self._render_lane_positions()
        self._set_busy(True)
        self._render_controls()

        def operate() -> None:
            root: Path | None = None
            specs: list[dict[str, Any]] = []
            log_handle: Any = None
            temporary: tempfile.TemporaryDirectory[str] | None = None
            log_unavailable = False
            log_lock = threading.Lock()
            lane_threads: list[threading.Thread] = []
            command_results: dict[tuple[str, str], dict[str, Any]] = {}
            result_lock = threading.Lock()
            outcome: dict[str, Any] = {"error": None, "code": None, "setup_only": setup_only}
            started_at = collector.now_iso()

            def emit(kind: str, value: Any) -> None:
                nonlocal log_unavailable
                if kind == "log":
                    value = redact_log(value).rstrip("\r\n")
                    with log_lock:
                        if log_handle:
                            try:
                                log_handle.write(value + "\n")
                                log_handle.flush()
                            except OSError:
                                if not log_unavailable:
                                    self._emit("log", "[collection-desktop.log 暂不可写；状态仍以采集清单为准]")
                                log_unavailable = True
                        self._emit("log", value)
                else:
                    self._emit(kind, value)

            def monitor() -> None:
                if root is not None and not setup_only:
                    self._emit("progress", read_collection_progress(root))

            def lane_emit(lane: _CollectionLane, kind: str, value: Any, slot_key: str = "1") -> None:
                label = (f"{lane.label} 登录" if slot_key == "setup" else
                         lane.label if request.browser_workers == 1 else f"{lane.label} #{slot_key}")
                if kind == "log":
                    emit("log", f"[{label}] {value}")
                else:
                    slot_runner = next((slot.runner for slot in lane.slots if str(slot.number) == slot_key), None)
                    active_combination = slot_runner and slot_runner.result and slot_runner.result.combination
                    if kind == "waiting" and value and active_combination and lane.gate_file is not None:
                        # The CLI also marks real security responses before a
                        # peer can begin another group. Keep fake/older prompt
                        # sources bounded by the same owned session marker.
                        try:
                            with lane.gate_file.open("x", encoding="utf-8"):
                                pass
                        except FileExistsError:
                            pass
                        except OSError as caught:
                            emit("log", f"[{label}] 无法记录验证暂停：{redact_log(caught)}")
                    self._emit("lane", {"browser": lane.browser, "slot": slot_key, "kind": kind, "value": value})

            def run_command(lane: _CollectionLane, slot_key: str, runner: _OwnedCommand,
                            command: list[str], *, collecting: bool) -> CommandResult | None:
                result: CommandResult | None = None
                error: str | None = None
                try:
                    if not control.requested:
                        lane_emit(lane, "state", "准备专用浏览器 / 登录" if not collecting else "采集中", slot_key)
                        environment = {GATE_STOP_FILE_ENV: str(lane.gate_file)} if collecting and lane.gate_file is not None else None
                        result = runner.run(command, control, lambda kind, value: lane_emit(lane, kind, value, slot_key),
                                            hold_tree=True, extra_env=environment)
                except Exception as caught:
                    # An individual target/session failure never stops peers.
                    error = redact_log(caught)
                    result = runner.result
                    lane_emit(lane, "log", f"未完成：{error}；其他槽位继续运行。", slot_key)
                finally:
                    # Reclaim a dead side's short lock promptly; a healthy
                    # peer may already be waiting to publish its next record.
                    if result and root is not None and collecting:
                        _clear_owned_lock(root, result, lambda line: lane_emit(lane, "log", line, slot_key))
                    with result_lock:
                        command_results[(lane.browser, slot_key)] = {"result": result, "error": error,
                                                                    "runner": runner, "collecting": collecting}
                    state = ("已暂停" if control.requested == "paused" else "已停止" if control.requested else
                             "异常退出" if error else "已结束" if result and result.code == 0 else "未全部完成")
                    lane_emit(lane, "finished", state, slot_key)
                return result

            def start_slot(lane: _CollectionLane, slot: _CollectionSlot) -> None:
                slot_key = str(slot.number)
                command = make_collect_command(run_request, run_root, scraper, browser=lane.browser,
                                               shard=slot.shard, total_shards=slot.total_shards,
                                               setup_browser=request.browser_workers == 1)
                lane_emit(lane, "log", f"CDP {lane.port}；分片 {slot.shard + 1}/{slot.total_shards}；"
                          f"本次 {len(collector.shard_specs(specs, slot.shard, slot.total_shards))} 组", slot_key)
                lane_emit(lane, "log", subprocess.list2cmdline(command), slot_key)
                run_command(lane, slot_key, slot.runner, command, collecting=True)

            def run_lane(lane: _CollectionLane) -> None:
                slot_threads: list[threading.Thread] = []
                try:
                    if setup_only or request.browser_workers > 1:
                        command = make_collect_command(run_request, setup_root / lane.browser, scraper, setup_only=True,
                                                       browser=lane.browser, shard=lane.shard, total_shards=lane.total_shards)
                        lane_emit(lane, "log", subprocess.list2cmdline(command), "setup")
                        result = run_command(lane, "setup", lane.runner, command, collecting=False)
                        if setup_only or control.requested or result is None or result.code != 0:
                            for slot in lane.slots:
                                lane_emit(lane, "finished", "仅准备浏览器" if setup_only else "浏览器未就绪，未启动", str(slot.number))
                            return
                    for slot in lane.slots:
                        if not collector.shard_specs(specs, slot.shard, slot.total_shards):
                            lane_emit(lane, "finished", "无分配组合", str(slot.number))
                            continue
                        if control.requested or (lane.gate_file is not None and lane.gate_file.exists()):
                            lane_emit(lane, "finished", "验证暂停，未启动" if not control.requested else "已停止，未启动", str(slot.number))
                            continue
                        try:
                            slot_threads.append(self._spawn(f"collection-{lane.browser}-{slot.number}",
                                                           lambda lane=lane, slot=slot: start_slot(lane, slot)))
                        except Exception as caught:
                            error = redact_log(caught)
                            with result_lock:
                                command_results[(lane.browser, str(slot.number))] = {
                                    "result": None, "error": error, "runner": slot.runner, "collecting": True}
                            lane_emit(lane, "log", f"启动失败：{error}", str(slot.number))
                            lane_emit(lane, "finished", "启动失败", str(slot.number))
                except Exception as caught:
                    error = redact_log(caught)
                    with result_lock:
                        command_results[(lane.browser, "supervisor")] = {
                            "result": None, "error": error, "runner": lane.runner, "collecting": False}
                    lane_emit(lane, "log", f"浏览器调度未完成：{error}")
                    for slot in lane.slots:
                        if slot.runner.result is None:
                            lane_emit(lane, "finished", "启动失败", str(slot.number))
                finally:
                    for thread in slot_threads:
                        thread.join()

            try:
                root = validate_collection_root(request.root_text)
                config = (request.config_snapshot if request.config_snapshot is not None
                          else collector.load_config(request.config_path.expanduser().resolve()))
                preview = build_collection_preview(config, request.selection, request.combination_ids)
                config, specs = preview["config"], preview["execution_specs"]
                if setup_only:
                    run_plan = plans.select_plan_combinations(config, None)
                else:
                    if not specs:
                        raise ValueError("当前城市 / 检索分组内请至少勾选 1 组；未启动采集。")
                    run_plan = plans.select_plan_combinations(config, [spec["combination"] for spec in specs])
                    _guard_output_paths(root, specs)
                    if read_collection_progress(root)["manifest_state"] == "invalid":
                        raise ValueError("现有 collection_manifest.json 无效；未改写，请检查或选择新的采集根目录。")
                    assert_source_specs_compatible(root, specs)
                if request.scraper_text:
                    supplied = Path(request.scraper_text).expanduser().resolve()
                    if not (supplied / "scripts" / "boss_cdp_raw.py").is_file():
                        raise ValueError("上游目录缺少 scripts/boss_cdp_raw.py；请重新选择或留空自动发现。")
                scraper = collector.resolve_scraper_root(request.scraper_text or None)
                if control.requested:
                    return
                self._emit("scraper", (request.scraper_text, str(scraper)))
                # Temporary preparation manifests and browser gate markers
                # belong to this operation, never the persisted source scope.
                temporary = tempfile.TemporaryDirectory(prefix="boss-collection-browser-")
                temporary_base = Path(temporary.name)
                setup_root = temporary_base / "setup"
                gates_root = temporary_base / "gates"
                gates_root.mkdir()
                if not setup_only:
                    for lane in lanes:
                        lane.gate_file = gates_root / f"{lane.browser}.stop"
                if setup_only:
                    run_root = setup_root
                    snapshot = plans.save_plan_config(run_plan, temporary_base / "plans")
                else:
                    root.mkdir(parents=True, exist_ok=True)
                    _guard_output_paths(root, specs)
                    snapshot = plans.save_plan_config(run_plan, root / "collection-plans")
                    run_root = root
                    self._emit("active_plan", {"path": snapshot, "config": run_plan, "checked": preview["all_checked"]})
                    try:
                        log_handle = (root / "collection-desktop.log").open("a", encoding="utf-8", newline="\n")
                    except OSError:
                        self._emit("log", "[collection-desktop.log 暂不可写；状态仍以采集清单为准]")
                run_request = replace(request, config_path=snapshot)
                emit("log", f"\n[{started_at}] {'准备浏览器 / 人工登录（不采集）' if setup_only else '开始 / 续采（仅原始列表）'}")
                emit("log", f"原方案：{request.config_path}；{'临时方案（不写采集输出根）' if setup_only else '本次实际组合快照'}：{snapshot}")
                emit("log", f"翻页等待 {request.page_wait_min:g}–{request.page_wait_max:g} 秒；每浏览器最多 {request.browser_workers} 个独立采集槽。")
                emit("log", "每侧使用独立浏览器、CDP 端口与 profile；各侧人工完成后点击对应的继续按钮。")
                self._emit("state", "正在准备浏览器；请分别人工登录并点击各侧继续。" if setup_only
                           else "正在并发采集原始列表；各侧状态独立，已完成组合会跳过。")
                for lane in lanes:
                    assigned = collector.shard_specs(specs, lane.shard, lane.total_shards)
                    if not setup_only and not assigned:
                        for slot in lane.slots:
                            lane_emit(lane, "finished", "无分配组合", str(slot.number))
                        if request.browser_workers > 1:
                            lane_emit(lane, "finished", "无分配组合", "setup")
                        continue
                    try:
                        lane_threads.append(self._spawn(f"collection-{lane.browser}", lambda lane=lane: run_lane(lane)))
                    except Exception as caught:
                        error = redact_log(caught)
                        slot_key = "setup" if setup_only or request.browser_workers > 1 else "1"
                        with result_lock:
                            command_results[(lane.browser, slot_key)] = {
                                "result": None, "error": error, "runner": lane.runner, "collecting": not setup_only}
                        lane_emit(lane, "log", f"启动失败：{error}", slot_key)
                        for key in (("setup",) if setup_only else tuple(str(slot.number) for slot in lane.slots)):
                            lane_emit(lane, "finished", "启动失败", key)
                last_monitor = 0.0
                while any(thread.is_alive() for thread in lane_threads):
                    for thread in lane_threads:
                        thread.join(0.05)
                    if time.monotonic() - last_monitor >= 0.7:
                        monitor()
                        last_monitor = time.monotonic()
                errors = [f"{BROWSER_NAMES[browser]} #{slot}：{value['error']}"
                          for (browser, slot), value in command_results.items() if value["error"]]
                outcome["error"] = "；".join(errors) or None
                codes = [value["result"].code for value in command_results.values() if value["result"]]
                if any(lane.gate_file is not None and lane.gate_file.exists() for lane in lanes):
                    codes.append(20)
                outcome["code"] = 1 if errors else next((code for code in codes if code != 0), 0)
                if not control.requested and outcome["code"] != 0 and not setup_only:
                    emit("log", "本次采集未全部完成；请查看落盘失败 / 暂停记录，人工处理后使用开始 / 续采。")
            except Exception as error:
                outcome["error"] = redact_log(error)
                emit("log", f"未完成：{outcome['error']}")
            finally:
                # Do not release temporary roots, logs, or the host interlock
                # while an already-started owned lane is still shutting down.
                if any(thread.is_alive() for thread in lane_threads):
                    control.stop()
                    for thread in lane_threads:
                        thread.join()
                try:
                    if root is not None and not setup_only:
                        # Clear both dead owners before either interruption is
                        # saved: the peer may have held the shared lock.
                        for value in command_results.values():
                            if value["result"] and value["collecting"]:
                                _clear_owned_lock(root, value["result"], lambda line: emit("log", line))
                        for (browser, slot_key), value in command_results.items():
                            result = value.get("result")
                            reason = control.requested or ("failed" if value.get("error") else None)
                            if result and reason and value["collecting"] and value["runner"].command_complete:
                                preserve_interrupted_combination(root, specs, request.selection.pages, result,
                                                                 reason, started_at,
                                                                 lambda line, browser=browser, slot_key=slot_key: lane_emit(
                                                                     self._lanes[browser], "log", line, slot_key))
                        monitor()
                except Exception as caught:
                    outcome["error"] = outcome["error"] or redact_log(caught)
                    emit("log", f"最终进度保存 / 读取未完成：{redact_log(caught)}")
                if log_handle:
                    try:
                        log_handle.close()
                    except OSError:
                        pass
                if temporary is not None:
                    try:
                        temporary.cleanup()
                    except OSError:
                        emit("log", "浏览器准备的临时文件暂未清理；采集输出根未被改写。")
                # Keep even an early successful side's browser tree owned
                # through final progress I/O. A cancellation at that point
                # must still stop every browser created by this operation.
                with control._lock:
                    for lane in lanes:
                        for runner in lane.all_runners:
                            runner.release_tree(preserve_detached=control._requested is None)
                    outcome["requested"] = control._requested
                self._emit("finished", outcome)
        self._operation_thread = self._spawn("collection-worker", operate)

    def pause(self) -> None:
        if not self._closing and self._busy and self._control:
            self._control.pause()
            self._waiting = False
            for state in self._lane_states.values():
                state["waiting"] = False
                for slot_state in state["slots"].values():
                    slot_state["waiting"] = False
            self.status_text.set("正在暂停本次自有采集进程；已完成组合和已有检查点保留，当前组合可续采。")
            self._render_controls()

    def stop(self) -> None:
        if not self._closing and self._busy and self._control:
            self._control.stop()
            self._waiting = False
            for state in self._lane_states.values():
                state["waiting"] = False
                for slot_state in state["slots"].values():
                    slot_state["waiting"] = False
            self.status_text.set("正在停止本次自有采集进程；不会删除 raw、清单或检查点。")
            self._render_controls()

    def continue_after_gate(self, browser: str = "chrome") -> None:
        if self._closing or not self._busy or not self._control or self._control.requested:
            return
        lane = self._lanes.get(browser)
        state = self._lane_states.get(browser, {})
        if not lane or not state.get("waiting"):
            return
        runners = {str(slot.number): slot.runner for slot in lane.slots}
        runners["setup"] = lane.runner
        confirmed = 0
        # Snapshot only currently waiting slots. An active target cannot receive
        # a queued Enter when it later happens to encounter another gate.
        for slot_key, slot_state in state["slots"].items():
            if slot_state["waiting"] and runners[slot_key].request_continue():
                slot_state.update(waiting=False, state="已确认，等待继续 / 重试")
                confirmed += 1
        if not confirmed:
            return
        self._sync_lane_state(browser)
        self._waiting = any(value["waiting"] for value in self._lane_states.values())
        self._render_lane_positions()
        self.status_text.set(f"已确认 {lane.label} 的 {confirmed} 个等待槽；仅继续 / 重试当前已等待的操作。")
        self._render_controls()

    def _render_lane_positions(self) -> None:
        self.position_text.set("\n".join(
            f"{BROWSER_NAMES[browser]} · CDP {self._lanes[browser].port} · {state['state']} · "
            + (state["position"] if len(self._lanes[browser].slots) == 1 else
               f"最多 {len(self._lanes[browser].slots)} 槽；各槽组合 / 页见“浏览器槽位”")
            for browser, state in self._lane_states.items()))
        expected_rows = set()
        for browser, state in self._lane_states.items():
            for slot_key, slot_state in state["slots"].items():
                row_id = f"{browser}-{slot_key}"
                expected_rows.add(row_id)
                values = (f"{BROWSER_NAMES[browser]} / {self._lanes[browser].port}", "登录准备" if slot_key == "setup" else slot_key,
                          slot_state["state"], slot_state["position"])
                if self.slot_table.exists(row_id):
                    self.slot_table.item(row_id, values=values)
                else:
                    self.slot_table.insert("", "end", iid=row_id, values=values)
        for row_id in self.slot_table.get_children():
            if row_id not in expected_rows:
                self.slot_table.delete(row_id)

    def _sync_lane_state(self, browser: str) -> None:
        state = self._lane_states[browser]
        slots = state["slots"]
        state["waiting"] = any(item["waiting"] for item in slots.values())
        if len(self._lanes[browser].slots) == 1 and "setup" not in slots:
            state.update(state=slots["1"]["state"], position=slots["1"]["position"])
            return
        setup = slots.get("setup", {})
        numeric = [item for key, item in slots.items() if key != "setup"]
        running = sum(item["state"] in ("采集中", "继续采集 / 准备", "已确认，等待继续 / 重试") for item in numeric)
        waiting = sum(item["waiting"] for item in numeric)
        done = sum(item["state"] in ("已结束", "无分配组合", "仅准备浏览器") for item in numeric)
        if setup.get("waiting") or setup.get("state") == "准备专用浏览器 / 登录":
            state["state"] = setup["state"]
        elif running or waiting:
            state["state"] = f"运行 {running} / 等待 {waiting} / 完成 {done}"
        elif all(item["state"] == "无分配组合" for item in numeric):
            state["state"] = "无分配组合"
        elif done == len(numeric):
            state["state"] = "已结束"
        elif any(item["state"] == "等待检查" for item in numeric):
            state["state"] = "等待启动采集槽"
        else:
            state["state"] = "已暂停" if self._control and self._control.requested == "paused" else "部分未完成"

    def _apply_lane_event(self, value: dict[str, Any]) -> None:
        browser, kind, item = value["browser"], value["kind"], value["value"]
        state = self._lane_states.get(browser)
        if state is None:
            return
        slot_key = str(value.get("slot", "1"))
        slot_state = state["slots"].get(slot_key)
        if slot_state is None:
            return
        if kind == "position":
            state["position"] = slot_state["position"] = redact_log(item)
        elif kind == "state":
            if not self._control or not self._control.requested:
                slot_state["state"] = str(item)
        elif kind == "finished":
            slot_state.update(waiting=False, state=str(item))
        elif kind == "waiting":
            if not self._control or self._control.requested:
                return
            slot_state.update(waiting=bool(item), state="等待人工登录 / 验证" if item else "继续采集 / 准备")
            if item:
                self.status_text.set(f"{BROWSER_NAMES[browser]} 等待人工登录 / 安全验证；完成后点击该侧的继续按钮。")
        self._sync_lane_state(browser)
        self._waiting = any(value["waiting"] for value in self._lane_states.values())
        self._render_lane_positions()
        self._render_controls()

    def _choose_root(self) -> None:
        if not self._closing and not self._busy:
            value = filedialog.askdirectory(parent=self, title="选择采集根目录（也可直接输入新目录）", mustexist=False)
            if value:
                self.collection_root.set(value)

    def _choose_scraper(self) -> None:
        if not self._closing and not self._busy:
            value = filedialog.askdirectory(parent=self, title="选择含 scripts/boss_cdp_raw.py 的上游目录", mustexist=True)
            if value:
                self.scraper_root.set(value)

    def _auxiliary(self, kind: str) -> None:
        if self._closing or self._aux_pending or (kind == "handoff" and self._busy):
            return
        if kind == "handoff" and not self._host_allows_start():
            self.status_text.set("后处理正在工作；未改变已有任务，请等待完成后再手动使用此数据。")
            return
        root_text = self.collection_root.get().strip()
        self._aux_pending = True
        self._render_controls()

        def work() -> None:
            value: dict[str, Any] = {"kind": kind, "root_text": root_text, "error": None}
            try:
                root = validate_collection_root(root_text)
                if not root.is_dir():
                    raise ValueError("采集目录尚不存在；开始采集后再打开或手动交给后处理。")
                if kind == "handoff":
                    if not (root / "raw").is_dir() or _reparse(root / "raw"):
                        raise ValueError("此目录尚无普通 raw 文件夹；请选择已采集的根目录。")
                    value["root"] = root
                elif os.name == "nt":
                    os.startfile(str(root))
                else:
                    raise ValueError("打开采集目录仅支持当前 Windows 桌面；请按显示的路径手动打开。")
            except (OSError, ValueError) as error:
                value["error"] = redact_log(error)
            self._emit("auxiliary", value)
        self._spawn("collection-directory", work)

    def open_collection_directory(self) -> None:
        self._auxiliary("open")

    def use_source(self) -> None:
        if self.on_use_source is not None:
            self._auxiliary("handoff")

    def _append_log(self, value: Any) -> None:
        text = redact_log(value).rstrip("\r\n")
        self.log_view.configure(state="normal")
        self.log_view.insert("end", text + "\n")
        lines = int(self.log_view.index("end-1c").split(".")[0])
        if lines > UI_LOG_LINES:
            self.log_view.delete("1.0", f"{lines - UI_LOG_LINES + 1}.0")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    def _apply_progress(self, value: dict[str, Any]) -> None:
        self._snapshot = value
        counts, total = value["counts"], value["total"]
        page_total = value["pages_requested"]
        self.progress_text.set(value["message"] + f" 已记录页 {value['pages_completed']}/{page_total if page_total is not None else '未知'}。")
        self.stats_text.set(f"成功 {counts['success']}　无数据 {counts['no_data']}　失败 {counts['failed']}　暂停 {counts['paused']}"
                            f"　运行中 {counts['running']}　原始条目（可重复）{value['jobs']}")
        self.progress_bar.configure(maximum=max(1, total or 1), value=value["done"] if total is not None else 0)
        selected = self.progress_table.selection()
        old_y = self.progress_table.yview()
        self.progress_table.delete(*self.progress_table.get_children())
        for index, row in enumerate(value["rows"]):
            status = row["status"]
            completed = _integer(row.get("pages_completed"))
            requested = _integer(row.get("pages_requested"))
            pages = f"{completed if completed is not None else '—'}/{requested if requested is not None else '—'}"
            jobs = _integer(row.get("job_count"))
            self.progress_table.insert("", "end", iid=str(index), tags=(status,), values=(
                redact_log(row["combination"]), redact_log(f"{row.get('city', '—')} / {row.get('track', '—')}"),
                STATUS_LABELS.get(status, STATUS_LABELS["unknown"]), pages, jobs if jobs is not None else "—"))
        if selected and self.progress_table.exists(selected[0]):
            self.progress_table.selection_set(selected[0])
        if old_y:
            self.progress_table.yview_moveto(old_y[0])

    def _finish_operation_if_safe(self) -> None:
        if self._operation_result is None or (self._operation_thread and self._operation_thread.is_alive()):
            return
        if any(not runner.shutdown_complete for lane in self._lanes.values() for runner in lane.all_runners):
            self.status_text.set("正在等待本次自有进程 / 输出线程退出；尚不能安全开始另一项操作。")
            return
        result, self._operation_result = self._operation_result, None
        self._waiting = False
        for state in self._lane_states.values():
            state["waiting"] = False
            if state["state"] == "等待检查":
                state["state"] = "未启动"
            for slot_state in state["slots"].values():
                slot_state["waiting"] = False
                if slot_state["state"] == "等待检查":
                    slot_state["state"] = "未启动"
        self._render_lane_positions()
        if result["error"]:
            self.status_text.set(f"未完成：{result['error']}")
        elif result.get("requested"):
            self.status_text.set("已暂停；已完成组合和检查点保留，可用开始 / 续采继续。" if result["requested"] == "paused"
                                 else "已停止；raw、清单和检查点保留，可稍后开始 / 续采。")
        elif result.get("code") == 0:
            self.status_text.set("浏览器准备完成；未执行采集或后处理。" if result["setup_only"]
                                 else "采集器已结束；请核对落盘清单。后处理须手动点击“用此数据新建后处理任务”，不会自动开始。")
        else:
            self.status_text.set("采集未全部完成；查看落盘失败 / 暂停状态，人工处理后开始 / 续采。")
        self._set_busy(False)
        self._render_controls()

    def _drain_events(self) -> None:
        if self._closing:
            return
        for _ in range(160):
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append_log(value)
            elif kind == "progress":
                self._apply_progress(value)
            elif kind == "lane":
                self._apply_lane_event(value)
            elif kind == "position":
                self.position_text.set("当前组合 / 页：" + redact_log(value))
            elif kind == "state":
                if self._control and not self._control.requested:
                    self.status_text.set(value)
            elif kind == "waiting":
                if self._busy and self._control and not self._control.requested:
                    self._waiting = True
                    self.status_text.set("等待人工登录 / 安全验证：请在专用 Chrome 中完成，确认后点击“人工处理完成，继续”。")
                    self._render_controls()
            elif kind == "scraper":
                original, resolved = value
                if not original and not self.scraper_root.get().strip():
                    self.scraper_root.set(resolved)
            elif kind == "preview":
                self._preview_active = False
                if value["generation"] == self._preview_id:
                    if "progress" in value:
                        self._apply_progress(value["progress"])
                    elif value.get("path_error"):
                        self.progress_text.set("输出路径只读检查：" + value["path_error"])
                    if value["error"]:
                        self.estimate_text.set("只读预览：" + value["error"])
                        self._plan_rows = []
                        self._render_plan_rows()
                    else:
                        self._apply_plan_preview(value)
                if self._preview_pending:
                    self._preview_pending = False
                    self._schedule_preview(0)
            elif kind == "active_plan":
                if self._checked_combinations is None:
                    self._checked_combinations = set(value["checked"])
                self._apply_loaded_plan(value["path"], value["config"], preserve_checks=True)
            elif kind in ("plan_editor_open", "plan_loaded"):
                if value["token"] != self._plan_action_id:
                    continue
                if value["error"]:
                    self._aux_pending = False
                    self.status_text.set("当前方案未改变：" + value["error"])
                elif kind == "plan_loaded":
                    self._aux_pending = False
                    self._apply_loaded_plan(value["path"], value["config"])
                    self.status_text.set("已只读加载新方案；请核对组合，点击“开始 / 续采”才会采集。")
                elif value["path"] != self.config_path:
                    self._aux_pending = False
                    self.status_text.set("当前方案已变化；请重新打开检索条件编辑器。")
                else:
                    self._editor = _CollectionPlanEditor(self, value["config"], value["editor_values"])
                    self.status_text.set("正在编辑独立草稿；只有“另存并应用新方案”会保存，取消不会改变当前计划。")
                self._render_controls()
            elif kind == "plan_editor_count":
                if value["editor"] is self._editor:
                    self._editor.receive_count(value)
            elif kind == "plan_editor_saved":
                if value["editor"] is self._editor:
                    self._editor.receive_saved(value)
            elif kind == "auxiliary":
                self._aux_pending = False
                if value["error"]:
                    self.status_text.set(value["error"])
                elif value["kind"] == "handoff":
                    if (not self._busy and self.collection_root.get().strip() == value["root_text"]
                            and self._host_allows_start() and self.on_use_source):
                        try:
                            self.on_use_source(value["root"])
                        except Exception as error:
                            self.status_text.set("手动交接未完成：" + redact_log(error))
                    else:
                        self.status_text.set("当前任务或路径已变化；未自动交接，请空闲后重新点击。")
                self._render_controls()
            elif kind == "finished":
                self._operation_result = value
        self._finish_operation_if_safe()
        self._schedule(70, self._drain_events)

    def request_close(self) -> None:
        """Nonblocking: signal our worker, cancel only this page's callbacks."""
        if self._closing:
            return
        self._closing = True
        if self._editor is not None:
            self._editor.close()
        if self._control:
            self._control.stop()
        for identifier in tuple(self._after_ids):
            try:
                self.after_cancel(identifier)
            except tk.TclError:
                pass
        self._after_ids.clear()
        self._preview_after = None
        for variable, identifier in self._traces:
            try:
                variable.trace_remove("write", identifier)
            except tk.TclError:
                pass
        self._traces.clear()
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                break

    def _destroyed(self, event: tk.Event) -> None:
        if event.widget is self:
            self.request_close()
            for variable in self._variables:
                variable.release()

    def destroy(self) -> None:
        if self.tk is None:
            return
        install_tk_lifecycle(self)  # Validate the Tk thread before closing children.
        # Editor shutdown may notify the page. Mark closing before any child
        # widgets disappear so that notification cannot redraw dead controls.
        self.request_close()
        destroy_tk_widget(self)


class _CollectionPlanEditor(tk.Toplevel):
    """Nonmodal, detached draft; only Save can publish a new plan file."""

    def __init__(self, page: CollectionPage, config: dict[str, Any], values: dict[str, Any]):
        super().__init__(page)
        self.page = page
        self.base_config = deepcopy(config)
        self.applied = False
        self._closed = False
        self._saving = False
        self._generation = 0
        self._cancel = threading.Event()
        self._after_ids: set[str] = set()
        self._count_after: str | None = None
        self._traces: list[tuple[tk.Variable, str]] = []
        self._variables: list[_PageStringVar] = []
        self._edit_controls: list[ttk.Widget | tk.Text] = []
        self.city_vars: dict[str, _PageStringVar] = {}
        self.salary_vars: dict[str, _PageStringVar] = {}
        self.experience_vars: dict[str, dict[str, _PageStringVar]] = {"current": {}, "target": {}}
        self.keyword_texts: dict[str, tk.Text] = {}
        self.title("编辑检索条件 · 另存新方案")
        self.geometry("920x700")
        self.minsize(800, 600)
        self.transient(page.winfo_toplevel())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        header = ttk.Frame(self, padding=(14, 12, 14, 6))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="编辑的是新方案草稿，原方案不会被覆盖。", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(header, text="城市 / 薪资 / 经验从此方案支持的选项中多选；学历固定为本科。关键词每行一个，可增删。",
                  style="Muted.TLabel", wraplength=860).pack(anchor="w", pady=(4, 0))
        center = ttk.Frame(self)
        center.grid(row=1, column=0, sticky="nsew", padx=14)
        center.columnconfigure(0, weight=1)
        center.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(center, borderwidth=0, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(center, orient="vertical", command=self.canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.body = ttk.Frame(self.canvas)
        self.body.columnconfigure(0, weight=1)
        self._canvas_window = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.canvas.bind("<Configure>", self._resize, add="+")
        self.body.bind("<Configure>", self._resize, add="+")
        city_card = ttk.Frame(self.body, style="Card.TFrame", padding=10)
        city_card.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        ttk.Label(city_card, text="城市（可多选）", style="Section.TLabel").pack(anchor="w")
        city_options = ttk.Frame(city_card, style="Card.TFrame")
        city_options.pack(fill="x", pady=(5, 0))
        self._add_choices(city_options, values["cities"], values["city_ids"], self.city_vars)
        salary_card = ttk.Frame(self.body, style="Card.TFrame", padding=10)
        salary_card.grid(row=1, column=0, sticky="ew", pady=(0, 7))
        ttk.Label(salary_card, text="薪资（可多选） · 学历：本科（固定）", style="Section.TLabel").pack(anchor="w")
        salary_options = ttk.Frame(salary_card, style="Card.TFrame")
        salary_options.pack(fill="x", pady=(5, 0))
        self._add_choices(salary_options, values["salaries"], values["salary_ids"], self.salary_vars)
        track_cards = ttk.Frame(self.body)
        track_cards.grid(row=2, column=0, sticky="ew")
        for column, track in enumerate(("current", "target")):
            track_cards.columnconfigure(column, weight=1, uniform="plan-group")
            card = ttk.Frame(track_cards, style="Card.TFrame", padding=10)
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 7) if not column else (0, 0))
            card.columnconfigure(0, weight=1)
            ttk.Label(card, text=TRACK_LABELS[track], style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
            ttk.Label(card, text="关键词（每行一个；空行忽略）", style="Hint.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 3))
            text = tk.Text(card, height=7, width=1, wrap="word", undo=True, relief="flat", borderwidth=0,
                           padx=6, pady=5, font=("Microsoft YaHei UI", 10))
            text.grid(row=2, column=0, sticky="nsew")
            text_scroll = ttk.Scrollbar(card, orient="vertical", command=text.yview)
            text_scroll.grid(row=2, column=1, sticky="ns")
            text.configure(yscrollcommand=text_scroll.set)
            text.insert("1.0", "\n".join(values[track]["keywords"]))
            text.edit_modified(False)
            text.bind("<<Modified>>", self._keywords_changed, add="+")
            self.keyword_texts[track] = text
            self._edit_controls.append(text)
            ttk.Label(card, text="经验范围（可多选；分组不是岗位类别）", style="Hint.TLabel").grid(
                row=3, column=0, columnspan=2, sticky="w", pady=(8, 3))
            experience_options = ttk.Frame(card, style="Card.TFrame")
            experience_options.grid(row=4, column=0, columnspan=2, sticky="ew")
            self._add_choices(experience_options, values["experiences"][track], values[track]["experience_ids"],
                              self.experience_vars[track], columns=2)
        footer = ttk.Frame(self, padding=(14, 8, 14, 12))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.count_text = self._var("正在计算草稿组合数…")
        self.status_text = self._var("只保存新方案，不会自动开始采集。取消会丢弃草稿。")
        ttk.Label(footer, textvariable=self.count_text, wraplength=860).grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(footer, textvariable=self.status_text, style="Muted.TLabel", wraplength=860).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        ttk.Label(footer, text=f"新方案另存目录：{PLAN_DIRECTORY}", style="Muted.TLabel", wraplength=860).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(3, 7))
        self.save_button = ttk.Button(footer, text="另存并应用新方案", style="Primary.TButton", command=self.save)
        self.save_button.grid(row=3, column=0, sticky="e", padx=(0, 8))
        self.cancel_button = ttk.Button(footer, text="取消", command=self.close)
        self.cancel_button.grid(row=3, column=1, sticky="e")
        for variable in (*self.city_vars.values(), *self.salary_vars.values(),
                         *(variable for group in self.experience_vars.values() for variable in group.values())):
            self._traces.append((variable, variable.trace_add("write", self._changed)))
        self.bind("<Destroy>", self._destroyed, add="+")
        self.apply_theme(page.palette)
        self._changed()

    def _var(self, value: str) -> _PageStringVar:
        variable = _PageStringVar(self, value)
        self._variables.append(variable)
        return variable

    def _add_choices(self, parent: ttk.Frame, options: list[dict[str, str]], selected: list[str],
                     variables: dict[str, _PageStringVar], *, columns: int = 5) -> None:
        selected_ids = set(selected)
        for index, option in enumerate(options):
            variable = self._var("1" if option["id"] in selected_ids else "0")
            variables[option["id"]] = variable
            button = ttk.Checkbutton(parent, text=option["label"], variable=variable, onvalue="1", offvalue="0")
            button.grid(row=index // columns, column=index % columns, sticky="w", padx=(0, 13), pady=2)
            self._edit_controls.append(button)

    def _resize(self, _event: tk.Event | None = None) -> None:
        if self._closed:
            return
        self.canvas.itemconfigure(self._canvas_window, width=max(1, self.canvas.winfo_width()))
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def apply_theme(self, palette: Mapping[str, str]) -> None:
        if self._closed:
            return
        self.configure(background=palette["background"])
        self.canvas.configure(background=palette["background"])
        for text in self.keyword_texts.values():
            text.configure(background=palette["log"], foreground=palette["log_text"],
                           insertbackground=palette["log_text"], selectbackground=palette["log_selection"],
                           selectforeground=palette["log_selected_text"])

    def _schedule_count(self) -> None:
        if self._closed or self._saving:
            return
        holder: list[str] = []

        def invoke() -> None:
            if holder:
                self._after_ids.discard(holder[0])
            self._count_after = None
            if not self._closed and not self._saving:
                self._begin_count()
        identifier = self.after(150, invoke)
        holder.append(identifier)
        self._after_ids.add(identifier)
        self._count_after = identifier

    def _cancel_count_callback(self) -> None:
        if self._count_after:
            try:
                self.after_cancel(self._count_after)
            except tk.TclError:
                pass
            self._after_ids.discard(self._count_after)
            self._count_after = None

    def _changed(self, *_args: object) -> None:
        if self._closed or self._saving:
            return
        self._generation += 1
        self.count_text.set("正在后台计算草稿组合数…")
        self._cancel_count_callback()
        self._schedule_count()

    def _keywords_changed(self, event: tk.Event) -> None:
        if self._closed:
            return
        if event.widget.edit_modified():
            event.widget.edit_modified(False)
            self._changed()

    def _capture_values(self) -> dict[str, list[str]]:
        selected = lambda variables: [key for key, variable in variables.items() if variable.get() == "1"]
        return {"city_ids": selected(self.city_vars), "salary_ids": selected(self.salary_vars),
                "current_keywords": self.keyword_texts["current"].get("1.0", "end-1c").splitlines(),
                "target_keywords": self.keyword_texts["target"].get("1.0", "end-1c").splitlines(),
                "current_experience_ids": selected(self.experience_vars["current"]),
                "target_experience_ids": selected(self.experience_vars["target"])}

    def _begin_count(self) -> None:
        generation, inputs = self._generation, self._capture_values()
        base = deepcopy(self.base_config)

        def work() -> None:
            value: dict[str, Any] = {"editor": self, "generation": generation, "error": None}
            try:
                candidate = plans.update_plan_config(base, **inputs)
                specs = collector.build_specs(candidate)
                value["counts"] = {track: sum(spec["track"] == track for spec in specs) for track in ("current", "target")}
            except Exception as error:
                value["error"] = redact_log(error)
            self.page._emit("plan_editor_count", value)
        self.page._spawn("collection-plan-count", work)

    def receive_count(self, value: dict[str, Any]) -> None:
        if self._closed or self._saving or value["generation"] != self._generation:
            return
        if value["error"]:
            self.count_text.set("草稿尚不能应用：" + value["error"])
        else:
            counts = value["counts"]
            self.count_text.set(f"草稿完整组合：{sum(counts.values())} 组（当前求职 {counts['current']} · 成长参考 {counts['target']}）"
                                "；应用后可再逐组勾选和设置本次上限。")

    def _render_saving(self) -> None:
        for widget in self._edit_controls:
            if isinstance(widget, tk.Text):
                widget.configure(state="disabled" if self._saving else "normal")
            else:
                widget.state(["disabled"] if self._saving else ["!disabled"])
        self.save_button.state(["disabled"] if self._saving else ["!disabled"])

    def save(self) -> None:
        if self._closed or self._saving:
            return
        inputs, base = self._capture_values(), deepcopy(self.base_config)
        self._saving = True
        self._generation += 1
        self._cancel_count_callback()
        self._render_saving()
        self.status_text.set("正在校验并另存唯一的新方案；不会覆盖原文件，也不会启动采集。")

        def work() -> None:
            value: dict[str, Any] = {"editor": self, "error": None}
            try:
                candidate = _validated_page_config(plans.update_plan_config(base, **inputs))
                if self._cancel.is_set():
                    return
                value["path"] = plans.save_plan_config(candidate, PLAN_DIRECTORY)
                value["config"] = candidate
            except Exception as error:
                value["error"] = redact_log(error)
            self.page._emit("plan_editor_saved", value)
        self.page._spawn("collection-plan-save", work)

    def receive_saved(self, value: dict[str, Any]) -> None:
        if self._closed:
            return
        if value["error"]:
            self._saving = False
            self._render_saving()
            self.status_text.set("未另存或应用：" + value["error"])
            self._changed()
            return
        self.page._apply_loaded_plan(value["path"], value["config"])
        self.page.status_text.set("已另存并应用新方案；请查看检索组合预览，明确点击“开始 / 续采”才会采集。")
        self.applied = True
        self.close()

    def _dispose(self) -> None:
        self._closed = True
        self._cancel.set()
        for identifier in tuple(self._after_ids):
            try:
                self.after_cancel(identifier)
            except tk.TclError:
                pass
        self._after_ids.clear()
        self._count_after = None
        for variable, identifier in self._traces:
            try:
                variable.trace_remove("write", identifier)
            except tk.TclError:
                pass
        self._traces.clear()

    def close(self) -> None:
        if self._closed:
            return
        self._dispose()
        self.page._editor_closed(self)
        self.destroy()

    def _destroyed(self, event: tk.Event) -> None:
        if event.widget is self:
            self._dispose()
            self.page._editor_closed(self)
            for variable in self._variables:
                variable.release()

    def destroy(self) -> None:
        destroy_tk_widget(self)
