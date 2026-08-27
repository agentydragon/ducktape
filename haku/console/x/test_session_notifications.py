"""The wake LISTEN/NOTIFY channels, against a real Postgres.

The driver and the connection lifecycle are what these are for: a listener written against the
wrong driver's API passes every test that never opens a socket.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_bazel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.x.session_notifications import (
    CHANNEL,
    CONVERSATION_CHANNEL,
    ConversationWakeEvent,
    ConversationWakeKind,
    RecheckHeld,
    SessionEvent,
    SessionEventKind,
    SessionNotifications,
    libpq_dsn,
    notify,
    notify_conversation,
    notify_raw,
)
from util.enum_vocab import UnknownValue


@pytest.fixture
async def raw_payloads(migrated_db_url: str) -> AsyncIterator[Callable[[str], Awaitable[asyncio.Queue[str]]]]:
    """A raw listener per channel, so a wire pin has no code under test on its reading end."""
    connections: list[asyncpg.Connection[asyncpg.Record]] = []

    async def listen(channel: str) -> asyncio.Queue[str]:
        received: asyncio.Queue[str] = asyncio.Queue()
        connection = await asyncpg.connect(libpq_dsn(migrated_db_url))
        connections.append(connection)
        await connection.add_listener(channel, lambda _conn, _pid, _channel, payload: received.put_nowait(str(payload)))
        return received

    yield listen
    for connection in connections:
        await connection.close(timeout=5)


@contextlib.contextmanager
def _watched_session(notifications: SessionNotifications, session_id: UUID) -> Iterator[asyncio.Queue[SessionEvent]]:
    received: asyncio.Queue[SessionEvent] = asyncio.Queue()
    with notifications.watch_session(session_id, received.put_nowait):
        yield received


@contextlib.contextmanager
def _watched_conversations(
    notifications: SessionNotifications,
) -> Iterator[asyncio.Queue[ConversationWakeEvent | RecheckHeld]]:
    received: asyncio.Queue[ConversationWakeEvent | RecheckHeld] = asyncio.Queue()
    with notifications.watch_conversations(received.put_nowait):
        yield received


async def _delivered[T](received: asyncio.Queue[T]) -> T:
    async with asyncio.timeout(30):
        return await received.get()


async def _nothing_within[T](received: asyncio.Queue[T], seconds: float) -> bool:
    """Delivery is asynchronous, so an absence can only be asserted by waiting one out."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await received.get()
            return False
    return True


async def _emit_raw(db_sessions: async_sessionmaker[AsyncSession], channel: str, payload: str) -> None:
    """`notify_raw`, with the transaction the production callers already hold opened here."""
    async with db_sessions.begin() as db:
        await notify_raw(db, channel, payload)


async def _kill_listener_backends(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    """The closest thing to the real failure: a database restart, a failover, a connection reaper."""
    async with db_sessions.begin() as db:
        await db.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND query LIKE 'LISTEN%'"
            )
        )


async def test_a_session_watcher_hears_every_kind_for_its_session(notifications, migrated_sessions) -> None:
    """Registration is by scope; the kind arrives as payload for the consumer to dispatch on."""
    session_id = uuid4()

    with _watched_session(notifications, session_id) as received:
        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, session_id)
            await notify(db, SessionEventKind.ABORT, session_id)

        assert await _delivered(received) == SessionEvent(kind=SessionEventKind.UPDATE, session_id=session_id)
        assert await _delivered(received) == SessionEvent(kind=SessionEventKind.ABORT, session_id=session_id)


async def test_a_wake_for_another_session_does_not_reach_this_watcher(notifications, migrated_sessions) -> None:
    mine = uuid4()

    with _watched_session(notifications, mine) as received:
        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, uuid4())
        assert await _nothing_within(received, 2)


async def test_wait_is_woken_by_any_kind_for_its_session(notifications, migrated_sessions) -> None:
    """A waiter re-checks the durable state it waits on, so any wake about its session serves."""
    session_id = uuid4()
    waiting = asyncio.create_task(notifications.wait(session_id, timeout_seconds=30))
    await asyncio.sleep(0)  # one tick, so the task registers before the notify

    async with migrated_sessions.begin() as db:
        await notify(db, SessionEventKind.PROMPT, session_id)

    assert await waiting is True


async def test_wait_reports_a_timeout_rather_than_hanging(notifications) -> None:
    assert await notifications.wait(uuid4(), timeout_seconds=0.5) is False


async def test_notify_puts_a_readable_event_on_the_channel(raw_payloads, migrated_sessions) -> None:
    session_id = uuid4()
    received = await raw_payloads(CHANNEL)

    async with migrated_sessions.begin() as db:
        await notify(db, SessionEventKind.ABORT, session_id)

    assert SessionEvent.model_validate_json(await _delivered(received)) == SessionEvent(
        kind=SessionEventKind.ABORT, session_id=session_id
    )


async def test_notify_conversation_puts_a_readable_event_on_its_channel(raw_payloads, migrated_sessions) -> None:
    conversation_id = uuid4()
    received = await raw_payloads(CONVERSATION_CHANNEL)

    async with migrated_sessions.begin() as db:
        await notify_conversation(db, ConversationWakeKind.UPDATE, conversation_id, position=7)

    assert ConversationWakeEvent.model_validate_json(await _delivered(received)) == ConversationWakeEvent(
        kind=ConversationWakeKind.UPDATE, conversation_id=conversation_id, position=7
    )


async def test_a_conversation_watcher_is_handed_the_wake(notifications, migrated_sessions) -> None:
    conversation_id = uuid4()

    with _watched_conversations(notifications) as received:
        async with migrated_sessions.begin() as db:
            await notify_conversation(db, ConversationWakeKind.RUNTIME_DEMAND, conversation_id)

        assert await _delivered(received) == ConversationWakeEvent(
            kind=ConversationWakeKind.RUNTIME_DEMAND, conversation_id=conversation_id, position=None
        )


async def test_a_session_wake_does_not_reach_conversation_watchers(notifications, migrated_sessions) -> None:
    """The layers do not share a wire: a conversation subscriber never sees a session's wake."""
    with _watched_conversations(notifications) as received:
        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, uuid4())
        assert await _nothing_within(received, 2)

        # And the watcher itself is alive: its own channel still reaches it.
        async with migrated_sessions.begin() as db:
            await notify_conversation(db, ConversationWakeKind.UPDATE, uuid4())
        follow_up = await _delivered(received)
        assert isinstance(follow_up, ConversationWakeEvent)


async def test_an_unreadable_payload_does_not_take_the_listener_down(notifications, migrated_sessions) -> None:
    """The parse runs on asyncpg's reader task, which is shared by every waiting consumer."""
    session_id = uuid4()

    with _watched_session(notifications, session_id) as received:
        await _emit_raw(migrated_sessions, CHANNEL, "not an event")
        assert await _nothing_within(received, 2)

        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, session_id)
        assert await _delivered(received) == SessionEvent(kind=SessionEventKind.UPDATE, session_id=session_id)


async def test_a_field_a_later_release_adds_still_wakes_this_one(notifications, migrated_sessions) -> None:
    """The kind is one this release has; only the envelope grew. Under `extra="forbid"` the whole
    payload failed to parse and the wake was dropped in silence — a turn nobody picks up, for the
    length of the roll, on a kind both images understand."""
    session_id = uuid4()

    with _watched_session(notifications, session_id) as received:
        await _emit_raw(
            migrated_sessions,
            CHANNEL,
            f'{{"kind": "update", "session_id": "{session_id}", "queued_at": "2026-08-18T00:00:00Z"}}',
        )
        assert await _delivered(received) == SessionEvent(kind=SessionEventKind.UPDATE, session_id=session_id)


async def test_a_conversation_field_a_later_release_adds_still_wakes_this_one(notifications, migrated_sessions) -> None:
    """The conversation channel carries the same wire contract as the session one."""
    conversation_id = uuid4()

    with _watched_conversations(notifications) as received:
        await _emit_raw(
            migrated_sessions,
            CONVERSATION_CHANNEL,
            f'{{"kind": "update", "conversation_id": "{conversation_id}", "queued_at": "2026-08-18T00:00:00Z"}}',
        )
        assert await _delivered(received) == ConversationWakeEvent(
            kind=ConversationWakeKind.UPDATE, conversation_id=conversation_id, position=None
        )


async def test_a_kind_a_later_release_adds_is_delivered_as_a_named_unknown(notifications, migrated_sessions) -> None:
    """Kinds are payload, so an unknown one reaches the consumer as `UnknownValue` for its own
    dispatch to pass over — waking on one is safe, because every wake means "re-check". What must
    survive is the listener: the next notification on a kind it does have still arrives."""
    session_id = uuid4()

    with _watched_session(notifications, session_id) as received:
        await _emit_raw(migrated_sessions, CHANNEL, f'{{"kind": "compacted", "session_id": "{session_id}"}}')
        first = await _delivered(received)
        assert isinstance(first.kind, UnknownValue)

        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, session_id)
        assert await _delivered(received) == SessionEvent(kind=SessionEventKind.UPDATE, session_id=session_id)


async def test_a_conversation_kind_a_later_release_adds_is_delivered_as_a_named_unknown(
    notifications, migrated_sessions
) -> None:
    conversation_id = uuid4()

    with _watched_conversations(notifications) as received:
        await _emit_raw(
            migrated_sessions, CONVERSATION_CHANNEL, f'{{"kind": "compacted", "conversation_id": "{conversation_id}"}}'
        )
        first = await _delivered(received)
        assert isinstance(first, ConversationWakeEvent)
        assert isinstance(first.kind, UnknownValue)


async def test_the_listener_reconnects_and_wakes_its_waiters(notifications, migrated_sessions) -> None:
    """A dropped listener must not take its waiters with it: notifications committed while the
    socket was down are gone, so the only safe answer is "re-read your own state"."""
    session_id = uuid4()
    waiting = asyncio.create_task(notifications.wait(session_id, timeout_seconds=30))
    await asyncio.sleep(0)  # one tick, so the task registers before the kill

    await _kill_listener_backends(migrated_sessions)
    # The reconnect itself wakes every waiter.
    assert await waiting is True

    # And the rebuilt connection is really listening, not merely open.
    with _watched_session(notifications, session_id) as received:
        async with asyncio.timeout(30):
            while True:
                async with migrated_sessions.begin() as db:
                    await notify(db, SessionEventKind.UPDATE, session_id)
                if not await _nothing_within(received, 1):
                    break


async def test_a_reconnect_tells_conversation_watchers_to_recheck(notifications, migrated_sessions) -> None:
    """A conversation watcher cannot be woken toward a session it should re-read — it holds
    conversations — so the reconnect's "re-check" arrives as its own named variant."""
    with _watched_conversations(notifications) as received:
        await _kill_listener_backends(migrated_sessions)
        async with asyncio.timeout(30):
            while not isinstance(await received.get(), RecheckHeld):
                pass


async def test_start_is_idempotent_enough_to_close_twice(migrated_db_url) -> None:
    channel = SessionNotifications(migrated_db_url)
    await channel.start()
    await channel.aclose()
    await channel.aclose()


if __name__ == "__main__":
    pytest_bazel.main()
