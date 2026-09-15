# Photo Culler

<p align="center">
  <img src="assets/logo.png" alt="Photo Culler logo" width="280"/>
</p>

一个面向 Windows 摄影工作流的快速选片工具。打开照片文件夹，快速浏览并标记保留/取消保留，最后把选中的原始文件复制到另一个文件夹。

[English](README.en.md)

License: [MIT](LICENSE)

## 功能特性

- **快速浏览**：`←` / `→`（到头循环）或点击缩略图栏切换照片，切换带滑动过场；预览后台双帧渲染
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

依赖：`Pillow`、`rawpy`、`numpy`。

可选加速 JPEG 预览/缩略图解码：

```bash
pip install PyTurboJPEG
```

Windows 还需本机有 libjpeg-turbo 动态库（`turbojpeg` / `jpeg62`）。未安装时自动回退 Pillow `draft()`；可用环境变量 `PHOTOCULLER_NO_TURBOJPEG=1` 强制走 Pillow。

可选 GPU 预览重采样（仅 crop/缩放，Tk 界面不变）：

```bash
# DirectML（NVIDIA / AMD / Intel）
pip install torch torch-directml
# 或 CUDA（NVIDIA，CuPy）
pip install cupy-cuda12x
```

探测顺序：**DirectML → CUDA → CPU**。`PHOTOCULLER_RESAMPLE=auto|cpu|gpu`（默认 `auto`）。未安装 GPU 包时与现在一样走 CPU。

> 未安装 `rawpy` 时程序自动降级：相机 RAW 无法预览，其余格式正常。

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

- **预览区滚轮**：缩放
- **预览区拖拽**：平移大图
- **缩略图栏滚轮**：横向滚动
- **点击缩略图**：跳转到该照片

## 项目结构

```
PhotoCuller-source/
├── app.py                    # 入口（配置打包 Tcl/Tk 后启动 UI）
├── config.py                 # 共享常量
├── domain.py                 # PhotoGroup / 分组 / 导出成员规划（无 Tk 依赖）
├── imaging.py                # 图片解码（JPG/PNG/TIFF/DNG）
├── winshell.py               # HiDPI 与回收站删除
├── selection_store.py        # 选片记录持久化（%LOCALAPPDATA%）
├── jpeg_preloader.py         # JPEG 预览 LRU + 滑动窗口预载（单 worker）
├── image_loader.py           # 后台解码（预览尺寸 / 全分辨率）
├── preview_engine.py         # 预览几何 + 双帧后台渲染（zoom 相对原图像素）
├── export_service.py         # 后台导出（进度 / Esc 取消）
├── workers.py                # latest-wins 单线程 worker
├── sysmem.py                 # 物理内存探测 + 自适应缓存上限
├── ui.py                     # Tkinter 界面层
├── requirements.txt
├── test_smoke.py             # 功能冒烟测试
├── test_delete.py            # 删除功能测试
└── Photo Culler-实现说明.md
```

## 技术架构

- **分层**：`domain` / services（解码、缓存、导出、回收站）与 `ui` 分离，领域逻辑可不依赖 Tk 测试
- **UI**：Tkinter / ttk；主线程只负责绘制与事件，不再同步解码全图或拷贝导出文件
- **预览渲染**：2 线程后台池，双帧合并（降采样交互帧 + 全分辨率质量帧），generation 丢弃过期帧
- **当前图解码**：后台线程池完成；JPEG 命中滑动窗口缓存时直接复用
- **JPG 缓存**：只缓存**预览尺寸**（长边 ≤2560）解码图，数量按系统总内存/空闲内存自适应（约 6–60 张）；快速翻页单 worker latest-wins
- **100% 检视**：缩放超过适合窗口或按 `1` 时再按需读入全分辨率原图，回到适合窗口会释放全图
- **导出**：后台拷贝，状态栏显示进度，导出中按 `Esc` 可取消
- **目录读取**：`os.scandir` 单次枚举第一层文件
- **缩略图**：只为可见范围生成；JPEG 用 `draft()` 降采样解码；缓存键为路径身份 + mtime（不含显示序号）
- **RAW 解码**：rawpy/LibRaw；`extract_thumb()` 优先，`postprocess(half_size=True)` 兜底；100% 用全尺寸 postprocess
- **删除**：`SHFileOperationW` + `FOF_ALLOWUNDO` 整组移入回收站，删除前二次确认；部分失败时保留剩余成员
- **选片记录**：`%LOCALAPPDATA%\PhotoCuller\selections\<hash>.json`，保存失败会在状态栏提示
- **内存探测**：`sysmem.py` 通过 `GlobalMemoryStatusEx` 读取物理内存，打开文件夹时重算缓存上限

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
python test_smoke.py             # 功能冒烟测试（预览/导航/缩放/保留/筛选）
python test_delete.py            # 删除功能测试（回收站调用/单张删除/整组删除）
```
