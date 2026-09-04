import pytest
import pytest_bazel

from finance.augur.rust.differential.rounding_boundary import half_way_operand


@pytest.mark.parametrize(
    ("multiplier", "denominator", "near"),
    [
        # A half-unit sale against the millionths quantity scale, which is the site the
        # generator aims at most.
        (1_500_000, 1_000_000, 40_000),
        (500_000, 1_000_000, 3),
        # The quarterly estimated tax, `prior_year_tax * 1 / 4`.
        (1, 4, 1_000_000),
        # A monthly mortgage interest accrual, `principal * annual_rate_ppb / (12 * 1e9)`.
        (60_000_000, 12_000_000_000, 40_000_000),
        (7, 1_000_000_000, 1),
    ],
)
def test_solution_puts_the_quotient_on_the_half(multiplier: int, denominator: int, near: int) -> None:
    solution = half_way_operand(multiplier, denominator, near=near)
    assert solution is not None
    product = solution * multiplier
    assert product % denominator == denominator // 2
    # What the tie is for: floor and round-half-up part company by exactly one there, so the
    # two engines' tie-break rules are the only thing deciding the answer.
    assert (product + denominator // 2) // denominator - product // denominator == 1


def test_solution_is_the_one_nearest_the_requested_value() -> None:
    # Solutions to `x * 3 == 5 (mod 10)` are 5, 15, 25, ...; 25 is the closest to 24.
    assert half_way_operand(3, 10, near=24) == 25


def test_solution_clears_the_requested_minimum() -> None:
    assert half_way_operand(3, 10, near=0, minimum=1) == 5


@pytest.mark.parametrize(
    ("multiplier", "denominator"),
    [
        # An odd denominator has no exact half to land on.
        (1, 3),
        # `x * 4` only ever reaches multiples of 4 modulo 10, and 5 is not one.
        (4, 10),
    ],
)
def test_unreachable_ties_are_reported_rather_than_approximated(multiplier: int, denominator: int) -> None:
    assert half_way_operand(multiplier, denominator, near=100) is None


if __name__ == "__main__":
    pytest_bazel.main()
