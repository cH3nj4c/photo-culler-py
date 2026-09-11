"""Windows-specific process/DPI and Recycle Bin helpers."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


def configure_bundled_tk_runtime() -> None:
    """Point the packaged app at its own complete Tcl/Tk runtime before tkinter imports."""
    if not getattr(sys, "frozen", False):
        return
    bundle = sys._MEIPASS
    os.environ["TCL_LIBRARY"] = str(Path(bundle) / "tcl" / "tcl8.6")
    os.environ["TK_LIBRARY"] = str(Path(bundle) / "tk" / "tk8.6")
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(Path(bundle) / "bin"))


def enable_windows_high_dpi() -> bool:
    """Opt out of Windows bitmap scaling so Tk is rendered sharply on HiDPI monitors.

    Must be called before any Tk window (or other top-level HWND / COM) is created.
    """
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    ctypes.windll.kernel32.SetLastError(0)

    # 1. Per-Monitor V2 — best quality on mixed-DPI setups.
    try:
        set_pm_v2 = user32.SetProcessDpiAwarenessContext
        set_pm_v2.argtypes = [ctypes.c_void_p]
        set_pm_v2.restype = ctypes.c_bool
        if set_pm_v2(ctypes.c_void_p(-4)):
            return True
    except (AttributeError, OSError):
        pass

    # 2. Per-Monitor (pre-Win10).
    try:
        set_pm = ctypes.windll.shcore.SetProcessDpiAwareness
        set_pm.argtypes = [ctypes.c_int]
        set_pm.restype = ctypes.c_long
        if set_pm(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            return True
    except (AttributeError, OSError):
        pass

    # 3. System aware — last resort.
    try:
        set_system = user32.SetProcessDPIAware
        set_system.restype = ctypes.c_bool
        if set_system():
            return True
    except (AttributeError, OSError):
        pass

    return False


FO_DELETE = 0x0003
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


class _SHFileOpStructW(ctypes.Structure):
    """Win32 SHFILEOPSTRUCTW, used only to issue one Recycle Bin delete."""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_void_p),
        ("pTo", ctypes.c_void_p),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_void_p),
    ]


def send_to_recycle_bin(paths) -> list[tuple[Path, str]]:
    """Move *paths* to the Recycle Bin and return a (path, reason) list of failures.

    FOF_ALLOWUNDO makes the delete undoable. FOF_NOCONFIRMATION and FOF_NOERRORUI
    suppress Windows' own dialogs because the caller already asked the user.
    """
    targets = [Path(path) for path in paths]
    if not targets:
        return []
    if sys.platform != "win32":
        return [(path, "只有 Windows 支持回收站删除") for path in targets]

    failures = [(path, "文件已不存在") for path in targets if not path.exists()]
    pending = [path for path in targets if path.exists()]
    if not pending:
        return failures

    # SHFileOperationW requires a double-NUL-terminated path block.
    source_block = ctypes.create_unicode_buffer(
        "\0".join(str(path) for path in pending) + "\0"
    )
    operation = _SHFileOpStructW()
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.cast(source_block, ctypes.c_void_p)
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI
    shell = ctypes.windll.shell32
    shell.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFileOpStructW)]
    shell.SHFileOperationW.restype = ctypes.c_int
    try:
        code = shell.SHFileOperationW(ctypes.byref(operation))
    except OSError as exc:
        return failures + [(path, str(exc)) for path in pending]
    if code != 0:
        failures.extend((path, f"Shell 错误码 {code}") for path in pending)
    elif operation.fAnyOperationsAborted:
        failures.extend((path, "操作被系统中断") for path in pending)
    else:
        failures.extend((path, "文件仍在原处") for path in pending if path.exists())
    return failures
