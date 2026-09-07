from __future__ import annotations

from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, Field

from mcp_infra.prefix import MCPMountPrefix


class ResourceEntry(BaseModel):
    server: MCPMountPrefix = Field(description="Origin MCP server mount prefix")
    resource: mcp_types.Resource
    model_config = ConfigDict(extra="forbid")
