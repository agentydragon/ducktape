"""Session updates arriving on the console socket, against a real Postgres and a real hub.

Both ends are the point. The notification is emitted by the write's own transaction and travels a
broadcast channel; the invalidation goes out on the console channel's sockets. Standing either end
in would assert the fan-out against a shape this file's author imagined — which is how the session
listener passed every test it had while raising on every call in production (<README.md> § Tests
run against a real database).
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
from haku.console.x.session_live_updates import SessionLiveUpdates
from haku.console.x.session_notifications import SessionEventKind, SessionNotifications, notify
from haku.console.x.session_runtime import SessionStore, SpaSession

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

    async def session_events(self, *, within: timedelta) -> list[dict[str, Any]]:
        """Every `session_changed` message that arrives within *within*.

        Delivery is asynchronous and deliberately delayed, so a presence and an absence alike can
        only be established by waiting one out.
        """
        await asyncio.sleep(within.total_seconds())
        return [message for message in self.messages if message["event_type"] == "session_changed"]


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
    notifications: SessionNotifications, hub: ConsoleEventHub, migrated_sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[SessionLiveUpdates]:
    updates = SessionLiveUpdates(notifications, hub, migrated_sessions, window=WINDOW)
    async with updates.run():
        yield updates


async def _tab(hub: ConsoleEventHub, operator_id: UUID) -> RecordingSocket:
    socket = RecordingSocket()
    assert await hub.connect(cast(WebSocket, socket), operator_id)
    return socket


async def test_a_write_that_changes_a_session_reaches_the_owning_operators_tab(
    live_updates: SessionLiveUpdates, hub: ConsoleEventHub, chat_store: SessionStore, operator_id: UUID
) -> None:
    """Through an ordinary store write, not a hand-rolled notify: the publish belongs to the
    transaction that makes the change, so a change that rolled back announces nothing."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    socket = await _tab(hub, operator_id)

    await chat_store.request_close(operator_id, view.session_id)

    assert await socket.session_events(within=WINDOW * 3) == [
        {"event_type": "session_changed", "session_id": str(view.session_id)}
    ]


async def test_the_event_says_only_which_session_changed(
    live_updates: SessionLiveUpdates,
    hub: ConsoleEventHub,
    chat_store: SessionStore,
    migrated_sessions: async_sessionmaker[AsyncSession],
    operator_id: UUID,
) -> None:
    """The wire shape is the contract: an invalidation, never the transcript itself.

    Content here would make the socket a second source of truth for what a session holds, and
    every consumer would then have to decide which of the two to believe.
    """
    view, _ = await chat_store.create(operator_id, SpaSession())
    socket = await _tab(hub, operator_id)

    async with migrated_sessions.begin() as db:
        await notify(db, SessionEventKind.UPDATE, view.session_id)

    assert [set(event) for event in await socket.session_events(within=WINDOW * 3)] == [{"event_type", "session_id"}]


async def test_a_burst_of_changes_becomes_one_invalidation(
    live_updates: SessionLiveUpdates,
    hub: ConsoleEventHub,
    chat_store: SessionStore,
    migrated_sessions: async_sessionmaker[AsyncSession],
    operator_id: UUID,
) -> None:
    """A streaming turn changes a session per delta, and each event costs a tab a whole
    transcript — so coalescing is what keeps the notification cheaper than what it triggers."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    socket = await _tab(hub, operator_id)

    async with migrated_sessions.begin() as db:
        for _ in range(20):
            await notify(db, SessionEventKind.UPDATE, view.session_id)

    assert len(await socket.session_events(within=WINDOW * 3)) == 1


async def test_two_sessions_changing_together_are_invalidated_separately(
    live_updates: SessionLiveUpdates,
    hub: ConsoleEventHub,
    chat_store: SessionStore,
    migrated_sessions: async_sessionmaker[AsyncSession],
    operator_id: UUID,
) -> None:
    """One window collapses a session's own changes, never two sessions into one event."""
    first, _ = await chat_store.create(operator_id, SpaSession())
    second, _ = await chat_store.create(operator_id, SpaSession())
    socket = await _tab(hub, operator_id)

    async with migrated_sessions.begin() as db:
        await notify(db, SessionEventKind.UPDATE, first.session_id)
        await notify(db, SessionEventKind.UPDATE, second.session_id)

    events = await socket.session_events(within=WINDOW * 3)
    assert {event["session_id"] for event in events} == {str(first.session_id), str(second.session_id)}


async def test_an_update_is_not_delivered_to_another_operators_tab(
    live_updates: SessionLiveUpdates,
    hub: ConsoleEventHub,
    chat_store: SessionStore,
    migrated_sessions: async_sessionmaker[AsyncSession],
    migrated_identity_store: PostgresOperatorIdentityStore,
    operator_id: UUID,
) -> None:
    """`SessionEvent` carries no operator, so the routing rests entirely on the session's row."""
    other_operator = await migrated_identity_store.resolve_configured_external_user_key("another-authentik-user-id")
    view, _ = await chat_store.create(operator_id, SpaSession())
    mine = await _tab(hub, operator_id)
    theirs = await _tab(hub, other_operator)

    async with migrated_sessions.begin() as db:
        await notify(db, SessionEventKind.UPDATE, view.session_id)

    assert len(await mine.session_events(within=WINDOW * 3)) == 1
    assert await theirs.session_events(within=timedelta(0)) == []


async def test_a_session_this_database_does_not_have_is_dropped(
    live_updates: SessionLiveUpdates,
    hub: ConsoleEventHub,
    migrated_sessions: async_sessionmaker[AsyncSession],
    operator_id: UUID,
) -> None:
    """The id arrives over a broadcast channel, so it can name a session with no owner here."""
    socket = await _tab(hub, operator_id)

    async with migrated_sessions.begin() as db:
        await notify(db, SessionEventKind.UPDATE, uuid4())

    assert await socket.session_events(within=WINDOW * 3) == []


async def test_a_failed_flush_does_not_stop_the_next_one(
    notifications: SessionNotifications,
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
    view, _ = await chat_store.create(operator_id, SpaSession())
    socket = await _tab(flaky, operator_id)
    try:
        async with SessionLiveUpdates(notifications, flaky, migrated_sessions, window=WINDOW).run():
            async with migrated_sessions.begin() as db:
                await notify(db, SessionEventKind.UPDATE, view.session_id)
            assert await socket.session_events(within=WINDOW * 3) == []

            async with migrated_sessions.begin() as db:
                await notify(db, SessionEventKind.UPDATE, view.session_id)
            assert len(await socket.session_events(within=WINDOW * 3)) == 1
    finally:
        await flaky.aclose()


if __name__ == "__main__":
    pytest_bazel.main()
