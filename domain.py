"""Domain model: photo groups, scanning, and selection-related pure logic."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from config import JPEG_EXTENSIONS, RAW_EXTENSIONS, SUPPORTED_EXTENSIONS

PAIR_MODES = ("both", "raw", "jpg")


@dataclass(frozen=True)
class PhotoGroup:
    """One culling decision, optionally made of a RAW and its JPEG preview."""

    key: str
    primary: Path
    primary_id: str
    members: tuple[Path, ...]
    primary_mtime_ns: int = 0

    @property
    def paired_raw_jpeg(self) -> bool:
        return (
            any(p.suffix.lower() in RAW_EXTENSIONS for p in self.members)
            and any(p.suffix.lower() in JPEG_EXTENSIONS for p in self.members)
        )


def normalize_pair_mode(mode: object) -> str:
    if isinstance(mode, str) and mode in PAIR_MODES:
        return mode
    return "both"


def next_pair_mode(current: str) -> str:
    modes = list(PAIR_MODES)
    return modes[(modes.index(normalize_pair_mode(current)) + 1) % len(modes)]


def pair_mode_label(mode: str) -> str:
    return {"both": "RAW+JPG", "raw": "仅 RAW", "jpg": "仅 JPG"}.get(
        normalize_pair_mode(mode), "RAW+JPG"
    )


def selected_members(item: PhotoGroup, mode: str) -> tuple[Path, ...]:
    if not item.paired_raw_jpeg:
        return item.members
    mode = normalize_pair_mode(mode)
    if mode == "raw":
        return tuple(p for p in item.members if p.suffix.lower() in RAW_EXTENSIONS)
    if mode == "jpg":
        return tuple(p for p in item.members if p.suffix.lower() in JPEG_EXTENSIONS)
    return item.members


def unique_destination(folder: Path, filename: str) -> Path:
    """Pick a non-colliding destination path, preserving the original extension."""
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    number = 1
    while True:
        candidate = folder / f"{stem} ({number}){suffix}"
        if not candidate.exists():
            return candidate
        number += 1


def build_photo_groups(
    paths: list[Path],
    mtime_ns_by_path: dict[str, int] | None = None,
) -> list[PhotoGroup]:
    """Hide RAW + JPEG pairs behind one culling item, without grouping unrelated files.

    ``mtime_ns_by_path`` maps ``str(path)`` → st_mtime_ns from the directory
    scan so thumbnail cache keys need no extra ``stat()`` per paint.
    """
    mtimes = mtime_ns_by_path or {}

    def mtime_of(path: Path) -> int:
        return mtimes.get(str(path), 0)

    by_stem: dict[str, list[Path]] = {}
    for path in paths:
        by_stem.setdefault(path.stem.casefold(), []).append(path)

    result: list[PhotoGroup] = []
    for same_name_paths in by_stem.values():
        ordered = sorted(same_name_paths, key=lambda path: path.name.casefold())
        raws = [path for path in ordered if path.suffix.lower() in RAW_EXTENSIONS]
        jpegs = [path for path in ordered if path.suffix.lower() in JPEG_EXTENSIONS]
        paired_members = tuple(raws + jpegs)
        if raws and jpegs:
            primary = jpegs[0]
            primary_id = str(primary.resolve())
            key = "pair|" + str(primary.parent.resolve()).casefold() + "|" + primary.stem.casefold()
            result.append(
                PhotoGroup(
                    key=key,
                    primary=primary,
                    primary_id=primary_id,
                    members=paired_members,
                    primary_mtime_ns=mtime_of(primary),
                )
            )
            paired_paths = set(paired_members)
            for path in ordered:
                if path not in paired_paths:
                    result.append(_single_group(path, mtime_of(path)))
        else:
            for path in ordered:
                result.append(_single_group(path, mtime_of(path)))

    return sorted(result, key=lambda item: item.primary.name.casefold())


def _single_group(path: Path, mtime_ns: int = 0) -> PhotoGroup:
    resolved = str(path.resolve())
    return PhotoGroup(
        key=resolved,
        primary=path,
        primary_id=resolved,
        members=(path,),
        primary_mtime_ns=mtime_ns,
    )


def scan_photo_entries(folder: Path) -> list[tuple[Path, int]]:
    """Recursive scan of *folder* and ordinary subfolders (one snapshot)."""
    found, _dirs, _errors = scan_photo_tree(folder)
    return found


def scan_photo_paths(folder: Path) -> list[Path]:
    """List supported photos under *folder* (including subfolders)."""
    return [path for path, _mtime in scan_photo_entries(folder)]


def scan_photo_tree(
    root: Path,
    on_progress=None,
    should_cancel=None,
) -> tuple[list[tuple[Path, int]], int, int]:
    """Walk *root* with an explicit directory stack (no Python recursion).

    Returns ``(entries, dirs_visited, error_count)``; each entry is
    ``(path, mtime_ns)`` sorted by path relative to *root* (case-insensitive).

    - ``os.scandir`` supplies file/dir types from the listing.
    - Does not follow symlink or junction/reparse directories.
    - Failures increment the error count and the walk continues.
    - ``on_progress(found, dirs_visited, errors)`` runs on the scanner thread.
    - ``should_cancel() -> bool`` aborts the walk early.
    """
    root = Path(root)
    found: list[tuple[Path, int]] = []
    dirs_visited = 0
    errors = 0
    stack: list[Path] = [root]
    supported = SUPPORTED_EXTENSIONS

    while stack:
        if should_cancel is not None and should_cancel():
            break
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        # Never follow links: stay inside the chosen root.
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if Path(entry.name).suffix.casefold() not in supported:
                            continue
                        try:
                            mtime_ns = entry.stat(follow_symlinks=False).st_mtime_ns
                        except OSError:
                            mtime_ns = 0
                        found.append((Path(entry.path), mtime_ns))
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1
            continue
        dirs_visited += 1
        if on_progress is not None:
            try:
                on_progress(len(found), dirs_visited, errors)
            except Exception:
                pass

    def rel_key(pair: tuple[Path, int]) -> str:
        path = pair[0]
        try:
            return str(path.relative_to(root)).casefold()
        except ValueError:
            return path.name.casefold()

    found.sort(key=rel_key)
    return found, dirs_visited, errors


def filter_visible_items(
    all_items: list[PhotoGroup],
    kept: set[str],
    show_kept_only: bool,
) -> list[PhotoGroup]:
    if not show_kept_only:
        return list(all_items)
    return [item for item in all_items if item.key not in kept]
