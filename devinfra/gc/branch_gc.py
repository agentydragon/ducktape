"""Classify and safely remove local git branches whose work is already merged.

Mirrors worktree_gc's PRUNE/KEEP/REVIEW model, one level up: a *branch* is prunable when
deleting its ref loses nothing, established by either

  * git — its tip adds nothing not already in the default branch (an ancestor, an empty
    branch, or a squash/rebase-merge no-op), reusing `git_repo.content_in_main`;
  * git again, by patch equivalence — every commit it adds has a twin already on the default
    branch (`git_repo.patches_landed_in_main`). This catches the rebase-merged and
    cherry-picked branches whose tree-equality check has decayed: the merge base is thousands
    of commits back, so merging today conflicts even though the work landed long ago; or
  * a merged GitHub PR whose merged tip the branch has not advanced beyond — the
    squash-merge case that survives even when later default-branch divergence defeats the
    git tree-equality check.

A branch checked out in a worktree cannot be deleted while that worktree exists (git refuses
even `-D`), so classification depends on the worktree scan: a branch held by a retained
worktree is kept, one held by a prunable worktree is eligible but only deletable once that
worktree is gone. Deletion uses `git branch -D` (git's safe `-d` rejects squash-merges) after
re-proving the branch is still prunable — the same revalidate-then-act contract worktree and
output-base removal use. The default branch is never a candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pygit2

from devinfra.gc import git_repo
from devinfra.gc.git_repo import content_in_main, patches_landed_in_main
from devinfra.gc.pull_request import PrInfo, PrState, pr_phrase
from devinfra.gc.worktree_gc import Classification, PrunableWorktree, RetainedWorktree, ReviewWorktree


@dataclass(frozen=True, slots=True)
class Branch:
    name: str


@dataclass(frozen=True, slots=True)
class MainCheckout:
    """Sentinel holder: the branch is checked out in the repo's main worktree."""


type Holder = Classification | MainCheckout | None


@dataclass(frozen=True, slots=True)
class PrunableBranch:
    branch: Branch
    reason: str
    checkout: Path | None  # the prunable worktree holding it, removed before the branch


@dataclass(frozen=True, slots=True)
class RetainedBranch:
    branch: Branch
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewBranch:
    branch: Branch
    reason: str


type BranchClassification = PrunableBranch | RetainedBranch | ReviewBranch


def local_branches(pg: pygit2.Repository) -> list[str]:
    return sorted(pg.branches.local)


def branch_holders(repo: Path) -> dict[str, Path]:
    """Each branch that is checked out, mapped to the worktree holding it (main included)."""
    return {wt.branch: wt.path for wt in git_repo.list_worktrees(repo) if wt.branch}


def _tip_within_merged_head(pg: pygit2.Repository, branch_oid: pygit2.Oid, pr: PrInfo) -> bool:
    """True when the branch tip is at, or an ancestor of, the SHA the PR merged.

    That means the branch added nothing past the squash-merge point. Needs the merged head
    object present locally to reason about ancestry; if it was never fetched, defer.
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


def classify_branch(
    name: str, *, pg: pygit2.Repository, main: str, default_branch: str, pr: PrInfo | None, holder: Holder
) -> BranchClassification:
    branch = Branch(name)

    if name == default_branch:
        return RetainedBranch(branch, "default branch")
    if isinstance(holder, MainCheckout):
        return RetainedBranch(branch, "checked out in main checkout")
    if isinstance(holder, RetainedWorktree | ReviewWorktree):
        return RetainedBranch(branch, f"checked out in retained worktree {holder.worktree.path}")
    if pr is not None and pr.state is PrState.OPEN:
        return RetainedBranch(branch, pr_phrase(pr))

    # holder is None or a PrunableWorktree (removed before the branch is deleted).
    checkout = holder.worktree.path if isinstance(holder, PrunableWorktree) else None
    branch_oid = pg.branches.local[name].peel(pygit2.Commit).id
    if content_in_main(pg, branch_oid, main):
        annotation = f" ({pr_phrase(pr)})" if pr is not None and pr.state is PrState.MERGED else ""
        return PrunableBranch(branch, f"changes already in {main}{annotation}", checkout)
    if pr is not None and pr.state is PrState.MERGED and _tip_within_merged_head(pg, branch_oid, pr):
        return PrunableBranch(branch, f"{pr_phrase(pr)}; nothing beyond the merged head", checkout)
    # Last, because it is the only check here that shells out: ~200ms, worth paying for the
    # handful of branches otherwise bound for REVIEW but not for every branch in the repo.
    # The cheaper tests above also name a more specific reason when they apply.
    if patches_landed_in_main(Path(pg.path), name, main):
        return PrunableBranch(branch, f"every commit has an equivalent already on {main}", checkout)
    if pr is not None and pr.state is PrState.MERGED:
        return ReviewBranch(branch, f"{pr_phrase(pr)} but branch has commits beyond it")
    return ReviewBranch(branch, f"commits not in {main}")


@dataclass(frozen=True, slots=True)
class RemovedBranch:
    name: str


@dataclass(frozen=True, slots=True)
class SkippedBranch:
    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class FailedBranch:
    name: str
    error: str


type BranchRemovalResult = RemovedBranch | SkippedBranch | FailedBranch


def delete_branch(repo: Path, name: str) -> BranchRemovalResult:
    """Force-delete one branch with `git branch -D` (git's safe `-d` rejects squash-merges).

    Safe because the caller re-scans and only passes still-prunable branches — its content is
    proven to be in main. `git branch` refuses a branch still checked out in any worktree, so
    the caller must remove a holding worktree first.
    """
    outcome = git_repo.git(repo, "branch", "-D", name, check=False)
    if outcome.returncode == 0:
        return RemovedBranch(name)
    return FailedBranch(name, outcome.stderr.strip() or "git branch -D failed")
