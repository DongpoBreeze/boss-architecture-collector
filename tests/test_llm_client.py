import io
import http.client
import json
import os
import tempfile
import traceback
import unittest
import urllib.error
import urllib.request
import urllib.response
from contextlib import contextmanager
from dataclasses import replace
from email.message import Message
from itertools import product
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(PROJECT_ROOT))

import project_llm_settings
from llm_client import (  # noqa: E402
    APPLICATION_USER_AGENT,
    API_MODE_CHAT_COMPLETIONS,
    API_MODE_RESPONSES,
    CodexConfig,
    CodexResponsesClient,
    ConfigurationError,
    InvalidJsonResponseError,
    JsonSchemaValidationError,
    LLMHTTPError,
    LLMRateLimitError,
    LLMRequestError,
    LLMServerError,
    LLMTimeoutError,
    load_codex_config,
    normalize_review_result,
    parse_json_response,
    validate_json_schema,
    REVIEW_RESPONSE_SCHEMA as SIMPLE_REVIEW_RESPONSE_SCHEMA,
    LEGACY_REVIEW_RESPONSE_SCHEMA as REVIEW_RESPONSE_SCHEMA,
    SIMPLE_REVIEW_FORMAT,
    simple_review_payload,
)


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    def read(self):
        if isinstance(self._payload, bytes):
            return self._payload
        return json.dumps(self._payload).encode("utf-8")


def chat_sse(*chunks, done=True):
    events = ["data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks]
    if done:
        events.append("data: [DONE]\n\n")
    return "".join(events).encode("utf-8")


def responses_sse(*chunks, done=False):
    events = ["event: " + chunk["type"] + "\ndata: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
              for chunk in chunks]
    if done:
        events.append("data: [DONE]\n\n")
    return "".join(events).encode("utf-8")


def review_result(job_id="job-1"):
    """A complete, deliberately uncertain review accepted by the wire schema."""
    return {
        "job_id": job_id,
        "target_relevance": {"assessment": "insufficient", "evidence": []},
        "dimensions": {name: {"present": "unknown", "confidence": 0.0, "evidence": []}
                       for name in REVIEW_RESPONSE_SCHEMA["properties"]["dimensions"]["required"]},
        "exclusions": {name: {"present": "unknown", "evidence": []}
                       for name in REVIEW_RESPONSE_SCHEMA["properties"]["exclusions"]["required"]},
        "experience_assessment": {"minimum_years": None, "hard_conflict": "unknown", "evidence": []},
        "ability_gaps": [], "verification_items": [], "summary": "",
        "evidence_quality": "unknown", "needs_human_review": True,
    }


def review_envelope(mode, result, *, response_id="private-response-id"):
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    if mode == API_MODE_RESPONSES:
        return {"id": response_id, "status": "completed", "output_text": text}
    return {"id": response_id, "choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": text, "reasoning_content": "private-reasoning",
    }}]}


class ConversationReviewTests(unittest.TestCase):
    def client(self, mode, transport, **kwargs):
        # These tests retain explicit rich-format compatibility coverage.
        # Current/default wire behavior is exercised by SimpleReviewTests.
        kwargs.setdefault("response_schema", REVIEW_RESPONSE_SCHEMA)
        return CodexResponsesClient(
            CodexConfig(model="test", base_url="https://provider.test/v1", api_key="private-token",
                        api_mode=mode, max_retries=0), transport=transport, **kwargs,
        )

    def test_responses_conversation_initial_followup_and_safe_metadata(self):
        requests = []

        def transport(request, timeout):
            body = json.loads(request.data)
            requests.append(body)
            return FakeResponse(review_envelope(API_MODE_RESPONSES, review_result(f"job-{len(requests)}"),
                                                response_id=f"private-id-{len(requests)}"))

        client = self.client(API_MODE_RESPONSES, transport)
        self.assertIs(client.supports_conversations, True)
        state = {}
        first = client.review_job({"job_id": "job-1", "jd": "FIRST JOB"}, prompt="Original rules", conversation=state)
        self.assertEqual(state["previous_response_id"], "private-id-1")
        self.assertEqual([item["role"] for item in state["messages"]], ["system", "user", "assistant"])
        self.assertEqual(json.loads(state["messages"][-1]["content"]), review_result("job-1"))
        second = client.review_job({"job_id": "job-2", "jd": "SECOND JOB"}, prompt="Original rules", conversation=state)
        self.assertEqual(state["previous_response_id"], "private-id-2")
        self.assertEqual([item["role"] for item in state["messages"]], ["system", "user", "assistant", "user", "assistant"])
        self.assertEqual([item["role"] for item in requests[0]["input"]], ["system", "user"])
        self.assertEqual(requests[0]["input"][0]["content"], "Original rules")
        self.assertNotIn("previous_response_id", requests[0])
        self.assertIs(requests[0]["store"], True)
        self.assertEqual(requests[1]["previous_response_id"], "private-id-1")
        self.assertIs(requests[1]["store"], True)
        self.assertEqual(len(requests[1]["input"]), 1)
        self.assertEqual(requests[1]["input"][0]["role"], "user")
        self.assertIn("independent job", requests[1]["input"][0]["content"])
        self.assertIn("SECOND JOB", requests[1]["input"][0]["content"])
        self.assertNotIn("FIRST JOB", requests[1]["input"][0]["content"])
        self.assertEqual(first["_conversation"], {
            "reused": False, "request_count": 1, "format_repair_attempts": 0,
            "provider_response_id_missing": False, "client_history": False,
        })
        self.assertIs(second["_conversation"]["reused"], True)
        for result in (first, second):
            self.assertTrue(all(type(item) in {int, bool} for item in result["_conversation"].values()))
            self.assertNotIn("private-id-", json.dumps(result))
            self.assertNotIn("private-token", json.dumps(result))
            self.assertNotIn("provider.test", json.dumps(result))
            self.assertNotIn("_response_id", result)
            self.assertNotIn("messages", result)

    def test_stateless_response_id_never_changes_original_envelope_or_result(self):
        requests = []
        client = self.client(API_MODE_RESPONSES, lambda request, timeout: (
            requests.append(json.loads(request.data))
            or FakeResponse(review_envelope(API_MODE_RESPONSES, {"ok": True}))), response_schema=None)
        self.assertEqual(client.review_job("JD", prompt="Return JSON"), {"ok": True})
        self.assertEqual(set(requests[0]), {"model", "input", "store", "reasoning"})
        self.assertIs(requests[0]["store"], False)
        self.assertEqual([item["role"] for item in requests[0]["input"]], ["system", "user"])

    def test_independent_jobs_never_chain_or_replay_history_in_either_protocol(self):
        for mode in (API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS):
            with self.subTest(mode=mode):
                requests = []

                def transport(request, timeout):
                    requests.append(json.loads(request.data))
                    return FakeResponse(review_envelope(mode, review_result(f"job-{len(requests)}")))

                client = self.client(mode, transport)
                for index in (1, 2):
                    result = client.review_job({"job_id": f"job-{index}", "jd": f"PRIVATE JD {index}"},
                                               prompt="Current review rules")
                    self.assertNotIn("_conversation", result)
                for index, body in enumerate(requests, 1):
                    self.assertNotIn("previous_response_id", body)
                    self.assertNotIn("conversation", body)
                    messages = body["input" if mode == API_MODE_RESPONSES else "messages"]
                    self.assertEqual([item["role"] for item in messages], ["system", "user"])
                    self.assertIn(f"PRIVATE JD {index}", json.dumps(messages))
                    self.assertNotIn(f"PRIVATE JD {3 - index}", json.dumps(messages))
                    if mode == API_MODE_RESPONSES:
                        self.assertIs(body["store"], False)

    def test_chat_conversation_keeps_system_and_bounded_text_history(self):
        requests = []

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            return FakeResponse(review_envelope(API_MODE_CHAT_COMPLETIONS, review_result(f"job-{len(requests)}")))

        client = self.client(API_MODE_CHAT_COMPLETIONS, transport)
        state = {}
        first = client.review_job({"job_id": "job-1"}, prompt="Original rules", conversation=state)
        second = client.review_job({"job_id": "job-2"}, prompt="Original rules", conversation=state)
        self.assertEqual([item["role"] for item in requests[0]["messages"]], ["system", "user"])
        self.assertEqual([item["role"] for item in requests[1]["messages"]], ["system", "user", "assistant", "user"])
        self.assertEqual(requests[1]["messages"][0]["content"], "Original rules")
        self.assertIn("independent job", requests[1]["messages"][-1]["content"])
        self.assertIn('"job-2"', requests[1]["messages"][-1]["content"])
        self.assertFalse(first["_conversation"]["reused"])
        self.assertTrue(second["_conversation"]["reused"])
        self.assertTrue(first["_conversation"]["client_history"])
        for index in range(3, 41):
            client.review_job({"job_id": f"job-{index}"}, prompt="Original rules", conversation=state)
        self.assertLessEqual(len(state["messages"]), client.MAX_CONVERSATION_MESSAGES)
        self.assertEqual(state["messages"][0], {"role": "system", "content": "Original rules"})
        self.assertEqual(state["messages"][-1]["role"], "assistant")
        self.assertNotIn("private-reasoning", json.dumps(state))
        self.assertNotIn("private-response-id", json.dumps(state))
        self.assertTrue(all(set(item) == {"role", "content"} for item in state["messages"]))
        for body in requests:
            self.assertEqual(set(body), {"model", "messages", "stream"})
            self.assertLessEqual(len(body["messages"]), client.MAX_CONVERSATION_MESSAGES)

    def test_conversation_state_is_isolated_between_workers(self):
        for mode in (API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS):
            with self.subTest(mode=mode):
                requests = []
                jobs = iter(("a-1", "b-1", "a-2", "b-2"))

                def transport(request, timeout):
                    requests.append(json.loads(request.data))
                    job_id = next(jobs)
                    return FakeResponse(review_envelope(mode, review_result(job_id), response_id=f"private-{job_id}"))

                client = self.client(mode, transport)
                a, b = {}, {}
                for job_id, state in (("a-1", a), ("b-1", b), ("a-2", a), ("b-2", b)):
                    client.review_job({"job_id": job_id}, prompt="Rules", conversation=state)
                if mode == API_MODE_RESPONSES:
                    self.assertEqual(requests[2]["previous_response_id"], "private-a-1")
                    self.assertEqual(requests[3]["previous_response_id"], "private-b-1")
                else:
                    self.assertIn("a-1", json.dumps(requests[2]["messages"], ensure_ascii=False))
                    self.assertIn("b-1", json.dumps(requests[3]["messages"], ensure_ascii=False))
                    self.assertNotIn("b-1", json.dumps(requests[2]["messages"], ensure_ascii=False))
                    self.assertNotIn("a-1", json.dumps(requests[3]["messages"], ensure_ascii=False))
                    self.assertIsNot(a["messages"], b["messages"])

    def test_state_commits_only_after_successful_parse_and_validation(self):
        for mode in (API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS):
            for bad_output, expected_error in (("private-invalid-json", InvalidJsonResponseError),
                                               ({}, JsonSchemaValidationError)):
                with self.subTest(mode=mode, bad_output=bad_output):
                    requests = []
                    state = ({"previous_response_id": "private-committed"} if mode == API_MODE_RESPONSES else
                             {"messages": [{"role": "system", "content": "Rules"},
                                           {"role": "user", "content": "previous job"},
                                           {"role": "assistant", "content": "previous result"}]})
                    before = json.loads(json.dumps(state))

                    def transport(request, timeout):
                        requests.append(json.loads(request.data))
                        self.assertEqual(state, before)
                        return FakeResponse(review_envelope(mode, bad_output, response_id="private-uncommitted"))

                    with self.assertRaises(expected_error) as raised:
                        self.client(mode, transport).review_job({"job_id": "job-1"}, prompt="Rules", conversation=state)
                    self.assertEqual(state, before)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(raised.exception.format_repair_attempts, 1)
                    self.assertNotIn("private-invalid-json", json.dumps(requests[1]))
                    self.assertNotIn("private-invalid-json", "".join(traceback.format_exception(raised.exception)))

    def test_success_without_response_id_removes_stale_state(self):
        for missing_id in (None, "", " ", 42):
            with self.subTest(missing_id=missing_id):
                state = {"previous_response_id": "private-stale"}
                requests = []
                client = self.client(API_MODE_RESPONSES, lambda request, timeout: (
                    requests.append(json.loads(request.data)) or FakeResponse(review_envelope(
                        API_MODE_RESPONSES, review_result(f"job-{len(requests)}"), response_id=missing_id))))
                result = client.review_job({"job_id": "job-1"}, prompt="Rules", conversation=state)
                self.assertNotIn("previous_response_id", state)
                self.assertTrue(state["responses_history_only"])
                self.assertEqual([item["role"] for item in state["messages"]], ["system", "user", "assistant"])
                self.assertTrue(result["_conversation"]["provider_response_id_missing"])
                self.assertFalse(result["_conversation"]["client_history"])
                second = client.review_job({"job_id": "job-2"}, prompt="Rules", conversation=state)
                self.assertNotIn("previous_response_id", requests[-1])
                self.assertIs(requests[-1]["store"], False)
                self.assertEqual([item["role"] for item in requests[-1]["input"]], ["system", "user", "assistant", "user"])
                self.assertIn('"job-1"', requests[-1]["input"][1]["content"])
                self.assertIn('"job-2"', requests[-1]["input"][-1]["content"])
                self.assertTrue(second["_conversation"]["client_history"])
                self.assertTrue(second["_conversation"]["reused"])

    def test_failed_server_chaining_can_retry_same_job_with_committed_client_history(self):
        requests = []

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            if len(requests) == 2:
                return FakeResponse({"error": {"message": "private-provider-diagnostic"}}, status=400)
            job_id = {1: "job-1", 3: "job-2", 4: "job-3"}[len(requests)]
            return FakeResponse(review_envelope(API_MODE_RESPONSES, review_result(job_id),
                                                response_id=f"private-server-id-{len(requests)}"))

        client = self.client(API_MODE_RESPONSES, transport)
        state = {}
        client.review_job({"job_id": "job-1", "jd": "FIRST JOB"}, prompt="Rules", conversation=state)
        before = json.loads(json.dumps(state))
        with self.assertRaises(LLMHTTPError) as raised:
            client.review_job({"job_id": "job-2", "jd": "CURRENT JOB"}, prompt="Rules", conversation=state)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(state, before)
        self.assertEqual(requests[1]["previous_response_id"], "private-server-id-1")
        # Emulate the controller's single compatibility retry with the same
        # memory object, preserving every previously committed job turn.
        state["responses_history_only"] = True
        state.pop("previous_response_id", None)
        result = client.review_job({"job_id": "job-2", "jd": "CURRENT JOB"}, prompt="Rules", conversation=state)
        self.assertTrue(result["_conversation"]["client_history"])
        self.assertTrue(result["_conversation"]["reused"])
        retry = requests[2]
        self.assertIs(retry["store"], False)
        self.assertNotIn("previous_response_id", retry)
        self.assertEqual([item["role"] for item in retry["input"]], ["system", "user", "assistant", "user"])
        self.assertEqual(retry["input"][0]["content"], "Rules")
        self.assertIn("FIRST JOB", retry["input"][1]["content"])
        self.assertEqual(json.loads(retry["input"][2]["content"]), review_result("job-1"))
        self.assertIn("CURRENT JOB", retry["input"][-1]["content"])
        self.assertNotIn("private-server-id", json.dumps(retry))
        client.review_job({"job_id": "job-3"}, prompt="Rules", conversation=state)
        self.assertNotIn("previous_response_id", state)
        self.assertIs(requests[3]["store"], False)
        self.assertEqual([item["role"] for item in requests[3]["input"]],
                         ["system", "user", "assistant", "user", "assistant", "user"])
        self.assertNotIn("private-", json.dumps(state))

    def test_responses_client_history_is_bounded_and_stores_only_validated_semantic_json(self):
        requests = []

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            payload = review_result(f"job-{len(requests)}")
            payload.update({"raw_output": "private-raw", "response_id": "private-response-id"})
            payload["target_relevance"]["raw_output"] = "private-nested-raw"
            payload["dimensions"]["方案设计"]["reasoning_content"] = "private-reasoning"
            response = review_envelope(API_MODE_RESPONSES, payload)
            response["output"] = [{"type": "reasoning", "text": "private-provider-reasoning"}]
            return FakeResponse(response)

        client = self.client(API_MODE_RESPONSES, transport)
        state = {"responses_history_only": True, "previous_response_id": "private-stale"}
        for index in range(1, 41):
            result = client.review_job({"job_id": f"job-{index}"}, prompt="Original rules", conversation=state)
            self.assertTrue(result["_conversation"]["client_history"])
            self.assertEqual(result["_conversation"]["reused"], index > 1)
            self.assertNotIn("previous_response_id", state)
            self.assertIs(state["responses_history_only"], True)
            self.assertLessEqual(len(state["messages"]), client.MAX_CONVERSATION_MESSAGES)
            self.assertEqual(state["messages"][0], {"role": "system", "content": "Original rules"})
            self.assertNotIn("private-", json.dumps(state))
            for item in state["messages"]:
                self.assertEqual(set(item), {"role", "content"})
                if item["role"] == "assistant":
                    semantic = json.loads(item["content"])
                    self.assertEqual(semantic, review_result(semantic["job_id"]))
        for body in requests:
            self.assertIs(body["store"], False)
            self.assertNotIn("previous_response_id", body)
            self.assertLessEqual(len(body["input"]), client.MAX_CONVERSATION_MESSAGES)
        self.assertEqual([item["role"] for item in requests[1]["input"]], ["system", "user", "assistant", "user"])
        self.assertIn('"job-1"', requests[1]["input"][1]["content"])
        self.assertIn('"job-2"', requests[1]["input"][-1]["content"])

    def test_responses_client_history_correction_never_chains_unstored_ids(self):
        requests = []

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            if len(requests) == 1:
                payload = review_result("job-1")
            elif len(requests) == 2:
                payload = "private-invalid-reply"
            else:
                payload = {"job_id": "job-2", "target_relevance": {"assessment": "insufficient"}}
            return FakeResponse(review_envelope(API_MODE_RESPONSES, payload, response_id="private-unstored-id"))

        client = self.client(API_MODE_RESPONSES, transport)
        state = {"responses_history_only": True}
        client.review_job({"job_id": "job-1", "jd": "PREVIOUS JD"}, prompt="Rules", conversation=state)
        result = client.review_job({"job_id": "job-2", "jd": "CURRENT JD"}, prompt="Rules", conversation=state)
        self.assertEqual(result["_format_repair_attempts"], 1)
        self.assertEqual(result["_conversation"]["request_count"], 2)
        self.assertTrue(result["_conversation"]["client_history"])
        self.assertEqual([item["role"] for item in requests[2]["input"]],
                         ["system", "user", "assistant", "user", "user"])
        self.assertIn("PREVIOUS JD", requests[2]["input"][1]["content"])
        self.assertIn("CURRENT JD", requests[2]["input"][3]["content"])
        for body in requests:
            self.assertIs(body["store"], False)
            self.assertNotIn("previous_response_id", body)
            self.assertNotIn("private-invalid-reply", json.dumps(body))
        self.assertEqual([item["role"] for item in state["messages"]],
                         ["system", "user", "assistant", "user", "assistant"])
        self.assertEqual(json.loads(state["messages"][-1]["content"]), review_result("job-2"))
        self.assertNotIn("local validation", json.dumps(state))
        self.assertNotIn("private-", json.dumps(state))

    def test_responses_client_history_failed_validation_does_not_commit_current_job(self):
        requests = []

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            payload = review_result("job-1") if len(requests) == 1 else {}
            return FakeResponse(review_envelope(API_MODE_RESPONSES, payload))

        client = self.client(API_MODE_RESPONSES, transport)
        state = {"responses_history_only": True}
        client.review_job({"job_id": "job-1"}, prompt="Rules", conversation=state)
        before = json.loads(json.dumps(state))
        with self.assertRaises(JsonSchemaValidationError):
            client.review_job({"job_id": "job-2", "jd": "FAILED CURRENT JOB"}, prompt="Rules", conversation=state)
        self.assertEqual(state, before)
        self.assertNotIn("FAILED CURRENT JOB", json.dumps(state))
        self.assertNotIn("private-response-id", json.dumps(state))
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(body["store"] is False and "previous_response_id" not in body for body in requests))

    def test_responses_client_history_is_isolated_between_workers(self):
        requests = []
        jobs = iter(("a-1", "b-1", "a-2", "b-2"))

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            return FakeResponse(review_envelope(API_MODE_RESPONSES, review_result(next(jobs))))

        client = self.client(API_MODE_RESPONSES, transport)
        a, b = {"responses_history_only": True}, {"responses_history_only": True}
        for job_id, state in (("a-1", a), ("b-1", b), ("a-2", a), ("b-2", b)):
            client.review_job({"job_id": job_id}, prompt="Rules", conversation=state)
        self.assertIn("a-1", json.dumps(requests[2]["input"]))
        self.assertNotIn("b-1", json.dumps(requests[2]["input"]))
        self.assertIn("b-1", json.dumps(requests[3]["input"]))
        self.assertNotIn("a-1", json.dumps(requests[3]["input"]))
        self.assertNotIn("b-", json.dumps(a))
        self.assertNotIn("a-", json.dumps(b))
        self.assertIsNot(a["messages"], b["messages"])

    def test_format_correction_chains_retained_responses_and_commits_final_id_only(self):
        state = {"previous_response_id": "private-committed"}
        requests = []

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            self.assertEqual(state, {"previous_response_id": "private-committed"})
            payload = review_result()
            if len(requests) == 1:
                payload["dimensions"]["方案设计"]["present"] = "是"
            return FakeResponse(review_envelope(API_MODE_RESPONSES, payload,
                                                response_id=f"private-repair-{len(requests)}"))

        result = self.client(API_MODE_RESPONSES, transport).review_job(
            {"job_id": "job-1"}, prompt="Rules", conversation=state)
        self.assertEqual(requests[1]["previous_response_id"], "private-repair-1")
        self.assertEqual(len(requests[1]["input"]), 1)
        self.assertIn("present 字段只能使用 JSON 布尔值", requests[1]["input"][0]["content"])
        self.assertEqual(state["previous_response_id"], "private-repair-2")
        self.assertEqual([item["role"] for item in state["messages"]], ["system", "user", "assistant"])
        self.assertEqual(json.loads(state["messages"][-1]["content"]), review_result())
        self.assertNotIn("local validation", json.dumps(state["messages"]))
        self.assertEqual(result["_format_repair_attempts"], 1)
        self.assertEqual(result["_schema_repair_fields"], [])
        self.assertEqual(result["_conversation"]["request_count"], 2)
        self.assertEqual(result["_conversation"]["format_repair_attempts"], 1)

    def test_missing_correction_response_id_replays_current_job_from_committed_state(self):
        requests = []

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            return FakeResponse(review_envelope(API_MODE_RESPONSES,
                "private-bad-json" if len(requests) == 1 else review_result(),
                response_id=None if len(requests) == 1 else "private-final"))

        state = {"previous_response_id": "private-committed"}
        result = self.client(API_MODE_RESPONSES, transport).review_job(
            {"job_id": "job-1", "jd": "CURRENT JD"}, prompt="Rules", conversation=state)
        self.assertEqual(requests[1]["previous_response_id"], "private-committed")
        self.assertIn("CURRENT JD", requests[1]["input"][0]["content"])
        self.assertEqual(state["previous_response_id"], "private-final")
        self.assertEqual([item["role"] for item in state["messages"]], ["system", "user", "assistant"])
        self.assertEqual(result["_conversation"]["request_count"], 2)

    def test_stateless_format_correction_does_not_chain_a_store_false_response(self):
        for retained in (False, True):
            with self.subTest(retained=retained):
                requests = []

                def transport(request, timeout):
                    requests.append(json.loads(request.data))
                    result = "private-invalid-output" if len(requests) == 1 else {"ok": True}
                    return FakeResponse(review_envelope(API_MODE_RESPONSES, result, response_id="private-provider-id"))

                config = CodexConfig(base_url="https://provider.test/v1", api_key="private-token", max_retries=0,
                                     disable_response_storage=not retained)
                client = CodexResponsesClient(config, transport=transport, response_schema=None)
                self.assertEqual(client.review_job("CURRENT JD", prompt="Rules"), {
                    "ok": True, "_format_repair_attempts": 1, "_schema_repair_fields": [],
                })
                self.assertIs(requests[0]["store"], retained)
                self.assertIs(requests[1]["store"], retained)
                if retained:
                    self.assertEqual(requests[1]["previous_response_id"], "private-provider-id")
                    self.assertEqual([item["role"] for item in requests[1]["input"]], ["user"])
                else:
                    self.assertNotIn("previous_response_id", requests[1])
                    self.assertEqual([item["role"] for item in requests[1]["input"]], ["system", "user"])
                    self.assertIn("CURRENT JD", requests[1]["input"][1]["content"])
                self.assertNotIn("private-invalid-output", json.dumps(requests[1]))

    def test_chat_correction_commits_only_job_input_and_canonical_validated_json(self):
        requests = []
        state = {}

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            self.assertEqual(state, {})
            output = "private-invalid-output" if len(requests) == 1 else {
                "job_id": "job-1", "target_relevance": {"assessment": "insufficient"},
            }
            return FakeResponse(review_envelope(API_MODE_CHAT_COMPLETIONS, output))

        result = self.client(API_MODE_CHAT_COMPLETIONS, transport).review_job(
            {"job_id": "job-1"}, prompt="Rules", conversation=state)
        self.assertEqual([item["role"] for item in requests[1]["messages"]], ["system", "user", "user"])
        self.assertIn('"job-1"', requests[1]["messages"][1]["content"])
        self.assertNotIn("private-invalid-output", json.dumps(requests[1]))
        self.assertNotIn("private-invalid-output", json.dumps(state))
        self.assertNotIn("private-reasoning", json.dumps(state))
        self.assertNotIn("local validation", json.dumps(state))
        self.assertNotIn("_schema_repair_fields", json.dumps(state))
        self.assertNotIn("_conversation", json.dumps(state))
        self.assertEqual([item["role"] for item in state["messages"]], ["system", "user", "assistant"])
        self.assertEqual(json.loads(state["messages"][-1]["content"]), review_result())
        self.assertEqual(result["_conversation"]["request_count"], 2)

    def test_chat_replays_only_canonical_semantic_results_without_raw_extra_fields(self):
        requests = []

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            payload = review_result(f"job-{len(requests)}")
            payload.update({"raw_output": "private-raw", "response_id": "private-model-id",
                            "unrequested": "private-extra"})
            payload["target_relevance"]["raw_response"] = "private-nested-raw"
            payload["dimensions"]["方案设计"]["reasoning_content"] = "private-nested-reasoning"
            # Markdown is allowed on the wire, but is not committed/replayed.
            return FakeResponse(review_envelope(API_MODE_CHAT_COMPLETIONS,
                "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"))

        client = self.client(API_MODE_CHAT_COMPLETIONS, transport)
        state = {}
        client.review_job({"job_id": "job-1", "jd": "FIRST JD"}, prompt="Old rules", conversation=state)
        self.assertEqual(json.loads(state["messages"][-1]["content"]), review_result("job-1"))
        self.assertNotIn("private-", json.dumps(state))
        self.assertNotIn("```", state["messages"][-1]["content"])
        client.review_job({"job_id": "job-2", "jd": "CURRENT JD"}, prompt="Current rules", conversation=state)
        replay = requests[1]["messages"]
        self.assertEqual([item["role"] for item in replay], ["system", "user", "assistant", "user"])
        self.assertEqual(replay[0], {"role": "system", "content": "Current rules"})
        self.assertIn("FIRST JD", replay[1]["content"])
        self.assertEqual(json.loads(replay[2]["content"]), review_result("job-1"))
        self.assertIn("CURRENT JD", replay[3]["content"])
        self.assertNotIn("private-", json.dumps(replay))
        self.assertNotIn("Old rules", json.dumps(replay))
        self.assertNotIn("private-", json.dumps(state))
        self.assertEqual(json.loads(state["messages"][-1]["content"]), review_result("job-2"))

    def test_chat_failed_correction_keeps_committed_history_atomic_for_next_job(self):
        requests = []
        state = {}
        committed_before_failure = None

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            if len(requests) in (2, 3):
                self.assertEqual(state, committed_before_failure)
            payload = {1: review_result("job-1"), 2: "private-invalid-output", 3: {},
                       4: review_result("job-3")}[len(requests)]
            return FakeResponse(review_envelope(API_MODE_CHAT_COMPLETIONS, payload))

        client = self.client(API_MODE_CHAT_COMPLETIONS, transport)
        client.review_job({"job_id": "job-1", "jd": "COMMITTED JD"}, prompt="Rules", conversation=state)
        committed_before_failure = json.loads(json.dumps(state))
        with self.assertRaises(JsonSchemaValidationError):
            client.review_job({"job_id": "job-2", "jd": "FAILED JD"}, prompt="Rules", conversation=state)
        self.assertEqual(state, committed_before_failure)
        self.assertNotIn("FAILED JD", json.dumps(state))
        client.review_job({"job_id": "job-3", "jd": "NEXT JD"}, prompt="Rules", conversation=state)
        replay = requests[-1]["messages"]
        self.assertEqual([item["role"] for item in replay], ["system", "user", "assistant", "user"])
        self.assertIn("COMMITTED JD", replay[1]["content"])
        self.assertEqual(json.loads(replay[2]["content"]), review_result("job-1"))
        self.assertIn("NEXT JD", replay[-1]["content"])
        self.assertNotIn("FAILED JD", json.dumps(replay))
        self.assertNotIn("local validation", json.dumps(replay))
        self.assertNotIn("private-", json.dumps(replay))
        self.assertEqual([item["role"] for item in state["messages"]],
                         ["system", "user", "assistant", "user", "assistant"])

    def test_wrong_job_id_is_corrected_or_typed_failure_never_relabelled(self):
        for mode, corrected in product((API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS), (False, True)):
            with self.subTest(mode=mode, corrected=corrected):
                requests = []
                state = {}

                def transport(request, timeout):
                    requests.append(json.loads(request.data))
                    job_id = "job-1" if len(requests) == 2 and corrected else "private-previous-job"
                    return FakeResponse(review_envelope(mode, review_result(job_id)))

                client = self.client(mode, transport)
                if corrected:
                    result = client.review_job({"job_id": "job-1"}, prompt="Rules", conversation=state)
                    self.assertEqual(result["job_id"], "job-1")
                    self.assertEqual(result["_format_repair_attempts"], 1)
                else:
                    with self.assertRaisesRegex(JsonSchemaValidationError, r"job_id") as raised:
                        client.review_job({"job_id": "job-1"}, prompt="Rules", conversation=state)
                    self.assertEqual(state, {})
                    self.assertNotIn("private-previous-job", str(raised.exception))
                self.assertEqual(len(requests), 2)
                self.assertNotIn("private-previous-job", json.dumps(requests[1]))

    def test_empty_or_arbitrary_objects_cannot_default_into_successful_reviews(self):
        for payload in ({}, {"job_id": "job-1"}, {"job_id": "job-1", "summary": "arbitrary text"},
                        {"job_id": "job-1", "dimensions": {}}, {"unrelated": True},
                        {"job_id": "job-1", "target_relevance": {"assessment": []}}):
            with self.subTest(payload=payload):
                requests = []
                state = {}
                client = self.client(API_MODE_RESPONSES, lambda request, timeout: (
                    requests.append(request) or FakeResponse(review_envelope(API_MODE_RESPONSES, payload))))
                with self.assertRaises(JsonSchemaValidationError):
                    client.review_job({"job_id": "job-1"}, prompt="Rules", conversation=state)
                self.assertEqual(len(requests), 2)
                self.assertEqual(state, {})

    def test_partial_recognized_correction_can_be_conservatively_normalized(self):
        requests = []

        def transport(request, timeout):
            requests.append(request)
            payload = {} if len(requests) == 1 else {
                "target_relevance": {"assessment": "insufficient"},
            }
            return FakeResponse(review_envelope(API_MODE_RESPONSES, payload))

        result = self.client(API_MODE_RESPONSES, transport).review_job({"job_id": "job-1"}, prompt="Rules")
        self.assertEqual(result["job_id"], "job-1")
        self.assertIs(result["needs_human_review"], True)
        self.assertEqual(result["dimensions"]["方案设计"]["present"], "unknown")
        self.assertIn("$.job_id", result["_schema_repair_fields"])
        validate_json_schema(result, REVIEW_RESPONSE_SCHEMA)

    def test_normalizer_does_not_silently_relabel_another_jobs_review(self):
        with self.assertRaisesRegex(JsonSchemaValidationError, r"job_id"):
            normalize_review_result(review_result("private-other-job"), expected_job_id="job-1")
        with self.assertRaises(JsonSchemaValidationError):
            normalize_review_result({}, expected_job_id="job-1")

    def test_internal_provider_fields_and_forged_metadata_never_escape_results(self):
        for mode in (API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS):
            with self.subTest(mode=mode):
                payload = review_result()
                payload.update({"_response_id": "private-provider-id", "response_id": "private-provider-id",
                                "messages": [{"content": "private-history"}], "raw_output": "private-raw",
                                "_conversation": {"bad": "private-provider-id"},
                                "_format_repair_attempts": "private-provider-id",
                                "_schema_repair_fields": ["private-provider-id"]})
                result = self.client(mode, lambda request, timeout: FakeResponse(review_envelope(mode, payload))).review_job(
                    {"job_id": "job-1"}, prompt="Rules", conversation={})
                self.assertNotIn("private-", json.dumps(result))
                self.assertNotIn("_format_repair_attempts", result)
                self.assertNotIn("_schema_repair_fields", result)
                self.assertTrue(all(type(item) in {int, bool} for item in result["_conversation"].values()))

    def test_transport_errors_preserve_type_status_and_state_without_format_retries(self):
        for mode, repair_first in product((API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS), (False, True)):
            with self.subTest(mode=mode, repair_first=repair_first):
                requests = []
                state = {"previous_response_id": "private-committed"} if mode == API_MODE_RESPONSES else {}
                before = dict(state)

                def transport(request, timeout):
                    requests.append(request)
                    if repair_first and len(requests) == 1:
                        return FakeResponse(review_envelope(mode, "private-invalid-output"))
                    return FakeResponse({"error": "private-token"}, status=403)

                with self.assertRaises(LLMHTTPError) as raised:
                    self.client(mode, transport).review_job({"job_id": "job-1"}, prompt="Rules", conversation=state)
                self.assertEqual(raised.exception.status_code, 403)
                self.assertEqual(raised.exception.format_repair_attempts, int(repair_first))
                self.assertEqual(len(requests), 2 if repair_first else 1)
                self.assertEqual(state, before)
                self.assertNotIn("private-token", "".join(traceback.format_exception(raised.exception)))
                self.assertNotIn("provider.test", "".join(traceback.format_exception(raised.exception)))


class SimpleReviewTests(unittest.TestCase):
    def client(self, mode, transport):
        return CodexResponsesClient(CodexConfig(
            model="test", base_url="https://provider.test/v1", api_key="private-token",
            api_mode=mode, max_retries=0), transport=transport)

    def test_default_wire_prompt_and_validator_have_only_two_fields_without_new_api_requirements(self):
        for mode in (API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS):
            with self.subTest(mode=mode):
                requests = []

                def transport(request, timeout):
                    body = json.loads(request.data)
                    # Existing third-party text-JSON providers may not support
                    # response_format/text.format. Do not force that capability.
                    self.assertNotIn("response_format", body)
                    self.assertNotIn("text", body)
                    requests.append(body)
                    return FakeResponse(review_envelope(mode, {"判断": "符合", "理由": "负责AI应用方案与交付。"}))

                client = self.client(mode, transport)
                for key in ("one", "two"):
                    result = client.review_job({"job_id": key, "responsibilities": key})
                    self.assertEqual(set(result), {"判断", "理由"})
                self.assertEqual(len(requests), 2)
                for index, body in enumerate(requests):
                    schema = client.response_schema
                    self.assertEqual(set(schema["properties"]), {"判断", "理由"})
                    self.assertEqual(set(schema["required"]), {"判断", "理由"})
                    self.assertFalse(schema["additionalProperties"])
                    self.assertEqual(tuple(schema["properties"]["判断"]["enum"]), ("符合", "不符合", "信息不足"))
                    self.assertNotIn("job_id", schema["properties"])
                    self.assertNotIn("previous_response_id", body)
                    messages = body["input" if mode == API_MODE_RESPONSES else "messages"]
                    protocol = messages[0]["content"].split("## 严格 JSON 输出", 1)[1]
                    example = json.loads(protocol.strip().splitlines()[-1])
                    self.assertEqual(set(example), {"判断", "理由"})
                    self.assertEqual([message["role"] for message in messages], ["system", "user"])
                    self.assertIn(("one", "two")[index], messages[-1]["content"])
                    self.assertNotIn(("two", "one")[index], messages[-1]["content"])

    def test_invalid_rich_missing_extra_enum_and_blank_outputs_require_real_correction(self):
        invalid_values = [review_result(), {}, {"判断": "符合"}, {"理由": "工作内容相关。"},
                          {"判断": "是", "理由": "相关。"}, {"判断": "符合", "理由": ""},
                          {"判断": "符合", "理由": " \n\t"}, {"判断": "符合", "理由": 1},
                          {"判断": "符合", "理由": "相关。", "job_id": "job-1"}]
        for mode, invalid in product((API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS), invalid_values):
            with self.subTest(mode=mode, invalid=invalid):
                for corrected in (False, True):
                    requests = []
                    valid = {"判断": "信息不足", "理由": "工作职责描述不足，需进一步核实。"}

                    def transport(request, timeout):
                        requests.append(json.loads(request.data))
                        return FakeResponse(review_envelope(mode, valid if corrected and len(requests) == 2 else invalid))

                    client = self.client(mode, transport)
                    if corrected:
                        result = client.review_job({"job_id": "job-1"}, prompt="两字段规则")
                        self.assertEqual({name: result[name] for name in valid}, valid)
                        self.assertEqual(result["_format_repair_attempts"], 1)
                        self.assertEqual(result["_schema_repair_fields"], [])
                        self.assertNotIn("dimensions", result)
                    else:
                        with self.assertRaises(JsonSchemaValidationError) as raised:
                            client.review_job({"job_id": "job-1"}, prompt="两字段规则")
                        self.assertEqual(raised.exception.format_repair_attempts, 1)
                    self.assertEqual(len(requests), 2)
                    for body in requests:
                        self.assertNotIn("text", body)
                        self.assertNotIn("response_format", body)

    def test_local_checkpoint_validation_requires_content_not_version_claim(self):
        for judgment in ("符合", "不符合", "信息不足"):
            payload = {"判断": judgment, "理由": "根据工作内容判断。"}
            self.assertEqual(simple_review_payload({**payload, "job_id": "local-id", "status": "success",
                                                   "review_format": SIMPLE_REVIEW_FORMAT}), payload)
            validate_json_schema(payload, SIMPLE_REVIEW_RESPONSE_SCHEMA)
        for payload in ({"review_format": SIMPLE_REVIEW_FORMAT},
                        {**review_result(), "review_format": SIMPLE_REVIEW_FORMAT},
                        {"判断": "符合", "理由": "相关。", "dimensions": {}}):
            with self.subTest(payload=payload), self.assertRaises(JsonSchemaValidationError):
                simple_review_payload(payload)

    def test_history_contains_only_two_field_model_judgment(self):
        for mode in (API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS):
            state = {}
            client = self.client(mode, lambda request, timeout: FakeResponse(review_envelope(
                mode, {"判断": "不符合", "理由": "主业为代码开发。"})))
            client.review_job({"job_id": "local-only"}, prompt="规则", conversation=state)
            self.assertEqual(json.loads(state["messages"][-1]["content"]),
                             {"判断": "不符合", "理由": "主业为代码开发。"})


class DefaultTransportTests(unittest.TestCase):
    """Exercise the production opener, replacing only its socket handlers."""

    fake_key = "fixture-only-redirect-key"
    private_body = "fixture-only-private-redirect-body"

    @staticmethod
    def operations(mode):
        common = ("review_job", "list_models", "test_connection")
        return (*common, "test_chat_reasoning_effort") if mode == API_MODE_CHAT_COMPLETIONS else common

    @staticmethod
    def invoke(client, operation):
        if operation == "review_job":
            return client.review_job({"job_id": "fixture-job"}, prompt="Fixture-only rules.")
        return getattr(client, operation)()

    @contextmanager
    def memory_http(self, mode, *, status=200, location=None, stream=False):
        requests, responses = [], []
        build_opener = urllib.request.build_opener
        private_body = self.private_body

        class MemoryHTTP(urllib.request.BaseHandler):
            handler_order = 100

            def https_open(self, request):
                requests.append(request)
                headers = Message()
                headers["Content-Type"] = "application/json"
                # A redirect destination would return a valid success, making
                # the test fail if urllib ever follows Location again.
                response_status = status if len(requests) == 1 else 200
                if 300 <= response_status < 400:
                    if location is not None:
                        headers["Location"] = location
                    raw = private_body.encode("utf-8")
                elif request.full_url.endswith("/models"):
                    raw = json.dumps({"data": [{"id": "fixture-model"}]}).encode("utf-8")
                else:
                    result = {"判断": "符合", "理由": "离线复核。"}
                    if stream:
                        headers["Content-Type"] = "text/event-stream"
                        if mode == API_MODE_RESPONSES:
                            raw = responses_sse({"type": "response.completed",
                                                 "response": review_envelope(mode, result)})
                        else:
                            raw = chat_sse({"choices": [{"index": 0, "delta": {
                                "role": "assistant", "content": json.dumps(result, ensure_ascii=False),
                            }, "finish_reason": "stop"}]})
                    else:
                        raw = json.dumps(review_envelope(mode, result), ensure_ascii=False).encode("utf-8")
                response = urllib.response.addinfourl(io.BytesIO(raw), headers, request.full_url,
                                                    code=response_status)
                response.msg = private_body
                responses.append(response)
                return response

            http_open = https_open

        # Disable proxy discovery and guard the real sockets as well. No model
        # transport is injected; the production redirect handlers remain live.
        with patch("llm_client.urllib.request.build_opener", side_effect=lambda *handlers: build_opener(
                urllib.request.ProxyHandler({}), MemoryHTTP(), *handlers)), \
             patch("llm_client.urllib.request._opener", None), \
             patch("http.client.HTTPConnection.connect", side_effect=AssertionError("no network")), \
             patch("http.client.HTTPSConnection.connect", side_effect=AssertionError("no network")), \
             patch("llm_client.load_codex_config", side_effect=AssertionError("no personal configuration")):
            yield requests, responses

    def test_default_transport_rejects_redirects_before_forwarding_authorization(self):
        targets = (
            "/moved?token=fixture-only-private-target",
            "https://other-provider.invalid/moved?token=fixture-only-private-target",
            "http://provider.invalid/moved?token=fixture-only-private-target",
            "http://other-provider.invalid/moved?token=fixture-only-private-target",
            "https://provider.invalid:444/moved?token=fixture-only-private-target",
        )
        operations = [(mode, operation) for mode in (API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS)
                      for operation in self.operations(mode)]
        for (mode, operation), status, target in product(operations, (301, 302, 303, 307, 308), targets):
            with self.subTest(mode=mode, operation=operation, status=status, target=target):
                config = CodexConfig(model="fixture-model", base_url="https://provider.invalid/v1",
                                     api_key=self.fake_key, api_mode=mode, max_retries=3, timeout=0.75)
                with self.memory_http(mode, status=status, location=target) as (requests, responses), \
                     patch("llm_client.time.sleep") as sleep:
                    client = CodexResponsesClient(config)
                    with self.assertRaises(LLMHTTPError) as raised:
                        self.invoke(client, operation)
                self.assertEqual(raised.exception.status_code, status)
                self.assertIn("不允许 API 重定向", str(raised.exception))
                diagnostic = "".join(traceback.format_exception(raised.exception))
                for secret in (self.fake_key, self.private_body, "fixture-only-private-target", target):
                    self.assertNotIn(secret, diagnostic)
                self.assertEqual(len(requests), 1, "the redirect destination must receive no request")
                self.assertEqual(requests[0].get_header("Authorization"), f"Bearer {self.fake_key}")
                self.assertEqual(requests[0].timeout, config.timeout)
                self.assertTrue(all(response.closed for response in responses))
                self.assertEqual(client.config, config)
                sleep.assert_not_called()

    def test_redirect_location_is_never_parsed_or_exposed(self):
        for target in (None, "http://[fixture-only-private-target", "file:///fixture-only-private-target"):
            with self.subTest(target=target):
                config = CodexConfig(base_url="https://provider.invalid/v1", api_key=self.fake_key)
                with self.memory_http(API_MODE_RESPONSES, status=302, location=target) as (requests, responses):
                    with self.assertRaises(LLMHTTPError) as raised:
                        CodexResponsesClient(config).list_models()
                self.assertEqual(raised.exception.status_code, 302)
                self.assertIn("不允许 API 重定向", str(raised.exception))
                self.assertNotIn("fixture-only-private-target", "".join(traceback.format_exception(raised.exception)))
                self.assertEqual(len(requests), 1)
                self.assertTrue(all(response.closed for response in responses))

    def test_default_transport_still_accepts_each_api_and_stream(self):
        for mode, stream in ((API_MODE_RESPONSES, False), (API_MODE_RESPONSES, True),
                             (API_MODE_CHAT_COMPLETIONS, False),
                             (API_MODE_CHAT_COMPLETIONS, True)):
            for operation in self.operations(mode):
                with self.subTest(mode=mode, stream=stream, operation=operation):
                    config = CodexConfig(model="fixture-model", base_url="https://provider.invalid/v1",
                                         api_key=self.fake_key, api_mode=mode, chat_stream=stream)
                    with self.memory_http(mode, stream=stream) as (requests, responses):
                        result = self.invoke(CodexResponsesClient(config), operation)
                    expected = ({"判断": "符合", "理由": "离线复核。"} if operation == "review_job" else
                                ["fixture-model"] if operation == "list_models" else True)
                    self.assertEqual(result, expected)
                    self.assertEqual(len(requests), 1)
                    self.assertTrue(all(response.closed for response in responses))

    def test_injected_redirect_responses_are_closed_without_parsing_their_body(self):
        for mode, status in product((API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS),
                                    (300, 301, 302, 303, 304, 307, 308, 399)):
            with self.subTest(mode=mode, status=status):
                response = FakeResponse({}, status=status)
                with patch.object(response, "read", side_effect=AssertionError("do not read redirect body")) as read:
                    response.close = lambda: None
                    with patch.object(response, "close") as close, patch("llm_client.time.sleep") as sleep:
                        client = CodexResponsesClient(
                            CodexConfig(api_key=self.fake_key, api_mode=mode, max_retries=3),
                            transport=lambda request, timeout: response,
                        )
                        with self.assertRaises(LLMHTTPError) as raised:
                            client.test_connection()
                    self.assertEqual(raised.exception.status_code, status)
                    read.assert_not_called()
                    close.assert_called_once()
                    sleep.assert_not_called()


class LLMClientTests(unittest.TestCase):
    def test_review_retries_once_for_structural_output_and_records_repair_count(self):
        from process_architecture import DIMENSIONS

        valid = {
            "job_id": "job-1",
            "target_relevance": {"assessment": "insufficient", "evidence": []},
            "dimensions": {name: {"present": "unknown", "confidence": 0.0, "evidence": []}
                           for name in DIMENSIONS},
            "exclusions": {name: {"present": "unknown", "evidence": []}
                            for name in ("pure_sales", "pure_algorithm", "pure_infrastructure", "irrelevant")},
            "experience_assessment": {"minimum_years": None, "hard_conflict": "unknown", "evidence": []},
            "ability_gaps": [], "verification_items": [], "summary": "",
            "evidence_quality": "unknown", "needs_human_review": True,
        }
        attempts = []

        def transport(request, timeout):
            attempts.append(json.loads(request.data))
            payload = {**valid, "dimensions": dict(valid["dimensions"])}
            if len(attempts) == 1:
                payload["dimensions"]["方案设计"]["present"] = "是"
            return FakeResponse({"output_text": json.dumps(payload, ensure_ascii=False)})

        client = CodexResponsesClient(
            CodexConfig(model="test", base_url="https://provider.test/v1", api_key="secret", max_retries=0),
            transport=transport, response_schema=REVIEW_RESPONSE_SCHEMA,
        )
        result = client.review_job({"job_id": "job-1"}, prompt="Return a structured review")
        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(result["dimensions"]["方案设计"]["present"], "unknown")
        self.assertEqual(result["_format_repair_attempts"], 1)
        self.assertEqual(result["_schema_repair_fields"], ["$.dimensions.方案设计.present"])
        self.assertEqual(len(attempts), 2)
        self.assertIn("present 字段只能使用 JSON 布尔值 true/false", attempts[1]["input"][0]["content"])

    def test_review_schema_rejects_wrong_types_but_accepts_null_years(self):
        from process_architecture import DIMENSIONS

        value = {
            "job_id": "job-1",
            "target_relevance": {"assessment": "insufficient", "evidence": []},
            "dimensions": {name: {"present": "unknown", "confidence": 0, "evidence": []}
                           for name in DIMENSIONS},
            "exclusions": {name: {"present": "unknown", "evidence": []}
                            for name in ("pure_sales", "pure_algorithm", "pure_infrastructure", "irrelevant")},
            "experience_assessment": {"minimum_years": None, "hard_conflict": "unknown", "evidence": []},
            "ability_gaps": [], "verification_items": [], "summary": "",
            "evidence_quality": "unknown", "needs_human_review": True,
        }
        validate_json_schema(value, REVIEW_RESPONSE_SCHEMA)
        value["dimensions"]["方案设计"]["confidence"] = "0.8"
        with self.assertRaisesRegex(JsonSchemaValidationError, r"dimensions\.方案设计\.confidence"):
            validate_json_schema(value, REVIEW_RESPONSE_SCHEMA)

    def test_load_config_and_auth_with_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.toml").write_text(
                'model_provider = "custom"\n'
                'model = "config-model"\n'
                'model_reasoning_effort = "high"\n'
                '[model_providers.custom]\n'
                'base_url = "http://provider.test/v1"\n'
                'requires_openai_auth = true\n',
                encoding="utf-8",
            )
            (root / "auth.json").write_text('{"OPENAI_API_KEY": "secret-token"}', encoding="utf-8")
            config = load_codex_config(
                root / "config.toml",
                root / "auth.json",
                model="override-model",
                reasoning_effort="low",
            )
            self.assertEqual(config.model, "override-model")
            self.assertEqual(config.reasoning_effort, "low")
            self.assertEqual(config.endpoint, "http://provider.test/v1/responses")
            self.assertEqual(config.api_mode, API_MODE_RESPONSES)
            self.assertEqual(config.api_key, "secret-token")
            self.assertNotIn("secret-token", repr(config))
            self.assertNotIn("secret-token", str(config.redacted()))

    def test_api_mode_flows_from_config_dataclass_and_mapping_with_explicit_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"
            path.write_text(
                'model_provider = "custom"\napi_mode = "responses"\n'
                '[model_providers.custom]\nbase_url = "https://provider.test/v1"\n'
                'requires_openai_auth = false\napi_mode = "chat_completions"\n',
                encoding="utf-8",
            )
            before = path.read_bytes()
            config = load_codex_config(path, root / "auth.json")
            self.assertEqual(config.api_mode, API_MODE_CHAT_COMPLETIONS)
            self.assertEqual(config.endpoint, "https://provider.test/v1/chat/completions")
            self.assertEqual(dict(config)["api_mode"], API_MODE_CHAT_COMPLETIONS)
            self.assertEqual(config.redacted()["api_mode"], API_MODE_CHAT_COMPLETIONS)
            override = load_codex_config(path, root / "auth.json", api_mode=API_MODE_RESPONSES)
            self.assertEqual(override.api_mode, API_MODE_RESPONSES)
            self.assertEqual(override.endpoint, "https://provider.test/v1/responses")
            for source in (config, dict(config)):
                with self.subTest(source=type(source).__name__):
                    inherited = CodexResponsesClient(source, response_schema=None)
                    overridden = CodexResponsesClient(source, api_mode=API_MODE_RESPONSES,
                                                       response_schema=None)
                    self.assertEqual(inherited.config.api_mode, API_MODE_CHAT_COMPLETIONS)
                    self.assertEqual(overridden.config.api_mode, API_MODE_RESPONSES)
                    self.assertEqual(overridden.config.endpoint, "https://provider.test/v1/responses")
            direct = CodexResponsesClient(config_path=path, auth_path=root / "auth.json",
                                          api_mode=API_MODE_RESPONSES, response_schema=None)
            self.assertEqual(direct.config.api_mode, API_MODE_RESPONSES)
            self.assertEqual(path.read_bytes(), before)

    def test_api_endpoints_accept_either_protocol_suffix_without_double_appending(self):
        for base, mode in product(
            ("https://provider.test/v1", "https://provider.test/v1/",
             "https://provider.test/v1/responses/", "https://provider.test/v1/chat/completions/"),
            (API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS),
        ):
            with self.subTest(base=base, mode=mode):
                config = CodexConfig(base_url=base, api_mode=mode)
                self.assertEqual(config.responses_endpoint, "https://provider.test/v1/responses")
                self.assertEqual(config.chat_completions_endpoint, "https://provider.test/v1/chat/completions")
                self.assertEqual(config.endpoint, config.chat_completions_endpoint
                                 if mode == API_MODE_CHAT_COMPLETIONS else config.responses_endpoint)
        self.assertEqual(CodexConfig().api_mode, API_MODE_RESPONSES)
        self.assertEqual(CodexResponsesClient({"base_url": "https://provider.test/v1"}).config.api_mode,
                         API_MODE_RESPONSES)
        for source in (CodexConfig(), {"api_mode": API_MODE_RESPONSES}):
            self.assertEqual(CodexResponsesClient(source, api_mode=API_MODE_CHAT_COMPLETIONS).config.api_mode,
                             API_MODE_CHAT_COMPLETIONS)

    def test_invalid_api_mode_is_rejected_without_echoing_configuration(self):
        for build in (
            lambda: CodexConfig(api_mode="invalid-private-token"),
            lambda: CodexResponsesClient({"api_mode": "invalid-private-token", "api_key": "secret-token"}),
            lambda: CodexResponsesClient(CodexConfig(), api_mode="invalid-private-token"),
        ):
            with self.subTest(build=build), self.assertRaises(ConfigurationError) as raised:
                build()
            self.assertNotIn("private-token", str(raised.exception))
            self.assertNotIn("secret-token", str(raised.exception))

    def test_chat_reasoning_verification_flows_through_mapping_copy_and_explicit_arguments(self):
        marker = project_llm_settings.chat_reasoning_signature(
            "https://provider.test/v1", "chat-model", "high", chat_stream=True)
        config = CodexConfig(model="chat-model", reasoning_effort="high", base_url="https://provider.test/v1",
                             api_key="private-token", api_mode=API_MODE_CHAT_COMPLETIONS,
                             chat_reasoning_verified_for=marker, chat_stream=True)
        self.assertEqual(len(config), len(dict(config)))
        self.assertEqual(len(config), 13)
        self.assertEqual(config.effective_reasoning_effort, "high")
        self.assertEqual(dict(config)["chat_reasoning_verified_for"], marker)
        self.assertIs(dict(config)["chat_stream"], True)
        self.assertEqual(config.redacted()["chat_reasoning_verified_for"], marker)
        self.assertIs(config.redacted()["chat_stream"], True)
        self.assertNotIn("private-token", repr(config) + repr(config.redacted()))
        for source in (config, dict(config)):
            with self.subTest(source=type(source).__name__):
                inherited = CodexResponsesClient(source).config
                self.assertEqual(inherited.chat_reasoning_verified_for, marker)
                self.assertIs(inherited.chat_stream, True)
                self.assertEqual(inherited.effective_reasoning_effort, "high")
                for override in ({"model": "other-model"}, {"reasoning_effort": "max"},
                                 {"base_url": "https://other.test/v1"}, {"chat_stream": False},
                                 {"chat_reasoning_verified_for": ""}):
                    with self.subTest(override=override):
                        self.assertIsNone(CodexResponsesClient(source, **override).config.effective_reasoning_effort)
                explicit = CodexResponsesClient(source, chat_reasoning_verified_for=marker, chat_stream=True)
                self.assertEqual(explicit.config.effective_reasoning_effort, "high")
        for invalid in (None, "", "not-a-marker", "a" * 63, "A" * 64, [], 12):
            with self.subTest(invalid=invalid):
                invalid_config = replace(config, chat_reasoning_verified_for=invalid)
                self.assertIsNone(invalid_config.effective_reasoning_effort)
        responses = replace(config, api_mode=API_MODE_RESPONSES, chat_reasoning_verified_for="invalid")
        self.assertEqual(responses.effective_reasoning_effort, "high")
        self.assertEqual(replace(responses, reasoning_effort="").effective_reasoning_effort, "")
        for stream in (None, "false", 0):
            with self.subTest(stream=stream), self.assertRaises(ConfigurationError):
                CodexConfig(chat_stream=stream)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"
            path.write_text('model = "chat-model"\nmodel_reasoning_effort = "high"\n', encoding="utf-8")
            loaded = load_codex_config(
                path, root / "auth.json", base_url=config.base_url, api_mode=API_MODE_CHAT_COMPLETIONS,
                api_key="private-token", chat_stream=True, chat_reasoning_verified_for=marker,
            )
            self.assertEqual(loaded.effective_reasoning_effort, "high")
            direct = CodexResponsesClient(
                config_path=path, auth_path=root / "auth.json", base_url=config.base_url,
                api_mode=API_MODE_CHAT_COMPLETIONS, api_key="private-token", chat_stream=True,
                chat_reasoning_verified_for=marker,
            )
            self.assertEqual(direct.config.effective_reasoning_effort, "high")

    def test_provider_direct_token_precedes_account_auth_without_exposing_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, auth_path = root / "config.toml", root / "auth.json"
            config_path.write_text(
                'model_provider = "custom"\n[model_providers.custom]\n'
                'base_url = "https://provider.test/v1"\nrequires_openai_auth = true\n'
                'experimental_bearer_token = "provider-secret"\n',
                encoding="utf-8",
            )
            auth_path.write_text('{"OPENAI_API_KEY": "unrelated-account-secret"}', encoding="utf-8")
            before = config_path.read_bytes(), auth_path.read_bytes()
            config = load_codex_config(config_path, auth_path, reasoning_effort="high")
            self.assertEqual(config.api_key, "provider-secret")
            self.assertEqual(config.reasoning_effort, "high")
            self.assertEqual(config.endpoint, "https://provider.test/v1/responses")
            explicit = load_codex_config(config_path, auth_path, api_key="explicit-secret")
            self.assertEqual(explicit.api_key, "explicit-secret")
            for secret in ("provider-secret", "unrelated-account-secret", "explicit-secret"):
                self.assertNotIn(secret, repr(config) + str(config.redacted()) + repr(explicit))
            self.assertEqual((config_path.read_bytes(), auth_path.read_bytes()), before)

    def test_provider_env_key_is_single_source_and_missing_value_cannot_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, auth_path = root / "config.toml", root / "auth.json"
            config_path.write_text(
                'model_provider = "custom"\n[model_providers.custom]\n'
                'base_url = "https://provider.test/v1"\nrequires_openai_auth = true\n'
                'env_key = "TEST_PROVIDER_KEY"\nexperimental_bearer_token = "direct-secret"\n',
                encoding="utf-8",
            )
            auth_path.write_text('{"OPENAI_API_KEY": "unrelated-account-secret"}', encoding="utf-8")
            with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "provider-env-secret"}, clear=True):
                config = load_codex_config(config_path, auth_path)
            self.assertEqual(config.api_key, "provider-env-secret")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "unrelated-env-secret"}, clear=True):
                with self.assertRaises(ConfigurationError) as raised:
                    load_codex_config(config_path, auth_path)
                explicit = load_codex_config(config_path, auth_path, api_key="explicit-secret")
                self.assertEqual(explicit.api_key, "explicit-secret")
            for secret in ("direct-secret", "unrelated-account-secret", "unrelated-env-secret", "provider-env-secret"):
                self.assertNotIn(secret, str(raised.exception) + repr(config) + str(config.redacted()))

    def test_anonymous_provider_does_not_load_unrelated_account_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, auth_path = root / "config.toml", root / "auth.json"
            config_path.write_text(
                'model_provider = "local"\n[model_providers.local]\n'
                'base_url = "http://provider.test/v1"\nrequires_openai_auth = false\n',
                encoding="utf-8",
            )
            auth_path.write_text('invalid unrelated account JSON', encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "unrelated-secret"}):
                config = load_codex_config(config_path, auth_path)
            self.assertIsNone(config.api_key)
            self.assertFalse(config.requires_auth)

    def test_parse_output_text_and_nested_multi_part_output(self):
        self.assertEqual(parse_json_response({"output_text": "```json\n{\"ok\": true}\n```"}), {"ok": True})
        nested = {"output": [{"content": [{"type": "output_text", "text": "{\"a\": "}, {"text": "1}"}]}]}
        self.assertEqual(parse_json_response(nested), {"a": 1})

    def test_chat_content_accepts_plain_markdown_and_text_parts_and_uses_first_choice(self):
        for content in (
            '{"ok": true}',
            '```json\n{"ok": true}\n```',
            [{"type": "text", "text": '```json\n{"ok": '}, {"type": "text", "text": 'true}\n```'}],
        ):
            response = {"choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": content}},
                {"finish_reason": "stop", "message": {"content": '{"ok": false}'}},
            ]}
            with self.subTest(content=content):
                self.assertEqual(parse_json_response(response), {"ok": True})

    def test_chat_review_sends_conservative_messages_envelope_and_validates_schema(self):
        requests = []
        config = CodexConfig(model="chat-model", base_url="https://provider.test/v1/responses",
                             api_key="secret-token", reasoning_effort="max",
                             api_mode=API_MODE_CHAT_COMPLETIONS, max_retries=0)

        def transport(request, timeout):
            requests.append(request)
            return FakeResponse({"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": '```json\n{"ok": true}\n```',
            }}]})

        client = CodexResponsesClient(config, reasoning_effort="high", transport=transport,
                                      response_schema={"type": "object", "required": ["ok"]})
        job = {"job_id": "chat-job", "job_description": "负责方案设计与系统集成"}
        self.assertEqual(client.review_job(job, prompt="Review the complete JD."), {"ok": True})
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.full_url, "https://provider.test/v1/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(request.get_header("User-agent"), APPLICATION_USER_AGENT)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Accept"), "application/json")
        body = json.loads(request.data)
        self.assertEqual(set(body), {"model", "messages", "stream"})
        self.assertEqual(body["model"], "chat-model")
        self.assertIs(body["stream"], False)
        self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
        self.assertIn("Review the complete JD.", body["messages"][0]["content"])
        self.assertEqual(parse_json_response(body["messages"][1]["content"]), job)
        self.assertNotIn("secret-token", request.data.decode("utf-8"))
        with self.assertRaises(JsonSchemaValidationError):
            client.review_job(job, prompt="Review", response_schema={"type": "object", "required": ["missing"]})

    def test_chat_review_rejects_empty_malformed_error_truncated_and_tool_responses(self):
        complete = {"finish_reason": "stop", "message": {"content": '{"ok": true}'}}
        malformed = [
            {}, {"choices": []}, {"choices": {}}, {"choices": "private-token"},
            {"choices": [None]}, {"choices": [{}]},
            {"choices": [{"finish_reason": "stop", "delta": {"content": '{"ok": true}'}}]},
            {"choices": [complete], "error": {"message": "private-token"}},
        ]
        for content in (None, "", " ", [], {}, "private-token", '{"broken": } private-token',
                        [{"type": "image_url", "image_url": {"url": "private-token"}}]):
            malformed.append({"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
        for reason in ("length", "content_filter", "tool_calls", "function_call"):
            malformed.append({"choices": [{**complete, "finish_reason": reason}]})
        for extra in ({"refusal": "private-token"}, {"tool_calls": [{"id": "private-token"}]}):
            malformed.append({"choices": [{**complete, "message": {**complete["message"], **extra}}]})
        malformed.extend((b"", b"private-token", b"[]", b"null"))
        for response in malformed:
            with self.subTest(response=response):
                client = CodexResponsesClient(
                    {"base_url": "https://provider.test/v1", "api_key": "private-token",
                     "api_mode": API_MODE_CHAT_COMPLETIONS, "max_retries": 0},
                    transport=lambda request, timeout, payload=response: FakeResponse(payload),
                    response_schema=None,
                )
                with self.assertRaises((InvalidJsonResponseError, LLMRequestError)) as raised:
                    client.review_job("JD", prompt="Return JSON")
                self.assertNotIn("private-token", "".join(traceback.format_exception(raised.exception)))

    def test_verified_chat_review_and_connection_probe_use_only_the_matching_effort(self):
        marker = project_llm_settings.chat_reasoning_signature("https://provider.test/v1", "chat-model", "max")
        config = CodexConfig(model="chat-model", reasoning_effort="max", base_url="https://provider.test/v1/responses/",
                             api_key="private-token", api_mode=API_MODE_CHAT_COMPLETIONS,
                             chat_reasoning_verified_for=marker, max_retries=5)
        requests = []
        client = CodexResponsesClient(
            config, response_schema=None,
            transport=lambda request, timeout: (requests.append(request) or FakeResponse({"choices": [{
                "finish_reason": "stop", "message": {"role": "assistant", "content": '{"ok":true}'},
            }]})),
        )
        self.assertEqual(client.review_job("JD", prompt="Return JSON"), {"ok": True})
        review_body = json.loads(requests[-1].data)
        self.assertEqual(set(review_body), {"model", "messages", "reasoning_effort", "stream"})
        self.assertEqual(review_body["reasoning_effort"], "max")
        self.assertIs(review_body["stream"], False)
        for override, expected in (({}, "max"), ({"reasoning_effort": "max"}, "max"),
                                   ({"model": "chat-model"}, "max"),
                                   ({"model": "other-model"}, None), ({"reasoning_effort": "high"}, None)):
            with self.subTest(override=override):
                before = len(requests)
                self.assertTrue(client.test_connection(**override))
                self.assertEqual(len(requests), before + 1)
                body = json.loads(requests[-1].data)
                self.assertEqual(body.get("reasoning_effort"), expected)
                self.assertEqual(body["max_tokens"], 1024 if expected else 16)
                self.assertNotIn("reasoning", body)
                self.assertNotIn("store", body)
        unverified = CodexResponsesClient(config, reasoning_effort="high", response_schema=None,
                                         transport=client._transport)
        self.assertEqual(unverified.review_job("JD", prompt="Return JSON"), {"ok": True})
        self.assertNotIn("reasoning_effort", json.loads(requests[-1].data))
        self.assertEqual(client.config.chat_reasoning_verified_for, marker)

    def test_error_and_unfinished_response_cannot_be_success(self):
        responses = [
            {"status": status, "output_text": '{"ok": true}'}
            for status in ("failed", "incomplete", "in_progress", "queued", "cancelled")
        ]
        responses.extend(
            (
                {"status": "completed", "error": {"message": "secret-token"}, "output_text": '{}'},
                {"status": "completed", "incomplete_details": {"reason": "max_output_tokens"}, "output_text": '{}'},
            )
        )
        for response in responses:
            with self.subTest(status=response.get("status")), self.assertRaises(InvalidJsonResponseError) as raised:
                parse_json_response(response)
            self.assertNotIn("secret-token", str(raised.exception))
        self.assertEqual(parse_json_response({"status": "completed", "error": None, "output_text": '{}'}), {})

    def test_real_application_identity_preserves_responses_protocol(self):
        requests = []

        def transport(request, timeout):
            requests.append(request)
            return FakeResponse({"status": "completed", "output_text": '{}'})

        client = CodexResponsesClient(
            {"model": "configured-model", "base_url": "https://provider.test/v1", "api_key": "secret-token"},
            reasoning_effort="high",
            transport=transport,
            response_schema=None,
        )
        self.assertEqual(client.review_job({"job_id": "existing-job"}, prompt="Return JSON"), {})
        request = requests[0]
        self.assertEqual(request.get_header("User-agent"), APPLICATION_USER_AGENT)
        self.assertEqual(APPLICATION_USER_AGENT, "boss-architecture-collector")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.full_url, "https://provider.test/v1/responses")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        body = json.loads(request.data)
        self.assertEqual(set(body), {"model", "input", "store", "reasoning"})
        self.assertEqual(body["model"], "configured-model")
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertFalse(body["store"])
        self.assertEqual([item["role"] for item in body["input"]], ["system", "user"])
        self.assertNotIn("messages", body)
        self.assertNotIn("secret-token", request.data.decode("utf-8"))

    def test_list_models_uses_authenticated_models_endpoint_and_deduplicates_ids(self):
        requests = []

        def transport(request, timeout):
            requests.append(request)
            return FakeResponse({"object": "list", "data": [
                {"id": "gpt-6-luna"}, {"id": "gpt-6-astra"}, {"id": "gpt-6-luna"},
                {"object": "model"}, None,
            ]})

        for base, mode in product(
            ("https://provider.test/v1/", "https://provider.test/v1/responses/",
             "https://provider.test/v1/chat/completions/"),
            (API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS),
        ):
            with self.subTest(base=base, mode=mode):
                requests.clear()
                client = CodexResponsesClient(
                    {"base_url": base, "api_key": "private-token", "max_retries": 0, "api_mode": mode},
                    transport=transport,
                    response_schema=None,
                )
                self.assertEqual(client.list_models(), ["gpt-6-luna", "gpt-6-astra"])
                self.assertEqual(len(requests), 1)
                request = requests[0]
                self.assertEqual(request.full_url, "https://provider.test/v1/models")
                self.assertEqual(request.get_method(), "GET")
                self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
                self.assertIsNone(request.data)

    def test_list_models_handles_responses_base_and_redacts_endpoint_errors(self):
        client = CodexResponsesClient(
            {"base_url": "https://provider.test/v1/responses", "api_key": "private-token", "max_retries": 0},
            transport=lambda request, timeout: FakeResponse({"message": "private-token"}, status=404),
            response_schema=None,
        )
        with self.assertRaises(LLMHTTPError) as raised:
            client.list_models()
        self.assertIn("Models API HTTP 404", str(raised.exception))
        self.assertNotIn("private-token", str(raised.exception))

    def test_connection_test_sends_minimal_nonstored_request_and_does_not_retry(self):
        requests = []

        def transport(request, timeout):
            requests.append(request)
            return FakeResponse({"status": "completed", "output_text": "OK"})

        client = CodexResponsesClient(
            {"base_url": "https://provider.test/v1", "api_key": "private-token", "max_retries": 5},
            transport=transport,
            response_schema=None,
        )
        self.assertTrue(client.test_connection(model="custom-model", reasoning_effort="high"))
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.full_url, "https://provider.test/v1/responses")
        self.assertEqual(request.get_method(), "POST")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "custom-model")
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertFalse(body["store"])
        self.assertEqual(body["max_output_tokens"], 16)
        self.assertEqual(body["input"], "Reply with OK only.")
        self.assertNotIn("private-token", request.data.decode("utf-8"))

        failed_attempts = []
        failing = CodexResponsesClient(
            {"base_url": "https://provider.test/v1", "api_key": "private-token", "max_retries": 5},
            transport=lambda request, timeout: (failed_attempts.append(request)
                                                or FakeResponse({}, status=503)),
            response_schema=None,
        )
        with self.assertRaises(LLMServerError):
            failing.test_connection(model="custom-model", reasoning_effort="max")
        self.assertEqual(len(failed_attempts), 1, "connection checks must not retry a potentially billable request")

    def test_connection_test_rejects_http_success_without_completed_model_response(self):
        for response in (
            {"status": "in_progress", "id": "response-id"},
            {"status": "incomplete", "output_text": ""},
            {"error": {"message": "private-token"}},
            {"object": "unexpected"},
            {"status": "completed"},
            {"status": "completed", "output": []},
            {"status": "completed", "output_text": "  "},
            {"status": "completed", "output_text": "OK",
             "incomplete_details": {"reason": "max_output_tokens"}},
        ):
            client = CodexResponsesClient(
                {"base_url": "https://provider.test/v1", "api_key": "private-token", "max_retries": 0},
                transport=lambda request, timeout, payload=response: FakeResponse(payload),
                response_schema=None,
            )
            with self.subTest(response=response), self.assertRaises(LLMRequestError) as raised:
                client.test_connection(model="custom-model", reasoning_effort="max")
            self.assertNotIn("private-token", str(raised.exception))

    def test_chat_connection_probe_sends_max_tokens_and_never_retries(self):
        requests = []
        client = CodexResponsesClient(
            {"base_url": "https://provider.test/v1", "api_key": "private-token",
             "api_mode": API_MODE_CHAT_COMPLETIONS, "max_retries": 5},
            transport=lambda request, timeout: (requests.append(request) or FakeResponse({"choices": [{
                "finish_reason": "stop", "message": {"content": "OK"},
            }]})),
            response_schema=None,
        )
        self.assertTrue(client.test_connection(model="probe-model", reasoning_effort="high"))
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.full_url, "https://provider.test/v1/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "model": "probe-model", "messages": [{"role": "user", "content": "Reply with OK only."}],
            "max_tokens": 16, "stream": False,
        })
        self.assertNotIn("private-token", request.data.decode("utf-8"))
        for status, error_type in ((403, LLMHTTPError), (429, LLMRateLimitError), (503, LLMServerError)):
            with self.subTest(status=status):
                attempts = []
                failed = CodexResponsesClient(
                    replace(client.config, max_retries=5), response_schema=None,
                    transport=lambda request, timeout: (attempts.append(request)
                                                        or FakeResponse({"error": "private-token"}, status=status)),
                )
                with patch("llm_client.time.sleep") as sleep, self.assertRaises(error_type) as raised:
                    failed.test_connection()
                self.assertEqual(len(attempts), 1)
                sleep.assert_not_called()
                self.assertNotIn("private-token", "".join(traceback.format_exception(raised.exception)))

    def test_chat_connection_probe_rejects_empty_and_unfinished_envelopes(self):
        for response in (
            {}, {"status": "completed", "output_text": "OK"}, {"choices": []},
            {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
            {"choices": [{"finish_reason": "length", "message": {"content": "OK"}}]},
            {"choices": [{"finish_reason": "content_filter", "message": {"content": "OK"}}]},
            {"choices": [{"finish_reason": "tool_calls", "message": {"content": "OK"}}]},
            {"error": {"message": "private-token"}},
        ):
            with self.subTest(response=response):
                client = CodexResponsesClient(
                    {"base_url": "https://provider.test/v1", "api_key": "private-token",
                     "api_mode": API_MODE_CHAT_COMPLETIONS, "max_retries": 0},
                    transport=lambda request, timeout, payload=response: FakeResponse(payload),
                    response_schema=None,
                )
                with self.assertRaises(LLMRequestError) as raised:
                    client.test_connection()
                self.assertNotIn("private-token", "".join(traceback.format_exception(raised.exception)))

    def test_chat_reasoning_probe_sends_proposed_parameters_once_without_saving_proof(self):
        for stream, effort in product((False, True), ("none", "high", "max")):
            with self.subTest(stream=stream, effort=effort):
                requests = []
                complete = {"choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant", "content": "OK",
                }}]}
                payload = chat_sse({"choices": [{"index": 0, "delta": {"content": "OK"},
                                                   "finish_reason": "stop"}]}) if stream else complete
                client = CodexResponsesClient(
                    {"model": "configured-model", "reasoning_effort": "low", "api_key": "private-token",
                     "base_url": "https://provider.test/v1/responses", "api_mode": API_MODE_CHAT_COMPLETIONS,
                     "chat_stream": stream, "max_retries": 5},
                    transport=lambda request, timeout: (requests.append(request) or FakeResponse(payload)),
                    response_schema=None,
                )
                with patch("llm_client.time.sleep") as sleep:
                    self.assertTrue(client.test_chat_reasoning_effort(model="proposed-model", reasoning_effort=effort))
                self.assertEqual(len(requests), 1)
                sleep.assert_not_called()
                request = requests[0]
                self.assertEqual(request.full_url, "https://provider.test/v1/chat/completions")
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(request.get_header("User-agent"), APPLICATION_USER_AGENT)
                self.assertEqual(request.get_header("Accept"), "text/event-stream" if stream else "application/json")
                self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
                self.assertEqual(json.loads(request.data), {
                    "model": "proposed-model", "reasoning_effort": effort,
                    "messages": [{"role": "user", "content": "Reply with OK only."}],
                    "max_tokens": 1024, "stream": stream,
                })
                self.assertNotIn("private-token", request.data.decode("utf-8"))
                self.assertEqual(client.config.chat_reasoning_verified_for, "")
                self.assertIsNone(client.config.effective_reasoning_effort)
                self.assertEqual(client.config.model, "configured-model")
                self.assertEqual(client.config.reasoning_effort, "low")

    def test_chat_reasoning_probe_requires_chat_mode_and_valid_proposed_values(self):
        requests = []
        for mode, overrides in ((API_MODE_RESPONSES, {}), (API_MODE_CHAT_COMPLETIONS, {"model": " "}),
                                (API_MODE_CHAT_COMPLETIONS, {"reasoning_effort": ""}),
                                (API_MODE_CHAT_COMPLETIONS, {"reasoning_effort": "invalid-private-token"})):
            with self.subTest(mode=mode, overrides=overrides):
                client = CodexResponsesClient(
                    {"base_url": "https://provider.test/v1", "api_key": "private-token", "api_mode": mode},
                    transport=lambda request, timeout: requests.append(request), response_schema=None,
                )
                with self.assertRaises(ConfigurationError) as raised:
                    client.test_chat_reasoning_effort(**overrides)
                self.assertNotIn("private-token", "".join(traceback.format_exception(raised.exception)))
        self.assertEqual(requests, [])

    def test_chat_reasoning_probe_never_retries_http_transport_or_timeout_failures(self):
        failures = [(status, expected, raised) for status, expected in (
            (400, LLMHTTPError), (403, LLMHTTPError), (429, LLMRateLimitError), (503, LLMServerError),
        ) for raised in (False, True)]
        failures.extend((("timeout", LLMTimeoutError, True), ("url-timeout", LLMTimeoutError, True),
                         ("network", LLMRequestError, True), ("incomplete-read", LLMRequestError, True)))
        for failure, expected, raised_http in failures:
            with self.subTest(failure=failure, raised=raised_http):
                requests = []

                def transport(request, timeout):
                    requests.append(request)
                    if failure == "timeout":
                        raise TimeoutError("timed out private-token")
                    if failure == "url-timeout":
                        raise urllib.error.URLError(TimeoutError("timed out private-token"))
                    if failure == "network":
                        raise urllib.error.URLError("private-token")
                    if failure == "incomplete-read":
                        raise http.client.IncompleteRead(b"private-token", 120)
                    if raised_http:
                        raise urllib.error.HTTPError("https://provider.test/private-token", failure,
                                                     "private-token", {}, io.BytesIO(b"private-token"))
                    return FakeResponse({"error": {"message": "private-token"}}, status=failure)

                client = CodexResponsesClient(
                    {"base_url": "https://provider.test/v1", "api_key": "private-token",
                     "api_mode": API_MODE_CHAT_COMPLETIONS, "chat_stream": True, "max_retries": 5},
                    transport=transport, response_schema=None,
                )
                with patch("llm_client.time.sleep") as sleep, self.assertRaises(expected) as caught:
                    client.test_chat_reasoning_effort()
                self.assertEqual(len(requests), 1)
                sleep.assert_not_called()
                self.assertNotIn("private-token", "".join(traceback.format_exception(caught.exception)))

    def test_chat_reasoning_probe_requires_complete_nonempty_unambiguous_chat_response(self):
        complete = {"finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}
        responses = [
            {}, {"choices": None}, {"choices": []}, {"choices": {}}, {"choices": [None]},
            {"choices": [{"message": complete["message"]}]},
            {"status": "completed", "output_text": "OK"},
            {"choices": [complete], "error": {"message": "private-token"}},
            {"choices": [complete], "incomplete_details": {"reason": "private-token"}},
            {"choices": [complete], "status": "incomplete"},
            {"choices": [{**complete, "error": {"message": "private-token"}}]},
        ]
        for reason in (None, "length", "content_filter", "tool_calls", "function_call", "private-token"):
            responses.append({"choices": [{**complete, "finish_reason": reason}]})
        for content in (None, "", " ", [], {}, [{"type": "image_url", "url": "private-token"}]):
            responses.append({"choices": [{**complete, "message": {"content": content}}]})
        for extra in ({"error": "private-token"}, {"role": "user"}, {"refusal": "private-token"},
                      {"tool_calls": [{"id": "private-token"}]}, {"function_call": {"name": "private-token"}}):
            responses.append({"choices": [{**complete, "message": {**complete["message"], **extra}}]})
        responses.extend((b"private-token", b"[]", b"null", b""))
        for response in responses:
            with self.subTest(response=response):
                requests = []
                client = CodexResponsesClient(
                    {"base_url": "https://provider.test/v1", "api_key": "private-token",
                     "api_mode": API_MODE_CHAT_COMPLETIONS, "max_retries": 5},
                    transport=lambda request, timeout: (requests.append(request) or FakeResponse(response)),
                    response_schema=None,
                )
                with patch("llm_client.time.sleep") as sleep, self.assertRaises(LLMRequestError) as caught:
                    client.test_chat_reasoning_effort()
                self.assertEqual(len(requests), 1)
                sleep.assert_not_called()
                self.assertNotIn("private-token", "".join(traceback.format_exception(caught.exception)))

    def test_chat_sse_collects_text_and_reasoning_and_validates_review_and_probe(self):
        chunks = (
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"reasoning_content": "private-reasoning-"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"reasoning_content": "text"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": '{"ok":'}, "finish_reason": None},
                         {"index": 1, "delta": {"content": "ignore another choice"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": ' true}'}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"completion_tokens": 10}},
        )
        payload = chat_sse(*chunks)
        decoded = CodexResponsesClient._decode_chat_event_stream(payload)
        self.assertEqual(decoded["choices"][0]["message"]["reasoning_content"], "private-reasoning-text")
        self.assertEqual(parse_json_response(decoded), {"ok": True})
        for headers, prefix in (({}, b""), ({"content-type": "text/event-stream; charset=utf-8"},
                                           b"\xef\xbb\xbf: heartbeat\r\n\r\n")):
            with self.subTest(headers=headers):
                requests = []
                marker = project_llm_settings.chat_reasoning_signature(
                    "https://provider.test/v1", "chat-model", "high", chat_stream=True)
                client = CodexResponsesClient(
                    {"base_url": "https://provider.test/v1", "api_key": "private-token", "model": "chat-model",
                     "reasoning_effort": "high", "api_mode": API_MODE_CHAT_COMPLETIONS, "chat_stream": True,
                     "chat_reasoning_verified_for": marker},
                    transport=lambda request, timeout: (requests.append(request)
                                                        or FakeResponse(prefix + payload, headers=headers)),
                    response_schema=None,
                )
                self.assertEqual(client.review_job("JD", prompt="Return JSON"), {"ok": True})
                self.assertTrue(client.test_connection())
                self.assertTrue(client.test_chat_reasoning_effort())
                self.assertEqual(len(requests), 3)
                for request in requests:
                    body = json.loads(request.data)
                    self.assertIs(body["stream"], True)
                    self.assertEqual(body["reasoning_effort"], "high")
                    self.assertNotIn("reasoning", body)
                    self.assertNotIn("private-reasoning", request.data.decode("utf-8"))
        multiline = (b'event: message\ndata: {"choices":\ndata: [{"delta":{"content":"OK"},'
                     b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        self.assertEqual(CodexResponsesClient._decode_chat_event_stream(multiline)["choices"][0]["message"]["content"],
                         "OK")

    def test_chat_sse_incomplete_corrupt_truncated_empty_and_error_streams_cannot_verify(self):
        content = {"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": None}]}
        finish = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        invalid_streams = [
            chat_sse(content, finish, done=False), chat_sse(content), chat_sse(),
            chat_sse(finish), chat_sse({"choices": []}),
            chat_sse(content, {"choices": [{"delta": {}, "finish_reason": "length"}]}),
            chat_sse(content, finish, {"choices": [{"delta": {"content": "private-token"}}]}),
            chat_sse(content, finish, finish),
            chat_sse({"choices": [{"delta": {"content": []}, "finish_reason": "stop"}]}),
            chat_sse({"choices": [{"message": {"content": "private-token"}, "finish_reason": "stop"}]}),
            chat_sse(content, {"error": {"message": "private-token"}}),
            b"data: {private-token}\n\ndata: [DONE]\n\n",
            b"data: \xff\n\ndata: [DONE]\n\n",
            b"data: []\n\ndata: [DONE]\n\n",
            b"data: [DONE]\n\ndata: private-token\n\n",
            b"event: error\ndata: {\"message\":\"private-token\"}\n\n",
            b"private-token\n\n",
        ]
        for delta in ({"refusal": "private-token"}, {"tool_calls": [{"id": "private-token"}]},
                      {"function_call": {"name": "private-token"}}):
            invalid_streams.append(chat_sse(content, {"choices": [{"delta": delta, "finish_reason": "stop"}]}))
        for payload in invalid_streams:
            with self.subTest(payload=payload):
                requests = []
                client = CodexResponsesClient(
                    {"base_url": "https://provider.test/v1", "api_key": "private-token",
                     "api_mode": API_MODE_CHAT_COMPLETIONS, "chat_stream": True, "max_retries": 5},
                    transport=lambda request, timeout: (requests.append(request)
                                                        or FakeResponse(payload, headers={"Content-Type": "text/event-stream"})),
                    response_schema=None,
                )
                with patch("llm_client.time.sleep") as sleep, self.assertRaises(LLMRequestError) as caught:
                    client.test_chat_reasoning_effort()
                self.assertEqual(len(requests), 1)
                sleep.assert_not_called()
                self.assertNotIn("private-token", "".join(traceback.format_exception(caught.exception)))

    def test_chat_sse_complete_json_lines_allow_missing_event_separators(self):
        chunks = (
            {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"reasoning_content": "private-reasoning"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": '{"ok": true}'}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        )
        standard = chat_sse(*chunks)
        # Reproduce the observed first two chunks sharing one SSE event, and
        # also exercise a fully compact stream including the final marker.
        for payload in (standard.replace(b"\n\n", b"\n", 1), standard.replace(b"\n\n", b"\n"),
                        standard.replace(b"\n\ndata: [DONE]", b"\ndata: [DONE]")):
            with self.subTest(payload=payload):
                requests = []
                client = CodexResponsesClient(
                    {"base_url": "https://provider.test/v1", "api_key": "private-token",
                     "api_mode": API_MODE_CHAT_COMPLETIONS, "chat_stream": True, "max_retries": 5},
                    transport=lambda request, timeout: (requests.append(request) or FakeResponse(payload)),
                    response_schema=None,
                )
                self.assertEqual(client.review_job("JD", prompt="Return JSON"), {"ok": True})
                self.assertTrue(client.test_connection())
                self.assertTrue(client.test_chat_reasoning_effort())
                self.assertEqual(len(requests), 3)
                decoded = client._decode_chat_event_stream(payload)
                self.assertEqual(decoded["choices"][0]["message"]["reasoning_content"], "private-reasoning")
                self.assertEqual(decoded["choices"][0]["finish_reason"], "stop")
                for request in requests:
                    self.assertEqual(request.get_header("Accept"), "text/event-stream")
                    self.assertEqual(request.get_header("User-agent"), APPLICATION_USER_AGENT)

    def test_chat_sse_standard_multiline_json_still_takes_precedence(self):
        payload = (b'event: message\ndata: {"choices": [\ndata: {"index":0,"delta":\n'
                   b'data: {"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        client = CodexResponsesClient(
            {"base_url": "https://provider.test/v1", "api_key": "private-token",
             "api_mode": API_MODE_CHAT_COMPLETIONS, "chat_stream": True},
            transport=lambda request, timeout: FakeResponse(payload, headers={"Content-Type": "text/event-stream"}),
            response_schema=None,
        )
        self.assertTrue(client.test_chat_reasoning_effort())

    def test_chat_empty_legacy_function_call_placeholders_allow_completed_text(self):
        placeholders = [{}, {"name": None}, {"arguments": ""}]
        placeholders.extend({"name": name, "arguments": arguments}
                            for name, arguments in product((None, ""), (None, "")))
        for stream, placeholder in product((False, True), placeholders):
            with self.subTest(stream=stream, placeholder=placeholder):
                requests = []
                complete = {"choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant", "content": '{"ok": true}', "function_call": placeholder,
                }}]}
                payload = chat_sse(
                    {"choices": [{"delta": {"role": "assistant", "function_call": placeholder}}]},
                    {"choices": [{"delta": {"content": '{"ok": true}', "function_call": placeholder}}]},
                    {"choices": [{"delta": {"function_call": placeholder}, "finish_reason": "stop"}]},
                ) if stream else complete
                client = CodexResponsesClient(
                    {"base_url": "https://provider.test/v1", "api_key": "private-token",
                     "api_mode": API_MODE_CHAT_COMPLETIONS, "chat_stream": stream, "max_retries": 0},
                    transport=lambda request, timeout: (requests.append(request) or FakeResponse(payload)),
                    response_schema=None,
                )
                self.assertEqual(client.review_job("JD", prompt="Return JSON"), {"ok": True})
                self.assertTrue(client.test_connection())
                self.assertTrue(client.test_chat_reasoning_effort())
                self.assertEqual(len(requests), 3)
                if stream:
                    self.assertNotIn("function_call", client._decode_chat_event_stream(payload)["choices"][0]["message"])

    def test_chat_legacy_function_call_compatibility_rejects_calls_unknown_fields_and_nonobjects(self):
        invalid_calls = [
            {"name": "private-token", "arguments": ""}, {"name": "", "arguments": "{}"},
            {"name": " ", "arguments": None}, {"name": None, "arguments": " "},
            {"name": "", "arguments": "", "unexpected": None}, {"unexpected": ""},
            {"name": {}, "arguments": ""}, {"name": "", "arguments": []},
            [], "", 0, False,
        ]
        for stream, function_call in product((False, True), invalid_calls):
            with self.subTest(stream=stream, function_call=function_call):
                requests = []
                complete = {"choices": [{"finish_reason": "stop", "message": {
                    "content": "OK", "function_call": function_call,
                }}]}
                payload = chat_sse(
                    {"choices": [{"delta": {"content": "OK", "function_call": function_call}}]},
                    # A subsequent empty gateway object cannot hide a real call.
                    {"choices": [{"delta": {"function_call": {"name": "", "arguments": ""}},
                                   "finish_reason": "stop"}]},
                ) if stream else complete
                client = CodexResponsesClient(
                    {"base_url": "https://provider.test/v1", "api_key": "private-token",
                     "api_mode": API_MODE_CHAT_COMPLETIONS, "chat_stream": stream, "max_retries": 5},
                    transport=lambda request, timeout: (requests.append(request) or FakeResponse(payload)),
                    response_schema=None,
                )
                with patch("llm_client.time.sleep") as sleep, self.assertRaises(LLMRequestError) as caught:
                    client.test_chat_reasoning_effort()
                self.assertEqual(len(requests), 1)
                sleep.assert_not_called()
                self.assertNotIn("private-token", "".join(traceback.format_exception(caught.exception)))

    def test_chat_empty_function_call_placeholder_cannot_override_tool_finish_reason(self):
        placeholder = {"name": "", "arguments": ""}
        for stream, finish_reason in product((False, True), ("function_call", "tool_calls")):
            with self.subTest(stream=stream, finish_reason=finish_reason):
                complete = {"choices": [{"finish_reason": finish_reason, "message": {
                    "content": "OK", "function_call": placeholder,
                }}]}
                payload = chat_sse({"choices": [{"delta": {"content": "OK", "function_call": placeholder},
                                                  "finish_reason": finish_reason}]}) if stream else complete
                client = CodexResponsesClient(
                    {"base_url": "https://provider.test/v1", "api_key": "private-token",
                     "api_mode": API_MODE_CHAT_COMPLETIONS, "chat_stream": stream},
                    transport=lambda request, timeout: FakeResponse(payload), response_schema=None,
                )
                with self.assertRaises(LLMRequestError):
                    client.test_chat_reasoning_effort()

    def test_chat_sse_missing_separators_cannot_hide_bad_lines_errors_or_truncation(self):
        content = {"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": None}]}
        finish = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        prefix = chat_sse(content, finish, done=False).replace(b"\n\n", b"\n")
        bad_streams = [prefix, prefix + b"data: {\"private-token\":\n", prefix + b"data: [DO\n"]
        for bad_line in (b"{private-token}", b"[]", b"null", b"123", b'"private-token"', b"[DONE]\ndata: {}"):
            bad_streams.append(prefix + b"data: " + bad_line + b"\ndata: [DONE]\n\n")
        bad_streams.extend((
            chat_sse(content, {"error": {"message": "private-token"}}, finish).replace(b"\n\n", b"\n"),
            chat_sse(content, {"choices": [{"delta": {}, "finish_reason": "length"}]}).replace(b"\n\n", b"\n"),
            chat_sse(content).replace(b"\n\n", b"\n"),
        ))
        for payload in bad_streams:
            with self.subTest(payload=payload):
                requests = []
                client = CodexResponsesClient(
                    {"base_url": "https://provider.test/v1", "api_key": "private-token",
                     "api_mode": API_MODE_CHAT_COMPLETIONS, "chat_stream": True, "max_retries": 5},
                    transport=lambda request, timeout: (requests.append(request) or FakeResponse(payload)),
                    response_schema=None,
                )
                with patch("llm_client.time.sleep") as sleep, self.assertRaises(LLMRequestError) as caught:
                    client.test_chat_reasoning_effort()
                self.assertEqual(len(requests), 1)
                sleep.assert_not_called()
                self.assertNotIn("private-token", "".join(traceback.format_exception(caught.exception)))

    def test_responses_mode_does_not_accept_chat_sse_as_its_protocol(self):
        payload = chat_sse({"choices": [{"delta": {"content": '{}'}, "finish_reason": "stop"}]})
        client = CodexResponsesClient(
            {"base_url": "https://provider.test/v1", "api_key": "private-token", "max_retries": 0},
            transport=lambda request, timeout: FakeResponse(payload, headers={"Content-Type": "text/event-stream"}),
            response_schema=None,
        )
        with self.assertRaises(LLMRequestError):
            client.review_job("JD", prompt="Return JSON")

    def test_high_override_never_writes_codex_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, auth_path = root / "config.toml", root / "auth.json"
            config_path.write_text(
                'model_provider = "custom"\nmodel = "configured-model"\nmodel_reasoning_effort = "max"\n'
                '[model_providers.custom]\nbase_url = "https://provider.test/v1"\n',
                encoding="utf-8",
            )
            auth_path.write_text('{"OPENAI_API_KEY": "secret-token"}', encoding="utf-8")
            before = (config_path.read_bytes(), auth_path.read_bytes())
            original = load_codex_config(config_path, auth_path)
            config = load_codex_config(config_path, auth_path, reasoning_effort="high", timeout=120, max_retries=0)
            client = CodexResponsesClient(config, response_schema=None, transport=lambda request, timeout: FakeResponse({"output_text": '{}'}))
            self.assertEqual(client.review_job("existing JD", prompt="Return JSON"), {})
            self.assertEqual(config.reasoning_effort, "high")
            self.assertEqual(original.reasoning_effort, "max")
            self.assertEqual(config.model, original.model)
            self.assertEqual(config.endpoint, original.endpoint)
            self.assertEqual(config.api_key, original.api_key)
            self.assertEqual((config_path.read_bytes(), auth_path.read_bytes()), before)

    def test_auth_denials_are_redacted_and_never_retried(self):
        for mode, (status, raised_http_error) in product(
            (API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS),
            ((401, False), (401, True), (403, False), (403, True)),
        ):
            with self.subTest(mode=mode, status=status, raised_http_error=raised_http_error):
                attempts = []

                def transport(request, timeout):
                    attempts.append(request)
                    if raised_http_error:
                        raise urllib.error.HTTPError(
                            "https://provider.test/secret-token", status, "secret-token", {}, io.BytesIO(b"secret-token")
                        )
                    return FakeResponse({"error": "secret-token"}, status=status)

                client = CodexResponsesClient(
                    {"base_url": "https://provider.test/v1", "api_key": "secret-token", "max_retries": 3,
                     "api_mode": mode},
                    transport=transport,
                    response_schema=None,
                )
                with patch("llm_client.time.sleep") as sleep, self.assertRaises(LLMHTTPError) as raised:
                    client.review_job("job", prompt="Return JSON")
                self.assertEqual(raised.exception.status_code, status)
                self.assertIn(f"HTTP {status}", str(raised.exception))
                self.assertNotIn("secret-token", "".join(traceback.format_exception(raised.exception)))
                self.assertEqual(len(attempts), 1)
                sleep.assert_not_called()

    def test_invalid_json_is_typed_and_does_not_echo_response(self):
        with self.assertRaises(InvalidJsonResponseError) as raised:
            parse_json_response('{"broken": } secret-token')
        self.assertNotIn("secret-token", str(raised.exception))

    def test_basic_json_schema_validation(self):
        validate_json_schema({"confidence": 0.5}, {"type": "object", "required": ("confidence",)})
        with self.assertRaises(JsonSchemaValidationError):
            validate_json_schema({"confidence": 1.5}, {"type": "object", "properties": {"confidence": {"type": "number", "maximum": 1}}})

    def test_retry_timeout_and_429_then_success(self):
        attempts = []

        def transport(request, timeout):
            attempts.append((request, timeout))
            if len(attempts) == 1:
                raise TimeoutError("timed out")
            if len(attempts) == 2:
                return FakeResponse({"output_text": "{}"}, status=429)
            return FakeResponse({"output_text": "{}"})

        client = CodexResponsesClient(
            {
                "model": "test",
                "reasoning_effort": "low",
                "base_url": "http://provider.test/v1",
                "api_key": "secret-token",
                "max_retries": 2,
                "backoff_factor": 0,
            },
            transport=transport,
            response_schema=None,
        )
        with patch("llm_client.time.sleep") as sleep:
            self.assertEqual(client.review_job("job", prompt="return JSON object"), {})
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.call_count, 2)
        sent = json.loads(attempts[0][0].data.decode("utf-8"))
        self.assertEqual(sent["model"], "test")
        self.assertEqual([item["role"] for item in sent["input"]], ["system", "user"])
        self.assertIn("return JSON object", sent["input"][0]["content"])
        self.assertNotIn("secret-token", json.dumps(sent["input"]))
        self.assertEqual(attempts[0][0].get_header("Authorization"), "Bearer secret-token")

    def test_retry_error_classes_are_distinct(self):
        for status, expected in ((429, LLMRateLimitError), (503, LLMServerError)):
            client = CodexResponsesClient(
                {"base_url": "http://provider.test/v1", "api_key": "x", "max_retries": 0},
                transport=lambda request, timeout, status=status: FakeResponse({}, status=status),
                response_schema=None,
            )
            with self.assertRaises(expected):
                client._request({"model": "x", "input": "x"})

        client = CodexResponsesClient(
            {"base_url": "http://provider.test/v1", "api_key": "x", "max_retries": 0},
            transport=lambda request, timeout: (_ for _ in ()).throw(TimeoutError("timed out")),
            response_schema=None,
        )
        with self.assertRaises(LLMTimeoutError):
            client._request({"model": "x", "input": "x"})

    def test_missing_required_auth_is_reported_without_network(self):
        client = CodexResponsesClient(
            {"base_url": "http://provider.test/v1", "requires_auth": True},
            transport=lambda request, timeout: self.fail("network must not be called"),
            response_schema=None,
        )
        with self.assertRaises(ConfigurationError):
            client._request({"model": "x", "input": "x"})


class ResponsesStreamingTests(unittest.TestCase):
    result = {"判断": "符合", "理由": "负责AI应用方案与交付。"}
    private = "fixture-only-private-provider-data"

    @classmethod
    def envelope(cls, text=None):
        return {
            "id": "fixture-response", "status": "completed", "error": None,
            "incomplete_details": None,
            "output": [{"id": "fixture-message", "type": "message", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text", "text": (
                            json.dumps(cls.result, ensure_ascii=False) if text is None else text),
                            "annotations": []}]}],
        }

    @classmethod
    def complete(cls, response=None, *, done=False):
        return responses_sse({"type": "response.completed",
                              "response": cls.envelope() if response is None else response}, done=done)

    def client(self, transport, **kwargs):
        return CodexResponsesClient(
            {"model": "fixture-model", "reasoning_effort": "high", "api_mode": API_MODE_RESPONSES,
             "base_url": "https://provider.invalid/v1", "api_key": "fixture-only-key",
             "chat_stream": True, "max_retries": 3, "backoff_factor": 0},
            transport=transport, **kwargs,
        )

    def test_stream_flag_reaches_review_and_probe_preserving_the_legacy_payload(self):
        for operation in ("review_job", "test_connection"):
            bodies = []
            for stream in (False, True):
                requests = []
                response = self.envelope() if operation == "review_job" else self.envelope("OK")
                client = self.client(lambda request, timeout: (
                    requests.append(request) or FakeResponse(self.complete(response) if stream else response)),
                    chat_stream=stream)
                value = (client.review_job("JD", prompt="Return JSON") if operation == "review_job"
                         else client.test_connection())
                self.assertEqual(value, self.result if operation == "review_job" else True)
                self.assertEqual(len(requests), 1)
                request = requests[0]
                self.assertEqual(request.full_url, "https://provider.invalid/v1/responses")
                self.assertEqual(request.get_header("Accept"), "text/event-stream" if stream else "application/json")
                body = json.loads(request.data)
                if stream:
                    self.assertIs(body.pop("stream"), True)
                else:
                    self.assertNotIn("stream", body)
                self.assertEqual(body["reasoning"], {"effort": "high"})
                self.assertIs(body["store"], False)
                self.assertNotIn("messages", body)
                self.assertNotIn("reasoning_effort", body)
                bodies.append(body)
            self.assertEqual(bodies[0], bodies[1])

    def test_public_request_entry_point_does_not_mutate_its_caller_or_models_request(self):
        requests = []
        client = self.client(lambda request, timeout: requests.append(request) or FakeResponse(
            {"data": [{"id": "fixture-model"}]} if request.get_method() == "GET" else self.complete()))
        body = {"model": "fixture-model", "input": "JD", "store": False}
        before = dict(body)
        self.assertEqual(client._request(body)["id"], "fixture-response")
        self.assertEqual(body, before)
        self.assertTrue(json.loads(requests[0].data)["stream"])
        self.assertEqual(client.list_models(), ["fixture-model"])
        self.assertEqual(requests[1].full_url, "https://provider.invalid/v1/models")
        self.assertIsNone(requests[1].data)
        self.assertEqual(requests[1].get_header("Accept"), "application/json")

    def test_sse_final_response_is_authoritative_and_reasoning_never_enters_results_or_history(self):
        final = self.envelope()
        final["output"].insert(0, {"id": "fixture-reasoning", "type": "reasoning", "status": "completed",
                                    "summary": [{"type": "summary_text", "text": self.private}]})
        events = (
            {"type": "response.created", "response": {"id": "fixture-response", "status": "in_progress",
                                                       "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": {
                "id": "fixture-message", "type": "message", "status": "in_progress",
                "role": "assistant", "content": []}},
            {"type": "response.content_part.added", "part": {"type": "output_text", "text": ""}},
            {"type": "response.reasoning_summary_text.delta", "delta": self.private},
            {"type": "response.output_text.delta", "delta": "partial text must not become final JSON"},
            {"type": "response.output_text.done", "text": '{"判断":"不符合","理由":"obsolete partial"}'},
            {"type": "response.completed", "response": final},
        )
        for done, headers in product((False, True), ({}, {"Content-Type": "text/event-stream; charset=utf-8"})):
            with self.subTest(done=done, headers=headers):
                raw = (b"\xef\xbb\xbf: heartbeat\n\nid: fixture\nretry: 1000\n" + responses_sse(*events, done=done))
                raw = raw.replace(b"\n", b"\r\n")
                requests = []
                state = {}
                client = self.client(lambda request, timeout: requests.append(request) or FakeResponse(raw, headers=headers))
                value = client.review_job({"job_id": "fixture-job"}, prompt="Return JSON", conversation=state)
                self.assertEqual(simple_review_payload(value), self.result)
                self.assertEqual(state["previous_response_id"], "fixture-response")
                self.assertNotIn(self.private, json.dumps(value, ensure_ascii=False) + json.dumps(state, ensure_ascii=False))
                self.assertEqual(len(requests), 1)
                self.assertEqual(json.loads(requests[0].data)["store"], True)

    def test_provider_metadata_is_advisory_and_never_a_completion_signal(self):
        metadata = {"type": "response.metadata", "metadata": {"provider_note": self.private}}
        client = self.client(lambda request, timeout: FakeResponse(responses_sse(
            metadata, {"type": "response.completed", "response": self.envelope()})))
        self.assertEqual(client.review_job("JD", prompt="Return JSON"), self.result)
        for payload in (
            responses_sse(metadata),
            responses_sse(metadata, done=True),
            self.complete() + responses_sse(metadata),
            responses_sse({"type": "response.unknown_advisory"}) + self.complete(),
            *[responses_sse({**metadata, **failure}) + self.complete() for failure in (
                {"error": {"message": self.private}}, {"status": "incomplete"},
                {"incomplete_details": {"reason": "max_output_tokens"}},
                {"refusal": self.private}, {"tool_calls": [self.private]},
                {"function_call": {"name": self.private}},
            )],
        ):
            with self.subTest(payload=payload), self.assertRaises(LLMRequestError) as raised:
                self.client(lambda request, timeout: FakeResponse(payload)).test_connection()
            self.assertNotIn(self.private, "".join(traceback.format_exception(raised.exception)))

    def test_explicit_final_answer_excludes_progress_reasoning_and_convenience_text(self):
        for phase in (None, "commentary"):
            final = self.envelope()
            final["output"][0]["phase"] = "final_answer"
            progress = {"id": "fixture-progress", "type": "message", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text", "text": self.private}]}
            if phase is not None:
                progress["phase"] = phase
            final["output"].insert(0, progress)
            final["output"].insert(1, {"type": "reasoning", "summary": [{
                "type": "summary_text", "text": self.private}]})
            final["output_text"] = self.private + json.dumps(self.result)
            requests = []
            state = {}
            client = self.client(lambda request, timeout: requests.append(request) or FakeResponse(self.complete(final)))
            self.assertEqual(simple_review_payload(client.review_job(
                "JD", prompt="Return JSON", conversation=state)), self.result)
            self.assertEqual(len(requests), 1, "progress text must not cause a billable format correction")
            self.assertNotIn(self.private, json.dumps(state))
        final = self.envelope()
        final["output"][0]["phase"] = "commentary"
        final["output_text"] = json.dumps(self.result)
        with self.assertRaises(LLMRequestError):
            self.client(lambda request, timeout: FakeResponse(self.complete(final))).test_connection()

    def test_data_only_and_standard_multiline_events_accept_completed_text(self):
        chunk = {"type": "response.completed", "response": self.envelope()}
        data_only = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")
        multiline = ("event: response.completed\n" + "\n".join(
            "data: " + line for line in json.dumps(chunk, ensure_ascii=False, indent=2).splitlines()) + "\n\n").encode("utf-8")
        for raw in (data_only, multiline):
            with self.subTest(raw=raw):
                self.assertEqual(self.client(lambda request, timeout: FakeResponse(raw)).review_job(
                    "JD", prompt="Return JSON"), self.result)

    def test_unicode_line_separators_inside_json_are_not_sse_frame_boundaries(self):
        result = {"判断": "符合", "理由": "中文前段\u2028中段\u2029后段\u0085末段"}
        payload = self.complete(self.envelope(json.dumps(result, ensure_ascii=False)))
        for delimiter in (b"\n", b"\r\n", b"\r"):
            with self.subTest(delimiter=delimiter):
                raw = payload.replace(b"\n", delimiter)
                value = self.client(lambda request, timeout: FakeResponse(raw)).review_job("JD", prompt="Return JSON")
                self.assertEqual(value, result)

    def test_incomplete_errors_refusals_tools_and_corruption_never_promote_partial_text(self):
        prefix = responses_sse({"type": "response.output_text.delta", "delta": json.dumps(self.result)})
        streams = [
            b"", b"data: [DONE]\n\n", prefix,
            responses_sse({"type": "response.output_text.done", "text": json.dumps(self.result)}),
            responses_sse({"type": "response.created", "response": {"status": "in_progress"}}),
            b"event: response.completed\n\n", b"data: []\n\n", b"data: null\n\n",
            b"data: {private}\n\n", b"data: \xff\n\n", b"private\n\n",
            self.complete() + b"data: {private}\n\n", self.complete() + self.complete(),
            self.complete(done=True) + b"data: [DONE]\n\n",
            self.complete(done=True) + prefix,
            b"event: response.failed\ndata: " + json.dumps({
                "type": "response.completed", "response": self.envelope()}).encode("utf-8") + b"\n\n",
            responses_sse({"type": "response.completed", "response": None}),
            responses_sse({"type": "response.output_text.delta", "delta": []}) + self.complete(),
            responses_sse({"type": "response.output_item.done", "item": []}) + self.complete(),
            responses_sse({"type": "response.content_part.added", "part": {"type": "refusal", "refusal": self.private}})
            + self.complete(),
            responses_sse({"type": "response.output_item.added", "item": {
                "type": "function_call", "arguments": self.private}}) + self.complete(),
            responses_sse({"type": "response.created", "response": {
                "id": "fixture-other-response", "status": "in_progress"}}) + self.complete(),
            chat_sse({"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}]}),
        ]
        for kind in ("error", "response.failed", "response.incomplete", "response.cancelled",
                     "response.refusal.delta", "response.refusal.done", "response.function_call_arguments.delta",
                     "response.web_search_call.completed", "response.content_filter.done"):
            streams.append(prefix + responses_sse({"type": kind, "message": self.private,
                                                  "delta": self.private}) + self.complete())
        for change in (
            {"status": None}, {"status": "in_progress"}, {"status": "failed"}, {"status": "incomplete"},
            {"error": {"message": self.private}}, {"incomplete_details": {"reason": "max_output_tokens"}},
            {"refusal": self.private}, {"tool_calls": [{"id": self.private}]},
            {"choices": []}, {"output": []}, {"output": None}, {"output": self.private},
            {"output": [self.private]}, {"output_text": "", "output": []},
            {"output": [{"type": "reasoning", "text": json.dumps(self.result)}]},
            {"output": [{"type": "reasoning", "content": [{"text": json.dumps(self.result)}]}]},
            {"output": [{"type": "function_call", "arguments": self.private}], "output_text": json.dumps(self.result)},
            {"output": [{"type": "message", "status": "incomplete", "content": []}],
             "output_text": json.dumps(self.result)},
            {"output": [{"type": "message", "role": "user", "content": []}]},
            {"output": [{"type": "message", "content": [{"type": "refusal", "refusal": self.private}]}],
             "output_text": json.dumps(self.result)},
            {"output": [{"type": "message", "content": [{"type": "output_text", "text": 0}]}]},
        ):
            streams.append(self.complete({**self.envelope(), **change}))
        for payload, operation in product(streams, ("review_job", "test_connection")):
            with self.subTest(payload=payload, operation=operation):
                requests = []
                state = {"previous_response_id": "fixture-committed-id", "messages": [{
                    "role": "assistant", "content": "fixture-committed-result"}]}
                before = json.dumps(state, sort_keys=True)
                client = self.client(lambda request, timeout: requests.append(request) or FakeResponse(
                    payload, headers={"content-type": "text/event-stream"}))
                with patch("llm_client.time.sleep") as sleep, self.assertRaises(LLMRequestError) as caught:
                    if operation == "review_job":
                        client.review_job("JD", prompt="Return JSON", conversation=state)
                    else:
                        client.test_connection()
                self.assertEqual(len(requests), 1, "bad streams must not trigger a new billable or format request")
                self.assertEqual(json.dumps(state, sort_keys=True), before)
                self.assertNotIn(self.private, "".join(traceback.format_exception(caught.exception)))
                sleep.assert_not_called()

    def test_format_correction_uses_streaming_and_preserves_semantic_validation(self):
        for first_text in ("not-json-" + self.private, "{}"):
            with self.subTest(first_text=first_text):
                requests = []

                def transport(request, timeout):
                    requests.append(request)
                    return FakeResponse(self.complete(self.envelope(first_text) if len(requests) == 1 else None))

                value = self.client(transport).review_job({"job_id": "fixture-job"}, prompt="Return JSON")
                self.assertEqual(simple_review_payload(value), self.result)
                self.assertEqual(value["_format_repair_attempts"], 1)
                self.assertEqual(len(requests), 2)
                for request in requests:
                    body = json.loads(request.data)
                    self.assertTrue(body["stream"])
                    self.assertFalse(body["store"])
                    self.assertNotIn("previous_response_id", body)
                    self.assertEqual(request.get_header("Accept"), "text/event-stream")
                    self.assertNotIn(self.private, request.data.decode("utf-8"))
        requests = []
        client = self.client(lambda request, timeout: requests.append(request) or FakeResponse(
            self.complete(self.envelope("{}"))))
        with self.assertRaises(JsonSchemaValidationError):
            client.review_job("JD", prompt="Return JSON")
        self.assertEqual(len(requests), 2)

    def test_streaming_http_failures_keep_existing_retry_and_probe_policies(self):
        for operation, failure, expected, count in (
            ("review_job", 403, LLMHTTPError, 1),
            ("test_connection", 429, LLMRateLimitError, 1),
            ("test_connection", 503, LLMServerError, 1),
        ):
            with self.subTest(operation=operation, status=failure):
                requests = []
                client = self.client(lambda request, timeout: requests.append(request) or FakeResponse(
                    {"error": self.private}, status=failure))
                with self.assertRaises(expected) as caught:
                    (client.review_job("JD", prompt="Return JSON") if operation == "review_job"
                     else client.test_connection())
                self.assertEqual(caught.exception.status_code, failure)
                self.assertEqual(len(requests), count)
                self.assertNotIn(self.private, "".join(traceback.format_exception(caught.exception)))
        for status in (429, 503):
            with self.subTest(retry_status=status):
                requests = []

                def transport(request, timeout):
                    requests.append(request)
                    return (FakeResponse({"error": self.private}, status=status) if len(requests) == 1
                            else FakeResponse(self.complete()))

                with patch("llm_client.time.sleep") as sleep:
                    self.assertEqual(self.client(transport).review_job("JD", prompt="Return JSON"), self.result)
                self.assertEqual(len(requests), 2)
                self.assertEqual(sleep.call_count, 1)
                self.assertTrue(all(json.loads(request.data)["stream"] for request in requests))

    def test_interrupted_reads_close_the_response_and_cannot_commit_a_partial_result(self):
        for failure in (http.client.IncompleteRead(b"partial JSON"), ConnectionResetError(self.private)):
            with self.subTest(failure=type(failure).__name__):
                response = FakeResponse({})
                response.read = lambda: (_ for _ in ()).throw(failure)
                response.close = lambda: setattr(response, "closed", True)
                requests = []
                state = {}
                client = self.client(lambda request, timeout: requests.append(request) or response)
                with self.assertRaises(LLMRequestError) as caught:
                    client.review_job("JD", prompt="Return JSON", conversation=state)
                self.assertEqual(state, {})
                self.assertEqual(len(requests), 1)
                self.assertTrue(response.closed)
                self.assertNotIn(self.private, "".join(traceback.format_exception(caught.exception)))


if __name__ == "__main__":
    unittest.main()
