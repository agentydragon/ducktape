"""Typed work items and results for workspace garbage collection."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
from devinfra.gc.async_io import run_blocking
from devinfra.gc.branch_gc import BranchClassification, Holder
from devinfra.gc.foreign_clone_gc import ForeignCloneClassification, PrunableForeignClone
from devinfra.gc.git_repo import Worktree
from devinfra.gc.output_base_gc import BaseLayout, Inspection, PrunableBase
from devinfra.gc.pull_request import PrInfo
from devinfra.gc.removal import RemovalResult, Skipped
from devinfra.gc.repo_contents_cache_gc import EntryInspection, PrunableEntry, ReviewEntry, TrashedEntries
from devinfra.gc.verdict import verdict_of
from devinfra.gc.work_queue import AsyncMutation, AsyncWorkItem, Finished, WorkItem
from devinfra.gc.worktree_gc import Classification, PrunableWorktree


@dataclass(frozen=True, slots=True)
class RepositoryFacts:
    repo: Path
    main_path: Path
    active_path: Path | None
    main: str
    default_branch: str
    prs: dict[str, PrInfo]


type PrSource = Callable[[Path, set[str]], Awaitable[dict[str, PrInfo]]]


@dataclass(frozen=True, slots=True)
class PreparedRepositoryScan:
    facts: RepositoryFacts
    foreign_roots: list[Path]


@dataclass(frozen=True, slots=True)
class FoundBaseWorkspaces:
    facts: RepositoryFacts
    worktrees: list[Worktree]
    live: dict[Path, list[int]]


@dataclass(frozen=True, slots=True)
class InspectedBaseWorkspace:
    item: Classification


@dataclass(frozen=True, slots=True)
class FoundWorktrees:
    worktrees: list[Worktree]
    live: dict[Path, list[int]]


@dataclass(frozen=True, slots=True)
class InspectedWorktree:
    item: Classification


@dataclass(frozen=True, slots=True)
class RemovedWorktree:
    result: RemovalResult[Path]


@dataclass(frozen=True, slots=True)
class FoundBranches:
    names: list[str]
    holders: dict[str, Path]


@dataclass(frozen=True, slots=True)
class InspectedBranch:
    item: BranchClassification


@dataclass(frozen=True, slots=True)
class RemovedBranch:
    result: RemovalResult[str]


@dataclass(frozen=True, slots=True)
class FoundBases:
    bases: list[tuple[Path, BaseLayout]]
    points: set[Path]


@dataclass(frozen=True, slots=True)
class InspectedBase:
    item: Inspection
    initial: bool


@dataclass(frozen=True, slots=True)
class RemovedBase:
    result: RemovalResult[Path]
    size: int | None


@dataclass(frozen=True, slots=True)
class CacheFacts:
    referrers: dict[Path, set[Path]]
    points: set[Path]


@dataclass(frozen=True, slots=True)
class FoundCacheBuckets:
    buckets: list[Path]
    facts: CacheFacts


@dataclass(frozen=True, slots=True)
class FoundCacheCandidates:
    candidates: list[Path | ReviewEntry]
    facts: CacheFacts


@dataclass(frozen=True, slots=True)
class InspectedCacheEntry:
    item: EntryInspection


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


@dataclass(frozen=True, slots=True)
class RemovedForeignClone:
    result: RemovalResult[Path]


type Event = (
    PreparedRepositoryScan
    | FoundBaseWorkspaces
    | InspectedBaseWorkspace
    | FoundWorktrees
    | InspectedWorktree
    | RemovedWorktree
    | FoundBranches
    | InspectedBranch
    | RemovedBranch
    | FoundBases
    | InspectedBase
    | RemovedBase
    | FoundCacheBuckets
    | FoundCacheCandidates
    | InspectedCacheEntry
    | TrashedCacheEntries
    | RemovedCacheEntry
    | InspectedForeignClone
    | RemovedForeignClone
)


async def _active_worktree(repo: Path) -> Path | None:
    toplevel = (await git_repo.git(repo, "rev-parse", "--show-toplevel", check=False)).stdout.strip()
    return Path(toplevel) if toplevel else None


async def _repository_facts(repo: Path, prs: dict[str, PrInfo]) -> RepositoryFacts:
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
    root: Path
    session_bases: tuple[Path, ...]
    foreign: bool
    pr_source: PrSource
    phase: ClassVar[str] = "discover git state"

    async def run_async(self) -> Finished[Event]:
        roots = (
            await workspace_scan.foreign_clone_roots(self.repo, self.root, session_bases=self.session_bases)
            if self.foreign
            else []
        )
        names = await workspace_scan.pr_branch_candidates(self.repo)
        names |= await workspace_scan.foreign_clone_branch_candidates(roots)
        prs = await self.pr_source(self.repo, names)
        return Finished(PreparedRepositoryScan(await _repository_facts(self.repo, prs), roots), "done")


@dataclass(frozen=True, slots=True)
class DiscoverBaseWorkspaces(AsyncWorkItem[Event]):
    repo: Path
    bases: tuple[Inspection, ...]
    pr_source: PrSource
    phase: ClassVar[str] = "discover base workspaces"

    async def run_async(self) -> Finished[Event]:
        worktrees = await workspace_scan.base_worktrees(self.repo, self.bases)
        names = {worktree.branch for worktree in worktrees if worktree.branch}
        prs = await self.pr_source(self.repo, names)
        facts = await _repository_facts(self.repo, prs)
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
        return Finished(InspectedWorktree(item), verdict_of(item).lower())


async def _inspect_worktree(facts: RepositoryFacts, worktree: Worktree, live: list[int]) -> Classification:
    return await worktree_gc.classify_worktree(
        worktree,
        main=facts.main,
        pr_states=facts.prs,
        main_path=facts.main_path,
        active_path=facts.active_path,
        live_pids=live,
    )


@dataclass(frozen=True, slots=True)
class InspectBaseWorkspace(AsyncWorkItem[Event]):
    facts: RepositoryFacts
    worktree: Worktree
    live: list[int]
    phase: ClassVar[str] = "inspect base workspaces"

    async def run_async(self) -> Finished[Event]:
        item = await _inspect_worktree(self.facts, self.worktree, self.live)
        return Finished(InspectedBaseWorkspace(item), verdict_of(item).lower())


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
            pr_states=self.facts.prs,
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
    phase: ClassVar[str] = "inspect branches"

    async def run_async(self) -> Finished[Event]:
        pg = await run_blocking(git_repo.open_repo, self.facts.main_path)
        item = await branch_gc.classify_branch(
            self.name,
            pg=pg,
            main=self.facts.main,
            default_branch=self.facts.default_branch,
            pr=self.facts.prs.get(self.name),
            holder=self.holder,
        )
        return Finished(InspectedBranch(item), verdict_of(item).lower())


@dataclass(frozen=True, slots=True)
class RemoveBranch(AsyncMutation[Event]):
    facts: RepositoryFacts
    item: branch_gc.PrunableBranch
    phase: ClassVar[str] = "delete branches"

    async def run_async(self) -> Finished[Event]:
        result = await branch_gc.delete_branch(self.facts.repo, self.item.branch.name, expected_tip=self.item.tip)
        return Finished(RemovedBranch(result), type(result).__name__.lower())


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
        return Finished(InspectedBase(item, self.initial), verdict_of(item).lower())


@dataclass(frozen=True, slots=True)
class RemoveBase:
    item: PrunableBase
    sizes: bool
    phase: ClassVar[str] = "delete bases"

    def run(self) -> Finished[Event]:
        size = output_base_gc.allocated_bytes(self.item.path) if self.sizes else None
        result = output_base_gc.delete_prunable_base(self.item)
        return Finished(RemovedBase(result, size), type(result).__name__.lower())


@dataclass(frozen=True, slots=True)
class DiscoverCache:
    contents: Path
    base_paths: tuple[Path, ...]
    phase: ClassVar[str] = "discover repo cache"

    def run(self) -> Finished[Event]:
        try:
            contents = self.contents.resolve(strict=True)
        except FileNotFoundError:
            return Finished(FoundCacheBuckets([], CacheFacts({}, set())), "done")
        buckets = repo_contents_cache_gc.cache_buckets(contents)
        referrers = repo_contents_cache_gc.references(self.base_paths, contents)
        facts = CacheFacts(referrers, output_base_gc.mount_points())
        return Finished(FoundCacheBuckets(buckets, facts), "done")


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
        inspection = repo_contents_cache_gc.classify_entry(item, self.facts.referrers, set())
        return Finished(InspectedCacheEntry(inspection), verdict_of(inspection).lower())


@dataclass(frozen=True, slots=True)
class TrashCacheEntries:
    entries: tuple[PrunableEntry, ...]
    contents: Path
    root: Path
    session_bases: tuple[Path, ...]
    sizes: bool
    phase: ClassVar[str] = "trash cache batches"

    def run(self) -> Finished[Event]:
        sizes = (
            {item.entry.contents: output_base_gc.allocated_bytes(item.entry.contents) for item in self.entries}
            if self.sizes
            else {}
        )
        trashed = repo_contents_cache_gc.trash_prunable_entries(
            self.entries, contents=self.contents, output_user_root=self.root, session_bases=self.session_bases
        )
        return Finished(TrashedCacheEntries(trashed, sizes), "done")


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
            self.root, pr_states=self.facts.prs, active_path=self.facts.active_path
        )
        return Finished(InspectedForeignClone(item), verdict_of(item).lower())


@dataclass(frozen=True, slots=True)
class RemoveForeignClone(AsyncMutation[Event]):
    facts: RepositoryFacts
    root: Path
    output_user_root: Path
    session_bases: tuple[Path, ...]
    phase: ClassVar[str] = "remove foreign clones"

    async def run_async(self) -> Finished[Event]:
        try:
            roots = await workspace_scan.foreign_clone_roots(
                self.facts.repo, self.output_user_root, session_bases=self.session_bases
            )
            if self.root not in roots:
                result: RemovalResult[Path] = Skipped(self.root, "clone is no longer discoverable")
            else:
                fresh = await foreign_clone_gc.classify_foreign_clone(
                    self.root, pr_states=self.facts.prs, active_path=self.facts.active_path
                )
                if isinstance(fresh, PrunableForeignClone):
                    result = await foreign_clone_gc.prune_foreign_clone(fresh.clone)
                else:
                    result = Skipped(self.root, fresh.reason)
        except git_repo.INSPECTION_ERRORS as error:
            result = Skipped(self.root, str(error))
        return Finished(RemovedForeignClone(result), type(result).__name__.lower())


def describe(item: WorkItem[Event]) -> str:
    """Identify a job without logging its facts or pull request data."""
    if isinstance(item, PrepareRepositoryScan | DiscoverBaseWorkspaces):
        return str(item.repo)
    if isinstance(item, DiscoverWorktrees | DiscoverBranches):
        return str(item.facts.repo)
    if isinstance(item, InspectWorktree | InspectBaseWorkspace):
        return str(item.worktree.path)
    if isinstance(item, RemoveWorktree):
        return str(item.item.worktree.path)
    if isinstance(item, InspectBranch):
        return item.name
    if isinstance(item, RemoveBranch):
        return item.item.branch.name
    if isinstance(item, DiscoverBases | InspectForeignClone | RemoveForeignClone):
        return str(item.root)
    if isinstance(item, InspectBase | DiscoverCacheBucket | EmptyCacheTrash):
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
