import queue
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import desktop_app  # noqa: E402


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class QuietLog:
    def __init__(self):
        self.calls = []

    def configure(self, **kwargs):
        self.calls.append(("configure", kwargs))

    def insert(self, *args):
        self.calls.append(("insert", args))

    def see(self, *args):
        self.calls.append(("see", args))

    def index(self, *_args):
        return "1.0"

    def count(self, *_args):
        return (0,)


class DesktopCommandTests(unittest.TestCase):
    def test_collect_command_has_gui_and_optional_setup_flags(self):
        command = desktop_app.make_collect_command(
            "python",
            Path("results"),
            Path("scraper"),
            "上海",
            "成长目标",
            5,
            3,
            prepare_browser=True,
            setup_only=True,
        )
        self.assertEqual(
            command,
            [
                "python",
                "-u",
                str(desktop_app.PROJECT_ROOT / "collect_architecture.py"),
                "--root",
                "results",
                "--config",
                str(desktop_app.DEFAULT_CONFIG),
                "--batch",
                "target",
                "--pages",
                "5",
                "--scraper-root",
                "scraper",
                "--gui-interactive",
                "--cities",
                "上海",
                "--limit",
                "3",
                "--setup-browser",
                "--setup-only",
            ],
        )

    def test_process_command_omits_scraper_when_not_needed(self):
        command = desktop_app.make_process_command(
            "python",
            Path("source"),
            Path("output"),
            "report",
            "全部城市",
            "当前+成长",
            None,
            4,
            20,
            None,
            Path("config.json"),
        )
        self.assertNotIn("--scraper-root", command)
        self.assertIn("--stage", command)
        self.assertIn("report", command)


class DesktopUtilityTests(unittest.TestCase):
    def test_run_directory_preserves_city_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stamp = "20261017-120000-000000"
            with patch.object(desktop_app, "timestamp", return_value=stamp):
                for city in desktop_app.CITY_CHOICES:
                    with self.subTest(city=city):
                        output = desktop_app.make_run_directory(root, city, 50)
                        city_part = "all" if city == "全部城市" else city
                        self.assertEqual(output.parent, root / "runs")
                        self.assertEqual(output.name, f"{stamp}-{city_part}-50")
                        self.assertTrue(output.is_dir())

    def test_run_directory_is_unique_when_timestamp_collides(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(desktop_app, "timestamp", return_value="20261017-120000-000000"):
                first = desktop_app.make_run_directory(root, "全部城市", None)
                second = desktop_app.make_run_directory(root, "全部城市", None)
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())
            self.assertTrue(second.name.endswith("-2"))

    def test_secret_redaction_covers_assignments_bearer_query_and_openai_key(self):
        raw = (
            'api_key="secret-123" Authorization: Bearer abcdefghijklmnop '
            "https://example.test/?access_token=token-value sk-1234567890123456"
        )
        redacted = desktop_app.redact_secrets(raw)
        self.assertNotIn("secret-123", redacted)
        self.assertNotIn("abcdefghijklmnop", redacted)
        self.assertNotIn("token-value", redacted)
        self.assertNotIn("sk-1234567890123456", redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 4)
        self.assertEqual(
            desktop_app.redact_command(["tool", "--api-key", "separate-credential", "--mode", "safe"]),
            ["tool", "--api-key", "[REDACTED]", "--mode", "safe"],
        )

    def test_progress_parser_handles_collection_jd_and_batch_lines(self):
        self.assertEqual(desktop_app.parse_progress("进度: 3/10"), "3/10")
        self.assertEqual(desktop_app.parse_progress("[JD 总进度] 4/8 已完成"), "4/8")
        self.assertEqual(desktop_app.parse_progress("[批次 2/6] 本批"), "2/6")
        self.assertIsNone(desktop_app.parse_progress("没有进度"))

    def test_planned_full_workflow_is_explicit_and_ordered_by_stage(self):
        self.assertEqual(
            desktop_app.planned_stages("后处理全流程（已有数据）"),
            ["列表前处理", "JD补抓", "Codex语义复核", "生成报告"],
        )
        self.assertEqual(
            desktop_app.planned_stages("完整流程（采集+后处理）"),
            ["岗位采集", "列表前处理", "JD补抓", "Codex语义复核", "生成报告"],
        )

    def test_resume_requires_matching_scope(self):
        options = {
            "root": Path("source").resolve(),
            "city": "上海",
            "track": "当前+成长",
            "limit": None,
            "pages": 10,
            "sample": 50,
            "batch_size": 30,
        }
        manifest = {
            "source_root": str(options["root"]),
            "city": "上海",
            "track": "当前+成长",
            "combination_limit": None,
            "pages_per_combination": 10,
            "sample_size": 50,
            "batch_size": 30,
        }
        self.assertTrue(desktop_app.run_settings_match(manifest, options))
        manifest["sample_size"] = 100
        self.assertFalse(desktop_app.run_settings_match(manifest, options))

    def test_jd_stage_checkpoint_requires_all_batches_successful(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            shortlist = output / "prepared" / "jd_shortlists" / "jd_input.json"
            manifest_path = output / "prepared" / "jd_shortlists" / "details_manifest.json"
            shortlist.parent.mkdir(parents=True)
            shortlist.write_text(json.dumps({"jobs": [{"job_id": "a"}, {"job_id": "b"}]}), encoding="utf-8")
            manifest_path.write_text(
                json.dumps({"total_jobs": 2, "batch_size": 1, "records": [
                    {"batch": 1, "status": "success"}, {"batch": 2, "status": "paused"}
                ]}),
                encoding="utf-8",
            )
            state = {"stages": {"JD补抓": {"status": "success"}}}
            self.assertFalse(desktop_app.can_skip_completed_stage(
                "JD补抓", state, Path(temp), output, settings_match=True
            ))
            checkpoint = json.loads(manifest_path.read_text(encoding="utf-8"))
            checkpoint["records"][1]["status"] = "success"
            manifest_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            self.assertTrue(desktop_app.can_skip_completed_stage(
                "JD补抓", state, Path(temp), output, settings_match=True
            ))

    def test_workflow_resume_skips_all_successful_stages_with_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            output = Path(temp) / "run"
            prepared = output / "prepared"
            shortlist_dir = prepared / "jd_shortlists"
            shortlist_dir.mkdir(parents=True)
            artifacts = {
                prepared / "merged_jobs_index.json": {},
                shortlist_dir / "jd_input.json": {"jobs": []},
                shortlist_dir / "details_manifest.json": {
                    "total_jobs": 0, "batch_size": 30, "records": []
                },
                prepared / "llm_manifest.json": {"status": "completed", "failed_count": 0, "pending_count": 0, "fallback_count": 0},
                prepared / "llm_reviews.json": [],
                prepared / "hai_tou_pool.json": {
                    "summary": {"classification_counts": {}}
                },
            }
            for path, value in artifacts.items():
                path.write_text(json.dumps(value), encoding="utf-8")
            (prepared / "海投分层清单.md").write_text("# report\n", encoding="utf-8")

            labels = [label for label, _ in desktop_app.POST_PROCESS_STAGES]
            options = {
                "root": root,
                "scraper": Path(temp) / "scraper",
                "city": "上海",
                "track": "当前+成长",
                "pages": 10,
                "limit": None,
                "sample": 50,
                "workers": 8,
                "batch_size": 30,
                "write_run_manifest": True,
            }
            manifest = {
                "source_root": str(root),
                "city": options["city"],
                "track": options["track"],
                "combination_limit": options["limit"],
                "pages_per_combination": options["pages"],
                "sample_size": options["sample"],
                "batch_size": options["batch_size"],
                "stages": {label: {"status": "success"} for label in labels},
            }
            (output / "run_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            app = object.__new__(desktop_app.CollectorDesktopApp)
            app.events = queue.Queue()
            app.stop_requested = False
            app.config = {}
            app.config_path = desktop_app.DEFAULT_CONFIG
            app._run_child = Mock(side_effect=AssertionError("completed stage was run again"))

            with patch.object(desktop_app.processor, "inspect_city", return_value={}):
                app._run_workflow(options, "后处理全流程（已有数据）", output)

            app._run_child.assert_not_called()
            result = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "success")
            self.assertTrue(all(result["stages"][label]["status"] == "success" for label in labels))
            logged = []
            while not app.events.empty():
                logged.append(app.events.get_nowait())
            skipped = [value for kind, value in logged if kind == "log" and "复用已完成检查点" in value]
            self.assertEqual(len(skipped), len(labels))

    def test_workflow_stop_persists_manifest_and_stage_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            output = Path(temp) / "run"
            options = {
                "root": root,
                "scraper": Path(temp) / "scraper",
                "city": "上海",
                "track": "当前+成长",
                "pages": 10,
                "limit": None,
                "sample": 50,
                "workers": 8,
                "batch_size": 30,
                "write_run_manifest": True,
            }
            app = object.__new__(desktop_app.CollectorDesktopApp)
            app.events = queue.Queue()
            app.stop_requested = False
            app.config = {}
            app.config_path = desktop_app.DEFAULT_CONFIG

            def stop_child(_command):
                app.stop_requested = True
                return 130

            app._run_child = Mock(side_effect=stop_child)
            with patch.object(desktop_app.processor, "inspect_city", return_value={}):
                app._run_workflow(options, "列表前处理", output)

            self.assertTrue((output / "run_manifest.json").is_file())
            result = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "stopped_or_failed")
            self.assertEqual(result["stages"]["列表前处理"]["exit_code"], 130)


class DesktopWorkflowOutcomeTests(unittest.TestCase):
    def _checkpoint(self, status="partial", exit_code=0, gate=False):
        return {
            "total_jobs": 2,
            "batch_size": 1,
            "records": [
                {"batch": 1, "status": "success", "exit_code": 0,
                 "fetched_count": 1, "missing_job_ids": [], "security_or_login_gate": False},
                {"batch": 2, "status": status, "exit_code": exit_code,
                 "fetched_count": 0, "missing_job_ids": ["b"], "security_or_login_gate": gate},
            ],
        }

    def _run(self, directory, checkpoint, *, jd_exit=0, shortlist=None):
        root = Path(directory) / "source"
        output = Path(directory) / "run"
        shortlist_dir = output / "prepared" / "jd_shortlists"
        shortlist_dir.mkdir(parents=True)
        if shortlist is None:
            shortlist = {"jobs": [{"job_id": "a"}, {"job_id": "b"}]}
        (shortlist_dir / "jd_input.json").write_text(json.dumps(shortlist), encoding="utf-8")
        if checkpoint is not None:
            (shortlist_dir / "details_manifest.json").write_text(json.dumps(checkpoint), encoding="utf-8")
        options = {
            "root": root, "scraper": Path(directory) / "scraper",
            "city": "上海", "track": "当前+成长", "pages": 10, "limit": None,
            "sample": 50, "workers": 8, "batch_size": 1, "write_run_manifest": True,
        }
        app = object.__new__(desktop_app.CollectorDesktopApp)
        app.events = queue.Queue()
        app.stop_requested = False
        app.config = {}
        app.config_path = desktop_app.DEFAULT_CONFIG

        def run_child(command):
            stage = command[command.index("--stage") + 1]
            if stage == "llm":
                (output / "prepared" / "llm_manifest.json").write_text(json.dumps({"status": "completed", "failed_count": 0, "pending_count": 0, "fallback_count": 0}), encoding="utf-8")
            return jd_exit if stage == "fetch-jd" else 0

        app._run_child = Mock(side_effect=run_child)
        with patch.object(desktop_app.processor, "inspect_city", return_value={}):
            app._run_workflow(options, "后处理全流程（已有数据）", output)
        state = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        events = []
        while not app.events.empty():
            events.append(app.events.get_nowait())
        return app, state, events

    def _called_stages(self, app):
        commands = [call.args[0] for call in app._run_child.call_args_list]
        return [command[command.index("--stage") + 1] for command in commands]

    def test_non_gate_partial_or_failed_jd_continues_llm_and_report_as_partial(self):
        for status in ("partial", "failed"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                app, state, events = self._run(temp, self._checkpoint(status=status))
                self.assertEqual(self._called_stages(app), ["shortlist", "fetch-jd", "llm", "report"])
                self.assertEqual(state["stages"]["JD补抓"]["status"], "partial")
                self.assertEqual(state["stages"]["JD补抓"]["exit_code"], 0)
                self.assertEqual(state["stages"]["Codex语义复核"]["status"], "success")
                self.assertEqual(state["stages"]["生成报告"]["status"], "success")
                self.assertEqual(state["status"], "partial")
                self.assertTrue(state["warnings"])
                self.assertFalse(desktop_app.jd_stage_checkpoint_complete(Path(temp) / "run"))
                logs = "".join(value for kind, value in events if kind == "log")
                self.assertIn("缺失", logs)
                self.assertIn("待核实", logs)
                self.assertNotIn("完成验证", logs)
                finished = next(value for kind, value in events if kind == "finished")
                self.assertEqual(finished["exit_code"], 0)

    def test_gate_or_cdp_pause_stops_before_llm_and_report(self):
        for status, gate, pause_reason in (
            ("paused", True, None), ("partial", True, None),
            ("paused", False, "cdp_setup_failed"),
        ):
            with self.subTest(status=status, gate=gate, reason=pause_reason), tempfile.TemporaryDirectory() as temp:
                checkpoint = self._checkpoint(status=status, exit_code=None, gate=gate)
                if pause_reason:
                    checkpoint["records"][-1]["pause_reason"] = pause_reason
                app, state, events = self._run(temp, checkpoint)
                self.assertEqual(self._called_stages(app), ["shortlist", "fetch-jd"])
                self.assertEqual(state["stages"]["JD补抓"]["status"], "paused")
                self.assertEqual(state["status"], "paused")
                finished = next(value for kind, value in events if kind == "finished")
                self.assertEqual(finished["exit_code"], 20)

    def test_nonzero_jd_child_exit_is_not_softened_by_partial_checkpoint(self):
        for exit_code in (7, 20):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as temp:
                app, state, events = self._run(temp, self._checkpoint(), jd_exit=exit_code)
                self.assertEqual(self._called_stages(app), ["shortlist", "fetch-jd"])
                self.assertEqual(state["stages"]["JD补抓"]["status"], "failed")
                self.assertEqual(state["stages"]["JD补抓"]["exit_code"], exit_code)
                self.assertEqual(state["status"], "stopped_or_failed")
                finished = next(value for kind, value in events if kind == "finished")
                self.assertEqual(finished["exit_code"], exit_code)

    def test_nonzero_missing_or_malformed_batch_exit_code_stops_workflow(self):
        for exit_code in (9, None, "0", False, "invalid", "missing"):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as temp:
                checkpoint = self._checkpoint(status="failed", exit_code=exit_code)
                if exit_code == "missing":
                    checkpoint["records"][-1].pop("exit_code")
                app, state, _events = self._run(temp, checkpoint)
                self.assertEqual(self._called_stages(app), ["shortlist", "fetch-jd"])
                self.assertEqual(state["stages"]["JD补抓"]["status"], "failed")
                self.assertEqual(state["status"], "stopped_or_failed")

    def test_missing_or_invalid_checkpoint_stops_instead_of_claiming_pause(self):
        incomplete = self._checkpoint()
        incomplete["records"].pop()
        invalid_count = self._checkpoint()
        invalid_count["total_jobs"] = 3
        pending = self._checkpoint(status="running")
        for checkpoint in (None, "invalid", {}, incomplete, invalid_count, pending):
            with self.subTest(checkpoint=checkpoint), tempfile.TemporaryDirectory() as temp:
                app, state, _events = self._run(temp, checkpoint)
                self.assertEqual(self._called_stages(app), ["shortlist", "fetch-jd"])
                self.assertEqual(state["stages"]["JD补抓"]["status"], "failed")
                self.assertEqual(state["status"], "stopped_or_failed")

    def test_valid_empty_shortlist_skips_jd_without_requiring_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            app, state, events = self._run(temp, None, shortlist={"jobs": []})
            self.assertEqual(self._called_stages(app), ["shortlist", "llm", "report"])
            self.assertEqual(state["stages"]["JD补抓"]["status"], "success")
            self.assertEqual(state["stages"]["JD补抓"]["skipped_reason"], "no_candidates")
            self.assertEqual(state["status"], "success")
            logs = "".join(value for kind, value in events if kind == "log")
            self.assertIn("候选为 0", logs)

    def test_missing_or_invalid_shortlist_is_not_treated_as_zero_candidates(self):
        for shortlist in ({}, {"jobs": None}, {"jobs": ""}, {"jobs": [None]}):
            with self.subTest(shortlist=shortlist), tempfile.TemporaryDirectory() as temp:
                app, state, _events = self._run(temp, None, shortlist=shortlist)
                self.assertEqual(self._called_stages(app), ["shortlist", "fetch-jd"])
                self.assertEqual(state["stages"]["JD补抓"]["status"], "failed")
                self.assertEqual(state["status"], "stopped_or_failed")

    def test_finished_partial_result_is_explicit_in_gui_status_and_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            app, _state, events = self._run(temp, self._checkpoint())
            finished = next(value for kind, value in events if kind == "finished")
            app.events.put(("finished", finished))
            app.root = Mock()
            app.output_directory = Value("")
            app.status_text = Value("")
            app.run_summary = Value("")
            app.progress_text = Value("")
            app._set_busy = Mock()
            app._refresh_history = Mock()
            app._refresh_stats = Mock()
            app._closing = False
            app._poll_events()
            self.assertIn("部分完成", app.status_text.get())
            self.assertIn("待核实", app.status_text.get())
            self.assertIn("缺失", app.run_summary.get())
            self.assertNotIn("登录", app.status_text.get())


class DesktopValidationTests(unittest.TestCase):
    def _app(self, root: Path, scraper: Path):
        app = object.__new__(desktop_app.CollectorDesktopApp)
        app.data_root = Value(str(root))
        app.scraper_root = Value(str(scraper))
        app.city = Value("上海")
        app.track = Value("当前+成长")
        app.stage = Value("仅采集")
        app.pages = Value("10")
        app.workers = Value("8")
        app.batch_size = Value("30")
        app.combination_limit = Value("")
        app.sample_size = Value("")
        return app

    def test_validate_returns_typed_options(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "results"
            scraper = Path(temp) / "scraper"
            (scraper / "scripts").mkdir(parents=True)
            (scraper / "scripts" / "boss_cdp_raw.py").write_text("", encoding="utf-8")
            app = self._app(root, scraper)
            options = app._validate()
            self.assertEqual(options["pages"], 10)
            self.assertIsNone(options["limit"])
            self.assertIsNone(options["sample"])
            self.assertEqual(options["scraper"], scraper.resolve())

    def test_validate_rejects_non_positive_optional_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "results"
            scraper = Path(temp) / "scraper"
            (scraper / "scripts").mkdir(parents=True)
            (scraper / "scripts" / "boss_cdp_raw.py").write_text("", encoding="utf-8")
            app = self._app(root, scraper)
            app.combination_limit.set("0")
            with self.assertRaisesRegex(ValueError, "采集组合数"):
                app._validate()


class DesktopStatsTests(unittest.TestCase):
    def _app(self):
        app = object.__new__(desktop_app.CollectorDesktopApp)
        app.events = queue.Queue()
        app.root = Mock()
        app.worker = None
        app._stats_request_id = 0
        app.data_root = Value("results")
        app.city = Value("上海")
        app.track = Value("当前岗位")
        app.stats_text = Value("")
        app.config = {}
        return app

    def test_stats_ignore_out_of_order_results_and_errors(self):
        for stale_error in (False, True):
            with self.subTest(stale_error=stale_error):
                app = self._app()

                def inspect_city(_root, _config, _city, *, tracks):
                    if tracks == "current" and stale_error:
                        raise RuntimeError("earlier request failed")
                    return {"raw_record_count": 88 if tracks == "current" else 24}

                with patch.object(desktop_app.processor, "inspect_city", side_effect=inspect_city), \
                        patch.object(desktop_app.threading, "Thread") as thread:
                    app._refresh_stats()
                    app.track.set("成长目标")
                    app._refresh_stats()
                    self.assertEqual(thread.call_count, 2)
                    earlier = thread.call_args_list[0].kwargs["target"]
                    latest = thread.call_args_list[1].kwargs["target"]
                    # Complete the newer target scope before the older current scope.
                    latest()
                    app._poll_events()
                    latest_text = app.stats_text.get()
                    self.assertIn("原始记录 24 条", latest_text)
                    earlier()
                    app._poll_events()
                    self.assertEqual(app.stats_text.get(), latest_text)
                    self.assertEqual(app.track.get(), "成长目标")

    def test_latest_stats_error_is_displayed_and_redacted(self):
        app = self._app()
        with patch.object(desktop_app.processor, "inspect_city", side_effect=ValueError("api_key=fake-value")), \
                patch.object(desktop_app.threading, "Thread") as thread:
            app._refresh_stats()
            thread.call_args.kwargs["target"]()
            app._poll_events()
        self.assertIn("统计暂不可用", app.stats_text.get())
        self.assertIn("[REDACTED]", app.stats_text.get())
        self.assertNotIn("fake-value", app.stats_text.get())


class DesktopChildProcessTests(unittest.TestCase):
    def test_real_python_child_and_nested_child_preserve_utf8_and_environment(self):
        app = object.__new__(desktop_app.CollectorDesktopApp)
        app.events = queue.Queue()
        app._process_lock = threading.Lock()
        app.process = None
        app._stopping_process = None
        app.stop_requested = False
        app.waiting_for_human = False

        probe_code = (
            "import json, os, sys\n"
            "print('中文日志：' + sys.argv[1], flush=True)\n"
            "print('错误日志：' + sys.argv[1], file=sys.stderr, flush=True)\n"
            "print('进度: ' + sys.argv[2], flush=True)\n"
            "print(json.dumps({\n"
            "    'source': sys.argv[1],\n"
            "    'stdout_encoding': sys.stdout.encoding.lower(),\n"
            "    'stderr_encoding': sys.stderr.encoding.lower(),\n"
            "    'utf8_mode': sys.flags.utf8_mode,\n"
            "    'io_setting': os.environ['PYTHONIOENCODING'],\n"
            "    'utf8_setting': os.environ['PYTHONUTF8'],\n"
            "    'preserved': os.environ['BOSS_DESKTOP_UTF8_TEST'],\n"
            "    'proxy': os.environ['HTTPS_PROXY'],\n"
            "}), flush=True)\n"
        )
        nested_command = [sys.executable, "-u", "-c", probe_code, "nested", "2/2"]
        child_code = (
            probe_code
            + "\nimport subprocess\n"
            + f"subprocess.run({nested_command!r}, check=True, timeout=10)\n"
        )
        inherited_env = {
            "PYTHONIOENCODING": "gbk",
            "PYTHONUTF8": "0",
            "BOSS_DESKTOP_UTF8_TEST": "preserved-value",
            "HTTPS_PROXY": "http://127.0.0.1:1",
        }
        with patch.dict(os.environ, inherited_env):
            exit_code = app._run_child([sys.executable, "-u", "-c", child_code, "child", "1/2"])
            for key, value in inherited_env.items():
                self.assertEqual(os.environ[key], value)

        events = []
        while not app.events.empty():
            events.append(app.events.get_nowait())
        logs = "".join(value for kind, value in events if kind == "log")
        self.assertEqual(exit_code, 0, logs)
        self.assertNotIn("\ufffd", logs)
        for source in ("child", "nested"):
            self.assertIn(f"中文日志：{source}\n", logs)
            self.assertIn(f"错误日志：{source}\n", logs)
        self.assertEqual([value for kind, value in events if kind == "progress"], ["1/2", "2/2"])
        probes = [json.loads(line) for line in logs.splitlines() if line.startswith("{")]
        self.assertEqual({probe["source"] for probe in probes}, {"child", "nested"})
        for probe in probes:
            self.assertEqual(probe, {
                "source": probe["source"],
                "stdout_encoding": "utf-8",
                "stderr_encoding": "utf-8",
                "utf8_mode": 1,
                "io_setting": "utf-8",
                "utf8_setting": "1",
                "preserved": "preserved-value",
                "proxy": "http://127.0.0.1:1",
            })
        self.assertIsNone(app.process)
        self.assertFalse(app.waiting_for_human)


class DesktopThreadContractTests(unittest.TestCase):
    def test_background_append_log_only_queues_event(self):
        app = object.__new__(desktop_app.CollectorDesktopApp)
        app.events = queue.Queue()
        app.ui_thread_id = threading.get_ident()
        app.log_view = QuietLog()

        thread = threading.Thread(target=app._append_log, args=("api_key=hidden-value\n",))
        thread.start()
        thread.join()

        self.assertEqual(app.log_view.calls, [])
        self.assertEqual(app.events.get_nowait(), ("log", 'api_key="[REDACTED]"\n'))

    def test_child_log_is_redacted_before_display_and_persistence(self):
        app = object.__new__(desktop_app.CollectorDesktopApp)
        app.events = queue.Queue()
        app.ui_thread_id = threading.get_ident()
        app.log_view = QuietLog()
        with tempfile.TemporaryDirectory() as temp:
            app.current_log = Path(temp) / "desktop.log"
            app._record_log('collector says api_key="private-value"\n')
            self.assertTrue(app._log_writer.close().wait(timeout=3))
            app._log_writer.thread.join(timeout=3)
            persisted = app.current_log.read_text(encoding="utf-8")
        inserted = "".join(str(call[1]) for call in app.log_view.calls if call[0] == "insert")
        self.assertNotIn("private-value", persisted)
        self.assertNotIn("private-value", inserted)
        self.assertIn("[REDACTED]", persisted)
        self.assertIn("[REDACTED]", inserted)


class OrderedDesktopLogTests(unittest.TestCase):
    def writer(self, events=None, **options):
        writer = desktop_app._OrderedLogWriter(events if events is not None else queue.Queue(), **options)

        def close():
            self.assertTrue(writer.close().wait(timeout=3), "log writer did not drain")
            writer.thread.join(timeout=3)
            self.assertFalse(writer.thread.is_alive())

        self.addCleanup(close)
        return writer

    def app(self):
        app = object.__new__(desktop_app.CollectorDesktopApp)
        app.events = queue.Queue()
        app.ui_thread_id = threading.get_ident()
        app.log_view = QuietLog()
        return app

    def test_thousand_short_lines_use_one_background_open_write_and_flush(self):
        stream = Mock()
        threads = []

        def opened(*_args, **_kwargs):
            threads.append(threading.get_ident())
            return stream

        with patch.object(Path, "mkdir"), patch.object(Path, "open", side_effect=opened) as opens:
            writer = self.writer(batch_chars=1_000_000, flush_interval=60)
            expected = "".join(f"line-{index}\n" for index in range(1000))
            for index in range(1000):
                writer.append(Path("temp-log") / "desktop.log", f"line-{index}\n")
            self.assertTrue(writer.flush().wait(timeout=3))
            self.assertTrue(writer.close().wait(timeout=3))
        self.assertEqual(opens.call_count, 1)
        stream.write.assert_called_once_with(expected)
        stream.flush.assert_called_once()
        stream.close.assert_called_once()
        self.assertTrue(all(identity != threading.get_ident() for identity in threads))

    def test_periodic_flush_persists_a_quiet_tail_without_waiting_for_close(self):
        written = threading.Event()
        stream = Mock()
        stream.flush.side_effect = written.set
        with patch.object(Path, "mkdir"), patch.object(Path, "open", return_value=stream):
            writer = self.writer(flush_interval=.02)
            writer.append(Path("temporary.log"), "tail\n")
            self.assertTrue(written.wait(timeout=3))
            self.assertTrue(writer.close().wait(timeout=3))
        stream.write.assert_called_once_with("tail\n")

    def test_target_is_snapshotted_per_line_and_preserves_redacted_fifo_order(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "first.log", Path(directory) / "second.log"
            app = self.app()
            app._log_writer = self.writer(app.events, flush_interval=60)
            app.current_log = first
            app._record_log("first-1 api_key=PRIVATE_KEY\n")
            app.current_log = second
            app._record_log("second-1\n")
            app.current_log = first
            app._record_log("first-2 Cookie: PRIVATE_COOKIE\n")
            self.assertTrue(app._log_writer.close().wait(timeout=3))
            saved_first = first.read_text(encoding="utf-8")
            self.assertEqual(saved_first.splitlines(), ['first-1 api_key="[REDACTED]"', 'first-2 Cookie: [REDACTED]'])
            self.assertEqual(second.read_text(encoding="utf-8"), "second-1\n")
            self.assertNotIn("PRIVATE", saved_first)
            inserted = "".join(str(call[1]) for call in app.log_view.calls if call[0] == "insert")
            self.assertLess(inserted.index("first-1"), inserted.index("second-1"))
            self.assertLess(inserted.index("second-1"), inserted.index("first-2"))
            self.assertNotIn("PRIVATE", inserted)

    def test_blocked_disk_never_blocks_tk_and_close_waits_by_scheduling(self):
        entered, release = threading.Event(), threading.Event()
        stream = Mock()

        def opened(*_args, **_kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)
            entered.set()
            if not release.wait(timeout=3):
                raise OSError("test release timed out")
            return stream

        main_thread = threading.get_ident()
        app = self.app()
        app.root = Mock()
        app.root.winfo_exists.return_value = True
        app.worker = None
        app._poll_after_id = None
        app.current_log = Path("temp") / "desktop.log"
        app._closing = True
        with patch.object(Path, "mkdir"), patch.object(Path, "open", side_effect=opened):
            app._log_writer = self.writer(app.events, batch_chars=1)
            try:
                app._record_log("visible immediately\n")
                self.assertTrue(entered.wait(timeout=2))
                app.events.put(("log", "queued final line\n"))
                app._finalize_close()
                app.root.destroy.assert_not_called()
                self.assertEqual(app.root.after.call_args.args, (50, app._finalize_close))
                self.assertTrue(any("visible immediately" in str(call) for call in app.log_view.calls))
                self.assertTrue(any("queued final line" in str(call) for call in app.log_view.calls))
            finally:
                release.set()
                self.assertTrue(app._log_writer.close().wait(timeout=3))
            app._finalize_close()
            app.root.destroy.assert_called_once()
        self.assertEqual("".join(call.args[0] for call in stream.write.call_args_list),
                         "visible immediately\nqueued final line\n")

    def test_write_failure_has_one_safe_ui_warning_and_no_recursive_log_write(self):
        events = queue.Queue()
        with patch.object(Path, "mkdir"), \
                patch.object(Path, "open", side_effect=OSError("PRIVATE_PATH api_key=PRIVATE_KEY")):
            writer = self.writer(events, batch_chars=1)
            writer.append(Path("sensitive-path.log"), "PRIVATE_CONTENT\n")
            writer.append(Path("sensitive-path.log"), "second failure\n")
            self.assertTrue(writer.close().wait(timeout=3))
        self.assertEqual(list(events.queue), [("log_write_error", desktop_app.LOG_WRITE_WARNING)])
        app = self.app()
        app.root = Mock()
        app.events = events
        app._append_log = Mock()
        app._record_log = Mock()
        app._poll_events()
        app._append_log.assert_called_once_with(desktop_app.LOG_WRITE_WARNING)
        app._record_log.assert_not_called()

    def test_stop_requests_an_async_flush_without_changing_child_ownership(self):
        app = self.app()
        app._log_writer = Mock()
        app._process_lock = threading.Lock()
        app.process = None
        app.status_text = Value("")
        app._stop(pause=True)
        app._log_writer.flush.assert_called_once()
        self.assertTrue(app.stop_requested)
        self.assertTrue(app._pause_requested)
        self.assertIsNone(app.process)


if __name__ == "__main__":
    unittest.main()
