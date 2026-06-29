"""Capability tier: high-privilege actions the console performs that Haku cannot.

This is the console's one privileged surface: these endpoints use console-only
secrets and act on the world, so they are **CSRF-gated**, **audited** to this
trusted namespace's logs (which Haku has no RBAC to read), and the capability set
is a small, **PR-gated** allowlist. Today the one capability is `launch-routine`:
firing the Haku "claude-code-web routine" via its public Anthropic fire URL with
the bearer from the `haku-routine-launch-token` secret. The bearer never leaves
this process. See `haku/PLAN.md` → _The agent-authored console_.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi_csrf_protect import CsrfProtect
from pydantic import BaseModel, Field, field_validator

from haku.console.config import LaunchRoutineConfig
from haku.console.deps import SettingsDep

logger = logging.getLogger(__name__)

# Required on every Anthropic API request; without it the fire endpoint 400s
# ("anthropic-version: header is required").
ANTHROPIC_VERSION = "2023-06-01"

Csrf = Annotated[CsrfProtect, Depends()]

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])


class CsrfTokenResponse(BaseModel):
    csrf_token: str


class LaunchRoutineResult(BaseModel):
    session_url: str = Field(description="claude.ai/code URL of the launched Haku session")


class LaunchRoutineRequest(BaseModel):
    text: str | None = Field(
        default=None, description="Optional per-fire routine text; omitted or blank uses the routine's saved default"
    )

    @field_validator("text")
    @classmethod
    def blank_text_is_absent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


def _upstream_detail(resp: httpx.Response) -> str:
    """Best-effort human-readable reason from an upstream error response."""
    try:
        return str(resp.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return resp.text[:300]


def _launch_config(settings: SettingsDep) -> LaunchRoutineConfig:
    if settings.launch_routine is None:
        raise HTTPException(status_code=503, detail="launch-routine capability is not configured")
    return settings.launch_routine


# The SPA fetches a CSRF token here (and gets the signed double-submit cookie), then
# echoes the token in the X-CSRF-Token header on capability POSTs. Gating the
# privileged tier this way stops a cross-site request from riding the operator's
# Authentik session cookie to fire a capability.
@router.get("/csrf")
async def csrf_token(response: Response, csrf_protect: Csrf) -> CsrfTokenResponse:
    # Set the signed cookie on the injected Response (which FastAPI returns) so the
    # body can stay a typed model — the frontend generates its client off this schema.
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
    """Fire the Haku claude-code-web routine. CSRF-gated; the bearer stays server-side."""
    await csrf_protect.validate_csrf(request)
    payload = {"text": body.text} if body and body.text is not None else {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            config.fire_url,
            headers={
                "Authorization": f"Bearer {config.token.get_secret_value()}",
                "anthropic-version": ANTHROPIC_VERSION,
            },
            json=payload,
        )
    # Audit to stdout in the haku-console namespace (Haku can't read these logs).
    logger.info("capability launch-routine fired: upstream status %s", resp.status_code)
    if not resp.is_success:
        # Surface the upstream reason so the frontend can show it, not a bare 502.
        raise HTTPException(
            status_code=502, detail=f"routine fire failed ({resp.status_code}): {_upstream_detail(resp)}"
        )
    return LaunchRoutineResult(session_url=resp.json()["claude_code_session_url"])
