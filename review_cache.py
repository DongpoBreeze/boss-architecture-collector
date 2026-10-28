"""Shared, credential-free identities for semantic review checkpoints.

Only hashes are persisted.  The provider identity deliberately selects request
settings rather than serialising a configuration object (which contains auth).
Each review carries its own signature; a sidecar is never proof of provenance.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import llm_client
import project_llm_settings
from llm_client import (ConfigurationError, JsonSchemaValidationError, REVIEW_RESPONSE_SCHEMA,
                        SIMPLE_REVIEW_FORMAT, simple_review_payload)


REVIEW_CACHE_VERSION = "semantic-review-v1"
CACHE_SIGNATURE_FIELD = "cache_signature"
CACHE_INPUT_SIGNATURE_FIELD = "cache_input_signature"


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def provider_signature(config: Any) -> str:
    """Identify a usable provider without including its API key in the hash.

    A missing credential cannot validate yesterday's successful checkpoint.
    The URL is hashed separately so even the signature payload contains no
    raw service address, including any userinfo or query string it may carry.
    """

    def setting(name: str, default: Any = None) -> Any:
        return config.get(name, default) if isinstance(config, Mapping) else getattr(config, name, default)

    model, effort, base_url = setting("model"), setting("reasoning_effort"), setting("base_url")
    api_mode = setting("api_mode", llm_client.API_MODE_RESPONSES)
    if not isinstance(model, str) or not model.strip() or not isinstance(base_url, str) or not base_url.strip():
        raise ConfigurationError("模型与服务地址必须有效，无法验证模型缓存")
    if setting("requires_auth", True) and not setting("api_key"):
        raise ConfigurationError("模型服务凭据不可用，无法验证模型缓存")
    effective_effort = effort if api_mode == llm_client.API_MODE_RESPONSES else None
    if api_mode == llm_client.API_MODE_CHAT_COMPLETIONS:
        if project_llm_settings.is_chat_reasoning_verified(
            base_url, model, effort, setting("chat_reasoning_verified_for", ""),
            chat_stream=setting("chat_stream", False),
        ):
            effective_effort = effort
    identity = {
        "version": REVIEW_CACHE_VERSION,
        "model": model,
        "reasoning_effort": effective_effort,
        "base_url_sha256": hashlib.sha256(base_url.rstrip("/").encode("utf-8")).hexdigest(),
    }
    # Keep the existing Responses identity byte-for-byte compatible. Only the
    # new protocol needs a discriminator; otherwise upgrading would request all
    # successful Responses reviews again. Chat sends effort only when verified;
    # the marker and the transport's streaming option are not semantic inputs.
    if api_mode != llm_client.API_MODE_RESPONSES:
        identity["api_mode"] = api_mode
    return f"{REVIEW_CACHE_VERSION}:provider:{_digest(identity)}"


def review_input_signature(model_input: Mapping[str, Any] | str, prompt: str) -> str:
    """Identify messages/validator without reading provider settings or auth."""
    payload = {
        "version": REVIEW_CACHE_VERSION,
        "input": llm_client.review_request_messages(model_input, prompt),
        "response_schema": REVIEW_RESPONSE_SCHEMA,
        "review_format": SIMPLE_REVIEW_FORMAT,
    }
    return f"{REVIEW_CACHE_VERSION}:input:{_digest(payload)}"


def review_signature(model_input: Mapping[str, Any] | str, prompt: str, provider: str) -> str:
    """Hash the exact system/user messages, provider identity and validator."""
    payload = {"version": REVIEW_CACHE_VERSION, "provider": provider,
               "input_signature": review_input_signature(model_input, prompt)}
    return f"{REVIEW_CACHE_VERSION}:{_digest(payload)}"


def review_for_report(review: Any, model_input: Mapping[str, Any] | str,
                      prompt: str | None) -> tuple[dict[str, Any] | None, str]:
    """Validate only local inputs; reporting never resolves a provider or auth.

    Unsigned historic successes remain readable, explicitly unverified. A
    record claiming the new format needs both signatures and a matching input;
    a missing or changed input cannot be upgraded by the aggregate sidecar.
    """
    if not isinstance(review, dict):
        return None, "not_applicable"
    if review.get("status") != "success":
        return review, "not_applicable"
    is_simple = review.get("review_format") == SIMPLE_REVIEW_FORMAT or any(
        name in review for name in REVIEW_RESPONSE_SCHEMA["properties"]
    )
    if not is_simple:
        # Old rich records remain available offline, but are never certified
        # against today's different prompt and two-field response contract.
        return review, "legacy_unverified"
    try:
        simple_review_payload(review)
    except JsonSchemaValidationError:
        return None, "invalid_review_format"
    if review.get("review_format") != SIMPLE_REVIEW_FORMAT:
        return None, "invalid_review_format"
    if not all(isinstance(review.get(field), str) and review[field].strip()
               for field in (CACHE_SIGNATURE_FIELD, CACHE_INPUT_SIGNATURE_FIELD)):
        return None, "missing_signature"
    if prompt is None:
        return None, "prompt_unavailable"
    if review[CACHE_INPUT_SIGNATURE_FIELD] != review_input_signature(model_input, prompt):
        return None, "input_changed"
    return review, "input_match"


def _current_success(row: Mapping[str, Any]) -> bool:
    if row.get("review_format") != SIMPLE_REVIEW_FORMAT:
        return False
    if not all(isinstance(row.get(field), str) and row[field].strip()
               for field in (CACHE_SIGNATURE_FIELD, CACHE_INPUT_SIGNATURE_FIELD)):
        return False
    try:
        simple_review_payload(row)
    except JsonSchemaValidationError:
        return False
    return True


def _input_signature_matches(row: Mapping[str, Any], key: str,
                             input_signatures: Mapping[str, str | None] | None) -> bool:
    # None preserves the structural-only contract for existing helper callers.
    # All request/resume paths pass their exact per-job prompt/input identity.
    if input_signatures is None:
        return True
    expected = input_signatures.get(key)
    return isinstance(expected, str) and bool(expected.strip()) and row.get(CACHE_INPUT_SIGNATURE_FIELD) == expected


def matching_reviews(payload: Any, signatures: Mapping[str, str | None], *,
                     input_signatures: Mapping[str, str | None] | None = None) -> dict[str, dict[str, Any]]:
    """Read only signed results for the current input, including failures."""
    rows = payload.get("reviews", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return {}
    return {
        str(row["job_id"]): row for row in rows
        if isinstance(row, dict) and row.get("job_id") is not None
        and signatures.get(str(row["job_id"])) is not None
        and row.get(CACHE_SIGNATURE_FIELD) == signatures[str(row["job_id"])]
        and _input_signature_matches(row, str(row["job_id"]), input_signatures)
        and row.get("status") in ("success", "failed")
        and (row.get("status") != "success" or _current_success(row))
    }


@dataclass
class CacheSelection:
    reviews: dict[str, dict[str, Any]]
    previous_reviews: list[dict[str, Any]]
    invalidated_count: int = 0
    legacy_count: int = 0
    excluded_count: int = 0

    @property
    def hit_count(self) -> int:
        return sum(row.get("status") == "success" for row in self.reviews.values())

    def document(self, generated_at: str) -> dict[str, Any]:
        return {
            "generated_at": generated_at,
            "cache_version": REVIEW_CACHE_VERSION,
            "reviews": list(self.reviews.values()),
            "previous_reviews": self.previous_reviews,
        }


def select_cached_reviews(payload: Any, signatures: Mapping[str, str | None], *,
                          retry_failures: bool = True,
                          input_signatures: Mapping[str, str | None] | None = None) -> CacheSelection:
    """Keep valid successes and retain all displaced records as audit history.

    Legacy sidecar hashes cannot upgrade an unsigned record.  Archived records
    are not candidates for reuse, and out-of-scope IDs never affect counts.
    ``retry_failures=False`` normalizes a finished/interrupted child checkpoint
    without discarding its current signed request failures.
    ``input_signatures`` compares the independently stored input identity as
    well as the combined provider signature. Resume/monitor/finalize callers
    supply it so a corrupt nonempty input signature cannot become a cache hit.
    """
    payload = payload if isinstance(payload, dict) else {}
    old_history = payload.get("previous_reviews", [])
    history = [entry for entry in old_history if isinstance(entry, dict)] if isinstance(old_history, list) else []
    seen = {_digest(entry) for entry in history}
    rows = payload.get("reviews", [])
    old_rows = {str(row["job_id"]): row for row in rows
                if isinstance(row, dict) and row.get("job_id") is not None} if isinstance(rows, list) else {}
    selected = CacheSelection({}, list(history))
    for key, row in old_rows.items():
        signature = row.get(CACHE_SIGNATURE_FIELD)
        expected = signatures.get(key)
        signature_matches = expected is not None and signature == expected
        input_matches = _input_signature_matches(row, key, input_signatures)
        matches = signature_matches and input_matches
        valid_success = row.get("status") == "success" and _current_success(row)
        if matches and (valid_success or not retry_failures and row.get("status") == "failed"):
            selected.reviews[key] = row
            continue
        if key not in signatures:
            reason = "not_current_input"
            selected.excluded_count += 1
        else:
            if row.get("status") == "success":
                selected.invalidated_count += 1
                selected.legacy_count += not bool(signature)
            missing_input_signature = row.get("review_format") == SIMPLE_REVIEW_FORMAT and not (
                isinstance(row.get(CACHE_INPUT_SIGNATURE_FIELD), str) and row[CACHE_INPUT_SIGNATURE_FIELD].strip())
            reason = ("missing_signature" if not signature or missing_input_signature else "configuration_unavailable" if expected is None
                      else "signature_changed" if not signature_matches else "input_signature_changed" if not input_matches
                      else "review_format_changed"
                      if row.get("status") == "success" else "retry_failed")
        entry = {"reason": reason, "review": row}
        fingerprint = _digest(entry)
        if fingerprint not in seen:
            selected.previous_reviews.append(entry)
            seen.add(fingerprint)
    return selected
