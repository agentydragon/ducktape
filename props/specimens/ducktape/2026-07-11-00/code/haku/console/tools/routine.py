"""haku-console's in-process `haku_routine` MCP server.

Fires the Haku claude-code-web routine behind haku-console's operator-approval queue:
`launch_routine` is an ordinary approval-gated MCP tool, so a launch flows through the same
submit → approve → execute pipeline as every other console tool call (`mcp_approval.py`),
not a bespoke capability path. The routine (trigger) id + fire bearer come from the mounted
`haku-routine-launch-token` secret via `LaunchRoutineConfig`; the bearer stays in this
process (the `haku-console` namespace Haku can't read). See `haku/docs/security.md`.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from fastmcp import FastMCP
from pydantic import BaseModel, Field

from haku.console.config import LaunchRoutineConfig

logger = logging.getLogger(__name__)

HAKU_ROUTINE_SERVER_ID = "haku_routine"

# Required on every Anthropic API request; without it the fire endpoint 400s
# ("anthropic-version: header is required").
ANTHROPIC_VERSION = "2023-06-01"


class LaunchRoutineResult(BaseModel):
    session_url: str = Field(description="claude.ai/code URL of the launched Haku session.")


def _upstream_detail(resp: httpx.Response) -> str:
    """Best-effort human-readable reason from an upstream error response."""
    try:
        return str(resp.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return resp.text[:300]


class RoutineLauncher:
    """Fires the Haku claude-code-web routine via its Anthropic fire URL with the server-side
    bearer. The bearer never leaves this process."""

    def __init__(self, config: LaunchRoutineConfig) -> None:
        self._config = config

    async def launch(self, text: str | None) -> LaunchRoutineResult:
        # Blank/whitespace text means "use the routine's saved default" — same as omitting it.
        normalized = text.strip() if text else None
        payload = {"text": normalized} if normalized else {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self._config.fire_url,
                headers={
                    "Authorization": f"Bearer {self._config.token.get_secret_value()}",
                    "anthropic-version": ANTHROPIC_VERSION,
                },
                json=payload,
            )
        # Audit to stdout in the haku-console namespace (Haku can't read these logs).
        logger.info("launch_routine fired: upstream status %s", resp.status_code)
        if not resp.is_success:
            raise RuntimeError(f"routine fire failed ({resp.status_code}): {_upstream_detail(resp)}")
        return LaunchRoutineResult(session_url=resp.json()["claude_code_session_url"])


def build_mcp(launcher: RoutineLauncher) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=HAKU_ROUTINE_SERVER_ID,
        instructions="Launch the Haku claude-code-web routine (start a new Haku run). Every call is gated by "
        "haku-console's operator-approval queue — there is no autonomous path.",
    )

    @mcp.tool
    async def launch_routine(
        text: Annotated[
            str | None,
            Field(
                default=None,
                description="Optional per-run routine instructions; omit or leave blank to use the routine's "
                "saved default.",
            ),
        ] = None,
    ) -> LaunchRoutineResult:
        """Start a new Haku claude-code-web session, optionally with per-run instructions."""
        return await launcher.launch(text)

    return mcp
