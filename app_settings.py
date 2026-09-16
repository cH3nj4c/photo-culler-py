"""Small persisted settings store (one JSON file for the whole app).

Separate from ``selection_store`` on purpose: selections are per-folder and
high-volume, settings are per-user and tiny. Keeping them apart means a
corrupt settings file can never cost the user a folder's selection state.

Reads are total — a missing, unreadable or malformed file yields defaults,
because settings are a convenience and must never block startup. Writes
report failure as a string rather than raising, matching ``save_selection``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_DIR_NAME = "PhotoCuller"
SETTINGS_NAME = "settings.json"

# Everything the app persists across runs. Unknown keys found on disk are
# kept (a newer build may have written them) but never invented here.
DEFAULTS: dict[str, Any] = {
    # GPU acceleration scheme id; see gpu_accel.SCHEMES.
    "accel_scheme": "auto",
}

# Only these keys are written back; anything else on disk is preserved
# verbatim so a downgrade does not silently drop a newer build's settings.
KNOWN_KEYS = frozenset(DEFAULTS)


def settings_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else (Path.home() / "AppData" / "Local")
    return root / APP_DIR_NAME


def settings_file() -> Path:
    return settings_root() / SETTINGS_NAME


def load_settings() -> dict[str, Any]:
    """Return defaults merged with whatever is stored. Never raises."""
    stored: dict[str, Any] = {}
    try:
        raw = json.loads(settings_file().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            stored = raw
    except (OSError, ValueError, json.JSONDecodeError):
        stored = {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in stored.items() if k in KNOWN_KEYS})
    merged["_unknown"] = {k: v for k, v in stored.items() if k not in KNOWN_KEYS}
    return merged


def get_value(key: str, default: Any = None) -> Any:
    return load_settings().get(key, default)


def save_settings(values: dict[str, Any]) -> str | None:
    """Merge ``values`` into the stored settings. Error message or None."""
    current = load_settings()
    merged = dict(current.pop("_unknown", {}))
    merged.update({k: current[k] for k in KNOWN_KEYS})
    merged.update({k: v for k, v in values.items() if k in KNOWN_KEYS})
    path = settings_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write via a temp file + replace so an interrupted write cannot leave
        # a half-written settings file behind.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        return f"设置保存失败：{exc}"
    return None
