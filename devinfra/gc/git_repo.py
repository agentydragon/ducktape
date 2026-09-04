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


def patches_landed_in_main(repo: Path, head: str, main: str) -> bool:
    """True when every commit `head` adds since the merge base has an equivalent on `main`.

    Complements [`content_in_main`], which asks whether merging `head` would change `main`
    *today* — a question that decays as `main` advances. A branch squash- or rebase-merged
    long ago can conflict with a `main` that has since moved thousands of commits past the
    merge base, and then reads as unlanded even though its work is in. This asks the
    time-invariant question instead: did these patches land at some point after the merge
    base? `git cherry` answers it by patch id, the same equivalence `git rebase` uses to drop
    commits it has already applied.

    Weaker than `content_in_main` on purpose: it establishes that the work landed, not that
    it survives in `main`'s tree, so a later revert still reads as landed. For GC that is the
    right bar — the branch contributes no patch `main` has not already seen — but it is only
    ever an additional reason to prune, never a reason to keep.
    """
    outcome = git(repo, "cherry", main, head, check=False)
    if outcome.returncode != 0:
        return False
    lines = [line for line in outcome.stdout.splitlines() if line.strip()]
    # `-` marks a commit with an upstream equivalent, `+` one without. An empty listing means
    # nothing to land, which `content_in_main` already covers; require real evidence here.
    return bool(lines) and all(line.startswith("-") for line in lines)


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
