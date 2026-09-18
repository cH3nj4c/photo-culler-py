# Photo Culler 代码 Wiki

> 面向开发者/维护者的代码地图：整体架构、模块职责、关键类与函数、依赖关系与运行方式。
> 本文基于仓库当前源码（`APP_VERSION = 1.1.0`）整理；配套文档还有 [README.md](README.md)（使用/功能）与 [Photo Culler-实现说明.md](Photo%20Culler-%E5%AE%9E%E7%8E%B0%E8%AF%B4%E6%98%8E.md)（Tk 时代的实现细节，部分已被 GPU 版本取代）。

---

## 1. 项目概览

**Photo Culler** 是一个面向 **Windows 摄影工作流**的快速选片桌面工具（Python 3.13）。核心工作流：

1. 打开照片文件夹（`O`）
2. 快速浏览并标记保留 / 取消保留（`Space`）
3. RAW+JPG 同名文件自动合并为同一「选片项」（`F` 切换导出模式）
4. 导出时**只复制**保留的原始文件（`E`）
5. `Del` 把整组（RAW+JPG）移入 **Windows 回收站**（可恢复）

**核心原则：源文件只读**。浏览、选片、导出从不修改/移动/重命名原图，唯一写操作是显式删除（进回收站）。

技术栈：

| 领域 | 技术 |
|---|---|
| GPU 预览界面（默认） | PySide6 + VisPy/OpenGL + PyOpenGL |
| 降级界面（回退） | Tkinter / ttk（CPU 预览管线） |
| 图像解码 | Pillow（JPEG/PNG/TIFF）、rawpy/LibRaw（RAW）、可选 PyTurboJPEG |
| 数值 | numpy |
| 打包 | PyInstaller（onedir + 单文件安装器） |

---

## 2. 整体架构

### 2.1 分层结构

```
┌─────────────────────────────────────────────────────────────┐
│ 入口层    app.py（界面分发 + 自检 + 启动诊断）                │
├─────────────────────────────────────────────────────────────┤
│ 界面层    qt_ui.py（PySide6+GPU，默认）   ui.py（Tk，回退）  │
│           widgets.py（Tk 自绘控件）      bench_zoom.py（基准）│
├─────────────────────────────────────────────────────────────┤
│ 预览引擎  gpu_preview.py（GPU 纹理/缩放）                    │
│           preview_engine.py（Tk 预览几何/后台帧）            │
│           resample_backend.py（CPU/DirectML/CUDA 重采样）    │
├─────────────────────────────────────────────────────────────┤
│ 服务层（后台线程，queue+generation 回传）                    │
│   image_loader / jpeg_preloader / thumbnail_service          │
│   export_service / workers / sysmon                         │
├─────────────────────────────────────────────────────────────┤
│ 解码层    imaging.py / jpeg_fast.py                          │
├─────────────────────────────────────────────────────────────┤
│ 领域层    domain.py（PhotoGroup/扫描/分组/选片纯逻辑）        │
├─────────────────────────────────────────────────────────────┤
│ 基础设施  config / winshell / selection_store / app_settings │
│           sysmem / ram_frames / temp_cleanup / version_info  │
│           gpu_info / gpu_accel / app_icon                    │
└─────────────────────────────────────────────────────────────┘
```

分层原则：`domain` 与全部服务层**不依赖 GUI**（可无窗口测试）；两个界面层共享同一套解码、缓存、导出与选片记录模块，保证两个界面的选片语义完全一致。

### 2.2 启动分发（app.py）

```
main()
 ├─ --self-test → _run_self_test()：版本/Tk/GPU 依赖/显卡检测/资源采样自检
 ├─ PHOTOCULLER_UI=tk → run_tk_ui()（ui.PhotoCuller）
 ├─ 默认 auto → 尝试 qt_ui.main()；ImportError 时回退 run_tk_ui() 并在 stderr 提示
 └─ PHOTOCULLER_UI=qt → 强制 qt_ui.main()，失败则报错退出
```

关键点：

- `configure_bundled_tk_runtime()`（[winshell.py](file:///workspace/winshell.py)）在冻结（PyInstaller）环境下先设置 `TCL_LIBRARY`/`TK_LIBRARY`，Tk 才能找到打包的 Tcl/Tk 运行库。
- `gpu_accel.apply_environment()` 必须在导入 PySide6/vispy **之前**执行（Qt 初始化平台插件时会读 `QT_OPENGL`）。
- 两个界面各自的缺失依赖被分别记录（`_tk_import_error` / `_qt_import_error`），启动失败时给出**按界面分别**的安装建议（防止把 numpy 缺失误报为 tkinter 缺失）。

### 2.3 线程模型与通信协议

所有后台服务遵循同一模式：

- **UI 线程**只负责绘制与事件处理；后台工作在线程池/守护线程完成。
- 结果经 **`queue.Queue` + 定时轮询**回主线程（Qt：16ms QTimer；Tk：`PREVIEW_POLL_MS` 轮询）。
- **过期结果丢弃**：每个服务维护自增 `generation`（代数）；事件携带 `(generation, ...)`，主线程 `drain_latest()` 只保留与当前 generation 匹配的最新事件。切换图片/文件夹时 `cancel_pending()` / `invalidate()` 使旧任务失效。
- 共享状态（如 `JpegCache` LRU、`PreviewEngine` 的金字塔层级缓存）用 `threading.Lock` 保护。

```
主线程 ──submit(generation, task)──▶ 后台线程池
   ▲                                   │
   └──────────queue 事件──(generation,...)──┘
```

### 2.4 主要数据流

```
打开文件夹
  → domain.scan_photo_tree()（后台目录栈扫描，返回 (path, mtime_ns)，按相对路径排序）
  → build_photo_groups()（按 stem 分组，RAW+JPG 合并为 PhotoGroup）
  → selection_store.load_selection()（读回上次 kept/pair_modes）
  → JpegPreloader.request_window()（当前索引前后滑动窗口预载预览图）
  → ImageLoader（当前图预览解码 → GPU 纹理上传 / Tk 双帧渲染）
  → 缩略图栏 ThumbnailService（仅可见范围，draft() 降采样）
交互
  → Space 标记保留（kept 集合 + save_selection 落盘）
  → Z/滚轮 缩放（GPU：SmoothPanZoomCamera 缓动；Tk：compute_geometry + ResampleService）
  → 超过阈值 → ImageLoader full_resolution → 替换纹理
  → E 导出（ExportService 后台 copy2，Esc 取消）
  → Del 删除（winshell.send_to_recycle_bin，SHFileOperationW FOF_ALLOWUNDO）
```

---

## 3. 模块职责总览

| 模块 | 行数 | 职责 |
|---|---|---|
| [app.py](file:///workspace/app.py) | 349 | 入口：界面分发（auto/qt/tk）、`--self-test` 自检、启动失败诊断与提示 |
| [config.py](file:///workspace/config.py) | 93 | 全局常量：格式扩展名集合、缓存/缩略图/缩放参数、`APP_VERSION`（版本唯一来源） |
| [domain.py](file:///workspace/domain.py) | 245 | 领域纯逻辑：`PhotoGroup` 分组、目录扫描（目录栈、不跟随符号链接）、选片模式、筛选 |
| [imaging.py](file:///workspace/imaging.py) | 212 | 图片解码：JPEG/PNG/TIFF（Pillow+TurboJPEG）、RAW（rawpy）、EXIF 方向、降采样、缩略图适配 |
| [jpeg_fast.py](file:///workspace/jpeg_fast.py) | 143 | libjpeg-turbo 快速 JPEG 解码（可选），失败自动回退 Pillow |
| [winshell.py](file:///workspace/winshell.py) | 132 | Windows 专用：打包 Tk 运行库定位、HiDPI 感知、回收站删除（SHFileOperationW） |
| [selection_store.py](file:///workspace/selection_store.py) | 67 | 选片记录持久化：`%LOCALAPPDATA%\PhotoCuller\selections\<folder-hash>.json` |
| [app_settings.py](file:///workspace/app_settings.py) | 82 | 用户设置持久化：`settings.json`（与选片记录分离），原子写入（tmp+replace） |
| [version_info.py](file:///workspace/version_info.py) | 171 | 从 `APP_VERSION` 派生版本资源（PyInstaller VSVersionInfo）与安装包名 |
| [gpu_info.py](file:///workspace/gpu_info.py) | 474 | 显卡检测：注册表 + DXGI（ctypes COM）+ 实时 GL 三源合并，核显/独显分类 |
| [gpu_accel.py](file:///workspace/gpu_accel.py) | 377 | 加速方案：auto/discrete/integrated/software，写 Windows 按应用 GPU 偏好 |
| [sysmon.py](file:///workspace/sysmon.py) | 550 | 实时资源采样（后台线程）：内存/工作集/GPU 占用（PDH）/显存 |
| [sysmem.py](file:///workspace/sysmem.py) | 101 | 物理内存探测（GlobalMemoryStatusEx）+ JPG 预览缓存上限自适应 |
| [jpeg_preloader.py](file:///workspace/jpeg_preloader.py) | 197 | JPEG 预览 LRU 缓存 + 滑动窗口并行预载（多 worker） |
| [image_loader.py](file:///workspace/image_loader.py) | 116 | 当前图后台解码（预览尺寸/全分辨率），可输出 uint8 数组直传 GPU |
| [export_service.py](file:///workspace/export_service.py) | 78 | 后台导出（copy2 保留元数据、进度事件、Esc 取消、失败收集） |
| [thumbnail_service.py](file:///workspace/thumbnail_service.py) | 114 | 后台缩略图解码（按需、防重、居中留黑边补边） |
| [workers.py](file:///workspace/workers.py) | 47 | `LatestOnlyWorker`：latest-wins 单线程 worker（新任务顶替未开始的任务） |
| [ram_frames.py](file:///workspace/ram_frames.py) | 67 | `ActivePreviewRam`：当前图的预览/全图/代理帧内存槽位 |
| [temp_cleanup.py](file:///workspace/temp_cleanup.py) | 98 | 退出时清理系统临时目录中本应用的残留（绝不动 selections） |
| [gpu_preview.py](file:///workspace/gpu_preview.py) | 548 | GPU 预览引擎：VisPy `SceneCanvas`、`ZoomPlan` 缩放数学、`SmoothPanZoomCamera` 缓动相机、`PhotoStage` |
| [qt_ui.py](file:///workspace/qt_ui.py) | 2002 | PySide6 界面层（默认）：左侧操作栏 + 中央 GPU 预览 + 右侧信息栏 + 缩略图条 |
| [ui.py](file:///workspace/ui.py) | 2164 | Tkinter 界面层（回退）：自绘工具栏、Canvas 预览、CPU 双帧渲染 |
| [widgets.py](file:///workspace/widgets.py) | 267 | Tk 自绘圆角按钮/开关（RoundedButton/RoundedToggle） |
| [preview_engine.py](file:///workspace/preview_engine.py) | 307 | Tk 预览几何（compute_geometry）+ 后台裁剪/重采样（金字塔层级缓存） |
| [resample_backend.py](file:///workspace/resample_backend.py) | 331 | 重采样后端：CPU（Pillow）/ DirectML（torch）/ CUDA（cupy），失败粘滞回退 |
| [app_icon.py](file:///workspace/app_icon.py) | 64 | 窗口/任务栏图标与应用 ID（AppUserModelID） |
| [bench_zoom.py](file:///workspace/bench_zoom.py) | 502 | GPU 缩放流畅度回归基准（帧间隔/jank/GC/全分辨率上传成本） |
| [installer_app.py](file:///workspace/installer_app.py) | 166 | 内置安装器（Tk）：复制 onedir 载荷 + PowerShell 创建快捷方式 |
| PhotoCuller.spec / Installer.spec / installer.iss | | PyInstaller 打包规格（版本资源构建时现场生成） |
| run.bat / build_exe.bat / build_installer.bat | | 源码运行 / onedir 打包 / 单文件安装器打包 |

---

## 4. 关键类与函数说明

### 4.1 领域层 [domain.py](file:///workspace/domain.py)（无 GUI 依赖）

| 符号 | 说明 |
|---|---|
| `PhotoGroup`（frozen dataclass） | 一个选片项：`key`（跨会话标识）、`primary`（代表文件，RAW+JPG 对取 JPG）、`primary_id`（唯一路径标识）、`members`（成员路径）、`primary_mtime_ns`（缩略图缓存键用）。`paired_raw_jpeg` 属性判断是否为 RAW+JPG 对 |
| `build_photo_groups(paths, mtime_ns_by_path)` | 按 `stem.casefold()` 分组，同 stem 的 RAW+JPG 合并为一组；性能要点：每个父目录只 `resolve()` 一次（逐文件 resolve 在 3000 文件文件夹上会卡 UI ~2s） |
| `scan_photo_tree(root, on_progress, should_cancel)` | 显式目录栈（非递归）遍历，只进普通目录、**不跟随符号链接/junction**；返回 `(path, mtime_ns)` 列表，按相对路径排序；失败计数继续遍历 |
| `scan_photo_entries / scan_photo_paths` | 便捷封装 |
| `selected_members(item, mode)` | 按 pair mode（both/raw/jpg）决定导出哪些成员 |
| `unique_destination(folder, filename)` | 冲突时生成 `名称 (1).ext` 等不冲突目标路径 |
| `filter_visible_items(all_items, kept, show_kept_only)` | 「只看保留」筛选（保留 = key 不在 kept 中） |
| `normalize_pair_mode / next_pair_mode / pair_mode_label` | 模式规范化/循环切换/中文标签 |

### 4.2 解码层 [imaging.py](file:///workspace/imaging.py) / [jpeg_fast.py](file:///workspace/jpeg_fast.py)

| 符号 | 说明 |
|---|---|
| `read_raster_image(path, max_size)` | 普通图解码：JPEG 先走 TurboJPEG 缩比解码；回退 Pillow `draft("RGB", max_size)` 跳过大部分像素；`ImageOps.exif_transpose` 校正方向；返回 `(image, 方向校正后的原始尺寸)` |
| `read_raw_image(path, thumbnail_size, full_resolution)` | rawpy 解码：100% 检视时 `postprocess(half_size=False)` 全像素；否则优先 `extract_thumb()` 内嵌预览（JPEG 或位图），无预览时 `postprocess(half_size=True)` 兜底。`original_size` 用传感器 `iwidth/iheight`（不是内嵌预览尺寸），保证 100% 时才真正全像素 |
| `decode_preview_photo(path, long_edge)` | 预览缓存解码：一次打开内完成降采样（长边 ≤ `PREVIEW_CACHE_LONG_EDGE`=2560） |
| `decode_photo(path, thumbnail, thumb_size, full_resolution)` | 通用解码入口（缩略图/全分辨率/预览） |
| `fit_long_edge / fit_for_display / downsample_to_edge` | 缩放工具：默认 BILINEAR（便宜且够预览用） |
| `thumbnail_decode_size` | 缩略图解码目标 = 显示尺寸 × `THUMBNAIL_DECODE_SCALE`（保证清晰） |
| `decode_jpeg_turbo / decode_jpeg_fast`（jpeg_fast） | libjpeg-turbo 缩比解码（分母 1/2/4/8），手动应用 EXIF 方向；`PHOTOCULLER_NO_TURBOJPEG=1` 或库缺失时返回 None 交给 Pillow |

### 4.3 后台服务层

#### [workers.py](file:///workspace/workers.py) — `LatestOnlyWorker`
单守护线程执行 job；`submit(generation, fn, ...)` 新 job 顶替未开始的任务；异常被吞掉（job 自身负责发终止事件）。用于导出（`ExportService`）。

#### [jpeg_preloader.py](file:///workspace/jpeg_preloader.py)
| 符号 | 说明 |
|---|---|
| `PreviewCacheEntry(NamedTuple)` | `(image, original_size)` |
| `JpegCache` | 线程安全 `OrderedDict` LRU，键 = `primary_id`；`set_limit`/`retune_from_system_memory`（按空闲内存 6~60 张） |
| `JpegPreloader` | 多 worker（3）预载当前索引前 12 / 后 24 的 JPEG 预览；导航**不取消**仍在途的有用解码，只有换文件夹/关闭才 `invalidate()`；窗口外两倍距离的缓存条目会被逐出；进度以 `(generation, done, total, idle)` 事件上报 |

#### [image_loader.py](file:///workspace/image_loader.py) — `ImageLoader`
当前图解码（预览尺寸或 `full_resolution=True` 全分辨率）。`want_array=True` 时直接产出 `np.ascontiguousarray` uint8（避免 UI 线程做全尺寸 PIL→numpy 拷贝，24MP 约 60ms）；JPEG 预览结果写回 `JpegCache`；`drain_latest()` 只返回最新 generation 的事件。

#### [thumbnail_service.py](file:///workspace/thumbnail_service.py) — `ThumbnailService`
只为可见范围生成缩略图；`cache_key` 为 `(路径身份, mtime)`；不足目标尺寸时用背景色 `#171A1F` 居中补边（与缩略图边框同色，无接缝）；`_pending` 集合防重，UI `drain()` 后才移除。

#### [export_service.py](file:///workspace/export_service.py) — `ExportService`
`plan(kept_items, pair_modes)` 按模式展开待导出的成员路径；`start()` 走 `LatestOnlyWorker` 逐个 `shutil.copy2`（保留元数据）；`cancel()` 请求停止；事件 `(generation, copied, total, failures, done, cancelled)`；任何异常都保证发出终止事件，UI 不会卡在“导出中”。

#### [preview_engine.py](file:///workspace/preview_engine.py)（仅 Tk 壳使用）
| 符号 | 说明 |
|---|---|
| `PreviewGeometry` | 一帧预览的源裁剪框（图像像素）、目标尺寸、画布原点、金字塔层级 downsample 因子 |
| `compute_geometry(...)` | **Tk 缩放核心**：`zoom_scale`/`fit_scale` 相对原图像素；先算原图坐标可见区（带 overscan），再映射回（可能降采样的）预览图像素；交互期按 `interactive_downsample_factor` 选金字塔层级；返回 `(geometry, new_fit, new_zoom, new_pan_x, new_pan_y)` |
| `PreviewEngine` | 后台裁剪/重采样线程池 + 金字塔层级缓存（`_levels`）；经 `ResampleService`（DirectML→CUDA→CPU）出帧；`build_frame_sync` 供内存中图片的首帧 |

#### [resample_backend.py](file:///workspace/resample_backend.py)（仅 Tk 壳使用）
| 符号 | 说明 |
|---|---|
| `ResampleRequest / ResampleResult / BackendInfo` | 请求/结果/后端描述 dataclass |
| `CpuResampleBackend` | Pillow crop+resize；`pil_resample_for` 决定 LANCZOS（轻度缩放）/ BILINEAR（重度缩小或交互） |
| `CupyResampleBackend` | CUDA 双线性（可选，`cupy`） |
| `DmlResampleBackend` | DirectML（可选，`torch` + `torch_directml`，优先级最高） |
| `ResampleService` | 按 `PHOTOCULLER_RESAMPLE`（auto/cpu/gpu）选后端；GPU 连续失败 3 次**粘滞回退 CPU**；任何情况都有 CPU 兜底 |

### 4.4 内存与资源

| 模块 | 说明 |
|---|---|
| [ram_frames.py](file:///workspace/ram_frames.py) `ActivePreviewRam` | 当前图的预览/全图/最近绘制代理帧内存槽；切图/关窗时 `clear()` 立即释放引用（与滑动窗口 LRU 分工：LRU 管“下一张”，这里管“这一张”） |
| [sysmem.py](file:///workspace/sysmem.py) | `get_memory_info()`（GlobalMemoryStatusEx，非 Windows 回退保守值）；`recommend_jpeg_cache_limit()`：可用内存 35% 与总内存 12% 取小，预留 180MB 全分辨率工作图，换算为 6~60 个槽位 |
| [temp_cleanup.py](file:///workspace/temp_cleanup.py) | `cleanup_on_exit()` 清系统临时目录中 `photoculler`/`pc_*` 前缀且年龄 >120s 的残留；`cleanup_app_data_scratch()` 清 `%LOCALAPPDATA%\PhotoCuller` 下除 selections 外的垃圾 |

### 4.5 GPU 检测 / 加速 / 预览

#### [gpu_info.py](file:///workspace/gpu_info.py)
| 符号 | 说明 |
|---|---|
| `classify(name, vendor_id, is_software)` | 纯函数分类（核显/独显/软件/未知）：先剥 `(R)/(TM)` 等商标噪声；NVIDIA 一律独显；Intel 默认核显、带型号 `Arc A7xx/B5xx` 判独显；AMD `Radeon RX/Pro/VII` 独显、`Radeon Graphics/Vega N/780M` 核显 |
| `Adapter` | 单个适配器：名称、种类、厂商、显存、驱动、来源（registry/dxgi）、`runtime_visible`、`luid`（PDH 关联用） |
| `GpuReport` | 汇总报告：`has_discrete/has_integrated/is_hybrid`、`adapter_names_by_luid()`、`headline()` |
| `_query_registry()` | 遍历 `HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-...}`：列出**已安装**的全部适配器（含未启用的核显）、驱动版本、真实显存（优先 64 位 qwMemorySize） |
| `_query_dxgi()` | 手工绑定 DXGI COM vtable（`EnumAdapters1`/`GetDesc1`）：当前可用适配器 + 软件适配器标志 + LUID |
| `detect_gpu()` | 合并三源（按归一化名称去重合并字段）、显存启发式兜底（≥1GB 判独显）、软件适配器排最后；**永不抛异常** |
| `apply_vram_heuristic` | 名称规则判不出的才按显存兜底 |

#### [gpu_accel.py](file:///workspace/gpu_accel.py)
| 符号 | 说明 |
|---|---|
| `Scheme`（frozen dataclass） | 方案描述：id/label/detail、要写的 `gpu_preference`、要强制的 `ui_shell`、`resample_mode`、所需硬件（`needs_discrete/needs_integrated`） |
| `SCHEMES` | 四个方案：`auto`（清偏好）、`discrete`（独显，写 `GpuPreference=2`）、`integrated`（核显，写 `=1`）、`software`（强制 Tk 壳 + `PHOTOCULLER_RESAMPLE=cpu`） |
| `scheme_availability` | 机器没有对应硬件时方案不可选（给出原因，避免“看似生效实则没做”） |
| `apply_environment()` | **必须在 QApplication 之前调用**：把方案的 env 写进当前进程（`PHOTOCULLER_ACCEL`/`PHOTOCULLER_UI`/`PHOTOCULLER_RESAMPLE`）；手动导出的 `PHOTOCULLER_ACCEL` 优先 |
| `target_executable()` | 仅冻结版有独立 exe 才允许写注册表偏好；源码运行拒绝（会作用到共用 python.exe） |
| `write_gpu_preference / read_gpu_preference` | 读写 `HKCU\Software\Microsoft\DirectX\UserGpuPreferences`（`GpuPreference=N;`），可随时撤销（写 None 删除值） |
| `apply_scheme` | 持久化选择 + 应用环境 + 写注册表；部分失败也会如实说明（“渲染方式将在下次启动时生效”） |

> 有意**不提供**的两个方案（模块 docstring 有实测记录）：ANGLE/D3D（PySide6 wheel 无 ANGLE DLL）与 Qt 内 `QT_OPENGL=software`（Mesa 11.2/GLSL 1.30 达不到 VisPy 要求，实测拿不到上下文）。真正的 CPU 通路是 Tk 壳。

#### [gpu_preview.py](file:///workspace/gpu_preview.py)
| 符号 | 说明 |
|---|---|
| `query_gpu_info(canvas)` | 从 VisPy 实际 GL 上下文读 vendor/renderer/GL 版本/`GL_MAX_TEXTURE_SIZE` |
| `ZoomPlan` | 纯缩放数学：`clamp_target_width` 上下界（最大=fit、最小=设备宽/4×）、`wheel_target_width` 把滚轮增量换算为目标宽度（指数/2^steps） |
| `SmoothPanZoomCamera(scene.PanZoomCamera)` | 光标锚定的缓动滚轮缩放：每次输入只记 `_pending_log_factor`，120Hz `app.Timer` 指数衰减逼近目标（`easing_seconds=0.065`）；`busy()`/`seconds_since_input()` 供“滚轮停下才加载全图”判定；边界**实时**从 stage 取（窗口缩放/换显示器不会留下过期上限） |
| `PhotoStage(QWidget)` | 中央视口：照片作为一张 GPU 纹理（`scene.visuals.Image`，cubic 插值）；`set_image` 用 `STTransform` 把降采样预览映射进**原图像素**坐标系（预览↔全图切换视野不跳）；`preview_native_scale()` 返回当前纹理 1:1 的 pixel_zoom，超过 `×1.02` 才值得加载全图；`initialize_gpu_info()` 必须等 `show()` 之后调用；换图后强制 `_need_interpolation_update=True`（VisPy 旧版会沿用上一张的插值形状）；`fade_in()` 提供换图淡入 |
| `_set_natural_fit` | fit 但**不放大**小图超过 100%；`_on_canvas_resize` 重算 fit 并重钳制 zoom |

缩放语义（全程序统一）：`pixel_zoom()` = 每个原图像素占的屏幕像素，`1.0`=100%；上限 `ZOOM_MAX_PIXEL_SCALE`=4×；fit 不放大。

### 4.6 系统监控 [sysmon.py](file:///workspace/sysmon.py)

| 符号 | 说明 |
|---|---|
| `Snapshot` | 一次只读采样：内存、本程序工作集、全机/本程序 GPU%、显存、按 LUID 分适配器的 GPU%（tooltip 用）、`notes`（不可用时说明原因） |
| `PdhGpuCounters` | PDH 计数器封装：`\GPU Engine(*)\Utilization Percentage`、`\GPU Process Memory(*)\Dedicated Usage`、`\GPU Adapter Memory(*)\Dedicated Usage`；从实例名解析 pid 与 LUID（高/低 32 位重组为 64 位）；**不可用时 `available=False` + 原因**（“—”≠“0%”） |
| `SystemMonitor` | 后台守护线程每秒采样（PDH 需相隔 ~1s 两次采集才能算比率）；`latest()` 线程安全，UI 定时轮询 |

### 4.7 持久化与版本

#### [selection_store.py](file:///workspace/selection_store.py)
- 路径：`%LOCALAPPDATA%\PhotoCuller\selections\sha256(文件夹resolve)[:20].json`
- 载荷：`{folder, kept[], pair_modes{}, updated}`；`load_selection` 读取失败返回空（不阻塞启动）；`save_selection` 失败返回错误字符串（状态栏提示）。

#### [app_settings.py](file:///workspace/app_settings.py)
- `settings.json` 与选片记录**分离**（损坏一个不影响另一个）；`DEFAULTS = {"accel_scheme": "auto"}`；未知键保留（新版本可能写过）；写盘用 **tmp+`os.replace`** 原子替换。

#### [version_info.py](file:///workspace/version_info.py)
- **唯一版本来源是 `config.APP_VERSION`**；`parse_version()` 只取前导数字核（`"1.1.0-beta2"`→`(1,1,0,0)`）；`build_version_info()` 用 PyInstaller 自己的 `VSVersionInfo` 序列化版本资源；`ensure_version_file()` 被两个 `.spec` 在构建时现场调用；派生 `INSTALLER_BASENAME = "Photo-Culler-Setup<version>"`。

### 4.8 界面层要点

#### [qt_ui.py](file:///workspace/qt_ui.py) — `PhotoCullerWindow(QMainWindow)`
- 布局：`QHBoxLayout(左栏 172px | 预览列 stretch | 右栏 196px)` + 状态栏 + 底部 `FilmStrip`（横向滚轮缩略图条）；`DARK_QSS` 暗色主题。
- 关键方法分组：
  - 初始化/构建：`__init__`、`_build_ui`、`initialize_after_show`（show 后初始化 GPU 信息、自动打开、启动采样）、`_fit_strip_height`
  - 加速菜单：`build_accel_menu`、`_select_accel_scheme`、`_show_gpu_details`、`_update_accel_panel`/`_update_resource_panel`
  - 扫描/打开：`open_folder`、`_open_folder_path`（后台 `scan_photo_tree` 线程 + generation 事件）、`_reset_session_caches`、`_handle_scan_events`
  - 选片：`toggle_keep`、`cycle_keep_mode`、`clear_all_kept`、`reset_all_pair_modes`、`toggle_filter`、`_save_selection`
  - 预览/全分辨率：`_show_current`、`_adopt_preview`、`_adopt_full`、`_maybe_request_full`（等滚轮停下 + `pixel_zoom > native×1.02` 才请求）、`_release_full`（回 fit 释放）
  - 缩放：`zoom_fit`、`zoom_actual`、`toggle_zoom`、`zoom_step_ui`
  - 事件泵：`_poll_services`（16ms）→ `_handle_image_load_events`/`_handle_preload_events`/`_handle_thumbnail_events`/`_handle_export_events`
  - 缩略图：`_render_thumbnails`（虚拟化只画可见区）、`_compose_thumb`（黄星=保留）
  - 删除：`delete_current`、`_do_delete`（二次确认→`send_to_recycle_bin`→`_forget_deleted` 重建分组）
  - 导出：`export_kept`（选目标目录→`ExportService.start`）
  - 收尾：`closeEvent`（先停采样线程，再逐项关闭服务，保留选片 JSON）
- `freeze_startup_garbage()`：`gc.freeze()` 把会话级大对象图（VisPy/Qt + 大数组）冻结进永久代，消除缩放手势中途 ~70ms 的 GC 卡顿（实测最差帧 72ms→8ms）。

#### [ui.py](file:///workspace/ui.py) — `PhotoCuller(tk.Tk)`
功能与 Qt 壳对等的 Tk 实现：顶部自绘工具栏、Canvas 预览（双帧：交互帧低清 + 质量帧）、`compute_geometry`+`PreviewEngine` CPU 管线、`_start_slide_animation` 换图滑动动画、代理帧缩放（`_draw_proxy_zoom`）、缩略图虚拟化等。

#### [widgets.py](file:///workspace/widgets.py)
`_RoundedWidget(tk.Canvas)` 自绘圆角按钮（hover/press 状态、按需重绘、`ttk.Button` 兼容 API）；`RoundedButton`（accent/danger 变体）、`RoundedToggle`（绑定 BooleanVar）。

---

## 5. 依赖关系

### 5.1 pip 依赖（[requirements.txt](file:///workspace/requirements.txt)）

| 包 | 用途 | 必选 |
|---|---|---|
| Pillow>=10.0 | JPEG/PNG/TIFF 解码、缩放、EXIF | ✅ |
| rawpy>=0.20 | RAW（DNG/CR2/NEF/ARW/...）解码 | ✅（缺失自动降级：RAW 无法预览） |
| numpy>=1.26 | 像素数组、GPU 上传 | ✅ |
| PySide6>=6.6 | GPU 界面（默认壳） | 可选（缺则回退 Tk） |
| vispy>=0.14 | GPU 场景画布 | 可选 |
| PyOpenGL>=3.1 | GL 绑定 | 可选 |
| PyTurboJPEG>=1.7 | libjpeg-turbo 快速 JPEG（需本机 `turbojpeg`/`jpeg62` DLL） | 可选 |
| torch + torch-directml | Tk 壳 DirectML 重采样 | 可选 |
| cupy-cuda12x | Tk 壳 CUDA 重采样 | 可选 |
| PyInstaller | 打包（构建期依赖） | 构建用 |

> GPU 壳的缩放/插值全部在 GPU 上，`PHOTOCULLER_RESAMPLE`（DirectML/CuPy）只对 Tk 壳生效。

### 5.2 模块依赖图（核心边）

```
app.py → winshell, gpu_accel(env 前置), domain, ui?, qt_ui?
domain → config
imaging → config, jpeg_fast, PIL, rawpy?
jpeg_fast → PIL, turbojpeg?
sysmem → ctypes
jpeg_preloader → config, domain, imaging, sysmem
image_loader → jpeg_preloader, imaging, numpy
thumbnail_service → imaging, PIL
export_service → domain, workers
workers → threading
preview_engine → config, resample_backend, PIL
resample_backend → PIL, cupy?, torch_directml?
gpu_preview → config, numpy, PySide6, vispy, OpenGL
qt_ui → config, domain, export_service, gpu_preview, gpu_accel, gpu_info, sysmon,
        image_loader, jpeg_preloader, selection_store, sysmem, temp_cleanup,
        thumbnail_service, version_info, winshell, app_icon
ui → config, domain, export_service, image_loader, jpeg_preloader, preview_engine,
     ram_frames, selection_store, sysmem, temp_cleanup, thumbnail_service,
     widgets, winshell, app_icon
gpu_accel → app_settings, gpu_info
gpu_info → ctypes, winreg
sysmon → sysmem
version_info → config, PyInstaller(构建时)
bench_zoom → qt_ui, config, imaging, PySide6, vispy
installer_app → config, version_info, tkinter
```

### 5.3 环境变量

| 变量 | 取值 | 作用 |
|---|---|---|
| `PHOTOCULLER_UI` | `auto`(默认)/`qt`/`tk` | 选择界面壳 |
| `PHOTOCULLER_ACCEL` | `auto`/`discrete`/`integrated`/`software` | 本次进程实际生效的加速方案（外部导出优先） |
| `PHOTOCULLER_RESAMPLE` | `auto`/`cpu`/`gpu`/`off`/`software`/`cupy`/`cuda` | Tk 壳重采样后端选择 |
| `PHOTOCULLER_NO_TURBOJPEG` | `1` | 强制走 Pillow（不用 TurboJPEG） |
| `QT_OPENGL` | （不使用） | Qt 内部 OpenGL 后端；ANGLE/software 已被实测否决 |
| `LOCALAPPDATA` | | 数据目录根（settings/selections） |

### 5.4 外部数据与系统资源

| 位置 | 内容 | 写入方 |
|---|---|---|
| `%LOCALAPPDATA%\PhotoCuller\selections\<hash>.json` | 每文件夹选片记录（kept、pair_modes） | `selection_store` |
| `%LOCALAPPDATA%\PhotoCuller\settings.json` | 用户设置（accel_scheme） | `app_settings` |
| `HKCU\Software\Microsoft\DirectX\UserGpuPreferences` | 按应用 GPU 偏好（`GpuPreference=N;`，仅安装版写） | `gpu_accel` |
| 系统临时目录 | 运行期临时文件（退出时按前缀清理） | `temp_cleanup` |
| Windows 回收站 | 删除的照片（FOF_ALLOWUNDO） | `winshell` |

---

## 6. 项目运行方式

> 目标平台为 **Windows**（回收站、注册表、PDH、DXGI 均为 Windows 特性）；非 Windows 上删除/GPU 计数/显卡偏好等会自动降级。

### 6.1 从源码运行

```bat
run.bat            :: 自动在 .venv / .venv-build 中挑一个装好依赖的解释器
```

或手动指定（**必须用装好依赖的 Python**）：

```bash
.venv-build\Scripts\python.exe app.py
```

- 强制界面：`set PHOTOCULLER_UI=qt`（GPU）或 `set PHOTOCULLER_UI=tk`（Tk）。
- ⚠️ 直接用系统 Python 运行 `app.py` 会因缺 numpy/PySide6 启动失败并弹窗——不是程序坏了，是解释器选错了。
- 启动后自动弹文件夹选择框（也可 `O` 重新打开）。

### 6.2 运行时自检

```bash
python app.py --self-test
```

首行报版本；真实创建隐藏 Tk 根窗；检查 GPU 壳依赖；实跑显卡检测（注册表+DXGI）与资源采样（PDH 只 open 不等待）；退出码 0 = runtime OK。

### 6.3 打包 / 安装

```bat
build_exe.bat          :: 创建/补全 .venv-build → PyInstaller onedir → dist\PhotoCuller\Photo Culler.exe
build_installer.bat    :: 基于 dist\PhotoCuller 打单文件安装器 → dist\Photo-Culler-Setup<版本>.exe
```

- 版本资源与安装包名在构建时由 `version_info.ensure_version_file()` 现场生成（`build/` 下），版本永远跟随 `config.APP_VERSION`。
- 构建脚本必须保持 **CRLF** 行尾（含 `for`/`if` 嵌套块）；构建运行期间不要编辑这几个 .bat（cmd 边读边执行）。
- 备选：Inno Setup 手编 `installer.iss`（`MyAppVersion` 需与常量一致，测试会检查）。
- 安装包安装到 `%LOCALAPPDATA%\Programs\PhotoCuller` 并创建快捷方式（Tk 安装器 `installer_app.py` 内置）。

### 6.4 升级版本

改 `config.py` 的 `APP_VERSION`（唯一来源）→ `build_exe.bat` → `build_installer.bat` → `python test_version.py` 复核产物。

### 6.5 测试

| 命令 | 覆盖 |
|---|---|
| `python test_repo_hygiene.py` | 仓库卫生：无冲突残留、git 索引无未解决项、所有 .py 可解析 |
| `python test_version.py` | 版本链路：常量→版本资源→规格文件→从已构建 exe 读回版本资源比对 |
| `python test_sysmon.py` | 资源采样：格式化、缺失降级为「—」、LUID 解析、采样线程启停 |
| `python test_gpu_ui.py` | GPU 界面端到端（隐形窗口真实建 GL 上下文；扫描/导航/缩放/全分辨率/筛选/删除/导出/布局/加速菜单） |
| `python test_gpu_accel.py` | 显卡检测与加速方案（分类、多源合并、设置往返、注册表可撤销） |
| `python test_entry_dispatch.py` | 入口分发与降级提示 |
| `python test_smoke.py` | Tk 界面功能冒烟（预览/导航/缩放/保留/筛选） |
| `python test_delete.py` | 删除功能（回收站调用/单张/整组） |
| `python bench_zoom.py` | GPU 缩放流畅度基准（帧间隔中位数/p95/最大、jank、GC、24MP 全图上传成本拆解） |

---

## 7. 关键设计约定（改代码前必读）

1. **源文件只读**：除删除外任何路径不得写源图；导出只 `copy2`。
2. **版本单一来源**：永远只改 `config.APP_VERSION`，其它消费方（版本资源/安装包名/标题/关于）都派生自它。
3. **双壳同语义**：`qt_ui` 与 `ui` 共用 domain/服务层；改选片逻辑只改领域层即可。
4. **过期结果丢弃**：所有后台回传事件带 generation，主线程只认最新；新增异步任务必须沿用该协议。
5. **缩放语义**：`pixel_zoom`（原图像素↔屏幕像素）、fit 不放大、上限 4×；GPU 壳场景坐标恒为原图像素（换图不跳视野）。
6. **100% 检视**：仅在预览纹理被放大到 1:1 以上（native×1.02）且滚轮停稳后才加载全分辨率；回 fit 释放。
7. **GPU 上下文延迟初始化**：GL 查询/上下文必须在 `show()` 之后；`apply_environment` 必须在 Qt 导入之前。
8. **回收站删除**：`SHFileOperationW` + `FOF_ALLOWUNDO`，删除前二次确认，部分失败保留剩余成员。
9. **内存自适应**：JPG 预览缓存 6~60 张按空闲内存自适应；当前图内存槽与 LRU 分工明确。
10. **持久化容错**：settings/selections 读失败一律回退默认，写失败返回字符串提示，绝不让文件损坏阻塞启动。
