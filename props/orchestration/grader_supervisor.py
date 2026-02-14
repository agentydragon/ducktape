"""Grader supervisor — reconciliation-based per-snapshot grader lifecycle.

Maintains the invariant: every snapshot has exactly one running grader
container, provided a grader image is available in the registry.

Reconciliation is triggered by:
- startup (spawn_existing)
- pg_notify snapshot_created
- pg_notify grader_definition_changed (image push)

Each trigger calls reconcile(), which compares desired state (all snapshot
slugs from DB) against actual state (self._handles) and creates/kills
containers to converge.
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
from props.orchestration.executor import ContainerHandle

if TYPE_CHECKING:
    from props.orchestration.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)


class GraderSupervisor:
    """Manages per-snapshot grader containers via reconciliation.

    Each snapshot gets one long-lived grader container. The supervisor
    maintains a dict of handles and reconciles against the DB snapshot
    list whenever an event fires (new snapshot, new image, startup).

    Use as async context manager:

        async with GraderSupervisor(...) as gs:
            await gs.spawn_existing()
            ...
        # gs.shutdown() called automatically
    """

    def __init__(self, registry: AgentRegistry, db_config: DatabaseConfig, model: str, db: Database):
        self._registry = registry
        self._db_config = db_config
        self._model = model
        self._db = db
        self._handles: dict[SnapshotSlug, ContainerHandle] = {}
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

    # --- Event handlers (thin wrappers that trigger reconcile) ---

    def _snapshot_created_callback(
        self, connection: asyncpg.Connection[Any] | PoolConnectionProxy[Any], pid: int, channel: str, payload: object
    ) -> None:
        if self._shutdown:
            return
        if not isinstance(payload, str):
            logger.error(f"pg_notify payload is not a string: {type(payload)}")
            return
        notification = SnapshotCreatedNotification.model_validate_json(payload)
        logger.info(f"Snapshot created: {notification.snapshot_slug}")
        self._launch_background(self.reconcile(), name="reconcile-snapshot-created")

    def _grader_definition_changed_callback(
        self, connection: asyncpg.Connection[Any] | PoolConnectionProxy[Any], pid: int, channel: str, payload: object
    ) -> None:
        if self._shutdown:
            return
        if not isinstance(payload, str):
            logger.error(f"pg_notify payload is not a string: {type(payload)}")
            return
        notification = GraderDefinitionChangedNotification.model_validate_json(payload)
        logger.info(f"Grader definition changed: {notification.tag} -> {notification.digest}")
        self._launch_background(self.reconcile(restart_existing=True), name="reconcile-definition-changed")

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start pg_notify listeners. Call spawn_existing() separately after HTTP is ready."""
        await self._start_listener()

    async def spawn_existing(self) -> None:
        """Initial reconciliation after HTTP server is ready."""
        await self.reconcile()

    # --- Core reconciliation ---

    async def reconcile(self, *, restart_existing: bool = False) -> None:
        """Converge actual state toward desired state.

        Desired: one grader per snapshot (if image available).
        Actual: self._handles.

        If restart_existing is True, kill all tracked graders first (used
        when the grader image changes).
        """
        if self._shutdown:
            return

        # Resolve image — if unavailable, nothing to do.
        try:
            resolved = await self._registry.resolve_image(AgentType.GRADER, BUILTIN_TAG)
        except ImageResolutionError:
            logger.warning("Grader image not available — skipping reconciliation")
            return

        # Get desired set from DB.
        with self._db.session() as session:
            desired: set[SnapshotSlug] = {s.slug for s in session.query(Snapshot.slug).all()}

        # If image changed, kill everything so containers pick up the new image.
        if restart_existing:
            for slug in list(self._handles.keys()):
                await self._kill_grader(slug)

        # Kill graders for snapshots that no longer exist.
        for slug in list(self._handles.keys()):
            if slug not in desired:
                logger.info(f"Snapshot {slug} removed, killing grader")
                await self._kill_grader(slug)

        # Spawn missing graders.
        spawned = 0
        for slug in desired:
            if slug not in self._handles:
                await self._spawn_grader(slug, image=resolved)
                spawned += 1

        if spawned or restart_existing:
            logger.info(f"Reconciled: {len(self._handles)} graders running ({spawned} spawned, {len(desired)} desired)")

    # --- Internal helpers ---

    async def _start_listener(self) -> None:
        self._listener_conn = await self._db_config.asyncpg_connect()
        await self._listener_conn.add_listener(SNAPSHOT_CREATED_CHANNEL, self._snapshot_created_callback)
        await self._listener_conn.add_listener(
            GRADER_DEFINITION_CHANGED_CHANNEL, self._grader_definition_changed_callback
        )
        logger.info(f"Listening on channels '{SNAPSHOT_CREATED_CHANNEL}', '{GRADER_DEFINITION_CHANGED_CHANNEL}'")

    async def _stop_listener(self) -> None:
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

    async def _spawn_grader(self, snapshot_slug: SnapshotSlug, *, image: ResolvedImage) -> None:
        try:
            logger.info(f"Starting grader for {snapshot_slug}")
            handle = await self._registry.start_snapshot_grader(
                image=image, snapshot_slug=snapshot_slug, model=self._model
            )
            self._handles[snapshot_slug] = handle
            logger.info(f"Grader container {handle.name} running for {snapshot_slug}")
        except Exception:
            logger.exception(f"Failed to start grader for {snapshot_slug}")

    async def _kill_grader(self, snapshot_slug: SnapshotSlug) -> None:
        handle = self._handles.pop(snapshot_slug, None)
        if handle:
            await handle.kill_and_delete()

    async def shutdown(self) -> None:
        """Kill all grader containers and stop listeners."""
        self._shutdown = True
        logger.info("Shutting down graders...")
        await self._stop_listener()
        for slug in list(self._handles.keys()):
            await self._kill_grader(slug)
        logger.info("All graders stopped")
