import json
import io
import copy
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from collect_architecture import (  # noqa: E402
    DEFAULT_PAGE_WAIT_MIN,
    DEFAULT_PAGE_WAIT_MAX,
    MAX_PAGE_WAIT,
    build_specs,
    invoke_spec,
    load_config,
    main,
    parse_args,
    page_progress,
    persist_manifest_record,
    persist_manifest_state,
    shard_specs,
    security_detected,
    should_display_upstream_line,
    valid_payload,
    validate_page_wait,
)
from collection_plan import select_plan_combinations  # noqa: E402


class FakeCollectionProcess:
    def __init__(self, output, returncode=0):
        self.stdout = iter(output.splitlines(keepends=True))
        self.returncode = returncode

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 130


class CollectionConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(PROJECT_ROOT / "config" / "architecture_collection.json")

    def test_full_matrix_has_672_unique_combinations(self):
        specs = build_specs(self.config)
        self.assertEqual(len(specs), 672)
        self.assertEqual(len({spec["combination"] for spec in specs}), 672)

    def test_original_city_and_positional_interfaces_are_compatible(self):
        self.assertEqual(len(build_specs(self.config, "all", None, ["上海"])), 112)
        self.assertEqual(len(build_specs(self.config, "current", None, ["SHANGHAI"])), 88)
        self.assertEqual(len(build_specs(self.config, "target", None, ["shanghai"])), 24)
        self.assertEqual(build_specs(self.config, "all", 3), build_specs(self.config)[:3])

    def test_plan_exact_selection_precedes_external_batch_city_and_limit(self):
        full = build_specs(self.config)
        selected = [full[123], full[-1]]
        plan = select_plan_combinations(self.config, [spec["combination"] for spec in selected])
        self.assertEqual(build_specs(plan, limit=1), [selected[0]])
        self.assertEqual(build_specs(plan, "target", 1, ["北京"]), [selected[1]])
        self.assertEqual(build_specs(plan, "current", None, ["上海"]), [])
        self.assertEqual(build_specs(plan, "target", None, ["深圳"]), [])

    def test_plan_dimension_filter_and_empty_track_do_not_fall_back_to_all(self):
        plan = copy.deepcopy(self.config)
        plan["collection_selection"] = {
            "cities": ["shenzhen"], "salaries": ["20_50"],
            "current": {"keywords": [], "experiences": []},
            "target": {"keywords": ["aiarch"], "experiences": ["5_10"]},
            "combinations": None,
        }
        selected = build_specs(plan)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["combination"], "target__shenzhen__aiarch__20_50__5_10")
        self.assertEqual(build_specs(plan, "current", 1), [])

    def test_saved_selection_is_honored_by_cli_dry_run_without_browser(self):
        full = build_specs(self.config)
        selected = [full[123], full[-1]]
        plan = select_plan_combinations(self.config, [spec["combination"] for spec in selected])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "selected-plan.json"
            path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            console = io.StringIO()
            with patch("collect_architecture.resolve_scraper_root") as resolver, redirect_stdout(console):
                result = main(["--config", str(path), "--batch", "target", "--limit", "1", "--cities", "北京", "--dry-run"])
            resolver.assert_not_called()
            self.assertEqual(result, 0)
            self.assertIn(selected[1]["combination"], console.getvalue())
            self.assertNotIn(selected[0]["combination"], console.getvalue())

    def test_invalid_plan_and_track_fail_before_any_collection(self):
        plan = copy.deepcopy(self.config)
        plan["cities"][0]["id"] = "../unsafe"
        with self.assertRaisesRegex(ValueError, "不安全"):
            build_specs(plan)
        with self.assertRaisesRegex(ValueError, "采集轨道"):
            build_specs(self.config, "other")
        with self.assertRaisesRegex(ValueError, "正整数"):
            build_specs(self.config, limit=0)

    def test_added_keywords_cover_current_and_target(self):
        specs = build_specs(self.config)
        added = {"aiarch", "agentarch", "solconsult"}
        self.assertEqual(sum(spec["keyword_id"] in added for spec in specs), 216)
        self.assertEqual(sum(spec["track"] == "current" and spec["keyword_id"] in added for spec in specs), 144)
        self.assertEqual(sum(spec["track"] == "target" and spec["keyword_id"] in added for spec in specs), 72)

    def test_security_output_stops_collection(self):
        self.assertTrue(security_detected("打开 _security_check 页面"))
        self.assertTrue(security_detected("请完成安全验证"))
        self.assertFalse(security_detected("完成: 150 条,其中新增 150 条"))

    def test_page_progress(self):
        self.assertEqual(page_progress("[1/10 页]\n[10/10 页]"), (10, 2))
        self.assertEqual(page_progress("没有页码"), (0, 0))

    def test_upstream_job_rows_are_hidden_only_by_quiet_filter(self):
        self.assertFalse(
            should_display_upstream_line(
                "  ✓ NEW AI应用工程师/研究员 | 20-35K·16薪 | 深圳 | 华为技术有限公司"
            )
        )
        self.assertFalse(
            should_display_upstream_line(
                "  √ NEW AI应用开发工程师 | 25-40K | 深圳·南山区 | 某公司"
            )
        )
        self.assertTrue(should_display_upstream_line("  本页 15 条, 新增 15, 累计 15"))
        self.assertTrue(should_display_upstream_line("--- [1/10 页, 0 条已抓] ---"))
        self.assertTrue(should_display_upstream_line("⚠️ 登录状态异常，已停止抓取"))

    def test_upstream_rows_are_visible_by_default(self):
        self.assertTrue(parse_args([]).verbose_upstream)
        self.assertTrue(parse_args(["--verbose-upstream"]).verbose_upstream)
        self.assertFalse(parse_args(["--quiet-upstream"]).verbose_upstream)

    def test_page_wait_contract_defaults_fixed_and_finite_range(self):
        self.assertEqual((DEFAULT_PAGE_WAIT_MIN, DEFAULT_PAGE_WAIT_MAX, MAX_PAGE_WAIT), (12.0, 22.0, 300.0))
        args = parse_args([])
        self.assertEqual((args.page_wait_min, args.page_wait_max), (12.0, 22.0))
        for minimum, maximum in ((0, 0), (1.25, 3.75), (300, 300)):
            self.assertEqual(validate_page_wait(minimum, maximum), (float(minimum), float(maximum)))
            args = parse_args(["--page-wait-min", str(minimum), "--page-wait-max", str(maximum)])
            self.assertEqual((args.page_wait_min, args.page_wait_max), (minimum, maximum))
            self.assertIsInstance(args.page_wait_min, float)
        for minimum, maximum in ((float("nan"), 22), (12, float("nan")), (float("inf"), 22),
                                 (12, float("inf")), (float("-inf"), 22), (-1, 22), (22, 12),
                                 (0, 301), (None, 22), ("not-a-number", 22)):
            with self.subTest(minimum=minimum, maximum=maximum), self.assertRaises(ValueError):
                validate_page_wait(minimum, maximum)

    def test_invalid_page_wait_fails_before_resolving_or_starting_any_subprocess(self):
        with patch("collect_architecture.resolve_scraper_root") as resolver, \
                patch("collect_architecture.subprocess.Popen") as process, \
                patch("collect_architecture.run_setup_browser") as setup, \
                redirect_stderr(io.StringIO()):
            for minimum, maximum in (("nan", "22"), ("12", "inf"), ("-1", "22"), ("22", "12"), ("0", "301")):
                with self.subTest(minimum=minimum, maximum=maximum), self.assertRaises(SystemExit) as error:
                    main(["--page-wait-min", minimum, "--page-wait-max", maximum, "--setup-only"])
                self.assertEqual(error.exception.code, 2)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "never-created"
                with self.assertRaises(ValueError):
                    invoke_spec(self._invoke_spec_fixture(), root=root, manifest={"records": []},
                                manifest_path=root / "collection_manifest.json", python=Path(sys.executable),
                                scraper_root=root / "scraper", pages=2, page_wait_min=2, page_wait_max=1)
                self.assertFalse(root.exists())
        resolver.assert_not_called()
        process.assert_not_called()
        setup.assert_not_called()

    def test_wait_help_and_dry_run_are_offline_and_describe_configured_bounds(self):
        console = io.StringIO()
        with patch("collect_architecture.resolve_scraper_root") as resolver, \
                patch("collect_architecture.subprocess.Popen") as process, \
                patch("collect_architecture.run_setup_browser") as setup, \
                redirect_stdout(console):
            with self.assertRaises(SystemExit) as help_exit:
                main(["--help"])
            self.assertEqual(help_exit.exception.code, 0)
            self.assertEqual(main(["--dry-run", "--limit", "1", "--page-wait-min", "1.25", "--page-wait-max", "3.75"]), 0)
        self.assertIn("翻页等待: 1.25–3.75 秒", console.getvalue())
        self.assertIn("--page-wait-min", console.getvalue())
        resolver.assert_not_called()
        process.assert_not_called()
        setup.assert_not_called()

    def test_main_passes_configured_waits_to_each_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("collect_architecture.resolve_scraper_root", return_value=root / "scraper"), \
                    patch("collect_architecture.resolve_python", return_value=Path(sys.executable)), \
                    patch("collect_architecture.invoke_spec", return_value="success") as collect, \
                    redirect_stdout(io.StringIO()):
                result = main(["--root", str(root), "--limit", "1", "--page-wait-min", "0.25", "--page-wait-max", "1.5"])
            self.assertEqual(result, 0)
            self.assertEqual((collect.call_args.kwargs["page_wait_min"], collect.call_args.kwargs["page_wait_max"]), (0.25, 1.5))

    def test_invoke_wait_adapter_command_checkpoint_and_manifest_keep_original_args(self):
        cases = (({}, (12.0, 22.0)), ({"page_wait_min": 1.5, "page_wait_max": 2.5}, (1.5, 2.5)),
                 ({"page_wait_min": 0, "page_wait_max": 0}, (0.0, 0.0)))
        for options, expected in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "中文 输出目录"
                spec = self._invoke_spec_fixture()
                checkpoint = root / "checkpoints" / f"{spec['combination']}.partial.json"
                checkpoint.parent.mkdir(parents=True)
                checkpoint.write_text('{"jobs": []}', encoding="utf-8")
                manifest = {"records": []}
                scraper = Path(directory) / "上游 目录"

                def fake_popen(command, **_kwargs):
                    Path(command[command.index("--output") + 1]).write_text('{"jobs": [{"id": "1"}]}', encoding="utf-8")
                    return FakeCollectionProcess("完成: 1 条\n")

                with patch("collect_architecture.subprocess.Popen", side_effect=fake_popen) as process, \
                        redirect_stdout(io.StringIO()):
                    status = invoke_spec(spec, root=root, manifest=manifest,
                                         manifest_path=root / "collection_manifest.json", python=Path(sys.executable),
                                         scraper_root=scraper, pages=2, cdp_port=9333, **options)
                self.assertEqual(status, "success")
                command = process.call_args.args[0]
                self.assertEqual(command[:10], [str(Path(sys.executable)), "-u", str(PROJECT_ROOT / "collection_runtime.py"),
                                               "--scraper-root", str(scraper), "--page-wait-min", str(expected[0]),
                                               "--page-wait-max", str(expected[1]), "--"])
                self.assertEqual(command[10:], ["--keyword", spec["keyword"], "--city", spec["city"], "--pages", "2",
                                               "--salary", spec["salary_code"], "--experience", spec["experience_code"],
                                               "--degree", spec["degree"], "--cdp-port", "9333", "--no-detail", "--output",
                                               str(root / "raw" / spec["track"] / spec["city_id"] / f"{spec['combination']}.json"),
                                               "--merge", str(checkpoint)])
                saved = json.loads((root / "collection_manifest.json").read_text(encoding="utf-8"))["records"][0]
                self.assertEqual((saved["page_wait_min"], saved["page_wait_max"]), expected)

    def test_gui_security_marks_browser_immediately_then_stops_at_next_combination_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "chrome.session-paused"
            calls = []

            class SecurityProcess(FakeCollectionProcess):
                def __init__(inner_self):
                    def lines():
                        yield "请完成安全验证\n"
                        self.assertTrue(marker.is_file(), "signal siblings while stdout is still being consumed")
                        yield "已停止当前搜索\n"
                    inner_self.stdout = lines()
                    inner_self.returncode = 1

                def wait(inner_self):
                    self.assertTrue(marker.is_file())
                    return inner_self.returncode

            def fake_popen(command, **_kwargs):
                calls.append(command)
                if len(calls) == 1:
                    return SecurityProcess()
                Path(command[command.index("--output") + 1]).write_text('{"jobs": [{"id": "1"}]}', encoding="utf-8")
                return FakeCollectionProcess("完成: 1 条\n")

            def confirm(_prompt):
                self.assertTrue(marker.exists())
                saved = json.loads((root / "collection_manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["records"][0]["status"], "paused")
                return ""

            console = io.StringIO()
            with patch.dict(os.environ, {"BOSS_COLLECTION_DESKTOP_OWNER_TOKEN": "a" * 32,
                                         "BOSS_COLLECTION_GATE_STOP_FILE": str(marker)}), \
                    patch("collect_architecture.resolve_scraper_root", return_value=root / "scraper"), \
                    patch("collect_architecture.resolve_python", return_value=Path(sys.executable)), \
                    patch("collect_architecture.subprocess.Popen", side_effect=fake_popen), \
                    patch("collect_architecture.input", side_effect=confirm) as human, \
                    redirect_stdout(console):
                result = main(["--root", str(root), "--gui-interactive", "--limit", "2"])
            self.assertEqual(result, 20)
            self.assertEqual(len(calls), 2, "only the gated combination can retry after confirmation")
            human.assert_called_once()
            self.assertIn("BROWSER_GATE_PAUSED", console.getvalue())
            self.assertTrue(marker.exists())
            saved = json.loads((root / "collection_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["records"]), 1)
            self.assertEqual(saved["records"][0]["status"], "success")

    def test_existing_gui_gate_marker_exits_before_resolving_or_spawning_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "session-paused"
            marker.write_text("external marker is preserved", encoding="utf-8")
            console = io.StringIO()
            with patch.dict(os.environ, {"BOSS_COLLECTION_DESKTOP_OWNER_TOKEN": "b" * 32,
                                         "BOSS_COLLECTION_GATE_STOP_FILE": str(marker)}), \
                    patch("collect_architecture.resolve_scraper_root") as resolver, \
                    patch("collect_architecture.subprocess.Popen") as process, redirect_stdout(console):
                result = main(["--gui-interactive", "--limit", "1"])
            self.assertEqual(result, 20)
            resolver.assert_not_called()
            process.assert_not_called()
            self.assertIn("BROWSER_GATE_PAUSED", console.getvalue())
            self.assertEqual(marker.read_text(encoding="utf-8"), "external marker is preserved")

    def test_gate_marker_isolated_between_browser_sessions_and_ignored_without_gui_owner(self):
        for gui, token, same_browser in ((False, "c" * 32, True), (True, "invalid", True), (True, "c" * 32, False)):
            with self.subTest(gui=gui, token=token, same_browser=same_browser), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                old_marker, other_marker = root / "chrome-paused", root / "edge-paused"
                old_marker.write_text("paused", encoding="utf-8")
                selected_marker = old_marker if same_browser else other_marker
                with patch.dict(os.environ, {"BOSS_COLLECTION_DESKTOP_OWNER_TOKEN": token,
                                             "BOSS_COLLECTION_GATE_STOP_FILE": str(selected_marker)}), \
                        patch("collect_architecture.resolve_scraper_root", return_value=root / "scraper"), \
                        patch("collect_architecture.resolve_python", return_value=Path(sys.executable)), \
                        patch("collect_architecture.invoke_spec", return_value="success") as collect, \
                        redirect_stdout(io.StringIO()):
                    result = main(["--root", str(root), "--limit", "1", *(["--gui-interactive"] if gui else [])])
                self.assertEqual(result, 0)
                collect.assert_called_once()
                self.assertEqual(old_marker.read_text(encoding="utf-8"), "paused")
                self.assertFalse(other_marker.exists())

    def test_security_marker_write_requires_gui_owner_and_preserves_existing_content(self):
        for gui, token, existing in ((False, "d" * 32, False), (True, "invalid", False), (True, "d" * 32, True)):
            with self.subTest(gui=gui, token=token, existing=existing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                marker = root / "browser-paused"
                if existing:
                    marker.write_text("preserved", encoding="utf-8")
                with patch.dict(os.environ, {"BOSS_COLLECTION_DESKTOP_OWNER_TOKEN": token,
                                             "BOSS_COLLECTION_GATE_STOP_FILE": str(marker)}), \
                        patch("collect_architecture.GUI_INTERACTIVE_MODE", gui):
                    status, _record, _console, _process = self._run_mock_collection(root, "请完成安全验证\n", [], exit_code=1)
                self.assertEqual(status, "paused")
                self.assertEqual(marker.exists(), existing)
                if existing:
                    self.assertEqual(marker.read_text(encoding="utf-8"), "preserved")

    def test_collection_logs_redact_credentials_and_cookies(self):
        spec = self._invoke_spec_fixture()

        class FakeProcess:
            stdout = iter([
                "Authorization: Bearer auth-secret\n",
                "Cookie: sid=cookie-secret\n",
                "api_key=key-secret\n",
            ])

            def wait(self):
                return 0

            def poll(self):
                return 0

            def terminate(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            with patch("collect_architecture.subprocess.Popen", return_value=FakeProcess()), redirect_stdout(output):
                status = invoke_spec(
                    spec,
                    root=root,
                    manifest={"records": []},
                    manifest_path=root / "collection_manifest.json",
                    python=Path(sys.executable),
                    scraper_root=root,
                    pages=1,
                    verbose_upstream=True,
                )

            persisted = (root / "raw" / "current" / "shanghai" / f"{spec['combination']}.log").read_text(encoding="utf-8")

        self.assertEqual(status, "failed")
        for secret in ("auth-secret", "cookie-secret", "key-secret"):
            self.assertNotIn(secret, output.getvalue())
            self.assertNotIn(secret, persisted)
        self.assertIn("[REDACTED]", output.getvalue())
        self.assertIn("[REDACTED]", persisted)

    def test_shard_specs_split_deterministically_and_cover_all(self):
        specs = [{"combination": f"c{i}"} for i in range(100)]
        shards = [shard_specs(specs, i, 4) for i in range(4)]
        self.assertEqual(sum(len(s) for s in shards), 100)
        all_ids = [spec["combination"] for s in shards for spec in s]
        self.assertEqual(sorted(all_ids), sorted(spec["combination"] for spec in specs))
        for i in range(4):
            self.assertEqual(shard_specs(specs, i, 4), shards[i])

    def test_valid_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.json"
            path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
            self.assertTrue(valid_payload(path))
            path.write_text(json.dumps({"jobs": "not-a-list"}), encoding="utf-8")
            self.assertFalse(valid_payload(path))

    def _invoke_spec_fixture(self):
        return {
            "combination": "current__shanghai__kw__salary__exp",
            "track": "current",
            "city": "上海",
            "city_id": "shanghai",
            "keyword": "AI 架构",
            "keyword_id": "kw",
            "salary": "不限",
            "salary_code": "",
            "experience": "不限",
            "experience_code": "",
            "degree": "203",
        }

    def _saved_collection_fixture(self, root, status, jobs, output=None, **metadata):
        spec = self._invoke_spec_fixture()
        track_dir = root / "raw" / spec["track"] / spec["city_id"]
        track_dir.mkdir(parents=True, exist_ok=True)
        output_path = track_dir / f"{spec['combination']}.json"
        log_path = output_path.with_suffix(".log")
        output_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        if output is not None:
            log_path.write_text(output, encoding="utf-8")
        manifest = {"records": [{
            **spec,
            "status": status,
            "job_count": len(jobs),
            "exit_code": 0,
            "json": str(output_path),
            "log": str(log_path),
            **metadata,
        }]}
        (root / "collection_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest, output_path, log_path

    def _run_mock_collection(self, root, output, jobs, manifest=None, exit_code=0):
        if manifest is None:
            manifest = {"records": []}

        def fake_popen(command, **_kwargs):
            output_path = Path(command[command.index("--output") + 1])
            output_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
            return FakeCollectionProcess(output, exit_code)

        console = io.StringIO()
        with patch("collect_architecture.subprocess.Popen", side_effect=fake_popen) as mocked_popen, \
            patch("collect_architecture.input") as mocked_input, redirect_stdout(console):
            status = invoke_spec(
                self._invoke_spec_fixture(),
                root=root,
                manifest=manifest,
                manifest_path=root / "collection_manifest.json",
                python=Path(sys.executable),
                scraper_root=root,
                pages=2,
                wait_on_gate=False,
            )
        mocked_input.assert_not_called()
        return status, manifest["records"][0], console.getvalue(), mocked_popen

    def test_empty_capture_errors_fail_once_and_are_retryable(self):
        cases = (
            ("未捕获到页面自身的搜索响应", "搜索响应未捕获"),
            ("未捕获到搜索响应", "搜索响应未捕获"),
            ("未捕获到职位数据", "职位数据未捕获"),
            ("未捕获到 API 职位数据", "职位数据未捕获"),
            ("joblist 响应不是有效 JSON: Expecting value", "职位响应 JSON 解析失败"),
            ("JSON 解析失败", "职位响应 JSON 解析失败"),
            ("joblist 响应解析异常", "职位响应 JSON 解析失败"),
        )
        for warning, expected_reason in cases:
            with self.subTest(warning=warning), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                status, record, console, process = self._run_mock_collection(root, warning + "\n", [])
                self.assertEqual(status, "failed")
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["exit_code"], 0)
                self.assertEqual(record["job_count"], 0)
                self.assertTrue(record["retryable"])
                self.assertIn(expected_reason, record["failure_reason"])
                self.assertIn("重试", record["failure_reason"])
                self.assertIn(expected_reason, console)
                self.assertNotIn("DONE ", console)
                process.assert_called_once()
                saved = json.loads((root / "collection_manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["records"][0], record)

    def test_normal_empty_result_remains_no_data(self):
        with tempfile.TemporaryDirectory() as directory:
            status, record, console, process = self._run_mock_collection(
                Path(directory), "本页 0 条\n无数据，已保存空结果\n", []
            )
            self.assertEqual(status, "no_data")
            self.assertIsNone(record["failure_reason"])
            self.assertFalse(record["retryable"])
            self.assertIn("DONE ", console)
            process.assert_called_once()

    def test_confirmed_empty_response_allows_missing_job_data_warning(self):
        output = (
            "探测样本没有职位，暂时无法确认登录态；继续执行实际职位搜索。\n"
            "未捕获到职位数据，已跳过 DOM fallback\n"
            "无数据，已保存空结果\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            status, record, _console, _process = self._run_mock_collection(Path(directory), output, [])
            self.assertEqual(status, "no_data")
            self.assertIsNone(record["failure_reason"])
            self.assertFalse(record["retryable"])

    def test_empty_response_marker_does_not_override_capture_or_parse_failure(self):
        for warning in ("未捕获到页面自身的搜索响应", "joblist 响应不是有效 JSON", "JSON 解析失败"):
            with self.subTest(warning=warning), tempfile.TemporaryDirectory() as directory:
                output = (
                    "探测样本没有职位，暂时无法确认登录态；继续执行实际职位搜索。\n"
                    "未捕获到职位数据\n" + warning + "\n"
                )
                status, record, _console, process = self._run_mock_collection(Path(directory), output, [])
                self.assertEqual(status, "failed")
                self.assertTrue(record["retryable"])
                process.assert_called_once()

    def test_legacy_empty_errors_are_recollected_instead_of_skipped(self):
        for old_status in ("no_data", "success"):
            for warning in ("未捕获到页面自身的搜索响应", "未捕获到职位数据", "joblist 响应不是有效 JSON"):
                with self.subTest(status=old_status, warning=warning), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    manifest, _output_path, _log_path = self._saved_collection_fixture(
                        root, old_status, [], warning
                    )
                    status, record, console, process = self._run_mock_collection(
                        root, "完成: 1 条\n", [{"job_id": "recovered"}], manifest
                    )
                    self.assertEqual(status, "success")
                    self.assertEqual(record["job_count"], 1)
                    self.assertIn("重试", record["resume_retry_reason"])
                    self.assertIn("RETRY ", console)
                    self.assertNotIn("SKIP ", console)
                    process.assert_called_once()

    def test_legacy_genuine_empty_results_remain_skipped(self):
        outputs = (
            None,
            "无数据，已保存空结果\n",
            "探测样本没有职位，暂时无法确认登录态；继续执行实际职位搜索。\n未捕获到职位数据\n",
        )
        for output in outputs:
            with self.subTest(output=output), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest, output_path, _log_path = self._saved_collection_fixture(root, "no_data", [], output)
                original_payload = output_path.read_text(encoding="utf-8")
                status, _record, console, process = self._run_mock_collection(
                    root, "完成: 1 条\n", [{"job_id": "must-not-run"}], manifest
                )
                self.assertEqual(status, "no_data")
                self.assertIn("SKIP ", console)
                self.assertEqual(output_path.read_text(encoding="utf-8"), original_payload)
                process.assert_not_called()

    def test_legacy_empty_retry_can_read_the_manifest_log_reference(self):
        for relative in (False, True):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest, _output_path, _log_path = self._saved_collection_fixture(root, "no_data", [])
                archived_log = root / "previous_attempt.log"
                archived_log.write_text("joblist 响应不是有效 JSON: token=old-secret\n", encoding="utf-8")
                manifest["records"][0]["log"] = archived_log.name if relative else str(archived_log)
                status, record, console, process = self._run_mock_collection(
                    root, "完成: 1 条\n", [{"job_id": "recovered"}], manifest
                )
                self.assertEqual(status, "success")
                self.assertIn("JSON 解析失败", record["resume_retry_reason"])
                self.assertNotIn("old-secret", console)
                self.assertNotIn("old-secret", json.dumps(record))
                process.assert_called_once()

    def test_local_log_takes_priority_over_old_manifest_log_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, _output_path, _log_path = self._saved_collection_fixture(
                root, "no_data", [], "无数据，已保存空结果\n"
            )
            stale_log = root / "stale.log"
            stale_log.write_text("未捕获到搜索响应\n", encoding="utf-8")
            manifest["records"][0]["log"] = str(stale_log)
            status, _record, console, process = self._run_mock_collection(root, "", [], manifest)
            self.assertEqual(status, "no_data")
            self.assertIn("SKIP ", console)
            process.assert_not_called()

    def test_successful_nonempty_result_is_skipped_despite_later_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, _output_path, _log_path = self._saved_collection_fixture(
                root, "success", [{"job_id": "saved"}],
                "[1/2 页]\n本页 1 条\n[2/2 页]\n未捕获到职位数据\njoblist 响应不是有效 JSON\n",
            )
            status, _record, console, process = self._run_mock_collection(root, "", [], manifest)
            self.assertEqual(status, "success")
            self.assertIn("SKIP ", console)
            process.assert_not_called()

    def test_nonempty_result_with_later_page_errors_remains_success(self):
        output = (
            "[1/2 页]\n本页 1 条, 累计 1\n[2/2 页]\n"
            "joblist 响应不是有效 JSON\n未捕获到页面自身的搜索响应\n未捕获到职位数据\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            status, record, _console, process = self._run_mock_collection(
                Path(directory), output, [{"job_id": "usable"}]
            )
            self.assertEqual(status, "success")
            self.assertEqual(record["job_count"], 1)
            self.assertEqual(record["pages_completed"], 2)
            self.assertIsNone(record["failure_reason"])
            self.assertFalse(record["retryable"])
            process.assert_called_once()

    def test_empty_error_logs_and_manifest_do_not_expose_credentials(self):
        output = (
            "joblist 响应不是有效 JSON: token=parse-secret\n"
            "未捕获到搜索响应 Authorization: Bearer auth-secret\n"
            "Cookie: sid=cookie-secret\napi_key=key-secret\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status, record, console, process = self._run_mock_collection(root, output, [])
            self.assertEqual(status, "failed")
            process.assert_called_once()
            persisted = Path(record["log"]).read_text(encoding="utf-8")
            saved = (root / "collection_manifest.json").read_text(encoding="utf-8")
            for secret in ("parse-secret", "auth-secret", "cookie-secret", "key-secret"):
                self.assertNotIn(secret, console)
                self.assertNotIn(secret, persisted)
                self.assertNotIn(secret, saved)
            self.assertIn("[REDACTED]", console)
            self.assertIn("[REDACTED]", persisted)

    def test_security_or_login_gate_takes_priority_over_empty_errors(self):
        for gate in (
            "请完成安全验证", "登录状态异常", "未检测到 BOSS直聘登录状态", "BOSS 接口返回限制状态(code=37)",
        ):
            with self.subTest(gate=gate), tempfile.TemporaryDirectory() as directory:
                output = gate + "\n未捕获到搜索响应\njoblist 响应不是有效 JSON\n"
                status, record, _console, process = self._run_mock_collection(Path(directory), output, [])
                self.assertEqual(status, "paused")
                self.assertTrue(record["security_or_login_gate"])
                self.assertEqual(record["gate_wait_result"], "waiting_disabled")
                self.assertTrue(Path(record["checkpoint"]).is_file())
                self.assertIsNone(record["failure_reason"])
                self.assertFalse(record["retryable"])
                process.assert_called_once()

    def test_retryable_empty_failure_is_retried_only_on_next_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {"records": []}
            status, record, _console, first_process = self._run_mock_collection(
                root, "未捕获到搜索响应\n", [], manifest
            )
            self.assertEqual(status, "failed")
            self.assertTrue(record["retryable"])
            first_process.assert_called_once()
            status, record, _console, second_process = self._run_mock_collection(
                root, "完成: 1 条\n", [{"job_id": "recovered"}], manifest
            )
            self.assertEqual(status, "success")
            self.assertIsNone(record["failure_reason"])
            self.assertFalse(record["retryable"])
            second_process.assert_called_once()

    def test_new_no_data_diagnostics_override_errors_from_earlier_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, _output_path, _log_path = self._saved_collection_fixture(
                root, "no_data", [], "joblist 响应不是有效 JSON\n请完成安全验证\n无数据，已保存空结果\n",
                failure_reason=None, retryable=False,
            )
            status, _record, console, process = self._run_mock_collection(root, "", [], manifest)
            self.assertEqual(status, "no_data")
            self.assertIn("SKIP ", console)
            process.assert_not_called()

    def test_gate_retry_to_valid_empty_uses_final_attempt_diagnostics(self):
        spec = self._invoke_spec_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {"records": []}
            outputs = [
                "请完成安全验证\n未捕获到搜索响应\njoblist 响应不是有效 JSON\n",
                "探测样本没有职位，暂时无法确认登录态；继续执行实际职位搜索。\n未捕获到职位数据\n",
            ]

            def fake_popen(command, **_kwargs):
                output_path = Path(command[command.index("--output") + 1])
                output_path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
                return FakeCollectionProcess(outputs.pop(0))

            with patch("collect_architecture.subprocess.Popen", side_effect=fake_popen) as process, \
                patch("collect_architecture.gate_wait_is_interactive", return_value=True), \
                patch("collect_architecture.input", return_value="") as mocked_input, redirect_stdout(io.StringIO()):
                status = invoke_spec(
                    spec, root=root, manifest=manifest, manifest_path=root / "collection_manifest.json",
                    python=Path(sys.executable), scraper_root=root, pages=2,
                )
            self.assertEqual(status, "no_data")
            self.assertEqual(process.call_count, 2)
            mocked_input.assert_called_once()
            record = manifest["records"][0]
            self.assertEqual(record["gate_wait_count"], 1)
            self.assertIsNone(record["failure_reason"])
            self.assertFalse(record["retryable"])
            self.assertIn("--merge", process.call_args_list[1].args[0])
            self.assertIn("joblist 响应不是有效 JSON", Path(record["log"]).read_text(encoding="utf-8"))
            status, _record, console, process = self._run_mock_collection(root, "", [], manifest)
            self.assertEqual(status, "no_data")
            self.assertIn("SKIP ", console)
            process.assert_not_called()

    def test_gate_waits_for_enter_retries_current_combination(self):
        spec = self._invoke_spec_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "collection_manifest.json"
            manifest = {"records": []}
            calls = []

            class FakeProcess:
                def __init__(self, lines, returncode):
                    self.stdout = iter(lines)
                    self.returncode = returncode

                def wait(self):
                    return self.returncode

                def poll(self):
                    return self.returncode

                def terminate(self):
                    self.returncode = 130

            def fake_popen(command, **_kwargs):
                calls.append(command)
                if len(calls) == 1:
                    return FakeProcess(["请完成安全验证\n"], 1)
                output_path = Path(command[command.index("--output") + 1])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps({"jobs": [{"job_id": "1"}]}), encoding="utf-8")
                return FakeProcess(["完成: 1 条\n"], 0)

            with patch("collect_architecture.subprocess.Popen", side_effect=fake_popen), \
                patch("collect_architecture.gate_wait_is_interactive", return_value=True), \
                patch("collect_architecture.input", return_value="") as mocked_input:
                status = invoke_spec(
                    spec,
                    root=root,
                    manifest=manifest,
                    manifest_path=manifest_path,
                    python=Path(sys.executable),
                    scraper_root=root,
                    pages=1,
                    verbose_upstream=False,
                )

            self.assertEqual(status, "success")
            self.assertEqual(len(calls), 2)
            mocked_input.assert_called_once()
            record = manifest["records"][0]
            self.assertEqual(record["gate_wait_count"], 1)
            self.assertIsNotNone(record["last_gate_at"])
            self.assertEqual(record["status"], "success")
            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["records"][0]["status"], "success")

    def test_gate_q_keeps_combination_paused_without_retry(self):
        spec = self._invoke_spec_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "collection_manifest.json"
            manifest = {"records": []}
            calls = []

            class FakeProcess:
                stdout = iter(["登录状态异常\n"])

                def wait(self):
                    return 1

                def poll(self):
                    return 1

                def terminate(self):
                    return None

            with patch("collect_architecture.subprocess.Popen", side_effect=lambda *args, **kwargs: calls.append(args) or FakeProcess()), \
                patch("collect_architecture.gate_wait_is_interactive", return_value=True), \
                patch("collect_architecture.input", return_value="q") as mocked_input:
                status = invoke_spec(
                    spec,
                    root=root,
                    manifest=manifest,
                    manifest_path=manifest_path,
                    python=Path(sys.executable),
                    scraper_root=root,
                    pages=1,
                    verbose_upstream=False,
                )

            self.assertEqual(status, "paused")
            self.assertEqual(len(calls), 1)
            mocked_input.assert_called_once()
            record = manifest["records"][0]
            self.assertEqual(record["status"], "paused")
            self.assertEqual(record["gate_wait_count"], 1)
            self.assertEqual(record["gate_wait_result"], "quit")

    def test_gate_wait_flags_default_to_enabled_and_can_be_disabled(self):
        self.assertTrue(parse_args([]).wait_on_gate)
        self.assertFalse(parse_args(["--no-wait-on-gate"]).wait_on_gate)

    def test_browser_setup_options_are_parsed(self):
        args = parse_args([
            "--browser", "edge",
            "--cdp-port", "9223",
            "--profile-dir", "edge-profile",
            "--setup-browser",
            "--setup-only",
        ])
        self.assertEqual(args.browser, "edge")
        self.assertEqual(args.cdp_port, 9223)
        self.assertEqual(args.profile_dir, Path("edge-profile"))
        self.assertTrue(args.setup_browser)
        self.assertTrue(args.setup_only)

    def test_gui_owner_identity_is_token_bound_and_precedes_config_or_manifest_access(self):
        token = "a" * 32
        output = io.StringIO()
        with patch.dict(os.environ, {"BOSS_COLLECTION_DESKTOP_OWNER_TOKEN": token}), \
                patch("collect_architecture.load_config", side_effect=ValueError("offline early stop")), \
                redirect_stdout(output), self.assertRaisesRegex(ValueError, "offline early stop"):
            main(["--gui-interactive"])
        self.assertEqual(output.getvalue(), f"DESKTOP_COLLECTOR_OWNER token={token} pid={os.getpid()}\n")
        for gui_args, value in (([], token), (["--gui-interactive"], "invalid-token")):
            with self.subTest(gui_args=gui_args, value=value), \
                    patch.dict(os.environ, {"BOSS_COLLECTION_DESKTOP_OWNER_TOKEN": value}), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--dry-run", "--limit", "1", *gui_args]), 0)
            self.assertNotIn("DESKTOP_COLLECTOR_OWNER", output.getvalue())

    def test_setup_only_prepares_browser_and_never_collects(self):
        for setup_flag in ([], ["--setup-browser"], ["--setup-chrome"]):
            for setup_exit in (0, 7):
                with self.subTest(setup_flag=setup_flag, setup_exit=setup_exit), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    scraper_root = root / "scraper"
                    profile = root / "profile"
                    python = Path(sys.executable)
                    with patch("collect_architecture.resolve_scraper_root", return_value=scraper_root), \
                         patch("collect_architecture.resolve_python", return_value=python), \
                         patch("collect_architecture.run_setup_browser", return_value=setup_exit) as setup, \
                         patch("collect_architecture.invoke_spec", return_value="success") as collect, \
                         redirect_stdout(io.StringIO()):
                        result = main([
                            "--root", str(root / "output"), "--limit", "1",
                            "--browser", "edge", "--cdp-port", "9223",
                            "--profile-dir", str(profile), "--setup-only", *setup_flag,
                        ])
                    setup.assert_called_once_with(python, scraper_root, "edge", 9223, profile_dir=profile)
                    collect.assert_not_called()
                    self.assertEqual(result, setup_exit)

    def test_setup_only_dry_run_stays_offline(self):
        with patch("collect_architecture.resolve_scraper_root") as resolver, \
             patch("collect_architecture.run_setup_browser") as setup, \
             patch("collect_architecture.invoke_spec") as collect, \
             redirect_stdout(io.StringIO()):
            result = main(["--setup-only", "--dry-run", "--limit", "1"])
        self.assertEqual(result, 0)
        resolver.assert_not_called()
        setup.assert_not_called()
        collect.assert_not_called()

    def test_manifest_record_persistence_merges_latest_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "collection_manifest.json"
            specs = [
                {"combination": "a", "track": "current"},
                {"combination": "b", "track": "target"},
            ]
            manifest = persist_manifest_state(
                manifest_path,
                specs=specs,
                pages=10,
                root=root,
            )
            persist_manifest_record(
                manifest_path,
                {"combination": "a", "status": "success"},
                expected_specs=specs,
                pages=10,
                root=root,
                local_manifest=manifest,
            )
            persist_manifest_record(
                manifest_path,
                {"combination": "b", "status": "success"},
                expected_specs=specs,
                pages=10,
                root=root,
                local_manifest=manifest,
            )
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {record["combination"] for record in saved["records"]},
                {"a", "b"},
            )

    def test_concurrent_cli_shards_share_full_limited_scope_and_resume_only_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "plan.json"
            config_path.write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
            specs = build_specs(self.config, limit=3)
            manifest_path = root / "collection_manifest.json"
            persist_manifest_state(manifest_path, specs=specs, pages=2, root=root)
            completed = specs[0]
            completed_path = root / "raw" / completed["track"] / completed["city_id"] / f"{completed['combination']}.json"
            completed_path.parent.mkdir(parents=True)
            completed_path.write_text(json.dumps({"jobs": [{"id": "already-done"}]}), encoding="utf-8")
            completed_record = {**completed, "status": "success", "job_count": 1, "pages_requested": 10,
                                "finished_at": "2020-01-01", "json": str(completed_path)}
            persist_manifest_record(manifest_path, completed_record)
            completed_before = completed_path.read_bytes()
            barrier = threading.Barrier(2)
            launched = []
            launch_lock = threading.Lock()

            def fake_upstream(command, **_options):
                output_path = Path(command[command.index("--output") + 1])
                port = int(command[command.index("--cdp-port") + 1])
                with launch_lock:
                    launched.append((output_path.stem, port, threading.get_ident()))
                # Both CLI instances must reach an incomplete group's upstream
                # invocation while their peer is still active.
                barrier.wait(4)
                output_path.write_text(json.dumps({"jobs": [{"id": output_path.stem}]}), encoding="utf-8")
                return FakeCollectionProcess("[1/2 页] 本页1条\n完成: fixture\n")

            commands = [["--config", str(config_path), "--root", str(root), "--limit", "3", "--pages", "2",
                         "--browser", browser, "--cdp-port", str(port), "--shard", str(index), "--total-shards", "2"]
                        for index, (browser, port) in enumerate((("chrome", 9222), ("edge", 9223)))]
            results, errors = [], []

            def run(cli_args):
                try:
                    results.append(main(cli_args))
                except Exception as caught:
                    errors.append(caught)

            with patch("collect_architecture.resolve_scraper_root", return_value=root / "scraper"), \
                    patch("collect_architecture.resolve_python", return_value=Path(sys.executable)), \
                    patch("collect_architecture.subprocess.Popen", side_effect=fake_upstream), \
                    redirect_stdout(io.StringIO()):
                workers = [threading.Thread(target=run, args=(cli_args,)) for cli_args in commands]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(10)
                self.assertFalse(any(worker.is_alive() for worker in workers))
                self.assertEqual(errors, [])
                self.assertEqual(sorted(results), [0, 0])
                self.assertEqual(len(launched), 2)
                self.assertEqual(len({worker for _combination, _port, worker in launched}), 2)
                self.assertEqual({(combination, port) for combination, port, _worker in launched},
                                 {(specs[1]["combination"], 9223), (specs[2]["combination"], 9222)})
                # The second explicit start reuses complete records and raw
                # files; any upstream invocation would hit the broken barrier.
                self.assertEqual([main(cli_args) for cli_args in commands], [0, 0])
                self.assertEqual(len(launched), 2)
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["expected_combinations"], [spec["combination"] for spec in specs])
            self.assertEqual({record["combination"] for record in saved["records"]}, set(saved["expected_combinations"]))
            self.assertTrue(all(record["status"] == "success" for record in saved["records"]))
            self.assertIn(completed_record, saved["records"])
            self.assertEqual(completed_path.read_bytes(), completed_before)

    def test_real_local_processes_merge_records_without_losing_peer_or_full_scope(self):
        script = textwrap.dedent("""
            import sys, time
            from pathlib import Path
            from collect_architecture import persist_manifest_record, persist_manifest_state, shard_specs
            root, shard = Path(sys.argv[1]), int(sys.argv[2])
            specs = [{"combination": f"offline-{index}", "track": "current"} for index in range(24)]
            manifest_path = root / "collection_manifest.json"
            persist_manifest_state(manifest_path, specs=specs, pages=2, root=root)
            (root / f"ready-{shard}").write_text("ready", encoding="utf-8")
            deadline = time.monotonic() + 5
            while not (root / f"ready-{1-shard}").exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("peer did not reach the shared manifest")
                time.sleep(0.005)
            for spec in shard_specs(specs, shard, 2):
                persist_manifest_record(manifest_path, {**spec, "status": "success", "shard": shard},
                                        expected_specs=specs, pages=2, root=root)
            persist_manifest_state(manifest_path, specs=specs, pages=2, root=root)
        """)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processes = []
            try:
                for shard in (0, 1):
                    processes.append(subprocess.Popen([sys.executable, "-u", "-c", script, str(root), str(shard)],
                                                      cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                      text=True, encoding="utf-8", errors="replace"))
                self.assertNotEqual(processes[0].pid, processes[1].pid)
                for process in processes:
                    output, _error = process.communicate(timeout=12)
                    self.assertEqual(process.returncode, 0, output)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=3)
                    if process.stdout:
                        process.stdout.close()
            saved = json.loads((root / "collection_manifest.json").read_text(encoding="utf-8"))
            expected = [f"offline-{index}" for index in range(24)]
            self.assertEqual(saved["expected_combinations"], expected)
            self.assertEqual([spec["combination"] for spec in saved["expected_combination_specs"]], expected)
            self.assertEqual({record["combination"] for record in saved["records"]}, set(expected))
            self.assertEqual(len(saved["records"]), 24)
            self.assertEqual({record["shard"] for record in saved["records"]}, {0, 1})


if __name__ == "__main__":
    unittest.main()
