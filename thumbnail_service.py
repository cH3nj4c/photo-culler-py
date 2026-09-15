"""Background thumbnail decode; UI thread only builds Tk PhotoImages."""

from __future__ import annotations

import queue
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from imaging import decode_photo, fit_for_display, thumbnail_decode_size


class ThumbnailService:
    """Decode strip thumbnails off the UI thread.

    Events: ``(cache_key, pil_image|None, error|None)``. The UI converts PIL
    images into ``ImageTk.PhotoImage`` on the main thread.
    """

    def __init__(self, max_workers: int = 2) -> None:
        self.events: queue.Queue = queue.Queue()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="photo-culler-thumb"
        )
        self._pending: set = set()
        self._futures: set[Future] = set()
        self._generation = 0

    def bump_generation(self) -> int:
        self._generation += 1
        return self._generation

    def cancel_pending(self) -> None:
        self.bump_generation()
        self._pending.clear()
        for future in self._futures:
            future.cancel()
        self._futures = {f for f in self._futures if not f.done()}

    def request(
        self,
        cache_key: tuple,
        path: Path,
        target_w: int,
        target_h: int,
    ) -> bool:
        """Queue a thumbnail decode. Returns False if already pending."""
        if cache_key in self._pending:
            return False
        self._pending.add(cache_key)
        generation = self._generation
        future = self._executor.submit(
            self._decode, generation, cache_key, path, target_w, target_h
        )
        self._futures.add(future)
        return True

    def _decode(
        self,
        generation: int,
        cache_key: tuple,
        path: Path,
        target_w: int,
        target_h: int,
    ) -> None:
        try:
            if generation != self._generation:
                self._pending.discard(cache_key)
                return
            image = decode_photo(
                path,
                thumbnail=True,
                thumb_size=thumbnail_decode_size(target_w, target_h),
            )
            image = fit_for_display(image, target_w, target_h)
            if image.width < target_w and image.height < target_h:
                background = Image.new("RGB", (target_w, target_h), "#1C2027")
                background.paste(
                    image,
                    ((target_w - image.width) // 2, (target_h - image.height) // 2),
                )
                image = background
            # Stay in _pending until the UI drains this event, so a redraw
            # between decode-finish and cache-apply cannot queue a duplicate.
            self.events.put((cache_key, image, None))
        except Exception as exc:
            self._pending.discard(cache_key)
            self.events.put((cache_key, None, exc))

    def drain(self, limit: int | None = None) -> list[tuple]:
        """Consume finished thumbnail events."""
        items: list[tuple] = []
        while limit is None or len(items) < limit:
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                break
        for cache_key, _image, _error in items:
            self._pending.discard(cache_key)
        if items:
            self._futures = {f for f in self._futures if not f.done()}
        return items

    def shutdown(self) -> None:
        self.cancel_pending()
        self._executor.shutdown(wait=False, cancel_futures=True)
