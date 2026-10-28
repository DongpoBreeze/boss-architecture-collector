"""The normal launcher opens review; collection is a deliberate separate entry."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import desktop_app
import desktop_review_ui


class DesktopReviewEntryTests(unittest.TestCase):
    def test_default_entry_is_review_and_does_not_launch_collection(self):
        window = Mock()
        with patch.object(desktop_app.tk, "Tk", return_value=window), \
             patch.object(desktop_review_ui, "ReviewDesktopApp") as review, \
             patch.object(desktop_app, "CollectorDesktopApp") as collector:
            result = desktop_app.main(["--root", "fixed-source"])
        self.assertEqual(result, 0)
        review.assert_called_once()
        self.assertIs(review.call_args.args[0], window)
        self.assertEqual(review.call_args.args[1].root, Path("fixed-source"))
        collector.assert_not_called()
        window.mainloop.assert_called_once()

    def test_legacy_workbench_remains_an_explicit_separate_entry(self):
        window = Mock()
        with patch.object(desktop_app.tk, "Tk", return_value=window), \
             patch.object(desktop_review_ui, "ReviewDesktopApp") as review, \
             patch.object(desktop_app, "CollectorDesktopApp") as collector:
            self.assertEqual(desktop_app.main(["--legacy-workbench"]), 0)
        collector.assert_called_once()
        review.assert_not_called()
        window.mainloop.assert_called_once()

    def test_path_arguments_and_default_mode_are_preserved(self):
        args = desktop_app.parse_args(["--root", "数据源", "--scraper-root", "采集器", "--config", "配置.json"])
        self.assertFalse(args.legacy_workbench)
        self.assertEqual(args.root, Path("数据源"))
        self.assertEqual(args.scraper_root, "采集器")
        self.assertEqual(args.config, Path("配置.json"))


if __name__ == "__main__":
    unittest.main()
