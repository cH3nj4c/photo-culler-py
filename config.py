"""Shared constants for Photo Culler."""

APP_NAME = "Photo Culler"

SUPPORTED_EXTENSIONS = {".tiff", ".tif", ".png", ".jpeg", ".jpg", ".dng"}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}

THUMB_WIDTH = 132
THUMB_HEIGHT = 88
THUMB_SLOT = 148
THUMB_CACHE_LIMIT = 110
THUMBNAIL_DECODE_SCALE = 2

# Hard ceiling; live limit comes from sysmem.recommend_jpeg_cache_limit.
JPEG_CACHE_LIMIT = 60
JPEG_PRELOAD_AHEAD = 20
JPEG_PRELOAD_BEHIND = 10

# Cached "preview" JPEGs are downscaled to this long edge (display/fit path).
# Full-resolution pixels are loaded only for the current photo when zoomed.
PREVIEW_CACHE_LONG_EDGE = 2560

PREVIEW_OVERSCAN = 0.72
PREVIEW_OVERSCAN_MAX_PX = 560
PREVIEW_INTERACTIVE_DELAY_MS = 24
PREVIEW_QUALITY_DELAY_MS = 150
PREVIEW_POLL_MS = 16

RESIZE_DEBOUNCE_MS = 220
