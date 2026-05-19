"""Tests for `augur.core.tax_actor`."""

from __future__ import annotations

import numpy as np
import pytest_bazel

from augur.core.scenario_set import TaxFilingStatus, TaxProfile
from augur.core.tax_actor import TaxActor


def _profile(prior_year_tax_usd: float | None = None) -> TaxProfile:
    return TaxProfile(
        filing_status=TaxFilingStatus.SINGLE,
        annual_ordinary_income_usd=100_000.0,
        prior_year_tax_usd=prior_year_tax_usd,
    )


def test_quarterly_obligation_uses_prior_year_for_first_year_only_when_supplied() -> None:
    actor = TaxActor(tax_profile=_profile(prior_year_tax_usd=20_000.0), rollout_count=2)
    # Q1 of year 0 (offset 3): should return safe-harbor amount.
    q1 = actor.quarterly_obligation_due(month_index=3)
    assert q1 is not None
    np.testing.assert_allclose(q1, [5000.0, 5000.0])  # 20_000 * 1.0 / 4 (income < high-AGI threshold)


def test_quarterly_obligation_year_zero_skips_when_no_prior_year_tax() -> None:
    actor = TaxActor(tax_profile=_profile(prior_year_tax_usd=None), rollout_count=2)
    assert actor.quarterly_obligation_due(month_index=3) is None


def test_quarterly_obligation_year_n_uses_prior_year_actual() -> None:
    actor = TaxActor(tax_profile=_profile(prior_year_tax_usd=None), rollout_count=2)
    # Simulate year 0 having closed out with $12k tax.
    actor.year_actual_tax_usd[0] = np.array([12_000.0, 8_000.0])
    # Q1 of year 1 (offset 3 in year 1 = month_index 15)
    q1_year_1 = actor.quarterly_obligation_due(month_index=15)
    assert q1_year_1 is not None
    np.testing.assert_allclose(q1_year_1, [3000.0, 2000.0])


def test_q4_of_year_zero_is_emitted_at_jan_15_of_year_one() -> None:
    actor = TaxActor(tax_profile=_profile(prior_year_tax_usd=20_000.0), rollout_count=1)
    # Jan 15 of year 1 = month_index 12, offset 0. Q4 for year 0.
    q4 = actor.quarterly_obligation_due(month_index=12)
    assert q4 is not None
    np.testing.assert_allclose(q4, [5000.0])


def test_annual_obligation_only_at_december() -> None:
    actor = TaxActor(tax_profile=_profile(prior_year_tax_usd=20_000.0), rollout_count=1)
    assert actor.annual_obligation_due(month_index=10) is None
    # Year 0 ended without any events recorded — actual tax is zero.
    annual = actor.annual_obligation_due(month_index=11)
    assert annual is not None
    np.testing.assert_array_equal(annual, [0.0])


def test_annual_obligation_subtracts_estimated_paid() -> None:
    actor = TaxActor(tax_profile=_profile(prior_year_tax_usd=20_000.0), rollout_count=1)
    actor.record_estimated_paid(month_index=3, paid_usd=np.array([5000.0]))  # Q1
    actor.record_estimated_paid(month_index=5, paid_usd=np.array([5000.0]))  # Q2
    actor.record_estimated_paid(month_index=8, paid_usd=np.array([5000.0]))  # Q3
    # Year 0 with no taxable events → actual = 0. Year-end true-up = max(0, 0 - 15_000) = 0.
    annual = actor.annual_obligation_due(month_index=11)
    assert annual is not None
    np.testing.assert_array_equal(annual, [0.0])


def test_observed_capital_gain_drives_year_end_tax() -> None:
    actor = TaxActor(tax_profile=_profile(prior_year_tax_usd=None), rollout_count=1)
    # Big sp500 gain in month 5 of year 0.
    actor.observe_month(
        month_index=5,
        property_depreciation_recapture_usd=np.array([0.0]),
        taxable_property_capital_gain_usd=np.array([0.0]),
        generic_sp500_sale_gain_usd=np.array([100_000.0]),
        private_equity_sale_taxable_gain_usd=np.array([0.0]),
        net_rental_taxable_income_usd=np.array([0.0]),
        property_tax_paid_usd=np.array([0.0]),
        mortgage_interest_paid_usd=np.array([0.0]),
        mortgage_principal_balance_usd=np.array([0.0]),
    )
    annual = actor.annual_obligation_due(month_index=11)
    assert annual is not None
    # Long-term cap gain on $100k at $100k ordinary income — non-trivial federal+CA tax,
    # so the obligation is meaningfully positive.
    assert annual[0] > 5000.0


def test_q4_credit_routes_to_prior_year() -> None:
    actor = TaxActor(tax_profile=_profile(prior_year_tax_usd=20_000.0), rollout_count=1)
    # Q4 due Jan 15 of year 1 (month_index 12) pays for tax year 0.
    actor.record_estimated_paid(month_index=12, paid_usd=np.array([5000.0]))
    # That credit lands on year 0's accumulator, not year 1's.
    assert actor.year_estimated_paid_usd[0][0] == 5000.0
    assert actor.year_estimated_paid_usd.get(1) is None or actor.year_estimated_paid_usd[1][0] == 0.0


if __name__ == "__main__":
    pytest_bazel.main()
