"""pytest configuration and shared test infrastructure for airlock tests."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import AsyncGenerator, Callable, Generator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import anyio
import pytest
import uvicorn
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.mcp_config import MCPServerTypes, RemoteMCPServer
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from mcp import types as mcp_types
from pydantic import AnyUrl
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette
from starlette.routing import Mount
from testcontainers.postgres import PostgresContainer

from airlock.config import Settings
from airlock.models import Action, ActionKey, ActionStatus, WaitMode
from airlock.oauth.provider import OAuthConfig
from airlock.predicates import NeedsHumanDecision, PredicateFn
from airlock.proxy_server import _DEFAULT_WAIT_MODE, DECIDE_SCOPE, PROPOSE_SCOPE, READ_SCOPE, AirlockServer
from airlock.storage import ActionStorage
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.resource_utils import read_text_json_typed
from mcp_utils.resources import parse_tool_result_as
from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.net import pick_free_port
from util.oci import load_oci_image
from util.testing.postgres import force_drop_database


@asynccontextmanager
async def as_remote_server(server: FastMCP) -> AsyncGenerator[RemoteMCPServer]:
    """Start a FastMCP server as HTTP and yield a RemoteMCPServer pointing at it."""
    port = pick_free_port()
    mcp_app = server.http_app(path="/")
    starlette = Starlette(routes=[Mount("/mcp", app=mcp_app)], lifespan=mcp_app.lifespan)
    async with serve_app(starlette, port=port):
        yield RemoteMCPServer(url=f"http://127.0.0.1:{port}/mcp")


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
        self,
        text: str,
        *,
        justification: str = "test",
        session_key: UUID | str,
        wait_mode: dict[str, object] | None = None,
    ) -> Action:
        """Call test_echo and return the Action."""
        args: dict[str, object] = {
            "input": {"text": text},
            "justification": justification,
            "session_key": str(session_key),
        }
        if wait_mode is not None:
            args["wait_mode"] = wait_mode
        return await self.call_gate_tool("test_echo", args)

    async def approve(self, key: ActionKey) -> None:
        await self.call_tool_mcp("approve_action", {"key": key.model_dump(mode="json")})

    async def reject(self, key: ActionKey, reason: str | None = None) -> None:
        await self.call_tool_mcp("reject_action", {"key": key.model_dump(mode="json"), "reason": reason})

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


def operator_transport(base_url: str, operator_jwt: str):
    """Create an operator-scoped MCP client transport with Bearer auth."""
    return RemoteMCPServer(url=f"{base_url}/mcp", headers={"Authorization": f"Bearer {operator_jwt}"}).to_transport()


# ── PostgreSQL testcontainer fixtures ─────────────────────────────────────────


@pytest.fixture(scope="session", autouse=True)
def _preload_postgres() -> None:
    """Pre-load PostgreSQL and Ryuk images into the Docker daemon."""
    load_oci_image(RYUK)
    load_oci_image(POSTGRES_18)


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    """Session-scoped PostgreSQL container shared across all tests."""
    container = PostgresContainer(image=POSTGRES_18.tag, username="postgres", password="postgres", dbname="postgres")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: PostgresContainer) -> str:
    """asyncpg URL for the admin (postgres) database in the testcontainer."""
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    return f"postgresql+asyncpg://postgres:postgres@{host}:{port}/postgres"


@pytest.fixture
async def db_url(postgres_admin_url: str, request: pytest.FixtureRequest) -> AsyncGenerator[str]:
    """Per-test isolated PostgreSQL database URL, named after the test."""
    db_name = re.sub(r"[^a-z0-9]", "_", request.node.name.lower())[:45]
    admin_engine = create_async_engine(postgres_admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    await admin_engine.dispose()
    yield make_url(postgres_admin_url).set(database=db_name).render_as_string(hide_password=False)
    await force_drop_database(postgres_admin_url, db_name)


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
def operator_client_transport(base_url: str, operator_jwt: str):
    return operator_transport(base_url, operator_jwt)


@pytest.fixture
def rsa_key_pair():
    return RSAKeyPair.generate()


@pytest.fixture
def agent_jwt(rsa_key_pair):
    return rsa_key_pair.create_token(subject="agent", scopes=[PROPOSE_SCOPE, READ_SCOPE])


@pytest.fixture
def operator_jwt(rsa_key_pair):
    return rsa_key_pair.create_token(subject="operator", scopes=[DECIDE_SCOPE, READ_SCOPE])


@pytest.fixture
async def storage(db_url: str) -> AsyncGenerator[ActionStorage]:
    """Per-test ActionStorage backed by an isolated PostgreSQL database."""
    store = await ActionStorage.initialize(db_url)
    try:
        yield store
    finally:
        await store.close()


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


GateServerFactory = Callable[..., AirlockServer]


@pytest.fixture
def make_gate_server(rsa_key_pair: RSAKeyPair, db_url: str) -> GateServerFactory:
    """Fixture factory: creates a JWTVerifier-protected AirlockServer.

    ``predicate`` defaults to NeedsHumanDecision for all tools.
    """

    def _factory(
        backends: Mapping[MCPMountPrefix, MCPServerTypes],
        *,
        predicate: PredicateFn | None = None,
        default_wait_mode: WaitMode = _DEFAULT_WAIT_MODE,
    ) -> AirlockServer:
        settings = Settings(
            backends=dict(backends),
            db_url=db_url,
            public_base_url="http://test",
            predicate_path=Path("/unused"),
            oidc_issuer="https://unused.example.com",
            oidc_client_id="test",
            oauth=OAuthConfig(providers=[]),
            port=0,
            default_wait_mode=default_wait_mode,
        )
        return AirlockServer(
            settings,
            predicate=predicate or (lambda ns, tool, args: NeedsHumanDecision()),
            auth=JWTVerifier(public_key=rsa_key_pair.public_key),
        )

    return _factory


def gate_http_app(gate: AirlockServer) -> Starlette:
    """Wrap an AirlockServer in a Starlette app with /mcp mount."""
    mcp_app = gate.http_app(path="/")
    return Starlette(routes=[Mount("/mcp", app=mcp_app)], lifespan=mcp_app.lifespan)


GateAppFactory = Callable[..., Starlette]


@pytest.fixture
def make_gate_app(make_gate_server: GateServerFactory) -> GateAppFactory:
    """Convenience fixture: creates a gate server and wraps it in a Starlette app."""

    def _factory(backends: Mapping[MCPMountPrefix, MCPServerTypes], **kwargs: object) -> Starlette:
        return gate_http_app(make_gate_server(backends, **kwargs))

    return _factory


@pytest.fixture
async def echo_http(echo_backend: EchoBackend) -> AsyncGenerator[RemoteMCPServer]:
    """Start the echo backend as an HTTP server; yield its RemoteMCPServer spec."""
    async with as_remote_server(echo_backend.server) as spec:
        yield spec


@pytest.fixture
def echo_gate_app(make_gate_app: GateAppFactory, echo_http: RemoteMCPServer) -> Starlette:
    """Gate app with a single echo backend under TEST_NS."""
    return make_gate_app({TEST_NS: echo_http})


@pytest.fixture
def session_key() -> UUID:
    return uuid4()
