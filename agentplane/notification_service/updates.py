"""Commit-driven queue wakeups. NOTIFY is an invalidation, never the durable delivery."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.postgres.listener import PostgresListener

CHANNEL = "agentplane_notification_work"


async def notify(session: AsyncSession) -> None:
    # Empty payload; delivery content and scheduling live in the committed rows.
    await session.execute(select(func.pg_notify(CHANNEL, "")))


class Wakeups:
    def __init__(self, url: URL) -> None:
        self._waiters: set[asyncio.Event] = set()
        self.listener = PostgresListener(
            url,
            channels=(CHANNEL,),
            application_name="agentplane-notification-wakeups",
            notified=self._notified,
            invalidated=self._wake,
        )

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Event]:
        changed = asyncio.Event()
        self._waiters.add(changed)
        try:
            yield changed
        finally:
            self._waiters.remove(changed)

    def _wake(self) -> None:
        for changed in self._waiters:
            changed.set()

    def _notified(self, _channel: str, _payload: object) -> None:
        self._wake()
