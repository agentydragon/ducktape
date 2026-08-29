"""Kubernetes SAR access inspection (`can_i`) for the shared `grants` server.

`can_i` is a kubernetes-specific check, not a grant verb, but it rides the same in-process `grants`
server (#4918) as the `kubernetes_can_i` tool rather than a separate server. This module owns its
request/result vocabulary and the service that answers it; `haku.console.tools.grants` composes it
onto the server.
"""

from __future__ import annotations

import asyncio

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from haku.console.grants.kubernetes.authorization import (
    AuthorizationRequest,
    RequestAttributes,
    required_rule,
    required_scope,
)
from haku.console.grants.kubernetes.authorization_service import KubernetesAuthorizationService
from haku.console.grants.kubernetes.models import GrantScopeKind
from haku.console.mcp.execution import McpExecutionContext
from haku.grants.authorization import AuthorizationDecision

# The batch bound the `kubernetes_can_i` tool advertises.
CAN_I_BATCH_LIMIT = 32


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

    async def can_i(
        self, *, context: McpExecutionContext, requests: list[KubernetesAccessCheck]
    ) -> list[AuthorizationDecision]:
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
        return await asyncio.gather(
            *(
                self.authorization.evaluate(request_principal=context.request_principal, request=request)
                for request in authorization_requests
            )
        )


CAN_I_INSTRUCTIONS = (
    "kubernetes_can_i: check whether the calling Agent may perform one or more hypothetical Kubernetes requests, "
    "given its standing SAR identity plus any active kubernetes-domain grants. Agent identity is trusted request "
    "metadata, never a tool argument."
)
