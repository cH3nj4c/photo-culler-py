"""Tkinter presentation layer for Photo Culler."""

from __future__ import annotations

import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from .config import (
    APP_NAME,
    PREVIEW_INTERACTIVE_DELAY_MS,
    PREVIEW_POLL_MS,
    PREVIEW_QUALITY_DELAY_MS,
    RESIZE_DEBOUNCE_MS,
    THUMB_CACHE_LIMIT,
    THUMB_HEIGHT,
    THUMB_SLOT,
    THUMB_WIDTH,
)
from .domain import (
    PhotoGroup,
    build_photo_groups,
    filter_visible_items,
    next_pair_mode,
    normalize_pair_mode,
    pair_mode_label,
    scan_photo_paths,
)
from .export_service import ExportService
from .image_loader import ImageLoader
from .imaging import decode_photo, fit_for_display, thumbnail_decode_size
from .jpeg_preloader import JpegCache, JpegPreloader
from .preview_engine import (
    PreviewEngine,
    compute_geometry,
    constrain_pan,
)
from .selection_store import load_selection, save_selection
from .winshell import enable_windows_high_dpi, send_to_recycle_bin


class PhotoCuller(tk.Tk):
    def __init__(self) -> None:
        # Critical order: process DPI awareness must be set before any Tk window.
        enable_windows_high_dpi()
        super().__init__()
        self.title(APP_NAME)
        self._configure_dpi_layout()
        self.configure(bg="#17191d")

        # Folder session / selection state.
        self.folder: Path | None = None
        self.all_items: list[PhotoGroup] = []
        self.index = 0
        self.kept: set[str] = set()
        self.pair_modes: dict[str, str] = {}
        self.show_kept_only = tk.BooleanVar(value=False)
        self._visible_items: list[PhotoGroup] | None = None

        # Preview view state.
        self.preview_photo = None
        self.preview_image_item = None
        self._preview_item_origin = None
        self._preview_item_size = None
        self.current_source_image = None
        self.current_source_path: Path | None = None
        self.current_source_id: str | None = None
        self.zoom_scale = 1.0
        self.fit_scale = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._drag_state = None
        self._interactive_render_job = None
        self._quality_render_job = None
        self._pending_reset_zoom = True
        self._loading_path_id: str | None = None

        # Background services.
        self.jpeg_cache = JpegCache()
        self.preloader = JpegPreloader(self.jpeg_cache)
        self.preview_engine = PreviewEngine()
        self.image_loader = ImageLoader(self.jpeg_cache)
        self.export_service = ExportService()
        self._export_active = False

        self.thumbnail_cache = {}
        self._resize_job = None
        self._status_note = ""

        self._make_style()
        self._build_ui()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job = self.after(PREVIEW_POLL_MS, self._poll_services)
        self.after(250, self.open_folder)

    # --- layout / chrome -------------------------------------------------

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
        style.configure(
            "Header.TLabel", background="#202329", foreground="#e7e9ed",
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "Zoom.TLabel", background="#202329", foreground="#8bd7ff",
            font=("Segoe UI", 10, "bold"),
        )
        style.configure("App.TButton", font=("Segoe UI", 10), padding=(11, 7))
        style.configure("Keep.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 7))
        style.configure(
            "App.TCheckbutton", background="#202329", foreground="#e7e9ed",
            font=("Segoe UI", 10),
        )
        style.map(
            "App.TCheckbutton",
            background=[("active", "#202329")],
            foreground=[("active", "#ffffff")],
        )

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, style="Toolbar.TFrame", padding=(16, 10))
        toolbar.pack(fill="x")

        ttk.Button(
            toolbar, text="打开照片文件夹  O", style="App.TButton", command=self.open_folder
        ).pack(side="left")
        self.folder_label = ttk.Label(toolbar, text="尚未打开文件夹", style="Header.TLabel")
        self.folder_label.pack(side="left", padx=(14, 0))

        ttk.Button(
            toolbar, text="导出保留照片  E", style="App.TButton", command=self.export_kept
        ).pack(side="right")
        ttk.Button(
            toolbar, text="适合屏幕  Z", style="App.TButton", command=self.zoom_fit
        ).pack(side="right", padx=(0, 6))
        ttk.Button(
            toolbar, text="100%  1", style="App.TButton", command=self.zoom_actual
        ).pack(side="right", padx=(0, 6))
        self.zoom_label = ttk.Label(toolbar, text="适合屏幕", style="Zoom.TLabel")
        self.zoom_label.pack(side="right", padx=(0, 12))
        ttk.Button(
            toolbar, text="重置模式", style="App.TButton", command=self.reset_all_pair_modes
        ).pack(side="right", padx=(0, 6))
        ttk.Button(
            toolbar, text="全不保留", style="App.TButton", command=self.clear_all_kept
        ).pack(side="right", padx=(0, 6))
        self.keep_mode_button = ttk.Button(
            toolbar, text="模式：单文件", style="App.TButton", command=self.cycle_keep_mode
        )
        self.keep_mode_button.pack(side="right", padx=(0, 6))
        ttk.Button(
            toolbar, text="删除  Del", style="App.TButton", command=self.delete_current
        ).pack(side="right", padx=(0, 6))
        ttk.Button(
            toolbar, text="保留 / 取消  Space", style="Keep.TButton", command=self.toggle_keep
        ).pack(side="right", padx=(0, 10))
        ttk.Checkbutton(
            toolbar, text="只看保留", variable=self.show_kept_only,
            style="App.TCheckbutton", command=self.toggle_filter,
        ).pack(side="right", padx=(0, 16))

        self.preview_frame = tk.Frame(self, bg="#111317", highlightthickness=0)
        self.preview_frame.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        self.preview_canvas = tk.Canvas(
            self.preview_frame, bg="#111317", highlightthickness=0, cursor="arrow"
        )
        self.preview_canvas.pack(fill="both", expand=True)
        self.preview_canvas.create_text(
            0, 0, text="打开一个照片文件夹开始选片",
            fill="#bdc3cd", font=("Segoe UI", 16), tags="preview-message",
        )
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
        self.help_label = ttk.Label(
            info,
            text="[ ] 切换 · Space 保留 · F 模式 · Del 删除 · 滚轮缩放 · Z 适合/100% · + − 微调 · 导出中按 Esc 取消",
            style="Muted.TLabel",
        )
        self.help_label.pack(side="right")

        thumbs_container = tk.Frame(self, bg="#202329", height=self._px(132))
        thumbs_container.pack(fill="x", padx=16, pady=(0, 16))
        thumbs_container.pack_propagate(False)
        self.thumb_canvas = tk.Canvas(
            thumbs_container, bg="#202329", highlightthickness=0, height=self._px(132)
        )
        self.thumb_scrollbar = ttk.Scrollbar(
            thumbs_container, orient="horizontal", command=self.thumb_canvas.xview
        )
        self.thumb_canvas.configure(xscrollcommand=self.thumb_scrollbar.set)
        self.thumb_canvas.pack(fill="both", expand=True)
        self.thumb_scrollbar.pack(fill="x")
        self.thumb_canvas.bind("<Button-1>", self._thumbnail_clicked)
        self.thumb_canvas.bind("<MouseWheel>", self._scroll_thumbnails)
        self.thumb_canvas.bind("<Configure>", lambda _event: self._render_thumbnails())

    def _bind_keys(self) -> None:
        self.bind_all("<bracketleft>", lambda _e: self.change_index(-1))
        self.bind_all("<bracketright>", lambda _e: self.change_index(1))
        self.bind_all("<space>", self._on_space)
        self.bind_all("<f>", self._on_mode_key)
        self.bind_all("<F>", self._on_mode_key)
        self.bind_all("<o>", lambda _e: self.open_folder())
        self.bind_all("<O>", lambda _e: self.open_folder())
        self.bind_all("<e>", lambda _e: self.export_kept())
        self.bind_all("<E>", lambda _e: self.export_kept())
        self.bind_all("<Delete>", self._on_delete_key)
        self.bind_all("<Escape>", self._on_escape)
        self.bind_all("<z>", lambda _e: self.toggle_zoom())
        self.bind_all("<Z>", lambda _e: self.toggle_zoom())
        self.bind_all("<Key-1>", lambda _e: self.zoom_actual())
        self.bind_all("<plus>", lambda _e: self.zoom_step(1))
        self.bind_all("<KP_Add>", lambda _e: self.zoom_step(1))
        self.bind_all("<minus>", lambda _e: self.zoom_step(-1))
        self.bind_all("<KP_Subtract>", lambda _e: self.zoom_step(-1))
        self.bind_all("<Control-Shift-x>", self._on_clear_all_shortcut)
        self.bind_all("<Control-Shift-m>", self._on_reset_modes_shortcut)

    def _on_space(self, event: tk.Event) -> str:
        widget = event.widget
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

    def _on_delete_key(self, event: tk.Event) -> str:
        widget = event.widget
        widget_class = widget.winfo_class() if hasattr(widget, "winfo_class") else ""
        if widget_class in {"Checkbutton", "TButton", "TCheckbutton", "Button"}:
            return "break"
        self.delete_current()
        return "break"

    def _on_escape(self, _event: tk.Event) -> str:
        if self._export_active:
            self.export_service.cancel()
            self._set_status("正在取消导出…")
        return "break"

    # --- session state ---------------------------------------------------

    def _invalidate_visible(self) -> None:
        self._visible_items = None

    @property
    def visible_items(self) -> list[PhotoGroup]:
        if self._visible_items is None:
            self._visible_items = filter_visible_items(
                self.all_items, self.kept, self.show_kept_only.get()
            )
        return self._visible_items

    def _ensure_index(self) -> None:
        items = self.visible_items
        if not items:
            self.index = 0
            return
        self.index = min(max(self.index, 0), len(items) - 1)

    @property
    def current_item(self) -> PhotoGroup | None:
        items = self.visible_items
        if not items:
            return None
        if 0 <= self.index < len(items):
            return items[self.index]
        return items[min(max(self.index, 0), len(items) - 1)]

    def _pair_mode(self, item: PhotoGroup) -> str:
        return normalize_pair_mode(self.pair_modes.get(item.key, "both"))

    def _update_keep_mode_ui(self) -> None:
        item = self.current_item
        if item is not None and item.paired_raw_jpeg:
            self.keep_mode_button.configure(
                text=f"模式：{pair_mode_label(self._pair_mode(item))}  F"
            )
            self.keep_mode_button.state(["!disabled"])
            return
        self.keep_mode_button.configure(text="模式：单文件  F")
        self.keep_mode_button.state(["disabled"])

    def _save_selection(self) -> None:
        error = save_selection(self.folder, self.kept, self.pair_modes)
        if error:
            self._status_note = error
        else:
            self._status_note = ""

    def open_folder(self) -> None:
        chosen = filedialog.askdirectory(
            title="选择包含照片的文件夹",
            initialdir=str(self.folder) if self.folder else None,
        )
        if not chosen:
            return
        folder = Path(chosen)
        try:
            paths = scan_photo_paths(folder)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法读取这个文件夹：\n{exc}")
            return

        self.image_loader.cancel_pending()
        self.preview_engine.cancel_all()
        self.preview_engine.clear_levels()
        self.preloader.invalidate()
        self.jpeg_cache.clear()
        self.thumbnail_cache.clear()
        self._status_note = ""

        self.folder = folder
        self.all_items = build_photo_groups(paths)
        self.index = 0
        self._invalidate_visible()
        self.current_source_image = None
        self.current_source_path = None
        self.current_source_id = None

        saved, saved_pair_modes = load_selection(folder)
        current_keys = {item.key for item in self.all_items}
        self.kept = saved.intersection(current_keys)
        pair_keys = {item.key for item in self.all_items if item.paired_raw_jpeg}
        self.pair_modes = {
            key: mode
            for key, mode in saved_pair_modes.items()
            if key in pair_keys and mode in {"both", "jpg", "raw"}
        }
        self._invalidate_visible()
        self.folder_label.configure(text=folder.name if folder.name else str(folder))

        if not self.all_items:
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
            self.kept.discard(key)
        else:
            self.kept.add(key)
        if item.paired_raw_jpeg:
            self.pair_modes.setdefault(key, "both")
        self._invalidate_visible()
        self._save_selection()
        if self.show_kept_only.get():
            self._ensure_index()
        self._show_current(center=False, reset_zoom=False)

    def cycle_keep_mode(self) -> None:
        item = self.current_item
        if item is None or not item.paired_raw_jpeg:
            return
        self.pair_modes[item.key] = next_pair_mode(self._pair_mode(item))
        self._save_selection()
        self._update_keep_mode_ui()
        self._set_status(self._status_text(item))
        self._render_thumbnails(center=False)

    def clear_all_kept(self) -> None:
        if not self.kept:
            messagebox.showinfo(APP_NAME, "当前没有已保留的照片。")
            return
        answer = messagebox.askyesno(
            APP_NAME,
            f"确定要取消全部 {len(self.kept)} 个保留项目吗？\n\n各组的 RAW/JPG 模式不会改变。",
        )
        if not answer:
            return
        self.kept.clear()
        self._invalidate_visible()
        self._save_selection()
        if self.show_kept_only.get():
            self.show_kept_only.set(False)
            self._invalidate_visible()
        self._ensure_index()
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
        answer = messagebox.askyesno(
            APP_NAME,
            f"确定要将 {len(pair_items)} 个 RAW+JPG 绑定组的模式全部重置为 RAW+JPG 吗？\n\n"
            "各组的保留/不保留状态不会改变。",
        )
        if not answer:
            return
        self.pair_modes = {item.key: "both" for item in pair_items}
        self._save_selection()
        self._update_keep_mode_ui()
        self._set_status(self._status_text(self.current_item))
        self._render_thumbnails(center=False)

    def toggle_filter(self) -> None:
        active = self.current_item
        self._invalidate_visible()
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

    # --- current photo / preview -----------------------------------------

    def _show_current(self, center, reset_zoom=True):
        item = self.current_item
        if item is None:
            return
        self._ensure_index()
        item = self.current_item
        if item is None:
            return
        path = item.primary
        path_id = item.primary_id

        self.preview_engine.cancel_all()
        self._cancel_ui_render_jobs()
        self._set_status("正在载入：" + path.name)
        self._update_keep_mode_ui()
        self._render_thumbnails(center=center)
        self._ensure_jpeg_window(self.index)

        if self.current_source_path == path and self.current_source_image is not None:
            self._pending_reset_zoom = reset_zoom
            self._render_preview_now(interactive=True)
            self._schedule_preview_render(interactive=False, quality_delay=90)
            self._set_status(self._status_text(item))
            return

        cached = self.image_loader.try_cached(path_id)
        if cached is not None:
            self._adopt_source_image(path, path_id, cached)
            self._pending_reset_zoom = reset_zoom
            self._render_preview_now(interactive=True)
            self._schedule_preview_render(interactive=False, quality_delay=90)
            self._set_status(self._status_text(item))
            return

        # Decode off the UI thread.
        self.current_source_image = None
        self.current_source_path = path
        self.current_source_id = path_id
        self._loading_path_id = path_id
        self._pending_reset_zoom = reset_zoom
        self._show_preview_message(f"正在载入\n{path.name}")
        self.image_loader.submit(path, path_id)

    def _adopt_source_image(self, path: Path, path_id: str, image) -> None:
        if self.current_source_id != path_id:
            self.preview_engine.clear_levels()
        self.current_source_image = image
        self.current_source_path = path
        self.current_source_id = path_id
        self._loading_path_id = None

    def _render_preview_now(self, interactive: bool) -> None:
        image = self.current_source_image
        path_id = self.current_source_id
        if image is None or path_id is None:
            return
        try:
            geometry, new_fit, new_zoom, new_pan_x, new_pan_y = self._geometry(interactive)
        except Exception:
            return
        self.fit_scale = new_fit
        self.zoom_scale = new_zoom
        self.pan_x = new_pan_x
        self.pan_y = new_pan_y
        frame = self.preview_engine.build_frame_sync(image, path_id, geometry, interactive)
        self._apply_preview_frame(frame, geometry)
        self._pending_reset_zoom = False

    def _geometry(self, interactive: bool):
        image = self.current_source_image
        if image is None:
            raise RuntimeError("没有可显示的照片")
        canvas_width = max(self.preview_canvas.winfo_width(), 1)
        canvas_height = max(self.preview_canvas.winfo_height(), 1)
        return compute_geometry(
            image=image,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            zoom_scale=self.zoom_scale,
            fit_scale=self.fit_scale,
            pan_x=self.pan_x,
            pan_y=self.pan_y,
            reset_zoom=self._pending_reset_zoom,
            previous_fit=self.fit_scale,
            interactive=interactive,
        )

    def _apply_preview_frame(self, frame, geometry) -> None:
        self.preview_photo = ImageTk.PhotoImage(frame)
        if self.preview_image_item is None:
            self.preview_canvas.delete("preview-message")
            self.preview_image_item = self.preview_canvas.create_image(
                round(geometry.origin[0]),
                round(geometry.origin[1]),
                image=self.preview_photo,
                anchor="nw",
                tags="preview-image",
            )
        else:
            self.preview_canvas.itemconfigure(
                self.preview_image_item, image=self.preview_photo
            )
        self.preview_canvas.coords(
            self.preview_image_item,
            round(geometry.origin[0]),
            round(geometry.origin[1]),
        )
        self._preview_item_origin = geometry.origin
        self._preview_item_size = frame.size
        self._update_zoom_label()
        self.preview_canvas.configure(
            cursor="fleur" if self.zoom_scale > self.fit_scale + 0.0001 else "arrow"
        )

    def _show_preview_message(self, message: str) -> None:
        self.preview_engine.cancel_all()
        self._cancel_ui_render_jobs()
        self.preview_photo = None
        self.preview_image_item = None
        self._preview_item_origin = None
        self._preview_item_size = None
        self._drag_state = None
        self.preview_canvas.delete("all")
        width = max(self.preview_canvas.winfo_width(), 1)
        height = max(self.preview_canvas.winfo_height(), 1)
        self.preview_canvas.create_text(
            width / 2,
            height / 2,
            text=message,
            fill="#bdc3cd",
            font=("Segoe UI", 16),
            justify="center",
        )
        self.preview_canvas.configure(cursor="arrow")
        self.zoom_label.configure(text="—")

    def _cancel_ui_render_jobs(self) -> None:
        if self._interactive_render_job is not None:
            self.after_cancel(self._interactive_render_job)
            self._interactive_render_job = None
        if self._quality_render_job is not None:
            self.after_cancel(self._quality_render_job)
            self._quality_render_job = None

    def _schedule_preview_render(self, interactive=True, quality_delay=PREVIEW_QUALITY_DELAY_MS):
        self._cancel_ui_render_jobs()
        if interactive:
            self._interactive_render_job = self.after(
                PREVIEW_INTERACTIVE_DELAY_MS, self._render_interactive_frame
            )
            self._quality_render_job = self.after(
                quality_delay, self._render_quality_frame
            )

    def _render_interactive_frame(self):
        self._interactive_render_job = None
        self._request_preview_render(interactive=True)

    def _render_quality_frame(self):
        self._quality_render_job = None
        self._request_preview_render(interactive=False)

    def _request_preview_render(self, interactive):
        image = self.current_source_image
        path_id = self.current_source_id
        if image is None or path_id is None:
            return
        try:
            geometry, new_fit, new_zoom, new_pan_x, new_pan_y = self._geometry(interactive)
        except Exception:
            return
        self.fit_scale = new_fit
        self.zoom_scale = new_zoom
        self.pan_x = new_pan_x
        self.pan_y = new_pan_y
        generation = self.preview_engine.bump_generation()
        self.preview_engine.submit(generation, path_id, image, geometry, interactive)

    def _poll_services(self) -> None:
        try:
            self._handle_image_load_events()
            self._handle_preview_events()
            self._handle_preload_events()
            self._handle_export_events()
        finally:
            if self.winfo_exists():
                self._poll_job = self.after(PREVIEW_POLL_MS, self._poll_services)

    def _handle_image_load_events(self) -> None:
        event = self.image_loader.drain_latest()
        if event is None:
            return
        generation, path_id, image, error = event
        if generation != self.image_loader.generation:
            return
        if path_id != self.current_source_id and path_id != self._loading_path_id:
            return
        if error is not None or image is None:
            path = self.current_source_path
            name = path.name if path is not None else path_id
            self.current_source_image = None
            self.current_source_path = None
            self.current_source_id = None
            self._loading_path_id = None
            self._show_preview_message(f"无法显示\n{name}\n\n{error}")
            self._update_keep_mode_ui()
            return
        self._adopt_source_image(self.current_source_path or Path(path_id), path_id, image)
        self.preview_image_item = None  # message canvas may still be showing
        self.preview_canvas.delete("all")
        self._render_preview_now(interactive=True)
        self._schedule_preview_render(interactive=False, quality_delay=90)
        item = self.current_item
        self._set_status(self._status_text(item))

    def _handle_preview_events(self) -> None:
        event = self.preview_engine.drain_latest()
        if event is None:
            return
        _generation, path_id, frame, geometry, _error = event
        current_id = self.current_source_id
        if frame is None or geometry is None or current_id is None:
            return
        if path_id != current_id:
            return
        self._apply_preview_frame(frame, geometry)

    def _handle_preload_events(self) -> None:
        try:
            while True:
                event = self.preloader.events.get_nowait()
                generation, completed, total, done = event
                if generation != self.preloader.generation:
                    continue
                if done:
                    self.preload_label.configure(text=f"JPG 已预载：{completed} 张")
                else:
                    self.preload_label.configure(
                        text=f"正在预载 JPG：{completed} / {total}"
                    )
        except queue.Empty:
            pass

    def _handle_export_events(self) -> None:
        try:
            latest = None
            while True:
                event = self.export_service.events.get_nowait()
                if event[0] == self.export_service.generation:
                    latest = event
        except queue.Empty:
            pass
        if latest is None:
            return
        _generation, copied, total, failures, done, cancelled = latest
        if not done:
            self._set_status(f"正在导出 {copied}/{total}… 按 Esc 取消")
            return
        self._export_active = False
        if cancelled:
            self._set_status(f"导出已取消（已复制 {copied}/{total}）")
            return
        if failures:
            messagebox.showwarning(
                APP_NAME,
                f"已复制 {copied} 张；{len(failures)} 张未能复制。\n\n" + "\n".join(failures[:3]),
            )
        else:
            messagebox.showinfo(APP_NAME, f"已复制 {copied} 张保留照片。")
        self._set_status(self._status_text(self.current_item))

    # --- zoom / pan ------------------------------------------------------

    def _constrain_pan_now(self) -> None:
        image = self.current_source_image
        if image is None:
            return
        canvas_width = max(self.preview_canvas.winfo_width(), 1)
        canvas_height = max(self.preview_canvas.winfo_height(), 1)
        self.pan_x, self.pan_y = constrain_pan(
            image, self.zoom_scale, canvas_width, canvas_height, self.pan_x, self.pan_y
        )

    def _update_zoom_label(self):
        if self.current_source_image is None:
            self.zoom_label.configure(text="—")
            return
        percent = round(self.zoom_scale * 100)
        if abs(self.zoom_scale - self.fit_scale) < 0.0001:
            self.zoom_label.configure(text=f"适合 {percent}%")
            return
        self.zoom_label.configure(text=f"{percent}%")

    def _set_zoom(self, scale, anchor):
        image = self.current_source_image
        if image is None:
            return
        canvas_width = max(self.preview_canvas.winfo_width(), 1)
        canvas_height = max(self.preview_canvas.winfo_height(), 1)
        self._constrain_pan_now()
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
            self.pan_x = (
                anchor_x - source_x * new_scale - canvas_width / 2 + image.width * new_scale / 2
            )
            self.pan_y = (
                anchor_y - source_y * new_scale - canvas_height / 2 + image.height * new_scale / 2
            )
        self.zoom_scale = new_scale
        self._pending_reset_zoom = False
        self._schedule_preview_render()

    def zoom_fit(self):
        if self.current_source_image is None:
            return
        self.zoom_scale = self.fit_scale
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._pending_reset_zoom = False
        self.preview_engine.cancel_all()
        self._render_preview_now(interactive=True)
        self._schedule_preview_render(interactive=False, quality_delay=80)

    def zoom_actual(self):
        if self.current_source_image is None:
            return
        self.zoom_scale = max(self.fit_scale, 1.0)
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._pending_reset_zoom = False
        self.preview_engine.cancel_all()
        self._render_preview_now(interactive=True)
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
        center = (
            self.preview_canvas.winfo_width() / 2,
            self.preview_canvas.winfo_height() / 2,
        )
        self._set_zoom(self.zoom_scale * factor, center)

    def _preview_mouse_wheel(self, event):
        if self.current_source_image is None:
            return "break"
        steps = 0 if event.delta == 0 else event.delta / 120
        self._set_zoom(self.zoom_scale * 1.25 ** steps, (event.x, event.y))
        return "break"

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
        self._constrain_pan_now()
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
            self._preview_item_origin = (
                self._preview_item_origin[0] + dx,
                self._preview_item_origin[1] + dy,
            )

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
        return (
            left > -safety
            or top > -safety
            or right < canvas_width + safety
            or bottom < canvas_height + safety
        )

    def _queue_preview_resize(self, _event):
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(RESIZE_DEBOUNCE_MS, self._refresh_after_resize)

    def _refresh_after_resize(self):
        self._resize_job = None
        if self.current_source_image is not None and self.preview_photo is not None:
            self.preview_engine.cancel_all()
            self._pending_reset_zoom = False
            self._render_preview_now(interactive=True)
            self._schedule_preview_render(interactive=False, quality_delay=90)

    # --- thumbnails / preload --------------------------------------------

    def _ensure_jpeg_window(self, center_index: int) -> None:
        label = self.preloader.request_window(self.all_items, center_index)
        if label:
            self.preload_label.configure(text=label)
        else:
            self.preload_label.configure(text="")

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
            left = max(
                0,
                self.index * self.thumb_slot + self.thumb_slot / 2 - canvas_width / 2,
            )
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
                photo = self._thumbnail(item)
                self.thumb_canvas.create_image(x, self._px(57), image=photo)
            except Exception:
                self.thumb_canvas.create_text(
                    x, self._px(57), text="无法预览", fill="#aab0ba", font=("Segoe UI", 9)
                )
            marker = "★" if item.key in self.kept else ""
            self.thumb_canvas.create_text(
                x - self._px(59),
                self._px(18),
                text=marker,
                fill="#ffd35a",
                font=("Segoe UI Symbol", 12, "bold"),
                anchor="nw",
            )
            if item.paired_raw_jpeg:
                mode_text = (
                    pair_mode_label(self._pair_mode(item))
                    if item.key in self.kept
                    else "未保留"
                )
                self.thumb_canvas.create_text(
                    x + self._px(59),
                    self._px(18),
                    text=mode_text,
                    fill="#8bd7ff",
                    font=("Segoe UI", 7, "bold"),
                    anchor="ne",
                )
            label = path.name
            if len(label) > 18:
                label = label[:16] + "…"
            self.thumb_canvas.create_text(
                x, self._px(115), text=label, fill="#d9dde5", font=("Segoe UI", 8)
            )

    def _thumbnail(self, item: PhotoGroup):
        path = item.primary
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        cache_key = (item.primary_id, mtime_ns)
        cached = self.thumbnail_cache.get(cache_key)
        if cached is not None:
            return cached
        image = decode_photo(
            path, thumbnail=True, thumb_size=thumbnail_decode_size(
                self.thumb_width, self.thumb_height
            )
        )
        image = fit_for_display(image, self.thumb_width, self.thumb_height)
        if image.width < self.thumb_width and image.height < self.thumb_height:
            background = Image.new(
                "RGB", (self.thumb_width, self.thumb_height), "#202329"
            )
            background.paste(
                image,
                (
                    (self.thumb_width - image.width) // 2,
                    (self.thumb_height - image.height) // 2,
                ),
            )
            image = background
        photo = ImageTk.PhotoImage(image)
        self.thumbnail_cache[cache_key] = photo
        while len(self.thumbnail_cache) > THUMB_CACHE_LIMIT:
            self.thumbnail_cache.pop(next(iter(self.thumbnail_cache)))
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

    # --- export / delete -------------------------------------------------

    def export_kept(self):
        if self._export_active:
            messagebox.showinfo(APP_NAME, "已有导出任务在进行中。按 Esc 可取消。")
            return
        if not self.kept:
            messagebox.showinfo(APP_NAME, "还没有保留照片。按 Space 标记后再导出。")
            return
        destination = filedialog.askdirectory(title="选择导出保留照片的文件夹")
        if not destination:
            return
        destination_path = Path(destination)
        kept_items = [item for item in self.all_items if item.key in self.kept]
        sources = self.export_service.plan(kept_items, self.pair_modes)
        answer = messagebox.askyesno(
            APP_NAME,
            f"将导出 {len(kept_items)} 个保留项目（共 {len(sources)} 个原始文件）到：\n"
            f"{destination_path}\n\nRAW+JPG 组按当前模式导出。原照片不会被移动或修改。继续吗？",
        )
        if not answer:
            return
        self._export_active = True
        self.export_service.start(sources, destination_path)
        self._set_status(f"正在导出 0/{len(sources)}… 按 Esc 取消")

    def delete_current(self):
        item = self.current_item
        if item is None:
            return
        victims = list(item.members)
        target = "RAW+JPG 绑定组" if item.paired_raw_jpeg else item.primary.name
        preview = "\n".join(f"· {path.name}" for path in victims[:5])
        if len(victims) > 5:
            preview += f"\n· …（共 {len(victims)} 个文件）"
        answer = messagebox.askyesno(
            APP_NAME,
            f"将「{target}」移入回收站？\n\n{preview}\n\n"
            "文件会进入回收站，之后仍可恢复。本组的保留状态和导出模式会一并移除。",
            icon="warning",
            default="no",
        )
        if not answer:
            return
        failures = send_to_recycle_bin(victims)
        removed = [path for path in victims if not path.exists()]
        if not removed:
            messagebox.showerror(
                APP_NAME,
                "删除失败，文件仍在原处：\n"
                + "\n".join(f"{path.name}：{reason}" for path, reason in failures[:3]),
            )
            return
        was_kept = item.key in self.kept
        self._forget_deleted(item, removed)
        self._ensure_index()
        visible = self.visible_items
        if visible:
            self._show_current(center=True)
        else:
            self.index = 0
            self._show_preview_message("这个文件夹中没有可显示的照片")
            self._update_keep_mode_ui()
            self._render_thumbnails()
        note = f"已移入回收站 {len(removed)} 个文件"
        if was_kept:
            note += "（原为保留项，已移出保留集合）"
        self._set_status(f"{note}    {self._status_text(self.current_item)}")
        if len(removed) < len(victims):
            messagebox.showwarning(
                APP_NAME,
                f"有 {len(victims) - len(removed)} 个文件未能删除：\n"
                + "\n".join(f"{path.name}：{reason}" for path, reason in failures[:3]),
            )

    def _forget_deleted(self, item: PhotoGroup, removed: list[Path]) -> None:
        self.preview_engine.cancel_all()
        self._cancel_ui_render_jobs()
        self.image_loader.cancel_pending()
        self.preloader.invalidate()

        removed_ids = {str(path.resolve()) for path in removed}
        removed_ids.add(item.primary_id)
        for path_id in removed_ids:
            self.jpeg_cache.pop(path_id)
            self.preview_engine.drop_levels_for(path_id)
            for key in [k for k in self.thumbnail_cache if k[0] == path_id]:
                self.thumbnail_cache.pop(key, None)

        self.all_items = [c for c in self.all_items if c.key != item.key]
        self.kept.discard(item.key)
        self.pair_modes.pop(item.key, None)
        self._invalidate_visible()
        if self.current_source_id in removed_ids or (
            self.current_source_path is not None
            and str(self.current_source_path.resolve()) in removed_ids
        ):
            self.current_source_image = None
            self.current_source_path = None
            self.current_source_id = None
            self._loading_path_id = None
        self._save_selection()

    # --- status / shutdown -----------------------------------------------

    def _status_text(self, item: PhotoGroup | None) -> str:
        base = self._status_note + "    " if self._status_note else ""
        if item is None:
            return f"{base}保留 {len(self.kept)} 张照片"
        prefix = "★ 已保留" if item.key in self.kept else "未保留"
        if item.paired_raw_jpeg and item.key in self.kept:
            paired = f"    绑定组：{pair_mode_label(self._pair_mode(item))}"
        elif item.paired_raw_jpeg:
            paired = "    RAW+JPG 绑定组"
        else:
            paired = ""
        return (
            f"{base}{self.index + 1} / {len(self.visible_items)}    {prefix}"
            f"    已保留 {len(self.kept)} 个项目{paired}    {item.primary.name}"
        )

    def _set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _on_close(self):
        """Stop background workers before Tk tears down its image runtime."""
        self.export_service.cancel()
        self.preloader.invalidate()
        self.image_loader.cancel_pending()
        self.preview_engine.cancel_all()
        self._cancel_ui_render_jobs()
        if getattr(self, "_poll_job", None) is not None:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
        self.preview_engine.shutdown()
        self.image_loader.shutdown()
        self.preloader.close()
        self.export_service.close()
        self.destroy()


def main() -> None:
    import sys

    try:
        if "--self-test" in sys.argv:
            probe = tk.Tcl()
            probe.eval("package require Tk")
            print("Photo Culler runtime OK")
            raise SystemExit(0)
        PhotoCuller().mainloop()
    except SystemExit:
        raise
    except Exception as error:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, f"程序启动失败：\n{error}")
        root.destroy()
        raise
