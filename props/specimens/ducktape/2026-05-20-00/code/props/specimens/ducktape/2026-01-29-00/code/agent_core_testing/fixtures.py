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

import json
from collections.abc import Callable
from typing import Any

import pytest
from fastmcp.server import FastMCP
from openai import AsyncOpenAI

from agent_core.events import AssistantText, SystemText, ToolCall, ToolCallOutput, UserText
from agent_core.handler import BaseHandler, FinishOnTextMessageHandler
from agent_core.mcp_provider import MCPToolProvider
from agent_core.tool_provider import ToolProvider
from agent_core_testing.echo_server import make_echo_server
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix

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


@pytest.fixture
def mcp_tool_provider(compositor_client) -> ToolProvider:
    """MCPToolProvider wrapping compositor_client.

    Use this fixture instead of manually wrapping compositor_client.
    """
    return MCPToolProvider(compositor_client)


@pytest.fixture
def mcp_tool_provider_echo(mcp_client_echo) -> ToolProvider:
    """MCPToolProvider wrapping echo-only client (no compositor)."""
    return MCPToolProvider(mcp_client_echo)


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
    """Factory for ToolCall events with auto call_id generation."""

    def _make(server: MCPMountPrefix, tool: str, args: dict[str, Any] | None = None) -> ToolCall:
        args_json = json.dumps(args) if args is not None else None
        return ToolCall(name=build_mcp_function(server, tool), args_json=args_json, call_id=call_id_gen())

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
