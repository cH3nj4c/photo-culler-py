"""Remove Photo Culler temp artifacts on exit.

User data (selection JSON under %LOCALAPPDATA%\\PhotoCuller\\selections) is
never touched. Only throwaway dirs/files under the system temp folder that
match this app's prefixes are deleted.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

# Prefixes used by the app and its smoke/delete tests.
_TEMP_PREFIXES = (
    "photoculler",
    "photo_culler",
    "photo-culler",
    "pc_smoke_",
    "pc_delete_",
    "pc_temp_",
    "pc_culler_",
)

# Skip very fresh dirs so a concurrently running test isn't wiped mid-run.
_MIN_AGE_SECONDS = 120.0


def _is_app_temp_name(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.startswith(prefix) for prefix in _TEMP_PREFIXES)


def cleanup_temp_files(max_age_seconds: float = _MIN_AGE_SECONDS) -> int:
    """Delete matching temp files/dirs. Returns how many entries were removed."""
    root = Path(tempfile.gettempdir())
    removed = 0
    if not root.is_dir():
        return 0
    now = time.time()
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not _is_app_temp_name(entry.name):
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < max_age_seconds:
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed


def cleanup_app_data_scratch() -> int:
    """Remove empty scratch under %LOCALAPPDATA%\\PhotoCuller except selections."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else (Path.home() / "AppData" / "Local")
    app_root = root / "PhotoCuller"
    if not app_root.is_dir():
        return 0
    removed = 0
    for child in app_root.iterdir():
        if child.name.lower() == "selections":
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed


def cleanup_on_exit() -> None:
    """Best-effort cleanup when the app window closes."""
    try:
        cleanup_temp_files()
    except Exception:
        pass
    try:
        cleanup_app_data_scratch()
    except Exception:
        pass
