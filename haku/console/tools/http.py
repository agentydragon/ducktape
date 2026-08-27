"""Credential-free in-process MCP tools for owned, principal-scoped HTTP egress grants."""

from __future__ import annotations

import datetime
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from pydantic import Field

from haku.console.grant_principal import GrantPrincipalKind, grant_principal_for
from haku.console.http_grant_models import HttpGrant, HttpOrigin
from haku.console.http_grant_service import HttpGrantService
from haku.console.mcp_execution import McpExecutionContext, require_mcp_execution_context

HTTP_SERVER_ID = "http"
_EXECUTION_CONTEXT_DEPENDENCY = Depends(require_mcp_execution_context)


class HttpToolsService:
    def __init__(self, *, grants: HttpGrantService) -> None:
        self.grants = grants

    async def create_grants(
        self,
        *,
        context: McpExecutionContext,
        origins: list[HttpOrigin],
        duration_seconds: int,
        applies_to: GrantPrincipalKind = GrantPrincipalKind.AGENT,
    ) -> tuple[HttpGrant, ...]:
        principal = context.request_principal
        if context.tool_call_id is None:
            raise PermissionError("HTTP grant creation requires durable tool-call provenance")
        now = datetime.datetime.now(datetime.UTC)
        return await self.grants.create_grants(
            owner_agent_id=principal.agent_id,
            grant_principal=grant_principal_for(principal, applies_to),
            source_tool_call_id=context.tool_call_id,
            origins=origins,
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


def build_mcp(service: HttpToolsService) -> FastMCP:
    """Build one stable server instance; request identity enters only via hidden dependencies."""

    mcp = FastMCP(
        name=HTTP_SERVER_ID,
        instructions=(
            "Create/release explicit Agent- or session-scoped temporary HTTP egress grants for exact "
            "canonical public origins (scheme, IDNA A-label host, explicit port) — no wildcards, paths, "
            "methods, or IP literals. One create_grant call may create multiple exact origins with a "
            "shared expiry. One release_grants call may release up to 32 durable grant IDs sequentially. "
            "Agent identity and tool-call provenance are trusted request metadata, never tool arguments."
        ),
    )

    @mcp.tool
    async def create_grant(
        origins: Annotated[
            list[HttpOrigin],
            Field(
                min_length=1,
                max_length=32,
                description="Exact origins to grant atomically with one shared start and expiry.",
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
        context: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[HttpGrant]:
        return list(
            await service.create_grants(
                context=context, origins=origins, duration_seconds=duration_seconds, applies_to=applies_to
            )
        )

    @mcp.tool
    async def list_grants(context: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY) -> list[HttpGrant]:
        return list(await service.list_grants(context=context))

    @mcp.tool
    async def get_grant(
        grant_id: Annotated[UUID, Field(description="Grant UUID returned by create_grant.")],
        context: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY,
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
        context: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[HttpGrant]:
        return list(await service.release_grants(context=context, grant_ids=grant_ids, reason=reason))

    return mcp
