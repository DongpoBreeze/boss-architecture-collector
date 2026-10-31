"""Exercise collection PowerShell argument forwarding with offline dry runs."""

from __future__ import annotations

import os
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


@unittest.skipUnless(os.name == "nt" and SHELL, "Windows PowerShell is required")
class CollectionLauncherTests(unittest.TestCase):
    def run_launcher(self, script, *arguments):
        with tempfile.TemporaryDirectory(prefix="boss-collection-launcher-") as temporary:
            base = Path(temporary)
            output = base / "new output"
            environment = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run(
                [SHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(PROJECT / script), "-DryRun", "-Python", sys.executable,
                 "-Root", str(output), *arguments],
                cwd=base, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", timeout=45,
            )
            self.assertFalse(output.exists(), result.stdout[-2000:])
            return result

    def test_both_launchers_forward_fractional_waits_to_real_cli(self):
        for script, count in (("run_collect.ps1", 1), ("run_collect_dual.ps1", 2)):
            with self.subTest(script=script):
                result = self.run_launcher(script, "-PageWaitMin", "1.5", "-PageWaitMax", "2.5")
                self.assertEqual(result.returncode, 0, result.stdout[-3000:])
                self.assertEqual(len(re.findall(r"翻页等待[^\r\n]*1\.5[^\r\n]*2\.5", result.stdout)), count)

    def test_invalid_waits_fail_before_cli_or_output_creation(self):
        for script in ("run_collect.ps1", "run_collect_dual.ps1"):
            for minimum, maximum in (("-1", "2"), ("5", "2"), ("0", "301"), ("NaN", "2")):
                with self.subTest(script=script, minimum=minimum, maximum=maximum):
                    result = self.run_launcher(script, "-PageWaitMin", minimum, "-PageWaitMax", maximum)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("current__", result.stdout)


@unittest.skipUnless(os.name == "nt" and SHELL, "Windows PowerShell is required")
class ProcessLauncherTests(unittest.TestCase):
    def run_process(self, *arguments):
        with tempfile.TemporaryDirectory(prefix="boss-jd-launcher-") as temporary:
            folder = Path(temporary)
            launcher = folder / "run_process.ps1"
            shutil.copyfile(PROJECT / "run_process.ps1", launcher)
            (folder / "process_architecture.py").write_text(
                "import json,sys\nprint('OFFLINE_ARGS='+json.dumps(sys.argv[1:]))\n", encoding="utf-8")
            output = folder / "must-stay-absent"
            result = subprocess.run(
                [SHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(launcher),
                 "-Python", sys.executable, "-Root", str(output), "-Stage", "fetch-jd", *arguments],
                cwd=folder, env=dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1"),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertFalse(output.exists())
            return result

    def test_process_launcher_forwards_zero_defaults_and_wait_boundaries(self):
        for minimum, maximum in ((0, 0), (0, 30), (30, 30)):
            with self.subTest(minimum=minimum, maximum=maximum):
                options = () if maximum == 0 else ("-JdPageWaitMin", str(minimum), "-JdPageWaitMax", str(maximum))
                result = self.run_process(*options)
                self.assertEqual(result.returncode, 0, result.stdout[-2000:])
                line = next(line for line in result.stdout.splitlines() if line.startswith("OFFLINE_ARGS="))
                args = json.loads(line.removeprefix("OFFLINE_ARGS="))
                self.assertEqual(args[args.index("--jd-page-wait-min") + 1], str(minimum))
                self.assertEqual(args[args.index("--jd-page-wait-max") + 1], str(maximum))

    def test_process_launcher_rejects_invalid_ranges_before_python_runs(self):
        for minimum, maximum in (("-1", "0"), ("0", "31"), ("5", "4"), ("True", "3")):
            with self.subTest(minimum=minimum, maximum=maximum):
                result = self.run_process("-JdPageWaitMin", minimum, "-JdPageWaitMax", maximum)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("OFFLINE_ARGS=", result.stdout)


if __name__ == "__main__":
    unittest.main()
