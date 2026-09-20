"""A wake-up for whoever is reading whatever owns one: no payload, no queue, no ordering.

A reader clears its waiter, reads the current state, and waits again, so a burst of changes
coalesces into one read instead of a backlog of readings that are already out of date. One waiter
can be registered with several `Changes`, which is how the live stream waits on the cluster index
and the trajectory store at once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager


class Changes:
    def __init__(self) -> None:
        self._waiters: set[asyncio.Event] = set()

    @contextmanager
    def subscribe(self, waiter: asyncio.Event) -> Iterator[None]:
        self._waiters.add(waiter)
        try:
            yield
        finally:
            self._waiters.discard(waiter)

    def notify(self) -> None:
        for waiter in self._waiters:
            waiter.set()
