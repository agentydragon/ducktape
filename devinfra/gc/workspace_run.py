"""Workspace garbage collection as concurrent, dependency-aware item coroutines."""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import shutil
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager, nullcontext, suppress
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
    Branch,
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


class Run:
    """One TaskGroup owns every discovered item; prerequisites never hold permits.

    An item keeps its permit from inspection through immediately eligible removal. This
    gives deletion a turn even when a large inventory is waiting at the semaphore.
    Only genuine prerequisites (PRs, holders, references, cache locking) release it.
    """

    def __init__(self, options: Options, observer: Observer = NULL_OBSERVER) -> None:
        if options.workers < 1:
            raise ValueError("workers must be positive")
        self.options, self.observer = options, observer
        self._phases: dict[str, _Phase] = {}
        self._waiting = 0
        self._stopped = False
        self._discovering = True
        self._producers = 0
        self.semaphore = asyncio.Semaphore(options.workers)
        # Bound only scans waiting at the shared semaphore, not active scans. Follow-ups
        # can then join behind at most `workers` scans instead of the entire inventory.
        self.scan_admission = asyncio.Semaphore(options.workers)
        self.pr_semaphore = asyncio.Semaphore(min(github_prs.MAX_CONCURRENT_REQUESTS, max(1, options.workers - 1)))
        self.cache_lock = asyncio.Lock()
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
        self.worktree_tasks: dict[Path, asyncio.Task[None]] = {}
        self.base_tasks: dict[Path, asyncio.Task[None]] = {}
        self.worktrees: dict[Path, Classification] = {}
        self.resolved_worktrees: dict[Path, Path | None] = {}
        self.worktree_outcomes: dict[Path, RemovalResult[Path]] = {}
        self.branch_order: list[str] = []
        self.branches: dict[str, BranchClassification] = {}
        self.bases: dict[Path, Inspection] = {}
        self.base_workspaces: dict[Path, Path | None] = {}
        self.annotated_workspaces: set[Path] = set()
        self.entries: dict[Path, EntryInspection] = {}
        self.foreign: dict[Path, ForeignCloneClassification] = {}
        self.foreign_roots: set[Path] = set()
        self.foreign_workspaces: set[Path] = set()
        self.worktree_removals: list[RemovalResult[Path]] = []
        self.branch_removals: list[RemovalResult[str]] = []
        self.branch_recovery_refs: dict[str, str] = {}
        self.base_removals: list[RemovalResult[Path]] = []
        self.cache_removals: list[RemovalResult[Path]] = []
        self.base_sizes: dict[Path, int | None] = {}
        self.cache_sizes: dict[Path, int | None] = {}
        self.base_free_before: int | None = None
        self.cache_free_before: int | None = None
        self.cache_pending: list[PrunableEntry] = []
        self.cache_flush: asyncio.Task[None] | None = None

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

    def _spawn(self, coroutine: Coroutine[Any, Any, object], *, producer: bool = True) -> asyncio.Task[None]:
        assert self.tasks is not None
        self._producers += int(producer)
        self._discovering = self._producers > 0
        task = self.tasks.create_task(self._guard(coroutine))

        def finished(_: asyncio.Task[None]) -> None:
            # The wrapper can be cancelled before its first instruction. Close its child
            # even then, and count producers only after all their descendants were added.
            coroutine.close()
            self._producers -= int(producer)
            self._discovering = self._producers > 0

        task.add_done_callback(finished)
        return task

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
                    names = tasks.create_task(workspace_scan.pr_branch_candidates(self.options.repo))
                repository = facts.result()
                self.facts = repository
                self.repository_ready.set()
                self._request_prs(names.result())
                trees = []
                if self.options.domains & {Domain.WORKTREES, Domain.BRANCHES}:
                    trees = [
                        wt for wt in await git_repo.list_worktrees(self.options.repo) if wt.path != self.facts.main_path
                    ]
                live = await run_blocking(worktree_gc.processes_by_worktree, [wt.path for wt in trees])
                self._request_prs({wt.branch for wt in trees if wt.branch})
                self.local_pending += len(trees)
                for wt in trees:
                    self.worktree_tasks[wt.path] = self._spawn(self._worktree(wt, live.get(wt.path, [])))
                self.worktrees_ready.set()
                if Domain.BRANCHES in self.options.domains:
                    self.branch_order = await run_blocking(
                        lambda: branch_gc.local_branches(git_repo.open_repo(repository.main_path))
                    )
                    holders = await branch_gc.branch_holders(self.options.repo)
                    self._request_prs(set(self.branch_order))
                    for name in self.branch_order:
                        holder = holders.get(name)
                        local = holder is None or holder == self.facts.main_path
                        self.local_pending += int(local)
                        self._spawn(self._branch(name, holder, local))
                self.local_inventory_done = True
                if self.local_pending == 0:
                    self.local_ready.set()

    async def _worktree(self, wt: Worktree, live: list[int]) -> None:
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
                self.resolved_worktrees[wt.path] = await run_blocking(workspace_scan.resolve_path, wt.path)
                self._local_finished()
                names = {item.worktree.branch} if item.worktree.branch else set()
                self._request_prs(names)
            # If the lookup already finished, continue to deletion without joining the
            # semaphore's scan backlog a second time.
            if self.prs.disabled or all(self.pr_tasks[name].done() for name in names):
                await self._finish_worktree(item)
                return
        await self._prerequisite_prs(names)
        async with self.slot("remove worktrees", wt.path, scan=False):
            await self._finish_worktree(item)

    async def _finish_worktree(self, item: Classification) -> None:
        assert self.facts is not None
        facts = self.facts
        item = worktree_gc.resolve_worktree_pr(item, self.prs)
        self.worktrees[item.worktree.path] = item
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

                result = await self._mutate(remove(), item.worktree.path, self.worktree_removals)
                self.worktree_outcomes[item.worktree.path] = result
                record.value = type(result).__name__.lower()

    async def _branch(self, name: str, path: Path | None, local: bool) -> None:
        assert self.facts is not None
        facts = self.facts
        holder: Holder = None
        if path == facts.main_path:
            holder = MainCheckout()
        elif path is not None:
            task = self.worktree_tasks.get(path)
            if task is not None:
                await self._wait(asyncio.shield(task))
            holder = self.worktrees.get(path)
            if holder is None or (
                self.options.apply
                and Domain.WORKTREES in self.options.domains
                and isinstance(holder, PrunableWorktree)
                and not isinstance(self.worktree_outcomes.get(path), Removed)
            ):
                self.branches[name] = RetainedBranch(Branch(name), RetainedBranchReason.HELD_BY_WORKTREE, path)
                return
            if isinstance(self.worktree_outcomes.get(path), Removed):
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
        self.branches[name] = item
        if isinstance(item, PrunableBranch) and self.options.apply:
            with self._step("remove branches", name) as record:
                try:
                    outcome = await self._owned(
                        branch_gc.remove_prunable_branch(
                            facts.repo, item, main=item.main, default_branch=facts.default_branch
                        )
                    )
                except Exception as error:
                    self.branch_removals.append(Failed(name, f"{type(error).__name__}: {error}"))
                    raise
                self.branch_removals.append(outcome.result)
                record.value = type(outcome.result).__name__.lower()
                if outcome.recovery_prefix is not None:
                    self.branch_recovery_refs[name] = outcome.recovery_prefix

    async def _discover_bases(self) -> None:
        async with self.slot("discover"):
            with self._step("discover", self.options.root):
                bases = await run_blocking(
                    output_base_gc.list_output_bases, self.options.root, session_bases=self.options.session_bases
                )
                points = await run_blocking(output_base_gc.mount_points)
                self.base_free_before = await run_blocking(_free_space, self.options.root)
                for path, layout in bases:
                    self.base_tasks[path] = self._spawn(self._base(path, layout, points))
                if Domain.REPO_CACHE in self.options.domains:
                    self._spawn(self._cache(tuple(self.base_tasks)))
        if self.options.domains == {Domain.BASES} and self.options.repo is not None:
            for task in self.base_tasks.values():
                await self._wait(asyncio.shield(task))
            await self._annotate_base_workspaces()

    async def _base(self, path: Path, layout: BaseLayout, points: set[Path]) -> None:
        async with self.slot("inspect bases", path):
            await self._inspect_base(path, layout, points)
            item = self.bases[path]
            if isinstance(item, PrunableBase):
                return
        if isinstance(item, RetainedBase) and item.workspace is not None and Domain.WORKTREES in self.options.domains:
            await self._wait(self.worktrees_ready.wait())
            # Resolve both sides: Git and Bazel can spell the same workspace differently.
            resolved = self.base_workspaces[path]
            holder = None
            async with self.slot("resolve workspaces", path):
                with self._step("resolve workspaces", path):
                    for wt in self.worktree_tasks:
                        if await run_blocking(workspace_scan.resolve_path, wt) == resolved:
                            holder = wt
                            break
            if holder is not None:
                await self._wait(asyncio.shield(self.worktree_tasks[holder]))
                if isinstance(self.worktree_outcomes.get(holder), Removed):
                    async with self.slot("inspect bases", path, scan=False):
                        await self._inspect_base(path, layout, points)

    async def _inspect_base(self, path: Path, layout: BaseLayout, points: set[Path]) -> None:
        with self._step("inspect bases", path) as record:
            item = await run_blocking(
                output_base_gc.inspect_listed_base, path, layout=layout, uid=os.getuid(), points=points
            )
            self.bases[path] = item
            record.value = verdict_of(item).lower()
            resolved = (
                await run_blocking(workspace_scan.resolve_path, item.workspace)
                if isinstance(item, RetainedBase) and item.workspace is not None
                else None
            )
            self.base_workspaces[path] = resolved
            if (
                Domain.FOREIGN_CLONES in self.options.domains
                and isinstance(item, RetainedBase)
                and item.workspace is not None
                and item.workspace not in self.foreign_workspaces
            ):
                self.foreign_workspaces.add(item.workspace)
                self._spawn(self._foreign_workspace(item.workspace))
        if (
            self.options.sizes
            and (self.options.show_all or not isinstance(item, RetainedBase))
            and path not in self.base_sizes
        ):
            with self._step("measure bases", path):
                self.base_sizes[path] = await _allocated_bytes(path)
        if isinstance(item, PrunableBase) and self.options.apply and Domain.BASES in self.options.domains:
            with self._step("delete bases", path) as record:
                result = await self._mutate(
                    run_blocking(output_base_gc.delete_prunable_base, item), path, self.base_removals
                )
                record.value = type(result).__name__.lower()

    async def _annotate_base_workspaces(self) -> None:
        assert self.options.repo is not None
        async with self.slot("discover base workspaces"):
            with self._step("discover base workspaces", self.options.repo):
                worktrees = await workspace_scan.base_worktrees(self.options.repo, self.bases.values())
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

    async def _cache(self, bases: tuple[Path, ...]) -> None:
        async with self.slot("discover repo cache"):
            with self._step("discover repo cache"):
                contents = repo_contents_cache_gc.repo_contents_cache(self.options.root)
                try:
                    contents = await run_blocking(contents.resolve, strict=True)
                except FileNotFoundError:
                    return
                buckets = await run_blocking(repo_contents_cache_gc.cache_buckets, contents)
                references = await run_blocking(repo_contents_cache_gc.references, bases, contents)
                points = await run_blocking(output_base_gc.mount_points)
                self.cache_free_before = await run_blocking(_free_space, contents)
                for bucket in buckets:
                    self._spawn(self._cache_bucket(bucket, references, points))

    async def _cache_bucket(self, bucket: Path, references: ReferenceInventory, points: set[Path]) -> None:
        async with self.slot("discover repo-cache buckets", bucket):
            with self._step("discover repo-cache buckets", bucket):
                candidates = await run_blocking(repo_contents_cache_gc.list_cache_bucket, bucket)
            if candidates:
                # Look past retained/review entries before giving up the discovery
                # permit, so an eligible entry is not stranded behind blocked scans.
                for index, candidate in enumerate(candidates):
                    remaining = index + 1
                    if await self._cache_entry(candidate, references, points, held=True):
                        break
                for candidate in candidates[remaining:]:
                    self._spawn(self._cache_entry(candidate, references, points))

    async def _cache_entry(
        self, candidate: Path | ReviewEntry, references: ReferenceInventory, points: set[Path], *, held: bool = False
    ) -> bool:
        """Inspect one entry; return whether its eligible removal ran on this permit."""
        async with nullcontext() if held else self.slot("inspect repo-cache entries"):
            with self._step("inspect repo-cache entries") as record:
                raw = await run_blocking(
                    repo_contents_cache_gc.inspect_cache_candidate, candidate, uid=os.getuid(), points=points
                )
                item = repo_contents_cache_gc.classify_entry(raw, references, set())
                path = repo_contents_cache_gc.entry_path(item)
                self.entries[path] = item
                record.value = verdict_of(item).lower()
            if self.options.sizes and (self.options.show_all or not isinstance(item, RetainedEntry)):
                with self._step("measure cache entries", path):
                    self.cache_sizes[path] = await _allocated_bytes(path)
            if self.options.apply and isinstance(item, PrunableEntry) and not self.cache_lock.locked():
                async with self.cache_lock:
                    # Include already eligible peers while preserving immediate progress.
                    peers = tuple(self.cache_pending[:127])
                    del self.cache_pending[:127]
                    self._waiting -= len(peers)
                    trashed = await self._trash_cache_batch((item, *peers), schedule_cleanup=False)
                for original, trash in trashed:
                    if not self._stopped:
                        await self._empty_cache(original, trash, held=True)
                return True
            if held:
                self._spawn(self._finish_cache_entry(item))
                return False
        await self._finish_cache_entry(item)
        return False

    async def _finish_cache_entry(self, item: EntryInspection) -> None:
        path = repo_contents_cache_gc.entry_path(item)
        if not self.options.apply:
            return
        if isinstance(item, RetainedEntry):
            for base in item.referrers:
                task = self.base_tasks.get(base)
                if task is None:
                    return
                await self._wait(asyncio.shield(task))
            deleted = {result.subject for result in self.base_removals if isinstance(result, Removed)}
            if not item.referrers <= deleted:
                return
            item = PrunableEntry(item.entry)
        if isinstance(item, PrunableEntry):
            if self.options.sizes and path not in self.cache_sizes:
                async with self.slot("measure cache entries", path, scan=False):
                    with self._step("measure cache entries", path):
                        self.cache_sizes[path] = await _allocated_bytes(path)
            self.cache_pending.append(item)
            self._waiting += 1
            if self.cache_flush is None or self.cache_flush.done():
                self.cache_flush = self._spawn(self._flush_cache())

    async def _flush_cache(self) -> None:
        # Coalesce entries that become eligible together; one lock owner checks references
        # and trashes up to 128 at once. Trash emptying is independent concurrent work.
        async with self.cache_lock:
            while self.cache_pending:
                async with self.slot("trash cache batches", scan=False):
                    batch = tuple(self.cache_pending[:128])
                    del self.cache_pending[:128]
                    self._waiting -= len(batch)
                    await self._trash_cache_batch(batch, schedule_cleanup=True)

    async def _trash_cache_batch(
        self, batch: tuple[PrunableEntry, ...], *, schedule_cleanup: bool
    ) -> list[tuple[Path, Path]]:
        with self._step("trash cache batches", f"{len(batch)} entries"):
            try:
                trashed = await self._owned(
                    run_blocking(
                        repo_contents_cache_gc.trash_prunable_entries,
                        batch,
                        contents=repo_contents_cache_gc.repo_contents_cache(self.options.root),
                        output_user_root=self.options.root,
                        session_bases=self.options.session_bases,
                    )
                )
            except Exception as error:
                self.cache_removals.extend(
                    Failed(item.entry.contents, f"{type(error).__name__}: {error}") for item in batch
                )
                raise
            self.cache_removals.extend(trashed.results)
            # Record ownership before spawning cleanup: cancellation can close
            # admission immediately after the rename has succeeded.
            for original, trash in trashed.trashed:
                self.cache_removals.append(Failed(original, "cleanup stopped with entry in trash", trash))
            if schedule_cleanup and not self._stopped:
                for original, trash in trashed.trashed:
                    self._spawn(self._empty_cache(original, trash), producer=False)
            return trashed.trashed

    async def _empty_cache(self, original: Path, trash: Path, *, held: bool = False) -> None:
        async with nullcontext() if held else self.slot("delete cache entries", original, scan=False):
            with self._step("delete cache entries", original) as record:
                results: list[RemovalResult[Path]] = []
                try:
                    result = await self._mutate(
                        run_blocking(repo_contents_cache_gc.empty_trash, original, trash), original, results
                    )
                    record.value = type(result).__name__.lower()
                finally:
                    if results:
                        self.cache_removals[:] = [
                            result for result in self.cache_removals if result.subject != original
                        ]
                        self.cache_removals.extend(
                            replace(result, leftover=trash) if isinstance(result, Failed) else result
                            for result in results
                        )

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
                        self._spawn(self._foreign_clone(root))

    async def _foreign_clone(self, root: Path) -> None:
        assert self.facts is not None
        async with self.slot("discover foreign clone branches", root):
            with self._step("discover foreign clone branches", root):
                names = await workspace_scan.foreign_clone_branch_candidates((root,))
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
            resolved
            for path, item in self.worktrees.items()
            if isinstance(item, PrunableWorktree)
            if (resolved := self.resolved_worktrees.get(path)) is not None
        }
        bases = [
            replace(item, workspace_is_prunable_worktree=True)
            if isinstance(item, RetainedBase) and self.base_workspaces.get(path) in prunable_worktrees
            else item
            for path, item in self.bases.items()
        ]
        prunable_bases = {path for path, item in self.bases.items() if isinstance(item, PrunableBase)}
        entries = [
            replace(
                item,
                reason=RetainedEntryReason.ONLY_PRUNABLE_REFERRERS
                if item.referrers <= prunable_bases
                else RetainedEntryReason.REFERENCED,
            )
            if isinstance(item, RetainedEntry)
            else item
            for path, item in sorted(self.entries.items())
        ]
        return Report(
            WorkspaceScan(
                [self.worktrees[path] for path in self.worktree_tasks if path in self.worktrees],
                [self.branches[name] for name in self.branch_order if name in self.branches],
                bases,
                [self.foreign[path] for path in sorted(self.foreign)],
            ),
            entries,
            self.worktree_removals,
            self.branch_removals,
            self.branch_recovery_refs,
            self.base_removals,
            self.cache_removals,
            self.base_sizes,
            self.cache_sizes,
            self.base_free_before,
            self.cache_free_before,
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
                self._spawn(self._repository())
            else:
                self.local_ready.set()
                self.worktrees_ready.set()
            if self.options.domains & {Domain.BASES, Domain.REPO_CACHE, Domain.FOREIGN_CLONES}:
                self._spawn(self._discover_bases())

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
