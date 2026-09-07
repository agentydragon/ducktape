from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from typing import Annotated, Final, Literal

from fastmcp.exceptions import ToolError
from mcp import types as mcp_types
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, ConfigDict, Field

from mcp_infra.compositor.server import BaseCompositor
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.flat_tool import FlatTool
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.resource_utils import add_resource_prefix
from mcp_infra.resources.types import ResourceEntry
from mcp_infra.snapshots import RunningServerEntry
from mcp_infra.urls import ANY_URL
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

## ResourceEntry moved to adgn.mcp.resources.types to avoid cycles

logger = logging.getLogger(__name__)

# Server naming (internal display name)
RESOURCES_SERVER_NAME: Final[str] = "Resources Server"

# Default max bytes for resource reading operations
DEFAULT_MAX_BYTES = 25_000


class ResourcesListArgs(OpenAIStrictModeBaseModel):
    """Filter arguments for listing resources.

    All fields are required but accept None to indicate "no filter".
    """

    server: MCPMountPrefix | None = Field(description="Filter by server name (None = all servers)")
    uri_prefix: str | None = Field(description="Restrict to URIs starting with this prefix (None = no filter)")


class ResourcesListResult(BaseModel):
    resources: list[ResourceEntry] = Field(
        description="Aggregated resources across servers (each item includes origin server)"
    )
    model_config = ConfigDict(extra="forbid")


class ResourceTemplateEntry(BaseModel):
    server: MCPMountPrefix = Field(description="Origin MCP server name that owns this template")
    template: mcp_types.ResourceTemplate = Field(description="Resource template from origin server")
    model_config = ConfigDict(extra="forbid")


class ResourceTemplatesListResult(BaseModel):
    templates: list[ResourceTemplateEntry] = Field(
        description="Aggregated resource templates across servers (each item includes origin server)"
    )
    model_config = ConfigDict(extra="forbid")


class ResourceWindowInfo(BaseModel):
    start_offset: int = Field(description="Start byte offset used for this window")
    max_bytes: int = Field(description="Max bytes requested for this window (0 means unbounded)")
    model_config = ConfigDict(extra="forbid")


"""
Typed read result is defined after WindowedPart to avoid forward refs.
"""


class ResourcesReadArgs(OpenAIStrictModeBaseModel):
    server: MCPMountPrefix = Field(description="Origin MCP server name that owns the resource")
    uri: str = Field(description="Resource URI as reported by the origin server's list")
    start_offset: int = Field(ge=0, description="Start byte offset for windowed reads")
    max_bytes: int | None = Field(
        description=f"Max bytes to return (None means use default limit of {DEFAULT_MAX_BYTES:,} bytes)"
    )


# No compositor meta resources here; see adgn.mcp.compositor.meta_server


class _MimeBase(BaseModel):
    mime: str | None = None
    model_config = ConfigDict(extra="forbid")


class TextPart(_MimeBase):
    kind: Literal["text"] = "text"
    raw_bytes: bytes


class BlobPart(_MimeBase):
    kind: Literal["base64"] = "base64"
    raw_str: str


NormalizedPart = Annotated[TextPart | BlobPart, Field(discriminator="kind")]


class _WindowedBase(_MimeBase):
    total_bytes: int
    bytes_returned: int


class WindowedTextPart(_WindowedBase):
    kind: Literal["text"] = "text"
    text: str


class WindowedBlobPart(_WindowedBase):
    kind: Literal["base64"] = "base64"
    base64: str


WindowedPart = Annotated[WindowedTextPart | WindowedBlobPart, Field(discriminator="kind")]


class ResourceReadResult(BaseModel):
    window: ResourceWindowInfo = Field(description="Windowing parameters reflected back")
    parts: list[WindowedPart] = Field(description="Windowed parts (text/base64)")
    total_parts: int = Field(description="Total number of parts reported by the origin server")
    model_config = ConfigDict(extra="forbid")


BaseResourceContents = mcp_types.TextResourceContents | mcp_types.BlobResourceContents


class TruncatedBlock(BaseModel):
    """Represents a partially returned resource block with truncation metadata."""

    kind: Literal["truncated"] = "truncated"
    block_index: int = Field(description="Index of this block in the full resource (0-based)")
    started_at: int = Field(description="Byte offset where returned content starts within this block")
    ended_at: int = Field(description="Byte offset where returned content ends within this block")
    full_size: int = Field(description="Total size of the complete block in bytes")
    content: BaseResourceContents = Field(description="The partial content for this block")
    model_config = ConfigDict(extra="forbid")


BlockContent = Annotated[BaseResourceContents | TruncatedBlock, Field(discriminator="kind")]


class ReadBlocksArgs(OpenAIStrictModeBaseModel):
    server: str = Field(description="Origin MCP server name")
    uri: str = Field(description="Resource URI")
    start_block: int = Field(ge=0, description="Block index to start from (0-based)")
    start_offset: int = Field(ge=0, description="Byte offset within start_block to begin reading")
    max_bytes: int | None = Field(
        description=f"Maximum bytes to return (None means use default limit of {DEFAULT_MAX_BYTES:,} bytes)"
    )


class ReadBlocksResult(BaseModel):
    """Result of reading resource blocks."""

    blocks: list[mcp_types.TextResourceContents | mcp_types.BlobResourceContents | TruncatedBlock] = Field(
        description="Resource content blocks, with TruncatedBlock markers for partial blocks"
    )
    model_config = ConfigDict(extra="forbid")


def _normalize_parts(
    contents: Sequence[mcp_types.TextResourceContents | mcp_types.BlobResourceContents],
) -> list[NormalizedPart]:
    """Normalize resource parts to a small, typed internal shape."""
    norm: list[NormalizedPart] = []
    for p in contents:
        if isinstance(p, mcp_types.TextResourceContents):
            norm.append(TextPart(mime=p.mimeType, raw_bytes=p.text.encode("utf-8")))
            continue
        if isinstance(p, mcp_types.BlobResourceContents):
            base = p.blob  # base64 string per MCP spec
            norm.append(BlobPart(mime=p.mimeType, raw_str=str(base)))
            continue
        raise TypeError(f"Unsupported resource content type: {type(p).__name__}")
    return norm


def _iter_window_parts(
    contents: Sequence[mcp_types.TextResourceContents | mcp_types.BlobResourceContents],
    start_offset: int,
    max_bytes: int | None,
) -> Iterator[WindowedPart]:
    remaining: int | None = max_bytes if isinstance(max_bytes, int) and max_bytes > 0 else None
    cursor = 0
    for part in _normalize_parts(contents):
        mime = part.mime
        if isinstance(part, TextPart):
            raw: bytes = part.raw_bytes
            total_len = len(raw)
            if remaining is None or remaining > 0:
                start_in_part = max(0, start_offset - cursor)
                take_cap = remaining if isinstance(remaining, int) else total_len
                take = max(0, min(take_cap, total_len - start_in_part))
                if take > 0:
                    chunk = raw[start_in_part : start_in_part + take]
                    yield WindowedTextPart(
                        mime=mime,
                        text=chunk.decode("utf-8", errors="replace"),
                        total_bytes=total_len,
                        bytes_returned=take,
                    )
                    if remaining is not None:
                        remaining -= take
            cursor += total_len
        elif isinstance(part, BlobPart):
            base: str = part.raw_str
            total_len = len(base)
            if remaining is None or remaining > 0:
                start_in_part = max(0, start_offset - cursor)
                take_cap = remaining if isinstance(remaining, int) else total_len
                take = max(0, min(take_cap, total_len - start_in_part))
                if take > 0:
                    yield WindowedBlobPart(
                        mime=mime,
                        base64=base[start_in_part : start_in_part + take],
                        total_bytes=total_len,
                        bytes_returned=take,
                    )
                    if remaining is not None:
                        remaining -= take
            cursor += total_len
        if remaining is not None and remaining <= 0:
            break


def _build_window_payload(
    contents: Sequence[mcp_types.TextResourceContents | mcp_types.BlobResourceContents],
    start_offset: int,
    max_bytes: int | None,
) -> ResourceReadResult:
    # TODO: Optimization for full resource reads
    # When reading a full resource (start_offset=0 and max_bytes covers the entire resource),
    # return the content as native MCP blocks instead of the current windowed response format.
    # This would be more efficient and idiomatic for full resource reads.
    # Current behavior: Always returns windowed response with {"window": {...}, "parts": [...], "total_parts": N}
    # Proposed: When full resource is read, return as standard MCP TextResourceContents or BlobResourceContents
    parts_out: list[WindowedPart] = list(_iter_window_parts(contents, start_offset, max_bytes))
    return ResourceReadResult(
        window=ResourceWindowInfo(start_offset=start_offset, max_bytes=max_bytes or 0),
        parts=parts_out,
        total_parts=len(contents),
    )


class ResourcesServer(EnhancedFastMCP):
    """Resources MCP server with typed tool access.

    Aggregates resources across servers for discovery and reading.

    - Synthetic server injected by the runtime; reserved mount name is ``resources``.
    - Provides a uniform API to discover and read resources exposed by other servers.
    - Only servers that advertise ``initialize.capabilities.resources`` are queried.

    **Policy enforcement architecture:**
        - LLM tool calls to this server go through the policy gateway (tool-level enforcement)
        - This server's internal operations use direct compositor methods to avoid client dependency
        - This prevents double policy enforcement and keeps the resources server as a pure facade

    **Window semantics:**
    - Windowing is byte-based across the concatenation of all parts reported by the
      underlying server. Text is sliced by UTF-8 bytes and decoded with
      ``errors="replace"`` if a multi-byte character is split at the boundary.
    - Base64 parts are sliced as base64 text; decoding is the caller's responsibility.
    """

    # Tool references
    list_tool: FlatTool
    list_templates_tool: FlatTool
    read_tool: FlatTool
    read_blocks_tool: FlatTool

    def __init__(self, *, compositor: BaseCompositor):
        """Create a Resources MCP server.

        Args:
            compositor: BaseCompositor for resource operations and metadata
        """
        # Initialize state
        self._compositor = compositor

        # Pass explicit version to avoid importlib.metadata.version() lookup which can hang under pytest-xdist
        super().__init__(
            RESOURCES_SERVER_NAME,
            version="1.0.0",
            instructions=(
                "Resources aggregator for accessing MCP resources across all mounted servers.\n\n"
                "**Access model:** Resources are identified by (server, URI) pairs. Each resource belongs to "
                "a specific MCP server that exposes it. Use this server to discover what resources are available "
                "and read their contents without directly connecting to each origin server.\n\n"
                "**Discovery:** Use `list` to find available resources (optionally filtered by server name or URI prefix). "
                "Each result includes the origin server name and the resource URI. "
                "Use `list_resource_templates` to discover URI templates (RFC 6570) that describe patterns "
                "for constructing parameterized resource URIs.\n\n"
                "**Reading:** Use `read` to fetch resource contents by specifying the (server, URI) pair. "
                "Supports optional windowing for large resources (e.g., read lines 100-200 from a text resource).\n\n"
                "**Important:** Resources are server-specific - the same URI path on different servers "
                "represents different resources. Always specify both server and URI when accessing resources.\n\n"
                "Note: Only servers that advertise the resources capability in their initialize response are queried."
            ),
        )

        # Register tools
        async def list_resources(input: ResourcesListArgs) -> ResourcesListResult:
            """List MCP resources that are exposed by servers (if any). Filter by server name or URI prefix if desired.

            Returns only resources that MCP servers explicitly expose via the resources capability.
            Call this first to see what resources are available before trying to read them.
            """
            # Query each mounted server individually to get unprefixed resources
            entries = await self._compositor.server_entries()
            out: list[ResourceEntry] = []

            for server_name, entry in entries.items():
                # Filter by server name if specified
                if input.server and server_name != input.server:
                    continue

                if not isinstance(entry, RunningServerEntry):
                    continue

                # Only query servers that advertise resources capability
                if entry.initialize.capabilities.resources is None:
                    continue

                # Get resources directly from this server (unprefixed)
                child_client = self._compositor.get_child_client(server_name)
                resources = await child_client.list_resources()

                for r in resources:
                    # Apply uri_prefix filter if specified
                    if input.uri_prefix and not str(r.uri).startswith(input.uri_prefix):
                        continue
                    out.append(ResourceEntry(server=server_name, resource=r))

            return ResourcesListResult(resources=out)

        self.list_tool = self.flat_model()(list_resources)

        async def list_resource_templates() -> ResourceTemplatesListResult:
            """List URI templates (RFC 6570) that servers expose for constructing resource URIs, tagged by origin server.

            Returns templates only from servers that expose them via the resources capability.
            Use this to discover what resource URIs can be constructed (e.g., resource://runtime/container.info).
            """
            # Query each mounted server individually to track template ownership
            entries = await self._compositor.server_entries()
            out: list[ResourceTemplateEntry] = []
            for server_prefix, entry in entries.items():
                if not isinstance(entry, RunningServerEntry):
                    continue
                # Only query servers that advertise resources capability
                # (templates are part of the resources capability)
                if entry.initialize.capabilities.resources is None:
                    continue
                # Query this server's templates
                child_client = self._compositor.get_child_client(server_prefix)
                templates = await child_client.list_resource_templates()
                out.extend(ResourceTemplateEntry(server=server_prefix, template=template) for template in templates)
            return ResourceTemplatesListResult(templates=out)

        self.list_templates_tool = self.flat_model()(list_resource_templates)

        async def read(input: ResourcesReadArgs) -> ResourceReadResult:
            """Read contents of an MCP resource that a server exposes. Use start_offset and max_bytes for pagination.

            Only works for resources that servers explicitly expose via their resources capability.
            Use list_resources first to see what resources are available.
            """
            prefixed = add_resource_prefix(input.uri, input.server)
            uri_value = ANY_URL.validate_python(prefixed)
            # Call compositor method that converts FastMCP types to MCP protocol types
            # (resources server is tightly coupled to compositor for metadata)
            try:
                contents = await self._compositor.read_resource_contents(uri_value)
            except McpError as e:
                raise ToolError(
                    f"The MCP server '{input.server}' does not provide the resource '{input.uri}'. "
                    f"Use list_resources to see available resources. Original error: {e}"
                ) from e
            max_bytes = input.max_bytes if input.max_bytes is not None else DEFAULT_MAX_BYTES
            return _build_window_payload(contents, input.start_offset, max_bytes)

        self.read_tool = self.flat_model()(read)

        async def read_blocks(input: ReadBlocksArgs) -> ReadBlocksResult:
            """Read MCP resource with block-level windowing. Returns complete blocks plus truncation markers for partial blocks.

            Only works for resources that servers explicitly expose via their resources capability.
            Use list_resources first to see what resources are available.

            Size semantics:
            - Text blocks: size measured in UTF-8 bytes (not characters)
            - Blob blocks: size measured in base64 string length (not decoded bytes)
            - max_bytes limit applies to the sum across all block types

            Slicing behavior:
            - Text: slice at UTF-8 byte boundaries (may split multi-byte characters, uses errors='replace')
            - Blob: slice base64 string directly (always valid since base64 is ASCII)
            """
            prefixed = add_resource_prefix(input.uri, input.server)
            uri_value = ANY_URL.validate_python(prefixed)
            try:
                contents = await self._compositor.read_resource_contents(uri_value)
            except McpError as e:
                raise ToolError(
                    f"The MCP server '{input.server}' does not provide the resource '{input.uri}'. "
                    f"Use list_resources to see available resources. Original error: {e}"
                ) from e

            max_bytes = input.max_bytes if input.max_bytes is not None else DEFAULT_MAX_BYTES
            result_blocks: list[mcp_types.TextResourceContents | mcp_types.BlobResourceContents | TruncatedBlock] = []
            bytes_accumulated = 0

            for block_idx, block in enumerate(contents):
                # Skip blocks before start_block
                if block_idx < input.start_block:
                    continue

                # Get block size
                if isinstance(block, mcp_types.TextResourceContents):
                    # For text, measure size in UTF-8 bytes
                    block_bytes = block.text.encode("utf-8")
                    block_size = len(block_bytes)
                    is_text = True
                elif isinstance(block, mcp_types.BlobResourceContents):
                    # For blob, size is base64 string length (no need for bytes representation)
                    block_size = len(block.blob)
                    is_text = False
                    block_bytes = b""  # Not used for blobs
                else:
                    raise TypeError(f"Unsupported content type: {type(block).__name__}")

                # Determine slice range for this block
                slice_start = input.start_offset if block_idx == input.start_block else 0
                remaining_budget = max_bytes - bytes_accumulated

                if slice_start >= block_size:
                    # start_offset is beyond this block, skip it
                    continue

                available_in_block = block_size - slice_start
                can_take = min(available_in_block, remaining_budget)

                sliced_content: BaseResourceContents
                if can_take >= available_in_block:
                    # Can include full remainder of block
                    if slice_start == 0:
                        # Full block, no truncation
                        result_blocks.append(block)
                    else:
                        # Partial from start
                        slice_end = block_size
                        if is_text:
                            sliced_bytes = block_bytes[slice_start:slice_end]
                            sliced_content = mcp_types.TextResourceContents(
                                uri=block.uri,
                                mimeType=block.mimeType,
                                text=sliced_bytes.decode("utf-8", errors="replace"),
                            )
                        else:
                            # blob is base64 string, slice directly
                            assert isinstance(block, mcp_types.BlobResourceContents)
                            sliced_content = mcp_types.BlobResourceContents(
                                uri=block.uri, mimeType=block.mimeType, blob=block.blob[slice_start:slice_end]
                            )
                        result_blocks.append(
                            TruncatedBlock(
                                block_index=block_idx,
                                started_at=slice_start,
                                ended_at=slice_end,
                                full_size=block_size,
                                content=sliced_content,
                            )
                        )
                    bytes_accumulated += available_in_block
                else:
                    # Must truncate this block
                    slice_end = slice_start + can_take
                    if is_text:
                        sliced_bytes = block_bytes[slice_start:slice_end]
                        sliced_content = mcp_types.TextResourceContents(
                            uri=block.uri, mimeType=block.mimeType, text=sliced_bytes.decode("utf-8", errors="replace")
                        )
                    else:
                        # blob is base64 string, slice directly
                        assert isinstance(block, mcp_types.BlobResourceContents)
                        sliced_content = mcp_types.BlobResourceContents(
                            uri=block.uri, mimeType=block.mimeType, blob=block.blob[slice_start:slice_end]
                        )
                    result_blocks.append(
                        TruncatedBlock(
                            block_index=block_idx,
                            started_at=slice_start,
                            ended_at=slice_end,
                            full_size=block_size,
                            content=sliced_content,
                        )
                    )
                    bytes_accumulated += can_take
                    break  # Hit budget limit

                if bytes_accumulated >= max_bytes:
                    break

            return ReadBlocksResult(blocks=result_blocks)

        self.read_blocks_tool = self.flat_model()(read_blocks)
