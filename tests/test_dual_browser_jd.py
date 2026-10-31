"""Offline contract tests for dual-browser JD collection.

The fake CDP implementation below records navigation and connection ownership,
but never starts a browser or contacts the network.
"""

from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import ModuleType
import unittest
from unittest.mock import patch
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import jd_parallel as adapter  # noqa: E402
import checkpoint_io  # noqa: E402
import process_architecture as processor  # noqa: E402


ZERO_TIMING = adapter.RenderTiming(0, 0, 0)
FULL_JD = "职位描述\n" + "负责架构设计、系统集成和交付，提供项目实施支持。\n" * 8


def _job(number: int) -> dict:
    return {
        "job_id": f"job-{number}",
        "job_link": f"https://www.zhipin.com/job_detail/job-{number}.html",
        "title": f"岗位 {number}",
        "boss_name": "公司",
        "salary": "20-30K",
    }


class _FakeSocket:
    def settimeout(self, _timeout):
        pass


class _FakeBrowser:
    def __init__(self, snapshots=None):
        self.snapshots = snapshots or {}
        self.connections = []
        self.navigation = []

    def connect(self):
        connection = _FakeCDP(self, len(self.connections) + 1)
        self.connections.append(connection)
        return connection


class _FakeCDP:
    def __init__(self, browser, number):
        self.browser = browser
        self.number = number
        self.ws = _FakeSocket()
        self.target = f"target-{number}"
        self.session = f"session-{number}"
        self.job_id = ""
        self.closed = False

    def send(self, method, params=None, sid=None, timeout=30):
        del timeout
        params = params or {}
        if method == "Target.createTarget":
            return {"result": {"targetId": self.target}}
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": self.session}}
        if method == "Target.closeTarget":
            return {"result": {"success": True}}
        if sid != self.session:
            raise AssertionError("fake page session mismatch")
        if method == "Page.navigate":
            self.job_id = Path(urlparse(params["url"]).path).stem
            self.browser.navigation.append(self.job_id)
            return {"result": {"frameId": self.target}}
        if method == "Runtime.evaluate":
            expression = params["expression"]
            if expression.startswith("window.scrollBy"):
                return {"result": {"result": {"value": None}}}
            if expression != "EXTRACT_DETAIL":
                raise AssertionError(f"unexpected expression: {expression}")
            snapshot = deepcopy(self.browser.snapshots.get(self.job_id, {"jd": FULL_JD}))
            snapshot.setdefault("url", f"https://www.zhipin.com/job_detail/{self.job_id}.html")
            return {"result": {"result": {"value": json.dumps(snapshot, ensure_ascii=False)}}}
        return {"result": {}}

    def close(self):
        self.closed = True


class _FakeScraper(ModuleType):
    class DetailExtractionError(ValueError):
        pass

    class DetailLoginRequiredError(DetailExtractionError):
        pass

    DETAIL_DESCRIPTION_MARKER = "职位描述"
    DETAIL_LOGIN_MARKER = "登录查看完整内容"
    EXTRACT_DETAIL_JS = "EXTRACT_DETAIL"

    @staticmethod
    def require_runtime_dependencies(*_names):
        return True

    def CDPSession(self, port):
        self.requested_ports.append(port)
        return self.browsers[port].connect()

    @staticmethod
    def build_detail_url(record):
        return record.get("job_link", "")

    @staticmethod
    def create_page_session(cdp, background=True):
        target = cdp.send("Target.createTarget", {"url": "about:blank", "background": background})["result"]["targetId"]
        sid = cdp.send("Target.attachToTarget", {"targetId": target, "flatten": True})["result"]["sessionId"]
        cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid)
        cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": "fake"}, sid)
        return target, sid

    @classmethod
    def extract_detail_fields(cls, extracted, min_length=120):
        raw = str(extracted.get("jd") or "")
        body = str(extracted.get("page_text") or "")
        if cls.DETAIL_LOGIN_MARKER in raw + body:
            raise cls.DetailLoginRequiredError("login wall")
        if cls.DETAIL_DESCRIPTION_MARKER in raw:
            raw = raw.split(cls.DETAIL_DESCRIPTION_MARKER, 1)[1]
        raw = raw.strip()
        if len(raw) < min_length:
            raise cls.DetailExtractionError("short JD")
        return {"jd": raw, "boss_active_status": "今日活跃"}

    @staticmethod
    def build_detail_record(record, extracted):
        return {
            "job_id": record.get("job_id", ""),
            "job_link": record.get("job_link", ""),
            "title": record.get("title", ""),
            "jd": extracted.get("jd", ""),
        }

    @staticmethod
    def _atomic_write_json(path, records):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)


class DualBrowserJDTests(unittest.TestCase):
    def setUp(self):
        self.scraper = _FakeScraper("fake_dual_scraper")
        self.scraper.requested_ports = []
        self.scraper.browsers = {}
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output = Path(self.temp_dir.name) / "details.json"
        self.configs = {
            "chrome": adapter.BrowserConfig("chrome", 9311, Path(self.temp_dir.name) / "chrome-profile"),
            "edge": adapter.BrowserConfig("edge", 9312, Path(self.temp_dir.name) / "edge-profile"),
        }

    def run_dual(self, jobs, chrome, edge, *, use_factories=True, **kwargs):
        self.scraper.browsers = {9311: chrome, 9312: edge}
        factory_kwargs = {
            "cdp_factories": {"chrome": chrome.connect, "edge": edge.connect},
        } if use_factories else {}
        result = adapter.collect_details(
            jobs,
            self.output,
            scraper=self.scraper,
            workers=kwargs.pop("workers", 1),
            timing=ZERO_TIMING,
            browser_mode="dual",
            browser_configs=self.configs,
            emit=kwargs.pop("emit", lambda _message: None),
            **factory_kwargs,
            **kwargs,
        )
        return result

    def test_stable_balanced_contiguous_split_and_unique_merged_ids(self):
        chrome = _FakeBrowser()
        edge = _FakeBrowser()
        jobs = [_job(number) for number in range(1, 6)]

        result = self.run_dual(jobs, chrome, edge)

        self.assertEqual(chrome.navigation, ["job-1", "job-2", "job-3"])
        self.assertEqual(edge.navigation, ["job-4", "job-5"])
        ids = [record["job_id"] for record in result.records]
        self.assertEqual(ids, [f"job-{number}" for number in range(1, 6)])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(self.output.is_file())
        self.assertTrue(self.output.with_name("details.chrome.json").is_file())
        self.assertTrue(self.output.with_name("details.edge.json").is_file())

    @unittest.skipUnless(os.name == "nt", "ordinary Windows readers deny replacement")
    def test_default_merged_and_both_side_writers_recover_from_real_reader_locks(self):
        paths = [self.output, *(adapter._side_output_path(self.output, name) for name in ("chrome", "edge"))]
        readers, conflicts, releases = {}, set(), []
        guard = threading.Lock()
        replace = checkpoint_io._replace_once
        logs = []
        for path in paths:
            path.write_text("[]", encoding="utf-8")
            readers[path] = path.open("rb")

        def publish(source, destination):
            try:
                replace(source, destination)
            except PermissionError:
                with guard:
                    path = Path(destination)
                    if path not in conflicts:
                        conflicts.add(path)
                        release = threading.Timer(.1, readers[path].close)
                        releases.append(release)
                        release.start()
                raise

        chrome, edge = _FakeBrowser(), _FakeBrowser()
        try:
            with patch.object(checkpoint_io, "_replace_once", side_effect=publish), \
                 patch.object(self.scraper, "_atomic_write_json", side_effect=AssertionError("upstream writer must not be used")):
                result = self.run_dual([_job(i) for i in range(1, 5)], chrome, edge, emit=logs.append)
        finally:
            for release in releases:
                release.join(timeout=1)
            for reader in readers.values():
                reader.close()
        self.assertEqual(result.status, "completed")
        self.assertCountEqual(conflicts, paths)
        self.assertEqual(len(json.loads(self.output.read_text(encoding="utf-8"))), 4)
        for path in paths[1:]:
            self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 2)
        self.assertTrue(all(record["status"] == "success" for record in result.records))
        self.assertFalse(any("status=stopped" in line for line in logs))
        self.assertEqual(list(self.output.parent.glob(".*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "ordinary Windows readers deny replacement")
    def test_outer_stop_cancels_a_blocked_initial_merged_publish_before_browser_requests(self):
        chrome, edge, control = _FakeBrowser(), _FakeBrowser(), adapter.StopState()
        self.output.write_text("[]", encoding="utf-8")
        with self.output.open("rb"):
            cancel = threading.Timer(.15, control.request_stop)
            cancel.start()
            started = time.monotonic()
            try:
                with self.assertRaises(adapter.CheckpointWriteError):
                    self.run_dual([_job(1), _job(2)], chrome, edge, control=control)
            finally:
                cancel.join(timeout=1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(chrome.connections + edge.connections, [])
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8")), [])

    @unittest.skipUnless(os.name == "nt", "ordinary Windows readers deny replacement")
    def test_persistent_merged_reader_lock_preserves_side_successes_and_resume_does_not_refetch(self):
        chrome, edge = _FakeBrowser(), _FakeBrowser()
        jobs = [_job(i) for i in range(1, 7)]
        write = adapter._write_checkpoint
        collect_one = adapter._collect_one
        side_successes = set()
        both_sides_saved = threading.Event()
        guard = threading.Lock()
        held, previous = [], []

        def save(path, records, **kwargs):
            path = Path(path)
            with guard:
                should_lock = (path == self.output and not held
                               and {"job-1", "job-4"}.issubset(record["job_id"] for record in records))
            if should_lock:
                self.assertTrue(both_sides_saved.wait(2), "both side checkpoints did not become durable")
                previous.append(self.output.read_bytes())
                held.append(self.output.open("rb"))
            write(path, records, **kwargs)
            if path != self.output and any(record.get("status") == "success" for record in records):
                with guard:
                    side_successes.add(path)
                    if len(side_successes) == 2:
                        both_sides_saved.set()

        def capture(job, scraper, factory, control, *args, **kwargs):
            if job["job_id"] in {"job-2", "job-5"}:
                try:
                    control.wait(30)
                except adapter._Cancelled:
                    return None
            return collect_one(job, scraper, factory, control, *args, **kwargs)

        started = time.monotonic()
        try:
            with patch.object(adapter, "_write_checkpoint", side_effect=save), \
                 patch.object(adapter, "_collect_one", side_effect=capture), \
                 self.assertRaises(adapter.CheckpointWriteError) as raised:
                self.run_dual(jobs, chrome, edge)
            self.assertEqual(raised.exception.operation, "replace")
            self.assertEqual(raised.exception.checkpoint_path, self.output)
            self.assertLess(time.monotonic() - started, 7)
            self.assertEqual(len(held), 1)
            self.assertEqual(self.output.read_bytes(), previous[0])
        finally:
            for reader in held:
                reader.close()

        self.assertEqual(chrome.navigation, ["job-1"])
        self.assertEqual(edge.navigation, ["job-4"])
        for name, identity in (("chrome", "job-1"), ("edge", "job-4")):
            records = json.loads(adapter._side_output_path(self.output, name).read_text(encoding="utf-8"))
            self.assertEqual([(record["job_id"], record["status"]) for record in records], [(identity, "success")])
        result = self.run_dual(jobs, chrome, edge)
        self.assertEqual(result.status, "completed")
        self.assertEqual(chrome.navigation, ["job-1", "job-2", "job-3"])
        self.assertEqual(edge.navigation, ["job-4", "job-5", "job-6"])
        self.assertEqual([record["job_id"] for record in result.records], [f"job-{i}" for i in range(1, 7)])
        self.assertTrue(all(record["status"] == "success" for record in result.records))
        self.assertTrue(all(connection.closed for connection in chrome.connections + edge.connections))
        self.assertEqual(list(self.output.parent.glob(".*.tmp")), [])

    def test_merge_keeps_job_id_unique_when_same_input_id_reaches_both_records(self):
        chrome = _FakeBrowser()
        edge = _FakeBrowser()

        result = self.run_dual([_job(1), _job(1), _job(2)], chrome, edge)

        ids = [record["job_id"] for record in result.records]
        self.assertEqual(ids, ["job-1", "job-2"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_each_browser_keeps_distinct_port_and_profile_metadata(self):
        chrome = _FakeBrowser()
        edge = _FakeBrowser()

        result = self.run_dual([_job(1), _job(2)], chrome, edge, use_factories=False)

        self.assertCountEqual(self.scraper.requested_ports, [9311, 9312])
        chrome_result = result.browser_results["chrome"]
        edge_result = result.browser_results["edge"]
        self.assertEqual(chrome_result["port"], 9311)
        self.assertEqual(edge_result["port"], 9312)
        self.assertEqual(chrome_result["profile_dir"], str(self.configs["chrome"].profile_dir))
        self.assertEqual(edge_result["profile_dir"], str(self.configs["edge"].profile_dir))
        self.assertIsNot(chrome_result, edge_result)

    def test_one_pending_job_only_opens_chrome_while_dual_mode_is_retained(self):
        chrome = _FakeBrowser()
        edge = _FakeBrowser()
        result = self.run_dual([_job(1)], chrome, edge)
        self.assertEqual(result.browser, "dual")
        self.assertEqual(chrome.navigation, ["job-1"])
        self.assertEqual(edge.navigation, [])
        self.assertEqual(edge.connections, [])
        self.assertEqual(result.status, "completed")

    def test_switch_from_single_checkpoint_to_dual_preserves_complete_jobs(self):
        jobs = [_job(number) for number in range(1, 5)]
        checkpoint = {**_job(1), "jd": FULL_JD, "status": "success", "jd_complete": True}
        self.scraper._atomic_write_json(self.output, [checkpoint])
        chrome = _FakeBrowser()
        edge = _FakeBrowser()
        result = self.run_dual(jobs, chrome, edge)
        self.assertEqual(chrome.navigation, ["job-2"])
        self.assertEqual(edge.navigation, ["job-3", "job-4"])
        self.assertEqual(len(result.records), 4)
        self.assertEqual(len({record["job_id"] for record in result.records}), 4)
        self.assertEqual(result.status, "completed")

    def test_pause_on_one_side_preserves_other_side_results(self):
        chrome = _FakeBrowser({"job-1": {"jd": FULL_JD}, "job-2": {"url": "https://www.zhipin.com/security-check"}})
        edge = _FakeBrowser({"job-3": {"jd": FULL_JD}, "job-4": {"jd": FULL_JD}})

        result = self.run_dual([_job(1), _job(2), _job(3), _job(4)], chrome, edge)

        self.assertEqual(result.status, "paused")
        self.assertEqual([record["job_id"] for record in result.records], ["job-1", "job-2", "job-3", "job-4"])
        self.assertEqual(result.browser_results["chrome"]["status"], "paused")
        self.assertEqual(result.browser_results["edge"]["status"], "completed")
        self.assertEqual([record["status"] for record in result.records], ["success", "paused", "success", "success"])

    def test_same_extra_wait_range_reaches_both_sides_and_one_gate_keeps_other_wait_alive(self):
        chrome = _FakeBrowser({"job-1": {"url": "https://www.zhipin.com/security-check"}})
        edge = _FakeBrowser()
        edge_waiting, chrome_gated = threading.Event(), threading.Event()
        waits = []
        collect_single = adapter._collect_details_single
        request_stop = adapter.StopState.request_stop

        def side(*args, **kwargs):
            kwargs["control"].browser_name = kwargs["browser"]
            return collect_single(*args, **kwargs)

        def extra_wait(control, seconds):
            waits.append((control.browser_name, seconds))
            if control.browser_name == "edge":
                edge_waiting.set()
                self.assertTrue(chrome_gated.wait(2), "Chrome did not report its gate")
                self.assertFalse(control.event.is_set(), "Chrome gate cancelled Edge's wait")
            else:
                self.assertTrue(edge_waiting.wait(2), "Edge did not enter its extra wait")
            control.check()

        def stop(control, reason="stopped", *, gate_message=""):
            request_stop(control, reason, gate_message=gate_message)
            if gate_message:
                chrome_gated.set()

        with patch.object(adapter, "_collect_details_single", side_effect=side), \
             patch.object(adapter.StopState, "wait", new=extra_wait), \
             patch.object(adapter.StopState, "request_stop", new=stop), \
             patch.object(adapter.random, "uniform", return_value=6) as draw:
            result = self.run_dual([_job(1), _job(2), _job(3), _job(4)], chrome, edge,
                                   page_wait_min=4, page_wait_max=8)
        self.assertEqual(result.status, "paused")
        self.assertEqual(chrome.navigation, ["job-1"])
        self.assertEqual(edge.navigation, ["job-3", "job-4"])
        self.assertCountEqual(waits, [("chrome", 6), ("edge", 6), ("edge", 6)])
        self.assertEqual(draw.call_args_list, [unittest.mock.call(4, 8)] * 3)
        self.assertEqual((result.page_wait_min, result.page_wait_max), (4, 8))
        for payload in result.browser_results.values():
            self.assertEqual((payload["page_wait_min"], payload["page_wait_max"]), (4, 8))
        self.assertEqual(result.browser_results["chrome"]["status"], "paused")
        self.assertEqual(result.browser_results["edge"]["status"], "completed")

    def test_outer_stop_interrupts_both_extra_waits_before_any_browser_request(self):
        chrome, edge, control = _FakeBrowser(), _FakeBrowser(), adapter.StopState()
        entered = threading.Barrier(3)
        wait = adapter.StopState.wait
        results, failures = [], []

        def extra_wait(side_control, seconds):
            entered.wait(timeout=2)
            wait(side_control, seconds)

        def run():
            try:
                results.append(self.run_dual([_job(1), _job(2)], chrome, edge, control=control,
                                             page_wait_min=30, page_wait_max=30))
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=run)
        with patch.object(adapter.StopState, "wait", new=extra_wait), \
             patch.object(adapter.random, "uniform", return_value=30):
            worker.start()
            try:
                entered.wait(timeout=2)
            finally:
                control.request_stop()
                worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "stopped")
        self.assertEqual(results[0].records, [])
        self.assertEqual(chrome.connections + edge.connections, [])
        self.assertEqual(chrome.navigation + edge.navigation, [])

    def test_completed_side_does_not_wait_or_request_again_on_resume(self):
        chrome, edge = _FakeBrowser(), _FakeBrowser()
        jobs = [_job(1), _job(2)]
        first = self.run_dual(jobs, chrome, edge)
        with patch.object(adapter.random, "uniform", side_effect=AssertionError("completed jobs must not draw")), \
             patch.object(adapter.StopState, "wait", side_effect=AssertionError("completed jobs must not wait")):
            second = self.run_dual(jobs, chrome, edge, page_wait_min=30, page_wait_max=30)
        self.assertEqual(second.records, first.records)
        self.assertEqual(chrome.navigation, ["job-1"])
        self.assertEqual(edge.navigation, ["job-2"])

    def test_resume_skips_completed_records_and_retries_paused_side(self):
        chrome = _FakeBrowser({"job-1": {"jd": FULL_JD}, "job-2": {"url": "https://www.zhipin.com/security-check"}})
        edge = _FakeBrowser({"job-3": {"jd": FULL_JD}, "job-4": {"jd": FULL_JD}})
        jobs = [_job(1), _job(2), _job(3), _job(4)]

        first = self.run_dual(jobs, chrome, edge)
        self.assertEqual(first.status, "paused")
        chrome_connections = len(chrome.connections)
        edge_connections = len(edge.connections)
        chrome.snapshots["job-2"] = {"jd": FULL_JD}

        second = self.run_dual(jobs, chrome, edge, resume=True)

        self.assertEqual(second.status, "completed")
        self.assertEqual(len(chrome.connections), chrome_connections + 1)
        self.assertEqual(len(edge.connections), edge_connections)
        self.assertEqual([record["job_id"] for record in second.records], [f"job-{number}" for number in range(1, 5)])
        self.assertTrue(all(record["status"] == "success" for record in second.records))


class FetchJDBrowserReadinessTests(unittest.TestCase):
    """Exercise the launcher and real adapter using only fake CDP/subprocesses."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.shortlist = self.root / "prepared/jd_shortlists"
        self.scraper_root = self.root / "scraper"
        entry = self.scraper_root / "scripts/boss_cdp_raw.py"
        entry.parent.mkdir(parents=True)
        entry.write_text("# synthetic; never executed\n", encoding="utf-8")
        self.chrome, self.edge = _FakeBrowser(), _FakeBrowser()
        self.scraper = _FakeScraper("fake_fetch_jd_scraper")
        self.scraper.requested_ports = []
        self.scraper.browsers = {9311: self.chrome, 9312: self.edge}
        self.readiness = {"chrome": True, "edge": True}
        self.probes, self.batch_inputs, self.child_results = [], [], []

    def prepare(self, jobs, complete=(), *, checkpoint="details.json"):
        processor.write_json(self.shortlist / "jd_input.json", {"jobs": jobs})
        if complete:
            processor.write_json(self.shortlist / checkpoint, [
                {**row, "jd": FULL_JD, "status": "success", "jd_complete": True}
                for row in jobs if row["job_id"] in set(complete)
            ])

    def check_ready(self, _entry, _python, port, *, browser, profile_dir):
        self.assertEqual(port, {"chrome": 9311, "edge": 9312}[browser])
        self.assertEqual(Path(profile_dir), self.root / f"{browser}-profile")
        self.probes.append(browser)
        available = self.readiness[browser]
        ready = available.pop(0) if isinstance(available, list) else available
        return ready, None if ready else f"synthetic {browser} unavailable"

    def run_child(self, command, **_kwargs):
        def argument(flag):
            return command[command.index(flag) + 1]

        payload = processor.read_json(Path(argument("--input")))
        self.batch_inputs.append([row["job_id"] for row in payload["jobs"]])
        mode = argument("--browser-mode")
        readiness = (processor.read_json(Path(argument("--browser-readiness-file")))
                     if mode == "dual" else None)
        logs = []
        result = adapter.collect_details(
            payload, Path(argument("--detail-output")), scraper=self.scraper,
            workers=1, timing=ZERO_TIMING, browser_mode=mode,
            browser_configs={
                name: adapter.BrowserConfig(name, int(argument(f"--{name}-port")),
                                            Path(argument(f"--{name}-profile-dir")))
                for name in ("chrome", "edge")
            },
            browser_readiness=readiness,
            cdp_factory=self.chrome.connect if mode == "single" else None,
            cdp_factories={"chrome": self.chrome.connect, "edge": self.edge.connect},
            emit=logs.append,
        )
        self.child_results.append(result)
        return subprocess.CompletedProcess(command, 2 if result.status == "paused" else 0,
                                           "\n".join(logs), "")

    def fetch(self, *, batch_size=10, browser_mode="dual", retry=None):
        with patch.object(processor, "ensure_detail_cdp_ready", side_effect=self.check_ready), \
             patch.object(processor, "run_detail_subprocess", side_effect=self.run_child), \
             patch.object(processor, "wait_for_detail_gate_retry", side_effect=retry or [False]), \
             patch.object(processor, "local_cdp_ready", side_effect=AssertionError("real CDP is forbidden")), \
             patch.object(processor.subprocess, "Popen", side_effect=AssertionError("real child is forbidden")), \
             redirect_stdout(io.StringIO()):
            return processor.fetch_jd(
                self.root, self.scraper_root, batch_size=batch_size, python_path=sys.executable,
                browser_mode=browser_mode, chrome_port=9311, edge_port=9312,
                chrome_profile_dir=self.root / "chrome-profile", edge_profile_dir=self.root / "edge-profile",
                wait_on_gate=False,
            )

    def assert_complete(self, result, ids):
        self.assertTrue(all(record["status"] == "success" for record in result["manifest"]["records"]))
        details = processor.read_json(self.shortlist / "details.json")
        self.assertEqual({row["job_id"] for row in details}, set(ids))
        self.assertTrue(all(adapter.detail_completeness(row) == "complete" for row in details))

    def test_last_pending_edge_job_is_prechecked_and_resumes_its_original_shard(self):
        jobs = [_job(1), _job(2)]
        self.prepare(jobs, ["job-1"])

        result = self.fetch()

        self.assertEqual(self.probes, ["edge"])
        self.assertEqual(self.batch_inputs, [["job-1", "job-2"]])
        self.assertEqual(self.chrome.navigation, [])
        self.assertEqual(self.edge.navigation, ["job-2"])
        self.assertTrue(result["manifest"]["browser_readiness"]["edge"]["ready"])
        self.assertEqual([row["job_id"] for row in self.child_results[0].records], ["job-1", "job-2"])
        self.assert_complete(result, ["job-1", "job-2"])

    def test_last_pending_chrome_job_does_not_require_completed_edge_login(self):
        self.prepare([_job(1), _job(2)], ["job-2"])
        self.readiness["edge"] = False

        result = self.fetch()

        self.assertEqual(self.probes, ["chrome"])
        self.assertEqual(self.batch_inputs, [["job-1", "job-2"]])
        self.assertEqual(self.chrome.navigation, ["job-1"])
        self.assertEqual(self.edge.connections, [])
        self.assert_complete(result, ["job-1", "job-2"])

    def test_failed_edge_precheck_is_recovered_without_rechecking_or_refetching_chrome(self):
        self.prepare([_job(1), _job(2)])
        self.readiness["edge"] = False
        first = self.fetch()
        self.assertEqual(self.probes, ["chrome", "edge"])
        self.assertEqual(first["manifest"]["records"][0]["status"], "paused")
        self.assertEqual(self.chrome.navigation, ["job-1"])
        self.assertEqual(self.edge.navigation, [])

        self.probes.clear()
        self.readiness.update(chrome=False, edge=True)
        second = self.fetch()

        self.assertEqual(self.probes, ["edge"])
        self.assertEqual(self.batch_inputs, [["job-1", "job-2"], ["job-1", "job-2"]])
        self.assertEqual(self.chrome.navigation, ["job-1"])
        self.assertEqual(self.edge.navigation, ["job-2"])
        self.assert_complete(second, ["job-1", "job-2"])
        self.probes.clear()
        self.fetch()
        self.assertEqual(self.probes, [])
        self.assertEqual(len(self.batch_inputs), 2)

    def test_manual_retry_rechecks_only_the_still_pending_unready_browser(self):
        self.prepare([_job(1), _job(2)])
        self.readiness["edge"] = [False, True]

        result = self.fetch(retry=[True])

        self.assertEqual(self.probes, ["chrome", "edge", "edge"])
        self.assertEqual(self.chrome.navigation, ["job-1"])
        self.assertEqual(self.edge.navigation, ["job-2"])
        self.assert_complete(result, ["job-1", "job-2"])

    def test_precheck_uses_each_full_batch_partition_after_recovering_old_sidecars(self):
        jobs = [_job(number) for number in (9, 2, 8, 1, 7)]
        self.prepare(jobs, ["job-9", "job-2", "job-1", "job-7"],
                     checkpoint="detail_batches/details_0099.edge.json")
        self.readiness["chrome"] = False

        result = self.fetch(batch_size=3)

        self.assertEqual(self.probes, ["edge"])
        self.assertEqual(self.batch_inputs, [["job-9", "job-2", "job-8"]])
        self.assertEqual(self.chrome.connections, [])
        self.assertEqual(self.edge.navigation, ["job-8"])
        self.assert_complete(result, [row["job_id"] for row in jobs])

    def test_both_pending_sides_are_prechecked_and_keep_input_merge_order(self):
        jobs = [_job(number) for number in (9, 2, 8, 1, 7)]
        self.prepare(jobs)

        result = self.fetch(batch_size=3)

        self.assertEqual(self.probes, ["chrome", "edge"])
        self.assertEqual(self.batch_inputs, [["job-9", "job-2", "job-8"], ["job-1", "job-7"]])
        self.assertEqual(self.chrome.navigation, ["job-9", "job-2", "job-1"])
        self.assertEqual(self.edge.navigation, ["job-8", "job-7"])
        self.assertEqual([[row["job_id"] for row in child.records] for child in self.child_results], self.batch_inputs)
        self.assert_complete(result, [row["job_id"] for row in jobs])

    def test_empty_input_does_not_probe_or_start_an_adapter(self):
        self.prepare([])
        result = self.fetch()
        self.assertEqual(self.probes, [])
        self.assertEqual(self.batch_inputs, [])
        self.assertEqual(result["manifest"]["records"], [])

    def test_single_job_dual_batch_does_not_probe_the_empty_edge_side(self):
        self.prepare([_job(1)])
        self.readiness["edge"] = False
        result = self.fetch()
        self.assertEqual(self.probes, ["chrome"])
        self.assertEqual(self.edge.connections, [])
        self.assert_complete(result, ["job-1"])

    def test_complete_both_sides_do_not_require_browsers_or_start_an_adapter(self):
        self.prepare([_job(1), _job(2)], ["job-1", "job-2"])
        self.readiness.update(chrome=False, edge=False)
        result = self.fetch()
        self.assertEqual(self.probes, [])
        self.assertEqual(self.batch_inputs, [])
        self.assert_complete(result, ["job-1", "job-2"])

    def test_only_pending_unready_side_cannot_use_a_cached_side_as_a_ready_browser(self):
        for pending in ("chrome", "edge"):
            with self.subTest(pending=pending):
                complete, missing = ("job-2", "job-1") if pending == "chrome" else ("job-1", "job-2")
                self.prepare([_job(1), _job(2)], [complete])
                self.probes.clear()
                self.readiness.update(chrome=False, edge=False)
                result = self.fetch()
                self.assertEqual(self.probes, [pending])
                self.assertEqual(self.batch_inputs, [])
                self.assertEqual(result["manifest"]["records"][0]["status"], "paused")
                self.assertEqual(result["manifest"]["records"][0]["missing_job_ids"], [missing])

    def test_single_browser_mode_still_prechecks_chrome_for_any_pending_job(self):
        self.prepare([_job(1), _job(2)], ["job-1"])
        result = self.fetch(browser_mode="single")
        self.assertEqual(self.probes, ["chrome"])
        self.assertEqual(self.batch_inputs, [["job-2"]])
        self.assertEqual(self.chrome.navigation, ["job-2"])
        self.assertEqual(self.edge.connections, [])
        self.assert_complete(result, ["job-1", "job-2"])


if __name__ == "__main__":
    unittest.main()
