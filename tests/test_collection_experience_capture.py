"""Offline experience capture checks use synthetic API, DOM and CLI inputs."""

import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import collection_runtime as runtime
from test_collection_company_capture import COMPANY_SOURCE


class ExperienceCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.scraper = self.base / "合成 上游"
        self.script = self.scraper / "scripts" / "boss_cdp_raw.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text(COMPANY_SOURCE, encoding="utf-8")

    def load(self):
        return runtime.load_upstream(self.scraper, 0.25, 0.5)

    def test_explicit_api_experience_keeps_original_label_and_tags_independent(self):
        module = self.load()
        for label in ("1-3年", "3-5年", "5-10年", "10年以上", "10年+", "1年以内", "经验不限", "不限",
                      "在校/应届", "在校生", "应届生", "无经验", " 3 - 5 年 "):
            raw = {"encryptJobId": "a", "brandName": "中文公司", "jobExperience": label, "jobDegree": "本科"}
            before = copy.deepcopy(raw)
            row = module.map_api_jobs({"zpData": {"jobList": [raw]}})[0]
            with self.subTest(label=label):
                self.assertEqual(row["experience"], label.strip())
                self.assertEqual(row["tags"], " | ".join(value for value in (label, "本科") if value != "不限"))
                self.assertEqual(row["company"], "中文公司")
                self.assertEqual(raw, before)

    def test_api_aliases_do_not_infer_from_filters_tags_or_job_description(self):
        module = self.load()
        for field in ("jobExperience", "job_experience", "experience"):
            with self.subTest(field=field):
                self.assertEqual(module.map_api_job({field: " 5-10年 "})["experience"], "5-10年")
        self.assertEqual(module.map_api_job({"jobExperience": "3-5年", "job_experience": "5-10年",
                                            "experience": "10年+"})["experience"], "3-5年")
        self.assertEqual(module.map_api_job({"jobExperience": None, "job_experience": "3-5年"})["experience"], "3-5年")
        unknown = {"filters": {"experience": "105"}, "experience_code": "105", "tags": "3-5年 | 本科",
                   "tags_list": ["3-5年"], "jd": "任职要求：3-5年经验", "bossName": "招聘者"}
        self.assertEqual(module.map_api_job(unknown)["experience"], "")
        self.assertEqual(module.map_api_job({"jobDegree": "不限"})["experience"], "")

    def test_api_missing_untrusted_types_and_numeric_codes_are_unknown_without_crashing_tags(self):
        module = self.load()
        for field in ("jobExperience", "job_experience", "experience"):
            for value in (None, "", " \n ", 105, "105", " 105 ", "１０５", True, [], ["3-5年"], {"name": "3-5年"}):
                raw = {field: value, "jobDegree": "本科"}
                before = copy.deepcopy(raw)
                with self.subTest(field=field, value=value):
                    row = module.map_api_job(raw)
                    self.assertEqual(row["experience"], "")
                    self.assertEqual(raw, before)
                    if field == "jobExperience" and isinstance(value, str) and value:
                        self.assertEqual(row["tags"], value + " | 本科")
                    else:
                        self.assertEqual(row["tags"], "本科")
        self.assertIsNone(module.map_api_job(None))
        self.assertEqual(module.map_api_job({})["experience"], "")

    @unittest.skipUnless(shutil.which("node"), "Node is required for synthetic DOM execution")
    def test_dom_full_labels_and_unambiguous_delimiters_preserve_tags_and_raw_label(self):
        module = self.load()
        labels = ["1-3年", "3-5年", "5-10年", "10年以上", "10年+", "10年＋", "1年以内", "经验不限",
                  "在校/应届", "在校生", "应届生", "无经验", "在校生/应届生", "在校生/应届生/无经验",
                  "3–5年", "3～5年", "3至5年", "5年以下", "3 - 5 年"]
        cases = [{"tags": ["本科", f" {label} ", "Python"], "expected": label} for label in labels]
        cases.extend({"tags": [f"本科{separator} 3-5年{separator}Python", "3-5年"], "expected": "3-5年"}
                     for separator in ("|", "｜", ",", "，", ";", "；", "、", "\n", "\r\n"))
        cases.extend([
            {"tags": ["不限", "本科", "Python"], "expected": ""},
            {"tags": ["学历不限", "硕士", "开发经验丰富"], "expected": ""},
            {"tags": ["要求3-5年工作经验", "熟悉Python且有3-5年经验"], "expected": ""},
            {"tags": ["3-5年开发经验", "5年以上经验优先", "经验：3-5年"], "expected": ""},
            {"tags": ["Java/Python", "106", "105", "3-5", "3年"], "expected": ""},
            {"tags": [None, 105, True, {"name": "3-5年"}, ["3-5年"]], "expected": ""},
            {"tags": ["3-5年", "5-10年"], "expected": ""},
            {"tags": ["经验不限 | 3-5年"], "expected": ""},
            {"tags": ["在校生", "应届生"], "expected": ""},
            {"tags": ["3-5年", "3 - 5 年"], "expected": ""},
            {"tags": [], "expected": ""},
        ])
        bootstrap = r'''
            const fixture = JSON.parse(require('fs').readFileSync(0, 'utf8'));
            global.fetch = () => { throw new Error('network is forbidden'); };
            const document = { querySelectorAll(selector) {
                if (selector !== 'li.job-card-box') throw new Error('unexpected selector');
                return fixture.cases.map((row, index) => ({
                    querySelectorAll(selector) {
                        if (selector !== '.tag-list li') throw new Error('unexpected tag selector');
                        return row.tags.map(value => ({ innerText: value }));
                    },
                    querySelector(selector) {
                        if (selector === '.job-name') return { innerText: '岗位要求3-5年', getAttribute: () => '/job_detail/' + index + '.html' };
                        if (selector === '.company-name') return { innerText: '中文公司' };
                        if (selector === '.boss-name') return { innerText: '招聘者' };
                        if (selector === '.boss-info') return { getAttribute: () => '/gongsi/brand.html' };
                        if (selector === '.job-salary') return { innerText: '20-30K' };
                        if (selector === '.company-location') return { innerText: '上海' };
                        throw new Error('unexpected selector ' + selector);
                    }
                }));
            }};
            process.stdout.write(eval(fixture.js));
        '''
        completed = subprocess.run([shutil.which("node"), "-e", bootstrap],
                                   input=json.dumps({"js": module.EXTRACT_LIST_JS, "cases": cases}, ensure_ascii=False),
                                   capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        rows = json.loads(completed.stdout)
        self.assertEqual(len(rows), len(cases))
        for case, row in zip(cases, rows):
            with self.subTest(tags=case["tags"]):
                self.assertEqual(row["experience"], case["expected"])
                self.assertEqual(row["tags"], " | ".join(value.strip() for value in case["tags"] if isinstance(value, str)))
                self.assertEqual((row["company"], row["boss_name"], row["salary_source"]), ("中文公司", "招聘者", "dom_untrusted"))

    def test_same_job_merge_fills_both_fields_without_overriding_selected_nonempty_values(self):
        module = self.load()
        for overrides in (False, True):
            old = [{"job_id": "a", "title": "旧标题", "company": "", "experience": ""},
                   {"job_id": "b", "company": "已有公司", "experience": "5-10年"},
                   {"job_id": "c", "experience": "105"},
                   {"job_id": "unknown", "tags": "3-5年"}]
            new = [{"job_id": "a", "title": "新标题", "company": "补全公司", "experience": "3-5年"},
                   {"job_id": "b", "company": "", "experience": ""},
                   {"job_id": "c", "experience": "经验不限"},
                   {"job_id": "d", "experience": "在校/应届"}]
            before = copy.deepcopy((old, new))
            rows = module.merge_unique(old, new, new_overrides=overrides)
            self.assertEqual([row["job_id"] for row in rows], ["a", "b", "c", "unknown", "d"])
            self.assertEqual(rows[0], {"job_id": "a", "title": "新标题" if overrides else "旧标题",
                                       "company": "补全公司", "experience": "3-5年"})
            self.assertEqual((rows[1]["company"], rows[1]["experience"]), ("已有公司", "5-10年"))
            self.assertEqual(rows[2]["experience"], "经验不限")
            self.assertNotIn("experience", rows[3])
            self.assertEqual((old, new), before)
        for overrides, expected in ((False, "10年+"), (True, "3-5年")):
            rows = module.merge_unique([{"job_id": "a", "experience": "10年+"}],
                                        [{"job_id": "a", "experience": "3-5年"}], new_overrides=overrides)
            self.assertEqual(rows[0]["experience"], expected)
        rows = module.merge_unique([{"other": "a"}], [{"other": "a", "experience": "1年以内"}], key="other")
        self.assertEqual(rows, [{"other": "a", "experience": "1年以内"}])
        rows = module.merge_unique([{"job_id": ""}], [{"job_id": "", "experience": "3-5年"}])
        self.assertEqual(rows, [{"job_id": ""}])

    def test_copied_isolated_cli_publishes_experience_and_resume_cannot_erase_it(self):
        copied = self.base / "独立 runtime" / "collection_runtime.py"
        copied.parent.mkdir()
        shutil.copyfile(runtime.__file__, copied)
        payload, output, checkpoint = (self.base / name for name in ("响应.json", "raw.json", "checkpoint.json"))
        old = {"job_id": "same", "title": "旧标题", "company": "已有公司"}
        output.write_text(json.dumps({"jobs": [old]}, ensure_ascii=False), encoding="utf-8")
        checkpoint.write_text(json.dumps({"jobs": [{"job_id": "checkpoint-only", "experience": "在校/应届"}]},
                                         ensure_ascii=False), encoding="utf-8")
        original, modified, checkpoint_before = self.script.read_bytes(), self.script.stat().st_mtime_ns, checkpoint.read_bytes()
        for experience in ("5-10年", "", "105"):
            raw = [{"encryptJobId": "same", "jobExperience": experience, "jobDegree": "本科"},
                   {"encryptJobId": "new-unlimited", "jobExperience": "不限", "jobDegree": "不限"},
                   {"encryptJobId": "numeric-code", "jobExperience": 105, "jobDegree": "本科"}]
            payload.write_text(json.dumps({"zpData": {"jobList": raw}}, ensure_ascii=False), encoding="utf-8")
            arguments = ["--payload", str(payload), "--output", str(output), "--merge", str(checkpoint),
                         "--pages", "2", "--experience", "106", "--no-detail"]
            completed = subprocess.run([sys.executable, "-I", str(copied), "--scraper-root", str(self.scraper),
                                         "--page-wait-min", "0.25", "--page-wait-max", "0.5", "--", *arguments],
                                        cwd=self.base, capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            saved = json.loads(output.read_text(encoding="utf-8"))
            rows = {row["job_id"]: row for row in saved["jobs"]}
            self.assertEqual(rows["same"], {**old, "experience": "5-10年"})
            self.assertEqual(rows["new-unlimited"]["experience"], "不限")
            self.assertEqual(rows["numeric-code"]["experience"], "")
            self.assertEqual(rows["checkpoint-only"]["experience"], "在校/应届")
            self.assertEqual(saved["total"], 4)
            self.assertEqual(saved["argv"], arguments)
            self.assertEqual(saved["events"], [2, 0.5, 0.25, 0.5])
        self.assertEqual((self.script.read_bytes(), self.script.stat().st_mtime_ns), (original, modified))
        self.assertEqual(checkpoint.read_bytes(), checkpoint_before)
        self.assertFalse(list(self.scraper.rglob("*.pyc")))
        self.assertFalse(list(self.scraper.rglob("__pycache__")))

    def test_unknown_dom_tag_loop_fails_before_module_initialization(self):
        marker = self.base / "must-not-initialize"
        source = (f"from pathlib import Path\nPath({str(marker)!r}).touch()\n" + COMPANY_SOURCE.replace(
            "for (var j = 0; j < tagEls.length; j++) tags.push(tagEls[j].innerText.trim());",
            "tags = unknownExperienceExtraction();"))
        self.script.write_text(source, encoding="utf-8")
        with self.assertRaisesRegex(runtime.RuntimeCompatibilityError, "招聘经验 DOM"):
            self.load()
        self.assertFalse(marker.exists())
        self.assertEqual(self.script.read_text(encoding="utf-8"), source)


if __name__ == "__main__":
    unittest.main()
