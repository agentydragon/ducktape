"""Exact-decimal authored money, the currency unit, and signed currency quanta with checked
persisted counts and exact intermediate arithmetic."""

from decimal import Decimal
from fractions import Fraction
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from finance.augur.sim.fixed_point import (
    MONEY_FACTOR_SCALE,
    currency_amount_to_quanta,
    validate_currency_amount,
    validate_currency_quantum,
)

type CurrencyAmount = Annotated[Decimal, BeforeValidator(validate_currency_amount)]
type NonNegativeCurrencyAmount = Annotated[CurrencyAmount, Field(ge=0)]
type PositiveCurrencyAmount = Annotated[CurrencyAmount, Field(gt=0)]


class Currency(BaseModel):
    """A money unit; `World` counts integer quanta and knows no currency, so callers convert here.

    ``quantum`` is deliberately an exact decimal rather than an ISO exponent: it describes
    the smallest monetary amount this unit represents, so a zero-decimal currency or another
    deliberately declared quantum needs no special case.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    quantum: Decimal

    @field_validator("code")
    @classmethod
    def _validate_code(cls, code: str) -> str:
        normalized = code.strip().upper()
        if not normalized:
            raise ValueError("currency code must not be empty")
        return normalized

    @field_validator("quantum", mode="before")
    @classmethod
    def _validate_quantum(cls, quantum: object) -> Decimal:
        return validate_currency_quantum(quantum)

    def quanta(self, amount: Decimal | int) -> int:
        """`amount` as a count of quanta; raises on a float or an amount finer than one quantum."""
        return int(currency_amount_to_quanta(amount, quantum=self.quantum))


USD = Currency(code="USD", quantum=Decimal("0.01"))

MIN_COUNT = -(1 << 63)
MAX_COUNT = (1 << 63) - 1


def checked_count(value: int, operation: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{operation} requires an integer count")
    if not MIN_COUNT <= value <= MAX_COUNT:
        raise OverflowError(f"integer overflow during {operation}")
    return value


def checked_wide(value: int, operation: str) -> int:
    if not -(1 << 127) <= value < 1 << 127:
        raise OverflowError(f"integer overflow during {operation}")
    return value


def round_ratio(numerator: int, denominator: int) -> int:
    """Nearest integer, with exact ties away from zero; never a float conversion."""
    quotient, remainder = divmod(abs(numerator), abs(denominator))
    rounded = quotient + (2 * remainder >= abs(denominator))
    return -rounded if (numerator < 0) != (denominator < 0) else rounded


def mul_div(lhs: int, rhs: int, denominator: int, operation: str) -> int:
    checked_count(lhs, operation)
    checked_count(rhs, operation)
    checked_count(denominator, operation)
    return checked_count(round_ratio(lhs * rhs, denominator), operation)


def mul_div_wide(lhs: int, rhs: int, denominator: int, operation: str) -> int:
    checked_wide(lhs, operation)
    checked_wide(rhs, operation)
    checked_wide(denominator, operation)
    if denominator == 0:
        raise ZeroDivisionError(f"division by zero during {operation}")
    product = checked_wide(lhs * rhs, operation)
    # Contractual wide formulas have a checked rounding remainder as well as product.
    checked_wide(2 * (abs(product) % abs(denominator)), operation)
    return checked_wide(round_ratio(product, denominator), operation)


def scaled(amount: int, factor: Fraction, operation: str) -> int:
    return mul_div(amount, factor.numerator, factor.denominator, operation)


def apportion(basis: int, units: int, units_remaining: int) -> int:
    return mul_div(basis, units, units_remaining, "lot basis apportionment")


def position_value(price: int, units: int, quantity_scale: int) -> int:
    return mul_div(price, units, quantity_scale, "holding value")


def distribution_value(rate: int, units: int, quantity_scale: int) -> int:
    checked_count(rate, "distribution rate")
    checked_count(units, "distribution units")
    checked_count(quantity_scale, "quantity scale")
    return checked_count(
        mul_div_wide(rate, units, quantity_scale * MONEY_FACTOR_SCALE, "distribution value"), "distribution value"
    )


def is_quantity_scale(scale: int) -> bool:
    return 0 < scale <= MAX_COUNT and scale in (10**exponent for exponent in range(19))
