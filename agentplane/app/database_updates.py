"""Replica-local invalidations of durable state, per NOTIFY channel, including reconnect gaps."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.app.changes import Changes
from agentplane.postgres.listener import PostgresListener


class Channel(StrEnum):
    """What a replica listens for. A notification carries no payload -- no thread and no identity --
    so its readers re-read what they follow."""

    # A thread, its copied event log or its feed state was written.
    THREADS = "agentplane_thread_updates"
    # A browser session row was deleted: logout, the rotation a login makes, or expiry cleanup.
    OPERATOR_SESSIONS = "agentplane_operator_sessions"


class DatabaseUpdates:
    """One LISTEN connection per replica, fanning each channel out to that channel's `Changes`."""

    def __init__(self, database_url: URL) -> None:
        self.changes = {channel: Changes() for channel in Channel}
        self.listener = PostgresListener(
            database_url,
            channels=tuple(Channel),
            application_name="agentplane-database-updates",
            notified=self._notified,
            invalidated=self._wake_all,
        )

    def _notified(self, channel: str, _payload: object) -> None:
        self.changes[Channel(channel)].notify()

    def _wake_all(self) -> None:
        for changes in self.changes.values():
            changes.notify()


async def notify(session: AsyncSession, channel: Channel) -> None:
    # PostgreSQL delivers NOTIFY only on commit, to every replica listening.
    await session.execute(select(func.pg_notify(channel, "")))
