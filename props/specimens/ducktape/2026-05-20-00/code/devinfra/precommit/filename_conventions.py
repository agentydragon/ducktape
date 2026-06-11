"""Check that new .py/.md files and directories use underscores, not dashes."""

from __future__ import annotations

from pathlib import PurePosixPath

import pygit2
from pygit2.enums import DeltaStatus


def check_filename_conventions(deltas: list[pygit2.DiffDelta], head_tree: pygit2.Tree | None) -> list[str]:
    """Check newly added files for filename convention violations.

    Expects deltas with lint-ignored paths already filtered out.
    Only flags files newly added to the index (not modified/renamed),
    so existing files with dashes are grandfathered in.
    """
    new_files = [d.new_file.path for d in deltas if d.status == DeltaStatus.ADDED]
    if not new_files:
        return []

    violations: list[str] = []
    checked_dirs: set[str] = set()

    for filepath in sorted(new_files):
        path = PurePosixPath(filepath)

        if path.suffix in (".py", ".md") and "-" in path.stem:
            violations.append(f"{filepath}: filename '{path.name}' contains dash, use underscore")

        for parent in path.parents:
            dir_str = str(parent)
            if dir_str == "." or dir_str in checked_dirs:
                continue
            checked_dirs.add(dir_str)

            if "-" not in parent.name:
                continue

            # Only flag directories that don't exist in HEAD
            if head_tree is not None:
                try:
                    head_tree[dir_str]
                    continue
                except KeyError:
                    pass

            violations.append(f"{filepath}: new directory '{parent.name}' contains dash, use underscore")

    return violations
