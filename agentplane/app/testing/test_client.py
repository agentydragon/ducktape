"""`Client.events` follows a Thread across the app going away mid-stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict

from agentplane.app.testing import client as client_module
from agentplane.app.testing.client import Client, SessionStreamError
from agentplane.proto import event_log_pb2

# A server answers one connection per call: `(after, writer)`. Raw sockets, because what is under
# test is how the client reads a connection that dies, which an ASGI transport cannot produce.
Connection = Callable[[int, asyncio.StreamWriter], Awaitable[None]]


def _frame(name: str, data: dict[str, object], *, cursor: int | None = None) -> bytes:
    ident = f"id: {cursor}\n" if cursor is not None else ""
    return f"{ident}event: {name}\ndata: {json.dumps(data)}\n\n".encode()


def _entry(cursor: int) -> bytes:
    return _frame("event", MessageToDict(event_log_pb2.EventEntry(cursor=cursor)), cursor=cursor)


async def _respond(writer: asyncio.StreamWriter, status: str, body: bytes = b"") -> None:
    writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n".encode() + body)
    await writer.drain()


@asynccontextmanager
async def _app(*connections: Connection, port: int = 0) -> AsyncIterator[tuple[Client, asyncio.Server]]:
    remaining = list(connections)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = (await reader.readuntil(b"\r\n\r\n")).decode()
        after = int(request.split("after=")[1].split(" ")[0].split("&")[0])
        try:
            await remaining.pop(0)(after, writer)
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", port)
    bound = server.sockets[0].getsockname()[1]
    async with server, Client(base_url=f"http://127.0.0.1:{bound}", token="test-token") as client:
        yield client, server


@pytest.fixture(autouse=True)
def quick_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "RECONNECT_PAUSE_SECONDS", 0.01)
    monkeypatch.setattr(client_module, "RECONNECT_SECONDS", 0.5)


async def _cursors(client: Client, after: int = 0) -> list[int]:
    return [entry.cursor async for entry in client.events(uuid4(), after=after, read_seconds=5)]


async def _first_two_then_close(after: int, writer: asyncio.StreamWriter) -> None:
    await _respond(writer, "200 OK", _entry(after + 1) + _entry(after + 2))


async def _rest_then_end(after: int, writer: asyncio.StreamWriter) -> None:
    await _respond(writer, "200 OK", _entry(after + 1) + _frame("end", {}))


async def _unavailable(after: int, writer: asyncio.StreamWriter) -> None:
    await _respond(writer, "503 Service Unavailable")


async def test_resumes_after_the_last_entry_when_the_stream_closes_without_an_end_frame() -> None:
    async with _app(_first_two_then_close, _rest_then_end) as (client, _):
        assert await _cursors(client) == [1, 2, 3]


async def _forbidden(after: int, writer: asyncio.StreamWriter) -> None:
    await _respond(writer, "403 Forbidden")


async def test_waits_out_a_rollout_that_has_no_replica_up() -> None:
    async with _app(_first_two_then_close, _unavailable, _forbidden, _unavailable, _rest_then_end) as (client, _):
        assert await _cursors(client) == [1, 2, 3]


async def test_resumes_after_a_connection_reset_mid_frame() -> None:
    async def torn(after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(writer, "200 OK", _entry(after + 1) + b"event: event\ndata: {")
        writer.transport.abort()

    async with _app(torn, _rest_then_end) as (client, _):
        assert await _cursors(client) == [1, 2]


async def test_resumes_after_connection_refused_while_no_replica_listens() -> None:
    async with _app(_first_two_then_close) as (client, server):
        events = client.events(uuid4(), after=0, read_seconds=5)
        assert [(await anext(events)).cursor, (await anext(events)).cursor] == [1, 2]
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        reopened = asyncio.create_task(_serve_again(port))
        try:
            assert [entry.cursor async for entry in events] == [3]
        finally:
            (await reopened).close()


async def _serve_again(port: int) -> asyncio.Server:
    """Listen on `port` again after the client has had a connection refused."""
    await asyncio.sleep(0.1)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = (await reader.readuntil(b"\r\n\r\n")).decode()
        await _rest_then_end(int(request.split("after=")[1].split(" ")[0].split("&")[0]), writer)
        writer.close()

    return await asyncio.start_server(serve, "127.0.0.1", port)


async def test_an_outage_with_no_entries_is_reported() -> None:
    async with _app(*[_unavailable] * 100) as (client, _):
        with pytest.raises(SessionStreamError):
            await _cursors(client)


async def test_a_client_error_is_not_retried() -> None:
    async def conflict(after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(writer, "409 Conflict")

    async with _app(conflict) as (client, _):
        with pytest.raises(Exception, match="409"):
            await _cursors(client)


if __name__ == "__main__":
    pytest_bazel.main()
