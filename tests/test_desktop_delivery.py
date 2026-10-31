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

    def task(self, name="delivery", count=45, *, confirmed=True, blocked=False, job_overrides=()):
        task = ReviewTask.create(self.source, self.base / "tasks", name)
        jobs = [{"job_id": f"{name}-{index}", "title": f"方案工程师 {index:03d}", "company": "合成公司",
                 "city": "上海", "salary": "20-30K", "job_link": f"https://www.zhipin.com/job_detail/synthetic-{name}-{index}.html",
                 "observed_in": [{"keyword": "合成检索词", "source_file": "raw/current/jobs.json"}]}
                for index in range(count)]
        for job, overrides in zip(jobs, job_overrides):
            job.update(overrides)
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
        self.assertIn("缺少 BOSS", self.page.detail.get("1.0", "end"))
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

    def test_confirmed_keep_rows_export_without_revalidating_ai_records(self):
        task = self.task(count=3)
        manifest = task.snapshot()
        result = task.path / manifest["stages"]["llm"]["result_file"]
        payload = json.loads(result.read_text(encoding="utf-8"))
        payload["jobs"][0].pop("llm_review")
        payload["jobs"][1]["llm_review"]["status"] = "failed"
        payload["jobs"][2]["llm_review"].update(job_id="old-record", cache_input_signature="old-signature")
        for row in payload["jobs"]:
            row.pop("llm_stage_projection", None)
            row.pop("llm_stage_decision", None)
        write_json(result, payload)
        before = result.read_bytes()
        manifest_before = (task.path / "task_manifest.json").read_bytes()

        self.bind(task)
        self.assertEqual(self.page._preview["summary"],
                         {"selected_count": 3, "ready_count": 3, "blocked_count": 0})
        self.assertIn("AI 判断与状态仅作记录", self.page.status_text.get())
        self.assertTrue(all(self.page.table.item(key, "values")[0] == "可导出"
                            for key in self.page.table.get_children()))
        self.page.full_export_button.invoke()
        self.idle()
        self.assertIsNotNone(self.page._latest_export, self.page.status_text.get())
        document = json.loads(self.page._latest_export.read_text(encoding="utf-8"))
        self.assertEqual(document["summary"]["ready_count"], 3)
        self.assertTrue(all(row["ai_review"]["validation"] == "not_checked" for row in document["jobs"]))
        self.assertEqual(result.read_bytes(), before)
        self.assertEqual((task.path / "task_manifest.json").read_bytes(), manifest_before)
        self.executor.assert_not_called()

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
            self.assertEqual(first.name, "格式版本_导出ID_导出时间.json")
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

    def test_published_json_survives_failed_followup_preview_and_opens_as_history(self):
        task = self.task(count=2)
        self.bind(task)
        original_export = delivery_export.export_task
        published = []

        def publish_then_change(*args, **kwargs):
            artifact = original_export(*args, **kwargs)
            published.append(artifact)
            task.set_keep("llm", ["delivery-0"], False)
            return artifact

        with patch.object(delivery_export, "export_task", side_effect=publish_then_change):
            self.page.export_button.invoke()
            self.idle()
        artifact = published[0]
        content = artifact.read_bytes()
        self.assertEqual(self.page._latest_export, artifact)
        self.assertFalse(self.page._latest_current)
        self.assertFalse(self.page._preview_current)
        self.assertIn("已导出", self.page.status_text.get())
        self.assertIn("刷新", self.page.status_text.get())
        self.assertIn(str(artifact), self.page.latest_text.get())
        self.assertIn("历史", self.page.latest_text.get())
        self.assertTrue(self.page.export_button.instate(["disabled"]))
        self.assertFalse(self.page.open_json_button.instate(["disabled"]))
        self.assertIn("历史", self.page.open_json_button.cget("text"))
        # Opening a known historical artifact requires no valid current task
        # preview and does not pretend to revalidate its old selection.
        with patch.object(delivery_export, "preview_task", side_effect=AssertionError("history read current preview")):
            self.page.open_json_button.invoke()
            self.idle()
            self.page.open_path.assert_called_with(artifact)
            self.page.open_directory_button.invoke()
            self.idle()
            self.page.open_path.assert_called_with(artifact.parent)
        task.confirm("llm")
        self.page.refresh_preview()
        self.idle()
        self.assertTrue(self.page._preview_current)
        self.assertFalse(self.page._latest_current)
        self.assertEqual(self.page._latest_export, artifact)
        self.assertIn("最终保留 1", self.page.summary_text.get())
        self.assertEqual(artifact.read_bytes(), content)

    def test_switching_source_keeps_success_as_history_until_new_source_is_exported(self):
        current, other = self.task("current", 2), self.task("other", 1)
        self.bind(current)
        self.page.export()
        self.idle()
        artifact = self.page._latest_export
        self.assertTrue(self.page._latest_current)
        self.page.open_task(other.path)
        self.idle()
        self.assertTrue(self.page._preview_current)
        self.assertFalse(self.page._latest_current)
        self.assertEqual(self.page._latest_export, artifact)
        self.assertEqual(self.page._latest_source, current.path)
        self.assertIn("历史", self.page.latest_text.get())
        self.page.open_json_button.invoke()
        self.idle()
        self.page.open_path.assert_called_with(artifact)
        self.page.export()
        self.idle()
        self.assertTrue(self.page._latest_current)
        self.assertEqual(self.page._latest_source, other.path)
        self.assertNotEqual(self.page._latest_export, artifact)

    def test_source_revision_change_during_publish_keeps_committed_history(self):
        task = self.task(count=2)
        self.bind(task)
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        original_export = delivery_export.export_task
        published = []

        def pause_after_publish(*args, **kwargs):
            artifact = original_export(*args, **kwargs)
            published.append(artifact)
            entered.set()
            release.wait(4)
            return artifact

        with patch.object(delivery_export, "export_task", side_effect=pause_after_publish):
            self.page.export()
            self.assertTrue(entered.wait(2))
            self.page.set_current_task(task.path, revision=999)
            release.set()
            self.idle()
        self.assertEqual(self.page._latest_export, published[0])
        self.assertTrue(published[0].is_file())
        self.assertFalse(self.page._preview_current)
        self.assertFalse(self.page._latest_current)
        self.assertIn("已导出", self.page.status_text.get())
        self.assertFalse(self.page.open_json_button.instate(["disabled"]))

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
        self.assertFalse(list((task.path / "delivery-exports").rglob("*.json")))
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
        fields = self.page._export_fields
        with patch.object(delivery_export, "available_export_fields", side_effect=AssertionError("rescanned unchanged preview")), \
             patch.object(delivery_export, "compact_export_record", side_effect=AssertionError("recompacted unchanged rows")):
            self.page.refresh_preview()
            self.idle()
        self.assertIs(self.page._prepared_rows, prepared)
        self.assertIs(self.page._source_reader, reader)
        self.assertIs(self.page._export_fields, fields)
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

    def test_selected_fields_export_only_checked_nested_values_and_full_export_is_independent(self):
        task = self.task(count=3, blocked=True)
        self.bind(task)
        self.assertEqual(self.page.export_button.cget("text"), "导出指定信息")
        self.assertEqual(self.page.full_export_button.cget("text"), "导出完整 JSON")
        self.page.choose_fields_button.invoke()
        dialog = self.page._field_dialog
        self.assertIsNotNone(dialog)
        self.assertEqual(dialog.fields, delivery_export.available_export_fields(self.page._preview))
        dialog.clear_button.invoke()
        chosen = ("source_task.name", "jobs.title", "jobs.ai_review.reason", "blocked_jobs.blocked_reasons")
        for field in chosen:
            dialog.checkboxes[field].invoke()
        dialog.actions.buttons["save"].invoke()
        self.assertIsNone(self.page._field_dialog)
        self.assertEqual(set(self.page._selected_fields), set(chosen))
        self.page.export_button.invoke()
        self.idle()
        selected_path = self.page._latest_export
        selected_bytes = selected_path.read_bytes()
        document = json.loads(selected_bytes)
        self.assertEqual(set(document), {"source_task", "jobs", "blocked_jobs"})
        self.assertEqual(document["source_task"], {"name": "delivery"})
        self.assertEqual(len(document["jobs"]), 2)
        for job in document["jobs"]:
            self.assertEqual(set(job), {"title", "ai_review"})
            self.assertEqual(set(job["ai_review"]), {"reason"})
        self.assertEqual(document["blocked_jobs"], [{"blocked_reasons": ["missing_boss_identifier"]}])
        self.assertIn("指定信息", self.page.latest_text.get())
        self.page.open_json_button.invoke()
        self.idle()
        self.page.open_path.assert_called_with(selected_path)
        self.page.full_export_button.invoke()
        self.idle()
        full_path = self.page._latest_export
        full_document = json.loads(full_path.read_text(encoding="utf-8"))
        delivery_export.validate_export(full_document)
        self.assertIn("job_url", full_document["jobs"][0])
        self.assertIn("完整 JSON", self.page.latest_text.get())
        self.assertEqual(set(self.page._selected_fields), set(chosen))
        self.assertNotEqual(selected_path, full_path)
        self.assertEqual(selected_path.read_bytes(), selected_bytes)
        self.page.export_button.invoke()
        self.idle()
        self.assertEqual(json.loads(self.page._latest_export.read_text(encoding="utf-8")), document)

    def test_field_dialog_groups_search_draft_cancel_and_variable_cleanup(self):
        self.bind(self.task(count=1))
        original = self.page._selected_fields
        self.page.choose_fields()
        dialog = self.page._field_dialog
        value = dialog.values["jobs.title"]
        dialog.checkboxes["jobs"].invoke()
        self.assertFalse(any(dialog.values[field].get() for field in dialog.fields if field.startswith("jobs.")))
        dialog.checkboxes["jobs.title"].invoke()
        self.assertTrue(dialog.checkboxes["jobs"].instate(["alternate"]))
        dialog.checkboxes["jobs"].invoke()
        self.assertTrue(all(dialog.values[field].get() for field in dialog.fields if field.startswith("jobs.")))
        self.assertFalse(dialog.checkboxes["jobs"].instate(["alternate"]))
        dialog.clear_button.invoke()
        dialog.checkboxes["jobs.ai_review.judgment"].invoke()
        dialog.search.set("判断")
        self.pump()
        self.assertEqual({path for path in dialog._visible_paths if path in dialog.fields},
                         {"jobs.ai_review.judgment"})
        self.assertNotIn("blocked_jobs", dialog.checkboxes)
        self.assertTrue(dialog.values["jobs.ai_review.judgment"].get())
        dialog.search.set("source_task.review_stages")
        self.assertIn("source_task.review_stages.fetch-jd", dialog._visible_paths)
        dialog.select_all_button.invoke()
        self.assertTrue(all(dialog.values[field].get() for field in dialog.fields))
        dialog.actions.buttons["cancel"].invoke()
        self.assertIsNone(self.page._field_dialog)
        self.assertEqual(self.page._selected_fields, original)
        self.assertIsNone(value._tk)
        self.assertFalse((self.page._source_path / "delivery-exports").exists())

    def test_empty_task_picker_omits_job_groups_and_exports_real_zero_counts(self):
        self.bind(self.task(count=0))
        self.page.choose_fields()
        dialog = self.page._field_dialog
        dialog.clear_button.invoke()
        self.assertTrue(dialog.actions.buttons["save"].instate(["disabled"]))
        self.assertIn("至少", dialog.count_text.get())
        dialog.save()
        self.assertIs(self.page._field_dialog, dialog)
        self.assertNotIn("jobs", dialog.checkboxes)
        self.assertNotIn("blocked_jobs", dialog.checkboxes)
        self.assertFalse(any(field.startswith(("jobs.", "blocked_jobs.")) for field in dialog.fields))
        dialog.checkboxes["summary.ready_count"].invoke()
        self.assertFalse(dialog.actions.buttons["save"].instate(["disabled"]))
        dialog.actions.buttons["save"].invoke()
        self.page.export()
        self.idle()
        self.assertIsNotNone(self.page._latest_export, self.page.status_text.get())
        self.assertEqual(json.loads(self.page._latest_export.read_text(encoding="utf-8")), {"summary": {"ready_count": 0}})
        self.assertEqual(self.page._latest_export.name, "可导出数.json")
        self.assertTrue(self.page._preview_current)
        self.assertFalse(self.page.choose_fields_button.instate(["disabled"]))
        self.page.full_export_button.invoke()
        self.idle()
        document = json.loads(self.page._latest_export.read_text(encoding="utf-8"))
        self.assertNotIn("jobs", document)
        self.assertNotIn("blocked_jobs", document)
        self.assertEqual(document["summary"], {"selected_count": 0, "ready_count": 0, "blocked_count": 0})

    def test_selected_field_export_rejects_stale_preview_even_without_identity_fields(self):
        task = self.task(count=2)
        self.bind(task)
        self.page._apply_fields(("jobs.title",))
        task.set_keep("llm", ["delivery-0"], False)
        self.page.export_button.invoke()
        self.idle()
        self.assertIsNone(self.page._latest_export)
        self.assertFalse(list((task.path / "delivery-exports").rglob("*.json")))
        for button in (self.page.export_button, self.page.full_export_button, self.page.choose_fields_button):
            self.assertTrue(button.instate(["disabled"]))

    def test_field_dialog_selection_survives_preview_refresh_and_source_switch(self):
        first, other = self.task("first", 1), self.task("other", 1)
        self.bind(first)
        self.page._apply_fields(("jobs.title", "jobs.company"))
        self.page.refresh_preview()
        self.idle()
        self.page.open_task(other.path)
        self.idle()
        self.assertEqual(self.page._selected_fields, ("jobs.title", "jobs.company"))
        self.page.choose_fields()
        dialog = self.page._field_dialog
        self.assertTrue(dialog.values["jobs.title"].get())
        self.assertFalse(dialog.values["jobs.salary"].get())
        self.assertTrue(dialog.checkboxes["jobs"].instate(["alternate"]))
        self.app._set_theme("dark", persist=False)
        self.pump()
        self.assertEqual(str(dialog.canvas.cget("background")), self.page.palette["surface"])
        dialog.close()

    def test_picker_unions_actual_rows_and_detail_omits_empty_nested_fields(self):
        self.bind(self.task(count=2, job_overrides=[
            {"experience": " ", "skills": []}, {"experience": "3-5年", "skills": ["Python"]},
        ]))
        self.page.choose_fields()
        dialog = self.page._field_dialog
        self.assertIn("jobs.experience", dialog.fields)
        self.assertIn("jobs.skills", dialog.fields)
        self.assertNotIn("jobs.education", dialog.fields)
        self.assertNotIn("jobs.navigation.ka", dialog.fields)
        self.assertNotIn("blocked_jobs", dialog.checkboxes)
        dialog.close()
        first, second = self.page.table.get_children()
        self.page.table.selection_set(first)
        self.page._show_detail()
        first_detail = self.page.detail.get("1.0", "end")
        self.assertNotIn('"experience"', first_detail)
        self.assertNotIn('"manual_keep"', first_detail)
        self.assertNotIn('"ka"', first_detail)
        self.page.table.selection_set(second)
        self.page._show_detail()
        self.assertIn('"experience": "3-5年"', self.page.detail.get("1.0", "end"))
        self.assertIsNone(self.page._preview["jobs"][0]["experience"])
        self.assertIn("manual_keep", self.page._preview["jobs"][0]["selection"])

    def test_refresh_rebuilds_actual_fields_and_drops_disappeared_choices_without_reviving_them(self):
        task = self.task(count=2, job_overrides=[{}, {"experience": "3-5年"}])
        self.bind(task)
        self.page._apply_fields(("jobs.title", "jobs.experience"))
        task.set_keep("llm", ["delivery-1"], False)
        task.confirm("llm")
        self.page.refresh_preview()
        self.idle()
        self.assertNotIn("jobs.experience", self.page._export_fields)
        self.assertEqual(self.page._selected_fields, ("jobs.title",))
        task.set_keep("llm", ["delivery-1"], True)
        task.confirm("llm")
        self.page.refresh_preview()
        self.idle()
        self.assertIn("jobs.experience", self.page._export_fields)
        self.assertEqual(self.page._selected_fields, ("jobs.title",))
        self.page._apply_fields(("jobs.experience",))
        self.page.choose_fields()
        dialog = self.page._field_dialog
        variable = dialog.values["jobs.experience"]
        task.set_keep("llm", ["delivery-1"], False)
        task.confirm("llm")
        self.page.refresh_preview()
        self.idle()
        self.assertIsNone(self.page._field_dialog)
        self.assertIsNone(variable._tk)
        self.assertEqual(self.page._selected_fields, self.page._export_fields)
        self.assertNotIn("jobs.experience", self.page._selected_fields)
        self.assertFalse(self.page.export_button.instate(["disabled"]))
        self.page.export()
        self.idle()
        self.assertIsNotNone(self.page._latest_export, self.page.status_text.get())

    def test_switching_to_task_with_different_values_removes_previous_task_fields(self):
        first = self.task("first", 1, job_overrides=[{"experience": "3-5年"}])
        other = self.task("other", 1, job_overrides=[{"education": "本科"}])
        self.bind(first)
        self.page._apply_fields(("jobs.experience",))
        self.page.choose_fields()
        previous = self.page._field_dialog
        self.page.open_task(other.path)
        self.idle()
        self.assertIsNone(previous.tk)
        self.assertIsNone(self.page._field_dialog)
        self.assertNotIn("jobs.experience", self.page._export_fields)
        self.assertIn("jobs.education", self.page._export_fields)
        self.assertEqual(self.page._selected_fields, self.page._export_fields)
        self.assertFalse(self.page.export_button.instate(["disabled"]))
        self.page.choose_fields()
        self.assertNotIn("jobs.experience", self.page._field_dialog.checkboxes)
        self.assertIn("jobs.education", self.page._field_dialog.checkboxes)
        self.page._field_dialog.close()

    def test_field_picker_and_both_export_actions_obey_background_busy_guard(self):
        self.bind(self.task(count=1))
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        original = delivery_export.export_task

        def slow(*args, **kwargs):
            entered.set()
            release.wait(4)
            return original(*args, **kwargs)

        self.page._apply_fields(("jobs.title",))
        with patch.object(delivery_export, "export_task", side_effect=slow):
            self.page.export_button.invoke()
            self.assertTrue(entered.wait(2))
            for button in (self.page.export_button, self.page.full_export_button, self.page.choose_fields_button):
                self.assertTrue(button.instate(["disabled"]))
            self.page.choose_fields()
            self.assertIsNone(self.page._field_dialog)
            release.set()
            self.idle()
        self.assertEqual(set(json.loads(self.page._latest_export.read_text(encoding="utf-8"))), {"jobs"})

    def test_closing_workbench_closes_field_dialog_and_releases_selection_variables(self):
        self.bind(self.task(count=1))
        self.page.choose_fields()
        dialog = self.page._field_dialog
        value = dialog.values["jobs.title"]
        self.app._close()
        deadline = time.monotonic() + 5
        while not self.app._closed and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)
        self.assertTrue(self.app._closed)
        self.assertIsNone(value._tk)
        self.assertIsNone(self.page._field_dialog)

    def test_export_actions_and_scrollable_field_picker_fit_small_windows(self):
        self.bind(self.task(count=1))
        self.root.geometry("1024x720")
        self.root.deiconify()
        self.pump()
        for button in (self.page.choose_fields_button, self.page.export_button, self.page.full_export_button,
                       self.page.open_json_button, self.page.open_directory_button):
            self.assertGreater(button.winfo_width(), 1)
            self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                 self.root.winfo_rootx() + self.root.winfo_width())
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                 self.root.winfo_rooty() + self.root.winfo_height())
        self.page.choose_fields()
        dialog = self.page._field_dialog
        dialog.geometry("620x440")
        self.pump()
        self.assertGreater(dialog.canvas.winfo_height(), 80)
        self.assertGreater(dialog.fields_frame.winfo_height(), dialog.canvas.winfo_height())
        for button in dialog.actions.buttons.values():
            self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                 dialog.winfo_rootx() + dialog.winfo_width())
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                 dialog.winfo_rooty() + dialog.winfo_height())
        last = dialog.fields[-1]
        dialog._reveal(last)
        self.pump()
        self.assertGreater(dialog.canvas.yview()[0], 0)
        dialog.close()

    def test_chinese_filename_preview_uses_first_three_schema_ordered_labels_and_matches_saved_name(self):
        self.bind(self.task(count=1))
        with patch.object(delivery_export, "export_field_paths", side_effect=AssertionError("Tk read schema")):
            self.page._apply_fields(("jobs.jd", "jobs.salary", "jobs.company", "jobs.title"))
        self.assertEqual(self.page._selected_fields, ("jobs.title", "jobs.company", "jobs.salary", "jobs.jd"))
        self.assertIn("岗位名称_公司_薪资.json", self.page.filename_text.get())
        self.page.export()
        self.idle()
        self.assertEqual(self.page._latest_export.name, "岗位名称_公司_薪资.json")
        self.page.open_json_button.invoke()
        self.idle()
        self.page.open_path.assert_called_with(self.page._latest_export)
        document = json.loads(self.page._latest_export.read_text(encoding="utf-8"))
        self.assertEqual(set(document["jobs"][0]), {"title", "company", "salary", "jd"})

    def test_absent_fields_never_enter_picker_and_populated_selection_preserves_existing_export(self):
        self.bind(self.task(count=2))
        self.page.full_export_button.invoke()
        self.idle()
        original_path = self.page._latest_export
        original_bytes = original_path.read_bytes()
        document = json.loads(original_bytes)
        self.assertNotIn("blocked_jobs", document)
        for job in document["jobs"]:
            self.assertNotIn("experience", job)
            self.assertNotIn("manual_keep", job["selection"])
            self.assertNotIn("ka", job["navigation"])
        self.page.choose_fields()
        dialog = self.page._field_dialog
        for field in ("jobs.experience", "jobs.selection.manual_keep", "jobs.navigation.ka", "source_task.city"):
            self.assertNotIn(field, dialog.fields)
            self.assertNotIn(field, dialog.checkboxes)
        self.assertNotIn("blocked_jobs", dialog.checkboxes)
        dialog.clear_button.invoke()
        dialog.checkboxes["jobs.title"].invoke()
        dialog.save()
        self.page.export()
        self.idle()
        self.assertEqual(self.page._latest_export.name, "岗位名称.json")
        self.assertEqual(list(json.loads(self.page._latest_export.read_text(encoding="utf-8"))), ["jobs"])
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertTrue(self.page._preview_current)
        self.assertTrue(self.page._latest_current)

    def test_company_metadata_from_review_is_selectable_and_exported_with_original_values(self):
        company_fields = {
            "encrypt_brand_id": "CompanyFixture~~",
            "company_link": "https://www.zhipin.com/gongsi/CompanyFixture~~.html",
            "company_industry": "计算机软件",
            "company_scale": "100-499人",
            "company_stage": "B轮",
        }
        self.bind(self.task(count=1, job_overrides=(company_fields,)))
        self.page.choose_fields()
        dialog = self.page._field_dialog
        selected = tuple("jobs." + name for name in company_fields)
        for field in selected:
            self.assertIn(field, dialog.fields)
        dialog.clear_button.invoke()
        for field in selected:
            dialog.checkboxes[field].invoke()
        dialog.save()
        self.page.export_button.invoke()
        self.idle()
        self.assertIsNotNone(self.page._latest_export, self.page.status_text.get())
        document = json.loads(self.page._latest_export.read_text(encoding="utf-8"))
        self.assertEqual(document, {"jobs": [company_fields]})
        self.assertEqual(self.page._preview["summary"],
                         {"selected_count": 1, "ready_count": 1, "blocked_count": 0})

    def test_legacy_api_company_name_is_available_for_export_but_dom_recruiter_is_not(self):
        metadata = {
            "company": None, "boss_name": "合成历史公司", "boss_title": "招聘经理", "salary_source": "api",
            "encrypt_brand_id": "LegacyCompany~~",
            "company_link": "https://www.zhipin.com/gongsi/LegacyCompany~~.html",
            "company_industry": "计算机软件", "company_scale": "100-499人", "company_stage": "B轮",
        }
        task = self.task(count=1, job_overrides=(metadata,))
        result = task.path / task.snapshot()["stages"]["llm"]["result_file"]
        original_result = result.read_bytes()
        original_manifest = (task.path / "task_manifest.json").read_bytes()
        self.bind(task)
        self.assertEqual(self.page._preview["jobs"][0]["company"], "合成历史公司")
        self.page.choose_fields()
        dialog = self.page._field_dialog
        self.assertIn("jobs.company", dialog.fields)
        dialog.clear_button.invoke()
        dialog.checkboxes["jobs.company"].invoke()
        dialog.save()
        self.page.export_button.invoke()
        self.idle()
        document = json.loads(self.page._latest_export.read_text(encoding="utf-8"))
        self.assertEqual(document, {"jobs": [{"company": "合成历史公司"}]})
        self.assertEqual(result.read_bytes(), original_result)
        self.assertEqual((task.path / "task_manifest.json").read_bytes(), original_manifest)

        dom = self.task(name="dom-recruiter", count=1,
                        job_overrides=({**metadata, "salary_source": "dom_untrusted", "boss_name": "招聘者李女士"},))
        self.bind(dom)
        self.assertIsNone(self.page._preview["jobs"][0]["company"])
        self.assertNotIn("jobs.company", self.page._export_fields)

    def test_experience_from_explicit_fields_and_legacy_labels_is_selectable_and_exported_readonly(self):
        task = self.task(count=4, job_overrides=(
            {"tags": "1-3年 | 本科"},
            {"tags": "经验不限 | 本科"},
            {"tags": "在校/应届 | 本科"},
            {"jobExperience": "3-5年", "tags": "5-10年 | 硕士"},
        ))
        protected = {task.path / "task_manifest.json": (task.path / "task_manifest.json").read_bytes()}
        for stage in ui.REVIEW_STAGES:
            result = task.path / task.stage_info(stage)["result_file"]
            protected[result] = result.read_bytes()
        expected = ["1-3年", "经验不限", "在校/应届", "3-5年"]
        self.bind(task)
        self.assertEqual([row["experience"] for row in self.page._preview["jobs"]], expected)
        self.page.choose_fields()
        dialog = self.page._field_dialog
        self.assertIn("jobs.experience", dialog.fields)
        dialog.clear_button.invoke()
        dialog.checkboxes["jobs.experience"].invoke()
        dialog.save()
        self.page.export_button.invoke()
        self.idle()
        self.assertIsNotNone(self.page._latest_export, self.page.status_text.get())
        self.assertEqual(self.page._latest_export.name, "经验要求.json")
        document = json.loads(self.page._latest_export.read_text(encoding="utf-8"))
        self.assertEqual(document, {"jobs": [{"experience": value} for value in expected]})
        self.page.full_export_button.invoke()
        self.idle()
        full = json.loads(self.page._latest_export.read_text(encoding="utf-8"))
        delivery_export.validate_export(full)
        self.assertEqual([row["experience"] for row in full["jobs"]], expected)
        self.assertTrue(all("experience_source" not in row for row in full["jobs"]))
        self.assertEqual({path: path.read_bytes() for path in protected}, protected)

        unknown = self.task(name="unknown-experience", count=1, job_overrides=({
            "tags": "本科 | 不限", "job_labels": ["薪资不限", "学历不限"],
            "observed_in": [{"experience_filter": "3-5年", "experience_id": "105"}],
        },))
        self.bind(unknown)
        self.assertIsNone(self.page._preview["jobs"][0]["experience"])
        self.assertNotIn("jobs.experience", self.page._export_fields)

    def test_open_path_accepts_generated_chinese_and_legacy_names_but_rejects_other_files(self):
        parent = self.base / "exports"
        folder = parent / "snapshot"
        folder.mkdir(parents=True)
        for name in ("岗位名称_公司_薪资.json", "格式版本_导出ID_导出时间.json", "delivery_jobs.json"):
            path = folder / name
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(desktop_delivery._validated_export_path(path, parent), path)
            self.assertEqual(desktop_delivery._validated_export_path(path, parent, directory=True), folder)
        for name in (".delivery_jobs.json.tmp", "README.md", ".hidden.json", "CON.json"):
            path = folder / name
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(desktop_tasks.TaskStateError):
                desktop_delivery._validated_export_path(path, parent)
        outside = self.base / "岗位名称.json"
        outside.write_text("{}", encoding="utf-8")
        with self.assertRaises(desktop_tasks.TaskStateError):
            desktop_delivery._validated_export_path(outside, parent)


if __name__ == "__main__":
    unittest.main()
