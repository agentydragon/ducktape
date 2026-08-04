"""Bond mechanics and the phase-1 config boundary."""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.sim.bonds import coupon_amount_cents, coupon_months, is_held
from finance.augur.sim.scenario import BondHolding


def _bond(**overrides: object) -> BondHolding:
    return BondHolding.model_validate(
        {
            "bond_id": "t10",
            "agent_id": "alice",
            "issuer_jurisdiction_id": "federal_us",
            "face_value_usd": 100_000.0,
            "purchase_price_usd": 100_000.0,
            "annual_coupon_rate": 0.04,
            "coupon_period_months": 6,
            "purchase_month_index": 0,
            "maturity_month_index": 120,
            **overrides,
        }
    )


def test_coupons_run_from_first_period_to_maturity_inclusive() -> None:
    months = coupon_months(purchase_month_index=0, maturity_month_index=24, coupon_period_months=6)

    # Not month 0: a bond bought today does not pay today.
    assert months == [6, 12, 18, 24]


def test_coupon_schedule_survives_a_purchase_before_the_horizon() -> None:
    """A bond bought before month 0 keeps paying on its own anniversary, not the sim's."""

    assert coupon_months(purchase_month_index=-4, maturity_month_index=8, coupon_period_months=6) == [2, 8]


def test_coupon_is_the_periodic_fraction_of_the_annual_rate() -> None:
    semiannual = coupon_amount_cents(face_value_usd=100_000.0, annual_coupon_rate=0.04, coupon_period_months=6)
    quarterly = coupon_amount_cents(face_value_usd=100_000.0, annual_coupon_rate=0.04, coupon_period_months=3)

    assert semiannual == 200_000  # $2,000.00
    assert quarterly == 100_000  # $1,000.00


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

    coupon = coupon_amount_cents(
        face_value_usd=250_000.0, annual_coupon_rate=0.037, coupon_period_months=coupon_period_months
    )

    assert abs(coupon * periods_per_year - 925_000) * 2 <= periods_per_year


def test_zero_coupon_pays_nothing_until_maturity() -> None:
    assert coupon_amount_cents(face_value_usd=100_000.0, annual_coupon_rate=0.0, coupon_period_months=6) == 0


def test_maturity_month_still_counts_as_held() -> None:
    """The face comes back during the maturity month, so the position is still on the
    balance sheet when that month's net worth is struck."""

    assert is_held(month_index=120, purchase_month_index=0, maturity_month_index=120)
    assert not is_held(month_index=121, purchase_month_index=0, maturity_month_index=120)
    assert not is_held(month_index=-1, purchase_month_index=0, maturity_month_index=120)


def test_non_par_purchase_is_rejected_naming_phase_2() -> None:
    """The whole reason phase 1 needs no discount curve. A bond bought at 98.5 must raise
    rather than be silently treated as par."""

    with pytest.raises(ValidationError, match="phase 2"):
        _bond(purchase_price_usd=98_500.0)


def test_par_purchase_is_accepted() -> None:
    assert _bond().purchase_price_usd == 100_000.0


def test_stub_period_is_rejected() -> None:
    with pytest.raises(ValidationError, match="whole number"):
        _bond(maturity_month_index=121)


def test_maturity_at_or_before_purchase_is_rejected() -> None:
    with pytest.raises(ValidationError, match="matures at or before purchase"):
        _bond(purchase_month_index=12, maturity_month_index=12)


def test_corporate_issuer_is_expressible() -> None:
    """`None` is a real issuer state (non-governmental), not a missing value — no
    jurisdiction exempts it."""

    assert _bond(issuer_jurisdiction_id=None).issuer_jurisdiction_id is None


if __name__ == "__main__":
    pytest_bazel.main()
