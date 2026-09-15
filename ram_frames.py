"""RAM slots for the photo currently being previewed.

While a photo is on the Stage we keep its decoded preview (and optional
full-res copy) in process memory only — never on disk. Leaving the photo,
switching folders, or closing the app releases those slots so RAM returns
to the OS/GC promptly.

This is separate from the sliding-window JPEG LRU (neighborhood cache);
that cache is for *next* photos, these slots are for *this* photo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActivePreviewRam:
    path_id: str | None = None
    preview: Any = None          # PIL.Image (preview-sized)
    full: Any = None             # PIL.Image (full-res, optional)
    original_size: tuple[int, int] = (1, 1)
    proxy: Any = None            # last painted viewport frame (PIL)

    def retain_preview(self, path_id: str, image, original_size: tuple[int, int]) -> None:
        if self.path_id != path_id:
            self.clear()
        self.path_id = path_id
        self.preview = image
        self.original_size = original_size

    def retain_full(self, path_id: str, image) -> None:
        if self.path_id != path_id:
            self.clear()
        self.path_id = path_id
        self.full = image

    def retain_proxy(self, image) -> None:
        self.proxy = image

    def release_full(self) -> None:
        self.full = None

    def release_proxy(self) -> None:
        self.proxy = None

    def clear(self) -> None:
        """Drop every RAM reference for the previous photo."""
        self.path_id = None
        self.preview = None
        self.full = None
        self.proxy = None
        self.original_size = (1, 1)

    def approx_bytes(self) -> int:
        total = 0
        for img in (self.preview, self.full, self.proxy):
            if img is None:
                continue
            try:
                total += int(getattr(img, "nbytes", 0)) or (
                    img.width * img.height * len(img.getbands())
                )
            except Exception:
                continue
        return total
