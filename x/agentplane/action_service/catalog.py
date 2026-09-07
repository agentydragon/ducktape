"""Reviewed, config-driven ActionGroup/Action catalog: the Agent-facing discovery seam.

Groups and their child Actions are code-owned data, not a dynamic registry: an operator edits the
configured catalog (see `main.Settings.action_groups`) and the process picks it up on restart. This
module owns validation and lookup only; it does not select an Executor or gate ActionRequest
submission — that remains `db.ActionStore.submit`'s `supported_capabilities` check.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

_KEY = r"^[a-z][a-z0-9_-]*$"
Key = Annotated[str, StringConstraints(pattern=_KEY, min_length=1, max_length=200)]


class ActionDefinition(BaseModel):
    """One namespaced Action's Agent-facing description and parameter contract."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        min_length=1, max_length=20_000, description="Tool description; some MCP servers write long ones."
    )
    input_schema: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="A small JSON-Schema-shaped parameter contract, opaque to this catalog. Execution "
        "re-checks the current executor/tool schema; this catalog entry is discovery only.",
    )


class ExecutorBinding(BaseModel):
    """Where an ActionGroup's Actions execute. Reviewed runtime configuration, not a live registry."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=100, description="Executor adapter kind, e.g. 'mcp' or 'hostexec'.")
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


class ActionGroup(BaseModel):
    """The discovery and ownership unit: one executor binding, many namespaced child Actions."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    executor: ExecutorBinding
    available: bool = Field(default=True, description="Whether this group is currently offered to Agents.")
    actions: dict[Key, ActionDefinition] = Field(default_factory=dict)


class ActionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: str
    name: str
    id: str
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
    actions: list[ActionView]


class UnknownActionError(Exception):
    def __init__(self, group_key: str, action_key: str) -> None:
        super().__init__(f"unknown group/action {group_key}.{action_key}")
        self.group_key = group_key
        self.action_key = action_key


class ActionCatalog(BaseModel):
    """The validated, reviewed universe of ActionGroups this Action Service process was started with."""

    model_config = ConfigDict(extra="forbid")

    groups: dict[Key, ActionGroup] = Field(default_factory=dict)

    def resolve(self, group_key: str, action_key: str) -> tuple[ActionGroup, ActionDefinition]:
        group = self.groups.get(group_key)
        action = group.actions.get(action_key) if group is not None else None
        if group is None or action is None:
            raise UnknownActionError(group_key, action_key)
        return group, action

    def group_views(self) -> list[ActionGroupView]:
        return [_group_view(key, group) for key, group in self.groups.items()]

    def action_view(self, group_key: str, action_key: str) -> ActionView:
        _, action = self.resolve(group_key, action_key)
        return _action_view(group_key, action_key, action)


def _action_view(group_key: str, action_key: str, action: ActionDefinition) -> ActionView:
    return ActionView(
        group=group_key,
        name=action_key,
        id=f"{group_key}.{action_key}",
        description=action.description,
        input_schema=action.input_schema,
    )


def _group_view(group_key: str, group: ActionGroup) -> ActionGroupView:
    return ActionGroupView(
        key=group_key,
        title=group.title,
        description=group.description,
        executor_kind=group.executor.kind,
        executor_description=group.executor.description,
        available=group.available,
        actions=[_action_view(group_key, name, action) for name, action in group.actions.items()],
    )
