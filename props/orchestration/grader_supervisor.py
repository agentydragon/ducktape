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

Event-driven triggers (snapshot_created, grader_definition_changed) are
debounced: a burst coalesces into a single trailing-edge reconcile, so rapid
image pushes delay-and-collapse into one respawn instead of many restart
storms. Startup reconciles immediately.
"""

from __future__ import annotations

import asyncio
import logging
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
from props.orchestration.agent_registry import AgentRunHandle, ImageResolutionError, ResolvedImage

if TYPE_CHECKING:
    from props.orchestration.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)

# Coalesce reconcile triggers arriving within this window into a single
# trailing-edge reconcile (delay-and-collapse, never skip).
DEFAULT_RECONCILE_DEBOUNCE_S = 30.0


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

    def __init__(
        self,
        registry: AgentRegistry,
        db_config: DatabaseConfig,
        model: str,
        db: Database,
        *,
        reconcile_debounce_s: float = DEFAULT_RECONCILE_DEBOUNCE_S,
    ):
        self._registry = registry
        self._db_config = db_config
        self._model = model
        self._db = db
        self._handles: dict[SnapshotSlug, AgentRunHandle] = {}
        self._listener_conn: asyncpg.Connection[Any] | None = None
        self._shutdown = False
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Trailing-edge debounce of event-driven reconciles.
        self._reconcile_debounce_s = reconcile_debounce_s
        self._debounce_task: asyncio.Task[None] | None = None
        self._pending_restart_existing = False
        self._pending_triggers: list[str] = []

    async def __aenter__(self) -> GraderSupervisor:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        await self.shutdown()

    # --- Event handlers (debounced — coalesce bursts into one reconcile) ---

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
        self._schedule_reconcile(trigger=f"snapshot_created:{notification.snapshot_slug}")

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
        self._schedule_reconcile(trigger=f"grader_definition_changed:{notification.tag}", restart_existing=True)

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start pg_notify listeners. Call spawn_existing() separately after HTTP is ready."""
        await self._start_listener()

    async def spawn_existing(self) -> None:
        """Initial reconciliation after HTTP server is ready."""
        await self.reconcile(trigger="startup")

    # --- Debounced reconcile scheduling ---

    def _schedule_reconcile(self, *, trigger: str, restart_existing: bool = False) -> None:
        """Debounce an event-driven reconcile: coalesce rapid triggers into one
        trailing-edge run.

        A burst of events (several grader image pushes, a batch of new
        snapshots) collapses into a single reconcile that fires after
        ``reconcile_debounce_s`` of quiet. The respawn is *delayed and
        coalesced*, never skipped — ``restart_existing`` is OR'd across the
        window so a genuine image change in the burst still restarts graders,
        exactly once.
        """
        if self._shutdown:
            return
        self._pending_restart_existing |= restart_existing
        self._pending_triggers.append(trigger)
        # Reset the quiet window so we fire once, after the LAST trigger. Only a
        # still-debouncing timer is cancelled — never an in-flight reconcile.
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        task = asyncio.create_task(self._run_debounced_reconcile(), name="grader-debounced-reconcile")
        self._debounce_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_debounced_reconcile(self) -> None:
        try:
            await asyncio.sleep(self._reconcile_debounce_s)
        except asyncio.CancelledError:
            # Superseded by a newer trigger (window reset) or shutdown — the
            # replacement timer, if any, will fire. Don't reconcile here.
            return
        # Window elapsed. Clear the timer handle first so a trigger arriving
        # during the (awaited) reconcile opens a fresh window instead of
        # cancelling this run.
        self._debounce_task = None
        if self._shutdown:
            return
        restart_existing = self._pending_restart_existing
        triggers = self._pending_triggers
        self._pending_restart_existing = False
        self._pending_triggers = []
        label = triggers[0] if len(triggers) == 1 else f"debounced({len(triggers)}): {', '.join(triggers)}"
        await self.reconcile(trigger=label, restart_existing=restart_existing)

    # --- Core reconciliation ---

    async def reconcile(self, *, trigger: str, restart_existing: bool = False) -> None:
        """Converge actual state toward desired state.

        Desired: one grader per snapshot (if image available).
        Actual: self._handles.

        `trigger` labels what caused this reconcile (startup, snapshot_created,
        grader_definition_changed, ...) — logged so grader churn is attributable.
        If restart_existing is True, kill all tracked graders first (used when the
        grader image changes); note this cancels graders mid-run.
        """
        if self._shutdown:
            return

        logger.info("Reconcile triggered by %s (restart_existing=%s)", trigger, restart_existing)

        # Resolve image — if unavailable, nothing to do.
        try:
            resolved = await self._registry.resolve_image(AgentType.GRADER, BUILTIN_TAG)
        except ImageResolutionError:
            logger.warning("Reconcile [%s] aborted: grader image not available", trigger)
            return

        # Get desired set from DB.
        with self._db.session() as session:
            desired: set[SnapshotSlug] = {s.slug for s in session.query(Snapshot.slug).all()}

        killed = 0
        # If the image changed, kill everything so containers pick up the new image.
        # This cancels any in-flight grade (-> AgentRunStatus.CANCELLED).
        if restart_existing:
            for slug in list(self._handles.keys()):
                await self._kill_grader(slug, reason="grader_image_changed")
                killed += 1

        # Kill graders for snapshots that no longer exist.
        for slug in list(self._handles.keys()):
            if slug not in desired:
                await self._kill_grader(slug, reason="snapshot_removed")
                killed += 1

        # Spawn missing graders.
        spawned = 0
        for slug in desired:
            if slug not in self._handles:
                await self._spawn_grader(slug, image=resolved, trigger=trigger)
                spawned += 1

        logger.info(
            "Reconcile [%s] done: desired=%d killed=%d spawned=%d running=%d",
            trigger,
            len(desired),
            killed,
            spawned,
            len(self._handles),
        )

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

    async def _spawn_grader(self, snapshot_slug: SnapshotSlug, *, image: ResolvedImage, trigger: str) -> None:
        try:
            handle = await self._registry.start_snapshot_grader(
                image=image, snapshot_slug=snapshot_slug, model=self._model
            )
            self._handles[snapshot_slug] = handle
            logger.info(
                "Spawned grader %s for %s (trigger: %s, image: %s)", handle.name, snapshot_slug, trigger, image.digest
            )
        except Exception:
            logger.exception("Failed to start grader for %s (trigger: %s)", snapshot_slug, trigger)

    async def _kill_grader(self, snapshot_slug: SnapshotSlug, *, reason: str) -> None:
        handle = self._handles.pop(snapshot_slug, None)
        if handle:
            logger.info("Killing grader %s for %s (reason: %s)", handle.name, snapshot_slug, reason)
            await handle.kill_and_delete()

    async def shutdown(self) -> None:
        """Kill all grader containers and stop listeners."""
        self._shutdown = True
        logger.info("Shutting down graders...")
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        await self._stop_listener()
        for slug in list(self._handles.keys()):
            await self._kill_grader(slug, reason="shutdown")
        logger.info("All graders stopped")
