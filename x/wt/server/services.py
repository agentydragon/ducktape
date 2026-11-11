from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable

from x.wt.server.types import DiscoveredWorktree
from x.wt.server.worktree_ids import make_worktree_id


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
