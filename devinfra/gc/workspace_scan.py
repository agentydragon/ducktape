"""The single joint scan over local development state — worktrees, branches, output bases.

The three domains are coupled, so they are classified in one flow rather than three
independent passes: worktrees are the root; a branch's fate depends on the worktree holding
it, and a base's on whether its workspace (a worktree) survives. `scan_workspace` classifies
worktrees first, then branches against that result, then bases (annotating a retained base
whose workspace is a prunable worktree). It is network-free — PR state is injected — so the
CLI owns GitHub access and this stays unit-testable offline.

`annotate_bases` is the bases-only path. A base's own PRUNE/KEEP verdict is decided by
`output_base_gc` from the filesystem alone; the joint scan is needed only for the annotation
on a *retained* base whose workspace is a prunable worktree. So the caller inspects the bases
first and this classifies just the handful of worktrees that are some base's workspace —
never the whole repo, and never any branch. On a checkout with a hundred worktrees that is
the difference between five seconds and forty.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pygit2

from devinfra.gc import branch_gc, git_repo, output_base_gc, worktree_gc
from devinfra.gc.branch_gc import BranchClassification, Holder, MainCheckout
from devinfra.gc.output_base_gc import Inspection, RetainedBase
from devinfra.gc.pull_request import PrInfo
from devinfra.gc.worktree_gc import Classification, PrunableWorktree

_BRANCH_WORKERS = 8  # content_in_main runs pygit2 merges (GIL released), so threads help


@dataclass(frozen=True, slots=True)
class WorkspaceScan:
    worktrees: list[Classification]
    branches: list[BranchClassification]
    bases: list[Inspection]


def pr_branch_candidates(repo: Path) -> set[str]:
    """Branch names worth a GitHub PR lookup: every local branch plus any checked out."""
    pg = pygit2.Repository(os.fspath(git_repo.main_worktree(repo)))
    names = set(branch_gc.local_branches(pg))
    names.update(wt.branch for wt in git_repo.list_worktrees(repo) if wt.branch)
    return names


def _resolve(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def _resolved(paths: set[Path]) -> set[Path]:
    return {resolved for path in paths if (resolved := _resolve(path)) is not None}


def _annotate_base(base: Inspection, prunable_workspaces: set[Path]) -> Inspection:
    """Flag a retained base whose workspace is a prunable worktree — it orphans once removed."""
    if not isinstance(base, RetainedBase) or base.workspace is None:
        return base
    try:
        workspace = base.workspace.resolve()
    except OSError:
        return base
    if workspace not in prunable_workspaces:
        return base
    return dataclasses.replace(base, reason=f"{base.reason} — workspace is a prunable worktree (prune it first)")


def _classify_branches(
    main_path: Path,
    names: list[str],
    *,
    main: str,
    default_branch: str,
    pr_states: dict[str, PrInfo],
    holder_for: Callable[[str], Holder],
) -> list[BranchClassification]:
    """Classify every local branch, parallelizing the pygit2 content-in-main merges.

    Each worker opens its own `pygit2.Repository` — handles aren't shareable across threads —
    and classifies a contiguous slice, so the flattened result stays in `names` order.
    """

    def classify_slice(slice_names: list[str]) -> list[BranchClassification]:
        pg = pygit2.Repository(os.fspath(main_path))
        return [
            branch_gc.classify_branch(
                name, pg=pg, main=main, default_branch=default_branch, pr=pr_states.get(name), holder=holder_for(name)
            )
            for name in slice_names
        ]

    workers = min(_BRANCH_WORKERS, len(names))
    if workers <= 1:
        return classify_slice(names)
    step = -(-len(names) // workers)  # ceil → `workers` contiguous slices
    slices = [names[i : i + step] for i in range(0, len(names), step)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [item for chunk in pool.map(classify_slice, slices) for item in chunk]


def base_workspace_branches(repo: Path, bases: list[Inspection]) -> set[str]:
    """Branch names worth a PR lookup for a bases-only scan: those of base workspaces.

    Only a worktree that is some retained base's workspace can change a base's annotation,
    so the bases view queries PR state for those branches instead of every local branch.
    """
    workspaces = _retained_workspaces(bases)
    return {wt.branch for wt in _worktrees_at(repo, workspaces) if wt.branch}


def _retained_workspaces(bases: list[Inspection]) -> set[Path]:
    return _resolved(
        {base.workspace for base in bases if isinstance(base, RetainedBase) and base.workspace is not None}
    )


def _worktrees_at(repo: Path, workspaces: set[Path]) -> list[git_repo.Worktree]:
    """The linked worktrees sitting at one of `workspaces` (already resolved)."""
    if not workspaces:
        return []
    main_path = git_repo.main_worktree(repo)
    return [wt for wt in git_repo.list_worktrees(repo) if wt.path != main_path and _resolve(wt.path) in workspaces]


def annotate_bases(
    repo: Path,
    bases: list[Inspection],
    *,
    main: str,
    pr_states: dict[str, PrInfo],
    active_path: Path | None = None,
    proc_root: Path = Path("/proc"),
) -> list[Inspection]:
    """Flag each retained base whose workspace is a prunable worktree.

    Same annotation `scan_workspace` applies, but it classifies only the worktrees that are
    some base's workspace — no other worktree, and no branch, can change the outcome.
    """
    candidates = _worktrees_at(repo, _retained_workspaces(bases))
    if not candidates:
        return bases

    main_path = git_repo.main_worktree(repo)
    live = worktree_gc.processes_by_worktree((wt.path for wt in candidates), proc_root=proc_root)
    prunable = _resolved(
        {
            wt.path
            for wt in candidates
            if isinstance(
                worktree_gc.classify_worktree(
                    wt,
                    main=main,
                    pr_states=pr_states,
                    main_path=main_path,
                    active_path=active_path,
                    live_pids=live.get(wt.path, []),
                ),
                PrunableWorktree,
            )
        }
    )
    return [_annotate_base(base, prunable) for base in bases]


def scan_workspace(
    repo: Path,
    *,
    main: str,
    default_branch: str,
    pr_states: dict[str, PrInfo],
    active_path: Path | None = None,
    output_user_root: Path | None = None,
    proc_root: Path = Path("/proc"),
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> WorkspaceScan:
    main_path = git_repo.main_worktree(repo)
    pg = pygit2.Repository(os.fspath(main_path))

    linked = [wt for wt in git_repo.list_worktrees(repo) if wt.path != main_path]
    live = worktree_gc.processes_by_worktree((wt.path for wt in linked), proc_root=proc_root)
    worktrees = [
        worktree_gc.classify_worktree(
            wt,
            main=main,
            pr_states=pr_states,
            main_path=main_path,
            active_path=active_path,
            live_pids=live.get(wt.path, []),
        )
        for wt in linked
    ]

    holders = branch_gc.branch_holders(repo)
    holder_by_path = {item.worktree.path: item for item in worktrees}

    def holder_for(name: str) -> Holder:
        holder_path = holders.get(name)
        if holder_path is None:
            return None
        if holder_path == main_path:
            return MainCheckout()
        return holder_by_path.get(holder_path)

    branches = _classify_branches(
        main_path,
        branch_gc.local_branches(pg),
        main=main,
        default_branch=default_branch,
        pr_states=pr_states,
        holder_for=holder_for,
    )

    bases: list[Inspection] = []
    if output_user_root is not None:
        prunable_workspaces = _resolved(
            {item.worktree.path for item in worktrees if isinstance(item, PrunableWorktree)}
        )
        bases = [
            _annotate_base(base, prunable_workspaces)
            for base in output_base_gc.scan_output_user_root(
                output_user_root, proc_root=proc_root, mountinfo_path=mountinfo_path
            )
        ]

    return WorkspaceScan(worktrees=worktrees, branches=branches, bases=bases)
