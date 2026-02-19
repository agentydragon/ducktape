"""Find git-tracked files that are not inputs to any Bazel target.

Also reports whitelist entries that are no longer needed (dead patterns),
and for each pattern shows how many matching files are covered by Bazel.

Usage:
    bazel run //tools/orphans:find_orphans           # List orphans + dead whitelist entries
    bazel run //tools/orphans:find_orphans -- --check  # Fail if orphans or dead entries exist
"""

import argparse
import dataclasses
import sys
from pathlib import Path

import pathspec
import pygit2

from bazel_util.query import run_query
from bazel_util.workspace import get_build_workspace_directory


@dataclasses.dataclass
class PatternStats:
    """Per-pattern match counts against the current git and Bazel file sets."""

    pattern: str
    bazel_covered: int  # git files also in Bazel matched by this pattern
    orphaned: int  # git orphans (not in Bazel) matched by this pattern


def query_bazel_files(repo_root: Path) -> set[Path]:
    """Query all source files covered by Bazel targets.

    Uses labels(srcs/data, //...) for standard attributes, and adds full
    deps() traversal for rules that hold source files in non-standard
    attributes (helm_package auto-discovers chart files; create_data_blob
    uses issue_files rather than srcs/data for specimen YAML).
    All sets are retrieved in a single bazel query invocation.
    """
    expr = (
        "kind('source file',"
        "  labels(srcs, //...)"
        "  union labels(data, //...)"
        "  union deps(kind('helm_package', //...))"
        "  union deps(kind('create_data_blob', //...))"
        ")"
    )
    labels = run_query(expr, cwd=repo_root)
    return {p for label in labels if (p := label.path)}


def get_git_files(repo_root: Path) -> set[Path]:
    """Get all git-tracked files."""
    repo = pygit2.Repository(repo_root)
    index = repo.index
    index.read()
    return {Path(entry.path) for entry in index}


def run_report(repo_root: Path, whitelist_path: Path) -> tuple[list[Path], list[PatternStats]]:
    """Return (orphaned_files, per_pattern_stats), querying Bazel once.

    Each non-blank, non-comment whitelist pattern is parsed exactly once and
    reused for orphan filtering, dead-entry detection, and Bazel-coverage stats.

    A pattern is "dead" (orphaned == 0) when it suppresses no orphaned files,
    either because all matching files are now in Bazel (bazel_covered > 0)
    or because the pattern matches no git-tracked files at all.
    """
    git_files = get_git_files(repo_root)
    bazel_files = query_bazel_files(repo_root)

    raw_orphans = git_files - bazel_files
    covered_files = git_files - raw_orphans  # files in both git and Bazel

    whitelist_lines = whitelist_path.read_text().splitlines()

    # Parse each pattern once; reuse for filtering and stats.
    specs: list[tuple[str, pathspec.PathSpec]] = []
    for line in whitelist_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            specs.append((stripped, pathspec.PathSpec.from_lines("gitwildmatch", [stripped])))

    def _is_whitelisted(path: Path) -> bool:
        return any(spec.match_file(path) for _, spec in specs)

    orphans = sorted(p for p in raw_orphans if not _is_whitelisted(p))
    stats = [
        PatternStats(
            pattern=pattern,
            orphaned=sum(spec.match_file(p) for p in raw_orphans),
            bazel_covered=sum(spec.match_file(p) for p in covered_files),
        )
        for pattern, spec in specs
    ]
    return orphans, stats


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

    orphans, stats = run_report(repo_root, whitelist_path)

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
