"""Live terminal snapshots handed to streams before a deleted Sandbox loses its rows."""

import asyncio
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

from agentplane.proto import session_dispatcher_pb2


@dataclass(frozen=True)
class TerminalEvent:
    event_id: str
    dispatch: session_dispatcher_pb2.Dispatch


@dataclass
class TerminalSubscription:
    events: list[TerminalEvent] = field(default_factory=list)
    failure: str | None = None


class EndedDispatches:
    """A replica-local fanout for terminal snapshots received locally or through PostgreSQL."""

    def __init__(self) -> None:
        self._watchers: dict[asyncio.Event, TerminalSubscription] = {}
        self._available = True
        self._published: OrderedDict[str, None] = OrderedDict()

    def has_published(self, event_id: str) -> bool:
        return event_id in self._published

    @contextmanager
    def subscribe(self, waiter: asyncio.Event) -> Iterator[TerminalSubscription]:
        subscription = TerminalSubscription()
        if not self._available:
            subscription.failure = "terminal event listener is reconnecting; reconnect this WatchDispatches stream"
        self._watchers[waiter] = subscription
        try:
            yield subscription
        finally:
            del self._watchers[waiter]

    def publish(self, events: Sequence[TerminalEvent]) -> None:
        for event in events:
            if event.event_id in self._published:
                continue
            self._published[event.event_id] = None
            if len(self._published) > 8192:
                self._published.popitem(last=False)
            for waiter, subscription in self._watchers.items():
                subscription.events.append(event)
                waiter.set()

    def listener_state_changed(self, *, connected: bool) -> None:
        self._available = connected
        if not connected:
            self.fail_active("terminal event listener disconnected; reconnect this WatchDispatches stream")

    def fail_active(self, message: str) -> None:
        for waiter, subscription in self._watchers.items():
            subscription.failure = message
            waiter.set()
