"""Tool provider protocol and types for agent_core.

Defines the interface for tool discovery and execution without MCP dependency.
MCP integration lives in mcp_provider.py which wraps fastmcp.Client.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, Field

# --- Tool result content types (discriminated union) ---


class TextContent(BaseModel):
    """Text content in a tool result."""

    type: Literal["text"] = "text"
    text: str


class ImageContent(BaseModel):
    """Image content in a tool result (base64 data URL)."""

    type: Literal["image"] = "image"
    mime_type: str
    data: str  # base64-encoded


ResultContent = Annotated[TextContent | ImageContent, Field(discriminator="type")]

# Structured tool output data. Wider than MCP's structuredContent (dict[str, Any] | None)
# because DirectToolProvider bypasses MCP and can return lists directly. OpenAI's tool
# output is ultimately a JSON string, so any JSON-serializable value works.
ToolOutputData = dict[str, Any] | list[Any]

# --- Tool result ---


class ToolResult(BaseModel):
    """Result from a tool invocation."""

    content: list[ResultContent] = Field(default_factory=list)
    structured_content: ToolOutputData | None = None
    is_error: bool = False

    @classmethod
    def text(cls, text: str, *, is_error: bool = False) -> ToolResult:
        """Create a text result."""
        return cls(content=[TextContent(text=text)], is_error=is_error)

    @classmethod
    def error(cls, message: str) -> ToolResult:
        """Create an error result."""
        return cls(content=[TextContent(text=message)], is_error=True)

    @classmethod
    def structured(cls, data: ToolOutputData, *, is_error: bool = False) -> ToolResult:
        """Create a structured result with JSON data (dict or list)."""
        return cls(structured_content=data, is_error=is_error)


# --- Tool schema ---


class ToolSchema(BaseModel):
    """Schema for a tool that can be called by the agent."""

    name: str
    description: str
    input_schema: dict[str, Any]


# --- Tool provider protocol ---


class ToolProvider(Protocol):
    """Protocol for tool discovery and execution.

    Implementations:
    - MCPToolProvider: Wraps fastmcp.Client for MCP-based tools
    - DirectToolProvider: Direct function calls without MCP overhead
    """

    async def list_tools(self) -> list[ToolSchema]:
        """Return available tools."""
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute a tool and return the result."""
        ...
