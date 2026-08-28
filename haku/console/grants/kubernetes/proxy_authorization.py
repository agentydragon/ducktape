"""HTTP adapter for the Kubernetes API proxy authorization service."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Header, HTTPException, Request

from haku.console.grants.kubernetes.authorization import (
    AuthorizationRequest,
    AuthorizationResponse,
    KubernetesAuthorizationService,
    KubernetesAuthorizationUnavailableError,
    KubernetesBearerRejectedError,
)

router = APIRouter(prefix="/api/internal/kubernetes", tags=["kubernetes-proxy"])


@router.post("/authorize", response_model=AuthorizationResponse, response_model_exclude_none=True)
async def authorize_kubernetes_request(
    request: Request, body: AuthorizationRequest, authorization: Annotated[str | None, Header()] = None
) -> AuthorizationResponse:
    """Authorize one canonicalized Kubernetes request with the standing SAR policy.

    The endpoint is intentionally not operator-session protected: it is the
    machine-to-machine hop from the separate proxy.  Its bearer is resolved
    through the same canonical Agent authority as MCP, and the SAR subject is
    selected only by the Agent's deploy-managed access profile.
    """

    if authorization is None:
        raise HTTPException(status_code=401, detail="Bearer authorization is required")
    service = getattr(request.app.state, "kubernetes_authorization", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Kubernetes authorization is not configured")
    service = cast(KubernetesAuthorizationService, service)
    try:
        return await service.authorize(bearer=authorization, request=body)
    except KubernetesBearerRejectedError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except KubernetesAuthorizationUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
