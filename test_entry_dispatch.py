"""Entry-point dispatch verification (no window is opened)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 1. `app` must import even without tkinter (Qt-only environment).
import app  # noqa: E402

print("[1] import app OK; PhotoCuller =", app.PhotoCuller)

# 2. environment-variable dispatch mapping.
cases = [
    (None, "auto"),
    ("", "auto"),
    ("qt", "qt"),
    ("QT", "qt"),
    ("tk", "tk"),
    (" Tk ", "tk"),
    ("bogus", "auto"),
]
for raw, expected in cases:
    if raw is None:
        os.environ.pop("PHOTOCULLER_UI", None)
    else:
        os.environ["PHOTOCULLER_UI"] = raw
    got = app._ui_preference()
    assert got == expected, f"{raw!r} -> {got!r}, expected {expected!r}"
os.environ.pop("PHOTOCULLER_UI", None)
print("[2] _ui_preference mapping OK")

# 3. the Tk fallback must fail loudly and say *what* is missing, not guess.
#    Asserting on the word "tkinter" would be wrong: whenever the Tk shell
#    trips over numpy instead (a bare system Python), tkinter is present and
#    the message legitimately never mentions it.
if app.PhotoCuller is None:
    try:
        app.run_tk_ui()
    except RuntimeError as exc:
        text = str(exc)
        assert "Tkinter 界面" in text, text
        assert "缺少" in text, text
        print("[3] run_tk_ui raises a diagnostic RuntimeError")
    else:
        raise AssertionError("run_tk_ui should have raised")
else:
    print("[3] tkinter present here; fallback is the real ui.main")

# 4. forcing tk in a tkinter-less Python must surface a clear message,
#    never a bare traceback from `import ui`.
os.environ["PHOTOCULLER_UI"] = "tk"
if app.PhotoCuller is None:
    try:
        app.main()
    except RuntimeError as exc:
        assert "Tkinter 界面" in str(exc)
        print("[4] PHOTOCULLER_UI=tk fails with a clear message")
    else:
        print("[4] skipped (main() would block on the Tk window here)")
else:
    print("[4] skipped (tkinter present)")
os.environ.pop("PHOTOCULLER_UI", None)

# 5. the startup diagnosis must probe every shell's own requirements and name
#    the interpreter. Reporting only the first ImportError is what made the
#    original message blame tkinter for a missing numpy.
diag = app._startup_diagnosis("测试标题")
assert "测试标题" in diag, diag
assert "GPU 界面缺少：" in diag, diag
assert "Tkinter 界面缺少：" in diag, diag
assert sys.executable in diag, diag
# numpy is required by both shells, so it must appear in both lists.
assert ("numpy", "numpy") in app._GPU_SHELL_MODULES
assert ("numpy", "numpy") in app._TK_SHELL_MODULES
assert ("tkinter", "tkinter（需换一个编译时带 tkinter 的 Python）") in app._TK_SHELL_MODULES
assert all("rawpy" != m for m, _ in app._GPU_SHELL_MODULES + app._TK_SHELL_MODULES), \
    "rawpy is optional; it must not be reported as a startup blocker"
print("[5] startup diagnosis names both shells and the interpreter")

# 6. the self-test must exercise a *real* Tk root, not a bare Tcl() probe.
#    `tk.Tcl().eval("package require Tk")` resolves tk86t.dll through Tcl's
#    relative path guess, which fails on a source run even though Tk works
#    fine there — it only ever seemed to pass in the packaged build, where the
#    build script copies the DLLs into bin/. That made the check report a
#    broken runtime while the app itself started correctly.
import subprocess  # noqa: E402

proc = subprocess.run(
    [sys.executable, str(Path(app.__file__).resolve()), "--self-test"],
    capture_output=True, text=True, timeout=300,
)
assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
assert "runtime OK" in proc.stdout, proc.stdout
assert "Traceback" not in proc.stderr, proc.stderr
for expected in ("Tkinter 界面可用", "GPU 界面依赖齐全"):
    assert expected in proc.stdout, (expected, proc.stdout)
# The self-test also exercises the real DXGI/registry detection, which is the
# part most likely to work from source but break once frozen. Assert the
# report is present without requiring specific hardware (a VM has none).
assert "显示适配器：" in proc.stdout, proc.stdout
assert "加速方案：" in proc.stdout, proc.stdout
print("[6] --self-test verifies both shells with a real Tk root and exits 0")

# 7. hardware detection must survive independently of the shells, and must
#    never raise — it is consulted from a menu click on the UI thread.
import gpu_info  # noqa: E402

report = gpu_info.detect_gpu()
assert not report.errors, report.errors
assert isinstance(report.headline(), str) and report.headline()
assert report.is_hybrid == (report.has_discrete and report.has_integrated)
# A machine with no adapters must still produce a usable headline.
assert gpu_info.GpuReport().headline()
print(f"[7] gpu detection OK: {report.headline()} "
      f"(hybrid={report.is_hybrid}, {len(report.adapters)} adapter(s))")

print("ENTRY-POINT DISPATCH TEST PASSED")
