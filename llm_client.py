"""Small synchronous client for OpenAI-compatible model providers.

The collector runs the client from a thread pool, so this module deliberately
does not keep a shared session or any mutable global request state.  Requests
use the standard library and the transport can be injected in tests.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from project_resources import DEFAULT_PROMPT as DEFAULT_PROMPT_PATH
import project_llm_settings

DEFAULT_CODEX_DIR = Path.home() / ".codex"
APPLICATION_USER_AGENT = "boss-architecture-collector"
API_MODE_RESPONSES = project_llm_settings.API_MODE_RESPONSES
API_MODE_CHAT_COMPLETIONS = project_llm_settings.API_MODE_CHAT_COMPLETIONS
API_MODES = project_llm_settings.API_MODES


def _normalize_api_mode(value: Any) -> str:
    """Return the canonical OpenAI-compatible protocol name."""

    if value is None:
        return API_MODE_RESPONSES
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "response": API_MODE_RESPONSES,
        "responses_api": API_MODE_RESPONSES,
        "chat": API_MODE_CHAT_COMPLETIONS,
        "chat_completion": API_MODE_CHAT_COMPLETIONS,
        "chat_completions_api": API_MODE_CHAT_COMPLETIONS,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in API_MODES:
        raise ConfigurationError(
            "API 协议模式必须是 responses 或 chat_completions"
        )
    return normalized


class LLMClientError(RuntimeError):
    """Base class for configuration, transport, and response errors."""


class ConfigurationError(LLMClientError):
    """The Codex provider configuration is missing or malformed."""


class LLMRequestError(LLMClientError):
    """A request failed with an HTTP or transport error."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMTimeoutError(LLMRequestError):
    """The provider did not respond before the request timeout."""


class LLMRateLimitError(LLMRequestError):
    """The provider returned HTTP 429."""


class LLMServerError(LLMRequestError):
    """The provider returned a retryable 5xx response."""


class LLMHTTPError(LLMRequestError):
    """The provider returned a non-retryable HTTP response."""


class InvalidJsonResponseError(LLMClientError):
    """The model response did not contain a valid JSON document."""


class JsonSchemaValidationError(LLMClientError):
    """The decoded model response failed the requested basic JSON schema."""


# A single corrective pass is enough to handle common provider/model formatting
# drift without allowing malformed outputs to loop indefinitely.
REVIEW_FORMAT_REPAIR_ATTEMPTS = 1


@dataclass(frozen=True)
class CodexConfig(Mapping[str, Any]):
    """Resolved settings used by :class:`CodexResponsesClient`.

    ``api_key`` is intentionally excluded from ``repr`` and ``str``.  The
    mapping methods are provided for callers that prefer ``config["model"]``
    over attributes without exposing a mutable dictionary.
    """

    model: str = "gpt-6.1-sol"
    reasoning_effort: str = "max"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = field(default=None, repr=False)
    provider: str = "openai"
    timeout: float = 60.0
    max_retries: int = 3
    backoff_factor: float = 0.5
    disable_response_storage: bool = True
    requires_auth: bool = True
    api_mode: str = API_MODE_RESPONSES
    chat_reasoning_verified_for: str = ""
    # Preserve the persisted key while sharing the option across protocols.
    chat_stream: bool = False

    def __post_init__(self) -> None:
        # Dataclass callers and mapping callers receive the same validation.
        object.__setattr__(self, "api_mode", _normalize_api_mode(self.api_mode))
        if not isinstance(self.chat_stream, bool):
            raise ConfigurationError("流式响应设置必须是布尔值")

    def __getitem__(self, key: str) -> Any:
        if key == "api_key":
            return self.api_key
        if key == "model_reasoning_effort":
            return self.reasoning_effort
        if key == "endpoint":
            return self.endpoint
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def __iter__(self):
        return iter(
            (
                "model",
                "reasoning_effort",
                "base_url",
                "api_key",
                "provider",
                "timeout",
                "max_retries",
                "backoff_factor",
                "disable_response_storage",
                "requires_auth",
                "api_mode",
                "chat_reasoning_verified_for",
                "chat_stream",
            )
        )

    def __len__(self) -> int:
        return 13

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    @property
    def _api_root(self) -> str:
        base = self.base_url.rstrip("/")
        for suffix in ("/responses", "/chat/completions"):
            if base.endswith(suffix):
                return base[: -len(suffix)]
        return base

    @property
    def responses_endpoint(self) -> str:
        return self._api_root + "/responses"

    @property
    def chat_completions_endpoint(self) -> str:
        return self._api_root + "/chat/completions"

    @property
    def endpoint(self) -> str:
        return (self.chat_completions_endpoint
                if self.api_mode == API_MODE_CHAT_COMPLETIONS
                else self.responses_endpoint)

    @property
    def effective_reasoning_effort(self) -> str | None:
        """Return only the effort this configuration is allowed to send."""
        if self.api_mode == API_MODE_RESPONSES:
            return self.reasoning_effort
        if project_llm_settings.is_chat_reasoning_verified(
            self.base_url, self.model, self.reasoning_effort, self.chat_reasoning_verified_for,
            chat_stream=self.chat_stream,
        ):
            return self.reasoning_effort
        return None

    def redacted(self) -> dict[str, Any]:
        """Return diagnostic settings with the credential removed."""

        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "base_url": self.base_url,
            "provider": self.provider,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "backoff_factor": self.backoff_factor,
            "disable_response_storage": self.disable_response_storage,
            "requires_auth": self.requires_auth,
            "api_mode": self.api_mode,
            "chat_reasoning_verified_for": self.chat_reasoning_verified_for,
            "chat_stream": self.chat_stream,
        }

    def private_payload(self) -> str:
        """Serialize only for a private child stdin, never an artifact or log."""
        return json.dumps({**self.redacted(), "api_key": self.api_key}, ensure_ascii=True)


def config_from_private_payload(payload: str) -> CodexConfig:
    """Parse a bounded, complete config snapshot without touching local auth."""
    try:
        if len(payload) > 65536:
            raise ValueError
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != set(CodexConfig().redacted()) | {"api_key"}:
            raise ValueError
        config = CodexConfig(**value)
        if not all(isinstance(item, str) and item.strip() for item in (config.model, config.base_url, config.reasoning_effort)):
            raise ValueError
        if config.requires_auth and (not isinstance(config.api_key, str) or not config.api_key.strip()):
            raise ValueError
        return config
    except (ValueError, TypeError):
        raise ConfigurationError("AI 配置快照无效") from None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib  # type: ignore[import-not-found]

        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
        try:
            import tomli  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise ConfigurationError("读取 Codex TOML 需要 tomllib 或 tomli") from exc
        with path.open("rb") as handle:
            value = tomli.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"无法读取 Codex 配置: {path}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("Codex 配置必须是 TOML 对象")
    return value


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"无法读取 Codex auth.json: {path}") from exc


def _find_secret(value: Any) -> str | None:
    """Find a conventional auth key without serialising the auth object."""

    preferred = {"openai_api_key", "api_key", "apikey", "access_token", "token"}
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in preferred and isinstance(item, str) and item.strip():
                return item.strip()
        for item in value.values():
            found = _find_secret(item)
            if found:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            found = _find_secret(item)
            if found:
                return found
    return None


def _as_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_positive_number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _as_nonnegative_number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _as_nonnegative_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


_UNREAD_PROJECT_SETTINGS = object()


def load_codex_config(
    config_path: str | os.PathLike[str] | None = None,
    auth_path: str | os.PathLike[str] | None = None,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    provider: str | None = None,
    api_mode: str | None = None,
    chat_reasoning_verified_for: str | None = None,
    chat_stream: bool | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    backoff_factor: float | None = None,
    project_settings_path: str | os.PathLike[str] | None = None,
    project_settings_snapshot: Any = _UNREAD_PROJECT_SETTINGS,
) -> CodexConfig:
    """Load local Codex settings, with explicit arguments taking precedence.

    Missing ``config.toml`` is allowed when all request settings are supplied;
    this makes the client straightforward to use in tests and other hosts.
    The selected provider's ``env_key`` or direct bearer token takes precedence
    over OpenAI account credentials.  An explicitly configured but missing
    ``env_key`` is an error, not permission to try an unrelated credential.
    """

    env_codex_home = os.environ.get("CODEX_HOME")
    default_codex_dir = Path(env_codex_home).expanduser() if env_codex_home else DEFAULT_CODEX_DIR
    resolved_config_path = Path(config_path).expanduser() if config_path else default_codex_dir / "config.toml"
    raw: dict[str, Any] = {}
    if resolved_config_path.is_file():
        raw = _read_toml(resolved_config_path)
    elif config_path is not None:
        raise ConfigurationError(f"Codex 配置不存在: {resolved_config_path}")

    # Keep the generic client isolated unless a project caller explicitly opts
    # into its own provider settings. This leaves direct Codex-config reads and
    # other clients unchanged.
    project_settings = project_settings_snapshot if project_settings_snapshot is not _UNREAD_PROJECT_SETTINGS else (
        project_llm_settings.load_settings(project_settings_path)
        if project_settings_path is not None else None
    )
    selected_provider = provider or (project_settings or {}).get("provider") or raw.get("model_provider") or "openai"
    providers = raw.get("model_providers")
    provider_config: Mapping[str, Any] = {}
    if isinstance(providers, Mapping):
        candidate = providers.get(selected_provider)
        if isinstance(candidate, Mapping):
            provider_config = candidate

    if base_url is None:
        base_url = str((project_settings or {}).get("base_url") or provider_config.get("base_url") or raw.get("base_url") or "https://api.openai.com/v1")
    if not base_url.strip():
        raise ConfigurationError("Codex provider base_url 不能为空")
    selected_model = model or (project_settings or {}).get("model") or raw.get("model") or provider_config.get("model") or "gpt-6.1-sol"
    selected_effort = (
        reasoning_effort
        or (project_settings or {}).get("reasoning_effort")
        or raw.get("model_reasoning_effort")
        or provider_config.get("reasoning_effort")
        or "max"
    )
    selected_api_mode = _normalize_api_mode(
        api_mode
        or (project_settings or {}).get("api_mode")
        or provider_config.get("api_mode")
        or raw.get("api_mode")
        or API_MODE_RESPONSES
    )

    requires_auth = True if project_settings is not None else _as_bool(provider_config.get("requires_openai_auth"), True)
    if api_key is None:
        if project_settings is not None:
            try:
                api_key = project_llm_settings.load_api_key(project_settings["credential_target"])
            except project_llm_settings.ProjectProviderError as exc:
                raise ConfigurationError("无法读取本机项目 API Key") from exc
            if not api_key:
                raise ConfigurationError("本机项目 API Key 尚未配置")
        elif "env_key" in provider_config:
            env_key = provider_config["env_key"]
            token = os.environ.get(env_key) if isinstance(env_key, str) and env_key.strip() else None
            if not isinstance(token, str) or not token.strip():
                raise ConfigurationError("所选 Codex provider 的 env_key 未提供有效凭据；不会回退尝试其他密钥")
            api_key = token.strip()
        elif "experimental_bearer_token" in provider_config:
            token = provider_config["experimental_bearer_token"]
            if not isinstance(token, str) or not token.strip():
                raise ConfigurationError("所选 Codex provider 的 direct bearer token 无效；不会回退尝试其他密钥")
            api_key = token.strip()
        elif requires_auth:
            resolved_auth_path = Path(auth_path).expanduser() if auth_path else default_codex_dir / "auth.json"
            auth_value = _find_secret(_read_json(resolved_auth_path)) if resolved_auth_path.is_file() else None
            api_key = auth_value or os.environ.get("OPENAI_API_KEY")

    selected_timeout = _as_positive_number(timeout, _as_positive_number(raw.get("timeout"), 60.0))
    selected_retries = _as_nonnegative_int(
        max_retries,
        _as_nonnegative_int(raw.get("max_retries"), 3),
    )
    selected_backoff = _as_nonnegative_number(
        backoff_factor,
        _as_nonnegative_number(raw.get("backoff_factor"), 0.5),
    )
    return CodexConfig(
        model=str(selected_model),
        reasoning_effort=str(selected_effort),
        base_url=str(base_url).rstrip("/"),
        api_key=api_key,
        provider=str(selected_provider),
        timeout=selected_timeout,
        max_retries=selected_retries,
        backoff_factor=selected_backoff,
        disable_response_storage=_as_bool(raw.get("disable_response_storage"), True),
        requires_auth=requires_auth,
        api_mode=selected_api_mode,
        chat_reasoning_verified_for=(
            (project_settings or {}).get("chat_reasoning_verified_for", "")
            if chat_reasoning_verified_for is None else chat_reasoning_verified_for
        ),
        chat_stream=(
            (project_settings or {}).get("chat_stream", False)
            if chat_stream is None else chat_stream
        ),
    )


def load_review_config(*, codex_home: str | None = None, base_url: str | None = None,
                       api_key: str | None = None, model: str | None = None,
                       reasoning_effort: str | None = None, api_mode: str | None = None,
                       timeout: int = 120, max_retries: int = 3) -> CodexConfig:
    """Resolve one immutable effective review config for signatures and I/O."""
    from screening_policy import AI_DEFAULT_MODEL, AI_DEFAULT_REASONING_EFFORT

    env_home = os.environ.get("CODEX_HOME")
    home = Path(codex_home).expanduser() if codex_home else Path(env_home).expanduser() if env_home else None
    settings_path = None if codex_home else project_llm_settings.default_settings_path()
    settings = project_llm_settings.load_settings(settings_path) if settings_path is not None else None
    return load_codex_config(
        config_path=home / "config.toml" if home else None,
        auth_path=home / "auth.json" if home else None,
        project_settings_path=settings_path, project_settings_snapshot=settings,
        base_url=base_url, api_key=api_key,
        model=model or (settings or {}).get("model") or AI_DEFAULT_MODEL,
        reasoning_effort=reasoning_effort or (settings or {}).get("reasoning_effort") or AI_DEFAULT_REASONING_EFFORT,
        api_mode=api_mode or (settings or {}).get("api_mode") or (None if codex_home else API_MODE_RESPONSES),
        timeout=timeout, max_retries=max_retries,
    )


def _object_to_mapping(value: Any) -> Any:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return vars(value)
    return value


def _empty_function_call_placeholder(value: Any) -> bool:
    """Recognize only the empty legacy-call objects emitted by gateways."""
    value = _object_to_mapping(value)
    return (isinstance(value, Mapping)
            and all(key in {"name", "arguments"} for key in value)
            and all(item is None or isinstance(item, str) and item == "" for item in value.values()))


def extract_output_text(response: Any) -> str:
    """Extract text from Responses or Chat Completions response envelopes."""

    response = _object_to_mapping(response)
    if isinstance(response, str):
        return response
    if not isinstance(response, Mapping):
        direct_text = getattr(response, "output_text", None)
        if isinstance(direct_text, str):
            return direct_text
        direct_output = getattr(response, "output", None)
        if direct_output is not None:
            response = {"output": direct_output}
    if not isinstance(response, Mapping):
        raise InvalidJsonResponseError("模型 API 返回体不是对象")

    # A partial/error response may still contain perfectly valid JSON text.
    # Never promote that text to a successful review.  Status-less test and
    # compatible-provider envelopes remain supported.
    if (response.get("status") not in (None, "completed")
            or response.get("error") is not None
            or response.get("incomplete_details") is not None):
        raise InvalidJsonResponseError("模型 API 返回错误或未完成的响应")

    # Chat Completions returns the assistant text under choices[].message.
    # Some compatible providers use a content-part list, so accept both the
    # normal string form and the small text-part subset used by OpenAI clients.
    choices = response.get("choices")
    if choices is not None:
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)) or not choices:
            raise InvalidJsonResponseError("Chat Completions 返回体缺少 choices")
        first = _object_to_mapping(choices[0])
        if not isinstance(first, Mapping) or first.get("finish_reason") not in (None, "stop"):
            raise InvalidJsonResponseError("Chat Completions 未正常完成文本响应")
        message = first.get("message") if isinstance(first, Mapping) else None
        message = _object_to_mapping(message)
        if isinstance(message, Mapping):
            function_call = message.get("function_call")
            if (message.get("refusal") or message.get("tool_calls")
                    or function_call is not None and not _empty_function_call_placeholder(function_call)):
                raise InvalidJsonResponseError("Chat Completions 未返回可用的复核文本")
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, str):
            return content
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
            chunks = []
            for part in content:
                part = _object_to_mapping(part)
                if isinstance(part, str):
                    chunks.append(part)
                elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
            if chunks:
                return "".join(chunks)
        raise InvalidJsonResponseError("Chat Completions 返回体缺少 message.content 文本")

    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text
    if isinstance(output_text, Sequence) and not isinstance(output_text, (str, bytes, bytearray)):
        chunks = [str(item.get("text", item)) if isinstance(item, Mapping) else str(item) for item in output_text]
        if chunks:
            return "".join(chunks)

    output = response.get("output")
    chunks: list[str] = []
    if isinstance(output, Sequence) and not isinstance(output, (str, bytes, bytearray)):
        for item in output:
            item = _object_to_mapping(item)
            if isinstance(item, str):
                chunks.append(item)
                continue
            if not isinstance(item, Mapping):
                continue
            direct_text = item.get("text")
            if isinstance(direct_text, str):
                chunks.append(direct_text)
            content = item.get("content")
            if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
                for part in content:
                    part = _object_to_mapping(part)
                    if isinstance(part, str):
                        chunks.append(part)
                    elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
                        chunks.append(part["text"])
    if chunks:
        return "".join(chunks)
    raise InvalidJsonResponseError("Responses API 返回体缺少 output_text/output 文本")


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL)


def _clean_json_text(text: str) -> str:
    cleaned = text.strip().lstrip("\ufeff")
    match = _FENCE_RE.search(cleaned)
    if match:
        return match.group(1).strip()
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.removesuffix("``").strip()
    return cleaned


def parse_json_response(response: Any) -> Any:
    """Decode a JSON model result, accepting Markdown JSON fences."""

    text = extract_output_text(response) if not isinstance(response, str) else response
    cleaned = _clean_json_text(text)
    try:
        return json.loads(cleaned)
    except (TypeError, json.JSONDecodeError) as exc:
        # Do not include model output in the error: it may contain credentials
        # copied from a job description or a provider diagnostic.
        raise InvalidJsonResponseError("模型响应不是合法 JSON") from exc


def validate_json_schema(value: Any, schema: Mapping[str, Any], *, path: str = "$") -> None:
    """Perform a deliberately small, dependency-free JSON Schema check."""

    expected_type = schema.get("type")
    type_ok = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if isinstance(expected_type, str) and not type_ok.get(expected_type, True):
        raise JsonSchemaValidationError(f"{path}: 期望类型 {expected_type}")
    if (isinstance(expected_type, Sequence) and not isinstance(expected_type, (str, bytes, bytearray))
            and not any(type_ok.get(item, True) for item in expected_type)):
        raise JsonSchemaValidationError(f"{path}: 期望类型 {' 或 '.join(map(str, expected_type))}")
    if isinstance(value, Mapping):
        required = schema.get("required", [])
        if isinstance(required, Sequence) and not isinstance(required, (str, bytes, bytearray)):
            for key in required:
                if key not in value:
                    raise JsonSchemaValidationError(f"{path}: 缺少字段 {key}")
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            if schema.get("additionalProperties") is False:
                if any(key not in properties for key in value):
                    raise JsonSchemaValidationError(f"{path}: 不允许额外字段")
            for key, child_schema in properties.items():
                if key in value and isinstance(child_schema, Mapping):
                    validate_json_schema(value[key], child_schema, path=f"{path}.{key}")
    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise JsonSchemaValidationError(f"{path}: 字符串至少需要 {min_length} 个字符")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise JsonSchemaValidationError(f"{path}: 字符串内容不符合要求")
    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise JsonSchemaValidationError(f"{path}: 至少需要 {min_items} 项")
        child_schema = schema.get("items")
        if isinstance(child_schema, Mapping):
            for index, child in enumerate(value):
                validate_json_schema(child, child_schema, path=f"{path}[{index}]")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise JsonSchemaValidationError(f"{path}: 数值不能小于 {minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise JsonSchemaValidationError(f"{path}: 数值不能大于 {maximum}")
    enum = schema.get("enum")
    if isinstance(enum, Sequence) and value not in enum:
        raise JsonSchemaValidationError(f"{path}: 值不在允许范围内")


_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}
_PRESENT = {"type": ("boolean", "string"), "enum": (True, False, "unknown")}
_EXPERIENCE_YEARS = {"type": ("number", "null"), "minimum": 0}
_REVIEW_DIMENSIONS = ("方案设计", "系统边界与集成", "Agent/RAG/Workflow", "知识与数据", "PoC/交付", "迭代与评测")
_REVIEW_EXCLUSIONS = ("pure_sales", "pure_algorithm", "pure_infrastructure", "irrelevant")


def _has_recognized_review_structure(value: Mapping[str, Any]) -> bool:
    """Reject unrelated/empty objects before conservative defaulting.

    A deliberate all-unknown review is valid, but an arbitrary JSON mapping
    must not become one just because the normalizer can manufacture defaults.
    At least one recognized semantic judgment is required in the model output.
    """
    def recognized_present(raw: Any) -> bool:
        return (type(raw) is bool or isinstance(raw, str) and raw.strip().casefold() in {
            "unknown", "true", "yes", "是", "有", "存在", "present",
            "false", "no", "否", "无", "不存在", "absent",
        })

    relevance = value.get("target_relevance")
    if (isinstance(relevance, Mapping)
            and isinstance(relevance.get("assessment"), str)
            and relevance.get("assessment") in {"direct", "adjacent", "unrelated", "insufficient"}):
        return True
    for section, names in (("dimensions", _REVIEW_DIMENSIONS), ("exclusions", _REVIEW_EXCLUSIONS)):
        items = value.get(section)
        if isinstance(items, Mapping):
            for name in names:
                item = items.get(name)
                if isinstance(item, Mapping) and recognized_present(item.get("present")):
                    return True
    experience = value.get("experience_assessment")
    if isinstance(experience, Mapping):
        years = experience.get("minimum_years")
        if (recognized_present(experience.get("hard_conflict"))
                or type(years) in {int, float} and years >= 0):
            return True
    return False


def normalize_review_result(value: Any, *, expected_job_id: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """Normalize explicit legacy-rich reviews, never the default two fields.

    Missing or malformed semantic judgments become unknown/insufficient. Text
    evidence is never fabricated; malformed evidence is dropped and flagged.
    The returned field paths are safe to persist as diagnostics.
    """
    if not isinstance(value, dict):
        raise JsonSchemaValidationError("$: 评估结果必须是 JSON 对象")
    if not _has_recognized_review_structure(value):
        raise JsonSchemaValidationError("$: 缺少可识别的岗位复核判断结构")
    result = dict(value)
    repairs: list[str] = []

    def mark(path: str) -> None:
        repairs.append(path)

    def normalize_present(raw: Any, path: str) -> Any:
        if raw is True or raw is False or raw == "unknown":
            return raw
        if isinstance(raw, str):
            normalized = raw.strip().casefold()
            if normalized in {"true", "yes", "是", "有", "存在", "present"}:
                mark(path)
                return True
            if normalized in {"false", "no", "否", "无", "不存在", "absent"}:
                mark(path)
                return False
        mark(path)
        return "unknown"

    def string_list(raw: Any, path: str) -> list[str]:
        if isinstance(raw, list):
            cleaned = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
            if len(cleaned) != len(raw):
                mark(path)
            return cleaned
        if isinstance(raw, str) and raw.strip():
            mark(path)
            return [raw.strip()]
        if raw not in (None, []):
            mark(path)
        return []

    raw_id = result.get("job_id")
    if expected_job_id is not None and raw_id not in (None, "", expected_job_id):
        raise JsonSchemaValidationError("$.job_id: 岗位 ID 与当前任务不匹配")
    if not isinstance(raw_id, str) or not raw_id.strip():
        if expected_job_id is None and not isinstance(raw_id, str):
            raise JsonSchemaValidationError("$.job_id: 缺少可用岗位 ID")
        if expected_job_id is not None:
            result["job_id"] = expected_job_id
            mark("$.job_id")

    relevance = result.get("target_relevance")
    allowed_relevance = {"direct", "adjacent", "unrelated", "insufficient"}
    if not isinstance(relevance, dict):
        relevance = {}
        result["target_relevance"] = relevance
        mark("$.target_relevance")
    if not isinstance(relevance.get("assessment"), str) or relevance["assessment"] not in allowed_relevance:
        relevance["assessment"] = "insufficient"
        mark("$.target_relevance.assessment")
    relevance["evidence"] = string_list(relevance.get("evidence"), "$.target_relevance.evidence")
    if relevance["assessment"] in {"direct", "adjacent", "unrelated"} and not relevance["evidence"]:
        relevance["assessment"] = "insufficient"
        mark("$.target_relevance.assessment")

    dimensions = result.get("dimensions")
    if not isinstance(dimensions, dict):
        dimensions = {}
        result["dimensions"] = dimensions
        mark("$.dimensions")
    for name in _REVIEW_DIMENSIONS:
        path = f"$.dimensions.{name}"
        item = dimensions.get(name)
        if not isinstance(item, dict):
            item = {}
            dimensions[name] = item
            mark(path)
        if "present" not in item:
            item["present"] = "unknown"
            mark(path + ".present")
        else:
            item["present"] = normalize_present(item["present"], path + ".present")
        confidence = item.get("confidence")
        if isinstance(confidence, str):
            try:
                confidence = float(confidence.strip())
            except ValueError:
                confidence = None
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            confidence = 0.0
            mark(path + ".confidence")
        item["confidence"] = confidence
        item["evidence"] = string_list(item.get("evidence"), path + ".evidence")
        if item["present"] is True and not item["evidence"]:
            item["present"] = "unknown"
            mark(path + ".present")

    exclusions = result.get("exclusions")
    if not isinstance(exclusions, dict):
        exclusions = {}
        result["exclusions"] = exclusions
        mark("$.exclusions")
    for name in _REVIEW_EXCLUSIONS:
        path = f"$.exclusions.{name}"
        item = exclusions.get(name)
        if not isinstance(item, dict):
            item = {}
            exclusions[name] = item
            mark(path)
        if "present" not in item:
            item["present"] = "unknown"
            mark(path + ".present")
        else:
            item["present"] = normalize_present(item["present"], path + ".present")
        item["evidence"] = string_list(item.get("evidence"), path + ".evidence")
        if item["present"] is True and not item["evidence"]:
            item["present"] = "unknown"
            mark(path + ".present")

    experience = result.get("experience_assessment")
    if not isinstance(experience, dict):
        experience = {}
        result["experience_assessment"] = experience
        mark("$.experience_assessment")
    years = experience.get("minimum_years")
    if years is None or type(years) in {int, float} and years >= 0:
        pass
    elif isinstance(years, str):
        try:
            years = float(years.strip())
            if years < 0:
                years = None
            mark("$.experience_assessment.minimum_years")
        except ValueError:
            years = None
            mark("$.experience_assessment.minimum_years")
    else:
        years = None
        mark("$.experience_assessment.minimum_years")
    experience["minimum_years"] = years
    if "hard_conflict" not in experience:
        experience["hard_conflict"] = "unknown"
        mark("$.experience_assessment.hard_conflict")
    else:
        experience["hard_conflict"] = normalize_present(experience["hard_conflict"], "$.experience_assessment.hard_conflict")
    experience["evidence"] = string_list(experience.get("evidence"), "$.experience_assessment.evidence")
    if experience["hard_conflict"] is True and not experience["evidence"]:
        experience["hard_conflict"] = "unknown"
        mark("$.experience_assessment.hard_conflict")

    for name in ("ability_gaps", "verification_items"):
        result[name] = string_list(result.get(name), f"$.{name}")
    if not isinstance(result.get("summary"), str):
        result["summary"] = ""
        mark("$.summary")
    if (not isinstance(result.get("evidence_quality"), str)
            or result["evidence_quality"] not in {"high", "medium", "low", "unknown"}):
        result["evidence_quality"] = "unknown"
        mark("$.evidence_quality")
    if not isinstance(result.get("needs_human_review"), bool):
        result["needs_human_review"] = True
        mark("$.needs_human_review")
    if repairs:
        result["needs_human_review"] = True
        result["_schema_repair_fields"] = list(dict.fromkeys(repairs))
    return result, list(dict.fromkeys(repairs))

LEGACY_REVIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": (
        "job_id",
        "target_relevance",
        "dimensions",
        "exclusions",
        "experience_assessment",
        "ability_gaps",
        "verification_items",
        "summary",
        "evidence_quality",
        "needs_human_review",
    ),
    "properties": {
        "job_id": {"type": "string"},
        "target_relevance": {
            "type": "object",
            "required": ("assessment", "evidence"),
            "properties": {
                "assessment": {"enum": ("direct", "adjacent", "unrelated", "insufficient")},
                "evidence": _STRING_ARRAY,
            },
        },
        "dimensions": {
            "type": "object",
            "required": ("方案设计", "系统边界与集成", "Agent/RAG/Workflow", "知识与数据", "PoC/交付", "迭代与评测"),
            "properties": {
                name: {
                    "type": "object",
                    "required": ("present", "confidence", "evidence"),
                    "properties": {
                        "present": _PRESENT,
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "evidence": _STRING_ARRAY,
                    },
                }
                for name in ("方案设计", "系统边界与集成", "Agent/RAG/Workflow", "知识与数据", "PoC/交付", "迭代与评测")
            },
        },
        "exclusions": {
            "type": "object",
            "required": ("pure_sales", "pure_algorithm", "pure_infrastructure", "irrelevant"),
            "properties": {
                name: {"type": "object", "required": ("present", "evidence"), "properties": {"present": _PRESENT, "evidence": _STRING_ARRAY}}
                for name in ("pure_sales", "pure_algorithm", "pure_infrastructure", "irrelevant")
            },
        },
        "experience_assessment": {
            "type": "object",
            "required": ("minimum_years", "hard_conflict", "evidence"),
            "properties": {
                "minimum_years": _EXPERIENCE_YEARS,
                "hard_conflict": _PRESENT,
                "evidence": _STRING_ARRAY,
            },
        },
        "ability_gaps": _STRING_ARRAY,
        "verification_items": _STRING_ARRAY,
        "summary": {"type": "string"},
        "evidence_quality": {"type": "string", "enum": ("high", "medium", "low", "unknown")},
        "needs_human_review": {"type": "boolean"},
    },
}


SIMPLE_REVIEW_FORMAT = "simple-v1"
REVIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ("判断", "理由"),
    "properties": {
        "判断": {"type": "string", "enum": ("符合", "不符合", "信息不足")},
        "理由": {"type": "string", "minLength": 1, "pattern": r"\S"},
    },
}


def simple_review_payload(value: Any) -> dict[str, Any]:
    """Validate judgments inside an application-owned checkpoint, not metadata."""
    if not isinstance(value, Mapping):
        raise JsonSchemaValidationError("$: 评估结果必须是 JSON 对象")
    legacy_fields = set(LEGACY_REVIEW_RESPONSE_SCHEMA["properties"]) - {"job_id", "needs_human_review"}
    if any(name in value for name in legacy_fields):
        raise JsonSchemaValidationError("$: 旧版复核结构不是两字段判断")
    payload = {name: value[name] for name in REVIEW_RESPONSE_SCHEMA["properties"] if name in value}
    validate_json_schema(payload, REVIEW_RESPONSE_SCHEMA)
    return payload


def load_review_prompt(path: str | os.PathLike[str] | None = None) -> str:
    """Read the default job-description review prompt."""

    prompt_path = Path(path).expanduser() if path else DEFAULT_PROMPT_PATH
    try:
        return prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(f"无法读取提示词: {prompt_path}") from exc


Transport = Callable[[urllib.request.Request, float], Any]


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Keep authenticated API requests at the explicitly configured endpoint."""

    def http_error_302(self, req, fp, code, msg, headers):
        # Reject before even parsing Location, including malformed targets.
        # _send closes this response and replaces the exception with fixed,
        # status-only guidance. Same-origin method/body changes are refused too.
        raise urllib.error.HTTPError(req.full_url, code, "API redirect blocked", headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def review_request_messages(job: Mapping[str, Any] | str, prompt: str) -> list[dict[str, str]]:
    """Build the actual messages shared by requests and cache signatures."""
    if isinstance(job, Mapping):
        job_text = json.dumps(job, ensure_ascii=False, sort_keys=True)
    elif isinstance(job, str):
        job_text = job
    else:
        raise TypeError("job 必须是映射或字符串")
    return [
        {"role": "system", "content": prompt.rstrip()},
        {"role": "user", "content": f"岗位原始数据：\n```json\n{job_text}\n```"},
    ]


class CodexResponsesClient:
    """Synchronous Responses/Chat client; retain the public name for callers."""

    supports_conversations = True
    # Enough room for fifteen turns including one correction exchange per turn.
    MAX_CONVERSATION_MESSAGES = 64

    def __init__(
        self,
        config: CodexConfig | Mapping[str, Any] | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
        auth_path: str | os.PathLike[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        provider: str | None = None,
        api_mode: str | None = None,
        chat_reasoning_verified_for: str | None = None,
        chat_stream: bool | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        backoff_factor: float | None = None,
        transport: Transport | None = None,
        prompt_path: str | os.PathLike[str] | None = None,
        response_schema: Mapping[str, Any] | None = REVIEW_RESPONSE_SCHEMA,
    ) -> None:
        if config is None:
            resolved = load_codex_config(
                config_path,
                auth_path,
                model=model,
                reasoning_effort=reasoning_effort,
                base_url=base_url,
                api_key=api_key,
                provider=provider,
                api_mode=api_mode,
                chat_reasoning_verified_for=chat_reasoning_verified_for,
                chat_stream=chat_stream,
                timeout=timeout,
                max_retries=max_retries,
                backoff_factor=backoff_factor,
            )
        elif isinstance(config, CodexConfig):
            resolved = CodexConfig(
                model=model or config.model,
                reasoning_effort=reasoning_effort or config.reasoning_effort,
                base_url=(base_url or config.base_url).rstrip("/"),
                api_key=config.api_key if api_key is None else api_key,
                provider=provider or config.provider,
                timeout=_as_positive_number(timeout, config.timeout),
                max_retries=_as_nonnegative_int(max_retries, config.max_retries),
                backoff_factor=_as_nonnegative_number(backoff_factor, config.backoff_factor),
                disable_response_storage=config.disable_response_storage,
                requires_auth=config.requires_auth,
                api_mode=api_mode or config.api_mode,
                chat_reasoning_verified_for=(
                    config.chat_reasoning_verified_for
                    if chat_reasoning_verified_for is None else chat_reasoning_verified_for
                ),
                chat_stream=config.chat_stream if chat_stream is None else chat_stream,
            )
        elif isinstance(config, Mapping):
            # A mapping is an explicit, self-contained configuration.  Do not
            # silently merge the caller's values with the host's ~/.codex
            # credentials; that would make tests and worker processes depend on
            # whichever desktop account happens to run them.
            defaults = CodexConfig()
            selected_base_url = base_url or config.get("base_url") or defaults.base_url
            resolved = CodexConfig(
                model=str(model or config.get("model") or defaults.model),
                reasoning_effort=str(
                    reasoning_effort
                    or config.get("reasoning_effort")
                    or config.get("model_reasoning_effort")
                    or defaults.reasoning_effort
                ),
                base_url=str(selected_base_url).rstrip("/"),
                api_key=api_key if api_key is not None else config.get("api_key"),
                provider=str(provider or config.get("provider") or defaults.provider),
                timeout=_as_positive_number(
                    timeout if timeout is not None else config.get("timeout"), defaults.timeout
                ),
                max_retries=_as_nonnegative_int(
                    max_retries if max_retries is not None else config.get("max_retries"),
                    defaults.max_retries,
                ),
                backoff_factor=_as_nonnegative_number(
                    backoff_factor if backoff_factor is not None else config.get("backoff_factor"),
                    defaults.backoff_factor,
                ),
                disable_response_storage=_as_bool(
                    config.get("disable_response_storage"), defaults.disable_response_storage
                ),
                requires_auth=_as_bool(
                    config.get("requires_auth", config.get("requires_openai_auth")),
                    defaults.requires_auth,
                ),
                api_mode=_normalize_api_mode(api_mode or config.get("api_mode") or defaults.api_mode),
                chat_reasoning_verified_for=(
                    config.get("chat_reasoning_verified_for", "")
                    if chat_reasoning_verified_for is None else chat_reasoning_verified_for
                ),
                chat_stream=config.get("chat_stream", False) if chat_stream is None else chat_stream,
            )
        else:
            raise ConfigurationError("config 必须是 CodexConfig 或映射")
        self.config = resolved
        self._transport = transport or self._default_transport
        self.prompt_path = Path(prompt_path).expanduser() if prompt_path else DEFAULT_PROMPT_PATH
        self.response_schema = response_schema

    @staticmethod
    def _default_transport(request: urllib.request.Request, timeout: float) -> Any:
        # Use a private opener so process-wide urllib handlers cannot re-enable
        # redirects and forward Authorization to a different origin or HTTP.
        return urllib.request.build_opener(_RejectRedirects()).open(request, timeout=timeout)

    @staticmethod
    def _read_response(response: Any) -> bytes:
        if isinstance(response, bytes):
            return response
        if isinstance(response, str):
            return response.encode("utf-8")
        if isinstance(response, Mapping):
            return json.dumps(response, ensure_ascii=False).encode("utf-8")
        payload = response.read()
        return payload if isinstance(payload, bytes) else str(payload).encode("utf-8")

    @staticmethod
    def _decode_chat_event_stream(raw: bytes) -> dict[str, Any]:
        """Collect standard Chat SSE chunks without exposing reasoning text.

        A finish chunk and [DONE] are both required. A disconnected stream is
        never promoted to a complete response, even if its text is valid JSON.
        """
        try:
            source = raw.decode("utf-8-sig")
        except UnicodeError:
            raise LLMRequestError("Chat Completions API 流式响应编码无效") from None
        content: list[str] = []
        reasoning: list[str] = []
        message: dict[str, Any] = {"role": "assistant"}
        finish_reason: str | None = None
        saw_choice = False
        done = False
        error: Any = None
        data: list[str] = []
        event = ""

        def consume_chunk(chunk: Mapping[str, Any] | None) -> None:
            nonlocal finish_reason, saw_choice, done, error
            if done:
                raise LLMRequestError("Chat Completions API 流式响应结束标记后仍有数据")
            if chunk is None:
                done = True
                return
            if event == "error" or chunk.get("error") is not None:
                error = chunk["error"] if chunk.get("error") is not None else chunk
                return
            choices = chunk.get("choices")
            if not isinstance(choices, list):
                raise LLMRequestError("Chat Completions API 流式响应缺少 choices")
            # Usage-only chunks have an empty choices list. Only aggregate the
            # first completion, matching the normal response extractor.
            if not choices:
                return
            first = next((item for item in choices
                          if isinstance(item, Mapping) and item.get("index", 0) == 0), None)
            if first is None:
                raise LLMRequestError("Chat Completions API 流式响应缺少首个 choice")
            if first.get("error") is not None:
                error = first["error"]
                return
            delta = first.get("delta")
            if not isinstance(delta, Mapping):
                raise LLMRequestError("Chat Completions API 流式响应缺少 delta")
            if delta.get("error") is not None:
                error = delta["error"]
                return
            if delta.get("role") not in (None, "assistant"):
                raise LLMRequestError("Chat Completions API 流式响应角色无效")
            saw_choice = True
            for key, destination in (("content", content), ("reasoning_content", reasoning)):
                part = delta.get(key)
                if part is not None and not isinstance(part, str):
                    raise LLMRequestError("Chat Completions API 流式响应文本格式无效")
                if part:
                    if finish_reason is not None:
                        raise LLMRequestError("Chat Completions API 流式响应完成后仍有文本")
                    destination.append(part)
            for key in ("refusal", "tool_calls"):
                if delta.get(key):
                    message[key] = delta[key]
            function_call = delta.get("function_call")
            if function_call is not None and not _empty_function_call_placeholder(function_call):
                # Never let a later empty placeholder erase an actual call.
                message["function_call"] = function_call
            reason = first.get("finish_reason")
            if reason is not None:
                if not isinstance(reason, str) or not reason or finish_reason is not None:
                    raise LLMRequestError("Chat Completions API 流式响应完成标记无效")
                finish_reason = reason

        def consume() -> None:
            if not data:
                return
            payload = "\n".join(data)
            chunks: list[Mapping[str, Any] | None]
            if payload.strip() == "[DONE]":
                chunks = [None]
            else:
                try:
                    chunk = json.loads(payload)
                except (ValueError, TypeError):
                    # Some compatible gateways omit the blank line between
                    # complete JSON chunks. Prefer standard multiline SSE;
                    # only fall back if every line is a complete object or
                    # the final [DONE] marker. Validate all lines before use.
                    chunks = []
                    try:
                        for index, line in enumerate(data):
                            if line.strip() == "[DONE]" and index == len(data) - 1:
                                chunks.append(None)
                                continue
                            item = json.loads(line)
                            if not isinstance(item, Mapping):
                                raise ValueError
                            chunks.append(item)
                    except (ValueError, TypeError):
                        raise LLMRequestError("Chat Completions API 流式响应包含无效 JSON") from None
                else:
                    if not isinstance(chunk, Mapping):
                        raise LLMRequestError("Chat Completions API 流式响应格式无效")
                    chunks = [chunk]
            for chunk in chunks:
                consume_chunk(chunk)

        for line in source.splitlines():
            if not line:
                consume()
                data = []
                event = ""
            elif line.startswith(":"):
                continue  # SSE heartbeat/comment
            elif line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith(("id:", "retry:")):
                continue
            else:
                raise LLMRequestError("Chat Completions API 流式响应包含无效事件")
        consume()
        if error is None and (not done or not saw_choice or finish_reason is None):
            raise LLMRequestError("Chat Completions API 流式响应中断或未正常结束")
        message["content"] = "".join(content)
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        result = {"choices": [{"finish_reason": finish_reason, "message": message}]}
        if error is not None:
            result["error"] = error
        return result

    @staticmethod
    def _decode_responses_event_stream(raw: bytes) -> dict[str, Any]:
        """Require a complete Responses envelope; never synthesize it from deltas.

        Responses uses response.completed as its terminal success event. Chat's
        optional gateway [DONE] marker cannot substitute for that response.
        Reasoning is ignored, and provider data never appears in diagnostics.
        """
        try:
            source = raw.decode("utf-8-sig")
        except UnicodeError:
            raise LLMRequestError("Responses API 流式响应编码无效") from None
        completed: dict[str, Any] | None = None
        response_id: str | None = None
        done = False
        data: list[str] = []
        event = ""

        def validate_output(value: Mapping[str, Any], *, final: bool) -> None:
            if (value.get("error") is not None or value.get("incomplete_details") is not None
                    or value.get("refusal") or value.get("tool_calls") or value.get("function_call")
                    or value.get("status") not in ((None, "completed") if final
                                                   else (None, "queued", "in_progress", "completed"))):
                raise LLMRequestError("Responses API 流式响应包含错误或未完成的输出")
            if value.get("type") not in (None, "message", "reasoning"):
                raise LLMRequestError("Responses API 流式响应未返回可用的复核文本")
            if value.get("type") == "reasoning":
                return
            if value.get("role") not in (None, "assistant"):
                raise LLMRequestError("Responses API 流式响应角色无效")
            content = value.get("content")
            if content is not None:
                if not isinstance(content, list):
                    raise LLMRequestError("Responses API 流式响应文本格式无效")
                for part in content:
                    if (not isinstance(part, Mapping) or part.get("type") not in (None, "output_text")
                            or not isinstance(part.get("text"), str) or part.get("refusal")
                            or part.get("error") is not None):
                        raise LLMRequestError("Responses API 流式响应未返回可用的复核文本")

        def validate_response(value: Mapping[str, Any], *, final: bool) -> None:
            nonlocal response_id
            if final and value.get("status") != "completed":
                raise LLMRequestError("Responses API 流式响应未正常完成")
            validate_output(value, final=final)
            current_id = value.get("id")
            if current_id is not None:
                if not isinstance(current_id, str) or not current_id.strip():
                    raise LLMRequestError("Responses API 流式响应标识无效")
                if response_id is not None and current_id != response_id:
                    raise LLMRequestError("Responses API 流式响应标识不一致")
                response_id = current_id
            if "choices" in value:
                raise LLMRequestError("Responses API 流式响应格式无效")
            output = value.get("output")
            if output is not None:
                if not isinstance(output, list):
                    raise LLMRequestError("Responses API 流式响应输出格式无效")
                for item in output:
                    if not isinstance(item, Mapping):
                        raise LLMRequestError("Responses API 流式响应输出格式无效")
                    validate_output(item, final=final)

        def consume() -> None:
            nonlocal completed, done
            if not data:
                if event:
                    raise LLMRequestError("Responses API 流式响应缺少事件数据")
                return
            payload = "\n".join(data)
            if payload.strip() == "[DONE]":
                if completed is None or done or event:
                    raise LLMRequestError("Responses API 流式响应缺少完整完成事件")
                done = True
                return
            if completed is not None or done:
                raise LLMRequestError("Responses API 流式响应完成后仍有数据")
            try:
                chunk = json.loads(payload)
            except (ValueError, TypeError):
                raise LLMRequestError("Responses API 流式响应包含无效 JSON") from None
            if not isinstance(chunk, Mapping):
                raise LLMRequestError("Responses API 流式响应格式无效")
            kind = chunk.get("type")
            if not isinstance(kind, str) or not kind or event and event != kind:
                raise LLMRequestError("Responses API 流式响应事件类型无效")
            if (kind in {"error", "response.failed", "response.incomplete", "response.cancelled"}
                    or chunk.get("error") is not None):
                raise LLMRequestError("Responses API 流式响应失败或未完成")
            if kind in {"response.created", "response.queued", "response.in_progress", "response.completed"}:
                response = chunk.get("response")
                if not isinstance(response, Mapping):
                    raise LLMRequestError("Responses API 流式响应缺少响应对象")
                final = kind == "response.completed"
                validate_response(response, final=final)
                if final:
                    completed = dict(response)
                    if isinstance(response.get("output"), list):
                        # Reasoning and commentary are never review answers.
                        # Some gateways send an unphased progress message before
                        # an explicitly phased final answer, so the final phase
                        # must also take precedence over those legacy messages.
                        items = [item for item in response["output"]
                                 if item.get("type") != "reasoning"
                                 and item.get("phase") != "commentary"]
                        final_items = [item for item in items if item.get("phase") == "final_answer"]
                        completed["output"] = final_items or items
                        # A gateway's convenience output_text can concatenate
                        # progress and final messages. The completed structured
                        # output is authoritative whenever it is present.
                        completed.pop("output_text", None)
                    try:
                        text = extract_output_text(completed)
                    except InvalidJsonResponseError:
                        raise LLMRequestError("Responses API 流式响应缺少最终文本") from None
                    if not text.strip():
                        raise LLMRequestError("Responses API 流式响应返回空文本")
            elif kind in {"response.output_item.added", "response.output_item.done"}:
                item = chunk.get("item")
                if not isinstance(item, Mapping):
                    raise LLMRequestError("Responses API 流式响应缺少输出对象")
                validate_output(item, final=kind.endswith(".done"))
            elif kind in {"response.content_part.added", "response.content_part.done"}:
                part = chunk.get("part")
                validate_output({"content": [part]}, final=kind.endswith(".done"))
            elif kind in {"response.output_text.delta", "response.output_text.done"}:
                if not isinstance(chunk.get("delta" if kind.endswith(".delta") else "text"), str):
                    raise LLMRequestError("Responses API 流式响应文本格式无效")
            elif kind == "response.metadata":
                # Observed compatible-provider advisory event. Its payload is
                # never output or a terminal signal; a separate fully validated
                # response.completed envelope is still mandatory. Do not allow
                # error/incomplete markers to hide in this extension event.
                if (chunk.get("status") not in (None, "completed")
                        or chunk.get("incomplete_details") is not None
                        or chunk.get("refusal") or chunk.get("tool_calls") or chunk.get("function_call")):
                    raise LLMRequestError("Responses API 流式响应包含错误或未完成的输出")
            elif kind not in {
                "response.output_text.annotation.added",
                "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
                "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
                "response.reasoning_text.delta", "response.reasoning_text.done",
            }:
                # Refusals, filtering and tool output are never text-review success.
                raise LLMRequestError("Responses API 流式响应未返回可用的复核文本")

        # SSE line endings are CR/LF only. str.splitlines() would split valid
        # JSON strings containing U+0085, U+2028 or U+2029 as well.
        for line in source.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if not line:
                consume()
                data = []
                event = ""
            elif line.startswith(":"):
                continue
            elif line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith(("id:", "retry:")):
                continue
            else:
                raise LLMRequestError("Responses API 流式响应包含无效事件")
        consume()
        if completed is None:
            raise LLMRequestError("Responses API 流式响应中断或缺少完整完成事件")
        return completed

    @staticmethod
    def _http_error(status: int, operation: str = "Responses API") -> LLMRequestError:
        # Provider bodies, reason phrases and headers can contain credentials
        # or request material.  Report only our own status-specific guidance.
        if 300 <= status < 400:
            return LLMHTTPError(
                f"{operation} HTTP {status}：不允许 API 重定向；请检查服务地址；不会自动重试",
                status_code=status,
            )
        if status == 429:
            return LLMRateLimitError(f"{operation} HTTP 429：请求被限流", status_code=status)
        if 500 <= status <= 599:
            return LLMServerError(f"{operation} HTTP {status}：服务端错误", status_code=status)
        if status == 403:
            return LLMHTTPError(
                f"{operation} HTTP 403：服务方拒绝访问，需要服务方授权；不会自动重试",
                status_code=status,
            )
        return LLMHTTPError(f"{operation} HTTP {status}：请求被拒绝；不会自动重试", status_code=status)

    def _send(self, request: urllib.request.Request, *, operation: str,
              max_retries: int | None = None) -> dict[str, Any]:
        if self.config.requires_auth and not self.config.api_key:
            raise ConfigurationError("当前模型服务要求 API Key，但项目凭据未提供")
        retries = self.config.max_retries if max_retries is None else max(0, int(max_retries))
        last_error: LLMRequestError | None = None
        for attempt in range(retries + 1):
            try:
                response = self._transport(request, self.config.timeout)
                try:
                    status = int(
                        getattr(
                            response,
                            "status",
                            getattr(response, "status_code", getattr(response, "code", 200)),
                        )
                    )
                    if 300 <= status < 400:
                        # Custom transports must not promote a redirect's body
                        # into model output either. Close it without reading.
                        raise self._http_error(status, operation)
                    raw = self._read_response(response)
                    headers = getattr(response, "headers", None)
                    content_type = (headers.get("Content-Type") or headers.get("content-type") or ""
                                    if hasattr(headers, "get") else "")
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
                if status >= 400:
                    raise self._http_error(status, operation)
                if operation != "Models API" and (
                    "text/event-stream" in str(content_type).lower()
                    or raw.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(
                        (b"data:", b"event:", b":", b"id:", b"retry:")
                    )
                ):
                    if self.config.api_mode == API_MODE_CHAT_COMPLETIONS:
                        return self._decode_chat_event_stream(raw)
                    return self._decode_responses_event_stream(raw)
                try:
                    decoded = json.loads(raw.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise LLMRequestError(f"{operation} 返回了非 JSON 数据", status_code=status) from exc
                if not isinstance(decoded, dict):
                    raise LLMRequestError(f"{operation} 返回体不是对象", status_code=status)
                return decoded
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                exc.close()
                last_error = self._http_error(status, operation)
                if not isinstance(last_error, (LLMRateLimitError, LLMServerError)):
                    # Suppress the original HTTPError traceback, whose URL or
                    # reason phrase need not be safe for logs/checkpoints.
                    raise last_error from None
            except (TimeoutError, urllib.error.URLError) as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                    last_error = LLMTimeoutError(f"{operation} 请求超时")
                else:
                    last_error = LLMRequestError(f"{operation} 网络请求失败")
            except (http.client.IncompleteRead, ConnectionError):
                raise LLMRequestError(f"{operation} 响应连接中断") from None
            except LLMRequestError as exc:
                if not isinstance(exc, (LLMRateLimitError, LLMServerError, LLMTimeoutError)):
                    raise
                last_error = exc
            if attempt < retries:
                time.sleep(self.config.backoff_factor * (2**attempt))
        assert last_error is not None
        raise last_error

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            # Identify the actual caller instead of urllib's default identity.
            # This is not a browser/Codex impersonation or a challenge bypass.
            "User-Agent": APPLICATION_USER_AGENT,
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _request(self, body: Mapping[str, Any], *, max_retries: int | None = None) -> dict[str, Any]:
        if self.config.api_mode == API_MODE_RESPONSES and self.config.chat_stream:
            # All review/correction/probe paths share this entry point. Preserve
            # the legacy non-streaming envelope and the caller's original body.
            body = {**body, "stream": True}
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = self._headers(json_body=True)
        if body.get("stream") is True:
            headers["Accept"] = "text/event-stream"
        request = urllib.request.Request(
            self.config.endpoint, data=encoded, headers=headers, method="POST"
        )
        operation = ("Chat Completions API" if self.config.api_mode == API_MODE_CHAT_COMPLETIONS
                     else "Responses API")
        return self._send(request, operation=operation, max_retries=max_retries)

    def list_models(self) -> list[str]:
        """Return model IDs exposed by the configured OpenAI-compatible service."""
        request = urllib.request.Request(self.config._api_root + "/models", headers=self._headers(), method="GET")
        response = self._send(request, operation="Models API")
        data = response.get("data")
        if not isinstance(data, list):
            raise LLMRequestError("Models API 返回格式不受支持；请手动填写模型名称")
        models: list[str] = []
        seen: set[str] = set()
        for item in data:
            if not isinstance(item, Mapping):
                continue
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip() and model_id.strip() not in seen:
                seen.add(model_id.strip())
                models.append(model_id.strip())
        return models

    def test_connection(self, *, model: str | None = None,
                        reasoning_effort: str | None = None) -> bool:
        """Send one tiny request; never retry to avoid duplicate usage."""
        selected_model = str(self.config.model if model is None else model).strip()
        if not selected_model:
            raise ConfigurationError("请先填写模型名称")
        selected_effort = self.config.reasoning_effort if reasoning_effort is None else reasoning_effort
        probe_config = replace(self.config, model=selected_model, reasoning_effort=selected_effort)
        effective_effort = probe_config.effective_reasoning_effort
        if self.config.api_mode == API_MODE_CHAT_COMPLETIONS:
            body: dict[str, Any] = {
                "model": selected_model,
                "messages": [{"role": "user", "content": "Reply with OK only."}],
                "max_tokens": 1024 if effective_effort else 16,
                "stream": self.config.chat_stream,
            }
            if effective_effort:
                body["reasoning_effort"] = effective_effort
        else:
            body = {
                "model": selected_model,
                "input": "Reply with OK only.",
                # NikoAPI and other OpenAI-compatible reasoning providers commonly
                # enforce a minimum of 16 output tokens.  Keep the probe tiny while
                # satisfying that protocol constraint.
                "max_output_tokens": 16,
                "store": False,
            }
            if effective_effort:
                body["reasoning"] = {"effort": effective_effort}
        response = self._request(body, max_retries=0)
        if self.config.api_mode == API_MODE_CHAT_COMPLETIONS:
            return self._validate_chat_probe_response(response)
        response_status = response.get("status")
        if response.get("error") is not None or (
            response_status is not None and response_status != "completed"
        ):
            raise LLMRequestError("Responses API 已连接，但模型未能完成测试请求")
        try:
            output = extract_output_text(response)
        except InvalidJsonResponseError:
            raise LLMRequestError("Responses API 已连接，但模型未能完成测试请求") from None
        if not output.strip():
            raise LLMRequestError("Responses API 返回空文本，无法确认模型已完成测试请求")
        return True

    @staticmethod
    def _validate_chat_probe_response(response: Mapping[str, Any], *, require_stop: bool = False) -> bool:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMRequestError("Chat Completions API 已连接，但模型未返回 choices")
        first = _object_to_mapping(choices[0])
        message = _object_to_mapping(first.get("message")) if isinstance(first, Mapping) else None
        if (not isinstance(first, Mapping) or first.get("error") is not None
                or not isinstance(message, Mapping) or message.get("error") is not None
                or require_stop and (first.get("finish_reason") != "stop"
                                    or message.get("role") not in (None, "assistant"))):
            raise LLMRequestError("Chat Completions API 已连接，但模型未能完成测试请求")
        try:
            output = extract_output_text(response)
        except InvalidJsonResponseError:
            raise LLMRequestError("Chat Completions API 已连接，但模型未能完成测试请求") from None
        if not output.strip():
            raise LLMRequestError("Chat Completions API 返回空文本，无法确认模型已完成测试请求")
        return True

    def test_chat_reasoning_effort(self, *, model: str | None = None,
                                   reasoning_effort: str | None = None) -> bool:
        """Probe a proposed top-level Chat effort once, without prior proof.

        Success confirms parameter acceptance and a complete text response. It
        cannot prove how the provider applies reasoning internally, and does
        not save or change the configuration's verification marker.
        """
        if self.config.api_mode != API_MODE_CHAT_COMPLETIONS:
            raise ConfigurationError("验证 Chat 思考强度需要 Chat Completions 模式")
        selected_model = str(self.config.model if model is None else model).strip()
        if not selected_model:
            raise ConfigurationError("请先填写模型名称")
        selected_effort = self.config.reasoning_effort if reasoning_effort is None else reasoning_effort
        if not isinstance(selected_effort, str) or selected_effort not in project_llm_settings.REASONING_EFFORTS:
            raise ConfigurationError("Chat 思考强度无效")
        body = {
            "model": selected_model,
            "messages": [{"role": "user", "content": "Reply with OK only."}],
            "reasoning_effort": selected_effort,
            "max_tokens": 1024,
            "stream": self.config.chat_stream,
        }
        response = self._request(body, max_retries=0)
        return self._validate_chat_probe_response(response, require_stop=True)

    def review_job(
        self,
        job: Mapping[str, Any] | str,
        *,
        prompt: str | None = None,
        prompt_path: str | os.PathLike[str] | None = None,
        response_schema: Mapping[str, Any] | None = None,
        conversation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Review one job and return the validated structured result."""

        instructions = prompt if prompt is not None else load_review_prompt(prompt_path or self.prompt_path)
        schema = response_schema if response_schema is not None else self.response_schema
        initial_messages = review_request_messages(job, instructions)
        job_text = json.dumps(job, ensure_ascii=False, sort_keys=True) if isinstance(job, Mapping) else str(job)
        expected_job_id = None
        if isinstance(job, Mapping):
            raw_expected_job_id = job.get("job_id")
            if isinstance(raw_expected_job_id, str) and raw_expected_job_id.strip():
                expected_job_id = raw_expected_job_id
        followup = {
            "role": "user",
            "content": (
                "This is a new, independent job. Ignore all previous job descriptions and conclusions in this "
                "conversation. Apply the original system review instructions, and evaluate only this job. "
                "Return one JSON object using only the original output fields.\nCurrent job data:\n" + job_text
            ),
        }

        def bounded_text_history(items: Sequence[Any]) -> list[dict[str, Any]]:
            history = [
                {"role": item["role"], "content": item["content"]}
                for item in items
                if (isinstance(item, Mapping) and isinstance(item.get("role"), str)
                    and item["role"] in {"system", "user", "assistant"}
                    and isinstance(item.get("content"), str))
            ]
            limit = self.MAX_CONVERSATION_MESSAGES
            if len(history) <= limit:
                return history
            system_index = next(
                (index for index, item in enumerate(history) if item.get("role") == "system"),
                None,
            )
            if system_index is None:
                return history[-limit:]
            system_message = history[system_index]
            tail = [item for index, item in enumerate(history) if index != system_index]
            return [system_message, *tail[-(limit - 1):]]

        # Local transcripts contain only committed job turns. For Responses,
        # this history also allows a caller to retry a failed provider-managed
        # continuation without losing previously validated jobs.
        responses_history_only = (conversation is not None
                                  and self.config.api_mode == API_MODE_RESPONSES
                                  and conversation.get("responses_history_only") is True)
        local_messages = initial_messages
        if conversation is not None:
            saved_history = conversation.get("messages")
            if isinstance(saved_history, list) and saved_history:
                history = bounded_text_history(saved_history)
                if any(item.get("role") in {"user", "assistant"} for item in history):
                    local_messages = [initial_messages[0], *[
                        item for item in history if item.get("role") != "system"
                    ], followup]

        messages = initial_messages
        active_response_id = None
        reused = False
        if conversation is not None and self.config.api_mode == API_MODE_RESPONSES:
            saved_id = conversation.get("previous_response_id")
            if responses_history_only:
                messages = bounded_text_history(local_messages)
                reused = len(local_messages) > len(initial_messages)
            elif isinstance(saved_id, str) and saved_id.strip():
                active_response_id = saved_id.strip()
                reused = True
                messages = [followup]
        elif conversation is not None and self.config.api_mode == API_MODE_CHAT_COMPLETIONS:
            history = conversation.get("messages")
            if isinstance(history, list) and history:
                bounded_history = bounded_text_history(history)
                reused = any(item.get("role") in {"user", "assistant"} for item in bounded_history)
                messages = [initial_messages[0], *[
                    item for item in bounded_history if item.get("role") != "system"
                ], followup]

        request_count = 0
        provider_response_id_missing = False
        for attempt in range(REVIEW_FORMAT_REPAIR_ATTEMPTS + 1):
            if self.config.api_mode == API_MODE_CHAT_COMPLETIONS:
                request_messages = bounded_text_history(messages) if conversation is not None else messages
                # Responses-only fields do not belong in the Chat envelope. Send
                # an explicit effort only after the exact Chat options were tested.
                body: dict[str, Any] = {
                    "model": self.config.model,
                    "messages": request_messages,
                    "stream": self.config.chat_stream,
                }
                if self.config.effective_reasoning_effort:
                    body["reasoning_effort"] = self.config.effective_reasoning_effort
            else:
                request_messages = bounded_text_history(messages) if responses_history_only else messages
                body = {
                    "model": self.config.model,
                    "input": request_messages,
                    "store": (False if responses_history_only else True if conversation is not None
                              else not self.config.disable_response_storage),
                }
                if active_response_id:
                    body["previous_response_id"] = active_response_id
                if self.config.effective_reasoning_effort:
                    body["reasoning"] = {"effort": self.config.effective_reasoning_effort}
            # Preserve the configured provider's existing text-JSON envelope.
            # A two-field review does not require new Structured Outputs API
            # capabilities; the prompt and local schema enforce its contract.
            request_count += 1
            try:
                response = self._request(body)
            except LLMClientError as exc:
                # Keep the transport error's exact type/status/message. A
                # correction is not a second transport retry policy.
                setattr(exc, "format_repair_attempts", attempt)
                raise
            try:
                result = parse_json_response(response)
                if not isinstance(result, dict):
                    raise JsonSchemaValidationError("$: 评估结果必须是 JSON 对象")
                if (expected_job_id is not None and "job_id" in result
                        and result["job_id"] not in (None, "", expected_job_id)):
                    raise JsonSchemaValidationError("$.job_id: 岗位 ID 与当前任务不匹配")
                repair_fields: list[str] = []
                if schema is LEGACY_REVIEW_RESPONSE_SCHEMA:
                    # First enforce the exact wire contract. This deliberately
                    # sends non-canonical values (e.g. "是" or "yes") through
                    # the correction prompt instead of silently normalizing
                    # them before the model gets a chance to comply.
                    if attempt == 0:
                        validate_json_schema(result, schema)
                    result, repair_fields = normalize_review_result(
                        result, expected_job_id=expected_job_id
                    )
                if schema is not None:
                    validate_json_schema(result, schema)
                if schema in (REVIEW_RESPONSE_SCHEMA, LEGACY_REVIEW_RESPONSE_SCHEMA):
                    # Only the selected semantic wire contract belongs in a review.
                    # Additional provider/model fields are not raw provenance.
                    result = {key: result[key] for key in schema["properties"] if key in result}
                # Prevent model-controlled values from impersonating client
                # metadata or returning an upstream response ID.
                for field_name in (
                    "_response_id", "response_id", "previous_response_id", "_conversation", "conversation",
                    "messages", "history", "_messages", "_history", "raw_response", "raw_output",
                    "_raw_response", "_raw_output", "_format_repair_attempts", "_schema_repair_fields",
                ):
                    result.pop(field_name, None)
                if conversation is not None:
                    # Both protocols retain canonical, validated assistant
                    # JSON only: no raw text, reasoning, correction prompts,
                    # provider IDs, or arbitrary extra review fields.
                    def semantic_history_value(value: Any, value_schema: Mapping[str, Any] | None) -> Any:
                        if isinstance(value, Mapping):
                            properties = value_schema.get("properties") if isinstance(value_schema, Mapping) else None
                            return {
                                key: semantic_history_value(item, properties.get(key) if isinstance(properties, Mapping) else None)
                                for key, item in value.items()
                                if not isinstance(properties, Mapping) or key in properties
                            }
                        if isinstance(value, list):
                            item_schema = value_schema.get("items") if isinstance(value_schema, Mapping) else None
                            return [semantic_history_value(item, item_schema) for item in value]
                        return value

                    assistant_text = json.dumps(semantic_history_value(result, schema), ensure_ascii=False, sort_keys=True)
                    committed_history = bounded_text_history(local_messages + [
                        {"role": "assistant", "content": assistant_text}
                    ])
                    if self.config.api_mode == API_MODE_RESPONSES:
                        response_id = response.get("id")
                        provider_response_id_missing = not (isinstance(response_id, str) and response_id.strip())
                        if not responses_history_only and not provider_response_id_missing:
                            conversation["previous_response_id"] = response_id.strip()
                        else:
                            # Never preserve an unusable/stale ID. A valid
                            # missing-ID turn still continues locally next time.
                            conversation.pop("previous_response_id", None)
                            conversation["responses_history_only"] = True
                    conversation["messages"] = committed_history
                if attempt or repair_fields:
                    # Internal diagnostic metadata; the schema is validated
                    # before it is attached and the pipeline removes this
                    # field from the semantic payload.
                    result["_format_repair_attempts"] = attempt
                    result["_schema_repair_fields"] = repair_fields
                if conversation is not None:
                    result["_conversation"] = {
                        "reused": bool(reused),
                        "request_count": int(request_count),
                        "format_repair_attempts": int(attempt),
                        "provider_response_id_missing": bool(provider_response_id_missing),
                        "client_history": bool(responses_history_only or self.config.api_mode == API_MODE_CHAT_COMPLETIONS),
                    }
                return result
            except (InvalidJsonResponseError, JsonSchemaValidationError) as exc:
                if attempt >= REVIEW_FORMAT_REPAIR_ATTEMPTS:
                    setattr(exc, "format_repair_attempts", attempt)
                    raise
                # Do not echo the failed response into another request: it can
                # contain arbitrary model text. Ask for a fresh result against
                # the original JD and point only to the local schema failure.
                detail = (f"本地结构校验提示：{exc}" if isinstance(exc, JsonSchemaValidationError)
                          else "上次响应不是合法 JSON。")
                if isinstance(exc, JsonSchemaValidationError) and ".present:" in str(exc):
                    detail += ("present 字段只能使用 JSON 布尔值 true/false，或 JSON 字符串 \"unknown\"；"
                               "不要输出 是/否、yes/no、有/无、命中/未命中等其他值。")
                correction = (
                    "The previous answer for the current job did not pass local validation. "
                    "Return a corrected complete JSON object for the same current job only. "
                    "Follow every field, type and enum in the original review instructions; "
                    "do not add Markdown or explanations. " + detail
                )
                if self.config.api_mode == API_MODE_RESPONSES:
                    response_id = response.get("id")
                    can_chain_response = (
                        isinstance(response_id, str)
                        and bool(response_id.strip())
                        and body.get("store") is True
                    )
                    if can_chain_response:
                        active_response_id = response_id.strip()
                        messages = [{"role": "user", "content": correction}]
                    elif responses_history_only:
                        # These requests use store=false even if a gateway
                        # happens to return an ID. Replay safe history instead.
                        messages = local_messages + [{"role": "user", "content": correction}]
                    elif conversation is not None and active_response_id:
                        # The malformed turn cannot be chained. Restart from
                        # the last committed response, restating this job.
                        messages = [followup, {"role": "user", "content": correction}]
                    else:
                        # With store=false this is a fresh request, not a
                        # previous_response_id chain to an unretained reply.
                        messages = list(initial_messages)
                        messages[0] = {
                            **messages[0],
                            "content": messages[0]["content"] + "\n\n" + correction,
                        }
                elif self.config.api_mode == API_MODE_CHAT_COMPLETIONS:
                    # Do not replay arbitrary invalid assistant output (or
                    # fail while extracting an already failed envelope).
                    # Restating this job plus a safe local diagnostic is enough.
                    messages = request_messages + [{"role": "user", "content": correction}]


__all__ = [
    "APPLICATION_USER_AGENT",
    "API_MODE_CHAT_COMPLETIONS",
    "API_MODE_RESPONSES",
    "API_MODES",
    "CodexConfig",
    "CodexResponsesClient",
    "ConfigurationError",
    "InvalidJsonResponseError",
    "JsonSchemaValidationError",
    "LLMClientError",
    "LLMHTTPError",
    "LLMRateLimitError",
    "LLMRequestError",
    "LLMServerError",
    "LLMTimeoutError",
    "REVIEW_RESPONSE_SCHEMA",
    "LEGACY_REVIEW_RESPONSE_SCHEMA",
    "SIMPLE_REVIEW_FORMAT",
    "extract_output_text",
    "load_codex_config",
    "load_review_prompt",
    "parse_json_response",
    "review_request_messages",
    "simple_review_payload",
    "validate_json_schema",
]
