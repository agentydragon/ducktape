#!/usr/bin/env python3
"""Find git-tracked files that are not inputs to any Bazel target.

Usage:
    bazel run //tools/orphans:find_orphans           # List orphans
    bazel run //tools/orphans:find_orphans -- --check  # Fail if orphans exist
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pathspec
import pygit2


def get_repo_root() -> Path:
    """Get repository root, handling both direct and bazel run invocations."""
    # BUILD_WORKSPACE_DIRECTORY is set by `bazel run`
    if workspace := os.environ.get("BUILD_WORKSPACE_DIRECTORY"):
        return Path(workspace)
    # Fallback for direct invocation
    return Path(__file__).parent.parent.parent


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


def get_git_files(repo_root: Path) -> set[Path]:
    """Get all git-tracked files."""
    repo = pygit2.Repository(repo_root)
    index = repo.index
    index.read()
    return {Path(entry.path) for entry in index}


def load_whitelist(whitelist_path: Path) -> pathspec.PathSpec:
    """Load whitelist patterns from file."""
    lines = whitelist_path.read_text().splitlines()
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def find_orphans(repo_root: Path, whitelist_path: Path) -> list[Path]:
    """Find git files not in any Bazel target, excluding whitelisted patterns."""
    git_files = get_git_files(repo_root)
    bazel_files = query_bazel_files(repo_root)
    whitelist = load_whitelist(whitelist_path)

    orphans = git_files - bazel_files
    # Filter out whitelisted patterns
    orphans = {p for p in orphans if not whitelist.match_file(str(p))}

    return sorted(orphans)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--whitelist", type=Path, default=None, help="Path to whitelist file (default: tools/orphans/whitelist.txt)"
    )
    parser.add_argument("--check", action="store_true", help="Exit with code 1 if any orphans found")
    args = parser.parse_args()

    repo_root = get_repo_root()
    whitelist_path = args.whitelist or repo_root / "tools/orphans/whitelist.txt"

    orphans = find_orphans(repo_root, whitelist_path)

    for orphan in orphans:
        print(orphan)

    if args.check and orphans:
        print(f"\n{len(orphans)} orphaned files found", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
