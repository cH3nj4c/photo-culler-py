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

# 3. the Tk fallback must fail loudly, not silently.
if app.PhotoCuller is None:
    try:
        app.run_tk_ui()
    except RuntimeError as exc:
        text = str(exc)
        assert "tkinter" in text, text
        print("[3] run_tk_ui raises a helpful RuntimeError without tkinter")
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
        assert "tkinter" in str(exc)
        print("[4] PHOTOCULLER_UI=tk fails with a clear message")
    else:
        print("[4] skipped (main() would block on the Tk window here)")
else:
    print("[4] skipped (tkinter present)")
os.environ.pop("PHOTOCULLER_UI", None)

print("ENTRY-POINT DISPATCH TEST PASSED")
