"""Portable pytest fixtures for agent tests.

Register in downstream packages via:
    pytest_plugins = [
        "agent_core_testing.fixtures",
        "agent_core_testing.responses",
    ]

For compositor fixtures, also register:
    pytest_plugins = ["mcp_infra.testing.fixtures"]
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from typing import Any

import mcp.types
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server import FastMCP
from fastmcp.tools import FunctionTool
from openai import AsyncOpenAI
from pydantic import BaseModel

from agent_core.agent import Agent
from agent_core.events import AssistantText, SystemText, ToolCall, ToolCallOutput, UserText
from agent_core.handler import BaseHandler, FinishOnTextMessageHandler
from agent_core.loop_control import RequireAnyTool
from agent_core_testing.echo_server import make_echo_server
from agent_core_testing.openai_mock import CapturingOpenAIModel, FakeOpenAIModel
from agent_core_testing.responses import ResponsesFactory
from mcp_infra.enhanced.flat_mixin import FlatModelMixin
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.testing.simple_servers import SendMessageInput
from openai_utils.model import ResponsesResult
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# ---- Recording handler ----


class RecordingHandler(BaseHandler):
    """Handler that records all events for test assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[ToolCall | ToolCallOutput | SystemText | UserText | AssistantText] = []

    def on_tool_call_event(self, evt: ToolCall, /) -> None:
        self.records.append(evt)

    def on_tool_result_event(self, evt: ToolCallOutput, /) -> None:
        self.records.append(evt)

    def on_system_text_event(self, evt: SystemText, /) -> None:
        self.records.append(evt)

    def on_user_text_event(self, evt: UserText, /) -> None:
        self.records.append(evt)

    def on_assistant_text_event(self, evt: AssistantText, /) -> None:
        self.records.append(evt)


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


# ---- OpenAI model factories ----


@pytest.fixture
def make_fake_openai() -> Callable[[Iterable[ResponsesResult]], FakeOpenAIModel]:
    """Factory to create FakeOpenAIModel instances from response sequences.

    Usage:
        client = make_fake_openai([responses_factory.make_assistant_message("ok")])
    """

    def _make(outputs: Iterable[ResponsesResult]) -> FakeOpenAIModel:
        return FakeOpenAIModel(list(outputs))

    return _make


@pytest.fixture
def make_capturing_client():
    """Factory to create a CapturingOpenAIModel wrapping FakeOpenAIModel.

    Usage:
        client = make_capturing_client([responses_factory.make_assistant_message("done")])
        # Use client with agent...
        assert client.calls == 1
    """

    def _make(responses):
        fake_client = FakeOpenAIModel(responses)
        return CapturingOpenAIModel(fake_client)

    return _make


@pytest.fixture
def make_test_agent(responses_factory: ResponsesFactory):
    """Factory to create Agent backed by FakeOpenAIModel with canned responses.

    Returns (agent, fake_client) tuple so tests can inspect the client after run.

    Usage:
        agent, client = await make_test_agent(
            mcp_client,
            [responses_factory.make_assistant_message("done")],
        )
        result = await agent.run("hi")
        assert client.calls == 1
    """

    async def _make(mcp_client, responses, *, handlers=(), system="test", tool_policy=None, **kwargs):
        fake_model = FakeOpenAIModel(list(responses))
        client = CapturingOpenAIModel(fake_model)  # Wrap to enable .captured
        # Minimal defaults - tests should be explicit about their needs
        if not handlers:
            handlers = [BaseHandler()]  # Minimal no-op handler (Agent requires at least one)
        if tool_policy is None:
            tool_policy = RequireAnyTool()
        agent = await Agent.create(
            mcp_client=mcp_client, client=client, handlers=handlers, tool_policy=tool_policy, **kwargs
        )
        return agent, client

    return _make


# ---- MCP fixtures ----
# Note: compositor and compositor_client fixtures are in mcp_infra.testing.fixtures


@pytest.fixture
def echo_server() -> FastMCP:
    """Echo FastMCP server instance."""
    return make_echo_server()


@pytest.fixture
def echo_spec(echo_server) -> dict[str, FastMCP]:
    """In-proc FastMCP server spec for echo tests."""
    return {"echo": echo_server}


@pytest.fixture
async def mcp_client_echo(make_compositor, echo_spec):
    """Plain MCP client with echo server (no policy gateway).

    For tests that don't need policy approval but need a simple MCP server.
    Using plain Compositor avoids Docker overhead and potential timeouts.

    Note: Requires mcp_infra.testing.fixtures to be registered for make_compositor.
    """
    async with make_compositor(echo_spec) as (client, _comp):
        yield client


# ---- Slow server for parallel call testing ----


class _EmptyInput(OpenAIStrictModeBaseModel):
    """Empty input for slow tools."""


class _SlowOutput(BaseModel):
    """Output for slow tools."""

    ok: bool
    tool: str
    args: dict[str, Any]


@pytest.fixture
def slow_server() -> FlatModelMixin:
    """FastMCP server with two slow async tools for parallel call testing."""
    mcp = FlatModelMixin("dummy")

    @mcp.flat_model()
    async def slow(input: _EmptyInput) -> _SlowOutput:
        """Slow tool that takes 0.30s."""
        await asyncio.sleep(0.30)
        return _SlowOutput(ok=True, tool="slow", args={})

    @mcp.flat_model()
    async def slow2(input: _EmptyInput) -> _SlowOutput:
        """Second slow tool that takes 0.30s."""
        await asyncio.sleep(0.30)
        return _SlowOutput(ok=True, tool="slow2", args={})

    return mcp


# ---- Event/call factories ----


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
    """Factory for ToolCall events with auto call_id generation.

    Note: Uses simple string concatenation for tool names. Downstream tests
    should use their own naming helpers (e.g., build_mcp_function) if needed.
    """

    def _make(server: str, tool: str, args: dict[str, Any] | None = None) -> ToolCall:
        args_json = json.dumps(args) if args is not None else None
        return ToolCall(name=f"{server}_{tool}", args_json=args_json, call_id=call_id_gen())

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


# ---- Live OpenAI fixture ----


@pytest.fixture
def live_openai(request):
    """Provide a live AsyncOpenAI client for tests marked with `live_openai_api`.

    - For non-`live_openai_api` tests that include this fixture in the signature but
      do not actually use it (e.g., parameterized tests with a mock branch),
      return a lightweight no-op placeholder to avoid network work and keep
      those tests running.
    - For `live_openai_api` tests, construct AsyncOpenAI (marker skip logic
      ensures OPENAI_API_KEY is set).
    """
    if request.node.get_closest_marker("live_openai_api") is not None:
        return AsyncOpenAI()

    class _Noop:
        pass

    return _Noop()


# ---- Validation and failing server fixtures ----
# TODO: Consider merging ValidationServer into simple_servers.py with a fail-on-condition tool


class ValidationServer(EnhancedFastMCP):
    """EnhancedFastMCP server with a tool that validates input strictly."""

    send_message_tool: FunctionTool

    def __init__(self):
        super().__init__("validator")

        def send_message(input: SendMessageInput) -> dict[str, Any]:
            """Send a message with mime type validation."""
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


# Tool name constant for test assertions
FAIL_TOOL_NAME = "fail"


@pytest.fixture
def error_payload_server() -> EnhancedFastMCP:
    """Server with a tool that returns an application-level error in structuredContent.

    Note: This does NOT set the MCP-level isError flag. It returns a successful
    MCP response containing {"ok": False, "error": "boom"} in structuredContent.
    Use ToolError to signal MCP-level errors (see validation_server).
    """
    mcp = EnhancedFastMCP("editor", version="test")

    @mcp.flat_model()
    def fail(input: _FailInput) -> dict[str, Any]:
        # Application-level error payload, not MCP isError
        return {"ok": False, "error": "boom"}

    return mcp
