"""Commit wakeups for bounded Action waits; PostgreSQL remains the state authority.

One dedicated LISTEN connection fans out UUID-only invalidations to this process's waiters.
Unlike console session followers, these short-lived waits fail explicitly on channel loss:
there is no best-effort startup or state-polling fallback. Recovery is supervised separately.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy.engine import make_url

from agentplane.postgres.listener import PostgresListener

CHANNEL = "agentplane_action_updates"
logger = logging.getLogger(__name__)


class UpdatesUnavailableError(Exception):
    """The caller can recover its receipt with an immediate read, without resubmission."""


class ActionSubscription:
    """A wakeup and an immutable connection generation: loss cannot be erased by recovery."""

    def __init__(self, listener: PostgresListener) -> None:
        self.changed = asyncio.Event()
        self._listener = listener
        self._generation = listener.generation
        self.check_available()

    def check_available(self) -> None:
        if not self._listener.connected or self._listener.generation != self._generation:
            raise UpdatesUnavailableError(
                "Action update channel unavailable; read the existing request with wait_seconds=0. "
                "Do not submit a new idempotency key."
            )


class ActionUpdates:
    def __init__(self, database_url: str) -> None:
        self._subscribers: dict[UUID, set[asyncio.Event]] = {}
        self._all_subscribers: set[asyncio.Event] = set()
        self.listener = PostgresListener(
            make_url(database_url),
            channels=(CHANNEL,),
            application_name="agentplane-action-updates",
            notified=self._notified,
            invalidated=self._wake_all,
        )

    @contextmanager
    def subscribe(self, request_id: UUID) -> Iterator[ActionSubscription]:
        subscription = ActionSubscription(self.listener)
        subscribers = self._subscribers.setdefault(request_id, set())
        subscribers.add(subscription.changed)
        try:
            yield subscription
        finally:
            subscribers.remove(subscription.changed)
            if not subscribers:
                del self._subscribers[request_id]

    @contextmanager
    def subscribe_all(self) -> Iterator[ActionSubscription]:
        """Subscribe to every committed Action event for server-push consumers."""
        subscription = ActionSubscription(self.listener)
        self._all_subscribers.add(subscription.changed)
        try:
            yield subscription
        finally:
            self._all_subscribers.discard(subscription.changed)

    def _notified(self, _channel: str, payload: object) -> None:
        for changed in self._all_subscribers:
            changed.set()
        try:
            request_id = UUID(str(payload))
        except ValueError:
            # An unreadable invalidation could name any waiter. Re-read them all rather than
            # silently losing an update or trusting notification text as durable state.
            logger.warning("Unreadable Action invalidation; waking all subscribers to re-read durable state")
            for subscribers in self._subscribers.values():
                for changed in subscribers:
                    changed.set()
            return
        for changed in self._subscribers.get(request_id, ()):
            changed.set()

    def _wake_all(self) -> None:
        for subscribers in self._subscribers.values():
            for changed in subscribers:
                changed.set()
        for changed in self._all_subscribers:
            changed.set()
