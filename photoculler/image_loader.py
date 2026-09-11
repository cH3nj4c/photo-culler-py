"""Off-UI-thread full-resolution photo loading with a small worker pool."""

from __future__ import annotations

import queue
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from .jpeg_preloader import JpegCache
from .imaging import decode_photo


class ImageLoader:
    """Decode source photos away from the Tk event loop.

    Callers submit a path and receive ``(generation, path_id, image|None, error|None)``
    on ``events``. Generation numbers let the UI drop results for photos the user
    already navigated past.
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

    def try_cached(self, path_id: str) -> Image.Image | None:
        cached = self.jpeg_cache.get(path_id)
        return cached if isinstance(cached, Image.Image) else None

    def submit(self, path: Path, path_id: str) -> int:
        self.bump_generation()
        generation = self._generation
        for future in self._futures:
            future.cancel()
        self._futures = {f for f in self._futures if not f.done()}
        future = self._executor.submit(self._decode, generation, path, path_id)
        self._futures.add(future)
        return generation

    def _decode(self, generation: int, path: Path, path_id: str) -> None:
        try:
            image = decode_photo(path, thumbnail=False)
            # Cache only JPEGs; other formats stay on-demand to bound memory.
            if path.suffix.lower() in {".jpg", ".jpeg"}:
                self.jpeg_cache.put(path_id, image)
            self.events.put((generation, path_id, image, None))
        except Exception as exc:
            self.events.put((generation, path_id, None, exc))

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
