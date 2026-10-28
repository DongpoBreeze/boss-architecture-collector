"""Project-scoped OpenAI-compatible provider settings and secret storage.

The URL/model/effort live in a small per-user config file. On Windows the API
key is kept in Windows Credential Manager, not in source, Codex global config,
the workspace preferences, process arguments, or review artifacts.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit


DEFAULT_BASE_URL = "https://nikoapi.xyz/v1"
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "max"
DEFAULT_PROVIDER = "nikoapi"
CREDENTIAL_TARGET = "BossArchitectureCollector/NikoAPI"
SETTINGS_VERSION = 1

# The project can use either of the two OpenAI-compatible request envelopes.
# Keep the stored values stable and machine-readable; labels belong to the UI.
API_MODE_RESPONSES = "responses"
API_MODE_CHAT_COMPLETIONS = "chat_completions"
API_MODES = (API_MODE_RESPONSES, API_MODE_CHAT_COMPLETIONS)
API_MODE_LABELS = {
    API_MODE_RESPONSES: "Responses API",
    API_MODE_CHAT_COMPLETIONS: "Chat Completions",
}
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_VERIFIED_SIGNATURE_RE = re.compile(r"[0-9a-f]{64}")


class ProjectProviderError(RuntimeError):
    """A safe-to-display project provider configuration error."""


def chat_reasoning_signature(base_url: str, model: str, reasoning_effort: str, *,
                             chat_stream: bool = False) -> str:
    """Bind a successful Chat probe to its endpoint, model and wire options.

    API keys are deliberately absent. Endpoint suffixes identify the same API
    root; model and effort values otherwise retain their exact identity.
    """
    if not all(isinstance(item, str) and item.strip()
               for item in (base_url, model, reasoning_effort)) or not isinstance(chat_stream, bool):
        raise ProjectProviderError("Chat 思考强度验证配置无效")
    base = base_url.strip().rstrip("/")
    for suffix in ("/responses", "/chat/completions"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    if not base:
        raise ProjectProviderError("Chat 思考强度验证配置无效")
    identity = {"base_url": base, "model": model, "reasoning_effort": reasoning_effort,
                "chat_stream": chat_stream}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def is_chat_reasoning_verified(base_url: str, model: str, reasoning_effort: str,
                               verified_for: str, *, chat_stream: bool = False) -> bool:
    """Return False for missing, malformed or stale verification markers."""
    if not isinstance(verified_for, str) or not _VERIFIED_SIGNATURE_RE.fullmatch(verified_for):
        return False
    try:
        expected = chat_reasoning_signature(base_url, model, reasoning_effort, chat_stream=chat_stream)
    except ProjectProviderError:
        return False
    return verified_for == expected


def default_settings_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local and Path(local).is_absolute():
        return Path(local) / "BossArchitectureCollector" / "llm-provider.json"
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Local" / "BossArchitectureCollector" / "llm-provider.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / "boss-architecture-collector" / "llm-provider.json"


def _validated_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != SETTINGS_VERSION:
        raise ProjectProviderError("项目模型配置格式无效")
    base_url = value.get("base_url")
    model = value.get("model")
    effort = value.get("reasoning_effort")
    provider = value.get("provider", DEFAULT_PROVIDER)
    target = value.get("credential_target", CREDENTIAL_TARGET)
    api_mode = value.get("api_mode", API_MODE_RESPONSES)
    verified_for = value.get("chat_reasoning_verified_for", "")
    chat_stream = value.get("chat_stream", False)
    if not all(isinstance(item, str) and item.strip() for item in (base_url, model, effort, provider, target, api_mode)):
        raise ProjectProviderError("项目模型配置缺少必填项")
    parsed = urlsplit(base_url.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProjectProviderError("项目服务地址须为不含凭据的 HTTPS Base URL")
    if effort.strip() not in REASONING_EFFORTS:
        raise ProjectProviderError("项目模型思考强度无效")
    if api_mode.strip() not in API_MODES:
        raise ProjectProviderError("项目模型协议模式无效")
    if not isinstance(verified_for, str) or (verified_for and not _VERIFIED_SIGNATURE_RE.fullmatch(verified_for)):
        raise ProjectProviderError("Chat 思考强度验证记录无效")
    if not isinstance(chat_stream, bool):
        raise ProjectProviderError("Chat 流式响应设置无效")
    return {
        "base_url": base_url.strip().rstrip("/"),
        "model": model.strip(),
        "reasoning_effort": effort.strip(),
        "api_mode": api_mode.strip(),
        "chat_reasoning_verified_for": verified_for,
        "chat_stream": chat_stream,
        "provider": provider.strip(),
        "credential_target": target.strip(),
    }


def load_settings(path: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    target = Path(path).expanduser() if path is not None else default_settings_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectProviderError("无法读取本机项目模型配置") from exc
    return _validated_settings(value)


def save_settings(
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    *,
    path: str | os.PathLike[str] | None = None,
    credential_target: str = CREDENTIAL_TARGET,
    api_mode: str = API_MODE_RESPONSES,
    chat_reasoning_verified_for: str = "",
    chat_stream: bool = False,
) -> Path:
    target = Path(path).expanduser() if path is not None else default_settings_path()
    value = _validated_settings({
        "version": SETTINGS_VERSION,
        "base_url": base_url,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "api_mode": api_mode,
        "chat_reasoning_verified_for": chat_reasoning_verified_for,
        "chat_stream": chat_stream,
        "provider": DEFAULT_PROVIDER,
        "credential_target": credential_target,
    })
    document = {"version": SETTINGS_VERSION, **value}
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, filename = tempfile.mkstemp(prefix=".llm-provider-", suffix=".tmp", dir=target.parent)
        temporary = Path(filename)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.c_void_p),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


def _credential_api():
    if sys.platform != "win32":
        raise ProjectProviderError("项目密钥存储目前需要 Windows Credential Manager")
    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), ctypes.c_uint32]
    advapi.CredWriteW.restype = ctypes.c_int
    advapi.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
    advapi.CredReadW.restype = ctypes.c_int
    advapi.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    advapi.CredDeleteW.restype = ctypes.c_int
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None
    return advapi


def store_api_key(secret: str, target: str = CREDENTIAL_TARGET) -> None:
    if not isinstance(secret, str) or not secret.strip() or "\x00" in secret:
        raise ProjectProviderError("项目 API Key 不能为空或包含无效字符")
    advapi = _credential_api()
    blob = secret.strip().encode("utf-16-le")
    buffer = ctypes.create_string_buffer(blob)
    credential = _CREDENTIALW()
    credential.Type = 1  # CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.c_void_p)
    credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE, scoped to this user
    credential.UserName = "project API key"
    if not advapi.CredWriteW(ctypes.byref(credential), 0):
        raise ProjectProviderError("无法将项目 API Key 保存到 Windows Credential Manager")


def load_api_key(target: str = CREDENTIAL_TARGET) -> str | None:
    if sys.platform != "win32":
        secret = os.environ.get("BOSS_ARCHITECTURE_COLLECTOR_API_KEY")
        return secret.strip() if secret and secret.strip() else None
    advapi = _credential_api()
    pointer = ctypes.POINTER(_CREDENTIALW)()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND
            return None
        raise ProjectProviderError("无法从 Windows Credential Manager 读取项目 API Key")
    try:
        credential = pointer.contents
        size = int(credential.CredentialBlobSize)
        if size <= 0 or size > 5 * 1024:
            raise ProjectProviderError("Windows Credential Manager 中的项目凭据长度无效")
        secret = ctypes.string_at(credential.CredentialBlob, size).decode("utf-16-le")
        return secret or None
    except (UnicodeError, ValueError) as exc:
        raise ProjectProviderError("Windows Credential Manager 中的项目凭据无效") from exc
    finally:
        advapi.CredFree(pointer)


def delete_api_key(target: str = CREDENTIAL_TARGET) -> None:
    advapi = _credential_api()
    if not advapi.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
        raise ProjectProviderError("无法从 Windows Credential Manager 删除项目 API Key")
