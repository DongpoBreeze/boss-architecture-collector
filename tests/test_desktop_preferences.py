"""Shared storage, concurrency and import-boundary tests; no user settings."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import desktop_preferences as preferences
import desktop_ui


class DesktopPreferencesTests(unittest.TestCase):
    def test_shared_and_legacy_imports_keep_the_same_whitelist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            desktop_ui.save_preferences({"city": "上海", "jd_workers": 5, "theme": "dark",
                                         "api_key": "PRIVATE_KEY", "cookie": "PRIVATE_COOKIE"}, path)
            expected = {"city": "上海", "jd_workers": "5", "theme": "dark"}
            self.assertEqual(preferences.load_preferences(path), expected)
            self.assertEqual(desktop_ui.load_preferences(path), expected)
            self.assertEqual(desktop_ui.PREFERENCE_FIELDS, preferences.PREFERENCE_FIELDS)
            self.assertNotIn("PRIVATE", path.read_text(encoding="utf-8"))
            path.write_text('{"workers":3,"theme":"unknown","pages":"10","token":"PRIVATE"}', encoding="utf-8")
            self.assertEqual(preferences.load_preferences(path), {"theme": "light", "pages": "10"})

    def test_legacy_preference_path_patch_remains_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-settings.json"
            with patch.object(desktop_ui, "preference_path", return_value=path):
                desktop_ui.save_preferences({"city": "杭州"})
                self.assertEqual(desktop_ui.load_preferences(), {"city": "杭州"})

    def test_concurrent_writers_use_unique_temporaries_and_never_mix_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "settings.json"
            barrier = threading.Barrier(2)
            original_replace = os.replace
            sources, payloads, errors = [], [], []
            lock = threading.Lock()
            values = ({"city": "上海", "workers": "17"}, {"city": "杭州", "workers": "23"})

            def replace(source, destination):
                with lock:
                    first_attempt = Path(source) not in sources
                    if first_attempt:
                        sources.append(Path(source))
                        payloads.append(json.loads(Path(source).read_text(encoding="utf-8")))
                if first_attempt:
                    barrier.wait(timeout=5)
                original_replace(source, destination)

            def save(value):
                try:
                    preferences.save_preferences(value, target)
                except Exception as error:
                    errors.append(error)

            with patch.object(preferences.os, "replace", side_effect=replace):
                threads = [threading.Thread(target=save, args=(value,)) for value in values]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=6)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(set(sources)), 2)
            self.assertCountEqual(payloads, values)
            self.assertIn(preferences.load_preferences(target), values)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_transient_windows_sharing_error_is_retried_with_the_same_unique_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "settings.json"
            original_replace = os.replace
            sharing_error = PermissionError("sharing violation")
            sharing_error.winerror = 32
            attempts = []

            def replace(source, destination):
                attempts.append(Path(source))
                if len(attempts) == 1:
                    raise sharing_error
                original_replace(source, destination)

            with patch.object(preferences.os, "replace", side_effect=replace), \
                    patch.object(preferences.time, "sleep") as sleep:
                preferences.save_preferences({"city": "上海"}, target)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(len(set(attempts)), 1)
            sleep.assert_called_once_with(.005)
            self.assertEqual(preferences.load_preferences(target), {"city": "上海"})

    def test_failed_replace_preserves_prior_settings_and_cleans_only_own_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "settings.json"
            preferences.save_preferences({"city": "上海"}, target)
            original = target.read_bytes()
            unrelated = Path(directory) / ".another-window.tmp"
            unrelated.write_text("do not touch", encoding="utf-8")
            with patch.object(preferences.os, "replace", side_effect=OSError("permission denied")):
                with self.assertRaises(OSError):
                    preferences.save_preferences({"city": "杭州"}, target)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob(".desktop-settings-*.tmp")), [])
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "do not touch")

    def test_shared_preferences_and_review_import_do_not_load_legacy_ui(self):
        code = ("import sys; import desktop_preferences; "
                "assert 'desktop_ui' not in sys.modules; "
                "import desktop_review_ui; assert 'desktop_ui' not in sys.modules")
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "CODEX_HOME": directory, "LOCALAPPDATA": directory,
                           "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, "-B", "-c", code], cwd=PROJECT_ROOT, env=environment,
                                    capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
