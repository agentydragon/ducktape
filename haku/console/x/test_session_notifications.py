"""The session LISTEN/NOTIFY channel, against a real Postgres.

The point of these is that the driver and the connection lifecycle are the thing that broke
before: a listener written against the wrong driver's API passed every test it had, because
those tests never opened a socket.

**The channel is being renamed, and that sets a trap for this file.** While `CHANNEL` and
`LEGACY_CHANNEL` are both notified, every event is delivered twice, so a test that calls `notify`
and watches a waiter wake proves nothing about *which* name woke it — it would pass with either
path completely broken. The two `_alone` tests below are the ones that do prove it: they drive
`pg_notify` on exactly one channel, so each name is covered end to end by itself.
"""

from __future__ import annotations

import asyncio
import contextlib
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_bazel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.x.session_notifications import (
    CHANNEL,
    LEGACY_CHANNEL,
    SessionEvent,
    SessionEventKind,
    SessionNotifications,
    libpq_dsn,
    notify,
)


async def _woken_within(event: asyncio.Event, seconds: float) -> bool:
    """Delivery is asynchronous, so an absence can only be asserted by waiting one out."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await event.wait()
    return event.is_set()


async def _notify_one_channel(
    sessions: async_sessionmaker[AsyncSession], channel: str, kind: SessionEventKind, session_id: UUID
) -> None:
    """What `notify` emits, but on a single name — which is what `notify` itself cannot do."""
    async with sessions.begin() as db:
        await db.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": channel, "payload": SessionEvent(kind=kind, session_id=session_id).model_dump_json()},
        )


async def test_a_notify_wakes_the_waiter_for_that_session(notifications, migrated_sessions) -> None:
    session_id = uuid4()

    async with notifications.subscribe(SessionEventKind.UPDATE, session_id) as woken:
        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, session_id)
        async with asyncio.timeout(30):
            await woken.wait()


async def test_a_notify_for_another_session_does_not_wake_this_one(notifications, migrated_sessions) -> None:
    mine, theirs = uuid4(), uuid4()

    async with notifications.subscribe(SessionEventKind.UPDATE, mine) as woken:
        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, theirs)
        assert not await _woken_within(woken, 2)


async def test_a_notify_of_another_kind_does_not_wake_this_one(notifications, migrated_sessions) -> None:
    """One channel now carries every kind, so the kind is what keeps the fan-out separate."""
    session_id = uuid4()

    async with notifications.subscribe(SessionEventKind.ABORT, session_id) as woken:
        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, session_id)
        assert not await _woken_within(woken, 2)


@pytest.mark.parametrize("channel", [CHANNEL, LEGACY_CHANNEL])
async def test_notify_puts_a_readable_event_on_the_channel(channel, migrated_db_url, migrated_sessions) -> None:
    """The wire format, read off a raw connection — the only test that would notice it drifting.

    Both names, because during the overlap both are load-bearing: the new one is what this release
    listens on, and the old one is the only thing a replica from the previous image hears.
    """
    session_id = uuid4()
    received: asyncio.Queue[str] = asyncio.Queue()
    connection = await asyncpg.connect(libpq_dsn(migrated_db_url))
    try:
        await connection.add_listener(channel, lambda _conn, _pid, _channel, payload: received.put_nowait(str(payload)))
        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.ABORT, session_id)
        async with asyncio.timeout(30):
            payload = await received.get()
    finally:
        await connection.close(timeout=5)

    assert SessionEvent.model_validate_json(payload) == SessionEvent(kind=SessionEventKind.ABORT, session_id=session_id)


@pytest.mark.parametrize("channel", [CHANNEL, LEGACY_CHANNEL])
async def test_an_event_on_one_channel_alone_wakes_the_waiter(channel, notifications, migrated_sessions) -> None:
    """Each name, end to end by itself — `notify` fires both, so it cannot answer this.

    `CHANNEL` alone is the new path proving it works before the old one is deleted. `LEGACY_CHANNEL`
    alone is a replica from the previous image, which only ever notifies there.
    """
    session_id = uuid4()

    async with notifications.subscribe(SessionEventKind.UPDATE, session_id) as woken:
        await _notify_one_channel(migrated_sessions, channel, SessionEventKind.UPDATE, session_id)
        async with asyncio.timeout(30):
            await woken.wait()


async def test_an_unreadable_payload_does_not_take_the_listener_down(notifications, migrated_sessions) -> None:
    """The parse runs on asyncpg's reader task, which is shared by every waiting session."""
    session_id = uuid4()

    async with notifications.subscribe(SessionEventKind.UPDATE, session_id) as woken:
        async with migrated_sessions.begin() as db:
            await db.execute(text("SELECT pg_notify(:channel, 'not an event')"), {"channel": CHANNEL})
        assert not await _woken_within(woken, 2)

        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, session_id)
        async with asyncio.timeout(30):
            await woken.wait()


async def test_wait_reports_a_timeout_rather_than_hanging(notifications) -> None:
    assert await notifications.wait(SessionEventKind.UPDATE, uuid4(), timeout_seconds=0.5) is False


async def test_the_listener_reconnects_and_wakes_its_waiters(notifications, migrated_sessions) -> None:
    """A dropped listener must not take its waiters with it.

    This is the property the previous implementation lacked: it borrowed a pooled connection
    per wait, so a connection that died surfaced as an exception to whoever was waiting —
    and in the case of the session-lifetime abort watcher, aborts simply stopped working
    until the session ended. Killing the backend here is the closest thing to the real
    failure (a database restart, a failover, an idle-connection reaper).
    """
    session_id = uuid4()

    async with notifications.subscribe(SessionEventKind.UPDATE, session_id) as woken:
        async with migrated_sessions.begin() as db:
            await db.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "AND query LIKE 'LISTEN%'"
                )
            )
        # The reconnect itself wakes every waiter, because notifications committed while the
        # socket was down are gone and the only safe answer is "re-read your own state".
        async with asyncio.timeout(30):
            await woken.wait()
        woken.clear()

        # And the rebuilt connection is really listening, not merely open.
        async with asyncio.timeout(30):
            while not woken.is_set():
                async with migrated_sessions.begin() as db:
                    await notify(db, SessionEventKind.UPDATE, session_id)
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(1):
                        await woken.wait()


async def test_start_is_idempotent_enough_to_close_twice(migrated_db_url) -> None:
    channel = SessionNotifications(migrated_db_url)
    await channel.start()
    await channel.aclose()
    await channel.aclose()


if __name__ == "__main__":
    pytest_bazel.main()
