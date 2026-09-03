"""The relay against a fake central proxy that records what reached it: the token header on every
hop, a CONNECT piped both ways, the token re-read between requests, and a refusal with nothing
forwarded when the token file is missing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import pytest
import pytest_bazel
from more_itertools import one

from x.agentplane.egress.sidecar import REFUSED_HEADER, RefusalReason, SidecarRelay

REFUSED_HOST = "refused.test"
CONNECT_REFUSAL_HEADER = "x-agentplane-egress"


@dataclass(frozen=True)
class SeenRequest:
    method: str
    target: str
    headers: dict[str, str]  # names lowercased, last value wins
    header_lines: list[str]  # verbatim, so a duplicated name is visible

    @property
    def token(self) -> str | None:
        scheme, _, token = self.headers.get("proxy-authorization", "").partition(" ")
        return token if scheme == "Bearer" else None


@dataclass
class FakeCentralProxy:
    """Records every request head. A CONNECT to any host but REFUSED_HOST is answered 200 and then
    echoed back byte for byte; a plain request is answered with the token it carried."""

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
async def relay(central: FakeCentralProxy, token_file: Path) -> AsyncIterator[SidecarRelay]:
    async with SidecarRelay(proxy_host="127.0.0.1", proxy_port=central.port, token_file=token_file) as relay:
        yield relay


async def get_through(relay: SidecarRelay, path: str, headers: dict[str, str] | None = None) -> tuple[int, str, str]:
    """`(status, body, sidecar refusal header)` of a plain proxied GET."""
    async with (
        aiohttp.ClientSession() as session,
        session.get(
            f"http://upstream.test{path}", proxy=f"http://127.0.0.1:{relay.listen_port}", proxy_headers=headers
        ) as response,
    ):
        return response.status, await response.text(), response.headers.get(REFUSED_HEADER, "")


async def test_plain_request_carries_the_token(relay: SidecarRelay, central: FakeCentralProxy) -> None:
    status, body, _ = await get_through(relay, "/path?q=1", headers={"Proxy-Authorization": "Bearer forged"})
    assert (status, body) == (200, "seen-token=token-1")
    seen = one(central.seen)
    # Absolute-form target as the client sent it; the client's own token header replaced, not added to.
    assert (seen.method, seen.target, seen.token) == ("GET", "http://upstream.test/path?q=1", "token-1")
    assert [line for line in seen.header_lines if line.lower().startswith("proxy-authorization:")] == [
        "Proxy-Authorization: Bearer token-1"
    ]
    assert seen.headers["connection"] == "close"


async def test_token_is_reread_on_every_request(
    relay: SidecarRelay, central: FakeCentralProxy, token_file: Path
) -> None:
    await get_through(relay, "/one")
    await asyncio.to_thread(token_file.write_text, "token-2")
    await get_through(relay, "/two")
    assert [seen.token for seen in central.seen] == ["token-1", "token-2"]


async def test_missing_token_file_refuses_without_forwarding(central: FakeCentralProxy, tmp_path: Path) -> None:
    async with SidecarRelay(proxy_host="127.0.0.1", proxy_port=central.port, token_file=tmp_path / "absent") as relay:
        status, _, refusal = await get_through(relay, "/path")
    assert (status, refusal) == (503, f"reason={RefusalReason.TOKEN_UNAVAILABLE}")
    assert central.connections == 0


async def test_unreachable_central_proxy_refuses(tmp_path: Path, token_file: Path) -> None:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    unused_port = one(server.sockets).getsockname()[1]
    server.close()
    await server.wait_closed()
    async with SidecarRelay(proxy_host="127.0.0.1", proxy_port=unused_port, token_file=token_file) as relay:
        status, _, refusal = await get_through(relay, "/path")
    assert (status, refusal) == (502, f"reason={RefusalReason.PROXY_UNREACHABLE}")


async def connect_through(relay: SidecarRelay, host: str) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a CONNECT tunnel; returns the central proxy's response head as relayed, and the streams."""
    reader, writer = await asyncio.open_connection("127.0.0.1", relay.listen_port)
    writer.write(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\nProxy-Connection: Keep-Alive\r\n\r\n".encode())
    await writer.drain()
    return await reader.readuntil(b"\r\n\r\n"), reader, writer


async def test_connect_is_authenticated_then_piped_both_ways(relay: SidecarRelay, central: FakeCentralProxy) -> None:
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


async def test_connect_refusal_is_relayed_verbatim(relay: SidecarRelay, central: FakeCentralProxy) -> None:
    head, reader, writer = await connect_through(relay, REFUSED_HOST)
    assert head == f"HTTP/1.1 403 Forbidden\r\n{CONNECT_REFUSAL_HEADER}: denied; reason=no-rule\r\n\r\n".encode()
    assert await reader.read() == b""  # no tunnel: the relay ends with the refusal
    writer.close()
    await writer.wait_closed()


async def test_malformed_request_is_refused(relay: SidecarRelay, central: FakeCentralProxy) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", relay.listen_port)
    writer.write(b"not http at all\r\n\r\n")
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 400 ")
    assert f"{REFUSED_HEADER}: reason={RefusalReason.BAD_REQUEST}".encode() in head
    assert central.connections == 0
    writer.close()
    await writer.wait_closed()


if __name__ == "__main__":
    pytest_bazel.main()
