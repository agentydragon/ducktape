"""Replica-local wakeups for WatchSessionSummaries; the fold's rows stay the authority.

A notification carries no payload: a woken watcher only tells its caller to re-read what it holds,
so commits on any replica, and commits missed while the LISTEN connection was down, are covered.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.postgres.listener import PostgresListener

CHANNEL = "agentplane_history_summary_changes"


async def notify_summaries(session: AsyncSession) -> None:
    # PostgreSQL delivers NOTIFY only on commit, to every replica listening.
    await session.execute(select(func.pg_notify(CHANNEL, "")))


class SummaryChanges:
    def __init__(self, database_url: URL) -> None:
        self._waiters: set[asyncio.Event] = set()
        self.listener = PostgresListener(
            database_url,
            channels=(CHANNEL,),
            application_name="agentplane-history-summary-changes",
            notified=lambda _channel, _payload: self._wake(),
            invalidated=self._wake,
        )

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Event]:
        """Clear the event before each notice; it is set by any later fold commit or listener reconnect."""
        waiter = asyncio.Event()
        self._waiters.add(waiter)
        try:
            yield waiter
        finally:
            self._waiters.discard(waiter)

    def _wake(self) -> None:
        for waiter in self._waiters:
            waiter.set()
