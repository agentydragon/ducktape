"""Tool provider protocol and types for agent_core.

Defines the interface for tool discovery and execution without MCP dependency.
MCP integration lives in mcp_provider.py which wraps fastmcp.Client.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, Field, TypeAdapter

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


# --- Tool result ---


class ToolResult(BaseModel):
    """Result from a tool invocation."""

    content: list[ResultContent] = Field(default_factory=list)
    structured_content: dict[str, Any] | None = None
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
    def structured(cls, data: dict[str, Any], *, is_error: bool = False) -> ToolResult:
        """Create a structured result with JSON data."""
        return cls(structured_content=data, is_error=is_error)

    def extract_structured[T](self, output_type: type[T]) -> T:
        """Extract and validate structured content from this result.

        Raises ValueError if result is an error or lacks structured content.
        """
        if self.is_error:
            raise ValueError(f"Cannot extract from error result: {self}")
        if self.structured_content is None:
            raise ValueError(f"ToolResult missing structured content: {self}")
        return TypeAdapter(output_type).validate_python(self.structured_content)


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
    - CompositeToolProvider: Combines multiple providers
    """

    async def list_tools(self) -> list[ToolSchema]:
        """Return available tools."""
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute a tool and return the result."""
        ...


# --- Composite provider ---


class CompositeToolProvider:
    """Combines multiple tool providers into one.

    Tools must be unique across all providers - duplicates raise ValueError.
    Use this to combine local tools with remote MCP tools.
    """

    def __init__(self, *providers: ToolProvider) -> None:
        self._providers = providers
        self._tool_index: dict[str, ToolProvider] | None = None

    async def _build_index(self) -> dict[str, ToolProvider]:
        """Build tool name -> provider index, checking for duplicates."""
        if self._tool_index is not None:
            return self._tool_index

        index: dict[str, ToolProvider] = {}
        for provider in self._providers:
            for tool in await provider.list_tools():
                if tool.name in index:
                    raise ValueError(f"Duplicate tool '{tool.name}' across providers")
                index[tool.name] = provider
        self._tool_index = index
        return index

    async def list_tools(self) -> list[ToolSchema]:
        """Return all tools from all providers."""
        await self._build_index()  # Validate no duplicates
        all_tools: list[ToolSchema] = []
        for provider in self._providers:
            all_tools.extend(await provider.list_tools())
        return all_tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Call tool by name."""
        index = await self._build_index()
        provider = index.get(name)
        if provider is None:
            return ToolResult.error(f"Tool not found: {name}")
        return await provider.call_tool(name, arguments)
