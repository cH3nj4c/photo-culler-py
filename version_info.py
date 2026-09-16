"""Derive every version-bearing artifact from ``config.APP_VERSION``.

One constant, several consumers:

- the **Windows version resource** stamped into both exes, written in the
  text format PyInstaller's ``EXE(version=...)`` expects (it ``eval``s the
  file, so we serialize PyInstaller's own ``VSVersionInfo`` object rather than
  hand-writing the structure — round-tripping through ``str()`` is what the
  loader is built for);
- the **installer's output filename**, so the setup binary is named
  ``Photo-Culler-Setup<version>.exe`` without anyone renaming it by hand;
- the **displayed version** in the UI and in ``--self-test``.

PyInstaller is imported lazily inside the functions: it is a build-time
dependency, and the running app only needs ``config.APP_VERSION``.
"""

from __future__ import annotations

import re
from pathlib import Path

from config import APP_NAME, APP_PUBLISHER, APP_VERSION

# Only the leading numeric core counts: "1.1.0" from "1.1.0", "1.1.0" from
# "1.1.0-beta2". Trailing text must not leak digits into the tuple — taking
# every digit in the string would turn "1.1.0-beta2" into (1, 1, 2, 0).
_VERSION_RE = re.compile(r"\s*(\d+(?:\.\d+)*)")

# Language/code-page id used in the version resource: 0x0804 (zh-CN) with
# code page 1200 (Unicode). Kept in one place so the StringTable key and the
# VarFileInfo translation cannot drift apart.
LANG_ID = 0x0804
CODEPAGE = 1200
_STRING_TABLE_KEY = f"{LANG_ID:04x}{CODEPAGE:04x}"

INTERNAL_NAME = "PhotoCuller"
APP_EXE_NAME = "Photo Culler.exe"
INSTALLER_BASENAME = f"Photo-Culler-Setup{APP_VERSION}"
APP_BASENAME = f"Photo-Culler-{APP_VERSION}"


def parse_version(text: str | None = None) -> tuple[int, int, int, int]:
    """``"1.1.0"`` → ``(1, 1, 0, 0)``.

    Windows version resources are fixed 4-tuples, so a shorter string is
    zero-padded. Only the leading numeric core is used, so a pre-release
    suffix is ignored rather than parsed: ``"1.1.0-beta2"`` → ``(1, 1, 0, 0)``,
    not ``(1, 1, 2, 0)``. Unparseable input yields all zeros instead of
    raising — a bad version string must not break the build.
    """
    source = APP_VERSION if text is None else text
    match = _VERSION_RE.match(str(source or ""))
    if not match:
        return (0, 0, 0, 0)
    parts = [int(chunk) for chunk in match.group(1).split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])  # type: ignore[return-value]


def build_version_info(
    *,
    original_filename: str = APP_EXE_NAME,
    file_description: str = f"{APP_NAME} 照片选片工具",
):
    """Build the PyInstaller ``VSVersionInfo`` for this release."""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    version = parse_version()
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=version,
            prodvers=version,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,  # VOS_NT_WINDOWS32
            fileType=0x1,  # VFT_APP
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        _STRING_TABLE_KEY,
                        [
                            StringStruct("CompanyName", APP_PUBLISHER),
                            StringStruct("FileDescription", file_description),
                            StringStruct("FileVersion", APP_VERSION),
                            StringStruct("InternalName", INTERNAL_NAME),
                            StringStruct("OriginalFilename", original_filename),
                            StringStruct("ProductName", APP_NAME),
                            StringStruct("ProductVersion", APP_VERSION),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [LANG_ID, CODEPAGE])]),
        ],
    )


def write_version_file(
    path: str | Path,
    *,
    original_filename: str = APP_EXE_NAME,
    file_description: str | None = None,
) -> str:
    """Write the version resource text file and return its path.

    The content is ``str(VSVersionInfo)`` because PyInstaller's loader
    ``eval``s the file back into the same structure.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"original_filename": original_filename}
    if file_description is not None:
        kwargs["file_description"] = file_description
    info = build_version_info(**kwargs)
    target.write_text(str(info), encoding="utf-8")
    return str(target)


def ensure_version_file(
    directory: str | Path,
    *,
    original_filename: str = APP_EXE_NAME,
    file_description: str | None = None,
) -> str:
    """Generate ``<directory>/version_info.txt``, returning its path.

    Called from the spec files so a build works whether it was started through
    ``build_*.bat`` or by invoking PyInstaller directly.
    """
    name = f"version_info_{Path(original_filename).stem.replace(' ', '_')}.txt"
    return write_version_file(
        Path(directory) / name,
        original_filename=original_filename,
        file_description=file_description,
    )


def describe() -> str:
    """One-line version summary for dialogs and ``--self-test``."""
    return f"{APP_NAME} {APP_VERSION}"


def about_text(*, renderer: str = "") -> str:
    """Body text for the 关于 dialog."""
    lines = [
        f"{APP_NAME}  {APP_VERSION}",
        "",
        "本地照片选片工具：只读源文件，导出时复制保留的照片。",
        "唯一的写操作是 Del（送进回收站，可恢复）。",
        "",
        f"版本：{APP_VERSION}",
        f"版本资源：{'.'.join(str(n) for n in parse_version())}",
    ]
    if renderer:
        lines.append(f"预览渲染器：{renderer}")
    lines.append(f"安装包名称：{INSTALLER_BASENAME}.exe")
    return "\n".join(lines)
