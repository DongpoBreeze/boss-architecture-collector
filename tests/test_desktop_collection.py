"""Offline collection-page acceptance: real Tk, temporary roots, fake CDP children."""

from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import collect_architecture as collector
import desktop_collection as ui
from review_pipeline import _pid_alive
from desktop_theme import configure_styles, palette_for


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def small_config():
    return {
        "cities": [{"id": name, "name": label} for name, label in (
            ("shanghai", "上海"), ("shenzhen", "深圳"), ("hangzhou", "杭州"),
            ("guangzhou", "广州"), ("chengdu", "成都"))],
        "salaries": [{"id": "20_50", "code": "406"}],
        "current": {"keywords": [{"id": "solution", "query": "AI解决方案"},
                                  {"id": "product", "query": "AI产品"}],
                    "experiences": [{"id": "1_3", "code": "104"}]},
        "target": {"keywords": [{"id": "architecture", "query": "AI架构"}],
                   "experiences": [{"id": "3_5", "code": "105"}]},
    }


class FakeOutput:
    """A character pipe that really blocks until stdin/stop releases it."""
    def __init__(self, text=""):
        self._parts = deque(text)
        self._condition = threading.Condition()
        self._ended = False
        self.closed = False

    def feed(self, text="", *, end=False):
        with self._condition:
            self._parts.extend(text)
            self._ended |= end
            self._condition.notify_all()

    def read(self, _size):
        with self._condition:
            while not self._parts and not self._ended and not self.closed:
                self._condition.wait(0.05)
            return self._parts.popleft() if self._parts else ""

    def close(self):
        with self._condition:
            self.closed = True
            self._condition.notify_all()


class FakeInput:
    def __init__(self, process):
        self.process = process
        self.writes = []
        self.closed = False

    def write(self, value):
        self.writes.append(value)
        if self.process.on_input:
            self.process.on_input(value)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    next_pid = 900_000

    def __init__(self, output="", on_input=None):
        FakeProcess.next_pid += 1
        self.pid = FakeProcess.next_pid
        self.stdout = FakeOutput(output)
        self.stdin = FakeInput(self)
        self.on_input = on_input
        self.returncode = None
        self._done = threading.Event()
        self.stop_release = None
        self.terminated = 0

    def finish(self, code=0, text=""):
        self.returncode = code
        self.stdout.feed(text, end=True)
        self._done.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(["fake-collection"], timeout)
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if self.stop_release:
            self.stop_release.wait(4)
        if self.returncode is None:
            self.finish(-15)

    def kill(self):
        self.finish(-9)


class FakeTree:
    instances = []

    def __init__(self, child):
        self.child = child
        self.resumed = False
        self.closed = False
        self.preserved = None
        FakeTree.instances.append(self)

    def resume(self):
        self.resumed = True

    def terminate(self):
        self.child.terminate()

    def close(self, preserve_detached=False):
        self.closed = True
        self.preserved = preserve_detached
        if not preserve_detached and self.child.poll() is None:
            self.child.terminate()


def stop_fake(child, tree=None):
    if tree:
        tree.terminate()
    elif child.poll() is None:
        child.terminate()


class CollectionHelpersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "collection"
        self.config = small_config()
        self.config_path = self.base / "config.json"
        write_json(self.config_path, self.config)
        self.specs = collector.build_specs(self.config)

    def manifest(self, records, expected=None):
        value = {"records": records, "pages": 10}
        if expected is not None:
            value["expected_combinations"] = expected
            value["expected_combination_specs"] = self.specs
        write_json(self.root / "collection_manifest.json", value)

    def test_all_city_track_page_and_limit_choices_use_real_build_specs(self):
        config = deepcopy(self.config)
        config["cities"].append({"id": "beijing", "name": "北京"})
        for city in ui.CITY_CHOICES:
            for track in ui.TRACK_CHOICES:
                for pages in (1, 10):
                    with self.subTest(city=city, track=track, pages=pages):
                        selection = ui.CollectionSelection.parse(city, track, "", str(pages))
                        specs = collector.build_specs(config, selection.track, selection.limit, selection.cities)
                        per_city = {"all": 3, "current": 2, "target": 1}[track]
                        self.assertEqual(len(specs), per_city * (6 if city == "全部城市" else 1))
                        self.assertEqual(selection.pages, pages)
        selected = ui.CollectionSelection.parse("全部", "all", "2", "3")
        self.assertEqual(len(collector.build_specs(self.config, selected.track, selected.limit, selected.cities)), 2)
        self.assertIsNone(ui.CollectionSelection.parse("上海", "current", "  ", "1").limit)

    def test_invalid_selectors_and_numeric_bounds_are_rejected(self):
        for city, track, limit, pages in (
            ("不存在的城市", "all", "", "1"), ("上海", "other", "", "1"),
            ("上海", "current", "0", "1"), ("上海", "current", "-1", "1"),
            ("上海", "all", "1.5", "1"), ("上海", "all", "", "0"),
            ("上海", "all", "", "11"), ("上海", "all", "", "1.5"),
            ("上海", "all", "", ""),
        ):
            with self.subTest(values=(city, track, limit, pages)), self.assertRaises(ValueError):
                ui.CollectionSelection.parse(city, track, limit, pages)

    def test_command_invokes_only_collect_cli_and_explicit_setup_only(self):
        request = ui.CollectionRequest(str(self.root), "", self.config_path,
                                       ui.CollectionSelection.parse("杭州", "target", "3", "2"))
        for setup_only in (False, True):
            command = ui.make_collect_command(request, self.root, self.base / "scraper", setup_only=setup_only)
            self.assertEqual(Path(command[2]).name, "collect_architecture.py")
            parsed = collector.parse_args(command[3:])
            self.assertEqual((parsed.batch, parsed.cities, parsed.limit, parsed.pages), ("target", ["杭州"], 3, 2))
            self.assertTrue(parsed.gui_interactive)
            self.assertTrue(parsed.setup_browser)
            self.assertEqual(parsed.setup_only, setup_only)
            self.assertTrue(parsed.wait_on_gate)
            self.assertFalse(parsed.verbose_upstream)
            self.assertNotIn("process_architecture.py", " ".join(command))
            self.assertNotIn("--stage", command)

    def test_runtime_request_defaults_and_custom_wait_command_preserve_positional_interface(self):
        request = ui.CollectionRequest(str(self.root), "", self.config_path, ui.CollectionSelection())
        self.assertEqual((request.page_wait_min, request.page_wait_max, request.browser_workers), (12.0, 22.0, 3))
        request = ui.CollectionRequest(str(self.root), "", self.config_path, ui.CollectionSelection(),
                                       page_wait_min=0.25, page_wait_max=1.75, browser_workers=8)
        args = collector.parse_args(ui.make_collect_command(request, self.root, self.base / "scraper",
                                                            browser="edge", shard=15, total_shards=16,
                                                            setup_browser=False)[3:])
        self.assertEqual((args.page_wait_min, args.page_wait_max), (0.25, 1.75))
        self.assertEqual((args.shard, args.total_shards, args.browser, args.cdp_port), (15, 16, "edge", 9223))
        self.assertFalse(args.setup_browser)
        self.assertFalse(args.setup_only)

    def test_new_root_preview_is_read_only_and_total_is_unknown(self):
        self.assertEqual(ui.validate_collection_root(self.root), self.root.resolve())
        snapshot = ui.read_collection_progress(self.root)
        self.assertEqual(snapshot["manifest_state"], "missing")
        self.assertIsNone(snapshot["total"])
        self.assertIsNone(snapshot["pages_requested"])
        self.assertEqual(snapshot["rows"], [])
        self.assertIn("总量未知", snapshot["message"])
        self.assertFalse(self.root.exists())

    def test_invalid_manifest_is_not_backed_up_overwritten_or_counted(self):
        path = self.root / "collection_manifest.json"
        for invalid in ("{broken", "[]", '{"records":{}}', '{"records":[null]}',
                        '{"records":[],"expected_combinations":[null]}'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(invalid, encoding="utf-8")
            before = path.read_bytes()
            snapshot = ui.read_collection_progress(self.root)
            self.assertEqual(snapshot["manifest_state"], "invalid")
            self.assertIsNone(snapshot["total"])
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(path.with_suffix(".json.corrupt").exists())

    def test_success_no_data_failure_pause_running_statistics_come_from_manifest(self):
        expected = [spec["combination"] for spec in self.specs[:6]]
        records = [{**spec, "status": status, "job_count": count, "pages_completed": pages, "pages_requested": 10}
                   for spec, status, count, pages in zip(self.specs, ("success", "no_data", "failed", "paused", "running"),
                                                       (8, 0, None, 3, 2), (10, 10, 1, 2, 3))]
        records.append({"combination": "outside-old-scope", "status": "success", "job_count": 999})
        self.manifest(records, expected)
        value = ui.read_collection_progress(self.root)
        self.assertEqual(value["total"], 6)
        self.assertEqual(value["done"], 2)
        self.assertEqual(value["jobs"], 13)
        self.assertEqual(value["pages_completed"], 26)
        self.assertEqual(value["pages_requested"], 60)
        for status in ("success", "no_data", "failed", "paused", "running"):
            self.assertEqual(value["counts"][status], 1)
        self.assertEqual(value["rows"][-1]["status"], "pending")

    def test_records_without_expected_scope_do_not_invent_a_total(self):
        self.manifest([{"combination": "one", "status": "success", "job_count": 4}])
        value = ui.read_collection_progress(self.root)
        self.assertIsNone(value["total"])
        self.assertEqual(value["done"], 1)
        self.assertEqual(value["jobs"], 4)

    def test_resume_page_total_uses_historical_request_before_new_global_setting(self):
        first, second = self.specs[:2]
        write_json(self.root / "collection_manifest.json", {
            "records": [{**first, "status": "success", "pages_requested": 10, "pages_completed": 10}],
            "expected_combinations": [first["combination"], second["combination"]],
            "expected_combination_specs": [{**first, "pages_requested": 1}, {**second, "pages_requested": 1}],
            "pages": 1,
        })
        value = ui.read_collection_progress(self.root)
        self.assertEqual(value["pages_completed"], 10)
        self.assertEqual(value["pages_requested"], 11)
        self.assertEqual([row["pages_requested"] for row in value["rows"]], [10, 1])

    def test_missing_per_group_and_global_page_requests_stay_unknown(self):
        self.manifest([{"combination": "one", "status": "paused", "pages_completed": 2}], ["one"])
        path = self.root / "collection_manifest.json"
        value = collector.read_json(path)
        value.pop("pages")
        write_json(path, value)
        snapshot = ui.read_collection_progress(self.root)
        self.assertEqual(snapshot["pages_completed"], 2)
        self.assertIsNone(snapshot["pages_requested"])

    def test_latest_duplicate_record_counts_once_and_invalid_numbers_are_not_jobs(self):
        self.manifest([
            {"combination": "one", "status": "success", "job_count": 50},
            {"combination": "one", "status": "paused", "job_count": True, "pages_completed": -2},
        ], ["one", "one"])
        value = ui.read_collection_progress(self.root)
        self.assertEqual((value["total"], value["done"], value["jobs"], value["pages_completed"]), (1, 0, 0, 0))
        self.assertEqual(value["counts"]["paused"], 1)

    def test_root_rejects_files_reserved_subfolders_and_existing_collection_descendants(self):
        self.root.mkdir()
        file = self.base / "output.json"
        file.write_text("{}", encoding="utf-8")
        forbidden = [file, self.root / "raw/current", self.root / "raw/target", self.root / "prepared",
                     self.root / "runtime/fetch-jd", self.root / "runs/old", self.root / "checkpoints"]
        for value in forbidden:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ui.validate_collection_root(value)
        self.manifest([], [])
        with self.assertRaises(ValueError):
            ui.validate_collection_root(self.root / "another-folder")
        self.assertEqual(ui.validate_collection_root(self.root), self.root.resolve())

    def test_task_root_or_arbitrary_task_descendant_is_never_collection_output(self):
        task = self.base / "tasks" / "named-task"
        write_json(task / "task_manifest.json", {"task_id": "untouched"})
        before = (task / "task_manifest.json").read_bytes()
        for path in (task, task / "runtime/import", task / "other-new-folder"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                ui.validate_collection_root(path)
        self.assertEqual((task / "task_manifest.json").read_bytes(), before)

    def test_write_guard_rejects_injected_paths_and_linked_output(self):
        invalid = {**self.specs[0], "city_id": "../outside"}
        with self.assertRaises(ValueError):
            ui._guard_output_paths(self.root, [invalid])
        linked = self.root / "raw"
        with patch.object(ui, "_reparse", side_effect=lambda path: path == linked):
            with self.assertRaises(ValueError):
                ui._guard_output_paths(self.root, self.specs)

    def test_cookie_authorization_json_and_url_credentials_are_redacted(self):
        value = ('Cookie: a=PRIVATE_COOKIE; b=PRIVATE_COOKIE_2\nAuthorization: Basic PRIVATE_AUTH\n'
                 'api_key="PRIVATE_KEY" password=PRIVATE_PASSWORD\n'
                 '{"access_token":"PRIVATE_TOKEN","cookie":"PRIVATE_JSON_COOKIE"}\n'
                 'https://user:PRIVATE_URL_PASSWORD@example.invalid/?session_id=PRIVATE_SESSION\n')
        safe = ui.redact_log(value)
        for secret in ("PRIVATE_COOKIE", "PRIVATE_AUTH", "PRIVATE_KEY", "PRIVATE_PASSWORD",
                       "PRIVATE_TOKEN", "PRIVATE_JSON_COOKIE", "PRIVATE_URL_PASSWORD", "PRIVATE_SESSION"):
            self.assertNotIn(secret, safe)
        self.assertIn("[REDACTED]", safe)

    def test_interruption_retains_completed_records_and_copies_standard_checkpoint(self):
        spec, completed = self.specs[:2]
        done_record = {**completed, "status": "success", "job_count": 8, "finished_at": "2020-01-01"}
        self.manifest([done_record], [item["combination"] for item in self.specs])
        output = self.root / "raw" / spec["track"] / spec["city_id"] / f"{spec['combination']}.json"
        write_json(output, {"jobs": [{"id": "partial"}]})
        before = output.read_bytes()
        result = ui.CommandResult(-15, 111, spec["combination"], 2, 2)
        ui.preserve_interrupted_combination(self.root, self.specs, 10, result, "paused", "2026-10-21", lambda _: None)
        manifest = collector.read_json(self.root / "collection_manifest.json")
        record = collector.record_for(manifest, spec["combination"])
        self.assertEqual((record["status"], record["pages_completed"], record["job_count"]), ("paused", 2, 1))
        self.assertEqual(Path(record["checkpoint"]).read_bytes(), before)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(collector.record_for(manifest, completed["combination"]), done_record)

    def test_stop_unions_a_preexisting_checkpoint_without_demoting_done_combination(self):
        spec = self.specs[0]
        self.manifest([], [spec["combination"]])
        output = self.root / "raw" / spec["track"] / spec["city_id"] / f"{spec['combination']}.json"
        checkpoint = self.root / "checkpoints" / f"{spec['combination']}.partial.json"
        write_json(output, {"jobs": [{"id": "new"}]})
        write_json(checkpoint, {"jobs": [{"id": "old-checkpoint"}]})
        result = ui.CommandResult(-15, 111, spec["combination"])
        ui.preserve_interrupted_combination(self.root, self.specs, 10, result, "stopped", "2026-10-21", lambda _: None)
        self.assertEqual(collector.read_json(checkpoint)["jobs"], [{"id": "new"}, {"id": "old-checkpoint"}])
        self.assertEqual(collector.read_json(output)["jobs"], collector.read_json(checkpoint)["jobs"])
        path = self.root / "collection_manifest.json"
        before_manifest = path.read_bytes()
        result.completed.add(spec["combination"])
        ui.preserve_interrupted_combination(self.root, self.specs, 10, result, "paused", "2026-10-21", lambda _: None)
        self.assertEqual(path.read_bytes(), before_manifest)

    def test_foreign_manifest_lock_is_not_removed_or_waited_on(self):
        spec = self.specs[0]
        self.manifest([], [spec["combination"]])
        lock = self.root / "collection_manifest.json.lock"
        lock.write_text("pid=222\ncreated_at=now\n", encoding="utf-8")
        before = (self.root / "collection_manifest.json").read_bytes()
        started = time.monotonic()
        ui.preserve_interrupted_combination(self.root, self.specs, 10,
                                           ui.CommandResult(-15, 111, spec["combination"]),
                                           "paused", "2026-10-21", lambda _: None)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(lock.read_text(encoding="utf-8"), "pid=222\ncreated_at=now\n")
        self.assertEqual((self.root / "collection_manifest.json").read_bytes(), before)

    def test_only_the_exited_owned_root_manifest_lock_can_be_reclaimed(self):
        self.root.mkdir()
        lock = self.root / "collection_manifest.json.lock"
        lock.write_text("pid=111\ncreated_at=now\n", encoding="utf-8")
        ui._clear_owned_lock(self.root, ui.CommandResult(-15, 111), lambda _: None)
        self.assertFalse(lock.exists())

    def test_owner_interpreter_lock_is_reclaimed_only_when_both_known_processes_exited(self):
        self.root.mkdir()
        lock = self.root / "collection_manifest.json.lock"
        lock.write_text("pid=222\ncreated_at=now\n", encoding="utf-8")
        result = ui.CommandResult(-15, 111, collector_pid=222)
        with patch.object(ui, "_pid_alive", side_effect=lambda pid: pid == 222):
            ui._clear_owned_lock(self.root, result, lambda _: None)
            self.assertTrue(lock.exists(), "live actual interpreter still owns its lock")
        with patch.object(ui, "_pid_alive", side_effect=lambda pid: pid == 111):
            ui._clear_owned_lock(self.root, result, lambda _: None)
            self.assertTrue(lock.exists(), "live launcher has not fully stopped")
        with patch.object(ui, "_pid_alive", return_value=False):
            ui._clear_owned_lock(self.root, result, lambda _: None)
            self.assertFalse(lock.exists())
            lock.write_text("pid=333\ncreated_at=now\n", encoding="utf-8")
            ui._clear_owned_lock(self.root, result, lambda _: None)
            self.assertTrue(lock.exists(), "an exited but unowned PID is not sufficient")

    def test_chinese_group_labels_keep_programmatic_cli_values_compatible(self):
        for internal, label in ui.TRACK_LABELS.items():
            self.assertEqual(ui.CollectionSelection.parse("上海", label, "", "2").track, internal)
            self.assertEqual(ui.CollectionSelection.parse("上海", internal, "", "2").track, internal)

    def test_default_plan_preview_is_672_and_shanghai_112_without_disk_progress(self):
        path = PROJECT_ROOT / "config" / "architecture_collection.json"
        before = path.read_bytes()
        config = collector.load_config(path)
        all_plan = ui.build_collection_preview(config, ui.CollectionSelection())
        shanghai = ui.build_collection_preview(config, ui.CollectionSelection(city="上海", pages=3))
        self.assertEqual((all_plan["full"], all_plan["estimate"], len(all_plan["plan_rows"])), (672, 672, 672))
        self.assertEqual((shanghai["full"], shanghai["estimate"]), (112, 112))
        self.assertEqual(sum(row["track"] == "current" for row in shanghai["plan_rows"]), 88)
        self.assertEqual(sum(row["track"] == "target" for row in shanghai["plan_rows"]), 24)
        self.assertEqual({row["track_label"] for row in shanghai["plan_rows"]}, {"当前求职", "成长参考"})
        self.assertTrue(all(row["degree_label"] == "本科" and row["pages_requested"] == 3
                            and row["execution_status"] == "本次执行" for row in shanghai["plan_rows"]))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.root.exists())

    def test_preview_limit_is_after_checkboxes_and_keeps_config_order_not_click_order(self):
        ids = tuple(self.specs[index]["combination"] for index in (4, 2, 0))
        config_before = deepcopy(self.config)
        value = ui.build_collection_preview(self.config, ui.CollectionSelection(limit=2), ids)
        self.assertEqual([spec["combination"] for spec in value["execution_specs"]],
                         [self.specs[index]["combination"] for index in (0, 2)])
        self.assertEqual((value["full"], value["selected_count"], value["estimate"]), (15, 3, 2))
        limited = [row for row in value["plan_rows"] if row["execution_status"] == "超出本次上限"]
        self.assertEqual([row["combination"] for row in limited], [self.specs[4]["combination"]])
        self.assertEqual(sum(not row["checked"] for row in value["plan_rows"]), 12)
        self.assertEqual(self.config, config_before)

    def test_loaded_exact_mask_can_be_previewed_and_reenabled_without_losing_catalog(self):
        selected = ui.plans.select_plan_combinations(self.config, [self.specs[2]["combination"]])
        value = ui.build_collection_preview(selected, ui.CollectionSelection())
        self.assertEqual((value["full"], value["selected_count"], value["estimate"]), (15, 1, 1))
        self.assertEqual(len(value["config"]["current"]["keywords"]), 2)
        empty = ui.build_collection_preview(selected, ui.CollectionSelection(), ())
        self.assertEqual((empty["full"], empty["selected_count"], empty["estimate"]), (15, 0, 0))
        self.assertTrue(all(row["execution_status"] == "未勾选" for row in empty["plan_rows"]))
        with self.assertRaisesRegex(ValueError, "不匹配"):
            ui.build_collection_preview(selected, ui.CollectionSelection(), ("not-in-this-plan",))

    def test_page_rejects_non_bachelor_degree_instead_of_showing_a_false_fixed_label(self):
        config = {**self.config, "degree": {"id": "202", "name": "大专"}}
        with self.assertRaisesRegex(ValueError, "仅支持本科"):
            ui.build_collection_preview(config, ui.CollectionSelection())


class OwnedCommandTests(unittest.TestCase):
    def setUp(self):
        self.processes = []
        self.workers = []
        for patcher in (
            patch.object(ui, "_child_options", return_value={}),
            patch.object(ui, "_WindowsProcessTree", FakeTree),
            patch.object(ui, "_stop_owned_child", side_effect=stop_fake),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.cleanup_workers)

    def cleanup_workers(self):
        for process in self.processes:
            if process.stop_release:
                process.stop_release.set()
            process.finish(-15)
        for worker in self.workers:
            worker.join(3)

    def run_background(self, process, control, events, monitor=None):
        self.processes.append(process)
        runner = ui._OwnedCommand()
        result = []
        error = []

        def work():
            try:
                with patch.object(ui.subprocess, "Popen", return_value=process):
                    result.append(runner.run(["fake-collect"], control, lambda kind, value: events.append((kind, value)), monitor))
            except Exception as caught:
                error.append(caught)
        worker = threading.Thread(target=work)
        self.workers.append(worker)
        worker.start()
        return runner, worker, result, error

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("condition did not arrive")

    def test_newline_free_human_prompt_waits_and_only_explicit_continue_sends_enter(self):
        process = FakeProcess("验证码：需人工处理\n验证完成后按 Enter 继续，q 退出: ")
        process.on_input = lambda value: process.finish(0, "\nDONE example jobs=1\n")
        events = []
        control = ui.CollectionControl()
        runner, worker, result, error = self.run_background(process, control, events)
        self.wait_for(lambda: any(kind == "waiting" for kind, _ in events))
        self.assertEqual(process.stdin.writes, [])
        self.assertFalse(runner.shutdown_complete)
        self.assertTrue(runner.request_continue())
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(error, [])
        self.assertEqual(result[0].code, 0)
        self.assertEqual(process.stdin.writes, ["\n"])
        self.assertTrue(runner.shutdown_complete)

    def test_process_identity_requires_this_launch_token_and_accepts_only_first_owner(self):
        process = FakeProcess()
        self.processes.append(process)
        runner = ui._OwnedCommand()
        events = []
        tokens = []

        def launch(_command, **options):
            token = options["env"][collector.DESKTOP_OWNER_TOKEN_ENV]
            tokens.append(token)
            process.finish(0, f"{collector.DESKTOP_OWNER_PREFIX} token={'f' * 32} pid=999\n"
                           f"{collector.DESKTOP_OWNER_PREFIX} token={token} pid=222\n"
                           f"{collector.DESKTOP_OWNER_PREFIX} token={token} pid=333\n")
            return process
        with patch.object(ui.subprocess, "Popen", side_effect=launch):
            result = runner.run(["fake-collect"], ui.CollectionControl(), lambda kind, value: events.append((kind, value)))
        self.assertEqual(result.collector_pid, 222)
        self.assertNotIn(tokens[0], str(events))
        self.assertNotIn("pid=999", str(events))
        self.assertTrue(runner.shutdown_complete)

    def test_entire_output_is_redacted_before_events_and_overlong_line_is_hidden(self):
        process = FakeProcess('api_key="SPLIT_PRIVATE_KEY"\nCookie: a=PRIVATE_COOKIE\nAuthorization: Bearer PRIVATE_AUTH\n'
                              + "x" * 40 + "password=OVERFLOW_SECRET\n")
        process.finish(0)
        events = []
        with patch.object(ui, "MAX_LOG_LINE", 32):
            runner, worker, result, error = self.run_background(process, ui.CollectionControl(), events)
            worker.join(3)
        self.assertEqual(error, [])
        visible = str(events)
        for secret in ("SPLIT_PRIVATE_KEY", "PRIVATE_COOKIE", "PRIVATE_AUTH", "OVERFLOW_SECRET"):
            self.assertNotIn(secret, visible)
        self.assertIn("日志行过长", visible)
        self.assertTrue(runner.shutdown_complete)

    def test_pause_stops_only_this_owned_popen_and_keeps_last_real_page_markers(self):
        process = FakeProcess("RUN current__shanghai__one (now), pages=10\n[2/10 页] 本页 1 条\n")
        control = ui.CollectionControl()
        events = []
        runner, worker, result, error = self.run_background(process, control, events)
        self.wait_for(lambda: any(kind == "position" and "2/10" in value for kind, value in events))
        control.pause()
        worker.join(3)
        self.assertEqual(error, [])
        self.assertEqual(result[0].combination, "current__shanghai__one")
        self.assertEqual(result[0].pages_completed, 2)
        self.assertEqual(result[0].code, -15)
        self.assertGreater(process.terminated, 0)
        self.assertTrue(runner.shutdown_complete)

    def test_tree_assignment_failure_stops_suspended_root_and_closes_pipes(self):
        process = FakeProcess()
        self.processes.append(process)
        runner = ui._OwnedCommand()
        with patch.object(ui.subprocess, "Popen", return_value=process), \
                patch.object(ui, "_WindowsProcessTree", side_effect=RuntimeError("assignment failed")), \
                self.assertRaises(RuntimeError):
            runner.run(["fake-collect"], ui.CollectionControl(), lambda *_: None)
        self.assertGreater(process.terminated, 0)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(runner.shutdown_complete)

    def test_final_monitor_cancellation_cannot_detach_a_new_browser_tree(self):
        process = FakeProcess("ready\n")
        process.finish(0)
        control = ui.CollectionControl()
        runner = ui._OwnedCommand()
        trees_before = len(FakeTree.instances)
        self.processes.append(process)
        with patch.object(ui.subprocess, "Popen", return_value=process):
            runner.run(["fake-collect"], control, lambda *_: None, control.stop)
        if sys.platform == "win32":
            tree = FakeTree.instances[trees_before]
            self.assertFalse(tree.preserved)
        self.assertEqual(control.requested, "stopped")
        self.assertTrue(runner.shutdown_complete)


class LocalOwnedProcessTests(unittest.TestCase):
    """Harmless Python stand-ins exercise the real Windows ownership helpers.

    No collector entry point, CDP endpoint, browser, network or model is used.
    """
    def test_real_local_child_redacts_output_and_accepts_newline_free_confirmation(self):
        control = ui.CollectionControl()
        runner = ui._OwnedCommand()
        events = []

        def emit(kind, value):
            events.append((kind, value))
            if kind == "waiting" and value:
                self.assertTrue(runner.request_continue())
        command = [sys.executable, "-u", "-c", (
            "import sys; print('Cookie: sid=REAL_PIPE_PRIVATE_COOKIE',flush=True); "
            "sys.stdout.write('验证完成后按 Enter 继续: ');sys.stdout.flush(); "
            "assert sys.stdin.readline()=='\\n'; print('confirmed',flush=True)"
        )]
        result = runner.run(command, control, emit)
        self.assertEqual(result.code, 0)
        self.assertTrue(any(kind == "waiting" for kind, _ in events))
        self.assertNotIn("REAL_PIPE_PRIVATE_COOKIE", str(events))
        self.assertIn("confirmed", str(events))
        self.assertTrue(runner.shutdown_complete)

    def test_real_owned_descendant_stops_even_when_its_root_has_already_exited(self):
        control = ui.CollectionControl()
        runner = ui._OwnedCommand()
        descendants = []

        def emit(kind, value):
            if kind == "log" and value.startswith("descendant:"):
                descendants.append(int(value.split(":", 1)[1].strip()))

        def monitor():
            if descendants and runner.child.poll() is not None:
                control.pause()
        command = [sys.executable, "-u", "-c", (
            "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(8)']); "
            "print('descendant:'+str(p.pid),flush=True)"
        )]
        started = time.monotonic()
        runner.run(command, control, emit, monitor)
        self.assertEqual(len(descendants), 1)
        self.assertEqual(control.requested, "paused")
        self.assertFalse(_pid_alive(descendants[0]))
        self.assertLess(time.monotonic() - started, 6)
        self.assertTrue(runner.shutdown_complete)

    @unittest.skipUnless(sys.platform == "win32", "Windows venv interpreter identity and owned jobs")
    def test_real_local_venv_identity_recovers_actual_interpreter_lock_after_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = {"combination": "offline-only", "track": "current", "city_id": "offline"}
            control, runner = ui.CollectionControl(), ui._OwnedCommand()
            lock_owner = []
            logs = []

            def emit(kind, value):
                if kind == "log":
                    logs.append(value)
                    if "holding-offline-manifest-lock" in value:
                        lock_owner.append((root / "collection_manifest.json.lock").read_text(encoding="utf-8"))
                        control.pause()
            script = (
                "import os,sys,time;from pathlib import Path;import collect_architecture as c;"
                "root=Path(sys.argv[1]);"
                "print(f'{c.DESKTOP_OWNER_PREFIX} token={os.environ[c.DESKTOP_OWNER_TOKEN_ENV]} pid={os.getpid()}',flush=True);"
                "print('RUN offline-only (now), pages=2',flush=True);"
                "\nwith c.manifest_lock(root/'collection_manifest.json'):"
                "\n print('holding-offline-manifest-lock',flush=True)"
                "\n time.sleep(30)\n"
            )
            result = runner.run([sys.executable, "-u", "-c", script, str(root)], control, emit)
            self.assertTrue(runner.shutdown_complete)
            self.assertEqual(control.requested, "paused")
            self.assertIsNotNone(result.collector_pid)
            self.assertIn(f"pid={result.collector_pid}\n", lock_owner[0])
            if (Path(sys.executable).parent.parent / "pyvenv.cfg").exists():
                self.assertNotEqual(result.pid, result.collector_pid, "venv redirector and actual Python must both be tracked")
            ui.preserve_interrupted_combination(root, [spec], 2, result, "paused", "2026-01-01", logs.append)
            self.assertFalse((root / "collection_manifest.json.lock").exists())
            manifest = collector.read_json(root / "collection_manifest.json")
            self.assertEqual(collector.record_for(manifest, "offline-only")["status"], "paused")


class CollectionTkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / "collection-output"
        self.config = small_config()
        self.config_path = self.base / "config.json"
        write_json(self.config_path, self.config)
        self.specs = collector.build_specs(self.config)
        self.scraper = self.base / "scraper"
        entry = self.scraper / "scripts/boss_cdp_raw.py"
        entry.parent.mkdir(parents=True)
        entry.write_text("# offline fixture, never executed", encoding="utf-8")
        self.pages = []
        self.processes = []
        self.releases = []
        self.errors = []
        self.main_thread = threading.get_ident()
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.root.attributes("-alpha", 0.0)
        if sys.platform == "win32":
            self.root.attributes("-disabled", True)
        errors = self.errors
        self.root.report_callback_exception = lambda *error: errors.append(error)
        configure_styles(self.root, palette_for("light"))
        self.popen = Mock(side_effect=AssertionError("real collector must never launch in an offline Tk test"))
        self.resolver = Mock(return_value=self.scraper)
        self.executor = Mock(side_effect=AssertionError("collection page cannot execute review"))
        for patcher in (
            patch.object(ui.subprocess, "Popen", self.popen),
            patch.object(ui.collector, "resolve_scraper_root", self.resolver),
            patch.object(ui, "_child_options", return_value={}),
            patch.object(ui, "_WindowsProcessTree", FakeTree),
            patch.object(ui, "_stop_owned_child", side_effect=stop_fake),
            patch("review_pipeline.execute_stage", self.executor),
            patch.object(ui.os, "startfile", Mock(), create=True),
            patch.object(ui, "PLAN_DIRECTORY", self.base / "saved-plans"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.cleanup_windows)

    def cleanup_windows(self):
        for event in self.releases:
            event.set()
        for process in self.processes:
            process.finish(-15)
        for page in self.pages:
            page.request_close()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and any(not page.shutdown_complete for page in self.pages):
            self.root.update()
            time.sleep(0.01)
        self.root.destroy()
        self.pages.clear()

    def make_page(self, browser_mode="chrome", browser_workers="1", **kwargs):
        page = ui.CollectionPage(self.root, config_path=self.config_path, initial_root=str(self.output), **kwargs)
        if browser_mode is not None:
            page.browser_mode.set(ui.BROWSER_MODES[browser_mode])
        if browser_workers is not None:
            page.browser_workers.set(str(browser_workers))
        page.pack(fill="both", expand=True)
        self.pages.append(page)
        self.root.update()
        return page

    def pump_until(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Tk condition did not arrive")

    def attach_fake(self, process, before_launch=None):
        self.processes.append(process)

        def launch(command, **options):
            self.assertEqual(Path(command[2]).name, "collect_architecture.py")
            self.assertNotIn("--stage", command)
            self.assertIs(options["stdin"], subprocess.PIPE)
            self.assertNotEqual(threading.get_ident(), self.main_thread)
            if before_launch:
                before_launch(command)
            return process
        self.popen.side_effect = launch

    def attach_browsers(self, processes, before_launch=None):
        self.processes.extend(value for value in processes.values() if isinstance(value, FakeProcess))
        observed = {}
        lock = threading.Lock()

        def launch(command, **options):
            self.assertNotEqual(threading.get_ident(), self.main_thread)
            self.assertEqual(Path(command[2]).name, "collect_architecture.py")
            self.assertIs(options["stdin"], subprocess.PIPE)
            args = collector.parse_args(command[3:])
            with lock:
                observed[args.browser] = args
            if before_launch:
                before_launch(args)
            process = processes[args.browser]
            if isinstance(process, Exception):
                raise process
            return process
        self.popen.side_effect = launch
        return observed

    def attach_slots(self, processes, before_launch=None):
        processes = dict(processes)
        for browser in ("chrome", "edge"):
            if (browser, "setup") not in processes:
                prepared = FakeProcess("browser prepared\n")
                prepared.finish(0)
                processes[(browser, "setup")] = prepared
        self.processes.extend(value for value in processes.values() if isinstance(value, FakeProcess))
        observed, lock = [], threading.Lock()

        def launch(command, **options):
            self.assertNotEqual(threading.get_ident(), self.main_thread)
            self.assertEqual(Path(command[2]).name, "collect_architecture.py")
            self.assertIs(options["stdin"], subprocess.PIPE)
            args = collector.parse_args(command[3:])
            key = (args.browser, "setup" if args.setup_only else args.shard)
            with lock:
                observed.append((key, args, options))
            if before_launch:
                before_launch(args, key, options)
            process = processes[key]
            if isinstance(process, Exception):
                raise process
            return process
        self.popen.side_effect = launch
        return observed, processes

    def write_manifest(self, records, specs=None):
        specs = self.specs if specs is None else specs
        write_json(self.output / "collection_manifest.json", {
            "records": records, "expected_combinations": [spec["combination"] for spec in specs],
            "expected_combination_specs": specs, "pages": 10,
        })

    def open_editor(self, page):
        page.open_plan_editor()
        self.pump_until(lambda: page._editor is not None)
        page._editor.withdraw()
        return page._editor

    @staticmethod
    def replace_keywords(editor, track, content):
        text = editor.keyword_texts[track]
        text.delete("1.0", "end")
        text.insert("1.0", content)
        text.edit_modified(True)
        text.event_generate("<<Modified>>")

    def test_construction_and_initial_read_only_preview_do_not_execute_or_discover_upstream(self):
        changed = Mock()
        page = self.make_page(on_busy_change=changed, initial_scraper_root=str(self.base / "not-installed"))
        self.pump_until(lambda: page._snapshot is not None)
        self.assertFalse(page.busy)
        self.assertEqual(page._snapshot["manifest_state"], "missing")
        self.assertIsNone(page._snapshot["total"])
        self.assertIn("15", page.estimate_text.get())
        self.assertFalse(self.output.exists())
        self.popen.assert_not_called()
        self.resolver.assert_not_called()
        self.executor.assert_not_called()
        changed.assert_not_called()

    def test_default_dual_selection_and_read_only_navigation_never_launch(self):
        page = self.make_page(browser_mode=None)
        self.assertEqual(page.browser_mode.get(), ui.BROWSER_MODES["dual"])
        self.pump_until(lambda: len(page._plan_rows) == 15)
        page.browser_mode.set(ui.BROWSER_MODES["chrome"])
        page.notebook.select(page.log_frame)
        page.apply_theme(palette_for("dark"))
        page.notebook.select(page.plan_frame)
        page.refresh_progress()
        self.pump_until(lambda: not page._preview_active)
        self.assertEqual(page.browser_mode.get(), ui.BROWSER_MODES["chrome"])
        self.popen.assert_not_called()
        self.resolver.assert_not_called()

    def test_runtime_defaults_validation_and_page_local_settings_are_read_only(self):
        page = self.make_page(browser_mode=None, browser_workers=None)
        self.assertEqual((page.page_wait_min.get(), page.page_wait_max.get(), page.browser_workers.get()), ("12", "22", "3"))
        self.assertEqual(page._capture_request().browser_workers, 3)
        for minimum, maximum, workers in (("", "22", "3"), ("nan", "22", "3"), ("1", "inf", "3"),
                                           ("-0.1", "22", "3"), ("2", "1", "3"), ("1", "300.1", "3"),
                                           ("1", "2", "0"), ("1", "2", "9"), ("1", "2", "1.5")):
            with self.subTest(minimum=minimum, maximum=maximum, workers=workers):
                page.page_wait_min.set(minimum)
                page.page_wait_max.set(maximum)
                page.browser_workers.set(workers)
                page.start()
                self.assertFalse(page.busy)
                self.assertTrue(page.status_text.get())
                self.popen.assert_not_called()
                self.resolver.assert_not_called()
        for workers in (1, 3, 8):
            page.browser_workers.set(str(workers))
            for minimum, maximum in ((0, 0), (0.25, 1.75), (300, 300)):
                page.page_wait_min.set(str(minimum))
                page.page_wait_max.set(str(maximum))
                request = page._capture_request()
                self.assertEqual((request.page_wait_min, request.page_wait_max, request.browser_workers), (minimum, maximum, workers))
        page.notebook.select(page.slot_frame)
        page.apply_theme(palette_for("dark"))
        page.notebook.select(page.plan_frame)
        self.assertEqual((page.page_wait_min.get(), page.page_wait_max.get(), page.browser_workers.get()), ("300", "300", "8"))
        self.assertFalse(self.output.exists())
        self.popen.assert_not_called()

    def test_multiple_targets_really_overlap_per_browser_and_cover_frozen_scope_at_3_and_8(self):
        expanded = deepcopy(self.config)
        expanded["current"]["keywords"].append({"id": "parallel", "query": "离线并发fixture"})
        write_json(self.config_path, expanded)
        all_specs = collector.build_specs(expanded)
        for workers in (3, 8):
            with self.subTest(workers=workers):
                run_output = self.base / f"parallel-{workers}"
                chosen = all_specs[2:19]
                per_browser = {browser: min(workers, len(chosen[index::2])) for index, browser in enumerate(("chrome", "edge"))}
                barriers = {browser: threading.Barrier(count) for browser, count in per_browser.items()}
                started = {browser: threading.Event() for browser in per_browser}
                processes = {}
                for index, browser in enumerate(("chrome", "edge")):
                    for slot in range(workers):
                        shard = index + 2 * slot
                        assigned = collector.shard_specs(chosen, shard, 2 * workers)
                        if assigned:
                            processes[(browser, shard)] = FakeProcess(
                                f"RUN {assigned[0]['combination']} (now), pages=3\n[1/3 页] 本页1条\n")

                def overlap(args, key, _options):
                    if not args.setup_only:
                        barriers[args.browser].wait(4)
                        started[args.browser].set()
                observed, all_processes = self.attach_slots(processes, overlap)
                page = self.make_page(browser_mode="dual", browser_workers=str(workers))
                page.collection_root.set(str(run_output))
                self.pump_until(lambda: len(page._plan_rows) == len(all_specs))
                page.toggle_combination(all_specs[0]["combination"])
                page.toggle_combination(all_specs[1]["combination"])
                page.limit.set("17")
                page.pages.set("3")
                page.page_wait_min.set("0.25")
                page.page_wait_max.set("1.75")
                page.start()
                responsive = []
                self.root.after(0, lambda: responsive.append(True))
                self.pump_until(lambda: all(event.is_set() for event in started.values()))
                self.assertTrue(responsive)
                self.assertTrue(page.busy)
                setup_calls = [item for item in observed if item[1].setup_only]
                worker_calls = [item for item in observed if not item[1].setup_only]
                self.assertEqual([key[0] for key, _args, _options in setup_calls].count("chrome"), 1)
                self.assertEqual([key[0] for key, _args, _options in setup_calls].count("edge"), 1)
                self.assertEqual(len(worker_calls), sum(per_browser.values()))
                self.assertEqual(len({options["env"][collector.DESKTOP_OWNER_TOKEN_ENV] for _key, _args, options in worker_calls}), len(worker_calls))
                expected = [spec["combination"] for spec in chosen]
                seen = []
                for (browser, shard), args, options in worker_calls:
                    self.assertFalse(args.setup_browser)
                    self.assertEqual((args.page_wait_min, args.page_wait_max), (0.25, 1.75))
                    self.assertEqual(args.total_shards, workers * 2)
                    self.assertEqual(args.root, run_output)
                    full = collector.build_specs(collector.load_config(args.config), args.batch, args.limit, args.cities)
                    self.assertEqual([spec["combination"] for spec in full], expected)
                    assigned = collector.shard_specs(full, shard, args.total_shards)
                    seen.extend(spec["combination"] for spec in assigned)
                    self.assertEqual(shard % 2, 0 if browser == "chrome" else 1)
                    self.assertEqual(args.cdp_port, 9222 if browser == "chrome" else 9223)
                    self.assertTrue(Path(options["env"][ui.GATE_STOP_FILE_ENV]).parent.is_dir())
                self.assertEqual(len(seen), len(set(seen)))
                self.assertEqual(set(seen), set(expected))
                for widget in (page.page_wait_min_entry, page.page_wait_max_entry, page.browser_workers_spin):
                    self.assertTrue(widget.instate(["disabled"]))
                for process in processes.values():
                    process.finish(0, "BATCH_DONE\n")
                self.pump_until(lambda: not page.busy)
                self.assertTrue(all(not args.root.exists() for _key, args, _options in setup_calls))
                self.assertTrue(all(not Path(options["env"][ui.GATE_STOP_FILE_ENV]).exists() for _key, _args, options in worker_calls))
                self.assertEqual(len(page.slot_table.get_children()), 2 * workers + 2)
                page.destroy()
                self.pages.remove(page)

    def test_single_browser_three_targets_prepare_once_and_empty_shards_stay_idle(self):
        first = FakeProcess(f"RUN {self.specs[0]['combination']} (now), pages=10\n[1/10 页] 本页1条\n")
        second = FakeProcess(f"RUN {self.specs[1]['combination']} (now), pages=10\n[2/10 页] 本页1条\n")
        observed, _processes = self.attach_slots({("chrome", 0): first, ("chrome", 1): second})
        page = self.make_page(browser_workers="3")
        page.limit.set("2")
        page.start()
        self.pump_until(lambda: len(observed) == 3 and "2/10" in page._lane_states["chrome"]["slots"]["2"]["position"])
        self.assertTrue(all(args.browser == "chrome" for _key, args, _options in observed))
        self.assertEqual(sum(args.setup_only for _key, args, _options in observed), 1)
        self.assertEqual(page._lane_states["chrome"]["slots"]["3"]["state"], "无分配组合")
        self.assertTrue(page.edge_continue_button.instate(["disabled"]))
        page.stop()
        self.pump_until(lambda: not page.busy)

    def test_one_slot_login_does_not_gate_collection_but_later_verification_bounds_resume(self):
        process = FakeProcess("登录完成后按 Enter 开始采集: ")
        entries = []

        def on_input(_value):
            entries.append(True)
            if len(entries) == 1:
                process.stdout.feed(f"\nRUN {self.specs[0]['combination']} (now), pages=10\n[1/10 页] 本页1条\n")
            else:
                process.finish(20, "\nBROWSER_GATE_PAUSED\n")
        process.on_input = on_input
        self.attach_fake(process)
        page = self.make_page()
        page.start()
        self.pump_until(lambda: page._lane_states["chrome"]["waiting"])
        gate = page._lanes["chrome"].gate_file
        self.assertFalse(gate.exists(), "initial browser login is not a security gate for a running combination")
        page.continue_after_gate("chrome")
        self.pump_until(lambda: "/10" in page.position_text.get())
        process.stdout.feed("验证完成后按 Enter 继续，q 退出: ")
        self.pump_until(lambda: page._lane_states["chrome"]["waiting"])
        self.assertTrue(gate.is_file())
        page.continue_after_gate("chrome")
        self.pump_until(lambda: not page.busy)
        self.assertEqual(process.stdin.writes, ["\n", "\n"])
        self.assertIn("未全部完成", page.status_text.get())
        self.assertFalse(gate.exists())

    def test_browser_preparation_is_once_per_side_and_only_readies_its_own_targets(self):
        processes = {(browser, shard): FakeProcess(f"RUN {self.specs[shard]['combination']} (now), pages=10\n")
                     for browser, shards in (("chrome", (0, 2, 4)), ("edge", (1, 3, 5))) for shard in shards}
        edge_setup = FakeProcess("登录完成后按 Enter 开始采集: ")
        edge_setup.on_input = lambda _value: edge_setup.finish(0, "\nprepared\n")
        processes[("edge", "setup")] = edge_setup
        observed, _all = self.attach_slots(processes)
        page = self.make_page(browser_mode="dual", browser_workers="3")
        page.start()
        self.pump_until(lambda: page._lane_states["edge"]["waiting"] and sum(not args.setup_only for _key, args, _opts in observed) == 3)
        self.assertEqual({args.browser for _key, args, _opts in observed if not args.setup_only}, {"chrome"})
        page.continue_after_gate("chrome")
        self.assertEqual(edge_setup.stdin.writes, [])
        page.continue_after_gate("edge")
        self.pump_until(lambda: sum(not args.setup_only for _key, args, _opts in observed) == 6)
        self.assertEqual(edge_setup.stdin.writes, ["\n"])
        self.assertEqual(sum(args.setup_only for _key, args, _opts in observed), 2)
        page.stop()
        self.pump_until(lambda: not page.busy)

    def test_same_browser_continue_confirms_only_current_waiters_and_preserves_gate_boundary(self):
        processes = {(browser, shard): FakeProcess(f"RUN {self.specs[shard]['combination']} (now), pages=10\n[1/10 页] 本页1条\n")
                     for browser, shards in (("chrome", (0, 2, 4)), ("edge", (1, 3, 5))) for shard in shards}
        observed, _all = self.attach_slots(processes)
        page = self.make_page(browser_mode="dual", browser_workers="3")
        page.start()
        self.pump_until(lambda: len(observed) == 8 and all(slot.runner.result for lane in page._lanes.values() for slot in lane.slots))
        for shard in (0, 2):
            process = processes[("chrome", shard)]
            process.on_input = lambda _value, process=process: process.finish(0, "\nBROWSER_GATE_PAUSED\n")
            process.stdout.feed("验证完成后按 Enter 继续，q 退出: ")
        self.pump_until(lambda: all(page._lane_states["chrome"]["slots"][str(slot)]["waiting"] for slot in (1, 2)))
        gate = page._lanes["chrome"].gate_file
        self.assertTrue(gate.is_file())
        self.assertFalse(page._lanes["edge"].gate_file.exists())
        page.continue_after_gate("chrome")
        page.continue_after_gate("chrome")
        self.pump_until(lambda: all(processes[("chrome", shard)].poll() == 0 for shard in (0, 2)))
        for shard in (0, 2):
            self.assertEqual(processes[("chrome", shard)].stdin.writes, ["\n"])
        for key, process in processes.items():
            if key not in (("chrome", 0), ("chrome", 2)):
                self.assertEqual(process.stdin.writes, [])
                self.assertEqual(process.terminated, 0)
        self.assertTrue(gate.exists(), "manual confirmation does not erase the browser's bounded-exit marker")
        last = processes[("chrome", 4)]
        last.on_input = lambda _value: last.finish(0, "\nBROWSER_GATE_PAUSED\n")
        last.stdout.feed("验证完成后按 Enter 继续，q 退出: ")
        self.pump_until(lambda: page._lane_states["chrome"]["slots"]["3"]["waiting"])
        self.assertEqual(last.stdin.writes, [], "a later gate requires another explicit confirmation")
        page.continue_after_gate("edge")
        self.assertEqual(last.stdin.writes, [])
        page.continue_after_gate("chrome")
        self.pump_until(lambda: last.poll() == 0)
        self.assertEqual(last.stdin.writes, ["\n"])
        self.assertTrue(page.busy)
        for shard in (1, 3, 5):
            processes[("edge", shard)].finish(0, "BATCH_DONE\n")
        self.pump_until(lambda: not page.busy)
        self.assertFalse(gate.exists())
        self.assertIn("未全部完成", page.status_text.get())

    def test_multislot_failed_browser_preparation_never_launches_its_targets_or_stops_peer(self):
        failed_setup = FakeProcess("offline browser preparation failed\n")
        failed_setup.finish(7)
        processes = {("edge", "setup"): failed_setup}
        for shard in (0, 2, 4):
            processes[("chrome", shard)] = FakeProcess(f"RUN {self.specs[shard]['combination']} (now), pages=10\n")
        observed, _all = self.attach_slots(processes)
        page = self.make_page(browser_mode="dual", browser_workers="3")
        page.start()
        self.pump_until(lambda: len(observed) == 5 and all(
            item["state"] == "浏览器未就绪，未启动" for key, item in page._lane_states["edge"]["slots"].items() if key != "setup"))
        self.assertFalse(any(args.browser == "edge" and not args.setup_only for _key, args, _options in observed))
        self.assertTrue(page.busy)
        for key, process in processes.items():
            if key[0] == "chrome":
                self.assertIsNone(process.poll())
                self.assertEqual(process.terminated, 0)
                process.finish(0, "BATCH_DONE\n")
        self.pump_until(lambda: not page.busy)
        self.assertIn("未全部完成", page.status_text.get())

    def test_dead_slot_releases_real_owned_interpreter_lock_without_stopping_other_slots(self):
        first = FakeProcess()
        peer = FakeProcess(f"RUN {self.specs[1]['combination']} (now), pages=10\n")
        third = FakeProcess(f"RUN {self.specs[2]['combination']} (now), pages=10\n")
        lock_created, peer_saved = threading.Event(), threading.Event()
        real_owner = 2_000_001

        def acquire_then_publish(args, key, options):
            if args.setup_only:
                return
            if key == ("chrome", 0):
                owner_token = options["env"][collector.DESKTOP_OWNER_TOKEN_ENV]
                (args.root / "collection_manifest.json.lock").write_text(f"pid={real_owner}\n", encoding="utf-8")
                first.stdout.feed(f"{collector.DESKTOP_OWNER_PREFIX} token={owner_token} pid={real_owner}\n"
                                  f"RUN {self.specs[0]['combination']} (now), pages=10\n")
                lock_created.set()
            elif key == ("chrome", 1):
                self.assertTrue(lock_created.wait(2))
                first.finish(20)
                collector.persist_manifest_record(args.root / "collection_manifest.json",
                                                  {**self.specs[1], "status": "running"},
                                                  expected_specs=self.specs, pages=10, root=args.root)
                peer_saved.set()
        observed, _all = self.attach_slots({("chrome", 0): first, ("chrome", 1): peer, ("chrome", 2): third}, acquire_then_publish)
        page = self.make_page(browser_workers="3")
        with patch.object(collector, "MANIFEST_LOCK_TIMEOUT", 2):
            page.start()
            self.pump_until(lambda: peer_saved.is_set() and len(observed) == 4)
            self.assertEqual(page._lanes["chrome"].slots[0].runner.result.collector_pid, real_owner)
            self.assertTrue(page.busy)
            self.assertEqual((peer.terminated, third.terminated), (0, 0))
            page.stop()
            self.pump_until(lambda: not page.busy)

    def test_multislot_pause_preserves_each_active_combination_and_owned_setup_tree(self):
        active = self.specs[:6]
        done = {**self.specs[10], "status": "success", "job_count": 7, "finished_at": "2020-01-01"}
        self.write_manifest([done])
        processes = {}
        raw_before = {}
        for shard, spec in enumerate(active):
            raw = self.output / "raw" / spec["track"] / spec["city_id"] / f"{spec['combination']}.json"
            write_json(raw, {"jobs": [{"id": f"partial-{shard}"}]})
            raw_before[raw] = raw.read_bytes()
            processes[("chrome" if shard % 2 == 0 else "edge", shard)] = FakeProcess(
                f"RUN {spec['combination']} (now), pages=10\n[{shard + 1}/10 页] 本页1条\n")
        observed, all_processes = self.attach_slots(processes)
        page = self.make_page(browser_mode="dual", browser_workers="3")
        page.start()
        self.pump_until(lambda: len(observed) == 8 and all(
            "/10" in item["position"] for state in page._lane_states.values()
            for key, item in state["slots"].items() if key != "setup"))
        lock_path = self.output / "collection_manifest.json.lock"
        lock_path.write_text(f"pid={processes[('edge', 5)].pid}\n", encoding="utf-8")
        page.pause()
        self.pump_until(lambda: not page.busy)
        manifest = collector.read_json(self.output / "collection_manifest.json")
        self.assertEqual(manifest["expected_combinations"], [spec["combination"] for spec in self.specs])
        for index, spec in enumerate(active):
            record = collector.record_for(manifest, spec["combination"])
            self.assertEqual((record["status"], record["pages_completed"], record["desktop_interruption"]), ("paused", index + 1, "paused"))
            self.assertTrue(Path(record["checkpoint"]).is_file())
        self.assertEqual(collector.record_for(manifest, done["combination"]), done)
        self.assertEqual(page._snapshot["counts"]["paused"], 6)
        for path, before in raw_before.items():
            self.assertEqual(path.read_bytes(), before)
        self.assertFalse(lock_path.exists())
        if sys.platform == "win32":
            trees = [tree for tree in FakeTree.instances if tree.child in all_processes.values()]
            self.assertEqual(len(trees), 8)
            self.assertTrue(all(tree.closed and tree.preserved is False for tree in trees))

    def test_multislot_partial_launch_failure_leaves_other_targets_running(self):
        processes = {(browser, shard): FakeProcess(f"RUN {self.specs[shard]['combination']} (now), pages=10\n")
                     for browser, shards in (("chrome", (0, 2, 4)), ("edge", (1, 3, 5))) for shard in shards}
        processes[("edge", 3)] = OSError("offline slot launch failure")
        observed, _all = self.attach_slots(processes)
        page = self.make_page(browser_mode="dual", browser_workers="3")
        page.start()
        self.pump_until(lambda: len(observed) == 8 and page._lane_states["edge"]["slots"]["2"]["state"] == "异常退出")
        self.assertTrue(page.busy)
        for process in processes.values():
            if isinstance(process, FakeProcess):
                self.assertIsNone(process.poll())
                self.assertEqual(process.terminated, 0)
        page.stop()
        self.pump_until(lambda: not page.busy)
        self.assertIn("offline slot launch failure", page.status_text.get())

    def test_multislot_close_remains_nonblocking_and_waits_for_every_owned_target(self):
        processes = {(browser, shard): FakeProcess(f"RUN {self.specs[shard]['combination']} (now), pages=10\n")
                     for browser, shards in (("chrome", (0, 2, 4)), ("edge", (1, 3, 5))) for shard in shards}
        for process in processes.values():
            process.stop_release = threading.Event()
            self.releases.append(process.stop_release)
        observed, _all = self.attach_slots(processes)
        page = self.make_page(browser_mode="dual", browser_workers="3")
        page.start()
        self.pump_until(lambda: len(observed) == 8)
        began = time.monotonic()
        page.request_close()
        self.assertLess(time.monotonic() - began, 0.2)
        self.assertFalse(page.shutdown_complete)
        processes[("chrome", 0)].stop_release.set()
        self.pump_until(lambda: processes[("chrome", 0)].poll() is not None)
        self.assertFalse(page.shutdown_complete)
        for process in processes.values():
            process.stop_release.set()
        self.pump_until(lambda: page.shutdown_complete)
        self.assertTrue(all(process.terminated for process in processes.values()))
        self.assertFalse(page.busy)
        self.assertEqual(self.errors, [])

    @unittest.skipUnless(sys.platform == "win32", "Windows owned browser jobs")
    def test_multislot_final_progress_cancellation_stops_preparation_and_all_target_trees(self):
        processes = {}
        for browser, shards in (("chrome", (0, 2, 4)), ("edge", (1, 3, 5))):
            for shard in shards:
                process = FakeProcess("BATCH_DONE\n")
                process.finish(0)
                processes[(browser, shard)] = process
        observed, all_processes = self.attach_slots(processes)
        page = self.make_page(browser_mode="dual", browser_workers="3")
        original_read = ui.read_collection_progress
        cancelled = threading.Event()

        def cancel_at_end(root):
            if len(observed) == 8 and all(runner.result is not None and runner.command_complete
                                         for lane in page._lanes.values() for runner in lane.all_runners):
                page._control.stop()
                cancelled.set()
            return original_read(root)
        with patch.object(ui, "read_collection_progress", side_effect=cancel_at_end):
            page.start()
            self.pump_until(lambda: not page.busy)
        self.assertTrue(cancelled.is_set())
        trees = [tree for tree in FakeTree.instances if tree.child in all_processes.values()]
        self.assertEqual(len(trees), 8)
        self.assertTrue(all(tree.closed and tree.preserved is False for tree in trees))
        self.assertIn("已停止", page.status_text.get())

    def test_maximum_slot_count_keeps_runtime_controls_and_scrollable_details_visible_at_1024(self):
        header = ttk.Frame(self.root, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        processes = {("chrome", 0): FakeProcess(f"RUN {self.specs[0]['combination']} (now), pages=10\n[1/10 页] 本页1条\n"),
                     ("edge", 1): FakeProcess(f"RUN {self.specs[1]['combination']} (now), pages=10\n[1/10 页] 本页1条\n")}
        observed, _all = self.attach_slots(processes)
        page = self.make_page(browser_mode="dual", browser_workers="8")
        page.limit.set("2")
        page.start()
        self.pump_until(lambda: len(observed) == 4 and len(page.slot_table.get_children()) == 18)
        self.root.geometry("1024x760")
        self.root.deiconify()
        for theme in ("dark", "light"):
            configure_styles(self.root, palette_for(theme))
            page.apply_theme(palette_for(theme))
            for tab, target in ((page.slot_frame, page.slot_table), (page.log_frame, page.log_view), (page.plan_frame, page.plan_table)):
                page.notebook.select(tab)
                self.root.update()
                for widget in (page.start_button, page.prepare_browser_button, page.pause_button, page.stop_button,
                               page.continue_button, page.edge_continue_button, page.page_wait_min_entry,
                               page.page_wait_max_entry, page.browser_workers_spin, page.browser_mode_box, target):
                    with self.subTest(theme=theme, tab=str(tab), widget=str(widget)):
                        x = widget.winfo_rootx() - self.root.winfo_rootx()
                        y = widget.winfo_rooty() - self.root.winfo_rooty()
                        self.assertGreaterEqual(x, 0)
                        self.assertGreaterEqual(y, 0)
                        self.assertLessEqual(x + widget.winfo_width(), 1024)
                        self.assertLessEqual(y + widget.winfo_height(), 760)
                self.assertGreater(target.winfo_height(), 55)
        self.assertIn("最多 8 槽", page.position_text.get())
        self.assertEqual(page.position_text.get().count("\n"), 1)
        page.stop()
        self.pump_until(lambda: not page.busy)

    def test_dual_lanes_really_overlap_and_share_exact_checked_then_limited_scope(self):
        first = FakeProcess(f"RUN {self.specs[2]['combination']} (now), pages=3\n[1/3 页] 本页1条\n")
        second = FakeProcess(f"RUN {self.specs[3]['combination']} (now), pages=3\n[2/3 页] 本页1条\n")
        barrier = threading.Barrier(2)
        both_launched = threading.Event()
        launch_threads = set()

        def overlap(_args):
            launch_threads.add(threading.get_ident())
            # The first launch cannot return until the second worker reaches
            # Popen too: sequential command construction cannot satisfy this.
            barrier.wait(3)
            both_launched.set()
        observed = self.attach_browsers({"chrome": first, "edge": second}, overlap)
        page = self.make_page(browser_mode="dual")
        self.pump_until(lambda: len(page._plan_rows) == 15)
        page.toggle_combination(self.specs[0]["combination"])
        page.toggle_combination(self.specs[1]["combination"])
        page.limit.set("5")
        page.pages.set("3")
        page.start()
        responsive = []
        self.root.after(0, lambda: responsive.append(True))
        self.pump_until(lambda: both_launched.is_set() and "2/3" in page.position_text.get())
        self.assertEqual(len(launch_threads), 2)
        self.assertTrue(responsive)
        self.assertTrue(page.busy)
        for spec in self.specs[2:4]:
            self.assertIn(spec["combination"], page.position_text.get())
        self.assertEqual(observed["chrome"].root, observed["edge"].root)
        self.assertEqual(observed["chrome"].config, observed["edge"].config)
        self.assertEqual((observed["chrome"].cdp_port, observed["edge"].cdp_port), (9222, 9223))
        self.assertNotEqual(observed["chrome"].profile_dir, observed["edge"].profile_dir)
        scopes = {}
        for browser, args in observed.items():
            specs = collector.build_specs(collector.load_config(args.config), args.batch, args.limit, args.cities)
            scopes[browser] = [spec["combination"] for spec in collector.shard_specs(specs, args.shard, args.total_shards)]
            self.assertEqual(args.total_shards, 2)
            self.assertFalse(args.setup_only)
        expected = [spec["combination"] for spec in self.specs[2:7]]
        self.assertEqual(scopes["chrome"], expected[::2])
        self.assertEqual(scopes["edge"], expected[1::2])
        self.assertFalse(set(scopes["chrome"]) & set(scopes["edge"]))
        self.assertEqual(set(scopes["chrome"]) | set(scopes["edge"]), set(expected))
        self.assertTrue(page.browser_mode_box.instate(["disabled"]))
        first.finish(0, "BATCH_DONE\n")
        self.pump_until(lambda: page._lane_states["chrome"]["state"] == "已结束")
        self.assertTrue(page.busy, "a completed side does not release the host interlock")
        if sys.platform == "win32":
            self.assertIsNotNone(page._lanes["chrome"].runner.tree)
            self.assertFalse(page._lanes["chrome"].runner.tree.closed)
        second.finish(0, "BATCH_DONE\n")
        self.pump_until(lambda: not page.busy)
        before = page.position_text.get()
        page.notebook.select(page.log_frame)
        page.apply_theme(palette_for("dark"))
        self.assertEqual(page.position_text.get(), before)
        for label in ("[Chrome]", "[Edge]"):
            self.assertIn(label, page.log_view.get("1.0", "end"))
            self.assertIn(label, (self.output / "collection-desktop.log").read_text(encoding="utf-8"))
        self.assertEqual(self.errors, [])

    def test_dual_continue_buttons_only_confirm_their_current_waiting_side(self):
        chrome = FakeProcess("登录完成后按 Enter 开始采集: ")
        edge = FakeProcess(f"RUN {self.specs[1]['combination']} (now), pages=10\n[1/10 页] 本页1条\n")
        chrome.on_input = lambda _value: chrome.finish(0, "\nBATCH_DONE\n")
        edge.on_input = lambda _value: edge.finish(0, "\nBATCH_DONE\n")
        self.attach_browsers({"chrome": chrome, "edge": edge})
        page = self.make_page(browser_mode="dual")
        page.start()
        self.pump_until(lambda: page._lane_states["chrome"]["waiting"] and "1/10" in page.position_text.get())
        self.assertFalse(page.continue_button.instate(["disabled"]))
        self.assertTrue(page.edge_continue_button.instate(["disabled"]))
        page.continue_after_gate("edge")
        self.assertEqual(edge.stdin.writes, [])
        page.continue_after_gate("chrome")
        page.continue_after_gate("chrome")
        self.pump_until(lambda: chrome.poll() == 0 and page._lane_states["chrome"]["state"] == "已结束")
        self.assertEqual(chrome.stdin.writes, ["\n"])
        self.assertEqual(edge.stdin.writes, [])
        self.assertEqual(edge.terminated, 0)
        self.assertTrue(page.busy)
        edge.stdout.feed("验证完成后按 Enter 继续，q 退出: ")
        self.pump_until(lambda: page._lane_states["edge"]["waiting"])
        self.assertFalse(page.edge_continue_button.instate(["disabled"]))
        self.assertTrue(page.continue_button.instate(["disabled"]))
        page.continue_after_gate("chrome")
        page.continue_after_gate("edge")
        self.pump_until(lambda: not page.busy)
        self.assertEqual(edge.stdin.writes, ["\n"])
        self.assertEqual(chrome.stdin.writes, ["\n"])

    def test_dual_setup_prepares_both_sides_in_removed_temporary_root(self):
        chrome = FakeProcess("登录完成后按 Enter 开始采集: ")
        edge = FakeProcess("登录完成后按 Enter 开始采集: ")
        for process in (chrome, edge):
            process.on_input = lambda _value, process=process: process.finish(0, "\nbrowser prepared\n")
        observed = self.attach_browsers({"chrome": chrome, "edge": edge})
        page = self.make_page(browser_mode="dual")
        original_config = self.config_path.read_bytes()
        page.prepare_browser()
        self.pump_until(lambda: all(page._lane_states[browser]["waiting"] for browser in ("chrome", "edge")))
        self.assertEqual(len(observed), 2)
        for args in observed.values():
            self.assertTrue(args.setup_only)
            self.assertTrue(args.config.is_file())
            self.assertNotEqual(args.root, self.output)
        page.continue_after_gate("chrome")
        self.pump_until(lambda: chrome.poll() == 0)
        self.assertEqual(edge.stdin.writes, [])
        self.assertTrue(page.busy)
        page.continue_after_gate("edge")
        self.pump_until(lambda: not page.busy)
        self.assertTrue(all(not args.config.exists() and not args.root.exists() for args in observed.values()))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertEqual(page.config_path, self.config_path)

    def test_dual_single_group_leaves_edge_idle_and_empty_scope_launches_neither(self):
        chrome = FakeProcess("BATCH_DONE\n")
        chrome.finish(0)
        observed = self.attach_browsers({"chrome": chrome})
        page = self.make_page(browser_mode="dual")
        page.limit.set("1")
        page.start()
        self.pump_until(lambda: not page.busy)
        self.assertEqual(set(observed), {"chrome"})
        self.assertEqual(page._lane_states["edge"]["state"], "无分配组合")
        self.pump_until(lambda: len(page._visible_plan_rows) == 15)
        page.select_visible_combinations(False)
        self.popen.reset_mock()
        page.start()
        self.pump_until(lambda: not page.busy)
        self.popen.assert_not_called()
        self.assertIn("至少勾选 1 组", page.status_text.get())

    def test_partial_browser_launch_failure_does_not_stop_the_running_side(self):
        chrome = FakeProcess(f"RUN {self.specs[0]['combination']} (now), pages=10\n[2/10 页] 本页1条\n")
        self.attach_browsers({"chrome": chrome, "edge": OSError("offline edge launch failed")})
        page = self.make_page(browser_mode="dual")
        page.start()
        self.pump_until(lambda: page._lane_states["edge"]["state"] == "异常退出" and "2/10" in page.position_text.get())
        self.assertTrue(page.busy)
        self.assertEqual(chrome.terminated, 0)
        self.assertIsNone(chrome.poll())
        page.stop()
        self.pump_until(lambda: not page.busy)
        self.assertGreater(chrome.terminated, 0)
        self.assertIn("Edge", page.status_text.get())
        self.assertIn("offline edge launch failed", page.log_view.get("1.0", "end"))

    def test_dead_side_releases_its_lock_while_peer_is_still_collecting(self):
        chrome = FakeProcess(f"RUN {self.specs[0]['combination']} (now), pages=10\n")
        edge = FakeProcess(f"RUN {self.specs[1]['combination']} (now), pages=10\n[1/10 页] 本页1条\n")
        lock_created, peer_persisted = threading.Event(), threading.Event()

        def persist_peer(args):
            if args.browser == "chrome":
                (args.root / "collection_manifest.json.lock").write_text(f"pid={chrome.pid}\n", encoding="utf-8")
                lock_created.set()
            else:
                self.assertTrue(lock_created.wait(2))
                chrome.finish(20)
                collector.persist_manifest_record(args.root / "collection_manifest.json",
                                                  {**self.specs[1], "status": "running"},
                                                  expected_specs=self.specs, pages=10, root=args.root)
                peer_persisted.set()
        self.attach_browsers({"chrome": chrome, "edge": edge}, persist_peer)
        page = self.make_page(browser_mode="dual")
        with patch.object(collector, "MANIFEST_LOCK_TIMEOUT", 2):
            page.start()
            self.pump_until(lambda: peer_persisted.is_set() and "1/10" in page.position_text.get())
            self.assertTrue(page.busy)
            self.assertIsNone(edge.poll())
            self.assertEqual(edge.terminated, 0)
            self.assertEqual(page._lane_states["chrome"]["state"], "未全部完成")
            manifest = collector.read_json(self.output / "collection_manifest.json")
            self.assertEqual(collector.record_for(manifest, self.specs[1]["combination"])["status"], "running")
            page.stop()
            self.pump_until(lambda: not page.busy)

    def test_dual_pause_preserves_both_checkpoints_full_scope_and_completed_record(self):
        active = self.specs[:2]
        done = {**self.specs[2], "status": "success", "job_count": 7, "finished_at": "2020-01-01"}
        self.write_manifest([done])
        raw_before = {}
        for index, spec in enumerate(active):
            raw = self.output / "raw" / spec["track"] / spec["city_id"] / f"{spec['combination']}.json"
            write_json(raw, {"jobs": [{"id": f"partial-{index}"}]})
            raw_before[raw] = raw.read_bytes()
        existing = self.output / "checkpoints" / f"{active[1]['combination']}.partial.json"
        write_json(existing, {"jobs": [{"id": "older-checkpoint"}]})
        chrome, edge = [FakeProcess(f"RUN {spec['combination']} (now), pages=10\n[{index + 2}/10 页] 本页1条\n")
                        for index, spec in enumerate(active)]
        self.attach_browsers({"chrome": chrome, "edge": edge})
        page = self.make_page(browser_mode="dual")
        page.start()
        self.pump_until(lambda: "2/10" in page.position_text.get() and "3/10" in page.position_text.get())
        # Forced termination can leave the peer's PID in the shared lock.
        lock_path = self.output / "collection_manifest.json.lock"
        lock_path.write_text(f"pid={edge.pid}\n", encoding="utf-8")
        page.pause()
        self.pump_until(lambda: not page.busy)
        manifest = collector.read_json(self.output / "collection_manifest.json")
        for index, spec in enumerate(active):
            record = collector.record_for(manifest, spec["combination"])
            self.assertEqual((record["status"], record["pages_completed"], record["desktop_interruption"]), ("paused", index + 2, "paused"))
            self.assertTrue(Path(record["checkpoint"]).is_file())
        self.assertEqual(collector.read_json(existing)["jobs"], [{"id": "partial-1"}, {"id": "older-checkpoint"}])
        self.assertEqual(collector.record_for(manifest, done["combination"]), done)
        self.assertEqual(manifest["expected_combinations"], [spec["combination"] for spec in self.specs])
        self.assertEqual(page._snapshot["counts"]["paused"], 2)
        for index, (path, before) in enumerate(raw_before.items()):
            if index == 0:
                self.assertEqual(path.read_bytes(), before)
            else:
                self.assertEqual(collector.read_json(path)["jobs"], collector.read_json(existing)["jobs"])
        self.assertTrue(all(process.terminated for process in (chrome, edge)))
        self.assertFalse(lock_path.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows owned browser jobs")
    def test_completed_lane_keeps_browser_tree_owned_until_peer_is_stopped(self):
        chrome = FakeProcess("BATCH_DONE\n")
        chrome.finish(0)
        edge = FakeProcess(f"RUN {self.specs[1]['combination']} (now), pages=10\n[1/10 页] 本页1条\n")
        self.attach_browsers({"chrome": chrome, "edge": edge})
        page = self.make_page(browser_mode="dual")
        page.start()
        self.pump_until(lambda: page._lane_states["chrome"]["state"] == "已结束" and "1/10" in page.position_text.get())
        chrome_tree = page._lanes["chrome"].runner.tree
        edge_tree = page._lanes["edge"].runner.tree
        self.assertIsNotNone(chrome_tree)
        self.assertFalse(chrome_tree.closed)
        page.stop()
        self.pump_until(lambda: not page.busy)
        self.assertTrue(chrome_tree.closed)
        self.assertFalse(chrome_tree.preserved)
        self.assertTrue(edge_tree.closed)
        self.assertFalse(edge_tree.preserved)

    @unittest.skipUnless(sys.platform == "win32", "Windows owned browser jobs")
    def test_dual_final_progress_cancellation_cannot_detach_either_browser(self):
        chrome, edge = FakeProcess("BATCH_DONE\n"), FakeProcess("BATCH_DONE\n")
        chrome.finish(0)
        edge.finish(0)
        observed = self.attach_browsers({"chrome": chrome, "edge": edge})
        page = self.make_page(browser_mode="dual")
        original_read = ui.read_collection_progress
        cancelled = threading.Event()

        def cancel_from_final_progress(root):
            if len(observed) == 2 and all(lane.runner.command_complete for lane in page._lanes.values()):
                page._control.stop()
                cancelled.set()
            return original_read(root)
        with patch.object(ui, "read_collection_progress", side_effect=cancel_from_final_progress):
            page.start()
            self.pump_until(lambda: not page.busy)
        self.assertTrue(cancelled.is_set())
        trees = [tree for tree in FakeTree.instances if tree.child in (chrome, edge)]
        self.assertEqual(len(trees), 2)
        self.assertTrue(all(tree.closed and tree.preserved is False for tree in trees))
        self.assertIn("已停止", page.status_text.get())

    def test_dual_close_is_nonblocking_and_waits_for_both_owned_trees(self):
        chrome, edge = FakeProcess("chrome running\n"), FakeProcess("edge running\n")
        for process in (chrome, edge):
            process.stop_release = threading.Event()
            self.releases.append(process.stop_release)
        observed = self.attach_browsers({"chrome": chrome, "edge": edge})
        page = self.make_page(browser_mode="dual")
        page.start()
        self.pump_until(lambda: len(observed) == 2)
        began = time.monotonic()
        page.request_close()
        self.assertLess(time.monotonic() - began, 0.2)
        self.assertFalse(page.shutdown_complete)
        chrome.stop_release.set()
        self.pump_until(lambda: chrome.poll() is not None)
        self.assertFalse(page.shutdown_complete)
        edge.stop_release.set()
        self.pump_until(lambda: page.shutdown_complete)
        self.assertFalse(page.busy)
        self.assertTrue(all(process.terminated for process in (chrome, edge)))
        self.assertEqual(self.errors, [])

    def test_plan_preview_is_default_tab_and_shows_real_columns_and_chinese_groups(self):
        write_json(self.config_path, collector.load_config(PROJECT_ROOT / "config" / "architecture_collection.json"))
        page = self.make_page()
        self.pump_until(lambda: len(page._plan_rows) == 672)
        self.assertEqual(page.notebook.select(), str(page.plan_frame))
        headings = [page.plan_table.heading(key)["text"] for key in page.plan_table["columns"]]
        self.assertEqual(headings, ["勾选", "序号", "检索分组", "城市", "关键词", "薪资", "经验", "学历", "页数", "本次计划"])
        self.assertEqual(tuple(page.track_box["values"]), ("全部分组", "当前求职", "成长参考"))
        self.assertIn("北京", page.city_box["values"])
        self.assertIn("11 个关键词", page.scope_help.get())
        self.assertIn("6 个关键词", page.scope_help.get())
        self.assertIn("不是岗位最终分层", page.scope_help.get())
        page.city.set("北京")
        self.pump_until(lambda: len(page._plan_rows) == 112)
        self.assertEqual(len(page.plan_table.get_children()), 112)
        self.assertTrue(all(row["city_label"] == "北京" for row in page._plan_rows))
        self.assertIsNone(page._snapshot["total"])
        self.assertFalse(self.output.exists())
        self.popen.assert_not_called()
        self.resolver.assert_not_called()

    def test_group_display_and_internal_track_stay_synchronized_both_directions(self):
        page = self.make_page()
        page.track.set("current")
        self.assertEqual(page.track_label.get(), "当前求职")
        self.pump_until(lambda: len(page._plan_rows) == 10)
        page.track_label.set("成长参考")
        self.assertEqual(page.track.get(), "target")
        self.pump_until(lambda: len(page._plan_rows) == 5)
        page.track.set("全部分组")
        self.assertEqual(page.track.get(), "all")
        self.assertEqual(page.track_label.get(), "全部分组")
        self.assertEqual(self.errors, [])

    def test_invalid_output_path_does_not_hide_the_read_only_search_plan(self):
        invalid = self.base / "a-file"
        invalid.write_text("untouched", encoding="utf-8")
        page = self.make_page()
        page.collection_root.set(str(invalid))
        self.pump_until(lambda: len(page._plan_rows) == 15 and "输出路径只读检查" in page.progress_text.get())
        self.assertIn("15", page.estimate_text.get())
        self.assertEqual(invalid.read_text(encoding="utf-8"), "untouched")
        self.popen.assert_not_called()

    def test_search_and_select_clear_only_visible_scope_preserve_hidden_choices(self):
        page = self.make_page()
        self.pump_until(lambda: len(page._plan_rows) == 15)
        all_ids = set(page._checked_combinations)
        page.plan_search.set("AI解决方案")
        self.assertEqual(len(page._visible_plan_rows), 5)
        visible = {row["combination"] for row in page._visible_plan_rows}
        page.select_visible_combinations(False)
        self.pump_until(lambda: sum(row["checked"] for row in page._plan_rows) == 10)
        self.assertEqual(page._checked_combinations, all_ids - visible)
        self.assertTrue(all(not row["checked"] for row in page._visible_plan_rows))
        page.select_visible_combinations(True)
        self.pump_until(lambda: all(row["checked"] for row in page._plan_rows))
        self.assertEqual(page._checked_combinations, all_ids)
        page.plan_search.set("上海")
        self.assertEqual(len(page._visible_plan_rows), 3)
        page.plan_search.set("")
        self.assertEqual(len(page.plan_table.get_children()), 15)
        self.assertFalse(self.output.exists())
        self.popen.assert_not_called()

    def test_editor_is_nonmodal_counts_draft_and_cancel_never_changes_or_saves_plan(self):
        page = self.make_page()
        self.pump_until(lambda: len(page._plan_rows) == 15)
        page.toggle_combination(self.specs[0]["combination"])
        checked = set(page._checked_combinations)
        before = self.config_path.read_bytes()
        editor = self.open_editor(page)
        self.assertIsNone(self.root.grab_current())
        ticks = []
        self.root.after(10, lambda: ticks.append(True))
        self.pump_until(lambda: ticks and "15 组" in editor.count_text.get())
        self.replace_keywords(editor, "current", "新关键词一\n新关键词二\n新关键词三")
        self.replace_keywords(editor, "target", "")
        for key, variable in editor.city_vars.items():
            variable.set("1" if key == "shanghai" else "0")
        self.pump_until(lambda: "3 组" in editor.count_text.get())
        self.assertEqual(page.config_path, self.config_path)
        self.assertEqual(page._checked_combinations, checked)
        editor.close()
        self.assertIsNone(page._editor)
        self.assertFalse(page._aux_pending)
        self.assertEqual(page.config_path, self.config_path)
        self.assertEqual(page._checked_combinations, checked)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse((self.base / "saved-plans").exists())
        self.assertFalse(self.output.exists())
        self.popen.assert_not_called()
        self.resolver.assert_not_called()
        self.assertEqual(self.errors, [])

    def test_editor_only_save_applies_unique_file_and_preserves_original_catalog(self):
        page = self.make_page()
        before = self.config_path.read_bytes()
        editor = self.open_editor(page)
        self.replace_keywords(editor, "current", "只在新方案出现的关键词")
        self.replace_keywords(editor, "target", "")
        for key, variable in editor.city_vars.items():
            variable.set("1" if key == "shanghai" else "0")
        self.pump_until(lambda: "1 组" in editor.count_text.get())
        editor.save()
        self.pump_until(lambda: page._editor is None and len(page._plan_rows) == 1)
        first_path = page.config_path
        self.assertEqual(first_path.parent, self.base / "saved-plans")
        self.assertEqual(self.config_path.read_bytes(), before)
        saved = collector.load_config(first_path)
        self.assertEqual([item["id"] for item in saved["current"]["keywords"][:2]], ["solution", "product"])
        actual = collector.build_specs(saved)
        self.assertEqual(len(actual), 1)
        self.assertTrue(actual[0]["keyword_id"].startswith("k_"))
        self.assertEqual(actual[0]["keyword"], "只在新方案出现的关键词")
        self.assertEqual(saved["degree"], {"id": "203", "name": "本科"})
        editor = self.open_editor(page)
        self.assertEqual(editor.keyword_texts["current"].get("1.0", "end-1c"), "只在新方案出现的关键词")
        editor.save()
        self.pump_until(lambda: page._editor is None and page.config_path != first_path)
        self.assertEqual(collector.load_config(first_path), collector.load_config(page.config_path))
        self.assertEqual(len(list((self.base / "saved-plans").glob("*.json"))), 2)
        self.assertFalse(self.output.exists())
        self.popen.assert_not_called()
        self.resolver.assert_not_called()

    def test_editor_validation_and_save_error_leave_the_active_plan_intact(self):
        page = self.make_page()
        editor = self.open_editor(page)
        for variable in editor.city_vars.values():
            variable.set("0")
        self.pump_until(lambda: "至少选择一个城市" in editor.count_text.get())
        editor.save()
        self.pump_until(lambda: "未另存或应用" in editor.status_text.get())
        self.assertFalse(editor._saving)
        self.assertEqual(page.config_path, self.config_path)
        self.assertFalse((self.base / "saved-plans").exists())
        editor.city_vars["shanghai"].set("1")
        with patch.object(ui.plans, "save_plan_config", side_effect=OSError("offline save denied")):
            editor.save()
            self.pump_until(lambda: "offline save denied" in editor.status_text.get())
        self.assertFalse(editor._saving)
        self.assertEqual(page.config_path, self.config_path)
        self.assertFalse(self.output.exists())
        self.popen.assert_not_called()

    def test_load_valid_plan_is_read_only_and_bad_or_unsupported_plan_never_replaces_it(self):
        page = self.make_page()
        self.pump_until(lambda: len(page._plan_rows) == 15)
        valid_path = self.base / "imported.json"
        selected = ui.plans.select_plan_combinations(self.config, [self.specs[4]["combination"]])
        write_json(valid_path, selected)
        before = valid_path.read_bytes()
        page.load_plan(valid_path)
        self.pump_until(lambda: not page._aux_pending and page.config_path == valid_path and len(page._plan_rows) == 15)
        self.pump_until(lambda: page._checked_combinations == {self.specs[4]["combination"]})
        self.assertEqual(valid_path.read_bytes(), before)
        self.assertEqual(len(list(self.base.glob("imported*.json"))), 1)
        previous = deepcopy(page._plan_config)
        checked = set(page._checked_combinations)
        for name, payload in (("bad", {"cities": []}),
                              ("not-bachelor", {**self.config, "degree": {"id": "202", "name": "大专"}})):
            invalid = self.base / f"{name}.json"
            write_json(invalid, payload)
            page.load_plan(invalid)
            self.pump_until(lambda: not page._aux_pending)
            self.assertIn("当前方案未改变", page.status_text.get())
            self.assertEqual(page.config_path, valid_path)
            self.assertEqual(page._plan_config, previous)
            self.assertEqual(page._checked_combinations, checked)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.base / "saved-plans").exists())
        self.popen.assert_not_called()
        self.resolver.assert_not_called()

    def test_malformed_config_cannot_break_page_construction_or_leave_preview_worker_stuck(self):
        malformed = small_config()
        malformed["degree"] = None
        write_json(self.config_path, malformed)
        page = self.make_page()
        self.pump_until(lambda: "只读预览" in page.estimate_text.get())
        self.assertFalse(page.busy)
        self.assertFalse(page._preview_active)
        self.assertEqual(self.errors, [])
        self.popen.assert_not_called()

    def test_config_missing_and_scraper_missing_are_only_explicit_start_errors(self):
        self.config_path.unlink()
        page = self.make_page()
        self.pump_until(lambda: "只读预览" in page.estimate_text.get())
        self.assertFalse(page.busy)
        page.start()
        self.pump_until(lambda: not page.busy)
        self.assertIn("未完成", page.status_text.get())
        self.popen.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_bad_explicit_scraper_is_not_silently_replaced_or_created(self):
        page = self.make_page(initial_scraper_root=str(self.base / "wrong-scraper"))
        page.start()
        self.pump_until(lambda: not page.busy)
        self.resolver.assert_not_called()
        self.popen.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertIn("boss_cdp_raw.py", page.status_text.get())

    def test_start_and_prepare_recheck_host_interlock_at_each_click(self):
        allowed = [True]
        page = self.make_page(can_start=lambda: allowed[0])
        allowed[0] = False
        page.start()
        page.prepare_browser()
        self.root.update()
        self.assertFalse(page.busy)
        self.assertIn("后处理正在工作", page.status_text.get())
        self.popen.assert_not_called()

    def test_disk_preview_and_start_validation_are_background_not_tk_work(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = ui.read_collection_progress
        threads = []

        def slow_read(root):
            threads.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return original(root)
        with patch.object(ui, "read_collection_progress", side_effect=slow_read):
            page = self.make_page()
            self.pump_until(entered.is_set)
            ticks = []
            self.root.after(20, lambda: ticks.append(True))
            self.pump_until(lambda: bool(ticks))
            self.assertTrue(all(thread != self.main_thread for thread in threads))
            self.assertFalse(page.busy)
            release.set()
            self.pump_until(lambda: page._snapshot is not None)

    def test_human_continue_without_newline_is_explicit_and_busy_callbacks_are_tk_only(self):
        changed = []
        handoff = Mock()
        spec = self.specs[0]
        process = FakeProcess("登录完成后按 Enter 开始采集: ")

        def on_enter(value):
            self.assertEqual(value, "\n")
            self.write_manifest([{**spec, "status": "success", "job_count": 2, "pages_completed": 1}])
            process.finish(0, f"\nRUN {spec['combination']} (now), pages=10\n[1/10 页] 本页2条\nDONE {spec['combination']} jobs=2\nBATCH_DONE\n")
        process.on_input = on_enter
        self.attach_fake(process)
        page = self.make_page(on_use_source=handoff,
                              on_busy_change=lambda value: changed.append((value, threading.get_ident())))
        page.start()
        self.assertTrue(page.busy)
        self.pump_until(lambda: page._waiting)
        self.assertEqual(process.stdin.writes, [])
        self.assertFalse(page.continue_button.instate(["disabled"]))
        page.continue_after_gate()
        self.pump_until(lambda: not page.busy)
        self.assertEqual(process.stdin.writes, ["\n"])
        self.assertEqual(changed, [(True, self.main_thread), (False, self.main_thread)])
        self.assertEqual(page._snapshot["counts"]["success"], 1)
        self.assertIn("后处理须手动", page.status_text.get())
        handoff.assert_not_called()
        self.executor.assert_not_called()

    def test_prepare_browser_command_cannot_fall_through_to_collection_or_review(self):
        process = FakeProcess("browser prepared\n")
        process.finish(0)
        self.attach_fake(process)
        page = self.make_page()
        page.prepare_browser()
        self.pump_until(lambda: not page.busy)
        command = self.popen.call_args.args[0]
        self.assertIn("--setup-only", command)
        self.assertIn("未执行采集或后处理", page.status_text.get())
        self.assertFalse((self.output / "raw").exists())
        self.assertFalse(self.output.exists(), "setup-only must not initialize even the real output root")
        self.assertEqual(page.config_path, self.config_path)
        self.executor.assert_not_called()

    def test_prepare_browser_legacy_cli_writes_are_isolated_in_a_removed_temporary_root(self):
        existing = self.output / "raw/current/old.json"
        write_json(existing, {"jobs": [{"id": "untouched"}]})
        self.write_manifest([{**self.specs[0], "status": "success", "job_count": 1}])
        log = self.output / "collection-desktop.log"
        log.write_text("existing source log", encoding="utf-8")
        before = {path.relative_to(self.output): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        observed = []

        def legacy_setup(command):
            args = collector.parse_args(command[3:])
            self.assertTrue(args.setup_only)
            self.assertNotEqual(args.root, self.output)
            self.assertNotIn(self.output, args.root.parents)
            self.assertTrue(args.config.is_file())
            write_json(args.root / "collection_manifest.json", {"records": []})
            observed.append((args.root, args.config))
        process = FakeProcess("browser prepared\n")
        process.finish(0)
        self.attach_fake(process, legacy_setup)
        page = self.make_page()
        page.prepare_browser()
        self.pump_until(lambda: not page.busy)
        self.assertEqual(len(observed), 1)
        self.assertTrue(all(not path.exists() for pair in observed for path in pair))
        after = {path.relative_to(self.output): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertFalse((self.output / "collection-plans").exists())
        self.assertEqual(page.config_path, self.config_path)
        self.assertIn("不写采集输出根", page.log_view.get("1.0", "end"))

    def test_explicit_start_uses_exact_checked_then_limited_snapshot_and_keeps_all_checkbox_choices(self):
        original = self.config_path.read_bytes()
        page = self.make_page()
        self.pump_until(lambda: len(page._plan_rows) == 15)
        page.city.set("上海")
        self.pump_until(lambda: len(page._plan_rows) == 3)
        page.toggle_combination(self.specs[0]["combination"])
        checked = set(page._checked_combinations)
        page.limit.set("1")
        page.pages.set("3")
        observed = []

        def inspect(command):
            args = collector.parse_args(command[3:])
            config = collector.load_config(args.config)
            specs = collector.build_specs(config, args.batch, args.limit, args.cities)
            observed.append((args, config, specs))
        process = FakeProcess("BATCH_DONE\n")
        process.finish(0)
        self.attach_fake(process, inspect)
        page.start()
        self.pump_until(lambda: not page.busy and observed)
        args, config, specs = observed[0]
        self.assertEqual([spec["combination"] for spec in specs], [self.specs[1]["combination"]])
        self.assertEqual(config["collection_selection"]["combinations"], [self.specs[1]["combination"]])
        self.assertEqual(args.config.parent, self.output / "collection-plans")
        self.assertEqual(page.config_path, args.config)
        self.assertEqual(args.pages, 3)
        self.assertEqual(len(config["current"]["keywords"]), 2)
        self.assertEqual(len(config["target"]["keywords"]), 1)
        self.assertEqual(page._checked_combinations, checked)
        self.assertEqual(len(list((self.output / "collection-plans").glob("*.json"))), 1)
        self.assertEqual(self.config_path.read_bytes(), original)
        content = (self.output / "collection-desktop.log").read_text(encoding="utf-8")
        self.assertIn(str(self.config_path), content)
        self.assertIn(str(args.config), content)
        self.assertNotIn("process_architecture", " ".join(self.popen.call_args.args[0]))

    def test_zero_checkbox_selection_rejects_start_before_discovery_or_any_source_write(self):
        page = self.make_page()
        self.pump_until(lambda: len(page._plan_rows) == 15)
        page.select_visible_combinations(False)
        page.start()
        self.pump_until(lambda: not page.busy)
        self.assertIn("至少勾选 1 组", page.status_text.get())
        self.assertFalse(self.output.exists())
        self.popen.assert_not_called()
        self.resolver.assert_not_called()

    def test_busy_locks_preview_checkboxes_editor_loading_and_scope_without_late_edits(self):
        process = FakeProcess("等待人工登录后按 Enter 开始采集: ")
        self.attach_fake(process)
        page = self.make_page()
        self.pump_until(lambda: len(page._plan_rows) == 15)
        checked = set(page._checked_combinations)
        page.start()
        self.pump_until(lambda: page._waiting)
        for widget in (page.edit_plan_button, page.load_plan_button, page.select_all_button,
                       page.clear_selection_button, page.plan_search_entry, page.plan_table):
            self.assertTrue(widget.instate(["disabled"]))
        self.assertEqual(str(page.track_box["state"]), "disabled")
        page.toggle_combination(self.specs[0]["combination"])
        page.select_visible_combinations(False)
        page.open_plan_editor()
        page.load_plan(self.base / "does-not-exist.json")
        self.assertEqual(page._checked_combinations, checked)
        self.assertIsNone(page._editor)
        self.assertEqual(self.popen.call_count, 1)
        page.stop()
        self.pump_until(lambda: not page.busy)
        self.assertFalse(page.edit_plan_button.instate(["disabled"]))
        self.assertFalse(page.plan_table.instate(["disabled"]))

    def test_imported_reused_combination_with_changed_query_is_rejected_before_snapshot_or_log(self):
        old = {**self.specs[0], "status": "success", "job_count": 7}
        self.write_manifest([old])
        before = (self.output / "collection_manifest.json").read_bytes()
        changed = deepcopy(self.config)
        changed["current"]["keywords"][0]["query"] = "手改同一 ID 的不同关键词"
        write_json(self.config_path, changed)
        page = self.make_page()
        page.start()
        self.pump_until(lambda: not page.busy)
        self.assertIn("复用了旧组合 ID", page.status_text.get())
        self.assertEqual((self.output / "collection_manifest.json").read_bytes(), before)
        self.assertFalse((self.output / "collection-plans").exists())
        self.assertFalse((self.output / "collection-desktop.log").exists())
        self.popen.assert_not_called()

    def test_successful_collector_exit_is_not_faked_manifest_completion(self):
        process = FakeProcess("BATCH_DONE\n")
        process.finish(0)
        self.attach_fake(process)
        page = self.make_page()
        page.start()
        self.pump_until(lambda: not page.busy)
        self.assertEqual(page._snapshot["manifest_state"], "missing")
        self.assertIsNone(page._snapshot["total"])
        self.assertEqual(page.progress_bar["value"], 0)
        self.assertIn("核对落盘清单", page.status_text.get())

    def test_invalid_existing_manifest_rejects_start_without_changing_any_source(self):
        path = self.output / "collection_manifest.json"
        path.parent.mkdir()
        path.write_text("{broken", encoding="utf-8")
        before = path.read_bytes()
        page = self.make_page()
        page.start()
        self.pump_until(lambda: not page.busy)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(path.with_suffix(".json.corrupt").exists())
        self.assertFalse((self.output / "collection-desktop.log").exists())
        self.popen.assert_not_called()

    def test_pause_persists_real_current_checkpoint_and_preserves_completed_combinations(self):
        active, done = self.specs[:2]
        done_record = {**done, "status": "success", "job_count": 8, "finished_at": "2020-01-01"}
        self.write_manifest([done_record])
        output = self.output / "raw" / active["track"] / active["city_id"] / f"{active['combination']}.json"
        write_json(output, {"jobs": [{"id": "published-partial"}]})
        raw_before = output.read_bytes()
        process = FakeProcess(f"RUN {active['combination']} (now), pages=10\n[2/10 页] 本页1条\n")
        self.attach_fake(process)
        page = self.make_page()
        page.start()
        self.pump_until(lambda: "2/10" in page.position_text.get())
        page.pause()
        self.pump_until(lambda: not page.busy)
        manifest = collector.read_json(self.output / "collection_manifest.json")
        record = collector.record_for(manifest, active["combination"])
        self.assertEqual((record["status"], record["pages_completed"], record["desktop_interruption"]), ("paused", 2, "paused"))
        self.assertEqual(Path(record["checkpoint"]).read_bytes(), raw_before)
        self.assertEqual(output.read_bytes(), raw_before)
        self.assertEqual(collector.record_for(manifest, done["combination"]), done_record)
        self.assertEqual(page._snapshot["counts"]["paused"], 1)

    def test_stop_retains_existing_checkpoint_and_next_start_uses_same_cli_resume(self):
        active = self.specs[0]
        checkpoint = self.output / "checkpoints" / f"{active['combination']}.partial.json"
        write_json(checkpoint, {"jobs": [{"id": "old-data"}]})
        before = checkpoint.read_bytes()
        self.write_manifest([])
        process = FakeProcess(f"RUN {active['combination']} (now), pages=10\n")
        self.attach_fake(process)
        page = self.make_page()
        page.city.set("上海")
        page.track.set("current")
        page.limit.set("1")
        page.pages.set("2")
        page.start()
        self.pump_until(lambda: active["combination"] in page.position_text.get())
        first_command = self.popen.call_args.args[0]
        page.stop()
        self.pump_until(lambda: not page.busy)
        self.assertEqual(checkpoint.read_bytes(), before)
        next_process = FakeProcess("SKIP already-completed (已完成)\nBATCH_DONE\n")
        next_process.finish(0)
        self.attach_fake(next_process)
        page.start()
        self.pump_until(lambda: not page.busy)
        next_command = self.popen.call_args.args[0]
        first_args = collector.parse_args(first_command[3:])
        next_args = collector.parse_args(next_command[3:])
        self.assertNotEqual(first_args.config, next_args.config, "each explicit start publishes a unique plan snapshot")
        self.assertEqual(collector.load_config(first_args.config), collector.load_config(next_args.config))
        first_args.config = next_args.config
        self.assertEqual(vars(first_args), vars(next_args))
        self.assertEqual(checkpoint.read_bytes(), before)

    def test_durable_collection_log_is_separate_redacted_and_appended_not_review_log(self):
        self.output.mkdir()
        path = self.output / "collection-desktop.log"
        path.write_text("existing collection log\n", encoding="utf-8")
        review = self.output / "task.log"
        review.write_text("untouched review log", encoding="utf-8")
        process = FakeProcess('api_key="PRIVATE_KEY"\nCookie: sid=PRIVATE_COOKIE\nAuthorization: Basic PRIVATE_AUTH\n')
        process.finish(1)
        self.attach_fake(process)
        page = self.make_page()
        page.start()
        self.pump_until(lambda: not page.busy)
        visible = page.log_view.get("1.0", "end") + path.read_text(encoding="utf-8")
        for secret in ("PRIVATE_KEY", "PRIVATE_COOKIE", "PRIVATE_AUTH"):
            self.assertNotIn(secret, visible)
        self.assertTrue(path.read_text(encoding="utf-8").startswith("existing collection log\n"))
        self.assertEqual(review.read_text(encoding="utf-8"), "untouched review log")

    def test_only_manual_handoff_calls_host_and_it_never_executes_postprocessing(self):
        (self.output / "raw/current").mkdir(parents=True)
        received = []
        page = self.make_page(on_use_source=lambda root: received.append((root, threading.get_ident())))
        page.refresh_progress()
        self.root.update()
        self.assertEqual(received, [])
        page.use_source()
        self.pump_until(lambda: bool(received))
        self.assertEqual(received, [(self.output.resolve(), self.main_thread)])
        self.popen.assert_not_called()
        self.executor.assert_not_called()

    def test_manual_handoff_rechecks_host_after_background_directory_read(self):
        (self.output / "raw").mkdir(parents=True)
        allowed = [True]
        callback = Mock()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = ui.validate_collection_root

        def slow_validate(value):
            if threading.current_thread().name == "collection-directory":
                entered.set()
                release.wait(3)
            return original(value)
        page = self.make_page(can_start=lambda: allowed[0], on_use_source=callback)
        with patch.object(ui, "validate_collection_root", side_effect=slow_validate):
            page.use_source()
            self.pump_until(entered.is_set)
            allowed[0] = False
            release.set()
            self.pump_until(lambda: not page._aux_pending)
        callback.assert_not_called()
        self.assertIn("未自动交接", page.status_text.get())

    def test_theme_switch_preserves_inputs_scope_and_does_not_reconfigure_host_theme(self):
        page = self.make_page()
        page.city.set("深圳")
        page.track.set("target")
        page.limit.set("7")
        page.pages.set("3")
        values = tuple(var.get() for var in (page.collection_root, page.scraper_root, page.city, page.track, page.limit, page.pages))
        with patch.object(ttk.Style, "theme_use", side_effect=AssertionError("host owns theme")), \
                patch.object(self.root, "option_add", side_effect=AssertionError("host owns options")):
            page.apply_theme(palette_for("dark"))
        self.assertEqual(tuple(var.get() for var in (page.collection_root, page.scraper_root, page.city, page.track, page.limit, page.pages)), values)
        self.assertEqual(page.log_view["background"], palette_for("dark")["log"])
        self.assertEqual(page.canvas["background"], palette_for("dark")["background"])
        self.assertEqual(self.errors, [])

    def test_tab_and_theme_switch_preserve_plan_checks_search_and_unsaved_editor_content(self):
        page = self.make_page()
        self.pump_until(lambda: len(page._plan_rows) == 15)
        page.toggle_combination(self.specs[0]["combination"])
        page.plan_search.set("AI产品")
        checked = set(page._checked_combinations)
        page.notebook.select(page.log_frame)
        page.apply_theme(palette_for("dark"))
        page.notebook.select(page.plan_frame)
        self.assertEqual(page._checked_combinations, checked)
        self.assertEqual(page.plan_search.get(), "AI产品")
        self.assertEqual(len(page._visible_plan_rows), 5)
        editor = self.open_editor(page)
        self.replace_keywords(editor, "current", "尚未保存的新输入\n另一行")
        editor.city_vars["shenzhen"].set("0")
        with patch.object(ttk.Style, "theme_use", side_effect=AssertionError("host owns theme")), \
                patch.object(self.root, "option_add", side_effect=AssertionError("host owns options")):
            page.apply_theme(palette_for("light"))
            page.apply_theme(palette_for("dark"))
        self.assertEqual(editor.keyword_texts["current"].get("1.0", "end-1c"), "尚未保存的新输入\n另一行")
        self.assertEqual(editor.city_vars["shenzhen"].get(), "0")
        self.assertEqual(editor.keyword_texts["current"]["background"], palette_for("dark")["log"])
        self.assertEqual(page._checked_combinations, checked)
        editor.close()
        self.assertFalse((self.base / "saved-plans").exists())
        self.assertEqual(self.errors, [])

    def test_closing_editor_and_page_cancels_tcl_callbacks_but_waits_for_inflight_draft_worker(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = ui.plans.update_plan_config
        threads = []

        def slow_count(*args, **kwargs):
            if threading.current_thread().name == "collection-plan-count":
                threads.append(threading.get_ident())
                entered.set()
                release.wait(3)
            return original(*args, **kwargs)
        with patch.object(ui.plans, "update_plan_config", side_effect=slow_count):
            page = self.make_page()
            editor = self.open_editor(page)
            self.pump_until(entered.is_set)
            variables = list(editor._variables)
            callbacks = set(editor._after_ids) | set(page._after_ids)
            started = time.monotonic()
            page.request_close()
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertFalse(page.shutdown_complete)
            self.assertIsNone(page._editor)
            self.assertFalse(editor._after_ids)
            self.assertTrue(all(variable._tk is None for variable in variables))
            self.assertFalse(callbacks.intersection(self.root.tk.call("after", "info")))
            self.assertTrue(all(thread != self.main_thread for thread in threads))
            release.set()
            self.pump_until(lambda: page.shutdown_complete)
        self.assertEqual(self.errors, [])
        self.assertFalse((self.base / "saved-plans").exists())
        self.popen.assert_not_called()

    def test_cancel_during_editor_save_validation_cannot_apply_or_publish_a_late_plan(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = ui.plans.update_plan_config

        def slow_save(*args, **kwargs):
            if threading.current_thread().name == "collection-plan-save":
                entered.set()
                release.wait(3)
            return original(*args, **kwargs)
        with patch.object(ui.plans, "update_plan_config", side_effect=slow_save):
            page = self.make_page()
            editor = self.open_editor(page)
            editor.save()
            self.pump_until(entered.is_set)
            editor.close()
            self.assertIsNone(page._editor)
            self.assertEqual(page.config_path, self.config_path)
            release.set()
            self.pump_until(lambda: page.shutdown_complete)
        self.assertEqual(page.config_path, self.config_path)
        self.assertFalse((self.base / "saved-plans").exists())
        self.assertFalse(self.output.exists())
        self.assertEqual(self.errors, [])

    def test_1024_by_760_keeps_actions_and_log_visible_below_host_header(self):
        header = ttk.Frame(self.root, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        chrome, edge = [FakeProcess(f"RUN {spec['combination']} (now), pages=10\n[2/10 页] 本页1条\n")
                        for spec in self.specs[:2]]
        self.attach_browsers({"chrome": chrome, "edge": edge})
        page = self.make_page(browser_mode="dual")
        page.start()
        self.pump_until(lambda: "2/10" in page.position_text.get() and page.config_path != self.config_path)
        self.root.geometry("1024x760")
        self.root.deiconify()
        self.assertEqual(page.notebook.select(), str(page.plan_frame))
        for theme in ("dark", "light"):
            configure_styles(self.root, palette_for(theme))
            page.apply_theme(palette_for(theme))
            page.notebook.select(page.log_frame)
            self.root.update()
            for widget in (page.start_button, page.prepare_browser_button, page.pause_button, page.stop_button,
                           page.continue_button, page.edge_continue_button, page.browser_mode_box,
                           page.refresh_button, page.open_root_button, page.use_source_button,
                           page.edit_plan_button, page.load_plan_button, page.log_view):
                with self.subTest(theme=theme, widget=str(widget)):
                    x = widget.winfo_rootx() - self.root.winfo_rootx()
                    y = widget.winfo_rooty() - self.root.winfo_rooty()
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertLessEqual(x + widget.winfo_width(), 1024)
                    self.assertLessEqual(y + widget.winfo_height(), 760)
            self.assertGreater(page.log_view.winfo_height(), 55)
            page.notebook.select(page.plan_frame)
            self.root.update()
            self.assertGreater(page.plan_table.winfo_height(), 55)
        page.stop()
        self.pump_until(lambda: not page.busy)

    def test_short_host_and_larger_windows_fonts_keep_controls_and_full_table_rows_visible(self):
        # Hosted CI desktops can cap the requested 760 px client height.
        # Exercise that restriction and a larger system-font/DPI request;
        # neither case may consume the detail tabs down to a heading only.
        # Tk scaling is shared by later Tk roots in this process, even after
        # this interpreter is destroyed. Restore it before window cleanup.
        self.addCleanup(self.root.tk.call, "tk", "scaling", self.root.tk.call("tk", "scaling"))
        default_font = tkfont.nametofont("TkDefaultFont", root=self.root)
        self.addCleanup(default_font.configure, **default_font.actual())
        self.root.tk.call("tk", "scaling", 1.5)
        default_font.configure(family="Segoe UI", size=9)
        header = ttk.Frame(self.root, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        processes = {(browser, index): FakeProcess(
            f"RUN {spec['combination']} (now), pages=10\n[2/10 页] 本页1条\n")
            for index, (browser, spec) in enumerate(zip(("chrome", "edge"), self.specs))}
        observed, _all = self.attach_slots(processes)
        page = self.make_page(browser_mode="dual", browser_workers="8")
        page.limit.set("2")
        page.start()
        self.pump_until(lambda: len(observed) == 4 and len(page.slot_table.get_children()) == 18)
        self.pump_until(lambda: bool(page.plan_table.get_children()))
        self.root.maxsize(1024, 720)
        self.root.geometry("1024x760")
        self.root.deiconify()
        for theme in ("dark", "light"):
            configure_styles(self.root, palette_for(theme))
            style = ttk.Style(self.root)
            host_paddings = {name: style.lookup(name, "padding") for name in (
                "TEntry", "TCombobox", "TSpinbox", "Small.TButton", "Primary.TButton", "TNotebook.Tab", "Treeview.Heading")}
            page.apply_theme(palette_for(theme))
            for tab, target in ((page.plan_frame, page.plan_table), (page.log_frame, page.log_view),
                                (page.slot_frame, page.slot_table)):
                page.notebook.select(tab)
                self.root.update()
                self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (1024, 720))
                self.assertGreater(target.winfo_height(), 55)
                for widget in (page.root_entry, page.scraper_entry, page.start_button, page.prepare_browser_button,
                               page.pause_button, page.stop_button, page.continue_button, page.edge_continue_button,
                               page.page_wait_min_entry, page.page_wait_max_entry, page.browser_workers_spin,
                               page.browser_mode_box, page.refresh_button, page.open_root_button, page.use_source_button,
                               page.edit_plan_button, page.load_plan_button, target):
                    with self.subTest(theme=theme, tab=str(tab), widget=str(widget)):
                        x = widget.winfo_rootx() - self.root.winfo_rootx()
                        y = widget.winfo_rooty() - self.root.winfo_rooty()
                        self.assertGreaterEqual(x, 0)
                        self.assertGreaterEqual(y, 64)
                        self.assertLessEqual(x + widget.winfo_width(), self.root.winfo_width())
                        self.assertLessEqual(y + widget.winfo_height(), self.root.winfo_height())
                if isinstance(target, ttk.Treeview):
                    bounds = target.bbox(target.get_children()[0])
                    self.assertTrue(bounds, "a complete first data row must remain visible below its heading")
                    self.assertLessEqual(bounds[1] + bounds[3], target.winfo_height())
            self.assertEqual(host_paddings, {name: style.lookup(name, "padding") for name in host_paddings})
            self.assertGreater(page.root_entry.winfo_width(), 120)
            self.assertGreater(page.scraper_entry.winfo_width(), 120)
        self.assertEqual(self.errors, [])
        page.stop()
        self.pump_until(lambda: not page.busy)

    def test_close_is_nonblocking_cancels_own_callbacks_and_waits_for_owned_child_exit(self):
        process = FakeProcess("RUN example (now), pages=10\n")
        release = threading.Event()
        process.stop_release = release
        self.releases.append(release)
        self.attach_fake(process)
        changed = []
        page = self.make_page(on_busy_change=lambda value: changed.append((value, threading.get_ident())))
        page.start()
        self.pump_until(lambda: self.popen.called)
        callbacks = set(page._after_ids)
        start = time.monotonic()
        page.request_close()
        self.assertLess(time.monotonic() - start, 0.2)
        self.assertFalse(page.shutdown_complete)
        self.assertTrue(page.winfo_exists())
        self.assertTrue(self.root.winfo_exists())
        self.assertFalse(callbacks.intersection(self.root.tk.call("after", "info")))
        page._emit("log", "late callback")
        page._drain_events()
        self.assertTrue(page._events.empty())
        release.set()
        self.pump_until(lambda: page.shutdown_complete)
        self.assertFalse(page.busy)
        self.assertEqual(changed, [(True, self.main_thread), (False, self.main_thread)])
        self.assertEqual(self.errors, [])

    def test_close_waits_for_read_only_worker_and_destroyed_callbacks_do_not_leak(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = ui.read_collection_progress

        def slow_read(root):
            entered.set()
            release.wait(3)
            return original(root)
        with patch.object(ui, "read_collection_progress", side_effect=slow_read):
            page = self.make_page()
            self.pump_until(entered.is_set)
            page.request_close()
            self.assertFalse(page.shutdown_complete)
            self.assertFalse(page._after_ids)
            page.pages.set("2")  # Traces have been removed; no new Tk callback.
            self.assertFalse(page._after_ids)
            release.set()
            self.pump_until(lambda: page.shutdown_complete)
            page.destroy()
            self.root.update()
        self.assertEqual(self.errors, [])

    def test_close_during_validation_never_launches_a_late_child_or_creates_output(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def slow_resolve(_value):
            entered.set()
            release.wait(3)
            return self.scraper
        self.resolver.side_effect = slow_resolve
        page = self.make_page()
        page.start()
        self.pump_until(entered.is_set)
        page.request_close()
        self.assertFalse(page.shutdown_complete)
        release.set()
        self.pump_until(lambda: page.shutdown_complete)
        self.popen.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.errors, [])

    def test_actual_destroy_releases_page_variables_on_main_thread_not_future_worker_gc(self):
        page = self.make_page()
        self.pump_until(lambda: page.shutdown_complete)
        variables = page._variables
        self.assertTrue(all(variable._tk is not None for variable in variables))
        page.request_close()
        self.assertEqual(page.pages.get(), "10", "host can still render before actual destruction")
        page.destroy()
        self.assertTrue(all(variable._tk is None for variable in variables))
        self.root.update()
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
