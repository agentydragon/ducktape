"""Admission metadata, captured before forwarding; never upstream completion evidence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Outcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Decision(BaseModel):
    """One admission shown in the browser; names only, never values.

    Paths are deliberately never captured: arbitrary path segments can contain secrets.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    at: datetime
    method: str
    host: str
    port: int
    outcome: Outcome
    reason: str | None = Field(default=None, description="The proxy's machine-readable refusal, on a deny.")
    binding: str | None = None
    policy: str | None = None
    rule: int | None = None
    substituted: bool = Field(default=False, description="Whether a credential replaced a placeholder.")
    address: str | None = Field(default=None, description="The address the host was pinned to, when admitted.")
