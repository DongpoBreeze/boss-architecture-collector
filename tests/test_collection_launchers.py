"""Exercise collection PowerShell argument forwarding with offline dry runs."""

from __future__ import annotations

import os
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


if __name__ == "__main__":
    unittest.main()
