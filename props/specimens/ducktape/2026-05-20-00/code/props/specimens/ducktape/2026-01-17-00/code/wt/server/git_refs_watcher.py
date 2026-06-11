"""Watcher for git refs to cache ahead/behind computations.

Watches config.main_repo/.git/ for changes and recomputes ahead/behind
for all active branches. All worktrees share the same underlying .git,
so a single watcher suffices.

# TODO: Currently watches all .git changes. Could be refined to only watch
# refs/heads/ and refs/remotes/ since that's what affects ahead/behind.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pygit2
from reaktiv import Signal
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from ..shared.protocol import BranchAheadBehind

if TYPE_CHECKING:
    from ..shared.configuration import Configuration
    from .git_manager import GitManager
    from .stores import DaemonStore

logger = logging.getLogger(__name__)


class GitRefsWatcher:
    """Watches main repo's .git for changes, caches ahead/behind per branch.

    Uses reactive pattern:
    - Reads from store.active_branches to know which branches to compute for
    - Writes to ahead_behind_cache signal (keyed by branch name)
    - status_handler reads from ahead_behind_cache
    """

    def __init__(
        self, store: DaemonStore, git_manager: GitManager, config: Configuration, debounce_delay: float = 0.3
    ) -> None:
        self._store = store
        self._git_manager = git_manager
        self._config = config
        self._debounce_delay = debounce_delay

        # Output: cached ahead/behind per branch name
        # Branches that fail to compute are omitted from the dict (consumer handles missing keys)
        self.ahead_behind_cache: Signal[dict[str, BranchAheadBehind]] = Signal({})

        self._observer: BaseObserver | None = None
        self._pending: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False

    async def start(self) -> None:
        """Start watching the main repo's .git directory."""
        if self._running:
            return

        self._loop = asyncio.get_running_loop()
        self._running = True

        git_dir = self._config.main_repo / ".git"
        if not git_dir.exists():
            logger.warning("Main repo .git not found: %s", git_dir)
            return

        self._observer = Observer()
        handler = _GitRefsHandler(self)
        self._observer.schedule(handler, str(git_dir), recursive=True)
        self._observer.start()
        logger.info("Watching main repo .git for refs changes: %s", git_dir)

        # Initial computation
        await self._compute_ahead_behind()

    async def stop(self) -> None:
        """Stop watching and cleanup."""
        self._running = False
        if self._observer:
            self._observer.stop()
            await asyncio.to_thread(self._observer.join)
            self._observer = None
        if self._pending:
            self._pending.cancel()
            self._pending = None
        self._loop = None

    def trigger(self, reason: str) -> None:
        """Schedule a debounced recomputation, thread-safe."""
        if not self._loop:
            return

        def _schedule() -> None:
            if self._pending:
                self._pending.cancel()
            self._pending = asyncio.create_task(self._debounced(reason))

        self._loop.call_soon_threadsafe(_schedule)

    async def _debounced(self, reason: str) -> None:
        """Wait for debounce delay then recompute."""
        try:
            await asyncio.sleep(self._debounce_delay)
            await self._compute_ahead_behind()
        except asyncio.CancelledError:
            pass

    async def _compute_ahead_behind(self) -> None:
        """Compute ahead/behind for all active branches."""
        branches = self._store.active_branches()
        result: dict[str, BranchAheadBehind] = {}

        for branch in branches:
            try:
                ahead, behind = self._compute_for_branch(branch)
                result[branch] = BranchAheadBehind(ahead=ahead, behind=behind)
            except (pygit2.GitError, KeyError, ValueError, OSError) as e:
                # Branch that fails to compute is omitted - consumer handles missing keys
                logger.warning("Failed to compute ahead/behind for branch %s: %s", branch, e)

        self.ahead_behind_cache.set(result)

    def _compute_for_branch(self, branch: str) -> tuple[int, int]:
        """Compute ahead/behind for a branch vs config.upstream_branch.

        Returns (0, 0) if:
        - Branch is the upstream branch itself
        - Any ref lookup fails
        """
        if branch == self._config.upstream_branch:
            return (0, 0)

        main_repo = self._git_manager.get_repo(self._config.main_repo)

        # Resolve local branch tip
        local_branch = main_repo.branches.local[branch]
        local_id = local_branch.target

        # Resolve upstream branch tip
        upstream_branch = main_repo.branches.local[self._config.upstream_branch]
        upstream_id = upstream_branch.target

        ahead, behind = main_repo.ahead_behind(local_id, upstream_id)
        return (ahead, behind)


class _GitRefsHandler(FileSystemEventHandler):
    """Watchdog handler that triggers GitRefsWatcher on file changes."""

    def __init__(self, parent: GitRefsWatcher) -> None:
        self._parent = parent

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._parent.trigger("modified")

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._parent.trigger("created")
