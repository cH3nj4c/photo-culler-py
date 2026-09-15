# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Photo Culler (Windows onedir)."""

from pathlib import Path

block_cipher = None
project = Path(SPECPATH)

# Sibling modules imported dynamically / from app.py — keep explicit.
hiddenimports = [
    "app",
    "config",
    "domain",
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

datas = []
binaries = []

# Bundle Tcl/Tk script trees from the base interpreter (venv may not copy them).
import sys

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
