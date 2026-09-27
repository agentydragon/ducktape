from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, NonNegativeFloat


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Percentage = Annotated[NonNegativeFloat, Field(le=100)]


def _whole_basis_points(value: float) -> float:
    """A percentage no finer than a basis point.

    A rate the simulator carries as an integer count of basis points has nowhere to put a
    finer figure, and rounding one silently answers a question the caller did not ask.
    """

    hundredths = Decimal(str(value)) * 100
    if hundredths != hundredths.to_integral_value():
        raise ValueError(f"{value} is finer than a basis point")
    return value


BasisPointPercentage = Annotated[Percentage, AfterValidator(_whole_basis_points)]

type Frame = dict[str, list[float | int | bool | str | None]]
"""Rectangular, JSON-safe table payload: one column per key, equal-length lists."""
