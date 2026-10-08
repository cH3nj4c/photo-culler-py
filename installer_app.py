"""Photo Culler Windows installer (no third-party setup toolkit required).

Upgrade path — the reason this file is more than "copy files":

The installer registers the app under the standard Windows uninstall key, so
the *next* run can find what is already on the machine. Installing therefore:

1. reads ``HKCU`` / ``HKLM`` ``Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall``
   for both its own key and the one the optional Inno Setup script writes;
2. if something is installed, **removes that version first** — runs its
   ``UninstallString`` when that target still exists, then deletes leftover
   files, shortcuts and the registration entries;
3. installs the new files **at the location the old registration recorded**
   (pre-filled in the dialog, still editable) and rewrites the registration at
   the *same* registry key with the new version.

User data is out of bounds throughout: selections, ``settings.json`` and the
render-mode cache live in ``%LOCALAPPDATA%\\PhotoCuller`` while the program
lives in ``%LOCALAPPDATA%\\Programs\\PhotoCuller`` — and even a hand-edited
install location cannot change that, because both removal and the copy step
refuse any directory that *is* or *contains* the user-data directory.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
import tkinter as tk

from config import APP_NAME, APP_PUBLISHER, APP_VERSION
from version_info import APP_EXE_NAME, INSTALLER_BASENAME

# Standard per-user / per-machine uninstall registration (Windows "应用和功能").
UNINSTALL_KEY_ROOT = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
# Our key, plus the one installer.iss (Inno Setup, AppId + "_is1") writes —
# users may have installed through either path and both count as installed.
OUR_KEY_NAME = APP_NAME
INNO_KEY_NAME = "{8F3C2A91-5B6E-4D2A-9C41-PhotoCuller01}_is1"
CANDIDATE_KEY_NAMES = (OUR_KEY_NAME, INNO_KEY_NAME)
UNINSTALL_SCRIPT_NAME = "uninstall.ps1"

# hive label -> winreg constant name
_HIVE_NAMES = {"HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE"}


def payload_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "app_payload"
    return Path(__file__).resolve().parent / "dist" / "PhotoCuller"


def user_data_dir() -> Path:
    """%LOCALAPPDATA%\\PhotoCuller — selections, settings, render-mode cache."""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else (Path.home() / "AppData" / "Local")
    return base / "PhotoCuller"


def default_install_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else (Path.home() / "AppData" / "Local")
    return base / "Programs" / "PhotoCuller"


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def touches_user_data(path: Path, user_data: Path | None = None) -> bool:
    """Would acting on ``path`` reach the user's data?

    True when ``path`` *is* the user-data directory, lies inside it, or is an
    ancestor of it (deleting an ancestor would take the data with it). Used to
    refuse both the upgrade cleanup and the payload copy in those cases.
    """
    data = _resolve(user_data or user_data_dir())
    target = _resolve(path)
    return target == data or target in data.parents or data in target.parents


# --- installed-version detection --------------------------------------------

@dataclass(frozen=True)
class InstalledVersion:
    hive: str  # "HKCU" | "HKLM"
    key_name: str
    display_name: str
    display_version: str
    install_location: Path | None
    uninstall_string: str
    root: str = UNINSTALL_KEY_ROOT

    @property
    def key_path(self) -> str:
        return f"{self.root}\\{self.key_name}"


def _hive_handle(hive: str):
    try:
        import winreg
    except ImportError:
        return None
    return getattr(winreg, _HIVE_NAMES.get(hive, hive), None)


def _read_value(key, name: str):
    try:
        import winreg

        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def read_installed(
    key_name: str, *, hive: str = "HKCU", root: str = UNINSTALL_KEY_ROOT
) -> InstalledVersion | None:
    """One uninstall registration, or None when the key is absent/unreadable."""
    handle = _hive_handle(hive)
    if handle is None:
        return None
    try:
        import winreg

        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(handle, f"{root}\\{key_name}", 0, access) as key:
            display = _as_text(_read_value(key, "DisplayName"))
            version = _as_text(_read_value(key, "DisplayVersion"))
            uninstall = _as_text(_read_value(key, "UninstallString"))
            location = _as_text(_read_value(key, "InstallLocation"))
    except OSError:
        return None
    return InstalledVersion(
        hive=hive,
        key_name=key_name,
        display_name=(display or key_name).strip(),
        display_version=version,
        install_location=Path(location) if location else None,
        uninstall_string=uninstall,
        root=root,
    )


def _as_text(raw) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-16-le", "replace").rstrip("\x00")
    return str(raw or "").strip() if raw is not None else ""


def find_installed_versions(
    *,
    hives: tuple[str, ...] = ("HKCU", "HKLM"),
    root: str = UNINSTALL_KEY_ROOT,
) -> list[InstalledVersion]:
    """Every existing registration, most specific first (HKCU before HKLM)."""
    found: list[InstalledVersion] = []
    for hive in hives:
        for key_name in CANDIDATE_KEY_NAMES:
            item = read_installed(key_name, hive=hive, root=root)
            if item is not None:
                found.append(item)
    return found


def preferred_install_dir(installed: list[InstalledVersion] | None = None) -> Path:
    """Where to install: the recorded location of an existing install, else default."""
    items = find_installed_versions() if installed is None else installed
    for item in items:
        location = item.install_location
        if location and location.is_dir():
            return location
    return default_install_dir()


def _dir_size_kb(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                total += child.stat().st_size
    except OSError:
        return 0
    return total // 1024


def write_installed_record(
    install_dir: Path,
    *,
    hive: str = "HKCU",
    root: str = UNINSTALL_KEY_ROOT,
    key_name: str = OUR_KEY_NAME,
) -> str | None:
    """(Re)register the app for 应用和功能. Error message or None.

    Always the same key name, so an upgrade overwrites the old record in place
    — Windows sees one entry, not one per version.
    """
    handle = _hive_handle(hive)
    if handle is None:
        return "此平台不支持写入注册表"
    script = install_dir / UNINSTALL_SCRIPT_NAME
    uninstall = (
        f'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
        f'-File "{script}"'
    )
    values: list[tuple[str, object, str]] = [
        ("DisplayName", APP_NAME, "REG_SZ"),
        ("DisplayVersion", APP_VERSION, "REG_SZ"),
        ("Publisher", APP_PUBLISHER, "REG_SZ"),
        ("InstallLocation", f"{install_dir}", "REG_SZ"),
        ("UninstallString", uninstall, "REG_SZ"),
        ("QuietUninstallString", uninstall, "REG_SZ"),
        ("InstallDate", time.strftime("%Y%m%d"), "REG_SZ"),
        ("EstimatedSize", _dir_size_kb(install_dir), "REG_DWORD"),
        ("NoModify", 1, "REG_DWORD"),
        ("NoRepair", 1, "REG_DWORD"),
    ]
    try:
        import winreg

        with winreg.CreateKeyEx(
            handle,
            f"{root}\\{key_name}",
            0,
            winreg.KEY_SET_VALUE | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as key:
            for name, data, kind in values:
                reg_type = winreg.REG_DWORD if kind == "REG_DWORD" else winreg.REG_SZ
                winreg.SetValueEx(key, name, 0, reg_type, data)
    except OSError as exc:
        return f"写入注册表失败：{exc}"
    return None


def remove_installed_record(item: InstalledVersion) -> str | None:
    """Drop one registration entry. Error message or None (best effort)."""
    handle = _hive_handle(item.hive)
    if handle is None:
        return "此平台不支持删除注册表项"
    try:
        import winreg

        winreg.DeleteKey(handle, item.key_path)
    except OSError as exc:
        return f"无法删除注册项 {item.key_name}：{exc}"
    return None


# --- uninstall command -------------------------------------------------------

def _split_command(text: str) -> list[str]:
    try:
        return shlex.split(text, posix=False)
    except ValueError:
        return text.split()


def _clean(token: str) -> str:
    return token.strip().strip('"')


def _target_exists(exe: str) -> bool:
    path = Path(exe)
    if path.is_absolute():
        return path.exists()
    return shutil.which(exe) is not None


def _uninstall_argv(command: str) -> list[str] | None:
    """Argv to run an existing ``UninstallString``, or None when there is none.

    Handles the two shapes seen in the wild: an Inno ``unins000.exe`` (made
    silent explicitly — its recorded command is interactive) and our own
    ``powershell … -File <script>`` (already silent; only run when the script
    still exists, otherwise powershell would just error out).
    """
    text = (command or "").strip()
    if not text:
        return None
    tokens = _split_command(text)
    if not tokens:
        return None

    lowered = [_clean(t).lower() for t in tokens]
    if "-file" in lowered:
        index = lowered.index("-file")
        if index + 1 >= len(tokens):
            return None
        script = _clean(tokens[index + 1])
        if not Path(script).exists():
            return None
        return [_clean(t) for t in tokens]

    exe = _clean(tokens[0])
    if not _target_exists(exe):
        return None
    if Path(exe).name.lower().startswith("unins"):
        return [exe, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
    return [_clean(t) for t in tokens]


def _run_uninstall_command(argv: list[str]) -> str | None:
    """Run it. Batch files need cmd; everything else goes direct. Error or None."""
    if Path(argv[0]).suffix.lower() in (".bat", ".cmd"):
        argv = ["cmd", "/c", *argv]
    try:
        subprocess.run(argv, check=False, capture_output=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"旧卸载程序运行失败：{exc}"
    return None


# --- shortcuts ---------------------------------------------------------------

def shortcut_paths() -> list[Path]:
    """The per-user shortcuts this installer creates (best effort)."""
    ps = (
        "[Environment]::GetFolderPath('Desktop'); "
        "[Environment]::GetFolderPath('StartMenu')"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=False, capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    folders = [Path(line.strip()) for line in out.splitlines() if line.strip()]
    if len(folders) >= 2:
        folders[1] = folders[1] / "Programs"
    return [folder / f"{APP_NAME}.lnk" for folder in folders]


def remove_shortcuts(paths: list[Path] | None = None) -> None:
    for path in (shortcut_paths() if paths is None else paths):
        try:
            path.unlink()
        except OSError:
            pass


def is_app_running() -> bool:
    ps = "if (Get-Process -Name 'Photo Culler' -ErrorAction SilentlyContinue) { echo run }"
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=False, capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "run" in out


# --- removal -----------------------------------------------------------------

def remove_previous_versions(
    items: list[InstalledVersion],
    *,
    shortcuts: list[Path] | None = None,
    user_data: Path | None = None,
) -> tuple[bool, str]:
    """Remove old versions. Returns ``(ok, message)``.

    User data is never deleted: a directory that is the user-data directory or
    contains it is skipped with a note instead. ``ok`` is False only when old
    program files are left behind (locked), because copying over them would
    produce a mixed install.
    """
    data = _resolve(user_data or user_data_dir())
    notes: list[str] = []
    problems: list[str] = []
    for item in items:
        argv = _uninstall_argv(item.uninstall_string)
        if argv:
            error = _run_uninstall_command(argv)
            if error:
                problems.append(error)
            else:
                notes.append(f"已运行旧版本卸载程序（{item.display_version or item.key_name}）")

        location = item.install_location
        if location is not None:
            target = _resolve(location)
            if target == data or target in data.parents or data in target.parents:
                notes.append(f"保留 {location}（与用户数据目录重合）")
            elif target.exists():
                shutil.rmtree(target, ignore_errors=True)
                if target.exists():
                    problems.append(f"无法删除旧版本文件：{target}")

        record_error = remove_installed_record(item)
        if record_error:
            notes.append(record_error)

    remove_shortcuts(shortcuts)

    if problems:
        return False, "；".join(problems + notes)
    return True, "；".join(notes) or "未发现旧版本残留"


# --- uninstall script (written into the install dir) ------------------------

def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def uninstall_script(dest: Path) -> str:
    """PowerShell body for ``UninstallString`` — app files only, never user data.

    Generated rather than shipped, so it always matches the real install path
    and key names. The whole file is parsed by PowerShell before it runs, so
    deleting the directory that contains it is safe.
    """
    key_paths = "\n".join(
        f"Remove-Item -Recurse -Force -LiteralPath {_ps_quote(f'HKCU:{root}\\\\{name}')}"
        for root, name in (
            (UNINSTALL_KEY_ROOT, OUR_KEY_NAME),
            (UNINSTALL_KEY_ROOT, INNO_KEY_NAME),
        )
    )
    return f"""# {APP_NAME} 卸载脚本（由安装程序生成，{APP_VERSION}）
# 只删除程序文件、快捷方式与注册信息。
# %LOCALAPPDATA%\\PhotoCuller 中的选片记录、settings.json、render_mode.json
# 等用户数据不会被删除 —— 这是有意为之。
$ErrorActionPreference = 'SilentlyContinue'
Remove-Item -LiteralPath {_ps_quote(dest)} -Recurse -Force
$desktop = [Environment]::GetFolderPath('Desktop')
$programs = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
Remove-Item -LiteralPath (Join-Path $desktop {_ps_quote(f'{APP_NAME}.lnk')}) -Force
Remove-Item -LiteralPath (Join-Path $programs {_ps_quote(f'{APP_NAME}.lnk')}) -Force
{key_paths}
"""


def write_uninstall_script(dest: Path) -> Path:
    path = dest / UNINSTALL_SCRIPT_NAME
    path.write_text(uninstall_script(dest), encoding="utf-8")
    return path


# --- file copy / shortcuts ---------------------------------------------------

def copy_payload(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)


def create_shortcuts(exe: Path, desktop: bool, start_menu: bool) -> None:
    ps = f"""
$exe = '{exe}'
$name = '{APP_NAME}'
$ws = New-Object -ComObject WScript.Shell
"""
    if desktop:
        ps += f"""
$d = [Environment]::GetFolderPath('Desktop')
$s = $ws.CreateShortcut((Join-Path $d ($name + '.lnk')))
$s.TargetPath = $exe
$s.WorkingDirectory = Split-Path $exe
$s.Save()
"""
    if start_menu:
        ps += f"""
$sm = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
New-Item -ItemType Directory -Force -Path $sm | Out-Null
$s2 = $ws.CreateShortcut((Join-Path $sm ($name + '.lnk')))
$s2.TargetPath = $exe
$s2.WorkingDirectory = Split-Path $exe
$s2.Save()
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        check=False,
        capture_output=True,
    )


# --- UI ----------------------------------------------------------------------

class InstallerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"安装 {APP_NAME} {APP_VERSION}")
        self.resizable(False, False)
        self.configure(bg="#17191d")
        self.installed = find_installed_versions()
        self.dest = preferred_install_dir(self.installed)
        self._desktop = tk.BooleanVar(value=True)
        self._startmenu = tk.BooleanVar(value=True)

        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame, text=f"{APP_NAME} 安装程序", font=("Segoe UI", 14, "bold")
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=f"版本 {APP_VERSION}　·　安装包 {INSTALLER_BASENAME}.exe\n"
            "将复制程序文件到本机，并创建快捷方式。\n源照片不会被修改。",
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(8, 12))

        if self.installed:
            versions = "、".join(
                dict.fromkeys(i.display_version or "未知版本" for i in self.installed)
            )
            ttk.Label(
                frame,
                text=f"检测到已安装版本 {versions}：将先移除旧版本，再装到原位置。\n"
                "选片记录、设置与渲染模式缓存等用户数据会保留。",
                foreground="#4f9cff",
                font=("Segoe UI", 9),
            ).pack(anchor="w", pady=(0, 8))

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="安装到：").pack(side="left")
        self.path_var = tk.StringVar(value=str(self.dest))
        ent = ttk.Entry(row, textvariable=self.path_var, width=48)
        ent.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(row, text="浏览…", command=self._browse).pack(side="left")

        ttk.Checkbutton(frame, text="创建桌面快捷方式", variable=self._desktop).pack(
            anchor="w", pady=2
        )
        ttk.Checkbutton(
            frame, text="创建开始菜单快捷方式", variable=self._startmenu
        ).pack(anchor="w", pady=2)

        self.status = ttk.Label(frame, text="", foreground="#4f9cff")
        self.status.pack(anchor="w", pady=(12, 4))

        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="安装", command=self._install).pack(side="right")
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=8)

    def _browse(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(title="选择安装文件夹", initialdir=str(self.dest))
        if chosen:
            self.path_var.set(chosen)

    def _install(self) -> None:
        dest = Path(self.path_var.get().strip())
        src = payload_root()
        if not src.is_dir():
            messagebox.showerror("Photo Culler", f"找不到程序文件：\n{src}")
            return
        if touches_user_data(dest):
            messagebox.showerror(
                "Photo Culler",
                f"安装目录不能是（或包含）用户数据目录：\n{user_data_dir()}\n\n"
                "否则安装过程会连同选片记录、设置与渲染模式缓存一起删除。",
            )
            return

        installed = self.installed or find_installed_versions()
        if installed:
            if is_app_running():
                messagebox.showwarning(
                    "Photo Culler",
                    "Photo Culler 正在运行，请先退出程序再安装。",
                )
                return
            versions = "、".join(
                dict.fromkeys(i.display_version or i.key_name for i in installed)
            )
            self.status.configure(text=f"正在移除旧版本（{versions}）…")
            self.update_idletasks()
            ok, message = remove_previous_versions(installed)
            if not ok:
                messagebox.showerror(
                    "Photo Culler",
                    f"无法移除旧版本：\n{message}\n\n"
                    "请确认 Photo Culler 已退出、没有文件被占用，然后重试。",
                )
                return
        try:
            self.status.configure(text="正在复制文件…")
            self.update_idletasks()
            copy_payload(src, dest)
            exe = dest / APP_EXE_NAME
            if not exe.exists():
                messagebox.showerror("Photo Culler", f"安装不完整，缺少：\n{exe}")
                return
            self.status.configure(text="正在注册卸载信息…")
            self.update_idletasks()
            write_uninstall_script(dest)
            registry_error = write_installed_record(dest)
            self.status.configure(text="正在创建快捷方式…")
            self.update_idletasks()
            create_shortcuts(
                exe, desktop=self._desktop.get(), start_menu=self._startmenu.get()
            )
            self.status.configure(text="安装完成")
            body = f"安装完成（{APP_NAME} {APP_VERSION}）。\n\n{exe}\n"
            if installed:
                body += "\n已移除旧版本并安装到注册表记录的位置。"
            body += "\n用户数据（选片记录 / 设置 / 渲染模式缓存）已保留。"
            if registry_error:
                body += f"\n\n注意：{registry_error}（应用和功能列表中可能看不到本程序）"
            messagebox.showinfo(f"{APP_NAME} {APP_VERSION}", body + "\n\n是否现在启动？")
            os.startfile(exe)  # noqa: S606
            self.destroy()
        except Exception as exc:
            traceback.print_exc()
            messagebox.showerror("Photo Culler", f"安装失败：\n{exc}")


def main() -> None:
    try:
        InstallerApp().mainloop()
    except Exception as error:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Photo Culler", f"安装程序无法启动：\n{error}")
            root.destroy()
        except Exception:
            print(error)


if __name__ == "__main__":
    main()
