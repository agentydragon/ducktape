"""Discover, inspect and remove workspace state through a shared work queue."""

from __future__ import annotations

import asyncio
import enum
import logging
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from devinfra.gc import branch_gc, github_prs, repo_contents_cache_gc, workspace_jobs as jobs
from devinfra.gc.branch_gc import (
    Branch,
    BranchClassification,
    MainCheckout,
    PrunableBranch,
    RetainedBranch,
    RetainedBranchReason,
)
from devinfra.gc.foreign_clone_gc import ForeignCloneClassification
from devinfra.gc.output_base_gc import BaseLayout, Inspection, PrunableBase, RetainedBase
from devinfra.gc.pull_request import PrInfo, PrLookup
from devinfra.gc.removal import Failed, RemovalResult, Removed
from devinfra.gc.repo_contents_cache_gc import EntryInspection, PrunableEntry, RetainedEntry, RetainedEntryReason
from devinfra.gc.work_queue import Cancelled, Finished, WorkFailure, WorkItem, WorkQueue
from devinfra.gc.workspace_scan import WorkspaceScan
from devinfra.gc.worktree_gc import Classification, PrunableWorktree

logger = logging.getLogger(__name__)


class Domain(enum.StrEnum):
    WORKTREES = enum.auto()
    BRANCHES = enum.auto()
    BASES = enum.auto()
    REPO_CACHE = enum.auto()
    FOREIGN_CLONES = enum.auto()


@dataclass(frozen=True, slots=True)
class Options:
    domains: frozenset[Domain]
    root: Path
    session_bases: tuple[Path, ...]
    workers: int
    apply: bool
    sizes: bool
    repo: Path | None
    pr_source: jobs.PrSource
    show_all: bool = False


@dataclass(frozen=True, slots=True)
class Report:
    scan: WorkspaceScan
    entries: list[EntryInspection]
    worktree_removals: list[RemovalResult[Path]]
    branch_removals: list[RemovalResult[str]]
    branch_recovery_refs: dict[str, str]
    base_removals: list[RemovalResult[Path]]
    cache_removals: list[RemovalResult[Path]]
    base_sizes: dict[Path, int | None]
    cache_sizes: dict[Path, int | None]
    base_free_before: int | None
    cache_free_before: int | None


class Observer(Protocol):
    def update(self, queue: WorkQueue[jobs.Event], *, waiting: int) -> None: ...


class NullObserver:
    def update(self, queue: WorkQueue[jobs.Event], *, waiting: int) -> None:
        pass


NULL_OBSERVER: Observer = NullObserver()


class QueueError(Exception):
    def __init__(self, error: Exception, report: Report) -> None:
        super().__init__(str(error))
        self.error = error
        self.report = report


class _WorktreesAndBranches:
    """Schedule branch inspection after the worktree holding it is settled."""

    def __init__(self, options: Options, queue: WorkQueue[jobs.Event]) -> None:
        self.options = options
        self.queue = queue
        self.facts: jobs.RepositoryFacts | None = None
        self.worktree_order: list[Path] | None = None
        self.worktrees: dict[Path, Classification] = {}
        self.resolved_paths: dict[Path, Path | None] = {}
        self.worktree_outcomes: dict[Path, RemovalResult[Path]] = {}
        self.branch_order: list[str] = []
        self.branches: dict[str, BranchClassification] = {}
        self.waiting_branches: dict[Path, list[str]] = {}
        self.worktree_removals: list[RemovalResult[Path]] = []
        self.branch_removals: list[RemovalResult[str]] = []
        self.branch_recovery_refs: dict[str, str] = {}
        self.worktree_candidates: dict[str, list[tuple[Classification, Path | None]]] = {}
        self.branch_candidates: dict[str, list[jobs.InspectedBranch]] = {}

    def repository(self) -> jobs.RepositoryFacts:
        assert self.facts is not None
        return self.facts

    def repository_ready(self, facts: jobs.RepositoryFacts) -> None:
        self.facts = facts
        if self.options.domains & {Domain.WORKTREES, Domain.BRANCHES}:
            self.queue.submit(jobs.DiscoverWorktrees(facts))
        if Domain.BRANCHES in self.options.domains:
            self.queue.submit(jobs.DiscoverBranches(facts))

    def found_worktrees(self, event: jobs.FoundWorktrees) -> None:
        self.worktree_order = [wt.path for wt in event.worktrees]
        for wt in event.worktrees:
            self.queue.submit(jobs.InspectWorktree(self.local_facts(), wt, event.live.get(wt.path, [])))
        for path in list(self.waiting_branches):
            self._release_branches(path)

    def inspected_worktree_candidate(
        self, item: Classification, resolved_path: Path | None, lookup: PrLookup, pending: set[str]
    ) -> None:
        branch = item.worktree.branch
        if branch is not None and branch in pending:
            self.worktree_candidates.setdefault(branch, []).append((item, resolved_path))
            return
        self._finish_worktree_candidate(item, resolved_path, lookup)

    def resolve_worktree_candidates(self, branches: set[str], lookup: PrLookup) -> None:
        for branch in branches:
            for item, resolved_path in self.worktree_candidates.pop(branch, []):
                self._finish_worktree_candidate(item, resolved_path, lookup)

    def _finish_worktree_candidate(self, item: Classification, resolved_path: Path | None, lookup: PrLookup) -> None:
        self.facts = replace(self.repository(), prs=lookup)
        self.inspected_worktree(jobs.resolve_worktree_pr(item, item.worktree.branch, lookup), resolved_path)

    def inspected_worktree(self, item: Classification, resolved_path: Path | None) -> None:
        path = item.worktree.path
        self.worktrees[path] = item
        self.resolved_paths[path] = resolved_path
        if isinstance(item, PrunableWorktree) and self.options.apply and Domain.WORKTREES in self.options.domains:
            self.queue.submit(jobs.RemoveWorktree(self.repository(), item), followup=True)
        else:
            self._release_branches(path)

    def removed_worktree(self, result: RemovalResult[Path]) -> None:
        self.worktree_removals.append(result)
        self.worktree_outcomes[result.subject] = result
        self._release_branches(result.subject)

    def found_branches(self, event: jobs.FoundBranches) -> None:
        self.branch_order = event.names
        for name in event.names:
            holder = event.holders.get(name)
            if holder is None:
                self.queue.submit(jobs.InspectBranch(self.local_facts(), name, None))
            elif holder == self.repository().main_path:
                self.queue.submit(jobs.InspectBranch(self.local_facts(), name, MainCheckout()))
            else:
                self.waiting_branches.setdefault(holder, []).append(name)
                self._release_branches(holder)

    def _release_branches(self, path: Path) -> None:
        waiting = self.waiting_branches.get(path)
        if not waiting:
            return
        item = self.worktrees.get(path)
        if item is None:
            if self.worktree_order is None or path in self.worktree_order:
                return
            # A worktree appeared after discovery; never classify its branch as unheld.
            for name in waiting:
                self.branches[name] = RetainedBranch(Branch(name), RetainedBranchReason.HELD_BY_WORKTREE, path)
        elif isinstance(item, PrunableWorktree) and self.options.apply and Domain.WORKTREES in self.options.domains:
            outcome = self.worktree_outcomes.get(path)
            if outcome is None:
                return
            if isinstance(outcome, Removed):
                for name in waiting:
                    self.queue.submit(jobs.InspectBranch(self.local_facts(), name, None), followup=True)
            else:
                for name in waiting:
                    self.branches[name] = RetainedBranch(Branch(name), RetainedBranchReason.HELD_BY_WORKTREE, path)
        else:
            for name in waiting:
                self.queue.submit(jobs.InspectBranch(self.local_facts(), name, item), followup=True)
        del self.waiting_branches[path]

    def inspected_branch_candidate(self, event: jobs.InspectedBranch, lookup: PrLookup, pending: set[str]) -> None:
        if event.name in pending:
            self.branch_candidates.setdefault(event.name, []).append(event)
            return
        self._finish_branch_candidate(event, lookup)

    def resolve_branch_candidates(self, branches: set[str], lookup: PrLookup) -> None:
        for branch in branches:
            for event in self.branch_candidates.pop(branch, []):
                self._finish_branch_candidate(event, lookup)

    def _finish_branch_candidate(self, event: jobs.InspectedBranch, lookup: PrLookup) -> None:
        self.facts = replace(self.repository(), prs=lookup)
        if event.name in lookup.states:
            self.queue.submit(
                jobs.InspectBranch(self.repository(), event.name, event.holder, final=True), followup=True
            )
            return
        item = event.item
        if event.name in lookup.unknown and isinstance(item, PrunableBranch):
            item = branch_gc.ReviewBranch(item.branch, branch_gc.ReviewBranchReason.PR_LOOKUP_UNAVAILABLE, item.main)
        self.inspected_branch(item)

    def inspected_branch(self, item: BranchClassification) -> None:
        self.branches[item.branch.name] = item
        if isinstance(item, PrunableBranch) and self.options.apply:
            self.queue.submit(jobs.RemoveBranch(self.repository(), item), followup=True)

    def removed_branch(self, result: RemovalResult[str], recovery_prefix: str | None) -> None:
        self.branch_removals.append(result)
        if recovery_prefix is not None:
            self.branch_recovery_refs[result.subject] = recovery_prefix

    def local_facts(self) -> jobs.RepositoryFacts:
        return replace(self.repository(), prs=PrLookup({}, disabled=True))

    def pending_worktrees(self) -> set[Path]:
        return {
            resolved
            for path, item in self.worktrees.items()
            if Domain.WORKTREES in self.options.domains
            and isinstance(item, PrunableWorktree)
            and path not in self.worktree_outcomes
            if (resolved := self.resolved_paths.get(path)) is not None
        }

    def waiting(self) -> int:
        return sum(map(len, self.waiting_branches.values()))

    def prunable_paths(self) -> set[Path]:
        return {
            path
            for item in self.worktrees.values()
            if isinstance(item, PrunableWorktree)
            if (path := self.resolved_paths.get(item.worktree.path)) is not None
        }


@dataclass(slots=True)
class _Base:
    layout: BaseLayout
    inspection: Inspection | None = None
    attempted: bool = False
    reinspected: bool = False
    reinspection_pending: bool = False
    removal: RemovalResult[Path] | None = None
    resolved_workspace: Path | None = None
    measuring: bool = False


class _Bases:
    """Own base reinspection after a referenced worktree disappears."""

    def __init__(self, options: Options, queue: WorkQueue[jobs.Event]) -> None:
        self.options = options
        self.queue = queue
        self.workspaces: dict[Path, tuple[Classification, Path | None]] = {}
        self.records: dict[Path, _Base] = {}
        self.points: set[Path] = set()
        self.deleted_worktrees: set[Path] = set()
        self.annotation_scheduled = False
        self.pending_workspaces: jobs.FoundBaseWorkspaces | None = None
        self.removals: list[RemovalResult[Path]] = []
        self.sizes: dict[Path, int | None] = {}
        self.free_before: int | None = None

    def found(self, event: jobs.FoundBases) -> None:
        self.records = {path: _Base(layout) for path, layout in event.bases}
        self.points = event.points
        self.free_before = event.free_before
        for path, layout in event.bases:
            self.queue.submit(jobs.InspectBase(path, layout, event.points))
        self._after_initial()

    def found_workspaces(self, event: jobs.FoundBaseWorkspaces) -> None:
        for worktree in event.worktrees:
            self.queue.submit(jobs.InspectBaseWorkspace(event.facts, worktree, event.live.get(worktree.path, [])))

    def hold_workspaces(self, event: jobs.FoundBaseWorkspaces) -> None:
        self.pending_workspaces = event

    def schedule_workspaces(self, lookup: PrLookup, pending_branches: set[str]) -> None:
        event = self.pending_workspaces
        if event is None:
            return
        names = {wt.branch for wt in event.worktrees if wt.branch}
        if names & pending_branches:
            return
        self.pending_workspaces = None
        self.found_workspaces(replace(event, facts=replace(event.facts, prs=lookup)))

    def inspected(self, item: Inspection, *, initial: bool, resolved_workspace: Path | None) -> None:
        record = self.records[item.path]
        record.inspection = item
        record.resolved_workspace = resolved_workspace
        if not initial:
            record.reinspection_pending = False
        if self.options.sizes and (self.options.show_all or not isinstance(item, RetainedBase)):
            self._measure(item.path)
        if isinstance(item, PrunableBase) and self.options.apply and Domain.BASES in self.options.domains:
            if not record.attempted:
                record.attempted = True
                if self.options.sizes and item.path not in self.sizes:
                    self._measure(item.path)
                else:
                    self.queue.submit(jobs.RemoveBase(item, self.sizes.get(item.path)), followup=True)
        else:
            self._reinspect(item)
        self._after_initial()

    def _measure(self, path: Path) -> None:
        record = self.records[path]
        if path not in self.sizes and not record.measuring:
            record.measuring = True
            self.queue.submit(jobs.MeasureBase(path), followup=True)

    def measured(self, path: Path, size: int | None) -> None:
        record = self.records[path]
        record.measuring = False
        self.sizes[path] = size
        if record.attempted and record.removal is None and isinstance(record.inspection, PrunableBase):
            self.queue.submit(jobs.RemoveBase(record.inspection, size), followup=True)

    def worktree_removed(self, result: RemovalResult[Path], resolved_path: Path | None) -> None:
        if not isinstance(result, Removed):
            return
        if resolved_path is not None:
            self.deleted_worktrees.add(resolved_path)
        for record in self.records.values():
            if record.inspection is not None:
                self._reinspect(record.inspection)

    def removed(self, result: RemovalResult[Path], size: int | None, *, measured: bool = True) -> None:
        self.removals.append(result)
        self.records[result.subject].removal = result
        if self.options.sizes and measured:
            self.sizes[result.subject] = size

    def _reinspect(self, item: Inspection) -> None:
        if not isinstance(item, RetainedBase) or item.workspace is None:
            return
        record = self.records[item.path]
        if record.resolved_workspace not in self.deleted_worktrees or record.reinspected:
            return
        record.reinspected = True
        record.reinspection_pending = True
        self.queue.submit(jobs.InspectBase(item.path, record.layout, self.points, initial=False), followup=True)

    def _after_initial(self) -> None:
        if any(record.inspection is None for record in self.records.values()):
            return
        if self.options.domains == {Domain.BASES} and self.options.repo is not None and not self.annotation_scheduled:
            self.annotation_scheduled = True
            self.queue.submit(
                jobs.DiscoverBaseWorkspaces(
                    self.options.repo,
                    tuple(record.inspection for record in self.records.values() if record.inspection is not None),
                )
            )

    def pending(self, worktrees: _WorktreesAndBranches) -> set[Path]:
        pending = {path for path, record in self.records.items() if record.attempted and record.removal is None}
        if self.options.apply and Domain.BASES in self.options.domains:
            pending_worktrees = worktrees.pending_worktrees()
            pending |= {
                path
                for path, record in self.records.items()
                if record.inspection is None
                or record.reinspection_pending
                or (
                    isinstance(record.inspection, RetainedBase)
                    and record.inspection.workspace is not None
                    and record.resolved_workspace in pending_worktrees
                )
            }
        return pending

    def report(self, prunable: set[Path]) -> list[Inspection]:
        prunable |= {
            path for item, path in self.workspaces.values() if isinstance(item, PrunableWorktree) if path is not None
        }
        return [
            replace(record.inspection, workspace_is_prunable_worktree=True)
            if isinstance(record.inspection, RetainedBase) and record.resolved_workspace in prunable
            else record.inspection
            for record in self.records.values()
            if record.inspection is not None
        ]

    def prunable_paths(self) -> set[Path]:
        return {path for path, record in self.records.items() if isinstance(record.inspection, PrunableBase)}


@dataclass(slots=True)
class _CacheEntry:
    inspection: EntryInspection
    attempted: bool = False
    measuring: bool = False


class _Cache:
    """Unlock cache entries as their referring bases are removed, then batch trashing."""

    def __init__(self, options: Options, queue: WorkQueue[jobs.Event]) -> None:
        self.options = options
        self.queue = queue
        self.entries: dict[Path, _CacheEntry] = {}
        self.by_base: dict[Path, set[Path]] = {}
        self.waiting_batch: deque[PrunableEntry] = deque()
        self.batch_active = False
        self.deleted_bases: set[Path] = set()
        self.removals: list[RemovalResult[Path]] = []
        self.sizes: dict[Path, int | None] = {}
        self.free_before: int | None = None

    def found_bases(self, paths: list[Path]) -> None:
        if Domain.REPO_CACHE in self.options.domains:
            self.queue.submit(
                jobs.DiscoverCache(repo_contents_cache_gc.repo_contents_cache(self.options.root), tuple(paths)),
                followup=True,
            )

    def inspected(self, item: EntryInspection) -> None:
        path = repo_contents_cache_gc.entry_path(item)
        self.entries[path] = _CacheEntry(item)
        if isinstance(item, RetainedEntry):
            for referrer in item.referrers:
                self.by_base.setdefault(referrer, set()).add(path)
        if self.options.sizes and (self.options.show_all or not isinstance(item, RetainedEntry)):
            self._measure(path)
        self._eligible(item)

    def _measure(self, path: Path) -> None:
        record = self.entries[path]
        if path not in self.sizes and not record.measuring:
            record.measuring = True
            self.queue.submit(jobs.MeasureCacheEntry(path), followup=True)

    def measured(self, path: Path, size: int | None) -> None:
        record = self.entries[path]
        record.measuring = False
        self.sizes[path] = size
        self._eligible(record.inspection)

    def base_removed(self, result: RemovalResult[Path]) -> None:
        if isinstance(result, Removed):
            self.deleted_bases.add(result.subject)
            for path in self.by_base.get(result.subject, set()):
                self._eligible(self.entries[path].inspection)

    def _eligible(self, item: EntryInspection) -> None:
        if not self.options.apply or Domain.REPO_CACHE not in self.options.domains:
            return
        path = repo_contents_cache_gc.entry_path(item)
        record = self.entries[path]
        if record.attempted:
            return
        if isinstance(item, PrunableEntry):
            candidate = item
        elif isinstance(item, RetainedEntry) and item.referrers <= self.deleted_bases:
            candidate = PrunableEntry(item.entry)
        else:
            return
        if self.options.sizes and path not in self.sizes:
            self._measure(path)
            return
        record.attempted = True
        self.waiting_batch.append(candidate)
        self._schedule_batch()

    def _schedule_batch(self) -> None:
        if self.batch_active or not self.waiting_batch:
            return
        batch = tuple(self.waiting_batch.popleft() for _ in range(min(128, len(self.waiting_batch))))
        self.batch_active = True
        self.queue.submit(
            jobs.TrashCacheEntries(
                batch,
                repo_contents_cache_gc.repo_contents_cache(self.options.root),
                self.options.root,
                self.options.session_bases,
                {
                    item.entry.contents: self.sizes[item.entry.contents]
                    for item in batch
                    if item.entry.contents in self.sizes
                },
            ),
            followup=True,
        )

    def trashed(self, event: jobs.TrashedCacheEntries) -> None:
        self.batch_active = False
        self.removals.extend(event.trashed.results)
        self.sizes.update(event.sizes)
        for original, trash in event.trashed.trashed:
            self.queue.submit(jobs.EmptyCacheTrash(original, trash), followup=True)
        self._schedule_batch()

    def waiting(self, pending_bases: set[Path]) -> int:
        pending = sum(
            isinstance(record.inspection, RetainedEntry)
            and bool(record.inspection.referrers & pending_bases)
            and record.inspection.referrers <= pending_bases | self.deleted_bases
            for record in self.entries.values()
            if not record.attempted
        )
        return pending + len(self.waiting_batch)

    def report(self, prunable_bases: set[Path]) -> list[EntryInspection]:
        entries: list[EntryInspection] = []
        for record in self.entries.values():
            item = record.inspection
            if isinstance(item, RetainedEntry):
                item = replace(
                    item,
                    reason=(
                        RetainedEntryReason.ONLY_PRUNABLE_REFERRERS
                        if item.referrers <= prunable_bases
                        else RetainedEntryReason.REFERENCED
                    ),
                )
            entries.append(item)
        return sorted(entries, key=repo_contents_cache_gc.entry_path)


class _ForeignClones:
    def __init__(self, options: Options, queue: WorkQueue[jobs.Event]) -> None:
        self.options = options
        self.queue = queue
        self.facts: jobs.RepositoryFacts | None = None
        self.roots: list[Path] = []
        self.branch_discovery_scheduled: set[Path] = set()
        self.workspaces: set[Path] = set()
        self.inspections: dict[Path, ForeignCloneClassification] = {}
        self.candidates: dict[Path, frozenset[str]] = {}

    def repository_ready(self, facts: jobs.RepositoryFacts) -> None:
        self.facts = facts

    def inspected_base(self, item: Inspection) -> None:
        if Domain.FOREIGN_CLONES not in self.options.domains:
            return
        if not isinstance(item, RetainedBase) or item.workspace is None or item.workspace in self.workspaces:
            return
        self.workspaces.add(item.workspace)
        assert self.options.repo is not None
        self.queue.submit(jobs.DiscoverForeignWorkspace(self.options.repo, item.workspace))

    def found(self, roots: list[Path]) -> None:
        for root in roots:
            if root in self.roots:
                continue
            self.roots.append(root)
            self._discover_branches(root)

    def _discover_branches(self, root: Path) -> None:
        if root in self.branch_discovery_scheduled:
            return
        self.branch_discovery_scheduled.add(root)
        self.queue.submit(jobs.DiscoverForeignCloneBranches(root))

    def candidate(self, root: Path, branches: frozenset[str]) -> None:
        self.candidates[root] = branches

    def schedule_candidates(self, lookup: PrLookup, pending_branches: set[str]) -> None:
        if self.facts is None:
            return
        for root, branches in list(self.candidates.items()):
            if branches & pending_branches:
                continue
            self.queue.submit(jobs.InspectForeignClone(replace(self.facts, prs=lookup), root))
            del self.candidates[root]

    def inspected(self, item: ForeignCloneClassification) -> None:
        self.inspections[item.clone.root] = item


class _PrLookupBatches:
    """Own deduplicated, bounded PR requests and their aggregate result."""

    def __init__(self, repo: Path | None, source: jobs.PrSource, workers: int) -> None:
        self.repo = repo
        self.source = source
        self.workers = workers
        self.states: dict[str, PrInfo] = {}
        self.unknown: set[str] = set()
        self.requested: set[str] = set()
        self.pending: set[str] = set()
        self.queued: deque[frozenset[str]] = deque()
        self.active: set[frozenset[str]] = set()
        self.disabled = False

    def snapshot(self) -> PrLookup:
        return PrLookup(dict(self.states), self.unknown, disabled=self.disabled)

    def request(self, branches: set[str] | frozenset[str]) -> None:
        if self.disabled:
            return
        new = set(branches) - self.requested
        if not new:
            return
        self.requested.update(new)
        self.queued.extend(github_prs.pr_batches(new))
        self.pending.update(new)

    def start(self, queue: WorkQueue[jobs.Event], *, local_discovery_ready: bool) -> None:
        if not self.queued or (self.workers == 1 and not local_discovery_ready):
            return
        limit = min(github_prs.MAX_CONCURRENT_REQUESTS, max(1, self.workers - 1))
        assert self.repo is not None
        while self.queued and len(self.active) < limit:
            branches = self.queued.popleft()
            self.active.add(branches)
            queue.submit(jobs.LookupPrBatch(self.repo, branches, self.source))

    def complete(self, event: jobs.PrBatchCompleted) -> set[str]:
        self.active.discard(event.branches)
        if not self.disabled:
            if event.lookup.disabled:
                self.disabled = True
                self.states.clear()
                self.unknown.clear()
                self.pending.clear()
                self.queued.clear()
            else:
                self.states.update(event.lookup.states)
                self.unknown.update(event.lookup.unknown)
                self.pending.difference_update(event.branches)
        resolved = set(event.branches)
        if self.disabled:
            resolved |= self.requested
        return resolved

    @property
    def quiescent(self) -> bool:
        return not self.pending and not self.queued and not self.active


class _LocalDiscovery:
    """Track only local work needed before a one-worker run may start PR requests."""

    def __init__(self, domains: frozenset[Domain]) -> None:
        self.worktrees_done = not bool(domains & {Domain.WORKTREES, Domain.BRANCHES})
        self.branches_done = Domain.BRANCHES not in domains
        self.worktrees_pending = 0
        self.branches_pending: set[str] = set()

    @property
    def ready(self) -> bool:
        return self.worktrees_done and self.branches_done and self.worktrees_pending == 0 and not self.branches_pending

    def branches_found(self, names: list[str], holders: dict[str, Path], main_path: Path) -> None:
        self.branches_done = True
        self.branches_pending = {name for name in names if holders.get(name) is None or holders.get(name) == main_path}

    def branch_inspected(self, name: str) -> None:
        self.branches_pending.discard(name)


class Run:
    """Own one run and retain its partial report after cancellation."""

    def __init__(self, options: Options, observer: Observer = NULL_OBSERVER) -> None:
        self.options = options
        self.observer = observer
        self.queue: WorkQueue[jobs.Event] = WorkQueue(options.workers)
        self.trees = _WorktreesAndBranches(options, self.queue)
        self.bases = _Bases(options, self.queue)
        self.cache = _Cache(options, self.queue)
        self.foreign = _ForeignClones(options, self.queue)
        self._task: asyncio.Task[Report] | None = None
        self._stop_requested = False
        self.prs = _PrLookupBatches(options.repo, options.pr_source, options.workers)
        self.local_discovery = _LocalDiscovery(options.domains)

    def request_stop(self) -> None:
        """Request shutdown once; repeated signals do not interrupt the drain."""
        if self._stop_requested:
            return
        self._stop_requested = True
        if self._task is not None:
            self._task.cancel()

    def seed(self) -> None:
        domains = self.options.domains
        if domains & {Domain.WORKTREES, Domain.BRANCHES, Domain.FOREIGN_CLONES}:
            assert self.options.repo is not None
            self.queue.submit(jobs.PrepareRepositoryScan(self.options.repo))
        if domains & {Domain.BASES, Domain.REPO_CACHE, Domain.FOREIGN_CLONES}:
            self.queue.submit(jobs.DiscoverBases(self.options.root, self.options.session_bases))

    def waiting(self) -> int:
        return (
            self.trees.waiting()
            + self.cache.waiting(self.bases.pending(self.trees))
            + sum(len(items) for items in self.trees.worktree_candidates.values())
            + sum(len(items) for items in self.trees.branch_candidates.values())
            + len(self.foreign.candidates)
            + self._pending_base_workspaces()
        )

    def _pending_base_workspaces(self) -> int:
        event = self.bases.pending_workspaces
        if event is None:
            return 0
        names = {wt.branch for wt in event.worktrees if wt.branch}
        return len(event.worktrees) if names & self.prs.pending else 0

    def _local_discovery_ready(self) -> bool:
        return self.local_discovery.ready

    def start_pr_batches(self) -> None:
        self.prs.start(self.queue, local_discovery_ready=self._local_discovery_ready())

    def _finish_pr_batch(self, event: jobs.PrBatchCompleted) -> None:
        resolved = self.prs.complete(event)
        lookup = self.prs.snapshot()
        self.trees.resolve_worktree_candidates(resolved, lookup)
        self.trees.resolve_branch_candidates(resolved, lookup)
        self._schedule_base_workspaces()
        self._schedule_foreign_clones()
        self.start_pr_batches()

    def _schedule_base_workspaces(self) -> None:
        self.bases.schedule_workspaces(self.prs.snapshot(), self.prs.pending)

    def _schedule_foreign_clones(self) -> None:
        self.foreign.schedule_candidates(self.prs.snapshot(), self.prs.pending)

    def handle(self, event: jobs.Event) -> None:
        match event:
            case jobs.PreparedRepositoryScan(facts=facts, pr_candidates=candidates):
                self.trees.repository_ready(facts)
                self.foreign.repository_ready(facts)
                self.prs.request(candidates)
                self.start_pr_batches()
                self._schedule_foreign_clones()
            case jobs.PrBatchCompleted():
                self._finish_pr_batch(event)
            case jobs.FoundForeignCloneRoots(roots=roots):
                self.foreign.found(roots)
            case jobs.FoundForeignCloneBranches(root=root, branches=branches):
                self.foreign.candidate(root, branches)
                self.prs.request(branches)
                self.start_pr_batches()
                self._schedule_foreign_clones()
            case jobs.FoundBaseWorkspaces():
                self.bases.hold_workspaces(event)
                self.prs.request({wt.branch for wt in event.worktrees if wt.branch})
                self.start_pr_batches()
                self._schedule_base_workspaces()
            case jobs.InspectedBaseWorkspace(item=item, resolved_path=resolved):
                self.bases.workspaces[item.worktree.path] = item, resolved
            case jobs.FoundWorktrees():
                self.local_discovery.worktrees_done = True
                self.local_discovery.worktrees_pending += len(event.worktrees)
                self.prs.request({wt.branch for wt in event.worktrees if wt.branch})
                self.trees.found_worktrees(event)
                self.start_pr_batches()
            case jobs.InspectedWorktree(item=item, resolved_path=resolved):
                self.local_discovery.worktrees_pending -= 1
                if item.worktree.branch is not None:
                    self.prs.request({item.worktree.branch})
                self.trees.inspected_worktree_candidate(item, resolved, self.prs.snapshot(), self.prs.pending)
                self.start_pr_batches()
            case jobs.RemovedWorktree(result=result):
                self.trees.removed_worktree(result)
                self.bases.worktree_removed(result, self.trees.resolved_paths.get(result.subject))
            case jobs.RemovedBranch(result=result, recovery_prefix=prefix):
                self.trees.removed_branch(result, prefix)
            case jobs.FoundBranches():
                main_path = self.trees.repository().main_path
                self.local_discovery.branches_found(event.names, event.holders, main_path)
                self.prs.request(set(event.names))
                self.trees.found_branches(event)
                self.start_pr_batches()
            case jobs.InspectedBranch(item=item):
                if event.candidate:
                    self.local_discovery.branch_inspected(event.name)
                    self.trees.inspected_branch_candidate(event, self.prs.snapshot(), self.prs.pending)
                    self.start_pr_batches()
                else:
                    self.trees.inspected_branch(item)
            case jobs.FoundBases():
                self.bases.found(event)
                self.cache.found_bases(list(self.bases.records))
            case jobs.InspectedBase(item=item, initial=initial, resolved_workspace=resolved):
                self.foreign.inspected_base(item)
                self.bases.inspected(item, initial=initial, resolved_workspace=resolved)
            case jobs.MeasuredBase(path=path, size=size):
                self.bases.measured(path, size)
            case jobs.RemovedBase(result=result, size=size):
                self.bases.removed(result, size)
                self.cache.base_removed(result)
            case jobs.FoundCacheBuckets(buckets=buckets, facts=facts, free_before=free):
                self.cache.free_before = free
                for bucket in buckets:
                    self.queue.submit(jobs.DiscoverCacheBucket(bucket, facts))
            case jobs.FoundCacheCandidates(candidates=candidates, facts=facts):
                for candidate in candidates:
                    self.queue.submit(jobs.InspectCacheEntry(candidate, facts))
            case jobs.InspectedCacheEntry(item=item):
                self.cache.inspected(item)
            case jobs.MeasuredCacheEntry(path=path, size=size):
                self.cache.measured(path, size)
            case jobs.TrashedCacheEntries():
                self.cache.trashed(event)
            case jobs.RemovedCacheEntry(result=result):
                self.cache.removals.append(result)
            case jobs.InspectedForeignClone(item=item):
                self.foreign.inspected(item)

    def _record_stopped(
        self, item: WorkItem[jobs.Event], result: Finished[jobs.Event] | WorkFailure | Cancelled
    ) -> None:
        if isinstance(result, Cancelled):
            if isinstance(item, jobs.EmptyCacheTrash):
                self.cache.removals.append(Failed(item.path, "cleanup stopped with entry in trash", item.trash))
            return
        if isinstance(result, WorkFailure):
            error = f"{type(result.error).__name__}: {result.error}"
            match item:
                case jobs.RemoveWorktree(item=worktree):
                    self.trees.worktree_removals.append(Failed(worktree.worktree.path, error))
                case jobs.RemoveBranch(item=branch):
                    self.trees.removed_branch(Failed(branch.branch.name, error), None)
                case jobs.RemoveBase(item=base):
                    self.bases.removed(Failed(base.path, error), None, measured=False)
                case jobs.EmptyCacheTrash(path=path, trash=trash):
                    self.cache.removals.append(Failed(path, error, trash))
                case jobs.TrashCacheEntries(entries=entries):
                    self.cache.removals.extend(Failed(entry.entry.contents, error) for entry in entries)
            return
        match result.value:
            case jobs.RemovedWorktree(result=removed):
                self.trees.worktree_removals.append(removed)
            case jobs.RemovedBranch(result=removed, recovery_prefix=prefix):
                self.trees.removed_branch(removed, prefix)
            case jobs.RemovedBase(result=removed, size=size):
                self.bases.removed(removed, size)
            case jobs.TrashedCacheEntries(trashed=trashed, sizes=sizes):
                self.cache.sizes.update(sizes)
                self.cache.removals.extend(trashed.results)
                self.cache.removals.extend(
                    Failed(path, "cleanup stopped with entry in trash", trash) for path, trash in trashed.trashed
                )
            case jobs.RemovedCacheEntry(result=removed):
                self.cache.removals.append(removed)

    def report(self) -> Report:
        return Report(
            WorkspaceScan(
                [
                    self.trees.worktrees[path]
                    for path in self.trees.worktree_order or []
                    if path in self.trees.worktrees
                ],
                [self.trees.branches[name] for name in self.trees.branch_order if name in self.trees.branches],
                self.bases.report(self.trees.prunable_paths()),
                [self.foreign.inspections[path] for path in self.foreign.roots if path in self.foreign.inspections],
            ),
            self.cache.report(self.bases.prunable_paths()),
            self.trees.worktree_removals,
            self.trees.branch_removals,
            self.trees.branch_recovery_refs,
            self.bases.removals,
            self.cache.removals,
            self.bases.sizes,
            self.cache.sizes,
            self.bases.free_before,
            self.cache.free_before,
        )

    async def run(self) -> Report:
        task = asyncio.current_task()
        assert task is not None
        self._task = task
        try:
            self.seed()
            if self._stop_requested:
                task.cancel()
            result: Report | None = None
            failure: QueueError | asyncio.CancelledError | None = None
            async with asyncio.TaskGroup() as tasks:
                try:
                    result = await self._run_tasks(tasks)
                except (QueueError, asyncio.CancelledError) as error:
                    failure = error
            if failure is not None:
                raise failure
            assert result is not None
            return result
        finally:
            self._task = None

    async def _run_tasks(self, tasks: asyncio.TaskGroup) -> Report:
        loop = asyncio.get_running_loop()
        next_update = loop.time()
        reporting = self.observer is not NULL_OBSERVER
        try:
            while self.queue:
                for started in self.queue.start(tasks):
                    logger.info("Starting %s: %s", started.phase, jobs.describe(started))
                if reporting and loop.time() >= next_update:
                    self.observer.update(self.queue, waiting=self.waiting())
                    next_update = loop.time() + 0.2
                if reporting:
                    try:
                        async with asyncio.timeout(max(0, next_update - loop.time())):
                            item, result = await self.queue.next()
                    except TimeoutError:
                        continue
                else:
                    item, result = await self.queue.next()
                if isinstance(result, WorkFailure):
                    self._record_stopped(item, result)
                    raise result.error
                logger.info("Finished %s: %s (%s)", item.phase, jobs.describe(item), result.outcome)
                self.handle(result.value)
            if (
                self.trees.waiting_branches
                or self.trees.worktree_candidates
                or self.trees.branch_candidates
                or self.bases.pending_workspaces is not None
                or self.foreign.candidates
                or not self.prs.quiescent
                or self.cache.waiting_batch
                or self.cache.batch_active
            ):
                raise RuntimeError("work queue drained with unresolved dependencies")
            if reporting:
                self.observer.update(self.queue, waiting=self.waiting())
        except BaseException as error:
            for item in self.queue.stop():
                if isinstance(item, jobs.EmptyCacheTrash):
                    self.cache.removals.append(Failed(item.path, "cleanup stopped with entry in trash", item.trash))
            if reporting:
                try:
                    self.observer.update(self.queue, waiting=self.waiting())
                except Exception:
                    logger.warning("Progress observer failed while queue was draining", exc_info=True)
            # Keep the drain inside the TaskGroup, but outside the cancellation of this
            # coordinator task. A second cancellation must not cancel admitted writes.
            drain = asyncio.create_task(self.queue.drain())
            while True:
                try:
                    stopped = await asyncio.shield(drain)
                    break
                except asyncio.CancelledError:
                    continue
            for stopped_item, stopped_result in stopped:
                self._record_stopped(stopped_item, stopped_result)
            if reporting:
                try:
                    self.observer.update(self.queue, waiting=self.waiting())
                except Exception:
                    logger.warning("Progress observer failed after queue drain", exc_info=True)
            if isinstance(error, Exception):
                raise QueueError(error, self.report()) from error
            raise
        return self.report()
