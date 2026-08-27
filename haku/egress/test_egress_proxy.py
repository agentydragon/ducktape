"""Fail-closed conformance tests for the embedded egress proxy.

Every test drives a real client through a real in-process mitmproxy toward a
local recording upstream. "Fail closed" is asserted as both halves: the client
gets a refusal AND the upstream sees no TCP connection at all.

Plain HTTP (absolute-form proxying) keeps TLS trust out of the setup; the
CONNECT tests cover the tunnel path without the MITM CA because refusal happens
before any TLS.

``LocalhostDecideClient`` runs the same drills against ``StubConsole``, an
in-process decide endpoint speaking the real wire models.

Pinned-dial enforcement (#4670's DNS-rebinding property) is also asserted with
the plain-HTTP harness: the TCP-recording upstreams observe the dialed socket
peer, and a decoy listener on the address a fresh resolution would yield must
stay silent. TLS behavior is preserved by construction rather than asserted
here: the gate rewrites only ``Server.address`` at ``server_connect`` time,
while ``Server.sni`` and the Host header keep the hostname (asserted below), so
mitmproxy's upstream verification still checks the real hostname against the
real certificate.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import assert_never, cast

import aiohttp
import pytest
import pytest_bazel
from aiohttp import web
from mitmproxy.connection import Client, Server
from mitmproxy.proxy.server_hooks import ServerConnectionHookData
from more_itertools import one
from pydantic import SecretStr

from haku.egress.addon import DEFAULT_DECIDE_TIMEOUT_SECONDS, EgressGateAddon, ResolveAddresses, resolve_addresses
from haku.egress.decide_client import DecideClient
from haku.egress.decision import (
    DecideAllowed,
    DecideDenied,
    DecideRequest,
    DecideResponse,
    DecisionSource,
    GrantScope,
    PlaceholderSubstitution,
    RequestMeta,
)
from haku.egress.localhost_decide_client import DEFAULT_TIMEOUT_SECONDS, LocalhostDecideClient
from haku.egress.runner import EgressProxy
from haku.egress.static_decide_client import StaticDecideClient

PLACEHOLDER = "proxy-github-placeholder"
REAL_CREDENTIAL = "real-redeemed-credential"


def allow_with_substitution() -> DecideAllowed:
    return DecideAllowed(
        source=DecisionSource.GRANT,
        decision_id="grant:50000000-0000-4000-8000-000000000005",
        valid_until=datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5),
        substitutions=[
            PlaceholderSubstitution(
                placeholder=PLACEHOLDER, value=REAL_CREDENTIAL, match_headers=frozenset({"Authorization"})
            )
        ],
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


@asynccontextmanager
async def recording_upstream(host: str, port: int = 0) -> AsyncIterator[RecordingUpstream]:
    recording = RecordingUpstream(port=port)
    server = await asyncio.start_server(recording.handle, host, port)
    recording.port = server.sockets[0].getsockname()[1]
    try:
        yield recording
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
async def upstream() -> AsyncIterator[RecordingUpstream]:
    async with recording_upstream("127.0.0.1") as recording:
        yield recording


def make_proxy(
    decide: DecideClient,
    tmp_path: Path,
    decide_timeout_seconds: float = DEFAULT_DECIDE_TIMEOUT_SECONDS,
    resolve: ResolveAddresses = resolve_addresses,
) -> EgressProxy:
    return EgressProxy(
        decide, confdir=tmp_path / "mitmproxy-confdir", decide_timeout_seconds=decide_timeout_seconds, resolve=resolve
    )


async def proxied_get(proxy: EgressProxy, url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    async with (
        aiohttp.ClientSession() as session,
        session.get(url, proxy=f"http://127.0.0.1:{proxy.listen_port}", headers=headers) as response,
    ):
        return response.status, await response.text()


class RaisingDecideClient(DecideClient):
    async def decide(
        self,
        request: RequestMeta,
        *,
        resolved_ips: frozenset[IPv4Address | IPv6Address],
        upstream_ip: IPv4Address | IPv6Address,
    ) -> DecideResponse:
        raise RuntimeError("decide transport exploded")


class HangingDecideClient(DecideClient):
    async def decide(
        self,
        request: RequestMeta,
        *,
        resolved_ips: frozenset[IPv4Address | IPv6Address],
        upstream_ip: IPv4Address | IPv6Address,
    ) -> DecideResponse:
        await asyncio.Event().wait()
        raise AssertionError("unreachable: the event is never set")


class MalformedDecideClient(DecideClient):
    async def decide(
        self,
        request: RequestMeta,
        *,
        resolved_ips: frozenset[IPv4Address | IPv6Address],
        upstream_ip: IPv4Address | IPv6Address,
    ) -> DecideResponse:
        return cast(DecideResponse, {"allowed": True, "substitutions": []})


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
    decide = StaticDecideClient(DecideDenied(reason="no standing policy or active grant"))
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
    decide = StaticDecideClient(DecideDenied(reason="no grant for origin"))
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


PROXY_BEARER = "proxy-identity-bearer"
FENCE_CREDENTIAL = "agent-fence-credential"


@dataclass(frozen=True)
class Unconfigured:
    """503 before authentication, as the real endpoint answers until a deploy wires it."""


@dataclass(frozen=True)
class Hang:
    """Accept the POST, never answer: the client's own timeout must fire."""


@dataclass(frozen=True)
class GarbageBody:
    """200 whose body is not a ``DecideResponse``."""


type StubBehavior = DecideAllowed | DecideDenied | Unconfigured | Hang | GarbageBody


@dataclass
class StubConsole:
    """In-process decide endpoint speaking the real wire models; parses and records ``DecideRequest``s."""

    behavior: StubBehavior
    port: int = 0
    requests: list[DecideRequest] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.Response:
        behavior = self.behavior
        if isinstance(behavior, Unconfigured):
            return web.json_response({"detail": "HTTP egress decision is not configured"}, status=503)
        if request.headers.get("Authorization") != f"Bearer {PROXY_BEARER}":
            return web.json_response({"detail": "proxy identity bearer was rejected"}, status=401)
        self.requests.append(DecideRequest.model_validate_json(await request.read()))
        match behavior:
            case DecideAllowed() | DecideDenied() as verdict:
                return web.Response(text=verdict.model_dump_json(), content_type="application/json")
            case Hang():
                await asyncio.Event().wait()
                raise AssertionError("unreachable: the event is never set")
            case GarbageBody():
                return web.Response(text="} not a decide response {", content_type="application/json")
            case _:
                assert_never(behavior)


@asynccontextmanager
async def stub_console(behavior: StubBehavior) -> AsyncIterator[StubConsole]:
    stub = StubConsole(behavior=behavior)
    app = web.Application()
    # The path is pinned to Console's route (haku/console/http_decide_routes.py),
    # deliberately not imported from the client: drift on either side must fail here.
    app.router.add_post("/api/internal/http/decide", stub.handle)
    # handler_cancellation: a Hang handler outliving its disconnected client
    # would otherwise stall cleanup for the shutdown timeout.
    runner = web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    stub.port = one(runner.addresses)[1]
    try:
        yield stub
    finally:
        await runner.cleanup()


def stub_client(
    stub: StubConsole, *, proxy_bearer: str = PROXY_BEARER, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> LocalhostDecideClient:
    return LocalhostDecideClient(
        base_url=f"http://127.0.0.1:{stub.port}",
        proxy_bearer=SecretStr(proxy_bearer),
        fence_credential=SecretStr(FENCE_CREDENTIAL),
        timeout_seconds=timeout_seconds,
    )


async def test_localhost_decide_allow_flows_end_to_end(upstream: RecordingUpstream, tmp_path: Path) -> None:
    async with (
        stub_console(allow_with_substitution()) as stub,
        aclosing(stub_client(stub)) as decide,
        make_proxy(decide, tmp_path) as proxy,
    ):
        status, body = await proxied_get(
            proxy, f"http://127.0.0.1:{upstream.port}/hello", headers={"Authorization": f"Bearer {PLACEHOLDER}"}
        )
    assert (status, body) == (200, "upstream ok")
    assert one(upstream.requests).headers["authorization"] == f"Bearer {REAL_CREDENTIAL}"
    sent = one(stub.requests)
    assert sent.request == RequestMeta(method="GET", scheme="http", host="127.0.0.1", port=upstream.port, path="/hello")
    assert sent.fence_credential.get_secret_value() == FENCE_CREDENTIAL
    assert (sent.resolved_ips, sent.upstream_ip) == (frozenset({IPv4Address("127.0.0.1")}), IPv4Address("127.0.0.1"))


async def test_localhost_decide_deny_refuses_without_upstream_contact(
    upstream: RecordingUpstream, tmp_path: Path
) -> None:
    denied = DecideDenied(
        reason="no standing policy or active grant",
        grant_scope=GrantScope(scheme="http", host="127.0.0.1", port=upstream.port),
    )
    async with (
        stub_console(denied) as stub,
        aclosing(stub_client(stub)) as decide,
        make_proxy(decide, tmp_path) as proxy,
    ):
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/secret")
    assert status == 403
    assert "no standing policy or active grant" in body
    assert (upstream.connections, upstream.requests) == (0, [])


async def test_localhost_decide_connect_deny_refuses_tunnel(upstream: RecordingUpstream, tmp_path: Path) -> None:
    async with (
        stub_console(DecideDenied(reason="no grant for origin")) as stub,
        aclosing(stub_client(stub)) as decide,
        make_proxy(decide, tmp_path) as proxy,
        aiohttp.ClientSession() as session,
    ):
        with pytest.raises(aiohttp.ClientHttpProxyError) as excinfo:
            await session.get(f"https://127.0.0.1:{upstream.port}/", proxy=f"http://127.0.0.1:{proxy.listen_port}")
    assert excinfo.value.status == 403
    assert (upstream.connections, upstream.requests) == (0, [])
    assert one(stub.requests).request == RequestMeta(
        method="CONNECT", scheme=None, host="127.0.0.1", port=upstream.port, path=None
    )


@dataclass(frozen=True)
class EndpointFailure:
    """One way the decide hop fails; each must refuse with zero upstream contact."""

    behavior: StubBehavior
    proxy_bearer: str = PROXY_BEARER
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


ENDPOINT_FAILURES = [
    pytest.param(
        EndpointFailure(behavior=allow_with_substitution(), proxy_bearer="not-the-proxy-bearer"),
        id="rejected-bearer-401",
    ),
    pytest.param(EndpointFailure(behavior=Unconfigured()), id="unconfigured-503"),
    pytest.param(EndpointFailure(behavior=Hang(), timeout_seconds=0.2), id="endpoint-timeout"),
    pytest.param(EndpointFailure(behavior=GarbageBody()), id="garbage-body"),
]


@pytest.mark.parametrize("failure", ENDPOINT_FAILURES)
async def test_localhost_decide_endpoint_failure_fails_closed(
    failure: EndpointFailure, upstream: RecordingUpstream, tmp_path: Path
) -> None:
    async with (
        stub_console(failure.behavior) as stub,
        aclosing(
            stub_client(stub, proxy_bearer=failure.proxy_bearer, timeout_seconds=failure.timeout_seconds)
        ) as decide,
        make_proxy(decide, tmp_path) as proxy,
    ):
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/secret")
    assert status == 502
    assert "fail closed" in body
    assert (upstream.connections, upstream.requests) == (0, [])


@dataclass
class QueueResolver:
    """Resolver double whose successive answers differ — the DNS-rebinding scenario (#4670).

    Indexing by call number makes a resolution past the scripted answers fail loudly: any
    extra call IS the second resolution the pinned dial exists to prevent.
    """

    answers: list[frozenset[IPv4Address | IPv6Address]]
    calls: int = 0

    async def __call__(self, host: str, port: int) -> frozenset[IPv4Address | IPv6Address]:
        answer = self.answers[self.calls]
        self.calls += 1
        return answer


@asynccontextmanager
async def pinned_and_decoy_upstreams() -> AsyncIterator[tuple[RecordingUpstream, RecordingUpstream]]:
    """Two recorders on one port: 127.0.0.2 (the validated first answer) and 127.0.0.1 (the
    address a fresh resolution of ``localhost`` yields — where an unpinned dial would land)."""
    async with AsyncExitStack() as stack:
        pinned = await stack.enter_async_context(recording_upstream("127.0.0.2"))
        decoy = await stack.enter_async_context(recording_upstream("127.0.0.1", port=pinned.port))
        yield pinned, decoy


async def test_pinned_dial_survives_rebinding_second_answer(tmp_path: Path) -> None:
    """The dial goes to the first validated answer even though a fresh resolution differs.

    End to end through ``LocalhostDecideClient``: the resolver's first answer (127.0.0.2) is
    validated by the decide exchange and pinned; its scripted second answer — and the system
    resolver's real answer for ``localhost`` — is 127.0.0.1, so a re-resolving dial would hit
    the decoy. Exactly one resolution may happen, and the decoy must stay silent.
    """
    resolver = QueueResolver(answers=[frozenset({IPv4Address("127.0.0.2")}), frozenset({IPv4Address("127.0.0.1")})])
    async with (
        pinned_and_decoy_upstreams() as (pinned, decoy),
        stub_console(allow_with_substitution()) as stub,
        aclosing(stub_client(stub)) as decide,
        make_proxy(decide, tmp_path, resolve=resolver) as proxy,
    ):
        status, body = await proxied_get(proxy, f"http://localhost:{pinned.port}/pinned")
    assert (status, body) == (200, "upstream ok")
    recorded = one(pinned.requests)
    assert recorded.path == "/pinned"
    # Host semantics preserved: the upstream sees the hostname, never the pinned literal.
    assert recorded.headers["host"] == f"localhost:{pinned.port}"
    assert (decoy.connections, decoy.requests) == (0, [])
    assert resolver.calls == 1
    sent = one(stub.requests)
    assert sent.request == RequestMeta(method="GET", scheme="http", host="localhost", port=pinned.port, path="/pinned")
    assert (sent.resolved_ips, sent.upstream_ip) == (frozenset({IPv4Address("127.0.0.2")}), IPv4Address("127.0.0.2"))


async def tunneled_get(proxy_port: int, authority: str, path: str) -> tuple[int, int, str]:
    """CONNECT to ``authority`` through the proxy, then one cleartext GET inside the tunnel.

    Plain HTTP inside the tunnel keeps TLS trust out of the harness while still driving the
    tunnel-establishment and intercepted-inner-request paths (mitmproxy parses the tunneled
    cleartext as HTTP, so the inner request is gated like any other).
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    try:
        writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
        await writer.drain()
        connect_head = await reader.readuntil(b"\r\n\r\n")
        connect_status = int(connect_head.split(b" ", 2)[1])
        if connect_status != 200:
            return connect_status, 0, ""
        writer.write(f"GET {path} HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        raw = await reader.read()
        head, _, body = raw.partition(b"\r\n\r\n")
        return connect_status, int(head.split(b" ", 2)[1]), body.decode()
    finally:
        writer.close()
        await writer.wait_closed()


async def test_pinned_dial_connect_tunnel_and_inner_request(tmp_path: Path) -> None:
    """Both the tunnel admission and the inner request dial the validated address.

    The stub resolver answers 127.0.0.2 for the CONNECT decide and again for the inner
    request's decide; the system resolver's answer for ``localhost`` (127.0.0.1, the decoy)
    must never be dialed, and no third resolution may happen.
    """
    answer = frozenset({IPv4Address("127.0.0.2")})
    resolver = QueueResolver(answers=[answer, answer])
    decide = StaticDecideClient(allow_with_substitution())
    async with pinned_and_decoy_upstreams() as (pinned, decoy), make_proxy(decide, tmp_path, resolve=resolver) as proxy:
        connect_status, status, body = await tunneled_get(proxy.listen_port, f"localhost:{pinned.port}", "/tunneled")
    assert (connect_status, status, body) == (200, 200, "upstream ok")
    recorded = one(pinned.requests)
    assert (recorded.method, recorded.path) == ("GET", "/tunneled")
    assert recorded.headers["host"] == f"localhost:{pinned.port}"
    assert (decoy.connections, decoy.requests) == (0, [])
    assert resolver.calls == 2
    assert [request.method for request in decide.requests] == ["CONNECT", "GET"]
    assert all(request.host == "localhost" for request in decide.requests)


async def test_unpinned_upstream_dial_is_killed() -> None:
    """Defense in depth at the dial chokepoint: a ``server_connect`` no allow pinned is refused.

    Every gated flow pins its destination before mitmproxy dials, so this only triggers for a
    dial that never passed the gate — which must die rather than resolve.
    """
    addon = EgressGateAddon(StaticDecideClient(DecideDenied(reason="unreached")))
    server = Server(address=("evil.example", 443))
    addon.server_connect(
        ServerConnectionHookData(
            server=server, client=Client(peername=("127.0.0.1", 51234), sockname=("127.0.0.1", 8080))
        )
    )
    assert server.error is not None
    assert server.address == ("evil.example", 443)  # never rewritten toward a resolution


if __name__ == "__main__":
    pytest_bazel.main()
