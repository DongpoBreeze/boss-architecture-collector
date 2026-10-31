import codecs
import copy
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import desktop_data as data  # noqa: E402
import checkpoint_io  # noqa: E402


class DataTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_json(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def write_text(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def index(self, jobs, metadata=None, run="run"):
        return self.write_json(f"{run}/prepared/merged_jobs_index.json",
                               {"metadata": metadata or {}, "jobs": jobs})

    def manifest(self, value=None, run="run"):
        return self.write_json(f"{run}/run_manifest.json", value or {"status": "success"})

    def make_fifty(self):
        return [
            {
                "job_id": str(number), "title": f"岗位 {number}",
                "classification": "重点投递" if number < 7 else "海投池" if number < 35 else "暂不投",
                "jd": f"JD 原文 {number}" if number < 42 else "",
                "jd_fetched": number < 42,
                "model_status": "success" if number < 42 else "not_run",
                "observed_in": [{"city": "上海", "source_file": "raw/list.json"}],
            }
            for number in range(50)
        ]


class RunResultsTests(DataTestCase):
    @unittest.skipUnless(os.name == "nt", "Windows shared checkpoint reads")
    def test_result_browser_reader_does_not_block_checkpoint_replacement(self):
        target = self.write_json("run/prepared/jd_shortlists/detail_batches/details_0001.json",
                                 [{"job_id": "saved", "jd": "旧的完整正文"}])
        replacement = self.write_json("replacement.json", [{"job_id": "saved", "jd": "新的完整正文"}])
        open_read = checkpoint_io.open_shared_read

        def open_and_publish(path):
            handle = open_read(path)
            try:
                checkpoint_io.replace_with_retry(replacement, path)
            except BaseException:
                handle.close()
                raise
            return handle

        warnings = []
        with patch.object(checkpoint_io, "open_shared_read", side_effect=open_and_publish), \
             patch.object(checkpoint_io.time, "sleep", side_effect=AssertionError("result browser blocked publication")):
            saved = data._read_json(target, warnings)
        self.assertEqual(saved.state, "valid")
        self.assertEqual(saved.value[0]["jd"], "旧的完整正文")
        self.assertEqual(checkpoint_io.read_json(target)[0]["jd"], "新的完整正文")
        self.assertEqual(warnings, [])

    def test_full_fifty_index_is_authoritative_not_thirty_five_pool(self):
        jobs = self.make_fifty()
        self.manifest({"status": "partial", "classification_counts": {"重点投递": 999}})
        self.index(jobs, {"unique_job_count": 50})
        self.write_json("run/prepared/hai_tou_pool.json", {
            "metadata": {"unique_job_count": 50},
            "summary": {"total": 35}, "jobs": jobs[:35],
        })
        loaded = data.load_run_results(self.root / "run")
        self.assertEqual(loaded["summary"]["total"], 50)
        self.assertEqual(loaded["summary"]["classification_counts"], {
            "重点投递": 7, "海投池": 28, "暂不投": 15,
        })
        self.assertEqual(loaded["summary"]["classification_status"], "complete")
        self.assertEqual(loaded["summary"]["classified_count"], 50)
        self.assertEqual(loaded["summary"]["unclassified_count"], 0)
        for key, expected in {"jd_success": 42, "llm_success": 42, "llm_failed": 0,
                              "llm_not_run": 8, "is_subset": False}.items():
            self.assertEqual(loaded["summary"][key], expected)
        self.assertEqual(loaded["jobs"], jobs)
        self.assertEqual(loaded["manifest"]["status"], "partial")
        self.assertIn("merged_jobs", loaded["artifacts"])

    def test_pool_only_is_explicitly_a_subset_and_counts_only_its_rows(self):
        self.manifest({"status": "partial", "sample_size": 50})
        self.write_json("run/prepared/hai_tou_pool.json", {
            "metadata": {"unique_job_count": 50, "llm_success_count": 42},
            "summary": {"total": 35, "classification_counts": {"暂不投": 15}},
            "jobs": self.make_fifty()[:35],
        })
        loaded = data.load_run_results(self.root / "run")
        self.assertEqual(loaded["summary"]["total"], 35)
        self.assertEqual(loaded["summary"]["llm_success"], 35)
        self.assertEqual(loaded["summary"]["classification_counts"]["暂不投"], 0)
        self.assertTrue(loaded["summary"]["is_subset"])
        self.assertIn("子集", "；".join(loaded["warnings"]))

    def test_missing_empty_and_invalid_are_distinct_not_faked_zero(self):
        cases = (("missing", None, "缺失文件"), ("empty", " \n", "空文件"),
                 ("invalid", '{"jobs": [', "JSON 无效"))
        for name, content, expected in cases:
            with self.subTest(name=name):
                self.manifest(run=name)
                if content is not None:
                    self.write_text(f"{name}/prepared/merged_jobs_index.json", content)
                    self.write_json(f"{name}/prepared/hai_tou_pool.json", {"jobs": [{"job_id": "pool"}]})
                loaded = data.load_run_results(self.root / name)
                self.assertEqual(loaded["jobs"], [])
                self.assertIsNone(loaded["summary"]["total"])
                self.assertIsNone(loaded["summary"]["llm_success"])
                self.assertEqual(loaded["summary"]["classification_counts"], {})
                self.assertEqual(loaded["summary"]["classification_status"], "unavailable")
                self.assertIsNone(loaded["summary"]["classified_count"])
                self.assertIsNone(loaded["summary"]["unclassified_count"])
                self.assertIn(expected, "；".join(loaded["warnings"]))
                self.assertNotIn("pool", [job.get("job_id") for job in loaded["jobs"]])

    def test_valid_empty_arrays_are_known_zero(self):
        self.manifest()
        self.index([])
        summary = data.load_run_results(self.root / "run")["summary"]
        for key in ("total", "jd_success", "llm_success", "llm_failed", "llm_not_run"):
            self.assertEqual(summary[key], 0)

    def test_nonempty_preprocessed_index_without_report_has_unknown_class_counts(self):
        self.manifest({"status": "success", "stages": {"列表前处理": {"status": "success"}},
                       "classification_counts": {"重点投递": 0, "海投池": 0, "暂不投": 0}})
        source = self.index([{"job_id": "a", "title": "still waiting"},
                             {"job_id": "b", "classification": ""}])
        original = source.read_bytes()
        result = data.load_run_results(self.root / "run")
        summary = result["summary"]
        self.assertFalse((self.root / "run/prepared/海投分层清单.md").exists())
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["classification_counts"], {})
        self.assertEqual(summary["classification_status"], "unclassified")
        self.assertEqual((summary["classified_count"], summary["unclassified_count"]), (0, 2))
        self.assertIn("三类数量未知", "；".join(result["warnings"]))
        snapshot = data.read_live_snapshot(self.root / "run")
        self.assertEqual(snapshot["classification_counts"], {})
        self.assertEqual(snapshot["classification_status"], "unclassified")
        self.assertEqual(snapshot["unclassified_count"], 2)
        self.assertEqual(snapshot["stage"], "列表前处理")
        self.assertEqual(source.read_bytes(), original)

    def test_partial_classification_counts_only_known_categories_and_tracks_unclassified(self):
        self.manifest(run="runs/partial")
        self.index([
            {"job_id": "a", "classification": "重点投递"},
            {"job_id": "b", "classification": "海投池"},
            {"job_id": "c"}, {"job_id": "d", "classification": "unknown"},
            {"job_id": "e", "classification": ["暂不投"]},
        ], run="runs/partial")
        result = data.load_run_results(self.root / "runs/partial")
        summary = result["summary"]
        self.assertEqual(summary["classification_counts"], {"重点投递": 1, "海投池": 1})
        self.assertNotIn("暂不投", summary["classification_counts"])
        self.assertEqual(summary["classification_status"], "partial")
        self.assertEqual((summary["classified_count"], summary["unclassified_count"]), (2, 3))
        self.assertIn("2/5", "；".join(result["warnings"]))
        self.assertIn("未出现类别仍未知", "；".join(result["warnings"]))
        before = copy.deepcopy(result["jobs"])
        self.assertEqual(len(data.filter_jobs(result["jobs"])), 5)
        self.assertEqual(result["jobs"], before)
        snapshot = data.read_live_snapshot(self.root / "runs/partial")
        history = data.list_run_history(self.root)[0]
        for view in (snapshot, history):
            self.assertEqual(view["classification_counts"], summary["classification_counts"])
            self.assertEqual(view["classification_status"], "partial")
            self.assertEqual((view["classified_count"], view["unclassified_count"]), (2, 3))

    def test_complete_classification_proves_absent_category_is_really_zero(self):
        self.manifest({"status": "success", "stages": {"生成报告": {"status": "success"}}})
        self.index([{"job_id": "a", "classification": "海投池"},
                    {"job_id": "b", "classification": "海投池"},
                    {"job_id": "c", "classification": "暂不投"}])
        self.write_text("run/prepared/海投分层清单.md", "# 已有报告\n重点投递 0；海投池 2；暂不投 1。")
        summary = data.load_run_results(self.root / "run")["summary"]
        self.assertEqual(summary["classification_counts"], {"重点投递": 0, "海投池": 2, "暂不投": 1})
        self.assertEqual(summary["classification_status"], "complete")
        self.assertEqual((summary["classified_count"], summary["unclassified_count"]), (3, 0))

    def test_valid_empty_report_keeps_true_zero_classification_counts(self):
        self.manifest({"status": "success", "stages": {"生成报告": {"status": "success"}}})
        self.index([])
        self.write_text("run/prepared/海投分层清单.md", "# 有效空报告\n去重岗位 0。")
        for summary in (data.load_run_results(self.root / "run")["summary"],
                        data.read_live_snapshot(self.root / "run")):
            self.assertEqual(summary["classification_counts"], {"重点投递": 0, "海投池": 0, "暂不投": 0})
            self.assertEqual(summary["classification_status"], "complete")
            self.assertEqual((summary["classified_count"], summary["unclassified_count"]), (0, 0))

    def test_bad_schema_and_manifest_are_friendly(self):
        self.write_json("run/run_manifest.json", ["not an object"])
        self.write_json("run/prepared/merged_jobs_index.json", {"jobs": "invalid"})
        loaded = data.load_run_results(self.root / "run")
        self.assertEqual(loaded["manifest"], {})
        self.assertIsNone(loaded["summary"]["total"])
        self.assertGreaterEqual("；".join(loaded["warnings"]).count("结构无效"), 2)

    def test_duplicate_ids_merge_provenance_keep_first_classification(self):
        self.manifest()
        jobs = [
            {"job_id": "same", "title": "first", "classification": "海投池", "skills": "",
             "source_file": "a.json", "observed_in": [{"source_file": "a.json"}]},
            {"job_id": "same", "title": "later", "classification": "重点投递", "skills": "RAG",
             "source_file": "b.json", "observed_in": [{"source_file": "b.json"}]},
            {"title": "anonymous one"}, {"title": "anonymous two"},
        ]
        source = self.index(jobs)
        original = source.read_bytes()
        loaded = data.load_run_results(self.root / "run")
        self.assertEqual(len(loaded["jobs"]), 3)
        first = loaded["jobs"][0]
        self.assertEqual(first["classification"], "海投池")
        self.assertEqual(first["title"], "first")
        self.assertEqual(first["skills"], "RAG")
        self.assertEqual(first["source_files"], ["a.json", "b.json"])
        self.assertEqual(first["observed_in"], [{"source_file": "a.json"}, {"source_file": "b.json"}])
        self.assertIn("未重新判断", "；".join(loaded["warnings"]))
        self.assertEqual(source.read_bytes(), original)

    def test_actual_company_and_jd_can_be_supplemented_on_copies_only(self):
        self.manifest()
        source = self.index([{"job_id": "a", "boss_name": "某招聘者", "classification": "海投池"}])
        original = source.read_bytes()
        detail = self.write_json("run/prepared/jd_shortlists/details.json", [
            {"job_id": "a", "company": "实际公司", "jd": "保存的完整 JD"},
        ])
        job = data.load_run_results(self.root / "run")["jobs"][0]
        self.assertEqual(job["company"], "实际公司")
        self.assertEqual(job["jd"], "保存的完整 JD")
        self.assertEqual(job["classification"], "海投池")
        self.assertEqual(job["desktop_detail_source"], str(detail))
        self.assertEqual(source.read_bytes(), original)

    def test_bom_and_non_list_manifest_warnings_do_not_crash(self):
        self.manifest({"status": "running", "warnings": 42})
        self.write_text("run/prepared/merged_jobs_index.json", '\ufeff{"jobs": []}')
        self.assertEqual(data.load_run_results(self.root / "run")["summary"]["total"], 0)

    def test_non_json_numeric_constants_are_invalid_not_completion_counts(self):
        self.manifest()
        self.write_text("run/prepared/merged_jobs_index.json", '{"jobs": [], "count": NaN}')
        result = data.load_run_results(self.root / "run")
        self.assertIsNone(result["summary"]["total"])
        self.assertIn("JSON 无效", "；".join(result["warnings"]))


class CompanyNameTests(DataTestCase):
    @staticmethod
    def legacy(**changes):
        return {"salary_source": "api", "boss_name": "历史 API 公司",
                "company_scale": "100-499人", "company_stage": "", "company_industry": "软件",
                "encrypt_brand_id": "", "company_link": "", **changes}

    def test_explicit_names_objects_and_aliases_have_priority(self):
        cases = [
            ({"company": " 正式公司 ", "company_name": "其他名称"}, ("正式公司", "company")),
            ({"company": {"name": "", "full_name": "完整公司名称"}}, ("完整公司名称", "company.full_name")),
            ({"company": {"name": None, "full_name": 42}, "companyName": "驼峰公司"}, ("驼峰公司", "companyName")),
            ({"brandName": "品牌公司"}, ("品牌公司", "brandName")),
            ({"companyFullName": "完整名称"}, ("完整名称", "companyFullName")),
            ({"company_short_name": "公司简称"}, ("公司简称", "company_short_name")),
            ({"companyInfo": {"fullName": "公司对象"}}, ("公司对象", "companyInfo.fullName")),
            ({"brand_info": {"short_name": "品牌简称"}}, ("品牌简称", "brand_info.short_name")),
        ]
        for explicit, expected in cases:
            with self.subTest(explicit=explicit):
                row = self.legacy(**explicit)
                before = copy.deepcopy(row)
                self.assertEqual(data.company_name_source(row), expected)
                self.assertEqual(row, before)

    def test_only_string_names_are_accepted(self):
        for invalid in (None, 42, True, [], {}, {"name": None, "full_name": 42, "company_name": []}):
            with self.subTest(invalid=invalid):
                self.assertEqual(data.company_name_source({"company": invalid, "boss_name": "招聘者"}), ("", None))
        for invalid_source in ([], {}, 42, None):
            with self.subTest(source=invalid_source):
                self.assertEqual(data.company_name_source({"company": "明确公司", "company_name_source": invalid_source}),
                                 ("明确公司", "company"))

    def test_legacy_api_shape_allows_empty_brand_id_link_and_funding(self):
        for salary_source in ("api", "api_empty"):
            with self.subTest(source=salary_source):
                self.assertEqual(data.company_name_source(self.legacy(salary_source=salary_source)),
                                 ("历史 API 公司", "legacy_api_boss_name"))

    def test_legacy_api_shape_requires_every_string_metadata_field(self):
        for key in ("company_scale", "company_stage", "company_industry", "encrypt_brand_id", "company_link"):
            row = self.legacy()
            del row[key]
            self.assertEqual(data.company_name_source(row), ("", None), key)
            for invalid in (None, 42, True, [], {}):
                with self.subTest(key=key, invalid=invalid):
                    self.assertEqual(data.company_name_source(self.legacy(**{key: invalid})), ("", None))

    def test_dom_unknown_sources_and_invalid_recruiter_values_never_supply_company(self):
        for source in ("dom_untrusted", "dom", "API", "api ", "", None, [], {}):
            with self.subTest(source=source):
                self.assertEqual(data.company_name_source(self.legacy(salary_source=source)), ("", None))
        for boss_name in (None, 42, True, [], {}, " \t "):
            with self.subTest(boss_name=boss_name):
                self.assertEqual(data.company_name_source(self.legacy(boss_name=boss_name)), ("", None))

    def test_normalized_provenance_is_stable_and_explicit_alias_overrides_inference(self):
        inferred = self.legacy(company="恢复公司", company_name_source="legacy_api_boss_name")
        self.assertEqual(data.company_name_source(inferred), ("恢复公司", "legacy_api_boss_name"))
        self.assertEqual(data.company_name_source({**inferred, "brandName": "明确品牌"}), ("明确品牌", "brandName"))
        normalized = {"company": "公司对象", "company_name_source": "companyInfo.fullName"}
        self.assertEqual(data.company_name_source(normalized), ("公司对象", "companyInfo.fullName"))
        self.assertEqual(data.company_name_source(self.legacy(company="", company_name_source="unavailable")), ("", None))

    def test_readonly_browser_search_details_and_csv_use_recovered_name(self):
        self.manifest()
        row = self.legacy(job_id="old", title="企业架构师")
        source = self.index([row])
        before = source.read_bytes()
        jobs = data.load_run_results(self.root / "run")["jobs"]
        self.assertEqual(jobs, [row])
        self.assertEqual(data.filter_jobs(jobs, search="历史 API 公司"), jobs)
        self.assertIn("公司：历史 API 公司", data.format_job_detail(jobs[0]))
        path = data.export_jobs(self.root / "legacy.csv", jobs)
        exported = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
        self.assertEqual(exported[0]["company"], "历史 API 公司")
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(jobs, [row])


class ExperienceSourceTests(DataTestCase):
    def test_explicit_aliases_take_priority_and_preserve_trimmed_wording(self):
        cases = [
            ({"experience": " 1 - 3 年 ", "job_experience": "5-10年", "tags": "10年以上 | 本科"},
             ("1 - 3 年", "experience")),
            ({"experience": "", "job_experience": " 经验不限 ", "jobExperience": "3-5年"},
             ("经验不限", "job_experience")),
            ({"experience": None, "job_experience": {}, "jobExperience": " 在校/应届 "},
             ("在校/应届", "jobExperience")),
            ({"experience": " 不限 ", "tags": "1-3年 | 本科"}, ("不限", "experience")),
        ]
        for row, expected in cases:
            with self.subTest(row=row):
                before = copy.deepcopy(row)
                self.assertEqual(data.experience_source(row), expected)
                self.assertEqual(row, before)

    def test_common_complete_labels_are_recovered_without_reformatting(self):
        labels = ("1-3年", "3-5年", "5-10年", "10年以上", "10年+", "1年以内", "经验不限",
                  "在校/应届", "在校生/应届生/无经验", "无经验", "应届生", "在校生", "1～3年",
                  "3至5年", "5—10年", "10 年 ＋", "1 年以下", "在校 ／ 应届生", "1 - 3 年")
        for field in ("tags", "tags_list", "experience_tags", "job_labels"):
            for label in labels:
                with self.subTest(field=field, label=label):
                    self.assertEqual(data.experience_source({field: f" 本科 | {label} | 五险一金 "}), (label, field))
                    self.assertEqual(data.experience_source({field: ["本科", f" {label} ", "五险一金"]}), (label, field))

    def test_explicit_separators_and_duplicate_same_labels_are_safe(self):
        for separator in (" | ", "｜", ",", "，", ";", "；", "、", "\n", "\r\n"):
            with self.subTest(separator=separator):
                self.assertEqual(data.experience_source({"tags": f"本科{separator}1-3年{separator}五险一金"}),
                                 ("1-3年", "tags"))
        self.assertEqual(data.experience_source({"tags": " 1-3年 | 1-3年 | 本科 ", "job_labels": ["1-3年", "本科"]}),
                         ("1-3年", "tags"))

    def test_multiple_distinct_tag_values_stay_unknown_even_across_fields(self):
        for row in ({"tags": "1-3年 | 3-5年 | 本科"},
                    {"job_labels": ["经验不限", "1-3年", "本科"]},
                    {"tags": "在校/应届 | 本科", "experience_tags": "1年以内"},
                    {"tags": "1 - 3 年", "tags_list": ["1-3年"]}):
            with self.subTest(row=row):
                self.assertEqual(data.experience_source(row), ("", None))
                self.assertEqual(data.experience_source({**row, "jobExperience": "3-5年"}), ("3-5年", "jobExperience"))

    def test_education_descriptions_skills_jd_filters_and_numeric_codes_are_not_experience(self):
        labels = ("不限", "本科", "学历不限", "本科 | 不限", "技能不限", "要求1-3年相关经验", "工作经验1-3年",
                  "具备3-5年经验优先", "公司成立10年以上", "3-5年发展规划", "在校/应届优先", "Python 3-5年",
                  "1-3年 本科", "105", "经验：1-3年")
        for label in labels:
            with self.subTest(label=label):
                self.assertEqual(data.experience_source({"tags": label, "job_labels": [label]}), ("", None))
        row = {"title": "1-3年", "jd": "任职要求：具备3-5年工作经验。", "description": "5-10年",
               "skills": ["1-3年"], "skill_tags": ["3-5年"], "experience_filter": "经验不限", "experience_id": "105",
               "filters": {"experience": "1-3年"},
               "observed_in": [{"experience_filter": "1-3年", "experience_id": "1_3"}]}
        self.assertEqual(data.experience_source(row), ("", None))
        for invalid in (None, 105, "105", " 105 ", "０１０５", True, [], {}, ["1-3年"]):
            for field in ("experience", "job_experience", "jobExperience"):
                with self.subTest(field=field, invalid=invalid):
                    self.assertEqual(data.experience_source({field: invalid}), ("", None))
        for invalid in (None, 105, True, {}, {"name": "1-3年"}, [["1-3年"]]):
            self.assertEqual(data.experience_source({"tags": invalid}), ("", None))

    def test_provenance_retains_quality_and_blocks_later_mixed_tag_inference(self):
        normalized = {"experience": "1-3年", "experience_source": "tags", "job_labels": ["3-5年"]}
        self.assertEqual(data.experience_source(normalized), ("1-3年", "tags"))
        self.assertEqual(data.experience_source({**normalized, "jobExperience": "10年以上"}), ("10年以上", "jobExperience"))
        self.assertEqual(data.experience_source({"experience": "1-3年", "experience_source": "jobExperience"}),
                         ("1-3年", "jobExperience"))
        for marker in ("unavailable", None, [], {}):
            row = {"experience": "", "experience_source": marker, "tags": "1-3年 | 本科"}
            self.assertEqual(data.experience_source(row), ("", None))
            self.assertEqual(data.experience_source({**row, "jobExperience": "不限"}), ("不限", "jobExperience"))

    def test_result_reader_and_detail_remain_readonly_and_show_alias_with_education(self):
        self.manifest()
        rows = [{"job_id": "explicit", "jobExperience": "3-5年", "tags": "本科 | 五险一金"},
                {"job_id": "legacy", "tags": "经验不限 | 本科"}]
        path = self.index(rows)
        before = path.read_bytes()
        jobs = data.load_run_results(self.root / "run")["jobs"]
        self.assertEqual(jobs, rows)
        self.assertIn("经验/学历：3-5年；本科 | 五险一金", data.format_job_detail(jobs[0]))
        self.assertIn("经验/学历：经验不限", data.format_job_detail(jobs[1]))
        self.assertEqual([data.experience_source(job)[0] for job in jobs], ["3-5年", "经验不限"])
        self.assertEqual(jobs, rows)
        self.assertEqual(path.read_bytes(), before)

    def test_csv_projects_shared_experience_while_json_keeps_original_rows(self):
        rows = [{"job_id": "legacy", "tags": "1-3年 | 本科"},
                {"job_id": "alias", "jobExperience": " 经验不限 "},
                {"job_id": "unknown", "tags": "本科 | 不限"},
                {"job_id": "code", "experience": "105"}]
        before = copy.deepcopy(rows)
        path = data.export_jobs(self.root / "experience.csv", rows)
        reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig")))
        self.assertIn("experience", reader.fieldnames)
        self.assertEqual([row["experience"] for row in reader], ["1-3年", "经验不限", "", ""])
        json_path = data.export_jobs(self.root / "experience.json", rows)
        self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["jobs"], rows)
        self.assertEqual(rows, before)


class HistoryTests(DataTestCase):
    def test_history_is_sorted_by_saved_start_includes_broken_runs_and_limit(self):
        self.manifest({"started_at": "2026-10-16T12:00:00+08:00", "status": "failed",
                       "city": "上海", "sample_size": 50}, "runs/old")
        self.index(self.make_fifty(), run="runs/old")
        self.manifest({"started_at": "2026-10-18T12:00:00+08:00", "status": "partial",
                       "city": "深圳", "sample_size": 25}, "runs/new")
        self.index([], run="runs/new")
        self.write_text("runs/20261017-120000-000000-broken/run_manifest.json", "{")
        entries = data.list_run_history(self.root)
        self.assertEqual([entry["name"] for entry in entries], [
            "new", "20261017-120000-000000-broken", "old",
        ])
        self.assertEqual(entries[0]["status"], "partial")
        self.assertEqual(entries[0]["city"], "深圳")
        self.assertEqual(entries[0]["sample_size"], 25)
        self.assertEqual(entries[1]["status"], "unknown")
        self.assertIsNone(entries[1]["unique_job_count"])
        self.assertTrue(entries[1]["warnings"])
        self.assertEqual(entries[2]["unique_job_count"], 50)
        self.assertEqual(entries[2]["classification_counts"]["暂不投"], 15)
        self.assertEqual(len(data.list_run_history(self.root, limit=1)), 1)

    def test_empty_directories_and_equal_dates_have_stable_order(self):
        for name in ("a", "z"):
            self.manifest({"started_at": "2026-10-18T12:00:00+08:00", "status": "paused"}, f"runs/{name}")
        first = data.list_run_history(self.root)
        self.assertEqual([entry["name"] for entry in first], ["z", "a"])
        self.assertEqual(first, data.list_run_history(self.root))
        self.assertTrue(all(entry["warnings"] for entry in first))

    def test_absent_history_is_not_created(self):
        self.assertEqual(data.list_run_history(self.root / "absent"), [])
        self.assertFalse((self.root / "absent").exists())
        self.assertEqual(data.list_run_history(self.root, limit=0), [])


class LiveSnapshotTests(DataTestCase):
    def test_unknown_progress_remains_none(self):
        snapshot = data.read_live_snapshot(self.root / "absent")
        for key in ("raw_record_count", "unique_job_count", "jd_success", "jd_total", "jd_missing",
                    "jd_failed", "jd_paused", "llm_success", "llm_total", "llm_failed", "llm_fallback"):
            self.assertIsNone(snapshot[key], key)
        self.assertEqual(snapshot["stage_statuses"], {})
        self.assertEqual(snapshot["status"], "unknown")
        self.assertFalse((self.root / "absent").exists())

    def test_live_reviews_checkpoint_without_final_manifest_shows_progress(self):
        self.manifest({"status": "running", "stages": {"列表前处理": {"status": "success"},
                                                            "Codex语义复核": {"status": "running"}}})
        jobs = [{"job_id": str(number), "title": "saved"} for number in range(4)]
        self.index(jobs)
        self.write_json("run/raw_sample.json", {"metadata": {"raw_record_count": 120, "unique_job_count": 4}, "jobs": jobs})
        self.write_json("run/prepared/jd_shortlists/jd_input.json", {"jobs": jobs})
        self.write_json("run/prepared/jd_shortlists/details.json", [
            {"job_id": str(number), "jd": "JD body"} for number in range(3)
        ])
        self.write_json("run/prepared/llm_reviews.json", {"reviews": [
            {"job_id": "0", "status": "failed"}, {"job_id": "0", "status": "success"},
            {"job_id": "1", "status": "failed"}, {"job_id": "foreign", "status": "success"},
        ]})
        snapshot = data.read_live_snapshot(self.root / "run")
        self.assertEqual(snapshot["stage"], "Codex语义复核")
        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(snapshot["stage_statuses"]["Codex语义复核"], "running")
        self.assertEqual(snapshot["raw_record_count"], 120)
        self.assertEqual(snapshot["unique_job_count"], 4)
        self.assertEqual((snapshot["jd_success"], snapshot["jd_total"], snapshot["jd_missing"]), (3, 4, 1))
        self.assertEqual((snapshot["llm_success"], snapshot["llm_total"], snapshot["llm_failed"], snapshot["llm_fallback"]), (1, 3, 1, 1))
        self.assertIsNone(snapshot["jd_failed"])
        self.assertIsNone(snapshot["jd_paused"])

    def test_in_flight_detail_batch_checkpoint_is_counted_without_manifest(self):
        self.manifest({"status": "running", "stages": {"JD补抓": {"status": "running"}}})
        jobs = [{"job_id": str(number)} for number in range(3)]
        self.index(jobs)
        self.write_json("run/prepared/jd_shortlists/jd_input.json", {"jobs": jobs})
        self.write_json("run/prepared/jd_shortlists/detail_batches/details_0001.json", [
            {"job_id": "0", "jd": "body 0"}, {"job_id": "1", "jd": "body 1"},
            {"job_id": "0", "jd": "body 0 duplicate"}, {"job_id": "2", "jd": ""},
        ])
        self.write_json("run/prepared/llm_reviews.json", {"reviews": []})
        snapshot = data.read_live_snapshot(self.root / "run")
        self.assertEqual((snapshot["jd_success"], snapshot["jd_total"], snapshot["jd_missing"]), (2, 3, 1))
        self.assertEqual((snapshot["llm_success"], snapshot["llm_total"]), (0, 2))

    def test_failed_and_paused_counts_are_unfinished_jobs_not_batches(self):
        self.manifest({"status": "paused", "stages": {"JD补抓": {"status": "paused"}}})
        jobs = [{"job_id": str(number)} for number in range(5)]
        self.write_json("run/prepared/jd_shortlists/jd_input.json", {"jobs": jobs})
        self.write_json("run/prepared/jd_shortlists/details.json", [
            {"job_id": "0", "jd": "done"}, {"job_id": "3", "jd": "recovered"},
        ])
        self.write_json("run/prepared/jd_shortlists/details_manifest.json", {
            "total_jobs": 5, "records": [
                {"batch": 1, "status": "paused", "count": 3, "fetched_count": 1,
                 "job_ids": ["0", "1", "2"], "missing_job_ids": ["1", "2"]},
                {"batch": 2, "status": "failed", "count": 2, "fetched_count": 0,
                 "job_ids": ["3", "4"], "missing_job_ids": ["3", "4"]},
            ],
        })
        snapshot = data.read_live_snapshot(self.root / "run")
        self.assertEqual(snapshot["jd_paused"], 2)
        self.assertEqual(snapshot["jd_failed"], 1)
        self.assertEqual((snapshot["jd_success"], snapshot["jd_missing"]), (2, 3))

    def test_half_written_details_are_unknown_not_zero(self):
        self.manifest({"status": "running", "stages": {"JD补抓": {"status": "running"}}})
        self.write_json("run/prepared/jd_shortlists/jd_input.json", {"jobs": [{"job_id": "0"}]})
        self.write_text("run/prepared/jd_shortlists/details.json", '[{"job_id":')
        snapshot = data.read_live_snapshot(self.root / "run")
        self.assertEqual(snapshot["jd_total"], 1)
        self.assertIsNone(snapshot["jd_success"])
        self.assertIsNone(snapshot["jd_missing"])
        self.assertIn("JSON 无效", "；".join(snapshot["warnings"]))

    def test_explicit_manifest_counts_can_be_used_when_records_are_missing(self):
        self.manifest({"status": "partial", "stages": {"Codex语义复核": {"status": "success"}}})
        self.write_json("run/prepared/llm_manifest.json", {
            "candidate_count": 6, "completed_count": 4, "failed_count": 1, "fallback_count": 1,
        })
        self.write_json("run/prepared/jd_shortlists/details_manifest.json", {
            "total_jobs": 7, "records": [{"batch": 1, "status": "partial", "fetched_count": 6}],
        })
        snapshot = data.read_live_snapshot(self.root / "run")
        self.assertEqual((snapshot["llm_success"], snapshot["llm_total"], snapshot["llm_failed"]), (4, 6, 1))
        self.assertEqual((snapshot["jd_success"], snapshot["jd_total"], snapshot["jd_missing"]), (6, 7, 1))

    def test_configuration_failed_fallback_is_not_a_model_request_failure(self):
        self.manifest({"status": "partial"})
        self.write_json("run/prepared/llm_manifest.json", {
            "status": "configuration_failed", "candidate_count": 3,
            "completed_count": 0, "failed_count": 0, "fallback_count": 3,
        })
        self.write_json("run/prepared/llm_reviews.json", {"reviews": []})
        snapshot = data.read_live_snapshot(self.root / "run")
        self.assertEqual((snapshot["llm_total"], snapshot["llm_failed"], snapshot["llm_fallback"]), (3, 0, 3))

    def test_zero_candidate_checkpoint_is_known_zero(self):
        self.manifest()
        self.write_json("run/prepared/jd_shortlists/jd_input.json", {"jobs": []})
        snapshot = data.read_live_snapshot(self.root / "run")
        for key in ("jd_success", "jd_total", "jd_missing", "jd_failed", "jd_paused",
                    "llm_total", "llm_success", "llm_failed", "llm_fallback"):
            self.assertEqual(snapshot[key], 0, key)

    def test_current_candidate_scope_wins_over_a_stale_llm_manifest(self):
        self.manifest({"status": "running", "stages": {"Codex语义复核": {"status": "running"}}})
        self.write_json("run/prepared/jd_shortlists/jd_input.json", {"jobs": [{"job_id": "new"}]})
        self.write_json("run/prepared/jd_shortlists/details.json", [{"job_id": "new", "jd": "body"}])
        self.write_json("run/prepared/llm_manifest.json", {"candidate_count": 42, "completed_count": 42})
        self.write_json("run/prepared/llm_reviews.json", {"reviews": [{"job_id": "new", "status": "success"}]})
        snapshot = data.read_live_snapshot(self.root / "run")
        self.assertEqual((snapshot["llm_success"], snapshot["llm_total"]), (1, 1))
        self.assertIn("不一致", "；".join(snapshot["warnings"]))

    def test_unavailable_jd_does_not_hide_saved_model_progress(self):
        self.manifest({"status": "running", "stages": {"Codex语义复核": {"status": "running"}}})
        self.index([{"job_id": "a", "jd_fetched": True}])
        self.write_json("run/prepared/jd_shortlists/jd_input.json", {"jobs": [{"job_id": "a"}]})
        self.write_text("run/prepared/jd_shortlists/details.json", "[")
        self.write_json("run/prepared/llm_manifest.json", {"candidate_count": 1})
        self.write_json("run/prepared/llm_reviews.json", {"reviews": [{"job_id": "a", "status": "success"}]})
        snapshot = data.read_live_snapshot(self.root / "run")
        self.assertEqual((snapshot["llm_success"], snapshot["llm_total"]), (1, 1))

    def test_resumed_failure_is_not_mislabelled_as_old_report_stage(self):
        self.manifest({"status": "stopped_or_failed", "stages": {
            "JD补抓": {"status": "failed"}, "生成报告": {"status": "success"},
        }})
        snapshot = data.read_live_snapshot(self.root / "run")
        self.assertEqual(snapshot["stage"], "JD补抓")

    def test_collection_record_counts_are_read_only_and_unknown_when_incomplete(self):
        self.manifest({"status": "running", "stages": {"岗位采集": {"status": "running"}}})
        source = self.write_json("source/collection_manifest.json", {
            "records": [{"job_count": 10}, {"job_count": None}],
        })
        first = data.read_live_snapshot(self.root / "run", self.root / "source")
        self.assertIsNone(first["raw_record_count"])
        self.write_json("source/collection_manifest.json", {"records": [{"job_count": 10}, {"job_count": 2}]})
        before = source.read_bytes()
        self.assertEqual(data.read_live_snapshot(self.root / "run", self.root / "source")["raw_record_count"], 12)
        self.assertEqual(source.read_bytes(), before)


class FilteringTests(unittest.TestCase):
    def jobs(self):
        return [
            {"job_id": "h", "title": "Z job", "classification": "海投池", "score": 90,
             "company": "Acme", "location": "上海·浦东", "skills": "RAG Python", "jd": "body", "model_status": "failed"},
            {"job_id": "p2", "title": "A job", "classification": "重点投递", "score": 10,
             "company": "Beta", "city": "深圳", "jd_fetched": False, "model_status": "not_run"},
            {"job_id": "p1", "title": "A job", "classification": "重点投递", "score": 10,
             "company": "Acme", "city": "上海", "skills": ["Agent"], "jd_status": "已抓取", "llm_review": {"status": "success"}},
            {"job_id": "n", "title": "B job", "classification": "暂不投", "score": 1000},
        ]

    def test_priority_sort_is_stable_and_does_not_mutate_inputs(self):
        jobs = self.jobs()
        before = copy.deepcopy(jobs)
        self.assertEqual([job["job_id"] for job in data.filter_jobs(jobs)], ["p1", "p2", "h", "n"])
        self.assertEqual(jobs, before)
        duplicate = {"job_id": "p1", "title": "A job", "classification": "重点投递", "score": 10}
        tied = [duplicate, dict(duplicate)]
        self.assertIs(data.filter_jobs(tied)[0], tied[0])

    def test_search_covers_title_company_skill_city_and_multiple_keywords(self):
        jobs = self.jobs()
        for keyword in ("Z job", "rag", "Python", "上海", "acme"):
            self.assertIn("h", [job["job_id"] for job in data.filter_jobs(jobs, search=keyword)])
        self.assertEqual([job["job_id"] for job in data.filter_jobs(jobs, search="rag acme 上海")], ["h"])
        self.assertEqual(data.filter_jobs(jobs, search="nonexistent"), [])

    def test_classification_jd_city_and_model_filters_use_saved_facts(self):
        jobs = self.jobs()
        self.assertEqual([job["job_id"] for job in data.filter_jobs(
            jobs, classification="重点投递", city="上海", jd_status="已抓取", model_status="success",
        )], ["p1"])
        self.assertEqual([job["job_id"] for job in data.filter_jobs(jobs, model_status="not_run")], ["p2"])
        self.assertEqual([job["job_id"] for job in data.filter_jobs(jobs, jd_status="待核实")], ["p2", "n"])
        self.assertEqual([job["job_id"] for job in data.filter_jobs(jobs, model_status="规则回退")], ["p2", "h"])

    def test_salary_sort_is_numeric_with_monthly_annual_daily_units(self):
        jobs = [{"job_id": name, "salary": salary} for name, salary in (
            ("high", "80-100K"), ("low", "5-8K"), ("mid", "17-25K·14薪"),
            ("annual", "24-36万/年"), ("daily", "400-600元/天"), ("unknown", "面议"),
        )]
        self.assertEqual([job["job_id"] for job in data.filter_jobs(jobs, sort_by="salary")],
                         ["low", "daily", "mid", "annual", "high", "unknown"])

    def test_all_sort_columns_keep_priority_ties_and_invalid_sort_is_explicit(self):
        jobs = self.jobs()
        self.assertEqual([job["job_id"] for job in data.filter_jobs(jobs, sort_by="title")], ["p1", "p2", "n", "h"])
        self.assertEqual([job["job_id"] for job in data.filter_jobs(jobs, sort_by="company")][-1], "p2")
        self.assertEqual(len(data.filter_jobs(jobs, sort_by="city")), 4)
        dimension_jobs = [{"job_id": "one", "jd_dimension_count": 1}, {"job_id": "six", "jd_dimension_count": 6}]
        self.assertEqual(data.filter_jobs(dimension_jobs, sort_by="dimensions", reverse=True)[0]["job_id"], "six")
        self.assertEqual(data.filter_jobs(jobs, reverse=True)[0]["job_id"], "n")
        with self.assertRaises(ValueError):
            data.filter_jobs(jobs, sort_by="unsupported")


class DetailTests(unittest.TestCase):
    def test_company_is_unknown_not_recruiter(self):
        detail = data.format_job_detail({"title": "role", "boss_name": "招聘王先生"})
        self.assertIn("公司：未知公司", detail)
        self.assertIn("招聘者：招聘王先生", detail)
        self.assertNotIn("公司：招聘王先生", detail)
        self.assertIn("未提供 JD 正文", detail)
        self.assertIn("未知（未提供状态）", detail)

    def test_detail_contains_all_evidence_sources_aliases_full_jd_and_link(self):
        job = {
            "job_id": "a", "title": "Role", "company": "实际公司", "salary": "17-25K",
            "tags": "1-3年 | 本科", "jd": "完整 JD 第一行\n完整 JD 第二行", "jd_dimension_count": 1,
            "jd_dimensions": {"方案设计": {"present": True, "evidence": ["原文职责证据"]}},
            "model_status": "success", "model_evidence": ["模型引用"],
            "verification": ["核实甲"], "verify": ["核实乙", "核实甲"],
            "gaps": ["差距甲"], "ability_gaps": ["差距乙"],
            "llm_review": {"verification_items": ["核实丙"], "ability_gaps": ["差距丙"]},
            "observed_in": [{"source_file": "a.json", "source_line_number": 7, "city": "上海", "keyword": "RAG"}],
            "job_link": "https://example.test/job/a",
        }
        before = copy.deepcopy(job)
        detail = data.format_job_detail(job)
        for value in (*data.DIMENSIONS, "原文职责证据", "模型引用", "核实甲", "核实乙", "核实丙",
                      "差距甲", "差距乙", "差距丙", "a.json", "源行：7", job["jd"], job["job_link"]):
            self.assertIn(value, detail)
        self.assertEqual(detail.count("核实甲"), 1)
        self.assertEqual(job, before)

    def test_detail_redacts_cookie_authorization_and_environment_keys(self):
        secrets = ["cookie-one", "cookie-two", "basic-secret", "environment-secret", "experimental-secret"]
        job = {"title": "role", "jd": (
            "Cookie: sid=cookie-one; uid=cookie-two\n"
            "Authorization: Basic basic-secret\n"
            "OPENAI_API_KEY=environment-secret\n"
            "experimental_bearer_token=experimental-secret\n"
            "正常 JD 职责"
        ), "llm_review": {"error": "Bearer bearer-secret"}}
        detail = data.format_job_detail(job)
        for secret in secrets + ["bearer-secret"]:
            self.assertNotIn(secret, detail)
        self.assertIn("正常 JD 职责", detail)
        self.assertIn("[REDACTED]", detail)

    def test_cookie_assignments_and_url_passwords_are_redacted_completely(self):
        detail = data.format_job_detail({"jd": (
            "Cookie=sid=cookie-first; uid=cookie-second\n"
            "Authorization=Basic encoded-secret\n"
            "https://name:password-secret@example.test/job\n"
            "X-Auth-Token=header-secret"
        )})
        for secret in ("cookie-first", "cookie-second", "encoded-secret", "password-secret", "header-secret"):
            self.assertNotIn(secret, detail)


class ExportTests(DataTestCase):
    def test_json_round_trips_all_evidence_and_redacts_nested_credentials(self):
        jobs = [{
            "job_id": "a", "title": "role", "classification": "海投池",
            "job_link": "https://example.test/job/a?access_token=query-secret&keep=1",
            "jd_dimensions": {"方案设计": {"present": True, "evidence": ["原文"]}},
            "verification": ["核实"], "Cookie": "sid=cookie-secret", "Set-Cookie": ["other-cookie-secret"],
            "Authorization": "Basic auth-secret", "credential": {"value": "credential-secret"},
            "headers": {"OPENAI_API_KEY": "api-secret", "experimental_bearer_token": "exp-secret"},
            "security_id": "security-secret", "model_error": "Authorization: Basic error-secret",
        }]
        before = copy.deepcopy(jobs)
        path = self.root / "export.json"
        self.assertEqual(data.export_jobs(path, jobs), path)
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
        self.assertEqual(value["total"], 1)
        self.assertEqual(value["jobs"][0]["classification"], "海投池")
        self.assertEqual(value["jobs"][0]["jd_dimensions"], jobs[0]["jd_dimensions"])
        self.assertIn("https://example.test/job/a?", value["jobs"][0]["job_link"])
        self.assertIn("keep=1", value["jobs"][0]["job_link"])
        for secret in ("query-secret", "cookie-secret", "other-cookie-secret", "auth-secret", "credential-secret",
                       "api-secret", "exp-secret", "security-secret", "error-secret"):
            self.assertNotIn(secret, raw)
        self.assertEqual(jobs, before)

    def test_csv_has_bom_parseable_nested_evidence_safe_cells_and_unknown_company(self):
        jobs = [{
            "job_id": "=HYPERLINK(\"bad\")", "title": " +SUM(1,1)", "boss_name": "招聘者不是公司",
            "skills": "@cmd", "salary": "-cmd", "city": "\t=bad", "classification": "重点投递",
            "job_link": "https://example.test/job/a", "jd_evidence": ["保留原文"],
            "jd_dimensions": {"方案设计": {"present": True, "evidence": ["证据"]}},
            "verification": ["需核实"], "custom": "\n=bad", "=evil_header": "ordinary",
            "Cookie": "sid=cookie-secret", "authorization": "Bearer secret-bearer",
        }]
        before = copy.deepcopy(jobs)
        path = data.export_jobs(self.root / "export.csv", jobs)
        self.assertTrue(path.read_bytes().startswith(codecs.BOM_UTF8))
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline="")))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        for field in ("job_id", "title", "skills", "salary", "city", "custom"):
            self.assertTrue(row[field].startswith("'"), field)
        self.assertIn("'=evil_header", row)
        self.assertEqual(row["company"], "未知公司")
        self.assertEqual(row["classification"], "重点投递")
        self.assertEqual(row["job_link"], jobs[0]["job_link"])
        self.assertEqual(json.loads(row["jd_evidence"]), ["保留原文"])
        self.assertEqual(json.loads(row["jd_dimensions"]), jobs[0]["jd_dimensions"])
        self.assertEqual(row["Cookie"], "[REDACTED]")
        self.assertNotIn("secret-bearer", path.read_text(encoding="utf-8-sig"))
        self.assertEqual(jobs, before)

    def test_unsupported_suffix_and_source_overwrite_are_rejected(self):
        path = self.root / "new-directory" / "bad.xlsx"
        with self.assertRaises(ValueError):
            data.export_jobs(path, [])
        self.assertFalse(path.parent.exists())
        source = self.index([{"job_id": "a"}])
        original = source.read_bytes()
        with self.assertRaises(ValueError):
            data.export_jobs(source, [])
        self.assertEqual(source.read_bytes(), original)

    def test_empty_exports_are_valid_and_existing_export_can_be_replaced(self):
        path = self.root / "selection.JSON"
        data.export_jobs(path, [{"job_id": "a"}])
        data.export_jobs(path, [])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"total": 0, "jobs": []})
        csv_path = data.export_jobs(self.root / "selection.CSV", [])
        self.assertIn("classification", next(csv.reader(io.StringIO(csv_path.read_text(encoding="utf-8-sig")))))

    def test_failed_export_does_not_leave_temporary_files_or_partial_output(self):
        output = self.root / "bad.json"
        with self.assertRaises(ValueError):
            data.export_jobs(output, [{"invalid_number": float("nan")}])
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
