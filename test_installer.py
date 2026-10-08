"""Installer upgrade-path verification.

Covers the behaviour the release installer is built on: an existing install is
found through the Windows uninstall registration, removed first, the new files
land at the location that registration recorded, the registration is rewritten
at the same key — and none of it may ever reach the user's data
(selections / settings.json / render_mode.json).

Everything runs against a throwaway registry root under
``Software\\PhotoCullerTest\\<random>`` (never the real uninstall key, so a run
cannot show up in 应用和功能) and against temp directories.

Run:  python test_installer.py
"""

from __future__ import annotations

import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import installer_app as inst  # noqa: E402
from config import APP_NAME, APP_VERSION  # noqa: E402

_TEST_ROOT = rf"Software\PhotoCullerTest\{uuid.uuid4().hex}\Uninstall"


def _delete_tree(key_path: str) -> None:
    """Best-effort removal of the throwaway test keys."""
    try:
        import winreg
    except ImportError:
        return
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ
        ) as key:
            subs = []
            index = 0
            while True:
                try:
                    subs.append(winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
    except OSError:
        return
    for sub in subs:
        _delete_tree(f"{key_path}\\{sub}")
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
    except OSError:
        pass


# --- 1. the generated uninstall script must never touch user data ------------
with tempfile.TemporaryDirectory(prefix="pc_temp_installer_") as tmp:
    root = Path(tmp)
    dest = root / "Programs" / "PhotoCuller"
    dest.mkdir(parents=True)
    data = root / "PhotoCuller"
    script = inst.uninstall_script(dest)

    assert str(dest) in script, "the script must delete the install dir"
    assert f"Remove-Item -LiteralPath '{dest}'" in script, script
    assert str(data) not in script, "the script must not name the user-data dir"
    assert "settings.json" in script and "render_mode.json" in script, (
        "the script documents what it deliberately keeps"
    )
    assert "用户数据" in script
    # Both registrations (ours and Inno's) are cleaned up.
    assert inst.OUR_KEY_NAME in script and inst.INNO_KEY_NAME in script
print("[1] uninstall script removes app files only; user data is documented as kept")

# --- 2. detection: nothing registered -> nothing found -----------------------
assert inst.find_installed_versions(root=_TEST_ROOT) == []
assert inst.read_installed(inst.OUR_KEY_NAME, root=_TEST_ROOT) is None
print("[2] an empty uninstall root reports no installed version")

# --- 3. registration round-trip ----------------------------------------------
with tempfile.TemporaryDirectory(prefix="pc_temp_installer_") as tmp:
    root = Path(tmp)
    dest = root / "install"
    dest.mkdir()
    (dest / "Photo Culler.exe").write_bytes(b"x")

    error = inst.write_installed_record(dest, root=_TEST_ROOT)
    assert error is None, error
    item = inst.read_installed(inst.OUR_KEY_NAME, root=_TEST_ROOT)
    assert item is not None, "the record must be readable after writing"
    assert item.display_name == APP_NAME
    assert item.display_version == APP_VERSION, item.display_version
    assert item.install_location == dest, item.install_location
    assert inst.UNINSTALL_SCRIPT_NAME in item.uninstall_string
    assert item.uninstall_string.startswith("powershell.exe")
    assert item.key_path == f"{_TEST_ROOT}\\{inst.OUR_KEY_NAME}"

    # An upgrade rewrites *the same key* — one entry, updated in place.
    dest2 = root / "install2"
    dest2.mkdir()
    assert inst.write_installed_record(dest2, root=_TEST_ROOT) is None
    items = inst.find_installed_versions(root=_TEST_ROOT)
    assert len(items) == 1, f"an upgrade must not create a second entry: {items}"
    assert items[0].install_location == dest2, items[0].install_location
    assert items[0].display_version == APP_VERSION

    # ...and the new install is directed to that recorded location.
    assert inst.preferred_install_dir(items) == dest2
    assert inst.preferred_install_dir([]) == inst.default_install_dir()
    gone = replace(items[0], install_location=root / "vanished")
    assert inst.preferred_install_dir([gone]) == inst.default_install_dir(), (
        "a stale location must not be offered"
    )
print("[3] registration round-trips; upgrade keeps one key; install targets it")

# --- 4. uninstall command parsing --------------------------------------------
assert inst._uninstall_argv("") is None
assert inst._uninstall_argv(r'"C:\definitely\gone\unins000.exe"') is None
assert inst._uninstall_argv("powershell.exe -File \"C:\\nope\\missing.ps1\"") is None

with tempfile.TemporaryDirectory(prefix="pc_temp_installer_") as tmp:
    root = Path(tmp)
    exe = root / "unins000.exe"
    exe.write_bytes(b"MZ")  # existence is all that is checked
    argv = inst._uninstall_argv(f'"{exe}" /SD x')
    assert argv is not None and argv[0] == str(exe), argv
    assert "/VERYSILENT" in argv, "an Inno uninstaller must be made silent"

    script = root / "uninstall.ps1"
    script.write_text("# stub", encoding="utf-8")
    argv = inst._uninstall_argv(
        f'powershell.exe -NoProfile -File "{script}"'
    )
    assert argv is not None and script.name in " ".join(argv), argv

    # A recorded command with no runnable target is skipped, not attempted.
    assert inst._uninstall_argv("missing.exe --uninstall") is None
print("[4] UninstallString parsing: silent Inno, existing script, else skip")

# --- 5. removal: old version gone, user data untouched -----------------------
with tempfile.TemporaryDirectory(prefix="pc_temp_installer_") as tmp:
    root = Path(tmp)
    old = root / "old_install"
    old.mkdir()
    (old / "Photo Culler.exe").write_bytes(b"x")
    (old / "payload.dll").write_bytes(b"x")

    bat = root / "unins000.bat"
    bat.write_text(
        '@echo off\r\nif not "%~1"=="" echo ran> "%~dp0ran.txt"\r\n',
        encoding="utf-8",
    )

    desktop_lnk = root / "Photo Culler.lnk"
    desktop_lnk.write_text("stub", encoding="utf-8")
    start_lnk = root / "Programs" / "Photo Culler.lnk"
    start_lnk.parent.mkdir()
    start_lnk.write_text("stub", encoding="utf-8")

    data = root / "PhotoCuller"
    data.mkdir()
    (data / "settings.json").write_text('{"accel_scheme": "discrete"}', encoding="utf-8")
    (data / "render_mode.json").write_text('{"mode": "discrete"}', encoding="utf-8")
    (data / "selections").mkdir()
    (data / "selections" / "a.json").write_text("{}", encoding="utf-8")

    item = inst.InstalledVersion(
        hive="HKCU",
        key_name=inst.OUR_KEY_NAME,
        display_name=APP_NAME,
        display_version="1.1.0",
        install_location=old,
        uninstall_string=f'"{bat}"',
        root=_TEST_ROOT,
    )
    assert inst.write_installed_record(old, root=_TEST_ROOT) is None

    ok, message = inst.remove_previous_versions(
        [item], shortcuts=[desktop_lnk, start_lnk], user_data=data
    )
    assert ok, message
    assert not old.exists(), "old program files must be gone"
    assert (root / "ran.txt").exists(), f"the old uninstaller never ran: {message}"
    assert not desktop_lnk.exists() and not start_lnk.exists(), "stale shortcuts"
    assert inst.read_installed(inst.OUR_KEY_NAME, root=_TEST_ROOT) is None, (
        "the old registration must be gone"
    )
    assert (data / "settings.json").exists(), "settings deleted!"
    assert (data / "render_mode.json").exists(), "render-mode cache deleted!"
    assert (data / "selections" / "a.json").exists(), "selections deleted!"
print("[5] upgrade removal: uninstaller ran, files/shortcuts/registry gone, data kept")

# --- 6. a stale registration (no uninstaller on disk) still cleans up ---------
with tempfile.TemporaryDirectory(prefix="pc_temp_installer_") as tmp:
    root = Path(tmp)
    stale = root / "leftovers"
    stale.mkdir()
    (stale / "old.exe").write_bytes(b"x")
    item = inst.InstalledVersion(
        hive="HKCU",
        key_name=inst.INNO_KEY_NAME,
        display_name=APP_NAME,
        display_version="1.0.1",
        install_location=stale,
        uninstall_string=f'"{root / "unins000.exe"}"',  # already deleted
        root=_TEST_ROOT,
    )
    ok, message = inst.remove_previous_versions(
        [item], shortcuts=[], user_data=root / "PhotoCuller"
    )
    assert ok, message
    assert not stale.exists(), "leftover files must still be removed"
print("[6] a registration whose uninstaller is missing is cleaned up anyway")

# --- 7. user data can never be reached, even from a hand-edited location ------
with tempfile.TemporaryDirectory(prefix="pc_temp_installer_") as tmp:
    root = Path(tmp)
    data = root / "PhotoCuller"
    data.mkdir()
    (data / "render_mode.json").write_text('{"mode": "software"}', encoding="utf-8")

    assert inst.touches_user_data(data, user_data=data)
    assert inst.touches_user_data(data / "sub", user_data=data)
    assert inst.touches_user_data(root, user_data=data), "an ancestor is unsafe too"
    assert not inst.touches_user_data(root / "Programs" / "PhotoCuller", user_data=data)

    # Install location set *to* the data dir: skipped, with a note.
    item = inst.InstalledVersion(
        hive="HKCU",
        key_name=inst.OUR_KEY_NAME,
        display_name=APP_NAME,
        display_version="1.1.0",
        install_location=data,
        uninstall_string="",
        root=_TEST_ROOT,
    )
    ok, message = inst.remove_previous_versions([item], shortcuts=[], user_data=data)
    assert ok, message
    assert (data / "render_mode.json").exists(), "user data was deleted!"
    assert "保留" in message, message

    # Install location set to the *parent* of the data dir: also refused.
    parent_item = replace(item, install_location=root, uninstall_string="")
    ok, message = inst.remove_previous_versions(
        [parent_item], shortcuts=[], user_data=data
    )
    assert ok and (data / "render_mode.json").exists(), message
    assert "保留" in message, message
print("[7] removal refuses any location that is / contains / is contained by user data")

# --- 8. misc live probes ------------------------------------------------------
assert isinstance(inst.is_app_running(), bool)
assert isinstance(inst.shortcut_paths(), list)
assert inst.default_install_dir().parent.name == "Programs", inst.default_install_dir()
print("[8] live probes (running process, shortcut folders) do not raise")

# --- cleanup -----------------------------------------------------------------
_delete_tree(_TEST_ROOT)
print()
print("INSTALLER UPGRADE TEST PASSED")
