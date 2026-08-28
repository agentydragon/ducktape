from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
import pytest_bazel
from fastapi import WebSocket
from pydantic import ValidationError

from haku.console.notifications import console_events
from haku.console.notifications.console_events import (
    ConnectionStatus,
    ConsoleEventHub,
    ConsoleHelloEvent,
    McpOperatorAuthChangedEvent,
    ToolCallsChangedEvent,
)
from haku.console.operator_identity_store import PostgresOperatorIdentityStore

OPERATOR_A = UUID("00000000-0000-0000-0000-00000000000a")
OPERATOR_B = UUID("00000000-0000-0000-0000-00000000000b")


class RecordingWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.closed = False
        self.messages: list[dict[str, Any]] = []
        self.message_received = asyncio.Event()

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self.message_received.set()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = True


class BlockingWebSocket(RecordingWebSocket):
    async def send_json(self, message: dict[str, Any]) -> None:
        await asyncio.Event().wait()


class HangingPublisher:
    def __init__(self) -> None:
        self.closed = False

    async def execute(self, query: str, *args: object) -> None:
        del query, args
        await asyncio.Event().wait()

    async def close(self, **_: object) -> None:
        self.closed = True


class ListenConnection:
    """An asyncpg connection far enough along to drive the hub's reconnect path.

    `end_notifications` terminates the connection as soon as it is listening, which is how a
    dropped listener looks from the loop's side.
    """

    def __init__(self, *, end_notifications: bool) -> None:
        self.end_notifications = end_notifications
        self.listened = asyncio.Event()
        self._terminate: Callable[[object], None] | None = None

    def add_termination_listener(self, callback: Callable[[object], None]) -> None:
        self._terminate = callback

    async def add_listener(self, channel: str, callback: object) -> None:
        assert channel == "haku_console_events"
        del callback
        self.listened.set()
        if self.end_notifications and self._terminate is not None:
            self._terminate(self)

    async def close(self, **_: object) -> None:
        """asyncpg's `close` takes a `timeout`; swallowing kwargs keeps ASYNC109 out of a fake."""


def _identity_store(*, active: bool = True) -> PostgresOperatorIdentityStore:
    store = Mock(spec=PostgresOperatorIdentityStore)
    store.is_active.return_value = active
    return cast(PostgresOperatorIdentityStore, store)


async def test_event_hub_routes_across_replicas_by_operator_id(migrated_db_url: str) -> None:
    first_hub = ConsoleEventHub(migrated_db_url, operator_identity_store=_identity_store())
    second_hub = ConsoleEventHub(migrated_db_url, operator_identity_store=_identity_store())
    operator_a_first = RecordingWebSocket()
    operator_b_first = RecordingWebSocket()
    operator_a_second = RecordingWebSocket()
    operator_b_second = RecordingWebSocket()
    try:
        await asyncio.gather(first_hub.start(), second_hub.start())
        await first_hub.connect(cast(WebSocket, operator_a_first), OPERATOR_A)
        await first_hub.connect(cast(WebSocket, operator_b_first), OPERATOR_B)
        await second_hub.connect(cast(WebSocket, operator_a_second), OPERATOR_A)
        await second_hub.connect(cast(WebSocket, operator_b_second), OPERATOR_B)

        event = McpOperatorAuthChangedEvent(server_id="grocy-sf", status=ConnectionStatus.CONNECTED)
        # start() guarantees both LISTEN subscriptions are ready, so an immediate publish reaches
        # the local replica and its peer without a test sleep or a production startup race.
        await first_hub.broadcast(OPERATOR_A, [event])
        await asyncio.wait_for(
            asyncio.gather(operator_a_first.message_received.wait(), operator_a_second.message_received.wait()),
            timeout=5,
        )

        expected = [event.model_dump(mode="json")]
        assert operator_a_first.messages == expected
        assert operator_a_second.messages == expected
        assert operator_b_first.messages == []
        assert operator_b_second.messages == []
    finally:
        await asyncio.gather(first_hub.aclose(), second_hub.aclose())


async def test_tool_call_subscription_is_scoped_and_does_not_require_a_websocket() -> None:
    hub = ConsoleEventHub("postgresql+psycopg://unused.invalid/db", operator_identity_store=_identity_store())
    event = ToolCallsChangedEvent(tool_call_id="tc_target")

    async with (
        hub.subscribe(OPERATOR_A, "tc_target") as target,
        hub.subscribe(OPERATOR_A, "tc_other") as other_call,
        hub.subscribe(OPERATOR_B, "tc_target") as other_operator,
    ):
        await hub.deliver_locally(OPERATOR_A, event)
        assert target.is_set()
        assert not other_call.is_set()
        assert not other_operator.is_set()

    assert hub._tool_call_waiters == {}


async def test_tool_call_subscription_routes_across_replicas(migrated_db_url: str) -> None:
    publishing_hub = ConsoleEventHub(migrated_db_url, operator_identity_store=_identity_store())
    waiting_hub = ConsoleEventHub(migrated_db_url, operator_identity_store=_identity_store())
    tool_call_id = "tc_cross_replica"
    try:
        await asyncio.gather(publishing_hub.start(), waiting_hub.start())
        async with waiting_hub.subscribe(OPERATOR_A, tool_call_id) as changed:
            await publishing_hub.tool_call_changed(OPERATOR_A, tool_call_id)
            await asyncio.wait_for(changed.wait(), timeout=5)
    finally:
        await asyncio.gather(publishing_hub.aclose(), waiting_hub.aclose())


async def test_successful_relisten_wakes_waiter_registered_during_reconnect_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ListenConnection(end_notifications=True)
    second = ListenConnection(end_notifications=False)
    connect = AsyncMock(side_effect=[first, second])
    reconnect_gap = asyncio.Event()
    resume_reconnect = asyncio.Event()

    async def pause_in_reconnect_gap(_: float) -> None:
        reconnect_gap.set()
        await resume_reconnect.wait()

    monkeypatch.setattr(console_events.asyncpg, "connect", connect)
    monkeypatch.setattr(console_events.asyncio, "sleep", pause_in_reconnect_gap)
    hub = ConsoleEventHub("postgresql+psycopg://unused.invalid/db", operator_identity_store=_identity_store())
    listen_task = asyncio.create_task(hub._listen_loop())
    try:
        await asyncio.wait_for(reconnect_gap.wait(), timeout=1)
        async with hub.subscribe(OPERATOR_A, "tc_reconnect_gap") as changed:
            assert not changed.is_set()
            resume_reconnect.set()
            await asyncio.wait_for(second.listened.wait(), timeout=1)
            await asyncio.wait_for(changed.wait(), timeout=1)
    finally:
        listen_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listen_task


async def test_event_hub_start_fails_bounded_when_postgres_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    connect = AsyncMock(side_effect=OSError("postgres unavailable"))
    monkeypatch.setattr(console_events.asyncpg, "connect", connect)
    monkeypatch.setattr(ConsoleEventHub, "_START_TIMEOUT_SECONDS", 0.02)
    hub = ConsoleEventHub("postgresql+psycopg://unreachable.invalid/db", operator_identity_store=_identity_store())

    with pytest.raises(RuntimeError, match="did not become ready"):
        await hub.start()

    assert hub._listen_task is None


async def test_event_hub_publish_timeout_is_lossy_not_a_request_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = ConsoleEventHub("postgresql+psycopg://unused.invalid/db", operator_identity_store=_identity_store())
    publisher = HangingPublisher()
    hub._publisher = cast(Any, publisher)
    monkeypatch.setattr(ConsoleEventHub, "_PUBLISH_TIMEOUT_SECONDS", 0.02)

    await hub.broadcast(
        OPERATOR_A, [McpOperatorAuthChangedEvent(server_id="grocy-sf", status=ConnectionStatus.CONNECTED)]
    )

    assert publisher.closed
    assert hub._publisher is None


async def test_stuck_websocket_does_not_block_other_operator_tabs(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = ConsoleEventHub("postgresql+psycopg://unused.invalid/db", operator_identity_store=_identity_store())
    hub._listening.set()
    stuck = BlockingWebSocket()
    healthy = RecordingWebSocket()
    monkeypatch.setattr(ConsoleEventHub, "_SOCKET_TIMEOUT_SECONDS", 0.02)
    await hub.connect(cast(WebSocket, stuck), OPERATOR_A)
    await hub.connect(cast(WebSocket, healthy), OPERATOR_A)

    await hub.deliver_locally(
        OPERATOR_A, McpOperatorAuthChangedEvent(server_id="grocy-sf", status=ConnectionStatus.CONNECTED)
    )

    assert healthy.messages == [
        {"event_type": "mcp_operator_auth_changed", "server_id": "grocy-sf", "status": "connected"}
    ]
    assert stuck.closed
    assert cast(WebSocket, stuck) not in hub._connections
    await hub.aclose()


async def test_disabled_operator_socket_is_closed_before_event_delivery() -> None:
    hub = ConsoleEventHub(
        "postgresql+psycopg://unused.invalid/db", operator_identity_store=_identity_store(active=False)
    )
    hub._listening.set()
    websocket = RecordingWebSocket()
    await hub.connect(cast(WebSocket, websocket), OPERATOR_A)

    await hub.deliver_locally(
        OPERATOR_A, McpOperatorAuthChangedEvent(server_id="grocy-sf", status=ConnectionStatus.CONNECTED)
    )

    assert websocket.messages == []
    assert websocket.closed
    assert cast(WebSocket, websocket) not in hub._connections
    await hub.aclose()


def test_operator_auth_event_is_pydantic_validated() -> None:
    with pytest.raises(ValidationError):
        McpOperatorAuthChangedEvent.model_validate(
            {"event_type": "mcp_operator_auth_changed", "server_id": "grocy-sf", "status": "unknown"}
        )


def test_a_field_a_later_release_adds_does_not_cost_the_previous_one_the_event() -> None:
    """These envelopes cross replicas, which during a roll run different releases. Refusing an
    unknown field would make the release that adds one drop every invalidation the previous image
    is owed — including on the kinds it does understand."""
    event = McpOperatorAuthChangedEvent.model_validate(
        {
            "event_type": "mcp_operator_auth_changed",
            "server_id": "grocy-sf",
            "status": "connected",
            "reauthorized_at": "2026-08-18T00:00:00Z",
        }
    )

    assert (event.server_id, event.status) == ("grocy-sf", "connected")


def test_console_hello_event_is_a_pydantic_shape() -> None:
    assert ConsoleHelloEvent().model_dump(mode="json") == {"event_type": "hello"}


if __name__ == "__main__":
    pytest_bazel.main()
