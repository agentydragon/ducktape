"""CONNECT-tunnel and WebSocket integration tests through the embedded proxy (#4914).

The conformance suite already covers a denied CONNECT and a CONNECT whose decide raises, plus a
fully allowed tunnel with a pinned inner request. These add the paths the wishlist calls out:
per-inner-request gating (a tunnel the fence admits does not blanket-admit the requests inside it)
and WebSocket upgrades on allowed and denied hosts, over wss:// (a CONNECT tunnel + TLS the proxy
intercepts): the upgrade is an ordinary intercepted GET, gated like any request, and the denied case
never reaches the upstream. Absolute-form ws:// is not proxyable here — mitmproxy answers 400 — so
wss:// is the path exercised, and the one real clients use through a proxy.

The runner always intercepts (regular mode with the MITM CA), so a deliberately un-intercepted
passthrough — where end-to-end HTTP/2 would live — is not a configuration this fence offers and is
out of scope here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path

import aiohttp
import pytest
import pytest_bazel
from aiohttp import web
from more_itertools import one

from haku.egress.decide_client import DecideClient
from haku.egress.decision import DecideDenied, DecideResponse, RequestMeta
from haku.egress.proxy_test_harness import RecordingUpstream, allow, make_proxy, proxy_url, tunneled_get
from haku.egress.static_decide_client import StaticDecideClient
from haku.egress.tls_test_support import client_tls_context, make_self_signed_cert, server_tls_context


@dataclass
class MethodGatingDecideClient(DecideClient):
    """Allows only the listed methods; everything else is denied. Records what it was asked."""

    allowed_methods: frozenset[str]
    requests: list[RequestMeta] = field(default_factory=list)

    async def decide(
        self,
        request: RequestMeta,
        *,
        resolved_ips: frozenset[IPv4Address | IPv6Address],
        upstream_ip: IPv4Address | IPv6Address,
        proxy_client_credential: str,
    ) -> DecideResponse:
        del resolved_ips, upstream_ip, proxy_client_credential
        self.requests.append(request)
        if request.method in self.allowed_methods:
            return allow()
        return DecideDenied(reason=f"{request.method} is not admitted in this tunnel")


async def test_connect_admitted_but_inner_request_denied(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """A tunnel the fence admits still gates each request inside it: the inner GET is denied on its own."""
    decide = MethodGatingDecideClient(allowed_methods=frozenset({"CONNECT"}))
    async with make_proxy(decide, tmp_path) as proxy:
        connect_status, inner_status, _ = await tunneled_get(proxy.listen_port, f"127.0.0.1:{upstream.port}", "/inner")
    assert connect_status == 200  # the tunnel was established
    assert inner_status == 403  # ...but the request inside it was decided on its own and refused
    assert (upstream.connections, upstream.requests) == (0, [])  # denied inner request never dialed upstream
    assert [request.method for request in decide.requests] == ["CONNECT", "GET"]


@dataclass
class WebSocketUpstream:
    """An echo WebSocket server counting how many upgrades reached it (0 proves a denied upgrade never did)."""

    port: int = 0
    connections: int = 0

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        self.connections += 1
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str(f"echo:{message.data}")
        return ws


@asynccontextmanager
async def websocket_upstream(cert_path: Path, key_path: Path) -> AsyncIterator[WebSocketUpstream]:
    """A ``wss://`` echo server: WebSockets reach the proxy over a CONNECT tunnel + TLS, the path a
    real client (and mitmproxy) support, rather than absolute-form ws:// which the proxy rejects."""
    upstream = WebSocketUpstream()
    app = web.Application()
    app.router.add_get("/ws", upstream.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls_context(cert_path, key_path))
    await site.start()
    upstream.port = one(runner.addresses)[1]
    try:
        yield upstream
    finally:
        await runner.cleanup()


async def test_websocket_upgrade_on_allowed_host(tmp_path: Path) -> None:
    """An allowed wss:// upgrade tunnels through the proxy to the upstream and frames round-trip."""
    cert_path, key_path = make_self_signed_cert("localhost", tmp_path)
    decide = StaticDecideClient(allow())
    async with (
        websocket_upstream(cert_path, key_path) as up,
        make_proxy(decide, tmp_path, extra_options={"ssl_insecure": True}) as proxy,
        aiohttp.ClientSession() as session,
        session.ws_connect(
            f"wss://localhost:{up.port}/ws", proxy=proxy_url(proxy), ssl=client_tls_context(tmp_path)
        ) as ws,
    ):
        await ws.send_str("ping")
        reply = await ws.receive()
    assert (reply.type, reply.data) == (aiohttp.WSMsgType.TEXT, "echo:ping")
    assert up.connections == 1


async def test_websocket_upgrade_on_denied_host_refused(tmp_path: Path) -> None:
    """A denied wss:// upgrade is refused at the tunnel; the upstream is never contacted."""
    cert_path, key_path = make_self_signed_cert("localhost", tmp_path)
    decide = StaticDecideClient(DecideDenied(reason="no grant for this websocket origin"))
    async with (
        websocket_upstream(cert_path, key_path) as up,
        make_proxy(decide, tmp_path, extra_options={"ssl_insecure": True}) as proxy,
        aiohttp.ClientSession() as session,
    ):
        with pytest.raises(aiohttp.ClientError):
            await session.ws_connect(
                f"wss://localhost:{up.port}/ws", proxy=proxy_url(proxy), ssl=client_tls_context(tmp_path)
            )
    assert up.connections == 0


if __name__ == "__main__":
    pytest_bazel.main()
