"""Repository hygiene guard: no unresolved conflict markers may be committed.

This exists because they *were*: on 2026-09-17 a merge of two copies of the same
commit conflicted, and the conflicted files were committed with the markers
intact (`3a6f7ce`). The result was not subtle but it was easy to miss — two test
files no longer parsed (`SyntaxError: invalid decimal literal`, pointing at the
`>>>>>>>` line) and `PhotoCuller.spec` could not be built at all.

Nothing in the test suite noticed, because the markers were in *other* files
than the ones a given test imports. A repository-wide scan catches it wherever
it lands.

The detection is deliberately narrow. A bare `=======` line is legitimate
Markdown (a level-1 heading underline), so the presence of one is never enough
on its own — a file is only flagged when it contains the paired
``<<<<<<<`` / ``>>>>>>>`` markers that only a real conflict produces.

Run:  python test_repo_hygiene.py
"""

import re
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent

# A conflict block opens with `<<<<<<< ` (ours, usually followed by a ref name)
# and closes with `>>>>>>> `. Requiring the pair avoids false positives.
#
# re.MULTILINE is passed at COMPILE time on purpose. `Pattern.search(s, flags)`
# does not exist — the second positional argument of `search` is `pos`, so
# `compiled.search(text, re.M)` silently searches from offset 8 with MULTILINE
# off, and `^` then never matches. It raises nothing, it just never fires.
_OPEN_RE = re.compile(r"^<{7}(\s|$)", re.M)
_CLOSE_RE = re.compile(r"^>{7}(\s|$)", re.M)
# diff3 style adds a base section; this one never appears in real content.
_BASE_RE = re.compile(r"^\|{7}(\s|$)", re.M)
_SEP_RE = re.compile(r"^={7}$", re.M)

TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".spec", ".bat", ".iss", ".json", ".toml", ".cfg",
    ".ini", ".yml", ".yaml", ".gitignore", ".gitattributes",
}


def _git_z(*args: str) -> list[str]:
    """Run git with NUL-separated output and return the raw fields.

    `-z` matters: without it git *quotes* paths that contain spaces or non-ASCII
    characters (`"Photo Culler-\\345\\256\\236\\347\\216\\260...md"`), so a
    lookup by that literal string silently finds nothing — a file the guard is
    meant to protect becomes invisible to it. `-z` emits the real bytes with no
    quoting.
    """
    result = subprocess.run(
        ["git", *args, "-z"], capture_output=True, cwd=str(PROJECT)
    )
    if result.returncode != 0:
        raise SystemExit(
            f"git {' '.join(args)} failed: "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    text = result.stdout.decode("utf-8", "surrogateescape")
    return [field for field in text.split("\0") if field]


def tracked_files() -> list[str]:
    return _git_z("ls-files")


def scan_conflict_markers(paths, root: Path = PROJECT) -> dict[str, list[int]]:
    """Return {path: [line numbers]} for files holding a real conflict block.

    ``root`` exists so the scanner can be pointed at any checkout — that is how
    it gets verified against a commit that really did contain markers, instead
    of only against synthetic strings.
    """
    found: dict[str, list[int]] = {}
    for rel in paths:
        path = Path(root) / rel
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable: not a place markers matter
        if not (_OPEN_RE.search(text) and _CLOSE_RE.search(text)):
            continue
        lines = [
            i
            for i, line in enumerate(text.splitlines(), 1)
            if _OPEN_RE.match(line) or _CLOSE_RE.match(line)
            or _BASE_RE.match(line) or _SEP_RE.match(line)
        ]
        found[str(rel)] = lines
    return found




# --- 1. no conflict markers anywhere in the tracked tree ---------------------
tracked = tracked_files()
assert tracked, "git ls-files returned nothing — is this a git checkout?"
offenders = scan_conflict_markers(tracked)
if offenders:
    print("Unresolved conflict markers are committed in these files:")
    for rel, lines in sorted(offenders.items()):
        print(f"  {rel}: line(s) {lines}")
    print()
    print("Resolve them (keep the correct side, delete the marker lines) before")
    print("committing. Do not commit a merge with conflicts outstanding.")
    sys.exit(1)
print(f"[1] no conflict markers in {len(tracked)} tracked file(s)")

# --- 2. the index holds no unresolved (unmerged) entries ---------------------
# `git ls-files -u` lists stage 1/2/3 entries, which only exist mid-conflict.
unmerged = _git_z("ls-files", "-u")
if unmerged:
    # Entries look like `100644 <sha> 1\t<path>`; the stage number is the field
    # before the tab and the path follows it.
    paths = sorted({entry.split("\t", 1)[-1] for entry in unmerged})
    print(f"Index has unresolved conflict entries: {paths}")
    sys.exit(1)
print("[2] the index has no unmerged entries")

# --- 3. the scanner actually detects a real conflict block -------------------
# A guard that cannot fire is worse than no guard, so prove it on a synthetic
# buffer rather than trusting the regexes by eye.
#
# The markers are BUILT here rather than written literally: a file that contains
# a real `<<<<<<<` … `>>>>>>>` pair is itself a flagged file, so this test would
# fail on itself the moment it was committed.
OPEN = "<" * 7
CLOSE = ">" * 7
SEP = "=" * 7
BASE = "|" * 7

sample = "\n".join(
    [
        "def f():",
        f"{OPEN} HEAD",
        "    return 1",
        SEP,
        "    return 2",
        f"{CLOSE} deadbeef",
        "    return 3",
    ]
)
assert _OPEN_RE.search(sample), "scanner missed the opening marker"
assert _CLOSE_RE.search(sample), "scanner missed the closing marker"
assert sum(1 for line in sample.splitlines() if _SEP_RE.match(line)) == 1
# diff3 style, which git emits under merge.conflictStyle=diff3
diff3 = "\n".join([f"{OPEN} HEAD", "a", f"{BASE} base", "b", SEP, "c", f"{CLOSE} x"])
assert _BASE_RE.search(diff3), "scanner missed the diff3 base marker"

# ...and it must NOT fire on the Markdown heading underline that looks similar.
assert not _SEP_RE.match("=" * 5), "five equals must not read as a marker"
assert not _SEP_RE.match(SEP + "="), "eight equals must not read as a marker"
assert not (_OPEN_RE.search("Title\n" + SEP + "\n\nBody.\n"))
# A bare separator with no paired markers is not a conflict.
assert not _OPEN_RE.search("Heading\n" + SEP + "\n")
assert not _CLOSE_RE.search("Heading\n" + SEP + "\n")
# A quoted marker with trailing text but no pair must not trip it either.
assert not (_OPEN_RE.search(f"{OPEN} HEAD\n") and _CLOSE_RE.search(f"{OPEN} HEAD\n"))
print("[3] scanner fires on real conflicts (incl. diff3), ignores markdown underlines")

# --- 3b. this guard must not flag itself -------------------------------------
# Cheap insurance against the self-reference trap described above.
assert not scan_conflict_markers([Path(__file__).name]), (
    "this test file contains literal conflict markers and would flag itself"
)
print("[3b] the guard does not flag its own source")

# --- 4. the project's own files parse ----------------------------------------
# Cheap extra net: a syntax error in any module means something was committed
# broken, whatever the cause.
import ast  # noqa: E402

checked = 0
for rel in tracked:
    if not rel.endswith(".py"):
        continue
    try:
        ast.parse((PROJECT / rel).read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError) as exc:
        print(f"{rel} does not parse: {exc}")
        sys.exit(1)
    checked += 1
print(f"[4] all {checked} tracked Python files parse")

print("REPO HYGIENE TEST PASSED")
