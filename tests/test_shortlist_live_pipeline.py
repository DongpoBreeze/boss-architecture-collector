"""Durable partial-list publication and stage-rule execution, using fake data."""

from unittest.mock import patch

from test_review_pipeline import PipelineCase

import review_pipeline as pipeline
from desktop_tasks import TaskStateError, job_id


class ShortlistLiveTests(PipelineCase):
    def populate(self, count):
        self.original = [{"job_id": f"synthetic-{number:05d}", "title": f"合成方案岗位 {number}",
                          "job_link": f"https://www.zhipin.com/job_detail/synthetic-{number:05d}.html",
                          "salary": "20-30K", "skills": ["系统集成"]}
                         for number in range(count)]
        self.write(self.source / "raw/current/shanghai/current__shanghai__aiarch__20_50__1_3.json",
                   {"city": "上海", "filters": {}, "jobs": self.original})
        self.task = self.create(track="current", sample_size=None)
        self.import_and_confirm()

    @staticmethod
    def score(job):
        keep = int(job_id(job).split("-")[-1]) % 3 != 0
        return {"list_pass": keep, "list_queue": "relevant" if keep else "excluded",
                "list_queue_reason": "合成保留" if keep else "合成剔除"}

    def test_six_thousand_jobs_publish_bounded_consistent_live_prefixes_before_finish(self):
        self.populate(6000)
        published = []

        def observe(kind, value):
            if kind != "progress":
                return
            info = value["snapshot"]["stages"]["shortlist"]
            path = self.task.run_directory("shortlist") / "prepared/live_shortlist.json"
            live = self.read(path)
            count = live["processed_count"]
            self.assertEqual(info["status"], "running")
            self.assertFalse(info["confirmed"])
            self.assertEqual(live["attempt"], info["attempt"])
            self.assertEqual(live["input_signature"], pipeline.shortlist_live_signature(info))
            self.assertEqual([job_id(job) for job in live["jobs"]], info["input_ids"][:count])
            self.assertEqual(live["total"], 6000)
            self.assertEqual(sum(live["stats"]["list_queue_counts"].values()), count)
            self.assertEqual(live["stats"]["recommended_count"],
                             sum(job["recommended_keep"] for job in live["jobs"]))
            published.append(count)

        with patch.object(pipeline.processor, "score_job", side_effect=self.score), \
                patch.object(pipeline.time, "monotonic", return_value=1.0):
            result = pipeline.execute_stage(self.task, "shortlist", on_event=observe)
        self.assertEqual(published, [25, 6000], "fast jobs must not rewrite all rows every 25 jobs")
        info = result["stages"]["shortlist"]
        self.assertEqual(info["status"], "completed")
        self.assertEqual(len(info["keep_ids"]), 4000)
        self.assertEqual(len(info["excluded_ids"]), 2000)
        self.assertFalse(info["confirmed"])
        self.assertEqual(result["stages"]["fetch-jd"]["status"], "pending")

    def test_pause_preserves_only_completed_prefix_and_new_attempt_changes_signature(self):
        self.populate(60)
        control = pipeline.StageControl()

        def pause(kind, value):
            if kind == "progress":
                control.pause()

        with patch.object(pipeline.processor, "score_job", side_effect=self.score):
            result = pipeline.execute_stage(self.task, "shortlist", control=control, on_event=pause)
        self.assertEqual(result["stages"]["shortlist"]["status"], "paused")
        path = self.task.run_directory("shortlist") / "prepared/live_shortlist.json"
        previous = self.read(path)
        self.assertEqual(previous["processed_count"], 25)
        self.assertEqual(len(previous["jobs"]), 25)
        with self.assertRaises(TaskStateError):
            self.task.confirm("shortlist")
        with patch.object(pipeline.processor, "score_job", side_effect=self.score):
            result = self.run_step("shortlist")
        current = self.read(path)
        self.assertEqual(result["stages"]["shortlist"]["status"], "completed")
        self.assertEqual(current["processed_count"], 60)
        self.assertNotEqual(current["input_signature"], previous["input_signature"])

    def test_failure_flushes_the_latest_successfully_processed_prefix(self):
        self.populate(60)

        def fail_after_batch(job):
            if job_id(job) == "synthetic-00026":
                raise RuntimeError("synthetic failure")
            return self.score(job)

        with patch.object(pipeline.processor, "score_job", side_effect=fail_after_batch), \
                patch.object(pipeline.time, "monotonic", return_value=1.0):
            result = self.run_step("shortlist")
        self.assertEqual(result["stages"]["shortlist"]["status"], "failed")
        checkpoint = self.read(self.task.run_directory("shortlist") / "prepared/live_shortlist.json")
        self.assertEqual(checkpoint["processed_count"], 26)
        self.assertEqual(len(checkpoint["jobs"]), 26)
        self.assertFalse(result["stages"]["shortlist"]["confirmed"])
        self.assertFalse(self.task.input_jobs("fetch-jd"))

    def test_live_write_failure_is_not_advertised_as_published_progress(self):
        self.populate(30)
        original = pipeline.processor.write_json

        def write(path, value):
            if path.name == "live_shortlist.json":
                raise OSError("synthetic write failure")
            return original(path, value)

        with patch.object(pipeline.processor, "score_job", side_effect=self.score), \
                patch.object(pipeline.processor, "write_json", side_effect=write):
            result = self.run_step("shortlist")
        self.assertEqual(result["stages"]["shortlist"]["status"], "failed")
        self.assertFalse(any(kind == "progress" for kind, _ in self.events))
        self.assertFalse((self.task.run_directory("shortlist") / "prepared/live_shortlist.json").exists())

    def test_preview_identity_ignores_progress_but_binds_attempt_input_and_revision(self):
        info = {"attempt": 1, "input_revision": 2, "input_ids": ["中文岗位", "b"], "progress": {"done": 1}}
        token = pipeline.shortlist_live_signature(info)
        progress = {**info, "progress": {"done": 2}}
        self.assertEqual(token, pipeline.shortlist_live_signature(progress))
        for field, value in (("attempt", 2), ("input_revision", 3), ("input_ids", ["b", "中文岗位"])):
            with self.subTest(field=field):
                self.assertNotEqual(token, pipeline.shortlist_live_signature({**info, field: value}))


class StageRuleExecutionTests(PipelineCase):
    def test_import_rule_recommends_without_losing_rows_and_manual_keep_has_priority(self):
        before = {path: path.read_bytes() for path in self.source.rglob("*.json")}
        self.task.set_stage_rules("import", {"exclude_title_keywords": ["销售"]})
        result = self.run_step("import")
        info = result["stages"]["import"]
        self.assertEqual(set(info["result_ids"]), set("abcd"))
        self.assertEqual(set(info["keep_ids"]), set("abd"))
        self.assertEqual(info["excluded_ids"], ["c"])
        self.task.set_keep("import", ["c"], True)
        self.task.confirm("import")
        self.assertEqual({job_id(job) for job in self.task.input_jobs("shortlist")}, set("abcd"))
        self.assertEqual(before, {path: path.read_bytes() for path in self.source.rglob("*.json")})

    def test_shortlist_uses_saved_word_rules_while_preserving_manual_choices(self):
        self.import_and_confirm()
        self.run_step("shortlist")
        self.task.set_keep("shortlist", ["c"], True)
        self.task.set_stage_rules("shortlist", {"use_default_direction_rules": False, "include_keywords": ["RAG"]})
        result = self.run_step("shortlist")
        info = result["stages"]["shortlist"]
        self.assertEqual(set(info["keep_ids"]), set("ac"))
        self.assertEqual(info["recommended_keep_ids"], ["a"])
        self.assertEqual(info["manual_choices"], {"c": True})
        self.assertFalse(info["confirmed"])

    def test_jd_rule_reaches_child_command_and_durable_statistics(self):
        self.import_and_confirm()
        self.shortlist_and_confirm()
        self.task.set_stage_rules("fetch-jd", {"scroll_count": 7, "detail_speed_profile": "safe",
                                                "page_wait_min": 4, "page_wait_max": 8})
        commands = []
        fake = self.fake_jd([])

        def child(task, stage, command, control, emit, monitor=None):
            commands.append(command)
            return fake(task, stage, command, control, emit, monitor)

        with patch.object(pipeline, "_run_child", side_effect=child):
            result = self.run_step("fetch-jd", scraper_root=self.scraper)
        command = commands[0]
        self.assertEqual(command[command.index("--jd-scroll-count") + 1], "7")
        self.assertEqual(command[command.index("--detail-speed-profile") + 1], "safe")
        self.assertEqual(command[command.index("--jd-page-wait-min") + 1], "4")
        self.assertEqual(command[command.index("--jd-page-wait-max") + 1], "8")
        self.assertEqual(result["stages"]["fetch-jd"]["stats"]["scroll_count"], 7)
        self.assertEqual(result["stages"]["fetch-jd"]["stats"]["page_wait_min"], 4)
        self.assertEqual(result["stages"]["fetch-jd"]["stats"]["page_wait_max"], 8)

