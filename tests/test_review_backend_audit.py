"""Synthetic acceptance cases for the shared review backend audit fixes."""

import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import jd_parallel
import llm_client
import process_architecture as processor
import project_llm_settings
import review_pipeline as pipeline
from review_cache import matching_reviews, provider_signature, review_input_signature, review_signature
import stage_rules
from test_review_pipeline import JD, PipelineCase


class ImportAndModelInputAuditTests(unittest.TestCase):
    def test_company_names_and_objects_are_matched_without_recruiter_names(self):
        rules = {"exclude_company_keywords": ["EXAMPLE"]}
        variants = [
            {"company_name": "example公司"}, {"brand_name": "Example品牌"},
            {"companyName": "Example公司"}, {"brandName": "Example品牌"},
            {"companyFullName": "Example公司"}, {"company_info": {"companyShortName": "Example"}},
            {"company": {"name": "Example公司", "recruiter": "无关"}},
            {"company": {"brandName": "Example公司"}},
            {"field_candidates": {"company": [{"value": {"name": "Example公司"}}]}},
            {"field_candidates": {"brand_name": [{"value": "Example公司"}]}},
        ]
        for row in variants:
            with self.subTest(row=row):
                original = copy.deepcopy(row)
                self.assertFalse(stage_rules.apply_import_rules(row, rules)["import_rule_keep"])
                self.assertEqual(row, original)
        for row in ({"boss_name": "Example招聘者"},
                    {"company": {"recruiter": "Example招聘者"}},
                    {"field_candidates": {"boss_name": [{"value": "Example招聘者"}]}}):
            self.assertTrue(stage_rules.apply_import_rules(row, rules)["import_rule_keep"])

    def test_raw_delivery_target_candidates_survive_bridge_and_repeated_merges(self):
        first = {"job_id": "a", "job_link": "https://www.zhipin.com/job_detail/FirstTarget.html", "title": "甲"}
        second = {"encrypt_job_id": "b", "job_url": "https://www.zhipin.com/job_detail/SecondTarget.html", "title": "乙"}
        bridge = {"job_id": "a", "encrypt_job_id": "b", "company": "公司"}
        raw = [first, second, bridge]
        before = copy.deepcopy(raw)
        merged, _ = processor.merge_rows(raw)
        self.assertEqual(len(merged), 1)
        candidates = merged[0]["delivery_identity_candidates"]
        self.assertIn({"job_link": first["job_link"]}, candidates)
        self.assertIn({"encrypt_job_id": "b", "job_url": second["job_url"]}, candidates)
        self.assertIn({"encrypt_job_id": "b"}, candidates)
        self.assertTrue(all(set(value) <= set(processor._DELIVERY_IDENTITY_FIELDS) for value in candidates))
        for _ in range(3):
            again, _ = processor.merge_rows([*merged, *merged, bridge])
            self.assertEqual(again[0]["delivery_identity_candidates"], candidates)
            merged = again
        self.assertEqual(raw, before)
        unkeyed, _ = processor.merge_rows([{"title": "无定位岗位"}])
        self.assertEqual(unkeyed[0]["delivery_identity_candidates"], [])

    def test_bracketed_and_chinese_numbered_sections_preserve_only_duties_and_requirements(self):
        for heading in ("【岗位职责】", "【岗位职责】：", "一、岗位职责", "（一）岗位职责", "一.岗位职责",
                        "(一) 工作职责", "1. 岗位职责", "## 【工作内容】"):
            with self.subTest(heading=heading):
                jd = ("【公司介绍】\n公司经营多年，主营数据服务。\n" + heading +
                      "\n面向业务的智能体编排与系统交付。\n二、任职要求\n熟悉 Python。\n"
                      "（三）福利待遇\n六险一金，带薪年假。")
                value = processor.model_input({"job_id": "duties", "jd": jd})
                self.assertIn("智能体编排与系统交付", value["responsibilities"])
                self.assertIn("Python", value["requirements"])
                for excluded in ("经营多年", "六险一金", "带薪年假", "Python"):
                    self.assertNotIn(excluded, value["responsibilities"])
                self.assertNotIn("六险一金", json.dumps(value, ensure_ascii=False))
        inline = processor.model_input({"job_id": "x", "jd": "【岗位职责】负责方案交付。\n【福利待遇】带薪休假。"})
        self.assertEqual(inline["responsibilities"], "负责方案交付。")

    def test_explicit_missing_or_partial_never_upgrades_residual_text(self):
        self.assertEqual(jd_parallel.detail_completeness({"jd": JD}), "complete")
        for field in ("status", "jd_status", "jd_completeness"):
            self.assertEqual(jd_parallel.detail_completeness({"jd": JD, field: "missing"}), "missing")
        self.assertEqual(jd_parallel.detail_completeness({"jd": JD, "jd_completeness": "partial"}), "partial")


class BrowserCheckpointAuditTests(unittest.TestCase):
    def test_missing_side_records_cannot_complete_and_input_order_controls_odd_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "details.json"
            processor.write_json(output.with_name("details.chrome.json"), [{"job_id": "z", "jd": JD}])
            processor.write_json(output.with_name("details.edge.json"), [{"job_id": "b", "jd": JD}])
            result = processor.browser_detail_checkpoint_status(output, ["z", "a", "b", "c"])
            self.assertEqual(result["chrome"]["expected_job_ids"], ["a", "z"])
            self.assertEqual(result["chrome"]["status"], "partial")
            self.assertEqual(result["edge"]["status"], "partial")
            self.assertEqual(result["edge"]["missing_job_ids"], ["c"])
            result = processor.browser_detail_checkpoint_status(output, ["z", "a", "b"])
            self.assertEqual(result["chrome"]["total"], 2)
            self.assertEqual(result["edge"]["expected_job_ids"], ["b"])
            self.assertEqual(result["edge"]["status"], "completed")
            result = processor.browser_detail_checkpoint_status(output, ["z"])
            self.assertEqual(result["edge"]["status"], "completed")
            self.assertEqual(result["edge"]["total"], 0)
            self.assertEqual(result["edge"]["job_ids"], [])

    def test_batch_aggregation_keeps_earlier_failures_and_ignores_unrelated_history(self):
        inputs = [{"job_id": key} for key in "abcde"]
        details = {key: {"job_id": key, "jd": JD} for key in "abcde"}
        manifest = {"records": [
            {"batch": 1, "job_ids": ["a", "b"], "browser_results": {"chrome": "failed", "edge": "completed"}},
            {"batch": 2, "job_ids": ["c", "d"], "browser_results": {"chrome": "completed", "edge": "completed"}},
            {"batch": 3, "job_ids": ["e"], "browser_results": {"chrome": "completed", "edge": "pending"}},
            {"batch": 99, "job_ids": ["unrelated"], "browser_results": {"chrome": "paused", "edge": "failed"}},
        ]}
        self.assertEqual(pipeline._jd_browser_results(inputs, details, manifest, 2, "dual"),
                         {"chrome": "failed", "edge": "completed"})
        self.assertEqual(pipeline._jd_browser_results(inputs, details, manifest, 100, "dual"),
                         {"chrome": "failed", "edge": "completed"})
        manifest["records"][0]["browser_results"]["chrome"] = "completed"
        details.pop("b")
        self.assertEqual(pipeline._jd_browser_results(inputs, details, manifest, 2, "dual"),
                         {"chrome": "completed", "edge": "pending"})
        self.assertEqual(pipeline._jd_browser_results([], {}, manifest, 2, "dual"),
                         {"chrome": "completed", "edge": "completed"})

    def test_dual_fetch_seeds_known_results_and_keeps_full_partition_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, scraper = root / "output", root / "scraper"
            folder = output / "prepared/jd_shortlists"
            processor.write_json(folder / "jd_input.json", {"jobs": [{"job_id": key} for key in "abc"]})
            processor.write_json(folder / "details.json", [{"job_id": "a", "jd": JD}])
            (scraper / "scripts").mkdir(parents=True)
            (scraper / "scripts/boss_cdp_raw.py").write_text("# synthetic", encoding="utf-8")
            calls = []

            def collect(command, **_options):
                jobs = processor.read_json(Path(command[command.index("--input") + 1]))["jobs"]
                self.assertEqual([row["job_id"] for row in jobs], list("abc"))
                checkpoint = Path(command[command.index("--detail-output") + 1])
                self.assertIn("a", {row["job_id"] for row in processor.read_json(checkpoint)})
                calls.append(jobs)
                keys = "ac" if len(calls) == 1 else "abc"
                processor.write_json(checkpoint, [{"job_id": key, "jd": JD} for key in keys])
                processor.write_json(checkpoint.with_name("details_0001.chrome.json"),
                                     [{"job_id": key, "jd": JD} for key in keys if key in "ab"])
                processor.write_json(checkpoint.with_name("details_0001.edge.json"), [{"job_id": "c", "jd": JD}])
                return subprocess.CompletedProcess(command, 0, "synthetic-complete", "")

            with patch.object(processor, "ensure_detail_cdp_ready", return_value=(True, None)), \
                    patch.object(processor, "run_detail_subprocess", side_effect=collect), redirect_stdout(io.StringIO()):
                first = processor.fetch_jd(output, scraper, batch_size=3, python_path=sys.executable, browser_mode="dual")
                second = processor.fetch_jd(output, scraper, batch_size=3, python_path=sys.executable, browser_mode="dual")
            sides = first["manifest"]["records"][0]["browser_results"]
            self.assertEqual((sides["chrome"]["status"], sides["edge"]["status"]), ("partial", "completed"))
            self.assertEqual(sides["chrome"]["expected_job_ids"], ["a", "b"])
            self.assertEqual(second["manifest"]["records"][0]["status"], "success")
            self.assertEqual(len(calls), 2)

    def test_prepare_and_execute_reject_isolation_before_any_browser_work(self):
        for options in ({"chrome_port": 9444, "edge_port": 9444},
                        {"chrome_profile_dir": "same-profile", "edge_profile_dir": "same-profile/../same-profile"},
                        {"chrome_port": True}):
            with self.subTest(options=options), patch.object(pipeline, "_run_child") as child, \
                    patch.object(processor, "resolve_python") as python:
                with self.assertRaises(ValueError):
                    pipeline.prepare_browser(Path("does-not-exist"), browser_mode="dual", **options)
                child.assert_not_called()
                python.assert_not_called()
                with self.assertRaises(ValueError):
                    processor.fetch_jd(Path("does-not-exist"), Path("does-not-exist"), browser_mode="dual", **options)
                python.assert_not_called()


class PipelineAuditTests(PipelineCase):
    def test_jd_stage_aggregates_all_batches_instead_of_showing_only_last_success(self):
        self.import_and_confirm()
        self.shortlist_and_confirm()
        ids = [row["job_id"] for row in self.task.input_jobs("fetch-jd")]
        manifest = {"records": [
            {"batch": 1, "job_ids": ids[:2], "missing_job_ids": ids[:1], "status": "failed", "exit_code": 9,
             "browser_results": {"chrome": {"status": "failed"}, "edge": {"status": "completed"}}},
            {"batch": 2, "job_ids": ids[2:], "status": "success", "exit_code": 0,
             "browser_results": {"chrome": {"status": "completed"}, "edge": {"status": "pending"}}},
        ]}
        with patch.object(pipeline, "_run_child", side_effect=self.fake_jd(
                [{"job_id": key, "jd": JD} for key in ids[1:]], manifest)):
            info = self.run_step("fetch-jd", scraper_root=self.scraper, batch_size=2, browser_mode="dual")["stages"]["fetch-jd"]
        self.assertEqual(info["status"], "failed")
        self.assertEqual(info["stats"]["browser_results"], {"chrome": "failed", "edge": "completed"})

    def test_invalid_source_files_are_in_stats_issues_and_log_without_discarding_valid_rows(self):
        corrupt = self.source / "raw/current/shanghai/current__shanghai__broken__20_50__1_3.json"
        corrupt.write_text('{"jobs": [broken}', encoding="utf-8")
        self.task = self.create()
        info = self.run_step("import")["stages"]["import"]
        self.assertEqual(info["status"], "completed")
        self.assertEqual(info["stats"]["selected_job_count"], 4)
        self.assertEqual(info["stats"]["invalid_file_count"], 1)
        self.assertEqual(info["stats"]["import_warning_count"], 1)
        self.assertEqual(info["stats"]["invalid_files"], [corrupt.relative_to(self.source).as_posix()])
        self.assertTrue(any(corrupt.name in issue for issue in info["issues"]))
        self.assertIn(corrupt.name, self.task.path.joinpath("task.log").read_text(encoding="utf-8"))
        self.assertEqual(len(self.task.jobs("import")), 4)

    def test_config_change_during_request_uses_one_snapshot_then_invalidates_on_next_run(self):
        self.seed_llm_input()
        settings_path = project_llm_settings.default_settings_path()
        project_llm_settings.save_settings("https://first.fixture.test/v1", "first-model", "high", path=settings_path)
        secret = "synthetic-private-key-never-persist"
        seen, contexts, commands = [], [], []

        def child(task, stage, command, control, emit, monitor=None):
            runtime = task.run_directory(stage)
            cfg = command.review_config
            seen.append(cfg)
            commands.append(command)
            context = self.read(runtime / "review_cache_context.json")
            contexts.append(context)
            current_info = task.manifest["stages"][stage]
            for field in ("attempt", "input_revision", "input_ids"):
                self.assertEqual(context[field], current_info[field])
            self.assertTrue(context["review_prompt_file"].startswith("runtime/llm/review_prompts/"))
            prompt_path = task.path / context["review_prompt_file"]
            prompt = prompt_path.read_text(encoding="utf-8")
            self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(), prompt_path.stem)
            self.assertEqual(context["provider"], provider_signature(cfg))
            self.assertIn("--config-stdin", command)
            self.assertNotIn(secret, repr(command))
            with self.assertRaises(FrozenInstanceError):
                cfg.model = "mutation"
            # The settings change happens after the parent's signature and
            # context were published, before the child's model request.
            project_llm_settings.save_settings("https://second.fixture.test/v1", "second-model", "max", path=settings_path)
            with redirect_stdout(io.StringIO()):
                processor.run_llm(runtime, workers=2, resolved_config=cfg,
                                  prompt_file=Path(command[command.index("--prompt-file") + 1]))
            if monitor:
                monitor()
            payload = self.read(runtime / "prepared/llm_reviews.json")
            hashes = {job_id: review_signature(processor.model_input(job), prompt, context["provider"])
                      for job in task.input_jobs(stage) for job_id in [job["job_id"]]}
            input_hashes = {job["job_id"]: review_input_signature(processor.model_input(job), prompt)
                            for job in task.input_jobs(stage)}
            self.assertEqual(len(matching_reviews(payload, hashes, input_signatures=input_hashes)), 3)
            return 0

        original_load = project_llm_settings.load_settings
        with patch("project_llm_settings.load_api_key", return_value=secret), \
                patch("project_llm_settings.load_settings", wraps=original_load) as load_settings, \
                patch("llm_client.CodexResponsesClient") as client, \
                patch.object(pipeline, "_run_child", side_effect=child) as runner:
            client.return_value.review_job.return_value = {"判断": "符合", "理由": "方案与系统交付。"}
            first = self.run_step("llm")["stages"]["llm"]
            self.assertEqual((first["stats"]["llm_success_count"], first["stats"]["model"], first["stats"]["reasoning_effort"]),
                             (3, "first-model", "high"))
            self.assertEqual(load_settings.call_count, 1)
            second = self.run_step("llm")["stages"]["llm"]
            self.assertEqual((second["stats"]["llm_success_count"], second["stats"]["model"]), (3, "second-model"))
            self.assertEqual(second["stats"]["llm_cache_hit_count"], 0)
            self.assertEqual(second["stats"]["llm_cache_invalidated_count"], 3)
            self.assertEqual(load_settings.call_count, 2)
            self.assertEqual(runner.call_count, 2)
        self.assertEqual([cfg.model for cfg in seen], ["first-model", "second-model"])
        self.assertNotEqual(contexts[0]["provider"], contexts[1]["provider"])
        for path in self.task.path.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), str(path))

    def test_legacy_full_jd_is_eligible_but_explicit_missing_text_is_not(self):
        self.seed_llm_input()
        original_inputs = self.task.input_jobs

        def inputs(stage):
            rows = original_inputs(stage)
            if stage == "llm":
                rows[0].pop("jd_completeness")
                rows[1]["jd_completeness"] = "missing"
            return rows

        with patch.object(self.task, "input_jobs", side_effect=inputs), \
                patch.object(pipeline, "_run_child", side_effect=self.fake_reviews()):
            info = self.run_step("llm")["stages"]["llm"]
        self.assertEqual(info["stats"]["llm_candidate_count"], 2)
        self.assertEqual(info["stats"]["llm_success_count"], 2)

    def test_private_stdin_transports_config_without_credential_in_argv_or_artifacts(self):
        cfg = llm_client.CodexConfig(model="synthetic-model", api_key="synthetic-pipe-secret")
        child_code = ("import json,sys; value=json.load(sys.stdin); "
                      "assert value['model']=='synthetic-model'; assert len(value['api_key'])==21; "
                      "print('private-snapshot-ok',flush=True)")
        command = pipeline._ReviewCommand([sys.executable, "-u", "-c", child_code], cfg)
        self.assertNotIn(cfg.api_key, json.dumps(command))
        events = []
        code = pipeline._run_child(None, "llm", command, pipeline.StageControl(),
                                   pipeline._Emitter(None, "llm", lambda kind, value: events.append((kind, value))))
        self.assertEqual(code, 0)
        self.assertIn("private-snapshot-ok", str(events))
        self.assertNotIn(cfg.api_key, str(events))
        decoded = llm_client.config_from_private_payload(cfg.private_payload())
        self.assertEqual(decoded, cfg)
        self.assertNotIn(cfg.api_key, repr(decoded))

    def test_private_stdin_cli_forwards_config_without_loading_current_provider(self):
        cfg = llm_client.CodexConfig(model="stdin-model", api_key="synthetic-stdin-secret")
        with patch.object(processor, "run_llm") as run, \
                patch.object(processor.sys, "stdin", io.StringIO(cfg.private_payload())), \
                patch("llm_client.load_review_config", side_effect=AssertionError("snapshot must not reload")), \
                redirect_stdout(io.StringIO()) as output:
            code = processor.main(["--root", str(self.source), "--stage", "llm", "--config-stdin",
                                   "--output-root", str(self.folder / "cli-output")])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["resolved_config"], cfg)
        self.assertNotIn(cfg.api_key, output.getvalue())
        for invalid in ("{}", "[]", "not json", "x" * 65537):
            with self.assertRaises(llm_client.ConfigurationError):
                llm_client.config_from_private_payload(invalid)


if __name__ == "__main__":
    unittest.main()
