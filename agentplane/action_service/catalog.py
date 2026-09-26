"""Reviewed, config-driven ActionGroup/Action catalog: the Agent-facing discovery seam.

Groups are reviewed runtime configuration. Child Actions may be mirrored from the bound backend.
The same group/action lookup drives discovery and ActionService admission and routing.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

from agentplane.action_service.sandbox.binding import SandboxExecutorBinding

_KEY = r"^[a-z][a-z0-9_-]*$"
Key = Annotated[str, StringConstraints(pattern=_KEY, min_length=1, max_length=200)]


class ActionIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    group: Key
    name: Key


class ActionDefinition(BaseModel):
    """One namespaced Action's Agent-facing description and parameter contract."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        min_length=1, max_length=20_000, description="Tool description; some MCP servers write long ones."
    )
    input_schema: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="A small JSON-Schema-shaped parameter contract, opaque to this catalog. Submission "
        "validates against this advertised schema; execution re-checks the current backend schema.",
    )


class McpExecutorBinding(BaseModel):
    """Where an ActionGroup's Actions execute. Reviewed runtime configuration, not a live registry."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["mcp"] = "mcp"
    description: str = Field(
        min_length=1,
        max_length=2000,
        description="Agent-visible executor description, e.g. account/credential ownership.",
    )
    config: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Adapter-specific backend configuration (e.g. server address, account reference). "
        "Never projected to an Agent; excluded from every discovery view.",
    )


class McpLifecycle(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    DISCOVERING = "discovering"
    AVAILABLE = "available"
    DRAINING = "draining"
    STOPPED = "stopped"


class McpUnavailableReason(StrEnum):
    CONNECT_FAILED = "connect_failed"
    DISCOVERY_FAILED = "discovery_failed"
    INVALID_CATALOG = "invalid_catalog"
    LINKAGE_UNAVAILABLE = "linkage_unavailable"
    SESSION_FAILED = "session_failed"
    SUPERVISOR_STOPPED = "supervisor_stopped"


class McpHealth(BaseModel):
    """Replica-local diagnostics. Only `detail` may carry backend exception text or configuration."""

    state: McpLifecycle = McpLifecycle.DISCONNECTED
    reason: McpUnavailableReason | None = None
    detail: str | None = Field(
        default=None,
        description="What `reason` came from: the backend's error, or the OAuth linkage's state. Operators only: "
        "it can name backend addresses, so a workload's view leaves it out.",
    )
    last_discovery_at: datetime | None = None
    retry_at: datetime | None = None
    failures: int = 0


type ExecutorBinding = Annotated[McpExecutorBinding | SandboxExecutorBinding, Field(discriminator="kind")]
"""Where a group's Actions run. `mcp` reaches a reviewed upstream server; `sandbox` runs in this
service, because the caller's own identity is what it acts as and an MCP hop would have to carry
that assertion over the wire.

A PEP 695 alias and not a bare `Annotated`, so the discriminator survives on the field's own
annotation: pydantic folds `Annotated` metadata into its `FieldInfo`, where a deployment rendering
this settings model (`util/settings_contract.py`) reads the annotation and would find a union it
cannot pick a member of.
"""


class ActionGroup(BaseModel):
    """The discovery and ownership unit: one executor binding, many namespaced child Actions."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    executor: ExecutorBinding
    available: bool = Field(default=True, description="Whether this group is currently offered to Agents.")
    health: McpHealth | None = Field(default=None, exclude=True)
    actions: dict[Key, ActionDefinition] = Field(default_factory=dict)


class ActionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: str
    name: str
    description: str
    input_schema: dict[str, JsonValue]


class ActionGroupView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    description: str
    executor_kind: str
    executor_description: str
    available: bool
    health: McpHealth | None = None
    actions: list[ActionView]


class UnknownActionError(Exception):
    def __init__(self, group_key: str, action_key: str) -> None:
        super().__init__(f"unknown group/action {(group_key, action_key)!r}")
        self.group_key = group_key
        self.action_key = action_key


class ActionUnavailableError(Exception):
    """A known group has no currently validated catalog; nothing was dispatched."""


class ActionCatalog(BaseModel):
    """The validated, reviewed universe of ActionGroups this Action Service process was started with."""

    model_config = ConfigDict(extra="forbid")

    groups: dict[Key, ActionGroup] = Field(default_factory=dict)

    # Lets the operator settings page join an ActionGroupView back to its McpLinkageView by
    # matching `key` against `server_id` directly, with no separate wire-carried join key needed.
    @model_validator(mode="after")
    def _server_id_matches_group_key(self) -> ActionCatalog:
        for key, group in self.groups.items():
            if not isinstance(group.executor, McpExecutorBinding):
                continue
            server_id = group.executor.config.get("server_id")
            if server_id is not None and server_id != key:
                raise ValueError(f"ActionGroup {key!r} executor config server_id {server_id!r} must match its key")
        return self

    def resolve(self, group_key: str, action_key: str) -> tuple[ActionGroup, ActionDefinition]:
        group = self.groups.get(group_key)
        if group is not None and not group.available:
            raise ActionUnavailableError("ActionGroup is temporarily unavailable")
        action = group.actions.get(action_key) if group is not None else None
        if group is None or action is None:
            raise UnknownActionError(group_key, action_key)
        return group, action

    def group_views(self, *, with_detail: bool) -> list[ActionGroupView]:
        """`with_detail` keeps `McpHealth.detail`, which only an operator may read."""
        return [_group_view(key, group, with_detail=with_detail) for key, group in self.groups.items()]

    def action_view(self, group_key: str, action_key: str) -> ActionView:
        _, action = self.resolve(group_key, action_key)
        return _action_view(group_key, action_key, action)


def _action_view(group_key: str, action_key: str, action: ActionDefinition) -> ActionView:
    return ActionView(
        group=group_key, name=action_key, description=action.description, input_schema=action.input_schema
    )


def _group_view(group_key: str, group: ActionGroup, *, with_detail: bool) -> ActionGroupView:
    return ActionGroupView(
        key=group_key,
        title=group.title,
        description=group.description,
        executor_kind=group.executor.kind,
        executor_description=group.executor.description,
        available=group.available,
        health=group.health
        if with_detail or group.health is None
        else group.health.model_copy(update={"detail": None}),
        actions=[_action_view(group_key, name, action) for name, action in group.actions.items()]
        if group.available
        else [],
    )
