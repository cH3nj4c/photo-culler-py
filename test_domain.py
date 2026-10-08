"""Domain-logic tests for the subfolder tree.

Pure: no Qt and no window, so this runs anywhere and in well under a second.
The tree feeds the right sidebar's Explorer-style navigation pane, so what
matters is that the counts and the structure are right — a wrong total would
mislabel every folder in the panel.

Run:  python test_domain.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import domain  # noqa: E402

ROOT = Path("C:/shoot")

# --- 1. structure, and direct vs subtree counts ------------------------------
paths = [
    ROOT / "a.jpg",
    ROOT / "b.jpg",
    ROOT / "portraits" / "p1.jpg",
    ROOT / "portraits" / "p2.jpg",
    ROOT / "portraits" / "p3.jpg",
    ROOT / "portraits" / "raw" / "r1.dng",
    ROOT / "landscape" / "l1.jpg",
    ROOT / "landscape" / "wide" / "deep" / "w1.jpg",
]
tree = domain.build_folder_tree(ROOT, paths)

assert tree.name == "shoot", tree.name
assert tree.rel_path == "", tree.rel_path
# Two photos sit directly here; eight are in the subtree.
assert tree.direct_count == 2, tree.direct_count
assert tree.total_count == 8, tree.total_count
# Children are sorted by name, case-insensitively.
assert [c.name for c in tree.children] == ["landscape", "portraits"], tree.children

landscape = tree.children[0]
portraits = tree.children[1]
assert (landscape.direct_count, landscape.total_count) == (1, 2), landscape
assert (portraits.direct_count, portraits.total_count) == (3, 4), portraits

# `wide` holds no photos itself but leads to `deep`, so it must survive as an
# intermediate node with direct=0 — dropping it would make `deep` unreachable.
wide = landscape.children[0]
deep = wide.children[0]
assert wide.name == "wide" and wide.direct_count == 0 and wide.total_count == 1
assert deep.name == "deep" and deep.direct_count == 1 and deep.total_count == 1
assert portraits.children[0].name == "raw"
assert portraits.children[0].rel_path == "portraits/raw"
print("[1] structure and direct/total counts OK")

# --- 2. the depth-ordering regression ---------------------------------------
# Top-level folders tie with the root under `key.count("/")`, so an earlier
# version processed a parent before its own children and produced wrong totals.
# Several top-level branches, each with children, is what exposes that.
wide_tree = domain.build_folder_tree(
    ROOT,
    [
        ROOT / "one" / "x.jpg",
        ROOT / "one" / "kid" / "y.jpg",
        ROOT / "two" / "z.jpg",
        ROOT / "two" / "kid" / "w.jpg",
        ROOT / "three" / "kid" / "deep" / "v.jpg",
    ],
)
assert wide_tree.total_count == 5, wide_tree.total_count
by_name = {c.name: c for c in wide_tree.children}
assert sorted(by_name) == ["one", "three", "two"], sorted(by_name)
assert {n: c.total_count for n, c in by_name.items()} == {
    "one": 2, "three": 1, "two": 2,
}, {n: c.total_count for n, c in by_name.items()}
# `three` keeps its photo one level deeper, so it has no direct photos of its own.
assert {n: c.direct_count for n, c in by_name.items()} == {
    "one": 1, "three": 0, "two": 1,
}, {n: c.direct_count for n, c in by_name.items()}
print("[2] siblings are assembled after their own children (depth ordering)")

# --- 3. paths outside the root are ignored ----------------------------------
mixed = domain.build_folder_tree(
    ROOT, [ROOT / "in.jpg", Path("C:/elsewhere/out.jpg"), Path("D:/other/x.jpg")]
)
assert mixed.total_count == 1, mixed.total_count
assert mixed.direct_count == 1
assert [c.rel_path for c in mixed.children] == [], mixed.children
print("[3] paths outside the root are ignored, not counted")

# --- 4. degenerate inputs ----------------------------------------------------
assert domain.build_folder_tree(ROOT, []).total_count == 0
assert domain.build_folder_tree(ROOT, []).children == ()
assert domain.count_photo_folders(domain.build_folder_tree(ROOT, [])) == 0
# A root that is itself a photo's parent, with no subfolders at all.
flat = domain.build_folder_tree(ROOT, [ROOT / "only.jpg"])
assert flat.total_count == 1 and not flat.has_children
print("[4] empty and flattened inputs behave")

# --- 5. path_for rebuilds the real directory --------------------------------
assert tree.path_for(ROOT) == ROOT
assert tree.path_for(ROOT).name == "shoot"
for node in tree.walk():
    if node.rel_path == "landscape/wide/deep":
        # The separator is always "/", so this must work on Windows too.
        assert node.path_for(Path("C:/shoot")) == Path("C:/shoot/landscape/wide/deep")
        break
else:
    raise AssertionError("deep node missing from walk()")
# walk() reaches every node, root first, depth first.
seen = [n.rel_path for n in tree.walk()]
assert seen[0] == ""
assert set(seen) == {
    "", "landscape", "landscape/wide", "landscape/wide/deep", "portraits", "portraits/raw",
}, seen
print("[5] path_for rebuilds directory paths; walk covers the tree")

# --- 6. count_photo_folders --------------------------------------------------
# root, portraits, portraits/raw, landscape, deep hold photos; `wide` does not.
assert domain.count_photo_folders(tree) == 5, domain.count_photo_folders(tree)
print("[6] count_photo_folders counts only directories holding photos")

# --- 7. a deep chain does not recurse away the stack -------------------------
deep_path = ROOT
for level in range(120):
    deep_path = deep_path / f"d{level}"
chain = domain.build_folder_tree(ROOT, [deep_path / "x.jpg"])
assert chain.total_count == 1
assert chain.children[0].name == "d0"
print("[7] a 120-level chain builds without recursion")

print("DOMAIN TEST PASSED")
