"""Reviewed ActionGroup configuration shared by deployment synthesis and the Go service."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, field_serializer

from agentplane.action_service.orchestration_v1.binding import OrchestrationExecutorBinding
from agentplane.action_service.sandbox.binding import PodExecutorBinding

_KEY = r"^[a-z][a-z0-9_-]*$"
Key = Annotated[str, StringConstraints(pattern=_KEY, min_length=1, max_length=200)]
DIRECT_TOOL_SEPARATOR = "__"


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
    title: str | None = Field(default=None, description="Human-readable display name, as MCP tools carry one.")
    annotations: ToolAnnotations | None = Field(
        default=None,
        description="MCP behavior hints (read-only, destructive, idempotent, open-world). Advisory: they "
        "shape how a client presents the Action as a direct tool, never what the service allows.",
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


type ExecutorBinding = Annotated[
    McpExecutorBinding | PodExecutorBinding | OrchestrationExecutorBinding, Field(discriminator="kind")
]
"""Where a group's Actions run. `mcp` reaches a reviewed upstream server; `pod` runs in this
service, because the caller's own identity is what it acts as and an MCP hop would have to carry
that assertion over the wire; `orchestration_v1` runs in this service too, because it writes grants
for that same caller.

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
    actions: dict[Key, ActionDefinition] = Field(default_factory=dict)
    direct_tools: frozenset[Key] | None = Field(
        default=None,
        min_length=1,
        description="Actions of this group external Connections also see as MCP tools of their own, named "
        f"`<group>{DIRECT_TOOL_SEPARATOR}<action>`. Only these: a new upstream tool is never exposed unreviewed.",
    )

    @field_serializer("direct_tools")
    def _sorted_direct_tools(self, direct_tools: frozenset[str] | None) -> list[str] | None:
        # A set's iteration order follows per-process string hashing, and the manifest generator
        # writes this group into a ConfigMap that has to come out the same on every run.
        return None if direct_tools is None else sorted(direct_tools)
