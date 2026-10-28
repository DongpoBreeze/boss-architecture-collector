#!/usr/bin/env python3
"""Portable, resumable BOSS architecture-job collection orchestrator.

This project owns orchestration and metadata only.  The actual browser/CDP
collector is the upstream ``boss-zhipin-scraper`` project and must use the
user's own logged-in browser. Security checks pause the current combination
and wait for explicit human confirmation before retrying; this script never
attempts to bypass them.
"""

from __future__ import annotations

import argparse
import builtins
from contextlib import contextmanager
import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from collection_plan import filter_plan_specs, validate_plan_config
from project_resources import DEFAULT_CONFIG, PROJECT_ROOT, default_state_root


SECURITY_PATTERNS = (
    r"security_check",
    r"_security_check",
    r"安全验证",
    r"人工验证",
    r"验证码",
    r"登录状态异常",
    r"登录探测响应异常",
    r"未检测到\s*(?:BOSS直聘\s*)?(?:可用)?登录(?:状态|态)",
    r"BOSS\s*接口返回限制状态",
    r"detail login expired",
)
UPSTREAM_PROGRESS_PATTERNS = (
    r"\[\d+\s*/\s*\d+\s*页",
    r"本页\s*\d+\s*条",
    r"累计\s*\d+",
    r"翻页等待",
    r"已到最后一页",
    r"完成\s*[:：]",
    r"已保存\s*[:：]",
    r"合并(?:结果|详情)?(?:已保存)?",
    r"无数据",
)
UPSTREAM_ERROR_PATTERNS = (
    r"❌",
    r"⚠",
    r"错误",
    r"异常",
    r"失败",
    r"中断",
)

# Keep diagnostic output useful while ensuring collection logs and the desktop
# console do not persist credentials accidentally echoed by an upstream tool.
# Raw output remains in memory only for security/login gate detection.
_LOG_SECRET_RE = re.compile(
    r"(?ix)(?P<prefix>"
    r"\b(?:authorization|proxy-authorization)\s*:\s*bearer\s+"
    r"|\bbearer\s+"
    r"|\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|password|passwd|secret|token)\b[\"']?\s*[:=]\s*[\"']?"
    r")(?P<secret>[^\"'\s,;}]+)"
)
_LOG_COOKIE_RE = re.compile(r"(?im)(?P<prefix>\b(?:cookie|set-cookie)\s*:\s*)[^\r\n]+")


def redact_log(text_value: str) -> str:
    """Redact common credential/cookie fields from user-visible logs."""

    redacted = _LOG_SECRET_RE.sub(lambda match: f"{match.group('prefix')}[REDACTED]", text_value)
    return _LOG_COOKIE_RE.sub(lambda match: f"{match.group('prefix')}[REDACTED]", redacted)

# Keep a module-level alias so tests can inject ``input`` while real
# non-interactive runs can exit without blocking on a redirected stdin.
input = builtins.input
_ORIGINAL_INPUT = input
GUI_INTERACTIVE_MODE = False

DEFAULT_CDP_PORT = 9222
DEFAULT_PROFILE_ROOT = Path.home() / ".boss-zhipin-scraper"
MANIFEST_LOCK_TIMEOUT = 180
MANIFEST_LOCK_STALE_AFTER = 6 * 60 * 60

def now_iso() -> str:
    """Return a local timezone ISO timestamp suitable for manifests."""

    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_root() -> Path:
    return default_state_root() / "job-result" / f"{datetime.now():%Y-%m-%d}-architecture-screen-full"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON atomically so an interrupted run does not corrupt a manifest."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


@contextmanager
def manifest_lock(manifest_path: Path):
    """Serialize cross-process manifest updates with a small lock file."""

    lock_path = manifest_path.with_name(manifest_path.name + ".lock")
    deadline = time.monotonic() + MANIFEST_LOCK_TIMEOUT
    acquired = False
    while not acquired:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()}\ncreated_at={now_iso()}\n")
            acquired = True
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > MANIFEST_LOCK_STALE_AFTER
            except OSError:
                stale = False
            if stale:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"manifest lock timeout: {lock_path}")
            time.sleep(0.2)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def persist_manifest_record(
    manifest_path: Path,
    record: dict[str, Any],
    *,
    expected_specs: list[dict[str, Any]] | None = None,
    pages: int | None = None,
    root: Path | None = None,
    local_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge one record into the latest manifest under an interprocess lock."""

    with manifest_lock(manifest_path):
        current = load_manifest(manifest_path)
        if expected_specs is not None and pages is not None and root is not None:
            prepare_manifest(current, expected_specs, pages, root)
        save_record(current, record)
        write_json_atomic(manifest_path, current)
    if local_manifest is not None:
        local_manifest.clear()
        local_manifest.update(current)
    return current


def persist_manifest_state(
    manifest_path: Path,
    *,
    specs: list[dict[str, Any]],
    pages: int,
    root: Path,
) -> dict[str, Any]:
    """Initialize or refresh manifest metadata without losing another shard."""

    from collection_sources import assert_source_specs_compatible

    with manifest_lock(manifest_path):
        assert_source_specs_compatible(root, specs)
        manifest = load_manifest(manifest_path)
        prepare_manifest(manifest, specs, pages, root)
        write_json_atomic(manifest_path, manifest)
    return manifest


def load_config(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"配置必须是 JSON 对象：{path}")
    for key in ("cities", "salaries", "current", "target"):
        if key not in payload:
            raise ValueError(f"配置缺少字段 {key!r}：{path}")
    return validate_plan_config(payload)


def _track_specs(config: dict[str, Any], track: str) -> Iterable[dict[str, Any]]:
    track_config = config[track]
    for city in config["cities"]:
        for keyword in track_config["keywords"]:
            for salary in config["salaries"]:
                for experience in track_config["experiences"]:
                    combination = "__".join(
                        (
                            track,
                            str(city["id"]),
                            str(keyword["id"]),
                            str(salary["id"]),
                            str(experience["id"]),
                        )
                    )
                    yield {
                        "combination": combination,
                        "track": track,
                        "city": city["name"],
                        "city_id": city["id"],
                        "keyword": keyword["query"],
                        "keyword_id": keyword["id"],
                        "salary": salary["id"],
                        "salary_code": str(salary["code"]),
                        "experience": experience["id"],
                        "experience_code": str(experience["code"]),
                        "degree": str(config.get("degree", {}).get("id", "203")),
                    }


def build_specs(
    config: dict[str, Any],
    batch: str = "all",
    limit: int | None = None,
    cities: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Expand selected groups, then apply CLI track/city filters and limit.

    Catalog entries remain available for recognizing historical source files;
    collection_selection alone decides which of them will be collected now.
    """

    if limit is not None and limit < 1:
        raise ValueError("--limit 必须是正整数")
    if batch not in {"all", "current", "target"}:
        raise ValueError("采集轨道必须是 all、current 或 target")
    config = validate_plan_config(config)
    selected_specs = filter_plan_specs(config, [
        spec for track in ("current", "target") for spec in _track_specs(config, track)
    ])
    selected_cities = {str(value).strip().lower() for value in cities or () if str(value).strip()}
    specs = [
        spec
        for spec in selected_specs
        if (batch == "all" or spec["track"] == batch)
        and (
            not selected_cities
            or spec["city"].lower() in selected_cities
            or str(spec["city_id"]).lower() in selected_cities
        )
    ]
    combinations = [spec["combination"] for spec in specs]
    if len(combinations) != len(set(combinations)):
        raise ValueError("配置生成了重复 combination/job source")
    if limit is not None:
        specs = specs[:limit]
    return specs


def shard_specs(specs: list[dict[str, Any]], shard: int, total_shards: int) -> list[dict[str, Any]]:
    """Deterministically split combinations across account/browser shards."""

    if total_shards < 1:
        raise ValueError("--total-shards 必须 >= 1")
    if not 0 <= shard < total_shards:
        raise ValueError(f"--shard 必须在 0..{total_shards - 1}")
    if total_shards == 1:
        return list(specs)
    return [
        spec
        for index, spec in enumerate(specs)
        if index % total_shards == shard
    ]


def valid_payload(path: Path) -> bool:
    return payload_job_count(path) is not None


def payload_job_count(path: Path) -> int | None:
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    return len(jobs) if isinstance(jobs, list) else None


def security_detected(text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in SECURITY_PATTERNS)


def empty_result_failure_reason(text: str) -> str | None:
    """Explain why a zero-job result cannot be trusted as a genuine empty search.

    The upstream collector also emits its missing-job-data warning after a
    valid empty jobList response. Its explicit empty-response message makes
    that warning benign, but cannot excuse a missing search response or a
    response parsing failure. Return fixed messages, never raw log contents.
    """

    reasons = []
    if re.search(r"未捕获到\s*(?:页面自身的\s*)?搜索响应", text):
        reasons.append("搜索响应未捕获")
    if re.search(
        r"joblist[^\r\n]*(?:不是有效\s*JSON|解析(?:失败|异常|错误)|JSONDecodeError)"
        r"|JSON\s*解析(?:失败|异常|错误)",
        text,
        flags=re.IGNORECASE,
    ):
        reasons.append("职位响应 JSON 解析失败")
    confirmed_empty_response = bool(
        re.search(r"探测样本没有职位[^\r\n]*继续执行实际职位搜索", text)
    )
    if (
        re.search(r"未捕获到\s*(?:API\s*)?职位数据", text, flags=re.IGNORECASE)
        and not confirmed_empty_response
    ):
        reasons.append("职位数据未捕获")
    if not reasons:
        return None
    return "空结果无法确认为无岗位：" + "；".join(reasons) + "；可重新运行当前组合重试"


def is_job_listing_line(line: str) -> bool:
    """Identify the verbose one-line job rows printed by the upstream scraper."""

    stripped = line.strip()
    if not stripped or "|" not in stripped:
        return False
    if re.match(r"^[✓√☑]\s*(?:NEW\s*|🆕\s*)?", stripped, flags=re.IGNORECASE):
        return True
    return bool(re.match(r"^(?:\[?NEW\]?\s+|🆕\s+)", stripped, flags=re.IGNORECASE))


def should_display_upstream_line(line: str) -> bool:
    """Keep progress/errors visible when the terminal is in quiet mode."""

    if not line.strip() or is_job_listing_line(line):
        return False
    if security_detected(line):
        return True
    if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in UPSTREAM_PROGRESS_PATTERNS):
        return True
    return any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in UPSTREAM_ERROR_PATTERNS)


def page_progress(text: str) -> tuple[int, int]:
    matches = re.findall(r"\[(\d+)\s*/\s*(\d+)\s*页", text)
    if not matches:
        return 0, 0
    return max(int(page) for page, _ in matches), len(matches)


def resolve_scraper_root(value: str | None) -> Path:
    candidates: list[Path] = []
    if value:
        candidates.append(Path(value).expanduser())
    environment_value = os.environ.get("BOSS_SCRAPER_ROOT")
    if environment_value:
        candidates.append(Path(environment_value).expanduser())
    candidates.extend(
        (
            PROJECT_ROOT / "boss-zhipin-scraper",
            PROJECT_ROOT.parent / "boss-zhipin-scraper",
            Path.cwd() / "boss-zhipin-scraper",
        )
    )
    for candidate in candidates:
        root = candidate.resolve()
        if (root / "scripts" / "boss_cdp_raw.py").is_file():
            return root
    searched = "\n".join(f"  - {path}" for path in candidates)
    raise FileNotFoundError(
        "找不到上游 boss-zhipin-scraper。请用 --scraper-root 指定其目录，或设置 BOSS_SCRAPER_ROOT。\n"
        + searched
    )


def resolve_python(scraper_root: Path, value: str | None) -> Path:
    if value:
        executable = Path(value).expanduser()
        if executable.is_file():
            return executable.resolve()
        raise FileNotFoundError(f"指定的 Python 不存在：{executable}")
    environment_value = os.environ.get("BOSS_SCRAPER_PYTHON")
    if environment_value:
        executable = Path(environment_value).expanduser()
        if executable.is_file():
            return executable.resolve()
    virtualenv_names = ("Scripts/python.exe", "bin/python")
    for relative in virtualenv_names:
        executable = scraper_root / ".venv" / relative
        if executable.is_file():
            return executable.resolve()
    return Path(sys.executable).resolve()


def load_manifest(path: Path) -> dict[str, Any]:
    if path.is_file():
        try:
            payload = read_json(path)
            if isinstance(payload, dict):
                payload.setdefault("records", [])
                return payload
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            backup = path.with_suffix(path.suffix + ".corrupt")
            shutil.copy2(path, backup)
    return {"schema_version": "1.0", "created_at": now_iso(), "records": []}


def prepare_manifest(manifest: dict[str, Any], specs: list[dict[str, Any]], pages: int, root: Path) -> None:
    manifest["schema_version"] = "1.0"
    manifest["root"] = str(root.resolve())
    manifest["pages"] = pages
    manifest["updated_at"] = now_iso()
    manifest["expected_combinations"] = [spec["combination"] for spec in specs]
    manifest["expected_combination_specs"] = [
        {**spec, "pages_requested": pages} for spec in specs
    ]
    if not isinstance(manifest.get("records"), list):
        manifest["records"] = []


def record_for(manifest: dict[str, Any], combination: str) -> dict[str, Any] | None:
    for record in manifest.get("records", []):
        if isinstance(record, dict) and record.get("combination") == combination:
            return record
    return None


def save_record(manifest: dict[str, Any], record: dict[str, Any]) -> None:
    records = [
        item
        for item in manifest.get("records", [])
        if not isinstance(item, dict) or item.get("combination") != record["combination"]
    ]
    records.append(record)
    manifest["records"] = records
    manifest["updated_at"] = now_iso()


def stamp_payload(path: Path, spec: dict[str, Any]) -> int | None:
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        return None
    payload["keyword"] = spec["keyword"]
    payload["city"] = spec["city"]
    payload["filters"] = {
        "salary": spec["salary_code"],
        "experience": spec["experience_code"],
        "degree": spec["degree"],
    }
    payload["total"] = len(jobs)
    write_json_atomic(path, payload)
    return len(jobs)


def gate_wait_is_interactive() -> bool:
    """Return whether waiting for a human gate response is safe.

    A redirected stdin must never block a batch run. Tests may replace either
    this module's ``input`` alias or ``builtins.input`` to exercise the wait
    path without requiring a real terminal.
    """

    input_function = input
    if input_function is _ORIGINAL_INPUT and builtins.input is not _ORIGINAL_INPUT:
        input_function = builtins.input
    try:
        stdin_is_tty = bool(sys.stdin.isatty())
    except (AttributeError, OSError):
        stdin_is_tty = False
    return GUI_INTERACTIVE_MODE or stdin_is_tty or input_function is not _ORIGINAL_INPUT


def default_profile_dir(browser: str) -> Path:
    if browser not in {"chrome", "edge"}:
        raise ValueError(f"不支持的浏览器: {browser}")
    suffix = "chrome-profile" if browser == "chrome" else "edge-profile"
    return DEFAULT_PROFILE_ROOT / suffix


def resolve_browser_executable(browser: str) -> Path:
    """Resolve the installed Windows browser without touching the main profile."""

    if browser not in {"chrome", "edge"}:
        raise ValueError(f"不支持的浏览器: {browser}")
    executable = "chrome.exe" if browser == "chrome" else "msedge.exe"
    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
        candidates.append(base / ("Google" if browser == "chrome" else "Microsoft") / ("Chrome" if browser == "chrome" else "Edge") / "Application" / executable)
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        base_value = os.environ.get(env_name)
        if not base_value:
            continue
        base = Path(base_value)
        candidates.append(base / ("Google" if browser == "chrome" else "Microsoft") / ("Chrome" if browser == "chrome" else "Edge") / "Application" / executable)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return Path(executable)


def cdp_info(cdp_port: int) -> dict[str, Any] | None:
    try:
        with urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else None
    except (OSError, URLError, json.JSONDecodeError, ValueError):
        return None


def wait_for_cdp(cdp_port: int, timeout: int = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cdp_info(cdp_port) is not None:
            return True
        time.sleep(0.5)
    return cdp_info(cdp_port) is not None


def run_setup_browser(
    python: Path,
    scraper_root: Path,
    browser: str,
    cdp_port: int,
    profile_dir: Path | None = None,
) -> int:
    """Launch an isolated Chrome/Edge profile for one BOSS account.

    The upstream scraper only needs a CDP endpoint while collecting. Launching
    the browser here gives Chrome and Edge separate profiles, so two accounts
    can run concurrently without sharing cookies or locking one profile.
    """

    del python, scraper_root
    label = "Edge" if browser == "edge" else "Chrome"
    profile = (profile_dir or default_profile_dir(browser)).expanduser().resolve()
    existing = cdp_info(cdp_port)
    if existing is not None:
        print(f"CDP 端口 {cdp_port} 已经可用（{existing.get('Browser', '未知浏览器')}）。", flush=True)
        print(f"请确认它对应 {label} 的目标账号，然后按 Enter 开始采集。", flush=True)
    else:
        executable = resolve_browser_executable(browser)
        if not executable.is_file() and executable.name not in {"chrome.exe", "msedge.exe"}:
            print(f"找不到 {label} 可执行文件: {executable}", flush=True)
            return 1
        profile.mkdir(parents=True, exist_ok=True)
        command = [
            str(executable),
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
            "https://www.zhipin.com/",
        ]
        print(f"启动专用 {label}: 端口 {cdp_port}", flush=True)
        print(f"独立 profile: {profile}", flush=True)
        try:
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as error:
            print(f"启动 {label} 失败: {error}", flush=True)
            return 1
        if not wait_for_cdp(cdp_port):
            print(f"等待 CDP 端口 {cdp_port} 超时，请检查 {label} 是否启动。", flush=True)
            return 1
        print(f"{label} 已启动。请在这个窗口登录对应的 BOSS 账号。", flush=True)

    if not gate_wait_is_interactive():
        print("当前终端不可交互；请先手动登录，再去掉 --setup 参数启动采集。", flush=True)
        return 2
    try:
        input("登录完成后按 Enter 开始采集: ")
    except (EOFError, KeyboardInterrupt):
        return 2
    return 0


def invoke_spec(
    spec: dict[str, Any],
    *,
    root: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    python: Path,
    scraper_root: Path,
    pages: int,
    cdp_port: int = DEFAULT_CDP_PORT,
    verbose_upstream: bool = True,
    wait_on_gate: bool = True,
    browser: str = "chrome",
    shard: int = 0,
    total_shards: int = 1,
    expected_specs: list[dict[str, Any]] | None = None,
) -> str:
    from collection_sources import assert_source_specs_compatible

    assert_source_specs_compatible(root, expected_specs if expected_specs is not None else [spec])
    track_dir = root / "raw" / spec["track"] / spec["city_id"]
    track_dir.mkdir(parents=True, exist_ok=True)
    combination = spec["combination"]
    output_path = track_dir / f"{combination}.json"
    log_path = track_dir / f"{combination}.log"
    checkpoint_path = root / "checkpoints" / f"{combination}.partial.json"
    old = record_for(manifest, combination)
    resume_retry_reason = None
    if old and old.get("status") in {"success", "no_data"}:
        old_job_count = payload_job_count(output_path)
        if old_job_count is not None:
            # New records describe the final attempt explicitly. Their log may
            # also contain earlier, failed attempts before a human gate retry.
            diagnostics_confirmed = (
                "failure_reason" in old
                and old["failure_reason"] is None
                and old.get("retryable") is False
            )
            if old_job_count == 0 and not diagnostics_confirmed:
                previous_logs = [log_path]
                recorded_log = old.get("log")
                if isinstance(recorded_log, str) and recorded_log:
                    recorded_log_path = Path(recorded_log).expanduser()
                    if not recorded_log_path.is_absolute():
                        recorded_log_path = root / recorded_log_path
                    if recorded_log_path != log_path:
                        previous_logs.append(recorded_log_path)
                for previous_log in previous_logs:
                    try:
                        previous_output = previous_log.read_text(encoding="utf-8-sig", errors="replace")
                    except OSError:
                        continue
                    resume_retry_reason = empty_result_failure_reason(previous_output)
                    # A relocated root's local log takes precedence over a
                    # potentially stale absolute path in its old manifest.
                    break
            if resume_retry_reason is None:
                print(f"SKIP {combination} (已完成)", flush=True)
                return str(old["status"])
            print(f"RETRY {combination} ({resume_retry_reason})", flush=True)

    script = scraper_root / "scripts" / "boss_cdp_raw.py"
    command = [
        str(python),
        "-u",
        str(script),
        "--keyword",
        spec["keyword"],
        "--city",
        spec["city"],
        "--pages",
        str(pages),
        "--salary",
        spec["salary_code"],
        "--experience",
        spec["experience_code"],
        "--degree",
        spec["degree"],
        "--cdp-port",
        str(cdp_port),
        "--no-detail",
        "--output",
        str(output_path),
    ]
    if checkpoint_path.is_file():
        command.extend(("--merge", str(checkpoint_path)))

    started = now_iso()
    print(f"RUN {combination} ({started}), pages={pages}", flush=True)
    old_gate_wait_count = old.get("gate_wait_count", 0) if old else 0
    gate_wait_count = old_gate_wait_count if isinstance(old_gate_wait_count, int) else 0
    last_gate_at = old.get("last_gate_at") if old else None
    attempt_logs: list[str] = []

    while True:
        process: subprocess.Popen[str] | None = None
        output_lines: list[str] = []
        try:
            attempt_command = list(command)
            if checkpoint_path.is_file() and "--merge" not in attempt_command:
                attempt_command.extend(("--merge", str(checkpoint_path)))
            process = subprocess.Popen(
                attempt_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            # Retain complete upstream output for logs and security detection. The
            # default terminal mode shows each upstream job row so collection can
            # be inspected live; quiet mode keeps only progress and errors visible.
            for line in process.stdout:
                output_lines.append(line)
                if verbose_upstream or should_display_upstream_line(line):
                    print(redact_log(line), end="", flush=True)
            exit_code = process.wait()
            output = "".join(output_lines)
        except KeyboardInterrupt:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait()
            output = "".join(output_lines) + "\n用户中断采集\n"
            exit_code = 130
        except OSError as error:
            output = "".join(output_lines) + f"\n启动采集器失败：{error}\n"
            exit_code = 1

        attempt_logs.append(redact_log(output))
        log_path.write_text("\n".join(attempt_logs), encoding="utf-8")

        security = security_detected(output)
        job_count = stamp_payload(output_path, spec) if output_path.is_file() else None
        valid = job_count is not None
        empty_failure = empty_result_failure_reason(output) if job_count == 0 else None
        if (security or exit_code != 0) and valid and not checkpoint_path.exists():
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_path, checkpoint_path)
        status = (
            "paused"
            if security or exit_code != 0
            else "failed"
            if not valid or empty_failure
            else "no_data"
            if job_count == 0
            else "success"
        )
        pages_completed, page_markers = page_progress(output)
        record = {
            **spec,
            "pages_requested": pages,
            "pages_completed": pages_completed,
            "page_markers": page_markers,
            "status": status,
            "exit_code": exit_code,
            "job_count": job_count,
            "json": str(output_path),
            "log": str(log_path),
            "checkpoint": str(checkpoint_path) if checkpoint_path.exists() else None,
            "started_at": started,
            "finished_at": now_iso(),
            "security_or_login_gate": security,
            "failure_reason": empty_failure if status == "failed" else None,
            "retryable": status == "failed" and empty_failure is not None,
            "gate_wait_count": gate_wait_count,
            "last_gate_at": last_gate_at,
            "browser": browser,
            "cdp_port": cdp_port,
            "shard": shard,
            "total_shards": total_shards,
        }
        if resume_retry_reason is not None:
            record["resume_retry_reason"] = resume_retry_reason

        if security:
            last_gate_at = now_iso()
            interactive = gate_wait_is_interactive()
            can_wait = wait_on_gate and interactive
            if can_wait:
                gate_wait_count += 1
            record.update(
                {
                    "status": "paused",
                    "gate_wait_count": gate_wait_count,
                    "last_gate_at": last_gate_at,
                    "gate_wait_result": "waiting" if can_wait else (
                        "waiting_disabled" if not wait_on_gate else "non_interactive"
                    ),
                }
            )
            persist_manifest_record(
                manifest_path,
                record,
                expected_specs=expected_specs,
                pages=pages,
                root=root,
                local_manifest=manifest,
            )

            if not can_wait:
                print(
                    f"PAUSE/STOP {combination} status=paused; 请检查日志和专用浏览器后重新运行",
                    flush=True,
                )
                return "paused"

            print(
                "检测到登录或安全验证。请在浏览器中手动完成验证；"
                "完成后按 Enter 重试当前组合，输入 q 退出并保留暂停状态。",
                flush=True,
            )
            try:
                input_function = input
                if input_function is _ORIGINAL_INPUT and builtins.input is not _ORIGINAL_INPUT:
                    input_function = builtins.input
                response = input_function("验证完成后按 Enter 继续，q 退出: ").strip().lower()
            except EOFError:
                response = "q"
                record["gate_wait_result"] = "eof"
            except KeyboardInterrupt:
                response = "q"
                record["gate_wait_result"] = "interrupted"

            if response in {"q", "quit", "exit"}:
                record["status"] = "paused"
                if record.get("gate_wait_result") == "waiting":
                    record["gate_wait_result"] = "quit"
                persist_manifest_record(
                    manifest_path,
                    record,
                    expected_specs=expected_specs,
                    pages=pages,
                    root=root,
                    local_manifest=manifest,
                )
                print(f"PAUSE/STOP {combination} status=paused; 保留当前进度", flush=True)
                return "paused"

            record["status"] = "running"
            record["gate_wait_result"] = "retry"
            persist_manifest_record(
                manifest_path,
                record,
                expected_specs=expected_specs,
                pages=pages,
                root=root,
                local_manifest=manifest,
            )
            print(f"重试当前组合: {combination}", flush=True)
            continue

        persist_manifest_record(
            manifest_path,
            record,
            expected_specs=expected_specs,
            pages=pages,
            root=root,
            local_manifest=manifest,
        )
        if status == "success" or status == "no_data":
            print(f"DONE {combination} jobs={job_count}", flush=True)
        else:
            reason = f"; {record['failure_reason']}" if record["failure_reason"] else ""
            print(f"PAUSE/STOP {combination} status={status}{reason}; 请检查日志和专用浏览器后重新运行", flush=True)
        return status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="断点续跑的 BOSS 架构岗位组合采集器")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="检索组合 JSON 配置")
    parser.add_argument("--scraper-root", help="上游 boss-zhipin-scraper 目录")
    parser.add_argument("--python", dest="python_path", help="运行上游采集器的 Python 路径")
    parser.add_argument("--root", type=Path, default=None, help="结果根目录；源码默认项目 job-result，安装后默认用户状态目录")
    parser.add_argument("--batch", choices=("all", "current", "target"), default="all")
    parser.add_argument("--cities", nargs="+", default=None, help="仅运行所选城市名称或 ID；省略表示全部城市")
    parser.add_argument("--pages", type=int, choices=range(1, 11), default=10)
    parser.add_argument("--browser", choices=("chrome", "edge"), default="chrome", help="本分片使用的专用浏览器")
    parser.add_argument("--cdp-port", type=int, default=9222, help="上游 CDP 调试端口")
    parser.add_argument("--profile-dir", type=Path, default=None, help="所选浏览器的独立 CDP profile 目录")
    parser.add_argument("--total-shards", type=int, default=1, help="把组合确定性拆成 N 份")
    parser.add_argument("--shard", type=int, default=0, help="当前运行的分片编号，从 0 开始")
    parser.add_argument("--limit", type=int, help="仅运行前 N 个组合，用于 smoke test")
    parser.add_argument("--dry-run", action="store_true", help="只打印组合，不连接浏览器")
    parser.add_argument(
        "--setup-browser",
        "--setup-chrome",
        dest="setup_browser",
        action="store_true",
        help="先启动所选浏览器的独立 CDP profile 并等待人工登录",
    )
    parser.add_argument(
        "--setup-only",
        action="store_true",
        help="只启动并等待浏览器登录，完成后退出，不开始采集",
    )
    parser.add_argument("--gui-interactive", action="store_true", help="桌面 GUI 通过标准输入确认人工验证")
    parser.add_argument("--continue-on-error", action="store_true", help="失败或安全验证后继续后续组合；默认立即停止")
    gate_wait_group = parser.add_mutually_exclusive_group()
    gate_wait_group.add_argument(
        "--wait-on-gate",
        dest="wait_on_gate",
        action="store_true",
        help="遇到登录或安全验证时等待人工完成后按 Enter 重试当前组合（默认）",
    )
    gate_wait_group.add_argument(
        "--no-wait-on-gate",
        dest="wait_on_gate",
        action="store_false",
        help="遇到登录或安全验证时保持 paused，不等待输入",
    )
    parser.set_defaults(wait_on_gate=True)
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "--verbose-upstream",
        dest="verbose_upstream",
        action="store_true",
        help="显示上游采集器的逐条岗位明细（默认行为）",
    )
    verbosity.add_argument(
        "--quiet-upstream",
        dest="verbose_upstream",
        action="store_false",
        help="隐藏逐条岗位明细，仅显示进度、等待和错误信息",
    )
    parser.set_defaults(verbose_upstream=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global GUI_INTERACTIVE_MODE
    args = parse_args(argv)
    GUI_INTERACTIVE_MODE = bool(args.gui_interactive)
    config = load_config(args.config.expanduser().resolve())
    all_specs = build_specs(config, args.batch, args.limit, args.cities)
    expected_total = len(build_specs(config, args.batch, cities=args.cities))
    specs = shard_specs(all_specs, args.shard, args.total_shards)
    print(
        f"组合数: {len(specs)} (完整配置 {expected_total}) | 分片 {args.shard + 1}/{args.total_shards} | "
        f"浏览器: {args.browser} | CDP: {args.cdp_port} | 页数: {args.pages}",
        flush=True,
    )
    if args.dry_run:
        for index, spec in enumerate(specs, start=1):
            print(f"{index:03d}/{len(specs)} {spec['combination']} | {spec['city']} | {spec['keyword']} | {spec['salary']} | {spec['experience']}")
        return 0

    root = (args.root.expanduser() if args.root else default_root()).resolve()
    from collection_sources import assert_source_specs_compatible

    try:
        assert_source_specs_compatible(root, all_specs)
    except ValueError as exc:
        print(f"采集配置冲突：{exc}", file=sys.stderr, flush=True)
        return 20
    scraper_root = resolve_scraper_root(args.scraper_root)
    python = resolve_python(scraper_root, args.python_path)
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "collection_manifest.json"
    manifest = persist_manifest_state(
        manifest_path,
        specs=all_specs,
        pages=args.pages,
        root=root,
    )

    if args.setup_browser or args.setup_only:
        setup_exit = run_setup_browser(
            python,
            scraper_root,
            args.browser,
            args.cdp_port,
            profile_dir=args.profile_dir,
        )
        if setup_exit != 0:
            return setup_exit
        if args.setup_only:
            print("浏览器准备完成，按 --setup-only 要求退出。", flush=True)
            return 0

    for index, spec in enumerate(specs, start=1):
        print(
            f"\n[{index}/{len(specs)}] {spec['combination']} | "
            f"{spec['city']} | {spec['keyword']} | {spec['salary']} | {spec['experience']}",
            flush=True,
        )
        status = invoke_spec(
            spec,
            root=root,
            manifest=manifest,
            manifest_path=manifest_path,
            python=python,
            scraper_root=scraper_root,
            pages=args.pages,
            cdp_port=args.cdp_port,
            verbose_upstream=args.verbose_upstream,
            wait_on_gate=args.wait_on_gate,
            browser=args.browser,
            shard=args.shard,
            total_shards=args.total_shards,
            expected_specs=all_specs,
        )
        print(f"进度: {index}/{len(specs)}", flush=True)
        if status in {"paused", "failed"} and not args.continue_on_error:
            return 20
    persist_manifest_state(
        manifest_path,
        specs=all_specs,
        pages=args.pages,
        root=root,
    )
    print("BATCH_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
