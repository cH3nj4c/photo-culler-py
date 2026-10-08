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

    # ``Path.resolve()`` is a real filesystem call on Windows (~1 ms per file
    # here), and this runs for every photo in the folder. Calling it per photo
    # made opening a 3000-file folder freeze the UI for ~2 s. Resolving the
    # *directory* once and appending the file name produces the same string for
    # regular files, so resolve one directory per unique parent instead.
    resolved_dirs: dict[Path, str] = {}

    def resolved_dir(path: Path) -> str:
        parent = path.parent
        base = resolved_dirs.get(parent)
        if base is None:
            base = str(parent.resolve())
            resolved_dirs[parent] = base
        return base

    def resolved_id(path: Path) -> str:
        return str(Path(resolved_dir(path)) / path.name)

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
            primary_id = resolved_id(primary)
            key = "pair|" + resolved_dir(primary).casefold() + "|" + primary.stem.casefold()
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
                    result.append(
                        _single_group(path, mtime_of(path), resolved_id(path))
                    )
        else:
            for path in ordered:
                result.append(_single_group(path, mtime_of(path), resolved_id(path)))

    return sorted(result, key=lambda item: item.primary.name.casefold())


@dataclass(frozen=True)
class FolderNode:
    """One directory in the scanned tree, with the photos it holds.

    ``rel_path`` is the path relative to the scan root, using ``/`` as the
    separator on every platform so it can be used as a dict key and rebuilt with
    :meth:`path_for`. It is ``""`` for the root itself.
    """

    name: str
    rel_path: str
    direct_count: int
    total_count: int
    children: tuple["FolderNode", ...] = ()

    @property
    def has_children(self) -> bool:
        return bool(self.children)

    def path_for(self, root: Path) -> Path:
        """The absolute directory this node stands for."""
        if not self.rel_path:
            return Path(root)
        return Path(root).joinpath(*self.rel_path.split("/"))

    def walk(self):
        """Yield this node and every descendant, depth first."""
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))


def build_folder_tree(root: Path, paths) -> FolderNode:
    """Nest scanned photo paths into a directory tree rooted at *root*.

    Only directories that lead to at least one photo appear, which is what makes
    this cheap: the tree is derived from the scan result already in hand, with
    no second directory enumeration. Intermediate directories are kept so the
    result stays navigable, and ``direct_count`` distinguishes photos sitting in
    a directory from those in its subtree.

    Paths outside *root* are ignored rather than raising: the scanner should not
    produce them, and a tree is not the place to discover that.
    """
    root = Path(root)
    names: dict[str, str] = {"": root.name or str(root)}
    direct: dict[str, int] = {"": 0}
    children: dict[str, set[str]] = {"": set()}

    for path in paths:
        try:
            relative = Path(path).parent.relative_to(root)
        except ValueError:
            continue
        key = ""
        for part in relative.parts:
            if part in ("", "."):
                continue
            child_key = f"{key}/{part}" if key else part
            names.setdefault(child_key, part)
            direct.setdefault(child_key, 0)
            children.setdefault(child_key, set())
            children[key].add(child_key)
            key = child_key
        direct[key] += 1

    # Roll the per-directory counts up into subtree totals, and assemble the
    # nodes, deepest first. Depth must be counted with +1 for non-empty keys:
    # `key.count("/")` alone gives 0 for both the root *and* every top-level
    # folder, which would let a parent be processed before its own children.
    def _depth(key: str) -> int:
        return key.count("/") + 1 if key else 0

    by_depth = sorted(direct, key=_depth, reverse=True)

    totals = dict(direct)
    for key in by_depth:
        if not key:
            continue
        parent = key.rsplit("/", 1)[0] if "/" in key else ""
        totals[parent] = totals.get(parent, 0) + totals[key]

    built: dict[str, FolderNode] = {}
    for key in by_depth:
        ordered = sorted(children.get(key, ()), key=lambda k: names[k].casefold())
        built[key] = FolderNode(
            name=names[key],
            rel_path=key,
            direct_count=direct[key],
            total_count=totals[key],
            children=tuple(built[child] for child in ordered),
        )
    return built[""]


def count_photo_folders(node: FolderNode) -> int:
    """How many directories in *node*'s subtree hold photos of their own."""
    return sum(1 for item in node.walk() if item.direct_count > 0)


def _single_group(
    path: Path, mtime_ns: int = 0, path_id: str | None = None
) -> PhotoGroup:
    # ``path_id`` lets build_photo_groups share one resolve() per directory;
    # falling back to resolve() here keeps this usable on its own.
    resolved = path_id if path_id is not None else str(path.resolve())
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
