# -*- mode: python ; coding: utf-8 -*-
"""Single-file installer that embeds the PyInstaller onedir payload."""

from pathlib import Path

block_cipher = None
project = Path(SPECPATH)
payload = project / "dist" / "PhotoCuller"

if not payload.is_dir():
    raise SystemExit("Run build_exe.bat first so dist/PhotoCuller exists.")

a = Analysis(
    [str(project / "installer_app.py")],
    pathex=[str(project)],
    binaries=[],
    datas=[(str(payload), "app_payload")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["rawpy", "numpy", "PIL", "matplotlib"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Photo-Culler-Setup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
