"""Server-side client for the Console's audited launch-routine capability."""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, Field

from haku.console.config import LaunchRoutineConfig

logger = logging.getLogger(__name__)

# Required on every Anthropic API request; without it the fire endpoint 400s
# ("anthropic-version: header is required").
ANTHROPIC_VERSION = "2023-06-01"


class LaunchRoutineResult(BaseModel):
    session_url: str = Field(description="claude.ai/code URL of the launched Haku session.")


class LaunchRoutineArguments(BaseModel):
    """Stored request shape retained for rendering historical approval rows."""

    text: str | None = Field(
        default=None,
        description="Optional per-run routine instructions; omit or leave blank to use the routine's saved default.",
    )


def _upstream_detail(resp: httpx.Response) -> str:
    """Best-effort human-readable reason from an upstream error response."""
    try:
        return str(resp.json()["error"]["message"])
    except ValueError, KeyError, TypeError:
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
