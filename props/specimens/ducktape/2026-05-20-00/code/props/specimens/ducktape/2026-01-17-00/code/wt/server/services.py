from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from ..shared.protocol import Collector, GitstatusdData
from .gitstatus_refresh import DebouncedGitstatusRefresh
from .gitstatusd_listener import GitstatusdListener
from .types import DiscoveredWorktree
from .worktree_ids import make_worktree_id
from .worktree_index import WorktreeIndex


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
        # Squash trivial wrapper: expose provided callable directly (method-to-attribute assignment)
        self.get_client = get_client

    def get_cached_status(self, path: Path) -> Collector[GitstatusdData]:
        """Get cached gitstatusd data from the listener's signal."""
        client = self._get_client(path)
        if not client:
            return Collector()
        return cast(Collector[GitstatusdData], client.status())

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
        # Squash trivial wrapper: expose provided callable directly (method-to-attribute assignment)
        self.is_scanning = is_scanning

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


@runtime_checkable
class WorktreeCoordinator(Protocol):
    """Protocol for worktree registration/unregistration.

    Implemented by WtDaemon which provides register_worktree/unregister_worktree methods.
    """

    async def register_worktree(self, wt: DiscoveredWorktree) -> None: ...
    async def unregister_worktree(self, wt: DiscoveredWorktree) -> None: ...
