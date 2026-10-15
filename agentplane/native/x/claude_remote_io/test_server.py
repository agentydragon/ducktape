"""Conformance of the experimental peer, independent of the real-CLI interop tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
from aiohttp import ClientResponse
from aiohttp.test_utils import TestClient, TestServer

from agentplane.native.x.claude_remote_io.server import SESSION_PATH, TEST_TOKEN, RemoteIOServer

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
async def peer(tmp_path: Path) -> AsyncIterator[tuple[RemoteIOServer, TestClient]]:
    server = RemoteIOServer(tmp_path / "trace.jsonl")
    async with TestClient(TestServer(server.app)) as client:
        try:
            yield server, client
        finally:
            # Wake held SSE requests before aiohttp waits for handlers to exit.
            await server.close()


async def event(response: ClientResponse) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while line := await response.content.readline():
            if line.startswith(b"data: "):
                value = json.loads(line.removeprefix(b"data: "))
                assert isinstance(value, dict)
                return value
    raise AssertionError("stream closed without an event")


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", TEST_TOKEN])
async def test_rejects_unauthenticated_upload_without_recording_it(peer, authorization: str | None) -> None:
    server, client = peer
    headers = {} if authorization is None else {"Authorization": authorization}
    async with client.post(SESSION_PATH + "/worker/events", headers=headers, json={"worker_epoch": 1}) as response:
        assert response.status == 401
    assert server.uploads == []
    assert not server.trace.exists()


@pytest.mark.parametrize("path", ["/v1/code/sessions/other/worker", SESSION_PATH + "/unknown"])
async def test_rejects_other_sessions_and_unknown_routes(peer, path: str) -> None:
    server, client = peer
    async with client.get(path, headers=AUTH) as response:
        assert response.status == 404
    assert server.uploads == []


@pytest.mark.parametrize("body", ["not-json", "[]", "null"])
async def test_rejects_malformed_uploads(peer, body: str) -> None:
    server, client = peer
    async with client.post(SESSION_PATH + "/worker/events", headers=AUTH, data=body) as response:
        assert response.status == 400
    assert server.uploads == []


async def test_epoch_rejects_stale_uploads_and_retains_accepted_evidence(peer) -> None:
    server, client = peer
    path = SESSION_PATH + "/worker/events"
    async with client.post(path, headers=AUTH, json={"worker_epoch": 1, "events": []}) as response:
        assert response.status == 200
    await server.send({"type": "user", "uuid": "settled"})
    await server.begin_incarnation()
    assert server.commands == []
    async with client.post(SESSION_PATH + "/worker/register", headers=AUTH) as response:
        assert await response.json() == {"worker_epoch": 2}
    async with client.post(path, headers=AUTH, json={"worker_epoch": 1, "events": []}) as response:
        assert response.status == 409
        assert await response.json() == {"reason": "worker_epoch_mismatch"}
    async with client.post(path, headers=AUTH, json={"worker_epoch": 2, "events": []}) as response:
        assert response.status == 200
    assert [upload["body"]["worker_epoch"] for upload in server.uploads] == [1, 2]
    assert TEST_TOKEN not in server.trace.read_text()


@pytest.mark.parametrize("cursor", ["-1", "nonsense"])
async def test_invalid_cursor_is_rejected_before_stream_admission(peer, cursor: str) -> None:
    server, client = peer
    async with client.get(
        SESSION_PATH + "/worker/events/stream", headers=AUTH, params={"from_sequence_num": cursor}
    ) as response:
        assert response.status == 400
    assert server.stream_cursors == []


async def test_resume_cursor_replays_original_envelopes_and_new_events(peer) -> None:
    server, client = peer
    await server.send({"type": "user", "uuid": "first"})
    second_id = await server.send({"type": "user", "uuid": "second"})
    path = SESSION_PATH + "/worker/events/stream"
    async with client.get(path, headers={**AUTH, "Last-Event-ID": "1"}) as response:
        assert response.status == 200
        second = await event(response)
        assert second["event_id"] == second_id
        assert second["sequence_num"] == 2
        third_id = await server.send({"type": "user", "uuid": "third"})
        third = await event(response)
        assert third["event_id"] == third_id
        assert third["sequence_num"] == 3
    # A repeated cursor is an explicit replay, not a new command envelope. The
    # query cursor takes precedence over Last-Event-ID, as in the CLI peer.
    async with client.get(path, headers={**AUTH, "Last-Event-ID": "3"}, params={"from_sequence_num": "1"}) as response:
        assert await event(response) == second
        assert await event(response) == third
    assert server.stream_cursors == [1, 1]


async def test_empty_history_does_not_echo_uploaded_transcript(peer) -> None:
    server, client = peer
    async with client.post(
        SESSION_PATH + "/worker/internal-events", headers=AUTH, json={"worker_epoch": 1, "events": [{"text": "past"}]}
    ) as response:
        assert response.status == 200
    async with client.get(SESSION_PATH + "/worker/internal-events", headers=AUTH) as response:
        assert await response.json() == {"data": []}
    assert len(server.uploads) == 1


if __name__ == "__main__":
    pytest_bazel.main()
