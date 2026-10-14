"""Hands the Dispatches a deleted Sandbox's deletion ended to the streams open on them, which the
deletion of their rows would otherwise leave unannounced."""

import asyncio
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from agentplane.session_dispatcher.db import Dispatch


class EndedDispatches:
    """Replica-local: a stream on another replica does not see them."""

    def __init__(self) -> None:
        self._watchers: dict[asyncio.Event, list[Dispatch]] = {}

    @contextmanager
    def subscribe(self, waiter: asyncio.Event) -> Iterator[list[Dispatch]]:
        """The list fills with ended Dispatches, and `waiter` is set when it does; the reader drains it."""
        ended: list[Dispatch] = []
        self._watchers[waiter] = ended
        try:
            yield ended
        finally:
            del self._watchers[waiter]

    def publish(self, rows: Sequence[Dispatch]) -> None:
        for waiter, ended in self._watchers.items():
            ended.extend(rows)
            waiter.set()
