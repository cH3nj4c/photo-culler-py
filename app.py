"""Photo Culler - a small Windows-first photo selection application.

The app deliberately copies selected originals on export; it never moves,
renames, or edits the source photographs.  The single exception is the
explicit delete command, which sends originals to the Windows Recycle Bin
so a mistaken deletion stays recoverable.

This module is a thin entry point. Implementation lives in sibling modules
next to this file:

- ``qt_ui``   PySide6 shell with the GPU preview engine (VisPy/OpenGL).
- ``ui``      Tkinter shell with the CPU preview pipeline (fallback).
- ``gpu_accel`` / ``gpu_info``  hardware detection and the selectable
  acceleration schemes (see the "GPU 加速…" menu entry).

Startup dispatch (see :func:`main`):

- ``PHOTOCULLER_UI=qt``  force the PySide6 + VisPy GPU shell;
- ``PHOTOCULLER_UI=tk``  force the Tkinter shell;
- default ``auto``       prefer the GPU shell, fall back to Tk when
  PySide6/vispy are not installed.

The selected acceleration scheme is applied to the environment *before* the
shells are imported, because Qt reads ``QT_OPENGL`` while initialising its
platform plugin.
"""

from __future__ import annotations

import os

from winshell import configure_bundled_tk_runtime

configure_bundled_tk_runtime()

# Must happen before PySide6/vispy are imported: Qt reads QT_OPENGL while it
# initialises its platform plugin, and the GPU shell imports Qt at module
# import time. Settings are read here rather than in qt_ui so that the Tk
# shell's resampler honours the same choice.
import gpu_accel  # noqa: E402

gpu_accel.apply_environment()

from domain import (  # noqa: E402
    PhotoGroup,
    build_photo_groups,
    scan_photo_entries,
    scan_photo_paths,
    selected_members,
)
from winshell import send_to_recycle_bin  # noqa: E402

# Filled in when an optional shell fails to import. Keeping the exceptions
# around matters: without them the final message can only guess at what is
# missing, and a wrong guess sends the user down the wrong path (the original
# version always blamed tkinter even when the real culprit was numpy).
_tk_import_error: BaseException | None = None
_qt_import_error: BaseException | None = None

# (import name, pip name). rawpy is optional — imaging.decode_photo raises a
# clear error at the point of use when RAW support is absent, so a missing
# rawpy must not be reported as a startup blocker.
_GPU_SHELL_MODULES = (
    ("numpy", "numpy"),
    ("PIL", "Pillow"),
    ("PySide6", "PySide6"),
    ("vispy", "vispy"),
    ("OpenGL", "PyOpenGL"),
)
_TK_SHELL_MODULES = (
    ("numpy", "numpy"),
    ("PIL", "Pillow"),
    ("tkinter", "tkinter（需换一个编译时带 tkinter 的 Python）"),
)

try:  # Tk is the fallback shell; a Qt-only Python may lack the tkinter module.
    from ui import PhotoCuller  # noqa: E402
    from ui import main as run_tk_ui  # noqa: E402
except ImportError as _exc:  # noqa: E402
    # The `as` target is cleared when the except block ends, so copy it out.
    _tk_import_error = _exc

    PhotoCuller = None  # type: ignore[assignment]

    def run_tk_ui() -> None:
        raise RuntimeError(_startup_diagnosis("Tkinter 界面无法启动。"))


def _missing_for(specs) -> list[tuple[str, str]]:
    """Which of ``specs`` are not importable in this interpreter.

    Uses ``find_spec`` rather than importing: probing must not pull in
    PySide6/vispy (slow) and must not raise on a partially broken install.
    """
    import importlib.util

    missing: list[tuple[str, str]] = []
    for module, dist in specs:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append((module, dist))
    return missing


def _startup_diagnosis(headline: str) -> str:
    """Explain what actually failed, per shell, plus what to install.

    Both shells are listed with their *own* missing modules, because the fix
    differs: the GPU shell needs PySide6/vispy/PyOpenGL, the Tk shell needs a
    Python built with tkinter. Reporting only the first ImportError is
    misleading — in a bare Python the Qt shell trips over numpy before it ever
    reaches PySide6, which made the GPU shell look like it needed numpy only.
    """
    import sys

    gpu_missing = _missing_for(_GPU_SHELL_MODULES)
    tk_missing = _missing_for(_TK_SHELL_MODULES)

    lines = [headline, f"  当前 Python：{sys.executable}", ""]
    lines.append(
        "  GPU 界面缺少：" + ("、".join(d for _, d in gpu_missing) or "无（可用）")
    )
    lines.append(
        "  Tkinter 界面缺少：" + ("、".join(d for _, d in tk_missing) or "无（可用）")
    )

    # The tkinter entry carries guidance in parentheses, not a pip name.
    gpu_pips = [d for _, d in gpu_missing if "（" not in d]
    tk_pips = [d for _, d in tk_missing if "（" not in d]
    hints = []
    if gpu_pips:
        hints.append("  GPU 界面：pip install " + " ".join(gpu_pips))
    if tk_pips:
        hints.append("  Tkinter 界面：pip install " + " ".join(tk_pips))
    if hints:
        lines.append("")
        lines.append("安装上面对应界面的依赖后重试：")
        lines.extend(hints)
    if any(m == "tkinter" for m, _ in tk_missing):
        lines.append("  （tkinter 无法用 pip 安装，请改用自带 tkinter 的 Python 3.13）")

    for label, error in (
        ("Tkinter 界面导入错误", _tk_import_error),
        ("GPU 界面导入错误", _qt_import_error),
    ):
        if error is not None:
            lines.append(f"  {label}：{error}")

    lines.append("")
    lines.append("也可以直接运行已打包的版本：dist\\PhotoCuller\\Photo Culler.exe")
    return "\n".join(lines)


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


def _run_self_test() -> None:
    """Verify the packaged runtime can actually bring up each shell.

    Creates a real (hidden) Tk root rather than evaluating ``package require
    Tk`` on a bare ``Tcl()`` interpreter. The latter resolves ``tk86t.dll``
    through Tcl's own relative path guess, which fails in a source run even
    when Tk is perfectly usable — it only ever looked like it worked in the
    packaged build because the build script copies the DLLs into ``bin/``.
    Creating ``tk.Tk()`` is what the app itself does, and it sets up the DLL
    search path correctly either way. Reported as OK/fail, never raised: the
    point is to print a verdict and exit with a status.
    """
    import sys

    verdicts: list[str] = []
    failed = False

    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.destroy()
    except Exception as exc:  # noqa: BLE001 - report, do not traceback
        verdicts.append(f"Tkinter 界面不可用：{type(exc).__name__}: {exc}")
        failed = True
    else:
        verdicts.append("Tkinter 界面可用")

    missing = _missing_for(_GPU_SHELL_MODULES)
    if missing:
        verdicts.append(
            "GPU 界面缺少：" + "、".join(dist for _mod, dist in missing)
        )
    else:
        verdicts.append("GPU 界面依赖齐全")

    # Hardware detection runs real ctypes/DXGI/registry code, which is exactly
    # the kind of thing that can work from source and break once frozen. Print
    # what the packaged build actually sees (also handy in a support report).
    try:
        import gpu_info

        report = gpu_info.detect_gpu()
        if report.adapters:
            kinds = []
            if report.has_discrete:
                kinds.append("独显")
            if report.has_integrated:
                kinds.append("核显")
            verdicts.append(f"显示适配器：{report.headline()}")
            if kinds:
                verdicts.append(
                    "显卡类型：" + " + ".join(kinds)
                    + ("（混合显卡）" if report.is_hybrid else "")
                )
        else:
            verdicts.append("显示适配器：未检测到（可能是虚拟机或远程会话）")
        if report.errors:
            verdicts.append("显卡检测问题：" + "；".join(report.errors))
    except Exception as exc:  # noqa: BLE001 - detection is informational
        verdicts.append(f"显卡检测不可用：{type(exc).__name__}: {exc}")

    try:
        import gpu_accel

        # Name the scheme the Tk probe above just ran under, and whether the
        # app would be allowed to write the Windows preference here.
        verdicts.append(
            f"加速方案：{gpu_accel.get_scheme(gpu_accel.effective_scheme_id()).label}"
        )
        if gpu_accel.target_executable() is None:
            verdicts.append("Windows 显卡偏好：源码运行，不写入注册表")
    except Exception as exc:  # noqa: BLE001
        verdicts.append(f"加速方案不可用：{type(exc).__name__}: {exc}")

    for line in verdicts:
        print(line)
    if failed:
        print("Photo Culler runtime INCOMPLETE")
    else:
        print("Photo Culler runtime OK")


def main() -> None:
    import sys

    global _qt_import_error

    try:
        if "--self-test" in sys.argv:
            _run_self_test()
            raise SystemExit(0)

        preference = _ui_preference()
        if preference != "tk":
            try:
                from qt_ui import main as qt_main
            except ImportError as exc:
                _qt_import_error = exc
                if preference == "qt":
                    raise SystemExit(
                        _startup_diagnosis(
                            "PHOTOCULLER_UI=qt，但 GPU 界面依赖不可用。"
                        )
                    ) from exc
                # auto: falling back to Tk is expected, but say so on stderr —
                # otherwise "the GPU engine is silently absent" looks like the
                # engine is broken rather than simply not installed.
                print(
                    f"[Photo Culler] GPU 界面不可用（{exc}），改用 Tkinter 界面。\n"
                    "  想要 GPU 界面请先：pip install PySide6 vispy PyOpenGL",
                    file=sys.stderr,
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
