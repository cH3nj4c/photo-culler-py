"""Photo Culler - a small Windows-first photo selection application.

The app deliberately copies selected originals on export; it never moves,
renames, or edits the source photographs.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import math
import os
import queue
import shutil
import sys
import tkinter as tk
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread


def configure_bundled_tk_runtime() -> None:
    """Point the packaged app at its own complete Tcl/Tk runtime before tkinter imports."""
    if not getattr(sys, 'frozen', False):
        return
    bundle = sys._MEIPASS
    os.environ['TCL_LIBRARY'] = str(Path(bundle) / 'tcl' / 'tcl8.6')
    os.environ['TK_LIBRARY'] = str(Path(bundle) / 'tcl' / 'tk8.6')
    if hasattr(os, 'add_dll_directory'):
        _tk_dll_directory = os.add_dll_directory(str(Path(bundle) / 'bin'))


_tk_dll_directory: object | None = None
configure_bundled_tk_runtime()

from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageOps, ImageTk, UnidentifiedImageError

try:
    import rawpy
except ImportError:
    rawpy = None

APP_NAME = 'Photo Culler'
SUPPORTED_EXTENSIONS = {'.tiff', '.tif', '.png', '.jpeg', '.jpg', '.dng'}
THUMB_WIDTH = 132
THUMB_HEIGHT = 88
THUMB_SLOT = 148
THUMB_CACHE_LIMIT = 110
JPEG_EXTENSIONS = {'.jpg', '.jpeg'}
JPEG_CACHE_LIMIT = 60
JPEG_PRELOAD_AHEAD = 20
JPEG_PRELOAD_BEHIND = 10
PREVIEW_OVERSCAN = 0.72
PREVIEW_OVERSCAN_MAX_PX = 560
PREVIEW_INTERACTIVE_DELAY_MS = 24
PREVIEW_QUALITY_DELAY_MS = 150

# 应用核心约定：
# 1. 只读取照片，不移动/重命名源文件；
# 2. RAW+JPG 可视为同一条“筛选项”，依赖 pair_modes 控制导出策略；
# 3. 预览和缩略图采用缓存 + 后台线程解码，以保证交互流畅。


def enable_windows_high_dpi() -> None:
    """Opt out of Windows bitmap scaling so Tk is rendered sharply on HiDPI monitors.

    Must be called before any Tk window (or other top-level HWND / COM) is created.
    Tries, in order of preference:
      1. Per-Monitor V2    (DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)   -> Win10 1703+
      2. Per-Monitor       (SetProcessDpiAwareness, PM)                   -> Win8.1+
      3. System            (SetProcessDPIAware, System aware)             -> Win7/Vista
    Returns True if one of the calls succeeded, False otherwise.
    """
    if sys.platform != 'win32':
        return False
    user32 = ctypes.windll.user32
    ctypes.windll.kernel32.SetLastError(0)

    # 1. Per-Monitor V2 — best quality: each monitor scales independently (no blur on mixed-DPI).
    try:
        SetProcessDpiAwarenessContext = user32.SetProcessDpiAwarenessContext
        SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        if SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except (AttributeError, OSError):
        pass

    # 2. Per-Monitor (pre-Win10) — still no bitmap stretch within each monitor.
    try:
        SetProcessDpiAwareness = ctypes.windll.shcore.SetProcessDpiAwareness
        SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        SetProcessDpiAwareness.restype = ctypes.c_long
        if SetProcessDpiAwareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            return True
    except (AttributeError, OSError):
        pass

    # 3. System aware — last resort; avoids the worst bitmap scaling.
    try:
        SetProcessDPIAware = user32.SetProcessDPIAware
        SetProcessDPIAware.restype = ctypes.c_bool
        if SetProcessDPIAware():
            return True
    except (AttributeError, OSError):
        pass

    return False


@dataclass(frozen=True)
class PhotoGroup:
    """One culling decision, optionally made of a DNG and its JPEG preview."""

    key: str
    primary: Path
    members: tuple[Path, ...]

    @property
    def paired_raw_jpeg(self) -> bool:
        return (any(p.suffix.lower() == '.dng' for p in self.members)
                and any(p.suffix.lower() in JPEG_EXTENSIONS for p in self.members))


@dataclass(frozen=True)
class PreviewGeometry:
    """The source region and on-canvas position for one preview frame."""

    source_box: tuple[int, int, int, int]
    target_size: tuple[int, int]
    origin: tuple[float, float]
    downsample_factor: int


def build_photo_groups(paths: list[Path]) -> list[PhotoGroup]:
    """Hide DNG + JPEG pairs behind one culling item, without grouping unrelated files."""
    by_stem = {}
    for path in paths:
        by_stem.setdefault(path.stem.casefold(), []).append(path)

    result = []
    for same_name_paths in by_stem.values():
        ordered = sorted(same_name_paths, key=lambda path: path.name.casefold())
        raws = [path for path in ordered if path.suffix.lower() == '.dng']
        jpegs = [path for path in ordered if path.suffix.lower() in JPEG_EXTENSIONS]
        paired_members = tuple(raws + jpegs)
        if raws and jpegs:
            primary = jpegs[0]
            key = 'pair|' + str(primary.parent.resolve()).casefold() + '|' + primary.stem.casefold()
            result.append(PhotoGroup(key=key, primary=primary, members=paired_members))
            paired_paths = set(paired_members)
            for path in ordered:
                if path not in paired_paths:
                    result.append(PhotoGroup(key=str(path.resolve()), primary=path, members=(path,)))
        else:
            for path in ordered:
                result.append(PhotoGroup(key=str(path.resolve()), primary=path, members=(path,)))

    return sorted(result, key=lambda item: item.primary.name.casefold())


class PhotoCuller(tk.Tk):
    def __init__(self) -> None:
        # 关键顺序：必须先声明进程级 DPI 感知（Per-Monitor V2），再创建任何 Tk 窗口，
        # 否则 Windows 会对整个界面做位图拉伸，造成文字/控件模糊、分辨率偏低。
        enable_windows_high_dpi()
        # Tk 窗口初始化：再校准 DPI 缩放，最后建立 UI 与交互状态。
        super().__init__()
        self.title(APP_NAME)
        self._configure_dpi_layout()
        self.configure(bg="#17191d")

        # 当前打开的文件夹与所有筛选项。
        self.folder = None
        self.all_items = []
        self.index = 0
        self.kept = set()
        self.pair_modes = {}
        self.show_kept_only = tk.BooleanVar(value=False)

        # 预览区的核心状态：当前 image、缩放位移、拖拽状态和后台渲染任务。
        self.preview_photo = None
        self.preview_image_item = None
        self._preview_item_origin = None
        self._preview_item_size = None
        self.current_source_image = None
        self.current_source_path = None
        self.zoom_scale = 1.0
        self.fit_scale = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._drag_state = None
        self._interactive_render_job = None
        self._quality_render_job = None
        self._preview_render_generation = 0
        self._preview_render_events = queue.Queue()
        self._preview_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="photo-culler-preview")
        self._preview_futures = set()
        self._preview_levels = {}
        self._preview_levels_lock = Lock()

        # 缩略图和 JPEG 预加载缓存：保持内存使用在可控范围内。
        self.thumbnail_cache = OrderedDict()
        self.jpeg_cache = OrderedDict()
        self._jpeg_tombstones = set()
        self._jpeg_cache_lock = Lock()
        self._preload_events = queue.Queue()
        self._preload_generation = 0
        self._preload_done = True
        self._resize_job = None

        self._make_style()
        self._build_ui()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._preview_poll_job = self.after(16, self._poll_preview_render_events)
        self.after(250, self.open_folder)

    def _configure_dpi_layout(self) -> None:
        """Scale geometry and raster thumbnail dimensions to the monitor's real DPI."""
        monitor_dpi = self.winfo_fpixels("1i")
        self.ui_scale = max(1.0, monitor_dpi / 96.0)
        self.tk.call("tk", "scaling", monitor_dpi / 72.0)
        self.thumb_width = self._px(THUMB_WIDTH)
        self.thumb_height = self._px(THUMB_HEIGHT)
        self.thumb_slot = self._px(THUMB_SLOT)
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        width = min(self._px(1280), int(screen_width * 0.94))
        height = min(self._px(820), int(screen_height * 0.88))
        self.geometry(f"{width}x{height}")
        self.minsize(min(self._px(880), screen_width), min(self._px(620), screen_height))

    def _px(self, logical_pixels: int) -> int:
        return max(1, round(logical_pixels * self.ui_scale))

    def _make_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#17191d")
        style.configure("Toolbar.TFrame", background="#202329")
        style.configure("App.TLabel", background="#17191d", foreground="#e7e9ed")
        style.configure("Muted.TLabel", background="#17191d", foreground="#a8adb7")
        style.configure("Header.TLabel", background="#202329", foreground="#e7e9ed", font=("Segoe UI", 10, "bold"))
        style.configure("Zoom.TLabel", background="#202329", foreground="#8bd7ff", font=("Segoe UI", 10, "bold"))
        style.configure("App.TButton", font=("Segoe UI", 10), padding=(11, 7))
        style.configure("Keep.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 7))
        style.configure("App.TCheckbutton", background="#202329", foreground="#e7e9ed", font=("Segoe UI", 10))
        style.map("App.TCheckbutton", background=[("active", "#202329")], foreground=[("active", "#ffffff")])

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, style="Toolbar.TFrame", padding=(16, 10))
        toolbar.pack(fill="x")

        ttk.Button(toolbar, text="打开照片文件夹  O", style="App.TButton", command=self.open_folder).pack(side="left")
        self.folder_label = ttk.Label(toolbar, text="尚未打开文件夹", style="Header.TLabel")
        self.folder_label.pack(side="left", padx=(14, 0))

        ttk.Button(toolbar, text="导出保留照片  E", style="App.TButton", command=self.export_kept).pack(side="right")
        ttk.Button(toolbar, text="适合屏幕  Z", style="App.TButton", command=self.zoom_fit).pack(side="right", padx=(0, 6))
        ttk.Button(toolbar, text="100%  1", style="App.TButton", command=self.zoom_actual).pack(side="right", padx=(0, 6))
        self.zoom_label = ttk.Label(toolbar, text="适合屏幕", style="Zoom.TLabel")
        self.zoom_label.pack(side="right", padx=(0, 12))
        ttk.Button(toolbar, text="重置模式", style="App.TButton", command=self.reset_all_pair_modes).pack(side="right", padx=(0, 6))
        ttk.Button(toolbar, text="全不保留", style="App.TButton", command=self.clear_all_kept).pack(side="right", padx=(0, 6))
        self.keep_mode_button = ttk.Button(toolbar, text="模式：单文件", style="App.TButton", command=self.cycle_keep_mode)
        self.keep_mode_button.pack(side="right", padx=(0, 6))
        ttk.Button(toolbar, text="保留 / 取消  Space", style="Keep.TButton", command=self.toggle_keep).pack(side="right", padx=(0, 10))
        ttk.Checkbutton(toolbar, text="只看保留", variable=self.show_kept_only, style="App.TCheckbutton", command=self.toggle_filter).pack(side="right", padx=(0, 16))

        self.preview_frame = tk.Frame(self, bg="#111317", highlightthickness=0)
        self.preview_frame.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        self.preview_canvas = tk.Canvas(self.preview_frame, bg="#111317", highlightthickness=0, cursor="arrow")
        self.preview_canvas.pack(fill="both", expand=True)
        self.preview_canvas.create_text(0, 0, text="打开一个照片文件夹开始选片", fill="#bdc3cd", font=("Segoe UI", 16), tags="preview-message")
        self.preview_canvas.bind("<Configure>", self._queue_preview_resize)
        self.preview_canvas.bind("<MouseWheel>", self._preview_mouse_wheel)
        self.preview_canvas.bind("<ButtonPress-1>", self._preview_drag_start)
        self.preview_canvas.bind("<B1-Motion>", self._preview_drag_motion)
        self.preview_canvas.bind("<ButtonRelease-1>", self._preview_drag_end)

        info = ttk.Frame(self, style="App.TFrame", padding=(18, 5))
        info.pack(fill="x")
        self.status_label = ttk.Label(info, text="", style="App.TLabel")
        self.status_label.pack(side="left")
        self.preload_label = ttk.Label(info, text="", style="Muted.TLabel")
        self.preload_label.pack(side="left", padx=(18, 0))
        self.help_label = ttk.Label(info, text="[ ] 切换 · Space 保留 · F 模式 · 滚轮缩放 · Z 适合/100% · + − 微调", style="Muted.TLabel")
        self.help_label.pack(side="right")

        thumbs_container = tk.Frame(self, bg="#202329", height=self._px(132))
        thumbs_container.pack(fill="x", padx=16, pady=(0, 16))
        thumbs_container.pack_propagate(False)
        self.thumb_canvas = tk.Canvas(thumbs_container, bg="#202329", highlightthickness=0, height=self._px(132))
        self.thumb_scrollbar = ttk.Scrollbar(thumbs_container, orient="horizontal", command=self.thumb_canvas.xview)
        self.thumb_canvas.configure(xscrollcommand=self.thumb_scrollbar.set)
        self.thumb_canvas.pack(fill="both", expand=True)
        self.thumb_scrollbar.pack(fill="x")
        self.thumb_canvas.bind("<Button-1>", self._thumbnail_clicked)
        self.thumb_canvas.bind("<MouseWheel>", self._scroll_thumbnails)
        self.thumb_canvas.bind("<Configure>", lambda _event: self._render_thumbnails())

    def _bind_keys(self) -> None:
        self.bind_all("<bracketleft>", lambda _event: self.change_index(-1))
        self.bind_all("<bracketright>", lambda _event: self.change_index(1))
        self.bind_all("<space>", self._on_space)
        self.bind_all("<f>", self._on_mode_key)
        self.bind_all("<F>", self._on_mode_key)
        self.bind_all("<o>", lambda _event: self.open_folder())
        self.bind_all("<O>", lambda _event: self.open_folder())
        self.bind_all("<e>", lambda _event: self.export_kept())
        self.bind_all("<E>", lambda _event: self.export_kept())
        self.bind_all("<z>", lambda _event: self.toggle_zoom())
        self.bind_all("<Z>", lambda _event: self.toggle_zoom())
        self.bind_all("<Key-1>", lambda _event: self.zoom_actual())
        self.bind_all("<plus>", lambda _event: self.zoom_step(1))
        self.bind_all("<KP_Add>", lambda _event: self.zoom_step(1))
        self.bind_all("<minus>", lambda _event: self.zoom_step(-1))
        self.bind_all("<KP_Subtract>", lambda _event: self.zoom_step(-1))
        self.bind_all("<Control-Shift-x>", self._on_clear_all_shortcut)
        self.bind_all("<Control-Shift-m>", self._on_reset_modes_shortcut)

    def _on_space(self, _event: tk.Event) -> str:
        widget = _event.widget
        widget_class = widget.winfo_class() if hasattr(widget, "winfo_class") else ""
        if widget_class in {"Checkbutton", "TButton", "TCheckbutton", "Button"}:
            return "break"
        self.toggle_keep()
        return "break"

    def _on_mode_key(self, _event: tk.Event) -> str:
        self.cycle_keep_mode()
        return "break"

    def _on_clear_all_shortcut(self, _event: tk.Event) -> str:
        self.clear_all_kept()
        return "break"

    def _on_reset_modes_shortcut(self, _event: tk.Event) -> str:
        self.reset_all_pair_modes()
        return "break"

    @property
    def visible_items(self) -> list[PhotoGroup]:
        if not self.show_kept_only.get():
            return self.all_items
        return [item for item in self.all_items if item.key not in self.kept]

    @staticmethod
    def _pair_mode_label(mode: str) -> str:
        return {"both": "RAW+JPG", "raw": "仅 RAW", "jpg": "仅 JPG"}.get(mode, "RAW+JPG")

    def _pair_mode(self, item: PhotoGroup) -> str:
        mode = self.pair_modes.get(item.key, "both")
        if mode in {"both", "jpg", "raw"}:
            return mode
        return "both"

    def _update_keep_mode_ui(self) -> None:
        item = self.current_item
        if item is not None and item.paired_raw_jpeg:
            self.keep_mode_button.configure(text=f"模式：{self._pair_mode_label(self._pair_mode(item))}  F")
            self.keep_mode_button.state(["!disabled"])
            return
        self.keep_mode_button.configure(text="模式：单文件  F")
        self.keep_mode_button.state(["disabled"])

    @property
    def current_item(self) -> PhotoGroup | None:
        items = self.visible_items
        if not items:
            return None
        self.index = min(max(self.index, 0), len(items) - 1)
        return items[self.index]

    def open_folder(self) -> None:
        # 打开文件夹后：
        # - 过滤出受支持格式；
        # - 按文件名建立分组（RAW+JPG 成组）；
        # - 还原历史保留状态与导出模式；
        # - 重新更新 UI 与缓存。
        chosen = filedialog.askdirectory(title="选择包含照片的文件夹", initialdir=str(self.folder) if self.folder else None)
        if not chosen:
            return
        folder = Path(chosen)
        try:
            paths = sorted(
                (path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS),
                key=lambda path: path.name.casefold(),
            )
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法读取这个文件夹：\n{exc}")
            return
        self.folder = folder
        self.all_items = build_photo_groups(paths)
        self.index = 0
        self.thumbnail_cache.clear()
        with self._jpeg_cache_lock:
            self.jpeg_cache.clear()
            self._jpeg_tombstones.clear()
        self._ensure_jpeg_window(self.index)
        saved, saved_pair_modes = self._load_selection()
        current_keys = {item.key for item in self.all_items}
        self.kept = saved.intersection(current_keys)
        pair_keys = {item.key for item in self.all_items if item.paired_raw_jpeg}
        self.pair_modes = {
            key: mode
            for key, mode in saved_pair_modes.items()
            if key in pair_keys and mode in {"both", "jpg", "raw"}
        }
        self.folder_label.configure(text=folder.name if folder.name else str(folder))
        if not self.all_items:
            self.current_source_image = None
            self.current_source_path = None
            self._show_preview_message("这个文件夹中没有受支持的照片")
            self._set_status("支持 JPG、JPEG、PNG、TIFF、DNG")
            self._update_keep_mode_ui()
            self._render_thumbnails()
            return
        self._show_current(center=True)

    def change_index(self, direction: int) -> None:
        items = self.visible_items
        if not items:
            return
        new_index = self.index + direction
        if 0 <= new_index < len(items):
            self.index = new_index
            self._show_current(center=True)

    def toggle_keep(self) -> None:
        item = self.current_item
        if item is None:
            return
        key = item.key
        if key in self.kept:
            self.kept.remove(key)
        else:
            self.kept.add(key)
        if item.paired_raw_jpeg:
            self.pair_modes.setdefault(key, "both")
        self._save_selection()
        if self.show_kept_only.get() and key not in self.kept:
            items = self.visible_items
            if not items:
                self.index = 0
                self._show_preview_message("没有保留的照片")
                self._set_status("保留 0 张照片")
                self._render_thumbnails()
                return
            self.index = min(self.index, len(items) - 1)
        self._show_current(center=False, reset_zoom=False)

    def cycle_keep_mode(self) -> None:
        item = self.current_item
        if item is None or not item.paired_raw_jpeg:
            return
        modes = ["both", "raw", "jpg"]
        current = self._pair_mode(item)
        self.pair_modes[item.key] = modes[(modes.index(current) + 1) % len(modes)]
        self._save_selection()
        self._update_keep_mode_ui()
        self._set_status(self._status_text(item))
        self._render_thumbnails(center=False)

    def clear_all_kept(self) -> None:
        if not self.kept:
            messagebox.showinfo(APP_NAME, "当前没有已保留的照片。")
            return
        answer = messagebox.askyesno(APP_NAME, f"确定要取消全部 {len(self.kept)} 个保留项目吗？\n\n各组的 RAW/JPG 模式不会改变。")
        if not answer:
            return
        self.kept.clear()
        self._save_selection()
        if self.show_kept_only.get():
            self.show_kept_only.set(False)
        self.index = min(self.index, max(len(self.visible_items) - 1, 0))
        if self.visible_items:
            self._show_current(center=True, reset_zoom=False)
            return
        self._show_preview_message("打开一个照片文件夹开始选片")
        self._update_keep_mode_ui()
        self._render_thumbnails()

    def reset_all_pair_modes(self) -> None:
        pair_items = [item for item in self.all_items if item.paired_raw_jpeg]
        if not pair_items:
            messagebox.showinfo(APP_NAME, "当前文件夹没有 RAW+JPG 绑定组。")
            return
        answer = messagebox.askyesno(APP_NAME, f"确定要将 {len(pair_items)} 个 RAW+JPG 绑定组的模式全部重置为 RAW+JPG 吗？\n\n各组的保留/不保留状态不会改变。")
        if not answer:
            return
        self.pair_modes = {item.key: "both" for item in pair_items}
        self._save_selection()
        self._update_keep_mode_ui()
        self._set_status(self._status_text(self.current_item))
        self._render_thumbnails(center=False)

    def toggle_filter(self) -> None:
        active = self.current_item
        items = self.visible_items
        if active is not None and active in items:
            self.index = items.index(active)
        else:
            self.index = 0
        if items:
            self._show_current(center=True)
        else:
            self._show_preview_message("没有保留的照片")
            self._set_status("保留 0 张照片")
            self._update_keep_mode_ui()
            self._render_thumbnails()

    def _show_current(self, center, reset_zoom=True):
        # 切到新照片时，先清理旧的预览状态，再重新解码当前图片并安排后台重绘。
        item = self.current_item
        if item is None:
            return
        path = item.primary
        self._cancel_preview_jobs()
        self._set_status('正在载入：' + path.name)
        self.update_idletasks()
        try:
            if self.current_source_path != path or self.current_source_image is None:
                with self._preview_levels_lock:
                    self._preview_levels.clear()
                self.current_source_image = self._load_image(path, thumbnail=False)
                self.current_source_path = path
                self._render_preview(reset_zoom=reset_zoom, interactive=True)
                self._schedule_preview_render(interactive=False, quality_delay=90)
        except Exception as exc:
            self.current_source_image = None
            self.current_source_path = None
            self._show_preview_message(f'无法显示\n{path.name}\n\n{exc}')
        self._set_status(self._status_text(item))
        self._update_keep_mode_ui()
        self._render_thumbnails(center=center)
        self._ensure_jpeg_window(self.index)

    def _load_image(self, path, thumbnail):
        if path.suffix.lower() != '.dng':
            if path.suffix.lower() in JPEG_EXTENSIONS:
                key = str(path.resolve())
                with self._jpeg_cache_lock:
                    cached = self.jpeg_cache.get(key)
                    if cached is not None:
                        self.jpeg_cache.move_to_end(key)
                        return cached
                image = self._read_raster_image(path)
                with self._jpeg_cache_lock:
                    self.jpeg_cache[key] = image
                    self.jpeg_cache.move_to_end(key)
                    while len(self.jpeg_cache) > JPEG_CACHE_LIMIT:
                        evicted_key, _ = self.jpeg_cache.popitem(last=False)
                        self._jpeg_tombstones.add(evicted_key)
                return image
            return self._read_raster_image(path)
        if rawpy is None:
            raise RuntimeError('DNG 支持组件未安装')
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    with io.BytesIO(thumb.data) as embedded:
                        return ImageOps.exif_transpose(Image.open(embedded)).convert('RGB').copy()
                return Image.fromarray(thumb.data).convert('RGB')
            except Exception:
                array = raw.postprocess(use_camera_wb=True, no_auto_bright=False, half_size=True, output_bps=8)
                return Image.fromarray(array).convert('RGB')

    @staticmethod
    def _read_raster_image(path):
        """Decode a normal image once and detach it from its file handle."""
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened)
            return image.convert('RGB').copy()

    @staticmethod
    def _fit_for_display(image, max_width, max_height):
        """Return a display-sized copy without modifying an image held in the JPEG memory cache."""
        scale = min(max_width / image.width, max_height / image.height, 1.0)
        if scale >= 1.0:
            return image
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        return image.resize(size, Image.Resampling.LANCZOS)

    def _show_preview_message(self, message):
        self._cancel_preview_jobs()
        self.preview_photo = None
        self.preview_image_item = None
        self._preview_item_origin = None
        self._preview_item_size = None
        self._drag_state = None
        self.preview_canvas.delete('all')
        width = max(self.preview_canvas.winfo_width(), 1)
        height = max(self.preview_canvas.winfo_height(), 1)
        self.preview_canvas.create_text(
            width / 2, height / 2,
            text=message, fill='#bdc3cd', font=('Segoe UI', 16), justify='center')
        self.preview_canvas.configure(cursor='arrow')
        self.zoom_label.configure(text='—')

    def _render_preview(self, reset_zoom, interactive=False):
        """Render one immediate frame; deferred frames use the background worker."""
        image = self.current_source_image
        path = self.current_source_path
        if image is None or path is None:
            return
        geometry = self._preview_geometry(reset_zoom=reset_zoom, interactive=interactive)
        frame = self._build_preview_frame(image, path, geometry, interactive)
        self._apply_preview_frame(frame, geometry)

    def _preview_geometry(self, reset_zoom, interactive):
        """Calculate an oversized source crop so normal drags need no rerender."""
        image = self.current_source_image
        if image is None:
            raise RuntimeError('没有可显示的照片')
        canvas_width = max(self.preview_canvas.winfo_width(), 1)
        canvas_height = max(self.preview_canvas.winfo_height(), 1)
        previous_fit = self.fit_scale
        new_fit = min(canvas_width / image.width, canvas_height / image.height, 1.0)
        was_at_fit = abs(self.zoom_scale - previous_fit) < 0.0001
        self.fit_scale = new_fit
        if reset_zoom or was_at_fit:
            self.zoom_scale = new_fit
            self.pan_x = 0.0
            self.pan_y = 0.0
        else:
            self.zoom_scale = min(4.0, self.zoom_scale)
            self._constrain_pan(canvas_width, canvas_height)
        display_width = image.width * self.zoom_scale
        display_height = image.height * self.zoom_scale
        left = canvas_width / 2 + self.pan_x - display_width / 2
        top = canvas_height / 2 + self.pan_y - display_height / 2
        overscan = min(max(canvas_width, canvas_height) * PREVIEW_OVERSCAN, PREVIEW_OVERSCAN_MAX_PX)
        source_left = max(0, math.floor((-overscan - left) / self.zoom_scale))
        source_top = max(0, math.floor((-overscan - top) / self.zoom_scale))
        source_right = min(image.width, math.ceil((canvas_width + overscan - left) / self.zoom_scale))
        source_bottom = min(image.height, math.ceil((canvas_height + overscan - top) / self.zoom_scale))
        if source_right <= source_left or source_bottom <= source_top:
            raise RuntimeError('无法显示这个缩放区域')
        factor = self._interactive_downsample_factor(image) if interactive else 1
        level_left = max(0, source_left // factor)
        level_top = max(0, source_top // factor)
        level_right = min(math.ceil(image.width / factor), math.ceil(source_right / factor))
        level_bottom = min(math.ceil(image.height / factor), math.ceil(source_bottom / factor))
        if level_right <= level_left or level_bottom <= level_top:
            raise RuntimeError('无法显示这个缩放区域')
        target_width = max(1, round((level_right - level_left) * self.zoom_scale * factor))
        target_height = max(1, round((level_bottom - level_top) * self.zoom_scale * factor))
        return PreviewGeometry(
            source_box=(level_left, level_top, level_right, level_bottom),
            target_size=(target_width, target_height),
            origin=(left + level_left * factor * self.zoom_scale,
                    top + level_top * factor * self.zoom_scale),
            downsample_factor=factor,
        )

    def _interactive_downsample_factor(self, image):
        """Choose a pyramid level close to screen resolution for responsive input."""
        factor = 1
        max_factor = min(16, max(1, min(image.width, image.height)))
        while factor * 2 <= max_factor and self.zoom_scale * factor * 2 <= 1.0:
            factor *= 2
        return factor

    def _preview_source_for(self, image, path, factor, interactive):
        if factor == 1:
            return image
        key = (str(path.resolve()), factor)
        with self._preview_levels_lock:
            cached = self._preview_levels.get(key)
        if cached is not None:
            return cached
        size = (max(1, math.ceil(image.width / factor)), max(1, math.ceil(image.height / factor)))
        level = image.resize(size, Image.Resampling.BILINEAR if interactive else Image.Resampling.LANCZOS)
        with self._preview_levels_lock:
            self._preview_levels.setdefault(key, level)
        return level

    def _build_preview_frame(self, image, path, geometry, interactive):
        source = self._preview_source_for(image, path, geometry.downsample_factor, interactive)
        crop = source.crop(geometry.source_box)
        if crop.size != geometry.target_size:
            crop = crop.resize(
                geometry.target_size,
                Image.Resampling.BILINEAR if interactive else Image.Resampling.LANCZOS)
        return crop

    def _apply_preview_frame(self, frame, geometry):
        self.preview_photo = ImageTk.PhotoImage(frame)
        if self.preview_image_item is None:
            self.preview_canvas.delete('preview-message')
            self.preview_image_item = self.preview_canvas.create_image(
                round(geometry.origin[0]), round(geometry.origin[1]),
                image=self.preview_photo, anchor='nw', tags='preview-image')
        else:
            self.preview_canvas.itemconfigure(self.preview_image_item, image=self.preview_photo)
        self.preview_canvas.coords(self.preview_image_item, round(geometry.origin[0]), round(geometry.origin[1]))
        self._preview_item_origin = geometry.origin
        self._preview_item_size = frame.size
        self._update_zoom_label()
        self.preview_canvas.configure(
            cursor='fleur' if self.zoom_scale > self.fit_scale + 0.0001 else 'arrow')

    def _cancel_preview_jobs(self):
        if self._interactive_render_job is not None:
            self.after_cancel(self._interactive_render_job)
            self._interactive_render_job = None
        if self._quality_render_job is not None:
            self.after_cancel(self._quality_render_job)
            self._quality_render_job = None
        self._preview_render_generation += 1
        for future in self._preview_futures:
            future.cancel()
        self._preview_futures.clear()

    def _schedule_preview_render(self, interactive=True, quality_delay=PREVIEW_QUALITY_DELAY_MS):
        """Coalesce input; only the newest viewport is allowed to reach the canvas."""
        self._preview_render_generation += 1
        for future in self._preview_futures:
            future.cancel()
        self._preview_futures = {future for future in self._preview_futures if not future.done()}
        if self._interactive_render_job is not None:
            self.after_cancel(self._interactive_render_job)
            self._interactive_render_job = None
        if self._quality_render_job is not None:
            self.after_cancel(self._quality_render_job)
            self._quality_render_job = None
        if interactive:
            self._interactive_render_job = self.after(
                PREVIEW_INTERACTIVE_DELAY_MS, self._render_interactive_frame)
            self._quality_render_job = self.after(
                quality_delay, self._render_quality_frame)

    def _render_interactive_frame(self):
        self._interactive_render_job = None
        self._request_preview_render(interactive=True)

    def _render_quality_frame(self):
        self._quality_render_job = None
        self._request_preview_render(interactive=False)

    def _request_preview_render(self, interactive):
        """Crop and resample outside Tk's event loop; only Tk image creation stays on the UI thread."""
        image = self.current_source_image
        path = self.current_source_path
        if image is None or path is None:
            return
        self._preview_render_generation += 1
        generation = self._preview_render_generation
        for future in self._preview_futures:
            future.cancel()
        self._preview_futures = {future for future in self._preview_futures if not future.done()}
        try:
            geometry = self._preview_geometry(reset_zoom=False, interactive=interactive)
        except Exception:
            return
        future = self._preview_executor.submit(
            self._preview_render_worker,
            generation,
            str(path.resolve()),
            image,
            path,
            geometry,
            interactive,
        )
        self._preview_futures.add(future)

    def _preview_render_worker(self, generation, path_key, image, path, geometry, interactive):
        try:
            frame = self._build_preview_frame(image, path, geometry, interactive)
            self._preview_render_events.put((generation, path_key, frame, geometry, None))
        except Exception as exc:
            self._preview_render_events.put((generation, path_key, None, None, exc))

    def _poll_preview_render_events(self):
        newest = None
        try:
            while True:
                event = self._preview_render_events.get_nowait()
                if event[0] == self._preview_render_generation:
                    newest = event
        except queue.Empty:
            pass
        if newest is not None:
            _, path_key, frame, geometry, _error = newest
            current_path = self.current_source_path
            if (frame is not None and geometry is not None and current_path is not None
                    and str(current_path.resolve()) == path_key):
                self._apply_preview_frame(frame, geometry)
                self._preview_futures = {future for future in self._preview_futures if not future.done()}
        if self.winfo_exists():
            self._preview_poll_job = self.after(16, self._poll_preview_render_events)

    def _constrain_pan(self, canvas_width, canvas_height):
        image = self.current_source_image
        if image is None:
            return
        display_width = image.width * self.zoom_scale
        display_height = image.height * self.zoom_scale
        max_x = max(0.0, (display_width - canvas_width) / 2)
        max_y = max(0.0, (display_height - canvas_height) / 2)
        self.pan_x = max(-max_x, min(max_x, self.pan_x))
        self.pan_y = max(-max_y, min(max_y, self.pan_y))

    def _update_zoom_label(self):
        if self.current_source_image is None:
            self.zoom_label.configure(text='—')
            return
        percent = round(self.zoom_scale * 100)
        if abs(self.zoom_scale - self.fit_scale) < 0.0001:
            self.zoom_label.configure(text=f'适合 {percent}%')
            return
        self.zoom_label.configure(text=f'{percent}%')

    def _set_zoom(self, scale, anchor):
        image = self.current_source_image
        if image is None:
            return
        canvas_width = max(self.preview_canvas.winfo_width(), 1)
        canvas_height = max(self.preview_canvas.winfo_height(), 1)
        self._constrain_pan(canvas_width, canvas_height)
        old_scale = self.zoom_scale
        new_scale = max(self.fit_scale, min(4.0, scale))
        if abs(new_scale - old_scale) < 1e-06:
            return
        if anchor is not None:
            anchor_x, anchor_y = anchor
            old_left = canvas_width / 2 + self.pan_x - image.width * old_scale / 2
            old_top = canvas_height / 2 + self.pan_y - image.height * old_scale / 2
            source_x = max(0.0, min(float(image.width), (anchor_x - old_left) / old_scale))
            source_y = max(0.0, min(float(image.height), (anchor_y - old_top) / old_scale))
            self.pan_x = anchor_x - source_x * new_scale - canvas_width / 2 + image.width * new_scale / 2
            self.pan_y = anchor_y - source_y * new_scale - canvas_height / 2 + image.height * new_scale / 2
        self.zoom_scale = new_scale
        self._schedule_preview_render()

    def zoom_fit(self):
        if self.current_source_image is None:
            return
        self.zoom_scale = self.fit_scale
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._cancel_preview_jobs()
        self._render_preview(reset_zoom=False, interactive=True)
        self._schedule_preview_render(interactive=False, quality_delay=80)

    def zoom_actual(self):
        if self.current_source_image is None:
            return
        self.zoom_scale = max(self.fit_scale, 1.0)
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._cancel_preview_jobs()
        self._render_preview(reset_zoom=False, interactive=True)
        self._schedule_preview_render(interactive=False, quality_delay=80)

    def toggle_zoom(self):
        if self.current_source_image is None:
            return
        if abs(self.zoom_scale - self.fit_scale) < 0.0001:
            self.zoom_actual()
            return
        self.zoom_fit()

    def zoom_step(self, direction):
        if self.current_source_image is None:
            return
        factor = 1.25 if direction > 0 else 0.8
        center = (self.preview_canvas.winfo_width() / 2, self.preview_canvas.winfo_height() / 2)
        self._set_zoom(self.zoom_scale * factor, center)

    def _preview_mouse_wheel(self, event):
        if self.current_source_image is None:
            return 'break'
        if event.delta == 0:
            steps = 0
        else:
            steps = event.delta / 120
        self._set_zoom(self.zoom_scale * 1.25 ** steps, (event.x, event.y))
        return 'break'

    def _preview_drag_start(self, event):
        if self.current_source_image is None:
            return
        if self.zoom_scale <= self.fit_scale + 0.0001:
            self._drag_state = None
            return
        self._drag_state = (event.x, event.y, self.pan_x, self.pan_y)

    def _preview_drag_motion(self, event):
        if self._drag_state is None:
            return
        start_x, start_y, start_pan_x, start_pan_y = self._drag_state
        previous_pan_y, previous_pan_x = self.pan_y, self.pan_x
        self.pan_x = start_pan_x + event.x - start_x
        self.pan_y = start_pan_y + event.y - start_y
        self._constrain_pan(max(self.preview_canvas.winfo_width(), 1),
                            max(self.preview_canvas.winfo_height(), 1))
        self._move_preview_item(self.pan_x - previous_pan_x, self.pan_y - previous_pan_y)
        if self._preview_frame_needs_refresh():
            self._schedule_preview_render(interactive=True, quality_delay=180)
        else:
            self._schedule_preview_render(interactive=False, quality_delay=180)

    def _preview_drag_end(self, _event):
        self._drag_state = None
        self._schedule_preview_render(interactive=True, quality_delay=70)

    def _move_preview_item(self, dx, dy):
        if self.preview_image_item is None:
            return
        if dx != 0 or dy != 0:
            self.preview_canvas.move(self.preview_image_item, dx, dy)
        if self._preview_item_origin is not None:
            self._preview_item_origin = (self._preview_item_origin[0] + dx, self._preview_item_origin[1] + dy)

    def _preview_frame_needs_refresh(self):
        if self.preview_image_item is None:
            return True
        bounds = self.preview_canvas.bbox(self.preview_image_item)
        if bounds is None:
            return True
        canvas_width = max(self.preview_canvas.winfo_width(), 1)
        canvas_height = max(self.preview_canvas.winfo_height(), 1)
        safety = max(80, round(max(canvas_width, canvas_height) * 0.15))
        left, top, right, bottom = bounds
        return (left > -safety or top > -safety
                or right < canvas_width + safety
                or bottom < canvas_height + safety)

    def _ensure_jpeg_window(self, center_index):
        """Preload a sliding window of JPEGs around *center_index*.

        Only images within [center - BEHIND, center + AHEAD] that are not
        already cached are decoded in a background thread.  Entries outside
        the widened window are evicted to tombstones, keeping memory bounded.
        """
        items = self.all_items
        if not items:
            return
        # Build the list of JPG paths in display order.
        jpeg_items = [(i, item.primary) for i, item in enumerate(items)
                      if item.primary.suffix.lower() in JPEG_EXTENSIONS]
        if not jpeg_items:
            self.preload_label.configure(text="")
            return

        # Window bounds in *display* (item-list) space.
        lo = max(0, center_index - JPEG_PRELOAD_BEHIND)
        hi = min(len(items) - 1, center_index + JPEG_PRELOAD_AHEAD)
        window_paths = {path for i, path in jpeg_items if lo <= i <= hi}
        window_keys = {str(p.resolve()) for p in window_paths}

        # Invalidate stale generation; start a new one.
        self._preload_generation += 1
        generation = self._preload_generation

        # Evict entries that fall outside the widened window.
        evict_lo = max(0, center_index - JPEG_PRELOAD_BEHIND * 2)
        evict_hi = min(len(items) - 1, center_index + JPEG_PRELOAD_AHEAD * 2)
        keep_keys = {str(p.resolve()) for i, p in jpeg_items if evict_lo <= i <= evict_hi}
        with self._jpeg_cache_lock:
            stale = [k for k in self.jpeg_cache if k not in keep_keys]
            for k in stale:
                self.jpeg_cache.pop(k, None)
                self._jpeg_tombstones.add(k)

        # Paths that still need decoding.
        with self._jpeg_cache_lock:
            already = set(self.jpeg_cache.keys())
        to_load = [p for p in window_paths if str(p.resolve()) not in already]
        self._preload_done = not to_load
        if not to_load:
            self.preload_label.configure(text=f"JPG 缓存 {len(already & window_keys)} / {len(window_paths)}")
            return
        self.preload_label.configure(text=f"正在预载 JPG：0 / {len(to_load)}")
        worker = Thread(
            target=self._preload_jpegs,
            args=(generation, to_load),
            daemon=True,
            name="photo-culler-jpeg-preload",
        )
        worker.start()
        self.after(75, lambda: self._poll_preload_events(generation))

    def _preload_jpegs(self, generation, jpeg_paths):
        total = len(jpeg_paths)
        for number, path in enumerate(jpeg_paths, start=1):
            if generation != self._preload_generation:
                return
            image = self._read_raster_image(path)
            with self._jpeg_cache_lock:
                if generation != self._preload_generation:
                    return
                key = str(path.resolve())
                self.jpeg_cache[key] = image
                self.jpeg_cache.move_to_end(key)
                while len(self.jpeg_cache) > JPEG_CACHE_LIMIT:
                    evicted_key, _ = self.jpeg_cache.popitem(last=False)
                    self._jpeg_tombstones.add(evicted_key)
            if number == 1 or number == total or number % 10 == 0:
                self._preload_events.put((generation, number, total, False))
        self._preload_events.put((generation, total, total, True))

    def _poll_preload_events(self, generation):
        if generation != self._preload_generation:
            return
        latest = None
        while True:
            try:
                event = self._preload_events.get_nowait()
                if event[0] == generation:
                    latest = event
            except queue.Empty:
                break
        if latest is not None:
            _, completed, total, done = latest
            self._preload_done = done
            if done:
                self.preload_label.configure(text=f"JPG 已预载：{completed} 张")
            else:
                self.preload_label.configure(text=f"正在预载 JPG：{completed} / {total}")
                if not self._preload_done:
                    self.after(75, lambda: self._poll_preload_events(generation))

    def _render_thumbnails(self, center=False):
        items = self.visible_items
        self.thumb_canvas.delete("all")
        if not items:
            self.thumb_canvas.configure(scrollregion=(0, 0, 1, self._px(120)))
            return
        canvas_width = self.thumb_canvas.winfo_width() - self.thumb_slot * 5
        total_width = len(items) * self.thumb_slot
        self.thumb_canvas.configure(scrollregion=(0, 0, total_width, self._px(120)))
        if center:
            left = max(0, self.index * self.thumb_slot + self.thumb_slot / 2 - canvas_width / 2)
            max_left = max(0, total_width - canvas_width)
            self.thumb_canvas.xview_moveto(min(left, max_left) / max(total_width, 1))
        view_left = self.thumb_canvas.canvasx(0)
        view_right = view_left + canvas_width
        first = max(0, int(view_left // self.thumb_slot) - 2)
        last = min(len(items), int(view_right // self.thumb_slot) + 3)
        for displayed_index in range(first, last):
            item = items[displayed_index]
            path = item.primary
            x = displayed_index * self.thumb_slot + self.thumb_slot // 2
            selected = displayed_index == self.index
            color = "#4f9cff" if selected else "#343944"
            thickness = 3 if selected else 1
            self.thumb_canvas.create_rectangle(
                x - self.thumb_width // 2 - self._px(3),
                self._px(9),
                x + self.thumb_width // 2 + self._px(3),
                self._px(105),
                fill="#15171b",
                outline=color,
                width=thickness,
            )
            try:
                photo = self._thumbnail(displayed_index, path)
                self.thumb_canvas.create_image(x, self._px(57), image=photo)
            except Exception:
                self.thumb_canvas.create_text(
                    x, self._px(57), text="无法预览", fill="#aab0ba", font=("Segoe UI", 9)
                )
            marker = "★" if item.key in self.kept else ""
            self.thumb_canvas.create_text(
                x - self._px(59), self._px(18), text=marker, fill="#ffd35a",
                font=("Segoe UI Symbol", 12, "bold"), anchor="nw",
            )
            if item.paired_raw_jpeg:
                if item.key in self.kept:
                    mode_text = self._pair_mode_label(self._pair_mode(item))
                else:
                    mode_text = "未保留"
                self.thumb_canvas.create_text(
                    x + self._px(59), self._px(18), text=mode_text, fill="#8bd7ff",
                    font=("Segoe UI", 7, "bold"), anchor="ne",
                )
            label = path.name
            if len(label) > 18:
                label = label[:16] + "…"
            self.thumb_canvas.create_text(
                x, self._px(115), text=label, fill="#d9dde5", font=("Segoe UI", 8)
            )

    def _thumbnail(self, displayed_index, path):
        cache_key = hash((str(path), path.stat().st_mtime_ns, displayed_index))
        cached = self.thumbnail_cache.get(cache_key)
        if cached is not None:
            self.thumbnail_cache.move_to_end(cache_key)
            return cached
        image = self._load_image(path, thumbnail=True)
        image = self._fit_for_display(image, self.thumb_width, self.thumb_height)
        if image.width < self.thumb_width and image.height < self.thumb_height:
            background = Image.new("RGB", (self.thumb_width, self.thumb_height), "#202329")
            background.paste(
                image,
                ((self.thumb_width - image.width) // 2, (self.thumb_height - image.height) // 2),
            )
            image = background
        photo = ImageTk.PhotoImage(image)
        self.thumbnail_cache[cache_key] = photo
        self.thumbnail_cache.move_to_end(cache_key)
        while len(self.thumbnail_cache) > THUMB_CACHE_LIMIT:
            self.thumbnail_cache.popitem(last=False)
        return photo

    def _thumbnail_clicked(self, event):
        items = self.visible_items
        if not items:
            return
        clicked = int(self.thumb_canvas.canvasx(event.x) // self.thumb_slot)
        if 0 <= clicked < len(items):
            self.index = clicked
            self._show_current(center=False)

    def _scroll_thumbnails(self, event):
        self.thumb_canvas.xview_scroll(int(-event.delta / 120) * 3, "units")
        self._render_thumbnails()
        return "break"

    def _queue_preview_resize(self, _event):
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(220, self._refresh_after_resize)

    def _refresh_after_resize(self):
        self._resize_job = None
        if self.current_source_image is not None and self.preview_photo is not None:
            self._cancel_preview_jobs()
            self._render_preview(reset_zoom=False, interactive=True)
            self._schedule_preview_render(interactive=False, quality_delay=90)

    def export_kept(self):
        # 导出逻辑遵循“只复制、不改动源文件”的设计。
        # 对于 RAW+JPG 绑定组，可按 "both / raw / jpg" 模式决定导出哪些成员。
        if not self.kept:
            messagebox.showinfo(APP_NAME, "还没有保留照片。按 Space 标记后再导出。")
            return
        destination = filedialog.askdirectory(title="选择导出保留照片的文件夹")
        if not destination:
            return
        destination_path = Path(destination)
        kept_items = [item for item in self.all_items if item.key in self.kept]
        sources = [path for item in kept_items for path in self._selected_members(item)]
        answer = messagebox.askyesno(
            APP_NAME,
            f"将导出 {len(kept_items)} 个保留项目（共 {len(sources)} 个原始文件）到：\n"
            f"{destination_path}\n\nRAW+JPG 组按当前模式导出。原照片不会被移动或修改。继续吗？",
        )
        if not answer:
            return
        copied = 0
        failures = []
        for source in sources:
            try:
                target = self._unique_destination(destination_path, source.name)
                shutil.copy2(source, target)
                copied += 1
            except OSError as exc:
                failures.append(f"{source.name}: {exc}")
            self._set_status(f"正在导出 {copied}/{len(sources)}…")
            self.update_idletasks()
        if failures:
            messagebox.showwarning(
                APP_NAME,
                f"已复制 {copied} 张；{len(failures)} 张未能复制。\n\n"
                + "\n".join(failures[:3]),
            )
        else:
            messagebox.showinfo(APP_NAME, f"已复制 {copied} 张保留照片。")
        self._set_status(self._status_text(self.current_item))

    def _selected_members(self, item):
        if not item.paired_raw_jpeg:
            return item.members
        mode = self._pair_mode(item)
        if mode == "raw":
            return tuple(path for path in item.members if path.suffix.lower() == ".dng")
        if mode == "jpg":
            return tuple(path for path in item.members if path.suffix.lower() in JPEG_EXTENSIONS)
        return item.members

    def _unique_destination(self, folder, filename):
        candidate = folder / filename
        if not candidate.exists():
            return candidate
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        number = 1
        while True:
            candidate = folder / f"{stem} ({number}){suffix}"
            if not candidate.exists():
                return candidate
            number += 1

    def _selection_file(self):
        assert self.folder is not None
        digest = hashlib.sha256(str(self.folder.resolve()).encode("utf-8")).hexdigest()[:20]
        appdata = Path.home() / "AppData" / "Local" / "PhotoCuller" / "selections"
        appdata.mkdir(parents=True, exist_ok=True)
        return appdata / f"{digest}.json"

    def _load_selection(self):
        if self.folder is None:
            return set(), {}
        try:
            data = json.loads(self._selection_file().read_text(encoding="utf-8"))
            kept = {key for key in data.get("kept", []) if isinstance(key, str)}
            raw_pair_modes = data.get("pair_modes", {})
            if not isinstance(raw_pair_modes, dict):
                raw_pair_modes = {}
            pair_modes = {
                key: mode for key, mode in raw_pair_modes.items()
                if isinstance(key, str) and isinstance(mode, str)
            }
            return kept, pair_modes
        except (OSError, ValueError, json.JSONDecodeError):
            return set(), {}

    def _save_selection(self):
        if self.folder is None:
            return
        payload = {
            "folder": str(self.folder),
            "kept": sorted(self.kept),
            "pair_modes": dict(sorted(self.pair_modes.items())),
            "updated": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            self._selection_file().write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def _status_text(self, item):
        if item is None:
            return f"保留 {len(self.kept)} 张照片"
        prefix = "★ 已保留" if item.key in self.kept else "未保留"
        if item.paired_raw_jpeg and item.key in self.kept:
            paired = f"    绑定组：{self._pair_mode_label(self._pair_mode(item))}"
        elif item.paired_raw_jpeg:
            paired = "    RAW+JPG 绑定组"
        else:
            paired = ""
        return (
            f"{self.index + 1} / {len(self.visible_items)}    {prefix}"
            f"    已保留 {len(self.kept)} 个项目{paired}    {item.primary.name}"
        )

    def _set_status(self, text):
        self.status_label.configure(text=text)

    def _on_close(self):
        """Stop background preview jobs before Tk tears down its image runtime."""
        self._cancel_preview_jobs()
        if getattr(self, "_preview_poll_job", None) is not None:
            try:
                self.after_cancel(self._preview_poll_job)
            except tk.TclError:
                pass
            self._preview_executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()


if __name__ == '__main__':
    try:
        if '--self-test' in sys.argv:
            probe = tk.Tcl()
            probe.eval('package require Tk')
            print('Photo Culler runtime OK')
            raise SystemExit(0)
        PhotoCuller().mainloop()
    except Exception as error:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, f'程序启动失败：\n{error}')
        root.destroy()
        raise
