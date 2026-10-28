"""Source-catalog handoff stays local and preserves old and custom keywords."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from collection_sources import assert_source_specs_compatible, review_config_for_source
import process_architecture as processor


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class CollectionSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.output = self.base / "review-configs"
        self.original = json.loads((PROJECT_ROOT / "config" / "architecture_collection.json").read_text(encoding="utf-8"))
        self.fallback = self.base / "default.json"
        write_json(self.fallback, self.original)

    def resolve(self, **kwargs):
        return review_config_for_source(self.source, self.fallback, output_dir=kwargs.get("output_dir", self.output))

    def snapshot(self, config, name="first.json"):
        path = self.source / "collection-plans" / name
        write_json(path, config)
        return path

    def test_no_snapshots_or_empty_directory_use_old_path_without_writes(self):
        self.assertEqual(self.resolve(), self.fallback)
        (self.source / "collection-plans").mkdir()
        self.assertEqual(self.resolve(), self.fallback)
        self.assertFalse(self.output.exists())

    def test_unchanged_catalog_does_not_create_a_redundant_configuration(self):
        self.snapshot(self.original)
        self.assertEqual(self.resolve(), self.fallback)
        self.assertFalse(self.output.exists())

    def test_custom_and_old_keywords_remain_importable_and_all_sources_unchanged(self):
        config = copy.deepcopy(self.original)
        config["current"]["keywords"].append({"id": "custom_consultant", "query": "企业智能体咨询"})
        config["api_key"] = "PRIVATE_KEY_MUST_NOT_BE_COPIED"
        snapshot = self.snapshot(config)
        for key, query, job in (("solarch", "AI解决方案架构师", "old-job"),
                                ("custom_consultant", "企业智能体咨询", "new-job")):
            path = self.source / "raw" / "current" / "shanghai" / f"current__shanghai__{key}__20_50__1_3.json"
            write_json(path, {"city": "上海", "keyword": query,
                              "filters": {"salary": "406", "experience": "104", "degree": "203"},
                              "jobs": [{"job_id": job, "title": query, "salary": "20-30K"}]})
        files = [self.fallback, snapshot, *self.source.rglob("raw/**/*.json")]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
        result = self.resolve()
        self.assertNotEqual(result, self.fallback)
        self.assertTrue(result.is_relative_to(self.output))
        value = json.loads(result.read_text(encoding="utf-8"))
        self.assertNotIn("collection_selection", value)
        self.assertNotIn("PRIVATE_KEY_MUST_NOT_BE_COPIED", result.read_text(encoding="utf-8"))
        rows, _stats = processor.collect_rows(self.source, value, city="上海")
        self.assertEqual({row["job_id"] for row in rows}, {"old-job", "new-job"})
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}, before)

    def test_multiple_snapshots_union_catalogs_and_new_outputs_are_immutable(self):
        for index, track in enumerate(("current", "target")):
            config = copy.deepcopy(self.original)
            config[track]["keywords"].append({"id": f"custom_{index}", "query": f"新咨询方向{index}"})
            self.snapshot(config, f"{index}.json")
        first = self.resolve()
        before = (first.read_bytes(), first.stat().st_mtime_ns)
        second = self.resolve()
        self.assertNotEqual(first, second)
        self.assertEqual((first.read_bytes(), first.stat().st_mtime_ns), before)
        value = json.loads(second.read_text(encoding="utf-8"))
        self.assertIn("custom_0", {row["id"] for row in value["current"]["keywords"]})
        self.assertIn("custom_1", {row["id"] for row in value["target"]["keywords"]})

    def test_new_search_selection_does_not_remove_already_collected_metadata(self):
        config = copy.deepcopy(self.original)
        config["collection_selection"] = {
            "cities": ["shanghai"], "salaries": ["20_50"],
            "current": {"keywords": ["solarch"], "experiences": ["1_3"]},
            "target": {"keywords": [], "experiences": []}, "combinations": None,
        }
        self.snapshot(config)
        self.assertEqual(self.resolve(), self.fallback)
        self.assertFalse(self.output.exists())

    def test_corrupt_snapshot_is_not_silently_skipped_or_overwritten(self):
        path = self.snapshot(self.original)
        path.write_text("{broken", encoding="utf-8")
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with self.assertRaisesRegex(ValueError, "损坏|无效"):
            self.resolve()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertFalse(self.output.exists())

    def test_conflicting_keyword_id_fails_instead_of_relabeling_old_jobs(self):
        config = copy.deepcopy(self.original)
        config["current"]["keywords"][0]["query"] = "不同的查询不能复用旧ID"
        config["target"]["keywords"][0]["query"] = "不同的查询不能复用旧ID"
        self.snapshot(config)
        with self.assertRaisesRegex(ValueError, "同一 ID|不同条件"):
            self.resolve()
        self.assertFalse(self.output.exists())

    def test_output_inside_source_and_directory_with_json_suffix_are_rejected(self):
        config = copy.deepcopy(self.original)
        config["current"]["keywords"].append({"id": "new_keyword", "query": "新业务方案"})
        self.snapshot(config)
        with self.assertRaisesRegex(ValueError, "原始采集源"):
            self.resolve(output_dir=self.source / "review-configs")
        (self.source / "collection-plans" / "not-a-file.json").mkdir()
        with self.assertRaisesRegex(ValueError, "普通 JSON"):
            self.resolve()
        self.assertFalse(self.output.exists())

    def test_imported_plan_cannot_reuse_a_recorded_combination_for_new_conditions(self):
        spec = {"combination": "current__shanghai__solarch__20_50__1_3", "city_id": "shanghai",
                "city": "上海", "keyword_id": "solarch", "keyword": "AI解决方案架构师",
                "salary_code": "406", "experience_code": "104", "degree": "203"}
        path = self.source / "collection_manifest.json"
        write_json(path, {"records": [{**spec, "status": "success"}]})
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        assert_source_specs_compatible(self.source, [spec])
        for field, changed in (("keyword", "不同的AI查询"), ("salary_code", "405"),
                               ("experience_code", "105"), ("degree", "202"), ("city", "深圳")):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "旧组合 ID"):
                assert_source_specs_compatible(self.source, [{**spec, field: changed}])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_new_ids_and_legacy_partial_records_remain_compatible_without_writes(self):
        new_root = self.base / "not-created"
        assert_source_specs_compatible(new_root, [{"combination": "new"}])
        self.assertFalse(new_root.exists())
        write_json(self.source / "collection_manifest.json", {"records": [{"combination": "old", "status": "success"}]})
        assert_source_specs_compatible(self.source, [{"combination": "new", "keyword": "新查询"},
                                                    {"combination": "old", "keyword": "旧字段未知，保持兼容"}])

    def test_compatibility_check_does_not_replace_a_corrupt_manifest(self):
        path = self.source / "collection_manifest.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "采集清单"):
            assert_source_specs_compatible(self.source, [])
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
