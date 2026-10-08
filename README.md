# Photo Culler

<p align="center">
  <img src="assets/logo.png" alt="Photo Culler logo" width="280"/>
</p>

一个面向 Windows 摄影工作流的快速选片工具。打开照片文件夹，快速浏览并标记保留/取消保留，最后把选中的原始文件复制到另一个文件夹。

[English](README.en.md)

License: [MIT](LICENSE)

## 功能特性

- **快速浏览**：`←` / `→`（到头循环）或点击缩略图栏切换照片；预览由 GPU 引擎即时呈现
- **GPU 缩放预览**：PySide6 + VisPy/OpenGL 纹理渲染，滚轮缩放以光标为锚点、带缓动动画，平移/缩放由 GPU 完成，大图拖动不掉帧
- **RAW+JPG 自动绑定**：同名 RAW（DNG/CR2/NEF/ARW 等）与 JPG 自动合并为一个选片项
- **保留标记**：`Space` 标记/取消保留，缩略图黄色星号表示已保留；支持"只看保留"筛选
- **快速删除**：`Del` 把当前组（RAW+JPG 整组）移入 Windows 回收站，可随时还原
- **只读原则**：浏览和选片从不修改、移动或重命名原始照片；导出仅复制；唯一例外是删除（进回收站，可恢复）
- **RAW 支持**：DNG 及 Canon/Nikon/Sony/Olympus/Panasonic/Fuji 等主流 RAW；优先内嵌预览；100% 检视再读全分辨率
- **低内存架构**：JPG 只缓存预览尺寸（长边 ≤2560），张数按空闲内存自适应；缩略图后台解码

## 支持格式

| 类型 | 说明 |
|---|---|
| `.jpg` / `.jpeg` | 预览尺寸滑动窗口缓存；100% 时按需读全图 |
| `.png` / `.tif` / `.tiff` | 按需解码 |
| 相机 RAW | 经 rawpy/LibRaw：DNG、CR2/CR3、NEF/NRW、ARW/SR2、ORF、RW2、RAF、PEF、3FR、MRW、ERF、DCR、KDC、MOS、IIQ 等；优先内嵌预览，100% 再全像素解码 |

仅扫描所选文件夹及其**普通子文件夹**（不递归符号链接/junction，避免环与越界）。扫描在后台线程用目录栈完成，不递归解码；结束后按相对路径排序并一次性替换照片列表。

## 安装

需要 Python 3.13（Windows）。

```bash
pip install -r requirements.txt
```

依赖：`PySide6`、`vispy`、`PyOpenGL`（GPU 预览界面）、`Pillow`、`rawpy`、`numpy`。

### 界面后端

| `PHOTOCULLER_UI` | 行为 |
|---|---|
| 未设置 / `auto` | 优先启动 PySide6 + VisPy GPU 界面；缺少 Qt 依赖时自动回退 Tkinter |
| `qt` | 强制 GPU 界面；缺依赖时直接报错并提示安装命令 |
| `tk` | 强制 Tkinter 界面（旧版 CPU 预览管线） |

Tkinter 界面完整保留，作为无 OpenGL 环境（虚拟机、远程桌面、老旧显卡）的降级通路；两个界面共用同一套解码、缓存、导出与选片记录模块。

可选加速 JPEG 预览/缩略图解码：

```bash
pip install PyTurboJPEG
```

Windows 还需本机有 libjpeg-turbo 动态库（`turbojpeg` / `jpeg62`）。未安装时自动回退 Pillow `draft()`；可用环境变量 `PHOTOCULLER_NO_TURBOJPEG=1` 强制走 Pillow。

> 未安装 `rawpy` 时程序自动降级：相机 RAW 无法预览，其余格式正常。

> 说明：GPU 界面下预览与缩放不再经过 `preview_engine`，因此 DirectML/CuPy 那套 GPU 重采样开关（`PHOTOCULLER_RESAMPLE`）仅对 Tkinter 界面生效。

### GPU 加速方案

程序会自动检测本机的显示适配器（核显 / 独显），并给出几种可选的加速方案。入口在左侧边栏的 **加速 → GPU 加速…**，也可从「更多…」菜单进入；右侧边栏「加速」分区随时显示检测到的显卡与当前方案。

检测合并三个来源，因为任何一个单独都不完整：

| 来源 | 提供什么 |
|---|---|
| 注册表 `HKLM\SYSTEM\...\Class\{4d36e968-...}` | **已安装的**全部显示适配器（含当前会话未启用的核显）、驱动版本、真实显存 |
| DXGI（`dxgi.dll` → `EnumAdapters1`） | 图形栈**当前可用**的适配器、`DXGI_ADAPTER_FLAG_SOFTWARE` 标志 |
| 实时 OpenGL 上下文 | 预览**实际在用**的渲染器 |

混合显卡笔记本上注册表与 DXGI 会有意义地不一致：注册表同时列出核显与独显，DXGI 只列出当前会话挂载的那块。只看 DXGI 会把「有没有核显」答错，所以两者都要。

可选方案：

| 方案 | 生效机制 | 何时生效 |
|---|---|---|
| **自动（推荐）** | 清除本应用的显卡偏好，交回 Windows 决定 | 下次启动 |
| **独显优先（性能最强）** | 把本应用的 Windows 显卡偏好写为「高性能」（`GpuPreference=2`） | 下次启动 |
| **核显优先（省电）** | 同上写为「省电」（`GpuPreference=1`） | 下次启动 |
| **兼容模式（纯 CPU 渲染）** | 切到 Tkinter 界面层 + 关闭 GPU 重采样，完全不走 OpenGL | 下次启动 |

机器上没有对应硬件时，该方案仍会显示但**不可选**，并标注原因（例如「未检测到独立显卡」），避免一项看起来生效却什么都没做。

写入位置为 `HKCU\Software\Microsoft\DirectX\UserGpuPreferences`（Microsoft 文档中的「按应用 GPU 偏好」），**仅当前用户、可随时撤销**——选回「自动」即删除该值。源码运行时**不会**写入，因为那时进程是共用的 `python.exe`，写进去会影响机器上所有 Python 程序；安装版中此项直接生效。

> 有两个方案经过实测后**故意不提供**：
> - **ANGLE / Direct3D 后端**：Qt 6 已从官方构建中移除 ANGLE，PySide6 wheel 里没有 `libEGL.dll` / `libGLESv2.dll`，`QT_OPENGL=angle` 只会静默地什么都不做。
> - **`QT_OPENGL=software`（Qt 内软件 OpenGL）**：Qt 自带的 `opengl32sw.dll` 是 Mesa 11.2 / GLSL **1.30**，而 VisPy 的场景着色器需要远高于此。实测在该模式下 Qt 界面**拿不到上下文**（`stage.gpu_info` 为空），预览直接坏掉——它会是陷阱而不是退路。真正的 CPU 通路是 Tkinter 界面层。

设置持久化在 `%LOCALAPPDATA%\PhotoCuller\settings.json`（与选片记录分开存放，互不影响）。

### 实时资源占用

右栏「资源」分区显示五项，每秒刷新一次：

| 显示 | 来源 |
|---|---|
| CPU | 全机 CPU 使用率（`GetSystemTimes`，不依赖性能计数器） |
| 内存 | 系统物理内存占用（已用/总量 + 百分比） |
| 本程序 | 本进程工作集（常驻内存） |
| GPU | 全机 GPU 占用率（所有引擎合计，上限 100%） |
| 显存 | 专用显存占用（已用/总量 + 百分比） |

悬停任意一行可看到本程序 CPU（占整机比例）、分适配器的 GPU 占用、本程序 GPU 与显存占用。适配器按 DXGI 的 LUID 对应，所以显示的是真实显卡名而不是十六进制标识。

### 子文件夹树（类似资源管理器的导航窗格）

「当前文件夹」下方是一棵**子文件夹树**：

- **只列出含照片的文件夹**。空文件夹不占位置；但中间层目录（自己没照片、下面有）会保留，否则深层目录就点不到了。
- 每行显示张数：`landscape  4/10` 表示「本目录 4 张、含子目录共 10 张」；两者相同时只显示一个数字。
- **点击文件夹即可进入**（以它为新的根重新扫描，树也随之更新）。点箭头只展开/折叠，不会误触发进入。
- 「↑ 上一级」回到父目录；已经在盘符根目录时该按钮为禁用状态。
- 树默认展开第一层，高度贴合内容，超过上限时内部滚动。
- 树只做浏览与导航，**不提供新建/重命名/删除文件夹**等会改动磁盘的操作。

采样在**后台线程**进行（GPU 计数器需要相隔约 1 秒采集两次才能算出比率，CPU 同理），UI 只在有新读数时更新标签。若机器没有 GPU 性能计数器（虚拟机、精简系统等），GPU 与显存显示为「—」并在下方说明原因 —— **「—」表示测不到，和「0%」不是一回事**。CPU 走的是 `GetSystemTimes`，在没有性能计数器的机器上照常可用。

## 使用

从源码运行（推荐，会自动挑一个装好依赖的解释器）：

```bat
run.bat
```

或者手动指定解释器（**必须用装好依赖的那个 Python**）：

```bash
.venv-build\Scripts\python.exe app.py     # 本仓库的 venv
```

> ⚠️ 直接 `python app.py`（例如双击 `app.py`）会用系统 Python。系统 Python 通常
> 既没有 `numpy` 也没有 `PySide6`，于是两个界面都起不来，只会弹一个启动失败对话框。
> 这不是程序坏了，是解释器选错了 —— 用上面的 `run.bat` 即可避免。

强制指定界面后端：`set PHOTOCULLER_UI=qt` / `set PHOTOCULLER_UI=tk`。

启动后程序会自动弹出文件夹选择框，也可按 `O` 重新打开。

### 快捷键

| 按键 | 功能 |
|---|---|
| `←` / `→` | 上一张 / 下一张 |
| `Space` | 保留 / 取消保留 |
| `F` | 切换 RAW / JPG 模式 |
| `Del` | 删除当前组（移入回收站） |
| `O` | 打开照片文件夹 |
| `E` | 导出保留照片 |
| `Z` | 适应窗口 / 100% 实际尺寸 |
| `1` | 100% 实际尺寸 |
| `+` / `-` | 放大 / 缩小 |
| `Ctrl+Shift+X` | 全部不保留 |
| `Ctrl+Shift+M` | 重置所有模式 |

### 鼠标操作

- **预览区滚轮**：以光标位置为锚点缩放（GPU 缓动动画）
- **预览区拖拽**：平移大图
- **缩略图栏滚轮**：横向滚动
- **点击缩略图**：跳转到该照片

## 项目结构

```
PhotoCuller-source/
├── app.py                    # 入口（按 PHOTOCULLER_UI 分发 GPU / Tk 界面）
├── config.py                 # 共享常量
├── domain.py                 # PhotoGroup / 分组 / 导出成员规划（无 GUI 依赖）
├── imaging.py                # 图片解码（JPG/PNG/TIFF/DNG）
├── winshell.py               # HiDPI 与回收站删除
├── selection_store.py        # 选片记录持久化（%LOCALAPPDATA%）
├── app_settings.py           # 用户设置持久化（%LOCALAPPDATA%\PhotoCuller\settings.json）
├── version_info.py           # 版本号 → exe 版本资源 + 安装包命名（唯一来源是 config.APP_VERSION）
├── gpu_info.py               # 显卡检测（注册表 + DXGI + 实时 GL，核显/独显分类）
├── gpu_accel.py              # 可选加速方案（环境变量 + Windows 按应用 GPU 偏好）
├── sysmon.py                 # 实时资源采样（RAM / 工作集 / GPU 占用 / 显存，后台线程）
├── jpeg_preloader.py         # JPEG 预览 LRU + 滑动窗口预载（多 worker）
├── image_loader.py           # 后台解码（预览尺寸 / 全分辨率）
├── export_service.py         # 后台导出（进度 / Esc 取消）
├── thumbnail_service.py      # 后台缩略图解码
├── workers.py                # latest-wins 单线程 worker
├── sysmem.py                 # 物理内存探测 + 自适应缓存上限
├── ram_frames.py             # 共享内存帧
├── temp_cleanup.py           # 临时文件清理
├── gpu_preview.py            # GPU 预览引擎（VisPy/OpenGL 纹理 + ZoomPlan 缩放数学）
├── qt_ui.py                  # PySide6 界面层（默认）
├── ui.py                     # Tkinter 界面层（降级通路，保留完整功能）
├── widgets.py                # Tk 自绘控件
├── preview_engine.py         # Tk 预览几何 + 双帧后台渲染（zoom 相对原图像素）
├── resample_backend.py       # Tk 重采样后端（CPU / DirectML / CUDA）
├── requirements.txt
├── run.bat                   # 从源码启动（自动挑一个装好依赖的解释器）
├── build_exe.bat             # 打包 onedir 版本（dist\PhotoCuller）
├── build_installer.bat       # 打包单文件安装器
├── bench_zoom.py             # 缩放流畅度回归基准（帧间隔 / jank / 成本拆解）
├── test_gpu_ui.py            # GPU 界面端到端冒烟测试
├── test_sysmon.py            # 资源采样测试（格式化、降级、LUID 解析、采样线程）
├── test_domain.py            # 领域逻辑测试（子文件夹树）
├── test_version.py           # 版本链路测试（含从已构建 exe 读回版本资源）
├── test_gpu_accel.py         # 显卡检测 / 加速方案测试（分类、合并、持久化、注册表往返）
├── test_entry_dispatch.py    # 入口分发 / 降级行为测试
├── test_smoke.py             # Tk 功能冒烟测试
├── test_delete.py            # 删除功能测试
└── Photo Culler-实现说明.md
```

## 技术架构

- **分层**：`domain` / services（解码、缓存、导出、回收站）与界面层分离，领域逻辑可不依赖 GUI 测试
- **UI 分发**：`app.py` 按 `PHOTOCULLER_UI` 选择 `qt_ui`（默认）或 `ui`；Tkinter 缺失或 Qt 依赖缺失时自动走可用的那一侧
- **GPU 预览**：`gpu_preview.py` 把预览图作为 OpenGL 纹理上传，场景坐标恒为**原图像素**；`ZoomPlan` 负责纯数学的缩放钳制，`SmoothPanZoomCamera` 实现光标锚定的缓动滚轮缩放。GPU 上下文在窗口首次显示后才初始化
- **缩放语义**：`pixel_zoom()` = 每个原图像素占多少屏幕像素（`1.0` 即 100%）；适应窗口不会放大小图；上限为 `ZOOM_MAX_PIXEL_SCALE`（4×）
- **100% 检视**：只有当预览纹理**真的被放大**到 1:1 以上（`pixel_zoom` 超过预览的 1:1 比例 × 1.02）才按需读入全分辨率原图并替换纹理，场景坐标不变所以视野位置保持不变；回到适应窗口释放全图。整条升级链路（请求 → 解码 → 换图）都等滚轮停下后才发起，因此换图不会打断手势
- **缩放流畅度**：24MP 照片实测缩放手势中最大帧间隔 ≤20ms、无掉帧（`bench_zoom.py` 可复测）
- **当前图解码**：后台线程池完成；JPEG 命中滑动窗口缓存时直接复用
- **JPG 缓存**：只缓存**预览尺寸**（长边 ≤2560）解码图，数量按系统总内存/空闲内存自适应（约 6–60 张）
- **导出**：后台拷贝，状态栏显示进度，导出中按 `Esc` 可取消
- **目录读取**：`os.scandir` 目录栈枚举所选文件夹及其普通子文件夹（不递归符号链接/junction）
- **缩略图**：只为可见范围生成；JPEG 用 `draft()` 降采样解码；缓存键为路径身份 + mtime（不含显示序号）
- **RAW 解码**：rawpy/LibRaw；`extract_thumb()` 优先，`postprocess(half_size=True)` 兜底；100% 用全尺寸 postprocess
- **删除**：`SHFileOperationW` + `FOF_ALLOWUNDO` 整组移入回收站，删除前二次确认；部分失败时保留剩余成员
- **选片记录**：`%LOCALAPPDATA%\PhotoCuller\selections\<hash>.json`，保存失败会在状态栏提示
- **内存探测**：`sysmem.py` 通过 `GlobalMemoryStatusEx` 读取物理内存，打开文件夹时重算缓存上限
- **过期结果丢弃**：后台结果经 `queue` + 定时轮询回主线程，并用 generation / path_id 双重校验丢弃过期帧

详见 [Photo Culler-实现说明.md](Photo%20Culler-%E5%AE%9E%E7%8E%B0%E8%AF%B4%E6%98%8E.md)。

## 版本号

**`config.py` 里的 `APP_VERSION` 是唯一的版本来源。** 改这一个字符串，以下全部自动跟随：

| 跟随项 | 位置 |
|---|---|
| exe 的 Windows 版本资源 | 两个 exe 都会盖章，右键「属性 → 详细信息」可见 |
| 安装包文件名 | `dist\Photo-Culler-Setup<版本>.exe`，**不用手动改名** |
| 窗口标题 | `Photo Culler <版本> — <文件夹>` |
| 右侧边栏底部的版本页脚 | `v<版本>`，点击打开「关于」 |
| 「更多… → 关于 Photo Culler…」 | 版本、版本资源、渲染器、安装包名称（并复制到剪贴板） |
| `app.py --self-test` 首行 | 报告版本，便于排查"你装的是哪个版本" |
| 安装程序窗口 | 标题与说明里都带版本 |

`version_info.py` 负责把 `APP_VERSION` 转成 PyInstaller 需要的版本资源文本（用 PyInstaller 自己的 `VSVersionInfo` 序列化，而不是手写结构），并派生安装包名。两个 `.spec` 在构建时**现场生成**该文件（写到 `build/`），所以不依赖任何提交进仓库的中间产物，也不可能出现"源码是 1.1.0、安装包却是旧版本"。

`test_version.py` 会把这条链子钉住：常量 → 版本资源字段 → 生成文件能被 PyInstaller 反序列化 → 每个消费方都引用常量（而不是硬编码）→ `installer.iss` 的 `MyAppVersion` 与常量一致 → **并从已构建的 exe 里把版本资源读回来比对**，dist 过期会直接报错。

> 升级流程：改 `config.APP_VERSION` → 依次跑 `build_exe.bat`、`build_installer.bat` → `python test_version.py` 复核产物。
> `installer.iss` 是**可选的手动路径**（`ISCC.exe installer.iss`），自动化构建走的是 `Installer.spec`；若要用它，记得让 `MyAppVersion` 与常量保持一致（测试会检查）。

## 打包 / 安装

```bat
build_exe.bat          :: PyInstaller onedir → dist\PhotoCuller\Photo Culler.exe
build_installer.bat    :: 生成一键安装程序 dist\Photo-Culler-Setup<版本>.exe
```

安装包会把程序装到 `%LOCALAPPDATA%\Programs\PhotoCuller`，并可创建桌面/开始菜单快捷方式。需要 Inno Setup 时也可用 `installer.iss` 自行编译。

> ⚠️ `dist\` 里残留的旧安装包（例如 `Photo-Culler-Setup1.0.1.exe`）**不含后续修复**，双击它会装出旧版本。构建完请认准带当前版本号的那个文件。

## 测试

```bash
python app.py --self-test        # 运行时自检：首行报版本 + 真实建 Tk 根窗 + GPU 依赖 + 实跑显卡检测与资源采样
python test_repo_hygiene.py      # 仓库卫生：无残留冲突标记、索引无未解决条目、所有 .py 可解析
python test_domain.py            # 领域逻辑：子文件夹树的计数/排序/中间层保留/退化输入（无需界面）
python test_version.py           # 版本链路：常量→版本资源→规格文件→已构建 exe 实际盖章，并实跑 --self-test 验证 bundle 可运行
python test_sysmon.py            # 资源采样：格式化、缺失降级为「—」、LUID 解析、采样线程启停
python test_gpu_ui.py            # GPU 界面端到端（扫描/导航/GPU 缩放/全分辨率/筛选/删除/导出/布局/加速菜单）
python test_gpu_accel.py         # 显卡检测与加速方案（分类规则、多源合并、设置往返、注册表可撤销）
python test_entry_dispatch.py    # 入口分发与降级提示
python test_smoke.py             # Tk 界面功能冒烟测试（预览/导航/缩放/保留/筛选）
python test_delete.py            # 删除功能测试（回收站调用/单张删除/整组删除）
```

`test_gpu_ui.py` 用 `WA_DontShowOnScreen` 创建隐形窗口，因此会真实创建 OpenGL 上下文；删除步骤会真实调用回收站。

> 打包脚本 `build_exe.bat` / `build_installer.bat` 和 `run.bat` **必须是 CRLF 行尾**：它们含 `for` / `if` 嵌套块，LF-only 会被 cmd 解析错位。修改后请复核行尾，并且**不要在构建运行期间编辑这几个脚本**（cmd 边读边执行，会导致 PyInstaller 被跑两遍）。
