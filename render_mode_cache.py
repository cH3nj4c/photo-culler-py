"""Persistent cache for the render mode (the GPU acceleration scheme).

The render mode is the one choice that has to outlive a session: whatever the
user picks under "GPU 加速…" must be the mode the *next* launch starts with, so
it is stored on disk instead of being re-derived from the hardware each run.

It lives in a tiny JSON file of its own — ``%LOCALAPPDATA%\\PhotoCuller\\render_mode.json``
— for the same reason settings and selections are kept apart: a corrupt or
missing settings file must never cost the user their render mode, and this
file breaking must not take unrelated settings down with it. ``temp_cleanup``
treats it as user data, so closing the app does not delete it.

Guarantees:

- Reads are total. A missing, unreadable or malformed file yields the caller's
  default, because this runs during startup and must never block it.
- Writes report failure as a string rather than raising, matching
  ``save_selection`` and ``app_settings.save_settings``.
- Writes are atomic (temp file + replace), so an interrupted write cannot
  leave a half-written file behind.
- ``app_settings.accel_scheme`` is kept as a compatibility copy: older builds
  know only that key, so it is written on every save and read as a fallback
  when this cache holds nothing usable. The cache file wins whenever it does.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import app_settings

APP_DIR_NAME = "PhotoCuller"
CACHE_NAME = "render_mode.json"
# Key of the compatibility copy in app_settings.settings.json.
LEGACY_KEY = "accel_scheme"
DEFAULT_MODE = "auto"


def cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else (Path.home() / "AppData" / "Local")
    return root / APP_DIR_NAME


def cache_file() -> Path:
    return cache_root() / CACHE_NAME


def _read_object(path: Path) -> dict[str, Any] | None:
    """Parsed JSON object at ``path``, or None when absent/malformed/not one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _legacy_mode() -> str | None:
    """The compatibility copy as actually stored, or None when not stored.

    Read raw rather than through ``app_settings.get_value``: that merges the
    default back in, so "the user never chose" would come back looking like a
    real choice and the caller's own default would never be reached.
    """
    data = _read_object(app_settings.settings_file())
    if data is None:
        return None
    mode = data.get(LEGACY_KEY)
    return mode if isinstance(mode, str) and mode else None


def load_mode(default: str = DEFAULT_MODE) -> str:
    """The render mode stored for the next launch, or ``default`` if none.

    Falls back to the compatibility copy in settings, so a choice made before
    this cache existed (or on a build older than it) is not thrown away.
    """
    data = _read_object(cache_file())
    if data is not None:
        mode = data.get("mode")
        if isinstance(mode, str) and mode:
            return mode
    return _legacy_mode() or default


def save_mode(mode: str) -> str | None:
    """Persist ``mode`` so the next launch runs it. Error message or None."""
    if not isinstance(mode, str) or not mode.strip():
        return f"无效的渲染模式：{mode!r}"
    payload = {
        "mode": mode,
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    path = cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        return f"渲染模式保存失败：{exc}"
    # Best-effort copy for older builds; the cache file above is authoritative,
    # so a failure here must not make a successful save look like a failure.
    app_settings.save_settings({LEGACY_KEY: mode})
    return None
