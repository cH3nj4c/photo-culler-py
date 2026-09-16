"""PySide6 presentation layer for Photo Culler, with the GPU preview engine.

Functional parity with the Tkinter shell (ui.py): folder scanning, RAW+JPG
grouping, keep/mode state, "只看保留" filter, recycle-bin delete, background
export, JPEG preload window, and a virtualized thumbnail strip.

The preview itself is a :class:`gpu_preview.PhotoStage`: photos upload once
as GPU textures and pan/zoom/interpolation run on the GPU (VisPy/OpenGL).
The Tk shell stays available as a fallback when PySide6/vispy are missing;
`app.py` picks the shell at startup.

Same architecture rules as the Tk version: services communicate through
queues drained by a 16 ms timer; the main thread only paints and handles
events; generation numbers drop stale results.
"""

from __future__ import annotations

import os
import queue
import sys
from pathlib import Path
from threading import Thread

os.environ.setdefault("QT_API", "pyside6")

import numpy as np
from PIL import Image
from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from vispy import app as vispy_app

from app_icon import logo_candidates
from config import (
    APP_NAME,
    APP_VERSION,
    PREVIEW_CACHE_LONG_EDGE,
    THUMB_CACHE_LIMIT,
    THUMB_HEIGHT,
    THUMB_SLOT,
    THUMB_WIDTH,
    ZOOM_FULLRES_SETTLE_MS,
    ZOOM_PREVIEW_NATIVE_MARGIN,
)
from domain import (
    PhotoGroup,
    build_photo_groups,
    filter_visible_items,
    next_pair_mode,
    normalize_pair_mode,
    pair_mode_label,
    scan_photo_tree,
)
from export_service import ExportService
from gpu_preview import PhotoStage
import gpu_accel
import gpu_info
from image_loader import ImageLoader
from jpeg_preloader import JpegCache, JpegPreloader
from selection_store import load_selection, save_selection
from sysmem import describe_cache_plan, recommend_jpeg_cache_limit
from temp_cleanup import cleanup_on_exit
from thumbnail_service import ThumbnailService
from version_info import about_text
from winshell import send_to_recycle_bin

THUMB_PIX_HEIGHT = 116  # 8px frame + 88px thumb + filename strip
# Clear air between the slot bottom and the scrollbar, so the filename painted
# at the bottom of every slot always stays readable. The scrollbar's own room
# is measured in `_fit_strip_height`, not guessed.
STRIP_NAME_GAP = 8
# Every action lives in a settings-style sidebar down the left edge; the
# per-folder context (folder name, counters) lives in a mirror sidebar on the
# right edge, so the left column stays a pure action menu.
SIDEBAR_WIDTH = 172
SIDEBAR_RIGHT_WIDTH = 196
HINT_TEXT = (
    "← → 切换    Space 保留    Del 删除    F 模式    "
    "滚轮缩放    Z 适合 / 100%    Esc 取消导出    点击查看全部快捷键（F1）"
)

DARK_QSS = """
QWidget { background: #171A1F; color: #E8EAED; font-family: "Segoe UI"; font-size: 10pt; }
QLabel#folder { font-size: 10pt; font-weight: 600; padding: 2px 2px 6px 2px; }
QLabel#section { color: #6F7783; font-size: 9pt; font-weight: 600; padding: 8px 2px 1px 2px; }
QWidget#sidebar { background: #14171C; border-right: 1px solid #22262E; }
QWidget#sidebar QPushButton { text-align: left; padding: 7px 10px; }
QWidget#sidebarRight { background: #14171C; border-left: 1px solid #22262E; }
QLabel#folderName {
    background: #1B2029; border: 1px solid #262C36; border-radius: 6px;
    padding: 6px 8px; font-size: 10pt; font-weight: 600;
}
QLabel#meta { color: #AEB6C2; }
QLabel#metaDim { color: #6F7783; }
QLabel#hint { background: #14171C; color: #8B939E; padding: 5px 16px; }
QLabel#zoom { color: #4f9cff; font-weight: 600; }
QLabel#preload { color: #8B939E; }
QLabel#empty { color: #bdc3cd; font-size: 16pt; }
QPushButton {
    background: #252A33; border: 1px solid #2A303A; border-radius: 6px;
    padding: 5px 13px; color: #E8EAED;
}
QPushButton:hover { background: #2E3540; }
QPushButton:pressed { background: #4f9cff; color: #0E1014; }
QPushButton:focus { border: 1px solid #4f9cff; }
QPushButton:disabled { color: #6b7280; background: #20242B; border-color: #23272e; }
QPushButton#accent { background: #4f9cff; color: #0E1014; font-weight: 600; border: none; }
QPushButton#accent:hover { background: #6bb0ff; }
QPushButton#accent:pressed { background: #9ec2ff; }
QPushButton#danger { background: #3A2226; border: 1px solid #5A3038; color: #F3E0E2; }
QPushButton#danger:hover { background: #4C2C32; }
QPushButton#danger:pressed { background: #ef9a9a; color: #1A0D0F; }
QPushButton#toggle:checked { background: #4f9cff; color: #0E1014; border: none; font-weight: 600; }
/* Version footer: reads as dim text, still behaves like a button. */
QPushButton#link {
    background: transparent; border: none; padding: 2px 0; text-align: left;
    color: #6F7783; font-size: 9pt;
}
QPushButton#link:hover { color: #4f9cff; }
QListWidget { background: #1C2027; border: none; outline: none; }
QListWidget::item { border: 1px solid transparent; border-radius: 8px; padding: 1px; }
QListWidget::item:selected { border: 1px solid #4f9cff; background: #15171B; }
QStatusBar { background: #171A1F; }
QMenu { background: #2a2f38; color: #e7e9ed; border: 1px solid #3a4250; }
QMenu::item { padding: 6px 24px; }
QMenu::item:selected { background: #3a4250; }
QScrollBar:horizontal { background: #1C2027; height: 10px; }
QScrollBar::handle:horizontal { background: #3a4150; border-radius: 4px; min-width: 40px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""


def _pil_to_qimage(image: Image.Image) -> QImage:
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    width, height = rgb.size
    data = rgb.tobytes()
    return QImage(data, width, height, 3 * width, QImage.Format.Format_RGB888).copy()


def freeze_startup_garbage() -> None:
    """Keep the session-long object graph out of the GC's periodic scan.

    This app holds a deep VisPy/Qt visual tree plus multi-megabyte image
    arrays for the whole session. A full generational collection has to walk
    all of it and blocks the UI thread for ~70 ms — which shows up as a
    one-off hitch in the middle of a zoom gesture, at an unpredictable
    moment. Freezing the startup objects into the permanent generation (the
    standard mitigation for this) drops that pause to noise; measured worst
    frame went from 72 ms to 8 ms while the median was unchanged.

    Called once after the window and its services exist, before any photo is
    loaded, so nothing transient gets pinned.
    """

    import gc

    gc.collect()
    gc.freeze()
    # Fewer, cheaper gen-0 passes now that the big graph is untracked, and a
    # much rarer full collection on top of that.
    gc.set_threshold(20_000, 250, 250)


class FilmStrip(QListWidget):
    """Icon-mode strip whose mouse wheel scrolls horizontally."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Sub-notch wheel deltas (precision trackpads, free-spinning wheels)
        # are accumulated here instead of being rounded away to nothing.
        self._wheel_remainder = 0.0

    def wheelEvent(self, event) -> None:  # noqa: N802
        pixel = event.pixelDelta()
        if not pixel.isNull():
            # Trackpads usually report real pixels.
            step = float(pixel.y() or pixel.x())
        else:
            angle = event.angleDelta()
            # A horizontal gesture carries x, a vertical one y; accept both so
            # a sideways swipe on a trackpad still scrolls the strip.
            step = (angle.y() or angle.x()) / 120.0 * 96.0
        if step:
            total = self._wheel_remainder - step
            whole = int(total)
            self._wheel_remainder = total - whole
            if whole:
                bar = self.horizontalScrollBar()
                bar.setValue(bar.value() + whole)
        event.accept()


class PhotoCullerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        for path in logo_candidates():
            try:
                self.setWindowIcon(QIcon(str(path)))
                break
            except Exception:
                continue
        self.resize(1440, 900)
        # The sidebar takes a fixed slice off the left, so keep enough width
        # that the preview and the hint bar are not squeezed at minimum size.
        self.setMinimumSize(QSize(1040, 620))

        # Folder session / selection state (mirrors ui.py).
        self.folder: Path | None = None
        self.all_items: list[PhotoGroup] = []
        self.index = 0
        self.kept: set[str] = set()
        self.pair_modes: dict[str, str] = {}
        self.show_kept_only = False
        self._visible_items: list[PhotoGroup] | None = None

        # Current photo state.
        self._current_path: Path | None = None
        self._current_path_id: str | None = None
        self._original_size: tuple[int, int] = (1, 1)
        self._preview_pil: Image.Image | None = None
        # Full-resolution pixels arrive as a ready-to-upload uint8 array,
        # converted on the decode thread rather than the UI thread.
        self._full_pixels: np.ndarray | None = None
        # Decoded but not yet uploaded: held back until the gesture settles.
        self._full_ready: tuple[str, np.ndarray, tuple[int, int]] | None = None
        self._using_full = False
        self._loading_path_id: str | None = None
        self._loading_full_path_id: str | None = None
        self._after_show_done = False
        self._syncing_filmstrip = False
        self.auto_open_enabled = True
        # Cached hardware report; detection touches the registry and DXGI, so
        # it runs once and is reused by the menu, the dialog and the panel.
        self._gpu_report: gpu_info.GpuReport | None = None

        # Background services. PreviewEngine/resample_backend are not needed:
        # the GPU stage replaces the whole crop+resample pipeline.
        self.jpeg_cache = JpegCache(recommend_jpeg_cache_limit())
        self.preloader = JpegPreloader(self.jpeg_cache)
        self.image_loader = ImageLoader(self.jpeg_cache)
        self.export_service = ExportService()
        self.thumbnail_service = ThumbnailService()
        self._export_active = False

        self._thumb_pil: dict[tuple, Image.Image] = {}
        self._status_note = ""
        self._scan_events: queue.Queue = queue.Queue()
        self._scan_generation = 0
        self._scan_cancel = False
        self._scan_active = False
        self._pending_scan_folder: Path | None = None

        self._build_ui()
        self._install_shortcuts()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_services)
        self._poll_timer.start(16)
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._tick_status)
        self._status_timer.start(100)
        # Swapping in a full-resolution texture costs tens of milliseconds, so
        # it waits until the zoom/pan gesture has been quiet for a moment.
        self._full_apply_timer = QTimer(self)
        self._full_apply_timer.setSingleShot(True)
        self._full_apply_timer.timeout.connect(self._apply_ready_full)
        QTimer.singleShot(250, self._auto_open)

    # --- layout / chrome -------------------------------------------------

    def _build_ui(self) -> None:
        def make_button(
            label: str,
            callback,
            *,
            name: str | None = None,
            checkable: bool = False,
            tip: str = "",
        ) -> QPushButton:
            btn = QPushButton(label)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            if name:
                btn.setObjectName(name)
            if checkable:
                btn.setCheckable(True)
            if tip:
                btn.setToolTip(tip)
            btn.clicked.connect(callback)
            return btn

        # --- left sidebar: every action, grouped like a settings menu ---
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 12, 12, 12)
        side.setSpacing(5)

        def section(title: str) -> None:
            label = QLabel(title)
            label.setObjectName("section")
            side.addWidget(label)

        section("文件")
        side.addWidget(make_button("打开文件夹", self.open_folder, tip="O"))
        side.addWidget(make_button("导出保留照片", self.export_kept, tip="E"))

        section("选片")
        self.keep_button = make_button(
            "保留 / 取消", self.toggle_keep, name="accent", tip="Space"
        )
        side.addWidget(self.keep_button)
        self.keep_mode_button = make_button("模式", self.cycle_keep_mode, tip="F")
        side.addWidget(self.keep_mode_button)
        # objectName "toggle" is what the :checked rule in DARK_QSS keys on;
        # without it the filter button showed no sign of being switched on.
        self.filter_button = make_button(
            "只看保留",
            self.toggle_filter,
            name="toggle",
            checkable=True,
            tip="只显示已保留的照片",
        )
        side.addWidget(self.filter_button)

        section("编辑")
        self.delete_button = make_button(
            "删除到回收站", self.delete_current, name="danger", tip="Del"
        )
        side.addWidget(self.delete_button)

        section("视图")
        side.addWidget(make_button("适合窗口", self.zoom_fit, tip="Z"))
        side.addWidget(make_button("100% 实际尺寸", self.zoom_actual, tip="1"))

        section("加速")
        self.accel_button = make_button(
            "GPU 加速…", self._show_accel_menu, tip="选择显卡加速方案 / 查看显卡状态"
        )
        side.addWidget(self.accel_button)

        side.addStretch(1)
        self.more_button = make_button("更多…", self._show_more_menu, tip="F1 快捷键")
        side.addWidget(self.more_button)
        sidebar.setFixedWidth(SIDEBAR_WIDTH)

        # --- right sidebar: per-folder context (name, counts, preload) ---
        sidebar_right = QWidget()
        sidebar_right.setObjectName("sidebarRight")
        sider = QVBoxLayout(sidebar_right)
        sider.setContentsMargins(12, 12, 12, 12)
        sider.setSpacing(5)

        folder_header = QLabel("当前文件夹")
        folder_header.setObjectName("section")
        sider.addWidget(folder_header)

        self.folder_label = QLabel("尚未打开文件夹")
        self.folder_label.setObjectName("folderName")
        self.folder_label.setWordWrap(True)
        self.folder_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        sider.addWidget(self.folder_label)

        self.folder_count_label = QLabel("")
        self.folder_count_label.setObjectName("metaDim")
        sider.addWidget(self.folder_count_label)

        sider.addStretch(1)

        # Acceleration state, pinned at the bottom: what hardware was detected
        # and which scheme is currently selected. Read-only context.
        accel_header = QLabel("加速")
        accel_header.setObjectName("section")
        sider.addWidget(accel_header)

        self.accel_scheme_label = QLabel("")
        self.accel_scheme_label.setObjectName("meta")
        self.accel_scheme_label.setWordWrap(True)
        sider.addWidget(self.accel_scheme_label)

        self.accel_gpu_label = QLabel("")
        self.accel_gpu_label.setObjectName("metaDim")
        self.accel_gpu_label.setWordWrap(True)
        sider.addWidget(self.accel_gpu_label)

        # Version footer: answers "am I running the build I just installed?"
        # at a glance, which is exactly what the filename alone could not.
        # A flat button rather than a clickable QLabel — monkey-patching
        # mousePressEvent onto a QLabel instance is fragile under PySide6's
        # virtual dispatch, and a button gets hover/focus styling for free.
        self.version_button = QPushButton(f"v{APP_VERSION}")
        self.version_button.setObjectName("link")
        self.version_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.version_button.setToolTip("查看版本、安装包名称与显卡详情")
        self.version_button.clicked.connect(self._show_about)
        sider.addWidget(self.version_button)

        sidebar_right.setFixedWidth(SIDEBAR_RIGHT_WIDTH)

        # --- right column: preview, hint bar, filmstrip ---
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # Preview stage + empty-state page.
        self.stage = PhotoStage()
        self._empty_label = QLabel("打开一个照片文件夹开始选片")
        self._empty_label.setObjectName("empty")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stack = QWidget()
        self._stack_layout = QVBoxLayout(self._stack)
        self._stack_layout.setContentsMargins(0, 0, 0, 0)
        self._stack_layout.addWidget(self.stage)
        self._stack_layout.addWidget(self._empty_label)
        self._show_empty_page(True)
        right_layout.addWidget(self._stack, 1)

        hint = QLabel(HINT_TEXT)
        hint.setObjectName("hint")
        right_layout.addWidget(hint)

        self.filmstrip = FilmStrip()
        self.filmstrip.setViewMode(QListWidget.ViewMode.IconMode)
        self.filmstrip.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.filmstrip.setMovement(QListWidget.Movement.Static)
        self.filmstrip.setUniformItemSizes(True)
        self.filmstrip.setWrapping(False)
        self.filmstrip.setFlow(QListWidget.Flow.LeftToRight)
        self.filmstrip.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.filmstrip.setIconSize(QSize(THUMB_SLOT, THUMB_PIX_HEIGHT))
        self.filmstrip.setGridSize(QSize(THUMB_SLOT, THUMB_PIX_HEIGHT))
        self.filmstrip.setSpacing(6)
        self.filmstrip.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.filmstrip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._fit_strip_height()
        self.filmstrip.currentRowChanged.connect(self._on_filmstrip_row_changed)
        right_layout.addWidget(self.filmstrip)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(sidebar)
        layout.addWidget(right, 1)
        layout.addWidget(sidebar_right)
        self.setCentralWidget(central)

        status_host = QWidget()
        status_layout = QHBoxLayout(status_host)
        status_layout.setContentsMargins(16, 2, 16, 2)
        self.status_label = QLabel("")
        self.preload_label = QLabel("")
        self.preload_label.setObjectName("preload")
        self.zoom_label = QLabel("—")
        self.zoom_label.setObjectName("zoom")
        status_layout.addWidget(self.status_label, 1)
        status_layout.addWidget(self.preload_label)
        status_layout.addSpacing(14)
        status_layout.addWidget(self.zoom_label)
        statusBar = self.statusBar()
        statusBar.addWidget(status_host, 1)

    def _fit_strip_height(self) -> None:
        """Size the strip so the scrollbar can never cover the photo names.

        The slot's filename is painted at the bottom of each 148x116 pixmap, so
        the viewport must be at least a slot tall. How much room the horizontal
        scrollbar needs depends on the style, the font metrics and the display
        scale — hardcoding it is what let the scrollbar cover the names on some
        setups.

        The room is taken from the scrollbar's own size hint plus the scroll
        area's frame, which are available before any layout pass (measuring
        ``height() - viewport().height()`` is *not*: both are provisional until
        the widget has been laid out, and the value they produce is garbage).
        """
        strip = self.filmstrip
        bar = strip.horizontalScrollBar()
        overhead = bar.sizeHint().height() + 2 * max(1, strip.frameWidth())
        strip.setFixedHeight(THUMB_PIX_HEIGHT + overhead + STRIP_NAME_GAP)

    def _show_empty_page(self, empty: bool) -> None:
        self._empty_label.setVisible(empty)
        self.stage.setVisible(not empty)

    def _install_shortcuts(self) -> None:
        bindings = [
            (QKeySequence(Qt.Key.Key_Left), lambda: self.change_index(-1)),
            (QKeySequence(Qt.Key.Key_Right), lambda: self.change_index(1)),
            (QKeySequence(Qt.Key.Key_Space), self.toggle_keep),
            (QKeySequence(Qt.Key.Key_F), self.cycle_keep_mode),
            (QKeySequence(Qt.Key.Key_O), self.open_folder),
            (QKeySequence(Qt.Key.Key_E), self.export_kept),
            (QKeySequence(Qt.Key.Key_Delete), self.delete_current),
            (QKeySequence(Qt.Key.Key_Escape), self._on_escape),
            (QKeySequence(Qt.Key.Key_Z), self.toggle_zoom),
            (QKeySequence(Qt.Key.Key_1), self.zoom_actual),
            (QKeySequence(Qt.Key.Key_Plus), lambda: self.zoom_step_ui(1)),
            (QKeySequence(Qt.Key.Key_Equal), lambda: self.zoom_step_ui(1)),
            (QKeySequence(Qt.Key.Key_Minus), lambda: self.zoom_step_ui(-1)),
            (QKeySequence("Ctrl+Shift+X"), self.clear_all_kept),
            (QKeySequence("Ctrl+Shift+M"), self.reset_all_pair_modes),
            (QKeySequence(Qt.Key.Key_F1), self._show_shortcuts),
            (QKeySequence(Qt.Key.Key_Question), self._show_shortcuts),
        ]
        for sequence, callback in bindings:
            action = QAction(self)
            action.setShortcut(sequence)
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            action.triggered.connect(callback)
            self.addAction(action)

    def _show_more_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("全部不保留", self.clear_all_kept)
        menu.addAction("重置所有 RAW/JPG 模式", self.reset_all_pair_modes)
        menu.addSeparator()
        menu.addAction("GPU 加速…", self._show_accel_menu)
        menu.addAction(f"关于 {APP_NAME}…", self._show_about)
        menu.addAction("快捷键说明", self._show_shortcuts)
        point = self.more_button.mapToGlobal(self.more_button.rect().bottomLeft())
        menu.exec(point)

    def _show_about(self) -> None:
        """Version + detected hardware, so a build can be identified on sight.

        The text is also put on the clipboard, because "which version are you
        running?" is normally answered by pasting it into a message. The dialog
        says so rather than overwriting the clipboard silently.
        """
        info = self.stage.gpu_info or {}
        text = about_text(renderer=str(info.get("renderer", "")))
        if self.folder:
            text += f"\n\n当前文件夹：{self.folder}"
        text += f"\n照片数量：{len(self.all_items)}"
        try:
            QApplication.clipboard().setText(text)
        except Exception:  # noqa: BLE001 - clipboard is a nicety, not the point
            pass
        else:
            text += "\n\n（以上信息已复制到剪贴板）"
        QMessageBox.information(self, f"关于 {APP_NAME}", text)

    # --- GPU acceleration --------------------------------------------------

    def gpu_report(self) -> gpu_info.GpuReport:
        """Detected hardware, cached. Refreshes once the GL context is known."""
        stage_info = self.stage.gpu_info
        if self._gpu_report is None or (
            stage_info and not self._gpu_report.renderer
        ):
            self._gpu_report = gpu_info.detect_gpu(
                renderer=str((stage_info or {}).get("renderer", "")),
                opengl_version=str((stage_info or {}).get("opengl", "")),
                gl_vendor=str((stage_info or {}).get("vendor", "")),
            )
        return self._gpu_report

    def _update_accel_panel(self) -> None:
        report = self.gpu_report()
        # What this process is running under (env wins), not just what is
        # stored — otherwise the panel would claim "自动" while the renderer is
        # actually software.
        active_id = gpu_accel.effective_scheme_id()
        stored_id = gpu_accel.current_scheme_id()
        scheme = gpu_accel.get_scheme(active_id)
        available, reason = gpu_accel.scheme_availability(scheme, report)
        state = scheme.label if available else f"{scheme.label}（{reason}）"
        if stored_id != active_id:
            # The choice was made after this process started, so the OpenGL
            # side of it cannot take effect until the next launch.
            state += f" → {gpu_accel.get_scheme(stored_id).label}（重启后生效）"
        self.accel_scheme_label.setText(f"方案：{state}")
        self.accel_gpu_label.setText(report.headline())
        tip = (
            f"当前方案：{scheme.label}\n"
            f"{scheme.summary}\n\n"
            f"检测到的显卡：\n"
            + "\n".join(f"  · {a.kind_label}：{a.describe()}" for a in report.adapters)
        )
        if report.renderer:
            tip += f"\n\n实际渲染器：{report.renderer}"
        self.accel_scheme_label.setToolTip(tip)
        self.accel_gpu_label.setToolTip(tip)

    def build_accel_menu(self) -> QMenu:
        """Construct the acceleration menu without showing it.

        Split from ``_show_accel_menu`` because ``QMenu.exec`` blocks, which
        would make the menu impossible to inspect from a test.
        """
        report = self.gpu_report()
        menu = QMenu(self)

        header = menu.addAction(f"检测到：{report.headline()}")
        header.setEnabled(False)
        kinds = []
        if report.has_discrete:
            kinds.append("独立显卡")
        if report.has_integrated:
            kinds.append("集成显卡")
        shape = " + ".join(kinds) if kinds else "未识别"
        shape_item = menu.addAction(
            f"类型：{shape}（混合显卡）" if report.is_hybrid else f"类型：{shape}"
        )
        shape_item.setEnabled(False)
        menu.addSeparator()

        # Exclusive radio group: the schemes are mutually exclusive by nature.
        self._accel_action_group = QActionGroup(menu)
        self._accel_action_group.setExclusive(True)
        effective = gpu_accel.effective_scheme_id()
        for scheme in gpu_accel.SCHEMES:
            action = QAction(
                f"{scheme.label}    — {gpu_accel.describe_effect(scheme.id, report)}",
                menu,
            )
            action.setCheckable(True)
            action.setChecked(scheme.id == effective)
            action.setData(scheme.id)
            available, reason = gpu_accel.scheme_availability(scheme, report)
            if not available:
                # Still shown, so the reason is discoverable, but not pickable.
                action.setEnabled(False)
                action.setToolTip(reason)
                action.setText(f"{scheme.label}    — 不可用：{reason}")
            else:
                action.setToolTip(f"{scheme.summary}\n\n{scheme.detail}")
                action.triggered.connect(
                    lambda _checked=False, sid=scheme.id: self._select_accel_scheme(sid)
                )
            self._accel_action_group.addAction(action)
            menu.addAction(action)

        menu.addSeparator()
        menu.addAction("查看显卡详情…", self._show_gpu_details)
        return menu

    def _show_accel_menu(self) -> None:
        menu = self.build_accel_menu()
        point = self.accel_button.mapToGlobal(self.accel_button.rect().bottomLeft())
        menu.exec(point)

    def _select_accel_scheme(self, scheme_id: str) -> None:
        scheme = gpu_accel.get_scheme(scheme_id)
        # Nothing to do only when both the stored choice and the running one
        # already match; an external PHOTOCULLER_ACCEL override makes those
        # differ, and picking the stored one must then still take effect.
        if (
            scheme_id == gpu_accel.current_scheme_id()
            and scheme_id == gpu_accel.effective_scheme_id()
        ):
            return
        ok, message = gpu_accel.apply_scheme(scheme_id)
        self._update_accel_panel()
        if not ok:
            QMessageBox.warning(self, APP_NAME, message)
            return
        # Every scheme is read at process start, so say so rather than letting
        # the user think nothing happened.
        self._set_status(f"加速方案已切换为「{scheme.label}」· {message}")
        QMessageBox.information(
            self,
            APP_NAME,
            f"已选择「{scheme.label}」。\n\n{scheme.summary}\n\n{message}",
        )

    def _show_gpu_details(self) -> None:
        report = self.gpu_report()
        lines: list[str] = []
        if report.adapters:
            lines.append(f"检测到 {len(report.adapters)} 个显示适配器：\n")
            for adapter in report.adapters:
                mark = "（当前可用）" if adapter.runtime_visible else ""
                lines.append(f"· {adapter.kind_label}：{adapter.name}{mark}")
                detail = []
                if adapter.vendor:
                    detail.append(f"厂商 {adapter.vendor}")
                if adapter.dedicated_vram_mb:
                    detail.append(f"专用显存 {adapter.dedicated_vram_mb} MB")
                if adapter.driver_version:
                    detail.append(f"驱动 {adapter.driver_version}")
                if detail:
                    lines.append("    " + " · ".join(detail))
            lines.append("")
            if report.is_hybrid:
                lines.append("这是混合显卡机器：集成显卡省电，独立显卡性能更强。")
            elif report.has_discrete:
                lines.append("只检测到独立显卡。")
            elif report.has_integrated:
                lines.append("只检测到集成显卡。")
            lines.append("")
        else:
            lines.append("未能检测到显示适配器信息。\n")

        if report.renderer:
            lines.append(f"预览实际使用的渲染器：\n  {report.renderer}")
        if report.gl_vendor:
            lines.append(f"GL 厂商：{report.gl_vendor}")
        if report.opengl_version:
            lines.append(f"OpenGL 版本：{report.opengl_version}")
        if not report.renderer:
            lines.append("预览渲染器：尚未就绪（窗口显示后才可查询）")

        lines.append("")
        active_id = gpu_accel.effective_scheme_id()
        stored_id = gpu_accel.current_scheme_id()
        lines.append(f"当前加速方案：{gpu_accel.get_scheme(active_id).label}")
        if stored_id != active_id:
            lines.append(f"已选（重启后生效）：{gpu_accel.get_scheme(stored_id).label}")
        lines.append(f"Windows 显卡偏好：{gpu_accel.stored_preference_label()}")
        lines.append(
            f"重采样后端：{gpu_accel.active_resample_mode() or '自动'}"
        )
        if report.errors:
            lines.append("")
            lines.append("检测过程中的问题：")
            lines.extend(f"· {e}" for e in report.errors)

        QMessageBox.information(self, f"{APP_NAME} — 显卡状态", "\n".join(lines))

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            APP_NAME,
            "选片\n"
            "  ← →     上一张 / 下一张（循环）\n"
            "  Space   保留 / 取消保留\n"
            "  F       切换 RAW+JPG 导出模式\n"
            "  Del     删除当前组（回收站）\n\n"
            "文件\n"
            "  O       打开文件夹\n"
            "  E       导出保留照片\n"
            "  Esc     取消导出\n\n"
            "查看（GPU 预览）\n"
            "  滚轮    无极缩放（锚点跟随鼠标）\n"
            "  Z / 1   适合屏幕 / 100%\n"
            "  + / −   放大 / 缩小（平滑）\n"
            "  拖拽    平移\n\n"
            "批量\n"
            "  Ctrl+Shift+X   全部不保留\n"
            "  Ctrl+Shift+M   重置所有模式\n\n"
            "加速\n"
            "  左栏「GPU 加速…」选择显卡加速方案，\n"
            "  并查看检测到的显卡状态\n\n"
            "F1 或 ？ 可再次打开本说明",
        )

    def initialize_after_show(self) -> None:
        """Deferred GPU context discovery (QOpenGLWidget must be shown first)."""
        if self._after_show_done:
            return
        self._after_show_done = True
        self.stage.initialize_gpu_info()
        freeze_startup_garbage()
        # Now that the widget is polished, re-apply: the scrollbar's real cost
        # depends on the style that only became active once shown.
        self._fit_strip_height()
        # Hardware detection is best-effort and must never block the window
        # from appearing, so it happens here rather than in the constructor.
        try:
            self._update_accel_panel()
        except Exception:  # noqa: BLE001 - the panel is informational only
            pass

    def _auto_open(self) -> None:
        if self.auto_open_enabled:
            self.open_folder()

    # --- session state ---------------------------------------------------

    def _invalidate_visible(self) -> None:
        self._visible_items = None

    @property
    def visible_items(self) -> list[PhotoGroup]:
        if self._visible_items is None:
            self._visible_items = filter_visible_items(
                self.all_items, self.kept, self.show_kept_only
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
            self.keep_mode_button.setText(pair_mode_label(self._pair_mode(item)))
            self.keep_mode_button.setEnabled(True)
        else:
            self.keep_mode_button.setText("模式")
            self.keep_mode_button.setEnabled(False)

    def _save_selection(self) -> None:
        error = save_selection(self.folder, self.kept, self.pair_modes)
        self._status_note = error or ""

    # --- folder open / scan ------------------------------------------------

    def open_folder(self) -> None:
        if not self.auto_open_enabled and not self.folder:
            return
        chosen = QFileDialog.getExistingDirectory(
            self, "选择包含照片的文件夹",
            str(self.folder) if self.folder else "",
        )
        if not chosen:
            return
        self._open_folder_path(Path(chosen))

    def _open_folder_path(self, folder: Path) -> None:
        # Cancel any in-flight tree walk from a previous folder choice.
        self._scan_cancel = True
        self._reset_session_caches()
        cache_limit = self.jpeg_cache.retune_from_system_memory()

        self.folder = folder
        name = folder.name if folder.name else str(folder)
        self.folder_label.setText(name)
        self.folder_label.setToolTip(str(folder))
        self.folder_count_label.setText("正在扫描…")
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION} — {name}")
        self.all_items = []
        self.index = 0
        self._invalidate_visible()
        self.kept = set()
        self.pair_modes = {}
        self._update_keep_mode_ui()
        self._render_thumbnails()
        self._set_status(
            f"正在扫描文件夹…    {describe_cache_plan(cache_limit)}    "
            f"{self.stage.describe_backend()}"
        )
        self.zoom_label.setText("—")

        self._scan_generation += 1
        generation = self._scan_generation
        self._scan_cancel = False
        self._scan_active = True
        self._pending_scan_folder = folder

        def should_cancel() -> bool:
            return self._scan_cancel or generation != self._scan_generation

        def on_progress(found_n: int, dirs_n: int, errors_n: int) -> None:
            self._scan_events.put((generation, "progress", found_n, dirs_n, errors_n))

        def work() -> None:
            try:
                entries, dirs_visited, errors = scan_photo_tree(
                    folder,
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                )
                # Group here, off the UI thread. build_photo_groups resolves one
                # directory per folder and Path.resolve() is a real filesystem
                # call (~1 ms), so for a folder tree with many subdirectories it
                # still costs ~1 s. The scanner has already walked every
                # directory by now, so this is the natural place for it.
                groups: list[PhotoGroup] | None = None
                if not should_cancel():
                    mtimes = {str(path): mtime for path, mtime in entries}
                    groups = build_photo_groups(
                        [path for path, _mtime in entries], mtimes
                    )
                self._scan_events.put(
                    (generation, "done", entries, dirs_visited, errors, None, groups)
                )
            except Exception as exc:  # pragma: no cover - scan crash guard
                self._scan_events.put((generation, "done", [], 0, 0, exc, None))

        Thread(target=work, name="photo-culler-scan", daemon=True).start()

    def _reset_session_caches(self) -> None:
        self.image_loader.cancel_pending()
        self.preloader.invalidate()
        self.jpeg_cache.clear()
        self.thumbnail_service.cancel_pending()
        self._thumb_pil.clear()
        self._preview_pil = None
        self._full_pixels = None
        self._full_ready = None
        self._using_full = False
        self._loading_path_id = None
        self._loading_full_path_id = None
        self._current_path = None
        self._current_path_id = None
        self._original_size = (1, 1)
        self._status_note = ""
        self.stage.clear_image()
        self._show_empty_page(True)

    def _handle_scan_events(self) -> None:
        latest_progress = None
        terminal = None
        try:
            while True:
                event = self._scan_events.get_nowait()
                if event[0] != self._scan_generation:
                    continue
                if event[1] == "progress":
                    latest_progress = event
                elif event[1] == "done":
                    terminal = event
        except queue.Empty:
            pass

        if latest_progress is not None:
            _, _kind, found_n, dirs_n, errors_n = latest_progress
            extra = f" · 异常 {errors_n}" if errors_n else ""
            self._set_status(
                f"正在扫描… 已发现 {found_n} 个文件 · 已访问 {dirs_n} 个目录{extra}"
            )

        if terminal is None:
            return

        generation, _kind, entries, dirs_visited, errors, error, groups = terminal
        self._scan_active = False
        folder = self._pending_scan_folder or self.folder
        if error is not None or folder is None:
            QMessageBox.critical(self, APP_NAME, f"无法读取这个文件夹：\n{error}")
            return

        if groups is None:
            # Fallback: an older/cancelled scan that could not group up front.
            mtime_ns_by_path = {str(path): mtime_ns for path, mtime_ns in entries}
            groups = build_photo_groups(
                [path for path, _mtime in entries], mtime_ns_by_path
            )
        self.all_items = groups
        self.index = 0
        self._invalidate_visible()

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

        cache_limit = self.jpeg_cache.limit
        if not self.all_items:
            self._update_keep_mode_ui()
            self._render_thumbnails()
            self._update_folder_count()
            note = f"    · 异常 {errors}" if errors else ""
            self._set_status(
                f"0 张照片 · 目录 {dirs_visited}{note}    "
                f"支持 JPG、PNG、TIFF 及主流相机 RAW    "
                f"{describe_cache_plan(cache_limit)}    "
                f"{self.stage.describe_backend()}"
            )
            self.zoom_label.setText("—")
            return
        self._render_thumbnails(center=True)
        self._show_current(center=True)
        self._update_folder_count()
        if errors:
            self._status_note = f"扫描完成，{errors} 项读取异常"
        else:
            self._status_note = ""

    # --- selection actions -------------------------------------------------

    def change_index(self, direction: int) -> None:
        items = self.visible_items
        if not items:
            return
        self.index = (self.index + direction) % len(items)
        self._show_current(center=False)

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
        self._refresh_item_icons({key})
        self._update_folder_count()
        if self.show_kept_only:
            self._ensure_index()
            if not self.visible_items:
                self._show_preview_message("没有保留的照片")
                self._set_status("保留 0 张照片")
                self._update_keep_mode_ui()
                self._render_thumbnails()
                return
        # Marking is a state change, not navigation: keep the current zoom.
        self._show_current(center=False, reset_zoom=False)

    def cycle_keep_mode(self) -> None:
        item = self.current_item
        if item is None or not item.paired_raw_jpeg:
            return
        self.pair_modes[item.key] = next_pair_mode(self._pair_mode(item))
        self._save_selection()
        self._update_keep_mode_ui()
        self._set_status(self._status_text(item))
        self._refresh_item_icons({item.key})

    def clear_all_kept(self) -> None:
        if not self.kept:
            QMessageBox.information(self, APP_NAME, "当前没有已保留的照片。")
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            f"确定要取消全部 {len(self.kept)} 个保留项目吗？\n\n各组的 RAW/JPG 模式不会改变。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.kept.clear()
        self._invalidate_visible()
        self._save_selection()
        if self.show_kept_only:
            self.show_kept_only = False
            self.filter_button.setChecked(False)
            self._invalidate_visible()
        self._ensure_index()
        if self.visible_items:
            self._show_current(center=True)
            self._refresh_all_icons()
            return
        self._show_preview_message(
            "打开一个照片文件夹开始选片",
        )
        self._update_keep_mode_ui()
        self._render_thumbnails()

    def reset_all_pair_modes(self) -> None:
        pair_items = [item for item in self.all_items if item.paired_raw_jpeg]
        if not pair_items:
            QMessageBox.information(self, APP_NAME, "当前文件夹没有 RAW+JPG 绑定组。")
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            f"确定要将 {len(pair_items)} 个 RAW+JPG 绑定组的模式全部重置为 RAW+JPG 吗？\n\n"
            "各组的保留/不保留状态不会改变。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.pair_modes = {item.key: "both" for item in pair_items}
        self._save_selection()
        self._update_keep_mode_ui()
        self._set_status(self._status_text(self.current_item))
        self._refresh_all_icons()

    def toggle_filter(self) -> None:
        active = self.current_item
        self.show_kept_only = self.filter_button.isChecked()
        self._invalidate_visible()
        items = self.visible_items
        if active is not None and active in items:
            self.index = items.index(active)
        else:
            self.index = 0
        if items:
            self._render_thumbnails(center=True)
            self._show_current(center=True)
        else:
            self._show_preview_message("没有保留的照片")
            self._set_status("保留 0 张照片")
            self._update_keep_mode_ui()
            self._render_thumbnails()
        self._update_folder_count()

    # --- current photo / preview -------------------------------------------

    def _show_current(self, center: bool, reset_zoom: bool = True) -> None:
        self._ensure_index()
        item = self.current_item
        if item is None:
            return
        path = item.primary
        path_id = item.primary_id

        if (
            not reset_zoom
            and path_id == self._current_path_id
            and self.stage.has_image
        ):
            # Same photo, state-only refresh (keep / mode toggle): leave the
            # zoom, pan and full-resolution texture exactly as they are.
            self._set_status(self._status_text(item))
            self._update_keep_mode_ui()
            self._sync_filmstrip_selection()
            self._ensure_jpeg_window(self.index)
            return

        self._set_status("正在载入：" + path.name)
        self._update_keep_mode_ui()
        self._sync_filmstrip_selection()
        self._ensure_jpeg_window(self.index)

        # A *different* photo invalidates any pending full-resolution request.
        if path_id != self._current_path_id:
            self._loading_full_path_id = None
            self._full_ready = None
            self._full_apply_timer.stop()

        cached = self.image_loader.try_cached(path_id)
        if cached is not None:
            self._adopt_preview(path, path_id, cached.image, cached.original_size)
            return

        self._current_path = path
        self._current_path_id = path_id
        self._preview_pil = None
        self._full_pixels = None
        self._full_ready = None
        self._full_apply_timer.stop()
        self._using_full = False
        self._loading_path_id = path_id
        self.image_loader.submit(path, path_id, full_resolution=False)

    def _adopt_preview(
        self,
        path: Path,
        path_id: str,
        image: Image.Image,
        original_size: tuple[int, int],
    ) -> None:
        # Re-showing the photo we already have sharp: keep the full-resolution
        # texture and the camera, so e.g. a filter toggle does not soften the
        # view or lose the zoom position.
        if (
            path_id == self._current_path_id
            and self._using_full
            and self._full_pixels is not None
        ):
            self._preview_pil = image
            self._loading_path_id = None
            self._show_empty_page(False)
            self._set_status(self._status_text(self.current_item))
            self._update_zoom_label()
            return

        self._preview_pil = image
        self._full_pixels = None
        self._full_ready = None
        self._full_apply_timer.stop()
        self._using_full = False
        self._loading_path_id = None
        if original_size and original_size[0] > 0 and original_size[1] > 0:
            self._original_size = original_size
        else:
            self._original_size = image.size
        self._current_path = path
        self._current_path_id = path_id
        is_full_pixels = max(self._original_size) <= PREVIEW_CACHE_LONG_EDGE
        self.stage.set_image(
            np.ascontiguousarray(np.asarray(image, dtype=np.uint8)),
            self._original_size,
            full=is_full_pixels,
        )
        self.stage.fit()
        self.stage.fade_in()
        self._show_empty_page(False)
        self._set_status(self._status_text(self.current_item))
        self._update_zoom_label()

    def _adopt_full(
        self, path_id: str, pixels: np.ndarray, original_size: tuple[int, int]
    ) -> None:
        self._full_pixels = pixels
        self._using_full = True
        self._original_size = original_size
        self._loading_full_path_id = None
        # Camera view stays put: scene coordinates are original pixels in
        # both preview and full-resolution uploads.
        self.stage.set_image(pixels, original_size, full=True)
        self._set_status(self._status_text(self.current_item))
        self._update_zoom_label()

    def _zoom_settled(self) -> bool:
        """True once the wheel/drag has been quiet long enough for heavy work.

        The full-resolution upgrade allocates and copies ~72 MB buffers on the
        decode thread. That does not hold the GIL, but the allocator churn is
        enough to cost the UI thread an occasional ~60 ms frame, so the whole
        upgrade — request included — waits for the gesture to end. Since the
        texture swap already had to wait for that moment anyway, this costs
        nothing in perceived latency.
        """

        if self.stage.camera_busy():
            return False
        return (
            self.stage.seconds_since_zoom_input()
            >= ZOOM_FULLRES_SETTLE_MS / 1000.0
        )

    def _maybe_request_full(self) -> None:
        """Start the full-resolution decode once it can actually add detail."""

        if self._loading_full_path_id or self._full_ready is not None:
            return
        if self._using_full or self._full_pixels is not None:
            return
        if not self.stage.has_image:
            return
        native = self.stage.preview_native_scale()
        if native is None or native >= 1.0:
            return  # the uploaded texture already carries full detail
        # Below the texture's own 1:1 scale the GPU is minifying, so a bigger
        # upload would add nothing while costing a ~50-110 ms texture swap.
        if self.stage.pixel_zoom() <= native * ZOOM_PREVIEW_NATIVE_MARGIN:
            return
        if not self._zoom_settled():
            return
        max_texture = self.stage.max_texture_size()
        if max_texture and max(self._original_size) > max_texture:
            return  # Photo exceeds the GPU single-texture limit; keep preview.
        path = self._current_path
        path_id = self._current_path_id
        if path is None or path_id is None:
            return
        self._loading_full_path_id = path_id
        self._set_status(f"正在载入原图以检视：{path.name}")
        # want_array: the uint8 conversion is a full-size copy (~60 ms for
        # 24 MP) and must not run on the UI thread.
        self.image_loader.submit(
            path, path_id, full_resolution=True, want_array=True
        )

    def _arm_full_apply(self) -> None:
        """Wait out the current gesture before swapping in a heavy texture."""

        self._full_apply_timer.start(ZOOM_FULLRES_SETTLE_MS)

    def _apply_ready_full(self) -> None:
        ready = self._full_ready
        if ready is None:
            return
        if not self._zoom_settled():
            # Still zooming/panning: re-arm and try again once it settles.
            self._arm_full_apply()
            return
        path_id, pixels, original_size = ready
        self._full_ready = None
        if path_id != self._current_path_id:
            return
        self._adopt_full(path_id, pixels, original_size)

    def _release_full(self) -> None:
        """Drop the full-res texture when returning to fit/preview mode."""
        self._full_apply_timer.stop()
        self._full_ready = None
        self._loading_full_path_id = None
        if self._full_pixels is None:
            return
        self._full_pixels = None
        self._using_full = False
        if self._preview_pil is not None:
            self.stage.set_image(
                np.ascontiguousarray(np.asarray(self._preview_pil, dtype=np.uint8)),
                self._original_size,
                full=False,
            )

    def _show_preview_message(self, message: str) -> None:
        self._empty_label.setText(message)
        self.stage.clear_image()
        self._show_empty_page(True)
        self.zoom_label.setText("—")

    # --- zoom ----------------------------------------------------------------

    def zoom_fit(self) -> None:
        self._release_full()
        if not self.stage.has_image:
            return
        self.stage.fit()
        self._update_zoom_label()

    def zoom_actual(self) -> None:
        if not self.stage.has_image:
            return
        # No explicit full-res request here: _tick_status runs the same check
        # and honours the settle gate, so the zoom animation is never
        # interrupted by a 72 MB upload.
        self.stage.zoom_actual()
        self._update_zoom_label()

    def toggle_zoom(self) -> None:
        if not self.stage.has_image:
            return
        zoom = self.stage.pixel_zoom()
        fit = self.stage.fit_pixel_scale()
        if abs(zoom - fit) < 0.0001 * max(1.0, fit):
            self.zoom_actual()
        else:
            self.zoom_fit()

    def zoom_step_ui(self, direction: int) -> None:
        if not self.stage.has_image:
            return
        self.stage.zoom_step(direction)

    def _update_zoom_label(self) -> None:
        if not self.stage.has_image:
            self.zoom_label.setText("—")
            return
        percent = round(self.stage.pixel_zoom() * 100)
        fit = self.stage.fit_pixel_scale()
        if abs(self.stage.pixel_zoom() - fit) < max(0.005, fit * 0.001):
            self.zoom_label.setText(f"适合 {percent}%")
        else:
            self.zoom_label.setText(f"{percent}%")

    def _on_escape(self) -> None:
        if self._export_active:
            self.export_service.cancel()
            self._set_status("正在取消导出…")

    def _tick_status(self) -> None:
        try:
            self._update_zoom_label()
            self._maybe_request_full()
            if self._full_ready is not None and not self._full_apply_timer.isActive():
                self._arm_full_apply()
        except Exception:
            # Never let a status tick die; the next one retries.
            pass

    # --- background service polling -------------------------------------------

    def _poll_services(self) -> None:
        try:
            self._handle_scan_events()
            self._handle_image_load_events()
            self._handle_preload_events()
            self._handle_export_events()
            self._handle_thumbnail_events()
        except Exception:
            # Never let polling die; the next tick retries.
            pass

    def _handle_image_load_events(self) -> None:
        event = self.image_loader.drain_latest()
        if event is None:
            return
        generation, path_id, image, original_size, error, full_flag = event
        if generation != self.image_loader.generation:
            return
        # Drop stale decodes: a cached preview hit in _show_current does not
        # bump the loader generation, so an in-flight decode for the photo we
        # just navigated away from can still arrive here and clobber state.
        if path_id != self._current_path_id and path_id != self._loading_path_id:
            if not (full_flag and path_id == self._loading_full_path_id):
                return
        path = self._current_path
        name = path.name if path is not None else path_id
        if error is not None or image is None:
            if full_flag:
                self._loading_full_path_id = None
                self._set_status(f"无法载入原图：{name}（继续使用预览）")
                return
            self._loading_path_id = None
            self._show_preview_message(f"无法显示\n{name}\n\n{error}")
            self._update_keep_mode_ui()
            return
        if full_flag:
            # Hold the heavy swap until the gesture settles; the decode itself
            # already ran in the background so there is no latency cost.
            self._full_ready = (path_id, image, original_size or (image.shape[1], image.shape[0]))
            self._loading_full_path_id = None
            self._arm_full_apply()
            return
        self._adopt_preview(path, path_id, image, original_size or image.size)

    def _handle_preload_events(self) -> None:
        try:
            while True:
                event = self.preloader.events.get_nowait()
                generation, completed, total, done = event
                if generation != self.preloader.generation:
                    continue
                if done:
                    self.preload_label.setText(f"JPG 已预载：{completed} 张")
                else:
                    self.preload_label.setText(f"正在预载 JPG：{completed} / {total}")
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
            QMessageBox.warning(
                self,
                APP_NAME,
                f"已复制 {copied} 张；{len(failures)} 张未能复制。\n\n"
                + "\n".join(failures[:3]),
            )
        else:
            QMessageBox.information(self, APP_NAME, f"已复制 {copied} 张保留照片。")
        self._set_status(self._status_text(self.current_item))

    # --- thumbnails ---------------------------------------------------------

    def _ensure_jpeg_window(self, center_index: int) -> None:
        label = self.preloader.request_window(self.visible_items, center_index)
        self.preload_label.setText(label or "")

    def _sync_filmstrip_selection(self) -> None:
        items = self.visible_items
        if not items:
            return
        row = min(max(self.index, 0), len(items) - 1)
        self._syncing_filmstrip = True
        try:
            self.filmstrip.setCurrentRow(row)
            item = self.filmstrip.item(row)
            if item is not None:
                self.filmstrip.scrollToItem(
                    item, QListWidget.ScrollHint.EnsureVisible
                )
        finally:
            self._syncing_filmstrip = False

    def _on_filmstrip_row_changed(self, row: int) -> None:
        if self._syncing_filmstrip or row < 0:
            return
        items = self.visible_items
        if row < len(items):
            self.index = row
            self._show_current(center=False)

    def _render_thumbnails(self, center: bool = False) -> None:
        items = self.visible_items
        self._syncing_filmstrip = True
        try:
            self.filmstrip.clear()
            for position, item in enumerate(items):
                entry = QListWidgetItem()
                entry.setData(Qt.ItemDataRole.UserRole, position)
                # An explicit size hint is what keeps the slot from collapsing.
                # An item with no icon yet (thumbnail still decoding) otherwise
                # reports an empty hint and paints as a 1-px sliver — the whole
                # strip looks like a row of ticks until decodes finish.
                entry.setSizeHint(QSize(THUMB_SLOT, THUMB_PIX_HEIGHT))
                pixmap = self._compose_thumb(item)
                if pixmap is not None:
                    entry.setIcon(QIcon(pixmap))
                self.filmstrip.addItem(entry)
        finally:
            self._syncing_filmstrip = False
        if not items:
            return
        row = min(max(self.index, 0), len(items) - 1)
        self.filmstrip.setCurrentRow(row)
        if center:
            item = self.filmstrip.item(row)
            if item is not None:
                self.filmstrip.scrollToItem(
                    item, QListWidget.ScrollHint.PositionAtCenter
                )

    def _compose_thumb(self, item: PhotoGroup) -> QPixmap | None:
        cache_key = (item.primary_id, item.primary_mtime_ns)
        pil = self._thumb_pil.get(cache_key)
        if pil is None:
            self.thumbnail_service.request(
                cache_key, item.primary, THUMB_WIDTH, THUMB_HEIGHT
            )
            return None

        dpr = self.devicePixelRatioF() or 1.0
        pixmap = QPixmap(int(THUMB_SLOT * dpr), int(THUMB_PIX_HEIGHT * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            x = (THUMB_SLOT - THUMB_WIDTH) / 2.0
            y = 6.0
            painter.setPen(QPen(QColor("#2A303A"), 1))
            painter.setBrush(QColor("#171A1F"))
            painter.drawRoundedRect(
                float(x - 4), float(y - 4), float(THUMB_WIDTH + 8), float(THUMB_HEIGHT + 8),
                6.0, 6.0,
            )
            painter.drawImage(
                QRectF(float(x), float(y), float(THUMB_WIDTH), float(THUMB_HEIGHT)).toRect(),
                _pil_to_qimage(pil),
            )
            kept = item.key in self.kept
            if kept:
                font = QFont("Segoe UI Symbol")
                font.setPointSize(9)
                font.setBold(True)
                painter.setFont(font)
                painter.setPen(QColor("#ffd35a"))
                painter.drawText(
                    QRectF(float(x - 4), float(y - 4), 44.0, 22.0),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    "★",
                )
            if item.paired_raw_jpeg:
                mode_text = (
                    pair_mode_label(self._pair_mode(item)) if kept else "未保留"
                )
                font = QFont("Segoe UI")
                font.setPointSize(7)
                font.setBold(True)
                painter.setFont(font)
                painter.setPen(QColor("#8bd7ff"))
                painter.drawText(
                    QRectF(
                        float(x + THUMB_WIDTH - 46), float(y - 3), 48.0, 14.0
                    ),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    mode_text,
                )
            label = item.primary.name
            if len(label) > 18:
                label = label[:16] + "…"
            painter.setFont(QFont("Segoe UI", 8))
            painter.setPen(QColor("#d9dde5"))
            painter.drawText(
                QRectF(4.0, float(y + THUMB_HEIGHT + 6), float(THUMB_SLOT - 8), 16.0),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
        finally:
            painter.end()
        return pixmap

    def _refresh_item_icons(self, keys: set[str]) -> None:
        for row in range(self.filmstrip.count()):
            entry = self.filmstrip.item(row)
            if entry is None:
                continue
            position = entry.data(Qt.ItemDataRole.UserRole)
            items = self.visible_items
            if not isinstance(position, int) or not (0 <= position < len(items)):
                continue
            item = items[position]
            if item.key in keys:
                pixmap = self._compose_thumb(item)
                if pixmap is not None:
                    entry.setIcon(QIcon(pixmap))

    def _refresh_all_icons(self) -> None:
        for row in range(self.filmstrip.count()):
            entry = self.filmstrip.item(row)
            if entry is None:
                continue
            position = entry.data(Qt.ItemDataRole.UserRole)
            items = self.visible_items
            if not isinstance(position, int) or not (0 <= position < len(items)):
                continue
            pixmap = self._compose_thumb(items[position])
            if pixmap is not None:
                entry.setIcon(QIcon(pixmap))

    def _handle_thumbnail_events(self) -> None:
        if self.thumbnail_service.events.empty():
            return
        events = self.thumbnail_service.drain()
        arrived: set[tuple] = set()
        for cache_key, image, error in events:
            if image is None:
                continue
            self._thumb_pil[cache_key] = image
            arrived.add(cache_key)
        while len(self._thumb_pil) > THUMB_CACHE_LIMIT:
            self._thumb_pil.pop(next(iter(self._thumb_pil)))
        if not arrived:
            return
        for row in range(self.filmstrip.count()):
            entry = self.filmstrip.item(row)
            if entry is None:
                continue
            position = entry.data(Qt.ItemDataRole.UserRole)
            items = self.visible_items
            if not isinstance(position, int) or not (0 <= position < len(items)):
                continue
            item = items[position]
            if (item.primary_id, item.primary_mtime_ns) in arrived:
                pixmap = self._compose_thumb(item)
                if pixmap is not None:
                    entry.setIcon(QIcon(pixmap))

    # --- export / delete ------------------------------------------------------

    def export_kept(self) -> None:
        if self._export_active:
            QMessageBox.information(self, APP_NAME, "已有导出任务在进行中。按 Esc 可取消。")
            return
        if not self.kept:
            QMessageBox.information(self, APP_NAME, "还没有保留照片。按 Space 标记后再导出。")
            return
        destination = QFileDialog.getExistingDirectory(self, "选择导出保留照片的文件夹")
        if not destination:
            return
        destination_path = Path(destination)
        kept_items = [item for item in self.all_items if item.key in self.kept]
        sources = self.export_service.plan(kept_items, self.pair_modes)
        answer = QMessageBox.question(
            self,
            APP_NAME,
            f"将导出 {len(kept_items)} 个保留项目（共 {len(sources)} 个原始文件）到：\n"
            f"{destination_path}\n\nRAW+JPG 组按当前模式导出。原照片不会被移动或修改。继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._export_active = True
        self.export_service.start(sources, destination_path)
        self._set_status(f"正在导出 0/{len(sources)}… 按 Esc 取消")

    def delete_current(self) -> None:
        item = self.current_item
        if item is None:
            return
        victims = list(item.members)
        target = "RAW+JPG 绑定组" if item.paired_raw_jpeg else item.primary.name
        preview = "\n".join(f"· {path.name}" for path in victims[:5])
        if len(victims) > 5:
            preview += f"\n· …（共 {len(victims)} 个文件）"
        answer = QMessageBox.question(
            self,
            APP_NAME,
            f"将「{target}」移入回收站？\n\n{preview}\n\n"
            "文件会进入回收站，之后仍可恢复。本组的保留状态和导出模式会一并移除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._do_delete(item, victims)

    def _do_delete(self, item: PhotoGroup, victims: list[Path]) -> None:
        failures = send_to_recycle_bin(victims)
        removed = [path for path in victims if not path.exists()]
        if not removed:
            QMessageBox.critical(
                self,
                APP_NAME,
                "删除失败，文件仍在原处：\n"
                + "\n".join(f"{path.name}：{reason}" for path, reason in failures[:3]),
            )
            return
        was_kept = item.key in self.kept
        was_mode = self.pair_modes.get(item.key)
        successor = self._forget_deleted(item, removed)
        if successor is not None and was_kept:
            self.kept.add(successor.key)
            if successor.paired_raw_jpeg and was_mode:
                self.pair_modes[successor.key] = normalize_pair_mode(was_mode)
            self._save_selection()
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
        if was_kept and successor is None:
            note += "（原为保留项，已移出保留集合）"
        elif was_kept and successor is not None:
            note += "（保留状态已转移到剩余文件）"
        self._set_status(f"{note}    {self._status_text(self.current_item)}")
        if len(removed) < len(victims):
            QMessageBox.warning(
                self,
                APP_NAME,
                f"有 {len(victims) - len(removed)} 个文件未能删除：\n"
                + "\n".join(f"{path.name}：{reason}" for path, reason in failures[:3]),
            )

    def _forget_deleted(self, item: PhotoGroup, removed: list[Path]) -> PhotoGroup | None:
        """Drop deleted members from caches and session state.

        If some members of a group survived, rebuild them as new group(s) in
        place so orphan files do not vanish from the UI. Returns the sole
        successor group when exactly one remains, else None.
        """
        self.image_loader.cancel_pending()
        self.preloader.invalidate()
        self.thumbnail_service.cancel_pending()

        removed_ids = {str(path.resolve()) for path in removed}
        cache_ids = set(removed_ids)
        cache_ids.add(item.primary_id)
        for path_id in cache_ids:
            self.jpeg_cache.pop(path_id)
            for key in [k for k in self._thumb_pil if k[0] == path_id]:
                self._thumb_pil.pop(key, None)

        remaining_paths = [
            path for path in item.members if str(path.resolve()) not in removed_ids
        ]
        self.kept.discard(item.key)
        self.pair_modes.pop(item.key, None)

        source_gone = self._current_path_id in removed_ids or (
            self._current_path is not None
            and str(self._current_path.resolve()) in removed_ids
        )
        if source_gone:
            self._current_path = None
            self._current_path_id = None
            self._preview_pil = None
            self._full_pixels = None
            self._full_ready = None
            self._full_apply_timer.stop()
            self._using_full = False
            self._loading_path_id = None
            self._loading_full_path_id = None
            self._original_size = (1, 1)
            self.stage.clear_image()

        if not remaining_paths:
            self.all_items = [c for c in self.all_items if c.key != item.key]
            self._invalidate_visible()
            self._save_selection()
            self._update_folder_count()
            self._render_thumbnails(center=True)
            return None

        rebuilt = build_photo_groups(remaining_paths)
        new_items: list[PhotoGroup] = []
        for candidate in self.all_items:
            if candidate.key == item.key:
                new_items.extend(rebuilt)
            else:
                new_items.append(candidate)
        self.all_items = new_items
        self._invalidate_visible()
        self._save_selection()
        self._update_folder_count()
        self._render_thumbnails(center=True)
        return rebuilt[0] if len(rebuilt) == 1 else None

    # --- status / shutdown ------------------------------------------------------

    def _status_text(self, item: PhotoGroup | None) -> str:
        base = (self._status_note + "  ") if self._status_note else ""
        if item is None:
            return f"{base}★ {len(self.kept)}"
        star = "★" if item.key in self.kept else "☆"
        mode = ""
        if item.paired_raw_jpeg:
            mode = f"  ·  {pair_mode_label(self._pair_mode(item))}"
        return f"{base}{star} {len(self.kept)}{mode}"

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _update_folder_count(self) -> None:
        """Refresh the right sidebar's photo counter.

        Prefers the visible (filtered) count while "只看保留" is on, since that
        is the set the filmstrip and arrow keys actually walk.
        """
        total = len(self.all_items)
        if total == 0:
            self.folder_count_label.setText("没有照片")
            return
        paired = sum(1 for item in self.all_items if item.paired_raw_jpeg)
        kept = len(self.kept)
        parts = [f"{total} 张照片"]
        if paired:
            parts.append(f"RAW+JPG 组 {paired}")
        parts.append(f"已保留 {kept}")
        if self.show_kept_only:
            parts.append(f"当前显示 {len(self.visible_items)}")
        self.folder_count_label.setText("\n".join(parts))

    def closeEvent(self, event) -> None:  # noqa: N802
        self._scan_cancel = True
        self._scan_generation += 1
        self.export_service.cancel()
        self.preloader.invalidate()
        self.image_loader.cancel_pending()
        self.thumbnail_service.shutdown()
        self.stage.shutdown()
        self.preloader.close()
        self.image_loader.shutdown()
        self.export_service.close()
        # Selection JSON under %LOCALAPPDATA% is kept on purpose.
        cleanup_on_exit()
        super().closeEvent(event)


def main() -> None:
    qt_app = QApplication.instance() or QApplication([sys.argv[0]])
    qt_app.setApplicationName(APP_NAME)
    qt_app.setStyle("Fusion")
    qt_app.setStyleSheet(DARK_QSS)
    vispy_app.use_app("pyside6")

    window = PhotoCullerWindow()
    window.show()
    # QOpenGLWidget's native context is valid only after the widget has been
    # shown; defer GPU discovery until then (prototype lesson).
    QTimer.singleShot(0, window.initialize_after_show)
    raise SystemExit(qt_app.exec())


if __name__ == "__main__":
    main()
