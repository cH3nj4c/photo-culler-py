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

## 使用

```bash
python app.py
```

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
├── test_gpu_ui.py            # GPU 界面端到端冒烟测试
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
- **100% 检视**：缩放超过"适应窗口 + 8%"时按需读入全分辨率原图并替换纹理，场景坐标不变所以视野位置保持不变；回到适应窗口释放全图
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

## 打包 / 安装

```bat
build_exe.bat          :: PyInstaller onedir → dist\PhotoCuller\Photo Culler.exe
build_installer.bat    :: 生成一键安装程序 dist\Photo-Culler-Setup.exe
```

`Photo-Culler-Setup.exe` 会把程序装到 `%LOCALAPPDATA%\Programs\PhotoCuller`，并可创建桌面/开始菜单快捷方式。需要 Inno Setup 时也可用 `installer.iss` 自行编译。

## 测试

```bash
python app.py --self-test        # 运行时自检
python test_gpu_ui.py            # GPU 界面端到端（扫描/导航/GPU 缩放/全分辨率/筛选/删除/导出）
python test_entry_dispatch.py    # 入口分发与降级提示
python test_smoke.py             # Tk 界面功能冒烟测试（预览/导航/缩放/保留/筛选）
python test_delete.py            # 删除功能测试（回收站调用/单张删除/整组删除）
```

`test_gpu_ui.py` 用 `WA_DontShowOnScreen` 创建隐形窗口，因此会真实创建 OpenGL 上下文；删除步骤会真实调用回收站。
