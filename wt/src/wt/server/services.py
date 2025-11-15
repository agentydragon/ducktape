from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
import contextlib
from pathlib import Path
import time
from weakref import WeakSet

import pygit2

from ..shared.protocol import DaemonHealth, PRInfo, PRInfoDisabled, PRInfoError, PRInfoOk, WorktreeID
from .git_manager import GitManager, WorktreeInfo as GMWorktreeInfo
from .gitstatus_refresh import DebouncedGitstatusRefresh
from .gitstatusd_listener import GitstatusdListener, GitstatusWorkingSummary
from .pr_service import PRCacheError, PRCacheOk, PRService
from .repo_status import RepoStatus
from .types import DiscoveredWorktree
from .worktree_ids import make_worktree_id
from .worktree_index import WorktreeIndex


class GitService:
    def __init__(self, git_manager: GitManager) -> None:
        self._gm = git_manager

    def list_worktrees(self) -> list[GMWorktreeInfo]:
        return self._gm.list_worktrees()

    def get_repo_head_shorthand(self, path: Path) -> str | None:
        repo = self._gm.get_repo(path)
        if repo.head_is_detached:
            return None
        shorthand = repo.head.shorthand
        return shorthand if shorthand else None

    def worktree_remove(self, path: Path, *, force: bool = False) -> None:
        self._gm.worktree_remove(path, force=force)


class WorktreeIndexService:
    def __init__(
        self,
        *,
        get_index: Callable[[], WorktreeIndex | None],
        rebuild_index: Callable[[], Awaitable[object]],
        run_discovery_once: Callable[[], Awaitable[object]],
    ) -> None:
        self._get_index = get_index
        self._rebuild_index = rebuild_index
        self._run_discovery_once = run_discovery_once

    async def ensure_discovery(self) -> None:
        await self._run_discovery_once()

    async def ensure_index(self) -> None:
        if self._get_index() is None:
            await self._rebuild_index()

    def list_paths(self) -> list[Path]:
        idx = self._get_index()
        if not idx:
            return []
        return list(idx.by_path.keys())

    def get_by_path(self, p: Path) -> DiscoveredWorktree | None:
        idx = self._get_index()
        if not idx:
            return None
        return idx.get_by_path(p)

    def get_by_name(self, name: str) -> DiscoveredWorktree | None:
        idx = self._get_index()
        if not idx:
            return None
        return idx.get_by_name(name)

    def resolve_target(self, name: str | None, current_path: Path):
        idx = self._get_index()
        if not idx:
            return None
        return idx.resolve_target(name, current_path)

    def main(self) -> DiscoveredWorktree | None:
        idx = self._get_index()
        if not idx:
            return None
        return idx.main


class GitstatusdService:
    def __init__(
        self,
        get_client: Callable[[Path], GitstatusdListener | None],
        iter_client_paths: Callable[[], Iterable[Path]] | None = None,
        ensure_watcher_for_path: Callable[[Path], Awaitable[object]] | None = None,
        list_watchers: Callable[[], list[DebouncedGitstatusRefresh]] | None = None,
        clear_watchers: Callable[[], None] | None = None,
    ) -> None:
        self._get_client = get_client
        self._iter_client_paths = iter_client_paths
        self._ensure_watcher_for_path = ensure_watcher_for_path
        self._list_watchers = list_watchers
        self._clear_watchers = clear_watchers
        # Squash trivial wrapper: expose provided callable directly
        self.get_client = get_client  # type: ignore[assignment]

    def get_cached_status(self, path: Path) -> GitstatusWorkingSummary:
        client = self._get_client(path)
        if not client:
            return GitstatusWorkingSummary.empty()
        return client.get_cached_working_status()

    def is_running(self, path: Path) -> bool:
        client = self._get_client(path)
        return bool(client and client.is_running)

    async def start(self) -> None:
        if not (self._iter_client_paths and self._ensure_watcher_for_path):
            return
        for p in list(self._iter_client_paths()):
            if not self._get_client(p):
                continue
            await self._ensure_watcher_for_path(p)

    async def stop(self) -> None:
        if not (self._list_watchers and self._clear_watchers):
            return
        for w in list(self._list_watchers()):
            with contextlib.suppress(Exception):
                await w.stop()
        self._clear_watchers()


class PRServiceProvider:
    def __init__(self, services: dict[WorktreeID, PRService]) -> None:
        self._services = services
        self._tasks: WeakSet[asyncio.Task] = WeakSet()
        self._inflight: set[tuple[WorktreeID, str]] = set()
        self._recent: dict[tuple[WorktreeID, str], float] = {}
        self._recent_ttl_s: float = 3.0

    async def start(self) -> None:
        for svc in self._services.values():
            with contextlib.suppress(Exception):
                await svc.start()

    async def stop(self) -> None:
        for svc in self._services.values():
            with contextlib.suppress(Exception):
                await svc.stop()

    def get_pr_info_cached(self, wtid: WorktreeID) -> PRInfo:
        prsvc = self._services.get(wtid)
        if not prsvc or prsvc.cached is None:
            return PRInfoDisabled()
        cached = prsvc.cached
        # Map runtime cache variants to wire variants
        if isinstance(cached, PRCacheOk):
            return PRInfoOk(pr_data=cached.data)
        if isinstance(cached, PRCacheError):
            return PRInfoError(error=cached.error)
        return PRInfoDisabled()

    def schedule_pr_refresh(self, wtid: WorktreeID, branch: str) -> None:
        prsvc = self._services.get(wtid)
        if not prsvc:
            return
        key = (wtid, branch)
        now = time.monotonic()
        # Skip if already running or completed very recently
        if key in self._inflight or (now - self._recent.get(key, 0.0)) < self._recent_ttl_s:
            return

        async def _run() -> None:
            try:
                await prsvc.get_pr_info(branch, force_refresh=True)
            finally:
                self._inflight.discard(key)
                # Record completion time and drop stale entries occasionally
                self._recent[key] = time.monotonic()
                if len(self._recent) > 1024:
                    cutoff = time.monotonic() - self._recent_ttl_s
                    self._recent = {k: t for k, t in self._recent.items() if t >= cutoff}

        self._inflight.add(key)
        task = asyncio.create_task(_run())
        self._tasks.add(task)

    async def refresh_now(self, wtid: WorktreeID) -> None:
        """Synchronously refresh PR cache for a given worktree.

        Uses the worktree's current branch as determined by its repository.
        If the worktree directory was manually deleted or is no longer a git repo,
        skip refresh gracefully.
        """
        prsvc = self._services.get(wtid)
        if not prsvc:
            return
        try:
            repo = prsvc.git_manager.get_repo(prsvc.worktree_info.path)
        except (pygit2.GitError, OSError, ValueError, FileNotFoundError):
            return
        branch_name = repo.head.shorthand or ""
        await prsvc.get_pr_info(branch_name, force_refresh=True)

    def has(self, wtid: WorktreeID) -> bool:
        return wtid in self._services

    def values(self) -> list[PRService]:
        return list(self._services.values())


class StatusService:
    def __init__(self, repo_status: RepoStatus) -> None:
        # Squash trivial wrapper by exposing underlying method directly
        self._status = repo_status
        self.summarize_status = repo_status.summarize_status  # type: ignore[assignment]


class DiscoveryService:
    def __init__(
        self,
        is_scanning: Callable[[], bool],
        periodic: Callable[[], Awaitable[object]] | None = None,
        cancel_periodic: Callable[[], None] | None = None,
    ) -> None:
        self._is_scanning = is_scanning
        self._periodic = periodic
        self._cancel = cancel_periodic
        # Squash trivial wrapper: expose provided callable directly
        self.is_scanning = is_scanning  # type: ignore[assignment]

    async def start(self) -> None:
        if self._periodic:
            await self._periodic()

    async def stop(self) -> None:
        if self._cancel:
            self._cancel()


async def scan_worktrees(worktrees_dir: Path) -> set[DiscoveredWorktree]:
    """Discover worktrees under a directory.

    Keeps a simple rule: include direct subdirectories that contain a `.git/`.
    Returns a set of DiscoveredWorktree with ids derived from directory names.
    """
    if not worktrees_dir.exists():
        return set()
    current: set[DiscoveredWorktree] = set()
    for path in worktrees_dir.iterdir():
        if not path.is_dir():
            continue
        if (path / ".git").exists():
            current.add(DiscoveredWorktree(path, path.name, make_worktree_id(path.name)))
    return current


class HealthService:
    def __init__(self, get_health: Callable[[], DaemonHealth]) -> None:
        self._get = get_health

    def health(self) -> DaemonHealth:
        return self._get()


class WorktreeCoordinator:
    def __init__(
        self,
        register_fn: Callable[[DiscoveredWorktree], Awaitable[object]],
        unregister_fn: Callable[[DiscoveredWorktree], Awaitable[object]],
    ) -> None:
        self._register = register_fn
        self._unregister = unregister_fn

    async def register_worktree(self, wt: DiscoveredWorktree) -> None:
        await self._register(wt)

    async def unregister_worktree(self, wt: DiscoveredWorktree) -> None:
        await self._unregister(wt)
