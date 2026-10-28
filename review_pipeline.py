"""Execute exactly one confirmed review step; never discover another run.

The Tk workbench calls this module on a worker thread.  File-only steps stay
local, while the existing JD/Responses processor runs in an owned subprocess
so a paused window cannot leave an uninterruptible network thread behind.
"""

from __future__ import annotations

import copy
import concurrent.futures
import hashlib
import json
import os
import queue
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

import desktop_data
import process_architecture as processor
from desktop_tasks import ReviewTask, STAGES, STAGE_LABELS, TaskStateError, job_id
from review_cache import (REVIEW_CACHE_VERSION, matching_reviews, provider_signature, review_input_signature,
                          review_signature, select_cached_reviews)
from project_resources import DEFAULT_CONFIG, PROJECT_ROOT
import project_llm_settings
import review_rules
from llm_client import SIMPLE_REVIEW_FORMAT, JsonSchemaValidationError, simple_review_payload
from screening_policy import (
    AI_DEFAULT_MODEL, AI_DEFAULT_REASONING_EFFORT, AI_DEFAULT_WORKERS,
    AI_STAGE_PROJECTION, AI_STAGE_RESULT_FIELDS,
    JD_DEFAULT_WORKERS, JD_SCROLL_COUNT, LIST_QUEUE_LABELS, MAX_WORKERS,
)


MAX_LOG_LINE = 1_000_000
CHILD_DRAIN_TIMEOUT = 2.0
EventCallback = Callable[[str, Any], None]


class PipelineError(RuntimeError):
    """An actionable, credential-free execution error."""


class _Interrupted(Exception):
    pass


class StageControl:
    """A nonblocking request object; only the worker owns its child process."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._requested: str | None = None
        self._children: set[subprocess.Popen] = set()

    def pause(self) -> None:
        with self._lock:
            if self._requested != "stopped":
                self._requested = "paused"
            self._event.set()

    def stop(self) -> None:
        with self._lock:
            self._requested = "stopped"
            self._event.set()

    @property
    def requested(self) -> str | None:
        with self._lock:
            return self._requested

    def check(self) -> None:
        if self._event.is_set():
            raise _Interrupted()


def _redact(value: Any) -> Any:
    # Share the result browser's credential handling without importing the GUI.
    return desktop_data._redact(value)


def _reparse(path: Path) -> bool:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False


def _safe_path(task: ReviewTask, path: Path) -> Path:
    """Reject linked checkpoints, including links pointing inside the task."""
    base = task.path.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(base)
        path.resolve().relative_to(base.resolve())
    except (ValueError, OSError, RuntimeError):
        raise PipelineError("任务输出路径越界，已停止以保护原始数据。") from None
    current = base
    if _reparse(current):
        raise PipelineError("任务目录不能是符号链接或目录联接。")
    for part in relative.parts:
        current = current / part
        if _reparse(current):
            raise PipelineError("任务检查点含符号链接或目录联接，未读取或改写。")
    return path


def _guard_tree(task: ReviewTask, directory: Path) -> None:
    directory = _safe_path(task, directory)
    if not directory.exists():
        return
    for parent, dirs, files in os.walk(directory, followlinks=False):
        for name in (*dirs, *files):
            _safe_path(task, Path(parent) / name)


def _read_json(task: ReviewTask, path: Path, default: Any = None) -> Any:
    path = _safe_path(task, path)
    if not path.exists():
        return copy.deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        # A live atomic writer may not yet have published a valid checkpoint.
        return copy.deepcopy(default)


def _write_json(task: ReviewTask, path: Path, value: Any) -> None:
    path = _safe_path(task, path)
    _safe_path(task, path.with_name(path.name + ".tmp"))
    processor.write_json(path, _redact(value))


class _Emitter:
    def __init__(self, task: ReviewTask | None, stage: str, callback: EventCallback | None):
        self.task, self.stage, self.callback = task, stage, callback

    def emit(self, kind: str, value: Any) -> None:
        value = _redact(value)
        if kind == "log":
            value = str(value).rstrip("\r\n") + "\n"
            if self.task:
                try:
                    path = _safe_path(self.task, self.task.path / "task.log")
                    with path.open("a", encoding="utf-8", newline="\n") as handle:
                        handle.write(value)
                except (OSError, PipelineError):
                    # A full disk or an unavailable log must not turn a
                    # committed result into a second, conflicting interrupt.
                    value = "[日志文件暂不可写；任务状态以任务清单为准]\n" + value
        if self.callback:
            try:
                self.callback(kind, value)
            except Exception:
                # UI delivery is best effort; durable task state is not.
                pass

    def log(self, value: str) -> None:
        self.emit("log", value)

    def progress(self, done: int, total: int, stats: dict | None = None, message: str | None = None) -> None:
        if not self.task:
            return
        self.task.record_progress(self.stage, done, total, stats=_redact(stats or {}), message=message)
        self.emit("progress", {"stage": self.stage, "snapshot": self.task.snapshot()})


def _child_options() -> dict[str, Any]:
    options: dict[str, Any] = {}
    if os.name == "nt":
        # A venv Python executable can spawn its actual interpreter before
        # executing any Python code. Suspend at the OS boundary, not in a
        # Python stdin bootstrap, so even that redirector stays in our job.
        options["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                    | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004))
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startup
    else:
        options["start_new_session"] = True
    return options


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Unknown owner: fail closed.
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _WindowsProcessTree:
    """An anonymous Job Object owns our descendants even after a root exits.

    The root starts OS-suspended until assignment, so even a venv redirector
    cannot spawn an unowned child in the Popen/AssignProcessToJobObject interval.
    A successful, drained command can detach its browser on normal completion;
    interruption, failure or GUI process death kills the still-owned tree.
    """

    def __init__(self, child: subprocess.Popen[str]):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel, self._ctypes, self._limits = kernel, ctypes, ExtendedLimits()
        self._child = child
        self._job = kernel.CreateJobObjectW(None, None)
        if not self._job:
            raise PipelineError("无法建立安全的任务进程组；尚未启动采集或模型请求。")
        try:
            self._limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self._set_limits() or not kernel.AssignProcessToJobObject(self._job, wintypes.HANDLE(child._handle)):
                raise PipelineError("无法绑定安全的任务进程组；尚未启动采集或模型请求。")
        except BaseException:
            self.close()
            raise

    def _set_limits(self) -> bool:
        return bool(self._kernel.SetInformationJobObject(
            self._job, 9, self._ctypes.byref(self._limits), self._ctypes.sizeof(self._limits)))

    def resume(self) -> None:
        """Resume only the initial threads of our assigned, suspended root.

        Popen closes CreateProcess's primary-thread handle. Toolhelp supplies
        its ID without undocumented NT calls or a dependency on pywin32.
        """
        from ctypes import wintypes

        ctypes, kernel = self._ctypes, self._kernel

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD)]

        kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for name in ("Thread32First", "Thread32Next"):
            function = getattr(kernel, name)
            function.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
            function.restype = wintypes.BOOL
        kernel.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = (wintypes.HANDLE,)
        kernel.ResumeThread.restype = wintypes.DWORD
        for _ in range(3):
            snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
            if snapshot and snapshot != ctypes.c_void_p(-1).value:
                break
            if ctypes.get_last_error() != 24:  # ERROR_BAD_LENGTH: retry the snapshot only.
                break
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            raise PipelineError("无法恢复安全的任务进程组；尚未启动采集或模型请求。")
        handles = []
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == self._child.pid:
                    handle = kernel.OpenThread(0x0002, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                    if not handle:
                        raise PipelineError("无法恢复安全的任务进程组；尚未启动采集或模型请求。")
                    handles.append(handle)
                entry.dwSize = ctypes.sizeof(entry)
                found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            if not handles or self._child.poll() is not None:
                raise PipelineError("任务进程在安全启动前已退出，未执行请求。")
            for handle in handles:
                if kernel.ResumeThread(handle) == 0xFFFFFFFF:
                    raise PipelineError("无法恢复任务进程；已停止本次自有进程组。")
        finally:
            for handle in handles:
                kernel.CloseHandle(handle)
            kernel.CloseHandle(snapshot)

    def terminate(self) -> None:
        if self._job:
            self._kernel.TerminateJobObject(self._job, 1)

    def close(self, preserve_detached: bool = False) -> None:
        if not self._job:
            return
        if preserve_detached:
            # CDP setup may deliberately leave a dedicated Chrome running.
            # Never assign an existing user browser; only our descendants
            # enter this anonymous job in the first place.
            self._limits.BasicLimitInformation.LimitFlags = 0
            if not self._set_limits():
                self.terminate()
        else:
            # Explicitly terminate before relinquishing ownership, including
            # cancellation from the final progress callback after root exit.
            self.terminate()
        self._kernel.CloseHandle(self._job)
        self._job = None


def _stop_owned_child(child: subprocess.Popen[str], tree: _WindowsProcessTree | None = None) -> None:
    """Stop only the Popen object launched by this worker, never an old PID."""
    if tree:
        # The handle remains valid when the original processor has exited.
        tree.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        return
    if child.poll() is not None:
        if os.name != "nt":
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
        return
    if os.name == "nt":
        # Kill the owned tree while its root is still alive. Sending a break
        # to Python first can orphan boss_cdp_raw.py before /T sees it.
        try:
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=6,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except (OSError, ValueError):
            child.terminate()
    try:
        child.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name != "nt":
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        child.kill()
        child.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _check_child_lease(task: ReviewTask, stage: str) -> None:
    root = _safe_path(task, task.path / "runtime")
    # Inspect former rounds as well as the current pointer, without creating
    # directories. Switching rounds must never hide a surviving child lease.
    directories = [root / stage, *sorted(root.glob(f"{stage}-redo-*"))]
    for directory in directories:
        old = _read_json(task, directory / "child_lease.json", {})
        if isinstance(old, dict) and old.get("active") and type(old.get("pid")) is int and _pid_alive(old["pid"]):
            raise PipelineError("本任务的旧子进程仍在运行；请等待其结束后再续跑，未重复启动。")


def _run_child(task: ReviewTask | None, stage: str, command: list[str], control: StageControl,
               emit: _Emitter, monitor: Callable[[], None] | None = None) -> int:
    runtime = task.run_directory(stage) if task else None
    lease_path = runtime / "child_lease.json" if runtime else None
    if task and lease_path:
        _check_child_lease(task, stage)
    control.check()
    environment = processor.subprocess_env_with_local_bypass()
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    child = subprocess.Popen(command, cwd=PROJECT_ROOT, env=environment,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", bufsize=1, **_child_options())
    with control._lock:
        control._children.add(child)
    output: queue.Queue[str | None] = queue.Queue(maxsize=512)
    reader_stop = threading.Event()

    def deliver(value: str | None) -> None:
        while not reader_stop.is_set():
            try:
                output.put(value, timeout=0.1)
                return
            except queue.Full:
                continue

    def read_output() -> None:
        assert child.stdout is not None
        parts: list[str] = []
        size = 0
        overflow = False
        try:
            while not reader_stop.is_set():
                char = child.stdout.read(1)
                if not char:
                    break
                if char in "\r\n":
                    if parts or overflow:
                        line = "[日志行过长，已隐藏整行以保护敏感信息]" if overflow else "".join(parts)
                        deliver(str(_redact(line)))
                    parts, size, overflow = [], 0, False
                elif not overflow:
                    size += 1
                    if size > MAX_LOG_LINE:
                        parts, overflow = [], True
                    else:
                        parts.append(char)
            if parts or overflow:
                deliver("[日志行过长，已隐藏整行以保护敏感信息]" if overflow else str(_redact("".join(parts))))
        finally:
            child.stdout.close()
            deliver(None)

    reader = threading.Thread(target=read_output, name="review-step-output", daemon=True)
    eof = False
    last_poll = 0.0
    last_output = time.monotonic()
    exited_at: float | None = None
    drain_started_at: float | None = None
    tree: _WindowsProcessTree | None = None
    preserve_detached = False
    drain_failed = False
    try:
        if os.name == "nt":
            tree = _WindowsProcessTree(child)
        if task and lease_path:
            _write_json(task, lease_path, {"pid": child.pid, "active": True, "started_at": processor.now_iso()})
        reader.start()
        if tree:
            control.check()
            tree.resume()
        while not eof or child.poll() is None:
            if control.requested:
                _stop_owned_child(child, tree)
            try:
                line = output.get(timeout=0.1)
                if line is None:
                    eof = True
                else:
                    last_output = time.monotonic()
                    emit.log(line)
                    if any(term in line for term in ("[暂停]", "需要人工验证", "触发安全验证", "JD 批次已暂停", "请在专用 Chrome")):
                        emit.emit("waiting", "需要在专用浏览器中人工登录或完成验证；本步停止后可点击继续。")
            except queue.Empty:
                pass
            now = time.monotonic()
            if child.poll() is not None and not eof:
                exited_at = exited_at or now
                if not drain_failed and (now - max(exited_at, last_output) >= CHILD_DRAIN_TIMEOUT
                                         or now - exited_at >= 10):
                    # An inherited pipe must not leave Stop/Close stuck forever
                    # after the processor root has already exited.
                    drain_failed = True
                    drain_started_at = now
                    emit.log("处理器已退出，但本次后代仍占用输出；已结束自有进程组并保留检查点，请重试本步。")
                    _stop_owned_child(child, tree)
                elif drain_failed and drain_started_at and now - drain_started_at >= CHILD_DRAIN_TIMEOUT:
                    # A detached process outside our job may have inherited a
                    # pipe. Do not wait indefinitely or target an unknown PID.
                    break
            if monitor and time.monotonic() - last_poll >= 0.35:
                monitor()
                last_poll = time.monotonic()
        code = child.wait()
        if drain_failed and not code:
            code = 1
        if monitor:
            monitor()
        # A final progress callback can request Stop/Close. Decide whether to
        # retain a deliberately detached browser only after that callback.
        preserve_detached = code == 0 and not control.requested
        return code
    except BaseException:
        preserve_detached = False
        _stop_owned_child(child, tree)
        raise
    finally:
        if tree:
            # Linearize cancellation against the normal ownership hand-off:
            # a request already made must never disable kill-on-close.
            with control._lock:
                tree.close(preserve_detached=preserve_detached and control._requested is None)
        elif control.requested:
            _stop_owned_child(child)
        if child.stdin and not child.stdin.closed:
            try:
                child.stdin.close()
            except OSError:
                pass
        reader_stop.set()
        if reader.ident is not None:
            reader.join(timeout=1)
        elif child.stdout:
            child.stdout.close()
        # The in-memory ownership check is authoritative even if persisting
        # child_lease.json fails. Never release the run lease for a live child.
        if child.poll() is not None:
            with control._lock:
                control._children.discard(child)
        if task and lease_path:
            _write_json(task, lease_path, {"pid": child.pid, "active": child.poll() is None,
                                          "finished_at": processor.now_iso()})


def prepare_browser(scraper_root: Path, on_event: EventCallback | None = None,
                    control: StageControl | None = None, browser_mode: str = "single",
                    chrome_port: int = 9222, edge_port: int = 9223,
                    chrome_profile_dir: Path | str | None = None,
                    edge_profile_dir: Path | str | None = None,
                    chrome_profile: Path | str | None = None,
                    edge_profile: Path | str | None = None) -> bool | dict[str, str]:
    """Only invoked by an explicit Prepare browser/Login action."""
    scraper_root = Path(scraper_root).expanduser().resolve()
    entry = scraper_root / "scripts" / "boss_cdp_raw.py"
    if not entry.is_file():
        raise PipelineError("上游目录缺少 scripts/boss_cdp_raw.py，请先选择正确的采集器目录。")
    python = processor.resolve_python(scraper_root)
    if browser_mode not in {"single", "dual"}:
        raise ValueError("browser_mode 必须为 single 或 dual")
    chrome_profile_dir = chrome_profile_dir or chrome_profile
    edge_profile_dir = edge_profile_dir or edge_profile
    emit = _Emitter(None, "browser", on_event)
    parent_control = control or StageControl()
    configs = {
        "chrome": (chrome_port, chrome_profile_dir),
        "edge": (edge_port, edge_profile_dir),
    }

    def prepare_one(name: str) -> tuple[str, int]:
        port, profile = configs[name]
        label = "Chrome" if name == "chrome" else "Edge"
        emit.log(f"正在准备独立 {label}（CDP {port}）；请在该窗口人工登录，浏览器登录态彼此隔离。")
        command = [str(python), "-u", str(PROJECT_ROOT / "browser_setup.py"),
                   "--browser", name, "--cdp-port", str(port)]
        if profile:
            command.extend(("--profile-dir", str(profile)))
        result = _run_child(None, "browser", command, parent_control, emit)
        emit.log(f"{label} 窗口与 CDP 已准备，请在该窗口确认 BOSS 登录后运行本步。" if result == 0
                 else f"{label} 窗口或 CDP 准备失败，请查看上方具体错误；尚未开始采集。")
        return name, result

    if browser_mode == "single":
        _name, result = prepare_one("chrome")
        return result == 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="browser-setup") as pool:
        futures = [pool.submit(prepare_one, name) for name in ("chrome", "edge")]
        results = dict(future.result() for future in futures)
    return {name: ("completed" if code == 0 else "failed") for name, code in results.items()}


def _base_metadata(task: ReviewTask) -> dict[str, Any]:
    manifest = task.manifest
    return {"task_name": manifest.get("name"), "source_root": manifest.get("source_root"),
            "city": manifest.get("city"), "tracks": manifest.get("track"),
            "sample_size": manifest.get("sample_size"), "generated_at": processor.now_iso(),
            "screening_version": processor.SCREENING_VERSION}


def _with_flags(job: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(job)
    parsed = processor.parse_salary(result.get("salary"))
    flags: list[str] = []
    lower, upper = parsed.get("lower_k"), parsed.get("upper_k")
    if lower is not None and lower < 17:
        flags.append("薪资低于目标")
    if lower is not None and upper is not None and lower < 17 <= upper:
        flags.append("需核实固定薪资")
    if lower is None:
        flags.append("薪资待核实")
    result.update(salary_parsed=parsed, salary_status=processor.salary_status(parsed), salary_flags=flags)
    labels = processor.extract_experience_labels(result)
    result["experience_labels"] = labels
    high_experience = False
    for label in labels:
        minimum = re.search(r"(\d+)\s*(?:[-_–至~～]\s*\d+\s*年?|年(?:以上|起))", label)
        if label in {"105", "106"} or (minimum and int(minimum.group(1)) >= 3):
            high_experience = True
    jd_minimum = result.get("jd_minimum_years")
    high_experience |= type(jd_minimum) in {int, float} and jd_minimum >= 3
    result["experience_gap"] = "经验要求偏高，用项目案例争取" if high_experience else ""
    return result


def _index(task: ReviewTask, runtime: Path, jobs: list[dict[str, Any]], *,
           metadata: dict[str, Any] | None = None) -> None:
    _write_json(task, runtime / "prepared" / "merged_jobs_index.json",
                {"metadata": {**_base_metadata(task), **(metadata or {}), "unique_job_count": len(jobs)}, "jobs": jobs})


def _run_import(task: ReviewTask, runtime: Path, control: StageControl, emit: _Emitter) -> tuple[list[dict], dict]:
    manifest = task.manifest
    source = Path(manifest["source_root"])
    config = processor.load_config(Path(manifest.get("config_path") or DEFAULT_CONFIG))
    emit.log("读取固定数据源，合并来源并按稳定检索组合顺序均衡抽取去重岗位；不读取其他任务。")
    jobs, metadata = processor.collect_rows(source, config, city=manifest.get("city"),
                                            sample_size=manifest.get("sample_size"),
                                            sample_strategy="balanced-dedup", tracks=manifest.get("track", "all"))
    control.check()
    normalized = []
    for job in jobs:
        item = copy.deepcopy(job)
        item["job_id"] = job_id(item)
        item["recommended_keep"] = True
        normalized.append(_with_flags(item))
    _index(task, runtime, normalized)
    _write_json(task, task.path / "raw_sample.json", {"metadata": metadata, "jobs": normalized})
    stats = {**metadata, "raw_record_count": metadata.get("raw_record_count", 0),
             "deduplicated_job_count": metadata.get("unique_job_count", len(normalized)),
             "selected_job_count": len(normalized), "sampled_job_count": len(normalized),
             "source_combinations": sorted({processor.text(src.get("combination"))
                for job in normalized for src in job.get("observed_in", []) if isinstance(src, dict)}),
             "json_path": str(runtime / "prepared" / "merged_jobs_index.json")}
    emit.log(f"导入完成：范围内原始 {stats['raw_record_count']} 条，去重总量 {stats['deduplicated_job_count']} 岗，本任务 {len(normalized)} 岗。")
    return normalized, stats


def _run_shortlist(task: ReviewTask, runtime: Path, control: StageControl, emit: _Emitter) -> tuple[list[dict], dict]:
    inputs = task.input_jobs("shortlist")
    results = []
    for number, job in enumerate(inputs, 1):
        control.check()
        item = _with_flags({**job, **processor.score_job(job)})
        item["recommended_keep"] = bool(item["list_pass"])
        results.append(item)
        if number % 25 == 0:
            emit.progress(number, len(inputs), {"processed_count": number})
    queue_counts = Counter(job["list_queue"] for job in results)
    counts = {key: queue_counts.get(key, 0) for key in LIST_QUEUE_LABELS}
    queue_metadata = {"screening_version": processor.SCREENING_VERSION, "list_queue_counts": counts}
    _index(task, runtime, results, metadata=queue_metadata)
    _write_json(task, runtime / "prepared" / "jd_shortlists" / "jd_input.json",
                {**queue_metadata, "total": sum(j.get("recommended_keep") is True for j in results),
                 "jobs": [j for j in results if j["recommended_keep"]]})
    for key, filename in (("relevant", "jd_relevant.json"), ("verify", "jd_verify.json")):
        queued = [job for job in results if job["list_queue"] == key]
        _write_json(task, runtime / "prepared" / "jd_shortlists" / filename,
                    {**queue_metadata, "list_queue": key, "total": len(queued), "jobs": queued})
    stats = {"total_jobs": len(results), "processed_count": len(results),
             "recommended_count": sum(j.get("recommended_keep") is True for j in results),
             **queue_metadata}
    emit.log(f"列表宽筛完成：{len(results)} 岗；明确相关 {counts['relevant']}、待核实 {counts['verify']}、剔除 {counts['excluded']}。"
             f"默认建议保留前两类 {stats['recommended_count']} 岗；可显式仅保留明确相关或加入待核实，人工确认之前不会补抓 JD。")
    return results, stats


def _detail_completeness(detail: dict[str, Any] | None) -> str:
    return processor.detail_completeness(detail)


def _analyze(job: dict[str, Any], review: dict | None = None) -> dict[str, Any]:
    if (job.get("review_format") == SIMPLE_REVIEW_FORMAT
            or isinstance(review, dict) and review.get("review_format") == SIMPLE_REVIEW_FORMAT):
        item = _analyze_ai_stage(job, review)
        item["classification"] = "暂不投" if item.get("ai_judgment") == "不符合" else "海投池"
        if item.get("ai_reason"):
            item["reason"] = item["ai_reason"]
            item["reasons"] = [item["ai_reason"]]
        item.pop("llm_stage_projection", None)
        item.pop("llm_stage_decision", None)
        return item
    completeness = job.get("jd_completeness") or ("complete" if processor.text(job.get("jd")).strip() else "missing")
    semantic_review = copy.deepcopy(review) if completeness == "complete" and review else None
    # Keep model experience suggestions as evidence, not as a hard threshold.
    # Salary, experience labels and JD year requirements are parsed in code.
    if semantic_review and isinstance(semantic_review.get("experience_assessment"), dict):
        semantic_review["experience_assessment"]["hard_conflict"] = False
    item = processor.merge_model_review(job, semantic_review)
    if semantic_review and review:
        item["llm_review"] = copy.deepcopy(review)
    verification = list(item.get("verification") or [])
    if review and isinstance(review.get("experience_assessment"), dict) and review["experience_assessment"].get("hard_conflict") is True:
        verification.append("模型提示经验可能冲突，仅供核实；最终经验门槛仍按代码解析的列表标签和JD年限判断")
    if completeness != "complete":
        item["jd_status"] = "部分可用" if completeness == "partial" else "待核实"
        verification.append("JD待核实：未获得完整正文，不把部分文本当作完整职责匹配")
        # merge_model_review already recomputes the list gate and classifies
        # saved body evidence. Manual retention grants another review step,
        # not eligibility: no JD requires list_pass, and partial JD may still
        # fail the responsibility threshold. Do not reopen either decision.
        item["model_status"] = "fallback_incomplete_jd"
    item["jd_completeness"] = completeness
    item["jd_analysis_status"] = "reviewed" if semantic_review and semantic_review.get("status") == "success" else "rule_fallback"
    item["verification"] = item["verify"] = list(dict.fromkeys(verification))
    item["recommended_keep"] = item.get("classification") != "暂不投"
    item.pop("llm_stage_projection", None)
    item.pop("llm_stage_decision", None)
    return _with_flags(item)


def _analyze_ai_stage(job: dict[str, Any], review: dict | None = None) -> dict[str, Any]:
    """Project the two-field judgment, never reclassify it with code rules."""
    item = copy.deepcopy(job)
    for field in AI_STAGE_RESULT_FIELDS:
        item.pop(field, None)
    completeness = job.get("jd_completeness") or ("complete" if processor.text(job.get("jd")).strip() else "missing")
    complete = completeness == "complete" and bool(processor.text(job.get("jd")).strip())
    raw = copy.deepcopy(review) if isinstance(review, dict) else None
    identity_matches = raw is not None and str(raw.get("job_id", "")) == job_id(job)
    successful = bool(complete and identity_matches and raw.get("status") == "success")
    payload = None
    if successful and raw.get("review_format") == SIMPLE_REVIEW_FORMAT:
        try:
            payload = simple_review_payload(raw)
        except JsonSchemaValidationError:
            pass
    verified_model = payload is not None
    assessment = payload["判断"] if verified_model else None
    verification = []
    if verified_model:
        item.update(ai_judgment=assessment, ai_reason=payload["理由"])
        item["model_status"] = "success"
        item["jd_analysis_status"] = "reviewed"
    else:
        item["model_status"] = ("not_run_incomplete_jd" if not complete else
                                "failed" if raw and raw.get("status") == "failed" and identity_matches else
                                "invalid_result" if raw else "not_run")
        item["jd_analysis_status"] = "pending_verification"
        item["model_error"] = raw.get("error") if raw and raw.get("error") else None
    if verified_model and assessment == "符合":
        decision, keep = "related", True
        reason = "AI判断符合，建议保留"
    elif verified_model and assessment == "不符合":
        decision, keep, reason = "unrelated", False, "AI判断不符合，建议剔除"
    else:
        decision, keep = "pending_verification", None
        reason = ("AI判断信息不足，待人工核实；当前不进入保留或剔除名单" if verified_model else
                  "JD未完整，未完成AI判断；待人工核实，当前不进入保留或剔除名单" if not complete else
                  "AI请求失败，未完成AI判断；待人工核实，当前不进入保留或剔除名单" if item["model_status"] == "failed" else
                  "未获得有效AI判断；待人工核实，当前不进入保留或剔除名单")
        verification.append(reason)
    item.update(llm_stage_projection=AI_STAGE_PROJECTION, llm_stage_decision=decision,
                jd_completeness=completeness, recommended_keep=keep, reason=reason, reasons=[reason],
                llm_review=raw or {"status": "not_run", "review_format": SIMPLE_REVIEW_FORMAT},
                review_format=SIMPLE_REVIEW_FORMAT, analysis_pending=decision == "pending_verification",
                verification=verification, verify=verification)
    return _with_flags(item)


def _detail_checkpoint(task: ReviewTask, runtime: Path, inputs: list[dict]) -> tuple[dict[str, dict], dict]:
    folder = runtime / "prepared" / "jd_shortlists"
    _guard_tree(task, folder)
    details: dict[str, dict] = {}
    paths = [folder / "details.json", *sorted((folder / "detail_batches").glob("details_*.json"))]
    for path in paths:
        payload = _read_json(task, path, [])
        records = payload if isinstance(payload, list) else next((payload[k] for k in ("jobs", "details", "records") if isinstance(payload.get(k), list)), []) if isinstance(payload, dict) else []
        processor.merge_detail_records(details, (r for r in records if isinstance(r, dict)))
    relevant: dict[str, dict] = {}
    for job in inputs:
        detail = processor.find_detail(job, details)
        if detail:
            relevant[job_id(job)] = detail
    manifest = _read_json(task, folder / "details_manifest.json", {"records": []})
    return relevant, manifest if isinstance(manifest, dict) else {"records": []}


def _jd_stats(inputs: list[dict], details: dict[str, dict], manifest: dict) -> dict[str, int]:
    success = partial = failed = paused = paused_partial = paused_complete = 0
    failures, waits = set(), set()
    for record in manifest.get("records", []):
        if not isinstance(record, dict):
            continue
        affected = ((record.get("job_ids") or record.get("missing_job_ids")) if record.get("security_or_login_gate") is True else
                    record.get("missing_job_ids") or record.get("job_ids"))
        keys = set(map(str, affected or []))
        if record.get("status") == "paused":
            waits.update(keys)
        elif record.get("status") in ("failed", "partial"):
            failures.update(keys)
    for job in inputs:
        identity = job_id(job)
        completeness = _detail_completeness(details.get(identity))
        if completeness == "complete":
            success += 1
            if identity in waits:
                paused += 1
                paused_complete += 1
        elif completeness == "partial":
            partial += 1
            if identity in waits:
                # Body quality and a live verification gate are independent.
                # A truncated body must not hide a pause affecting this job.
                paused += 1
                paused_partial += 1
        elif identity in waits:
            paused += 1
        elif identity in failures:
            failed += 1
    return {"total_jobs": len(inputs), "jd_success_count": success, "jd_partial_count": partial,
            "jd_failed_count": failed, "jd_paused_count": paused,
            "jd_paused_partial_count": paused_partial,
            "jd_paused_complete_count": paused_complete,
            "jd_pending_count": len(inputs) - success - partial - failed - paused + paused_partial + paused_complete}


def _process_command(task: ReviewTask, runtime: Path, stage: str, workers: int, batch_size: int,
                     scraper_root: Path | None, jd_workers: int = JD_DEFAULT_WORKERS,
                     browser_mode: str = "single", chrome_port: int = 9222,
                     edge_port: int = 9223, chrome_profile_dir: Path | str | None = None,
                     edge_profile_dir: Path | str | None = None) -> list[str]:
    manifest = task.manifest
    command = [sys.executable, "-u", str(PROJECT_ROOT / "process_architecture.py"),
               "--root", manifest["source_root"], "--config", str(manifest.get("config_path") or DEFAULT_CONFIG),
               "--output-root", str(runtime), "--stage", stage, "--workers", str(workers),
               "--batch-size", str(batch_size), "--jd-workers", str(jd_workers),
               "--browser-mode", browser_mode, "--chrome-port", str(chrome_port),
               "--edge-port", str(edge_port),
               "--no-wait-on-gate"]
    if stage == "llm":
        model, reasoning_effort = _project_model_options()
        command.extend(("--model", model, "--reasoning-effort", reasoning_effort,
                        "--api-mode", _project_api_mode()))
    if scraper_root:
        command.extend(("--scraper-root", str(scraper_root)))
    if chrome_profile_dir:
        command.extend(("--chrome-profile-dir", str(chrome_profile_dir)))
    if edge_profile_dir:
        command.extend(("--edge-profile-dir", str(edge_profile_dir)))
    return command


def _run_jd(task: ReviewTask, runtime: Path, scraper_root: Path | None, workers: int,
            batch_size: int, control: StageControl, emit: _Emitter,
            jd_workers: int = JD_DEFAULT_WORKERS, browser_mode: str = "single",
            chrome_port: int = 9222, edge_port: int = 9223,
            chrome_profile_dir: Path | str | None = None,
            edge_profile_dir: Path | str | None = None) -> tuple[list[dict], dict, str, list[str]]:
    inputs = task.input_jobs("fetch-jd")
    _index(task, runtime, inputs)
    folder = runtime / "prepared" / "jd_shortlists"
    _write_json(task, folder / "jd_input.json", {"total": len(inputs), "jobs": inputs})
    if not inputs:
        _write_json(task, folder / "details.json", [])
        _write_json(task, folder / "details_manifest.json", {"total_jobs": 0, "records": [], "status": "completed"})
        return [], {**_jd_stats([], {}, {}), "jd_workers": jd_workers, "scroll_count": JD_SCROLL_COUNT,
                    "browser_mode": browser_mode,
                    "browser_results": {"chrome": "completed"} if browser_mode == "single" else
                    {"chrome": "completed", "edge": "completed"}}, "completed", []
    if not scraper_root or not (Path(scraper_root) / "scripts" / "boss_cdp_raw.py").is_file():
        raise PipelineError("JD 补抓需要正确的上游采集器目录（scripts/boss_cdp_raw.py）。")

    def monitor() -> None:
        details, manifest = _detail_checkpoint(task, runtime, inputs)
        stats = _jd_stats(inputs, details, manifest)
        stats.update(jd_workers=jd_workers, scroll_count=JD_SCROLL_COUNT, browser_mode=browser_mode)
        emit.progress(stats["jd_success_count"] + stats["jd_partial_count"], len(inputs), stats)

    emit.log(f"JD 仅补抓正文：页面并发 {jd_workers}，每页滚动 {JD_SCROLL_COUNT} 次，浏览器模式 {browser_mode}；本步不进行关键词或职责筛选。")
    code = _run_child(task, "fetch-jd", _process_command(
        task, runtime, "fetch-jd", workers, batch_size, scraper_root, jd_workers,
        browser_mode, chrome_port, edge_port, chrome_profile_dir, edge_profile_dir,
    ), control, emit, monitor)
    details, manifest = _detail_checkpoint(task, runtime, inputs)
    stats = _jd_stats(inputs, details, manifest)
    # Prefer the latest per-batch checkpoint: readiness only says that a CDP
    # endpoint was available before work started and must not hide a later
    # login/security pause reported by a browser side.
    browser_results = None
    for entry in reversed(manifest.get("records", []) if isinstance(manifest, dict) else []):
        if isinstance(entry, dict) and isinstance(entry.get("browser_results"), dict):
            browser_results = entry["browser_results"]
            break
    if not isinstance(browser_results, dict):
        browser_results = manifest.get("browser_readiness") if isinstance(manifest, dict) else None
        if isinstance(browser_results, dict):
            browser_results = {
                name: ("completed" if value.get("ready") is True else "failed")
                if isinstance(value, dict) else str(value)
                for name, value in browser_results.items()
            }
    if not isinstance(browser_results, dict):
        browser_results = {"chrome": "completed"} if browser_mode == "single" else {
            "chrome": "completed", "edge": "completed",
        }
    stats.update(jd_workers=jd_workers, scroll_count=JD_SCROLL_COUNT,
                 browser_mode=browser_mode, browser_results=browser_results)
    # A previous, wider selection may have left unrelated paused batches.
    # Only a gate affecting this step's confirmed jobs can pause this step.
    gate = stats["jd_paused_count"] > 0
    input_ids = {job_id(job) for job in inputs}
    fatal_batches = [record for record in manifest.get("records", []) if isinstance(record, dict)
                     and record.get("status") == "failed" and record.get("exit_code") not in (None, 0)
                     and input_ids.intersection(map(str, record.get("job_ids") or record.get("missing_job_ids") or []))]
    status = control.requested or ("waiting" if gate else "failed" if code or fatal_batches else "completed")
    issues = ["请在专用浏览器完成人工登录或安全验证，然后继续本步。"] if gate else []
    if code and not control.requested:
        issues.append(f"JD 子进程退出码 {code}；已有明细和失败岗位均保留，可重试本步。")
    elif fatal_batches and not control.requested:
        issues.append("JD 采集批次执行失败；已有检查点保留，请检查已脱敏日志并重试本步。")
    results = []
    for job in inputs:
        detail = details.get(job_id(job))
        item = {**job, "jd": processor.text((detail or {}).get("jd")),
                "jd_completeness": _detail_completeness(detail)}
        # An upstream record may have inherited results from an older run. This
        # step owns body acquisition only; semantic conclusions belong to AI or
        # report generation and must not silently select this step's inputs.
        for field in (*AI_STAGE_RESULT_FIELDS, "jd_fetch_issue", "jd_record"):
            item.pop(field, None)
        if detail:
            item["jd_record"] = detail
        completeness = item["jd_completeness"]
        item.update(jd_status="已抓取" if completeness == "complete" else "部分可用" if completeness == "partial" else "待核实",
                    jd_fetched=bool(item["jd"].strip()), jd_analysis_status="not_run", recommended_keep=True)
        if completeness == "missing":
            item["jd_fetch_issue"] = issues[0] if issues else "本次未获得 JD；保留待核实，可重试本步。"
        elif completeness == "partial":
            item["jd_fetch_issue"] = "JD 部分可用，需重试获取完整正文；本步未据此筛选。"
        results.append(_with_flags(item))
    _index(task, runtime, results)
    _write_json(task, folder / "details.json", list(details.values()))
    emit.log(f"JD 状态：完整 {stats['jd_success_count']}，部分 {stats['jd_partial_count']}，失败 {stats['jd_failed_count']}，等待 {stats['jd_paused_count']}。")
    return results, stats, status, issues


def _review_hash(job: dict, *, prompt: str | None = None, provider: str | None = None) -> str | None:
    """Compatibility helper; callers with many jobs share one prompt snapshot."""
    provider = provider if provider is not None else _provider_hash()
    if provider is None:
        return None
    return review_signature(processor.model_input(job), processor.load_prompt() if prompt is None else prompt, provider)


def _project_model_options() -> tuple[str, str]:
    """Return the project-configured model and effort, without reading secrets."""
    settings = project_llm_settings.load_settings(project_llm_settings.default_settings_path()) or {}
    return (
        settings.get("model", AI_DEFAULT_MODEL),
        settings.get("reasoning_effort", AI_DEFAULT_REASONING_EFFORT),
    )


def _project_api_mode() -> str:
    """Return the project protocol without reading or exposing its secret."""
    settings = project_llm_settings.load_settings(project_llm_settings.default_settings_path()) or {}
    return settings.get("api_mode", "responses")


def _provider_hash() -> str | None:
    try:
        from llm_client import load_codex_config
        model, reasoning_effort = _project_model_options()
        config = load_codex_config(model=model, reasoning_effort=reasoning_effort,
                                   api_mode=_project_api_mode(),
                                   project_settings_path=project_llm_settings.default_settings_path())
        return provider_signature(config)
    except Exception:
        return None


def _save_review_prompt(task: ReviewTask, runtime: Path, prompt: str) -> Path:
    """Keep immutable task-local prompt snapshots alongside old results."""
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    path = _safe_path(task, runtime / "review_prompts" / f"{digest}.md")
    if path.exists():
        if path.read_text(encoding="utf-8") != prompt:
            raise PipelineError("已保存的 AI 复核规则快照损坏，请检查任务目录。")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _safe_path(task, path.with_name(path.name + ".tmp"))
    temporary.write_text(prompt, encoding="utf-8", newline="\n")
    os.replace(temporary, path)
    return path


def _task_report_prompt(task: ReviewTask) -> str | None:
    """Validate reports with the exact AI-stage snapshot, without auth I/O."""
    manifest = task.manifest
    reference = manifest["stages"]["llm"]["stats"].get("review_prompt_file")
    if reference is not None:
        runtime = manifest["stages"]["llm"].get("runtime_directory", "runtime/llm")
        pattern = re.escape(runtime) + r"/review_prompts/[a-f0-9]{64}\.md"
        if not isinstance(reference, str) or not re.fullmatch(pattern, reference):
            return None
        path = _safe_path(task, task.path / reference)
        try:
            prompt = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        return prompt if hashlib.sha256(prompt.encode("utf-8")).hexdigest() == path.stem else None
    # Older tasks predate snapshots. Keep their default-path compatibility,
    # while never validating a task override against global default rules.
    prompt = processor.report_prompt()
    override = manifest.get("review_rules")
    if prompt is not None and override:
        try:
            return review_rules.compose_review_prompt(override["text"], default_prompt=prompt)
        except ValueError:
            return None
    return prompt


def _run_reviews(task: ReviewTask, runtime: Path, workers: int, batch_size: int,
                 control: StageControl, emit: _Emitter) -> tuple[list[dict], dict, str, list[str]]:
    inputs = task.input_jobs("llm")
    eligible = [j for j in inputs if j.get("jd_completeness") == "complete" and processor.text(j.get("jd")).strip()]
    _index(task, runtime, inputs)
    folder = runtime / "prepared" / "jd_shortlists"
    _write_json(task, folder / "jd_input.json", {"total": len(eligible), "jobs": eligible})
    _write_json(task, folder / "details.json", [{"job_id": job_id(j), "jd": j["jd"]} for j in eligible])
    review_path, manifest_path = runtime / "prepared" / "llm_reviews.json", runtime / "prepared" / "llm_manifest.json"
    jsonl_path = runtime / "prepared" / "llm_reviews.jsonl"
    context_path = runtime / "review_cache_context.json"
    provider = _provider_hash() if eligible else None
    prompt_path = None
    try:
        prompt = processor.load_prompt() if provider is not None else None
        override = task.manifest.get("review_rules")
        if prompt is not None and override:
            prompt = review_rules.compose_review_prompt(override["text"], default_prompt=prompt)
        if prompt is not None:
            prompt = prompt.replace("\r\n", "\n").replace("\r", "\n")
            prompt_path = _save_review_prompt(task, runtime, prompt)
    except (OSError, UnicodeError, ValueError):
        provider, prompt = None, None
    hashes = {job_id(j): _review_hash(j, prompt=prompt, provider=provider) if provider is not None else None for j in eligible}
    input_hashes = {job_id(j): review_input_signature(processor.model_input(j), prompt)
                    if prompt is not None else None for j in eligible}
    old_payload = _read_json(task, review_path, {})
    selection = select_cached_reviews(old_payload, hashes, input_signatures=input_hashes)
    initial_hits = set(selection.reviews)
    request_count = len(eligible) - selection.hit_count if provider is not None else 0
    counts = {"cache_version": REVIEW_CACHE_VERSION, "cache_hit_count": selection.hit_count,
              "cache_miss_count": len(eligible) - selection.hit_count,
              "cache_invalidated_count": selection.invalidated_count, "cache_legacy_count": selection.legacy_count,
              "cache_excluded_count": selection.excluded_count, "request_count": request_count}

    def write_reviews(value: Any) -> None:
        # Check JSONL's temporary path as well as its final path before either
        # artifact is written.  Both files expose exactly the same active rows.
        _safe_path(task, jsonl_path.with_name(jsonl_path.name + ".tmp"))
        _safe_path(task, jsonl_path)
        _write_json(task, review_path, value.document(processor.now_iso()))
        processor.write_jsonl(jsonl_path, _redact(list(value.reviews.values())))

    write_reviews(selection)
    # Retain the sidecar for diagnostics/older readers, never as cache proof.
    _write_json(task, context_path, {"cache_version": REVIEW_CACHE_VERSION, "inputs": hashes,
                                      "input_signatures": input_hashes, "provider": provider})

    def review_map() -> dict[str, dict]:
        value = _read_json(task, review_path, {})
        return matching_reviews(value, hashes, input_signatures=input_hashes)

    def statistics(reviews: dict[str, dict]) -> dict[str, Any]:
        successes = 0
        for row in reviews.values():
            if row.get("status") == "success" and row.get("review_format") == SIMPLE_REVIEW_FORMAT:
                try:
                    simple_review_payload(row)
                except JsonSchemaValidationError:
                    continue
                successes += 1
        failures = sum(r.get("status") == "failed" for r in reviews.values())
        return {"total_jobs": len(inputs), "llm_candidate_count": len(eligible), "llm_success_count": successes,
                "review_format": SIMPLE_REVIEW_FORMAT,
                "llm_failed_count": failures, "llm_fallback_count": len(inputs) - len(eligible) + failures,
                "llm_pending_count": max(0, len(eligible) - successes - failures), "workers": workers,
                "llm_cache_hit_count": counts["cache_hit_count"], "llm_cache_miss_count": counts["cache_miss_count"],
                "llm_cache_invalidated_count": counts["cache_invalidated_count"],
                "llm_cache_legacy_count": counts["cache_legacy_count"], "llm_request_count": counts["request_count"],
                "review_prompt_file": prompt_path.relative_to(task.path).as_posix() if prompt_path is not None else None}

    def monitor() -> None:
        stats = statistics(review_map())
        emit.progress(stats["llm_success_count"] + stats["llm_failed_count"] + len(inputs) - len(eligible), len(inputs), stats)

    initial_manifest = {"candidate_count": len(eligible), "completed_count": selection.hit_count,
                        "pending_count": len(eligible) - selection.hit_count, "failed_count": 0,
                        "fallback_count": 0, "workers": workers, **counts}
    # Do not let a stale final manifest describe a newly started/interrupted
    # invocation.  The child will replace this with its own checkpoints.
    _write_json(task, manifest_path, {**initial_manifest, "status": "running"})
    if eligible and provider is None:
        code = 0
        _write_json(task, manifest_path, {**initial_manifest, "status": "configuration_failed",
                                          "fallback_count": len(eligible)})
    elif eligible and selection.hit_count == len(eligible):
        code = 0
        _write_json(task, manifest_path, {**initial_manifest, "status": "completed"})
    elif eligible:
        command = _process_command(task, runtime, "llm", workers, batch_size, None)
        command.extend(("--prompt-file", str(prompt_path), "--conversation-size", "1"))
        emit.log(f"AI 使用 {workers} 个并发请求，每个岗位独立复核；不跨岗位续接会话或重放历史。"
                 "每岗单独校验并保存检查点，已有有效成功结果继续复用。")
        code = _run_child(task, "llm", command, control, emit, monitor)
    else:
        code = 0
        _write_json(task, manifest_path, {**initial_manifest, "status": "skipped_no_candidates",
                                          "fallback_count": len(inputs)})
    # A child uses the same per-record contract.  Reject unsigned/changed or
    # unrelated results even if a sidecar was advanced before it exited.
    normalized = select_cached_reviews(_read_json(task, review_path, {}), hashes, retry_failures=False,
                                       input_signatures=input_hashes)
    write_reviews(normalized)
    reviews = normalized.reviews
    manifest = _read_json(task, manifest_path, {})
    manifest = manifest if isinstance(manifest, dict) else {}
    lost_hits = len(initial_hits - {key for key, row in reviews.items() if row.get("status") == "success"})
    counts["cache_hit_count"] -= lost_hits
    counts["cache_miss_count"] += lost_hits
    counts["cache_invalidated_count"] += lost_hits
    if type(manifest.get("request_count")) is int:
        counts["request_count"] = manifest["request_count"]
    stats = statistics(reviews)
    issues = []
    if manifest.get("status") == "configuration_failed":
        issues.append("模型配置暂不可用，未完成AI判断；岗位不进入保留或剔除名单，修好本机配置后可重跑本步。")
        stats["llm_pending_count"] = 0
    if code and not control.requested:
        issues.append(f"模型子进程退出码 {code}；未成功岗位不进入保留或剔除名单，可重试本步。")
    if normalized.invalidated_count:
        issues.append("模型结果的输入或配置签名不匹配，已保留为历史记录；本次未完成AI判断，岗位不进入保留或剔除名单，可重试本步。")
    status = control.requested or "completed"
    _write_json(task, manifest_path, {**manifest, **counts, "candidate_count": len(eligible),
                                     "completed_count": stats["llm_success_count"],
                                     "failed_count": stats["llm_failed_count"],
                                     "pending_count": max(0, len(eligible) - stats["llm_success_count"] - stats["llm_failed_count"]),
                                     "fallback_count": len(inputs) - stats["llm_success_count"], "workers": workers})
    if status == "completed":
        stats["llm_unreviewed_count"] = len(eligible) - stats["llm_success_count"] - stats["llm_failed_count"]
        stats["llm_fallback_count"] = len(inputs) - stats["llm_success_count"]
        stats["llm_pending_count"] = 0
    results = []
    for job in inputs:
        item = _analyze_ai_stage(job, reviews.get(job_id(job)))
        if job.get("jd_completeness") == "complete" and job_id(job) not in reviews:
            item["model_error"] = issues[0] if issues else "未获得有效AI判断；岗位不进入保留或剔除名单，待人工核实"
        results.append(item)
    _index(task, runtime, results)
    try:
        settings = project_llm_settings.load_settings(project_llm_settings.default_settings_path()) or {}
    except project_llm_settings.ProjectProviderError:
        settings = {}
    model = manifest.get("model") or settings.get("model", AI_DEFAULT_MODEL)
    api_mode = manifest.get("api_mode") or settings.get("api_mode", project_llm_settings.API_MODE_RESPONSES)
    configured_effort = settings.get("reasoning_effort", AI_DEFAULT_REASONING_EFFORT)
    verified = project_llm_settings.is_chat_reasoning_verified(
        settings.get("base_url", project_llm_settings.DEFAULT_BASE_URL), model, configured_effort,
        settings.get("chat_reasoning_verified_for", ""), chat_stream=settings.get("chat_stream", False),
    )
    effective_effort = manifest.get("reasoning_effort", configured_effort if api_mode == "responses" or verified else None)
    mode_label = project_llm_settings.API_MODE_LABELS[api_mode]
    effort_label = f"强度 {effective_effort}" if effective_effort is not None else "Chat 思考强度未验证，未发送"
    stats["llm_verification_count"] = sum(item["llm_stage_decision"] == "pending_verification" for item in results)
    stats["llm_recommended_count"] = sum(item.get("recommended_keep") is True for item in results)
    stats["llm_recommended_exclude_count"] = sum(item.get("recommended_keep") is False for item in results)
    stats["llm_related_count"] = stats["llm_recommended_count"]
    stats["llm_unrelated_count"] = stats["llm_recommended_exclude_count"]
    stats["llm_triage_total"] = (stats["llm_related_count"] + stats["llm_unrelated_count"]
                                  + stats["llm_verification_count"])
    emit.log(f"AI 复核：成功 {stats['llm_success_count']}，未完成AI判断 {stats['llm_fallback_count']}，待人工核实 {stats['llm_verification_count']}，请求失败 {stats['llm_failed_count']}；协议 {mode_label}，模型 {model}，{effort_label}。")
    return results, stats, status, issues


def _report_selection_summary(job: dict[str, Any]) -> str:
    """Summarize upstream choices, never the report step's in-flight snapshot."""

    audit = job.get("review_selection")
    audit = audit if isinstance(audit, dict) else {}
    summaries = []
    for stage in STAGES[:-1]:
        selection = audit.get(stage)
        if not isinstance(selection, dict):
            state = "未记录"
        elif selection.get("confirmed") is True:
            keep = selection.get("keep")
            state = "已确认保留" if keep is True else "已确认剔除" if keep is False else "已确认（选择未记录）"
            if type(selection.get("manual_keep")) is bool:
                state += "（手动）"
        elif selection.get("has_result") is True:
            keep = selection.get("keep")
            state = "保留，未确认" if keep is True else "剔除，未确认" if keep is False else "已有结果，未确认"
        elif selection.get("in_input") is False:
            state = "未进入本步"
        else:
            state = {
                "pending": "尚未执行", "running": "生成时正在处理", "completed": "已完成，未确认",
                "paused": "已暂停", "stopped": "已停止", "waiting": "等待处理", "failed": "执行失败",
                "stale": "结果已失效，待重跑",
            }.get(selection.get("status"), "未记录")
        summaries.append(f"{STAGE_LABELS[stage]}：{state}")
    return "；".join(summaries)


def _report_markdown_lines(records: list[dict], pool: list[dict], metadata: dict) -> list[str]:
    """Render an existing result snapshot without running or changing a task."""

    counts = metadata.get("classification_counts") or {
        **{name: sum(j["classification"] == name for j in records) for name in processor.CLASSIFICATIONS},
        "待核实": sum(j["classification"] == "待核实" for j in records),
    }
    pending = [item for item in records if item["classification"] == "待核实"]
    display = processor._report_markdown
    lines = ["# BOSS 岗位分步审核清单", "", f"任务：{display(metadata.get('task_name'))}", "",
             f"生成时间：{display(metadata.get('generated_at'))}", ""]
    lines += processor._report_overview_lines(records, pool)
    pending_count = len(pending)
    lines += [f"待核实：{pending_count} 岗（未完成有效 AI 判断，不计入可投清单）。", "",
              "人工确认剔除项保留追踪记录，但不进入可投 JSON。低薪、经验偏高、JD缺失均独立标注。", "",
              "审核摘要为报告生成时的前四步快照；后续确认不会改写报告，当前步骤状态以桌面任务页为准。", "",
              "以下为全部岗位详细证据。", ""]
    for classification in (*processor.CLASSIFICATIONS, "待核实"):
        lines += [f"## {classification}（{counts.get(classification, 0)}）", ""]
        cities: dict[str, list[dict]] = defaultdict(list)
        for item in records:
            if item["classification"] == classification:
                location = processor.text(item.get("city") or item.get("location") or metadata.get("city") or "城市待核实")
                cities[location.split("·", 1)[0]].append(item)
        for city, items in sorted(cities.items()):
            lines += [f"### {display(city)}（{len(items)}）", ""]
            for item in items:
                lines += processor._report_job_lines(item)
                lines.append("  - 前四步人工审核（生成时快照）：" + display(_report_selection_summary(item)))
                lines.append("  - 独立薪资标记：" + display(item.get("salary_flags")))
                lines.append("")
    return lines


def _run_report(task: ReviewTask, runtime: Path, control: StageControl, emit: _Emitter) -> tuple[list[dict], dict]:
    inputs = {job_id(j): j for j in task.input_jobs("report")}
    records = []
    jobs = task.all_jobs()
    simple_report = task.manifest["stages"]["llm"]["stats"].get("review_format") == SIMPLE_REVIEW_FORMAT
    prompt = _task_report_prompt(task) if any(isinstance(j.get("llm_review"), dict)
        and j["llm_review"].get("status") == "success" for j in jobs) else None
    for job in jobs:
        control.check()
        if simple_report:
            # A new-format report covers the entire trace, including records
            # excluded before AI. Do not synthesize a model judgment or rerun
            # list screening for those never-reviewed records.
            job = {**job, "review_format": SIMPLE_REVIEW_FORMAT}
        review = job.get("llm_review") if isinstance(job.get("llm_review"), dict) else None
        if simple_report and review and review.get("review_format") != SIMPLE_REVIEW_FORMAT:
            # Its original stage/checkpoint remains the historical source;
            # never promote legacy rich data into the active simple record.
            review = None
        item = processor.report_decision(processor.analyze_report_job(job, review, prompt=prompt, analyzer=_analyze))
        excluded = job.get("excluded_at")
        if excluded:
            item["reason"] = f"在「{STAGE_LABELS.get(excluded, excluded)}」确认剔除（仅本任务，原始数据保留）"
            item["reasons"] = [item["reason"]]
            item["recommended_keep"] = False
        records.append(item)
    if len({job_id(j) for j in records}) != len(records):
        raise PipelineError("报告岗位 ID 不唯一，未生成可投清单。")
    queues = Counter(job.get("list_queue") for job in records)
    pending_count = sum(j.get("classification") == "待核实" for j in records)
    all_pending_count = sum(bool(j.get("analysis_pending")) for j in records)
    counts = {name: sum(j["classification"] == name for j in records)
              for name in (*processor.CLASSIFICATIONS, "待核实")}
    metadata = {**_base_metadata(task), "unique_job_count": len(records), "classification_counts": counts,
                "pending_verification_count": pending_count,
                "analysis_pending_count": all_pending_count,
                "screening_version": processor.SCREENING_VERSION,
                "list_queue_counts": {key: queues.get(key, 0) for key in LIST_QUEUE_LABELS},
                "manual_excluded_count": sum(bool(j.get("excluded_at")) for j in records),
                "jd_fetched_count": sum(j.get("jd_completeness") == "complete" for j in records),
                **processor.report_cache_statistics(records)}
    if simple_report:
        metadata["review_format"] = SIMPLE_REVIEW_FORMAT
    prepared = runtime / "prepared"
    _write_json(task, prepared / "merged_jobs_index.json", {"metadata": metadata, "jobs": records})
    pool = [j for j in records if processor.report_pool_eligible(j)]
    pool_counts = {name: sum(j["classification"] == name for j in pool) for name in processor.CLASSIFICATIONS}
    _write_json(task, prepared / "hai_tou_pool.json", {
        "metadata": {**metadata, "unique_job_count": len(pool), "total_imported_jobs": len(records),
                     "classification_counts": pool_counts, "all_classification_counts": counts},
        "summary": {"total": len(pool), "classification_counts": pool_counts}, "jobs": pool})
    for track in ("current", "target"):
        rows = [j for j in records if any(isinstance(s, dict) and s.get("track") == track for s in j.get("observed_in", []))]
        _write_json(task, prepared / f"{track}_candidates.json", {"total": len(rows), "jobs": rows})
    lines = _report_markdown_lines(records, pool, metadata)
    markdown = _safe_path(task, prepared / "海投分层清单.md")
    temporary = _safe_path(task, markdown.with_name(markdown.name + ".tmp"))
    temporary.write_text(str(_redact("\n".join(lines))), encoding="utf-8")
    os.replace(temporary, markdown)
    artifacts = {"json_path": str(prepared / "merged_jobs_index.json"), "pool_path": str(prepared / "hai_tou_pool.json"),
                 "markdown_path": str(markdown)}
    stats = {**metadata, **artifacts, "artifacts": artifacts, "pool_count": len(pool), "total_jobs": len(records)}
    emit.log(f"报告已生成：{len(records)} 岗；重点 {counts['重点投递']}，海投 {counts['海投池']}，暂不投 {counts['暂不投']}，待核实 {pending_count}。可投池不含待核实项；未执行投递。")
    # The engine's result-set invariant concerns the confirmed input; excluded
    # originals remain in the complete artifact, never silently disappear.
    return [j for j in records if job_id(j) in inputs], stats


def reset_stage(task: ReviewTask, stage: str) -> dict:
    """Clear one stage's files and state, without invoking its executor."""
    if stage not in STAGES:
        raise TaskStateError("未知处理步骤。")

    def preflight() -> None:
        _guard_tree(task, task.path / "runtime")
        for previous in STAGES:
            _check_child_lease(task, previous)

    task.reset_stage(stage, before_reset=preflight)
    return task.snapshot()


def execute_stage(task: ReviewTask, stage: str, *, scraper_root: Path | None = None,
                   workers: int = AI_DEFAULT_WORKERS, batch_size: int = 30,
                   jd_workers: int = JD_DEFAULT_WORKERS, control: StageControl | None = None,
                   on_event: EventCallback | None = None,
                   browser_mode: str = "single", chrome_port: int = 9222,
                   edge_port: int = 9223, chrome_profile_dir: Path | str | None = None,
                   edge_profile_dir: Path | str | None = None,
                   chrome_profile: Path | str | None = None,
                   edge_profile: Path | str | None = None, redo: bool = False) -> dict:
    """Run one step only; ``redo=True`` now only resets it for later execution."""
    if stage not in STAGES:
        raise TaskStateError("未知处理步骤。")
    if type(redo) is not bool:
        raise TaskStateError("重置选项必须明确为是或否。")
    if redo:
        emit = _Emitter(task, stage, on_event)
        result = reset_stage(task, stage)
        emit.log(f"已重置「{STAGE_LABELS[stage]}」：本阶段文件与进度已清理，尚未运行；需要再次点击运行本步。")
        emit.emit("finished", result["stages"][stage])
        return result
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError("AI 并发数须为 1–32。")
    if type(jd_workers) is not int or not 1 <= jd_workers <= MAX_WORKERS:
        raise ValueError("JD 页面并发数须为 1–32。")
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError("JD 每批数量须为 1–100。")
    control = control or StageControl()
    chrome_profile_dir = chrome_profile_dir or chrome_profile
    edge_profile_dir = edge_profile_dir or edge_profile
    emit = _Emitter(task, stage, on_event)

    def preflight() -> None:
        _guard_tree(task, task.path / "runtime")
        for previous in STAGES:
            _check_child_lease(task, previous)

    # The reset action has already completed any destructive work. A normal
    # begin now starts or resumes the selected round; preflight still runs
    # under the task/run leases before the checkpoint is committed.
    try:
        task.begin(stage, before_start=preflight)
    except PipelineError as error:
        # Preflight rejected the run before begin committed. Keep its previous
        # state and checkpoints; this worker never owned a running stage.
        emit.log(str(error))
        snapshot = task.snapshot()
        emit.emit("finished", snapshot)
        return snapshot
    task._run_release_deferred = True
    try:
        runtime = task.run_directory(stage)
        _guard_tree(task, runtime)
        # Check before replacing any input/checkpoint, including a live child
        # from a different step left behind by an unexpectedly closed window.
        control.check()
        emit.log(f"开始「{STAGE_LABELS[stage]}」：本次只运行这一步，完成后等待人工确认。")
        status, issues = "completed", []
        if stage == "import":
            rows, stats = _run_import(task, runtime, control, emit)
        elif stage == "shortlist":
            rows, stats = _run_shortlist(task, runtime, control, emit)
        elif stage == "fetch-jd":
            rows, stats, status, issues = _run_jd(
                task, runtime, scraper_root, workers, batch_size, control, emit, jd_workers,
                browser_mode, chrome_port, edge_port, chrome_profile_dir, edge_profile_dir,
            )
        elif stage == "llm":
            rows, stats, status, issues = _run_reviews(task, runtime, workers, batch_size, control, emit)
        else:
            rows, stats = _run_report(task, runtime, control, emit)
        # A late pause must never turn an interrupted stage into a confirmed one.
        if control.requested:
            status = control.requested
        task.finish(stage, rows, stats=_redact(stats), status=status, issues=_redact(issues))
        if status == "waiting":
            emit.emit("waiting", issues[0] if issues else "请人工处理后继续本步。")
        elif status == "completed":
            emit.log("本步已完成，等待你查看结果并确认名单；下一步不会自动运行。")
        else:
            emit.log("本步已停止或暂停，已落盘检查点保留。")
    except _Interrupted:
        task.interrupt(stage, status=control.requested or "paused", issue="已在安全点停止，原始数据和检查点保留。")
    except Exception as error:
        # Third-party exception bodies can contain headers or provider responses.
        # Only deliberate local PipelineError messages are safe to show verbatim.
        message = str(error) if isinstance(error, PipelineError) else f"本步执行失败（{type(error).__name__}）；检查任务输入、工具路径和已脱敏日志后重试。"
        emit.log(message)
        task.interrupt(stage, status="failed", issue=str(_redact(message)))
    finally:
        # finish and even interrupt may both fail to persist (for example a
        # competing opener holds .task.lock). Once our children have exited,
        # abandon only this worker's lease so the next open can recover the
        # durable running checkpoint. A live owned child keeps the lease.
        with control._lock:
            owned_children = tuple(control._children)
        owned_lease = task._run_lease
        task._run_release_deferred = False

        def release_finished_owner() -> None:
            # Hold concrete child/Popen objects and this exact lease, never an
            # old PID or a newer worker's ownership. A failed termination can
            # outlive execute_stage; retain ownership until it actually exits.
            while any(child.poll() is None for child in owned_children):
                threading.Event().wait(.05)
            with task._thread_lock:
                if task._run_lease is owned_lease and task._running_stage == stage:
                    task._release_run()

        if any(child.poll() is None for child in owned_children):
            control._cleanup_thread = threading.Thread(
                target=release_finished_owner, name="review-owned-child-cleanup", daemon=True)
            control._cleanup_thread.start()
        else:
            release_finished_owner()
    snapshot = task.snapshot()
    emit.emit("finished", snapshot)
    return snapshot
