"""Open a visible, isolated local browser without invoking scraper CLI setup.

Browser/profile flags belong to this project. Upstream versions do not share
that command-line contract. This entry point only prepares CDP; it never
collects jobs, copies cookies, checks login or prompts on standard input.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

from collect_architecture import resolve_browser_executable


BOSS_URL = "https://www.zhipin.com/"


class BrowserPreparationError(RuntimeError):
    """A launch/connection failure, distinct from a BOSS login gate."""


def default_profile_dir(browser: str) -> Path:
    if browser not in {"chrome", "edge"}:
        raise ValueError("浏览器必须为 chrome 或 edge")
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    base = Path(home).expanduser() if home else Path.cwd()
    return base / ".boss-zhipin-scraper" / f"{browser}-profile"


def cdp_info(port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    """Never send local browser-debugging traffic to a configured proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
        if isinstance(value, dict) and value.get("webSocketDebuggerUrl"):
            return value
    except (OSError, urllib.error.URLError, ValueError, UnicodeError):
        pass
    return None


def open_dedicated_browser(
    browser: str = "chrome", port: int = 9222,
    profile_dir: Path | str | None = None, *,
    show_window: bool = True, timeout: float = 30.0,
) -> None:
    """Ensure CDP exists and, when requested, open a visible BOSS window.

    Reusing a running profile opens a new window in that instance. No process
    is stopped on failure: existing user browsers never belong to this helper.
    """
    if browser not in {"chrome", "edge"}:
        raise ValueError("浏览器必须为 chrome 或 edge")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("CDP 端口须为 1–65535 的整数")
    label = "Chrome" if browser == "chrome" else "Edge"
    profile = Path(profile_dir or default_profile_dir(browser)).expanduser().resolve()
    existing = cdp_info(port)
    if existing is not None and not show_window:
        print(f"专用 {label} 的 CDP {port} 已就绪。", flush=True)
        return
    executable = resolve_browser_executable(browser)
    try:
        profile.mkdir(parents=True, exist_ok=True)
        command = [
            str(executable), f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--remote-allow-origins=*",
            "--new-window", BOSS_URL,
        ]
        print(f"正在打开专用 {label} 的 BOSS 页面（CDP {port}）。", flush=True)
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError as error:
        raise BrowserPreparationError(f"专用 {label} 启动失败：{error}") from error

    deadline = time.monotonic() + timeout
    while True:
        if cdp_info(port) is not None:
            print(f"专用 {label} 已连接（CDP {port}）。请在该窗口确认 BOSS 登录或完成人工验证。", flush=True)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserPreparationError(
                f"已发送 {label} 打开命令，但 CDP {port} 未就绪；"
                "请检查浏览器启动提示、端口占用，以及该独立 profile 是否正在其他端口运行。"
            )
        time.sleep(min(0.25, remaining))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只准备独立浏览器/CDP，不采集或等待终端输入")
    parser.add_argument("--browser", choices=("chrome", "edge"), default="chrome")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--profile-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        open_dedicated_browser(args.browser, args.cdp_port, args.profile_dir)
    except (BrowserPreparationError, ValueError) as error:
        print(str(error), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
