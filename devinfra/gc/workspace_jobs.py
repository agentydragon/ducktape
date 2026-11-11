"""Typed work items and results for workspace garbage collection."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

from devinfra.gc import (
    branch_gc,
    foreign_clone_gc,
    git_repo,
    output_base_gc,
    repo_contents_cache_gc,
    workspace_scan,
    worktree_gc,
)
from devinfra.gc.async_io import run_blocking, run_process
from devinfra.gc.branch_gc import BranchClassification, Holder
from devinfra.gc.foreign_clone_gc import ForeignCloneClassification
from devinfra.gc.git_repo import Worktree
from devinfra.gc.output_base_gc import BaseLayout, Inspection, PrunableBase
from devinfra.gc.pull_request import PrLookup
from devinfra.gc.removal import RemovalResult
from devinfra.gc.repo_contents_cache_gc import (
    EntryInspection,
    PrunableEntry,
    ReferenceInventory,
    ReviewEntry,
    TrashedEntries,
)
from devinfra.gc.verdict import verdict_of
from devinfra.gc.work_queue import AsyncMutation, AsyncWorkItem, Finished, WorkItem
from devinfra.gc.worktree_gc import Classification, PrunableWorktree

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RepositoryFacts:
    repo: Path
    main_path: Path
    active_path: Path | None
    main: str
    default_branch: str
    prs: PrLookup


type PrSource = Callable[[Path, set[str]], Awaitable[PrLookup]]


@dataclass(frozen=True, slots=True)
class PreparedRepositoryScan:
    facts: RepositoryFacts
    pr_candidates: frozenset[str]


@dataclass(frozen=True, slots=True)
class PrBatchCompleted:
    branches: frozenset[str]
    lookup: PrLookup


@dataclass(frozen=True, slots=True)
class FoundForeignCloneRoots:
    roots: list[Path]


@dataclass(frozen=True, slots=True)
class FoundForeignCloneBranches:
    root: Path
    branches: frozenset[str]


@dataclass(frozen=True, slots=True)
class FoundBaseWorkspaces:
    facts: RepositoryFacts
    worktrees: list[Worktree]
    live: dict[Path, list[int]]


@dataclass(frozen=True, slots=True)
class InspectedBaseWorkspace:
    item: Classification
    resolved_path: Path | None


@dataclass(frozen=True, slots=True)
class FoundWorktrees:
    worktrees: list[Worktree]
    live: dict[Path, list[int]]


@dataclass(frozen=True, slots=True)
class InspectedWorktree:
    item: Classification
    resolved_path: Path | None


@dataclass(frozen=True, slots=True)
class RemovedWorktree:
    result: RemovalResult[Path]


@dataclass(frozen=True, slots=True)
class RemovedBranch:
    result: RemovalResult[str]
    recovery_prefix: str | None


@dataclass(frozen=True, slots=True)
class FoundBranches:
    names: list[str]
    holders: dict[str, Path]


@dataclass(frozen=True, slots=True)
class InspectedBranch:
    item: BranchClassification
    name: str
    holder: Holder
    candidate: bool = False


@dataclass(frozen=True, slots=True)
class FoundBases:
    bases: list[tuple[Path, BaseLayout]]
    points: set[Path]
    free_before: int | None


@dataclass(frozen=True, slots=True)
class InspectedBase:
    item: Inspection
    initial: bool
    resolved_workspace: Path | None


@dataclass(frozen=True, slots=True)
class MeasuredBase:
    path: Path
    size: int | None


@dataclass(frozen=True, slots=True)
class RemovedBase:
    result: RemovalResult[Path]
    size: int | None


@dataclass(frozen=True, slots=True)
class CacheFacts:
    references: ReferenceInventory
    points: set[Path]


@dataclass(frozen=True, slots=True)
class FoundCacheBuckets:
    buckets: list[Path]
    facts: CacheFacts
    free_before: int | None


@dataclass(frozen=True, slots=True)
class FoundCacheCandidates:
    candidates: list[Path | ReviewEntry]
    facts: CacheFacts


@dataclass(frozen=True, slots=True)
class InspectedCacheEntry:
    item: EntryInspection


@dataclass(frozen=True, slots=True)
class MeasuredCacheEntry:
    path: Path
    size: int | None


@dataclass(frozen=True, slots=True)
class TrashedCacheEntries:
    trashed: TrashedEntries
    sizes: dict[Path, int | None]


@dataclass(frozen=True, slots=True)
class RemovedCacheEntry:
    result: RemovalResult[Path]


@dataclass(frozen=True, slots=True)
class InspectedForeignClone:
    item: ForeignCloneClassification


type Event = (
    PreparedRepositoryScan
    | PrBatchCompleted
    | FoundForeignCloneRoots
    | FoundForeignCloneBranches
    | FoundBaseWorkspaces
    | InspectedBaseWorkspace
    | FoundWorktrees
    | InspectedWorktree
    | RemovedWorktree
    | RemovedBranch
    | FoundBranches
    | InspectedBranch
    | FoundBases
    | InspectedBase
    | MeasuredBase
    | RemovedBase
    | FoundCacheBuckets
    | FoundCacheCandidates
    | InspectedCacheEntry
    | MeasuredCacheEntry
    | TrashedCacheEntries
    | RemovedCacheEntry
    | InspectedForeignClone
)


async def _active_worktree(repo: Path) -> Path | None:
    toplevel = (await git_repo.git(repo, "rev-parse", "--show-toplevel", check=False)).stdout.strip()
    return Path(toplevel) if toplevel else None


def _free_space(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def resolve_worktree_pr(item: Classification, branch: str | None, lookup: PrLookup) -> Classification:
    """Apply the completed branch lookup to a local worktree classification.

    `InspectWorktree` deliberately runs without GitHub state. It still performs the expensive
    local inspection while a request is in flight; this pure step applies the same PR
    precedence once that branch's batch resolves.
    """
    if branch is None:
        return item
    pr = lookup.states.get(branch)
    if isinstance(item, PrunableWorktree):
        if branch in lookup.unknown:
            return worktree_gc.ReviewWorktree(
                item.worktree, worktree_gc.ReviewWorktreeReason.PR_LOOKUP_UNAVAILABLE, item.last_activity, item.main
            )
        if item.reason is worktree_gc.PrunableWorktreeReason.DIRECTORY_MISSING:
            return item
        if pr is not None and pr.state is worktree_gc.PrState.OPEN:
            return worktree_gc.RetainedWorktree(
                item.worktree, worktree_gc.RetainedWorktreeReason.OPEN_PR, item.last_activity, pr=pr
            )
        if pr is not None and pr.state in (worktree_gc.PrState.MERGED, worktree_gc.PrState.CLOSED):
            return worktree_gc.PrunableWorktree(
                item.worktree, worktree_gc.PrunableWorktreeReason.PR_LANDED, item.last_activity, item.main, pr=pr
            )
        return item
    if isinstance(item, worktree_gc.RetainedWorktree):
        if item.reason in (
            worktree_gc.RetainedWorktreeReason.UNCOMMITTED_CHANGES,
            worktree_gc.RetainedWorktreeReason.IGNORED_FILES,
        ):
            return replace(item, pr=pr)
        return item
    if item.reason is not worktree_gc.ReviewWorktreeReason.UNMERGED:
        return item
    if branch in lookup.unknown:
        return worktree_gc.ReviewWorktree(
            item.worktree, worktree_gc.ReviewWorktreeReason.PR_LOOKUP_UNAVAILABLE, item.last_activity, item.main
        )
    if pr is not None and pr.state is worktree_gc.PrState.OPEN:
        return worktree_gc.RetainedWorktree(
            item.worktree, worktree_gc.RetainedWorktreeReason.OPEN_PR, item.last_activity, pr=pr
        )
    if pr is not None and pr.state in (worktree_gc.PrState.MERGED, worktree_gc.PrState.CLOSED):
        return worktree_gc.PrunableWorktree(
            item.worktree, worktree_gc.PrunableWorktreeReason.PR_LANDED, item.last_activity, item.main, pr=pr
        )
    return item


async def _allocated_bytes(path: Path) -> int | None:
    try:
        result = await run_process("du", "-sx", "--block-size=1", "--", os.fspath(path))
        if result.returncode:
            return None
        return int(result.stdout.split(maxsplit=1)[0])
    except OSError, ValueError, IndexError:
        return None


async def _repository_facts(repo: Path, prs: PrLookup) -> RepositoryFacts:
    def refs() -> tuple[str, str]:
        pg = git_repo.open_repo(repo)
        return git_repo.main_ref(pg), git_repo.default_branch_name(pg)

    main_path = await git_repo.main_worktree(repo)
    active_path = await _active_worktree(repo)
    main, default_branch = await run_blocking(refs)
    return RepositoryFacts(repo, main_path, active_path, main, default_branch, prs)


@dataclass(frozen=True, slots=True)
class PrepareRepositoryScan(AsyncWorkItem[Event]):
    repo: Path
    phase: ClassVar[str] = "discover git state"

    async def run_async(self) -> Finished[Event]:
        # Repository facts and candidate discovery are local Git work. Publish both without
        # waiting for GitHub so the coordinator can start the local inventory immediately.
        async with asyncio.TaskGroup() as tasks:
            facts_task = tasks.create_task(_repository_facts(self.repo, PrLookup({}, disabled=True)))
            names_task = tasks.create_task(workspace_scan.pr_branch_candidates(self.repo))
        facts, names = facts_task.result(), names_task.result()
        return Finished(PreparedRepositoryScan(facts, frozenset(names)), "done")


@dataclass(frozen=True, slots=True)
class LookupPrBatch(AsyncWorkItem[Event]):
    repo: Path
    branches: frozenset[str]
    pr_source: PrSource
    phase: ClassVar[str] = "check pull requests"

    async def run_async(self) -> Finished[Event]:
        try:
            lookup = await self.pr_source(self.repo, set(self.branches))
        except Exception as error:
            # A failed batch is unknown, never a successful lookup with no PR.
            logger.warning("PR check failed for %d branches: %s", len(self.branches), error)
            lookup = PrLookup({}, self.branches)
        return Finished(PrBatchCompleted(self.branches, lookup), "done")


@dataclass(frozen=True, slots=True)
class DiscoverForeignWorkspace(AsyncWorkItem[Event]):
    repo: Path
    workspace: Path
    phase: ClassVar[str] = "discover foreign clones"

    async def run_async(self) -> Finished[Event]:
        workspace = await run_blocking(workspace_scan.resolve_path, self.workspace)
        if workspace is None:
            return Finished(FoundForeignCloneRoots([]), "done")
        known = await workspace_scan.known_worktree_paths(self.repo)
        roots = await foreign_clone_gc.discover_foreign_clones({workspace}, known_paths=known, repo=self.repo)
        return Finished(FoundForeignCloneRoots(roots), "done")


@dataclass(frozen=True, slots=True)
class DiscoverForeignCloneBranches(AsyncWorkItem[Event]):
    root: Path
    phase: ClassVar[str] = "discover foreign clone branches"

    async def run_async(self) -> Finished[Event]:
        names = await workspace_scan.foreign_clone_branch_candidates((self.root,))
        return Finished(FoundForeignCloneBranches(self.root, frozenset(names)), "done")


@dataclass(frozen=True, slots=True)
class DiscoverBaseWorkspaces(AsyncWorkItem[Event]):
    repo: Path
    bases: tuple[Inspection, ...]
    phase: ClassVar[str] = "discover base workspaces"

    async def run_async(self) -> Finished[Event]:
        worktrees = await workspace_scan.base_worktrees(self.repo, self.bases)
        facts = await _repository_facts(self.repo, PrLookup({}, disabled=True))
        live = await run_blocking(worktree_gc.processes_by_worktree, [wt.path for wt in worktrees])
        return Finished(FoundBaseWorkspaces(facts, worktrees, live), "done")


@dataclass(frozen=True, slots=True)
class DiscoverWorktrees(AsyncWorkItem[Event]):
    facts: RepositoryFacts
    phase: ClassVar[str] = "discover"

    async def run_async(self) -> Finished[Event]:
        worktrees = [wt for wt in await git_repo.list_worktrees(self.facts.repo) if wt.path != self.facts.main_path]
        live = await run_blocking(worktree_gc.processes_by_worktree, (wt.path for wt in worktrees))
        return Finished(FoundWorktrees(worktrees, live), "done")


@dataclass(frozen=True, slots=True)
class InspectWorktree(AsyncWorkItem[Event]):
    facts: RepositoryFacts
    worktree: Worktree
    live: list[int]
    phase: ClassVar[str] = "inspect worktrees"

    async def run_async(self) -> Finished[Event]:
        item = await _inspect_worktree(self.facts, self.worktree, self.live)
        resolved = await run_blocking(workspace_scan.resolve_path, self.worktree.path)
        return Finished(InspectedWorktree(item, resolved), verdict_of(item).lower())


async def _inspect_worktree(facts: RepositoryFacts, worktree: Worktree, live: list[int]) -> Classification:
    item = await worktree_gc.classify_worktree(
        worktree,
        main=facts.main,
        pr_states=facts.prs.states,
        main_path=facts.main_path,
        active_path=facts.active_path,
        live_pids=live,
    )
    if isinstance(item, PrunableWorktree) and worktree.branch in facts.prs.unknown:
        return worktree_gc.ReviewWorktree(
            worktree, worktree_gc.ReviewWorktreeReason.PR_LOOKUP_UNAVAILABLE, item.last_activity, item.main
        )
    return item


@dataclass(frozen=True, slots=True)
class InspectBaseWorkspace(AsyncWorkItem[Event]):
    facts: RepositoryFacts
    worktree: Worktree
    live: list[int]
    phase: ClassVar[str] = "inspect base workspaces"

    async def run_async(self) -> Finished[Event]:
        item = await _inspect_worktree(self.facts, self.worktree, self.live)
        resolved = await run_blocking(workspace_scan.resolve_path, self.worktree.path)
        return Finished(InspectedBaseWorkspace(item, resolved), verdict_of(item).lower())


@dataclass(frozen=True, slots=True)
class RemoveWorktree(AsyncMutation[Event]):
    facts: RepositoryFacts
    item: PrunableWorktree
    phase: ClassVar[str] = "remove worktrees"

    async def run_async(self) -> Finished[Event]:
        live = await run_blocking(worktree_gc.processes_by_worktree, [self.item.worktree.path])
        result = await worktree_gc.remove_if_still_prunable(
            self.facts.repo,
            self.item.worktree,
            main=self.item.main,
            pr_states=self.facts.prs.states,
            main_path=self.facts.main_path,
            active_path=self.facts.active_path,
            live_pids=live.get(self.item.worktree.path, []),
        )
        return Finished(RemovedWorktree(result), type(result).__name__.lower())


@dataclass(frozen=True, slots=True)
class DiscoverBranches(AsyncWorkItem[Event]):
    facts: RepositoryFacts
    phase: ClassVar[str] = "discover"

    async def run_async(self) -> Finished[Event]:
        names = await run_blocking(lambda: branch_gc.local_branches(git_repo.open_repo(self.facts.main_path)))
        return Finished(FoundBranches(names, await branch_gc.branch_holders(self.facts.repo)), "done")


@dataclass(frozen=True, slots=True)
class InspectBranch(AsyncWorkItem[Event]):
    facts: RepositoryFacts
    name: str
    holder: Holder
    final: bool = False
    phase: ClassVar[str] = "inspect branches"

    async def run_async(self) -> Finished[Event]:
        pg = await run_blocking(git_repo.open_repo, self.facts.main_path)
        item = await branch_gc.classify_branch(
            self.name,
            pg=pg,
            main=self.facts.main,
            default_branch=self.facts.default_branch,
            pr=self.facts.prs.states.get(self.name),
            holder=self.holder,
        )
        if self.final and isinstance(item, branch_gc.PrunableBranch) and self.name in self.facts.prs.unknown:
            item = branch_gc.ReviewBranch(item.branch, branch_gc.ReviewBranchReason.PR_LOOKUP_UNAVAILABLE, item.main)
        return Finished(
            InspectedBranch(item, self.name, self.holder, candidate=not self.final), verdict_of(item).lower()
        )


@dataclass(frozen=True, slots=True)
class RemoveBranch(AsyncMutation[Event]):
    facts: RepositoryFacts
    item: branch_gc.PrunableBranch
    phase: ClassVar[str] = "remove branches"

    async def run_async(self) -> Finished[Event]:
        # An admitted ref mutation must finish and publish its recovery location even when
        # queue shutdown cancels this worker task.
        operation = asyncio.create_task(
            branch_gc.remove_prunable_branch(
                self.facts.repo, self.item, main=self.item.main, default_branch=self.facts.default_branch
            )
        )
        while True:
            try:
                outcome = await asyncio.shield(operation)
                break
            except asyncio.CancelledError:
                continue
        result = outcome.result
        return Finished(RemovedBranch(result, outcome.recovery_prefix), type(result).__name__.lower())


@dataclass(frozen=True, slots=True)
class DiscoverBases:
    root: Path
    session_bases: tuple[Path, ...]
    phase: ClassVar[str] = "discover"

    def run(self) -> Finished[Event]:
        return Finished(
            FoundBases(
                output_base_gc.list_output_bases(self.root, session_bases=self.session_bases),
                output_base_gc.mount_points(),
                _free_space(self.root),
            ),
            "done",
        )


@dataclass(frozen=True, slots=True)
class InspectBase:
    path: Path
    layout: BaseLayout
    points: set[Path]
    initial: bool = True
    phase: ClassVar[str] = "inspect bases"

    def run(self) -> Finished[Event]:
        item = output_base_gc.inspect_listed_base(self.path, layout=self.layout, uid=os.getuid(), points=self.points)
        resolved = (
            workspace_scan.resolve_path(item.workspace)
            if isinstance(item, output_base_gc.RetainedBase) and item.workspace is not None
            else None
        )
        return Finished(InspectedBase(item, self.initial, resolved), verdict_of(item).lower())


@dataclass(frozen=True, slots=True)
class MeasureBase(AsyncWorkItem[Event]):
    path: Path
    phase: ClassVar[str] = "measure bases"

    async def run_async(self) -> Finished[Event]:
        return Finished(MeasuredBase(self.path, await _allocated_bytes(self.path)), "done")


@dataclass(frozen=True, slots=True)
class RemoveBase:
    item: PrunableBase
    size: int | None
    phase: ClassVar[str] = "delete bases"

    def run(self) -> Finished[Event]:
        result = output_base_gc.delete_prunable_base(self.item)
        return Finished(RemovedBase(result, self.size), type(result).__name__.lower())


@dataclass(frozen=True, slots=True)
class DiscoverCache:
    contents: Path
    base_paths: tuple[Path, ...]
    phase: ClassVar[str] = "discover repo cache"

    def run(self) -> Finished[Event]:
        try:
            contents = self.contents.resolve(strict=True)
        except FileNotFoundError:
            return Finished(FoundCacheBuckets([], CacheFacts(ReferenceInventory({}, ()), set()), None), "done")
        buckets = repo_contents_cache_gc.cache_buckets(contents)
        references = repo_contents_cache_gc.references(self.base_paths, contents)
        facts = CacheFacts(references, output_base_gc.mount_points())
        return Finished(FoundCacheBuckets(buckets, facts, _free_space(contents)), "done")


@dataclass(frozen=True, slots=True)
class DiscoverCacheBucket:
    path: Path
    facts: CacheFacts
    phase: ClassVar[str] = "discover repo-cache buckets"

    def run(self) -> Finished[Event]:
        candidates = repo_contents_cache_gc.list_cache_bucket(self.path)
        return Finished(FoundCacheCandidates(candidates, self.facts), "done")


@dataclass(frozen=True, slots=True)
class InspectCacheEntry:
    candidate: Path | ReviewEntry
    facts: CacheFacts
    phase: ClassVar[str] = "inspect repo-cache entries"

    def run(self) -> Finished[Event]:
        item = repo_contents_cache_gc.inspect_cache_candidate(self.candidate, uid=os.getuid(), points=self.facts.points)
        inspection = repo_contents_cache_gc.classify_entry(item, self.facts.references, set())
        return Finished(InspectedCacheEntry(inspection), verdict_of(inspection).lower())


@dataclass(frozen=True, slots=True)
class MeasureCacheEntry(AsyncWorkItem[Event]):
    path: Path
    phase: ClassVar[str] = "measure cache entries"

    async def run_async(self) -> Finished[Event]:
        return Finished(MeasuredCacheEntry(self.path, await _allocated_bytes(self.path)), "done")


@dataclass(frozen=True, slots=True)
class TrashCacheEntries:
    entries: tuple[PrunableEntry, ...]
    contents: Path
    root: Path
    session_bases: tuple[Path, ...]
    sizes: dict[Path, int | None]
    phase: ClassVar[str] = "trash cache batches"

    def run(self) -> Finished[Event]:
        trashed = repo_contents_cache_gc.trash_prunable_entries(
            self.entries, contents=self.contents, output_user_root=self.root, session_bases=self.session_bases
        )
        return Finished(TrashedCacheEntries(trashed, self.sizes), "done")


@dataclass(frozen=True, slots=True)
class EmptyCacheTrash:
    path: Path
    trash: Path
    phase: ClassVar[str] = "delete cache entries"

    def run(self) -> Finished[Event]:
        result = repo_contents_cache_gc.empty_trash(self.path, self.trash)
        return Finished(RemovedCacheEntry(result), type(result).__name__.lower())


@dataclass(frozen=True, slots=True)
class InspectForeignClone(AsyncWorkItem[Event]):
    facts: RepositoryFacts
    root: Path
    phase: ClassVar[str] = "inspect foreign clones"

    async def run_async(self) -> Finished[Event]:
        item = await foreign_clone_gc.classify_foreign_clone(
            self.root, pr_states=self.facts.prs.states, active_path=self.facts.active_path
        )
        if not isinstance(item, foreign_clone_gc.RetainedForeignClone | foreign_clone_gc.ReviewForeignClone) and any(
            name in self.facts.prs.unknown for name in (member.branch for member in item.clone.members if member.branch)
        ):
            item = foreign_clone_gc.ReviewForeignClone(item.clone, "pull request state could not be determined")
        return Finished(InspectedForeignClone(item), verdict_of(item).lower())


def describe(item: WorkItem[Event]) -> str:
    """Identify a job without logging its facts or pull request data."""
    if isinstance(item, PrepareRepositoryScan | DiscoverBaseWorkspaces):
        return str(item.repo)
    if isinstance(item, DiscoverForeignWorkspace):
        return str(item.workspace)
    if isinstance(item, DiscoverWorktrees | DiscoverBranches):
        return str(item.facts.repo)
    if isinstance(item, InspectWorktree | InspectBaseWorkspace):
        return str(item.worktree.path)
    if isinstance(item, RemoveWorktree):
        return str(item.item.worktree.path)
    if isinstance(item, RemoveBranch):
        return item.item.branch.name
    if isinstance(item, InspectBranch):
        return item.name
    if isinstance(item, DiscoverBases | InspectForeignClone | DiscoverForeignCloneBranches):
        return str(item.root)
    if isinstance(item, InspectBase | MeasureBase | MeasureCacheEntry | DiscoverCacheBucket | EmptyCacheTrash):
        return str(item.path)
    if isinstance(item, InspectCacheEntry):
        return str(item.candidate if isinstance(item.candidate, Path) else item.candidate.path)
    if isinstance(item, RemoveBase):
        return str(item.item.path)
    if isinstance(item, DiscoverCache):
        return str(item.contents)
    if isinstance(item, TrashCacheEntries):
        return f"{len(item.entries)} entries"
    return type(item).__name__
