"""Coupon schedule and coupon arithmetic."""

from __future__ import annotations

import pytest
import pytest_bazel

from finance.augur.sim.bonds import coupon_amount_quanta, coupon_months

_FACE_QUANTA = 10_000_000  # $100,000.00


def test_coupons_run_from_first_period_to_maturity_inclusive() -> None:
    months = coupon_months(purchase_month_index=0, maturity_month_index=24, coupon_period_months=6)

    # Not month 0: a bond bought today does not pay today.
    assert months == [6, 12, 18, 24]


def test_coupon_schedule_survives_a_purchase_before_the_horizon() -> None:
    """A bond bought before month 0 keeps paying on its own anniversary, not the sim's."""

    assert coupon_months(purchase_month_index=-4, maturity_month_index=8, coupon_period_months=6) == [2, 8]


def test_coupon_is_the_periodic_fraction_of_the_annual_rate() -> None:
    semiannual = coupon_amount_quanta(
        face_quanta=_FACE_QUANTA, annual_coupon_rate_ppb=40_000_000, coupon_period_months=6
    )
    quarterly = coupon_amount_quanta(
        face_quanta=_FACE_QUANTA, annual_coupon_rate_ppb=40_000_000, coupon_period_months=3
    )

    assert semiannual == 200_000  # $2,000.00
    assert quarterly == 100_000  # $1,000.00


def test_coupon_arithmetic_is_exact_at_a_scale_that_breaks_float64() -> None:
    """Above 2^53 cents a float64 cannot represent every cent, so a coupon routed through
    one would land on a neighbouring value. Fractions are integer pairs, so this is exact.
    """

    face_quanta = 1_000_000_000_000_000_001  # ~$10 quadrillion, and odd

    assert (
        coupon_amount_quanta(face_quanta=face_quanta, annual_coupon_rate_ppb=1_000_000_000, coupon_period_months=12)
        == face_quanta
    )


@pytest.mark.parametrize(("coupon_period_months", "periods_per_year"), [(1, 12), (3, 4), (6, 2), (12, 1)])
def test_a_years_coupons_land_within_rounding_of_the_annual_rate(
    coupon_period_months: int, periods_per_year: int
) -> None:
    """$9,250.00/yr on 250k at 3.7%. Exact where the split is exact, and never off by more
    than the per-period rounding otherwise — 925000 cents does not divide by 12, so monthly
    coupons genuinely cannot sum to it.

    A real bond pays a fixed coupon each period rather than one that varies to make the year
    total come out round, so the fixed coupon is the faithful model and this residual is a
    property of money being discrete, not an error to spread away.
    """

    coupon = coupon_amount_quanta(
        face_quanta=25_000_000, annual_coupon_rate_ppb=37_000_000, coupon_period_months=coupon_period_months
    )

    assert abs(coupon * periods_per_year - 925_000) * 2 <= periods_per_year


def test_zero_coupon_pays_nothing_until_maturity() -> None:
    assert coupon_amount_quanta(face_quanta=_FACE_QUANTA, annual_coupon_rate_ppb=0, coupon_period_months=6) == 0


@pytest.mark.parametrize(("face", "rate", "period"), [(-1, 1, 1), (1, -1, 1), (1, 1, 0)])
def test_coupon_rejects_invalid_exact_terms(face: int, rate: int, period: int) -> None:
    with pytest.raises(ValueError, match="face/rate must be nonnegative and coupon period positive"):
        coupon_amount_quanta(face_quanta=face, annual_coupon_rate_ppb=rate, coupon_period_months=period)


def test_coupon_rejects_overflow_without_float_conversion() -> None:
    with pytest.raises(OverflowError, match="coupon does not fit"):
        coupon_amount_quanta(face_quanta=(1 << 63) - 1, annual_coupon_rate_ppb=2_000_000_000, coupon_period_months=12)


if __name__ == "__main__":
    pytest_bazel.main()
