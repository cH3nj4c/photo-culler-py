# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Photo Culler (Windows onedir)."""

import sys
from pathlib import Path

block_cipher = None
project = Path(SPECPATH)

# Stamp the Windows version resource (属性 → 详细信息) from config.APP_VERSION.
# Generated here rather than committed so the version has exactly one source of
# truth and cannot drift from the installer's filename.
sys.path.insert(0, str(project))
import version_info  # noqa: E402

version_file = version_info.ensure_version_file(
    project / "build",
    original_filename=version_info.APP_EXE_NAME,
    file_description=f"{version_info.APP_NAME} 照片选片工具",
)

# Sibling modules imported dynamically / from app.py — keep explicit.
hiddenimports = [
    "app",
    "config",
    "version_info",
    "domain",
    "gpu_info",
    "gpu_accel",
    "sysmon",
    "app_settings",
    "render_mode_cache",
    "imaging",
    "winshell",
    "selection_store",
    "jpeg_preloader",
    "image_loader",
    "preview_engine",
    "export_service",
    "workers",
    "sysmem",
    "ui",
    "thumbnail_service",
    "resample_backend",
    "jpeg_fast",
    "widgets",
    "temp_cleanup",
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.ttk",
]

# Optional GPU preview shell (qt_ui + gpu_preview, PySide6 + VisPy).
# Bundled only when the build venv has the packages; without them the app
# automatically falls back to the Tkinter shell at runtime.
_has_qt_ui = True
try:
    import PySide6  # noqa: F401
    import vispy  # noqa: F401
except ImportError:
    _has_qt_ui = False

datas = []
binaries = []

if _has_qt_ui:
    from PyInstaller.utils.hooks import collect_data_files, collect_submodules

    hiddenimports += ["qt_ui", "gpu_preview"]
    hiddenimports += collect_submodules("vispy")
    datas += collect_data_files("vispy")

# Bundle Tcl/Tk script trees from the base interpreter (venv may not copy them).
base = Path(sys.base_prefix)
tcl_root = base / "tcl"
if tcl_root.is_dir():
    for sub in ("tcl8.6", "tk8.6"):
        src = tcl_root / sub
        if src.is_dir():
            datas.append((str(src), sub))
    # Optional support packages
    for sub in ("dde1.4", "reg1.3"):
        src = tcl_root / sub
        if src.is_dir():
            datas.append((str(src), sub))

a = Analysis(
    [str(project / "app.py")],
    pathex=[str(project)],
    binaries=binaries,
    datas=datas + [(str(project / "assets" / "app.ico"), "assets"),
                   (str(project / "assets" / "logo.png"), "assets"),
                   (str(project / "assets" / "logo-mark.png"), "assets")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "scipy", "pandas", "IPython"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Photo Culler",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(project / "assets" / "app.ico"),
    version=version_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PhotoCuller",
)
