"""HTTP/2 integration tests through the embedded proxy (#4914).

HTTP/2 is why mitmproxy was chosen for the fence: ``bbr`` → BuildBuddy is gRPC, i.e. HTTP/2, so the
fence must gate and substitute on requests to an HTTP/2 upstream exactly as on HTTP/1.1, without a
multiplexed stream slipping past the request hook.

Finding — client-leg HTTP/2 is not interceptable here. mitmproxy's ``tlsconfig`` forces the
client-facing ALPN to ``http/1.1`` for a secure web proxy ("we currently don't support CONNECT over
HTTP/2"), so a client tunnelling h2 through the CONNECT proxy is downgraded to HTTP/1.1 on the client
leg. The **upstream** leg is still HTTP/2: the client's h2 ALPN offer propagates to the upstream, so
mitmproxy speaks h1 to the client and h2 to the server (its h1→h2 translation). End-to-end h2 would
require the fence to pass the tunnel through un-intercepted, which forgoes inner-request gating and
substitution — a separate mode this runner does not enable. These tests therefore exercise the
gated, intercepted **h2 upstream** path; the client is httpx with ``http2=True`` so its h2 offer
reaches the upstream leg.

The upstream advertises only ``h2`` in ALPN, so a recorded request proves the upstream leg is
HTTP/2 (an h1 downgrade could not have completed the handshake). The client trusts the runner's MITM
CA; the runner is given ``ssl_insecure`` for the self-signed upstream (a test seam — the behaviour
under test is the gate's, not mitmproxy's upstream-cert verification).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import h2.config
import h2.connection
import h2.events
import httpx
import pytest_bazel
from more_itertools import one

from haku.egress.proxy_test_harness import (
    PLACEHOLDER,
    REAL_CREDENTIAL,
    allow,
    allow_with_substitution,
    make_proxy,
    proxy_url,
)
from haku.egress.static_decide_client import StaticDecideClient
from haku.egress.tls_test_support import client_tls_context, make_self_signed_cert, server_tls_context


@dataclass
class Http2Upstream:
    """Records the request headers of every h2 stream it serves (pseudo-headers included)."""

    port: int = 0
    requests: list[dict[str, str]] = field(default_factory=list)


class _Http2ServerProtocol(asyncio.Protocol):
    def __init__(self, upstream: Http2Upstream) -> None:
        self._upstream = upstream
        self._conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
        )
        self._transport: asyncio.Transport | None = None
        self._headers: dict[int, dict[str, str]] = {}

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.Transport)
        self._transport = transport
        self._conn.initiate_connection()
        transport.write(self._conn.data_to_send())

    def data_received(self, data: bytes) -> None:
        assert self._transport is not None
        for event in self._conn.receive_data(data):
            if isinstance(event, h2.events.RequestReceived):
                self._headers[event.stream_id] = {str(name): str(value) for name, value in event.headers or []}
            elif isinstance(event, h2.events.DataReceived):
                self._conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            elif isinstance(event, h2.events.StreamEnded):
                self._respond(event.stream_id)
        out = self._conn.data_to_send()
        if out:
            self._transport.write(out)

    def _respond(self, stream_id: int) -> None:
        assert self._transport is not None
        self._upstream.requests.append(self._headers.get(stream_id, {}))
        body = b"h2 upstream ok"
        self._conn.send_headers(
            stream_id, [(":status", "200"), ("content-type", "text/plain"), ("content-length", str(len(body)))]
        )
        self._conn.send_data(stream_id, body, end_stream=True)
        self._transport.write(self._conn.data_to_send())


@asynccontextmanager
async def http2_upstream(cert_path: Path, key_path: Path) -> AsyncIterator[Http2Upstream]:
    upstream = Http2Upstream()
    # h2 only in ALPN: a completed handshake proves the upstream leg is HTTP/2, not a downgrade.
    context = server_tls_context(cert_path, key_path, alpn_protocols=["h2"])
    server = await asyncio.get_running_loop().create_server(
        lambda: _Http2ServerProtocol(upstream), "127.0.0.1", 0, ssl=context
    )
    upstream.port = server.sockets[0].getsockname()[1]
    try:
        yield upstream
    finally:
        server.close()
        await server.wait_closed()


async def test_http2_upstream_gated_and_substituted_like_h1(tmp_path: Path) -> None:
    """A request to an HTTP/2 upstream is gated and credential-substituted, identically to HTTP/1.1.

    The h2-only upstream recording the request is proof the upstream leg negotiated HTTP/2; the gate
    ran on the intercepted request (CONNECT then GET) and the placeholder was swapped for the real
    credential just as on the h1 path.
    """
    cert_path, key_path = make_self_signed_cert("localhost", tmp_path)
    decide = StaticDecideClient(allow_with_substitution())
    async with (
        http2_upstream(cert_path, key_path) as up,
        make_proxy(decide, tmp_path, extra_options={"ssl_insecure": True}) as proxy,
        httpx.AsyncClient(http2=True, verify=client_tls_context(tmp_path), proxy=proxy_url(proxy)) as client,
    ):
        response = await client.get(
            f"https://localhost:{up.port}/h2path", headers={"Authorization": f"Bearer {PLACEHOLDER}"}
        )
    assert (response.status_code, response.text) == (200, "h2 upstream ok")
    recorded = one(up.requests)  # only an HTTP/2 upstream leg could have produced this
    assert (recorded[":method"], recorded[":path"]) == ("GET", "/h2path")
    assert recorded["authorization"] == f"Bearer {REAL_CREDENTIAL}"  # substitution identical to h1
    assert [request.method for request in decide.requests] == ["CONNECT", "GET"]  # the request hit the gate


async def test_http2_upstream_every_request_is_gated(tmp_path: Path) -> None:
    """Several requests to an HTTP/2 upstream are each decided: none rides the tunnel un-gated."""
    cert_path, key_path = make_self_signed_cert("localhost", tmp_path)
    decide = StaticDecideClient(allow())
    async with (
        http2_upstream(cert_path, key_path) as up,
        make_proxy(decide, tmp_path, extra_options={"ssl_insecure": True}) as proxy,
        httpx.AsyncClient(http2=True, verify=client_tls_context(tmp_path), proxy=proxy_url(proxy)) as client,
    ):
        first, second = await asyncio.gather(
            client.get(f"https://localhost:{up.port}/one"), client.get(f"https://localhost:{up.port}/two")
        )
    assert {first.status_code, second.status_code} == {200}
    assert {recorded[":path"] for recorded in up.requests} == {"/one", "/two"}  # both served over h2
    gated_paths = {request.path for request in decide.requests if request.method == "GET"}
    assert gated_paths == {"/one", "/two"}  # both requests were decided on their own


if __name__ == "__main__":
    pytest_bazel.main()
