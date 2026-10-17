"""Simple echo MCP server for testing.

Provides a minimal FastMCP server that echoes input text back.
Used for testing agent tool execution without external dependencies.
"""

from __future__ import annotations

from fastmcp.server import FastMCP

from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# Test server constants (SSOT for test fixtures)
ECHO_TOOL_NAME = "echo"


class EchoInput(OpenAIStrictModeBaseModel):
    """Input payload helper for mock responses and serialized test calls."""

    text: str


class EchoOutput(OpenAIStrictModeBaseModel):
    """Output for echo tool."""

    echo: str


def make_echo_server(name: str = "echo") -> FastMCP:
    """Create a FastMCP server with an echo tool."""
    server = FastMCP(name)

    @server.tool(output_schema=None)
    def echo(text: str) -> EchoOutput:
        """Echo the input text back."""
        return EchoOutput(echo=text)

    return server
