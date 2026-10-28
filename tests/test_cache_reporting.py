"""Signed review provenance is checked locally by both report entry points."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import process_architecture as processor
import review_cache as cache
import review_pipeline as pipeline
from desktop_tasks import ReviewTask, STAGES
from llm_client import CodexConfig, SIMPLE_REVIEW_FORMAT


JD = "负责方案设计、系统集成、Agent工作流、知识库建设、PoC交付和持续迭代评测。"


class CacheReportingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.job = {"job_id": "one", "title": "AI解决方案架构师", "company": "fixture",
                    "salary": "20-30K", "tags": ["1-3年"], "location": "上海",
                    "jd": JD, "jd_completeness": "complete", "observed_in": []}
        self.prompt = "Fixture-only semantic review instructions"
        config = CodexConfig(model="fixture-model", reasoning_effort="high",
                             base_url="https://fixture.test/v1", api_key="fixture-only-key")
        self.provider = cache.provider_signature(config)
        model_input = processor.model_input(self.job)
        self.review = {"job_id": "one", "status": "success",
                       "review_format": SIMPLE_REVIEW_FORMAT, "判断": "符合", "理由": "承担企业AI方案与交付。",
                       "cache_signature": cache.review_signature(model_input, self.prompt, self.provider),
                       "cache_input_signature": cache.review_input_signature(model_input, self.prompt)}
        self.legacy_review = {
            "job_id": "one", "status": "success",
            "dimensions": {name: {"present": False, "evidence": []} for name in processor.DIMENSIONS},
        }

    def analyze(self, job=None, review=None, prompt=None):
        return processor.analyze_report_job(self.job if job is None else job,
                                            self.review if review is None else review,
                                            prompt=self.prompt if prompt is None else prompt)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def cli_fixture(self, review=None):
        output = self.root / "cli"
        prepared = output / "prepared"
        self.write(prepared / "merged_jobs_index.json", {"jobs": [self.job]})
        self.write(prepared / "llm_reviews.json", {"reviews": [review or self.review]})
        self.write(prepared / "jd_shortlists/details.json", [{"job_id": "one", "jd": JD}])
        return output

    def desktop_fixture(self, review=None):
        source = self.root / "source"
        self.write(source / "raw/current/jobs.json", [])
        self.write(source / "raw/target/jobs.json", [])
        task = ReviewTask.create(source, self.root / "tasks", "cache-report")
        self.addCleanup(task._release_run)
        for stage in STAGES[:-1]:
            task.begin(stage)
            rows = [copy.deepcopy(self.job)] if stage == "import" else task.input_jobs(stage)
            for row in rows:
                row["recommended_keep"] = True
                if stage == "shortlist":
                    row["list_pass"] = True
                if stage == "llm":
                    row["llm_review"] = copy.deepcopy(review or self.review)
            task.finish(stage, rows)
            task.confirm(stage)
        return task

    def test_unchanged_signed_review_is_used_without_reading_credentials(self):
        with patch("llm_client.load_codex_config", side_effect=AssertionError("no credentials")):
            item = self.analyze()
        self.assertEqual(item["model_cache_validation"], "input_match")
        self.assertTrue(item["model_cache_input_verified"])
        self.assertEqual(item["model_status"], "success")
        self.assertEqual(item["ai_judgment"], "符合")
        self.assertEqual(item["ai_reason"], self.review["理由"])
        self.assertEqual(item["classification"], "海投池")
        self.assertNotIn("jd_dimension_count", item)

    def test_only_changed_jd_or_prompt_invalidates_semantic_conclusions(self):
        changed_jd = self.analyze(job={**self.job, "jd": JD + "负责部署大模型应用服务。"})
        self.assertEqual(changed_jd["model_cache_validation"], "input_changed")
        self.assertEqual(changed_jd["model_status"], "failed")
        self.assertIn("重跑AI", " ".join(changed_jd["verification_items"]))

        # These fields are intentionally not part of the AI semantic input;
        # changing them must not make an otherwise matching review stale.
        for field, value in (("title", "企业AI架构师"), ("salary", "22-35K"),
                             ("company", "changed"), ("location", "深圳"),
                             ("skills", ["RAG"])):
            with self.subTest(field=field):
                item = self.analyze(job={**self.job, field: value})
                self.assertEqual(item["model_cache_validation"], "input_match")
                self.assertEqual(item["model_status"], "success")
                self.assertEqual(item["ai_judgment"], "符合")
                self.assertNotIn("jd_dimension_count", item)
        self.assertEqual(self.analyze(prompt=self.prompt + " changed")["model_cache_validation"], "input_changed")

    def test_changed_validator_schema_or_message_template_invalidates_input(self):
        revised_schema = copy.deepcopy(cache.REVIEW_RESPONSE_SCHEMA)
        revised_schema["properties"]["理由"]["minLength"] = 2
        with patch.object(cache, "REVIEW_RESPONSE_SCHEMA", revised_schema):
            self.assertEqual(self.analyze()["model_cache_validation"], "input_changed")
        import llm_client
        original = llm_client.review_request_messages
        with patch.object(llm_client, "review_request_messages", side_effect=lambda job, prompt: [
                *original(job, prompt), {"role": "system", "content": "new template rule"}]):
            self.assertEqual(self.analyze()["model_cache_validation"], "input_changed")

    def test_partial_signature_cannot_be_treated_as_unsigned_legacy(self):
        for field in ("cache_signature", "cache_input_signature"):
            review = dict(self.review)
            review.pop(field)
            with self.subTest(missing=field):
                item = self.analyze(review=review)
                self.assertEqual(item["model_cache_validation"], "missing_signature")
                self.assertEqual(item["model_status"], "failed")

    def test_missing_or_partial_jd_never_uses_old_model_even_if_signed(self):
        for fields in ({"jd": "", "jd_completeness": "missing"},
                       {"jd_completeness": "partial"}):
            with self.subTest(fields=fields):
                item = self.analyze(job={**self.job, **fields})
                self.assertEqual(item["model_cache_validation"], "incomplete_jd")
                self.assertNotEqual(item["model_status"], "success")
                self.assertIsNone(item["ai_judgment"])
                self.assertTrue(item["analysis_pending"])
                self.assertNotIn("model_evidence", item)

    def test_legacy_review_stays_compatible_but_explicitly_unverified(self):
        before = copy.deepcopy(self.legacy_review)
        with patch("llm_client.load_codex_config", side_effect=AssertionError("no credentials")), \
                patch("llm_client.CodexResponsesClient", side_effect=AssertionError("no model requests")):
            item = self.analyze(review=self.legacy_review)
        self.assertEqual(self.legacy_review, before)
        self.assertEqual(item["model_cache_validation"], "legacy_unverified")
        self.assertFalse(item["model_cache_input_verified"])
        self.assertEqual(item["jd_dimension_count"], 0)
        self.assertIn("历史模型记录未按当前两字段协议验证", " ".join(item["verification"]))
        lines = "\n".join(processor._report_job_lines(item))
        self.assertIn("历史语义结论，缓存未验证", lines)
        self.assertEqual(processor.report_cache_statistics([item])["model_cache_legacy_unverified_count"], 1)

    def test_cli_report_rejects_changed_prompt_and_does_not_change_checkpoint(self):
        output = self.cli_fixture()
        checkpoint = output / "prepared/llm_reviews.json"
        before = (checkpoint.read_bytes(), checkpoint.stat().st_mtime_ns)
        with patch.object(processor, "load_prompt", return_value="changed prompt") as prompt, \
                patch("llm_client.load_codex_config", side_effect=AssertionError("no provider config")), \
                patch("llm_client.CodexResponsesClient", side_effect=AssertionError("no requests")):
            processor.report_stage(output)
        data = json.loads((output / "prepared/merged_jobs_index.json").read_text(encoding="utf-8"))
        self.assertEqual(data["jobs"][0]["model_cache_validation"], "input_changed")
        self.assertEqual(data["metadata"]["model_cache_rejected_count"], 1)
        self.assertEqual((checkpoint.read_bytes(), checkpoint.stat().st_mtime_ns), before)
        prompt.assert_called_once()

    def test_cli_report_marks_unverified_history_in_json_and_markdown(self):
        output = self.cli_fixture(self.legacy_review)
        checkpoint = output / "prepared/llm_reviews.json"
        before = checkpoint.read_bytes(), checkpoint.stat().st_mtime_ns
        with patch.object(processor, "load_prompt", return_value=self.prompt), \
                patch("llm_client.load_codex_config", side_effect=AssertionError("no credentials")), \
                patch("llm_client.CodexResponsesClient", side_effect=AssertionError("no model requests")):
            processor.report_stage(output)
        data = json.loads((output / "prepared/merged_jobs_index.json").read_text(encoding="utf-8"))
        markdown = (output / "prepared/海投分层清单.md").read_text(encoding="utf-8")
        self.assertEqual(data["metadata"]["model_cache_legacy_unverified_count"], 1)
        self.assertIn("历史语义结论，缓存未验证", markdown)
        self.assertIn("需重跑AI复核", markdown)
        self.assertEqual((checkpoint.read_bytes(), checkpoint.stat().st_mtime_ns), before)

    def test_desktop_report_uses_same_validation_without_child_or_model(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                # Each fixture is independent so confirmation/stale rules stay real.
                self.root = self.root / ("changed" if changed else "same")
                task = self.desktop_fixture()
                with patch.object(processor, "load_prompt", return_value=self.prompt + (" revised" if changed else "")) as prompt, \
                        patch.object(pipeline, "_run_child", side_effect=AssertionError("no child")), \
                        patch("llm_client.load_codex_config", side_effect=AssertionError("no provider config")):
                    snapshot = pipeline.execute_stage(task, "report")
                self.assertEqual(snapshot["stages"]["report"]["status"], "completed")
                data = json.loads((task.run_directory("report") / "prepared/merged_jobs_index.json").read_text(encoding="utf-8"))
                expected = "input_changed" if changed else "input_match"
                self.assertEqual(data["jobs"][0]["model_cache_validation"], expected)
                self.assertEqual(data["metadata"]["model_cache_rejected_count"], int(changed))
                prompt.assert_called_once()

    def test_unreadable_prompt_falls_back_safely_without_error_body(self):
        output = self.cli_fixture()
        with patch.object(processor, "load_prompt", side_effect=OSError("fixture-secret-must-not-be-saved")):
            processor.report_stage(output)
        data = json.loads((output / "prepared/merged_jobs_index.json").read_text(encoding="utf-8"))
        self.assertEqual(data["jobs"][0]["model_cache_validation"], "prompt_unavailable")
        self.assertNotIn("fixture-secret-must-not-be-saved", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
