#!/usr/bin/env python3
"""Re-screen a saved sample with current rules, without collecting new evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import process_architecture as pipeline


def _fingerprints(paths: list[Path]) -> dict[str, str]:
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
        if path.is_file()
    }


def _counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(job.get("classification", "未分类") for job in jobs)
    return {name: counts.get(name, 0) for name in pipeline.CLASSIFICATIONS}


def rescreen_saved_run(
    source_run: Path,
    output_root: Path | None = None,
    expected_size: int | None = None,
) -> dict[str, Any]:
    """Use exactly the saved IDs and source fields; never write into source_run."""
    source_run = source_run.expanduser().resolve()
    sample_path = source_run / "raw_sample.json"
    old_index_path = source_run / "prepared" / "merged_jobs_index.json"
    old_shortlist_path = source_run / "prepared" / "jd_shortlists" / "jd_input.json"
    details_path = source_run / "prepared" / "jd_shortlists" / "details.json"
    reviews_path = source_run / "prepared" / "llm_reviews.json"
    source_paths = [sample_path, old_index_path, old_shortlist_path, details_path, reviews_path, source_run / "run_manifest.json"]
    before_hashes = _fingerprints(source_paths)
    sample = pipeline.read_json(sample_path)
    old_index = pipeline.read_json(old_index_path)
    raw_jobs = sample.get("jobs", [])
    ids = [pipeline.detail_key(job) for job in raw_jobs]
    id_set = set(ids)
    if not ids or any(not key for key in ids) or len(ids) != len(id_set):
        raise ValueError("保存的样本必须包含非空且唯一的岗位 ID；不会重新抽样或静默去重。")
    if expected_size is not None and len(ids) != expected_size:
        raise ValueError(f"期望 {expected_size} 个去重岗位，保存的样本实际为 {len(ids)} 个。")
    old_jobs = old_index.get("jobs", [])
    old_by_id = {pipeline.detail_key(job): job for job in old_jobs}
    if len(old_jobs) != len(ids) or set(old_by_id) != id_set:
        raise ValueError("旧分层清单与原始样本 ID 不一致，不能进行同样本对照。")
    if any(job.get("classification") not in pipeline.CLASSIFICATIONS for job in old_jobs):
        raise ValueError("旧清单有未分类岗位，无法生成完整的三层数量对照。")

    started = pipeline.now_iso()
    if output_root is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        city = sample.get("metadata", {}).get("city")
        slug = next((key for key, value in pipeline.CITY_NAMES.items() if city in (key, value)), "all")
        output_root = source_run.parent / f"{stamp}-{slug}-{len(ids)}-wide-rescreen"
    output_root = output_root.expanduser().resolve()
    if output_root == source_run or source_run in output_root.parents:
        raise ValueError("新输出目录不能是旧运行目录或其子目录。")
    output_root.mkdir(parents=True, exist_ok=False)

    metadata = {
        **copy.deepcopy(sample.get("metadata") or {}),
        "generated_at": started,
        "sample_origin_run": str(source_run),
        "same_sample_rule_only_rescreen": True,
        "sample_sha256": before_hashes[str(sample_path)],
        "unique_job_count": len(ids),
    }
    jobs = copy.deepcopy(raw_jobs)
    pipeline.write_indexes(output_root, jobs, metadata, copy.deepcopy(raw_jobs))
    shortlist = pipeline.read_json(output_root / "prepared" / "jd_shortlists" / "jd_input.json")["jobs"]
    old_details = pipeline.load_details(details_path)
    reused_details: list[dict[str, Any]] = []
    seen_details: set[str] = set()
    for job in jobs:
        detail = pipeline.find_detail(job, old_details)
        if detail and pipeline.text(detail.get("jd")).strip():
            key = pipeline.detail_key(detail)
            if key not in seen_details:
                reused_details.append(copy.deepcopy(detail))
                seen_details.add(key)
    prepared = output_root / "prepared"
    pipeline.write_json(prepared / "jd_shortlists" / "details.json", reused_details)

    saved_reviews = pipeline.read_json(reviews_path).get("reviews", []) if reviews_path.is_file() else []
    reused_reviews = [copy.deepcopy(review) for review in saved_reviews if review.get("status") == "success" and pipeline.text(review.get("job_id")) in id_set]
    pipeline.write_json(prepared / "llm_reviews.json", {"reviews": reused_reviews})
    llm_manifest = {
        "status": "not_run",
        "new_requests": 0,
        "reused_review_count": len(reused_reviews),
        "completed_count": len(reused_reviews),
        "failed_count": 0,
        "fallback_count": 0,
        "note": "未调用模型 API；只复用旧运行中成功的语义复核，原失败记录不伪装为本次请求。",
    }
    pipeline.write_json(prepared / "llm_manifest.json", llm_manifest)
    pipeline.report_stage(output_root)
    final = pipeline.read_json(prepared / "merged_jobs_index.json")
    new_jobs = final["jobs"]
    final_ids = [pipeline.detail_key(job) for job in new_jobs]
    if final_ids != ids:
        raise AssertionError("重筛后的岗位 ID 或顺序发生变化。")
    final_by_id = {pipeline.detail_key(job): job for job in new_jobs}
    pending_shortlist_ids = [pipeline.detail_key(job) for job in shortlist if not final_by_id[pipeline.detail_key(job)].get("jd_fetched")]
    retained_jobs = [job for job in new_jobs if job.get("classification") in ("重点投递", "海投池")]
    if before_hashes != _fingerprints(source_paths):
        raise AssertionError("旧运行输入发生变化，停止生成成功验收结论。")
    if pipeline.read_json(output_root / "raw_sample.json")["jobs"] != raw_jobs:
        raise AssertionError("原始样本字段或来源发生变化。")

    detailed = []
    for job in new_jobs:
        old = old_by_id[pipeline.detail_key(job)]
        detailed.append({
            "job_id": pipeline.detail_key(job),
            "title": job.get("title"),
            "skills": job.get("skills"),
            "job_link": job.get("job_link"),
            "old_classification": old.get("classification"),
            "new_classification": job.get("classification"),
            "old_reason": old.get("reason"),
            "new_reason": job.get("reason"),
            "list_pass": job.get("list_pass"),
            "list_priority": job.get("list_priority"),
            "list_families": job.get("list_families", []),
            "list_match_evidence": job.get("list_match_evidence", []),
            "jd_status": job.get("jd_status"),
            "jd_dimension_count": job.get("jd_dimension_count"),
            "verification": job.get("verification", []),
        })
    newly_retained = [job for job in detailed if job["old_classification"] == "暂不投" and job["new_classification"] in ("重点投递", "海投池")]
    comparison = {
        "source_run": str(source_run),
        "output_root": str(output_root),
        "sample_size": len(ids),
        "same_job_ids_and_order": True,
        "original_sample_fields_and_sources_preserved": True,
        "source_files_unchanged": True,
        "source_input_sha256": before_hashes,
        "before": _counts(old_jobs),
        "after": _counts(new_jobs),
        "old_list_shortlist_count": len(pipeline.read_json(old_shortlist_path).get("jobs", [])) if old_shortlist_path.is_file() else None,
        "new_list_shortlist_count": len(shortlist),
        "newly_retained_count": len(newly_retained),
        "retained_count": len(retained_jobs),
        "retained_with_jd_count": sum(bool(job.get("jd_fetched")) for job in retained_jobs),
        "retained_pending_jd_count": sum(not job.get("jd_fetched") for job in retained_jobs),
        "shortlist_pending_jd_count": len(pending_shortlist_ids),
        "reused_jd_count": len(reused_details),
        "jd_pending_count": sum(not job.get("jd_fetched") for job in new_jobs),
        "new_jd_requests": 0,
        "new_llm_requests": 0,
        "reused_successful_model_reviews": len(reused_reviews),
        "screening_version": final.get("metadata", {}).get("screening_version"),
        "jobs": detailed,
    }
    pipeline.write_json(output_root / "screening_comparison.json", comparison)

    markdown = ["# 上海同样本：放宽规则前后对照", "", f"样本：同一批 {len(ids)} 个不同岗位，ID、顺序、原始标题/技能及全部来源不变。", "", "| 分层 | 旧规则 | 新规则 |", "| --- | ---: | ---: |"]
    markdown.extend(f"| {name} | {comparison['before'][name]} | {comparison['after'][name]} |" for name in pipeline.CLASSIFICATIONS)
    markdown.extend(["", f"新增保留 {len(newly_retained)} 个；复用 {len(reused_details)} 份原 JD。保留候选中 {comparison['retained_with_jd_count']} 个有 JD、{comparison['retained_pending_jd_count']} 个仍待补 JD。", "", "本次只验证规则变化，不补抓 JD、不发起模型请求、不投递。无 JD 的候选必须继续核实，不能等同于已满足任职要求。", "", "## 逐岗变化", ""])
    for job in detailed:
        title = pipeline.text(job["title"]).replace("\n", " ")
        markdown.append(f"- {title}：{job['old_classification']} → {job['new_classification']}；{job['new_reason']}（{job['jd_status']}）")
    (output_root / "规则放宽前后对照.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    finished = pipeline.now_iso()
    jd_manifest = {"status": "reused_only", "total_jobs": len(shortlist), "reused_count": len(reused_details), "pending_count": len(pending_shortlist_ids), "records": [], "pending_job_ids": pending_shortlist_ids, "source_run": str(source_run), "note": "本次未调用采集器；既有详情原文复用，其余待补抓。"}
    pipeline.write_json(prepared / "jd_shortlists" / "details_manifest.json", jd_manifest)
    pipeline.write_json(output_root / "run_manifest.json", {
        "schema_version": "1.0", "source_root": metadata.get("source_root"),
        "source_run": str(source_run), "output_root": str(output_root),
        "city": metadata.get("city"), "track": metadata.get("tracks", "all"),
        "sample_size": len(ids), "sample_stats": final.get("metadata"),
        "started_at": started, "finished_at": finished, "updated_at": finished,
        "status": "partial" if pending_shortlist_ids else "success", "test_mode": True,
        # The rescreen manifest keeps the historical three-layer comparison
        # shape; the full report metadata separately records the pending layer.
        "mode": "same_sample_rule_only_rescreen", "classification_counts": _counts(new_jobs),
        "jd_manifest": jd_manifest, "llm_manifest": llm_manifest,
        "stages": {"列表前处理": {"status": "success"}, "JD补抓": {"status": "not_run", "message": "复用已有JD，未新采集"}, "Codex语义复核": {"status": "not_run"}, "生成报告": {"status": "success"}},
        "warnings": ["这是同样本规则对照；JD缺失岗位只是方向候选，资格、时效和实际职责仍待核实。"],
        "output_files": {"raw_sample": str(output_root / "raw_sample.json"), "merged_jobs": str(prepared / "merged_jobs_index.json"), "hai_tou_pool": str(prepared / "hai_tou_pool.json"), "report": str(prepared / "海投分层清单.md"), "comparison": str(output_root / "screening_comparison.json")},
    })
    return {key: value for key, value in comparison.items() if key not in ("jobs", "source_input_sha256")}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--expected-size", type=int)
    args = parser.parse_args(argv)
    result = rescreen_saved_run(args.source_run, args.output_root, args.expected_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
