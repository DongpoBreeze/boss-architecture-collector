"""Stage actions must only change the reviewed version and relevant inputs."""

import unittest

from test_desktop_tasks import TaskTestCase, tasks
from test_review_pipeline import PipelineCase


class RecommendationGuards(unittest.TestCase):
    setUp = TaskTestCase.setUp
    write_json = TaskTestCase.write_json
    create = TaskTestCase.create
    imported = TaskTestCase.imported
    run_import = TaskTestCase.run_import
    run_stage = TaskTestCase.run_stage
    run_all = TaskTestCase.run_all

    def test_stale_window_cannot_clear_new_manual_choices_or_confirmation(self):
        for stage in tasks.STAGES[:-1]:
            with self.subTest(stage=stage):
                self.task = self.create()
                self.run_all()
                expected = self.task.stage_info(stage)
                other = tasks.ReviewTask.open(self.task.path)
                other.set_keep(stage, ["b"], False)
                other.confirm(stage)
                manifest = self.task.path / "task_manifest.json"
                before = manifest.read_bytes()
                with self.assertRaisesRegex(tasks.TaskStateError, "其他窗口"):
                    self.task.apply_recommendations(stage, expected_stage=expected)
                self.assertEqual(manifest.read_bytes(), before)
                self.assertEqual(other.stage_info(stage)["manual_choices"], {"b": False})
                self.assertTrue(other.stage_info(stage)["confirmed"])

    def test_current_snapshot_and_legacy_call_restore_recommendations_explicitly(self):
        self.run_all()
        self.task.set_keep("llm", ["b"], False)
        self.task.confirm("llm")
        expected = self.task.stage_info("llm")
        self.task.apply_recommendations("llm", expected_stage=expected)
        current = self.task.stage_info("llm")
        self.assertEqual(current["manual_choices"], {})
        self.assertEqual(current["keep_ids"], expected["recommended_keep_ids"])
        self.assertFalse(current["confirmed"])
        self.assertEqual(self.task.stage_info("report")["status"], "stale")
        self.task.set_keep("llm", ["a"], False)
        self.task.apply_recommendations("llm")
        self.assertEqual(self.task.stage_info("llm")["manual_choices"], {})


class LocalStageParameters(unittest.TestCase):
    setUp = PipelineCase.setUp
    write = staticmethod(PipelineCase.write)
    create = PipelineCase.create
    run_step = PipelineCase.run_step

    def test_unrelated_invalid_parameters_do_not_block_import_or_shortlist(self):
        result = self.run_step("import", workers="bad", jd_workers=False, batch_size=-1)
        self.assertEqual(result["stages"]["import"]["status"], "completed")
        self.task.confirm("import")
        result = self.run_step("shortlist", workers=0, jd_workers=99, batch_size="bad")
        self.assertEqual(result["stages"]["shortlist"]["status"], "completed")
