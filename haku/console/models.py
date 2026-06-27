"""Typed models for the Haku console API.

The console is now item-agnostic: it records opaque operator-authored text as
intake notes (trace tier) and surfaces config to the SPA (launch URL + Haku UI URL).
Item rendering lives in ``haku/state_template/ui/`` — Haku's own UI embedded via iframe.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TraceRequest(BaseModel):
    text: str = Field(description="Operator-authored note to append as an intake entry in haku-state")


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
