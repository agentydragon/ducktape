"""Sandbox identity shared with the runner and its test helpers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SandboxIdentity(BaseModel):
    """The Sandbox as the Notification Service addresses it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    namespace: str = Field(min_length=1)
    name: str = Field(min_length=1)
    uid: str = Field(min_length=1)
