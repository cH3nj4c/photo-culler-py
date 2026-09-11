"""Photo Culler - a small Windows-first photo selection application.

The app deliberately copies selected originals on export; it never moves,
renames, or edits the source photographs.  The single exception is the
explicit delete command, which sends originals to the Windows Recycle Bin
so a mistaken deletion stays recoverable.

This module is a thin entry point. Implementation lives in sibling modules
next to this file.
"""

from __future__ import annotations

from winshell import configure_bundled_tk_runtime

configure_bundled_tk_runtime()

from domain import (  # noqa: E402
    PhotoGroup,
    build_photo_groups,
    scan_photo_paths,
    selected_members,
)
from ui import PhotoCuller, main  # noqa: E402
from winshell import send_to_recycle_bin  # noqa: E402

__all__ = [
    "PhotoCuller",
    "PhotoGroup",
    "build_photo_groups",
    "scan_photo_paths",
    "selected_members",
    "send_to_recycle_bin",
    "main",
]


if __name__ == "__main__":
    main()
