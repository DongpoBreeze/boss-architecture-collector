"""Company capture uses synthetic API/DOM data and never contacts a browser."""

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import collection_runtime as runtime
from test_collection_runtime import UPSTREAM_SOURCE


# Mirror the known upstream hook interfaces and DOM template. The only waits
# and pagination come from the pre-existing fake-time fixture; no browser,
# network client or credential handling exists in this source.
COMPANY_HOOKS = textwrap.dedent(r'''
    EXTRACT_LIST_JS = """
    (function(){
        var results = [];
        var cards = document.querySelectorAll('li.job-card-box');
        for (var i = 0; i < cards.length; i++) {
            var card = cards[i];
            var nameEl = card.querySelector('.job-name');
            var salaryEl = card.querySelector('.job-salary');
            var locEl = card.querySelector('.company-location');
            var tagEls = card.querySelectorAll('.tag-list li');
            var bossEl = card.querySelector('.boss-name');
            var bossLink = card.querySelector('.boss-info');
            var tags = [];
            for (var j = 0; j < tagEls.length; j++) tags.push(tagEls[j].innerText.trim());
            var jobLink = nameEl ? (nameEl.getAttribute('href') || '') : '';
            if (jobLink && jobLink.charAt(0) === '/') jobLink = 'https://www.zhipin.com' + jobLink;
            var cLink = bossLink ? (bossLink.getAttribute('href') || '') : '';
            if (cLink && cLink.charAt(0) === '/') cLink = 'https://www.zhipin.com' + cLink;
            var t = nameEl ? nameEl.innerText.trim() : '';
            if (t) results.push({
                title: t,
                salary: salaryEl ? salaryEl.innerText.trim() : '',
                salary_source: 'dom_untrusted',
                location: locEl ? locEl.innerText.trim() : '',
                tags: tags.join(' | '),
                boss_name: bossEl ? bossEl.innerText.trim() : '',
                job_link: jobLink,
                company_link: cLink
            });
        }
        return JSON.stringify(results);
    })()
    """

    def map_api_job(raw):
        if not isinstance(raw, dict):
            return None
        if raw.get("raise_mapping_error"):
            raise ValueError("synthetic mapper failure")
        return {
            "job_id": raw.get("encryptJobId") or "",
            "title": raw.get("jobName") or "",
            "salary": raw.get("salaryDesc") or "",
            "salary_source": "api" if raw.get("salaryDesc") else "api_empty",
            "tags": " | ".join(t for t in (raw.get("jobExperience") or "", raw.get("jobDegree") or "") if t and t != "不限"),
            "boss_name": raw.get("brandName") or "",
            "boss_title": raw.get("bossTitle") or "",
            "company_scale": raw.get("brandScaleName") or "",
            "company_stage": raw.get("brandStageName") or "",
            "company_industry": raw.get("brandIndustry") or "",
            "encrypt_brand_id": raw.get("encryptBrandId") or "",
            "company_link": raw.get("company_link") or "",
            "job_link": "https://www.zhipin.com/job_detail/" + str(raw.get("encryptJobId") or "") + ".html",
        }

    def map_api_jobs(data):
        if not isinstance(data, dict):
            return []
        jobs = (data.get("zpData") or {}).get("jobList")
        if not isinstance(jobs, list):
            return []
        return [row for row in (map_api_job(raw) for raw in jobs) if row]

    def merge_unique(existing, incoming, key="job_id", new_overrides=False):
        if new_overrides:
            by_key = {}
            for item in existing:
                if isinstance(item, dict) and item.get(key):
                    by_key[item.get(key)] = item
            for item in incoming:
                if isinstance(item, dict) and item.get(key):
                    by_key[item.get(key)] = item
            return list(by_key.values())
        seen = {item.get(key, "") for item in existing if isinstance(item, dict)}
        merged = list(existing)
        for item in incoming:
            if isinstance(item, dict) and item.get(key, "") not in seen:
                seen.add(item.get(key, ""))
                merged.append(item)
        return merged

    def flush_jobs(path, meta, jobs):
        old = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {"jobs": []}
        merged = merge_unique(old["jobs"], jobs)
        Path(path).write_text(json.dumps({**meta, "jobs": merged, "total": len(merged)}, ensure_ascii=False), encoding="utf-8")

    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("--payload", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--merge")
        parser.add_argument("--pages", type=int, default=2)
        parser.add_argument("--gate", action="store_true")
        args, _remaining = parser.parse_known_args()
        if args.gate:
            print("请完成安全验证；未执行岗位映射")
            return 1
        scrape_list("AI 架构", "上海", args.pages, {}, args.output)
        jobs = map_api_jobs(json.loads(Path(args.payload).read_text(encoding="utf-8")))
        flush_jobs(args.output, {"events": events, "argv": sys.argv[1:]}, jobs)
        if args.merge:
            previous = json.loads(Path(args.merge).read_text(encoding="utf-8"))["jobs"]
            flush_jobs(args.output, {"events": events, "argv": sys.argv[1:]}, merge_unique(previous, jobs))
        return 0
''')
COMPANY_SOURCE = UPSTREAM_SOURCE.replace("def main():", "def pagination_fixture_main():") + COMPANY_HOOKS


class CompanyCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.scraper = self.base / "上游 源码"
        self.script = self.scraper / "scripts" / "boss_cdp_raw.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text(COMPANY_SOURCE, encoding="utf-8")

    def load(self):
        return runtime.load_upstream(self.scraper, 0.25, 0.5)

    def test_api_company_uses_brand_not_recruiter_and_retains_original_fields(self):
        module = self.load()
        raw = {"encryptJobId": "job-a", "jobName": "架构顾问", "salaryDesc": "30-50K",
               "brandName": "  上海中文科技有限公司  ", "bossName": "王女士", "bossTitle": "招聘经理"}
        before = copy.deepcopy(raw)
        row = module.map_api_jobs({"zpData": {"jobList": [raw]}})[0]
        self.assertEqual(row["company"], "上海中文科技有限公司")
        self.assertNotEqual(row["company"], raw["bossName"])
        self.assertEqual(row["boss_name"], raw["brandName"])
        self.assertEqual(row["boss_title"], "招聘经理")
        self.assertEqual((row["title"], row["salary"], row["salary_source"]), ("架构顾问", "30-50K", "api"))
        self.assertEqual(raw, before)

    def test_api_explicit_company_aliases_are_text_only_and_never_use_boss_name(self):
        module = self.load()
        for field in ("brandName", "brand_name", "companyName", "company_name", "company"):
            with self.subTest(field=field):
                self.assertEqual(module.map_api_job({field: " 北京公司\n研究院 ", "bossName": "李先生"})["company"],
                                 "北京公司 研究院")
        self.assertEqual(module.map_api_job({"company": {"name": "明确的公司名称"}})["company"], "明确的公司名称")
        self.assertEqual(module.map_api_job({"brandName": "优先品牌", "companyName": "次选公司"})["company"], "优先品牌")
        for value in (None, "", "  ", [], ["不能猜测"], {"name": "不能解析brandName对象"}, 42, True, "无效\x00名称"):
            for field in ("brandName", "companyName"):
                with self.subTest(field=field, value=value):
                    row = module.map_api_job({field: value, "bossName": "招聘者姓名", "boss_name": "另一个招聘者"})
                    self.assertEqual(row["company"], "")
                    self.assertEqual(row["salary_source"], "api_empty")
        for raw in (None, [], "not-a-record", 7):
            self.assertIsNone(module.map_api_job(raw))
        self.assertEqual(module.map_api_jobs({"zpData": {"jobList": []}}), [])
        self.assertEqual(module.map_api_job({"bossName": "仅招聘者"})["company"], "")
        with self.assertRaisesRegex(ValueError, "synthetic mapper failure"):
            module.map_api_job({"raise_mapping_error": True, "brandName": "不会绕过错误"})

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the synthetic DOM")
    def test_dom_company_is_read_from_company_node_instead_of_recruiter(self):
        module = self.load()
        cards = [
            {"company": " 上海公司\n研发中心 ", "recruiter": "王女士"},
            {"recruiter": "只有招聘者"},
            *({"company": value, "recruiter": "不能拿来填公司"}
              for value in (None, "", 7, True, ["伪装名称"], {"name": "伪装公司"}, "无效\x00名称")),
        ]
        # A minimal DOM substitute executes the adapted upstream JavaScript;
        # unknown selectors and all network operations are rejected.
        bootstrap = r'''
            const fixture = JSON.parse(require('fs').readFileSync(0, 'utf8'));
            global.fetch = () => { throw new Error('network is forbidden'); };
            const document = { querySelectorAll(selector) {
                if (selector !== 'li.job-card-box') throw new Error('unexpected card selector');
                return fixture.cards.map((row, index) => ({
                    querySelectorAll(selector) {
                        if (selector !== '.tag-list li') throw new Error('unexpected list selector');
                        return [];
                    },
                    querySelector(selector) {
                        if (selector === '.job-name') return { innerText: '岗位' + index,
                            getAttribute: () => '/job_detail/job' + index + '.html' };
                        if (selector === '.company-name') return Object.hasOwn(row, 'company') ? { innerText: row.company } : null;
                        if (selector === '.boss-name') return { innerText: row.recruiter };
                        if (selector === '.job-salary') return { innerText: '30-50K' };
                        if (selector === '.company-location') return { innerText: '上海' };
                        if (selector === '.boss-info') return { getAttribute: () => '/gongsi/brand' + index + '.html' };
                        throw new Error('unexpected selector: ' + selector);
                    }
                }));
            }};
            process.stdout.write(eval(fixture.js));
        '''
        result = subprocess.run([shutil.which("node"), "-e", bootstrap],
                                input=json.dumps({"js": module.EXTRACT_LIST_JS, "cards": cards}, ensure_ascii=False),
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), len(cards))
        self.assertEqual(rows[0]["company"], "上海公司 研发中心")
        self.assertEqual(rows[0]["boss_name"], "王女士")
        self.assertEqual(rows[0]["salary_source"], "dom_untrusted")
        self.assertTrue(all(row["company"] == "" for row in rows[1:]))
        self.assertEqual(rows[1]["boss_name"], "只有招聘者")
        self.assertEqual(rows[0]["job_link"], "https://www.zhipin.com/job_detail/job0.html")

    def test_merge_enriches_only_missing_company_and_preserves_original_precedence_and_inputs(self):
        module = self.load()
        for new_overrides in (False, True):
            old = [{"job_id": "a", "title": "原始标题", "company": ""},
                   {"job_id": "b", "company": "已有公司"},
                   {"job_id": "c", "boss_name": "仅旧招聘者"}]
            new = [{"job_id": "a", "title": "新标题", "company": "新补公司"},
                   {"job_id": "b", "company": ""},
                   {"job_id": "d", "company": "新增岗位公司"}]
            before = copy.deepcopy((old, new))
            result = module.merge_unique(old, new, new_overrides=new_overrides)
            self.assertEqual([row["job_id"] for row in result], ["a", "b", "c", "d"])
            self.assertEqual(result[0]["company"], "新补公司")
            self.assertEqual(result[0]["title"], "新标题" if new_overrides else "原始标题")
            self.assertEqual(result[1]["company"], "已有公司")
            self.assertNotIn("company", result[2])
            self.assertEqual((old, new), before)
        for new_overrides, expected in ((False, "旧公司"), (True, "新公司")):
            result = module.merge_unique([{"job_id": "a", "company": "旧公司"}],
                                         [{"job_id": "a", "company": "新公司"}], new_overrides=new_overrides)
            self.assertEqual(result[0]["company"], expected)
        result = module.merge_unique([{"other_id": "a"}], [{"other_id": "a", "company": "按指定身份补全"}], key="other_id")
        self.assertEqual(result, [{"other_id": "a", "company": "按指定身份补全"}])
        result = module.merge_unique([{"job_id": "", "boss_name": "空ID招聘者"}], [{"job_id": "", "company": "不能关联"}])
        self.assertNotIn("company", result[0])

    def test_copied_isolated_runtime_cli_writes_company_and_preserves_it_across_resume(self):
        copied_runtime = self.base / "独立 执行目录" / "collection_runtime.py"
        copied_runtime.parent.mkdir()
        shutil.copyfile(runtime.__file__, copied_runtime)
        payload_path, output, checkpoint = (self.base / name for name in ("被动 响应.json", "原始 输出.json", "恢复 检查点.json"))
        old_row = {"job_id": "same", "title": "旧标题", "boss_name": "旧字段原样保留"}
        output.write_text(json.dumps({"jobs": [old_row]}, ensure_ascii=False), encoding="utf-8")
        checkpoint.write_text(json.dumps({"jobs": [{"job_id": "checkpoint-only", "company": "检查点公司"}]}, ensure_ascii=False), encoding="utf-8")
        source_bytes, source_mtime = self.script.read_bytes(), self.script.stat().st_mtime_ns
        checkpoint_bytes = checkpoint.read_bytes()
        raw_rows = [{"encryptJobId": "same", "jobName": "新标题", "brandName": "新采集的中文公司", "bossName": "孙先生"},
                    {"encryptJobId": "new", "brandName": "另一家公司", "bossName": "招聘者乙"}]
        for iteration in range(2):
            if iteration:
                raw_rows[0].pop("brandName")  # An incomplete retry cannot erase the first company.
            payload_path.write_text(json.dumps({"zpData": {"jobList": raw_rows}}, ensure_ascii=False), encoding="utf-8")
            upstream_args = ["--payload", str(payload_path), "--output", str(output), "--merge", str(checkpoint),
                             "--pages", "2", "--no-detail", "--city", "上海", "--cdp-port", "9333"]
            result = subprocess.run([sys.executable, "-I", str(copied_runtime), "--scraper-root", str(self.scraper),
                                     "--page-wait-min", "0.25", "--page-wait-max", "0.5", "--", *upstream_args],
                                    cwd=self.base, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                                    capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            saved = json.loads(output.read_text(encoding="utf-8"))
            rows = {row["job_id"]: row for row in saved["jobs"]}
            self.assertEqual(set(rows), {"same", "new", "checkpoint-only"})
            self.assertEqual(rows["same"], {**old_row, "company": "新采集的中文公司"})
            self.assertEqual(rows["new"]["company"], "另一家公司")
            self.assertEqual(rows["checkpoint-only"]["company"], "检查点公司")
            self.assertEqual(saved["total"], 3)
            self.assertEqual(saved["events"], [2, 0.5, 0.25, 0.5])
            self.assertEqual(saved["argv"], upstream_args)
        self.assertEqual((self.script.read_bytes(), self.script.stat().st_mtime_ns), (source_bytes, source_mtime))
        self.assertEqual(checkpoint.read_bytes(), checkpoint_bytes)
        self.assertFalse(list(self.scraper.rglob("*.pyc")))
        self.assertFalse(list(self.scraper.rglob("__pycache__")))

    def test_gate_exit_remains_nonzero_without_creating_output(self):
        payload, output = self.base / "ignored.json", self.base / "must-not-exist.json"
        result = subprocess.run([sys.executable, "-I", str(Path(runtime.__file__).resolve()),
                                 "--scraper-root", str(self.scraper), "--", "--payload", str(payload),
                                 "--output", str(output), "--gate"], cwd=self.base,
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("请完成安全验证", result.stdout)
        self.assertFalse(output.exists())
        self.assertEqual(self.script.read_text(encoding="utf-8"), COMPANY_SOURCE)

    def test_unknown_company_dom_fails_before_upstream_initialization(self):
        initialized = self.base / "must-not-initialize"
        source = (f"from pathlib import Path\nPath({str(initialized)!r}).touch()\n"
                  + COMPANY_SOURCE.replace("var bossEl = card.querySelector('.boss-name');", "var bossEl = unknownExtractor();"))
        self.script.write_text(source, encoding="utf-8")
        with self.assertRaisesRegex(runtime.RuntimeCompatibilityError, "公司名称 DOM"):
            self.load()
        self.assertFalse(initialized.exists())
        self.assertEqual(self.script.read_text(encoding="utf-8"), source)

    def test_unknown_mapping_or_merge_hook_fails_clearly_without_guessing(self):
        for original, changed in (("def map_api_job(raw):", "def map_api_job(raw, unsupported_required_argument):"),
                                  ('def merge_unique(existing, incoming, key="job_id", new_overrides=False):',
                                   'def merge_unique(existing, incoming):')):
            with self.subTest(changed=changed):
                self.script.write_text(COMPANY_SOURCE.replace(original, changed), encoding="utf-8")
                with self.assertRaisesRegex(runtime.RuntimeCompatibilityError, "不支持的上游"):
                    self.load()


if __name__ == "__main__":
    unittest.main()
