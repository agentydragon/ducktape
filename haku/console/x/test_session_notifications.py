"""The session LISTEN/NOTIFY channel, against a real Postgres.

The driver and the connection lifecycle are what these are for: a listener written against the
wrong driver's API passes every test that never opens a socket.
"""

from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4

import asyncpg
import pytest_bazel
from sqlalchemy import text

from haku.console.x.session_notifications import (
    CHANNEL,
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


async def test_notify_puts_a_readable_event_on_the_channel(migrated_db_url, migrated_sessions) -> None:
    session_id = uuid4()
    received: asyncio.Queue[str] = asyncio.Queue()
    connection = await asyncpg.connect(libpq_dsn(migrated_db_url))
    try:
        await connection.add_listener(CHANNEL, lambda _conn, _pid, _channel, payload: received.put_nowait(str(payload)))
        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.ABORT, session_id)
        async with asyncio.timeout(30):
            payload = await received.get()
    finally:
        await connection.close(timeout=5)

    assert SessionEvent.model_validate_json(payload) == SessionEvent(kind=SessionEventKind.ABORT, session_id=session_id)


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


async def test_a_field_a_later_release_adds_still_wakes_this_one(notifications, migrated_sessions) -> None:
    """The kind is one this release has; only the envelope grew. Under `extra="forbid"` the whole
    payload failed to parse and the wake was dropped in silence — a turn nobody picks up, for the
    length of the roll, on a kind both images understand."""
    session_id = uuid4()
    from_a_later_release = f'{{"kind": "update", "session_id": "{session_id}", "queued_at": "2026-08-18T00:00:00Z"}}'

    async with notifications.subscribe(SessionEventKind.UPDATE, session_id) as woken:
        async with migrated_sessions.begin() as db:
            await db.execute(
                text("SELECT pg_notify(:channel, :payload)"), {"channel": CHANNEL, "payload": from_a_later_release}
            )
        async with asyncio.timeout(30):
            await woken.wait()


async def test_a_kind_a_later_release_adds_wakes_nobody_and_costs_nothing(notifications, migrated_sessions) -> None:
    """The other direction, where doing nothing is the correct answer rather than a loss: no waiter
    is registered under a kind this release does not have. What must survive is the listener — the
    next notification on a kind it does have still arrives."""
    session_id = uuid4()
    a_kind_from_later = f'{{"kind": "compacted", "session_id": "{session_id}"}}'

    async with notifications.subscribe(SessionEventKind.UPDATE, session_id) as woken:
        async with migrated_sessions.begin() as db:
            await db.execute(
                text("SELECT pg_notify(:channel, :payload)"), {"channel": CHANNEL, "payload": a_kind_from_later}
            )
        assert not await _woken_within(woken, 2)

        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, session_id)
        async with asyncio.timeout(30):
            await woken.wait()


async def test_wait_reports_a_timeout_rather_than_hanging(notifications) -> None:
    assert await notifications.wait(SessionEventKind.UPDATE, uuid4(), timeout_seconds=0.5) is False


async def test_the_listener_reconnects_and_wakes_its_waiters(notifications, migrated_sessions) -> None:
    """A dropped listener must not take its waiters with it: a session-lifetime abort watcher whose
    connection died would stop aborting until the session ended. Killing the backend here is the
    closest thing to the real failure (a database restart, a failover, an idle-connection reaper).
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
        # The reconnect itself wakes every waiter: notifications committed while the socket was
        # down are gone, so the only safe answer is "re-read your own state".
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
