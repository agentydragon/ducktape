"""Classify and safely remove local git worktrees whose work is already merged.

Mirrors output_base_gc's PRUNE/KEEP/REVIEW model. A worktree is prunable only when it is
clean (no tracked or untracked changes), idle (not the main checkout, the invoking worktree,
or a process's working directory), and its work has landed in the main branch or its branch's
PR has merged or closed. Detached commits qualify by content or patch equivalence. Dirty,
active, and open-PR worktrees are kept; unique work is reported for review.

Repo-level git plumbing lives in `git_repo`; the PR model in `pull_request`. PR state is an
injected mapping so this module needs no network; the CLI (workspace_gc) fills it from
GitHub. `git worktree remove` refuses tracked or untracked changes, but deletes ignored files.
The final detached HEAD check and removal are not atomic.
"""

from __future__ import annotations

import enum
import logging
import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pygit2

from devinfra.gc.async_io import run_blocking
from devinfra.gc.git_repo import (
    INSPECTION_ERRORS,
    Worktree,
    content_in_main,
    git,
    list_worktrees,
    open_repo,
    patches_landed_in_main,
)
from devinfra.gc.pull_request import PrInfo, PrLookup, PrState, pr_phrase
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
    PR_LOOKUP_UNAVAILABLE = enum.auto()
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
    pr: PrInfo | None = None  # set for OPEN_PR; for retained local data when the branch has a PR
    pid: int | None = None  # set for LIVE_PROCESS: one process working in the tree


@dataclass(frozen=True, slots=True)
class ReviewWorktree(Review):
    worktree: Worktree
    reason: ReviewWorktreeReason
    last_activity: datetime | None
    main: str  # the ref the verdict was judged against, e.g. `origin/devel`
    detail: str | None = None


type Classification = PrunableWorktree | RetainedWorktree | ReviewWorktree


def resolve_worktree_pr(item: Classification, lookup: PrLookup) -> Classification:
    """Apply the completed branch lookup to a local worktree classification.

    Local inspection can run while the GitHub request is in flight.
    """
    branch = item.worktree.branch
    if branch is None:
        return item
    pr = lookup.states.get(branch)
    if isinstance(item, PrunableWorktree):
        if branch in lookup.unknown:
            return ReviewWorktree(
                item.worktree, ReviewWorktreeReason.PR_LOOKUP_UNAVAILABLE, item.last_activity, item.main
            )
        if item.reason is PrunableWorktreeReason.DIRECTORY_MISSING:
            return item
        if pr is not None and pr.state is PrState.OPEN:
            return RetainedWorktree(item.worktree, RetainedWorktreeReason.OPEN_PR, item.last_activity, pr=pr)
        if pr is not None and pr.state in (PrState.MERGED, PrState.CLOSED):
            return PrunableWorktree(
                item.worktree, PrunableWorktreeReason.PR_LANDED, item.last_activity, item.main, pr=pr
            )
        return item
    if isinstance(item, RetainedWorktree):
        if item.reason is RetainedWorktreeReason.UNCOMMITTED_CHANGES:
            return replace(item, pr=pr)
        return item
    if item.reason is not ReviewWorktreeReason.UNMERGED:
        return item
    if branch in lookup.unknown:
        return ReviewWorktree(item.worktree, ReviewWorktreeReason.PR_LOOKUP_UNAVAILABLE, item.last_activity, item.main)
    if pr is not None and pr.state is PrState.OPEN:
        return RetainedWorktree(item.worktree, RetainedWorktreeReason.OPEN_PR, item.last_activity, pr=pr)
    if pr is not None and pr.state in (PrState.MERGED, PrState.CLOSED):
        return PrunableWorktree(item.worktree, PrunableWorktreeReason.PR_LANDED, item.last_activity, item.main, pr=pr)
    return item


def describe_reason(item: Classification) -> str:
    match item:
        case ReviewWorktree(reason=ReviewWorktreeReason.INSPECTION_FAILED, detail=str() as detail):
            return f"cannot inspect: {detail}"
        case ReviewWorktree(reason=ReviewWorktreeReason.PR_LOOKUP_UNAVAILABLE):
            return "pull request state could not be determined"
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
        case ReviewWorktree(reason=ReviewWorktreeReason.DETACHED_HEAD, main=main):
            return f"detached HEAD with commits not proven landed in {main}"
        case ReviewWorktree(reason=ReviewWorktreeReason.UNMERGED, main=main):
            return f"commits not in {main} and no merged PR"
        case _:
            raise ValueError(f"{item=} lacks the data its reason cites")


async def _status(path: Path) -> list[str]:
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
        outcome = await git(
            path, "--no-optional-locks", "status", "--porcelain=v1", "-z", f"--untracked-files={untracked}"
        )
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


async def classify_worktree(
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
        return await _classify_worktree(
            worktree, main=main, pr_states=pr_states, main_path=main_path, active_path=active_path, live_pids=live_pids
        )
    except INSPECTION_ERRORS as error:
        detail = f"{type(error).__name__}: {error}"
        logger.warning("Cannot inspect worktree %s: %s", worktree.path, detail)
        return ReviewWorktree(worktree, ReviewWorktreeReason.INSPECTION_FAILED, None, main, detail=detail)


async def _classify_worktree(
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
        await run_blocking(path.lstat)
    except FileNotFoundError:
        if worktree.branch is None:
            return ReviewWorktree(worktree, ReviewWorktreeReason.DETACHED_HEAD, None, main)
        return PrunableWorktree(worktree, PrunableWorktreeReason.DIRECTORY_MISSING, None, main)
    pg = await run_blocking(pygit2.Repository, os.fspath(path))
    branch = await run_blocking(lambda: None if pg.head_is_detached else pg.head.shorthand)
    head = await run_blocking(lambda: str(pg.head.peel(pygit2.Commit).id)) if branch is None else None
    if branch is None and (worktree.head is None or head != worktree.head):
        return ReviewWorktree(worktree, ReviewWorktreeReason.DETACHED_HEAD, None, main)
    worktree = Worktree(path, branch, head)
    logger.info("Scanning worktree %s: reading Git status", path)
    status = await _status(path)
    logger.info("Scanning worktree %s: Git status complete (%d entries)", path, len(status))
    activity = await run_blocking(_last_activity, pg, path, status)

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
    if worktree.branch is None:
        assert head is not None
        head_oid = pygit2.Oid(hex=head)
        if await run_blocking(lambda: content_in_main(pg, head_oid, main)):
            return prune(PrunableWorktreeReason.CONTENT_IN_MAIN)
        if await patches_landed_in_main(path, head, main):
            return prune(PrunableWorktreeReason.PATCHES_IN_MAIN)
        return review(ReviewWorktreeReason.DETACHED_HEAD)
    if await run_blocking(lambda: content_in_main(pg, pg.head.peel(pygit2.Commit).id, main)):
        return prune(PrunableWorktreeReason.CONTENT_IN_MAIN)
    # As in `branch_gc`: the shell-out only runs once the in-process test has already failed.
    if await patches_landed_in_main(path, "HEAD", main):
        return prune(PrunableWorktreeReason.PATCHES_IN_MAIN)
    return review(ReviewWorktreeReason.UNMERGED)


async def remove_worktree(repo: Path, path: Path, *, expected_detached_head: str | None = None) -> RemovalResult[Path]:
    """Remove one worktree with `git worktree remove` (never `--force`).

    Git refuses tracked or untracked changes but deletes ignored data. This entry point checks
    the detached HEAD when an expected commit is supplied. An attached branch is left intact
    (branch GC is a separate step). Use `remove_if_still_prunable` unless the caller has just
    classified `path` itself. A file or
    detached commit created after inspection but before Git removes the directory remains a
    race this interface cannot make atomic.
    """
    try:
        await run_blocking(path.lstat)
    except FileNotFoundError:
        return Skipped(path, "worktree directory is missing; review and prune its stale Git metadata separately")
    except INSPECTION_ERRORS as error:
        return Failed(path, f"cannot inspect worktree directory before removal: {error}")
    if expected_detached_head is not None:
        try:

            def head_matches() -> bool:
                current = open_repo(path)
                return current.head_is_detached and str(current.head.peel(pygit2.Commit).id) == expected_detached_head

            if not await run_blocking(head_matches):
                return Skipped(path, "HEAD changed since classification")
        except INSPECTION_ERRORS as error:
            return Failed(path, f"cannot inspect worktree safety before removal: {error}")
    try:
        outcome = await git(repo, "worktree", "remove", path, check=False, interruptible=False)
    except (OSError, subprocess.SubprocessError) as error:
        return Failed(path, str(error))
    if outcome.returncode == 0:
        return Removed(path)
    return Failed(path, outcome.stderr.strip() or "git worktree remove failed")


async def remove_if_still_prunable(
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

    The scan that found it may be seconds old: a process may have entered the tree, the
    checked-out branch may have changed. The direct removal helper checks the current HEAD
    again immediately before invoking Git,
    though these checks are not atomic with the removal.
    """
    try:
        current = next((item for item in await list_worktrees(repo) if item.path == worktree.path), None)
    except INSPECTION_ERRORS as error:
        return Skipped(worktree.path, f"cannot list worktrees: {error}")
    if current != worktree:
        return Skipped(worktree.path, "worktree or checked-out branch changed since the scan")
    fresh = await classify_worktree(
        worktree, main=main, pr_states=pr_states, main_path=main_path, active_path=active_path, live_pids=live_pids
    )
    if fresh.worktree.branch != worktree.branch:
        return Skipped(worktree.path, "checked-out branch changed since the scan")
    if not isinstance(fresh, PrunableWorktree):
        return Skipped(worktree.path, describe_reason(fresh))
    return await remove_worktree(repo, worktree.path, expected_detached_head=fresh.worktree.head)
