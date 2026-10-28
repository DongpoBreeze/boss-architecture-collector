"""Offline contract tests for dual-browser JD collection.

The fake CDP implementation below records navigation and connection ownership,
but never starts a browser or contacts the network.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import jd_parallel as adapter  # noqa: E402


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
            emit=lambda _message: None,
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


if __name__ == "__main__":
    unittest.main()
