"""The session wake LISTEN/NOTIFY channel, against a real Postgres.

The driver and the connection lifecycle are what these are for: a listener written against the
wrong driver's API passes every test that never opens a socket.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest_bazel

from haku.console.notifications.session_wakes import CHANNEL, SessionEvent, SessionEventKind, SessionWakes, notify
from haku.console.x.testing.wake_probes import delivered, emit_raw, kill_listener_backends, nothing_within
from util.enum_vocab import UnknownValue


@contextlib.contextmanager
def _watched(session_wakes: SessionWakes, session_id: UUID) -> Iterator[asyncio.Queue[SessionEvent]]:
    received: asyncio.Queue[SessionEvent] = asyncio.Queue()
    with session_wakes.watch_session(session_id, received.put_nowait):
        yield received


async def test_a_session_watcher_hears_every_kind_for_its_session(session_wakes, migrated_sessions) -> None:
    """Registration is by scope; the kind arrives as payload for the consumer to dispatch on."""
    session_id = uuid4()

    with _watched(session_wakes, session_id) as received:
        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, session_id)
            await notify(db, SessionEventKind.ABORT, session_id)

        assert await delivered(received) == SessionEvent(kind=SessionEventKind.UPDATE, session_id=session_id)
        assert await delivered(received) == SessionEvent(kind=SessionEventKind.ABORT, session_id=session_id)


async def test_a_wake_for_another_session_does_not_reach_this_watcher(session_wakes, migrated_sessions) -> None:
    mine = uuid4()

    with _watched(session_wakes, mine) as received:
        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, uuid4())
        assert await nothing_within(received, 2)


async def test_wait_is_woken_by_any_kind_for_its_session(session_wakes, migrated_sessions) -> None:
    """A waiter re-checks the durable state it waits on, so any wake about its session serves."""
    session_id = uuid4()
    waiting = asyncio.create_task(session_wakes.wait(session_id, timeout_seconds=30))
    await asyncio.sleep(0)  # one tick, so the task registers before the notify

    async with migrated_sessions.begin() as db:
        await notify(db, SessionEventKind.PROMPT, session_id)

    assert await waiting is True


async def test_wait_reports_a_timeout_rather_than_hanging(session_wakes) -> None:
    assert await session_wakes.wait(uuid4(), timeout_seconds=0.5) is False


async def test_notify_puts_a_readable_event_on_the_channel(raw_payloads, migrated_sessions) -> None:
    session_id = uuid4()
    received = await raw_payloads(CHANNEL)

    async with migrated_sessions.begin() as db:
        await notify(db, SessionEventKind.ABORT, session_id)

    assert SessionEvent.model_validate_json(await delivered(received)) == SessionEvent(
        kind=SessionEventKind.ABORT, session_id=session_id
    )


async def test_an_unreadable_payload_does_not_take_the_listener_down(session_wakes, migrated_sessions) -> None:
    """The parse runs on asyncpg's reader task, which is shared by every waiting consumer."""
    session_id = uuid4()

    with _watched(session_wakes, session_id) as received:
        await emit_raw(migrated_sessions, CHANNEL, "not an event")
        assert await nothing_within(received, 2)

        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, session_id)
        assert await delivered(received) == SessionEvent(kind=SessionEventKind.UPDATE, session_id=session_id)


async def test_a_field_a_later_release_adds_still_wakes_this_one(session_wakes, migrated_sessions) -> None:
    """The kind is one this release has; only the envelope grew. Under `extra="forbid"` the whole
    payload failed to parse and the wake was dropped in silence — a turn nobody picks up, for the
    length of the roll, on a kind both images understand."""
    session_id = uuid4()

    with _watched(session_wakes, session_id) as received:
        await emit_raw(
            migrated_sessions,
            CHANNEL,
            f'{{"kind": "update", "session_id": "{session_id}", "queued_at": "2026-08-18T00:00:00Z"}}',
        )
        assert await delivered(received) == SessionEvent(kind=SessionEventKind.UPDATE, session_id=session_id)


async def test_a_kind_a_later_release_adds_is_delivered_as_a_named_unknown(session_wakes, migrated_sessions) -> None:
    """Kinds are payload, so an unknown one reaches the consumer as `UnknownValue` for its own
    dispatch to pass over — waking on one is safe, because every wake means "re-check". What must
    survive is the listener: the next notification on a kind it does have still arrives."""
    session_id = uuid4()

    with _watched(session_wakes, session_id) as received:
        await emit_raw(migrated_sessions, CHANNEL, f'{{"kind": "compacted", "session_id": "{session_id}"}}')
        first = await delivered(received)
        assert isinstance(first.kind, UnknownValue)

        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, session_id)
        assert await delivered(received) == SessionEvent(kind=SessionEventKind.UPDATE, session_id=session_id)


async def test_the_listener_reconnects_and_wakes_its_waiters(session_wakes, migrated_sessions) -> None:
    """A dropped listener must not take its waiters with it: notifications committed while the
    socket was down are gone, so the only safe answer is "re-read your own state"."""
    session_id = uuid4()
    waiting = asyncio.create_task(session_wakes.wait(session_id, timeout_seconds=30))
    await asyncio.sleep(0)  # one tick, so the task registers before the kill

    await kill_listener_backends(migrated_sessions)
    # The reconnect itself wakes every waiter.
    assert await waiting is True

    # And the rebuilt connection is really listening, not merely open.
    with _watched(session_wakes, session_id) as received:
        async with asyncio.timeout(30):
            while True:
                async with migrated_sessions.begin() as db:
                    await notify(db, SessionEventKind.UPDATE, session_id)
                if not await nothing_within(received, 1):
                    break


async def test_start_is_idempotent_enough_to_close_twice(migrated_db_url) -> None:
    wakes = SessionWakes(migrated_db_url)
    await wakes.start()
    await wakes.aclose()
    await wakes.aclose()


if __name__ == "__main__":
    pytest_bazel.main()
