"""Domain model: photo groups, scanning, and selection-related pure logic."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from config import JPEG_EXTENSIONS, SUPPORTED_EXTENSIONS

PAIR_MODES = ("both", "raw", "jpg")


@dataclass(frozen=True)
class PhotoGroup:
    """One culling decision, optionally made of a DNG and its JPEG preview."""

    key: str
    primary: Path
    primary_id: str
    members: tuple[Path, ...]

    @property
    def paired_raw_jpeg(self) -> bool:
        return (
            any(p.suffix.lower() == ".dng" for p in self.members)
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
        return tuple(p for p in item.members if p.suffix.lower() == ".dng")
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


def build_photo_groups(paths: list[Path]) -> list[PhotoGroup]:
    """Hide DNG + JPEG pairs behind one culling item, without grouping unrelated files."""
    by_stem: dict[str, list[Path]] = {}
    for path in paths:
        by_stem.setdefault(path.stem.casefold(), []).append(path)

    result: list[PhotoGroup] = []
    for same_name_paths in by_stem.values():
        ordered = sorted(same_name_paths, key=lambda path: path.name.casefold())
        raws = [path for path in ordered if path.suffix.lower() == ".dng"]
        jpegs = [path for path in ordered if path.suffix.lower() in JPEG_EXTENSIONS]
        paired_members = tuple(raws + jpegs)
        if raws and jpegs:
            primary = jpegs[0]
            primary_id = str(primary.resolve())
            key = "pair|" + str(primary.parent.resolve()).casefold() + "|" + primary.stem.casefold()
            result.append(
                PhotoGroup(key=key, primary=primary, primary_id=primary_id, members=paired_members)
            )
            paired_paths = set(paired_members)
            for path in ordered:
                if path not in paired_paths:
                    result.append(_single_group(path))
        else:
            for path in ordered:
                result.append(_single_group(path))

    return sorted(result, key=lambda item: item.primary.name.casefold())


def _single_group(path: Path) -> PhotoGroup:
    resolved = str(path.resolve())
    return PhotoGroup(key=resolved, primary=path, primary_id=resolved, members=(path,))


def scan_photo_paths(folder: Path) -> list[Path]:
    """List supported photos with one directory enumeration pass.

    ``Path.iterdir()`` followed by ``Path.is_file()`` can issue an additional
    stat call for every entry. ``os.scandir`` keeps the directory metadata
    returned by Windows and is cheaper for folders containing many files.
    """
    paths: list[Path] = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if not entry.is_file():
                continue
            if Path(entry.name).suffix.casefold() in SUPPORTED_EXTENSIONS:
                paths.append(Path(entry.path))
    return sorted(paths, key=lambda path: path.name.casefold())


def filter_visible_items(
    all_items: list[PhotoGroup],
    kept: set[str],
    show_kept_only: bool,
) -> list[PhotoGroup]:
    if not show_kept_only:
        return list(all_items)
    return [item for item in all_items if item.key not in kept]
