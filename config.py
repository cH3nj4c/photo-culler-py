"""Shared constants for Photo Culler."""

APP_NAME = "Photo Culler"

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

RESIZE_DEBOUNCE_MS = 220

# Preview crop/resize backend: auto | cpu | gpu
# auto = DirectML → CUDA → CPU; gpu = any available accelerator; cpu = software only
RESAMPLE_MODE = "auto"
