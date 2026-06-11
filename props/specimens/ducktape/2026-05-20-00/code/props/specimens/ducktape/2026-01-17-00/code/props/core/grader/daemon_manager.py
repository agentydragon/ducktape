"""Daemon manager for snapshot grader daemons.

Manages lifecycle of all snapshot grader daemons:
- Auto-starts daemons for all snapshots on startup
- Restarts on context exhaustion with fresh agent
- Tracks active daemons

TODO: Resume existing IN_PROGRESS runs with transcript from DB on startup
      (currently starts fresh, which is correct since grading_edges is checkpoint)

TODO: Handle new snapshots added after startup (pg_notify on snapshot insert)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from props.core.db.models import AgentRun, AgentRunStatus, Snapshot
from props.core.db.session import get_session
from props.core.ids import SnapshotSlug

if TYPE_CHECKING:
    from openai_utils.model import OpenAIModelProto
    from props.core.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)


class DaemonManager:
    """Manages snapshot grader daemons with restart-on-context-exhaustion.

    Each snapshot gets one daemon. Daemons sleep when no drift and wake on
    pg_notify. If a daemon hits context limit, it's restarted with fresh context.
    """

    def __init__(self, registry: AgentRegistry, client: OpenAIModelProto):
        self._registry = registry
        self._client = client
        self._tasks: dict[SnapshotSlug, asyncio.Task] = {}
        self._shutdown = False

    async def start_all(self) -> None:
        """Start daemons for all snapshots."""
        with get_session() as session:
            snapshots = session.query(Snapshot.slug).all()
            snapshot_slugs = [s.slug for s in snapshots]

        logger.info(f"Starting grader daemons for {len(snapshot_slugs)} snapshots")

        for slug in snapshot_slugs:
            self._tasks[slug] = asyncio.create_task(self._run_daemon_with_restart(slug), name=f"grader-daemon-{slug}")

    async def _run_daemon_with_restart(self, snapshot_slug: SnapshotSlug) -> None:
        """Run daemon for a snapshot, restarting on context exhaustion."""
        restart_count = 0
        max_restarts = 10  # Safety limit

        while not self._shutdown and restart_count < max_restarts:
            try:
                run_id = await self._registry.run_snapshot_grader(snapshot_slug=snapshot_slug, client=self._client)

                # Check exit status
                with get_session() as session:
                    run = session.get(AgentRun, run_id)
                    if run and run.status == AgentRunStatus.CONTEXT_LENGTH_EXCEEDED:
                        restart_count += 1
                        logger.info(
                            f"Daemon for {snapshot_slug} hit context limit, restarting ({restart_count}/{max_restarts})"
                        )
                        continue  # Restart with fresh context

                    # Normal exit or other status - don't restart
                    logger.info(f"Daemon for {snapshot_slug} exited with status: {run.status if run else 'unknown'}")
                    break

            except Exception as e:
                logger.error(f"Daemon for {snapshot_slug} failed unexpectedly: {e}", exc_info=True)
                # Don't restart on unexpected errors
                break

        if restart_count >= max_restarts:
            logger.error(f"Daemon for {snapshot_slug} hit max restarts ({max_restarts})")

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
