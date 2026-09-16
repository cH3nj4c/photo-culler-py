"""GPU UI smoke test: exercise the PySide6 + VisPy shell end to end.

Covers: ZoomPlan math, folder scan, RAW+JPG grouping, navigation, keep /
mode state, "只看保留" filter, wheel zoom animation, on-demand full-res
upload, thumbnail pipeline, single-photo delete (Recycle Bin), and export.

The window is created with WA_DontShowOnScreen so nothing appears on the
user's desktop, but QOpenGLWidget still gets a real context (same technique
as gpu_preview_prototype.py's ui-self-test). Deleted files go to the real
Recycle Bin, same as test_delete.py.
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

QAPP = QApplication.instance() or QApplication(["pc-gpu-ui-test"])
QAPP.setStyle("Fusion")

from vispy import app as vispy_app  # noqa: E402

vispy_app.use_app("pyside6")

import qt_ui  # noqa: E402


def pump(seconds: float = 0.2) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        QAPP.processEvents()
        time.sleep(0.005)


def wait_for(condition, timeout: float = 15.0, what: str = "condition") -> None:
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        QAPP.processEvents()
        time.sleep(0.01)
        if condition():
            return
    raise AssertionError(f"timed out waiting for {what}")


def wait_photo(window, index: int, timeout: float = 20.0) -> str:
    """Wait until the stage is really showing the photo at ``index``.

    ``has_image`` is not enough: the previous photo stays on screen while the
    next one decodes, so that condition is already satisfied and a later
    arrival would silently re-fit the camera under the test's feet.
    """

    target = window.visible_items[index].primary_id
    wait_for(
        lambda: window._current_path_id == target
        and window._preview_pil is not None,
        timeout=timeout,
        what=f"displaying {Path(target).name}",
    )
    pump(0.25)  # let the fade and any pending polish land
    return target


# --- 1. ZoomPlan pure math (bounds are live rect widths, not a ratio) ---
from gpu_preview import ZoomPlan  # noqa: E402

# fit width 1000, zoom capped at 4x -> smallest allowed rect width is 250
plan = ZoomPlan(max_width=1000.0, min_width=250.0)
assert plan.clamp_target_width(100.0) == 250.0  # 4x zoom-in cap
assert plan.clamp_target_width(5000.0) == 1000.0  # cannot zoom out past fit
assert plan.clamp_target_width(2000.0) == 1000.0  # cannot zoom out past fit
assert plan.clamp_target_width(600.0) == 600.0
assert 700 < plan.wheel_target_width(1000.0, 0.0, 1.0) < 900  # wheel up zooms in
# a wider viewport moves the cap up: 1000px fit but a 2x larger device
wide = ZoomPlan(max_width=1000.0, min_width=500.0)
assert wide.clamp_target_width(100.0) == 500.0
print("[1] ZoomPlan math OK")

# --- 2. make test photos (one larger than the preview cache edge) ---
tmp = Path(tempfile.mkdtemp(prefix="pc_gpu_"))
photo_dir = tmp / "photos"
photo_dir.mkdir()
Image.new("RGB", (3200, 2400), "purple").save(photo_dir / "A_0001.jpg", quality=90)
Image.new("RGB", (400, 300), "blue").save(photo_dir / "A_0002.png")
Image.new("RGB", (800, 600), "green").save(photo_dir / "B_0001.tif")
Image.new("RGB", (500, 400), "orange").save(photo_dir / "DSC_0001.JPG")
(photo_dir / "DSC_0001.DNG").write_bytes(b"fake-raw-bytes")
print("[2] test photos created")


# --- 3. stub dialogs so the hidden window never blocks ---
class _FakeButtons:
    Yes = 0x00004000
    No = 0x00010000


class _FakeMessageBox:
    StandardButton = _FakeButtons

    @staticmethod
    def question(*args, **kwargs):
        return _FakeButtons.Yes

    @staticmethod
    def information(*args, **kwargs):
        return None

    @staticmethod
    def warning(*args, **kwargs):
        return None

    @staticmethod
    def critical(*args, **kwargs):
        return None


class _FakeFileDialog:
    last_directory = None

    @staticmethod
    def getExistingDirectory(*args, **kwargs):
        return str(_FakeFileDialog.last_directory)


qt_ui.QMessageBox = _FakeMessageBox
qt_ui.QFileDialog = _FakeFileDialog

# --- 4. hidden window + folder open ---
window = qt_ui.PhotoCullerWindow()
window.auto_open_enabled = False  # suppress the automatic dialog
window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
window.show()
QAPP.processEvents()
window.initialize_after_show()
QAPP.processEvents()
if window.stage.gpu_info:
    print("[3] GPU context:", window.stage.gpu_info["renderer"])
else:
    print("[3] GPU context query failed (continuing)")

window._open_folder_path(photo_dir)
wait_for(lambda: window.all_items, what="folder scan")
assert len(window.all_items) == 4, window.all_items
pair = next(g for g in window.all_items if g.paired_raw_jpeg)
assert pair.primary.name == "DSC_0001.JPG"
assert sorted(p.name for p in pair.members) == ["DSC_0001.DNG", "DSC_0001.JPG"]
print("[4] scan + grouping OK:", [g.primary.name for g in window.all_items])

# --- 5. preview decode + GPU upload of first photo ---
wait_photo(window, 0)
wait_for(lambda: window._thumb_pil, what="first thumbnail decode")
assert window.stage.pixel_zoom() > 0
print("[5] preview upload OK, zoom =", round(window.stage.pixel_zoom(), 3))

# --- 6. navigation + keep + mode ---
window.change_index(1)
wait_photo(window, 1)
item = window.current_item
window.toggle_keep()
assert item.key in window.kept
window.cycle_keep_mode()  # item is not a pair; no-op
assert window.index == 1
print("[6] navigation + keep OK")

# --- 6b. marking a photo must not disturb the zoom ---
stage = window.stage
stage.camera._queue_wheel_zoom(1.0, (480.0, 320.0))
pump(0.5)  # let the easing settle
zoom_before = stage.pixel_zoom()
key_before = window.current_item.key
assert zoom_before > stage.fit_pixel_scale() * 1.05, zoom_before
window.toggle_keep()  # a state change, not navigation
assert abs(stage.pixel_zoom() - zoom_before) < 1e-6, (
    f"marking reset the zoom: {zoom_before:.4f} -> {stage.pixel_zoom():.4f}"
)
assert window.current_item.key == key_before
window.toggle_keep()  # restore the kept state the later steps rely on
print("[6b] keep toggle preserves zoom OK")

# --- 7. cursor-anchored wheel zoom animation (prototype parity) ---
window.change_index(-1)  # back from index 1 to index 0
wait_photo(window, 0)
camera = window.stage.camera
width_before = float(camera.rect.width)
camera._queue_wheel_zoom(1.0, (480.0, 320.0))
for _ in range(180):
    camera._last_tick -= 1 / 120
    camera._tick()
QAPP.processEvents()
width_after = float(camera.rect.width)
ratio = width_before / width_after
assert 1.05 < ratio < 1.30, f"unexpected zoom ratio {ratio}"
print("[7] wheel zoom OK, ratio =", round(ratio, 3))

# --- 8. on-demand full resolution (A_0001.jpg is 3200px > 2560px) ---
stage = window.stage
native = stage.preview_native_scale()
assert native is not None, "no texture uploaded"
assert native < 1.0, f"preview already carries full detail ({native})"
assert not window._using_full, "full-res loaded earlier than the native scale"
zoom_deadline = time.perf_counter() + 10.0
while stage.pixel_zoom() <= native and time.perf_counter() < zoom_deadline:
    stage.camera._queue_wheel_zoom(1.0, (480.0, 320.0))
    pump(0.05)
assert stage.pixel_zoom() > native, (
    f"zoom {stage.pixel_zoom():.3f} never passed native scale {native:.3f}"
)
# The upgrade is deliberately deferred: nothing heavy may start while the
# gesture is in flight, or the allocator churn costs a ~60 ms frame.
assert window._loading_full_path_id is None, "heavy work started mid-gesture"
assert not window._using_full, "texture swapped mid-gesture"
# Once the wheel goes quiet the app requests, decodes and swaps on its own.
wait_for(lambda: window._using_full, timeout=25.0, what="full-resolution upload")
assert stage.using_full
assert window._original_size == (3200, 2400), window._original_size
assert window._full_pixels is not None
print("[8] full-resolution upload OK (native scale =", round(native, 3), ")")

# --- 9. back to fit releases the full-res texture ---
window.zoom_fit()
assert not window._using_full
assert window._full_pixels is None
print("[9] fit releases full-res OK")

# --- 10. 只看保留 filter keeps current semantics (un-kept items visible) ---
window.change_index(0)  # A_0001 (not kept)
window.filter_button.setChecked(True)
window.toggle_filter()
assert window.show_kept_only
visible = window.visible_items
assert len(visible) == 3, [i.primary.name for i in visible]
assert not any(i.key in window.kept for i in visible)
window.filter_button.setChecked(False)
window.toggle_filter()
assert len(window.visible_items) == 4
print("[10] 只看保留 filter OK")

# --- 11. delete the RAW+JPG pair (goes to the real Recycle Bin) ---
window.index = next(
    i for i, g in enumerate(window.all_items) if g.paired_raw_jpeg
)
window._sync_filmstrip_selection()
window.change_index(0)  # ensure show path works from pair index
window.index = next(
    i for i, g in enumerate(window.all_items) if g.paired_raw_jpeg
)
window._show_current(center=False)
pair_item = window.current_item
assert pair_item.paired_raw_jpeg
victims = list(pair_item.members)
window.delete_current()
assert all(not p.exists() for p in victims), "pair members survived delete"
assert len(window.all_items) == 3, [g.primary.name for g in window.all_items]
assert pair_item.key not in window.kept and pair_item.key not in window.pair_modes
pump(0.6)
assert window.stage.has_image, "preview did not recover after delete"
print("[11] pair delete OK")

# --- 12. export kept photos (kept item A_0002.png from step 6) ---
export_dir = tmp / "exported"
export_dir.mkdir()
_FakeFileDialog.last_directory = str(export_dir)
window.export_kept()
wait_for(lambda: not window._export_active and any(export_dir.iterdir()), what="export")
exported = sorted(p.name for p in export_dir.iterdir())
assert "A_0002.png" in exported, exported
print("[12] export OK:", exported)

# --- 13. clean shutdown ---
window.close()
QAPP.processEvents()
print("GPU UI SMOKE TEST PASSED")
