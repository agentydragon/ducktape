"""Exercise Go sidecar process readiness and SIGTERM shutdown."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_bazel

from agentplane.egress.sidecar_config import READINESS_PATH, Proxy, SpiffeProxyAuth, TLSProxyTransport
from agentplane.egress.testing.sidecar_process import SidecarProcess
from agentplane.environment_runtime_transport.config import EGRESS_PROXY_URI
from agentplane.testing.tls import (
    CertificateAuthority,
    issue_workload_leaf,
    make_ca,
    server_tls_context,
    workload_uri,
    write_ca,
)
from util.net import Listener, listen_tcp


@pytest.fixture
def ca() -> CertificateAuthority:
    return make_ca("agentplane-egress-sidecar-test")


@pytest.fixture
async def central(ca: CertificateAuthority, tmp_path: Path) -> AsyncIterator[Listener]:
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

    directory = tmp_path / "central"
    directory.mkdir()
    security = server_tls_context(*issue_workload_leaf(ca, EGRESS_PROXY_URI, directory))
    # The sidecar's hop requires the proxy to ask for its SVID, as the real one does.
    security.verify_mode = ssl.CERT_REQUIRED
    security.load_verify_locations(cadata=ca.cert_pem.decode())
    with listen_tcp() as listener:
        server = await asyncio.start_server(handle, sock=listener.socket, ssl=security)
        async with server:
            yield listener


@pytest.fixture
def proxy_config(central: Listener, ca: CertificateAuthority, tmp_path: Path) -> Proxy:
    directory = tmp_path / "sidecar"
    directory.mkdir()
    cert_file, key_file = issue_workload_leaf(ca, workload_uri("test-ns", "test-sandbox", "credential-id"), directory)
    return Proxy(
        address=f"{central.host}:{central.port}",
        transport=TLSProxyTransport(
            ca_file=write_ca(ca, tmp_path, "root")[0], auth=SpiffeProxyAuth(cert_file=cert_file, key_file=key_file)
        ),
    )


async def get_readiness(listener: Listener, method: str = "GET") -> int:
    async with (
        aiohttp.ClientSession() as session,
        session.request(method, f"http://{listener.host}:{listener.port}{READINESS_PATH}") as response,
    ):
        return response.status


async def test_readiness_tracks_listener_startup_and_shutdown(proxy_config: Proxy) -> None:
    relay = SidecarProcess(proxy=proxy_config)
    async with relay:
        assert await get_readiness(relay.readiness_listener) == 200
        assert await get_readiness(relay.readiness_listener, "HEAD") == 200
    with pytest.raises(aiohttp.ClientError):
        await get_readiness(relay.readiness_listener)


async def test_sigterm_shutdown_closes_an_active_tunnel(proxy_config: Proxy) -> None:
    relay = SidecarProcess(proxy=proxy_config)
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
