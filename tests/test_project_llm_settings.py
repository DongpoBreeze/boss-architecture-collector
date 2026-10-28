"""Offline tests for project-only provider configuration and secret handling."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import project_llm_settings as settings
from llm_client import CodexResponsesClient, ConfigurationError, load_codex_config


class ProjectSettingsTests(unittest.TestCase):
    def test_project_settings_are_atomic_and_never_contain_the_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = settings.save_settings(path=Path(directory) / "provider.json")
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["base_url"], "https://nikoapi.xyz/v1")
            self.assertEqual(value["model"], "gpt-5.6-luna")
            self.assertEqual(value["reasoning_effort"], "max")
            self.assertEqual(value["api_mode"], settings.API_MODE_RESPONSES)
            self.assertEqual(value["chat_reasoning_verified_for"], "")
            self.assertIs(value["chat_stream"], False)
            self.assertEqual(settings.load_settings(path), settings._validated_settings(value))
            self.assertNotIn("api_key", value)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_legacy_settings_without_api_mode_default_to_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-provider.json"
            value = {
                "version": settings.SETTINGS_VERSION,
                "base_url": "https://provider.test/v1",
                "model": "legacy-model",
                "reasoning_effort": "high",
            }
            path.write_text(json.dumps(value), encoding="utf-8")
            before = path.read_bytes()
            loaded = settings.load_settings(path)
            self.assertEqual(loaded["api_mode"], settings.API_MODE_RESPONSES)
            self.assertEqual(loaded["chat_reasoning_verified_for"], "")
            self.assertIs(loaded["chat_stream"], False)
            self.assertEqual(path.read_bytes(), before, "loading old settings must not rewrite them")
            config_path = Path(directory) / "config.toml"
            config_path.write_text('api_mode = "chat_completions"\n', encoding="utf-8")
            with patch.object(settings, "load_api_key", return_value="fixture-project-key"):
                config = load_codex_config(config_path, Path(directory) / "auth.json",
                                           project_settings_path=path)
            self.assertEqual(config.api_mode, settings.API_MODE_RESPONSES)
            self.assertEqual(config.endpoint, "https://provider.test/v1/responses")

    def test_chat_reasoning_signature_normalizes_only_equivalent_api_endpoints(self):
        base = "https://provider.test/v1"
        marker = settings.chat_reasoning_signature(base, "chat-model", "high")
        self.assertRegex(marker, r"^[0-9a-f]{64}$")
        for suffix in ("", "/", "/responses", "/responses/", "/chat/completions", "/chat/completions/"):
            with self.subTest(suffix=suffix):
                self.assertEqual(settings.chat_reasoning_signature(base + suffix, "chat-model", "high"), marker)
                self.assertTrue(settings.is_chat_reasoning_verified(base + suffix, "chat-model", "high", marker))
        for changed_base, changed_model, changed_effort, stream in (
            ("https://another.test/v1", "chat-model", "high", False),
            (base + "/v2", "chat-model", "high", False),
            (base, "other-model", "high", False),
            (base, "chat-model", "max", False),
            (base, "chat-model", "high", True),
            (base, "chat-model ", "high", False),
        ):
            with self.subTest(base=changed_base, model=changed_model, effort=changed_effort, stream=stream):
                self.assertFalse(settings.is_chat_reasoning_verified(
                    changed_base, changed_model, changed_effort, marker, chat_stream=stream))
        streaming_marker = settings.chat_reasoning_signature(base, "chat-model", "none", chat_stream=True)
        self.assertTrue(settings.is_chat_reasoning_verified(base, "chat-model", "none", streaming_marker,
                                                            chat_stream=True))

    def test_malformed_or_missing_chat_reasoning_marker_fails_closed(self):
        base, model, effort = "https://provider.test/v1", "chat-model", "high"
        marker = settings.chat_reasoning_signature(base, model, effort)
        for invalid in (None, "", "invalid-private-token", "a" * 63, "a" * 65, marker.upper(), [], 0):
            with self.subTest(invalid=invalid):
                self.assertFalse(settings.is_chat_reasoning_verified(base, model, effort, invalid))
        for values in (("", model, effort), (base, " ", effort), (base, model, ""), (base, model, None)):
            with self.subTest(values=values), self.assertRaises(settings.ProjectProviderError):
                settings.chat_reasoning_signature(*values)
            self.assertFalse(settings.is_chat_reasoning_verified(*values, marker))
        with self.assertRaises(settings.ProjectProviderError):
            settings.chat_reasoning_signature(base, model, effort, chat_stream="true")
        self.assertFalse(settings.is_chat_reasoning_verified(base, model, effort, marker, chat_stream=None))

    def test_chat_verification_and_stream_options_persist_and_override_without_global_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = settings.chat_reasoning_signature("https://provider.test/v1", "chat-model", "none",
                                                        chat_stream=True)
            project = settings.save_settings(
                "https://provider.test/v1/", "chat-model", "none", path=root / "provider.json",
                api_mode=settings.API_MODE_CHAT_COMPLETIONS,
                chat_reasoning_verified_for=marker, chat_stream=True,
            )
            loaded = settings.load_settings(project)
            self.assertEqual(loaded["chat_reasoning_verified_for"], marker)
            self.assertIs(loaded["chat_stream"], True)
            config_path = root / "config.toml"
            config_path.write_text("", encoding="utf-8")
            before = project.read_bytes(), config_path.read_bytes()
            options = {"project_settings_path": project, "api_key": "fake-project-key"}
            inherited = load_codex_config(config_path, root / "auth.json", **options)
            self.assertEqual(inherited.chat_reasoning_verified_for, marker)
            self.assertIs(inherited.chat_stream, True)
            self.assertEqual(inherited.effective_reasoning_effort, "none")
            for override in ({"model": "other-model"}, {"reasoning_effort": "high"},
                             {"base_url": "https://other.test/v1"}, {"chat_stream": False},
                             {"chat_reasoning_verified_for": ""}):
                with self.subTest(override=override):
                    config = load_codex_config(config_path, root / "auth.json", **options, **override)
                    self.assertIsNone(config.effective_reasoning_effort)
            normalized = load_codex_config(config_path, root / "auth.json", **options,
                                            base_url="https://provider.test/v1/chat/completions/")
            self.assertEqual(normalized.effective_reasoning_effort, "none")
            explicit = load_codex_config(config_path, root / "auth.json", **options,
                                         chat_reasoning_verified_for=marker)
            self.assertEqual(explicit.effective_reasoning_effort, "none")
            self.assertEqual((project.read_bytes(), config_path.read_bytes()), before)
            self.assertNotIn("fake-project-key", project.read_text(encoding="utf-8") + repr(inherited.redacted()))

    def test_chat_mode_is_saved_loaded_and_can_be_overridden_per_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = settings.save_settings(
                "https://provider.test/v1/", "chat-model", "high",
                api_mode=settings.API_MODE_CHAT_COMPLETIONS, path=root / "provider.json",
            )
            value = json.loads(project.read_text(encoding="utf-8"))
            self.assertEqual(value["api_mode"], settings.API_MODE_CHAT_COMPLETIONS)
            self.assertEqual(settings.load_settings(project)["api_mode"], settings.API_MODE_CHAT_COMPLETIONS)
            self.assertNotIn("api_key", value)
            self.assertEqual(list(root.iterdir()), [project])
            config_path = root / "config.toml"
            config_path.write_text('api_mode = "responses"\n', encoding="utf-8")
            before = project.read_bytes(), config_path.read_bytes()
            with patch.object(settings, "load_api_key", return_value="fixture-project-key") as read_key:
                inherited = load_codex_config(config_path, root / "auth.json", project_settings_path=project)
                explicit = load_codex_config(config_path, root / "auth.json", project_settings_path=project,
                                             api_mode=settings.API_MODE_RESPONSES)
            self.assertEqual(read_key.call_count, 2)
            self.assertEqual(inherited.api_mode, settings.API_MODE_CHAT_COMPLETIONS)
            self.assertEqual(inherited.endpoint, "https://provider.test/v1/chat/completions")
            self.assertEqual(inherited.reasoning_effort, "high")
            self.assertEqual(explicit.api_mode, settings.API_MODE_RESPONSES)
            self.assertEqual(explicit.endpoint, "https://provider.test/v1/responses")
            self.assertEqual((project.read_bytes(), config_path.read_bytes()), before)
            self.assertNotIn("fixture-project-key", project.read_text(encoding="utf-8") + repr(inherited))

    def test_responses_reuses_the_existing_stream_key_without_changing_the_project_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = settings.save_settings(
                "https://provider.invalid/v1", "fixture-model", "high",
                api_mode=settings.API_MODE_RESPONSES, chat_stream=True, path=root / "provider.json",
            )
            config_path = root / "config.toml"
            config_path.write_text("", encoding="utf-8")
            before = project.read_bytes(), config_path.read_bytes()
            value = json.loads(project.read_text(encoding="utf-8"))
            self.assertIs(value["chat_stream"], True)
            self.assertEqual(value["api_mode"], settings.API_MODE_RESPONSES)
            self.assertNotIn("responses_stream", value)
            inherited = load_codex_config(
                config_path, root / "auth.json", project_settings_path=project, api_key="fixture-only-key")
            self.assertIs(inherited.chat_stream, True)
            self.assertEqual(inherited.api_mode, settings.API_MODE_RESPONSES)
            self.assertEqual(inherited.effective_reasoning_effort, "high")
            for source in (inherited, dict(inherited)):
                with self.subTest(source=type(source).__name__):
                    self.assertTrue(CodexResponsesClient(source).config.chat_stream)
                    self.assertFalse(CodexResponsesClient(source, chat_stream=False).config.chat_stream)
            self.assertEqual((project.read_bytes(), config_path.read_bytes()), before)
            self.assertNotIn("fixture-only-key", project.read_text(encoding="utf-8") + repr(inherited.redacted()))

    def test_settings_reject_http_urls_credentials_and_unknown_effort(self):
        for change in (
            {"base_url": "http://nikoapi.xyz/v1"},
            {"base_url": "https://user:pass@nikoapi.xyz/v1"},
            {"base_url": "https://nikoapi.xyz/v1?key=secret"},
            {"reasoning_effort": "unbounded"},
            {"api_mode": "chat"},
            {"api_mode": ""},
            {"api_mode": None},
            {"chat_reasoning_verified_for": None},
            {"chat_reasoning_verified_for": "private-token"},
            {"chat_reasoning_verified_for": "A" * 64},
            {"chat_reasoning_verified_for": "a" * 63},
            {"chat_reasoning_verified_for": []},
            {"chat_stream": None},
            {"chat_stream": "false"},
            {"chat_stream": 1},
        ):
            with self.subTest(change=change), self.assertRaises(settings.ProjectProviderError):
                settings._validated_settings({
                    "version": settings.SETTINGS_VERSION,
                    "base_url": "https://nikoapi.xyz/v1",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                    **change,
                })

    def test_project_config_overrides_only_when_explicitly_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config_path = home / "config.toml"
            auth_path = home / "auth.json"
            project_path = home / "llm-provider.json"
            config_path.write_text(
                'model_provider = "old"\n'
                '[model_providers.old]\nbase_url = "https://old.example/v1"\n'
                'experimental_bearer_token = "old-secret"\n', encoding="utf-8")
            auth_path.write_text('{"OPENAI_API_KEY":"account-secret"}', encoding="utf-8")
            settings.save_settings(path=project_path)
            with patch.object(settings, "load_api_key", return_value="project-fixture-key") as read_key:
                project = load_codex_config(config_path, auth_path, project_settings_path=project_path)
                read_key.assert_called_once_with(settings.CREDENTIAL_TARGET)
            self.assertEqual(project.provider, "nikoapi")
            self.assertEqual(project.endpoint, "https://nikoapi.xyz/v1/responses")
            self.assertEqual((project.model, project.reasoning_effort), ("gpt-5.6-luna", "max"))
            self.assertEqual(project.api_key, "project-fixture-key")
            self.assertNotIn("project-fixture-key", repr(project) + str(project.redacted()))
            generic = load_codex_config(config_path, auth_path)
            self.assertEqual(generic.endpoint, "https://old.example/v1/responses")
            self.assertEqual(generic.api_key, "old-secret")

    def test_missing_project_credential_never_falls_back_to_codex_or_account_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            auth = root / "auth.json"
            project = root / "llm-provider.json"
            config.write_text(
                '[model_providers.old]\nbase_url = "https://old.example/v1"\n'
                'experimental_bearer_token = "must-not-use"\n', encoding="utf-8")
            auth.write_text('{"OPENAI_API_KEY":"also-must-not-use"}', encoding="utf-8")
            settings.save_settings(path=project)
            with patch.object(settings, "load_api_key", return_value=None):
                with self.assertRaisesRegex(ConfigurationError, "项目 API Key 尚未配置"):
                    load_codex_config(config, auth, project_settings_path=project)

    def test_explicit_request_overrides_project_url_and_key_without_mutating_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = settings.save_settings(path=root / "provider.json")
            config_path = root / "config.toml"
            config_path.write_text("", encoding="utf-8")
            before = project.read_bytes()
            with patch.object(settings, "load_api_key", return_value="stored-project-key"):
                config = load_codex_config(
                    config_path,
                    root / "auth.json",
                    project_settings_path=project,
                    base_url="https://override.example/v1",
                    api_key="explicit-request-key",
                    model="explicit-model",
                    reasoning_effort="high",
                )
            self.assertEqual(config.endpoint, "https://override.example/v1/responses")
            self.assertEqual((config.api_key, config.model, config.reasoning_effort),
                             ("explicit-request-key", "explicit-model", "high"))
            self.assertEqual(project.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
