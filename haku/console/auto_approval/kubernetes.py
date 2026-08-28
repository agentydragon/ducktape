"""Kubernetes-passthrough redundancy: auto-deny when the caller can already reach it directly."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from haku.console.auto_approval.decision import AutoApprovalDecision, AutoDenied
from haku.console.grants.kubernetes.authorization import KubernetesAuthorizationService
from haku.console.grants.kubernetes.kubectl_passthrough_policy import map_kubectl_passthrough_request
from haku.console.grants.principal import RequestPrincipal
from haku.console.tool_call_actor import AgentActor, RuntimeActor

logger = logging.getLogger(__name__)


async def evaluate_passthrough_redundancy(
    actor: RuntimeActor,
    tool_name: str,
    arguments: dict[str, Any],
    kubernetes_authorization: KubernetesAuthorizationService | None,
) -> AutoApprovalDecision | None:
    """``KubernetesPassthroughAutoApprovalPolicy``: never grants standing authority -- it only checks
    whether the caller's own Kubernetes SAR identity already covers this call, and if so auto-
    *denies* it with a message to use that direct path instead of the operator's broader
    passthrough credential. Returns ``None`` when the policy has nothing to say (unmapped tool,
    not covered, evaluation unavailable): such calls fall through to manual review, never to a
    silent approval.
    """
    if (
        not isinstance(actor, AgentActor)
        or actor.access_profile_id is None
        or kubernetes_authorization is None
        or (auth_requests := map_kubectl_passthrough_request(tool_name, arguments)) is None
    ):
        return None
    try:
        decisions = await asyncio.gather(
            *[
                kubernetes_authorization.evaluate(
                    request_principal=RequestPrincipal.from_source(actor), request=auth_req
                )
                for auth_req in auth_requests
            ]
        )
    except Exception:
        logger.warning(
            "Kubernetes authorization evaluation failed during kubectl-passthrough policy check", exc_info=True
        )
        return None
    if not all(decision.allowed for decision in decisions):
        return None
    return AutoDenied(
        reason=(
            "Covered by your direct Kubernetes permissions. Use your direct Haku "
            "Kubernetes proxy or local kubectl instead of kubectl-passthrough-mcp."
        ),
        evaluation="denied: covered by direct agent access",
    )
