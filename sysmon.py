"""Live resource sampling for the right-hand sidebar.

Reports four things, each from the cheapest source that can actually observe it:

- **total RAM** — ``GlobalMemoryStatusEx`` (via :mod:`sysmem`); used is derived
  as total − available, which is what the OS reports rather than a sum of
  processes and therefore matches Task Manager closely.
- **this app's RAM** — ``GetProcessMemoryInfo`` → ``WorkingSetSize``. This is
  the resident set, the number people compare against Task Manager's "内存".
- **GPU utilisation** — the PDH counter ``\\GPU Engine(*)\\Utilization
  Percentage``. Each instance is one engine of one adapter for one process
  (``pid_1234_luid_0x..._phys_0_eng_0_engtype_3D``), so the same numbers give
  both the machine-wide figure (sum of every instance) and this app's figure
  (sum of the instances whose pid is ours).
- **VRAM** — ``\\GPU Adapter Memory(*)\\Dedicated Usage`` for the machine (one
  instance per adapter LUID) and ``\\GPU Process Memory(*)\\Dedicated Usage``
  for this app. "Dedicated" is deliberate: integrated GPUs draw from system
  RAM, so their dedicated figure is ~0 and summing dedicated usage avoids
  double-counting shared memory.

Two constraints shape the implementation:

- **Sampling runs on a background daemon thread.** PDH has to be collected
  twice, about a second apart, before it yields a rate; doing that on the UI
  thread would stall painting.
- **Nothing here may raise.** These are decorative readouts on a sidebar; a
  machine without PDH counters (or with a broken one) must degrade to "—" with
  a reason in :attr:`Snapshot.notes`, not take the window down.

PDH status codes are signed ``LONG`` values such as ``PDH_MORE_DATA``
(``0x800007D2``). Declaring the return type as ``c_ulong`` keeps the
comparisons in unsigned space — declaring nothing makes ctypes return a signed
int and every equality test silently fails.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import sysmem

# How often the sampler wakes up. PDH rates need ~1s between collections, so
# anything faster would just report the same interval twice.
DEFAULT_INTERVAL_S = 1.0

# Summing every engine of every adapter can exceed 100% (an adapter running
# 3D + copy + video decode at once). Task Manager shows the machine as a whole
# at most fully busy, so the displayed figures are clamped.
_MAX_PERCENT = 100.0

_PID_RE = re.compile(r"^pid_(\d+)_")
# PDH instance names look like `pid_1234_luid_0x00000000_0x000114AD_phys_0_eng_0_engtype_3D`.
# The LUID is split into high/low halves; DXGI reports the same value as one
# 64-bit integer, so recombining lets us label a percentage with the adapter's
# real name instead of a hex blob.
_LUID_RE = re.compile(r"_luid_(0x[0-9A-Fa-f]+)_(0x[0-9A-Fa-f]+)_")


def _parse_luid(instance: str) -> int | None:
    match = _LUID_RE.search(instance or "")
    if not match:
        return None
    try:
        high = int(match.group(1), 16)
        low = int(match.group(2), 16)
    except ValueError:
        return None
    # Mask to 64 bits: DXGI's AdapterLuid is a signed LARGE_INTEGER and the
    # halves are unsigned here, so a large LUID must not drift negative.
    return ((high << 32) | low) & 0xFFFFFFFFFFFFFFFF


@dataclass(frozen=True)
class Snapshot:
    """One immutable reading. ``None`` means "not available on this machine"."""

    ts: float = 0.0
    ram_total_mb: int = 0
    ram_used_mb: int = 0
    app_ram_mb: int = 0
    gpu_percent: float | None = None
    app_gpu_percent: float | None = None
    vram_total_mb: int | None = None
    vram_used_mb: int | None = None
    app_vram_mb: int | None = None
    # Per-adapter GPU utilisation, keyed by the adapter's 64-bit LUID (the same
    # value DXGI reports), for the tooltip. Empty when unknown.
    adapters: tuple[tuple[int, float], ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def ram_percent(self) -> float:
        if self.ram_total_mb <= 0:
            return 0.0
        return min(_MAX_PERCENT, self.ram_used_mb * 100.0 / self.ram_total_mb)

    @property
    def vram_percent(self) -> float | None:
        if not self.vram_total_mb or self.vram_used_mb is None:
            return None
        return min(_MAX_PERCENT, self.vram_used_mb * 100.0 / self.vram_total_mb)

    @property
    def has_gpu(self) -> bool:
        return self.gpu_percent is not None


# --- app working set ---------------------------------------------------------

class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _bind_kernel32():
    """GetCurrentProcess returns a pseudo-handle (-1), so restype must be
    HANDLE (pointer-sized). Leaving the default c_int truncates it and
    GetProcessMemoryInfo then fails with a zero working set."""
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi")
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    return kernel32, psapi


def app_working_set_mb() -> int:
    """This process's resident memory in MB. 0 when the probe fails."""
    if sys.platform != "win32":
        return 0
    try:
        kernel32, psapi = _bind_kernel32()
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return int(counters.WorkingSetSize // 1048576)
    except (AttributeError, OSError, ValueError):
        pass
    return 0


# --- PDH ---------------------------------------------------------------------

class _PdhFmtCounterValue(ctypes.Structure):
    class _Value(ctypes.Union):
        _fields_ = [
            ("longValue", ctypes.c_long),
            ("doubleValue", ctypes.c_double),
            ("largeValue", ctypes.c_longlong),
            ("AnsiStringValue", ctypes.c_char_p),
            ("WideStringValue", ctypes.c_wchar_p),
        ]

    _fields_ = [("CStatus", ctypes.c_ulong), ("u", _Value)]


class _PdhFmtCounterValueItem(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _PdhFmtCounterValue)]


_PDH_FMT_DOUBLE = 0x00000200
_PDH_MORE_DATA = 0x800007D2
_PDH_CSTATUS_VALID_DATA = 0x00000000

_COUNTER_ENGINE = r"\GPU Engine(*)\Utilization Percentage"
_COUNTER_PROC_MEM = r"\GPU Process Memory(*)\Dedicated Usage"
_COUNTER_ADAPTER_MEM = r"\GPU Adapter Memory(*)\Dedicated Usage"


class PdhGpuCounters:
    """Thin wrapper over the PDH GPU counters.

    ``available`` is False whenever any step failed; callers then treat the GPU
    figures as unknown rather than zero, because "0%" and "cannot measure" look
    identical to a user but mean very different things.
    """

    def __init__(self) -> None:
        self.available = False
        self.reason = ""
        self._pdh = None
        self._query = ctypes.c_void_p()
        self._counters: dict[str, ctypes.c_void_p] = {}

    def open(self) -> bool:
        if sys.platform != "win32":
            self.reason = "仅 Windows 支持 GPU 计数器"
            return False
        try:
            pdh = ctypes.WinDLL("pdh")
        except OSError as exc:
            self.reason = f"无法加载 pdh.dll：{exc}"
            return False

        # PDH_STATUS is a signed LONG; c_ulong keeps comparisons unsigned.
        pdh.PdhOpenQueryW.restype = ctypes.c_ulong
        pdh.PdhOpenQueryW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)
        ]
        pdh.PdhAddEnglishCounterW.restype = ctypes.c_ulong
        pdh.PdhAddEnglishCounterW.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        pdh.PdhCollectQueryData.restype = ctypes.c_ulong
        pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_ulong
        pdh.PdhGetFormattedCounterArrayW.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
        ]
        pdh.PdhCloseQuery.restype = ctypes.c_ulong
        pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]

        self._pdh = pdh
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
            self.reason = "PdhOpenQuery 失败"
            return False

        for name, path in (
            ("engine", _COUNTER_ENGINE),
            ("proc_mem", _COUNTER_PROC_MEM),
            ("adapter_mem", _COUNTER_ADAPTER_MEM),
        ):
            handle = ctypes.c_void_p()
            if pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(handle)) != 0:
                # A machine with no GPU at all may legitimately lack these.
                self.reason = f"缺少性能计数器 {path}"
                self.close()
                return False
            self._counters[name] = handle

        self.available = True
        return True

    def close(self) -> None:
        if self._pdh is not None and self._query:
            try:
                self._pdh.PdhCloseQuery(self._query)
            except OSError:
                pass
        self._query = ctypes.c_void_p()
        self._counters.clear()
        self.available = False

    def collect(self) -> bool:
        """Take one sample. The first call only primes the rate counters."""
        if not self.available:
            return False
        try:
            return self._pdh.PdhCollectQueryData(self._query) == 0
        except OSError:
            return False

    def _read(self, name: str) -> list[tuple[str, float]]:
        """Enumerate one counter's instances as (instance_name, value)."""
        counter = self._counters.get(name)
        if not counter:
            return []
        size = ctypes.c_ulong(0)
        count = ctypes.c_ulong(0)
        status = self._pdh.PdhGetFormattedCounterArrayW(
            counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None
        )
        if status == _PDH_MORE_DATA:
            buffer = ctypes.create_string_buffer(size.value)
            status = self._pdh.PdhGetFormattedCounterArrayW(
                counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count),
                ctypes.cast(buffer, ctypes.c_void_p),
            )
        if status != 0 or count.value == 0:
            return []
        items = ctypes.cast(buffer, ctypes.POINTER(_PdhFmtCounterValueItem))
        rows: list[tuple[str, float]] = []
        for i in range(count.value):
            item = items[i]
            # Skip instances the counter could not compute a value for; a
            # wildcard on an idle engine returns invalid entries.
            if item.FmtValue.CStatus != _PDH_CSTATUS_VALID_DATA:
                continue
            rows.append((item.szName or "", float(item.FmtValue.u.doubleValue)))
        return rows

    def gpu_percent(self, pid: int | None = None) -> tuple[float, tuple[tuple[int, float], ...]]:
        """Total GPU utilisation, or one process's. Returns (percent, per_luid)."""
        rows = self._read("engine")
        total = 0.0
        by_luid: dict[int, float] = {}
        mine = 0.0
        for instance, value in rows:
            if pid is not None:
                match = _PID_RE.match(instance)
                if not match or int(match.group(1)) != pid:
                    continue
                mine += value
                continue
            total += value
            luid = _parse_luid(instance)
            if luid is not None:
                by_luid[luid] = by_luid.get(luid, 0.0) + value
        if pid is not None:
            return min(_MAX_PERCENT, mine), ()
        return (
            min(_MAX_PERCENT, total),
            tuple(sorted(by_luid.items(), key=lambda kv: -kv[1])),
        )

    def dedicated_bytes(self, counter_name: str, pid: int | None = None) -> int:
        """Sum dedicated VRAM, either for the whole machine or one process."""
        total = 0
        for instance, value in self._read(counter_name):
            if pid is not None:
                match = _PID_RE.match(instance)
                if not match or int(match.group(1)) != pid:
                    continue
            total += int(value)
        return total


# --- display helpers ---------------------------------------------------------

def format_mb(value: float | int | None) -> str:
    """``1536`` → ``"1.5 GB"``, ``512`` → ``"512 MB"``, ``None`` → ``"—"``."""
    if value is None:
        return "—"
    size = float(value)
    if size >= 1024:
        return f"{size / 1024:.1f} GB"
    return f"{size:.0f} MB"


def _format_usage(used: int | None, total: int | None, percent: float | None) -> str:
    """``used / total`` plus a percentage, degrading as data goes missing.

    The unit is factored out when both sides share it, because the sidebar is
    narrow: ``"11.0/15.8 GB 70%"`` reads fine where ``"11.0 GB/15.8 GB 70%"``
    starts to crowd the label.
    """
    if used is None:
        return "—"
    if not total:
        return format_mb(used)
    if used >= 1024 and total >= 1024:
        text = f"{used / 1024:.1f}/{total / 1024:.1f} GB"
    else:
        text = f"{format_mb(used)}/{format_mb(total)}"
    if percent is not None:
        text += f" {percent:.0f}%"
    return text


def format_percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}%"


def resource_rows(snapshot: Snapshot) -> tuple[tuple[str, str], ...]:
    """The sidebar's four lines as (label, value) pairs.

    Kept here rather than in the Qt layer so the wording and the rounding can be
    tested without a window.
    """
    return (
        ("内存", _format_usage(snapshot.ram_used_mb, snapshot.ram_total_mb, snapshot.ram_percent)),
        ("本程序", format_mb(snapshot.app_ram_mb)),
        ("GPU", format_percent(snapshot.gpu_percent)),
        ("显存", _format_usage(snapshot.vram_used_mb, snapshot.vram_total_mb, snapshot.vram_percent)),
    )


def resource_tooltip(
    snapshot: Snapshot, adapter_names: dict[int, str] | None = None
) -> str:
    """Detail that does not fit in the sidebar: per-adapter and per-process."""
    names = adapter_names or {}
    lines = ["实时资源占用", ""]
    lines.append(f"内存：{snapshot.ram_used_mb:,} / {snapshot.ram_total_mb:,} MB"
                 f"（{snapshot.ram_percent:.0f}%）")
    lines.append(f"本程序内存：{snapshot.app_ram_mb:,} MB（工作集）")

    if snapshot.gpu_percent is None:
        lines.append("GPU 占用：不可用")
    else:
        lines.append(f"GPU 占用：{snapshot.gpu_percent:.1f}%（全机所有引擎合计，上限 100%）")
        for luid, percent in snapshot.adapters:
            label = names.get(luid) or f"适配器 LUID 0x{luid:016X}"
            lines.append(f"    · {label}：{percent:.1f}%")
    lines.append(f"本程序 GPU：{format_percent(snapshot.app_gpu_percent)}")

    if snapshot.vram_used_mb is None:
        lines.append("显存占用：不可用")
    elif snapshot.vram_total_mb:
        pct = snapshot.vram_percent
        shown = f"{pct:.0f}%" if pct is not None else "—"
        lines.append(
            f"显存占用：{snapshot.vram_used_mb:,} / {snapshot.vram_total_mb:,} MB（{shown}）"
        )
    else:
        lines.append(f"显存占用：{snapshot.vram_used_mb:,} MB（总容量未知）")
    lines.append(f"本程序显存：{format_mb(snapshot.app_vram_mb)}")
    lines.append("")
    lines.append("显存指「专用显存」；核显从系统内存划分，因此其专用显存接近 0。")
    if snapshot.notes:
        lines.append("")
        lines.extend(f"· {note}" for note in snapshot.notes)
    return "\n".join(lines)


# --- the monitor -------------------------------------------------------------

def _empty_snapshot(note: str = "") -> Snapshot:
    info = sysmem.get_memory_info()
    return Snapshot(
        ts=time.monotonic(),
        ram_total_mb=int(info.total_mb),
        ram_used_mb=int(info.total_mb - info.avail_mb),
        app_ram_mb=app_working_set_mb(),
        notes=(note,) if note else (),
    )


def sample_once(counters: PdhGpuCounters | None, vram_total_mb: int | None) -> Snapshot:
    """Take a single reading. Never raises."""
    info = sysmem.get_memory_info()
    total_mb = int(info.total_mb)
    used_mb = int(info.total_mb - info.avail_mb)
    notes: list[str] = []

    gpu_percent: float | None = None
    app_gpu: float | None = None
    adapters: tuple[tuple[int, float], ...] = ()
    vram_used: int | None = None
    app_vram: int | None = None

    if counters is not None and counters.available:
        try:
            pid = os.getpid()
            gpu_percent, adapters = counters.gpu_percent()
            app_gpu, _ = counters.gpu_percent(pid=pid)
            vram_used = counters.dedicated_bytes("adapter_mem") // 1048576
            app_vram = counters.dedicated_bytes("proc_mem", pid=pid) // 1048576
        except (OSError, ValueError) as exc:
            notes.append(f"GPU 采样失败：{type(exc).__name__}: {exc}")
            gpu_percent = app_gpu = None
            vram_used = app_vram = None
    elif vram_total_mb:
        notes.append(counters.reason if counters else "GPU 计数器不可用")

    return Snapshot(
        ts=time.monotonic(),
        ram_total_mb=total_mb,
        ram_used_mb=used_mb,
        app_ram_mb=app_working_set_mb(),
        gpu_percent=gpu_percent,
        app_gpu_percent=app_gpu,
        vram_total_mb=vram_total_mb,
        vram_used_mb=vram_used,
        app_vram_mb=app_vram,
        adapters=adapters,
        notes=tuple(notes),
    )


class SystemMonitor:
    """Samples resources on a background daemon thread.

    ``latest()`` is safe to call from the UI thread at any time and always
    returns a complete snapshot — the previous one until the first sample
    lands. Call :meth:`stop` on shutdown.
    """

    def __init__(
        self,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        vram_total_mb: int | None = None,
    ) -> None:
        self.interval_s = max(0.25, float(interval_s))
        self.vram_total_mb = vram_total_mb
        self._lock = threading.Lock()
        self._snapshot = _empty_snapshot()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="photoculler-sysmon", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def latest(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def sample_now(self) -> Snapshot:
        """Synchronously take one reading (used by tests and the first paint)."""
        return self.latest()

    def _publish(self, snapshot: Snapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def _run(self) -> None:
        counters = PdhGpuCounters()
        try:
            counters.open()
            if counters.available:
                # Prime the rate counters so the first published sample is real
                # rather than a meaningless zero.
                counters.collect()
            while not self._stop.is_set():
                # Wait first: the counters need ~1s between collections, and
                # waiting up front means the process spends its first moment
                # responsive instead of blocking on PDH.
                if self._stop.wait(self.interval_s):
                    break
                if counters.available:
                    counters.collect()
                self._publish(sample_once(counters, self.vram_total_mb))
        except Exception:  # noqa: BLE001 - a monitor must never kill the app
            pass
        finally:
            counters.close()
