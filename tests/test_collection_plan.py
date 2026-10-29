import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from collect_architecture import build_specs  # noqa: E402
from collection_plan import (  # noqa: E402
    filter_plan_specs,
    plan_editor_values,
    preview_rows,
    save_plan_config,
    select_plan_combinations,
    update_plan_config,
    validate_plan_config,
)


class CollectionPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.default_path = PROJECT_ROOT / "config" / "architecture_collection.json"
        cls.default_config = json.loads(cls.default_path.read_text(encoding="utf-8-sig"))

    def setUp(self):
        self.config = copy.deepcopy(self.default_config)

    def update(self, base=None, **overrides):
        base = self.config if base is None else base
        values = plan_editor_values(base)
        arguments = {
            "city_ids": values["city_ids"],
            "salary_ids": values["salary_ids"],
            "current_keywords": values["current"]["keywords"],
            "current_experience_ids": values["current"]["experience_ids"],
            "target_keywords": values["target"]["keywords"],
            "target_experience_ids": values["target"]["experience_ids"],
        }
        arguments.update(overrides)
        return update_plan_config(base, **arguments)

    def test_default_matrix_and_city_counts(self):
        normalized = validate_plan_config(self.config)
        self.assertEqual(normalized, self.config)
        self.assertNotIn("collection_selection", normalized)
        specs = build_specs(normalized)
        self.assertEqual(len(specs), 672)
        self.assertEqual(sum(spec["track"] == "current" for spec in specs), 528)
        self.assertEqual(sum(spec["track"] == "target" for spec in specs), 144)
        self.assertEqual(len(build_specs(normalized, cities=["上海"])), 112)
        self.assertEqual(len(build_specs(normalized, cities=["SHANGHAI"])), 112)

    def test_normalization_is_a_detached_whitelist_copy(self):
        secret = "must-not-be-saved-secret-value"
        self.config["api_key"] = secret
        self.config["degree"]["password"] = secret
        self.config["cities"][0]["cookie"] = secret
        self.config["salaries"][0]["arbitrary_raw"] = {"secret": secret}
        self.config["current"]["token"] = secret
        self.config["current"]["keywords"][0]["api_key"] = secret
        self.config["target"]["experiences"][0]["authorization"] = secret
        self.config["collection_selection"] = {"arbitrary_raw": secret}
        normalized = validate_plan_config(self.config)
        self.assertNotIn(secret, json.dumps(normalized))
        normalized["cities"][0]["name"] = "独立副本"
        self.assertEqual(self.config["cities"][0]["name"], "上海")
        self.assertIn("api_key", self.config)

    def test_editor_values_have_exact_options_and_selected_values(self):
        values = plan_editor_values(self.config)
        self.assertEqual(set(values), {"cities", "salaries", "experiences", "city_ids", "salary_ids", "current", "target"})
        self.assertEqual(values["cities"][0], {"id": "shanghai", "label": "上海"})
        self.assertEqual(values["salaries"][0], {"id": "10_20", "label": "10-20K"})
        self.assertEqual(values["experiences"]["current"][0], {"id": "unlimited", "label": "经验不限"})
        self.assertEqual(values["current"]["keywords"][0], "AI解决方案架构师")
        self.assertEqual(values["target"]["experience_ids"], ["3_5", "5_10"])
        values["current"]["keywords"].clear()
        self.assertEqual(len(self.config["current"]["keywords"]), 11)

    def test_dimension_update_preserves_complete_historical_catalogs(self):
        plan = self.update(
            city_ids=["shanghai"], salary_ids=["10_20"],
            current_keywords=["AI解决方案架构师"], current_experience_ids=["under1"],
            target_keywords=[], target_experience_ids=[],
        )
        self.assertEqual(len(build_specs(plan)), 1)
        for key in ("cities", "salaries", "current", "target"):
            self.assertEqual(plan[key], self.config[key])
        self.assertEqual(plan["collection_selection"]["current"], {"keywords": ["solarch"], "experiences": ["under1"]})
        values = plan_editor_values(plan)
        self.assertEqual(values["city_ids"], ["shanghai"])
        self.assertEqual(values["current"]["keywords"], ["AI解决方案架构师"])
        self.assertEqual(values["target"]["keywords"], [])
        self.assertEqual(self.config, self.default_config)

    def test_new_query_uses_stable_hash_id_and_old_query_keeps_legacy_id(self):
        query = "AI 数据/智能体架构师"
        expected = "k_" + hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        first = self.update(current_keywords=[f"  {query}  ", "FDE"])
        second = self.update(current_keywords=[query])
        self.assertEqual(first["collection_selection"]["current"]["keywords"], [expected, "fde"])
        self.assertEqual(second["collection_selection"]["current"]["keywords"], [expected])
        self.assertEqual(first["current"]["keywords"][:11], self.config["current"]["keywords"])
        reselected = self.update(first, current_keywords=["AI解决方案架构师", query])
        self.assertEqual(reselected["collection_selection"]["current"]["keywords"], ["solarch", expected])
        self.assertEqual(sum(item["id"] == expected for item in reselected["current"]["keywords"]), 1)

    def test_editing_a_query_never_reuses_its_old_source_identity(self):
        changed = self.update(current_keywords=["AI解决方案架构师（金融）"])
        new_id = changed["collection_selection"]["current"]["keywords"][0]
        self.assertNotEqual(new_id, "solarch")
        self.assertEqual(next(item["query"] for item in changed["current"]["keywords"] if item["id"] == "solarch"), "AI解决方案架构师")
        self.assertTrue(all(spec["keyword_id"] == new_id for spec in build_specs(changed, "current")))

    def test_query_can_be_added_to_another_track_with_its_existing_id(self):
        plan = self.update(current_keywords=["全新 双轨 查询"], target_keywords=["全新 双轨 查询", "AI产品经理"])
        current_id = plan["collection_selection"]["current"]["keywords"][0]
        self.assertEqual(plan["collection_selection"]["target"]["keywords"], [current_id, "prodmanager"])
        self.assertEqual(plan["target"]["keywords"][:6], self.config["target"]["keywords"])
        self.assertEqual(next(item["query"] for item in plan["target"]["keywords"] if item["id"] == "prodmanager"), "AI产品经理")

    def test_old_deselected_keyword_and_experience_remain_parseable(self):
        from process_architecture import parse_source

        plan = self.update(current_keywords=["新的检索"], current_experience_ids=["1_3"], target_keywords=[])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "raw" / "current" / "shanghai" / "current__shanghai__solarch__10_20__fresh.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"jobs": [], "filters": {"salary": "405", "experience": "102", "degree": "203"}}), encoding="utf-8")
            source = parse_source(path, root, plan)
        self.assertIsNotNone(source)
        self.assertEqual(source["keyword"], "AI解决方案架构师")
        self.assertEqual(source["experience_id"], "fresh")

    def test_individual_selection_is_exact_not_first_n_and_keeps_order(self):
        specs = build_specs(self.config)
        selected = [specs[-1]["combination"], specs[73]["combination"]]
        plan = select_plan_combinations(self.config, selected)
        result = build_specs(plan)
        self.assertEqual(result, [specs[73], specs[-1]])
        self.assertEqual(build_specs(plan, limit=1), [specs[73]])
        self.assertEqual(build_specs(plan, "target", limit=1, cities=["北京"]), [specs[-1]])
        self.assertEqual(build_specs(plan, cities=["深圳"]), [])
        self.assertNotIn("collection_selection", self.config)

    def test_dimension_updates_reset_individual_selection(self):
        first = select_plan_combinations(self.config, [build_specs(self.config)[-1]["combination"]])
        updated = self.update(first, city_ids=["shanghai"])
        self.assertIsNone(updated["collection_selection"]["combinations"])
        self.assertEqual(len(build_specs(updated)), 112)
        reset = select_plan_combinations(first, None)
        self.assertEqual(len(build_specs(reset)), 672)
        self.assertEqual(len(build_specs(first)), 1)

    def test_filter_uses_same_dimensions_without_mutating_supplied_specs(self):
        full = build_specs(self.config)
        before = copy.deepcopy(full)
        plan = self.update(city_ids=["shanghai", "shenzhen"], salary_ids=["20_50"], current_experience_ids=["under1"], target_keywords=[])
        self.assertEqual(filter_plan_specs(plan, full), build_specs(plan))
        self.assertEqual(len(filter_plan_specs(plan, full)), 22)
        self.assertEqual(full, before)
        self.assertEqual(filter_plan_specs(self.config, full), full)

    def test_missing_selection_fields_default_to_all_but_empty_track_is_none(self):
        self.config["collection_selection"] = {"current": {"keywords": []}}
        self.assertEqual(len(build_specs(self.config)), 144)
        plan = self.update(current_experience_ids=[])
        self.assertEqual(len(build_specs(plan, "current")), 0)
        self.assertEqual(len(build_specs(plan, "target")), 144)
        specs = build_specs(self.default_config)
        partial = copy.deepcopy(self.default_config)
        partial["collection_selection"] = {"combinations": [specs[10]["combination"]]}
        self.assertEqual(build_specs(partial), [specs[10]])

    def test_empty_city_salary_or_entire_plan_is_rejected_in_chinese(self):
        for arguments, message in (
            ({"city_ids": []}, "至少.*城市"),
            ({"salary_ids": []}, "至少.*薪资"),
            ({"current_keywords": [], "target_keywords": []}, "没有可执行组合"),
            ({"current_experience_ids": [], "target_experience_ids": []}, "没有可执行组合"),
        ):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, message):
                self.update(**arguments)
        for key, message in (("cities", "城市目录不能为空"), ("salaries", "薪资目录不能为空")):
            config = copy.deepcopy(self.config)
            config[key] = []
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, message):
                validate_plan_config(config)

    def test_empty_unknown_malformed_and_duplicate_combinations_are_rejected(self):
        valid = build_specs(self.config)[0]["combination"]
        for combinations, message in (
            ([], "至少勾选"),
            ([""], "不能为空"),
            ([valid, valid], "重复组合"),
            (["current__shanghai__missing__10_20__fresh"], "不存在"),
            (["../outside.json"], "格式"),
            ([42], "组合ID文本"),
            (valid, "组合列表"),
        ):
            with self.subTest(combinations=combinations), self.assertRaisesRegex(ValueError, message):
                select_plan_combinations(self.config, combinations)
        restricted = self.update(city_ids=["shenzhen"])
        with self.assertRaisesRegex(ValueError, "不符合"):
            select_plan_combinations(restricted, [valid])

    def test_unknown_duplicate_and_invalid_dimension_selection_is_rejected(self):
        for arguments, message in (
            ({"city_ids": ["missing"]}, "不存在"),
            ({"salary_ids": ["10_20", "10_20"]}, "重复ID"),
            ({"current_experience_ids": ["3_5"]}, "不存在"),
            ({"current_keywords": ["FDE", " FDE "]}, "重复关键词"),
            ({"current_keywords": [42]}, "文本"),
            ({"target_keywords": "FDE"}, "文本列表"),
            ({"city_ids": "shanghai"}, "ID列表"),
        ):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, message):
                self.update(**arguments)
        for selection in (None, [], {"cities": None}, {"current": None}, {"target": {"experiences": "3_5"}}):
            config = copy.deepcopy(self.config)
            config["collection_selection"] = selection
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                validate_plan_config(config)

    def test_blank_keyword_lines_are_ignored_without_enabling_an_empty_track(self):
        plan = self.update(current_keywords=["", "  ", "\n"], target_keywords=[" AI架构师 ", ""])
        self.assertEqual(plan["collection_selection"]["current"]["keywords"], [])
        self.assertEqual(plan["collection_selection"]["target"]["keywords"], ["aiarch"])
        self.assertEqual(len(build_specs(plan)), 24)
        with self.assertRaisesRegex(ValueError, "控制字符"):
            self.update(current_keywords=["FDE\n架构师"])

    def test_unsafe_catalog_ids_are_rejected(self):
        for unsafe in ("../escape", "a/b", "a\\b", "a__b", "CON", "com1", "a b", "", ".", "x" * 65):
            config = copy.deepcopy(self.config)
            config["cities"][0]["id"] = unsafe
            with self.subTest(identifier=unsafe), self.assertRaisesRegex(ValueError, "不安全"):
                validate_plan_config(config)

    def test_duplicate_catalog_ids_queries_and_conflicting_track_ids_are_rejected(self):
        cases = []
        duplicate_id = copy.deepcopy(self.config)
        duplicate_id["cities"].append({"id": "SHANGHAI", "name": "重复上海"})
        cases.append((duplicate_id, "重复ID"))
        duplicate_query = copy.deepcopy(self.config)
        duplicate_query["current"]["keywords"].append({"id": "newid", "query": "FDE"})
        cases.append((duplicate_query, "重复关键词"))
        conflicting_query = copy.deepcopy(self.config)
        conflicting_query["target"]["keywords"][0]["query"] = "同ID但不同查询"
        cases.append((conflicting_query, "不能修改旧ID"))
        conflicting_id = copy.deepcopy(self.config)
        conflicting_id["target"]["keywords"][0]["id"] = "otherid"
        cases.append((conflicting_id, "复用同一旧ID"))
        conflicting_exp = copy.deepcopy(self.config)
        conflicting_exp["target"]["experiences"].append({"id": "fresh", "label": "错误应届", "code": "199"})
        cases.append((conflicting_exp, "同一经验编码"))
        for config, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_plan_config(config)

    def test_malformed_catalog_and_codes_are_rejected(self):
        for key, value in (("cities", {}), ("current", []), ("target", {}), ("degree", [])):
            config = copy.deepcopy(self.config)
            config[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_plan_config(config)
        self.config["salaries"][0]["code"] = "405 --unsafe"
        with self.assertRaisesRegex(ValueError, "数字编码"):
            validate_plan_config(self.config)

    def test_hash_collision_does_not_reinterpret_an_existing_id(self):
        identifier = "k_" + "a" * 16
        self.config["current"]["keywords"].append({"id": identifier, "query": "之前的查询"})
        with patch("collection_plan.hashlib.sha256", return_value=SimpleNamespace(hexdigest=lambda: "a" * 64)):
            with self.assertRaisesRegex(ValueError, "冲突"):
                self.update(current_keywords=["新的查询"])

    def test_save_is_unique_and_never_overwrites_default_or_existing_plans(self):
        before_default = self.default_path.read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            original = directory / "architecture_collection.json"
            original.write_bytes(b"original-config")
            first = save_plan_config(self.config, directory)
            first_bytes = first.read_bytes()
            changed = self.update(city_ids=["shanghai"])
            changed["secret"] = "not-copied"
            second = save_plan_config(changed, directory)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), first_bytes)
            self.assertEqual(original.read_bytes(), b"original-config")
            self.assertNotIn("not-copied", second.read_text(encoding="utf-8"))
            self.assertEqual(len(build_specs(json.loads(second.read_text(encoding="utf-8")))), 112)
            self.assertFalse(list(directory.glob("*.tmp")))
            self.assertFalse(list(directory.glob(".*.tmp")))
        self.assertEqual(self.default_path.read_bytes(), before_default)

    def test_save_retries_existing_target_without_overwriting_it(self):
        moment = datetime(2026, 10, 21, 12, 34, 56, 123456)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            existing = directory / "collection-plan-20261021-123456-123456-aaaaaaaaaaaa.json"
            existing.write_text("existing", encoding="utf-8")
            with patch("collection_plan.datetime") as clock, patch("collection_plan.uuid.uuid4", side_effect=[SimpleNamespace(hex="a" * 32), SimpleNamespace(hex="b" * 32)]):
                clock.now.return_value = moment
                saved = save_plan_config(self.config, directory)
            self.assertEqual(existing.read_text(encoding="utf-8"), "existing")
            self.assertEqual(saved.name, "collection-plan-20261021-123456-123456-bbbbbbbbbbbb.json")
            self.assertEqual(json.loads(saved.read_text(encoding="utf-8")), self.config)

    def test_save_publishes_complete_json_and_cleans_failed_temporary_files(self):
        publish_name = "rename" if os.name == "nt" else "link"
        original_publish = getattr(os, publish_name)
        observed = []

        def publish(source, destination):
            self.assertFalse(Path(destination).exists())
            observed.append(json.loads(Path(source).read_text(encoding="utf-8")))
            return original_publish(source, destination)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch(f"collection_plan.os.{publish_name}", side_effect=publish):
                saved = save_plan_config(self.config, directory)
            self.assertEqual(observed, [self.config])
            with patch("collection_plan.json.dump", side_effect=OSError("写入失败")):
                with self.assertRaisesRegex(OSError, "写入失败"):
                    save_plan_config(self.config, directory)
            self.assertEqual(list(directory.iterdir()), [saved])

    def test_invalid_plan_is_not_saved_and_does_not_create_a_directory(self):
        self.config["collection_selection"] = {"combinations": []}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "not-created"
            with self.assertRaisesRegex(ValueError, "至少勾选"):
                save_plan_config(self.config, directory)
            self.assertFalse(directory.exists())

    def make_directory_link(self, target, link):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("当前文件系统不支持目录链接")
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, check=False,
            )
            if result.returncode:
                self.skipTest("当前Windows环境不支持创建测试用junction")

        def remove_link():
            if link.is_symlink():
                link.unlink()
            elif link.exists():
                link.rmdir()

        return remove_link

    def test_save_rejects_actual_link_or_junction_and_its_descendants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "outside"
            target.mkdir()
            sentinel = target / "existing.json"
            sentinel.write_text("untouched", encoding="utf-8")
            link = root / "linked-directory"
            remove_link = self.make_directory_link(target, link)
            try:
                for directory in (link, link / "not-created"):
                    with self.subTest(directory=directory), self.assertRaisesRegex(ValueError, "符号链接|junction"):
                        save_plan_config(self.config, directory)
                self.assertEqual(list(target.iterdir()), [sentinel])
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")
            finally:
                remove_link()

    def test_save_rechecks_directory_after_creation_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "outside"
            target.mkdir()
            directory = root / "new-directory"
            original_mkdir = Path.mkdir
            remove_links = []

            def replaced_mkdir(path, *args, **kwargs):
                result = original_mkdir(path, *args, **kwargs)
                if path == directory:
                    path.rmdir()
                    remove_links.append(self.make_directory_link(target, path))
                return result

            try:
                with patch("collection_plan.Path.mkdir", new=replaced_mkdir):
                    with self.assertRaisesRegex(ValueError, "符号链接|junction"):
                        save_plan_config(self.config, directory)
                self.assertEqual(list(target.iterdir()), [])
            finally:
                for remove_link in remove_links:
                    remove_link()

    def test_save_rejects_windows_reparse_attributes_even_without_symlink_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "not-created"
            original_lstat = Path.lstat

            def reparse_info(path, *args, **kwargs):
                if path == directory:
                    return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
                return original_lstat(path, *args, **kwargs)

            with patch("collection_plan.Path.lstat", new=reparse_info):
                with self.assertRaisesRegex(ValueError, "junction"):
                    save_plan_config(self.config, directory)
            self.assertFalse(directory.exists())

    def test_save_rejects_parent_traversal_before_normalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "possible-link" / ".." / "plans"
            with self.assertRaisesRegex(ValueError, "上级路径"):
                save_plan_config(self.config, path)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_preview_labels_do_not_change_combination_identity(self):
        specs = build_specs(self.config)
        before = copy.deepcopy(specs)
        rows = preview_rows(self.config, [specs[0], specs[-1]], pages=3)
        for row, spec in zip(rows, [specs[0], specs[-1]]):
            self.assertTrue(all(row[key] == value for key, value in spec.items()))
            self.assertEqual(row["pages_requested"], 3)
            self.assertEqual(row["degree_label"], "本科")
        self.assertEqual(rows[0]["city_label"], "上海")
        self.assertEqual(rows[0]["salary_label"], "10-20K")
        self.assertEqual(rows[0]["experience_label"], "经验不限")
        self.assertEqual(rows[1]["experience_label"], "5-10年")
        self.assertEqual(rows[1]["salary_label"], "20-50K")
        rows[0]["keyword"] = "不影响原组合"
        self.assertEqual(specs, before)

    def test_preview_rejects_unsupported_pages_and_unknown_options(self):
        spec = build_specs(self.config)[0]
        for pages in (0, 11, "10", True, None):
            with self.subTest(pages=pages), self.assertRaisesRegex(ValueError, "1到10"):
                preview_rows(self.config, [spec], pages=pages)
        with self.assertRaisesRegex(ValueError, "不存在"):
            preview_rows(self.config, [{**spec, "experience": "missing"}])


if __name__ == "__main__":
    unittest.main()
