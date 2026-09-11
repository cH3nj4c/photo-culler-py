"""Sliding-window JPEG cache with a single latest-wins preload worker."""

from __future__ import annotations

import queue
from collections import OrderedDict
from pathlib import Path
from threading import Lock

from config import JPEG_CACHE_LIMIT, JPEG_PRELOAD_AHEAD, JPEG_PRELOAD_BEHIND
from domain import PhotoGroup
from imaging import read_raster_image
from workers import LatestOnlyWorker


class JpegCache:
    """Thread-safe LRU of full-resolution decoded JPEGs, keyed by resolved path."""

    def __init__(self, limit: int = JPEG_CACHE_LIMIT) -> None:
        self._limit = limit
        self._lock = Lock()
        self._data: OrderedDict[str, object] = OrderedDict()

    def get(self, key: str):
        with self._lock:
            image = self._data.get(key)
            if image is not None:
                self._data.move_to_end(key)
            return image

    def put(self, key: str, image) -> None:
        with self._lock:
            self._data[key] = image
            self._data.move_to_end(key)
            while len(self._data) > self._limit:
                self._data.popitem(last=False)

    def pop(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def keys(self) -> set[str]:
        with self._lock:
            return set(self._data.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class JpegPreloader:
    """Keep a sliding window of JPEGs decoded around the current index.

    One worker thread owns decoding. Rapid navigation replaces the pending job
    instead of spawning additional threads.
    """

    def __init__(self, cache: JpegCache) -> None:
        self.cache = cache
        self.events: queue.Queue = queue.Queue()
        self._generation = 0
        self._worker = LatestOnlyWorker("photo-culler-jpeg-preload")

    @property
    def generation(self) -> int:
        return self._generation

    def invalidate(self) -> int:
        self._generation += 1
        return self._generation

    def request_window(self, all_items: list[PhotoGroup], center_index: int) -> str:
        """Schedule preload around *center_index*. Returns a short status label."""
        if not all_items:
            return ""
        jpeg_items = [
            (i, item)
            for i, item in enumerate(all_items)
            if item.primary.suffix.lower() in {".jpg", ".jpeg"}
        ]
        if not jpeg_items:
            return ""

        lo = max(0, center_index - JPEG_PRELOAD_BEHIND)
        hi = min(len(all_items) - 1, center_index + JPEG_PRELOAD_AHEAD)
        window = sorted(
            ((i, item) for i, item in jpeg_items if lo <= i <= hi),
            key=lambda pair: abs(pair[0] - center_index),
        )
        window_paths = [item.primary for _i, item in window]
        window_keys = {item.primary_id for _i, item in window}

        evict_lo = max(0, center_index - JPEG_PRELOAD_BEHIND * 2)
        evict_hi = min(len(all_items) - 1, center_index + JPEG_PRELOAD_AHEAD * 2)
        keep_keys = {
            item.primary_id for i, item in jpeg_items if evict_lo <= i <= evict_hi
        }
        for key in list(self.cache.keys()):
            if key not in keep_keys:
                self.cache.pop(key)

        already = self.cache.keys()
        to_load = [item for _i, item in window if item.primary_id not in already]
        if not to_load:
            return f"JPG 缓存 {len(already & window_keys)} / {len(window_paths)}"

        self.invalidate()
        generation = self._generation
        self.events.put((generation, 0, len(to_load), False))
        self._worker.submit(generation, self._preload, to_load)
        return f"正在预载 JPG：0 / {len(to_load)}"

    def _preload(self, generation: int, items: list[PhotoGroup]) -> None:
        total = len(items)
        for number, item in enumerate(items, start=1):
            if generation != self._generation:
                return
            try:
                image = read_raster_image(item.primary)
            except Exception:
                # One corrupt file must not kill the whole preload window.
                if number == 1 or number == total or number % 10 == 0:
                    self.events.put((generation, number, total, False))
                continue
            if generation != self._generation:
                return
            self.cache.put(item.primary_id, image)
            if number == 1 or number == total or number % 10 == 0:
                self.events.put((generation, number, total, False))
        self.events.put((generation, total, total, True))

    def close(self) -> None:
        self.invalidate()
        self._worker.close()
