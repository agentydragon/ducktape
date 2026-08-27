"""Trusted execution identity for Console-owned in-process MCP tools — the execution role among the
five identity roles the tool-call domain keeps apart.

`McpExecutionCaller` and `McpExecutionContext` are the runtime-actor/execution role: the trusted
identity a Console-owned in-process tool reads at execution. The one boundary across the five roles:
an actor is a request principal plus the accountability identities (owning Operator, exact
credential binding) that authorization and audit read and applicability must not; grant principals
are stored selectors those request principals are tested against; tool-call principal rows are the
durable submitter provenance both are revalidated from. The other four definitions: the actor
`RuntimeActor` (`tool_call_actor.py`), `RequestPrincipal` and `GrantPrincipal` (`grant_principal.py`),
and the `McpToolCallPrincipal` submitter-provenance row (`database_schema.py`; wire in
`haku/shared/haku/console/tool_calls.py`).

The application service constructs this identity from authenticated actors and durable ToolCall
state. The dispatcher places it in MCP request metadata only for an in-process transport; a FastMCP
dependency reads and validates that metadata at tool execution. It never appears in tool arguments
or remote MCP traffic, and Console keeps one stable FastMCP server instance where credentials allow.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastmcp import Context
from fastmcp.dependencies import CurrentContext, Depends
from pydantic import BaseModel, ConfigDict, Field

from haku.console.grant_principal import RequestPrincipal

_HAKU_EXECUTION_META_KEY = "haku_execution"
_CURRENT_CONTEXT = CurrentContext()


class AgentMcpExecutionCaller(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["agent"] = "agent"
    # The complete trusted request principal, embedded whole rather than as scattered id fields.
    # Re-read from the durable Agent immediately before an approved call executes: the profile is
    # the fail-closed value for profile-scoped in-process servers, and the exact session is present
    # only when the ToolCall was submitted with a session bearer — static credentials omit it and
    # therefore cannot mint or use session principals.
    principal: RequestPrincipal


class OperatorMcpExecutionCaller(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["operator"] = "operator"
    operator_id: UUID


type McpExecutionCaller = Annotated[AgentMcpExecutionCaller | OperatorMcpExecutionCaller, Field(discriminator="kind")]


class McpExecutionContext(BaseModel):
    """Explicit caller and approval provenance for one trusted in-process tool execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    caller: McpExecutionCaller
    tool_call_id: str | None
    approving_operator_id: UUID | None
    approval_policy_id: str | None

    @property
    def request_principal(self) -> RequestPrincipal:
        """The trusted Agent principal this execution acts as.

        Operator-direct execution carries no Agent principal, so for it this access is the
        permission failure itself, not an absent value: a tool serving only Agents reads the
        property and lets the error propagate, while a tool that also serves Operator-direct
        execution dispatches on ``caller``.
        """

        if not isinstance(self.caller, AgentMcpExecutionCaller):
            raise PermissionError("an Agent caller is required")
        return self.caller.principal


def mcp_execution_request_meta(context: McpExecutionContext) -> dict[str, object]:
    """Serialize trusted execution identity into the reserved MCP request metadata field."""

    return {_HAKU_EXECUTION_META_KEY: context.model_dump(mode="json")}


def require_mcp_execution_context(ctx: Context = _CURRENT_CONTEXT) -> McpExecutionContext:
    """FastMCP dependency for Console-owned tools which require authenticated caller identity."""

    request_context = ctx.request_context
    meta = request_context.meta if request_context is not None else None
    raw = getattr(meta, _HAKU_EXECUTION_META_KEY, None) if meta is not None else None
    if raw is None:
        raise RuntimeError("trusted Haku MCP execution context is required")
    return McpExecutionContext.model_validate(raw)


# The default-parameter marker every Console-owned tool signature uses to receive the context.
EXECUTION_CONTEXT_DEPENDENCY = Depends(require_mcp_execution_context)
