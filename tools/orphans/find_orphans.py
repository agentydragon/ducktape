"""Find git-tracked files that are not inputs to any Bazel target.

Also reports whitelist entries that are no longer needed (dead patterns).

Usage:
    bazel run //tools/orphans:find_orphans           # List orphans + dead whitelist entries
    bazel run //tools/orphans:find_orphans -- --check  # Fail if orphans or dead entries exist

Covers files referenced via labels(srcs/data), plus files auto-discovered by
rules that don't use explicit srcs (helm_chart via helm_package rule kind).
"""

import argparse
import subprocess
import sys
from pathlib import Path

import pathspec
import pygit2

from bazel_util.workspace import get_build_workspace_directory


def label_to_path(label: str) -> Path | None:
    """Convert Bazel label to file path.

    //pkg:path/to/file.py -> pkg/path/to/file.py
    //:file.py -> file.py

    Returns None for non-file labels (external deps, target names).
    """
    if label.startswith("@") or ":" not in label:
        return None

    label = label.removeprefix("//")
    pkg, file = label.split(":", 1)

    # Skip target names (no extension, starts with underscore)
    if file.startswith("_") and "." not in file:
        return None

    return Path(pkg) / file if pkg else Path(file)


def query_bazel_files(repo_root: Path) -> set[Path]:
    """Query all files referenced in Bazel srcs and data attributes."""
    result = subprocess.run(
        ["bazel", "query", "labels(srcs, //...) union labels(data, //...)"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=False,
    )
    if result.returncode != 0:
        print(f"Warning: bazel query failed: {result.stderr}", file=sys.stderr)
        return set()

    paths = set()
    for label in result.stdout.strip().split("\n"):
        if label and (path := label_to_path(label)):
            paths.add(path)
    return paths


def query_helm_chart_files(repo_root: Path, git_files: set[Path]) -> set[Path]:
    """Find git-tracked files inside helm chart packages.

    helm_chart (helm_package) auto-discovers Chart.yaml, values.yaml, and
    templates/ — these don't appear in labels(srcs, //...).  We query for
    helm_package targets, derive their package directories, and claim all
    git-tracked files under those directories.
    """
    result = subprocess.run(
        ["bazel", "query", 'kind("helm_package", //...)'], capture_output=True, text=True, cwd=repo_root, check=False
    )
    if result.returncode != 0 or not result.stdout.strip():
        return set()

    chart_dirs: list[Path] = []
    for label in result.stdout.strip().split("\n"):
        if not label or not label.startswith("//"):
            continue
        # //cluster/charts/attic:attic -> cluster/charts/attic
        pkg = label.removeprefix("//").split(":")[0]
        if pkg:
            chart_dirs.append(Path(pkg))

    covered = set()
    for git_file in git_files:
        for chart_dir in chart_dirs:
            if git_file == chart_dir or str(git_file).startswith(str(chart_dir) + "/"):
                covered.add(git_file)
                break
    return covered


def get_git_files(repo_root: Path) -> set[Path]:
    """Get all git-tracked files."""
    repo = pygit2.Repository(repo_root)
    index = repo.index
    index.read()
    return {Path(entry.path) for entry in index}


def unused_whitelist_patterns(raw_orphans: set[Path], whitelist_lines: list[str]) -> list[str]:
    """Return whitelist lines that suppress no file in *raw_orphans*.

    A line is considered "unused" when it is a non-comment, non-blank pattern
    that matches none of the provided raw-orphan paths.  Such entries are dead
    weight — either the files they referenced were deleted, or they are now
    fully covered by Bazel targets and no longer appear as orphans.
    """
    unused: list[str] = []
    for line in whitelist_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        single_spec = pathspec.PathSpec.from_lines("gitwildmatch", [stripped])
        if not any(single_spec.match_file(p) for p in raw_orphans):
            unused.append(line)
    return unused


def run_report(repo_root: Path, whitelist_path: Path) -> tuple[list[Path], list[str]]:
    """Return (orphaned_files, unused_whitelist_lines), querying Bazel once.

    Each non-blank, non-comment whitelist pattern is parsed exactly once and
    reused for both orphan filtering and dead-entry detection.
    """
    git_files = get_git_files(repo_root)
    bazel_files = query_bazel_files(repo_root)
    helm_files = query_helm_chart_files(repo_root, git_files)

    raw_orphans = git_files - bazel_files - helm_files

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
