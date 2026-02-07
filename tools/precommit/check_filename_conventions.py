"""Check that new .py/.md files and directories use underscores, not dashes."""

from __future__ import annotations

from pathlib import PurePosixPath

import pygit2


def check_filename_conventions(repo: pygit2.Repository) -> list[str]:
    """Check newly staged files for filename convention violations.

    Only flags files with GIT_STATUS_INDEX_NEW (newly added to the index),
    so existing files with dashes are grandfathered in.
    """
    new_files = [path for path, flags in repo.status().items() if flags & pygit2.GIT_STATUS_INDEX_NEW]
    if not new_files:
        return []

    try:
        head_tree = repo.head.peel(pygit2.Tree)
    except pygit2.GitError:
        head_tree = None  # No HEAD yet (initial commit)

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
