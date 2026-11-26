"""Shared APB packing policy. An omitted entry target uses the service default."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EventBlockProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_bytes: int = Field(default=65536, ge=1, le=8 << 20)
    target_entries: int | None = Field(default=None, ge=1, le=256, exclude_if=lambda v: v is None)
    codec: Literal["zstd"] = "zstd"
    level: int = Field(default=3, ge=1, le=4)
