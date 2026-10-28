from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    contains: str | None = None


class SearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    cursor: str | None = None
    required_note: str | None
    filters: list[SearchFilter]
    labels: dict[str, str]


PolicyMode = Literal["automatic", "manual", "review"]
