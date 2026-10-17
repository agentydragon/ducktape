"""The Go binary against a fake central proxy: auth replacement, rotating tokens, CONNECT and shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import pytest
import pytest_bazel
from more_itertools import one

from agentplane.egress.sidecar_config import PROJECTED_TOKEN_HEADER, READINESS_PATH, REFUSED_HEADER
from agentplane.egress.testing.sidecar_process import SidecarProcess

REFUSED_HOST = "refused.test"
CONNECT_REFUSAL_HEADER = "x-agentplane-egress"


@dataclass(frozen=True)
class SeenRequest:
    method: str
    target: str
    headers: dict[str, str]
    header_lines: list[str]

    @property
    def token(self) -> str | None:
        scheme, _, token = self.headers.get("proxy-authorization", "").partition(" ")
        return token if scheme == "Bearer" else None

    @property
    def projected(self) -> list[str]:
        prefix = f"{PROJECTED_TOKEN_HEADER.lower()}:"
        return [line.split(":", 1)[1].strip() for line in self.header_lines if line.lower().startswith(prefix)]


@dataclass
class FakeCentralProxy:
    port: int = 0
    connections: int = 0
    seen: list[SeenRequest] = field(default_factory=list)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        request_line, *header_lines = head[:-4].split("\r\n")
        method, target, _ = request_line.split(" ")
        headers = {name.strip().lower(): value.strip() for name, value in (line.split(":", 1) for line in header_lines)}
        request = SeenRequest(method, target, headers, header_lines)
        self.seen.append(request)
        if method == "CONNECT" and target.startswith(REFUSED_HOST):
            writer.write(f"HTTP/1.1 403 Forbidden\r\n{CONNECT_REFUSAL_HEADER}: denied; reason=no-rule\r\n\r\n".encode())
        elif method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            while data := await reader.read(65536):
                writer.write(b"echo:" + data)
                await writer.drain()
        else:
            body = f"seen-token={request.token}".encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
        await writer.drain()
        writer.close()


@pytest.fixture
async def central() -> AsyncIterator[FakeCentralProxy]:
    central = FakeCentralProxy()
    server = await asyncio.start_server(central.handle, "127.0.0.1", 0)
    central.port = one(server.sockets).getsockname()[1]
    async with server:
        yield central


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text("token-1\n")
    return path


@pytest.fixture
async def relay(central: FakeCentralProxy, token_file: Path) -> AsyncIterator[SidecarProcess]:
    async with SidecarProcess(proxy_host="127.0.0.1", proxy_port=central.port, token_file=token_file) as relay:
        yield relay


async def get_through(relay: SidecarProcess, path: str, headers: dict[str, str] | None = None) -> tuple[int, str, str]:
    async with (
        aiohttp.ClientSession() as session,
        session.get(
            f"http://upstream.test{path}", proxy=f"http://127.0.0.1:{relay.listen_port}", proxy_headers=headers
        ) as response,
    ):
        return response.status, await response.text(), response.headers.get(REFUSED_HEADER, "")


async def get_readiness(port: int, method: str = "GET") -> int:
    async with (
        aiohttp.ClientSession() as session,
        session.request(method, f"http://127.0.0.1:{port}{READINESS_PATH}") as response,
    ):
        return response.status


async def test_readiness_tracks_listener_startup_and_shutdown(central: FakeCentralProxy, token_file: Path) -> None:
    relay = SidecarProcess(proxy_host="127.0.0.1", proxy_port=central.port, token_file=token_file)
    async with relay:
        assert await get_readiness(relay.readiness_port) == 200
        assert await get_readiness(relay.readiness_port, "HEAD") == 200
    with pytest.raises(aiohttp.ClientError):
        await get_readiness(relay.readiness_port)


async def test_failed_listener_start_never_reports_ready(central: FakeCentralProxy, token_file: Path) -> None:
    occupied = await asyncio.start_server(lambda reader, writer: None, "127.0.0.1", 0)
    listen_port = one(occupied.sockets).getsockname()[1]
    relay = SidecarProcess(
        proxy_host="127.0.0.1", proxy_port=central.port, token_file=token_file, listen_port=listen_port
    )
    try:
        with pytest.raises(RuntimeError, match="address already in use"):
            await relay.__aenter__()
    finally:
        occupied.close()
        await occupied.wait_closed()


async def test_plain_request_carries_the_token(relay: SidecarProcess, central: FakeCentralProxy) -> None:
    status, body, _ = await get_through(relay, "/path?q=1", headers={"Proxy-Authorization": "Bearer forged"})
    assert (status, body) == (200, "seen-token=token-1")
    seen = one(central.seen)
    assert (seen.method, seen.target, seen.token) == ("GET", "http://upstream.test/path?q=1", "token-1")
    assert [line for line in seen.header_lines if line.lower().startswith("proxy-authorization:")] == [
        "Proxy-Authorization: Bearer token-1"
    ]
    assert seen.headers["connection"] == "close"


async def test_token_is_reread_on_every_request(
    relay: SidecarProcess, central: FakeCentralProxy, token_file: Path
) -> None:
    await get_through(relay, "/one")
    await asyncio.to_thread(token_file.write_text, "token-2")
    await get_through(relay, "/two")
    assert [seen.token for seen in central.seen] == ["token-1", "token-2"]


async def test_missing_token_file_refuses_without_forwarding(central: FakeCentralProxy, tmp_path: Path) -> None:
    async with SidecarProcess(proxy_host="127.0.0.1", proxy_port=central.port, token_file=tmp_path / "absent") as relay:
        status, _, refusal = await get_through(relay, "/path")
    assert (status, refusal) == (503, "reason=token-unavailable")
    assert central.connections == 0


async def test_unreachable_central_proxy_refuses(tmp_path: Path, token_file: Path) -> None:
    server = await asyncio.start_server(lambda reader, writer: None, "127.0.0.1", 0)
    unused_port = one(server.sockets).getsockname()[1]
    server.close()
    await server.wait_closed()
    async with SidecarProcess(proxy_host="127.0.0.1", proxy_port=unused_port, token_file=token_file) as relay:
        status, _, refusal = await get_through(relay, "/path")
    assert (status, refusal) == (502, "reason=proxy-unreachable")


async def connect_through(relay: SidecarProcess, host: str) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", relay.listen_port)
    writer.write(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\nProxy-Connection: Keep-Alive\r\n\r\n".encode())
    await writer.drain()
    return await reader.readuntil(b"\r\n\r\n"), reader, writer


async def test_connect_is_authenticated_then_piped_both_ways(relay: SidecarProcess, central: FakeCentralProxy) -> None:
    head, reader, writer = await connect_through(relay, "tunnel.test")
    assert head == b"HTTP/1.1 200 Connection established\r\n\r\n"
    seen = one(central.seen)
    assert (seen.method, seen.target, seen.token) == ("CONNECT", "tunnel.test:443", "token-1")
    assert "proxy-connection" not in seen.headers
    for payload in (b"ping", b"pong"):
        writer.write(payload)
        await writer.drain()
        assert await reader.readexactly(len(b"echo:") + len(payload)) == b"echo:" + payload
    writer.close()
    await writer.wait_closed()


async def test_connect_refusal_is_relayed_verbatim(relay: SidecarProcess) -> None:
    head, reader, writer = await connect_through(relay, REFUSED_HOST)
    assert head == f"HTTP/1.1 403 Forbidden\r\n{CONNECT_REFUSAL_HEADER}: denied; reason=no-rule\r\n\r\n".encode()
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()


async def test_malformed_request_is_refused(relay: SidecarProcess, central: FakeCentralProxy) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", relay.listen_port)
    writer.write(b"not http at all\r\n\r\n")
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 400 ")
    assert f"{REFUSED_HEADER}: reason=bad-request".encode() in head
    assert central.connections == 0
    writer.close()
    await writer.wait_closed()


AUDIENCE = "https://kubernetes.test.invalid"


@pytest.fixture
def audience_file(tmp_path: Path) -> Path:
    path = tmp_path / "kubernetes-token"
    path.write_text("api-server-token-1\n")
    return path


@pytest.fixture
async def audience_relay(
    central: FakeCentralProxy, token_file: Path, audience_file: Path
) -> AsyncIterator[SidecarProcess]:
    async with SidecarProcess(
        proxy_host="127.0.0.1",
        proxy_port=central.port,
        token_file=token_file,
        audience_token_files={AUDIENCE: audience_file},
    ) as relay:
        yield relay


async def test_a_configured_audience_is_offered_beside_the_hop_token(
    audience_relay: SidecarProcess, central: FakeCentralProxy
) -> None:
    await get_through(audience_relay, "/path")
    seen = one(central.seen)
    assert (seen.token, seen.projected) == ("token-1", [f"{AUDIENCE} api-server-token-1"])


async def test_a_clients_own_projected_header_is_never_forwarded(
    audience_relay: SidecarProcess, central: FakeCentralProxy
) -> None:
    await get_through(
        audience_relay, "/path", headers={PROJECTED_TOKEN_HEADER: f"{AUDIENCE} stolen-token-of-another-pod"}
    )
    assert one(central.seen).projected == [f"{AUDIENCE} api-server-token-1"]


async def test_a_projected_token_is_reread_between_requests(
    audience_relay: SidecarProcess, central: FakeCentralProxy, audience_file: Path
) -> None:
    await get_through(audience_relay, "/one")
    await asyncio.to_thread(audience_file.write_text, "api-server-token-2")
    await get_through(audience_relay, "/two")
    assert [seen.projected for seen in central.seen] == [
        [f"{AUDIENCE} api-server-token-1"],
        [f"{AUDIENCE} api-server-token-2"],
    ]


async def test_an_unreadable_audience_is_left_out_rather_than_offered_blank(
    central: FakeCentralProxy, token_file: Path, tmp_path: Path
) -> None:
    async with SidecarProcess(
        proxy_host="127.0.0.1",
        proxy_port=central.port,
        token_file=token_file,
        audience_token_files={AUDIENCE: tmp_path / "absent"},
    ) as relay:
        status, _, _ = await get_through(relay, "/path")
    assert status == 200
    assert one(central.seen).projected == []


async def test_a_connect_carries_the_projected_token(audience_relay: SidecarProcess, central: FakeCentralProxy) -> None:
    _, _, writer = await connect_through(audience_relay, "tunnel.test")
    assert one(central.seen).projected == [f"{AUDIENCE} api-server-token-1"]
    writer.close()
    await writer.wait_closed()


async def test_shutdown_closes_an_active_tunnel(central: FakeCentralProxy, token_file: Path) -> None:
    relay = SidecarProcess(proxy_host="127.0.0.1", proxy_port=central.port, token_file=token_file)
    async with relay:
        head, reader, writer = await connect_through(relay, "tunnel.test")
        assert head == b"HTTP/1.1 200 Connection established\r\n\r\n"
    assert await asyncio.wait_for(reader.read(), timeout=2) == b""
    writer.close()
    await writer.wait_closed()


if __name__ == "__main__":
    pytest_bazel.main()
