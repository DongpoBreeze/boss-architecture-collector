import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from process_architecture import (  # noqa: E402
    detail_completeness,
    fetch_jd,
    load_details,
    merge_detail_records,
    read_json,
    write_json,
)


class DetailCompletenessTests(unittest.TestCase):
    def test_empty_text_is_missing_even_with_success_or_failure_flags(self):
        for record in (None, {}, {"jd": None}, {"jd": " \n\t"},
                       {"jd": "", "complete": True, "status": "success"},
                       {"jd": "", "complete": False, "status": "failed"}):
            with self.subTest(record=record):
                self.assertEqual(detail_completeness(record), "missing")

    def test_nonempty_text_with_explicit_partial_markers_is_partial(self):
        flags = [{"complete": False}, {"jd_complete": False}, {"truncated": True}]
        for field in ("status", "jd_status", "jd_completeness"):
            for marker in ("PARTIAL", "incomplete", "failed", "error", "部分", "截断"):
                flags.append({field: f"result: {marker}"})
        flags.append({"complete": True, "jd_complete": True, "truncated": True})
        for flag in flags:
            with self.subTest(flag=flag):
                self.assertEqual(detail_completeness({"jd": "职责正文", **flag}), "partial")

    def test_legacy_nonempty_text_and_explicit_complete_remain_complete(self):
        for flags in ({}, {"complete": True}, {"jd_complete": True}, {"truncated": False},
                      {"status": "success", "jd_status": "complete", "jd_completeness": "complete"}):
            with self.subTest(flags=flags):
                self.assertEqual(detail_completeness({"jd": "完整职责正文", **flags}), "complete")

    def test_merge_prefers_completeness_before_length_without_mutating_records(self):
        records = [
            {"job_id": "job", "jd": "", "status": "failed"},
            {"job_id": "job", "jd": "截断职责" * 40, "truncated": True},
            {"job_id": "job", "jd": "完整职责", "jd_complete": True},
        ]
        original = deepcopy(records)
        for ordered in (records, list(reversed(records))):
            with self.subTest(order=[detail_completeness(item) for item in ordered]):
                details = merge_detail_records({}, ordered)
                self.assertEqual(details, {"job": records[2]})
                self.assertIs(merge_detail_records(details, records[:2]), details)
                self.assertEqual(details["job"], records[2])
        self.assertEqual(records, original)

    def test_merge_uses_longer_text_within_a_tier_and_keeps_stable_ties(self):
        for flags in ({}, {"truncated": True}):
            short = {"job_id": "job", "jd": "职责", **flags}
            long = {"job_id": "job", "jd": "更详细的职责", **flags}
            padded = {"job_id": "job", "jd": " " * 100 + "职责" + " " * 100, **flags}
            tie = {**long, "source": "later record"}
            with self.subTest(flags=flags):
                self.assertEqual(merge_detail_records({}, [short, long, padded, tie])["job"], long)
                self.assertEqual(merge_detail_records({}, [long, short])["job"], long)

    def test_load_details_uses_the_same_quality_order_for_all_supported_wrappers(self):
        partial = {"job_id": "job", "jd": "长但不完整的职责" * 30, "jd_status": "partial"}
        complete = {"job_id": "job", "jd": "完整职责"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "details.json"
            for records in ([partial, complete], [complete, partial]):
                for wrapper in (None, "jobs", "details", "records"):
                    with self.subTest(wrapper=wrapper, first=records[0]):
                        value = [None, "invalid", {}, *records]
                        write_json(path, value if wrapper is None else {wrapper: value})
                        original = path.read_bytes()
                        self.assertEqual(load_details(path), {"job": complete})
                        self.assertEqual(path.read_bytes(), original)


class FetchJDCompletenessTests(unittest.TestCase):
    def _fixture(self, directory: str, job_count: int = 1) -> tuple[Path, Path, Path]:
        output_root = Path(directory)
        shortlist = output_root / "prepared" / "jd_shortlists"
        write_json(shortlist / "jd_input.json", {
            "jobs": [{"job_id": f"job-{number}", "title": "AI 架构岗位"}
                     for number in range(1, job_count + 1)]
        })
        scraper_root = output_root / "scraper"
        entry = scraper_root / "scripts" / "boss_cdp_raw.py"
        entry.parent.mkdir(parents=True)
        entry.write_text("# Offline fixture; execution is always mocked.\n", encoding="utf-8")
        return output_root, scraper_root, shortlist

    def test_recovered_batch_cache_is_durable_before_adapter_replaces_batch_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root, shortlist = self._fixture(directory, job_count=2)
            complete = {"job_id": "job-1", "jd": "已完成的完整正文", "jd_complete": True}
            batch_file = shortlist / "detail_batches/details_0001.json"
            write_json(batch_file, [complete])
            self.assertFalse((shortlist / "details.json").exists())

            def interrupted(command, **kwargs):
                self.assertEqual(load_details(shortlist / "details.json"), {"job-1": complete})
                pending = read_json(Path(command[command.index("--input") + 1]))["jobs"]
                self.assertEqual([j["job_id"] for j in pending], ["job-2"])
                write_json(Path(command[command.index("--detail-output") + 1]), [])
                raise KeyboardInterrupt

            with patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None)), \
                 patch("process_architecture.run_detail_subprocess", side_effect=interrupted), \
                 redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
                fetch_jd(output_root, scraper_root, batch_size=2, python_path=sys.executable)
            self.assertEqual(load_details(shortlist / "details.json"), {"job-1": complete})

    def test_partial_is_refetched_complete_is_skipped_and_short_complete_replaces_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root, shortlist = self._fixture(directory, job_count=3)
            partial = {"job_id": "job-1", "jd": "旧的截断职责" * 40, "complete": False}
            already_complete = {"job_id": "job-2", "jd": "旧版完整职责"}
            write_json(shortlist / "details.json", [partial, already_complete,
                                                    {"job_id": "job-3", "jd": "", "status": "failed"}])
            write_json(shortlist / "details_manifest.json", {"records": [{
                "batch": 1, "status": "success", "job_ids": ["job-1", "job-2", "job-3"]
            }]})
            calls = []
            complete = {"job_id": "job-1", "jd": "完整职责", "jd_complete": True}
            remaining_partial = {"job_id": "job-3", "jd": "尚未补全的职责", "jd_status": "incomplete"}

            def collect(command, **kwargs):
                pending = read_json(Path(command[command.index("--input") + 1]))["jobs"]
                calls.append([job["job_id"] for job in pending])
                output = Path(command[command.index("--detail-output") + 1])
                write_json(output, [complete, remaining_partial] if len(calls) == 1
                           else [{"job_id": "job-3", "jd": "补全职责"}])
                return subprocess.CompletedProcess(command, 0, "抓取完成\n", "")

            with patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None)) as cdp, \
                    patch("process_architecture.run_detail_subprocess", side_effect=collect), \
                    redirect_stdout(io.StringIO()):
                result = fetch_jd(output_root, scraper_root, batch_size=3, python_path=sys.executable)
                self.assertEqual(calls, [["job-1", "job-3"]])
                record = result["manifest"]["records"][0]
                self.assertEqual(record["status"], "partial")
                self.assertEqual(record["fetched_count"], 2)
                self.assertEqual(record["progress_completed"], 2)
                self.assertEqual(record["missing_job_ids"], ["job-3"])
                details = load_details(shortlist / "details.json")
                self.assertEqual(details["job-1"], complete)
                self.assertEqual(details["job-2"], already_complete)
                self.assertEqual(details["job-3"], remaining_partial)
                self.assertEqual(result["details"], 3)  # Partial evidence remains in the checkpoint.

                resumed = fetch_jd(output_root, scraper_root, batch_size=3, python_path=sys.executable)
                self.assertEqual(calls, [["job-1", "job-3"], ["job-3"]])
                self.assertEqual(resumed["manifest"]["records"][0]["fetched_count"], 3)
                self.assertEqual(resumed["manifest"]["records"][0]["status"], "success")
                self.assertEqual(cdp.call_count, 2)

            with patch("process_architecture.ensure_detail_cdp_ready") as cdp, \
                    patch("process_architecture.run_detail_subprocess") as collect_mock, \
                    redirect_stdout(io.StringIO()):
                skipped = fetch_jd(output_root, scraper_root, batch_size=3, python_path=sys.executable)
            cdp.assert_not_called()
            collect_mock.assert_not_called()
            self.assertEqual(skipped["manifest"]["records"][0]["fetched_count"], 3)

    def test_recovered_long_partial_does_not_downgrade_complete_or_touch_raw_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root, shortlist = self._fixture(directory)
            complete = {"job_id": "job-1", "jd": "完整职责"}
            partial = {"job_id": "job-1", "jd": "更长的截断职责" * 50, "truncated": True}
            write_json(shortlist / "details.json", [complete])
            checkpoint = shortlist / "detail_batches" / "details_0001.json"
            write_json(checkpoint, [partial])
            original = checkpoint.read_bytes()
            with patch("process_architecture.ensure_detail_cdp_ready") as cdp, \
                    patch("process_architecture.run_detail_subprocess") as collect_mock, \
                    redirect_stdout(io.StringIO()):
                result = fetch_jd(output_root, scraper_root, python_path=sys.executable)
            cdp.assert_not_called()
            collect_mock.assert_not_called()
            self.assertEqual(load_details(shortlist / "details.json"), {"job-1": complete})
            self.assertEqual(checkpoint.read_bytes(), original)
            self.assertTrue(result["manifest"]["records"][0]["recovered_from_checkpoint"])
            self.assertEqual(result["manifest"]["records"][0]["fetched_count"], 1)

    def test_failed_retry_retains_partial_batch_evidence_but_does_not_count_it_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root, shortlist = self._fixture(directory)
            checkpoint = shortlist / "detail_batches" / "details_0001.json"
            partial = {"job_id": "job-1", "jd": "保留的部分职责", "jd_completeness": "partial"}
            write_json(checkpoint, [partial])
            original = checkpoint.read_bytes()
            with patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None)), \
                    patch("process_architecture.run_detail_subprocess", return_value=
                          subprocess.CompletedProcess([], 2, "network failure\n", "")) as collect_mock, \
                    redirect_stdout(io.StringIO()):
                result = fetch_jd(output_root, scraper_root, python_path=sys.executable)
            collect_mock.assert_called_once()
            self.assertEqual(load_details(shortlist / "details.json"), {"job-1": partial})
            self.assertEqual(checkpoint.read_bytes(), original)
            self.assertEqual(result["details"], 1)
            record = result["manifest"]["records"][0]
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["fetched_count"], 0)
            self.assertEqual(record["progress_completed"], 0)
            self.assertEqual(record["missing_job_ids"], ["job-1"])

    def test_empty_failed_result_is_missing_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root, shortlist = self._fixture(directory)

            def collect(command, **kwargs):
                output = Path(command[command.index("--detail-output") + 1])
                write_json(output, [{"job_id": "job-1", "jd": "", "status": "failed"}])
                return subprocess.CompletedProcess(command, 2, "network failure\n", "")

            with patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None)), \
                    patch("process_architecture.run_detail_subprocess", side_effect=collect), \
                    redirect_stdout(io.StringIO()):
                result = fetch_jd(output_root, scraper_root, python_path=sys.executable)
            self.assertEqual(detail_completeness(load_details(shortlist / "details.json")["job-1"]), "missing")
            self.assertEqual(result["manifest"]["records"][0]["fetched_count"], 0)
            self.assertEqual(result["manifest"]["records"][0]["missing_job_ids"], ["job-1"])

    def test_live_progress_counts_only_unique_complete_checkpoint_records(self):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root, _shortlist = self._fixture(directory, job_count=2)

            def collect(command, **kwargs):
                output = Path(command[command.index("--detail-output") + 1])
                partial = {"job_id": "job-2", "jd": "较长的部分职责", "truncated": True}
                write_json(output, [{"job_id": "job-1", "jd": "部分职责", "complete": False}, partial])
                kwargs["on_line"]("[1/2] JD: 4 字\n")
                write_json(output, [{"job_id": "job-1", "jd": "完整职责"}, partial])
                kwargs["on_line"]("[1/2] JD: 4 字\n")
                kwargs["on_line"]("[2/2] JD: 7 字\n")
                return subprocess.CompletedProcess(command, 0, "抓取完成\n", "")

            displayed = io.StringIO()
            with patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None)), \
                    patch("process_architecture.run_detail_subprocess", side_effect=collect), \
                    redirect_stdout(displayed):
                result = fetch_jd(output_root, scraper_root, batch_size=2, python_path=sys.executable)
            progress = [line for line in displayed.getvalue().splitlines() if line.startswith("[JD 总进度]")]
            self.assertEqual(progress, [
                "[JD 总进度] 0/2 已完成，当前批次 0/2。",
                "[JD 总进度] 1/2 已完成，当前批次 1/2。",
                "[JD 总进度] 1/2 已完成，当前批次 1/2。",
            ])
            self.assertEqual(result["manifest"]["records"][0]["fetched_count"], 1)

    def test_narrowed_scope_discards_old_gate_and_reuses_complete_details_without_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root, shortlist = self._fixture(directory, job_count=2)
            jobs = read_json(shortlist / "jd_input.json")["jobs"]

            def collect(command, **kwargs):
                pending = read_json(Path(command[command.index("--input") + 1]))["jobs"]
                self.assertEqual(len(pending), 1)
                key = pending[0]["job_id"]
                write_json(Path(command[command.index("--detail-output") + 1]), [{
                    "job_id": key, "jd": "完整职责", "source": f"raw/{key}.json",
                    "observed_in": [{"track": "current", "city": "上海"}],
                }])
                return subprocess.CompletedProcess(command, 1 if key == "job-2" else 0,
                                                   "security_check required\n" if key == "job-2" else "完成\n", "")

            with patch("process_architecture.ensure_detail_cdp_ready", return_value=(True, None)), \
                    patch("process_architecture.run_detail_subprocess", side_effect=collect) as collect_mock, \
                    redirect_stdout(io.StringIO()):
                first = fetch_jd(output_root, scraper_root, batch_size=1,
                                 python_path=sys.executable, wait_on_gate=False)
            self.assertEqual(collect_mock.call_count, 2)
            self.assertEqual([record["status"] for record in first["manifest"]["records"]], ["success", "paused"])
            self.assertTrue(first["manifest"]["records"][1]["security_or_login_gate"])
            self.assertEqual(first["manifest"]["records"][1]["fetched_count"], 1)
            cached = load_details(shortlist / "details.json")
            checkpoints = {path: path.read_bytes() for path in (shortlist / "detail_batches").glob("details_*.json")}

            write_json(shortlist / "jd_input.json", {"jobs": [jobs[1]]})
            with patch("process_architecture.ensure_detail_cdp_ready") as cdp, \
                    patch("process_architecture.run_detail_subprocess") as collect_mock, \
                    redirect_stdout(io.StringIO()):
                for _ in range(2):
                    resumed = fetch_jd(output_root, scraper_root, batch_size=1,
                                       python_path=sys.executable, wait_on_gate=False)
                    records = resumed["manifest"]["records"]
                    self.assertEqual([(record["batch"], record["job_ids"], record["status"])
                                      for record in records], [(1, ["job-2"], "success")])
                    self.assertFalse(any(record.get("security_or_login_gate") for record in records))
                    self.assertEqual(read_json(shortlist / "details_manifest.json"), resumed["manifest"])
            cdp.assert_not_called()
            collect_mock.assert_not_called()
            self.assertEqual(resumed["manifest"]["total_jobs"], 1)
            self.assertEqual(load_details(shortlist / "details.json"), cached)
            self.assertEqual({path: path.read_bytes() for path in checkpoints}, checkpoints)

    def test_rebatching_persists_only_current_groups_before_a_real_gate_still_pauses(self):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root, shortlist = self._fixture(directory, job_count=3)
            complete = {"job_id": "job-3", "jd": "缓存完整职责", "source": "raw/job-3.json"}
            write_json(shortlist / "details.json", [complete])
            valid = {"batch": 2, "job_ids": ["job-3"], "status": "success", "security_or_login_gate": False}
            write_json(shortlist / "details_manifest.json", {"batch_size": 1, "records": [
                {"batch": 1, "job_ids": ["job-1"], "status": "success"},
                {"batch": 2, "job_ids": ["job-2"], "status": "paused", "security_or_login_gate": True},
                {"batch": 3, "job_ids": ["job-3"], "status": "paused", "security_or_login_gate": True},
                valid,
            ]})
            old_checkpoint = shortlist / "detail_batches" / "details_0003.json"
            write_json(old_checkpoint, [complete])
            original = old_checkpoint.read_bytes()

            def cdp_ready(*args):
                persisted = read_json(shortlist / "details_manifest.json")
                self.assertEqual(persisted["records"], [valid])
                self.assertEqual(persisted["batch_size"], 2)
                self.assertEqual(persisted["total_jobs"], 3)
                return True, None

            def collect(command, **kwargs):
                pending = read_json(Path(command[command.index("--input") + 1]))["jobs"]
                self.assertEqual([job["job_id"] for job in pending], ["job-1", "job-2"])
                write_json(Path(command[command.index("--detail-output") + 1]), [
                    {"job_id": "job-1", "jd": "完整职责"},
                    {"job_id": "job-2", "jd": "部分职责", "truncated": True},
                ])
                return subprocess.CompletedProcess(command, 1, "security_check required\n", "")

            with patch("process_architecture.ensure_detail_cdp_ready", side_effect=cdp_ready), \
                    patch("process_architecture.run_detail_subprocess", side_effect=collect) as collect_mock, \
                    redirect_stdout(io.StringIO()):
                result = fetch_jd(output_root, scraper_root, batch_size=2,
                                  python_path=sys.executable, wait_on_gate=False)
            collect_mock.assert_called_once()
            records = {record["batch"]: record for record in result["manifest"]["records"]}
            self.assertEqual(set(records), {1, 2})
            self.assertEqual(records[2], valid)
            self.assertEqual(records[1]["job_ids"], ["job-1", "job-2"])
            self.assertEqual(records[1]["status"], "paused")
            self.assertTrue(records[1]["security_or_login_gate"])
            self.assertEqual(records[1]["fetched_count"], 1)
            self.assertEqual(records[1]["missing_job_ids"], ["job-2"])
            self.assertEqual(records[1]["progress_completed"], 2)
            self.assertEqual(load_details(shortlist / "details.json")["job-3"], complete)
            self.assertEqual(old_checkpoint.read_bytes(), original)

    def test_empty_scope_clears_old_gate_records_without_removing_cached_data(self):
        with tempfile.TemporaryDirectory() as directory:
            output_root, scraper_root, shortlist = self._fixture(directory)
            complete = {"job_id": "job-1", "jd": "完整职责", "source": "raw/job-1.json"}
            write_json(shortlist / "details.json", [complete])
            checkpoint = shortlist / "detail_batches" / "details_0001.json"
            write_json(checkpoint, [complete])
            original = checkpoint.read_bytes()
            write_json(shortlist / "details_manifest.json", {"records": [{
                "batch": 1, "job_ids": ["job-1"], "status": "paused", "security_or_login_gate": True,
            }]})
            write_json(shortlist / "jd_input.json", {"jobs": []})
            with patch("process_architecture.ensure_detail_cdp_ready") as cdp, \
                    patch("process_architecture.run_detail_subprocess") as collect_mock, \
                    redirect_stdout(io.StringIO()):
                result = fetch_jd(output_root, scraper_root, batch_size=1,
                                  python_path=sys.executable, wait_on_gate=False)
            cdp.assert_not_called()
            collect_mock.assert_not_called()
            self.assertEqual(result["manifest"]["records"], [])
            self.assertEqual(result["manifest"]["total_jobs"], 0)
            self.assertEqual(result["manifest"]["batch_size"], 1)
            self.assertEqual(read_json(shortlist / "details_manifest.json"), result["manifest"])
            self.assertEqual(load_details(shortlist / "details.json"), {"job-1": complete})
            self.assertEqual(checkpoint.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
