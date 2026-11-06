"""Helper functions for pygit2-based git operations in tests.

This module provides pygit2 wrappers that replace subprocess git calls in tests,
improving performance and reducing subprocess overhead.
"""

from pathlib import Path

import pygit2

from x.wt.testing.data import TestData


def add_and_commit(
    repo_path: Path,
    files: dict[str, str],
    message: str,
    *,
    author_name: str | None = None,
    author_email: str | None = None,
) -> pygit2.Oid:
    """Stage files and commit them with pygit2, using test author defaults when omitted."""
    repo = pygit2.Repository(repo_path)

    # Write and stage files
    for filename, content in files.items():
        file_path = repo_path / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        repo.index.add(filename)

    repo.index.write()

    tree = repo.index.write_tree()
    signature = pygit2.Signature(author_name or TestData.Git.USER_NAME, author_email or TestData.Git.USER_EMAIL)
    parents = [repo.head.target] if not repo.head_is_unborn else []

    return repo.create_commit("HEAD", signature, signature, message, tree, parents)


def worktree_exists(repo: pygit2.Repository, worktree_path: Path) -> bool:
    """Return whether pygit2 lists a worktree at the given path."""
    for wt_name in repo.list_worktrees():
        wt = repo.lookup_worktree(wt_name)
        if Path(wt.path) == worktree_path:
            return True
    return False


def add_worktree(repo: pygit2.Repository, worktree_path: Path, branch: str) -> None:
    """Add a worktree using pygit2.

    Note: This performs a checkout (unlike git worktree add --no-checkout).
    For no-checkout worktrees, use subprocess or git_run.
    """
    branch_ref = repo.lookup_branch(branch)
    if branch_ref is None:
        commit = repo.head.peel(pygit2.Commit)
        branch_ref = repo.branches.local.create(branch, commit)

    worktree_name = worktree_path.name
    repo.add_worktree(worktree_name, worktree_path, branch_ref)
