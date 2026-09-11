"""Image decoding helpers used by preview and thumbnail paths."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError  # noqa: F401  (re-export)

from .config import JPEG_EXTENSIONS, THUMB_HEIGHT, THUMB_WIDTH, THUMBNAIL_DECODE_SCALE

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


def is_jpeg_path(path: Path) -> bool:
    return path.suffix.lower() in JPEG_EXTENSIONS
