"""Image decoding helpers used by preview and thumbnail paths."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

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


def _oriented_size_from_open(opened: Image.Image) -> tuple[int, int]:
    width, height = opened.size
    try:
        orientation = opened.getexif().get(0x0112, 1)
    except Exception:
        orientation = 1
    if orientation in (5, 6, 7, 8):
        return height, width
    return width, height


def read_raster_image(
    path: Path,
    max_size: tuple[int, int] | None = None,
) -> tuple[Image.Image, tuple[int, int]]:
    """Decode a normal image and detach it from its file handle.

    Returns ``(image, original_oriented_size)``. For preview/thumbnail reads,
    Pillow's ``draft`` skips most source pixels before decoding.
    """
    with Image.open(path) as opened:
        original_size = _oriented_size_from_open(opened)
        if max_size is not None:
            try:
                opened.draft("RGB", max_size)
            except (AttributeError, OSError, ValueError):
                # PNG/TIFF and some plugins do not implement draft().
                pass
        image = ImageOps.exif_transpose(opened)
        if max_size is not None:
            image.thumbnail(max_size, Image.Resampling.BILINEAR)
        return image.convert("RGB").copy(), original_size


def read_dng_image(
    path: Path,
    thumbnail_size: tuple[int, int] | None = None,
    full_resolution: bool = False,
) -> tuple[Image.Image, tuple[int, int]]:
    """Decode DNG for display.

    ``original_size`` is the processed (``iwidth``/``iheight``) sensor output
    size, not the embedded JPEG thumb — so 100% zoom can request a true full
    postprocess when ``full_resolution=True``.
    """
    if rawpy is None:
        raise RuntimeError("DNG 支持组件未安装")
    with rawpy.imread(str(path)) as raw:
        try:
            original_size = (int(raw.sizes.iwidth), int(raw.sizes.iheight))
        except Exception:
            original_size = None

        if full_resolution:
            # True full pixels for 100% inspect (memory/CPU heavy by design).
            array = raw.postprocess(
                use_camera_wb=True, no_auto_bright=False, half_size=False, output_bps=8
            )
            image = Image.fromarray(array).convert("RGB")
            if original_size is None:
                original_size = image.size
            return image.copy(), original_size

        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with io.BytesIO(thumb.data) as embedded:
                    with Image.open(embedded) as thumb_im:
                        image = ImageOps.exif_transpose(thumb_im).convert("RGB")
            else:
                image = Image.fromarray(thumb.data).convert("RGB")
        except Exception:
            array = raw.postprocess(
                use_camera_wb=True, no_auto_bright=False, half_size=True, output_bps=8
            )
            image = Image.fromarray(array).convert("RGB")

    if original_size is None:
        original_size = image.size
    if thumbnail_size is not None:
        image.thumbnail(thumbnail_size, Image.Resampling.BILINEAR)
    return image.copy(), original_size


def fit_long_edge(image: Image.Image, long_edge: int, resample=None) -> Image.Image:
    """Return a copy scaled so the long edge is at most *long_edge*.

    Downscale uses BILINEAR by default — much cheaper than LANCZOS and enough
    for the preview cache (100% inspect still loads full pixels).
    """
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
    if resample is None:
        resample = Image.Resampling.BILINEAR
    return image.resize(size, resample)


def decode_preview_photo(
    path: Path,
    long_edge: int = PREVIEW_CACHE_LONG_EDGE,
) -> tuple[Image.Image, tuple[int, int]]:
    """Decode a photo for the preview cache in a single file open.

    Returns ``(preview_image, original_size)``. JPEG uses Pillow ``draft`` so
    most source pixels are skipped before decode; ``original_size`` is the
    oriented full-resolution size used for zoom/fit math.
    """
    if path.suffix.lower() == ".dng":
        full, original_size = read_dng_image(path)
        return downsample_to_edge(full, long_edge), original_size
    preview, original_size = read_raster_image(
        path, (long_edge, long_edge) if long_edge > 0 else None
    )
    if max(preview.size) > long_edge > 0:
        preview = downsample_to_edge(preview, long_edge)
    return preview, original_size


def decode_photo(
    path: Path,
    thumbnail: bool = False,
    thumb_size: tuple[int, int] | None = None,
    full_resolution: bool = False,
) -> Image.Image:
    suffix = path.suffix.lower()
    if suffix == ".dng":
        image, _size = read_dng_image(
            path,
            thumb_size if thumbnail else None,
            full_resolution=full_resolution,
        )
        return image
    image, _size = read_raster_image(path, thumb_size if thumbnail else None)
    return image


def fit_for_display(
    image: Image.Image, max_width: int, max_height: int
) -> Image.Image:
    """Return a display-sized copy without mutating the source image."""
    scale = min(max_width / image.width, max_height / image.height, 1.0)
    if scale >= 1.0:
        return image
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.BILINEAR)


def downsample_to_edge(image: Image.Image, max_edge: int) -> Image.Image:
    """Return a copy limited to *max_edge* on the long side."""
    if max_edge <= 0 or (image.width <= max_edge and image.height <= max_edge):
        return image.copy()
    clone = image.copy()
    clone.thumbnail((max_edge, max_edge), Image.Resampling.BILINEAR)
    return clone
