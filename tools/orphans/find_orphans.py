"""Find git-tracked files that are not inputs to any Bazel target.

Also reports whitelist entries that are no longer needed (dead patterns).

Usage:
    bazel run //tools/orphans:find_orphans           # List orphans + dead whitelist entries
    bazel run //tools/orphans:find_orphans -- --check  # Fail if orphans or dead entries exist
"""

import argparse
import sys
from pathlib import Path

import pathspec
import pygit2

from bazel_util.query import run_query
from bazel_util.workspace import get_build_workspace_directory


def query_bazel_files(repo_root: Path) -> set[Path]:
    """Query all source files covered by Bazel targets.

    Covers explicit srcs/data attributes plus helm_chart (helm_package) targets
    whose files are auto-discovered and don't appear in labels(srcs, //...).
    Both sets are retrieved in a single bazel query invocation.
    """
    expr = (
        "kind('source file',  labels(srcs, //...) union labels(data, //...)  union deps(kind('helm_package', //...)))"
    )
    labels = run_query(expr, cwd=repo_root)
    return {p for label in labels if (p := label.path)}


def get_git_files(repo_root: Path) -> set[Path]:
    """Get all git-tracked files."""
    repo = pygit2.Repository(repo_root)
    index = repo.index
    index.read()
    return {Path(entry.path) for entry in index}


def run_report(repo_root: Path, whitelist_path: Path) -> tuple[list[Path], list[str]]:
    """Return (orphaned_files, unused_whitelist_lines), querying Bazel once.

    Each non-blank, non-comment whitelist pattern is parsed exactly once and
    reused for both orphan filtering and dead-entry detection.
    """
    git_files = get_git_files(repo_root)
    bazel_files = query_bazel_files(repo_root)

    raw_orphans = git_files - bazel_files

    whitelist_lines = whitelist_path.read_text().splitlines()

    # Parse each pattern once; reuse for both filtering and dead-entry detection.
    patterns: list[tuple[str, pathspec.PathSpec]] = []
    for line in whitelist_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            patterns.append((line, pathspec.PathSpec.from_lines("gitwildmatch", [stripped])))

    def _is_whitelisted(path: Path) -> bool:
        return any(spec.match_file(path) for _, spec in patterns)

    orphans = sorted(p for p in raw_orphans if not _is_whitelisted(p))
    unused = [line for line, spec in patterns if not any(spec.match_file(p) for p in raw_orphans)]
    return orphans, unused


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--whitelist", type=Path, default=None, help="Path to whitelist file (default: tools/orphans/whitelist.txt)"
    )
    parser.add_argument(
        "--check", action="store_true", help="Exit with code 1 if any orphans or dead whitelist entries exist"
    )
    args = parser.parse_args()

    repo_root = get_build_workspace_directory()
    whitelist_path = args.whitelist or repo_root / "tools/orphans/whitelist.txt"

    orphans, unused = run_report(repo_root, whitelist_path)

    for orphan in orphans:
        print(orphan)

    if unused:
        print("\nDead whitelist entries (suppress no orphan):")
        for pattern in unused:
            print(f"  {pattern}")

    if args.check and (orphans or unused):
        counts = []
        if orphans:
            counts.append(f"{len(orphans)} orphaned file(s)")
        if unused:
            counts.append(f"{len(unused)} dead whitelist entry/entries")
        print(f"\n{' and '.join(counts)} found", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
