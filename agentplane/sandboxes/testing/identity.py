"""Exact Sandbox identity shared by integration test helpers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SandboxIdentity(BaseModel):
    """The Sandbox namespace, name and immutable UID used by test fixtures."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    namespace: str = Field(min_length=1)
    name: str = Field(min_length=1)
    uid: str = Field(min_length=1)
