"""Selection persistence under the user's local app-data directory."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from domain import normalize_pair_mode


def selections_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else (Path.home() / "AppData" / "Local")
    path = root / "PhotoCuller" / "selections"
    path.mkdir(parents=True, exist_ok=True)
    return path


def selection_file(folder: Path) -> Path:
    digest = hashlib.sha256(str(folder.resolve()).encode("utf-8")).hexdigest()[:20]
    return selections_root() / f"{digest}.json"


def load_selection(folder: Path | None) -> tuple[set[str], dict[str, str]]:
    if folder is None:
        return set(), {}
    try:
        data = json.loads(selection_file(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return set(), {}

    kept = {key for key in data.get("kept", []) if isinstance(key, str)}
    raw_pair_modes = data.get("pair_modes", {})
    if not isinstance(raw_pair_modes, dict):
        raw_pair_modes = {}
    pair_modes = {
        key: normalize_pair_mode(mode)
        for key, mode in raw_pair_modes.items()
        if isinstance(key, str) and isinstance(mode, str)
    }
    return kept, pair_modes


def save_selection(
    folder: Path | None,
    kept: set[str],
    pair_modes: dict[str, str],
) -> str | None:
    """Persist selection state. Returns an error message, or None on success."""
    if folder is None:
        return "尚未打开文件夹"
    payload = {
        "folder": str(folder),
        "kept": sorted(kept),
        "pair_modes": dict(sorted(pair_modes.items())),
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        selection_file(folder).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        return f"选片记录保存失败：{exc}"
    return None
