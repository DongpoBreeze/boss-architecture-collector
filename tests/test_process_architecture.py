import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import project_llm_settings
from process_architecture import (  # noqa: E402
    CLASSIFICATIONS,
    DIMENSIONS,
    SCREENING_VERSION,
    _report_job_lines,
    _report_overview_lines,
    collect_rows,
    extract_jd_sections,
    fetch_jd,
    inspect_city,
    merge_model_review,
    merge_rows,
    main,
    model_input,
    output_root_for,
    parse_jd_experience,
    parse_salary,
    report_stage,
    rule_analyze_jd,
    run_detail_subprocess,
    run_llm,
    salary_status,
    score_job,
    shortlist_jobs,
    wait_for_detail_gate_retry,
)


def collection_config():
    return {
        "current": {
            "keywords": [{"id": "kw", "query": "AI 架构"}],
            "experiences": [{"id": "exp", "query": "不限"}],
        },
        "target": {
            "keywords": [{"id": "kw", "query": "AI 架构"}],
            "experiences": [{"id": "exp", "query": "不限"}],
        },
    }


def write_raw_file(root: Path, name: str, jobs: list[dict], track: str = "current") -> None:
    directory = root / "raw" / track / "shanghai"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "city": "上海",
                "filters": {"salary": "", "experience": "不限", "degree": "203"},
                "jobs": jobs,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class ProcessArchitectureTests(unittest.TestCase):
    def test_semantic_input_excludes_general_keywords_titles_and_search_metadata(self):
        job = {
            "job_id": "semantic-input",
            "title": "AI解决方案架构师",
            "company": "Agent RAG 公司介绍",
            "skills": ["Agent", "RAG", "Workflow", "系统集成"],
            "tags": "5-10年 | 本科",
            "salary": "30-50K",
            "jd": "岗位职责\n负责企业内部流程梳理与方案交付。\n"
                  "任职要求\n熟悉Agent、RAG、Workflow和系统集成；有大模型项目经验。",
        }
        semantic = model_input(job)
        self.assertEqual(set(semantic), {"job_id", "responsibilities", "requirements"})
        self.assertEqual(semantic["job_id"], "semantic-input")
        self.assertIn("方案交付", semantic["responsibilities"])
        for keyword in ("Agent", "RAG", "Workflow", "系统集成", "大模型"):
            self.assertNotIn(keyword, semantic["responsibilities"])
        self.assertIn("Agent", semantic["requirements"])
        self.assertNotIn("title", semantic)
        self.assertNotIn("skills", semantic)

    def test_generic_terms_in_requirements_or_skill_lists_do_not_hit_duties(self):
        cases = (
            "岗位职责\n负责客户培训和问题跟进。\n任职要求\n熟悉Agent、RAG、Workflow、架构设计和系统集成。",
            "岗位职责\n负责企业软件维护。\n技能要求：Agent | RAG | Workflow | 知识库 | 系统集成 | 交付",
            "熟悉Agent、RAG、Workflow、方案架构、系统集成和PoC交付。",
        )
        for jd in cases:
            with self.subTest(jd=jd):
                result = rule_analyze_jd(jd, "AI应用架构师")
                self.assertEqual(result["dimension_count"], 0)
                self.assertNotEqual(result["dimensions"]["Agent/RAG/Workflow"]["present"], True)
                self.assertNotEqual(result["dimensions"]["系统边界与集成"]["present"], True)

    def test_explicit_action_bearing_ai_application_duties_are_still_detected(self):
        jd = (
            "岗位职责\n负责为企业客户设计AI解决方案并建设Agent/RAG工作流，"
            "完成知识库、系统集成、PoC交付和效果迭代。\n"
            "任职要求\n熟悉大模型应用和Workflow。"
        )
        result = rule_analyze_jd(jd, "通用岗位名称")
        self.assertGreaterEqual(result["dimension_count"], 4)
        self.assertIn("Agent", result["responsibilities"])
        self.assertNotIn("熟悉大模型应用", result["responsibilities"])

    def test_semantic_ai_direction_and_priority_need_grounded_direct_duties(self):
        jd = (
            "岗位职责\n负责企业应用总体架构设计、系统集成、PoC交付和效果迭代。\n"
            "任职要求\n熟悉Agent、RAG、Workflow，具备大模型项目经验。"
        )
        review = {
            "status": "success",
            "target_relevance": {"assessment": "direct", "evidence": ["熟悉Agent、RAG、Workflow，具备大模型项目经验。"]},
            "dimensions": {
                name: {"present": name in (DIMENSIONS[0], DIMENSIONS[1], DIMENSIONS[4], DIMENSIONS[5]),
                       "evidence": [jd.splitlines()[1]] if name in (DIMENSIONS[0], DIMENSIONS[1], DIMENSIONS[4], DIMENSIONS[5]) else []}
                for index, name in enumerate(DIMENSIONS)
            },
        }
        final = merge_model_review({
            "title": "AI应用架构师", "skills": "Agent | RAG", "salary": "30-50K",
            "jd": jd, "jd_completeness": "complete",
        }, review)
        self.assertEqual(final["jd_dimension_count"], 4)
        self.assertNotEqual(final["target_relevance"]["assessment"], "direct")
        self.assertNotEqual(final["classification"], "重点投递")

        direct = merge_model_review({
            "title": "AI应用架构师", "salary": "30-50K", "jd": (
                "岗位职责\n负责设计AI应用方案和Agent/RAG工作流，完成知识库建设、"
                "系统集成、PoC交付和效果迭代。\n任职要求\n熟悉Python。"
            ), "jd_completeness": "complete",
        }, {
            "status": "success",
            "target_relevance": {"assessment": "direct", "evidence": [
                "负责设计AI应用方案和Agent/RAG工作流，完成知识库建设、系统集成、PoC交付和效果迭代。"
            ]},
            "dimensions": {name: {"present": True, "evidence": [
                "负责设计AI应用方案和Agent/RAG工作流，完成知识库建设、系统集成、PoC交付和效果迭代。"
            ]} for name in DIMENSIONS},
        })
        self.assertEqual(direct["target_relevance"]["assessment"], "direct")
        self.assertEqual(direct["classification"], "重点投递")

        unrelated = merge_model_review({
            "title": "AI架构师", "skills": "Agent | RAG", "salary": "30-50K",
            "jd": "岗位职责\n负责云原生基础设施架构、Kubernetes集群治理和高并发平台性能优化。\n"
                  "任职要求\n熟悉Agent、RAG和大模型。",
        }, {
            "status": "success",
            "target_relevance": {"assessment": "unrelated", "evidence": ["负责云原生基础设施架构、Kubernetes集群治理和高并发平台性能优化。"]},
            "dimensions": {},
        })
        self.assertEqual(unrelated["classification"], "暂不投")
        self.assertIn("不相关", unrelated["reason"])

    def test_semantic_review_accepts_paraphrased_duties_without_fixed_keywords(self):
        duty = "负责把企业制度和历史案例转化为可检索、可生成答复的内部服务，并接入现有工单系统。"
        review = {
            "status": "success",
            "target_relevance": {"assessment": "direct", "evidence": [duty]},
            "dimensions": {
                name: {
                    "present": name in ("Agent/RAG/Workflow", "知识与数据", "系统边界与集成"),
                    "evidence": [duty] if name in ("Agent/RAG/Workflow", "知识与数据", "系统边界与集成") else [],
                }
                for name in DIMENSIONS
            },
        }
        result = merge_model_review({"title": "不含关键词的岗位名", "salary": "20-30K", "jd": duty,
                                     "jd_completeness": "complete"}, review)
        self.assertEqual(result["target_relevance"]["assessment"], "direct")
        self.assertEqual(result["jd_dimension_count"], 3)
        self.assertEqual(result["classification"], "海投池")
        self.assertEqual(result["model_evidence"], [duty])

    def test_collect_and_inspect_preserve_identical_provenance_and_original_row_numbers(self):
        name = "current__shanghai__kw__salary__exp.json"
        source_file = f"raw/current/shanghai/{name}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_raw_file(root, name, [
                {"job_id": "first", "source_row": 99, "source_line_number": 100},
                None,
                "invalid row",
                {"job_id": "last"},
            ])
            path = root / source_file
            before = path.read_bytes()
            rows, metadata = collect_rows(root, collection_config(), city="上海")
            with patch("process_architecture.merge_rows", wraps=merge_rows) as merge:
                summary = inspect_city(root, collection_config(), city="上海")
            self.assertEqual(merge.call_args.args[0], rows)
            self.assertEqual(path.read_bytes(), before)

        self.assertEqual([row["source_row"] for row in rows], [0, 3])
        self.assertEqual([row["source_line_number"] for row in rows], [1, 4])
        for row, line in zip(rows, (1, 4)):
            context = row["source_context"]
            self.assertEqual(row["source_file"], source_file)
            self.assertEqual(context["source_file"], source_file)
            self.assertEqual(context["source_line_number"], line)
            self.assertEqual(context["combination"], Path(name).stem)
            self.assertEqual(context["keyword"], "AI 架构")
            self.assertNotIn("payload", context)
        self.assertEqual(metadata["raw_sample_count"], 2)
        self.assertEqual(summary["raw_record_count"], 2)
        self.assertEqual(summary["deduplicated_job_count"], 2)

    def test_balanced_dedup_sample_round_robins_combinations_and_filters_track_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def write_combination(track: str, salary_id: str, jobs: list[dict]) -> None:
                target = root / "raw" / track / "shanghai" / f"{track}__shanghai__kw__{salary_id}__exp.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps({"city": "上海", "filters": {"salary": salary_id, "experience": "不限"}, "jobs": jobs}, ensure_ascii=False),
                    encoding="utf-8",
                )

            write_combination("current", "salary-a", [{"job_id": "shared"}, {"job_id": "current-a"}])
            write_combination("current", "salary-b", [{"job_id": "shared"}, {"job_id": "current-b"}])
            write_combination("target", "salary-a", [{"job_id": "target-a"}, {"job_id": "shared"}])

            jobs, metadata = collect_rows(
                root,
                collection_config(),
                city="上海",
                sample_size=4,
                sample_strategy="balanced-dedup",
            )
            target_jobs, target_metadata = collect_rows(
                root,
                collection_config(),
                city="shanghai",
                sample_size=10,
                sample_strategy="balanced-dedup",
                tracks="target",
            )

        self.assertEqual(len(jobs), 4)
        self.assertEqual(len({job["dedupe_key"] for job in jobs}), 4)
        self.assertEqual(metadata["raw_record_count"], 6)
        self.assertEqual(metadata["unique_job_count"], 4)
        self.assertEqual(metadata["dedupe_removed"], 2)
        self.assertEqual({context["track"] for job in jobs for context in job["observed_in"]}, {"current", "target"})
        self.assertEqual({job["job_id"] for job in target_jobs}, {"target-a", "shared"})
        self.assertEqual(target_metadata["tracks"], "target")

    def test_inspect_city_counts_raw_unique_combinations_and_manifest_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_raw_file(root, "current__shanghai__kw__salary-a__exp.json", [{"job_id": "shared"}, {"job_id": "current-only"}])
            write_raw_file(root, "target__shanghai__kw__salary-a__exp.json", [{"job_id": "shared"} ], track="target")
            write_raw_file(root, "current__shenzhen__kw__salary-a__exp.json", [{"job_id": "other-city"}])
            (root / "collection_manifest.json").write_text(
                json.dumps({
                    "records": [
                        {"combination": "current__shanghai__kw__salary-a__exp", "city": "上海", "city_id": "shanghai", "track": "current", "status": "success"},
                        {"combination": "target__shanghai__kw__salary-a__exp", "city": "上海", "city_id": "shanghai", "track": "target", "status": "paused"},
                        {"combination": "current__shenzhen__kw__salary-a__exp", "city": "深圳", "city_id": "shenzhen", "track": "current", "status": "failed"},
                    ]
                }),
                encoding="utf-8",
            )

            summary = inspect_city(root, collection_config(), city="上海")
            current_summary = inspect_city(root, collection_config(), city="shanghai", tracks="current")

        self.assertEqual(summary["raw_record_count"], 3)
        self.assertEqual(summary["deduplicated_job_count"], 2)
        self.assertEqual(summary["combination_count"], 2)
        self.assertEqual(
            summary["manifest_status_counts"],
            {"failed": 0, "no_data": 0, "paused": 1, "success": 1},
        )
        self.assertEqual(
            current_summary["manifest_status_counts"],
            {"failed": 0, "no_data": 0, "paused": 0, "success": 1},
        )
        self.assertEqual(current_summary["combination_count"], 1)

    def test_inspect_city_reports_expected_combinations_and_tolerates_corrupt_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                "cities": [{"id": "shanghai", "name": "上海"}],
                "salaries": [{"id": "salary-a"}, {"id": "salary-b"}],
                "current": {
                    "keywords": [{"id": "kw", "query": "AI 架构"}],
                    "experiences": [{"id": "exp", "query": "不限"}],
                },
                "target": {
                    "keywords": [{"id": "kw", "query": "AI 架构"}],
                    "experiences": [{"id": "exp", "query": "不限"}],
                },
            }
            write_raw_file(root, "current__shanghai__kw__salary-a__exp.json", [{"job_id": "one"}])
            (root / "raw" / "current" / "shanghai" / "current__shanghai__kw__salary-b__exp.json").write_text("{broken", encoding="utf-8")
            (root / "collection_manifest.json").write_text("[broken", encoding="utf-8")

            summary = inspect_city(root, config, city="上海", tracks="current")

        self.assertEqual(summary["expected_combination_count"], 2)
        self.assertEqual(
            summary["expected_combinations"],
            [
                "current__shanghai__kw__salary-a__exp",
                "current__shanghai__kw__salary-b__exp",
            ],
        )
        self.assertEqual(summary["raw_combination_count"], 1)
        self.assertEqual(summary["completed_combination_count"], 0)
        self.assertEqual(summary["pending_combination_count"], 2)
        self.assertEqual(summary["invalid_raw_file_count"], 1)
        self.assertEqual(
            summary["manifest_status_counts"],
            {"failed": 0, "no_data": 0, "paused": 0, "success": 0},
        )

    def test_inspect_city_counts_completed_and_pending_manifest_combinations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                "cities": [{"id": "shanghai", "name": "上海"}],
                "salaries": [{"id": "salary-a"}, {"id": "salary-b"}],
                "current": {
                    "keywords": [{"id": "kw", "query": "AI 架构"}],
                    "experiences": [{"id": "exp", "query": "不限"}],
                },
                "target": {
                    "keywords": [{"id": "kw", "query": "AI 架构"}],
                    "experiences": [{"id": "exp", "query": "不限"}],
                },
            }
            statuses = {
                "current__shanghai__kw__salary-a__exp": "success",
                "current__shanghai__kw__salary-b__exp": "no_data",
                "target__shanghai__kw__salary-a__exp": "paused",
                "target__shanghai__kw__salary-b__exp": "failed",
            }
            (root / "collection_manifest.json").write_text(
                json.dumps({"records": [{"combination": name, "status": status} for name, status in statuses.items()]}),
                encoding="utf-8",
            )

            summary = inspect_city(root, config, city="shanghai")

        self.assertEqual(summary["expected_combination_count"], 4)
        self.assertEqual(summary["completed_combination_count"], 2)
        self.assertEqual(summary["pending_combination_count"], 2)
        self.assertEqual(
            summary["manifest_status_counts"],
            {"failed": 1, "no_data": 1, "paused": 1, "success": 1},
        )

    def test_balanced_dedup_is_stable_and_preserves_provenance_when_rerun_on_merged_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_raw_file(
                root,
                "current__shanghai__kw__salary-a__exp.json",
                [{"job_id": "shared"}, {"job_id": "current-a"}],
            )
            write_raw_file(
                root,
                "target__shanghai__kw__salary-a__exp.json",
                [{"job_id": "shared"}, {"job_id": "target-a"}],
                track="target",
            )
            first, first_meta = collect_rows(root, collection_config(), sample_strategy="balanced-dedup", sample_size=3)
            merged_file = root / "raw" / "current" / "shanghai" / "current__shanghai__kw__merged__exp.json"
            merged_file.write_text(
                json.dumps(
                    {
                        "city": "上海",
                        "filters": {"salary": "merged", "experience": "不限"},
                        "jobs": first,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            second, second_meta = collect_rows(root, collection_config(), sample_strategy="balanced-dedup", sample_size=3)

        self.assertEqual([job["job_id"] for job in first], [job["job_id"] for job in second])
        self.assertEqual(first_meta["unique_job_count"], second_meta["unique_job_count"])
        provenance = {context["source_file"] for job in second for context in job["observed_in"]}
        self.assertIn("raw/current/shanghai/current__shanghai__kw__salary-a__exp.json", provenance)
        self.assertIn("raw/target/shanghai/target__shanghai__kw__salary-a__exp.json", provenance)
        self.assertIn("raw/current/shanghai/current__shanghai__kw__merged__exp.json", provenance)

    def test_output_root_run_timestamp_preserves_legacy_test_run_and_cli_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            legacy = output_root_for(root, "上海", 20)
            independent = output_root_for(root, "上海", 20, "20261017-123456")
            explicit = Path(directory) / "explicit-output"
            config = Path(directory) / "config.json"
            config.write_text("{}", encoding="utf-8")
            with patch("process_architecture.merge_stage", return_value=([], {})) as merge:
                with redirect_stdout(io.StringIO()):
                    result = main([
                        "--root", str(root),
                        "--config", str(config),
                        "--city", "上海",
                        "--sample-size", "20",
                        "--sample-strategy", "balanced-dedup",
                        "--tracks", "target",
                        "--output-root", str(explicit),
                        "--stage", "merge",
                    ])

        self.assertEqual(result, 0)
        self.assertEqual(legacy, root / "test-runs" / "shanghai-20")
        self.assertEqual(independent, root / "runs" / "20261017-123456-上海-20")
        self.assertEqual(merge.call_args.args[:2], (root.resolve(), explicit.resolve()))
        self.assertEqual(merge.call_args.kwargs["sample_strategy"], "balanced-dedup")
        self.assertEqual(merge.call_args.kwargs["tracks"], "target")

    def test_gui_interactive_gate_wait_accepts_non_tty_stdin(self):
        with patch("process_architecture.sys.stdin", SimpleNamespace(isatty=lambda: False)), patch(
            "builtins.input", return_value=""
        ) as prompt:
            self.assertTrue(wait_for_detail_gate_retry(wait_on_gate=True, allow_non_tty=True))
        prompt.assert_called_once()

    def test_shanghai_sample_is_deterministic_and_capped_at_first_50_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = [{"job_id": f"first-{index:02d}", "title": "first"} for index in range(30)]
            second = [{"job_id": f"second-{index:02d}", "title": "second"} for index in range(30)]
            # File ordering is part of the read contract: the first sorted file
            # contributes all 30 rows, then only the first 20 rows of the next.
            write_raw_file(root, "a__shanghai__kw__salary__exp.json", first)
            write_raw_file(root, "b__shanghai__kw__salary__exp.json", second)

            rows, metadata = collect_rows(root, collection_config(), city="上海", sample_size=50)

            self.assertEqual(len(rows), 50)
            self.assertEqual(metadata["raw_sample_count"], 50)
            self.assertEqual([row["job_id"] for row in rows[:30]], [f"first-{i:02d}" for i in range(30)])
            self.assertEqual([row["job_id"] for row in rows[30:]], [f"second-{i:02d}" for i in range(20)])
            self.assertTrue(all(row["source_context"]["city"] == "上海" for row in rows))

    def test_merge_deduplicates_identity_aliases_and_records_observed_in(self):
        rows = [
            {
                "job_id": "job-1",
                "job_link": "https://jobs.test/one",
                "title": "AI 架构师",
                "source_file": "raw/current/shanghai/a.json",
                "source_row": 0,
                "source_context": {"track": "current", "city": "上海"},
            },
            {
                "job_id": "job-1",
                "job_link": "https://jobs.test/one",
                "salary": "20-30K",
                "source_file": "raw/target/shanghai/b.json",
                "source_row": 4,
                "source_context": {"track": "target", "city": "上海"},
            },
            {
                "encrypt_job_id": "encrypted-1",
                "job_link": "https://jobs.test/one",
                "source_file": "raw/current/shanghai/c.json",
                "source_row": 8,
                "source_context": {"track": "current", "city": "上海"},
            },
            {
                "job_id": "job-2",
                "job_link": "https://jobs.test/two",
                "title": "另一个岗位",
                "source_file": "raw/current/shanghai/d.json",
                "source_row": 1,
                "source_context": {"track": "current", "city": "上海"},
            },
        ]

        jobs, metadata = merge_rows(rows)

        self.assertEqual(metadata, {"raw_list_rows": 4, "unique_job_count": 2, "dedupe_removed": 2})
        merged = next(job for job in jobs if job["job_id"] == "job-1")
        self.assertEqual(merged["salary"], "20-30K")
        self.assertEqual(len(merged["observed_in"]), 3)
        self.assertEqual({entry["track"] for entry in merged["observed_in"]}, {"current", "target"})
        self.assertEqual({entry["source_row"] for entry in merged["observed_in"]}, {0, 4, 8})
        self.assertEqual(len({job["dedupe_key"] for job in jobs}), 2)

    def test_merge_collapses_late_bridge_between_two_identity_aliases(self):
        rows = [
            {"job_id": "job-a", "title": "A"},
            {"encrypt_job_id": "enc-b", "title": "B"},
            {"job_id": "job-a", "encrypt_job_id": "enc-b", "job_link": "https://jobs.test/bridge"},
        ]
        jobs, metadata = merge_rows(rows)
        self.assertEqual(metadata["unique_job_count"], 1)
        self.assertEqual(jobs[0]["job_id"], "job-a")
        self.assertEqual(jobs[0]["encrypt_job_id"], "enc-b")
        self.assertEqual(len(jobs[0]["observed_in"]), 3)
        remerged, _ = merge_rows(jobs)
        self.assertEqual(remerged[0]["observed_in"], jobs[0]["observed_in"])

    def test_title_and_skill_screening_keeps_relevant_jobs_and_excludes_pure_sales(self):
        relevant = {"title": "AI 解决方案架构师", "skills": "Agent | RAG"}
        skill_only = {"title": "客户顾问", "skills": "解决方案 Agent"}
        technical_sales = {"title": "AI 销售经理", "skills": "解决方案"}
        sales = {"title": "AI 电话销售经理", "skills": "电话销售 | 陌拜 | 开发新客户"}

        relevant_info = score_job(relevant)
        skill_info = score_job(skill_only)
        sales_info = score_job(sales)
        shortlisted = shortlist_jobs([relevant, skill_only, technical_sales, sales])

        self.assertGreaterEqual(relevant_info["score"], 5)
        self.assertIn("解决方案", relevant_info["hit_terms"])
        self.assertIn("Agent", relevant_info["hit_terms"])
        self.assertGreaterEqual(skill_info["score"], 5)
        self.assertFalse(skill_info["list_excluded"])
        self.assertTrue(sales_info["list_excluded"])
        self.assertTrue(any("纯销售" in reason for reason in sales_info["list_exclusion_reasons"]))
        self.assertTrue(technical_sales["list_pass"])
        self.assertFalse(technical_sales["list_excluded"])
        self.assertEqual(technical_sales["list_priority"], "低优先待JD")
        self.assertEqual(shortlisted, [relevant, skill_only, technical_sales])

    def test_empty_skills_do_not_block_ai_product_application_or_enablement_recall(self):
        titles = ("AI产品经理", "智能体产品内部孵化岗", "AI应用专员", "AI业务赋能工程师")
        jobs = [{"job_id": str(index), "title": title, "skills": []} for index, title in enumerate(titles)]
        candidates = shortlist_jobs(jobs)
        self.assertEqual(candidates, jobs)
        self.assertTrue(any(job["score"] < 5 for job in candidates))
        for job in candidates:
            with self.subTest(title=job["title"]):
                self.assertTrue(job["list_pass"])
                self.assertFalse(job["list_excluded"])
                self.assertEqual(job["list_priority"], "优先补JD")
                self.assertEqual(job["screening_version"], SCREENING_VERSION)
                self.assertTrue(job["list_match_evidence"])
                self.assertTrue(any("技能字段缺失" in item for item in job["list_verification"]))
                self.assertEqual(job["skills"], [])
                final = merge_model_review(job, None)
                self.assertEqual(final["classification"], "海投池")
                self.assertEqual(final["jd_dimension_count"], 0)
                self.assertEqual(final["jd_evidence"], [])
                self.assertEqual(final["model_evidence"], [])
                self.assertEqual(final["jd_status"], "待核实")
                self.assertTrue(all(value["present"] == "unknown" for value in final["jd_dimensions"].values()))

    def test_applied_agent_algorithm_sdk_and_operations_titles_keep_literal_evidence(self):
        jobs = [
            {"title": "AI 算法工程师（大模型应用 / Agent 方向）", "skills": "PyTorch | 模型训练"},
            {"title": "AI SDK & Framework Engineer", "skills": "客户技术方案 | 系统集成"},
            {"title": "Python方向大模型工程化/AI框架开发工程师", "skills": "K8S | PoC交付"},
            {"title": "AI业务运营管培生", "skills": "ai业务应用落地 | BI平台建设"},
            {"title": "AI业务分析师（售前）", "skills": ""},
            {"title": "资深算法工程师–智能客服机器人", "skills": "视觉 | 自然语言处理"},
            {"title": "AI培训师", "skills": "RPA | 业务流程自动化"},
            {"title": "产品专员", "skills": "", "skill_tags": ["AI应用", "需求分析"]},
        ]
        for job in jobs:
            with self.subTest(title=job["title"]):
                info = score_job(job)
                self.assertTrue(info["list_pass"])
                self.assertFalse(info["list_excluded"])
                for item in info["list_match_evidence"]:
                    raw = job[item["field"]]
                    raw_text = " | ".join(raw) if isinstance(raw, list) else raw
                    self.assertIn(item["quote"], raw_text)
        self.assertEqual(score_job(jobs[0])["list_priority"], "优先补JD")
        self.assertEqual(score_job(jobs[3])["list_priority"], "优先补JD")

    def test_application_technical_manager_and_ambiguous_trainee_pm_titles_are_retained(self):
        manager = {
            "title": "AI/人工智能应用技术经理/主管",
            "skills": "AI机器人 | 机器学习类AI | B端产品 | C端产品 | AI餐饮应用",
        }
        info = score_job(manager)
        self.assertTrue(info["list_pass"])
        self.assertEqual(info["list_priority"], "优先补JD")
        for title in ("AI管培生", "27届秋招-游戏PM（棋牌）"):
            with self.subTest(title=title):
                info = score_job({"title": title, "skills": []})
                self.assertTrue(info["list_pass"])
                self.assertEqual(info["list_priority"], "低优先待JD")
                if "PM" in title:
                    self.assertTrue(any("核实PM" in item for item in info["list_verification"]))

    def test_fde_and_technical_sales_kpis_do_not_override_application_responsibilities(self):
        jd = "负责客户技术方案设计、系统集成。协助销售额、签约与回款目标。"
        for title in ("大客户FDE（营销侧）", "技术销售工程师", "AI售前解决方案工程师"):
            with self.subTest(title=title):
                job = {"title": title, "skills": "销售目标 | 回款 | 签约", "salary": "10-20K", "jd": jd}
                self.assertTrue(score_job(job)["list_pass"])
                self.assertFalse(rule_analyze_jd(jd, title)["pure_sales"])
                final = merge_model_review(job, None)
                self.assertEqual(final["classification"], "海投池")
                self.assertEqual(final["jd_dimension_count"], 2)
                self.assertIn("核实固定薪资和绩效占比", final["verification"])

    def test_ai_product_sales_with_acquisition_and_explicit_no_technical_work_is_excluded(self):
        job = {
            "title": "AI智能体销售", "skills": "电话销售 | 陌拜 | 销售额 | 签单", "salary": "20-30K",
            "jd": "岗位职责：每天电话销售、陌拜拜访，完成销售额与签单指标；向客户介绍Agent产品。无需参与技术方案、研发、PoC和交付，相关工作由技术团队负责。",
        }
        info = score_job(job)
        self.assertFalse(info["list_pass"])
        self.assertTrue(info["list_excluded"])
        final = merge_model_review(job, None)
        self.assertEqual(final["classification"], "暂不投")
        self.assertEqual(final["jd_dimension_count"], 0)
        self.assertEqual(final["jd_evidence"], [])
        self.assertTrue(rule_analyze_jd(job["jd"], job["title"])["pure_sales"])
        ambiguous = score_job({"title": "AI智能体销售", "skills": []})
        self.assertTrue(ambiguous["list_pass"])
        self.assertFalse(ambiguous["list_excluded"])
        self.assertEqual(ambiguous["list_priority"], "低优先待JD")

    def test_negated_or_other_team_duties_do_not_create_matches_even_with_model_quotes(self):
        jd = "负责方案设计，不负责Agent研发、PoC和交付；系统集成由技术团队负责。"
        review = {
            "status": "success",
            "dimensions": {
                "Agent/RAG/Workflow": {"present": True, "evidence": ["不负责Agent研发、PoC和交付"]},
                "PoC/交付": {"present": True, "evidence": ["不负责Agent研发、PoC和交付"]},
                "系统边界与集成": {"present": True, "evidence": ["系统集成由技术团队负责"]},
            },
        }
        final = merge_model_review({"title": "AI产品经理", "jd": jd, "salary": "20-30K"}, review)
        self.assertEqual(final["jd_dimension_names"], ["方案设计"])
        self.assertEqual(final["jd_dimension_count"], 1)
        self.assertEqual(final["classification"], "暂不投")
        self.assertEqual(final["model_evidence"], [])
        self.assertEqual(final["jd_evidence"], ["负责方案设计"])

    def test_fde_keeps_actual_positive_work_before_a_separate_negative_clause(self):
        jd = "负责技术方案设计、系统集成与客户PoC验证，不负责模型训练和陌拜销售。"
        final = merge_model_review({"title": "大客户FDE（营销侧）", "skills": "销售额 | 签单", "jd": jd, "salary": "10-20K"}, None)
        self.assertEqual(final["classification"], "海投池")
        self.assertEqual(final["jd_dimension_count"], 3)
        self.assertFalse(rule_analyze_jd(jd, "大客户FDE（营销侧）")["pure_sales"])
        self.assertTrue(all("不负责" not in quote for quote in final["jd_evidence"]))

    def test_generic_product_project_and_quality_are_low_priority_transitions(self):
        jobs = [
            {"title": "初级产品经理", "skills": []},
            {"title": "项目经理管培生（27届校招）", "skills": "项目管理"},
            {"title": "AI测试工程师（平台）", "skills": "K8S"},
            {"title": "大模型训练师/提示词优化", "skills": "模型效果评估"},
            {"title": "AI Product Designer", "skills": "UI/UX"},
        ]
        for job in jobs:
            with self.subTest(title=job["title"]):
                info = score_job(job)
                self.assertTrue(info["list_pass"])
                self.assertEqual(info["list_priority"], "低优先待JD")
                self.assertTrue(any("不据此认定AI相关" in item for item in info["list_verification"]))
        python_only = score_job({"title": "开发岗位", "skills": "Python"})
        self.assertEqual(python_only["list_families"], ["通用软件/系统工程过渡"])
        self.assertFalse(python_only["list_pass"])

    def test_pure_development_is_excluded_even_with_ai_stack_and_mixed_delivery_is_kept(self):
        for title, skills in (("软件开发工程师", "Python | Java"), ("数据开发工程师", "ETL"),
                              ("AI全栈", "Agent | RAG"), ("大模型应用开发工程师", "Agent | RAG | Python"),
                              ("AI SDK & Framework Engineer", ""),
                              ("Python方向大模型工程化/AI框架开发工程师", "K8S"),
                              ("前端开发工程师", "Vue | TypeScript"), ("后端研发工程师", "Java | Spring")):
            with self.subTest(title=title):
                item = score_job({"title": title, "skills": skills})
                self.assertFalse(item["list_pass"])
                self.assertTrue(item["list_pure_development"])
                self.assertTrue(any("纯开发" in reason for reason in item["list_exclusion_reasons"]))
                self.assertTrue(item["list_exclusion_evidence"])
        for title, skills in (("AI产品经理", "Python | Agent"), ("解决方案工程师", "Java | Python"),
                              ("AI应用开发工程师", "客户方案设计 | 系统集成 | PoC交付")):
            with self.subTest(title=title):
                item = score_job({"title": title, "skills": skills})
                self.assertTrue(item["list_pass"])
                self.assertFalse(item["list_pure_development"])

    def test_clear_main_work_object_exclusions_are_not_single_word_blacklists(self):
        jobs = [
            {"title": "电话销售", "skills": "电话邀约 | 陌拜"},
            {"title": "AI产品主播", "skills": "直播 | 需要露脸"},
            {"title": "AI短视频编导", "skills": "Premiere | 剪辑"},
            {"title": "广告平面设计师", "skills": "PS | AE | AI"},
            {"title": "PS/AE/AI美术", "skills": "角色原画 | 手绘"},
            {"title": "课程顾问", "skills": "线下教育"},
            {"title": "IC可靠性/封装工艺/芯片测试", "skills": "晶圆制造"},
            {"title": "应用软件研发工程师", "skills": "熟悉MCU | 嵌入式软件开发"},
            {"title": "基座大模型预训练算法研究员", "skills": "模型训练 | 强化学习"},
            {"title": "AI软件开发工程师（训练）", "skills": "强化学习 | 自然语言处理算法 | 多模态算法"},
            {"title": "AI Infra基础设施研发", "skills": "CUDA | 算子 | 分布式训练"},
            {"title": "晶圆工艺集成工程师", "skills": "晶圆制造"},
            {"title": "口腔产品经理（牙膏）", "skills": "日化产品"},
        ]
        for job in jobs:
            with self.subTest(title=job["title"]):
                info = score_job(job)
                self.assertFalse(info["list_pass"])
                self.assertTrue(info["list_excluded"])
                self.assertTrue(info["list_exclusion_reasons"])
                self.assertTrue(info["list_exclusion_evidence"])
                self.assertEqual(merge_model_review(job, None)["classification"], "暂不投")
                for item in info["list_exclusion_evidence"]:
                    self.assertIn(item["quote"], job[item["field"]])

    def test_ambiguous_algorithm_infra_and_service_work_wait_for_jd(self):
        for title, skills in (
            ("算法工程师", "PyTorch | 计算机视觉"),
            ("大模型算法工程师", ""),
            ("AI Infra训练与推理研发", "容器技术 | 分布式"),
            ("基础架构工程师", "K8S"),
            ("AI运营专员", ""),
            ("AI培训师", ""),
            ("AI客服工程师", ""),
            ("基座模型工程师", "基座模型 | API调用"),
        ):
            with self.subTest(title=title):
                info = score_job({"title": title, "skills": skills})
                self.assertTrue(info["list_pass"])
                self.assertFalse(info["list_excluded"])
                self.assertEqual(info["list_priority"], "低优先待JD")

    def test_adobe_ai_and_coding_tools_do_not_promote_generic_product_or_software(self):
        for job in (
            {"title": "产品经理", "skills": "PS | AE | AI"},
            {"title": "软件开发工程师（应届/AI Coding）", "skills": "Java | Python"},
        ):
            with self.subTest(job=job):
                info = score_job(job)
                self.assertEqual(info["list_pass"], job["title"] == "产品经理")
                self.assertEqual(info["list_priority"], "低优先待JD" if job["title"] == "产品经理" else "暂不优先")
                self.assertNotIn("AI产品/孵化", info["list_families"])
                self.assertNotIn("AI应用/全栈", info["list_families"])

    def test_bare_ai_benefits_or_illustrator_are_not_positive_direction_evidence(self):
        for job in (
            {"title": "AI", "skills": "五险一金 | 校招"},
            {"title": "行政助理", "skills": "AI | 五险一金 | 接受应届"},
            {"title": "培训助理", "skills": "PS | AE | AI"},
        ):
            with self.subTest(job=job):
                info = score_job(job)
                self.assertFalse(info["list_pass"])
                self.assertEqual(info["list_match_evidence"], [])

    def test_cross_target_salary_and_senior_experience_do_not_block_list_or_pool(self):
        job = {
            "title": "资深AI产品经理（校招）", "skills": [], "salary": "12-20K", "tags": "5-10年",
            "observed_in": [{"track": "target", "experience_id": "106"}],
        }
        for jd in ("", "负责方案设计与Agent应用。要求5年以上开发经验。"):
            with self.subTest(jd=jd):
                final = merge_model_review({**job, "jd": jd}, None)
                self.assertTrue(final["list_pass"])
                self.assertEqual(final["classification"], "海投池")
                self.assertIn("核实固定薪资和绩效占比", final["verification"])
                self.assertIn("经验要求偏高，用项目案例争取", final["verification"])
                self.assertTrue(any("实际职级" in item for item in final["verification"]))
                self.assertTrue(any("毕业届别" in item for item in final["verification"]))

    def test_complete_jd_with_zero_or_one_dimension_still_does_not_enter_pool(self):
        for jd, count in (("负责产品竞品调研和竞品资料整理。", 0), ("负责企业方案设计。", 1)):
            with self.subTest(jd=jd):
                final = merge_model_review({"title": "AI产品经理", "skills": [], "jd": jd, "salary": "20-30K"}, None)
                self.assertTrue(final["list_pass"])
                self.assertEqual(final["jd_dimension_count"], count)
                self.assertEqual(final["classification"], "暂不投")

    def test_partial_jd_cannot_be_priority_but_retains_its_actual_dimensions(self):
        cases = (
            ("负责竞品资料整理。", 0, "暂不投"),
            ("负责AI应用方案设计。", 1, "暂不投"),
            ("负责AI应用方案设计、系统集成。", 2, "海投池"),
            ("负责AI应用方案设计、系统集成、Agent工作流、知识库建设。", 4, "海投池"),
            ("负责AI应用方案设计、系统集成、Agent工作流、知识库建设、PoC交付、持续迭代评测。", 6, "海投池"),
        )
        partial_flags = (
            {"jd_completeness": "partial"}, {"jd_completeness": "complete", "truncated": True},
            {"complete": False}, {"jd_complete": False}, {"jd_status": "部分可用"}, {"status": "failed"},
        )
        for jd, count, classification in cases:
            complete = merge_model_review({"title": "AI架构师", "salary": "20-30K", "jd": jd,
                                           "jd_completeness": "complete"}, None)
            self.assertEqual(complete["jd_dimension_count"], count)
            self.assertEqual(complete["classification"], "重点投递" if count >= 4 else classification)
            self.assertFalse(any("待补齐完整正文" in item for item in complete["verification"]))
            for flags in partial_flags:
                with self.subTest(count=count, flags=flags):
                    job = {"title": "AI架构师", "salary": "20-30K", "jd": jd, **flags}
                    original = json.loads(json.dumps(job))
                    partial = merge_model_review(job, None)
                    self.assertEqual(job, original)
                    self.assertEqual(partial["classification"], classification)
                    self.assertEqual(partial["jd_status"], "部分可用")
                    self.assertEqual(partial["jd_dimension_count"], count)
                    self.assertEqual(partial["jd_dimensions"], complete["jd_dimensions"])
                    self.assertEqual(partial["jd_evidence"], complete["jd_evidence"])
                    self.assertTrue(all(quote in jd for quote in partial["jd_evidence"]))
                    self.assertTrue(any("待补齐完整正文" in item for item in partial["verification"]))
                    self.assertEqual(merge_model_review(partial, None)["classification"], classification)
        jd = cases[-1][0]
        review = {"status": "success", "dimensions": {
            name: {"present": True, "evidence": [jd]} for name in DIMENSIONS
        }}
        final = merge_model_review({"title": "AI架构师", "salary": "20-30K", "jd": jd,
                                    "jd_completeness": "partial"}, review)
        self.assertEqual(final["jd_dimension_count"], 6)
        self.assertEqual(final["classification"], "海投池")

    def test_missing_jd_pool_still_requires_list_recall_and_has_no_duty_evidence(self):
        for title, classification in (("AI架构师", "海投池"), ("AI", "暂不投")):
            with self.subTest(title=title):
                final = merge_model_review({"title": title, "salary": "20-30K", "jd": "",
                                            "jd_completeness": "partial"}, None)
                self.assertEqual(final["classification"], classification)
                self.assertEqual(final["jd_status"], "待核实")
                self.assertEqual(final["jd_dimension_count"], 0)
                self.assertEqual(final["jd_evidence"], [])

    def test_jd_application_counterevidence_can_reopen_a_list_sales_exclusion(self):
        job = {"title": "电话销售经理", "skills": "陌拜 | 电话销售", "salary": "10-20K"}
        self.assertTrue(score_job(job)["list_excluded"])
        final = merge_model_review({**job, "jd": "实际负责客户技术方案设计、系统集成和Agent应用开发，不负责陌拜。"}, None)
        self.assertEqual(final["classification"], "海投池")
        self.assertTrue(any("已按JD重新判断" in item for item in final["verification"]))

    def test_salary_parsing_and_target_status_preserve_months(self):
        self.assertEqual(
            parse_salary("17-30K·13薪"),
            {"raw": "17-30K·13薪", "lower_k": 17.0, "upper_k": 30.0, "months": 13},
        )
        self.assertEqual(
            parse_salary("15~20千"),
            {"raw": "15~20千", "lower_k": 15.0, "upper_k": 20.0, "months": None},
        )
        unknown = parse_salary("面议")
        self.assertIsNone(unknown["lower_k"])
        self.assertIsNone(unknown["upper_k"])
        self.assertEqual(salary_status(parse_salary("20-30K")), "达标（下限>=17K）")
        self.assertEqual(salary_status(parse_salary("10-15K")), "低于目标（上限<17K）")
        self.assertEqual(salary_status(parse_salary("15-20K")), "跨17K（下限<17K、上限>=17K）")

    def test_salary_ceiling_below_seventeen_excludes_with_traceable_salary_evidence(self):
        for salary in ("10-16K", "16.9K", "8-12K·16薪", "15~16千", "1-1.6万/月", "15000-16000元/月"):
            with self.subTest(salary=salary):
                job = {"title": "AI解决方案架构师", "skills": ["系统集成", "PoC交付"], "salary": salary}
                info = score_job(job)
                self.assertFalse(info["list_pass"])
                self.assertTrue(info["list_excluded"])
                self.assertTrue(info["list_salary_below_target"])
                self.assertIn("薪资上限低于17K目标", info["list_exclusion_reasons"])
                self.assertIn({"family": "薪资上限低于17K目标", "field": "salary", "quote": salary}, info["list_exclusion_evidence"])

    def test_salary_ceiling_at_or_above_seventeen_and_unknown_do_not_exclude(self):
        salaries = ("10-17K", "17K", "15-20K", "20-30K", "1-2万", "面议", "", None,
                    "15K以上", "10-15K+", "月薪1.5万元起", "15K/月起", "最低16K", "起薪16K", "不低于16K",
                    "150-200元/天", "10-15元/小时", "10-15万/年", "15-10K",
                    "底薪10K+绩效10-15K", "12-16")
        for salary in salaries:
            with self.subTest(salary=salary):
                info = score_job({"title": "AI产品经理", "salary": salary})
                self.assertTrue(info["list_pass"])
                self.assertFalse(info["list_salary_below_target"])

    def test_salary_ceiling_exclusion_cannot_be_reopened_by_jd_or_model_dimensions(self):
        jd = "负责需求分析和方案设计、系统集成、Agent工作流、知识库建设、PoC交付、持续迭代评测。"
        reviews = (None, {"status": "success", "dimensions": {
            name: {"present": True, "evidence": [jd]} for name in DIMENSIONS}})
        for review in reviews:
            with self.subTest(model=bool(review)):
                item = merge_model_review({"title": "AI解决方案架构师", "salary": "10-16K", "jd": jd}, review)
                self.assertEqual(item["jd_dimension_count"], 6)
                self.assertEqual(item["classification"], "暂不投")
                self.assertIn("薪资上限低于17K目标", item["reason"])
                self.assertFalse(any("已按JD重新判断" in text for text in item["verification"]))

    def test_monthly_salary_units_and_nonmonthly_unknown_are_not_confused(self):
        for salary, lower, upper in (("1.5-2万/月", 15.0, 20.0), ("1万-1.7万", 10.0, 17.0),
                                    ("15000-17000元/月", 15.0, 17.0), ("16K", 16.0, 16.0),
                                    ("15K以上", 15.0, None), ("15-20万/年", None, None)):
            with self.subTest(salary=salary):
                parsed = parse_salary(salary)
                self.assertEqual((parsed["lower_k"], parsed["upper_k"]), (lower, upper))

    def test_model_dimensions_override_rules_and_failed_review_falls_back(self):
        quotes = ["与业务团队共同定义AI应用技术蓝图。", "明确AI应用上游系统的调用接口。", "串联Agent多步智能决策链。", "整理企业资料与引用来源。"]
        focus_job = {
            "job_id": "focus",
            "title": "AI 方案岗位",
            "salary": "20-30K",
            "jd": "\n".join(quotes),
            "observed_in": [],
        }
        model_dimensions = {
            name: {"present": index < 4, "evidence": [quotes[index]] if index < 4 else []}
            for index, name in enumerate(DIMENSIONS)
        }
        merged = merge_model_review(
            focus_job,
            {"status": "success", "dimensions": model_dimensions, "ability_gaps": ["gap"]},
        )
        self.assertEqual(merged["jd_dimension_count"], 4)
        self.assertEqual(merged["classification"], "重点投递")
        self.assertEqual(merged["llm_review"]["status"], "success")
        self.assertEqual(merged["ability_gaps"], ["gap"])

        failed = merge_model_review(
            {"job_id": "failed", "title": "AI 方案岗位", "salary": "20-30K", "jd": "负责方案设计", "observed_in": []},
            {"status": "failed", "error": "offline"},
        )
        self.assertEqual(failed["jd_dimension_count"], 1)
        self.assertEqual(failed["classification"], "暂不投")
        self.assertEqual(failed["llm_review"], {"status": "failed", "error": "offline"})

        unknown = merge_model_review(
            {"job_id": "unknown", "title": "AI 方案岗位", "salary": "20-30K", "jd": "", "observed_in": []},
            {
                "status": "success",
                "dimensions": {name: {"present": "unknown", "evidence": []} for name in DIMENSIONS},
            },
        )
        self.assertEqual(unknown["jd_dimension_count"], 0)
        self.assertTrue(all(value["present"] == "unknown" for value in unknown["jd_dimensions"].values()))

    def test_missing_jd_ignores_cached_model_responsibilities_and_exclusions(self):
        review = {
            "status": "success",
            "dimensions": {name: {"present": True, "evidence": ["缓存中的职责"]} for name in DIMENSIONS},
            "exclusions": {"pure_sales": {"present": True, "evidence": ["缓存中的销售指标"]}},
            "ability_gaps": ["缓存能力差距"],
        }
        final = merge_model_review({"title": "AI应用专员", "skills": [], "jd": "", "score": 0, "list_excluded": True}, review)
        self.assertEqual(final["classification"], "海投池")
        self.assertEqual(final["jd_dimension_count"], 0)
        self.assertEqual(final["jd_evidence"], [])
        self.assertEqual(final["model_evidence"], [])
        self.assertEqual(final["ability_gaps"], [])
        self.assertEqual(final["llm_review"], review)
        self.assertTrue(all(value["present"] == "unknown" for value in final["jd_dimensions"].values()))

    def test_unsupported_model_quotes_do_not_invent_jd_evidence_or_duties(self):
        review = {
            "status": "success",
            "dimensions": {name: {"present": True, "evidence": ["从未提供的模型证据"]} for name in DIMENSIONS},
            "exclusions": {"pure_sales": {"present": True, "evidence": ["从未提供的销售指标"]}},
        }
        final = merge_model_review({"title": "AI产品经理", "jd": "负责方案设计", "salary": "20-30K"}, review)
        self.assertEqual(final["jd_dimension_count"], 1)
        self.assertEqual(final["classification"], "暂不投")
        self.assertEqual(final["jd_evidence"], ["负责方案设计"])
        self.assertEqual(final["model_evidence"], [])
        self.assertNotIn("JD以纯销售KPI为主", final["reason"])
        self.assertTrue(any("未从岗位职责确认与目标方向的语义关联" in item for item in final["verification"]))

    def test_target_experience_codes_are_marked_as_high_experience(self):
        result = merge_model_review(
            {
                "job_id": "target",
                "title": "AI 方案岗位",
                "salary": "10-20K",
                "jd": "负责AI应用方案设计并构建Agent工作流。",
                "observed_in": [{"track": "target", "experience_id": "105"}],
            },
            None,
        )
        self.assertEqual(result["classification"], "海投池")
        self.assertIn("经验要求偏高，用项目案例争取", result["verification"])

    def test_jd_experience_parser_handles_common_forms_and_ignores_non_requirements(self):
        cases = [
            ("3 年及以上 C++/Golang 开发经验", 3),
            ("要求3年以上相关工作经验", 3),
            ("具备3年工作经验", 3),
            ("具备3年开发经验", 3),
            ("3年工作/开发经验", 3),
            ("工作经验：3年以上", 3),
            ("相关开发经验不少于3年", 3),
            ("3-5年相关工作经验", 3),
            ("3 年至 5 年 C++ 开发经验", 3),
            ("3～5年项目交付经历", 3),
            ("工作年限：3-5年", 3),
            ("1至3年开发经验", 1),
            ("0-1年工作经验", 0),
            ("2.5年相关开发经验", 2.5),
            ("3年以上工作经验，其中1年以上Agent开发经验", 3),
            ("公司成立3年以上并持续积累交付经验", None),
            ("公司成立于2023年，年薪30-50万元，13薪，合同3年", None),
            ("公司成立3年，要求1年以上开发经验", 1),
            ("工作经验不限，薪资20-30K·13薪", None),
            ("3年以内开发经验", None),
            ("开发经验不超过3年", None),
            ("有3年以上开发经验者优先", None),
            ("1-3年工作经验，5年以上开发经验优先", 1),
            ("5-3年工作经验", None),
            ("加分项\n3年及以上大模型开发经验", None),
            ("任职要求\n1年以上工作经验\n加分项\n5年以上AI经验", 1),
        ]
        for requirement, expected in cases:
            with self.subTest(requirement=requirement):
                parsed = parse_jd_experience(requirement)
                self.assertEqual(parsed["minimum_years"], expected)
                self.assertEqual(bool(parsed["evidence"]), expected is not None)

    def test_jd_high_experience_overrides_unlimited_labels_and_is_traceable_in_report(self):
        requirement = "1、本科及以上学历，计算机相关专业，3 年及以上 C++/Golang 开发经验；"
        job = {
            "job_id": "fde-high-experience",
            "title": "FDE前沿部署工程师（Golang/C++ 方向）",
            "job_link": "https://jobs.test/fde-high-experience",
            "salary": "30-60K",
            "tags": "经验不限 | 本科",
            "jd": "方案设计 系统集成 Agent RAG 知识库 交付 迭代\n任职要求\n" + requirement,
            "observed_in": [{"track": "current", "experience_id": "unlimited", "experience_filter": "101"}],
        }
        reviews = [
            None,
            {"job_id": job["job_id"], "status": "failed", "error": "HTTP 403"},
            {
                "job_id": job["job_id"],
                "status": "success",
                "dimensions": {name: {"present": True, "evidence": [name]} for name in DIMENSIONS},
                "experience_assessment": {"minimum_years": None, "hard_conflict": False, "evidence": []},
            },
        ]
        for review in reviews:
            with self.subTest(model_status=review.get("status") if review else "not_run"):
                result = merge_model_review(job, review)
                self.assertEqual(result["classification"], "海投池")
                self.assertEqual(result["jd_dimension_count"], 6)
                self.assertEqual(result["jd_minimum_years"], 3)
                self.assertIn("3 年及以上 C++/Golang 开发经验", " ".join(result["jd_experience_evidence"]))
                self.assertIn("经验要求偏高，用项目案例争取", result["verification"])
                self.assertIn("JD要求至少3年相关经验，需核实是否为硬性门槛", result["verification"])
                self.assertIn("JD/列表或检索经验标签可能冲突，需核实实际最低年限", result["verification"])
                self.assertEqual(result["tags"], job["tags"])
                self.assertEqual(result["observed_in"], job["observed_in"])

        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            prepared = output_root / "prepared"
            prepared.mkdir()
            (prepared / "merged_jobs_index.json").write_text(
                json.dumps({"jobs": [job]}, ensure_ascii=False), encoding="utf-8"
            )
            (prepared / "llm_reviews.json").write_text(
                json.dumps({"reviews": [reviews[1]]}, ensure_ascii=False), encoding="utf-8"
            )
            summary = report_stage(output_root)
            final = json.loads((prepared / "merged_jobs_index.json").read_text(encoding="utf-8"))
            markdown = (prepared / "海投分层清单.md").read_text(encoding="utf-8")

        self.assertEqual(summary["classification_counts"], {"重点投递": 0, "海投池": 1, "暂不投": 0, "待核实": 0})
        self.assertEqual(final["jobs"][0]["jd_minimum_years"], 3)
        self.assertIn("代码经验评估：JD最低年限：3", markdown)
        self.assertIn("3 年及以上 C++/Golang 开发经验", markdown)
        self.assertIn("JD/列表或检索经验标签可能冲突", markdown)
        self.assertIn("模型状态：failed（规则回退）", markdown)

    def test_low_experience_range_and_company_age_do_not_demote_priority_job(self):
        for experience in ("要求1-3年开发经验", "公司成立3年以上，要求1年以上工作经验"):
            with self.subTest(experience=experience):
                result = merge_model_review(
                    {
                        "job_id": "low-experience",
                        "title": "AI 解决方案架构师",
                        "salary": "20-30K",
                        "tags": "1-3年 | 本科",
                        "jd": "方案设计 系统集成 Agent RAG 知识库 交付 迭代\n" + experience,
                        "observed_in": [{"track": "current", "experience_id": "1_3"}],
                    },
                    None,
                )
                self.assertEqual(result["classification"], "重点投递")
                self.assertEqual(result["jd_minimum_years"], 1)
                self.assertNotIn("经验要求偏高，用项目案例争取", result["verification"])

    def test_report_has_three_mutually_exclusive_layers_unique_ids_and_matching_counts(self):
        focus_jd = "负责AI应用方案设计、系统集成、Agent/RAG工作流建设、知识库治理、PoC交付与效果迭代。"
        pool_jd = "负责AI应用方案设计并搭建Agent工作流。"
        jobs = [
            {
                "job_id": "focus",
                "title": "重点岗位",
                "company": "公司A",
                "job_link": "https://jobs.test/focus",
                "salary": "20-30K",
                "jd": focus_jd,
                "observed_in": [],
            },
            {
                "job_id": "pool",
                "title": "海投岗位",
                "company": "公司B",
                "job_link": "https://jobs.test/pool",
                "salary": "20-30K",
                "jd": pool_jd,
                "observed_in": [],
            },
            {
                "job_id": "skip",
                "title": "销售经理",
                "company": "公司C",
                "job_link": "https://jobs.test/skip",
                "salary": "20-30K",
                "jd": "销售额 回款 签约",
                "list_excluded": True,
                "observed_in": [],
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            prepared = output_root / "prepared"
            prepared.mkdir(parents=True)
            (prepared / "merged_jobs_index.json").write_text(
                json.dumps({"metadata": {"raw_sample_count": 3}, "jobs": jobs}, ensure_ascii=False),
                encoding="utf-8",
            )

            result = report_stage(output_root)
            final = json.loads((prepared / "merged_jobs_index.json").read_text(encoding="utf-8"))
            pool = json.loads((prepared / "hai_tou_pool.json").read_text(encoding="utf-8"))
            markdown = (prepared / "海投分层清单.md").read_text(encoding="utf-8")

        self.assertEqual(result["unique_job_count"], 3)
        self.assertEqual(len(final["jobs"]), 3)
        self.assertEqual(len({job["job_id"] for job in final["jobs"]}), 3)
        self.assertEqual(set(job["classification"] for job in final["jobs"]), set(CLASSIFICATIONS))
        self.assertEqual({job["job_id"] for job in pool["jobs"]}, {"focus", "pool"})
        self.assertEqual(pool["summary"]["total"], 2)
        self.assertEqual(sum(final["metadata"]["classification_counts"].values()), len(final["jobs"]))
        self.assertEqual(sum(final["metadata"]["classification_counts"].values()), len(re.findall(r"^- \[", markdown, flags=re.MULTILINE)))
        overview = markdown.split("以下为全部岗位详细证据。", 1)[0]
        self.assertIn("## 可投总览", overview)
        self.assertIn("全部岗位：3；重点投递：1；海投池：1；暂不投：1；待核实：0。", overview)
        self.assertIn("可投清单：2 岗", overview)
        self.assertIn("[hai_tou_pool.json](hai_tou_pool.json)", overview)
        self.assertEqual(len(re.findall(r"^\| (?:重点投递|海投池) \|", overview, flags=re.MULTILINE)), len(pool["jobs"]))
        for job in pool["jobs"]:
            self.assertIn(f"[{job['title']}]({job['job_link']})", overview)
            self.assertIn(job["company"], overview)
            self.assertIn(job["salary"], overview)
        self.assertNotIn("https://jobs.test/skip", overview)
        for job in final["jobs"]:
            self.assertEqual(len(re.findall(rf"^- \[{re.escape(job['title'])}\]", markdown, flags=re.MULTILINE)), 1)
        for classification in CLASSIFICATIONS:
            match = re.search(rf"^## {re.escape(classification)}（(\d+)）$", markdown, flags=re.MULTILINE)
            self.assertIsNotNone(match)
            self.assertEqual(int(match.group(1)), final["metadata"]["classification_counts"][classification])

    def test_report_recomputes_legacy_gate_and_does_not_reuse_stale_scores_or_exclusions(self):
        jobs = [
            {"job_id": "product", "title": "AI产品经理", "skills": [], "score": 0, "list_pass": False, "list_excluded": True, "list_exclusion_reasons": ["旧黑名单"], "screening_version": "old"},
            {"job_id": "application", "title": "AI应用专员", "skills": [], "score": 0, "list_pass": False, "list_excluded": False},
            {"job_id": "bare-ai", "title": "AI", "skills": [], "score": 99, "list_pass": True, "list_excluded": False},
            {"job_id": "course", "title": "课程顾问", "skills": "线下教育", "score": 99, "list_pass": True, "list_excluded": False},
            {"job_id": "complete-one", "title": "AI产品经理", "skills": [], "jd": "负责方案设计", "score": 99, "list_pass": True, "list_excluded": False},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = root / "prepared"
            prepared.mkdir()
            (prepared / "merged_jobs_index.json").write_text(json.dumps({"metadata": {"screening_version": "old"}, "jobs": jobs}, ensure_ascii=False), encoding="utf-8")
            result = report_stage(root)
            saved = json.loads((prepared / "merged_jobs_index.json").read_text(encoding="utf-8"))
            pool = json.loads((prepared / "hai_tou_pool.json").read_text(encoding="utf-8"))
            markdown = (prepared / "海投分层清单.md").read_text(encoding="utf-8")
            repeated = report_stage(root)
            second = json.loads((prepared / "merged_jobs_index.json").read_text(encoding="utf-8"))

        by_id = {job["job_id"]: job for job in saved["jobs"]}
        self.assertEqual(result["classification_counts"], {"重点投递": 0, "海投池": 2, "暂不投": 3, "待核实": 0})
        self.assertEqual(repeated["classification_counts"], result["classification_counts"])
        self.assertEqual(second["jobs"], saved["jobs"])
        self.assertEqual(len(by_id), len(jobs))
        self.assertEqual({job["job_id"] for job in pool["jobs"]}, {"product", "application"})
        self.assertTrue(by_id["product"]["list_pass"])
        self.assertFalse(by_id["product"]["list_excluded"])
        self.assertEqual(by_id["product"]["list_exclusion_reasons"], [])
        self.assertLess(by_id["product"]["score"], 5)
        self.assertFalse(by_id["bare-ai"]["list_pass"])
        self.assertTrue(by_id["course"]["list_excluded"])
        self.assertEqual(by_id["complete-one"]["classification"], "暂不投")
        self.assertEqual(saved["metadata"]["screening_version"], SCREENING_VERSION)
        self.assertEqual(saved["metadata"]["list_pass_count"], 3)
        for job in saved["jobs"]:
            self.assertEqual(job["screening_version"], SCREENING_VERSION)
            if not job.get("jd"):
                self.assertEqual(job["jd_dimension_count"], 0)
                self.assertEqual(job["jd_evidence"], [])
                self.assertEqual(job["model_evidence"], [])
        self.assertIn(SCREENING_VERSION, markdown)
        self.assertIn("列表方向依据：", markdown)
        self.assertIn("AI产品/孵化 / title：AI", markdown)
        self.assertIn("JD正文未提供", markdown)
        self.assertEqual(len(re.findall(r"^- \[", markdown, flags=re.MULTILINE)), len(jobs))

    def test_report_includes_provenance_evidence_gaps_and_explicit_missing_jd(self):
        jd_lines = [
            "围绕企业大模型应用需求开展方案设计，形成可交付的应用架构方案。",
            "负责业务系统集成、接口权限设计和私有化部署方案评审。",
            "通过 Agent RAG Workflow 编排支撑真实业务场景和多工具调用。",
            "负责建设企业知识库与文档解析流程，并持续治理知识数据来源。",
            "推进客户 PoC 试点和项目交付，组织实施上线与验收。",
            "建立效果评测、监控反馈和持续迭代机制，并跟踪业务质量。",
        ]
        sources = [
            {
                "track": "current",
                "city": "上海",
                "keyword": "AI 应用架构",
                "keyword_id": "ai_arch",
                "salary_filter": "20-50K",
                "salary_id": "20_50",
                "experience_filter": "1-3年",
                "experience_id": "1_3",
                "degree": "本科",
                "combination": "当前__上海__AI架构",
                "source_file": "raw/current/上海/中文|来源.json",
                "source_line_number": 4,
                "source_row": 3,
            },
            {
                "track": "target",
                "city": "上海",
                "keyword": "Agent/RAG 应用",
                "source_file": "raw/target/上海/补充来源.json",
                "source_line_number": 9,
            },
        ]
        jobs = [
            {
                "job_id": "evidence-job",
                "title": "AI [解决方案] | 架构师\n方案负责人",
                "company": "<技术 & 方案公司>",
                "job_link": "https://jobs.test/role(v2)?q=AI Agent|应用",
                "salary": "20-30K·13薪",
                "tags": "1-3年 | 本科",
                "observed_in": sources,
            },
            {
                "job_id": "missing-jd",
                "title": "AI应用专员（缺失JD）",
                "job_link": "https://jobs.test/missing",
                "score": 8,
                "list_excluded": False,
                "observed_in": [],
            },
        ]
        review = {
            "job_id": "evidence-job",
            "status": "success",
            "dimensions": {
                name: {
                    "present": True if index < 4 else "unknown" if index == 4 else False,
                    "evidence": [jd_lines[index]] if index < 4 else [],
                    "confidence": 0.9,
                }
                for index, name in enumerate(DIMENSIONS)
            },
            "experience_assessment": {
                "minimum_years": 1,
                "hard_conflict": False,
                "evidence": ["具备1年以上相关项目经验"],
            },
            "ability_gaps": ["补充私有化部署[案例]"],
            "verification_items": ["确认绩效占比 | 试用期待遇"],
            "summary": "偏企业应用架构与知识工程。",
            "evidence_quality": "high",
        }
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            prepared = output_root / "prepared"
            details = prepared / "jd_shortlists"
            details.mkdir(parents=True)
            (prepared / "merged_jobs_index.json").write_text(
                json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8"
            )
            (details / "details.json").write_text(
                json.dumps([{"job_id": "evidence-job", "jd": "\n".join(jd_lines)}], ensure_ascii=False),
                encoding="utf-8",
            )
            (prepared / "llm_reviews.json").write_text(
                json.dumps({"reviews": [review]}, ensure_ascii=False), encoding="utf-8"
            )

            result = report_stage(output_root)
            final = json.loads((prepared / "merged_jobs_index.json").read_text(encoding="utf-8"))
            markdown = (prepared / "海投分层清单.md").read_text(encoding="utf-8")

        self.assertEqual(result["unique_job_count"], 2)
        self.assertEqual(result["classification_counts"], {"重点投递": 1, "海投池": 1, "暂不投": 0, "待核实": 0})
        self.assertEqual(result["jd_fetched_count"], 1)
        self.assertEqual(len({job["job_id"] for job in final["jobs"]}), 2)
        self.assertEqual(len(re.findall(r"^- \[", markdown, flags=re.MULTILINE)), 2)
        overview = markdown.split("以下为全部岗位详细证据。", 1)[0]
        self.assertIn(r"[AI \[解决方案\] \| 架构师 方案负责人]", overview)
        self.assertIn("https://jobs.test/role%28v2%29?q=AI%20Agent%7C%E5%BA%94%E7%94%A8", overview)
        self.assertIn("&lt;技术 &amp; 方案公司&gt;", overview)
        self.assertNotIn("<技术 & 方案公司>", overview)
        self.assertEqual(len(re.findall(r"^\| (?:重点投递|海投池) \|", overview, flags=re.MULTILINE)), 2)
        detailed, missing = re.findall(r"^- \[.*?(?=^- \[|\Z)", markdown, flags=re.MULTILINE | re.DOTALL)
        self.assertIn(r"- [AI \[解决方案\] \| 架构师 方案负责人]", detailed)
        self.assertIn("https://jobs.test/role%28v2%29?q=AI%20Agent%7C%E5%BA%94%E7%94%A8", detailed)
        self.assertIn("&lt;技术 &amp; 方案公司&gt;", detailed)
        self.assertIn("岗位ID：evidence-job；最终分层：重点投递", detailed)
        self.assertIn("检索来源：", detailed)
        for label in ("轨道：current", "城市：上海", "关键词：AI 应用架构", "薪资筛选：20-50K", "经验筛选：1-3年", "学历筛选：本科", "源行号：4"):
            self.assertIn(label, detailed)
        self.assertIn(r"文件：raw/current/上海/中文\|来源.json", detailed)
        self.assertIn("文件：raw/target/上海/补充来源.json", detailed)
        self.assertIn("六项命中（4/6）", detailed)
        for name in DIMENSIONS[:4]:
            self.assertIn(f"{name}：命中", detailed)
        self.assertIn("PoC/交付：待核实", detailed)
        self.assertIn("迭代与评测：未命中", detailed)
        self.assertIn(f"JD证据：{jd_lines[0]}", detailed)
        self.assertIn(f"模型证据：{jd_lines[0]}", detailed)
        self.assertIn(r"能力差距：补充私有化部署\[案例\]", detailed)
        self.assertIn("薪资状态：达标（下限&gt;=17K）", detailed)
        self.assertIn("经验标签（列表/检索）：1-3年", detailed)
        self.assertIn("最低年限：1；硬性冲突：否；经验依据：具备1年以上相关项目经验", detailed)
        self.assertIn("JD完整度：完整（正文", detailed)
        self.assertNotIn("完整性待核实", detailed)
        self.assertIn("模型状态：success（历史语义结论，缓存未验证）", detailed)
        self.assertIn("历史模型缓存未验证", overview)
        self.assertIn("模型证据质量：high", detailed)
        self.assertIn("模型摘要：偏企业应用架构与知识工程。", detailed)
        self.assertIn(r"待核实：确认绩效占比 \| 试用期待遇", detailed)
        for label in ("检索来源", "JD证据", "模型证据", "能力差距"):
            self.assertIn(f"{label}：未提供（待核实）", missing)
        for name in DIMENSIONS:
            self.assertIn(f"{name}：待核实", missing)
        self.assertIn("JD完整度：缺失（JD正文未提供，待核实）", missing)
        self.assertIn("未运行，规则回退", missing)
        self.assertIn("JD正文未提供，六项职责及岗位要求待核实", missing)
        self.assertNotIn("JD完整度：已抓取", missing)
        self.assertEqual(final["jobs"][0]["observed_in"], sources)
        self.assertEqual(final["jobs"][0]["llm_review"], review)
        self.assertEqual(final["jobs"][0]["ability_gaps"], review["ability_gaps"])
        self.assertIn("JD正文未提供，六项职责及岗位要求待核实", final["jobs"][1]["verification"])
        self.assertTrue(final["jobs"][1]["list_pass"])
        self.assertFalse(final["jobs"][1]["jd_fetched"])

    def test_report_reuses_checkpoint_quality_and_marks_unverified_legacy_text(self):
        jd = "负责AI应用方案设计、系统集成、Agent RAG知识库建设、项目交付与持续迭代。"
        jobs = [
            {"job_id": identity, "title": f"AI应用架构师-{identity}", "salary": "20-30K",
             "job_link": f"https://jobs.test/{identity}", "observed_in": [],
             **({"jd": jd} if identity.startswith("legacy") else {}),
             **({"jd_status": "部分可用"} if identity == "legacy-partial" else {})}
            for identity in ("complete", "partial", "missing", "legacy", "legacy-partial")
        ]
        details = [
            {"job_id": "complete", "jd": jd},
            {"job_id": "partial", "jd": jd, "truncated": True},
            {"job_id": "missing", "jd": ""},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = root / "prepared"
            prepared.joinpath("jd_shortlists").mkdir(parents=True)
            (prepared / "merged_jobs_index.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
            (prepared / "jd_shortlists/details.json").write_text(json.dumps(details), encoding="utf-8")
            result = report_stage(root)
            saved = json.loads((prepared / "merged_jobs_index.json").read_text(encoding="utf-8"))
            markdown = (prepared / "海投分层清单.md").read_text(encoding="utf-8")

        by_id = {job["job_id"]: job for job in saved["jobs"]}
        blocks = {re.search(r"岗位ID：([^；\n]+)", block).group(1): block
                  for block in re.findall(r"^- \[.*?(?=^- \[|\Z)", markdown, flags=re.MULTILINE | re.DOTALL)}
        self.assertEqual(result["classification_counts"], {"重点投递": 2, "海投池": 3, "暂不投": 0, "待核实": 0})
        self.assertEqual(by_id["complete"]["classification"], "重点投递")
        self.assertEqual(by_id["partial"]["classification"], "海投池")
        self.assertEqual(by_id["legacy-partial"]["classification"], "海投池")
        self.assertEqual(by_id["complete"]["jd_completeness"], "complete")
        self.assertEqual(by_id["partial"]["jd_completeness"], "partial")
        self.assertEqual(by_id["missing"]["jd_completeness"], "missing")
        self.assertEqual(by_id["legacy-partial"]["jd_completeness"], "partial")
        self.assertNotIn("jd_completeness", by_id["legacy"])
        self.assertIn(f"JD完整度：完整（正文 {len(jd)} 字符）", blocks["complete"])
        self.assertIn(f"JD完整度：部分（正文 {len(jd)} 字符；待补齐完整正文）", blocks["partial"])
        self.assertIn("JD完整度：缺失（JD正文未提供，待核实）", blocks["missing"])
        self.assertIn(f"JD完整度：待核实（正文 {len(jd)} 字符；旧数据未记录完整度）", blocks["legacy"])
        self.assertIn(f"JD完整度：部分（正文 {len(jd)} 字符；待补齐完整正文）", blocks["legacy-partial"])
        overview = markdown.split("以下为全部岗位详细证据。", 1)[0]
        for label in ("JD完整", "JD部分（待补齐）", "JD缺失（待核实）", "JD完整性待核实（旧数据未记录完整度）"):
            self.assertIn(label, overview)
        self.assertEqual(set(blocks), set(by_id))

    def test_report_completeness_respects_partial_flags_and_does_not_infer_from_length(self):
        cases = [
            ({"jd": "短正文", "jd_completeness": "complete"}, "完整（正文 3 字符）"),
            ({"jd": "短正文", "complete": True}, "完整（正文 3 字符）"),
            ({"jd": "短正文", "jd_complete": True}, "完整（正文 3 字符）"),
            ({"jd": "短正文", "jd_completeness": "partial"}, "部分（正文 3 字符；待补齐完整正文）"),
            ({"jd": "短正文", "jd_completeness": "complete", "truncated": True}, "部分（正文 3 字符；待补齐完整正文）"),
            ({"jd": "短正文", "complete": False}, "部分（正文 3 字符；待补齐完整正文）"),
            ({"jd": "短正文", "jd_status": "部分可用"}, "部分（正文 3 字符；待补齐完整正文）"),
            ({"jd": "", "jd_completeness": "complete"}, "缺失（JD正文未提供，待核实）"),
            ({"jd": "正文" * 100}, "待核实（正文 200 字符；旧数据未记录完整度）"),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                job = {**merge_model_review({"job_id": "quality", "title": "AI应用架构师", **fields}, None), **fields}
                before = json.loads(json.dumps(job))
                lines = _report_job_lines(job)
                self.assertIn(f"JD完整度：{expected}；模型状态：", "\n".join(lines))
                self.assertEqual(job, before)

    def test_report_empty_pool_overview_is_explicit_without_duplicating_detail_entries(self):
        jobs = [{"job_id": "excluded", "classification": "暂不投"}]
        overview = "\n".join(_report_overview_lines(jobs, []))
        self.assertIn("全部岗位：1；重点投递：0；海投池：0；暂不投：1；待核实：0。", overview)
        self.assertIn("可投清单：0 岗", overview)
        self.assertIn("本次没有可投岗位", overview)
        self.assertNotRegex(overview, r"(?m)^- \[")
        self.assertNotIn("| 分层 |", overview)

    def test_report_marks_failed_model_as_fallback_without_inventing_evidence_or_gaps(self):
        jd = "负责AI应用方案设计、系统集成、Agent和RAG知识库建设、项目交付与持续迭代。"
        job = {
            "job_id": "failed-review",
            "title": "AI 解决方案架构师",
            "job_link": "https://jobs.test/failed-review",
            "salary": "20-30K",
            "jd": jd,
            "observed_in": [],
        }
        review = {"job_id": "failed-review", "status": "failed", "error": "HTTP 403"}
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            prepared = output_root / "prepared"
            prepared.mkdir()
            (prepared / "merged_jobs_index.json").write_text(
                json.dumps({"jobs": [job]}, ensure_ascii=False), encoding="utf-8"
            )
            (prepared / "llm_reviews.json").write_text(
                json.dumps({"reviews": [review]}, ensure_ascii=False), encoding="utf-8"
            )

            result = report_stage(output_root)
            final = json.loads((prepared / "merged_jobs_index.json").read_text(encoding="utf-8"))
            markdown = (prepared / "海投分层清单.md").read_text(encoding="utf-8")

        self.assertEqual(result["classification_counts"], {"重点投递": 1, "海投池": 0, "暂不投": 0, "待核实": 0})
        self.assertIn(f"JD证据：{jd}", markdown)
        self.assertIn("模型状态：failed（规则回退）", markdown)
        self.assertIn("模型失败，规则回退", markdown.split("以下为全部岗位详细证据。", 1)[0])
        self.assertIn("模型证据：未提供（待核实）", markdown)
        self.assertIn("能力差距：未提供（待核实）", markdown)
        self.assertIn("模型未完成，能力差距与语义结论待核实", markdown)
        self.assertEqual(final["jobs"][0]["model_status"], "failed")
        self.assertEqual(final["jobs"][0]["model_error"], "HTTP 403")
        self.assertEqual(final["jobs"][0]["model_evidence"], [])
        self.assertEqual(final["jobs"][0]["ability_gaps"], [])
        self.assertEqual(final["jobs"][0]["llm_review"], review)

    def test_llm_stage_respects_worker_limit_and_checkpoints_successes(self):
        class ConcurrentClient:
            def __init__(self):
                self.lock = threading.Lock()
                self.active = 0
                self.max_active = 0

            def review_job(self, job, *, prompt):
                self.assert_prompt(prompt)
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.01)
                with self.lock:
                    self.active -= 1
                return {"判断": "符合", "理由": "负责AI方案与交付。"}

            @staticmethod
            def assert_prompt(prompt):
                if prompt != "prompt-for-test":
                    raise AssertionError("prompt was not passed by keyword")

        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            prepared = output_root / "prepared"
            shortlist_dir = prepared / "jd_shortlists"
            shortlist_dir.mkdir(parents=True)
            jobs = [
                {"job_id": f"job-{index}", "title": "AI 架构岗位", "salary": "20-30K"}
                for index in range(12)
            ]
            (prepared / "merged_jobs_index.json").write_text(
                json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8"
            )
            (shortlist_dir / "jd_input.json").write_text(
                json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8"
            )
            (shortlist_dir / "details.json").write_text(
                json.dumps([{"job_id": job["job_id"], "jd": "AI 方案设计和交付"} for job in jobs], ensure_ascii=False),
                encoding="utf-8",
            )
            fake_client = ConcurrentClient()
            from llm_client import CodexConfig
            config = CodexConfig(model="test-model", reasoning_effort="low", base_url="https://provider.test/v1", api_key="test-only-key")
            codex_home = output_root / "codex-home"
            with patch("llm_client.load_codex_config", return_value=config) as load_config, patch(
                "llm_client.CodexResponsesClient", return_value=fake_client
            ), patch("process_architecture.load_prompt", return_value="prompt-for-test"):
                result = run_llm(output_root, workers=3, codex_home=str(codex_home))

            self.assertEqual(load_config.call_args.kwargs["config_path"], codex_home / "config.toml")
            self.assertEqual(load_config.call_args.kwargs["auth_path"], codex_home / "auth.json")
            self.assertIsNone(load_config.call_args.kwargs["project_settings_path"])
            self.assertEqual(result["candidate_count"], 12)
            self.assertEqual(result["completed_count"], 12)
            self.assertEqual(result["pending_count"], 0)
            self.assertLessEqual(fake_client.max_active, 3)
            review_json = json.loads((prepared / "llm_reviews.json").read_text(encoding="utf-8"))
            review_lines = (prepared / "llm_reviews.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(review_json["reviews"]), 12)
            self.assertEqual(len(review_lines), 12)

    def test_llm_reuses_parallel_conversations_and_rotates_after_fifteen_jobs(self):
        class ConversationClient:
            supports_conversations = True

            def __init__(self):
                self.lock = threading.Lock()
                self.next_session = 0
                self.sequences = {}

            def review_job(self, job, *, prompt, conversation=None):
                if prompt != "prompt-for-test" or not isinstance(conversation, dict):
                    raise AssertionError("conversation state or prompt was not passed")
                with self.lock:
                    previous = conversation.get("previous_response_id")
                    if previous:
                        session, turn = previous.split(":")
                        turn = int(turn) + 1
                    else:
                        self.next_session += 1
                        session, turn = f"session-{self.next_session}", 1
                        self.sequences[session] = []
                    self.sequences[session].append(job["job_id"])
                    conversation["previous_response_id"] = f"{session}:{turn}"
                return {"判断": "符合", "理由": "负责AI方案与交付。"}

        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            prepared = output_root / "prepared"
            shortlist_dir = prepared / "jd_shortlists"
            shortlist_dir.mkdir(parents=True)
            jobs = [{"job_id": f"job-{index}", "title": "AI 应用架构师", "salary": "20-30K"}
                    for index in range(31)]
            (prepared / "merged_jobs_index.json").write_text(
                json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8"
            )
            (shortlist_dir / "jd_input.json").write_text(
                json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8"
            )
            (shortlist_dir / "details.json").write_text(
                json.dumps([{"job_id": job["job_id"], "jd": "设计并交付 AI 应用"} for job in jobs],
                           ensure_ascii=False), encoding="utf-8"
            )
            client = ConversationClient()
            from llm_client import CodexConfig
            config = CodexConfig(model="test-model", reasoning_effort="low",
                                 base_url="https://provider.test/v1", api_key="test-only-key")
            with patch("llm_client.load_codex_config", return_value=config), \
                 patch("llm_client.CodexResponsesClient", return_value=client), \
                 patch("process_architecture.load_prompt", return_value="prompt-for-test"):
                stats = run_llm(output_root, workers=2, conversation_size=15)

            self.assertEqual((stats["candidate_count"], stats["completed_count"], stats["failed_count"]), (31, 31, 0))
            self.assertEqual(stats["conversation_mode"], "parallel_reused_sessions")
            self.assertEqual(stats["conversation_session_size"], 15)
            self.assertEqual(stats["conversation_sessions_started"], 3)
            self.assertEqual(stats["conversation_followup_count"], 28)
            self.assertEqual(sorted(map(len, client.sequences.values())), [1, 15, 15])
            review_payload = json.loads((prepared / "llm_reviews.json").read_text(encoding="utf-8"))
            self.assertEqual(len(review_payload["reviews"]), 31)
            self.assertTrue(all("previous_response_id" not in row and "conversation" not in row
                                for row in review_payload["reviews"]))

    def test_llm_luna_max_defaults_refresh_legacy_without_changing_codex_config(self):
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            prepared = output_root / "prepared"
            shortlist_dir = prepared / "jd_shortlists"
            shortlist_dir.mkdir(parents=True)
            codex_home = output_root / "codex-home"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            auth_path = codex_home / "auth.json"
            config_path.write_text(
                'model_provider = "local-provider"\n'
                'model = "configured-model"\n'
                'model_reasoning_effort = "high"\n'
                '[model_providers.local-provider]\n'
                'base_url = "http://provider.test/v1"\n'
                'requires_openai_auth = true\n',
                encoding="utf-8",
            )
            auth_path.write_text('{"OPENAI_API_KEY": "fixture-secret"}', encoding="utf-8")
            config_before, auth_before = config_path.read_bytes(), auth_path.read_bytes()
            project_settings_path = output_root / "missing-project-llm.json"
            jobs = [
                {"job_id": "cached-job", "title": "AI应用工程师"},
                {"job_id": "pending-job", "title": "AI解决方案工程师"},
            ]
            (prepared / "merged_jobs_index.json").write_text(
                json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8"
            )
            (shortlist_dir / "jd_input.json").write_text(
                json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8"
            )
            (shortlist_dir / "details.json").write_text(
                json.dumps([{"job_id": job["job_id"], "jd": "负责AI方案设计与交付"} for job in jobs], ensure_ascii=False),
                encoding="utf-8",
            )
            cached = {"job_id": "cached-job", "status": "success", "review_reasoning_effort": "high"}
            (prepared / "llm_reviews.json").write_text(
                json.dumps({"reviews": [cached]}), encoding="utf-8"
            )
            with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-secret"}), \
                 patch("project_llm_settings.default_settings_path", return_value=project_settings_path), patch(
                "llm_client.CodexResponsesClient"
            ) as factory, patch("process_architecture.load_prompt", return_value="test prompt"):
                factory.return_value.review_job.return_value = {
                    "判断": "符合", "理由": "负责AI应用方案与交付。",
                }
                result = run_llm(output_root, workers=8, codex_home=str(codex_home))
                resumed = run_llm(output_root, workers=8, codex_home=str(codex_home))

            actual_config = factory.call_args.args[0]
            self.assertEqual(actual_config.model, "gpt-5.6-luna")
            self.assertEqual(actual_config.base_url, "http://provider.test/v1")
            self.assertEqual(actual_config.reasoning_effort, "max")
            self.assertEqual(actual_config.api_key, "fixture-secret")
            self.assertEqual(config_path.read_bytes(), config_before)
            self.assertEqual(auth_path.read_bytes(), auth_before)
            self.assertEqual(factory.return_value.review_job.call_count, 2)
            self.assertEqual({call.args[0]["job_id"] for call in factory.return_value.review_job.call_args_list},
                             {"cached-job", "pending-job"})
            self.assertEqual(result["completed_count"], 2)
            self.assertEqual((result["cache_hit_count"], result["cache_legacy_count"], result["request_count"]), (0, 1, 2))
            self.assertEqual((resumed["cache_hit_count"], resumed["request_count"], resumed["pending_count"]), (2, 0, 0))
            self.assertEqual(result["fallback_count"], 0)
            self.assertEqual(result["reasoning_effort"], "max")
            reviews = json.loads((prepared / "llm_reviews.json").read_text(encoding="utf-8"))["reviews"]
            self.assertEqual({review["job_id"] for review in reviews}, {"cached-job", "pending-job"})
            document = json.loads((prepared / "llm_reviews.json").read_text(encoding="utf-8"))
            self.assertEqual(document["previous_reviews"], [{"reason": "missing_signature", "review": cached}])
            self.assertTrue(all(review.get("cache_signature", "").startswith("semantic-review-v1:") for review in reviews))
            pending_review = next(review for review in reviews if review["job_id"] == "pending-job")
            self.assertEqual(pending_review["review_model"], "gpt-5.6-luna")
            self.assertEqual(pending_review["review_reasoning_effort"], "max")
            self.assertEqual(pending_review["source"], "configured_responses_api")
            self.assertNotIn("fixture-secret", (prepared / "llm_manifest.json").read_text(encoding="utf-8"))

    def _prepare_fetch_jd_fixture(self, directory: str, job_count: int = 1) -> tuple[Path, Path]:
        output_root = Path(directory)
        shortlist_dir = output_root / "prepared" / "jd_shortlists"
        shortlist_dir.mkdir(parents=True)
        jobs = [{"job_id": f"job-{index}", "title": "AI 架构岗位"} for index in range(1, job_count + 1)]
        (shortlist_dir / "jd_input.json").write_text(json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8")
        scraper_root = output_root / "scraper"
        entry = scraper_root / "scripts" / "boss_cdp_raw.py"
        entry.parent.mkdir(parents=True)
        entry.write_text("# test fixture\n", encoding="utf-8")
        return output_root, scraper_root

    def test_cli_keeps_ai_and_jd_concurrency_separate_and_defaults_ai_to_ten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("process_architecture.fetch_jd", return_value={}) as fetch, \
                 patch("process_architecture.run_llm", return_value={}) as llm, \
                 redirect_stdout(io.StringIO()):
                main(["--root", str(root), "--stage", "fetch-jd", "--scraper-root", str(root / "scraper"),
                      "--jd-workers", "7", "--workers", "12", "--batch-size", "30"])
                self.assertEqual(fetch.call_args.kwargs["jd_workers"], 7)
                self.assertEqual(fetch.call_args.args[2], 30)
                main(["--root", str(root), "--stage", "llm"])
                self.assertEqual(llm.call_args.args[1], 10)
                self.assertEqual(llm.call_args.args[-2:], (None, None))
                main(["--root", str(root), "--stage", "llm",
                      "--model", "explicit-model", "--reasoning-effort", "low"])
                self.assertEqual(llm.call_args.args[-2:], ("explicit-model", "low"))

    def test_cli_chat_protocol_override_is_forwarded_only_to_llm(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("process_architecture.run_llm", return_value={}) as llm, redirect_stdout(io.StringIO()):
                main(["--root", directory, "--stage", "llm", "--api-mode", "chat_completions"])
            self.assertEqual(llm.call_args.kwargs["api_mode"], "chat_completions")

    def test_fatal_adapter_exit_reaches_cli_and_all_stops_before_ai_on_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for status, code, expected in (("failed", 1, 1), ("paused", 2, 0)):
                manifest = {"records": [{"status": status, "exit_code": code}]}
                with self.subTest(status=status), \
                     patch("process_architecture.merge_stage"), \
                     patch("process_architecture.fetch_jd", return_value={"manifest": manifest}), \
                     patch("process_architecture.run_llm") as llm, \
                     patch("process_architecture.report_stage") as report, redirect_stdout(io.StringIO()):
                    result = main(["--root", str(root), "--stage", "all", "--scraper-root", str(root / "scraper")])
                    self.assertEqual(result, expected)
                    llm.assert_not_called()
                    report.assert_not_called()

    def test_invalid_concurrency_is_rejected_before_writing_or_starting_network(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "must-stay-absent"
            for flag, value in (("--jd-workers", "0"), ("--jd-workers", "33"),
                                ("--workers", "0"), ("--workers", "33")):
                with self.subTest(flag=flag, value=value), patch("sys.stderr", io.StringIO()), \
                     self.assertRaises(SystemExit):
                    main(["--root", str(output), "--stage", "merge", flag, value])
            self.assertFalse(output.exists())
            with self.assertRaises(ValueError):
                fetch_jd(output, output, jd_workers=True)
            with self.assertRaises(ValueError):
                run_llm(output, workers=0)

    def test_partial_jd_does_not_read_credentials_or_call_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            output, _ = self._prepare_fetch_jd_fixture(directory)
            prepared = output / "prepared"
            jobs = [{"job_id": "job-1", "title": "AI解决方案架构师"}]
            (prepared / "merged_jobs_index.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
            (prepared / "jd_shortlists/details.json").write_text(
                json.dumps([{"job_id": "job-1", "jd": "尚未完整的方案职责", "truncated": True}]), encoding="utf-8")
            with patch("llm_client.load_codex_config", side_effect=AssertionError("incomplete body must not load credentials")):
                result = run_llm(output)
            self.assertEqual(result["candidate_count"], 0)
            self.assertEqual(result["workers"], 10)
            self.assertEqual(result["status"], "skipped_no_candidates")

    @patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None))
    def test_fetch_jd_dispatches_project_multitab_adapter_and_records_settings(self, _ensure_cdp):
        with tempfile.TemporaryDirectory() as directory:
            output, scraper = self._prepare_fetch_jd_fixture(directory)
            with patch("process_architecture.run_detail_subprocess",
                       return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                 redirect_stdout(io.StringIO()):
                result = fetch_jd(output, scraper, python_path=sys.executable, jd_workers=6)
            command = run.call_args.args[0]
            self.assertEqual(Path(command[2]), PROJECT_ROOT / "jd_parallel.py")
            self.assertEqual(command[command.index("--workers") + 1], "6")
            self.assertEqual(command[command.index("--scraper-root") + 1], str(scraper))
            self.assertNotIn("--max-details", command)
            self.assertEqual(result["manifest"]["jd_workers"], 6)
            self.assertEqual(result["manifest"]["scroll_count"], 3)

    @patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None))
    def test_fetch_jd_gate_enter_retries_current_batch_then_continues(self, _ensure_cdp):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root = self._prepare_fetch_jd_fixture(directory, job_count=2)
            calls = []

            def run_side_effect(command, **kwargs):
                calls.append(command)
                if len(calls) == 1:
                    return subprocess.CompletedProcess(command, 1, "登录状态异常，请完成安全验证\n", "")
                detail_output = Path(command[command.index("--detail-output") + 1])
                job_id = "job-1" if len(calls) == 2 else "job-2"
                detail_output.write_text(json.dumps([{"job_id": job_id, "jd": "方案设计"}], ensure_ascii=False), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "抓取完成\n", "")

            interactive_stdin = SimpleNamespace(isatty=lambda: True)
            with patch("process_architecture.run_detail_subprocess", side_effect=run_side_effect), patch(
                "process_architecture.sys.stdin", interactive_stdin
            ), patch("builtins.input", return_value="") as prompt:
                result = fetch_jd(output_root, scraper_root, batch_size=1, python_path=sys.executable)

            self.assertEqual(len(calls), 3)
            self.assertEqual(prompt.call_count, 1)
            self.assertEqual({record["status"] for record in result["manifest"]["records"]}, {"success"})
            self.assertEqual(result["details"], 2)
            self.assertNotIn("paused", (output_root / "prepared" / "jd_shortlists" / "details_manifest.json").read_text(encoding="utf-8"))

    @patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None))
    def test_fetch_jd_gate_q_keeps_paused_and_does_not_retry(self, _ensure_cdp):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root = self._prepare_fetch_jd_fixture(directory, job_count=2)
            with patch(
                "process_architecture.run_detail_subprocess",
                return_value=subprocess.CompletedProcess([], 1, "security_check required\n", ""),
            ) as run, patch("process_architecture.sys.stdin", SimpleNamespace(isatty=lambda: True)), patch(
                "builtins.input", return_value="q"
            ):
                result = fetch_jd(output_root, scraper_root, batch_size=1, python_path=sys.executable)

            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(result["manifest"]["records"]), 1)
            self.assertEqual(result["manifest"]["records"][0]["status"], "paused")
            self.assertTrue(result["manifest"]["records"][0]["security_or_login_gate"])

    @patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None))
    def test_fetch_jd_gate_noninteractive_keeps_paused_without_input(self, _ensure_cdp):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root = self._prepare_fetch_jd_fixture(directory, job_count=2)
            with patch(
                "process_architecture.run_detail_subprocess",
                return_value=subprocess.CompletedProcess([], 1, "验证码\n", ""),
            ) as run, patch("process_architecture.sys.stdin", SimpleNamespace(isatty=lambda: False)), patch(
                "builtins.input"
            ) as prompt:
                result = fetch_jd(output_root, scraper_root, batch_size=1, python_path=sys.executable)

            self.assertEqual(run.call_count, 1)
            prompt.assert_not_called()
            self.assertEqual(len(result["manifest"]["records"]), 1)
            self.assertEqual(result["manifest"]["records"][0]["status"], "paused")

    @patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None))
    def test_fetch_jd_no_wait_gate_stops_all_batches_and_keeps_partial_checkpoint(self, _ensure_cdp):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root = self._prepare_fetch_jd_fixture(directory, job_count=5)
            shortlist_dir = output_root / "prepared" / "jd_shortlists"
            existing_detail = {"job_id": "job-1", "jd": "原有方案设计详情"}
            (shortlist_dir / "details.json").write_text(
                json.dumps([existing_detail], ensure_ascii=False), encoding="utf-8"
            )

            def run_side_effect(command, **kwargs):
                input_path = Path(command[command.index("--input") + 1])
                pending = json.loads(input_path.read_text(encoding="utf-8"))["jobs"]
                self.assertEqual([job["job_id"] for job in pending], ["job-2", "job-3"])
                detail_output = Path(command[command.index("--detail-output") + 1])
                detail_output.write_text(
                    json.dumps([{"job_id": "job-2", "jd": "新采集的方案交付详情"}], ensure_ascii=False),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 1, "security_check required\n", "")

            with patch("process_architecture.run_detail_subprocess", side_effect=run_side_effect) as run, patch(
                "builtins.input"
            ) as prompt:
                result = fetch_jd(
                    output_root, scraper_root, batch_size=3, python_path=sys.executable, wait_on_gate=False
                )

            self.assertEqual(run.call_count, 1)
            prompt.assert_not_called()
            self.assertEqual(result["details"], 2)
            self.assertEqual(len(result["manifest"]["records"]), 1)
            record = result["manifest"]["records"][0]
            self.assertEqual(record["status"], "paused")
            self.assertEqual(record["fetched_count"], 2)
            self.assertEqual(record["missing_job_ids"], ["job-3"])
            details = json.loads((shortlist_dir / "details.json").read_text(encoding="utf-8"))
            self.assertEqual(details[0], existing_detail)
            self.assertEqual({item["job_id"] for item in details}, {"job-1", "job-2"})
            self.assertFalse((shortlist_dir / "detail_batches" / "jd_input_0002.json").exists())

    @patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None))
    def test_fetch_jd_non_gate_failure_does_not_wait_or_launch_later_batches(self, _ensure_cdp):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root = self._prepare_fetch_jd_fixture(directory, job_count=2)
            with patch(
                "process_architecture.run_detail_subprocess",
                return_value=subprocess.CompletedProcess([], 2, "network failure\n", ""),
            ) as run, patch("builtins.input") as prompt:
                result = fetch_jd(output_root, scraper_root, batch_size=1, python_path=sys.executable)

            self.assertEqual(run.call_count, 1)
            prompt.assert_not_called()
            self.assertEqual(len(result["manifest"]["records"]), 1)
            self.assertEqual(result["manifest"]["records"][0]["status"], "failed")

    @patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None))
    def test_fetch_jd_redacts_credentials_from_persisted_log(self, _ensure_cdp):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root = self._prepare_fetch_jd_fixture(directory)
            output = "Authorization: Bearer secret-token\nCookie: sid=secret-cookie\ntoken=secret-value\n"
            with patch(
                "process_architecture.run_detail_subprocess",
                return_value=subprocess.CompletedProcess([], 2, output, ""),
            ):
                fetch_jd(output_root, scraper_root, batch_size=1, python_path=sys.executable)

            log = (output_root / "prepared" / "jd_shortlists" / "detail_batches" / "details_0001.log").read_text(encoding="utf-8")
            self.assertNotIn("secret-token", log)
            self.assertNotIn("secret-cookie", log)
            self.assertNotIn("secret-value", log)
            self.assertIn("[REDACTED]", log)

    @patch("process_architecture.ensure_detail_cdp_ready", return_value=(False, "CDP 启动失败"))
    def test_fetch_jd_pauses_without_running_scraper_when_cdp_setup_fails(self, _ensure_cdp):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root = self._prepare_fetch_jd_fixture(directory, job_count=2)
            with patch("process_architecture.run_detail_subprocess") as run:
                result = fetch_jd(output_root, scraper_root, batch_size=1, python_path=sys.executable)

            run.assert_not_called()
            self.assertEqual(len(result["manifest"]["records"]), 1)
            record = result["manifest"]["records"][0]
            self.assertEqual(record["status"], "paused")
            self.assertEqual(record["pause_reason"], "cdp_setup_failed")
            self.assertEqual(result["details"], 0)
            self.assertFalse(
                (output_root / "prepared" / "jd_shortlists" / "detail_batches" / "details_0002.log").exists()
            )

    @patch("process_architecture.local_cdp_ready", side_effect=[False, True])
    def test_ensure_detail_cdp_ready_opens_browser_then_checks_saved_login(self, _cdp_ready):
        entry = Path("C:/scraper/scripts/boss_cdp_raw.py")
        python = Path("C:/scraper/.venv/Scripts/python.exe")
        with patch(
            "process_architecture.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run, patch("process_architecture.browser_setup.open_dedicated_browser") as launch:
            from process_architecture import ensure_detail_cdp_ready

            ready, error = ensure_detail_cdp_ready(entry, python)

        self.assertTrue(ready)
        self.assertIsNone(error)
        launch.assert_called_once_with("chrome", 9222, None, show_window=True)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [str(python), "-u", str(entry), "--check", "--cdp-port", "9222"])

    @patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None))
    def test_fetch_jd_subprocess_bypasses_proxy_for_loopback(self, _ensure_cdp):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root = self._prepare_fetch_jd_fixture(directory)
            with patch.dict("os.environ", {"HTTP_PROXY": "http://proxy.invalid:8888"}, clear=True):
                with patch(
                    "process_architecture.run_detail_subprocess",
                    return_value=subprocess.CompletedProcess([], 2, "network failure\n", ""),
                ) as run:
                    fetch_jd(output_root, scraper_root, batch_size=1, python_path=sys.executable)

            child_env = run.call_args.kwargs["env"]
            self.assertIn("127.0.0.1", child_env["NO_PROXY"])
            self.assertIn("localhost", child_env["NO_PROXY"])
            self.assertEqual(child_env["HTTP_PROXY"], "http://proxy.invalid:8888")
            command = run.call_args.args[0]
            self.assertIn("--detail-speed-profile", command)
            self.assertEqual(command[command.index("--detail-speed-profile") + 1], "fast")

    def test_detail_subprocess_streams_redacted_output_and_returns_full_log(self):
        class FakeProcess:
            def __init__(self):
                self.stdout = io.StringIO("[1/2] JD: 120 字\nAuthorization: Bearer test-secret\n")

            def wait(self):
                return 0

            def terminate(self):
                return None

        displayed = io.StringIO()
        callbacks = []
        with patch("process_architecture.subprocess.Popen", return_value=FakeProcess()):
            with redirect_stdout(displayed):
                result = run_detail_subprocess(["python", "scraper.py"], env={}, on_line=callbacks.append)

        self.assertEqual(result.returncode, 0)
        self.assertIn("JD: 120 字", result.stdout)
        self.assertIn("test-secret", result.stdout)  # retained for gate parsing, not displayed or persisted
        self.assertIn("[1/2] JD: 120 字", displayed.getvalue())
        self.assertNotIn("test-secret", displayed.getvalue())
        self.assertEqual(len(callbacks), 2)


class SimpleReviewReportTests(unittest.TestCase):
    def test_current_manifest_with_no_records_keeps_config_failure_partial_jd_and_interruption_pending(self):
        from llm_client import CodexConfig, ConfigurationError
        config = CodexConfig(model="fixture", base_url="https://fixture.test/v1", api_key="fixture-key")
        for outcome in ("configuration_failed", "skipped_no_candidates", "running"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                folder = output / "prepared/jd_shortlists"
                folder.mkdir(parents=True)
                job = {"job_id": "pending", "title": "Java开发工程师", "salary": "8-12K"}
                detail = {"job_id": "pending", "jd": "负责Java代码开发。",
                          "jd_completeness": "partial" if outcome == "skipped_no_candidates" else "complete"}
                for path, value in ((output / "prepared/merged_jobs_index.json", {"jobs": [job]}),
                                    (folder / "jd_input.json", {"jobs": [job]}), (folder / "details.json", [detail])):
                    path.write_text(json.dumps(value), encoding="utf-8")
                client = Mock()
                client.review_job.side_effect = KeyboardInterrupt("fixture interruption")
                with patch("project_llm_settings.default_settings_path", return_value=output / "absent-settings.json"), \
                        patch("llm_client.load_codex_config", side_effect=ConfigurationError("unavailable")
                              if outcome == "configuration_failed" else None, return_value=config) as loader, \
                        patch("llm_client.CodexResponsesClient", return_value=client), \
                        patch("process_architecture.load_prompt", return_value="fixture rules"):
                    if outcome == "running":
                        with self.assertRaises(KeyboardInterrupt):
                            run_llm(output, workers=1)
                    else:
                        run_llm(output, workers=1)
                if outcome == "skipped_no_candidates":
                    loader.assert_not_called()
                manifest = json.loads((output / "prepared/llm_manifest.json").read_text(encoding="utf-8"))
                self.assertEqual((manifest["review_format"], manifest["status"]), ("simple-v1", outcome))
                checkpoint = json.loads((output / "prepared/llm_reviews.json").read_text(encoding="utf-8"))
                self.assertEqual(checkpoint["reviews"], [])
                with patch("process_architecture.rule_analyze_jd", side_effect=AssertionError("no legacy rejudge")), \
                        patch("llm_client.load_codex_config", side_effect=AssertionError("report remains offline")):
                    report_stage(output)
                item = json.loads((output / "prepared/merged_jobs_index.json").read_text(encoding="utf-8"))["jobs"][0]
                self.assertEqual(item["classification"], "待核实")
                self.assertTrue(item["analysis_pending"])
                self.assertIsNone(item["ai_judgment"])
                self.assertIsNone(item["ai_reason"])
                self.assertNotEqual(item["model_status"], "success")

    def test_rejected_new_checkpoint_remains_pending_and_clears_stale_ai_fields(self):
        from process_architecture import analyze_report_job
        from review_cache import review_input_signature
        job = {"job_id": "changed", "title": "纯开发", "jd": "完整职责正文。", "jd_completeness": "complete",
               "ai_judgment": "不符合", "ai_reason": "stale"}
        review = {"job_id": "changed", "status": "success", "review_format": "simple-v1",
                  "判断": "符合", "理由": "相关。", "cache_signature": "signature",
                  "cache_input_signature": review_input_signature(model_input(job), "old rules")}
        with patch("process_architecture.rule_analyze_jd", side_effect=AssertionError("no old fallback")):
            result = analyze_report_job(job, review, prompt="changed rules")
        self.assertEqual(result["model_cache_validation"], "input_changed")
        self.assertEqual(result["classification"], "待核实")
        self.assertTrue(result["analysis_pending"])
        self.assertIsNone(result["ai_judgment"])
        self.assertIsNone(result["ai_reason"])

    def test_new_failed_missing_and_invalid_reviews_stay_pending_without_legacy_rejudge(self):
        job = {"job_id": "pending", "title": "Java纯开发", "salary": "8-12K", "review_format": "simple-v1",
               "ai_judgment": "不符合", "ai_reason": "stale", "jd": "负责代码开发。", "jd_completeness": "complete"}
        for review in (None, {"status": "failed", "review_format": "simple-v1", "error": "request failed"},
                       {"status": "success", "review_format": "simple-v1", "dimensions": {}}):
            with self.subTest(review=review), patch("process_architecture.rule_analyze_jd", side_effect=AssertionError("no fallback")):
                result = merge_model_review(job, review)
                self.assertEqual(result["classification"], "海投池")
                self.assertTrue(result["analysis_pending"])
                self.assertIsNone(result["ai_judgment"])
                self.assertIsNone(result["ai_reason"])
                self.assertNotEqual(result["model_status"], "success")
                rendered = "\n".join(_report_job_lines(result))
                self.assertNotIn("六项", rendered)
                self.assertNotIn("规则回退", rendered)

    def test_two_field_decisions_bypass_legacy_dimensions_keywords_salary_and_scores(self):
        for judgment, classification in (("符合", "海投池"), ("不符合", "暂不投"), ("信息不足", "海投池")):
            with self.subTest(judgment=judgment):
                review = {"status": "success", "review_format": "simple-v1", "判断": judgment,
                          "理由": "按实际工作内容判断。"}
                job = {"job_id": "simple", "title": "Java工程师", "salary": "8-12K",
                       "jd": "负责所述工作。", "jd_completeness": "complete",
                       "classification": "重点投递", "jd_dimensions": {"stale": True},
                       "jd_dimension_count": 6, "target_relevance": {"assessment": "direct"},
                       "model_evidence": ["stale evidence"], "ability_gaps": ["stale gaps"]}
                with patch("process_architecture.rule_analyze_jd", side_effect=AssertionError("no six-dimension gate")):
                    result = merge_model_review(job, review)
                self.assertEqual(result["classification"], classification)
                self.assertEqual(result["ai_judgment"], judgment)
                self.assertEqual(result["reason"], review["理由"])
                self.assertEqual(result["analysis_pending"], judgment == "信息不足")
                self.assertTrue(any("薪资" in note for note in result["verification"]))
                for field in ("jd_dimensions", "jd_dimension_count", "target_relevance", "model_evidence", "ability_gaps"):
                    self.assertNotIn(field, result)
                rendered = "\n".join(_report_job_lines(result))
                self.assertIn("AI判断：" + judgment, rendered)
                for forbidden in ("六项", "/6", "能力差距", "置信度", "模型证据", "stale evidence", "排序分"):
                    self.assertNotIn(forbidden, rendered)

    def test_unsectioned_complete_jd_is_valid_and_years_are_annotation_only(self):
        review = {"status": "success", "review_format": "simple-v1", "判断": "符合", "理由": "相关岗位。"}
        for jd in ("负责企业AI方案设计与交付。", "岗位职责\n负责产品设计。\n任职要求\n需要5年产品经验。"):
            with self.subTest(jd=jd):
                result = merge_model_review({"job_id": "simple", "title": "产品经理", "jd": jd,
                                             "jd_completeness": "complete"}, review)
                self.assertEqual(result["classification"], "海投池")
                self.assertEqual(result["model_status"], "success")
        self.assertEqual(result["jd_minimum_years"], 5)
        self.assertTrue(result["jd_experience_evidence"])

    def test_cli_report_preserves_current_two_fields_without_fabricating_rich_judgments(self):
        from review_cache import review_input_signature
        prompt = "snapshot rules"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            folder = output / "prepared/jd_shortlists"
            folder.mkdir(parents=True)
            jobs = [{"job_id": key, "title": "待判断岗位", "salary": "20-30K"} for key in ("yes", "no", "unknown")]
            details = [{"job_id": key, "jd": "负责业务服务的方案与交付。", "jd_completeness": "complete"}
                       for key in ("yes", "no", "unknown")]
            reviews = []
            for job, judgment in zip(jobs, ("符合", "不符合", "信息不足")):
                item = {**job, "jd": details[0]["jd"]}
                reviews.append({"job_id": job["job_id"], "status": "success", "review_format": "simple-v1",
                                "判断": judgment, "理由": "根据工作内容判断。", "cache_signature": "offline-signature",
                                "cache_input_signature": review_input_signature(model_input(item), prompt)})
            for path, data in ((output / "prepared/merged_jobs_index.json", {"jobs": jobs}),
                               (folder / "details.json", details),
                               (output / "prepared/llm_reviews.json", {"reviews": reviews})):
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with patch("process_architecture.load_prompt", return_value=prompt), \
                    patch("llm_client.load_codex_config", side_effect=AssertionError("report stays offline")):
                report_stage(output)
            data = json.loads((output / "prepared/merged_jobs_index.json").read_text(encoding="utf-8"))
            self.assertEqual([item["classification"] for item in data["jobs"]], ["海投池", "暂不投", "待核实"])
            self.assertTrue(all(item["model_cache_validation"] == "input_match" for item in data["jobs"]))
            markdown = (output / "prepared/海投分层清单.md").read_text(encoding="utf-8")
            self.assertIn("AI信息不足", markdown)
            for forbidden in ("六项", "/6", "能力差距", "模型证据"):
                self.assertNotIn(forbidden, markdown)


class ConversationPoolTests(unittest.TestCase):
    def setUp(self):
        from llm_client import CodexConfig
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.prepared = self.output / "prepared"
        (self.prepared / "jd_shortlists").mkdir(parents=True)
        self.config = CodexConfig(model="test", base_url="https://provider.test/v1", api_key="fixture-secret")

    def run_client(self, client, count=4, *, conversation_size=15):
        jobs = [{"job_id": f"job-{index}", "title": "AI 方案架构师"} for index in range(count)]
        for path, payload in (
            (self.prepared / "merged_jobs_index.json", {"jobs": jobs}),
            (self.prepared / "jd_shortlists" / "jd_input.json", {"jobs": jobs}),
            (self.prepared / "jd_shortlists" / "details.json",
             [{"job_id": job["job_id"], "jd": "负责 AI 方案设计与交付"} for job in jobs]),
        ):
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with patch("llm_client.load_codex_config", return_value=self.config), \
                patch("llm_client.CodexResponsesClient", return_value=client), \
                patch("process_architecture.load_prompt", return_value="review rules"):
            options = {} if conversation_size is None else {"conversation_size": conversation_size}
            return run_llm(self.output, workers=1, **options)

    def reviews(self):
        return json.loads((self.prepared / "llm_reviews.json").read_text(encoding="utf-8"))["reviews"]

    def test_response_chain_rejection_keeps_history_and_rotates_after_fifteen(self):
        from llm_client import LLMHTTPError
        calls, sessions = [], []

        class Client:
            supports_conversations = True

            def review_job(inner, job, *, prompt, conversation=None):
                self.assertIsInstance(conversation, dict)
                calls.append((job["job_id"], bool(conversation.get("responses_history_only")),
                              list(conversation.get("messages", []))))
                if conversation.get("previous_response_id") and not conversation.get("responses_history_only"):
                    raise LLMHTTPError("response chain unavailable", status_code=400)
                history = conversation.setdefault("messages", [])
                if not history:
                    sessions.append([])
                sessions[-1].append(job["job_id"])
                reused = bool(history)
                history.extend([job["job_id"], "validated result"])
                local = conversation.get("responses_history_only") is True
                if not local:
                    conversation["previous_response_id"] = "private-provider-id"
                return {"判断": "符合", "理由": "负责AI应用方案。",
                        "_conversation": {"reused": reused, "client_history": local, "request_count": 1}}

        stats = self.run_client(Client(), count=31)
        self.assertEqual((stats["completed_count"], stats["failed_count"]), (31, 0))
        self.assertEqual([len(session) for session in sessions], [15, 15, 1])
        self.assertEqual(stats["conversation_sessions_started"], 3)
        self.assertEqual(stats["conversation_followup_count"], 28)
        self.assertEqual(stats["conversation_fallback_count"], 1)
        self.assertEqual(stats["conversation_client_history_job_count"], 30)
        self.assertEqual(stats["conversation_validated_request_count"], 31)
        self.assertEqual(calls[2][2], ["job-0", "validated result"])
        self.assertTrue(all(call[1] for call in calls[2:]))
        persisted = json.dumps(self.reviews())
        self.assertNotIn("private-provider-id", persisted)
        self.assertNotIn('"messages"', persisted)
        self.assertEqual(sum(row["conversation_reused"] for row in self.reviews()), 28)

    def test_failed_job_resets_lane_without_retrying_auth_failure(self):
        from llm_client import LLMHTTPError
        calls = []

        class Client:
            supports_conversations = True

            def review_job(inner, job, *, prompt, conversation=None):
                calls.append((job["job_id"], bool(conversation.get("previous_response_id"))))
                if job["job_id"] == "job-1":
                    raise LLMHTTPError("fixture-secret must stay private", status_code=403)
                reused = bool(conversation)
                conversation["previous_response_id"] = "private-response"
                return {"判断": "符合", "理由": "负责AI应用方案。",
                        "_conversation": {"reused": reused, "request_count": 1}}

        stats = self.run_client(Client())
        self.assertEqual(calls, [("job-0", False), ("job-1", True), ("job-2", False), ("job-3", True)])
        self.assertEqual((stats["completed_count"], stats["failed_count"]), (3, 1))
        self.assertEqual(stats["conversation_sessions_started"], 2)
        self.assertEqual(stats["conversation_fallback_count"], 0)
        self.assertNotIn("fixture-secret", json.dumps(self.reviews()))

    def test_invalid_chained_json_retries_same_job_with_committed_history(self):
        from llm_client import InvalidJsonResponseError, JsonSchemaValidationError
        for failure in (InvalidJsonResponseError("invalid"), JsonSchemaValidationError("invalid structure")):
            with self.subTest(error=type(failure).__name__):
                calls = []

                class Client:
                    supports_conversations = True

                    def review_job(inner, job, *, prompt, conversation=None):
                        local = conversation.get("responses_history_only") is True
                        history = conversation.setdefault("messages", [])
                        calls.append((job["job_id"], local, list(history)))
                        if history and not local:
                            raise failure
                        reused = bool(history)
                        history.append(job["job_id"])
                        if not local:
                            conversation["previous_response_id"] = "private-id"
                        return {"判断": "符合", "理由": "负责AI应用方案。",
                                "_conversation": {"reused": reused, "client_history": local, "request_count": 1}}

                review_file = self.prepared / "llm_reviews.json"
                if review_file.exists():
                    review_file.unlink()
                stats = self.run_client(Client(), count=3)
                self.assertEqual((stats["completed_count"], stats["failed_count"]), (3, 0))
                self.assertEqual(stats["conversation_fallback_count"], 1)
                self.assertEqual(stats["conversation_sessions_started"], 1)
                self.assertEqual(calls[2], ("job-1", True, ["job-0"]))
                self.assertEqual(calls[3], ("job-2", True, ["job-0", "job-1"]))

    def test_missing_response_id_can_continue_through_local_history(self):
        starts = []

        class Client:
            supports_conversations = True

            def review_job(inner, job, *, prompt, conversation=None):
                history = conversation.setdefault("messages", [])
                starts.append(not history)
                history.append(job["job_id"])
                conversation["responses_history_only"] = True
                return {"判断": "符合", "理由": "负责AI应用方案。",
                        "_conversation": {"reused": len(history) > 1, "client_history": True, "request_count": 1}}

        stats = self.run_client(Client())
        self.assertEqual(starts, [True, False, False, False])
        self.assertEqual(stats["conversation_sessions_started"], 1)
        self.assertEqual(stats["conversation_client_history_job_count"], 4)
        self.assertEqual(stats["conversation_fallback_count"], 1)

    def test_default_is_stateless_for_both_protocols_and_resumes_successes(self):
        from dataclasses import replace

        for mode in ("responses", "chat_completions"):
            with self.subTest(mode=mode):
                self.config = replace(self.config, api_mode=mode)
                calls = []

                class Client:
                    supports_conversations = True

                    def review_job(inner, job, *, prompt, conversation=None):
                        self.assertIsNone(conversation)
                        calls.append(job["job_id"])
                        return {"判断": "符合", "理由": "负责AI应用方案。"}

                client = Client()
                stats = self.run_client(client, conversation_size=None)
                self.assertEqual(stats["conversation_mode"], "stateless")
                self.assertEqual(stats["conversation_session_size"], 1)
                self.assertIs(stats["response_storage_requested"], False)
                self.assertEqual(stats["conversation_followup_count"], 0)
                self.assertEqual(stats["conversation_client_history_job_count"], 0)
                self.assertEqual(stats["completed_count"], 4)
                self.assertEqual(len(calls), 4)
                resumed = self.run_client(client, conversation_size=None)
                self.assertEqual(len(calls), 4)
                self.assertEqual(resumed["cache_hit_count"], 4)
                self.assertEqual(resumed["request_count"], 0)

    def test_cli_and_powershell_default_to_one_job_per_session(self):
        with patch("process_architecture.run_llm") as runner, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--root", str(self.output), "--stage", "llm"]), 0)
        self.assertEqual(runner.call_args.kwargs["conversation_size"], 1)
        script = (PROJECT_ROOT / "run_process.ps1").read_text(encoding="utf-8-sig")
        self.assertRegex(script, r"\[int\]\$ConversationSize\s*=\s*1\s*,")

    def test_size_one_turns_off_conversations_and_uses_success_checkpoint(self):
        calls = []

        class Client:
            supports_conversations = True

            def review_job(inner, job, *, prompt, conversation=None):
                self.assertIsNone(conversation)
                calls.append(job["job_id"])
                return {"判断": "符合", "理由": "负责AI应用方案。"}

        client = Client()
        stats = self.run_client(client, conversation_size=1)
        self.assertEqual(stats["conversation_mode"], "stateless")
        self.assertEqual(stats["conversation_sessions_started"], 0)
        self.assertEqual(stats["completed_count"], 4)
        resumed = self.run_client(client, conversation_size=1)
        self.assertEqual(len(calls), 4)
        self.assertEqual(resumed["cache_hit_count"], 4)
        self.assertEqual(resumed["request_count"], 0)

    def test_worker_interruption_reaches_main_thread_instead_of_hanging(self):
        class Client:
            supports_conversations = True

            def review_job(inner, job, *, prompt, conversation=None):
                raise KeyboardInterrupt("worker interrupted")

        with self.assertRaises(KeyboardInterrupt):
            self.run_client(Client(), count=1)
        manifest = json.loads((self.prepared / "llm_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "running")
        self.assertEqual(manifest["pending_count"], 1)

    def test_invalid_session_size_is_rejected_before_files_or_requests(self):
        for invalid in (0, 101, True, "15"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                run_llm(self.output, conversation_size=invalid)
        self.assertFalse((self.prepared / "llm_manifest.json").exists())


class LocalCodexConnectionTests(unittest.TestCase):
    """Use real config, auth, requests and checkpoints with an offline transport."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / "environment-codex"
        self.write_connection(self.home, "https://first-provider.test/v1", "first-fixture-key")
        project_settings = patch("project_llm_settings.default_settings_path",
                                 return_value=self.base / "missing-project-llm.json")
        project_settings.start()
        self.addCleanup(project_settings.stop)
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.home), "OPENAI_API_KEY": "unrelated-fixture-key"})
        environment.start()
        self.addCleanup(environment.stop)
        self.requests = []
        transport = patch("llm_client.CodexResponsesClient._default_transport", side_effect=self.respond)
        transport.start()
        self.addCleanup(transport.stop)

    @staticmethod
    def write_connection(home, url, key):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(
            'model_provider = "selected"\nmodel = "desktop-model"\nmodel_reasoning_effort = "high"\n'
            '[model_providers.selected]\n'
            f'base_url = "{url}"\nrequires_openai_auth = true\n'
            '[model_providers.unselected]\nbase_url = "https://unselected-provider.test/v1"\n'
            'env_key = "UNSELECTED_KEY_MUST_NOT_BE_READ"\n', encoding="utf-8")
        (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": key}), encoding="utf-8")

    def prepare_output(self, name):
        output = self.base / name
        folder = output / "prepared/jd_shortlists"
        folder.mkdir(parents=True)
        jobs = [{"job_id": "test-job", "title": "AI解决方案工程师"}]
        (output / "prepared/merged_jobs_index.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        (folder / "jd_input.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        (folder / "details.json").write_text(
            json.dumps([{"job_id": "test-job", "jd": "负责技术方案设计、系统集成和交付。"}]), encoding="utf-8")
        (output / "prepared/海投分层清单.md").write_text("historical report must stay unchanged", encoding="utf-8")
        return output

    def respond(self, request, timeout):
        self.requests.append(request)
        response = {"判断": "符合", "理由": "负责技术方案设计、系统集成和交付。"}
        if request.full_url.endswith("/chat/completions"):
            return {"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(response),
            }}]}
        return {"status": "completed", "output_text": json.dumps(response)}

    def assert_request(self, url, key):
        request = self.requests[-1]
        self.assertEqual(request.full_url, url + "/responses")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {key}")
        body = json.loads(request.data)
        self.assertEqual((body["model"], body["reasoning"]["effort"]), ("gpt-5.6-luna", "max"))
        self.assertEqual(request.get_header("User-agent"), "boss-architecture-collector")

    def test_codex_home_url_and_key_edits_are_reloaded_without_changing_fixed_model(self):
        output = self.prepare_output("runtime")
        report = output / "prepared/海投分层清单.md"
        report_before = report.read_bytes()
        first = run_llm(output, workers=1)
        self.assert_request("https://first-provider.test/v1", "first-fixture-key")
        self.assertEqual((first["cache_hit_count"], first["request_count"]), (0, 1))
        checkpoint = output / "prepared/llm_reviews.json"
        first_review = json.loads(checkpoint.read_text(encoding="utf-8"))["reviews"][0]

        # A key change alone can reuse a success, while a subsequent request
        # must use that new credential, never a cached client or account key.
        self.write_connection(self.home, "https://first-provider.test/v1", "rotated-fixture-key")
        cached = run_llm(output, workers=1)
        self.assertEqual((cached["cache_hit_count"], cached["request_count"]), (1, 0))
        self.assertEqual(len(self.requests), 1)
        self.write_connection(self.home, "https://second-provider.test/v1", "rotated-fixture-key")
        config_before = {path: path.read_bytes() for path in self.home.iterdir()}
        refreshed = run_llm(output, workers=1)
        self.assert_request("https://second-provider.test/v1", "rotated-fixture-key")
        self.assertEqual((refreshed["cache_hit_count"], refreshed["request_count"], refreshed["cache_invalidated_count"]), (0, 1, 1))
        self.assertEqual((refreshed["model"], refreshed["reasoning_effort"]), ("gpt-5.6-luna", "max"))
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(saved["previous_reviews"], [{"reason": "signature_changed", "review": first_review}])
        self.assertEqual((saved["reviews"][0]["review_model"], saved["reviews"][0]["review_reasoning_effort"]),
                         ("gpt-5.6-luna", "max"))
        self.assertEqual(config_before, {path: path.read_bytes() for path in self.home.iterdir()})
        self.assertEqual(report.read_bytes(), report_before)
        persisted = "\n".join(path.read_text(encoding="utf-8") for path in (output / "prepared").glob("llm_*"))
        for private in ("first-fixture-key", "rotated-fixture-key", "unrelated-fixture-key",
                        "https://first-provider.test/v1", "https://second-provider.test/v1"):
            self.assertNotIn(private, persisted)

    def test_explicit_codex_home_can_select_chat_protocol_and_cli_can_override_it(self):
        config = self.home / "config.toml"
        config.write_text('api_mode = "chat_completions"\n' + config.read_text(encoding="utf-8"), encoding="utf-8")
        before = {path: path.read_bytes() for path in self.home.iterdir()}
        output = self.prepare_output("chat-cli")
        report = output / "prepared/海投分层清单.md"
        report_before = report.read_bytes()
        with redirect_stdout(io.StringIO()):
            result = main(["--root", str(output), "--stage", "llm", "--codex-home", str(self.home)])
        self.assertEqual(result, 0)
        request = self.requests[-1]
        self.assertEqual(request.full_url, "https://first-provider.test/v1/chat/completions")
        body = json.loads(request.data)
        self.assertEqual(set(body), {"model", "messages", "stream"})
        self.assertFalse(body["stream"])
        reviews = json.loads((output / "prepared/llm_reviews.json").read_text(encoding="utf-8"))["reviews"]
        self.assertEqual(reviews[0]["status"], "success")
        self.assertEqual(reviews[0]["review_api_mode"], "chat_completions")

        with redirect_stdout(io.StringIO()):
            main(["--root", str(output), "--stage", "llm", "--codex-home", str(self.home), "--api-mode", "responses"])
        self.assert_request("https://first-provider.test/v1", "first-fixture-key")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(before, {path: path.read_bytes() for path in self.home.iterdir()})
        self.assertEqual(report.read_bytes(), report_before)

    def test_cli_custom_codex_home_and_connection_overrides_are_independent(self):
        custom_home = self.base / "custom-codex"
        self.write_connection(custom_home, "https://custom-provider.test/v1", "custom-fixture-key")
        before = {path: path.read_bytes() for home in (self.home, custom_home) for path in home.iterdir()}
        cases = (
            ([], "https://custom-provider.test/v1", "custom-fixture-key"),
            (["--base-url", "https://override-provider.test/v1"], "https://override-provider.test/v1", "custom-fixture-key"),
            (["--api-key", "explicit-fixture-key"], "https://custom-provider.test/v1", "explicit-fixture-key"),
            (["--base-url", "https://override-provider.test/v1", "--api-key", "explicit-fixture-key"],
             "https://override-provider.test/v1", "explicit-fixture-key"),
        )
        for index, (overrides, url, key) in enumerate(cases):
            with self.subTest(overrides=overrides), redirect_stdout(io.StringIO()):
                output = self.prepare_output(f"cli-{index}")
                code = main(["--root", str(output), "--stage", "llm", "--codex-home", str(custom_home), *overrides])
                self.assertEqual(code, 0)
                self.assert_request(url, key)
                manifest = json.loads((output / "prepared/llm_manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["completed_count"], 1)
                persisted = "\n".join(path.read_text(encoding="utf-8") for path in (output / "prepared").glob("llm_*"))
                for private in (url, key, "custom-fixture-key", "explicit-fixture-key"):
                    self.assertNotIn(private, persisted)
        self.assertEqual(len(self.requests), len(cases))
        self.assertEqual(before, {path: path.read_bytes() for path in before})


class LLMCacheTests(unittest.TestCase):
    """Exercise real checkpoint orchestration with no provider or auth I/O."""

    def setUp(self):
        from llm_client import CodexConfig
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.project_settings_path = self.output / "missing-project-llm.json"
        self.start_patch("project_llm_settings.default_settings_path", return_value=self.project_settings_path)
        self.prepared = self.output / "prepared"
        self.folder = self.prepared / "jd_shortlists"
        self.folder.mkdir(parents=True)
        self.jobs = [{"job_id": key, "title": "AI解决方案工程师", "company": "测试公司", "salary": "20-30K"}
                     for key in ("a", "b")]
        self.details = [{"job_id": key, "jd": "负责完整的技术方案设计、系统集成和项目交付。"} for key in ("a", "b")]
        self.write_inputs()
        self.config = CodexConfig(model="fake-model", reasoning_effort="max", base_url="https://private-provider.test/v1",
                                  api_key="fixture-key-never-saved")
        self.failures = {}
        self.requests = []

        def load_config(**kwargs):
            from dataclasses import replace
            fields = ("model", "reasoning_effort", "base_url", "api_key", "api_mode",
                      "chat_reasoning_verified_for", "chat_stream")
            saved = project_llm_settings.load_settings(kwargs["project_settings_path"]) if kwargs.get("project_settings_path") else {}
            overrides = {key: value for key, value in (saved or {}).items() if key in fields}
            overrides.update({key: kwargs[key] for key in fields
                         if kwargs.get(key) is not None}
            )
            return replace(self.config, **overrides)

        def request(job, *, prompt):
            self.requests.append((dict(job), prompt))
            if job["job_id"] in self.failures:
                raise self.failures[job["job_id"]]
            return {"判断": "符合", "理由": "负责企业应用方案与交付。"}

        self.client = Mock()
        self.client.review_job.side_effect = request
        self.loader = self.start_patch("llm_client.load_codex_config", side_effect=load_config)
        self.factory = self.start_patch("llm_client.CodexResponsesClient", return_value=self.client)
        self.prompt = self.start_patch("process_architecture.load_prompt", return_value="Full system prompt for fake requests")
        self.protected = {}
        for name in ("海投分层清单.md", "hai_tou_pool.json"):
            path = self.prepared / name
            path.write_text("historical report must remain byte-for-byte unchanged", encoding="utf-8")
            self.protected[path] = (path.stat().st_mtime_ns, path.read_bytes())

    def start_patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def tearDown(self):
        for path, expected in self.protected.items():
            self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()), expected)

    def write_inputs(self):
        (self.prepared / "merged_jobs_index.json").write_text(json.dumps({"jobs": self.jobs}), encoding="utf-8")
        (self.folder / "jd_input.json").write_text(json.dumps({"jobs": self.jobs}), encoding="utf-8")
        (self.folder / "details.json").write_text(json.dumps(self.details), encoding="utf-8")

    def document(self):
        value = json.loads((self.prepared / "llm_reviews.json").read_text(encoding="utf-8"))
        lines = (self.prepared / "llm_reviews.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(value["reviews"], [json.loads(line) for line in lines])
        return value

    def test_run_llm_uses_saved_project_model_and_effort_when_cli_does_not_override(self):
        project_llm_settings.save_settings(
            "https://custom-provider.example/v1", "custom-review-model", "high",
            path=self.project_settings_path,
        )

        result = run_llm(self.output, workers=1)

        self.assertEqual(self.loader.call_args.kwargs["model"], "custom-review-model")
        self.assertEqual(self.loader.call_args.kwargs["reasoning_effort"], "high")
        self.assertEqual(self.loader.call_args.kwargs["project_settings_path"], self.project_settings_path)
        actual_config = self.factory.call_args.args[0]
        self.assertEqual((actual_config.model, actual_config.reasoning_effort), ("custom-review-model", "high"))
        self.assertEqual((result["model"], result["reasoning_effort"]), ("custom-review-model", "high"))
        self.assertEqual({row["review_model"] for row in self.document()["reviews"]}, {"custom-review-model"})

    def test_adapter_cannot_upgrade_old_rich_or_forge_local_metadata_into_success(self):
        for invalid in ({"dimensions": {}, "target_relevance": {"assessment": "direct"}},
                        {"判断": "符合", "理由": "相关。", "job_id": "forged-id"},
                        {"判断": "符合", "理由": "相关。", "review_format": "simple-v1"},
                        {"判断": "符合", "理由": " \n"}):
            with self.subTest(invalid=invalid):
                self.client.review_job.side_effect = lambda job, prompt: invalid if job["job_id"] == "a" else {
                    "判断": "信息不足", "理由": "职责描述不足，需核实。"}
                # Start independent temporary checkpoint inputs for each case.
                for filename in ("llm_reviews.json", "llm_reviews.jsonl", "llm_manifest.json"):
                    path = self.prepared / filename
                    if path.exists():
                        path.unlink()
                manifest = run_llm(self.output, workers=2)
                self.assertEqual((manifest["completed_count"], manifest["failed_count"]), (1, 1))
                records = {row["job_id"]: row for row in self.document()["reviews"]}
                self.assertEqual(set(records), {"a", "b"})
                self.assertEqual(records["a"]["status"], "failed")
                self.assertEqual(records["b"]["status"], "success")
                self.assertEqual(records["a"]["review_format"], "simple-v1")
                self.assertEqual(records["b"]["review_format"], "simple-v1")
                self.assertNotIn("dimensions", records["a"])
                self.assertNotIn("判断", records["a"])
                self.client.review_job.side_effect = lambda job, prompt: {"判断": "符合", "理由": "完成复核。"}
                resumed = run_llm(self.output, workers=2)
                self.assertEqual((resumed["cache_hit_count"], resumed["request_count"], resumed["completed_count"]), (1, 1, 2))

    def test_continuation_repairs_new_success_missing_either_signature_by_requesting_again(self):
        self.assert_counts(run_llm(self.output), hits=0, requests=2)
        for missing in ("cache_signature", "cache_input_signature"):
            with self.subTest(missing=missing):
                document = self.document()
                broken = next(row for row in document["reviews"] if row["job_id"] == "a")
                broken.pop(missing)
                (self.prepared / "llm_reviews.json").write_text(json.dumps(document), encoding="utf-8")
                self.assert_counts(run_llm(self.output), hits=1, requests=1, invalidated=1)
                repaired = next(row for row in self.document()["reviews"] if row["job_id"] == "a")
                self.assertTrue(repaired["cache_signature"])
                self.assertTrue(repaired["cache_input_signature"])
                self.assertTrue(any(entry["review"] == broken for entry in self.document()["previous_reviews"]))

    def test_both_protocols_request_again_for_wrong_input_signature_then_resume_corrected_success(self):
        for mode in ("responses", "chat_completions"):
            with self.subTest(mode=mode):
                run_llm(self.output, workers=1, api_mode=mode)
                self.assertEqual(len(self.document()["reviews"]), 2)
                for value in ("wrong-nonempty", None):
                    document = self.document()
                    broken = next(row for row in document["reviews"] if row["job_id"] == "a")
                    combined = broken["cache_signature"]
                    if value is None:
                        broken.pop("cache_input_signature")
                    else:
                        broken["cache_input_signature"] = value
                    (self.prepared / "llm_reviews.json").write_text(json.dumps(document), encoding="utf-8")
                    before = len(self.requests)
                    self.assert_counts(run_llm(self.output, workers=1, api_mode=mode), hits=1, requests=1, invalidated=1)
                    self.assertEqual(len(self.requests), before + 1)
                    self.assertEqual(self.requests[-1][0]["job_id"], "a")
                    repaired = next(row for row in self.document()["reviews"] if row["job_id"] == "a")
                    self.assertEqual(repaired["cache_signature"], combined)
                    self.assertTrue(repaired["cache_input_signature"])
                    self.assertNotEqual(repaired["cache_input_signature"], "wrong-nonempty")
                    self.assertTrue(any(entry["review"] == broken for entry in self.document()["previous_reviews"]))
                    self.assert_counts(run_llm(self.output, workers=1, api_mode=mode), hits=2, requests=0)
                    self.assertEqual(len(self.requests), before + 1)

    def test_saved_chat_protocol_reaches_requests_and_records_effective_provenance(self):
        project_llm_settings.save_settings(
            "https://chat-provider.example/v1", "compatible-chat-model", "high",
            path=self.project_settings_path, api_mode="chat_completions",
        )
        result = run_llm(self.output, workers=1)
        self.assertEqual(self.factory.call_args.args[0].api_mode, "chat_completions")
        self.assertEqual(result["api_mode"], "chat_completions")
        self.assertIsNone(result["reasoning_effort"], "do not claim to have sent an ignored effort")
        reviews = self.document()["reviews"]
        self.assertEqual({row["review_api_mode"] for row in reviews}, {"chat_completions"})
        self.assertEqual({row["source"] for row in reviews}, {"configured_chat_completions_api"})
        self.assertEqual({row["review_reasoning_effort"] for row in reviews}, {None})

        self.assert_counts(run_llm(self.output), hits=2, requests=0)
        project_llm_settings.save_settings(
            "https://chat-provider.example/v1", "compatible-chat-model", "max",
            path=self.project_settings_path, api_mode="chat_completions",
        )
        self.assert_counts(run_llm(self.output), hits=2, requests=0)
        self.assert_counts(run_llm(self.output, api_mode="responses"), hits=0, requests=2, invalidated=2)

    def test_chat_failure_uses_selected_protocol_in_redacted_failure_record(self):
        from llm_client import LLMHTTPError

        self.failures["a"] = LLMHTTPError("diagnostic with fixture-key-never-saved", status_code=400)
        result = run_llm(self.output, workers=1, api_mode="chat_completions")
        self.assertEqual(result["failed_count"], 1)
        failed = next(row for row in self.document()["reviews"] if row["status"] == "failed")
        self.assertIn("Chat Completions API", failed["error"])
        self.assertEqual(failed["requested_api_mode"], "chat_completions")
        self.assertIsNone(failed["requested_reasoning_effort"])

    def test_verified_chat_effort_reaches_provenance_and_invalidates_default_effort_cache(self):
        url, model = "https://chat-provider.example/v1", "deepseek-v4.1-flash"
        project_llm_settings.save_settings(
            url, model, "max", path=self.project_settings_path, api_mode="chat_completions", chat_stream=True,
        )
        self.assert_counts(run_llm(self.output), hits=0, requests=2)
        self.assertIsNone(self.document()["reviews"][0]["review_reasoning_effort"])
        marker = project_llm_settings.chat_reasoning_signature(url, model, "max", chat_stream=True)
        project_llm_settings.save_settings(
            url, model, "max", path=self.project_settings_path, api_mode="chat_completions", chat_stream=True,
            chat_reasoning_verified_for=marker,
        )
        result = run_llm(self.output)
        self.assert_counts(result, hits=0, requests=2, invalidated=2)
        self.assertEqual(result["reasoning_effort"], "max")
        self.assertTrue(result["chat_stream"])
        actual = self.factory.call_args.args[0]
        self.assertEqual(actual.effective_reasoning_effort, "max")
        self.assertEqual(actual.chat_reasoning_verified_for, marker)
        self.assertEqual({row["review_reasoning_effort"] for row in self.document()["reviews"]}, {"max"})
        self.assertEqual({row["review_chat_stream"] for row in self.document()["reviews"]}, {True})
        self.assert_counts(run_llm(self.output), hits=2, requests=0)
        changed = run_llm(self.output, reasoning_effort="high")
        self.assert_counts(changed, hits=0, requests=2, invalidated=2)
        self.assertIsNone(changed["reasoning_effort"], "an effort override requires its own successful probe")
        self.assertIsNone(self.factory.call_args.args[0].effective_reasoning_effort)

    def test_verified_chat_failure_records_the_effort_actually_requested(self):
        from llm_client import LLMHTTPError

        url, model = "https://chat-provider.example/v1", "deepseek-v4.1-flash"
        marker = project_llm_settings.chat_reasoning_signature(url, model, "max", chat_stream=True)
        project_llm_settings.save_settings(
            url, model, "max", path=self.project_settings_path, api_mode="chat_completions", chat_stream=True,
            chat_reasoning_verified_for=marker,
        )
        self.failures["a"] = LLMHTTPError("fixture-key-never-saved", status_code=429)
        run_llm(self.output)
        failed = next(row for row in self.document()["reviews"] if row["status"] == "failed")
        self.assertEqual(failed["requested_reasoning_effort"], "max")
        self.assertTrue(failed["requested_chat_stream"])
        self.assertNotIn("fixture-key-never-saved", json.dumps(failed))

    def assert_counts(self, manifest, *, hits, requests, invalidated=0, completed=2, failed=0):
        self.assertEqual(manifest["candidate_count"], 2)
        self.assertEqual(manifest["cache_hit_count"], hits)
        self.assertEqual(manifest["cache_miss_count"], 2 - hits)
        self.assertEqual(manifest["request_count"], requests)
        self.assertEqual(manifest["cache_invalidated_count"], invalidated)
        self.assertEqual(manifest["completed_count"], completed)
        self.assertEqual(manifest["failed_count"], failed)
        self.assertEqual(manifest["pending_count"], 2 - completed - failed)
        self.document()

    def test_effective_jd_prompt_model_effort_and_url_changes_invalidate(self):
        self.assert_counts(run_llm(self.output), hits=0, requests=2)
        self.assert_counts(run_llm(self.output), hits=2, requests=0)
        self.details[0]["jd"] += "负责新增知识库评测职责。"
        self.write_inputs()
        self.assert_counts(run_llm(self.output), hits=1, requests=1, invalidated=1)
        overrides = {}
        for field, value in (("model", "changed-model"), ("reasoning_effort", "high"),
                             ("base_url", "https://changed-provider.test/v1")):
            with self.subTest(setting=field):
                overrides[field] = value
                self.assert_counts(run_llm(self.output, **overrides), hits=0, requests=2, invalidated=2)
                self.assert_counts(run_llm(self.output, **overrides), hits=2, requests=0)
        self.prompt.return_value += "\nFull revised schema guidance."
        self.assert_counts(run_llm(self.output, **overrides), hits=0, requests=2, invalidated=2)
        self.assert_counts(run_llm(self.output, **overrides), hits=2, requests=0)

    def test_luna_max_defaults_do_not_reuse_an_old_model_high_checkpoint(self):
        self.assert_counts(run_llm(self.output, model="previous-model", reasoning_effort="high"), hits=0, requests=2)
        self.assert_counts(run_llm(self.output), hits=0, requests=2, invalidated=2)
        checkpoint = self.document()
        self.assertEqual({(row["review_model"], row["review_reasoning_effort"])
                          for row in checkpoint["reviews"]}, {("gpt-5.6-luna", "max")})
        self.assertEqual({entry["reason"] for entry in checkpoint["previous_reviews"]}, {"signature_changed"})
        self.assert_counts(run_llm(self.output), hits=2, requests=0)

    def test_only_duty_and_requirement_text_invalidate_semantic_review(self):
        run_llm(self.output)
        self.jobs[0].update(recommended_keep=False, source_context={"track": "changed"}, jd_analysis_status="stale")
        self.write_inputs()
        self.assert_counts(run_llm(self.output), hits=2, requests=0)
        for field, value in (("title", "新的架构岗位"), ("company", "新的公司"), ("salary", "25-35K"),
                             ("location", "上海"), ("tags", ["1-3年"]), ("skills", ["RAG", "系统集成"])):
            with self.subTest(field=field):
                self.jobs[0][field] = value
        self.write_inputs()
        self.assert_counts(run_llm(self.output), hits=2, requests=0)

        self.details[0]["jd"] += "负责新增企业Agent应用评测。"
        self.write_inputs()
        self.assert_counts(run_llm(self.output), hits=1, requests=1, invalidated=1)

    def test_api_key_rotation_does_not_change_identity_or_leak_provider(self):
        from dataclasses import replace
        run_llm(self.output)
        self.config = replace(self.config, api_key="rotated-fixture-key-never-saved")
        self.assert_counts(run_llm(self.output), hits=2, requests=0)
        persisted = "\n".join(path.read_text(encoding="utf-8") for path in self.prepared.glob("llm_*"))
        for forbidden in (self.config.base_url, "fixture-key-never-saved", "rotated-fixture-key-never-saved"):
            self.assertNotIn(forbidden, persisted)
        self.assertTrue(all(row["cache_signature"] != "forged-signature" for row in self.document()["reviews"]))

    def test_unusable_auth_quarantines_old_success_and_recovery_requests_again(self):
        from dataclasses import replace
        run_llm(self.output)
        self.config = replace(self.config, api_key=None)
        result = run_llm(self.output)
        self.assert_counts(result, hits=0, requests=0, invalidated=2, completed=0)
        self.assertEqual(result["status"], "configuration_failed")
        self.assertEqual(result["fallback_count"], 2)
        self.assertEqual(self.document()["reviews"], [])
        self.assertEqual(len(self.requests), 2)
        self.config = replace(self.config, api_key="restored-fixture-key")
        self.assert_counts(run_llm(self.output), hits=0, requests=2)

    def test_legacy_and_unrelated_rows_are_retained_only_as_history(self):
        old = [{"job_id": "a", "status": "success"},
               {"job_id": "b", "status": "success", "cache_signature": "old-signature"},
               {"job_id": "outside", "status": "success"}]
        (self.prepared / "llm_reviews.json").write_text(json.dumps({"reviews": old}), encoding="utf-8")
        result = run_llm(self.output)
        self.assert_counts(result, hits=0, requests=2, invalidated=2)
        self.assertEqual((result["cache_legacy_count"], result["cache_excluded_count"]), (1, 1))
        document = self.document()
        self.assertEqual({row["job_id"] for row in document["reviews"]}, {"a", "b"})
        self.assertEqual({entry["review"]["job_id"] for entry in document["previous_reviews"]}, {"a", "b", "outside"})
        self.assert_counts(run_llm(self.output), hits=2, requests=0)
        self.assertEqual(self.document()["previous_reviews"], document["previous_reviews"])

    def test_failed_request_retries_without_losing_success_or_saving_error_secrets(self):
        from llm_client import LLMHTTPError
        self.failures["b"] = LLMHTTPError(f"{self.config.api_key} {self.config.base_url}", status_code=403)
        self.assert_counts(run_llm(self.output), hits=0, requests=2, completed=1, failed=1)
        self.failures.clear()
        self.assert_counts(run_llm(self.output), hits=1, requests=1)
        self.assertEqual(len(self.document()["previous_reviews"]), 1)
        self.assertEqual(self.document()["previous_reviews"][0]["reason"], "retry_failed")
        persisted = json.dumps(self.document())
        self.assertNotIn(self.config.api_key, persisted)
        self.assertNotIn(self.config.base_url, persisted)
        self.assert_counts(run_llm(self.output), hits=2, requests=0)

    def test_interruption_keeps_per_job_signature_and_resumes_only_missing_job(self):
        import process_architecture as processor
        original_write = processor.write_json

        def interrupted(path, payload):
            original_write(path, payload)
            if (path.name == "llm_manifest.json" and payload.get("status") == "running"
                    and payload.get("completed_count") == 1):
                raise KeyboardInterrupt("simulated interruption after first checkpoint")

        with patch("process_architecture.write_json", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                run_llm(self.output, workers=1)
        checkpoint = self.document()
        self.assertEqual(len(checkpoint["reviews"]), 1)
        self.assertTrue(checkpoint["reviews"][0]["cache_signature"].startswith("semantic-review-v1:"))
        manifest = json.loads((self.prepared / "llm_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["status"], manifest["completed_count"], manifest["pending_count"]), ("running", 1, 1))
        self.assert_counts(run_llm(self.output), hits=1, requests=1)
        self.assertEqual(len(self.requests), 3)

    def test_partial_or_removed_jobs_never_read_credentials_or_keep_current_results(self):
        run_llm(self.output)
        self.jobs = self.jobs[:1]
        self.details[0]["truncated"] = True
        self.write_inputs()
        self.loader.side_effect = AssertionError("partial input must not read credentials")
        result = run_llm(self.output)
        self.assertEqual((result["status"], result["candidate_count"], result["completed_count"], result["request_count"]),
                         ("skipped_no_candidates", 0, 0, 0))
        self.assertEqual(self.document()["reviews"], [])
        self.assertEqual(len(self.document()["previous_reviews"]), 2)

    def test_configuration_and_prompt_errors_cannot_keep_success_or_leak_exception_text(self):
        from llm_client import ConfigurationError
        run_llm(self.output)
        self.prompt.side_effect = OSError(f"{self.config.base_url} {self.config.api_key}")
        result = run_llm(self.output)
        self.assert_counts(result, hits=0, requests=0, invalidated=2, completed=0)
        self.prompt.side_effect = None
        self.loader.side_effect = ConfigurationError(f"{self.config.base_url} {self.config.api_key}")
        result = run_llm(self.output)
        self.assert_counts(result, hits=0, requests=0, completed=0)
        persisted = "\n".join(path.read_text(encoding="utf-8") for path in self.prepared.glob("llm_*"))
        self.assertNotIn(self.config.api_key, persisted)
        self.assertNotIn(self.config.base_url, persisted)

    def test_json_and_jsonl_match_at_every_published_checkpoint(self):
        import process_architecture as processor
        original = processor.write_jsonl
        snapshots = []

        def checked_write(path, rows):
            rows = list(rows)
            original(path, rows)
            snapshots.append(self.document()["reviews"])

        with patch.object(processor, "write_jsonl", side_effect=checked_write):
            run_llm(self.output, workers=1)
        self.assertEqual([len(rows) for rows in snapshots], [0, 1, 2, 2])


if __name__ == "__main__":
    unittest.main()
