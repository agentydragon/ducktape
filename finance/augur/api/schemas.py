from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Percentage = Annotated[NonNegativeFloat, Field(le=100)]

type Frame = dict[str, list[float | int | bool | str | None]]
"""Rectangular, JSON-safe table payload: one column per key, equal-length lists."""
