"""Offline integration checks for the fixed-task, manually reviewed pipeline."""

import copy
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import project_llm_settings
import review_pipeline as pipeline
import desktop_tasks as task_store
from desktop_tasks import ReviewTask, STAGES, TaskStateError, job_id


JD = (
    "负责业务需求分析与技术方案设计，确定系统边界；使用RAG与Agent工作流，"
    "建设企业知识库，完成系统集成、PoC验证与项目交付，"
    "持续迭代优化并制定评测指标。"
)


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        project_settings = self.folder / "missing-project-llm.json"
        self.project_settings_patcher = patch(
            "project_llm_settings.default_settings_path", return_value=project_settings
        )
        self.project_settings_patcher.start()
        self.addCleanup(self.project_settings_patcher.stop)
        self.source = self.folder / "source"
        self.config = PROJECT_ROOT / "config" / "architecture_collection.json"
        self.original = [
            {"job_id": "a", "title": "AI应用架构师", "company": "甲公司", "salary": "20-30K",
             "skills": ["Agent", "RAG", "系统集成"], "tags": ["1-3年", "本科"]},
            {"job_id": "b", "title": "解决方案工程师", "company": "乙公司", "salary": "12-20K",
             "skills": ["技术方案", "项目交付"], "tags": ["1-3年", "本科"]},
            {"job_id": "c", "title": "电话销售", "company": "丙公司", "salary": "10-15K",
             "skills": ["电话邀约", "陌拜", "销售业绩"], "tags": ["经验不限"]},
            {"job_id": "d", "title": "AI产品经理", "company": "丁公司", "salary": "20-35K",
             "skills": ["Agent", "需求分析", "项目交付"], "tags": ["3-5年", "本科"]},
        ]
        for item in self.original:
            item.update(location="上海", job_link=f"https://www.zhipin.com/job_detail/{item['job_id']}.html")
        self.write(self.source / "raw/current/shanghai/current__shanghai__aiarch__20_50__1_3.json",
                   {"city": "上海", "filters": {"salary": "20-50K", "experience": "1-3年", "degree": "203"},
                    "jobs": self.original[:3]})
        self.write(self.source / "raw/target/shanghai/target__shanghai__aiarch__20_50__3_5.json",
                   {"city": "上海", "filters": {"salary": "20-50K", "experience": "3-5年", "degree": "203"},
                    "jobs": [self.original[0], self.original[3]]})
        self.write(self.source / "raw/current/shenzhen/current__shenzhen__aiarch__20_50__1_3.json",
                   {"city": "深圳", "jobs": [{"job_id": "other-city", "title": "AI应用工程师"}]})
        self.task = self.create()
        self.scraper = self.folder / "scraper"
        self.scraper.joinpath("scripts").mkdir(parents=True)
        self.scraper.joinpath("scripts/boss_cdp_raw.py").write_text("# mock only\n", encoding="utf-8")
        self.events = []

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def read(path):
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def create(self, **options):
        kwargs = {"city": "上海", "track": "all", "sample_size": 50, "config_path": self.config}
        kwargs.update(options)
        task = ReviewTask.create(self.source, self.folder / "tasks", "上海审核测试", **kwargs)
        self.addCleanup(task._release_run)
        return task

    def run_step(self, stage, **kwargs):
        return pipeline.execute_stage(self.task, stage, on_event=lambda k, v: self.events.append((k, v)), **kwargs)

    def import_and_confirm(self, exclude=()):
        self.run_step("import")
        if exclude:
            self.task.set_keep("import", list(exclude), False)
        self.task.confirm("import")

    def shortlist_and_confirm(self, restore=()):
        self.run_step("shortlist")
        if restore:
            self.task.set_keep("shortlist", list(restore), True)
        self.task.confirm("shortlist")

    def seed_llm_input(self, complete=("a", "b", "d"), partial=()):
        self.import_and_confirm()
        self.shortlist_and_confirm()
        self.task.begin("fetch-jd")
        jobs = self.task.input_jobs("fetch-jd")
        for row in jobs:
            row.update(jd=JD if row["job_id"] in (*complete, *partial) else "",
                       jd_completeness="complete" if row["job_id"] in complete else "partial" if row["job_id"] in partial else "missing")
        self.task.finish("fetch-jd", jobs)
        self.task.confirm("fetch-jd")

    def fake_jd(self, records, manifest=None, code=0):
        def child(task, stage, command, control, emit, monitor=None):
            self.assertEqual(stage, "fetch-jd")
            folder = task.run_directory(stage) / "prepared/jd_shortlists"
            self.write(folder / "details.json", records)
            self.write(folder / "details_manifest.json", manifest or {"records": []})
            if monitor:
                monitor()
            return code
        return child

    def fake_reviews(self, status="success", configuration_failed=False):
        def child(task, stage, command, control, emit, monitor=None):
            self.assertEqual(stage, "llm")
            self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-luna")
            self.assertEqual(command[command.index("--reasoning-effort") + 1], "max")
            runtime = task.run_directory(stage)
            rows = self.read(runtime / "prepared/jd_shortlists/jd_input.json")["jobs"]
            context = self.read(runtime / "review_cache_context.json")
            from review_cache import review_input_signature
            prompt = pipeline.processor.load_prompt()
            reviews = [] if configuration_failed else [
                {"job_id": job_id(j), "status": status,
                 "review_format": "simple-v1", "判断": "符合", "理由": "承担业务分析、方案设计与交付。",
                 "cache_signature": context["inputs"][job_id(j)],
                 "cache_input_signature": review_input_signature(pipeline.processor.model_input(j), prompt)}
                for j in rows]
            checkpoint_path = runtime / "prepared/llm_reviews.json"
            previous_payload = self.read(checkpoint_path) if checkpoint_path.exists() else {}
            self.write(checkpoint_path, {
                "reviews": reviews,
                "previous_reviews": previous_payload.get("previous_reviews", []),
            })
            self.write(runtime / "prepared/llm_manifest.json", {
                "status": "configuration_failed" if configuration_failed else "completed"})
            if monitor:
                monitor()
            return 0
        return child


class LocalStepsTests(PipelineCase):
    def test_import_uses_fixed_shanghai_scope_balanced_unique_jobs_and_all_sources(self):
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.source.rglob("*.json")}
        with patch.object(pipeline, "_run_child", side_effect=AssertionError("unexpected network step")):
            snapshot = self.run_step("import")
        jobs = self.task.jobs("import")
        self.assertEqual({job_id(j) for j in jobs}, set("abcd"))
        self.assertEqual(len(next(j for j in jobs if job_id(j) == "a")["observed_in"]), 2)
        stats = snapshot["stages"]["import"]["stats"]
        self.assertEqual(stats["raw_record_count"], 5)
        self.assertEqual(stats["deduplicated_job_count"], 4)
        self.assertEqual(stats["selected_job_count"], 4)
        self.assertFalse(snapshot["stages"]["import"]["confirmed"])
        self.assertEqual(snapshot["stages"]["shortlist"]["status"], "pending")
        self.assertEqual(self.task.input_jobs("shortlist"), [])
        self.assertEqual(before, {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.source.rglob("*.json")})

    def test_confirmed_ids_alone_enter_shortlist_and_cross_target_salary_is_not_removed(self):
        self.import_and_confirm(exclude=("a",))
        snapshot = self.run_step("shortlist")
        self.assertEqual({job_id(j) for j in self.task.jobs("shortlist")}, set("bcd"))
        info = snapshot["stages"]["shortlist"]
        self.assertIn("b", info["keep_ids"])
        b = next(j for j in self.task.jobs("shortlist") if job_id(j) == "b")
        self.assertEqual(b["salary_flags"], ["薪资低于目标", "需核实固定薪资"])
        self.assertIn("c", info["excluded_ids"])
        self.assertEqual(snapshot["stages"]["fetch-jd"]["status"], "pending")

    def test_shortlist_excludes_salary_ceiling_below_target_and_confirmed_jd_input_omits_it(self):
        self.original[1]["salary"] = "12-16K"
        path = self.source / "raw/current/shanghai/current__shanghai__aiarch__20_50__1_3.json"
        payload = self.read(path)
        payload["jobs"] = self.original[:3]
        self.write(path, payload)
        self.task = self.create()
        before = path.read_bytes()
        self.import_and_confirm()
        result = self.run_step("shortlist")
        info = result["stages"]["shortlist"]
        self.assertIn("b", info["excluded_ids"])
        b = next(j for j in self.task.jobs("shortlist") if job_id(j) == "b")
        self.assertFalse(b["recommended_keep"])
        self.assertTrue(b["list_salary_below_target"])
        self.assertIn("薪资上限低于17K目标", b["list_exclusion_reasons"])
        self.task.confirm("shortlist")
        self.assertNotIn("b", {job_id(j) for j in self.task.input_jobs("fetch-jd")})
        self.assertEqual(path.read_bytes(), before)

    def test_preconfirmed_stage_cannot_be_run_out_of_order(self):
        with self.assertRaises(TaskStateError):
            self.run_step("llm")

    def test_stop_request_prevents_local_work_and_keeps_checkpoint(self):
        control = pipeline.StageControl()
        control.stop()
        with patch.object(pipeline.processor, "collect_rows", side_effect=AssertionError("should stop first")):
            result = self.run_step("import", control=control)
        self.assertEqual(result["stages"]["import"]["status"], "stopped")
        self.assertFalse(result["stages"]["import"]["confirmed"])
        self.run_step("import")
        self.assertEqual(self.task.stage_info("import")["status"], "completed")

    def test_bad_worker_limits_do_not_mutate_task(self):
        for workers in (0, 33, True):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                self.run_step("import", workers=workers)
        for jd_workers in (0, 33, True):
            with self.subTest(jd_workers=jd_workers), self.assertRaises(ValueError):
                self.run_step("import", jd_workers=jd_workers)
        self.assertEqual(self.task.stage_info("import")["status"], "pending")

    def test_bad_callback_cannot_change_a_committed_stage_to_failed(self):
        snapshot = pipeline.execute_stage(self.task, "import", on_event=Mock(side_effect=RuntimeError("UI gone")))
        self.assertEqual(snapshot["stages"]["import"]["status"], "completed")
        self.assertTrue(self.task.path.joinpath("task.log").is_file())

    def test_live_child_in_any_step_blocks_checkpoint_replacement(self):
        runtime = self.task.run_directory("llm")
        self.write(runtime / "child_lease.json", {"active": True, "pid": os.getpid()})
        with patch.object(pipeline.processor, "collect_rows") as rows:
            result = self.run_step("import")
        rows.assert_not_called()
        self.assertEqual(result["stages"]["import"]["status"], "pending")
        self.assertEqual(result["stages"]["import"]["attempt"], 0)
        self.assertIn("旧子进程", "".join(str(value) for kind, value in self.events if kind == "log"))

    def test_empty_selection_does_not_prepare_browser_read_model_or_launch_children(self):
        self.import_and_confirm(exclude=tuple("abcd"))
        with patch.object(pipeline, "_run_child", side_effect=AssertionError("empty task launched a child")), \
             patch.object(pipeline, "_provider_hash", side_effect=AssertionError("empty task read credentials")):
            for stage in ("shortlist", "fetch-jd", "llm", "report"):
                self.run_step(stage)
                self.assertEqual(self.task.stage_info(stage)["status"], "completed")
                self.task.confirm(stage)
        report = self.task.stage_info("report")["stats"]
        self.assertEqual(report["total_jobs"], 4)
        self.assertEqual(report["pool_count"], 0)
        self.assertEqual(report["classification_counts"]["暂不投"], 4)


class LeaseFinalizationTests(PipelineCase):
    def test_finish_retries_real_task_lock_contention_before_committing(self):
        original_try = task_store._try_lease
        attempts = []
        holder = None

        def local_import(*_args):
            nonlocal holder
            holder = original_try(self.task.path / ".task.lock")
            self.assertIsNotNone(holder)
            return [{"job_id": "offline"}], {}

        def try_lease(path):
            if path.name == ".task.lock" and holder is not None:
                attempts.append(path)
                if len(attempts) == 3:
                    holder.close()
            return original_try(path)

        with patch.object(pipeline, "_run_import", side_effect=local_import), \
                patch.object(task_store, "_try_lease", side_effect=try_lease):
            result = self.run_step("import")
        self.assertGreaterEqual(len(attempts), 3)
        self.assertEqual(result["stages"]["import"]["status"], "completed")
        self.assertIsNone(self.task._run_lease)

    def test_finish_and_interrupt_lock_failure_release_only_owned_idle_run(self):
        holder = None
        original_try = task_store._try_lease

        def local_import(*_args):
            nonlocal holder
            holder = original_try(self.task.path / ".task.lock")
            self.assertIsNotNone(holder)
            return [{"job_id": "offline"}], {}

        with patch.object(pipeline, "_run_import", side_effect=local_import):
            try:
                with self.assertRaises(TaskStateError):
                    self.run_step("import")
                self.assertIsNone(self.task._run_lease)
                lease = original_try(self.task.path / ".running.lock")
                self.assertIsNotNone(lease)
                lease.close()
            finally:
                if holder is not None:
                    holder.close()
        recovered = ReviewTask.open(self.task.path)
        self.assertEqual(recovered.stage_info("import")["status"], "paused")
        recovered.begin("import")
        recovered.interrupt("import", "stopped")

    def test_persistence_failure_does_not_release_a_live_owned_child_lease(self):
        control = pipeline.StageControl()
        child = Mock()
        child.poll.return_value = None

        def failed_import(*_args):
            control._children.add(child)
            raise pipeline.PipelineError("fixture work failed")

        with patch.object(pipeline, "_run_import", side_effect=failed_import), \
                patch.object(self.task, "interrupt", side_effect=TaskStateError("fixture persistence failed")):
            with self.assertRaises(TaskStateError):
                self.run_step("import", control=control)
        self.assertIsNotNone(self.task._run_lease)
        self.assertIsNone(task_store._try_lease(self.task.path / ".running.lock"))
        other = ReviewTask.open(self.task.path)
        self.assertEqual(other.stage_info("import")["status"], "running")
        child.poll.return_value = 0
        control._cleanup_thread.join(timeout=3)
        self.assertFalse(control._cleanup_thread.is_alive())
        self.assertIsNone(self.task._run_lease)
        recovered = ReviewTask.open(self.task.path)
        self.assertEqual(recovered.stage_info("import")["status"], "paused")
        recovered.begin("import")
        recovered.interrupt("import", "stopped")


class ShortlistQueueTests(PipelineCase):
    """Queue suggestions and manual selections share one durable task truth."""

    def setUp(self):
        super().setUp()
        self.source = self.folder / "queue-source"
        rows = [
            {"job_id": "related", "title": "AI架构师", "salary": "25-50K", "skills": []},
            {"job_id": "verify", "title": "系统架构师", "salary": "20-25K", "skills": []},
            {"job_id": "sales", "title": "AI Agent大客户销售", "salary": "18-25K",
             "skills": ["电话销售", "客户开发", "陌拜", "销售业绩"]},
            {"job_id": "developer", "title": "Java", "salary": "18-25K",
             "skills": ["Java", "Spring Boot", "MySQL"]},
            {"job_id": "low", "title": "AI应用架构师", "salary": "12-16K",
             "skills": ["Agent", "系统集成"]},
            {"job_id": "edge", "title": "AI产品助理", "salary": "12-17K",
             "skills": ["B端产品", "需求分析"]},
        ]
        for row in rows:
            row.update(company="测试公司", location="上海", tags=["1-3年", "本科"],
                       job_link=f"https://www.zhipin.com/job_detail/{row['job_id']}.html")
        self.write(self.source / "raw/current/shanghai/current__shanghai__aiarch__20_50__1_3.json",
                   {"city": "上海", "filters": {"salary": "20-50K", "experience": "1-3年", "degree": "203"},
                    "jobs": rows})
        self.write(self.source / "raw/target/shanghai/target__shanghai__aiarch__20_50__3_5.json",
                   {"city": "上海", "filters": {"salary": "20-50K", "experience": "3-5年", "degree": "203"},
                    "jobs": [rows[0], rows[1]]})
        self.task = self.create(sample_size=None)

    def prepare_shortlist(self):
        self.import_and_confirm()
        return self.run_step("shortlist")

    def test_queue_counts_and_split_files_match_the_same_results_and_keep_both_eligible_queues(self):
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.source.rglob("*.json")}
        with patch.object(pipeline, "_run_child", side_effect=AssertionError("local shortlist launched a child")), \
             patch.object(pipeline, "_provider_hash", side_effect=AssertionError("local shortlist read credentials")):
            snapshot = self.prepare_shortlist()
        rows = self.task.jobs("shortlist")
        by_id = {job_id(row): row for row in rows}
        self.assertEqual(len(by_id), len(rows))
        for row in rows:
            self.assertIn(row["list_queue"], pipeline.LIST_QUEUE_LABELS)
            self.assertTrue(row["list_queue_reason"])
            self.assertEqual(row["list_pass"], row["list_queue"] in {"relevant", "verify"})
            self.assertEqual(row["recommended_keep"], row["list_pass"])
            self.assertIn("list_priority", row)
        self.assertEqual(by_id["related"]["list_queue"], "relevant")
        self.assertEqual(by_id["verify"]["list_queue"], "verify")
        self.assertEqual(by_id["edge"]["list_queue"], "relevant")
        for identity in ("sales", "developer", "low"):
            self.assertEqual(by_id[identity]["list_queue"], "excluded")
        expected = {"relevant": 2, "verify": 1, "excluded": 3}
        self.assertEqual(sum(expected.values()), len(rows))
        info = snapshot["stages"]["shortlist"]
        self.assertEqual(info["stats"]["list_queue_counts"], expected)
        self.assertEqual(set(info["keep_ids"]), {"related", "verify", "edge"})
        runtime = self.task.run_directory("shortlist") / "prepared"
        index = self.read(runtime / "merged_jobs_index.json")
        self.assertEqual(index["metadata"]["list_queue_counts"], expected)
        self.assertEqual(index["metadata"]["screening_version"], pipeline.processor.SCREENING_VERSION)
        files = {name: self.read(runtime / "jd_shortlists" / name)
                 for name in ("jd_input.json", "jd_relevant.json", "jd_verify.json")}
        for document in files.values():
            self.assertEqual(document["list_queue_counts"], expected)
            self.assertEqual(document["total"], len(document["jobs"]))
        self.assertEqual(files["jd_relevant.json"]["list_queue"], "relevant")
        self.assertEqual(files["jd_verify.json"]["list_queue"], "verify")
        relevant = {job_id(row) for row in files["jd_relevant.json"]["jobs"]}
        verify = {job_id(row) for row in files["jd_verify.json"]["jobs"]}
        combined = {job_id(row) for row in files["jd_input.json"]["jobs"]}
        self.assertFalse(relevant & verify)
        self.assertEqual(combined, relevant | verify)
        self.assertEqual(combined, set(info["keep_ids"]))
        self.assertEqual(len(by_id["related"]["observed_in"]), 2)
        self.assertEqual(snapshot["stages"]["fetch-jd"]["status"], "pending")
        self.assertEqual(before, {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.source.rglob("*.json")})

    def test_manual_queue_selection_controls_next_input_without_running_it_and_survives_rerun(self):
        self.prepare_shortlist()
        rows = self.task.jobs("shortlist")
        verify_ids = [job_id(row) for row in rows if row["list_queue"] == "verify"]
        with patch.object(pipeline, "_run_child", side_effect=AssertionError("selection launched a child")):
            self.task.set_keep("shortlist", verify_ids, False)
            self.task.confirm("shortlist")
            self.assertEqual({job_id(row) for row in self.task.input_jobs("fetch-jd")}, {"related", "edge"})
            self.assertEqual(self.task.stage_info("fetch-jd")["status"], "pending")
            self.run_step("shortlist")
            self.assertEqual(set(self.task.stage_info("shortlist")["keep_ids"]), {"related", "edge"})
            self.assertEqual(self.task.stage_info("shortlist")["manual_choices"], {"verify": False})
            # Adding the verification queue back is an explicit manual choice.
            self.task.set_keep("shortlist", verify_ids, True)
            self.assertFalse(self.task.stage_info("shortlist")["confirmed"])
            self.assertEqual(self.task.input_jobs("fetch-jd"), [])
            self.task.confirm("shortlist")
            self.assertEqual({job_id(row) for row in self.task.input_jobs("fetch-jd")}, {"related", "verify", "edge"})
            self.assertEqual(self.task.stage_info("fetch-jd")["status"], "pending")
        # A manual salary override remains traceable; adopting suggestions resets it.
        self.task.set_keep("shortlist", ["low"], True)
        self.run_step("shortlist")
        self.assertIn("low", self.task.stage_info("shortlist")["keep_ids"])
        low = next(row for row in self.task.jobs("shortlist") if job_id(row) == "low")
        self.assertEqual(low["list_queue"], "excluded")
        self.assertFalse(low["list_pass"])
        self.task.apply_recommendations("shortlist")
        self.assertNotIn("low", self.task.stage_info("shortlist")["keep_ids"])
        self.assertEqual(self.task.stage_info("shortlist")["manual_choices"], {})

    def test_empty_shortlist_persists_all_zero_queue_counts_and_empty_split_files(self):
        self.run_step("import")
        self.task.set_keep("import", [job_id(row) for row in self.task.jobs("import")], False)
        self.task.confirm("import")
        snapshot = self.run_step("shortlist")
        counts = {"relevant": 0, "verify": 0, "excluded": 0}
        self.assertEqual(snapshot["stages"]["shortlist"]["stats"]["list_queue_counts"], counts)
        folder = self.task.run_directory("shortlist") / "prepared/jd_shortlists"
        for name in ("jd_input.json", "jd_relevant.json", "jd_verify.json"):
            document = self.read(folder / name)
            self.assertEqual(document["list_queue_counts"], counts)
            self.assertEqual(document["jobs"], [])


class JDTests(PipelineCase):
    def test_jd_input_includes_manually_restored_list_reject_and_no_other_ids(self):
        self.import_and_confirm(exclude=("d",))
        self.shortlist_and_confirm(restore=("c",))
        with patch.object(pipeline, "_run_child", side_effect=self.fake_jd([
            {"job_id": "a", "jd": JD}, {"job_id": "b", "jd": JD},
            {"job_id": "c", "jd": "负责电话销售和陌拜，完成销售额指标。"},
            {"job_id": "foreign", "jd": "不得进入任务"},
        ])):
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
        runtime = self.task.run_directory("fetch-jd")
        self.assertEqual({j["job_id"] for j in self.read(runtime / "prepared/jd_shortlists/jd_input.json")["jobs"]}, set("abc"))
        self.assertEqual({j["job_id"] for j in self.read(runtime / "prepared/jd_shortlists/details.json")}, set("abc"))
        self.assertEqual(result["stages"]["fetch-jd"]["stats"]["jd_success_count"], 3)
        self.assertFalse(result["stages"]["fetch-jd"]["confirmed"])
        c = next(j for j in self.task.jobs("fetch-jd") if j["job_id"] == "c")
        self.assertNotIn("classification", c)
        self.assertNotIn("jd_dimensions", c)
        self.assertTrue(c["recommended_keep"])
        self.assertEqual(c["jd_analysis_status"], "not_run")

    def test_jd_only_acquires_bodies_without_rules_and_keeps_confirmed_inputs(self):
        self.import_and_confirm()
        self.shortlist_and_confirm(restore=("c",))
        records = [{"job_id": "a", "jd": "负责竞品整理。"},
                   {"job_id": "b", "jd": "只取得开头", "truncated": True},
                   {"job_id": "c", "jd": "纯电话销售，完成销售额指标。"}]
        with patch.object(pipeline, "_run_child", side_effect=self.fake_jd(records)), \
             patch.object(pipeline, "_analyze", side_effect=AssertionError("JD must not classify")), \
             patch.object(pipeline.processor, "rule_analyze_jd", side_effect=AssertionError("JD must not match keywords")), \
             patch.object(pipeline.processor, "score_job", side_effect=AssertionError("JD must not rescreen titles")):
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
        info = result["stages"]["fetch-jd"]
        self.assertEqual(set(info["keep_ids"]), set("abcd"))
        self.assertEqual(info["excluded_ids"], [])
        rows = self.task.jobs("fetch-jd")
        self.assertEqual({j["jd_completeness"] for j in rows}, {"complete", "partial", "missing"})
        for row in rows:
            self.assertTrue(row["recommended_keep"])
            self.assertNotIn("classification", row)
            self.assertNotIn("jd_dimension_count", row)
            self.assertEqual(row["jd_analysis_status"], "not_run")
        self.assertEqual(result["stages"]["llm"]["status"], "pending")

    def test_jd_page_concurrency_is_distinct_from_ai_workers_and_batch_size(self):
        self.import_and_confirm()
        self.shortlist_and_confirm()
        with patch.object(pipeline, "_run_child", side_effect=self.fake_jd([])) as child:
            result = self.run_step("fetch-jd", scraper_root=self.scraper,
                                   workers=10, jd_workers=7, batch_size=30)
        command = child.call_args.args[2]
        self.assertEqual(command[command.index("--workers") + 1], "10")
        self.assertEqual(command[command.index("--jd-workers") + 1], "7")
        self.assertEqual(command[command.index("--batch-size") + 1], "30")
        self.assertEqual(result["stages"]["fetch-jd"]["stats"]["jd_workers"], 7)
        self.assertEqual(result["stages"]["fetch-jd"]["stats"]["scroll_count"], 3)

    def test_fatal_adapter_batch_is_failed_even_when_wrapper_exits_zero(self):
        self.import_and_confirm()
        self.shortlist_and_confirm()
        manifest = {"records": [{"status": "failed", "exit_code": 1,
                                  "job_ids": ["a", "b", "d"], "missing_job_ids": ["a", "b", "d"]}]}
        with patch.object(pipeline, "_run_child", side_effect=self.fake_jd([], manifest, code=0)):
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
        info = result["stages"]["fetch-jd"]
        self.assertEqual(info["status"], "failed")
        self.assertEqual(info["stats"]["jd_failed_count"], 3)
        self.assertTrue(info["issues"])
        with self.assertRaises(TaskStateError):
            self.task.confirm("fetch-jd")

    def test_gate_stops_step_and_resume_keeps_completed_detail(self):
        self.import_and_confirm()
        self.shortlist_and_confirm()
        manifest = {"records": [{"status": "paused", "job_ids": ["a", "b", "d"], "missing_job_ids": ["b", "d"]}]}
        with patch.object(pipeline, "_run_child", side_effect=self.fake_jd([{"job_id": "a", "jd": JD}], manifest)):
            first = self.run_step("fetch-jd", scraper_root=self.scraper)
        self.assertEqual(first["stages"]["fetch-jd"]["status"], "waiting")
        self.assertEqual(first["stages"]["fetch-jd"]["stats"]["jd_paused_count"], 2)
        with self.assertRaises(TaskStateError):
            self.task.confirm("fetch-jd")
        def resumed(task, stage, command, control, emit, monitor):
            folder = task.run_directory(stage) / "prepared/jd_shortlists"
            self.assertEqual(self.read(folder / "details.json")[0]["job_id"], "a")
            return self.fake_jd([{"job_id": k, "jd": JD} for k in "abd"])(task, stage, command, control, emit, monitor)
        with patch.object(pipeline, "_run_child", side_effect=resumed):
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
        self.assertEqual(result["stages"]["fetch-jd"]["status"], "completed")
        self.assertEqual(result["stages"]["fetch-jd"]["stats"]["jd_success_count"], 3)

    def test_partial_and_failed_jobs_are_counted_separately(self):
        self.import_and_confirm()
        self.shortlist_and_confirm()
        with patch.object(pipeline, "_run_child", side_effect=self.fake_jd([
            {"job_id": "a", "jd": JD}, {"job_id": "b", "jd": "部分职责", "truncated": True}],
            {"records": [{"status": "partial", "job_ids": ["a", "b", "d"], "missing_job_ids": ["d"]}]})):
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
        stats = result["stages"]["fetch-jd"]["stats"]
        self.assertEqual((stats["jd_success_count"], stats["jd_partial_count"], stats["jd_failed_count"]), (1, 1, 1))
        b = next(j for j in self.task.jobs("fetch-jd") if j["job_id"] == "b")
        self.assertEqual(b["jd_completeness"], "partial")
        self.assertNotIn("classification", b)
        self.assertTrue(b["recommended_keep"])

    def test_unrelated_old_paused_batch_does_not_pause_narrower_input(self):
        self.import_and_confirm()
        self.shortlist_and_confirm()
        with patch.object(pipeline, "_run_child", side_effect=self.fake_jd(
            [{"job_id": k, "jd": JD} for k in "abd"], {"records": [{"status": "paused", "job_ids": ["not-selected"]}]})):
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
        self.assertEqual(result["stages"]["fetch-jd"]["status"], "completed")

    def test_truncated_jd_cannot_hide_a_security_gate_or_be_confirmed(self):
        self.import_and_confirm(exclude=("b", "c", "d"))
        self.shortlist_and_confirm()
        manifest = {"records": [{"status": "paused", "security_or_login_gate": True,
                                  "job_ids": ["a"], "missing_job_ids": ["a"]}]}
        with patch.object(pipeline, "_run_child", side_effect=self.fake_jd([
            {"job_id": "a", "jd": "尚未完整加载的部分正文。" * 30, "truncated": True}], manifest)):
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
        info = result["stages"]["fetch-jd"]
        self.assertEqual(info["status"], "waiting")
        self.assertEqual(info["stats"]["jd_partial_count"], 1)
        self.assertEqual(info["stats"]["jd_paused_count"], 1)
        self.assertEqual(info["stats"]["jd_paused_partial_count"], 1)
        self.assertGreaterEqual(info["stats"]["jd_pending_count"], 0)
        self.assertTrue(info["issues"])
        with self.assertRaises(TaskStateError):
            self.task.confirm("fetch-jd")

        def resumed(task, stage, command, control, emit, monitor):
            folder = task.run_directory(stage) / "prepared/jd_shortlists"
            old = self.read(folder / "details.json")[0]
            self.assertTrue(old["truncated"])
            self.assertGreater(len(old["jd"]), len(JD))
            # Keep the longer partial checkpoint, as a real resumed batch does.
            self.write(folder / "detail_batches/details_001.json", [{"job_id": "a", "jd": JD, "complete": True}])
            self.write(folder / "details_manifest.json", {"records": [{"status": "completed", "job_ids": ["a"]}]})
            monitor()
            return 0

        with patch.object(pipeline, "_run_child", side_effect=resumed):
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
        info = result["stages"]["fetch-jd"]
        self.assertEqual(info["status"], "completed")
        self.assertEqual((info["stats"]["jd_success_count"], info["stats"]["jd_partial_count"],
                          info["stats"]["jd_paused_count"]), (1, 0, 0))
        self.assertEqual(self.task.jobs("fetch-jd")[0]["jd"], JD)
        self.assertEqual(self.task.jobs("fetch-jd")[0]["jd_completeness"], "complete")
        self.task.confirm("fetch-jd")
        self.assertEqual(self.task.stage_info("llm")["status"], "pending")
        with patch.object(pipeline, "_provider_hash", return_value="provider-hash"), \
             patch.object(pipeline, "_run_child", side_effect=self.fake_reviews()):
            result = self.run_step("llm")
        self.assertEqual(result["stages"]["llm"]["stats"]["llm_candidate_count"], 1)
        self.assertEqual(result["stages"]["llm"]["stats"]["llm_success_count"], 1)

    def test_invalid_scraper_is_actionable_and_does_not_start_a_child(self):
        self.import_and_confirm()
        self.shortlist_and_confirm()
        with patch.object(pipeline, "_run_child") as child:
            result = self.run_step("fetch-jd", scraper_root=self.folder / "wrong")
        child.assert_not_called()
        self.assertEqual(result["stages"]["fetch-jd"]["status"], "failed")
        self.assertIn("boss_cdp_raw.py", result["stages"]["fetch-jd"]["issues"][-1])

    def test_complete_jd_does_not_hide_a_gate_and_resumes_without_redownloading(self):
        self.import_and_confirm(exclude=("b", "c", "d"))
        self.shortlist_and_confirm()

        def collect(command, **kwargs):
            output = Path(command[command.index("--detail-output") + 1])
            self.write(output, [{"job_id": "a", "jd": JD, "complete": True}])
            return subprocess.CompletedProcess(command, 0, "触发安全验证", "")

        def child(task, stage, command, control, emit, monitor):
            with redirect_stdout(io.StringIO()):
                pipeline.processor.fetch_jd(task.run_directory(stage), self.scraper,
                                            python_path=sys.executable, wait_on_gate=False)
            monitor()
            return 0

        with patch.object(pipeline.processor, "ensure_detail_cdp_ready", return_value=(True, None)) as cdp, \
             patch.object(pipeline.processor, "run_detail_subprocess", side_effect=collect) as collect_mock, \
             patch.object(pipeline, "_run_child", side_effect=child):
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
            info = result["stages"]["fetch-jd"]
            self.assertEqual(info["status"], "waiting")
            self.assertEqual((info["stats"]["jd_success_count"], info["stats"]["jd_paused_count"],
                              info["stats"]["jd_paused_complete_count"], info["stats"]["jd_pending_count"]), (1, 1, 1, 0))
            with self.assertRaises(TaskStateError):
                self.task.confirm("fetch-jd")
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
        collect_mock.assert_called_once()
        cdp.assert_called_once()
        self.assertEqual(result["stages"]["fetch-jd"]["status"], "completed")
        self.assertEqual(result["stages"]["fetch-jd"]["stats"]["jd_paused_count"], 0)

    def test_narrowed_selection_does_not_keep_an_obsolete_batch_gate(self):
        self.import_and_confirm(exclude=("c", "d"))
        self.shortlist_and_confirm()
        calls = []

        def collect(command, **kwargs):
            rows = self.read(Path(command[command.index("--input") + 1]))["jobs"]
            identity = rows[0]["job_id"]
            calls.append(identity)
            output = Path(command[command.index("--detail-output") + 1])
            self.write(output, [{"job_id": identity, "jd": JD, "complete": True}])
            return subprocess.CompletedProcess(command, 0, "触发安全验证" if identity == "b" else "完成", "")

        def child(task, stage, command, control, emit, monitor):
            with redirect_stdout(io.StringIO()):
                pipeline.processor.fetch_jd(task.run_directory(stage), self.scraper, batch_size=1,
                                            python_path=sys.executable, wait_on_gate=False)
            monitor()
            return 0

        with patch.object(pipeline.processor, "ensure_detail_cdp_ready", return_value=(True, None)), \
             patch.object(pipeline.processor, "run_detail_subprocess", side_effect=collect), \
             patch.object(pipeline, "_run_child", side_effect=child):
            first = self.run_step("fetch-jd", scraper_root=self.scraper, batch_size=1)
            self.assertEqual(first["stages"]["fetch-jd"]["status"], "waiting")
            folder = self.task.run_directory("fetch-jd") / "prepared/jd_shortlists"
            checkpoints = {path: path.read_bytes() for path in (folder / "detail_batches").glob("details_*.json")}
            self.task.set_keep("shortlist", ["a"], False)
            self.task.confirm("shortlist")
            for _ in range(2):
                result = self.run_step("fetch-jd", scraper_root=self.scraper, batch_size=1)
                info = result["stages"]["fetch-jd"]
                self.assertEqual(info["status"], "completed")
                self.assertEqual((info["stats"]["jd_success_count"], info["stats"]["jd_paused_count"]), (1, 0))
                self.assertEqual([job_id(job) for job in self.task.jobs("fetch-jd")], ["b"])
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(checkpoints, {path: path.read_bytes() for path in checkpoints})
        records = self.read(folder / "details_manifest.json")["records"]
        self.assertEqual([(row["batch"], row["job_ids"], row["status"]) for row in records], [(1, ["b"], "success")])


class AiStageDecisionTests(unittest.TestCase):
    def job(self, **fields):
        return {"job_id": "ai-stage", "title": "电话销售", "salary": "10-15K", "tags": ["5-10年"],
                "jd": "岗位职责：负责客户开发与合同签订。", "jd_completeness": "complete", **fields}

    def review(self, assessment="符合", **fields):
        return {"job_id": "ai-stage", "status": "success",
                "review_format": "simple-v1", "判断": assessment,
                "理由": "需要理解技术并配合开发，主要承担业务分析与方案交付。", **fields}

    def test_related_is_kept_without_code_semantics_or_a_dimension_threshold(self):
        review = self.review()
        original = copy.deepcopy(review)
        with patch.object(pipeline.processor, "merge_model_review", side_effect=AssertionError("code classifier")), \
             patch.object(pipeline.processor, "rule_analyze_jd", side_effect=AssertionError("keyword fallback")):
            item = pipeline._analyze_ai_stage(self.job(), review)
        self.assertTrue(item["recommended_keep"])
        self.assertEqual(item["llm_stage_decision"], "related")
        self.assertEqual(item["ai_judgment"], "符合")
        self.assertEqual(item["ai_reason"], review["理由"])
        self.assertEqual(item["llm_review"], original)
        self.assertEqual(review, original)
        for obsolete in ("classification", "jd_dimensions", "confidence", "ability_gaps", "target_relevance", "model_evidence"):
            self.assertNotIn(obsolete, item)

    def test_only_unrelated_rejects_and_insufficient_is_successful_but_pending(self):
        self.assertTrue(pipeline._analyze_ai_stage(self.job(), self.review())["recommended_keep"])
        unrelated = pipeline._analyze_ai_stage(self.job(), self.review("不符合"))
        self.assertFalse(unrelated["recommended_keep"])
        self.assertEqual(unrelated["llm_stage_decision"], "unrelated")
        insufficient = pipeline._analyze_ai_stage(self.job(), self.review("信息不足"))
        self.assertIsNone(insufficient["recommended_keep"])
        self.assertEqual(insufficient["model_status"], "success")
        self.assertEqual(insufficient["llm_stage_decision"], "pending_verification")

    def test_incomplete_failed_missing_unknown_or_wrong_identity_never_fall_back_or_claim_success(self):
        cases = ((self.job(jd="", jd_completeness="missing"), self.review()),
                 (self.job(jd_completeness="partial"), self.review()),
                 (self.job(), self.review(status="failed", error="fixture request failed")),
                 (self.job(), None), (self.job(), self.review("unknown")),
                 (self.job(), self.review(["符合"])), (self.job(), self.review(理由="   ")),
                 (self.job(), self.review(job_id="wrong-id")),
                 (self.job(), self.review(review_format="old")),
                 (self.job(), {"job_id": "ai-stage", "status": "success", "target_relevance": {"assessment": "direct"}}),
                 (self.job(), self.review(dimensions={})))
        for job, review in cases:
            with self.subTest(job=job, review=review), \
                 patch.object(pipeline.processor, "merge_model_review", side_effect=AssertionError("semantic fallback")):
                item = pipeline._analyze_ai_stage(job, review)
                self.assertIsNone(item["recommended_keep"])
                self.assertEqual(item["llm_stage_decision"], "pending_verification")
                self.assertNotEqual(item["model_status"], "success")
                self.assertEqual(item["jd_analysis_status"], "pending_verification")
                self.assertIn("待人工核实", item["reason"])
                self.assertNotIn("jd_dimensions", item)

    def test_old_code_and_simple_derived_conclusions_are_removed_before_projection(self):
        job = self.job(classification="暂不投", rule_classification="暂不投", reason="旧代码证据不足",
                       hard_rule_reasons=["旧规则硬排"], verification=["旧证据门槛"], jd_dimension_count=6,
                       model_evidence=["旧证据"], risk_flags=["旧风险"], ai_judgment="不符合", ai_reason="旧理由",
                       confidence="high", ability_gaps=["旧差距"], summary="旧摘要")
        review = self.review()
        before = copy.deepcopy(job)
        item = pipeline._analyze_ai_stage(job, review)
        self.assertTrue(item["recommended_keep"])
        for field in ("classification", "rule_classification", "hard_rule_reasons", "model_evidence", "risk_flags",
                      "jd_dimensions", "confidence", "ability_gaps", "summary"):
            self.assertNotIn(field, item)
        self.assertEqual(item["verification"], [])
        self.assertEqual(item["ai_judgment"], "符合")
        self.assertEqual(item["ai_reason"], review["理由"])
        self.assertEqual(job, before)

    def test_simple_report_uses_judgment_and_keeps_failures_pending_without_code_reclassification(self):
        cases = ((self.review(), "海投池", False), (self.review("不符合"), "暂不投", False),
                 (self.review("信息不足"), "海投池", True),
                 (self.review(status="failed", error="fixture failure"), "海投池", True),
                 (None, "海投池", True))
        with patch.object(pipeline.processor, "merge_model_review", side_effect=AssertionError("code classifier")), \
             patch.object(pipeline.processor, "rule_analyze_jd", side_effect=AssertionError("keyword fallback")):
            for review, classification, pending in cases:
                with self.subTest(review=review):
                    item = pipeline._analyze(self.job(review_format="simple-v1", ai_judgment="不符合"), review)
                    self.assertEqual(item["classification"], classification)
                    self.assertEqual(item["analysis_pending"], pending)
                    self.assertNotIn("llm_stage_projection", item)
                    self.assertNotIn("jd_dimensions", item)


class ReviewTests(PipelineCase):
    def test_both_protocols_reject_corrupt_child_input_signature_and_repair_on_normal_continuation(self):
        from llm_client import CodexConfig
        from review_cache import provider_signature, review_input_signature

        for mode in ("responses", "chat_completions"):
            with self.subTest(mode=mode):
                self.task = self.create()
                self.seed_llm_input()
                config = CodexConfig(model="gpt-5.6-luna", reasoning_effort="max", api_mode=mode,
                                     base_url="https://offline.invalid/v1", api_key="fixture-key")
                requested = []
                corrupt_next = True

                def request(job, *, prompt):
                    requested.append(job["job_id"])
                    return {"判断": "符合", "理由": "承担业务方案设计和交付。"}

                def child(task, stage, command, control, emit, monitor=None):
                    nonlocal corrupt_next
                    runtime = task.run_directory(stage)
                    prompt_file = Path(command[command.index("--prompt-file") + 1])
                    pipeline.processor.run_llm(runtime, workers=1, api_mode=mode, prompt_file=prompt_file)
                    context = self.read(runtime / "review_cache_context.json")
                    prompt = prompt_file.read_text(encoding="utf-8")
                    for job in task.input_jobs(stage):
                        self.assertEqual(context["input_signatures"][job_id(job)],
                                         review_input_signature(pipeline.processor.model_input(job), prompt))
                    if corrupt_next:
                        path = runtime / "prepared/llm_reviews.json"
                        payload = self.read(path)
                        next(row for row in payload["reviews"] if row["job_id"] == "a")["cache_input_signature"] = "wrong-nonempty"
                        self.write(path, payload)
                        corrupt_next = False
                        if monitor:
                            monitor()
                            self.assertEqual(task.stage_info(stage)["stats"]["llm_success_count"], 2)
                    elif monitor:
                        monitor()
                    return 0

                with patch("llm_client.load_codex_config", return_value=config), \
                     patch("llm_client.CodexResponsesClient") as client, \
                     patch.object(pipeline, "_provider_hash", return_value=provider_signature(config)), \
                     patch.object(pipeline, "_project_api_mode", return_value=mode), \
                     patch.object(pipeline, "_run_child", side_effect=child) as runner:
                    client.return_value.review_job.side_effect = request
                    first = self.run_step("llm")["stages"]["llm"]
                    self.assertEqual(first["stats"]["llm_success_count"], 2)
                    runtime = self.task.run_directory("llm")
                    path = runtime / "prepared/llm_reviews.json"
                    after_bad_child = self.read(path)
                    self.assertEqual({row["job_id"] for row in after_bad_child["reviews"]}, {"b", "d"})
                    self.assertEqual(after_bad_child["previous_reviews"][0]["reason"], "input_signature_changed")
                    self.task.set_keep("llm", ["b"], False)
                    resumed = self.run_step("llm")["stages"]["llm"]
                    self.assertEqual((resumed["stats"]["llm_cache_hit_count"], resumed["stats"]["llm_request_count"]), (2, 1))
                    self.assertEqual(requested[-1], "a")
                    payload = self.read(path)
                    next(row for row in payload["reviews"] if row["job_id"] == "b")["cache_input_signature"] = "wrong-nonempty"
                    self.write(path, payload)
                    repaired = self.run_step("llm")["stages"]["llm"]
                    self.assertEqual((repaired["stats"]["llm_cache_hit_count"], repaired["stats"]["llm_request_count"]), (2, 1))
                    self.assertEqual(requested[-1], "b")
                    cached = self.run_step("llm")["stages"]["llm"]
                    self.assertEqual((cached["stats"]["llm_cache_hit_count"], cached["stats"]["llm_request_count"]), (3, 0))
                    self.assertEqual(len(requested), 5)
                    self.assertEqual(runner.call_count, 3)
                    self.assertNotIn("b", cached["keep_ids"])
                    self.assertIs(cached["manual_choices"]["b"], False)
                archived = self.read(path)["previous_reviews"]
                self.assertEqual({entry["review"]["job_id"] for entry in archived}, {"a", "b"})

    def test_legacy_rich_success_is_archived_not_counted_as_new_success(self):
        self.seed_llm_input()

        def legacy_results(task, stage, command, control, emit, monitor=None):
            code = self.fake_reviews()(task, stage, command, control, emit, monitor)
            path = task.run_directory(stage) / "prepared/llm_reviews.json"
            payload = self.read(path)
            for row in payload["reviews"]:
                for key in ("review_format", "判断", "理由"):
                    row.pop(key, None)
                row.update(target_relevance={"assessment": "direct"}, dimensions={}, confidence="high")
            self.write(path, payload)
            return code

        with patch.object(pipeline, "_provider_hash", return_value="fixture-provider"), \
             patch.object(pipeline, "_run_child", side_effect=legacy_results):
            result = self.run_step("llm")
        stats = result["stages"]["llm"]["stats"]
        self.assertEqual(stats["llm_success_count"], 0)
        self.assertEqual(stats["llm_fallback_count"], 3)
        self.assertEqual(stats["llm_verification_count"], 3)
        for row in self.task.jobs("llm"):
            self.assertIsNone(row["recommended_keep"])
            self.assertNotEqual(row["model_status"], "success")
            self.assertNotIn("ai_judgment", row)
        cache = self.read(self.task.run_directory("llm") / "prepared/llm_reviews.json")
        self.assertEqual(cache["reviews"], [])
        self.assertEqual(len(cache["previous_reviews"]), 3)

    def test_ai_decision_and_pending_statistics_match_the_actual_saved_suggestions(self):
        self.seed_llm_input()

        def mixed_results(task, stage, command, control, emit, monitor=None):
            code = self.fake_reviews()(task, stage, command, control, emit, monitor)
            path = task.run_directory(stage) / "prepared/llm_reviews.json"
            payload = self.read(path)
            for review in payload["reviews"]:
                review["判断"] = {"a": "符合", "b": "不符合", "d": "信息不足"}[review["job_id"]]
            self.write(path, payload)
            return code

        with patch.object(pipeline, "_provider_hash", return_value="fixture-provider"), \
             patch.object(pipeline, "_run_child", side_effect=mixed_results):
            result = self.run_step("llm")
        info = result["stages"]["llm"]
        self.assertEqual(set(info["recommended_keep_ids"]), {"a"})
        self.assertEqual(set(info["recommended_exclude_ids"]), {"b"})
        self.assertEqual(set(info["pending_ids"]), {"d"})
        self.task.set_keep("llm", ["d"], True)
        edited = self.task.stage_info("llm")
        self.assertEqual(set(edited["keep_ids"]), {"a", "d"})
        self.assertEqual(edited["pending_ids"], [])
        self.assertEqual(edited["stats"]["llm_verification_count"], 0)
        self.assertEqual(edited["stats"]["llm_success_count"], 3)
        self.assertEqual(set(edited["recommended_keep_ids"]), {"a"})
        self.assertEqual(next(row for row in self.task.jobs("llm") if job_id(row) == "d")["ai_judgment"], "信息不足")
        self.task.apply_recommendations("llm")
        self.assertEqual(self.task.stage_info("llm")["pending_ids"], ["d"])
        self.assertEqual(info["stats"]["llm_success_count"], 3)
        self.assertEqual(info["stats"]["llm_fallback_count"], 0)
        self.assertEqual(info["stats"]["llm_verification_count"], 1)
        self.assertEqual(info["stats"]["llm_recommended_count"], 1)
        self.assertEqual(info["stats"]["llm_recommended_exclude_count"], 1)
        self.assertEqual(info["stats"]["llm_triage_total"], len(info["result_ids"]))
        self.assertEqual(info["stats"]["llm_triage_total"], 3)
        logs = "\n".join(str(value) for kind, value in self.events if kind == "log")
        self.assertIn("待人工核实 1", logs)
        self.assertNotIn("规则回退", logs)

    def test_ai_reprojects_valid_checkpoints_locally_and_preserves_manual_choices(self):
        self.seed_llm_input()
        with patch.object(pipeline, "_provider_hash", return_value="fixture-provider"), \
             patch.object(pipeline, "_run_child", side_effect=self.fake_reviews()) as child:
            self.run_step("llm")
            self.task.set_keep("llm", ["a"], False)
            # Persist an old-style view without touching the signed checkpoint.
            rows = self.task.jobs("llm")
            for row in rows:
                row.pop("llm_stage_projection", None)
                row.pop("llm_stage_decision", None)
                row.update(classification="暂不投", reason="旧代码证据不足", recommended_keep=False)
            self.task.begin("llm")
            self.task.finish("llm", rows)
            reopened = ReviewTask.open(self.task.path)
            # Recompute from the signed checkpoint under the same provider
            # identity; no child request should be needed for valid hits.
            result = self.run_step("llm")
        self.assertTrue(all(row["recommended_keep"] is True for row in reopened.jobs("llm")))
        self.assertEqual(child.call_count, 1)
        info = result["stages"]["llm"]
        self.assertEqual(info["stats"]["llm_cache_hit_count"], 3)
        self.assertEqual(set(info["recommended_keep_ids"]), set("abd"))
        self.assertNotIn("a", info["keep_ids"])
        self.assertFalse(info["manual_choices"]["a"])
        self.assertTrue(all(row["recommended_keep"] is True for row in self.task.jobs("llm")))
        self.assertTrue(all("classification" not in row for row in self.task.jobs("llm")))
        self.task.apply_recommendations("llm")
        self.assertEqual(set(self.task.stage_info("llm")["keep_ids"]), set("abd"))

    def test_desktop_runs_each_job_independently_and_preserves_success_checkpoints(self):
        self.seed_llm_input()
        with patch.object(pipeline, "_provider_hash", return_value="fixture-provider"), \
             patch.object(pipeline, "_run_child", side_effect=self.fake_reviews()) as child:
            self.run_step("llm")
            command = child.call_args.args[2]
            self.assertEqual(command[command.index("--conversation-size") + 1], "1")
            resumed = self.run_step("llm")
        self.assertEqual(child.call_count, 1)
        self.assertEqual(resumed["stages"]["llm"]["stats"]["llm_cache_hit_count"], 3)
        logs = "\n".join(str(value) for kind, value in self.events if kind == "log")
        self.assertIn("每个岗位独立复核", logs)
        self.assertNotIn("15 个岗位", logs)
        self.assertNotIn("暂存响应", logs)

    def test_provider_hash_uses_the_same_project_model_and_max_effort_as_the_request(self):
        from dataclasses import replace
        from llm_client import CodexConfig
        from review_cache import provider_signature

        local = CodexConfig(model="local-model", reasoning_effort="high",
                            base_url="https://fixture-only.test/v1", api_key="fixture-only-key")
        project_settings_path = self.folder / "llm-provider.json"
        with patch("project_llm_settings.default_settings_path", return_value=project_settings_path), \
             patch("llm_client.load_codex_config", side_effect=lambda **options: replace(
                 local, **{key: value for key, value in options.items()
                           if key in {"model", "reasoning_effort", "api_mode"}}
             )) as loader:
            actual = pipeline._provider_hash()
        loader.assert_called_once_with(model="gpt-5.6-luna", reasoning_effort="max",
                                       api_mode="responses",
                                       project_settings_path=project_settings_path)
        self.assertEqual(actual, provider_signature(replace(local, model="gpt-5.6-luna", reasoning_effort="max")))
        self.assertNotEqual(actual, provider_signature(local))

    def test_saved_project_model_and_effort_reach_request_and_provider_signature(self):
        from dataclasses import replace
        from llm_client import CodexConfig
        from review_cache import provider_signature

        project_settings_path = self.folder / "llm-provider.json"
        project_llm_settings.save_settings(
            "https://custom-provider.example/v1", "custom-model", "high", path=project_settings_path,
        )
        local = CodexConfig(model="fallback-model", reasoning_effort="low",
                            base_url="https://custom-provider.example/v1", api_key="fixture-only-key")
        with patch("project_llm_settings.default_settings_path", return_value=project_settings_path), \
             patch("llm_client.load_codex_config", side_effect=lambda **options: replace(
                 local, **{key: value for key, value in options.items()
                           if key in {"model", "reasoning_effort", "api_mode"}}
             )) as loader:
            actual = pipeline._provider_hash()
            command = pipeline._process_command(
                self.task, self.task.run_directory("llm"), "llm", 8, 30, None,
            )
            project_llm_settings.save_settings(
                "https://custom-provider.example/v1", "another-custom-model", "max",
                path=project_settings_path,
            )
            changed = pipeline._provider_hash()

        self.assertEqual(loader.call_args_list[0].kwargs, {
            "model": "custom-model", "reasoning_effort": "high",
            "api_mode": "responses",
            "project_settings_path": project_settings_path,
        })
        self.assertEqual(loader.call_args_list[1].kwargs, {
            "model": "another-custom-model", "reasoning_effort": "max",
            "api_mode": "responses",
            "project_settings_path": project_settings_path,
        })
        self.assertEqual(actual, provider_signature(replace(local, model="custom-model", reasoning_effort="high")))
        self.assertNotEqual(actual, changed, "model/effort edits must invalidate the old provider signature")
        self.assertEqual(command[command.index("--model") + 1], "custom-model")
        self.assertEqual(command[command.index("--reasoning-effort") + 1], "high")
        self.assertEqual(command[command.index("--api-mode") + 1], "responses")
        self.assertNotIn("fixture-only-key", command)

    def test_protocol_change_reaches_child_and_invalidates_provider_signature(self):
        from dataclasses import replace
        from llm_client import CodexConfig
        from review_cache import provider_signature

        path = self.folder / "chat-provider.json"
        config = CodexConfig(model="custom-model", reasoning_effort="high",
                             base_url="https://provider.example/v1", api_key="fixture-only-key")
        with patch("project_llm_settings.default_settings_path", return_value=path), \
             patch("llm_client.load_codex_config", side_effect=lambda **options: replace(
                 config, **{key: value for key, value in options.items()
                            if key in {"model", "reasoning_effort", "api_mode"}}
             )):
            project_llm_settings.save_settings(config.base_url, config.model, config.reasoning_effort, path=path)
            responses_hash = pipeline._provider_hash()
            project_llm_settings.save_settings(config.base_url, config.model, config.reasoning_effort,
                                              api_mode="chat_completions", path=path)
            chat_hash = pipeline._provider_hash()
            command = pipeline._process_command(self.task, self.task.run_directory("llm"), "llm", 10, 30, None)
        self.assertNotEqual(responses_hash, chat_hash)
        self.assertEqual(chat_hash, provider_signature(replace(config, api_mode="chat_completions")))
        self.assertEqual(command[command.index("--api-mode") + 1], "chat_completions")
        self.assertNotIn(config.api_key, command)

    def test_verified_chat_effort_uses_identical_parent_child_signatures_and_logs_actual_effort(self):
        self.seed_llm_input()
        url, model = "https://chat-provider.example/v1", "deepseek-v4.1-flash"
        path = project_llm_settings.default_settings_path()
        project_llm_settings.save_settings(url, model, "max", path=path, api_mode="chat_completions", chat_stream=True)

        def child(task, stage, command, control, emit, monitor=None):
            self.assertEqual(stage, "llm")
            self.assertEqual(command[command.index("--api-mode") + 1], "chat_completions")
            self.assertNotIn("fixture-only-key", command)
            pipeline.processor.run_llm(
                task.run_directory(stage), workers=1,
                prompt_file=Path(command[command.index("--prompt-file") + 1]),
            )
            if monitor:
                monitor()
            return 0

        with patch("llm_client._read_toml", return_value={}), \
             patch("project_llm_settings.load_api_key", return_value="fixture-only-key"), \
             patch("llm_client.CodexResponsesClient") as client, \
             patch.object(pipeline, "_run_child", side_effect=child) as runner:
            client.return_value.review_job.return_value = {"判断": "符合", "理由": "承担企业方案设计与交付。"}
            first = self.run_step("llm")["stages"]["llm"]["stats"]
            self.assertEqual((first["llm_success_count"], first["llm_request_count"]), (3, 3))
            self.assertIsNone(client.call_args.args[0].effective_reasoning_effort)
            marker = project_llm_settings.chat_reasoning_signature(url, model, "max", chat_stream=True)
            project_llm_settings.save_settings(
                url, model, "max", path=path, api_mode="chat_completions", chat_stream=True,
                chat_reasoning_verified_for=marker,
            )
            result = self.run_step("llm")["stages"]["llm"]["stats"]
            self.assertEqual(result["llm_success_count"], 3)
            self.assertEqual(result["llm_cache_invalidated_count"], 3)
            self.assertEqual(result["llm_request_count"], 3)
            self.assertEqual(client.call_args.args[0].effective_reasoning_effort, "max")
            self.assertTrue(client.call_args.args[0].chat_stream)
            cached = self.run_step("llm")["stages"]["llm"]["stats"]
            self.assertEqual((cached["llm_cache_hit_count"], cached["llm_request_count"]), (3, 0))
        self.assertEqual(runner.call_count, 2)
        logs = [value for kind, value in self.events if kind == "log" and str(value).startswith("AI 复核：")]
        self.assertTrue(logs)
        self.assertIn("强度 max", str(logs[-1]))

    def test_local_codex_url_changes_invalidate_desktop_cache_and_keys_are_never_saved(self):
        self.seed_llm_input(complete=("a",))
        home = self.folder / "codex-home"
        home.mkdir()
        config_path, auth_path = home / "config.toml", home / "auth.json"
        config_path.write_text(
            'model_provider = "selected"\nmodel = "ignored-local-model"\nmodel_reasoning_effort = "high"\n'
            '[model_providers.selected]\nbase_url = "https://first-provider.test/v1"\n'
            'requires_openai_auth = true\n', encoding="utf-8")
        auth_path.write_text('{"OPENAI_API_KEY":"first-fixture-key"}', encoding="utf-8")

        with patch.dict(os.environ, {"CODEX_HOME": str(home)}), \
             patch.object(pipeline, "_run_child", side_effect=self.fake_reviews()) as child:
            first_snapshot = self.run_step("llm")
            first_hash = pipeline._provider_hash()
            auth_path.write_text('{"OPENAI_API_KEY":"rotated-fixture-key"}', encoding="utf-8")
            rotated_auth = auth_path.read_bytes()
            second_snapshot = self.run_step("llm")
            self.assertEqual(child.call_count, 1, "key rotation alone must not invalidate successful reviews")
            self.assertEqual(second_snapshot["stages"]["llm"]["stats"]["llm_cache_hit_count"], 1)
            config_path.write_text(
                'model_provider = "selected"\nmodel = "another-ignored-model"\nmodel_reasoning_effort = "low"\n'
                '[model_providers.selected]\nbase_url = "https://second-provider.test/v1"\n'
                'requires_openai_auth = true\n', encoding="utf-8")
            second_hash = pipeline._provider_hash()
            self.assertNotEqual(first_hash, second_hash)
            second_config = config_path.read_bytes()
            third_snapshot = self.run_step("llm")

        self.assertEqual(child.call_count, 2, "the desktop parent must reject cache signed for the previous URL")
        self.assertEqual(first_snapshot["stages"]["llm"]["stats"]["llm_request_count"], 1)
        self.assertEqual(third_snapshot["stages"]["llm"]["stats"]["llm_cache_invalidated_count"], 1)
        command = child.call_args.args[2]
        for option in ("--codex-home", "--base-url", "--api-key"):
            self.assertNotIn(option, command, "desktop inherits the active CODEX_HOME without copying connection details")
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-luna")
        self.assertEqual(command[command.index("--reasoning-effort") + 1], "max")
        self.assertEqual((config_path.read_bytes(), auth_path.read_bytes()), (second_config, rotated_auth))
        runtime = self.task.run_directory("llm")
        reviews = self.read(runtime / "prepared/llm_reviews.json")
        self.assertEqual([entry["reason"] for entry in reviews["previous_reviews"]], ["signature_changed"])
        persisted = "\n".join(path.read_text(encoding="utf-8") for path in runtime.rglob("*") if path.is_file())
        for private in ("first-fixture-key", "rotated-fixture-key",
                        "https://first-provider.test/v1", "https://second-provider.test/v1"):
            self.assertNotIn(private, persisted)
        self.assertTrue(first_hash.startswith("semantic-review-v1:provider:"))

    def test_ai_defaults_to_ten_requests_independently_of_jd_pages(self):
        self.seed_llm_input()
        with patch.object(pipeline, "_provider_hash", return_value="provider-hash"), \
             patch.object(pipeline, "_run_child", side_effect=self.fake_reviews()) as child:
            result = self.run_step("llm", jd_workers=3)
        command = child.call_args.args[2]
        self.assertEqual(command[command.index("--workers") + 1], "10")
        self.assertEqual(result["stages"]["llm"]["stats"]["workers"], 10)

    def test_only_full_jd_is_sent_to_luna_with_max_effort(self):
        self.seed_llm_input(complete=("a",), partial=("b",))
        with patch.object(pipeline, "_provider_hash", return_value="provider-hash"), \
             patch.object(pipeline, "_run_child", side_effect=self.fake_reviews()) as child:
            result = self.run_step("llm", workers=8)
        child.assert_called_once()
        stats = result["stages"]["llm"]["stats"]
        self.assertEqual((stats["llm_candidate_count"], stats["llm_success_count"], stats["llm_fallback_count"]), (1, 1, 2))
        payload = self.read(self.task.run_directory("llm") / "prepared/jd_shortlists/jd_input.json")
        self.assertEqual([j["job_id"] for j in payload["jobs"]], ["a"])
        self.assertFalse(result["stages"]["llm"]["confirmed"])
        self.assertEqual(result["stages"]["report"]["status"], "pending")
        self.assertEqual(stats["llm_verification_count"], 2)
        by_id = {row["job_id"]: row for row in self.task.jobs("llm")}
        self.assertTrue(by_id["a"]["recommended_keep"])
        self.assertEqual(by_id["a"]["llm_stage_decision"], "related")
        for key in ("b", "d"):
            self.assertIsNone(by_id[key]["recommended_keep"])
            self.assertEqual(by_id[key]["llm_stage_decision"], "pending_verification")
            self.assertNotEqual(by_id[key]["model_status"], "success")

    def test_failed_model_requests_stay_pending_without_losing_jobs(self):
        self.seed_llm_input()
        with patch.object(pipeline, "_provider_hash", return_value="hash"), \
             patch.object(pipeline, "_run_child", side_effect=self.fake_reviews(status="failed")):
            result = self.run_step("llm")
        stats = result["stages"]["llm"]["stats"]
        self.assertEqual((stats["llm_success_count"], stats["llm_failed_count"], stats["llm_fallback_count"], stats["llm_pending_count"]), (0, 3, 3, 0))
        self.assertEqual({job_id(j) for j in self.task.jobs("llm")}, set("abd"))
        self.assertTrue(all(j["recommended_keep"] is None for j in self.task.jobs("llm")))
        self.assertTrue(all(j["model_status"] == "failed" for j in self.task.jobs("llm")))
        self.assertEqual(stats["llm_verification_count"], 3)
        self.assertEqual(result["stages"]["llm"]["status"], "completed")

    def test_unavailable_configuration_finishes_with_explicit_pending_verification(self):
        self.seed_llm_input()
        with patch.object(pipeline, "_provider_hash", return_value=None), \
             patch.object(pipeline, "_run_child", side_effect=self.fake_reviews(configuration_failed=True)):
            result = self.run_step("llm")
        info = result["stages"]["llm"]
        self.assertEqual(info["status"], "completed")
        self.assertEqual(info["stats"]["llm_fallback_count"], 3)
        self.assertEqual(info["stats"]["llm_pending_count"], 0)
        self.assertIn("模型配置", info["issues"][0])
        self.assertTrue(all(j["recommended_keep"] is None for j in self.task.jobs("llm")))
        self.assertTrue(all(j["model_status"] != "success" for j in self.task.jobs("llm")))
        self.assertNotIn("规则回退", info["issues"][0])

    def test_identical_successful_reviews_do_not_launch_a_second_subprocess(self):
        self.seed_llm_input()
        with patch.object(pipeline, "_provider_hash", return_value="provider"), \
             patch.object(pipeline, "_run_child", side_effect=self.fake_reviews()) as child:
            self.run_step("llm")
            self.run_step("llm")
        child.assert_called_once()
        self.assertEqual(self.task.stage_info("llm")["stats"]["llm_success_count"], 3)

    def test_provider_change_does_not_reuse_an_old_review(self):
        self.seed_llm_input()
        with patch.object(pipeline, "_provider_hash", return_value="provider-1"), \
             patch.object(pipeline, "_run_child", side_effect=self.fake_reviews()):
            self.run_step("llm")
        def changed(task, stage, command, control, emit, monitor):
            self.assertEqual(self.read(task.run_directory(stage) / "prepared/llm_reviews.json")["reviews"], [])
            return self.fake_reviews()(task, stage, command, control, emit, monitor)
        with patch.object(pipeline, "_provider_hash", return_value="provider-2"), \
             patch.object(pipeline, "_run_child", side_effect=changed) as child:
            self.run_step("llm")
        child.assert_called_once()

    def test_cli_signed_checkpoint_is_reused_by_desktop_with_identical_inputs(self):
        from llm_client import CodexConfig
        from review_cache import provider_signature

        self.seed_llm_input()
        runtime = self.task.run_directory("llm")
        rows = self.task.input_jobs("llm")
        self.write(runtime / "prepared/merged_jobs_index.json", {"jobs": rows})
        self.write(runtime / "prepared/jd_shortlists/jd_input.json", {"jobs": rows})
        self.write(runtime / "prepared/jd_shortlists/details.json", [{"job_id": job_id(j), "jd": j["jd"]} for j in rows])
        config = CodexConfig(model="gpt-5.6-luna", reasoning_effort="max",
                             base_url="https://fixture-only.test/v1", api_key="fixture-only-key")
        with patch("llm_client.load_codex_config", return_value=config), \
             patch("llm_client.CodexResponsesClient") as client:
            client.return_value.review_job.return_value = {"判断": "符合", "理由": "承担企业方案设计与交付。"}
            result = pipeline.processor.run_llm(runtime)
        self.assertEqual(result["completed_count"], 3)
        with patch.object(pipeline, "_provider_hash", return_value=provider_signature(config)), \
             patch.object(pipeline, "_run_child", side_effect=AssertionError("signed cache should avoid child")):
            snapshot = self.run_step("llm")
        stats = snapshot["stages"]["llm"]["stats"]
        self.assertEqual((stats["llm_success_count"], stats["llm_cache_hit_count"], stats["llm_request_count"]), (3, 3, 0))

    def test_legacy_sidecar_cannot_upgrade_unsigned_reviews_to_current_success(self):
        self.seed_llm_input()
        runtime = self.task.run_directory("llm")
        inputs = self.task.input_jobs("llm")
        hashes = {job_id(j): pipeline._review_hash(j, provider="provider") for j in inputs}
        self.write(runtime / "review_cache_context.json", {"inputs": hashes, "provider": "provider"})
        self.write(runtime / "prepared/llm_reviews.json", {"reviews": [
            {"job_id": job_id(j), "status": "success", "dimensions": {}} for j in inputs]})
        with patch.object(pipeline, "_provider_hash", return_value="provider"), \
             patch.object(pipeline, "_run_child", side_effect=self.fake_reviews()) as child:
            snapshot = self.run_step("llm")
        child.assert_called_once()
        stats = snapshot["stages"]["llm"]["stats"]
        self.assertEqual((stats["llm_cache_hit_count"], stats["llm_cache_legacy_count"], stats["llm_request_count"]), (0, 3, 3))
        saved = self.read(runtime / "prepared/llm_reviews.json")
        self.assertEqual(len(saved["reviews"]), 3)
        self.assertEqual({entry["reason"] for entry in saved["previous_reviews"]}, {"missing_signature"})

    def test_model_cannot_override_salary_or_experience_hard_rules(self):
        row = {**self.original[1], "jd": JD, "jd_completeness": "complete",
               "observed_in": [{"track": "current", "experience_filter": "1-3年"}]}
        review = {"job_id": "b", "status": "success", "dimensions": {},
                  "salary_parsed": {"lower_k": 100}, "classification": "重点投递",
                  "experience_assessment": {"minimum_years": 99, "hard_conflict": True, "evidence": []}}
        item = pipeline._analyze(row, review)
        self.assertEqual(item["salary_parsed"]["lower_k"], 12)
        self.assertEqual(item["classification"], "海投池")
        self.assertEqual(item["llm_review"], review)
        row["salary"] = "20-30K"
        item = pipeline._analyze(row, review)
        self.assertEqual(item["classification"], "重点投递")
        self.assertIn("仅供核实", " ".join(item["verification"]))


class ReportTests(PipelineCase):
    def test_report_tracks_all_imported_jobs_but_pool_excludes_confirmed_removals(self):
        self.import_and_confirm(exclude=("a",))
        self.shortlist_and_confirm()
        with patch.object(pipeline, "_run_child", side_effect=self.fake_jd([{"job_id": k, "jd": JD} for k in "bd"])):
            self.run_step("fetch-jd", scraper_root=self.scraper)
        self.task.confirm("fetch-jd")
        with patch.object(pipeline, "_provider_hash", return_value="hash"), \
             patch.object(pipeline, "_run_child", side_effect=self.fake_reviews(status="failed")):
            self.run_step("llm")
        self.task.confirm("llm")
        with patch.object(pipeline, "_run_child", side_effect=AssertionError("report accessed network")):
            result = self.run_step("report")
        stats = result["stages"]["report"]["stats"]
        all_rows = self.read(Path(stats["json_path"]))["jobs"]
        pool_document = self.read(Path(stats["pool_path"]))
        pool = pool_document["jobs"]
        markdown = Path(stats["markdown_path"]).read_text(encoding="utf-8")
        self.assertEqual({job_id(j) for j in all_rows}, set("abcd"))
        self.assertEqual(len(all_rows), len({job_id(j) for j in all_rows}))
        self.assertEqual({job_id(j) for j in pool}, set())
        self.assertEqual(stats["pool_count"], len(pool))
        self.assertEqual(pool_document["summary"]["total"], len(pool))
        self.assertEqual(sum(pool_document["summary"]["classification_counts"].values()), len(pool))
        self.assertEqual(pool_document["metadata"]["unique_job_count"], len(pool))
        self.assertEqual(stats["total_jobs"], len(all_rows))
        self.assertEqual(sum(stats["classification_counts"].values()), len(all_rows))
        overview = markdown.split("以下为全部岗位详细证据。", 1)[0]
        self.assertIn("## 可投总览", overview)
        self.assertIn(f"可投清单：{len(pool)} 岗", overview)
        self.assertEqual(len(re.findall(r"^\| (?:重点投递|海投池) \|", overview, flags=re.MULTILINE)), len(pool))
        self.assertEqual(len(re.findall(r"^- \[", markdown, flags=re.MULTILINE)), len(all_rows))
        self.assertEqual(markdown.count("前四步人工审核（生成时快照）："), len(all_rows))
        self.assertIn("审核摘要为报告生成时的前四步快照", markdown)
        self.assertNotIn("running", markdown)
        self.assertNotIn('"has_result"', markdown)
        self.assertNotIn('"confirmed"', markdown)
        for item in pool:
            self.assertIn(item["job_link"], overview)
        for item in all_rows:
            if item not in pool:
                self.assertNotIn(item["job_link"], overview)
            self.assertEqual(item["review_selection"]["report"]["status"], "running")
            self.assertFalse(item["review_selection"]["report"]["confirmed"])
        for name, count in stats["classification_counts"].items():
            self.assertEqual(count, sum(j["classification"] == name for j in all_rows))
            self.assertIn(f"## {name}（{count}）", markdown)
        for item in all_rows:
            self.assertIn(item["job_link"], markdown)
            self.assertTrue(item["observed_in"])
            self.assertEqual(item["review_format"], "simple-v1")
            for obsolete in ("jd_dimensions", "jd_dimension_count", "model_evidence", "ability_gaps", "confidence"):
                self.assertNotIn(obsolete, item)
            self.assertIn("review_selection", item)
            self.assertTrue(Path(stats["json_path"]).is_relative_to(self.task.path))
        for obsolete in ("六维", "JD证据：", "模型证据：", "能力差距：", "置信度"):
            self.assertNotIn(obsolete, markdown)
        a = next(j for j in all_rows if job_id(j) == "a")
        self.assertEqual(a["excluded_at"], "import")
        self.assertEqual(a["classification"], "暂不投")
        self.assertIn("确认剔除", a["reason"])
        self.task.set_keep("import", ["a"], True)
        self.assertEqual(self.task.stage_info("report")["status"], "stale")
        with self.assertRaises(TaskStateError):
            self.task.confirm("report")

    def test_report_quality_and_audit_snapshot_stay_accurate_after_read_only_confirmation(self):
        self.seed_llm_input(complete=("a",), partial=("b",))
        self.task.begin("llm")
        rows = [pipeline._analyze(job, {"job_id": job_id(job), "status": "success", "dimensions": {}})
                for job in self.task.input_jobs("llm")]
        self.task.finish("llm", rows)
        self.task.confirm("llm")
        with patch.object(pipeline, "_run_child", side_effect=AssertionError("report accessed network")), \
             patch.object(pipeline, "_provider_hash", side_effect=AssertionError("report accessed model configuration")):
            result = self.run_step("report")
        stats = result["stages"]["report"]["stats"]
        paths = [Path(stats[key]) for key in ("json_path", "pool_path", "markdown_path")]
        outputs = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in paths}
        saved = self.read(paths[0])
        pool = self.read(paths[1])
        markdown = paths[2].read_text(encoding="utf-8")
        queue_counts = {key: sum(job["list_queue"] == key for job in saved["jobs"])
                        for key in pipeline.LIST_QUEUE_LABELS}
        self.assertEqual(saved["metadata"]["list_queue_counts"], queue_counts)
        self.assertEqual(stats["list_queue_counts"], queue_counts)
        self.assertEqual(saved["metadata"]["screening_version"], pipeline.processor.SCREENING_VERSION)
        self.assertEqual(markdown, str(pipeline._redact("\n".join(
            pipeline._report_markdown_lines(saved["jobs"], pool["jobs"], saved["metadata"])
        ))))
        blocks = {re.search(r"岗位ID：([^；\n]+)", block).group(1): block
                  for block in re.findall(r"^- \[.*?(?=^- \[|\Z)", markdown, flags=re.MULTILINE | re.DOTALL)}
        self.assertIn(f"JD完整度：完整（正文 {len(JD)} 字符）", blocks["a"])
        self.assertIn(f"JD完整度：部分（正文 {len(JD)} 字符；待补齐完整正文）", blocks["b"])
        self.assertIn("JD完整度：缺失（JD正文未提供，待核实）", blocks["d"])
        self.assertNotIn("完整性待核实", blocks["a"])
        self.assertIn("列表初筛：已确认剔除", blocks["c"])
        self.assertIn("JD 补抓：未进入本步", blocks["c"])
        self.assertIn("AI 语义复核：已确认（选择未记录）", blocks["a"])
        self.assertIn("检索来源：", blocks["a"])
        self.assertIn("源行号：", blocks["a"])
        self.assertIn("JD证据：", blocks["a"])
        self.assertIn("模型证据：", blocks["a"])
        self.assertIn("规则回退", markdown.split("以下为全部岗位详细证据。", 1)[0])
        self.assertEqual(len(blocks), len(saved["jobs"]))
        self.assertEqual(pool["summary"]["total"], 3)
        self.assertIn("可投清单：3 岗", markdown)
        self.task.confirm("report")
        self.assertTrue(self.task.stage_info("report")["confirmed"])
        self.assertEqual(self.task.stage_info("report")["stats"], stats)
        for path, original in outputs.items():
            self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()), original)
        self.assertEqual(self.read(paths[0]), saved)
        self.assertTrue(all(job["review_selection"]["report"]["status"] == "running" for job in saved["jobs"]))

    def test_report_human_audit_summary_preserves_manual_and_missing_history_without_raw_report_state(self):
        job = {"review_selection": {
            "import": {"confirmed": True, "keep": False, "manual_keep": False},
            "shortlist": {"in_input": False, "has_result": False, "status": "completed"},
            "fetch-jd": {"has_result": True, "keep": True, "confirmed": False},
            "report": {"status": "running", "confirmed": False},
        }}
        before = copy.deepcopy(job)
        self.assertEqual(pipeline._report_selection_summary(job),
                         "导入与去重：已确认剔除（手动）；列表初筛：未进入本步；JD 补抓：保留，未确认；AI 语义复核：未记录")
        self.assertEqual(job, before)
        self.assertEqual(pipeline._report_selection_summary({"review_selection": None}),
                         "导入与去重：未记录；列表初筛：未记录；JD 补抓：未记录；AI 语义复核：未记录")

    def test_no_jd_manual_retention_does_not_override_pure_sales_or_nonsoftware_exclusions(self):
        for original in (self.original[2], {"job_id": "lawyer", "title": "律师", "skills": ["诉讼", "法律"]}):
            item = pipeline._analyze({**original, "jd_completeness": "missing"})
            self.assertEqual(item["classification"], "暂不投")
            self.assertFalse(item["recommended_keep"])

    def test_incomplete_jd_does_not_reopen_a_legacy_gate_or_bypass_saved_duties(self):
        cases = [
            ({"title": "AI", "skills": [], "jd": "", "jd_completeness": "missing"}, "暂不投"),
            ({"title": "AI架构师", "skills": [], "jd": "", "jd_completeness": "missing"}, "海投池"),
            ({"title": "系统架构师", "skills": [], "jd": "", "jd_completeness": "missing"}, "海投池"),
            ({"title": "AI产品经理", "skills": [], "jd": "负责资料整理。", "jd_completeness": "partial"}, "暂不投"),
            ({"title": "AI产品经理", "skills": [], "jd": "负责技术方案设计并完成系统集成。", "jd_completeness": "partial"}, "海投池"),
            ({"title": "AI产品经理", "skills": [], "jd": JD, "jd_completeness": "partial"}, "海投池"),
            ({"title": "AI产品经理", "skills": [], "jd": JD, "jd_completeness": "complete"}, "重点投递"),
        ]
        for number, (fields, expected) in enumerate(cases):
            with self.subTest(fields=fields):
                job = {"job_id": f"legacy-{number}", "salary": "20-30K", "list_pass": True,
                       "list_excluded": False, "screening_version": "old", **fields}
                before = copy.deepcopy(job)
                cli = pipeline.processor.analyze_report_job(job, None, prompt=None)
                desktop = pipeline.processor.analyze_report_job(job, None, prompt=None, analyzer=pipeline._analyze)
                self.assertEqual(cli["classification"], expected)
                self.assertEqual(desktop["classification"], expected)
                self.assertEqual(desktop["recommended_keep"], expected != "暂不投")
                if fields["jd_completeness"] != "complete":
                    self.assertEqual(desktop["model_status"], "fallback_incomplete_jd")
                self.assertEqual(desktop["jd_dimension_count"], cli["jd_dimension_count"])
                if not fields["jd"]:
                    self.assertEqual(desktop["jd_dimension_count"], 0)
                    self.assertEqual(desktop["jd_evidence"], [])
                self.assertEqual(job, before)

    def test_manual_retention_with_missing_jd_remains_in_new_report_pool_as_pending(self):
        for path in self.source.glob("raw/*/shanghai/*.json"):
            document = self.read(path)
            for row in document["jobs"]:
                if row["job_id"] == "a":
                    row.update(title="AI", skills=[])
            self.write(path, document)
        self.task = self.create()
        self.import_and_confirm()
        self.shortlist_and_confirm(restore=("a",))
        self.assertFalse(next(j for j in self.task.jobs("shortlist") if job_id(j) == "a")["list_pass"])
        with patch.object(pipeline, "_run_child", side_effect=self.fake_jd([])):
            self.run_step("fetch-jd", scraper_root=self.scraper)
        self.task.confirm("fetch-jd")
        with patch.object(pipeline, "_run_child", side_effect=AssertionError("missing bodies requested a model")), \
             patch.object(pipeline, "_provider_hash", side_effect=AssertionError("missing bodies read credentials")):
            self.run_step("llm")
            self.assertNotIn("a", self.task.stage_info("llm")["keep_ids"])
            self.assertIn("a", self.task.stage_info("llm")["pending_ids"])
            a = next(row for row in self.task.jobs("llm") if job_id(row) == "a")
            self.assertEqual(a["llm_stage_decision"], "pending_verification")
            self.assertNotEqual(a["model_status"], "success")
            self.task.confirm("llm")
            snapshot = self.run_step("report")
        stats = snapshot["stages"]["report"]["stats"]
        all_rows = self.read(Path(stats["json_path"]))["jobs"]
        pool = self.read(Path(stats["pool_path"]))["jobs"]
        a = next(row for row in all_rows if job_id(row) == "a")
        self.assertIsNone(a.get("excluded_at"))
        self.assertEqual(a["list_queue"], "excluded")
        self.assertFalse(a["list_pass"])
        self.assertEqual(a["classification"], "待核实")
        self.assertTrue(a["analysis_pending"])
        self.assertNotEqual(a["model_status"], "success")
        self.assertNotIn("ai_judgment", a)
        self.assertNotIn("a", {job_id(row) for row in pool})
        self.assertEqual(stats["pending_verification_count"], 3)
        self.assertEqual(stats["pool_count"], len(pool))

    def test_low_salary_and_experience_flags_are_independent(self):
        self.assertEqual(pipeline._with_flags({"salary": "16-20K", "tags": ["1年以上"]})["experience_gap"], "")
        for value in ("3-5年", "5年以上"):
            self.assertIn("经验要求偏高", pipeline._with_flags({"salary": "20-30K", "tags": [value]})["experience_gap"])
        for salary, expected in (("16-20K", ["薪资低于目标", "需核实固定薪资"]),
                                 ("17-20K", []), ("10-16K", ["薪资低于目标"]),
                                 ("面议", ["薪资待核实"])):
            self.assertEqual(pipeline._with_flags({"salary": salary})["salary_flags"], expected)


class ChildAndSafetyTests(PipelineCase):
    def test_owned_subprocess_stdout_is_redacted_before_callback_or_log(self):
        emit = pipeline._Emitter(self.task, "llm", lambda k, v: self.events.append((k, v)))
        text = 'api_key="dummy-private-value"\nCookie: first=private-cookie; second=private-cookie-two\nAuthorization: Bearer private-auth\n'
        code = pipeline._run_child(self.task, "llm", [sys.executable, "-c", f"print({text!r})"], pipeline.StageControl(), emit)
        self.assertEqual(code, 0)
        visible = json.dumps(self.events, ensure_ascii=False) + self.task.path.joinpath("task.log").read_text(encoding="utf-8")
        for secret in ("dummy-private-value", "private-cookie", "private-auth"):
            self.assertNotIn(secret, visible)
        lease = self.read(self.task.run_directory("llm") / "child_lease.json")
        self.assertFalse(lease["active"])

    def test_pause_ends_our_local_child_tree_not_only_python_parent(self):
        control = pipeline.StageControl()
        grandchildren = []
        def callback(kind, value):
            if kind == "log" and value.startswith("grandchild:"):
                grandchildren.append(int(value.split(":", 1)[1].strip()))
                control.pause()
        command = [sys.executable, "-u", "-c", (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(15)']); "
            "print('grandchild:'+str(p.pid),flush=True); time.sleep(15)"
        )]
        started = time.monotonic()
        pipeline._run_child(self.task, "llm", command, control, pipeline._Emitter(self.task, "llm", callback))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(len(grandchildren), 1)
        self.assertFalse(pipeline._pid_alive(grandchildren[0]))
        self.assertEqual(control.requested, "paused")

    def test_pause_after_parent_exit_still_ends_the_owned_descendant(self):
        control = pipeline.StageControl()
        descendants = []
        parent_exited = []
        def callback(kind, value):
            if kind == "log" and value.startswith("descendant:"):
                descendants.append(int(value.split(":", 1)[1].strip()))
        def monitor():
            lease = self.read(self.task.run_directory("llm") / "child_lease.json")
            if descendants and not pipeline._pid_alive(lease["pid"]):
                parent_exited.append(True)
                control.pause()
        command = [sys.executable, "-u", "-c", (
            "import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(12)']); "
            "print('descendant:'+str(p.pid),flush=True)"
        )]
        started = time.monotonic()
        pipeline._run_child(self.task, "llm", command, control,
                            pipeline._Emitter(self.task, "llm", callback), monitor)
        self.assertLess(time.monotonic() - started, 6)
        self.assertTrue(parent_exited, "pause must occur after the actual root has exited")
        self.assertEqual(len(descendants), 1)
        self.assertFalse(pipeline._pid_alive(descendants[0]))

    def test_parent_exit_pipe_drain_is_bounded_without_a_pause_click(self):
        descendants = []
        def callback(kind, value):
            if kind == "log" and value.startswith("descendant:"):
                descendants.append(int(value.split(":", 1)[1].strip()))
        command = [sys.executable, "-u", "-c", (
            "import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(12)']); "
            "print('descendant:'+str(p.pid),flush=True)"
        )]
        started = time.monotonic()
        code = pipeline._run_child(self.task, "llm", command, pipeline.StageControl(),
                                   pipeline._Emitter(self.task, "llm", callback))
        self.assertNotEqual(code, 0)
        self.assertLess(time.monotonic() - started, 7)
        self.assertEqual(len(descendants), 1)
        self.assertFalse(pipeline._pid_alive(descendants[0]))

    def test_successful_setup_can_leave_a_detached_local_process_alive(self):
        # This harmless short-lived process stands in for a dedicated Chrome.
        # Normal completion must not close the user's verification window.
        descendants = []
        def callback(kind, value):
            if kind == "log" and value.startswith("detached:"):
                descendants.append(int(value.split(":", 1)[1].strip()))
        command = [sys.executable, "-u", "-c", (
            "import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(1.5)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "print('detached:'+str(p.pid),flush=True)"
        )]
        code = pipeline._run_child(None, "browser", command, pipeline.StageControl(),
                                   pipeline._Emitter(None, "browser", callback))
        self.assertEqual(code, 0)
        self.assertEqual(len(descendants), 1)
        self.assertTrue(pipeline._pid_alive(descendants[0]))
        deadline = time.monotonic() + 3
        while pipeline._pid_alive(descendants[0]) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(pipeline._pid_alive(descendants[0]))

    def test_pause_from_final_monitor_does_not_detach_the_owned_descendant(self):
        control = pipeline.StageControl()
        descendants = []

        def callback(kind, value):
            if kind == "log" and value.startswith("detached:"):
                descendants.append(int(value.split(":", 1)[1].strip()))

        def monitor():
            # Only the final monitor call runs after child.wait() has assigned
            # `code`; force exactly the previously missed cancellation window.
            if "code" in sys._getframe(1).f_locals:
                control.pause()

        assign = pipeline._WindowsProcessTree.__init__

        def delayed_assignment(tree, child):
            # A venv redirector used to start the real interpreter before job
            # assignment. An OS-suspended root must stay safe across this gap.
            time.sleep(.2)
            assign(tree, child)

        command = [sys.executable, "-u", "-c", (
            "import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(3)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "print('detached:'+str(p.pid),flush=True)"
        )]
        with patch.object(pipeline._WindowsProcessTree, "__init__", new=delayed_assignment):
            pipeline._run_child(self.task, "llm", command, control,
                                pipeline._Emitter(self.task, "llm", callback), monitor)
        self.assertEqual(control.requested, "paused")
        self.assertEqual(len(descendants), 1)
        deadline = time.monotonic() + 1
        while pipeline._pid_alive(descendants[0]) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(pipeline._pid_alive(descendants[0]))

    def test_log_queue_does_not_block_after_callback_errors(self):
        callback = Mock(side_effect=RuntimeError("window closed"))
        code = pipeline._run_child(None, "llm", [sys.executable, "-c", "for i in range(600): print(i)"],
                                   pipeline.StageControl(), pipeline._Emitter(None, "llm", callback))
        self.assertEqual(code, 0)
        self.assertGreaterEqual(callback.call_count, 600)

    def test_linked_or_external_checkpoint_is_rejected(self):
        with self.assertRaises(pipeline.PipelineError):
            pipeline._safe_path(self.task, self.source / "raw/overwrite.json")
        checkpoint = self.task.run_directory("llm") / "prepared"
        with patch.object(pipeline, "_reparse", side_effect=lambda p: p == checkpoint):
            with self.assertRaises(pipeline.PipelineError):
                pipeline._safe_path(self.task, checkpoint / "llm_reviews.json")

    def test_stop_takes_precedence_over_pause(self):
        control = pipeline.StageControl()
        control.stop()
        control.pause()
        self.assertEqual(control.requested, "stopped")

    def test_unexpected_error_body_cannot_reach_manifest_or_logs(self):
        with patch.object(pipeline.processor, "collect_rows", side_effect=RuntimeError("api_key=private-failure-value")):
            result = self.run_step("import")
        self.assertEqual(result["stages"]["import"]["status"], "failed")
        all_text = json.dumps(result, ensure_ascii=False) + self.task.path.joinpath("task.log").read_text(encoding="utf-8")
        self.assertNotIn("private-failure-value", all_text)


class StageRedoPipelineTests(PipelineCase):
    def offline_jd_child(self, task, stage, command, control, emit, monitor=None):
        self.assertEqual(stage, "fetch-jd")
        self.assertEqual(Path(command[command.index("--output-root") + 1]), task.run_directory(stage))
        with redirect_stdout(io.StringIO()):
            pipeline.processor.fetch_jd(task.run_directory(stage), self.scraper, batch_size=1,
                                        python_path=sys.executable, wait_on_gate=False)
        if monitor:
            monitor()
        return 0

    def offline_jd_request(self, command, **options):
        rows = self.read(Path(command[command.index("--input") + 1]))["jobs"]
        self.jd_requests.extend(job_id(job) for job in rows)
        self.write(Path(command[command.index("--detail-output") + 1]),
                   [{"job_id": job_id(job), "jd": JD, "complete": True} for job in rows])
        return subprocess.CompletedProcess(command, 0, "完成", "")

    def offline_ai_child(self, task, stage, command, control, emit, monitor=None):
        self.assertEqual(stage, "llm")
        self.assertEqual(Path(command[command.index("--output-root") + 1]), task.run_directory(stage))
        with redirect_stdout(io.StringIO()):
            pipeline.processor.run_llm(task.run_directory(stage), workers=1,
                                        prompt_file=Path(command[command.index("--prompt-file") + 1]))
        if monitor:
            monitor()
        return 0

    def test_redo_jd_refetches_all_complete_bodies_with_real_cli_cache_logic(self):
        self.import_and_confirm()
        self.shortlist_and_confirm()
        expected = [job_id(job) for job in self.task.input_jobs("fetch-jd")]
        self.jd_requests = []
        with patch.object(pipeline.processor, "ensure_detail_cdp_ready", return_value=(True, None)), \
             patch.object(pipeline.processor, "run_detail_subprocess", side_effect=self.offline_jd_request), \
             patch.object(pipeline, "_run_child", side_effect=self.offline_jd_child):
            self.run_step("fetch-jd", scraper_root=self.scraper)
            self.assertEqual(self.jd_requests, expected)
            old_runtime = self.task.run_directory("fetch-jd")
            self.task.set_keep("fetch-jd", ["a"], False)
            self.task.confirm("fetch-jd")
            self.run_step("fetch-jd", scraper_root=self.scraper)
            self.assertEqual(self.jd_requests, expected, "ordinary run must keep complete JD checkpoints")
            upstream = {stage: self.task.stage_info(stage) for stage in ("import", "shortlist")}
            reset = self.run_step("fetch-jd", scraper_root=self.scraper, redo=True)
            self.assertEqual(reset["stages"]["fetch-jd"]["status"], "pending")
            self.assertEqual(self.jd_requests, expected, "reset must not request JD")
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
        self.assertEqual(self.jd_requests, expected * 2)
        info = result["stages"]["fetch-jd"]
        self.assertEqual(info["stats"]["jd_success_count"], 3)
        self.assertEqual(info["excluded_ids"], [], "reset clears previous manual selections")
        self.assertEqual(info["redo_count"], 1)
        self.assertFalse(info["confirmed"])
        self.assertEqual(result["stages"]["llm"]["attempt"], 0)
        for stage, before in upstream.items():
            self.assertEqual(result["stages"][stage], before)
        self.assertNotEqual(self.task.run_directory("fetch-jd"), old_runtime)
        self.assertFalse(old_runtime.exists())

    def test_paused_jd_redo_resumes_only_remaining_jobs_in_its_new_round(self):
        self.import_and_confirm()
        self.shortlist_and_confirm()
        with patch.object(pipeline, "_run_child", side_effect=self.fake_jd(
                [{"job_id": key, "jd": JD} for key in "abd"])):
            self.run_step("fetch-jd", scraper_root=self.scraper)
        old_runtime = self.task.run_directory("fetch-jd")

        def partial_child(task, stage, command, control, emit, monitor=None):
            self.fake_jd([{"job_id": "a", "jd": JD}])(task, stage, command, control, emit, monitor)
            control.pause()
            return 0

        reset = self.run_step("fetch-jd", scraper_root=self.scraper, redo=True)
        self.assertEqual(reset["stages"]["fetch-jd"]["status"], "pending")
        with patch.object(pipeline, "_run_child", side_effect=partial_child):
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
        self.assertEqual(result["stages"]["fetch-jd"]["status"], "paused")
        new_runtime = self.task.run_directory("fetch-jd")
        self.assertNotEqual(new_runtime, old_runtime)
        self.assertFalse(old_runtime.exists(), "redo must delete the old stage runtime directory")
        reopened = ReviewTask.open(self.task.path)
        self.task = reopened
        self.addCleanup(reopened._release_run)
        self.jd_requests = []
        with patch.object(pipeline.processor, "ensure_detail_cdp_ready", return_value=(True, None)), \
             patch.object(pipeline.processor, "run_detail_subprocess", side_effect=self.offline_jd_request), \
             patch.object(pipeline, "_run_child", side_effect=self.offline_jd_child):
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
        self.assertEqual(self.jd_requests, [job_id(job) for job in self.task.input_jobs("fetch-jd") if job_id(job) != "a"])
        self.assertEqual(self.task.run_directory("fetch-jd"), new_runtime)
        self.assertEqual(result["stages"]["fetch-jd"]["redo_count"], 1)
        self.assertEqual(result["stages"]["fetch-jd"]["stats"]["jd_success_count"], 3)

    def test_ai_redo_bypasses_parent_and_cli_success_cache_and_report_remains_local(self):
        from llm_client import CodexConfig

        self.seed_llm_input()
        config = CodexConfig(model="gpt-5.6-luna", reasoning_effort="max",
                             base_url="https://offline.invalid/v1", api_key="fixture-key")
        with patch("llm_client.load_codex_config", return_value=config), \
             patch("llm_client.CodexResponsesClient") as client, \
             patch.object(pipeline, "_run_child", side_effect=self.offline_ai_child) as runner:
            client.return_value.review_job.return_value = {"判断": "符合", "理由": "承担企业方案设计与交付。"}
            self.run_step("llm")
            self.assertEqual(client.return_value.review_job.call_count, 3)
            old_runtime = self.task.run_directory("llm")
            self.task.set_keep("llm", ["b"], False)
            cached = self.run_step("llm")["stages"]["llm"]["stats"]
            self.assertEqual((cached["llm_cache_hit_count"], cached["llm_request_count"]), (3, 0))
            self.assertEqual(client.return_value.review_job.call_count, 3)
            self.task.confirm("llm")
            self.run_step("report")
            self.task.confirm("report")
            old_report = self.task.stage_info("report")
            old_markdown = Path(old_report["stats"]["markdown_path"])
            old_markdown_bytes = old_markdown.read_bytes()
            reset = self.run_step("llm", redo=True)
            self.assertEqual(reset["stages"]["llm"]["status"], "pending")
            self.assertEqual(client.return_value.review_job.call_count, 3)
            result = self.run_step("llm")
            self.assertEqual(client.return_value.review_job.call_count, 6)
            self.assertEqual(runner.call_count, 2)
        info = result["stages"]["llm"]
        self.assertEqual((info["stats"]["llm_cache_hit_count"], info["stats"]["llm_request_count"]), (0, 3))
        self.assertEqual(info["stats"]["llm_success_count"], 3)
        self.assertEqual(info["excluded_ids"], [], "reset clears previous manual selections")
        self.assertEqual(result["stages"]["report"]["status"], "stale")
        self.assertFalse(result["stages"]["report"]["confirmed"])
        self.assertFalse(old_runtime.exists())
        self.assertTrue(old_markdown.exists(), "LLM redo does not delete another stage's files")
        self.assertEqual(pipeline._task_report_prompt(self.task), pipeline.processor.load_prompt())
        self.task.confirm("llm")
        with patch.object(pipeline, "_provider_hash", side_effect=AssertionError("report read provider")), \
             patch("llm_client.load_codex_config", side_effect=AssertionError("report read auth")), \
             patch.object(pipeline, "_run_child", side_effect=AssertionError("report launched child")):
            report_reset = self.run_step("report", redo=True)
            self.assertEqual(report_reset["stages"]["report"]["status"], "pending")
            report = self.run_step("report")
        report_info = report["stages"]["report"]
        self.assertEqual(report_info["status"], "completed")
        self.assertEqual(report_info["redo_count"], 1)
        self.assertNotEqual(report_info["stats"]["markdown_path"], str(old_markdown))
        payload = self.read(Path(report_info["stats"]["json_path"]))
        self.assertEqual(len(payload["jobs"]), report_info["stats"]["total_jobs"])
        self.assertFalse(old_markdown.exists())

    def test_paused_ai_redo_reuses_only_new_round_successes_when_continued(self):
        from llm_client import CodexConfig

        self.seed_llm_input()
        config = CodexConfig(model="gpt-5.6-luna", reasoning_effort="max",
                             base_url="https://offline.invalid/v1", api_key="fixture-key")

        def interrupted_child(task, stage, command, control, emit, monitor=None):
            self.offline_ai_child(task, stage, command, control, emit, monitor)
            path = task.run_directory(stage) / "prepared/llm_reviews.json"
            payload = self.read(path)
            payload["reviews"] = payload["reviews"][:1]
            self.write(path, payload)
            control.pause()
            return 0

        with patch("llm_client.load_codex_config", return_value=config), \
             patch("llm_client.CodexResponsesClient") as client:
            client.return_value.review_job.return_value = {"判断": "符合", "理由": "承担企业方案设计与交付。"}
            with patch.object(pipeline, "_run_child", side_effect=self.offline_ai_child):
                self.run_step("llm")
            old_runtime = self.task.run_directory("llm")
            reset = self.run_step("llm", redo=True)
            self.assertEqual(reset["stages"]["llm"]["status"], "pending")
            self.assertEqual(client.return_value.review_job.call_count, 3)
            with patch.object(pipeline, "_run_child", side_effect=interrupted_child):
                result = self.run_step("llm")
            self.assertEqual(result["stages"]["llm"]["status"], "paused", result["stages"]["llm"].get("issues"))
            self.assertEqual(result["stages"]["llm"]["stats"]["llm_success_count"], 1)
            new_runtime = self.task.run_directory("llm")
            self.task = ReviewTask.open(self.task.path)
            self.addCleanup(self.task._release_run)
            with patch.object(pipeline, "_run_child", side_effect=self.offline_ai_child):
                result = self.run_step("llm")
            self.assertEqual(client.return_value.review_job.call_count, 8)
        stats = result["stages"]["llm"]["stats"]
        self.assertEqual((stats["llm_cache_hit_count"], stats["llm_request_count"], stats["llm_success_count"]), (1, 2, 3))
        self.assertEqual(self.task.run_directory("llm"), new_runtime)
        self.assertEqual(result["stages"]["llm"]["redo_count"], 1)
        self.assertFalse(old_runtime.exists())

    def test_live_children_from_any_current_or_old_round_reject_redo_before_pointer_switch(self):
        self.import_and_confirm()
        self.run_step("import", redo=True)
        before = self.task.snapshot()
        for directory in ("llm", "llm-redo-" + "a" * 32, "import"):
            with self.subTest(directory=directory):
                path = self.task.path / "runtime" / directory / "child_lease.json"
                self.write(path, {"active": True, "pid": os.getpid()})
                with patch.object(pipeline.processor, "collect_rows") as collect, \
                     self.assertRaises(pipeline.PipelineError):
                    self.run_step("import", redo=True)
                collect.assert_not_called()
                self.assertEqual(self.task.snapshot(), before)
                self.write(path, {"active": False, "pid": os.getpid()})

    def test_prompt_snapshot_from_another_round_cannot_validate_current_report(self):
        self.seed_llm_input()
        with patch.object(pipeline, "_provider_hash", return_value="fixture-provider"), \
             patch.object(pipeline, "_run_child", side_effect=self.fake_reviews()):
            self.run_step("llm")
            old_reference = self.task.stage_info("llm")["stats"]["review_prompt_file"]
            self.run_step("llm", redo=True)
            self.run_step("llm")
        self.assertIsNotNone(pipeline._task_report_prompt(self.task))
        with self.task._edit() as value:
            value["stages"]["llm"]["stats"]["review_prompt_file"] = old_reference
        self.assertIsNone(pipeline._task_report_prompt(self.task))


if __name__ == "__main__":
    unittest.main()
