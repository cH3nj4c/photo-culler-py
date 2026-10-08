"""Remove Photo Culler temp artifacts on exit.

User data under %LOCALAPPDATA%\\PhotoCuller is never touched: the per-folder
selection records, ``settings.json`` and the render-mode cache
(``render_mode.json``) all have to outlive the session — deleting them here is
exactly how a choice silently reverts to its default on the next launch. Only
throwaway dirs/files under the system temp folder that match this app's
prefixes, and scratch entries beside that user data, are deleted.
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

# Entries under %LOCALAPPDATA%\PhotoCuller that are user data, not scratch.
# Compared case-insensitively, like the rest of the names in this module.
_USER_DATA_NAMES = frozenset({
    "selections",
    "settings.json",
    "render_mode.json",
})

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


def app_data_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else (Path.home() / "AppData" / "Local")
    return root / "PhotoCuller"


def cleanup_app_data_scratch(root: Path | None = None) -> int:
    """Remove scratch beside the user data. Returns how many entries went.

    ``root`` defaults to the real ``%LOCALAPPDATA%\\PhotoCuller``; tests pass a
    sandbox so the assertion never runs against the user's own files.
    """
    app_root = app_data_root() if root is None else root
    if not app_root.is_dir():
        return 0
    removed = 0
    for child in app_root.iterdir():
        if child.name.lower() in _USER_DATA_NAMES:
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
