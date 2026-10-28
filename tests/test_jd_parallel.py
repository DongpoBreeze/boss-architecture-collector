"""Offline CDP protocol tests; no Chrome, website, or model is contacted."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import re
import signal
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
from process_architecture import _DETAIL_GATE_RE, detail_completeness  # noqa: E402


ZERO_TIMING = adapter.RenderTiming(0, 0, 0)
FULL_JD = "职位描述\n" + "负责面向客户的业务方案、系统集成和交付，提供项目实施和运行支持。\n" * 8


def job(number):
    return {"job_id": f"job-{number}", "job_link": f"https://www.zhipin.com/job_detail/job-{number}.html",
            "title": f"岗位 {number}", "boss_name": "公司", "salary": "20-30K", "list_pass": False}


class FakeSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)


class FakeBrowser:
    def __init__(self, *, simultaneous=0, snapshots=None, hook=None, attach_failure=False):
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(simultaneous) if simultaneous else None
        self.snapshots = snapshots or {}
        self.hook = hook
        self.attach_failure = attach_failure
        self.connections = []
        self.active = {"user-original-tab"}
        self.owned_targets = []
        self.closed_targets = []
        self.navigation_order = []
        self.peak = 0
        self.commands = []

    def connect(self):
        with self.lock:
            connection = FakeCDP(self, len(self.connections) + 1)
            self.connections.append(connection)
            return connection


class FakeCDP:
    def __init__(self, browser, number):
        self.browser = browser
        self.number = number
        self.ws = FakeSocket()
        self.thread_id = None
        self.target = f"owned-{number}"
        self.session = f"session-{number}"
        self.job_id = None
        self.scrolls = 0
        self.reads = 0
        self.closed = False

    def send(self, method, params=None, sid=None, timeout=30):
        current_thread = threading.get_ident()
        if self.thread_id is None:
            self.thread_id = current_thread
        if self.thread_id != current_thread:
            raise AssertionError("A CDP WebSocket was shared across threads")
        params = params or {}
        browser = self.browser
        with browser.lock:
            browser.commands.append((self.number, method, deepcopy(params), sid, timeout))
        if method == "Target.createTarget":
            if params != {"url": "about:blank", "background": True}:
                raise AssertionError("Only background pages may be created")
            with browser.lock:
                browser.active.add(self.target)
                browser.owned_targets.append(self.target)
                browser.peak = max(browser.peak, len(browser.active) - 1)
            return {"result": {"targetId": self.target}}
        if method == "Target.attachToTarget":
            if browser.attach_failure and self.number == 1:
                raise TimeoutError("attach failed after target creation")
            if params != {"targetId": self.target, "flatten": True}:
                raise AssertionError("Wrong target attached")
            return {"result": {"sessionId": self.session}}
        if method == "Target.closeTarget":
            if params.get("targetId") != self.target:
                raise AssertionError("Attempted to close another tab")
            with browser.lock:
                browser.closed_targets.append(self.target)
                browser.active.discard(self.target)
            return {"result": {"success": True}}
        if sid != self.session:
            raise AssertionError("Wrong page session")
        if method == "Page.navigate":
            self.job_id = Path(urlparse(params["url"]).path).stem
            with browser.lock:
                browser.navigation_order.append(self.job_id)
                should_wait = browser.barrier and len(browser.navigation_order) <= browser.barrier.parties
            if should_wait:
                browser.barrier.wait(timeout=3)
            if self.job_id == "navigation-fails":
                return {"result": {"errorText": "net::ERR_FAILED"}}
            return {"result": {"frameId": self.target}}
        if method == "Runtime.evaluate":
            expression = params["expression"]
            if expression.startswith("window.scrollBy"):
                self.scrolls += 1
                return {"result": {"result": {"value": None}}}
            if expression != "EXTRACT_DETAIL":
                raise AssertionError("Unexpected JS; passive extraction and scrolling only")
            self.reads += 1
            snapshot = None
            if browser.hook:
                snapshot = browser.hook(self, self.reads)
            if snapshot is None:
                snapshot = deepcopy(browser.snapshots.get(self.job_id, {"jd": FULL_JD, "page_text": "", "tags": ["集成"]}))
            snapshot.setdefault("url", f"https://www.zhipin.com/job_detail/{self.job_id}.html")
            return {"result": {"result": {"value": json.dumps(snapshot, ensure_ascii=False)}}}
        if method not in ("Emulation.setFocusEmulationEnabled", "Page.addScriptToEvaluateOnNewDocument"):
            raise AssertionError(f"Unexpected CDP command: {method}")
        return {"result": {}}

    def close(self):
        self.closed = True


class FakeScraper(ModuleType):
    class DetailExtractionError(ValueError):
        pass

    class DetailLoginRequiredError(DetailExtractionError):
        pass

    DETAIL_DESCRIPTION_MARKER = "职位描述"
    DETAIL_LOGIN_MARKER = "登录查看完整内容"
    EXTRACT_DETAIL_JS = "EXTRACT_DETAIL"

    def __init__(self, browser):
        super().__init__("fake_upstream")
        self.browser = browser
        self.dependency_checks = []
        self.ports = []

    def require_runtime_dependencies(self, *names):
        self.dependency_checks.append(names)
        return True

    def CDPSession(self, port):
        self.ports.append(port)
        return self.browser.connect()

    @staticmethod
    def build_detail_url(record):
        return record.get("job_link", "")

    @staticmethod
    def create_page_session(cdp, background=True):
        target = cdp.send("Target.createTarget", {"url": "about:blank", "background": background})["result"]["targetId"]
        sid = cdp.send("Target.attachToTarget", {"targetId": target, "flatten": True})["result"]["sessionId"]
        cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid)
        cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": "existing visibility helper"}, sid)
        return target, sid

    def extract_detail_fields(self, extracted, min_length=120):
        raw = str(extracted.get("jd") or "")
        body = str(extracted.get("page_text") or "")
        if self.DETAIL_LOGIN_MARKER in raw + body:
            raise self.DetailLoginRequiredError("upstream login wall")
        if not raw and self.DETAIL_DESCRIPTION_MARKER in body:
            raw = body
        if self.DETAIL_DESCRIPTION_MARKER in raw:
            raw = raw.split(self.DETAIL_DESCRIPTION_MARKER, 1)[1]
        raw = raw.strip()
        if len(raw) < min_length:
            raise self.DetailExtractionError("unusable or short JD")
        return {"jd": raw, "boss_active_status": "今日活跃"}

    @staticmethod
    def build_detail_record(record, extracted):
        return {"job_id": record.get("job_id", ""), "job_link": record.get("job_link", ""),
                "link": record.get("job_link", ""), "company": record.get("boss_name", ""),
                "title": record.get("title", ""), "salary": record.get("salary", ""),
                "skill_tags": extracted.get("tags", []), "boss_active_status": extracted.get("boss_active_status", ""),
                "jd": extracted.get("jd", "")}

    @staticmethod
    def _atomic_write_json(path, records):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)


class ParallelJDTests(unittest.TestCase):
    def run_batch(self, browser=None, jobs=None, *, workers=3, control=None, timing=ZERO_TIMING, persist=None, emit=None):
        browser = browser or FakeBrowser()
        scraper = FakeScraper(browser)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output = Path(directory.name) / "details.json"
        writes = []
        logs = []
        main_thread = threading.get_ident()

        def writer(path, records):
            self.assertEqual(threading.get_ident(), main_thread)
            scraper._atomic_write_json(path, records)
            writes.append(deepcopy(records))
            if persist:
                persist(path, records)

        def logger(message):
            self.assertEqual(threading.get_ident(), main_thread)
            logs.append(message)
            if emit:
                emit(message)

        inputs = [job(i) for i in range(1, 7)] if jobs is None else jobs
        original = deepcopy(inputs)
        result = adapter.collect_details(
            {"jobs": inputs}, output, scraper=scraper, workers=workers, control=control,
            cdp_factory=browser.connect, timing=timing, persist=writer, emit=logger,
        )
        self.assertEqual(inputs, original)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result.records)
        return result, browser, scraper, output, writes, logs

    def assert_cleaned(self, browser):
        self.assertEqual(browser.active, {"user-original-tab"})
        self.assertCountEqual(browser.closed_targets, browser.owned_targets)
        self.assertTrue(all(connection.closed for connection in browser.connections))

    def test_real_parallel_pages_stay_within_limit_use_unique_connections_and_scroll_three_times(self):
        browser = FakeBrowser(simultaneous=3)
        result, browser, _, _, writes, logs = self.run_batch(browser)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(browser.peak, 3)
        self.assertEqual(len(browser.connections), 6)
        self.assertEqual(len(set(browser.owned_targets)), 6)
        self.assertGreater(len({connection.thread_id for connection in browser.connections}), 1)
        self.assertTrue(all(connection.scrolls == 3 for connection in browser.connections))
        self.assertEqual([len(records) for records in writes], list(range(7)))
        self.assertEqual([record["job_id"] for record in result.records], [f"job-{i}" for i in range(1, 7)])
        self.assertEqual(sum(bool(re.search(r"JD:\s*\d+字", line)) for line in logs), 6)
        self.assertTrue(all(detail_completeness(record) == "complete" for record in result.records))
        for connection in browser.connections:
            self.assertEqual(connection.ws.timeouts, [3.0])
            commands = [method for number, method, *_ in browser.commands if number == connection.number]
            self.assertLess(commands.index("Emulation.setFocusEmulationEnabled"), commands.index("Page.navigate"))
            self.assertLess(commands.index("Page.addScriptToEvaluateOnNewDocument"), commands.index("Page.navigate"))
        self.assert_cleaned(browser)

    def test_worker_one_is_serial_and_all_manually_restored_jobs_are_still_fetched(self):
        result, browser, _, _, _, _ = self.run_batch(workers=1)
        self.assertEqual(browser.peak, 1)
        self.assertEqual(len(result.records), 6)  # Every input has list_pass=False.
        self.assert_cleaned(browser)

    def test_security_job_responsibilities_are_not_confused_with_a_gate(self):
        text = FULL_JD + "\n安全验证与身份认证方案设计\n访问异常处理、验证码产品接入"
        result, browser, _, _, _, _ = self.run_batch(
            FakeBrowser(snapshots={"job-1": {"jd": text, "page_text": text}}), jobs=[job(1)],
        )
        self.assertEqual(result.status, "completed")
        self.assertIn("验证码产品接入", result.records[0]["jd"])
        self.assert_cleaned(browser)

    def test_each_completion_is_persisted_before_its_progress_line(self):
        observed = []

        def on_log(message):
            if "JD:" in message:
                observed.append(len(last_write))

        last_write = []

        def on_persist(_path, records):
            last_write[:] = records

        result, _, _, _, _, _ = self.run_batch(persist=on_persist, emit=on_log)
        self.assertEqual(observed, list(range(1, 7)))
        self.assertEqual(last_write, result.records)

    def test_gate_stops_new_scheduling_retains_completed_details_and_closes_own_pages(self):
        control = adapter.StopState()
        first_saved = threading.Event()

        def snapshot(connection, _read):
            if connection.job_id == "job-2":
                self.assertTrue(first_saved.wait(2))
                return {"jd": "", "page_text": "安全验证\n请完成验证", "url": "https://www.zhipin.com/web/common/security-check.html?_security_check=1"}
            return None

        def persisted(_path, records):
            if any(record["status"] == "success" for record in records):
                first_saved.set()
                self.assertTrue(control.event.wait(2))

        browser = FakeBrowser(simultaneous=2, hook=snapshot)
        result, browser, _, _, writes, logs = self.run_batch(browser, workers=2, control=control, persist=persisted)
        self.assertEqual(result.status, "paused")
        self.assertEqual(result.exit_code, 2)
        self.assertCountEqual(browser.navigation_order, ["job-1", "job-2"])
        self.assertEqual([record["status"] for record in result.records], ["success", "paused"])
        self.assertEqual(detail_completeness(result.records[0]), "complete")
        self.assertEqual(result.records[1]["jd"], "")
        self.assertTrue(result.records[1]["security_or_login_gate"])
        self.assertTrue(any(_DETAIL_GATE_RE.search(line) for line in logs))
        self.assertTrue(any(len(records) == 1 and records[0]["status"] == "success" for records in writes))
        self.assert_cleaned(browser)

    def test_running_pages_leave_long_render_wait_promptly_after_another_page_hits_a_gate(self):
        read_barrier = threading.Barrier(3)

        def snapshot(connection, read):
            if read == 1:
                read_barrier.wait(timeout=2)
                if connection.job_id == "job-1":
                    return {"jd": "", "page_text": "登录查看完整内容"}
            return None

        started = time.monotonic()
        result, browser, _, _, _, _ = self.run_batch(
            FakeBrowser(simultaneous=3, hook=snapshot), timing=adapter.RenderTiming(10, 10, 10),
        )
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result.status, "paused")
        self.assertEqual(len(browser.connections), 3)
        self.assertTrue(all(connection.scrolls == 0 for connection in browser.connections))
        self.assertTrue(any(detail_completeness(record) == "partial" for record in result.records))
        self.assert_cleaned(browser)

    def test_login_gate_and_security_url_gate_share_existing_processor_detection(self):
        cases = [
            {"jd": "", "page_text": "登录查看完整内容"},
            {"jd": "", "page_text": "验证码\n请拖动滑块"},
            {"jd": "", "page_text": "安全校验\n当前环境存在异常"},
            {"jd": "", "page_text": "", "url": "https://www.zhipin.com/web/user/?ka=header-login"},
            {"jd": "", "page_text": "", "url": "https://www.zhipin.com/_security_check"},
            {"jd": "", "page_text": "", "url": "https://www.zhipin.com/web/common/security-check.html"},
            {"jd": "", "page_text": "", "url": "https://www.zhipin.com/web/common/check?_security_check=1"},
            {"jd": "", "page_text": "", "url": "https://passport.zhipin.com/login"},
        ]
        for snapshot in cases:
            with self.subTest(snapshot=snapshot):
                result, browser, _, _, _, logs = self.run_batch(
                    FakeBrowser(snapshots={"job-1": snapshot}), workers=1,
                )
                self.assertEqual(result.status, "paused")
                self.assertEqual(len(browser.connections), 1)
                self.assertTrue(any(_DETAIL_GATE_RE.search(line) for line in logs))
                self.assert_cleaned(browser)

    def test_gate_after_text_was_rendered_keeps_only_prior_jd_as_explicitly_partial(self):
        def snapshot(_connection, read):
            if read >= 2:
                return {"jd": "", "page_text": "登录查看完整内容"}
            return None

        result, browser, _, _, _, _ = self.run_batch(FakeBrowser(hook=snapshot), workers=1)
        self.assertEqual(result.status, "paused")
        self.assertEqual(len(result.records), 1)
        record = result.records[0]
        self.assertEqual(detail_completeness(record), "partial")
        self.assertFalse(record["jd_complete"])
        self.assertEqual(record["jd"], FULL_JD.split("职位描述", 1)[1].strip())
        self.assertNotIn("登录", record["jd"])
        self.assertEqual(browser.connections[0].scrolls, 1)
        self.assert_cleaned(browser)

    def test_partial_and_missing_pages_and_single_errors_do_not_stop_other_jobs(self):
        def snapshot(connection, read):
            if connection.job_id == "job-3" and read >= 2:
                raise TimeoutError("one page disconnected")
            if connection.job_id == "job-6":
                return {"jd": "", "page_text": "首页\n公司\n职位\n无障碍专区"}
            return None

        inputs = [job(i) for i in range(1, 7)]
        inputs[3]["job_link"] = ""
        inputs[4]["job_link"] = "https://www.zhipin.com/job_detail/navigation-fails.html"
        browser = FakeBrowser(snapshots={"job-2": {"jd": "职位描述\n真实的短职责", "page_text": ""}}, hook=snapshot)
        result, browser, _, _, writes, _ = self.run_batch(browser, inputs)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(result.records), len(inputs))
        self.assertEqual([detail_completeness(record) for record in result.records],
                         ["complete", "partial", "partial", "missing", "missing", "missing"])
        self.assertEqual(result.records[1]["jd"], "真实的短职责")
        self.assertEqual([record["status"] for record in result.records],
                         ["success", "partial", "failed", "failed", "failed", "failed"])
        self.assertEqual(len(writes), 7)
        self.assertEqual(result.records[-1]["jd"], "")
        self.assert_cleaned(browser)

    def test_third_party_errors_do_not_persist_or_emit_credentials(self):
        secrets = ("fixture-token", "fixture-cookie", "fixture-user", "fixture-password", "fixture-query-key")
        message = (
            "token=fixture-token; Cookie: sid=fixture-cookie\n"
            "https://fixture-user:fixture-password@provider.test/path?api_key=fixture-query-key"
        )
        for error_type in (RuntimeError, FakeScraper.DetailExtractionError):
            for fail_on_read in (1, 2):
                with self.subTest(error_type=error_type.__name__, fail_on_read=fail_on_read):
                    def snapshot(_connection, read):
                        if read >= fail_on_read:
                            raise error_type(message)
                        return None

                    result, browser, _, output, writes, logs = self.run_batch(
                        FakeBrowser(hook=snapshot), jobs=[job(1)], workers=1,
                    )
                    record = result.records[0]
                    self.assertEqual(result.status, "partial")
                    expected = "partial" if error_type is FakeScraper.DetailExtractionError and fail_on_read == 2 else "failed"
                    self.assertEqual(record["status"], expected)
                    self.assertEqual(detail_completeness(record), "partial" if fail_on_read == 2 else "missing")
                    self.assertFalse(record["jd_complete"])
                    self.assertIn(error_type.__name__, record["error"])
                    for artifact in (output.read_text(encoding="utf-8"), json.dumps(writes), "\n".join(logs)):
                        for secret in secrets:
                            self.assertNotIn(secret, artifact)
                    self.assert_cleaned(browser)

    def test_cleanup_errors_do_not_persist_or_emit_credentials(self):
        secrets = ("fixture-token", "fixture-cookie", "fixture-user", "fixture-password")
        message = "token=fixture-token Cookie: sid=fixture-cookie https://fixture-user:fixture-password@provider.test"

        class CleanupErrorBrowser(FakeBrowser):
            def connect(self):
                connection = super().connect()
                original_send = connection.send

                def send(method, params=None, sid=None, timeout=30):
                    response = original_send(method, params, sid, timeout)
                    if method == "Target.closeTarget":
                        raise OSError(message)
                    return response

                def close():
                    connection.closed = True
                    raise RuntimeError(message)

                connection.send = send
                connection.close = close
                return connection

        result, browser, _, output, writes, logs = self.run_batch(
            CleanupErrorBrowser(), jobs=[job(1)], workers=1,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.records[0]["cleanup_errors"], [
            "自有详情页关闭失败：OSError", "CDP 连接关闭失败：RuntimeError",
        ])
        for artifact in (output.read_text(encoding="utf-8"), json.dumps(writes), "\n".join(logs)):
            for secret in secrets:
                self.assertNotIn(secret, artifact)
        self.assert_cleaned(browser)

    def test_target_created_before_helper_attach_failure_is_still_closed(self):
        result, browser, _, _, _, _ = self.run_batch(FakeBrowser(attach_failure=True), jobs=[job(1), job(2)], workers=1)
        self.assertEqual([record["status"] for record in result.records], ["failed", "success"])
        self.assertEqual(browser.navigation_order, ["job-2"])
        self.assert_cleaned(browser)

    def test_explicit_stop_saves_successes_and_does_not_start_more_jobs(self):
        control = adapter.StopState()

        def on_progress(line):
            if "JD:" in line:
                control.request_stop()

        result, browser, _, _, _, _ = self.run_batch(workers=1, control=control, emit=on_progress)
        self.assertEqual(result.status, "stopped")
        self.assertEqual(result.exit_code, 130)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(detail_completeness(result.records[0]), "complete")
        self.assert_cleaned(browser)

    def test_gate_while_connection_is_opening_prevents_page_creation_and_closes_connection(self):
        browser = FakeBrowser()
        control = adapter.StopState()
        scraper = FakeScraper(browser)

        def connect():
            connection = browser.connect()
            control.request_stop(gate_message="安全验证：需要人工验证")
            return connection

        with tempfile.TemporaryDirectory() as directory:
            result = adapter.collect_details(
                {"jobs": [job(1), job(2)]}, Path(directory) / "details.json", scraper=scraper,
                workers=1, cdp_factory=connect, control=control, timing=ZERO_TIMING, emit=lambda _: None,
            )
        self.assertEqual(result.status, "paused")
        self.assertEqual(len(browser.connections), 1)
        self.assertEqual(browser.owned_targets, [])
        self.assert_cleaned(browser)

    def test_write_failure_stops_workers_and_still_closes_all_created_pages(self):
        browser = FakeBrowser(simultaneous=2)

        def writer(_path, records):
            if records:
                raise OSError("disk full")

        with self.assertRaisesRegex(OSError, "disk full"):
            self.run_batch(browser, persist=writer, workers=2)
        self.assert_cleaned(browser)

    def test_empty_batch_is_local_only_and_supports_legacy_job_wrapper(self):
        result, browser, scraper, _, writes, _ = self.run_batch(jobs=[])
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.records, [])
        self.assertEqual(browser.connections, [])
        self.assertEqual(scraper.dependency_checks, [])
        self.assertEqual(writes, [[]])

    def test_workers_one_to_32_and_invalid_input_are_checked_before_browser_actions(self):
        scraper = FakeScraper(FakeBrowser())
        with tempfile.TemporaryDirectory() as directory:
            for workers in (0, 33, True, 1.5, "3"):
                with self.subTest(workers=workers), self.assertRaises(ValueError):
                    adapter.collect_details([], Path(directory) / "out.json", scraper=scraper, workers=workers)
            for value in (None, {"jobs": None}, {"jobs": [None]}):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    adapter.collect_details(value, Path(directory) / "out.json", scraper=scraper)
            result = adapter.collect_details([], Path(directory) / "out.json", scraper=scraper, workers=32, emit=lambda _: None)
            self.assertEqual(result.records, [])
        self.assertEqual(scraper.browser.connections, [])


class CLIAndImportTests(unittest.TestCase):
    def test_cli_errors_do_not_echo_third_party_credentials(self):
        secrets = ("fixture-token", "fixture-cookie", "fixture-user", "fixture-password", "fixture-query-key")
        message = (
            "token=fixture-token; Cookie: sid=fixture-cookie\n"
            "https://fixture-user:fixture-password@provider.test/path?api_key=fixture-query-key"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            source.write_text('{"jobs": []}', encoding="utf-8")
            args = ["--scraper-root", str(root), "--input", str(source),
                    "--detail-output", str(root / "details.json")]
            for failing_step, error_type in (("load_scraper", RuntimeError), ("collect_details", OSError)):
                with self.subTest(failing_step=failing_step):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with patch.object(adapter, "load_scraper", return_value=FakeScraper(FakeBrowser())) as load, \
                         patch.object(adapter, "collect_details") as collect, \
                         redirect_stdout(stdout), redirect_stderr(stderr):
                        (load if failing_step == "load_scraper" else collect).side_effect = error_type(message)
                        code = adapter.main(args)
                    self.assertEqual(code, 1)
                    self.assertIn(error_type.__name__, stderr.getvalue())
                    for secret in secrets:
                        self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())
                    if failing_step == "load_scraper":
                        collect.assert_not_called()

    def test_dynamic_import_supports_upstream_dataclasses_without_changing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            source = root / "scripts" / "boss_cdp_raw.py"
            source.write_text("from dataclasses import dataclass\n@dataclass\nclass Detail:\n    jd: str\n", encoding="utf-8")
            original = source.read_bytes()
            module = adapter.load_scraper(root)
            self.assertEqual(module.Detail("内容").jd, "内容")
            self.assertEqual(source.read_bytes(), original)

    def test_cli_passes_page_workers_to_independent_connections_and_restores_signal_handlers(self):
        browser = FakeBrowser(simultaneous=3)
        scraper = FakeScraper(browser)
        signals = [getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGBREAK") if hasattr(signal, name)]
        before = {number: signal.getsignal(number) for number in signals}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path, output = root / "input.json", root / "output.json"
            input_path.write_text(json.dumps({"jobs": [job(i) for i in range(4)]}), encoding="utf-8-sig")
            with patch.object(adapter, "load_scraper", return_value=scraper), patch.dict(adapter._TIMINGS, {"fast": ZERO_TIMING}), redirect_stdout(io.StringIO()):
                code = adapter.main(["--scraper-root", str(root), "--input", str(input_path),
                                     "--detail-output", str(output), "--workers", "3", "--detail-speed-profile", "fast"])
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))), 4)
        self.assertEqual(scraper.ports, [9222] * 4)
        self.assertEqual(scraper.dependency_checks, [("requests", "websocket")])
        self.assertEqual(browser.peak, 3)
        self.assertEqual({number: signal.getsignal(number) for number in signals}, before)
        self.assertEqual(browser.active, {"user-original-tab"})

    def test_cli_signal_requests_a_cooperative_stop_and_keeps_checkpoint(self):
        def snapshot(_connection, _read):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            return None

        scraper = FakeScraper(FakeBrowser(hook=snapshot))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "input.json", root / "output.json"
            source.write_text(json.dumps({"jobs": [job(1), job(2)]}), encoding="utf-8")
            with patch.object(adapter, "load_scraper", return_value=scraper), patch.dict(adapter._TIMINGS, {"fast": ZERO_TIMING}), redirect_stdout(io.StringIO()):
                code = adapter.main(["--scraper-root", str(root), "--input", str(source),
                                     "--detail-output", str(output), "--workers", "1"])
            self.assertEqual(code, 130)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))), 1)
        self.assertEqual(scraper.browser.active, {"user-original-tab"})

    def test_cli_reports_invalid_workers_and_does_not_allow_changing_scroll_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            source.write_text('{"jobs": []}', encoding="utf-8")
            args = ["--scraper-root", str(root), "--input", str(source), "--detail-output", str(root / "out.json")]
            with patch.object(adapter, "load_scraper", return_value=FakeScraper(FakeBrowser())), redirect_stderr(io.StringIO()):
                self.assertEqual(adapter.main(args + ["--workers", "33"]), 1)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                adapter.main(args + ["--scroll-count", "7"])
            self.assertEqual(error.exception.code, 2)


class InstalledHelperTests(unittest.TestCase):
    @unittest.skipUnless((Path.home() / ".codex" / "tools" / "boss-zhipin-scraper" / "scripts" / "boss_cdp_raw.py").is_file(), "Installed upstream helper is optional")
    def test_installed_page_visibility_extraction_and_record_helpers_work_with_fake_cdp(self):
        scraper = adapter.load_scraper(Path.home() / ".codex" / "tools" / "boss-zhipin-scraper")
        original_expression = scraper.EXTRACT_DETAIL_JS
        # The fake protocol recognizes a sentinel, but the adapter still calls
        # the real installed helper functions, including visibility and focus.
        scraper.EXTRACT_DETAIL_JS = "EXTRACT_DETAIL"
        self.addCleanup(setattr, scraper, "EXTRACT_DETAIL_JS", original_expression)
        browser = FakeBrowser(simultaneous=2)
        inputs = [job(1), job(2)]
        inputs[0].update({"security_id": "existing-page-token", "lid": "list-context"})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "details.json"
            result = adapter.collect_details({"jobs": inputs}, output, scraper=scraper, workers=2,
                                              cdp_factory=browser.connect, timing=ZERO_TIMING, emit=lambda _: None)
            self.assertEqual(result.status, "completed")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result.records)
            self.assertEqual(len(result.records), 2)
            self.assertTrue(all(detail_completeness(record) == "complete" for record in result.records))
        navigations = [params["url"] for _, method, params, *_ in browser.commands if method == "Page.navigate"]
        self.assertTrue(any("securityId=existing-page-token" in url and "lid=list-context" in url for url in navigations))
        scripts = [params["source"] for _, method, params, *_ in browser.commands if method == "Page.addScriptToEvaluateOnNewDocument"]
        self.assertEqual(scripts, [scraper.BACKGROUND_VISIBILITY_SCRIPT] * 2)
        self.assertEqual(browser.active, {"user-original-tab"})
        self.assertTrue(all(connection.scrolls == 3 for connection in browser.connections))


if __name__ == "__main__":
    unittest.main()
