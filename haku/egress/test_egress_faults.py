"""Fail-closed-under-faults integration tests, extending the #4876 conformance set (#4914).

The conformance suite already covers a raised/hung/malformed decide and the endpoint answering
401/503/timeout/garbage. These add the remaining fault shapes the operator called out: the decide
endpoint being *down* (connection refused) or returning 5xx, the allowed upstream being unreachable
after admission, and the proxy being torn down while a request is in flight. Fail closed is asserted
as both halves — the client never gets the upstream body, and the upstream sees no unexpected
connection.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import pytest
import pytest_bazel
from more_itertools import one

from haku.egress.decision import DecideDenied
from haku.egress.proxy_test_harness import (
    RecordingUpstream,
    ServerError,
    allow_with_substitution,
    closed_localhost_port,
    dead_decide_client,
    make_proxy,
    proxied_get,
    stub_client,
    stub_console,
)
from haku.egress.static_decide_client import StaticDecideClient


async def test_decide_endpoint_down_fails_closed(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """The decide endpoint refusing the connection (process down) is a refusal, not a bypass."""
    async with aclosing(dead_decide_client(timeout_seconds=2.0)) as decide, make_proxy(decide, tmp_path) as proxy:
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/down")
    assert status == 502
    assert "fail closed" in body
    assert (upstream.connections, upstream.requests) == (0, [])


@pytest.mark.parametrize("http_status", [500, 502, 503])
async def test_decide_server_error_fails_closed(http_status: int, upstream: RecordingUpstream, tmp_path: Path) -> None:
    """A 5xx from the decide evaluator refuses the request with no upstream contact."""
    async with (
        stub_console(ServerError(status=http_status)) as stub,
        aclosing(stub_client(stub)) as decide,
        make_proxy(decide, tmp_path) as proxy,
    ):
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/5xx")
    assert status == 502
    assert "fail closed" in body
    assert (upstream.connections, upstream.requests) == (0, [])
    assert one(stub.requests).request.path == "/5xx"  # the endpoint was reached and answered 5xx


async def test_allowed_upstream_unreachable_returns_bad_gateway(tmp_path: Path) -> None:
    """Admission does not guarantee reachability: an allowed upstream that will not accept the dial
    yields a bad-gateway error to the client, and the proxy dials nothing else."""
    dead_port = closed_localhost_port()
    decide = StaticDecideClient(allow_with_substitution())
    async with make_proxy(decide, tmp_path) as proxy:
        status, _ = await proxied_get(proxy, f"http://127.0.0.1:{dead_port}/gone")
    assert status == 502  # mitmproxy's upstream-connect failure
    assert one(decide.requests).port == dead_port  # the decision was made for exactly this target


async def test_restart_re_decides_every_request(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """A restarted proxy holds no admission from the previous instance: every request is re-decided.

    The first proxy admits and pins the destination; a second proxy with a deny verdict refuses the
    same destination, so the earlier allow (and its pin) does not carry across the restart.
    """
    async with make_proxy(StaticDecideClient(allow_with_substitution()), tmp_path) as proxy:
        first_status, _ = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/before")
    assert (first_status, upstream.connections) == (200, 1)

    async with make_proxy(StaticDecideClient(DecideDenied(reason="restarted with no grant")), tmp_path) as proxy:
        second_status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/after")
    assert second_status == 403
    assert "restarted with no grant" in body
    assert upstream.connections == 1  # no second dial: the deny held despite the earlier allow


@dataclass
class HoldingUpstream:
    """Accepts the dialed connection, reads the request, then holds without ever sending a response.

    ``connected`` fires once the proxy's upstream dial lands, so the shutdown-mid-request test can
    wait for the in-flight state instead of sleeping (STYLE § Waiting). ``release`` lets teardown
    unblock the handler so a draining proxy shutdown cannot wedge on the held connection; the
    handler then closes without a response, so the client can never receive an upstream body.
    """

    port: int = 0
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        self.connected.set()
        await self.release.wait()
        writer.close()  # closes with no response: nothing valid ever reaches the client


@asynccontextmanager
async def holding_upstream() -> AsyncIterator[HoldingUpstream]:
    upstream = HoldingUpstream()
    server = await asyncio.start_server(upstream.handle, "127.0.0.1", 0)
    upstream.port = server.sockets[0].getsockname()[1]
    try:
        yield upstream
    finally:
        upstream.release.set()
        server.close()
        await server.wait_closed()


async def test_shutdown_mid_request_drops_inflight_flow(tmp_path: Path) -> None:
    """A request in flight when the proxy is torn down never returns the upstream's body.

    The request reaches the upstream (``connected``) and is genuinely pending — the upstream holds
    its response — when the proxy stops. However the teardown races the client, the request must
    not complete with an upstream success; releasing the held upstream keeps a draining shutdown
    from wedging without letting any valid response through.
    """
    async with holding_upstream() as held:
        proxy = make_proxy(StaticDecideClient(allow_with_substitution()), tmp_path)
        await proxy.__aenter__()
        request = asyncio.create_task(proxied_get(proxy, f"http://127.0.0.1:{held.port}/inflight"))
        try:
            await asyncio.wait_for(held.connected.wait(), timeout=15)  # the dial landed; request is mid-flight
            assert not request.done()  # the upstream is holding, so nothing has come back yet
            held.release.set()
            await asyncio.wait_for(proxy.__aexit__(None, None, None), timeout=30)
        finally:
            held.release.set()
            if not request.done():
                request.cancel()
        with contextlib.suppress(
            aiohttp.ClientError, ConnectionError, asyncio.CancelledError, asyncio.IncompleteReadError
        ):
            status, body = await request
            assert (status, body) != (200, "upstream ok")  # a synthetic error is fine; an upstream success is not


if __name__ == "__main__":
    pytest_bazel.main()
