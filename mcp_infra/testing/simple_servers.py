"""Shared FastMCP helpers for unit tests and demos.

Provides minimal test tools that exercise meaningfully different functionality:

- ``echo(input)`` - Normal tool call: echoes input text back in structured output

Each tool tests a distinct pattern. Removed redundant tools (ping, noop) that
don't add coverage beyond echo().
"""

from __future__ import annotations

from typing import Literal

from fastmcp.server import FastMCP
from pydantic import BaseModel

from mcp_infra.prefix import MCPMountPrefix
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# Test server constants (SSOT for test fixtures)
ECHO_MOUNT_PREFIX = MCPMountPrefix("echo")
ECHO_TOOL_NAME = "echo"


class EchoOutput(BaseModel):
    """Output for echo tool."""

    echo: str


class SendMessageInput(OpenAIStrictModeBaseModel):
    """Input for validation send_message tool.

    Used by validation_server fixture for testing strict validation.
    """

    mime: Literal["text/markdown", "text/plain"]
    content: str


def build_simple_tools(server: FastMCP) -> None:
    """Register the standard simple tools on ``server``.

    The helpers are intentionally deterministic and side-effect free so they can
    be reused across unit, integration, and approval-policy tests.
    """

    @server.tool(output_schema=None)
    def echo(text: str) -> EchoOutput:
        return EchoOutput(echo=text)


def make_simple_mcp() -> FastMCP:
    """Create a FastMCP server exposing the shared simple tools."""

    server = FastMCP("simple")
    build_simple_tools(server)
    return server
