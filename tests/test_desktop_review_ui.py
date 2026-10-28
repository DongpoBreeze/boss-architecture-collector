"""Real Tk + durable temporary tasks; no browser, collector, or model requests."""

import argparse
import copy
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import desktop_review_ui as ui
import desktop_ui
import project_llm_settings
import review_pipeline
from desktop_tasks import ReviewTask, STAGES, job_id


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class ReviewUIHelpersTests(unittest.TestCase):
    def test_ai_view_shows_only_judgment_reason_metadata_and_manual_context(self):
        review = {"job_id": "ai-ui", "status": "success",
                  "review_format": "simple-v1", "判断": "符合", "理由": "配合开发完成企业方案设计与交付。",
                  "review_model": "fixture-model"}
        row = review_pipeline._analyze_ai_stage(
            {"job_id": "ai-ui", "jd": "职位职责：拓展客户。", "jd_completeness": "complete",
             "classification": "暂不投", "reason": "旧代码证据不足", "hard_rule_reasons": ["旧硬排"]}, review)
        content = ui._evidence_text(row, stage="llm")
        for phrase in ("AI判断：符合", "配合开发完成企业方案设计与交付。", "fixture-model"):
            self.assertIn(phrase, content)
        for fixed_rule in ("不设代码语义门槛", "符合保留", "信息不足或未完成默认保留"):
            self.assertNotIn(fixed_rule, content)
        for obsolete in ("已有分层", "暂不投", "旧代码证据不足", "旧硬排", "代码硬规则", "六维", "置信", "能力差距", "模型证据"):
            self.assertNotIn(obsolete, content)
        app = SimpleNamespace(stage=SimpleNamespace(get=lambda: "llm"), _previews=set())
        self.assertIn("建议保留", ui.ReviewDesktopApp._row_reason(app, row))
        information = review_pipeline._analyze_ai_stage(
            {"job_id": "ai-ui", "jd": "完整职责正文"}, {**review, "判断": "信息不足", "理由": "缺少本人交付职责说明。"})
        self.assertIn("缺少本人交付职责说明。", ui.ReviewDesktopApp._row_reason(app, information))
        self.assertIn("待人工核实", ui.ReviewDesktopApp._row_reason(app, information))
        self.assertNotIn("默认保留", ui.ReviewDesktopApp._row_reason(app, information))
        pending = review_pipeline._analyze_ai_stage({"job_id": "ai-ui", "jd_completeness": "missing"})
        reason = ui.ReviewDesktopApp._row_reason(app, pending)
        self.assertIn("待人工核实", reason)
        self.assertIn("不在保留或剔除名单", reason)
        self.assertNotIn("默认保留", reason)
        self.assertNotIn("建议剔除", reason)
        self.assertNotIn("规则回退", ui._evidence_text(pending, stage="llm"))
        self.assertEqual(ui._metric_parts("llm", {"review_format": "simple-v1", "llm_success_count": 2, "llm_fallback_count": 3,
                                                "llm_verification_count": 4}),
                         ["AI 成功 2", "未完成AI判断 3", "待人工核实 4"])

    def test_old_rich_result_is_readonly_and_cannot_look_like_new_success(self):
        job = {"job_id": "legacy", "llm_stage_projection": "model_relevance_v1", "model_status": "success",
               "llm_stage_decision": "related", "recommended_keep": True,
               "llm_review": {"status": "success", "summary": "旧判断", "dimensions": {}}}
        original = copy.deepcopy(job)
        content = ui._evidence_text(job, stage="llm")
        for phrase in ("旧版结果", "只读历史", "不代表新版AI成功", "重置 AI 阶段后再点击运行才会切换为简化默认",
                       "重置会清除本阶段结果", "旧判断"):
            self.assertIn(phrase, content)
        self.assertNotIn("AI判断：符合", content)
        self.assertEqual(ui._metric_parts("llm", {"llm_success_count": 12}), ["旧版成功（只读，需重跑） 12"])
        app = SimpleNamespace(stage=SimpleNamespace(get=lambda: "llm"), _previews=set())
        self.assertIn("需重跑AI", ui.ReviewDesktopApp._row_reason(app, job))
        self.assertEqual(job, original)

    def test_new_report_detail_hides_old_rich_fields_and_separates_technical_failure(self):
        job = review_pipeline._analyze_ai_stage({"job_id": "failed", "jd": "完整正文"},
              {"job_id": "failed", "review_format": "simple-v1", "status": "failed", "error": "fixture timeout"})
        job.update(classification="海投池", jd_dimensions={"旧维度": {}}, ability_gaps=["旧能力差距"], confidence="high")
        detail = ui._evidence_text(job, stage="report")
        for phrase in ("未产生模型理由", "技术问题（不是AI理由）", "fixture timeout"):
            self.assertIn(phrase, detail)
        for phrase in ("六维", "旧维度", "能力差距", "confidence", "模型证据"):
            self.assertNotIn(phrase, detail)

    def test_saved_list_queues_are_mutually_exclusive_and_old_labels_are_never_rescored(self):
        examples = (
            ({"list_queue": "relevant", "list_pass": False}, ("relevant", False)),
            ({"list_queue": "verify"}, ("verify", False)),
            ({"list_queue": "excluded"}, ("excluded", False)),
            ({"list_pass": True, "list_priority": "优先补JD"}, ("relevant", True)),
            ({"list_pass": True, "list_priority": "低优先待JD"}, ("verify", True)),
            ({"list_pass": True}, ("verify", True)),
            ({"list_pass": False, "list_priority": "优先补JD"}, ("excluded", True)),
            ({"list_priority": "优先补JD"}, ("relevant", True)),
            ({"list_priority": "低优先待JD"}, ("verify", True)),
            ({"list_priority": "暂不优先"}, ("excluded", True)),
            ({"title": "AI架构师", "list_queue": "unknown"}, (None, True)),
        )
        for job, expected in examples:
            with self.subTest(job=job):
                saved = copy.deepcopy(job)
                self.assertEqual(ui._saved_list_queue(job), expected)
                self.assertEqual(job, saved, "displaying compatibility must not rewrite an old result")
                if expected[1]:
                    self.assertIn(ui.LEGACY_LIST_QUEUE_NOTICE, ui._list_queue_reason(job))
                    self.assertIn("旧版兼容", ui._list_queue_label(job))
        counts, legacy = ui._list_queue_summary([job for job, _expected in examples])
        self.assertEqual(sum(counts.values()), len(examples) - 1)
        self.assertEqual(legacy, len(examples) - 3)

    def test_shortlist_queue_detail_and_metrics_use_saved_fields_and_redact_secrets(self):
        job = {"job_id": "queue-detail", "title": "列表岗位", "list_queue": "relevant",
               "list_queue_reason": "已保存的方向原因 Cookie=PRIVATE_COOKIE"}
        content = ui._evidence_text(job, stage="shortlist")
        for expected in ("列表方向队列：明确相关", "已保存的方向原因", "仍须 JD", "不是人工保留状态", "[REDACTED]"):
            self.assertIn(expected, content)
        self.assertNotIn("PRIVATE_COOKIE", content)
        counts = {"relevant": 17, "verify": 23, "excluded": 10}
        parts = ui._metric_parts("shortlist", {"list_queue_counts": counts})
        self.assertEqual(parts, ["明确相关 17", "待核实 23", "队列剔除 10"])
        self.assertEqual(ui._metric_parts("fetch-jd", {"list_queue_counts": counts}), [])

    def test_positive_settings_have_real_upper_and_lower_bounds(self):
        self.assertEqual(ui._positive("1", "并发", maximum=32), 1)
        self.assertEqual(ui._positive("32", "并发", maximum=32), 32)
        self.assertEqual(ui._positive("30", "批次", maximum=100), 30)
        self.assertIsNone(ui._positive("", "数量", optional=True))
        for value in ("0", "-1", "33", "1.2", "x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ui._positive(value, "并发", maximum=32)

    def test_sources_and_evidence_keep_all_provenance_and_redact_secrets(self):
        job = {"job_id": "x", "title": "方案", "observed_in": [
            {"keyword": "第一个关键词", "source_file": "one.json"},
            {"keyword": "第二个关键词", "source_file": "two.json", "cookie": "PRIVATE_COOKIE"},
        ], "jd": "系统集成职责", "salary_flags": ["固定薪资待核实"],
            "experience_risks": ["年限偏高"], "experience_gap": "经验要求偏高，用项目案例争取",
            "llm_review": {"summary": "有证据", "api_key": "PRIVATE_KEY"}}
        sources = ui._sources_text(job)
        self.assertIn("第一个关键词", sources)
        self.assertIn("第二个关键词", sources)
        self.assertIn("two.json", sources)
        self.assertNotIn("PRIVATE_COOKIE", sources)
        evidence = ui._evidence_text(job)
        for text in ("系统集成职责", "固定薪资待核实", "年限偏高", "用项目案例争取", "有证据"):
            self.assertIn(text, evidence)
        self.assertNotIn("PRIVATE_KEY", evidence)
        self.assertIn("[REDACTED]", evidence)

    def test_jd_evidence_reports_completeness_without_a_semantic_rejection(self):
        job = {"job_id": "jd-only", "title": "软件开发", "jd": "普通开发职责。", "jd_status": "部分可用",
               "jd_completeness": "partial", "jd_fetch_issue": "正文截断，待重试", "reason": "职责无命中",
               "classification": "暂不投", "jd_dimension_count": 0,
               "jd_record": {"jd": "普通开发职责。", "status": "partial", "cookie": "PRIVATE_COOKIE"}}
        evidence = ui._evidence_text(job, stage="fetch-jd")
        for expected in ("本步仅补正文", "正文部分可用", "正文截断，待重试", "抓取记录"):
            self.assertIn(expected, evidence)
        for unrelated in ("暂不投", "职责无命中", "六维证据", "PRIVATE_COOKIE"):
            self.assertNotIn(unrelated, evidence)
        self.assertIn("[REDACTED]", evidence)
        self.assertEqual(ui._jd_completeness_label({"jd": "完整旧记录。"}), "正文完整")
        self.assertEqual(ui._jd_completeness_label({"jd": "", "jd_completeness": "complete"}), "正文缺失")

    def test_jd_concurrency_preferences_round_trip_through_the_shared_whitelist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            desktop_ui.save_preferences({"jd_workers": 7, "workers": 11, "batch_size": 30,
                                         "api_key": "PRIVATE_KEY"}, path)
            self.assertEqual(desktop_ui.load_preferences(path),
                             {"jd_workers": "7", "workers": "11", "batch_size": "30"})
            self.assertNotIn("PRIVATE_KEY", path.read_text(encoding="utf-8"))

    def test_report_requires_completed_valid_upstream_not_merely_an_old_file(self):
        manifest = {"stages": {key: {"status": "completed", "confirmed": True} for key in STAGES}}
        self.assertTrue(ui._report_ready(manifest))
        manifest["stages"]["report"]["confirmed"] = False
        self.assertTrue(ui._report_ready(manifest), "a generated report can be opened before its final confirmation")
        for stage in STAGES:
            old = copy.deepcopy(manifest)
            old["stages"][stage]["status"] = "stale"
            with self.subTest(stage=stage):
                self.assertFalse(ui._report_ready(old))
        manifest["stages"]["shortlist"]["confirmed"] = False
        self.assertFalse(ui._report_ready(manifest))


class ReviewTkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "fixed-source"
        self.parent = self.base / "tasks"
        self.jobs = []
        for index in range(50):
            sources = [{"track": "current", "city": "上海", "keyword": f"检索词{index}",
                        "source_file": "raw/current/shanghai/jobs.json", "source_line_number": index + 1}]
            if index == 0:
                sources.append({"track": "target", "city": "上海", "keyword": "稀有来源",
                                "source_file": "raw/target/shanghai/jobs.json", "source_line_number": 1})
            self.jobs.append({"job_id": f"job-{index:03d}", "title": f"AI 方案工程师 {index:03d}",
                              "company": f"公司 {index:03d}", "city": "上海", "salary": "20-30K",
                              "experience": "1-3年", "observed_in": sources, "job_link": f"https://example.invalid/job/{index}"})
        write_json(self.source / "raw" / "current" / "shanghai" / "jobs.json", self.jobs)
        write_json(self.source / "raw" / "target" / "shanghai" / "jobs.json", [self.jobs[0]])
        self.raw_before = (self.source / "raw" / "current" / "shanghai" / "jobs.json").read_bytes()
        self.calls = []
        self.legacy_shortlist = False
        self.browser = Mock(return_value=True)
        self.collector = Mock()
        self.errors = Mock()
        self.ask = Mock(return_value=True)
        self.windows = []
        self.release_events = []
        for patcher in (
            patch.object(ui, "load_preferences", return_value={}),
            patch.object(ui, "save_preferences"),
            patch("project_llm_settings.default_settings_path", return_value=self.base / "project-llm.json"),
            patch("project_llm_settings.store_api_key"),
            patch.object(ui.messagebox, "showerror", self.errors),
            patch.object(ui.messagebox, "askyesno", self.ask),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._cleanup_windows)
        self.root, self.app = self.make_window()

    def make_window(self, **options):
        try:
            root = ui.tk.Tk()
        except ui.tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        root.withdraw()
        # Geometry checks map transparent, disabled windows so this offline
        # suite cannot display a workbench or take keyboard focus on Windows.
        root.attributes("-alpha", 0.0)
        if sys.platform == "win32":
            root.attributes("-disabled", True)
        callback_errors = []
        root.report_callback_exception = lambda *error: callback_errors.append(error)
        args = argparse.Namespace(root=self.source, config=ui.DEFAULT_CONFIG, scraper_root=None, task_parent=self.parent)
        try:
            app = ui.ReviewDesktopApp(root, args, executor=options.pop("executor", self.fake_executor),
                                      browser_preparer=options.pop("browser_preparer", self.browser),
                                      collector_opener=options.pop("collector_opener", self.collector), **options)
        except BaseException:
            root.destroy()
            raise
        self.windows.append((root, app, callback_errors))
        root.update_idletasks()
        return root, app

    def _cleanup_windows(self):
        for event in self.release_events:
            event.set()
        for root, app, _errors in reversed(self.windows):
            if app._closed:
                continue
            if app._control:
                app._control.stop()
            if app.worker:
                app.worker.join(timeout=3)
            root.update()
            self.ask.return_value = True
            app._close()
            deadline = time.monotonic() + 3
            while not app._closed and time.monotonic() < deadline:
                root.update()
                time.sleep(.005)
            if not app._closed:
                for identifier in tuple(app._after_ids):
                    app._cancel_after(identifier)
                root.destroy()
        self.windows.clear()

    def pump(self, duration=.16, *, root=None):
        root = root or self.root
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            root.update()
            time.sleep(.005)

    def wait_idle(self, app=None, root=None, timeout=8):
        app, root = app or self.app, root or self.root
        deadline = time.monotonic() + timeout
        while app._busy and time.monotonic() < deadline:
            root.update()
            time.sleep(.005)
        root.update()
        self.assertFalse(app._busy, f"worker did not finish: {app._operation}")
        for _r, _a, errors in self.windows:
            self.assertEqual(errors, [], "Tk callback raised an exception")

    def wait_model_check(self, app=None, root=None, timeout=5):
        app, root = app or self.app, root or self.root
        deadline = time.monotonic() + timeout
        while app._model_config_request_busy and time.monotonic() < deadline:
            root.update()
            time.sleep(.005)
        root.update()
        self.assertFalse(app._model_config_request_busy, "model configuration check did not finish")
        for _r, _a, errors in self.windows:
            self.assertEqual(errors, [], "Tk callback raised an exception")

    def fake_executor(self, task, stage, **options):
        self.calls.append((stage, threading.get_ident(), options))
        task.begin(stage, redo=options.get("redo", False))
        rows = copy.deepcopy(self.jobs) if stage == "import" else task.input_jobs(stage)
        for index, job in enumerate(rows):
            if stage == "shortlist":
                job["list_pass"] = index % 7 != 0
                job["reason"] = "职责待核实" if index % 7 == 0 else "技术方案相关"
                queue = "excluded" if not job["list_pass"] else "verify" if index % 2 else "relevant"
                job["list_priority"] = {"relevant": "优先补JD", "verify": "低优先待JD", "excluded": "暂不优先"}[queue]
                if not self.legacy_shortlist:
                    job["list_queue"] = queue
                    job["list_queue_reason"] = {"relevant": "列表方向有明确证据，仍须补 JD", "verify": "工作对象待 JD 核实", "excluded": "列表明确排除证据"}[queue]
            elif stage == "fetch-jd":
                job.update(jd="负责方案设计、系统集成与 Agent 工作流。", jd_status="success", recommended_keep=True)
            elif stage == "llm":
                job.update(review_pipeline._analyze_ai_stage(job, {
                    "job_id": job_id(job), "status": "success", "review_format": ui.SIMPLE_REVIEW_FORMAT,
                    "判断": "符合", "理由": "AI方案设计与企业交付相关。"}))
            elif stage == "report":
                job["classification"] = "重点投递" if index % 2 else "海投池"
        stats = {"completed_count": len(rows)}
        if stage == "import":
            stats.update(raw_record_count=51, deduplicated_job_count=50)
        if stage == "shortlist" and not self.legacy_shortlist:
            stats["list_queue_counts"] = {key: sum(job.get("list_queue") == key for job in rows)
                                          for key in ui.LIST_QUEUE_LABELS}
        if stage == "report":
            prepared = task.run_directory(stage) / "prepared"
            full = task.all_jobs()
            final = {job_id(job): job for job in rows}
            for job in full:
                job.update(final.get(job_id(job), {}))
                if job.get("excluded_at"):
                    job["classification"] = "暂不投"
            json_path = prepared / "merged_jobs_index.json"
            markdown_path = prepared / "海投分层清单.md"
            pool_path = prepared / "hai_tou_pool.json"
            write_json(json_path, full)
            write_json(pool_path, [job for job in full if job.get("classification") != "暂不投"])
            markdown_path.write_text("# 全部岗位追踪\n\n" + "\n".join(job_id(job) for job in full), encoding="utf-8")
            stats.update(json_path=str(json_path), markdown_path=str(markdown_path), pool_path=str(pool_path))
            stats.update(total_jobs=len(full), classification_counts={name: sum(job.get("classification") == name for job in full)
                                                                     for name in ("重点投递", "海投池", "暂不投")})
        task.record_progress(stage, len(rows), len(rows), stats=stats)
        options["on_event"]("progress", {"done": len(rows), "total": len(rows)})
        options["on_event"]("log", f"真实伪执行器完成 {stage}：{len(rows)}\n")
        task.finish(stage, rows, stats=stats)
        options["on_event"]("finished", task.snapshot())
        return task.snapshot()

    def create_task(self, app=None, root=None):
        app, root = app or self.app, root or self.root
        # Existing workflow fixtures intentionally cover a 50-job task; this
        # is an explicit range, not the application's default anymore.
        app.sample_mode.set(ui.SAMPLE_CHOICES[1])
        app.sample_size.set("50")
        app._sample_mode_changed()
        app._create_task()
        self.wait_idle(app, root)
        self.assertIsNotNone(app.task, self.errors.call_args_list)
        return app.task

    def complete_through(self, stage, *, confirm_last=False):
        if self.app.task is None:
            self.create_task()
        for key in STAGES[:STAGES.index(stage) + 1]:
            if self.app.stage.get() != key:
                self.app._select_stage(key)
            self.app._run_stage()
            self.wait_idle()
            self.assertEqual(self.app._stage_info()["status"], "completed", self.errors.call_args_list)
            if key != stage or confirm_last:
                self.app._confirm_stage()
                self.wait_idle()

    def task_json(self):
        return json.loads((self.app.task.path / "task_manifest.json").read_text(encoding="utf-8"))

    def queue_ids(self, queue):
        return [job_id(job) for job in self.app._rows["shortlist"] if job.get("list_queue") == queue]

    def test_reset_button_is_visible_on_all_five_pages_and_disabled_until_a_first_run(self):
        self.assertTrue(self.app.reset_button.instate(["disabled"]))
        self.create_task()
        self.root.deiconify()
        for stage in STAGES:
            self.app._select_stage(stage)
            self.root.update()
            self.assertTrue(self.app.reset_button.winfo_ismapped())
            self.assertEqual(self.app.reset_button.cget("text"), "重置本阶段")
            self.assertTrue(self.app.reset_button.instate(["disabled"]))
        self.assertEqual(self.calls, [])

    def test_cancelling_each_stage_reset_keeps_every_file_and_manifest_unchanged(self):
        self.complete_through("report", confirm_last=True)
        self.ask.return_value = False
        for stage in STAGES:
            with self.subTest(stage=stage):
                self.app._select_stage(stage)
                before = self.task_json()
                files = {path: path.read_bytes() for path in self.app.task.path.rglob("*") if path.is_file()}
                calls = len(self.calls)
                self.app.reset_button.invoke()
                self.pump(.03)
                self.assertEqual(self.task_json(), before)
                self.assertEqual({path: path.read_bytes() for path in self.app.task.path.rglob("*") if path.is_file()}, files)
                self.assertEqual(len(self.calls), calls)
                prompt = self.ask.call_args.args[1]
                for text in ("清除本阶段", "结果与确认保持不变", "下游", "失效", "不会立即", "运行本步"):
                    self.assertIn(text, prompt)

    def test_stage_reset_clears_current_outputs_without_running_and_separate_run_executes(self):
        self.complete_through("report", confirm_last=True)
        for stage in reversed(STAGES):
            with self.subTest(stage=stage):
                self.app._select_stage(stage)
                before = self.task_json()
                old_result = self.app.task.path / before["stages"][stage]["result_file"]
                self.assertTrue(old_result.is_file())
                count = len(self.calls)
                self.app.reset_button.invoke()
                self.wait_idle()
                self.assertEqual(len(self.calls), count, "reset must not invoke a stage executor")
                self.assertFalse(old_result.exists())
                after = self.task_json()
                info = after["stages"][stage]
                self.assertEqual(info["status"], "pending")
                self.assertFalse(info["confirmed"])
                self.assertIsNone(info["result_file"])
                self.assertEqual(info["progress"], {"done": 0, "total": 0})
                self.assertEqual(self.app.run_button.cget("text"), "运行本步")
                self.assertFalse(self.app.run_button.instate(["disabled"]))
                index = STAGES.index(stage)
                for earlier in STAGES[:index]:
                    self.assertEqual(after["stages"][earlier], before["stages"][earlier])
                for later in STAGES[index + 1:]:
                    self.assertEqual(after["stages"][later]["status"], "stale")
                    self.assertFalse(after["stages"][later]["confirmed"])

                self.app.run_button.invoke()
                self.wait_idle()
                self.assertEqual(len(self.calls), count + 1)
                self.assertEqual(self.calls[-1][0], stage)
                self.assertNotIn("redo", self.calls[-1][2])
                self.assertNotEqual(self.calls[-1][1], threading.get_ident())
                self.assertEqual(self.app._stage_info()["status"], "completed")
                self.app._confirm_stage()
                self.wait_idle()
                for later in STAGES[index + 1:]:
                    self.app._select_stage(later)
                    self.app.run_button.invoke()
                    self.wait_idle()
                    self.app._confirm_stage()
                    self.wait_idle()

    def test_reset_invalidates_stale_reports_without_running_downstream(self):
        self.complete_through("report", confirm_last=True)
        self.app._select_stage("shortlist")
        count = len(self.calls)
        self.app.reset_button.invoke()
        self.wait_idle()
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.app._stage_info()["status"], "pending")
        self.app._select_stage("report")
        self.assertTrue(self.app.open_report_button.instate(["disabled"]))
        self.assertTrue(self.app.reset_button.instate(["disabled"]))
        self.assertFalse(self.app._stage_info()["confirmed"])

    def test_reset_is_background_work_and_does_not_start_an_executor(self):
        self.complete_through("import")
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        main_thread = threading.get_ident()
        reset_threads = []
        original_reset = ui.reset_review_stage

        def slow_reset(task, stage):
            reset_threads.append(threading.get_ident())
            entered.set()
            release.wait(3)
            original_reset(task, stage)

        before = time.monotonic()
        with patch.object(ui, "reset_review_stage", new=slow_reset):
            self.app.reset_button.invoke()
        self.assertLess(time.monotonic() - before, .2)
        self.assertTrue(entered.wait(2))
        count = len(self.calls)
        self.assertTrue(self.app.reset_button.instate(["disabled"]))
        ticks = []
        self.root.after(1, lambda: ticks.append(True))
        self.pump(.04)
        self.assertEqual(ticks, [True])
        release.set()
        self.wait_idle()
        self.assertEqual(len(reset_threads), 1)
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.app._stage_info()["status"], "pending")
        self.assertNotEqual(reset_threads[0], main_thread)

    def test_reset_is_disabled_for_external_run_collection_and_close(self):
        self.complete_through("report", confirm_last=True)
        other = ReviewTask.open(self.app.task.path)
        self.addCleanup(other._release_run)
        other.begin("llm")
        self.app._refresh_task()
        self.wait_idle()
        self.app._select_stage("import")
        self.assertTrue(self.app.reset_button.instate(["disabled"]))
        other.interrupt("llm")
        self.app._refresh_task()
        self.wait_idle()
        self.assertFalse(self.app.reset_button.instate(["disabled"]))
        with patch.object(type(self.app), "_collection_busy", new_callable=PropertyMock, return_value=True):
            self.app._render()
            self.assertTrue(self.app.reset_button.instate(["disabled"]))
        self.app._closing = True
        self.app._render()
        self.assertTrue(self.app.reset_button.instate(["disabled"]))
        self.app._closing = False

    def test_reset_does_not_depend_on_executor_support(self):
        self.complete_through("import")
        before = self.task_json()

        def old_executor(task, stage, on_event=None):
            raise AssertionError("reset must not invoke the stage executor")

        self.app.executor = old_executor
        self.app.reset_button.invoke()
        self.wait_idle()
        self.assertFalse(self.errors.called)
        self.assertNotEqual(self.task_json(), before)
        self.assertEqual(self.app._stage_info()["status"], "pending")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.app._busy)

    def test_start_and_navigation_make_no_requests_or_task_directories(self):
        self.assertIsNone(self.app.task)
        self.assertFalse(self.parent.exists())
        with patch("llm_client.load_codex_config", side_effect=AssertionError("navigation read model credentials")), \
             patch("project_llm_settings.load_api_key", side_effect=AssertionError("idle UI read saved API key")):
            for stage in STAGES:
                self.app._select_stage(stage)
        self.pump()
        self.assertEqual(self.calls, [])
        self.browser.assert_not_called()
        self.collector.assert_not_called()
        self.assertIsNone(self.app.worker)
        self.assertFalse(self.parent.exists())

    def test_collection_and_review_are_visible_separate_pages_without_execution(self):
        self.root.geometry("1024x760")
        self.root.deiconify()
        self.root.update()
        app = self.app
        source_before = app.source_root.get()
        self.assertEqual(app.workspace.get(), "review")
        self.assertEqual(tuple(app.stage_buttons), STAGES)
        self.assertFalse(app.collection_page.winfo_manager())
        for theme in ("dark", "light"):
            app._set_theme(theme, persist=False)
            with patch.object(ui.subprocess, "Popen") as launch:
                app.collector_button.invoke()
                self.root.update()
                self.assertEqual(app.workspace.get(), "collection")
                self.assertTrue(app.collection_page.winfo_ismapped())
                self.assertFalse(app.main_pane.winfo_manager())
                self.assertFalse(app.stage_navigation.winfo_manager())
                self.assertFalse(app.setup_frame.winfo_manager())
                self.assertFalse(app.collection_page.busy)
                app.review_button.invoke()
                self.root.update()
                self.assertEqual(app.workspace.get(), "review")
                self.assertTrue(app.main_pane.winfo_ismapped())
                self.assertTrue(app.setup_frame.winfo_ismapped())
                self.assertFalse(app.collection_page.winfo_manager())
                launch.assert_not_called()
            for button in (app.collector_button, app.review_button, app.theme_button):
                self.assertTrue(button.winfo_ismapped())
                self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                     self.root.winfo_rootx() + self.root.winfo_width())
        self.assertEqual(app.source_root.get(), source_before)
        self.assertEqual(self.calls, [])
        self.browser.assert_not_called()
        self.assertFalse(self.parent.exists())

    def test_collection_refresh_shortcut_targets_only_the_visible_workspace(self):
        app = self.app
        self.assertIn("_refresh_workspace", self.root.bind("<F5>"))
        with patch.object(app.collection_page, "refresh_progress") as collection_refresh, \
                patch.object(app, "_refresh_task") as review_refresh:
            app._show_workspace("collection")
            self.assertEqual(app._refresh_workspace(), "break")
            collection_refresh.assert_called_once_with()
            review_refresh.assert_not_called()
            app._show_workspace("review")
            self.assertEqual(app._refresh_workspace(), "break")
            review_refresh.assert_called_once_with()
            collection_refresh.assert_called_once_with()
            app._closing = True
            try:
                self.assertEqual(app._refresh_workspace(), "break")
            finally:
                app._closing = False
            review_refresh.assert_called_once_with()
            collection_refresh.assert_called_once_with()
        self.assertEqual(self.calls, [])
        self.browser.assert_not_called()

    def test_switching_collection_page_preserves_review_task_selection_and_scope(self):
        self.complete_through("shortlist")
        self.app._set_keep(["job-001"], False)
        self.wait_idle()
        task = self.app.task
        before = (task.path / "task_manifest.json").read_bytes()
        self.app.search.set("方案")
        self.pump()
        rows = copy.deepcopy(self.app._rows)
        scope = (self.app.source_root.get(), self.app.city.get(), self.app.sample_size.get())
        calls = len(self.calls)
        self.app._show_workspace("collection")
        self.app._set_theme("dark", persist=False)
        self.app._show_workspace("review")
        self.assertIs(self.app.task, task)
        self.assertEqual(self.app.stage.get(), "shortlist")
        self.assertEqual(self.app.search.get(), "方案")
        self.assertEqual(self.app._rows, rows)
        self.assertEqual((self.app.source_root.get(), self.app.city.get(), self.app.sample_size.get()), scope)
        self.assertEqual((task.path / "task_manifest.json").read_bytes(), before)
        self.assertEqual(len(self.calls), calls)

    def test_manual_collection_handoff_only_fills_new_review_task_form(self):
        self.complete_through("import")
        old_task = self.app.task
        manifest_path = old_task.path / "task_manifest.json"
        before = manifest_path.read_bytes()
        calls = len(self.calls)
        new_source = self.base / "new-collection"
        new_source.mkdir()
        custom_config = self.base / "custom-collection-plan.json"
        write_json(custom_config, json.loads(ui.DEFAULT_CONFIG.read_text(encoding="utf-8")))
        self.app.collection_page.config_path = custom_config
        self.app._show_workspace("collection")
        self.ask.return_value = False
        self.app._use_collection_source(new_source)
        self.assertIs(self.app.task, old_task)
        self.assertEqual(self.app.workspace.get(), "collection")
        self.ask.return_value = True
        self.app._use_collection_source(new_source)
        self.assertIsNone(self.app.task)
        self.assertEqual(self.app.workspace.get(), "review")
        self.assertEqual(self.app.source_root.get(), str(new_source))
        self.assertEqual(self.app.config_path, custom_config)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertIn("尚未创建任务", self.app.notice.get())

    def test_new_review_task_imports_custom_collected_keywords_without_changing_old_sources(self):
        config = json.loads(ui.DEFAULT_CONFIG.read_text(encoding="utf-8"))
        config["current"]["keywords"].append({"id": "custom_consultant", "query": "企业智能体咨询"})
        snapshot = self.source / "collection-plans" / "custom.json"
        write_json(snapshot, config)
        for key, identity in (("solarch", "old-source-job"), ("custom_consultant", "custom-source-job")):
            write_json(self.source / "raw" / "current" / "shanghai" /
                       f"current__shanghai__{key}__20_50__1_3.json",
                       {"city": "上海", "filters": {"salary": "406", "experience": "104", "degree": "203"},
                        "jobs": [{"job_id": identity, "title": "AI方案咨询", "city": "上海", "salary": "20-30K"}]})
        before = (snapshot.read_bytes(), snapshot.stat().st_mtime_ns, ui.DEFAULT_CONFIG.read_bytes())
        root, app = self.make_window(executor=review_pipeline.execute_stage)
        with patch.object(ui, "REVIEW_SOURCE_CONFIGS", self.base / "review-source-configs"):
            self.create_task(app, root)
        config_path = Path(app.task.manifest["config_path"])
        self.assertNotEqual(config_path, ui.DEFAULT_CONFIG)
        self.assertTrue(config_path.is_relative_to(self.base / "review-source-configs"))
        app.run_button.invoke()
        self.wait_idle(app, root)
        self.assertEqual({job_id(job) for job in app.task.jobs("import")}, {"old-source-job", "custom-source-job"})
        self.assertEqual((snapshot.read_bytes(), snapshot.stat().st_mtime_ns, ui.DEFAULT_CONFIG.read_bytes()), before)
        self.assertEqual(app.task.stage_info("shortlist")["status"], "pending")
        self.browser.assert_not_called()

    def test_invalid_source_plan_blocks_new_task_instead_of_silently_missing_jobs(self):
        snapshot = self.source / "collection-plans" / "broken.json"
        snapshot.parent.mkdir()
        snapshot.write_text("{broken", encoding="utf-8")
        before = snapshot.read_bytes()
        with patch.object(ui, "REVIEW_SOURCE_CONFIGS", self.base / "review-source-configs"):
            self.app._create_task()
            self.wait_idle()
        self.assertIsNone(self.app.task)
        self.assertTrue(self.errors.called)
        self.assertEqual(snapshot.read_bytes(), before)
        self.assertFalse((self.base / "review-source-configs").exists())
        self.assertEqual(self.calls, [])

    def test_collection_and_review_execution_are_mutually_exclusive(self):
        self.create_task()
        collection_type = type(self.app.collection_page)
        from unittest.mock import PropertyMock
        with patch.object(collection_type, "busy", new_callable=PropertyMock, return_value=True):
            self.app._collection_state_changed(True)
            self.assertFalse(self.app._can_run())
            self.assertTrue(self.app.run_button.instate(["disabled"]))
            self.assertTrue(self.app.create_button.instate(["disabled"]))
            self.app._run_stage()
            self.app._prepare_browser()
            self.app._submit("blocked", lambda: self.fail("ran review while collecting"), status="blocked")
            self.assertFalse(self.app._busy)
            self.assertIn("岗位采集正在运行", self.app.notice.get())
        self.assertEqual(self.calls, [])
        self.browser.assert_not_called()
        self.app._collection_state_changed(False)
        self.assertTrue(self.app._can_run())
        self.app._busy = True
        try:
            self.assertFalse(self.app._can_start_collection())
            self.app._show_workspace("collection")
            self.assertEqual(self.app.workspace.get(), "collection", "navigation is read-only even during review")
        finally:
            self.app._busy = False

    def test_concurrency_defaults_and_saved_values_are_independent(self):
        self.assertEqual(self.app.workers.get(), "10")
        self.assertEqual(self.app.jd_workers.get(), "3")
        self.assertEqual(self.app.batch_size.get(), "30")
        with patch.object(ui, "load_preferences", return_value={"workers": "8", "jd_workers": "6"}):
            _root, restored = self.make_window()
        self.assertEqual(restored.workers.get(), "8")
        self.assertEqual(restored.jd_workers.get(), "6")
        with patch.object(ui, "load_preferences", return_value={"workers": "5"}):
            _root, old_preferences = self.make_window()
        self.assertEqual(old_preferences.workers.get(), "5")
        self.assertEqual(old_preferences.jd_workers.get(), "3")
        self.assertEqual(self.calls, [])

    def test_browser_defaults_to_dual_and_respects_explicit_single_preference(self):
        self.assertEqual(self.app._browser_mode_value(), "dual")
        self.assertIn("Chrome + Edge", self.app.prepare_browser_button.cget("text"))
        with patch.object(ui, "load_preferences", return_value={"browser_mode": "single"}):
            _root, restored = self.make_window()
        self.assertEqual(restored._browser_mode_value(), "single")
        self.assertIn("disabled", restored.edge_port_entry.state())
        with patch.object(ui, "load_preferences", return_value={"browser_mode": "dual"}):
            _root, dual = self.make_window()
        self.assertEqual(dual._browser_mode_value(), "dual")
        self.assertEqual(self.calls, [])
        self.browser.assert_not_called()

    def test_browser_combobox_selection_persists_immediately_without_running(self):
        app = self.app
        app._select_stage("fetch-jd")
        for mode in ("single", "dual"):
            app.browser_mode.set(ui.BROWSER_MODE_LABELS[mode])
            app.browser_mode_box.event_generate("<<ComboboxSelected>>")
            self.root.update()
            for worker in app._preference_workers:
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
            self.assertEqual(ui.save_preferences.call_args.args[0]["browser_mode"], mode)
            self.assertEqual(app._browser_mode_value(), mode)
        self.assertEqual(self.calls, [])
        self.browser.assert_not_called()

    def test_visible_dual_mode_passes_to_executor_and_processor_command(self):
        self.complete_through("shortlist", confirm_last=True)
        app = self.app
        app.jd_workers.set("2")
        app._run_stage()
        self.wait_idle()
        stage, _thread, options = self.calls[-1]
        self.assertEqual(stage, "fetch-jd")
        self.assertEqual(options["browser_mode"], "dual")
        self.assertEqual((options["chrome_port"], options["edge_port"], options["jd_workers"]), (9222, 9223, 2))
        self.assertFalse(app._stage_info()["confirmed"])
        from review_pipeline import _process_command
        command = _process_command(app.task, app.task.run_directory("fetch-jd"), "fetch-jd",
                                   workers=10, batch_size=30, scraper_root=None,
                                   jd_workers=options["jd_workers"], browser_mode=options["browser_mode"],
                                   chrome_port=options["chrome_port"], edge_port=options["edge_port"],
                                   chrome_profile_dir=options["chrome_profile_dir"],
                                   edge_profile_dir=options["edge_profile_dir"])
        self.assertEqual(command[command.index("--browser-mode") + 1], "dual")
        self.assertEqual(command[command.index("--edge-port") + 1], "9223")
        self.assertIn("Chrome + Edge 各半（dual）", app.log_view.get("1.0", "end-1c"))

    def test_preparing_browsers_uses_the_same_visible_dual_mode_as_jd(self):
        self.create_task()
        self.app._select_stage("fetch-jd")
        self.app.scraper_root.set(str(self.base / "fake-tool"))
        self.app._prepare_browser()
        self.wait_idle()
        self.assertEqual(self.browser.call_args.kwargs["browser_mode"], "dual")
        self.assertEqual(self.calls, [])

    def test_stage_descriptions_replace_static_rule_views(self):
        app = self.app
        self.assertFalse(app.settings_frame.winfo_manager())
        self.assertFalse(hasattr(app, "rules_button"))
        detail_tabs = [app.detail_notebook.tab(tab_id, "text") for tab_id in app.detail_notebook.tabs()]
        self.assertNotIn("当前规则", detail_tabs)
        self.assertNotIn("完整规则", detail_tabs)
        for stage in ("shortlist", "fetch-jd", "llm"):
            app._select_stage(stage)
            self.assertEqual(app.stage_description.get(), ui.STAGE_DESCRIPTIONS[stage])
            self.assertNotIn("当前规则：", app.stage_description.get())
            self.assertNotIn("当前执行规则：", app.detail_text.get("1.0", "end-1c"))
            self.assertNotIn(ui.default_review_rules(), app.detail_text.get("1.0", "end-1c"))
            self.assertEqual(bool(app.jd_settings_frame.winfo_manager()), stage == "fetch-jd")
            self.assertEqual(bool(app.llm_settings_frame.winfo_manager()), stage == "llm")
            self.assertFalse(app.settings_frame.winfo_manager())
        self.assertEqual(app.edit_rules_button.cget("text"), "修改复核规则")
        self.assertTrue(app.edit_rules_button.winfo_manager())
        self.assertIn("每页滚动3次", app.jd_mode_label.cget("text"))
        self.assertIn("仅补正文不筛选", app.jd_mode_label.cget("text"))
        self.assertIn("AI 并发（每岗独立）", [str(widget.cget("text"))
                                         for widget in app.llm_settings_frame.winfo_children()
                                         if isinstance(widget, ui.ttk.Label)])
        self.assertIn("gpt-5.6-luna", app.model_config_label.cget("text"))
        self.assertIn("max", app.model_config_label.cget("text"))
        self.assertIn("https://nikoapi.xyz/v1", app.model_config_label.cget("text"))
        self.assertIn("API Key：Windows Credential Manager", app.model_config_label.cget("text"))
        self.assertEqual(app.model_config_button.cget("text"), "修改模型配置")
        self.assertEqual(self.calls, [])

    def test_review_rules_editor_saves_only_this_task_and_invalidates_ai_and_report(self):
        self.complete_through("report", confirm_last=True)
        app = self.app
        before = app.task.snapshot()
        task_path = app.task.path
        default_rules = app.task.get_review_rules()
        calls = len(self.calls)
        app._select_stage("llm")
        self.assertTrue(app.edit_rules_button.winfo_manager())
        with patch("llm_client.load_codex_config", side_effect=AssertionError("editor read credentials")):
            app.edit_rules_button.invoke()
            self.assertIsNotNone(app._review_rules_dialog)
            editor = app._review_rules_editor
            self.assertEqual(editor.get("1.0", "end-1c"), default_rules)
            custom = default_rules + "\n\n优先考虑本人承担业务方案设计及项目实施的岗位。"
            editor.delete("1.0", "end")
            editor.insert("1.0", custom)
            app._review_rules_buttons["save"].invoke()
            self.wait_idle()
        self.assertIsNone(app._review_rules_dialog)
        self.assertEqual(len(self.calls), calls, "saving must not run any stage")
        after = app.task.snapshot()
        for stage in ("import", "shortlist", "fetch-jd"):
            self.assertEqual(before["stages"][stage], after["stages"][stage])
        for stage in ("llm", "report"):
            self.assertEqual(after["stages"][stage]["status"], "stale")
            self.assertFalse(after["stages"][stage]["confirmed"])
        self.assertEqual(ReviewTask.open(task_path).get_review_rules(), custom)
        self.assertEqual(app._saved_review_rules, custom)
        self.assertNotIn(custom, app.detail_text.get("1.0", "end-1c"))
        self.assertNotIn(default_rules, app.detail_text.get("1.0", "end-1c"))
        self.assertNotIn("本任务自定义", app.detail_text.get("1.0", "end-1c"))
        app.edit_rules_button.invoke()
        self.assertEqual(app._review_rules_editor.get("1.0", "end-1c"), custom)
        app._close_review_rules_editor()
        self.assertEqual((self.source / "raw/current/shanghai/jobs.json").read_bytes(), self.raw_before)

    def test_review_rules_cancel_restore_default_and_blank_validation_are_explicit(self):
        self.complete_through("fetch-jd", confirm_last=True)
        app = self.app
        original = app.task.get_review_rules()
        app.edit_rules_button.invoke()
        self.assertIsNotNone(app._review_rules_editor)
        app._review_rules_editor.delete("1.0", "end")
        app._review_rules_buttons["save"].invoke()
        self.assertIn("不能为空", app._review_rules_status.get())
        self.assertFalse(app._busy)
        self.assertEqual(app.task.get_review_rules(), original)
        app._review_rules_editor.insert("1.0", "仅用于取消测试的规则。")
        app._review_rules_buttons["cancel"].invoke()
        self.assertIsNone(app._review_rules_dialog)
        self.assertEqual(app.task.get_review_rules(), original)
        custom = original + "\n\n优先系统集成职责。"
        app.task.set_review_rules(custom)
        app._refresh_task()
        self.wait_idle()
        app.edit_rules_button.invoke()
        app._review_rules_buttons["reset"].invoke()
        self.assertEqual(app._review_rules_editor.get("1.0", "end-1c"), app._default_review_rules)
        self.assertEqual(app.task.get_review_rules(), custom, "reset changes the draft until saved")
        app._review_rules_buttons["save"].invoke()
        self.wait_idle()
        self.assertEqual(app.task.get_review_rules(), original)
        self.assertFalse(app.task.snapshot().get("review_rules"))

    def test_review_rules_save_runs_in_background_and_preserves_draft_on_error(self):
        self.complete_through("fetch-jd", confirm_last=True)
        app = self.app
        app.edit_rules_button.invoke()
        draft = app._saved_review_rules + "\n\n职责证据优先。"
        app._review_rules_editor.insert("end", "\n\n职责证据优先。")
        threads = []

        def fail(_rules):
            threads.append(threading.get_ident())
            raise ui.TaskStateError("模拟规则文件暂时无法写入")

        with patch.object(app.task, "set_review_rules", side_effect=fail):
            app._review_rules_buttons["save"].invoke()
            self.wait_idle()
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], app.ui_thread_id)
        self.assertIsNotNone(app._review_rules_dialog)
        self.assertEqual(app._review_rules_editor.get("1.0", "end-1c"), draft)
        self.assertIn("编辑内容已保留", app._review_rules_status.get())
        self.assertEqual(str(app._review_rules_editor.cget("state")), "normal")
        app._close_review_rules_editor()

    def test_review_rules_editor_is_task_scoped_and_follows_day_night_theme(self):
        app = self.app
        app._select_stage("llm")
        self.assertTrue(app.edit_rules_button.winfo_manager())
        self.assertEqual(str(app.edit_rules_button.cget("state")), "disabled")
        app.edit_rules_button.invoke()
        self.assertIsNone(app._review_rules_dialog)
        self.complete_through("fetch-jd", confirm_last=True)
        app.edit_rules_button.invoke()
        for theme in ("dark", "light"):
            app._set_theme(theme, persist=False)
            self.assertEqual(app._review_rules_editor.cget("background"), ui.palette_for(theme)["surface"])
            self.assertEqual(app._review_rules_editor.cget("foreground"), ui.palette_for(theme)["text"])
        app._close_review_rules_editor()
        app._busy, app._operation = True, "execute"
        app._render(render_rows=False)
        self.assertEqual(str(app.edit_rules_button.cget("state")), "disabled")
        app._edit_review_rules()
        self.assertIsNone(app._review_rules_dialog)
        app._busy, app._operation = False, ""
        app._render(render_rows=False)

    def test_model_configuration_dialog_saves_provider_and_secret_separately(self):
        app = self.app
        app._select_stage("llm")
        app.model_config_button.invoke()
        dialog = app._model_config_dialog
        variables = app._model_config_vars
        variables["base_url"].set("https://custom-provider.example/v1")
        variables["model"].set("custom-semantic-model")
        variables["reasoning_effort"].set("high")
        variables["api_key"].set("fixture-api-key-never-write-to-file")

        app._save_model_configuration(
            dialog, variables["base_url"].get(), variables["model"].get(),
            variables["reasoning_effort"].get(), variables["api_key"].get(), app._model_config_status,
        )

        settings_path = self.base / "project-llm.json"
        settings = project_llm_settings.load_settings(settings_path)
        self.assertEqual(settings["base_url"], "https://custom-provider.example/v1")
        self.assertEqual(settings["model"], "custom-semantic-model")
        self.assertEqual(settings["reasoning_effort"], "high")
        persisted = settings_path.read_text(encoding="utf-8")
        self.assertNotIn("fixture-api-key-never-write-to-file", persisted)
        project_llm_settings.store_api_key.assert_called_once_with(
            "fixture-api-key-never-write-to-file", project_llm_settings.CREDENTIAL_TARGET,
        )
        self.assertIn("custom-semantic-model", app.model_config_label.cget("text"))
        self.assertIn("https://custom-provider.example/v1", app.model_config_label.cget("text"))
        self.assertIn("后续 AI 语义复核将使用新配置", app.notice.get())
        self.assertIsNone(dialog.tk, "closed model dialog must release its Tcl ownership on the UI thread")

        app.model_config_button.invoke()
        dialog = app._model_config_dialog
        variables = app._model_config_vars
        self.assertEqual(variables["api_key"].get(), "", "saved credentials must never be echoed into the form")
        app._save_model_configuration(
            dialog, variables["base_url"].get(), variables["model"].get(),
            variables["reasoning_effort"].get(), variables["api_key"].get(), app._model_config_status,
        )
        project_llm_settings.store_api_key.assert_called_once()
        self.assertNotIn("fixture-api-key-never-write-to-file",
                         settings_path.read_text(encoding="utf-8"))

    def test_model_dialog_reads_models_and_tests_connection_in_background(self):
        app = self.app
        app._select_stage("llm")
        app.model_config_button.invoke()
        variables = app._model_config_vars
        variables["base_url"].set("https://custom-provider.example/v1")
        variables["model"].set("manual-model")
        variables["reasoning_effort"].set("high")
        variables["api_key"].set("visible-only-test-key")
        fake_client = Mock()
        fake_client.list_models.return_value = ["provider-model-a", "provider-model-b"]
        calling_threads = []
        fake_client.list_models.side_effect = lambda: (calling_threads.append(threading.get_ident())
                                                       or ["provider-model-a", "provider-model-b"])

        with patch.object(ui, "CodexResponsesClient", return_value=fake_client) as client_factory:
            app._run_model_config_check(app._model_config_dialog, "models")
            self.assertTrue(app._model_config_request_busy)
            self.wait_model_check()
            self.assertEqual(tuple(app._model_config_model_box.cget("values")),
                             ("provider-model-a", "provider-model-b"))
            self.assertEqual(variables["model"].get(), "manual-model", "loading models must not change selection")
            self.assertIn("读取到 2 个可用模型", app._model_config_status.get())
            self.assertNotEqual(calling_threads[0], app.ui_thread_id, "network work must run off the Tk thread")
            config = client_factory.call_args.args[0]
            self.assertEqual(config["base_url"], "https://custom-provider.example/v1")
            self.assertEqual(config["api_key"], "visible-only-test-key")
            self.assertEqual(config["max_retries"], 0)
            self.assertEqual(config["api_mode"], "responses")

            app._run_model_config_check(app._model_config_dialog, "connection")
            self.wait_model_check()
            fake_client.test_connection.assert_called_once_with(model="manual-model", reasoning_effort="high")
            self.assertIn("连接成功", app._model_config_status.get())
            self.assertNotIn("visible-only-test-key", app._model_config_status.get())
            self.assertEqual(self.calls, [], "configuration checks must not run a review stage")

    def test_chat_protocol_selection_tests_and_saves_without_starting_review(self):
        app = self.app
        app._select_stage("llm")
        app.model_config_button.invoke()
        variables = app._model_config_vars
        self.assertEqual(tuple(app._model_config_api_mode_box.cget("values")),
                         ("Responses API", "Chat Completions"))
        self.assertEqual(variables["api_mode"].get(), "Responses API")
        variables["base_url"].set("https://chat-provider.example/v1")
        variables["model"].set("compatible-chat-model")
        variables["reasoning_effort"].set("high")
        variables["api_key"].set("fixture-chat-key")
        variables["api_mode"].set("Chat Completions")
        self.assertEqual(str(app._model_config_effort_box.cget("state")), "readonly")
        self.assertIn("未验证，不发送", app._model_config_mode_hint.get())
        self.assertIsNone(project_llm_settings.load_settings(), "editing must not save configuration")

        fake_client = Mock()
        fake_client.list_models.return_value = ["compatible-chat-model"]
        with patch.object(ui, "CodexResponsesClient", return_value=fake_client) as factory:
            app._run_model_config_check(app._model_config_dialog, "models")
            self.wait_model_check()
            self.assertEqual(factory.call_args.args[0]["api_mode"], "chat_completions")
            app._run_model_config_check(app._model_config_dialog, "connection")
            self.wait_model_check()
            self.assertEqual(factory.call_args.args[0]["api_mode"], "chat_completions")
            self.assertIn("Chat Completions", app._model_config_status.get())
            self.assertIn("未发送思考强度", app._model_config_status.get())
            self.assertNotIn("fixture-chat-key", app._model_config_status.get())

        app._model_config_buttons["save"].invoke()
        settings = project_llm_settings.load_settings()
        self.assertEqual(settings["api_mode"], "chat_completions")
        self.assertEqual(settings["reasoning_effort"], "high", "saved Responses effort must be preserved")
        self.assertIn("Chat Completions", app.model_config_label.cget("text"))
        self.assertIn("未验证，不发送", app.model_config_label.cget("text"))
        self.assertEqual(self.calls, [], "saving configuration must not start any stage")
        project_llm_settings.store_api_key.assert_called_once_with(
            "fixture-chat-key", project_llm_settings.CREDENTIAL_TARGET,
        )

        app.model_config_button.invoke()
        variables = app._model_config_vars
        self.assertEqual(variables["api_mode"].get(), "Chat Completions")
        self.assertEqual(variables["api_key"].get(), "")
        self.assertEqual(str(app._model_config_effort_box.cget("state")), "readonly")
        variables["api_mode"].set("Responses API")
        self.assertEqual(str(app._model_config_effort_box.cget("state")), "readonly")
        self.assertEqual(variables["reasoning_effort"].get(), "high")

    def open_chat_model_configuration(self):
        app = self.app
        app.model_config_button.invoke()
        values = app._model_config_vars
        values["base_url"].set("https://chat-provider.example/v1")
        values["model"].set("deepseek-v4.1-flash")
        values["reasoning_effort"].set("max")
        values["api_mode"].set("Chat Completions")
        values["chat_stream"].set(True)
        values["api_key"].set("fixture-verification-key")
        return app._model_config_dialog, values

    def test_verified_chat_strength_is_saved_and_reloaded_with_the_actual_wire_options(self):
        app = self.app
        dialog, variables = self.open_chat_model_configuration()
        threads = []
        client = Mock()
        client.test_chat_reasoning_effort.side_effect = lambda **_kw: threads.append(threading.get_ident()) or True
        with patch.object(ui, "CodexResponsesClient", return_value=client) as factory:
            app._model_config_buttons["reasoning"].invoke()
            self.wait_model_check()
            client.test_chat_reasoning_effort.assert_called_once_with(
                model="deepseek-v4.1-flash", reasoning_effort="max",
            )
            request = factory.call_args.args[0]
            self.assertTrue(request["chat_stream"])
            self.assertEqual(request["max_retries"], 0)
            self.assertGreaterEqual(request["timeout"], 60)
            self.assertNotEqual(threads[0], app.ui_thread_id)
            self.assertIn("验证成功", app._model_config_status.get())
            self.assertIn("思考强度已验证", app._model_config_mode_hint.get())
            self.assertIsNone(project_llm_settings.load_settings(), "a successful probe must not auto-save")
            app._run_model_config_check(dialog, "connection")
            self.wait_model_check()
            self.assertTrue(project_llm_settings.is_chat_reasoning_verified(
                request["base_url"], request["model"], request["reasoning_effort"],
                factory.call_args.args[0]["chat_reasoning_verified_for"], chat_stream=True,
            ))
            self.assertIn("思考强度 max", app._model_config_status.get())
        app._model_config_buttons["save"].invoke()
        saved = project_llm_settings.load_settings()
        self.assertTrue(saved["chat_stream"])
        self.assertTrue(project_llm_settings.is_chat_reasoning_verified(
            saved["base_url"], saved["model"], saved["reasoning_effort"],
            saved["chat_reasoning_verified_for"], chat_stream=True,
        ))
        self.assertIn("已验证，将发送", app.model_config_label.cget("text"))
        self.assertIsNone(dialog.tk)
        self.assertEqual(self.calls, [])
        persisted = (self.base / "project-llm.json").read_text(encoding="utf-8")
        self.assertNotIn("fixture-verification-key", persisted)
        app.model_config_button.invoke()
        self.assertIn("思考强度已验证", app._model_config_mode_hint.get())
        self.assertEqual(app._model_config_vars["api_key"].get(), "")

    def test_failed_or_false_verification_cannot_enable_the_chat_parameter(self):
        app = self.app
        dialog, _variables = self.open_chat_model_configuration()
        client = Mock()
        with patch.object(ui, "CodexResponsesClient", return_value=client):
            client.test_chat_reasoning_effort.side_effect = RuntimeError("fixture-verification-key timed out")
            app._run_model_config_check(dialog, "reasoning")
            self.wait_model_check()
            self.assertIn("验证失败，未启用", app._model_config_status.get())
            self.assertNotIn("fixture-verification-key", app._model_config_status.get())
            self.assertEqual(app._model_config_verified_for, "")
            client.test_chat_reasoning_effort.side_effect = None
            client.test_chat_reasoning_effort.return_value = False
            app._run_model_config_check(dialog, "reasoning")
            self.wait_model_check()
            self.assertEqual(app._model_config_verified_for, "")
        app._model_config_buttons["save"].invoke()
        self.assertEqual(project_llm_settings.load_settings()["chat_reasoning_verified_for"], "")

    def test_model_discovery_and_basic_connection_never_validate_chat_strength(self):
        app = self.app
        dialog, _variables = self.open_chat_model_configuration()
        client = Mock()
        client.list_models.return_value = ["deepseek-v4.1-flash"]
        with patch.object(ui, "CodexResponsesClient", return_value=client):
            for operation in ("models", "connection"):
                app._run_model_config_check(dialog, operation)
                self.wait_model_check()
                self.assertEqual(app._model_config_verified_for, "")
            client.test_chat_reasoning_effort.assert_not_called()
        app._model_config_buttons["save"].invoke()
        self.assertEqual(project_llm_settings.load_settings()["chat_reasoning_verified_for"], "")

    def test_changes_to_chat_form_invalidate_the_verified_parameter(self):
        app = self.app
        dialog, variables = self.open_chat_model_configuration()
        client = Mock()
        client.test_chat_reasoning_effort.return_value = True
        with patch.object(ui, "CodexResponsesClient", return_value=client):
            for field, changed in (
                ("base_url", "https://other-provider.example/v1"), ("model", "other-model"),
                ("reasoning_effort", "high"), ("chat_stream", False), ("api_key", "new-fixture-key"),
            ):
                with self.subTest(field=field):
                    app._run_model_config_check(dialog, "reasoning")
                    self.wait_model_check()
                    self.assertTrue(app._model_config_verified_for)
                    variables[field].set(changed)
                    self.assertEqual(app._model_config_verified_for, "")
                    self.assertIn("未验证，不发送", app._model_config_mode_hint.get())
        app._model_config_buttons["save"].invoke()
        self.assertEqual(project_llm_settings.load_settings()["chat_reasoning_verified_for"], "")

    def test_form_edit_during_verification_cannot_bless_the_changed_configuration(self):
        app = self.app
        dialog, variables = self.open_chat_model_configuration()
        release = threading.Event()
        self.release_events.append(release)
        client = Mock()
        client.test_chat_reasoning_effort.side_effect = lambda **_kw: release.wait(3) or True
        with patch.object(ui, "CodexResponsesClient", return_value=client):
            app._run_model_config_check(dialog, "reasoning")
            self.assertTrue(app._model_config_request_busy)
            variables["model"].set("changed-during-request")
            self.pump(.05)
            release.set()
            self.wait_model_check()
        self.assertEqual(app._model_config_verified_for, "")
        self.assertIn("表单已修改", app._model_config_status.get())
        app._model_config_buttons["save"].invoke()
        saved = project_llm_settings.load_settings()
        self.assertEqual(saved["model"], "changed-during-request")
        self.assertEqual(saved["chat_reasoning_verified_for"], "")

    def test_saved_verified_configuration_keeps_its_credential_target(self):
        marker = project_llm_settings.chat_reasoning_signature("https://chat-provider.example/v1", "test", "max")
        project_llm_settings.save_settings(
            "https://chat-provider.example/v1", "test", "max", api_mode="chat_completions",
            chat_reasoning_verified_for=marker, credential_target="custom-saved-credential",
        )
        app = self.app
        app.model_config_button.invoke()
        app._model_config_buttons["save"].invoke()
        saved = project_llm_settings.load_settings()
        self.assertEqual(saved["credential_target"], "custom-saved-credential")
        self.assertEqual(saved["chat_reasoning_verified_for"], marker)

    def test_verified_new_key_cannot_enable_the_old_credential_when_secret_storage_fails(self):
        app = self.app
        dialog, _variables = self.open_chat_model_configuration()
        client = Mock()
        client.test_chat_reasoning_effort.return_value = True
        with patch.object(ui, "CodexResponsesClient", return_value=client):
            app._run_model_config_check(dialog, "reasoning")
            self.wait_model_check()
        with patch.object(project_llm_settings, "store_api_key", side_effect=project_llm_settings.ProjectProviderError("storage failed")):
            app._model_config_buttons["save"].invoke()
        self.assertEqual(project_llm_settings.load_settings()["chat_reasoning_verified_for"], "")
        self.assertIn("API Key 未能保存", app._model_config_status.get())
        self.assertIsNotNone(dialog.tk)

    def test_chat_connection_error_cannot_display_raw_custom_api_key(self):
        app = self.app
        app.model_config_button.invoke()
        app._model_config_vars["base_url"].set("https://chat-provider.example/v1")
        app._model_config_vars["api_mode"].set("Chat Completions")
        app._model_config_vars["api_key"].set("opaque-fixture-key-without-known-prefix")
        client = Mock()
        client.test_connection.side_effect = RuntimeError("transport echoed opaque-fixture-key-without-known-prefix")
        with patch.object(ui, "CodexResponsesClient", return_value=client):
            app._run_model_config_check(app._model_config_dialog, "connection")
            self.wait_model_check()
        self.assertIn("连接测试失败", app._model_config_status.get())
        self.assertNotIn("opaque-fixture-key-without-known-prefix", app._model_config_status.get())

    def test_saved_project_key_is_read_only_after_an_explicit_provider_action(self):
        app = self.app
        app._select_stage("llm")
        with patch("project_llm_settings.load_api_key", return_value="stored-test-key") as load_key, \
             patch.object(ui, "CodexResponsesClient") as client_factory:
            app.model_config_button.invoke()
            variables = app._model_config_vars
            variables["model"].set("stored-model")
            variables["base_url"].set("https://custom-provider.example/v1")
            self.assertEqual(variables["api_key"].get(), "")
            load_key.assert_not_called()

            app._run_model_config_check(app._model_config_dialog, "connection")
            self.wait_model_check()
            load_key.assert_called_once_with(project_llm_settings.CREDENTIAL_TARGET)
            self.assertEqual(client_factory.call_args.args[0]["api_key"], "stored-test-key")
            self.assertNotIn("stored-test-key", app._model_config_status.get())

    def test_model_check_rejects_credentials_embedded_in_url_before_reading_key_or_network(self):
        app = self.app
        app._select_stage("llm")
        app.model_config_button.invoke()
        app._model_config_vars["base_url"].set("https://provider.example/v1?api_key=private-query-token")
        with patch("project_llm_settings.load_api_key") as load_key, \
             patch.object(ui, "CodexResponsesClient") as client_factory:
            app._run_model_config_check(app._model_config_dialog, "models")
            self.assertFalse(app._model_config_request_busy)
            load_key.assert_not_called()
            client_factory.assert_not_called()
            self.assertIn("不含凭据、查询参数或片段", app._model_config_status.get())
            self.assertNotIn("private-query-token", app._model_config_status.get())

    def test_editing_concurrency_remembers_both_values_without_running_a_stage(self):
        app = self.app
        app.jd_workers.set("4")
        app.workers.set("12")
        app._remember_execution_settings()
        app._set_theme("dark")
        for worker in app._preference_workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        saved = ui.save_preferences.call_args.args[0]
        self.assertEqual(saved["jd_workers"], "4")
        self.assertEqual(saved["workers"], "12")
        self.assertEqual(saved["batch_size"], "30")
        self.assertEqual(saved["theme"], "dark")
        self.assertEqual(self.calls, [])

    def test_creating_task_binds_manifest_and_import_still_requires_run(self):
        task = self.create_task()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.app.stage.get(), "import")
        self.assertEqual(self.app._stage_info()["status"], "pending")
        self.assertFalse(self.app._can_confirm())
        self.assertEqual(self.app.manifest["sample_size"], 50)
        self.assertEqual(self.app.manifest["city"], "上海")
        self.assertIn(str(task.path), self.app.binding_output.get())
        self.assertIn(str(self.source.resolve()), self.app.binding_source.get())
        self.assertTrue(self.app.binding_frame.winfo_manager())
        self.assertFalse(self.app.setup_frame.winfo_manager())
        self.assertTrue((task.path / "desktop.log").is_file())

    def test_new_task_defaults_to_unlimited_and_does_not_start_any_step(self):
        self.assertEqual(self.app.sample_mode.get(), ui.SAMPLE_CHOICES[0])
        self.assertEqual(self.app.sample_size.get(), "")
        self.assertIn("disabled", self.app.sample_entry.state())
        self.app._create_task()
        self.wait_idle()
        self.assertIsNone(self.app.manifest["sample_size"])
        self.assertIn("全部（不限）", self.app.binding_scope.get())
        self.assertEqual(self.app._stage_info()["status"], "pending")
        self.assertEqual(self.calls, [])
        self.browser.assert_not_called()

    def test_custom_quantity_accepts_any_positive_integer_and_rejects_bad_values(self):
        self.app.sample_mode_box.current(1)
        self.app.sample_mode_box.event_generate("<<ComboboxSelected>>")
        self.root.update()
        self.assertNotIn("disabled", self.app.sample_entry.state())
        for value in ("", " ", "0", "-1", "1.5", "bad"):
            self.app.sample_size.set(value)
            self.app._create_task()
            self.assertIsNone(self.app.task)
            self.assertFalse(self.parent.exists())
        self.assertEqual(self.errors.call_count, 6)
        self.app.sample_size.set("1200")
        self.app._create_task()
        self.wait_idle()
        self.assertEqual(self.app.manifest["sample_size"], 1200)
        self.assertIn("1200", self.app.binding_scope.get())
        self.assertEqual(self.calls, [])

    def test_unlimited_ignores_previous_custom_value_and_stays_disabled_after_theme_change(self):
        app = self.app
        app.sample_mode_box.current(1)
        app.sample_mode_box.event_generate("<<ComboboxSelected>>")
        app.sample_size.set("200")
        app.sample_mode_box.current(0)
        app.sample_mode_box.event_generate("<<ComboboxSelected>>")
        for theme in ("dark", "light"):
            app._set_theme(theme, persist=False)
            app._render()
            self.assertIn("disabled", app.sample_entry.state())
            self.assertEqual(str(app.sample_mode_box.cget("state")), "readonly")
        app._create_task()
        self.wait_idle()
        self.assertIsNone(app.manifest["sample_size"])

    def test_changing_quantity_creates_a_new_task_and_preserves_the_old_one(self):
        original = self.create_task()
        before = (original.path / "task_manifest.json").read_bytes()
        self.ask.return_value = False
        self.app._new_task()
        self.assertIs(self.app.task, original)
        self.assertEqual(self.app.sample_size.get(), "50")
        self.ask.return_value = True
        self.app.new_task_button.invoke()
        self.assertIsNone(self.app.task)
        self.assertEqual(self.app.sample_mode.get(), ui.SAMPLE_CHOICES[0])
        self.assertEqual(self.app.sample_size.get(), "")
        self.app.sample_mode.set(ui.SAMPLE_CHOICES[1])
        self.app.sample_size.set("150")
        self.app._create_task()
        self.wait_idle()
        self.assertNotEqual(self.app.task.path, original.path)
        self.assertEqual(self.app.manifest["sample_size"], 150)
        self.assertEqual((original.path / "task_manifest.json").read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_real_import_honors_unlimited_custom_and_more_than_available_without_services(self):
        write_json(self.source / "raw/current/shanghai/current__shanghai__aiarch__20_50__1_3.json",
                   {"city": "上海", "filters": {"salary": "20-50K", "experience": "1-3年"}, "jobs": self.jobs})
        write_json(self.source / "raw/target/shanghai/target__shanghai__aiarch__20_50__3_5.json",
                   {"city": "上海", "filters": {"salary": "20-50K", "experience": "3-5年"}, "jobs": [self.jobs[0]]})
        self.app.executor = ui.execute_stage
        paths = []
        with patch.object(review_pipeline, "_run_child", side_effect=AssertionError("local import must not request services")) as child:
            for size, expected in ((None, 50), (7, 7), (200, 50)):
                with self.subTest(size=size):
                    self.app.sample_mode.set(ui.SAMPLE_CHOICES[0] if size is None else ui.SAMPLE_CHOICES[1])
                    self.app.sample_size.set("" if size is None else str(size))
                    self.app._create_task()
                    self.wait_idle()
                    paths.append(self.app.task.path)
                    self.app._run_stage()
                    self.wait_idle()
                    self.assertEqual(self.app._stage_info()["status"], "completed", self.errors.call_args_list)
                    self.assertEqual(len(self.app._rows["import"]), expected)
                    self.assertEqual(self.app.manifest["sample_size"], size)
                    self.assertEqual(self.app._stage_info()["stats"]["deduplicated_job_count"], 50)
                    self.assertFalse(self.app.manifest["stages"]["shortlist"]["confirmed"])
                    self.app._new_task()
            child.assert_not_called()
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual((self.source / "raw/current/shanghai/jobs.json").read_bytes(), self.raw_before)
        self.browser.assert_not_called()

    def test_quantity_controls_fit_the_1024_setup_in_both_themes(self):
        self.root.geometry("1024x760")
        self.root.deiconify()
        for theme in ("dark", "light"):
            self.app._set_theme(theme, persist=False)
            self.root.update()
            for widget in (self.app.sample_mode_box, self.app.sample_entry, self.app.create_button,
                           self.app.choose_source_button, self.app.choose_parent_button):
                with self.subTest(theme=theme, widget=str(widget)):
                    self.assertTrue(widget.winfo_ismapped())
                    self.assertGreaterEqual(widget.winfo_rootx(), self.root.winfo_rootx())
                    self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                         self.root.winfo_rootx() + self.root.winfo_width())
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                         self.root.winfo_rooty() + self.root.winfo_height())

    def test_pending_input_preview_is_not_falsely_labelled_as_excluded(self):
        self.complete_through("import", confirm_last=True)
        self.assertEqual(self.app.stage.get(), "shortlist")
        self.assertEqual(self.app._stage_info()["status"], "pending")
        self.assertEqual(len(self.app._rows["shortlist"]), 50)
        self.assertEqual(self.app.results_table.set("job-000", "keep"), "—")
        self.assertEqual(self.app.filter_buttons["剔除"].cget("text"), "剔除 0")
        self.app.filter.set("剔除")
        self.pump()
        self.assertEqual(self.app._page_ids, [])

    def test_each_stage_stops_at_review_and_confirm_only_loads_next_step(self):
        self.create_task()
        for index, stage in enumerate(STAGES):
            self.assertEqual(self.app.stage.get(), stage)
            before = len(self.calls)
            self.app.run_button.invoke()
            self.wait_idle()
            self.assertEqual(len(self.calls), before + 1)
            self.assertEqual(self.app.stage.get(), stage)
            self.assertFalse(self.app._stage_info()["confirmed"])
            self.assertNotIn("disabled", self.app.confirm_button.state())
            self.app.confirm_button.invoke()
            self.wait_idle()
            self.assertEqual(len(self.calls), before + 1, "confirmation must not invoke any executor")
            self.assertEqual(self.app.stage.get(), STAGES[min(index + 1, 4)])
        self.assertEqual([call[0] for call in self.calls], list(STAGES))
        self.assertTrue(all(call[1] != self.app.ui_thread_id for call in self.calls))
        self.assertTrue(self.app.manifest["stages"]["report"]["confirmed"])
        self.assertIn("50", self.app.binding_scope.get())
        self.assertEqual(len(self.app._rows["report"]), 50)
        self.assertEqual(self.errors.call_count, 0)
        with patch.object(self.app, "_open_path") as opener:
            self.app.json_button.invoke()
            self.wait_idle()
            self.assertEqual(opener.call_args.args[0].name, "merged_jobs_index.json")
            self.app.markdown_button.invoke()
            self.wait_idle()
            self.assertEqual(opener.call_args.args[0].name, "海投分层清单.md")

    def test_report_page_open_button_waits_for_generation_but_not_final_confirmation(self):
        self.complete_through("llm")
        self.assertFalse(self.app.open_report_button.winfo_manager(), "report action belongs to the report page")
        self.app.confirm_button.invoke()
        self.wait_idle()
        self.assertEqual(self.app.stage.get(), "report")
        self.assertEqual(self.app.open_report_button.cget("text"), "打开报告")
        self.assertEqual(self.app.open_report_button.winfo_manager(), "grid")
        self.assertIn("disabled", self.app.open_report_button.state())
        with patch.object(self.app, "_open_path") as opener:
            self.app.open_report_button.invoke()
            opener.assert_not_called()
            self.app.run_button.invoke()
            self.wait_idle()
            opener.assert_not_called()
            self.assertFalse(self.app._stage_info()["confirmed"])
            self.assertNotIn("disabled", self.app.open_report_button.state())
            self.assertIn("打开报告", self.app.notice.get())
            before = len(self.calls)
            path = self.app._artifacts["markdown"]
            self.app.open_report_button.invoke()
            self.wait_idle()
            opener.assert_called_once_with(path)
            self.assertEqual(len(self.calls), before, "opening must not rerun processing")
            self.assertFalse(self.app._stage_info()["confirmed"], "opening must not confirm the report")
        self.assertEqual((self.source / "raw/current/shanghai/jobs.json").read_bytes(), self.raw_before)

    def test_report_open_button_rechecks_staleness_before_opening(self):
        self.complete_through("report")
        other = ReviewTask.open(self.app.task.path)
        other.set_keep("shortlist", ["job-001"], False)
        self.assertNotIn("disabled", self.app.open_report_button.state(), "UI has not refreshed yet")
        with patch.object(self.app, "_open_path") as opener:
            self.app.open_report_button.invoke()
            self.wait_idle()
            opener.assert_not_called()
        self.assertEqual(self.app._stage_info("report")["status"], "stale")
        self.assertIn("disabled", self.app.open_report_button.state())
        self.assertTrue(self.errors.called)

    def test_report_open_button_does_not_open_a_missing_report_file(self):
        self.complete_through("report")
        self.app._artifacts["markdown"].unlink()
        with patch.object(self.app, "_open_path") as opener:
            self.app.open_report_button.invoke()
            self.wait_idle()
            opener.assert_not_called()
        self.assertIn("disabled", self.app.open_report_button.state())
        self.assertTrue(self.errors.called)

    def test_opening_an_existing_task_exposes_its_report_without_regeneration(self):
        self.complete_through("report")
        path = self.app.task.path
        report = self.app._artifacts["markdown"]
        before = len(self.calls)
        root, app = self.make_window()
        app._open_task(path)
        self.wait_idle(app, root)
        self.assertEqual(app.stage.get(), "report")
        self.assertEqual(app.open_report_button.winfo_manager(), "grid")
        self.assertNotIn("disabled", app.open_report_button.state())
        with patch.object(app, "_open_path") as opener:
            app.open_report_button.invoke()
            self.wait_idle(app, root)
            opener.assert_called_once_with(report)
        self.assertEqual(len(self.calls), before)
        self.browser.assert_not_called()

    def test_creation_and_opening_slow_io_are_off_the_tk_thread(self):
        release = threading.Event()
        entered = threading.Event()
        self.release_events.append(release)
        real_create = ReviewTask.create
        ids = []

        def slow_create(*args, **kwargs):
            ids.append(threading.get_ident())
            entered.set()
            release.wait(4)
            return real_create(*args, **kwargs)

        with patch.object(ReviewTask, "create", side_effect=slow_create):
            before = time.monotonic()
            self.app._create_task()
            self.assertLess(time.monotonic() - before, .3)
            self.assertTrue(entered.wait(2))
            heartbeat = []
            self.root.after(1, lambda: heartbeat.append(True))
            self.pump(.06)
            self.assertEqual(heartbeat, [True])
            self.assertTrue(self.app._busy)
            release.set()
            self.wait_idle()
        self.assertNotEqual(ids[0], self.app.ui_thread_id)
        task_path = self.app.task.path
        real_open = ReviewTask.open
        release.clear()
        entered.clear()

        def slow_open(path):
            entered.set()
            release.wait(4)
            return real_open(path)

        with patch.object(ReviewTask, "open", side_effect=slow_open):
            self.app._open_task(task_path)
            self.assertTrue(entered.wait(2))
            heartbeat.clear()
            self.root.after(1, lambda: heartbeat.append(True))
            self.pump(.06)
            self.assertEqual(heartbeat, [True])
            release.set()
            self.wait_idle()
        self.assertEqual(self.calls, [])

    def test_slow_executor_keeps_tk_responsive_and_updates_from_durable_manifest(self):
        self.create_task()
        release = threading.Event()
        entered = threading.Event()
        self.release_events.append(release)

        def slow(task, stage, **options):
            self.calls.append((stage, threading.get_ident(), options))
            task.begin(stage)
            task.record_progress(stage, 3, 50)
            options["on_event"]("progress", {"done": 999, "total": 999})
            entered.set()
            release.wait(4)
            task.finish(stage, copy.deepcopy(self.jobs))
            return task.snapshot()

        self.app.executor = slow
        self.app._run_stage()
        self.assertTrue(entered.wait(2))
        heartbeat = []
        self.root.after(1, lambda: heartbeat.append(True))
        self.pump(.2)
        self.assertEqual(heartbeat, [True])
        self.assertIn("3 / 50", self.app.progress_text.get())
        self.assertNotIn("999", self.app.progress_text.get())
        for widget in (self.app.jd_workers_spin, self.app.workers_spin, self.app.batch_spin):
            self.assertIn("disabled", widget.state())
        self.app._set_theme("dark", persist=False)
        self.assertTrue(self.app.workflow_running)
        self.assertEqual(self.app.theme.get(), "dark")
        release.set()
        self.wait_idle()
        for widget in (self.app.jd_workers_spin, self.app.workers_spin, self.app.batch_spin):
            self.assertNotIn("disabled", widget.state())

    def test_jd_and_ai_pass_independent_concurrency_and_batch_size_to_the_executor(self):
        self.complete_through("shortlist", confirm_last=True)

        def strict_executor(task, stage, *, scraper_root, workers, jd_workers, batch_size, control, on_event):
            return self.fake_executor(task, stage, scraper_root=scraper_root, workers=workers,
                                      jd_workers=jd_workers, batch_size=batch_size, control=control, on_event=on_event)

        app = self.app
        app.executor = strict_executor
        app.jd_workers.set("7")
        app.workers.set("13")
        app.batch_size.set("30")
        app._run_stage()
        self.wait_idle()
        stage, thread_id, options = self.calls[-1]
        self.assertEqual(stage, "fetch-jd")
        self.assertNotEqual(thread_id, app.ui_thread_id)
        self.assertEqual((options["jd_workers"], options["workers"], options["batch_size"]), (7, 13, 30))
        calls = len(self.calls)
        app._confirm_stage()
        self.wait_idle()
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(app.stage.get(), "llm")
        app.workers.set("19")
        app._run_stage()
        self.wait_idle()
        self.assertEqual(self.calls[-1][0], "llm")
        options = self.calls[-1][2]
        self.assertEqual((options["jd_workers"], options["workers"], options["batch_size"]), (7, 19, 30))

    def test_jd_table_and_details_show_body_completeness_instead_of_keyword_rejection(self):
        self.complete_through("shortlist", confirm_last=True)

        def body_only(task, stage, **options):
            task.begin(stage)
            rows = task.input_jobs(stage)
            for index, job in enumerate(rows):
                state = ("complete", "partial", "missing")[index % 3]
                job.update(jd="与方案无关的开发正文。" if state != "missing" else "",
                           jd_completeness=state, jd_analysis_status="not_run", recommended_keep=True,
                           reason="旧职责关键词无命中", jd_dimension_count=0)
            task.finish(stage, rows)
            return task.snapshot()

        self.app.executor = body_only
        self.app._run_stage()
        self.wait_idle()
        rows = self.app._rows["fetch-jd"]
        self.assertEqual(len(self.app._stage_info()["keep_ids"]), len(rows))
        for index, expected in enumerate(("正文完整", "正文部分可用", "正文缺失")):
            job = rows[index]
            reason = self.app._row_reason(job)
            self.assertIn(expected, reason)
            self.assertIn("本步不筛选", reason)
            self.assertNotIn("建议剔除", reason)
            self.assertNotIn("关键词无命中", reason)
        # Old records may retain a prior suggestion; this page still reports
        # acquisition state rather than presenting it as a new JD decision.
        legacy = {**rows[0], "recommended_keep": False}
        self.assertNotIn("建议剔除", self.app._row_reason(legacy))
        self.app._focus_id = job_id(rows[0])
        self.app._show_detail()
        detail = self.app.detail_text.get("1.0", "end-1c")
        self.assertIn("本步仅补正文", detail)
        self.assertNotIn("六维证据", detail)
        self.assertNotIn("旧职责关键词无命中", detail)

    def test_real_jd_and_llm_stat_names_are_displayed_from_the_persisted_snapshot(self):
        self.complete_through("shortlist", confirm_last=True)

        def counters(task, stage, **options):
            task.begin(stage)
            rows = task.input_jobs(stage)
            stats = ({"jd_success_count": 7, "jd_partial_count": 3, "jd_failed_count": 2,
                      "jd_paused_count": 1, "jd_pending_count": len(rows) - 13}
                     if stage == "fetch-jd" else
                     {"review_format": "simple-v1", "llm_success_count": 8, "llm_fallback_count": 5,
                      "llm_failed_count": 2, "llm_pending_count": 1})
            task.record_progress(stage, 1, len(rows), stats=stats)
            options["on_event"]("progress", {"done": 999, "total": 999})
            task.finish(stage, rows, stats=stats)
            return task.snapshot()

        self.app.executor = counters
        self.app._run_stage()
        self.wait_idle()
        jd_stats = self.task_json()["stages"]["fetch-jd"]["stats"]
        for expected in ("JD 成功 7", "部分可用 3", "失败 2", "已暂停 1", f"待处理 {jd_stats['jd_pending_count']}"):
            self.assertIn(expected, self.app.progress_text.get())
        self.app._confirm_stage()
        self.wait_idle()
        self.app._run_stage()
        self.wait_idle()
        for expected in ("AI 成功 8", "未完成AI判断 5", "失败 2", "待处理 1"):
            self.assertIn(expected, self.app.progress_text.get())
        self.assertNotIn("999", self.app.progress_text.get())

    def test_checkbox_space_bulk_exclude_restore_and_json_are_consistent(self):
        self.complete_through("shortlist")
        app = self.app
        identity = app._page_ids[1]
        self.assertIn(identity, app._stage_info()["keep_ids"])
        app.results_table.focus(identity)
        app.results_table.selection_set(identity)
        app._table_space()
        self.wait_idle()
        self.assertNotIn(identity, self.task_json()["stages"]["shortlist"]["keep_ids"])
        self.assertEqual(app.results_table.set(identity, "keep"), "☐")
        app.restore_button.invoke()
        self.wait_idle()
        self.assertIn(identity, self.task_json()["stages"]["shortlist"]["keep_ids"])
        app.select_page_button.invoke()
        self.wait_idle()
        visible = set(app._page_ids)
        self.assertEqual(len(visible), ui.PAGE_SIZE)
        self.assertTrue(visible <= set(self.task_json()["stages"]["shortlist"]["keep_ids"]))
        app.exclude_button.invoke()
        self.wait_idle()
        self.assertTrue(visible <= set(self.task_json()["stages"]["shortlist"]["excluded_ids"]))
        app.restore_button.invoke()
        self.wait_idle()
        self.assertTrue(visible <= set(self.task_json()["stages"]["shortlist"]["keep_ids"]))
        self.assertEqual((self.source / "raw/current/shanghai/jobs.json").read_bytes(), self.raw_before)

    def test_click_in_keep_column_really_toggles_a_tk_row(self):
        self.complete_through("shortlist")
        self.root.geometry("1024x820")
        self.root.deiconify()
        self.root.update()
        identity = self.app._page_ids[1]
        before = identity in self.app._stage_info()["keep_ids"]
        x, y, width, height = self.app.results_table.bbox(identity, "keep")
        self.app.results_table.event_generate("<Button-1>", x=x + width // 2, y=y + height // 2)
        self.wait_idle()
        self.assertNotEqual(identity in self.app._stage_info()["keep_ids"], before)
        self.assertEqual(self.calls[-1][0], "shortlist")

    def test_search_pagination_keep_exclude_filters_and_full_sources(self):
        self.complete_through("shortlist")
        app = self.app
        app._change_page(1)
        self.assertEqual(app._page_ids[0], "job-020")
        app.search.set("公司 049")
        self.pump()
        self.assertEqual(app._page_ids, ["job-049"])
        app.search.set("")
        app.filter.set("剔除")
        self.pump()
        self.assertTrue(app._page_ids)
        self.assertTrue(all(identity in app._stage_info()["excluded_ids"] for identity in app._page_ids))
        app.filter.set("全部")
        app.search.set("稀有来源")
        self.pump()
        self.assertEqual(app._page_ids, ["job-000"])
        self.assertIn("稀有来源", app.sources_text.get("1.0", "end-1c"))
        self.assertIn("检索词0", app.sources_text.get("1.0", "end-1c"))
        self.assertEqual(len(self.calls), 2)

    def test_shortlist_queue_search_manual_filters_and_paging_are_independent(self):
        self.complete_through("shortlist")
        app = self.app
        groups = {key: set(self.queue_ids(key)) for key in ui.LIST_QUEUE_LABELS}
        self.assertEqual(set.union(*groups.values()), set(app._stage_info()["result_ids"]))
        self.assertEqual(sum(map(len, groups.values())), 50)
        self.assertTrue(all(not (groups[a] & groups[b]) for a, b in (("relevant", "verify"), ("relevant", "excluded"), ("verify", "excluded"))))
        for key, label in ui.LIST_QUEUE_LABELS.items():
            self.assertIn(f"{'队列剔除' if key == 'excluded' else label} {len(groups[key])}", app.progress_text.get())
        app.list_queue_filter.set("待核实")
        self.pump()
        self.assertEqual(len(app._filtered_jobs), 21)
        self.assertEqual(len(app._page_ids), ui.PAGE_SIZE)
        app._change_page(1)
        self.assertEqual(len(app._page_ids), 1)
        self.assertTrue(set(app._page_ids) <= groups["verify"])
        removed = sorted(groups["verify"])[0]
        app._set_keep([removed], False)
        self.wait_idle()
        app.filter.set("剔除")
        self.pump()
        self.assertEqual(app._page_ids, [removed])
        self.assertEqual(app._page, 0)
        app.search.set("公司 " + removed.rsplit("-", 1)[1])
        self.pump()
        self.assertEqual(app._page_ids, [removed])
        self.assertIn("待核实", app.results_table.set(removed, "reason"))
        content = app.detail_text.get("1.0", "end-1c")
        for text in ("人工名单：剔除", "列表方向队列：待核实", "工作对象待 JD 核实"):
            self.assertIn(text, content)
        app.list_queue_filter.set("明确相关")
        self.pump()
        self.assertEqual(app._page_ids, [])
        app.search.set("")
        app.filter.set("保留")
        app.list_queue_filter.set("剔除")
        self.pump()
        self.assertEqual(app._page_ids, [])
        restored = sorted(groups["excluded"])[0]
        app._set_keep([restored], True)
        self.wait_idle()
        self.assertEqual(app._page_ids, [restored], "manual restoration must not change the saved direction queue")
        self.assertIn("剔除", app.results_table.set(restored, "reason"))
        self.assertEqual(len(self.calls), 2)

    def test_relevant_only_requires_replacement_confirmation_and_uses_complete_results(self):
        self.complete_through("shortlist")
        app = self.app
        relevant, excluded = self.queue_ids("relevant"), self.queue_ids("excluded")
        app._set_keep([relevant[0]], False)
        self.wait_idle()
        app._set_keep([excluded[0]], True)
        self.wait_idle()
        app.list_queue_filter.set("待核实")
        app.filter.set("剔除")
        app.search.set("no visible job")
        self.pump()
        self.assertEqual(app._page_ids, [])
        snapshot = self.task_json()
        self.ask.return_value = False
        app.relevant_only_button.invoke()
        self.assertFalse(app._busy)
        self.assertEqual(self.task_json(), snapshot)
        prompt = self.ask.call_args.args[1]
        for text in ("完整结果（50 岗）", "覆盖", "人工保留 / 剔除", "分页", "不确认、不补抓"):
            self.assertIn(text, prompt)
        self.ask.return_value = True
        calls, write_threads = len(self.calls), []
        original = ReviewTask.set_keep

        def record_keep(task, *args, **kwargs):
            write_threads.append(threading.get_ident())
            return original(task, *args, **kwargs)

        with patch.object(ReviewTask, "set_keep", new=record_keep):
            app.relevant_only_button.invoke()
            self.wait_idle()
        info = self.task_json()["stages"]["shortlist"]
        self.assertEqual(info["keep_ids"], relevant)
        self.assertEqual(len(info["manual_choices"]), 50)
        self.assertTrue(all(value == (key in relevant) for key, value in info["manual_choices"].items()))
        self.assertFalse(info["confirmed"])
        self.assertEqual(app.stage.get(), "shortlist")
        self.assertEqual(app._stage_info("fetch-jd")["input_ids"], [])
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(len(write_threads), 1, "a queue replacement must use one atomic storage edit")
        self.assertTrue(all(value != app.ui_thread_id for value in write_threads))
        app._confirm_stage()
        self.wait_idle()
        self.assertEqual(app.stage.get(), "fetch-jd")
        self.assertEqual(app._stage_info()["input_ids"], relevant)
        self.assertEqual([job_id(job) for job in app.task.input_jobs("fetch-jd")], relevant)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual((self.source / "raw/current/shanghai/jobs.json").read_bytes(), self.raw_before)
        self.browser.assert_not_called()

    def test_relevant_only_preserves_out_of_scope_history_without_a_false_later_edit_warning(self):
        self.complete_through("shortlist")
        app = self.app
        historical = self.queue_ids("verify")[0]
        app._set_keep([historical], False)
        self.wait_idle()
        app.task.set_keep("import", [historical], False)
        app.task.confirm("import")
        app._refresh_task()
        self.wait_idle()
        app._run_stage()
        self.wait_idle()
        self.assertNotIn(historical, app._stage_info()["result_ids"])
        self.assertEqual(app._stage_info()["manual_choices"], {historical: False})

        relevant = self.queue_ids("relevant")
        app.relevant_only_button.invoke()
        self.wait_idle()
        info = app._stage_info()
        self.assertEqual(info["keep_ids"], relevant)
        self.assertEqual(info["manual_choices"], {
            historical: False,
            **{key: key in relevant for key in info["result_ids"]},
        })
        self.assertIn("已保存", app.notice.get())
        self.assertNotIn("保存后又被其他窗口修改", app.notice.get())
        self.assertEqual(app._notice_kind, "note")

        app.task.set_keep("import", [historical], True)
        app.task.confirm("import")
        app._refresh_task()
        self.wait_idle()
        app._run_stage()
        self.wait_idle()
        restored = app._stage_info()
        self.assertIn(historical, restored["recommended_keep_ids"])
        self.assertFalse(restored["manual_choices"][historical])
        self.assertNotIn(historical, restored["keep_ids"])
        self.errors.assert_not_called()
        self.assertEqual((self.source / "raw/current/shanghai/jobs.json").read_bytes(), self.raw_before)

    def test_add_verify_includes_complete_queue_and_preserves_other_manual_choices(self):
        self.complete_through("shortlist")
        app = self.app
        relevant, verify, excluded = (self.queue_ids(key) for key in ("relevant", "verify", "excluded"))
        app._set_keep([relevant[0], verify[0]], False)
        self.wait_idle()
        app._set_keep([excluded[0]], True)
        self.wait_idle()
        before = set(app._stage_info()["keep_ids"])
        app.list_queue_filter.set("明确相关")
        app.filter.set("剔除")
        app.search.set("公司 " + relevant[0].rsplit("-", 1)[1])
        self.pump()
        self.assertEqual(app._page_ids, [relevant[0]])
        calls = len(self.calls)
        app.add_verify_button.invoke()
        self.wait_idle()
        info = app._stage_info()
        self.assertEqual(set(info["keep_ids"]), before | set(verify))
        self.assertFalse(info["manual_choices"][relevant[0]])
        self.assertTrue(info["manual_choices"][excluded[0]])
        self.assertTrue(all(info["manual_choices"][identity] for identity in verify))
        self.assertFalse(info["confirmed"])
        self.assertIn("其他队列的人工取舍保持不变", self.ask.call_args.args[1])
        self.assertIn("不受当前搜索、筛选或分页影响", self.ask.call_args.args[1])
        self.assertEqual(app.stage.get(), "shortlist")
        self.assertEqual(len(self.calls), calls)
        expected = list(info["keep_ids"])
        app._confirm_stage()
        self.wait_idle()
        self.assertEqual(app._stage_info("fetch-jd")["input_ids"], expected)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual((self.source / "raw/current/shanghai/jobs.json").read_bytes(), self.raw_before)

    def test_queue_change_after_confirmation_invalidates_downstream_without_running(self):
        self.complete_through("report", confirm_last=True)
        app = self.app
        artifacts, calls = dict(app._artifacts), len(self.calls)
        app._select_stage("shortlist")
        expected = self.queue_ids("relevant")
        app.relevant_only_button.invoke()
        self.wait_idle()
        self.assertFalse(app._stage_info()["confirmed"])
        self.assertEqual(app._stage_info()["keep_ids"], expected)
        for stage in ("fetch-jd", "llm", "report"):
            self.assertEqual(app._stage_info(stage)["status"], "stale")
        self.assertIn("下游旧结果已失效", app.notice.get())
        self.assertTrue(artifacts["json"].is_file())
        self.assertEqual(len(self.calls), calls)
        app._confirm_stage()
        self.wait_idle()
        self.assertEqual(app._stage_info("fetch-jd")["input_ids"], expected)
        self.assertEqual(app._stage_info("fetch-jd")["status"], "stale")
        self.assertEqual(len(self.calls), calls)

    def test_queue_selection_rejects_a_changed_durable_snapshot_before_writing(self):
        self.complete_through("shortlist")
        app = self.app
        other = ReviewTask.open(app.task.path)
        removed = self.queue_ids("verify")[0]

        def confirm_after_external_edit(*_args, **_kwargs):
            other.set_keep("shortlist", [removed], False)
            return True

        self.ask.side_effect = confirm_after_external_edit
        app.relevant_only_button.invoke()
        self.wait_idle()
        info = app._stage_info()
        self.assertEqual(info["manual_choices"], {removed: False})
        self.assertEqual(set(info["keep_ids"]), set(info["recommended_keep_ids"]) - {removed})
        self.assertIn("列表结果或人工选择已变化", self.errors.call_args.args[1])
        self.assertEqual(len(self.calls), 2)

    def test_queue_selection_rechecks_external_edits_inside_the_single_storage_write(self):
        self.complete_through("shortlist")
        app = self.app
        other, original = ReviewTask.open(app.task.path), ReviewTask.set_keep
        for action, identity in ((app._keep_relevant_only, self.queue_ids("relevant")[0]),
                                 (app._add_verify_queue, self.queue_ids("verify")[0])):
            saved, writes = [], []

            def interleave_at_write(task, *args, **kwargs):
                original(other, "shortlist", [identity], False)
                saved.append(other.snapshot())
                writes.append(kwargs)
                return original(task, *args, **kwargs)

            with patch.object(ReviewTask, "set_keep", new=interleave_at_write):
                action()
                self.wait_idle()
            self.assertEqual(len(writes), 1)
            self.assertIsInstance(writes[0]["expected_stage"], dict)
            self.assertEqual(app.task.snapshot(), saved[0], "the preset must not partially override another window")
            self.assertIn("其他窗口修改", self.errors.call_args.args[1])
            self.assertEqual(app.stage.get(), "shortlist")
            self.assertFalse(app._stage_info()["confirmed"])
        self.assertEqual(len(self.calls), 2)

    def test_queue_selection_protects_new_manual_intent_even_without_a_stage_revision_change(self):
        self.complete_through("shortlist")
        app = self.app
        other, original = ReviewTask.open(app.task.path), ReviewTask.set_keep
        identity = self.queue_ids("verify")[0]
        old_revision = app._stage_info()["revision"]

        def interleave_same_keep(task, *args, **kwargs):
            original(other, "shortlist", [identity], True)
            return original(task, *args, **kwargs)

        with patch.object(ReviewTask, "set_keep", new=interleave_same_keep):
            app._keep_relevant_only()
            self.wait_idle()
        info = app._stage_info()
        self.assertEqual(info["revision"], old_revision)
        self.assertEqual(info["keep_ids"], info["recommended_keep_ids"])
        self.assertEqual(info["manual_choices"], {identity: True})
        self.assertIn("其他窗口修改", self.errors.call_args.args[1])

    def test_a_later_window_edit_is_preserved_and_not_reported_as_a_current_queue_preset(self):
        self.complete_through("shortlist")
        app = self.app
        other, original = ReviewTask.open(app.task.path), ReviewTask.set_keep
        relevant = self.queue_ids("relevant")
        writes = []

        def edit_after_commit(task, *args, **kwargs):
            result = original(task, *args, **kwargs)
            writes.append(kwargs)
            original(other, "shortlist", [relevant[0]], False)
            return result

        with patch.object(ReviewTask, "set_keep", new=edit_after_commit):
            app._keep_relevant_only()
            self.wait_idle()
        self.assertEqual(len(writes), 1)
        self.assertEqual(set(app._stage_info()["keep_ids"]), set(relevant[1:]))
        self.assertFalse(app._stage_info()["manual_choices"][relevant[0]])
        self.assertIn("保存后又被其他窗口修改", app.notice.get())
        self.assertEqual(app._notice_kind, "warning")
        self.assertFalse(app._stage_info()["confirmed"])
        self.errors.assert_not_called()

    def test_old_shortlist_uses_visible_compatibility_without_rewriting_or_new_policy(self):
        self.legacy_shortlist = True
        self.complete_through("shortlist")
        app = self.app
        result = app.task.path / app._stage_info()["result_file"]
        original, manifest, calls = result.read_bytes(), self.task_json(), len(self.calls)
        self.assertTrue(all("list_queue" not in job for job in app._rows["shortlist"]))
        with patch("process_architecture.score_job", side_effect=AssertionError("old tasks cannot be rescored by the UI")) as score:
            app.list_queue_filter.set("明确相关")
            self.pump()
            self.assertEqual(len(app._filtered_jobs), 21)
            self.assertIn("旧版兼容", app.results_table.set(app._page_ids[0], "reason"))
            self.assertIn(ui.LEGACY_LIST_QUEUE_NOTICE, app.detail_text.get("1.0", "end-1c"))
            self.assertIn(ui.LEGACY_LIST_QUEUE_NOTICE, app.list_queue_hint.get())
            for button in (app.relevant_only_button, app.add_verify_button):
                self.assertIn("disabled", button.state())
                button.invoke()
            app._keep_relevant_only()
            app._add_verify_queue()
            self.root.geometry("1024x760")
            self.root.deiconify()
            for theme in ("dark", "light"):
                app._set_theme(theme, persist=False)
                self.root.update()
                self.assertGreaterEqual(app.results_table.winfo_height(), 60)
                self.assertGreaterEqual(app.log_view.winfo_height(), 40)
            app._refresh_task()
            self.wait_idle()
            score.assert_not_called()
        self.assertEqual(app.list_queue_filter.get(), "明确相关")
        self.assertEqual(result.read_bytes(), original)
        self.assertEqual(self.task_json(), manifest)
        self.assertEqual(len(self.calls), calls)
        self.assertFalse(self.ask.called)

    def test_a_hidden_legacy_or_invalid_queue_disables_presets_for_the_complete_result(self):
        self.complete_through("shortlist")
        app = self.app
        other = ReviewTask.open(app.task.path)
        jobs = other.jobs("shortlist")
        jobs[0]["list_queue"] = "not-a-current-queue"
        other.begin("shortlist")
        other.finish("shortlist", jobs)
        app._refresh_task()
        self.wait_idle()
        app.list_queue_filter.set("明确相关")
        self.pump()
        self.assertTrue(app._page_ids)
        self.assertTrue(all(job["list_queue"] == "relevant" for job in app._filtered_jobs))
        self.assertIn(ui.LEGACY_LIST_QUEUE_NOTICE, app.list_queue_hint.get())
        saved = app.task.snapshot()
        for button in (app.relevant_only_button, app.add_verify_button):
            self.assertIn("disabled", button.state())
            button.invoke()
        self.assertNotIn("disabled", app.exclude_button.state(), "legacy compatibility must retain ordinary manual review")
        self.assertEqual(app.task.snapshot(), saved)
        self.assertFalse(self.ask.called)

    def test_queue_filter_survives_refresh_theme_and_navigation_but_new_results_reset_it(self):
        self.complete_through("shortlist")
        app = self.app
        app._set_keep([self.queue_ids("relevant")[0]], False)
        self.wait_idle()
        manual = dict(app._stage_info()["manual_choices"])
        app.list_queue_filter.set("待核实")
        app.search.set("工程师")
        self.pump()
        app._change_page(1)
        before = (tuple(app._page_ids), app._page, app.task.snapshot())
        for theme in ("dark", "light"):
            app._set_theme(theme, persist=False)
            self.assertEqual(app.list_queue_filter.get(), "待核实")
            self.assertEqual((tuple(app._page_ids), app._page, app.task.snapshot()), before)
        app._refresh_task()
        self.wait_idle()
        self.assertEqual(app.list_queue_filter.get(), "待核实")
        self.assertEqual((tuple(app._page_ids), app._page, app.task.snapshot()), before)
        app._select_stage("import")
        self.assertFalse(app.list_queue_frame.winfo_manager())
        self.assertEqual(len(app._filtered_jobs), 50, "shortlist queues must not filter another stage")
        app._select_stage("shortlist")
        self.assertEqual(app.list_queue_filter.get(), "待核实")
        self.assertEqual(len(app._filtered_jobs), 21)
        old_result = app._stage_info()["result_file"]
        app._run_stage()
        self.wait_idle()
        self.assertNotEqual(app._stage_info()["result_file"], old_result)
        self.assertEqual(app.list_queue_filter.get(), ui.ALL_LIST_QUEUES)
        self.assertEqual(app._page, 0)
        self.assertEqual(app._stage_info()["manual_choices"], manual)

    def test_opening_another_task_and_new_task_reset_queue_without_execution(self):
        self.complete_through("shortlist")
        app, old_path = self.app, self.app.task.path
        app.list_queue_filter.set("待核实")
        self.pump()
        app._new_task()
        self.assertEqual(app.list_queue_filter.get(), ui.ALL_LIST_QUEUES)
        self.complete_through("shortlist")
        self.assertNotEqual(app.task.path, old_path)
        app.list_queue_filter.set("明确相关")
        self.pump()
        calls = len(self.calls)
        app._open_task(old_path)
        self.wait_idle()
        self.assertEqual(app.task.path, old_path)
        self.assertEqual(app.list_queue_filter.get(), ui.ALL_LIST_QUEUES)
        self.assertEqual(len(self.calls), calls)

    def test_queue_actions_are_disabled_for_preview_running_unfinished_stale_and_empty(self):
        app = self.app
        app._select_stage("shortlist")
        for button in (app.relevant_only_button, app.add_verify_button):
            self.assertIn("disabled", button.state())
        self.complete_through("import", confirm_last=True)
        self.assertIn("shortlist", app._previews)
        self.assertIn("尚未运行列表初筛", app.detail_text.get("1.0", "end-1c"))
        self.assertNotIn("旧版兼容", app.detail_text.get("1.0", "end-1c"))
        app._keep_relevant_only()
        app._add_verify_queue()
        self.assertFalse(self.ask.called)
        app._run_stage()
        self.wait_idle()
        rows = app.task.jobs("shortlist")
        other = ReviewTask.open(app.task.path)
        self.addCleanup(other._release_run)
        for status in ("paused", "stopped", "waiting", "failed"):
            other.begin("shortlist")
            app._refresh_task()
            self.wait_idle()
            self.assertEqual(app._stage_info()["status"], "running")
            for button in (app.relevant_only_button, app.add_verify_button):
                self.assertIn("disabled", button.state())
                button.invoke()
            other.finish("shortlist", rows, status=status)
            app._refresh_task()
            self.wait_idle()
            self.assertEqual(app._stage_info()["status"], status)
            before = other.snapshot()
            app._keep_relevant_only()
            app._add_verify_queue()
            self.assertEqual(other.snapshot(), before)
            for button in (app.relevant_only_button, app.add_verify_button):
                self.assertIn("disabled", button.state())
        other.set_keep("import", ["job-000"], False)
        app._refresh_task()
        self.wait_idle()
        self.assertEqual(app._stage_info()["status"], "stale")
        app._keep_relevant_only()
        app._add_verify_queue()
        for button in (app.relevant_only_button, app.add_verify_button):
            self.assertIn("disabled", button.state())
        self.assertFalse(self.ask.called)
        app._select_stage("import")
        app._set_keep([job_id(job) for job in app._rows["import"]], False)
        self.wait_idle()
        app._confirm_stage()
        self.wait_idle()
        app._run_stage()
        self.wait_idle()
        self.assertEqual(app._stage_info()["status"], "completed")
        self.assertEqual(app._rows["shortlist"], [])
        for button in (app.relevant_only_button, app.add_verify_button):
            self.assertIn("disabled", button.state())

    def test_queue_actions_are_disabled_during_own_worker_or_collection_execution(self):
        self.complete_through("shortlist")
        app = self.app
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        original_executor = app.executor

        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original_executor(*args, **kwargs)

        app.executor = slow
        app._run_stage()
        self.assertTrue(entered.wait(2))
        for button in (app.relevant_only_button, app.add_verify_button):
            self.assertIn("disabled", button.state())
            button.invoke()
        app._keep_relevant_only()
        app._add_verify_queue()
        self.assertFalse(self.ask.called)
        release.set()
        self.wait_idle()
        with patch.object(type(app.collection_page), "busy", new_callable=unittest.mock.PropertyMock, return_value=True):
            app._render(render_rows=False)
            for button in (app.relevant_only_button, app.add_verify_button):
                self.assertIn("disabled", button.state())
            app._keep_relevant_only()
            app._add_verify_queue()
        self.assertFalse(self.ask.called)

    def test_manual_overrides_survive_rerun_until_explicit_adopt_recommendations(self):
        self.complete_through("shortlist")
        self.app._set_keep(["job-001"], False)
        self.wait_idle()
        self.app._run_stage()
        self.wait_idle()
        self.assertIn("job-001", self.app._stage_info()["excluded_ids"])
        self.app.recommend_button.invoke()
        self.wait_idle()
        self.assertIn("job-001", self.app._stage_info()["keep_ids"])
        self.assertIn("job-000", self.app._stage_info()["excluded_ids"])

    def test_upstream_edits_invalidate_downstream_and_protect_confirmation_and_reports(self):
        self.complete_through("report", confirm_last=True)
        paths = dict(self.app._artifacts)
        call_count = len(self.calls)
        self.app._select_stage("shortlist")
        self.app._set_keep(["job-001"], False)
        self.wait_idle()
        for stage in STAGES[2:]:
            self.assertEqual(self.app._stage_info(stage)["status"], "stale")
        self.assertIn("失效", self.app.notice.get())
        self.assertTrue(paths["json"].is_file(), "old outputs must remain inspectable on disk")
        self.app._select_stage("report")
        self.assertIn("disabled", self.app.confirm_button.state())
        self.assertIn("disabled", self.app.open_report_button.state())
        self.assertIn("disabled", self.app.json_button.state())
        self.assertIn("disabled", self.app.markdown_button.state())
        with patch.object(self.app, "_open_path") as opener:
            self.app._open_location("json")
            self.app._confirm_stage()
            self.app._run_stage()
            self.pump()
            opener.assert_not_called()
        self.assertEqual(len(self.calls), call_count)
        self.assertEqual((self.source / "raw/current/shanghai/jobs.json").read_bytes(), self.raw_before)

    def test_file_location_rechecks_manifest_instead_of_opening_a_stale_cached_report(self):
        self.complete_through("report", confirm_last=True)
        other = ReviewTask.open(self.app.task.path)
        other.set_keep("shortlist", ["job-001"], False)
        self.assertTrue(ui._report_ready(self.app.manifest), "UI has deliberately not refreshed yet")
        with patch.object(self.app, "_open_path") as opener:
            self.app._open_location("json")
            self.wait_idle()
            opener.assert_not_called()
        self.assertEqual(self.app._stage_info("report")["status"], "stale")
        self.assertIn("disabled", self.app.json_button.state())

    def test_final_report_table_and_counts_come_from_the_complete_report_json(self):
        self.complete_through("report")
        report = json.loads(self.app._artifacts["json"].read_text(encoding="utf-8"))
        by_id = {job_id(job): job for job in report}
        self.assertEqual(len(self.app._rows["report"]), len(by_id))
        for job in self.app._rows["report"]:
            self.assertEqual(job["classification"], by_id[job_id(job)]["classification"])
        counts = self.app._stage_info()["stats"]["classification_counts"]
        self.assertIn(f"全量追踪 {len(report)}", self.app.count_text.get())
        self.assertIn(f"暂不投 {counts['暂不投']}", self.app.count_text.get())
        # A malformed replacement is never treated as a new report view.
        write_json(self.app._artifacts["json"], report[:-1])
        self.app._refresh_task()
        self.wait_idle()
        self.assertIn("disabled", self.app.json_button.state())
        self.assertTrue(self.errors.called)

    def test_explicit_open_restores_fixed_scope_and_choices_without_running(self):
        self.complete_through("shortlist")
        self.app._set_keep(["job-001"], False)
        self.wait_idle()
        path = self.app.task.path
        calls = len(self.calls)
        root, app = self.make_window()
        self.assertIsNone(app.task)
        app._open_task(path)
        self.wait_idle(app, root)
        self.assertEqual(app.task.path, path)
        self.assertEqual(app.source_root.get(), str(self.source.resolve()))
        self.assertEqual(app.city.get(), "上海")
        self.assertEqual(app.sample_size.get(), "50")
        self.assertIn("job-001", app._stage_info("shortlist")["excluded_ids"])
        self.assertEqual(len(self.calls), calls)
        self.browser.assert_not_called()

    def test_suggestion_colors_are_independent_of_manual_keep_choices(self):
        def suggestions(task, stage, **options):
            if stage != "llm":
                return self.fake_executor(task, stage, **options)
            task.begin(stage)
            rows = task.input_jobs(stage)
            for index, job in enumerate(rows):
                decision = ("符合", "不符合", "信息不足")[index % 3]
                rows[index] = review_pipeline._analyze_ai_stage(job, {
                    "job_id": job_id(job), "status": "success", "review_format": ui.SIMPLE_REVIEW_FORMAT,
                    "判断": decision, "理由": "测试岗位语义判断。"})
            task.finish(stage, rows)
            return task.snapshot()

        self.app.executor = suggestions
        self.complete_through("llm")
        app = self.app
        # Exercise a missing/unknown suggestion in the view as well; the
        # task merger intentionally preserves non-null earlier-stage fields.
        app._rows["llm"][2]["recommended_keep"] = None
        app._render_rows()
        keep, exclude, unknown = [job_id(job) for job in app._rows["llm"][:3]]
        self.assertEqual(app.results_table.item(keep, "tags"), ("suggest_keep",))
        self.assertEqual(app.results_table.item(exclude, "tags"), ("suggest_exclude",))
        self.assertEqual(app.results_table.item(unknown, "tags"), ("pending_review",))
        self.assertEqual(app.results_table.set(unknown, "keep"), "—")
        self.assertIn("建议保留", app.results_table.set(keep, "reason"))
        self.assertIn("建议剔除", app.results_table.set(exclude, "reason"))
        self.assertIn("待人工核实", app.results_table.set(unknown, "reason"))
        app._set_keep([keep], False)
        self.wait_idle()
        app._set_keep([exclude], True)
        self.wait_idle()
        saved = self.task_json()
        for theme in ("light", "dark", "light"):
            with self.subTest(theme=theme):
                app._set_theme(theme, persist=False)
                app._render_rows()
                self.assertEqual(app.results_table.item(keep, "tags"), ("suggest_keep",))
                self.assertEqual(app.results_table.item(exclude, "tags"), ("suggest_exclude",))
                self.assertEqual(app.results_table.set(keep, "keep"), "☐")
                self.assertEqual(app.results_table.set(exclude, "keep"), "☑")
                self.assertEqual(self.task_json(), saved, "rendering must not modify task decisions")
        self.browser.assert_not_called()

    def test_ai_projection_rows_keep_related_show_pending_and_hide_old_code_verdicts(self):
        def model_projection(task, stage, **options):
            if stage != "llm":
                return self.fake_executor(task, stage, **options)
            task.begin(stage)
            rows = []
            for index, job in enumerate(task.input_jobs(stage)):
                job.update(classification="暂不投", reason="旧代码证据不足")
                assessment = ("符合", "符合", "不符合", "信息不足")[index % 4]
                rows.append(review_pipeline._analyze_ai_stage(job, {
                    "job_id": job_id(job), "status": "success", "review_format": "simple-v1",
                    "判断": assessment, "理由": "该岗位主要承担企业方案交付。"}))
            task.finish(stage, rows)
            return task.snapshot()

        self.app.executor = model_projection
        self.complete_through("llm")
        app = self.app
        ids = [job_id(job) for job in app._rows["llm"][:4]]
        for identity in ids[:2]:
            self.assertEqual(app.results_table.set(identity, "keep"), "☑")
            self.assertEqual(app.results_table.item(identity, "tags"), ("suggest_keep",))
            self.assertIn("建议保留", app.results_table.set(identity, "reason"))
            self.assertNotIn("旧代码", app.results_table.set(identity, "reason"))
        self.assertEqual(app.results_table.set(ids[2], "keep"), "☐")
        self.assertIn("建议剔除", app.results_table.set(ids[2], "reason"))
        self.assertEqual(app.results_table.set(ids[3], "keep"), "—")
        self.assertIn("待人工核实", app.results_table.set(ids[3], "reason"))
        app._focus_id = ids[0]
        app._show_detail()
        detail = app.detail_text.get("1.0", "end-1c")
        self.assertIn("AI判断：符合", detail)
        self.assertNotIn("已有分层：暂不投", detail)
        self.assertNotIn("旧代码证据不足", detail)
        self.browser.assert_not_called()

    def test_running_ai_checkpoint_updates_three_exclusive_lists_and_pending_filter(self):
        self.complete_through("fetch-jd", confirm_last=True)
        app, task = self.app, self.app.task
        task.begin("llm")
        jobs = task.input_jobs("llm")
        reviews = [
            {"job_id": job_id(jobs[0]), "status": "success", "review_format": ui.SIMPLE_REVIEW_FORMAT,
             "判断": "符合", "理由": "企业方案架构岗位。"},
            {"job_id": job_id(jobs[1]), "status": "success", "review_format": ui.SIMPLE_REVIEW_FORMAT,
             "判断": "不符合", "理由": "纯开发岗位。"},
            {"job_id": job_id(jobs[2]), "status": "success", "review_format": ui.SIMPLE_REVIEW_FORMAT,
             "判断": "信息不足", "理由": "JD信息不足。"},
            {"job_id": job_id(jobs[3]), "status": "failed", "review_format": ui.SIMPLE_REVIEW_FORMAT,
             "error": "Responses API 请求失败（HTTP 429）"},
        ]
        write_json(task.run_directory("llm") / "prepared" / "llm_reviews.json", {"reviews": reviews})
        try:
            view = ui._capture_task(task, cache=ui.TaskViewCache())
            info = view["manifest"]["stages"]["llm"]
            self.assertEqual(len(info["keep_ids"]), 1)
            self.assertEqual(len(info["excluded_ids"]), 1)
            self.assertEqual(len(info["pending_ids"]), len(jobs) - 2)
            self.assertEqual(set(info["keep_ids"]) | set(info["excluded_ids"]) | set(info["pending_ids"]),
                             {job_id(job) for job in jobs})
            self.assertFalse(set(info["keep_ids"]) & set(info["pending_ids"]))
            self.assertFalse(set(info["excluded_ids"]) & set(info["pending_ids"]))
            self.assertIn("HTTP 429", ui.ReviewDesktopApp._row_reason(
                SimpleNamespace(stage=SimpleNamespace(get=lambda: "llm"), _previews=set()), view["rows"]["llm"][3]))

            app.stage.set("llm")
            app._apply_view(task, view)
            app._render()
            self.assertEqual(app.filter_buttons["保留"].cget("text"), "保留 1")
            self.assertEqual(app.filter_buttons["剔除"].cget("text"), "剔除 1")
            self.assertEqual(app.filter_buttons["待核实"].cget("text"), f"待核实 {len(jobs) - 2}")
            app.filter.set("待核实")
            self.pump()
            self.assertEqual(len(app._filtered_jobs), len(jobs) - 2)
            self.assertTrue(all(job_id(job) in info["pending_ids"] for job in app._filtered_jobs))
            self.assertEqual(app.results_table.set(job_id(jobs[2]), "keep"), "—")
        finally:
            task.interrupt("llm", "stopped")

    def test_two_windows_confirmation_rejects_changed_list_then_explicitly_confirms_empty(self):
        self.complete_through("import")
        app = self.app
        other_root, other_app = self.make_window()
        other_app._open_task(app.task.path)
        self.wait_idle(other_app, other_root)
        other_app._set_keep(list(other_app._stage_info()["result_ids"]), False)
        self.wait_idle(other_app, other_root)
        self.assertEqual(len(app._stage_info()["keep_ids"]), 50)
        self.ask.reset_mock()
        app._confirm_stage()
        self.wait_idle()
        self.assertFalse(app._stage_info()["confirmed"])
        self.assertEqual(app.stage.get(), "import")
        self.assertEqual(app._stage_info()["keep_ids"], [])
        self.assertIn("其他窗口修改", self.errors.call_args.args[1])
        self.ask.assert_not_called()
        self.ask.return_value = False
        app._confirm_stage()
        self.assertFalse(app._busy)
        self.ask.assert_called_once()
        self.ask.return_value = True
        app._confirm_stage()
        self.wait_idle()
        self.assertTrue(app.manifest["stages"]["import"]["confirmed"])
        self.assertEqual(app._stage_info("shortlist")["input_ids"], [])
        self.assertEqual(len(self.calls), 1)

    def test_duplicate_views_coalesce_and_unchanged_page_has_no_tk_mutations(self):
        self.complete_through("llm")
        app = self.app
        view = app._capture_view(app.task)
        app._apply_view(app.task, view)
        app._render()
        app._busy, app._operation = True, "execute"
        self.addCleanup(setattr, app, "_busy", False)
        self.addCleanup(setattr, app, "_operation", "")
        for _ in range(12):
            app.events.put(("live-view", app._operation_id, (app.task, view)))
        app._cancel_after(app._poll_after_id)
        with patch.object(app, "_render", wraps=app._render) as renders, \
                patch.object(app.results_table, "insert", wraps=app.results_table.insert) as inserts, \
                patch.object(app.results_table, "delete", wraps=app.results_table.delete) as deletes, \
                patch.object(app.results_table, "item", wraps=app.results_table.item) as updates:
            app._poll_events()
            self.assertEqual(renders.call_count, 1)
            inserts.assert_not_called()
            deletes.assert_not_called()
            updates.assert_not_called()
        app._busy, app._operation = False, ""

    def test_serial_capture_sequence_rejects_older_live_and_newer_durable_edits(self):
        self.complete_through("llm")
        app = self.app
        older = app._capture_view(app.task)
        changed = app._stage_info()["keep_ids"][0]
        app.task.set_keep("llm", [changed], False)
        newer = app._capture_view(app.task)
        self.assertTrue(app._apply_view(app.task, newer))
        self.assertFalse(app._apply_view(app.task, older))
        self.assertNotIn(changed, app._stage_info()["keep_ids"])
        self.assertEqual(app._stage_info()["manual_choices"][changed], False)

    def test_large_running_ai_checkpoint_updates_keep_exclude_pending_with_tk_heartbeat(self):
        # A complete source fixture; no real browser, provider or credentials.
        self.jobs = [{**self.jobs[index % 50], "job_id": f"large-{index:04d}",
                      "title": f"实时方案岗位 {index:04d}"} for index in range(1200)]
        raw = self.source / "raw/current/shanghai/jobs.json"
        write_json(raw, self.jobs)
        self.raw_before = raw.read_bytes()
        self.complete_through("fetch-jd", confirm_last=True)
        app, task = self.app, self.app.task
        advance = threading.Event()
        self.release_events.append(advance)
        written = [0]
        reviews = []
        heartbeat = [0]

        def beat():
            heartbeat[0] += 1
            if not app._closed:
                app._after(10, beat)

        app._after(0, beat)

        def streaming(current_task, stage, **options):
            current_task.begin(stage)
            jobs = current_task.input_jobs(stage)
            path = current_task.run_directory(stage) / "prepared/llm_reviews.json"
            for index in range(6):
                if options["control"].requested:
                    break
                reviews.append({"job_id": job_id(jobs[index]), "status": "success", "review_format": "simple-v1",
                                "判断": ("符合", "不符合", "信息不足")[index % 3], "理由": f"持续落盘 {index}"})
                # Use the production atomic writer, allowing the UI to read
                # every durable generation while the executor remains alive.
                review_pipeline.processor.write_json(path, {"reviews": list(reviews)})
                written[0] = index + 1
                while not advance.wait(.01) and not options["control"].requested:
                    pass
                advance.clear()
            while not options["control"].requested:
                options["control"]._event.wait(.02)
            by_id = {review["job_id"]: review for review in reviews}
            rows = [review_pipeline._analyze_ai_stage(job, by_id.get(job_id(job))) for job in jobs]
            current_task.finish(stage, rows, status=options["control"].requested)

        app.executor = streaming
        app._select_stage("llm")
        app._run_stage()
        for count in range(1, 7):
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                self.root.update()
                info = app._stage_info()
                successes = info.get("stats", {}).get("llm_success_count", 0)
                if written[0] == count and successes == count and info.get("status") == "running":
                    break
                time.sleep(.005)
            self.assertEqual(successes, count, "each durable checkpoint must reach the live lists")
            self.assertTrue(app.worker.is_alive())
            self.assertEqual(len(info["keep_ids"]), (count + 2) // 3)
            self.assertEqual(len(info["excluded_ids"]), (count + 1) // 3)
            self.assertEqual(len(info["pending_ids"]), len(app._rows["llm"]) - (count + 2) // 3 - (count + 1) // 3)
            if count == 2:
                app.filter.set("待核实")
                self.pump(.13)
                app._change_page(1)
                selected = app._page_ids[:2]
                app.results_table.selection_set(selected)
                app.results_table.focus(selected[1])
                app._show_selected_job()
            if count == 3:
                self.assertEqual(app.results_table.selection(), tuple(selected))
                self.assertEqual(app._focus_id, selected[1])
                self.assertTrue(all(identity in app._stage_info()["pending_ids"] for identity in selected))
            if count == 4:
                app.search.set("实时方案岗位")
                self.pump(.13)
                self.assertEqual(app.search.get(), "实时方案岗位")
                self.assertTrue(app._filtered_jobs)
            advance.set()
        self.assertGreater(heartbeat[0], 10, "real Tk callbacks must run throughout checkpoint reads")
        app._pause_stage()
        advance.set()
        self.wait_idle(timeout=15)
        self.assertEqual(app._stage_info()["status"], "paused")
        self.assertFalse(app._stage_info()["confirmed"])
        self.assertEqual(app.filter_buttons["保留"].cget("text"), "保留 2")
        self.assertEqual(app.filter_buttons["剔除"].cget("text"), "剔除 2")
        self.assertIn("待核实", app.count_text.get())
        self.browser.assert_not_called()

    def test_running_ai_checkpoint_poll_updates_lists_without_progress_callbacks(self):
        """The durable checkpoint remains the live source when callbacks stop."""
        self.complete_through("fetch-jd", confirm_last=True)
        app, task = self.app, self.app.task
        release = threading.Event()
        self.release_events.append(release)

        def silent_executor(current_task, stage, **options):
            current_task.begin(stage)
            jobs = current_task.input_jobs(stage)
            reviews = []
            for index, job in enumerate(jobs[:2]):
                reviews.append({
                    "job_id": job_id(job), "status": "success",
                    "review_format": ui.SIMPLE_REVIEW_FORMAT,
                    "判断": "符合" if index == 0 else "不符合", "理由": "测试检查点",
                })
                write_json(current_task.run_directory(stage) / "prepared" / "llm_reviews.json",
                           {"reviews": reviews})
                current_task.record_progress(stage, index + 1, len(jobs),
                                             stats={"llm_success_count": index + 1,
                                                    "llm_pending_count": len(jobs) - index - 1})
                # Deliberately do not call options["on_event"]("progress", …).
                time.sleep(.55)
            release.wait(4)
            rows = [review_pipeline._analyze_ai_stage(
                job, next((review for review in reviews if review["job_id"] == job_id(job)), None))
                    for job in jobs]
            current_task.finish(stage, rows, status="stopped")

        app.executor = silent_executor
        app._select_stage("llm")
        app._run_stage()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            if app.filter_buttons["保留"].cget("text") == "保留 1" and app.filter_buttons["剔除"].cget("text") == "剔除 1":
                break
            time.sleep(.02)
        self.assertEqual(app.filter_buttons["保留"].cget("text"), "保留 1")
        self.assertEqual(app.filter_buttons["剔除"].cget("text"), "剔除 1")
        self.assertEqual(app.filter_buttons["待核实"].cget("text"), f"待核实 {len(app._rows['llm']) - 2}")
        release.set()
        self.wait_idle()

    def test_idle_window_observes_another_window_running_ai_without_enabling_edits(self):
        self.complete_through("fetch-jd", confirm_last=True)
        owner = self.app
        owner.task.begin("llm")
        jobs = owner.task.input_jobs("llm")
        path = owner.task.run_directory("llm") / "prepared/llm_reviews.json"
        write_json(path, {"reviews": [{"job_id": job_id(jobs[0]), "status": "success",
                                        "review_format": ui.SIMPLE_REVIEW_FORMAT,
                                        "判断": "符合", "理由": "外部窗口已落盘"}]})
        observer_root, observer = self.make_window()
        observer._open_task(owner.task.path)
        self.wait_idle(observer, observer_root)
        observer.stage.set("llm")
        observer._schedule_live_refresh()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observer_root.update()
            if observer._stage_info("llm").get("keep_ids") == [job_id(jobs[0])]:
                break
            time.sleep(.01)
        self.assertEqual(observer._stage_info("llm")["status"], "running")
        self.assertEqual(observer._stage_info("llm")["keep_ids"], [job_id(jobs[0])])
        self.assertFalse(observer._can_edit())
        self.assertFalse(observer._can_confirm())
        self.assertFalse(observer.workflow_running)
        owner.task.interrupt("llm", "stopped")

    def test_keep_suggestion_preserves_original_colors_and_only_exclude_is_red(self):
        def luminance(color):
            rgb = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
                      for value in rgb]
            return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))

        for theme in ("light", "dark"):
            self.app._set_theme(theme, persist=False)
            palette = ui.palette_for(theme)
            for tag, foreground, background in (("suggest_keep", "text", "surface"),
                                                ("suggest_exclude", "red", "table_suggest_exclude")):
                with self.subTest(theme=theme, tag=tag):
                    actual_fg = str(self.app.results_table.tag_configure(tag, "foreground"))
                    actual_bg = str(self.app.results_table.tag_configure(tag, "background"))
                    self.assertEqual(actual_fg, palette[foreground])
                    self.assertEqual(actual_bg, palette[background])
                    dark, light = sorted((luminance(actual_fg), luminance(actual_bg)))
                    self.assertGreaterEqual((light + .05) / (dark + .05), 4.5)
            for option in ("foreground", "background"):
                self.assertEqual(str(self.app.results_table.tag_configure("suggest_keep", option)),
                                 str(self.app.results_table.tag_configure("kept", option)))
            self.assertNotEqual(palette["surface"], palette["table_suggest_exclude"])
            self.assertNotEqual(palette["text"], palette["red"])

    def test_shortlist_uses_saved_list_pass_and_stale_overrides_suggestion_colors(self):
        self.complete_through("shortlist")
        app = self.app
        for job in app._rows["shortlist"][:3]:
            expected = "suggest_keep" if job["list_pass"] else "suggest_exclude"
            self.assertNotIn("recommended_keep", job)
            self.assertEqual(app.results_table.item(job_id(job), "tags"), (expected,))
        saved = self.task_json()
        app.manifest["stages"]["shortlist"]["status"] = "stale"
        app._render_rows()
        for identity in app._page_ids:
            self.assertEqual(app.results_table.item(identity, "tags"), ("stale",))
        self.assertEqual(self.task_json(), saved)

    def test_preview_import_jd_and_report_do_not_inherit_suggestion_colors(self):
        self.complete_through("report")
        app = self.app
        for stage in ("import", "fetch-jd", "report", "llm"):
            with self.subTest(stage=stage):
                app._select_stage(stage)
                if stage == "llm":
                    app._previews.add(stage)
                rows = app._rows[stage]
                rows[0]["recommended_keep"] = False
                rows[1]["recommended_keep"] = True
                rows[0]["classification"] = "海投池"
                rows[1]["classification"] = "暂不投"
                app._render_rows()
                for job in rows[:2]:
                    expected = "kept" if app._kept(job) or stage == "llm" else "excluded"
                    self.assertEqual(app.results_table.item(job_id(job), "tags"), (expected,))
                if stage == "llm":
                    self.assertEqual(app.results_table.set(job_id(rows[0]), "keep"), "—")
        self.browser.assert_not_called()

    def test_theme_switch_preserves_widgets_filters_checks_focus_details_logs_and_manifest(self):
        self.complete_through("shortlist")
        app = self.app
        app.search.set("工程师")
        self.pump()
        app._change_page(1)
        identity = app._page_ids[2]
        app.results_table.focus(identity)
        app.results_table.selection_set(identity)
        self.root.update()
        app.log_view.tag_add("sel", "1.0", "1.3")
        app.detail_text.tag_add("sel", "1.0", "1.3")
        app.jd_workers.set("6")
        app.workers.set("17")
        app.detail_notebook.select(app.sources_text.master)
        widgets = []
        pending = [self.root]
        while pending:
            widget = pending.pop()
            widgets.append(str(widget))
            pending.extend(widget.winfo_children())
        before = dict(rows=app.results_table.get_children(), selection=app.results_table.selection(), page=app._page,
                      focus=app._focus_id, manifest=app.task.snapshot(), log=app.log_view.get("1.0", "end-1c"),
                      detail=app.detail_text.get("1.0", "end-1c"), log_selection=app.log_view.tag_ranges("sel"),
                      detail_selection=app.detail_text.tag_ranges("sel"),
                      tab=app.detail_notebook.select())
        for theme in ("dark", "light", "dark"):
            app._set_theme(theme, persist=False)
            self.assertEqual(app.theme.get(), theme)
            self.assertEqual(app.results_table.get_children(), before["rows"])
            self.assertEqual(app.results_table.selection(), before["selection"])
            self.assertEqual(app._focus_id, before["focus"])
            self.assertEqual(app._page, before["page"])
            self.assertEqual(app.search.get(), "工程师")
            self.assertEqual(app.task.snapshot(), before["manifest"])
            self.assertEqual(app.log_view.get("1.0", "end-1c"), before["log"])
            self.assertEqual(app.detail_text.get("1.0", "end-1c"), before["detail"])
            self.assertEqual(app.log_view.tag_ranges("sel"), before["log_selection"])
            self.assertEqual(app.detail_text.tag_ranges("sel"), before["detail_selection"])
            self.assertEqual(app.detail_notebook.select(), before["tab"])
            self.assertEqual((app.jd_workers.get(), app.workers.get()), ("6", "17"))
            self.assertEqual(app.detail_text.cget("background"), ui.palette_for(theme)["surface"])
            self.assertEqual(str(app.log_view.cget("state")), "disabled")
        pending, current = [self.root], []
        while pending:
            widget = pending.pop()
            current.append(str(widget))
            pending.extend(widget.winfo_children())
        self.assertEqual(current, widgets)

    def test_pause_stop_and_resume_are_stage_local_and_never_confirm(self):
        self.create_task()
        entered = threading.Event()
        attempts = []

        def controlled(task, stage, **options):
            attempts.append(stage)
            task.begin(stage)
            entered.set()
            deadline = time.monotonic() + 4
            control = options["control"]
            while not control.requested and time.monotonic() < deadline:
                time.sleep(.005)
            task.interrupt(stage, status=control.requested or "failed", issue="用户控制测试")
            return task.snapshot()

        self.app.executor = controlled
        for request, status in ((self.app._pause_stage, "paused"), (self.app._stop_stage, "stopped")):
            entered.clear()
            self.app._run_stage()
            self.assertTrue(entered.wait(2))
            request()
            self.wait_idle()
            self.assertEqual(self.app.stage.get(), "import")
            self.assertEqual(self.app._stage_info()["status"], status)
            self.assertFalse(self.app._stage_info()["confirmed"])
            self.assertIn("disabled", self.app.confirm_button.state())
        self.app.executor = self.fake_executor
        self.app._run_stage()
        self.wait_idle()
        self.assertEqual(attempts, ["import", "import"])
        self.assertEqual(self.app._stage_info()["status"], "completed")
        self.assertFalse(self.app._stage_info()["confirmed"])

    def test_waiting_for_human_continue_reruns_only_current_stage(self):
        self.complete_through("shortlist", confirm_last=True)
        stages = []

        def gate(task, stage, **options):
            stages.append(stage)
            if len(stages) == 1:
                task.begin(stage)
                task.interrupt(stage, status="waiting", issue="请在专用浏览器完成人工验证")
                options["on_event"]("waiting", "安全验证暂停")
                return task.snapshot()
            return self.fake_executor(task, stage, **options)

        self.app.executor = gate
        self.app._run_stage()
        self.wait_idle()
        self.assertEqual(self.app._stage_info()["status"], "waiting")
        self.assertNotIn("disabled", self.app.continue_button.state())
        self.app.continue_button.invoke()
        self.wait_idle()
        self.assertEqual(stages, ["fetch-jd", "fetch-jd"])
        self.assertEqual(self.app.stage.get(), "fetch-jd")
        self.assertFalse(self.app._stage_info()["confirmed"])
        self.browser.assert_not_called()

    def test_zero_selection_requires_explicit_prompt_and_remains_a_valid_chain(self):
        self.complete_through("import")
        self.app._set_keep([job_id(job) for job in self.jobs], False)
        self.wait_idle()
        self.ask.return_value = False
        self.app._confirm_stage()
        self.assertFalse(self.app._busy)
        self.assertFalse(self.app._stage_info()["confirmed"])
        self.assertIn("不会发起 JD 或模型请求", self.ask.call_args.args[1])
        self.ask.return_value = True
        count = len(self.calls)
        self.app._confirm_stage()
        self.wait_idle()
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.app.stage.get(), "shortlist")
        for stage in STAGES[1:]:
            self.assertEqual(self.app.stage.get(), stage)
            self.app._run_stage()
            self.wait_idle()
            self.assertEqual(self.app._stage_info()["input_ids"], [])
            self.app._confirm_stage()
            self.wait_idle()
        self.assertEqual(len(self.app._rows["report"]), 50)
        self.assertTrue(all(job["excluded_at"] == "import" for job in self.app._rows["report"]))

    def test_browser_and_collector_only_run_after_explicit_actions(self):
        self.create_task()
        self.app._select_stage("import")
        self.browser.assert_not_called()
        self.collector.assert_not_called()
        self.app.scraper_root.set(str(self.base / "fake-tool"))
        self.app._prepare_browser()
        self.wait_idle()
        self.browser.assert_called_once()
        self.assertIn("control", self.browser.call_args.kwargs)
        self.assertEqual(self.app._stage_info()["status"], "pending")
        self.assertEqual(self.calls, [])
        self.app.collector_button.invoke()
        self.collector.assert_called_once_with()
        with patch.object(ui.subprocess, "Popen") as launch:
            self.app.collector_opener = None
            self.app._open_collector()
            self.assertEqual(self.app.workspace.get(), "collection")
            self.assertTrue(self.app.collection_page.winfo_manager())
            self.assertFalse(self.app.collection_page.busy)
            launch.assert_not_called()

    def test_browser_preparer_old_two_parameter_signature_is_supported(self):
        self.create_task()
        calls = []

        def legacy(scraper_root, on_event=None):
            calls.append(threading.get_ident())
            on_event("log", "仅准备浏览器的伪日志")
            return True

        self.app.browser_preparer = legacy
        self.app.scraper_root.set(str(self.base / "fake-tool"))
        self.app._prepare_browser()
        self.wait_idle()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0], self.app.ui_thread_id)
        self.assertIn("伪日志", self.app.log_view.get("1.0", "end-1c"))

    def test_preparing_browser_can_be_stopped_on_close_without_freezing_tk(self):
        self.create_task()
        entered = threading.Event()
        controls = []

        def preparer(scraper_root, on_event=None, *, control=None):
            controls.append(control)
            entered.set()
            while not control.requested:
                time.sleep(.005)
            return False

        self.app.browser_preparer = preparer
        self.app.scraper_root.set(str(self.base / "fake-tool"))
        self.app._prepare_browser()
        self.assertTrue(entered.wait(2))
        self.assertNotIn("disabled", self.app.stop_button.state())
        self.app._close()
        deadline = time.monotonic() + 4
        while not self.app._closed and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)
        self.assertEqual(controls[0].requested, "stopped")
        self.assertTrue(self.app._closed)
        self.assertFalse(self.app.worker.is_alive())
        self.assertEqual(self.app._after_ids, set())

    def test_invalid_execution_settings_do_not_call_the_executor(self):
        self.create_task()
        for variable in (self.app.workers, self.app.jd_workers):
            for value in ("0", "-1", "33", "bad"):
                variable.set(value)
                self.app._run_stage()
            variable.set("10")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.errors.call_count, 8)
        for jd_workers, workers in ((1, 32), (32, 1)):
            self.app.jd_workers.set(str(jd_workers))
            self.app.workers.set(str(workers))
            self.app._run_stage()
            self.wait_idle()
            self.assertEqual(self.calls[-1][2]["workers"], workers)
            self.assertEqual(self.calls[-1][2]["jd_workers"], jd_workers)
            self.assertEqual(self.calls[-1][2]["batch_size"], 30)

    def test_real_local_executor_empty_downstream_chain_matches_report_without_services(self):
        # Actual importer intentionally recognizes complete search-combination
        # filenames rather than arbitrary JSON files in raw/.
        write_json(self.source / "raw/current/shanghai/current__shanghai__aiarch__20_50__1_3.json",
                   {"city": "上海", "filters": {"salary": "20-50K", "experience": "1-3年"}, "jobs": self.jobs})
        write_json(self.source / "raw/target/shanghai/target__shanghai__aiarch__20_50__3_5.json",
                   {"city": "上海", "filters": {"salary": "20-50K", "experience": "3-5年"}, "jobs": [self.jobs[0]]})
        self.create_task()
        self.app.executor = ui.execute_stage
        with patch.object(review_pipeline, "_run_child", side_effect=AssertionError("no external process is allowed")) as child:
            self.app._run_stage()
            self.wait_idle()
            self.assertEqual(self.app._stage_info()["status"], "completed", self.errors.call_args_list)
            imported = len(self.app._stage_info()["keep_ids"])
            self.assertEqual(imported, 50)
            self.app._confirm_stage()
            self.wait_idle()
            self.app._run_stage()
            self.wait_idle()
            self.assertEqual(self.app.stage.get(), "shortlist")
            self.assertEqual(self.app._stage_info()["status"], "completed", self.errors.call_args_list)
            self.app._set_keep([job_id(job) for job in self.app._rows["shortlist"]], False)
            self.wait_idle()
            self.app._confirm_stage()
            self.wait_idle()
            for stage in ("fetch-jd", "llm", "report"):
                self.assertEqual(self.app.stage.get(), stage)
                self.app._run_stage()
                self.wait_idle()
                self.assertEqual(self.app._stage_info()["status"], "completed", self.errors.call_args_list)
                self.app._confirm_stage()
                self.wait_idle()
            child.assert_not_called()
        report = json.loads(self.app._artifacts["json"].read_text(encoding="utf-8"))
        self.assertEqual(len(report["jobs"]), imported)
        self.assertTrue(all(job["classification"] == "暂不投" for job in report["jobs"]))
        self.assertTrue(all(job["excluded_at"] == "shortlist" for job in self.app._rows["report"]))
        self.assertEqual(self.app._stage_info()["stats"]["classification_counts"]["暂不投"], imported)
        self.assertIn(f"暂不投 {imported}", self.app.count_text.get())
        self.assertEqual((self.source / "raw/current/shanghai/jobs.json").read_bytes(), self.raw_before)
        self.browser.assert_not_called()

    def test_log_and_detail_redaction_and_read_only_scrollable_widgets(self):
        self.complete_through("import")
        self.app._append_log("Cookie=PRIVATE_COOKIE\napi_key=PRIVATE_KEY\n")
        content = self.app.log_view.get("1.0", "end-1c")
        self.assertIn("[REDACTED]", content)
        self.assertNotIn("PRIVATE_COOKIE", content)
        self.assertNotIn("PRIVATE_KEY", content)
        for widget in (self.app.detail_text, self.app.jd_text, self.app.sources_text, self.app.log_view):
            self.assertEqual(str(widget.cget("state")), "disabled")
            self.assertTrue(widget.cget("yscrollcommand"))
        self.assertIn("真实伪执行器", (self.app.task.path / "desktop.log").read_text(encoding="utf-8"))

    def test_report_artifact_paths_cannot_escape_the_current_task(self):
        self.complete_through("report")
        task = self.app.task
        outside = self.base / "other-task.json"
        write_json(outside, [{"do_not_open": True}])
        manifest = task.snapshot()
        manifest["stages"]["report"]["stats"]["json_path"] = str(outside)
        paths = ui._artifact_paths(task, manifest)
        self.assertNotIn("json", paths)
        self.assertIn("markdown", paths)

    def test_artifact_file_reparse_points_are_rejected_even_when_inside_the_task(self):
        self.complete_through("report")
        path = self.app._artifacts["json"]
        actual_lstat = Path.lstat

        def reparse(candidate, *args, **kwargs):
            if candidate == path:
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_file_attributes=0x400)
            return actual_lstat(candidate, *args, **kwargs)

        with patch.object(Path, "lstat", autospec=True, side_effect=reparse):
            self.assertIsNone(ui._confined_file(self.app.task, path))

    def test_1024_window_keeps_essential_actions_table_details_and_log_reachable(self):
        self.complete_through("report")
        self.root.geometry("1024x760")
        self.root.deiconify()
        for theme in ("dark", "light"):
            self.app._set_theme(theme, persist=False)
            for stage in ("shortlist", "fetch-jd", "llm", "report"):
                self.app._select_stage(stage)
                self.root.update()
                widgets = [self.app.theme_button, self.app.run_button, self.app.reset_button, self.app.pause_button,
                           self.app.stop_button, self.app.continue_button, self.app.confirm_button,
                           self.app.select_page_button, self.app.exclude_button, self.app.restore_button,
                           self.app.recommend_button, self.app.search_entry, self.app.results_table,
                           self.app.detail_notebook, self.app.log_view,
                           self.app.description_label]
                if stage == "shortlist":
                    widgets.extend((self.app.list_queue_box, self.app.relevant_only_button,
                                    self.app.add_verify_button, self.app.list_queue_hint_label))
                elif stage == "fetch-jd":
                    widgets.extend((self.app.jd_workers_spin, self.app.jd_mode_label,
                                    self.app.browser_mode_box, self.app.prepare_browser_button,
                                    self.app.chrome_status_label, self.app.edge_status_label))
                elif stage == "llm":
                    widgets.extend((self.app.workers_spin, self.app.model_config_label, self.app.edit_rules_button))
                elif stage == "report":
                    widgets.append(self.app.open_report_button)
                for widget in widgets:
                    x = widget.winfo_rootx() - self.root.winfo_rootx()
                    y = widget.winfo_rooty() - self.root.winfo_rooty()
                    with self.subTest(theme=theme, stage=stage, widget=str(widget)):
                        self.assertTrue(widget.winfo_ismapped())
                        self.assertGreaterEqual(x, 0)
                        self.assertGreaterEqual(y, 0)
                        self.assertLessEqual(x + widget.winfo_width(), self.root.winfo_width())
                        self.assertLessEqual(y + widget.winfo_height(), self.root.winfo_height())
                self.assertGreaterEqual(self.app.results_table.winfo_height(), 60, f"{theme} / {stage}")
                self.assertGreaterEqual(self.app.log_view.winfo_height(), 40)
                self.assertFalse(self.app.settings_frame.winfo_manager())

    def test_close_cancels_poll_filter_and_all_owned_after_callbacks(self):
        self.app.search.set("pending filter")
        identifiers = set(self.app._after_ids)
        self.assertTrue(identifiers)
        remaining = set()
        destroy = self.root.destroy

        def inspect_before_destroy():
            remaining.update(self.root.tk.call("after", "info"))
            destroy()

        with patch.object(self.root, "destroy", side_effect=inspect_before_destroy):
            self.app._close()
        self.assertTrue(self.app._closed)
        self.assertEqual(self.app._after_ids, set())
        self.assertIsNone(self.app._poll_after_id)
        self.assertIsNone(self.app._filter_after_id)
        self.assertFalse(identifiers & remaining)
        self.assertIsNone(self.root.tk)

    def test_close_waits_for_collection_page_shutdown_without_blocking_tk(self):
        from unittest.mock import PropertyMock
        page = self.app.collection_page
        with patch.object(type(page), "shutdown_complete", new_callable=PropertyMock, return_value=False) as ready, \
             patch.object(page, "request_close", wraps=page.request_close) as request_close:
            before = time.monotonic()
            self.app._close()
            self.assertLess(time.monotonic() - before, .2)
            request_close.assert_called_once_with()
            self.assertTrue(self.app._closing)
            self.assertFalse(self.app._closed)
            self.root.update()
            ready.return_value = True
            deadline = time.monotonic() + 3
            while not self.app._closed and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
            self.assertTrue(self.app._closed)
            self.assertEqual(self.app._after_ids, set())

    def test_close_requests_stop_without_blocking_and_waits_for_own_worker(self):
        self.create_task()
        entered = threading.Event()
        stopped = threading.Event()

        def running(task, stage, **options):
            task.begin(stage)
            entered.set()
            while not options["control"].requested:
                time.sleep(.005)
            time.sleep(.08)
            task.interrupt(stage, status="stopped")
            stopped.set()
            return task.snapshot()

        self.app.executor = running
        self.app._run_stage()
        self.assertTrue(entered.wait(2))
        before = time.monotonic()
        self.app._close()
        self.assertLess(time.monotonic() - before, .2)
        self.assertTrue(self.app._closing)
        deadline = time.monotonic() + 4
        while not self.app._closed and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)
        self.assertTrue(stopped.is_set())
        self.assertTrue(self.app._closed)
        self.assertFalse(self.app.worker.is_alive())
        self.assertEqual(self.app._after_ids, set())
        self.assertEqual(ReviewTask.open(self.app.task.path).stage_info("import")["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
