"""Version plumbing verification.

The project previously had no version number at all, which is how a freshly
built exe still "looked old": nothing on disk distinguished one build from
another. These checks pin the single source of truth (``config.APP_VERSION``)
to every consumer, so a bump cannot silently miss one of them.

The most valuable check is the last one: it reads the version resource back out
of the *built* exe, which is the only way to catch "the source says 1.1.0 but
the artifact I am about to ship says something else".

Run:  python test_version.py
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import version_info  # noqa: E402

PROJECT = Path(__file__).resolve().parent

# --- 1. the constant itself --------------------------------------------------
assert re.fullmatch(r"\d+(\.\d+){1,3}", config.APP_VERSION), config.APP_VERSION
assert config.APP_NAME and config.APP_PUBLISHER
parsed = version_info.parse_version()
assert parsed == tuple(int(p) for p in config.APP_VERSION.split(".")) + (0,) * (
    4 - len(config.APP_VERSION.split(".")
)
), (parsed, config.APP_VERSION)
assert all(isinstance(n, int) and n >= 0 for n in parsed)
print(f"[1] config.APP_VERSION = {config.APP_VERSION} -> {parsed}")

# --- 2. parsing edge cases (pure) -------------------------------------------
cases = [
    ("1.1.0", (1, 1, 0, 0)),
    ("2.10.3", (2, 10, 3, 0)),
    ("1", (1, 0, 0, 0)),
    ("1.1.0.4", (1, 1, 0, 4)),
    # A pre-release suffix must not leak its digits into the tuple: taking
    # every digit in the string would turn this into (1, 1, 2, 0).
    ("1.1.0-beta2", (1, 1, 0, 0)),
    ("1.2.0-rc1", (1, 2, 0, 0)),
    ("", (0, 0, 0, 0)),
    ("x.y", (0, 0, 0, 0)),
    (None, parsed),
]
for text, expected in cases:
    got = version_info.parse_version(text)
    assert got == expected, f"{text!r} -> {got}, expected {expected}"
assert version_info.parse_version("99.99.99.99.99") == (99, 99, 99, 99), "must clamp to 4"
print(f"[2] {len(cases)} parse cases OK (including pre-release suffixes)")

# --- 3. the resource carries the same version --------------------------------
info = version_info.build_version_info()
strings = {s.name: s.val for s in info.kids[0].kids[0].kids}
assert strings["ProductVersion"] == config.APP_VERSION, strings
assert strings["FileVersion"] == config.APP_VERSION, strings
assert strings["ProductName"] == config.APP_NAME, strings
assert strings["CompanyName"] == config.APP_PUBLISHER, strings
assert strings["OriginalFilename"] == version_info.APP_EXE_NAME, strings
assert strings["FileDescription"], "a missing FileDescription shows as blank in 属性"
translation = info.kids[1].kids[0]
assert translation.name == "Translation", translation.name
assert list(translation.kids) == [version_info.LANG_ID, version_info.CODEPAGE], (
    translation.kids
)
print("[3] version resource strings match config.APP_VERSION")

# --- 4. generated file round-trips through PyInstaller's own loader ----------
from PyInstaller.utils.win32.versioninfo import load_version_info_from_text_file  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="pc-ver-"))
path = version_info.ensure_version_file(
    tmp, original_filename=version_info.APP_EXE_NAME
)
loaded = load_version_info_from_text_file(path)
assert isinstance(loaded, type(info)), type(loaded)
reloaded = {s.name: s.val for s in loaded.kids[0].kids[0].kids}
assert reloaded == strings, (reloaded, strings)
# Same call with a different filename must not clobber the app's file, since
# the app exe and the setup binary are stamped in separate spec runs.
other = version_info.ensure_version_file(tmp, original_filename="Photo-Culler-Setup.exe")
assert other != path and Path(other).exists() and Path(path).exists()
print(f"[4] generated resource round-trips through the PyInstaller loader")

# --- 5. derived names --------------------------------------------------------
assert config.APP_VERSION in version_info.INSTALLER_BASENAME
assert version_info.INSTALLER_BASENAME.startswith("Photo-Culler-Setup")
for name in (version_info.INSTALLER_BASENAME, version_info.APP_BASENAME):
    assert not any(ch in name for ch in '\\/:*?"<>|'), name
print(f"[5] installer name = {version_info.INSTALLER_BASENAME}.exe")

# --- 6. every consumer actually references the constant ----------------------
# Guards against a consumer being reverted to a hardcoded version.
for spec in ("PhotoCuller.spec", "Installer.spec"):
    text = (PROJECT / spec).read_text(encoding="utf-8")
    assert "version_info" in text, f"{spec} does not derive the version"
    assert "version=version_file" in text, f"{spec} does not stamp the version resource"
assert "APP_VERSION" in (PROJECT / "qt_ui.py").read_text(encoding="utf-8")
assert "APP_VERSION" in (PROJECT / "ui.py").read_text(encoding="utf-8")
assert "APP_VERSION" in (PROJECT / "app.py").read_text(encoding="utf-8")
assert "APP_VERSION" in (PROJECT / "installer_app.py").read_text(encoding="utf-8")
assert config.APP_VERSION in version_info.about_text()
print("[6] specs, both shells, self-test, installer and 关于 all use the constant")

# --- 7. the optional Inno Setup path must not drift -------------------------
iss = (PROJECT / "installer.iss").read_text(encoding="utf-8")
match = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', iss)
assert match, "installer.iss lost its MyAppVersion define"
assert match.group(1) == config.APP_VERSION, (
    f"installer.iss says {match.group(1)} but config.APP_VERSION is "
    f"{config.APP_VERSION} — sync them"
)
print(f"[7] installer.iss MyAppVersion matches ({match.group(1)})")

# --- 8. the built exe actually carries it -----------------------------------
exe = PROJECT / "dist" / "PhotoCuller" / version_info.APP_EXE_NAME
if not exe.exists():
    print("[8] no dist exe yet (run build_exe.bat); artifact check skipped")
else:
    try:
        import pefile
    except ImportError:
        print("[8] pefile unavailable; artifact check skipped")
    else:
        pe = pefile.PE(str(exe), fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
        )
        found = {}
        for group in getattr(pe, "FileInfo", []):
            for entry in group:
                if getattr(entry, "Key", b"") == b"StringFileInfo":
                    for table in entry.StringTable:
                        for key, value in table.entries.items():
                            found[key.decode()] = value.decode()
        pe.close()
        assert found, (
            f"{exe.name} has no version resource at all — 属性 → 详细信息 "
            "would show nothing, which is what made a fresh build look old"
        )
        assert found.get("ProductVersion") == config.APP_VERSION, (
            f"dist\\PhotoCuller is stale: it says {found.get('ProductVersion')!r} "
            f"but config.APP_VERSION is {config.APP_VERSION!r}. "
            "Re-run build_exe.bat (and build_installer.bat)."
        )
        print(
            f"[8] built exe carries version {found.get('ProductVersion')} "
            f"(FileVersion={found.get('FileVersion')})"
        )

        # A matching version is not enough: the exe can carry the right number
        # and still predate the source. Compare build time against the newest
        # *bundled* source file, which is the failure that actually happens
        # (edit code, forget to rebuild, ship the old bundle). Tests and
        # benchmarks are excluded — they never enter the bundle, so editing one
        # is not a reason to rebuild.
        exe_time = exe.stat().st_mtime
        bundled = [
            p for p in PROJECT.glob("*.py")
            if not p.name.startswith(("test_", "bench_"))
        ]
        newest = max(((p.stat().st_mtime, p.name) for p in bundled), default=(0.0, ""))
        assert exe_time >= newest[0], (
            f"dist is older than the source: {newest[1]} was modified after the "
            f"exe was built. Re-run build_exe.bat."
        )
        print(f"[8b] exe is newer than every bundled source (newest: {newest[1]})")

        # Version and timestamp both check out — but neither proves the bundle
        # is *runnable*. A build killed midway through PyInstaller's
        # "Removing dir dist\PhotoCuller" step (which is what a sandboxed
        # file-deletion shim or an interrupted session does) leaves an onedir
        # whose files have been partly deleted: the exe can still be the current
        # one while its DLLs are gone. Running --self-test is the only check that
        # actually exercises the bundle, and it is also the check that catches a
        # missing Tk runtime, which is how the packaged Tk shell silently breaks.
        result = subprocess.run(
            [str(exe), "--self-test"],
            capture_output=True,
            timeout=180,
        )
        stdout = result.stdout.decode("utf-8", "replace")
        stderr = result.stderr.decode("utf-8", "replace")
        assert result.returncode == 0, (
            f"the packaged exe failed its own self-test (exit {result.returncode}). "
            f"dist may be a half-deleted build — re-run build_exe.bat.\n"
            f"stdout: {stdout[:400]}\nstderr: {stderr[:400]}"
        )
        # It must also agree with the constant it was stamped with.
        assert config.APP_VERSION in stdout, stdout[:400]
        first_line = stdout.strip().splitlines()[0] if stdout.strip() else ""
        print(f"[8c] packaged exe runs: {first_line!r} (--self-test exit 0)")

print("VERSION TEST PASSED")
