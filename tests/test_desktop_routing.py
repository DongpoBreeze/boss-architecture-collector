"""Daily-folder routing and imports: temporary data, no browser or network."""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import desktop_app
import desktop_routing as routing


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class Value:
    def __init__(self, value):
        self.value = value
        self.owner = threading.get_ident()

    def get(self):
        if threading.get_ident() != self.owner:
            raise AssertionError("Tk-like variable accessed on background thread")
        return self.value

    def set(self, value):
        if threading.get_ident() != self.owner:
            raise AssertionError("Tk-like variable changed on background thread")
        self.value = value


class RoutingFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "2026-10-18-day"
        self.raw = self.root / "raw/current/shanghai/current__shanghai__solarch__20_50__unlimited.json"
        self.jobs = [dict(job_id=f"job-{index}", title="AI解决方案工程师", skills=["Agent", "RAG"],
                          city="上海", salary="20-30K", company="本地测试", experience="经验不限")
                     for index in range(3)]
        write_json(self.raw, {"city": "上海", "filters": {"salary": "20-50K", "experience": "经验不限"}, "jobs": self.jobs})

    def checkpoint(self, name="checkpoint", *, jd=0, model=0, stamp="2026-01-01T00:00:00+00:00",
                   scope=None, statuses=None, container="runs", manifest=True):
        output = self.root / container / name if container else self.root
        scope = {"source_root": str(self.root.resolve()), "city": "上海", "track": "all", "sample_size": 50, **(scope or {})}
        meta = {"source_root": scope["source_root"], "city": scope["city"], "tracks": scope["track"],
                "sample_size": scope["sample_size"], "sample_strategy": "balanced-dedup", "unique_job_count": 3}
        write_json(output / routing.MERGED, {"metadata": meta, "jobs": self.jobs})
        write_json(output / routing.SHORTLIST, {"total": 3, "jobs": self.jobs})
        write_json(output / "raw_sample.json", {"metadata": meta, "jobs": self.jobs})
        stages = {"列表前处理": {"status": "success"}, **(statuses or {})}
        if jd:
            details = [{"job_id": job["job_id"], "jd": "负责 Agent/RAG 方案设计、系统集成、PoC 和交付。"}
                       for job in self.jobs[:jd]]
            write_json(output / routing.DETAILS, details)
            write_json(output / routing.JD_MANIFEST, {"total_jobs": 3, "batch_size": 30,
                       "records": [{"batch": 1, "status": "success" if jd == 3 else "partial", "exit_code": 0,
                                    "job_ids": [job["job_id"] for job in self.jobs]}]})
            write_json(output / "prepared/jd_shortlists/detail_batches/details_0001.json", details)
        if model:
            write_json(output / routing.REVIEWS, {"reviews": [{"job_id": job["job_id"], "status": "success"}
                                                               for job in self.jobs[:model]]})
            write_json(output / routing.LLM_MANIFEST, {"status": "completed", "candidate_count": jd,
                       "completed_count": model, "pending_count": jd - model, "failed_count": 0, "fallback_count": 0})
        if manifest:
            write_json(output / "run_manifest.json", {**scope, "status": "partial", "stages": stages,
                       "updated_at": stamp, "combination_limit": None, "pages_per_combination": 10, "batch_size": 30})
        return output

    def plan(self, stage, **kwargs):
        return routing.resolve_route(self.root, stage, "上海", "当前+成长", 50, **kwargs)

    def app(self):
        app = object.__new__(desktop_app.CollectorDesktopApp)
        app.events = queue.Queue()
        app.stop_requested = False
        app._pause_requested = False
        app.config_path = desktop_app.DEFAULT_CONFIG
        app.config = desktop_app.collector.load_config(app.config_path)
        app._run_child = Mock(return_value=0)
        return app

    def options(self):
        return dict(root=self.root, city="上海", track="当前+成长", sample=50, pages=10,
                    limit=None, workers=8, batch_size=30, scraper=None, prepare_browser=False)


class DailyRootTests(RoutingFixture):
    def test_empty_root_is_not_the_source_code_working_directory(self):
        with self.assertRaisesRegex(ValueError, "不能为空"):
            routing.resolve_route("  ", "仅采集", "上海", "当前岗位", 50)
        app = self.app()
        app.data_root = Value("")
        with self.assertRaisesRegex(ValueError, "不能为空"):
            app._validate()

    def test_raw_prepared_run_and_nested_paths_resolve_to_the_same_day(self):
        run = self.checkpoint()
        for selected in (self.root, self.root / "raw", self.raw.parent,
                         self.root / "prepared", run, run / "prepared/jd_shortlists"):
            with self.subTest(selected=selected):
                daily, warnings = routing.resolve_daily_root(selected)
                self.assertEqual(daily, self.root.resolve())
                self.assertEqual(bool(warnings), selected != self.root)

    def test_parent_of_multiple_days_does_not_choose_the_newest(self):
        (Path(self.temp.name) / "2026-10-17-day/raw").mkdir(parents=True)
        before = snapshot(Path(self.temp.name))
        with self.assertRaisesRegex(ValueError, "某天|job-result"):
            routing.resolve_daily_root(Path(self.temp.name))
        self.assertEqual(snapshot(Path(self.temp.name)), before)

    def test_collection_can_create_a_new_daily_folder_without_old_prepared(self):
        selected = self.root / "new-day"
        plan = routing.resolve_route(selected, "仅采集", "上海", "当前岗位", 50)
        self.assertEqual(plan.mode, "collection")
        self.assertEqual(plan.stages, ("岗位采集",))
        self.assertFalse(selected.exists())
        self.assertIn("不读取旧 prepared", plan.summary)


class CheckpointSelectionTests(RoutingFixture):
    def test_jd_without_shortlist_automatically_plans_local_shortlist_first(self):
        plan = self.plan("JD补抓")
        self.assertEqual(plan.stages, ("列表前处理", "JD补抓"))
        self.assertIsNone(plan.input_run)
        self.assertEqual(plan.errors, ())
        self.assertEqual(plan.read_files, (self.raw,))
        self.assertIn("本地", "".join(plan.missing))

    def test_model_uses_a_partial_jd_run_not_the_newest_empty_run(self):
        useful = self.checkpoint("has-jd", jd=2)
        self.checkpoint("empty-newer", stamp="2099-01-01T00:00:00+00:00")
        plan = self.plan("Codex语义复核")
        self.assertEqual(plan.input_run, useful)
        self.assertEqual(plan.stages, ("Codex语义复核",))
        self.assertIn(routing.DETAILS, plan.copy_files)
        self.assertIn("JD 正文 1/3", "".join(plan.missing))
        self.assertFalse(plan.errors)

    def test_different_city_track_sample_and_source_root_are_not_mixed(self):
        valid = self.checkpoint("valid", jd=1)
        for field, value in (("city", "深圳"), ("track", "target"), ("sample_size", 200),
                             ("source_root", str(Path(self.temp.name) / "other"))):
            self.checkpoint(f"foreign-{field}", jd=3, scope={field: value}, stamp="2099-01-01T00:00:00+00:00")
        plan = self.plan("Codex语义复核")
        self.assertEqual(plan.input_run, valid)
        self.assertIn("已忽略 4", plan.summary)

    def test_metadata_scope_mismatch_and_invalid_manifest_are_rejected(self):
        valid = self.checkpoint("valid", jd=1)
        for value in ([], {}, {"city": "上海"}):
            bad = self.checkpoint(f"bad-{len(json.dumps(value))}", jd=3)
            write_json(bad / "run_manifest.json", value)
        bad = self.checkpoint("bad-json", jd=3)
        (bad / "run_manifest.json").write_text("{", encoding="utf-8")
        bad = self.checkpoint("bad-meta", jd=3)
        merged = json.loads((bad / routing.MERGED).read_text(encoding="utf-8"))
        merged["metadata"]["tracks"] = "target"
        write_json(bad / routing.MERGED, merged)
        self.assertEqual(self.plan("Codex语义复核").input_run, valid)

    def test_legacy_all_track_alias_and_metadata_only_prepared_are_supported(self):
        legacy = self.checkpoint(container="", jd=1, manifest=False)
        plan = self.plan("Codex语义复核")
        self.assertEqual(plan.input_run, legacy)
        self.assertFalse(plan.errors)

    def test_old_row_sample_strategy_is_not_treated_as_unique_job_scope(self):
        run = self.checkpoint(jd=3)
        value = json.loads((run / routing.MERGED).read_text(encoding="utf-8"))
        value["metadata"]["sample_strategy"] = "sequential"
        write_json(run / routing.MERGED, value)
        plan = self.plan("Codex语义复核")
        self.assertTrue(plan.errors)
        self.assertNotIn("JD补抓", plan.stages)

    def test_stale_upstream_is_not_a_complete_model_prerequisite(self):
        for label in ("列表前处理", "JD补抓"):
            with self.subTest(label=label):
                run = self.checkpoint(jd=3, model=3, statuses={label: {"status": "stale"}})
                plan = self.plan("Codex语义复核")
                self.assertTrue(plan.errors)
                self.assertIsNone(plan.input_run)
                self.assertIn("JD补抓", "".join(plan.errors))
                self.assertNotIn("JD补抓", plan.stages)
                self.assertTrue(run.exists())

    def test_model_without_jd_reports_a_prerequisite_error_not_a_network_plan(self):
        self.checkpoint()
        plan = self.plan("Codex语义复核")
        self.assertTrue(plan.errors)
        self.assertEqual(plan.stages, ("Codex语义复核",))
        self.assertIn("不会自行启动 BOSS", "".join(plan.errors))

    def test_raw_only_report_plans_only_local_rules(self):
        plan = self.plan("生成报告")
        self.assertEqual(plan.stages, ("列表前处理", "生成报告"))
        self.assertTrue(plan.quality_incomplete)
        self.assertIn("模型", "".join(plan.missing))
        self.assertNotIn("JD补抓", plan.stages)
        self.assertNotIn("Codex语义复核", plan.stages)

    def test_full_workflows_do_not_import_old_model_before_rerunning_upstream(self):
        self.checkpoint(jd=3, model=3)
        for stage in ("后处理全流程（已有数据）", "完整流程（采集+后处理）", "列表前处理"):
            with self.subTest(stage=stage):
                plan = self.plan(stage)
                self.assertEqual(plan.copy_files, ())
                self.assertIsNone(plan.input_run)
                self.assertIn(self.raw, plan.read_files)

    def test_new_mode_remains_distinct_and_explicit_resume_checks_scope(self):
        run = self.checkpoint(jd=1)
        self.assertTrue(self.plan("生成报告", run_choice=routing.NEW_RUN_CHOICE).errors)
        resume = self.plan("生成报告", run_choice=str(run))
        self.assertEqual(resume.mode, "resume")
        self.assertEqual(resume.input_run, run)
        self.assertFalse(resume.errors)
        changed = routing.resolve_route(self.root, "生成报告", "深圳", "当前+成长", 50, str(run))
        self.assertTrue(changed.errors)

    def test_no_matching_raw_never_falls_back_to_another_city(self):
        plan = routing.resolve_route(self.root, "JD补抓", "深圳", "当前+成长", 50)
        self.assertTrue(plan.errors)
        self.assertEqual(plan.read_files, ())

    def test_all_route_previews_leave_every_file_unchanged(self):
        self.checkpoint(jd=2, model=2)
        before = snapshot(self.root)
        directories = sorted(str(path) for path in self.root.rglob("*") if path.is_dir())
        for stage in desktop_app.STAGE_CHOICES:
            self.plan(stage)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(sorted(str(path) for path in self.root.rglob("*") if path.is_dir()), directories)

    def test_untrusted_report_execution_chain_cannot_authorize_boss_or_model(self):
        run = self.checkpoint()
        manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
        manifest.update(stage_selection="生成报告", routing={"execution_plan": list(routing._POST_STAGES)})
        write_json(run / "run_manifest.json", manifest)
        plan = self.plan("生成报告", run_choice=str(run))
        self.assertEqual(plan.stages, ("生成报告",))
        self.assertIn("无效的历史路由", plan.summary)


class CheckpointImportTests(RoutingFixture):
    def test_imports_only_fixed_prerequisites_and_not_old_reports_raw_or_credentials(self):
        source = self.checkpoint(jd=3, model=3)
        for relative in ("auth.json", "config.toml", "prepared/海投分层清单.md", "prepared/hai_tou_pool.json",
                         "raw/hidden.json", "prepared/jd_shortlists/detail_batches/details_0001.log"):
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep old output or secret", encoding="utf-8")
        manifest = json.loads((source / "run_manifest.json").read_text(encoding="utf-8"))
        manifest["output_files"] = {"merged_jobs": str(Path(self.temp.name) / "escape.json")}
        write_json(source / "run_manifest.json", manifest)
        before = snapshot(source)
        plan = self.plan("生成报告")
        output = desktop_app.make_run_directory(self.root, "上海", 50)
        imported = routing.copy_route_inputs(plan, output)
        actual = {path.relative_to(output).as_posix() for path in imported}
        self.assertEqual(actual, set(plan.copy_files))
        self.assertIn(routing.REVIEWS, actual)
        self.assertNotIn("run_manifest.json", actual)
        self.assertNotIn("prepared/海投分层清单.md", actual)
        self.assertFalse((output / "raw").exists())
        self.assertFalse((output / "auth.json").exists())
        self.assertEqual(snapshot(source), before)

    def test_jd_import_never_brings_old_model_conclusions(self):
        self.checkpoint(jd=2, model=2)
        plan = self.plan("JD补抓")
        self.assertIn(routing.DETAILS, plan.copy_files)
        self.assertNotIn(routing.REVIEWS, plan.copy_files)
        self.assertNotIn(routing.LLM_MANIFEST, plan.copy_files)

    def test_stale_model_is_not_copied_into_a_new_report(self):
        self.checkpoint(jd=3, model=3, statuses={"Codex语义复核": {"status": "stale"}})
        plan = self.plan("生成报告")
        self.assertIn(routing.DETAILS, plan.copy_files)
        self.assertNotIn(routing.REVIEWS, plan.copy_files)
        self.assertTrue(plan.quality_incomplete)

    def test_stale_jd_embedded_by_an_old_report_cannot_survive_import(self):
        source = self.checkpoint(jd=3, model=3, statuses={"JD补抓": {"status": "stale"}})
        value = json.loads((source / routing.MERGED).read_text(encoding="utf-8"))
        for job in value["jobs"]:
            job["jd"] = "OLD STALE JD"
        write_json(source / routing.MERGED, value)
        before = (source / routing.MERGED).read_bytes()
        plan = self.plan("生成报告")
        self.assertNotIn(routing.DETAILS, plan.copy_files)
        output = desktop_app.make_run_directory(self.root, "上海", 50)
        routing.copy_route_inputs(plan, output)
        result = json.loads((output / routing.MERGED).read_text(encoding="utf-8"))
        self.assertTrue(all("jd" not in job for job in result["jobs"]))
        self.assertEqual((source / routing.MERGED).read_bytes(), before)

    def test_nonempty_or_outside_output_and_non_whitelist_input_are_refused(self):
        self.checkpoint(jd=3)
        plan = self.plan("生成报告")
        output = desktop_app.make_run_directory(self.root, "上海", 50)
        keep = output / "keep.txt"
        keep.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "空的新"):
            routing.copy_route_inputs(plan, output)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(ValueError, "新 runs"):
            routing.copy_route_inputs(plan, outside)
        empty = desktop_app.make_run_directory(self.root, "上海", 50)
        with self.assertRaisesRegex(ValueError, "白名单"):
            routing.copy_route_inputs(replace(plan, copy_files=("../auth.json",)), empty)
        self.assertEqual(keep.read_text(encoding="utf-8"), "keep")

    def test_symlink_artifact_cannot_escape_the_day(self):
        source = self.checkpoint(jd=3)
        external = Path(self.temp.name) / "external.json"
        external.write_text((source / routing.MERGED).read_text(encoding="utf-8"), encoding="utf-8")
        destination = source / routing.MERGED
        destination.unlink()
        try:
            destination.symlink_to(external)
        except OSError as error:
            self.skipTest(f"Symlink creation is unavailable: {error}")
        plan = self.plan("Codex语义复核")
        self.assertTrue(plan.errors)
        self.assertIsNone(plan.input_run)

    def test_resolved_external_artifact_is_rejected_without_symlink_privileges(self):
        source = self.checkpoint(jd=3)
        external = Path(self.temp.name) / "outside.json"
        resolve = Path.resolve

        def resolve_path(path, *args, **kwargs):
            return external if path == source / routing.MERGED else resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve_path):
            plan = self.plan("Codex语义复核")
        self.assertTrue(plan.errors)
        self.assertIsNone(plan.input_run)


class RawBoundaryTests(RoutingFixture):
    def test_actual_raw_processing_never_follows_an_external_junction_or_symlink(self):
        external = Path(self.temp.name) / "outside-raw"
        external_file = external / "target__shanghai__solarch__20_50__3_5.json"
        write_json(external_file, {"city": "上海", "filters": {"salary": "20-50K", "experience": "3-5年"},
                                  "jobs": [{"job_id": "outside-job", "title": "AI解决方案工程师"}]})
        link = self.root / "raw/target/shanghai"
        link.parent.mkdir(parents=True)
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)],
                                    capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                self.skipTest("Directory junction creation is unavailable")
        else:
            link.symlink_to(external, target_is_directory=True)

        def remove_link():
            # Nonrecursive removal of the explicitly created link only.  The
            # external directory stays intact until TemporaryDirectory cleanup.
            link.absolute().relative_to(self.root.absolute())
            if link.is_symlink():
                link.unlink()
            else:
                link.rmdir()

        self.addCleanup(remove_link)
        before = external_file.read_bytes()
        plan = self.plan("列表前处理")
        self.assertEqual(plan.read_files, (self.raw,))
        app = self.app()
        read_json = desktop_app.processor.read_json

        def safe_read(path):
            if Path(path).resolve().is_relative_to(external.resolve()):
                raise AssertionError("External raw payload was opened")
            return read_json(path)

        with patch.object(desktop_app.processor, "read_json", side_effect=safe_read):
            rows, _metadata = desktop_app.processor.collect_rows(self.root, app.config, "上海", 50,
                                                                  "balanced-dedup", "all")
            info = desktop_app.processor.inspect_city(self.root, app.config, "上海", tracks="all")
            self.assertIsNone(desktop_app.processor.parse_source(link / external_file.name, self.root, app.config))
        self.assertEqual({job["job_id"] for job in rows}, {job["job_id"] for job in self.jobs})
        self.assertEqual(info["raw_record_count"], 3)
        self.assertEqual(external_file.read_bytes(), before)

    def test_city_and_track_are_filtered_before_reading_payloads(self):
        other_city = self.root / "raw/current/shenzhen/current__shenzhen__solarch__20_50__unlimited.json"
        other_track = self.root / "raw/target/shanghai/target__shanghai__solarch__20_50__3_5.json"
        for path in (other_city, other_track):
            write_json(path, {"filters": {}, "jobs": [{"job_id": "foreign"}]})
        app = self.app()
        read_json = desktop_app.processor.read_json

        def selected_only(path):
            if Path(path) in (other_city, other_track):
                raise AssertionError("An out-of-scope payload was opened")
            return read_json(path)

        with patch.object(desktop_app.processor, "read_json", side_effect=selected_only):
            rows, _metadata = desktop_app.processor.collect_rows(self.root, app.config, "上海", 50,
                                                                  "balanced-dedup", "current")
            info = desktop_app.processor.inspect_city(self.root, app.config, "上海", tracks="current")
        self.assertEqual(len(rows), 3)
        self.assertEqual(info["raw_record_count"], 3)


class RoutedWorkflowTests(RoutingFixture):
    def local_child(self, app):
        def child(command):
            stage = command[command.index("--stage") + 1]
            output = Path(command[command.index("--output-root") + 1])
            if stage == "shortlist":
                desktop_app.processor.merge_stage(self.root, output, app.config_path, "上海", 50,
                                                  sample_strategy="balanced-dedup", tracks="all")
            elif stage == "report":
                desktop_app.processor.report_stage(output)
            elif stage == "fetch-jd":
                jobs = json.loads((output / routing.SHORTLIST).read_text(encoding="utf-8"))["jobs"]
                write_json(output / routing.JD_MANIFEST, {"total_jobs": len(jobs), "batch_size": 30,
                           "records": [{"batch": 1, "status": "success", "exit_code": 0}]})
            else:
                raise AssertionError(f"Unexpected network/model stage {stage}")
            return 0
        return child

    def pause_manifest(self, run, stage="生成报告"):
        manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
        manifest.update(status="paused", stage_selection=stage,
                        stages={"列表前处理": {"status": "paused"}},
                        routing={"mode": "auto", "execution_plan": ["列表前处理", stage], "preparation_complete": True})
        write_json(run / "run_manifest.json", manifest)

    def test_auto_jd_executes_shortlist_then_fetch_and_records_provenance(self):
        app = self.app()
        before = snapshot(self.root)

        def child(command):
            stage = command[command.index("--stage") + 1]
            output = Path(command[command.index("--output-root") + 1])
            if stage == "shortlist":
                desktop_app.processor.merge_stage(self.root, output, app.config_path, "上海", 50,
                                                  sample_strategy="balanced-dedup", tracks="all")
            elif stage == "fetch-jd":
                write_json(output / routing.JD_MANIFEST, {"total_jobs": 3, "batch_size": 30,
                           "records": [{"batch": 1, "status": "success", "exit_code": 0}]})
            return 0

        app._run_child.side_effect = child
        with patch.object(desktop_app.processor, "inspect_city", return_value={}):
            app._run_routed_workflow_safely(self.options(), "JD补抓", routing.AUTO_RUN_CHOICE)
        commands = [call.args[0] for call in app._run_child.call_args_list]
        self.assertEqual([command[command.index("--stage") + 1] for command in commands], ["shortlist", "fetch-jd"])
        events = list(app.events.queue)
        self.assertEqual(events[0][0], "route_started")
        finished = next(value for kind, value in events if kind == "finished")
        output = Path(finished["output"])
        self.assertEqual(output.parent, self.root / "runs")
        self.assertEqual(finished["exit_code"], 0)
        manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["routing"]["execution_plan"], ["列表前处理", "JD补抓"])
        self.assertIn(str(self.raw), manifest["routing"]["read_files"])
        self.assertEqual(self.raw.read_bytes(), before[str(self.raw.relative_to(self.root))])

    def test_auto_model_without_jd_never_creates_a_run_or_calls_a_child(self):
        self.checkpoint()
        app = self.app()
        before = snapshot(self.root)
        app._run_routed_workflow_safely(self.options(), "Codex语义复核", routing.AUTO_RUN_CHOICE)
        app._run_child.assert_not_called()
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual([kind for kind, _ in app.events.queue], ["route_failed"])

    def test_model_imports_one_jd_source_then_only_runs_model(self):
        source = self.checkpoint("jd-source", jd=3)
        self.checkpoint("latest-empty", stamp="2099-01-01T00:00:00+00:00")
        app = self.app()
        before = snapshot(source)

        def child(command):
            output = Path(command[command.index("--output-root") + 1])
            write_json(output / routing.LLM_MANIFEST, {"status": "completed", "failed_count": 0,
                       "pending_count": 0, "fallback_count": 0})
            return 0

        app._run_child.side_effect = child
        with patch.object(desktop_app.processor, "inspect_city", return_value={}):
            app._run_routed_workflow_safely(self.options(), "Codex语义复核", routing.AUTO_RUN_CHOICE)
        self.assertEqual(app._run_child.call_count, 1)
        command = app._run_child.call_args.args[0]
        self.assertEqual(command[command.index("--stage") + 1], "llm")
        output = Path(command[command.index("--output-root") + 1])
        self.assertNotEqual(output, source)
        manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["imported_from"], str(source))
        self.assertIn(str(source / routing.DETAILS), manifest["routing"]["read_files"])
        self.assertEqual(snapshot(source), before)
        logs = "".join(value for kind, value in app.events.queue if kind == "log")
        self.assertIn(str(source / routing.DETAILS), logs)
        self.assertIn(str(output / routing.DETAILS), logs)

    def test_raw_only_report_runs_local_prepare_and_rules_and_preserves_old_output(self):
        old_report = self.root / "runs/old/prepared/海投分层清单.md"
        old_report.parent.mkdir(parents=True)
        old_report.write_text("old report must stay", encoding="utf-8")
        app = self.app()

        def child(command):
            stage = command[command.index("--stage") + 1]
            output = Path(command[command.index("--output-root") + 1])
            if stage == "shortlist":
                desktop_app.processor.merge_stage(self.root, output, app.config_path, "上海", 50,
                                                  sample_strategy="balanced-dedup", tracks="all")
            elif stage == "report":
                desktop_app.processor.report_stage(output)
            else:
                raise AssertionError(f"Unexpected browser/model stage: {stage}")
            return 0

        app._run_child.side_effect = child
        with patch.object(desktop_app.processor, "inspect_city", return_value={}):
            app._run_routed_workflow_safely(self.options(), "生成报告", routing.AUTO_RUN_CHOICE)
        stages = [call.args[0][call.args[0].index("--stage") + 1] for call in app._run_child.call_args_list]
        self.assertEqual(stages, ["shortlist", "report"])
        finished = next(value for kind, value in app.events.queue if kind == "finished")
        self.assertEqual(finished["manifest"]["status"], "partial")
        self.assertIn("JD", "".join(finished["manifest"]["warnings"]))
        self.assertIn("模型", "".join(finished["manifest"]["warnings"]))
        self.assertTrue((Path(finished["output"]) / "prepared/海投分层清单.md").is_file())
        self.assertEqual(old_report.read_text(encoding="utf-8"), "old report must stay")

    def test_explicit_resume_keeps_same_directory_and_scope_alias_skips_successful_stage(self):
        run = self.checkpoint()
        app = self.app()
        with patch.object(desktop_app.processor, "inspect_city", return_value={}):
            app._run_routed_workflow_safely(self.options(), "列表前处理", str(run))
        app._run_child.assert_not_called()
        finished = next(value for kind, value in app.events.queue if kind == "finished")
        self.assertEqual(Path(finished["output"]), run)
        self.assertEqual(finished["manifest"]["routing"]["mode"], "resume")

    def test_immediate_started_event_log_cannot_race_the_empty_import_check(self):
        self.checkpoint(jd=3)
        app = self.app()

        class ImmediateLogQueue(queue.Queue):
            def put(self, item, *args, **kwargs):
                if item[0] == "route_started":
                    output = Path(item[1]["output"])
                    (output / "desktop.log").write_text("immediate Tk log", encoding="utf-8")
                return super().put(item, *args, **kwargs)

        app.events = ImmediateLogQueue()
        app._run_child.side_effect = self.local_child(app)
        with patch.object(desktop_app.processor, "inspect_city", return_value={}):
            app._run_routed_workflow_safely(self.options(), "生成报告", routing.AUTO_RUN_CHOICE)
        finished = next(value for kind, value in app.events.queue if kind == "finished")
        self.assertEqual(finished["exit_code"], 0)
        self.assertTrue((Path(finished["output"]) / routing.MERGED).is_file())
        self.assertNotIn("error", finished["manifest"] if finished["manifest"].get("error") else {})

    def test_paused_auto_prefix_before_any_list_write_is_restored_on_resume(self):
        for stage in ("生成报告", "JD补抓"):
            with self.subTest(stage=stage):
                app = self.app()

                def pause_child(_command):
                    app.stop_requested = True
                    app._pause_requested = True
                    return 130

                app._run_child.side_effect = pause_child
                with patch.object(desktop_app.processor, "inspect_city", return_value={}):
                    app._run_routed_workflow_safely(self.options(), stage, routing.AUTO_RUN_CHOICE)
                finished = next(value for kind, value in app.events.queue if kind == "finished")
                output = Path(finished["output"])
                self.assertEqual(finished["manifest"]["status"], "paused")
                self.assertFalse((output / routing.MERGED).exists())
                app.events = queue.Queue()
                app.stop_requested = app._pause_requested = False
                app._run_child.reset_mock()
                app._run_child.side_effect = self.local_child(app)
                with patch.object(desktop_app.processor, "inspect_city", return_value={}):
                    app._run_routed_workflow_safely(self.options(), stage, str(output))
                commands = [call.args[0] for call in app._run_child.call_args_list]
                self.assertEqual([command[command.index("--stage") + 1] for command in commands],
                                 ["shortlist", "report" if stage == "生成报告" else "fetch-jd"])
                self.assertTrue((output / routing.MERGED).is_file())
                if stage == "生成报告":
                    self.assertTrue((output / "prepared/海投分层清单.md").is_file())
                # The next subtest must also exercise the no-checkpoint path.
                for path in output.rglob("*.json"):
                    if path.name == "run_manifest.json":
                        continue
                    path.unlink()

    def test_fixed_raw_sample_restores_indexes_without_reading_or_resampling_raw(self):
        run = self.checkpoint()
        self.pause_manifest(run)
        (run / routing.MERGED).unlink()
        (run / routing.SHORTLIST).unlink()
        write_json(self.raw, {"city": "上海", "filters": {}, "jobs": [{"job_id": "new-raw-job"}]})
        plan = self.plan("生成报告", run_choice=str(run))
        self.assertEqual(plan.fixed_sample, run / "raw_sample.json")
        app = self.app()
        app._run_child.side_effect = self.local_child(app)
        with patch.object(desktop_app.processor, "inspect_city", return_value={}), \
                patch.object(desktop_app.processor, "collect_rows", side_effect=AssertionError("must not resample raw")):
            app._run_routed_workflow_safely(self.options(), "生成报告", str(run))
        calls = [call.args[0] for call in app._run_child.call_args_list]
        self.assertEqual([command[command.index("--stage") + 1] for command in calls], ["report"])
        merged = json.loads((run / routing.MERGED).read_text(encoding="utf-8"))
        self.assertEqual({job["job_id"] for job in merged["jobs"]}, {job["job_id"] for job in self.jobs})

    def test_complete_indexes_after_pause_are_recovered_without_repeating_shortlist(self):
        run = self.checkpoint()
        self.pause_manifest(run)
        plan = self.plan("生成报告", run_choice=str(run))
        self.assertEqual(plan.stages, ("生成报告",))
        self.assertEqual(plan.recovered_stages, ("列表前处理",))
        app = self.app()
        app._run_child.side_effect = self.local_child(app)
        with patch.object(desktop_app.processor, "inspect_city", return_value={}), \
                patch.object(desktop_app.processor, "collect_rows", side_effect=AssertionError("must not resample raw")):
            app._run_routed_workflow_safely(self.options(), "生成报告", str(run))
        manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["stages"]["列表前处理"]["status"], "success")
        self.assertTrue(manifest["stages"]["列表前处理"]["recovered_from_durable_checkpoint"])

    def test_pause_during_import_finishes_the_chain_before_publishing_a_resumable_run(self):
        self.checkpoint(jd=3)
        app = self.app()
        copy_inputs = desktop_app.copy_route_inputs

        def pause_during_copy(plan, output):
            app.stop_requested = app._pause_requested = True
            return copy_inputs(plan, output)

        with patch.object(desktop_app, "copy_route_inputs", side_effect=pause_during_copy), \
                patch.object(desktop_app.processor, "inspect_city", return_value={}):
            app._run_routed_workflow_safely(self.options(), "生成报告", routing.AUTO_RUN_CHOICE)
        app._run_child.assert_not_called()
        finished = next(value for kind, value in app.events.queue if kind == "finished")
        output = Path(finished["output"])
        self.assertEqual(finished["manifest"]["status"], "paused")
        self.assertTrue(finished["manifest"]["routing"]["preparation_complete"])
        self.assertTrue(all((output / relative).is_file() for relative in finished["manifest"]["routing"]["copy_files"]))
        app.events = queue.Queue()
        app.stop_requested = app._pause_requested = False
        app._run_child.side_effect = self.local_child(app)
        with patch.object(desktop_app.processor, "inspect_city", return_value={}):
            app._run_routed_workflow_safely(self.options(), "生成报告", str(output))
        self.assertTrue((output / "prepared/海投分层清单.md").is_file())

    def test_failed_incomplete_import_is_not_silently_resumed(self):
        self.checkpoint(jd=3)
        app = self.app()

        def fail_copy(_plan, output):
            write_json(output / routing.MERGED, {"jobs": []})
            raise OSError("simulated disk failure during import")

        with patch.object(desktop_app, "copy_route_inputs", side_effect=fail_copy):
            app._run_routed_workflow_safely(self.options(), "生成报告", routing.AUTO_RUN_CHOICE)
        finished = next(value for kind, value in app.events.queue if kind == "finished")
        output = Path(finished["output"])
        self.assertFalse(finished["manifest"]["routing"]["preparation_complete"])
        before = snapshot(output)
        app.events = queue.Queue()
        app._run_routed_workflow_safely(self.options(), "生成报告", str(output))
        app._run_child.assert_not_called()
        self.assertEqual([kind for kind, _ in app.events.queue], ["route_failed"])
        self.assertEqual(snapshot(output), before)

    def test_preview_captures_variables_on_main_thread_and_worker_never_writes(self):
        self.checkpoint(jd=1)
        app = self.app()
        app._route_after_id = None
        app._route_request_id = 7
        app._closing = False
        app.data_root = Value(str(self.root))
        app.stage = Value("Codex语义复核")
        app.city = Value("上海")
        app.track = Value("当前+成长")
        app.sample_size = Value("50")
        app.resume_run = Value(routing.AUTO_RUN_CHOICE)
        app.routing_summary = Value("")
        before = snapshot(self.root)
        threads = []
        thread_type = threading.Thread

        def make_thread(*args, **kwargs):
            thread = thread_type(*args, **kwargs)
            threads.append(thread)
            return thread

        with patch.object(desktop_app.threading, "Thread", side_effect=make_thread):
            app._begin_route_preview()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
        kind, (request_id, summary) = app.events.get_nowait()
        self.assertEqual((kind, request_id), ("route_preview", 7))
        self.assertIn("merged_jobs_index.json", summary)
        self.assertEqual(snapshot(self.root), before)

    def test_route_failure_unlocks_and_restores_old_context_without_logging_to_it(self):
        app = self.app()
        old = self.checkpoint()
        log = old / "desktop.log"
        log.write_text("old log", encoding="utf-8")
        app._route_previous_context = (old, log, "original task")
        app.active_run = None
        app.current_log = None
        app.output_directory = Value("preparing")
        app.routing_summary = Value("")
        app.status_text = Value("")
        app.run_summary = Value("")
        app.progress_text = Value("")
        app.workflow_running = True
        app._closing = False
        app.root = Mock()
        app._append_log = Mock()
        app._set_busy = Mock()
        app.events.put(("route_failed", {"message": "缺少完整 JD", "cancelled": False}))
        with patch.object(desktop_app.messagebox, "showerror"):
            app._poll_events()
        self.assertFalse(app.workflow_running)
        self.assertEqual(app.active_run, old)
        self.assertEqual(app.current_log, log)
        self.assertEqual(app.output_directory.get(), "original task")
        app._set_busy.assert_called_once_with(False)
        self.assertEqual(log.read_text(encoding="utf-8"), "old log")


if __name__ == "__main__":
    unittest.main()
