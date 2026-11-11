"""Discover, inspect and remove workspace state through a shared work queue."""

from __future__ import annotations

import asyncio
import enum
import logging
import shutil
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from devinfra.gc import repo_contents_cache_gc, workspace_jobs as jobs
from devinfra.gc.branch_gc import (
    Branch,
    BranchClassification,
    MainCheckout,
    PrunableBranch,
    RetainedBranch,
    RetainedBranchReason,
)
from devinfra.gc.foreign_clone_gc import ForeignCloneClassification, PrunableForeignClone
from devinfra.gc.output_base_gc import BaseLayout, Inspection, PrunableBase, RetainedBase
from devinfra.gc.removal import Failed, RemovalResult, Removed
from devinfra.gc.repo_contents_cache_gc import EntryInspection, PrunableEntry, RetainedEntry, RetainedEntryReason
from devinfra.gc.work_queue import Cancelled, Finished, WorkFailure, WorkItem, WorkQueue
from devinfra.gc.workspace_scan import WorkspaceScan, annotate_base
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
    delete_foreign_clones: bool
    repo: Path | None
    pr_source: jobs.PrSource


@dataclass(frozen=True, slots=True)
class Report:
    scan: WorkspaceScan
    entries: list[EntryInspection]
    worktree_removals: list[RemovalResult[Path]]
    branch_removals: list[RemovalResult[str]]
    base_removals: list[RemovalResult[Path]]
    cache_removals: list[RemovalResult[Path]]
    foreign_removals: list[RemovalResult[Path]]
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


class QueueInterrupted(KeyboardInterrupt):
    def __init__(self, report: Report) -> None:
        self.report = report


def _resolved(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


class _WorktreesAndBranches:
    """Schedule branch inspection after the worktree holding it is settled."""

    def __init__(self, options: Options, queue: WorkQueue[jobs.Event]) -> None:
        self.options = options
        self.queue = queue
        self.facts: jobs.RepositoryFacts | None = None
        self.worktree_order: list[Path] | None = None
        self.worktrees: dict[Path, Classification] = {}
        self.worktree_outcomes: dict[Path, RemovalResult[Path]] = {}
        self.branch_order: list[str] = []
        self.branches: dict[str, BranchClassification] = {}
        self.waiting_branches: dict[Path, list[str]] = {}
        self.worktree_removals: list[RemovalResult[Path]] = []
        self.branch_removals: list[RemovalResult[str]] = []

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
            self.queue.submit(jobs.InspectWorktree(self.repository(), wt, event.live.get(wt.path, [])))
        for path in list(self.waiting_branches):
            self._release_branches(path)

    def inspected_worktree(self, item: Classification) -> None:
        path = item.worktree.path
        self.worktrees[path] = item
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
                self.queue.submit(jobs.InspectBranch(self.repository(), name, None))
            elif holder == self.repository().main_path:
                self.queue.submit(jobs.InspectBranch(self.repository(), name, MainCheckout()))
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
                    self.queue.submit(jobs.InspectBranch(self.repository(), name, None), followup=True)
            else:
                for name in waiting:
                    self.branches[name] = RetainedBranch(Branch(name), RetainedBranchReason.HELD_BY_WORKTREE, path)
        else:
            for name in waiting:
                self.queue.submit(jobs.InspectBranch(self.repository(), name, item), followup=True)
        del self.waiting_branches[path]

    def inspected_branch(self, item: BranchClassification) -> None:
        self.branches[item.branch.name] = item
        if isinstance(item, PrunableBranch) and self.options.apply:
            self.queue.submit(jobs.RemoveBranch(self.repository(), item), followup=True)

    def pending_worktrees(self) -> set[Path]:
        return {
            resolved
            for path, item in self.worktrees.items()
            if Domain.WORKTREES in self.options.domains
            and isinstance(item, PrunableWorktree)
            and path not in self.worktree_outcomes
            if (resolved := _resolved(path)) is not None
        }

    def waiting(self) -> int:
        return sum(map(len, self.waiting_branches.values()))

    def prunable_paths(self) -> set[Path]:
        return {
            path
            for item in self.worktrees.values()
            if isinstance(item, PrunableWorktree)
            if (path := _resolved(item.worktree.path)) is not None
        }


@dataclass(slots=True)
class _Base:
    layout: BaseLayout
    inspection: Inspection | None = None
    attempted: bool = False
    reinspected: bool = False
    reinspection_pending: bool = False
    removal: RemovalResult[Path] | None = None


class _Bases:
    """Own base reinspection after a referenced worktree disappears."""

    def __init__(self, options: Options, queue: WorkQueue[jobs.Event]) -> None:
        self.options = options
        self.queue = queue
        self.workspaces: dict[Path, Classification] = {}
        self.records: dict[Path, _Base] = {}
        self.points: set[Path] = set()
        self.deleted_worktrees: set[Path] = set()
        self.annotation_scheduled = False
        self.removals: list[RemovalResult[Path]] = []
        self.sizes: dict[Path, int | None] = {}
        self.free_before: int | None = None

    def found(self, event: jobs.FoundBases) -> None:
        self.records = {path: _Base(layout) for path, layout in event.bases}
        self.points = event.points
        for path, layout in event.bases:
            self.queue.submit(jobs.InspectBase(path, layout, event.points))
        self._after_initial()

    def found_workspaces(self, event: jobs.FoundBaseWorkspaces) -> None:
        for worktree in event.worktrees:
            self.queue.submit(jobs.InspectBaseWorkspace(event.facts, worktree, event.live.get(worktree.path, [])))

    def inspected(self, item: Inspection, *, initial: bool) -> None:
        record = self.records[item.path]
        record.inspection = item
        if not initial:
            record.reinspection_pending = False
        if isinstance(item, PrunableBase) and self.options.apply and Domain.BASES in self.options.domains:
            if not record.attempted:
                record.attempted = True
                if self.free_before is None:
                    self.free_before = shutil.disk_usage(self.options.root).free
                self.queue.submit(jobs.RemoveBase(item, self.options.sizes), followup=True)
        else:
            self._reinspect(item)
        self._after_initial()

    def worktree_removed(self, result: RemovalResult[Path]) -> None:
        if not isinstance(result, Removed):
            return
        path = _resolved(result.subject)
        if path is not None:
            self.deleted_worktrees.add(path)
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
        if _resolved(item.workspace) not in self.deleted_worktrees or record.reinspected:
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
                    self.options.pr_source,
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
                    and _resolved(record.inspection.workspace) in pending_worktrees
                )
            }
        return pending

    def report(self, prunable: set[Path]) -> list[Inspection]:
        prunable |= {
            path
            for item in self.workspaces.values()
            if isinstance(item, PrunableWorktree)
            if (path := _resolved(item.worktree.path)) is not None
        }
        return [
            annotate_base(record.inspection, prunable)
            for record in self.records.values()
            if record.inspection is not None
        ]

    def prunable_paths(self) -> set[Path]:
        return {path for path, record in self.records.items() if isinstance(record.inspection, PrunableBase)}


@dataclass(slots=True)
class _CacheEntry:
    inspection: EntryInspection
    attempted: bool = False


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
        self._eligible(item)

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
        record.attempted = True
        self.waiting_batch.append(candidate)
        if self.free_before is None:
            self.free_before = shutil.disk_usage(repo_contents_cache_gc.repo_contents_cache(self.options.root)).free
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
                self.options.sizes,
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
        self.inspections: dict[Path, ForeignCloneClassification] = {}
        self.removals: list[RemovalResult[Path]] = []

    def repository_ready(self, facts: jobs.RepositoryFacts, roots: list[Path]) -> None:
        self.facts = facts
        self.roots = roots
        if Domain.FOREIGN_CLONES in self.options.domains:
            for root in roots:
                self.queue.submit(jobs.InspectForeignClone(facts, root))

    def inspected(self, item: ForeignCloneClassification) -> None:
        self.inspections[item.clone.root] = item
        if isinstance(item, PrunableForeignClone) and self.options.apply and self.options.delete_foreign_clones:
            assert self.facts is not None
            self.queue.submit(
                jobs.RemoveForeignClone(self.facts, item.clone.root, self.options.root, self.options.session_bases),
                followup=True,
            )


class _Run:
    def __init__(self, options: Options, observer: Observer) -> None:
        self.options = options
        self.observer = observer
        self.queue: WorkQueue[jobs.Event] = WorkQueue(options.workers)
        self.trees = _WorktreesAndBranches(options, self.queue)
        self.bases = _Bases(options, self.queue)
        self.cache = _Cache(options, self.queue)
        self.foreign = _ForeignClones(options, self.queue)

    def seed(self) -> None:
        domains = self.options.domains
        if domains & {Domain.WORKTREES, Domain.BRANCHES, Domain.FOREIGN_CLONES}:
            assert self.options.repo is not None
            self.queue.submit(
                jobs.PrepareRepositoryScan(
                    self.options.repo,
                    self.options.root,
                    self.options.session_bases,
                    Domain.FOREIGN_CLONES in domains,
                    self.options.pr_source,
                )
            )
        if domains & {Domain.BASES, Domain.REPO_CACHE}:
            self.queue.submit(jobs.DiscoverBases(self.options.root, self.options.session_bases))

    def waiting(self) -> int:
        return self.trees.waiting() + self.cache.waiting(self.bases.pending(self.trees))

    def handle(self, event: jobs.Event) -> None:
        match event:
            case jobs.PreparedRepositoryScan(facts=facts, foreign_roots=roots):
                self.trees.repository_ready(facts)
                self.foreign.repository_ready(facts, roots)
            case jobs.FoundBaseWorkspaces():
                self.bases.found_workspaces(event)
            case jobs.InspectedBaseWorkspace(item=item):
                self.bases.workspaces[item.worktree.path] = item
            case jobs.FoundWorktrees():
                self.trees.found_worktrees(event)
            case jobs.InspectedWorktree(item=item):
                self.trees.inspected_worktree(item)
            case jobs.RemovedWorktree(result=result):
                self.trees.removed_worktree(result)
                self.bases.worktree_removed(result)
            case jobs.FoundBranches():
                self.trees.found_branches(event)
            case jobs.InspectedBranch(item=item):
                self.trees.inspected_branch(item)
            case jobs.RemovedBranch(result=result):
                self.trees.branch_removals.append(result)
            case jobs.FoundBases():
                self.bases.found(event)
                self.cache.found_bases(list(self.bases.records))
            case jobs.InspectedBase(item=item, initial=initial):
                self.bases.inspected(item, initial=initial)
            case jobs.RemovedBase(result=result, size=size):
                self.bases.removed(result, size)
                self.cache.base_removed(result)
            case jobs.FoundCacheBuckets(buckets=buckets, facts=facts):
                for bucket in buckets:
                    self.queue.submit(jobs.DiscoverCacheBucket(bucket, facts))
            case jobs.FoundCacheCandidates(candidates=candidates, facts=facts):
                for candidate in candidates:
                    self.queue.submit(jobs.InspectCacheEntry(candidate, facts))
            case jobs.InspectedCacheEntry(item=item):
                self.cache.inspected(item)
            case jobs.TrashedCacheEntries():
                self.cache.trashed(event)
            case jobs.RemovedCacheEntry(result=result):
                self.cache.removals.append(result)
            case jobs.InspectedForeignClone(item=item):
                self.foreign.inspected(item)
            case jobs.RemovedForeignClone(result=result):
                self.foreign.removals.append(result)

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
                    self.trees.branch_removals.append(Failed(branch.branch.name, error))
                case jobs.RemoveBase(item=base):
                    self.bases.removed(Failed(base.path, error), None, measured=False)
                case jobs.EmptyCacheTrash(path=path, trash=trash):
                    self.cache.removals.append(Failed(path, error, trash))
                case jobs.TrashCacheEntries(entries=entries):
                    self.cache.removals.extend(Failed(entry.entry.contents, error) for entry in entries)
                case jobs.RemoveForeignClone(root=root):
                    self.foreign.removals.append(Failed(root, error))
            return
        match result.value:
            case jobs.RemovedWorktree(result=removed):
                self.trees.worktree_removals.append(removed)
            case jobs.RemovedBranch(result=removed):
                self.trees.branch_removals.append(removed)
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
            case jobs.RemovedForeignClone(result=removed):
                self.foreign.removals.append(removed)

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
            self.bases.removals,
            self.cache.removals,
            self.foreign.removals,
            self.bases.sizes,
            self.cache.sizes,
            self.bases.free_before,
            self.cache.free_before,
        )

    async def run(self) -> Report:
        self.seed()
        result: Report | None = None
        failure: QueueError | QueueInterrupted | None = None
        async with asyncio.TaskGroup() as tasks:
            try:
                result = await self._run_tasks(tasks)
            except (QueueError, QueueInterrupted) as error:
                failure = error
        if failure is not None:
            raise failure
        assert result is not None
        return result

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
            if self.trees.waiting_branches or self.cache.waiting_batch or self.cache.batch_active:
                raise RuntimeError("work queue drained with unresolved dependencies")
            if reporting:
                self.observer.update(self.queue, waiting=self.waiting())
        except BaseException as error:
            for item in self.queue.stop():
                if isinstance(item, jobs.EmptyCacheTrash):
                    self.cache.removals.append(Failed(item.path, "cleanup stopped with entry in trash", item.trash))
            for stopped_item, stopped_result in await self.queue.drain():
                self._record_stopped(stopped_item, stopped_result)
            if reporting:
                self.observer.update(self.queue, waiting=self.waiting())
            if isinstance(error, Exception):
                raise QueueError(error, self.report()) from error
            raise QueueInterrupted(self.report()) from error
        return self.report()


async def run(options: Options, observer: Observer = NULL_OBSERVER) -> Report:
    return await _Run(options, observer).run()
