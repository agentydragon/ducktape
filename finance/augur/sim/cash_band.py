"""Optional cash-budget proposals, without choosing or executing any trades.

Crossing a cash-band bound proposes moving to the far edge: raise to the ceiling
below the floor, or invest down to the floor above the ceiling. Inside the inclusive
band, hold. A policy supplies projected cash after its planned outflows and may
compose or ignore the proposal. It does not promise available funding or preview tax.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Raise:
    amount: int


@dataclass(frozen=True)
class Invest:
    amount: int


@dataclass(frozen=True)
class Hold:
    pass


def cash_band(*, projected_cash: int, floor: int, ceiling: int) -> Raise | Invest | Hold:
    """Use exact currency-quanta counts; negative projected cash is a shortfall.

    Inputs and the proposed amount must fit signed 64-bit money. Floats and booleans
    are not monetary counts. Bounds must satisfy ``0 <= floor <= ceiling``.
    """

    values = (projected_cash, floor, ceiling)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise TypeError("cash band requires integer currency-quanta counts")
    if any(not -(1 << 63) <= value < 1 << 63 for value in values):
        raise OverflowError("cash band input does not fit signed 64-bit money")
    validate_band_bounds(floor=floor, ceiling=ceiling)
    if floor <= projected_cash <= ceiling:
        return Hold()
    amount = ceiling - projected_cash if projected_cash < floor else projected_cash - floor
    if amount >= 1 << 63:
        raise OverflowError("cash band proposal does not fit signed 64-bit money")
    return Raise(amount=amount) if projected_cash < floor else Invest(amount=amount)


def validate_band_bounds(*, floor: Decimal | int, ceiling: Decimal | int) -> None:
    """Validate authored decimal bounds before sampling, or resolved integer bounds."""

    if floor < 0:
        raise ValueError(f"cash band floor must not be negative; got {floor=}")
    if floor > ceiling:
        raise ValueError(f"cash band floor must not exceed its ceiling; got {floor=}, {ceiling=}")
