"""Grader supervisor - manages per-snapshot grader container lifecycle.

Supervises one grader container per snapshot:
- Listens for pg_notify on snapshot_created to spawn new graders
- Listens for pg_notify on grader_definition_changed to restart all graders
  when the grader image tag moves (e.g. new image pushed)
- Tracks running containers via GraderHandle, kills them directly on shutdown/restart

Startup sequence:
1. Lifespan calls start() to set up pg_notify listeners
2. After uvicorn binds, caller invokes spawn_existing() to start graders
   for all existing snapshots (avoids chicken-and-egg with registry proxy)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from types import TracebackType
from typing import TYPE_CHECKING, Any

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from props.core.agent_types import AgentType
from props.core.ids import SnapshotSlug
from props.core.oci_utils import BUILTIN_TAG
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.models import Snapshot
from props.db.notifications import (
    GRADER_DEFINITION_CHANGED_CHANNEL,
    SNAPSHOT_CREATED_CHANNEL,
    GraderDefinitionChangedNotification,
    SnapshotCreatedNotification,
)
from props.orchestration.agent_registry import ImageResolutionError, ResolvedImage

if TYPE_CHECKING:
    from props.orchestration.agent_registry import AgentRegistry, GraderHandle

logger = logging.getLogger(__name__)


class GraderSupervisor:
    """Manages per-snapshot grader containers.

    Each snapshot gets one long-lived grader container. Graders sleep when
    no drift and wake on pg_notify. Context exhaustion is handled inside
    the container via transcript summarization.

    Use as async context manager for proper lifecycle:

        async with GraderSupervisor(...) as gs:
            await gs.spawn_existing()
            await some_long_running_task()
        # gs.shutdown() called automatically
    """

    def __init__(self, registry: AgentRegistry, db_config: DatabaseConfig, model: str, db: Database):
        self._registry = registry
        self._db_config = db_config
        self._model = model
        self._db = db
        self._handles: dict[SnapshotSlug, GraderHandle] = {}
        self._listener_conn: asyncpg.Connection[Any] | None = None
        self._shutdown = False
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> GraderSupervisor:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        await self.shutdown()

    def _launch_background(self, coro: Coroutine[Any, Any, None], *, name: str) -> None:
        """Launch a background task and prevent garbage collection."""
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _snapshot_created_callback(
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
        if slug in self._handles:
            logger.debug(f"Grader for {slug} already running, ignoring notification")
            return

        logger.info(f"Snapshot created: {slug}, spawning grader")
        self._launch_background(self._resolve_and_spawn_grader(slug), name=f"grader-spawn-{slug}")

    def _grader_definition_changed_callback(
        self, connection: asyncpg.Connection[Any] | PoolConnectionProxy[Any], pid: int, channel: str, payload: object
    ) -> None:
        """Handle grader tag push — restart all graders to pick up the new image."""
        if self._shutdown:
            return

        if not isinstance(payload, str):
            logger.error(f"pg_notify payload is not a string: {type(payload)}")
            return

        notification = GraderDefinitionChangedNotification.model_validate_json(payload)
        logger.info(f"Grader definition changed: {notification.tag} -> {notification.digest}")

        slugs = list(self._handles.keys())
        self._launch_background(self._restart_all(slugs), name="grader-restart-all")

    async def start(self) -> None:
        """Start listening for notifications.

        Sets up pg_notify listeners immediately (during lifespan). Container
        spawning is deferred until spawn_existing() is called after the HTTP
        server is ready.
        """
        await self._start_listener()

    async def spawn_existing(self) -> None:
        """Spawn graders for all existing snapshots.

        Call after the HTTP server is ready (uvicorn has bound its socket),
        so that containers can resolve images through the registry proxy.
        """
        if self._shutdown:
            return

        try:
            resolved = await self._registry.resolve_image(AgentType.GRADER, BUILTIN_TAG)
        except ImageResolutionError:
            logger.warning("Grader image not available in registry — grader spawning disabled until image is pushed")
            return

        with self._db.session() as session:
            snapshots = session.query(Snapshot.slug).all()
            snapshot_slugs = [s.slug for s in snapshots]

        if snapshot_slugs:
            logger.info(f"Starting graders for {len(snapshot_slugs)} existing snapshots")
            for slug in snapshot_slugs:
                await self._spawn_grader(slug, image=resolved)
        else:
            logger.info("No existing snapshots, listening for new ones via pg_notify")

    async def _start_listener(self) -> None:
        """Start listening for snapshot_created and grader_definition_changed notifications."""
        self._listener_conn = await self._db_config.asyncpg_connect()
        await self._listener_conn.add_listener(SNAPSHOT_CREATED_CHANNEL, self._snapshot_created_callback)
        await self._listener_conn.add_listener(
            GRADER_DEFINITION_CHANGED_CHANNEL, self._grader_definition_changed_callback
        )
        logger.info(f"Listening on channels '{SNAPSHOT_CREATED_CHANNEL}', '{GRADER_DEFINITION_CHANGED_CHANNEL}'")

    async def _stop_listener(self) -> None:
        """Stop the notification listeners."""
        if self._listener_conn:
            try:
                await self._listener_conn.remove_listener(SNAPSHOT_CREATED_CHANNEL, self._snapshot_created_callback)
                await self._listener_conn.remove_listener(
                    GRADER_DEFINITION_CHANGED_CHANNEL, self._grader_definition_changed_callback
                )
                await self._listener_conn.close()
            except Exception as e:
                logger.warning(f"Error closing listener connection: {e}")
            self._listener_conn = None

    async def _resolve_and_spawn_grader(self, snapshot_slug: SnapshotSlug) -> None:
        """Resolve grader image and spawn a grader. Used for notification-triggered single spawns."""
        try:
            resolved = await self._registry.resolve_image(AgentType.GRADER, BUILTIN_TAG)
        except ImageResolutionError:
            logger.warning(f"Grader image not available for {snapshot_slug}")
            return
        await self._spawn_grader(snapshot_slug, image=resolved)

    async def _spawn_grader(self, snapshot_slug: SnapshotSlug, *, image: ResolvedImage) -> None:
        """Start a grader container for a snapshot with a pre-resolved image."""
        try:
            logger.info(f"Starting grader for {snapshot_slug}")
            handle = await self._registry.start_snapshot_grader(
                image=image, snapshot_slug=snapshot_slug, model=self._model
            )
            self._handles[snapshot_slug] = handle
            logger.info(f"Grader container {handle.container_name} running for {snapshot_slug}")
        except Exception:
            logger.exception(f"Failed to start grader for {snapshot_slug}")

    async def _kill_grader(self, snapshot_slug: SnapshotSlug) -> None:
        """Kill and remove a grader container."""
        handle = self._handles.pop(snapshot_slug, None)
        if handle:
            await handle.kill()

    async def _restart_all(self, slugs: list[SnapshotSlug]) -> None:
        """Kill all graders and restart them (e.g. after image update)."""
        for slug in slugs:
            await self._kill_grader(slug)
        try:
            resolved = await self._registry.resolve_image(AgentType.GRADER, BUILTIN_TAG)
        except ImageResolutionError:
            logger.warning("Grader image not available — skipping restart")
            return
        for slug in slugs:
            await self._spawn_grader(slug, image=resolved)
        logger.info(f"Restarted {len(slugs)} graders after definition change")

    async def shutdown(self) -> None:
        """Kill all grader containers and stop listeners."""
        self._shutdown = True
        logger.info("Shutting down graders...")

        await self._stop_listener()

        for slug in list(self._handles.keys()):
            await self._kill_grader(slug)

        logger.info("All graders stopped")
