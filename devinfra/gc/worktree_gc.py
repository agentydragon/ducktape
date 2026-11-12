"""Classify and remove clean, idle worktrees whose work has landed.

Ignored files do not prevent removal and Git deletes them. The workspace must not change
while workspace-gc is running; inspection and removal are not atomic.
"""

from __future__ import annotations

import enum
import logging
import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pygit2

from devinfra.gc.git_repo import INSPECTION_ERRORS, Worktree, content_in_main, git, patches_landed_in_main
from devinfra.gc.pull_request import PrInfo, PrState, pr_phrase
from devinfra.gc.removal import Failed, RemovalResult, Removed, Skipped
from devinfra.gc.verdict import Prunable, Retained, Review

logger = logging.getLogger(__name__)


class PrunableWorktreeReason(enum.StrEnum):
    DIRECTORY_MISSING = enum.auto()
    PR_LANDED = enum.auto()  # the branch's PR is merged or closed
    CONTENT_IN_MAIN = enum.auto()
    PATCHES_IN_MAIN = enum.auto()


class RetainedWorktreeReason(enum.StrEnum):
    MAIN_CHECKOUT = enum.auto()
    INVOKING_WORKTREE = enum.auto()
    LIVE_PROCESS = enum.auto()
    UNCOMMITTED_CHANGES = enum.auto()
    OPEN_PR = enum.auto()


class ReviewWorktreeReason(enum.StrEnum):
    INSPECTION_FAILED = enum.auto()
    DETACHED_HEAD = enum.auto()
    UNMERGED = enum.auto()


@dataclass(frozen=True, slots=True)
class PrunableWorktree(Prunable):
    worktree: Worktree
    reason: PrunableWorktreeReason
    last_activity: datetime | None
    main: str  # the ref the verdict was judged against, e.g. `origin/devel`
    pr: PrInfo | None = None  # set for PR_LANDED


@dataclass(frozen=True, slots=True)
class RetainedWorktree(Retained):
    worktree: Worktree
    reason: RetainedWorktreeReason
    last_activity: datetime | None
    pr: PrInfo | None = None  # set for OPEN_PR; for UNCOMMITTED_CHANGES when the branch has a PR
    pid: int | None = None  # set for LIVE_PROCESS: one process working in the tree


@dataclass(frozen=True, slots=True)
class ReviewWorktree(Review):
    worktree: Worktree
    reason: ReviewWorktreeReason
    last_activity: datetime | None
    main: str  # the ref the verdict was judged against, e.g. `origin/devel`
    detail: str | None = None


type Classification = PrunableWorktree | RetainedWorktree | ReviewWorktree


def describe_reason(item: Classification) -> str:
    match item:
        case ReviewWorktree(reason=ReviewWorktreeReason.INSPECTION_FAILED, detail=str() as detail):
            return f"cannot inspect: {detail}"
        case PrunableWorktree(reason=PrunableWorktreeReason.DIRECTORY_MISSING):
            return "worktree directory is missing"
        case PrunableWorktree(reason=PrunableWorktreeReason.PR_LANDED, pr=PrInfo() as pr):
            return pr_phrase(pr)
        case PrunableWorktree(reason=PrunableWorktreeReason.CONTENT_IN_MAIN, main=main):
            return f"changes already in {main}"
        case PrunableWorktree(reason=PrunableWorktreeReason.PATCHES_IN_MAIN, main=main):
            return f"every commit has an equivalent already on {main}"
        case RetainedWorktree(reason=RetainedWorktreeReason.MAIN_CHECKOUT):
            return "main checkout"
        case RetainedWorktree(reason=RetainedWorktreeReason.INVOKING_WORKTREE):
            return "the invoking worktree"
        case RetainedWorktree(reason=RetainedWorktreeReason.LIVE_PROCESS, pid=int() as pid):
            return f"a process is working in it (pid {pid})"
        case RetainedWorktree(reason=RetainedWorktreeReason.UNCOMMITTED_CHANGES, pr=pr):
            return "uncommitted changes" + (f" ({pr_phrase(pr)})" if pr is not None else "")
        case RetainedWorktree(reason=RetainedWorktreeReason.OPEN_PR, pr=PrInfo() as pr):
            return pr_phrase(pr)
        case ReviewWorktree(reason=ReviewWorktreeReason.DETACHED_HEAD):
            return "detached HEAD commit reachability is not proven safe for removal"
        case ReviewWorktree(reason=ReviewWorktreeReason.UNMERGED, main=main):
            return f"commits not in {main} and no merged PR"
        case _:
            raise ValueError(f"{item=} lacks the data its reason cites")


def _status(path: Path) -> list[str]:
    """Paths `git status` reports (tracked changes, then untracked files; ignored excluded).

    Shells out because libgit2's status holds the GIL, which serializes the scan's worker
    threads; `git` runs in its own process. `--no-optional-locks` keeps concurrent runs from
    contending on the index lock.

    Untracked-file detection has no early-exit: proving "nothing untracked exists" means
    walking every directory the tree doesn't already fully ignore, a cost set by repo size
    rather than by how much actually changed. A tracked-only check has no such walk, and on a
    worktree with real tracked changes or deletions it already proves the tree isn't clean, so
    the untracked walk is skipped. Only a worktree that's clean on tracked files pays for it.

    The caller's `_last_activity` sees only the tracked paths when that already proved dirty,
    so an untracked file's mtime can't push the reported last-activity time later in that case
    — acceptable since the classification itself (kept, uncommitted changes) doesn't depend
    on that timestamp.
    """
    for untracked in ("no", "normal"):
        outcome = git(path, "--no-optional-locks", "status", "--porcelain=v1", "-z", f"--untracked-files={untracked}")
        # `-z` entries are `XY <path>` joined by NUL; renames are not detected in porcelain v1
        # without `-M`, so each entry carries exactly one path.
        paths = [entry[3:] for entry in outcome.stdout.split("\0") if entry]
        if paths:
            return paths
    return []


def _last_activity(pg: pygit2.Repository, path: Path, status: Iterable[str]) -> datetime | None:
    """Most recent sign of work: the HEAD commit or the newest mtime among uncommitted
    (changed or untracked) files, so a dirty tree reflects when it was last *touched*."""
    times: list[datetime] = []
    if not pg.head_is_unborn:
        commit = pg[pg.head.target].peel(pygit2.Commit)
        tz = timezone(timedelta(minutes=commit.commit_time_offset))
        times.append(datetime.fromtimestamp(commit.commit_time, tz=tz))
    for rel in status:
        try:
            times.append(datetime.fromtimestamp((path / rel).lstat().st_mtime, tz=UTC))
        except OSError:
            continue
    return max(times) if times else None


def processes_by_worktree(paths: Iterable[Path], *, proc_root: Path = Path("/proc")) -> dict[Path, list[int]]:
    """Map each worktree path to the PIDs whose cwd is inside it (best-effort)."""
    live: dict[Path, list[int]] = {}
    resolved = {path: path.resolve() for path in paths}
    if not resolved:
        logger.info("No worktrees need a process working-directory scan")
        return live
    logger.info("Scanning process working directories for %d worktrees", len(resolved))
    process_count = 0
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        process_count += 1
        if process_count % 500 == 0:
            logger.info("Scanned %d process working directories", process_count)
        try:
            cwd = (entry / "cwd").resolve()
        except OSError:
            continue
        for path, target in resolved.items():
            if cwd == target or target in cwd.parents:
                live.setdefault(path, []).append(int(entry.name))
    logger.info("Process working-directory scan complete: %d processes, %d worktrees active", process_count, len(live))
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
    """Classify one worktree; unreadable state requires review and cannot justify pruning."""
    try:
        return _classify_worktree(
            worktree, main=main, pr_states=pr_states, main_path=main_path, active_path=active_path, live_pids=live_pids
        )
    except INSPECTION_ERRORS as error:
        detail = f"{type(error).__name__}: {error}"
        logger.warning("Cannot inspect worktree %s: %s", worktree.path, detail)
        return ReviewWorktree(worktree, ReviewWorktreeReason.INSPECTION_FAILED, None, main, detail=detail)


def _classify_worktree(
    worktree: Worktree,
    *,
    main: str,
    pr_states: dict[str, PrInfo],
    main_path: Path,
    active_path: Path | None,
    live_pids: list[int],
) -> Classification:
    path = worktree.path
    try:
        path.lstat()
    except FileNotFoundError:
        if worktree.branch is None:
            return ReviewWorktree(worktree, ReviewWorktreeReason.DETACHED_HEAD, None, main)
        return PrunableWorktree(worktree, PrunableWorktreeReason.DIRECTORY_MISSING, None, main)
    pg = pygit2.Repository(os.fspath(path))
    worktree = Worktree(path, None if pg.head_is_detached else pg.head.shorthand)
    logger.info("Scanning worktree %s: reading Git status", path)
    status = _status(path)
    logger.info("Scanning worktree %s: Git status complete (%d entries)", path, len(status))
    activity = _last_activity(pg, path, status)
    head = pg.head.peel(pygit2.Commit).id

    def keep(reason: RetainedWorktreeReason, *, pr: PrInfo | None = None, pid: int | None = None) -> RetainedWorktree:
        return RetainedWorktree(worktree, reason, activity, pr=pr, pid=pid)

    def prune(reason: PrunableWorktreeReason, *, pr: PrInfo | None = None) -> PrunableWorktree:
        return PrunableWorktree(worktree, reason, activity, main, pr=pr)

    def review(reason: ReviewWorktreeReason) -> ReviewWorktree:
        return ReviewWorktree(worktree, reason, activity, main)

    if path == main_path:
        return keep(RetainedWorktreeReason.MAIN_CHECKOUT)
    if active_path is not None and path == active_path:
        return keep(RetainedWorktreeReason.INVOKING_WORKTREE)
    if live_pids:
        return keep(RetainedWorktreeReason.LIVE_PROCESS, pid=live_pids[0])
    pr = pr_states.get(worktree.branch) if worktree.branch else None
    if status:
        # Uncommitted work is never auto-pruned, but surface the branch's PR so a dirty
        # tree whose PR already merged reads as stale scratch, not live work.
        return keep(RetainedWorktreeReason.UNCOMMITTED_CHANGES, pr=pr)
    if pr is not None and pr.state is PrState.OPEN:
        return keep(RetainedWorktreeReason.OPEN_PR, pr=pr)
    if pr is not None and pr.state in (PrState.MERGED, PrState.CLOSED):
        # Removing a worktree never touches its branch, so this is safe regardless of
        # whether the branch has advanced past the PR's head — an advanced branch is
        # caught separately by branch_gc, which does have to care (deleting a branch ref
        # is destructive).
        return prune(PrunableWorktreeReason.PR_LANDED, pr=pr)
    if content_in_main(pg, head, main):
        return prune(PrunableWorktreeReason.CONTENT_IN_MAIN)
    if patches_landed_in_main(path, str(head), main):
        return prune(PrunableWorktreeReason.PATCHES_IN_MAIN)
    return review(ReviewWorktreeReason.UNMERGED)


def remove_worktree(repo: Path, path: Path) -> RemovalResult[Path]:
    """Remove a worktree; Git refuses tracked and untracked changes but deletes ignored files."""
    try:
        path.lstat()
    except FileNotFoundError:
        return Skipped(path, "worktree directory is missing; review and prune its stale Git metadata separately")
    except OSError as error:
        return Failed(path, f"cannot inspect worktree directory before removal: {error}")
    try:
        outcome = git(repo, "worktree", "remove", os.fspath(path), check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return Failed(path, str(error))
    if outcome.returncode == 0:
        return Removed(path)
    return Failed(path, outcome.stderr.strip() or "git worktree remove failed")


def remove_if_still_prunable(
    repo: Path,
    worktree: Worktree,
    *,
    main: str,
    pr_states: dict[str, PrInfo],
    main_path: Path,
    active_path: Path | None,
    live_pids: list[int],
) -> RemovalResult[Path]:
    """Re-classify `worktree` now and remove it only if it is still prunable.

    Reclassification and removal are not atomic; the workspace must remain unchanged.
    """
    fresh = classify_worktree(
        worktree, main=main, pr_states=pr_states, main_path=main_path, active_path=active_path, live_pids=live_pids
    )
    if not isinstance(fresh, PrunableWorktree):
        return Skipped(worktree.path, describe_reason(fresh))
    return remove_worktree(repo, worktree.path)
