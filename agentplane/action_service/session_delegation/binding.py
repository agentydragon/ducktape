"""Reviewed configuration for same-principal environment and Session Actions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SessionDelegationExecutorBinding(BaseModel):
    """An executor that forwards the authenticated caller to the owning Session APIs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["session_delegation"] = "session_delegation"
    description: str = Field(min_length=1, max_length=2000, description="How this executor preserves caller authority.")
