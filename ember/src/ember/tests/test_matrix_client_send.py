from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ember.matrix_client import MatrixClient


class FakeAsyncClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def room_send(self, room_id: str, message_type: str, content: dict[str, Any]) -> None:
        self.sent.append({"room_id": room_id, "message_type": message_type, "content": content})

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_matrix_client_send_text_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakeAsyncClient()

    async def fake_create_client(self: MatrixClient) -> FakeAsyncClient:  # type: ignore[override]
        return fake_client

    async def fake_initialise_control_rooms(self: MatrixClient) -> set[str]:
        return set()

    async def fake_sync_loop(self: MatrixClient) -> None:  # pragma: no cover - exercised via task
        await asyncio.sleep(0)

    monkeypatch.setattr(MatrixClient, "_create_client", fake_create_client, raising=False)
    monkeypatch.setattr(MatrixClient, "_initialise_control_rooms", fake_initialise_control_rooms, raising=False)
    monkeypatch.setattr(MatrixClient, "_sync_loop", fake_sync_loop, raising=False)

    monkeypatch.setenv("MATRIX_BASE_URL", "https://matrix.test")
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "token")

    client = MatrixClient.from_projected_secrets(
        state_store=tmp_path / "state.json",
        store_dir=tmp_path / "store",
        device_id="test-device",
        pickle_key="test-pickle",
    )

    async with client.session() as session:
        await session.send_text_message("!room:test", "integration hello")

    assert fake_client.closed is True
    assert fake_client.sent == [
        {
            "room_id": "!room:test",
            "message_type": "m.room.message",
            "content": {"msgtype": "m.notice", "body": "integration hello"},
        }
    ]
