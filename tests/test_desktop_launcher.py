"""Console-free launcher checks: no real GUI, collection, or model calls."""

from __future__ import annotations

import contextlib
import ctypes
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT / "run_desktop.ps1"
SOURCE = LAUNCHER.read_text(encoding="ascii")
PAYLOAD_KEY = "BOSS_ARCHITECTURE_DESKTOP_LAUNCH"


def python_source(name: str) -> str:
    match = re.search(rf"\$script:{name} = @'\n(.*?)\n'@", SOURCE, re.S)
    if not match:
        raise AssertionError(f"Missing embedded Python source: {name}")
    return match.group(1)


def ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class EmbeddedLauncherTests(unittest.TestCase):
    def test_probe_requires_python_310_and_working_tcl_without_windows(self):
        probe = compile(python_source("DesktopPythonProbe"), "<desktop-probe>", "exec")
        for version, tcl_error, expected in (
            ((3, 9, 99), None, 10),
            ((3, 10, 0), RuntimeError("private interpreter detail"), 11),
            ((3, 11, 0), None, 0),
        ):
            with self.subTest(version=version, tcl_error=bool(tcl_error)):
                tcl = mock.Mock(side_effect=tcl_error)
                output = io.StringIO()
                with mock.patch.object(sys, "version_info", version), mock.patch.dict(
                    sys.modules, {"tkinter": types.SimpleNamespace(Tcl=tcl)}
                ), contextlib.redirect_stdout(output):
                    try:
                        exec(probe, {})
                    except SystemExit as error:
                        code = error.code
                    else:
                        code = 0
                self.assertEqual(code, expected)
                if expected == 0:
                    self.assertEqual(json.loads(output.getvalue())["executable"], sys.executable)
                    tcl.assert_called_once_with()
                else:
                    self.assertEqual(output.getvalue(), "")
                if version < (3, 10):
                    tcl.assert_not_called()

    def test_bootstrap_preserves_arguments_and_removes_private_payload(self):
        message = mock.Mock()
        arguments = ["--root", "C:\\中文 空格 & 数据\\", "--config", 'C:\\配置 "字面".json']
        script = "C:\\项目 空格\\desktop_app.py"

        def app(path, *, run_name):
            self.assertEqual(path, script)
            self.assertEqual(run_name, "__main__")
            self.assertEqual(sys.argv, [script, *arguments])
            self.assertNotIn(PAYLOAD_KEY, os.environ)

        code = self.run_bootstrap(script, arguments, app, message)
        self.assertEqual(code, 0)
        message.assert_not_called()

    def test_bootstrap_errors_are_visible_and_never_echo_exception_or_exit_text(self):
        secret = "sk-test-do-not-display-provider-body"
        for error, expected in (
            (RuntimeError(secret), 1),
            (SystemExit(secret), 1),
            (SystemExit(2), 1),
            (SystemExit(0), 0),
            (SystemExit(None), 0),
        ):
            with self.subTest(error=type(error).__name__, expected=expected):
                message = mock.Mock()
                code = self.run_bootstrap("desktop_app.py", [], mock.Mock(side_effect=error), message)
                self.assertEqual(code, expected)
                if expected:
                    message.assert_called_once()
                    self.assertIn("could not start or continue", message.call_args.args[1])
                    self.assertNotIn(secret, repr(message.call_args))
                else:
                    message.assert_not_called()

    def run_bootstrap(self, script, arguments, app, message):
        native = types.SimpleNamespace(user32=types.SimpleNamespace(MessageBoxW=message))
        payload = json.dumps({"script": script, "arguments": arguments})
        with mock.patch.dict(os.environ, {PAYLOAD_KEY: payload}), mock.patch.object(
            sys, "argv", []
        ), mock.patch.object(sys, "path", list(sys.path)), mock.patch.object(
            ctypes, "windll", native, create=True
        ), mock.patch("runpy.run_path", side_effect=app):
            try:
                exec(compile(python_source("DesktopPythonBootstrap"), "<desktop-bootstrap>", "exec"), {})
            except SystemExit as error:
                return error.code
            return 0


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows PowerShell required")
class PowerShellLauncherTests(unittest.TestCase):
    def run_ps(self, body: str, *, env=None, cwd=None):
        command = (
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
            f". {ps_literal(LAUNCHER)}\n"
            "$ErrorActionPreference = 'Stop'\n" + body
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=cwd or PROJECT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=25,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_double_dash_and_powershell_options_preserve_paths(self):
        env = dict(os.environ, TEST_OPTIONS=json.dumps([
            "--root", "C:\\中文 空格 & 数据\\", "-ScraperRoot", "C:\\工具\\抓取器",
            "--config=.\\配置 文件.json", "-Python", sys.executable,
        ]))
        result = json.loads(self.run_ps(
            "Read-DesktopLaunchOptions -Arguments (ConvertFrom-Json $env:TEST_OPTIONS) | ConvertTo-Json -Compress",
            env=env,
        ))
        self.assertEqual(result["Root"], "C:\\中文 空格 & 数据\\")
        self.assertEqual(result["ScraperRoot"], "C:\\工具\\抓取器")
        self.assertEqual(result["Config"], ".\\配置 文件.json")
        self.assertEqual(result["Python"], sys.executable)

    def test_invalid_options_produce_safe_visible_error_without_opening_dialog(self):
        secret = "sk-test-provider-response-not-for-display"
        env = dict(os.environ, TEST_SECRET=secret)
        output = self.run_ps("""
function Show-DesktopLaunchError { param($Message) Write-Output ('VISIBLE: ' + $Message) }
Invoke-DesktopLauncher -ProjectRoot $PSScriptRoot -Arguments @('--unknown', $env:TEST_SECRET)
Invoke-DesktopLauncher -ProjectRoot $PSScriptRoot -Arguments @('--root')
""", env=env)
        self.assertEqual(output.count("VISIBLE:"), 2)
        self.assertIn("Invalid launch options", output)
        self.assertNotIn(secret, output)
        self.assertEqual(output.count("\n1"), 2)

    def test_missing_project_and_python_fail_without_external_work(self):
        with tempfile.TemporaryDirectory(prefix="desktop-missing-") as temporary:
            output = self.run_ps(f"""
function Show-DesktopLaunchError {{ param($Message) Write-Output ('VISIBLE: ' + $Message) }}
function Find-DesktopPython {{ return $null }}
Invoke-DesktopLauncher -ProjectRoot {ps_literal(temporary)}
Invoke-DesktopLauncher -ProjectRoot {ps_literal(PROJECT)}
""")
        self.assertIn("desktop_app.py is missing", output)
        self.assertIn("No usable Python 3.10+ with tkinter", output)
        self.assertEqual(output.count("VISIBLE:"), 2)

    def test_candidate_order_prefers_project_and_active_environments(self):
        with tempfile.TemporaryDirectory(prefix="desktop-candidates-") as temporary:
            root = Path(temporary)
            expected = [root / ".venv/Scripts/python.exe", root / "venv/Scripts/python.exe", root / "active/Scripts/python.exe"]
            for path in expected:
                path.parent.mkdir(parents=True)
                path.touch()
            env = dict(os.environ, VIRTUAL_ENV=str(root / "active"), CONDA_PREFIX="")
            result = json.loads(self.run_ps(f"""
function Get-Command {{ param($Name, $CommandType, $ErrorAction) [pscustomobject]@{{Source = ('C:\\mock\\' + $Name)}} }}
@(Get-DesktopPythonCandidates -ProjectRoot {ps_literal(root)}) | ConvertTo-Json -Depth 4 -Compress
""", env=env))
        self.assertEqual([item["Path"] for item in result[:3]], [str(path) for path in expected])
        self.assertEqual(result[3]["Path"], "C:\\mock\\py.exe")
        self.assertEqual(result[3]["Prefix"], ["-3"])
        self.assertEqual(result[4]["Path"], "C:\\mock\\python.exe")

    def test_unconfigured_store_alias_is_not_started(self):
        output = self.run_ps("""
function Get-Command { param($Name, $CommandType, $ErrorAction)
    if ($Name -eq 'python.exe') { [pscustomobject]@{Source = 'C:\\Users\\test\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe'} }
}
@(Get-DesktopPythonCandidates -ProjectRoot 'C:\\absent-desktop-project').Count
""", env=dict(os.environ, VIRTUAL_ENV="", CONDA_PREFIX=""))
        self.assertEqual(output, "0")

    def test_invalid_candidate_falls_back_but_explicit_missing_python_does_not(self):
        output = self.run_ps("""
function Test-DesktopPython { param($Candidate) if ($Candidate.Path -eq 'valid') { 'selected-python.exe' } }
function Get-DesktopPythonCandidates {
    param($ProjectRoot, $Python)
    if ($Python) { return }
    [pscustomobject]@{Path = 'invalid'; Prefix = @()}
    [pscustomobject]@{Path = 'valid'; Prefix = @('-3')}
}
Find-DesktopPython -ProjectRoot 'C:\\project'
if (Find-DesktopPython -ProjectRoot 'C:\\project' -Python 'missing.exe') { throw 'Unexpected fallback' }
""")
        self.assertEqual(output, "selected-python.exe")

    def test_native_quoting_roundtrips_backslashes_quotes_spaces_and_unicode(self):
        arguments = ["", "C:\\中文 空格\\", 'a"b', 'a\\\\"b', "a&b", "plain", "line\nfeed"]
        env = dict(os.environ, TEST_NATIVE_ARGS=json.dumps(arguments))
        output = self.run_ps(f"""
$extra = ConvertFrom-Json $env:TEST_NATIVE_ARGS
$native = @('-c', 'import json,sys;print(json.dumps(sys.argv[1:]))') + $extra
$start = New-Object System.Diagnostics.ProcessStartInfo
$start.FileName = {ps_literal(sys.executable)}
$start.Arguments = ($native | ForEach-Object {{ ConvertTo-DesktopNativeArgument $_ }}) -join ' '
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardOutput = $true
$process = [System.Diagnostics.Process]::Start($start)
$output = $process.StandardOutput.ReadToEnd()
$process.WaitForExit()
$process.Dispose()
Write-Output $output
""", env=env)
        self.assertEqual(json.loads(output), arguments)

    def test_bat_and_ps1_launch_dummy_app_from_unicode_space_directory(self):
        try:
            import tkinter
            tkinter.Tcl()
        except Exception:
            self.skipTest("Current test Python has no usable Tcl/Tk")
        for use_bat in (False, True):
            with self.subTest(use_bat=use_bat), tempfile.TemporaryDirectory(prefix="desktop-test-") as temporary:
                root = Path(temporary)
                project = root / "中文 空间 & project"
                project.mkdir()
                # A fixture-only error sink guarantees a regression cannot open a dialog.
                fixture = SOURCE.replace("ctypes.windll.user32.MessageBoxW(", "print(")
                fixture = fixture.replace(
                    "# Dot-sourcing exposes only the functions for non-interactive unit tests.",
                    "function Show-DesktopLaunchError { param($Message) [Console]::Error.WriteLine($Message) }\n"
                    "# Fixture error sink: no native dialogs.",
                )
                (project / "run_desktop.ps1").write_text(fixture, encoding="ascii")
                shutil.copyfile(PROJECT / "run_desktop.bat", project / "run_desktop.bat")
                (project / "desktop_app.py").write_text(
                    "import ctypes, json, os, sys\nfrom pathlib import Path\n"
                    "Path(os.environ['DESKTOP_LAUNCH_TEST_OUTPUT']).write_text(json.dumps({"
                    "'argv':sys.argv[1:], 'cwd':os.getcwd(), 'payload_present':"
                    f"{PAYLOAD_KEY!r} in os.environ"
                    ", 'console_window':ctypes.windll.kernel32.GetConsoleWindow()"
                    "}, ensure_ascii=False), encoding='utf-8')\n", encoding="utf-8",
                )
                output = root / "captured.json"
                env = dict(os.environ, DESKTOP_LAUNCH_TEST_OUTPUT=str(output))
                data_root = root / "数据 & 结果"
                scraper_root = root / "工具 空格"
                options = ["--root", str(data_root), "--scraper-root", str(scraper_root),
                           "--config", "配置 文件.json", "--python", sys.executable]
                if use_bat:
                    # shell=True adds cmd.exe's required outer /c quotation.
                    command = subprocess.list2cmdline([str(project / "run_desktop.bat"), *options])
                else:
                    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(project / "run_desktop.ps1"), *options]
                result = subprocess.run(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                        capture_output=True, timeout=25, shell=use_bat)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                deadline = time.monotonic() + 5
                while not output.is_file() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(output.is_file(), "The detached fixture app did not finish")
                captured = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(captured["argv"], ["--root", str(data_root), "--scraper-root", str(scraper_root),
                                                     "--config", str(root / "配置 文件.json")])
                self.assertEqual(captured["cwd"], str(project))
                self.assertFalse(captured["payload_present"])
                self.assertEqual(captured["console_window"], 0)


if __name__ == "__main__":
    unittest.main()
