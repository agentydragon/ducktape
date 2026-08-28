"""Credential-free in-process MCP tools for owned, principal-scoped Kubernetes access."""

from __future__ import annotations

import asyncio
import datetime
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from haku.console.grants.envelope import GRANT_SET_LIMIT
from haku.console.grants.kubernetes.authorization import (
    AuthorizationRequest,
    AuthorizationResponse,
    KubernetesAuthorizationService,
    KubernetesAuthorizationSource,
    RequestAttributes,
    required_rule,
    required_scope,
)
from haku.console.grants.kubernetes.models import KubernetesGrant, KubernetesGrantScopeKind, KubernetesGrantSpec
from haku.console.grants.kubernetes.service import KubernetesGrantService
from haku.console.grants.principal import GrantPrincipalKind, grant_principal_for
from haku.console.mcp_execution import EXECUTION_CONTEXT_DEPENDENCY, McpExecutionContext

KUBERNETES_SERVER_ID = "kubernetes"


class CanIResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason: str | None = None
    source: KubernetesAuthorizationSource
    valid_until: datetime.datetime | None = None


class KubernetesAccessCheck(BaseModel):
    """One hypothetical request for ``can_i``.

    A named namespace, a non-resource path, and a built-in cluster-scoped kind are self-describing.
    Any other unnamespaced resource request is ambiguous without API discovery, so the caller must
    state whether it means all namespaces or a cluster-scoped resource. The executing proxy
    performs that discovery itself before forwarding.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    attributes: RequestAttributes = Field(
        description="The hypothetical request, in Kubernetes' canonical SubjectAccessReview attributes."
    )
    unnamespaced_resource_kind: KubernetesGrantScopeKind | None = Field(
        default=None,
        description=(
            "How to read an empty namespace on a resource request: 'cluster' for a cluster-scoped "
            "resource, 'all_namespaces' for a namespaced resource across all namespaces. Built-in "
            "cluster-scoped kinds (namespaces, nodes, persistentvolumes, clusterroles, ...) are "
            "inferred as 'cluster'; any other kind, e.g. a CRD, must declare one."
        ),
    )


class KubernetesToolsService:
    def __init__(self, *, grants: KubernetesGrantService, authorization: KubernetesAuthorizationService) -> None:
        self.grants = grants
        self.authorization = authorization

    async def create_grants(
        self,
        *,
        context: McpExecutionContext,
        grants: list[KubernetesGrantSpec],
        duration_seconds: int,
        applies_to: GrantPrincipalKind = GrantPrincipalKind.AGENT,
    ) -> tuple[KubernetesGrant, ...]:
        principal = context.request_principal
        if context.tool_call_id is None:
            raise PermissionError("Kubernetes grant creation requires durable tool-call provenance")
        now = datetime.datetime.now(datetime.UTC)
        return await self.grants.create_grants(
            owner_agent_id=principal.agent_id,
            grant_principal=grant_principal_for(principal, applies_to),
            source_tool_call_id=context.tool_call_id,
            grants=grants,
            expires_at=now + datetime.timedelta(seconds=duration_seconds),
        )

    async def list_grants(self, *, context: McpExecutionContext) -> tuple[KubernetesGrant, ...]:
        return await self.grants.list_applicable_grants(request_principal=context.request_principal)

    async def get_grant(self, *, context: McpExecutionContext, grant_id: UUID) -> KubernetesGrant:
        return await self.grants.get_applicable_grant(request_principal=context.request_principal, grant_id=grant_id)

    async def release_grants(
        self, *, context: McpExecutionContext, grant_ids: list[UUID], reason: str = "released"
    ) -> tuple[KubernetesGrant, ...]:
        return await self.grants.release_applicable_grants(
            request_principal=context.request_principal, grant_ids=grant_ids, reason=reason
        )

    async def can_i(self, *, context: McpExecutionContext, requests: list[KubernetesAccessCheck]) -> list[CanIResult]:
        authorization_requests = []
        for index, request in enumerate(requests):
            try:
                scope = required_scope(
                    request.attributes, unnamespaced_resource_kind=request.unnamespaced_resource_kind
                )
            except ValueError as error:
                # FastMCP surfaces a pydantic argument-validation failure as the whole multi-line
                # trace, so the scope contract is checked here and raised as a one-line ToolError.
                raise ToolError(f"requests[{index}]: {error}") from error
            authorization_requests.append(
                AuthorizationRequest(
                    attributes=request.attributes,
                    required_scope=scope,
                    required_rules=[required_rule(request.attributes)],
                )
            )
        decisions = await asyncio.gather(
            *(
                self.authorization.authorize_agent(request_principal=context.request_principal, request=request)
                for request in authorization_requests
            )
        )
        return [_can_i_result(decision) for decision in decisions]


def _can_i_result(decision: AuthorizationResponse) -> CanIResult:
    return CanIResult(
        allowed=decision.allowed, reason=decision.reason, source=decision.source, valid_until=decision.valid_until
    )


def build_mcp(service: KubernetesToolsService) -> FastMCP:
    """Build one stable server instance; request identity enters only via hidden dependencies."""

    mcp = FastMCP(
        name=KUBERNETES_SERVER_ID,
        instructions=(
            "Inspect Kubernetes access with can_i, or create/release explicit Agent- or session-scoped temporary "
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
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[CanIResult]:
        return await service.can_i(context=context, requests=requests)

    @mcp.tool
    async def create_grant(
        grants: Annotated[
            list[KubernetesGrantSpec],
            Field(
                min_length=1,
                max_length=GRANT_SET_LIMIT,
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
    ) -> list[KubernetesGrant]:
        return list(
            await service.create_grants(
                context=context, grants=grants, duration_seconds=duration_seconds, applies_to=applies_to
            )
        )

    @mcp.tool
    async def list_grants(context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY) -> list[KubernetesGrant]:
        return list(await service.list_grants(context=context))

    @mcp.tool
    async def get_grant(
        grant_id: Annotated[UUID, Field(description="Grant UUID returned by create_grant.")],
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> KubernetesGrant:
        return await service.get_grant(context=context, grant_id=grant_id)

    @mcp.tool
    async def release_grants(
        grant_ids: Annotated[
            list[UUID],
            Field(
                min_length=1,
                max_length=GRANT_SET_LIMIT,
                description="Grant UUIDs returned by create_grant; released sequentially in the supplied order.",
            ),
        ],
        reason: Annotated[str, Field(min_length=1, max_length=500)] = "released",
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[KubernetesGrant]:
        return list(await service.release_grants(context=context, grant_ids=grant_ids, reason=reason))

    return mcp
