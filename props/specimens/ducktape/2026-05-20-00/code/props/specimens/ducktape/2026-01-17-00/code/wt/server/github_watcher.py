"""Centralized GitHub PR watcher for all branches.

Uses reaktiv Effect for automatic dependency tracking:
- Effect subscribes to worktree_paths signal
- When paths change, effect automatically re-runs and fetches branches
- No manual trigger_refresh() needed for reactivity

Replaces per-worktree PRService with a single watcher that:
- Input: Signal[frozenset[Path]] of worktree paths + GitManager to get branches
- Output: Signal[Collector[dict[str, PRData | None]]] batch response keyed by branch
- Join from (worktree → branch) + (branch → PR) happens at API boundary in status_handler
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from reaktiv import Effect, Signal

from ..shared.env import is_test_mode
from ..shared.fixtures import load_pr_fixture
from ..shared.github_models import PRData, PRState
from ..shared.protocol import Collector

if TYPE_CHECKING:
    from ..shared.configuration import Configuration
    from .github_client import GitHubInterface

logger = logging.getLogger(__name__)


class GitHubWatcher:
    """Centralized GitHub PR watcher for all branches.

    Uses reaktiv Effect for automatic dependency tracking:
    - Effect subscribes to active_branches signal
    - When branches change, effect automatically re-runs
    - No manual trigger_refresh() needed for reactivity
    """

    def __init__(
        self,
        branches_signal: Callable[[], frozenset[str]],
        github_interface: GitHubInterface | None,
        config: Configuration,
    ) -> None:
        self._branches = branches_signal
        self._github = github_interface
        self._config = config

        # Output: batch PR data for all branches
        self.pr_cache: Signal[Collector[dict[str, PRData | None]]] = Signal(Collector())

        self._effect: Effect | None = None
        self._periodic_task: asyncio.Task[None] | None = None
        self._manual_refresh_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the watcher with reactive effect and periodic refresh."""
        # Create reactive effect - automatically tracks branches_signal dependency
        # When active_branches changes, this effect re-runs
        self._effect = Effect(self._refresh_effect)

        # Periodic refresh as separate concern (for cache freshness, not reactivity)
        self._periodic_task = asyncio.create_task(self._periodic_loop())

    async def stop(self) -> None:
        """Stop the watcher and dispose of effect."""
        if self._effect:
            self._effect.dispose()
            self._effect = None
        if self._periodic_task:
            self._periodic_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._periodic_task
            self._periodic_task = None

    async def _refresh_effect(self) -> None:
        """Reactive effect that runs when active_branches changes.

        By calling self._branches(), we subscribe to that signal.
        reaktiv tracks this dependency and re-runs the effect when it changes.
        """
        branches = self._branches()  # This subscribes to the signal!
        await self._fetch_pr_data(branches)

    async def _periodic_loop(self) -> None:
        """Periodic refresh for cache freshness (orthogonal to reactivity)."""
        while True:
            await asyncio.sleep(self._config.github_periodic_interval.total_seconds())
            branches = self._branches()
            await self._fetch_pr_data(branches)

    async def _fetch_pr_data(self, branches: frozenset[str]) -> None:
        """Fetch PR data for all given branches."""
        if not branches:
            self.pr_cache.update(lambda c: c.ok({}))
            return

        # Test mode: use fixture file
        if is_test_mode():
            result: dict[str, PRData | None] = {}
            for branch in branches:
                result[branch] = load_pr_fixture(self._config, branch)

            def _set_fixture_result(c: Collector[dict[str, PRData | None]]) -> Collector[dict[str, PRData | None]]:
                return c.ok(result)

            self.pr_cache.update(_set_fixture_result)
            return

        # No GitHub interface: nothing to fetch
        if not self._github:
            self.pr_cache.update(lambda c: c.ok({}))
            return

        try:
            result = {}
            loop = asyncio.get_running_loop()

            for branch in branches:
                prs = await loop.run_in_executor(None, self._github.pr_search, branch)
                if prs:
                    pr = prs[0]
                    result[branch] = PRData(
                        pr_number=int(pr.number),
                        pr_state=PRState(pr.state),
                        draft=bool(pr.draft),
                        mergeable=pr.mergeable,
                        merged_at=(pr.merged_at.isoformat() if pr.merged_at else None),
                        additions=pr.additions,
                        deletions=pr.deletions,
                    )
                else:
                    result[branch] = None

            def _set_fetch_result(c: Collector[dict[str, PRData | None]]) -> Collector[dict[str, PRData | None]]:
                return c.ok(result)

            self.pr_cache.update(_set_fetch_result)
        except Exception as exc:
            logger.warning("GitHub batch fetch failed: %s", exc)
            captured_exc = exc

            def _set_fetch_error(c: Collector[dict[str, PRData | None]]) -> Collector[dict[str, PRData | None]]:
                return c.exception(captured_exc)

            self.pr_cache.update(_set_fetch_error)

    def trigger_refresh(self) -> None:
        """Manual refresh trigger (for pr_refresh_now RPC)."""
        # Schedule a fetch - the effect handles reactivity automatically
        self._manual_refresh_task = asyncio.create_task(self._fetch_pr_data(self._branches()))

    async def refresh_now(self) -> None:
        """Synchronously refresh PR cache (blocking)."""
        await self._fetch_pr_data(self._branches())
