"""Offline list-policy regressions, using literal non-personal job-list fields."""

import copy
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from process_architecture import (  # noqa: E402
    DIMENSIONS,
    SCREENING_VERSION,
    merge_model_review,
    score_job,
    text,
    write_indexes,
)
from screening_policy import LIST_QUEUE_LABELS  # noqa: E402


class ShortlistQueuePolicyTests(unittest.TestCase):
    def assertQueue(self, job, expected):
        original = copy.deepcopy(job)
        info = score_job(job)
        self.assertEqual(job, original, "scoring must not modify source or cached input")
        self.assertEqual(info, score_job(job), "queue and evidence must be deterministic")
        self.assertEqual(info["list_queue"], expected, job)
        self.assertEqual(info["list_pass"], expected != "excluded")
        self.assertEqual(info["list_priority"], {
            "relevant": "优先补JD", "verify": "低优先待JD", "excluded": "暂不优先",
        }[expected])
        self.assertIsInstance(info["list_queue_reason"], str)
        self.assertTrue(info["list_queue_reason"].strip())
        self.assertEqual(info["screening_version"], SCREENING_VERSION)
        for evidence in info["list_match_evidence"] + info["list_exclusion_evidence"]:
            self.assertEqual(set(evidence), {"family", "field", "quote"})
            self.assertTrue(evidence["quote"].strip())
            self.assertIn(evidence["quote"], text(job[evidence["field"]]))
        return info

    def test_queue_labels_and_compatibility_fields_have_exactly_three_values(self):
        self.assertEqual(LIST_QUEUE_LABELS, {
            "relevant": "明确相关", "verify": "待核实", "excluded": "剔除",
        })
        jobs = [{"title": "AI架构师"}, {"title": "系统架构师"}, {"title": "Java"}]
        queues = [self.assertQueue(job, queue)["list_queue"]
                  for job, queue in zip(jobs, LIST_QUEUE_LABELS)]
        self.assertEqual(Counter(queues), Counter(LIST_QUEUE_LABELS.keys()))

    def test_real_architecture_and_product_misses_recall_even_without_skills(self):
        cases = [
            {"title": "AI架构师", "skills": "", "salary": "25-50K"},
            {"title": "AI大模型架构师", "skills": "", "salary": "35-60K"},
            {"title": "智能应用架构师", "skills": "", "salary": "45-60K·15薪"},
            {"title": "产品架构师", "salary": "15-30K",
             "skills": "B端产品 | 用户/功能产品 | ERP产品/软件产品"},
            {"title": "AI产品助理", "salary": "15-30K", "skills": "B端产品 | 语义类AI | 视觉类AI"},
            {"title": "大模型产品助理", "skills": "", "salary": "12-20K·13薪"},
        ]
        for job in cases:
            with self.subTest(title=job["title"]):
                info = self.assertQueue(job, "relevant")
                self.assertTrue(info["list_match_evidence"])
                if not job["skills"]:
                    self.assertTrue(any("技能字段缺失" in item for item in info["list_verification"]))

    def test_real_integration_success_enablement_and_knowledge_misses_are_retained(self):
        cases = [
            ({"title": "系统集成工程师（应届）", "skills": "", "salary": "13-20K"}, "verify"),
            ({"title": "SaaS客户成功（交付&客户关系方向）", "skills": "", "salary": "12-20K"}, "verify"),
            ({"title": "AI Adoption Consultant", "skills": "AI项目 | 人工智能 | 制造业"}, "relevant"),
            ({"title": "AI提效专家", "skills": "预估预测 | AI机器人", "salary": "17-25K"}, "relevant"),
            ({"title": "知识管理专家/AI知识管理经理—大科技集团", "skills": ""}, "relevant"),
            ({"title": "知识管理专家", "skills": ""}, "verify"),
        ]
        for job, queue in cases:
            with self.subTest(title=job["title"]):
                self.assertTrue(self.assertQueue(job, queue)["list_match_evidence"])

    def test_reasonable_english_synonyms_follow_the_same_evidence_contract(self):
        for title in ("AI Architect", "LLM Architect", "Intelligent Application Architect",
                      "AI Solutions Architect", "AI Product Assistant", "AI Enablement Consultant",
                      "AI Productivity Specialist", "AI Knowledge Manager", "Forward Deployed Engineer"):
            with self.subTest(title=title):
                self.assertQueue({"title": title}, "relevant")
        for title in ("System Architect", "System Integration Engineer", "Customer Success Manager"):
            with self.subTest(title=title):
                self.assertQueue({"title": title}, "verify")

    def test_generic_roles_and_weak_ai_algorithm_infra_clues_are_verify_only(self):
        cases = [
            {"title": "系统架构师", "skills": ""},
            {"title": "产品架构师", "skills": ""},
            {"title": "项目经理", "skills": "AI | K8S"},
            {"title": "AI工程师", "skills": ""},
            {"title": "算法工程师", "skills": "Agent | RAG | PyTorch"},
            {"title": "AI Infra工程师", "skills": "Agent | K8S"},
            {"title": "AI平台产品经理", "skills": "芯片 | GPU | CUDA"},
            {"title": "AI架构师", "skills": "MVC开发 | MVP | 平台架构"},
        ]
        for job in cases:
            with self.subTest(title=job["title"]):
                info = self.assertQueue(job, "verify")
                self.assertFalse(info["list_excluded"])

    def test_real_sales_mistakes_are_not_exempted_by_solution_agent_or_ai_nouns(self):
        cases = [
            {"title": "网络解决方案销售—算力服务商方向（上海）", "salary": "15-25K",
             "skills": "电话销售经验 | 面销/陌拜经验 | 电话销售 | 面销/陌拜 | 会展销售 | 互联网/科技"},
            {"title": "AI Agent大客户销售 接受小白 完善培训体系", "salary": "13-18K",
             "skills": "渠道/机构客户开发（销售方向） | 商务谈判能力 | 个人客户开发（销售方向） | 渠道开发能力"},
            {"title": "AI智能体招商销售", "salary": "20-30K", "skills": "Agent | 解决方案 | 商务谈判"},
            {"title": "AI Agent Sales Executive", "skills": "cold calling | customer acquisition"},
        ]
        for job in cases:
            with self.subTest(title=job["title"]):
                info = self.assertQueue(job, "excluded")
                self.assertTrue(any("纯销售" in reason for reason in info["list_exclusion_reasons"]))
                self.assertFalse(info["list_pure_development"], "client development is not software development")
        for title in ("AI商务顾问", "AI Agent销售", "AI Agent解决方案销售"):
            with self.subTest(title=title):
                self.assertQueue({"title": title, "skills": "Agent | 解决方案"}, "verify")

    def test_real_personal_technical_work_retains_sales_presales_and_fde(self):
        for field in ("skills", "skill_tags", "job_labels"):
            with self.subTest(field=field):
                job = {"title": "AI Agent大客户销售", "skills": "客户开发 | 商务谈判",
                       field: "客户开发；负责方案设计、系统集成与客户PoC交付"}
                self.assertQueue(job, "relevant")
        for title in ("技术销售工程师", "AI售前工程师", "FDE"):
            with self.subTest(title=title):
                info = score_job({"title": title, "skills": "销售目标 | 签约 | 回款"})
                self.assertTrue(info["list_pass"])
                self.assertFalse(info["list_excluded"])

    def test_negated_and_other_team_technical_work_cannot_reopen_sales(self):
        cases = [
            ("AI Agent销售", "客户开发 | 不负责方案设计、系统集成和PoC交付"),
            ("网络解决方案销售", "电话销售；方案设计由技术团队负责；系统集成由研发同事完成"),
            ("AI解决方案销售", "客户开发；方案设计和PoC交付由售前工程师负责"),
            ("AI Agent Sales Executive", "customer acquisition; not responsible for solution design or PoC delivery"),
            ("AI Agent Sales Executive", "customer acquisition; solution design and PoC delivery handled by the engineering team"),
            ("AI Sales", "customer acquisition; PoC delivery handled by presales engineers"),
        ]
        for title, skills in cases:
            with self.subTest(title=title, skills=skills):
                info = self.assertQueue({"title": title, "skills": skills}, "excluded")
                self.assertTrue(any("纯销售" in reason for reason in info["list_exclusion_reasons"]))

    def test_language_only_and_architecture_experience_do_not_recall_pure_developers(self):
        for title, skills in (
            ("Java", "Java | SpringCloud | Spring | MyBatis | MySQL | Redis"),
            ("Python", "Python | Django | Flask | MySQL"), ("Golang", ""),
            ("软件开发工程师", "Python | 架构设计经验 | Linux开发/部署经验"),
            ("JAVA研发", "Java | 微服务架构设计经验 | 分布式经验"),
            ("Software Engineer", "Java | Experience with System Architecture"),
            ("后端开发工程师", "Java；系统集成由技术团队负责；不承担方案设计或PoC"),
        ):
            with self.subTest(title=title, skills=skills):
                info = self.assertQueue({"title": title, "skills": skills}, "excluded")
                self.assertTrue(info["list_pure_development"])
        self.assertQueue({"title": "岗位待定", "skills": "Python | Java | Golang | Spring"}, "excluded")

    def test_real_mixed_development_work_and_programming_solution_jobs_are_retained(self):
        for skills in ("负责总体架构设计", "方案设计", "产品规划", "业务集成", "PoC交付",
                       "System Integration", "Project Delivery"):
            with self.subTest(skills=skills):
                info = score_job({"title": "Java", "skills": skills})
                self.assertTrue(info["list_pass"])
                self.assertFalse(info["list_pure_development"])
        for title in ("AI解决方案工程师", "AI产品助理", "FDE"):
            with self.subTest(title=title):
                self.assertQueue({"title": title, "skills": ["Java", "Python", "Spring"]}, "relevant")

    def test_explicit_low_level_hardware_and_nontechnical_objects_are_excluded(self):
        cases = [
            {"title": "AI算子研发工程师", "skills": ""},
            {"title": "AI编译器开发工程师", "skills": ""},
            {"title": "高性能计算/算子开发工程师(A129987)", "skills": "C++ | Python"},
            {"title": "AI Infra工程师【算子开发】", "skills": "Agent"},
            {"title": "Compiler Development Engineer", "skills": "AI | Python"},
            {"title": "AI硬件开发工程师", "skills": "Python | PCB"},
            {"title": "芯片架构师", "skills": "架构设计经验"},
            {"title": "整车架构师", "skills": "AI"},
            {"title": "AI Chip Architect", "skills": ""},
            {"title": "财务BP-研发（SRE/基础设施）", "skills": "", "salary": "15-30K·16薪"},
            {"title": "猎头顾问（AI方向&具身智能方向&机器人）", "salary": "11-22K",
             "skills": "互联网/科技 | 制造业 | 研发岗 | 市场岗 | 产品岗"},
            {"title": "Finance Business Partner - AI Infrastructure", "skills": "AI | SRE"},
        ]
        for job in cases:
            with self.subTest(title=job["title"]):
                info = self.assertQueue(job, "excluded")
                self.assertTrue(info["list_exclusion_reasons"])
                self.assertTrue(info["list_exclusion_evidence"])

    def test_hardware_industry_does_not_blacklist_business_ai_solution_or_product_work(self):
        for title in ("芯片行业AI解决方案顾问", "AI平台产品经理（硬件企业）"):
            with self.subTest(title=title):
                self.assertQueue({
                    "title": title,
                    "skills": "负责业务流程方案设计、企业知识库系统集成与PoC交付",
                }, "relevant")

    def test_real_tax_consulting_service_is_not_technical_presales(self):
        job = {
            "title": "财税咨询师（高薪～双休法休不加班）", "salary": "11-22K",
            "skills": "CPA中国注册会计师 | 财务分析 | CTA | 售前/售后咨询 | 财税工作经验 | 中级会计职称证书 | 税务代理/咨询 | 税务师非执业会员 | 财务诊断优化 | CPA非执业会员 | 税务筹划",
        }
        info = self.assertQueue(job, "excluded")
        self.assertFalse(info["list_salary_below_target"])
        self.assertTrue(any("财税/会计专业咨询" in reason for reason in info["list_exclusion_reasons"]))
        self.assertTrue(any(item["field"] == "title" and item["quote"] == "财税咨询师"
                            for item in info["list_exclusion_evidence"]))
        self.assertTrue(any(item["field"] == "skills" and item["quote"] == "税务筹划"
                            for item in info["list_exclusion_evidence"]))
        for title in ("财税顾问", "税务咨询师", "Tax Consultant"):
            with self.subTest(title=title):
                self.assertQueue({"title": title, "skills": "AI | 税务筹划 | 售前/售后咨询"}, "excluded")
        self.assertQueue({"title": "猎头顾问（AI财税软件方向）", "skills": "AI产品 | 产品设计 | 系统集成"}, "excluded")

    def test_tax_domain_keeps_real_ai_product_or_enterprise_software_work(self):
        cases = [
            {"title": "AI财税产品经理", "skills": "CPA | 税务筹划 | 产品规划"},
            {"title": "AI会计软件产品经理", "skills": "注册会计师 | 负责产品规划、企业知识库和系统集成"},
            {"title": "企业软件财税解决方案顾问", "skills": "CTA | ERP系统 | 负责方案设计、系统集成和PoC交付"},
            {"title": "财税咨询师（AI产品方向）", "skills": "负责AI产品规划、财务系统集成和PoC交付"},
        ]
        for job in cases:
            with self.subTest(title=job["title"]):
                self.assertQueue(job, "relevant")
        self.assertQueue({"title": "税务软件实施顾问", "skills": "财税软件 | 实施交付 | 系统集成"}, "verify")

    def test_tax_service_requires_positive_personal_software_counterevidence_in_list_and_jd(self):
        for skills in (
            "税务筹划 | 售前/售后咨询 | 不负责AI产品规划和财务系统集成",
            "税务筹划 | 售前/售后咨询 | AI产品规划和财务系统集成由技术团队负责",
            "tax planning; AI product design and system integration handled by engineering team",
        ):
            with self.subTest(skills=skills):
                self.assertQueue({"title": "财税咨询师", "skills": skills}, "excluded")
        job = {"title": "财税咨询师", "skills": "CPA | 售前/售后咨询 | 税务筹划", "salary": "11-22K"}
        for jd in ("负责财税售前/售后咨询、税务筹划和财务分析。",
                   "负责财税售前/售后咨询和税务筹划，AI产品规划和财务系统集成由技术团队负责。"):
            with self.subTest(jd=jd):
                self.assertEqual(merge_model_review({**job, "jd": jd}, None)["classification"], "暂不投")
        final = merge_model_review({
            **job, "jd": "实际负责AI产品规划、财务系统集成、客户PoC交付与迭代评测。",
        }, None)
        self.assertEqual(final["classification"], "海投池")
        self.assertTrue(any("已按JD重新判断" in item for item in final["verification"]))

    def test_salary_boundary_and_jd_model_evidence_cannot_override_hard_exclusion(self):
        jd = "负责方案设计、系统集成、Agent工作流、知识库、PoC交付和迭代评测。"
        review = {"status": "success", "dimensions": {
            name: {"present": True, "evidence": [jd]} for name in DIMENSIONS
        }}
        for salary, queue in (("10-16K", "excluded"), ("16.99K", "excluded"),
                              ("10-17K", "relevant"), ("17K", "relevant"),
                              ("面议", "relevant"), ("12-16万/年", "relevant")):
            with self.subTest(salary=salary):
                job = {"title": "AI架构师", "salary": salary, "jd": jd}
                self.assertQueue(job, queue)
                final = merge_model_review(job, review)
                self.assertEqual(final["list_queue"], queue)
                if queue == "excluded":
                    self.assertEqual(final["classification"], "暂不投")
                    self.assertIn("薪资上限低于17K目标", final["reason"])

    def test_list_queue_never_fabricates_jd_responsibilities_or_shares_mutable_evidence(self):
        for title, queue in (("AI架构师", "relevant"), ("系统集成工程师", "verify")):
            with self.subTest(title=title):
                job = {"title": title, "skills": [], "company": "PoC交付方案公司"}
                self.assertQueue(job, queue)
                final = merge_model_review(job, {"status": "success", "dimensions": {
                    name: {"present": True, "evidence": ["未提供的JD"]} for name in DIMENSIONS
                }})
                self.assertEqual(final["jd_dimension_count"], 0)
                self.assertEqual(final["jd_evidence"], [])
                self.assertEqual(final["model_evidence"], [])
                self.assertTrue(all(value["present"] == "unknown" for value in final["jd_dimensions"].values()))
        result = score_job({"title": "AI架构师"})
        result["list_match_evidence"][0]["quote"] = "changed"
        self.assertQueue({"title": "AI架构师"}, "relevant")

    def test_report_cannot_use_a_coding_title_itself_to_reopen_pure_development(self):
        jd = "负责接口开发、文档编写、持续集成、单元测试及性能优化。"
        for title in ("软件工程师", "软件开发工程师", "后端开发工程师", "AI应用开发工程师"):
            with self.subTest(title=title):
                final = merge_model_review({
                    "title": title, "skills": "Java | 架构设计经验", "salary": "20-30K", "jd": jd,
                }, None)
                self.assertTrue(final["list_pure_development"])
                self.assertEqual(final["list_queue"], "excluded")
                self.assertEqual(final["classification"], "暂不投")
        reopened = merge_model_review({
            "title": "软件工程师", "skills": "Java | 架构设计经验", "salary": "20-30K",
            "jd": "实际负责总体架构设计、业务系统集成、客户PoC交付。",
        }, None)
        self.assertIn(reopened["classification"], ("重点投递", "海投池"))
        self.assertTrue(any("已按JD重新判断" in item for item in reopened["verification"]))


class ShortlistQueueOutputTests(unittest.TestCase):
    def test_split_files_and_merged_input_have_identical_unique_candidate_union(self):
        jobs = [
            {"job_id": "r1", "title": "AI架构师", "salary": "25-50K"},
            {"job_id": "v1", "title": "系统集成工程师", "salary": "13-20K"},
            {"job_id": "x1", "title": "AI Agent大客户销售", "skills": "客户开发"},
            {"job_id": "r2", "title": "AI产品助理", "salary": "15-30K"},
            {"job_id": "x2", "title": "AI架构师", "salary": "10-16K"},
        ]
        raw_rows, original = copy.deepcopy(jobs), copy.deepcopy(jobs)
        expected_counts = {"relevant": 2, "verify": 1, "excluded": 2}
        with tempfile.TemporaryDirectory() as temporary:
            root, metadata = Path(temporary), {}
            write_indexes(root, jobs, metadata, raw_rows)
            self.assertEqual(raw_rows, original)
            self.assertEqual(metadata["list_queue_counts"], expected_counts)
            self.assertEqual(metadata["list_pass_count"], 3)
            base = root / "prepared" / "jd_shortlists"
            outputs = {name: json.loads((base / f"jd_{name}.json").read_text(encoding="utf-8"))
                       for name in ("input", "relevant", "verify")}
            for name, output in outputs.items():
                self.assertEqual(output["total"], len(output["jobs"]))
                self.assertEqual(output["screening_version"], SCREENING_VERSION)
                self.assertEqual(output["list_queue_counts"], expected_counts)
                ids = [job["job_id"] for job in output["jobs"]]
                self.assertEqual(len(ids), len(set(ids)))
                if name != "input":
                    self.assertTrue(all(job["list_queue"] == name for job in output["jobs"]))
            self.assertEqual([job["job_id"] for job in outputs["input"]["jobs"]], ["r1", "v1", "r2"])
            split = outputs["relevant"]["jobs"] + outputs["verify"]["jobs"]
            self.assertEqual(len(split), len({job["job_id"] for job in split}))
            self.assertEqual({job["job_id"]: job for job in split},
                             {job["job_id"]: job for job in outputs["input"]["jobs"]})
            merged = json.loads((root / "prepared" / "merged_jobs_index.json").read_text(encoding="utf-8"))
            self.assertEqual(merged["metadata"]["list_queue_counts"], expected_counts)
            self.assertEqual(len(merged["jobs"]), len(original))

    def test_empty_input_still_writes_both_empty_queues_with_stable_zero_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, metadata = Path(temporary), {}
            write_indexes(root, [], metadata, [])
            self.assertEqual(metadata["list_queue_counts"], {queue: 0 for queue in LIST_QUEUE_LABELS})
            for name in ("input", "relevant", "verify"):
                output = json.loads((root / "prepared" / "jd_shortlists" / f"jd_{name}.json").read_text(encoding="utf-8"))
                self.assertEqual((output["total"], output["jobs"]), (0, []))


if __name__ == "__main__":
    unittest.main()
