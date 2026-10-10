"""Replica-local wakeups for WatchSessions; the session rows stay the authority.

A notification carries no payload. A woken reader re-reads after its position, so commits on
any replica, and commits missed while the LISTEN connection was down, are read from the table.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.engine import URL

from agentplane.postgres.listener import PostgresListener

CHANNEL = "agentplane_sandbox_session_changes"


class SessionChanges:
    def __init__(self, database_url: URL) -> None:
        self._waiters: set[asyncio.Event] = set()
        self.listener = PostgresListener(
            database_url,
            channels=(CHANNEL,),
            application_name="agentplane-sandbox-session-changes",
            notified=lambda _channel, _payload: self._wake(),
            invalidated=self._wake,
        )

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Event]:
        """Clear the event before each read; it is set by any later commit or listener reconnect."""
        waiter = asyncio.Event()
        self._waiters.add(waiter)
        try:
            yield waiter
        finally:
            self._waiters.discard(waiter)

    def _wake(self) -> None:
        for waiter in self._waiters:
            waiter.set()
