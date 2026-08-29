"""Typed models for the Haku console API.

The console is now just the trusted shell: it surfaces config to the SPA (launch URL +
Haku UI URL) and brokers the capability tier. All product surfaces — items, feedback —
live in haku-state's ``ui/`` (Haku's own UI), embedded via a sandboxed iframe.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from haku.console.harnesses.kind import HarnessKind


class LaunchOption(BaseModel):
    """One deploy-authorized Agent/harness pair the SPA may request explicitly."""

    agent_id: UUID
    agent_display_name: str
    harness_kind: HarnessKind
    harness_display_name: str


class ConfigResponse(BaseModel):
    # The launch-routine's claude.ai/code page, surfaced as a deep-link to review past
    # runs (there's no runs-listing API). None when the launch capability isn't configured.
    # The privileged launch action stays on the capability tier (see haku.console.capabilities).
    launch_routine_url: str | None = Field(
        default=None, description="Routine page URL for reviewing past runs; None when unconfigured"
    )
    # The Authentik-gated origin of Haku's own UI service (haku-sandbox), framed as a
    # sandboxed cross-origin iframe. Always present — it's the console's whole surface.
    haku_ui_url: str = Field(description="Origin of Haku's own UI service for the iframe embed")
    # Empty on harness-disabled replicas. The SPA keeps conversation reads available but disables
    # Web creation until an explicit deploy-authorized Agent/harness pair is present.
    launch_options: list[LaunchOption] = Field(default_factory=list)
