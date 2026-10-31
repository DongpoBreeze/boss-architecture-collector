"""Offline regressions for interrupted collection, catalog and UI publication."""

import copy
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import collect_architecture as collector
import collection_sources as sources
import desktop_collection as ui
import test_desktop_collection as fixtures
import test_collect_architecture as cli_fixtures


class CollectionRecoveryAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "source"
        self.config = fixtures.small_config()
        self.spec = collector.build_specs(self.config)[0]
        self.output = (self.root / "raw" / self.spec["track"] / self.spec["city_id"]
                       / f"{self.spec['combination']}.json")
        self.checkpoint = self.root / "checkpoints" / f"{self.spec['combination']}.partial.json"

    def invoke(self, jobs, *, code=0, inspect=None):
        manifest_path = self.root / "collection_manifest.json"
        manifest = collector.read_json(manifest_path) if manifest_path.exists() else {"records": []}

        def launch(command, **_kwargs):
            if inspect is not None:
                inspect(command)
            fixtures.write_json(self.output, {"jobs": jobs})
            return cli_fixtures.FakeCollectionProcess("[2/3 页]\n完成\n", code)

        with patch.object(collector.subprocess, "Popen", side_effect=launch), redirect_stdout(io.StringIO()):
            return collector.invoke_spec(self.spec, root=self.root, manifest=manifest,
                                         manifest_path=manifest_path, python=Path(sys.executable),
                                         scraper_root=self.root, pages=3, wait_on_gate=False)

    def test_desktop_repeated_interruptions_then_cli_merge_keep_a_b_c(self):
        result = ui.CommandResult(-15, combination=self.spec["combination"], pages_completed=1, page_markers=1)
        for rows in ([{"job_id": "a"}], [{"job_id": "a"}, {"job_id": "b"}]):
            fixtures.write_json(self.output, {"jobs": rows})
            ui.preserve_interrupted_combination(self.root, [self.spec], 3, result, "paused",
                                                 "2020-01-01", lambda _line: None)
            self.assertEqual(collector.read_json(self.checkpoint)["jobs"], rows)

        def inspect(command):
            merged = collector.read_json(Path(command[command.index("--merge") + 1]))
            self.assertEqual({row["job_id"] for row in merged["jobs"]}, {"a", "b"})

        self.assertEqual(self.invoke([{"job_id": "a"}, {"job_id": "c"}], inspect=inspect), "success")
        self.assertEqual({row["job_id"] for row in collector.read_json(self.output)["jobs"]}, {"a", "b", "c"})
        self.assertEqual(collector.read_json(self.output)["total"], 3)
        progress = ui.read_collection_progress(self.root)
        self.assertEqual((progress["jobs"], progress["done"]), (3, 1))

    def test_cli_failures_refresh_checkpoint_before_each_next_attempt(self):
        for rows in ([{"job_id": "a"}], [{"job_id": "a"}, {"job_id": "b"}]):
            self.assertEqual(self.invoke(rows, code=1), "failed")
            self.assertEqual(collector.read_json(self.checkpoint)["jobs"], rows)

        def inspect(command):
            self.assertEqual({row["job_id"] for row in collector.read_json(self.checkpoint)["jobs"]}, {"a", "b"})
            self.assertEqual(Path(command[command.index("--merge") + 1]), self.checkpoint)

        self.assertEqual(self.invoke([{"job_id": "c"}], inspect=inspect), "success")
        self.assertEqual({row["job_id"] for row in collector.read_json(self.output)["jobs"]}, {"a", "b", "c"})

    def test_union_preserves_duplicate_raw_accounting_and_newer_fields_without_growth(self):
        fixtures.write_json(self.checkpoint, {"jobs": [{"job_id": "a", "title": "old"}] * 2})
        fixtures.write_json(self.output, {"jobs": [{"job_id": "a", "title": "new"}, {"job_id": "b"}]})
        for _attempt in range(3):
            self.assertEqual(collector.refresh_collection_checkpoint(self.output, self.checkpoint), 3)
        rows = collector.read_json(self.output)["jobs"]
        self.assertEqual([row["job_id"] for row in rows].count("a"), 2)
        self.assertEqual(rows[0]["title"], "new")
        self.assertEqual(collector.read_json(self.checkpoint)["jobs"], rows)

    def test_corrupt_raw_never_replaces_valid_recovery_rows(self):
        fixtures.write_json(self.checkpoint, {"jobs": [{"job_id": "a"}]})
        fixtures.write_json(self.output, {})
        self.output.write_text("{incomplete", encoding="utf-8")
        checkpoint_bytes = self.checkpoint.read_bytes()
        self.assertIsNone(collector.refresh_collection_checkpoint(self.output, self.checkpoint))
        self.assertEqual(self.checkpoint.read_bytes(), checkpoint_bytes)
        self.assertEqual(self.invoke([{"job_id": "c"}]), "success")
        self.assertEqual({row["job_id"] for row in collector.read_json(self.output)["jobs"]}, {"a", "c"})

    def test_retained_jobs_do_not_hide_a_failed_empty_retry_response(self):
        fixtures.write_json(self.checkpoint, {"jobs": [{"job_id": "a"}]})

        def launch(_command, **_kwargs):
            fixtures.write_json(self.output, {"jobs": []})
            return cli_fixtures.FakeCollectionProcess("未捕获到搜索响应\n", 0)

        with patch.object(collector.subprocess, "Popen", side_effect=launch), redirect_stdout(io.StringIO()):
            status = collector.invoke_spec(self.spec, root=self.root, manifest={"records": []},
                                           manifest_path=self.root / "collection_manifest.json",
                                           python=Path(sys.executable), scraper_root=self.root, pages=3)
        self.assertEqual(status, "failed")
        record = collector.read_json(self.root / "collection_manifest.json")["records"][0]
        self.assertEqual(record["job_count"], 1)
        self.assertTrue(record["retryable"])

    def test_cross_city_keyword_id_conflict_is_rejected_with_and_without_a_manifest(self):
        old = collector.build_specs(self.config, "current", 1, ["上海"])[0]
        changed = copy.deepcopy(self.config)
        changed["current"]["keywords"][0]["query"] = "不同的新查询"
        new = collector.build_specs(changed, "current", 1, ["深圳"])[0]
        self.assertNotEqual(old["combination"], new["combination"])
        for snapshots in (False, True):
            with self.subTest(snapshots=snapshots):
                if snapshots:
                    fixtures.write_json(self.root / "collection-plans" / "old.json", self.config)
                else:
                    fixtures.write_json(self.root / "collection_manifest.json", {"records": [old]})
                with self.assertRaisesRegex(ValueError, "同一 ID|不同条件"):
                    sources.assert_source_specs_compatible(self.root, [new])

    def test_full_unselected_catalog_uses_the_same_compatibility_rules_as_handoff(self):
        original_path = self.root / "collection-plans" / "old.json"
        fixtures.write_json(original_path, self.config)
        before = original_path.read_bytes()
        for category in ("keyword", "city", "salary", "experience", "degree"):
            changed = copy.deepcopy(self.config)
            if category == "keyword":
                changed["target"]["keywords"][0]["query"] = "未勾选的新查询"
            elif category == "city":
                changed["cities"][1]["name"] = "同ID不同城市"
            elif category == "salary":
                changed["salaries"][0]["code"] = "999"
            elif category == "experience":
                changed["target"]["experiences"][0]["code"] = "999"
            else:
                changed["degree"] = {"id": "202", "name": "大专"}
            specs = collector.build_specs(changed, "current", 1, ["上海"])
            with self.subTest(category=category), self.assertRaises(ValueError):
                sources.assert_source_specs_compatible(self.root, specs, config=changed)
        self.assertEqual(original_path.read_bytes(), before)
        self.assertEqual(list((self.root / "collection-plans").iterdir()), [original_path])

    def test_casefold_keyword_reuse_is_rejected_from_records_and_expected_across_city_track(self):
        old = collector.build_specs(self.config, "current", 1, ["上海"])[0]
        for history in ("records", "expected_combination_specs"):
            source = self.root / history
            manifest = source / "collection_manifest.json"
            fixtures.write_json(manifest, {"records": [], history: [old]})
            before = manifest.read_bytes()
            for city in ("上海", "深圳"):
                for track in ("current", "target"):
                    for same_query in (False, True):
                        changed = copy.deepcopy(self.config)
                        keyword = {"id": "SOLUTION", "query": old["keyword"] if same_query else "新的查询"}
                        if track == "current":
                            changed[track]["keywords"][0] = keyword
                        else:
                            changed["current"]["keywords"].pop(0)
                            changed[track]["keywords"] = [keyword]
                        specs = collector.build_specs(changed, track, 1, [city])
                        for full_config in (False, True):
                            kwargs = {"config": changed} if full_config else {}
                            with self.subTest(history=history, city=city, track=track,
                                              same_query=same_query, full_config=full_config):
                                with self.assertRaisesRegex(ValueError, "大小写"):
                                    sources.assert_source_specs_compatible(source, specs, **kwargs)
            self.assertEqual(manifest.read_bytes(), before)
            self.assertFalse((source / "collection-plans").exists())

    def test_casefold_spelling_is_reserved_even_without_historical_meaning_fields(self):
        for history in ("records", "expected_combination_specs"):
            source = self.root / history
            fixtures.write_json(source / "collection_manifest.json", {
                "records": [], history: [{"combination": self.spec["combination"], "status": "paused"}],
            })
            changed = copy.deepcopy(self.config)
            changed["current"]["keywords"][0]["id"] = "SOLUTION"
            specs = collector.build_specs(changed, "current", 1)
            with self.subTest(history=history), self.assertRaisesRegex(ValueError, "大小写"):
                sources.assert_source_specs_compatible(source, specs)
            with self.subTest(history=history, full_config=True), self.assertRaisesRegex(ValueError, "大小写"):
                sources.assert_source_specs_compatible(source, specs, config=changed)
            sources.assert_source_specs_compatible(source, [self.spec], config=self.config)

    def test_casefold_all_path_catalog_ids_reject_case_only_changes(self):
        config = copy.deepcopy(self.config)
        config["salaries"][0]["id"] = "band"
        config["current"]["experiences"][0]["id"] = "early"
        old = collector.build_specs(config, "current", 1)[0]
        for category in ("city", "keyword", "salary", "experience"):
            changed = copy.deepcopy(config)
            item = {"city": changed["cities"][0], "keyword": changed["current"]["keywords"][0],
                    "salary": changed["salaries"][0], "experience": changed["current"]["experiences"][0]}[category]
            item["id"] = item["id"].upper()
            specs = collector.build_specs(changed, "current", 1)
            for history in ("records", "expected_combination_specs", "catalog"):
                source = self.root / f"{category}-{history}"
                if history == "catalog":
                    fixtures.write_json(source / "collection-plans" / "old.json", config)
                else:
                    fixtures.write_json(source / "collection_manifest.json", {"records": [], history: [old]})
                for full_config in (False, True):
                    kwargs = {"config": changed} if full_config else {}
                    with self.subTest(category=category, history=history, full_config=full_config):
                        with self.assertRaisesRegex(ValueError, "大小写"):
                            sources.assert_source_specs_compatible(source, specs, **kwargs)

    def test_casefold_full_unselected_catalog_and_handoff_refuse_changed_spelling(self):
        old = collector.build_specs(self.config, "current", 1)[0]
        fixtures.write_json(self.root / "collection_manifest.json", {"records": [old]})
        changed = copy.deepcopy(self.config)
        changed["current"]["keywords"][0]["id"] = "SOLUTION"
        selected = collector.build_specs(changed, "current", cities=["深圳"])[1:2]
        self.assertEqual(selected[0]["keyword_id"], "product")
        with self.assertRaisesRegex(ValueError, "大小写"):
            sources.assert_source_specs_compatible(self.root, selected, config=changed)
        fixtures.write_json(self.root / "collection-plans" / "old.json", self.config)
        fallback = self.root.parent / "changed-fallback.json"
        fixtures.write_json(fallback, changed)
        review_output = self.root.parent / "review-configs"
        with self.assertRaisesRegex(ValueError, "大小写"):
            sources.review_config_for_source(self.root, fallback, output_dir=review_output)
        self.assertFalse(review_output.exists())


class CollectionAuditTkTests(unittest.TestCase):
    # Reuse the existing isolated Tk/process fixture without inheriting and
    # rerunning the original test methods.
    setUp = fixtures.CollectionTkTests.setUp
    cleanup_windows = fixtures.CollectionTkTests.cleanup_windows
    make_page = fixtures.CollectionTkTests.make_page
    pump_until = fixtures.CollectionTkTests.pump_until
    attach_fake = fixtures.CollectionTkTests.attach_fake
    write_manifest = fixtures.CollectionTkTests.write_manifest
    open_editor = fixtures.CollectionTkTests.open_editor

    def test_nonzero_exit_without_exception_saves_failed_recovery_and_page_progress(self):
        spec = self.specs[0]
        output = self.output / "raw" / spec["track"] / spec["city_id"] / f"{spec['combination']}.json"
        checkpoint = self.output / "checkpoints" / f"{spec['combination']}.partial.json"
        fixtures.write_json(checkpoint, {"jobs": [{"job_id": "a"}]})
        fixtures.write_json(output, {"jobs": [{"job_id": "a"}, {"job_id": "b"}]})
        process = fixtures.FakeProcess(f"RUN {spec['combination']} (now), pages=3\n[2/3 页]\n")
        process.finish(1)
        self.attach_fake(process)
        page = self.make_page()
        page.pages.set("3")
        page.start()
        self.pump_until(lambda: not page.busy)
        record = collector.record_for(collector.read_json(self.output / "collection_manifest.json"), spec["combination"])
        self.assertEqual((record["status"], record["exit_code"], record["pages_completed"], record["job_count"]),
                         ("failed", 1, 2, 2))
        self.assertEqual(record["desktop_interruption"], "failed")
        self.assertEqual({row["job_id"] for row in collector.read_json(checkpoint)["jobs"]}, {"a", "b"})
        self.assertEqual(page._snapshot["counts"]["failed"], 1)
        self.assertEqual(page._snapshot["done"], 0)
        self.assertIn("未全部完成", page.status_text.get())

    def test_nonzero_security_exit_keeps_the_cli_pause_state(self):
        spec = self.specs[0]
        process = fixtures.FakeProcess(f"RUN {spec['combination']} (now), pages=3\n[2/3 页]\n")
        process.finish(20)

        def before_launch(_command):
            self.write_manifest([{**spec, "status": "paused", "finished_at": collector.now_iso(),
                                  "security_or_login_gate": True}])

        self.attach_fake(process, before_launch)
        page = self.make_page()
        page.start()
        self.pump_until(lambda: not page.busy)
        record = collector.record_for(collector.read_json(self.output / "collection_manifest.json"), spec["combination"])
        self.assertEqual(record["status"], "paused")
        self.assertTrue(record["security_or_login_gate"])

    def test_abnormal_exit_does_not_reuse_a_recent_old_pause_as_its_result(self):
        spec = self.specs[0]
        self.write_manifest([{**spec, "status": "paused", "finished_at": collector.now_iso(),
                              "security_or_login_gate": True}])
        process = fixtures.FakeProcess(f"RUN {spec['combination']} (now), pages=3\n[2/3 页]\n")
        process.finish(1)
        self.attach_fake(process)
        page = self.make_page()
        page.start()
        self.pump_until(lambda: not page.busy)
        record = collector.record_for(collector.read_json(self.output / "collection_manifest.json"), spec["combination"])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["exit_code"], 1)

    def test_source_plan_conflict_fails_before_publishing_another_snapshot(self):
        original = self.output / "collection-plans" / "old.json"
        fixtures.write_json(original, self.config)
        changed = copy.deepcopy(self.config)
        changed["current"]["keywords"][0]["query"] = "不同的新查询"
        fixtures.write_json(self.config_path, changed)
        page = self.make_page()
        page.city.set("深圳")
        page.start()
        self.pump_until(lambda: not page.busy)
        self.assertIn("未完成", page.status_text.get())
        self.assertEqual(list((self.output / "collection-plans").iterdir()), [original])
        self.popen.assert_not_called()
        self.resolver.assert_not_called()

    def test_casefold_manifest_only_collision_cannot_relabel_the_existing_windows_raw(self):
        old = self.specs[0]
        output = self.output / "raw" / old["track"] / old["city_id"] / f"{old['combination']}.json"
        fixtures.write_json(output, {"jobs": [{"job_id": "old-job"}], "keyword": old["keyword"]})
        self.write_manifest([{**old, "status": "success", "job_count": 1}])
        manifest = self.output / "collection_manifest.json"
        before = output.read_bytes(), manifest.read_bytes()
        changed = copy.deepcopy(self.config)
        changed["current"]["keywords"][0] = {"id": "SOLUTION", "query": "新查询不能覆盖旧结果"}
        new = collector.build_specs(changed, "current", 1)[0]
        new_output = output.with_name(f"{new['combination']}.json")
        if sys.platform == "win32":
            self.assertTrue(output.samefile(new_output), "case-only names address the same Windows raw file")
        fixtures.write_json(self.config_path, changed)
        page = self.make_page()
        page.city.set("上海")
        page.track.set("current")
        page.limit.set("1")
        page.start()
        self.pump_until(lambda: not page.busy)
        self.assertIn("大小写", page.status_text.get())
        self.assertEqual((output.read_bytes(), manifest.read_bytes()), before)
        self.assertFalse((self.output / "collection-plans").exists())
        self.popen.assert_not_called()
        self.resolver.assert_not_called()

    def test_invalid_new_path_clears_old_statistics_and_rejects_delayed_results(self):
        self.write_manifest([{**self.specs[0], "status": "success", "job_count": 99, "pages_completed": 2}])
        page = self.make_page()
        self.pump_until(lambda: page._snapshot is not None and page._snapshot["jobs"] == 99)
        old_progress = copy.deepcopy(page._snapshot)
        old_generation = page._preview_id
        invalid = self.base / "ordinary-file"
        invalid.write_text("untouched", encoding="utf-8")
        page.collection_root.set(str(invalid))
        self.assertNotIn("99", page.stats_text.get())
        self.assertEqual(page.progress_table.get_children(), ())
        self.pump_until(lambda: "输出路径只读检查" in page.progress_text.get())
        page._emit("preview", {"generation": old_generation, "root_text": str(self.output),
                               "error": None, "progress": old_progress})
        page._emit("progress", {**old_progress, "root_text": str(self.output)})
        page._drain_events()
        self.assertIsNone(page._snapshot)
        self.assertNotIn("99", page.stats_text.get())
        self.assertEqual(page.progress_table.get_children(), ())
        self.assertEqual(float(page.progress_bar.cget("value")), 0)
        self.assertIn("未知", page.progress_text.get())
        self.assertEqual(invalid.read_text(encoding="utf-8"), "untouched")

    def test_cancel_is_disabled_during_actual_atomic_save_and_wm_close_is_explicit(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original_save = ui.plans.save_plan_config

        def slow_commit(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(4))
            return original_save(*args, **kwargs)

        page = self.make_page()
        editor = self.open_editor(page)
        with patch.object(ui.plans, "save_plan_config", side_effect=slow_commit):
            editor.save()
            self.pump_until(entered.is_set)
            self.assertTrue(editor._committing)
            self.assertTrue(editor.cancel_button.instate(["disabled"]))
            editor.close()
            self.assertIs(page._editor, editor)
            self.assertFalse(editor._closed)
            self.assertIn("不能取消或关闭", editor.status_text.get())
            self.assertEqual(page.config_path, self.config_path)
            release.set()
            self.pump_until(lambda: page._editor is None)
        self.assertNotEqual(page.config_path, self.config_path)
        self.assertEqual(len(list((self.base / "saved-plans").glob("*.json"))), 1)
        self.assertTrue(editor.applied)
        self.assertEqual(self.errors, [])

    def test_cancel_before_tk_accepts_commit_never_calls_the_publisher(self):
        page = self.make_page()
        editor = self.open_editor(page)
        prepared = threading.Event()
        original_emit = page._emit

        def emit(kind, value):
            original_emit(kind, value)
            if kind == "plan_editor_commit":
                prepared.set()

        with patch.object(page, "_emit", side_effect=emit), patch.object(ui.plans, "save_plan_config") as publish:
            editor.save()
            self.assertTrue(prepared.wait(2))
            self.assertFalse(editor._committing)
            editor.close()
            page._drain_events()
            publish.assert_not_called()
        self.assertIsNone(page._editor)
        self.assertEqual(page.config_path, self.config_path)
        self.assertFalse((self.base / "saved-plans").exists())

    def test_page_close_waits_for_an_accepted_plan_commit_and_its_tk_result(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original_save = ui.plans.save_plan_config

        def slow_commit(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(4))
            return original_save(*args, **kwargs)

        page = self.make_page()
        editor = self.open_editor(page)
        with patch.object(ui.plans, "save_plan_config", side_effect=slow_commit):
            editor.save()
            self.pump_until(entered.is_set)
            page.request_close()
            self.assertTrue(page._close_after_plan_save)
            self.assertFalse(page._closing)
            self.assertFalse(page.shutdown_complete)
            release.set()
            self.pump_until(lambda: page._closing and page.shutdown_complete)
        self.assertTrue(editor.applied)
        self.assertEqual(len(list((self.base / "saved-plans").glob("*.json"))), 1)
        self.assertEqual(self.errors, [])

    def test_failed_commit_still_finishes_a_deferred_page_close(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def failed_commit(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(4))
            raise OSError("synthetic save failure")

        page = self.make_page()
        editor = self.open_editor(page)
        with patch.object(ui.plans, "save_plan_config", side_effect=failed_commit):
            editor.save()
            self.pump_until(entered.is_set)
            page.request_close()
            release.set()
            self.pump_until(lambda: page._closing and page.shutdown_complete)
        self.assertFalse(editor.applied)
        self.assertFalse((self.base / "saved-plans").exists())
        self.assertEqual(page.config_path, self.config_path)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
