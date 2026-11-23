"""Find orphaned files and py_library targets.

Detects two kinds of orphans:
1. Git-tracked files not found in the selected Bazel source-input attributes
   or dependency closures.
2. py_library targets not transitively depended on by any entry point
   (py_binary, py_test, oci_image, etc.).

Also reports whitelist entries that are no longer needed (dead patterns),
and for each pattern shows how many matching files are covered by Bazel.

Usage:
    bb run //devinfra/orphans:find_orphans_bin           # List orphans + dead whitelist entries
    bb run //devinfra/orphans:find_orphans_bin -- --check  # Fail if orphans or dead entries exist
"""

import argparse
import dataclasses
import sys
from collections.abc import Iterable
from pathlib import Path
from textwrap import dedent

import pathspec
import pygit2

from devinfra.bazel.workspace import BazelLabel, BazelWorkspace, detect_bazel_backend, get_build_workspace_directory


@dataclasses.dataclass
class PatternStats:
    """Per-pattern match counts against the current git and Bazel file sets."""

    pattern: str
    bazel_covered: int  # git files also in Bazel matched by this pattern
    orphaned: int  # git orphans (not in Bazel) matched by this pattern


_ENTRY_POINT_KINDS = [
    "aspect_py_binary",
    "go_binary",
    "js_binary",
    "native_test",
    "oci_image",
    "py_binary",
    "py_test",
    "py_wheel",
    "rust_binary",
]


def query_orphan_py_libraries(workspace: BazelWorkspace, *, keep_going: bool = False) -> set[BazelLabel]:
    """Find py_library targets not transitively depended on by any entry point."""
    kinds = "|".join(_ENTRY_POINT_KINDS)
    expr = dedent(f"""\
        let entry_points = kind("{kinds}", //...)
        in kind("py_library", //...) except deps($entry_points)
    """)
    return set(workspace.query(expr, keep_going=keep_going))


def query_bazel_files(workspace: BazelWorkspace, *, keep_going: bool = False) -> set[Path]:
    """Query all source files covered by Bazel targets.

    Uses srcs/data directly and traverses the entry_point and compile_data
    inputs, which may refer to filegroups or generated targets. It also
    traverses create_data_blob dependencies for its issue_files attribute.
    Other nonstandard attributes and nested workspaces may still be missed.
    All sets are retrieved in a single Bazel query invocation.
    """
    expr = (
        "kind('source file',"
        "  labels(srcs, //...)"
        "  union labels(data, //...)"
        "  union deps(labels(entry_point, //...) union labels(compile_data, //...))"
        "  union deps(kind('create_data_blob', //...))"
        ")"
    )
    labels = workspace.query(expr, keep_going=keep_going)
    return {p for label in labels if (p := label.path)}


def get_git_files(repo_root: Path) -> set[Path]:
    """Get all git-tracked files."""
    repo = pygit2.Repository(repo_root)
    index = repo.index
    index.read()
    return {Path(entry.path) for entry in index}


def classify_files(
    git_files: set[Path], bazel_files: set[Path], whitelist_lines: Iterable[str]
) -> tuple[list[Path], list[PatternStats]]:
    """Count each pattern independently, including files matched by overlapping patterns."""
    unlisted = git_files - bazel_files
    stats = []
    for line in whitelist_lines:
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        spec = pathspec.PathSpec.from_lines("gitwildmatch", [pattern])
        matches = {path for path in git_files if spec.match_file(path)}
        covered = len(matches & bazel_files)
        stats.append(PatternStats(pattern=pattern, bazel_covered=covered, orphaned=len(matches) - covered))
        unlisted.difference_update(matches)
    return sorted(unlisted), stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--whitelist", type=Path, default=None, help="Path to whitelist file (default: devinfra/orphans/whitelist.txt)"
    )
    parser.add_argument(
        "--check", action="store_true", help="Exit with code 1 if any orphans or dead whitelist entries exist"
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help=(
            "Continue past Bazel query errors (e.g. broken external deps); partial results may misclassify "
            "orphans and whitelist entries"
        ),
    )
    args = parser.parse_args()

    repo_root = get_build_workspace_directory()
    workspace = BazelWorkspace(root=repo_root, backend=detect_bazel_backend())
    whitelist_path = args.whitelist or repo_root / "devinfra/orphans/whitelist.txt"

    orphans, stats = classify_files(
        get_git_files(repo_root),
        query_bazel_files(workspace, keep_going=args.keep_going),
        whitelist_path.read_text().splitlines(),
    )
    orphan_libs = query_orphan_py_libraries(workspace, keep_going=args.keep_going)

    for orphan in orphans:
        print(orphan)

    unused = [s for s in stats if s.orphaned == 0]
    if unused:
        print("\nDead whitelist entries (suppress no orphan):")
        for s in unused:
            if s.bazel_covered:
                print(f"  {s.pattern}  ({s.bazel_covered} covered by Bazel, 0 orphaned)")
            else:
                print(f"  {s.pattern}  (no git files match)")

    mixed = [s for s in stats if s.bazel_covered > 0 and s.orphaned > 0]
    if mixed:
        print("\nPatterns with Bazel-covered files:")
        for s in mixed:
            total = s.bazel_covered + s.orphaned
            print(f"  {s.pattern}: {s.bazel_covered}/{total} in Bazel, {s.orphaned} orphaned")

    if orphan_libs:
        print(f"\nOrphaned py_library targets ({len(orphan_libs)}):")
        for lib in sorted(orphan_libs, key=str):
            print(f"  {lib}")

    if args.check and (orphans or unused or orphan_libs):
        counts = []
        if orphans:
            counts.append(f"{len(orphans)} orphaned file(s)")
        if unused:
            counts.append(f"{len(unused)} dead whitelist entry/entries")
        if orphan_libs:
            counts.append(f"{len(orphan_libs)} orphaned py_library target(s)")
        print(f"\n{' and '.join(counts)} found", file=sys.stderr)
        # The whole point of --check: the caller is a shell gate wanting a nonzero
        # status when the report is non-empty. Without it the flag does nothing.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
