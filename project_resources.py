"""Locate validated, read-only defaults without putting mutable state beside them.

Source launches use the existing config/prompts files.  Normal wheel installs
use only the generated resource package, with no cwd or source-tree fallback.
This module calculates paths; it never creates directories or reads credentials.
"""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import sys

import boss_architecture_resources as _resources


class ResourceError(RuntimeError):
    """A required release resource is missing, corrupt, or from a bad build."""


PROJECT_ROOT = Path(__file__).resolve().parent
_RESOURCE_ROOT = Path(_resources.__file__).resolve().parent
# _build.py is source-only; an installed wheel cannot accidentally become a
# checkout just because an unrelated pyproject.toml exists in site-packages.
IS_SOURCE_CHECKOUT = (
    (PROJECT_ROOT / "pyproject.toml").is_file()
    and (_RESOURCE_ROOT / "_build.py").is_file()
)


def _release_manifest() -> dict:
    path = _RESOURCE_ROOT / _resources.DATA_DIRECTORY / _resources.MANIFEST_FILENAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ResourceError(f"Packaged resource manifest is missing or invalid: {path}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != _resources.MANIFEST_SCHEMA_VERSION
        or manifest.get("project") != _resources.PROJECT_NAME
        or not isinstance(manifest.get("version"), str)
        or not manifest["version"]
        or not isinstance(manifest.get("resources"), dict)
        or set(manifest["resources"]) != set(_resources.RESOURCE_SOURCES)
        or any(
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in manifest["resources"].values()
        )
    ):
        raise ResourceError(f"Packaged resource manifest has an invalid schema: {path}")
    try:
        installed_version = version(_resources.PROJECT_NAME)
    except PackageNotFoundError as exc:
        raise ResourceError("Packaged distribution metadata is missing; reinstall the application") from exc
    if manifest["version"] != installed_version:
        raise ResourceError(
            f"Packaged resource version mismatch: {manifest['version']} != {installed_version}"
        )
    return manifest


def resource_path(name: str) -> Path:
    """Return an allowlisted resource Path, checking its release hash if installed."""
    if name not in _resources.RESOURCE_SOURCES:
        raise ResourceError(f"Unknown release resource: {name}")
    if IS_SOURCE_CHECKOUT:
        path = PROJECT_ROOT / _resources.RESOURCE_SOURCES[name]
        if not path.is_file():
            raise ResourceError(f"Source resource is missing: {path}")
        return path
    manifest = _release_manifest()
    path = _RESOURCE_ROOT / _resources.DATA_DIRECTORY / name
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ResourceError(f"Packaged resource is missing or unreadable: {path}") from exc
    if hashlib.sha256(payload).hexdigest() != manifest["resources"][name]:
        raise ResourceError(f"Packaged resource checksum mismatch (modified or corrupt): {path}")
    return path


def default_state_root() -> Path:
    """Calculate the writable-state base; do not create it or any user settings."""
    if IS_SOURCE_CHECKOUT:
        return PROJECT_ROOT
    if sys.platform == "win32":
        configured = os.environ.get("LOCALAPPDATA")
        base = Path(configured).expanduser() if configured else None
        if base is not None and base.is_absolute():
            return base / _resources.PROJECT_NAME
        return Path.home() / ".boss-architecture-collector"
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else None
    if base is None or not base.is_absolute():
        base = Path.home() / ".local" / "state"
    return base / _resources.PROJECT_NAME


DEFAULT_CONFIG = resource_path("architecture_collection.json")
DEFAULT_PROMPT = resource_path("jd_semantic_review.md")
