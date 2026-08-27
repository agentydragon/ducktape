"""The conversation wake LISTEN/NOTIFY channel, against a real Postgres.

The driver and the connection lifecycle are what these are for: a listener written against the
wrong driver's API passes every test that never opens a socket.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from uuid import uuid4

import pytest_bazel

from haku.console.x.conversation_wakes import (
    CHANNEL,
    ConversationWakeEvent,
    ConversationWakeKind,
    ConversationWakes,
    RecheckHeld,
    notify_conversation,
)
from haku.console.x.session_wakes import SessionEventKind, notify
from haku.console.x.testing.wake_probes import delivered, emit_raw, kill_listener_backends, nothing_within
from util.enum_vocab import UnknownValue


@contextlib.contextmanager
def _watched(conversation_wakes: ConversationWakes) -> Iterator[asyncio.Queue[ConversationWakeEvent | RecheckHeld]]:
    received: asyncio.Queue[ConversationWakeEvent | RecheckHeld] = asyncio.Queue()
    with conversation_wakes.watch(received.put_nowait):
        yield received


async def test_notify_conversation_puts_a_readable_event_on_its_channel(raw_payloads, migrated_sessions) -> None:
    conversation_id = uuid4()
    received = await raw_payloads(CHANNEL)

    async with migrated_sessions.begin() as db:
        await notify_conversation(db, ConversationWakeKind.UPDATE, conversation_id, position=7)

    assert ConversationWakeEvent.model_validate_json(await delivered(received)) == ConversationWakeEvent(
        kind=ConversationWakeKind.UPDATE, conversation_id=conversation_id, position=7
    )


async def test_a_conversation_watcher_is_handed_the_wake(conversation_wakes, migrated_sessions) -> None:
    conversation_id = uuid4()

    with _watched(conversation_wakes) as received:
        async with migrated_sessions.begin() as db:
            await notify_conversation(db, ConversationWakeKind.RUNTIME_DEMAND, conversation_id)

        assert await delivered(received) == ConversationWakeEvent(
            kind=ConversationWakeKind.RUNTIME_DEMAND, conversation_id=conversation_id, position=None
        )


async def test_a_session_wake_does_not_reach_conversation_watchers(conversation_wakes, migrated_sessions) -> None:
    """The layers do not share a wire, a connection, or a module: a conversation subscriber never
    sees a session's wake, and here that is structural — this watcher's listener is on the
    conversation channel alone, so a `session_events` notify has no path to it."""
    with _watched(conversation_wakes) as received:
        async with migrated_sessions.begin() as db:
            await notify(db, SessionEventKind.UPDATE, uuid4())
        assert await nothing_within(received, 2)

        # And the watcher itself is alive: its own channel still reaches it.
        async with migrated_sessions.begin() as db:
            await notify_conversation(db, ConversationWakeKind.UPDATE, uuid4())
        follow_up = await delivered(received)
        assert isinstance(follow_up, ConversationWakeEvent)


async def test_a_conversation_field_a_later_release_adds_still_wakes_this_one(
    conversation_wakes, migrated_sessions
) -> None:
    """The conversation channel carries the same wire contract as the session one."""
    conversation_id = uuid4()

    with _watched(conversation_wakes) as received:
        await emit_raw(
            migrated_sessions,
            CHANNEL,
            f'{{"kind": "update", "conversation_id": "{conversation_id}", "queued_at": "2026-08-18T00:00:00Z"}}',
        )
        assert await delivered(received) == ConversationWakeEvent(
            kind=ConversationWakeKind.UPDATE, conversation_id=conversation_id, position=None
        )


async def test_a_conversation_kind_a_later_release_adds_is_delivered_as_a_named_unknown(
    conversation_wakes, migrated_sessions
) -> None:
    conversation_id = uuid4()

    with _watched(conversation_wakes) as received:
        await emit_raw(migrated_sessions, CHANNEL, f'{{"kind": "compacted", "conversation_id": "{conversation_id}"}}')
        first = await delivered(received)
        assert isinstance(first, ConversationWakeEvent)
        assert isinstance(first.kind, UnknownValue)


async def test_a_reconnect_tells_conversation_watchers_to_recheck(conversation_wakes, migrated_sessions) -> None:
    """A conversation watcher cannot be woken toward a session it should re-read — it holds
    conversations — so the reconnect's "re-check" arrives as its own named variant."""
    with _watched(conversation_wakes) as received:
        await kill_listener_backends(migrated_sessions)
        async with asyncio.timeout(30):
            while not isinstance(await received.get(), RecheckHeld):
                pass


async def test_start_is_idempotent_enough_to_close_twice(migrated_db_url) -> None:
    wakes = ConversationWakes(migrated_db_url)
    await wakes.start()
    await wakes.aclose()
    await wakes.aclose()


if __name__ == "__main__":
    pytest_bazel.main()
