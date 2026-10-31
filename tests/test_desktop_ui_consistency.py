"""Independent acceptance checks for shared desktop controls; no external services."""

import copy
import gc
import re
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_desktop_delivery as delivery_fixtures
import test_desktop_review_ui as review_fixtures

import desktop_collection
import desktop_controls
import desktop_delivery

ui = review_fixtures.ui
pipeline = review_fixtures.review_pipeline
ReviewTask = review_fixtures.ReviewTask
job_id = review_fixtures.job_id


def descendants(widget):
    yield widget
    for child in tuple(widget.winfo_children()):
        yield from descendants(child)


def hide_dialog(dialog):
    dialog.attributes("-alpha", 0.0)
    if sys.platform == "win32":
        dialog.attributes("-disabled", True)


def invoke_key_binding(widget, sequence):
    """Exercise the real registered callback without focusing a user's desktop."""
    script = widget.bind(sequence)
    command = re.search(r"\[([^\s]+)", script).group(1)
    event = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "", 0, "Escape", 0, str(widget), "2", 0, 0, 0)
    return widget.tk.call(command, *event)


class ReviewConsistencyTests(unittest.TestCase):
    # Reuse setup/helpers only: inheriting ReviewTkTests would rerun its suite.
    setUp = review_fixtures.ReviewTkTests.setUp
    make_window = review_fixtures.ReviewTkTests.make_window
    _cleanup_windows = review_fixtures.ReviewTkTests._cleanup_windows
    pump = review_fixtures.ReviewTkTests.pump
    wait_idle = review_fixtures.ReviewTkTests.wait_idle
    fake_executor = review_fixtures.ReviewTkTests.fake_executor
    create_task = review_fixtures.ReviewTkTests.create_task
    complete_through = review_fixtures.ReviewTkTests.complete_through
    task_json = review_fixtures.ReviewTkTests.task_json

    def wait_until(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(.005)
        self.fail("background UI did not reach the expected state")

    def test_all_four_select_page_highlights_only_until_explicit_keep_or_exclude(self):
        self.complete_through("llm", confirm_last=True)
        app, task = self.app, self.app.task
        manifest_path = task.path / "task_manifest.json"
        before = manifest_path.read_bytes()
        with patch.object(task, "set_keep", wraps=task.set_keep) as save:
            for stage in ui.REVIEW_STAGES:
                with self.subTest(stage=stage):
                    app._select_stage(stage)
                    self.root.update()
                    self.assertTrue(app._stage_info()["confirmed"])
                    app.select_page_button.invoke()
                    self.pump(.025)
                    self.assertEqual(set(app.results_table.selection()), set(app._page_ids))
                    self.assertEqual(app._selected_ids, tuple(app._page_ids))
                    self.assertFalse(app._busy)
                    self.assertEqual(manifest_path.read_bytes(), before)
                    self.assertTrue(app._stage_info()["confirmed"])
                    app._focus_id = app._page_ids[0]
                    app._show_detail()
                    detail = app.detail_text.get("1.0", "end-1c")
                    self.assertIn("当前名单：", detail)
                    self.assertIn("本步建议", detail)
                    self.assertIn("已确认", detail)
                    if stage in {"import", "shortlist"}:
                        self.assertNotIn("六维", detail)
                        self.assertNotIn("AI 原始", detail)
            save.assert_not_called()
            selected = tuple(app._page_ids)
            app.exclude_button.invoke()
            self.wait_idle()
            info = task.snapshot()["stages"]["llm"]
            self.assertTrue(all(info["manual_choices"][key] is False for key in selected))
            self.assertFalse(info["confirmed"])
            app.results_table.selection_set(selected)
            app.restore_button.invoke()
            self.wait_idle()
            info = task.snapshot()["stages"]["llm"]
            self.assertTrue(all(info["manual_choices"][key] is True for key in selected))
            self.assertEqual(save.call_count, 2)
        self.assertEqual((self.source / "raw/current/shanghai/jobs.json").read_bytes(), self.raw_before)

    def test_two_windows_recommendations_reject_old_selection_and_legacy_api_still_works(self):
        self.complete_through("llm", confirm_last=True)
        task = self.app.task
        other_root, other = self.make_window()
        other._open_task(task.path)
        self.wait_idle(other, other_root)
        self.app._select_stage("llm")
        other._select_stage("llm")
        stale = copy.deepcopy(other._stage_info())
        changed = self.app._page_ids[0]
        self.app._set_keep([changed], False)
        self.wait_idle()
        latest = task.snapshot()
        self.assertNotEqual(stale["revision"], latest["stages"]["llm"]["revision"])
        other.recommend_button.invoke()
        self.wait_idle(other, other_root)
        self.assertEqual(task.snapshot(), latest, "a stale window must never erase another manual decision")
        self.assertTrue(any("其他窗口" in str(call) for call in self.errors.call_args_list))
        with self.assertRaisesRegex(ui.TaskStateError, "其他窗口"):
            ReviewTask.open(task.path).apply_recommendations("llm", expected_stage=stale)
        self.assertEqual(task.snapshot(), latest)
        current = task.snapshot()["stages"]["llm"]
        task.apply_recommendations("llm", expected_stage=current)
        self.assertEqual(task.snapshot()["stages"]["llm"]["manual_choices"], {})
        task.set_keep("llm", [changed], False)
        task.apply_recommendations("llm")
        self.assertEqual(task.snapshot()["stages"]["llm"]["manual_choices"], {})

    def test_ui_validates_only_current_stage_numbers_and_keeps_execution_settings_on_jd(self):
        self.create_task()
        app = self.app
        for stage in ui.REVIEW_STAGES:
            app._select_stage(stage)
            app.workers.set("invalid-ai")
            app.jd_workers.set("invalid-jd")
            app.batch_size.set("invalid-batch")
            if stage == "fetch-jd":
                app.workers.set("invalid-unrelated-ai")
                app.jd_workers.set("0")
                app.batch_size.set("20")
            elif stage == "llm":
                app.workers.set("0")
            if stage in {"fetch-jd", "llm"}:
                before = self.task_json()
                calls = len(self.calls)
                app.run_button.invoke()
                self.assertFalse(app._busy)
                self.assertEqual(len(self.calls), calls)
                self.assertEqual(self.task_json(), before)
                self.assertIn("执行设置无效", str(self.errors.call_args))
                if stage == "fetch-jd":
                    app.jd_workers.set("2")
                    app.batch_size.set("0")
                    app.run_button.invoke()
                    self.assertEqual(len(self.calls), calls)
                    self.assertEqual(self.task_json(), before)
                    app.batch_size.set("20")
                else:
                    app.workers.set("2")
            self.assertEqual(bool(app.settings_button.winfo_manager()), stage == "fetch-jd")
            app._toggle_settings()
            self.assertEqual(bool(app.settings_frame.winfo_manager()), stage == "fetch-jd")
            app.run_button.invoke()
            self.wait_idle()
            self.assertEqual(app._stage_info()["status"], "completed", self.errors.call_args_list)
            options = self.calls[-1][2]
            self.assertEqual(options["workers"], 2 if stage == "llm" else ui.AI_DEFAULT_WORKERS)
            self.assertEqual(options["jd_workers"], 2 if stage == "fetch-jd" else ui.JD_DEFAULT_WORKERS)
            self.assertEqual(options["batch_size"], 20 if stage == "fetch-jd" else 30)
            app._confirm_stage()
            self.wait_idle()

    def test_execute_stage_rejects_relevant_invalid_values_before_mutation_only(self):
        task = self.create_task()
        for stage in ui.REVIEW_STAGES:
            before = task.snapshot()
            invalid = ({"workers": 0},) if stage == "llm" else (
                {"jd_workers": 0}, {"batch_size": 0}) if stage == "fetch-jd" else ()
            for values in invalid:
                with self.subTest(stage=stage, invalid=values), self.assertRaises(ValueError):
                    pipeline.execute_stage(task, stage, **values)
                self.assertEqual(task.snapshot(), before)
            rows = copy.deepcopy(self.jobs) if stage == "import" else task.input_jobs(stage)
            if stage == "shortlist":
                for row in rows:
                    row.update(list_pass=True, list_queue="relevant")
            if stage == "fetch-jd":
                for row in rows:
                    row.update(jd="合成完整岗位职责", jd_completeness="complete")
            if stage == "llm":
                rows = [pipeline._analyze_ai_stage(row, {"job_id": job_id(row), "status": "success",
                        "review_format": "simple-v1", "判断": "符合", "理由": "合成岗位符合。"}) for row in rows]
            handler = {"import": "_run_import", "shortlist": "_run_shortlist",
                       "fetch-jd": "_run_jd", "llm": "_run_reviews"}[stage]
            result = (rows, {}, "completed", []) if stage in {"fetch-jd", "llm"} else (rows, {})
            options = {"workers": 2 if stage == "llm" else "invalid-ai",
                       "jd_workers": 2 if stage == "fetch-jd" else "invalid-jd",
                       "batch_size": 20 if stage == "fetch-jd" else "invalid-batch"}
            with patch.object(pipeline, handler, return_value=result) as execute, \
                    patch.object(pipeline, "_run_child", side_effect=AssertionError("external child forbidden")):
                snapshot = pipeline.execute_stage(task, stage, **options)
                execute.assert_called_once()
            self.assertEqual(snapshot["stages"][stage]["status"], "completed")
            task.confirm(stage)

    def test_shortlist_pending_filter_returns_to_all_when_live_run_finishes(self):
        self.complete_through("import", confirm_last=True)
        app, task = self.app, self.app.task
        task.begin("shortlist")
        try:
            rows = [{**row, "list_pass": True, "recommended_keep": True, "list_queue": "relevant"}
                    for row in task.input_jobs("shortlist")]
            info = task.snapshot()["stages"]["shortlist"]
            payload = {"stage": "shortlist", "attempt": info["attempt"], "input_revision": info["input_revision"],
                       "input_signature": pipeline.shortlist_live_signature(info), "processed_count": 3,
                       "total": len(rows), "jobs": rows[:3], "stats": {"processed_count": 3}}
            path = task.run_directory("shortlist") / "prepared/live_shortlist.json"
            pipeline._write_json(task, path, payload)
            app._apply_view(task, app._capture_view(task))
            app._render()
            app.filter.set("待核实")
            app._apply_filters()
            self.wait_until(lambda: len(app._filtered_jobs) == len(rows) - 3)
            self.assertIn("待处理", app.filter_buttons["待核实"].cget("text"))
            self.assertTrue(all("待处理" in app.results_table.set(key, "reason") for key in app._page_ids))
            task.finish("shortlist", rows)
            app._apply_view(task, app._capture_view(task))
            app._render()
            self.pump(.1)
            self.assertEqual(app.filter.get(), "全部")
            self.assertEqual(len(app._filtered_jobs), len(rows))
            self.assertTrue(app._page_ids)
            self.assertFalse(app.filter_buttons["待核实"].winfo_manager())
            self.assertEqual(app._stage_info()["pending_ids"], [])
        finally:
            if task.snapshot()["stages"]["shortlist"]["status"] == "running":
                task.interrupt("shortlist", "stopped")

    def test_rule_and_plan_dialog_actions_share_placement_and_release_tcl_before_worker_gc(self):
        app = self.app
        removed = []
        for stage in ui.REVIEW_STAGES:
            app._select_stage(stage)
            app.edit_rules_button.invoke()
            dialog = app._review_rules_dialog if stage == "llm" else app._stage_rules_dialog
            hide_dialog(dialog)
            buttons = app._review_rules_buttons if stage == "llm" else app._stage_rules_buttons
            self.assertIsInstance(buttons["save"].master, desktop_controls.DialogActions)
            self.assertIs(buttons["save"].master, buttons["cancel"].master)
            self.assertEqual(buttons["save"].cget("style"), "Workspace.Primary.TButton")
            self.assertEqual(buttons["cancel"].cget("style"), "Workspace.TButton")
            self.assertEqual(buttons["reset"].pack_info()["side"], "left")
            self.assertEqual(buttons["save"].pack_info()["side"], "right")
            self.assertEqual(buttons["cancel"].pack_info()["side"], "right")
            self.assertEqual(dialog.minsize(), (620, 440))
            removed.extend(descendants(dialog))
            buttons["cancel"].invoke()
            self.assertTrue(all(widget.tk is None for widget in removed))
        config = desktop_collection.collector.load_config(ui.DEFAULT_CONFIG)
        values = desktop_collection.plans.plan_editor_values(config)
        dialog = desktop_collection._CollectionPlanEditor(app.collection_page, config, values)
        hide_dialog(dialog)
        app.collection_page._editor = dialog
        self.assertIsInstance(dialog.save_button.master, desktop_controls.DialogActions)
        self.assertEqual(dialog.save_button.cget("style"), "Workspace.Primary.TButton")
        self.assertEqual(dialog.cancel_button.cget("style"), "Workspace.TButton")
        self.assertEqual(dialog.save_button.pack_info()["side"], "right")
        self.assertEqual(dialog.cancel_button.pack_info()["side"], "right")
        removed.extend(descendants(dialog))
        dialog.cancel_button.invoke()
        self.assertTrue(all(widget.tk is None for widget in removed))
        completed = threading.Event()

        def collect_closed_editors():
            gc.collect()
            completed.set()

        worker = threading.Thread(target=collect_closed_editors, name="closed-editor-gc")
        worker.start()
        worker.join(3)
        self.assertTrue(completed.is_set())
        self.pump(.05)

    def test_three_page_search_shortcuts_route_locally_and_escape_clears_only_that_search(self):
        app = self.app
        searches = (("collection", app.collection_page.plan_search_entry, app.collection_page.plan_search),
                    ("review", app.search_entry, app.search),
                    ("delivery", app.delivery_page.search_entry, app.delivery_page.search))
        for _workspace, _entry, value in searches:
            value.set("保留的合成查询")
        for workspace, entry, value in searches:
            app._show_workspace(workspace)
            for theme in ("dark", "light"):
                app._set_theme(theme, persist=False)
                value.set("本页合成查询")
                others = {name: variable.get() for name, _widget, variable in searches if name != workspace}
                with patch.object(entry, "focus_set") as focus, patch.object(entry, "selection_range") as select:
                    self.assertEqual(invoke_key_binding(self.root, "<Control-f>"), "break")
                    focus.assert_called_once()
                    select.assert_called_once_with(0, "end")
                self.assertEqual(invoke_key_binding(entry, "<Escape>"), "break")
                self.assertEqual(value.get(), "")
                for name, _widget, variable in searches:
                    if name != workspace:
                        self.assertEqual(variable.get(), others[name])
                self.assertEqual(entry.cget("style"), "Workspace.TEntry")
        self.assertEqual(self.calls, [])

    def test_collection_wraps_controls_inside_860_window_at_scaling_two_in_both_themes(self):
        app, root = self.app, self.root
        original_scaling = root.tk.call("tk", "scaling")
        root.tk.call("tk", "scaling", 2.0)
        self.addCleanup(lambda: root.tk is not None and root.tk.call("tk", "scaling", original_scaling))
        root.geometry("860x720")
        root.deiconify()
        app._show_workspace("collection")
        page = app.collection_page
        controls = (page.city_box, page.track_box, page.limit_entry, page.pages_spin,
                    page.page_wait_min_entry, page.page_wait_max_entry, page.browser_workers_spin,
                    page.start_button, page.prepare_browser_button, page.pause_button, page.stop_button,
                    page.continue_button, page.edge_continue_button, page.plan_search_entry)
        for theme in ("dark", "light"):
            app._set_theme(theme, persist=False)
            self.pump(.16)
            for widget in controls:
                with self.subTest(theme=theme, widget=str(widget)):
                    self.assertTrue(widget.winfo_ismapped())
                    x = widget.winfo_rootx() - root.winfo_rootx()
                    y = widget.winfo_rooty() - root.winfo_rooty()
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertLessEqual(x + widget.winfo_width(), root.winfo_width())
                    self.assertLessEqual(y + widget.winfo_height(), root.winfo_height())
        self.assertEqual(float(root.attributes("-alpha")), 0.0)
        self.assertEqual(self.calls, [])


class DeliveryConsistencyTests(unittest.TestCase):
    setUp = delivery_fixtures.DeliveryTkTests.setUp
    cleanup_window = delivery_fixtures.DeliveryTkTests.cleanup_window
    pump = delivery_fixtures.DeliveryTkTests.pump
    idle = delivery_fixtures.DeliveryTkTests.idle
    task = delivery_fixtures.DeliveryTkTests.task
    bind = delivery_fixtures.DeliveryTkTests.bind

    def test_review_and_delivery_share_real_pagination_sizes_boundaries_and_action_styles(self):
        self.bind(self.task(count=45))
        app, page = self.app, self.page
        self.assertEqual(ui.PAGE_SIZE, desktop_delivery.PAGE_SIZE)
        self.assertEqual(ui.PAGE_SIZE, desktop_controls.PAGE_SIZE)
        for previous, following in ((app.previous_page_button, app.next_page_button),
                                    (page.previous_button, page.next_button)):
            self.assertIsInstance(previous.master, desktop_controls.Pagination)
            self.assertIs(previous.master, following.master)
            self.assertEqual(previous.cget("style"), "Workspace.TButton")
            self.assertEqual(following.cget("style"), "Workspace.TButton")
        self.assertEqual(app.page_text.get(), page.page_text.get())
        self.assertEqual(len(app.results_table.get_children()), 20)
        self.assertEqual(len(page.table.get_children()), 20)
        self.assertTrue(page.previous_button.instate(["disabled"]))
        for number, count in ((1, 20), (2, 5)):
            app._change_page(1)
            page.next_button.invoke()
            self.assertEqual(app.page_text.get(), page.page_text.get())
            self.assertEqual(app._page, number)
            self.assertEqual(page._page, number)
            self.assertEqual(len(page.table.get_children()), count)
        self.assertTrue(page.next_button.instate(["disabled"]))
        self.assertTrue(app.next_page_button.instate(["disabled"]))
        for button in (app.run_button, app.confirm_button, app.collection_page.start_button, page.export_button):
            self.assertEqual(button.cget("style"), "Workspace.Primary.TButton")
        self.assertIs(app.continue_button, app.run_button)
        self.assertEqual(page.page_text.get(), "41–45 / 45 岗 · 第 3 / 3 页")

    def test_delivery_chinese_block_reason_search_and_readonly_detail_scroll_survive_theme(self):
        self.bind(self.task(blocked=True))
        page, root = self.page, self.root
        root.geometry("860x720")
        root.deiconify()
        page.search.set("缺少 BOSS")
        self.pump()
        rows = page.table.get_children()
        self.assertEqual(len(rows), 1, "the visible Chinese block reason must be searchable")
        key = rows[0]
        page.table.selection_set(key)
        page._show_detail()
        self.assertIn("缺少 BOSS", page.detail.get("1.0", "end-1c"))
        detail_panel = page.detail.master
        self.assertIsInstance(detail_panel, desktop_controls.ReadOnlyText)
        self.assertEqual(str(page.detail.cget("state")), "disabled")
        self.assertTrue(page.detail.cget("yscrollcommand"))
        self.assertTrue(detail_panel.scrollbar.cget("command"))
        for theme in ("dark", "light"):
            self.app._set_theme(theme, persist=False)
            self.pump()
            page.detail.yview_moveto(0)
            self.assertLess(page.detail.yview()[1], 1.0)
            root.tk.call(detail_panel.scrollbar.cget("command"), "moveto", 1.0)
            self.assertGreater(page.detail.yview()[0], 0.0)
            self.assertEqual(page.detail.cget("background"), self.app.colors["surface"])
            self.assertEqual(page.search.get(), "缺少 BOSS")
        self.assertEqual(float(root.attributes("-alpha")), 0.0)
        self.executor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
