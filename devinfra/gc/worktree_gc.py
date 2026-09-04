"""Classify and safely remove local git worktrees whose work is already merged.

Mirrors output_base_gc's PRUNE/KEEP/REVIEW model. A worktree is prunable only when it is
clean (no tracked *or* untracked changes), idle (not the main checkout, not the invoking
worktree, no process cwd'd inside), and its work is already in the main branch —
established by git (HEAD is an ancestor of main; merging HEAD into main is a no-op, which
catches squash/rebase-merges; or the branch is empty) or by a merged GitHub PR for its
branch. Everything else is kept (dirty/active/live/open-PR) or reported for manual review
(clean but unique unmerged work, detached HEAD with unique commits, undeterminable main).

Repo-level git plumbing lives in `git_repo`; the PR model in `pull_request`. PR state is an
injected mapping so this module needs no network; the CLI (workspace_gc) fills it from
GitHub. `git worktree remove` refuses a dirty tree and deletes the working directory
including read-only files (pygit2's `Worktree.prune()` does neither by default).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pygit2

from devinfra.gc.git_repo import Worktree, content_in_main, git, patches_landed_in_main
from devinfra.gc.pull_request import PrInfo, PrState, pr_phrase


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
        return keep("uncommitted changes" + (f" ({pr_phrase(pr)})" if pr is not None else ""))
    if pr is not None and pr.state is PrState.OPEN:
        return keep(pr_phrase(pr))
    if pr is not None and pr.state is PrState.MERGED:
        return prune(pr_phrase(pr))
    if content_in_main(pg, pg.head.peel(pygit2.Commit).id, main):
        return prune(f"changes already in {main}")
    # As in `branch_gc`: the shell-out only runs once the in-process test has already failed.
    if patches_landed_in_main(path, "HEAD", main):
        return prune(f"every commit has an equivalent already on {main}")
    if worktree.branch is None:
        return review(f"detached HEAD with commits not in {main}")
    return review(f"commits not in {main} and no merged PR")


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


def remove_worktree(repo: Path, path: Path) -> RemovalResult:
    """Remove one worktree with `git worktree remove` (never `--force`).

    git refuses a dirty tree, so a tree that turned dirty since the scan fails rather than
    losing work. The branch is left intact — its work stays reachable through it (branch GC
    is a separate step). The caller re-scans and only passes still-prunable paths here.
    """
    outcome = git(repo, "worktree", "remove", os.fspath(path), check=False)
    if outcome.returncode == 0:
        return RemovedWorktree(path)
    return FailedWorktree(path, outcome.stderr.strip() or "git worktree remove failed")
