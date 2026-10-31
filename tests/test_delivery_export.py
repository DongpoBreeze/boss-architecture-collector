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
from process_architecture import collect_rows, merge_rows, model_input
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

    def ready(self, rows=None, *, judgments=None, manual=(), prompt=True, confirm=True, jd_completeness="complete"):
        rows = self.rows if rows is None else rows
        self.task.begin("import")
        self.task.finish("import", rows)
        self.task.confirm("import")
        self.task.begin("shortlist")
        self.task.finish("shortlist", [{**row, "list_pass": True} for row in self.task.input_jobs("shortlist")])
        self.task.confirm("shortlist")
        self.task.begin("fetch-jd")
        self.task.finish("fetch-jd", [{**row, "jd": "岗位职责：负责企业需求分析、方案设计与系统集成。\n任职要求：了解 RAG。",
                                      **({"jd_completeness": jd_completeness} if jd_completeness is not None else {})}
                                     for row in self.task.input_jobs("fetch-jd")])
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
        self.assertEqual(preview["jobs"][0]["ai_review"]["status"], "missing")
        self.assertEqual(preview["jobs"][0]["ai_review"]["validation"], "not_checked")

    def test_corrupt_review_identity_or_status_does_not_override_manual_keep(self):
        self.ready(rows=self.rows[:2], manual=("a", "b"))
        def corrupt(rows):
            rows[0]["llm_review"]["job_id"] = "unrelated-job"
            rows[1]["llm_review"]["status"] = []
        self.rewrite_llm(corrupt)
        document = self.read(delivery.export_task(self.task))
        self.assertEqual(document["summary"], {"selected_count": 2, "ready_count": 2, "blocked_count": 0})
        self.assertNotIn("blocked_jobs", document)
        self.assertEqual([row["ai_review"]["status"] for row in document["jobs"]], ["success", "invalid"])
        self.assertTrue(all(row["ai_review"]["validation"] == "not_checked" for row in document["jobs"]))
        self.assertTrue(all(row["selection"]["source"] == "manual" for row in document["jobs"]))

    def test_failed_reviews_remain_failed_and_exportable_when_manually_kept(self):
        self.ready(rows=self.rows[:2], judgments={"a": "failed", "b": "failed"}, manual=("a", "b"))
        self.rewrite_llm(lambda rows: rows[0]["llm_review"].update(job_id="other-job"))
        document = self.read(delivery.export_task(self.task))
        self.assertEqual(document["summary"], {"selected_count": 2, "ready_count": 2, "blocked_count": 0})
        self.assertEqual([row["job_id"] for row in document["jobs"]], ["a", "b"])
        self.assertTrue(all(row["ai_review"]["status"] == "failed" for row in document["jobs"]))
        self.assertTrue(all(row["ai_review"]["validation"] == "not_checked" for row in document["jobs"]))
        self.assertTrue(all("judgment" not in row["ai_review"] for row in document["jobs"]))

    def test_legacy_complete_jd_without_completeness_marker_is_exportable(self):
        self.ready(rows=self.rows[:1], jd_completeness=None)
        document = self.read(delivery.export_task(self.task))
        self.assertEqual(document["summary"], {"selected_count": 1, "ready_count": 1, "blocked_count": 0})
        self.assertEqual(document["jobs"][0]["ai_review"]["validation"], "not_checked")

    def test_manually_kept_partial_jd_is_not_validated_as_complete(self):
        self.ready(rows=self.rows[:1], jd_completeness="partial", manual=("a",))
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"]["ready_count"], 1)
        self.assertEqual(preview["jobs"][0]["ai_review"]["validation"], "not_checked")
        self.assertEqual(preview["jobs"][0]["selection"]["source"], "manual")

    def test_manually_kept_missing_jd_is_not_validated_as_complete(self):
        self.ready(rows=self.rows[:1], jd_completeness="missing", manual=("a",))
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"]["ready_count"], 1)
        self.assertEqual(preview["jobs"][0]["ai_review"]["validation"], "not_checked")
        self.assertEqual(preview["jobs"][0]["selection"]["source"], "manual")

    def test_pending_and_ai_excluded_do_not_count_as_selected(self):
        self.ready(judgments={"b": "不符合", "c": "信息不足", "d": None})
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"], {"selected_count": 1, "ready_count": 1, "blocked_count": 0})
        self.assertEqual(preview["jobs"][0]["title"][-1], "a")

    def test_manual_exclusion_remains_outside_export_even_with_a_readable_positive_review(self):
        self.ready(judgments={"c": "信息不足", "d": "failed"})
        self.task.set_keep("llm", ["a"], False)
        self.task.confirm("llm")
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"], {"selected_count": 1, "ready_count": 1, "blocked_count": 0})
        self.assertEqual([row["job_id"] for row in preview["jobs"]], ["b"])
        self.assertEqual(self.task.stage_info("llm")["manual_choices"]["a"], False)

    def test_all_ai_record_variants_in_confirmed_keep_remain_exportable_with_their_original_fields(self):
        # Establish final keeps first, then model historic or externally edited
        # AI records. Export must use those confirmed keeps without rejudging.
        cases = [
            ("unsigned", {"review_format": "simple-v1", "status": "success", "判断": "符合", "理由": "原理由"},
             "simple-v1", "success", "符合", "原理由"),
            ("failed", {"status": "failed", "判断": "不符合", "理由": "失败记录中已有的理由"},
             None, "failed", "不符合", "失败记录中已有的理由"),
            ("missing", None, None, "missing", None, None),
            ("absent", None, None, "missing", None, None),
            ("insufficient", {"status": "success", "判断": "信息不足", "理由": "需要补充岗位信息"},
             None, "success", "信息不足", "需要补充岗位信息"),
            ("negative", {"status": "success", "判断": "不符合", "理由": "已有否定判断"},
             None, "success", "不符合", "已有否定判断"),
            ("legacy", {"review_format": "rich-v0", "status": "success", "判断": "旧版待核实", "理由": "旧版原理由",
                        "dimensions": {"方案设计": {"present": True}}},
             "rich-v0", "success", "旧版待核实", "旧版原理由"),
            ("identity", {"job_id": "another-job", "status": "success", "判断": "符合", "理由": "身份不一致的已有记录"},
             None, "success", "符合", "身份不一致的已有记录"),
            ("signature", {"status": "success", "判断": "符合", "理由": "签名不一致的已有记录",
                           "cache_signature": [], "cache_input_signature": "wrong-signature"},
             None, "success", "符合", "签名不一致的已有记录"),
            ("custom_status", {"status": "cancelled", "判断": "待核实", "理由": "取消前已有的理由"},
             None, "cancelled", "待核实", "取消前已有的理由"),
            ("invalid_status", {"status": [], "判断": "符合", "理由": "状态不可读取，仍保留理由"},
             None, "invalid", "符合", "状态不可读取，仍保留理由"),
            ("invalid_fields", {"review_format": [], "status": "failed", "判断": [], "理由": "可读取的理由"},
             None, "failed", None, "可读取的理由"),
            ("judgment_only", {"status": "success", "判断": "符合"}, None, "success", "符合", None),
            ("reason_only", {"status": "success", "理由": "仅有理由"}, None, "success", None, "仅有理由"),
            ("invalid_record", ["unreadable"], None, "invalid", None, None),
            ("legacy_aliases", {"review_format": "legacy-en", "status": "success", "judgment": "original judgment",
                                 "reason": "original reason"},
             "legacy-en", "success", "original judgment", "original reason"),
        ]
        rows = [{**self.rows[0], "job_id": case[0],
                 "job_link": f"https://www.zhipin.com/job_detail/Case{index}.html"}
                for index, case in enumerate(cases)]
        self.ready(rows=rows)

        def replace_records(outputs):
            for row, (name, review, *_expected) in zip(outputs, cases):
                if name == "absent":
                    row.pop("llm_review")
                else:
                    row["llm_review"] = copy.deepcopy(review)
                for field in ("llm_stage_projection", "llm_stage_decision"):
                    row.pop(field, None)
                if name == "insufficient":
                    row.update(jd=None, jd_completeness="missing")

        self.rewrite_llm(replace_records)
        paths = [self.task.path / "task_manifest.json",
                 self.task.path / self.read(self.task.path / "task_manifest.json")["stages"]["llm"]["result_file"]]
        before = {path: path.read_bytes() for path in paths}
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"], {"selected_count": len(cases), "ready_count": len(cases), "blocked_count": 0})
        for item, (name, _review, format_name, status, judgment, reason) in zip(preview["jobs"], cases):
            with self.subTest(case=name):
                self.assertEqual(item["job_id"], name)
                self.assertEqual(item["ai_review"], {"format": format_name, "status": status,
                                 "judgment": judgment, "reason": reason, "validation": "not_checked"})
                self.assertEqual(item["selection"], {"stage": "llm", "confirmed": True, "keep": True,
                                                    "source": "ai", "manual_keep": None})
        delivery.validate_export(self.document(preview))
        full = self.read(delivery.export_task(self.task))
        delivery.validate_export(full)
        self.assertEqual(len(full["jobs"]), len(cases))
        projected = self.read(delivery.export_task(self.task, selected_fields=[
            "jobs.job_id", "jobs.ai_review.format", "jobs.ai_review.status", "jobs.ai_review.judgment",
            "jobs.ai_review.reason", "jobs.ai_review.validation", "jobs.selection.source",
        ]))
        self.assertEqual(projected, {"jobs": [{key: row[key] for key in ("job_id", "ai_review")} |
                                             {"selection": {"source": "ai"}} for row in full["jobs"]]})
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_delivery_readers_use_only_persisted_keeps_without_ai_reprojection_or_constructor_pollution(self):
        self.ready(judgments={"c": "不符合", "d": "信息不足"})

        def change_records(rows):
            rows[0]["llm_review"] = None
            rows[1]["llm_review"].update(status="failed", job_id="other-job")
            for row in rows:
                row.pop("llm_stage_projection", None)
                row.pop("llm_stage_decision", None)
            # A new readable positive review cannot import excluded/pending
            # records into the persisted final keep list either.
            for row in rows[2:]:
                row["llm_review"].update(判断="符合", 理由="后来的新判断")

        self.rewrite_llm(change_records)
        manifest_path = self.task.path / "task_manifest.json"
        stored = self.read(manifest_path)
        result_path = self.task.path / stored["stages"]["llm"]["result_file"]
        before = {path: path.read_bytes() for path in (manifest_path, result_path)}
        import desktop_tasks as tasks

        with patch.object(tasks, "_ai_recommendation", side_effect=AssertionError("AI rejudgment")):
            readonly = ReviewTask.open_readonly(self.task.path, preserve_selection=True)
            for reader in (self.task, readonly):
                with self.subTest(readonly=reader is readonly):
                    preview = delivery.preview_task(reader)
                    self.assertEqual([row["job_id"] for row in preview["jobs"]], ["a", "b"])
                    self.assertEqual(preview["summary"], {"selected_count": 2, "ready_count": 2, "blocked_count": 0})
                    self.assertEqual([row["ai_review"]["status"] for row in preview["jobs"]], ["missing", "failed"])
                    self.assertTrue(all(row["selection"]["source"] == "ai" for row in preview["jobs"]))
                    saved = self.read(delivery.export_task(reader, expected_snapshot=preview["snapshot_token"]))
                    self.assertEqual([row["job_id"] for row in saved["jobs"]], ["a", "b"])
                    delivery.validate_export(saved)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

        # Normal review loading retains its existing conservative migration.
        # Its projected in-memory state cannot overwrite the delivery view.
        normal = ReviewTask.open(self.task.path)
        self.assertFalse(normal.stage_info("llm")["confirmed"])
        self.assertEqual(normal.stage_info("llm")["keep_ids"], ["c", "d"])
        self.assertEqual(delivery.preview_task(normal), preview)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_legacy_missing_partition_fields_are_filled_from_stored_lists_without_inventing_manual_choices(self):
        self.ready(judgments={"c": "不符合", "d": "信息不足"})
        manifest_path = self.task.path / "task_manifest.json"
        stored = self.read(manifest_path)
        for info in stored["stages"].values():
            info.pop("pending_ids")
            info.pop("recommended_exclude_ids")
        manifest_path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        before = manifest_path.read_bytes()
        readonly = ReviewTask.open_readonly(self.task.path, preserve_selection=True)
        normalized = readonly.manifest["stages"]["llm"]
        self.assertEqual(normalized["pending_ids"], ["d"])
        self.assertEqual(normalized["recommended_exclude_ids"], ["c"])
        self.assertEqual(normalized["manual_choices"], {})
        self.assertTrue(normalized["confirmed"])
        expected = delivery.preview_task(self.task)
        self.assertEqual([row["job_id"] for row in expected["jobs"]], ["a", "b"])
        self.assertEqual(delivery.preview_task(readonly), expected)
        delivery.validate_export(self.read(delivery.export_task(readonly)))
        self.assertEqual(manifest_path.read_bytes(), before)

    def test_cli_exports_persisted_automatic_keep_even_if_ai_review_was_removed(self):
        self.ready(rows=self.rows[:1])
        self.rewrite_llm(lambda rows: (rows[0].pop("llm_review"), rows[0].pop("llm_stage_projection", None),
                                       rows[0].pop("llm_stage_decision", None)))
        parent = self.root / "cli-persisted-keep"
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "delivery_export.py"),
                                 "--task", str(self.task.path), "--output-dir", str(parent)],
                                capture_output=True, text=True, encoding="utf-8",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = self.read(result.stdout.strip())
        self.assertEqual(saved["summary"], {"selected_count": 1, "ready_count": 1, "blocked_count": 0})
        self.assertEqual(saved["jobs"][0]["selection"]["source"], "ai")
        self.assertEqual(saved["jobs"][0]["ai_review"], {"status": "missing", "validation": "not_checked"})

    def test_empty_confirmed_task_exports_metadata_and_zero_counts_without_empty_lists(self):
        self.ready(rows=[], prompt=False)
        preview = delivery.preview_task(self.task)
        fields = delivery.available_export_fields(preview)
        self.assertEqual(fields[:3], ("schema_version", "export_id", "generated_at"))
        self.assertTrue(all(not field.startswith(("jobs.", "blocked_jobs.")) for field in fields))
        for field in ("summary.selected_count", "summary.ready_count", "summary.blocked_count"):
            self.assertIn(field, fields)
        path = delivery.export_task(self.task)
        document = self.read(path)
        self.assertEqual(document["summary"], {"selected_count": 0, "ready_count": 0, "blocked_count": 0})
        self.assertNotIn("jobs", document)
        self.assertNotIn("blocked_jobs", document)
        self.assertEqual(path.name, "格式版本_导出ID_导出时间.json")
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

    def test_signature_or_prompt_changes_do_not_redecide_the_confirmed_keep_set(self):
        self.ready(rows=self.rows[:2])
        self.rewrite_llm(lambda rows: rows[0]["llm_review"].update(cache_input_signature="wrong-input"))
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"], {"selected_count": 2, "ready_count": 2, "blocked_count": 0})
        self.assertTrue(all(row["ai_review"]["validation"] == "not_checked" for row in preview["jobs"]))
        self.prompt_path.write_text("这是另一份提示词。", encoding="utf-8")
        changed_prompt = delivery.preview_task(self.task)
        self.assertEqual(changed_prompt["summary"], preview["summary"])
        self.assertEqual(changed_prompt["jobs"], preview["jobs"])
        self.task.set_keep("llm", ["a"], True)
        self.task.confirm("llm")
        self.prompt_path.unlink()
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"]["ready_count"], 2)
        self.assertEqual(preview["jobs"][0]["selection"]["source"], "manual")
        self.assertEqual(preview["jobs"][1]["selection"]["source"], "ai")

    def test_unsigned_historic_ai_success_is_exportable_without_prompt(self):
        self.ready(rows=self.rows[:1], prompt=False)
        self.rewrite_llm(lambda rows: rows[0]["llm_review"].pop("cache_signature"))
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"], {"selected_count": 1, "ready_count": 1, "blocked_count": 0})
        self.assertEqual(preview["jobs"][0]["ai_review"]["validation"], "not_checked")
        self.assertEqual(preview["jobs"][0]["selection"],
                         {"stage": "llm", "confirmed": True, "keep": True, "source": "ai", "manual_keep": None})

    def test_output_text_is_exported_without_comparing_ai_input_signatures(self):
        self.ready(rows=self.rows[:1])
        def change(rows):
            rows[0]["jd"] = "岗位职责：这是另一份不匹配输入的合成 JD。"
            rows[0]["llm_review"]["cache_input_signature"] = review_input_signature(model_input(rows[0]), self.prompt)
        self.rewrite_llm(change)
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"], {"selected_count": 1, "ready_count": 1, "blocked_count": 0})
        self.assertEqual(preview["jobs"][0]["ai_review"]["validation"], "not_checked")
        self.assertEqual(preview["jobs"][0]["jd"], "岗位职责：这是另一份不匹配输入的合成 JD。")

    def test_input_checkpoint_must_still_match_the_previous_confirmed_stage(self):
        self.ready(rows=self.rows[:1])
        path = self.task.path / self.task.stage_info("llm")["input_file"]
        document = self.read(path)
        document["jobs"][0]["jd"] = "与前一步已确认结果不同的输入。"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.preview_task(self.task)
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.export_task(self.task)
        self.assertFalse((self.task.path / "delivery-exports").exists())

    def test_ai_result_file_change_after_preview_invalidates_the_publication_token(self):
        self.ready(rows=self.rows[:1])
        preview = delivery.preview_task(self.task)
        self.rewrite_llm(lambda rows: rows[0]["llm_review"].update(理由="预览后的新原始理由"))
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.export_task(self.task, expected_snapshot=preview["snapshot_token"])
        self.assertFalse((self.task.path / "delivery-exports").exists())


class CompanyNameDeliveryTests(DeliveryCase):
    @staticmethod
    def legacy(row, **changes):
        result = {**row, "salary_source": "api", "boss_name": "旧 API 合成公司",
                  "company_scale": "100-499人", "company_stage": "", "company_industry": "软件",
                  "encrypt_brand_id": "", "company_link": "", **changes}
        result.pop("company", None)
        return result

    def test_collected_names_survive_all_four_checkpoints_and_full_or_company_only_export(self):
        rows = [dict(self.rows[0]), self.legacy(self.rows[1])]
        path = self.source / "raw/current/shanghai/current__shanghai__kw__salary-a__exp.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"city": "上海", "filters": {"salary": "", "experience": "不限", "degree": "203"},
                                    "jobs": rows}, ensure_ascii=False), encoding="utf-8")
        raw_before = path.read_bytes()
        config = {"current": {"keywords": [{"id": "kw", "query": "AI 架构"}],
                              "experiences": [{"id": "exp", "query": "不限"}]}}
        collected, _ = collect_rows(self.source, config, city="上海", sample_strategy="balanced-dedup")
        expected = {"a": "合成测试公司", "b": "旧 API 合成公司"}
        self.assertEqual({row["job_id"]: row["company"] for row in collected}, expected)
        self.task = ReviewTask.create(self.source, self.root / "collected-tasks", "采集公司名贯穿测试", city="上海")
        self.addCleanup(self.task._release_run)
        self.ready(rows=collected)
        for stage in REVIEW_STAGES:
            document = self.read(self.task.path / self.task.stage_info(stage)["result_file"])
            self.assertEqual({row["job_id"]: row["company"] for row in document["jobs"]}, expected)
        before = {file: file.read_bytes() for file in self.task.path.rglob("*") if file.is_file()}
        preview = delivery.preview_task(self.task)
        self.assertIn("jobs.company", delivery.available_export_fields(preview))
        full = self.read(delivery.export_task(self.task, self.root / "new-company-export"))
        delivery.validate_export(full)
        self.assertEqual({row["job_id"]: row["company"] for row in full["jobs"]}, expected)
        selected = delivery.export_task(self.task, self.root / "new-company-export", selected_fields=["jobs.company"])
        self.assertEqual(selected.name, "公司.json")
        self.assertEqual(self.read(selected), {"jobs": [{"company": expected[row["job_id"]]} for row in preview["jobs"]]})
        self.assertEqual({file: file.read_bytes() for file in before}, before)
        self.assertEqual(path.read_bytes(), raw_before)

    def test_old_api_task_is_recovered_readonly_without_model_validation_or_selection_changes(self):
        rows = [self.legacy(row, salary_source="api_empty" if row["job_id"] == "b" else "api")
                for row in self.rows[:3]]
        self.ready(rows=rows, judgments={"b": "failed"}, manual=("b",))
        self.task.set_keep("llm", ["c"], False)
        self.task.confirm("llm")
        before = {path: path.read_bytes() for path in self.task.path.rglob("*") if path.is_file()}
        manifest_before = self.read(self.task.path / "task_manifest.json")
        self.assertTrue(all("company" not in row for row in self.task.all_jobs(through_stage="llm")))
        with patch("llm_client.CodexResponsesClient", side_effect=AssertionError("model access")), \
             patch("project_llm_settings.load_settings", side_effect=AssertionError("settings access")), \
             patch("process_architecture.model_input", side_effect=AssertionError("AI input reconstruction")), \
             patch("review_cache.review_for_report", side_effect=AssertionError("AI validation")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network access")):
            reader = ReviewTask.open_readonly(self.task.path, preserve_selection=True)
            preview = delivery.preview_task(reader)
            self.assertEqual([row["job_id"] for row in preview["jobs"]], ["a", "b"])
            self.assertEqual([row["company"] for row in preview["jobs"]], ["旧 API 合成公司"] * 2)
            self.assertEqual([row["selection"]["source"] for row in preview["jobs"]], ["ai", "manual"])
            self.assertTrue(all(row["ai_review"]["validation"] == "not_checked" for row in preview["jobs"]))
            self.assertIn("jobs.company", delivery.available_export_fields(preview))
            full = self.read(delivery.export_task(reader, self.root / "old-company-export"))
            delivery.validate_export(full)
            subset = self.read(delivery.export_task(reader, self.root / "old-company-export", selected_fields=["jobs.company"]))
            self.assertEqual(subset, {"jobs": [{"company": "旧 API 合成公司"}] * 2})
            refreshed = delivery.preview_task(ReviewTask.open_readonly(self.task.path, preserve_selection=True))
            self.assertEqual([row["job_key"] for row in refreshed["jobs"]], [row["job_key"] for row in preview["jobs"]])
            self.assertEqual(refreshed, preview)
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "delivery_export.py"), "--task", str(self.task.path),
                                 "--output-dir", str(self.root / "old-cli-export"), "--fields", "jobs.company"],
                                capture_output=True, text=True, encoding="utf-8", timeout=30,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0, result.stderr)
        cli_path = Path(result.stdout.strip())
        self.assertEqual(cli_path.name, "公司.json")
        self.assertEqual(self.read(cli_path), subset)
        self.assertEqual(self.read(self.task.path / "task_manifest.json"), manifest_before)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_explicit_aliases_and_objects_beat_legacy_and_remain_redacted(self):
        rows = [self.legacy(self.rows[0], companyInfo={"name": "明确对象公司"}),
                self.legacy(self.rows[1], companyName="明确公司 Cookie=PRIVATE_COMPANY_SECRET"),
                self.legacy(self.rows[2], boss_name=r"旧公司 C:\private\company.json")]
        self.ready(rows=rows)
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["jobs"][0]["company"], "明确对象公司")
        self.assertIn("[REDACTED]", preview["jobs"][1]["company"])
        self.assertIn("[REDACTED PATH]", preview["jobs"][2]["company"])
        full = self.read(delivery.export_task(self.task))
        encoded = json.dumps(full, ensure_ascii=False)
        self.assertNotIn("PRIVATE_COMPANY_SECRET", encoded)
        self.assertNotIn(r"C:\private", encoded)
        delivery.validate_export(full)

    def test_dom_recruiters_unknown_sources_and_invalid_names_stay_out_of_company_field(self):
        rows = [self.legacy(self.rows[0], salary_source="dom_untrusted"),
                self.legacy(self.rows[1], salary_source=""),
                {**self.rows[2], "company": {"name": None, "full_name": 42}, "boss_name": "招聘者"}]
        self.ready(rows=rows)
        preview = delivery.preview_task(self.task)
        self.assertTrue(all(row["company"] is None for row in preview["jobs"]))
        self.assertNotIn("jobs.company", delivery.available_export_fields(preview))
        full = self.read(delivery.export_task(self.task))
        self.assertTrue(all("company" not in row for row in full["jobs"]))
        with self.assertRaises(delivery.EmptyExportError):
            delivery.export_task(self.task, self.root / "no-company-export", selected_fields=["jobs.company"])
        self.assertFalse((self.root / "no-company-export").exists())


class ExperienceDeliveryTests(DeliveryCase):
    def setUp(self):
        super().setUp()
        for row in self.rows:
            row["job_labels"] = ["本科"]

    def test_collected_explicit_and_legacy_experience_survive_all_four_stages_and_exports(self):
        rows = [{**self.rows[0], "experience": "3-5年", "tags": "1-3年 | 本科"},
                {**self.rows[1], "tags": "经验不限 | 本科"},
                {**self.rows[2], "tags": "在校/应届 | 本科"},
                {**self.rows[3], "tags": "本科 | 不限"}]
        path = self.source / "raw/current/shanghai/current__shanghai__kw__salary-a__exp.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"city": "上海", "filters": {"salary": "", "experience": "不限", "degree": "203"},
                                    "jobs": rows}, ensure_ascii=False), encoding="utf-8")
        raw_before = path.read_bytes()
        config = {"current": {"keywords": [{"id": "kw", "query": "AI 架构"}],
                              "experiences": [{"id": "exp", "query": "不限"}]}}
        collected, _ = collect_rows(self.source, config, city="上海", sample_strategy="balanced-dedup")
        expected = {"a": "3-5年", "b": "经验不限", "c": "在校/应届", "d": ""}
        self.assertEqual({row["job_id"]: row["experience"] for row in collected}, expected)
        self.task = ReviewTask.create(self.source, self.root / "experience-tasks", "招聘经验透传测试", city="上海")
        self.addCleanup(self.task._release_run)
        self.ready(rows=collected)
        for stage in REVIEW_STAGES:
            document = self.read(self.task.path / self.task.stage_info(stage)["result_file"])
            self.assertEqual({row["job_id"]: row["experience"] for row in document["jobs"]}, expected)
        before = {file: file.read_bytes() for file in self.task.path.rglob("*") if file.is_file()}
        preview = delivery.preview_task(self.task)
        self.assertIn("jobs.experience", delivery.available_export_fields(preview))
        full = self.read(delivery.export_task(self.task, self.root / "experience-exports"))
        delivery.validate_export(full)
        self.assertEqual({row["job_id"]: row.get("experience", "") for row in full["jobs"]}, expected)
        selected = delivery.export_task(self.task, self.root / "experience-exports", selected_fields=["jobs.experience"])
        self.assertEqual(selected.name, "经验要求.json")
        self.assertEqual(self.read(selected), {"jobs": [{"experience": "3-5年"}, {"experience": "经验不限"},
                                                       {"experience": "在校/应届"}]})
        self.assertEqual({file: file.read_bytes() for file in before}, before)
        self.assertEqual(path.read_bytes(), raw_before)

    def test_ready_and_blocked_jobs_expose_dynamic_experience_with_alias_and_old_tag_projection(self):
        rows = [{**self.rows[0], "job_experience": "5-10年", "tags": "1-3年 | 本科"},
                {**self.rows[1], "job_link": None, "experience_tags": ["1年以内", "本科"]},
                {**self.rows[2], "job_link": None, "jobExperience": "不限"}]
        self.ready(rows=rows)
        preview = delivery.preview_task(self.task)
        self.assertEqual(preview["summary"], {"selected_count": 3, "ready_count": 1, "blocked_count": 2})
        self.assertEqual([row["experience"] for row in preview["jobs"]], ["5-10年"])
        self.assertEqual([row["experience"] for row in preview["blocked_jobs"]], ["1年以内", "不限"])
        available = delivery.available_export_fields(preview)
        self.assertIn("jobs.experience", available)
        self.assertIn("blocked_jobs.experience", available)
        self.assertEqual(delivery.FIELD_LABELS["experience"], "经验要求")
        full = self.read(delivery.export_task(self.task))
        delivery.validate_export(full)
        self.assertEqual([row["experience"] for row in full["blocked_jobs"]], ["1年以内", "不限"])
        selected = delivery.export_task(self.task, selected_fields=["jobs.experience", "blocked_jobs.experience"])
        self.assertEqual(selected.name, "经验要求_经验要求.json")
        self.assertEqual(self.read(selected), {"jobs": [{"experience": "5-10年"}],
                                              "blocked_jobs": [{"experience": "1年以内"}, {"experience": "不限"}]})

    def test_old_task_recovery_is_readonly_and_keeps_failed_ai_manual_choices_and_keys(self):
        tags = ("1-3年 | 本科", "经验不限 | 本科", "在校/应届 | 本科", "1年以内 | 本科")
        rows = [{**row, "tags": tag} for row, tag in zip(self.rows, tags)]
        self.ready(rows=rows, judgments={"b": "failed", "d": "信息不足"}, manual=("b", "d"))
        self.task.set_keep("llm", ["c"], False)
        self.task.confirm("llm")
        before = {path: path.read_bytes() for path in self.task.path.rglob("*") if path.is_file()}
        manifest_before = self.read(self.task.path / "task_manifest.json")
        self.assertTrue(all("experience" not in row for row in self.task.all_jobs(through_stage="llm")))
        cache = delivery.ExportCache()
        with patch("llm_client.CodexResponsesClient", side_effect=AssertionError("model access")), \
             patch("llm_client.load_review_prompt", side_effect=AssertionError("prompt access")), \
             patch("project_llm_settings.load_settings", side_effect=AssertionError("settings access")), \
             patch("process_architecture.model_input", side_effect=AssertionError("AI input reconstruction")), \
             patch("review_cache.review_input_signature", side_effect=AssertionError("AI input signing")), \
             patch("review_cache.review_for_report", side_effect=AssertionError("AI revalidation")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network access")):
            reader = ReviewTask.open_readonly(self.task.path, preserve_selection=True)
            preview = delivery.preview_task(reader, cache=cache)
            self.assertEqual([row["job_id"] for row in preview["jobs"]], ["a", "b", "d"])
            self.assertEqual([row["experience"] for row in preview["jobs"]], ["1-3年", "经验不限", "1年以内"])
            self.assertEqual([row["selection"]["source"] for row in preview["jobs"]], ["ai", "manual", "manual"])
            self.assertEqual(preview["jobs"][1]["ai_review"]["status"], "failed")
            self.assertTrue(all(row["ai_review"]["validation"] == "not_checked" for row in preview["jobs"]))
            full = self.read(delivery.export_task(reader, self.root / "legacy-experience", cache=cache))
            delivery.validate_export(full)
            path = delivery.export_task(reader, self.root / "legacy-experience", cache=cache, selected_fields=["jobs.experience"])
            self.assertEqual(path.name, "经验要求.json")
            self.assertEqual(self.read(path), {"jobs": [{"experience": "1-3年"}, {"experience": "经验不限"}, {"experience": "1年以内"}]})
            with patch.object(delivery, "_current_rows", side_effect=AssertionError("unchanged preview cache was missed")):
                self.assertEqual(delivery.preview_task(reader, cache=cache), preview)
            refreshed = delivery.preview_task(ReviewTask.open_readonly(self.task.path, preserve_selection=True))
            self.assertEqual(refreshed, preview)
            self.assertEqual([row["job_key"] for row in full["jobs"]], [row["job_key"] for row in refreshed["jobs"]])
        self.assertEqual(self.read(self.task.path / "task_manifest.json"), manifest_before)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_conflicting_labels_invalid_codes_and_search_or_jd_evidence_stay_missing(self):
        rows = [{**self.rows[0], "tags": "本科 | 不限", "job_labels": ["学历不限"], "experience": "105",
                 "observed_in": [{"experience_id": "1_3", "experience_filter": "1-3年"}]},
                {**self.rows[1], "tags": "1-3年 | 3-5年 | 本科", "jobExperience": 105},
                {**self.rows[2], "job_link": None, "tags": "要求3-5年工作经验", "experience": {"name": "1-3年"}},
                {**self.rows[3], "job_link": None, "skills": ["3-5年"], "filters": {"experience": "经验不限"}}]
        self.ready(rows=rows)
        self.rewrite_llm(lambda jobs: [row.update(jd="任职要求：具备3-5年工作经验。") for row in jobs])
        preview = delivery.preview_task(self.task)
        available = delivery.available_export_fields(preview)
        self.assertNotIn("jobs.experience", available)
        self.assertNotIn("blocked_jobs.experience", available)
        self.assertTrue(all(row["experience"] is None for section in ("jobs", "blocked_jobs") for row in preview[section]))
        full = self.read(delivery.export_task(self.task))
        delivery.validate_export(full)
        self.assertTrue(all("experience" not in row for section in ("jobs", "blocked_jobs") for row in full[section]))
        with self.assertRaises(delivery.EmptyExportError):
            delivery.export_task(self.task, self.root / "unknown-experience", selected_fields=["jobs.experience", "blocked_jobs.experience"])
        self.assertFalse((self.root / "unknown-experience").exists())

    def test_explicit_text_is_redacted_and_unavailable_marker_blocks_recovery(self):
        rows = [{**self.rows[0], "jobExperience": r"3-5年 C:\private\experience.json Cookie=PRIVATE_EXP_SECRET"},
                {**self.rows[1], "experience": "", "experience_source": "unavailable", "tags": "1-3年 | 本科"}]
        self.ready(rows=rows)
        preview = delivery.preview_task(self.task)
        self.assertIn("[REDACTED]", preview["jobs"][0]["experience"])
        self.assertIn("[REDACTED PATH]", preview["jobs"][0]["experience"])
        self.assertIsNone(preview["jobs"][1]["experience"])
        full = self.read(delivery.export_task(self.task))
        text = json.dumps(full, ensure_ascii=False)
        self.assertNotIn("PRIVATE_EXP_SECRET", text)
        self.assertNotIn(r"C:\private", text)
        self.assertNotIn("experience_source", text)
        delivery.validate_export(full)

    def test_cli_can_export_only_recovered_experience_without_mutating_old_task(self):
        self.ready(rows=[{**self.rows[0], "tags": "1-3年 | 本科"}, {**self.rows[1], "jobExperience": "经验不限"}])
        before = {path: path.read_bytes() for path in self.task.path.rglob("*") if path.is_file()}
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "delivery_export.py"), "--task", str(self.task.path),
                                 "--output-dir", str(self.root / "cli-experience"), "--fields", "jobs.experience"],
                                capture_output=True, text=True, encoding="utf-8", timeout=30,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0, result.stderr)
        path = Path(result.stdout.strip())
        self.assertEqual(path.name, "经验要求.json")
        self.assertEqual(self.read(path), {"jobs": [{"experience": "1-3年"}, {"experience": "经验不限"}]})
        self.assertEqual({path: path.read_bytes() for path in before}, before)


class CompanyMetadataTests(DeliveryCase):
    fields = ("encrypt_brand_id", "company_link", "company_industry", "company_scale", "company_stage")

    @staticmethod
    def metadata(company_id="CompanyFixture~"):
        return {"encrypt_brand_id": company_id,
                "company_link": f"https://www.zhipin.com/gongsi/{company_id}.html",
                "company_industry": "企业服务 · 计算机软件", "company_scale": "100-499人",
                "company_stage": "B轮"}

    def test_full_and_selected_exports_restore_company_fields_for_ready_and_blocked_rows(self):
        rows = [{**self.rows[0], **self.metadata()},
                {**self.rows[1], **self.metadata("CompanyBlocked~~"), "job_link": None}]
        self.ready(rows=rows)
        before = {path: path.read_bytes() for path in self.task.path.rglob("*") if path.is_file()}
        reader = ReviewTask.open_readonly(self.task.path, preserve_selection=True)
        cache = delivery.ExportCache()
        preview = delivery.preview_task(reader, cache=cache)
        self.assertEqual(preview["summary"], {"selected_count": 2, "ready_count": 1, "blocked_count": 1})
        available = delivery.available_export_fields(preview, cache=cache)
        labels = ("公司 ID", "公司链接", "公司行业", "公司规模", "融资阶段")
        self.assertEqual(tuple(delivery.FIELD_LABELS[field] for field in self.fields), labels)
        for section, row in zip(("jobs", "blocked_jobs"), rows):
            self.assertEqual({field: preview[section][0][field] for field in self.fields},
                             {field: row[field] for field in self.fields})
            self.assertTrue(all(f"{section}.{field}" in available for field in self.fields))
            self.assertEqual(preview[section][0]["ai_review"]["validation"], "not_checked")
        parent = self.root / "company-exports"
        full = self.read(delivery.export_task(reader, parent, cache=cache))
        delivery.validate_export(full)
        for section, row in zip(("jobs", "blocked_jobs"), rows):
            self.assertEqual({field: full[section][0][field] for field in self.fields},
                             {field: row[field] for field in self.fields})
        selected = [f"{section}.{field}" for section in ("jobs", "blocked_jobs") for field in self.fields]
        path = delivery.export_task(reader, parent, cache=cache, selected_fields=reversed(selected))
        self.assertEqual(path.name, "公司ID_公司链接_公司行业.json")
        self.assertEqual(self.read(path), {section: [{field: row[field] for field in self.fields}]
                                          for section, row in zip(("jobs", "blocked_jobs"), rows)})
        self.assertEqual(delivery.preview_task(reader, cache=cache), preview)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_missing_blank_company_fields_stay_out_of_dynamic_catalog_and_saved_json(self):
        rows = [dict(self.rows[0]),
                {**self.rows[1], **dict.fromkeys(self.fields, "")},
                {**self.rows[2], **dict.fromkeys(self.fields, " \t"), "job_link": None}]
        self.ready(rows=rows)
        preview = delivery.preview_task(self.task)
        self.assertTrue(all(row[field] is None for section in ("jobs", "blocked_jobs")
                            for row in preview[section] for field in self.fields))
        available = delivery.available_export_fields(preview)
        self.assertFalse(any(f"{section}.{field}" in available
                             for section in ("jobs", "blocked_jobs") for field in self.fields))
        full = self.read(delivery.export_task(self.task))
        self.assertTrue(all(field not in row for section in ("jobs", "blocked_jobs")
                            for row in full[section] for field in self.fields))
        parent = self.root / "empty-company-fields"
        with self.assertRaises(delivery.EmptyExportError):
            delivery.export_task(self.task, parent, selected_fields=[f"jobs.{field}" for field in self.fields])
        self.assertFalse(parent.exists())

    def test_older_full_documents_without_company_fields_remain_schema_compatible(self):
        self.ready(rows=[self.rows[0], {**self.rows[1], "job_link": None}])
        old_document = self.document()
        for section in ("jobs", "blocked_jobs"):
            for row in old_document[section]:
                for field in self.fields:
                    row.pop(field)
                row["ai_review"]["validation"] = "input_match"
        delivery.validate_export(old_document)
        compact = delivery.compact_export_record(old_document)
        delivery.validate_export(compact)
        schema = delivery.ExportCache()._read(delivery.resource_path(delivery._SCHEMA_NAME))
        for definition in ("job", "blocked_job"):
            self.assertTrue(all(field not in schema["$defs"][definition]["required"] for field in self.fields))

    def test_company_identity_never_supplies_a_job_target_or_creates_duplicate_job_targets(self):
        shared = self.metadata("SharedCompany~~")
        rows = [{**self.rows[0], **self.metadata("Bossb"), "job_link": None},
                {**self.rows[1], **shared}, {**self.rows[2], **shared}]
        self.ready(rows=rows)
        full = self.read(delivery.export_task(self.task))
        self.assertEqual(full["summary"], {"selected_count": 3, "ready_count": 2, "blocked_count": 1})
        self.assertEqual([row["boss_job_id"] for row in full["jobs"]], ["Bossb", "Bossc"])
        blocked = full["blocked_jobs"][0]
        self.assertEqual(blocked["blocked_reasons"], ["missing_boss_identifier"])
        self.assertNotIn("boss_job_id", blocked)
        self.assertNotIn("job_url", blocked)
        self.assertEqual(blocked["encrypt_brand_id"], "Bossb")
        self.assertEqual(blocked["company_link"], self.metadata("Bossb")["company_link"])

    def test_unsafe_company_values_are_omitted_or_redacted_without_blocking_the_job(self):
        unsafe_links = [
            "javascript:alert(1)", "data:text/html,unsafe", "file:///C:/private/company.json",
            r"C:\private\company.json", "https://example.test/gongsi/Company~.html",
            "https://www.zhipin.com.attacker.test/gongsi/Company~.html",
            "https://user:PRIVATE_LINK_SECRET@www.zhipin.com/gongsi/Company~.html",
            "https://www.zhipin.com/gongsi/Company~.html?cookie=PRIVATE_COOKIE_SECRET",
            "https://www.zhipin.com/gongsi/Company~.html?redirect=https://example.test",
            "https://www.zhipin.com/gongsi/Company~.html#PRIVATE_FRAGMENT_SECRET",
            "https://www.zhipin.com:444/gongsi/Company~.html",
            "https://www.zhipin.com/job_detail/Company~.html",
            "https://www.zhipin.com/gongsi/../job_detail/Company~.html",
            "https://www.zhipin.com/gongsi/Company%2fOther.html",
            "//www.zhipin.com/gongsi/Company~.html",
            "https://www.zhipin.com/gongsi/Company~.html\n", 42,
        ]
        rows = [{**self.rows[0], "job_id": f"company-safety-{index}",
                 "job_link": f"https://www.zhipin.com/job_detail/CompanySafety{index}.html",
                 "encrypt_brand_id": "Cookie=PRIVATE_ID_SECRET", "company_link": link,
                 "company_industry": "软件 · Cookie=PRIVATE_INDUSTRY_SECRET",
                 "company_scale": r"规模 · C:\private\company.json",
                 "company_stage": "融资 · /tmp/private/company.json", "cookies": "PRIVATE_UNLISTED_SECRET"}
                for index, link in enumerate(unsafe_links)]
        self.ready(rows=rows)
        full = self.read(delivery.export_task(self.task))
        self.assertEqual(full["summary"], {"selected_count": len(rows), "ready_count": len(rows), "blocked_count": 0})
        for row in full["jobs"]:
            self.assertNotIn("encrypt_brand_id", row)
            self.assertNotIn("company_link", row)
            self.assertNotIn("cookies", row)
            self.assertIn("[REDACTED]", row["company_industry"])
            self.assertIn("[REDACTED PATH]", row["company_scale"])
            self.assertIn("[REDACTED PATH]", row["company_stage"])
        text = json.dumps(full, ensure_ascii=False)
        for secret in ("PRIVATE_", "C:\\private", "/tmp/private"):
            self.assertNotIn(secret, text)

    def test_company_url_and_identifier_contract_is_enforced_for_ready_and_blocked_documents(self):
        self.ready(rows=[{**self.rows[0], **self.metadata()},
                         {**self.rows[1], **self.metadata("BlockedCompany~~"), "job_link": None}])
        document = self.document()
        for section in ("jobs", "blocked_jobs"):
            for field, value in (
                ("company_link", "javascript:alert(1)"),
                ("company_link", "https://www.zhipin.com/gongsi/Company~.html?cookie=private"),
                ("company_link", "https://example.test/gongsi/Company~.html"),
                ("encrypt_brand_id", "Cookie=private"),
                ("encrypt_brand_id", "CompanyFixture~\n"),
                ("company_industry", {"cookie": "private"}),
            ):
                with self.subTest(section=section, field=field, value=value):
                    wrong = copy.deepcopy(document)
                    wrong[section][0][field] = value
                    with self.assertRaises(delivery.DeliveryExportError):
                        delivery.validate_export(wrong)
        delivery.validate_export(document)


class IdentifierTests(DeliveryCase):
    def test_merged_source_target_conflict_is_blocked_without_leaking_candidates(self):
        first = {**self.rows[0], "job_link": "https://www.zhipin.com/job_detail/FirstTarget.html"}
        second = {**self.rows[0], "job_link": "https://www.zhipin.com/job_detail/SecondTarget.html"}
        merged, _ = merge_rows([first, second])
        self.assertEqual(len(merged), 1)
        # Re-importing a merged checkpoint must retain both original targets.
        merged, _ = merge_rows(merged)
        self.ready(rows=merged)
        document = self.read(delivery.export_task(self.task))
        self.assertEqual(document["summary"], {"selected_count": 1, "ready_count": 0, "blocked_count": 1})
        self.assertIn("conflicting_identifiers", document["blocked_jobs"][0]["blocked_reasons"])
        self.assertNotIn("delivery_identity_candidates", json.dumps(document, ensure_ascii=False))
        delivery.validate_export(document)

    def test_all_preserved_identity_aliases_participate_in_conflict_checks(self):
        for field in ("boss_job_id", "encrypt_job_id", "encryptJobId", "job_link", "job_url", "link", "url"):
            with self.subTest(field=field):
                other = "OtherTarget" if field in {"boss_job_id", "encrypt_job_id", "encryptJobId"} else "https://www.zhipin.com/job_detail/OtherTarget.html"
                row = {**self.rows[0], "delivery_identity_candidates": [{field: other}]}
                self.assertIn("conflicting_identifiers", delivery._identifiers([row])[3])

    def test_matching_source_candidates_do_not_block_and_stay_internal(self):
        merged, _ = merge_rows([self.rows[0], {**self.rows[0], "encryptJobId": "Bossa"}])
        self.ready(rows=merged)
        document = self.read(delivery.export_task(self.task))
        self.assertEqual(document["summary"]["ready_count"], 1)
        self.assertNotIn("delivery_identity_candidates", document["jobs"][0])

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
        self.assertNotIn("from", item["navigation"])
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
    def test_preview_and_full_or_selected_export_do_not_read_prompts_or_invoke_ai_validation(self):
        self.ready(rows=self.rows[:1])
        self.rewrite_llm(lambda rows: rows[0]["llm_review"].update(cache_input_signature="not-current"))
        original_read_text = Path.read_text
        prompt_reads = []

        def read_without_prompts(path, *args, **kwargs):
            if path.suffix.lower() == ".md":
                prompt_reads.append(path)
                raise AssertionError("prompt body access")
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", new=read_without_prompts), \
             patch("llm_client.load_codex_config", side_effect=AssertionError("provider access")), \
             patch("llm_client.load_review_prompt", side_effect=AssertionError("prompt access")), \
             patch("llm_client.CodexResponsesClient", side_effect=AssertionError("model access")), \
             patch("project_llm_settings.load_settings", side_effect=AssertionError("settings access")), \
             patch("process_architecture.model_input", side_effect=AssertionError("AI input reconstruction")), \
             patch("review_cache.review_for_report", side_effect=AssertionError("AI verification")), \
             patch("review_cache.review_input_signature", side_effect=AssertionError("AI signing")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network access")):
            preview = delivery.preview_task(self.task)
            full = self.read(delivery.export_task(self.task))
            delivery.validate_export(full)
            subset = self.read(delivery.export_task(self.task, selected_fields=[
                "jobs.ai_review.status", "jobs.ai_review.validation", "jobs.selection.source",
            ]))
        self.assertEqual(prompt_reads, [])
        self.assertEqual(preview["summary"], {"selected_count": 1, "ready_count": 1, "blocked_count": 0})
        self.assertEqual(full["jobs"][0]["ai_review"]["validation"], "not_checked")
        self.assertEqual(subset, {"jobs": [{"ai_review": {"status": "success", "validation": "not_checked"},
                                           "selection": {"source": "ai"}}]})

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


class AvailableFieldTests(unittest.TestCase):
    def test_catalog_uses_nonempty_value_union_for_each_array_and_stays_in_schema_order(self):
        preview = {
            "source_task": {"task_id": "fixture", "name": " \t ", "city": None,
                            "review_stages": {"import": 0, "shortlist": None, "fetch-jd": 3, "llm": 0}},
            "summary": {"selected_count": 0, "ready_count": 0, "blocked_count": 0},
            "jobs": [
                {"title": " First ", "company": None, "city": "\n ", "skills": [None, " ", [], {}],
                 "job_labels": [], "navigation": {"ka": "page-a", "security_id": None},
                 "selection": {"confirmed": False, "keep": True, "manual_keep": None},
                 "ai_review": {"reason": None, "validation": "not_checked"}},
                {"title": None, "company": "Company", "skills": ["RAG"], "job_labels": [" \t "],
                 "navigation": {"security_id": "detail"}, "selection": {"manual_keep": False},
                 "ai_review": {"reason": "rationale"}, "provider_config": {"api_key": "PRIVATE"}},
            ],
            "blocked_jobs": [{"title": "blocked", "salary": None, "skills": [],
                              "ai_review": {"reason": ""}, "blocked_reasons": ["missing_boss_identifier"]}],
            "snapshot_token": "internal", "raw_payload": {"private": True},
        }
        before = copy.deepcopy(preview)
        self.assertEqual(delivery.available_export_fields(preview), (
            "schema_version", "export_id", "generated_at", "source_task.task_id",
            "source_task.review_stages.import", "source_task.review_stages.fetch-jd",
            "source_task.review_stages.llm", "summary.selected_count", "summary.ready_count",
            "summary.blocked_count", "jobs.title", "jobs.company", "jobs.skills", "jobs.ai_review.reason",
            "jobs.ai_review.validation", "jobs.selection.confirmed", "jobs.selection.keep",
            "jobs.selection.manual_keep", "jobs.navigation.ka", "jobs.navigation.security_id",
            "blocked_jobs.title", "blocked_jobs.blocked_reasons",
        ))
        self.assertEqual(preview, before)

    def test_empty_content_exposes_only_legitimate_generated_metadata_and_no_empty_groups(self):
        for preview in ({}, {"source_task": {}, "summary": {}, "jobs": [], "blocked_jobs": []},
                        {"source_task": {"name": "\n "}, "jobs": [{"title": None, "skills": [None, " "]}],
                         "blocked_jobs": [{"navigation": {"ka": ""}}]}):
            with self.subTest(preview=preview):
                self.assertEqual(delivery.available_export_fields(preview),
                                 ("schema_version", "export_id", "generated_at"))
        for preview in (None, [], "preview"):
            with self.subTest(preview=preview), self.assertRaises(delivery.DeliveryExportError):
                delivery.available_export_fields(preview)

    def test_scalar_array_is_one_leaf_and_zero_false_are_values(self):
        fields = delivery.available_export_fields({
            "summary": {"ready_count": 0},
            "jobs": [{"skills": [None, " ", {}], "job_labels": [None, False, 0],
                      "selection": {"manual_keep": False}, "navigation": {}}],
            "blocked_jobs": [],
        })
        self.assertEqual(fields, ("schema_version", "export_id", "generated_at", "summary.ready_count",
                                  "jobs.job_labels", "jobs.selection.manual_keep"))

    def test_catalog_never_copies_jd_bodies_or_repeatedly_scans_an_already_populated_leaf(self):
        class TrackedText(str):
            def __init__(self, value):
                self.scans = 0

            def strip(self, *args, **kwargs):
                self.scans += 1
                return super().strip(*args, **kwargs)

            def __deepcopy__(self, memo):
                raise AssertionError("available fields must not copy JD text")

        body = TrackedText("岗位职责" * 100_000)
        private = TrackedText("unknown source data")
        preview = {"jobs": [{"jd": body, "provider_payload": private} for _ in range(500)]}
        self.assertEqual(delivery.available_export_fields(preview),
                         ("schema_version", "export_id", "generated_at", "jobs.jd"))
        self.assertEqual(body.scans, 1)
        self.assertEqual(private.scans, 0)

    def test_preview_filename_skips_absent_paths_without_changing_static_filename_api(self):
        preview = {"jobs": [{"title": "Title", "salary": "25K", "city": None}]}
        requested = ["jobs.city", "jobs.title", "jobs.company", "jobs.salary", "blocked_jobs.title"]
        self.assertEqual(delivery.export_filename(requested, preview=preview), "岗位名称_薪资.json")
        self.assertEqual(delivery.export_filename(requested), "岗位名称_公司_城市.json")
        self.assertEqual(delivery.export_filename(preview=preview), "格式版本_导出ID_导出时间.json")
        with self.assertRaises(delivery.EmptyExportError):
            delivery.export_filename(["jobs.city"], preview=preview)
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.export_filename(["jobs.title", "jobs.cookies"], preview=preview)


class FilenameTests(unittest.TestCase):
    def test_names_use_the_first_three_selected_chinese_labels_in_schema_order(self):
        cases = (
            (["jobs.title"], "岗位名称.json"),
            (["jobs.salary", "jobs.title"], "岗位名称_薪资.json"),
            (["jobs.salary", "jobs.company", "jobs.title"], "岗位名称_公司_薪资.json"),
            (["jobs.education", "jobs.salary", "jobs.company", "jobs.title"], "岗位名称_公司_薪资.json"),
            (["jobs.title", "jobs.company", "jobs.title", "jobs.salary"], "岗位名称_公司_薪资.json"),
            (["blocked_jobs.title", "jobs.title"], "岗位名称_岗位名称.json"),
            (["jobs.navigation.security_id", "jobs.ai_review.reason", "source_task.review_stages.llm"],
             "AI语义复核确认版本_理由_详情页定位值.json"),
            (None, "格式版本_导出ID_导出时间.json"),
        )
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertEqual(delivery.export_filename(fields), expected)
                self.assertTrue(delivery.is_export_filename(expected))

    def test_filename_validation_rejects_invalid_field_selections(self):
        for fields in ([], "jobs.title", 4, [None], ["jobs"], ["jobs.title", "unknown"], ["jobs.0.title"]):
            with self.subTest(fields=fields), self.assertRaises(delivery.DeliveryExportError):
                delivery.export_filename(fields)

    def test_pure_filename_builder_does_not_read_schema_or_files(self):
        with patch.object(delivery, "resource_path", side_effect=AssertionError("resource I/O")), \
             patch.object(delivery, "export_field_paths", side_effect=AssertionError("schema I/O")):
            self.assertEqual(delivery.field_filename(iter(["jobs.title", "jobs.company", "jobs.salary"])),
                             "岗位名称_公司_薪资.json")
            self.assertEqual(delivery.field_filename(["export_id"]), "导出ID.json")

    def test_generated_labels_are_sanitized_and_filename_length_is_bounded(self):
        labels = {"title": '  岗位:/\\<>*?"|\t名称  ', "company": "CON", "salary": "薪资" * 100}
        with patch.dict(delivery.FIELD_LABELS, labels):
            name = delivery.field_filename(["jobs.title", "jobs.company", "jobs.salary"])
        self.assertTrue(delivery.is_export_filename(name))
        self.assertLessEqual(len(name), 120)
        self.assertNotRegex(name, r'[<>:"/\\|?*\s\x00-\x1f]')
        self.assertTrue(name.startswith("岗位"))
        self.assertIn("字段CON", name)

    def test_filename_safety_accepts_legacy_and_rejects_paths_temporary_files_and_reserved_names(self):
        self.assertTrue(delivery.is_export_filename("delivery_jobs.json"))
        for name in (None, 0, "", ".json", ".岗位名称.json", ".delivery_jobs.json.tmp", "岗位名称.json.tmp",
                     "岗位名称.txt", "岗位名称.JSON", "../岗位名称.json", "folder/岗位名称.json",
                     "folder\\岗位名称.json", "C:\\岗位名称.json", "岗位:名称.json", "岗位?名称.json",
                     "岗位\0名称.json", "岗位\n名称.json", "岗位 名称.json", "岗位名称 .json", "岗位名称..json",
                     "CON.json", "nul.json", "LPT1.json", "COM².json", "x" * 121 + ".json"):
            with self.subTest(name=name):
                self.assertFalse(delivery.is_export_filename(name))


class EmptyValueTests(unittest.TestCase):
    def test_recursive_cleanup_keeps_zero_false_and_nonblank_text_without_mutating_values(self):
        value = {
            "null": None, "blank": "\t \n", "empty_list": [], "empty_dict": {},
            "zero": 0, "false": False, "text": "  原文不裁剪  ",
            "list": [None, "", " \t", [], {}, {"empty": None}, {"zero": 0}, False, 0, " 文本 ",
                     [None, {"empty": []}, "子项"]],
            "nested": {"empty": {"values": [None, " "]}, "keep": {"false": False, "zero": 0}},
        }
        before = copy.deepcopy(value)
        self.assertEqual(delivery._prune_empty(value), {
            "zero": 0, "false": False, "text": "  原文不裁剪  ",
            "list": [{"zero": 0}, False, 0, " 文本 ", ["子项"]],
            "nested": {"keep": {"false": False, "zero": 0}},
        })
        self.assertEqual(value, before)

    def test_compact_export_record_is_public_nonmutating_and_preserves_false_zero(self):
        record = {"title": "Title", "city": None, "meta": {"blank": " ", "zero": 0},
                  "items": [None, "", False, 0, {"empty": None}, {"text": "value"}]}
        before = copy.deepcopy(record)
        compact = delivery.compact_export_record(record)
        self.assertEqual(compact, {"title": "Title", "meta": {"zero": 0},
                                   "items": [False, 0, {"text": "value"}]})
        compact["meta"]["zero"] = 7
        compact["items"][-1]["text"] = "caller changed"
        self.assertEqual(record, before)
        self.assertEqual(delivery.compact_export_record({"navigation": {"ka": None}, "empty": []}), {})
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.compact_export_record([])


class FieldProjectionTests(DeliveryCase):
    def test_field_catalog_lists_every_leaf_in_schema_order_without_groups_or_indices(self):
        job_fields = (
            "job_key", "job_id", "boss_job_id", "job_url", "title", "company", "encrypt_brand_id",
            "company_link", "company_industry", "company_scale", "company_stage", "city", "location",
            "district", "business_district", "address", "salary", "experience", "education", "skills",
            "job_labels", "jd", "ai_review.format", "ai_review.status", "ai_review.judgment",
            "ai_review.reason", "ai_review.validation", "selection.stage", "selection.confirmed",
            "selection.keep", "selection.source", "selection.manual_keep", "navigation.original_job_url",
            "navigation.lid", "navigation.ka", "navigation.from", "navigation.security_id",
            "navigation.encrypt_boss_id",
        )
        expected = (
            "schema_version", "export_id", "generated_at", "source_task.task_id", "source_task.name",
            "source_task.city", "source_task.track", "source_task.created_at", "source_task.review_revision",
            "source_task.review_stages.import", "source_task.review_stages.shortlist",
            "source_task.review_stages.fetch-jd", "source_task.review_stages.llm", "summary.selected_count",
            "summary.ready_count", "summary.blocked_count",
            *("jobs." + field for field in job_fields),
            *("blocked_jobs." + field for field in job_fields), "blocked_jobs.blocked_reasons",
        )
        cache = delivery.ExportCache()
        schema = copy.deepcopy(cache._read(delivery.resource_path(delivery._SCHEMA_NAME)))
        self.assertEqual(delivery.export_field_paths(cache=cache), expected)
        self.assertEqual(cache._read(delivery.resource_path(delivery._SCHEMA_NAME)), schema)
        self.assertEqual(len(expected), len(set(expected)))

    def test_single_title_field_includes_only_its_ancestor_array_and_keeps_row_order(self):
        self.ready(rows=[self.rows[2], self.rows[0], self.rows[1]])
        path = delivery.export_task(self.task, selected_fields=(field for field in ["jobs.title"]))
        self.assertEqual(path.name, "岗位名称.json")
        self.assertEqual(self.read(path), {"jobs": [{"title": row["title"]}
                                                  for row in [self.rows[2], self.rows[0], self.rows[1]]]})
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_root_only_selection_does_not_inject_metadata_or_jobs(self):
        self.ready(rows=self.rows[:1])
        path = delivery.export_task(self.task, selected_fields=["schema_version"])
        self.assertEqual(self.read(path), {"schema_version": "1.0"})

    def test_nested_objects_ready_and_blocked_arrays_have_independent_field_selections(self):
        rows = copy.deepcopy(self.rows)
        for index in (1, 3):
            rows[index].pop("job_link")
        self.ready(rows=rows)
        preview = delivery.preview_task(self.task)
        fields = ["blocked_jobs.blocked_reasons", "source_task.review_stages.llm", "jobs.ai_review.reason",
                  "jobs.title", "source_task.review_stages.import", "jobs.selection.confirmed",
                  "blocked_jobs.job_id", "summary.ready_count"]
        path = delivery.export_task(self.task, selected_fields=fields)
        self.assertEqual(self.read(path), {
            "source_task": {"review_stages": {stage: preview["source_task"]["review_stages"][stage]
                                               for stage in ("import", "llm")}},
            "summary": {"ready_count": 2},
            "jobs": [{"title": row["title"], "ai_review": {"reason": row["ai_review"]["reason"]},
                      "selection": {"confirmed": True}} for row in preview["jobs"]],
            "blocked_jobs": [{"job_id": row["job_id"], "blocked_reasons": ["missing_boss_identifier"]}
                             for row in preview["blocked_jobs"]],
        })
        self.assertEqual([row["job_id"] for row in self.read(path)["blocked_jobs"]], ["b", "d"])

    def test_nulls_and_empty_ancestors_are_removed_and_nonempty_scalar_arrays_kept(self):
        self.ready(rows=self.rows[:1])
        path = delivery.export_task(self.task, selected_fields=[
            "jobs.city", "jobs.skills", "jobs.job_labels", "jobs.selection.manual_keep", "jobs.navigation.ka",
        ])
        self.assertEqual(self.read(path), {"jobs": [{
            "skills": ["RAG", "Agent"], "job_labels": ["本科", "3-5年"],
        }]})
        self.assertEqual(path.name, "技能_岗位标签.json")

    def test_current_records_control_field_options_content_and_filename_without_placeholders(self):
        rows = copy.deepcopy(self.rows[:3])
        rows[0].update(title=None, skills=None, salary=None)
        rows[1]["city"] = "杭州"
        rows[2].pop("job_link")
        rows[2]["skills"] = None
        self.ready(rows=rows)
        cache = delivery.ExportCache()
        preview = delivery.preview_task(self.task, cache=cache)
        fields = delivery.available_export_fields(preview, cache=cache)
        for field in ("jobs.title", "jobs.city", "jobs.skills", "blocked_jobs.title", "jobs.ai_review.validation"):
            self.assertIn(field, fields)
        for field in ("jobs.education", "jobs.navigation.ka", "blocked_jobs.city", "blocked_jobs.skills"):
            self.assertNotIn(field, fields)
        self.assertIn("jobs.education", delivery.export_field_paths(cache=cache))
        selected = ["blocked_jobs.skills", "jobs.skills", "jobs.city", "jobs.education", "jobs.title",
                    "blocked_jobs.title"]
        path = delivery.export_task(self.task, cache=cache, selected_fields=selected)
        self.assertEqual(path.name, "岗位名称_城市_技能.json")
        self.assertEqual(self.read(path), {
            "jobs": [{"title": rows[1]["title"], "city": "杭州", "skills": rows[1]["skills"]}],
            "blocked_jobs": [{"title": rows[2]["title"]}],
        })
        self.assertEqual(delivery.preview_task(self.task, cache=cache), preview)
        full = self.read(delivery.export_task(self.task, cache=cache))
        self.assertEqual([item["job_id"] for item in full["jobs"]], ["a", "b"])
        self.assertEqual([item["job_id"] for item in full["blocked_jobs"]], ["c"])
        self.assertEqual(full["summary"], {"selected_count": 3, "ready_count": 2, "blocked_count": 1})

    def test_selecting_only_empty_ready_and_blocked_arrays_publishes_nothing(self):
        self.ready(rows=[], prompt=False)
        parent = self.root / "empty-lists"
        with self.assertRaises(delivery.EmptyExportError) as caught:
            delivery.export_task(self.task, parent, selected_fields=["jobs.title", "blocked_jobs.ai_review.reason"])
        self.assertIn("所选字段当前均无值", str(caught.exception))
        self.assertFalse(parent.exists())

    def test_selecting_only_empty_scalar_array_fails_without_mutating_source(self):
        self.ready(rows=self.rows[:1])
        preview = delivery.preview_task(self.task)
        preview["jobs"][0]["skills"] = []
        before = copy.deepcopy(preview)
        with patch.object(delivery, "preview_task", return_value=preview):
            with self.assertRaises(delivery.EmptyExportError):
                delivery.export_task(self.task, selected_fields=["jobs.skills"])
        self.assertFalse((self.task.path / "delivery-exports").exists())
        self.assertEqual(preview, before)

    def test_selecting_only_null_fields_fails_without_creating_output(self):
        self.ready(rows=self.rows[:1])
        parent = self.root / "empty-null-values"
        with self.assertRaises(delivery.EmptyExportError):
            delivery.export_task(self.task, parent,
                                 selected_fields=["jobs.city", "jobs.selection.manual_keep", "jobs.navigation.ka"])
        self.assertFalse(parent.exists())

    def test_empty_array_items_are_removed_without_placeholders_and_keep_remaining_order(self):
        rows = copy.deepcopy(self.rows)
        rows[0]["title"], rows[2]["title"] = None, " \t "
        self.ready(rows=rows)
        path = delivery.export_task(self.task, selected_fields=["jobs.title"])
        self.assertEqual(self.read(path), {"jobs": [{"title": rows[index]["title"]} for index in (1, 3)]})

    def test_selected_zero_summary_is_exportable_with_empty_arrays_removed(self):
        self.ready(rows=[], prompt=False)
        path = delivery.export_task(self.task, selected_fields=["summary.ready_count", "jobs.title"])
        self.assertEqual(path.name, "可导出数.json")
        self.assertEqual(self.read(path), {"summary": {"ready_count": 0}})

    def test_selecting_all_fields_reproduces_the_full_validated_document(self):
        self.ready(rows=self.rows[:2])
        validated = []
        real_validate = delivery.validate_export

        def capture(document, **kwargs):
            real_validate(document, **kwargs)
            validated.append(copy.deepcopy(document))

        with patch.object(delivery, "validate_export", side_effect=capture):
            path = delivery.export_task(self.task, selected_fields=delivery.export_field_paths())
        self.assertEqual(len(validated), 2)
        self.assertEqual(validated[0], validated[1])
        compact = self.read(path)
        delivery.validate_export(compact)
        self.assertNotIn("blocked_jobs", compact)
        self.assertNotIn("city", compact["jobs"][0])
        self.assertNotIn("manual_keep", compact["jobs"][0]["selection"])
        full = self.read(delivery.export_task(self.task))
        for document in (compact, full):
            document.pop("export_id")
            document.pop("generated_at")
        self.assertEqual(compact, full)

    def test_invalid_empty_group_and_index_paths_fail_before_any_output_is_created(self):
        self.ready(rows=self.rows[:1])
        parent = self.root / "invalid-fields"
        invalid = [[], (), iter(()), "", "jobs.title", b"jobs.title", 1, True,
                   [None], [1], [["jobs.title"]], ["jobs"], ["summary"], ["source_task"],
                   ["source_task.review_stages"], ["jobs.ai_review"], ["jobs.skills.0"],
                   ["jobs.0.title"], ["blocked_jobs.navigation"], ["unknown"],
                   ["jobs.title", "jobs.cookies"], ["api_key=PRIVATE_FIELD_SECRET"]]
        for fields in invalid:
            with self.subTest(fields=fields):
                with self.assertRaises(delivery.DeliveryExportError) as caught:
                    delivery.export_task(self.task, parent, selected_fields=fields)
                self.assertNotIn("PRIVATE_FIELD_SECRET", str(caught.exception))
                self.assertFalse(parent.exists())

    def test_duplicate_paths_are_harmless_and_do_not_duplicate_values(self):
        self.ready(rows=self.rows[:1])
        path = delivery.export_task(self.task, selected_fields=["jobs.title", "jobs.title"])
        self.assertEqual(self.read(path), {"jobs": [{"title": self.rows[0]["title"]}]})

    def test_omitted_full_document_invariants_are_still_validated(self):
        self.ready(rows=self.rows[:1])
        original = delivery.preview_task(self.task)
        variants = []
        wrong = copy.deepcopy(original)
        wrong["jobs"][0].update(boss_job_id=None, job_url=None)
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        wrong["summary"]["selected_count"] = 2
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        wrong["jobs"][0]["selection"]["manual_keep"] = True
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        wrong["jobs"][0]["selection"]["confirmed"] = False
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        wrong["jobs"][0]["cookies"] = "private"
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        del wrong["source_task"]["review_revision"]
        variants.append(wrong)
        for preview in variants:
            with self.subTest(preview=preview), patch.object(delivery, "preview_task", return_value=preview):
                with self.assertRaises(delivery.DeliveryExportError):
                    delivery.export_task(self.task, selected_fields=["jobs.title"])
                self.assertFalse((self.task.path / "delivery-exports").exists())

    def test_omitting_snapshot_metadata_does_not_allow_an_old_preview(self):
        self.ready(rows=self.rows[:1])
        preview = delivery.preview_task(self.task)
        self.task.set_keep("llm", ["a"], True)
        self.task.confirm("llm")
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.export_task(self.task, expected_snapshot=preview["snapshot_token"],
                                 selected_fields=["jobs.title"])
        self.assertFalse((self.task.path / "delivery-exports").exists())

    def test_subset_concurrent_selection_edit_rejects_and_cleans_output(self):
        self.ready(rows=self.rows[:1])
        other = ReviewTask.open(self.task.path)
        parent = self.root / "subset-race"
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
                delivery.export_task(self.task, parent, selected_fields=["jobs.title"])
        self.assertEqual(validations, 2)
        self.assertEqual(list(parent.iterdir()), [])

    def test_readback_checks_exact_projection_including_boolean_types_and_unselected_fields(self):
        self.ready(rows=self.rows[:1])
        real_read = delivery._read_json
        parent = self.root / "subset-readback"
        for modification in ("value", "extra", "type"):
            def tamper(path):
                document = real_read(path)
                if Path(path).name == ".delivery_jobs.json.tmp":
                    if modification == "value":
                        document["jobs"][0]["selection"]["confirmed"] = False
                    elif modification == "type":
                        document["jobs"][0]["selection"]["confirmed"] = 1
                    else:
                        document["schema_version"] = "1.0"
                return document

            with self.subTest(modification=modification), patch.object(delivery, "_read_json", side_effect=tamper):
                with self.assertRaises(delivery.DeliveryExportError):
                    delivery.export_task(self.task, parent, selected_fields=["jobs.selection.confirmed"])
                self.assertEqual(list(parent.iterdir()), [])

    def test_subset_preserves_cached_preview_and_task_files(self):
        self.ready(rows=self.rows[:2])
        cache = delivery.ExportCache()
        before = delivery.preview_task(self.task, cache=cache)
        available = delivery.available_export_fields(before, cache=cache)
        self.assertNotIn("jobs.city", available)
        self.assertFalse(any(field.startswith("blocked_jobs.") for field in available))
        files = {path.relative_to(self.task.path): path.read_bytes()
                 for path in self.task.path.rglob("*") if path.is_file()}
        parent = self.root / "outside-task"
        path = delivery.export_task(self.task, parent, cache=cache,
                                    selected_fields=["jobs.skills", "jobs.ai_review.reason"])
        self.assertEqual(len(self.read(path)["jobs"]), 2)
        self.assertEqual(delivery.preview_task(self.task, cache=cache), before)
        self.assertEqual({path.relative_to(self.task.path): path.read_bytes()
                          for path in self.task.path.rglob("*") if path.is_file()}, files)

    def test_subset_publication_failure_preserves_existing_export_and_cleans_temporary_files(self):
        self.ready(rows=self.rows[:1])
        first = delivery.export_task(self.task, selected_fields=["jobs.title"])
        before = first.read_bytes()
        with patch.object(delivery, "_publish", side_effect=OSError("PRIVATE_PUBLICATION_SECRET")):
            with self.assertRaises(delivery.DeliveryExportError) as caught:
                delivery.export_task(self.task, selected_fields=["jobs.title"])
        self.assertNotIn("PRIVATE_PUBLICATION_SECRET", str(caught.exception))
        self.assertEqual(first.read_bytes(), before)
        self.assertEqual(list(first.parent.parent.iterdir()), [first.parent])
        second = delivery.export_task(self.task, selected_fields=["jobs.title"])
        self.assertNotEqual(first.parent, second.parent)
        self.assertEqual(second.read_bytes(), before)


class CompactExportTests(DeliveryCase):
    def test_full_export_omits_null_fields_and_empty_navigation_but_dense_preview_is_unchanged(self):
        row = {**self.rows[0], "job_link": None, "encrypt_job_id": "DirectTarget", "skills": None,
               "job_labels": []}
        self.ready(rows=[row])
        cache = delivery.ExportCache()
        preview = delivery.preview_task(self.task, cache=cache)
        dense = self.document(preview)
        delivery.validate_export(dense)
        self.assertIsNone(preview["jobs"][0]["job_url"])
        self.assertTrue(all(value is None for value in preview["jobs"][0]["navigation"].values()))
        path = delivery.export_task(self.task, cache=cache)
        compact = self.read(path)
        delivery.validate_export(compact)
        self.assertEqual(path.name, "格式版本_导出ID_导出时间.json")
        self.assertNotIn("blocked_jobs", compact)
        for name in ("navigation", "job_url", "skills", "job_labels", "city", "district", "address"):
            self.assertNotIn(name, compact["jobs"][0])
        self.assertEqual(compact["jobs"][0]["boss_job_id"], "DirectTarget")
        self.assertEqual(delivery.preview_task(self.task, cache=cache), preview)
        self.assertIsNone(dense["jobs"][0]["job_url"])
        delivery.validate_export(dense)

    def test_compact_validation_keeps_required_identity_review_summary_and_target_constraints(self):
        self.ready(rows=self.rows[:1])
        compact = self.read(delivery.export_task(self.task))
        required_paths = [
            ("schema_version",), ("export_id",), ("generated_at",), ("source_task",), ("summary",),
            ("source_task", "task_id"), ("source_task", "name"), ("source_task", "review_revision"),
            ("source_task", "review_stages", "llm"), ("summary", "selected_count"),
            ("summary", "ready_count"), ("summary", "blocked_count"), ("jobs", 0, "job_key"),
            ("jobs", 0, "job_id"), ("jobs", 0, "ai_review"), ("jobs", 0, "ai_review", "status"),
            ("jobs", 0, "ai_review", "validation"),
            ("jobs", 0, "selection"), ("jobs", 0, "selection", "stage"),
            ("jobs", 0, "selection", "confirmed"), ("jobs", 0, "selection", "keep"),
            ("jobs", 0, "selection", "source"),
        ]
        for path in required_paths:
            document = copy.deepcopy(compact)
            container = document
            for part in path[:-1]:
                container = container[part]
            del container[path[-1]]
            with self.subTest(path=path), self.assertRaises(delivery.DeliveryExportError):
                delivery.validate_export(document)
        missing_target = copy.deepcopy(compact)
        missing_target["jobs"][0].pop("boss_job_id")
        missing_target["jobs"][0].pop("job_url")
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.validate_export(missing_target)
        wrong_count = copy.deepcopy(compact)
        wrong_count.pop("jobs")
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.validate_export(wrong_count)
        wrong_source = copy.deepcopy(compact)
        wrong_source["jobs"][0]["selection"]["source"] = "manual"
        with self.assertRaises(delivery.DeliveryExportError):
            delivery.validate_export(wrong_source)
        without_judgment = copy.deepcopy(compact)
        without_judgment["jobs"][0]["ai_review"].pop("judgment")
        delivery.validate_export(without_judgment)

    def test_missing_prunable_canonical_slots_are_rejected_before_cleanup_for_both_modes(self):
        self.ready(rows=self.rows[:1])
        original = delivery.preview_task(self.task)
        paths = [("source_task", "city"), ("jobs", 0, "city"), ("jobs", 0, "navigation"),
                 ("jobs", 0, "navigation", "ka"), ("jobs", 0, "selection", "manual_keep"),
                 ("jobs", 0, "ai_review", "reason")]
        for path in paths:
            preview = copy.deepcopy(original)
            container = preview
            for part in path[:-1]:
                container = container[part]
            del container[path[-1]]
            for fields in (None, ["jobs.title"]):
                with self.subTest(path=path, fields=fields), \
                     patch.object(delivery, "preview_task", return_value=preview):
                    with self.assertRaises(delivery.DeliveryExportError):
                        delivery.export_task(self.task, selected_fields=fields)
                    self.assertFalse((self.task.path / "delivery-exports").exists())

    def test_full_readback_must_match_the_exact_compacted_canonical_document(self):
        self.ready(rows=self.rows[:1])
        real_read = delivery._read_json
        parent = self.root / "full-readback"

        def tamper(path):
            document = real_read(path)
            if Path(path).name == ".delivery_jobs.json.tmp":
                document["jobs"][0]["title"] = "Different but schema-valid text"
            return document

        with patch.object(delivery, "_read_json", side_effect=tamper):
            with self.assertRaises(delivery.DeliveryExportError):
                delivery.export_task(self.task, parent)
        self.assertEqual(list(parent.iterdir()), [])


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
        for field, value in (("format", 1), ("status", []), ("judgment", []), ("validation", "unknown-check")):
            wrong = copy.deepcopy(original)
            wrong["jobs"][0]["ai_review"][field] = value
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
        self.assertIn("--fields FIELD [FIELD ...]", help_result.stdout)
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

    def test_cli_selected_fields_and_safe_invalid_field_failures(self):
        self.ready(rows=self.rows[:1])
        command = [sys.executable, str(PROJECT_ROOT / "delivery_export.py"), "--task", str(self.task.path)]
        environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        parent = self.root / "cli-subset"
        result = subprocess.run([*command, "--output-dir", str(parent), "--fields", "jobs.title", "jobs.city"],
                                capture_output=True, text=True, encoding="utf-8", env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = Path(result.stdout.strip())
        self.assertEqual(path.name, "岗位名称.json")
        self.assertEqual(self.read(path), {"jobs": [{"title": self.rows[0]["title"]}]})
        invalid_parent = self.root / "cli-invalid-fields"
        for fields, expected_code in ((["jobs"], 1), (["api_key=PRIVATE_FIELD_SECRET"], 1), ([], 2)):
            with self.subTest(fields=fields):
                invalid = subprocess.run([*command, "--output-dir", str(invalid_parent), "--fields", *fields],
                                         capture_output=True, text=True, encoding="utf-8", env=environment)
                self.assertEqual(invalid.returncode, expected_code)
                self.assertNotIn("PRIVATE_FIELD_SECRET", invalid.stdout + invalid.stderr)
                self.assertFalse(invalid_parent.exists())
        empty = subprocess.run([*command, "--output-dir", str(invalid_parent), "--fields", "jobs.city"],
                               capture_output=True, text=True, encoding="utf-8", env=environment)
        self.assertEqual(empty.returncode, 1)
        self.assertIn("所选字段当前均无值", empty.stderr)
        self.assertFalse(invalid_parent.exists())


if __name__ == "__main__":
    unittest.main()
