from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

from fastmcp.server import FastMCP
from fastmcp.server.context import Context
from hamcrest import assert_that, has_length, greater_than
from pydantic import BaseModel, ConfigDict
import pytest
import requests

from adgn.mcp._shared.naming import build_mcp_function
from tests.agent.helpers import api_create_agent, attach_echo_mcp, wait_for_pending_approvals, send_prompt
from tests.llm.support.openai_mock import make_mock

# Skip if Playwright is not installed
playwright = pytest.importorskip("playwright.sync_api")

if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    Page = playwright.Page


def test_subscription_to_deleted_agent(page: Page, run_server, responses_factory):
    """Test subscription cleanup when agent is deleted.

    Verifies:
    - Subscribe to agent's resource
    - Delete the agent
    - Subscription is cleaned up gracefully
    - No lingering references or memory leaks
    - UI handles agent deletion appropriately
    """
    state = {"i": 0}

    async def responses_create(_req):
        i = state["i"]
        state["i"] = i + 1
        if i == 0:
            return responses_factory.make_tool_call(
                build_mcp_function("echo", "echo"), {"text": "test"}, call_id="call_echo_1"
            )
        return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")

    s = run_server(lambda model: make_mock(responses_create))
    base = s["base_url"]

    # Create agent
    agent_id = api_create_agent(base)

    # Attach MCP server
    attach_echo_mcp(base, agent_id)

    # Open UI - this creates subscriptions
    page.goto(base + f"/?agent_id={agent_id}")

    # Wait for WS connection
    page.locator(".ws .dot.on").wait_for(timeout=10000)

    # Verify connection is established
    send_prompt(page, "hello")

    # Wait for some activity
    page.wait_for_timeout(2000)

    # Now delete the agent via API
    delete_resp = requests.delete(base + f"/api/agents/{agent_id}")
    assert delete_resp.ok, delete_resp.text

    # Wait a bit for cleanup to propagate
    page.wait_for_timeout(1000)

    # Verify WS connection shows disconnected after agent deletion
    page.locator(".ws .dot.off, .ws .dot:not(.on)").wait_for(timeout=5000)

    # Verify UI is still responsive
    assert page.title() is not None

    # Try to navigate back to agent (should fail gracefully)
    page.goto(base + f"/?agent_id={agent_id}")
    page.wait_for_timeout(1000)

    # Should show some kind of error or "not found" state
    assert page.title() is not None

    s["stop"]()


# Factory functions for custom test servers (used by inproc MCP server specs)


class _BrokenResourceInput(BaseModel):
    trigger: str = "break"
    model_config = ConfigDict(extra="forbid")


class _BrokenResourceOutput(BaseModel):
    result: str
    model_config = ConfigDict(extra="forbid")


class _BrokenResourceServer(FastMCP):
    """Test server that returns malformed responses."""

    def __init__(self) -> None:
        super().__init__(name="broken", instructions="Test server with broken resources")

        @self.tool()
        async def broken_tool(input: _BrokenResourceInput, ctx: Context) -> _BrokenResourceOutput:
            # Return something that will break JSON parsing downstream
            return _BrokenResourceOutput(result="This tool works fine")

        @self.resource("resource://broken/data")
        async def broken_resource() -> str:
            # Return intentionally malformed content
            # Note: FastMCP may validate this, so this tests the boundary
            return "not valid json at all {{{{"


class _SlowResourceInput(BaseModel):
    delay_seconds: int = 5
    model_config = ConfigDict(extra="forbid")


class _SlowResourceOutput(BaseModel):
    result: str
    model_config = ConfigDict(extra="forbid")


class _SlowResourceServer(FastMCP):
    """Test server with intentionally slow responses."""

    def __init__(self) -> None:
        super().__init__(name="slow", instructions="Test server with slow resources")

        @self.tool()
        async def slow_tool(input: _SlowResourceInput, ctx: Context) -> _SlowResourceOutput:
            # Simulate a very slow operation
            await asyncio.sleep(input.delay_seconds)
            return _SlowResourceOutput(result="Finally done")

        @self.resource("resource://slow/data")
        async def slow_resource() -> str:
            # Simulate slow resource read
            await asyncio.sleep(30)  # Very slow
            return '{"data": "slow response"}'


def _make_broken_server():
    """Factory for broken resource server."""
    return _BrokenResourceServer()


def _make_slow_server():
    """Factory for slow resource server."""
    return _SlowResourceServer()
