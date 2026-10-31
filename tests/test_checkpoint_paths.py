"""Offline path checks; namespace fixtures never contact a UNC server."""

import errno
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import checkpoint_io
import desktop_review_ui as ui
import desktop_tasks


class ComparisonPathTests(unittest.TestCase):
    def test_only_equivalent_windows_dos_and_unc_namespaces_are_folded(self):
        for ordinary, extended in (
            (r"C:\任务\prepared\llm_reviews.jsonl", r"\\?\C:\任务\prepared\llm_reviews.jsonl"),
            (r"c:\TASK\prepared\result.json", r"\\?\C:\task\PREPARED\result.json"),
            (r"\\server\share\任务\result.json", r"\\?\UNC\server\share\任务\result.json"),
            (r"\\SERVER\Share\task\result.json", r"\\?\unc\server\share\TASK\result.json"),
        ):
            with self.subTest(ordinary=ordinary):
                self.assertEqual(checkpoint_io._comparison_path(PureWindowsPath(ordinary)),
                                 checkpoint_io._comparison_path(PureWindowsPath(extended)))

    def test_devices_and_non_equivalent_or_ambiguous_forms_are_rejected(self):
        for value in (
            r"\\.\C:\任务\result.json", r"\\.\NUL", r"\??\C:\任务\result.json",
            r"\\?\GLOBALROOT\Device\HarddiskVolume1\任务\result.json",
            r"\\?\Volume{00000000-0000-0000-0000-000000000000}\任务\result.json",
            r"\\?\pipe\task", r"\\?\UNC\server", r"C:task\result.json",
            r"C:\任务\NUL", r"C:\任务\CON.txt", r"C:\任务\aux\result.json",
            r"\\?\C:\任务.\result.json", "C:\\任务 \\result.json", r"C:\任务\result.json:stream",
        ):
            with self.subTest(path=value), self.assertRaises(ValueError):
                checkpoint_io._comparison_path(PureWindowsPath(value))

    def test_non_windows_paths_keep_case_and_literal_backslashes(self):
        for value in ("/tmp/Task/result.json", r"/tmp/\\?\C:\task\result.json"):
            path = PurePosixPath(value)
            self.assertEqual(checkpoint_io._comparison_path(path), path)
        self.assertNotEqual(checkpoint_io._comparison_path(PurePosixPath("/tmp/Task")),
                            checkpoint_io._comparison_path(PurePosixPath("/tmp/task")))


class ConfinedPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.path = self.base / "prepared" / "result.json"

    def test_new_output_tail_is_allowed_but_missing_base_is_not(self):
        relative, resolved = checkpoint_io.confined_path(self.path, self.base)
        self.assertEqual(relative.parts, ("prepared", "result.json"))
        self.assertEqual(resolved, self.base.resolve() / "prepared" / "result.json")
        with self.assertRaises(checkpoint_io.PathCheckError):
            checkpoint_io.confined_path(self.path, self.base / "prepared")
        self.assertFalse(self.path.parent.exists())

    def test_parent_traversal_and_sibling_prefix_are_rejected_before_resolution(self):
        for path in (self.base / ".." / "outside.json", self.base.with_name(self.base.name + "-other") / "result.json"):
            with self.subTest(path=path), patch.object(Path, "resolve", side_effect=AssertionError("must reject lexically")):
                with self.assertRaises(checkpoint_io.PathBoundaryError):
                    checkpoint_io.confined_path(path, self.base)

    def test_unreliable_resolutions_never_fall_back_to_an_unchecked_path(self):
        for error in (PermissionError(errno.EACCES, "private exception body"),
                      OSError(errno.EIO, "private exception body"), RuntimeError("symlink loop"), ValueError("invalid path")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(Path, "resolve", side_effect=error) as resolve, \
                 patch.object(checkpoint_io.time, "sleep") as sleep:
                with self.assertRaises(checkpoint_io.PathCheckError) as raised:
                    checkpoint_io.confined_path(self.path, self.base)
                self.assertNotIn("private", str(raised.exception))
                resolve.assert_called_once_with(strict=True)
                sleep.assert_not_called()

    def test_existing_but_unresolvable_output_is_not_treated_as_missing(self):
        self.path.parent.mkdir()
        self.path.write_text("old", encoding="utf-8")
        error = FileNotFoundError(errno.ENOENT, "unresolvable existing entry")
        with patch.object(Path, "resolve", side_effect=error) as resolve:
            with self.assertRaises(checkpoint_io.PathCheckError):
                checkpoint_io.confined_path(self.path, self.base)
        resolve.assert_called_once_with(strict=True)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old")

    def test_output_below_a_regular_file_is_not_a_missing_directory(self):
        self.path.parent.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(checkpoint_io.PathCheckError):
            checkpoint_io.confined_path(self.path, self.base)

    def test_non_absolute_or_incomplete_resolve_results_are_not_trusted(self):
        for result in (Path("relative-result.json"), self.base / ".." / "outside.json"):
            with self.subTest(result=result):
                def resolve(path, *, strict=False):
                    return self.base if path == self.base else result
                with patch.object(Path, "resolve", autospec=True, side_effect=resolve):
                    with self.assertRaises(checkpoint_io.PathCheckError):
                        checkpoint_io.confined_path(self.path, self.base)

    def test_known_windows_transient_errors_retry_only_within_the_short_bound(self):
        native_resolve = Path.resolve
        for winerror in (2, 3, 5, 32, 33):
            with self.subTest(winerror=winerror):
                error = OSError(errno.EACCES, "temporary lookup error")
                error.winerror = winerror
                calls = []

                def resolve(path, *, strict=False):
                    calls.append(path)
                    if len(calls) == 1:
                        raise error
                    return native_resolve(path, strict=strict)

                with patch.object(checkpoint_io, "os", SimpleNamespace(name="nt")), \
                     patch.object(Path, "resolve", autospec=True, side_effect=resolve), \
                     patch.object(checkpoint_io.time, "sleep") as sleep:
                    checkpoint_io.confined_path(self.path, self.base)
                sleep.assert_called_once_with(checkpoint_io.PATH_CHECK_RETRY_DELAYS[0])
                with patch.object(checkpoint_io, "os", SimpleNamespace(name="nt")), \
                     patch.object(Path, "resolve", side_effect=error) as resolve, \
                     patch.object(checkpoint_io.time, "sleep") as sleep:
                    with self.assertRaises(checkpoint_io.PathCheckError):
                        checkpoint_io.confined_path(self.path, self.base)
                    self.assertEqual(resolve.call_count, len(checkpoint_io.PATH_CHECK_RETRY_DELAYS) + 1)
                    self.assertEqual([call.args[0] for call in sleep.call_args_list], list(checkpoint_io.PATH_CHECK_RETRY_DELAYS))

    def test_ui_fails_closed_on_resolution_or_reparse_inspection_errors(self):
        self.path.parent.mkdir()
        self.path.write_text("{}", encoding="utf-8")
        task = SimpleNamespace(path=self.base)
        with patch.object(Path, "resolve", side_effect=OSError(errno.EIO, "unavailable")):
            self.assertIsNone(ui._confined_path(task, self.path))
        with patch.object(Path, "lstat", side_effect=PermissionError(errno.EACCES, "unavailable")):
            self.assertIsNone(ui._confined_path(task, self.path))

    @unittest.skipUnless(os.name == "nt", "Windows extended namespaces")
    def test_task_and_ui_boundaries_accept_mixed_resolve_prefixes(self):
        artifact = self.base / "stages" / "llm" / ("results-" + "a" * 32 + ".json")
        artifact.parent.mkdir(parents=True)
        artifact.write_text("{}", encoding="utf-8")
        runtime = self.base / "runtime" / "llm"
        runtime.mkdir(parents=True)
        task = object.__new__(desktop_tasks.ReviewTask)
        task._path = self.base

        def resolve(path, *, strict=False):
            self.assertTrue(strict)
            return path if path == self.base else Path("\\\\?\\" + str(path))

        with patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            self.assertEqual(task._artifact_path("llm", artifact.relative_to(self.base).as_posix()), artifact)
            self.assertEqual(task._runtime_path("llm", "runtime/llm"), runtime)
            self.assertEqual(task._redo_cleanup_targets("llm"), ([runtime], [artifact]))
            self.assertEqual(ui._confined_path(task, artifact), Path("\\\\?\\" + str(artifact)))


if __name__ == "__main__":
    unittest.main()
