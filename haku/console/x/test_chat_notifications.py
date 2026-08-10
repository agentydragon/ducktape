"""The chat LISTEN/NOTIFY channel, against a real Postgres.

The point of these is that the driver and the connection lifecycle are the thing that broke
before: a listener written against the wrong driver's API passed every test it had, because
those tests never opened a socket.
"""

from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4

import pytest_bazel
from sqlalchemy import text

from haku.console.x.chat_notifications import UPDATE_CHANNEL, ChatNotifications, notify


async def test_a_notify_wakes_the_waiter_for_that_session(notifications, migrated_sessions) -> None:
    session_id = uuid4()

    async with notifications.subscribe(UPDATE_CHANNEL, session_id) as woken:
        async with migrated_sessions.begin() as db:
            await notify(db, UPDATE_CHANNEL, session_id)
        async with asyncio.timeout(30):
            await woken.wait()


async def test_a_notify_for_another_session_does_not_wake_this_one(notifications, migrated_sessions) -> None:
    mine, theirs = uuid4(), uuid4()

    async with notifications.subscribe(UPDATE_CHANNEL, mine) as woken:
        async with migrated_sessions.begin() as db:
            await notify(db, UPDATE_CHANNEL, theirs)
        # Delivery is asynchronous, so waiting out a short window is the only way to assert
        # an absence; a bare check would pass before the notification had a chance to arrive.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(2):
                await woken.wait()
        assert not woken.is_set()


async def test_wait_reports_a_timeout_rather_than_hanging(notifications) -> None:
    assert await notifications.wait(UPDATE_CHANNEL, uuid4(), timeout_seconds=0.5) is False


async def test_the_listener_reconnects_and_wakes_its_waiters(notifications, migrated_sessions) -> None:
    """A dropped listener must not take its waiters with it.

    This is the property the previous implementation lacked: it borrowed a pooled connection
    per wait, so a connection that died surfaced as an exception to whoever was waiting —
    and in the case of the session-lifetime abort watcher, aborts simply stopped working
    until the session ended. Killing the backend here is the closest thing to the real
    failure (a database restart, a failover, an idle-connection reaper).
    """
    session_id = uuid4()

    async with notifications.subscribe(UPDATE_CHANNEL, session_id) as woken:
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
                    await notify(db, UPDATE_CHANNEL, session_id)
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(1):
                        await woken.wait()


async def test_start_is_idempotent_enough_to_close_twice(migrated_db_url) -> None:
    channel = ChatNotifications(migrated_db_url)
    await channel.start()
    await channel.aclose()
    await channel.aclose()


if __name__ == "__main__":
    pytest_bazel.main()
