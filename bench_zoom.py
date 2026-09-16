"""Zoom smoothness benchmark for the GPU preview engine.

Measures real frame pacing while simulating a wheel-zoom gesture, and times
the expensive on-demand full-resolution upload. Run it before/after changing
the zoom path so improvements are numbers, not opinions.

    python bench_zoom.py

Reports, for each phase:
    frames, median / p95 / max frame interval, jank ratio (> 2 vsyncs),
    and effective FPS.
"""

from __future__ import annotations

import gc
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

QAPP = QApplication.instance() or QApplication(["bench-zoom"])
QAPP.setStyle("Fusion")

from vispy import app as vispy_app  # noqa: E402

vispy_app.use_app("pyside6")

import qt_ui  # noqa: E402
from config import (  # noqa: E402
    PREVIEW_CACHE_LONG_EDGE,
    ZOOM_FULLRES_MARGIN,
    ZOOM_PREVIEW_NATIVE_MARGIN,
)

VSYNC_MS = 1000.0 / 60.0

# A serious full-frame photo: 24 MP, so the preview is a real downsample.
ORIGINAL_SIZE = (6000, 4000)


# --- harness ---------------------------------------------------------------


class FrameRecorder:
    """Timestamps every canvas draw so we can look at pacing, not averages."""

    def __init__(self, canvas) -> None:
        self.stamps: list[float] = []
        self._handle = None
        try:
            canvas.events.draw.connect(self._on_draw)
            self._handle = canvas.events.draw
        except Exception as exc:  # pragma: no cover - VisPy API guard
            print(f"  ! cannot hook draw events: {exc}")

    def _on_draw(self, _event=None) -> None:
        self.stamps.append(time.perf_counter())

    def reset(self) -> None:
        self.stamps.clear()

    def report(self, label: str, idle_tail: bool = False) -> dict:
        """Summarise pacing.

        ``idle_tail`` is for phases that deliberately end with the wheel quiet
        (the A/B runs and the settle tail). Only draws are timestamped, so an
        idle period shows up as one enormous "interval"; counting that as a
        dropped frame would be wrong, so it is reported separately.
        """

        if len(self.stamps) < 3:
            print(f"{label}: only {len(self.stamps)} frames captured — skipping")
            return {}
        intervals = [
            (self.stamps[i] - self.stamps[i - 1]) * 1000.0
            for i in range(1, len(self.stamps))
        ]
        # Drop the gap before the first frame so it does not skew the stats.
        intervals = intervals[1:] or intervals
        idle_gaps: list[float] = []
        if idle_tail:
            idle_gaps = [v for v in intervals if v > VSYNC_MS * 6]
            intervals = [v for v in intervals if v <= VSYNC_MS * 6]
        if not intervals:
            print(f"{label}: no active frames — skipping")
            return {}
        intervals.sort()
        median = statistics.median(intervals)
        p95 = intervals[min(len(intervals) - 1, int(len(intervals) * 0.95))]
        worst = intervals[-1]
        janky = sum(1 for v in intervals if v > VSYNC_MS * 2)
        print(
            f"{label}: {len(self.stamps)} frames  "
            f"median {median:6.1f} ms  p95 {p95:6.1f} ms  max {worst:7.1f} ms  "
            f"jank {janky}/{len(intervals)}  "
            f"~{1000.0 / max(median, 0.001):5.1f} fps"
        )
        if idle_gaps:
            print(f"  + {len(idle_gaps)} idle gap(s) of "
                  f"{', '.join(f'{v:.0f} ms' for v in sorted(idle_gaps, reverse=True))}"
                  f" — the wheel was quiet, not a dropped frame")
        return {
            "frames": len(self.stamps),
            "median": median,
            "p95": p95,
            "max": worst,
            "jank": janky,
            "samples": len(intervals),
        }


class GcWatch:
    """Times every garbage collection so long frames can be attributed."""

    def __init__(self) -> None:
        self.events: list[tuple[int, float]] = []
        self._t0: float | None = None
        self._gen = 0
        gc.callbacks.append(self._cb)

    def _cb(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._t0 = time.perf_counter()
            self._gen = int(info.get("generation", 0))
        elif self._t0 is not None:
            self.events.append((self._gen, (time.perf_counter() - self._t0) * 1000.0))
            self._t0 = None

    def reset(self) -> None:
        self.events.clear()

    def report(self, label: str) -> float:
        if not self.events:
            print(f"  GC during {label}: none")
            return 0.0
        total = sum(d for _g, d in self.events)
        worst = sorted(self.events, key=lambda e: -e[1])[:3]
        print(
            f"  GC during {label}: {len(self.events)} collections, "
            f"{total:6.1f} ms total, worst " +
            ", ".join(f"gen{g} {d:.1f} ms" for g, d in worst)
        )
        return total


def pump(seconds: float, slice_s: float = 0.002) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        QAPP.processEvents()
        time.sleep(slice_s)


def wait_for(condition, timeout: float = 20.0, what: str = "condition") -> bool:
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        QAPP.processEvents()
        time.sleep(0.01)
        if condition():
            return True
    print(f"  ! timed out waiting for {what}")
    return False


def make_wheel(pos_x: float, pos_y: float, delta: int) -> QWheelEvent:
    point = QPointF(pos_x, pos_y)
    return QWheelEvent(
        point,
        point,
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


class WheelDriver:
    """Feeds wheel notches through the real Qt event path.

    Delivery is probed once by spying on the camera's zoom entry point. The
    obvious test -- "did ``camera.rect.width`` change?" -- cannot work here:
    the camera only records a pending log factor and eases towards it on a
    timer, so ``rect.width`` is unchanged when ``sendEvent`` returns. A driver
    that concludes "not delivered" and then also calls the camera applies
    every notch *twice*, which inflates every number this file reports.
    """

    def __init__(self, window) -> None:
        self.window = window
        self.camera = window.stage.camera
        self.native = window.stage.canvas.native
        self.delivered = self._probe_delivery()
        if self.delivered:
            print("  wheel driver: Qt events reach the camera (one notch per call)")
        else:
            print("  wheel driver: Qt wheel events are NOT delivered to the "
                  "canvas; driving the camera directly instead")

    def _probe_delivery(self) -> bool:
        calls: list[int] = []
        real = self.camera._queue_wheel_zoom

        def spy(delta, pos):
            calls.append(1)
            return real(delta, pos)

        self.camera._queue_wheel_zoom = spy
        try:
            size = self.native.size()
            QAPP.sendEvent(
                self.native,
                make_wheel(
                    max(2.0, size.width() / 2.0), max(2.0, size.height() / 2.0), 120
                ),
            )
            QAPP.processEvents()
        finally:
            self.camera._queue_wheel_zoom = real
        return bool(calls)

    def notch(self, delta: int = 120) -> None:
        size = self.native.size()
        x, y = max(2.0, size.width() / 2.0), max(2.0, size.height() / 2.0)
        if self.delivered:
            QAPP.sendEvent(self.native, make_wheel(x, y, delta))
        else:
            # The camera's own entry point, so the benchmark still exercises
            # the real easing path.
            self.camera._queue_wheel_zoom(float(delta) / 120.0, (x, y))


def gesture(driver: WheelDriver, recorder: FrameRecorder, notches: int,
            spacing_s: float, tail_s: float, label: str,
            gc_watch: "GcWatch | None" = None) -> dict:
    recorder.reset()
    if gc_watch is not None:
        gc_watch.reset()
    pump(0.05)  # let the loop settle before we start timing
    recorder.reset()
    if gc_watch is not None:
        gc_watch.reset()
    start = time.perf_counter()
    for i in range(notches):
        target = start + i * spacing_s
        while time.perf_counter() < target:
            QAPP.processEvents()
            time.sleep(0.002)
        driver.notch()
    pump(tail_s)
    stats = recorder.report(
        f"{label} ({notches} notches @ {int(spacing_s*1000)} ms)",
        idle_tail=tail_s >= 0.5,
    )
    if gc_watch is not None:
        gc_watch.report(label)
    return stats


# --- setup -----------------------------------------------------------------


tmp = Path(tempfile.mkdtemp(prefix="pc_bench_"))
photo_dir = tmp / "photos"
photo_dir.mkdir()
big = photo_dir / "HDR_0001.jpg"
Image.new("RGB", ORIGINAL_SIZE, (90, 60, 40)).save(big, quality=88)
Image.new("RGB", (4000, 3000), (40, 80, 60)).save(photo_dir / "HDR_0002.jpg", quality=88)


class _FakeMessageBox:
    class StandardButton:
        Yes = 0x4000
        No = 0x10000

    question = staticmethod(lambda *a, **k: 0x4000)
    information = staticmethod(lambda *a, **k: None)
    warning = staticmethod(lambda *a, **k: None)
    critical = staticmethod(lambda *a, **k: None)


class _FakeFileDialog:
    last_directory = None
    getExistingDirectory = staticmethod(lambda *a, **k: str(_FakeFileDialog.last_directory))


qt_ui.QMessageBox = _FakeMessageBox
qt_ui.QFileDialog = _FakeFileDialog

window = qt_ui.PhotoCullerWindow()
window.auto_open_enabled = False
window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
window.show()
QAPP.processEvents()
window.initialize_after_show()
QAPP.processEvents()

info = window.stage.gpu_info or {}
print("=" * 78)
print(f"GPU          : {info.get('renderer', 'unknown')}")
print(f"OpenGL       : {info.get('opengl', 'unknown')}")
print(f"max texture  : {info.get('max_texture_size', '?')}")
print(f"original     : {ORIGINAL_SIZE[0]}x{ORIGINAL_SIZE[1]}  "
      f"({ORIGINAL_SIZE[0]*ORIGINAL_SIZE[1]/1e6:.1f} MP)")
print(f"preview edge : {PREVIEW_CACHE_LONG_EDGE} px")
print(f"viewport     : {window.stage._device_size()}")
print("=" * 78)

window._open_folder_path(photo_dir)
wait_for(lambda: window.all_items, what="scan")
wait_for(lambda: window.stage.has_image, what="preview upload")
pump(0.8)  # let the filmstrip / preload noise die down

stage = window.stage
preview_pixels = window._preview_pil.size if window._preview_pil else None
print(f"preview decoded: {preview_pixels}  original: {window._original_size}")
print(f"fit pixel_zoom : {stage.fit_pixel_scale():.4f}")


def _diag(tag: str) -> None:
    c = stage.camera
    plan = c.plan()
    print(
        f"  [{tag}] canvas.size={window.stage.canvas.size} "
        f"physical={window.stage.canvas.physical_size} "
        f"rect.width={c.rect.width:.1f} fit_width={c.fit_width} "
        f"bounds=[{plan.min_width:.1f}, {plan.max_width:.1f}] "
        f"pixel_zoom={stage.pixel_zoom():.4f} fit_scale={stage.fit_pixel_scale():.4f} "
        f"busy={c.busy()}"
    )


print("--- viewport / fit internals ---")
_diag("after preview upload")
native_scale = stage.preview_native_scale()
if preview_pixels:
    print(f"preview 1:1 zoom: {native_scale:.4f}  "
          f"(= {native_scale / max(stage.fit_pixel_scale(), 1e-9):.2f}x fit)")
print(f"full-res trigger : pixel_zoom > native * {ZOOM_PREVIEW_NATIVE_MARGIN:.2f} "
      f"= {native_scale * ZOOM_PREVIEW_NATIVE_MARGIN:.4f}")
print(f"  (old behaviour fired at fit * {1 + ZOOM_FULLRES_MARGIN:.2f} "
      f"= {stage.fit_pixel_scale() * (1 + ZOOM_FULLRES_MARGIN):.4f})")
print("-" * 78)

# --- cost breakdown: where do the milliseconds actually go? ---------------
import numpy as np  # noqa: E402

from imaging import decode_photo  # noqa: E402

marks = []
t = time.perf_counter()
full_pil = decode_photo(big, thumbnail=False, full_resolution=True)
marks.append(("decode full-res (background thread)", time.perf_counter() - t))

t = time.perf_counter()
full_arr = np.ascontiguousarray(np.asarray(full_pil, dtype=np.uint8))
marks.append(("PIL --array_interface--> uint8 array (GIL released, off-thread in the app)",
              time.perf_counter() - t))

t = time.perf_counter()
stage.visual.set_data(full_arr)
stage.canvas.update()
QAPP.processEvents()
marks.append(("set_data + glTexImage2D + first draw (UI thread)", time.perf_counter() - t))

print("cost breakdown for a 24 MP full-resolution upload:")
for label, seconds in marks:
    print(f"  {label:<72s} {seconds * 1000:8.1f} ms")
full_arr_mb = ORIGINAL_SIZE[0] * ORIGINAL_SIZE[1] * 3 / 1e6
print(f"  texture payload: {full_arr.shape} = {full_arr.nbytes / 1e6:.1f} MB")
print(f"  the app does the 2nd step on the decode thread, so the UI thread "
      f"only pays the 3rd (~{marks[2][1] * 1000:.0f} ms)")
print("-" * 78)
del full_arr, full_pil

# restore the preview texture
window._show_current(center=True)
pump(0.5)
print("-" * 78)

driver = WheelDriver(window)
# The delivery probe in the constructor sends one throwaway notch.
window.zoom_fit()
pump(0.3)
recorder = FrameRecorder(stage.canvas)
gc_watch = GcWatch()
print(f"gc frozen objects: {gc.get_freeze_count()}   "
      f"thresholds: {gc.get_threshold()}")

# --- phase 0: is the residual jank caused by the full-res path itself? ----
# Same gesture twice: once with the full-resolution upgrade disabled, once
# with it live. Anything that only appears in the second run is the cost of
# decoding/allocating the 72 MB full-resolution buffers in the background.
print("A/B: active gesture with the full-res upgrade OFF, then ON")
print("-" * 78)
window.zoom_fit()
pump(0.4)
_real_request = window._maybe_request_full
window._maybe_request_full = lambda: None  # keep the preview texture only
gesture(driver, recorder, notches=10, spacing_s=0.035, tail_s=1.0,
        label="A) gesture at fit, full-res OFF", gc_watch=gc_watch)
print(f"  -> using_full={stage.using_full} pixel_zoom={stage.pixel_zoom():.4f}")
window._maybe_request_full = _real_request
window.zoom_fit()
pump(0.4)
gesture(driver, recorder, notches=10, spacing_s=0.035, tail_s=1.0,
        label="B) gesture at fit, full-res ON ", gc_watch=gc_watch)
print(f"  -> using_full={stage.using_full} pixel_zoom={stage.pixel_zoom():.4f}")
print("-" * 78)
window.zoom_fit()
pump(0.4)

# --- phase 1: a normal zoom gesture right at fit ---------------------------
gesture(driver, recorder, notches=10, spacing_s=0.035, tail_s=0.5,
        label="gesture at fit", gc_watch=gc_watch)
print(f"  -> pixel_zoom now {stage.pixel_zoom():.4f}  using_full={stage.using_full}")

# --- phase 2: zoom in far enough to trigger the full-resolution swap ------
# The heavy texture swap must NOT land inside the active window; it is
# allowed to happen once the wheel goes quiet, so the two are measured apart.
window.zoom_fit()
pump(0.3)
print("-" * 78)
recorder.reset()
gc_watch.reset()
swap_at = None

for step in range(24):
    driver.notch()
    pump(0.045)
    if stage.using_full and swap_at is None:
        swap_at = f"notch {step + 1}"
        _diag("at full-res swap")
stats_active = recorder.report("ACTIVE window (24 notches @ 45 ms)")
gc_watch.report("ACTIVE window")
if swap_at is not None:
    print(f"  !! the {full_arr_mb} MB texture swap landed INSIDE the active "
          f"window at {swap_at}")
else:
    print("  the texture swap did not land inside the active window (good)")
print(f"  -> pixel_zoom {stage.pixel_zoom():.4f}  using_full={stage.using_full}")

# --- phase 2b: the settle tail, where the swap is supposed to happen ------
recorder.reset()
gc_watch.reset()
pump(1.5)
stats_tail = recorder.report("settle tail (wheel quiet)", idle_tail=True)
gc_watch.report("settle tail")
if stage.using_full and swap_at is None:
    swap_at = "settle tail"
    print("  the texture swap landed during the settle tail (as intended)")
else:
    print(f"  using_full after the tail: {stage.using_full}")
_diag("after deep zoom")

# --- phase 3: isolate the full-res adoption cost --------------------------
print("-" * 78)
window.zoom_fit()
pump(0.3)
for _ in range(6):
    driver.notch()
    pump(0.05)
# Force the request path directly and time the moment the upload lands.
window._maybe_request_full()
if window._loading_full_path_id:
    t0 = time.perf_counter()
    wait_for(lambda: window._using_full, timeout=15.0, what="full-res upload")
    print(f"full-res request -> texture active: {(time.perf_counter() - t0)*1000:.0f} ms "
          f"(includes background decode, not just the upload)")

# --- phase 4: pan smoothness on the heaviest texture ----------------------
print("-" * 78)
camera = stage.camera
pan_times: list[float] = []
recorder.reset()
base = float(camera.rect.left)
for i in range(60):
    step_start = time.perf_counter()
    camera.rect = (base + i * 3.0, camera.rect.bottom, camera.rect.width, camera.rect.height)
    stage.canvas.update()
    QAPP.processEvents()
    pan_times.append((time.perf_counter() - step_start) * 1000.0)
QAPP.processEvents()
pan_times.sort()
print(f"pan step (explicit update+processEvents): {len(pan_times)} steps  "
      f"median {statistics.median(pan_times):5.2f} ms  max {pan_times[-1]:5.2f} ms")
recorder.report("pan phase")

# --- cleanup ---------------------------------------------------------------
window.close()
QAPP.processEvents()
print("=" * 78)
print("benchmark finished")
