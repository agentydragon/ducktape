"""MCP domain types with Pydantic validation.

Provides strongly-typed, validated types for MCP identifiers and data structures.

Note: This module imports fastmcp which is slow to load (~2.5s). For code that
only needs MCPMountPrefix (e.g., policy evaluation), import from mcp_infra.prefix
instead.
"""

from __future__ import annotations

from enum import StrEnum

from fastmcp.mcp_config import MCPServerTypes
from fastmcp.server import FastMCP
from pydantic import BaseModel, Field

# MCP server specs: either typed specs (MCPServerTypes) or in-process server instances (FastMCP)
McpServerSpecs = dict[str, MCPServerTypes | FastMCP]


# ==============================================================================
# Container/Runtime Types
# ==============================================================================


class ContainerImageInfo(BaseModel):
    name: str | None = None
    id: str | None = None
    tags: list[str] | None = None


class NetworkMode(StrEnum):
    NONE = "none"
    BRIDGE = "bridge"
    HOST = "host"


class ContainerImageHistoryEntry(BaseModel):
    """One line from Docker image history (docker API).

    Docker engine returns keys with specific casing; we accept them via aliases and
    normalize to snake_case on our JSON output.
    """

    id: str | None = Field(default=None, alias="Id")
    created: int | None = Field(default=None, alias="Created")
    created_by: str | None = Field(default=None, alias="CreatedBy")
    tags: list[str] | None = Field(default=None, alias="Tags")
    size: int | None = Field(default=None, alias="Size")
    comment: str | None = Field(default=None, alias="Comment")


class ContainerInfo(BaseModel):
    """JSON shape for the runtime container.info resource.

    Returned by the runtime/docker exec servers as a single JSON part in ReadResourceResult.
    """

    image: ContainerImageInfo | dict
    container_id: str | None = None
    binds: dict | list | None = None
    working_dir: str | None = None
    network_mode: str | None = None
    image_history: list[ContainerImageHistoryEntry] | None = None


class SimpleOk(BaseModel):
    """Minimal ack type for tools that just signal success."""

    ok: bool = True
