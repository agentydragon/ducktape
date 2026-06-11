"""Grader daemon scaffold for persistent snapshot grading.

The daemon is a k8s controller-style reconciliation loop:
- Goal: make grading_pending empty for its snapshot
- When drift exists → grade; when empty → sleep until woken by pg_notify
- Context exhaustion → restart with fresh agent, query remaining drift
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from props.core.ids import SnapshotSlug
from props.db.config import DatabaseConfig
from props.grader.drift_handler import GraderDriftHandler, check_grading_pending
from props.grader.notifications import GRADING_PENDING_CHANNEL, GradingPendingNotification

logger = logging.getLogger(__name__)


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
        self._notification_queue: list[GradingPendingNotification] = []
        self._wake_event = asyncio.Event()
        self._listener_task: asyncio.Task | None = None
        self._listener_conn: asyncpg.Connection | None = None
        self._shutdown = False

    @property
    def snapshot_slug(self) -> SnapshotSlug:
        return self._snapshot_slug

    @property
    def notification_queue(self) -> list[GradingPendingNotification]:
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

    def _notification_callback(
        self, connection: asyncpg.Connection[Any] | PoolConnectionProxy[Any], pid: int, channel: str, payload: object
    ) -> None:
        """Handle incoming pg_notify notifications."""
        if not isinstance(payload, str):
            raise TypeError(f"Expected string payload, got {type(payload)}")

        notification = GradingPendingNotification.model_validate_json(payload)

        if notification.snapshot_slug != self._snapshot_slug:
            return  # Not for us

        logger.debug(f"Notification for {self._snapshot_slug}: {notification.operation} {notification.item.table}")
        self._notification_queue.append(notification)
        self._wake_event.set()

    async def _start_listener(self) -> None:
        """Start background pg_listen task."""
        self._listener_conn = await asyncpg.connect(self._db_config.admin.url())
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

    async def wait_for_drift_or_notification(self) -> list[GradingPendingNotification]:
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
