"""Functional smoke test: instantiate the app, load a real photo folder,
and verify the preview/thumbnail pipelines run end to end."""
import sys
import tempfile
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
paths = sorted(p for p in tmp.iterdir() if p.is_file())
groups = pc.build_photo_groups(paths)
assert len(groups) == 3, f"expected 3 groups, got {len(groups)}"
pair = next(g for g in groups if g.paired_raw_jpeg)
assert pair.key.startswith("pair|"), pair.key
assert len(pair.members) == 2
assert pair.primary.name == "DSC_0001.JPG"
assert pair.primary_id.endswith("DSC_0001.JPG")
singles = [g for g in groups if not g.paired_raw_jpeg]
assert all(len(g.members) == 1 for g in singles)
print("[1] build_photo_groups OK")

# --- 1b. directory scan and reduced thumbnail decode ---
scanned_names = [path.name for path in pc.scan_photo_paths(tmp)]
assert scanned_names == ["DSC_0001.DNG", "DSC_0001.JPG", "IMG_0002.PNG", "IMG_0003.TIFF"]
large_path = tmp / "large.jpg"
Image.new("RGB", (3200, 2400), "purple").save(large_path, quality=90)
from imaging import read_raster_image

small = read_raster_image(large_path, (264, 176))
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
