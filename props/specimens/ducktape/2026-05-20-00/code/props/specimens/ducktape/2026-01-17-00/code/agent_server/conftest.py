from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import docker
import mcp.types
import pytest
from fastapi.testclient import TestClient
from fastmcp.client import Client
from fastmcp.exceptions import ToolError
from fastmcp.mcp_config import MCPConfig, MCPServerTypes
from fastmcp.server import FastMCP
from fastmcp.tools import FunctionTool
from pydantic import BaseModel

from agent_core.events import EventType, ToolCall, ToolCallOutput, UserText
from agent_core.handler import FinishOnTextMessageHandler
from agent_core_testing.fixtures import RecordingHandler
from agent_server.approvals import load_default_policy_source
from agent_server.mcp.approval_policy.engine import PolicyEngine
from agent_server.persist.events import EventRecord
from agent_server.persist.sqlite import SQLitePersistence
from agent_server.persist.types import AgentMetadata
from agent_server.policies.loader import approve_all_policy_text
from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest
from agent_server.policy_eval.container import ContainerPolicyEvaluator
from agent_server.runtime.container import AgentContainerCompositor
from agent_server.server.app import create_app
from agent_server.server.protocol import FunctionCallOutput
from agent_server.server.state import new_state
from agent_server.testing.approval_policy_testdata import fetch_policy, make_policy
from mcp_infra.compositor.notifications_buffer import NotificationsBuffer
from mcp_infra.compositor.server import Compositor
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.mcp_types import McpServerSpecs
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.testing.fixtures import make_container_opts
from mcp_infra.testing.simple_servers import SendMessageInput
from openai_utils.model import OpenAIModelProto
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# Test server mount name used in fixtures
TEST_BACKEND_SERVER_NAME = "backend"

# Load external fixture modules so they're available in xdist workers
pytest_plugins = (
    "mcp_infra.testing.fixtures",  # Shared mcp_infra fixtures
    "agent_core_testing.fixtures",  # Core agent fixtures (make_test_agent, etc.)
    "agent_core_testing.responses",  # make_step_runner, responses_factory, etc.
)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip tests that require Docker or the runtime image."""
    if item.get_closest_marker("requires_docker") is None and item.get_closest_marker("requires_runtime_image") is None:
        return
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as exc:
        pytest.skip(f"Docker not available: {exc}")

    # Check for runtime image if marker is present
    if item.get_closest_marker("requires_runtime_image") is not None:
        try:
            client.images.get("adgn-runtime:latest")
        except docker.errors.ImageNotFound:
            with suppress(Exception):
                client.close()
            pytest.skip("Runtime image not loaded. Run: bazel run //agent_server:load")

    with suppress(Exception):
        client.close()


# --- Pytest fixtures ---


@pytest.fixture
def docker_client():
    """Sync docker client fixture."""
    return docker.from_env()


@pytest.fixture
async def docker_exec_server_py312slim(async_docker_client):
    """Canonical Docker exec server using python:3.12-slim image."""
    opts = make_container_opts("python:3.12-slim")
    return ContainerExecServer(async_docker_client, opts)


@pytest.fixture
async def mcp_client_box(docker_exec_server_py312slim, compositor, compositor_client):
    """MCP client with box Docker exec server (no policy gateway)."""
    await compositor.mount_inproc(MCPMountPrefix("box"), docker_exec_server_py312slim)
    return compositor_client


@pytest.fixture
async def sqlite_persistence(tmp_path):
    """Create isolated SQLite persistence in per-test tmpdir."""
    p = SQLitePersistence(tmp_path / "agent.sqlite")
    await p.ensure_schema()
    return p


@pytest.fixture
def test_agent_id(request: pytest.FixtureRequest) -> str:
    """Generate a sanitized agent ID from the test node ID."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", request.node.nodeid) or "tests"


@pytest.fixture
async def mock_registry(sqlite_persistence):
    """Create mock infrastructure registry using real persistence.

    Used by mcp_bridge and mcp/agents tests. Mocks agent container tracking
    while using real persistence for data storage.
    """
    registry = MagicMock()
    registry.persistence = sqlite_persistence
    registry.list_agents.return_value = []
    registry.is_external.return_value = False
    return registry


async def _mount_servers(comp: Compositor, servers: McpServerSpecs) -> None:
    """Mount all servers from McpServerSpecs dict onto a compositor.

    Validates that all servers are FastMCP instances and mounts them in-process.
    """
    for name, srv in servers.items():
        if not isinstance(srv, FastMCP):
            raise TypeError(f"invalid server for {name!r}: {type(srv).__name__}")
        await comp.mount_inproc(MCPMountPrefix(name), srv)


@pytest.fixture
def make_buffered_client():
    """Async helper to open a Compositor + Client with NotificationsBuffer.

    Yields (client, compositor, buffer) so tests can read buffered notifications
    or pass buffer.poll into handlers.
    """

    @asynccontextmanager
    async def _open(servers: McpServerSpecs):
        # Pass explicit version to avoid importlib.metadata.version() lookup
        async with Compositor(version="1.0.0-test") as comp:
            await _mount_servers(comp, servers)
            buf = NotificationsBuffer(compositor=comp)
            async with Client(comp, message_handler=buf.handler) as sess:
                yield sess, comp, buf

    return _open


@pytest.fixture
async def approval_policy_server(sqlite_persistence, async_docker_client) -> PolicyEngine:
    """PolicyEngine fixture that owns .reader, .proposer and .approver sub-servers."""
    return PolicyEngine(
        agent_id="tests",
        persistence=sqlite_persistence,
        policy_source=load_default_policy_source(),
        docker_client=async_docker_client,
    )


@pytest.fixture
async def make_approval_policy_server(
    sqlite_persistence, test_agent_id, async_docker_client
) -> Callable[[str], Awaitable[PolicyEngine]]:
    """Factory producing PolicyEngine instances with per-test defaults."""

    async def _make(policy_source: str) -> PolicyEngine:
        # Create agent in DB first to satisfy FK constraints
        agent_id_resolved = await sqlite_persistence.create_agent(
            mcp_config=MCPConfig(), metadata=AgentMetadata(preset="test")
        )
        return PolicyEngine(
            agent_id=agent_id_resolved,
            persistence=sqlite_persistence,
            policy_source=policy_source,
            docker_client=async_docker_client,
        )

    return _make


@pytest.fixture
async def policy_evaluator(async_docker_client, approval_policy_server: PolicyEngine) -> ContainerPolicyEvaluator:
    """Container-backed policy evaluator using the default policy engine.

    Deduplicates setup across tests that need to call policy.decide(...).
    Requires Docker (tests should mark with @pytest.mark.requires_docker).
    """
    return ContainerPolicyEvaluator(agent_id="tests", docker_client=async_docker_client, engine=approval_policy_server)


# ---- Standard policy text fixtures (string sources) ----


@pytest.fixture
def policy_allow_all() -> str:
    """Return the text of the approve-all policy from packaged resources."""
    return approve_all_policy_text()


@pytest.fixture
def policy_ui_send_message_allow() -> str:
    result: str = make_policy(
        decision_expr="ApprovalDecision.ALLOW",
        server="ui",
        tool="send_message",
        default=ApprovalDecision.ASK,
        doc="Allow UI send_message; ask otherwise.",
    )
    return result


@pytest.fixture
def policy_failing_tests() -> str:
    return str(fetch_policy("failing_tests"))


# --- Missing fixtures for default policy tests ---


@pytest.fixture
def policy_version_test() -> str:
    result: str = make_policy(
        decision_expr="ApprovalDecision.ALLOW",
        server="ui",
        tool="send_message",
        default=ApprovalDecision.ASK,
        doc="Version bump check policy used in tests.",
    )
    return result


@pytest.fixture
def policy_invalid_syntax() -> str:
    # Intentionally invalid Python
    return "class ApprovalPolicy:\n    '''invalid'''\n    def decide(self, ctx):\n        return (ApprovalDecision.ALLOW, 'ok'\n"


@pytest.fixture
def policy_context_checking() -> str:
    return str(fetch_policy("context_checking"))


@pytest.fixture
def policy_const() -> str:
    return str(fetch_policy("const"))


# ---- PolicyRequest test helper ----


def make_policy_request(server: MCPMountPrefix, tool: str, arguments: dict[str, Any] | None = None) -> PolicyRequest:
    """Helper to create PolicyRequest instances for tests.

    Args:
        server: MCP mount prefix (validated)
        tool: Tool name
        arguments: Tool arguments dict (will be JSON-encoded). Defaults to empty dict.

    Returns:
        PolicyRequest with arguments JSON-encoded as string.
    """
    return PolicyRequest(
        name=build_mcp_function(server, tool), arguments_json=json.dumps(arguments) if arguments is not None else None
    )


# ---- Shared ContainerOptions fixtures and in-proc docker exec specs ----
# Kept here so all tests can reuse the same settings consistently.


# Helper: create a live agent via HTTP on a TestClient and return its id
@pytest.fixture
def create_live_agent():
    def _create(client, *, specs: McpServerSpecs | None = None) -> str:
        specs = specs or {}
        # Split into typed JSON specs vs runtime slot specs
        typed: dict[str, MCPServerTypes] = {}
        inproc: dict[str, FastMCP] = {}
        for k, v in list(specs.items()):
            if isinstance(v, FastMCP):
                inproc[k] = v
                continue
            # Must be MCPServerTypes at this point
            assert isinstance(v, BaseModel), f"Expected MCPServerTypes or FastMCP, got {type(v)}"
            typed[k] = v
        # Create agent via API using a preset
        resp = client.post("/api/agents", json={"preset": "default"})
        assert resp.status_code == 200, resp.text
        agent_id = str(resp.json()["id"])
        # Attach typed specs via HTTP reconfigure, then runtime slots in-process
        if typed:
            # Enforce one format: ALL typed specs must be Pydantic models (MCPServerTypes).
            if not all(isinstance(v, BaseModel) for v in typed.values()):
                raise AssertionError("Typed MCP specs must be provided as Pydantic models only")
            # Send over HTTP; server rehydrates to typed MCPServerTypes (TestClient handles Pydantic serialization)
            r = client.patch(f"/api/agents/{agent_id}/mcp", json={"attach": typed})
            assert r.status_code == 200, r.text
        if inproc:

            async def _attach_async() -> None:
                reg = client.app.state.registry
                c = await reg.ensure_live(agent_id, with_ui=True)
                comp = c._compositor
                if comp is None:
                    raise AssertionError("compositor not initialized on container")
                for name, server in inproc.items():
                    await comp.mount_inproc(MCPMountPrefix(name), server)
                await c._push_snapshot_and_status()

            client.portal.call(_attach_async)
        return agent_id

    return _create


@pytest.fixture
def patch_agent_build_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[OpenAIModelProto], None]:
    """Return a function to patch container.build_client to a provided fake client.

    Keeps model patching independent from agent creation, so tests can opt-in.
    """

    def _patch(fake_model: OpenAIModelProto) -> None:
        monkeypatch.setattr("agent_server.runtime.container.build_client", lambda *a, **k: fake_model)

    return _patch


@pytest.fixture
def agent_app_client():
    """Yield a (app, client) pair for the UI server with static assets not required.

    Ensures a consistent pattern across tests, avoiding repeated create_app/TestClient boilerplate.
    """
    app = create_app(require_static_assets=False)
    with TestClient(app) as client:
        yield app, client


@pytest.fixture
def agent_test_client(agent_app_client):
    """Return just the TestClient for agent server tests.

    Use this when you only need the client and not the app.
    """
    _app, client = agent_app_client
    return client


# ---- Recording handler fixture -----------------------------------------------


@pytest.fixture
def recording_handler() -> RecordingHandler:
    """Fresh RecordingHandler for capturing agent events during tests."""
    return RecordingHandler()


@pytest.fixture
def test_handlers(recording_handler: RecordingHandler) -> list:
    """Standard handler list for agent tests.

    Includes:
    - FinishOnTextMessageHandler: Abort loop on text messages (test mocks often return text)
    - RecordingHandler: Capture events for assertions
    """

    return [FinishOnTextMessageHandler(), recording_handler]


# ---- Server fixtures for tool error and parallel tests ------------------------


class ValidationServer(EnhancedFastMCP):
    """EnhancedFastMCP server with a tool that validates input strictly."""

    # Tool attribute (assigned in __init__)
    send_message_tool: FunctionTool

    def __init__(self):
        super().__init__("validator")

        def send_message(input: SendMessageInput) -> dict[str, Any]:
            """Send a message with mime type validation."""
            # Reject text/plain to test error handling
            if input.mime == "text/plain":
                raise ToolError("Validation error: Only text/markdown is supported, not text/plain")
            return {"ok": True, "message": input.content}

        self.send_message_tool = self.flat_model()(send_message)


@pytest.fixture
def validation_server() -> ValidationServer:
    """ValidationServer with typed tool access."""
    return ValidationServer()


class _FailInput(OpenAIStrictModeBaseModel):
    """Input for fail tool (test fixture)."""

    x: int


@pytest.fixture
def failing_server() -> EnhancedFastMCP:
    """EnhancedFastMCP server with a tool that returns an error payload."""
    # Workaround: Pass version="test" to skip slow importlib.metadata.version() lookup
    # that hangs on os.stat() in Nix environment. Without this, MCP server initialization
    # would call pkg_version("mcp") which triggers filesystem operations that timeout.
    mcp = EnhancedFastMCP("editor", version="test")

    @mcp.flat_model()
    def fail(input: _FailInput) -> dict[str, Any]:
        # Return error payload in structured_content (not raise ToolError)
        # The test expects ok=False, error="boom" in structured_content
        return {"ok": False, "error": "boom"}

    return mcp


@pytest.fixture
def slow_server() -> FastMCP:
    """FastMCP server with two slow async tools for parallel call testing."""
    mcp = FastMCP("dummy")

    @mcp.tool()
    async def slow() -> dict[str, Any]:
        await asyncio.sleep(0.30)
        return {"ok": True, "tool": "slow", "args": {}}

    @mcp.tool()
    async def slow2() -> dict[str, Any]:
        await asyncio.sleep(0.30)
        return {"ok": True, "tool": "slow2", "args": {}}

    return mcp


# ---- UI reducer/history test fixtures ----------------------------------------


@pytest.fixture
def fresh_ui_state():
    """Fresh UI state for reducer tests."""
    return new_state()


@pytest.fixture
def call_id_gen() -> Callable[[], str]:
    """Lightweight call_id generator for tests."""
    counter = {"count": 0}

    def _gen() -> str:
        counter["count"] += 1
        return f"test_call:{counter['count']}"

    return _gen


@pytest.fixture
def make_tool_call(call_id_gen: Callable[[], str]) -> Callable[..., ToolCall]:
    """Factory for ToolCall events with auto call_id generation."""

    def _make(server: MCPMountPrefix, tool: str, args: dict[str, Any] | None = None) -> ToolCall:
        args_json = json.dumps(args) if args is not None else None
        return ToolCall(name=build_mcp_function(server, tool), args_json=args_json, call_id=call_id_gen())

    return _make


@pytest.fixture
def make_call_result() -> Callable[[dict[str, Any] | None, bool], mcp.types.CallToolResult]:
    """Factory for MCP CallToolResult."""

    def _make(structured_content: dict[str, Any] | None = None, is_error: bool = False) -> mcp.types.CallToolResult:
        return mcp.types.CallToolResult(content=[], structuredContent=structured_content or {}, isError=is_error)

    return _make


@pytest.fixture
def make_tool_call_output(
    make_call_result: Callable[[dict[str, Any] | None, bool], mcp.types.CallToolResult],
) -> Callable[[str, dict[str, Any] | None, bool], ToolCallOutput]:
    """Factory for ToolCallOutput events."""

    def _make(call_id: str, structured_content: dict[str, Any] | None = None, is_error: bool = False) -> ToolCallOutput:
        return ToolCallOutput(call_id=call_id, result=make_call_result(structured_content, is_error))

    return _make


@pytest.fixture
def make_function_output(
    make_call_result: Callable[[dict[str, Any] | None, bool], mcp.types.CallToolResult],
) -> Callable[[str, dict[str, Any] | None, bool], FunctionCallOutput]:
    """Factory for protocol FunctionCallOutput (not EventRecord)."""

    def _make(
        call_id: str, structured_content: dict[str, Any] | None = None, is_error: bool = False
    ) -> FunctionCallOutput:
        return FunctionCallOutput(call_id=call_id, result=make_call_result(structured_content, is_error))

    return _make


# --- EventRecord factories for history tests ---


@pytest.fixture
def event_ts() -> datetime:
    """Shared timestamp for EventRecord tests."""
    return datetime.now(UTC)


@pytest.fixture
def make_event_record(event_ts: datetime) -> Callable[[EventType, int | None], EventRecord]:
    """Wrap any EventType in an EventRecord with auto-sequencing."""
    seq_counter = {"count": 0}

    def _wrap(payload: EventType, seq: int | None = None) -> EventRecord:
        if seq is None:
            seq_counter["count"] += 1
            seq = seq_counter["count"]
        return EventRecord(seq=seq, ts=event_ts, payload=payload)

    return _wrap


@pytest.fixture
def make_user_text_event(
    make_event_record: Callable[[EventType, int | None], EventRecord],
) -> Callable[[int, str], EventRecord]:
    """Factory for UserText EventRecord."""

    def _make(seq: int, text: str) -> EventRecord:
        return make_event_record(UserText(text=text), seq)

    return _make


@pytest.fixture
def make_tool_call_event(
    make_event_record: Callable[[EventType, int | None], EventRecord], make_tool_call: Callable[..., ToolCall]
) -> Callable[..., EventRecord]:
    """Factory for ToolCall EventRecord."""

    def _make(seq: int, server: MCPMountPrefix, tool: str, args: dict[str, Any] | None = None) -> EventRecord:
        return make_event_record(make_tool_call(server, tool, args=args), seq)

    return _make


@pytest.fixture
def make_function_output_event(
    make_event_record: Callable[[EventType, int | None], EventRecord],
    make_tool_call_output: Callable[[str, dict[str, Any] | None, bool], ToolCallOutput],
) -> Callable[[int, str, dict[str, Any] | None], EventRecord]:
    """Factory for ToolCallOutput EventRecord."""

    def _make(seq: int, call_id: str, structured_content: dict[str, Any] | None = None) -> EventRecord:
        return make_event_record(make_tool_call_output(call_id, structured_content, False), seq)

    return _make


# --- Policy gateway fixtures (for MCP middleware tests) ---


async def _create_test_policy_engine(sqlite_persistence, async_docker_client) -> PolicyEngine:
    """Create a test policy engine with approve-all policy."""
    agent_id_resolved = await sqlite_persistence.create_agent(
        mcp_config=MCPConfig(), metadata=AgentMetadata(preset="test")
    )
    return PolicyEngine(
        agent_id=agent_id_resolved,
        persistence=sqlite_persistence,
        policy_source=approve_all_policy_text(),
        docker_client=async_docker_client,
    )


@pytest.fixture
async def _setup_policy_gateway_compositor(sqlite_persistence, async_docker_client, test_agent_id):
    """Fixture factory for AgentContainerCompositor with policy gateway middleware."""

    @asynccontextmanager
    async def _factory(servers: McpServerSpecs, *, policy_engine: PolicyEngine | None = None):
        if policy_engine is None:
            policy_engine = await _create_test_policy_engine(sqlite_persistence, async_docker_client)

        comp = AgentContainerCompositor(
            approval_engine=policy_engine,
            ui_bus=None,
            async_docker_client=async_docker_client,
            persistence=sqlite_persistence,
            agent_id=test_agent_id,
        )
        async with comp:
            comp.add_middleware(policy_engine.gateway)
            await _mount_servers(comp, servers)
            yield comp

    return _factory


@pytest.fixture
async def make_policy_gateway_client(_setup_policy_gateway_compositor):
    """Async helper to open an AgentContainerCompositor with policy gateway, yielding just the client."""

    @asynccontextmanager
    async def _open(servers: McpServerSpecs, *, policy_engine: PolicyEngine | None = None):
        async with _setup_policy_gateway_compositor(servers, policy_engine=policy_engine) as comp, Client(comp) as sess:
            yield sess

    return _open


@pytest.fixture
async def make_policy_gateway_compositor(_setup_policy_gateway_compositor):
    """Async helper factory yielding typed AgentContainerCompositor."""

    @asynccontextmanager
    async def _open(servers: McpServerSpecs, *, policy_engine: PolicyEngine | None = None):
        async with _setup_policy_gateway_compositor(servers, policy_engine=policy_engine) as comp:
            yield comp

    return _open


@pytest.fixture
async def policy_gateway_client(make_policy_gateway_client, make_simple_mcp):
    """Ready-to-use client with make_simple_mcp mounted and allow-all policy."""
    async with make_policy_gateway_client({TEST_BACKEND_SERVER_NAME: make_simple_mcp}) as sess:
        yield sess


def make_policy_source(decision: ApprovalDecision) -> str:
    """Generate a policy source that always returns the specified decision."""
    return f'''"""Policy that returns {decision.value} for all calls."""
from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest, PolicyResponse
from agent_server.policies.scaffold import run

def decide(_req: PolicyRequest) -> PolicyResponse:
    return PolicyResponse(decision=ApprovalDecision.{decision.name}, rationale="{decision.value}")

if __name__ == "__main__":
    raise SystemExit(run(decide))
'''


@pytest.fixture
async def make_decision_engine(
    make_approval_policy_server: Callable[[str], Awaitable[PolicyEngine]],
) -> Callable[[ApprovalDecision], Awaitable[PolicyEngine]]:
    """Factory for creating PolicyEngine with a specific decision policy."""

    async def _make(decision: ApprovalDecision) -> PolicyEngine:
        policy_source = make_policy_source(decision)
        return await make_approval_policy_server(policy_source)

    return _make
