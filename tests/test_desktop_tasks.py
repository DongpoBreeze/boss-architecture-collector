"""Offline state-machine, audit, persistence, and path-safety acceptance tests."""

import copy
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import desktop_tasks as tasks


def _abandon_running_task(path, started, finish):
    """Exercise real OS lock release on process exit, not a mocked PID check."""
    task = tasks.ReviewTask.open(Path(path))
    task.begin("import")
    task.record_progress("import", 1, 3, stats={"raw_record_count": 3})
    started.set()
    finish.wait(15)
    os._exit(0)


class TaskTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        (self.source / "raw" / "current").mkdir(parents=True)
        (self.source / "raw" / "target").mkdir(parents=True)
        self.raw = self.write_json(self.source / "raw" / "current" / "shanghai" / "jobs.json", {
            "jobs": [{"job_id": "a"}, {"job_id": "b"}, {"job_id": "c"}],
        })
        self.config = self.write_json(self.root / "config.json", {"current": {}, "target": {}})
        self.task = self.create()
        self.addCleanup(self.task._release_run)

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def create(self, **kwargs):
        options = {"city": "上海", "track": "all", "sample_size": 50, "config_path": self.config}
        options.update(kwargs)
        return tasks.ReviewTask.create(self.source, self.root / "tasks", "上海 50 个架构岗位", **options)

    def imported(self):
        return [
            {"job_id": "a", "title": "架构方案 A", "nested": {"items": [1]},
             "observed_in": [
                 {"source_file": "raw/current/shanghai/one.json", "source_line_number": 1, "track": "current"},
                 {"source_file": "raw/target/shanghai/two.json", "source_line_number": 7, "track": "target"},
             ]},
            {"job_id": "b", "title": "平台 B", "observed_in": [{"source_file": "raw/current/shanghai/one.json", "source_line_number": 2}]},
            {"job_id": "c", "title": "方案 C"},
        ]

    def run_import(self, jobs=None, confirm=True):
        self.task.begin("import")
        self.task.finish("import", self.imported() if jobs is None else jobs,
                         stats={"raw_record_count": 4, "selected_job_count": 3})
        if confirm:
            self.task.confirm("import")

    def run_stage(self, stage, changes=None, confirm=True):
        jobs = self.task.input_jobs(stage)
        for job in jobs:
            job.update((changes or {}).get(tasks.job_id(job), {}))
        self.task.begin(stage)
        self.task.finish(stage, jobs)
        if confirm:
            self.task.confirm(stage)

    def run_all(self):
        self.run_import()
        self.run_stage("shortlist", {key: {"list_pass": True} for key in "abc"})
        self.run_stage("fetch-jd", {key: {"jd": f"JD {key}"} for key in "abc"})
        self.run_stage("llm", {key: {"jd_completeness": "complete", "llm_review": {
            "job_id": key, "status": "success", "review_format": "simple-v1",
            "判断": "符合", "理由": f"review {key}"}} for key in "abc"})
        self.run_stage("report", {key: {"classification": "海投池"} for key in "abc"})

    def link_directory(self, target, link):
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name != "nt":
                self.skipTest("Directory symlinks are unavailable")
            # Junctions work without the symlink privilege and matter on 3.11,
            # where pathlib has no is_junction().  No delete/move shell is used.
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True)
            if result.returncode:
                self.skipTest("Neither directory symlinks nor junctions are available")
        self.addCleanup(lambda: link.rmdir() if link.exists() and not link.is_symlink()
                        else link.unlink(missing_ok=True))


class StableIdTests(unittest.TestCase):
    def test_precedence_and_normalization(self):
        self.assertEqual(tasks.job_id({"job_id": " first ", "dedupe_key": "second"}), "first")
        self.assertEqual(tasks.job_id({"job_id": " ", "dedupe_key": "dedupe", "encrypt_job_id": "encrypted"}), "dedupe")
        self.assertEqual(tasks.job_id({"encrypt_job_id": "encrypted", "job_link": "link"}), "encrypted")
        self.assertEqual(tasks.job_id({"job_link": "https://example.test/job"}), "https://example.test/job")
        self.assertEqual(tasks.job_id({"job_id": 0}), "0")

    def test_missing_invalid_and_boolean_ids_are_rejected_without_job_payload(self):
        for job in ({}, {"job_id": None}, {"job_id": " "}, {"link": "legacy-only"},
                    {"job_id": True}, {"job_id": {"api_key": "private-secret"}}, [], None):
            with self.subTest(job=job):
                with self.assertRaises(tasks.TaskStateError) as caught:
                    tasks.job_id(job)
                self.assertNotIn("private-secret", str(caught.exception))


class WorkflowTests(TaskTestCase):
    def test_open_preserves_legacy_custom_rules_until_explicit_reset_and_save(self):
        custom = "旧自定义：六项职责命中至少四项，输出原文证据与能力差距。"
        self.task.set_review_rules(custom)
        manifest_path = self.task.path / "task_manifest.json"
        saved = manifest_path.read_bytes()
        reopened = tasks.ReviewTask.open(self.task.path)
        self.addCleanup(reopened._release_run)
        self.assertEqual(reopened.get_review_rules(), custom)
        self.assertEqual(manifest_path.read_bytes(), saved)
        reopened.set_review_rules(None)
        current = reopened.get_review_rules()
        self.assertNotEqual(current, custom)
        for phrase in ("企业AI解决方案架构师", "AI应用架构师", "AI产品经理", "不强求", "配合开发"):
            self.assertIn(phrase, current.replace(" ", ""))
        self.assertIsNone(reopened.snapshot().get("review_rules"))

    def test_ai_projection_replaces_stale_and_empty_derived_values_only_when_explicit(self):
        base = {"job_id": "ai", "classification": "暂不投", "reason": "旧代码证据不足",
                "jd_dimensions": {"方案设计": {"present": True}}, "model_evidence": ["旧证据"],
                "verification": ["旧校验"], "risk_flags": ["旧风险"], "hard_rule_reasons": ["旧硬排"],
                "llm_review": {"status": "success", "target_relevance": {"assessment": "unrelated"}},
                "observed_in": [{"source_file": "original.json"}]}
        saved = copy.deepcopy(base)
        projected = {"job_id": "ai", "llm_stage_projection": tasks.AI_STAGE_PROJECTION,
                     "recommended_keep": True, "reason": "AI判断直接相关，建议保留",
                     "jd_dimensions": {}, "model_evidence": [], "verification": [], "risk_flags": [],
                     "llm_review": {"status": "success", "target_relevance": {"assessment": "direct"}}}
        result = tasks._merge_job(base, projected)
        self.assertNotIn("classification", result)
        self.assertNotIn("hard_rule_reasons", result)
        for field in ("jd_dimensions", "model_evidence", "verification", "risk_flags", "llm_review"):
            self.assertEqual(result[field], projected[field])
        self.assertEqual(result["observed_in"], base["observed_in"])
        self.assertEqual(base, saved)
        self.assertEqual(tasks._merge_job(base, {"job_id": "ai"})["classification"], "暂不投")
        pending = tasks._merge_job(base, {"job_id": "ai", "llm_stage_projection": tasks.AI_STAGE_PROJECTION,
                                          "review_format": "simple-v1", "analysis_pending": True,
                                          "recommended_keep": None})
        self.assertIsNone(pending["recommended_keep"])
        self.assertTrue(pending["analysis_pending"])
        report = tasks._merge_job(result, {"classification": "暂不投", "recommended_keep": False})
        self.assertNotIn("llm_stage_projection", report)
        self.assertEqual(report["classification"], "暂不投")
        fetched = tasks._merge_job(result, {"jd_analysis_status": "not_run", "jd": "新正文", "recommended_keep": True})
        self.assertNotIn("llm_stage_projection", fetched)
        self.assertNotIn("llm_review", fetched)

    def test_new_simple_report_clears_stale_success_and_rich_fields_on_pending_result(self):
        base = {"job_id": "simple", "classification": "暂不投", "ai_judgment": "不符合", "ai_reason": "旧理由",
                "confidence": "high", "ability_gaps": ["旧差距"], "jd_dimensions": {"旧维度": {}},
                "model_cache_input_verified": True, "llm_stage_projection": tasks.AI_STAGE_PROJECTION,
                "observed_in": [{"source_file": "source.json"}]}
        report = {"job_id": "simple", "review_format": "simple-v1", "classification": "海投池",
                  "analysis_pending": True, "model_status": "failed", "recommended_keep": None,
                  "llm_review": {"status": "failed", "review_format": "simple-v1"}}
        merged = tasks._merge_job(base, report)
        for obsolete in ("ai_judgment", "ai_reason", "confidence", "ability_gaps", "jd_dimensions",
                         "llm_stage_projection", "model_cache_input_verified"):
            self.assertNotIn(obsolete, merged)
        self.assertEqual(merged["classification"], "海投池")
        self.assertTrue(merged["analysis_pending"])
        self.assertIsNone(merged["recommended_keep"])
        self.assertEqual(merged["observed_in"], base["observed_in"])

    def test_body_only_jd_result_cannot_resurrect_cached_semantic_fields(self):
        originals = self.imported()
        originals[0].update(classification="暂不投", jd_dimensions={"方案设计": {"present": False}},
                            jd="旧正文", jd_record={"jd": "旧正文"}, reason="旧分类依据", model_status="success")
        self.run_import(originals)
        self.run_stage("shortlist", {key: {"list_pass": True} for key in "abc"})
        rows = self.task.input_jobs("fetch-jd")
        for row in rows:
            for field in tasks.SEMANTIC_RESULT_FIELDS:
                row.pop(field, None)
            row.pop("jd_record", None)
            row.update(jd="", jd_completeness="missing", jd_status="待核实",
                       jd_analysis_status="not_run", recommended_keep=True)
        self.task.begin("fetch-jd")
        self.task.finish("fetch-jd", rows)
        self.task.confirm("fetch-jd")
        for row in (self.task.jobs("fetch-jd")[0], self.task.input_jobs("llm")[0], self.task.all_jobs()[0]):
            self.assertNotIn("classification", row)
            self.assertNotIn("jd_dimensions", row)
            self.assertNotIn("model_status", row)
            self.assertNotIn("jd_record", row)
            self.assertEqual(row["jd"], "")
            self.assertEqual(len(row["observed_in"]), 2)
        self.run_stage("llm", {"a": {"jd_analysis_status": "rule_fallback", "classification": "海投池"}})
        self.assertEqual(self.task.all_jobs()[0]["classification"], "海投池")

    def test_create_is_pending_and_confirm_only_loads_next_input(self):
        self.assertEqual(tasks.STAGES, ("import", "shortlist", "fetch-jd", "llm", "report"))
        self.assertEqual(set(tasks.STAGE_LABELS), set(tasks.STAGES))
        self.assertTrue(all(info["status"] == "pending" for info in self.task.manifest["stages"].values()))
        self.assertEqual(self.task.input_jobs("import"), [])
        self.assertEqual(self.task.jobs("import"), [])
        with self.assertRaises(tasks.TaskStateError):
            self.task.confirm("import")
        self.run_import(confirm=False)
        self.assertFalse(self.task.stage_info("import")["confirmed"])
        self.assertEqual(self.task.input_jobs("shortlist"), [])
        with patch.object(tasks.ReviewTask, "begin", side_effect=AssertionError("confirmation executed work")):
            selected = self.task.confirm("import")
        self.assertEqual(selected, ["a", "b", "c"])
        self.assertEqual(self.task.stage_info("shortlist")["status"], "pending")
        self.assertEqual(self.task.stage_info("shortlist")["input_ids"], selected)
        self.assertEqual(self.task.stage_info("shortlist")["attempt"], 0)
        self.assertEqual([tasks.job_id(job) for job in self.task.input_jobs("shortlist")], selected)
        self.assertEqual(self.task.confirm("import"), selected)

    def test_full_pipeline_uses_only_confirmed_names_and_preserves_all_imports(self):
        original = self.raw.read_bytes()
        self.run_import()
        self.run_stage("shortlist", {"a": {"list_pass": True}, "b": {"list_pass": False}, "c": {"list_pass": True}}, confirm=False)
        self.task.set_keep("shortlist", ["b"], True)
        self.task.set_keep("shortlist", ["c"], False)
        self.assertEqual(self.task.confirm("shortlist"), ["a", "b"])
        self.assertEqual([tasks.job_id(job) for job in self.task.input_jobs("fetch-jd")], ["a", "b"])
        self.run_stage("fetch-jd", {"a": {"jd": "系统职责", "recommended_keep": True}, "b": {"jd": "边界职责", "recommended_keep": False}})
        self.assertEqual([tasks.job_id(job) for job in self.task.input_jobs("llm")], ["a"])
        self.run_stage("llm", {"a": {"llm_review": {"job_id": "a", "status": "success",
                                                       "review_format": "simple-v1", "判断": "符合",
                                                       "理由": "职责证据"}}})
        self.run_stage("report", {"a": {"classification": "海投池"}})
        all_jobs = {tasks.job_id(job): job for job in self.task.all_jobs()}
        self.assertEqual(set(all_jobs), {"a", "b", "c"})
        self.assertIsNone(all_jobs["a"]["excluded_at"])
        self.assertEqual(all_jobs["b"]["excluded_at"], "fetch-jd")
        self.assertEqual(all_jobs["c"]["excluded_at"], "shortlist")
        self.assertTrue(all_jobs["b"]["review_selection"]["shortlist"]["manual_keep"])
        self.assertEqual(all_jobs["a"]["llm_review"]["理由"], "职责证据")
        self.assertEqual(self.raw.read_bytes(), original)
        self.assertEqual(self.task.stage_info("report")["keep_ids"], ["a"])

    def test_every_stage_requires_valid_confirmed_predecessors(self):
        for stage in tasks.STAGES[1:]:
            with self.subTest(stage=stage):
                with self.assertRaises(tasks.TaskStateError):
                    self.task.begin(stage)
        self.run_import(confirm=False)
        with self.assertRaises(tasks.TaskStateError):
            self.task.begin("shortlist")
        self.task.confirm("import")
        self.task.begin("shortlist")
        for action in (lambda: self.task.begin("import"), lambda: self.task.confirm("import"),
                       lambda: self.task.set_keep("import", ["a"], False),
                       lambda: self.task.apply_recommendations("import")):
            with self.assertRaises(tasks.TaskStateError):
                action()
        self.task.interrupt("shortlist")

    def test_finish_requires_owner_and_exact_unique_ids(self):
        self.run_import()
        self.task.begin("shortlist")
        for jobs in ([{"job_id": "a"}], [{"job_id": key} for key in "abcd"],
                     [{"job_id": key} for key in "aabc"], [{"title": "no ID"}], {}):
            with self.subTest(jobs=jobs):
                with self.assertRaises(tasks.TaskStateError):
                    self.task.finish("shortlist", jobs)
                self.assertEqual(self.task.stage_info("shortlist")["status"], "running")
        other = tasks.ReviewTask.open(self.task.path)
        with self.assertRaises(tasks.TaskStateError):
            other.finish("shortlist", self.task.input_jobs("shortlist"))
        # Output order is normalized back to the frozen input, not worker order.
        self.task.finish("shortlist", [{"job_id": key, "list_pass": True} for key in "cba"])
        self.assertEqual([tasks.job_id(job) for job in self.task.jobs("shortlist")], list("abc"))
        with self.assertRaises(tasks.TaskStateError):
            self.task.finish("shortlist", [])

    def test_ai_pending_is_not_default_keep_and_prior_stage_recommendations_do_not_decide_llm(self):
        self.run_import()
        self.run_stage("shortlist", {key: {"list_pass": key != "c"} for key in "abc"})
        self.task.begin("fetch-jd")
        self.task.finish("fetch-jd", [{"job_id": "a", "jd": "A", "recommended_keep": False},
                                       {"job_id": "b", "jd": "B"}])
        self.assertEqual(self.task.stage_info("fetch-jd")["keep_ids"], ["b"])
        self.task.set_keep("fetch-jd", ["a"], True)
        self.task.confirm("fetch-jd")
        self.task.begin("llm")
        # Neither a prior-stage recommendation nor the old default keep is an
        # AI decision. Only a valid, identified review can move a row onward.
        self.task.finish("llm", [
            {"job_id": "a", "llm_review": {"job_id": "a", "status": "success", "review_format": "simple-v1",
                                                "判断": "符合", "理由": "承担方案设计。"}},
            {"job_id": "b", "llm_review": {"job_id": "b", "status": "success", "review_format": "simple-v1",
                                                "判断": "不符合", "理由": "职责无关。"}},
        ])
        self.assertEqual(self.task.stage_info("llm")["keep_ids"], ["a"])
        self.assertEqual(self.task.stage_info("llm")["excluded_ids"], ["b"])
        self.assertEqual(self.task.stage_info("llm")["pending_ids"], [])
        self.task.confirm("llm")
        self.run_stage("report", {key: {"recommended_keep": False} for key in "ab"}, confirm=False)
        self.assertEqual(self.task.stage_info("report")["keep_ids"], ["a"])

    def test_all_public_data_is_deep_copied_in_both_directions(self):
        jobs = self.imported()
        self.task.begin("import")
        stats = {"nested": {"count": 2}}
        self.task.finish("import", jobs, stats=stats)
        jobs[0]["nested"]["items"].append(9)
        stats["nested"]["count"] = 999
        for value in (self.task.manifest, self.task.snapshot(), self.task.refresh()):
            value["stages"]["import"]["keep_ids"].clear()
            value["city"] = "北京"
        stage = self.task.stage_info("import")
        stage["stats"]["nested"]["count"] = 999
        displayed = self.task.jobs("import")
        displayed[0]["nested"]["items"].append(8)
        displayed[0]["observed_in"].clear()
        self.task.confirm("import")
        inputs = self.task.input_jobs("shortlist")
        inputs[0]["nested"]["items"].append(7)
        all_jobs = self.task.all_jobs()
        all_jobs[0]["review_selection"]["import"]["keep"] = False
        all_jobs[0]["observed_in"].clear()
        selected = self.task.confirm("import")
        selected.clear()
        self.assertEqual(self.task.jobs("import")[0]["nested"]["items"], [1])
        self.assertEqual(len(self.task.jobs("import")[0]["observed_in"]), 2)
        self.assertEqual(self.task.stage_info("import")["stats"]["nested"]["count"], 2)
        self.assertEqual(self.task.stage_info("import")["keep_ids"], list("abc"))
        self.assertEqual(self.task.manifest["city"], "上海")

    def test_empty_source_and_empty_pipeline_are_explicitly_valid(self):
        self.run_import(jobs=[])
        self.assertEqual(self.task.stage_info("import")["status"], "completed")
        for stage in tasks.STAGES[1:]:
            self.assertEqual(self.task.input_jobs(stage), [])
            with self.assertRaises(tasks.TaskStateError):
                self.task.confirm(stage)
            self.task.begin(stage)
            self.task.finish(stage, [])
            self.assertEqual(self.task.confirm(stage), [])
        self.assertEqual(self.task.all_jobs(), [])

    def test_all_excluded_still_reaches_report_with_full_audit(self):
        self.run_import()
        self.run_stage("shortlist", {key: {"list_pass": False} for key in "abc"})
        self.assertEqual(self.task.stage_info("shortlist")["keep_ids"], [])
        for stage in ("fetch-jd", "llm", "report"):
            self.assertEqual(self.task.input_jobs(stage), [])
            self.run_stage(stage)
        jobs = self.task.all_jobs()
        self.assertEqual(len(jobs), 3)
        self.assertTrue(all(job["excluded_at"] == "shortlist" for job in jobs))
        self.assertEqual(self.task.stage_info("report")["status"], "completed")

    def test_all_imported_jobs_can_be_explicitly_excluded_without_losing_report_audit(self):
        self.run_import(confirm=False)
        self.task.set_keep("import", list("abc"), False)
        self.assertEqual(self.task.confirm("import"), [])
        for stage in tasks.STAGES[1:]:
            self.run_stage(stage)
        self.assertEqual(len(self.task.all_jobs()), 3)
        self.assertTrue(all(job["excluded_at"] == "import" for job in self.task.all_jobs()))

    def test_report_selection_is_read_only_and_confirmation_preserves_outputs_and_stats(self):
        for action in (lambda: self.task.set_keep("report", [], False),
                       lambda: self.task.apply_recommendations("report")):
            with self.assertRaisesRegex(tasks.TaskStateError, "报告页只读.*审核步骤.*重新生成"):
                action()
        self.run_all()
        runtime = self.task.run_directory("report")
        report_jobs = self.task.jobs("report")
        json_path = self.write_json(runtime / "prepared" / "report.json", {"jobs": report_jobs, "total": 3})
        pool_path = self.write_json(runtime / "prepared" / "pool.json", {"jobs": report_jobs, "total": 3})
        markdown_path = runtime / "prepared" / "report.md"
        markdown_path.write_text("# 已生成报告\n\n全量岗位与来源已核对。\n", encoding="utf-8")
        artifacts = {"json_path": str(json_path), "markdown_path": str(markdown_path), "pool_path": str(pool_path)}
        stats = {"total_jobs": 3, "pool_count": 3, "classification_counts": {"海投池": 3}, "artifacts": artifacts}
        self.task.begin("report")
        self.task.finish("report", report_jobs, stats=stats)
        before, audit = self.task.snapshot(), self.task.all_jobs()
        output_bytes = {path: path.read_bytes() for path in (json_path, pool_path, markdown_path)}
        for action in (lambda: self.task.set_keep("report", ["a"], False),
                       lambda: self.task.set_keep("report", ["a"], True),
                       lambda: self.task.set_keep("report", [], False),
                       lambda: self.task.apply_recommendations("report")):
            with self.assertRaisesRegex(tasks.TaskStateError, "报告页只读.*审核步骤.*重新生成"):
                action()
            self.assertEqual(self.task.snapshot(), before)
            self.assertEqual(self.task.all_jobs(), audit)
        with patch.object(tasks.ReviewTask, "begin", side_effect=AssertionError("report confirmation executed work")):
            self.assertEqual(self.task.confirm("report"), list("abc"))
        info = self.task.stage_info("report")
        self.assertTrue(info["confirmed"])
        self.assertEqual(info["keep_ids"], list("abc"))
        self.assertEqual(info["excluded_ids"], [])
        self.assertEqual(info["manual_choices"], {})
        self.assertEqual(info["stats"], stats)
        self.assertEqual([job["observed_in"] for job in self.task.all_jobs()],
                         [job["observed_in"] for job in audit])
        for path, original in output_bytes.items():
            self.assertEqual(path.read_bytes(), original)

    def test_report_read_only_guard_does_not_change_editing_in_the_first_four_steps(self):
        self.run_all()
        for stage in reversed(tasks.STAGES[:-1]):
            with self.subTest(stage=stage):
                self.task.set_keep(stage, ["a"], False)
                self.assertEqual(self.task.stage_info(stage)["keep_ids"], ["b", "c"])
                self.task.set_keep(stage, ["a"], True)
                self.assertEqual(self.task.stage_info(stage)["keep_ids"], list("abc"))
                self.task.apply_recommendations(stage)
                self.assertEqual(self.task.stage_info(stage)["manual_choices"], {})
                self.assertEqual(self.task.confirm(stage), list("abc"))
        self.assertEqual(self.task.stage_info("report")["status"], "stale")
        with self.assertRaises(tasks.TaskStateError):
            self.task.confirm("report")


class RerunAndAuditTests(TaskTestCase):
    def test_all_jobs_audit_indexes_id_lists_without_linear_membership_scans(self):
        class NoLinearMembership(list):
            def __contains__(self, item):
                raise AssertionError("Audit must use a per-stage membership index")

        self.run_all()
        self.task.set_keep("shortlist", ["b"], False)
        self.task.confirm("shortlist")
        expected = self.task.all_jobs()
        manifest = self.task.snapshot()
        before = (self.task.path / "task_manifest.json").read_bytes()
        for info in manifest["stages"].values():
            for field in ("input_ids", "result_ids", "keep_ids", "recommended_keep_ids", "excluded_ids"):
                info[field] = NoLinearMembership(info[field])
        with patch.object(self.task, "_load", return_value=manifest):
            actual = self.task.all_jobs()
        self.assertEqual(actual, expected)
        self.assertEqual([job["job_id"] for job in actual], list("abc"))
        self.assertEqual(actual[1]["excluded_at"], "shortlist")
        self.assertFalse(actual[1]["review_selection"]["shortlist"]["manual_keep"])
        self.assertEqual((self.task.path / "task_manifest.json").read_bytes(), before)

    def test_manual_overrides_survive_rerun_until_adopting_recommendations(self):
        self.run_import()
        self.run_stage("shortlist", {key: {"list_pass": key == "a"} for key in "abc"}, confirm=False)
        self.task.set_keep("shortlist", ["b"], True)
        self.task.set_keep("shortlist", ["a"], False)
        self.assertEqual([tasks.job_id(job) for job in self.task.jobs("shortlist", False)], ["b"])
        self.task.confirm("shortlist")
        before = self.task.stage_info("shortlist")["result_file"]
        self.run_stage("shortlist", {key: {"list_pass": key != "b"} for key in "abc"}, confirm=False)
        self.assertEqual(self.task.stage_info("shortlist")["keep_ids"], ["b", "c"])
        self.assertNotEqual(self.task.stage_info("shortlist")["result_file"], before)
        self.assertTrue((self.task.path / before).is_file())
        self.task.apply_recommendations("shortlist")
        self.assertEqual(self.task.stage_info("shortlist")["keep_ids"], ["a", "c"])
        self.assertEqual(self.task.stage_info("shortlist")["manual_choices"], {})

    def test_upstream_edit_marks_all_executed_downstream_stale_without_deleting(self):
        self.run_all()
        old_files = {stage: self.task.path / self.task.stage_info(stage)["result_file"] for stage in tasks.STAGES}
        old_content = {stage: path.read_bytes() for stage, path in old_files.items()}
        self.task.set_keep("shortlist", ["b"], False)
        self.assertFalse(self.task.stage_info("shortlist")["confirmed"])
        for stage in ("fetch-jd", "llm", "report"):
            self.assertEqual(self.task.stage_info(stage)["status"], "stale")
            self.assertFalse(self.task.stage_info(stage)["confirmed"])
            with self.assertRaises(tasks.TaskStateError):
                self.task.confirm(stage)
            with self.assertRaises(tasks.TaskStateError):
                self.task.begin(stage)
            self.assertEqual(old_files[stage].read_bytes(), old_content[stage])
        jobs = {tasks.job_id(job): job for job in self.task.all_jobs()}
        self.assertNotIn("jd", jobs["a"])
        self.assertNotIn("llm_review", jobs["a"])
        self.assertIsNone(jobs["b"]["excluded_at"])
        self.task.confirm("shortlist")
        self.assertEqual(self.task.stage_info("fetch-jd")["input_ids"], ["a", "c"])
        self.assertEqual(self.task.input_jobs("llm"), [])
        with self.assertRaises(tasks.TaskStateError):
            self.task.confirm("fetch-jd")
        self.run_stage("fetch-jd", {"a": {"jd": "fresh A"}, "c": {"jd": "fresh C"}})
        jobs = {tasks.job_id(job): job for job in self.task.all_jobs()}
        self.assertEqual(jobs["b"]["excluded_at"], "shortlist")
        self.assertEqual(jobs["a"]["jd"], "fresh A")
        self.assertNotIn("llm_review", jobs["a"])
        self.assertEqual(self.task.stage_info("llm")["status"], "stale")

    def test_rerunning_confirmed_upstream_invalidates_downstream_even_with_same_ids(self):
        self.run_all()
        self.task.begin("fetch-jd")
        self.assertEqual(self.task.stage_info("llm")["status"], "stale")
        self.assertEqual(self.task.stage_info("report")["status"], "stale")
        self.task.finish("fetch-jd", [{"job_id": key, "jd": "new JD"} for key in "abc"])
        with self.assertRaises(tasks.TaskStateError):
            self.task.confirm("llm")
        self.assertFalse(self.task.stage_info("fetch-jd")["confirmed"])

    def test_all_sources_and_sparse_enhancements_survive_every_stage(self):
        original = self.imported()
        original[0]["sources"] = ["source one"]
        self.run_import(jobs=original)
        self.task.begin("shortlist")
        self.task.finish("shortlist", [
            {"job_id": "a", "list_pass": True, "observed_in": [{"source_file": "extra.json"}], "sources": ["source two"]},
            {"job_id": "b", "list_pass": True}, {"job_id": "c", "list_pass": True},
        ])
        self.task.confirm("shortlist")
        self.task.begin("fetch-jd")
        self.task.finish("fetch-jd", [{"job_id": key, "jd": f"JD {key}", "observed_in": []} for key in "abc"])
        self.task.confirm("fetch-jd")
        self.task.begin("llm")
        self.task.finish("llm", [{"job_id": key, "model_evidence": {"gap": "needs review"}} for key in "abc"])
        self.task.confirm("llm")
        self.run_stage("report", {key: {"classification": "暂不投"} for key in "abc"})
        job = self.task.all_jobs()[0]
        self.assertEqual(job["title"], original[0]["title"])
        self.assertEqual(job["jd"], "JD a")
        self.assertEqual(len(job["observed_in"]), 3)
        self.assertEqual(job["sources"], ["source one", "source two"])
        self.assertEqual(job["model_evidence"]["gap"], "needs review")
        self.assertEqual(set(job["review_selection"]), set(tasks.STAGES))
        self.task.set_keep("import", ["b"], False)
        stale_job = self.task.all_jobs()[0]
        self.assertNotIn("jd", stale_job)
        self.assertNotIn("model_evidence", stale_job)
        self.assertEqual(len(stale_job["observed_in"]), 3)
        self.assertEqual(stale_job["sources"], ["source one", "source two"])

    def test_earliest_confirmed_exclusion_and_restore_are_auditable(self):
        self.run_import()
        self.task.set_keep("import", ["a"], False)
        self.task.confirm("import")
        self.run_stage("shortlist", {key: {"list_pass": key != "b"} for key in "bc"})
        jobs = {tasks.job_id(job): job for job in self.task.all_jobs()}
        self.assertEqual(jobs["a"]["excluded_at"], "import")
        self.assertEqual(jobs["b"]["excluded_at"], "shortlist")
        self.task.set_keep("import", ["a"], True)
        self.task.confirm("import")
        jobs = {tasks.job_id(job): job for job in self.task.all_jobs()}
        self.assertIsNone(jobs["a"]["excluded_at"])
        self.assertIsNone(jobs["b"]["excluded_at"])
        self.assertEqual(self.task.stage_info("shortlist")["status"], "stale")

    def test_unknown_manual_ids_and_stale_edit_are_rejected(self):
        self.run_all()
        before = self.task.snapshot()
        with self.assertRaises(tasks.TaskStateError):
            self.task.set_keep("shortlist", ["missing"], False)
        self.assertEqual(self.task.snapshot(), before)
        self.task.set_keep("import", ["a"], False)
        with self.assertRaises(tasks.TaskStateError):
            self.task.set_keep("llm", ["a"], True)
        with self.assertRaises(tasks.TaskStateError):
            self.task.apply_recommendations("llm")


class AtomicSelectionTests(TaskTestCase):
    def prepare_shortlist(self):
        self.run_import()
        self.run_stage("shortlist", {key: {"list_pass": True} for key in "abc"}, confirm=False)

    def test_confirm_compares_reviewed_selection_inside_the_write_transaction(self):
        self.prepare_shortlist()
        expected = self.task.stage_info("shortlist")
        other = tasks.ReviewTask.open(self.task.path)
        other.set_keep("shortlist", [], True, replace_selection=True)
        saved = other.snapshot()
        with self.assertRaisesRegex(tasks.TaskStateError, "其他窗口修改"):
            self.task.confirm("shortlist", expected_stage=expected)
        self.assertEqual(other.snapshot(), saved)
        self.assertFalse(saved["stages"]["shortlist"]["confirmed"])
        self.assertEqual(self.task.confirm("shortlist", expected_stage=other.stage_info("shortlist")), [])
        self.assertEqual(self.task.input_jobs("fetch-jd"), [])

    def test_confirm_protects_manual_intent_without_a_selection_revision_change(self):
        self.prepare_shortlist()
        expected = self.task.stage_info("shortlist")
        other = tasks.ReviewTask.open(self.task.path)
        other.set_keep("shortlist", ["b"], True)
        self.assertEqual(other.stage_info("shortlist")["revision"], expected["revision"])
        with self.assertRaisesRegex(tasks.TaskStateError, "其他窗口修改"):
            self.task.confirm("shortlist", expected_stage=expected)
        self.assertFalse(self.task.stage_info("shortlist")["confirmed"])

    def test_three_argument_edits_remain_additive_and_do_not_replace_other_choices(self):
        self.prepare_shortlist()
        self.task.set_keep("shortlist", ["a"], False)
        self.task.set_keep("shortlist", ["b"], False)
        self.task.set_keep("shortlist", ["a"], True)
        info = self.task.stage_info("shortlist")
        self.assertEqual(info["keep_ids"], ["a", "c"])
        self.assertEqual(info["manual_choices"], {"a": True, "b": False})
        before = self.task.snapshot()
        self.task.set_keep("shortlist", [], True)
        self.assertEqual(self.task.snapshot(), before)

    def test_full_replacement_is_one_commit_and_invalidates_downstream_once(self):
        self.run_all()
        raw = self.raw.read_bytes()
        before = self.task.stage_info("shortlist")
        frozen = copy.deepcopy(before)
        old_files = {stage: self.task.path / self.task.stage_info(stage)["result_file"] for stage in tasks.STAGES}
        old_contents = {stage: path.read_bytes() for stage, path in old_files.items()}
        manifest_revision = self.task.snapshot()["revision"]
        with patch.object(self.task, "_selection_changed", wraps=self.task._selection_changed) as changed, \
             patch.object(tasks, "_atomic_manifest", wraps=tasks._atomic_manifest) as commit:
            self.task.set_keep("shortlist", ["c", "a", "a"], True,
                               replace_selection=True, expected_stage=before)
        self.assertEqual(changed.call_count, 1)
        self.assertEqual(commit.call_count, 1)
        info = self.task.stage_info("shortlist")
        self.assertEqual(info["keep_ids"], ["a", "c"])
        self.assertEqual(info["excluded_ids"], ["b"])
        self.assertEqual(info["manual_choices"], {"a": True, "b": False, "c": True})
        self.assertEqual(info["revision"], before["revision"] + 1)
        self.assertEqual(self.task.snapshot()["revision"], manifest_revision + 1)
        self.assertFalse(info["confirmed"])
        self.assertEqual(before, frozen, "the optimistic snapshot belongs to the caller")
        for stage in tasks.STAGES[2:]:
            self.assertEqual(self.task.stage_info(stage)["status"], "stale")
            self.assertEqual(old_files[stage].read_bytes(), old_contents[stage])
        self.assertEqual(self.task.confirm("shortlist"), ["a", "c"])
        self.assertEqual([tasks.job_id(job) for job in self.task.input_jobs("fetch-jd")], ["a", "c"])
        self.assertEqual(self.raw.read_bytes(), raw)

    def test_empty_replacement_and_an_empty_completed_result_are_valid(self):
        self.prepare_shortlist()
        self.task.set_keep("shortlist", [], True, replace_selection=True,
                           expected_stage=self.task.stage_info("shortlist"))
        info = self.task.stage_info("shortlist")
        self.assertEqual(info["keep_ids"], [])
        self.assertEqual(info["manual_choices"], {key: False for key in "abc"})
        self.assertEqual(self.task.confirm("shortlist"), [])
        self.assertEqual(self.task.input_jobs("fetch-jd"), [])
        self.task.begin("fetch-jd")
        self.task.finish("fetch-jd", [])
        expected = self.task.stage_info("fetch-jd")
        before = self.task.snapshot()
        self.task.set_keep("fetch-jd", [], True, replace_selection=True, expected_stage=expected)
        self.assertEqual(self.task.snapshot(), before)

    def test_replacement_preserves_out_of_scope_manual_choices_and_their_rerun_overrides(self):
        self.prepare_shortlist()
        self.task.set_keep("shortlist", ["b"], False)
        self.task.set_keep("shortlist", ["c"], True)
        self.task.set_keep("import", ["b", "c"], False)
        self.task.confirm("import")
        self.run_stage("shortlist", {"a": {"list_pass": True}}, confirm=False)
        expected = self.task.stage_info("shortlist")
        self.assertEqual(expected["result_ids"], ["a"])
        self.assertEqual(expected["manual_choices"], {"b": False, "c": True})

        self.task.set_keep("shortlist", [], True, replace_selection=True, expected_stage=expected)
        choices = {"a": False, "b": False, "c": True}
        self.assertEqual(self.task.stage_info("shortlist")["manual_choices"], choices)
        self.assertEqual(self.task.confirm("shortlist"), [])

        # Returning inputs must retain both historical exclusion and inclusion,
        # even when the newly saved recommendations disagree with each one.
        self.task.set_keep("import", ["b", "c"], True)
        self.task.confirm("import")
        self.run_stage("shortlist", {key: {"list_pass": key != "c"} for key in "abc"}, confirm=False)
        info = self.task.stage_info("shortlist")
        self.assertEqual(info["result_ids"], list("abc"))
        self.assertEqual(info["manual_choices"], choices)
        self.assertEqual(info["keep_ids"], ["c"])
        self.assertEqual(self.task.confirm("shortlist"), ["c"])

    def test_invalid_replacement_arguments_and_snapshots_never_partially_commit(self):
        self.prepare_shortlist()
        expected = self.task.stage_info("shortlist")
        malformed_choices = copy.deepcopy(expected)
        malformed_choices["manual_choices"] = {"a": "PRIVATE_SECRET"}
        malformed_revision = copy.deepcopy(expected)
        malformed_revision["revision"] = True
        variants = (
            (["a"], True, {"replace_selection": 1}),
            (["a"], True, {"replace_selection": None}),
            (["a"], True, {"replace_selection": "yes"}),
            (["a"], False, {"replace_selection": True}),
            (["missing"], True, {"replace_selection": True, "expected_stage": expected}),
            (("a",), True, {"replace_selection": True}),
            ([None], True, {"replace_selection": True}),
            (["a"], 1, {"replace_selection": True}),
            (["a"], True, {"expected_stage": []}),
            (["a"], True, {"expected_stage": {}}),
            (["a"], True, {"expected_stage": "PRIVATE_SECRET"}),
            (["a"], True, {"expected_stage": malformed_choices}),
            (["a"], True, {"expected_stage": malformed_revision}),
        )
        before = self.task.snapshot()
        content = (self.task.path / "task_manifest.json").read_bytes()
        for identities, keep, options in variants:
            with self.subTest(identities=identities, keep=keep, options=options):
                with self.assertRaises(tasks.TaskStateError) as error:
                    self.task.set_keep("shortlist", identities, keep, **options)
                self.assertNotIn("PRIVATE_SECRET", str(error.exception))
                self.assertEqual(self.task.snapshot(), before)
                self.assertEqual((self.task.path / "task_manifest.json").read_bytes(), content)

    def test_another_task_window_selection_change_is_rejected_inside_the_transaction(self):
        self.prepare_shortlist()
        expected = self.task.stage_info("shortlist")
        other = tasks.ReviewTask.open(self.task.path)
        other.set_keep("shortlist", ["a"], False)
        saved = other.snapshot()
        for replace in (False, True):
            with self.subTest(replace=replace), self.assertRaisesRegex(tasks.TaskStateError, "其他窗口修改"):
                self.task.set_keep("shortlist", ["b"], True, replace_selection=replace, expected_stage=expected)
            self.assertEqual(other.snapshot(), saved)
        self.assertEqual(self.task.stage_info("shortlist")["manual_choices"], {"a": False})

    def test_new_manual_intent_is_protected_even_when_stage_revision_did_not_change(self):
        self.prepare_shortlist()
        expected = self.task.stage_info("shortlist")
        other = tasks.ReviewTask.open(self.task.path)
        other.set_keep("shortlist", ["b"], True)  # Same keep, but newly explicit intent.
        current = other.stage_info("shortlist")
        self.assertEqual(current["revision"], expected["revision"])
        self.assertEqual(current["keep_ids"], expected["keep_ids"])
        self.assertNotEqual(tasks.stage_selection_version(current), tasks.stage_selection_version(expected))
        saved = other.snapshot()
        for replace in (False, True):
            with self.subTest(replace=replace), self.assertRaisesRegex(tasks.TaskStateError, "其他窗口修改"):
                self.task.set_keep("shortlist", ["a"], True, replace_selection=replace, expected_stage=expected)
            self.assertEqual(other.snapshot(), saved)
        self.assertEqual(other.stage_info("shortlist")["manual_choices"], {"b": True})

    def test_selection_token_covers_identity_input_ids_confirmation_and_manual_intent(self):
        self.prepare_shortlist()
        current = self.task.stage_info("shortlist")
        variants = {
            "status": "paused", "result_file": "different-results.json", "revision": current["revision"] + 1,
            "result_input_revision": current["result_input_revision"] + 1, "confirmed": True,
            "confirmed_revision": current["revision"], "input_file": "different-input.json",
            "input_revision": current["input_revision"] + 1, "result_input_file": "different-input.json",
            "result_ids": ["a", "b"], "keep_ids": ["a"], "input_ids": ["a", "b"],
            "result_input_ids": ["a", "b"], "excluded_ids": ["b"], "recommended_keep_ids": ["a"],
            "manual_choices": {"a": True},
        }
        saved = self.task.snapshot()
        for field, value in variants.items():
            with self.subTest(field=field):
                expected = copy.deepcopy(current)
                expected[field] = value
                self.assertNotEqual(tasks.stage_selection_version(current), tasks.stage_selection_version(expected))
                with self.assertRaises(tasks.TaskStateError):
                    self.task.set_keep("shortlist", ["a"], True, replace_selection=True, expected_stage=expected)
                self.assertEqual(self.task.snapshot(), saved)
        incomplete = copy.deepcopy(current)
        incomplete.pop("manual_choices")
        with self.assertRaises(tasks.TaskStateError):
            tasks.stage_selection_version(incomplete)

    def test_replacement_holds_one_real_edit_lease_until_the_complete_manifest_commit(self):
        self.prepare_shortlist()
        other = tasks.ReviewTask.open(self.task.path)
        expected = self.task.stage_info("shortlist")
        entered, release = threading.Event(), threading.Event()
        errors, proposed = [], []
        original = tasks._atomic_manifest

        def hold_commit(path, value):
            proposed.append(copy.deepcopy(value["stages"]["shortlist"]))
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release the commit")
            original(path, value)

        def replace():
            try:
                self.task.set_keep("shortlist", ["c"], True, replace_selection=True, expected_stage=expected)
            except BaseException as error:
                errors.append(error)

        with patch.object(tasks, "_atomic_manifest", side_effect=hold_commit) as commit:
            worker = threading.Thread(target=replace)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(other.stage_info("shortlist")["keep_ids"], list("abc"), "disk stays at the prior complete snapshot until commit")
                with self.assertRaisesRegex(tasks.TaskStateError, "任务正在更新"):
                    other.set_keep("shortlist", ["a"], False)
                self.assertEqual(proposed[0]["manual_choices"], {"a": False, "b": False, "c": True})
            finally:
                release.set()
                worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(commit.call_count, 1)
        self.assertEqual(other.stage_info("shortlist")["keep_ids"], ["c"])
        # A later edit is a newer user decision, not an interleaved half-write.
        other.set_keep("shortlist", ["a"], True)
        self.assertEqual(self.task.stage_info("shortlist")["keep_ids"], ["a", "c"])

    def test_replacement_never_bypasses_running_stale_or_read_only_report_guards(self):
        self.prepare_shortlist()
        expected = self.task.stage_info("shortlist")
        self.task.begin("shortlist")
        with self.assertRaises(tasks.TaskStateError):
            self.task.set_keep("shortlist", ["a"], True, replace_selection=True, expected_stage=expected)
        self.task.interrupt("shortlist")
        self.task.set_keep("import", ["a"], False)
        saved = self.task.snapshot()
        with self.assertRaises(tasks.TaskStateError):
            self.task.set_keep("shortlist", ["b"], True, replace_selection=True,
                               expected_stage=self.task.stage_info("shortlist"))
        self.assertEqual(self.task.snapshot(), saved)
        self.run_all()
        saved = self.task.snapshot()
        with self.assertRaises(tasks.TaskStateError):
            self.task.set_keep("report", ["b"], True, replace_selection=True,
                               expected_stage=self.task.stage_info("report"))
        self.assertEqual(self.task.snapshot(), saved)


class PersistenceTests(TaskTestCase):
    def test_legacy_manifest_without_redo_count_opens_without_rewrite(self):
        self.run_import()
        manifest_path = self.task.path / "task_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for info in manifest["stages"].values():
            info.pop("redo_count", None)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        before = manifest_path.read_bytes()
        reopened = tasks.ReviewTask.open(self.task.path)
        self.addCleanup(reopened._release_run)
        self.assertEqual(reopened.stage_info("import")["redo_count"], 0)
        self.assertEqual(manifest_path.read_bytes(), before)

    def test_legacy_ai_default_keep_migrates_in_memory_consistently_and_edit_persists_safe_partition(self):
        self.run_import()
        self.run_stage("shortlist", {key: {"list_pass": True} for key in "abc"})
        self.task.begin("fetch-jd")
        fetched = self.task.input_jobs("fetch-jd")
        for row in fetched:
            row.update(jd=f"职责 {row['job_id']}", jd_completeness="complete")
        self.task.finish("fetch-jd", fetched)
        self.task.confirm("fetch-jd")
        self.task.begin("llm")
        reviewed = []
        for key, judgment in (("a", "符合"), ("b", "不符合"), ("c", "信息不足")):
            reviewed.append({
                "job_id": key,
                "llm_review": {"job_id": key, "status": "success", "review_format": "simple-v1",
                               "判断": judgment, "理由": "fixture judgment"},
            })
        self.task.finish("llm", reviewed)
        self.task.set_keep("llm", ["a"], False)  # A real override on a definite judgment survives.

        manifest_path = self.task.path / "task_manifest.json"
        result_path = self.task.path / self.task.stage_info("llm")["result_file"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        results = json.loads(result_path.read_text(encoding="utf-8"))
        for row in results["jobs"]:
            row["recommended_keep"] = True  # Old checkpoints treated every pending row as a keep.
        result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        llm = manifest["stages"]["llm"]
        llm["keep_ids"], llm["excluded_ids"] = list("abc"), []
        llm["recommended_keep_ids"] = list("abc")
        llm.pop("recommended_exclude_ids", None)
        llm.pop("pending_ids", None)
        llm["manual_choices"] = {"a": False, "c": True, "outside-old-input": True}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        legacy_bytes = manifest_path.read_bytes()

        reopened = tasks.ReviewTask.open(self.task.path)
        self.addCleanup(reopened._release_run)
        for state in (reopened.stage_info("llm"), reopened.refresh()["stages"]["llm"],
                      reopened._load()["stages"]["llm"]):
            self.assertEqual(state["recommended_keep_ids"], ["a"])
            self.assertEqual(state["recommended_exclude_ids"], ["b"])
            self.assertEqual(state["pending_ids"], ["c"])
            self.assertEqual(state["keep_ids"], [])
            self.assertEqual(state["excluded_ids"], ["a", "b"])
            self.assertEqual(state["manual_choices"], {"a": False, "outside-old-input": True})
        self.assertIsNone(next(row for row in reopened.jobs("llm") if row["job_id"] == "c")["recommended_keep"])
        self.assertEqual(manifest_path.read_bytes(), legacy_bytes, "read-only migration must not rewrite an old manifest")

        reopened.set_keep("llm", ["b"], True)
        saved = reopened.stage_info("llm")
        self.assertEqual(saved["keep_ids"], ["b"])
        self.assertEqual(saved["excluded_ids"], ["a"])
        self.assertEqual(saved["pending_ids"], ["c"])
        self.assertEqual(saved["manual_choices"], {"a": False, "b": True, "outside-old-input": True})
        with self.assertRaisesRegex(tasks.TaskStateError, "只能待核实"):
            reopened.set_keep("llm", ["c"], False)

    def test_save_open_round_trip_keeps_scope_choices_progress_and_audit(self):
        self.run_import()
        self.run_stage("shortlist", {key: {"list_pass": True} for key in "abc"}, confirm=False)
        self.task.set_keep("shortlist", ["b"], False)
        self.task.confirm("shortlist")
        self.task.begin("fetch-jd")
        self.task.record_progress("fetch-jd", 1, 2, {"fetched": 1, "nested": {"ok": True}}, "one fetched")
        self.task.interrupt("fetch-jd", "paused", "暂停待继续", {"cached": True})
        state = self.task.snapshot()
        reopened = tasks.ReviewTask.open(self.task.path / "task_manifest.json")
        self.addCleanup(reopened._release_run)
        self.assertEqual(reopened.snapshot(), state)
        self.assertEqual(reopened.all_jobs(), self.task.all_jobs())
        self.assertEqual(reopened.stage_info("fetch-jd")["progress"], {"done": 1, "total": 2, "message": "one fetched"})
        reopened.begin("fetch-jd")
        self.assertEqual(reopened.stage_info("fetch-jd")["progress"]["done"], 1)
        reopened.record_progress("fetch-jd", 2, 2, {"done": True})
        reopened.finish("fetch-jd", [{"job_id": "a", "jd": "A"}, {"job_id": "c", "jd": "C"}], stats={"final": True})
        stats = reopened.stage_info("fetch-jd")["stats"]
        self.assertEqual(stats, {"fetched": 1, "nested": {"ok": True}, "cached": True, "done": True, "final": True})
        self.assertFalse(reopened.stage_info("fetch-jd")["confirmed"])

    def test_waiting_failed_and_stopped_are_not_completed_or_confirmable(self):
        self.run_import()
        for status in ("waiting", "failed", "stopped", "paused"):
            with self.subTest(status=status):
                self.task.begin("shortlist")
                self.task.finish("shortlist", [{"job_id": key, "list_pass": True} for key in "abc"],
                                 status=status, issues=["人工处理后继续"])
                self.assertEqual(self.task.stage_info("shortlist")["status"], status)
                with self.assertRaises(tasks.TaskStateError):
                    self.task.confirm("shortlist")
        self.task.begin("shortlist")
        for status in ("completed", "pending", "running", "stale", "unknown"):
            with self.assertRaises(tasks.TaskStateError):
                self.task.interrupt("shortlist", status=status)
        for status in ("pending", "running", "stale", "unknown"):
            with self.assertRaises(tasks.TaskStateError):
                self.task.finish("shortlist", [], status=status)
        self.task.interrupt("shortlist", "failed")

    def test_active_open_does_not_interrupt_and_external_edit_or_finish_is_denied(self):
        self.run_import()
        self.task.begin("shortlist")
        other = tasks.ReviewTask.open(self.task.path)
        self.assertEqual(other.stage_info("shortlist")["status"], "running")
        for action in (lambda: other.set_keep("import", ["a"], False),
                       lambda: other.confirm("import"), lambda: other.begin("import"),
                       lambda: other.interrupt("shortlist"),
                       lambda: other.record_progress("shortlist", 0, 3)):
            with self.assertRaises(tasks.TaskStateError):
                action()
        self.task.interrupt("shortlist")
        other.set_keep("import", ["b"], False)
        self.assertEqual(self.task.refresh()["stages"]["import"]["keep_ids"], ["a", "c"])

    def test_real_process_exit_releases_run_lease_and_recovers_paused(self):
        context = multiprocessing.get_context("spawn")
        started, finish = context.Event(), context.Event()
        process = context.Process(target=_abandon_running_task, args=(str(self.task.path), started, finish))
        process.start()
        try:
            self.assertTrue(started.wait(10), "child did not begin")
            active = tasks.ReviewTask.open(self.task.path)
            self.assertEqual(active.stage_info("import")["status"], "running")
            finish.set()
            process.join(10)
            self.assertEqual(process.exitcode, 0)
            recovered = tasks.ReviewTask.open(self.task.path)
            self.addCleanup(recovered._release_run)
            info = recovered.stage_info("import")
            self.assertEqual(info["status"], "paused")
            self.assertEqual(info["progress"], {"done": 1, "total": 3})
            self.assertEqual(info["stats"]["raw_record_count"], 3)
            recovered.begin("import")
            recovered.finish("import", self.imported())
            self.assertEqual(recovered.stage_info("import")["status"], "completed")
        finally:
            finish.set()
            if process.is_alive():
                process.terminate()
            process.join(5)

    def test_failed_atomic_replace_preserves_prior_manifest_and_returns_safe_error(self):
        manifest_path = self.task.path / "task_manifest.json"
        original = manifest_path.read_bytes()
        with patch.object(tasks.os, "replace", side_effect=OSError("api_key=private-do-not-print")):
            with self.assertRaises(tasks.TaskStateError) as caught:
                self.task.begin("import")
        self.assertNotIn("private-do-not-print", str(caught.exception))
        self.assertEqual(manifest_path.read_bytes(), original)
        self.assertEqual(self.task.stage_info("import")["status"], "pending")
        self.assertEqual(list(self.task.path.glob(".task-manifest-*.tmp")), [])
        self.task.begin("import")
        self.task.interrupt("import")

    def test_issues_progress_stats_and_exception_messages_redact_credentials(self):
        self.task.begin("import")
        self.task.record_progress("import", 1, 3, stats={"api_key": "private-a", "nested": {"cookie": "private-b"}},
                                  message="Authorization: Bearer private-c")
        self.task.interrupt("import", "waiting", "Cookie=a=private-d; b=private-e", {"request": "https://u:private-f@example.test/?token=private-g"})
        text = (self.task.path / "task_manifest.json").read_text(encoding="utf-8")
        for secret in ("private-a", "private-b", "private-c", "private-d", "private-e", "private-f", "private-g"):
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("private-a", str(tasks.TaskStateError("api_key=private-a")))
        self.task.begin("import")
        self.task.finish("import", self.imported(), issues=[{"authorization": "private-a"}, "Bearer private-b"])
        self.assertNotIn("private-a", json.dumps(self.task.stage_info("import")["issues"]))
        self.assertNotIn("private-b", json.dumps(self.task.stage_info("import")["issues"]))

    def test_invalid_json_and_invalid_manifest_shapes_produce_state_errors(self):
        path = self.task.path / "task_manifest.json"
        original = path.read_text(encoding="utf-8")
        invalid = ["{", "[]", "null", "NaN"]
        malformed = json.loads(original)
        malformed["stages"]["import"]["status"] = []
        invalid.append(json.dumps(malformed))
        for raw in invalid:
            with self.subTest(raw=raw[:30]):
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(tasks.TaskStateError):
                    tasks.ReviewTask.open(self.task.path)
        path.write_text(original, encoding="utf-8")

    def test_corrupt_state_sets_versions_history_and_baseline_are_rejected(self):
        self.run_all()
        original = self.task.snapshot()
        changes = [
            ("shortlist", "keep_ids", ["unknown"]),
            ("shortlist", "excluded_ids", ["a"]),
            ("shortlist", "result_ids", ["a", "a"]),
            ("shortlist", "progress", {"done": 4, "total": 3}),
            ("shortlist", "input_revision", "wrong"),
            ("shortlist", "result_file", None),
            ("shortlist", "manual_choices", {"a": "yes"}),
            ("shortlist", "history", [{"result_file": "../external.json"}]),
            ("import", "confirmed", False),
        ]
        manifest_path = self.task.path / "task_manifest.json"
        for stage, field, value in changes:
            with self.subTest(stage=stage, field=field):
                corrupted = copy.deepcopy(original)
                corrupted["stages"][stage][field] = value
                self.write_json(manifest_path, corrupted)
                with self.assertRaises(tasks.TaskStateError):
                    tasks.ReviewTask.open(self.task.path)
        for field, value in (("revision", "wrong"), ("baseline_ids", None), ("import_baseline_file", None)):
            corrupted = copy.deepcopy(original)
            corrupted[field] = value
            self.write_json(manifest_path, corrupted)
            with self.assertRaises(tasks.TaskStateError):
                tasks.ReviewTask.open(self.task.path)
        self.write_json(manifest_path, original)
        baseline_file = self.task.path / original["import_baseline_file"]
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
        baseline["jobs"].pop()
        self.write_json(baseline_file, baseline)
        with self.assertRaises(tasks.TaskStateError):
            self.task.all_jobs()

    def test_partial_import_does_not_freeze_a_truncated_baseline(self):
        self.task.begin("import")
        self.task.finish("import", [{"job_id": "a"}], status="paused")
        self.assertIsNone(self.task.manifest["baseline_ids"])
        self.task.set_keep("import", ["a"], False)
        reopened = tasks.ReviewTask.open(self.task.path)
        self.addCleanup(reopened._release_run)
        reopened.begin("import")
        reopened.finish("import", self.imported())
        self.assertEqual(reopened.manifest["baseline_ids"], list("abc"))
        self.assertEqual(reopened.stage_info("import")["keep_ids"], ["b", "c"])
        self.assertEqual(len(reopened.all_jobs()), 3)

    def test_invalid_progress_and_unserializable_data_do_not_mutate(self):
        self.task.begin("import")
        original = self.task.snapshot()
        for done, total in ((-1, 3), (4, 3), (True, 3), (0, -1), (1.5, 3)):
            with self.assertRaises(tasks.TaskStateError):
                self.task.record_progress("import", done, total)
        for bad in ({"value": float("nan")}, {"value": object()}):
            with self.assertRaises(tasks.TaskStateError):
                self.task.record_progress("import", 0, 3, stats=bad)
        cyclic = {}
        cyclic["cycle"] = cyclic
        with self.assertRaises(tasks.TaskStateError):
            self.task.record_progress("import", 0, 3, stats=cyclic)
        with self.assertRaises(tasks.TaskStateError):
            self.task.finish("import", [], status=[])
        self.assertEqual(self.task.snapshot(), original)
        self.task.interrupt("import")


class ScopeAndPathTests(TaskTestCase):
    def test_opening_a_non_task_fails_before_walking_unrelated_data(self):
        with patch.object(tasks, "_walk_ordinary", side_effect=AssertionError("walked non-task")):
            with self.assertRaises(tasks.TaskStateError):
                tasks.ReviewTask.open(self.source)

    def test_tasks_and_runtime_are_unique_and_never_overwrite_raw_or_existing_outputs(self):
        old = self.write_json(self.source / "prepared" / "merged_jobs_index.json", {"old": True})
        raw_before, old_before = self.raw.read_bytes(), old.read_bytes()
        other = self.create()
        self.assertNotEqual(other.path, self.task.path)
        for stage in tasks.STAGES:
            directory = self.task.run_directory(stage)
            self.assertEqual(directory, self.task.path / "runtime" / stage)
            self.assertTrue(directory.is_dir())
            directory.resolve().relative_to(self.task.path.resolve())
            self.assertNotEqual(directory, other.run_directory(stage))
        self.run_all()
        self.assertEqual(self.raw.read_bytes(), raw_before)
        self.assertEqual(old.read_bytes(), old_before)

    def test_task_creation_rejects_partial_roots_raw_prepared_parents_and_bad_range(self):
        partial = self.root / "partial"
        (partial / "raw" / "current").mkdir(parents=True)
        with self.assertRaises(tasks.TaskStateError):
            tasks.ReviewTask.create(partial, self.root / "tasks", "bad")
        for parent in (self.source / "raw", self.source / "raw" / "current", self.source / "prepared"):
            with self.subTest(parent=parent):
                with self.assertRaises(tasks.TaskStateError):
                    tasks.ReviewTask.create(self.source, parent, "bad")
        for options in ({"sample_size": -1}, {"sample_size": True}, {"track": "unknown"}, {"city": " "}):
            with self.assertRaises(tasks.TaskStateError):
                self.create(**options)
        for name in ("", " ", "a" * 121):
            with self.assertRaises(tasks.TaskStateError):
                tasks.ReviewTask.create(self.source, self.root / "tasks", name)

    def test_name_is_a_label_not_a_path_and_unknown_stage_is_rejected(self):
        task = tasks.ReviewTask.create(self.source, self.root / "tasks", "../../raw/../old-result")
        self.assertEqual(task.path.parent, self.root / "tasks")
        self.assertEqual(task.manifest["name"], "../../raw/../old-result")
        for action in (lambda: task.run_directory("../raw"), lambda: task.begin("../raw"),
                       lambda: task.confirm("../raw"), lambda: task.jobs("../raw")):
            with self.assertRaises(tasks.TaskStateError):
                action()

    def test_source_file_add_change_delete_and_config_change_require_new_task(self):
        self.raw.write_text('{"jobs":[]}', encoding="utf-8")
        with self.assertRaises(tasks.TaskStateError):
            self.task.begin("import")
        fresh = self.create()
        self.write_json(self.source / "raw" / "target" / "shanghai" / "new.json", {"jobs": []})
        with self.assertRaises(tasks.TaskStateError):
            fresh.begin("import")
        fresh = self.create()
        self.raw.unlink()
        with self.assertRaises(tasks.TaskStateError):
            fresh.begin("import")
        fresh = self.create()
        self.config.write_text("{}", encoding="utf-8")
        with self.assertRaises(tasks.TaskStateError):
            fresh.begin("import")

    def test_import_detects_source_mutation_during_execution(self):
        self.task.begin("import")
        self.raw.write_text('{"jobs":[]}', encoding="utf-8")
        with self.assertRaises(tasks.TaskStateError):
            self.task.finish("import", self.imported())
        self.assertEqual(self.task.stage_info("import")["status"], "running")
        self.task.interrupt("import", "failed", "固定原始数据已改变")

    def test_import_baseline_ids_cannot_change_and_later_stages_use_snapshot(self):
        self.run_import()
        self.task.begin("import")
        with self.assertRaises(tasks.TaskStateError):
            self.task.finish("import", [{"job_id": "different"}])
        self.task.finish("import", [{"job_id": key} for key in "cba"])
        self.task.confirm("import")
        self.assertEqual([tasks.job_id(job) for job in self.task.jobs("import")], list("abc"))
        self.raw.write_text("{}", encoding="utf-8")
        # Processing a saved import does not re-read, resample, or mix new raw.
        self.run_stage("shortlist", {key: {"list_pass": True} for key in "abc"})
        self.assertEqual([tasks.job_id(job) for job in self.task.all_jobs()], list("abc"))
        self.assertEqual(self.task.all_jobs()[0]["title"], "架构方案 A")

    def test_scope_mutation_and_external_result_references_are_rejected(self):
        self.run_import()
        manifest_path = self.task.path / "task_manifest.json"
        original = self.task.snapshot()
        for key, value in (("city", "北京"), ("track", "target"), ("source_root", str(self.root))):
            corrupted = copy.deepcopy(original)
            corrupted[key] = value
            self.write_json(manifest_path, corrupted)
            with self.assertRaises(tasks.TaskStateError):
                tasks.ReviewTask.open(self.task.path)
        for relative in ("../external.json", str(self.raw), "stages/../outside.json", "stages/import/input-a.json"):
            corrupted = copy.deepcopy(original)
            corrupted["stages"]["import"]["result_file"] = relative
            self.write_json(manifest_path, corrupted)
            with self.assertRaises(tasks.TaskStateError):
                tasks.ReviewTask.open(self.task.path)
        self.write_json(manifest_path, original)

    def test_external_task_parent_link_or_junction_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "linked-tasks"
        self.link_directory(outside, link)
        with self.assertRaises(tasks.TaskStateError):
            tasks.ReviewTask.create(self.source, link, "bad")
        self.assertEqual(list(outside.iterdir()), [])

    def test_external_raw_link_or_junction_is_rejected_without_reading_target(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.write_json(outside / "secret.json", {"api_key": "private-never-read"})
        link = self.source / "raw" / "target" / "linked-city"
        self.link_directory(outside, link)
        with self.assertRaises(tasks.TaskStateError) as caught:
            self.create()
        self.assertNotIn("private-never-read", str(caught.exception))
        with self.assertRaises(tasks.TaskStateError):
            self.task.begin("import")

    def test_external_runtime_link_or_junction_is_rejected_on_use_and_open(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.task.path / "runtime" / "fetch-jd"
        self.link_directory(outside, link)
        with self.assertRaises(tasks.TaskStateError):
            self.task.run_directory("fetch-jd")
        with self.assertRaises(tasks.TaskStateError):
            tasks.ReviewTask.open(self.task.path)
        self.assertEqual(list(outside.iterdir()), [])

    def test_external_nested_runtime_link_or_junction_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        runtime = self.task.run_directory("fetch-jd")
        self.link_directory(outside, runtime / "prepared")
        with self.assertRaises(tasks.TaskStateError):
            self.task.run_directory("fetch-jd")
        self.assertEqual(list(outside.iterdir()), [])


class StageRedoTests(TaskTestCase):
    def test_reset_only_clears_one_stage_and_next_normal_begin_starts_fresh(self):
        legacy = "旧规则：六项职责命中至少四项，提供原文证据和能力差距。"
        self.task.set_review_rules(legacy)
        self.run_all()
        before = self.task.snapshot()
        old_runtime = self.task.run_directory("llm")
        runtime_marker = self.write_json(old_runtime / "prepared" / "old-cache.json", {"old": True})
        old_llm_result = self.task.path / before["stages"]["llm"]["result_file"]
        old_llm_input = self.task.path / before["stages"]["llm"]["input_file"]
        upstream = {stage: before["stages"][stage] for stage in ("import", "shortlist", "fetch-jd")}
        old_report_runtime = self.task.run_directory("report")

        self.task.reset_stage("llm")

        reset = self.task.snapshot()
        info = reset["stages"]["llm"]
        self.assertEqual(info["status"], "pending")
        self.assertEqual(info["progress"], {"done": 0, "total": 0})
        self.assertEqual(info["attempt"], 0)
        self.assertEqual(info["redo_count"], before["stages"]["llm"]["redo_count"] + 1)
        self.assertTrue(info["reset_pending"])
        self.assertIsNone(info["result_file"])
        self.assertIsNone(info["input_file"])
        self.assertEqual(info["stats"], {})
        self.assertEqual(info["issues"], [])
        self.assertEqual(info["manual_choices"], {})
        self.assertEqual(self.task.get_review_rules(), legacy, "reset must not mutate rules before execution")
        self.assertFalse(old_runtime.exists())
        self.assertFalse(runtime_marker.exists())
        self.assertFalse(old_llm_result.exists())
        self.assertFalse(old_llm_input.exists())
        self.assertTrue(old_report_runtime.exists(), "only this stage's runtime is removed")
        for stage, stage_info in upstream.items():
            self.assertEqual(reset["stages"][stage], stage_info)
        self.assertEqual(reset["stages"]["report"]["status"], "stale")

        self.task.begin("llm")
        self.assertFalse(self.task.stage_info("llm")["reset_pending"])
        self.assertEqual(self.task.stage_info("llm")["attempt"], 1)
        self.assertEqual(self.task.get_review_rules(), tasks.review_rules.default_review_rules())
        self.assertNotEqual(self.task.run_directory("llm"), old_runtime)
        self.task.finish("llm", self.task.input_jobs("llm"))

    def test_reset_refuses_active_executor_and_unrun_stage(self):
        before = self.task.snapshot()
        with self.assertRaises(tasks.TaskStateError):
            self.task.reset_stage("import")
        self.assertEqual(self.task.snapshot(), before)

        self.run_import()
        self.task.begin("shortlist")
        running = self.task.snapshot()
        with self.assertRaises(tasks.TaskStateError):
            self.task.reset_stage("shortlist")
        self.assertEqual(self.task.snapshot(), running)
        self.task.interrupt("shortlist")

    def test_each_stage_reset_only_deletes_its_outputs_and_waits_for_explicit_run(self):
        for stage in tasks.STAGES:
            with self.subTest(stage=stage):
                self.task = self.create()
                self.addCleanup(self.task._release_run)
                self.run_all()
                if stage != "report":
                    self.task.set_keep(stage, ["a"], False)
                    self.task.confirm(stage)
                old = self.task.snapshot()
                runtime = self.task.run_directory(stage)
                checkpoint = self.write_json(runtime / "completed.json", {"success": "old"})
                result = self.task.path / old["stages"][stage]["result_file"]
                input_checkpoint = self.task.path / old["stages"][stage]["input_file"]
                inputs = self.imported() if stage == "import" else self.task.input_jobs(stage)
                raw_sample = self.task.path / "raw_sample.json"
                raw_before = raw_sample.read_bytes() if raw_sample.exists() else None
                later_files = {path: path.read_bytes() for later in tasks.STAGES[tasks.STAGES.index(stage) + 1:]
                               for path in self.task.run_directory(later).rglob("*") if path.is_file()}
                self.task.reset_stage(stage)
                reset = self.task.stage_info(stage)
                self.assertEqual(reset["status"], "pending")
                self.assertTrue(reset["reset_pending"])
                self.assertEqual(reset["redo_count"], 1)
                self.assertEqual(reset["attempt"], 0)
                self.assertNotEqual(self.task.run_directory(stage), runtime)
                self.assertFalse(runtime.exists())
                self.assertFalse(result.exists())
                self.assertFalse(input_checkpoint.exists())
                self.assertFalse(checkpoint.exists())
                self.assertEqual(self.task.jobs(stage), [])
                self.assertEqual(reset["stats"], {})
                self.assertFalse(reset["confirmed"])
                self.assertEqual(reset["manual_choices"], {})
                self.assertEqual(reset["history"], [])
                self.assertIsNone(reset["input_file"])
                self.assertIsNone(reset["result_file"])
                if stage == "import":
                    self.assertFalse(raw_sample.exists())
                elif raw_before is not None:
                    self.assertEqual(raw_sample.read_bytes(), raw_before)
                for earlier in tasks.STAGES[:tasks.STAGES.index(stage)]:
                    self.assertEqual(self.task.stage_info(earlier), old["stages"][earlier])
                for later in tasks.STAGES[tasks.STAGES.index(stage) + 1:]:
                    self.assertEqual(self.task.stage_info(later)["status"], "stale")
                    self.assertFalse(self.task.stage_info(later)["confirmed"])
                self.task.begin(stage)
                self.assertEqual(self.task.stage_info(stage)["status"], "running")
                self.task.finish(stage, inputs)
                current_info = self.task.stage_info(stage)
                self.assertTrue((self.task.path / current_info["result_file"]).exists())
                self.assertTrue((self.task.path / current_info["input_file"]).exists())
                self.assertTrue(all(path.exists() and path.read_bytes() == content
                                    for path, content in later_files.items()))

    def test_reset_removes_owned_runtime_and_checkpoint_files_without_new_input(self):
        self.run_import()
        runtime = self.task.run_directory("import")
        checkpoint = self.task.path / self.task.stage_info("import")["result_file"]
        runtime_cache = runtime / "prepared" / "test-output.json"
        self.write_json(runtime_cache, {"owned": True})
        self.task.reset_stage("import")
        self.assertFalse(runtime.exists())
        self.assertFalse(checkpoint.exists())
        self.assertIsNone(self.task.stage_info("import")["input_file"])
        self.assertEqual(self.task.stage_info("import")["status"], "pending")
        self.assertEqual(self.task.stage_info("import")["progress"], {"done": 0, "total": 0})
        self.task.begin("import")
        self.assertTrue((self.task.path / self.task.stage_info("import")["input_file"]).exists())

    def test_redo_pause_stop_failure_and_crash_resume_the_same_new_round(self):
        self.run_import()
        for status in ("paused", "stopped", "waiting", "failed", "crash"):
            with self.subTest(status=status):
                self.task.reset_stage("import")
                self.task.begin("import")
                runtime = self.task.run_directory("import")
                self.write_json(runtime / "partial.json", {"status": status})
                number = self.task.stage_info("import")["redo_count"]
                self.task.record_progress("import", 1, 3, stats={"processed": 1})
                if status == "crash":
                    self.task._release_run()
                    self.task = tasks.ReviewTask.open(self.task.path)
                    self.addCleanup(self.task._release_run)
                    self.assertEqual(self.task.stage_info("import")["status"], "paused")
                else:
                    self.task.interrupt("import", status=status)
                self.task.begin("import")
                self.assertEqual(self.task.run_directory("import"), runtime)
                self.assertEqual(self.task.stage_info("import")["redo_count"], number)
                self.assertEqual(self.task.stage_info("import")["progress"], {"done": 1, "total": 3})
                self.assertEqual(self.task.stage_info("import")["stats"]["processed"], 1)
                self.task.finish("import", self.imported())

    def test_first_run_and_invalid_redo_options_do_not_change_the_task(self):
        before = self.task.snapshot()
        for value in (True, "yes", 1, None):
            with self.subTest(value=value), self.assertRaises(tasks.TaskStateError):
                self.task.begin("import", redo=value)
            self.assertEqual(self.task.snapshot(), before)
        self.assertEqual(list((self.task.path / "runtime").iterdir()), [])

    def test_redo_rejects_active_executor_and_invalid_upstream_without_resetting(self):
        self.run_all()
        other = tasks.ReviewTask.open(self.task.path)
        self.addCleanup(other._release_run)
        other.begin("llm")
        before = self.task.snapshot()
        with self.assertRaises(tasks.TaskStateError):
            self.task.reset_stage("import")
        self.assertEqual(self.task.snapshot(), before)
        other.interrupt("llm")
        self.task.set_keep("import", ["a"], False)
        before = self.task.snapshot()
        with self.assertRaises(tasks.TaskStateError):
            self.task.reset_stage("llm")
        self.assertEqual(self.task.snapshot(), before)

    def test_redo_obeys_narrowed_confirmed_input_and_preserves_other_manual_history(self):
        self.run_all()
        self.task.set_keep("shortlist", ["a"], False)
        self.task.confirm("shortlist")
        self.task.set_keep("import", ["a", "b"], False)
        self.task.confirm("import")
        self.task.reset_stage("shortlist")
        self.task.begin("shortlist")
        self.task.finish("shortlist", [{"job_id": "c", "list_pass": True}])
        info = self.task.stage_info("shortlist")
        self.assertEqual(info["result_ids"], ["c"])
        self.assertEqual(info["manual_choices"], {})
        self.assertEqual(info["keep_ids"], ["c"])
        self.task.confirm("shortlist")
        self.assertEqual([tasks.job_id(job) for job in self.task.input_jobs("fetch-jd")], ["c"])

    def test_import_redo_rejects_changed_fixed_source_before_any_state_change(self):
        self.run_all()
        before = self.task.snapshot()
        self.raw.write_text('{"jobs": []}', encoding="utf-8")
        with self.assertRaises(tasks.TaskStateError):
            self.task.reset_stage("import")
        self.assertEqual(self.task.snapshot(), before)

    def test_preflight_holds_both_leases_before_redo_and_failure_leaves_old_round_intact(self):
        self.run_import()
        before = self.task.snapshot()

        def preflight():
            self.assertIsNone(tasks._try_lease(self.task.path / ".running.lock"))
            self.assertIsNone(tasks._try_lease(self.task.path / ".task.lock"))
            self.assertEqual(self.task.snapshot(), before)
            raise tasks.TaskStateError("测试中拒绝仍活跃的子进程")

        with self.assertRaises(tasks.TaskStateError):
            self.task.reset_stage("import", before_reset=preflight)
        self.assertEqual(self.task.snapshot(), before)
        # Both leases are released even when preflight refuses the new round.
        self.task.reset_stage("import")
        self.task.begin("import")
        self.task.interrupt("import")

    def test_manifest_and_directory_failures_preserve_previous_manifest_and_old_data(self):
        self.run_import()
        old_runtime = self.task.run_directory("import")
        checkpoint = self.write_json(old_runtime / "cache.json", {"saved": True})
        before = self.task.snapshot()
        for target in ("manifest", "directory"):
            with self.subTest(target=target):
                patcher = (patch.object(tasks, "_atomic_manifest", side_effect=tasks.TaskStateError("disk full"))
                           if target == "manifest" else patch.object(Path, "mkdir", side_effect=OSError("disk full")))
                with patcher, self.assertRaises(tasks.TaskStateError):
                    self.task.reset_stage("import")
                self.assertEqual(self.task.snapshot(), before)
                self.assertEqual(self.task.run_directory("import"), old_runtime)
                self.assertEqual(json.loads(checkpoint.read_text()), {"saved": True})
        self.task.reset_stage("import")
        self.task.begin("import")
        self.task.interrupt("import")

    def test_runtime_pointers_cannot_escape_or_cross_stages(self):
        self.run_import()
        self.task.reset_stage("import")
        self.task.begin("import")
        self.task.interrupt("import")
        path = self.task.path / "task_manifest.json"
        original = self.task.snapshot()
        for relative in ("../outside", str(self.root), "runtime/llm", "runtime/import-redo-x",
                         "runtime/import/../llm", "runtime/import-redo-" + "a" * 32 + "/child"):
            with self.subTest(relative=relative):
                corrupt = copy.deepcopy(original)
                corrupt["stages"]["import"]["runtime_directory"] = relative
                self.write_json(path, corrupt)
                with self.assertRaises(tasks.TaskStateError):
                    tasks.ReviewTask.open(self.task.path)
        self.write_json(path, original)

    def test_redo_refuses_a_linked_old_checkpoint_without_changing_manifest(self):
        self.run_import()
        runtime = self.task.run_directory("import")
        outside = self.root / "outside"
        outside.mkdir()
        self.link_directory(outside, runtime / "linked-cache")
        before = self.task.snapshot()
        with self.assertRaises(tasks.TaskStateError):
            self.task.reset_stage("import")
        self.assertEqual(self.task.snapshot(), before)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
