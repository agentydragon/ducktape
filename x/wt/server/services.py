from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from x.wt.server.types import DiscoveredWorktree
from x.wt.server.worktree_ids import make_worktree_id
from x.wt.server.worktree_index import WorktreeIndex


class WorktreeIndexService:
    def __init__(
        self, *, get_index: Callable[[], WorktreeIndex | None], run_discovery_once: Callable[[], Awaitable[object]]
    ) -> None:
        self._get_index = get_index
        self._run_discovery_once = run_discovery_once

    async def ensure_discovery(self) -> None:
        await self._run_discovery_once()

    def list_paths(self) -> list[Path]:
        idx = self._get_index()
        if not idx:
            return []
        return list(idx.by_path)

    def get_by_name(self, name: str) -> DiscoveredWorktree | None:
        idx = self._get_index()
        if not idx:
            return None
        return idx.by_name.get(name)

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


async def scan_worktrees(worktrees_dir: Path) -> set[DiscoveredWorktree]:
    """Discover worktrees under a directory.

    Keeps a simple rule: include direct subdirectories that contain a `.git/`.
    Returns a set of DiscoveredWorktree with ids derived from directory names.
    """

    def _scan() -> set[DiscoveredWorktree]:
        if not worktrees_dir.exists():
            return set()
        current: set[DiscoveredWorktree] = set()
        for path in worktrees_dir.iterdir():
            if not path.is_dir():
                continue
            if (path / ".git").exists():
                current.add(DiscoveredWorktree(path, path.name, make_worktree_id(path.name)))
        return current

    return await asyncio.to_thread(_scan)


@runtime_checkable
class WorktreeCoordinator(Protocol):
    """Protocol for worktree registration/unregistration.

    Implemented by WtDaemon which provides register_worktree/unregister_worktree methods.
    """

    async def register_worktree(self, wt: DiscoveredWorktree) -> None: ...
    async def unregister_worktree(self, wt: DiscoveredWorktree) -> None: ...
