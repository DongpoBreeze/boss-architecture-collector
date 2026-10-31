"""Offline regressions for company exclusions after source-row deduplication."""

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import process_architecture as processor  # noqa: E402
import review_pipeline as pipeline  # noqa: E402
import stage_rules  # noqa: E402
from test_review_pipeline import PipelineCase  # noqa: E402


class ImportCompanyCandidateTests(unittest.TestCase):
    rules = {"exclude_company_keywords": ["eXcLuDeD"]}

    @staticmethod
    def source(company, number, **fields):
        return {"job_id": "same", "title": "AI解决方案工程师", "company": company,
                "source_file": f"raw/synthetic-{number}.json", "source_row": number,
                "jd": "岗位职责：负责业务需求分析和系统交付。", **fields}

    def merge_and_check(self, rows, *, keep=False):
        before = copy.deepcopy(rows)
        merged, metadata = processor.merge_rows(rows)
        self.assertEqual(metadata["unique_job_count"], 1)
        self.assertEqual(rows, before)
        result = stage_rules.apply_import_rules(merged[0], self.rules)
        self.assertEqual(result["import_rule_keep"], keep)
        self.assertEqual(processor.merge_rows(merged)[0], merged)
        return merged[0], result

    def test_same_field_names_and_sources_survive_dedupe_without_changing_display_or_ai_input(self):
        for names in (("Normal brand", "Excluded vendor"), ("Excluded vendor", "Normal brand")):
            with self.subTest(names=names):
                rows = [self.source(name, number) for number, name in enumerate(names)]
                merged, result = self.merge_and_check(rows)
                self.assertEqual(merged["company"], names[0])
                candidates = merged["field_candidates"]["company"]
                self.assertEqual({entry["value"] for entry in candidates}, set(names))
                for entry in candidates:
                    source = names.index(entry["value"])
                    self.assertEqual(entry["sources"], [{"source_file": f"raw/synthetic-{source}.json",
                                                         "source_row": source}])
                self.assertIn("命中公司排除词", result["import_rule_reason"])
                self.assertEqual(processor.model_input(rows[0]), processor.model_input(merged))
                self.assertEqual(processor.score_job(merged), processor.score_job(rows[0]))

    def test_all_whitelisted_company_aliases_keep_displaced_source_names(self):
        fields = (
            "company", "company_name", "companyName", "brand_name", "brandName",
            "company_full_name", "companyFullName", "company_short_name", "companyShortName",
            "brand_full_name", "brandFullName", "brand_short_name", "brandShortName",
            "company_info", "companyInfo", "brand_info", "brandInfo",
        )
        for field in fields:
            with self.subTest(field=field):
                first, second = "Normal brand", "Excluded vendor"
                if field.endswith(("info", "Info")):
                    first, second = {"fullName": first}, {"fullName": second}
                rows = [{"job_id": "same", field: first}, {"job_id": "same", field: second}]
                merged, _ = self.merge_and_check(rows)
                self.assertEqual(merged["company"], "Normal brand")
                self.assertEqual(len(merged["field_candidates"][field]), 2)

    def test_secondary_name_in_a_single_company_object_is_not_lost_during_normalization(self):
        row = self.source({"name": "Normal brand", "fullName": "Excluded vendor"}, 0)
        merged, _ = self.merge_and_check([row])
        self.assertEqual(merged["company"], "Normal brand")
        self.assertEqual(merged["company_name_source"], "company.name")

    def test_secondary_alias_on_one_row_and_prior_candidates_remain_rule_inputs(self):
        rows = [self.source("Normal brand", 0, company_name="Normal legal name"),
                self.source("Normal brand", 1, company_name="Excluded legal name")]
        merged, _ = self.merge_and_check(rows)
        self.assertEqual(merged["company_name"], "Normal legal name")
        saved = self.source("Normal brand", 0, field_candidates={"company": [
            {"value": "Normal brand", "sources": [{"source_file": "old.json", "source_row": 3}]},
            {"value": {"full_name": "Excluded prior name"},
             "sources": [{"source_file": "other.json", "source_row": 4}]},
        ]})
        prior, _ = self.merge_and_check([saved])
        self.assertEqual(len(prior["field_candidates"]["company"]), 2)
        self.assertTrue(any(entry["sources"] == [{"source_file": "other.json", "source_row": 4}]
                            for entry in prior["field_candidates"]["company"]))

    def test_late_identity_bridge_keeps_both_company_candidates(self):
        rows = [self.source("Normal brand", 0, job_id="first"),
                {"encrypt_job_id": "encrypted", "company": "Excluded vendor", "source_file": "second.json"},
                {"job_id": "first", "encrypt_job_id": "encrypted", "source_file": "bridge.json"}]
        merged, _ = self.merge_and_check(rows)
        self.assertEqual(merged["company"], "Normal brand")
        self.assertEqual({entry["value"] for entry in merged["field_candidates"]["company"]},
                         {"Normal brand", "Excluded vendor"})

    def test_repeated_merge_preserves_candidate_provenance_without_mutating_prior_rows(self):
        rows = [self.source("Normal brand", 0), self.source("Excluded vendor", 1)]
        merged, _ = self.merge_and_check(rows)
        before = copy.deepcopy(merged)
        for _ in range(3):
            again, _ = processor.merge_rows([merged, copy.deepcopy(merged)])
            self.assertEqual(again[0]["field_candidates"], before["field_candidates"])
            self.assertFalse(stage_rules.apply_import_rules(again[0], self.rules)["import_rule_keep"])
            merged = again[0]
        self.assertEqual(merged, before)

    def test_recruiter_names_and_non_name_object_fields_are_not_company_candidates(self):
        row = self.source({"name": "Normal brand", "recruiter": "Excluded recruiter",
                           "address": "Excluded address", "description": "Excluded description"}, 0,
                          boss_name="Excluded boss", recruiter="Excluded recruiter",
                          companyInfo={"name": "Normal legal name", "bossName": "Excluded boss"},
                          field_candidates={"boss_name": [{"value": "Excluded prior recruiter"}]})
        merged, _ = self.merge_and_check([row], keep=True)
        self.assertNotIn("Excluded", json.dumps(merged["field_candidates"], ensure_ascii=False))
        for company in (None, "", 42, ["Excluded non-name"]):
            with self.subTest(company=company):
                self.merge_and_check([self.source(company, 0, boss_name="Excluded recruiter")], keep=True)

    def test_verified_legacy_company_is_included_but_mixed_recruiter_evidence_is_not(self):
        legacy = {"job_id": "same", "salary_source": "api", "boss_name": "Excluded historical brand",
                  "company_scale": "", "company_stage": "", "company_industry": "",
                  "encrypt_brand_id": "", "company_link": ""}
        merged, _ = self.merge_and_check([self.source("Normal brand", 0), legacy])
        self.assertEqual(merged["company"], "Normal brand")
        for candidate in ({**legacy, "salary_source": "dom_untrusted"},
                          {key: value for key, value in legacy.items() if key != "company_link"}):
            with self.subTest(candidate=candidate):
                self.merge_and_check([self.source("Normal brand", 0), candidate], keep=True)
        recruiter = {"job_id": "same", "salary_source": "dom_untrusted", "boss_name": "Excluded recruiter"}
        metadata = {**legacy, "salary_source": "api_empty", "boss_name": ""}
        self.merge_and_check([recruiter, metadata], keep=True)


class ImportCompanyPipelineTests(PipelineCase):
    def test_real_import_deduplication_recommends_exclusion_from_any_original_company(self):
        first, second = copy.deepcopy(self.original[0]), copy.deepcopy(self.original[0])
        first["company"], second["company"] = "Normal brand", "Excluded vendor"
        self.write(self.source / "raw/current/shanghai/current__shanghai__aiarch__20_50__1_3.json",
                   {"city": "上海", "filters": {}, "jobs": [first]})
        self.write(self.source / "raw/target/shanghai/target__shanghai__aiarch__20_50__3_5.json",
                   {"city": "上海", "filters": {}, "jobs": [second]})
        self.task = self.create()
        self.task.set_stage_rules("import", {"exclude_company_keywords": ["Excluded"]})

        with patch.object(pipeline, "_run_child", side_effect=AssertionError("no real subprocess")):
            self.run_step("import")

        imported = self.task.jobs("import")
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["company"], "Normal brand")
        self.assertFalse(imported[0]["import_rule_keep"])
        self.assertEqual(self.task.stage_info("import")["keep_ids"], [])
        self.assertEqual(self.task.stage_info("import")["result_ids"], ["a"])
        self.assertEqual(self.task.snapshot()["baseline_ids"], ["a"])


if __name__ == "__main__":
    unittest.main()
