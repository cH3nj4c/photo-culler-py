"""Render-mode cache verification.

The cache is the only thing that makes a chosen render mode survive an app
restart, so these checks are about exactly that: the value round-trips through
disk, damage of every kind degrades to the default instead of raising, an
older build's copy is still honoured, and the exit-time cleanup leaves the
files alone (it used to delete them, which is why the choice would not stick).

Everything writes the real files, so both are snapshotted first and put back
at the end — a run must never cost the user their chosen scheme.

Run:  python test_render_mode_cache.py
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app_settings  # noqa: E402
import gpu_accel  # noqa: E402
import render_mode_cache  # noqa: E402
from temp_cleanup import cleanup_app_data_scratch  # noqa: E402

# --- 0. preserve whatever the user already had -------------------------------
_settings_path = app_settings.settings_file()
_cache_path = render_mode_cache.cache_file()
_saved_settings = _settings_path.read_text(encoding="utf-8") if _settings_path.exists() else None
_saved_cache = _cache_path.read_text(encoding="utf-8") if _cache_path.exists() else None


def _restore() -> None:
    for path, saved in ((_settings_path, _saved_settings), (_cache_path, _saved_cache)):
        try:
            if saved is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_text(saved, encoding="utf-8")
        except OSError:
            pass


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- 1. round-trip through disk ----------------------------------------------
assert render_mode_cache.save_mode("software") is None
assert render_mode_cache.load_mode() == "software"
stored = json.loads(_cache_path.read_text(encoding="utf-8"))
assert stored["mode"] == "software", stored
assert stored["updated"], "the cache records when it was written"
print("[1] save/load round-trips through render_mode.json")

# --- 2. a fresh process sees the same choice ---------------------------------
# Nothing is cached in memory: reload the module (as a new launch would import
# it) and the value must still come back from disk.
importlib.reload(render_mode_cache)
assert render_mode_cache.load_mode() == "software", "choice must survive a restart"
assert render_mode_cache.cache_file() == _cache_path, "reload must not move the file"
print("[2] a reloaded module still reads the stored mode (no in-memory state)")

# --- 3. the settings copy older builds read ----------------------------------
assert app_settings.get_value("accel_scheme") == "software", (
    "every save must also write the legacy key, or older builds would forget it"
)
print("[3] compatibility copy written to settings.json")

# --- 4. gpu_accel goes through the cache -------------------------------------
assert gpu_accel.set_scheme_id("discrete") is None
assert gpu_accel.current_scheme_id() == "discrete"
assert json.loads(_cache_path.read_text(encoding="utf-8"))["mode"] == "discrete"
assert gpu_accel.set_scheme_id("bogus") is not None, "unknown ids must be rejected"
assert gpu_accel.current_scheme_id() == "discrete", "a rejected write must not apply"
print("[4] scheme ids round-trip and unknown ids are refused")

# --- 5. a damaged cache falls back to the settings copy ----------------------
_write(_cache_path, "{ not json")
assert gpu_accel.current_scheme_id() == "discrete", "the legacy copy must still count"
_write(_cache_path, json.dumps(["not", "an", "object"]))
assert gpu_accel.current_scheme_id() == "discrete"
_write(_cache_path, json.dumps({"mode": 42}))
assert gpu_accel.current_scheme_id() == "discrete", "a non-string mode is not a mode"
print("[5] corrupt/odd cache files degrade to the settings copy, never raise")

# --- 6. nothing stored at all degrades to the default ------------------------
_write(_cache_path, "{ not json")
_write(_settings_path, "{ not json")
assert render_mode_cache.load_mode() == "auto"
assert gpu_accel.current_scheme_id() == gpu_accel.DEFAULT_SCHEME
# A caller-supplied default is honoured: app_settings' own default must not
# masquerade as a stored choice.
assert render_mode_cache.load_mode("software") == "software"
print("[6] missing/corrupt everything falls back to the default")

# --- 7. unusable writes are refused and leave the cache alone ----------------
_write(_cache_path, json.dumps({"mode": "integrated"}))
for bad in ("", "   ", None, 7):
    error = render_mode_cache.save_mode(bad)
    assert isinstance(error, str) and error, f"{bad!r} should not be storable"
assert json.loads(_cache_path.read_text(encoding="utf-8"))["mode"] == "integrated"
print("[7] blank/non-string modes are rejected without touching the file")

# --- 8. exit-time cleanup keeps the cache (and settings) alive ---------------
# This is the regression the whole module exists to survive: cleanup used to
# delete every file under %LOCALAPPDATA%\PhotoCuller except `selections`, so
# the render mode reverted to its default on the next launch.
with tempfile.TemporaryDirectory(prefix="pc_temp_render_mode_") as tmp:
    root = Path(tmp)
    (root / "render_mode.json").write_text('{"mode": "software"}', encoding="utf-8")
    (root / "settings.json").write_text('{"accel_scheme": "software"}', encoding="utf-8")
    (root / "selections").mkdir()
    (root / "selections" / "a.json").write_text("{}", encoding="utf-8")
    scratch_dir = root / "scratch"
    scratch_dir.mkdir()
    (scratch_dir / "junk.txt").write_text("x", encoding="utf-8")
    (root / "leftover.tmp").write_text("x", encoding="utf-8")

    removed = cleanup_app_data_scratch(root)

    assert (root / "render_mode.json").exists(), "cleanup deleted the render mode"
    assert (root / "settings.json").exists(), "cleanup deleted the settings"
    assert (root / "selections" / "a.json").exists(), "cleanup deleted selections"
    assert not scratch_dir.exists(), "scratch directories must still be removed"
    assert not (root / "leftover.tmp").exists(), "scratch files must still be removed"
    assert removed == 2, removed
print("[8] exit cleanup keeps user data (render mode / settings / selections)")

# --- cleanup -----------------------------------------------------------------
_restore()
print()
print("restored:", "both files were absent" if _saved_settings is None and _saved_cache is None
      else "both files as they were")
print("RENDER MODE CACHE TEST PASSED")
