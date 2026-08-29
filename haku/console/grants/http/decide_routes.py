"""HTTP adapter for the egress proxy's per-request decision call."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Header, HTTPException, Request

from haku.console.grants.http.decide_service import HttpDecideService, HttpDecideUnavailableError
from haku.egress.decision import DecideRequest, HttpAuthorizationAllowed, HttpAuthorizationDenied

router = APIRouter(prefix="/api/internal/http", tags=["http-egress"])


@router.post(
    "/decide", response_model=HttpAuthorizationAllowed | HttpAuthorizationDenied, response_model_exclude_none=True
)
async def decide_http_request(
    request: Request, body: DecideRequest, authorization: Annotated[str | None, Header()] = None
) -> HttpAuthorizationAllowed | HttpAuthorizationDenied:
    """Decide one admission for the colocated egress proxy: verdict plus substitutions.

    The endpoint is intentionally not operator-session protected: it is the machine-to-machine
    hop from the colocated proxy, bound on localhost and never sandbox-routable (#4670 § oracle
    constraint). The ``Authorization`` bearer is the shared-fence credential; it authenticates
    the fence but does not identify an Agent. The required sandbox bridge bearer in the body
    selects the live session Agent inside the service. Any non-2xx makes the proxy refuse the
    admission, so every error response here is fail-closed by construction.
    """
    if authorization is None:
        raise HTTPException(status_code=401, detail="Bearer authorization is required")
    service = getattr(request.app.state, "http_decide", None)
    if service is None:
        raise HTTPException(status_code=503, detail="HTTP egress decision is not configured")
    service = cast(HttpDecideService, service)
    if not service.authenticate_proxy(authorization):
        raise HTTPException(status_code=401, detail="fence credential was rejected")
    try:
        return await service.decide(body)
    except HttpDecideUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
