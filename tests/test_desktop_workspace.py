"""Workbench integration tests: real Tk widgets, temporary data, no network."""

import json
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import desktop_app
import desktop_ui


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class WorkspaceUtilityTests(unittest.TestCase):
    def test_preferences_round_trip_whitelists_only_ui_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            desktop_ui.save_preferences({"city": "上海", "workers": "8", "api_key": "do-not-save"}, path)
            self.assertEqual(desktop_ui.load_preferences(path), {"city": "上海", "workers": "8"})
            self.assertNotIn("do-not-save", path.read_text(encoding="utf-8"))

    def test_missing_or_broken_preferences_recover_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertEqual(desktop_ui.load_preferences(path), {})
            for content in ("{", "[]", '{"city":42,"token":"private","pages":"10"}'):
                path.write_text(content, encoding="utf-8")
                value = desktop_ui.load_preferences(path)
                self.assertEqual(value, {"pages": "10"} if "pages" in content else {})
                self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_unknown_counts_are_not_zero(self):
        self.assertEqual(desktop_ui.format_count(None), "—")
        self.assertEqual(desktop_ui.format_count(True), "—")
        self.assertEqual(desktop_ui.format_count(0), "0")
        self.assertEqual(desktop_ui.format_count(12500), "12,500")

    def test_progress_fraction_handles_unknown_zero_and_overflow(self):
        self.assertEqual(desktop_ui.progress_fraction(" 42 / 50 "), (42, 50))
        self.assertEqual(desktop_ui.progress_fraction("60/50"), (50, 50))
        for value in ("", "0/0", "waiting", "-1/50", "42/50 · 84%"):
            self.assertIsNone(desktop_ui.progress_fraction(value))

    def test_resume_scope_cannot_replace_an_old_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            saved = {"source_root": str(root), "city": "上海", "track": "当前+成长", "sample_size": 50}
            options = {"root": root, "city": "上海", "track": "当前+成长", "sample": 50}
            self.assertEqual(desktop_app.resume_scope_conflicts(saved, options), [])
            options.update(city="杭州", sample=200, track="成长目标", root=root / "other")
            self.assertEqual(set(desktop_app.resume_scope_conflicts(saved, options)), {"城市", "岗位轨道", "岗位数量", "采集根目录"})

    def test_model_zero_exit_requires_real_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            path = output / "prepared" / "llm_manifest.json"
            self.assertEqual(desktop_app.llm_stage_checkpoint_outcome(output)[0], "failed")
            for checkpoint, expected in (
                ({"status": "configuration_failed"}, "partial"),
                ({"status": "running"}, "failed"),
                ({"status": "skipped_no_candidates"}, "success"),
                ({"status": "completed", "failed_count": 0, "pending_count": 0, "fallback_count": 0}, "success"),
                ({"status": "completed", "failed_count": 1, "pending_count": 0, "fallback_count": 1}, "partial"),
                ({"status": "completed", "failed_count": False, "pending_count": 0, "fallback_count": 0}, "failed"),
            ):
                with self.subTest(checkpoint=checkpoint):
                    write_json(path, checkpoint)
                    self.assertEqual(desktop_app.llm_stage_checkpoint_outcome(output)[0], expected)

    def test_failed_model_checkpoint_is_not_skipped_even_if_run_says_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_json(output / "prepared" / "llm_manifest.json", {"status": "configuration_failed"})
            write_json(output / "prepared" / "llm_reviews.json", [])
            self.assertFalse(desktop_app.can_skip_completed_stage(
                "Codex语义复核", {"stages": {"Codex语义复核": {"status": "success"}}},
                output, output, settings_match=True,
            ))

    def test_process_request_uses_luna_max_without_changing_global_config(self):
        command = desktop_app.make_process_command("python", Path("source"), Path("run"), "llm", "上海", "当前+成长", 50, 8, 30, None)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-luna")
        self.assertEqual(command[command.index("--reasoning-effort") + 1], "max")
        self.assertNotIn("--api-key", command)

    def test_collect_and_browser_setup_use_the_selected_config(self):
        config = Path("自定义目录") / "custom-collection.json"
        command = desktop_app.make_collect_command("python", Path("data"), Path("scraper"), "上海", "当前+成长", 10, None, prepare_browser=False, config_path=config)
        self.assertEqual(command[command.index("--config") + 1], str(config))
        app = object.__new__(desktop_app.CollectorDesktopApp)
        app.config_path = config
        app.events = queue.Queue()
        app.stop_requested = False
        app._run_child = Mock(return_value=0)
        app._run_browser_setup(dict(scraper=Path("scraper"), city="上海", track="当前+成长", pages=10, limit=None), Path("temp-setup"))
        setup = app._run_child.call_args.args[0]
        self.assertEqual(setup[setup.index("--config") + 1], str(config))

    def test_long_subprocess_lines_are_redacted_before_display_chunking(self):
        app = object.__new__(desktop_app.CollectorDesktopApp)
        app.events = queue.Queue()
        app._process_lock = threading.Lock()
        app.process = None
        app._stopping_process = None
        app.stop_requested = False
        app.waiting_for_human = False
        code = "print('x'*492 + ' ' + 'api_key=NOT_A_REAL_SECRET_FOR_REVIEW')\nprint('y'*490 + ' Cookie=ONLY_TEST_COOKIE; second=ONLY_TEST_SECOND_COOKIE')"
        self.assertEqual(app._run_child([sys.executable, "-u", "-c", code]), 0)
        logs = []
        while not app.events.empty():
            kind, value = app.events.get_nowait()
            if kind == "log":
                logs.append(value)
        logs = "".join(logs)
        for value in ("NOT_A_REAL_SECRET_FOR_REVIEW", "ONLY_TEST_COOKIE", "ONLY_TEST_SECOND_COOKIE"):
            self.assertNotIn(value, logs)
        self.assertIn("[REDACTED]", logs)

    def test_no_newline_prompt_does_not_wait_again_after_continue(self):
        app = object.__new__(desktop_app.CollectorDesktopApp)
        app.events = queue.Queue()
        app._process_lock = threading.Lock()
        app.process = None
        app._stopping_process = None
        app.stop_requested = False
        app.waiting_for_human = False
        app.workflow_running = True
        app.continue_button = Mock()
        app.status_text = Mock()
        command = [sys.executable, "-u", "-c", "import time; input('人工验证后按 Enter 继续: '); print('resumed', flush=True); time.sleep(0.3)"]
        worker = threading.Thread(target=app._run_child, args=(command,))
        worker.start()
        waits = []
        resumed = False
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline and (worker.is_alive() or not app.events.empty()):
                try:
                    kind, value = app.events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if kind == "human_wait" and value:
                    waits.append(value)
                    if len(waits) == 1:
                        app._continue_child()
                if kind == "log" and "resumed" in value:
                    resumed = True
                    self.assertFalse(app.waiting_for_human)
            self.assertTrue(resumed)
            self.assertEqual(waits, [True])
        finally:
            if worker.is_alive() and app.process:
                app._request_child_stop(app.process)
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())

    def _rerun_partial_jd(self, directory, stage):
        root = Path(directory)
        output = root / "run"
        prepared = output / "prepared"
        shortlists = prepared / "jd_shortlists"
        write_json(prepared / "merged_jobs_index.json", {"jobs": [{"job_id": "new-jd"}]})
        write_json(shortlists / "jd_input.json", {"jobs": [{"job_id": "new-jd"}]})
        write_json(shortlists / "details_manifest.json", {"total_jobs": 1, "batch_size": 30, "records": [{"batch": 1, "status": "partial", "exit_code": 0}]})
        write_json(prepared / "llm_manifest.json", {"status": "skipped_no_candidates", "candidate_count": 0})
        write_json(prepared / "llm_reviews.json", [])
        write_json(prepared / "hai_tou_pool.json", {"summary": {"classification_counts": {}}})
        report = prepared / "海投分层清单.md"
        report.write_text("old report without JD", encoding="utf-8")
        options = dict(root=root, scraper=None, city="上海", track="当前+成长", pages=10, limit=None, sample=50, workers=8, batch_size=30)
        write_json(output / "run_manifest.json", dict(source_root=str(root), city="上海", track="当前+成长", combination_limit=None, pages_per_combination=10, sample_size=50, batch_size=30, stages={label: {"status": "partial" if label == "JD补抓" else "success"} for label, _ in desktop_app.POST_PROCESS_STAGES}))
        app = object.__new__(desktop_app.CollectorDesktopApp)
        app.events = queue.Queue()
        app.config = {}
        app.config_path = desktop_app.DEFAULT_CONFIG
        app.stop_requested = False

        def child(command):
            name = command[command.index("--stage") + 1]
            if name == "fetch-jd":
                write_json(shortlists / "details_manifest.json", {"total_jobs": 1, "batch_size": 30, "records": [{"batch": 1, "status": "success", "exit_code": 0}]})
            elif name == "llm":
                write_json(prepared / "llm_manifest.json", {"status": "completed", "candidate_count": 1, "completed_count": 1, "pending_count": 0, "failed_count": 0, "fallback_count": 0})
            elif name == "report":
                report.write_text("fresh report with JD", encoding="utf-8")
            return 0

        app._run_child = Mock(side_effect=child)
        with patch.object(desktop_app.processor, "inspect_city", return_value={}):
            app._run_workflow(options, stage, output)
        return app, json.loads((output / "run_manifest.json").read_text(encoding="utf-8")), report

    def test_new_jd_invalidates_old_model_and_report_in_the_same_full_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            app, state, report = self._rerun_partial_jd(directory, "后处理全流程（已有数据）")
            called = [call.args[0][call.args[0].index("--stage") + 1] for call in app._run_child.call_args_list]
            self.assertEqual(called, ["fetch-jd", "llm", "report"])
            self.assertEqual(state["status"], "success")
            self.assertEqual(state["llm_manifest"]["candidate_count"], 1)
            self.assertEqual(report.read_text(encoding="utf-8"), "fresh report with JD")

    def test_single_jd_stage_persists_downstream_staleness_without_deleting_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            app, state, report = self._rerun_partial_jd(directory, "JD补抓")
            self.assertEqual(app._run_child.call_count, 1)
            self.assertEqual(state["status"], "partial")
            self.assertEqual(state["stages"]["Codex语义复核"]["status"], "stale")
            self.assertEqual(state["stages"]["生成报告"]["status"], "stale")
            self.assertIn("待更新", "".join(state["warnings"]))
            self.assertEqual(report.read_text(encoding="utf-8"), "old report without JD")
            self.assertEqual(json.loads((Path(directory) / "run" / "prepared" / "llm_reviews.json").read_text(encoding="utf-8")), [])

    def test_cookie_assignments_and_headers_are_redacted(self):
        for text in ('Cookie="a=private-a; b=private-b"', "Cookie: a=private-a; b=private-b", '{"cookie": "private-a"}', "set_cookie=private-a", "Authorization: Bearer private-a", "experimental_bearer_token=private-a"):
            with self.subTest(text=text):
                safe = desktop_app.redact_secrets(text)
                self.assertNotIn("private-a", safe)
                self.assertNotIn("private-b", safe)

    def test_user_pause_is_distinct_from_failure_and_security_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "run"
            app = object.__new__(desktop_app.CollectorDesktopApp)
            app.events = queue.Queue()
            app.config = {}
            app.config_path = desktop_app.DEFAULT_CONFIG
            app.stop_requested = False
            app._pause_requested = False
            options = dict(root=root, scraper=None, city="上海", track="当前+成长", pages=10, limit=None, sample=50, workers=8, batch_size=30)

            def pause(_command):
                app.stop_requested = True
                app._pause_requested = True
                return 130

            app._run_child = Mock(side_effect=pause)
            with patch.object(desktop_app.processor, "inspect_city", return_value={}):
                app._run_workflow(options, "列表前处理", output)
            state = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "paused")
            self.assertEqual(state["pause_reason"], "user_requested")
            self.assertEqual(state["stages"]["列表前处理"]["status"], "paused")
            self.assertTrue(state["finished_at"])

    def test_config_failure_continues_rule_report_but_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "run"
            write_json(output / "prepared" / "jd_shortlists" / "jd_input.json", {"jobs": []})
            app = object.__new__(desktop_app.CollectorDesktopApp)
            app.events = queue.Queue()
            app.config = {}
            app.config_path = desktop_app.DEFAULT_CONFIG
            app.stop_requested = False
            options = dict(root=root, scraper=None, city="上海", track="当前+成长", pages=10, limit=None, sample=50, workers=8, batch_size=30)

            def child(command):
                if command[command.index("--stage") + 1] == "llm":
                    write_json(output / "prepared" / "llm_manifest.json", {"status": "configuration_failed"})
                return 0

            app._run_child = Mock(side_effect=child)
            with patch.object(desktop_app.processor, "inspect_city", return_value={}):
                app._run_workflow(options, "后处理全流程（已有数据）", output)
            state = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "partial")
            self.assertEqual(state["stages"]["Codex语义复核"]["status"], "partial")
            self.assertEqual(state["stages"]["生成报告"]["status"], "success")

    def test_collection_only_does_not_reuse_old_postprocessing_stats_or_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_files = {
                root / "run_manifest.json": {"classification_counts": {"重点投递": 99}, "stages": {"岗位采集": {"status": "success"}}},
                root / "raw_sample.json": {"metadata": {"selected_unique_count": 99}},
                root / "prepared" / "hai_tou_pool.json": {"summary": {"classification_counts": {"重点投递": 99}}},
                root / "prepared" / "llm_manifest.json": {"completed_count": 99},
                root / "prepared" / "jd_shortlists" / "details_manifest.json": {"total_jobs": 99},
            }
            for path, value in old_files.items():
                write_json(path, value)
            before = {path: path.read_bytes() for path in old_files}
            write_json(root / "collection_manifest.json", {"combinations": []})
            app = object.__new__(desktop_app.CollectorDesktopApp)
            app.events = queue.Queue()
            app.config = {}
            app.config_path = desktop_app.DEFAULT_CONFIG
            app.stop_requested = False
            app._run_child = Mock(return_value=0)
            options = dict(root=root, scraper=None, city="上海", track="当前+成长", pages=10, limit=None, sample=50, workers=8, batch_size=30)
            with patch.object(desktop_app.processor, "inspect_city", return_value={"raw_record_count": 50}):
                app._run_workflow(options, "仅采集", root)
            events = list(app.events.queue)
            state = next(value["manifest"] for kind, value in events if kind == "finished")
            self.assertEqual(app._run_child.call_count, 1)
            self.assertEqual(set(state["stages"]), {"岗位采集"})
            self.assertEqual(set(state["output_files"]), {"collection_manifest"})
            self.assertEqual(state["source_stats"]["raw_record_count"], 50)
            for key in ("sample_stats", "classification_counts", "jd_manifest", "llm_manifest"):
                self.assertNotIn(key, state)
            self.assertEqual(before, {path: path.read_bytes() for path in old_files})

    def test_collection_error_does_not_load_or_update_old_run_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "run_manifest.json"
            write_json(path, {"classification_counts": {"重点投递": 99}})
            original = path.read_bytes()
            app = object.__new__(desktop_app.CollectorDesktopApp)
            app.events = queue.Queue()
            app._run_workflow = Mock(side_effect=RuntimeError("simulated collector failure"))
            with patch.object(desktop_app, "load_run_manifest") as load:
                app._run_workflow_safely({}, "仅采集", root)
            load.assert_not_called()
            finished = next(value for kind, value in list(app.events.queue) if kind == "finished")
            self.assertEqual(finished["manifest"]["status"], "failed")
            self.assertNotIn("classification_counts", finished["manifest"])
            self.assertEqual(path.read_bytes(), original)


class TkWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source"
        self.source.mkdir()
        self.preferences = patch.object(desktop_app, "load_preferences", return_value={})
        self.workspace_preferences = patch.object(desktop_ui, "load_preferences", return_value={})
        self.preference_save = patch.object(desktop_ui, "save_preferences")
        self.refresh_history = patch.object(desktop_app.CollectorDesktopApp, "_refresh_history")
        self.refresh_stats = patch.object(desktop_app.CollectorDesktopApp, "_refresh_stats")
        for patcher in (self.preferences, self.workspace_preferences, self.preference_save, self.refresh_history, self.refresh_stats):
            patcher.start()
            self.addCleanup(patcher.stop)
        try:
            self.root = desktop_app.tk.Tk()
        except desktop_app.tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.root.withdraw()
        self.app = desktop_app.CollectorDesktopApp(self.root, desktop_app.parse_args(["--root", str(self.source)]))
        self.root.update_idletasks()
        self.addCleanup(self.close_tk)

    def close_tk(self):
        self.app._closing = True
        self.app._close_workspace()
        writer = self.app._log_writer
        if writer is not None:
            self.assertTrue(writer.close().wait(timeout=3))
            writer.thread.join(timeout=3)
        if self.app._poll_after_id:
            self.root.after_cancel(self.app._poll_after_id)
        self.root.destroy()

    def wait_until(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(0.01)
        self.fail("Desktop event did not complete before timeout")

    def jobs(self, count=50):
        return [dict(job_id=f"j-{i:03}", title=f"AI应用工程师 {i}", company="测试公司", city="上海", salary="20-30K", classification="重点投递" if i < 7 else "海投池" if i < 35 else "暂不投", jd="负责 Agent/RAG 的方案设计、系统集成与交付。", jd_status="已抓取", jd_dimension_count=4, model_status="success", job_link=f"https://www.zhipin.com/job_detail/test{i}.html") for i in range(count)]

    def load_jobs(self, jobs):
        self.app._result_jobs = jobs
        self.app._apply_result_filters()
        self.wait_until(lambda: len(self.app._filtered_jobs) == len(jobs))

    def test_three_pages_and_safe_first_run_defaults(self):
        self.assertEqual(len(self.app.notebook.tabs()), 3)
        self.assertEqual(self.app.stage.get(), "列表前处理")
        self.assertEqual(self.app.city.get(), "上海")
        self.assertEqual(self.app.sample_size.get(), "50")
        self.assertEqual(str(self.app.pause_button.cget("state")), "disabled")

    def test_minimum_window_keeps_primary_controls_and_log_visible(self):
        self.root.geometry("1080x700")
        self.root.deiconify()
        self.root.update()
        for widget in (self.app.start_button, self.app.pause_button, self.app.stop_button, self.app.prepare_browser_button, self.app.log_view):
            with self.subTest(widget=str(widget)):
                x = widget.winfo_rootx() - self.root.winfo_rootx()
                y = widget.winfo_rooty() - self.root.winfo_rooty()
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + widget.winfo_width(), self.root.winfo_width())
                self.assertLessEqual(y + widget.winfo_height(), self.root.winfo_height())
        self.assertGreater(self.app.log_view.winfo_height(), 50)
        self.app._apply_snapshot(dict(unique_job_count=50, jd_success=42, jd_total=43, jd_missing=1, jd_paused=0, jd_failed=0, llm_success=42, llm_total=42, llm_failed=0, llm_fallback=0))
        self.root.update()
        self.assertGreater(self.app.log_view.winfo_height(), 50)
        self.app._workspace_event("human_wait", True)
        self.root.update()
        self.assertGreater(self.app.log_view.winfo_height(), 50)
        self.root.withdraw()

    def test_fifty_jobs_filter_and_evidence_detail(self):
        self.load_jobs(self.jobs())
        self.assertEqual(len(self.app.results_table.get_children()), 50)
        self.app.result_class.set("重点投递")
        self.wait_until(lambda: len(self.app._filtered_jobs) == 7)
        self.assertEqual(len(self.app.results_table.get_children()), 7)
        self.assertIn("Agent/RAG", self.app.detail_text.get("1.0", "end"))
        self.app.result_search.set("无此公司")
        self.wait_until(lambda: not self.app._filtered_jobs)
        self.assertEqual(len(self.app.results_table.get_children()), 0)

    def test_small_window_results_and_history_actions_remain_reachable(self):
        self.root.geometry("1080x700")
        self.root.deiconify()
        for page in (self.app.results_page, self.app.history_page):
            self.app.notebook.select(page)
            self.root.update()
            pending = list(page.winfo_children())
            while pending:
                widget = pending.pop()
                pending.extend(widget.winfo_children())
                if widget.winfo_ismapped() and widget.winfo_class() in ("TButton", "TCombobox"):
                    with self.subTest(widget=str(widget)):
                        x = widget.winfo_rootx() - self.root.winfo_rootx()
                        y = widget.winfo_rooty() - self.root.winfo_rooty()
                        self.assertGreaterEqual(x, 0)
                        self.assertGreaterEqual(y, 0)
                        self.assertLessEqual(x + widget.winfo_width(), self.root.winfo_width())
                        self.assertLessEqual(y + widget.winfo_height(), self.root.winfo_height())
        self.root.withdraw()

    def test_large_result_is_paginated_without_dropping_jobs(self):
        self.load_jobs(self.jobs(205))
        self.assertEqual(len(self.app.results_table.get_children()), 100)
        self.app._change_result_page(1)
        self.assertEqual(len(self.app.results_table.get_children()), 100)
        self.app._change_result_page(1)
        self.assertEqual(len(self.app.results_table.get_children()), 5)
        self.assertEqual(str(self.app.next_page_button.cget("state")), "disabled")

    def test_switching_runs_clears_old_jobs_and_discards_stale_filters(self):
        self.load_jobs(self.jobs())
        old_request = self.app._filter_request_id
        gate = threading.Event()

        def loader(_path):
            gate.wait(timeout=3)
            return {"jobs": [], "warnings": []}

        with patch.object(desktop_ui.desktop_data, "load_run_results", side_effect=loader):
            self.app._load_result_run(Path(self.temp.name) / "new-run")
            self.assertEqual(self.app._filtered_jobs, [])
            self.assertEqual(self.app.results_table.get_children(), ())
            self.app._workspace_event("result_filter", (old_request, self.jobs()))
            self.assertEqual(self.app._filtered_jobs, [])
            gate.set()
            self.wait_until(lambda: not self.app.result_count.get().startswith("正在"))

    def test_history_selection_survives_insertion_by_path_not_row_number(self):
        def row(name):
            return {"path": str(Path(self.temp.name) / name), "name": name, "unique_job_count": 0}
        self.app._viewed_run = Path(self.temp.name) / "a"
        self.app._workspace_event("history_loaded", (0, [row("a"), row("b")]))
        self.app.history_table.selection_set("history-1")
        self.app._workspace_event("history_loaded", (0, [row("new"), row("a"), row("b")]))
        self.assertEqual(self.app.history_table.selection(), ("history-2",))
        self.app.notebook.select(self.app.history_page)
        self.assertEqual(self.app._artifact_run(), Path(self.temp.name) / "b")

    def test_unknown_live_snapshot_does_not_invent_progress(self):
        self.app._apply_snapshot({})
        self.assertEqual(self.app._metric_values["unique"].get(), "—")
        self.assertEqual(self.app._metric_values["jd"].get(), "—")
        snapshot = dict(unique_job_count=50, jd_success=42, jd_total=43, llm_success=42, llm_total=42, classification_counts={"重点投递": 7, "海投池": 28, "暂不投": 15})
        self.app._apply_snapshot(snapshot)
        self.assertEqual([self.app._metric_values[key].get() for key in ("unique", "jd", "llm", "priority", "pool", "excluded")], ["50", "42", "42", "7", "28", "15"])

    def test_unclassified_jobs_show_unknown_instead_of_zero(self):
        self.app._apply_snapshot(dict(unique_job_count=50, classification_counts={}, classification_status="unclassified", unclassified_count=50))
        self.assertEqual([self.app._metric_values[key].get() for key in ("priority", "pool", "excluded")], ["—", "—", "—"])
        self.assertEqual(self.app._metric_notes["priority"].get(), "尚未分层")
        self.assertIn("未分层 50 岗", self.app.live_detail.get())
        self.app._apply_snapshot(dict(classification_counts={"海投池": 3}, classification_status="partial", unclassified_count=47))
        self.assertEqual(self.app._metric_values["pool"].get(), "3")
        self.assertEqual(self.app._metric_values["priority"].get(), "—")
        self.assertEqual(self.app._metric_notes["pool"].get(), "仅已分层部分")

    def test_new_run_resets_pause_and_human_prompt_controls(self):
        self.app.prompt_button_text.set("继续此任务")
        self.app.continue_button.configure(state="normal")
        self.app._apply_snapshot({"unique_job_count": 99, "classification_counts": {"重点投递": 99}})
        self.app.updated_text.set("旧检查点")
        self.app._on_workspace_start("列表前处理")
        self.assertEqual(self.app.prompt_button_text.get(), "人工处理完成，继续")
        self.assertEqual(str(self.app.continue_button.cget("state")), "disabled")
        self.assertTrue(all(value.get() == "—" for value in self.app._metric_values.values()))
        self.assertTrue(all(note.get() == "等待本次数据" for note in self.app._metric_notes.values()))
        self.assertEqual(self.app.updated_text.get(), "等待本次检查点")

    def test_collection_metrics_never_fall_back_to_a_browsed_historical_run(self):
        old_run = self.source / "runs" / "old"
        self.app.active_run = self.source
        self.app._viewed_run = old_run
        self.app._collection_only = True
        self.app.workflow_running = False
        self.app._workspace_event("finished", {"exit_code": 0, "output": str(self.source), "manifest": {"stages": {"岗位采集": {"status": "success"}}}})
        self.assertIsNone(self.app._live_snapshot_target())
        self.root.after_cancel(self.app._live_after_id)
        self.app._live_after_id = None
        with patch.object(desktop_ui.desktop_data, "read_live_snapshot") as read:
            self.app._refresh_live()
            self.root.update()
        read.assert_not_called()
        for path in (old_run, self.source):
            self.app._workspace_event("live_snapshot", (str(path), {"unique_job_count": 99, "classification_counts": {"重点投递": 99}}))
        self.assertTrue(all(value.get() == "—" for value in self.app._metric_values.values()))
        self.assertIn("仅采集", self.app.live_detail.get())
        self.assertEqual(self.app._viewed_run, old_run)
        self.app.notebook.select(self.app.results_page)
        self.assertEqual(self.app._artifact_run(), old_run)

    def test_task_metrics_follow_active_task_instead_of_historical_browsing(self):
        active = self.source / "runs" / "active"
        browsed = self.source / "runs" / "browsed"
        self.app.active_run = active
        self.app._viewed_run = browsed
        self.app.workflow_running = False
        self.assertEqual(self.app._live_snapshot_target(), active)
        self.app._workspace_event("live_snapshot", (str(browsed), {"unique_job_count": 99}))
        self.assertEqual(self.app._metric_values["unique"].get(), "—")
        self.app._workspace_event("live_snapshot", (str(active), {"unique_job_count": 50}))
        self.assertEqual(self.app._metric_values["unique"].get(), "50")
        self.assertEqual(self.app._metric_notes["unique"].get(), "当前任务")
        self.app.active_run = None
        self.assertEqual(self.app._live_snapshot_target(), browsed)

    def test_resume_button_reuses_the_active_paused_directory(self):
        self.app._pause_requested = True
        self.app.active_run = Path(self.temp.name) / "paused-run"
        with patch.object(self.app, "_start") as start:
            self.app._continue_child()
        start.assert_called_once()
        self.assertEqual(self.app.resume_run.get(), str(self.app.active_run))

    def test_busy_state_locks_task_settings_but_not_result_browsing(self):
        self.app._set_busy(True)
        self.assertEqual(str(self.app.start_button.cget("state")), "disabled")
        self.assertTrue(all(str(widget.cget("state")) == "disabled" for widget, _ in self.app._settings_controls))
        self.assertEqual(str(self.app.result_search_entry.cget("state")), "normal")
        self.app._set_busy(False)
        self.assertEqual(str(self.app.history_box.cget("state")), "readonly")

    def test_export_filtered_jobs_writes_a_separate_file(self):
        self.load_jobs(self.jobs())
        self.app.result_class.set("重点投递")
        self.wait_until(lambda: len(self.app._filtered_jobs) == 7)
        path = Path(self.temp.name) / "export.json"
        with patch.object(desktop_ui.filedialog, "asksaveasfilename", return_value=str(path)):
            self.app._export_filtered_jobs()
        self.wait_until(lambda: "已导出" in self.app.result_count.get())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["total"], 7)
        self.assertEqual(len(payload["jobs"]), 7)

    def test_export_cannot_overwrite_a_raw_combination(self):
        self.load_jobs(self.jobs())
        path = self.source / "raw" / "current" / "shanghai" / "some-combination.json"
        write_json(path, {"jobs": ["keep-source"]})
        original = path.read_bytes()
        with patch.object(desktop_ui.filedialog, "asksaveasfilename", return_value=str(path)), patch.object(desktop_ui.messagebox, "showerror") as error:
            self.app._export_filtered_jobs()
        error.assert_called_once()
        self.assertEqual(path.read_bytes(), original)

    def test_post_stage_never_automatically_uses_the_collection_root(self):
        (self.source / "prepared").mkdir()
        self.app.active_run = self.source
        self.app.stage.set("生成报告")
        self.app.resume_run.set("新建运行目录")
        with patch.object(desktop_app.messagebox, "showerror") as error:
            self.app._start()
        error.assert_called_once()
        self.assertIsNone(self.app.worker)
        self.assertFalse((self.source / "run_manifest.json").exists())

    def test_resume_without_a_valid_manifest_does_not_overwrite_old_output(self):
        old_run = self.source / "runs" / "unknown-run"
        artifact = old_run / "prepared" / "merged_jobs_index.json"
        write_json(artifact, {"jobs": ["keep-old"]})
        original = artifact.read_bytes()
        self.app.resume_run.set(str(old_run))
        with patch.object(desktop_app.messagebox, "showerror") as error:
            self.app._start()
        error.assert_called_once()
        self.assertIsNone(self.app.worker)
        self.assertEqual(artifact.read_bytes(), original)

    def test_loading_results_runs_off_the_tk_thread(self):
        thread_ids = []

        def loader(_path):
            thread_ids.append(threading.get_ident())
            return {"jobs": self.jobs(), "warnings": []}

        with patch.object(desktop_ui.desktop_data, "load_run_results", side_effect=loader):
            self.app._load_result_run(Path(self.temp.name) / "run")
            self.wait_until(lambda: len(self.app._filtered_jobs) == 50)
        self.assertTrue(thread_ids)
        self.assertTrue(all(value != threading.get_ident() for value in thread_ids))

    def test_local_shanghai_fifty_workflow_preserves_raw_and_old_run(self):
        raw = self.source / "raw" / "current" / "shanghai" / "current__shanghai__solarch__20_50__unlimited.json"
        jobs = [dict(job_id=f"sh-{i:03}", title="AI解决方案工程师", skills=["Agent", "RAG"], city="上海", salary="20-30K", experience="经验不限", company="本地验收") for i in range(80)]
        write_json(raw, {"city": "上海", "filters": {"experience": "经验不限", "salary": "20-50K"}, "jobs": jobs})
        old_report = self.source / "runs" / "old-run" / "prepared" / "海投分层清单.md"
        old_report.parent.mkdir(parents=True)
        old_report.write_text("keep old report", encoding="utf-8")
        old_bytes, raw_bytes = old_report.read_bytes(), raw.read_bytes()
        self.app.stage.set("列表前处理")
        self.app._start()
        self.app._set_theme("dark", persist=False)
        self.wait_until(lambda: self.app.active_run is not None or not self.app.workflow_running)
        first = self.app.active_run
        self.wait_until(lambda: not self.app.workflow_running, timeout=20)
        self.assertEqual(self.app.theme.get(), "dark")
        self.assertEqual(first.parent, self.source / "runs")
        index = json.loads((first / "prepared" / "merged_jobs_index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(index["jobs"]), 50)
        self.assertEqual(len({job["job_id"] for job in index["jobs"]}), 50)
        self.app.stage.set("生成报告")
        self.app.resume_run.set(str(first))
        self.app._start()
        self.wait_until(lambda: not self.app.workflow_running, timeout=20)
        report = first / "prepared" / "海投分层清单.md"
        self.assertTrue(report.is_file())
        state = json.loads((first / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(state["classification_counts"].values()), 50)
        self.assertEqual(raw.read_bytes(), raw_bytes)
        self.assertEqual(old_report.read_bytes(), old_bytes)
        self.app.stage.set("列表前处理")
        self.app.resume_run.set("新建运行目录")
        self.app._start()
        self.wait_until(lambda: self.app.active_run != first or not self.app.workflow_running)
        self.assertNotEqual(self.app.active_run, first)
        self.wait_until(lambda: not self.app.workflow_running, timeout=20)

    def test_daily_root_auto_jd_prepares_local_lists_in_a_new_run(self):
        from desktop_routing import AUTO_RUN_CHOICE

        raw = self.source / "raw" / "current" / "shanghai" / "current__shanghai__solarch__20_50__unlimited.json"
        jobs = [dict(job_id=f"auto-{i:03}", title="AI解决方案工程师", skills=["Agent", "RAG"], city="上海", salary="20-30K", experience="经验不限", company="本地验收") for i in range(80)]
        write_json(raw, {"city": "上海", "filters": {"experience": "经验不限", "salary": "20-50K"}, "jobs": jobs})
        source_bytes = raw.read_bytes()
        scraper = Path(self.temp.name) / "scraper"
        upstream = scraper / "scripts" / "boss_cdp_raw.py"
        upstream.parent.mkdir(parents=True)
        upstream.write_text("# Mock upstream, never executed.\n", encoding="utf-8")
        self.app.scraper_root.set(str(scraper))
        self.app.resume_run.set(AUTO_RUN_CHOICE)
        self.app.stage.set("JD补抓")
        original_child = self.app._run_child
        called = []

        def child(command):
            stage = command[command.index("--stage") + 1]
            called.append(stage)
            if stage == "shortlist":
                return original_child(command)
            self.assertEqual(stage, "fetch-jd", "Automatic routing must not add model/report stages")
            output = Path(command[command.index("--output-root") + 1])
            directory = output / "prepared" / "jd_shortlists"
            candidates = json.loads((directory / "jd_input.json").read_text(encoding="utf-8"))["jobs"]
            size = int(command[command.index("--batch-size") + 1])
            records = [dict(batch=offset // size + 1, status="success", exit_code=0, fetched_count=len(candidates[offset:offset + size]), missing_job_ids=[], security_or_login_gate=False) for offset in range(0, len(candidates), size)]
            write_json(directory / "details.json", {"jobs": [dict(job_id=job["job_id"], jd="负责 Agent/RAG 应用方案设计、系统集成、PoC交付与迭代评测。") for job in candidates]})
            write_json(directory / "details_manifest.json", dict(total_jobs=len(candidates), batch_size=size, records=records))
            return 0

        with patch.object(self.app, "_run_child", side_effect=child):
            self.app._start()
            self.wait_until(lambda: not self.app.workflow_running, timeout=20)
        self.assertEqual(called, ["shortlist", "fetch-jd"])
        self.assertEqual(self.app.active_run.parent, self.source / "runs")
        state = json.loads((self.app.active_run / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertIn("routing", state)
        self.assertEqual(state["stages"]["JD补抓"]["status"], "success")
        self.assertEqual(raw.read_bytes(), source_bytes)

    def test_auto_model_without_jd_never_silently_starts_collection(self):
        from desktop_routing import AUTO_RUN_CHOICE

        write_json(self.source / "raw" / "current" / "shanghai" / "current__shanghai__solarch__20_50__unlimited.json", {"jobs": [{"job_id": "raw-only", "title": "AI应用架构师", "skills": ["RAG"], "city": "上海"}]})
        self.app.resume_run.set(AUTO_RUN_CHOICE)
        self.app.stage.set("Codex语义复核")
        with patch.object(self.app, "_run_child") as child, patch.object(desktop_app.messagebox, "showerror"):
            self.app._start()
            self.wait_until(lambda: not self.app.workflow_running)
        child.assert_not_called()
        self.assertFalse((self.source / "run_manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
