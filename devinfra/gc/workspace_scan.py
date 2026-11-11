"""Shared worktree, base, and foreign-clone discovery helpers."""

from __future__ import annotations

import dataclasses
import logging
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from devinfra.gc import branch_gc, foreign_clone_gc, git_repo, output_base_gc
from devinfra.gc.async_io import run_blocking
from devinfra.gc.branch_gc import BranchClassification
from devinfra.gc.foreign_clone_gc import ForeignCloneClassification
from devinfra.gc.output_base_gc import Inspection, RetainedBase
from devinfra.gc.worktree_gc import Classification

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkspaceScan:
    worktrees: list[Classification]
    branches: list[BranchClassification]
    bases: list[Inspection]
    foreign_clones: list[ForeignCloneClassification] = field(default_factory=list)


async def pr_branch_candidates(repo: Path) -> set[str]:
    """Branch names worth a GitHub PR lookup: every local branch plus any checked out."""
    main = await git_repo.main_worktree(repo)
    names = await run_blocking(lambda: set(branch_gc.local_branches(git_repo.open_repo(main))))
    names.update(wt.branch for wt in await git_repo.list_worktrees(repo) if wt.branch)
    return names


def _resolve(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def _resolved(paths: set[Path]) -> set[Path]:
    return {resolved for path in paths if (resolved := _resolve(path)) is not None}


def annotate_base(base: Inspection, prunable_workspaces: set[Path]) -> Inspection:
    """Flag a retained base whose workspace is a prunable worktree — it orphans once removed."""
    if not isinstance(base, RetainedBase) or base.workspace is None:
        return base
    try:
        workspace = base.workspace.resolve()
    except OSError:
        return base
    if workspace not in prunable_workspaces:
        return base
    return dataclasses.replace(base, workspace_is_prunable_worktree=True)


def _retained_workspaces(bases: Iterable[Inspection]) -> set[Path]:
    return _resolved(
        {base.workspace for base in bases if isinstance(base, RetainedBase) and base.workspace is not None}
    )


async def _known_worktree_paths(repo: Path) -> set[Path]:
    return await run_blocking(_resolved, {wt.path for wt in await git_repo.list_worktrees(repo)})


async def foreign_clone_roots(repo: Path, output_user_root: Path, *, session_bases: Sequence[Path]) -> list[Path]:
    """Roots of foreign clones of `repo`'s project found among Bazel output-base workspaces.

    Filesystem-only (no network): inspect output bases to find candidate workspace paths
    before requesting PR state for the foreign clones' branches.
    """
    # TODO: Add an age-based `/tmp` review pass for same-project Git clone roots that are not
    # discoverable through a Bazel output base. Dirty/unique-work and live-process checks must
    # continue to govern removal.
    bases = await run_blocking(output_base_gc.scan_output_user_root, output_user_root, session_bases=session_bases)
    workspaces = await run_blocking(_retained_workspaces, bases)
    return await foreign_clone_gc.discover_foreign_clones(
        workspaces, known_paths=await _known_worktree_paths(repo), repo=repo
    )


async def foreign_clone_branch_candidates(roots: Sequence[Path]) -> set[str]:
    """Branch names worth a PR lookup across every worktree these foreign clones hold."""
    names: set[str] = set()
    for root in roots:
        try:
            names.update(wt.branch for wt in await git_repo.list_worktrees(root) if wt.branch)
        except (OSError, subprocess.SubprocessError) as error:
            logger.warning("Cannot read PR branch candidates from %s: %s", root, error)
            continue
    return names


async def base_worktrees(repo: Path, bases: Iterable[Inspection]) -> list[git_repo.Worktree]:
    """Linked worktrees that are workspaces of retained bases."""
    workspaces = await run_blocking(_retained_workspaces, bases)
    if not workspaces:
        return []
    main_path = await git_repo.main_worktree(repo)
    worktrees = await git_repo.list_worktrees(repo)
    return await run_blocking(
        lambda: [wt for wt in worktrees if wt.path != main_path and _resolve(wt.path) in workspaces]
    )
