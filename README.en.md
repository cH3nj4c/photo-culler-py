# Photo Culler

A fast photo-culling tool for Windows photography workflows. Open a folder, flip through shots, mark keep / unkeep, then copy the selected originals to another folder.

## Features

- **Fast browsing**: `←` / `→` (wraps at both ends) or click the thumbnail strip; previews render on a background pool with interactive + quality frames
- **RAW+JPG pairing**: same-stem RAW (DNG/CR2/NEF/ARW/…) and JPEG merge into one culling item
- **Keep marks**: `Space` toggles keep; yellow star on the thumbnail; optional “kept only” filter
- **Quick delete**: `Del` sends the current group (RAW+JPG together) to the Windows Recycle Bin
- **Read-only sources**: browsing and marking never move, rename, or edit originals; export only copies; the only exception is delete, which is Recycle Bin (recoverable), not permanent erase
- **Camera RAW**: DNG plus Canon/Nikon/Sony/Olympus/Panasonic/Fujifilm and more via LibRaw; embedded preview first, full postprocess at 100%
- **Memory-aware cache**: sliding-window JPEG previews (long edge ≤ 2560) with a slot count derived from free/total RAM; full pixels load only for the current photo when zoomed
- **Gallery transition**: photo switches slide the old frame out and the new frame in

## Supported formats

| Type | Notes |
|---|---|
| `.jpg` / `.jpeg` | Preview-sized sliding-window cache + on-demand full decode for 100% |
| `.png` / `.tif` / `.tiff` | Decoded as needed |
| `.dng` `.cr2` `.cr3` `.nef` `.nrw` `.arw` `.srf` `.sr2` `.orf` `.rw2` `.raf` `.pef` `.raw` `.rwl` `.3fr` `.fff` `.mrw` `.erf` `.dcr` `.kdc` `.mos` `.iiq` | Camera RAW via LibRaw; embedded preview first; full postprocess for 100% |

Only the top level of the chosen folder is scanned (no recursion into subfolders).

## Requirements

- Windows
- Python 3.13 recommended

```bash
pip install -r requirements.txt
```

Dependencies: `Pillow`, `rawpy`, `numpy`.

Optional (faster JPEG previews/thumbnails):

```bash
pip install PyTurboJPEG
```

Windows also needs the libjpeg-turbo native library (`turbojpeg` / `jpeg62` DLL). Without it the app uses Pillow `draft()` as before. Set `PHOTOCULLER_NO_TURBOJPEG=1` to force the Pillow path.

Optional GPU preview resample (crop/zoom frames only; Tk UI unchanged):

```bash
# DirectML (NVIDIA / AMD / Intel)
pip install torch torch-directml

# or CUDA via CuPy (NVIDIA)
pip install cupy-cuda12x
```

Probe order is **DirectML → CUDA → CPU**. Control with `PHOTOCULLER_RESAMPLE=auto|cpu|gpu` (default `auto`). Without GPU packages the app uses the CPU path as before.

> Without `rawpy`, the app still runs: camera RAW preview is unavailable; other formats work normally.

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

- **Preview wheel**: zoom
- **Preview drag**: pan when zoomed in
- **Thumbnail strip wheel**: horizontal scroll
- **Thumbnail click**: jump to that photo

## Project layout

```
PhotoCuller-source/
├── app.py                 # Entry (bundled Tcl/Tk setup, then UI)
├── config.py              # Shared constants
├── domain.py              # PhotoGroup / grouping / export planning (no Tk)
├── imaging.py             # Image decode (JPG/PNG/TIFF/DNG)
├── winshell.py            # HiDPI + Recycle Bin
├── selection_store.py     # Selection persistence (%LOCALAPPDATA%)
├── jpeg_preloader.py      # JPEG preview LRU + sliding-window preload
├── image_loader.py        # Background decode (preview / full-res)
├── preview_engine.py      # Geometry + dual-frame background render
├── export_service.py      # Background export (progress / Esc cancel)
├── thumbnail_service.py   # Background thumbnail decode
├── workers.py             # Latest-wins single-thread worker
├── sysmem.py              # RAM probe + adaptive cache limit
├── widgets.py             # Rounded toolbar buttons / toggle
├── ui.py                  # Tkinter presentation layer
├── requirements.txt
├── test_smoke.py
├── test_delete.py
└── Photo Culler-实现说明.md
```

## Architecture notes

- **Layers**: `domain` and services (decode, cache, export, shell) are separate from `ui`; core logic is testable without Tk
- **UI thread**: paints and events only — no synchronous full-image decode or file copy on the main thread
- **Preview render**: 2-thread pool, interactive (downsampled) + quality frames; generation IDs drop stale results
- **JPEG cache**: preview-sized only (long edge ≤ 2560); slot count adapts to free RAM (~6–60); single latest-wins preload worker
- **100% inspect**: loads full-resolution pixels on demand; releasing back to fit drops the full copy
- **Export**: background copy, status-bar progress, `Esc` to cancel
- **Thumbnails**: visible range only; JPEG uses Pillow `draft()`; decoded off the UI thread; cache key is path id + scan-time mtime
- **Delete**: `SHFileOperationW` + `FOF_ALLOWUNDO` for the whole group; partial failures keep remaining members
- **Selections**: `%LOCALAPPDATA%\PhotoCuller\selections\<hash>.json`; save errors surface in the status bar

Chinese implementation notes: [Photo Culler-实现说明.md](Photo%20Culler-%E5%AE%9E%E7%8E%B0%E8%AF%B4%E6%98%8E.md).

## Tests

```bash
python app.py --self-test        # runtime self-check
python test_smoke.py             # preview / nav / zoom / keep / filter
python test_delete.py            # Recycle Bin / single / pair delete
```
