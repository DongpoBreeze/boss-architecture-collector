"""Browser startup regression checks; never open a real browser or BOSS page."""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_setup as setup
import process_architecture as processor
import review_pipeline as pipeline


READY = {"Browser": "Chrome/test", "webSocketDebuggerUrl": "ws://127.0.0.1/browser/test"}


class BrowserSetupTests(unittest.TestCase):
    def test_chrome_and_edge_launch_visible_boss_windows_with_distinct_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            for browser, port, executable in (("chrome", 9222, "chrome.exe"), ("edge", 9223, "msedge.exe")):
                with self.subTest(browser=browser):
                    profile = Path(directory) / browser
                    with patch.object(setup, "cdp_info", side_effect=[None, READY]), \
                         patch.object(setup, "resolve_browser_executable", return_value=Path(executable)), \
                         patch.object(setup.subprocess, "Popen") as launch, redirect_stdout(io.StringIO()):
                        setup.open_dedicated_browser(browser, port, profile)
                    command = launch.call_args.args[0]
                    self.assertEqual(command[0], executable)
                    self.assertIn(f"--remote-debugging-port={port}", command)
                    self.assertIn(f"--user-data-dir={profile.resolve()}", command)
                    self.assertIn("--new-window", command)
                    self.assertEqual(command[-1], "https://www.zhipin.com/")
                    self.assertIs(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)
                    self.assertTrue(launch.call_args.kwargs["close_fds"])

    def test_existing_cdp_is_reused_without_launch_when_window_not_requested(self):
        with patch.object(setup, "cdp_info", return_value=READY), \
             patch.object(setup.subprocess, "Popen") as launch, redirect_stdout(io.StringIO()):
            setup.open_dedicated_browser(show_window=False)
        launch.assert_not_called()

    def test_prepare_action_reopens_visible_window_even_when_cdp_already_exists(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(setup, "cdp_info", return_value=READY), \
             patch.object(setup.subprocess, "Popen") as launch, redirect_stdout(io.StringIO()):
            setup.open_dedicated_browser(profile_dir=directory)
        self.assertIn("--new-window", launch.call_args.args[0])

    def test_missing_executable_reports_launch_failure_without_claiming_window_open(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(setup, "cdp_info", return_value=None), \
             patch.object(setup.subprocess, "Popen", side_effect=FileNotFoundError("browser missing")), \
             redirect_stdout(output):
            code = setup.main(["--browser", "edge", "--profile-dir", directory])
        self.assertEqual(code, 1)
        self.assertIn("Edge 启动失败", output.getvalue())
        self.assertNotIn("已连接", output.getvalue())

    def test_launch_without_cdp_is_an_error_and_never_terminates_existing_processes(self):
        child = Mock()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(setup, "cdp_info", return_value=None), \
             patch.object(setup.subprocess, "Popen", return_value=child), redirect_stdout(output):
            with self.assertRaisesRegex(setup.BrowserPreparationError, "CDP 9223 未就绪"):
                setup.open_dedicated_browser("edge", 9223, directory, timeout=0)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
        self.assertNotIn("已连接", output.getvalue())

    def test_cdp_probe_bypasses_proxies_and_requires_debugger_endpoint(self):
        for payload, expected in ((READY, True), ({"Browser": "Chrome"}, False), ([], False)):
            with self.subTest(payload=payload):
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.read.return_value = json.dumps(payload).encode()
                opener = Mock()
                opener.open.return_value = response
                with patch.object(setup.urllib.request, "ProxyHandler") as proxy, \
                     patch.object(setup.urllib.request, "build_opener", return_value=opener):
                    self.assertEqual(setup.cdp_info(9223) is not None, expected)
                proxy.assert_called_once_with({})
                self.assertEqual(opener.open.call_args.args[0], "http://127.0.0.1:9223/json/version")

    def test_invalid_browser_or_port_never_launches(self):
        with patch.object(setup.subprocess, "Popen") as launch:
            for browser, port in (("firefox", 9222), ("edge", 0), ("chrome", 65536), ("chrome", True)):
                with self.subTest(browser=browser, port=port), self.assertRaises(ValueError):
                    setup.open_dedicated_browser(browser, port)
        launch.assert_not_called()


class BrowserEntrypointTests(unittest.TestCase):
    def test_fetch_login_checks_use_real_upstream_argument_contract(self):
        # This fixture really runs argparse in a subprocess. Generic mock
        # returncodes previously hid invalid --profile-dir/--setup-browser flags.
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "boss_cdp_raw.py"
            entry.write_text(
                "import argparse\n"
                "p = argparse.ArgumentParser()\n"
                "p.add_argument('--check', action='store_true', required=True)\n"
                "p.add_argument('--cdp-port', type=int, required=True)\n"
                "p.parse_args()\n", encoding="utf-8",
            )
            for browser, port in (("chrome", 9222), ("edge", 9223)):
                with self.subTest(browser=browser), \
                     patch.object(processor, "local_cdp_ready", side_effect=[False, True]), \
                     patch.object(setup, "open_dedicated_browser") as launch, redirect_stdout(io.StringIO()):
                    ready, error = processor.ensure_detail_cdp_ready(
                        entry, Path(sys.executable), port, browser=browser,
                        profile_dir=Path(directory) / browser,
                    )
                self.assertTrue(ready, error)
                launch.assert_called_once_with(browser, port, Path(directory) / browser, show_window=True)

    def test_existing_logged_in_browser_does_not_open_extra_windows(self):
        with patch.object(processor, "local_cdp_ready", return_value=True), \
             patch.object(processor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(setup, "open_dedicated_browser") as launch, redirect_stdout(io.StringIO()):
            self.assertEqual(processor.ensure_detail_cdp_ready(Path("entry.py"), Path(sys.executable)), (True, None))
        launch.assert_not_called()

    def test_security_gate_shows_window_then_pauses_without_repeated_checks(self):
        with patch.object(processor, "local_cdp_ready", return_value=True), \
             patch.object(processor.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "需要安全验证\n", "")) as run, \
             patch.object(setup, "open_dedicated_browser") as launch, redirect_stdout(io.StringIO()):
            ready, error = processor.ensure_detail_cdp_ready(Path("entry.py"), Path(sys.executable), 9223, browser="edge")
        self.assertFalse(ready)
        self.assertIn("专用 Edge 已打开", error)
        self.assertIn("已抓 JD 保留", error)
        launch.assert_called_once()
        run.assert_called_once()

    def test_actual_launch_failure_does_not_claim_login_is_needed(self):
        with patch.object(processor, "local_cdp_ready", return_value=False), \
             patch.object(setup, "open_dedicated_browser", side_effect=setup.BrowserPreparationError("Edge 启动失败：找不到文件")), \
             patch.object(processor.subprocess, "run") as run, redirect_stdout(io.StringIO()):
            ready, error = processor.ensure_detail_cdp_ready(Path("entry.py"), Path(sys.executable), 9223, browser="edge")
        self.assertFalse(ready)
        self.assertIn("启动失败", error)
        self.assertNotIn("已打开", error)
        self.assertNotIn("登录", error)
        run.assert_not_called()

    def test_login_check_timeout_pauses_with_bounded_diagnostics(self):
        with patch.object(processor, "local_cdp_ready", return_value=True), \
             patch.object(processor.subprocess, "run", side_effect=subprocess.TimeoutExpired("check", 60)), \
             patch.object(setup, "open_dedicated_browser"), redirect_stdout(io.StringIO()):
            ready, error = processor.ensure_detail_cdp_ready(Path("entry.py"), Path(sys.executable))
        self.assertFalse(ready)
        self.assertIn("登录检查超时", error)

    def test_failed_login_diagnostics_redact_credentials(self):
        output = io.StringIO()
        diagnostic = "Cookie: sid=private-cookie\nAuthorization: Bearer private-token\n"
        with patch.object(processor, "local_cdp_ready", return_value=True), \
             patch.object(processor.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, diagnostic, "")), \
             patch.object(setup, "open_dedicated_browser"), redirect_stdout(output):
            processor.ensure_detail_cdp_ready(Path("entry.py"), Path(sys.executable))
        self.assertNotIn("private-cookie", output.getvalue())
        self.assertNotIn("private-token", output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())

    def test_gui_prepare_uses_our_noninteractive_launcher_for_both_browsers(self):
        events, commands = [], []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "boss_cdp_raw.py").touch()

            def child(task, stage, command, control, emit):
                self.assertIsNone(task)
                self.assertEqual(stage, "browser")
                self.assertEqual(Path(command[2]).name, "browser_setup.py")
                parsed = setup.parse_args(command[3:])
                commands.append(parsed)
                return 0 if parsed.browser == "chrome" else 1

            with patch.object(pipeline, "_run_child", side_effect=child), \
                 patch.object(processor, "resolve_python", return_value=Path(sys.executable)):
                result = pipeline.prepare_browser(
                    root, on_event=lambda kind, value: events.append((kind, value)), browser_mode="dual",
                    chrome_port=9322, edge_port=9323,
                    chrome_profile_dir=root / "chrome", edge_profile_dir=root / "edge",
                )
            self.assertEqual(result, {"chrome": "completed", "edge": "failed"})
            self.assertEqual({item.browser: (item.cdp_port, item.profile_dir) for item in commands},
                             {"chrome": (9322, root / "chrome"), "edge": (9323, root / "edge")})
        logs = "\n".join(str(value) for kind, value in events if kind == "log")
        self.assertIn("Edge 窗口或 CDP 准备失败", logs)
        self.assertNotIn("Edge 窗口与 CDP 已准备", logs)


if __name__ == "__main__":
    unittest.main()
