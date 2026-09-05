from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, NonNegativeFloat

from finance.augur.sim.fixed_point import validate_currency_amount


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
type CurrencyAmount = Annotated[Decimal, BeforeValidator(validate_currency_amount)]
type NonNegativeCurrencyAmount = Annotated[CurrencyAmount, Field(ge=0)]
type PositiveCurrencyAmount = Annotated[CurrencyAmount, Field(gt=0)]

type Frame = dict[str, list[float | int | bool | str | None]]
"""Rectangular, JSON-safe table payload: one column per key, equal-length lists."""
