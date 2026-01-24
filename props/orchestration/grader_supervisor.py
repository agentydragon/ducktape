"""Grader supervisor - manages snapshot grader daemon lifecycle.

Supervises all snapshot grader daemons:
- Listens for pg_notify on snapshot_created channel
- Spawns daemons for existing snapshots on startup
- Spawns new daemons when snapshots are created

Each daemon runs eternally inside its container, handling context exhaustion
internally. Host-side we supervise container lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from props.core.ids import SnapshotSlug
from props.db.models import Snapshot
from props.db.session import get_session
from props.grader.notifications import SNAPSHOT_CREATED_CHANNEL, SnapshotCreatedNotification

if TYPE_CHECKING:
    from props.orchestration.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)

# Type alias for async connection factory (type statement is lazily evaluated)
type ConnectionFactory = Callable[[], Awaitable[asyncpg.Connection[Any]]]


class GraderSupervisor:
    """Manages snapshot grader daemons.

    Each snapshot gets one daemon running in a container. Daemons are eternal -
    they sleep when no drift and wake on pg_notify. Context exhaustion is
    handled inside the container via transcript summarization.

    Use as async context manager for proper lifecycle:

        async with GraderSupervisor(...) as dm:
            # dm is now listening and has started daemons for existing snapshots
            await some_long_running_task()
        # dm.shutdown() called automatically
    """

    def __init__(self, registry: AgentRegistry, connect: ConnectionFactory, model: str):
        self._registry = registry
        self._connect = connect
        self._model = model
        self._tasks: dict[SnapshotSlug, asyncio.Task[Any]] = {}
        self._listener_conn: asyncpg.Connection[Any] | None = None
        self._shutdown = False

    async def __aenter__(self) -> GraderSupervisor:
        """Start listening and spawn daemons for existing snapshots."""
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        """Shutdown all daemons and stop listening."""
        await self.shutdown()

    def _notification_callback(
        self, connection: asyncpg.Connection[Any] | PoolConnectionProxy[Any], pid: int, channel: str, payload: object
    ) -> None:
        """Handle incoming pg_notify notifications for snapshot creation."""
        if self._shutdown:
            return

        if not isinstance(payload, str):
            logger.error(f"pg_notify payload is not a string: {type(payload)}")
            return

        notification = SnapshotCreatedNotification.model_validate_json(payload)

        slug = notification.snapshot_slug
        if slug in self._tasks and not self._tasks[slug].done():
            logger.debug(f"Daemon for {slug} already running, ignoring notification")
            return

        logger.info(f"Snapshot created: {slug}, spawning daemon")
        self._tasks[slug] = asyncio.create_task(self._run_daemon(slug), name=f"grader-daemon-{slug}")

    async def start(self) -> None:
        """Start listening for notifications and spawn daemons for existing snapshots.

        Requires database schema to exist. Will raise if schema is missing.
        """
        # Start listener first so we don't miss any snapshots created during startup
        await self._start_listener()

        # Start daemons for existing snapshots
        with get_session() as session:
            snapshots = session.query(Snapshot.slug).all()
            snapshot_slugs = [s.slug for s in snapshots]

        if snapshot_slugs:
            logger.info(f"Starting grader daemons for {len(snapshot_slugs)} existing snapshots")
            for slug in snapshot_slugs:
                self._tasks[slug] = asyncio.create_task(self._run_daemon(slug), name=f"grader-daemon-{slug}")
        else:
            logger.info("No existing snapshots, listening for new ones via pg_notify")

    async def _start_listener(self) -> None:
        """Start listening for snapshot_created notifications."""
        self._listener_conn = await self._connect()
        await self._listener_conn.add_listener(SNAPSHOT_CREATED_CHANNEL, self._notification_callback)
        logger.info(f"Listening on channel '{SNAPSHOT_CREATED_CHANNEL}' for new snapshots")

    async def _stop_listener(self) -> None:
        """Stop the notification listener."""
        if self._listener_conn:
            try:
                await self._listener_conn.remove_listener(SNAPSHOT_CREATED_CHANNEL, self._notification_callback)
                await self._listener_conn.close()
            except Exception as e:
                logger.warning(f"Error closing listener connection: {e}")
            self._listener_conn = None

    async def _run_daemon(self, snapshot_slug: SnapshotSlug) -> None:
        """Run daemon for a snapshot. Daemons run indefinitely."""
        try:
            logger.info(f"Starting grader daemon for {snapshot_slug}")
            await self._registry.run_snapshot_grader(snapshot_slug=snapshot_slug, model=self._model)
            logger.info(f"Grader daemon for {snapshot_slug} exited")
        except asyncio.CancelledError:
            logger.info(f"Grader daemon for {snapshot_slug} cancelled")
            raise
        except Exception:
            logger.exception(f"Grader daemon for {snapshot_slug} failed")

    async def shutdown(self) -> None:
        """Signal all daemons to shutdown and wait for completion."""
        self._shutdown = True
        logger.info("Shutting down grader daemons...")

        # Stop listener first
        await self._stop_listener()

        # Cancel all tasks
        for task in self._tasks.values():
            if not task.done():
                task.cancel()

        # Wait for all to complete
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

        logger.info("All grader daemons stopped")
