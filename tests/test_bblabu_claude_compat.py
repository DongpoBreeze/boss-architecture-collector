"""Offline coverage for the verified BBLabu Claude Chat configuration.

No network, local credentials, or real job data are used by these tests.
The provider's exact model ID is intentional; it is not a client-side alias.
"""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import project_llm_settings as settings
from llm_client import API_MODE_CHAT_COMPLETIONS, CodexConfig, CodexResponsesClient, load_codex_config
from review_cache import provider_signature


class FakeResponse:
    def __init__(self, payload, *, stream=False):
        self.status = 200
        self.headers = {"Content-Type": "text/event-stream" if stream else "application/json"}
        self.payload = payload

    def read(self):
        return self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode("utf-8")


class BBLabuClaudeCompatibilityTests(unittest.TestCase):
    def config(self, **overrides):
        return replace(CodexConfig(
            model="claude-opus-5-5", base_url="https://api.bblabu.ai/v1",
            api_key="fixture-not-a-real-api-key", api_mode=API_MODE_CHAT_COMPLETIONS,
            reasoning_effort="none", chat_stream=False, max_retries=0,
        ), **overrides)

    def test_project_profile_overrides_gpt_protocol_without_persisting_a_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = settings.save_settings(
                "https://api.bblabu.ai/v1", "claude-opus-5-5", "none",
                api_mode=API_MODE_CHAT_COMPLETIONS, chat_stream=False,
                path=root / "provider.json",
            )
            codex = root / "config.toml"
            codex.write_text('model = "gpt-fixture"\nmodel_reasoning_effort = "xhigh"\n'
                             'api_mode = "responses"\n', encoding="utf-8")
            before = codex.read_bytes()
            config = load_codex_config(
                codex, root / "auth.json", project_settings_path=profile,
                api_key="fixture-not-a-real-api-key",
            )
            self.assertEqual(config.model, "claude-opus-5-5")
            self.assertEqual(config.endpoint, "https://api.bblabu.ai/v1/chat/completions")
            self.assertIsNone(config.effective_reasoning_effort)
            self.assertFalse(config.chat_stream)
            self.assertEqual(codex.read_bytes(), before)
            self.assertNotIn("api_key", json.loads(profile.read_text(encoding="utf-8")))
            self.assertNotIn("fixture-not-a-real-api-key", profile.read_text(encoding="utf-8"))

    def test_plain_and_streaming_reviews_keep_the_two_field_contract(self):
        expected = {"判断": "符合", "理由": "负责 AI 应用方案设计和交付。"}
        text = json.dumps(expected, ensure_ascii=False)
        for stream in (False, True):
            with self.subTest(stream=stream):
                requests = []

                def transport(request, timeout):
                    requests.append(request)
                    choice = {"index": 0, "finish_reason": "stop"}
                    if stream:
                        payload = ("data: " + json.dumps({"choices": [{**choice, "delta": {
                            "role": "assistant", "content": text,
                        }}]}, ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode("utf-8")
                    else:
                        payload = {"model": "claude-opus-5-5", "choices": [{**choice, "message": {
                            "role": "assistant", "content": text,
                        }}]}
                    return FakeResponse(payload, stream=stream)

                # Even a stale GPT effort must not enter an unverified Chat request.
                client = CodexResponsesClient(self.config(chat_stream=stream, reasoning_effort="xhigh"),
                                              transport=transport)
                result = client.review_job({"job_id": "synthetic-ai", "jd": "负责企业 AI 应用方案及交付。"})
                self.assertEqual(result, expected)
                self.assertEqual(len(requests), 1)
                request = requests[0]
                self.assertEqual(request.full_url, "https://api.bblabu.ai/v1/chat/completions")
                self.assertEqual(request.get_header("Authorization"), "Bearer fixture-not-a-real-api-key")
                body = json.loads(request.data)
                self.assertEqual(set(body), {"model", "messages", "stream"})
                self.assertEqual(body["model"], "claude-opus-5-5")
                self.assertIs(body["stream"], stream)
                self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
                self.assertNotIn("fixture-not-a-real-api-key", request.data.decode("utf-8"))

    def test_saved_claude_profile_cannot_reuse_the_gpt_provider_identity(self):
        claude = self.config()
        previous = replace(claude, model="gpt-6.1-sol", api_mode="responses", reasoning_effort="xhigh")
        self.assertNotEqual(provider_signature(claude), provider_signature(previous))
        self.assertEqual(provider_signature(claude), provider_signature(replace(claude, reasoning_effort="max")))
        self.assertNotIn("fixture-not-a-real-api-key", repr(claude.redacted()))


if __name__ == "__main__":
    unittest.main()
