"""Shared FastMCP helpers for tests and demos."""

from __future__ import annotations

from typing import Literal

from fastmcp.server import FastMCP
from pydantic import BaseModel

from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel


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
    """Register the shared echo tool."""

    @server.tool(output_schema=None)
    def echo(text: str) -> EchoOutput:
        return EchoOutput(echo=text)


def make_simple_mcp() -> FastMCP:
    server = FastMCP("simple")
    build_simple_tools(server)
    return server
