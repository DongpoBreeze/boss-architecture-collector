"""Local, durable state for the explicitly reviewed five-stage workbench.

No processing, browser, model, or subprocess is started here.  The manifest is
the atomic commit point; inputs/results are immutable, task-local JSON files.
An OS file lease distinguishes an active run from an abandoned ``running``
checkpoint without guessing from timestamps or interfering with another UI.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from desktop_data import _redact, _redact_text
from screening_policy import AI_STAGE_PROJECTION, AI_STAGE_RESULT_FIELDS, SEMANTIC_RESULT_FIELDS
import review_rules
from llm_client import SIMPLE_REVIEW_FORMAT, JsonSchemaValidationError, simple_review_payload


STAGES = ("import", "shortlist", "fetch-jd", "llm", "report")
REVIEW_STAGES = STAGES[:-1]
STAGE_LABELS = {
    "import": "导入与去重",
    "shortlist": "列表初筛",
    "fetch-jd": "JD 补抓",
    "llm": "AI 语义复核",
    "report": "生成报告",
}
_STATUSES = {"pending", "running", "completed", "paused", "stopped", "waiting", "failed", "stale"}
_FINISH_STATUSES = {"completed", "paused", "stopped", "waiting", "failed"}
_INTERRUPT_STATUSES = _FINISH_STATUSES - {"completed"}
_EDITABLE_STATUSES = _FINISH_STATUSES
_MANIFEST = "task_manifest.json"
_SCOPE_KEYS = (
    "schema_version", "task_id", "name", "source_root", "city", "track",
    "sample_size", "config_path", "config_sha256", "source_inventory",
)


class TaskStateError(ValueError):
    """A safe, user-facing task validation or persistence error."""

    def __init__(self, message: str):
        super().__init__(_redact_text(str(message)))


def job_id(job: dict) -> str:
    """Return one stable ID; never silently discard an unidentifiable row."""
    if not isinstance(job, dict):
        raise TaskStateError("岗位记录必须是对象。")
    for key in ("job_id", "dedupe_key", "encrypt_job_id", "job_link"):
        value = job.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TaskStateError("岗位的稳定 ID 格式无效。")
        value = str(value).strip()
        if value:
            return value
    raise TaskStateError("岗位缺少稳定 ID（job_id / dedupe_key / encrypt_job_id / job_link）。")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError("Not a JSON value")


def _encoded(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False,
                           default=_json_default) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise TaskStateError("任务数据无法安全保存为 JSON。") from None


def _json_copy(value: Any) -> Any:
    return json.loads(_encoded(value))


def _metadata(value: dict | None) -> dict:
    if value is not None and not isinstance(value, dict):
        raise TaskStateError("阶段统计必须是对象。")
    try:
        return _json_copy(_redact(value or {}))
    except (TypeError, ValueError, RecursionError):
        raise TaskStateError("阶段统计无法安全保存为 JSON。") from None


def _issues(value: list | None) -> list:
    if value is not None and not isinstance(value, list):
        raise TaskStateError("阶段问题必须是列表。")
    try:
        return _json_copy(_redact(value or []))
    except (TypeError, ValueError, RecursionError):
        raise TaskStateError("阶段问题无法安全保存为 JSON。") from None


def _absolute(path: Path) -> Path:
    try:
        return Path(os.path.abspath(Path(path).expanduser()))
    except (OSError, TypeError, ValueError, RuntimeError):
        raise TaskStateError("目录路径无效。") from None


def _no_links(path: Path) -> None:
    """Reject symlinks AND Windows junctions, including on Python < 3.12."""
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise TaskStateError("目录或文件含符号链接 / junction，已拒绝访问。")
    except TaskStateError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise TaskStateError("无法安全检查目录或文件。") from None


def _task_location(path: Path) -> None:
    _no_links(path)
    if any(part.casefold() in {"raw", "prepared"} for part in path.parts):
        raise TaskStateError("任务和运行目录不能位于 raw / prepared 数据目录内。")


def _walk_ordinary(root: Path) -> Iterator[Path]:
    """Walk only after inspecting each entry, never traverse a reparse point."""
    _no_links(root)
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
            for entry in entries:
                _no_links(entry)
                if entry.is_dir():
                    pending.append(entry)
                elif entry.is_file():
                    yield entry
                else:
                    raise TaskStateError("任务数据中存在非普通文件。")
        except TaskStateError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise TaskStateError("无法安全读取目录清单。") from None


def _source_inventory(root: Path) -> list[dict]:
    """Pin file identity cheaply; deliberately do not hash every raw file."""
    records = []
    for track in ("current", "target"):
        directory = root / "raw" / track
        _no_links(directory)
        if not directory.is_dir():
            raise TaskStateError("请选择同时包含 raw/current 与 raw/target 的完整采集根目录。")
        for path in _walk_ordinary(directory):
            if path.suffix.casefold() != ".json":
                continue
            try:
                info = path.stat()
            except OSError:
                raise TaskStateError("无法读取固定源文件信息。") from None
            records.append({"path": path.relative_to(root).as_posix(),
                            "size": info.st_size, "mtime_ns": info.st_mtime_ns})
    return sorted(records, key=lambda item: item["path"])


def _scope_signature(value: dict) -> str:
    return hashlib.sha256(_encoded({key: value[key] for key in _SCOPE_KEYS})).hexdigest()


class _Lease:
    def __init__(self, handle: Any):
        self.handle = handle

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    def __del__(self) -> None:
        self.close()


def _try_lease(path: Path) -> _Lease | None:
    _no_links(path)
    handle = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        handle = os.fdopen(os.open(path, flags, 0o600), "r+b", buffering=0)
        _no_links(path)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _Lease(handle)
    except OSError as error:
        if handle is not None:
            handle.close()
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EBUSY, errno.EDEADLK}:
            return None
        raise TaskStateError("无法安全锁定任务文件。") from None
    except TaskStateError:
        if handle is not None:
            handle.close()
        raise


def _read_json(path: Path) -> Any:
    _no_links(path)
    try:
        def invalid_constant(_value: str) -> None:
            raise ValueError("Invalid JSON number")
        return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=invalid_constant)
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise TaskStateError("任务 JSON 缺失、损坏或不可读取。") from None


def _windows_change_time(path: Path) -> int:
    """Read NTFS's change clock; Python < 3.12 st_ctime is creation time."""
    import ctypes
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [(field, ctypes.c_longlong) for field in (
            "CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime")]
        _fields_.append(("FileAttributes", wintypes.DWORD))

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    query = kernel.GetFileInformationByHandleEx
    query.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    query.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    name = str(path.absolute())
    if not name.startswith("\\\\?\\"):
        name = "\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\") else "\\\\?\\" + name
    handle = create(name, 0x80, 0x7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        basic = FileBasicInfo()
        if not query(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise ctypes.WinError(ctypes.get_last_error())
        if basic.FileAttributes & 0x400:
            raise OSError("reparse point")
        return basic.ChangeTime
    finally:
        close(handle)


def _file_version(path: Path) -> tuple:
    """A content-cache token, including same-size/mtime external rewrites."""
    try:
        _no_links(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        changed = _windows_change_time(path) if os.name == "nt" else info.st_ctime_ns
        return (str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, changed)
    except OSError:
        raise TaskStateError("任务文件缺失或无法安全读取其版本，请刷新后重试。") from None


def _atomic_manifest(path: Path, value: dict) -> None:
    data = _encoded(value)
    temporary: Path | None = None
    _no_links(path)
    try:
        fd, filename = tempfile.mkstemp(prefix=".task-manifest-", suffix=".tmp", dir=path.parent)
        temporary = Path(filename)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _no_links(path)
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    except OSError:
        raise TaskStateError("任务保存失败；上一次完整检查点仍然保留。") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _validated_jobs(jobs: list[dict]) -> list[dict]:
    if not isinstance(jobs, list):
        raise TaskStateError("阶段结果必须是岗位列表。")
    result, seen = [], set()
    for job in jobs:
        key = job_id(job)
        if key in seen:
            raise TaskStateError("阶段结果包含重复岗位 ID。")
        seen.add(key)
        item = _json_copy(job)
        item["job_id"] = key
        result.append(item)
    return result


def _merge_job(base: dict, enhancement: dict) -> dict:
    result = copy.deepcopy(base)
    ai_projection = (enhancement.get("llm_stage_projection") == AI_STAGE_PROJECTION
                     or enhancement.get("review_format") == "simple-v1")
    if ai_projection:
        # Only a newly executed AI step may replace its derived view.  Empty
        # model arrays are meaningful and must not resurrect older report/code
        # conclusions; provenance and durable manual choices stay untouched.
        # Explicit simple-format reports own the same derived projection.
        for field in AI_STAGE_RESULT_FIELDS:
            result.pop(field, None)
    for field, value in enhancement.items():
        if (value not in (None, "", [], {}) or field not in result
                or ai_projection and field == "recommended_keep"):
            # ``None`` is the AI pending state, not an absent enhancement.
            # It must replace a JD/legacy default recommendation of True.
            result[field] = copy.deepcopy(value)
    if "classification" in enhancement and "llm_stage_projection" not in enhancement:
        # A report classifier supersedes the AI-only display marker, even
        # though its input snapshot still contains the earlier stage view.
        result.pop("llm_stage_projection", None)
        result.pop("llm_stage_decision", None)
    if enhancement.get("jd_analysis_status") == "not_run" and "classification" not in enhancement:
        # A body-only JD result must not resurrect cached semantic conclusions
        # from the import/shortlist input while preserving source/audit fields.
        for field in AI_STAGE_RESULT_FIELDS:
            result.pop(field, None)
        for field in ("jd", "jd_record", "jd_fetch_issue"):
            if field in enhancement:
                result[field] = copy.deepcopy(enhancement[field])
            else:
                result.pop(field, None)
    # Empty or truncated downstream provenance must not discard any source.
    for field in ("observed_in", "sources", "source_files"):
        values = []
        for item in (base, enhancement):
            entries = item.get(field)
            entries = entries if isinstance(entries, list) else [entries] if entries not in (None, "") else []
            for value in entries:
                if value not in values:
                    values.append(copy.deepcopy(value))
        if values or field in base or field in enhancement:
            result[field] = values
    contexts = result.setdefault("observed_in", [])
    for item in (base, enhancement):
        context = copy.deepcopy(item.get("source_context")) if isinstance(item.get("source_context"), dict) else {}
        for field in ("source_file", "source_row", "source_line_number"):
            if field in item:
                context[field] = copy.deepcopy(item[field])
        if context and context not in contexts:
            contexts.append(context)
    return result


def _ai_recommendation(job: dict) -> bool | None:
    """Only a complete JD and a valid, identified simple AI result decide."""
    review = job.get("llm_review")
    jd = job.get("jd")
    complete = job.get("jd_completeness", "complete" if isinstance(jd, str) and jd.strip() else "missing")
    if complete != "complete" or not isinstance(jd, str) or not jd.strip():
        return None
    if (isinstance(review, dict) and review.get("status") == "success"
            and review.get("review_format") == SIMPLE_REVIEW_FORMAT
            and str(review.get("job_id", "")) == job_id(job)):
        try:
            judgment = simple_review_payload(review)["判断"]
        except JsonSchemaValidationError:
            return None
        return True if judgment == "符合" else False if judgment == "不符合" else None
    # A few intermediate local checkpoints stored an explicit binary AI
    # projection before review payload metadata was added. Accept that only
    # when it carries the projection marker; arbitrary/default booleans are
    # never evidence of a completed decision.
    if job.get("llm_stage_projection") == AI_STAGE_PROJECTION:
        decision = job.get("llm_stage_decision")
        return True if decision == "related" else False if decision == "unrelated" else None
    return None


def _stage_template() -> dict:
    return {
        "status": "pending", "confirmed": False,
        "input_ids": [], "keep_ids": [], "excluded_ids": [], "pending_ids": [], "result_ids": [],
        "recommended_keep_ids": [], "recommended_exclude_ids": [], "manual_choices": {},
        "progress": {"done": 0, "total": 0}, "stats": {}, "issues": [],
        "input_file": None, "result_file": None, "result_input_file": None,
        "input_revision": None, "result_input_revision": None, "result_input_ids": [],
        "revision": 0, "confirmed_revision": None, "attempt": 0, "redo_count": 0,
        "reset_pending": False,
        "started_at": None, "finished_at": None, "confirmed_at": None,
        "invalidated_by": None, "history": [],
    }


def stage_selection_version(info: dict) -> tuple:
    """Immutable selection token for optimistic, in-transaction UI edits.

    A new explicit manual choice can leave ``revision`` unchanged when its
    keep value already matched the recommendation. Include that intent, the
    result/input identities and confirmation, not only a revision number.
    """
    scalar_fields = ("status", "result_file", "revision", "result_input_revision",
                     "confirmed", "confirmed_revision", "input_file", "input_revision", "result_input_file")
    id_fields = ("result_ids", "keep_ids", "input_ids", "result_input_ids", "excluded_ids", "pending_ids",
                 "recommended_keep_ids", "recommended_exclude_ids")
    if (not isinstance(info, dict)
            or any(field not in info for field in (*scalar_fields, *id_fields, "manual_choices"))):
        raise TaskStateError("人工选择快照无效，请刷新后重新审核。")
    if (not isinstance(info["status"], str) or info["status"] not in _STATUSES
            or type(info["confirmed"]) is not bool
            or type(info["revision"]) is not int or info["revision"] < 0
            or any(info[field] is not None and (type(info[field]) is not int or info[field] < 0)
                   for field in ("result_input_revision", "confirmed_revision", "input_revision"))
            or any(info[field] is not None and not isinstance(info[field], str)
                   for field in ("result_file", "input_file", "result_input_file"))
            or any(not isinstance(info[field], list)
                   or any(not isinstance(key, str) or not key for key in info[field])
                   or len(set(info[field])) != len(info[field]) for field in id_fields)
            or not isinstance(info["manual_choices"], dict)
            or any(not isinstance(key, str) or type(keep) is not bool
                   for key, keep in info["manual_choices"].items())):
        raise TaskStateError("人工选择快照无效，请刷新后重新审核。")
    return (*(info[field] for field in scalar_fields),
            *(tuple(info[field]) for field in id_fields), tuple(sorted(info["manual_choices"].items())))


class ReviewTask:
    """A fixed source/range and independently run, manually confirmed stages."""

    def __init__(self, path: Path, manifest: dict, *, read_only: bool = False,
                 review_only: bool = False):
        self._path = path
        self._read_only = read_only
        self._review_only = review_only
        if isinstance(manifest, dict) and isinstance(manifest.get("stages"), dict):
            for info in manifest["stages"].values():
                if isinstance(info, dict) and "redo_count" not in info:
                    info["redo_count"] = 0
                if isinstance(info, dict) and "reset_pending" not in info:
                    info["reset_pending"] = False
        self._manifest = manifest
        self._scope = manifest.get("scope_signature") if isinstance(manifest, dict) else None
        self._thread_lock = threading.RLock()
        self._run_lease: _Lease | None = None
        self._running_stage: str | None = None
        self._run_release_deferred = False
        self._ai_projection_cache: tuple | None = None
        self._upgrade_pending_selection(manifest)
        self._validate_review(manifest) if review_only else self._validate(manifest)

    def _upgrade_pending_selection(self, value: dict) -> None:
        """Project older AI selections in memory; never rewrite old artifacts.

        This runs on every load, so open, refresh and edits share one contract.
        Manual choices take precedence over AI suggestions, including rows with
        no definite AI decision. Loading never rewrites their technical evidence.
        """
        stages = value.get("stages") if isinstance(value, dict) else None
        if not isinstance(stages, dict):
            return
        for info in stages.values():
            if isinstance(info, dict):
                if not isinstance(info.get("pending_ids"), list):
                    info["pending_ids"] = []
                if not isinstance(info.get("recommended_exclude_ids"), list):
                    info["recommended_exclude_ids"] = []
        info = stages.get("llm")
        if not isinstance(info, dict) or not isinstance(info.get("result_ids"), list):
            return
        manual = info.get("manual_choices")
        if (not isinstance(manual, dict)
                or any(not isinstance(key, str) or type(keep) is not bool for key, keep in manual.items())):
            return  # Let validation reject malformed audit data without coercing it.
        # Reset creates a genuinely empty AI stage.  Do not synthesize
        # zero-valued triage counters while loading that fresh checkpoint;
        # the empty stats map is part of the reset contract.
        if not info["result_ids"] and info.get("result_file") is None:
            self._ai_projection_cache = None
            return
        rows = []
        projection = None
        if isinstance(info.get("result_file"), str):
            try:
                path = self._artifact_path("llm", info["result_file"])
                version = _file_version(path)
                projection = self._ai_projection_cache
                if projection is None or projection[0] != version:
                    rows = self._read_jobs("llm", info["result_file"])
                    if _file_version(path) != version:
                        raise TaskStateError("任务文件在读取时发生变化，请刷新后重试。")
                    related, unrelated = [], []
                    for job in rows:
                        recommendation = _ai_recommendation(job)
                        if recommendation is True:
                            related.append(job_id(job))
                        elif recommendation is False:
                            unrelated.append(job_id(job))
                    projection = (version, rows, related, unrelated)
                    self._ai_projection_cache = projection
                else:
                    rows = projection[1]
            except (OSError, UnicodeError, ValueError, TaskStateError):
                rows = []
                projection = self._ai_projection_cache = None
        if [job_id(job) for job in rows] != info.get("result_ids"):
            # If legacy evidence cannot be read, do not trust its default
            # keep/exclude lists. Keep every result in the safe pending state.
            rows = []
        before = (info.get("keep_ids"), info.get("excluded_ids"), info.get("pending_ids"))
        recommended_keep, recommended_exclude = (list(projection[2]), list(projection[3])) if rows and projection else ([], [])
        info["recommended_keep_ids"] = recommended_keep
        info["recommended_exclude_ids"] = recommended_exclude
        self._select(info, "llm")
        if isinstance(info.get("stats"), dict):
            info["stats"].update({
                "llm_recommended_count": len(info["recommended_keep_ids"]),
                "llm_recommended_exclude_count": len(info["recommended_exclude_ids"]),
                "llm_related_count": len(info["recommended_keep_ids"]),
                "llm_unrelated_count": len(info["recommended_exclude_ids"]),
                "llm_verification_count": len(info["pending_ids"]),
                "llm_triage_total": len(info["result_ids"]),
            })
        after = (info["keep_ids"], info["excluded_ids"], info["pending_ids"])
        if before != after and info.get("confirmed"):
            info.update(confirmed=False, confirmed_revision=None, confirmed_at=None)
            self._invalidate_downstream(value, "llm")

    @classmethod
    def create(cls, source_root: Path, task_parent: Path, name: str,
               city: str | None = None, track: str = "all",
               sample_size: int | None = None, config_path: Path | None = None) -> ReviewTask:
        root, parent = _absolute(source_root), _absolute(task_parent)
        _no_links(root)
        _task_location(parent)
        if not root.is_dir():
            raise TaskStateError("采集根目录不存在。")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise TaskStateError("请输入 1–120 个字符的任务名称。")
        if city is not None and (not isinstance(city, str) or not city.strip()):
            raise TaskStateError("城市必须是名称、城市 ID 或留空。")
        tracks = {"all": "all", "current": "current", "target": "target",
                  "当前+成长": "all", "当前岗位": "current", "成长目标": "target"}
        if not isinstance(track, str) or track not in tracks:
            raise TaskStateError("岗位轨道必须是 all / current / target。")
        if sample_size is not None and (type(sample_size) is not int or sample_size < 0):
            raise TaskStateError("去重岗位数量必须是非负整数或留空。")
        inventory = _source_inventory(root)
        config, config_hash = None, None
        if config_path is not None:
            config = _absolute(config_path)
            _no_links(config)
            try:
                config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
            except OSError:
                raise TaskStateError("无法读取任务配置文件。") from None
        task_id = uuid.uuid4().hex
        slug = re.sub(r"[^\w.-]+", "-", name.strip(), flags=re.UNICODE).strip(".-")[:40] or "review"
        path = parent / f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}-{task_id}"
        _task_location(path)
        manifest = {
            "schema_version": 1, "task_id": task_id, "name": name.strip(),
            "source_root": str(root), "city": city.strip() if city else None,
            "track": tracks[track], "sample_size": sample_size,
            "config_path": str(config) if config else None, "config_sha256": config_hash,
            "source_inventory": inventory, "created_at": _now(), "updated_at": _now(),
            "stages": {stage: _stage_template() for stage in STAGES},
            "baseline_ids": None, "import_baseline_file": None, "revision": 0,
        }
        manifest["scope_signature"] = _scope_signature(manifest)
        try:
            parent.mkdir(parents=True, exist_ok=True)
            _task_location(parent)
            path.mkdir(exist_ok=False)
            (path / "stages").mkdir()
            (path / "runtime").mkdir()
            _atomic_manifest(path / _MANIFEST, manifest)
        except OSError:
            raise TaskStateError("无法创建唯一任务目录；未覆盖任何已有任务。") from None
        return cls(path, manifest)

    @classmethod
    def open(cls, path: Path) -> ReviewTask:
        path = _absolute(path)
        if path.name == _MANIFEST and not path.is_dir():
            path = path.parent
        _task_location(path)
        if not path.is_dir():
            raise TaskStateError("请选择已有任务目录或 task_manifest.json。")
        task = cls(path, _read_json(path / _MANIFEST))
        # Inspection, not execution: also reject an injected runtime junction.
        # Check the manifest first, so selecting a non-task does not recursively
        # enumerate an arbitrary collection root or user home directory.
        for _file in _walk_ordinary(path):
            pass
        if any(info["status"] == "running" for info in task._manifest["stages"].values()):
            with task._edit() as value:
                lease = _try_lease(path / ".running.lock")
                if lease is not None:
                    try:
                        for info in value["stages"].values():
                            if info["status"] == "running":
                                info["status"] = "paused"
                                info["finished_at"] = _now()
                                info["issues"].append("上次运行已中断；检查点已保留，请手动继续本步。")
                    finally:
                        lease.close()
        return task

    @classmethod
    def open_readonly(cls, path: Path, *, review_only: bool = True) -> ReviewTask:
        """Open for inspection without recovery writes or browsing raw data.

        Delivery readers deliberately validate only the four review stages.
        Historical report artifacts are outside that contract. The original
        five-key manifest remains intact and this instance rejects all edits.
        """
        path = _absolute(path)
        if path.name == _MANIFEST and not path.is_dir():
            path = path.parent
        _task_location(path)
        if not path.is_dir():
            raise TaskStateError("请选择已有任务目录或 task_manifest.json。")
        return cls(path, _read_json(path / _MANIFEST), read_only=True, review_only=review_only)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def manifest(self) -> dict:
        with self._thread_lock:
            return copy.deepcopy(self._manifest)

    def _stage(self, stage: str) -> str:
        if stage not in STAGES:
            raise TaskStateError("未知审核步骤。")
        return stage

    def _artifact_path(self, stage: str, relative: str) -> Path:
        pattern = rf"stages/{re.escape(stage)}/(?:input|results)-[a-f0-9]{{32}}\.json"
        if not isinstance(relative, str) or not re.fullmatch(pattern, relative):
            raise TaskStateError("任务结果路径越界或格式无效。")
        path = self.path.joinpath(*PurePosixPath(relative).parts)
        _no_links(path)
        try:
            path.resolve().relative_to(self.path.resolve())
        except (OSError, ValueError, RuntimeError):
            raise TaskStateError("任务结果路径不能指向任务目录外部。") from None
        return path

    def _runtime_path(self, stage: str, relative: str) -> Path:
        # Old manifests have no pointer and use runtime/<stage>. Each explicit
        # redo gets a sibling directory so a clean execution never reads stale
        # files while the prior step outputs are being removed.
        pattern = rf"runtime/{re.escape(stage)}(?:-redo-[a-f0-9]{{32}})?"
        if not isinstance(relative, str) or not re.fullmatch(pattern, relative):
            raise TaskStateError("任务运行目录越界或不属于当前步骤。")
        path = self.path.joinpath(*PurePosixPath(relative).parts)
        _task_location(path)
        try:
            path.resolve().relative_to(self.path.resolve())
        except (OSError, ValueError, RuntimeError):
            raise TaskStateError("任务运行目录不能指向任务目录外部。") from None
        return path

    def _validate(self, value: dict) -> None:
        _task_location(self.path)
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
            raise TaskStateError("不支持或损坏的任务格式。")
        if any(key not in value for key in _SCOPE_KEYS) or value.get("scope_signature") != _scope_signature(value):
            raise TaskStateError("任务固定数据源或范围已改变；请新建任务。")
        if self._scope is not None and value["scope_signature"] != self._scope:
            raise TaskStateError("任务身份或固定范围已改变。")
        if (not isinstance(value.get("name"), str) or not value["name"].strip()
                or not isinstance(value.get("source_root"), str)
                or not Path(value["source_root"]).is_absolute()
                or not isinstance(value.get("track"), str) or value["track"] not in {"all", "current", "target"}
                or not isinstance(value.get("source_inventory"), list)
                or (value.get("city") is not None and not isinstance(value["city"], str))
                or (value.get("sample_size") is not None and (type(value["sample_size"]) is not int or value["sample_size"] < 0))
                or type(value.get("revision")) is not int or value["revision"] < 0
                or any(not isinstance(value.get(key), str) for key in ("created_at", "updated_at"))):
            raise TaskStateError("任务固定范围字段损坏。")
        if value["config_path"] is not None and (
                not isinstance(value["config_path"], str) or not Path(value["config_path"]).is_absolute()
                or not isinstance(value["config_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["config_sha256"])):
            raise TaskStateError("任务固定配置字段损坏。")
        rules = value.get("review_rules")
        if rules is not None and (
                not isinstance(rules, dict)
                or not isinstance(rules.get("text"), str) or not rules["text"].strip()
                or not isinstance(rules.get("updated_at"), str) or not rules["updated_at"].strip()):
            raise TaskStateError("任务 AI 复核规则损坏。")
        source_paths = set()
        for record in value["source_inventory"]:
            if (not isinstance(record, dict) or not isinstance(record.get("path"), str)
                    or not re.fullmatch(r"raw/(?:current|target)/[^\\]+\.json", record["path"], re.I)
                    or any(part in {".", ".."} for part in PurePosixPath(record["path"]).parts)
                    or any(type(record.get(key)) is not int or record[key] < 0 for key in ("size", "mtime_ns"))
                    or record["path"] in source_paths):
                raise TaskStateError("任务固定源文件清单损坏。")
            source_paths.add(record["path"])
        stages = value.get("stages")
        if not isinstance(stages, dict) or set(stages) != set(STAGES):
            raise TaskStateError("任务步骤清单损坏。")
        running = 0
        for stage, info in stages.items():
            if not isinstance(info, dict) or any(key not in info for key in _stage_template()):
                raise TaskStateError("任务步骤字段缺失。")
            if not isinstance(info["status"], str) or info["status"] not in _STATUSES or type(info["confirmed"]) is not bool:
                raise TaskStateError("任务步骤状态无效。")
            running += info["status"] == "running"
            for key in ("input_ids", "result_ids", "keep_ids", "excluded_ids", "pending_ids",
                        "recommended_keep_ids", "recommended_exclude_ids", "result_input_ids"):
                ids = info[key]
                if (not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids)
                        or len(set(ids)) != len(ids)):
                    raise TaskStateError("任务岗位 ID 清单损坏。")
            result_ids = set(info["result_ids"])
            selections = [set(info[field]) for field in ("keep_ids", "excluded_ids", "pending_ids")]
            if (any(selections[i] & selections[j] for i in range(3) for j in range(i + 1, 3))
                    or set.union(*selections) != result_ids
                    or not set(info["recommended_keep_ids"]) <= result_ids
                    or not set(info["recommended_exclude_ids"]) <= result_ids
                    or set(info["recommended_keep_ids"]) & set(info["recommended_exclude_ids"])):
                raise TaskStateError("保留、剔除与待核实名单不构成完整互斥集合。")
            if (not isinstance(info["manual_choices"], dict)
                    or any(not isinstance(key, str) or type(keep) is not bool for key, keep in info["manual_choices"].items())
                    or not isinstance(info["stats"], dict) or not isinstance(info["issues"], list)
                    or not isinstance(info["history"], list)):
                raise TaskStateError("任务审核记录损坏。")
            if stage == "llm":
                recommended_keep = set(info["recommended_keep_ids"])
                recommended_exclude = set(info["recommended_exclude_ids"])
                manual = info["manual_choices"]
                expected_keep = {key for key in result_ids if manual.get(key, key in recommended_keep)}
                expected_exclude = {key for key in result_ids
                                    if manual.get(key, key not in recommended_exclude) is False}
                if (set(info["keep_ids"]) != expected_keep
                        or set(info["excluded_ids"]) != expected_exclude
                        or set(info["pending_ids"]) != result_ids - expected_keep - expected_exclude):
                    raise TaskStateError("AI 人工选择、自动建议与待核实名单不一致。")
            progress = info["progress"]
            if (not isinstance(progress, dict) or any(type(progress.get(key)) is not int or progress[key] < 0 for key in ("done", "total"))
                    or progress["done"] > progress["total"]):
                raise TaskStateError("任务进度损坏。")
            if any(type(info[key]) is not int or info[key] < 0 for key in ("revision", "attempt")):
                raise TaskStateError("任务版本损坏。")
            if type(info.get("redo_count", 0)) is not int or info.get("redo_count", 0) < 0:
                raise TaskStateError("任务重做次数损坏。")
            if type(info.get("reset_pending")) is not bool:
                raise TaskStateError("任务步骤重置状态损坏。")
            self._runtime_path(stage, info.get("runtime_directory", f"runtime/{stage}"))
            if any(info[key] is not None and (type(info[key]) is not int or info[key] < 0)
                   for key in ("input_revision", "result_input_revision", "confirmed_revision")):
                raise TaskStateError("任务输入或确认版本损坏。")
            if (info["status"] == "completed" and info["result_file"] is None
                    or info["result_file"] is None and bool(info["result_ids"])
                    or info["input_ids"] and info["input_file"] is None):
                raise TaskStateError("任务完成状态与输入 / 结果检查点不一致。")
            for key in ("input_file", "result_file", "result_input_file"):
                if info[key] is not None and not self._artifact_path(stage, info[key]).is_file():
                    raise TaskStateError("任务输入或结果检查点缺失。")
            for old in info["history"]:
                if not isinstance(old, dict):
                    raise TaskStateError("任务历史记录损坏。")
                if "runtime_directory" in old:
                    self._runtime_path(stage, old["runtime_directory"])
                for key in ("input_file", "result_file"):
                    if old.get(key) is not None and not self._artifact_path(stage, old[key]).is_file():
                        raise TaskStateError("任务历史检查点缺失。")
                if old.get("run_input_file") is not None and not self._artifact_path(stage, old["run_input_file"]).is_file():
                    raise TaskStateError("任务历史运行输入缺失。")
            if info["confirmed"] and (info["status"] != "completed" or info["result_file"] is None
                                      or info["confirmed_revision"] != info["revision"]):
                raise TaskStateError("任务确认状态无效。")
        if running > 1:
            raise TaskStateError("同一任务不能同时运行多个步骤。")
        for index, stage in enumerate(STAGES[1:], start=1):
            info, previous = stages[stage], stages[STAGES[index - 1]]
            if info["confirmed"] and (
                    any(not stages[key]["confirmed"] or stages[key]["status"] != "completed" for key in STAGES[:index])
                    or info["input_ids"] != previous["keep_ids"] or info["result_ids"] != info["input_ids"]
                    or info["input_revision"] != previous["revision"]):
                raise TaskStateError("已确认步骤的前置名单或结果已经失效。")
        if ("baseline_ids" not in value or "import_baseline_file" not in value
                or (value["baseline_ids"] is None) != (value["import_baseline_file"] is None)
                or stages["import"]["input_ids"]
                or stages["import"]["status"] == "completed" and value["import_baseline_file"] is None):
            raise TaskStateError("任务导入基线状态损坏。")
        if value["import_baseline_file"] is not None:
            if not self._artifact_path("import", value["import_baseline_file"]).is_file():
                raise TaskStateError("任务导入基线缺失。")
            baseline_ids = value.get("baseline_ids")
            if (not isinstance(baseline_ids, list) or any(not isinstance(key, str) or not key for key in baseline_ids)
                    or len(set(baseline_ids)) != len(baseline_ids)):
                raise TaskStateError("任务导入基线损坏。")

    def _validate_review(self, value: dict) -> None:
        if (not isinstance(value, dict) or not isinstance(value.get("stages"), dict)
                or set(value["stages"]) != set(STAGES)):
            raise TaskStateError("任务步骤清单损坏。")
        checked = {**value, "stages": {**value["stages"], "report": _stage_template()}}
        self._validate(checked)

    def _load(self, *, review_only: bool = False) -> dict:
        value = _read_json(self.path / _MANIFEST)
        # Tasks created before the independent stage-redo feature predate the
        # per-stage counter.  Normalize that additive field in memory so old
        # tasks remain openable without rewriting their source or checkpoints.
        if isinstance(value, dict) and isinstance(value.get("stages"), dict):
            for info in value["stages"].values():
                if isinstance(info, dict) and "redo_count" not in info:
                    info["redo_count"] = 0
                if isinstance(info, dict) and "reset_pending" not in info:
                    info["reset_pending"] = False
        self._upgrade_pending_selection(value)
        self._validate_review(value) if review_only or self._review_only else self._validate(value)
        self._manifest = value
        return value

    @contextmanager
    def _edit(self, *, wait_seconds: float = 0) -> Iterator[dict]:
        if self._read_only:
            raise TaskStateError("此任务以只读方式打开，不能修改审核状态。")
        with self._thread_lock:
            _task_location(self.path)
            lease = _try_lease(self.path / ".task.lock")
            deadline = time.monotonic() + wait_seconds
            while lease is None and time.monotonic() < deadline:
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
                lease = _try_lease(self.path / ".task.lock")
            if lease is None:
                raise TaskStateError("任务正在更新，请稍后刷新后重试。")
            try:
                original = self._load()
                value = copy.deepcopy(original)
                yield value
                if value != original:
                    value["updated_at"] = _now()
                    value["revision"] = original.get("revision", 0) + 1
                    self._validate(value)
                    _atomic_manifest(self.path / _MANIFEST, value)
                    self._manifest = value
            finally:
                lease.close()

    def refresh(self) -> dict:
        with self._thread_lock:
            return copy.deepcopy(self._load())

    def snapshot(self) -> dict:
        return self.refresh()

    def review_snapshot(self) -> dict:
        """Read a versioned first-four-stage snapshot, without writing files.

        The token covers selection intent and current input/result identities
        and file versions. It excludes report state, report artifacts and raw
        source files. Callers reading checkpoint bodies must compare this
        token again after those reads; publication can use review_read_lease.
        """
        with self._thread_lock:
            before = _file_version(self.path / _MANIFEST)
            value = self._load(review_only=True)
            references = {(stage, value["stages"][stage][field])
                          for stage in REVIEW_STAGES
                          for field in ("input_file", "result_file", "result_input_file")
                          if value["stages"][stage][field] is not None}
            if value.get("import_baseline_file") is not None:
                references.add(("import", value["import_baseline_file"]))
            versions = {reference: _file_version(self._artifact_path(stage, reference))
                        for stage, reference in sorted(references)}
            llm = value["stages"]["llm"]
            runtime = llm.get("runtime_directory", "runtime/llm")
            prompt_reference = llm["stats"].get("review_prompt_file")
            prompt_version = None
            if (isinstance(prompt_reference, str)
                    and re.fullmatch(re.escape(runtime) + r"/review_prompts/[a-f0-9]{64}\.md", prompt_reference)):
                try:
                    prompt_version = _file_version(self.path.joinpath(*PurePosixPath(prompt_reference).parts))
                except TaskStateError:
                    prompt_version = ("unavailable",)
            descriptor = {
                "task_id": value["task_id"], "scope": value["scope_signature"],
                "name": value["name"], "created_at": value["created_at"],
                "baseline_ids": value["baseline_ids"], "baseline_file": value["import_baseline_file"],
                "review_rules": value.get("review_rules"),
                "stages": {stage: stage_selection_version(value["stages"][stage]) for stage in REVIEW_STAGES},
                "invalidations": {stage: value["stages"][stage].get("invalidated_by") for stage in REVIEW_STAGES},
                "files": versions, "runtime": runtime, "prompt": prompt_reference,
                "prompt_version": prompt_version,
            }
            if _file_version(self.path / _MANIFEST) != before:
                raise TaskStateError("审核任务在读取时发生变化，请刷新后重试。")
            return {"manifest": copy.deepcopy(value), "file_versions": versions,
                    "snapshot_token": hashlib.sha256(_encoded(descriptor)).hexdigest()}

    @contextmanager
    def review_read_lease(self, *, expected_snapshot: str | None = None) -> Iterator[dict]:
        """Guard a read/publication against concurrent task selection edits.

        This shares the writer's OS lease but never commits a manifest. Only
        an explicit publication needs the lease; preview is an optimistic read.
        """
        with self._thread_lock:
            lease = _try_lease(self.path / ".task.lock")
            if lease is None:
                raise TaskStateError("任务正在更新，请刷新后重新导出。")
            try:
                snapshot = self.review_snapshot()
                if expected_snapshot is not None and snapshot["snapshot_token"] != expected_snapshot:
                    raise TaskStateError("审核名单或结果已变化，请刷新预览后重新导出。")
                yield snapshot
            finally:
                lease.close()

    def stage_info(self, stage: str) -> dict:
        return self.refresh()["stages"][self._stage(stage)]

    def get_review_rules(self) -> str:
        """Read this task's editable rules, without model or provider access."""
        with self._thread_lock:
            rules = self._load().get("review_rules")
            if rules is not None:
                return rules["text"]
            try:
                return review_rules.default_review_rules()
            except (OSError, UnicodeError, ValueError):
                raise TaskStateError("默认 AI 复核规则不可读取，请检查项目提示词文件。") from None

    def set_review_rules(self, rules: str | None) -> None:
        """Atomically change rules and invalidate only AI/report conclusions.

        Result files, manual selections and JD inputs are retained. No stage
        is executed here, and identical text does not revoke confirmation.
        """
        if rules is not None and (not isinstance(rules, str) or not rules.strip()):
            raise TaskStateError("复核规则不能为空；恢复项目默认请使用恢复默认操作。")
        normalized = rules.replace("\r\n", "\n").replace("\r", "\n").strip() if rules is not None else None
        with self._edit() as value:
            self._no_running(value)
            current = value.get("review_rules")
            if normalized is None:
                if current is None:
                    return
                try:
                    default = review_rules.default_review_rules()
                except (OSError, UnicodeError, ValueError):
                    default = None
                changed = current["text"] != default
                value["review_rules"] = None
            else:
                if current is None:
                    try:
                        previous = review_rules.default_review_rules()
                    except (OSError, UnicodeError, ValueError):
                        previous = None
                else:
                    previous = current["text"]
                if normalized == previous:
                    return
                value["review_rules"] = {"text": normalized, "updated_at": _now()}
                changed = True
            if changed:
                for stage in ("llm", "report"):
                    info = value["stages"][stage]
                    info.update(confirmed=False, confirmed_revision=None, confirmed_at=None)
                    if info["attempt"] or info["result_file"] is not None or info["status"] != "pending":
                        info.update(status="stale", invalidated_by="review_rules")

    def _read_jobs(self, stage: str, relative: str | None) -> list[dict]:
        if relative is None:
            return []
        value = _read_json(self._artifact_path(stage, relative))
        if not isinstance(value, dict) or value.get("stage") != stage or not isinstance(value.get("jobs"), list):
            raise TaskStateError("任务岗位检查点格式无效。")
        return _validated_jobs(value["jobs"])

    def _result_jobs(self, value: dict, stage: str) -> list[dict]:
        info = value["stages"][stage]
        jobs = self._read_jobs(stage, info["result_file"])
        if [job_id(job) for job in jobs] != info["result_ids"]:
            raise TaskStateError("任务结果 ID 与检查点清单不一致。")
        if stage == "llm":
            jobs = [{**job, "recommended_keep": _ai_recommendation(job)} for job in jobs]
        return jobs

    def _baseline_jobs(self, value: dict) -> list[dict]:
        if value["import_baseline_file"] is None:
            return self._result_jobs(value, "import")
        jobs = self._read_jobs("import", value["import_baseline_file"])
        if [job_id(job) for job in jobs] != value["baseline_ids"]:
            raise TaskStateError("导入基线岗位与固定 ID 清单不一致。")
        return jobs

    def _write_jobs(self, stage: str, kind: str, jobs: list[dict]) -> str:
        relative = f"stages/{stage}/{kind}-{uuid.uuid4().hex}.json"
        path = self._artifact_path(stage, relative)
        data = _encoded({"stage": stage, "jobs": jobs})
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _no_links(path)
            # Fresh file only.  It is not reachable until the manifest commits.
            with path.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            raise TaskStateError("阶段检查点保存失败；已有结果未被覆盖。") from None
        return relative

    def _input_jobs(self, value: dict, stage: str, *, required: bool = False) -> list[dict]:
        if stage == "import":
            return []
        index = STAGES.index(stage)
        for previous in STAGES[:index]:
            info = value["stages"][previous]
            if info["status"] != "completed" or not info["confirmed"]:
                if required:
                    raise TaskStateError("请先完成并确认所有前置步骤；失效结果需要重跑。")
                return []
        previous = STAGES[index - 1]
        info = value["stages"][previous]
        keep = set(info["keep_ids"])
        return [job for job in self._result_jobs(value, previous) if job_id(job) in keep]

    def input_jobs(self, stage: str) -> list[dict]:
        stage = self._stage(stage)
        with self._thread_lock:
            return copy.deepcopy(self._input_jobs(self._load(), stage))

    def jobs(self, stage: str, include_excluded: bool = True) -> list[dict]:
        stage = self._stage(stage)
        with self._thread_lock:
            value = self._load()
            jobs = self._result_jobs(value, stage)
            if not include_excluded:
                keep = set(value["stages"][stage]["keep_ids"])
                jobs = [job for job in jobs if job_id(job) in keep]
            return copy.deepcopy(jobs)

    def all_jobs(self, *, through_stage: str | None = None) -> list[dict]:
        stages = STAGES if through_stage is None else STAGES[:STAGES.index(self._stage(through_stage)) + 1]
        with self._thread_lock:
            value = self._load() if through_stage is None else self._load(review_only=stages[-1] != "report")
            jobs = self._baseline_jobs(value)
            by_id = {job_id(job): _merge_job({}, job) for job in jobs}
            for stage in stages:
                info = value["stages"][stage]
                if info["result_file"] is None:
                    continue
                available = (info["status"] not in {"pending", "running", "stale"}
                             and (stage == "import" or (info["result_input_ids"] == info["input_ids"]
                                  and info["result_input_revision"] == info["input_revision"])))
                for job in self._result_jobs(value, stage):
                    key = job_id(job)
                    if key in by_id:
                        # Provenance stays auditable even when old JD/model
                        # evidence is stale and cannot be used by a new report.
                        enhancement = job if available else {field: job[field] for field in (
                            "observed_in", "sources", "source_files", "source_context",
                            "source_file", "source_row", "source_line_number") if field in job}
                        by_id[key] = _merge_job(by_id[key], enhancement)
            # These collections stay ordered in the manifest. Index them once
            # for the per-job audit instead of repeatedly scanning every list.
            stage_ids = {
                stage: {field: set(info[field]) for field in (
                    "input_ids", "result_ids", "keep_ids", "recommended_keep_ids", "recommended_exclude_ids",
                    "excluded_ids", "pending_ids")}
                for stage, info in value["stages"].items() if stage in stages
            }
            for key, job in by_id.items():
                job["excluded_at"] = None
                audit = {}
                for stage in stages:
                    info = value["stages"][stage]
                    ids = stage_ids[stage]
                    present = key in ids["result_ids"]
                    current = present and (stage == "import" or (
                        info["result_input_ids"] == info["input_ids"]
                        and info["result_input_revision"] == info["input_revision"]))
                    confirmed = (current and info["confirmed"] and info["status"] == "completed"
                                 and info["confirmed_revision"] == info["revision"])
                    audit[stage] = {
                        "status": info["status"], "in_input": key in ids["input_ids"] or (stage == "import" and present),
                        "has_result": present,
                        "keep": (None if key in ids["pending_ids"] else key in ids["keep_ids"]) if present else None,
                        "recommended_keep": ((True if key in ids["recommended_keep_ids"] else
                                              False if key in ids["recommended_exclude_ids"] else None)
                                             if stage == "llm" else
                                             key in ids["recommended_keep_ids"] if present else None) if present else None,
                        "pending": key in ids["pending_ids"] if present else None,
                        "manual_keep": info["manual_choices"].get(key), "confirmed": bool(confirmed),
                    }
                    if job["excluded_at"] is None and confirmed and key in ids["excluded_ids"]:
                        job["excluded_at"] = stage
                job["review_selection"] = audit
            return copy.deepcopy(list(by_id.values()))

    def run_directory(self, stage: str) -> Path:
        stage = self._stage(stage)
        with self._thread_lock:
            info = self._load()["stages"][stage]
            path = self._runtime_path(stage, info.get("runtime_directory", f"runtime/{stage}"))
            try:
                path.mkdir(parents=True, exist_ok=True)
                for _file in _walk_ordinary(path):
                    pass
            except OSError:
                raise TaskStateError("无法创建安全的任务运行目录。") from None
            return path

    def _redo_cleanup_targets(self, stage: str) -> tuple[list[Path], list[Path]]:
        """Validate the narrowly scoped files/directories owned by one stage.

        Callers remove them only after the manifest no longer references old
        checkpoints. No raw source, other stage, or task-wide log is included.
        """
        runtime_root = self.path / "runtime"
        _task_location(runtime_root)
        _no_links(runtime_root)
        runtimes = []
        pattern = re.compile(rf"{re.escape(stage)}(?:-redo-[a-f0-9]{{32}})?\Z")
        for candidate in (runtime_root / stage, *runtime_root.glob(f"{stage}-redo-*")):
            if not pattern.fullmatch(candidate.name):
                raise TaskStateError("阶段运行目录名称不符合清理白名单；为避免误删，已拒绝重做。")
            _task_location(candidate)
            try:
                candidate.resolve().relative_to(self.path.resolve())
            except (OSError, ValueError, RuntimeError):
                raise TaskStateError("重做清理目录不能指向任务目录外部。") from None
            if candidate.exists():
                for _file in _walk_ordinary(candidate):
                    pass
                runtimes.append(candidate)

        artifacts = []
        stage_dir = self.path / "stages" / stage
        _no_links(stage_dir)
        if stage_dir.exists():
            for _file in _walk_ordinary(stage_dir):
                pass
            for path in stage_dir.iterdir():
                _no_links(path)
                if path.is_file() and re.fullmatch(r"(?:input|results)-[a-f0-9]{32}\.json", path.name):
                    artifacts.append(path)
                else:
                    raise TaskStateError("阶段检查点目录含未识别内容；为避免误删，已拒绝重做。")
        return runtimes, artifacts

    def _remove_redone_outputs(self, stage: str, runtimes: list[Path], artifacts: list[Path],
                               *, keep_artifact: str | None) -> None:
        """Delete only allowlisted outputs after the clean manifest commits."""
        keep_path = self._artifact_path(stage, keep_artifact) if keep_artifact else None
        try:
            for candidate in runtimes:
                if keep_path is not None and candidate == keep_path.parent:
                    continue
                _task_location(candidate)
                _no_links(candidate)
                for _file in _walk_ordinary(candidate):
                    pass
                if candidate.exists():
                    shutil.rmtree(candidate)
            for path in artifacts:
                if path == keep_path:
                    continue
                relative = path.relative_to(self.path).as_posix()
                if not any(re.fullmatch(rf"stages/{re.escape(stage)}/{kind}-[a-f0-9]{{32}}\.json", relative)
                           for kind in ("input", "results")):
                    raise TaskStateError("检查点路径未通过阶段清理白名单校验。")
                _no_links(path)
                path.unlink(missing_ok=True)
            if stage == "import":
                sample = self.path / "raw_sample.json"
                _no_links(sample)
                if sample.is_file():
                    sample.unlink()
        except (OSError, TaskStateError):
            raise TaskStateError("旧的本阶段结果未能完整清理；本步已停在失败状态，请检查任务目录后重试重做。") from None

    def _no_running(self, value: dict) -> None:
        if any(info["status"] == "running" for info in value["stages"].values()):
            raise TaskStateError("任务正在运行；请先暂停或停止，再编辑名单或确认。")

    def _invalidate_downstream(self, value: dict, stage: str) -> None:
        for later in STAGES[STAGES.index(stage) + 1:]:
            info = value["stages"][later]
            info["confirmed"] = False
            info["confirmed_revision"] = None
            info["confirmed_at"] = None
            info["invalidated_by"] = stage
            if info["attempt"] or info["result_file"] is not None or info["status"] != "pending":
                info["status"] = "stale"
            else:
                info["input_ids"], info["input_file"], info["input_revision"] = [], None, None
                info["progress"] = {"done": 0, "total": 0}

    def reset_stage(self, stage: str, *, before_reset: Callable[[], None] | None = None) -> None:
        """Delete one stage's outputs and checkpoints without starting it.

        The next ordinary ``begin(stage)`` consumes ``reset_pending`` and
        starts from an empty checkpoint. Upstream results remain intact and
        downstream results are marked stale, not deleted.
        """
        stage = self._stage(stage)
        run_lease: _Lease | None = None
        fresh_runtime: Path | None = None
        committed = False
        try:
            with self._edit() as value:
                self._no_running(value)
                self._input_jobs(value, stage, required=True)
                if stage == "import":
                    self._verify_source(value)
                info = value["stages"][stage]
                if (not info["attempt"] and info["result_file"] is None
                        and info["status"] == "pending" and not info.get("reset_pending")):
                    raise TaskStateError("本阶段尚无可清理的运行记录。")
                run_lease = _try_lease(self.path / ".running.lock")
                if run_lease is None:
                    raise TaskStateError("此任务已有活跃执行者，不能重置本阶段。")
                if before_reset is not None:
                    before_reset()

                runtimes, artifacts = self._redo_cleanup_targets(stage)
                relative = f"runtime/{stage}-redo-{uuid.uuid4().hex}"
                fresh_runtime = self._runtime_path(stage, relative)
                try:
                    fresh_runtime.mkdir(parents=True, exist_ok=False)
                except OSError:
                    raise TaskStateError("无法准备空白运行目录；现有结果和任务状态保持不变。") from None

                previous_redo_count = info.get("redo_count", 0)
                info.update(
                    status="pending", confirmed=False, confirmed_revision=None,
                    input_ids=[], input_file=None, input_revision=None,
                    result_file=None, result_ids=[], keep_ids=[], excluded_ids=[], pending_ids=[],
                    recommended_keep_ids=[], recommended_exclude_ids=[], result_input_file=None,
                    result_input_revision=None, result_input_ids=[], manual_choices={},
                    progress={"done": 0, "total": 0}, stats={}, issues=[], history=[],
                    attempt=0, redo_count=previous_redo_count + 1,
                    reset_pending=True, runtime_directory=relative,
                    started_at=None, finished_at=None, confirmed_at=None,
                    invalidated_by=None,
                )
                if stage == "import":
                    value["baseline_ids"] = None
                    value["import_baseline_file"] = None
                self._invalidate_downstream(value, stage)
            committed = True

            try:
                self._remove_redone_outputs(stage, runtimes, artifacts, keep_artifact=None)
            except TaskStateError:
                with self._edit() as value:
                    failed = value["stages"][stage]
                    failed.update(status="failed", issues=["旧的本阶段结果未能完整清理；请检查任务目录后重试重置。"])
                raise
        finally:
            if not committed and fresh_runtime is not None:
                try:
                    _no_links(fresh_runtime)
                    if fresh_runtime.exists():
                        shutil.rmtree(fresh_runtime)
                except OSError:
                    pass
            if run_lease is not None:
                run_lease.close()

    def _verify_source(self, value: dict) -> None:
        if _source_inventory(Path(value["source_root"])) != value["source_inventory"]:
            raise TaskStateError("固定原始数据已改变；已有任务不会悄悄导入新数据，请新建任务。")
        if value["config_path"] is not None:
            path = Path(value["config_path"])
            _no_links(path)
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                raise TaskStateError("固定任务配置已缺失或不可读取。") from None
            if digest != value["config_sha256"]:
                raise TaskStateError("固定任务配置已改变，请新建任务。")

    def begin(self, stage: str, *, redo: bool = False,
              before_start: Callable[[], None] | None = None) -> None:
        """Start or resume a step's current checkpoint round.

        Resetting is a separate, non-executing operation. After an explicit
        ``reset_stage`` call, this ordinary begin consumes its fresh-round
        marker; otherwise interrupted steps resume their existing checkpoint.
        """
        stage = self._stage(stage)
        if type(redo) is not bool or redo:
            raise TaskStateError("重置与运行已分开；请先调用 reset_stage(stage)，再单独运行本步。")
        acquired = None
        try:
            with self._edit() as value:
                self._no_running(value)
                inputs = self._input_jobs(value, stage, required=True)
                if stage == "import":
                    self._verify_source(value)
                acquired = _try_lease(self.path / ".running.lock")
                if acquired is None:
                    raise TaskStateError("此任务已有活跃执行者，不能重复运行。")
                info = value["stages"][stage]
                if before_start is not None:
                    before_start()
                ids = [job_id(job) for job in inputs]
                input_revision = value["stages"][STAGES[STAGES.index(stage) - 1]]["revision"] if stage != "import" else None
                fresh_round = info.get("reset_pending", False)
                resume = (not fresh_round and info["status"] in _INTERRUPT_STATUSES and info["input_ids"] == ids
                          and info["input_revision"] == input_revision)
                if stage == "llm" and fresh_round:
                    saved_rules = value.get("review_rules")
                    if (isinstance(saved_rules, dict)
                            and review_rules.is_legacy_complex_review_rules(saved_rules.get("text", ""))):
                        try:
                            review_rules.default_review_rules()
                        except (OSError, UnicodeError, ValueError, RuntimeError):
                            raise TaskStateError("当前简化复核规则不可读取；原有规则与结果保持不变。") from None
                        value["review_rules"] = None
                info["input_file"] = self._write_jobs(stage, "input", inputs)
                info["input_ids"], info["input_revision"] = ids, input_revision
                if not resume:
                    info["progress"] = {"done": 0, "total": len(inputs)}
                    info["stats"], info["issues"] = {}, []
                info.update(status="running", confirmed=False, confirmed_revision=None,
                            confirmed_at=None, started_at=_now(), finished_at=None,
                            invalidated_by=None, attempt=info["attempt"] + 1,
                            reset_pending=False)
                self._invalidate_downstream(value, stage)
            self._run_lease, self._running_stage = acquired, stage
            acquired = None
        finally:
            if acquired is not None:
                acquired.close()

    def _owned_run(self, value: dict, stage: str) -> dict:
        info = value["stages"][stage]
        if (info["status"] != "running" or self._run_lease is None
                or self._run_lease.handle is None or self._running_stage != stage):
            raise TaskStateError("仅当前步骤的活跃执行者可以提交运行结果或进度。")
        if stage != "import":
            inputs = self._input_jobs(value, stage, required=True)
            previous = value["stages"][STAGES[STAGES.index(stage) - 1]]
            if ([job_id(job) for job in inputs] != info["input_ids"]
                    or previous["revision"] != info["input_revision"]):
                raise TaskStateError("运行输入已失效，请重跑本步。")
        return info

    def _release_run(self) -> None:
        if self._run_release_deferred:
            return
        lease, self._run_lease = self._run_lease, None
        self._running_stage = None
        if lease is not None:
            lease.close()

    def finish(self, stage: str, jobs: list[dict], stats: dict | None = None,
               status: str = "completed", issues: list | None = None) -> None:
        stage = self._stage(stage)
        if not isinstance(status, str) or status not in _FINISH_STATUSES:
            raise TaskStateError("阶段完成状态无效；暂停 / 验证 / 失败不能冒充已完成。")
        outputs, metadata, problems = _validated_jobs(jobs), _metadata(stats), _issues(issues)
        with self._edit(wait_seconds=1.5) as value:
            info = self._owned_run(value, stage)
            output_by_id = {job_id(job): job for job in outputs}
            expected = value.get("baseline_ids") if stage == "import" else info["input_ids"]
            if expected is not None and set(output_by_id) != set(expected):
                raise TaskStateError("阶段结果 ID 必须与固定输入完全一致，不能新增或漏掉岗位。")
            if stage == "import":
                self._verify_source(value)
                previous = self._baseline_jobs(value)
                base = {job_id(job): job for job in previous}
            else:
                base = {job_id(job): job for job in self._input_jobs(value, stage, required=True)}
            order = expected if expected is not None else list(output_by_id)
            merged = [_merge_job(base.get(key, {}), output_by_id[key]) for key in order]
            if stage in {"import", "report"}:
                recommended = list(order)
            elif stage == "shortlist":
                recommended = [key for key in order if output_by_id[key].get("list_pass") is True]
            elif stage == "llm":
                for job in merged:
                    source = output_by_id[job_id(job)]
                    explicit_projection = ("llm_review" in source
                                           or source.get("llm_stage_projection") == AI_STAGE_PROJECTION)
                    job["recommended_keep"] = _ai_recommendation(job) if explicit_projection else None
                recommended = [job_id(job) for job in merged if job["recommended_keep"] is True]
            else:
                recommended = [key for key in order if output_by_id[key].get("recommended_keep", True) is not False]
            recommended_exclude = ([job_id(job) for job in merged if job["recommended_keep"] is False]
                                   if stage == "llm" else [])
            if info["result_file"] is not None:
                info["history"].append({"result_file": info["result_file"],
                                        "input_file": info["result_input_file"], "revision": info["revision"],
                                        "runtime_directory": info.get("runtime_directory", f"runtime/{stage}")})
            relative = self._write_jobs(stage, "results", merged)
            info.update(status=status, confirmed=False, confirmed_revision=None,
                        result_file=relative, result_ids=list(order), recommended_keep_ids=recommended,
                        recommended_exclude_ids=recommended_exclude, pending_ids=[],
                        result_input_file=info["input_file"],
                        result_input_ids=list(info["input_ids"]), result_input_revision=info["input_revision"],
                        revision=info["revision"] + 1, finished_at=_now(), invalidated_by=None)
            self._select(info, stage, merged)
            info["stats"].update(metadata)
            info["issues"] = problems
            if status == "completed":
                info["progress"] = {"done": len(merged), "total": len(merged)}
            if stage == "import" and status == "completed" and value.get("baseline_ids") is None:
                value["baseline_ids"], value["import_baseline_file"] = list(order), relative
            self._invalidate_downstream(value, stage)
        self._release_run()

    def interrupt(self, stage: str, status: str = "paused", issue: str | None = None,
                  stats: dict | None = None) -> None:
        stage = self._stage(stage)
        if not isinstance(status, str) or status not in _INTERRUPT_STATUSES:
            raise TaskStateError("中断状态只能是 paused / stopped / waiting / failed。")
        metadata = _metadata(stats)
        if issue is not None and not isinstance(issue, str):
            raise TaskStateError("中断原因必须是文字。")
        with self._edit(wait_seconds=1.5) as value:
            info = self._owned_run(value, stage)
            info.update(status=status, confirmed=False, confirmed_revision=None, finished_at=_now())
            info["stats"].update(metadata)
            if issue:
                info["issues"].append(_redact_text(issue))
        self._release_run()

    def _select(self, info: dict, stage: str, jobs: list[dict] | None = None) -> None:
        recommended = set(info["recommended_keep_ids"])
        recommended_exclude = set(info.get("recommended_exclude_ids", []))
        manual = info["manual_choices"]
        if stage == "llm":
            keep, excluded, pending = [], [], []
            for key in info["result_ids"]:
                if key in manual:
                    (keep if manual[key] else excluded).append(key)
                elif key in recommended:
                    keep.append(key)
                elif key in recommended_exclude:
                    excluded.append(key)
                else:
                    pending.append(key)
            info["keep_ids"], info["excluded_ids"], info["pending_ids"] = keep, excluded, pending
            return
        info["keep_ids"] = [key for key in info["result_ids"] if manual.get(key, key in recommended)]
        keep = set(info["keep_ids"])
        info["excluded_ids"] = [key for key in info["result_ids"] if key not in keep]
        info["pending_ids"] = []

    def _editable(self, value: dict, stage: str) -> dict:
        if stage == "report":
            raise TaskStateError("报告页只读；请返回前面的审核步骤修改名单，再重新生成报告。")
        self._no_running(value)
        info = value["stages"][stage]
        if info["status"] not in _EDITABLE_STATUSES or info["result_file"] is None:
            raise TaskStateError("请先运行本步；失效结果重跑后才能编辑名单。")
        if stage != "import" and (info["result_input_ids"] != info["input_ids"]
                                  or info["result_input_revision"] != info["input_revision"]):
            raise TaskStateError("旧结果不匹配当前输入；重跑本步后才能编辑名单。")
        return info

    def _selection_changed(self, value: dict, stage: str,
                           before: tuple[list[str], list[str], list[str]],
                           manual_before: dict[str, bool] | None = None) -> None:
        info = value["stages"][stage]
        self._select(info, stage, self._result_jobs(value, stage))
        after = (info["keep_ids"], info["excluded_ids"], info["pending_ids"])
        if after != before or (stage == "llm" and manual_before is not None
                               and manual_before != info["manual_choices"]):
            info.update(confirmed=False, confirmed_revision=None, confirmed_at=None, revision=info["revision"] + 1)
            self._invalidate_downstream(value, stage)

    def set_keep(self, stage: str, job_ids: list[str], keep: bool, *,
                 replace_selection: bool = False, expected_stage: dict | None = None) -> None:
        """Save a manual edit atomically, optionally replacing the full scope.

        ``expected_stage`` is the already-reviewed stage_info snapshot. Its
        selection token is checked while holding the same edit lease as the
        write. Replacement explicitly keeps the given IDs and removes every
        other current result ID in one commit, preserving historical choices
        outside that result scope; ordinary three-argument edits retain their
        additive behavior.
        """
        stage = self._stage(stage)
        if not isinstance(job_ids, list) or any(not isinstance(key, str) or not key for key in job_ids) or type(keep) is not bool:
            raise TaskStateError("人工选择需要岗位 ID 列表和明确的保留 / 剔除值。")
        if type(replace_selection) is not bool or (replace_selection and keep is not True):
            raise TaskStateError("替换完整人工名单须明确保留给定岗位，其他岗位将移出。")
        expected_version = stage_selection_version(expected_stage) if expected_stage is not None else None
        with self._edit() as value:
            info = self._editable(value, stage)
            if expected_version is not None and stage_selection_version(info) != expected_version:
                raise TaskStateError("审核名单或结果已被其他窗口修改，请刷新并重新审核后重试。")
            if not set(job_ids) <= set(info["result_ids"]):
                raise TaskStateError("人工选择包含不属于本步结果的岗位。")
            before = (list(info["keep_ids"]), list(info["excluded_ids"]), list(info["pending_ids"]))
            manual_before = dict(info["manual_choices"])
            if replace_selection:
                selected = set(job_ids)
                info["manual_choices"].update({key: key in selected for key in info["result_ids"]})
            else:
                info["manual_choices"].update({key: keep for key in job_ids})
            self._selection_changed(value, stage, before, manual_before)

    def apply_recommendations(self, stage: str) -> None:
        stage = self._stage(stage)
        with self._edit() as value:
            info = self._editable(value, stage)
            before = (list(info["keep_ids"]), list(info["excluded_ids"]), list(info["pending_ids"]))
            manual_before = dict(info["manual_choices"])
            info["manual_choices"] = {}
            self._selection_changed(value, stage, before, manual_before)

    def confirm(self, stage: str, *, expected_stage: dict | None = None) -> list[str]:
        stage = self._stage(stage)
        expected_version = stage_selection_version(expected_stage) if expected_stage is not None else None
        with self._edit() as value:
            self._no_running(value)
            info = value["stages"][stage]
            if expected_version is not None and stage_selection_version(info) != expected_version:
                raise TaskStateError("审核名单或结果已被其他窗口修改，请刷新并重新审核后重试。")
            if info["status"] != "completed" or info["result_file"] is None:
                raise TaskStateError("只有本步有效完成后才能确认；未运行、暂停或失效结果不可确认。")
            inputs = self._input_jobs(value, stage, required=True)
            if stage != "import":
                previous = value["stages"][STAGES[STAGES.index(stage) - 1]]
                if ([job_id(job) for job in inputs] != info["input_ids"]
                        or previous["revision"] != info["input_revision"]):
                    raise TaskStateError("输入名单已变化，请重跑本步后再确认。")
            self._result_jobs(value, stage)
            keep_ids = list(info["keep_ids"])
            if not info["confirmed"]:
                info.update(confirmed=True, confirmed_revision=info["revision"], confirmed_at=_now())
                index = STAGES.index(stage)
                if index + 1 < len(STAGES):
                    next_stage = STAGES[index + 1]
                    next_info = value["stages"][next_stage]
                    selected = [job for job in self._result_jobs(value, stage) if job_id(job) in set(keep_ids)]
                    next_info["input_file"] = self._write_jobs(next_stage, "input", selected)
                    next_info["input_ids"] = keep_ids
                    next_info["input_revision"] = info["revision"]
                    if next_info["status"] == "pending":
                        next_info["progress"] = {"done": 0, "total": len(keep_ids)}
        return list(keep_ids)

    def record_progress(self, stage: str, done: int, total: int,
                        stats: dict | None = None, message: str | None = None) -> None:
        stage = self._stage(stage)
        if type(done) is not int or type(total) is not int or not 0 <= done <= total:
            raise TaskStateError("进度必须满足 0 ≤ 已完成 ≤ 总数。")
        if message is not None and not isinstance(message, str):
            raise TaskStateError("进度说明必须是文字。")
        metadata = _metadata(stats)
        with self._edit() as value:
            info = self._owned_run(value, stage)
            info["progress"] = {"done": done, "total": total}
            info["stats"].update(metadata)
            if message is not None:
                info["progress"]["message"] = _redact_text(message)
