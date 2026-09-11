"""Preview geometry and background frame rendering."""

from __future__ import annotations

import math
import queue
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock

from PIL import Image

from config import (
    PREVIEW_OVERSCAN,
    PREVIEW_OVERSCAN_MAX_PX,
)


@dataclass(frozen=True)
class PreviewGeometry:
    """The source region and on-canvas position for one preview frame."""

    source_box: tuple[int, int, int, int]
    target_size: tuple[int, int]
    origin: tuple[float, float]
    downsample_factor: int


def interactive_downsample_factor(image: Image.Image, zoom_scale: float) -> int:
    """Choose a pyramid level close to screen resolution for responsive input."""
    factor = 1
    max_factor = min(16, max(1, min(image.width, image.height)))
    while factor * 2 <= max_factor and zoom_scale * factor * 2 <= 1.0:
        factor *= 2
    return factor


def compute_geometry(
    image: Image.Image,
    canvas_width: int,
    canvas_height: int,
    zoom_scale: float,
    fit_scale: float,
    pan_x: float,
    pan_y: float,
    reset_zoom: bool,
    previous_fit: float,
    interactive: bool,
) -> tuple[PreviewGeometry, float, float, float, float]:
    """Return (geometry, new_fit, new_zoom, new_pan_x, new_pan_y).

    View-state updates are returned so the caller can persist them once.
    """
    was_at_fit = abs(zoom_scale - previous_fit) < 0.0001
    new_fit = min(canvas_width / image.width, canvas_height / image.height, 1.0)
    if reset_zoom or was_at_fit:
        new_zoom = new_fit
        new_pan_x = 0.0
        new_pan_y = 0.0
    else:
        new_zoom = min(4.0, zoom_scale)
        new_pan_x, new_pan_y = _constrain_pan(
            image, new_zoom, canvas_width, canvas_height, pan_x, pan_y
        )

    display_width = image.width * new_zoom
    display_height = image.height * new_zoom
    left = canvas_width / 2 + new_pan_x - display_width / 2
    top = canvas_height / 2 + new_pan_y - display_height / 2
    overscan = min(
        max(canvas_width, canvas_height) * PREVIEW_OVERSCAN, PREVIEW_OVERSCAN_MAX_PX
    )
    source_left = max(0, math.floor((-overscan - left) / new_zoom))
    source_top = max(0, math.floor((-overscan - top) / new_zoom))
    source_right = min(image.width, math.ceil((canvas_width + overscan - left) / new_zoom))
    source_bottom = min(image.height, math.ceil((canvas_height + overscan - top) / new_zoom))
    if source_right <= source_left or source_bottom <= source_top:
        raise RuntimeError("无法显示这个缩放区域")

    factor = interactive_downsample_factor(image, new_zoom) if interactive else 1
    level_left = max(0, source_left // factor)
    level_top = max(0, source_top // factor)
    level_right = min(math.ceil(image.width / factor), math.ceil(source_right / factor))
    level_bottom = min(math.ceil(image.height / factor), math.ceil(source_bottom / factor))
    if level_right <= level_left or level_bottom <= level_top:
        raise RuntimeError("无法显示这个缩放区域")

    target_width = max(1, round((level_right - level_left) * new_zoom * factor))
    target_height = max(1, round((level_bottom - level_top) * new_zoom * factor))
    geometry = PreviewGeometry(
        source_box=(level_left, level_top, level_right, level_bottom),
        target_size=(target_width, target_height),
        origin=(
            left + level_left * factor * new_zoom,
            top + level_top * factor * new_zoom,
        ),
        downsample_factor=factor,
    )
    return geometry, new_fit, new_zoom, new_pan_x, new_pan_y  # type: ignore[return-value]


def _constrain_pan(
    image: Image.Image,
    zoom_scale: float,
    canvas_width: int,
    canvas_height: int,
    pan_x: float,
    pan_y: float,
) -> tuple[float, float]:
    display_width = image.width * zoom_scale
    display_height = image.height * zoom_scale
    max_x = max(0.0, (display_width - canvas_width) / 2)
    max_y = max(0.0, (display_height - canvas_height) / 2)
    return max(-max_x, min(max_x, pan_x)), max(-max_y, min(max_y, pan_y))


class PreviewEngine:
    """Background crop/resample for the preview canvas.

    Only Tk image creation stays on the UI thread; workers emit frames into a
    queue that the UI polls. Generation numbers drop stale frames.
    """

    def __init__(self, max_workers: int = 2) -> None:
        self.events: queue.Queue = queue.Queue()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="photo-culler-preview"
        )
        self._generation = 0
        self._futures: set[Future] = set()
        self._levels: dict[tuple[str, int], Image.Image] = {}
        self._levels_lock = Lock()

    @property
    def generation(self) -> int:
        return self._generation

    def bump_generation(self) -> int:
        self._generation += 1
        return self._generation

    def cancel_all(self) -> None:
        self.bump_generation()
        for future in self._futures:
            future.cancel()
        self._futures.clear()

    def clear_levels(self) -> None:
        with self._levels_lock:
            self._levels.clear()

    def drop_levels_for(self, path_id: str) -> None:
        with self._levels_lock:
            for key in [k for k in self._levels if k[0] == path_id]:
                self._levels.pop(key, None)

    def submit(
        self,
        generation: int,
        path_id: str,
        image: Image.Image,
        geometry: PreviewGeometry,
        interactive: bool,
    ) -> None:
        for future in self._futures:
            future.cancel()
        self._futures = {f for f in self._futures if not f.done()}
        future = self._executor.submit(
            self._render, generation, path_id, image, geometry, interactive
        )
        self._futures.add(future)

    def _render(
        self,
        generation: int,
        path_id: str,
        image: Image.Image,
        geometry: PreviewGeometry,
        interactive: bool,
    ) -> None:
        try:
            frame = self._build_frame(image, path_id, geometry, interactive)
            self.events.put((generation, path_id, frame, geometry, None))
        except Exception as exc:
            self.events.put((generation, path_id, None, None, exc))

    def _source_for(
        self, image: Image.Image, path_id: str, factor: int, interactive: bool
    ) -> Image.Image:
        if factor == 1:
            return image
        key = (path_id, factor)
        with self._levels_lock:
            cached = self._levels.get(key)
        if cached is not None:
            return cached
        size = (
            max(1, math.ceil(image.width / factor)),
            max(1, math.ceil(image.height / factor)),
        )
        level = image.resize(
            size,
            Image.Resampling.BILINEAR if interactive else Image.Resampling.LANCZOS,
        )
        with self._levels_lock:
            self._levels.setdefault(key, level)
        return level

    def _build_frame(
        self,
        image: Image.Image,
        path_id: str,
        geometry: PreviewGeometry,
        interactive: bool,
    ) -> Image.Image:
        source = self._source_for(image, path_id, geometry.downsample_factor, interactive)
        crop = source.crop(geometry.source_box)
        if crop.size != geometry.target_size:
            crop = crop.resize(
                geometry.target_size,
                Image.Resampling.BILINEAR if interactive else Image.Resampling.LANCZOS,
            )
        return crop

    def drain_latest(self) -> tuple | None:
        newest = None
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == self._generation:
                    newest = event
        except queue.Empty:
            pass
        if newest is not None:
            self._futures = {f for f in self._futures if not f.done()}
        return newest

    def build_frame_sync(
        self,
        image: Image.Image,
        path_id: str,
        geometry: PreviewGeometry,
        interactive: bool,
    ) -> Image.Image:
        """Synchronous first frame used when a photo is already in memory."""
        return self._build_frame(image, path_id, geometry, interactive)

    def shutdown(self) -> None:
        self.cancel_all()
        self._executor.shutdown(wait=False, cancel_futures=True)


def constrain_pan(
    image: Image.Image,
    zoom_scale: float,
    canvas_width: int,
    canvas_height: int,
    pan_x: float,
    pan_y: float,
) -> tuple[float, float]:
    return _constrain_pan(image, zoom_scale, canvas_width, canvas_height, pan_x, pan_y)
