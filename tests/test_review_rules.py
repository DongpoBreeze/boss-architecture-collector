"""Offline acceptance checks for editable rules and complete semantic duties."""

import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import desktop_tasks as tasks
import process_architecture as processor
import review_pipeline as pipeline
import review_rules
from llm_client import CodexConfig
from review_cache import provider_signature, review_input_signature, review_signature
from test_desktop_tasks import TaskTestCase
from test_review_pipeline import PipelineCase


class ReviewRulesBodyTests(unittest.TestCase):
    def test_legacy_complex_rule_detection_is_specific(self):
        legacy = "六项核心职责逐项引用原文证据，输出能力差距和 target_relevance。"
        self.assertTrue(review_rules.is_legacy_complex_review_rules(legacy))
        self.assertTrue(review_rules.is_legacy_complex_review_rules(
            "六项职责分别判断，并提供每项职责原文证据与能力差距。"))
        for ordinary in (
            "请判断岗位是否符合目标方向，并简要说明理由。",
            "考虑经验要求和证据是否充分，信息不足时说明原因。",
            "关注六项要求，结合岗位方向给出整体判断。",
            "只判断职责与岗位方向是否相关；说明主要原因。",
        ):
            with self.subTest(ordinary=ordinary):
                self.assertFalse(review_rules.is_legacy_complex_review_rules(ordinary))

    def test_editable_default_omits_schema_and_composition_keeps_fixed_protocol(self):
        default = review_rules.default_review_rules()
        self.assertIn("不要仅凭通用关键词判断", default)
        self.assertIn("主业为代码开发", default)
        self.assertIn("产品或方案岗位只是要求懂技术、配合开发", default)
        self.assertNotIn("## 严格 JSON 输出", default)
        self.assertNotIn('"job_id":', default)
        source = processor.load_prompt()
        custom = "只根据完整职位职责判断；任职要求仅用来识别能力差距。"
        actual = review_rules.compose_review_prompt(custom, default_prompt=source)
        self.assertEqual(actual.split("## 严格 JSON 输出", 1)[0].strip(), custom)
        self.assertEqual(actual.split("## 严格 JSON 输出", 1)[1].strip(),
                         source.split("## 严格 JSON 输出", 1)[1].strip())
        self.assertEqual(processor.load_prompt(), source)
        protocol = actual.split("## 严格 JSON 输出", 1)[1]
        self.assertIn('"判断"', protocol)
        self.assertIn('"理由"', protocol)
        for legacy in ('"job_id"', '"dimensions"', '"target_relevance"', '"confidence"', '"ability_gaps"'):
            self.assertNotIn(legacy, protocol)
        for invalid in (None, 1, "", " \n\t"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                review_rules.compose_review_prompt(invalid)


class TaskReviewRulesTests(TaskTestCase):
    def test_llm_reset_keeps_rules_until_fresh_execution_then_migrates_legacy(self):
        self.run_all()
        legacy = "旧自定义：六项职责命中至少四项，输出原文证据与能力差距。"
        self.task.set_review_rules(legacy)
        self.assertEqual(self.task.get_review_rules(), legacy)

        self.task.reset_stage("llm")

        self.assertEqual(self.task.get_review_rules(), legacy, "reset itself must not rewrite rules")
        self.assertEqual(self.task.stage_info("llm")["status"], "pending")
        self.assertTrue(self.task.stage_info("llm")["reset_pending"])
        self.task.begin("llm")
        self.assertEqual(self.task.get_review_rules(), review_rules.default_review_rules())
        self.assertIsNone(self.task.snapshot().get("review_rules"))
        self.assertEqual(self.task.stage_info("llm")["status"], "running")
        self.assertEqual(self.task.stage_info("llm")["redo_count"], 1)
        self.task.finish("llm", self.task.input_jobs("llm"))

    def test_normal_llm_begin_and_new_custom_rules_are_never_migrated(self):
        self.run_all()
        legacy = "旧自定义：六项职责命中至少四项，输出原文证据与能力差距。"
        self.task.set_review_rules(legacy)
        self.task.begin("llm")
        self.assertEqual(self.task.get_review_rules(), legacy)
        self.task.interrupt("llm")

        custom = "新规则：判断是否属于我的目标岗位，只用一句话说明。"
        self.task.set_review_rules(custom)
        self.task.reset_stage("llm")
        self.assertEqual(self.task.get_review_rules(), custom)
        self.task.begin("llm")
        self.assertEqual(self.task.get_review_rules(), custom)
        self.task.interrupt("llm")

    def test_redo_of_another_stage_does_not_migrate_legacy_rules(self):
        legacy = "旧自定义：六项职责命中至少四项，输出原文证据与能力差距。"
        self.task.set_review_rules(legacy)
        self.run_all()
        self.task.reset_stage("shortlist")
        self.assertEqual(self.task.get_review_rules(), legacy)
        self.task.begin("shortlist")
        self.task.finish("shortlist", self.task.input_jobs("shortlist"))

    def test_legacy_default_save_reopen_and_restore_are_local_and_idempotent(self):
        source_before = self.raw.read_bytes()
        default = self.task.get_review_rules()
        self.assertEqual(default, review_rules.default_review_rules())
        before = self.task.snapshot()
        self.task.set_review_rules(None)
        self.task.set_review_rules(default)
        self.assertEqual(self.task.snapshot(), before)

        self.task.set_review_rules(" \n自定义职责判断规则。\n ")
        saved = self.task.snapshot()
        self.assertEqual(saved["review_rules"]["text"], "自定义职责判断规则。")
        self.assertTrue(saved["review_rules"]["updated_at"])
        reopened = tasks.ReviewTask.open(self.task.path)
        self.assertEqual(reopened.get_review_rules(), "自定义职责判断规则。")
        reopened.set_review_rules("自定义职责判断规则。")
        self.assertEqual(reopened.snapshot(), saved)
        reopened.set_review_rules(None)
        self.assertFalse(reopened.snapshot().get("review_rules"))
        self.assertEqual(reopened.get_review_rules(), default)
        restored = reopened.snapshot()
        reopened.set_review_rules(None)
        self.assertEqual(reopened.snapshot(), restored)
        self.assertEqual(self.raw.read_bytes(), source_before)

    def test_windows_line_endings_have_one_effective_rule_identity(self):
        self.task.set_review_rules("第一行规则。\r\n第二行规则。")
        self.assertEqual(self.task.get_review_rules(), "第一行规则。\n第二行规则。")
        before = self.task.snapshot()
        self.task.set_review_rules("第一行规则。\n第二行规则。")
        self.assertEqual(self.task.snapshot(), before)
        prompt = review_rules.compose_review_prompt("第一行规则。\r\n第二行规则。")
        self.assertNotIn("\r", prompt)

    def test_empty_invalid_and_running_edits_never_commit(self):
        before = self.task.snapshot()
        for invalid in ("", " \t\n", 0, {}, []):
            with self.subTest(invalid=invalid), self.assertRaises(tasks.TaskStateError):
                self.task.set_review_rules(invalid)
            self.assertEqual(self.task.snapshot(), before)
        self.task.set_review_rules("当前任务规则")
        other = tasks.ReviewTask.open(self.task.path)
        self.task.begin("import")
        running = self.task.snapshot()
        for editor, value in ((self.task, "新规则"), (other, None), (other, "当前任务规则")):
            with self.assertRaises(tasks.TaskStateError):
                editor.set_review_rules(value)
        self.assertEqual(self.task.snapshot(), running)
        self.task.interrupt("import")

    def test_pending_ai_inputs_survive_a_rule_change(self):
        self.run_import()
        self.run_stage("shortlist", {key: {"list_pass": True} for key in "abc"})
        self.run_stage("fetch-jd", {key: {"jd": f"JD {key}"} for key in "abc"})
        before = self.task.snapshot()
        self.task.set_review_rules("新的语义判断规则")
        after = self.task.snapshot()
        for stage in tasks.STAGES:
            self.assertEqual(after["stages"][stage], before["stages"][stage])
        self.assertEqual([tasks.job_id(job) for job in self.task.input_jobs("llm")], list("abc"))

    def test_rule_edit_invalidates_only_ai_and_report_and_preserves_manual_results(self):
        self.run_all()
        self.task.set_keep("llm", ["b"], False)
        self.task.confirm("llm")
        self.run_stage("report")
        before = self.task.snapshot()
        result_paths = [self.task.path / before["stages"][stage]["result_file"] for stage in ("llm", "report")]
        previous_files = {path: path.read_bytes() for path in result_paths}
        self.task.set_review_rules("只判断有真实职责证据的方向。")
        after = self.task.snapshot()
        for stage in ("import", "shortlist", "fetch-jd"):
            self.assertEqual(after["stages"][stage], before["stages"][stage])
        for stage in ("llm", "report"):
            info, old = after["stages"][stage], before["stages"][stage]
            self.assertEqual(info["status"], "stale")
            self.assertFalse(info["confirmed"])
            self.assertEqual(info["invalidated_by"], "review_rules")
            for field in ("result_file", "input_file", "manual_choices", "keep_ids", "excluded_ids"):
                self.assertEqual(info[field], old[field])
            with self.assertRaises(tasks.TaskStateError):
                self.task.confirm(stage)
        self.assertEqual({path: path.read_bytes() for path in result_paths}, previous_files)
        unchanged = self.task.snapshot()
        self.task.set_review_rules("只判断有真实职责证据的方向。")
        self.assertEqual(self.task.snapshot(), unchanged)
        self.run_stage("llm", {key: {"llm_review": {
            "job_id": key, "status": "success", "review_format": "simple-v1",
            "判断": "符合", "理由": "按新规则重新完成判断"}} for key in "abc"})
        self.assertEqual(self.task.stage_info("llm")["manual_choices"], {"b": False})
        self.assertNotIn("b", self.task.stage_info("llm")["keep_ids"])
        self.assertEqual({path: path.read_bytes() for path in result_paths}, previous_files)

    def test_failed_atomic_save_preserves_prior_rules_and_stage_confirmation(self):
        self.run_all()
        before = self.task.snapshot()
        path = self.task.path / "task_manifest.json"
        original_bytes = path.read_bytes()
        with patch.object(tasks.os, "replace", side_effect=OSError("simulated disk error")):
            with self.assertRaises(tasks.TaskStateError):
                self.task.set_review_rules("未保存的新规则")
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(self.task.snapshot(), before)

    def test_corrupt_optional_override_is_rejected_but_missing_field_is_supported(self):
        base = self.task.snapshot()
        for invalid in ("text", {}, {"text": " ", "updated_at": "date"},
                        {"text": "rule", "updated_at": None}, {"text": 4, "updated_at": "date"}):
            candidate = {**base, "review_rules": invalid}
            with self.subTest(invalid=invalid), self.assertRaises(tasks.TaskStateError):
                self.task._validate(candidate)
        self.task._validate({**base, "review_rules": None})


class CompleteSemanticDutiesTests(unittest.TestCase):
    def test_named_duty_sections_preserve_every_sentence_and_separate_requirements_company(self):
        body = "1. 与业务同事共创从用户问题到可用服务的路径。\n2. 将散落制度变成能回答追问的助手。\n3. Agent、RAG、Workflow。"
        for heading in ("职位职责", "岗位职责", "工作内容"):
            job = {"job_id": "complete-body", "title": "标题专用词", "skills": ["技能专用词"],
                   "search_keyword": "搜索专用词", "company": "公司字段专用词",
                   "jd": f"公司介绍\n公司介绍专用词：全球用户。\n{heading}\n{body}\n"
                         "任职要求\n熟悉任职专用词；至少三年经验。\n福利待遇\n福利专用词。"}
            semantic = processor.model_input(job)
            with self.subTest(heading=heading):
                self.assertEqual(semantic["responsibilities"], body)
                self.assertEqual(semantic["requirements"], "熟悉任职专用词；至少三年经验。")
                for outside in ("标题专用词", "技能专用词", "搜索专用词", "公司字段专用词", "公司介绍专用词", "任职专用词", "福利专用词"):
                    self.assertNotIn(outside, semantic["responsibilities"])
                self.assertEqual(set(semantic), {"job_id", "responsibilities", "requirements"})
                self.assertNotIn("将散落制度", processor.extract_jd_sections(job["jd"]).get("responsibilities", ""))

    def test_model_evidence_uses_full_duties_without_fixed_verbs_and_never_requirements(self):
        duty = "同业务同事共创从用户问题到可用服务的路径。"
        requirement = "熟悉Agent、RAG、Workflow与知识库。"
        company = "公司负责方案设计与系统集成。"
        job = {"job_id": "rare-verb", "salary": "20-30K", "jd_completeness": "complete",
               "jd": f"职位职责\n{duty}\n任职要求\n{requirement}\n公司介绍\n{company}"}
        self.assertIsNone(processor._DUTY_ACTION_RE.search(duty))
        review = {"status": "success", "target_relevance": {"assessment": "direct", "evidence": [duty]},
                  "dimensions": {name: {"present": True, "evidence": [duty]} for name in processor.DIMENSIONS[:4]}}
        item = processor.merge_model_review(job, review)
        self.assertEqual(item["target_relevance"]["assessment"], "direct")
        self.assertEqual(item["jd_dimension_count"], 4)
        self.assertEqual(item["model_evidence"], [duty])
        self.assertEqual(item["classification"], "重点投递")
        for foreign in (requirement, company):
            contaminated = copy.deepcopy(review)
            contaminated["target_relevance"]["evidence"] = [foreign]
            for dimension in contaminated["dimensions"].values():
                dimension["evidence"] = [foreign]
            result = processor.merge_model_review(job, contaminated)
            self.assertEqual(result["target_relevance"]["assessment"], "insufficient")
            self.assertEqual(result["model_evidence"], [])
            self.assertEqual(result["jd_dimension_count"], 0)

    def test_unheaded_duties_are_not_filtered_by_verbs_and_requirements_remain_separate(self):
        duty = "同业务同事共创服务路径。"
        semantic = processor.model_input({"job_id": "unheaded", "jd": duty + "\n熟悉Python，有三年经验。"})
        self.assertEqual(semantic["responsibilities"], duty)
        self.assertIn("熟悉Python", semantic["requirements"])
        with_requirements_heading = processor.model_input({"jd": duty + "\n任职要求\n熟悉Python。"})
        self.assertEqual(with_requirements_heading["responsibilities"], duty)
        self.assertEqual(with_requirements_heading["requirements"], "熟悉Python。")
        with_duty_heading = processor.model_input({"jd": duty + "\n岗位职责\n与专家共创问答助手。\n任职要求\n熟悉Python。"})
        self.assertEqual(with_duty_heading["responsibilities"], duty + "\n与专家共创问答助手。")
        customer_duty = "同客户澄清要求并共创方案。"
        self.assertEqual(processor.model_input({"jd": customer_duty})["responsibilities"], customer_duty)

    def test_sales_work_is_retained_for_grounding_exclusion_evidence(self):
        duty = "向客户介绍Agent产品并争取新签订单。"
        job = {"job_id": "sales", "jd": "岗位职责\n" + duty, "jd_completeness": "complete"}
        self.assertEqual(processor.model_input(job)["responsibilities"], duty)
        self.assertEqual(processor._grounded_duty_quotes([duty], processor.model_input(job)["responsibilities"]), [duty])


class CustomRulePipelineTests(PipelineCase):
    def setUp(self):
        super().setUp()
        self.configured = CodexConfig(model="fixture-model", reasoning_effort="max",
                                      base_url="https://fixture-only.test/v1", api_key="fixture-only-key")
        self.requests = []
        self.client = Mock()

        def request(job, *, prompt):
            self.requests.append((copy.deepcopy(job), prompt))
            return {"判断": "符合", "理由": "负责业务方案设计与AI应用交付。"}

        self.client.review_job.side_effect = request
        for patcher in (
            patch("llm_client.load_codex_config", return_value=self.configured),
            patch("llm_client.CodexResponsesClient", return_value=self.client),
            patch.object(pipeline, "_provider_hash", return_value=provider_signature(self.configured)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_mock_child(self, task, stage, command, control, emit, monitor=None):
        self.assertEqual(stage, "llm")
        prompt_file = Path(command[command.index("--prompt-file") + 1])
        processor.run_llm(task.run_directory(stage), workers=2, prompt_file=prompt_file)
        if monitor:
            monitor()
        return 0

    def test_custom_snapshot_reaches_requests_cache_and_offline_reopened_report(self):
        self.task.set_review_rules("只基于完整职位职责判断，任职要求用于能力差距；这是任务专用规则。")
        self.seed_llm_input(complete=("a",), partial=("b",))
        source_before = {path: path.read_bytes() for path in self.source.rglob("*.json")}
        with patch.object(pipeline, "_run_child", side_effect=self.run_mock_child) as child:
            first = self.run_step("llm")
            second = self.run_step("llm")
        self.assertEqual(child.call_count, 1)
        self.assertEqual(len(self.requests), 1)
        payload, prompt = self.requests[0]
        self.assertEqual(payload["job_id"], "a")
        self.assertTrue(prompt.startswith(self.task.get_review_rules() + "\n\n"))
        self.assertIn("## 严格 JSON 输出", prompt)
        reference = first["stages"]["llm"]["stats"]["review_prompt_file"]
        self.assertEqual((self.task.path / reference).read_text(encoding="utf-8"), prompt)
        self.assertEqual(second["stages"]["llm"]["stats"]["llm_cache_hit_count"], 1)
        row = self.read(self.task.run_directory("llm") / "prepared/llm_reviews.json")["reviews"][0]
        self.assertEqual(row["cache_input_signature"], review_input_signature(payload, prompt))
        self.assertEqual(row["cache_signature"], review_signature(payload, prompt, provider_signature(self.configured)))

        self.task.confirm("llm")
        self.task = tasks.ReviewTask.open(self.task.path)
        with patch.object(processor, "load_prompt", side_effect=AssertionError("report must use task snapshot")), \
                patch("llm_client.load_codex_config", side_effect=AssertionError("report must stay offline")):
            report = self.run_step("report")
        records = self.read(Path(report["stages"]["report"]["stats"]["json_path"]))["jobs"]
        self.assertEqual(next(job for job in records if job["job_id"] == "a")["model_cache_validation"], "input_match")
        self.assertEqual({path: path.read_bytes() for path in source_before}, source_before)

    def test_editing_rules_invalidates_old_cache_and_keeps_old_prompts_and_manual_choices(self):
        self.task.set_review_rules("第一版规则：只根据实际职责做语义判断。")
        self.seed_llm_input()
        with patch.object(pipeline, "_run_child", side_effect=self.run_mock_child) as child:
            first = self.run_step("llm")
            self.task.set_keep("llm", ["b"], False)
            self.task.confirm("llm")
            self.run_step("report")
            old_reference = first["stages"]["llm"]["stats"]["review_prompt_file"]
            old_prompt = (self.task.path / old_reference).read_bytes()
            old_result = self.task.path / self.task.stage_info("llm")["result_file"]
            old_bytes = old_result.read_bytes()
            self.task.set_review_rules("第二版规则：补充新的业务方向判断。")
            self.assertEqual(self.task.stage_info("report")["status"], "stale")
            second = self.run_step("llm")
        self.assertEqual(child.call_count, 2)
        self.assertEqual(len(self.requests), 6)
        stats = second["stages"]["llm"]["stats"]
        self.assertEqual((stats["llm_cache_hit_count"], stats["llm_cache_invalidated_count"], stats["llm_request_count"]), (0, 3, 3))
        self.assertNotEqual(stats["review_prompt_file"], old_reference)
        self.assertEqual((self.task.path / old_reference).read_bytes(), old_prompt)
        self.assertEqual(old_result.read_bytes(), old_bytes)
        self.assertEqual(self.task.stage_info("llm")["manual_choices"], {"b": False})
        self.assertNotIn("b", self.task.stage_info("llm")["keep_ids"])
        history = self.read(self.task.run_directory("llm") / "prepared/llm_reviews.json")["previous_reviews"]
        self.assertEqual(len(history), 3)
        self.assertEqual({entry["reason"] for entry in history}, {"signature_changed"})

    def test_llm_redo_uses_simplified_prompt_after_legacy_rule_migration(self):
        legacy = "六项核心职责命中至少四项，逐项提供原文证据和能力差距；target_relevance 必须分级。"
        self.task.set_review_rules(legacy)
        self.seed_llm_input()

        first = self.run_step("llm")
        first_prompt = (self.task.path / first["stages"]["llm"]["stats"]["review_prompt_file"])
        self.assertTrue(first_prompt.read_text(encoding="utf-8").startswith(legacy))
        self.task.confirm("llm")

        reset = self.run_step("llm", redo=True)

        self.assertEqual(reset["stages"]["llm"]["status"], "pending")
        self.assertEqual(self.task.get_review_rules(), legacy, "reset must not change rules")
        second = self.run_step("llm")

        self.assertIsNone(self.task.snapshot().get("review_rules"))
        prompt_path = self.task.path / second["stages"]["llm"]["stats"]["review_prompt_file"]
        prompt = prompt_path.read_text(encoding="utf-8")
        self.assertTrue(prompt.startswith(review_rules.default_review_rules() + "\n\n"))
        self.assertNotIn("target_relevance", prompt)
        self.assertNotIn("六项核心职责命中至少四项", prompt)
        self.assertNotEqual(first_prompt, prompt_path)
        self.assertFalse(first_prompt.exists())

    def test_missing_or_changed_saved_snapshot_is_not_validated_against_global_default(self):
        self.task.set_review_rules("本任务完整规则。")
        self.seed_llm_input(complete=("a",))
        with patch.object(pipeline, "_run_child", side_effect=self.run_mock_child):
            self.run_step("llm")
        self.task.confirm("llm")
        info = self.task.stage_info("llm")
        path = self.task.path / info["stats"]["review_prompt_file"]
        original = path.read_bytes()
        for missing in (False, True):
            if missing:
                path.unlink()
            else:
                path.write_text("被修改的快照", encoding="utf-8")
            with patch.object(processor, "load_prompt", side_effect=AssertionError("must not use default")), \
                    patch("llm_client.load_codex_config", side_effect=AssertionError("must not read credentials")):
                report = self.run_step("report")
            rows = self.read(Path(report["stages"]["report"]["stats"]["json_path"]))["jobs"]
            self.assertEqual(next(row for row in rows if row["job_id"] == "a")["model_cache_validation"], "prompt_unavailable")
            path.write_bytes(original)

    def test_custom_rules_with_no_complete_jd_never_read_provider_or_launch_child(self):
        self.task.set_review_rules("只在完整JD下审查。")
        self.seed_llm_input(complete=(), partial=("b",))
        with patch.object(pipeline, "_provider_hash", side_effect=AssertionError("incomplete JD must not read auth")), \
                patch.object(processor, "load_prompt", side_effect=AssertionError("no candidates need no prompt")), \
                patch.object(pipeline, "_run_child", side_effect=AssertionError("must not start requests")):
            snapshot = self.run_step("llm")
            self.task.confirm("llm")
            self.run_step("report")
        self.assertEqual(snapshot["stages"]["llm"]["stats"]["llm_request_count"], 0)
        self.assertEqual(self.requests, [])


class PromptFileCliTests(unittest.TestCase):
    def test_cli_passes_explicit_snapshot_and_keeps_default_call_signature_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt_file = root / "task-prompt.md"
            with patch.object(processor, "run_llm") as llm, patch.object(processor, "report_stage") as report, \
                    redirect_stdout(io.StringIO()):
                processor.main(["--root", str(root), "--stage", "llm", "--prompt-file", str(prompt_file)])
                self.assertEqual(llm.call_args.kwargs, {"conversation_size": 1, "prompt_file": prompt_file})
                processor.main(["--root", str(root), "--stage", "report", "--prompt-file", str(prompt_file)])
                self.assertEqual(report.call_args.kwargs, {"prompt_file": prompt_file})
                processor.main(["--root", str(root), "--stage", "llm"])
                self.assertEqual(llm.call_args.kwargs, {"conversation_size": 1})
                processor.main(["--root", str(root), "--stage", "report"])
                self.assertEqual(report.call_args.kwargs, {})

    def test_parallel_requests_and_signatures_share_one_immutable_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "prepared/jd_shortlists"
            folder.mkdir(parents=True)
            prompt_file = root / "task-prompt.md"
            prompt_file.write_text("任务规则快照", encoding="utf-8")
            jobs = [{"job_id": key, "title": "标题字段"} for key in ("one", "two")]
            details = [{"job_id": key, "jd": "职位职责\n同业务专家共创可问答的企业知识服务。\n任职要求\n熟悉Python。"}
                       for key in ("one", "two")]
            (root / "prepared/merged_jobs_index.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
            (folder / "jd_input.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
            (folder / "details.json").write_text(json.dumps(details), encoding="utf-8")
            config = CodexConfig(model="fixture", reasoning_effort="max", base_url="https://fixture.test/v1", api_key="fixture-key")
            observed = []

            def request(job, *, prompt):
                observed.append((dict(job), prompt))
                prompt_file.write_text("后续修改不能影响已开始的批次", encoding="utf-8")
                return {"判断": "符合", "理由": "负责企业知识服务的应用设计。"}

            client = Mock()
            client.review_job.side_effect = request
            with patch("llm_client.load_codex_config", return_value=config), \
                    patch("llm_client.CodexResponsesClient", return_value=client), \
                    patch.object(processor, "load_prompt", side_effect=AssertionError("explicit prompt bypasses defaults")):
                result = processor.run_llm(root, workers=2, prompt_file=prompt_file)
            self.assertEqual(result["completed_count"], 2)
            self.assertEqual({prompt for _, prompt in observed}, {"任务规则快照"})
            for payload, _ in observed:
                self.assertEqual(payload["responsibilities"], "同业务专家共创可问答的企业知识服务。")
                self.assertEqual(payload["requirements"], "熟悉Python。")
            reviews = json.loads((root / "prepared/llm_reviews.json").read_text(encoding="utf-8"))["reviews"]
            inputs = {job["job_id"]: job for job, _ in observed}
            for row in reviews:
                self.assertEqual(row["cache_input_signature"], review_input_signature(inputs[row["job_id"]], "任务规则快照"))
            # Explicit CLI reporting uses the same local snapshot and never
            # falls through to the project default or provider credentials.
            prompt_file.write_text("任务规则快照", encoding="utf-8")
            with patch("llm_client.load_codex_config", side_effect=AssertionError("report must remain offline")), \
                    patch.object(processor, "load_prompt", side_effect=AssertionError("explicit report bypasses defaults")):
                processor.report_stage(root, prompt_file=prompt_file)
            report = json.loads((root / "prepared/merged_jobs_index.json").read_text(encoding="utf-8"))
            self.assertEqual({row["model_cache_validation"] for row in report["jobs"]}, {"input_match"})


if __name__ == "__main__":
    unittest.main()
