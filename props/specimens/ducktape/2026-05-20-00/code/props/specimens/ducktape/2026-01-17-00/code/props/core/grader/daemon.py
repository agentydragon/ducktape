"""Grader daemon scaffold for persistent snapshot grading.

The daemon is a k8s controller-style reconciliation loop:
- Goal: make grading_pending empty for its snapshot
- When drift exists → grade; when empty → sleep until woken by pg_notify
- Context exhaustion → restart with fresh agent, query remaining drift
"""

from __future__ import annotations

import asyncio
import json
import logging

import asyncpg

from props.core.db.config import DatabaseConfig
from props.core.grader.drift_handler import GraderDriftHandler, check_grading_pending
from props.core.ids import SnapshotSlug

logger = logging.getLogger(__name__)

# pg_notify channel for grading-related events
GRADING_PENDING_CHANNEL = "grading_pending"


class GraderDaemonScaffold:
    """Scaffold that manages the grader daemon lifecycle.

    Responsibilities:
    - Background pg_listen task for notifications
    - Wake/sleep coordination via asyncio.Event
    - Agent run loop with restart on context exhaustion
    - Notification queue management
    """

    def __init__(self, snapshot_slug: SnapshotSlug, db_config: DatabaseConfig):
        self._snapshot_slug = snapshot_slug
        self._db_config = db_config
        self._notification_queue: list[dict] = []
        self._wake_event = asyncio.Event()
        self._listener_task: asyncio.Task | None = None
        self._listener_conn: asyncpg.Connection | None = None
        self._shutdown = False

    @property
    def snapshot_slug(self) -> SnapshotSlug:
        return self._snapshot_slug

    @property
    def notification_queue(self) -> list[dict]:
        """Shared queue for handler to drain."""
        return self._notification_queue

    @property
    def wake_event(self) -> asyncio.Event:
        """Event set when notifications arrive."""
        return self._wake_event

    def create_drift_handler(self) -> GraderDriftHandler:
        """Create drift handler with shared queue and event."""
        return GraderDriftHandler(
            snapshot_slug=self._snapshot_slug, notification_queue=self._notification_queue, wake_event=self._wake_event
        )

    async def _notification_callback(
        self, connection: asyncpg.Connection, pid: int, channel: str, payload: object
    ) -> None:
        """Handle incoming pg_notify notifications."""
        if not isinstance(payload, str):
            logger.warning(f"Unexpected non-string payload type: {type(payload)}")
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON in notification payload: {payload}")
            return

        notif_snapshot = data.get("snapshot_slug")
        if notif_snapshot != self._snapshot_slug:
            # Not for us
            return

        logger.debug(f"Notification for {self._snapshot_slug}: {data.get('event')}")
        self._notification_queue.append(data)
        self._wake_event.set()

    async def _start_listener(self) -> None:
        """Start background pg_listen task."""
        admin = self._db_config.admin
        dsn = f"postgresql://{admin.user}:{admin.password}@{admin.host}:{admin.port}/{admin.database}"

        self._listener_conn = await asyncpg.connect(dsn)
        await self._listener_conn.add_listener(GRADING_PENDING_CHANNEL, self._notification_callback)
        logger.info(f"Listening on channel '{GRADING_PENDING_CHANNEL}' for {self._snapshot_slug}")

    async def _stop_listener(self) -> None:
        """Stop background listener."""
        if self._listener_conn:
            try:
                await self._listener_conn.remove_listener(GRADING_PENDING_CHANNEL, self._notification_callback)
                await self._listener_conn.close()
            except Exception as e:
                logger.warning(f"Error closing listener connection: {e}")
            self._listener_conn = None

    async def wait_for_drift_or_notification(self) -> list[dict]:
        """Wait until there's drift or a notification arrives.

        Returns accumulated notifications (may be empty if drift detected on check).
        """
        # First check if there's already drift
        if check_grading_pending(self._snapshot_slug):
            # There's work to do, don't wait
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
            return notifs

        # No drift, wait for notification
        logger.info(f"No drift for {self._snapshot_slug}, waiting for notification...")
        self._wake_event.clear()
        await self._wake_event.wait()

        # Drain queue
        notifs = list(self._notification_queue)
        self._notification_queue.clear()
        return notifs

    async def __aenter__(self) -> GraderDaemonScaffold:
        """Start listener on context entry."""
        await self._start_listener()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Stop listener on context exit."""
        self._shutdown = True
        await self._stop_listener()

    def shutdown(self) -> None:
        """Signal shutdown (can be called from another task)."""
        self._shutdown = True
        self._wake_event.set()  # Wake up if sleeping

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown
