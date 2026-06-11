from __future__ import annotations

from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, Field

from mcp_infra.prefix import MCPMountPrefix


class ResourceEntry(BaseModel):
    server: MCPMountPrefix = Field(description="Origin MCP server mount prefix")
    resource: mcp_types.Resource
    model_config = ConfigDict(extra="forbid")


class SubscriptionSummary(BaseModel):
    server: str
    uri: str
    pinned: bool
    present: bool
    active: bool
    last_error: str | None = None
    model_config = ConfigDict(extra="forbid")


class ListSubscriptionSummary(BaseModel):
    server: str
    present: bool
    active: bool
    model_config = ConfigDict(extra="forbid")


class SubscriptionsIndex(BaseModel):
    subscriptions: list[SubscriptionSummary]
    list_subscriptions: list[ListSubscriptionSummary] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")
