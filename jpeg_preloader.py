"""Sliding-window JPEG preview cache with parallel preload workers."""

from __future__ import annotations

import queue
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import NamedTuple

from config import JPEG_PRELOAD_AHEAD, JPEG_PRELOAD_BEHIND, JPEG_PRELOAD_WORKERS
from domain import PhotoGroup
from imaging import decode_preview_photo
from sysmem import recommend_jpeg_cache_limit


class PreviewCacheEntry(NamedTuple):
    image: object  # PIL Image
    original_size: tuple[int, int]


class JpegCache:
    """Thread-safe LRU of *preview-sized* decoded JPEGs, keyed by resolved path."""

    def __init__(self, limit: int | None = None) -> None:
        self._limit = limit if limit is not None else recommend_jpeg_cache_limit()
        self._lock = Lock()
        self._data: OrderedDict[str, PreviewCacheEntry] = OrderedDict()

    @property
    def limit(self) -> int:
        with self._lock:
            return self._limit

    def set_limit(self, limit: int) -> None:
        with self._lock:
            self._limit = max(1, int(limit))
            while len(self._data) > self._limit:
                self._data.popitem(last=False)

    def retune_from_system_memory(self) -> int:
        """Recompute the slot count from current free RAM."""
        limit = recommend_jpeg_cache_limit()
        self.set_limit(limit)
        return limit

    def get(self, key: str) -> PreviewCacheEntry | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                self._data.move_to_end(key)
            return entry

    def put(self, key: str, image, original_size: tuple[int, int]) -> None:
        with self._lock:
            self._data[key] = PreviewCacheEntry(image, original_size)
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
    """Keep a sliding window of preview JPEGs decoded around the current index.

    Multiple worker threads decode in parallel. Navigation does not cancel
    in-flight decodes that are still useful for the new window — only a folder
    switch / close invalidates the epoch.
    """

    def __init__(self, cache: JpegCache, max_workers: int | None = None) -> None:
        self.cache = cache
        self.events: queue.Queue = queue.Queue()
        self._generation = 0
        workers = max_workers or max(2, JPEG_PRELOAD_WORKERS)
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="photo-culler-jpeg-preload"
        )
        self._lock = Lock()
        self._inflight: dict[str, Future] = {}
        self._completed = 0
        self._pending_total = 0

    @property
    def generation(self) -> int:
        return self._generation

    def invalidate(self) -> int:
        """Cancel queued work (folder change / shutdown). Running decodes finish into cache."""
        self._generation += 1
        with self._lock:
            for fut in self._inflight.values():
                fut.cancel()
            self._inflight.clear()
            self._completed = 0
            self._pending_total = 0
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
        with self._lock:
            inflight = set(self._inflight.keys())
            to_load = [
                item
                for _i, item in window
                if item.primary_id not in already and item.primary_id not in inflight
            ]
            if not to_load:
                cached_n = len(already & window_keys)
                running = len(self._inflight)
                if running:
                    return f"正在预载 JPG：缓存 {cached_n}/{len(window_keys)} · 在途 {running}"
                return f"JPG 缓存 {cached_n} / {len(window_keys)}"

            generation = self._generation
            # Progress baseline: what we already have + what we just queued.
            self._completed = len(already & window_keys)
            self._pending_total = len(window_keys)
            for item in to_load:
                fut = self._executor.submit(self._decode_one, generation, item)
                self._inflight[item.primary_id] = fut
            done = self._completed
            total = self._pending_total

        self.events.put((generation, done, total, False))
        return f"正在预载 JPG：{done}/{total}"

    def _decode_one(self, generation: int, item: PhotoGroup) -> None:
        key = item.primary_id
        should_report = False
        try:
            image, original_size = decode_preview_photo(item.primary)
            # Always fill cache — useful even if the user already moved on.
            self.cache.put(key, image, original_size)
        except Exception:
            pass
        finally:
            with self._lock:
                self._inflight.pop(key, None)
                if generation == self._generation:
                    self._completed += 1
                    done = self._completed
                    total = max(self._pending_total, done)
                    idle = not self._inflight
                    should_report = True
        if should_report:
            if idle:
                self.events.put((generation, total, total, True))
            else:
                self.events.put((generation, done, total, False))

    def close(self) -> None:
        self.invalidate()
        self._executor.shutdown(wait=False, cancel_futures=True)
