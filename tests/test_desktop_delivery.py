"""Three-page delivery workflow; synthetic files and real Tk, no network."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import delivery_export
import desktop_delivery
import desktop_review_ui as ui
import desktop_tasks
from desktop_tasks import ReviewTask
from desktop_tk_lifecycle import StringVar
from review_pipeline import _analyze_ai_stage
from process_architecture import model_input
from review_cache import review_input_signature


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class DeliveryTkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "source"
        for track in ("current", "target"):
            write_json(self.source / "raw" / track / "jobs.json", [])
        for patcher in (
            patch.object(ui, "load_preferences", return_value={}),
            patch.object(ui, "save_preferences"),
            patch("project_llm_settings.default_settings_path", return_value=self.base / "project-llm.json"),
            patch.object(ui.messagebox, "askyesno", return_value=True),
            patch.object(ui.messagebox, "showerror"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        try:
            self.root = ui.tk.Tk()
        except ui.tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.root.withdraw()
        self.root.attributes("-alpha", 0.0)
        if sys.platform == "win32":
            self.root.attributes("-disabled", True)
        self.callback_errors = []
        self.root.report_callback_exception = lambda *error: self.callback_errors.append(error)
        self.executor = Mock()
        self.app = ui.ReviewDesktopApp(self.root, argparse.Namespace(root=self.source, config=ui.DEFAULT_CONFIG,
                                      scraper_root=None, task_parent=self.base / "tasks"), executor=self.executor)
        self.page = self.app.delivery_page
        self.page.open_path = Mock()
        self.gates = []
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        for gate in self.gates:
            gate.set()
        if not self.app._closed:
            self.app._close()
            deadline = time.monotonic() + 5
            while not self.app._closed and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
            self.assertTrue(self.app._closed, "workbench did not release its background workers")

    def pump(self, seconds=.05):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(.003)
        self.assertEqual(self.callback_errors, [])

    def idle(self):
        deadline = time.monotonic() + 8
        while (self.app._busy or self.page.busy) and time.monotonic() < deadline:
            self.pump(.01)
        self.pump(.02)
        self.assertFalse(self.app._busy)
        self.assertFalse(self.page.busy)

    def task(self, name="delivery", count=45, *, confirmed=True, blocked=False):
        task = ReviewTask.create(self.source, self.base / "tasks", name)
        jobs = [{"job_id": f"{name}-{index}", "title": f"方案工程师 {index:03d}", "company": "合成公司",
                 "city": "上海", "salary": "20-30K", "job_link": f"https://www.zhipin.com/job_detail/synthetic-{name}-{index}.html",
                 "observed_in": [{"keyword": "合成检索词", "source_file": "raw/current/jobs.json"}]}
                for index in range(count)]
        if blocked and jobs:
            jobs[0].pop("job_link")
        prompt = "合成测试规则，仅判断当前岗位。\n"
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        prompt_path = task.path / "runtime/llm/review_prompts" / (digest + ".md")
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8", newline="\n")
        for stage in ui.REVIEW_STAGES:
            task.begin(stage)
            rows = copy.deepcopy(jobs) if stage == "import" else task.input_jobs(stage)
            if stage == "shortlist":
                for row in rows:
                    row.update(list_pass=True, list_queue="relevant")
            if stage == "fetch-jd":
                for row in rows:
                    row.update(jd="负责方案设计、系统集成与交付。", jd_completeness="complete")
            if stage == "llm":
                rows = [_analyze_ai_stage(row, {"job_id": row["job_id"], "status": "success", "review_format": "simple-v1",
                                               "判断": "符合", "理由": "合成岗位职责符合。",
                                               "cache_signature": "synthetic-provider-hash",
                                               "cache_input_signature": review_input_signature(model_input(row), prompt)}) for row in rows]
            task.finish(stage, rows, stats={"review_format": "simple-v1", "review_prompt_file": prompt_path.relative_to(task.path).as_posix()} if stage == "llm" else {})
            if stage != "llm" or confirmed:
                if blocked and stage == "llm" and rows:
                    task.set_keep("llm", [rows[0]["job_id"]], True)
                task.confirm(stage)
        return task

    def bind(self, task, *, preview=True):
        self.app._open_task(task.path)
        self.idle()
        if preview:
            self.app._show_workspace("delivery")
            self.idle()

    def test_three_pages_navigation_and_confirmation_never_export_or_generate_markdown(self):
        task = self.task(confirmed=False)
        self.bind(task, preview=False)
        before = {path: path.read_bytes() for path in task.path.rglob("*") if path.is_file()}
        with patch.object(delivery_export, "export_task") as export:
            for workspace in ("collection", "delivery", "review"):
                self.app._show_workspace(workspace)
                self.idle()
            self.assertFalse(self.page._preview_current)
            self.assertEqual(before, {path: path.read_bytes() for path in task.path.rglob("*") if path.is_file()})
            self.app._confirm_stage()
            self.idle()
            self.assertEqual(self.app.stage.get(), "llm")
            self.assertIn("最终保留", self.app.confirm_text.get())
            self.assertIn("03", self.app.notice.get())
            self.app.go_delivery_button.invoke()
            self.idle()
            export.assert_not_called()
        self.assertEqual(tuple(self.app.stage_buttons), ui.REVIEW_STAGES)
        self.assertEqual(self.app._rows.keys(), dict.fromkeys(ui.REVIEW_STAGES).keys())
        self.assertFalse(self.app.open_report_button.winfo_manager())
        self.assertFalse((task.path / "runtime/report").exists())
        self.assertFalse((task.path / "delivery-exports").exists())
        self.executor.assert_not_called()

    def test_preview_has_counts_block_reasons_and_preserves_pagination_focus_filter_theme(self):
        self.bind(self.task(blocked=True))
        self.assertTrue(self.page._preview_current, self.page.status_text.get())
        self.assertIn("最终保留 45", self.page.summary_text.get())
        self.assertIn("阻止导出 1", self.page.summary_text.get())
        self.page.filter.set("阻止导出")
        self.pump()
        self.assertEqual(len(self.page.table.get_children()), 1)
        blocked = self.page.table.get_children()[0]
        self.page.table.selection_set(blocked)
        self.pump()
        self.assertIn("BOSS", self.page.detail.get("1.0", "end"))
        self.assertIn("缺少 BOSS", self.page.table.item(blocked, "values")[-1])
        self.page.filter.set("全部")
        self.page.change_page(1)
        focus = self.page.table.get_children()[3]
        self.page.table.selection_set(focus)
        self.pump()
        for workspace in ("review", "collection", "delivery"):
            self.app._show_workspace(workspace)
        self.app._set_theme("dark", persist=False)
        self.page.refresh_preview()
        self.idle()
        self.assertEqual(self.page._page, 1)
        self.assertEqual(self.page.table.selection(), (focus,))
        self.assertEqual(self.page.detail.cget("background"), self.app.colors["surface"])
        self.assertEqual(self.page.filter.get(), "全部")

    def test_unconfirmed_and_stale_sources_are_unavailable_without_export_side_effects(self):
        task = self.task(confirmed=False)
        self.bind(task)
        self.assertFalse(self.page._preview_current)
        self.assertTrue(self.page.export_button.instate(["disabled"]))
        self.assertIn("确认", self.page.status_text.get())
        task.confirm("llm")
        self.page.refresh_preview()
        self.idle()
        task.set_keep("shortlist", ["delivery-0"], False)
        self.page.refresh_preview()
        self.idle()
        self.assertFalse(self.page._preview_current)
        self.assertTrue(self.page.open_json_button.instate(["disabled"]))
        self.assertFalse((task.path / "delivery-exports").exists())

    def test_manual_source_is_independent_and_invalid_directory_is_never_opened(self):
        current, other = self.task("current", 2), self.task("other", 3)
        self.bind(current)
        self.page.open_task(other.path)
        self.idle()
        self.assertEqual(self.app.task.path, current.path)
        self.assertEqual(self.page._source_path, other.path)
        self.assertIn("不影响 02", self.page.source_text.get())
        self.assertIn("最终保留 3", self.page.summary_text.get())
        self.app._refresh_task()
        self.idle()
        self.assertEqual(self.page._source_path, other.path)
        self.page.open_task(self.source)
        self.idle()
        self.assertFalse(self.page._preview_current)
        self.page.open_path.assert_not_called()
        self.assertEqual(self.app.task.path, current.path)
        self.page.use_current_task()
        self.idle()
        self.assertEqual(self.page._source_path, current.path)

    def test_export_and_open_are_background_operations_with_immutable_history(self):
        task = self.task(count=3)
        self.bind(task)
        thread_ids = []
        original_open = ReviewTask.open_readonly
        original_get = StringVar.get
        self.page._source_reader = None  # Verify a worker opens the source when not cached.

        def record_open(*args, **kwargs):
            thread_ids.append(threading.get_ident())
            return original_open(*args, **kwargs)

        def tk_get(variable):
            self.assertEqual(threading.get_ident(), self.app.ui_thread_id, "worker accessed a Tk variable")
            return original_get(variable)

        with patch.object(ReviewTask, "open_readonly", side_effect=record_open), patch.object(StringVar, "get", new=tk_get):
            self.page.export_button.invoke()
            self.idle()
            first = self.page._latest_export
            self.assertIsNotNone(first, self.page.status_text.get())
            self.assertEqual(first.name, "delivery_jobs.json")
            content = first.read_bytes()
            self.page.open_json_button.invoke()
            self.idle()
            self.page.open_path.assert_called_with(first)
            self.page.open_directory_button.invoke()
            self.idle()
            self.page.open_path.assert_called_with(first.parent)
            chosen = self.base / "chosen-exports"
            chosen.mkdir()
            self.page.output_parent.set(str(chosen))
            self.page.export_button.invoke()
            self.idle()
        self.assertNotEqual(first, self.page._latest_export)
        self.assertEqual(first.read_bytes(), content)
        self.assertTrue(self.page._latest_export.is_relative_to(chosen))
        self.assertTrue(thread_ids)
        self.assertTrue(all(value != self.app.ui_thread_id for value in thread_ids))

    def test_slow_export_keeps_navigation_theme_and_tk_alive_and_blocks_other_execution(self):
        self.bind(self.task(count=2))
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        original = delivery_export.export_task

        def slow(*args, **kwargs):
            entered.set()
            release.wait(4)
            return original(*args, **kwargs)

        with patch.object(delivery_export, "export_task", side_effect=slow):
            before = time.monotonic()
            self.page.export_button.invoke()
            self.assertLess(time.monotonic() - before, .2)
            self.assertTrue(entered.wait(2))
            self.assertFalse(self.app._can_start_collection())
            self.app._show_workspace("review")
            self.app._run_stage()
            self.assertTrue(self.app.run_button.instate(["disabled"]))
            self.app._show_workspace("collection")
            self.assertEqual(self.app.workspace.get(), "collection")
            self.app._show_workspace("delivery")
            self.app._set_theme("dark", persist=False)
            ticks = []
            self.root.after(1, lambda: ticks.append(True))
            self.pump()
            self.assertEqual(ticks, [True])
            self.executor.assert_not_called()
            release.set()
            self.idle()
        self.assertTrue(self.page._latest_current, self.page.status_text.get())

    def test_edit_between_preview_export_and_open_never_publishes_or_opens_stale_current(self):
        task = self.task(count=2)
        self.bind(task)
        task.set_keep("llm", ["delivery-0"], False)
        self.page.export()
        self.idle()
        self.assertFalse(self.page._latest_current)
        self.assertIsNone(self.page._latest_export)
        self.assertFalse(list(task.path.rglob("delivery_jobs.json")))
        task.confirm("llm")
        self.page.refresh_preview()
        self.idle()
        self.page.export()
        self.idle()
        artifact = self.page._latest_export
        saved = artifact.read_bytes()
        task.set_keep("llm", ["delivery-1"], False)
        task.confirm("llm")
        self.page.open_json_button.invoke()
        self.idle()
        self.page.open_path.assert_not_called()
        self.assertFalse(self.page._latest_current)
        self.assertEqual(artifact.read_bytes(), saved)

    def test_f5_refreshes_only_visible_page_and_preserves_last_delivery_status(self):
        self.bind(self.task(count=2))
        with patch.object(self.app.collection_page, "refresh_progress") as collection, \
             patch.object(self.app, "_refresh_task") as review, \
             patch.object(self.page, "refresh_preview") as delivery:
            for name, expected in (("collection", collection), ("review", review), ("delivery", delivery)):
                self.app._show_workspace(name)
                expected.reset_mock()
                self.assertEqual(self.app._refresh_workspace(), "break")
                expected.assert_called_once()
                for other in (collection, review, delivery):
                    if other is not expected:
                        other.assert_not_called()
                expected.reset_mock()
        status = self.page.status_text.get()
        self.app._show_workspace("review")
        self.app._show_workspace("delivery")
        self.assertEqual(self.page.status_text.get(), status)

    def test_unchanged_preview_reuses_background_search_index(self):
        self.bind(self.task(count=45))
        prepared = self.page._prepared_rows
        reader = self.page._source_reader
        self.page.refresh_preview()
        self.idle()
        self.assertIs(self.page._prepared_rows, prepared)
        self.assertIs(self.page._source_reader, reader)
        self.page.search.set("方案工程师")
        self.page.change_page(1)
        self.assertIs(self.page._prepared_rows, prepared)
        self.assertLessEqual(len(self.page.table.get_children()), desktop_delivery.PAGE_SIZE)

    def test_review_revision_change_ignores_inflight_old_preview(self):
        task = self.task(count=2)
        self.bind(task, preview=False)
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        original = delivery_export.preview_task

        def delayed(*args, **kwargs):
            value = original(*args, **kwargs)
            entered.set()
            release.wait(4)
            return value

        with patch.object(delivery_export, "preview_task", side_effect=delayed):
            self.page.refresh_preview()
            self.assertTrue(entered.wait(2))
            self.page.set_current_task(task.path, revision=999)
            release.set()
            self.idle()
        self.assertFalse(self.page._preview_current)
        self.assertTrue(self.page.export_button.instate(["disabled"]))
        self.assertFalse((task.path / "delivery-exports").exists())

    def test_review_refresh_never_reads_report_checkpoint_or_legacy_artifact(self):
        task = self.task(count=2)
        task.begin("report")
        task.finish("report", [{"job_id": "delivery-0", "title": "legacy report poison"}, {"job_id": "delivery-1"}])
        checkpoint = task.path / task.stage_info("report")["result_file"]
        checkpoint.write_text("not valid JSON", encoding="utf-8")
        with patch.object(desktop_tasks, "_read_json", wraps=desktop_tasks._read_json) as reads, \
             patch.object(ui, "_capture_report", side_effect=AssertionError("normal UI tried to capture report")):
            self.bind(task, preview=False)
            self.app._refresh_task()
            self.idle()
        self.assertNotIn(checkpoint, [call.args[0] for call in reads.call_args_list])
        self.assertEqual(tuple(self.app._rows), ui.REVIEW_STAGES)
        self.assertNotIn("legacy report poison", json.dumps(self.app._rows, ensure_ascii=False))
        self.assertNotIn("markdown", self.app._artifacts)
        self.app._select_stage("report")
        self.assertEqual(self.app.stage.get(), "llm")
        self.app._run_stage()
        self.idle()
        self.assertFalse(any(call.args[1] == "report" for call in self.executor.call_args_list))

    def test_close_waits_for_export_and_releases_managed_tk_variables(self):
        self.bind(self.task(count=2))
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        original = delivery_export.export_task

        def slow(*args, **kwargs):
            entered.set()
            release.wait(4)
            return original(*args, **kwargs)

        with patch.object(delivery_export, "export_task", side_effect=slow):
            self.page.export()
            self.assertTrue(entered.wait(2))
            self.app._close()
            self.assertFalse(self.app._closed)
            self.assertTrue(self.page._closing)
            release.set()
            deadline = time.monotonic() + 6
            while not self.app._closed and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
        self.assertTrue(self.app._closed)
        self.assertIsNone(self.page.status_text._tk)
        self.assertTrue(self.page.shutdown_complete)
        self.assertEqual(self.callback_errors, [])


if __name__ == "__main__":
    unittest.main()
