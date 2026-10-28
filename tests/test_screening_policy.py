import copy
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from screening_policy import (  # noqa: E402
    AI_DEFAULT_MODEL,
    AI_DEFAULT_REASONING_EFFORT,
    AI_DEFAULT_WORKERS,
    JD_DEFAULT_WORKERS,
    JD_SCROLL_COUNT,
    MAX_WORKERS,
    SEMANTIC_RESULT_FIELDS,
    detect_pure_development,
    get_stage_rules,
)


class PureDevelopmentPolicyTests(unittest.TestCase):
    def assertExcluded(self, fields):
        original = copy.deepcopy(fields)
        result = detect_pure_development(fields)
        self.assertEqual(set(result), {"excluded", "reasons", "evidence"})
        self.assertIs(result["excluded"], True, fields)
        self.assertTrue(result["reasons"])
        self.assertTrue(result["evidence"])
        for item in result["evidence"]:
            self.assertEqual(set(item), {"family", "field", "quote"})
            self.assertIn(item["family"], result["reasons"])
            self.assertEqual(item["field"], "title")
            self.assertTrue(item["quote"].strip())
            self.assertIn(item["quote"], fields[item["field"]])
        self.assertEqual(fields, original)

    def assertRetained(self, fields):
        original = copy.deepcopy(fields)
        self.assertEqual(detect_pure_development(fields),
                         {"excluded": False, "reasons": [], "evidence": []}, fields)
        self.assertEqual(fields, original)

    def test_clear_backend_frontend_and_fullstack_titles_are_excluded(self):
        for title in ("Java后端开发工程师", "高级前端工程师", "全栈开发工程师", "AI全栈",
                      "后端工程师", "软件工程师", "软件研发工程师", "Python工程师",
                      "开发岗位", "纯开发岗位", "高级研发工程师", "数据开发工程师"):
            with self.subTest(title=title):
                self.assertExcluded({"title": title, "skills": "Python | Java | React"})

    def test_ai_and_agent_technology_do_not_exempt_a_coding_title(self):
        for title in ("AI开发工程师", "AI应用开发工程师", "Agent研发工程师", "大模型应用开发",
                      "Workflow开发工程师", "工作流开发工程师", "AI SDK & Framework Engineer",
                      "Python方向大模型工程化/AI框架开发工程师"):
            with self.subTest(title=title):
                self.assertExcluded({"title": title,
                                     "skills": "AI | Agent | Agentic | RAG | Workflow | Python | MCP"})

    def test_english_software_and_language_roles_have_real_word_boundaries(self):
        for title in ("Senior Software Engineer", "Software Development Engineer", "Backend Developer",
                      "Front-end Engineer", "Full Stack Developer", "AI Developer", "Java Developer",
                      "C++ Developer", "C# Engineer", ".NET Developer", "Senior Developer"):
            with self.subTest(title=title):
                self.assertExcluded({"title": title})
        for title in ("preSoftware Engineer", "Backendish Engineer", "Frameworks Engineer",
                      "JavaScripture Engineer", "Business Development Engineer", "Database Administrator"):
            with self.subTest(title=title):
                self.assertRetained({"title": title, "skills": "Python | AI"})

    def test_missing_skills_still_allow_literal_coding_title_evidence(self):
        for fields in ({"title": "后端开发工程师"}, {"title": "AI开发工程师", "skills": ""},
                       {"title": "软件工程师", "skill_tags": "", "job_labels": ""}):
            with self.subTest(fields=fields):
                self.assertExcluded(fields)

    def test_languages_in_skills_do_not_make_solution_or_product_roles_pure_development(self):
        for title in ("解决方案工程师", "AI解决方案架构师", "AI产品经理", "Product Manager",
                      "FDE", "实施交付工程师", "系统集成工程师"):
            with self.subTest(title=title):
                self.assertRetained({"title": title, "skills": "Java | Python | 开发 | 研发"})

    def test_mixed_duties_in_each_list_skill_field_reopen_a_coding_title(self):
        for field in ("skills", "skill_tags", "job_labels"):
            for mixed in ("方案设计", "解决方案", "总体架构", "系统架构设计", "产品规划", "业务集成",
                          "系统对接", "API集成", "实施交付", "项目交付", "PoC验证", "FDE"):
                with self.subTest(field=field, mixed=mixed):
                    self.assertRetained({"title": "AI开发工程师", field: f"Python | {mixed}"})

    def test_mixed_titles_reopen_development_even_without_skills(self):
        for title in ("软件开发/解决方案工程师", "软件开发兼产品经理", "研发工程师（系统集成与交付）",
                      "全栈开发与客户PoC", "后端开发架构师", "Python开发实施顾问",
                      "Software Engineer / Solutions Architect", "Software Developer / Product Owner"):
            with self.subTest(title=title):
                self.assertRetained({"title": title})

    def test_frameworks_and_ci_cd_are_not_system_architecture_or_delivery_duties(self):
        for skills in ("LangChain | LangGraph | LlamaIndex | Framework | SDK", "MVC架构 | MVVM架构",
                       "Spring | Spring Integration | React", "持续集成 | 持续交付 | 集成开发环境 | 集成测试",
                       "Continuous Integration | Continuous Delivery | CI/CD", "微服务架构 | 分布式架构"):
            with self.subTest(skills=skills):
                self.assertExcluded({"title": "AI应用开发工程师", "skills": skills})

    def test_explicit_overall_architecture_duties_are_kept(self):
        for skills in ("总体架构", "架构设计", "微服务架构设计", "System Architecture",
                       "Architectural Design", "Software Architecture"):
            with self.subTest(skills=skills):
                self.assertRetained({"title": "后端开发工程师", "skills": skills})

    def test_architecture_experience_is_a_capability_not_a_mixed_responsibility(self):
        for skills in ("架构设计经验", "具备系统架构设计能力", "熟悉软件架构设计",
                       "微服务架构设计经验 | 分布式经验", "Experience in System Architecture",
                       "Architecture Design Experience"):
            with self.subTest(skills=skills):
                self.assertExcluded({"title": "JAVA研发", "skills": skills})
        self.assertRetained({"title": "JAVA研发", "skills": "具备架构设计经验；负责客户方案设计与PoC交付"})

    def test_language_only_titles_are_coding_roles_but_language_skill_tags_are_not(self):
        for title in ("Java", "Python", "Golang", "C++", "C#", ".NET", "Senior Java"):
            with self.subTest(title=title):
                self.assertExcluded({"title": title, "skills": "Spring | Django | Redis"})
        self.assertRetained({"title": "解决方案工程师", "skills": "Java | Python | Golang"})

    def test_english_mixed_duties_are_case_insensitive_and_boundary_aware(self):
        for skills in ("SOLUTION DESIGN", "System Integration", "API Integration", "Product Planning",
                       "Project Delivery", "Implementation Engineer", "pOc"):
            with self.subTest(skills=skills):
                self.assertRetained({"title": "Software Engineer", "skills": skills})
        for skills in ("epoch | apocalypse | PoCtools", "Productivity | ProductManagementTools",
                       "microSystem IntegrationTools | myFDE | FDEtools", "solution designerTools"):
            with self.subTest(skills=skills):
                self.assertExcluded({"title": "Software Engineer", "skills": skills})

    def test_unknown_or_non_coding_titles_are_not_guessed_from_technology(self):
        for title in ("", "项目专员", "AI技术岗", "AI算法工程师", "AI工程师", "产品开发经理",
                      "业务开发经理", "客户开发工程师", "课程研发工程师", "市场开发经理"):
            with self.subTest(title=title):
                self.assertRetained({"title": title, "skills": "Python | Java | Agent | RAG | 开发"})
        self.assertRetained({})
        self.assertRetained({"skills": "Java | Python | 软件开发"})

    def test_development_as_the_domain_of_another_role_is_not_a_coding_title(self):
        for title in ("软件开发项目经理", "软件开发顾问", "软件开发讲师", "软件开发销售经理",
                      "软件开发测试工程师", "软件开发技术支持工程师", "Software Developer Advocate",
                      "Software Development Engineer / Project Manager"):
            with self.subTest(title=title):
                self.assertRetained({"title": title, "skills": "Java | Python"})

    def test_only_list_fields_are_used_and_no_input_is_mutated(self):
        self.assertExcluded({"title": "后端开发工程师", "skills": "Java", "jd": "负责方案设计与交付",
                             "company": "架构方案公司"})

    def test_returned_lists_are_not_shared_between_calls(self):
        result = detect_pure_development({"title": "软件工程师"})
        result["reasons"].clear()
        result["evidence"][0]["quote"] = "changed"
        self.assertExcluded({"title": "软件工程师"})

    def test_processor_removes_negated_and_other_team_duties_before_detection(self):
        # Exercise the real caller contract: this module receives positive
        # list clauses, rather than treating negated responsibilities as duties.
        from process_architecture import _responsibility_text

        for skills in ("Java，不负责方案设计、系统集成和交付", "Python，不要求产品规划与PoC",
                       "Java；系统集成由技术团队负责"):
            with self.subTest(skills=skills):
                self.assertExcluded({"title": "后端开发工程师", "skills": _responsibility_text(skills)})
        self.assertRetained({"title": _responsibility_text("不负责软件开发工程师工作"), "skills": "Python"})
        self.assertRetained({"title": "后端开发工程师",
                             "skills": _responsibility_text("负责方案设计，不负责模型研发")})


class StageRuleTextTests(unittest.TestCase):
    def test_semantic_model_defaults_match_the_requested_workload(self):
        self.assertEqual((AI_DEFAULT_MODEL, AI_DEFAULT_REASONING_EFFORT), ("gpt-5.6-luna", "max"))

    def test_concurrency_and_scroll_defaults_match_the_contract(self):
        self.assertEqual((AI_DEFAULT_WORKERS, JD_DEFAULT_WORKERS, JD_SCROLL_COUNT, MAX_WORKERS),
                         (10, 3, 3, 32))

    def test_semantic_field_contract_contains_cached_classification_and_evidence(self):
        expected = {
            "classification", "rule_classification", "reason", "reasons", "jd_dimensions",
            "jd_dimension_names", "jd_dimension_count", "jd_evidence", "jd_minimum_years",
            "jd_experience_evidence", "jd_exclusion_evidence", "model_evidence", "ability_gaps",
            "gaps", "verification", "verify", "llm_review", "model_status", "model_error", "confidence",
        }
        self.assertIsInstance(SEMANTIC_RESULT_FIELDS, tuple)
        self.assertEqual(set(SEMANTIC_RESULT_FIELDS), expected)
        self.assertEqual(len(SEMANTIC_RESULT_FIELDS), len(expected))

    def test_each_stage_has_a_standalone_first_line_and_full_rules(self):
        for stage in ("import", "shortlist", "fetch-jd", "llm", "report"):
            with self.subTest(stage=stage):
                value = get_stage_rules(stage)
                self.assertTrue(value.splitlines()[0].strip())
                self.assertGreaterEqual(len(value.splitlines()), 2)
        self.assertIn("请选择", get_stage_rules("unknown"))

    def test_shortlist_explains_wide_recall_and_pure_development_boundaries(self):
        rules = get_stage_rules("shortlist")
        for phrase in ("不设5分门槛", "分数仅排序", "宽召回", "纯开发", "纯销售", "纯算法/底模",
                       "纯底层", "明显无关", "总体架构", "信息不足不猜测", "月薪上限<17K直接剔除",
                       "上限=17K不因薪资剔除", "面议", "经验偏高只标注"):
            self.assertIn(phrase, rules)

    def test_jd_explains_no_filtering_and_actual_parallel_limit(self):
        rules = get_stage_rules("fetch-jd")
        self.assertIn("不作职责关键词筛选", rules.splitlines()[0])
        for phrase in ("正文与完整度", f"默认{JD_DEFAULT_WORKERS}", f"1–{MAX_WORKERS}", "当前批次待抓数",
                       "较小值", "批大小仅用于检查点分组", f"滚动{JD_SCROLL_COUNT}次", "专用Chrome",
                       "验证暂停调度", "检查点", "不自动剔除"):
            self.assertIn(phrase, rules)

    def test_ai_explains_two_field_judgment_without_code_thresholds(self):
        rules = get_stage_rules("llm")
        for phrase in ("完整JD", "企业AI解决方案架构师", "AI产品经理", "业务需求分析", "方案设计", "系统集成",
                       "不强求同名", "代码开发", "算法研究", "模型训练", "硬件", "渠道招商", "底层基础设施",
                       "不可误判", "判断", "理由", "一两句话", "符合→海投池", "不符合→暂不投", "独立待核实",
                       "默认待人工核实", "可逐条或批量人工保留或剔除", "有效已确认人工保留或剔除优先",
                       "原AI证据保留", "不走代码语义兜底剔除", "缺失/部分JD", "gpt-5.6-luna", "思考强度max",
                       "不接收职位名称", "通用关键词", "只读追溯", "恢复默认后仍须保存",
                       "信息不足", f"默认{AI_DEFAULT_WORKERS}", f"1–{MAX_WORKERS}", "独立于JD页面并发"):
            self.assertIn(phrase, rules)
        for obsolete in ("规则回退", ">=4", ">=2", "0–1项", "最终报告仍按原代码规则", "不进入保留或剔除名单"):
            self.assertNotIn(obsolete, rules)

    def test_manual_selection_precedence_does_not_promise_a_final_code_override(self):
        for stage in ("shortlist", "fetch-jd", "llm"):
            with self.subTest(stage=stage):
                rules = get_stage_rules(stage)
                for phrase in ("人工保留/剔除优先于本步建议", "重跑保留人工选择", "重置人工选择",
                               "不自动运行", "不进入最终可投池", "不直接改写AI判断"):
                    self.assertIn(phrase, rules)
        self.assertIn("报告只读", get_stage_rules("report"))


if __name__ == "__main__":
    unittest.main()
