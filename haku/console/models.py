"""Typed models for the Haku console API.

The console is now just the trusted shell: it surfaces config to the SPA (launch URL +
Haku UI URL) and brokers the capability tier. All product surfaces — items, feedback —
live in ``haku/state_template/ui/`` (Haku's own UI), embedded via a sandboxed iframe.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ConfigResponse(BaseModel):
    # The launch-routine's claude.ai/code page, surfaced as a deep-link to review past
    # runs (there's no runs-listing API). None when the launch capability isn't configured.
    # The privileged launch action stays on the capability tier (see haku.console.capabilities).
    launch_routine_url: str | None = Field(
        default=None, description="Routine page URL for reviewing past runs; None when unconfigured"
    )
    # The Authentik-gated origin of Haku's own UI service (haku-sandbox), embedded as
    # a sandboxed cross-origin iframe. None when unconfigured.
    haku_ui_url: str | None = Field(
        default=None, description="Origin of Haku's own UI service for the iframe embed; None when unconfigured"
    )
