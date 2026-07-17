"""Find and safely remove local git worktrees whose work is already merged.

Mirrors output_base_gc's PRUNE/KEEP/REVIEW model. A worktree is prunable only when it is
clean (no tracked *or* untracked changes), idle (not the main checkout, not the invoking
worktree, no process cwd'd inside), and its work is already in the main branch —
established by git (HEAD is an ancestor of main; merging HEAD into main is a no-op, which
catches squash/rebase-merges; or the branch is empty) or by a merged GitHub PR for its
branch. Everything else is kept (dirty/active/live/open-PR) or reported for manual review
(clean but unique unmerged work, detached HEAD with unique commits, undeterminable main).

Git access is via pygit2 for content queries. Worktree enumeration and removal use the
git CLI: `worktree list --porcelain` returns the main worktree and every linked one with
its branch in one call (pygit2's `list_worktrees()` gives only linked-worktree names), and
`worktree remove` refuses a dirty tree and deletes the working directory including
read-only files (pygit2's `Worktree.prune()` does neither by default). PR state is an
injected mapping so this module needs no network; the CLI (workspace_gc) fills it via
PyGithub.
"""

from __future__ import annotations

import enum
import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pygit2


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


@dataclass(frozen=True, slots=True)
class PrunableWorktree:
    worktree: Worktree
    reason: str
    last_activity: datetime | None


@dataclass(frozen=True, slots=True)
class RetainedWorktree:
    worktree: Worktree
    reason: str
    last_activity: datetime | None


@dataclass(frozen=True, slots=True)
class ReviewWorktree:
    worktree: Worktree
    reason: str
    last_activity: datetime | None


type Classification = PrunableWorktree | RetainedWorktree | ReviewWorktree


class GitError(RuntimeError):
    """A git invocation failed in a way that blocks classification."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", os.fspath(repo), *args], capture_output=True, text=True, check=check)


def _git_out(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.strip()


def list_worktrees(repo: Path) -> list[Worktree]:
    """Every worktree of `repo`, parsed from `git worktree list --porcelain`."""
    worktrees: list[Worktree] = []
    path: Path | None = None
    branch: str | None = None
    for line in [*_git_out(repo, "worktree", "list", "--porcelain").splitlines(), ""]:
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
    ref = _git_out(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if not ref:
        raise GitError("cannot determine the default branch (origin/HEAD is unset)")
    return ref


def main_worktree(repo: Path) -> Path:
    """The main (non-linked) worktree — its `.git` is the common dir's parent."""
    return Path(_git_out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent


def _dirty(pg: pygit2.Repository) -> bool:
    # status() reports tracked changes and untracked files (ignored excluded by default);
    # any entry means the tree is not clean.
    return bool(pg.status())


def _last_activity(pg: pygit2.Repository, path: Path) -> datetime | None:
    """Most recent sign of work: the HEAD commit or the newest mtime among uncommitted
    (changed or untracked) files, so a dirty tree reflects when it was last *touched*."""
    times: list[datetime] = []
    if not pg.head_is_unborn:
        commit = pg[pg.head.target].peel(pygit2.Commit)
        tz = timezone(timedelta(minutes=commit.commit_time_offset))
        times.append(datetime.fromtimestamp(commit.commit_time, tz=tz))
    for rel in pg.status():
        try:
            times.append(datetime.fromtimestamp((path / rel).lstat().st_mtime, tz=UTC))
        except OSError:
            continue
    return max(times) if times else None


def _content_in_main(pg: pygit2.Repository, main: str) -> bool:
    """True when the worktree's HEAD adds nothing not already in `main`.

    Covers a direct merge (HEAD is an ancestor of main), an empty branch (HEAD is the merge
    base), and a squash/rebase-merge (merging HEAD into main yields main's exact tree, so
    HEAD contributed no net change).
    """
    head = pg.head.target
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


def _pr_phrase(pr: PrInfo) -> str:
    match pr.state:
        case PrState.OPEN:
            return f"open PR #{pr.number}"
        case PrState.MERGED:
            return f"PR #{pr.number} merged"
        case PrState.CLOSED:
            return f"closed PR #{pr.number}"


def classify_worktree(
    worktree: Worktree,
    *,
    main: str,
    pr_states: dict[str, PrInfo],
    main_path: Path,
    active_path: Path | None,
    live_pids: list[int],
) -> Classification:
    path = worktree.path
    pg = pygit2.Repository(os.fspath(path))
    activity = _last_activity(pg, path)

    def keep(reason: str) -> RetainedWorktree:
        return RetainedWorktree(worktree, reason, activity)

    def prune(reason: str) -> PrunableWorktree:
        return PrunableWorktree(worktree, reason, activity)

    def review(reason: str) -> ReviewWorktree:
        return ReviewWorktree(worktree, reason, activity)

    if path == main_path:
        return keep("main checkout")
    if active_path is not None and path == active_path:
        return keep("the invoking worktree")
    if live_pids:
        return keep(f"a process is working in it (pid {live_pids[0]})")
    pr = pr_states.get(worktree.branch) if worktree.branch else None
    if _dirty(pg):
        # Uncommitted work is never auto-pruned, but surface the branch's PR so a dirty
        # tree whose PR already merged reads as stale scratch, not live work.
        return keep("uncommitted changes" + (f" ({_pr_phrase(pr)})" if pr is not None else ""))
    if pr is not None and pr.state is PrState.OPEN:
        return keep(_pr_phrase(pr))
    if pr is not None and pr.state is PrState.MERGED:
        return prune(_pr_phrase(pr))
    if _content_in_main(pg, main):
        return prune(f"changes already in {main}")
    if worktree.branch is None:
        return review(f"detached HEAD with commits not in {main}")
    return review(f"commits not in {main} and no merged PR")


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
