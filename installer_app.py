"""Photo Culler Windows installer (no third-party setup toolkit required)."""

from __future__ import annotations

import os
import shutil
import sys
import traceback
from pathlib import Path
from tkinter import messagebox, ttk
import tkinter as tk


def payload_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "app_payload"
    return Path(__file__).resolve().parent / "dist" / "PhotoCuller"


def default_install_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else (Path.home() / "AppData" / "Local")
    return base / "Programs" / "PhotoCuller"


def copy_payload(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)


def create_shortcuts(exe: Path, desktop: bool, start_menu: bool) -> None:
    import subprocess

    ps = f"""
$exe = '{exe}'
$name = 'Photo Culler'
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


class InstallerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("安装 Photo Culler")
        self.resizable(False, False)
        self.configure(bg="#17191d")
        self.dest = default_install_dir()
        self._desktop = tk.BooleanVar(value=True)
        self._startmenu = tk.BooleanVar(value=True)

        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Photo Culler 安装程序", font=("Segoe UI", 14, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            frame,
            text="将复制程序文件到本机，并创建快捷方式。\n源照片不会被修改。",
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(8, 12))

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
        try:
            self.status.configure(text="正在复制文件…")
            self.update_idletasks()
            copy_payload(src, dest)
            exe = dest / "Photo Culler.exe"
            if not exe.exists():
                messagebox.showerror("Photo Culler", f"安装不完整，缺少：\n{exe}")
                return
            self.status.configure(text="正在创建快捷方式…")
            self.update_idletasks()
            create_shortcuts(
                exe, desktop=self._desktop.get(), start_menu=self._startmenu.get()
            )
            self.status.configure(text="安装完成")
            messagebox.showinfo(
                "Photo Culler",
                f"安装完成。\n\n{exe}\n\n是否现在启动？",
            )
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
