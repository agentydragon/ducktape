"""Exact arithmetic properties, with multiplication-back oracles independent of division."""

from fractions import Fraction

import pytest
import pytest_bazel
from hypothesis import assume, given, strategies as st

from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.money import (
    MAX_COUNT,
    MIN_COUNT,
    apportion,
    checked_count,
    distribution_value,
    is_quantity_scale,
    mul_div,
    mul_div_wide,
    position_value,
    scaled,
    trunc_div,
)

COUNTS = st.integers(MIN_COUNT, MAX_COUNT)
DENOMINATORS = st.one_of(st.integers(1, 64), st.integers(-64, -1), COUNTS.filter(lambda value: value != 0))


def assert_nearest(product: int, denominator: int, quotient: int) -> None:
    scaled_quotient = quotient * denominator
    twice_residue = 2 * abs(product - scaled_quotient)
    assert twice_residue <= abs(denominator)
    if twice_residue == abs(denominator):
        assert abs(scaled_quotient) >= abs(product)


@given(lhs=COUNTS, rhs=COUNTS, denominator=DENOMINATORS)
def test_narrow_mul_div_rounds_half_away_from_zero(lhs: int, rhs: int, denominator: int) -> None:
    product = lhs * rhs
    try:
        quotient = mul_div(lhs, rhs, denominator, "test")
    except OverflowError:
        # Beyond the endpoint's half-quantum neighbourhood, no rounded result fits.
        bound = MAX_COUNT if (product < 0) == (denominator < 0) else -MIN_COUNT
        assert 2 * abs(product) >= (2 * bound + 1) * abs(denominator)
    else:
        assert_nearest(product, denominator, quotient)


@given(half=st.integers(1, 1 << 30), multiple=st.integers(-(1 << 30), 1 << 30), below=st.booleans())
def test_an_exact_tie_rounds_away_from_zero(half: int, multiple: int, below: bool) -> None:
    denominator = 2 * half
    numerator = multiple * denominator + (-half if below else half)
    quotient = mul_div(numerator, 1, denominator, "test")
    assert_nearest(numerator, denominator, quotient)
    assert abs(quotient * denominator) > abs(numerator)


@given(
    lhs=st.integers(MIN_COUNT + 1, MAX_COUNT),
    rhs=st.integers(MIN_COUNT + 1, MAX_COUNT),
    denominator=st.integers(MIN_COUNT + 1, MAX_COUNT).filter(lambda value: value != 0),
)
def test_narrow_mul_div_is_sign_symmetric(lhs: int, rhs: int, denominator: int) -> None:
    assume(2 * abs(lhs * rhs) < (2 * MAX_COUNT + 1) * abs(denominator))
    quotient = mul_div(lhs, rhs, denominator, "test")
    assert mul_div(-lhs, rhs, denominator, "test") == -quotient
    assert mul_div(lhs, -rhs, denominator, "test") == -quotient
    assert mul_div(lhs, rhs, -denominator, "test") == -quotient


@given(lhs=COUNTS, rhs=COUNTS)
def test_a_zero_denominator_is_refused(lhs: int, rhs: int) -> None:
    with pytest.raises(ZeroDivisionError):
        mul_div(lhs, rhs, 0, "test")
    with pytest.raises(ZeroDivisionError):
        mul_div_wide(lhs, rhs, 0, "test")


@given(
    lhs=st.integers(-(1 << 62), 1 << 62),
    rhs=st.integers(-(1 << 62), 1 << 62),
    denominator=st.integers(-(1 << 62), 1 << 62).filter(lambda value: value != 0),
)
def test_wide_mul_div_rounds_half_away_from_zero(lhs: int, rhs: int, denominator: int) -> None:
    assert_nearest(lhs * rhs, denominator, mul_div_wide(lhs, rhs, denominator, "test"))


@given(basis=COUNTS, units=st.integers(1, MAX_COUNT))
def test_apportioning_everything_moves_everything(basis: int, units: int) -> None:
    assert apportion(basis, units, units) == basis


@given(amount=COUNTS, basis_points=st.integers(0, 10_000))
def test_equal_factors_scale_money_identically(amount: int, basis_points: int) -> None:
    assert scaled(amount, Fraction(basis_points, 10_000), "test") == mul_div(
        amount, basis_points * 100_000, MONEY_FACTOR_SCALE, "test"
    )


@given(amount=st.integers(-(10**12), 10**12), parts=st.integers(0, MONEY_FACTOR_SCALE))
def test_a_factor_and_its_complement_split_an_amount(amount: int, parts: int) -> None:
    factor = Fraction(parts, MONEY_FACTOR_SCALE)
    assert abs(scaled(amount, factor, "test") + scaled(amount, 1 - factor, "test") - amount) <= 1


@given(amount=COUNTS, parts=st.integers(0, MONEY_FACTOR_SCALE))
def test_a_quantity_scales_like_money(amount: int, parts: int) -> None:
    # Counts have one arithmetic operation regardless of whether they count money or units.
    factor = Fraction(parts, MONEY_FACTOR_SCALE)
    assert_nearest(amount * parts, MONEY_FACTOR_SCALE, scaled(amount, factor, "test"))


@given(exponent=st.integers(0, 18))
def test_only_powers_of_ten_are_quantity_scales(exponent: int) -> None:
    assert is_quantity_scale(10**exponent)


@given(scale=st.integers(1, 100_000))
def test_a_scale_that_is_not_a_power_of_ten_is_refused(scale: int) -> None:
    assume(str(scale).rstrip("0") != "1")
    assert not is_quantity_scale(scale)


@given(amount=st.integers(-(10**12), 10**12), parts=st.integers(0, MONEY_FACTOR_SCALE), periods=st.integers(1, 360))
def test_a_rate_spread_over_periods_re_totals(amount: int, parts: int, periods: int) -> None:
    factor = Fraction(parts, MONEY_FACTOR_SCALE)
    assert abs(scaled(amount, factor / periods, "test") * periods - scaled(amount, factor, "test")) <= periods


@given(
    basis=st.integers(-(10**12), 10**12),
    units=st.integers(1, 1_000_000),
    cut_candidates=st.sets(st.integers(1, 1_000_000), max_size=8),
)
def test_liquidating_a_lot_consumes_exactly_its_basis(basis: int, units: int, cut_candidates: set[int]) -> None:
    cuts = [*sorted({cut % units for cut in cut_candidates} - {0}), units]
    basis_remaining, units_remaining, sold = basis, units, 0
    for cut in cuts:
        piece = cut - sold
        taken = apportion(basis_remaining, piece, units_remaining)
        basis_remaining -= taken
        units_remaining -= piece
        sold = cut
        assert abs(basis_remaining) <= abs(basis)
        assert basis_remaining * basis >= 0
    assert units_remaining == 0
    assert basis_remaining == 0


@pytest.mark.parametrize(("numerator", "expected"), [(5, 3), (-5, -3), (4, 2), (-4, -2)])
def test_half_up_rounding_is_symmetric(numerator: int, expected: int) -> None:
    assert mul_div(numerator, 1, 2, "test") == expected
    assert mul_div_wide(numerator, 1, 2, "test") == expected


@pytest.mark.parametrize("quanta_per_unit", [1, 20, 4237])
def test_a_rate_of_whole_quanta_per_unit_agrees_with_a_price(quanta_per_unit: int) -> None:
    assert distribution_value(quanta_per_unit * MONEY_FACTOR_SCALE, 10_000 * 1_000_000, 1_000_000) == position_value(
        quanta_per_unit, 10_000 * 1_000_000, 1_000_000
    )


def test_a_rate_below_one_quantum_per_unit_still_comes_to_money() -> None:
    assert distribution_value(40_000_000, 10_000 * 1_000_000, 1_000_000) == 400
    assert position_value(0, 10_000 * 1_000_000, 1_000_000) == 0


@pytest.mark.parametrize(("rate", "expected"), [(100_000, 1), (50_000, 1), (49_999, 0), (-50_000, -1)])
def test_the_product_is_formed_before_either_scale_divides_out(rate: int, expected: int) -> None:
    assert distribution_value(rate, 10_000 * 1_000_000, 1_000_000) == expected


def test_a_gwei_scaled_position_does_not_overflow_the_denominator() -> None:
    assert distribution_value(7 * MONEY_FACTOR_SCALE, 3 * 1_000_000_000, 1_000_000_000) == 21


@pytest.mark.parametrize(("numerator", "denominator", "expected"), [(-5, 2, -2), (5, -2, -2), (-5, -2, 2)])
def test_truncating_division_does_not_floor_negative_values(numerator: int, denominator: int, expected: int) -> None:
    assert trunc_div(numerator, denominator) == expected


def test_explicit_count_and_intermediate_overflows() -> None:
    assert checked_count(MIN_COUNT, "test") == MIN_COUNT
    assert checked_count(MAX_COUNT, "test") == MAX_COUNT
    for outside in (MIN_COUNT - 1, MAX_COUNT + 1):
        with pytest.raises(OverflowError):
            checked_count(outside, "test")
    with pytest.raises(OverflowError):
        mul_div(MAX_COUNT, 2, 1, "test")
    with pytest.raises(OverflowError):
        mul_div_wide(1 << 126, 2, 1, "test")


@pytest.mark.parametrize("inexact", [True, 1.0])
def test_inexact_counts_are_refused(inexact: int) -> None:
    with pytest.raises(TypeError):
        checked_count(inexact, "test")


if __name__ == "__main__":
    pytest_bazel.main()
