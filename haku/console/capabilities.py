"""Capability tier: high-privilege actions the console performs that Haku cannot.

This is the console's one privileged non-MCP surface: **CSRF-gated**, **audited** to this
trusted namespace's logs (which Haku has no RBAC to read), and a small **PR-gated** allowlist.
Today the one capability is `launch-routine`: firing the Haku "claude-code-web routine" with
the bearer from the `haku-routine-launch-token` secret. The fire itself lives in
`haku.console.tools.routine.RoutineLauncher` (shared with the `haku_routine` in-process MCP
server); the bearer never leaves this process. See `haku/docs/security.md` → enforcement #11.

CLEANUP(added 2026-07-11): Retire this whole launch-routine capability path (the endpoint +
`LaunchRoutineRequest` + the `requestLaunch` bridge verb + the shell launch confirm) once
haku-ui submits `launch_routine` through its backend to the standard approval queue (the
`haku_routine` MCP server, `tools/routine.py`) and the `requestLaunch` verb is dropped. The
`GET /api/capabilities/csrf` endpoint stays regardless — it is shared with the MCP approval
and operator-auth flows.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi_csrf_protect import CsrfProtect
from itsdangerous import BadData, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from haku.console.config import LaunchRoutineConfig
from haku.console.deps import SettingsDep
from haku.console.tools.routine import LaunchRoutineResult, RoutineLauncher

Csrf = Annotated[CsrfProtect, Depends()]

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])

# Match fastapi-csrf-protect 1.0.7's cookie serializer so this endpoint can return the
# raw token paired with an existing HttpOnly cookie instead of invalidating other tabs.
_CSRF_COOKIE_KEY = "fastapi-csrf-token"
_CSRF_SERIALIZER_SALT = "fastapi-csrf-token"
_CSRF_MAX_AGE_SECONDS = 3600


class CsrfTokenResponse(BaseModel):
    csrf_token: str


class LaunchRoutineRequest(BaseModel):
    text: str | None = Field(
        default=None, description="Optional per-fire routine text; omitted or blank uses the routine's saved default"
    )


def _launch_config(settings: SettingsDep) -> LaunchRoutineConfig:
    if settings.launch_routine is None:
        raise HTTPException(status_code=503, detail="launch-routine capability is not configured")
    return settings.launch_routine


# The SPA fetches a CSRF token here (and gets the signed double-submit cookie), then echoes the
# token in the X-CSRF-Token header on the launch POST and on MCP approval/operator-auth calls.
# Gating those privileged mutations this way stops a cross-site request from riding the
# operator's Authentik session cookie.
@router.get("/csrf")
async def csrf_token(request: Request, response: Response, csrf_protect: Csrf) -> CsrfTokenResponse:
    # Set the signed cookie on the injected Response (which FastAPI returns) so the
    # body can stay a typed model — the frontend generates its client off this schema.
    signed = request.cookies.get(_CSRF_COOKIE_KEY)
    if signed is not None:
        try:
            token = URLSafeTimedSerializer(cast(str, request.app.state.csrf_secret), salt=_CSRF_SERIALIZER_SALT).loads(
                signed, max_age=_CSRF_MAX_AGE_SECONDS
            )
        except BadData:
            pass
        else:
            if isinstance(token, str):
                return CsrfTokenResponse(csrf_token=token)
    token, signed = csrf_protect.generate_csrf_tokens()
    csrf_protect.set_csrf_cookie(signed, response)
    return CsrfTokenResponse(csrf_token=token)


@router.post("/launch-routine")
async def launch_routine(
    request: Request,
    csrf_protect: Csrf,
    config: Annotated[LaunchRoutineConfig, Depends(_launch_config)],
    body: LaunchRoutineRequest | None = None,
) -> LaunchRoutineResult:
    """Fire the Haku claude-code-web routine. CSRF-gated; the bearer stays server-side.

    Superseded by the `haku_routine` MCP tool `launch_routine` (approval-queue gated); kept
    while haku-ui still fires via the `requestLaunch` bridge verb (see the module tombstone)."""
    await csrf_protect.validate_csrf(request)
    try:
        return await RoutineLauncher(config).launch(body.text if body else None)
    except RuntimeError as exc:
        # RoutineLauncher raises on a non-2xx upstream; surface the reason, not a bare 502.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
