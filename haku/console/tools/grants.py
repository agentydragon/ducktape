"""Credential-free in-process MCP tools for owned, principal-scoped Kubernetes grants.

Kubernetes SAR inspection (`can_i`) is not a grant verb and lives on its own `kubernetes` server
(`haku.console.tools.kubernetes`).
"""

from __future__ import annotations

import datetime
from typing import Annotated, assert_never
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

import haku.console.grants.kubernetes.models as kubernetes_models
import haku.console.grants.kubernetes.service as kubernetes_service
import haku.console.tools.kubernetes as kubernetes_tools
from haku.console.grants.catalog import Grant, GrantCatalog
from haku.console.grants.envelope import GRANT_SET_LIMIT, GrantNotFoundError
from haku.console.grants.principal import GrantPrincipalInput, resolve_grant_principal_input
from haku.console.identity.enrollment import AgentEnrollmentService
from haku.console.mcp.execution import (
    EXECUTION_CONTEXT_DEPENDENCY,
    AgentMcpExecutionCaller,
    McpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
)
from haku.grants.authorization import AuthorizationDecision

GRANTS_SERVER_ID = "grants"


class GrantsToolsService:
    """Route the shared grant verbs to the Kubernetes grant service behind one server."""

    def __init__(
        self,
        *,
        kubernetes: kubernetes_service.GrantService,
        catalog: GrantCatalog,
        agents: AgentEnrollmentService,
        can_i: kubernetes_tools.KubernetesToolsService,
    ) -> None:
        self._kubernetes = kubernetes
        self._catalog = catalog
        self._agents = agents
        self._can_i = can_i

    async def kubernetes_can_i(
        self, *, context: McpExecutionContext, requests: list[kubernetes_tools.KubernetesAccessCheck]
    ) -> list[AuthorizationDecision]:
        return await self._can_i.can_i(context=context, requests=requests)

    async def create_grants(
        self,
        *,
        context: McpExecutionContext,
        requests: list[kubernetes_models.GrantSpec],
        duration_seconds: int | None,
        principal: GrantPrincipalInput,
    ) -> list[kubernetes_models.Grant]:
        if context.tool_call_id is None:
            raise PermissionError("grant creation requires durable tool-call provenance")
        request_principal = context.request_principal
        principal = resolve_grant_principal_input(principal, request_principal)
        expires_at = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=duration_seconds)
            if duration_seconds is not None
            else None
        )
        return list(
            await self._kubernetes.create_grants(
                owner_agent_id=request_principal.agent_id,
                grant_principal=principal,
                source_tool_call_id=context.tool_call_id,
                grants=requests,
                expires_at=expires_at,
            )
        )

    async def list_grants(
        self,
        *,
        context: McpExecutionContext,
        principal: GrantPrincipalInput | None = None,
        include_inactive: bool = False,
    ) -> list[Grant]:
        request_principal = context.request_principal
        if principal == "self":
            return list(
                await self._catalog.list_applicable(
                    request_principal=request_principal, include_inactive=include_inactive
                )
            )
        return list(await self._catalog.list(principal=principal, include_inactive=include_inactive))

    async def get_grant(self, *, context: McpExecutionContext, grant_id: UUID) -> Grant:
        return await self._catalog.get_kubernetes_grant(request_principal=context.request_principal, grant_id=grant_id)

    async def revoke_grants(
        self,
        *,
        context: McpExecutionContext,
        grant_ids: list[UUID],
        reason: str | None,
        owner_agent_id: UUID | None = None,
    ) -> list[kubernetes_models.Grant]:
        """End grants after authorizing the trusted caller's ownership scope."""

        match context.caller:
            case OperatorMcpExecutionCaller(operator_id=operator_id):
                if owner_agent_id is None:
                    raise ToolError("Operator revocation must name the owned Agent (owner_agent_id)")
                owned = await self._agents.list_agents(operator_id=operator_id)
                if owner_agent_id not in {agent.agent_id for agent in owned}:
                    raise GrantNotFoundError(str(owner_agent_id))
                return list(
                    await self._kubernetes.end_grants(owner_agent_id=owner_agent_id, grant_ids=grant_ids, reason=reason)
                )
            case AgentMcpExecutionCaller(principal=principal):
                if owner_agent_id is not None:
                    raise PermissionError("an Agent relinquishes only its own grants and may not name owner_agent_id")
                return list(
                    await self._kubernetes.end_applicable_grants(
                        request_principal=principal, grant_ids=grant_ids, reason=reason
                    )
                )
        assert_never(context.caller)


def build_mcp(service: GrantsToolsService) -> FastMCP:
    """Build one stable server instance; request identity enters only via hidden dependencies."""

    mcp = FastMCP(
        name=GRANTS_SERVER_ID,
        instructions=(
            "Create/list/get/end explicit Agent-, session-, or access-profile-scoped Kubernetes RBAC-like "
            "scope/rule database grants. get_grant and revoke_grants take the grant IDs returned by "
            "create/list. list_grants returns active database grants by default; include_inactive also "
            "returns expired and ended database history, while configuration-file grants are always "
            "listed. One revoke_grants call ends up to 32 durable grant IDs sequentially. An Agent ends "
            "its own grants; an Operator ends an owned Agent's grants by naming owner_agent_id. Agent "
            "identity and tool-call provenance are trusted request metadata, never tool arguments; grant "
            "creation is checked before any temporary authority is issued. whoami takes no arguments and "
            "returns the trusted console/MCP identity Console resolved for the caller (durable Agent id + "
            "live session id + access profile, or Operator id). " + kubernetes_tools.CAN_I_INSTRUCTIONS
        ),
    )

    @mcp.tool
    async def whoami(context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY) -> McpExecutionCaller:
        """Return the trusted identity Console resolved for this caller — its console/MCP principal.

        Takes no arguments and has no side effects. For an Agent it is the durable ``agent_id`` plus
        the live ``session_id`` (present only under a session bearer) and the ``access_profile_id``;
        for a direct Operator it is the ``operator_id``. This tool carries no approval provenance.
        """

        return context.caller

    @mcp.tool
    async def kubernetes_can_i(
        requests: Annotated[
            list[kubernetes_tools.KubernetesAccessCheck],
            Field(
                min_length=1,
                max_length=kubernetes_tools.CAN_I_BATCH_LIMIT,
                description="Kubernetes requests to authorize in one batch.",
            ),
        ],
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[AuthorizationDecision]:
        return await service.kubernetes_can_i(context=context, requests=requests)

    @mcp.tool
    async def create_grant(
        grants: Annotated[
            list[kubernetes_models.GrantSpec],
            Field(
                min_length=1,
                max_length=GRANT_SET_LIMIT,
                description="Exact Kubernetes scope/rule grants to create atomically with one shared start and expiry.",
            ),
        ],
        principal: Annotated[
            GrantPrincipalInput,
            Field(
                description=(
                    "Principal this grant covers. Use 'self' for the current session, or current Agent when no "
                    "session is active; otherwise name any valid Agent, live session, or access profile. "
                    "Creation is subject to Operator approval."
                )
            ),
        ],
        duration_seconds: Annotated[
            int | None,
            Field(
                ge=1,
                le=86_400,
                description=(
                    "Optional requested duration in seconds; omit for a grant without an expiry. "
                    "The deployment may enforce a lower maximum for expiring grants."
                ),
            ),
        ] = None,
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[kubernetes_models.Grant]:
        return await service.create_grants(
            context=context, requests=grants, duration_seconds=duration_seconds, principal=principal
        )

    @mcp.tool
    async def list_grants(
        principal: Annotated[
            GrantPrincipalInput | None,
            Field(
                description=(
                    "Grant subject to list. 'self' resolves the caller's trusted request principal and is "
                    "the click-free path. A named principal returns grants declared for exactly that subject; "
                    "it requires Operator approval. Omit it to list all declared grants."
                )
            ),
        ] = None,
        include_inactive: Annotated[
            bool,
            Field(
                description=(
                    "Include expired and ended database grants. Configuration-file grants are always listed because "
                    "they have no inactive lifecycle state."
                )
            ),
        ] = False,
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[Grant]:
        return await service.list_grants(context=context, principal=principal, include_inactive=include_inactive)

    @mcp.tool
    async def get_grant(
        grant_id: Annotated[UUID, Field(description="Grant UUID returned by create_grant.")],
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> Grant:
        return await service.get_grant(context=context, grant_id=grant_id)

    @mcp.tool
    async def revoke_grants(
        grant_ids: Annotated[
            list[UUID],
            Field(
                min_length=1,
                max_length=GRANT_SET_LIMIT,
                description="Grant UUIDs returned by create_grant; ended sequentially in the supplied order.",
            ),
        ],
        reason: Annotated[str | None, Field(max_length=500)] = None,
        owner_agent_id: Annotated[
            UUID | None,
            Field(
                description=(
                    "Operator-only: the acting Operator's owned Agent whose grants to revoke. Omit as an Agent "
                    "caller — you may end only your own grants."
                )
            ),
        ] = None,
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[kubernetes_models.Grant]:
        """End owned grants: an Agent may end its own, an Operator an owned Agent's."""
        return await service.revoke_grants(
            context=context, grant_ids=grant_ids, reason=reason, owner_agent_id=owner_agent_id
        )

    return mcp
