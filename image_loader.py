"""Off-UI-thread photo loading with preview vs full-resolution modes."""

from __future__ import annotations

import queue
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from jpeg_preloader import JpegCache, PreviewCacheEntry
from imaging import decode_photo, decode_preview_photo


class ImageLoader:
    """Decode source photos away from the Tk event loop.

    * Preview loads feed the sliding-window JPEG cache (downscaled) and report
      ``(generation, path_id, image, original_size, error, full_flag)``.
    * Full loads are for 100% / zoomed inspection of the *current* photo only
      and are never written into the preview cache.
    """

    def __init__(self, jpeg_cache: JpegCache, max_workers: int = 2) -> None:
        self.jpeg_cache = jpeg_cache
        self.events: queue.Queue = queue.Queue()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="photo-culler-decode"
        )
        self._generation = 0
        self._futures: set[Future] = set()

    @property
    def generation(self) -> int:
        return self._generation

    def bump_generation(self) -> int:
        self._generation += 1
        return self._generation

    def cancel_pending(self) -> None:
        self.bump_generation()
        for future in self._futures:
            future.cancel()
        self._futures = {f for f in self._futures if not f.done()}

    def try_cached(self, path_id: str) -> PreviewCacheEntry | None:
        return self.jpeg_cache.get(path_id)

    def submit(self, path: Path, path_id: str, full_resolution: bool = False) -> int:
        """Queue a decode."""
        self.bump_generation()
        generation = self._generation
        for future in self._futures:
            future.cancel()
        self._futures = {f for f in self._futures if not f.done()}
        future = self._executor.submit(
            self._decode, generation, path, path_id, full_resolution
        )
        self._futures.add(future)
        return generation

    def _decode(
        self, generation: int, path: Path, path_id: str, full_resolution: bool
    ) -> None:
        try:
            if full_resolution:
                image = decode_photo(path, thumbnail=False)
                original_size = image.size
            else:
                image, original_size = decode_preview_photo(path)
                if path.suffix.lower() in {".jpg", ".jpeg"}:
                    self.jpeg_cache.put(path_id, image, original_size)
            self.events.put(
                (generation, path_id, image, original_size, None, full_resolution)
            )
        except Exception as exc:
            self.events.put((generation, path_id, None, None, exc, full_resolution))

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

    def shutdown(self) -> None:
        self.cancel_pending()
        self._executor.shutdown(wait=False, cancel_futures=True)
