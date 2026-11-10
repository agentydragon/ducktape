"""Discover, inspect and remove workspace state through a shared work queue."""

from __future__ import annotations

import enum
import logging
import shutil
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from devinfra.gc import git_repo, repo_contents_cache_gc, workspace_jobs as jobs
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


class _Run:
    def __init__(self, options: Options, observer: Observer) -> None:
        self.options = options
        self.observer = observer
        self.queue: WorkQueue[jobs.Event] = WorkQueue(options.workers)
        self.context: jobs.Context | None = None
        self.foreign_roots: list[Path] = []
        self.base_workspaces: dict[Path, Classification] = {}
        self.worktree_order: list[Path] | None = None
        self.worktrees: dict[Path, Classification] = {}
        self.worktree_outcomes: dict[Path, RemovalResult[Path]] = {}
        self.branch_order: list[str] = []
        self.branches: dict[str, BranchClassification] = {}
        self.waiting_branches: dict[Path, list[str]] = {}
        self.base_order: list[Path] = []
        self.base_layouts: dict[Path, BaseLayout] = {}
        self.base_points: set[Path] = set()
        self.base_initial_pending = 0
        self.bases: dict[Path, Inspection] = {}
        self.base_attempted: set[Path] = set()
        self.base_reinspected: set[Path] = set()
        self.base_reinspection_pending: set[Path] = set()
        self.deleted_worktrees: set[Path] = set()
        self.deleted_bases: set[Path] = set()
        self.annotation_scheduled = False
        self.entries: list[EntryInspection] = []
        self.cache_by_path: dict[Path, EntryInspection] = {}
        self.cache_by_base: dict[Path, set[Path]] = {}
        self.cache_attempted: set[Path] = set()
        self.cache_waiting: deque[PrunableEntry] = deque()
        self.cache_batch_active = False
        self.foreign: dict[Path, ForeignCloneClassification] = {}
        self.worktree_removals: list[RemovalResult[Path]] = []
        self.branch_removals: list[RemovalResult[str]] = []
        self.base_removals: list[RemovalResult[Path]] = []
        self.cache_removals: list[RemovalResult[Path]] = []
        self.foreign_removals: list[RemovalResult[Path]] = []
        self.base_sizes: dict[Path, int | None] = {}
        self.cache_sizes: dict[Path, int | None] = {}
        self.base_free_before: int | None = None
        self.cache_free_before: int | None = None

    def _context(self) -> jobs.Context:
        assert self.context is not None
        return self.context

    def seed(self) -> None:
        domains = self.options.domains
        if domains & {Domain.WORKTREES, Domain.BRANCHES, Domain.FOREIGN_CLONES}:
            assert self.options.repo is not None
            self.queue.submit(
                jobs.DiscoverContext(
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
        pending_bases = self.base_attempted - {result.subject for result in self.base_removals}
        if self.options.apply and Domain.BASES in self.options.domains:
            pending_bases |= set(self.base_order) - self.bases.keys()
            pending_bases |= self.base_reinspection_pending
            pending_worktrees = {
                resolved
                for path, item in self.worktrees.items()
                if Domain.WORKTREES in self.options.domains
                and isinstance(item, PrunableWorktree)
                and path not in self.worktree_outcomes
                if (resolved := _resolved(path)) is not None
            }
            pending_bases |= {
                base.path
                for base in self.bases.values()
                if isinstance(base, RetainedBase)
                and base.workspace is not None
                and _resolved(base.workspace) in pending_worktrees
            }
        cache = sum(
            isinstance(item, RetainedEntry)
            and bool(item.referrers & pending_bases)
            and item.referrers <= pending_bases | self.deleted_bases
            for path, item in self.cache_by_path.items()
            if path not in self.cache_attempted
        )
        return sum(map(len, self.waiting_branches.values())) + cache + len(self.cache_waiting)

    def _queue_branch(self, name: str, holder: Path | None) -> None:
        context = self._context()
        if holder is None:
            self.queue.submit(jobs.InspectBranch(context, name, None))
        elif holder == context.main_path:
            self.queue.submit(jobs.InspectBranch(context, name, MainCheckout()))
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
                    self.queue.submit(jobs.InspectBranch(self._context(), name, None), followup=True)
            else:
                for name in waiting:
                    self.branches[name] = RetainedBranch(Branch(name), RetainedBranchReason.HELD_BY_WORKTREE, path)
        else:
            for name in waiting:
                self.queue.submit(jobs.InspectBranch(self._context(), name, item), followup=True)
        del self.waiting_branches[path]

    def _reinspect_base(self, item: Inspection) -> None:
        if not isinstance(item, RetainedBase) or item.workspace is None:
            return
        workspace = _resolved(item.workspace)
        if workspace not in self.deleted_worktrees or item.path in self.base_reinspected:
            return
        layout = self.base_layouts.get(item.path)
        if layout is None:
            return
        self.base_reinspected.add(item.path)
        self.base_reinspection_pending.add(item.path)
        self.queue.submit(jobs.InspectBase(item.path, layout, self.base_points, initial=False), followup=True)

    def _after_initial_bases(self) -> None:
        if self.base_initial_pending:
            return
        if self.options.domains == {Domain.BASES} and self.options.repo is not None and not self.annotation_scheduled:
            self.annotation_scheduled = True
            self.queue.submit(
                jobs.DiscoverBaseWorkspaces(self.options.repo, tuple(self.bases.values()), self.options.pr_source)
            )

    def _eligible_cache(self, item: EntryInspection) -> None:
        if not self.options.apply or Domain.REPO_CACHE not in self.options.domains:
            return
        path = repo_contents_cache_gc.entry_path(item)
        if path in self.cache_attempted:
            return
        if isinstance(item, PrunableEntry):
            candidate = item
        elif isinstance(item, RetainedEntry) and item.referrers <= self.deleted_bases:
            candidate = PrunableEntry(item.entry)
        else:
            return
        self.cache_attempted.add(path)
        self.cache_waiting.append(candidate)
        if self.cache_free_before is None:
            self.cache_free_before = shutil.disk_usage(
                repo_contents_cache_gc.repo_contents_cache(self.options.root)
            ).free
        self._schedule_cache_batch()

    def _schedule_cache_batch(self) -> None:
        if self.cache_batch_active or not self.cache_waiting:
            return
        batch = tuple(self.cache_waiting.popleft() for _ in range(min(128, len(self.cache_waiting))))
        self.cache_batch_active = True
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

    def handle(self, event: jobs.Event) -> None:
        match event:
            case jobs.FoundContext(context=context, foreign_roots=roots):
                self.context = context
                self.foreign_roots = roots
                if self.options.domains & {Domain.WORKTREES, Domain.BRANCHES}:
                    self.queue.submit(jobs.DiscoverWorktrees(context))
                if Domain.BRANCHES in self.options.domains:
                    self.queue.submit(jobs.DiscoverBranches(context))
                if Domain.FOREIGN_CLONES in self.options.domains:
                    for root in roots:
                        self.queue.submit(jobs.InspectForeignClone(context, root))
            case jobs.FoundBaseWorkspaces(context=base_context, worktrees=base_worktrees, live=base_live):
                for base_worktree in base_worktrees:
                    self.queue.submit(
                        jobs.InspectBaseWorkspace(base_context, base_worktree, base_live.get(base_worktree.path, []))
                    )
            case jobs.InspectedBaseWorkspace(item=base_workspace):
                self.base_workspaces[base_workspace.worktree.path] = base_workspace
            case jobs.FoundWorktrees(worktrees=worktrees, live=live):
                self.worktree_order = [wt.path for wt in worktrees]
                for wt in worktrees:
                    self.queue.submit(jobs.InspectWorktree(self._context(), wt, live.get(wt.path, [])))
                for holder_path in list(self.waiting_branches):
                    self._release_branches(holder_path)
            case jobs.InspectedWorktree(item=worktree):
                wt_path = worktree.worktree.path
                self.worktrees[wt_path] = worktree
                if (
                    isinstance(worktree, PrunableWorktree)
                    and self.options.apply
                    and Domain.WORKTREES in self.options.domains
                ):
                    self.queue.submit(jobs.RemoveWorktree(self._context(), worktree), followup=True)
                else:
                    self._release_branches(wt_path)
            case jobs.RemovedWorktree(result=worktree_result):
                self.worktree_removals.append(worktree_result)
                self.worktree_outcomes[worktree_result.subject] = worktree_result
                if isinstance(worktree_result, Removed):
                    resolved_worktree = _resolved(worktree_result.subject)
                    if resolved_worktree is not None:
                        self.deleted_worktrees.add(resolved_worktree)
                    for existing_base in self.bases.values():
                        self._reinspect_base(existing_base)
                self._release_branches(worktree_result.subject)
            case jobs.FoundBranches(names=names, holders=holders):
                self.branch_order = names
                for name in names:
                    self._queue_branch(name, holders.get(name))
            case jobs.InspectedBranch(item=branch):
                self.branches[branch.branch.name] = branch
                if isinstance(branch, PrunableBranch) and self.options.apply:
                    self.queue.submit(jobs.RemoveBranch(self._context(), branch), followup=True)
            case jobs.RemovedBranch(result=branch_result):
                self.branch_removals.append(branch_result)
            case jobs.FoundBases(bases=listed_bases, points=points):
                self.base_order = [base_path for base_path, _ in listed_bases]
                self.base_layouts = dict(listed_bases)
                self.base_points = points
                self.base_initial_pending = len(listed_bases)
                for base_path, layout in listed_bases:
                    self.queue.submit(jobs.InspectBase(base_path, layout, points))
                if Domain.REPO_CACHE in self.options.domains:
                    self.queue.submit(
                        jobs.DiscoverCache(
                            repo_contents_cache_gc.repo_contents_cache(self.options.root), tuple(self.base_order)
                        ),
                        followup=True,
                    )
                self._after_initial_bases()
            case jobs.InspectedBase(item=base, initial=initial):
                self.bases[base.path] = base
                if initial:
                    self.base_initial_pending -= 1
                else:
                    self.base_reinspection_pending.discard(base.path)
                if isinstance(base, PrunableBase) and self.options.apply and Domain.BASES in self.options.domains:
                    if base.path not in self.base_attempted:
                        self.base_attempted.add(base.path)
                        if self.base_free_before is None:
                            self.base_free_before = shutil.disk_usage(self.options.root).free
                        self.queue.submit(jobs.RemoveBase(base, self.options.sizes), followup=True)
                else:
                    self._reinspect_base(base)
                self._after_initial_bases()
            case jobs.RemovedBase(result=base_result, size=base_size):
                self.base_removals.append(base_result)
                if self.options.sizes:
                    self.base_sizes[base_result.subject] = base_size
                if isinstance(base_result, Removed):
                    self.deleted_bases.add(base_result.subject)
                    for cache_path in self.cache_by_base.get(base_result.subject, set()):
                        self._eligible_cache(self.cache_by_path[cache_path])
            case jobs.FoundCacheBuckets(buckets=buckets, context=cache_context):
                for bucket in buckets:
                    self.queue.submit(jobs.DiscoverCacheBucket(bucket, cache_context))
            case jobs.FoundCacheCandidates(candidates=cache_candidates, context=candidate_context):
                for cache_candidate in cache_candidates:
                    self.queue.submit(jobs.InspectCacheEntry(cache_candidate, candidate_context))
            case jobs.InspectedCacheEntry(item=cache_entry):
                self.entries.append(cache_entry)
                cache_path = repo_contents_cache_gc.entry_path(cache_entry)
                self.cache_by_path[cache_path] = cache_entry
                if isinstance(cache_entry, RetainedEntry):
                    for referrer in cache_entry.referrers:
                        self.cache_by_base.setdefault(referrer, set()).add(cache_path)
                self._eligible_cache(cache_entry)
            case jobs.TrashedCacheEntries(trashed=trashed, sizes=sizes):
                self.cache_batch_active = False
                self.cache_removals.extend(trashed.results)
                self.cache_sizes.update(sizes)
                for original, trash in trashed.trashed:
                    self.queue.submit(jobs.EmptyCacheTrash(original, trash), followup=True)
                self._schedule_cache_batch()
            case jobs.RemovedCacheEntry(result=cache_result):
                self.cache_removals.append(cache_result)
            case jobs.InspectedForeignClone(item=clone):
                self.foreign[clone.clone.root] = clone
                if (
                    isinstance(clone, PrunableForeignClone)
                    and self.options.apply
                    and self.options.delete_foreign_clones
                ):
                    self.queue.submit(
                        jobs.RemoveForeignClone(
                            self._context(), clone.clone.root, self.options.root, self.options.session_bases
                        ),
                        followup=True,
                    )
            case jobs.RemovedForeignClone(result=clone_result):
                self.foreign_removals.append(clone_result)

    def _record_stopped(
        self, item: WorkItem[jobs.Event], result: Finished[jobs.Event] | WorkFailure | Cancelled
    ) -> None:
        if isinstance(result, Cancelled):
            if isinstance(item, jobs.EmptyCacheTrash):
                self.cache_removals.append(Failed(item.path, "cleanup stopped with entry in trash", item.trash))
            return
        if isinstance(result, WorkFailure):
            error = f"{type(result.error).__name__}: {result.error}"
            match item:
                case jobs.RemoveWorktree(item=worktree):
                    self.worktree_removals.append(Failed(worktree.worktree.path, error))
                case jobs.RemoveBranch(item=branch):
                    self.branch_removals.append(Failed(branch.branch.name, error))
                case jobs.RemoveBase(item=base):
                    self.base_removals.append(Failed(base.path, error))
                case jobs.EmptyCacheTrash(path=path, trash=trash):
                    self.cache_removals.append(Failed(path, error, trash))
                case jobs.TrashCacheEntries(entries=entries):
                    self.cache_removals.extend(Failed(entry.entry.contents, error) for entry in entries)
                case jobs.RemoveForeignClone(root=root):
                    self.foreign_removals.append(Failed(root, error))
            return
        match result.value:
            case jobs.RemovedWorktree(result=worktree_result):
                self.worktree_removals.append(worktree_result)
            case jobs.RemovedBranch(result=branch_result):
                self.branch_removals.append(branch_result)
            case jobs.RemovedBase(result=base_result, size=size):
                self.base_removals.append(base_result)
                if self.options.sizes:
                    self.base_sizes[base_result.subject] = size
            case jobs.TrashedCacheEntries(trashed=trashed, sizes=sizes):
                self.cache_sizes.update(sizes)
                self.cache_removals.extend(trashed.results)
                self.cache_removals.extend(
                    Failed(path, "cleanup stopped with entry in trash", trash) for path, trash in trashed.trashed
                )
            case jobs.RemovedCacheEntry(result=cache_result):
                self.cache_removals.append(cache_result)
            case jobs.RemovedForeignClone(result=clone_result):
                self.foreign_removals.append(clone_result)

    def report(self) -> Report:
        prunable = {
            path
            for item in (*self.worktrees.values(), *self.base_workspaces.values())
            if isinstance(item, PrunableWorktree)
            if (path := _resolved(item.worktree.path)) is not None
        }
        bases = []
        for path in self.base_order:
            item = self.bases.get(path)
            if item is None:
                continue
            bases.append(annotate_base(item, prunable))
        prunable_bases = {base.path for base in self.bases.values() if isinstance(base, PrunableBase)}
        entries = [
            replace(
                entry,
                reason=(
                    RetainedEntryReason.ONLY_PRUNABLE_REFERRERS
                    if entry.referrers <= prunable_bases
                    else RetainedEntryReason.REFERENCED
                ),
            )
            if isinstance(entry, RetainedEntry)
            else entry
            for entry in self.entries
        ]
        return Report(
            WorkspaceScan(
                [self.worktrees[path] for path in self.worktree_order or [] if path in self.worktrees],
                [self.branches[name] for name in self.branch_order if name in self.branches],
                bases,
                [self.foreign[path] for path in self.foreign_roots if path in self.foreign],
            ),
            sorted(entries, key=repo_contents_cache_gc.entry_path),
            self.worktree_removals,
            self.branch_removals,
            self.base_removals,
            self.cache_removals,
            self.foreign_removals,
            self.base_sizes,
            self.cache_sizes,
            self.base_free_before,
            self.cache_free_before,
        )

    async def run(self) -> Report:
        self.seed()
        try:
            while self.queue:
                for started in self.queue.start():
                    logger.info("Starting %s: %s", started.phase, jobs.describe(started))
                self.observer.update(self.queue, waiting=self.waiting())
                item, result = await self.queue.next()
                if isinstance(result, WorkFailure):
                    self._record_stopped(item, result)
                    raise result.error
                logger.info("Finished %s: %s (%s)", item.phase, jobs.describe(item), result.outcome)
                self.handle(result.value)
                self.observer.update(self.queue, waiting=self.waiting())
            if self.waiting_branches or self.cache_waiting or self.cache_batch_active:
                raise RuntimeError("work queue drained with unresolved dependencies")
        except BaseException as error:
            for item in self.queue.stop():
                if isinstance(item, jobs.EmptyCacheTrash):
                    self.cache_removals.append(Failed(item.path, "cleanup stopped with entry in trash", item.trash))
            git_repo.interrupt_running_git()
            for stopped_item, stopped_result in await self.queue.drain():
                self._record_stopped(stopped_item, stopped_result)
            self.observer.update(self.queue, waiting=self.waiting())
            if isinstance(error, Exception):
                raise QueueError(error, self.report()) from error
            raise QueueInterrupted(self.report()) from error
        return self.report()


async def run(options: Options, observer: Observer = NULL_OBSERVER) -> Report:
    return await _Run(options, observer).run()
