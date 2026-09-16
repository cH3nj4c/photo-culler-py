"""Selectable GPU acceleration schemes.

A *scheme* is a named, reversible way of steering which graphics hardware the
app uses. Each one is backed by a real mechanism — there is no option here
that cannot actually take effect:

- ``discrete`` / ``integrated`` write the **Windows per-app GPU preference**
  (``HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences``), which is the
  documented way to tell Windows which adapter a hybrid laptop should use for
  a given executable. Windows reads it when the process starts, so these take
  effect on the next launch.
- ``software`` switches to the Tkinter shell, whose preview is a CPU
  pipeline that never touches OpenGL, and pins the resampler to CPU.

Two options were investigated and deliberately **not** offered:

- An ANGLE / Direct3D backend: Qt 6 dropped ANGLE from its official builds
  and the DLLs are absent from the PySide6 wheel, so ``QT_OPENGL=angle``
  would silently do nothing.
- ``QT_OPENGL=software`` inside the Qt shell: Qt's bundled ``opengl32sw.dll``
  is Mesa 11.2 / GLSL 1.30, and VisPy's scene shaders need far more. Measured
  on this machine, the Qt shell fails to obtain a context at all under it
  (``stage.gpu_info`` comes back empty), so the "fallback" would break the
  preview instead of rescuing it. The Tk shell is the working CPU path.

The environment half of a scheme must be applied before ``QApplication``
exists, which is why ``apply_environment`` is called from ``app.py`` rather
than from inside the UI.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import app_settings
import gpu_info

# Windows per-app GPU preference values (Microsoft's documented encoding).
GPU_PREF_DEFAULT = 0
GPU_PREF_POWER_SAVING = 1
GPU_PREF_HIGH_PERFORMANCE = 2

_GPU_PREF_KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"

# Qt's own switch for selecting a rasteriser backend. Kept for the record of
# what was ruled out (see the module docstring); no scheme sets it.
ENV_QT_OPENGL = "QT_OPENGL"
ENV_ACCEL_SCHEME = "PHOTOCULLER_ACCEL"
# Consumed by app.py to pick the shell, and by resample_backend.ResampleService
# for the Tk shell's crop/resize backend.
ENV_UI = "PHOTOCULLER_UI"
ENV_RESAMPLE = "PHOTOCULLER_RESAMPLE"


@dataclass(frozen=True)
class Scheme:
    id: str
    label: str
    summary: str
    detail: str
    # Windows per-app GPU preference to write, or None to leave it alone.
    gpu_preference: int | None = None
    ui_shell: str | None = None
    resample_mode: str | None = None
    # Which detected adapters make this scheme meaningful.
    needs_discrete: bool = False
    needs_integrated: bool = False

    @property
    def restart_required(self) -> bool:
        """Every scheme here is read at process start, so all need a restart."""
        return True

    def environment(self) -> dict[str, str | None]:
        """Environment variables this scheme sets (None = remove).

        ``PHOTOCULLER_UI`` is only included when the scheme actually forces a
        shell. Emitting it as None for the other schemes would let startup
        delete a ``PHOTOCULLER_UI=tk`` the user exported on purpose.
        """
        env: dict[str, str | None] = {ENV_ACCEL_SCHEME: self.id}
        if self.ui_shell is not None:
            env[ENV_UI] = self.ui_shell
        env[ENV_RESAMPLE] = self.resample_mode
        return env


SCHEMES: tuple[Scheme, ...] = (
    Scheme(
        id="auto",
        label="自动（推荐）",
        summary="不覆盖系统设置，由 Windows 与驱动自行选择",
        detail=(
            "清除本应用之前写入的 GPU 偏好，恢复由 Windows 决定使用哪块显卡。"
            "混合显卡笔记本通常已默认把 3D 应用交给独显，因此先试这一项。"
        ),
        gpu_preference=GPU_PREF_DEFAULT,
    ),
    Scheme(
        id="discrete",
        label="独显优先（性能最强）",
        summary="强制把本应用交给独立显卡",
        detail=(
            "把本应用的 Windows 显卡偏好设为「高性能」，由独显渲染预览与缩放。"
            "耗电与发热更高，插电使用或处理大尺寸 RAW 时更合适。"
        ),
        gpu_preference=GPU_PREF_HIGH_PERFORMANCE,
        needs_discrete=True,
    ),
    Scheme(
        id="integrated",
        label="核显优先（省电）",
        summary="交给集成显卡，降低功耗与发热",
        detail=(
            "把本应用的 Windows 显卡偏好设为「省电」，由核显渲染。"
            "电池续航更好，但高像素照片的缩放会更吃力。"
        ),
        gpu_preference=GPU_PREF_POWER_SAVING,
        needs_integrated=True,
    ),
    Scheme(
        id="software",
        label="兼容模式（纯 CPU 渲染）",
        summary="改用 CPU 预览管线，完全不依赖显卡驱动",
        detail=(
            "切换到 CPU 渲染的界面层并关闭 GPU 重采样，不走 OpenGL 与显卡驱动。"
            "显卡驱动异常、远程桌面、虚拟机、或独显/核显切换出问题时用这一项；"
            "代价是缩放明显变慢，且界面为较朴素的版本。"
        ),
        ui_shell="tk",
        resample_mode="cpu",
    ),
)

SCHEME_BY_ID = {s.id: s for s in SCHEMES}
DEFAULT_SCHEME = "auto"


def get_scheme(scheme_id: str | None) -> Scheme:
    return SCHEME_BY_ID.get(scheme_id or "", SCHEME_BY_ID[DEFAULT_SCHEME])


def scheme_availability(
    scheme: Scheme, report: gpu_info.GpuReport | None = None
) -> tuple[bool, str]:
    """Can this scheme do anything useful on this machine?

    Returns ``(available, reason_when_not)``. A scheme is only blocked when it
    names hardware the machine does not have — offering "独显优先" on a laptop
    with no discrete GPU would otherwise look like it did something.
    """
    if report is None:
        return True, ""
    if scheme.needs_discrete and not report.has_discrete:
        return False, "未检测到独立显卡"
    if scheme.needs_integrated and not report.has_integrated:
        return False, "未检测到集成显卡"
    return True, ""


# --- persistence ------------------------------------------------------------

def current_scheme_id() -> str:
    """The scheme stored for the next launch."""
    stored = app_settings.get_value("accel_scheme", DEFAULT_SCHEME)
    return stored if stored in SCHEME_BY_ID else DEFAULT_SCHEME


def effective_scheme_id() -> str:
    """The scheme this process is actually running under.

    ``PHOTOCULLER_ACCEL`` wins when it is set, because that is what Qt and the
    resampler were actually configured from at startup — ``apply_environment``
    honours an externally-set override rather than stomping it. Settings only
    describe the *next* launch, so a UI that read them alone could claim "自动"
    while the process was rendering in software.
    """
    override = (os.environ.get(ENV_ACCEL_SCHEME) or "").strip().lower()
    if override in SCHEME_BY_ID:
        return override
    return current_scheme_id()


def set_scheme_id(scheme_id: str) -> str | None:
    if scheme_id not in SCHEME_BY_ID:
        return f"未知的加速方案：{scheme_id}"
    return app_settings.save_settings({"accel_scheme": scheme_id})


# --- environment ------------------------------------------------------------

def apply_environment(scheme_id: str | None = None) -> Scheme:
    """Set the scheme's environment variables in this process.

    Must run before QApplication is constructed — Qt reads ``QT_OPENGL`` while
    it initialises its platform plugin, and VisPy reads its context from Qt.
    Defaults to the *effective* scheme so a manually exported
    ``PHOTOCULLER_ACCEL`` survives startup instead of being overwritten.
    """
    scheme = get_scheme(
        scheme_id if scheme_id is not None else effective_scheme_id()
    )
    for key, value in scheme.environment().items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return scheme


def force_software_opengl_requested() -> bool:
    """Always False; kept so callers cannot silently reintroduce the idea.

    ``QT_OPENGL=software`` was measured to break the Qt shell's preview (see
    the module docstring), so no scheme uses it. The Tk shell is the CPU path.
    """
    return False


def active_resample_mode() -> str | None:
    """Resample mode implied by the running scheme (None = let it decide)."""
    return get_scheme(effective_scheme_id()).resample_mode


# --- Windows per-app GPU preference -----------------------------------------

def target_executable() -> str | None:
    """Path whose GPU preference we may edit, or None when that is unsafe.

    Only the frozen build has an executable of its own. Under a source run the
    executable is shared ``python.exe``, and writing a preference for it would
    re-route *every* Python program on the machine — so we refuse.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe = sys.executable
    if not exe or not os.path.isfile(exe):
        return None
    return os.path.abspath(exe)


def read_gpu_preference(exe_path: str | None = None) -> int | None:
    """Current stored preference, or None when nothing is set / unreadable."""
    path = exe_path or target_executable()
    if not path:
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GPU_PREF_KEY) as key:
            raw, _ = winreg.QueryValueEx(key, path)
    except OSError:
        return None
    return _parse_preference(raw)


def _parse_preference(raw) -> int | None:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if chunk.lower().startswith("gpupreference="):
            try:
                return int(chunk.split("=", 1)[1])
            except (IndexError, ValueError):
                return None
    return None


def write_gpu_preference(preference: int | None, exe_path: str | None = None) -> tuple[bool, str]:
    """Apply a per-app GPU preference. Reversible; HKCU only.

    ``preference=None`` or ``GPU_PREF_DEFAULT`` removes the value, restoring
    Windows' own decision. Returns ``(ok, message)``.
    """
    path = exe_path or target_executable()
    if not path:
        return False, (
            "源码运行时无法设置显卡偏好：那会作用于共用的 python.exe，"
            "影响机器上所有 Python 程序。安装版中此项可直接生效。"
        )
    try:
        import winreg
    except ImportError:
        return False, "此平台不支持 Windows 显卡偏好设置。"

    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            _GPU_PREF_KEY,
            0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        ) as key:
            if preference in (None, GPU_PREF_DEFAULT):
                try:
                    winreg.DeleteValue(key, path)
                except FileNotFoundError:
                    pass
                return True, "已清除本应用的显卡偏好，恢复由 Windows 决定。"
            winreg.SetValueEx(key, path, 0, winreg.REG_SZ, f"GpuPreference={preference};")
    except OSError as exc:
        return False, f"写入显卡偏好失败：{exc}"
    label = {
        GPU_PREF_HIGH_PERFORMANCE: "高性能（独显）",
        GPU_PREF_POWER_SAVING: "省电（核显）",
    }.get(preference, str(preference))
    return True, f"已把本应用的 Windows 显卡偏好设为「{label}」，重启后生效。"


def apply_scheme(scheme_id: str) -> tuple[bool, str]:
    """Persist the choice and push whatever can be applied right now.

    Returns ``(ok, message)``. ``ok`` is False only when nothing happened at
    all. A scheme whose Windows preference cannot be written (a source run has
    no executable of its own) still counts as selected — the choice is stored
    and the environment is updated — but the message says plainly which part
    was skipped and why, so the user is never told a no-op succeeded.
    """
    scheme = get_scheme(scheme_id)
    if scheme_id not in SCHEME_BY_ID:
        return False, f"未知的加速方案：{scheme_id}"

    error = set_scheme_id(scheme_id)
    if error:
        return False, error

    for key, value in scheme.environment().items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    # An explicit choice replaces any earlier forced shell, so drop a stale
    # `tk` pin left over from switching away from the compatibility scheme.
    if scheme.ui_shell is None:
        os.environ.pop(ENV_UI, None)

    caveats: list[str] = []
    if scheme.gpu_preference is not None:
        applied, message = write_gpu_preference(scheme.gpu_preference)
        if not applied:
            caveats.append(message)
    notes = caveats + ["渲染方式将在下次启动时生效。"]
    return True, " ".join(notes)


def describe_effect(scheme_id: str, report: gpu_info.GpuReport | None = None) -> str:
    """Human-readable statement of what a scheme will do on this machine."""
    scheme = get_scheme(scheme_id)
    available, reason = scheme_availability(scheme, report)
    if not available:
        return f"不可用：{reason}"
    if scheme.id == "software":
        return "改用 CPU 预览管线（不依赖 OpenGL）"
    if scheme.gpu_preference == GPU_PREF_HIGH_PERFORMANCE:
        target = report.discrete.name if report and report.discrete else "独立显卡"
        return f"由 {target} 渲染"
    if scheme.gpu_preference == GPU_PREF_POWER_SAVING:
        target = report.integrated.name if report and report.integrated else "集成显卡"
        return f"由 {target} 渲染"
    return "由 Windows / 驱动自行选择"


def stored_preference_label() -> str:
    """Short description of the preference actually on disk right now."""
    if target_executable() is None:
        return "源码运行（未设置）"
    value = read_gpu_preference()
    if value is None:
        return "未设置"
    return {
        GPU_PREF_HIGH_PERFORMANCE: "高性能（独显）",
        GPU_PREF_POWER_SAVING: "省电（核显）",
        GPU_PREF_DEFAULT: "由 Windows 决定",
    }.get(value, f"未知（{value}）")
