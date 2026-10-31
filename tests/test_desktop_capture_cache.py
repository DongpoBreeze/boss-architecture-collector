"""Deterministic content-read/search-build counts; temporary tasks, no executors."""

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import desktop_review_ui as ui
import desktop_tasks
from desktop_tasks import ReviewTask, STAGES, TaskStateError, job_id
from test_desktop_review_ui import signed_ai_checkpoint_context


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class TaskViewCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        source = self.base / "source"
        write_json(source / "raw" / "current" / "jobs.json", [])
        write_json(source / "raw" / "target" / "jobs.json", [])
        self.task = ReviewTask.create(source, self.base / "tasks", "cache-test")
        self.jobs = [{"job_id": f"job-{index}", "title": f"岗位{index}", "observed_in": [{"keyword": "检索词"}]}
                     for index in range(3)]
        self.cache = ui.TaskViewCache()

    def complete(self, through="llm"):
        for stage in STAGES:
            self.task.begin(stage)
            rows = copy.deepcopy(self.jobs) if stage == "import" else self.task.input_jobs(stage)
            stats = {}
            if stage == "shortlist":
                for job in rows:
                    job["list_pass"] = True
            if stage == "fetch-jd":
                for job in rows:
                    job["jd"] = "职责：系统集成与技术方案。"
            if stage == "llm":
                for job in rows:
                    job["llm_review"] = {"job_id": job_id(job), "status": "success", "review_format": "simple-v1",
                                         "判断": "符合", "理由": "证据A"}
            if stage == "report":
                prepared = self.task.run_directory("report") / "prepared"
                self.report_path = prepared / "merged_jobs_index.json"
                self.markdown_path = prepared / "海投分层清单.md"
                records = self.task.all_jobs()
                for job in records:
                    job["classification"] = "海投池"
                write_json(self.report_path, records)
                self.markdown_path.write_text("# 报告\n", encoding="utf-8")
                stats = {"json_path": str(self.report_path), "markdown_path": str(self.markdown_path)}
            self.task.finish(stage, rows, stats=stats)
            if stage == through:
                return
            self.task.confirm(stage)

    def capture(self):
        return ui._capture_task(self.task, cache=self.cache)

    def test_report_groups_prioritize_manual_exclusion_over_ai_pending(self):
        manual_audit = {"status": "completed", "in_input": True, "has_result": True,
                        "confirmed": True, "pending": False, "manual_keep": True, "keep": True}
        jobs = [
            {"job_id": "manual-excluded", "classification": "暂不投", "excluded_at": "import",
             "analysis_pending": True},
            {"job_id": "ai-pending", "classification": "待核实", "analysis_pending": True},
            {"job_id": "actionable", "classification": "海投池", "analysis_pending": False},
            {"job_id": "manual-kept", "classification": "待核实", "analysis_pending": True,
             "review_selection": {"llm": manual_audit}},
            {"job_id": "manual-ai-excluded", "classification": "待核实", "analysis_pending": True,
             "review_selection": {"llm": {**manual_audit, "manual_keep": False, "keep": False}}},
        ]
        info = {"keep_ids": [], "excluded_ids": [], "pending_ids": []}
        view = ui._row_view(jobs, info, "report", ui.TaskViewCache())
        self.assertEqual([job_id(job) for job in view["groups"]["剔除"]], ["manual-excluded", "manual-ai-excluded"])
        self.assertEqual([job_id(job) for job in view["groups"]["待核实"]], ["ai-pending"])
        self.assertEqual([job_id(job) for job in view["groups"]["保留"]], ["actionable", "manual-kept"])

    def test_ai_pending_manual_edits_reuse_evidence_search_and_refresh_all_three_groups(self):
        self.complete("fetch-jd")
        self.task.confirm("fetch-jd")
        self.task.begin("llm")
        rows = [ui._analyze_ai_stage(job) for job in self.task.input_jobs("llm")]
        self.task.finish("llm", rows, stats={"review_format": "simple-v1"})
        before = self.capture()
        snapshot = copy.deepcopy(before)
        self.task.set_keep("llm", ["job-0"], True)
        self.task.set_keep("llm", ["job-1"], False)
        with patch.object(desktop_tasks, "_read_json", wraps=desktop_tasks._read_json) as reads, \
                patch.object(ui, "_build_search", wraps=ui._build_search) as builds:
            after = self.capture()
        self.assertEqual(self.checkpoint_reads(reads), [])
        self.assertEqual(builds.call_count, 1, "only the all-jobs selection audit needs new search text")
        self.assertIs(after["rows"]["llm"], before["rows"]["llm"])
        self.assertIs(after["search"]["llm"], before["search"]["llm"])
        groups = after["row_views"]["llm"]["groups"]
        self.assertEqual([job_id(job) for job in groups["保留"]], ["job-0"])
        self.assertEqual([job_id(job) for job in groups["剔除"]], ["job-1"])
        self.assertEqual([job_id(job) for job in groups["待核实"]], ["job-2"])
        self.assertEqual(before, snapshot, "selection-only captures must not mutate published evidence")

    def checkpoint_reads(self, mock):
        return [call.args[0] for call in mock.call_args_list if call.args[0].name != "task_manifest.json"]

    def test_1000_job_repeat_refresh_reads_no_checkpoint_and_builds_no_search(self):
        self.jobs = [{"job_id": f"large-{index}", "title": f"架构岗位{index}", "jd": "已有正文。" * 30}
                     for index in range(1000)]
        self.complete("import")
        self.task.confirm("import")
        with patch.object(desktop_tasks, "_read_json", wraps=desktop_tasks._read_json) as reads, \
                patch.object(ui, "_build_search", wraps=ui._build_search) as builds:
            first = self.capture()
            self.assertEqual(len(self.checkpoint_reads(reads)), 1)
            self.assertEqual(sum(len(call.args[0]) for call in builds.call_args_list), 3000)
            reads.reset_mock()
            builds.reset_mock()
            for _ in range(3):
                repeated = self.capture()
                for stage in STAGES:
                    self.assertIs(first["rows"][stage], repeated["rows"][stage])
                    self.assertIs(first["search"][stage], repeated["search"][stage])
            self.assertEqual(self.checkpoint_reads(reads), [])
            self.assertEqual(reads.call_count, 3, "one validated manifest read per refresh remains intentional")
            builds.assert_not_called()
            ui._capture_task(self.task)  # The old, cache-less interface still works.
            self.assertEqual(len(self.checkpoint_reads(reads)), 1)
            self.assertEqual(sum(len(call.args[0]) for call in builds.call_args_list), 3000)

    def test_complete_stage_refresh_reuses_all_four_checkpoints_and_search(self):
        self.complete()
        with patch.object(desktop_tasks, "_read_json", wraps=desktop_tasks._read_json) as reads, \
                patch.object(ui, "_build_search", wraps=ui._build_search) as builds:
            self.capture()
            self.assertEqual(len(self.checkpoint_reads(reads)), 4)
            self.assertEqual(builds.call_count, 5)
            reads.reset_mock()
            builds.reset_mock()
            self.capture()
            self.assertEqual(self.checkpoint_reads(reads), [])
            builds.assert_not_called()

    def test_manual_exclusion_confirmation_restore_and_stale_report_do_not_reread_rows(self):
        self.complete("report")
        before = self.capture()
        frozen = copy.deepcopy(before)
        self.task.set_keep("shortlist", ["job-0"], False)
        with patch.object(desktop_tasks, "_read_json", wraps=desktop_tasks._read_json) as reads, \
                patch.object(ui, "_build_search", wraps=ui._build_search) as builds:
            edited = self.capture()
            self.assertEqual(self.checkpoint_reads(reads), [])
            self.assertEqual(builds.call_count, 1, "only the full-trace selection audit needs new search JSON")
        self.assertNotIn("json", edited["artifacts"])
        self.assertFalse(ui._report_ready(edited["manifest"]))
        for stage in STAGES[:-1]:
            self.assertIs(before["rows"][stage], edited["rows"][stage])
            self.assertIs(before["search"][stage], edited["search"][stage])
        job = next(job for job in edited["rows"]["report"] if job_id(job) == "job-0")
        self.assertFalse(job["review_selection"]["shortlist"]["manual_keep"])
        self.assertIsNone(job["excluded_at"])
        self.assertNotIn("llm_review", job, "stale semantic evidence must not leak into a new report")
        self.task.confirm("shortlist")
        confirmed = self.capture()
        job = next(job for job in confirmed["rows"]["report"] if job_id(job) == "job-0")
        self.assertEqual(job["excluded_at"], "shortlist")
        self.task.set_keep("shortlist", ["job-0"], True)
        self.task.confirm("shortlist")
        restored = self.capture()
        job = next(job for job in restored["rows"]["report"] if job_id(job) == "job-0")
        self.assertTrue(job["review_selection"]["shortlist"]["manual_keep"])
        self.assertIsNone(job["excluded_at"])
        self.assertEqual(before, frozen, "a later worker capture must never mutate an already-published view")

    def test_pending_preview_tracks_upstream_confirmation_and_selection(self):
        self.complete("import")
        self.assertEqual(self.capture()["rows"]["shortlist"], [])
        self.task.confirm("import")
        self.assertEqual(len(self.capture()["rows"]["shortlist"]), 3)
        self.task.set_keep("import", ["job-0"], False)
        self.assertEqual(self.capture()["rows"]["shortlist"], [])
        self.task.confirm("import")
        self.assertEqual([job_id(job) for job in self.capture()["rows"]["shortlist"]], ["job-1", "job-2"])
        self.task.set_keep("import", ["job-0"], True)
        self.task.confirm("import")
        self.assertEqual(len(self.capture()["rows"]["shortlist"]), 3)

    def test_in_place_external_edit_with_same_size_and_mtime_invalidates_only_changed_stage(self):
        self.complete()
        before = self.capture()
        path = self.task.path / before["manifest"]["stages"]["llm"]["result_file"]
        original_info = path.stat()
        content = path.read_text(encoding="utf-8")
        path.write_bytes(content.replace("证据A", "证据B").encode("utf-8"))
        os.utime(path, ns=(original_info.st_atime_ns, original_info.st_mtime_ns))
        self.assertEqual(path.stat().st_size, original_info.st_size)
        self.assertEqual(path.stat().st_mtime_ns, original_info.st_mtime_ns)
        with patch.object(desktop_tasks, "_read_json", wraps=desktop_tasks._read_json) as reads, \
                patch.object(ui, "_build_search", wraps=ui._build_search) as builds:
            changed = self.capture()
            self.assertEqual(self.checkpoint_reads(reads), [path])
            self.assertEqual(builds.call_count, 2)
        self.assertEqual(before["manifest"]["revision"], changed["manifest"]["revision"])
        self.assertIn("证据B".casefold(), changed["search"]["llm"]["job-0"])
        self.assertIn("证据B".casefold(), changed["search"]["report"]["job-0"])
        self.assertIn("证据A".casefold(), before["search"]["llm"]["job-0"])
        self.assertIs(before["rows"]["fetch-jd"], changed["rows"]["fetch-jd"])

    def test_missing_or_wrong_id_checkpoint_is_rejected_even_after_cache_hit(self):
        self.complete()
        view = self.capture()
        path = self.task.path / view["manifest"]["stages"]["llm"]["result_file"]
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises(TaskStateError):
            self.capture()
        path.write_bytes(original)
        self.capture()
        value = json.loads(original)
        value["jobs"][0]["job_id"] = "not-this-task"
        write_json(path, value)
        with self.assertRaises(TaskStateError):
            self.capture()

    def test_report_reuse_external_edits_missing_files_and_id_validation(self):
        self.complete("report")
        original_read_text = Path.read_text
        report_reads = []

        def read_text(path, *args, **kwargs):
            if path == self.report_path:
                report_reads.append(path)
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", read_text), patch.object(ui, "_build_search", wraps=ui._build_search) as builds:
            before = self.capture()
            self.assertEqual(len(report_reads), 1)
            builds.reset_mock()
            self.capture()
            self.assertEqual(len(report_reads), 1)
            builds.assert_not_called()
            self.markdown_path.unlink()
            missing_markdown = self.capture()
            self.assertNotIn("markdown", missing_markdown["artifacts"])
            self.assertIs(before["rows"]["report"], missing_markdown["rows"]["report"])
            self.assertEqual(len(report_reads), 1)
            builds.assert_not_called()
            records = json.loads(original_read_text(self.report_path, encoding="utf-8"))
            records[0]["classification"] = "重点投递"
            write_json(self.report_path, records)
            changed = self.capture()
            self.assertEqual(len(report_reads), 2)
            self.assertEqual(builds.call_count, 1)
            self.assertEqual(changed["rows"]["report"][0]["classification"], "重点投递")
            records[0]["job_id"] = "other-task-id"
            write_json(self.report_path, records)
            with self.assertRaises(TaskStateError):
                self.capture()
            self.report_path.unlink()
            fallback = self.capture()
            self.assertNotIn("json", fallback["artifacts"])
            self.assertEqual({job_id(job) for job in fallback["rows"]["report"]}, {job_id(job) for job in self.jobs})

    def test_cross_task_report_path_is_not_reused_or_opened(self):
        self.complete("report")
        self.capture()
        outside = self.base / "other-task-report.json"
        write_json(outside, [{"job_id": "private-other-job"}])
        manifest_path = self.task.path / "task_manifest.json"
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["stages"]["report"]["stats"]["json_path"] = str(outside)
        write_json(manifest_path, value)
        view = self.capture()
        self.assertNotIn("json", view["artifacts"])
        self.assertNotIn("private-other-job", view["search"]["report"])

    def test_live_1000_job_projection_reuses_unchanged_rows_and_search(self):
        self.jobs = [{"job_id": f"live-{index}", "title": f"架构岗位{index}", "jd": "完整职责正文。" * 20}
                     for index in range(1000)]
        self.complete("fetch-jd")
        self.task.confirm("fetch-jd")
        self.task.begin("llm")
        self.addCleanup(self.task._release_run)
        # Live rows require this attempt's prompt/provider binding and both
        # per-record signatures, just like a real processor checkpoint.
        sign = signed_ai_checkpoint_context(self.task, self.task.input_jobs("llm"))
        path = self.task.run_directory("llm") / "prepared/llm_reviews.json"
        first = self.capture()
        write_json(path, {"reviews": [sign({"job_id": "live-0", "status": "success", "review_format": "simple-v1",
                                          "判断": "符合", "理由": "第一份落盘判断"})]})
        with patch.object(ui, "_analyze_ai_stage", wraps=ui._analyze_ai_stage) as analyzes, \
                patch.object(ui, "_build_search", wraps=ui._build_search) as builds:
            changed = self.capture()
            self.assertEqual(analyzes.call_count, 1, "only the changed job is projected")
            self.assertEqual(sum(len(call.args[0]) for call in builds.call_args_list), 1)
            self.assertEqual(changed["manifest"]["stages"]["llm"]["keep_ids"], ["live-0"])
            self.assertEqual(len(changed["manifest"]["stages"]["llm"]["pending_ids"]), 999)
            self.assertIs(first["rows"]["llm"][1], changed["rows"]["llm"][1])
            self.assertIsNot(first["rows"]["llm"][0], changed["rows"]["llm"][0])
            analyzes.reset_mock()
            builds.reset_mock()
            with patch.object(ui, "_read_json", wraps=ui._read_json) as checkpoint_reads:
                for _ in range(3):
                    unchanged = self.capture()
                    self.assertIs(unchanged["rows"]["llm"], changed["rows"]["llm"])
                checkpoint_reads.assert_not_called()
            analyzes.assert_not_called()
            builds.assert_not_called()
        self.assertEqual(first["manifest"]["stages"]["llm"]["keep_ids"], [])
        self.task.interrupt("llm", "stopped")

    def test_cache_holds_only_active_files_and_one_task_not_old_revisions(self):
        self.complete("import")
        first = self.capture()
        for _ in range(12):
            self.task.begin("import")
            self.task.finish("import", self.jobs)
            self.capture()
            self.assertLessEqual(len(self.cache.files), 2, "initial baseline + current result, never history")
            self.assertEqual(len(self.cache.stages), 4)
        other = ReviewTask.create(Path(first["manifest"]["source_root"]), self.base / "tasks", "other-task")
        other.begin("import")
        other.finish("import", [{"job_id": "other", "title": "别的任务"}])
        switched = ui._capture_task(other, cache=self.cache)
        self.assertEqual(list(switched["search"]["import"]), ["other"])
        self.assertEqual(len(self.cache.files), 1)
        self.assertTrue(all(str(other.path) in version[0] for version, _rows in self.cache.files.values()))
        back = self.capture()
        self.assertEqual(set(back["search"]["import"]), {job_id(job) for job in self.jobs})


if __name__ == "__main__":
    unittest.main()
