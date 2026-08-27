"""Credential-free in-process MCP tools for owned, principal-scoped HTTP egress grants."""

from __future__ import annotations

import datetime
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from pydantic import Field

from haku.console.agents.enrollment import AgentEnrollmentService
from haku.console.grant_principal import GrantPrincipalKind, grant_principal_for
from haku.console.http_grant_models import HttpGrant, HttpGrantNotFoundError, HttpGrantSpec
from haku.console.http_grant_service import HttpGrantService
from haku.console.mcp_execution import EXECUTION_CONTEXT_DEPENDENCY, McpExecutionContext, OperatorMcpExecutionCaller

HTTP_GRANTS_SERVER_ID = "http_grants"


class HttpToolsService:
    def __init__(self, *, grants: HttpGrantService, agents: AgentEnrollmentService) -> None:
        self.grants = grants
        self.agents = agents

    async def create_grants(
        self,
        *,
        context: McpExecutionContext,
        grants: list[HttpGrantSpec],
        duration_seconds: int,
        applies_to: GrantPrincipalKind,
    ) -> tuple[HttpGrant, ...]:
        principal = context.request_principal
        if context.tool_call_id is None:
            raise PermissionError("HTTP grant creation requires durable tool-call provenance")
        now = datetime.datetime.now(datetime.UTC)
        return await self.grants.create_grants(
            owner_agent_id=principal.agent_id,
            grant_principal=grant_principal_for(principal, applies_to),
            source_tool_call_id=context.tool_call_id,
            grants=grants,
            expires_at=now + datetime.timedelta(seconds=duration_seconds),
        )

    async def list_grants(self, *, context: McpExecutionContext) -> tuple[HttpGrant, ...]:
        return await self.grants.list_applicable_grants(request_principal=context.request_principal)

    async def get_grant(self, *, context: McpExecutionContext, grant_id: UUID) -> HttpGrant:
        return await self.grants.get_applicable_grant(request_principal=context.request_principal, grant_id=grant_id)

    async def release_grants(
        self, *, context: McpExecutionContext, grant_ids: list[UUID], reason: str = "released"
    ) -> tuple[HttpGrant, ...]:
        return await self.grants.release_applicable_grants(
            request_principal=context.request_principal, grant_ids=grant_ids, reason=reason
        )

    async def revoke_grants(
        self, *, context: McpExecutionContext, owner_agent_id: UUID, grant_ids: list[UUID], reason: str
    ) -> tuple[HttpGrant, ...]:
        """Operator-direct revocation of an owned Agent's grants; Agents release, never revoke."""

        if not isinstance(context.caller, OperatorMcpExecutionCaller):
            raise PermissionError("grant revocation requires Operator-direct execution")
        owned = await self.agents.list_agents(operator_id=context.caller.operator_id)
        if owner_agent_id not in {agent.agent_id for agent in owned}:
            raise HttpGrantNotFoundError(str(owner_agent_id))
        return await self.grants.revoke_grants(owner_agent_id=owner_agent_id, grant_ids=grant_ids, reason=reason)


def build_mcp(service: HttpToolsService) -> FastMCP:
    """Build one stable server instance; request identity enters only via hidden dependencies."""

    mcp = FastMCP(
        name=HTTP_GRANTS_SERVER_ID,
        instructions=(
            "Create/release explicit Agent- or session-scoped temporary HTTP egress grants. One grant "
            "covers one exact canonical public origin (scheme, IDNA A-label host, explicit port) narrowed "
            "by an explicit method set and an optional path regex — no wildcard hosts or IP literals. "
            "A grant may additionally name a Console-owned credential by handle: requests it admits then "
            "have that credential's inert placeholder replaced with the real value at the egress proxy — "
            "the value itself is never visible here or in the sandbox. "
            "One create_grant call may create multiple grants with a shared expiry. One release_grants "
            "call may release up to 32 durable grant IDs sequentially; revoke_grants is the Operator's "
            "direct revocation surface. Agent identity and tool-call provenance are trusted request "
            "metadata, never tool arguments."
        ),
    )

    @mcp.tool
    async def create_grant(
        grants: Annotated[
            list[HttpGrantSpec],
            Field(
                min_length=1,
                max_length=32,
                description="Exact coverage items to grant atomically with one shared start and expiry.",
            ),
        ],
        duration_seconds: Annotated[
            int,
            Field(
                ge=1,
                le=86_400,
                description="Requested duration in seconds; the deployment may enforce a lower maximum.",
            ),
        ],
        applies_to: Annotated[
            GrantPrincipalKind,
            Field(
                description=(
                    "Principal applicability resolved from trusted source identity. "
                    "'agent' covers every authenticated execution of this Agent; 'session' "
                    "covers only the exact live session that submitted this ToolCall."
                )
            ),
        ] = GrantPrincipalKind.AGENT,
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[HttpGrant]:
        return list(
            await service.create_grants(
                context=context, grants=grants, duration_seconds=duration_seconds, applies_to=applies_to
            )
        )

    @mcp.tool
    async def list_grants(context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY) -> list[HttpGrant]:
        return list(await service.list_grants(context=context))

    @mcp.tool
    async def get_grant(
        grant_id: Annotated[UUID, Field(description="Grant UUID returned by create_grant.")],
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> HttpGrant:
        return await service.get_grant(context=context, grant_id=grant_id)

    @mcp.tool
    async def release_grants(
        grant_ids: Annotated[
            list[UUID],
            Field(
                min_length=1,
                max_length=32,
                description="Grant UUIDs returned by create_grant; released sequentially in the supplied order.",
            ),
        ],
        reason: Annotated[str, Field(min_length=1, max_length=500)] = "released",
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[HttpGrant]:
        return list(await service.release_grants(context=context, grant_ids=grant_ids, reason=reason))

    @mcp.tool
    async def revoke_grants(
        owner_agent_id: Annotated[UUID, Field(description="The acting Operator's owned Agent whose grants to end.")],
        grant_ids: Annotated[
            list[UUID],
            Field(min_length=1, max_length=32, description="Grant UUIDs revoked sequentially in the supplied order."),
        ],
        reason: Annotated[str, Field(min_length=1, max_length=500)],
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[HttpGrant]:
        """Operator-direct only: an Agent caller is rejected and should release_grants instead."""
        return list(
            await service.revoke_grants(
                context=context, owner_agent_id=owner_agent_id, grant_ids=grant_ids, reason=reason
            )
        )

    return mcp
