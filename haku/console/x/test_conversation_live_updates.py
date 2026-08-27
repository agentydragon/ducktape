"""Conversation updates arriving on the console socket, against a real Postgres and a real hub.

Both ends are the point: the notification is emitted by the write's own transaction and travels a
broadcast channel, and the invalidation goes out on the console channel's sockets. Standing either
end in would assert the fan-out against an imagined shape, which is how the session listener passed
every test it had while raising on every call in production (<README.md> § Tests run against a real
database).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastapi import WebSocket
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.console_events import ConsoleEvent, ConsoleEventHub
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.x.conversation_live_updates import ConversationLiveUpdates
from haku.console.x.conversation_wakes import ConversationWakeKind, ConversationWakes, notify_conversation
from haku.console.x.session_store import SessionStore

# Long enough that several notifications land inside one window on a loaded machine, short enough
# that asserting a flush does not wait out anyone's patience.
WINDOW = timedelta(seconds=1)


class RecordingSocket:
    """A console tab, as much of one as the hub touches."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def accept(self) -> None: ...

    async def send_json(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...

    async def conversation_events(self, *, within: timedelta) -> list[dict[str, Any]]:
        """Every `conversation_changed` message that arrives within *within*.

        Delivery is asynchronous and deliberately delayed, so a presence and an absence alike can
        only be established by waiting one out.
        """
        await asyncio.sleep(within.total_seconds())
        return [message for message in self.messages if message["event_type"] == "conversation_changed"]


class FlakyHub(ConsoleEventHub):
    """A hub whose first local delivery fails — the one thing a healthy Postgres will not do."""

    def __init__(self, database_url: str, *, operator_identity_store: PostgresOperatorIdentityStore) -> None:
        super().__init__(database_url, operator_identity_store=operator_identity_store)
        self.deliveries_left_to_fail = 1

    async def deliver_locally(self, event_operator_id: UUID, event: ConsoleEvent) -> None:
        if self.deliveries_left_to_fail:
            self.deliveries_left_to_fail -= 1
            raise RuntimeError("the database went away mid-flush")
        await super().deliver_locally(event_operator_id, event)


@pytest.fixture
async def hub(
    migrated_db_url: str, migrated_identity_store: PostgresOperatorIdentityStore
) -> AsyncIterator[ConsoleEventHub]:
    started = ConsoleEventHub(migrated_db_url, operator_identity_store=migrated_identity_store)
    await started.start()
    try:
        yield started
    finally:
        await started.aclose()


@pytest.fixture
async def live_updates(
    conversation_wakes: ConversationWakes, hub: ConsoleEventHub, migrated_sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[ConversationLiveUpdates]:
    updates = ConversationLiveUpdates(conversation_wakes, hub, migrated_sessions, window=WINDOW)
    async with updates.run():
        yield updates


async def _tab(hub: ConsoleEventHub, operator_id: UUID) -> RecordingSocket:
    socket = RecordingSocket()
    assert await hub.connect(cast(WebSocket, socket), operator_id)
    return socket


async def test_a_write_that_changes_a_conversation_reaches_the_owning_operators_tab(
    live_updates: ConversationLiveUpdates, hub: ConsoleEventHub, chat_store: SessionStore, operator_id: UUID
) -> None:
    """Through an ordinary store write, not a hand-rolled notify: the publish belongs to the
    transaction that makes the change, so a change that rolled back announces nothing."""
    view, _ = await chat_store.create(operator_id)
    conversation_id = await chat_store.conversation_of(view.session_id)
    socket = await _tab(hub, operator_id)

    await chat_store.request_close(operator_id, view.session_id)

    assert await socket.conversation_events(within=WINDOW * 3) == [
        {"event_type": "conversation_changed", "conversation_id": str(conversation_id)}
    ]


async def test_the_event_says_only_which_conversation_changed(
    live_updates: ConversationLiveUpdates,
    hub: ConsoleEventHub,
    chat_store: SessionStore,
    migrated_sessions: async_sessionmaker[AsyncSession],
    operator_id: UUID,
) -> None:
    """The wire shape is the contract: an invalidation, never the record itself, which would make
    the socket a second source of truth for what a conversation holds."""
    view, _ = await chat_store.create(operator_id)
    conversation_id = await chat_store.conversation_of(view.session_id)
    socket = await _tab(hub, operator_id)

    async with migrated_sessions.begin() as db:
        await notify_conversation(db, ConversationWakeKind.UPDATE, conversation_id)

    assert [set(event) for event in await socket.conversation_events(within=WINDOW * 3)] == [
        {"event_type", "conversation_id"}
    ]


async def test_a_burst_of_changes_becomes_one_invalidation(
    live_updates: ConversationLiveUpdates,
    hub: ConsoleEventHub,
    chat_store: SessionStore,
    migrated_sessions: async_sessionmaker[AsyncSession],
    operator_id: UUID,
) -> None:
    """A streaming turn changes a conversation per delta, and each event costs a tab a refetch —
    so coalescing is what keeps the notification cheaper than what it triggers."""
    view, _ = await chat_store.create(operator_id)
    conversation_id = await chat_store.conversation_of(view.session_id)
    socket = await _tab(hub, operator_id)

    async with migrated_sessions.begin() as db:
        for _ in range(20):
            await notify_conversation(db, ConversationWakeKind.UPDATE, conversation_id)

    assert len(await socket.conversation_events(within=WINDOW * 3)) == 1


async def test_two_conversations_changing_together_are_invalidated_separately(
    live_updates: ConversationLiveUpdates,
    hub: ConsoleEventHub,
    chat_store: SessionStore,
    migrated_sessions: async_sessionmaker[AsyncSession],
    operator_id: UUID,
) -> None:
    """One window collapses a conversation's own changes, never two conversations into one event."""
    first, _ = await chat_store.create(operator_id)
    second, _ = await chat_store.create(operator_id)
    first_conversation = await chat_store.conversation_of(first.session_id)
    second_conversation = await chat_store.conversation_of(second.session_id)
    socket = await _tab(hub, operator_id)

    async with migrated_sessions.begin() as db:
        await notify_conversation(db, ConversationWakeKind.UPDATE, first_conversation)
        await notify_conversation(db, ConversationWakeKind.UPDATE, second_conversation)

    events = await socket.conversation_events(within=WINDOW * 3)
    assert {event["conversation_id"] for event in events} == {str(first_conversation), str(second_conversation)}


async def test_every_wake_kind_invalidates(
    live_updates: ConversationLiveUpdates,
    hub: ConsoleEventHub,
    chat_store: SessionStore,
    migrated_sessions: async_sessionmaker[AsyncSession],
    operator_id: UUID,
) -> None:
    """A prompt queued into a conversation with no open session emits only `runtime_demand`, and
    the queued prompt is a row the list shows — so filtering to `update` would go stale there."""
    view, _ = await chat_store.create(operator_id)
    conversation_id = await chat_store.conversation_of(view.session_id)
    socket = await _tab(hub, operator_id)

    async with migrated_sessions.begin() as db:
        await notify_conversation(db, ConversationWakeKind.RUNTIME_DEMAND, conversation_id)

    assert len(await socket.conversation_events(within=WINDOW * 3)) == 1


async def test_an_update_is_not_delivered_to_another_operators_tab(
    live_updates: ConversationLiveUpdates,
    hub: ConsoleEventHub,
    chat_store: SessionStore,
    migrated_sessions: async_sessionmaker[AsyncSession],
    migrated_identity_store: PostgresOperatorIdentityStore,
    operator_id: UUID,
) -> None:
    """`ConversationWakeEvent` carries no operator, so the routing rests entirely on the
    conversation's row."""
    other_operator = await migrated_identity_store.resolve_configured_external_user_key("another-authentik-user-id")
    view, _ = await chat_store.create(operator_id)
    conversation_id = await chat_store.conversation_of(view.session_id)
    mine = await _tab(hub, operator_id)
    theirs = await _tab(hub, other_operator)

    async with migrated_sessions.begin() as db:
        await notify_conversation(db, ConversationWakeKind.UPDATE, conversation_id)

    assert len(await mine.conversation_events(within=WINDOW * 3)) == 1
    assert await theirs.conversation_events(within=timedelta(0)) == []


async def test_a_conversation_this_database_does_not_have_is_dropped(
    live_updates: ConversationLiveUpdates,
    hub: ConsoleEventHub,
    migrated_sessions: async_sessionmaker[AsyncSession],
    operator_id: UUID,
) -> None:
    """The id arrives over a broadcast channel, so it can name a conversation with no owner here."""
    socket = await _tab(hub, operator_id)

    async with migrated_sessions.begin() as db:
        await notify_conversation(db, ConversationWakeKind.UPDATE, uuid4())

    assert await socket.conversation_events(within=WINDOW * 3) == []


async def test_a_failed_flush_does_not_stop_the_next_one(
    conversation_wakes: ConversationWakes,
    migrated_db_url: str,
    migrated_identity_store: PostgresOperatorIdentityStore,
    chat_store: SessionStore,
    migrated_sessions: async_sessionmaker[AsyncSession],
    operator_id: UUID,
) -> None:
    """A dropped invalidation is a delayed refresh; a dead publisher is a console that never
    updates again, so the loop has to outlive what fails inside it."""
    flaky = FlakyHub(migrated_db_url, operator_identity_store=migrated_identity_store)
    await flaky.start()
    view, _ = await chat_store.create(operator_id)
    conversation_id = await chat_store.conversation_of(view.session_id)
    socket = await _tab(flaky, operator_id)
    try:
        async with ConversationLiveUpdates(conversation_wakes, flaky, migrated_sessions, window=WINDOW).run():
            async with migrated_sessions.begin() as db:
                await notify_conversation(db, ConversationWakeKind.UPDATE, conversation_id)
            assert await socket.conversation_events(within=WINDOW * 3) == []

            async with migrated_sessions.begin() as db:
                await notify_conversation(db, ConversationWakeKind.UPDATE, conversation_id)
            assert len(await socket.conversation_events(within=WINDOW * 3)) == 1
    finally:
        await flaky.aclose()


if __name__ == "__main__":
    pytest_bazel.main()
