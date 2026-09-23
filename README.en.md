# Photo Culler

A fast photo-culling tool for Windows photography workflows. Open a folder, flip through shots, mark keep / unkeep, then copy the selected originals to another folder.

License: [MIT](LICENSE)

## Features

- **Fast browsing**: `←` / `→` (wraps at both ends) or click the thumbnail strip; previews are presented by the GPU engine
- **GPU zoom preview**: PySide6 + VisPy/OpenGL texture rendering; wheel zoom is cursor-anchored and eased, pan/zoom happen on the GPU, so large photos stay smooth
- **RAW+JPG pairing**: same-stem RAW (DNG/CR2/NEF/ARW/…) and JPEG merge into one culling item
- **Keep marks**: `Space` toggles keep; yellow star on the thumbnail; optional “kept only” filter
- **Quick delete**: `Del` sends the current group (RAW+JPG together) to the Windows Recycle Bin
- **Read-only sources**: browsing and marking never move, rename, or edit originals; export only copies; the only exception is delete, which is Recycle Bin (recoverable), not permanent erase
- **Camera RAW**: DNG plus Canon/Nikon/Sony/Olympus/Panasonic/Fujifilm and more via LibRaw; embedded preview first, full postprocess at 100%
- **Memory-aware cache**: sliding-window JPEG previews (long edge ≤ 2560) with a slot count derived from free/total RAM; full pixels load only for the current photo when zoomed

## Supported formats

| Type | Notes |
|---|---|
| `.jpg` / `.jpeg` | Preview-sized sliding-window cache + on-demand full decode for 100% |
| `.png` / `.tif` / `.tiff` | Decoded as needed |
| `.dng` `.cr2` `.cr3` `.nef` `.nrw` `.arw` `.srf` `.sr2` `.orf` `.rw2` `.raf` `.pef` `.raw` `.rwl` `.3fr` `.fff` `.mrw` `.erf` `.dcr` `.kdc` `.mos` `.iiq` | Camera RAW via LibRaw; embedded preview first; full postprocess for 100% |

The chosen folder and its **ordinary subfolders** are scanned recursively (symlinks/junctions are not followed). The walk uses `os.scandir` + an explicit directory stack on a background thread; results are sorted by relative path and swapped in as one snapshot.

## Requirements

- Windows
- Python 3.13 recommended

```bash
pip install -r requirements.txt
```

Dependencies: `PySide6`, `vispy`, `PyOpenGL` (GPU preview shell), `Pillow`, `rawpy`, `numpy`.

### UI backend

| `PHOTOCULLER_UI` | Behaviour |
|---|---|
| unset / `auto` | Prefer the PySide6 + VisPy GPU shell; fall back to Tkinter when Qt deps are missing |
| `qt` | Force the GPU shell; fail with install instructions if deps are missing |
| `tk` | Force the Tkinter shell (legacy CPU preview pipeline) |

The Tkinter shell is kept intact as the fallback for machines without usable OpenGL (VMs, remote desktops, old GPUs). Both shells share the same decode, cache, export, and selection-store modules.

Optional (faster JPEG previews/thumbnails):

```bash
pip install PyTurboJPEG
```

Windows also needs the libjpeg-turbo native library (`turbojpeg` / `jpeg62` DLL). Without it the app uses Pillow `draft()` as before. Set `PHOTOCULLER_NO_TURBOJPEG=1` to force the Pillow path.

> Without `rawpy`, the app still runs: camera RAW preview is unavailable; other formats work normally.

> Note: under the GPU shell the preview/zoom path no longer goes through `preview_engine`, so the DirectML/CuPy resample switch (`PHOTOCULLER_RESAMPLE`) only affects the Tkinter shell.

### GPU acceleration schemes

The app detects the machine's display adapters (integrated / discrete) and offers a few selectable acceleration schemes. Reach them at **加速 → GPU 加速…** in the left sidebar, or from the "更多…" menu; the right sidebar's 加速 section always shows the detected GPUs and the active scheme.

Detection merges three sources, because no single one is complete:

| Source | What it contributes |
|---|---|
| Registry `HKLM\SYSTEM\...\Class\{4d36e968-...}` | Every **installed** display adapter (including an iGPU the session is not using), driver version, real VRAM |
| DXGI (`dxgi.dll` → `EnumAdapters1`) | Adapters the graphics stack can actually hand out, plus `DXGI_ADAPTER_FLAG_SOFTWARE` |
| The live OpenGL context | The renderer the preview is actually using |

On a hybrid laptop the registry and DXGI disagree usefully: the registry lists both the iGPU and the dGPU, while DXGI lists only the one attached to the current session. Reading DXGI alone would answer "is there an integrated GPU?" incorrectly, so both are used.

Available schemes:

| Scheme | Mechanism | Takes effect |
|---|---|---|
| **Auto (recommended)** | Clears this app's GPU preference, handing the choice back to Windows | Next launch |
| **Prefer discrete (fastest)** | Writes "High performance" (`GpuPreference=2`) as this app's Windows GPU preference | Next launch |
| **Prefer integrated (battery)** | Writes "Power saving" (`GpuPreference=1`) | Next launch |
| **Compatibility (CPU only)** | Switches to the Tkinter shell and disables GPU resampling — no OpenGL at all | Next launch |

When the matching hardware is absent the scheme is still listed but **not selectable**, annotated with the reason (e.g. "no discrete GPU detected"), so an option never appears to work while doing nothing.

The preference is written to `HKCU\Software\Microsoft\DirectX\UserGpuPreferences` (Microsoft's documented per-app GPU preference) — **current user only, fully reversible**: choosing "Auto" deletes the value. It is **not** written during a source run, because the process is then the shared `python.exe` and the preference would re-route every Python program on the machine; in the packaged build it applies directly.

> Two options were investigated and deliberately **not** offered:
> - **ANGLE / Direct3D backend**: Qt 6 removed ANGLE from its official builds and the PySide6 wheel has no `libEGL.dll` / `libGLESv2.dll`, so `QT_OPENGL=angle` would silently do nothing.
> - **`QT_OPENGL=software`** (Qt's bundled software OpenGL): `opengl32sw.dll` is Mesa 11.2 / GLSL **1.30**, far below what VisPy's scene shaders need. Measured here, the Qt shell fails to obtain a context at all under it (`stage.gpu_info` comes back empty), so it would break the preview rather than rescue it. The real CPU path is the Tkinter shell.

Settings live in `%LOCALAPPDATA%\PhotoCuller\settings.json`, kept separate from the per-folder selection records.

### Live resource usage

The right sidebar's 资源 section shows five readings, refreshed about once a second:

| Reading | Source |
|---|---|
| CPU | Machine-wide CPU load (`GetSystemTimes`, no performance counters needed) |
| 内存 | System physical memory in use (used / total + percentage) |
| 本程序 | This process's working set (resident memory) |
| GPU | Machine-wide GPU utilisation (all engines summed, capped at 100%) |
| 显存 | Dedicated VRAM in use (used / total + percentage) |

Hovering any row reveals this app's CPU (as a share of the machine), per-adapter GPU utilisation, and this app's own GPU and VRAM usage. Adapters are matched by DXGI's LUID, so the tooltip names real cards rather than showing hex identifiers.

Sampling runs on a **background thread** — both GPU counters and CPU need two collections about a second apart before they can produce a rate — and the UI only touches its labels when a new reading lands. On a machine without GPU performance counters (a VM, a trimmed-down install) the GPU and VRAM rows show "—" with the reason underneath: **"—" means "cannot measure", which is not the same as "0%"**. CPU comes from `GetSystemTimes`, so it keeps working there.

## Usage

Run from source (recommended — picks an interpreter that has the dependencies):

```bat
run.bat
```

Or name the interpreter yourself (it **must** be the one with the dependencies):

```bash
.venv-build\Scripts\python.exe app.py     # the repo's own venv
```

> ⚠️ A bare `python app.py` (for example double-clicking `app.py`) uses the
> system Python, which normally has neither `numpy` nor `PySide6`. Both shells
> then fail and all you get is a startup error dialog. Nothing is broken — it is
> the wrong interpreter. Use `run.bat` to avoid this.

Force a shell with `set PHOTOCULLER_UI=qt` / `set PHOTOCULLER_UI=tk`.

A folder picker opens on start. Press `O` to choose another folder.

### Keyboard

| Key | Action |
|---|---|
| `←` / `→` | Previous / next photo (wraps around) |
| `Space` | Keep / unkeep |
| `F` | Cycle RAW+JPG export mode (both → RAW → JPG) |
| `Del` | Delete current group (Recycle Bin) |
| `O` | Open photo folder |
| `E` | Export kept photos |
| `Esc` | Cancel in-progress export |
| `Z` | Toggle fit / 100% |
| `1` | 100% actual pixels |
| `+` / `-` | Zoom in / out |
| `Ctrl+Shift+X` | Clear all keep marks |
| `Ctrl+Shift+M` | Reset all pair modes |

### Mouse

- **Preview wheel**: cursor-anchored zoom (GPU eased animation)
- **Preview drag**: pan when zoomed in
- **Thumbnail strip wheel**: horizontal scroll
- **Thumbnail click**: jump to that photo

## Project layout

```
PhotoCuller-source/
├── app.py                 # Entry (dispatches GPU / Tk shell via PHOTOCULLER_UI)
├── config.py              # Shared constants
├── domain.py              # PhotoGroup / grouping / export planning (no GUI)
├── imaging.py             # Image decode (JPG/PNG/TIFF/DNG)
├── winshell.py            # HiDPI + Recycle Bin
├── selection_store.py     # Selection persistence (%LOCALAPPDATA%)
├── app_settings.py        # User settings (%LOCALAPPDATA%\PhotoCuller\settings.json)
├── version_info.py        # Version → exe resource + installer name (source: config.APP_VERSION)
├── gpu_info.py            # GPU detection (registry + DXGI + live GL; iGPU/dGPU classification)
├── gpu_accel.py           # Selectable schemes (environment + Windows per-app GPU preference)
├── sysmon.py              # Live resource sampling (RAM / working set / GPU / VRAM, background thread)
├── jpeg_preloader.py      # JPEG preview LRU + sliding-window preload
├── image_loader.py        # Background decode (preview / full-res)
├── export_service.py      # Background export (progress / Esc cancel)
├── thumbnail_service.py   # Background thumbnail decode
├── workers.py             # Latest-wins single-thread worker
├── sysmem.py              # RAM probe + adaptive cache limit
├── ram_frames.py          # Shared-memory frames
├── temp_cleanup.py        # Temp file cleanup
├── gpu_preview.py         # GPU preview engine (VisPy/OpenGL texture + ZoomPlan math)
├── qt_ui.py               # PySide6 presentation layer (default)
├── ui.py                  # Tkinter presentation layer (fallback, fully featured)
├── widgets.py             # Rounded toolbar buttons / toggle
├── preview_engine.py      # Tk geometry + dual-frame background render
├── resample_backend.py    # Tk resample backends (CPU / DirectML / CUDA)
├── requirements.txt
├── test_gpu_ui.py         # GPU shell end-to-end smoke test
├── test_version.py        # Version chain (reads the resource back from the built exe)
├── test_sysmon.py         # Resource sampling (formatting, degradation, LUID parsing, thread)
├── test_gpu_accel.py      # GPU detection / scheme tests (classification, merge, settings, registry)
├── test_entry_dispatch.py # Entry dispatch / fallback behaviour
├── test_smoke.py          # Tk shell smoke test
├── test_delete.py         # Delete tests
├── run.bat                # Launch from source (picks a usable interpreter)
├── build_exe.bat          # Build the onedir bundle (dist\PhotoCuller)
├── build_installer.bat    # Build the one-file installer
├── bench_zoom.py          # Zoom smoothness benchmark (frame pacing / jank / cost breakdown)
└── Photo Culler-实现说明.md
```

## Architecture notes

- **Layers**: `domain` and services (decode, cache, export, shell) are separate from the presentation layers; core logic is testable without a GUI
- **UI dispatch**: `app.py` picks `qt_ui` (default) or `ui` from `PHOTOCULLER_UI`, degrading to whichever side is actually available
- **GPU preview**: `gpu_preview.py` uploads the preview as an OpenGL texture; scene coordinates are always **original pixels**. `ZoomPlan` owns the pure zoom-clamping math and `SmoothPanZoomCamera` implements cursor-anchored eased wheel zoom. The GPU context is initialised only after the window is first shown
- **Zoom semantics**: `pixel_zoom()` is display pixels per original pixel (`1.0` = 100%); fit never upscales small photos; the cap is `ZOOM_MAX_PIXEL_SCALE` (4×)
- **100% inspect**: the full-resolution pixels load on demand only once the preview texture is *actually magnified* past 1:1 (`pixel_zoom` beyond the preview's 1:1 scale ×1.02) and replace the texture; because scene coordinates are unchanged the view stays put. The whole upgrade (request → decode → texture swap) waits until the wheel goes quiet, so the swap never interrupts a gesture. Returning to fit releases the full copy
- **Zoom smoothness**: on a 24MP photo the worst frame interval during a zoom gesture is ≤20ms with zero dropped frames (`bench_zoom.py` reproduces this)
- **JPEG cache**: preview-sized only (long edge ≤ 2560); slot count adapts to free RAM (~6–60)
- **Export**: background copy, status-bar progress, `Esc` to cancel
- **Thumbnails**: visible range only; JPEG uses Pillow `draft()`; decoded off the UI thread; cache key is path id + scan-time mtime
- **Delete**: `SHFileOperationW` + `FOF_ALLOWUNDO` for the whole group; partial failures keep remaining members
- **Selections**: `%LOCALAPPDATA%\PhotoCuller\selections\<hash>.json`; save errors surface in the status bar
- **Dropping stale results**: background results return via a queue + timer poll, validated by both a generation counter and a path_id match

Chinese implementation notes: [Photo Culler-实现说明.md](Photo%20Culler-%E5%AE%9E%E7%8E%B0%E8%AF%B4%E6%98%8E.md).

## Versioning

**`APP_VERSION` in `config.py` is the single source of truth.** Change that one string and everything follows automatically:

| Follows | Where |
|---|---|
| Windows version resource | Stamped into both exes — visible in 属性 → 详细信息 |
| Installer filename | `dist\Photo-Culler-Setup<version>.exe`, **no manual renaming** |
| Window title | `Photo Culler <version> — <folder>` |
| Right-sidebar version footer | `v<version>`, click to open 关于 |
| 更多… → 关于 Photo Culler… | Version, version resource, renderer, installer name (also copied to the clipboard) |
| First line of `app.py --self-test` | So "which build are you running?" is answerable |
| Installer window | Version in both the title and the description |

`version_info.py` turns `APP_VERSION` into the version-resource text PyInstaller expects (serializing PyInstaller's own `VSVersionInfo` rather than hand-writing the structure) and derives the installer name. Both `.spec` files **generate that file at build time** into `build/`, so nothing version-related is committed and a stale artifact cannot silently ship.

`test_version.py` pins the whole chain: constant → resource fields → generated file deserializes through PyInstaller → every consumer references the constant instead of hardcoding → `installer.iss`'s `MyAppVersion` matches → and finally it **reads the version resource back out of the built exe**, so a stale `dist/` fails loudly.

> Release flow: bump `config.APP_VERSION` → run `build_exe.bat`, then `build_installer.bat` → `python test_version.py` to verify the artifacts.
> `installer.iss` is the **optional manual path** (`ISCC.exe installer.iss`); the automated build uses `Installer.spec`. If you do use it, keep `MyAppVersion` in sync — the test checks it.

> ⚠️ Older setups left in `dist\` (e.g. `Photo-Culler-Setup1.0.1.exe`) contain **none of the later fixes** — running one installs an old build. Use the file whose name carries the current version.

## Tests

```bash
python app.py --self-test        # runtime self-check: version first, real Tk root, GPU deps, live adapter + resource probe
python test_repo_hygiene.py      # repo hygiene: no committed conflict markers, no unmerged index entries, every .py parses
python test_version.py           # version chain: constant → resource → specs → actually stamped into the built exe
python test_sysmon.py            # resource sampling: formatting, "—" degradation, LUID parsing, sampler lifecycle
python test_gpu_ui.py            # GPU shell end to end (scan/nav/GPU zoom/full-res/filter/delete/export/layout/accel menu)
python test_gpu_accel.py         # GPU detection + schemes (classification, source merge, settings, reversible registry)
python test_entry_dispatch.py    # entry dispatch and fallback messages
python test_smoke.py             # Tk shell: preview / nav / zoom / keep / filter
python test_delete.py            # Recycle Bin / single / pair delete
```

`test_gpu_ui.py` builds its window with `WA_DontShowOnScreen`, so it gets a real OpenGL context without appearing on the desktop; its delete step does hit the real Recycle Bin.

> `build_exe.bat`, `build_installer.bat` and `run.bat` **must keep CRLF line endings**: they contain nested `for` / `if` blocks, and LF-only files get mis-parsed by cmd. Re-check the endings after editing, and **do not edit these scripts while a build is running** — cmd reads them as it executes, which makes PyInstaller run twice.
