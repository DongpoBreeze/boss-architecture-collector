"""Small, shared, whitelisted desktop preferences; no legacy UI imports."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

from desktop_theme import normalize_theme


PREFERENCE_FIELDS = (
    "data_root", "scraper_root", "city", "track", "stage", "combination_limit",
    "pages", "sample_size", "workers", "jd_workers", "batch_size", "theme",
    "browser_mode", "chrome_port", "edge_port", "chrome_profile", "edge_profile",
)


def preference_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config")
    return base / "BossArchitectureCollector" / "desktop-settings.json"


def load_preferences(path: Path | None = None) -> dict:
    try:
        value = json.loads((path or preference_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    safe = {key: value[key] for key in PREFERENCE_FIELDS if isinstance(value.get(key), str)}
    if "theme" in safe:
        safe["theme"] = normalize_theme(safe["theme"])
    return safe


def save_preferences(values: dict, path: Path | None = None) -> None:
    """Atomically save only UI values; concurrent writers never share a temp."""
    target = path or preference_path()
    safe = {key: str(values[key]) for key in PREFERENCE_FIELDS if key in values}
    if "theme" in safe:
        safe["theme"] = normalize_theme(safe["theme"])
    data = json.dumps(safe, ensure_ascii=False, indent=2)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, filename = tempfile.mkstemp(prefix=".desktop-settings-", suffix=".tmp", dir=target.parent)
        temporary = Path(filename)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
        for attempt in range(5):
            try:
                os.replace(temporary, target)
                break
            except OSError as error:
                # Concurrent Windows replace/read handles can briefly deny a
                # rename. Retry only those sharing/access errors, bounded to
                # 50 ms total; other failures retain the previous complete file.
                if getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 4:
                    raise
                time.sleep(.005 * (attempt + 1))
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
