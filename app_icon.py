"""Apply the app logo to the Tk window and Windows taskbar."""

from __future__ import annotations

import sys
from pathlib import Path


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def logo_candidates() -> list[Path]:
    root = resource_root()
    project = Path(__file__).resolve().parent
    names = ("logo-mark.png", "app.ico", "logo.png")
    out: list[Path] = []
    for base in (root / "assets", root, project / "assets", project):
        for name in names:
            p = base / name
            if p.is_file():
                out.append(p)
    # de-dupe while keeping order
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def apply_window_icon(root) -> None:
    """Set window + taskbar icon from assets/app.ico or logo PNG."""
    # Stable AppUserModelID so Windows taskbar uses our exe icon consistently.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "cH3nj4c.PhotoCuller"
            )
        except Exception:
            pass

    for path in logo_candidates():
        try:
            if path.suffix.lower() == ".ico":
                # Windows: default icon for window + taskbar.
                root.iconbitmap(default=str(path))
                root.iconbitmap(str(path))
            else:
                import tkinter as tk

                img = tk.PhotoImage(file=str(path))
                root.iconphoto(True, img)
                # Keep a reference so Tk does not garbage-collect the image.
                root._pc_icon_ref = img  # type: ignore[attr-defined]
            return
        except Exception:
            continue
