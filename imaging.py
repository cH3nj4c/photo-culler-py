"""Image decoding helpers used by preview and thumbnail paths."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError  # noqa: F401  (re-export)

from config import (
    PREVIEW_CACHE_LONG_EDGE,
    THUMB_HEIGHT,
    THUMB_WIDTH,
    THUMBNAIL_DECODE_SCALE,
)

try:
    import rawpy
except ImportError:  # pragma: no cover - optional dependency
    rawpy = None


def thumbnail_decode_size(thumb_width: int, thumb_height: int) -> tuple[int, int]:
    """Return a small decode target with enough pixels for a sharp thumbnail."""
    return (
        max(THUMB_WIDTH, thumb_width * THUMBNAIL_DECODE_SCALE),
        max(THUMB_HEIGHT, thumb_height * THUMBNAIL_DECODE_SCALE),
    )


def read_raster_image(path: Path, max_size: tuple[int, int] | None = None) -> Image.Image:
    """Decode a normal image and detach it from its file handle.

    For thumbnail reads, Pillow's ``draft`` lets JPEG decoders skip most of the
    source pixels before decoding. Full-size reads keep the original pixel data.
    """
    with Image.open(path) as opened:
        if max_size is not None:
            try:
                opened.draft("RGB", max_size)
            except (AttributeError, OSError, ValueError):
                # PNG/TIFF and some plugins do not implement draft().
                pass
        image = ImageOps.exif_transpose(opened)
        if max_size is not None:
            image.thumbnail(max_size, Image.Resampling.BILINEAR)
        return image.convert("RGB").copy()


def read_dng_image(path: Path, thumbnail_size: tuple[int, int] | None = None) -> Image.Image:
    if rawpy is None:
        raise RuntimeError("DNG 支持组件未安装")
    with rawpy.imread(str(path)) as raw:
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with io.BytesIO(thumb.data) as embedded:
                    image = ImageOps.exif_transpose(Image.open(embedded)).convert("RGB")
            else:
                image = Image.fromarray(thumb.data).convert("RGB")
        except Exception:
            array = raw.postprocess(
                use_camera_wb=True, no_auto_bright=False, half_size=True, output_bps=8
            )
            image = Image.fromarray(array).convert("RGB")
    if thumbnail_size is not None:
        image.thumbnail(thumbnail_size, Image.Resampling.BILINEAR)
    return image.copy()


def fit_long_edge(image: Image.Image, long_edge: int) -> Image.Image:
    """Return a copy scaled so the long edge is at most *long_edge*."""
    if long_edge <= 0:
        return image
    current = max(image.width, image.height)
    if current <= long_edge:
        return image
    scale = long_edge / current
    size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    return image.resize(size, Image.Resampling.LANCZOS)


def decode_preview_photo(
    path: Path,
    long_edge: int = PREVIEW_CACHE_LONG_EDGE,
) -> tuple[Image.Image, tuple[int, int]]:
    """Decode a photo for the preview cache.

    Returns ``(preview_image, original_size)``. The preview is downscaled to
    *long_edge*; ``original_size`` is the pre-downscale pixel size so zoom/fit
    stay relative to true camera pixels.
    """
    image = decode_photo(path, thumbnail=False)
    original_size = image.size
    return fit_long_edge(image, long_edge), original_size


def decode_photo(
    path: Path,
    thumbnail: bool = False,
    thumb_size: tuple[int, int] | None = None,
) -> Image.Image:
    suffix = path.suffix.lower()
    if suffix == ".dng":
        return read_dng_image(path, thumb_size if thumbnail else None)
    return read_raster_image(path, thumb_size if thumbnail else None)


def fit_for_display(
    image: Image.Image, max_width: int, max_height: int
) -> Image.Image:
    """Return a display-sized copy without mutating the source image."""
    scale = min(max_width / image.width, max_height / image.height, 1.0)
    if scale >= 1.0:
        return image
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def read_oriented_size(path: Path) -> tuple[int, int]:
    """Original pixel size after EXIF orientation, without decoding pixels."""
    with Image.open(path) as opened:
        width, height = opened.size
        try:
            orientation = opened.getexif().get(0x0112, 1)
        except Exception:
            orientation = 1
    if orientation in (5, 6, 7, 8):
        return height, width
    return width, height


def downsample_to_edge(image: Image.Image, max_edge: int) -> Image.Image:
    """Return a copy limited to *max_edge* on the long side."""
    if max_edge <= 0 or (image.width <= max_edge and image.height <= max_edge):
        return image.copy()
    clone = image.copy()
    clone.thumbnail((max_edge, max_edge), Image.Resampling.BILINEAR)
    return clone


def decode_preview_jpeg(path: Path, max_edge: int) -> tuple[Image.Image, int, int]:
    """Fast reduced decode for the sliding window.

    Returns ``(preview_image, original_width, original_height)``.
    JPEG uses Pillow ``draft`` so most source pixels are skipped.
    """
    orig_w, orig_h = read_oriented_size(path)
    preview = read_raster_image(path, (max_edge, max_edge) if max_edge > 0 else None)
    return preview, orig_w, orig_h


def decode_full_with_preview(
    path: Path, max_edge: int
) -> tuple[Image.Image, Image.Image, int, int]:
    """Decode full pixels once and derive a preview copy.

    Returns ``(full_image, preview_image, original_width, original_height)``.
    """
    if path.suffix.lower() == ".dng":
        full = read_dng_image(path)
        return full, downsample_to_edge(full, max_edge), full.width, full.height
    full = read_raster_image(path)
    return full, downsample_to_edge(full, max_edge), full.width, full.height
