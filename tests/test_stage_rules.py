"""Offline acceptance for typed stage rules, recommendations and CDP rendering."""

import copy
import io
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import desktop_tasks as tasks
import jd_parallel as adapter
import process_architecture as processor
import stage_rules as rules
from test_desktop_tasks import TaskTestCase
from test_jd_parallel import FakeBrowser, FakeScraper, ZERO_TIMING, job


class TypedStageRuleTests(unittest.TestCase):
    def test_defaults_field_descriptors_and_returned_values_are_independent(self):
        for stage in rules.RULE_STAGES:
            with self.subTest(stage=stage):
                default = rules.default_stage_rules(stage)
                self.assertEqual(rules.validate_stage_rules(stage, {}), default)
                fields = rules.stage_rule_fields(stage)
                self.assertEqual({field["key"] for field in fields}, set(default))
                for field in fields:
                    self.assertTrue(all(field[key] for key in ("key", "label", "help", "type")))
                    self.assertIn(field["type"], ("bool", "keywords", "int", "choice"))
                    if field["type"] == "int":
                        expected = (1, 20) if field["key"] == "scroll_count" else (0, 30)
                        self.assertEqual((field["min"], field["max"]), expected)
                    if field["type"] == "choice":
                        self.assertEqual(field["choices"], ["safe", "fast"])
                fields[0]["label"] = "changed"
                self.assertNotEqual(rules.stage_rule_fields(stage)[0]["label"], "changed")
                default.clear()
                self.assertTrue(rules.default_stage_rules(stage))

    def test_literal_keyword_normalization_preserves_first_spelling_and_input(self):
        value = {"include_keywords": ["  智能体 ", "", " \n", "智能体", "RAG", "rag", "C++", "[AI]"]}
        original = copy.deepcopy(value)
        normalized = rules.validate_stage_rules("shortlist", value)
        self.assertEqual(normalized["include_keywords"], ["智能体", "RAG", "C++", "[AI]"])
        self.assertEqual(value, original)
        normalized["include_keywords"].append("other")
        self.assertEqual(value, original)

    def test_unknown_and_invalid_configurations_are_rejected(self):
        invalid = [
            ("unknown", {}), ("llm", {}), ("report", {}),
            ("import", None), ("import", []), ("import", {"city": "changed"}),
            ("import", {"require_title": 1}), ("import", {"exclude_title_keywords": "AI"}),
            ("import", {"exclude_company_keywords": ["正常", 0]}),
            ("shortlist", {"include_keywords": [None]}),
            ("shortlist", {"keep_verification": "false"}),
            ("fetch-jd", {"scroll_count": True}), ("fetch-jd", {"scroll_count": 3.0}),
            ("fetch-jd", {"scroll_count": 0}), ("fetch-jd", {"scroll_count": 21}),
            ("fetch-jd", {"detail_speed_profile": "unknown"}),
            ("fetch-jd", {"detail_speed_profile": []}),
        ]
        for stage, value in invalid:
            with self.subTest(stage=stage, value=value), self.assertRaises(ValueError):
                rules.validate_stage_rules(stage, value)
        self.assertEqual(rules.validate_stage_rules("fetch-jd", {"scroll_count": 20})["scroll_count"], 20)

    def test_jd_page_wait_is_optional_bounded_and_ordered(self):
        legacy = {"scroll_count": 7, "detail_speed_profile": "safe"}
        normalized = rules.validate_stage_rules("fetch-jd", legacy)
        self.assertEqual((normalized["page_wait_min"], normalized["page_wait_max"]), (0, 0))
        self.assertEqual(legacy, {"scroll_count": 7, "detail_speed_profile": "safe"})
        for minimum, maximum in ((0, 0), (0, 30), (3, 8), (30, 30)):
            with self.subTest(minimum=minimum, maximum=maximum):
                actual = rules.validate_stage_rules("fetch-jd", {"page_wait_min": minimum, "page_wait_max": maximum})
                self.assertEqual((actual["page_wait_min"], actual["page_wait_max"]), (minimum, maximum))
        for invalid in ({"page_wait_min": -1}, {"page_wait_max": 31}, {"page_wait_min": True},
                        {"page_wait_max": False}, {"page_wait_max": 2.5}, {"page_wait_min": "3"},
                        {"page_wait_min": 4, "page_wait_max": 3}, {"page_wait_min": 1}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                rules.validate_stage_rules("fetch-jd", invalid)

    def test_import_rules_only_annotate_copies_with_stable_identity_and_all_rows(self):
        rows = [
            {"job_id": "固定岗位", "dedupe_key": "original", "title": "C++解决方案", "company": "正常公司",
             "observed_in": [{"source_file": "synthetic.json"}]},
            {"job_id": "empty", "title": " \t", "company_name": "招聘中介"},
            {"job_id": "case", "title": "[AI] Agent", "boss_name": "EXAMPLE企业"},
        ]
        original = copy.deepcopy(rows)
        self.assertTrue(all(rules.apply_import_rules(row, {})["import_rule_keep"] for row in rows))
        config = {"require_title": True, "exclude_title_keywords": ["[AI]"],
                  "exclude_company_keywords": ["中介", "example"]}
        changed = [rules.apply_import_rules(row, config) for row in rows]
        self.assertEqual([row["import_rule_keep"] for row in changed], [True, False, False])
        self.assertIn("职位名称为空", changed[1]["import_rule_reason"])
        self.assertNotIn("公司排除词", changed[2]["import_rule_reason"])
        self.assertIn("职位排除词", changed[2]["import_rule_reason"])
        self.assertEqual(rows, original)
        for before, after in zip(rows, changed):
            self.assertEqual(before, {key: value for key, value in after.items() if not key.startswith("import_rule_")})

    def test_import_rules_consider_same_field_source_variants(self):
        row = {"job_id": "x", "title": "架构师", "field_candidates": {
            "title": [{"value": "架构师"}, {"value": "高级销售"}],
        }}
        result = rules.apply_import_rules(row, {"exclude_title_keywords": ["销售"]})
        self.assertFalse(result["import_rule_keep"])


class StageRuleTaskTests(TaskTestCase):
    def test_legacy_jd_rule_values_default_to_no_wait_and_custom_range_persists(self):
        path = self.task.path / "task_manifest.json"
        value = self.task.snapshot()
        value["stage_rules"] = {"fetch-jd": {"scroll_count": 5, "detail_speed_profile": "safe"}}
        self.write_json(path, value)
        original = path.read_bytes()
        reopened = tasks.ReviewTask.open(self.task.path)
        self.addCleanup(reopened._release_run)
        actual = reopened.get_stage_rules("fetch-jd")
        self.assertEqual(actual, {"scroll_count": 5, "detail_speed_profile": "safe", "page_wait_min": 0, "page_wait_max": 0})
        self.assertEqual(path.read_bytes(), original, "reading old rules must not rewrite task data")
        reopened.set_stage_rules("fetch-jd", {**actual, "page_wait_min": 2, "page_wait_max": 9})
        again = tasks.ReviewTask.open(self.task.path)
        self.addCleanup(again._release_run)
        self.assertEqual((again.get_stage_rules("fetch-jd")["page_wait_min"],
                          again.get_stage_rules("fetch-jd")["page_wait_max"]), (2, 9))
        again.set_stage_rules("fetch-jd", None)
        self.assertEqual(again.get_stage_rules("fetch-jd"), rules.default_stage_rules("fetch-jd"))

    def test_old_manifest_without_stage_rules_opens_without_writes_and_uses_defaults(self):
        path = self.task.path / "task_manifest.json"
        value = self.task.snapshot()
        value.pop("stage_rules", None)
        self.write_json(path, value)
        before = path.read_bytes()
        reopened = tasks.ReviewTask.open(self.task.path)
        self.addCleanup(reopened._release_run)
        for stage in rules.RULE_STAGES:
            self.assertEqual(reopened.get_stage_rules(stage), rules.default_stage_rules(stage))
            reopened.set_stage_rules(stage, {})
            reopened.set_stage_rules(stage, None)
        self.assertEqual(path.read_bytes(), before)

    def test_save_restore_reopen_is_local_normalized_and_idempotent(self):
        raw_before = self.raw.read_bytes()
        fixed_before = {key: self.task.snapshot()[key] for key in tasks._SCOPE_KEYS}
        configs = {"import": {"exclude_title_keywords": [" 销售 ", "销售", ""]},
                   "shortlist": {"exclude_keywords": [" Java ", "java", "算法"]},
                   "fetch-jd": {"scroll_count": 7, "detail_speed_profile": "safe"}}
        for stage, config in configs.items():
            with self.subTest(stage=stage):
                before = self.task.snapshot()
                with patch.object(self.task, "begin", side_effect=AssertionError("saving must not execute")):
                    self.task.set_stage_rules(stage, config)
                after = self.task.snapshot()
                self.assertEqual(after["revision"], before["revision"] + 1)
                self.assertEqual(after["stages"], before["stages"])
                expected = rules.validate_stage_rules(stage, config)
                self.assertEqual(self.task.get_stage_rules(stage), expected)
                reopened = tasks.ReviewTask.open(self.task.path)
                self.addCleanup(reopened._release_run)
                self.assertEqual(reopened.get_stage_rules(stage), expected)
                reopened.set_stage_rules(stage, config)
                self.assertEqual(reopened.snapshot(), after)
                reopened.set_stage_rules(stage, None)
                self.assertEqual(reopened.get_stage_rules(stage), rules.default_stage_rules(stage))
                restored = reopened.snapshot()
                reopened.set_stage_rules(stage, None)
                self.assertEqual(reopened.snapshot(), restored)
        self.assertEqual(self.raw.read_bytes(), raw_before)
        self.assertEqual({key: self.task.snapshot()[key] for key in tasks._SCOPE_KEYS}, fixed_before)

    def test_effective_changes_invalidate_this_step_and_all_later_results_atomically(self):
        configs = {"import": {"require_title": True},
                   "shortlist": {"keep_verification": False},
                   "fetch-jd": {"scroll_count": 4}}
        for target, config in configs.items():
            with self.subTest(stage=target):
                self.task = self.create()
                self.addCleanup(self.task._release_run)
                self.run_all()
                # Matching manual choices are durable intent even when the
                # recommended keep set does not change.
                self.task.set_keep(target, ["b"], True)
                before = self.task.snapshot()
                paths = [self.task.path / before["stages"][stage]["result_file"] for stage in tasks.STAGES]
                files = {path: path.read_bytes() for path in paths}
                self.task.set_stage_rules(target, config)
                after = self.task.snapshot()
                self.assertEqual(after["revision"], before["revision"] + 1)
                for stage in tasks.STAGES[:tasks.STAGES.index(target)]:
                    self.assertEqual(after["stages"][stage], before["stages"][stage])
                for stage in tasks.STAGES[tasks.STAGES.index(target):]:
                    current, old = after["stages"][stage], before["stages"][stage]
                    self.assertEqual(current["status"], "stale")
                    self.assertFalse(current["confirmed"])
                    self.assertEqual(current["invalidated_by"], f"stage_rules:{target}")
                    for field in ("result_file", "input_file", "manual_choices", "keep_ids", "excluded_ids"):
                        self.assertEqual(current[field], old[field])
                    with self.assertRaises(tasks.TaskStateError):
                        self.task.confirm(stage)
                self.assertEqual({path: path.read_bytes() for path in paths}, files)
                self.task.set_stage_rules(target, config)
                self.assertEqual(self.task.snapshot(), after)

    def test_repeated_default_saves_preserve_all_confirmations(self):
        self.run_all()
        before = self.task.snapshot()
        for stage in rules.RULE_STAGES:
            self.task.set_stage_rules(stage, {})
            self.task.set_stage_rules(stage, rules.default_stage_rules(stage))
            self.task.set_stage_rules(stage, None)
        self.assertEqual(self.task.snapshot(), before)

    def test_repeated_custom_saves_preserve_confirmations_and_restore_invalidates(self):
        configs = {"import": {"require_title": True},
                   "shortlist": {"include_keywords": ["方案", "平台"]},
                   "fetch-jd": {"scroll_count": 7, "detail_speed_profile": "safe"}}
        for stage, config in configs.items():
            self.task.set_stage_rules(stage, config)
        self.run_all()
        before = self.task.snapshot()
        for stage, config in configs.items():
            self.task.set_stage_rules(stage, config)
            detached = self.task.get_stage_rules(stage)
            detached.clear()
        self.assertEqual(self.task.snapshot(), before)
        self.task.set_stage_rules("fetch-jd", None)
        after = self.task.snapshot()
        self.assertEqual(after["revision"], before["revision"] + 1)
        for stage in ("import", "shortlist"):
            self.assertEqual(after["stages"][stage], before["stages"][stage])
        for stage in ("fetch-jd", "llm", "report"):
            self.assertEqual(after["stages"][stage]["status"], "stale")
            self.assertFalse(after["stages"][stage]["confirmed"])

    def test_rule_changes_update_export_snapshot_even_when_all_stages_are_pending(self):
        before = self.task.review_snapshot()
        self.task.set_stage_rules("shortlist", {"include_keywords": ["知识工程"]})
        after = self.task.review_snapshot()
        self.assertNotEqual(after["snapshot_token"], before["snapshot_token"])
        self.assertEqual(after["manifest"]["revision"], before["manifest"]["revision"] + 1)
        self.assertEqual(after["manifest"]["stages"], before["manifest"]["stages"])
        with self.assertRaises(tasks.TaskStateError):
            with self.task.review_read_lease(expected_snapshot=before["snapshot_token"]):
                self.fail("stale export snapshot was accepted")

    def test_invalid_and_running_rules_never_partially_commit(self):
        path = self.task.path / "task_manifest.json"
        before = path.read_bytes()
        for stage, config in (("import", {"require_title": True, "exclude_company_keywords": [1]}),
                              ("shortlist", {"include_keywords": ["有效"], "keep_verification": 0}),
                              ("fetch-jd", {"scroll_count": 5, "detail_speed_profile": "wrong"}),
                              ("llm", {}), ("report", {})):
            with self.subTest(stage=stage), self.assertRaises(tasks.TaskStateError):
                self.task.set_stage_rules(stage, config)
            self.assertEqual(path.read_bytes(), before)
        other = tasks.ReviewTask.open(self.task.path)
        self.addCleanup(other._release_run)
        self.task.begin("import")
        before = path.read_bytes()
        for editor in (self.task, other):
            for stage in rules.RULE_STAGES:
                for config in ({}, None):
                    with self.subTest(stage=stage, config=config), self.assertRaises(tasks.TaskStateError):
                        editor.set_stage_rules(stage, config)
                    self.assertEqual(path.read_bytes(), before)
        self.task.interrupt("import")

    def test_corrupt_persisted_configuration_cannot_be_opened(self):
        path = self.task.path / "task_manifest.json"
        original = self.task.snapshot()
        for value in (None, [], {"llm": {}}, {"fetch-jd": {"scroll_count": False}},
                      {"shortlist": {"unknown": True}}):
            with self.subTest(value=value):
                self.write_json(path, {**original, "stage_rules": value})
                with self.assertRaises(tasks.TaskStateError):
                    tasks.ReviewTask.open(self.task.path)
        self.write_json(path, original)

    def test_import_finish_uses_only_real_booleans_and_manual_choices_remain_first(self):
        jobs = self.imported()
        jobs[0]["import_rule_keep"] = False
        jobs[1]["import_rule_keep"] = "false"
        jobs[2]["import_rule_keep"] = 0
        self.run_import(jobs)
        self.assertEqual(self.task.stage_info("import")["keep_ids"], ["b", "c"])
        self.task.set_keep("import", ["a"], True)
        self.task.set_keep("import", ["b"], False)
        self.task.confirm("import")
        self.task.set_stage_rules("import", {"exclude_title_keywords": ["方案"]})
        updated = [rules.apply_import_rules(row, self.task.get_stage_rules("import")) for row in self.imported()]
        self.run_import(updated)
        self.assertEqual(self.task.stage_info("import")["manual_choices"], {"a": True, "b": False})
        self.assertEqual(self.task.stage_info("import")["keep_ids"], ["a"])
        self.assertEqual(self.task.stage_info("import")["result_ids"], list("abc"))
        self.assertEqual(self.task.snapshot()["baseline_ids"], list("abc"))


class ShortlistStageRuleTests(unittest.TestCase):
    def test_explicit_defaults_preserve_all_existing_outputs(self):
        jobs = [
            {"title": "AI架构师", "skills": "RAG"}, {"title": "Java", "skills": "Java | Spring"},
            {"title": "系统架构师"}, {"title": "AI销售", "skills": "电话销售 | 陌拜"},
            {"title": "AI架构师", "salary": "5-8K"},
            {"title": "AI架构师", "jd": "不要用于列表判断", "field_candidates": {
                "title": [{"value": "AI架构师", "sources": [{"source_file": "one"}]},
                          {"value": "销售", "sources": [{"source_file": "two"}]}]}}
        ]
        for job_value in jobs:
            original = copy.deepcopy(job_value)
            self.assertEqual(processor.score_job(job_value), processor.score_job(job_value, {}))
            self.assertEqual(processor.score_job(job_value), processor.score_job(job_value, rules.default_stage_rules("shortlist")))
            self.assertEqual(job_value, original)

    def test_chinese_inclusion_changes_recommendations_and_exclusion_wins(self):
        config = {"include_keywords": ["知识工程", "智能体"]}
        matched = processor.score_job({"title": "智能体方案工程师"}, config)
        missing = processor.score_job({"title": "AI产品经理"}, config)
        excluded = processor.score_job({"title": "智能体知识工程师"}, {**config, "exclude_keywords": ["知识工程"]})
        self.assertTrue(matched["list_pass"])
        self.assertEqual(missing["list_queue"], "excluded")
        self.assertIn("未命中自定义包含词", missing["list_exclusion_reasons"])
        self.assertFalse(excluded["list_pass"])
        self.assertIn("自定义排除词", excluded["list_queue_reason"])

    def test_keyword_whitelist_excludes_jd_company_salary_and_other_metadata(self):
        row = {"title": "待定岗位", "jd": "知识工程", "company": "知识工程", "salary": "知识工程",
               "observed_in": [{"query": "知识工程"}], "field_candidates": {"jd": [{"value": "知识工程"}]}}
        custom = {"use_default_direction_rules": False, "include_keywords": ["知识工程"]}
        self.assertFalse(processor.score_job(row, custom)["list_pass"])
        for field in rules.SHORTLIST_MATCH_FIELDS:
            with self.subTest(field=field):
                self.assertTrue(processor.score_job({**row, field: "知识工程"}, custom)["list_pass"])
                candidate = {**row, "field_candidates": {field: [{"value": "知识工程"}]}}
                self.assertTrue(processor.score_job(candidate, custom)["list_pass"])

    def test_keywords_are_literal_and_ignore_case_without_regular_expressions(self):
        custom = {"use_default_direction_rules": False, "exclude_pure_development": False,
                  "include_keywords": ["[AI]", "C++", "n8n"]}
        for title in ("[ai] 咨询", "C++系统岗位", "N8N咨询"):
            self.assertTrue(processor.score_job({"title": title}, custom)["list_pass"])
        self.assertFalse(processor.score_job({"title": "AAA咨询"}, custom)["list_pass"])

    def test_default_direction_and_development_switches_have_independent_effects(self):
        sales = {"title": "AI Agent销售", "skills": "电话销售 | 陌拜"}
        self.assertFalse(processor.score_job(sales)["list_pass"])
        custom = processor.score_job(sales, {"use_default_direction_rules": False, "include_keywords": ["Agent"]})
        self.assertTrue(custom["list_pass"])
        self.assertEqual(custom["list_queue"], "relevant")
        java = {"title": "Java", "skills": "Spring | MySQL"}
        self.assertFalse(processor.score_job(java)["list_pass"])
        relaxed = processor.score_job(java, {"exclude_pure_development": False})
        self.assertEqual(relaxed["list_queue"], "verify")
        self.assertTrue(relaxed["list_pass"])
        self.assertTrue(relaxed["list_pure_development"])
        self.assertFalse(processor.score_job(java, {"use_default_direction_rules": False})["list_pass"])
        free = {"use_default_direction_rules": False, "exclude_pure_development": False}
        self.assertTrue(processor.score_job(java, free)["list_pass"])
        neutral = processor.score_job({}, {"use_default_direction_rules": False})
        self.assertEqual(neutral["list_queue"], "verify")
        self.assertTrue(neutral["list_pass"])

    def test_disabling_pure_development_also_handles_conflicting_source_variants(self):
        row = {"title": "AI架构师", "field_candidates": {
            "title": [{"value": "AI架构师", "sources": [{"source_file": "a"}]},
                      {"value": "Java", "sources": [{"source_file": "b"}]}]}}
        self.assertFalse(processor.score_job(row)["list_pass"])
        self.assertTrue(processor.score_job(row, {"exclude_pure_development": False})["list_pass"])

    def test_verification_switch_changes_keep_recommendation_and_retains_queue(self):
        row = {"title": "系统架构师"}
        self.assertTrue(processor.score_job(row)["list_pass"])
        changed = processor.score_job(row, {"keep_verification": False})
        self.assertEqual(changed["list_queue"], "verify")
        self.assertFalse(changed["list_pass"])
        self.assertFalse(changed["list_excluded"])
        self.assertIn("不建议保留待核实", changed["list_queue_reason"])
        self.assertTrue(processor.score_job({"title": "AI架构师"}, {"keep_verification": False})["list_pass"])


class JDStageRuleTests(unittest.TestCase):
    def run_capture(self, directory, count, *, mode="single"):
        browser = FakeBrowser()
        scraper = FakeScraper(browser)
        logs = []
        output = Path(directory) / f"details-{count}-{mode}.json"
        options = {"cdp_factory": browser.connect}
        if mode == "dual":
            options = {"cdp_factories": {"chrome": browser.connect, "edge": browser.connect}}
        result = adapter.collect_details([job(i) for i in range(4)], output, scraper=scraper,
                                         workers=2, scroll_count=count, speed_profile="safe", timing=ZERO_TIMING,
                                         browser_mode=mode, emit=logs.append, **options)
        return result, browser, logs

    def test_custom_scroll_count_reaches_actual_cdp_pages_logs_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            for mode, count in (("single", 1), ("single", 7), ("dual", 5)):
                with self.subTest(mode=mode, count=count):
                    result, browser, logs = self.run_capture(directory, count, mode=mode)
                    self.assertEqual(result.status, "completed")
                    self.assertTrue(all(connection.scrolls == count for connection in browser.connections))
                    self.assertTrue(any(f"每页滚动 {count} 次" in line for line in logs))
                    self.assertEqual(result.scroll_count, count)
                    self.assertEqual(result.detail_speed_profile, "safe")
                    for record in result.records:
                        self.assertEqual(record["scroll_count"], count)
                        self.assertEqual(record["scrolls_completed"], count)
                        self.assertEqual(record["detail_speed_profile"], "safe")
                    self.assertTrue(all(connection.closed for connection in browser.connections))
                    self.assertEqual(browser.active, {"user-original-tab"})
                    for side in result.browser_results.values():
                        self.assertEqual(side["scroll_count"], count)

    def test_concurrent_batches_do_not_override_one_another(self):
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.run_capture, directory, count) for count in (2, 9)]
            for count, future in zip((2, 9), futures):
                result, browser, _ = future.result()
                self.assertEqual(result.scroll_count, count)
                self.assertTrue(all(connection.scrolls == count for connection in browser.connections))
        self.assertEqual(adapter.SCROLL_COUNT, 3)

    def test_invalid_rendering_is_rejected_before_writes_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "must-not-exist.json"
            scraper = FakeScraper(FakeBrowser())
            factory = Mock(side_effect=AssertionError("must not access browser"))
            for invalid in (False, 0, 21, 1.5, "7"):
                with self.subTest(value=invalid), self.assertRaises(ValueError):
                    adapter.collect_details([job(1)], output, scraper=scraper, cdp_factory=factory, scroll_count=invalid)
                with self.assertRaises(ValueError):
                    processor.fetch_jd(output.parent / "absent", output.parent, jd_scroll_count=invalid)
                self.assertFalse(output.exists())
            with self.assertRaises(ValueError):
                adapter.collect_details([job(1)], output, scraper=scraper, speed_profile=[])
            factory.assert_not_called()

    def test_process_cli_forwards_rendering_and_rejects_ranges_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            with patch.object(processor, "fetch_jd", return_value={"manifest": {"records": []}}) as fetch, redirect_stdout(io.StringIO()):
                code = processor.main(["--root", str(root), "--output-root", str(output), "--stage", "fetch-jd",
                                       "--scraper-root", str(root), "--jd-scroll-count", "8", "--detail-speed-profile", "safe"])
            self.assertEqual(code, 0)
            self.assertEqual(fetch.call_args.kwargs["jd_scroll_count"], 8)
            self.assertEqual(fetch.call_args.kwargs["detail_speed_profile"], "safe")
            absent = root / "must-stay-absent"
            for invalid in ("0", "21"):
                with self.subTest(value=invalid), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    processor.main(["--root", str(root), "--output-root", str(absent), "--stage", "fetch-jd", "--jd-scroll-count", invalid])
            self.assertFalse(absent.exists())

    def test_process_fetch_forwards_scroll_to_adapter_and_writes_accurate_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, scraper = root / "output", root / "scraper"
            shortlist = output / "prepared" / "jd_shortlists" / "jd_input.json"
            shortlist.parent.mkdir(parents=True)
            shortlist.write_text(json.dumps({"jobs": [job(1)]}), encoding="utf-8")
            entry = scraper / "scripts" / "boss_cdp_raw.py"
            entry.parent.mkdir(parents=True)
            entry.write_text("# synthetic", encoding="utf-8")
            with patch.object(processor, "ensure_detail_cdp_ready", return_value=(True, None)), \
                    patch.object(processor, "run_detail_subprocess", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                    redirect_stdout(io.StringIO()) as logs:
                result = processor.fetch_jd(output, scraper, python_path=sys.executable,
                                            jd_scroll_count=6, detail_speed_profile="safe")
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--scroll-count") + 1], "6")
            self.assertEqual(command[command.index("--detail-speed-profile") + 1], "safe")
            self.assertIn("每页滚动 6 次", logs.getvalue())
            self.assertEqual(result["manifest"]["scroll_count"], 6)
            self.assertEqual(result["manifest"]["detail_speed_profile"], "safe")


if __name__ == "__main__":
    unittest.main()
