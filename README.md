# Photo Culler

一个面向 Windows 摄影工作流的快速选片工具。打开照片文件夹，快速浏览并标记保留/取消保留，最后把选中的原始文件复制到另一个文件夹。

## 功能特性

- **快速浏览**：`[` / `]` 或点击缩略图栏切换照片，预览图双线程后台渲染，拖动缩放流畅
- **RAW+JPG 自动绑定**：同一次拍摄的 `DSC_0001.DNG` 与 `DSC_0001.JPG` 自动合并为一个选片项，不再重复显示
- **保留标记**：`Space` 标记/取消保留，缩略图黄色星号表示已保留；支持"只看保留"筛选
- **快速删除**：`Del` 把当前组（RAW+JPG 整组）移入 Windows 回收站，可随时还原
- **只读原则**：浏览和选片从不修改、移动或重命名原始照片；导出仅复制，唯一的例外是删除——它只把文件送进回收站，不做永久删除
- **RAW 支持**：DNG 优先读取相机内嵌预览，无预览时用 LibRaw 生成屏幕预览
- **低内存架构**：JPG 采用"墓碑 LRU + 滑动窗口"缓存，只保留当前位置附近的解码图，可打开大型文件夹；缩略图优先按目标尺寸解码

## 支持格式

| 类型 | 说明 |
|---|---|
| `.jpg` / `.jpeg` | 附近照片全分辨率预载 + LRU 滑动窗口缓存 |
| `.png` / `.tif` / `.tiff` | 按需解码 |
| `.dng` | 内嵌预览优先，无预览时 LibRaw 降级解码 |

仅扫描文件夹第一层，不递归子目录。

## 安装

需要 Python 3.13（Windows）。

```bash
pip install -r requirements.txt
```

依赖：`Pillow`、`rawpy`、`numpy`。

> 未安装 `rawpy` 时程序自动降级：DNG 无法预览，其余格式正常。

## 使用

```bash
python app.py
```

启动后程序会自动弹出文件夹选择框，也可按 `O` 重新打开。

### 快捷键

| 按键 | 功能 |
|---|---|
| `[` / `]` | 上一张 / 下一张 |
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
├── jpeg_preloader.py         # JPEG LRU + 滑动窗口预载（单 worker）
├── image_loader.py           # 全分辨率后台解码
├── preview_engine.py         # 预览几何 + 双帧后台渲染
├── export_service.py         # 后台导出（进度 / Esc 取消）
├── workers.py                # latest-wins 单线程 worker
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
- **JPG 缓存**：线程安全 LRU（上限 60 张）+ 导航滑动窗口预载；单 worker，快速翻页会替换未开始的任务而不是堆线程
- **导出**：后台拷贝，状态栏显示进度，导出中按 `Esc` 可取消
- **目录读取**：`os.scandir` 单次枚举第一层文件
- **缩略图**：只为可见范围生成；JPEG 用 `draft()` 降采样解码；缓存键为路径身份 + mtime（不含显示序号）
- **RAW 解码**：rawpy `extract_thumb()` 优先，`postprocess(half_size=True)` 兜底
- **删除**：`SHFileOperationW` + `FOF_ALLOWUNDO` 整组移入回收站，删除前二次确认
- **选片记录**：`%LOCALAPPDATA%\PhotoCuller\selections\<hash>.json`，保存失败会在状态栏提示

详见 [Photo Culler-实现说明.md](Photo%20Culler-%E5%AE%9E%E7%8E%B0%E8%AF%B4%E6%98%8E.md)。

## 测试

```bash
python app.py --self-test        # 运行时自检
python test_smoke.py             # 功能冒烟测试（预览/导航/缩放/保留/筛选）
python test_delete.py            # 删除功能测试（回收站调用/单张删除/整组删除）
```
