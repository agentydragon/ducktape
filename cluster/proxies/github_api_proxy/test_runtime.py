import asyncio
import base64
import contextlib
import json
import ssl
import stat
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_bazel
from aiohttp import web
from mitmproxy import http, io
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.proxy.server_hooks import ServerConnectionHookData
from mitmproxy.tools.dump import DumpMaster
from prometheus_client import generate_latest

from cluster.proxies.github_api_proxy.config import Settings
from cluster.proxies.github_api_proxy.destinations import PublicOrigins
from cluster.proxies.github_api_proxy.metrics import Metrics
from cluster.proxies.github_api_proxy.runtime import create_master
from cluster.proxies.github_api_proxy.testing import certificates

PASSWORD = "test-private-password-alpha-0123456789"
SECOND_PASSWORD = "test-private-password-beta-0123456789"


@dataclass
class LocalOrigins:
    http_port: int
    https_port: int
    requests: list[tuple[str, str]] = field(default_factory=list)
    dials: int = 0
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    finish_events: asyncio.Event = field(default_factory=asyncio.Event)

    def running(self) -> None:
        self.ready.set()

    def server_connect(self, data: ServerConnectionHookData) -> None:
        if data.server.error is not None:
            return
        # Only this fixture redirects an already validated, numerically pinned
        # public destination to loopback. Production has no private-origin switch.
        assert data.server.address is not None
        assert data.server.address[0] == "93.184.216.34"
        assert data.server.address[1] in (80, 443)
        self.dials += 1
        data.server.address = ("127.0.0.1", self.https_port if data.server.tls else self.http_port)
        data.server.sni = "upstream.test"

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        # Regression for the former builtin auth metadata leak: the final writer
        # must redact even when another addon introduces it after authentication.
        flow.metadata["proxyauth"] = ("test-alpha", PASSWORD)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        assert "Proxy-Authorization" not in request.headers
        self.requests.append((request.method, request.path))
        if request.path == "/events":
            response = web.StreamResponse(headers={"Content-Type": request.query["content_type"]})
            await response.prepare(request)
            await response.write(b"data: first\n\n")
            await self.finish_events.wait()
            await response.write(b"data: last\n\n")
            await response.write_eof()
            return response
        return web.json_response({"data": {"rateLimit": {"cost": 7}}}, headers={"x-ratelimit-used": "999"})


@dataclass
class Harness:
    settings: Settings
    master: DumpMaster
    origins: LocalOrigins
    metrics: Metrics
    port: int
    client_tls: ssl.SSLContext
    outer_ca: Path
    interception_ca: Path

    async def connect(self, *, tls: bool = True) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(
            "127.0.0.1", self.port, ssl=self.client_tls if tls else None, server_hostname="localhost" if tls else None
        )


@pytest.fixture
async def proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    async def resolve(self: PublicOrigins, host: str, port: int) -> list[str]:
        match host:
            case "api.github.com" | "claude.ai":
                return ["93.184.216.34"]
            case "localhost" | "self-alias.test":
                return ["8.8.4.4"]
            case "private.test":
                return ["127.0.0.1"]
            case "mixed.test":
                return ["93.184.216.34", "10.0.0.1"]
            case _:
                return [host]

    monkeypatch.setattr(PublicOrigins, "resolve", resolve)
    outer = certificates(tmp_path, "outer", "localhost")
    interception = certificates(tmp_path, "interception", None)
    upstream = certificates(tmp_path, "upstream", "upstream.test")
    credentials = tmp_path / "alpha.json"
    credentials.write_text(json.dumps({"test-alpha": PASSWORD}))
    second_credentials = tmp_path / "beta.json"
    second_credentials.write_text(json.dumps({"test-beta": SECOND_PASSWORD}))
    origins = LocalOrigins(0, 0)
    runners = []
    for secure in (False, True):
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", origins.handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        runners.append(runner)
        context = None
        if secure:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(upstream.cert, upstream.key)
        await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context).start()
        if secure:
            origins.https_port = runner.addresses[0][1]
        else:
            origins.http_port = runner.addresses[0][1]
    settings = Settings(
        proxy_hostname="localhost",
        credential_files=[credentials, second_credentials],
        proxy_tls_cert_file=outer.cert,
        proxy_tls_key_file=outer.key,
        interception_ca_cert_file=interception.cert,
        interception_ca_key_file=interception.key,
        confdir=tmp_path / "conf",
        capture_path=tmp_path / "capture" / "raw.flows",
        session_ws_events=tmp_path / "capture" / "sessions.jsonl",
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_ca_file=upstream.ca,
    )
    metrics = Metrics()
    master = create_master(settings, metrics)
    master.addons.add(origins)
    task = asyncio.create_task(master.run())
    try:
        async with asyncio.timeout(10):
            await origins.ready.wait()
        server = master.addons.get("proxyserver")
        assert isinstance(server, Proxyserver)
        client_tls = ssl.create_default_context(cafile=str(outer.ca))
        client_tls.load_verify_locations(cafile=str(interception.ca))
        yield Harness(
            settings, master, origins, metrics, server.listen_addrs()[0][1], client_tls, outer.ca, interception.ca
        )
    finally:
        master.shutdown()
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            for runner in runners:
                await runner.cleanup()


def authorization(client: str = "test-alpha", password: str = PASSWORD) -> str:
    return "Basic " + base64.b64encode(f"{client}:{password}".encode()).decode()


async def request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    method: str,
    target: str,
    auth: str | None,
    connection_close: bool = False,
    duplicate_auth: str | None = None,
) -> tuple[int, bytes]:
    headers = [f"{method} {target} HTTP/1.1", "Host: api.github.com"]
    if auth is not None:
        headers.append(f"Proxy-Authorization: {auth}")
    if duplicate_auth is not None:
        headers.append(f"Proxy-Authorization: {duplicate_auth}")
    if connection_close:
        headers.append("Connection: close")
    headers.append("Content-Length: 0")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    status_line, *header_lines = head.decode().split("\r\n")
    response_headers = {
        key.lower(): value.strip()
        for line in header_lines
        if (key := line.partition(":")[0]) and (value := line.partition(":")[2])
    }
    body = await reader.readexactly(int(response_headers.get("content-length", "0")))
    return int(status_line.split()[1]), body


@pytest.mark.parametrize("method", ["CONNECT", "GET"])
@pytest.mark.parametrize(
    "auth",
    [None, "Basic !!!!", authorization(password="test-private-wrong"), authorization(client="test-private-unknown")],
)
async def test_missing_wrong_auth_never_forwards(proxy: Harness, method: str, auth: str | None) -> None:
    async with asyncio.timeout(5):
        reader, writer = await proxy.connect()
        try:
            target = "api.github.com:443" if method == "CONNECT" else "http://api.github.com/graphql"
            status, _ = await request(reader, writer, method=method, target=target, auth=auth)
            assert status == 407
            assert proxy.origins.dials == 0
            assert proxy.origins.requests == []
        finally:
            writer.close()
            await writer.wait_closed()
    with proxy.settings.capture_path.open("rb") as stream:
        captured = list(io.FlowReader(stream).stream())
    assert len(captured) == 1
    assert isinstance(captured[0], http.HTTPFlow)
    assert "Proxy-Authorization" not in captured[0].request.headers
    assert "proxyauth" not in captured[0].metadata
    assert b"test-private" not in proxy.settings.capture_path.read_bytes()
    assert b"test-private" not in generate_latest(proxy.metrics.registry)


@pytest.mark.parametrize("method", ["GET", "CONNECT"])
async def test_duplicate_proxy_authorization_rejected(proxy: Harness, method: str) -> None:
    async with asyncio.timeout(5):
        reader, writer = await proxy.connect()
        try:
            target = "api.github.com:443" if method == "CONNECT" else "http://mitm.it/"
            status, _ = await request(
                reader, writer, method=method, target=target, auth=authorization(), duplicate_auth=authorization()
            )
            assert status == 407
            assert proxy.origins.dials == 0
        finally:
            writer.close()
            await writer.wait_closed()
    assert PASSWORD.encode() not in proxy.settings.capture_path.read_bytes()
    assert authorization().encode() not in proxy.settings.capture_path.read_bytes()


@pytest.mark.parametrize("method", ["GET", "CONNECT"])
async def test_plaintext_transport_rejected_even_with_correct_auth(proxy: Harness, method: str) -> None:
    async with asyncio.timeout(5):
        reader, writer = await proxy.connect(tls=False)
        try:
            target = "api.github.com:443" if method == "CONNECT" else "http://mitm.it/"
            assert (await request(reader, writer, method=method, target=target, auth=authorization()))[0] == 407
            assert proxy.origins.dials == 0
        finally:
            writer.close()
            await writer.wait_closed()


async def test_inner_https_and_independent_auth_redaction(proxy: Harness) -> None:
    async with asyncio.timeout(10):
        reader, writer = await proxy.connect()
        try:
            assert (await request(reader, writer, method="CONNECT", target="api.github.com:443", auth=authorization()))[
                0
            ] == 200
            await writer.start_tls(proxy.client_tls, server_hostname="api.github.com")
            for _ in range(2):
                status, body = await request(reader, writer, method="GET", target="/graphql", auth=None)
                assert status == 200
                assert json.loads(body)["data"]["rateLimit"]["cost"] == 7
        finally:
            writer.close()
            await writer.wait_closed()
        reader, writer = await proxy.connect()
        try:
            assert (await request(reader, writer, method="CONNECT", target="api.github.com:443", auth=None))[0] == 407
        finally:
            writer.close()
            await writer.wait_closed()
    assert len(proxy.origins.requests) == 2
    assert (
        proxy.metrics.registry.get_sample_value(
            "github_api_proxy_graphql_observed_cost_total", {"client": "test-alpha"}
        )
        == 14
    )
    with proxy.settings.capture_path.open("rb") as stream:
        captured = list(io.FlowReader(stream).stream())
    assert len(captured) == 4
    for flow in captured:
        assert isinstance(flow, http.HTTPFlow)
        assert "Proxy-Authorization" not in flow.request.headers
        assert "proxyauth" not in flow.metadata
    raw = proxy.settings.capture_path.read_bytes()
    assert PASSWORD.encode() not in raw
    assert authorization().encode() not in raw
    assert stat.S_IMODE(proxy.settings.capture_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("content_type", ["text/event-stream", "Text/Event-Stream;%20charset=utf-8"])
async def test_event_stream_arrives_before_eof_and_is_captured(proxy: Harness, content_type: str) -> None:
    reader, writer = await proxy.connect()
    try:
        async with asyncio.timeout(5):
            assert (await request(reader, writer, method="CONNECT", target="claude.ai:443", auth=authorization()))[
                0
            ] == 200
            await writer.start_tls(proxy.client_tls, server_hostname="claude.ai")
            writer.write(f"GET /events?content_type={content_type} HTTP/1.1\r\nHost: claude.ai\r\n\r\n".encode())
            await writer.drain()
            headers = await reader.readuntil(b"\r\n\r\n")
            assert headers.startswith(b"HTTP/1.1 200 ")
            assert b"transfer-encoding: chunked" in headers.lower()
            # The origin cannot finish until the client receives its first event.
            await reader.readuntil(b"data: first\n\n")
            proxy.origins.finish_events.set()
            assert b"data: last\n\n" in await reader.readuntil(b"0\r\n\r\n")
    finally:
        proxy.origins.finish_events.set()
        writer.close()
        await writer.wait_closed()
    with proxy.settings.capture_path.open("rb") as stream:
        captured = [
            flow
            for flow in io.FlowReader(stream).stream()
            if isinstance(flow, http.HTTPFlow) and flow.request.path.startswith("/events?")
        ]
    assert len(captured) == 1
    assert captured[0].response is not None
    assert captured[0].response.raw_content == b"data: first\n\ndata: last\n\n"
    assert "Proxy-Authorization" not in captured[0].request.headers
    assert "proxyauth" not in captured[0].metadata


@pytest.mark.parametrize("next_auth", [None, authorization(password="test-private-wrong")])
async def test_http_auth_is_per_request_and_readiness_is_authenticated(proxy: Harness, next_auth: str | None) -> None:
    async with asyncio.timeout(5):
        reader, writer = await proxy.connect()
        try:
            status, body = await request(reader, writer, method="GET", target="http://mitm.it/", auth=authorization())
            assert (status, body) == (200, b"Authenticated proxy ready\n")
            assert (await request(reader, writer, method="GET", target="http://mitm.it/", auth=next_auth))[0] == 407
            assert proxy.origins.dials == 0
        finally:
            writer.close()
            await writer.wait_closed()


async def test_second_client_and_exact_cloud_block(proxy: Harness) -> None:
    async with asyncio.timeout(5):
        reader, writer = await proxy.connect()
        try:
            auth = authorization("test-beta", SECOND_PASSWORD)
            target = "http://claude.ai/v1/code/github/batch-branch-status?caller=test-private"
            assert (await request(reader, writer, method="POST", target=target, auth=auth))[0] == 429
            assert proxy.origins.dials == 0
            assert (await request(reader, writer, method="GET", target=target, auth=auth))[0] == 200
            assert (await request(reader, writer, method="POST", target=target.replace("?", "/extra?"), auth=auth))[
                0
            ] == 200
        finally:
            writer.close()
            await writer.wait_closed()
    assert len(proxy.origins.requests) == 2
    exposition = generate_latest(proxy.metrics.registry)
    assert b'client="test-beta",route="cloud_batch_branch_status",status="429"' in exposition
    assert b"test-private" not in exposition


@pytest.mark.parametrize("trust_outer", [False, True])
async def test_outer_tls_trust_and_hostname_are_verified(proxy: Harness, trust_outer: bool) -> None:
    context = ssl.create_default_context(cafile=str(proxy.outer_ca if trust_outer else proxy.interception_ca))
    if trust_outer:
        context.load_verify_locations(cafile=str(proxy.interception_ca))
    async with asyncio.timeout(5):
        with pytest.raises(ssl.SSLCertVerificationError):
            await asyncio.open_connection(
                "127.0.0.1", proxy.port, ssl=context, server_hostname="wrong.test" if trust_outer else "localhost"
            )
    assert proxy.origins.dials == 0


async def test_upstream_tls_is_verified_and_error_capture_is_redacted(proxy: Harness) -> None:
    proxy.master.options.update(ssl_verify_upstream_trusted_ca=str(proxy.outer_ca))
    async with asyncio.timeout(10):
        reader, writer = await proxy.connect()
        try:
            assert (await request(reader, writer, method="CONNECT", target="api.github.com:443", auth=authorization()))[
                0
            ] == 200
            await writer.start_tls(proxy.client_tls, server_hostname="api.github.com")
            assert (await request(reader, writer, method="GET", target="/graphql", auth=None))[0] == 502
        finally:
            writer.close()
            # A failed upstream TLS handshake can reset the intercepted tunnel after its 502.
            with contextlib.suppress(ConnectionResetError):
                await writer.wait_closed()
    assert proxy.origins.requests == []
    with proxy.settings.capture_path.open("rb") as stream:
        captured = list(io.FlowReader(stream).stream())
    assert any(isinstance(item, http.HTTPFlow) and item.error is not None for item in captured)
    assert PASSWORD.encode() not in proxy.settings.capture_path.read_bytes()


@pytest.mark.parametrize("target", ["api.github.com:22", "localhost:443", "LOCALHOST.:443"])
async def test_connect_authority_rejected_before_tunnel(proxy: Harness, target: str) -> None:
    async with asyncio.timeout(5):
        reader, writer = await proxy.connect()
        try:
            assert (await request(reader, writer, method="CONNECT", target=target, auth=authorization()))[0] == 403
        finally:
            writer.close()
            await writer.wait_closed()
    assert proxy.origins.dials == 0


async def test_denied_connect_does_not_authorize_later_outer_request(proxy: Harness) -> None:
    async with asyncio.timeout(5):
        reader, writer = await proxy.connect()
        try:
            assert (await request(reader, writer, method="CONNECT", target="localhost:443", auth=authorization()))[
                0
            ] == 403
            assert (await request(reader, writer, method="GET", target="http://mitm.it/", auth=None))[0] == 407
        finally:
            writer.close()
            await writer.wait_closed()
    assert proxy.origins.dials == 0


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "http://private.test/",
        "http://mixed.test/",
        "http://self-alias.test/",
    ],
)
async def test_private_or_self_dns_never_dials(proxy: Harness, target: str) -> None:
    async with asyncio.timeout(5):
        reader, writer = await proxy.connect()
        try:
            assert (await request(reader, writer, method="GET", target=target, auth=authorization()))[0] == 502
        finally:
            writer.close()
            await writer.wait_closed()
    assert proxy.origins.dials == 0
    assert proxy.origins.requests == []


if __name__ == "__main__":
    pytest_bazel.main()
