"""Workspace garbage collection as concurrent, dependency-aware item coroutines."""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import shutil
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from devinfra.gc import (
    branch_gc,
    foreign_clone_gc,
    git_repo,
    github_prs,
    output_base_gc,
    repo_contents_cache_gc,
    workspace_scan,
    worktree_gc,
)
from devinfra.gc.async_io import _wait_owned, run_blocking, run_process
from devinfra.gc.branch_gc import (
    BranchClassification,
    Holder,
    MainCheckout,
    PrunableBranch,
    RetainedBranch,
    RetainedBranchReason,
)
from devinfra.gc.foreign_clone_gc import ForeignCloneClassification
from devinfra.gc.git_repo import Worktree
from devinfra.gc.output_base_gc import BaseLayout, Inspection, PrunableBase, RetainedBase
from devinfra.gc.pull_request import PrLookup
from devinfra.gc.removal import Failed, RemovalResult, Removed
from devinfra.gc.repo_contents_cache_gc import (
    EntryInspection,
    PrunableEntry,
    ReferenceInventory,
    RetainedEntry,
    RetainedEntryReason,
    ReviewEntry,
)
from devinfra.gc.scan_progress import ProgressPhase, ProgressState
from devinfra.gc.verdict import verdict_of
from devinfra.gc.workspace_scan import WorkspaceScan
from devinfra.gc.worktree_gc import Classification, PrunableWorktree

logger = logging.getLogger(__name__)
DEFAULT_WORKERS = 8
type PrSource = Callable[[Path, set[str]], Awaitable[PrLookup]]


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
    pr_source: PrSource
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


@dataclass(frozen=True, slots=True)
class RepositoryFacts:
    repo: Path
    main_path: Path
    active_path: Path | None
    main: str
    default_branch: str
    prs: PrLookup


class Observer(Protocol):
    def update(self, progress: ProgressState) -> None: ...


class NullObserver:
    def update(self, progress: ProgressState) -> None:
        pass


NULL_OBSERVER: Observer = NullObserver()


class RunError(Exception):
    def __init__(self, error: Exception, report: Report) -> None:
        super().__init__(str(error))
        self.error = error
        self.report = report


def _free_space(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


async def _allocated_bytes(path: Path) -> int | None:
    try:
        result = await run_process("du", "-sx", "--block-size=1", "--", os.fspath(path))
        return None if result.returncode else int(result.stdout.split(maxsplit=1)[0])
    except OSError, ValueError, IndexError:
        return None


async def _repository_facts(repo: Path, prs: PrLookup) -> RepositoryFacts:
    def refs() -> tuple[str, str]:
        pg = git_repo.open_repo(repo)
        return git_repo.main_ref(pg), git_repo.default_branch_name(pg)

    main_path = await git_repo.main_worktree(repo)
    toplevel = (await git_repo.git(repo, "rev-parse", "--show-toplevel", check=False)).stdout.strip()
    main, default = await run_blocking(refs)
    return RepositoryFacts(repo, main_path, Path(toplevel) if toplevel else None, main, default, prs)


@dataclass(slots=True)
class _Phase:
    discovered: int = 0
    ready: int = 0
    active: int = 0
    completed: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class _Outcome:
    value: str = "done"


@dataclass(slots=True)
class _WorktreeState:
    worktree: Worktree
    resolved: Path | None
    task: asyncio.Task[None] | None = None
    classification: Classification | None = None
    removal: RemovalResult[Path] | None = None


@dataclass(slots=True)
class _BaseState:
    path: Path
    layout: BaseLayout
    task: asyncio.Task[None] | None = None
    classification: Inspection | None = None
    resolved_workspace: Path | None = None
    removal: RemovalResult[Path] | None = None


@dataclass(slots=True)
class _BranchState:
    classification: BranchClassification | None = None
    removal: RemovalResult[str] | None = None
    recovery_ref: str | None = None


class _CacheDomain:
    """Own repository-cache discovery, eligibility, batching, and cleanup."""

    def __init__(self, run: Run) -> None:
        self.run = run
        self.entries: dict[Path, EntryInspection] = {}
        self.removals: dict[Path, RemovalResult[Path]] = {}
        self.sizes: dict[Path, int | None] = {}
        self.free_before: int | None = None
        self.pending: list[PrunableEntry] = []
        self.flush: asyncio.Task[None] | None = None

    async def discover(self, bases: tuple[Path, ...]) -> None:
        async with self.run.slot("discover repo cache"):
            with self.run._step("discover repo cache"):
                contents = repo_contents_cache_gc.repo_contents_cache(self.run.options.root)
                try:
                    contents = await run_blocking(contents.resolve, strict=True)
                except FileNotFoundError:
                    return
                buckets = await run_blocking(repo_contents_cache_gc.cache_buckets, contents)
                references = await run_blocking(repo_contents_cache_gc.references, bases, contents)
                points = await run_blocking(output_base_gc.mount_points)
                self.free_before = await run_blocking(_free_space, contents)
        # Limit outstanding bucket discovery so entries from a completed bucket can
        # reach the shared worker pool before an arbitrary backlog of bucket scans.
        pending: set[asyncio.Task[None]] = set()
        for bucket in buckets:
            if len(pending) >= self.run.options.workers:
                _, pending = await self.run._wait(asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED))
            pending.add(self.run._spawn(self._cache_bucket(bucket, references, points), producer=True))

    async def _cache_bucket(self, bucket: Path, references: ReferenceInventory, points: set[Path]) -> None:
        async with self.run.slot("discover repo-cache buckets", bucket):
            with self.run._step("discover repo-cache buckets", bucket):
                candidates = await run_blocking(repo_contents_cache_gc.list_cache_bucket, bucket)
        inspections = [self.run._spawn(self._cache_entry(candidate, references, points)) for candidate in candidates]
        task = asyncio.current_task()
        assert task is not None
        self.run._finish_producer(task)
        if inspections:
            await self.run._wait(asyncio.gather(*inspections))

    async def _cache_entry(
        self, candidate: Path | ReviewEntry, references: ReferenceInventory, points: set[Path]
    ) -> None:
        async with self.run.slot("inspect repo-cache entries"):
            with self.run._step("inspect repo-cache entries") as record:
                raw = await run_blocking(
                    repo_contents_cache_gc.inspect_cache_candidate, candidate, uid=os.getuid(), points=points
                )
                item = repo_contents_cache_gc.classify_entry(raw, references, set())
                path = repo_contents_cache_gc.entry_path(item)
                self.entries[path] = item
                record.value = verdict_of(item).lower()
            if self.run.options.sizes and (self.run.options.show_all or not isinstance(item, RetainedEntry)):
                with self.run._step("measure cache entries", path):
                    self.sizes[path] = await _allocated_bytes(path)
        if isinstance(item, RetainedEntry):
            self.run._spawn(self._finish_cache_entry(item))
        else:
            await self._finish_cache_entry(item)
            if isinstance(item, PrunableEntry) and self.flush is not None:
                await self.run._wait(asyncio.shield(self.flush))

    async def _finish_cache_entry(self, item: EntryInspection) -> None:
        path = repo_contents_cache_gc.entry_path(item)
        if not self.run.options.apply:
            return
        if isinstance(item, RetainedEntry):
            for base in item.referrers:
                state = self.run.bases.get(base)
                if state is None or state.task is None:
                    return
                await self.run._wait(asyncio.shield(state.task))
                if not isinstance(state.removal, Removed):
                    return
            item = PrunableEntry(item.entry)
        if isinstance(item, PrunableEntry):
            if self.run.options.sizes and path not in self.sizes:
                async with self.run.slot("measure cache entries", path, scan=False):
                    with self.run._step("measure cache entries", path):
                        self.sizes[path] = await _allocated_bytes(path)
            self.pending.append(item)
            self.run._waiting += 1
            if self.flush is None or self.flush.done():
                self.flush = self.run._spawn(self._flush_cache())

    async def _flush_cache(self) -> None:
        # A single flusher takes whatever is ready, without waiting to fill a batch.
        while self.pending:
            async with self.run.slot("trash cache batches", scan=False):
                batch = tuple(self.pending[:128])
                del self.pending[:128]
                self.run._waiting -= len(batch)
                trashed = await self._trash_cache_batch(batch)
            # Cleanup must not hold the lock used for revalidation and rename.
            if not self.run._stopped:
                cleanup = [self.run._spawn(self._empty_cache(original, trash)) for original, trash in trashed]
                if cleanup:
                    await self.run._wait(asyncio.gather(*cleanup))

    async def _trash_cache_batch(self, batch: tuple[PrunableEntry, ...]) -> list[tuple[Path, Path]]:
        with self.run._step("trash cache batches", f"{len(batch)} entries"):
            try:
                trashed = await self.run._owned(
                    run_blocking(
                        repo_contents_cache_gc.trash_prunable_entries,
                        batch,
                        contents=repo_contents_cache_gc.repo_contents_cache(self.run.options.root),
                        output_user_root=self.run.options.root,
                        session_bases=self.run.options.session_bases,
                    )
                )
            except Exception as error:
                for item in batch:
                    path = item.entry.contents
                    self.removals[path] = Failed(path, f"{type(error).__name__}: {error}")
                raise
            for result in trashed.results:
                self.removals[result.subject] = result
            # Record ownership before spawning cleanup: cancellation can close
            # admission immediately after the rename has succeeded.
            for original, trash in trashed.trashed:
                self.removals[original] = Failed(original, "cleanup stopped with entry in trash", trash)
            return trashed.trashed

    async def _empty_cache(self, original: Path, trash: Path) -> None:
        async with self.run.slot("delete cache entries", original, scan=False):
            with self.run._step("delete cache entries", original) as record:
                results: list[RemovalResult[Path]] = []
                try:
                    result = await self.run._mutate(
                        run_blocking(repo_contents_cache_gc.empty_trash, original, trash), original, results
                    )
                    record.value = type(result).__name__.lower()
                finally:
                    if results:
                        result = results[-1]
                        self.removals[original] = (
                            replace(result, leftover=trash) if isinstance(result, Failed) else result
                        )


class Run:
    """One TaskGroup owns every discovered item; prerequisites never hold permits.

    Immediately eligible work can retain its permit through removal; prerequisites
    release it. Cache batches join the worker pool without waiting for all scans.
    """

    def __init__(self, options: Options, observer: Observer = NULL_OBSERVER) -> None:
        if options.workers < 1:
            raise ValueError("workers must be positive")
        self.options, self.observer = options, observer
        self.cache = _CacheDomain(self)
        self._phases: dict[str, _Phase] = {}
        self._waiting = 0
        self._stopped = False
        self._discovering = True
        self._producer_tasks: set[asyncio.Task[None]] = set()
        self.semaphore = asyncio.Semaphore(options.workers)
        # Bound only scans waiting at the shared semaphore, not active scans. Follow-ups
        # can then join behind at most `workers` scans instead of the entire inventory.
        self.scan_admission = asyncio.Semaphore(options.workers)
        self.pr_semaphore = asyncio.Semaphore(min(github_prs.MAX_CONCURRENT_REQUESTS, max(1, options.workers - 1)))
        self.tasks: asyncio.TaskGroup | None = None
        self._task: asyncio.Task[None] | None = None
        self._observer_error: Exception | None = None
        self.facts: RepositoryFacts | None = None
        self.repository_ready = asyncio.Event()
        self.worktrees_ready = asyncio.Event()
        self.local_ready = asyncio.Event()
        self.local_pending = 0
        self.local_inventory_done = False
        self.prs = PrLookup({})
        self.pr_tasks: dict[str, asyncio.Task[None]] = {}
        self.worktrees: dict[Path, _WorktreeState] = {}
        self.worktrees_by_resolved_path: dict[Path, _WorktreeState | None] = {}
        self.branches: dict[str, _BranchState] = {}
        self.bases: dict[Path, _BaseState] = {}
        self.annotated_workspaces: set[Path] = set()
        self.foreign: dict[Path, ForeignCloneClassification] = {}
        self.foreign_roots: set[Path] = set()
        self.foreign_workspaces: set[Path] = set()
        self.base_sizes: dict[Path, int | None] = {}
        self.base_free_before: int | None = None

    @property
    def progress(self) -> ProgressState:
        return ProgressState(
            {
                name: ProgressPhase(p.discovered, p.ready, p.active, p.completed, dict(p.outcomes))
                for name, p in self._phases.items()
            },
            self._waiting,
            self._discovering,
            self._stopped,
        )

    def request_stop(self) -> None:
        if not self._stopped:
            self._stopped = True
            if self._task is not None:
                self._task.cancel()
            self._notify()

    def _notify(self) -> None:
        try:
            self.observer.update(self.progress)
        except Exception:
            if not self._stopped:
                raise
            logger.warning("Progress observer failed while draining", exc_info=True)

    def _spawn(self, coroutine: Coroutine[Any, Any, object], *, producer: bool = False) -> asyncio.Task[None]:
        assert self.tasks is not None
        task = self.tasks.create_task(self._guard(coroutine))
        if producer:
            self._producer_tasks.add(task)
            self._discovering = True

        def finished(_: asyncio.Task[None]) -> None:
            # The wrapper can be cancelled before its first instruction. Close its child
            # even then, and count producers only after all their descendants were added.
            coroutine.close()
            self._finish_producer(task)

        task.add_done_callback(finished)
        return task

    def _finish_producer(self, task: asyncio.Task[Any]) -> None:
        if task in self._producer_tasks:
            self._producer_tasks.remove(task)
            self._discovering = bool(self._producer_tasks)

    async def _guard(self, coroutine: Coroutine[Any, Any, object]) -> None:
        try:
            if self._stopped:
                coroutine.close()
                return
            await coroutine
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._stopped = True
            self._notify()
            if isinstance(error, Exception):
                raise
            raise RuntimeError(f"{type(error).__name__}: {error}") from error

    @asynccontextmanager
    async def slot(self, phase: str, description: object = "", *, scan: bool = True) -> AsyncIterator[None]:
        counter = self._phases.setdefault(phase, _Phase())
        counter.discovered += 1
        counter.ready += 1
        admitted = False
        scan_admitted = False
        try:
            if scan:
                await self.scan_admission.acquire()
                scan_admitted = True
            await self.semaphore.acquire()
            admitted = True
            if self._stopped:
                raise asyncio.CancelledError
        except BaseException:
            if admitted:
                self.semaphore.release()
            counter.completed += 1
            counter.outcomes["cancelled"] += 1
            raise
        else:
            counter.discovered -= 1
        finally:
            if scan_admitted:
                self.scan_admission.release()
            counter.ready -= 1
        try:
            yield
        finally:
            if admitted:
                self.semaphore.release()

    @contextmanager
    def _step(self, phase: str, description: object = "") -> Iterator[_Outcome]:
        if self._stopped:
            raise asyncio.CancelledError
        counter = self._phases.setdefault(phase, _Phase())
        counter.discovered += 1
        counter.active += 1
        outcome = _Outcome()

        logger.info("Starting %s: %s", phase, description)
        try:
            yield outcome
        except asyncio.CancelledError:
            outcome.value = "cancelled"
            raise
        except BaseException:
            outcome.value = "failed"
            raise
        finally:
            counter.active -= 1
            counter.completed += 1
            counter.outcomes[outcome.value] += 1
            logger.info("Finished %s: %s (%s)", phase, description, outcome.value)

    async def _wait[T](self, awaitable: Awaitable[T]) -> T:
        self._waiting += 1
        try:
            return await awaitable
        finally:
            self._waiting -= 1

    async def _owned[T](self, operation: Coroutine[Any, Any, T]) -> T:
        """Admission and submission have no intervening await; drain admitted writes."""
        if self._stopped:
            operation.close()
            raise asyncio.CancelledError

        async def normalize() -> T:
            try:
                return await operation
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                if isinstance(error, Exception):
                    raise
                raise RuntimeError(f"{type(error).__name__}: {error}") from error

        return await _wait_owned(asyncio.create_task(normalize()))

    async def _mutate[T](
        self, operation: Coroutine[Any, Any, RemovalResult[T]], subject: T, results: list[RemovalResult[T]]
    ) -> RemovalResult[T]:
        try:
            result = await self._owned(operation)
        except Exception as error:
            results.append(Failed(subject, f"{type(error).__name__}: {error}"))
            raise
        results.append(result)
        return result

    def _request_prs(self, names: set[str]) -> None:
        if self.prs.disabled:
            return
        for batch in github_prs.pr_batches(names - self.pr_tasks.keys()):
            task = self._spawn(self._lookup_prs(batch), producer=False)
            self.pr_tasks.update(dict.fromkeys(batch, task))

    async def _lookup_prs(self, names: frozenset[str]) -> None:
        if self.options.workers == 1:
            await self._wait(self.local_ready.wait())
        async with self.pr_semaphore, self.slot("check pull requests"):
            if self.prs.disabled:
                return
            with self._step("check pull requests", f"{len(names)} branches"):
                assert self.options.repo is not None
                try:
                    result = await self.options.pr_source(self.options.repo, set(names))
                except Exception as error:
                    logger.warning("PR check failed for %d branches: %s", len(names), error)
                    result = PrLookup({}, names)
                if result.disabled:
                    self.prs = result
                elif not self.prs.disabled:
                    self.prs = PrLookup(self.prs.states | result.states, set(self.prs.unknown) | set(result.unknown))

    async def _prerequisite_prs(self, names: set[str]) -> None:
        self._request_prs(names)
        if self.prs.disabled:
            return
        for task in {self.pr_tasks[name] for name in names}:
            await self._wait(asyncio.shield(task))

    def _local_finished(self) -> None:
        self.local_pending -= 1
        if self.local_inventory_done and self.local_pending == 0:
            self.local_ready.set()

    async def _repository(self) -> None:
        assert self.options.repo is not None
        async with self.slot("discover git state"):
            with self._step("discover git state", self.options.repo):
                async with asyncio.TaskGroup() as tasks:
                    facts = tasks.create_task(_repository_facts(self.options.repo, PrLookup({}, disabled=True)))
                    pr_candidates = tasks.create_task(workspace_scan.pr_branch_candidates(self.options.repo))
                repository = facts.result()
                self.facts = repository
                self.repository_ready.set()
                self._request_prs(pr_candidates.result())
                trees = []
                if self.options.domains & {Domain.WORKTREES, Domain.BRANCHES}:
                    trees = [
                        wt for wt in await git_repo.list_worktrees(self.options.repo) if wt.path != self.facts.main_path
                    ]
                resolved_paths = await run_blocking(lambda: [workspace_scan.resolve_path(wt.path) for wt in trees])
                for wt, resolved in zip(trees, resolved_paths, strict=True):
                    state = _WorktreeState(wt, resolved)
                    self.worktrees[wt.path] = state
                    if resolved is not None:
                        # A duplicate identity cannot safely identify a single holder.
                        if resolved in self.worktrees_by_resolved_path:
                            self.worktrees_by_resolved_path[resolved] = None
                        else:
                            self.worktrees_by_resolved_path[resolved] = state
                live = await run_blocking(worktree_gc.processes_by_worktree, [wt.path for wt in trees])
                self._request_prs({wt.branch for wt in trees if wt.branch})
                self.local_pending += len(trees)
                for state in self.worktrees.values():
                    state.task = self._spawn(self._worktree(state, live.get(state.worktree.path, [])))
                self.worktrees_ready.set()
                if Domain.BRANCHES in self.options.domains:
                    names = await run_blocking(
                        lambda: branch_gc.local_branches(git_repo.open_repo(repository.main_path))
                    )
                    self.branches = {name: _BranchState() for name in names}
                    holders = await branch_gc.branch_holders(self.options.repo)
                    self._request_prs(set(names))
                    for name in names:
                        holder = holders.get(name)
                        local = holder is None or holder == self.facts.main_path
                        self.local_pending += int(local)
                        self._spawn(self._branch(name, holder, local))
                self.local_inventory_done = True
                if self.local_pending == 0:
                    self.local_ready.set()

    async def _worktree(self, state: _WorktreeState, live: list[int]) -> None:
        wt = state.worktree
        assert self.facts is not None
        facts = self.facts
        async with self.slot("inspect worktrees", wt.path):
            with self._step("inspect worktrees", wt.path) as record:
                item = await worktree_gc.classify_worktree(
                    wt,
                    main=facts.main,
                    pr_states={},
                    main_path=facts.main_path,
                    active_path=facts.active_path,
                    live_pids=live,
                )
                record.value = verdict_of(item).lower()
                self._local_finished()
                names = {item.worktree.branch} if item.worktree.branch else set()
                self._request_prs(names)
            # If the lookup already finished, continue to deletion without joining the
            # semaphore's scan backlog a second time.
            if self.prs.disabled or all(self.pr_tasks[name].done() for name in names):
                await self._finish_worktree(state, item)
                return
        await self._prerequisite_prs(names)
        async with self.slot("remove worktrees", wt.path, scan=False):
            await self._finish_worktree(state, item)

    async def _finish_worktree(self, state: _WorktreeState, item: Classification) -> None:
        assert self.facts is not None
        facts = self.facts
        item = worktree_gc.resolve_worktree_pr(item, self.prs)
        state.classification = item
        if isinstance(item, PrunableWorktree) and self.options.apply and Domain.WORKTREES in self.options.domains:
            with self._step("remove worktrees", item.worktree.path) as record:

                async def remove() -> RemovalResult[Path]:
                    live = await run_blocking(worktree_gc.processes_by_worktree, [item.worktree.path])
                    return await worktree_gc.remove_if_still_prunable(
                        facts.repo,
                        item.worktree,
                        main=item.main,
                        pr_states=self.prs.states,
                        main_path=facts.main_path,
                        active_path=facts.active_path,
                        live_pids=live.get(item.worktree.path, []),
                    )

                results: list[RemovalResult[Path]] = []
                try:
                    result = await self._mutate(remove(), item.worktree.path, results)
                finally:
                    if results:
                        state.removal = results[-1]
                record.value = type(result).__name__.lower()

    async def _branch(self, name: str, path: Path | None, local: bool) -> None:
        assert self.facts is not None
        facts = self.facts
        holder: Holder = None
        if path == facts.main_path:
            holder = MainCheckout()
        elif path is not None:
            state = self.worktrees.get(path)
            if state is not None and state.task is not None:
                await self._wait(asyncio.shield(state.task))
            holder = state.classification if state is not None else None
            if (
                state is None
                or holder is None
                or (
                    self.options.apply
                    and isinstance(holder, PrunableWorktree)
                    and not isinstance(state.removal, Removed)
                )
            ):
                self.branches[name].classification = RetainedBranch(name, RetainedBranchReason.HELD_BY_WORKTREE, path)
                return
            if state is not None and isinstance(state.removal, Removed):
                holder = None
        async with self.slot("inspect branches", name):
            with self._step("inspect branches", name) as record:
                pg = await run_blocking(git_repo.open_repo, facts.main_path)
                item = await branch_gc.classify_branch(
                    name, pg=pg, main=facts.main, default_branch=facts.default_branch, pr=None, holder=holder
                )
                record.value = verdict_of(item).lower()
                if local:
                    self._local_finished()
            if self.prs.disabled or self.pr_tasks[name].done():
                await self._finish_branch(name, item, holder)
                return
        await self._prerequisite_prs({name})
        async with self.slot("inspect branches", name, scan=False):
            await self._finish_branch(name, item, holder)

    async def _finish_branch(self, name: str, item: BranchClassification, holder: Holder) -> None:
        assert self.facts is not None
        facts = self.facts
        if name in self.prs.states:
            with self._step("inspect branches", name) as record:
                pg = await run_blocking(git_repo.open_repo, facts.main_path)
                item = await branch_gc.classify_branch(
                    name,
                    pg=pg,
                    main=facts.main,
                    default_branch=facts.default_branch,
                    pr=self.prs.states[name],
                    holder=holder,
                )
                record.value = verdict_of(item).lower()
        if name in self.prs.unknown and isinstance(item, PrunableBranch):
            item = branch_gc.ReviewBranch(item.branch, branch_gc.ReviewBranchReason.PR_LOOKUP_UNAVAILABLE, item.main)
        self.branches[name].classification = item
        if isinstance(item, PrunableBranch) and self.options.apply:
            with self._step("remove branches", name) as record:
                try:
                    outcome = await self._owned(branch_gc.remove_prunable_branch(facts.repo, item))
                except Exception as error:
                    self.branches[name].removal = Failed(name, f"{type(error).__name__}: {error}")
                    raise
                self.branches[name].removal = outcome.result
                record.value = type(outcome.result).__name__.lower()
                if outcome.recovery_prefix is not None:
                    self.branches[name].recovery_ref = outcome.recovery_prefix

    async def _discover_bases(self) -> None:
        async with self.slot("discover"):
            with self._step("discover", self.options.root):
                bases = await run_blocking(
                    output_base_gc.list_output_bases, self.options.root, session_bases=self.options.session_bases
                )
                points = await run_blocking(output_base_gc.mount_points)
                self.base_free_before = await run_blocking(_free_space, self.options.root)
                for path, layout in bases:
                    state = _BaseState(path, layout)
                    self.bases[path] = state
                    state.task = self._spawn(
                        self._base(state, points), producer=Domain.FOREIGN_CLONES in self.options.domains
                    )
                if Domain.REPO_CACHE in self.options.domains:
                    self._spawn(self.cache.discover(tuple(self.bases)), producer=True)
        if self.options.domains == {Domain.BASES} and self.options.repo is not None:
            for state in self.bases.values():
                assert state.task is not None
                await self._wait(asyncio.shield(state.task))
            await self._annotate_base_workspaces()

    async def _base(self, state: _BaseState, points: set[Path]) -> None:
        path = state.path
        async with self.slot("inspect bases", path):
            await self._inspect_base(state, points)
            item = state.classification
            if isinstance(item, PrunableBase):
                return
        if isinstance(item, RetainedBase) and item.workspace is not None and Domain.WORKTREES in self.options.domains:
            await self._wait(self.worktrees_ready.wait())
            # Resolve both sides: Git and Bazel can spell the same workspace differently.
            resolved = state.resolved_workspace
            holder = self.worktrees_by_resolved_path.get(resolved) if resolved is not None else None
            if holder is not None:
                assert holder.task is not None
                await self._wait(asyncio.shield(holder.task))
                if isinstance(holder.removal, Removed):
                    async with self.slot("inspect bases", path, scan=False):
                        await self._inspect_base(state, points)

    async def _inspect_base(self, state: _BaseState, points: set[Path]) -> None:
        path = state.path
        with self._step("inspect bases", path) as record:
            item = await run_blocking(
                output_base_gc.inspect_listed_base, path, layout=state.layout, uid=os.getuid(), points=points
            )
            state.classification = item
            record.value = verdict_of(item).lower()
            resolved = (
                await run_blocking(workspace_scan.resolve_path, item.workspace)
                if isinstance(item, RetainedBase) and item.workspace is not None
                else None
            )
            state.resolved_workspace = resolved
            if (
                Domain.FOREIGN_CLONES in self.options.domains
                and isinstance(item, RetainedBase)
                and item.workspace is not None
                and item.workspace not in self.foreign_workspaces
            ):
                self.foreign_workspaces.add(item.workspace)
                self._spawn(self._foreign_workspace(item.workspace), producer=True)
        if (
            self.options.sizes
            and (self.options.show_all or not isinstance(item, RetainedBase))
            and path not in self.base_sizes
        ):
            with self._step("measure bases", path):
                self.base_sizes[path] = await _allocated_bytes(path)
        if isinstance(item, PrunableBase) and self.options.apply and Domain.BASES in self.options.domains:
            with self._step("delete bases", path) as record:
                results: list[RemovalResult[Path]] = []
                try:
                    result = await self._mutate(run_blocking(output_base_gc.delete_prunable_base, item), path, results)
                finally:
                    if results:
                        state.removal = results[-1]
                record.value = type(result).__name__.lower()

    async def _annotate_base_workspaces(self) -> None:
        assert self.options.repo is not None
        async with self.slot("discover base workspaces"):
            with self._step("discover base workspaces", self.options.repo):
                worktrees = await workspace_scan.base_worktrees(
                    self.options.repo, (state.classification for state in self.bases.values() if state.classification)
                )
                facts = await _repository_facts(self.options.repo, PrLookup({}, disabled=True))
                live = await run_blocking(worktree_gc.processes_by_worktree, [wt.path for wt in worktrees])
                self._request_prs({wt.branch for wt in worktrees if wt.branch})
        for wt in worktrees:
            await self._prerequisite_prs({wt.branch} if wt.branch else set())
            async with self.slot("inspect base workspaces", wt.path):
                with self._step("inspect base workspaces", wt.path) as record:
                    item = await worktree_gc.classify_worktree(
                        wt,
                        main=facts.main,
                        pr_states=self.prs.states,
                        main_path=facts.main_path,
                        active_path=facts.active_path,
                        live_pids=live.get(wt.path, []),
                    )
                    item = worktree_gc.resolve_worktree_pr(item, self.prs)
                    record.value = verdict_of(item).lower()
                    resolved = await run_blocking(workspace_scan.resolve_path, wt.path)
                    if isinstance(item, PrunableWorktree) and resolved is not None:
                        self.annotated_workspaces.add(resolved)

    async def _foreign_workspace(self, workspace: Path) -> None:
        await self._wait(self.repository_ready.wait())
        assert self.facts is not None
        async with self.slot("discover foreign clones", workspace):
            with self._step("discover foreign clones", workspace):
                resolved = await run_blocking(workspace_scan.resolve_path, workspace)
                if resolved is None:
                    return
                known = await workspace_scan.known_worktree_paths(self.facts.repo)
                roots = await foreign_clone_gc.discover_foreign_clones(
                    {resolved}, known_paths=known, repo=self.facts.repo
                )
                for root in roots:
                    if root not in self.foreign_roots:
                        self.foreign_roots.add(root)
                        self._spawn(self._foreign_clone(root), producer=True)

    async def _foreign_clone(self, root: Path) -> None:
        assert self.facts is not None
        async with self.slot("discover foreign clone branches", root):
            with self._step("discover foreign clone branches", root):
                names = await workspace_scan.foreign_clone_branch_candidates((root,))
        self._request_prs(names)
        task = asyncio.current_task()
        assert task is not None
        self._finish_producer(task)
        await self._prerequisite_prs(names)
        async with self.slot("inspect foreign clones", root):
            with self._step("inspect foreign clones", root) as record:
                item = await foreign_clone_gc.classify_foreign_clone(
                    root, pr_states=self.prs.states, active_path=self.facts.active_path
                )
                if not isinstance(
                    item, foreign_clone_gc.RetainedForeignClone | foreign_clone_gc.ReviewForeignClone
                ) and any(member.branch in self.prs.unknown for member in item.clone.members if member.branch):
                    item = foreign_clone_gc.ReviewForeignClone(item.clone, "pull request state could not be determined")
                self.foreign[root] = item
                record.value = verdict_of(item).lower()

    def report(self) -> Report:
        prunable_worktrees = self.annotated_workspaces | {
            state.resolved
            for state in self.worktrees.values()
            if isinstance(state.classification, PrunableWorktree) and state.resolved is not None
        }
        bases = [
            replace(state.classification, workspace_is_prunable_worktree=True)
            if isinstance(state.classification, RetainedBase) and state.resolved_workspace in prunable_worktrees
            else state.classification
            for state in self.bases.values()
            if state.classification is not None
        ]
        prunable_bases = {state.path for state in self.bases.values() if isinstance(state.classification, PrunableBase)}
        entries = [
            replace(
                item,
                reason=RetainedEntryReason.ONLY_PRUNABLE_REFERRERS
                if item.referrers <= prunable_bases
                else RetainedEntryReason.REFERENCED,
            )
            if isinstance(item, RetainedEntry)
            else item
            for path, item in sorted(self.cache.entries.items())
        ]
        return Report(
            WorkspaceScan(
                [state.classification for state in self.worktrees.values() if state.classification is not None],
                [state.classification for state in self.branches.values() if state.classification is not None],
                bases,
                [self.foreign[path] for path in sorted(self.foreign)],
            ),
            entries,
            [state.removal for state in self.worktrees.values() if state.removal is not None],
            [state.removal for state in self.branches.values() if state.removal is not None],
            {name: state.recovery_ref for name, state in self.branches.items() if state.recovery_ref is not None},
            [state.removal for state in self.bases.values() if state.removal is not None],
            list(self.cache.removals.values()),
            self.base_sizes,
            self.cache.sizes,
            self.base_free_before,
            self.cache.free_before,
        )

    async def _observe(self, done: asyncio.Event) -> None:
        try:
            while not done.is_set():
                self._notify()
                try:
                    async with asyncio.timeout(0.2):
                        await done.wait()
                except TimeoutError:
                    pass
        except Exception as error:
            self._observer_error = error
            self.request_stop()

    async def _run_group(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            self.tasks = tasks
            if self.options.domains & {Domain.WORKTREES, Domain.BRANCHES, Domain.FOREIGN_CLONES}:
                self._spawn(self._repository(), producer=True)
            else:
                self.local_ready.set()
                self.worktrees_ready.set()
            if self.options.domains & {Domain.BASES, Domain.REPO_CACHE, Domain.FOREIGN_CLONES}:
                self._spawn(self._discover_bases(), producer=True)

    async def run(self) -> Report:
        done = asyncio.Event()
        # The caller owns this supervisor, but repeated caller cancellation never reaches
        # the TaskGroup twice. Children drain admitted mutations before group exit.
        self._task = asyncio.create_task(self._run_group())
        observer = asyncio.create_task(self._observe(done))
        try:
            if self._stopped:
                self._task.cancel()
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError:
                self.request_stop()
                with suppress(asyncio.CancelledError, Exception):
                    await _wait_owned(self._task)
                if self._observer_error is not None:
                    raise RunError(self._observer_error, self.report()) from self._observer_error
                raise
            except ExceptionGroup as error:
                # Preserve the original domain error for CLI text and library callers.
                cause = error.exceptions[0] if len(error.exceptions) == 1 else error
                assert isinstance(cause, Exception)
                raise RunError(cause, self.report()) from error
        finally:
            self._discovering = False
            done.set()
            await _wait_owned(observer)
            self._task = None
            try:
                self._notify()
            except Exception as error:
                self._observer_error = self._observer_error or error
        if self._observer_error is not None:
            raise RunError(self._observer_error, self.report()) from self._observer_error
        return self.report()
