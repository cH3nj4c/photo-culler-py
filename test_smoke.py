"""Functional smoke test: instantiate the app, load a real photo folder,
and verify the preview/thumbnail pipelines run end to end."""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image
import app as pc
import ui as pc_ui

# --- 1. pure-logic test: build_photo_groups ---
tmp = Path(tempfile.mkdtemp(prefix="pc_smoke_"))
(tmp / "DSC_0001.DNG").write_bytes(b"x")
(tmp / "DSC_0001.JPG").write_bytes(b"x")
(tmp / "IMG_0002.PNG").write_bytes(b"x")
(tmp / "IMG_0003.TIFF").write_bytes(b"x")
from domain import scan_photo_entries

entries = scan_photo_entries(tmp)
mtime_map = {str(p): m for p, m in entries}
groups = pc.build_photo_groups([p for p, _m in entries], mtime_map)
assert len(groups) == 3, f"expected 3 groups, got {len(groups)}"
pair = next(g for g in groups if g.paired_raw_jpeg)
assert pair.key.startswith("pair|"), pair.key
assert len(pair.members) == 2
assert pair.primary.name == "DSC_0001.JPG"
assert pair.primary_id.endswith("DSC_0001.JPG")
assert pair.primary_mtime_ns > 0, "scan-time mtime should be recorded"
singles = [g for g in groups if not g.paired_raw_jpeg]
assert all(len(g.members) == 1 for g in singles)
print("[1] build_photo_groups OK")

# --- 1b. directory scan and reduced thumbnail decode ---
scanned_names = [path.name for path in pc.scan_photo_paths(tmp)]
assert scanned_names == ["DSC_0001.DNG", "DSC_0001.JPG", "IMG_0002.PNG", "IMG_0003.TIFF"]
large_path = tmp / "large.jpg"
Image.new("RGB", (3200, 2400), "purple").save(large_path, quality=90)
from imaging import read_raster_image

small, _orig = read_raster_image(large_path, (264, 176))
assert small.width <= 264 and small.height <= 176, small.size
print("[1b] fast directory scan and reduced decode OK: %s" % (small.size,))

# --- 1c. selection store round-trip ---
from selection_store import load_selection, save_selection

sel_dir = tmp / "sel"
sel_dir.mkdir()
err = save_selection(sel_dir, {"k1", "k2"}, {"k1": "jpg"})
assert err is None, err
kept, modes = load_selection(sel_dir)
assert kept == {"k1", "k2"} and modes == {"k1": "jpg"}
print("[1c] selection store OK")

# --- 1d. partial-pair rebuild + visible filter ---
from domain import filter_visible_items

pair_dir = tmp / "pair_logic"
pair_dir.mkdir()
dng = pair_dir / "DSC_0100.DNG"
jpg = pair_dir / "DSC_0100.JPG"
dng.write_bytes(b"raw")
jpg.write_bytes(b"jpg")
other = pair_dir / "OTHER.JPG"
other.write_bytes(b"x")
groups = pc.build_photo_groups([dng, jpg, other])
pair = next(g for g in groups if g.paired_raw_jpeg)
assert pair.members == (dng, jpg) or set(pair.members) == {dng, jpg}
# Simulate DNG deleted, JPG remains → rebuild as a single item
rebuilt = pc.build_photo_groups([jpg])
assert len(rebuilt) == 1 and not rebuilt[0].paired_raw_jpeg
assert rebuilt[0].primary == jpg
kept_set = {rebuilt[0].key}
visible = filter_visible_items(rebuilt, kept_set, show_kept_only=True)
assert visible == [], "kept items must hide under 只看保留"
visible_all = filter_visible_items(rebuilt, set(), show_kept_only=False)
assert len(visible_all) == 1
print("[1d] partial rebuild + filter OK")

# --- 1e. adaptive JPEG cache from free RAM ---
from sysmem import (
    JPEG_CACHE_LIMIT_MAX,
    JPEG_CACHE_LIMIT_MIN,
    MemoryInfo,
    get_memory_info,
    recommend_jpeg_cache_limit,
)

info = get_memory_info()
assert info.total_bytes > 0 and info.avail_bytes > 0, info
# Plenty of free RAM → cap at max
assert recommend_jpeg_cache_limit(32 * 1024**3, 16 * 1024**3) == JPEG_CACHE_LIMIT_MAX
# Very tight free RAM → floor at min
tight = recommend_jpeg_cache_limit(8 * 1024**3, 40 * 1024 * 1024)
assert tight == JPEG_CACHE_LIMIT_MIN, tight
# Mid range sits between min and max
mid = recommend_jpeg_cache_limit(16 * 1024**3, 2 * 1024**3)
assert JPEG_CACHE_LIMIT_MIN <= mid <= JPEG_CACHE_LIMIT_MAX, mid
from jpeg_preloader import JpegCache

cache = JpegCache(mid)
assert cache.limit == mid
cache.set_limit(3)
assert cache.limit >= 1  # set_limit floors at 1
print("[1e] adaptive cache plan OK: live_limit=%s mid=%s tight=%s" % (
    recommend_jpeg_cache_limit(), mid, tight))

# --- 1f. preview downscale helper ---
from imaging import decode_preview_photo, fit_long_edge
from config import PREVIEW_CACHE_LONG_EDGE

big = Image.new("RGB", (5000, 3000), "white")
small = fit_long_edge(big, PREVIEW_CACHE_LONG_EDGE)
assert max(small.size) == PREVIEW_CACHE_LONG_EDGE
assert max(fit_long_edge(Image.new("RGB", (800, 600)), PREVIEW_CACHE_LONG_EDGE).size) == 800
prev, orig_size = decode_preview_photo(large_path)
assert max(prev.size) <= PREVIEW_CACHE_LONG_EDGE
assert orig_size == (3200, 2400), orig_size
print("[1f] preview downscale OK:", prev.size, "orig", orig_size)

# --- 1g. async thumbnail service ---
from thumbnail_service import ThumbnailService

thumb_svc = ThumbnailService()
assert thumb_svc.request(("k", 1), large_path, 132, 88) is True
assert thumb_svc.request(("k", 1), large_path, 132, 88) is False  # already pending/done key
got = []
for _ in range(80):
    got = thumb_svc.drain()
    if got:
        break
    time.sleep(0.02)
assert got, "thumbnail service produced no event"
key, img, err = got[0]
assert err is None and img is not None
assert img.width <= 132 and img.height <= 88
thumb_svc.shutdown()
print("[1g] async thumbnail service OK")

# --- 2. make real test photos ---
photo_dir = tmp / "photos"
photo_dir.mkdir()
for name, color, size in [
    ("A_0001.jpg", "red", (640, 480)),
    ("A_0002.png", "blue", (400, 300)),
    ("B_0001.tif", "green", (800, 600)),
]:
    img = Image.new("RGB", size, color)
    img.save(photo_dir / name)
print("[2] test photos created")

# --- 3. GUI functional run ---
import tkinter as tk
from tkinter import filedialog, messagebox

pc_ui.filedialog.askdirectory = lambda *a, **k: str(photo_dir)
messagebox.showinfo = lambda *a, **k: None
messagebox.askyesno = lambda *a, **k: True

app = pc.PhotoCuller()
app.withdraw()  # keep it off-screen

result = {"preview": False, "thumbs": 0, "status": "", "error": None}


def probe():
    try:
        result["preview"] = app.preview_photo is not None and app.current_source_image is not None
        result["thumbs"] = len(app.thumbnail_cache)
        result["status"] = app.status_label.cget("text")
        # exercise navigation + zoom + keep + filter
        app.change_index(1)
        assert app._slide_direction == 1, "navigation should trigger a rightward slide"
        app.zoom_fit()
        app.zoom_actual()
        app.toggle_keep()
        app.toggle_filter()
        app.toggle_filter()
        app.change_index(-1)
        result["index_after"] = app.index
    except Exception as exc:  # noqa: BLE001
        result["error"] = repr(exc)
    finally:
        app.after(50, finish)


def finish():
    try:
        app._on_close()
    except Exception:
        pass
    try:
        if app.winfo_exists():
            app.destroy()
    except tk.TclError:
        pass


app.after(2200, probe)  # wait for open_folder + async decode + preview render
app.mainloop()

assert result["error"] is None, result["error"]
assert result["preview"], "preview_photo was not populated"
assert result["thumbs"] > 0, "thumbnail cache is empty"
assert result["index_after"] == 0
print(
    "[3] GUI pipeline OK: preview=%s thumbs=%d status=%r"
    % (result["preview"], result["thumbs"], result["status"])
)
print("SMOKE TEST PASSED")
