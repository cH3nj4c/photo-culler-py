"""Functional test for the Recycle Bin delete command.

Covers three layers:
  1. the Win32 primitive (single path, multi-path double-NUL block, missing file)
  2. the GUI flow for a single photo (kept state + caches cleaned, preview recovers)
  3. the GUI flow for a RAW+JPG pair, which must disappear as a whole

Every file touched here is created inside a throwaway temp folder.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402
import app as pc  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix='pc_delete_'))

# --- 1. recycle bin primitive ---
single = tmp / 'trash_me.txt'
single.write_text('x')
untouched = tmp / 'keep_me.txt'
untouched.write_text('y')
assert pc.send_to_recycle_bin([single]) == [], 'single-path recycle failed'
assert not single.exists(), 'file still on disk'
assert untouched.exists(), 'unrelated file was touched'
print('[1] single-path recycle OK')

first, second_file = tmp / 'c.txt', tmp / 'd.txt'
first.write_text('x')
second_file.write_text('y')
assert pc.send_to_recycle_bin([first, second_file]) == [], 'multi-path recycle failed'
assert not first.exists() and not second_file.exists(), 'double-NUL path block rejected'
print('[2] multi-path double-NUL block OK')

failures = pc.send_to_recycle_bin([tmp / 'never_existed.txt'])
assert len(failures) == 1 and '不存在' in failures[0][1], failures
print('[3] missing file reported instead of raised OK')

# --- 2. GUI delete of a single photo ---
photo_dir = tmp / 'photos'
photo_dir.mkdir()
for name, color, size in [('A_0001.jpg', 'red', (640, 480)),
                          ('A_0002.png', 'blue', (400, 300)),
                          ('B_0001.tif', 'green', (800, 600))]:
    Image.new('RGB', size, color).save(photo_dir / name)

pc.filedialog.askdirectory = lambda *args, **kwargs: str(photo_dir)
pc.messagebox.showinfo = lambda *args, **kwargs: None
pc.messagebox.showwarning = lambda *args, **kwargs: None
pc.messagebox.showerror = lambda *args, **kwargs: None
pc.messagebox.askyesno = lambda *args, **kwargs: True

result = {'error': None}
app = pc.PhotoCuller()
app.withdraw()


def probe():
    try:
        result['before'] = len(app.all_items)
        target = app.current_item
        target_key = target.key
        victims = list(target.members)
        app.toggle_keep()
        assert target_key in app.kept, 'precondition: item should be kept'
        app.delete_current()
        result['after'] = len(app.all_items)
        result['files_gone'] = all(not path.exists() for path in victims)
        result['kept_clean'] = target_key not in app.kept and target_key not in app.pair_modes
        result['remaining'] = [item.primary.name for item in app.all_items]
        result['preview_ok'] = app.preview_photo is not None and app.current_source_path is not None
        result['thumbs_ok'] = app.thumb_canvas.find_all() != ()
    except Exception as exc:  # noqa: BLE001
        result['error'] = repr(exc)
    finally:
        app.after(60, finish)


def finish():
    try:
        app._on_close()
    except Exception:
        pass
    try:
        app.destroy()
    except Exception:
        pass


app.after(1600, probe)
app.mainloop()

assert result['error'] is None, result['error']
assert result['before'] == 3 and result['after'] == 2, result
assert result['files_gone'], 'originals still on disk'
assert result['kept_clean'], 'kept / pair_modes were not cleaned up'
assert result['remaining'] == ['A_0002.png', 'B_0001.tif'], result['remaining']
assert result['preview_ok'], 'preview did not recover after the delete'
assert result['thumbs_ok'], 'thumbnail strip is empty after the delete'
print('[4] GUI delete of a single photo OK')

# --- 3. GUI delete of a RAW+JPG pair ---
pair_dir = tmp / 'pairs'
pair_dir.mkdir()
Image.new('RGB', (500, 400), 'purple').save(pair_dir / 'DSC_0001.JPG')
(pair_dir / 'DSC_0001.DNG').write_bytes(b'fake-raw-bytes')
Image.new('RGB', (300, 200), 'orange').save(pair_dir / 'DSC_0002.JPG')

pc.filedialog.askdirectory = lambda *args, **kwargs: str(pair_dir)
pair_result = {'error': None}
pair_app = pc.PhotoCuller()
pair_app.withdraw()


def probe_pair():
    try:
        target = pair_app.current_item
        pair_result['is_pair'] = target.paired_raw_jpeg
        pair_result['members'] = sorted(path.name for path in target.members)
        victims = list(target.members)
        pair_app.cycle_keep_mode()
        pair_result['before'] = len(pair_app.all_items)
        pair_app.delete_current()
        pair_result['after'] = len(pair_app.all_items)
        pair_result['files_gone'] = all(not path.exists() for path in victims)
        pair_result['remaining'] = [item.primary.name for item in pair_app.all_items]
        pair_result['preview_ok'] = pair_app.preview_photo is not None
    except Exception as exc:  # noqa: BLE001
        pair_result['error'] = repr(exc)
    finally:
        pair_app.after(60, finish_pair)


def finish_pair():
    try:
        pair_app._on_close()
    except Exception:
        pass
    try:
        pair_app.destroy()
    except Exception:
        pass


pair_app.after(1600, probe_pair)
pair_app.mainloop()

assert pair_result['error'] is None, pair_result['error']
assert pair_result['is_pair'], 'expected a RAW+JPG pair'
assert pair_result['members'] == ['DSC_0001.DNG', 'DSC_0001.JPG'], pair_result['members']
assert pair_result['before'] == 2 and pair_result['after'] == 1, pair_result
assert pair_result['files_gone'], 'a pair member survived the delete'
assert pair_result['remaining'] == ['DSC_0002.JPG'], pair_result['remaining']
assert pair_result['preview_ok'], 'preview did not recover after the pair delete'
print('[5] RAW+JPG pair deleted as a whole OK')

print('DELETE TEST PASSED')
