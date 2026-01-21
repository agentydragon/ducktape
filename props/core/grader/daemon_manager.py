"""Daemon manager for snapshot grader daemons.

Manages lifecycle of all snapshot grader daemons:
- Auto-starts daemons for all snapshots on startup
- Tracks active daemons

The daemon runs eternally inside its container, handling context exhaustion
internally. Host-side we just need to manage the container lifecycle.

TODO: Handle new snapshots added after startup (pg_notify on snapshot insert)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from props.core.db.models import Snapshot
from props.core.db.session import get_session
from props.core.ids import SnapshotSlug

if TYPE_CHECKING:
    from props.core.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)

# Default timeout for daemon containers (very long since daemons are eternal)
DEFAULT_DAEMON_TIMEOUT_SECONDS = 86400  # 24 hours


class DaemonManager:
    """Manages snapshot grader daemons.

    Each snapshot gets one daemon running in a container. Daemons are eternal -
    they sleep when no drift and wake on pg_notify. Context exhaustion is
    handled inside the container via transcript summarization.
    """

    def __init__(self, registry: AgentRegistry, model: str, timeout_seconds: int = DEFAULT_DAEMON_TIMEOUT_SECONDS):
        self._registry = registry
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._tasks: dict[SnapshotSlug, asyncio.Task[Any]] = {}
        self._shutdown = False

    async def start_all(self) -> None:
        """Start daemons for all snapshots."""
        with get_session() as session:
            snapshots = session.query(Snapshot.slug).all()
            snapshot_slugs = [s.slug for s in snapshots]

        logger.info(f"Starting grader daemons for {len(snapshot_slugs)} snapshots")

        for slug in snapshot_slugs:
            self._tasks[slug] = asyncio.create_task(self._run_daemon(slug), name=f"grader-daemon-{slug}")

    async def _run_daemon(self, snapshot_slug: SnapshotSlug) -> None:
        """Run daemon for a snapshot."""
        try:
            logger.info(f"Starting grader daemon for {snapshot_slug}")
            await self._registry.run_snapshot_grader(
                snapshot_slug=snapshot_slug, model=self._model, timeout_seconds=self._timeout_seconds
            )
            logger.info(f"Grader daemon for {snapshot_slug} exited")
        except asyncio.CancelledError:
            logger.info(f"Grader daemon for {snapshot_slug} cancelled")
            raise
        except Exception as e:
            logger.error(f"Grader daemon for {snapshot_slug} failed: {e}", exc_info=True)

    async def shutdown(self) -> None:
        """Signal all daemons to shutdown and wait for completion."""
        self._shutdown = True
        logger.info("Shutting down grader daemons...")

        # Cancel all tasks
        for task in self._tasks.values():
            if not task.done():
                task.cancel()

        # Wait for all to complete
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

        logger.info("All grader daemons stopped")
