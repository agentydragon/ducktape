"""Typed stubs for resources MCP server."""

from mcp_infra.compositor.resources_server import (
    ListSubscribeArgs,
    ReadBlocksArgs,
    ReadBlocksResult,
    ResourcesListArgs,
    ResourcesListResult,
    ResourcesSubscribeArgs,
    ResourceTemplatesListResult,
)
from mcp_infra.mcp_types import SimpleOk
from mcp_infra.resource_utils import read_text_json_typed
from mcp_infra.resources.types import SubscriptionsIndex
from mcp_infra.stubs.server_stubs import ServerStub


class ResourcesServerStub(ServerStub):
    """Typed stub for resources server operations."""

    async def list_resources(self, input: ResourcesListArgs) -> ResourcesListResult:
        raise NotImplementedError  # Auto-wired at runtime

    async def subscribe(self, input: ResourcesSubscribeArgs) -> SimpleOk:
        raise NotImplementedError  # Auto-wired at runtime

    async def unsubscribe(self, input: ResourcesSubscribeArgs) -> SimpleOk:
        raise NotImplementedError  # Auto-wired at runtime

    async def subscribe_list_changes(self, input: ListSubscribeArgs) -> SimpleOk:
        raise NotImplementedError  # Auto-wired at runtime

    async def unsubscribe_list_changes(self, input: ListSubscribeArgs) -> SimpleOk:
        raise NotImplementedError  # Auto-wired at runtime

    async def read_blocks(self, input: ReadBlocksArgs) -> ReadBlocksResult:
        raise NotImplementedError  # Auto-wired at runtime

    async def list_resource_templates(self) -> ResourceTemplatesListResult:
        raise NotImplementedError  # Auto-wired at runtime

    async def list_subscriptions(self) -> SubscriptionsIndex:
        """Read the subscriptions index resource and parse into a typed model."""
        return await read_text_json_typed(self._client._session, "resources://subscriptions", SubscriptionsIndex)
