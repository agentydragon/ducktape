"""Find and safely remove local git worktrees whose work is already merged.

Mirrors output_base_gc's PRUNE/KEEP/REVIEW model. A worktree is prunable only when it is
clean (no tracked *or* untracked changes), idle (not the main checkout, not the invoking
worktree, no process cwd'd inside), and its work is already in the main branch —
established by git (HEAD is an ancestor of main; merging HEAD into main is a no-op, which
catches squash/rebase-merges; or the branch is empty) or by a merged GitHub PR for its
branch. Everything else is kept (dirty/active/live/open-PR) or reported for manual review
(clean but unique unmerged work, detached HEAD with unique commits, undeterminable main).

PR state is supplied as an injected mapping so this module stays git-only and dependency-
free; the CLI (workspace_gc) fills it in via PyGithub.
"""

from __future__ import annotations

import enum
import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


class PrState(enum.StrEnum):
    MERGED = "merged"
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class PrInfo:
    number: int
    state: PrState


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path
    branch: str | None  # None for a detached HEAD
    head: str


@dataclass(frozen=True, slots=True)
class PrunableWorktree:
    worktree: Worktree
    reason: str


@dataclass(frozen=True, slots=True)
class RetainedWorktree:
    worktree: Worktree
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewWorktree:
    worktree: Worktree
    reason: str


type Classification = PrunableWorktree | RetainedWorktree | ReviewWorktree


class GitError(RuntimeError):
    """A git invocation failed in a way that blocks classification."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", os.fspath(repo), *args], capture_output=True, text=True, check=check)


def _git_out(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.strip()


def _git_ok(repo: Path, *args: str) -> bool:
    return _git(repo, *args, check=False).returncode == 0


def list_worktrees(repo: Path) -> list[Worktree]:
    """Every worktree of `repo`, parsed from `git worktree list --porcelain`."""
    worktrees: list[Worktree] = []
    path: Path | None = None
    head = ""
    branch: str | None = None
    for line in [*_git_out(repo, "worktree", "list", "--porcelain").splitlines(), ""]:
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line.startswith("HEAD "):
            head = line.removeprefix("HEAD ")
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ").removeprefix("refs/heads/")
        elif line == "" and path is not None:
            worktrees.append(Worktree(path=path, branch=branch, head=head))
            path, head, branch = None, "", None
    return worktrees


def main_ref(repo: Path) -> str:
    """The upstream default branch ref (e.g. `origin/devel`)."""
    ref = _git_out(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if not ref:
        raise GitError("cannot determine the default branch (origin/HEAD is unset)")
    return ref


def main_worktree(repo: Path) -> Path:
    """The main (non-linked) worktree — its `.git` is the common dir's parent."""
    return Path(_git_out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent


def _dirty(path: Path) -> bool:
    # --porcelain lists tracked changes and untracked files; any line means "not clean".
    return bool(_git_out(path, "status", "--porcelain"))


def _content_in_main(repo: Path, head: str, main: str) -> bool:
    """True when `head` adds nothing not already in `main`.

    Covers a direct merge (HEAD is an ancestor of main), an empty branch (no commits past
    the merge base), and a squash/rebase-merge (merging HEAD into main yields main's exact
    tree, so HEAD contributed no net change).
    """
    if _git_ok(repo, "merge-base", "--is-ancestor", head, main):
        return True
    merge_base = _git_out(repo, "merge-base", main, head)
    if merge_base and _git_out(repo, "rev-list", "--count", f"{merge_base}..{head}") == "0":
        return True
    merged = _git(repo, "merge-tree", "--write-tree", main, head, check=False)
    if merged.returncode != 0:  # non-zero == merge conflict, i.e. real divergence
        return False
    merged_tree = merged.stdout.splitlines()[0] if merged.stdout else ""
    return bool(merged_tree) and merged_tree == _git_out(repo, "rev-parse", f"{main}^{{tree}}")


def processes_by_worktree(paths: Iterable[Path], *, proc_root: Path = Path("/proc")) -> dict[Path, list[int]]:
    """Map each worktree path to the PIDs whose cwd is inside it (best-effort)."""
    live: dict[Path, list[int]] = {}
    resolved = {path: path.resolve() for path in paths}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cwd = (entry / "cwd").resolve()
        except OSError:
            continue
        for path, target in resolved.items():
            if cwd == target or target in cwd.parents:
                live.setdefault(path, []).append(int(entry.name))
    return live


def classify_worktree(
    worktree: Worktree,
    *,
    repo: Path,
    main: str,
    pr_states: dict[str, PrInfo],
    main_path: Path,
    active_path: Path | None,
    live_pids: list[int],
) -> Classification:
    path = worktree.path
    if path == main_path:
        return RetainedWorktree(worktree, "main checkout")
    if active_path is not None and path == active_path:
        return RetainedWorktree(worktree, "the invoking worktree")
    if live_pids:
        return RetainedWorktree(worktree, f"a process is working in it (pid {live_pids[0]})")
    if _dirty(path):
        return RetainedWorktree(worktree, "uncommitted changes")

    pr = pr_states.get(worktree.branch) if worktree.branch else None
    if pr is not None and pr.state is PrState.OPEN:
        return RetainedWorktree(worktree, f"open PR #{pr.number}")
    if pr is not None and pr.state is PrState.MERGED:
        return PrunableWorktree(worktree, f"PR #{pr.number} merged")
    if _content_in_main(repo, worktree.head, main):
        return PrunableWorktree(worktree, f"changes already in {main}")
    if worktree.branch is None:
        return ReviewWorktree(worktree, f"detached HEAD with commits not in {main}")
    return ReviewWorktree(worktree, f"commits not in {main} and no merged PR")


def classify_worktrees(
    repo: Path,
    *,
    main: str,
    pr_states: dict[str, PrInfo],
    active_path: Path | None = None,
    proc_root: Path = Path("/proc"),
) -> list[Classification]:
    main_path = main_worktree(repo)
    worktrees = [wt for wt in list_worktrees(repo) if wt.path != main_path]
    live = processes_by_worktree((wt.path for wt in worktrees), proc_root=proc_root)
    return [
        classify_worktree(
            wt,
            repo=repo,
            main=main,
            pr_states=pr_states,
            main_path=main_path,
            active_path=active_path,
            live_pids=live.get(wt.path, []),
        )
        for wt in worktrees
    ]


@dataclass(frozen=True, slots=True)
class RemovedWorktree:
    path: Path


@dataclass(frozen=True, slots=True)
class SkippedWorktree:
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class FailedWorktree:
    path: Path
    error: str


type RemovalResult = RemovedWorktree | SkippedWorktree | FailedWorktree


def remove_prunable_worktrees(
    repo: Path,
    candidates: Iterable[PrunableWorktree],
    *,
    main: str,
    pr_states: dict[str, PrInfo],
    active_path: Path | None = None,
    proc_root: Path = Path("/proc"),
) -> list[RemovalResult]:
    """Remove each still-prunable candidate with `git worktree remove` (never --force).

    Re-classifies under the current state first, so a tree that turned dirty or busy since
    the scan is skipped, and `git worktree remove` itself refuses a dirty tree. Branches are
    left intact — the work stays reachable through them.
    """
    fresh = {
        item.worktree.path: item
        for item in classify_worktrees(
            repo, main=main, pr_states=pr_states, active_path=active_path, proc_root=proc_root
        )
    }
    results: list[RemovalResult] = []
    for candidate in candidates:
        path = candidate.worktree.path
        current = fresh.get(path)
        if not isinstance(current, PrunableWorktree):
            results.append(SkippedWorktree(path, current.reason if current is not None else "no longer listed"))
            continue
        outcome = _git(repo, "worktree", "remove", os.fspath(path), check=False)
        if outcome.returncode == 0:
            results.append(RemovedWorktree(path))
        else:
            results.append(FailedWorktree(path, outcome.stderr.strip() or "git worktree remove failed"))
    return results
