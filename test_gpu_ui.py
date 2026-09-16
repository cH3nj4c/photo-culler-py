"""GPU UI smoke test: exercise the PySide6 + VisPy shell end to end.

Covers: ZoomPlan math, folder scan, RAW+JPG grouping, navigation, keep /
mode state, "只看保留" filter, wheel zoom animation, on-demand full-res
upload, thumbnail pipeline (including that grouping stays off the UI thread
and that the strip is scrollable by every kind of wheel input), single-photo
delete (Recycle Bin), and export.

The window is created with WA_DontShowOnScreen so nothing appears on the
user's desktop, but QOpenGLWidget still gets a real context (same technique
as gpu_preview_prototype.py's ui-self-test). Deleted files go to the real
Recycle Bin, same as test_delete.py.
"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QListWidget,
    QPushButton,
    QWidget,
)

QAPP = QApplication.instance() or QApplication(["pc-gpu-ui-test"])
QAPP.setStyle("Fusion")

from vispy import app as vispy_app  # noqa: E402

vispy_app.use_app("pyside6")

import qt_ui  # noqa: E402
import config  # noqa: E402
from config import THUMB_SLOT  # noqa: E402
import gpu_accel  # noqa: E402
import gpu_info  # noqa: E402


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

# --- 13. thumbnail strip: every photo gets a row, and it can be scrolled ---
# Two field bugs lived here:
#  * build_photo_groups() resolved every path (a real filesystem call, ~1 ms
#    each) on the UI thread, so opening a large folder froze the app for
#    seconds while the strip was still filling in.
#  * FilmStrip.wheelEvent used int(-delta / 120) * 96, which is 0 for any
#    sub-notch delta (precision trackpads), and ignored angleDelta().x() — so
#    the photos past the first screen could not be reached at all.
strip_dir = tmp / "many"
strip_dir.mkdir()
STRIP_PHOTOS = 150
for i in range(STRIP_PHOTOS):
    Image.new("RGB", (200, 150), (30 + i % 200, 70, 110)).save(
        strip_dir / f"S_{i:04d}.jpg", quality=70
    )

# Watch where grouping happens: it must not run on the UI thread any more.
_main_thread = threading.get_ident()
_group_calls: list[tuple[int, int]] = []
_real_build_groups = qt_ui.build_photo_groups


def _spy_build_groups(paths, mtime_ns_by_path=None):
    _group_calls.append((threading.get_ident(), len(paths)))
    return _real_build_groups(paths, mtime_ns_by_path)


qt_ui.build_photo_groups = _spy_build_groups
try:
    window._open_folder_path(strip_dir)
    wait_for(lambda: len(window.all_items) == STRIP_PHOTOS, timeout=30, what="strip scan")
finally:
    qt_ui.build_photo_groups = _real_build_groups

assert _group_calls, "build_photo_groups was never called"
off_thread = [n for tid, n in _group_calls if tid != _main_thread]
assert off_thread, (
    "build_photo_groups ran on the UI thread; that is the multi-second "
    f"freeze on large folders (calls: {_group_calls})"
)
print(f"[13] grouping ran off the UI thread for {off_thread} photos")

strip = window.filmstrip
assert strip.count() == len(window.visible_items) == STRIP_PHOTOS, (
    strip.count(), len(window.visible_items)
)
wait_for(
    lambda: all(
        (e := strip.item(r)) is not None
        and e.icon() is not None
        and not e.icon().isNull()
        for r in range(strip.count())
    ),
    timeout=60,
    what="every strip row to get a thumbnail",
)
print(f"[13b] strip shows a row + thumbnail for all {strip.count()} photos")

bar = strip.horizontalScrollBar()
assert bar.maximum() > 0, "strip should be scrollable with this many photos"
# The scroll range must cover every item, otherwise the trailing photos are
# unreachable. Qt reports max + pageStep == items * grid width exactly.
assert bar.maximum() + bar.pageStep() == STRIP_PHOTOS * THUMB_SLOT, (
    bar.maximum(), bar.pageStep(), STRIP_PHOTOS * THUMB_SLOT
)
# A window kept off-screen may never lay its items out, which makes
# scrollToItem a no-op; re-applying the size hints forces the same layout a
# visible window gets.
strip.setGridSize(strip.gridSize())
strip.setIconSize(strip.iconSize())
QAPP.processEvents()
strip.scrollToItem(strip.item(strip.count() - 1), QListWidget.ScrollHint.PositionAtCenter)
QAPP.processEvents()
assert bar.value() == bar.maximum(), (bar.value(), bar.maximum())
print(f"[13c] all {STRIP_PHOTOS} photos are reachable (scrolled to "
      f"{bar.value()}/{bar.maximum()})")


def _wheel(angle_x=0, angle_y=0, pixel_x=0, pixel_y=0):
    p = QPointF(8.0, 8.0)
    return QWheelEvent(
        p,
        p,
        QPoint(pixel_x, pixel_y),
        QPoint(angle_x, angle_y),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


for label, event, minimum in (
    ("full notch", _wheel(angle_y=-120), 90),
    ("slow trackpad", _wheel(angle_y=-15), 1),
    ("horizontal swipe", _wheel(angle_x=-120), 90),
    ("pixel delta", _wheel(pixel_y=-40), 30),
):
    bar.setValue(0)
    strip.wheelEvent(event)
    assert bar.value() >= minimum, f"{label} did not scroll (got {bar.value()})"
print("[13d] wheel scrolls for full notch, slow trackpad, horizontal and pixel deltas")

# --- 13e. a slot must not collapse while its thumbnail is still decoding ---
# In the field the strip rendered as a row of 1-px vertical ticks right after a
# folder scan: QListWidgetItem with no icon yet reports an empty sizeHint, and
# the slot collapses. Wiping the PIL cache and re-rendering reproduces exactly
# that state, deterministically.
window._thumb_pil.clear()
window.thumbnail_service.cancel_pending()
window._render_thumbnails()
QAPP.processEvents()
strip.grab()  # force a real layout + paint pass
QAPP.processEvents()
for r in range(strip.count()):
    entry = strip.item(r)
    hint = entry.sizeHint()
    assert (hint.width(), hint.height()) == (THUMB_SLOT, qt_ui.THUMB_PIX_HEIGHT), (
        r, hint.width(), hint.height()
    )
    rect = strip.visualItemRect(entry)
    assert (rect.width(), rect.height()) == (THUMB_SLOT, qt_ui.THUMB_PIX_HEIGHT), (
        r, rect.width(), rect.height()
    )
print(f"[13e] all {strip.count()} slots keep {THUMB_SLOT}x{qt_ui.THUMB_PIX_HEIGHT} "
      f"while thumbnails are pending (no collapsed slivers)")

# --- 13f. the scrollbar must sit clear of the slot's filename ---------------
# The name is painted at the bottom of each slot pixmap (y 100..116). If the
# viewport is shorter than a slot, what gets clipped first is exactly that
# name — the field report was "the scrollbar covers the photo names".
vp_h = strip.viewport().height()
item_h = strip.visualItemRect(strip.item(0)).height()
assert vp_h >= item_h, f"viewport {vp_h}px is shorter than a {item_h}px slot"
air = vp_h - item_h
assert air >= 4, f"only {air}px of air under the filename; it reads as covered"
print(f"[13f] viewport is {vp_h}px for a {item_h}px slot — {air}px clear under "
      f"the filename, scrollbar cannot cover it")

# --- 14. every action lives in the left sidebar -----------------------------
# The toolbar used to run along the top; all controls now sit in a settings
# sidebar down the left edge, and the preview column starts to its right.
sidebar = window.centralWidget().findChild(QWidget, "sidebar")
assert sidebar is not None, "the sidebar widget is missing"
assert sidebar.x() == 0, f"sidebar should hug the left edge, x={sidebar.x()}"
assert sidebar.width() == qt_ui.SIDEBAR_WIDTH, sidebar.width()
# Context lives in the mirrored right sidebar; resolved here because step 14
# needs it to tell "action out of place" from "legitimate context control".
right_side = window.centralWidget().findChild(QWidget, "sidebarRight")
assert right_side is not None, "the right sidebar is missing"

sidebar_buttons = sidebar.findChildren(QPushButton)
assert len(sidebar_buttons) >= 9, [b.text() for b in sidebar_buttons]
for button in sidebar_buttons:
    assert sidebar.isAncestorOf(button), f"{button.text()} is outside the sidebar"
    assert button.width() >= sidebar.width() - 40, (
        f"{button.text()} is too narrow to read as a menu entry"
    )
stranded = [
    (b.text(), b.objectName())
    for b in window.centralWidget().findChildren(QPushButton)
    if not sidebar.isAncestorOf(b) and not right_side.isAncestorOf(b)
]
assert not stranded, f"buttons left outside both sidebars: {stranded}"
# The right sidebar is context-only, so it may hold the version footer but no
# action control — otherwise the two columns stop being "actions" vs "state".
right_buttons = right_side.findChildren(QPushButton)
assert [b.objectName() for b in right_buttons] == ["link"], [
    (b.text(), b.objectName()) for b in right_buttons
]
assert window.version_button.objectName() == "link"
assert config.APP_VERSION in window.version_button.text(), window.version_button.text()
# ...and the app must show which build it is, which is the whole point.
assert config.APP_VERSION in window.windowTitle(), window.windowTitle()

# The preview column must start where the sidebar ends.
for widget, label in ((window._stack, "preview"), (window.filmstrip, "filmstrip")):
    left = widget.mapTo(window, widget.rect().topLeft()).x()
    assert left >= sidebar.x() + sidebar.width(), f"{label} overlaps the sidebar"
# Keep the primary and destructive actions visually distinct.
assert window.keep_button.objectName() == "accent"
assert window.delete_button.objectName() == "danger"
# The filter toggle must carry the objectName its :checked style keys on,
# otherwise it gives no sign of being switched on.
assert window.filter_button.objectName() == "toggle"
print(f"[14] {len(sidebar_buttons)} actions are in a {sidebar.width()}px left sidebar; "
      f"preview starts at x={sidebar.x() + sidebar.width()}")

# --- 15. folder context lives in the right sidebar --------------------------
# The folder name sits opposite the actions, in its own column pinned to the
# right edge, so the left column stays a pure action menu.
# (`right_side` was resolved in step 14.)
assert right_side.width() == qt_ui.SIDEBAR_RIGHT_WIDTH, right_side.width()

central_w = window.centralWidget().width()
assert right_side.x() + right_side.width() == central_w, (
    f"right sidebar ends at {right_side.x() + right_side.width()}, "
    f"central widget is {central_w} wide"
)

# The folder name must be inside it, and to the right of the preview.
assert right_side.isAncestorOf(window.folder_label), "folder name left the right sidebar"
assert not sidebar.isAncestorOf(window.folder_label), "folder name is still on the left"
name_x = window.folder_label.mapTo(window, window.folder_label.rect().topLeft()).x()
preview_x = window._stack.mapTo(window, window._stack.rect().topLeft()).x()
assert name_x >= preview_x + window._stack.width(), (
    f"folder name (x={name_x}) is not to the right of the preview column"
)

# No *action* may have drifted into the right sidebar. The version footer is
# the one deliberate control there and is asserted separately in step 14.
action_buttons = [
    b for b in right_side.findChildren(QPushButton) if b.objectName() != "link"
]
assert not action_buttons, (
    f"action buttons leaked into the right sidebar: {[b.text() for b in action_buttons]}"
)

# The counter must be populated for the folder that is open, and follow the
# keep action rather than being a static label.
count_text = window.folder_count_label.text()
assert count_text.strip(), "the folder counter is empty for an open folder"
assert str(len(window.all_items)) in count_text, (count_text, len(window.all_items))
window.kept = {window.all_items[0].key}
window._invalidate_visible()
window._update_folder_count()
assert "已保留 1" in window.folder_count_label.text(), window.folder_count_label.text()
window.kept = set()
window._invalidate_visible()
window._update_folder_count()
print(f"[15] folder name + counter ({count_text.splitlines()[0]}) live in a "
      f"{right_side.width()}px right sidebar at x={right_side.x()}")

# --- 16. GPU acceleration menu + detected hardware --------------------------
# Selecting a scheme writes the real settings file, so snapshot it first and
# put it back at the end: a test run must never cost the user their choice.
import app_settings  # noqa: E402

_accel_settings_path = app_settings.settings_file()
_accel_saved = (
    _accel_settings_path.read_text(encoding="utf-8")
    if _accel_settings_path.exists()
    else None
)


def _restore_accel_settings() -> None:
    try:
        if _accel_saved is None:
            if _accel_settings_path.exists():
                _accel_settings_path.unlink()
        else:
            _accel_settings_path.write_text(_accel_saved, encoding="utf-8")
    except OSError:
        pass


# The schemes are offered from the left sidebar's 加速 group; the menu itself
# is built without exec() so it can be inspected here.
assert window.accel_button.text() == "GPU 加速…", window.accel_button.text()
assert sidebar.isAncestorOf(window.accel_button), "accel entry left the left sidebar"
assert right_side.isAncestorOf(window.accel_scheme_label), "accel state left the panel"

report = window.gpu_report()
assert isinstance(report, gpu_info.GpuReport)
if report.adapters:
    assert not report.errors, report.errors
    # A hybrid machine must expose both kinds separately.
    kinds = {a.kind for a in report.adapters}
    assert kinds & {gpu_info.DISCRETE, gpu_info.INTEGRATED}, report.headline()
    print(f"[16a] detected: {report.headline()} "
          f"(hybrid={report.is_hybrid}, discrete={report.has_discrete}, "
          f"integrated={report.has_integrated})")
else:
    print("[16a] no adapters enumerated in this environment; shapes verified")

menu = window.build_accel_menu()
actions = menu.actions()
labels = [a.text() for a in actions]
# Both header rows plus every scheme plus the details entry.
assert any(t.startswith("检测到：") for t in labels), labels
assert any(t.startswith("类型：") for t in labels), labels
assert menu.actions()[-1].text() == "查看显卡详情…", labels

scheme_actions = [a for a in actions if a.data() in gpu_accel.SCHEME_BY_ID]
assert len(scheme_actions) == len(gpu_accel.SCHEMES), labels
for action in scheme_actions:
    scheme = gpu_accel.get_scheme(action.data())
    available, reason = gpu_accel.scheme_availability(scheme, report)
    assert action.isCheckable(), action.text()
    # An unavailable scheme is shown (so the reason is discoverable) but not
    # selectable — offering 独显优先 on a machine without one would be a lie.
    assert action.isEnabled() == available, (action.text(), reason)
    if not available:
        assert "不可用" in action.text() and reason in action.text(), action.text()
    else:
        assert gpu_accel.describe_effect(scheme.id, report) in action.text(), action.text()
checked = [a.data() for a in scheme_actions if a.isChecked()]
assert checked == [gpu_accel.effective_scheme_id()], (
    f"exactly the running scheme must be ticked, got {checked}"
)
print(f"[16b] menu offers {len(scheme_actions)} schemes; "
      f"'{checked[0]}' is ticked; unavailable ones are shown but disabled")

# The panel must name the same scheme the menu ticks.
panel = window.accel_scheme_label.text()
assert gpu_accel.get_scheme(gpu_accel.effective_scheme_id()).label in panel, panel
assert window.accel_gpu_label.text() == report.headline(), window.accel_gpu_label.text()
print(f"[16c] right panel reads: {panel!r} / {window.accel_gpu_label.text()!r}")

# Selecting a scheme must persist it and re-tick the menu — without exec().
target = next(
    (
        s.id
        for s in gpu_accel.SCHEMES
        if s.id != gpu_accel.effective_scheme_id()
        and gpu_accel.scheme_availability(s, report)[0]
    ),
    None,
)
if target is not None:
    before = gpu_accel.current_scheme_id()
    window._select_accel_scheme(target)
    QAPP.processEvents()
    assert gpu_accel.current_scheme_id() == target, gpu_accel.current_scheme_id()
    assert gpu_accel.effective_scheme_id() == target
    reticked = [a.data() for a in window.build_accel_menu().actions() if a.isChecked()]
    assert reticked == [target], reticked
    # Switching away from the compatibility scheme must drop its forced shell.
    if gpu_accel.get_scheme(target).ui_shell is None:
        assert os.environ.get(gpu_accel.ENV_UI) is None, os.environ.get(gpu_accel.ENV_UI)
    # Put the user's setting back the way we found it.
    window._select_accel_scheme(before)
    QAPP.processEvents()
    assert gpu_accel.current_scheme_id() == before, gpu_accel.current_scheme_id()
    print(f"[16d] selecting '{target}' persisted + re-ticked; restored to '{before}'")

# The compatibility scheme must route to the CPU shell, not software OpenGL.
# Qt's bundled opengl32sw is Mesa 11.2 / GLSL 1.30 and VisPy cannot use it.
assert gpu_accel.get_scheme("software").environment()[gpu_accel.ENV_UI] == "tk"
assert gpu_accel.force_software_opengl_requested() is False
print("[16e] compatibility scheme targets the CPU (Tk) shell, not software OpenGL")

# --- 17. clean shutdown ---
_restore_accel_settings()
assert gpu_accel.current_scheme_id() == (
    json.loads(_accel_saved)["accel_scheme"] if _accel_saved else "auto"
), "the test must leave the stored scheme as it found it"
window.close()
QAPP.processEvents()
print("GPU UI SMOKE TEST PASSED")
