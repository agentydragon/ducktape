"""Grader supervisor — reconciliation-based per-snapshot grader lifecycle.

Maintains the invariant: every snapshot has exactly one running grader
container, provided a grader image is available in the registry.

Reconciliation is triggered by:
- startup (spawn_existing)
- pg_notify snapshot_created
- pg_notify grader_definition_changed (image push)
- a periodic backstop (catches drift between events)

Each trigger calls reconcile(), which compares desired state (all snapshot
slugs from DB) against **actual state read from the runtime** (grader pods
listed by label) and converges:

- adopt a healthy existing grader (right image, running) — so graders survive
  backend restarts instead of being orphaned and duplicated;
- reap duplicates, graders for removed snapshots, wrong-image graders, and
  terminal pods (finalizing their run record);
- spawn a grader for any snapshot that lacks a healthy one.

Because actual state comes from the API (not in-memory handles), a restarted
backend re-adopts the running graders rather than spawning a second generation.

Event-driven triggers are debounced: a burst coalesces into a single
trailing-edge reconcile. Startup reconciles immediately.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
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
from props.orchestration.agent_registry import AgentRunHandle, GraderPodInfo, ImageResolutionError, ResolvedImage
from props.orchestration.executor import PodPhase

if TYPE_CHECKING:
    from props.orchestration.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)

# Coalesce reconcile triggers arriving within this window into a single
# trailing-edge reconcile (delay-and-collapse, never skip).
DEFAULT_RECONCILE_DEBOUNCE_S = 30.0
# Backstop reconcile interval: catches drift the event triggers miss (a grader
# that crashed, a pod deleted out-of-band). 0 disables the loop.
DEFAULT_PERIODIC_RECONCILE_S = 120.0

# Crash-loop backoff: a grader that exits Failed is respawned, but only after an
# exponentially growing quiet window keyed on how many times it has failed in a
# row. Without this, a poison snapshot (e.g. an ungradeable critique) respawns a
# fresh grader every reconcile and burns tokens unbounded.
DEFAULT_BACKOFF_BASE_S = 120.0
DEFAULT_BACKOFF_MAX_S = 3600.0
# After this many consecutive failures the snapshot is quarantined: no respawn
# until an operator intervenes (or the grader definition changes, which clears
# the count). Surfaces a stuck snapshot instead of retrying forever.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 6


@dataclass
class _FailureState:
    """Per-snapshot crash-loop accounting (monotonic clock)."""

    consecutive: int
    # Earliest monotonic time at which a respawn may be attempted.
    respawn_not_before: float


class GraderSupervisor:
    """Manages per-snapshot grader containers via reconciliation against the
    Kubernetes/Docker API.

    Use as async context manager:

        async with GraderSupervisor(...) as gs:
            await gs.spawn_existing()
            ...
        # gs.shutdown() called automatically (leaves graders running)
    """

    def __init__(
        self,
        registry: AgentRegistry,
        db_config: DatabaseConfig,
        model: str,
        db: Database,
        *,
        reconcile_debounce_s: float = DEFAULT_RECONCILE_DEBOUNCE_S,
        periodic_reconcile_s: float = DEFAULT_PERIODIC_RECONCILE_S,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    ):
        self._registry = registry
        self._db_config = db_config
        self._model = model
        self._db = db
        # Lifecycle handles for the graders this process currently owns (spawned
        # or adopted). NOT the source of truth for reconcile — that's the API —
        # only a place to retain collector tasks so they aren't GC'd.
        self._handles: dict[SnapshotSlug, AgentRunHandle] = {}
        self._listener_conn: asyncpg.Connection[Any] | None = None
        self._shutdown = False
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Trailing-edge debounce of event-driven reconciles.
        self._reconcile_debounce_s = reconcile_debounce_s
        self._debounce_task: asyncio.Task[None] | None = None
        self._pending_triggers: list[str] = []
        # Periodic backstop.
        self._periodic_reconcile_s = periodic_reconcile_s
        self._periodic_task: asyncio.Task[None] | None = None
        # Crash-loop backoff state, keyed by snapshot.
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._max_consecutive_failures = max_consecutive_failures
        self._failures: dict[SnapshotSlug, _FailureState] = {}

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
        # A new grader image is a fresh attempt: clear crash-loop backoff so quarantined
        # snapshots get re-graded (the new image may fix the failure).
        self._failures.clear()
        # No restart flag: reconcile detects wrong-image graders from the API and
        # replaces them, so a tag move just schedules a normal reconcile.
        self._schedule_reconcile(trigger=f"grader_definition_changed:{notification.tag}")

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start pg_notify listeners + periodic backstop. Call spawn_existing() after HTTP is ready."""
        await self._start_listener()
        if self._periodic_reconcile_s > 0:
            self._periodic_task = asyncio.create_task(self._periodic_loop(), name="grader-periodic-reconcile")

    async def spawn_existing(self) -> None:
        """Initial reconciliation after HTTP server is ready."""
        await self.reconcile(trigger="startup")

    async def _periodic_loop(self) -> None:
        while not self._shutdown:
            try:
                await asyncio.sleep(self._periodic_reconcile_s)
            except asyncio.CancelledError:
                return
            self._schedule_reconcile(trigger="periodic")

    # --- Debounced reconcile scheduling ---

    def _schedule_reconcile(self, *, trigger: str) -> None:
        """Debounce an event-driven reconcile: coalesce rapid triggers into one
        trailing-edge run after ``reconcile_debounce_s`` of quiet."""
        if self._shutdown:
            return
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
        triggers = self._pending_triggers
        self._pending_triggers = []
        label = triggers[0] if len(triggers) == 1 else f"debounced({len(triggers)}): {', '.join(triggers)}"
        await self.reconcile(trigger=label)

    # --- Core reconciliation ---

    async def reconcile(self, *, trigger: str) -> None:
        """Converge actual state (grader pods listed from the runtime) toward
        desired state (one grader per snapshot on the current image)."""
        if self._shutdown:
            return

        logger.info("Reconcile triggered by %s", trigger)

        try:
            resolved = await self._registry.resolve_image(AgentType.GRADER, BUILTIN_TAG)
        except ImageResolutionError:
            logger.warning("Reconcile [%s] aborted: grader image not available", trigger)
            return

        with self._db.session() as session:
            desired: set[SnapshotSlug] = {s.slug for s in session.query(Snapshot.slug).all()}

        pods = await self._registry.list_grader_pods()
        by_snapshot: dict[SnapshotSlug, list[GraderPodInfo]] = defaultdict(list)
        for pod in pods:
            by_snapshot[pod.snapshot_slug].append(pod)

        now = time.monotonic()
        handles_by_pod_name = {h.name: h for h in self._handles.values()}
        new_handles: dict[SnapshotSlug, AgentRunHandle] = {}
        # Snapshots whose grader was observed Failed this cycle: never respawn them in
        # the same reconcile that saw the crash, regardless of backoff window length.
        failed_this_cycle: set[SnapshotSlug] = set()
        kept = adopted = reaped = spawned = 0

        for slug, plist in by_snapshot.items():
            # A keeper is one running pod, for a desired snapshot, on the current image.
            keeper = next(
                (
                    p
                    for p in plist
                    if slug in desired and p.phase == PodPhase.RUNNING and p.image_ref == resolved.oci_ref
                ),
                None,
            )
            for pod in plist:
                if keeper is not None and pod.name == keeper.name:
                    continue
                # Terminal pods on the current image carry the crash-loop signal: a
                # Failed exit feeds the backoff; a Succeeded exit clears it (a grader
                # that finished its run proves the snapshot is gradeable again). A
                # merely-Running pod is NOT proof of health — it may still crash — so
                # it never resets the count.
                if slug in desired and pod.image_ref == resolved.oci_ref:
                    if pod.phase == PodPhase.FAILED:
                        self._record_failure(slug, now=now)
                        failed_this_cycle.add(slug)
                    elif pod.phase == PodPhase.SUCCEEDED:
                        self._failures.pop(slug, None)
                await self._reap(pod, desired=desired, image=resolved, tracked=handles_by_pod_name)
                reaped += 1
            if keeper is not None:
                existing = handles_by_pod_name.get(keeper.name)
                if existing is not None:
                    new_handles[slug] = existing
                    kept += 1
                else:
                    new_handles[slug] = self._registry.adopt_grader_pod(keeper)
                    adopted += 1

        for slug in desired:
            if slug not in new_handles and slug not in failed_this_cycle and self._may_spawn(slug, now=now):
                handle = await self._spawn_grader(slug, image=resolved, trigger=trigger)
                if handle is not None:
                    new_handles[slug] = handle
                    spawned += 1

        # Cancel collectors for handles we no longer carry (pod vanished out-of-band
        # or was reaped); finalizes their run as CANCELLED.
        carried = {id(h) for h in new_handles.values()}
        for old in self._handles.values():
            if id(old) not in carried:
                await old.kill_and_delete()
        self._handles = new_handles

        logger.info(
            "Reconcile [%s] done: desired=%d kept=%d adopted=%d spawned=%d reaped=%d running=%d",
            trigger,
            len(desired),
            kept,
            adopted,
            spawned,
            reaped,
            len(self._handles),
        )

    async def _reap(
        self,
        pod: GraderPodInfo,
        *,
        desired: set[SnapshotSlug],
        image: ResolvedImage,
        tracked: dict[str, AgentRunHandle],
    ) -> None:
        """Delete an unwanted grader pod and finalize its run.

        If this process owns the pod (has a handle), cancel via the handle so its
        collector finalizes; otherwise reap it via the registry (orphan path).
        """
        reason = (
            "snapshot_removed"
            if pod.snapshot_slug not in desired
            else "wrong_image"
            if pod.image_ref != image.oci_ref
            else "terminal"
            if pod.phase in (PodPhase.SUCCEEDED, PodPhase.FAILED)
            else "duplicate"
        )
        handle = tracked.get(pod.name)
        if handle is not None:
            logger.info("Reaping tracked grader %s for %s (reason: %s)", pod.name, pod.snapshot_slug, reason)
            await handle.kill_and_delete()
        else:
            await self._registry.reap_grader_pod(pod, reason=reason)

    # --- Crash-loop backoff ---

    def _record_failure(self, slug: SnapshotSlug, *, now: float) -> None:
        """Account a grader crash for ``slug`` and schedule its next respawn window."""
        prev = self._failures.get(slug)
        consecutive = (prev.consecutive if prev else 0) + 1
        delay = min(self._backoff_base_s * 2 ** (consecutive - 1), self._backoff_max_s)
        self._failures[slug] = _FailureState(consecutive=consecutive, respawn_not_before=now + delay)
        if consecutive >= self._max_consecutive_failures:
            logger.error(
                "Grader for %s failed %d times in a row — quarantined, no respawn until the grader "
                "definition changes or an operator clears it",
                slug,
                consecutive,
            )
        else:
            logger.warning(
                "Grader for %s failed (%d consecutive); backing off %.0fs before respawn", slug, consecutive, delay
            )

    def _may_spawn(self, slug: SnapshotSlug, *, now: float) -> bool:
        """False while ``slug`` is quarantined or inside its crash-loop backoff window."""
        state = self._failures.get(slug)
        if state is None:
            return True
        if state.consecutive >= self._max_consecutive_failures:
            return False
        return now >= state.respawn_not_before

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

    async def _spawn_grader(
        self, snapshot_slug: SnapshotSlug, *, image: ResolvedImage, trigger: str
    ) -> AgentRunHandle | None:
        try:
            handle = await self._registry.start_snapshot_grader(
                image=image, snapshot_slug=snapshot_slug, model=self._model
            )
            logger.info(
                "Spawned grader %s for %s (trigger: %s, image: %s)", handle.name, snapshot_slug, trigger, image.digest
            )
            return handle
        except Exception:
            logger.exception("Failed to start grader for %s (trigger: %s)", snapshot_slug, trigger)
            return None

    async def shutdown(self) -> None:
        """Stop listeners + timers. Leaves grader pods running so the next backend
        instance adopts them (no restart churn)."""
        self._shutdown = True
        logger.info("Shutting down grader supervisor (leaving graders running)...")
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        if self._periodic_task is not None and not self._periodic_task.done():
            self._periodic_task.cancel()
        await self._stop_listener()
        # Intentionally do NOT kill grader pods or cancel their collector tasks:
        # the pods outlive this process and are adopted on the next startup.
        self._handles = {}
        logger.info("Grader supervisor stopped")
