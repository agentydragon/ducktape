"""Exercise Go sidecar process readiness and SIGTERM shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_bazel

from agentplane.egress.sidecar_config import READINESS_PATH
from agentplane.egress.testing.sidecar_process import SidecarProcess
from util.net import Listener, listen_tcp


@pytest.fixture
async def central() -> AsyncIterator[Listener]:
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

    with listen_tcp() as listener:
        server = await asyncio.start_server(handle, sock=listener.socket)
        async with server:
            yield listener


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text("token-1\n")
    return path


async def get_readiness(listener: Listener, method: str = "GET") -> int:
    async with (
        aiohttp.ClientSession() as session,
        session.request(method, f"http://{listener.host}:{listener.port}{READINESS_PATH}") as response,
    ):
        return response.status


async def test_readiness_tracks_listener_startup_and_shutdown(central: Listener, token_file: Path) -> None:
    relay = SidecarProcess(proxy_host=central.host, proxy_port=central.port, token_file=token_file)
    async with relay:
        assert await get_readiness(relay.readiness_listener) == 200
        assert await get_readiness(relay.readiness_listener, "HEAD") == 200
    with pytest.raises(aiohttp.ClientError):
        await get_readiness(relay.readiness_listener)


async def test_sigterm_shutdown_closes_an_active_tunnel(central: Listener, token_file: Path) -> None:
    relay = SidecarProcess(proxy_host=central.host, proxy_port=central.port, token_file=token_file)
    async with relay:
        reader, writer = await asyncio.open_connection(relay.proxy_listener.host, relay.proxy_listener.port)
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
