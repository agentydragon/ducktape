"""Real Go app processes and an external HTTP gate holding browser replay bytes."""

import asyncio
import json
import os
import signal
import socket
import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from urllib.parse import parse_qs
from uuid import uuid4

import grpc
import httpx
import uvicorn
from playwright.async_api import Page
from sqlalchemy.engine import make_url
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.oidc import OIDCSettings
from agentplane.app.testing.http2_proxy import BrowserCertificate, browser_certificate
from agentplane.app.testing.model_test_data import TEST_REASONING_EFFORTS
from agentplane.app.testing.notifications_operator import FakeNotificationsOperator
from agentplane.grpc.serving import serving
from agentplane.history_service.client import HistoryServiceClient
from agentplane.notification_service import notifications_operator_pb2_grpc
from util.bazel.runfiles import get_required_path
from util.net import Listener, listen_tcp
from util.testing.asgi import serve_app_in_loop
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf


@dataclass(frozen=True)
class SandboxesAccess:
    kubeconfig: Path
    namespace: str


@dataclass(frozen=True)
class GatewayAccess:
    target: str
    token_file: Path


@dataclass(frozen=True)
class ReplayHeld:
    cursor: int


class ReplayAction(StrEnum):
    RELEASE = "release"
    DISCONNECT = "disconnect"


class ReplayGate:
    def __init__(self, after_cursor: int) -> None:
        self.after_cursor = after_cursor
        self.observations: asyncio.Queue[ReplayHeld] = asyncio.Queue()
        self.release: asyncio.Future[ReplayAction] | None = None

    async def hold(self, cursor: int) -> ReplayAction:
        if self.release is None:
            self.release = asyncio.get_running_loop().create_future()
        if not self.release.done():
            self.observations.put_nowait(ReplayHeld(cursor))
        pending = self.release
        action = await asyncio.shield(pending)
        if action is ReplayAction.DISCONNECT and self.release is pending:
            self.release = None
        return action

    def send(self, action: ReplayAction) -> None:
        assert self.release is not None
        assert not self.release.done()
        self.release.set_result(action)


@dataclass
class AppProcess:
    url: str
    process: asyncio.subprocess.Process
    stderr: Path
    cookies: httpx.Cookies = field(default_factory=httpx.Cookies)
    gate: ReplayGate | None = None
    public_url: str | None = None
    login_ca: ssl.SSLContext | None = None
    oidc_issuer: str | None = None

    async def replay_held(self) -> ReplayHeld:
        assert self.gate is not None
        return await self.gate.observations.get()

    def release_replay(self) -> None:
        assert self.gate is not None
        self.gate.send(ReplayAction.RELEASE)

    def disconnect_replay(self) -> None:
        assert self.gate is not None
        self.gate.send(ReplayAction.DISCONNECT)

    async def kill(self) -> None:
        self.process.kill()
        assert await self.process.wait() == -signal.SIGKILL

    async def authenticate(self) -> None:
        assert self.public_url is not None
        assert self.login_ca is not None
        async with httpx.AsyncClient(verify=self.login_ca, follow_redirects=True) as login:
            await login.get(self.public_url + "/auth/login")
            response = await login.get(self.public_url + "/auth/me")
            response.raise_for_status()
            assert response.json()["username"] == "test-operator", response.text
            self.cookies = login.cookies

    async def login(self, page: Page) -> None:
        """Install the cookie obtained through the real OIDC authorization-code exchange."""
        await self.authenticate()
        assert self.public_url is not None
        for cookie in self.cookies.jar:
            assert cookie.value is not None
            await page.context.add_cookies(
                [
                    {
                        "name": cookie.name,
                        "value": cookie.value,
                        "url": self.public_url,
                        "secure": cookie.secure,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )


@dataclass
class OIDCProviderOptions:
    """Optional signed mock-provider behavior for app federation integration tests."""

    authentik_compatible: bool = False
    exchange_token_audience: str | None = None
    expected_exchange_client_id: str | None = None
    reject_refresh: bool = False
    refresh_tokens: list[str] = field(default_factory=list)
    issued_refresh_tokens: list[str] = field(default_factory=list)
    client_assertions: list[str] = field(default_factory=list)


def fixture_config() -> dict[str, object]:
    unused = "http://127.0.0.1:1"
    return {
        "models": {
            "models": [
                {
                    "model": name,
                    "display_name": "Test " + name.removeprefix("test-").replace("-", " ").title(),
                    "reasoning_efforts": list(TEST_REASONING_EFFORTS),
                }
                for name in (
                    "test-model-before",
                    "test-model-after",
                    "test-model",
                    "test-claude-model",
                    "test-codex-model",
                )
            ],
            "harnesses": {
                harness: [
                    "test-model-before",
                    "test-model-after",
                    "test-model",
                    "test-claude-model",
                    "test-codex-model",
                ]
                for harness in ("HARNESS_CLAUDE", "HARNESS_CODEX")
            },
        },
        "default_policy_sets": [],
        "runner_gateway_target": "127.0.0.1:1",
        "runner_gateway_control_target": "127.0.0.1:1",
        "session_dispatcher_target": "127.0.0.1:1",
        "history_service_target": "127.0.0.1:1",
        "notifications_url": unused,
        "notifications_token_file": "/test-unused-token",
        "runner_gateway_token_file": "/test-unused-token",
        "session_dispatcher_token_file": "/test-unused-token",
        "history_service_token_file": "/test-unused-token",
        "egress_admin_url": unused,
        "connection_service_url": unused,
        "action_federation": {
            "mode": "direct",
            "service_url": unused,
            "login_jwks_uri": unused + "/jwks",
            "login_token_profile": "dex",
            "target": {
                "issuer": unused,
                "audience": "test-agentplane",
                "jwks_uri": unused + "/jwks",
                "token_profile": "dex",
            },
            "scope": "test-agentplane",
        },
    }


@asynccontextmanager
async def spawn_go_app(
    config: dict[str, object],
    *,
    oidc: OIDCSettings | None = None,
    env: dict[str, str] | None = None,
    listener: Listener | None = None,
) -> AsyncIterator[AppProcess]:
    configured = fixture_config() | config
    configured["database_url"] = (
        make_url(str(configured["database_url"]))
        .set(drivername="postgresql", query={"sslmode": "disable"})
        .render_as_string(hide_password=False)
    )
    stderr = undeclared_outputs_dir() / f"go-app-{uuid4().hex}.log"
    with (
        nullcontext(listener) if listener is not None else listen_tcp() as endpoint,
        TemporaryDirectory(prefix="go-app-") as directory,
    ):
        url = f"http://{endpoint.host}:{endpoint.port}"
        listener_fd = endpoint.socket.fileno()
        configured["listen_address"] = f"fd:{listener_fd}"
        config_file = Path(directory) / "config.json"
        config_file.write_text(json.dumps(configured))
        process_env = {key: value for key, value in os.environ.items() if not key.startswith("AGENTPLANE_")}
        process_env.update({"AGENTPLANE_CONFIG_FILE": str(config_file), "OTEL_SDK_DISABLED": "true"})
        if oidc is not None:
            process_env.update(
                {f"AGENTPLANE_OIDC_{key.upper()}": str(value) for key, value in oidc.model_dump().items()}
            )
        process_env.update(env or {})
        with stderr.open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                get_required_path("_main/agentplane/app/go/agentplane-app_/agentplane-app"),
                pass_fds=(listener_fd,),
                env=process_env,
                stdout=log,
                stderr=log,
            )
        try:
            async with httpx.AsyncClient(timeout=1) as client:
                async for attempt in AsyncRetrying(
                    stop=stop_after_delay(20),
                    wait=wait_fixed(0.05),
                    retry=retry_if_exception_type(httpx.HTTPError),
                    reraise=True,
                ):
                    with attempt:
                        assert process.returncode is None, stderr.read_text()
                        (await client.get(url + "/readyz")).raise_for_status()
            yield AppProcess(url, process, stderr)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    async with asyncio.timeout(10):
                        await process.wait()
                except TimeoutError:
                    process.send_signal(signal.SIGQUIT)
                    await process.wait()
                    raise AssertionError(stderr.read_text()) from None
            assert process.returncode in (0, -signal.SIGKILL), stderr.read_text()


class ReadyServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = asyncio.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        assert self.started
        self.ready.set()


@asynccontextmanager
async def oidc_provider(
    directory: Path,
    *,
    subject: str = "test-operator",
    extra_claims: dict[str, object] | None = None,
    id_failure: str | None = None,
    reject_token_exchange: bool = False,
    options: OIDCProviderOptions | None = None,
) -> AsyncIterator[tuple[str, Path]]:
    provider_options = options or OIDCProviderOptions()
    certificate = browser_certificate(directory)
    certificate.port_reservation.close()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        issuer_path = "/application/o/agentplane/" if provider_options.authentik_compatible else ""
        url = f"https://127.0.0.1:{listener.getsockname()[1]}{issuer_path}"
        private, public = generate_rsa_keypair()
        claims = {"preferred_username": "test-operator"} | (extra_claims or {})
        failures: dict[str, tuple[str, object]] = {
            "issuer": ("iss", "https://wrong-issuer.invalid"),
            "audience": ("aud", "wrong-client"),
            "azp": ("azp", "wrong-client"),
            "expired": ("exp", 1),
            "subject": ("sub", ""),
        }
        if id_failure == "signature":
            _, public = generate_rsa_keypair()
        elif id_failure is not None:
            key, value = failures[id_failure]
            claims[key] = value
        app = build_mock_oidc_app(
            issuer_url=url,
            private_key=private,
            public_key=public,
            subject=subject,
            extra_id_token_claims=claims,
            authentik_compatible=provider_options.authentik_compatible,
            exchange_token_audience=provider_options.exchange_token_audience,
            expected_exchange_client_id=provider_options.expected_exchange_client_id,
            on_client_assertion=provider_options.client_assertions.append,
        )
        token_path = "/application/o/token/" if provider_options.authentik_compatible else "/token"
        token_route = next(route for route in app.routes if isinstance(route, Route) and route.path == token_path)
        issue_token = token_route.endpoint

        async def token(request: Request) -> Response:
            form = parse_qs((await request.body()).decode())
            grant_type = form.get("grant_type", ["authorization_code"])[0]
            if grant_type == "refresh_token":
                provider_options.refresh_tokens.append(form["refresh_token"][0])
                if provider_options.reject_refresh:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
            response = cast(Response, await issue_token(request))
            if grant_type in {"authorization_code", "refresh_token"} and response.status_code == 200:
                refresh_token = json.loads(bytes(response.body)).get("refresh_token")
                if isinstance(refresh_token, str):
                    provider_options.issued_refresh_tokens.append(refresh_token)
            return response

        app.routes.remove(token_route)
        app.routes.insert(0, Route(token_path, token, methods=["POST"]))
        if reject_token_exchange:

            async def reject(_request: Request) -> Response:
                return Response(status_code=403)

            app.routes.insert(0, Route("/token", reject, methods=["POST"]))
        ca = certificate.directory / "certificate.pem"
        server = ReadyServer(
            uvicorn.Config(
                app, log_level="warning", ssl_certfile=str(ca), ssl_keyfile=str(certificate.directory / "key.pem")
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                await server.ready.wait()
            yield url, ca
        finally:
            server.should_exit = True
            await task


class BrowserProxy:
    """Forward real app HTTP bytes; hold selected Electric chunks at the test's delivery fence."""

    def __init__(self, client: httpx.AsyncClient, history: HistoryServiceClient, gate: ReplayGate | None) -> None:
        self.client, self.history, self.gate = client, history, gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["type"] == "http"
        path = scope["path"]
        if path == "/upstream/actions/v1/action-requests:watch":
            await send(
                {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/x-ndjson")]}
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"result":{"snapshot":{"actionRequests":[]}}}\n{"result":{"snapshotComplete":{}}}\n',
                    "more_body": True,
                }
            )
            while (await receive())["type"] != "http.disconnect":
                pass
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        forwarding = asyncio.create_task(self._forward(scope, bytes(body), send))
        disconnected = asyncio.create_task(_disconnected(receive))
        try:
            done, _ = await asyncio.wait((forwarding, disconnected), return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()
        finally:
            forwarding.cancel()
            disconnected.cancel()
            await asyncio.gather(forwarding, disconnected, return_exceptions=True)

    async def _forward(self, scope: Scope, body: bytes, send: Send) -> None:
        path = scope["path"]
        headers = [
            (key, value) for key, value in scope["headers"] if key not in (b"host", b"connection", b"accept-encoding")
        ]
        query = scope["query_string"].decode()
        async with self.client.stream(
            scope["method"], path + ("?" + query if query else ""), headers=headers, content=bytes(body)
        ) as response:
            outgoing = [
                (key, value)
                for key, value in response.headers.raw
                if key.lower() not in (b"transfer-encoding", b"connection")
            ]
            streaming = response.headers.get("content-type", "").startswith(
                ("text/event-stream", "application/x-ndjson")
            ) or (path.endswith(":watch") and response.is_success)
            selected = self.gate is not None and path.endswith("/sync/entities")
            started = False
            if streaming and not selected:
                await send({"type": "http.response.start", "status": response.status_code, "headers": outgoing})
                started = True
            async for chunk in response.aiter_raw() if streaming else _whole_body(response):
                if selected:
                    assert self.gate is not None
                    summary = await self.history.get_session_summary(path.split("/")[2])
                    cursor = summary.folded_through
                    if cursor > self.gate.after_cursor and await self.gate.hold(cursor) is ReplayAction.DISCONNECT:
                        if not started:
                            await send({"type": "http.response.start", "status": 503, "headers": []})
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                        return
                if not started:
                    await send({"type": "http.response.start", "status": response.status_code, "headers": outgoing})
                    started = True
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            if not started:
                await send({"type": "http.response.start", "status": response.status_code, "headers": outgoing})
            await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _disconnected(receive: Receive) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass


async def _whole_body(response: httpx.Response) -> AsyncIterator[bytes]:
    yield b"".join([chunk async for chunk in response.aiter_raw()])


@asynccontextmanager
async def app_process(
    database_url: str,
    history: HistoryServiceClient,
    *,
    sandboxes: SandboxesAccess,
    gateway: GatewayAccess | None,
    browser_login: bool = False,
    replay_after: int | None = None,
    electric_url: str | None = None,
    config: dict[str, object] | None = None,
    certificate: BrowserCertificate | None = None,
    oidc_provider_options: OIDCProviderOptions | None = None,
    config_for_oidc_issuer: Callable[[str], dict[str, object]] | None = None,
    oidc_env: dict[str, str] | None = None,
) -> AsyncIterator[AppProcess]:
    with TemporaryDirectory(prefix="app-oidc-") as directory, listen_tcp() as listener:
        port = listener.port
        token = Path(directory) / "unused-token"
        token.write_text("test-unused-token")
        notices = grpc.aio.server()
        notifications_operator_pb2_grpc.add_NotificationsOperatorServicer_to_server(
            FakeNotificationsOperator(), notices
        )
        async with (
            oidc_provider(Path(directory) / "tls", options=oidc_provider_options) as (issuer, ca),
            serving(notices, "127.0.0.1:0", grace_s=0) as notices_port,
        ):
            settings: dict[str, object] = {
                "database_url": database_url,
                "kubeconfig": str(sandboxes.kubeconfig),
                "sandbox_namespace": sandboxes.namespace,
                "history_service_target": history.target,
                "history_service_token_file": str(history.token_file),
                "notifications_url": f"http://127.0.0.1:{notices_port}",
                "notifications_token_file": str(token),
            }
            if gateway is not None:
                settings.update(
                    {
                        "runner_gateway_target": gateway.target,
                        "runner_gateway_control_target": gateway.target,
                        "session_dispatcher_target": gateway.target,
                        "runner_gateway_token_file": str(gateway.token_file),
                        "session_dispatcher_token_file": str(gateway.token_file),
                    }
                )
            if electric_url is not None:
                settings["electric_url"] = electric_url
            oidc = OIDCSettings(
                issuer=issuer,
                client_id="test-agentplane",
                client_secret="test-secret",
                session_secret="test-session-secret",
                public_base_url=certificate.url if certificate is not None else f"http://127.0.0.1:{port}",
            )
            configured = settings.copy()
            if config_for_oidc_issuer is not None:
                configured |= config_for_oidc_issuer(issuer)
            configured |= config or {}
            async with spawn_go_app(
                configured, oidc=oidc, env={"SSL_CERT_FILE": str(ca)} | (oidc_env or {}), listener=listener
            ) as app:
                app.public_url = oidc.public_base_url
                app.login_ca = ssl.create_default_context(cafile=str(ca))
                app.oidc_issuer = issuer
                if certificate is None:
                    await app.authenticate()
                else:
                    app.login_ca.load_verify_locations(cafile=str(certificate.directory / "certificate.pem"))
                app.gate = ReplayGate(replay_after) if replay_after is not None else None
                if browser_login or app.gate is not None:
                    async with (
                        httpx.AsyncClient(base_url=app.url, timeout=65) as upstream,
                        serve_app_in_loop(BrowserProxy(upstream, history, app.gate)) as proxy,
                    ):
                        app.url = proxy
                        yield app
                else:
                    yield app
