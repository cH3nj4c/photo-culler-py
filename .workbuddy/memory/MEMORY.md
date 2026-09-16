# Photo Culler 项目长期笔记

## 项目性质
Windows 桌面选片工具（Python 3.13 + PySide6/VisPy 默认界面 + Pillow + rawpy）。
核心原则：**源文件只读**。唯一会移动源文件的操作是 `Del`（经 `SHFileOperationW` + `FOF_ALLOWUNDO` 进回收站，可恢复）。
打包：PyInstaller onedir（`PhotoCuller.spec`）→ 再打成单文件安装器（`Installer.spec` / `installer_app.py`）。

## 双界面层（2026-09-15 起）
- **默认**：`qt_ui.py`（PySide6）+ `gpu_preview.py`（VisPy/OpenGL GPU 预览引擎）。
- **降级**：`ui.py`（Tkinter，CPU 预览管线，功能完整保留，**不再改动它就是最好的改动**）。
- `app.py` 按 `PHOTOCULLER_UI=auto|qt|tk` 分发（`auto` 优先 Qt，ImportError 则回退 Tk）。
- 两个界面层共用 `domain` 与全部服务层，选片语义一致。
- GPU 引擎要点：场景坐标恒为**原图像素**，预览/全分辨率靠 `STTransform` 映射同一坐标系（换分辨率视野不跳）；`pixel_zoom()` = 屏显像素/原图像素（1.0 = 100%）；上限 `config.ZOOM_MAX_PIXEL_SCALE = 4.0`；GPU 上下文必须延迟到窗口首次显示后初始化（`initialize_after_show()`）；换图后要置 `_need_interpolation_update = True`。
- **全分辨率按需加载（2026-09-15 流畅度专项后定型）**：触发条件 = `stage.pixel_zoom() > preview_native_scale() × ZOOM_PREVIEW_NATIVE_MARGIN(1.02)`，**不是** `fit × 1.08`。整条升级链路（请求 + 解码 + 72MB 换图）都在 `_zoom_settled()` 之后才发起 —— 要求相机不忙 **且** `seconds_since_zoom_input() >= ZOOM_FULLRES_SETTLE_MS/1000`；只看 `camera.busy()` 不够，缩放被上限夹住时相机读起来是空闲的而滚轮还在转。换图因此落在滚轮停下的空闲尾巴里。
- `ZoomPlan` 边界是**实时矩形宽度** `(max_width, min_width)`，由 `plan()` 从 `fit_width` / `pixel_scale_limit` / `device_width_provider` 动态推导，视口尺寸变化时 `_on_canvas_resize()` 重推并保持当前缩放。旧版锁存倍率会让上限漂到 4.797。

## 分层架构（关键约定）
- `app.py`：薄入口，先 `configure_bundled_tk_runtime()`；`ui` 的导入包在 `try/except ImportError` 里（无 tkinter 的环境也能 `import app`）。
- `domain.py`：纯逻辑，**不许依赖 GUI**（可在无界面环境测试）。`PhotoGroup` + `build_photo_groups` 负责 RAW+JPG 绑定。
- 服务层：`imaging`/`jpeg_fast`（解码）、`jpeg_preloader`（JPG 预览 LRU + 滑动窗口预载）、`image_loader`（后台解码）、`preview_engine` + `resample_backend`（**仅 Tk 界面用**）、`thumbnail_service`（缩略图）、`export_service`（后台复制）、`selection_store`（JSON 持久化）、`winshell`（HiDPI + 回收站）、`sysmem`、`workers`（`LatestOnlyWorker`）、`ram_frames`、`temp_cleanup`。
- `ui.py`（~2160 行）：Tk 层。`qt_ui.py`（~1270 行）：Qt 层。两者主线程都只绘制与处理事件。

## 两个正交状态（易混淆，务必区分）
- `kept: set[key]` —— 是否保留，`Space` 切换。
- `pair_modes: dict[key, both|raw|jpg]` —— RAW+JPG 组导出什么，`F` 循环切换。**互不影响**。

## 关键机制
- 后台→UI 全靠 `queue` + 定时 `_poll_services` 轮询 + `generation` 丢弃过期结果。
- **Qt 层额外的过期校验**：`_handle_image_load_events` 必须同时校验 `path_id == _current_path_id or _loading_path_id`（若命中缓存则 `_show_current` 不会 bump generation，只靠 generation 不够，否则上一张的在途解码会覆写当前图）。
- 缩放相对**原图像素**；Tk 层靠 `downsample` 映射坐标，Qt 层靠 `STTransform`。
- Tk 层交互帧（BILINEAR）与质量帧（Lanczos，停手 150ms）双帧；两层的全分辨率都是放大超 `fit + ZOOM_FULLRES_MARGIN`（8%）才按需读。
- JPG 缓存只存**预览尺寸**（长边 ≤2560），张数由 `sysmem.recommend_jpeg_cache_limit()` 按空闲内存算（6–60）。
- 缩略图只渲染可见范围；缓存键 = `(primary_id, primary_mtime_ns)`，**不含显示序号**。
- `qt_ui` 的 `_show_current` **不重建缩略图栏**（性能考虑），所以凡是"换了一批可见项"的路径（扫描完成、切筛选、清空保留、删除）都要显式调 `_render_thumbnails()`。
- 选片记录：`%LOCALAPPDATA%\PhotoCuller\selections\<sha256前20位>.json`。

## 已知问题 / 待确认
- `domain.filter_visible_items(..., show_kept_only=True)` 返回的是**未保留**项，与界面标签「只看保留」及空态文案「没有保留的照片」语义相反。`test_smoke.py` 第 77–78 行和 `test_gpu_ui.py` 第 10 步都把这个（疑似反了的）行为断言下来，改动需同步改两处测试。
- `Photo Culler-实现说明.md` 曾经过时（"只扫第一层""RAW 仅 DNG""单 worker 预载""单文件 exe"），2026-09-15 已修正这几处并在开头加了"以 README.md 为准"的提示；正文其余部分仍偏 Tk 视角，大改前先对照代码。

## 测试
- `python bench_zoom.py` —— **缩放流畅度回归基准**（帧间隔中位/p95/max、jank 比、有效 FPS、GC 归因、全分辨率开销拆解、平移）。改缩放路径前后都该跑它对比。
- `python test_gpu_ui.py` —— GPU 界面端到端（13 步，含真实 OpenGL 上下文与真实回收站删除）。
- `python test_entry_dispatch.py` —— 入口分发与降级提示。
- `python test_smoke.py` / `python test_delete.py` / `python app.py --self-test` —— Tk 侧。
- 注意：WorkBuddy 的隔离 venv（managed 3.13.12）**没有 tkinter**，跑不了 Tk 版测试；系统 Python 3.14 有 tkinter 但缺 numpy/rawpy。GPU 测试用隔离 venv。
- 该环境 bash shim 的 PATH 损坏（`ls`/`grep`/`tail`/`head` 都 `command not found`），只有 bash 内建和绝对路径 exe 可用；`rm` 也是坏的（shim 报 `safe-delete-common.sh` 找不到），**删文件用 Python 或 PowerShell**；列目录/搜内容用 Glob/Grep 工具。
- 量帧技巧：hook `canvas.events.draw` 记录时间戳（只有绘制会被计时，空闲段会显示成一个几百 ms 的"间隔"，要单独归类为 idle gap 而不是掉帧）。基线参考：24MP + RTX 4050 下，缩放中位 ~15ms（60Hz vsync 锁帧）、max ≤20ms、jank 0；平移 ~144fps。
- 探针陷阱：直接设 `camera.rect` 驱动相机**不会**触发 `mark_input()`，于是 `seconds_since_zoom_input()` 永远很大、全分辨率 settle 闸门永远开着 —— 这样测的是「没有闸门」的行为，不能用来判断真实手势。

