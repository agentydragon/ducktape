"""Shared harness for the embedded egress-proxy integration tests.

Every test drives a real client through a real in-process mitmproxy toward a local
upstream: the proxy is the embedded runner (``EgressProxy``), the decision backend is
either an in-memory ``DecideClient`` double or ``StubConsole`` speaking the real wire
models over localhost. "Fail closed" is asserted as both halves — the client gets a
refusal AND the upstream sees no TCP connection — so a leak the connection-count would
catch cannot hide behind a refused response.

The fail-closed conformance set (``test_egress_proxy.py``) and the #4914 integration
suites (credentials, streaming, faults, tunnel, http2) all build on this module rather
than each re-deriving the harness.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import logging
import socket
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import assert_never, cast

import aiohttp
from aiohttp import web
from more_itertools import one
from pydantic import SecretStr

from haku.egress.addon import DEFAULT_DECIDE_TIMEOUT_SECONDS, ResolveAddresses, resolve_addresses
from haku.egress.decide_client import DecideClient
from haku.egress.decision import (
    DecideRequest,
    HttpAuthorizationAllowed,
    HttpAuthorizationDecision,
    HttpAuthorizationDenied,
    PlaceholderSubstitution,
    RequestMeta,
)
from haku.egress.localhost_decide_client import DEFAULT_TIMEOUT_SECONDS, LocalhostDecideClient
from haku.egress.runner import EgressProxy
from haku.grants.authorization import GrantSourceKind

PLACEHOLDER = "proxy-github-placeholder"
REAL_CREDENTIAL = "real-redeemed-credential"
FENCE_CREDENTIAL = "shared-fence-credential"
BRIDGE_BEARER = "bridge-session-bearer"


def allow(*substitutions: PlaceholderSubstitution) -> HttpAuthorizationAllowed:
    """An allow verdict carrying ``substitutions``, valid five minutes out."""
    return HttpAuthorizationAllowed(
        source=GrantSourceKind.DATABASE,
        decision_id="database:50000000-0000-4000-8000-000000000005",
        valid_until=datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5),
        substitutions=list(substitutions),
    )


def bearer_substitution() -> PlaceholderSubstitution:
    """The canonical Authorization-header swap the credential tests reuse."""
    return PlaceholderSubstitution(
        placeholder=PLACEHOLDER, value=REAL_CREDENTIAL, match_headers=frozenset({"Authorization"})
    )


def allow_with_substitution() -> HttpAuthorizationAllowed:
    return allow(bearer_substitution())


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]  # names lowercased, last value wins: assertions stay case-insensitive
    # Every header line in order, names lowercased, values verbatim: duplicate names survive here
    # where ``headers`` collapses them, so duplicate-header substitution is checkable.
    header_lines: tuple[tuple[str, str], ...] = ()


@dataclass
class RecordingUpstream:
    """Minimal HTTP/1.1 upstream tracking TCP connections separately from requests.

    A TCP connection without a request is already a fail-open leak (the eager connection
    strategy produces exactly that), so tests assert on both.
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
        request_line, *raw_header_lines = raw.decode("latin-1").rstrip("\r\n").split("\r\n")
        method, path, _version = request_line.split(" ", 2)
        headers = {}
        header_lines = []
        for line in raw_header_lines:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
            header_lines.append((name.strip().lower(), value.strip()))
        self.requests.append(
            RecordedRequest(method=method, path=path, headers=headers, header_lines=tuple(header_lines))
        )
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


def make_proxy(
    decide: DecideClient,
    tmp_path: Path,
    *,
    decide_timeout_seconds: float = DEFAULT_DECIDE_TIMEOUT_SECONDS,
    resolve: ResolveAddresses = resolve_addresses,
    extra_options: Mapping[str, object] | None = None,
) -> EgressProxy:
    return EgressProxy(
        decide,
        confdir=tmp_path / "mitmproxy-confdir",
        decide_timeout_seconds=decide_timeout_seconds,
        resolve=resolve,
        extra_options=extra_options,
    )


def proxy_url(proxy: EgressProxy) -> str:
    # URL userinfo makes aiohttp send the same Basic proxy credential ordinary runner-launched
    # clients use. The addon decodes it and the upstream never sees Proxy-Authorization.
    return f"http://:{BRIDGE_BEARER}@127.0.0.1:{proxy.listen_port}"


async def proxied_get(proxy: EgressProxy, url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    async with (
        aiohttp.ClientSession() as session,
        session.get(url, proxy=proxy_url(proxy), headers=headers) as response,
    ):
        return response.status, await response.text()


async def proxied_get_raw(proxy_port: int, url: str, header_lines: list[tuple[str, str]]) -> tuple[int, str]:
    """Absolute-form plain-HTTP GET through the proxy with hand-built headers.

    ``aiohttp`` folds duplicate headers and title-cases names; a raw request preserves the
    exact header names, cases, and duplicates the case/duplicate-header substitution edges
    need. ``url`` is the absolute request target (``http://host:port/path``).
    """
    authority = url.split("//", 1)[1].split("/", 1)[0]
    proxy_auth = base64.b64encode(f":{BRIDGE_BEARER}".encode()).decode()
    lines = [
        f"GET {url} HTTP/1.1",
        f"Host: {authority}",
        "Connection: close",
        f"Proxy-Authorization: Basic {proxy_auth}",
    ]
    lines += [f"{name}: {value}" for name, value in header_lines]
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    try:
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        raw = await reader.read()
        head, _, body = raw.partition(b"\r\n\r\n")
        return int(head.split(b" ", 2)[1]), body.decode()
    finally:
        writer.close()
        await writer.wait_closed()


class RaisingDecideClient(DecideClient):
    async def decide(
        self,
        request: RequestMeta,
        *,
        resolved_ips: frozenset[IPv4Address | IPv6Address],
        upstream_ip: IPv4Address | IPv6Address,
        proxy_client_credential: str,
    ) -> HttpAuthorizationDecision:
        del resolved_ips, upstream_ip, proxy_client_credential
        raise RuntimeError("decide transport exploded")


class HangingDecideClient(DecideClient):
    async def decide(
        self,
        request: RequestMeta,
        *,
        resolved_ips: frozenset[IPv4Address | IPv6Address],
        upstream_ip: IPv4Address | IPv6Address,
        proxy_client_credential: str,
    ) -> HttpAuthorizationDecision:
        del resolved_ips, upstream_ip, proxy_client_credential
        await asyncio.Event().wait()
        raise AssertionError("unreachable: the event is never set")


class MalformedDecideClient(DecideClient):
    async def decide(
        self,
        request: RequestMeta,
        *,
        resolved_ips: frozenset[IPv4Address | IPv6Address],
        upstream_ip: IPv4Address | IPv6Address,
        proxy_client_credential: str,
    ) -> HttpAuthorizationDecision:
        del request, resolved_ips, upstream_ip, proxy_client_credential
        return cast(HttpAuthorizationDecision, {"allowed": True, "substitutions": []})


@dataclass(frozen=True)
class Unconfigured:
    """503 before authentication, as the real endpoint answers until a deploy wires it."""


@dataclass(frozen=True)
class Hang:
    """Accept the POST, never answer: the client's own timeout must fire."""


@dataclass(frozen=True)
class GarbageBody:
    """200 whose body is not an ``HttpAuthorizationDecision``."""


@dataclass(frozen=True)
class ServerError:
    """5xx from the decide endpoint mid-evaluation."""

    status: int = 500


type StubBehavior = HttpAuthorizationAllowed | HttpAuthorizationDenied | Unconfigured | Hang | GarbageBody | ServerError


@dataclass
class StubConsole:
    """In-process decide endpoint speaking the real wire models; parses and records ``DecideRequest``s.

    ``behavior`` is mutable so a test can flip the verdict between admissions (e.g. allow the
    CONNECT, then deny an inner request).
    """

    behavior: StubBehavior
    port: int = 0
    requests: list[DecideRequest] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.Response:
        behavior = self.behavior
        if isinstance(behavior, Unconfigured):
            return web.json_response({"detail": "HTTP egress decision is not configured"}, status=503)
        if request.headers.get("Authorization") != f"Bearer {FENCE_CREDENTIAL}":
            return web.json_response({"detail": "fence credential was rejected"}, status=401)
        self.requests.append(DecideRequest.model_validate_json(await request.read()))
        match behavior:
            case HttpAuthorizationAllowed() | HttpAuthorizationDenied() as verdict:
                return web.Response(text=verdict.model_dump_json(), content_type="application/json")
            case Hang():
                await asyncio.Event().wait()
                raise AssertionError("unreachable: the event is never set")
            case GarbageBody():
                return web.Response(text="} not a decide response {", content_type="application/json")
            case ServerError():
                return web.json_response({"detail": "decide evaluator crashed"}, status=behavior.status)
            case _:
                assert_never(behavior)


@asynccontextmanager
async def stub_console(behavior: StubBehavior) -> AsyncIterator[StubConsole]:
    stub = StubConsole(behavior=behavior)
    app = web.Application()
    # The path is pinned to Console's route (haku/console/grants/http/decide_routes.py),
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
    stub: StubConsole, *, fence_credential: str = FENCE_CREDENTIAL, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> LocalhostDecideClient:
    return LocalhostDecideClient(
        base_url=f"http://127.0.0.1:{stub.port}",
        fence_credential=SecretStr(fence_credential),
        timeout_seconds=timeout_seconds,
    )


def closed_localhost_port() -> int:
    """A localhost port with nothing listening: bound to reserve it, then closed."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def dead_decide_client(*, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> LocalhostDecideClient:
    """A real decide client pointed at a closed port: every ``decide`` raises a connection error."""
    return LocalhostDecideClient(
        base_url=f"http://127.0.0.1:{closed_localhost_port()}",
        fence_credential=SecretStr(FENCE_CREDENTIAL),
        timeout_seconds=timeout_seconds,
    )


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


async def tunneled_get(proxy_port: int, authority: str, path: str) -> tuple[int, int, str]:
    """CONNECT to ``authority`` through the proxy, then one cleartext GET inside the tunnel.

    Plain HTTP inside the tunnel keeps TLS trust out of the harness while still driving the
    tunnel-establishment and intercepted-inner-request paths (mitmproxy parses the tunneled
    cleartext as HTTP, so the inner request is gated like any other). Returns
    ``(connect_status, inner_status, body)``; ``inner_status`` is 0 when the tunnel is refused.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    try:
        proxy_auth = base64.b64encode(f":{BRIDGE_BEARER}".encode()).decode()
        writer.write(
            f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
            f"Proxy-Authorization: Basic {proxy_auth}\r\n\r\n".encode()
        )
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


class LogCapture(logging.Handler):
    """Captures every record emitted by ``haku.egress`` so the never-log invariant is checkable.

    The gate logs at INFO/WARNING via ``haku.egress.addon``; the never-log invariant (#4670)
    is that neither the placeholder nor the real credential value ever appears in those records.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def rendered(self) -> str:
        # getMessage() applies %-args, which is where a credential would leak if one were logged.
        return "\n".join(record.getMessage() for record in self.records)


@asynccontextmanager
async def capture_egress_logs() -> AsyncIterator[LogCapture]:
    capture = LogCapture()
    logger = logging.getLogger("haku.egress")
    previous_level = logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    try:
        yield capture
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
