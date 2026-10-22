"""Readiness through the real HTTP transport, including a prematurely closed connection."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_bazel

from agentplane.app.testing.http2_proxy import _wait_for_frontend


@asynccontextmanager
async def _health_server(*responses: bytes) -> AsyncIterator[tuple[str, list[bytes]]]:
    requests: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            # An empty response closes the socket without sending HTTP headers, as Docker's
            # published port sometimes does before the frontend has started.
            writer.write(responses[min(len(requests), len(responses)) - 1])
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", requests


async def test_waits_for_health_after_connection_closes_before_response_headers() -> None:
    async with _health_server(b"", b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n") as (
        upstream,
        requests,
    ):
        await _wait_for_frontend(upstream)
    assert len(requests) == 2
    assert all(request.startswith(b"GET /healthz HTTP/1.1\r\n") for request in requests)


async def test_health_http_error_is_not_retried() -> None:
    async with _health_server(
        b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    ) as (upstream, requests):
        with pytest.raises(httpx.HTTPStatusError) as error:
            await _wait_for_frontend(upstream)
    assert error.value.response.status_code == 503
    assert len(requests) == 1


if __name__ == "__main__":
    pytest_bazel.main()
