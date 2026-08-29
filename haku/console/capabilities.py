"""Capability tier: high-privilege actions the console performs that Haku cannot.

This is the console's one privileged non-MCP surface: **same-origin gated**, **audited** to this
trusted namespace's logs (which Haku has no RBAC to read), and a small **PR-gated** allowlist.
Today the one capability is `launch-routine`: firing the Haku "claude-code-web routine" with
the bearer from the `haku-routine-launch-token` secret. The fire itself lives in
`haku.console.tools.routine.RoutineLauncher` (shared with the `haku_routine` in-process MCP
server); the bearer never leaves this process. See `haku/docs/security.md` → enforcement inventory,
"Console privileged-action tier".

CLEANUP(added 2026-07-11): Retire this whole launch-routine capability path (the endpoint +
`LaunchRoutineRequest` + the `requestLaunch` bridge verb + the shell launch confirm) once
haku-ui submits `launch_routine` through its backend to the standard approval queue (the
`haku_routine` MCP server, `tools/routine.py`) and the `requestLaunch` verb is dropped.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from haku.console.config import LaunchRoutineConfig
from haku.console.deps import SettingsDep
from haku.console.tools.routine import LaunchRoutineResult, RoutineLauncher

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])


class LaunchRoutineRequest(BaseModel):
    text: str | None = Field(
        default=None, description="Optional per-fire routine text; omitted or blank uses the routine's saved default"
    )


def _launch_config(settings: SettingsDep) -> LaunchRoutineConfig:
    if settings.launch_routine is None:
        raise HTTPException(status_code=503, detail="launch-routine capability is not configured")
    return settings.launch_routine


@router.post("/launch-routine")
async def launch_routine(
    config: Annotated[LaunchRoutineConfig, Depends(_launch_config)], body: LaunchRoutineRequest | None = None
) -> LaunchRoutineResult:
    """Fire the Haku claude-code-web routine. Same-origin gated; the bearer stays server-side.

    Superseded by the `haku_routine` MCP tool `launch_routine` (approval-queue gated); kept
    while haku-ui still fires via the `requestLaunch` bridge verb (see the module tombstone)."""
    try:
        return await RoutineLauncher(config).launch(body.text if body else None)
    except RuntimeError as exc:
        # RoutineLauncher raises on a non-2xx upstream; surface the reason, not a bare 502.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
