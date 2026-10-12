"""Commit wakeups for bounded Action waits; PostgreSQL remains the state authority.

One dedicated LISTEN connection fans out UUID-only invalidations to this process's waiters.
Unlike console session followers, these short-lived waits fail explicitly on channel loss:
there is no best-effort startup or state-polling fallback. Recovery is supervised separately.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from uuid import UUID

from sqlalchemy.engine import make_url

from agentplane.changes import ChangeSignal
from agentplane.postgres.listener import PostgresListener

CHANNEL = "agentplane_action_updates"
PUSH_CHANNEL = "agentplane_push_updates"
CONNECTIONS_CHANNEL = "agentplane_connection_updates"
MCP_LINKAGE_CHANNEL = "agentplane_mcp_linkage_updates"
DISPATCH_CHANNEL = "agentplane_action_dispatches"
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


class ActionUpdateBroker:
    def __init__(self, database_url: str) -> None:
        self._subscribers: dict[UUID, set[asyncio.Event]] = {}
        self._all_subscribers: set[asyncio.Event] = set()
        self._push_subscribers: set[asyncio.Event] = set()
        self._connection_subscribers: set[asyncio.Event] = set()
        self._linkage_subscribers: set[asyncio.Event] = set()
        self._health_subscribers: set[asyncio.Event] = set()
        # Unlike the subscriptions, no availability check: a long-lived reconciler re-reads every
        # pending dispatch on each wake, and connection loss and registration are wakes.
        self.dispatches = ChangeSignal()
        self.listener = PostgresListener(
            make_url(database_url),
            channels=(CHANNEL, PUSH_CHANNEL, CONNECTIONS_CHANNEL, MCP_LINKAGE_CHANNEL, DISPATCH_CHANNEL),
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
    def _subscribe_broadcast(self, subscribers: set[asyncio.Event]) -> Iterator[ActionSubscription]:
        subscription = ActionSubscription(self.listener)
        subscribers.add(subscription.changed)
        try:
            yield subscription
        finally:
            subscribers.discard(subscription.changed)

    def subscribe_all(self) -> AbstractContextManager[ActionSubscription]:
        """Subscribe to every committed Action event for server-push consumers."""
        return self._subscribe_broadcast(self._all_subscribers)

    def subscribe_push(self) -> AbstractContextManager[ActionSubscription]:
        return self._subscribe_broadcast(self._push_subscribers)

    def subscribe_connections(self) -> AbstractContextManager[ActionSubscription]:
        return self._subscribe_broadcast(self._connection_subscribers)

    def subscribe_mcp_linkages(self) -> AbstractContextManager[ActionSubscription]:
        return self._subscribe_broadcast(self._linkage_subscribers)

    def subscribe_mcp_health(self) -> AbstractContextManager[ActionSubscription]:
        return self._subscribe_broadcast(self._health_subscribers)

    @staticmethod
    def _wake(subscribers: set[asyncio.Event]) -> None:
        for changed in subscribers:
            changed.set()

    def wake_mcp_health(self) -> None:
        """The supervisor's replica-local catalog changed; stream readers re-read it, and dispatches
        parked while their backend was unavailable are retried."""
        self._wake(self._health_subscribers)
        self.dispatches.notify()

    def _notified(self, channel: str, payload: object) -> None:
        if channel == DISPATCH_CHANNEL:
            self.dispatches.notify()
            return
        broadcast = {
            PUSH_CHANNEL: self._push_subscribers,
            CONNECTIONS_CHANNEL: self._connection_subscribers,
            MCP_LINKAGE_CHANNEL: self._linkage_subscribers,
        }.get(channel)
        if broadcast is not None:
            self._wake(broadcast)
            return
        self._wake(self._all_subscribers)
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
        self.dispatches.notify()
        for subscribers in (
            self._linkage_subscribers,
            self._health_subscribers,
            self._push_subscribers,
            self._connection_subscribers,
            self._all_subscribers,
            *self._subscribers.values(),
        ):
            self._wake(subscribers)
