"""Classify and safely remove local Git branches with recoverable history.

A PRUNE classification means the branch is a historical content candidate, not that its
commits are disposable. Removal anchors the captured branch tip and available reflog commits
under `refs/workspace-gc/recovery/` in the same transaction as deletion. Classification and
removal assume branches and worktree checkouts do not change during the run.
"""

from __future__ import annotations

import enum
import logging
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pygit2

from devinfra.gc import git_repo
from devinfra.gc.async_io import run_blocking
from devinfra.gc.git_repo import content_in_main, patches_landed_in_main
from devinfra.gc.pull_request import PrInfo, PrState, pr_phrase
from devinfra.gc.removal import Failed, RemovalResult, Removed, Skipped
from devinfra.gc.verdict import Prunable, Retained, Review
from devinfra.gc.worktree_gc import Classification, RetainedWorktree, ReviewWorktree

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MainCheckout:
    """Sentinel holder: the branch is checked out in the repo's main worktree."""


type Holder = Classification | MainCheckout | None


class PrunableBranchReason(enum.StrEnum):
    CONTENT_IN_MAIN = enum.auto()
    PR_HEAD_REACHED = enum.auto()  # nothing beyond the head of the branch's merged or closed PR
    PATCHES_IN_MAIN = enum.auto()


class RetainedBranchReason(enum.StrEnum):
    DEFAULT_BRANCH = enum.auto()
    MAIN_CHECKOUT = enum.auto()
    HELD_BY_WORKTREE = enum.auto()  # by a retained or review worktree
    OPEN_PR = enum.auto()


class ReviewBranchReason(enum.StrEnum):
    INSPECTION_FAILED = enum.auto()
    PR_LOOKUP_UNAVAILABLE = enum.auto()
    PR_HEAD_EXCEEDED = enum.auto()  # commits beyond the head of the branch's merged or closed PR
    UNMERGED = enum.auto()


@dataclass(frozen=True, slots=True)
class PrunableBranch(Prunable):
    branch: str
    reason: PrunableBranchReason
    main: str  # the ref the verdict was judged against, e.g. `origin/devel`
    tip: str  # the captured commit OID every proof and deletion is bound to
    pr: PrInfo | None  # set for PR_HEAD_REACHED; for CONTENT_IN_MAIN when the branch has a merged or closed PR


@dataclass(frozen=True, slots=True)
class BranchRemoval:
    result: RemovalResult[str]
    recovery_prefix: str | None = None


@dataclass(frozen=True, slots=True)
class RetainedBranch(Retained):
    branch: str
    reason: RetainedBranchReason
    checkout: Path | None = None  # set for HELD_BY_WORKTREE: the worktree holding it
    pr: PrInfo | None = None  # set for OPEN_PR


@dataclass(frozen=True, slots=True)
class ReviewBranch(Review):
    branch: str
    reason: ReviewBranchReason
    main: str  # the ref the verdict was judged against, e.g. `origin/devel`
    pr: PrInfo | None = None  # set for PR_HEAD_EXCEEDED
    detail: str | None = None


type BranchClassification = PrunableBranch | RetainedBranch | ReviewBranch


def describe_reason(item: BranchClassification) -> str:
    match item:
        case ReviewBranch(reason=ReviewBranchReason.INSPECTION_FAILED, detail=str() as detail):
            return f"cannot inspect: {detail}"
        case ReviewBranch(reason=ReviewBranchReason.PR_LOOKUP_UNAVAILABLE):
            return "pull request state could not be determined"
        case PrunableBranch(reason=PrunableBranchReason.CONTENT_IN_MAIN, main=main, pr=pr):
            return f"changes already in {main}" + (f" ({pr_phrase(pr)})" if pr is not None else "")
        case PrunableBranch(reason=PrunableBranchReason.PR_HEAD_REACHED, pr=PrInfo() as pr):
            return f"{pr_phrase(pr)}; nothing beyond the {pr.state.lower()} head"
        case PrunableBranch(reason=PrunableBranchReason.PATCHES_IN_MAIN, main=main):
            return f"every commit has an equivalent already on {main}"
        case RetainedBranch(reason=RetainedBranchReason.DEFAULT_BRANCH):
            return "default branch"
        case RetainedBranch(reason=RetainedBranchReason.MAIN_CHECKOUT):
            return "checked out in main checkout"
        case RetainedBranch(reason=RetainedBranchReason.HELD_BY_WORKTREE, checkout=Path() as checkout):
            return f"checked out in retained worktree {checkout}"
        case RetainedBranch(reason=RetainedBranchReason.OPEN_PR, pr=PrInfo() as pr):
            return pr_phrase(pr)
        case ReviewBranch(reason=ReviewBranchReason.PR_HEAD_EXCEEDED, pr=PrInfo() as pr):
            return f"{pr_phrase(pr)} but branch has commits beyond it"
        case ReviewBranch(reason=ReviewBranchReason.UNMERGED, main=main):
            return f"commits not in {main}"
        case _:
            raise ValueError(f"{item=} lacks the data its reason cites")


def local_branches(pg: pygit2.Repository) -> list[str]:
    return sorted(pg.branches.local)


async def branch_holders(repo: Path) -> dict[str, Path]:
    """Each branch that is checked out, mapped to the worktree holding it (main included)."""
    return {wt.branch: wt.path for wt in await git_repo.list_worktrees(repo) if wt.branch}


def _tip_within_pr_head(pg: pygit2.Repository, branch_oid: pygit2.Oid, pr: PrInfo) -> bool:
    """True when the branch tip is at, or an ancestor of, the PR's head SHA.

    For a merged PR that means the branch added nothing past the squash-merge point; for a
    closed-not-merged PR it means nothing was committed after the PR was abandoned. Needs the
    head object present locally to reason about ancestry; if it was never fetched, defer.
    """
    if pr.head_sha is None:
        return False
    try:
        head_oid = pygit2.Oid(hex=pr.head_sha)
    except ValueError:
        return False
    if head_oid not in pg:
        return False
    return branch_oid == head_oid or pg.descendant_of(head_oid, branch_oid)


async def classify_branch(
    name: str, *, pg: pygit2.Repository, main: str, default_branch: str, pr: PrInfo | None, holder: Holder
) -> BranchClassification:
    """Classify one branch, reporting inspection failures without blocking other branches."""
    try:
        return await _classify_branch(name, pg=pg, main=main, default_branch=default_branch, pr=pr, holder=holder)
    except git_repo.INSPECTION_ERRORS as error:
        detail = f"{type(error).__name__}: {error}"
        logger.warning("Cannot inspect branch %s in %s: %s", name, pg.path, detail)
        return ReviewBranch(name, ReviewBranchReason.INSPECTION_FAILED, main, detail=detail)


async def _classify_branch(
    name: str, *, pg: pygit2.Repository, main: str, default_branch: str, pr: PrInfo | None, holder: Holder
) -> BranchClassification:
    if name == default_branch:
        return RetainedBranch(name, RetainedBranchReason.DEFAULT_BRANCH)
    if isinstance(holder, MainCheckout):
        return RetainedBranch(name, RetainedBranchReason.MAIN_CHECKOUT)
    if isinstance(holder, RetainedWorktree | ReviewWorktree):
        return RetainedBranch(name, RetainedBranchReason.HELD_BY_WORKTREE, checkout=holder.worktree.path)
    if pr is not None and pr.state is PrState.OPEN:
        return RetainedBranch(name, RetainedBranchReason.OPEN_PR, pr=pr)

    # A prunable worktree does not block this content candidate; its branch ref is still kept.
    branch_oid = await run_blocking(lambda: pg.branches.local[name].peel(pygit2.Commit).id)
    if await run_blocking(content_in_main, pg, branch_oid, main):
        return PrunableBranch(name, PrunableBranchReason.CONTENT_IN_MAIN, main, str(branch_oid), pr)
    if (
        pr is not None
        and pr.state in (PrState.MERGED, PrState.CLOSED)
        and await run_blocking(_tip_within_pr_head, pg, branch_oid, pr)
    ):
        return PrunableBranch(name, PrunableBranchReason.PR_HEAD_REACHED, main, str(branch_oid), pr)
    # Last, because it is the only check here that shells out: ~200ms, worth paying for the
    # handful of branches otherwise bound for REVIEW but not for every branch in the repo.
    # The cheaper tests above also name a more specific reason when they apply.
    if await patches_landed_in_main(Path(pg.path), str(branch_oid), main):
        return PrunableBranch(name, PrunableBranchReason.PATCHES_IN_MAIN, main, str(branch_oid), None)
    if pr is not None and pr.state in (PrState.MERGED, PrState.CLOSED):
        return ReviewBranch(name, ReviewBranchReason.PR_HEAD_EXCEEDED, main, pr)
    return ReviewBranch(name, ReviewBranchReason.UNMERGED, main)


_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


async def _reflog_oids(repo: Path, ref: str, tip: str) -> list[str] | None:
    """Inventory all available reflog commit IDs, failing closed on unreadable evidence."""
    env = {**os.environ, "LC_ALL": "C"}
    shown = await git_repo.git(repo, "reflog", "show", "--format=%H", ref, check=False, env=env)
    if shown.returncode != 0:
        return None
    shown_oids = [line.strip() for line in shown.stdout.splitlines() if line.strip()]
    if any(_OID.fullmatch(oid) is None for oid in shown_oids):
        return None

    path_result = await git_repo.git(
        repo, "rev-parse", "--path-format=absolute", "--git-path", f"logs/{ref}", check=False
    )
    if path_result.returncode != 0:
        return None
    log_path = Path(path_result.stdout.strip())
    try:
        raw_log = await run_blocking(log_path.read_bytes)
    except FileNotFoundError:
        if shown_oids:
            return None
        entries: list[tuple[str, str]] = []
    except OSError, UnicodeError:
        return None
    else:
        entries = []
        for line in raw_log.splitlines():
            # Git creates a linked worktree's initial HEAD reflog row without a
            # tab or message. Both forms still have OIDs, identity, time and zone.
            metadata, _separator, _message = line.partition(b"\t")
            fields = metadata.split()
            if len(fields) < 5:
                return None
            try:
                old_oid, new_oid = (field.decode("ascii") for field in fields[:2])
            except UnicodeDecodeError:
                return None
            if _OID.fullmatch(old_oid) is None or _OID.fullmatch(new_oid) is None:
                return None
            entries.append((old_oid, new_oid))

    if [new_oid for _, new_oid in reversed(entries)] != shown_oids:
        return None

    oids = {tip, *shown_oids}
    # `reflog show` lists each entry's new OID, but not the predecessor before its oldest
    # retained row. Reading the raw log above captures it; rev-parse independently verifies
    # Git can resolve that boundary. A new branch's all-zero predecessor is the one normal
    # case where @{N} has no resolvable object.
    if entries:
        oldest_old = entries[0][0]
        boundary = await git_repo.git(repo, "rev-parse", "--verify", f"{ref}@{{{len(entries)}}}", check=False, env=env)
        if boundary.returncode == 0:
            boundary_oid = boundary.stdout.strip()
            if _OID.fullmatch(boundary_oid) is None or boundary_oid != oldest_old:
                return None
            if int(boundary_oid, 16) != 0:
                oids.add(boundary_oid)
        elif int(oldest_old, 16) != 0 or f"only has {len(entries)} entries" not in boundary.stderr:
            return None
        if int(oldest_old, 16) != 0:
            oids.add(oldest_old)
        for old_oid, new_oid in entries:
            if int(old_oid, 16) != 0:
                oids.add(old_oid)
            if int(new_oid, 16) != 0:
                oids.add(new_oid)

    candidates = sorted(oids)
    checked = await git_repo.git(
        repo,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype)",
        check=False,
        input_text="".join(f"{oid}\n" for oid in candidates),
    )
    if checked.returncode != 0:
        return None
    rows = [line.split() for line in checked.stdout.splitlines()]
    if len(rows) != len(candidates) or any(row != [oid, "commit"] for oid, row in zip(candidates, rows, strict=True)):
        return None
    return candidates


async def _recovery_tip_exists(repo: Path, prefix: str) -> bool:
    result = await git_repo.git(repo, "show-ref", "--verify", "--hash", f"{prefix}/tip", check=False)
    return result.returncode == 0


async def remove_prunable_branch(repo: Path, candidate: PrunableBranch) -> BranchRemoval:
    """Remove a classified branch, preserving its tip and reflog in recovery refs."""
    name = candidate.branch
    ref = f"refs/heads/{name}"
    prefix: str | None = None
    transaction_committed = False
    transaction_started = False
    try:
        history = await _reflog_oids(repo, ref, candidate.tip)
        if history is None:
            return BranchRemoval(Failed(name, "could not safely inventory branch reflog"))
        # Keep the branch name in the ref so recovery remains discoverable even if the
        # process dies before it can print its report. The attempt ID prevents collisions.
        prefix = f"refs/workspace-gc/recovery/{uuid.uuid4().hex}/{name}"
        commands = ["start", f"create {prefix}/tip {candidate.tip}"]
        commands.extend(f"create {prefix}/history/{oid} {oid}" for oid in history if oid != candidate.tip)
        commands.extend(["option no-deref", f"delete {ref}", "prepare", "commit", ""])
        transaction_started = True
        transaction = await git_repo.git(
            repo, "update-ref", "--stdin", check=False, input_text="\n".join(commands), interruptible=False
        )
        if transaction.returncode != 0:
            try:
                recovery_exists = await _recovery_tip_exists(repo, prefix)
            except Exception:
                recovery_exists = True  # preserve the uncertain location in the report
            actual = await git_repo.git(repo, "rev-parse", "--verify", ref, check=False)
            if actual.returncode != 0 and recovery_exists:
                transaction_committed = True
            else:
                prefix = prefix if recovery_exists else None
                if actual.returncode != 0:
                    return BranchRemoval(Skipped(name, "branch no longer exists"), prefix)
                return BranchRemoval(
                    Failed(name, transaction.stderr.strip() or "branch removal transaction failed"), prefix
                )
        transaction_committed = True

        return BranchRemoval(Removed(name), prefix)
    except (OSError, subprocess.SubprocessError, git_repo.GitError) as error:
        recovery_prefix: str | None = prefix if transaction_committed else None
        if transaction_started and prefix is not None and recovery_prefix is None:
            try:
                if await _recovery_tip_exists(repo, prefix):
                    recovery_prefix = prefix
            except Exception:
                recovery_prefix = prefix
        return BranchRemoval(Failed(name, f"{type(error).__name__}: {error}"), recovery_prefix)
    except BaseException as error:
        # If any post-transaction operation fails, surface the recovery refs in the partial
        # report instead of losing their location behind an escaped worker exception.
        if transaction_started and prefix is not None:
            if transaction_committed:
                return BranchRemoval(Failed(name, f"{type(error).__name__}: {error}"), prefix)
            try:
                if await _recovery_tip_exists(repo, prefix):
                    return BranchRemoval(Failed(name, f"{type(error).__name__}: {error}"), prefix)
            except Exception:
                return BranchRemoval(Failed(name, f"{type(error).__name__}: {error}"), prefix)
        raise
