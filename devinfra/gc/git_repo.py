"""Git repository queries shared across the workspace-gc scanners.

Repo-level plumbing — running git, enumerating worktrees, resolving the default branch — and
the content-in-main ancestry test, used by both the worktree and branch classifiers. It does
not belong to either domain, so it lives here rather than in `worktree_gc` or `branch_gc`.

Worktree enumeration uses `git worktree list --porcelain` (pygit2's `list_worktrees()` gives
only linked-worktree names, not the main one or branches). Content queries use pygit2.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pygit2


class GitError(RuntimeError):
    """A git invocation failed in a way that blocks classification."""


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", os.fspath(repo), *args], capture_output=True, text=True, check=check)


def git_out(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path
    branch: str | None  # None for a detached HEAD


def list_worktrees(repo: Path) -> list[Worktree]:
    """Every worktree of `repo` (main included), parsed from `git worktree list --porcelain`."""
    worktrees: list[Worktree] = []
    path: Path | None = None
    branch: str | None = None
    for line in [*git_out(repo, "worktree", "list", "--porcelain").splitlines(), ""]:
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ").removeprefix("refs/heads/")
        elif line == "" and path is not None:
            worktrees.append(Worktree(path=path, branch=branch))
            path, branch = None, None
    return worktrees


def main_ref(repo: Path) -> str:
    """The upstream default branch ref (e.g. `origin/devel`)."""
    ref = git_out(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if not ref:
        raise GitError("cannot determine the default branch (origin/HEAD is unset)")
    return ref


def default_branch_name(repo: Path) -> str:
    """The default branch's short name (e.g. `devel`), from `origin/HEAD`."""
    ref = main_ref(repo)  # e.g. "origin/devel"
    _, _, branch = ref.partition("/")
    return branch or ref


def main_worktree(repo: Path) -> Path:
    """The main (non-linked) worktree — its `.git` is the common dir's parent."""
    return Path(git_out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent


def content_in_main(pg: pygit2.Repository, head: pygit2.Oid, main: str) -> bool:
    """True when commit `head` adds nothing not already in `main`.

    Covers a direct merge (`head` is an ancestor of main), an empty branch (`head` is the
    merge base), and a squash/rebase-merge (merging `head` into main yields main's exact
    tree, so `head` contributed no net change).
    """
    main_commit = pg.revparse_single(main).peel(pygit2.Commit)
    main_oid = main_commit.id
    if pg.descendant_of(main_oid, head):  # main descends from HEAD, i.e. HEAD is in main
        return True
    base = pg.merge_base(main_oid, head)
    if base is None:
        return False
    if base == head:
        return True
    index = pg.merge_commits(main_oid, head)
    if index.conflicts is not None:  # real divergence
        return False
    return index.write_tree(pg) == main_commit.tree.id
