from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
import pytest_bazel
from fastapi import WebSocket
from pydantic import ValidationError

from haku.console import console_events
from haku.console.console_events import ConsoleEventHub, ConsoleHelloEvent, McpOperatorAuthChangedEvent
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

    async def execute(self, query: str, params: Any) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


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

        event = McpOperatorAuthChangedEvent(server_id="grocy-sf", status="connected")
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


async def test_event_hub_start_fails_bounded_when_postgres_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    connect = AsyncMock(side_effect=OSError("postgres unavailable"))
    monkeypatch.setattr(console_events.psycopg.AsyncConnection, "connect", connect)
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

    await hub.broadcast(OPERATOR_A, [McpOperatorAuthChangedEvent(server_id="grocy-sf", status="connected")])

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

    await hub._deliver_locally(OPERATOR_A, McpOperatorAuthChangedEvent(server_id="grocy-sf", status="connected"))

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

    await hub._deliver_locally(OPERATOR_A, McpOperatorAuthChangedEvent(server_id="grocy-sf", status="connected"))

    assert websocket.messages == []
    assert websocket.closed
    assert cast(WebSocket, websocket) not in hub._connections
    await hub.aclose()


def test_operator_auth_event_is_pydantic_validated() -> None:
    with pytest.raises(ValidationError):
        McpOperatorAuthChangedEvent.model_validate(
            {"event_type": "mcp_operator_auth_changed", "server_id": "grocy-sf", "status": "unknown"}
        )
    with pytest.raises(ValidationError):
        McpOperatorAuthChangedEvent.model_validate(
            {
                "event_type": "mcp_operator_auth_changed",
                "server_id": "grocy-sf",
                "status": "connected",
                "unexpected": True,
            }
        )


def test_console_hello_event_is_a_pydantic_shape() -> None:
    assert ConsoleHelloEvent().model_dump(mode="json") == {"event_type": "hello"}


if __name__ == "__main__":
    pytest_bazel.main()
