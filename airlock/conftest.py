"""pytest configuration and shared test infrastructure for airlock tests."""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_public_key
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.mcp_config import MCPServerTypes, RemoteMCPServer
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from mcp import types as mcp_types
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from airlock.coordinator import ActionCoordinator
from airlock.models import Action, ActionKey, ActionStatus, WaitMode, YieldAfterMs
from airlock.oidc_auth import DualVerifierOIDCProxy
from airlock.operator_api import DECIDE_SCOPE, create_operator_api
from airlock.predicates import NeedsHumanDecision, PredicateFn
from airlock.proxy_server import PROPOSE_SCOPE, READ_SCOPE, AirlockServer
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.resource_utils import read_text_json_typed
from mcp_utils.resources import parse_tool_result_as
from util.net import pick_free_port

TEST_NS = MCPMountPrefix("test")


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio to auto mode with function-scoped event loops."""
    config.addinivalue_line("markers", "asyncio: mark test as async")
    config.option.asyncio_mode = "auto"
    # Ensure each test gets its own event loop. The default (None → session scope)
    # causes all tests to share one loop, leading to cross-test contamination when
    # one test's background tasks or anyio cancel scopes outlive the test.
    # config.override_ini is only available from pytest 9.1+; for 9.0.x we write
    # directly to _inicache, which getini() consults on every subsequent call.
    config._inicache["asyncio_default_fixture_loop_scope"] = "function"


# ── Mock OIDC provider ────────────────────────────────────────────────────────


def _rsa_public_key_to_jwks(public_key_pem: str) -> dict:
    """Convert an RSA public key PEM to a JWKS document."""
    pub_key = load_pem_public_key(public_key_pem.encode())
    rsa_numbers = pub_key.public_numbers()  # type: ignore[union-attr]
    assert isinstance(rsa_numbers, RSAPublicNumbers)

    def _int_to_base64url(n: int) -> str:
        byte_length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(byte_length, "big")).rstrip(b"=").decode()

    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": "test-key",
                "n": _int_to_base64url(rsa_numbers.n),
                "e": _int_to_base64url(rsa_numbers.e),
            }
        ]
    }


def _mock_oidc_app(issuer_url: str, jwks: dict) -> Starlette:
    """Starlette app serving OIDC discovery + JWKS endpoints."""
    discovery = {
        "issuer": issuer_url,
        "authorization_endpoint": f"{issuer_url}/authorize",
        "token_endpoint": f"{issuer_url}/token",
        "jwks_uri": f"{issuer_url}/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }

    async def well_known(request: Request) -> JSONResponse:
        return JSONResponse(discovery)

    async def jwks_endpoint(request: Request) -> JSONResponse:
        return JSONResponse(jwks)

    return Starlette(
        routes=[Route("/.well-known/openid-configuration", endpoint=well_known), Route("/jwks", endpoint=jwks_endpoint)]
    )


# ── Shared helpers ────────────────────────────────────────────────────────────


@asynccontextmanager
async def serve_app(app: Starlette, *, port: int):
    """Start a uvicorn server in a dedicated thread; yield when ready; shut down on exit."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("uvicorn thread exited before starting")
        if time.monotonic() > deadline:
            server.should_exit = True
            thread.join(timeout=3.0)
            raise TimeoutError(f"server did not start on port {port}")
        await asyncio.sleep(0.02)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=3.0)


class GateClient(Client, MessageHandler):
    """MCP Client subclass with typed methods for airlock tools.

    Also acts as its own MessageHandler to receive resource-updated notifications
    for ``wait_for``.
    """

    def __init__(self, transport: object, **kwargs: object) -> None:
        self._events: dict[str, anyio.Event] = {}
        super().__init__(transport, message_handler=self, **kwargs)

    async def on_resource_updated(self, notification: mcp_types.ResourceUpdatedNotification) -> None:
        uri = str(notification.params.uri)
        evt = self._events.get(uri)
        if evt is not None:
            evt.set()

    async def call_gate_tool(self, tool_name: str, args: dict[str, object]) -> Action:
        """Call a gate-wrapped tool and return the Action."""
        return parse_tool_result_as(await self.call_tool_mcp(tool_name, args), Action)

    async def call_echo(
        self, text: str, *, justification: str = "test", session_key: str, wait_mode: dict[str, object] | None = None
    ) -> Action:
        """Call test_echo and return the Action."""
        args: dict[str, object] = {"input": {"text": text}, "justification": justification, "session_key": session_key}
        if wait_mode is not None:
            args["wait_mode"] = wait_mode
        return await self.call_gate_tool("test_echo", args)

    async def wait_for(self, key: ActionKey, status: ActionStatus) -> Action:
        """Wait until the action reaches ``status`` via resource-updated notifications."""
        action_uri = f"resource://sessions/{key.session_key}/actions/{key.action_seq}"
        hwm_uri = f"resource://sessions/{key.session_key}/log_hwm"
        await self.session.subscribe_resource(AnyUrl(action_uri))
        await self.session.subscribe_resource(AnyUrl(hwm_uri))
        while True:
            event = anyio.Event()
            self._events[hwm_uri] = event
            self._events[action_uri] = event
            action: Action = await read_text_json_typed(self, action_uri, Action)
            if action.state.status == status:
                self._events.pop(hwm_uri, None)
                self._events.pop(action_uri, None)
                return action
            await event.wait()


def agent_transport(base_url: str, agent_jwt: str):
    """Create an agent-scoped MCP client transport with Bearer auth."""
    return RemoteMCPServer(url=f"{base_url}/mcp", headers={"Authorization": f"Bearer {agent_jwt}"}).to_transport()


class OperatorClient:
    """HTTP client for the operator REST API (/api/*)."""

    def __init__(self, base_url: str, jwt: str) -> None:
        self._base_url = base_url
        self._headers = {"Authorization": f"Bearer {jwt}"}

    async def approve(self, key: ActionKey) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/api/actions/{key.session_key}/{key.action_seq}/approve", headers=self._headers
            )
            resp.raise_for_status()

    async def reject(self, key: ActionKey, reason: str | None = None) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/api/actions/{key.session_key}/{key.action_seq}/reject",
                headers=self._headers,
                json={"reason": reason},
            )
            resp.raise_for_status()

    async def list_actions(self, status: ActionStatus | None = None) -> list[Action]:
        params = {}
        if status is not None:
            params["status"] = status
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self._base_url}/api/actions", headers=self._headers, params=params)
            resp.raise_for_status()
            return [Action.model_validate(a) for a in resp.json()]

    async def get_action(self, key: ActionKey) -> Action:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/api/actions/{key.session_key}/{key.action_seq}", headers=self._headers
            )
            resp.raise_for_status()
            return Action.model_validate(resp.json())


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def free_port() -> int:
    return pick_free_port()


@pytest.fixture
def base_url(free_port: int) -> str:
    return f"http://127.0.0.1:{free_port}"


@pytest.fixture
def agent_client_transport(base_url: str, agent_jwt: str):
    return agent_transport(base_url, agent_jwt)


@pytest.fixture
def operator_client(base_url: str, operator_jwt: str) -> OperatorClient:
    return OperatorClient(base_url, operator_jwt)


@pytest.fixture
def rsa_key_pair():
    return RSAKeyPair.generate()


@pytest.fixture
def agent_jwt(rsa_key_pair, mock_oidc_issuer):
    return rsa_key_pair.create_token(subject="agent", issuer=mock_oidc_issuer, scopes=[PROPOSE_SCOPE, READ_SCOPE])


@pytest.fixture
def operator_jwt(rsa_key_pair, mock_oidc_issuer):
    return rsa_key_pair.create_token(subject="operator", issuer=mock_oidc_issuer, scopes=[DECIDE_SCOPE, READ_SCOPE])


@pytest.fixture
async def coordinator(tmp_path: Path) -> AsyncGenerator[ActionCoordinator]:
    """Temporary coordinator with no backends for pure storage tests."""
    coord = ActionCoordinator(
        db_path=tmp_path / "test.db", backends={}, predicate=lambda ns, tool, args: NeedsHumanDecision()
    )
    async with coord:
        yield coord


class EchoBackend:
    """A simple echo backend for testing, tracking calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.server = FastMCP()

        @self.server.tool()
        async def echo(text: str) -> str:
            self.calls.append(text)
            return f"echoed: {text}"


@pytest.fixture
def echo_backend() -> EchoBackend:
    return EchoBackend()


@pytest.fixture
async def mock_oidc_issuer(rsa_key_pair: RSAKeyPair) -> AsyncGenerator[str]:
    """Start a mock OIDC provider and yield its issuer URL."""
    port = pick_free_port()
    issuer_url = f"http://localhost:{port}"
    jwks = _rsa_public_key_to_jwks(rsa_key_pair.public_key)
    app = _mock_oidc_app(issuer_url, jwks)
    async with serve_app(app, port=port):
        yield issuer_url


GateAppFactory = Callable[..., Starlette]


@pytest.fixture
def make_gate_app(rsa_key_pair: RSAKeyPair, tmp_path: Path, mock_oidc_issuer: str) -> GateAppFactory:
    """Fixture factory: creates a full Starlette app with /mcp and /api mounts.

    Creates coordinator first (owns storage), then passes it to both AirlockServer
    (MCP) and the operator REST API.
    """

    _default_wait = YieldAfterMs(timeout_ms=0)

    def _factory(
        backends: Mapping[MCPMountPrefix, MCPServerTypes | FastMCP],
        *,
        predicate: PredicateFn | None = None,
        default_wait_mode: WaitMode = _default_wait,
    ) -> Starlette:
        config_url = f"{mock_oidc_issuer}/.well-known/openid-configuration"
        auth = DualVerifierOIDCProxy(
            config_url=config_url,
            client_id="test-client",
            client_secret="test-secret",
            base_url="http://localhost/mcp",
            issuer_url="http://localhost",
            require_authorization_consent=False,
        )
        coordinator = ActionCoordinator(
            db_path=tmp_path / "gate.db",
            backends=dict(backends),
            predicate=predicate or (lambda ns, tool, args: NeedsHumanDecision()),
        )
        gate = AirlockServer(
            public_base_url="http://localhost", default_wait_mode=default_wait_mode, coordinator=coordinator, auth=auth
        )
        mcp_app = gate.http_app(path="/")
        operator_app = create_operator_api(coordinator=coordinator, oidc_issuer=mock_oidc_issuer)

        mcp_lifespan = mcp_app.router.lifespan_context

        @asynccontextmanager
        async def _lifespan(app):
            async with coordinator, mcp_lifespan(app):
                yield

        return Starlette(routes=[Mount("/mcp", app=mcp_app), Mount("/api", app=operator_app)], lifespan=_lifespan)

    return _factory


@pytest.fixture
def echo_gate_app(make_gate_app: GateAppFactory, echo_backend: EchoBackend) -> Starlette:
    """Gate app with a single echo backend under TEST_NS."""
    return make_gate_app({TEST_NS: echo_backend.server})


@pytest.fixture
def session_key() -> str:
    return "test-session"
