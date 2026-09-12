"""Signed currency quanta with checked persisted counts and exact intermediate arithmetic."""

from fractions import Fraction

from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE

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


def trunc_div(numerator: int, denominator: int) -> int:
    """Integer division toward zero, including negative income and basis adjustments."""
    magnitude = abs(numerator) // abs(denominator)
    return -magnitude if (numerator < 0) != (denominator < 0) else magnitude


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
