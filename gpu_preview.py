"""GPU preview engine for Photo Culler (PySide6 + VisPy/OpenGL).

Adopted from the validated `gpu_preview_prototype.py`:

- ``ZoomPlan``: pure zoom-target math (clamps to [fit, max magnification]).
- ``SmoothPanZoomCamera``: cursor-anchored wheel zoom with exponential
  easing, plus smooth keyboard zoom.
- ``PhotoStage``: a QWidget embedding a VisPy ``SceneCanvas``. The decoded
  photo is uploaded once as a GPU texture; pan / zoom / interpolation run on
  the GPU. This replaces the CPU crop+resample pipeline used by the Tk
  shell (``preview_engine`` / ``resample_backend``).

Zoom semantics follow the rest of the app: ``pixel_zoom()`` is display
pixels per *original* pixel (1.0 == 100%), fit never upscales beyond 100%,
and zoom is capped at ``ZOOM_MAX_PIXEL_SCALE``.

VisPy has no Tk backend, so this engine only lives in the PySide6 shell
(``qt_ui.py``). Two lessons from the prototype are kept here:

1. After uploading a new photo, mark the interpolation lookup dirty
   (older VisPy releases otherwise keep the previous texture shape and the
   first frame renders blurred).
2. Do not query the OpenGL context before the widget is shown; callers
   must invoke :meth:`PhotoStage.initialize_gpu_info` after ``show()``.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Callable

os.environ.setdefault("QT_API", "pyside6")

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget
from vispy import app, scene
from vispy.visuals.transforms import STTransform

from config import ZOOM_MAX_PIXEL_SCALE


def _decode_gl_string(value: bytes | None) -> str:
    return (value or b"").decode("utf-8", "replace") or "unknown"


def query_gpu_info(canvas: scene.SceneCanvas) -> dict[str, Any]:
    """Return information from the actual OpenGL context used by VisPy."""

    canvas.set_current()
    from OpenGL import GL

    return {
        "backend": canvas.app.backend_name,
        "vendor": _decode_gl_string(GL.glGetString(GL.GL_VENDOR)),
        "renderer": _decode_gl_string(GL.glGetString(GL.GL_RENDERER)),
        "opengl": _decode_gl_string(GL.glGetString(GL.GL_VERSION)),
        "max_texture_size": int(GL.glGetIntegerv(GL.GL_MAX_TEXTURE_SIZE)),
    }


@dataclass
class ZoomPlan:
    """Pure zoom-target calculation, kept separate for unit testing.

    Bounds are expressed as *live* rect widths rather than a latched
    magnification ratio. A ratio would bake in the viewport size that
    happened to be current when the photo was fitted, so resizing the
    window (or fitting before the first layout pass) would silently move
    the zoom cap. Both widths come from the caller's current device size.
    """

    max_width: float
    min_width: float

    def clamp_target_width(self, width: float) -> float:
        return min(self.max_width, max(self.min_width, width))

    def wheel_target_width(
        self,
        current_width: float,
        pending_log_factor: float,
        wheel_delta: float,
        steps_per_double: float = 5.0,
    ) -> float:
        wheel_delta = max(-4.0, min(4.0, float(wheel_delta)))
        delta_log = -wheel_delta * math.log(2.0) / steps_per_double
        requested = current_width * math.exp(pending_log_factor + delta_log)
        return self.clamp_target_width(requested)


class SmoothPanZoomCamera(scene.PanZoomCamera):
    """Pan/zoom camera that eases wheel input while keeping the cursor anchored.

    The camera never latches a zoom ratio: it asks the stage for the current
    fit width and device width every time it needs the bounds, so a window
    resize or monitor change cannot leave a stale cap behind.
    """

    def __init__(
        self,
        *,
        easing_seconds: float = 0.065,
        pixel_scale_limit: float = ZOOM_MAX_PIXEL_SCALE,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.easing_seconds = easing_seconds
        self.pixel_scale_limit = pixel_scale_limit
        self.fit_width: float | None = None
        # Set by PhotoStage: returns the live viewport width in device px.
        self.device_width_provider: Callable[[], float] | None = None
        self._pending_log_factor = 0.0
        self._anchor: tuple[float, float] | None = None
        self._last_tick = 0.0
        self._dragging = False
        self._last_input_at = 0.0
        self._timer = app.Timer(interval=1 / 120, connect=self._tick, start=False)

    # --- bounds --------------------------------------------------------------

    def _device_width(self) -> float:
        if self.device_width_provider is None:
            return float(self.rect.width)
        try:
            return max(1.0, float(self.device_width_provider()))
        except Exception:
            return float(self.rect.width)

    def plan(self) -> ZoomPlan | None:
        """Live zoom bounds; ``None`` until a photo has been fitted."""

        if not self.fit_width:
            return None
        min_width = max(1.0, self._device_width() / self.pixel_scale_limit)
        # Never zoom out past fit, and never ask for less than one texel.
        return ZoomPlan(
            max_width=max(float(self.fit_width), min_width),
            min_width=min_width,
        )

    @property
    def magnification(self) -> float:
        if not self.fit_width or self.rect.width <= 0:
            return 1.0
        return self.fit_width / self.rect.width

    @property
    def pixel_zoom(self) -> float:
        if self.rect.width <= 0:
            return 1.0
        return self._device_width() / self.rect.width

    def busy(self) -> bool:
        """True while a zoom animation or a drag is still in flight."""

        if self._dragging:
            return True
        if self._timer.running:
            return True
        return abs(self._pending_log_factor) > 1e-5

    def seconds_since_input(self) -> float:
        """Seconds since the last zoom/pan input from the user.

        ``busy()`` alone is not enough to decide that a gesture is over: once
        zoom hits its cap, further wheel notches change nothing, so the camera
        reads as idle while the user is still actively scrolling. Anything that
        costs tens of milliseconds to apply must also wait for this.
        """

        if self._last_input_at <= 0.0:
            return float("inf")
        return max(0.0, _perf_counter() - self._last_input_at)

    def mark_input(self) -> None:
        self._last_input_at = _perf_counter()

    def stop_animation(self) -> None:
        self._pending_log_factor = 0.0
        self._anchor = None
        if self._timer.running:
            self._timer.stop()

    def remember_fit(self) -> None:
        self.stop_animation()
        self.fit_width = float(self.rect.width)

    def smooth_zoom_factor(
        self,
        factor: float,
        center: tuple[float, float] | None = None,
    ) -> None:
        if factor <= 0:
            return
        plan = self.plan()
        if plan is None or self.rect.width <= 0:
            return
        requested = self.rect.width * math.exp(self._pending_log_factor) * factor
        target_width = plan.clamp_target_width(requested)
        self._pending_log_factor = math.log(target_width / self.rect.width)
        self._anchor = center or tuple(self.center[:2])
        self._last_tick = _perf_counter()
        self.mark_input()
        if abs(self._pending_log_factor) > 1e-5 and not self._timer.running:
            self._timer.start()

    def _queue_wheel_zoom(self, wheel_delta: float, pos: Any) -> None:
        plan = self.plan()
        if plan is None or self.rect.width <= 0:
            return
        try:
            mapped = self._scene_transform.imap(pos)
            anchor = (float(mapped[0]), float(mapped[1]))
        except Exception:
            anchor = tuple(self.center[:2])

        target_width = plan.wheel_target_width(
            self.rect.width, self._pending_log_factor, wheel_delta
        )
        self._pending_log_factor = math.log(target_width / self.rect.width)
        self._anchor = anchor
        self._last_tick = _perf_counter()
        self.mark_input()
        if abs(self._pending_log_factor) > 1e-5 and not self._timer.running:
            self._timer.start()

    def _tick(self, _event: Any = None) -> None:
        if abs(self._pending_log_factor) < 0.00035 or self._anchor is None:
            if self._anchor is not None and self._pending_log_factor:
                super().zoom(math.exp(self._pending_log_factor), self._anchor)
            self.stop_animation()
            return

        now = _perf_counter()
        dt = min(0.05, max(1 / 240, now - self._last_tick))
        self._last_tick = now
        fraction = 1.0 - math.exp(-dt / self.easing_seconds)
        step = self._pending_log_factor * fraction
        super().zoom(math.exp(step), self._anchor)
        self._pending_log_factor -= step

    def viewbox_mouse_event(self, event: Any) -> None:
        if event.handled or not self.interactive:
            return
        if event.type == "mouse_wheel":
            self._queue_wheel_zoom(float(event.delta[1]), event.pos)
            event.handled = True
            return
        if event.type in ("mouse_press", "mouse_move", "mouse_release"):
            self.mark_input()
        if event.type == "mouse_press" and event.button in (1, 2):
            self.stop_animation()
            self._dragging = True
        elif event.type == "mouse_release":
            self._dragging = False
        super().viewbox_mouse_event(event)


def _perf_counter() -> float:
    from time import perf_counter

    return perf_counter()


class PhotoStage(QWidget):
    """Central photo viewport: one GPU texture + smooth pan/zoom camera."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(1, 1)
        self.setStyleSheet("background-color: #0E1014;")

        self.canvas = scene.SceneCanvas(
            keys=None, show=False, bgcolor="#0E1014", vsync=True
        )
        self.view = self.canvas.central_widget.add_view()
        self.camera = SmoothPanZoomCamera(aspect=1)
        self.camera.flip = (False, True, False)
        self.camera.device_width_provider = lambda: self._device_size()[0]
        self.view.camera = self.camera
        self.visual = scene.visuals.Image(
            None,
            interpolation="cubic",
            method="subdivide",
            parent=self.view.scene,
        )
        # The natural fit depends on the viewport size, so it has to be
        # recomputed whenever the widget is resized or the monitor DPI
        # changes; otherwise the zoom bounds drift away from reality.
        self.canvas.events.resize.connect(self._on_canvas_resize)

        native = self.canvas.native
        native.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(native)

        self.original_size = (0, 0)
        self.texture_size = (0, 0)
        self._full = False
        self._fade_job: QTimer | None = None
        self.gpu_info: dict[str, Any] | None = None

    # --- lifecycle -------------------------------------------------------

    def initialize_gpu_info(self) -> dict[str, Any] | None:
        """Query the on-screen OpenGL context. Call only after show()."""

        if self.gpu_info is not None:
            return self.gpu_info
        try:
            self.gpu_info = query_gpu_info(self.canvas)
        except Exception:
            self.gpu_info = None
        return self.gpu_info

    def describe_backend(self) -> str:
        info = self.gpu_info
        if not info:
            return "GPU 预览（上下文未就绪）"
        return f"GPU 预览：{info['renderer']}"

    def max_texture_size(self) -> int:
        info = self.gpu_info
        if not info:
            return 0
        return int(info.get("max_texture_size") or 0)

    def shutdown(self) -> None:
        try:
            self.camera.stop_animation()
        except Exception:
            pass
        try:
            self.canvas.close()
        except Exception:
            pass

    # --- data ------------------------------------------------------------

    @property
    def has_image(self) -> bool:
        return self.original_size[0] > 0 and self.original_size[1] > 0

    @property
    def using_full(self) -> bool:
        return self._full

    def set_image(
        self,
        pixels: np.ndarray,
        original_size: tuple[int, int],
        *,
        full: bool,
    ) -> None:
        """Upload an RGB array as the displayed texture.

        Scene coordinates are always *original* pixels; when ``pixels`` is a
        downsampled preview, a scale transform maps it into place, so the
        camera view (and zoom math) survives preview→full swaps untouched.
        """

        height, width = pixels.shape[:2]
        orig_w, orig_h = original_size
        scale_x = max(1e-6, orig_w / max(1, width))
        scale_y = max(1e-6, orig_h / max(1, height))
        self.visual.set_data(pixels)
        # Prototype lesson: older VisPy keeps a stale cubic lookup shape.
        if hasattr(self.visual, "_need_interpolation_update"):
            self.visual._need_interpolation_update = True
        self.visual.transform = STTransform(scale=(scale_x, scale_y))
        self.visual.visible = True
        self.original_size = (int(orig_w), int(orig_h))
        self.texture_size = (int(width), int(height))
        self._full = bool(full)

    def clear_image(self) -> None:
        self.visual.visible = False
        self.original_size = (0, 0)
        self.texture_size = (0, 0)
        self._full = False
        self.camera.stop_animation()

    def fade_in(self, steps: tuple[float, ...] = (0.35, 0.7, 1.0), interval_ms: int = 28) -> None:
        """Brief opacity ramp so photo switches do not feel like a hard cut."""
        self._cancel_fade()
        visual = self.visual
        visual.opacity = 0.0

        def _advance(remaining: list[float]) -> None:
            self._fade_job = None
            if not remaining:
                visual.opacity = 1.0
                self.canvas.update()
                return
            visual.opacity = remaining.pop(0)
            self.canvas.update()
            if remaining:
                timer = QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(lambda: _advance(remaining))
                self._fade_job = timer
                timer.start(interval_ms)
            else:
                timer = QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(lambda: _advance([]))
                self._fade_job = timer
                timer.start(interval_ms)

        _advance(list(steps))

    def _cancel_fade(self) -> None:
        if self._fade_job is not None:
            self._fade_job.stop()
            self._fade_job = None
        try:
            self.visual.opacity = 1.0
        except Exception:
            pass

    # --- zoom ------------------------------------------------------------

    def _device_size(self) -> tuple[float, float]:
        size = self.canvas.physical_size
        return max(1.0, float(size[0])), max(1.0, float(size[1]))

    def pixel_zoom(self) -> float:
        """Display pixels per original-pixel (1.0 == 100%)."""

        if not self.has_image:
            return 1.0
        return self.camera.pixel_zoom

    def fit_pixel_scale(self) -> float:
        fit_width = self.camera.fit_width
        if not fit_width:
            return 1.0
        device_w, _ = self._device_size()
        return device_w / fit_width

    def preview_native_scale(self) -> float | None:
        """``pixel_zoom`` at which the currently uploaded texture is 1:1.

        Below this the GPU is *minifying* the texture, so there is nothing to
        gain from a higher-resolution upload. Only past it is the texture
        being magnified and a sharper source actually adds detail.
        """

        if not self.has_image:
            return None
        tex_w, tex_h = self.texture_size
        if tex_w <= 0 or tex_h <= 0:
            return None
        orig_w, orig_h = self.original_size
        return min(tex_w / orig_w, tex_h / orig_h)

    def camera_busy(self) -> bool:
        """True while a zoom animation or a pan drag is still in flight."""

        return self.camera.busy()

    def seconds_since_zoom_input(self) -> float:
        return self.camera.seconds_since_input()

    def _set_natural_fit(self) -> None:
        """Move the camera to the un-zoomed fit rect for the current viewport."""

        orig_w, orig_h = self.original_size
        self.camera.set_range(x=(0, orig_w), y=(0, orig_h), margin=0)
        if self.camera.pixel_zoom > 1.0:
            # Small photo: fitting would magnify past 100%; hold at 100%.
            device_w, device_h = self._device_size()
            center_x, center_y = orig_w / 2.0, orig_h / 2.0
            self.camera.rect = (
                center_x - device_w / 2.0,
                center_y - device_h / 2.0,
                device_w,
                device_h,
            )

    def _set_rect_around(self, center: tuple[float, float], width: float) -> None:
        """Resize the view rect about ``center``, preserving its aspect."""

        rect = self.camera.rect
        if rect.width <= 0:
            return
        ratio = rect.height / rect.width
        height = width * ratio
        self.camera.rect = (
            center[0] - width / 2.0,
            center[1] - height / 2.0,
            width,
            height,
        )

    def _on_canvas_resize(self, _event: Any = None) -> None:
        """Re-derive the fit and re-clamp zoom after a viewport size change."""

        if not self.has_image:
            return
        at_fit = self.camera.magnification <= 1.0 + 1e-3
        keep_zoom = self.camera.pixel_zoom
        keep_center = (float(self.camera.center[0]), float(self.camera.center[1]))

        self.camera.stop_animation()
        self._set_natural_fit()
        # Refresh fit_width for the *new* device size before deriving bounds.
        self.camera.fit_width = float(self.camera.rect.width)

        if not at_fit:
            plan = self.camera.plan()
            if plan is not None:
                device_w, _ = self._device_size()
                self._set_rect_around(
                    keep_center, plan.clamp_target_width(device_w / max(keep_zoom, 1e-9))
                )
        self.canvas.update()

    def fit(self) -> None:
        """Fit the whole photo, never upscaling beyond 100% pixels."""

        if not self.has_image:
            return
        self.camera.stop_animation()
        self._set_natural_fit()
        self.camera.remember_fit()
        self.canvas.update()

    def zoom_actual(self) -> None:
        """Smoothly zoom to 100% (one display pixel per original pixel)."""

        camera = self.camera
        if not camera.fit_width or not self.has_image:
            return
        device_w, _ = self._device_size()
        # pixel_zoom == 1.0 means rect.width == device width.
        factor = device_w / (
            camera.rect.width * math.exp(camera._pending_log_factor)
        )
        camera.smooth_zoom_factor(factor)

    def zoom_step(self, direction: int) -> None:
        factor = 1.0 / 1.25 if direction > 0 else 1.25
        self.camera.smooth_zoom_factor(factor)
