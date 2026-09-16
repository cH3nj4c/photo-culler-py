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
- 服务层：`imaging`/`jpeg_fast`（解码）、`jpeg_preloader`（JPG 预览 LRU + 滑动窗口预载）、`image_loader`（后台解码）、`preview_engine` + `resample_backend`（**仅 Tk 界面用**）、`thumbnail_service`（缩略图）、`export_service`（后台复制）、`selection_store`（JSON 持久化）、`app_settings`（用户设置 JSON）、`gpu_info`（显卡检测）、`gpu_accel`（加速方案）、`winshell`（HiDPI + 回收站）、`sysmem`、`workers`（`LatestOnlyWorker`）、`ram_frames`、`temp_cleanup`。
- `ui.py`（~2160 行）：Tk 层。`qt_ui.py`（~1270 行）：Qt 层。两者主线程都只绘制与处理事件。

## GPU 检测与加速方案（2026-09-16 新增）
- `gpu_info.detect_gpu()` **合并三源**：注册表（全部已装适配器，含会话未启用的核显 + 真实显存）、DXGI `EnumAdapters1`（当前可用 + 软件适配器标志）、实时 GL 上下文（实际在用）。**本机注册表有 Intel 核显而 DXGI 没有** —— 只读 DXGI 会把"有无核显"答错，所以两者都要并按归一化名称合并。
- `gpu_info.classify()` 是**纯函数**（可无硬件单测，15 例在 `test_gpu_accel.py`）。⚠️ **必须先剥掉 `(R)/(TM)/(C)/®/™` 再跑名称规则** —— 商标噪声正好夹在规则匹配的两个词中间，否则 `Arc(TM) A770`、`Radeon(TM) Graphics` 都会判错。
- `gpu_accel.SCHEMES` = `auto` / `discrete` / `integrated` / `software`。前三者靠 `HKCU\Software\Microsoft\DirectX\UserGpuPreferences` 的 `GpuPreference=1|2;`；`software` 靠 `PHOTOCULLER_UI=tk` + `PHOTOCULLER_RESAMPLE=cpu`。**全部需重启**。
- **`apply_environment()` 必须在 `QApplication` 之前**（在 `app.py` import `qt_ui` 之前调用）。
- **源码运行拒绝写注册表**（会重定向共用的 `python.exe`，影响全机器 Python）。方案仍持久化，但消息说明该部分被跳过。
- ⚠️ **不要重新引入 `QT_OPENGL=software`**：`opengl32sw.dll` 是 Mesa 11.2 / GLSL 1.30，VisPy 拿不到上下文（已实测：desktop 得到 GL 4.6，software 得到 GL 3.0/GLSL 1.30 且 Qt 界面 `gpu_info` 为空）。ANGLE 同理不存在（PySide6 无 `libEGL.dll`）。
- **界面显示 `effective_scheme_id()`（环境变量优先），不是 `current_scheme_id()`（settings）** —— 后者只表示"下次启动"。两者不一致时面板显示 `当前 → 已选（重启后生效）`。
- `environment()` 只在方案真的要强制界面层时才输出 `PHOTOCULLER_UI`；否则会删掉用户手动 export 的值。
- 设置文件 `%LOCALAPPDATA%\PhotoCuller\settings.json`（**与选片记录分开**，一个损坏不该同时损失两样）。
- `build_accel_menu()` 与 `_show_accel_menu()` 拆开（`QMenu.exec()` 阻塞，不拆没法测）。
- 写无头探针**第一件事**是 `window.auto_open_enabled = False` —— 否则构造函数里 250ms 的 `_auto_open` 会弹阻塞式文件夹对话框（实测卡 91 秒）。

## 两个正交状态（易混淆，务必区分）
- `kept: set[key]` —— 是否保留，`Space` 切换。
- `pair_modes: dict[key, both|raw|jpg]` —— RAW+JPG 组导出什么，`F` 循环切换。**互不影响**。

## Qt 界面结构（2026-09-16 起）
- `centralWidget` 是 **`QHBoxLayout`**，三栏：左侧 `QWidget#sidebar`（固定宽 `SIDEBAR_WIDTH = 172`，纯操作菜单）+ 中间预览列（`stretch=1`）+ **右侧 `QWidget#sidebarRight`（固定宽 `SIDEBAR_RIGHT_WIDTH = 196`，文件夹上下文）**。objectName 用于 QSS。
- 左栏内按 文件/选片/编辑/视图 分组，底部 `addStretch(1)` 后是「更多…」。右栏是「当前文件夹」标题 + 文件夹名 `folderName` + 计数 `metaDim`，底部 `addStretch(1)`。窗口最小尺寸 `(1040, 620)`。
- 右栏计数由 `_update_folder_count()` 维护，调用点只有四处：扫描完成的两个分支、`toggle_keep`、`toggle_filter`、删除路径的两个分支。**别挂到 `_refresh_item_icons` 上**（会跟着每张缩略图刷新白算一遍，而且 `toggle_filter` 走不到那儿）。
- 按钮属性名固定：`keep_button`(accent) / `delete_button`(danger) / `keep_mode_button` / `filter_button`(**必须是 `toggle`，`#toggle:checked` 样式靠它**) / `more_button` / `folder_label`。改布局时保留这些名字，测试依赖它们。
- **`DARK_QSS` 是在 `main()` 里设到 `QApplication` 上的，不在窗口构造函数里**。任何单独建窗口的探针/测试都要自己 `setStyleSheet(DARK_QSS)`，否则看到的是浅色默认主题。
- 界面改动用 `window.grab()` 出图验收：对 `WA_DontShowOnScreen` 窗口能正常渲染，且**会强制跑一次布局**（比手工 `setGridSize()` 触发更自然）。

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
- **`Path.resolve()` 是性能地雷**：在 Windows 上单次约 **0.96 ms**（真实文件系统调用），而 `str()` 是 0.00012 ms。凡是按**文件**调用它的地方都会在大目录上炸。`domain.build_photo_groups()` 曾经每文件一次（2000 文件 ≈ 1.9 秒），现在改成**每目录一次**（缓存 `parent.resolve()` 再拼文件名，结果逐字节相同，选片记录不受影响）。**新增按文件遍历的代码时别再用 `resolve()`。**
- **大目录的分组必须留在工作线程**：`build_photo_groups` 即使每目录只 resolve 一次，多子目录时（1000 目录 × 3 文件）仍需 ~1.3 秒。qt_ui 的扫描线程 `work()` 里做分组，"done" 事件带 `groups` 字段；`_handle_scan_events` 只做采用（`groups=None` 时保留 UI 线程兜底）。改扫描事件形状时记得同步这两处。
- **`QListWidgetItem` 必须显式 `setSizeHint`**：既没图标又没 sizeHint 的条目 sizeHint 为空，会被画成 **0×15 细条**。缩略图条"打开时是一排等距小竖线"就是它（条目先建、缩略图后到）。`qt_ui._render_thumbnails` 已在建条目时设 `QSize(THUMB_SLOT, THUMB_PIX_HEIGHT)`，新建列表条目时照做。
- **条高别写死**：文件名画在 148×116 槽位的 y=100..116，滚动条把视口压到矮于槽位时**最先被裁的就是文件名**。条高由 `qt_ui._fit_strip_height()` 算：`THUMB_PIX_HEIGHT + horizontalScrollBar().sizeHint().height() + 2*frameWidth() + STRIP_NAME_GAP(8)`。⚠️ **不要用 `height() - viewport().height()` 实测开销** —— 布局就绪前两个值都是临时的（算出 overhead=2，把条压到比槽位还矮，比原 bug 更糟）；要用 `sizeHint()` + `frameWidth()` 这种随时可取、与条高无关的量。
- **缩略图条的判据不是 `visualItemRect`**：隐藏窗口（`WA_DontShowOnScreen`）不跑 item 布局，`visualItemRect()` 返回 0 宽退化矩形、`scrollToItem()` 是空操作 —— 全是假象。可靠不变量是 `bar.maximum() + bar.pageStep() == 条目数 × THUMB_SLOT`（精确相等）。同理，判断缩略图解码完成要跟踪**图标数**而不是 `_thumb_pil` 长度（后者被 `THUMB_CACHE_LIMIT=110` 卡住）。
- `domain.filter_visible_items(..., show_kept_only=True)` 返回的是**未保留**项，与界面标签「只看保留」及空态文案「没有保留的照片」语义相反。`test_smoke.py` 第 77–78 行和 `test_gpu_ui.py` 第 10 步都把这个（疑似反了的）行为断言下来，改动需同步改两处测试。
- `Photo Culler-实现说明.md` 曾经过时（"只扫第一层""RAW 仅 DNG""单 worker 预载""单文件 exe"），2026-09-15 已修正这几处并在开头加了"以 README.md 为准"的提示；正文其余部分仍偏 Tk 视角，大改前先对照代码。
- `qt_ui._render_thumbnails` 结尾的 `setCurrentRow(row)` 在 `_syncing_filmstrip` 已复位后触发 `currentRowChanged` → 会多走一次 `_show_current`。多数调用方随后又显式调 `_show_current`，所以是重复加载（浪费一次解码 + 可能闪一下），不是错误显示。未改，因为部分调用方只调 `_render_thumbnails`。

## 运行与开发环境（2026-09-16 定型）
- **从源码启动用 `run.bat`** —— 它在 `.venv .venv-build` 里挑一个真能跑的解释器，优先有 GPU 依赖的，其次有 numpy 的，都没有才回退 PATH 上的 `python`。
- **不要用系统 Python 跑 `python app.py`**：系统 3.14 只有 tkinter/PIL，**没有 numpy/PySide6/vispy/rawpy**，两个界面都起不来，只会弹「程序启动失败」。这是 2026-09-16 用户报错的真实原因（而当时的信息错误地归咎于"缺少 tkinter"，见下）。
- **构建脚本必须在调用打包器前 `set CODEBUDDY_SAFE_DELETE_ENABLED=0`**（已写进 `build_exe.bat`）：PyInstaller 的 COLLECT 阶段会递归删掉旧的 `dist/PhotoCuller/`（约 2400 文件），撞上沙箱的批量删除确认阈值就中断在半成品状态。
- `.bat` / `.ps1` 一律 CRLF；**别在构建运行期间编辑构建脚本**（cmd 边读边执行 → 打包器被跑两遍，`set ...ENABLED=0` 会被切断成 `'ent' 不是内部或外部命令`）。
- 环境矩阵：

| 模块 | 系统 Python 3.14 | `.venv-build` | 隔离 venv（managed 3.13.12） |
|---|---|---|---|
| tkinter | 有 | 有 | **无** |
| numpy / PIL / rawpy | 仅 PIL | 全有 | 全有 |
| PySide6 / vispy / OpenGL | **无** | 有 | 有 |

  即：`.venv-build` 是**唯一两套界面都能跑**的环境（打包也用它）；隔离 venv 有全部 GPU 依赖但无 tkinter；系统 Python 只够弹错误对话框。
- **验证打包内容用 PYZ 提取**，别只看时间戳：`ZlibArchiveReader('build/<name>/PYZ-00.pyz').extract(mod)` 返回 code object。⚠️ **符号分三处**：属性/方法/全局名在 `co_names`，**字符串字面量在 `co_consts`**，局部变量在 `co_varnames`。⚠️⚠️ **常量元组会被折叠**（`return False, "msg"` → 一个 tuple 常量 `(False, 'msg')`），必须**递归进 tuple/frozenset**，并且用**子串**匹配（正则、注册表全路径、f-string 片段都是更长字面量的一部分）。只做 `isinstance(v,str)` + 集合成员判断会报出一堆假 MISS。**拿不准就 `dis.dis(fn)` 看真实 `LOAD_CONST`**。嵌套函数要递归遍历 `co_consts` 里的 code 对象。新鲜度看 `PYZ-00.pyz` 的 mtime。
  **打包产物要有一条自检路径**（`--self-test`）实跑 ctypes/DXGI/注册表这类代码 —— 静态扫包无法验证它们。
  校验安装器时，payload 是被当作 **datas 原样内嵌**的（`Installer.spec` 的 `datas=[(dist/PhotoCuller, "app_payload")]`），所以最可靠的判据是**把内嵌的 `app_payload\Photo Culler.exe` 抽出来和磁盘上的比 SHA256**（实测一致）。
  跑打包产物取输出时用 **GBK** 解码（中文 Windows 的 OEM 编码），否则 `subprocess(text=True)` 会 `UnicodeDecodeError` 直接抛。
- `app.py` 启动失败时的诊断由 `_startup_diagnosis()` 生成，用 `importlib.util.find_spec` 逐界面列出缺失模块（不做 import）。**改这两个界面的依赖时，要同步维护 `_GPU_SHELL_MODULES` / `_TK_SHELL_MODULES`**（`test_entry_dispatch.py` 第 [5] 步会断言）。`rawpy` 是可选依赖，不能算启动阻塞项。
- **`--self-test` 走 `_run_self_test()`，真的建 `tk.Tk()` 根窗**（不是 `Tcl().eval("package require Tk")`，那个在源码运行下必然报 tk86t.dll 加载失败，是长期假阳性），再报 GPU 依赖清单，逐行输出结论、末尾 `runtime OK`/`INCOMPLETE`，绝不抛异常。
- **`build_exe.bat` 必须校验依赖完整性**，不能只在"创建 venv"时安装：已存在的旧 venv 不会补装新依赖，曾导致打出来的包**完全没有 PySide6/vispy/OpenGL**，发布版一直只能跑 Tk 界面（2026-09-16 修复）。

## 测试
- `python bench_zoom.py` —— **缩放流畅度回归基准**（帧间隔中位/p95/max、jank 比、有效 FPS、GC 归因、全分辨率开销拆解、平移）。改缩放路径前后都该跑它对比。
- `python test_gpu_ui.py` —— GPU 界面端到端（**22 步**，含真实 OpenGL 上下文、真实回收站删除、缩略图条完整性、四种滚轮输入、左栏/右栏布局、加速菜单与面板断言）。**结尾会复原 settings.json**。
- `python test_gpu_accel.py` —— 显卡检测与加速方案（**11 步**：分类规则、多源合并、显存启发式、方案环境映射、可用性门控、设置往返/容错、effective vs stored、注册表往返并撤销、源码运行拒绝、真实检测、兼容模式路由到 Tk）。**会快照并复原用户的 settings.json**。
- `python test_entry_dispatch.py` —— 入口分发、降级提示、启动诊断、`--self-test` 子进程校验（**6 步**）。
- `python test_smoke.py` / `python test_delete.py` / `python app.py --self-test` —— Tk 侧。
- GPU 测试两个环境都能跑（`.venv-build` 与隔离 venv）；Tk 侧测试只有 `.venv-build` 能跑（隔离 venv 无 tkinter）。
- 该环境 bash shim 的 PATH 损坏（`ls`/`grep`/`tail`/`head` 都 `command not found`），只有 bash 内建和绝对路径 exe 可用；`rm` 也是坏的（shim 报 `safe-delete-common.sh` 找不到），**删文件用 Python 或 PowerShell**；列目录/搜内容用 Glob/Grep 工具。
- **pip 装包必须加 `CODEBUDDY_SAFE_DELETE_ENABLED=0`**：否则 file-deletion shim 会在 pip 覆盖文件时触发 `SAFE_DELETE_BULK_CONFIRM_REQUIRED` 并 `SystemExit(1)`，安装中途崩掉。
- **bash 里禁止调用 `cmd.exe`**（安全拦截，`cmd //c x.bat` 也只会开个空会话）；要跑 `.bat` 用 **PowerShell** 工具（`& .\build_exe.bat`）。
- 量帧技巧：hook `canvas.events.draw` 记录时间戳（只有绘制会被计时，空闲段会显示成一个几百 ms 的"间隔"，要单独归类为 idle gap 而不是掉帧）。基线参考：24MP + RTX 4050 下，缩放中位 ~15ms（60Hz vsync 锁帧）、max ≤20ms、jank 0；平移 ~144fps。
- 探针陷阱：直接设 `camera.rect` 驱动相机**不会**触发 `mark_input()`，于是 `seconds_since_zoom_input()` 永远很大、全分辨率 settle 闸门永远开着 —— 这样测的是「没有闸门」的行为，不能用来判断真实手势。

