"""Fail-closed conformance tests for the embedded egress proxy.

Every test drives a real client through a real in-process mitmproxy toward a
local recording upstream. "Fail closed" is asserted as both halves: the client
gets a refusal AND the upstream sees no TCP connection at all.

Plain HTTP (absolute-form proxying) keeps TLS trust out of the setup; the
CONNECT tests cover the tunnel path without the MITM CA because refusal happens
before any TLS.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import aiohttp
import pytest
import pytest_bazel
from more_itertools import one

from haku.egress.addon import DEFAULT_DECIDE_TIMEOUT_SECONDS
from haku.egress.decide_client import DecideClient
from haku.egress.decision import AllowDecision, Decision, DenyDecision, PlaceholderSubstitution, RequestMeta
from haku.egress.runner import EgressProxy
from haku.egress.static_decide_client import StaticDecideClient

PLACEHOLDER = "proxy-github-placeholder"
REAL_CREDENTIAL = "real-redeemed-credential"


def allow_with_substitution() -> AllowDecision:
    return AllowDecision(
        substitutions=[
            PlaceholderSubstitution(
                placeholder=PLACEHOLDER, value=REAL_CREDENTIAL, match_headers=frozenset({"Authorization"})
            )
        ]
    )


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]  # names lowercased: assertions stay case-insensitive


@dataclass
class RecordingUpstream:
    """Minimal HTTP/1.1 upstream tracking TCP connections separately from requests.

    A TCP connection without a request is already a fail-open leak (the eager
    connection strategy produces exactly that), so tests assert on both.
    """

    port: int
    connections: int = 0
    requests: list[RecordedRequest] = field(default_factory=list)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return  # connection opened but no complete request arrived
        request_line, *header_lines = raw.decode("latin-1").rstrip("\r\n").split("\r\n")
        method, path, _version = request_line.split(" ", 2)
        headers = {}
        for line in header_lines:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        self.requests.append(RecordedRequest(method=method, path=path, headers=headers))
        body = b"upstream ok"
        head = (
            f"HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\ncontent-length: {len(body)}\r\nconnection: close\r\n\r\n"
        )
        writer.write(head.encode() + body)
        await writer.drain()
        writer.close()


@pytest.fixture
async def upstream() -> AsyncIterator[RecordingUpstream]:
    recording = RecordingUpstream(port=0)
    server = await asyncio.start_server(recording.handle, "127.0.0.1", 0)
    recording.port = server.sockets[0].getsockname()[1]
    yield recording
    server.close()
    await server.wait_closed()


def make_proxy(
    decide: DecideClient, tmp_path: Path, decide_timeout_seconds: float = DEFAULT_DECIDE_TIMEOUT_SECONDS
) -> EgressProxy:
    return EgressProxy(decide, confdir=tmp_path / "mitmproxy-confdir", decide_timeout_seconds=decide_timeout_seconds)


async def proxied_get(proxy: EgressProxy, url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    async with (
        aiohttp.ClientSession() as session,
        session.get(url, proxy=f"http://127.0.0.1:{proxy.listen_port}", headers=headers) as response,
    ):
        return response.status, await response.text()


class RaisingDecideClient(DecideClient):
    async def decide(self, request: RequestMeta) -> Decision:
        raise RuntimeError("decide transport exploded")


class HangingDecideClient(DecideClient):
    async def decide(self, request: RequestMeta) -> Decision:
        await asyncio.Event().wait()
        raise AssertionError("unreachable: the event is never set")


class MalformedDecideClient(DecideClient):
    async def decide(self, request: RequestMeta) -> Decision:
        return cast(Decision, {"kind": "allow", "substitutions": []})


async def test_allow_substitutes_presented_placeholder(upstream: RecordingUpstream, tmp_path: Path) -> None:
    decide = StaticDecideClient(allow_with_substitution())
    async with make_proxy(decide, tmp_path) as proxy:
        status, body = await proxied_get(
            proxy, f"http://127.0.0.1:{upstream.port}/hello", headers={"Authorization": f"Bearer {PLACEHOLDER}"}
        )
    assert (status, body) == (200, "upstream ok")
    recorded = one(upstream.requests)
    assert recorded.method == "GET"
    assert recorded.path == "/hello"
    assert recorded.headers["authorization"] == f"Bearer {REAL_CREDENTIAL}"
    assert decide.requests == [
        RequestMeta(method="GET", scheme="http", host="127.0.0.1", port=upstream.port, path="/hello")
    ]


async def test_allow_substitutes_inside_basic_base64_payload(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """Git over HTTPS authenticates with ``Basic base64(user:token)``; the swap reaches inside the payload."""
    decide = StaticDecideClient(allow_with_substitution())
    presented = base64.b64encode(f"x-access-token:{PLACEHOLDER}".encode()).decode()
    async with make_proxy(decide, tmp_path) as proxy:
        status, _body = await proxied_get(
            proxy, f"http://127.0.0.1:{upstream.port}/git", headers={"Authorization": f"Basic {presented}"}
        )
    assert status == 200
    substituted = base64.b64encode(f"x-access-token:{REAL_CREDENTIAL}".encode()).decode()
    assert one(upstream.requests).headers["authorization"] == f"Basic {substituted}"


async def test_allow_without_placeholder_forwards_credential_free(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """A request that never presents the placeholder receives no credential anywhere."""
    decide = StaticDecideClient(allow_with_substitution())
    async with make_proxy(decide, tmp_path) as proxy:
        status_bare, _ = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/bare")
        status_other, _ = await proxied_get(
            proxy, f"http://127.0.0.1:{upstream.port}/other", headers={"Authorization": "Bearer something-else"}
        )
    assert (status_bare, status_other) == (200, 200)
    bare, other = upstream.requests
    assert "authorization" not in bare.headers
    assert other.headers["authorization"] == "Bearer something-else"
    for recorded in (bare, other):
        assert REAL_CREDENTIAL not in recorded.path
        assert all(REAL_CREDENTIAL not in value for value in recorded.headers.values())


async def test_allow_passes_unscanned_placeholder_through_verbatim(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """Only ``match_headers`` are scanned: elsewhere the inert placeholder rides along unsubstituted."""
    decide = StaticDecideClient(allow_with_substitution())
    async with make_proxy(decide, tmp_path) as proxy:
        status, _body = await proxied_get(
            proxy,
            f"http://127.0.0.1:{upstream.port}/lookup?q={PLACEHOLDER}",
            headers={"X-Unscanned": f"Bearer {PLACEHOLDER}"},
        )
    assert status == 200
    recorded = one(upstream.requests)
    assert recorded.path == f"/lookup?q={PLACEHOLDER}"
    assert recorded.headers["x-unscanned"] == f"Bearer {PLACEHOLDER}"
    assert REAL_CREDENTIAL not in recorded.path
    assert all(REAL_CREDENTIAL not in value for value in recorded.headers.values())


async def test_deny_refuses_without_upstream_contact(upstream: RecordingUpstream, tmp_path: Path) -> None:
    decide = StaticDecideClient(DenyDecision(reason="no standing policy or active grant"))
    async with make_proxy(decide, tmp_path) as proxy:
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/secret")
    assert status == 403
    assert "no standing policy or active grant" in body
    assert (upstream.connections, upstream.requests) == (0, [])


async def test_decide_exception_fails_closed(upstream: RecordingUpstream, tmp_path: Path) -> None:
    async with make_proxy(RaisingDecideClient(), tmp_path) as proxy:
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/secret")
    assert status == 502
    assert "fail closed" in body
    assert (upstream.connections, upstream.requests) == (0, [])


async def test_decide_hang_fails_closed(upstream: RecordingUpstream, tmp_path: Path) -> None:
    async with make_proxy(HangingDecideClient(), tmp_path, decide_timeout_seconds=0.2) as proxy:
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/secret")
    assert status == 502
    assert "fail closed" in body
    assert (upstream.connections, upstream.requests) == (0, [])


async def test_malformed_decision_fails_closed(upstream: RecordingUpstream, tmp_path: Path) -> None:
    async with make_proxy(MalformedDecideClient(), tmp_path) as proxy:
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/secret")
    assert status == 502
    assert "fail closed" in body
    assert (upstream.connections, upstream.requests) == (0, [])


async def test_connect_deny_refuses_tunnel(upstream: RecordingUpstream, tmp_path: Path) -> None:
    decide = StaticDecideClient(DenyDecision(reason="no grant for origin"))
    async with make_proxy(decide, tmp_path) as proxy, aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientHttpProxyError) as excinfo:
            await session.get(f"https://127.0.0.1:{upstream.port}/", proxy=f"http://127.0.0.1:{proxy.listen_port}")
    assert excinfo.value.status == 403
    assert (upstream.connections, upstream.requests) == (0, [])
    assert decide.requests == [
        RequestMeta(method="CONNECT", scheme=None, host="127.0.0.1", port=upstream.port, path=None)
    ]


async def test_connect_decide_exception_fails_closed(upstream: RecordingUpstream, tmp_path: Path) -> None:
    async with make_proxy(RaisingDecideClient(), tmp_path) as proxy, aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientHttpProxyError) as excinfo:
            await session.get(f"https://127.0.0.1:{upstream.port}/", proxy=f"http://127.0.0.1:{proxy.listen_port}")
    assert excinfo.value.status == 502
    assert (upstream.connections, upstream.requests) == (0, [])


if __name__ == "__main__":
    pytest_bazel.main()
