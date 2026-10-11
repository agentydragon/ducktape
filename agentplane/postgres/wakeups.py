"""Replica-local, payload-free wakeups per NOTIFY channel; the committed rows stay the authority.

A notification carries nothing, so a woken reader re-reads what it follows: commits on any replica,
and commits made while the LISTEN connection was down, are read from the tables. Losing or
re-registering the connection wakes every channel for the same reason.
"""

from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.changes import Changes
from agentplane.postgres.listener import PostgresListener


class Wakeups:
    """One LISTEN connection, fanning each channel out to that channel's `Changes`."""

    def __init__(self, url: URL, *, channels: Iterable[str], application_name: str) -> None:
        self.changes = {channel: Changes() for channel in channels}
        self.listener = PostgresListener(
            url,
            channels=tuple(self.changes),
            application_name=application_name,
            notified=self._notified,
            invalidated=self._wake_all,
        )

    def _notified(self, channel: str, _payload: object) -> None:
        self.changes[channel].notify()

    def _wake_all(self) -> None:
        for changes in self.changes.values():
            changes.notify()


async def notify(session: AsyncSession, channel: str) -> None:
    # PostgreSQL delivers NOTIFY only on commit, to every replica listening.
    await session.execute(select(func.pg_notify(channel, "")))
