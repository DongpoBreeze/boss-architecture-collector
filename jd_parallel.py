"""Collect a JD batch with independent CDP connections and background pages.

The caller owns browser preparation, login, batching, and resume decisions.
Only this adapter's targets are closed; the browser and user tabs stay open.
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from types import ModuleType
from typing import Any, Callable
from urllib.parse import urlparse


DEFAULT_JD_WORKERS = 3
MAX_JD_WORKERS = 32
SCROLL_COUNT = 3
CDP_PORT = 9222
EDGE_CDP_PORT = 9223
DEFAULT_BROWSER_MODE = "single"
_BROWSER_NAMES = ("chrome", "edge")
GATE_EXIT_CODE = 2
INTERRUPTED_EXIT_CODE = 130
SHUTDOWN_TIMEOUT = 12.0

_SCROLL_JS = (
    "window.scrollBy(0, Math.max(260, Math.min(600, window.innerHeight * 0.75))); void 0;"
)
_SECURITY_TEXT_RE = re.compile(
    r"(?:^|\n)\s*(?:(?:安全验证|安全校验|人工验证|人机验证|行为验证|访问异常)"
    r"\s*[:：]?\s*(?=\n|$)|请完成(?:安全|人机|下方)?验证|请先完成验证|"
    r"请进行安全验证|请(?:按住|拖动)滑块|请通过下方验证|当前环境存在异常|"
    r"您的访问行为异常|系统检测到(?:您的)?访问异常)"
)


@dataclass(frozen=True)
class RenderTiming:
    """Internal injection point for offline tests; scroll count is not tunable."""

    page_wait: float
    page_timeout: float
    scroll_wait: float
    poll_interval: float = 0.25


_TIMINGS = {
    "fast": RenderTiming(page_wait=3.0, page_timeout=10.0, scroll_wait=0.5),
    "safe": RenderTiming(page_wait=5.0, page_timeout=15.0, scroll_wait=1.0),
}


class _Cancelled(Exception):
    pass


class _Gate(Exception):
    pass


class StopState:
    """A gate stops scheduling and wakes every page's render wait immediately."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.lock = threading.RLock()
        self.reason = ""
        self.gate_message = ""

    def request_stop(self, reason: str = "stopped", *, gate_message: str = "") -> None:
        with self.lock:
            if gate_message:
                self.gate_message = self.gate_message or gate_message
                self.reason = "paused"
            elif not self.reason:
                self.reason = reason
            self.event.set()

    def check(self) -> None:
        if self.event.is_set():
            raise _Cancelled(self.reason or "stopped")

    def wait(self, seconds: float) -> None:
        if self.event.wait(max(0.0, seconds)):
            self.check()


@dataclass
class BatchResult:
    records: list[dict[str, Any]]
    status: str
    total: int
    gate_message: str = ""
    browser: str = "chrome"
    port: int = CDP_PORT
    profile_dir: str | None = None
    browser_results: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        if self.status == "paused":
            return GATE_EXIT_CODE
        if self.status == "stopped":
            return INTERRUPTED_EXIT_CODE
        # Individual failures are durable records. The caller uses completeness
        # to distinguish a partial batch from a fatal adapter failure.
        return 0


@dataclass(frozen=True)
class BrowserConfig:
    """Connection settings for one isolated browser instance.

    ``profile_dir`` is metadata for the launcher and manifest. The adapter
    only connects to the supplied CDP port and never reads browser profiles.
    """

    name: str
    port: int
    profile_dir: str | Path | None = None

    def __post_init__(self) -> None:
        normalized = str(self.name).strip().lower()
        if normalized not in _BROWSER_NAMES:
            raise ValueError("浏览器必须为 chrome 或 edge")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("CDP 端口必须为 1–65535 的整数")
        object.__setattr__(self, "name", normalized)
        if self.profile_dir is not None:
            object.__setattr__(self, "profile_dir", str(self.profile_dir))


def default_browser_configs() -> dict[str, BrowserConfig]:
    """Return independent, credential-free defaults for Chrome and Edge."""

    home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    root = Path(home).expanduser() if home else Path.cwd()
    return {
        "chrome": BrowserConfig("chrome", CDP_PORT,
                                root / ".boss-zhipin-scraper" / "chrome-profile"),
        "edge": BrowserConfig("edge", EDGE_CDP_PORT,
                              root / ".boss-zhipin-scraper" / "edge-profile"),
    }


def load_scraper(scraper_root: Path) -> ModuleType:
    """Read helpers from the existing installation without modifying it."""
    source = Path(scraper_root).expanduser().resolve() / "scripts" / "boss_cdp_raw.py"
    if not source.is_file():
        raise FileNotFoundError(f"找不到上游采集器：{source}")
    name = "_boss_jd_parallel_upstream"
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载上游采集器：{source}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module  # Upstream dataclasses resolve their module here.
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module


class _OwnedConnection:
    """Track even targets created before create_page_session fails to attach.

    Each instance is confined to its worker. No two threads read the same
    WebSocket. Cleanup uses only IDs returned by this connection's own create.
    """

    def __init__(self, cdp: Any, control: StopState) -> None:
        self.cdp = cdp
        self.control = control
        self.targets: list[str] = []
        self.cleanup_errors: list[str] = []

    def send(self, method: str, params=None, sid=None, timeout=3.0):
        self.control.check()
        response = self.cdp.send(method, params, sid, timeout=min(timeout, 3.0))
        if method == "Target.createTarget":
            target = response.get("result", {}).get("targetId")
            if target:
                self.targets.append(target)
        if response.get("error"):
            message = response["error"].get("message", "CDP protocol error")
            raise RuntimeError(f"{method}: {message}")
        return response

    def eval_js(self, expression: str, sid: str):
        response = self.send(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}, sid,
        )
        result = response.get("result", {})
        if result.get("exceptionDetails"):
            raise RuntimeError("详情页 JavaScript 执行失败")
        return result.get("result", {}).get("value")

    def close(self) -> None:
        # Deliberately bypass the cancellation check while releasing our tabs.
        for target in self.targets:
            try:
                response = self.cdp.send("Target.closeTarget", {"targetId": target}, timeout=3.0)
                if response.get("error") or response.get("result", {}).get("success") is False:
                    self.cleanup_errors.append("自有详情页关闭失败")
            except Exception as exc:
                self.cleanup_errors.append(f"自有详情页关闭失败：{type(exc).__name__}")
        try:
            self.cdp.close()
        except Exception as exc:
            self.cleanup_errors.append(f"CDP 连接关闭失败：{type(exc).__name__}")


@dataclass
class _Capture:
    latest: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    tags: list = field(default_factory=list)


def _gate_message(snapshot: dict[str, Any], scraper: ModuleType) -> str:
    url = urlparse(str(snapshot.get("url") or ""))
    path = url.path.casefold()
    if re.search(r"security[_-]check|/captcha(?:/|$)|/verify(?:/|$)", path + "?" + url.query.casefold()):
        return "安全验证：详情页需要人工验证，已停止调度"
    if (url.hostname or "").casefold().startswith("passport.") or re.search(
        r"/(?:login|signin)(?:/|\.|$)|/web/user(?:/|$)", path,
    ):
        return "登录状态异常：详情页跳转登录，已停止调度"
    diagnostic = "\n".join(str(snapshot.get(key) or "") for key in ("jd", "page_text"))
    if _SECURITY_TEXT_RE.search(diagnostic) or (
        not str(snapshot.get("jd") or "").strip()
        and re.search(r"(?:^|\n)\s*(?:请输入验证码|验证码|拖动滑块)", diagnostic)
    ):
        return "安全验证：详情页需要人工验证，已停止调度"
    try:
        # Reuse the upstream login-wall and navigation checks. Do not invoke
        # its login probe, which would navigate another page unnecessarily.
        scraper.extract_detail_fields(snapshot)
    except scraper.DetailLoginRequiredError:
        return "登录状态异常：登录查看完整内容，已停止调度"
    except scraper.DetailExtractionError:
        pass
    return ""


def _read_snapshot(page: _OwnedConnection, sid: str, scraper: ModuleType,
                   capture: _Capture, control: StopState) -> dict[str, Any]:
    value = page.eval_js(scraper.EXTRACT_DETAIL_JS, sid)
    snapshot = json.loads(value) if isinstance(value, str) else value
    if not isinstance(snapshot, dict):
        raise ValueError("详情提取未返回对象")
    capture.latest = snapshot
    message = _gate_message(snapshot, scraper)
    if message:
        control.request_stop(gate_message=message)
        raise _Gate(message)
    # Preserve only text the upstream extractor can identify as a JD. Page
    # chrome, a login screen, and arbitrary body text never become partial JD.
    try:
        fields = scraper.extract_detail_fields(snapshot, min_length=1)
    except scraper.DetailExtractionError:
        fields = {}
    if len(fields.get("jd", "")) > len(capture.fields.get("jd", "")):
        capture.fields = fields
        capture.tags = snapshot.get("tags") if isinstance(snapshot.get("tags"), list) else []
    return snapshot


def _wait_for_page(page: _OwnedConnection, sid: str, scraper: ModuleType,
                   capture: _Capture, control: StopState, timing: RenderTiming) -> None:
    started = time.monotonic()
    while True:
        snapshot = _read_snapshot(page, sid, scraper, capture, control)
        elapsed = time.monotonic() - started
        content = str(snapshot.get("jd") or "").strip() or (
            scraper.DETAIL_DESCRIPTION_MARKER in str(snapshot.get("page_text") or "")
        )
        if elapsed >= timing.page_wait and (content or elapsed >= timing.page_timeout):
            return
        remaining = max(timing.page_wait, timing.page_timeout) - elapsed
        control.wait(min(timing.poll_interval, max(0.0, remaining)))


def _wait_for_scroll(page: _OwnedConnection, sid: str, scraper: ModuleType,
                     capture: _Capture, control: StopState, timing: RenderTiming) -> None:
    deadline = time.monotonic() + timing.scroll_wait
    while True:
        _read_snapshot(page, sid, scraper, capture, control)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        control.wait(min(timing.poll_interval, remaining))


def _detail_record(scraper: ModuleType, job: dict, fields: dict, tags: list,
                   status: str, *, error: str = "", gate: bool = False) -> dict:
    record = scraper.build_detail_record(job, {**fields, "tags": tags})
    record.update({
        "status": status,
        "jd_complete": status == "success",
        "jd_completeness": "complete" if status == "success" else "partial" if record.get("jd") else "missing",
        "security_or_login_gate": gate,
    })
    if error:
        record["error"] = error
    return record


def _collect_one(job: dict, scraper: ModuleType, factory: Callable[[], Any],
                 control: StopState, timing: RenderTiming) -> dict | None:
    page = None
    capture = _Capture()
    record = None
    try:
        control.check()
        url = scraper.build_detail_url(job)
        if not url:
            raise ValueError("岗位缺少详情链接")
        cdp = factory()
        # Register the connection before any operation that could fail.
        page = _OwnedConnection(cdp, control)
        socket = getattr(cdp, "ws", None)
        if socket is not None:
            socket.settimeout(3.0)
        control.check()
        _, sid = scraper.create_page_session(page, background=True)
        navigation = page.send("Page.navigate", {"url": url}, sid)
        if navigation.get("result", {}).get("errorText"):
            raise RuntimeError(f"页面导航失败：{navigation['result']['errorText']}")
        _wait_for_page(page, sid, scraper, capture, control, timing)
        for _ in range(SCROLL_COUNT):
            control.check()
            page.eval_js(_SCROLL_JS, sid)
            _wait_for_scroll(page, sid, scraper, capture, control, timing)
        control.check()
        fields = scraper.extract_detail_fields(capture.latest)
        tags = capture.latest.get("tags") if isinstance(capture.latest.get("tags"), list) else []
        record = _detail_record(scraper, job, fields, tags, "success")
    except _Gate as exc:
        record = _detail_record(scraper, job, capture.fields, capture.tags, "paused", error=str(exc), gate=True)
    except _Cancelled:
        if page is not None:
            record = _detail_record(scraper, job, capture.fields, capture.tags, "paused" if control.gate_message else "stopped")
    except scraper.DetailExtractionError as exc:
        # Third-party exception bodies may contain URLs, cookies or tokens.
        # Keep the error type for diagnosis, never its raw message.
        record = _detail_record(
            scraper, job, capture.fields, capture.tags, "partial" if capture.fields else "failed",
            error=f"{type(exc).__name__}: 详情正文提取失败，异常正文未保存",
        )
    except Exception as exc:
        record = _detail_record(
            scraper, job, capture.fields, capture.tags, "failed",
            error=f"{type(exc).__name__}: 详情抓取失败，异常正文未保存",
        )
    finally:
        if page is not None:
            page.close()
            if record is not None and page.cleanup_errors:
                record["cleanup_errors"] = page.cleanup_errors
    return record


def _job_key(job: dict[str, Any]) -> str:
    for field_name in ("job_id", "dedupe_key", "job_link", "link", "url"):
        value = job.get(field_name)
        if value:
            return str(value)
    # Input jobs are expected to have an ID. This fallback keeps malformed
    # records deterministic without accidentally treating two jobs as one.
    return json.dumps(job, ensure_ascii=False, sort_keys=True, default=str)


def _record_key(record: dict[str, Any]) -> str:
    for field_name in ("job_id", "encrypt_job_id", "dedupe_key", "job_link", "link", "url"):
        value = record.get(field_name)
        if value:
            return str(value)
    return ""


def detail_completeness(record: dict[str, Any] | None) -> str:
    """Shared quality rule for legacy, single and dual JD checkpoints."""
    if not record or not str(record.get("jd") or "").strip():
        return "missing"
    states = " ".join(str(record.get(key) or "").casefold() for key in ("status", "jd_status", "jd_completeness"))
    if (record.get("complete") is False or record.get("jd_complete") is False
            or record.get("truncated") is True
            or any(word in states for word in ("partial", "incomplete", "failed", "error", "部分", "截断"))):
        return "partial"
    return "complete"


def detail_quality(record: dict[str, Any]) -> tuple[int, int]:
    return {"missing": 0, "partial": 1, "complete": 2}[detail_completeness(record)], len(str(record.get("jd") or "").strip())


def _record_is_complete(record: dict[str, Any]) -> bool:
    return detail_completeness(record) == "complete"


def _best_records(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        key = _record_key(record)
        if key and (key not in result or detail_quality(record) > detail_quality(result[key])):
            result[key] = record
    return result


def _read_checkpoint(path: Path) -> list[dict[str, Any]]:
    try:
        if not path.exists():
            return []
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("records", "jobs", "details"):
            if isinstance(value.get(key), list):
                return [item for item in value[key] if isinstance(item, dict)]
    return []


def _side_output_path(path: Path, browser: str) -> Path:
    return path.with_name(f"{path.stem}.{browser}{path.suffix}")


def _collect_details_single(list_data: dict | list, output_path: Path, *, scraper: ModuleType,
                            workers: int = DEFAULT_JD_WORKERS, speed_profile: str = "fast",
                            cdp_factory: Callable[[], Any] | None = None,
                            control: StopState | None = None, timing: RenderTiming | None = None,
                            persist: Callable[[str, list], None] | None = None,
                            emit: Callable[[str], None] | None = None,
                            resume: bool = True,
                            browser: str = "chrome", port: int = CDP_PORT,
                            profile_dir: str | Path | None = None) -> BatchResult:
    """Run one already-selected batch; no screening, login flow, or cache policy."""
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= MAX_JD_WORKERS:
        raise ValueError(f"JD 页面并发必须为 1–{MAX_JD_WORKERS} 的整数")
    if speed_profile not in _TIMINGS:
        raise ValueError("详情速度档位必须为 fast 或 safe")
    jobs = list_data.get("jobs", []) if isinstance(list_data, dict) else list_data
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise ValueError("输入必须为岗位列表或包含 jobs 列表的对象")
    control = control or StopState()
    timing = timing or _TIMINGS[speed_profile]
    persist = persist or scraper._atomic_write_json
    emit = emit or (lambda message: print(message, flush=True))
    if cdp_factory is None:
        if jobs and not scraper.require_runtime_dependencies("requests", "websocket"):
            raise RuntimeError("缺少 CDP 运行依赖")
        cdp_factory = lambda: scraper.CDPSession(port)

    completed: dict[int, dict] = {}
    pending: dict[Future, int] = {}
    next_index = 0
    gate_logged = False

    # Completed records are durable checkpoints. A paused/failed record is
    # intentionally retried, while a successful record is not requested again.
    if resume:
        old_by_key = _best_records(_read_checkpoint(Path(output_path)))
        for index, job in enumerate(jobs):
            old = old_by_key.get(_job_key(job))
            if old is not None:
                completed[index] = old
        while next_index < len(jobs) and next_index in completed and _record_is_complete(completed[next_index]):
            next_index += 1

    def records() -> list[dict]:
        return [completed[index] for index in sorted(completed)]

    def save_and_report(future: Future, index: int) -> None:
        nonlocal gate_logged
        if future.cancelled():
            return
        record = future.result()
        if record is None:
            return
        if index not in completed or detail_quality(record) >= detail_quality(completed[index]):
            completed[index] = record
        persist(str(output_path), records())
        if control.gate_message and not gate_logged:
            emit(control.gate_message)
            gate_logged = True
        emit(f"[{len(completed)}/{len(jobs)}] JD: {len(record.get('jd', ''))}字，状态：{record['status']}")

    if not resume or not Path(output_path).exists():
        persist(str(output_path), records())
    emit(f"{browser} JD 页面并发 {workers}，每页固定滚动 {SCROLL_COUNT} 次，岗位 {len(jobs)} 个，已恢复 {len(completed)} 个")
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jd-page")
    try:
        while pending or next_index < len(jobs):
            # Gate detection and scheduling share a lock. At most N futures
            # exist, so a stopped batch has no unbounded queue to drain.
            with control.lock:
                while next_index < len(jobs) and next_index in completed and _record_is_complete(completed[next_index]):
                    next_index += 1
                while len(pending) < workers and next_index < len(jobs) and not control.event.is_set():
                    future = executor.submit(_collect_one, dict(jobs[next_index]), scraper, cdp_factory, control, timing)
                    pending[future] = next_index
                    next_index += 1
                    while next_index < len(jobs) and next_index in completed and _record_is_complete(completed[next_index]):
                        next_index += 1
            if not pending:
                break
            done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=lambda item: pending[item]):
                index = pending.pop(future)
                save_and_report(future, index)
    except BaseException:
        control.request_stop()
        raise
    finally:
        active_error = sys.exc_info()[0] is not None
        if pending:
            control.request_stop()
            wait(pending, timeout=SHUTDOWN_TIMEOUT)
        executor.shutdown(wait=False, cancel_futures=True)
        # Signals and fatal main-thread errors still preserve work that reached
        # a result before shutdown. Page workers close their targets in finally.
        for future, index in sorted(pending.items(), key=lambda item: item[1]):
            if future.done():
                try:
                    save_and_report(future, index)
                except BaseException:
                    # Preserve an earlier coordinator/persistence failure. A
                    # surviving side checkpoint remains available for resume.
                    if not active_error:
                        raise
    if control.gate_message:
        status = "paused"
        if not gate_logged:
            emit(control.gate_message)
    elif control.event.is_set():
        status = "stopped"
    else:
        status = "partial" if any(not _record_is_complete(record) for record in completed.values()) else "completed"
    emit(f"JD 并发结束：status={status}，已保存 {len(completed)}/{len(jobs)}")
    return BatchResult(records(), status, len(jobs), control.gate_message,
                       browser=browser, port=port,
                       profile_dir=str(profile_dir) if profile_dir is not None else None)


class _ParentStopWatcher:
    """Mirror an outer stop into one side without sharing gate state."""

    def __init__(self, parent: StopState, child: StopState) -> None:
        self.parent, self.child = parent, child
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="jd-stop-watch")

    def _run(self) -> None:
        while not self.done.wait(0.05):
            if self.parent.event.is_set():
                self.child.request_stop("stopped")
                return

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.done.set()
        self.thread.join(timeout=1)


def _normalize_browser_configs(browser_configs: Any) -> dict[str, BrowserConfig]:
    defaults = default_browser_configs()
    if browser_configs is None:
        return defaults
    if isinstance(browser_configs, dict):
        values = browser_configs.items()
    elif isinstance(browser_configs, (list, tuple)):
        values = ((item.name, item) for item in browser_configs)
    else:
        raise ValueError("browser_configs 必须为映射或 BrowserConfig 列表")
    result = dict(defaults)
    for name, value in values:
        if isinstance(value, BrowserConfig):
            config = value
        elif isinstance(value, dict):
            config = BrowserConfig(value.get("name", name), value["port"], value.get("profile_dir"))
        else:
            raise ValueError("browser_configs 的值必须是 BrowserConfig 或对象")
        result[str(name).lower()] = config
    return result


def _factory_for(name: str, config: BrowserConfig, scraper: ModuleType,
                 cdp_factory: Callable[[], Any] | None,
                 cdp_factories: Any) -> Callable[[], Any] | None:
    if cdp_factories is not None:
        if isinstance(cdp_factories, dict):
            return cdp_factories.get(name)
        if isinstance(cdp_factories, (list, tuple)):
            return cdp_factories[0 if name == "chrome" else 1]
        if callable(cdp_factories):
            return cdp_factories if name == "chrome" else None
        raise ValueError("cdp_factories 必须为映射、序列或可调用对象")
    if cdp_factory is not None:
        # The legacy single-browser injection belongs to Chrome only. Edge
        # must receive its own port/factory unless dual callers explicitly
        # provide a mapping or sequence.
        return cdp_factory if name == "chrome" else lambda: scraper.CDPSession(config.port)
    return lambda: scraper.CDPSession(config.port)


def collect_details(list_data: dict | list, output_path: Path, *, scraper: ModuleType,
                    workers: int = DEFAULT_JD_WORKERS, speed_profile: str = "fast",
                    cdp_factory: Callable[[], Any] | None = None,
                    control: StopState | None = None, timing: RenderTiming | None = None,
                    persist: Callable[[str, list], None] | None = None,
                    emit: Callable[[str], None] | None = None,
                    browser_mode: str = DEFAULT_BROWSER_MODE,
                    browser_configs: Any = None, cdp_factories: Any = None,
                    resume: bool = True, browser_readiness: dict[str, Any] | None = None) -> BatchResult:
    """Collect details on one browser or two isolated browser connections.

    Dual mode partitions the input deterministically (Chrome receives the
    first ceil(N/2) jobs), runs both sides concurrently, and merges durable
    side checkpoints after every completion. A gate on one side does not stop
    the other side; an outer stop still stops both sides.
    """
    if browser_mode not in ("single", "dual"):
        raise ValueError("browser_mode 必须为 single 或 dual")
    if speed_profile not in _TIMINGS:
        raise ValueError("详情速度档位必须为 fast 或 safe")
    if browser_mode == "single":
        config = _normalize_browser_configs(browser_configs)["chrome"]
        return _collect_details_single(
            list_data, output_path, scraper=scraper, workers=workers,
            speed_profile=speed_profile, cdp_factory=cdp_factory,
            control=control, timing=timing, persist=persist, emit=emit,
            resume=resume, browser=config.name, port=config.port,
            profile_dir=config.profile_dir,
        )

    jobs = list_data.get("jobs", []) if isinstance(list_data, dict) else list_data
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise ValueError("输入必须为岗位列表或包含 jobs 列表的对象")
    if jobs and cdp_factories is None and cdp_factory is None:
        if not scraper.require_runtime_dependencies("requests", "websocket"):
            raise RuntimeError("缺少 CDP 运行依赖")
    configs = _normalize_browser_configs(browser_configs)
    if configs["chrome"].port == configs["edge"].port:
        raise ValueError("双浏览器必须使用独立 CDP 端口")
    chrome_profile, edge_profile = (configs[name].profile_dir for name in _BROWSER_NAMES)
    if chrome_profile and edge_profile and os.path.normcase(os.path.abspath(chrome_profile)) == os.path.normcase(os.path.abspath(edge_profile)):
        raise ValueError("双浏览器必须使用独立 profile 目录")
    parent = control or StopState()
    timing = timing or _TIMINGS.get(speed_profile)
    if timing is None:
        raise ValueError("详情速度档位必须为 fast 或 safe")
    persist = persist or scraper._atomic_write_json
    emit = emit or (lambda message: print(message, flush=True))
    split = (len(jobs) + 1) // 2
    groups = {"chrome": jobs[:split], "edge": jobs[split:]}
    # Read all three files before changing a shard. A prior crash can leave a
    # complete JD only in the side which no longer owns it after repartition.
    recovered = _best_records([
        record for path in (Path(output_path), *(_side_output_path(Path(output_path), name) for name in _BROWSER_NAMES))
        for record in (_read_checkpoint(path) if resume else [])
    ])
    side_records = {
        name: [recovered[_job_key(job)] for job in groups[name] if _job_key(job) in recovered]
        for name in _BROWSER_NAMES
    }
    side_lock = threading.RLock()
    side_dirty = [False]
    side_controls = {name: StopState() for name in _BROWSER_NAMES}

    def merged_records() -> list[dict[str, Any]]:
        by_key = _best_records([*recovered.values(), *(item for name in _BROWSER_NAMES for item in side_records[name])])
        order = {_job_key(job): index for index, job in enumerate(jobs)}
        return sorted(by_key.values(), key=lambda item: (order.get(_record_key(item), len(order)), _record_key(item)))

    def stop_all() -> None:
        parent.request_stop()
        for side_control in side_controls.values():
            side_control.request_stop()

    def run_side(name: str) -> BatchResult:
        config = configs[name]
        side_path = _side_output_path(Path(output_path), name)
        side_control = side_controls[name]
        watcher = _ParentStopWatcher(parent, side_control)
        watcher.start()
        try:
            def side_persist(_path: str, records: list) -> None:
                with side_lock:
                    side_records[name] = list(_best_records([*side_records[name], *records]).values())
                    # Side checkpoints are written directly and contain only
                    # jobs owned by this browser; the public callback observes
                    # the deterministic merged checkpoint from the parent
                    # thread after this side future completes.
                    side_dirty[0] = True
                    snapshot = list(side_records[name])
                scraper._atomic_write_json(str(side_path), snapshot)

            initial = list(side_records[name])
            scraper._atomic_write_json(str(side_path), initial)
            readiness = (browser_readiness or {}).get(name, True)
            ready = readiness.get("ready", False) if isinstance(readiness, dict) else readiness is True
            unfinished = [job for job in groups[name] if not _record_is_complete(recovered.get(_job_key(job), {}))]
            if not ready and unfinished:
                message = f"{name} 预检未通过，已暂停该侧；请准备浏览器后重试"
                saved = _best_records(initial)
                for job in unfinished:
                    key = _job_key(job)
                    saved[key] = {**job, **saved.get(key, {}), "status": "paused", "jd_complete": False,
                                  "pause_reason": "cdp_setup_failed"}
                side_persist(str(side_path), list(saved.values()))
                emit(message)
                return BatchResult(list(saved.values()), "paused", len(groups[name]), message,
                                   browser=name, port=config.port, profile_dir=config.profile_dir)
            result = _collect_details_single(
                {"jobs": groups[name]}, side_path, scraper=scraper, workers=workers,
                speed_profile=speed_profile, cdp_factory=_factory_for(name, config, scraper, cdp_factory, cdp_factories),
                control=side_control, timing=timing, persist=side_persist, emit=emit,
                resume=True, browser=name, port=config.port, profile_dir=config.profile_dir,
            )
            with side_lock:
                side_records[name] = result.records
                side_dirty[0] = True
            return result
        finally:
            watcher.close()

    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jd-browser")
    futures: dict[Future, str] = {}
    results: dict[str, BatchResult] = {}
    try:
        # Recovery is durable before any side file can be re-sharded/overwritten.
        persist(str(output_path), merged_records())
        futures = {executor.submit(run_side, name): name for name in _BROWSER_NAMES}
        remaining = set(futures)
        while remaining:
            done, remaining = wait(remaining, timeout=0.05, return_when=FIRST_COMPLETED)
            with side_lock:
                if side_dirty[0]:
                    persist(str(output_path), merged_records())
                    side_dirty[0] = False
            for future in done:
                name = futures[future]
                results[name] = future.result()
                with side_lock:
                    persist(str(output_path), merged_records())
                    side_dirty[0] = False
    except BaseException:
        # Broadcast before waiting: side loops must not schedule the remainder
        # while a merged write or browser coordinator has already failed.
        stop_all()
        raise
    finally:
        active_error = sys.exc_info()[0] is not None
        if active_error or any(not future.done() for future in futures):
            stop_all()
            wait(futures, timeout=SHUTDOWN_TIMEOUT)
        executor.shutdown(wait=False, cancel_futures=True)
        if active_error:
            # Side checkpoints use their own atomic writer and remain useful
            # even if the merged destination is unavailable. Keep the original
            # error if this last best-effort merged save fails as well.
            with side_lock:
                try:
                    persist(str(output_path), merged_records())
                except BaseException:
                    pass
    all_records = merged_records()
    statuses = {result.status for result in results.values()}
    status = "paused" if "paused" in statuses else "stopped" if "stopped" in statuses else (
        "partial" if "partial" in statuses else "completed"
    )
    gate = next((result.gate_message for result in results.values() if result.gate_message), "")
    browser_payload = {
        name: {"records": result.records, "status": result.status, "total": result.total,
               "gate_message": result.gate_message, "port": result.port,
               "profile_dir": result.profile_dir}
        for name, result in results.items()
    }
    return BatchResult(all_records, status, len(jobs), gate,
                       browser="dual", browser_results=browser_payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="使用已准备好的 Chrome/Edge 并发补抓 JD，每页固定三次滚动")
    parser.add_argument("--scraper-root", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--detail-output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=DEFAULT_JD_WORKERS)
    parser.add_argument("--detail-speed-profile", choices=tuple(_TIMINGS), default="fast")
    parser.add_argument("--browser-mode", choices=("single", "dual"), default=DEFAULT_BROWSER_MODE)
    parser.add_argument("--chrome-port", type=int, default=CDP_PORT)
    parser.add_argument("--edge-port", type=int, default=EDGE_CDP_PORT)
    parser.add_argument("--chrome-profile-dir", type=Path, default=None)
    parser.add_argument("--edge-profile-dir", type=Path, default=None)
    parser.add_argument("--browser-readiness-file", type=Path, default=None)
    args = parser.parse_args(argv)
    control = StopState()
    previous_handlers = {}

    def stop_on_signal(_signum, _frame) -> None:
        control.request_stop()

    try:
        if threading.current_thread() is threading.main_thread():
            for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                signum = getattr(signal, name, None)
                if signum is not None:
                    previous_handlers[signum] = signal.signal(signum, stop_on_signal)
        scraper = load_scraper(args.scraper_root)
        with args.input.open("r", encoding="utf-8-sig") as stream:
            data = json.load(stream)
        readiness = None
        if args.browser_readiness_file is not None:
            with args.browser_readiness_file.open("r", encoding="utf-8-sig") as stream:
                readiness = json.load(stream)
            if not isinstance(readiness, dict) or set(readiness) != set(_BROWSER_NAMES):
                raise ValueError("浏览器预检结果须包含 chrome 和 edge")
            if any(not isinstance(value, dict) or type(value.get("ready")) is not bool for value in readiness.values()):
                raise ValueError("浏览器预检结果须包含布尔 ready 状态")
        configs = default_browser_configs()
        configs["chrome"] = BrowserConfig("chrome", args.chrome_port,
                                          args.chrome_profile_dir or configs["chrome"].profile_dir)
        configs["edge"] = BrowserConfig("edge", args.edge_port,
                                        args.edge_profile_dir or configs["edge"].profile_dir)
        return collect_details(
            data, args.detail_output, scraper=scraper, workers=args.workers,
            speed_profile=args.detail_speed_profile, control=control,
            browser_mode=args.browser_mode, browser_configs=configs, browser_readiness=readiness,
        ).exit_code
    except KeyboardInterrupt:
        control.request_stop()
        return INTERRUPTED_EXIT_CODE
    except Exception as exc:
        print(f"JD 并发失败（{type(exc).__name__}）；检查输入和上游工具后重试。", file=sys.stderr, flush=True)
        return 1
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
