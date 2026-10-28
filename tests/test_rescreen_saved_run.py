import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import process_architecture as pipeline
from rescreen_saved_run import rescreen_saved_run


class SavedRunRescreenTests(unittest.TestCase):
    def make_source(self, root):
        source = root / "old-run"
        jobs = [
            {"job_id": "product", "title": "AI产品经理", "skills": "", "salary": "12-20K", "observed_in": [{"track": "current", "city": "上海", "combination": "sample-a"}, {"track": "current", "city": "上海", "combination": "sample-b"}]},
            {"job_id": "application", "title": "AI应用工程师", "skills": "Python", "salary": "20-30K", "tags": "1-3年", "observed_in": [{"track": "current", "city": "上海", "combination": "sample-c"}]},
            {"job_id": "sales", "title": "电话销售", "skills": "陌拜 | 电话获客 | 销售指标", "salary": "8-12K", "observed_in": [{"track": "current", "city": "上海", "combination": "sample-d"}]},
        ]
        for job in jobs:
            job["job_link"] = "https://example.invalid/" + job["job_id"]
        pipeline.write_json(source / "raw_sample.json", {"metadata": {"city": "上海", "tracks": "all", "raw_record_count": 20}, "jobs": jobs})
        old_jobs = copy.deepcopy(jobs)
        for job in old_jobs:
            job.update({"score": 0, "list_excluded": True, "classification": "暂不投", "reason": "旧规则列表排除"})
        pipeline.write_json(source / "prepared" / "merged_jobs_index.json", {"jobs": old_jobs})
        details = [{"job_id": "application", "jd": "负责需求分析、系统集成、Agent开发、知识库建设、PoC交付上线和评测迭代。"}]
        pipeline.write_json(source / "prepared" / "jd_shortlists" / "details.json", details)
        pipeline.write_json(source / "prepared" / "llm_reviews.json", {"reviews": [{"job_id": "application", "status": "failed", "error": "mock failure"}]})
        return source, jobs, details

    def test_same_sample_reuses_evidence_without_network_or_source_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, jobs, details = self.make_source(root)
            original_bytes = {str(path): path.read_bytes() for path in source.rglob("*.json")}
            output = root / "new-run"
            with patch.object(pipeline, "fetch_jd") as fetch, patch.object(pipeline, "run_llm") as llm:
                summary = rescreen_saved_run(source, output, expected_size=3)
            fetch.assert_not_called()
            llm.assert_not_called()
            self.assertEqual(summary["sample_size"], 3)
            self.assertTrue(summary["same_job_ids_and_order"])
            self.assertEqual(summary["reused_jd_count"], 1)
            self.assertEqual(summary["new_llm_requests"], 0)
            self.assertEqual(summary["new_jd_requests"], 0)
            self.assertEqual(summary["reused_successful_model_reviews"], 0)
            self.assertEqual(pipeline.read_json(output / "raw_sample.json")["jobs"], jobs)
            self.assertEqual(pipeline.read_json(output / "prepared" / "jd_shortlists" / "details.json"), details)
            for path, old in original_bytes.items():
                self.assertEqual(Path(path).read_bytes(), old)
            final = pipeline.read_json(output / "prepared" / "merged_jobs_index.json")
            self.assertEqual([job["job_id"] for job in final["jobs"]], [job["job_id"] for job in jobs])
            by_id = {job["job_id"]: job for job in final["jobs"]}
            self.assertEqual(by_id["product"]["classification"], "海投池")
            self.assertFalse(by_id["product"]["jd_fetched"])
            self.assertEqual(by_id["product"]["jd_dimension_count"], 0)
            self.assertEqual(by_id["product"]["observed_in"], jobs[0]["observed_in"])
            self.assertEqual(by_id["sales"]["classification"], "暂不投")
            self.assertEqual(sum(summary["after"].values()), 3)
            comparison = pipeline.read_json(output / "screening_comparison.json")
            self.assertEqual(len(comparison["jobs"]), 3)
            self.assertEqual(comparison["newly_retained_count"], 2)
            manifest = pipeline.read_json(output / "run_manifest.json")
            self.assertEqual(manifest["classification_counts"], summary["after"])
            self.assertEqual(manifest["jd_manifest"]["pending_count"], 1)
            self.assertEqual(manifest["llm_manifest"]["status"], "not_run")

    def test_default_outputs_are_unique(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, _, _ = self.make_source(Path(temporary))
            first = rescreen_saved_run(source, expected_size=3)
            second = rescreen_saved_run(source, expected_size=3)
            self.assertNotEqual(first["output_root"], second["output_root"])
            self.assertTrue(Path(first["output_root"]).is_dir())
            self.assertTrue(Path(second["output_root"]).is_dir())

    def test_rejects_output_inside_old_run_and_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, _ = self.make_source(root)
            for output in (source, source / "nested"):
                with self.subTest(output=output), self.assertRaises(ValueError):
                    rescreen_saved_run(source, output)
            existing = root / "existing"
            existing.mkdir()
            with self.assertRaises(FileExistsError):
                rescreen_saved_run(source, existing)
            self.assertEqual(list(existing.iterdir()), [])

    def test_rejects_wrong_sample_count_or_duplicate_ids_before_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, jobs, _ = self.make_source(root)
            output = root / "new"
            with self.assertRaises(ValueError):
                rescreen_saved_run(source, output, expected_size=50)
            self.assertFalse(output.exists())
            pipeline.write_json(source / "raw_sample.json", {"jobs": [jobs[0], jobs[0]]})
            with self.assertRaises(ValueError):
                rescreen_saved_run(source, output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
