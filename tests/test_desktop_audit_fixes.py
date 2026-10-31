"""Acceptance of audit fixes using synthetic tasks and invisible Tk windows."""

import copy
import unittest
from unittest.mock import patch

import test_desktop_review_ui as fixtures

ui = fixtures.ui
write_json = fixtures.write_json
job_id = fixtures.job_id


class DesktopAuditFixTests(unittest.TestCase):
    setUp = fixtures.ReviewTkTests.setUp
    make_window = fixtures.ReviewTkTests.make_window
    _cleanup_windows = fixtures.ReviewTkTests._cleanup_windows
    pump = fixtures.ReviewTkTests.pump
    wait_idle = fixtures.ReviewTkTests.wait_idle
    fake_executor = fixtures.ReviewTkTests.fake_executor
    create_task = fixtures.ReviewTkTests.create_task
    complete_through = fixtures.ReviewTkTests.complete_through
    task_json = fixtures.ReviewTkTests.task_json

    def test_unsaved_project_provider_is_explicit_without_resolving_global_secrets(self):
        with patch.object(ui.project_llm_settings, "read_api_key", create=True) as key_reader, \
                patch("llm_client.load_codex_config", side_effect=AssertionError("UI must not resolve auth")):
            self.app._refresh_model_config_label()
        caption = self.app.model_config_label.cget("text")
        self.assertIn("尚未保存项目模型配置", caption)
        self.assertIn("全局服务", caption)
        self.assertNotIn(ui.project_llm_settings.DEFAULT_BASE_URL, caption)
        key_reader.assert_not_called()

    def test_shortlist_summary_tracks_saved_flags_words_and_staleness(self):
        self.complete_through("shortlist")
        app, task = self.app, self.app.task
        rules = task.get_stage_rules("shortlist")
        rules.update(use_default_direction_rules=False, exclude_pure_development=False,
                     keep_verification=False, include_keywords=["中文方案"], exclude_keywords=["客服"])
        calls = len(self.calls)
        task.set_stage_rules("shortlist", rules)
        app._refresh_task()
        self.wait_idle()
        app.stage.set("shortlist")
        app._render()
        caption = app.list_queue_hint.get()
        for phrase in ("默认方向关", "不排除纯开发", "不建议保留待核实", "包含词 1", "排除词 1", "须重新运行"):
            self.assertIn(phrase, caption)
        self.assertNotIn("默认保留明确相关＋待核实", caption)
        self.assertEqual(len(self.calls), calls)

    def test_jd_completeness_filter_composes_with_manual_list_search_and_live_updates(self):
        self.complete_through("shortlist", confirm_last=True)
        app, task = self.app, self.app.task
        task.begin("fetch-jd")
        rows = task.input_jobs("fetch-jd")
        for index, row in enumerate(rows):
            state = ("complete", "partial", "missing")[index % 3]
            row.update(jd="" if state == "missing" else "中文职责正文", jd_completeness=state)
        task.finish("fetch-jd", rows)
        app._refresh_task()
        self.wait_idle()
        app.stage.set("fetch-jd")
        app._render()
        before = (task.path / "task_manifest.json").read_bytes()
        expected = [job_id(row) for row in rows if row["jd_completeness"] == "partial"]
        app.jd_completeness_filter.set("正文部分可用")
        self.pump()
        self.assertEqual([job_id(row) for row in app._filtered_jobs], expected)
        app.search.set(rows[1]["title"])
        self.pump()
        self.assertEqual([job_id(row) for row in app._filtered_jobs], [job_id(rows[1])])
        self.assertEqual(app.jd_filter_box.get(), "正文部分可用")
        self.assertEqual((task.path / "task_manifest.json").read_bytes(), before)
        self.assertEqual(len(self.calls), 2)
        app.search.set("")
        app._set_keep([expected[0]], False)
        self.wait_idle()
        app.filter.set("剔除")
        self.pump()
        self.assertEqual([job_id(row) for row in app._filtered_jobs], [expected[0]])
        app.stage.set("shortlist")
        app._render()
        self.assertFalse(app.jd_filter_frame.winfo_manager())
        app.stage.set("fetch-jd")
        app._render()
        self.assertEqual(app.jd_completeness_filter.get(), "正文部分可用")

    def test_import_issues_stay_visible_after_reopen_and_do_not_claim_full_success(self):
        self.create_task()
        task = self.app.task
        task.begin("import")
        task.finish("import", copy.deepcopy(self.jobs), stats={"invalid_file_count": 1},
                    issues=["已跳过 1 个损坏源文件：raw/current/损坏.json"])
        self.app._refresh_task()
        self.wait_idle()
        self.assertIn("注意：", self.app.stage_description.get())
        self.assertIn("损坏", self.app.stage_description.get())
        self.assertIn("损坏文件 1", self.app.progress_text.get())
        root, other = self.make_window()
        other._open_task(task.path)
        self.wait_idle(other, root)
        self.assertIn("损坏", other.stage_description.get())
        self.assertFalse(other._stage_info()["confirmed"])

    def test_rule_summary_and_jd_filters_fit_scaling_two_without_hiding_pagination(self):
        self.complete_through("shortlist", confirm_last=True)
        app, root = self.app, self.root
        original_scaling = root.tk.call("tk", "scaling")
        self.addCleanup(lambda: root.tk is not None and root.tk.call("tk", "scaling", original_scaling))
        root.tk.call("tk", "scaling", 2.0)
        root.geometry("860x720")
        root.deiconify()
        for theme in ("dark", "light"):
            app._set_theme(theme, persist=False)
            for stage in ("shortlist", "fetch-jd"):
                app.stage.set(stage)
                app._render()
                self.pump(.1)
                controls = ((app.list_queue_box, app.list_queue_hint_label) if stage == "shortlist" else
                            (app.jd_filter_box,))
                for widget in (*controls, app.previous_page_button, app.next_page_button):
                    with self.subTest(theme=theme, stage=stage, widget=str(widget)):
                        self.assertTrue(widget.winfo_ismapped())
                        x = widget.winfo_rootx() - root.winfo_rootx()
                        y = widget.winfo_rooty() - root.winfo_rooty()
                        self.assertGreaterEqual(x, 0)
                        self.assertGreaterEqual(y, 0)
                        self.assertLessEqual(x + widget.winfo_width(), root.winfo_width())
                        self.assertLessEqual(y + widget.winfo_height(), root.winfo_height())
                self.assertGreaterEqual(app.results_table.winfo_height(), 30)

    def test_live_ai_rejects_unsigned_changed_input_provider_and_attempt_without_losing_valid_rows(self):
        self.complete_through("fetch-jd", confirm_last=True)
        task = self.app.task
        task.begin("llm")
        self.addCleanup(lambda: task.interrupt("llm", "stopped"))
        jobs = task.input_jobs("llm")
        sign = fixtures.signed_ai_checkpoint_context(task, jobs)
        valid = {"job_id": job_id(jobs[0]), "status": "success", "review_format": "simple-v1",
                 "判断": "符合", "理由": "中文职责匹配"}
        records = [sign(valid)]
        for index, field in ((1, "cache_signature"), (2, "cache_input_signature")):
            row = sign({**valid, "job_id": job_id(jobs[index])})
            row[field] = "incorrect-signature"
            records.append(row)
        records.append({**valid, "job_id": job_id(jobs[3])})
        path = task.run_directory("llm") / "prepared/llm_reviews.json"
        write_json(path, {"reviews": records})
        cache = ui.TaskViewCache()
        view = ui._capture_task(task, cache=cache, include_report=False)
        info = view["manifest"]["stages"]["llm"]
        self.assertEqual(info["keep_ids"], [job_id(jobs[0])])
        self.assertEqual(info["stats"]["llm_success_count"], 1)
        repeated = ui._capture_task(task, cache=cache, include_report=False)
        self.assertIs(repeated["rows"]["llm"], view["rows"]["llm"])
        context_path = task.run_directory("llm") / "review_cache_context.json"
        context = ui._read_json(task, context_path)
        context["attempt"] += 1
        write_json(context_path, context)
        changed = ui._capture_task(task, cache=cache, include_report=False)
        self.assertEqual(changed["manifest"]["stages"]["llm"]["keep_ids"], [])
        self.assertEqual(changed["manifest"]["stages"]["llm"]["stats"]["llm_success_count"], 0)
        context["attempt"] -= 1
        context["provider"] = "semantic-review-v1:provider:" + "0" * 64
        write_json(context_path, context)
        self.assertEqual(ui._capture_task(task, cache=cache, include_report=False)["manifest"]["stages"]["llm"]["keep_ids"], [])
        fixtures.signed_ai_checkpoint_context(task, jobs)
        restored = ui._capture_task(task, cache=cache, include_report=False)
        self.assertEqual(restored["manifest"]["stages"]["llm"]["keep_ids"], [job_id(jobs[0])])
        context = ui._read_json(task, context_path)
        prompt_path = task.path / context["review_prompt_file"]
        prompt_path.write_text("内容被修改但旧文件名未变", encoding="utf-8")
        self.assertEqual(ui._capture_task(task, cache=cache, include_report=False)["manifest"]["stages"]["llm"]["keep_ids"], [])


if __name__ == "__main__":
    unittest.main()
