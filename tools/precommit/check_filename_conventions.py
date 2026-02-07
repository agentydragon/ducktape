"""Check that new .py/.md files and directories use underscores, not dashes."""

from __future__ import annotations

from pathlib import PurePosixPath

import pygit2


def check_filename_conventions(repo: pygit2.Repository) -> list[str]:
    """Check newly staged files for filename convention violations.

    Only flags files newly added to the index (not in HEAD),
    so existing files with dashes are grandfathered in.
    """
    try:
        head_tree = repo.head.peel(pygit2.Tree)
    except pygit2.GitError:
        head_tree = None  # No HEAD yet (initial commit)

    # Use index.diff_to_tree(HEAD) instead of repo.status().
    # repo.status() diffs the working tree against the index, triggering ~160k
    # syscalls in libgit2 (stat/readlink/access per file). On 9p filesystems
    # this takes ~12s. diff_to_tree only compares the index to HEAD (~0.003s).
    repo.index.read()
    if head_tree is not None:
        diff = repo.index.diff_to_tree(head_tree)
        new_files = [delta.new_file.path for delta in diff.deltas if delta.status == pygit2.GIT_DELTA_ADDED]
    else:
        new_files = [entry.path for entry in repo.index]
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
