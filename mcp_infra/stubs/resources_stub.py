"""Typed stubs for resources MCP server."""

from mcp_infra.compositor.resources_server import (
    ReadBlocksArgs,
    ReadBlocksResult,
    ResourcesListArgs,
    ResourcesListResult,
    ResourceTemplatesListResult,
)
from mcp_infra.stubs.server_stubs import ServerStub


class ResourcesServerStub(ServerStub):
    """Typed stub for resources server operations."""

    async def list_resources(self, input: ResourcesListArgs) -> ResourcesListResult:
        raise NotImplementedError  # Auto-wired at runtime

    async def read_blocks(self, input: ReadBlocksArgs) -> ReadBlocksResult:
        raise NotImplementedError  # Auto-wired at runtime

    async def list_resource_templates(self) -> ResourceTemplatesListResult:
        raise NotImplementedError  # Auto-wired at runtime
