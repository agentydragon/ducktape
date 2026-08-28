"""Kubernetes SAR access inspection (`can_i`) for the shared `grants` server.

`can_i` is a kubernetes-specific check, not a grant verb, but it rides the same in-process `grants`
server (#4918) as the `kubernetes_can_i` tool rather than a separate server. This module owns its
request/result vocabulary and the service that answers it; `haku.console.tools.grants` composes it
onto the server.
"""

from __future__ import annotations

import asyncio
import datetime

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from haku.console.grants.kubernetes.authorization import (
    AuthorizationRequest,
    AuthorizationResponse,
    KubernetesAuthorizationService,
    KubernetesAuthorizationSource,
    RequestAttributes,
    required_rule,
    required_scope,
)
from haku.console.grants.kubernetes.models import GrantScopeKind
from haku.console.mcp_execution import McpExecutionContext

# The batch bound the `kubernetes_can_i` tool advertises.
CAN_I_BATCH_LIMIT = 32


class CanIResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason: str | None = None
    source: KubernetesAuthorizationSource
    valid_until: datetime.datetime | None = None


class KubernetesAccessCheck(BaseModel):
    """One hypothetical request for ``kubernetes_can_i``.

    A named namespace, a non-resource path, and a built-in cluster-scoped kind are self-describing.
    Any other unnamespaced resource request is ambiguous without API discovery, so the caller must
    state whether it means all namespaces or a cluster-scoped resource. The executing proxy
    performs that discovery itself before forwarding.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    attributes: RequestAttributes = Field(
        description="The hypothetical request, in Kubernetes' canonical SubjectAccessReview attributes."
    )
    unnamespaced_resource_kind: GrantScopeKind | None = Field(
        default=None,
        description=(
            "How to read an empty namespace on a resource request: 'cluster' for a cluster-scoped "
            "resource, 'all_namespaces' for a namespaced resource across all namespaces. Built-in "
            "cluster-scoped kinds (namespaces, nodes, persistentvolumes, clusterroles, ...) are "
            "inferred as 'cluster'; any other kind, e.g. a CRD, must declare one."
        ),
    )


class KubernetesToolsService:
    """Answer the kubernetes SAR access check behind the `grants` server's `kubernetes_can_i` tool."""

    def __init__(self, *, authorization: KubernetesAuthorizationService) -> None:
        self.authorization = authorization

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


CAN_I_INSTRUCTIONS = (
    "kubernetes_can_i: check whether the calling Agent may perform one or more hypothetical Kubernetes requests, "
    "given its standing SAR identity plus any active kubernetes-domain grants. Agent identity is trusted request "
    "metadata, never a tool argument."
)
