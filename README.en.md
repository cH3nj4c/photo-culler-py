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

## Usage

```bash
python app.py
```

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
├── test_entry_dispatch.py # Entry dispatch / fallback behaviour
├── test_smoke.py          # Tk shell smoke test
├── test_delete.py         # Delete tests
└── Photo Culler-实现说明.md
```

## Architecture notes

- **Layers**: `domain` and services (decode, cache, export, shell) are separate from the presentation layers; core logic is testable without a GUI
- **UI dispatch**: `app.py` picks `qt_ui` (default) or `ui` from `PHOTOCULLER_UI`, degrading to whichever side is actually available
- **GPU preview**: `gpu_preview.py` uploads the preview as an OpenGL texture; scene coordinates are always **original pixels**. `ZoomPlan` owns the pure zoom-clamping math and `SmoothPanZoomCamera` implements cursor-anchored eased wheel zoom. The GPU context is initialised only after the window is first shown
- **Zoom semantics**: `pixel_zoom()` is display pixels per original pixel (`1.0` = 100%); fit never upscales small photos; the cap is `ZOOM_MAX_PIXEL_SCALE` (4×)
- **100% inspect**: past “fit + 8%” the full-resolution pixels load on demand and replace the texture; because scene coordinates are unchanged the view stays put. Returning to fit releases the full copy
- **JPEG cache**: preview-sized only (long edge ≤ 2560); slot count adapts to free RAM (~6–60)
- **Export**: background copy, status-bar progress, `Esc` to cancel
- **Thumbnails**: visible range only; JPEG uses Pillow `draft()`; decoded off the UI thread; cache key is path id + scan-time mtime
- **Delete**: `SHFileOperationW` + `FOF_ALLOWUNDO` for the whole group; partial failures keep remaining members
- **Selections**: `%LOCALAPPDATA%\PhotoCuller\selections\<hash>.json`; save errors surface in the status bar
- **Dropping stale results**: background results return via a queue + timer poll, validated by both a generation counter and a path_id match

Chinese implementation notes: [Photo Culler-实现说明.md](Photo%20Culler-%E5%AE%9E%E7%8E%B0%E8%AF%B4%E6%98%8E.md).

## Tests

```bash
python app.py --self-test        # runtime self-check
python test_gpu_ui.py            # GPU shell end to end (scan/nav/GPU zoom/full-res/filter/delete/export)
python test_entry_dispatch.py    # entry dispatch and fallback messages
python test_smoke.py             # Tk shell: preview / nav / zoom / keep / filter
python test_delete.py            # Recycle Bin / single / pair delete
```

`test_gpu_ui.py` builds its window with `WA_DontShowOnScreen`, so it gets a real OpenGL context without appearing on the desktop; its delete step does hit the real Recycle Bin.
