"""Typed local rules and live checkpoint UI; synthetic data, no services."""

import copy
import threading
import time
import unittest
from unittest.mock import patch

import test_desktop_review_ui as fixtures

ui = fixtures.ui
ReviewTask = fixtures.ReviewTask
job_id = fixtures.job_id
write_json = fixtures.write_json
pipeline = fixtures.review_pipeline


class ReviewStageUITests(unittest.TestCase):
    # Reuse only the fixture helpers, without inheriting/rerunning its suite.
    setUp = fixtures.ReviewTkTests.setUp
    make_window = fixtures.ReviewTkTests.make_window
    _cleanup_windows = fixtures.ReviewTkTests._cleanup_windows
    pump = fixtures.ReviewTkTests.pump
    wait_idle = fixtures.ReviewTkTests.wait_idle
    fake_executor = fixtures.ReviewTkTests.fake_executor
    create_task = fixtures.ReviewTkTests.create_task
    complete_through = fixtures.ReviewTkTests.complete_through
    task_json = fixtures.ReviewTkTests.task_json
    queue_ids = fixtures.ReviewTkTests.queue_ids

    def wait_until(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(.005)
        self.fail("background UI view did not reach the expected state")

    def shortlist_rows(self, jobs):
        return [{**job, "list_pass": index % 3 != 2, "recommended_keep": index % 3 != 2,
                 "list_queue": ("relevant", "verify", "excluded")[index % 3],
                 "list_queue_reason": f"合成方向依据 {index}"} for index, job in enumerate(jobs)]

    def publish(self, task, rows, count):
        info = task.snapshot()["stages"]["shortlist"]
        payload = {"stage": "shortlist", "attempt": info["attempt"], "input_revision": info["input_revision"],
                   "input_signature": pipeline.shortlist_live_signature(info), "processed_count": count,
                   "total": len(info["input_ids"]), "jobs": rows[:count],
                   "stats": {"processed_count": count}}
        path = task.run_directory("shortlist") / "prepared/live_shortlist.json"
        pipeline._write_json(task, path, payload)
        return path, payload

    def test_all_four_rules_entries_show_defaults_before_task_and_shortlist_has_no_execution_settings(self):
        app = self.app
        for stage in ui.REVIEW_STAGES:
            app._select_stage(stage)
            self.assertTrue(app.edit_rules_button.winfo_manager())
            self.assertEqual(app.edit_rules_button.cget("text"), "修改本步规则")
            app.edit_rules_button.invoke()
            status = app._review_rules_status if stage == "llm" else app._stage_rules_status
            buttons = app._review_rules_buttons if stage == "llm" else app._stage_rules_buttons
            self.assertIn("先创建", status.get())
            self.assertIn("disabled", buttons["save"].state())
            if stage == "llm":
                self.assertEqual(app._review_rules_editor.get("1.0", "end-1c"), ui.default_review_rules())
                app._close_review_rules_editor()
            else:
                self.assertEqual(set(app._stage_rules_inputs), set(ui.default_stage_rules(stage)))
                app._close_stage_rules_editor()
        for stage in ("fetch-jd",):
            app._select_stage(stage)
            app._toggle_settings()
            self.assertTrue(app.settings_frame.winfo_manager())
            workers = app.workers.get(), app.jd_workers.get(), app.browser_mode.get()
            app._select_stage("shortlist")
            self.assertFalse(app.settings_button.winfo_manager())
            self.assertFalse(app.settings_frame.winfo_manager())
            app._toggle_settings()
            self.assertFalse(app.settings_frame.winfo_manager())
            self.assertEqual((app.workers.get(), app.jd_workers.get(), app.browser_mode.get()), workers)
        app._select_stage("llm")
        app._toggle_settings()
        self.assertFalse(app.settings_button.winfo_manager())
        self.assertFalse(app.settings_frame.winfo_manager())
        self.assertEqual(self.calls, [])

    def test_typed_rules_save_reopen_cancel_default_and_task_invalidation_are_local(self):
        self.complete_through("report", confirm_last=True)
        app, task = self.app, self.app.task
        before = task.snapshot()
        calls = len(self.calls)
        app._select_stage("shortlist")
        app.edit_rules_button.invoke()
        app._stage_rules_inputs["include_keywords"].insert("1.0", "  Agent  , agent；业务方案\n系统集成")
        app._stage_rules_inputs["keep_verification"].set(False)
        threads = []
        original = task.set_stage_rules

        def save(stage, value):
            threads.append(threading.get_ident())
            return original(stage, value)

        with patch.object(task, "set_stage_rules", side_effect=save):
            app._stage_rules_buttons["save"].invoke()
            self.wait_idle()
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], app.ui_thread_id)
        self.assertIsNone(app._stage_rules_dialog)
        self.assertEqual(len(self.calls), calls)
        saved = ReviewTask.open(task.path).get_stage_rules("shortlist")
        self.assertEqual(saved["include_keywords"], ["Agent", "业务方案", "系统集成"])
        self.assertFalse(saved["keep_verification"])
        after = task.snapshot()
        self.assertEqual(before["stages"]["import"], after["stages"]["import"])
        for stage in ("shortlist", "fetch-jd", "llm", "report"):
            self.assertEqual(after["stages"][stage]["status"], "stale")
            self.assertFalse(after["stages"][stage]["confirmed"])
        app.edit_rules_button.invoke()
        self.assertIn("系统集成", app._stage_rules_inputs["include_keywords"].get("1.0", "end-1c"))
        app._stage_rules_buttons["reset"].invoke()
        self.assertEqual(app._stage_rules_inputs["include_keywords"].get("1.0", "end-1c"), "")
        self.assertEqual(task.get_stage_rules("shortlist"), saved, "reset only changes the draft")
        app._stage_rules_buttons["cancel"].invoke()
        self.assertEqual(task.get_stage_rules("shortlist"), saved)
        app.edit_rules_button.invoke()
        app._stage_rules_buttons["reset"].invoke()
        app._stage_rules_buttons["save"].invoke()
        self.wait_idle()
        self.assertEqual(task.get_stage_rules("shortlist"), ui.default_stage_rules("shortlist"))
        self.assertEqual(len(self.calls), calls)
        self.assertEqual((self.source / "raw/current/shanghai/jobs.json").read_bytes(), self.raw_before)

    def test_rules_validation_write_failure_and_running_protection_preserve_drafts(self):
        self.complete_through("shortlist", confirm_last=True)
        app, task = self.app, self.app.task
        app.edit_rules_button.invoke()  # next stage is JD
        self.assertEqual(app._stage_rules_stage, "fetch-jd")
        for invalid in ("", "0", "21", "1.5", "fast"):
            app._stage_rules_inputs["scroll_count"].set(invalid)
            app._stage_rules_buttons["save"].invoke()
            self.assertFalse(app._busy)
            self.assertIn("未保存", app._stage_rules_status.get())
            self.assertEqual(app._stage_rules_inputs["scroll_count"].get(), invalid)
        app._stage_rules_inputs["scroll_count"].set("7")
        app._stage_rules_inputs["detail_speed_profile"].set("safe")
        with patch.object(task, "set_stage_rules", side_effect=ui.TaskStateError("合成写入失败")):
            app._stage_rules_buttons["save"].invoke()
            self.wait_idle()
        self.assertEqual(app._stage_rules_inputs["scroll_count"].get(), "7")
        self.assertIn("编辑内容已保留", app._stage_rules_status.get())
        app._stage_rules_buttons["save"].invoke()
        self.wait_idle()
        self.assertEqual(ReviewTask.open(task.path).get_stage_rules("fetch-jd"),
                         {**ui.default_stage_rules("fetch-jd"), "scroll_count": 7, "detail_speed_profile": "safe"})
        self.assertIn("滚动7次", app.jd_mode_label.cget("text"))
        task.begin("fetch-jd")
        try:
            app._apply_view(task, app._capture_view(task))
            app._render()
            app.edit_rules_button.invoke()
            self.assertIn("只读", app._stage_rules_status.get())
            self.assertIn("disabled", app._stage_rules_buttons["save"].state())
            self.assertIn("disabled", app._stage_rules_buttons["reset"].state())
            self.assertNotIn("disabled", app._stage_rules_buttons["cancel"].state())
            before = task.snapshot()
            app._save_stage_rules()
            self.assertEqual(task.snapshot(), before)
            app._close_stage_rules_editor()
        finally:
            task.interrupt("fetch-jd", "stopped")

    def test_import_rule_switch_and_word_list_save_then_reopen_without_execution(self):
        self.complete_through("import")
        app, task = self.app, self.app.task
        app.edit_rules_button.invoke()
        app._stage_rules_inputs["require_title"].set(True)
        app._stage_rules_inputs["exclude_company_keywords"].insert("1.0", "排除公司")
        calls = len(self.calls)
        app._stage_rules_buttons["save"].invoke()
        self.wait_idle()
        self.assertEqual(task.get_stage_rules("import"), {
            "require_title": True, "exclude_title_keywords": [], "exclude_company_keywords": ["排除公司"]})
        self.assertEqual(task.snapshot()["stages"]["import"]["status"], "stale")
        self.assertEqual(len(self.calls), calls)
        app._open_task(task.path)
        self.wait_idle()
        app.edit_rules_button.invoke()
        self.assertTrue(app._stage_rules_inputs["require_title"].get())
        self.assertEqual(app._stage_rules_inputs["exclude_company_keywords"].get("1.0", "end-1c"), "排除公司")
        self.assertEqual(len(self.calls), calls)
        app._close_stage_rules_editor()
        self.assertEqual(app._row_reason({"job_id": "excluded", "import_rule_keep": False,
                                          "import_rule_reason": "公司排除词：排除公司"}),
                         "导入规则建议剔除 · 公司排除词：排除公司")

    def test_search_filter_work_is_off_tk_and_newer_live_event_wins_when_delivery_is_reordered(self):
        self.complete_through("shortlist")
        app, task = self.app, self.app.task
        release = threading.Event()
        self.release_events.append(release)
        threads = []
        original = ui._filter_rows

        def slow_filter(*args):
            threads.append(threading.get_ident())
            release.wait(2)
            return original(*args)

        with patch.object(ui, "_filter_rows", side_effect=slow_filter):
            app.search.set("公司 010")
            self.pump(.15)
            ticks = []
            self.root.after(1, lambda: ticks.append(True))
            self.pump(.05)
            self.assertEqual(ticks, [True])
            self.assertTrue(threads)
            self.assertTrue(all(thread != app.ui_thread_id for thread in threads))
            release.set()
            self.wait_until(lambda: len(app._page_ids) == 1)
        self.assertEqual(app._page_ids, ["job-010"])
        older = app._capture_view(task)
        changed = job_id(app._rows["shortlist"][0])
        task.set_keep("shortlist", [changed], False)
        newer = app._capture_view(task)
        app.events.put(("live-view", app._operation_id, (task, newer)))
        app.events.put(("live-view", app._operation_id, (task, older)))
        self.pump(.1)
        self.assertEqual(app._applied_capture_sequence, newer["capture_sequence"])
        self.assertEqual(app._stage_info()["manual_choices"][changed], False)

    def test_rules_dialog_theme_geometry_and_task_switch_are_safe(self):
        self.complete_through("shortlist")
        app = self.app
        self.root.geometry("1024x720")
        self.root.deiconify()
        app.edit_rules_button.invoke()
        dialog = app._stage_rules_dialog
        for theme in ("dark", "light"):
            app._set_theme(theme, persist=False)
            self.root.update()
            text = app._stage_rules_inputs["include_keywords"]
            self.assertEqual(text.cget("background"), ui.palette_for(theme)["surface"])
            self.assertEqual(text.cget("foreground"), ui.palette_for(theme)["text"])
            self.assertGreater(app._stage_rules_canvas.winfo_height(), 100)
            for button in app._stage_rules_buttons.values():
                self.assertGreaterEqual(button.winfo_rootx(), dialog.winfo_rootx())
                self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), dialog.winfo_rootx() + dialog.winfo_width())
                self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), dialog.winfo_rooty() + dialog.winfo_height())
        app._new_task()
        self.assertIsNone(app._stage_rules_dialog)
        self.assertIsNone(app.task)
        self.assertEqual(app._saved_stage_rules["shortlist"], ui.default_stage_rules("shortlist"))

    def test_shortlist_live_prefix_pending_manual_priority_cache_and_pause_are_readonly(self):
        self.complete_through("shortlist")
        app, task = self.app, self.app.task
        jobs = task.jobs("shortlist")
        task.set_keep("shortlist", [job_id(jobs[0])], False)
        task.set_keep("shortlist", [job_id(jobs[2])], True)
        task.begin("shortlist")
        try:
            rows = self.shortlist_rows(task.input_jobs("shortlist"))
            self.publish(task, rows, 8)
            cache = ui.TaskViewCache()
            before = task.snapshot()
            view = ui._capture_task(task, cache=cache, include_report=False)
            info = view["manifest"]["stages"]["shortlist"]
            self.assertEqual(len(view["rows"]["shortlist"]), 50)
            self.assertEqual(len(info["pending_ids"]), 42)
            self.assertIn(job_id(jobs[0]), info["excluded_ids"])
            self.assertIn(job_id(jobs[2]), info["keep_ids"])
            self.assertEqual(set(info["keep_ids"]) | set(info["excluded_ids"]) | set(info["pending_ids"]), set(info["input_ids"]))
            with patch.object(ui, "_read_json", wraps=ui._read_json) as reads, \
                    patch.object(ui, "_build_search", wraps=ui._build_search) as searches:
                same = ui._capture_task(task, cache=cache, include_report=False)
                reads.assert_not_called()
                searches.assert_not_called()
                self.assertIs(same["rows"]["shortlist"], view["rows"]["shortlist"])
                self.assertIs(same["row_views"]["shortlist"], view["row_views"]["shortlist"])
            app._apply_view(task, view)
            app._render()
            self.assertFalse(app._can_edit())
            self.assertFalse(app._can_confirm())
            self.assertIn("待处理 42", app.count_text.get())
            self.assertIn("待处理", app.results_table.set(job_id(rows[8]), "reason"))
            self.assertEqual(app.results_table.set(job_id(rows[8]), "keep"), "—")
            self.assertIn("人工保留", app.results_table.set(job_id(rows[2]), "reason"))
            self.assertEqual(task.snapshot(), before)
            task.interrupt("shortlist", "paused")
            view = ui._capture_task(task, cache=cache, include_report=False)
            app._apply_view(task, view)
            app._render()
            self.assertEqual(app._stage_info()["status"], "paused")
            self.assertEqual(len(app._stage_info()["pending_ids"]), 42)
            self.assertFalse(app._can_edit())
            self.assertFalse(app._can_confirm())
        finally:
            if task.snapshot()["stages"]["shortlist"]["status"] == "running":
                task.interrupt("shortlist", "stopped")

    def test_shortlist_old_attempt_changed_input_and_torn_snapshot_are_never_adopted(self):
        self.complete_through("import", confirm_last=True)
        task = self.app.task
        task.begin("shortlist")
        try:
            rows = self.shortlist_rows(task.input_jobs("shortlist"))
            path, payload = self.publish(task, rows, 4)
            malformed = []
            for field, value in (("attempt", payload["attempt"] - 1), ("input_revision", payload["input_revision"] + 1),
                                 ("input_signature", "old-input"), ("processed_count", 5), ("total", 49)):
                changed = copy.deepcopy(payload)
                changed[field] = value
                malformed.append(changed)
            changed = copy.deepcopy(payload)
            changed["jobs"][0], changed["jobs"][1] = changed["jobs"][1], changed["jobs"][0]
            malformed.append(changed)
            for changed in malformed:
                pipeline._write_json(task, path, changed)
                view = ui._capture_task(task, include_report=False)
                info = view["manifest"]["stages"]["shortlist"]
                self.assertEqual(info["keep_ids"], [])
                self.assertEqual(info["excluded_ids"], [])
                self.assertEqual(info["pending_ids"], info["input_ids"])
            pipeline._write_json(task, path, payload)
            original = ui._read_json

            def changing(current_task, current_path, default):
                result = original(current_task, current_path, default)
                if current_path == path:
                    replacement = {**payload, "processed_count": 5, "jobs": rows[:5]}
                    pipeline._write_json(task, path, replacement)
                return result

            with patch.object(ui, "_read_json", side_effect=changing):
                with self.assertRaisesRegex(ui.TaskStateError, "读取时发生变化"):
                    ui._capture_task(task, include_report=False)
        finally:
            task.interrupt("shortlist", "stopped")

    def test_interrupted_new_attempt_without_valid_snapshot_never_displays_previous_results(self):
        self.complete_through("shortlist")
        app, task = self.app, self.app.task
        jobs = task.jobs("shortlist")
        task.set_keep("shortlist", [job_id(jobs[0])], False)
        task.set_keep("shortlist", [job_id(jobs[1])], True)
        path, old_payload = self.publish(task, self.shortlist_rows(jobs), len(jobs))
        task.set_stage_rules("shortlist", {"exclude_keywords": ["AI"]})
        cache = ui.TaskViewCache()
        for index, status in enumerate(("paused", "stopped", "waiting", "failed")):
            task.begin("shortlist")
            if index % 2:
                pipeline._write_json(task, path, old_payload)
            else:
                path.unlink(missing_ok=True)
            task.interrupt("shortlist", status)
            before = task.snapshot()
            view = ui._capture_task(task, cache=cache, include_report=False)
            info = view["manifest"]["stages"]["shortlist"]
            self.assertEqual(info["status"], status)
            self.assertTrue(info["live_incomplete"])
            self.assertEqual(info["stats"]["processed_count"], 0)
            self.assertEqual(info["keep_ids"], [job_id(jobs[1])])
            self.assertEqual(info["excluded_ids"], [job_id(jobs[0])])
            self.assertEqual(len(info["pending_ids"]), len(jobs) - 2)
            self.assertEqual(len(view["rows"]["shortlist"]), len(jobs))
            self.assertTrue(all(job.get("_review_pending_stage") == "shortlist" for job in view["rows"]["shortlist"]))
            app._apply_view(task, view)
            app._render()
            self.assertFalse(app._can_edit())
            self.assertFalse(app._can_confirm())
            self.assertEqual(task.snapshot(), before)

    def test_running_jd_reads_only_current_inputs_and_reuses_unchanged_detail_checkpoints(self):
        self.complete_through("shortlist", confirm_last=True)
        task = self.app.task
        task.begin("fetch-jd")
        try:
            jobs = task.input_jobs("fetch-jd")
            path = task.run_directory("fetch-jd") / "prepared/jd_shortlists/details.json"
            write_json(path, [{"job_id": job_id(jobs[0]), "jd": "企业方案架构、实施交付及客户解决方案。", "status": "success"},
                              {"job_id": "outside-current-input", "jd": "其它任务岗位"}])
            cache = ui.TaskViewCache()
            view = ui._capture_task(task, cache=cache, include_report=False)
            self.assertEqual(len(view["rows"]["fetch-jd"]), len(jobs))
            self.assertIn("企业方案架构", view["rows"]["fetch-jd"][0]["jd"])
            self.assertEqual(view["manifest"]["stages"]["fetch-jd"]["stats"]["jd_success_count"], 1)
            self.assertNotIn("outside-current-input", view["row_views"]["fetch-jd"]["by_id"])
            with patch.object(ui, "_detail_checkpoint", wraps=ui._detail_checkpoint) as reads:
                again = ui._capture_task(task, cache=cache, include_report=False)
                reads.assert_not_called()
                self.assertIs(again["rows"]["fetch-jd"], view["rows"]["fetch-jd"])
        finally:
            task.interrupt("fetch-jd", "stopped")

    def test_6200_job_shortlist_live_lists_heartbeat_pause_and_final_result(self):
        self.jobs = [{**self.jobs[index % 50], "job_id": f"live-{index:05d}", "title": f"实时方案岗位 {index:05d}"}
                     for index in range(6200)]
        write_json(self.source / "raw/current/shanghai/jobs.json", self.jobs)
        self.app.sample_mode.set(ui.SAMPLE_CHOICES[0])
        self.app._create_task()
        self.wait_idle(timeout=12)
        task, app = self.app.task, self.app
        task.begin("import")
        task.finish("import", self.jobs)
        task.confirm("import")
        app._apply_view(task, app._capture_view(task))
        app._select_stage("shortlist")
        advance = threading.Event()
        self.release_events.append(advance)
        ticks = []

        def beat():
            ticks.append(time.monotonic())
            if not app._closed:
                app._after(10, beat)

        app._after(0, beat)
        rows = self.shortlist_rows(self.jobs)

        def streaming(current_task, stage, **options):
            current_task.begin(stage)
            for count in (25, 3325):
                self.publish(current_task, rows, count)
                for _index in range(200):
                    options["on_event"]("progress", {"done": count, "total": 6200})
                while not advance.wait(.01) and not options["control"].requested:
                    pass
                advance.clear()
                if options["control"].requested:
                    break
            while not options["control"].requested:
                options["control"]._event.wait(.01)
            current_task.interrupt(stage, options["control"].requested)

        app.executor = streaming
        with patch.object(app, "_capture_view", wraps=app._capture_view) as captures:
            app._run_stage()
            self.wait_until(lambda: app._stage_info().get("stats", {}).get("processed_count") == 25, timeout=15)
            self.assertEqual(len(app._rows["shortlist"]), 6200)
            self.assertGreater(len(app._stage_info()["keep_ids"]), 0)
            self.assertEqual(len(app._stage_info()["pending_ids"]), 6175)
            self.assertFalse(app._can_confirm())
            self.assertFalse(app._can_edit())
            app.search.set("实时方案岗位")
            self.wait_until(lambda: app._filtered_key and app._filtered_key[3] == "实时方案岗位")
            app._change_page(1)
            selected = tuple(app._page_ids[:2])
            app.results_table.selection_set(selected)
            app.results_table.focus(selected[1])
            app._show_selected_job()
            advance.set()
            self.wait_until(lambda: app._stage_info().get("stats", {}).get("processed_count") == 3325, timeout=15)
            self.assertEqual(app._page, 1)
            self.assertEqual(app.results_table.selection(), selected)
            self.assertEqual(app._focus_id, selected[1])
            self.assertEqual(len(app._stage_info()["pending_ids"]), 2875)
            self.assertIn("合成方向依据", app.results_table.set(selected[0], "reason"))
            app._pause_stage()
            advance.set()
            self.wait_idle(timeout=15)
            self.assertEqual(app._stage_info()["status"], "paused")
            self.assertEqual(len(app._stage_info()["pending_ids"]), 2875)
            self.assertFalse(app._can_confirm())
            self.assertFalse(app._can_edit())
            self.assertLess(captures.call_count, 20, "progress bursts must not trigger a capture per event")
        self.assertGreater(len(ticks), 10)
        self.assertLess(max(b - a for a, b in zip(ticks, ticks[1:])), 1.0)

        def finish(current_task, stage, **_options):
            current_task.begin(stage)
            self.publish(current_task, rows, len(rows))
            current_task.finish(stage, rows)

        app.executor = finish
        app._run_stage()
        self.wait_idle(timeout=15)
        self.assertEqual(app._stage_info()["status"], "completed")
        self.assertEqual(app._rows["shortlist"], task.jobs("shortlist"))
        self.assertEqual(len(app._stage_info()["keep_ids"]), sum(row["recommended_keep"] for row in rows))
        self.assertEqual(app._stage_info()["pending_ids"], [])
        self.assertFalse(app._stage_info()["confirmed"])
        self.assertTrue(app._can_confirm())
        self.browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
