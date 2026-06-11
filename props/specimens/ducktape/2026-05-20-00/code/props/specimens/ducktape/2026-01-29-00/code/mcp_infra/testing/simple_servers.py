"""Shared FastMCP helpers for unit tests and demos.

Provides minimal test tools that exercise meaningfully different functionality:

- ``echo(input)`` - Normal tool call: echoes input text back in structured output

Each tool tests a distinct pattern. Removed redundant tools (ping, noop) that
don't add coverage beyond echo().
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from mcp_infra.enhanced.flat_mixin import FlatModelMixin
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# Test server constants (SSOT for test fixtures)
ECHO_MOUNT_PREFIX = MCPMountPrefix("echo")
ECHO_TOOL_NAME = "echo"


class EchoInput(OpenAIStrictModeBaseModel):
    """Input for echo tool."""

    text: str


class EchoOutput(BaseModel):
    """Output for echo tool."""

    echo: str


class SendMessageInput(OpenAIStrictModeBaseModel):
    """Input for validation send_message tool.

    Used by validation_server fixture for testing strict validation.
    """

    mime: Literal["text/markdown", "text/plain"]
    content: str


def build_simple_tools(server: FlatModelMixin) -> None:
    """Register the standard simple tools on ``server``.

    The helpers are intentionally deterministic and side-effect free so they can
    be reused across unit, integration, and approval-policy tests.
    """

    @server.flat_model()
    def echo(input: EchoInput) -> EchoOutput:
        return EchoOutput(echo=input.text)


def make_simple_mcp() -> FlatModelMixin:
    """Create a FlatModelMixin server exposing the shared simple tools."""

    server = FlatModelMixin("simple")
    build_simple_tools(server)
    return server
