"""Simple echo MCP server for testing.

Provides a minimal FastMCP server that echoes input text back.
Used for testing agent tool execution without external dependencies.
"""

from __future__ import annotations

from typing import Final

from mcp_infra.enhanced.flat_mixin import FlatModelMixin
from mcp_infra.naming import MCPMountPrefix
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# Test server constants (SSOT for test fixtures)
ECHO_MOUNT_PREFIX: Final[MCPMountPrefix] = MCPMountPrefix("echo")
ECHO_TOOL_NAME: Final[str] = "echo"


class EchoInput(OpenAIStrictModeBaseModel):
    """Input for echo tool."""

    text: str


class EchoOutput(OpenAIStrictModeBaseModel):
    """Output for echo tool."""

    echo: str


def make_echo_server(name: str = "echo") -> FlatModelMixin:
    """Create a FastMCP server with an echo tool.

    Args:
        name: Server name (default: "echo")

    Returns:
        FastMCP server instance with echo tool registered
    """
    server = FlatModelMixin(name)

    @server.flat_model()
    def echo(input: EchoInput) -> EchoOutput:
        """Echo the input text back."""
        return EchoOutput(echo=input.text)

    return server
