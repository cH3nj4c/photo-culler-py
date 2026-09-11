"""Query physical RAM and derive image-cache budgets."""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

# Conservative defaults if the OS call fails (8 GB total / 3 GB free).
_DEFAULT_TOTAL = 8 * 1024**3
_DEFAULT_AVAIL = 3 * 1024**3


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


@dataclass(frozen=True)
class RamInfo:
    total_bytes: int
    available_bytes: int

    @property
    def total_gb(self) -> float:
        return self.total_bytes / 1024**3

    @property
    def available_gb(self) -> float:
        return self.available_bytes / 1024**3


def read_ram_info() -> RamInfo:
    """Return total and currently available physical memory."""
    if sys.platform == "win32":
        try:
            stat = _MemoryStatusEx()
            stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                total = int(stat.ullTotalPhys) or _DEFAULT_TOTAL
                avail = int(stat.ullAvailPhys) or _DEFAULT_AVAIL
                avail = min(avail, total)
                return RamInfo(total_bytes=total, available_bytes=avail)
        except (AttributeError, OSError, ValueError):
            pass
    return RamInfo(total_bytes=_DEFAULT_TOTAL, available_bytes=_DEFAULT_AVAIL)


def image_nbytes(width: int, height: int, bands: int = 3) -> int:
    """Approximate decoded RGB(A) footprint."""
    return max(1, width) * max(1, height) * max(1, bands)


def preview_budget_bytes(total_bytes: int, available_bytes: int) -> int:
    """Bytes allowed for downsampled preview JPEGs (sliding window)."""
    usable = min(total_bytes * 0.06, available_bytes * 0.22)
    usable = max(usable, 48 * 1024**2)
    usable = min(usable, 512 * 1024**2)
    return int(usable)


def fullres_budget_bytes(total_bytes: int, available_bytes: int) -> int:
    """Bytes allowed for full-resolution JPEGs (current / 100% inspect)."""
    usable = min(total_bytes * 0.12, available_bytes * 0.35)
    usable = max(usable, 96 * 1024**2)
    usable = min(usable, 1536 * 1024**2)
    return int(usable)


def estimate_fullres_slots(budget_bytes: int, sample_nbytes: int | None) -> int:
    """How many full-res images fit in the budget (for status / tuning)."""
    if not sample_nbytes or sample_nbytes <= 0:
        # Assume ~80 MB per modern camera JPEG until we measure one.
        sample_nbytes = 80 * 1024**2
    return max(1, budget_bytes // sample_nbytes)


def adaptive_cache_budgets(info: RamInfo | None = None) -> tuple[int, int]:
    """Return (preview_budget, fullres_budget) in bytes from current RAM."""
    info = info or read_ram_info()
    return (
        preview_budget_bytes(info.total_bytes, info.available_bytes),
        fullres_budget_bytes(info.total_bytes, info.available_bytes),
    )
