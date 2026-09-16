"""Detect the machine's display adapters and classify them.

Three independent sources, merged, because none of them is complete alone:

- **Registry** (``HKLM\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-...}``)
  lists every *installed* display adapter, so it is the only source that
  reports an integrated GPU that the current session is not using. It carries
  the driver version and the real VRAM size.
- **DXGI** (``dxgi.dll`` ``CreateDXGIFactory1`` → ``EnumAdapters1``) lists the
  adapters the graphics stack will actually hand out, and is the only source
  that flags a *software* adapter (``DXGI_ADAPTER_FLAG_SOFTWARE``).
- **OpenGL renderer string**, supplied by the caller from the live context,
  tells us which adapter the running preview is really using.

On a hybrid laptop the registry and DXGI disagree in a useful way: the
registry shows both the iGPU and the dGPU, while DXGI may show only the one
attached to the current session. Reporting only DXGI would therefore answer
"is there an integrated GPU?" with a wrong "no".

Everything here is best-effort. Detection runs on the UI thread during a menu
click, so nothing may raise and nothing may block for long: every source is
wrapped, and a total failure still yields a usable (if empty) report.
"""

from __future__ import annotations

import ctypes
import re
from dataclasses import dataclass, field
from typing import Any

# --- classification ---------------------------------------------------------

INTEGRATED = "integrated"
DISCRETE = "discrete"
SOFTWARE = "software"
UNKNOWN = "unknown"

KIND_LABELS = {
    INTEGRATED: "核显（集成显卡）",
    DISCRETE: "独显（独立显卡）",
    SOFTWARE: "软件渲染（无硬件加速）",
    UNKNOWN: "未知",
}

VENDOR_IDS = {
    0x10DE: "NVIDIA",
    0x1002: "AMD",
    0x1022: "AMD",
    0x8086: "Intel",
    0x1414: "Microsoft",
    0x1AE0: "Google",
    0x13B5: "ARM",
}

# Discrete Intel Arc parts carry a model number (A770, B580); the integrated
# Core Ultra graphics is plain "Intel(R) Arc(TM) Graphics". Matching the model
# number is what separates the two.
_INTEL_DISCRETE_RE = re.compile(r"\barc\s+[ab]\d{3}\b")
# AMD discrete: "Radeon RX 7900 XTX", "Radeon Pro W6800", "Radeon VII".
_AMD_DISCRETE_RE = re.compile(r"\bradeon\s+(rx|pro|vii)\b|\brx\s*\d{3,4}\b|\bw[xyz]\d{4}\b")
# AMD integrated: "Radeon Graphics", "Radeon Vega 8", "Radeon 780M".
_AMD_IGPU_RE = re.compile(r"\bradeon\s+(graphics|vega)\b|\bradeon\s+\d{3}m\b|\bvega\s+\d+")
_SOFTWARE_RE = re.compile(
    r"(basic\s+render|basic\s+display|software\s+adapter|llvmpipe|swiftshader|"
    r"microsoft\s+basic|mesa\s+offscreen)",
    re.I,
)
# Trademark noise sits *between* the words these rules match on, so it has to
# come out first: "Radeon(TM) Graphics" never matches a `radeon\s+graphics`
# pattern until "(TM)" is gone.
_BRANDING_RE = re.compile(r"\((?:r|tm|c)\)|[®™©]", re.I)


def _normalise_name(name: str) -> str:
    stripped = _BRANDING_RE.sub(" ", name or "")
    return re.sub(r"\s+", " ", stripped).strip().lower()


def classify(name: str, vendor_id: int | None = None, is_software: bool = False) -> str:
    """Best-effort integrated / discrete / software classification.

    Pure function of the name plus (when known) the DXGI vendor id and flags,
    so it can be unit-tested without any hardware.
    """
    norm = _normalise_name(name)
    if is_software or _SOFTWARE_RE.search(norm):
        return SOFTWARE
    if not norm:
        return UNKNOWN

    if vendor_id == 0x10DE or "nvidia" in norm or "geforce" in norm or "quadro" in norm:
        # GeForce/Quadro are discrete. There is no Windows iGPU from NVIDIA.
        return DISCRETE
    if vendor_id == 0x1414 or "microsoft" in norm:
        return SOFTWARE
    if vendor_id == 0x8086 or "intel" in norm:
        return DISCRETE if _INTEL_DISCRETE_RE.search(norm) else INTEGRATED
    if vendor_id in (0x1002, 0x1022) or "amd" in norm or "radeon" in norm:
        if _AMD_DISCRETE_RE.search(norm):
            return DISCRETE
        if _AMD_IGPU_RE.search(norm):
            return INTEGRATED
    return UNKNOWN


@dataclass(frozen=True)
class Adapter:
    name: str
    kind: str = UNKNOWN
    vendor: str = ""
    vendor_id: int | None = None
    dedicated_vram_mb: int = 0
    driver_version: str = ""
    source: str = ""
    # True when DXGI can hand this adapter to an application right now.
    runtime_visible: bool = False

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def describe(self) -> str:
        bits = [self.name]
        if self.dedicated_vram_mb:
            bits.append(f"{self.dedicated_vram_mb} MB 专用显存")
        if self.driver_version:
            bits.append(f"驱动 {self.driver_version}")
        return " · ".join(bits)


@dataclass
class GpuReport:
    adapters: list[Adapter] = field(default_factory=list)
    renderer: str = ""
    opengl_version: str = ""
    gl_vendor: str = ""
    errors: list[str] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[Adapter]:
        return [a for a in self.adapters if a.kind == kind]

    @property
    def has_discrete(self) -> bool:
        return bool(self.of_kind(DISCRETE))

    @property
    def has_integrated(self) -> bool:
        return bool(self.of_kind(INTEGRATED))

    @property
    def discrete(self) -> Adapter | None:
        found = self.of_kind(DISCRETE)
        return found[0] if found else None

    @property
    def integrated(self) -> Adapter | None:
        found = self.of_kind(INTEGRATED)
        return found[0] if found else None

    @property
    def is_hybrid(self) -> bool:
        return self.has_discrete and self.has_integrated

    def headline(self) -> str:
        """One-line summary for a menu item or a status panel."""
        if not self.adapters:
            return "未检测到显示适配器"
        names = [a.name for a in self.adapters if a.kind != SOFTWARE]
        if not names:
            return "未检测到硬件显示适配器"
        return " + ".join(names)


# --- DXGI -------------------------------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", ctypes.c_uint),
        ("DeviceId", ctypes.c_uint),
        ("SubSysId", ctypes.c_uint),
        ("Revision", ctypes.c_uint),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", ctypes.c_longlong),
        ("Flags", ctypes.c_uint),
    ]


_IID_IDXGIFACTORY1 = "{770AAE78-F26F-4DBA-A829-253C83D1B387}"
_DXGI_ADAPTER_FLAG_SOFTWARE = 0x2
# IDXGIFactory1::EnumAdapters1 and IDXGIAdapter1::GetDesc1 vtable slots.
# IUnknown(0-2) IDXGIObject(3-6) IDXGIFactory(7-11) → EnumAdapters1 = 12
# IUnknown(0-2) IDXGIObject(3-6) IDXGIAdapter(7-9) → GetDesc1 = 10
_SLOT_ENUM_ADAPTERS1 = 12
_SLOT_GET_DESC1 = 10
_SLOT_RELEASE = 2


def _com_method(ptr, slot: int, restype, *argtypes):
    """Bind a COM vtable entry to a callable."""
    table = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(table[slot])


def _query_dxgi() -> list[Adapter]:
    """Enumerate adapters via DXGI. Empty list on any failure."""
    if not hasattr(ctypes, "WinDLL"):
        return []
    adapters: list[Adapter] = []
    factory = ctypes.c_void_p()
    iid = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(_IID_IDXGIFACTORY1, ctypes.byref(iid))
    dxgi = ctypes.WinDLL("dxgi")
    hr = dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory))
    if hr < 0 or not factory:
        return []
    enum = _com_method(
        factory, _SLOT_ENUM_ADAPTERS1, ctypes.c_long, ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p),
    )
    try:
        index = 0
        while index < 16:  # defensive bound; DXGI returns NOT_FOUND to stop
            adapter = ctypes.c_void_p()
            if enum(factory, index, ctypes.byref(adapter)) < 0 or not adapter:
                break
            try:
                desc = _DXGI_ADAPTER_DESC1()
                get_desc = _com_method(
                    adapter, _SLOT_GET_DESC1, ctypes.c_long,
                    ctypes.POINTER(_DXGI_ADAPTER_DESC1),
                )
                if get_desc(adapter, ctypes.byref(desc)) >= 0:
                    name = (desc.Description or "").strip()
                    is_software = bool(desc.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE)
                    kind = classify(name, desc.VendorId, is_software)
                    adapters.append(
                        Adapter(
                            name=name or "未知适配器",
                            kind=kind,
                            vendor=VENDOR_IDS.get(desc.VendorId, hex(desc.VendorId)),
                            vendor_id=desc.VendorId,
                            dedicated_vram_mb=int(desc.DedicatedVideoMemory // 1048576),
                            source="dxgi",
                            runtime_visible=not is_software,
                        )
                    )
            finally:
                _com_method(adapter, _SLOT_RELEASE, ctypes.c_ulong)(adapter)
            index += 1
    finally:
        _com_method(factory, _SLOT_RELEASE, ctypes.c_ulong)(factory)
    return adapters


# --- registry ---------------------------------------------------------------

_DISPLAY_CLASS = (
    r"SYSTEM\CurrentControlSet\Control\Class"
    r"\{4d36e968-e325-11ce-bfc1-08002be10318}"
)


def _query_registry() -> list[Adapter]:
    """Enumerate installed display adapters from the driver class key."""
    try:
        import winreg
    except ImportError:
        return []
    adapters: list[Adapter] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS)
    except OSError:
        return []
    try:
        index = 0
        while index < 32:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if not sub.isdigit():
                continue
            try:
                key = winreg.OpenKey(root, sub)
            except OSError:
                continue
            with key:
                def value(name: str):
                    try:
                        return winreg.QueryValueEx(key, name)[0]
                    except OSError:
                        return None

                def as_text(raw) -> str:
                    if isinstance(raw, bytes):
                        return raw.decode("utf-16-le", "replace").rstrip("\x00")
                    return str(raw or "")

                name = as_text(value("DriverDesc"))
                if not name:
                    continue
                # Prefer the 64-bit size: the DWORD variant saturates at 4 GB.
                vram = value("HardwareInformation.qwMemorySize")
                if not isinstance(vram, int):
                    vram = value("HardwareInformation.MemorySize")
                vram_mb = int(vram // 1048576) if isinstance(vram, int) else 0
                provider = as_text(value("ProviderName"))
                adapters.append(
                    Adapter(
                        name=name,
                        kind=classify(name, None, False),
                        vendor=provider,
                        dedicated_vram_mb=vram_mb,
                        driver_version=as_text(value("DriverVersion")),
                        source="registry",
                    )
                )
    finally:
        root.Close()
    return adapters


# --- merge ------------------------------------------------------------------

def _normalise_key(name: str) -> str:
    """Compare adapter names across sources despite branding noise."""
    text = (name or "").lower()
    for noise in ("(r)", "(tm)", "(c)", "nvidia ", "intel(r) ", "amd "):
        text = text.replace(noise, " ")
    return re.sub(r"[^a-z0-9]+", "", text)


def _same_adapter(a: str, b: str) -> bool:
    ka, kb = _normalise_key(a), _normalise_key(b)
    if not ka or not kb:
        return False
    return ka == kb or ka in kb or kb in ka


def merge_sources(*groups: list[Adapter]) -> list[Adapter]:
    """Public merge that matches on normalised names, not just exact text."""
    merged: list[Adapter] = []
    for group in groups:
        for adapter in group:
            hit = next(
                (m for m in merged if _same_adapter(m.name, adapter.name)), None
            )
            if hit is None:
                merged.append(adapter)
                continue
            idx = merged.index(hit)
            prefer = adapter
            if adapter.kind == UNKNOWN and hit.kind != UNKNOWN:
                prefer = Adapter(
                    name=hit.name, kind=hit.kind, vendor=hit.vendor,
                    vendor_id=hit.vendor_id,
                    dedicated_vram_mb=hit.dedicated_vram_mb,
                    driver_version=hit.driver_version,
                    source=hit.source, runtime_visible=hit.runtime_visible,
                )
            merged[idx] = Adapter(
                name=prefer.name,
                kind=prefer.kind,
                vendor=hit.vendor or adapter.vendor,
                vendor_id=hit.vendor_id or adapter.vendor_id,
                dedicated_vram_mb=hit.dedicated_vram_mb or adapter.dedicated_vram_mb,
                driver_version=hit.driver_version or adapter.driver_version,
                source=hit.source,
                runtime_visible=hit.runtime_visible or adapter.runtime_visible,
            )
    return merged


def apply_vram_heuristic(adapters: list[Adapter]) -> list[Adapter]:
    """Split unknown adapters by dedicated VRAM as a last resort.

    An adapter reporting >= 1 GB of its own memory is almost certainly a
    discrete part; integrated parts either report a small carve-out or zero.
    Only ever used for adapters no name rule could place.
    """
    out: list[Adapter] = []
    for a in adapters:
        if a.kind != UNKNOWN:
            out.append(a)
            continue
        if a.dedicated_vram_mb >= 1024:
            out.append(
                Adapter(
                    name=a.name, kind=DISCRETE, vendor=a.vendor or "unknown",
                    vendor_id=a.vendor_id,
                    dedicated_vram_mb=a.dedicated_vram_mb,
                    driver_version=a.driver_version, source=a.source,
                    runtime_visible=a.runtime_visible,
                )
            )
        else:
            out.append(
                Adapter(
                    name=a.name, kind=INTEGRATED, vendor=a.vendor or "unknown",
                    vendor_id=a.vendor_id,
                    dedicated_vram_mb=a.dedicated_vram_mb,
                    driver_version=a.driver_version, source=a.source,
                    runtime_visible=a.runtime_visible,
                )
            )
    return out


def detect_gpu(*, renderer: str = "", opengl_version: str = "", gl_vendor: str = "") -> GpuReport:
    """Full hardware report. Never raises; failures land in ``errors``."""
    report = GpuReport(
        renderer=renderer, opengl_version=opengl_version, gl_vendor=gl_vendor
    )
    registry: list[Adapter] = []
    dxgi: list[Adapter] = []
    try:
        registry = _query_registry()
    except Exception as exc:  # noqa: BLE001 - detection must never break the UI
        report.errors.append(f"注册表枚举失败：{type(exc).__name__}: {exc}")
    try:
        dxgi = _query_dxgi()
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"DXGI 枚举失败：{type(exc).__name__}: {exc}")

    merged = merge_sources(registry, dxgi)
    merged = apply_vram_heuristic(merged)
    # Software adapters last: they are noise for the "which GPU" question.
    merged.sort(key=lambda a: (a.kind == SOFTWARE, a.kind != DISCRETE))
    report.adapters = merged
    return report


def report_as_dict(report: GpuReport) -> dict[str, Any]:
    """JSON-friendly form, for tests and for the detail dialog."""
    return {
        "headline": report.headline(),
        "is_hybrid": report.is_hybrid,
        "has_discrete": report.has_discrete,
        "has_integrated": report.has_integrated,
        "renderer": report.renderer,
        "opengl": report.opengl_version,
        "gl_vendor": report.gl_vendor,
        "adapters": [
            {
                "name": a.name,
                "kind": a.kind,
                "kind_label": a.kind_label,
                "vendor": a.vendor,
                "vram_mb": a.dedicated_vram_mb,
                "driver": a.driver_version,
                "source": a.source,
                "active": a.runtime_visible,
            }
            for a in report.adapters
        ],
        "errors": list(report.errors),
    }
