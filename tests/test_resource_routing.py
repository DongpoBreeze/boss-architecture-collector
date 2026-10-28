"""Application callers share resource defaults and never write beside a wheel."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import collect_architecture as collector
import desktop_app
import desktop_collection
import desktop_review_ui
import llm_client
import process_architecture as processor
import project_resources as resources
import review_pipeline


class ResourceRoutingTests(unittest.TestCase):
    def test_callers_use_the_same_default_config_and_prompt(self):
        for module in (collector, desktop_app, desktop_review_ui, processor, review_pipeline):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.DEFAULT_CONFIG, resources.DEFAULT_CONFIG)
                self.assertEqual(module.PROJECT_ROOT, resources.PROJECT_ROOT)
        self.assertEqual(processor.PROMPT_PATH, resources.DEFAULT_PROMPT)
        self.assertEqual(llm_client.DEFAULT_PROMPT_PATH, resources.DEFAULT_PROMPT)
        self.assertEqual(desktop_collection.PROJECT_ROOT, resources.PROJECT_ROOT)

    def test_source_defaults_keep_existing_locations_without_creating_them(self):
        self.assertTrue(resources.IS_SOURCE_CHECKOUT)
        self.assertEqual(resources.default_state_root(), PROJECT_ROOT)
        self.assertEqual(resources.DEFAULT_CONFIG, PROJECT_ROOT / "config" / "architecture_collection.json")
        self.assertEqual(resources.DEFAULT_PROMPT, PROJECT_ROOT / "prompts" / "jd_semantic_review.md")
        self.assertEqual(desktop_collection.PLAN_DIRECTORY, PROJECT_ROOT / "config" / "collection-plans")
        self.assertEqual(desktop_review_ui.REVIEW_SOURCE_CONFIGS,
                         PROJECT_ROOT / "config" / "review-source-catalogs")

    def test_collection_output_uses_writable_state_base_without_creating_it(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "new-user-state"
            with patch.object(collector, "default_state_root", return_value=state):
                result = collector.default_root()
            self.assertEqual(result.parent, state / "job-result")
            self.assertTrue(result.name.endswith("-architecture-screen-full"))
            self.assertFalse(state.exists())

    def test_legacy_discovery_checks_user_state_not_site_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            state = base / "user-state"
            source = state / "job-result" / "existing-collection"
            source.mkdir(parents=True)
            (source / "collection_manifest.json").write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {"USERPROFILE": str(base / "empty-profile")}), \
                 patch.object(desktop_app, "default_state_root", return_value=state):
                self.assertEqual(desktop_app.discover_default_root(), source)

    def test_cli_default_and_custom_config_paths_remain_compatible(self):
        self.assertEqual(collector.parse_args(["--dry-run"]).config, resources.DEFAULT_CONFIG)
        self.assertEqual(desktop_app.parse_args([]).config, resources.DEFAULT_CONFIG)
        custom = PROJECT_ROOT / "custom-plan-not-read.json"
        self.assertEqual(collector.parse_args(["--config", str(custom), "--dry-run"]).config, custom)
        self.assertEqual(desktop_app.parse_args(["--config", str(custom)]).config, custom)

    def test_read_only_defaults_still_expand_expected_search_ranges(self):
        config = collector.load_config(resources.DEFAULT_CONFIG)
        self.assertEqual(len(collector.build_specs(config)), 560)
        self.assertEqual(len(collector.build_specs(config, cities=["上海"])), 112)
        prompt = processor.load_prompt()
        self.assertTrue(prompt.strip())
        self.assertEqual(prompt, llm_client.load_review_prompt())


if __name__ == "__main__":
    unittest.main()
