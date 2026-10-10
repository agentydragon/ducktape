"""Replica-local invalidations of committed Session history."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.postgres.listener import PostgresListener

CHANNEL = "agentplane_session_history_changes"


async def notify_history(session: AsyncSession) -> None:
    # PostgreSQL delivers NOTIFY only on commit, to every replica listening.
    await session.execute(select(func.pg_notify(CHANNEL, "")))


class HistoryChanges:
    def __init__(self, database_url: URL) -> None:
        self._waiters: set[asyncio.Event] = set()
        self.listener = PostgresListener(
            database_url,
            channels=(CHANNEL,),
            application_name="agentplane-session-history-changes",
            notified=lambda _channel, _payload: self._wake(),
            invalidated=self._wake,
        )

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Event]:
        """Clear before each durable-state read; commits and listener reconnects set the event."""
        waiter = asyncio.Event()
        self._waiters.add(waiter)
        try:
            yield waiter
        finally:
            self._waiters.discard(waiter)

    def _wake(self) -> None:
        for waiter in self._waiters:
            waiter.set()
