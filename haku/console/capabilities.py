"""Capability tier: high-privilege actions the console performs that Haku cannot.

This is the console's one privileged capability surface: **same-origin gated**, **audited** to this
trusted namespace's logs (which Haku has no RBAC to read), and a small **PR-gated** allowlist.
Today the one capability is `launch-routine`: firing the Haku "claude-code-web routine" with
the bearer from the `haku-routine-launch-token` secret. The fire itself lives in
`haku.console.tools.routine.RoutineLauncher`; the bearer never leaves this process. See
`haku/docs/security.md` → enforcement inventory,
"Console privileged-action tier".

The `haku_routine` MCP wrapper was removed with the retired Console protocol endpoint. Keep this
capability route until haku-ui can launch routines through a replacement path; then retire the
endpoint, request model, Agent UI bridge action, and shell confirmation together.
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

    Kept while haku-ui still fires via the `requestLaunch` Agent UI bridge action."""
    try:
        return await RoutineLauncher(config).launch(body.text if body else None)
    except RuntimeError as exc:
        # RoutineLauncher raises on a non-2xx upstream; surface the reason, not a bare 502.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
