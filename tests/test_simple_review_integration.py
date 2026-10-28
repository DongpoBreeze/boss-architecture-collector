"""Offline end-to-end checks for the two-field AI review contract.

All task data lives in temporary directories.  Provider configuration and the
model client are mocked, so these tests never read credentials or use a network.
"""

from pathlib import Path
from unittest.mock import patch

import llm_client
import review_cache
import review_pipeline as pipeline
from desktop_tasks import ReviewTask, job_id
from tests.test_review_pipeline import PipelineCase


class SimpleReviewIntegrationTests(PipelineCase):
    def test_confirmed_pending_manual_decisions_drive_report_without_changing_ai_results(self):
        self.seed_llm_input(complete=("a", "b"))
        prompt = pipeline._task_report_prompt(self.task)
        rows = []
        for job in self.task.input_jobs("llm"):
            key = job_id(job)
            review = None
            if key == "a":
                review = {"job_id": key, "status": "success", "review_format": "simple-v1",
                          "判断": "信息不足", "理由": "保留原始信息不足理由。", "cache_signature": "offline-signature",
                          "cache_input_signature": review_cache.review_input_signature(pipeline.processor.model_input(job), prompt)}
            elif key == "b":
                review = {"job_id": key, "status": "failed", "review_format": "simple-v1",
                          "error": "Offline model request failed"}
            rows.append(pipeline._analyze_ai_stage(job, review))
        self.task.begin("llm")
        self.task.finish("llm", rows, stats={"review_format": "simple-v1"})
        original = {job_id(row): row for row in self.task.jobs("llm")}
        self.task.set_keep("llm", ["a", "b"], True)
        self.task.set_keep("llm", ["d"], False)
        self.assertEqual(self.task.stage_info("llm")["pending_ids"], [])
        self.task = ReviewTask.open(self.task.path)
        self.addCleanup(self.task._release_run)
        self.task.confirm("llm")
        with patch.object(pipeline, "_run_child", side_effect=AssertionError("report started a subprocess")), \
             patch("llm_client.load_codex_config", side_effect=AssertionError("report read credentials")):
            snapshot = self.run_step("report")
        self.assertEqual(snapshot["stages"]["report"]["status"], "completed")
        stats = snapshot["stages"]["report"]["stats"]
        report = {job_id(row): row for row in self.read(Path(stats["json_path"]))["jobs"]}
        pool = self.read(Path(stats["pool_path"]))["jobs"]
        markdown = Path(stats["markdown_path"]).read_text(encoding="utf-8")
        self.assertEqual({key: row["classification"] for key, row in report.items()},
                         {"a": "海投池", "b": "海投池", "c": "暂不投", "d": "暂不投"})
        self.assertEqual({job_id(row) for row in pool}, {"a", "b"})
        self.assertEqual(stats["pending_verification_count"], 0)
        self.assertEqual(stats["pool_count"], 2)
        for key in ("a", "b", "d"):
            self.assertEqual(report[key]["report_decision_source"], "manual_llm_confirmed")
            self.assertEqual(report[key]["report_manual_keep"], key != "d")
            self.assertEqual(report[key]["model_status"], original[key]["model_status"])
            self.assertEqual(report[key].get("ai_judgment"), original[key].get("ai_judgment"))
            self.assertEqual(report[key].get("ai_reason"), original[key].get("ai_reason"))
            self.assertTrue(report[key]["analysis_pending"])
        self.assertEqual(report["a"]["ai_judgment"], "信息不足")
        self.assertEqual(report["b"]["model_status"], "failed")
        self.assertIsNone(report["d"].get("ai_judgment"))
        self.assertIn("已确认人工保留", markdown)
        self.assertIn("已确认人工剔除", markdown)
        self.assertIn("已确认人工保留（AI复核阶段）", markdown.split("以下为全部岗位详细证据。", 1)[0])
        self.assertIn("保留原始信息不足理由。", markdown)
        self.assertIn("## 海投池（2）", markdown)
        self.assertIn("## 暂不投（2）", markdown)
        self.assertIn("## 待核实（0）", markdown)

    def test_report_manual_authority_requires_a_current_confirmed_result(self):
        processor = pipeline.processor
        audit = {"status": "completed", "in_input": True, "has_result": True,
                 "confirmed": True, "pending": False, "keep": True, "manual_keep": True}
        item = {"job_id": "manual-pending", "review_format": "simple-v1", "classification": "海投池",
                "analysis_pending": True, "ai_judgment": "信息不足", "model_status": "failed",
                "review_selection": {"llm": audit}}
        effective = processor.report_decision(item)
        self.assertEqual(effective["classification"], "海投池")
        self.assertTrue(processor.report_pool_eligible(effective))
        self.assertEqual(processor.report_decision(effective), effective)
        for override in ({"confirmed": False}, {"status": "stale"}, {"status": "running"},
                         {"has_result": False}, {"in_input": False}, {"keep": False},
                         {"manual_keep": None}, {"manual_keep": 1}, {"pending": True}):
            with self.subTest(override=override):
                invalid = {**effective, "review_selection": {"llm": {**audit, **override}}}
                actual = processor.report_decision(invalid)
                self.assertEqual(actual["classification"], "待核实")
                self.assertNotIn("report_manual_keep", actual)
                self.assertNotIn("report_decision_source", actual)
                self.assertFalse(processor.report_pool_eligible(invalid))
        prior_exclusion = processor.report_decision({**item, "excluded_at": "shortlist"})
        self.assertEqual(prior_exclusion["classification"], "暂不投")
        self.assertEqual(prior_exclusion["report_decision_source"], "confirmed_exclusion")
        self.assertFalse(processor.report_pool_eligible(prior_exclusion))
        rejected = processor.report_decision({**item, "review_selection": {"llm": {
            **audit, "keep": False, "manual_keep": False}}})
        self.assertEqual(rejected["classification"], "暂不投")
        self.assertFalse(processor.report_pool_eligible(rejected))
        for judgment, expected in (("符合", "海投池"), ("不符合", "暂不投"), (None, "待核实")):
            with self.subTest(previous_manual_classification=judgment):
                invalid = {**effective, "analysis_pending": False, "ai_judgment": judgment,
                           "review_selection": {"llm": {**audit, "confirmed": False}}}
                self.assertEqual(processor.report_decision(invalid)["classification"], expected)
                self.assertEqual(processor.report_pool_eligible(invalid), judgment == "符合")

    def test_report_does_not_overrule_a_valid_simple_judgment_with_old_code_gates(self):
        job = {
            "job_id": "retained-after-manual-review",
            "title": "Python开发工程师",
            "skills": ["Python", "后端开发"],
            "salary": "10-15K",
            "experience": "5-10年",
            "jd": "职位职责：负责企业AI应用的需求分析、方案设计和交付，配合研发实施。",
            "jd_completeness": "complete",
            "classification": "暂不投",
            "reason": "旧版代码判定：薪资与经验不符，六维证据不足",
            "jd_dimensions": {"旧标记": {"present": False}},
            "ability_gaps": ["旧能力差距"],
        }
        for judgment, keep, classification in (
            ("符合", True, "海投池"),
            ("不符合", False, "暂不投"),
            ("信息不足", None, "待核实"),
        ):
            with self.subTest(judgment=judgment):
                reason = "仅按本次岗位主要职责作出判断。"
                prompt = "Offline fixture rules."
                model_input = pipeline.processor.model_input(job)
                provider = review_cache.provider_signature(llm_client.CodexConfig(api_key="fixture-only-key"))
                review = {
                    "job_id": job["job_id"], "status": "success",
                    "review_format": llm_client.SIMPLE_REVIEW_FORMAT,
                    "判断": judgment, "理由": reason,
                    "cache_signature": review_cache.review_signature(model_input, prompt, provider),
                    "cache_input_signature": review_cache.review_input_signature(model_input, prompt),
                }
                stage = pipeline._analyze_ai_stage(job, review)
                report = pipeline.processor.analyze_report_job(stage, review, prompt=prompt,
                                                                analyzer=pipeline._analyze)
                self.assertIs(stage["recommended_keep"], keep)
                self.assertIs(report["recommended_keep"], keep)
                self.assertEqual(stage["analysis_pending"], keep is None)
                self.assertEqual(report["analysis_pending"], keep is None)
                self.assertEqual(report["model_cache_validation"], "input_match")
                self.assertEqual(report["classification"], classification)
                self.assertIn(reason, report["reason"])
                for result in (stage, report):
                    self.assertNotIn("jd_dimensions", result)
                    self.assertNotIn("ability_gaps", result)
                    self.assertNotIn("旧版代码判定", result.get("reason", ""))

    def test_new_review_cache_manual_selection_and_report_share_one_decision(self):
        self.seed_llm_input()
        config = llm_client.CodexConfig(
            model="offline-fixture-model",
            reasoning_effort="high",
            base_url="https://offline-fixture.invalid/v1",
            api_key="offline-fixture-not-a-real-key",
        )
        calls = []
        prompts = []
        judgments = {"a": "符合", "b": "不符合", "d": "信息不足"}
        reasons = {
            "a": "主要负责业务需求分析、AI应用方案和交付，开发仅为协作要求。",
            "b": "主要工作是代码开发，而非需求分析或方案设计。",
            "d": "现有职责不足以确定是否承担AI产品或方案工作。",
        }

        class OfflineClient:
            def review_job(self, job, *, prompt, **_):
                key = str(job["job_id"])
                calls.append(key)
                prompts.append(prompt)
                return {"判断": judgments[key], "理由": reasons[key]}

        runtime = self.task.run_directory("llm")
        legacy = {
            "job_id": "a", "status": "success",
            "target_relevance": {"assessment": "unrelated", "evidence": []},
            "dimensions": {},
        }
        self.write(runtime / "prepared/llm_reviews.json", {"reviews": [legacy]})

        def child(task, stage, command, control, emit, monitor=None):
            self.assertEqual(stage, "llm")
            self.assertEqual(command[command.index("--conversation-size") + 1], "1")
            prompt_path = Path(command[command.index("--prompt-file") + 1])
            pipeline.processor.run_llm(
                task.run_directory(stage), workers=2,
                prompt_file=prompt_path, conversation_size=1,
            )
            if monitor:
                monitor()
            control.pause()
            return 0

        with patch.object(pipeline, "_provider_hash", return_value=review_cache.provider_signature(config)), \
             patch("llm_client.load_codex_config", return_value=config), \
             patch("llm_client.CodexResponsesClient", return_value=OfflineClient()), \
             patch.object(pipeline, "_run_child", side_effect=child) as run_child:
            paused = self.run_step("llm")
            self.assertEqual(paused["stages"]["llm"]["status"], "paused")
            self.assertCountEqual(calls, ["a", "b", "d"])
            rows = {job_id(row): row for row in self.task.jobs("llm")}
            self.assertTrue(rows["a"]["recommended_keep"])
            self.assertFalse(rows["b"]["recommended_keep"])
            self.assertIsNone(rows["d"]["recommended_keep"])
            selection = self.task.stage_info("llm")
            self.assertEqual(selection["keep_ids"], ["a"])
            self.assertEqual(selection["excluded_ids"], ["b"])
            self.assertEqual(selection["pending_ids"], ["d"])
            self.assertEqual(len(set(prompts)), 1)
            for key, row in rows.items():
                self.assertEqual(row["llm_review"]["review_format"], llm_client.SIMPLE_REVIEW_FORMAT)
                self.assertEqual(row["llm_review"]["判断"], judgments[key])
                self.assertEqual(row["llm_review"]["理由"], reasons[key])
                model_input = pipeline.processor.model_input(row)
                self.assertEqual(row["llm_review"]["cache_input_signature"],
                                 review_cache.review_input_signature(model_input, prompts[0]))
                self.assertEqual(row["llm_review"]["cache_signature"], review_cache.review_signature(
                    model_input, prompts[0], review_cache.provider_signature(config)))
                self.assertNotIn("jd_dimensions", row)
                self.assertNotIn("target_relevance", row)

            checkpoint = self.read(runtime / "prepared/llm_reviews.json")
            self.assertTrue(any(entry.get("review") == legacy for entry in checkpoint["previous_reviews"]))
            for keep in (True, False):
                with self.subTest(pending_keep=keep):
                    self.task.set_keep("llm", ["d"], keep)
                    selection = self.task.stage_info("llm")
                    self.assertEqual(selection["pending_ids"], [])
                    self.assertIn("d", selection["keep_ids"] if keep else selection["excluded_ids"])
                    self.assertEqual(selection["manual_choices"]["d"], keep)
            self.task.apply_recommendations("llm")
            self.task.set_keep("llm", ["a"], False)
            self.task = ReviewTask.open(self.task.path)
            self.addCleanup(self.task._release_run)
            self.assertEqual(self.task.stage_info("llm")["status"], "paused")
            self.assertEqual(self.task.stage_info("llm")["pending_ids"], ["d"])
            resumed = self.run_step("llm")
            self.assertEqual(resumed["stages"]["llm"]["status"], "completed")
            self.assertEqual(run_child.call_count, 1, "valid two-field results must resume without model calls")
            self.assertEqual(len(calls), 3)
            self.assertEqual(resumed["stages"]["llm"]["stats"]["llm_cache_hit_count"], 3)
            self.assertFalse(self.task.stage_info("llm")["manual_choices"]["a"])
            self.assertNotIn("a", self.task.stage_info("llm")["keep_ids"])
            self.assertEqual(self.task.stage_info("llm")["keep_ids"], [])
            self.assertEqual(set(self.task.stage_info("llm")["excluded_ids"]), {"a", "b"})
            self.assertEqual(self.task.stage_info("llm")["pending_ids"], ["d"])
            self.assertNotIn("d", self.task.stage_info("llm")["manual_choices"])
            self.assertEqual(self.task.confirm("llm"), [])

        # Reporting must use the saved snapshot, never resolve a provider or
        # turn the AI decision into a second keyword/six-dimension classifier.
        with patch.object(pipeline, "_run_child", side_effect=AssertionError("report used a subprocess")), \
             patch("llm_client.load_codex_config", side_effect=AssertionError("report read credentials")):
            report = self.run_step("report")
        self.assertEqual(report["stages"]["report"]["status"], "completed", report["stages"]["report"])
        stats = report["stages"]["report"]["stats"]
        records = self.read(Path(stats["json_path"]))["jobs"]
        by_id = {job_id(row): row for row in records}
        pool = self.read(Path(stats["pool_path"]))
        markdown = Path(stats["markdown_path"]).read_text(encoding="utf-8")
        self.assertEqual(by_id["a"]["classification"], "暂不投", "manual exclusion must remain authoritative")
        self.assertEqual(by_id["b"]["classification"], "暂不投")
        self.assertEqual(by_id["d"]["classification"], "待核实")
        self.assertIsNone(by_id["d"]["recommended_keep"])
        self.assertTrue(by_id["d"]["analysis_pending"])
        self.assertEqual(pool["jobs"], [])
        self.assertEqual(stats["pool_count"], 0)
        self.assertEqual(stats["pending_verification_count"], 1)
        self.assertEqual(sum(stats["classification_counts"].values()), len(records))
        for row in records:
            if row.get("llm_review", {}).get("review_format") == llm_client.SIMPLE_REVIEW_FORMAT:
                self.assertNotIn("jd_dimensions", row)
                self.assertNotIn("ability_gaps", row)
                rendered = "\n".join(pipeline.processor._report_job_lines(row))
                self.assertNotIn("六项", rendered)
                self.assertNotIn("能力差距", rendered)
                self.assertNotIn("模型证据", rendered)
        for name, count in stats["classification_counts"].items():
            self.assertIn(f"## {name}（{count}）", markdown)
        self.assertIn(reasons["d"], markdown)
        self.assertNotIn("六项职责", markdown)
        self.assertNotIn("能力差距", markdown)
        self.assertNotIn("模型证据", markdown)
        self.assertTrue(all(row.get("review_format") == llm_client.SIMPLE_REVIEW_FORMAT for row in records))
        reopened = ReviewTask.open(self.task.path)
        self.assertFalse(reopened.stage_info("llm")["manual_choices"]["a"])
        self.assertTrue(self.source.exists())
