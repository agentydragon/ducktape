"""pytest configuration for approval_gate tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path

import pytest
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.mcp_config import RemoteMCPServer
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from starlette.applications import Starlette
from starlette.routing import Mount

from approval_gate.auth import DECIDE_SCOPE, PROPOSE_SCOPE, READ_SCOPE, AuthentikHeaderNormalizer
from approval_gate.models import Action, ActionKey
from approval_gate.predicates import NeedsHumanDecision
from approval_gate.proxy_server import ApprovalGateServer
from approval_gate.storage import ActionStorage
from mcp_infra.prefix import MCPMountPrefix
from mcp_utils.resources import parse_tool_result_as

_TEST_NS = MCPMountPrefix("test")


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


class GateClient(Client):
    """MCP Client subclass with typed methods for approval gate tools."""

    async def call_gate_tool(self, tool_name: str, args: dict[str, object]) -> ActionKey:
        """Call a gate-wrapped tool and parse the ActionKey from the result."""
        return parse_tool_result_as(await self.call_tool_mcp(tool_name, args), ActionKey)

    async def call_echo(self, text: str, *, justification: str = "test", session_key: str) -> ActionKey:
        return await self.call_gate_tool(
            "test_echo", {"input": {"text": text}, "justification": justification, "session_key": session_key}
        )

    async def approve(self, key: ActionKey) -> Action:
        return parse_tool_result_as(await self.call_tool_mcp("approve_action", {"key": key.model_dump()}), Action)

    async def reject(self, key: ActionKey, reason: str | None = None) -> Action:
        return parse_tool_result_as(
            await self.call_tool_mcp("reject_action", {"key": key.model_dump(), "reason": reason}), Action
        )


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
async def storage(tmp_path: Path) -> AsyncGenerator[ActionStorage]:
    """Temporary in-memory storage for tests."""
    store = await ActionStorage.initialize(tmp_path / "test.db")
    try:
        yield store
    finally:
        await store.close()


def agent_transport(base_url: str, agent_jwt: str):
    """Create an agent-scoped MCP client transport with Bearer auth."""
    return RemoteMCPServer(url=f"{base_url}/mcp", headers={"Authorization": f"Bearer {agent_jwt}"}).to_transport()


def operator_transport(base_url: str, operator_jwt: str):
    """Create an operator-scoped MCP client transport with x-authentik-jwt header."""
    return RemoteMCPServer(url=f"{base_url}/mcp", headers={"x-authentik-jwt": operator_jwt}).to_transport()


GateAppFactory = Callable[[FastMCP, Path], tuple[Starlette, ApprovalGateServer]]


@pytest.fixture
def make_gate_app(rsa_key_pair: RSAKeyPair) -> GateAppFactory:
    """Fixture factory: creates a Starlette app with JWTVerifier-protected ApprovalGateServer."""

    def _factory(backend: FastMCP, db_path: Path) -> tuple[Starlette, ApprovalGateServer]:
        auth = JWTVerifier(public_key=rsa_key_pair.public_key)
        gate = ApprovalGateServer(
            backends={_TEST_NS: backend},
            db_path=db_path,
            predicate=lambda ns, tool, args: NeedsHumanDecision(),
            public_base_url="http://test",
            auth=auth,
        )
        mcp_app = gate.http_app(path="/")
        mcp_app_with_header_norm = AuthentikHeaderNormalizer(mcp_app)
        app = Starlette(routes=[Mount("/mcp", app=mcp_app_with_header_norm)], lifespan=mcp_app.lifespan)
        return app, gate

    return _factory
