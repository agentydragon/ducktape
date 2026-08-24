"""Credential-free in-process MCP tools for Agent-owned Kubernetes access."""

from __future__ import annotations

import datetime
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator

from haku.console.kubernetes_authorization import (
    AuthorizationRequest,
    AuthorizationResponse,
    KubernetesAuthorizationService,
    KubernetesAuthorizationSource,
    RequestAttributes,
    required_rule,
    required_scope,
)
from haku.console.kubernetes_grant_models import KubernetesGrant, KubernetesGrantScopeKind, KubernetesGrantSpec
from haku.console.kubernetes_grant_service import KubernetesGrantService
from haku.console.mcp_execution import AgentMcpExecutionCaller, McpExecutionContext, require_mcp_execution_context

KUBERNETES_SERVER_ID = "kubernetes"
_EXECUTION_CONTEXT_DEPENDENCY = Depends(require_mcp_execution_context)


class CanIResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason: str | None = None
    source: KubernetesAuthorizationSource
    valid_until: datetime.datetime | None = None


class KubernetesAccessCheck(BaseModel):
    """One hypothetical request for ``can_i``.

    A named namespace and a non-resource path are self-describing. An unnamespaced resource path is
    ambiguous without API discovery, so the caller must state whether it means all namespaces or a
    cluster-scoped resource. The executing proxy performs that discovery itself before forwarding.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    attributes: RequestAttributes
    unnamespaced_resource_kind: KubernetesGrantScopeKind | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> KubernetesAccessCheck:
        required_scope(self.attributes, unnamespaced_resource_kind=self.unnamespaced_resource_kind)
        return self


class KubernetesToolsService:
    def __init__(self, *, grants: KubernetesGrantService, authorization: KubernetesAuthorizationService) -> None:
        self.grants = grants
        self.authorization = authorization

    @staticmethod
    def _caller(context: McpExecutionContext) -> AgentMcpExecutionCaller:
        if not isinstance(context.caller, AgentMcpExecutionCaller):
            raise PermissionError("Kubernetes grant operations require an Agent caller")
        return context.caller

    async def create_grants(
        self, *, context: McpExecutionContext, grants: list[KubernetesGrantSpec], duration_seconds: int
    ) -> tuple[KubernetesGrant, ...]:
        caller = self._caller(context)
        if context.tool_call_id is None:
            raise PermissionError("Kubernetes grant creation requires durable tool-call provenance")
        now = datetime.datetime.now(datetime.UTC)
        return await self.grants.create_grants(
            agent_id=caller.agent_id,
            source_tool_call_id=context.tool_call_id,
            grants=grants,
            expires_at=now + datetime.timedelta(seconds=duration_seconds),
        )

    async def list_grants(self, *, context: McpExecutionContext) -> tuple[KubernetesGrant, ...]:
        return await self.grants.list_grants(agent_id=self._caller(context).agent_id)

    async def get_grant(self, *, context: McpExecutionContext, grant_id: UUID) -> KubernetesGrant:
        return await self.grants.get_grant(agent_id=self._caller(context).agent_id, grant_id=grant_id)

    async def release_grants(
        self, *, context: McpExecutionContext, grant_ids: list[UUID], reason: str = "released"
    ) -> tuple[KubernetesGrant, ...]:
        return await self.grants.release_grants(
            agent_id=self._caller(context).agent_id, grant_ids=grant_ids, reason=reason
        )

    async def can_i(self, *, context: McpExecutionContext, requests: list[KubernetesAccessCheck]) -> list[CanIResult]:
        caller = self._caller(context)
        results: list[CanIResult] = []
        for request in requests:
            attributes = request.attributes
            decision = await self.authorization.authorize_agent(
                agent_id=caller.agent_id,
                access_profile_id=caller.access_profile_id,
                request=AuthorizationRequest(
                    attributes=attributes,
                    required_scope=required_scope(
                        attributes, unnamespaced_resource_kind=request.unnamespaced_resource_kind
                    ),
                    required_rules=[required_rule(attributes)],
                ),
            )
            results.append(_can_i_result(decision))
        return results


def _can_i_result(decision: AuthorizationResponse) -> CanIResult:
    return CanIResult(
        allowed=decision.allowed, reason=decision.reason, source=decision.source, valid_until=decision.valid_until
    )


def build_mcp(service: KubernetesToolsService) -> FastMCP:
    """Build one stable server instance; request identity enters only via hidden dependencies."""

    mcp = FastMCP(
        name=KUBERNETES_SERVER_ID,
        instructions=(
            "Inspect Kubernetes access with can_i, or create/release explicit Agent-owned temporary "
            "RBAC-like grants. One create_grant call may create multiple exact grants with a shared expiry. "
            "One release_grants call may release up to 32 durable grant IDs sequentially. "
            "Agent identity and tool-call provenance are trusted request metadata, "
            "never tool arguments. Kubernetes SAR is checked before temporary grants."
        ),
    )

    @mcp.tool
    async def can_i(
        requests: Annotated[
            list[KubernetesAccessCheck],
            Field(min_length=1, max_length=32, description="Kubernetes requests to authorize in one batch."),
        ],
        context: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[CanIResult]:
        return await service.can_i(context=context, requests=requests)

    @mcp.tool
    async def create_grant(
        grants: Annotated[
            list[KubernetesGrantSpec],
            Field(
                min_length=1,
                max_length=32,
                description="Exact grants to create atomically with one shared start and expiry.",
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
        context: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[KubernetesGrant]:
        return list(await service.create_grants(context=context, grants=grants, duration_seconds=duration_seconds))

    @mcp.tool
    async def list_grants(context: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY) -> list[KubernetesGrant]:
        return list(await service.list_grants(context=context))

    @mcp.tool
    async def get_grant(
        grant_id: Annotated[UUID, Field(description="Grant UUID returned by create_grant.")],
        context: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY,
    ) -> KubernetesGrant:
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
    ) -> list[KubernetesGrant]:
        return list(await service.release_grants(context=context, grant_ids=grant_ids, reason=reason))

    return mcp
