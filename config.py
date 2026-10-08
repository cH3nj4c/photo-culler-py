"""Shared constants for Photo Culler."""

APP_NAME = "Photo Culler"

# Single source of truth for the release version.
#
# Bump this one string and every artifact follows: the Windows version resource
# stamped into both exes (so 属性 → 详细信息 shows it), the window title, the
# 关于 dialog, the right sidebar footer, `--self-test`, and the installer's
# output filename (`Photo-Culler-Setup<version>.exe`). Nothing else should
# hardcode a version — see `version_info.py`.
APP_VERSION = "1.2.0"

# Shown as the publisher in the exe's version resource.
APP_PUBLISHER = "cH3nj4c"

# Camera RAW formats decoded via rawpy / LibRaw (vendor-specific + DNG).
RAW_EXTENSIONS = {
    ".dng",  # Adobe / Leica / Ricoh / phone DNG
    ".cr2",  # Canon
    ".cr3",  # Canon
    ".nef",  # Nikon
    ".nrw",  # Nikon
    ".arw",  # Sony
    ".srf",  # Sony
    ".sr2",  # Sony
    ".orf",  # Olympus / OM System
    ".rw2",  # Panasonic
    ".raf",  # Fujifilm
    ".pef",  # Pentax
    ".raw",  # Panasonic / generic
    ".rwl",  # Leica
    ".3fr",  # Hasselblad
    ".fff",  # Hasselblad / Leaf
    ".mrw",  # Minolta
    ".erf",  # Epson
    ".dcr",  # Kodak
    ".kdc",  # Kodak
    ".mos",  # Leaf / Mamiya
    ".iiq",  # Phase One
}

JPEG_EXTENSIONS = {".jpg", ".jpeg"}
RASTER_EXTENSIONS = {".tiff", ".tif", ".png", ".jpeg", ".jpg"}
SUPPORTED_EXTENSIONS = RASTER_EXTENSIONS | RAW_EXTENSIONS

THUMB_WIDTH = 132
THUMB_HEIGHT = 88
THUMB_SLOT = 148
THUMB_CACHE_LIMIT = 110
THUMBNAIL_DECODE_SCALE = 2

# Hard ceiling; live limit comes from sysmem.recommend_jpeg_cache_limit.
JPEG_CACHE_LIMIT = 60
JPEG_PRELOAD_AHEAD = 24
JPEG_PRELOAD_BEHIND = 12
# Parallel JPEG preview decodes (disk-bound on HDD; helps SSD).
JPEG_PRELOAD_WORKERS = 3

# Cached "preview" JPEGs are downscaled to this long edge (display/fit path).
# Full-resolution pixels are loaded only for the current photo when zoomed.
PREVIEW_CACHE_LONG_EDGE = 2560

PREVIEW_OVERSCAN = 0.72
PREVIEW_OVERSCAN_MAX_PX = 560
PREVIEW_INTERACTIVE_DELAY_MS = 24
PREVIEW_QUALITY_DELAY_MS = 150
PREVIEW_POLL_MS = 16

# Stepless zoom: wheel notches + short lerp toward the target scale.
ZOOM_WHEEL_FACTOR = 1.18
ZOOM_KEY_FACTOR = 1.25
ZOOM_LERP = 0.92
ZOOM_SETTLE_RATIO = 0.002
ZOOM_FULLRES_MARGIN = 0.08
ZOOM_FULLRES_SETTLE_MS = 120
ZOOM_INTERACTIVE_DELAY_MS = 1

# Hard zoom ceiling in original-pixel terms (GPU shell uses this; the Tk
# shell hardcodes the same 4.0 in ui.py / preview_engine.py).
ZOOM_MAX_PIXEL_SCALE = 4.0

# GPU shell only. The uploaded preview texture is already being *minified*
# below its own 1:1 scale, so loading the full-resolution image earlier than
# that buys no detail but costs a ~50-110 ms texture upload. This margin is a
# safety factor on top of the texture's native scale.
ZOOM_PREVIEW_NATIVE_MARGIN = 1.02

RESIZE_DEBOUNCE_MS = 220

# Preview crop/resize backend: auto | cpu | gpu
# auto = DirectML → CUDA → CPU; gpu = any available accelerator; cpu = software only
RESAMPLE_MODE = "auto"
