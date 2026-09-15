"""Optional fast JPEG decode via libjpeg-turbo (PyTurboJPEG).

Falls back to Pillow when the package or the native library is missing.
Used for preview/thumbnail paths where reduced-scale decode is enough.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from PIL import Image, ImageOps

_turbo = None
_turbo_probe_done = False
_turbo_error: str | None = None


def _probe_turbojpeg():
    """Create a process-wide TurboJPEG handle once, or remember why not."""
    global _turbo, _turbo_probe_done, _turbo_error
    if _turbo_probe_done:
        return _turbo
    _turbo_probe_done = True
    try:
        from turbojpeg import TurboJPEG  # type: ignore
    except ImportError as exc:
        _turbo_error = f"PyTurboJPEG not installed ({exc})"
        return None
    try:
        _turbo = TurboJPEG()
    except Exception as exc:  # missing jpeg62/turbojpeg DLL, etc.
        _turbo = None
        _turbo_error = f"TurboJPEG init failed ({exc})"
    return _turbo


def turbojpeg_status() -> str:
    if _probe_turbojpeg() is not None:
        return "TurboJPEG (libjpeg-turbo)"
    return _turbo_error or "TurboJPEG unavailable"


def is_turbojpeg_available() -> bool:
    return _probe_turbojpeg() is not None


def _jpeg_scaling_factor(width: int, height: int, max_size: tuple[int, int] | None) -> int:
    """Pick libjpeg-turbo denominator 1, 2, 4, or 8 (output ≈ size/denom)."""
    if max_size is None or width <= 0 or height <= 0:
        return 1
    target_w = max(1, max_size[0])
    target_h = max(1, max_size[1])
    denom = 1
    while denom < 8 and (
        width // (denom * 2) >= target_w or height // (denom * 2) >= target_h
    ):
        denom *= 2
    return denom


def decode_jpeg_turbo(
    path: Path,
    max_size: tuple[int, int] | None = None,
) -> tuple[Image.Image, tuple[int, int]] | None:
    """Decode JPEG with libjpeg-turbo. Returns None if turbo is unavailable.

    Returns ``(image, original_oriented_size)``. Output may still need a small
    Pillow thumbnail if the turbo scale factor overshoots ``max_size``.
    """
    jpeg = _probe_turbojpeg()
    if jpeg is None:
        return None

    data = Path(path).read_bytes()
    # Header-only size + orientation via Pillow (cheap; no full decode).
    with Image.open(io.BytesIO(data)) as header:
        width, height = header.size
        try:
            orientation = header.getexif().get(0x0112, 1)
        except Exception:
            orientation = 1
    if orientation in (5, 6, 7, 8):
        original_size = (height, width)
    else:
        original_size = (width, height)

    scaling = _jpeg_scaling_factor(width, height, max_size)
    try:
        raw = jpeg.decode(data, scaling_factor=scaling)
    except Exception:
        return None

    image = Image.fromarray(raw, mode="RGB" if raw.shape[2] == 3 else "RGBA")
    if image.mode != "RGB":
        image = image.convert("RGB")

    # Apply EXIF orientation the same way Pillow's exif_transpose would.
    image = _apply_orientation(image, orientation)
    if max_size is not None:
        image.thumbnail(max_size, Image.Resampling.BILINEAR)
    return image.copy(), original_size


def _apply_orientation(image: Image.Image, orientation: int) -> Image.Image:
    if orientation == 2:
        return ImageOps.mirror(image)
    if orientation == 3:
        return image.transpose(Image.Transpose.ROTATE_180)
    if orientation == 4:
        return ImageOps.flip(image)
    if orientation == 5:
        return image.transpose(Image.Transpose.ROTATE_90).transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if orientation == 6:
        return image.transpose(Image.Transpose.ROTATE_270)
    if orientation == 7:
        return image.transpose(Image.Transpose.ROTATE_270).transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if orientation == 8:
        return image.transpose(Image.Transpose.ROTATE_90)
    return image


def env_disable_turbojpeg() -> bool:
    return os.environ.get("PHOTOCULLER_NO_TURBOJPEG", "").strip() not in (
        "",
        "0",
        "false",
        "False",
    )


def decode_jpeg_fast(
    path: Path,
    max_size: tuple[int, int] | None = None,
) -> tuple[Image.Image, tuple[int, int]] | None:
    """Try TurboJPEG; return None to signal Pillow fallback."""
    if env_disable_turbojpeg():
        return None
    try:
        return decode_jpeg_turbo(path, max_size)
    except Exception:
        return None
