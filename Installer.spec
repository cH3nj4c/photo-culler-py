# -*- mode: python ; coding: utf-8 -*-
"""Single-file installer that embeds the PyInstaller onedir payload."""

import sys
from pathlib import Path

block_cipher = None
project = Path(SPECPATH)
payload = project / "dist" / "PhotoCuller"

if not payload.is_dir():
    raise SystemExit("Run build_exe.bat first so dist/PhotoCuller exists.")

# The output filename carries the version, so nobody has to rename the setup
# binary by hand (which is how the app exe and the setup ended up looking like
# different releases). Both come from config.APP_VERSION via version_info.
sys.path.insert(0, str(project))
import version_info  # noqa: E402

installer_name = version_info.INSTALLER_BASENAME
version_file = version_info.ensure_version_file(
    project / "build",
    original_filename=f"{installer_name}.exe",
    file_description=f"{version_info.APP_NAME} 安装程序 {version_info.APP_VERSION}",
)

a = Analysis(
    [str(project / "installer_app.py")],
    pathex=[str(project)],
    binaries=[],
    datas=[(str(payload), "app_payload")],
    hiddenimports=["version_info"],
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
    name=installer_name,
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
    icon=str(project / "assets" / "app.ico"),
    version=version_file,
)
