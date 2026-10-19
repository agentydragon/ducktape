"""Exercise Go sidecar process readiness, bind failure, and SIGTERM shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_bazel

from agentplane.egress.sidecar_config import READINESS_PATH
from agentplane.egress.testing.sidecar_process import SidecarProcess


@pytest.fixture
async def central() -> AsyncIterator[int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            while payload := await reader.read(65536):
                writer.write(b"echo:" + payload)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    assert server.sockets
    socket = server.sockets[0]
    async with server:
        yield socket.getsockname()[1]


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text("token-1\n")
    return path


async def get_readiness(port: int, method: str = "GET") -> int:
    async with (
        aiohttp.ClientSession() as session,
        session.request(method, f"http://127.0.0.1:{port}{READINESS_PATH}") as response,
    ):
        return response.status


async def test_readiness_tracks_listener_startup_and_shutdown(central: int, token_file: Path) -> None:
    relay = SidecarProcess(proxy_host="127.0.0.1", proxy_port=central, token_file=token_file)
    async with relay:
        assert await get_readiness(relay.readiness_port) == 200
        assert await get_readiness(relay.readiness_port, "HEAD") == 200
    with pytest.raises(aiohttp.ClientError):
        await get_readiness(relay.readiness_port)


async def test_failed_listener_start_never_reports_ready(central: int, token_file: Path) -> None:
    occupied = await asyncio.start_server(lambda reader, writer: None, "127.0.0.1", 0)
    assert occupied.sockets
    listen_port = occupied.sockets[0].getsockname()[1]
    relay = SidecarProcess(proxy_host="127.0.0.1", proxy_port=central, token_file=token_file, listen_port=listen_port)
    try:
        with pytest.raises(RuntimeError, match="address already in use"):
            await relay.__aenter__()
    finally:
        occupied.close()
        await occupied.wait_closed()


async def test_sigterm_shutdown_closes_an_active_tunnel(central: int, token_file: Path) -> None:
    relay = SidecarProcess(proxy_host="127.0.0.1", proxy_port=central, token_file=token_file)
    async with relay:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.listen_port)
        writer.write(
            b"CONNECT tunnel.test:443 HTTP/1.1\r\nHost: tunnel.test:443\r\nProxy-Connection: Keep-Alive\r\n\r\n"
        )
        await writer.drain()
        assert await reader.readuntil(b"\r\n\r\n") == b"HTTP/1.1 200 Connection established\r\n\r\n"
    assert await asyncio.wait_for(reader.read(), timeout=2) == b""
    writer.close()
    await writer.wait_closed()


if __name__ == "__main__":
    pytest_bazel.main()
