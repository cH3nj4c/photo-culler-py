"""Probe physical RAM and size the JPEG preview cache from what the machine can spare."""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass


# Approx RGB bytes for one cached preview (long edge PREVIEW_CACHE_LONG_EDGE, 3:2).
# Used only to convert a byte budget into a slot count.
PREVIEW_CACHE_APPROX_BYTES = 14 * 1024 * 1024

# Keep slots for slow navigation even on tight machines; cap avoids multi-GB piles.
JPEG_CACHE_LIMIT_MIN = 6
JPEG_CACHE_LIMIT_MAX = 60

# Headroom so the OS + Tk + one full-res working image still fit.
_FULL_RES_RESERVE = 180 * 1024 * 1024
_AVAIL_FRACTION = 0.35
_TOTAL_FRACTION = 0.12


@dataclass(frozen=True)
class MemoryInfo:
    total_bytes: int
    avail_bytes: int

    @property
    def total_mb(self) -> float:
        return self.total_bytes / (1024 * 1024)

    @property
    def avail_mb(self) -> float:
        return self.avail_bytes / (1024 * 1024)


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def get_memory_info() -> MemoryInfo:
    """Return (total, available) physical RAM. Falls back to a conservative default."""
    if sys.platform == "win32":
        try:
            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return MemoryInfo(
                    total_bytes=int(status.ullTotalPhys),
                    avail_bytes=int(status.ullAvailPhys),
                )
        except (AttributeError, OSError, ValueError):
            pass
    # Non-Windows or probe failure: assume a modest machine so limits stay small.
    fallback = 8 * 1024 * 1024 * 1024
    return MemoryInfo(total_bytes=fallback, avail_bytes=fallback // 4)


def recommend_jpeg_cache_limit(
    total_bytes: int | None = None,
    avail_bytes: int | None = None,
) -> int:
    """How many preview-sized JPEGs to keep, from spare RAM.

    Uses a fraction of *available* memory (and a fraction of total as a ceiling),
    reserves room for one full-resolution working image, then converts the rest
    into cache slots.
    """
    if total_bytes is None or avail_bytes is None:
        info = get_memory_info()
        total_bytes = info.total_bytes
        avail_bytes = info.avail_bytes

    if total_bytes <= 0 or avail_bytes <= 0:
        return JPEG_CACHE_LIMIT_MIN

    usable = min(avail_bytes * _AVAIL_FRACTION, total_bytes * _TOTAL_FRACTION)
    usable = max(usable - _FULL_RES_RESERVE, 32 * 1024 * 1024)
    slots = int(usable // PREVIEW_CACHE_APPROX_BYTES)
    return max(JPEG_CACHE_LIMIT_MIN, min(JPEG_CACHE_LIMIT_MAX, slots))


def describe_cache_plan(limit: int | None = None) -> str:
    info = get_memory_info()
    if limit is None:
        limit = recommend_jpeg_cache_limit(info.total_bytes, info.avail_bytes)
    return (
        f"内存 {info.avail_mb:.0f}/{info.total_mb:.0f} MB 可用，"
        f"JPG 预览缓存上限 {limit} 张"
    )
