"""Synthetic, offline acceptance coverage for independent delivery JSON."""

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import delivery_export as delivery
from desktop_tasks import REVIEW_STAGES, STAGES, ReviewTask, TaskStateError, _file_version, job_id
from process_architecture import model_input
from review_cache import review_input_signature


class DeliveryCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        for track in ("current", "target"):
            (self.source / "raw" / track).mkdir(parents=True)
        self.task = ReviewTask.create(self.source, self.root / "tasks", "上海中文投递测试", city="上海")
        self.addCleanup(self.task._release_run)
        self.rows = [{"job_id": key, "title": f"企业 AI 架构岗位 {key}", "company": "合成测试公司",
                      "location": "上海·浦东新区", "salary": "25-40K·16薪",
                      "job_link": f"https://www.zhipin.com/job_detail/Boss{key}.html?lid=fixture&securityId=navigation-hint",
                      "skills": ["RAG", "Agent"], "job_labels": ["本科", "3-5年"]}
                     for key in "abcd"]

    def ready(self, rows=None, *, judgments=None, manual=(), prompt=True, confirm=True):
        rows = self.rows if rows is None else rows
        self.task.begin("import")
        self.task.finish("import", rows)
        self.task.confirm("import")
        self.task.begin("shortlist")
        self.task.finish("shortlist", [{**row, "list_pass": True} for row in self.task.input_jobs("shortlist")])
        self.task.confirm("shortlist")
        self.task.begin("fetch-jd")
        self.task.finish("fetch-jd", [{**row, "jd": "岗位职责：负责企业需求分析、方案设计与系统集成。\n任职要求：了解 RAG。",
                                      "jd_completeness": "complete"} for row in self.task.input_jobs("fetch-jd")])
        self.task.confirm("fetch-jd")
        self.prompt = "这是合成测试规则；仅返回判断和理由。\n"
        digest = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()
        self.prompt_path = self.task.path / f"runtime/llm/review_prompts/{digest}.md"
        stats = {}
        if prompt:
            self.prompt_path.parent.mkdir(parents=True, exist_ok=True)
            self.prompt_path.write_text(self.prompt, encoding="utf-8", newline="\n")
            stats["review_prompt_file"] = self.prompt_path.relative_to(self.task.path).as_posix()
        self.task.begin("llm")
        outputs = []
        for row in self.task.input_jobs("llm"):
            key = job_id(row)
            judgment = (judgments or {}).get(key, "符合")
            review = None
            if judgment == "failed":
                review = {"job_id": key, "status": "failed", "review_format": "simple-v1", "error": "synthetic failure"}
            elif judgment is not None:
                review = {"job_id": key, "status": "success", "review_format": "simple-v1", "判断": judgment,
                          "理由": "岗位原文说明承担企业方案设计与落地交付。", "cache_signature": "synthetic-provider-hash",
                          "cache_input_signature": review_input_signature(model_input(row), self.prompt)}
            outputs.append({**row, "review_format": "simple-v1", "llm_review": review})
        self.task.finish("llm", outputs, stats=stats)
        if manual:
            self.task.set_keep("llm", list(manual), True)
        if confirm:
            self.task.confirm("llm")
        return self.task

    def document(self, preview=None):
        preview = preview or delivery.preview_task(self.task)
        return {"schema_version": "1.0", "export_id": "a" * 32,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                **{key: copy.deepcopy(preview[key]) for key in ("source_task", "summary", "jobs", "blocked_jobs")}}

    def read(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def rewrite_llm(self, change):
        info = self.task.stage_info("llm")
        path = self.task.path / info["result_file"]
        document = self.read(path)
        change(document["jobs"])
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


class SelectionTests(DeliveryCase):
    def test_auto_keep_manual_overrides_and_pending_excluded_are_separate(self):
        self.ready(judgments={"b": "不符合", "c": "信息不足", "d": "failed"}, manual=("c", "d"))
        with patch("llm_client.load_codex_config", side_effect=AssertionError("provider access")), \
             patch("project_llm_settings.load_settings", side_effect=AssertionError("settings access")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network access")):
            preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"], {"selected_count": 3, "ready_count": 3, "blocked_count": 0})
        jobs = {item["title"][-1]: item for item in preview["jobs"]}
        self.assertEqual(set(jobs), set("acd"))
        self.assertEqual(jobs["a"]["selection"]["source"], "ai")
        self.assertEqual(jobs["c"]["ai_review"]["judgment"], "信息不足")
        self.assertEqual(jobs["c"]["selection"]["source"], "manual")
        self.assertEqual(jobs["d"]["ai_review"]["status"], "failed")
        self.assertEqual(jobs["d"]["selection"]["manual_keep"], True)
        self.assertIsNone(jobs["a"]["city"])
        self.assertIn("上海", jobs["a"]["location"])
        self.assertEqual(jobs["a"]["navigation"]["security_id"], "navigation-hint")
        self.assertEqual(jobs["a"]["job_id"], "a")

    def test_missing_ai_manual_keep_is_exportable_without_prompt(self):
        self.ready(rows=self.rows[:1], judgments={"a": None}, manual=("a",), prompt=False)
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"]["ready_count"], 1)
        self.assertEqual(preview["jobs"][0]["ai_review"]["validation"], "missing_review")

    def test_corrupt_review_identity_or_status_remains_blocked_even_when_manually_kept(self):
        self.ready(rows=self.rows[:2], manual=("a", "b"))
        def corrupt(rows):
            rows[0]["llm_review"]["job_id"] = "unrelated-job"
            rows[1]["llm_review"]["status"] = []
        self.rewrite_llm(corrupt)
        document = self.read(delivery.export_task(self.task))
        self.assertEqual(document["summary"], {"selected_count": 2, "ready_count": 0, "blocked_count": 2})
        self.assertEqual({row["ai_review"]["validation"] for row in document["blocked_jobs"]},
                         {"job_identity_changed", "invalid_status"})
        self.assertTrue(all("ai_review_invalid" in row["blocked_reasons"] for row in document["blocked_jobs"]))

    def test_pending_and_ai_excluded_do_not_count_as_selected(self):
        self.ready(judgments={"b": "不符合", "c": "信息不足", "d": None})
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"], {"selected_count": 1, "ready_count": 1, "blocked_count": 0})
        self.assertEqual(preview["jobs"][0]["title"][-1], "a")

    def test_empty_confirmed_task_exports_valid_empty_lists(self):
        self.ready(rows=[], prompt=False)
        path = delivery.export_task(self.task)
        document = self.read(path)
        self.assertEqual(document["summary"], {"selected_count": 0, "ready_count": 0, "blocked_count": 0})
        delivery.validate_export(document)

    def test_unconfirmed_and_stale_stages_are_rejected(self):
        self.ready(confirm=False)
        before = (self.task.path / "task_manifest.json").read_bytes()
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.preview_task(self.task)
        self.assertEqual((self.task.path / "task_manifest.json").read_bytes(), before)
        self.task.confirm("llm")
        self.task.set_review_rules("新的合成规则")
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.export_task(self.task)
        self.assertFalse((self.task.path / "delivery-exports").exists())

    def test_signature_or_prompt_mismatch_blocks_auto_keep_but_manual_authority_survives(self):
        self.ready(rows=self.rows[:2])
        self.rewrite_llm(lambda rows: rows[0]["llm_review"].update(cache_input_signature="wrong-input"))
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"], {"selected_count": 2, "ready_count": 1, "blocked_count": 1})
        blocked = preview["blocked_jobs"][0]
        self.assertIn("ai_review_invalid", blocked["blocked_reasons"])
        self.assertEqual(blocked["ai_review"]["validation"], "input_changed")
        self.task.set_keep("llm", ["a"], True)
        self.task.confirm("llm")
        self.prompt_path.unlink()
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"]["ready_count"], 1)
        self.assertEqual(preview["jobs"][0]["selection"]["source"], "manual")

    def test_unsigned_historic_ai_success_is_blocked(self):
        self.ready(rows=self.rows[:1], prompt=False)
        self.rewrite_llm(lambda rows: rows[0]["llm_review"].pop("cache_signature"))
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"]["blocked_count"], 1)
        self.assertEqual(preview["blocked_jobs"][0]["ai_review"]["validation"], "missing_signature")

    def test_llm_output_cannot_substitute_changed_input_text_for_signed_input(self):
        self.ready(rows=self.rows[:1])
        def change(rows):
            rows[0]["jd"] = "岗位职责：这是另一份不匹配输入的合成 JD。"
            rows[0]["llm_review"]["cache_input_signature"] = review_input_signature(model_input(rows[0]), self.prompt)
        self.rewrite_llm(change)
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["blocked_jobs"][0]["ai_review"]["validation"], "input_changed")


class IdentifierTests(DeliveryCase):
    def test_synthetic_job_id_is_not_a_boss_id_and_valid_explicit_id_is_sufficient(self):
        rows = copy.deepcopy(self.rows[:2])
        for row in rows:
            row.pop("job_link")
        rows[1]["encrypt_job_id"] = "RealBossIdentifier"
        self.ready(rows=rows)
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"], {"selected_count": 2, "ready_count": 1, "blocked_count": 1})
        self.assertEqual(preview["jobs"][0]["boss_job_id"], "RealBossIdentifier")
        self.assertIsNone(preview["jobs"][0]["job_url"])
        self.assertIn("missing_boss_identifier", preview["blocked_jobs"][0]["blocked_reasons"])

    def test_conflicting_ids_lookalike_host_are_blocked_but_missing_geography_is_optional(self):
        rows = copy.deepcopy(self.rows)
        rows[0]["encrypt_job_id"] = "DifferentBossIdentifier"
        rows[1].pop("location")
        rows[2]["job_link"] = "https://zhipin.com.attacker.test/job_detail/NotBoss.html"
        rows[3]["job_url"] = "https://www.zhipin.com/job_detail/OtherBoss.html"
        self.ready(rows=rows)
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"]["blocked_count"], 3)
        by_title = {row["title"][-1]: row["blocked_reasons"] for row in preview["blocked_jobs"]}
        self.assertIn("conflicting_identifiers", by_title["a"])
        self.assertIsNone(preview["jobs"][0]["city"])
        self.assertIsNone(preview["jobs"][0]["location"])
        self.assertEqual(preview["jobs"][0]["job_id"], "b")
        self.assertIn("invalid_job_url", by_title["c"])
        self.assertIn("conflicting_identifiers", by_title["d"])

    def test_navigation_hints_and_source_url_keep_original_values_without_unknown_query_fields(self):
        original = "http://www.zhipin.com/job_detail/Bossa.html?lid=list%2D1&securityId=opaque%2Bhint&ka=search"
        row = {**self.rows[0], "job_link": original, "encrypt_boss_id": "Recruiter1",
               "skills": "RAG | Agent｜Python", "job_labels": "本科 | 3-5年"}
        self.ready(rows=[row])
        item = self.read(delivery.export_task(self.task))["jobs"][0]
        self.assertEqual(item["job_id"], "a")
        self.assertEqual(item["boss_job_id"], "Bossa")
        self.assertEqual(item["navigation"]["original_job_url"], original)
        self.assertEqual(item["navigation"]["security_id"], "opaque+hint")
        self.assertEqual(item["navigation"]["encrypt_boss_id"], "Recruiter1")
        self.assertEqual(item["navigation"]["lid"], "list-1")
        self.assertIsNone(item["navigation"]["from"])
        self.assertEqual(item["skills"], ["RAG", "Agent", "Python"])
        self.assertEqual(item["job_labels"], ["本科", "3-5年"])
        self.assertEqual(item["job_url"], "https://www.zhipin.com/job_detail/Bossa.html")

    def test_recruiter_id_alone_never_becomes_job_target(self):
        row = {**self.rows[0], "job_link": None, "encryptBossId": "RecruiterOnly"}
        self.ready(rows=[row])
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"]["blocked_count"], 1)
        self.assertIsNone(preview["blocked_jobs"][0]["boss_job_id"])
        self.assertEqual(preview["blocked_jobs"][0]["navigation"]["encrypt_boss_id"], "RecruiterOnly")

    def test_duplicate_boss_targets_block_all_rows_without_losing_selected_count(self):
        rows = copy.deepcopy(self.rows[:2])
        rows[1]["job_link"] = rows[0]["job_link"]
        self.ready(rows=rows)
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"], {"selected_count": 2, "ready_count": 0, "blocked_count": 2})
        self.assertTrue(all("duplicate_target" in row["blocked_reasons"] for row in preview["blocked_jobs"]))
        self.assertEqual(len({row["job_key"] for row in preview["blocked_jobs"]}), 2)
        delivery.validate_export(self.document(preview))

    def test_identity_conflicts_across_review_stages_are_not_hidden_by_latest_row(self):
        self.ready(rows=self.rows[:1])
        self.rewrite_llm(lambda rows: rows[0].update(job_link="https://www.zhipin.com/job_detail/ChangedTarget.html"))
        preview = delivery.preview_task(self.task)
        self.assertIn("conflicting_identifiers", preview["blocked_jobs"][0]["blocked_reasons"])


class IsolationAndPersistenceTests(DeliveryCase):
    def test_report_checkpoint_and_artifact_contents_do_not_affect_export_or_first_four_audit(self):
        self.ready(rows=self.rows[:1])
        before = delivery.preview_task(self.task)
        self.task.begin("report")
        self.task.finish("report", [{**row, "title": "REPORT MUST NEVER BE EXPORTED", "location": "旧报告城市",
                                     "classification": "暂不投", "job_link": "https://www.zhipin.com/job_detail/OLD.html"}
                                    for row in self.task.input_jobs("report")])
        self.task.confirm("report")
        self.assertEqual(delivery.preview_task(self.task), before)
        self.assertEqual(self.task.all_jobs()[0]["title"], "REPORT MUST NEVER BE EXPORTED")
        current = self.task.all_jobs(through_stage="llm")[0]
        self.assertEqual(current["title"], self.rows[0]["title"])
        self.assertEqual(set(current["review_selection"]), set(REVIEW_STAGES))
        report = self.task.stage_info("report")
        (self.task.path / report["result_file"]).unlink()
        readonly = ReviewTask.open_readonly(self.task.path)
        self.assertEqual(delivery.preview_task(readonly), before)
        self.assertEqual(set(readonly.manifest["stages"]), set(STAGES))

    def test_readonly_open_never_recovers_running_checkpoint_or_writes_files(self):
        self.task.begin("import")
        self.task._release_run()
        before = {path.relative_to(self.task.path): path.read_bytes()
                  for path in self.task.path.rglob("*") if path.is_file()}
        readonly = ReviewTask.open_readonly(self.task.path)
        self.assertEqual(readonly.manifest["stages"]["import"]["status"], "running")
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.preview_task(readonly)
        with self.assertRaises(TaskStateError):
            readonly.begin("import")
        after = {path.relative_to(self.task.path): path.read_bytes()
                 for path in self.task.path.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_unique_exports_keep_stable_keys_and_never_overwrite_old_bytes(self):
        self.ready(rows=self.rows[:1])
        preview = delivery.preview_task(self.task)
        first = delivery.export_task(self.task, expected_snapshot=preview["snapshot_token"])
        saved = first.read_bytes()
        second = delivery.export_task(self.task)
        self.assertNotEqual(first.parent, second.parent)
        self.assertEqual(first.read_bytes(), saved)
        self.assertEqual(self.read(first)["jobs"][0]["job_key"], self.read(second)["jobs"][0]["job_key"])
        self.assertNotEqual(self.read(first)["export_id"], self.read(second)["export_id"])
        self.assertEqual(list(first.parent.iterdir()), [first])
        self.assertIn("上海中文投递测试", saved.decode("utf-8"))

    def test_field_whitelist_redacts_embedded_secrets_and_local_paths(self):
        row = {**self.rows[0], "cookies": "PRIVATE_COOKIE_VALUE", "provider_config": {"api_key": "PRIVATE_API_VALUE"},
               "source_file": "C:\\private\\source.json", "raw_model_payload": "PRIVATE_RAW_OUTPUT",
               "title": "合成岗位 api_key=PRIVATE_API_VALUE", "company": "C:\\private\\fixture\\config.json"}
        self.ready(rows=[row])
        self.rewrite_llm(lambda rows: rows[0]["llm_review"].update(raw_response="PRIVATE_RAW_OUTPUT",
                           理由="合成理由 C:\\private\\fixture\\auth.json Authorization: Bearer PRIVATE_HEADER_VALUE"))
        document = self.read(delivery.export_task(self.task))
        encoded = json.dumps(document, ensure_ascii=False)
        for value in ("PRIVATE_COOKIE_VALUE", "PRIVATE_API_VALUE", "PRIVATE_RAW_OUTPUT", "PRIVATE_HEADER_VALUE",
                      "C:\\private", "source_file", "raw_response", "provider_config", "review_prompt_file"):
            self.assertNotIn(value, encoded)

    def test_embedded_urls_and_normal_slash_terms_in_jd_are_not_redacted_as_paths(self):
        self.ready(rows=self.rows[:1], manual=("a",))
        text = "负责 RAG /Agent、CI/CD、Linux/Windows 方案；参考 https://example.test/guide。"
        self.rewrite_llm(lambda rows: rows[0].update(jd=text))
        self.assertEqual(delivery.preview_task(self.task)["jobs"][0]["jd"], text)

    def test_preview_cache_reuses_rows_and_returns_isolated_values(self):
        self.ready(rows=self.rows[:1])
        cache = delivery.ExportCache()
        with patch.object(delivery, "_current_rows", wraps=delivery._current_rows) as reads:
            first = delivery.preview_task(self.task, cache=cache)
            key = first["jobs"][0]["job_key"]
            first["jobs"][0]["job_key"] = "caller-mutated"
            second = delivery.preview_task(self.task, cache=cache)
            self.assertEqual(reads.call_count, 1)
            self.assertEqual(second["jobs"][0]["job_key"], key)
            self.task.set_keep("llm", ["a"], True)
            self.task.confirm("llm")
            delivery.preview_task(self.task, cache=cache)
            self.assertEqual(reads.call_count, 2)

    def test_same_size_mtime_checkpoint_rewrite_invalidates_cache(self):
        self.ready(rows=self.rows[:1])
        cache = delivery.ExportCache()
        before = delivery.preview_task(self.task, cache=cache)
        path = self.task.path / self.task.stage_info("llm")["result_file"]
        old = path.stat()
        token = _file_version(path)
        payload = path.read_text(encoding="utf-8").replace("Boss a", "Boss z")
        # Both replacement strings have equal length; mutate a non-input display field.
        payload = payload.replace("合成测试公司", "合成复测公司")
        path.write_text(payload, encoding="utf-8", newline="")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertNotEqual(_file_version(path), token)
        after = delivery.preview_task(self.task, cache=cache)
        self.assertNotEqual(after["snapshot_token"], before["snapshot_token"])
        self.assertEqual(after["jobs"][0]["company"], "合成复测公司")


class PublicationAndSchemaTests(DeliveryCase):
    def test_schema_rejects_bad_version_extra_secrets_count_types_and_missing_target(self):
        self.ready(rows=self.rows[:1])
        original = self.document()
        delivery.validate_export(original)
        variants = []
        wrong = copy.deepcopy(original)
        wrong["schema_version"] = "2.0"
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        wrong["jobs"][0]["cookies"] = "secret"
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        wrong["summary"]["selected_count"] = True
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        wrong["summary"]["selected_count"] = 2
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        wrong["jobs"][0].update(boss_job_id=None, job_url=None)
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        wrong["jobs"][0]["selection"]["manual_keep"] = False
        variants.append(wrong)
        for document in variants:
            with self.subTest(document=document):
                with self.assertRaises(delivery.DeliveryExportError):
                    delivery.validate_export(document)

    def test_expected_preview_rejects_same_keep_new_manual_intent(self):
        self.ready(rows=self.rows[:1])
        preview = delivery.preview_task(self.task)
        self.task.set_keep("llm", ["a"], True)
        self.task.confirm("llm")
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.export_task(self.task, expected_snapshot=preview["snapshot_token"])
        self.assertFalse((self.task.path / "delivery-exports").exists())

    def test_manifest_invalidation_cannot_reuse_a_ready_cached_preview(self):
        self.ready(rows=self.rows[:1])
        cache = delivery.ExportCache()
        preview = delivery.preview_task(self.task, cache=cache)
        path = self.task.path / "task_manifest.json"
        manifest = self.read(path)
        manifest["stages"]["llm"]["invalidated_by"] = "fetch-jd"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.export_task(self.task, expected_snapshot=preview["snapshot_token"], cache=cache)
        self.assertFalse((self.task.path / "delivery-exports").exists())

    def test_concurrent_selection_edit_before_publication_rejects_and_cleans_output(self):
        self.ready(rows=self.rows[:1])
        other = ReviewTask.open(self.task.path)
        parent = self.root / "exports"
        real_validate = delivery.validate_export
        validations = 0
        def change_after_validation(document, **kwargs):
            nonlocal validations
            real_validate(document, **kwargs)
            validations += 1
            if validations == 2:
                other.set_keep("llm", ["a"], False)
                other.confirm("llm")
        with patch.object(delivery, "validate_export", side_effect=change_after_validation):
            with self.assertRaises(delivery.DeliveryExportError):
                delivery.export_task(self.task, parent)
        self.assertEqual(list(parent.iterdir()), [])

    def test_publication_lease_blocks_writer_at_commit_point(self):
        self.ready(rows=self.rows[:1])
        other = ReviewTask.open(self.task.path)
        real_publish = delivery._publish
        attempts = []
        def competing_writer(temporary, destination):
            with self.assertRaises(TaskStateError):
                other.set_keep("llm", ["a"], False)
            attempts.append(True)
            real_publish(temporary, destination)
        with patch.object(delivery, "_publish", side_effect=competing_writer):
            path = delivery.export_task(self.task)
        self.assertTrue(path.is_file())
        self.assertEqual(attempts, [True])

    def test_write_failure_has_no_success_or_old_overwrite(self):
        self.ready(rows=self.rows[:1])
        first = delivery.export_task(self.task)
        before = first.read_bytes()
        with patch.object(delivery, "_publish", side_effect=OSError("secret diagnostic")):
            with self.assertRaises(delivery.DeliveryExportError) as caught:
                delivery.export_task(self.task)
        self.assertNotIn("secret diagnostic", str(caught.exception))
        self.assertEqual(first.read_bytes(), before)
        self.assertEqual(list(first.parent.parent.iterdir()), [first.parent])

    def test_validation_failure_publishes_nothing(self):
        self.ready(rows=self.rows[:1])
        with patch.object(delivery, "validate_export", side_effect=delivery.DeliveryExportError("schema failed")):
            with self.assertRaises(delivery.DeliveryExportError):
                delivery.export_task(self.task)
        self.assertFalse((self.task.path / "delivery-exports").exists())

    def test_cli_export_help_and_safe_failures(self):
        self.ready(rows=self.rows[:1])
        command = [sys.executable, str(PROJECT_ROOT / "delivery_export.py")]
        help_result = subprocess.run([*command, "--help"], capture_output=True, text=True, encoding="utf-8",
                                     env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--output-dir", help_result.stdout)
        parent = self.root / "cli-exports"
        result = subprocess.run([*command, "--task", str(self.task.path), "--output-dir", str(parent)],
                                capture_output=True, text=True, encoding="utf-8",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(Path(result.stdout.strip()).is_file())
        delivery.validate_export(self.read(result.stdout.strip()))
        bad = subprocess.run([*command, "--task", str(self.root / "api_key=PRIVATE_CLI_SECRET")],
                             capture_output=True, text=True, encoding="utf-8",
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertNotEqual(bad.returncode, 0)
        self.assertNotIn("PRIVATE_CLI_SECRET", bad.stdout + bad.stderr)
        invalid = subprocess.run([*command, "--unknown=PRIVATE_CLI_SECRET"], capture_output=True, text=True,
                                 encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(invalid.returncode, 2)
        self.assertNotIn("PRIVATE_CLI_SECRET", invalid.stdout + invalid.stderr)


if __name__ == "__main__":
    unittest.main()
