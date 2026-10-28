"""Offline regressions for the collection and report review findings."""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import collect_architecture as collector
import jd_parallel as adapter
import process_architecture as processor
from review_cache import review_input_signature
from test_dual_browser_jd import _FakeBrowser, _FakeScraper, _job, FULL_JD, ZERO_TIMING


class CollectionReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.chrome, self.edge = _FakeBrowser(), _FakeBrowser()
        self.scraper = _FakeScraper("offline_review_regressions")
        self.scraper.requested_ports = []
        self.scraper.browsers = {9222: self.chrome, 9223: self.edge}

    def collect(self, jobs, output=None, **kwargs):
        return adapter.collect_details(
            jobs, output or self.root / "details.json", scraper=self.scraper, workers=1,
            timing=ZERO_TIMING, browser_mode="dual", emit=lambda _: None,
            cdp_factories={"chrome": self.chrome.connect, "edge": self.edge.connect}, **kwargs)

    def fixture(self, count=4):
        shortlist = self.root / "prepared/jd_shortlists"
        processor.write_json(shortlist / "jd_input.json", {"jobs": [_job(n) for n in range(1, count + 1)]})
        scraper_root = self.root / "fake-scraper"
        entry = scraper_root / "scripts/boss_cdp_raw.py"
        entry.parent.mkdir(parents=True)
        entry.write_text("# Offline fake; never executed.\n", encoding="utf-8")
        return shortlist, scraper_root

    def adapter_cli(self, command, **_kwargs):
        # Exercise the actual adapter argument parser and readiness-file reader.
        captured = io.StringIO()
        with patch.object(adapter, "load_scraper", return_value=self.scraper), \
                patch.dict(adapter._TIMINGS, {"fast": ZERO_TIMING}), redirect_stdout(captured):
            code = adapter.main(command[3:])
        return subprocess.CompletedProcess(command, code, captured.getvalue(), "")

    def test_adapter_recovers_both_sidecars_before_changed_remaining_input(self):
        output = self.root / "details.json"
        first = {**_job(1), "jd": FULL_JD, "status": "success", "jd_complete": True}
        saved = {**_job(3), "jd": "SIDE_SENTINEL\n" + FULL_JD, "status": "success", "jd_complete": True}
        processor.write_json(output, [first])
        processor.write_json(output.with_name("details.chrome.json"), [first])
        processor.write_json(output.with_name("details.edge.json"), [saved])
        self.chrome.snapshots["job-3"] = {"url": "https://www.zhipin.com/security-check"}

        result = self.collect([_job(2), _job(3), _job(4)], output)

        self.assertEqual(self.chrome.navigation, ["job-2"])
        self.assertEqual(self.edge.navigation, ["job-4"])
        self.assertEqual({row["job_id"] for row in result.records}, {"job-1", "job-2", "job-3", "job-4"})
        self.assertEqual(processor.load_details(output)["job-3"], saved)
        self.assertEqual(result.status, "completed")

    def test_fetch_entry_recovers_old_batch_sidecar_before_rebatching(self):
        shortlist, scraper_root = self.fixture()
        saved = {**_job(3), "jd": "UNMERGED_EDGE_SENTINEL\n" + FULL_JD, "status": "success", "jd_complete": True}
        processor.write_json(shortlist / "details.json", [{**_job(1), "jd": FULL_JD}])
        # Prior batch_size=1; the old batch no longer exists in the new grouping.
        sidecar = shortlist / "detail_batches/details_0003.edge.json"
        processor.write_json(sidecar, [saved])
        before = sidecar.read_bytes()
        with patch.object(processor, "ensure_detail_cdp_ready", return_value=(True, None)), \
                patch.object(processor, "run_detail_subprocess", side_effect=self.adapter_cli), redirect_stdout(io.StringIO()):
            result = processor.fetch_jd(self.root, scraper_root, batch_size=4, jd_workers=1,
                                        python_path=sys.executable, browser_mode="dual", wait_on_gate=False)
        self.assertEqual(self.chrome.navigation, ["job-2"])
        self.assertEqual(self.edge.navigation, ["job-4"])
        self.assertEqual(processor.load_details(shortlist / "details.json")["job-3"], saved)
        self.assertEqual(sidecar.read_bytes(), before)
        self.assertEqual(result["manifest"]["records"][0]["status"], "success")

    def test_readiness_reaches_adapter_cli_and_unready_side_never_connects(self):
        for ready_browser in ("chrome", "edge"):
            with self.subTest(ready_browser=ready_browser):
                # New output scope for each direction, with the same fake ports.
                run_root = self.root / ready_browser
                run_root.mkdir()
                with patch.object(self, "root", run_root):
                    shortlist, scraper_root = self.fixture()
                    self.chrome.navigation.clear()
                    self.edge.navigation.clear()
                    self.chrome.connections.clear()
                    self.edge.connections.clear()

                    def readiness(_entry, _python, _port, *, browser, **_kwargs):
                        return (True, None) if browser == ready_browser else (False, "Offline login precheck failed")

                    with patch.object(processor, "ensure_detail_cdp_ready", side_effect=readiness), \
                            patch.object(processor, "run_detail_subprocess", side_effect=self.adapter_cli), redirect_stdout(io.StringIO()):
                        result = processor.fetch_jd(run_root, scraper_root, batch_size=4, jd_workers=1,
                                                    python_path=sys.executable, browser_mode="dual", wait_on_gate=False)
                    unavailable = self.edge if ready_browser == "chrome" else self.chrome
                    available = self.chrome if ready_browser == "chrome" else self.edge
                    self.assertEqual(unavailable.connections, [])
                    self.assertEqual(unavailable.navigation, [])
                    self.assertEqual(len(available.navigation), 2)
                    record = result["manifest"]["records"][0]
                    self.assertEqual(record["status"], "paused")
                    self.assertEqual(record["fetched_count"], 2)
                    unready_browser = "edge" if ready_browser == "chrome" else "chrome"
                    self.assertEqual(record["browser_results"][unready_browser]["status"], "paused")
                    self.assertEqual(len(processor.load_details(shortlist / "details.json")), 4)

    def test_one_pending_job_requires_chrome_even_if_edge_is_available(self):
        _shortlist, scraper_root = self.fixture(1)
        with patch.object(processor, "ensure_detail_cdp_ready", return_value=(False, "Chrome unavailable")) as check, \
                patch.object(processor, "run_detail_subprocess") as run, redirect_stdout(io.StringIO()):
            result = processor.fetch_jd(self.root, scraper_root, python_path=sys.executable,
                                        browser_mode="dual", wait_on_gate=False)
        self.assertEqual([call.kwargs["browser"] for call in check.call_args_list], ["chrome"])
        run.assert_not_called()
        self.assertEqual(result["manifest"]["records"][0]["status"], "paused")

    def test_merged_write_failure_broadcasts_stop_and_keeps_side_checkpoints(self):
        parent = adapter.StopState()
        observed = []
        output = self.root / "details.json"

        def fail_merged(path, records):
            if records:
                observed.append(len(self.chrome.navigation) + len(self.edge.navigation))
                raise OSError("Offline merged write failure")
            self.scraper._atomic_write_json(path, records)

        started = time.monotonic()
        with self.assertRaisesRegex(OSError, "merged write failure"):
            adapter.collect_details([_job(n) for n in range(1, 41)], output, scraper=self.scraper,
                                    workers=1, timing=adapter.RenderTiming(0.02, 0.02, 0, 0.002),
                                    browser_mode="dual", control=parent, persist=fail_merged, emit=lambda _: None,
                                    cdp_factories={"chrome": self.chrome.connect, "edge": self.edge.connect})
        self.assertTrue(parent.event.is_set())
        self.assertLess(len(self.chrome.navigation) + len(self.edge.navigation), 40)
        self.assertLessEqual(len(self.chrome.navigation) + len(self.edge.navigation), observed[0] + 2)
        self.assertLess(time.monotonic() - started, adapter.SHUTDOWN_TIMEOUT)
        self.assertTrue(all(connection.closed for connection in self.chrome.connections + self.edge.connections))
        side_records = [record for name in ("chrome", "edge")
                        for record in processor.detail_records_from_file(output.with_name(f"details.{name}.json"))]
        self.assertTrue(any(processor.detail_completeness(record) == "complete" for record in side_records))

    def test_side_fatal_error_stops_other_coordinator_before_waiting(self):
        parent = adapter.StopState()
        peer_stopped = threading.Event()

        def fail_side(_data, _output, *, browser, control, **_kwargs):
            if browser == "edge":
                raise RuntimeError("Offline fatal side thread")
            self.assertTrue(control.event.wait(2), "Peer was not stopped before coordinator cleanup")
            peer_stopped.set()
            return adapter.BatchResult([], "stopped", 2)

        with patch.object(adapter, "_collect_details_single", side_effect=fail_side), \
                self.assertRaisesRegex(RuntimeError, "fatal side thread"):
            self.collect([_job(n) for n in range(1, 5)], control=parent)
        self.assertTrue(parent.event.is_set())
        self.assertTrue(peer_stopped.is_set())

    def test_false_complete_flag_with_empty_body_is_requested_again(self):
        processor.write_json(self.root / "details.edge.json", [{**_job(2), "jd": "", "status": "success", "jd_complete": True}])
        result = self.collect([_job(1), _job(2)])
        self.assertEqual(self.edge.navigation, ["job-2"])
        self.assertTrue(all(processor.detail_completeness(record) == "complete" for record in result.records))

    def test_cli_collection_conflict_refuses_all_writes_and_legacy_fields_still_work(self):
        config = collector.load_config(collector.DEFAULT_CONFIG)
        old_spec = collector.build_specs(config, limit=1)[0]
        raw = self.root / "raw" / old_spec["track"] / old_spec["city_id"] / (old_spec["combination"] + ".json")
        manifest = self.root / "collection_manifest.json"
        collector.write_json_atomic(raw, {"jobs": [{"job_id": "saved"}], "keyword": old_spec["keyword"]})
        collector.write_json_atomic(manifest, {"records": [{**old_spec, "status": "success"}]})
        before_manifest, before_raw = manifest.read_bytes(), raw.read_bytes()
        changed = deepcopy(config)
        for track in ("current", "target"):
            for keyword in changed[track]["keywords"]:
                if keyword["id"] == old_spec["keyword_id"]:
                    keyword["query"] = "Offline changed query"
        config_file = self.root / "changed.json"
        collector.write_json_atomic(config_file, changed)
        with patch.object(collector, "resolve_scraper_root") as resolve, \
                patch.object(collector.subprocess, "Popen", side_effect=AssertionError("No real process")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = collector.main(["--root", str(self.root), "--config", str(config_file), "--limit", "1"])
        self.assertEqual(code, 20)
        resolve.assert_not_called()
        self.assertEqual(manifest.read_bytes(), before_manifest)
        self.assertEqual(raw.read_bytes(), before_raw)

        collector.write_json_atomic(manifest, {"records": [{"combination": old_spec["combination"], "status": "success"}]})
        with patch.object(collector, "resolve_scraper_root", return_value=self.root / "fake-scraper"), \
                patch.object(collector, "resolve_python", return_value=Path(sys.executable)), \
                patch.object(collector.subprocess, "Popen", side_effect=AssertionError("Legacy success should skip")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(collector.main(["--root", str(self.root), "--limit", "1"]), 0)
        self.assertEqual(raw.read_bytes(), before_raw)


class ReportAndSourceRegressionTests(unittest.TestCase):
    def test_cli_and_desktop_report_decisions_share_pending_and_manual_exclusion(self):
        import review_pipeline

        prompt = "Offline report parity rules"
        for judgment in ("符合", "不符合", "信息不足", None):
            for excluded in (None, "shortlist"):
                with self.subTest(judgment=judgment, excluded=excluded):
                    job = {"job_id": "parity", "title": "AI产品经理", "salary": "20-30K",
                           "jd": FULL_JD, "jd_completeness": "complete", "review_format": "simple-v1",
                           "excluded_at": excluded, "review_selection": {"shortlist": {"keep": excluded is None}}}
                    original = deepcopy(job)
                    review = None if judgment is None else {
                        "job_id": "parity", "review_format": "simple-v1", "status": "success",
                        "判断": judgment, "理由": "离线报告一致性。", "cache_signature": "offline-signature",
                        "cache_input_signature": review_input_signature(processor.model_input(job), prompt)}
                    cli = processor.analyze_report_job(job, review, prompt=prompt)
                    desktop = processor.analyze_report_job(job, review, prompt=prompt, analyzer=review_pipeline._analyze)
                    expected = "暂不投" if excluded or judgment == "不符合" else "待核实" if judgment in (None, "信息不足") else "海投池"
                    self.assertEqual(cli["classification"], expected)
                    self.assertEqual(desktop["classification"], expected)
                    self.assertEqual(processor.report_pool_eligible(cli), processor.report_pool_eligible(desktop))
                    self.assertEqual(cli["excluded_at"], excluded)
                    self.assertEqual(cli["review_selection"], original["review_selection"])
                    self.assertEqual(job, original)
                    self.assertEqual(processor.report_decision(cli), cli)

    def test_cli_five_job_report_has_one_actionable_and_three_pending(self):
        prompt = "Offline current rules"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = [{"job_id": key, "title": "AI产品经理", "salary": "20-30K", "jd": FULL_JD,
                     "jd_completeness": "complete", "observed_in": []}
                    for key in ("yes", "no", "unknown", "failed", "missing")]
            reviews = []
            for job, judgment in zip(jobs, ("符合", "不符合", "信息不足", None)):
                review = {"job_id": job["job_id"], "review_format": "simple-v1",
                          "status": "success" if judgment else "failed", "cache_signature": "offline-signature",
                          "cache_input_signature": review_input_signature(processor.model_input(job), prompt)}
                if judgment:
                    review.update({"判断": judgment, "理由": "离线入口回归。"})
                else:
                    review["error"] = "Offline request failure"
                reviews.append(review)
            processor.write_json(root / "prepared/merged_jobs_index.json", {"jobs": jobs})
            processor.write_json(root / "prepared/llm_reviews.json", {"reviews": reviews})
            processor.write_json(root / "prepared/llm_manifest.json", {"review_format": "simple-v1"})
            with patch.object(processor, "report_prompt", return_value=prompt), \
                    patch("llm_client.load_codex_config", side_effect=AssertionError("Report must stay offline")), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(processor.main(["--root", str(root), "--stage", "report"]), 0)
            final = processor.read_json(root / "prepared/merged_jobs_index.json")
            pool = processor.read_json(root / "prepared/hai_tou_pool.json")
            markdown = (root / "prepared/海投分层清单.md").read_text(encoding="utf-8")
            expected = {"重点投递": 0, "海投池": 1, "暂不投": 1, "待核实": 3}
            self.assertEqual(final["metadata"]["classification_counts"], expected)
            self.assertEqual(final["metadata"]["pending_verification_count"], 3)
            self.assertEqual(final["metadata"]["analysis_pending_count"], 3)
            self.assertEqual([job["job_id"] for job in pool["jobs"]], ["yes"])
            self.assertEqual(pool["summary"]["total"], 1)
            self.assertEqual(sum(pool["summary"]["classification_counts"].values()), 1)
            self.assertEqual(pool["metadata"]["unique_job_count"], 1)
            self.assertEqual(pool["metadata"]["all_classification_counts"], expected)
            for classification, count in expected.items():
                self.assertRegex(markdown, rf"(?m)^## {re.escape(classification)}（{count}）$")
            self.assertEqual(len(re.findall(r"(?m)^- \[", markdown)), 5)
            overview = markdown.split("以下为全部岗位详细证据。", 1)[0]
            self.assertEqual(len(re.findall(r"(?m)^\| 海投池 \|", overview)), 1)

    @staticmethod
    def source_rows(salaries=("10-16K", "20-30K")):
        return [{"job_id": "same", "title": "AI产品经理" if index == 0 else "AI应用架构师",
                 "salary": salary, "skills": "RAG | 方案设计" if index == 0 else "Agent | 系统集成",
                 "source_file": f"raw/{index}.json", "source_row": index,
                 "source_context": {"track": "current" if index == 0 else "target", "keyword": "大对象" * 100}}
                for index, salary in enumerate(salaries)]

    def test_source_reversal_preserves_candidates_sources_and_screening(self):
        rows = self.source_rows()
        forward = processor.merge_rows(rows)[0][0]
        reverse = processor.merge_rows(reversed(rows))[0][0]
        remerged = processor.merge_rows([forward])[0][0]
        for field in ("title", "salary", "skills", "field_candidates", "field_conflicts"):
            self.assertEqual(forward[field], reverse[field])
            self.assertEqual(forward[field], remerged[field])
        self.assertEqual(set(forward["field_conflicts"]), {"title", "salary", "skills"})
        self.assertEqual({source["source_file"] for source in forward["observed_in"]}, {"raw/0.json", "raw/1.json"})
        self.assertEqual(processor.score_job(forward), processor.score_job(reverse))
        self.assertTrue(processor.score_job(forward)["list_pass"])
        self.assertEqual(processor.score_job(forward)["list_queue"], "verify")
        self.assertFalse(processor.score_job(forward)["list_salary_below_target"])
        for candidates in forward["field_candidates"].values():
            for candidate in candidates:
                self.assertTrue(candidate["sources"])
                self.assertTrue(all("keyword" not in source for source in candidate["sources"]))

    def test_only_all_explicit_low_monthly_ceilings_can_exclude_conflicted_salary(self):
        for salaries, excluded in ((("10-16K", "12-15K"), True), (("10-16K", "20-30K"), False),
                                   (("10-16K", "面议"), False), (("10-16K", "15K以上"), False),
                                   (("10-16K", "12-17K"), False), (("10-16K", "15万/年"), False)):
            with self.subTest(salaries=salaries):
                rows = self.source_rows(salaries)
                merged = processor.merge_rows(rows)[0][0]
                score = processor.score_job(merged)
                self.assertEqual(score["list_salary_below_target"], excluded)
                self.assertEqual(score["list_pass"], not excluded)
                self.assertEqual(score["list_queue"], "excluded" if excluded else "verify")

    def test_conflicts_do_not_erase_explicit_role_exclusions_or_rejudge_simple_ai(self):
        rows = self.source_rows()
        rows[1].update(title="AI电话销售经理", skills="电话销售 | 陌拜 | 开发新客户")
        merged = processor.merge_rows(rows)[0][0]
        self.assertTrue(processor.score_job(merged)["list_excluded"])
        self.assertEqual(processor.score_job(merged)["list_queue"], "excluded")
        for judgment, classification in (("符合", "海投池"), ("不符合", "暂不投")):
            with self.subTest(judgment=judgment):
                final = processor.merge_model_review({**merged, "jd": FULL_JD, "jd_completeness": "complete"},
                    {"review_format": "simple-v1", "status": "success", "判断": judgment, "理由": "以AI结论为准。"})
                self.assertEqual(final["classification"], classification)
                self.assertFalse(final["analysis_pending"])
                self.assertTrue(any("多来源" in note for note in final["verification"]))


if __name__ == "__main__":
    unittest.main()
