"""Photo Culler - a small Windows-first photo selection application.

The app deliberately copies selected originals on export; it never moves,
renames, or edits the source photographs.  The single exception is the
explicit delete command, which sends originals to the Windows Recycle Bin
so a mistaken deletion stays recoverable.

This module is a thin entry point. Implementation lives in sibling modules
next to this file:

- ``qt_ui``   PySide6 shell with the GPU preview engine (VisPy/OpenGL).
- ``ui``      Tkinter shell with the CPU preview pipeline (fallback).

Startup dispatch (see :func:`main`):

- ``PHOTOCULLER_UI=qt``  force the PySide6 + VisPy GPU shell;
- ``PHOTOCULLER_UI=tk``  force the Tkinter shell;
- default ``auto``       prefer the GPU shell, fall back to Tk when
  PySide6/vispy are not installed.
"""

from __future__ import annotations

import os

from winshell import configure_bundled_tk_runtime

configure_bundled_tk_runtime()

from domain import (  # noqa: E402
    PhotoGroup,
    build_photo_groups,
    scan_photo_entries,
    scan_photo_paths,
    selected_members,
)
from winshell import send_to_recycle_bin  # noqa: E402

try:  # Tk is the fallback shell; a Qt-only Python may lack the tkinter module.
    from ui import PhotoCuller  # noqa: E402
    from ui import main as run_tk_ui  # noqa: E402
except ImportError as _exc:  # noqa: E402
    # The `as` target is cleared when the except block ends, so copy it out.
    _tk_import_error = _exc

    PhotoCuller = None  # type: ignore[assignment]

    def run_tk_ui() -> None:
        raise RuntimeError(
            "Tkinter 界面不可用（缺少 tkinter 模块）。\n"
            f"{_tk_import_error}\n"
            "请安装带 tkinter 的 Python，或安装 PySide6、vispy、PyOpenGL 使用 GPU 界面。"
        )


__all__ = [
    "PhotoCuller",
    "PhotoGroup",
    "build_photo_groups",
    "scan_photo_paths",
    "selected_members",
    "send_to_recycle_bin",
    "main",
]


def _ui_preference() -> str:
    raw = os.environ.get("PHOTOCULLER_UI", "auto").strip().lower()
    return raw if raw in ("qt", "tk") else "auto"


def _report_startup_failure(error: BaseException) -> None:
    """Show a startup error. Falls back to stderr when Tk is unavailable."""
    import sys

    message = f"程序启动失败：\n{error}"
    try:
        import tkinter as tk
        from tkinter import messagebox

        from config import APP_NAME

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def main() -> None:
    import sys

    try:
        if "--self-test" in sys.argv:
            import tkinter as tk

            probe = tk.Tcl()
            probe.eval("package require Tk")
            print("Photo Culler runtime OK")
            raise SystemExit(0)

        preference = _ui_preference()
        if preference != "tk":
            try:
                from qt_ui import main as qt_main
            except ImportError as exc:
                if preference == "qt":
                    raise SystemExit(
                        "PHOTOCULLER_UI=qt，但 GPU 界面依赖不可用：\n"
                        f"{exc}\n"
                        "请安装 PySide6、vispy、PyOpenGL，或改用 PHOTOCULLER_UI=tk。"
                    )
            else:
                qt_main()
                return
        run_tk_ui()
    except SystemExit:
        raise
    except BaseException as error:  # noqa: BLE001 - report then re-raise
        _report_startup_failure(error)
        raise


if __name__ == "__main__":
    main()
