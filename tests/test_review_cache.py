"""Offline checks for the request-level cache contract shared by both paths."""

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm_client
import project_llm_settings
import review_cache as cache
from llm_client import CodexConfig, ConfigurationError


class ReviewCacheTests(unittest.TestCase):
    def setUp(self):
        self.config = CodexConfig(model="fake-model", reasoning_effort="high", base_url="https://provider.test/v1",
                                  api_key="fake-key-not-a-real-credential")
        self.job = {"job_id": "job-a", "job_description": "负责方案设计与交付", "title": "AI工程师"}
        self.provider = cache.provider_signature(self.config)
        self.signature = cache.review_signature(self.job, "Review JSON", self.provider)

    def test_deterministic_identity_uses_actual_messages_not_mapping_order(self):
        reordered = dict(reversed(list(self.job.items())))
        self.assertEqual(cache.review_signature(reordered, "Review JSON\n  ", self.provider), self.signature)
        self.assertNotEqual(cache.review_signature(self.job, " Review JSON", self.provider), self.signature)
        self.assertTrue(self.signature.startswith(cache.REVIEW_CACHE_VERSION + ":"))

    def test_all_semantic_inputs_and_actual_template_invalidate(self):
        for field, replacement in (("model", "other-model"), ("reasoning_effort", "max"),
                                   ("base_url", "https://another-provider.test/v1")):
            with self.subTest(field=field):
                provider = cache.provider_signature(replace(self.config, **{field: replacement}))
                self.assertNotEqual(cache.review_signature(self.job, "Review JSON", provider), self.signature)
        self.assertNotEqual(cache.review_signature({**self.job, "job_description": "新的完整正文"}, "Review JSON", self.provider),
                            self.signature)
        self.assertNotEqual(cache.review_signature(self.job, "A changed prompt", self.provider), self.signature)
        original = llm_client.review_request_messages
        with patch.object(llm_client, "review_request_messages", side_effect=lambda job, prompt: [
            *original(job, prompt), {"role": "system", "content": "Appended schema instructions"}]), \
             patch.object(cache, "_digest", wraps=cache._digest) as digest:
            self.assertNotEqual(cache.review_signature(self.job, "Review JSON", self.provider), self.signature)
            input_payload = next(call.args[0] for call in digest.call_args_list if "input" in call.args[0])
            self.assertEqual(input_payload["input"][-1]["content"], "Appended schema instructions")

    def test_provider_identity_stays_stable_while_new_format_successes_resume(self):
        # Fixed pre-api_mode fingerprint for the setUp provider, generated from
        # the original semantic-review-v1 identity. The response contract is
        # independently included in the input signature.
        legacy_provider = "semantic-review-v1:provider:e1bf79ecfa5b13951a5da2c0bc20700fde17782a4f0a595b503ce67bc804e15a"
        self.assertEqual(self.provider, legacy_provider)
        mapping = dict(self.config)
        mapping.pop("api_mode")
        self.assertEqual(cache.provider_signature(mapping), legacy_provider)
        self.assertEqual(cache.provider_signature(replace(self.config, api_mode=llm_client.API_MODE_RESPONSES)),
                         legacy_provider)
        legacy_signature = cache.review_signature(self.job, "Review JSON", legacy_provider)
        row = {"job_id": "job-a", "status": "success", "cache_signature": legacy_signature,
               "cache_input_signature": cache.review_input_signature(self.job, "Review JSON"),
               "review_format": llm_client.SIMPLE_REVIEW_FORMAT, "判断": "符合", "理由": "负责方案设计。"}
        selected = cache.select_cached_reviews({"reviews": [row]}, {"job-a": self.signature})
        self.assertEqual(selected.reviews, {"job-a": row})
        self.assertEqual(selected.invalidated_count, 0)

    def test_protocol_changes_invalidate_but_chat_effort_is_not_an_effective_parameter(self):
        chat = replace(self.config, api_mode=llm_client.API_MODE_CHAT_COMPLETIONS)
        chat_provider = cache.provider_signature(chat)
        self.assertEqual(chat_provider,
                         "semantic-review-v1:provider:16b0d495376984c87d1c2855a544fbdb6498e871db498a7c725b86c9eb2f8a5e")
        self.assertNotEqual(chat_provider, self.provider)
        chat_signature = cache.review_signature(self.job, "Review JSON", chat_provider)
        self.assertNotEqual(chat_signature, self.signature)
        self.assertEqual(cache.provider_signature(dict(chat)), chat_provider)
        for effort in ("minimal", "high", "max"):
            with self.subTest(effort=effort):
                self.assertEqual(cache.provider_signature(replace(chat, reasoning_effort=effort)), chat_provider)
        for change in ({"model": "different-chat-model"}, {"base_url": "https://another-provider.test/v1"}):
            with self.subTest(change=change):
                self.assertNotEqual(cache.provider_signature(replace(chat, **change)), chat_provider)
        self.assertEqual(cache.provider_signature(replace(chat, api_key="rotated-fake-key")), chat_provider)
        row = {"job_id": "job-a", "status": "success", "cache_signature": self.signature}
        selected = cache.select_cached_reviews({"reviews": [row]}, {"job-a": chat_signature})
        self.assertEqual(selected.reviews, {})
        self.assertEqual(selected.invalidated_count, 1)
        self.assertEqual(selected.previous_reviews, [{"reason": "signature_changed", "review": row}])

    def test_chat_cache_uses_verified_effective_effort_and_never_hashes_the_proof(self):
        chat = replace(self.config, api_mode=llm_client.API_MODE_CHAT_COMPLETIONS)
        default_provider = cache.provider_signature(chat)
        marker = project_llm_settings.chat_reasoning_signature(chat.base_url, chat.model, chat.reasoning_effort)
        verified = replace(chat, chat_reasoning_verified_for=marker)
        with patch.object(cache, "_digest", wraps=cache._digest) as digest:
            verified_provider = cache.provider_signature(verified)
        identity = digest.call_args.args[0]
        self.assertEqual(identity["reasoning_effort"], "high")
        self.assertNotIn(marker, json.dumps(identity))
        self.assertNotIn(verified.api_key, json.dumps(identity))
        self.assertNotEqual(verified_provider, default_provider)
        self.assertEqual(cache.provider_signature(dict(verified)), verified_provider)
        self.assertEqual(cache.provider_signature(replace(verified, api_key="rotated-fake-key")), verified_provider)
        default_signature = cache.review_signature(self.job, "Review JSON", default_provider)
        verified_signature = cache.review_signature(self.job, "Review JSON", verified_provider)
        row = {"job_id": "job-a", "status": "success", "cache_signature": default_signature}
        selected = cache.select_cached_reviews({"reviews": [row]}, {"job-a": verified_signature})
        self.assertEqual(selected.reviews, {})
        self.assertEqual(selected.invalidated_count, 1)
        self.assertEqual(selected.previous_reviews, [{"reason": "signature_changed", "review": row}])
        changed_effort = replace(verified, reasoning_effort="max")
        self.assertIsNone(changed_effort.effective_reasoning_effort)
        self.assertEqual(cache.provider_signature(changed_effort), default_provider)
        max_marker = project_llm_settings.chat_reasoning_signature(chat.base_url, chat.model, "max")
        verified_max = replace(changed_effort, chat_reasoning_verified_for=max_marker)
        self.assertNotEqual(cache.provider_signature(verified_max), verified_provider)
        self.assertNotEqual(cache.provider_signature(verified_max), default_provider)
        none_marker = project_llm_settings.chat_reasoning_signature(chat.base_url, chat.model, "none")
        verified_none = replace(chat, reasoning_effort="none", chat_reasoning_verified_for=none_marker)
        self.assertEqual(verified_none.effective_reasoning_effort, "none")
        self.assertNotEqual(cache.provider_signature(verified_none), default_provider)

    def test_chat_stream_changes_require_new_proof_but_are_not_semantic_cache_inputs(self):
        chat = replace(self.config, api_mode=llm_client.API_MODE_CHAT_COMPLETIONS)
        default_provider = cache.provider_signature(chat)
        self.assertEqual(cache.provider_signature(replace(chat, chat_stream=True)), default_provider)
        marker = project_llm_settings.chat_reasoning_signature(chat.base_url, chat.model, chat.reasoning_effort)
        verified = replace(chat, chat_reasoning_verified_for=marker)
        verified_provider = cache.provider_signature(verified)
        changed_stream = replace(verified, chat_stream=True)
        self.assertIsNone(changed_stream.effective_reasoning_effort)
        self.assertEqual(cache.provider_signature(changed_stream), default_provider)
        streaming_marker = project_llm_settings.chat_reasoning_signature(
            chat.base_url, chat.model, chat.reasoning_effort, chat_stream=True)
        streaming_verified = replace(changed_stream, chat_reasoning_verified_for=streaming_marker)
        self.assertEqual(streaming_verified.effective_reasoning_effort, "high")
        self.assertNotEqual(marker, streaming_marker)
        self.assertEqual(cache.provider_signature(streaming_verified), verified_provider)
        self.assertEqual(cache.provider_signature(dict(streaming_verified)), verified_provider)
        for invalid in (None, "", "private-token", "a" * 63, "A" * 64, [], 0):
            with self.subTest(invalid=invalid):
                config = replace(chat, chat_reasoning_verified_for=invalid)
                self.assertEqual(cache.provider_signature(config), default_provider)
                self.assertEqual(cache.provider_signature(dict(config)), default_provider)

    def test_provider_identity_excludes_credentials_and_hashes_url_before_payload(self):
        config = replace(self.config, base_url="https://user:url-secret@provider.test/v1?token=query-secret")
        with patch.object(cache, "_digest", wraps=cache._digest) as digest:
            first = cache.provider_signature(config)
        payload = json.dumps(digest.call_args.args[0])
        self.assertNotIn(config.api_key, payload)
        self.assertNotIn(config.base_url, payload)
        self.assertNotIn("url-secret", payload)
        self.assertNotIn("query-secret", payload)
        self.assertEqual(first, cache.provider_signature(replace(config, api_key="rotated-fake-key")))
        with self.assertRaises(ConfigurationError):
            cache.provider_signature(replace(config, api_key=None))
        anonymous = replace(config, api_key=None, requires_auth=False)
        self.assertEqual(first, cache.provider_signature(anonymous))

    def test_legacy_changed_failed_and_out_of_scope_records_are_not_reused(self):
        rows = [
            {"job_id": "a", "status": "success"},
            {"job_id": "b", "status": "success", "cache_signature": "old"},
            {"job_id": "c", "status": "failed", "cache_signature": self.signature},
            {"job_id": "d", "status": "success", "cache_signature": self.signature,
             "cache_input_signature": cache.review_input_signature(self.job, "Review JSON"),
             "review_format": llm_client.SIMPLE_REVIEW_FORMAT, "判断": "符合", "理由": "负责方案设计。"},
            {"job_id": "outside", "status": "success", "cache_signature": self.signature},
        ]
        selected = cache.select_cached_reviews({"reviews": rows}, dict.fromkeys("abcd", self.signature))
        self.assertEqual(list(selected.reviews), ["d"])
        self.assertEqual((selected.hit_count, selected.invalidated_count, selected.legacy_count, selected.excluded_count), (1, 2, 1, 1))
        self.assertEqual({entry["reason"] for entry in selected.previous_reviews},
                         {"missing_signature", "signature_changed", "retry_failed", "not_current_input"})
        normalized = cache.select_cached_reviews(selected.document("test-date"), {"d": self.signature})
        self.assertEqual(normalized.previous_reviews, selected.previous_reviews)
        self.assertEqual(cache.matching_reviews({"reviews": rows}, {"c": self.signature}), {"c": rows[2]})
        self.assertEqual(cache.select_cached_reviews({"reviews": rows}, {"c": self.signature}, retry_failures=False).reviews,
                         {"c": rows[2]})

    def test_unavailable_config_never_matches_even_signed_success(self):
        row = {"job_id": "a", "status": "success", "cache_signature": self.signature}
        selected = cache.select_cached_reviews({"reviews": [row]}, {"a": None})
        self.assertEqual(selected.reviews, {})
        self.assertEqual(selected.invalidated_count, 1)
        self.assertEqual(selected.previous_reviews, [{"reason": "configuration_unavailable", "review": row}])
        self.assertEqual(cache.matching_reviews({"reviews": [row]}, {"a": None}), {})

    def test_malformed_checkpoints_are_ignored_safely(self):
        for payload in (None, [], {"reviews": None}, {"reviews": [None, 12, {}]}, {"previous_reviews": 1}):
            with self.subTest(payload=payload):
                selected = cache.select_cached_reviews(payload, {"a": self.signature})
                self.assertEqual(selected.reviews, {})
                self.assertEqual(cache.matching_reviews(payload, {"a": self.signature}), {})

    def test_old_rich_and_forged_version_cannot_reuse_current_signature(self):
        old = {"job_id": "job-a", "status": "success", "cache_signature": self.signature,
               "cache_input_signature": cache.review_input_signature(self.job, "Review JSON"),
               "target_relevance": {"assessment": "direct", "evidence": ["负责方案设计"]},
               "dimensions": {}}
        for row in (old, {**old, "review_format": llm_client.SIMPLE_REVIEW_FORMAT},
                    {**old, "review_format": llm_client.SIMPLE_REVIEW_FORMAT, "判断": "符合", "理由": "相关。"},
                    {"job_id": "job-a", "status": "success", "cache_signature": self.signature,
                     "cache_input_signature": cache.review_input_signature(self.job, "Review JSON"),
                     "review_format": llm_client.SIMPLE_REVIEW_FORMAT, "判断": "符合", "理由": " "}):
            with self.subTest(row=row):
                selected = cache.select_cached_reviews({"reviews": [row], "cache_version": cache.REVIEW_CACHE_VERSION},
                                                       {"job-a": self.signature})
                self.assertEqual(selected.reviews, {})
                self.assertEqual(selected.invalidated_count, 1)
                self.assertEqual(selected.previous_reviews, [{"reason": "review_format_changed", "review": row}])
                self.assertEqual(cache.matching_reviews({"reviews": [row]}, {"job-a": self.signature}), {})

    def test_schema_and_prompt_change_invalidate_without_deleting_history(self):
        current_input = cache.review_input_signature(self.job, "Review JSON")
        with patch.object(cache, "REVIEW_RESPONSE_SCHEMA", llm_client.LEGACY_REVIEW_RESPONSE_SCHEMA):
            old_signature = cache.review_signature(self.job, "Review JSON", self.provider)
        self.assertNotEqual(old_signature, self.signature)
        self.assertNotEqual(current_input, cache.review_input_signature(self.job, "Updated rules"))
        row = {"job_id": "job-a", "status": "success", "cache_signature": old_signature, "dimensions": {}}
        old_history = {"reason": "older", "review": {"job_id": "historic"}}
        selected = cache.select_cached_reviews({"reviews": [row], "previous_reviews": [old_history]},
                                               {"job-a": self.signature})
        self.assertEqual(selected.reviews, {})
        self.assertEqual(selected.previous_reviews, [old_history, {"reason": "signature_changed", "review": row}])

    def test_new_success_missing_either_signature_cannot_resume_or_report_as_success(self):
        row = {"job_id": "job-a", "status": "success", "review_format": llm_client.SIMPLE_REVIEW_FORMAT,
               "判断": "符合", "理由": "负责方案设计。", "cache_signature": self.signature,
               "cache_input_signature": cache.review_input_signature(self.job, "Review JSON")}
        for missing in ("cache_signature", "cache_input_signature"):
            for empty in (True, False):
                broken = dict(row)
                if empty:
                    broken[missing] = ""
                else:
                    broken.pop(missing)
                with self.subTest(missing=missing, empty=empty):
                    selected = cache.select_cached_reviews({"reviews": [broken]}, {"job-a": self.signature})
                    self.assertEqual(selected.hit_count, 0)
                    self.assertEqual(selected.reviews, {})
                    self.assertEqual(selected.previous_reviews[0]["review"], broken)
                    self.assertEqual(cache.matching_reviews({"reviews": [broken]}, {"job-a": self.signature}), {})
                    self.assertEqual(cache.review_for_report(broken, self.job, "Review JSON"), (None, "missing_signature"))

    def test_new_report_validation_is_offline_and_old_records_are_explicit_legacy(self):
        row = {"job_id": "job-a", "status": "success", "review_format": llm_client.SIMPLE_REVIEW_FORMAT,
               "判断": "符合", "理由": "负责方案设计。", "cache_signature": self.signature,
               "cache_input_signature": cache.review_input_signature(self.job, "Review JSON")}
        with patch.object(llm_client, "load_codex_config", side_effect=AssertionError("must remain offline")):
            self.assertEqual(cache.review_for_report(row, self.job, "Review JSON"), (row, "input_match"))
            self.assertEqual(cache.review_for_report(row, self.job, "changed"), (None, "input_changed"))
            self.assertEqual(cache.review_for_report(row, self.job, None), (None, "prompt_unavailable"))
            unsigned = {key: value for key, value in row.items() if key not in {
                "cache_signature", "cache_input_signature"}}
            self.assertEqual(cache.review_for_report(unsigned, self.job, "Review JSON"), (None, "missing_signature"))
            legacy = {"job_id": "job-a", "status": "success", "dimensions": {},
                      "cache_signature": "old", "cache_input_signature": "old"}
            self.assertEqual(cache.review_for_report(legacy, self.job, "Review JSON"), (legacy, "legacy_unverified"))
            claimed = {**legacy, "review_format": llm_client.SIMPLE_REVIEW_FORMAT}
            self.assertEqual(cache.review_for_report(claimed, self.job, "Review JSON"), (None, "invalid_review_format"))

    def test_expected_input_signature_rejects_wrong_nonempty_missing_and_unavailable_identity(self):
        for mode in (llm_client.API_MODE_RESPONSES, llm_client.API_MODE_CHAT_COMPLETIONS):
            provider = cache.provider_signature(replace(self.config, api_mode=mode))
            combined = cache.review_signature(self.job, "Review JSON", provider)
            expected_input = cache.review_input_signature(self.job, "Review JSON")
            correct = {"job_id": "job-a", "status": "success", "review_format": llm_client.SIMPLE_REVIEW_FORMAT,
                       "判断": "符合", "理由": "负责方案设计。", "cache_signature": combined,
                       "cache_input_signature": expected_input}
            signatures, input_signatures = {"job-a": combined}, {"job-a": expected_input}
            for value in ("wrong-nonempty", "", None, expected_input):
                row = dict(correct)
                if value is None:
                    row.pop("cache_input_signature")
                else:
                    row["cache_input_signature"] = value
                with self.subTest(mode=mode, value=value):
                    payload = {"reviews": [row]}
                    selected = cache.select_cached_reviews(payload, signatures, input_signatures=input_signatures)
                    matched = cache.matching_reviews(payload, signatures, input_signatures=input_signatures)
                    if value == expected_input:
                        self.assertEqual(selected.hit_count, 1)
                        self.assertEqual(matched, {"job-a": row})
                        self.assertEqual(selected.previous_reviews, [])
                    else:
                        self.assertEqual(selected.hit_count, 0)
                        self.assertEqual(matched, {})
                        self.assertEqual(selected.previous_reviews[0]["review"], row)
                        self.assertEqual(selected.previous_reviews[0]["reason"],
                                         "input_signature_changed" if value else "missing_signature")
                        if value:
                            self.assertEqual(cache.review_for_report(row, self.job, "Review JSON"), (None, "input_changed"))
            for expected in ({}, {"job-a": None}, {"job-a": ""}):
                with self.subTest(mode=mode, expected=expected):
                    self.assertEqual(cache.select_cached_reviews({"reviews": [correct]}, signatures,
                                                                input_signatures=expected).hit_count, 0)
                    self.assertEqual(cache.matching_reviews({"reviews": [correct]}, signatures, input_signatures=expected), {})
            failed = {**correct, "status": "failed", "cache_input_signature": "wrong-nonempty"}
            self.assertEqual(cache.select_cached_reviews({"reviews": [failed]}, signatures, retry_failures=False,
                                                        input_signatures=input_signatures).reviews, {})
            self.assertEqual(cache.matching_reviews({"reviews": [failed]}, signatures, input_signatures=input_signatures), {})


if __name__ == "__main__":
    unittest.main()
