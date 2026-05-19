"""Per-actor tax accumulator that emits tax obligations during the main
month loop, replacing the post-loop `_settle_required_cash_obligations`
sweep for estimated and annual taxes.

The state-vector simulation refactor (Phase 4) treats taxes as an actor
that observes taxable events month by month and emits obligations at
their natural markers:

  - Quarterly estimated payments (Apr 15, Jun 15, Sep 15, and Jan 15 of
    the following year) — amount based on safe-harbor of prior year's
    actual tax (or `tax_profile.prior_year_tax_usd` for year 0).
  - Year-end true-up (Dec) — actual year's tax minus sum of estimated
    paid this year.

The TaxActor maintains per-year accumulators for each taxable-event
category. At year-end (offset 11 within the tax year), it computes the
year's actual tax via the same `federal_income_tax_due_usd` /
`california_income_tax_due_usd` calls that
`annual_sale_tax_allocation` uses internally. The post-loop
`annual_sale_tax_allocation` call still runs for per-month *reporting*
matrices (federal/CA/property/sp500/PE/rental per-month allocation);
this actor only handles the *obligation* side.

See `augur/plans/state_vector_simulation_refactor.md` for the broader
plan."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from augur.core.annual_tax import (
    FEDERAL_QUALIFIED_RESIDENCE_INTEREST_PRINCIPAL_CAP_USD,
    FEDERAL_SALT_CAP_USD,
    california_income_tax_due_usd,
    federal_income_tax_due_usd,
)
from augur.core.scenario_set import TaxProfile

MONTHS_PER_YEAR = 12

# Quarterly estimated-tax due months (offset within the tax year).
# Q1 = Apr 15 → month offset 3; Q2 = Jun 15 → offset 5;
# Q3 = Sep 15 → offset 8; Q4 = Jan 15 of next year → offset 12 (= month 0
# of year N+1, so applied as offset 0 of the next year).
_QUARTERLY_OFFSETS_IN_TAX_YEAR: tuple[int, ...] = (3, 5, 8)
_Q4_OFFSET_IN_NEXT_YEAR: int = 0
_YEAR_END_TAX_OFFSET: int = 11

# Safe-harbor fraction of prior year's tax to pay across the 4 quarters.
# 110% if prior-year AGI >= threshold, else 100%. First-year fallback:
# 90% of current year's tax (we can't know current year mid-loop, so we
# fall back to prior_year_tax_usd or skip — see TaxActor.quarterly_*).
_SAFE_HARBOR_FIRST_YEAR_FRACTION = 0.90
_SAFE_HARBOR_HIGH_INCOME_FRACTION = 1.10
_SAFE_HARBOR_LOW_INCOME_FRACTION = 1.00
_SAFE_HARBOR_HIGH_INCOME_AGI_THRESHOLD_USD = 150_000.0


def _safe_harbor_prior_year_fraction(tax_profile: TaxProfile) -> float:
    if tax_profile.annual_ordinary_income_usd >= _SAFE_HARBOR_HIGH_INCOME_AGI_THRESHOLD_USD:
        return _SAFE_HARBOR_HIGH_INCOME_FRACTION
    return _SAFE_HARBOR_LOW_INCOME_FRACTION


@dataclass
class _TaxYearAccumulator:
    """Per-(rollouts,) running totals for one tax year. Each field starts
    at zero and is mutated by `observe_month(...)`. Closed out at the
    year-end month into `TaxActor.year_actual_tax[year]`."""

    property_depreciation_recapture_usd: np.ndarray
    taxable_property_capital_gain_usd: np.ndarray
    generic_sp500_sale_gain_usd: np.ndarray
    private_equity_sale_taxable_gain_usd: np.ndarray
    net_rental_taxable_income_usd: np.ndarray
    property_tax_paid_usd: np.ndarray
    mortgage_interest_paid_usd: np.ndarray
    mortgage_principal_sum_usd: np.ndarray
    mortgage_principal_months_active: np.ndarray

    @classmethod
    def new(cls, rollout_count: int) -> _TaxYearAccumulator:
        zeros = lambda: np.zeros(rollout_count, dtype="float64")  # noqa: E731
        return cls(
            property_depreciation_recapture_usd=zeros(),
            taxable_property_capital_gain_usd=zeros(),
            generic_sp500_sale_gain_usd=zeros(),
            private_equity_sale_taxable_gain_usd=zeros(),
            net_rental_taxable_income_usd=zeros(),
            property_tax_paid_usd=zeros(),
            mortgage_interest_paid_usd=zeros(),
            mortgage_principal_sum_usd=zeros(),
            mortgage_principal_months_active=zeros(),
        )


@dataclass
class TaxActor:
    """Tracks per-year tax events and emits obligation amounts at
    quarterly + year-end markers.

    Single-actor today (the primary owner). Multi-actor would need one
    TaxActor instance per actor with its own TaxProfile."""

    tax_profile: TaxProfile
    rollout_count: int
    # Per-year running totals (year_index → accumulator).
    years: dict[int, _TaxYearAccumulator] = field(default_factory=dict)
    # Per-year closed-out actual tax (filled at year-end).
    year_actual_tax_usd: dict[int, np.ndarray] = field(default_factory=dict)
    # Per-year sum of estimated payments actually made (running, updated
    # by `record_estimated_paid`).
    year_estimated_paid_usd: dict[int, np.ndarray] = field(default_factory=dict)
    # Baselines (precomputed once from tax_profile) — the tax owed on
    # the user's payroll income alone, which is subtracted from the
    # year's total to leave only the *incremental* tax driven by the
    # simulation's events.
    _baseline_federal_usd: np.ndarray = field(init=False)
    _baseline_california_usd: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        ordinary_income = np.full(
            self.rollout_count, float(self.tax_profile.annual_ordinary_income_usd), dtype="float64"
        )
        zeros = np.zeros(self.rollout_count, dtype="float64")
        self._baseline_federal_usd = federal_income_tax_due_usd(
            self.tax_profile,
            ordinary_income_usd=ordinary_income,
            unrecaptured_1250_gain_usd=zeros,
            long_term_capital_gain_usd=zeros,
        )
        self._baseline_california_usd = california_income_tax_due_usd(
            self.tax_profile, ordinary_income_usd=ordinary_income, capital_income_usd=zeros
        )

    def _year_accumulator(self, year: int) -> _TaxYearAccumulator:
        if year not in self.years:
            self.years[year] = _TaxYearAccumulator.new(self.rollout_count)
            self.year_estimated_paid_usd[year] = np.zeros(self.rollout_count, dtype="float64")
        return self.years[year]

    def observe_month(
        self,
        *,
        month_index: int,
        property_depreciation_recapture_usd: np.ndarray,
        taxable_property_capital_gain_usd: np.ndarray,
        generic_sp500_sale_gain_usd: np.ndarray,
        private_equity_sale_taxable_gain_usd: np.ndarray,
        net_rental_taxable_income_usd: np.ndarray,
        property_tax_paid_usd: np.ndarray,
        mortgage_interest_paid_usd: np.ndarray,
        mortgage_principal_balance_usd: np.ndarray,
    ) -> None:
        """Add this month's taxable events to the appropriate year's
        accumulator. All inputs are `(rollouts,)` per-rollout vectors."""
        year = month_index // MONTHS_PER_YEAR
        accumulator = self._year_accumulator(year)
        accumulator.property_depreciation_recapture_usd += np.maximum(0.0, property_depreciation_recapture_usd)
        accumulator.taxable_property_capital_gain_usd += np.maximum(0.0, taxable_property_capital_gain_usd)
        accumulator.generic_sp500_sale_gain_usd += np.maximum(0.0, generic_sp500_sale_gain_usd)
        accumulator.private_equity_sale_taxable_gain_usd += np.maximum(0.0, private_equity_sale_taxable_gain_usd)
        accumulator.net_rental_taxable_income_usd += np.maximum(0.0, net_rental_taxable_income_usd)
        accumulator.property_tax_paid_usd += property_tax_paid_usd
        accumulator.mortgage_interest_paid_usd += mortgage_interest_paid_usd
        active = (mortgage_principal_balance_usd > 0).astype("float64")
        accumulator.mortgage_principal_sum_usd += mortgage_principal_balance_usd
        accumulator.mortgage_principal_months_active += active

    def quarterly_obligation_due(self, *, month_index: int) -> np.ndarray | None:
        """If `month_index` falls on a quarterly estimated-tax marker,
        return the safe-harbor amount this rollout owes; else None.

        Per-rollout amount = (prior-year actual × safe-harbor fraction)
        / 4. Year 0 uses `tax_profile.prior_year_tax_usd` when supplied;
        otherwise no quarterly obligation is emitted (the first-year-90%
        exception requires knowing the current year's tax, which we
        don't have until December — falling back to no obligation is
        consistent with the IRS rule's effect: no penalty, just the
        year-end true-up swallows the full amount)."""
        offset_in_year = month_index % MONTHS_PER_YEAR
        tax_year = month_index // MONTHS_PER_YEAR
        if offset_in_year in _QUARTERLY_OFFSETS_IN_TAX_YEAR:
            obligation_tax_year = tax_year
        elif offset_in_year == _Q4_OFFSET_IN_NEXT_YEAR and tax_year >= 1:
            # Jan 15 of year N pays Q4 for tax year N-1.
            obligation_tax_year = tax_year - 1
        else:
            return None
        if obligation_tax_year == 0:
            if self.tax_profile.prior_year_tax_usd is None:
                return None
            base_per_rollout = float(self.tax_profile.prior_year_tax_usd) * _safe_harbor_prior_year_fraction(
                self.tax_profile
            )
            return np.full(self.rollout_count, base_per_rollout / 4.0, dtype="float64")
        prior_year_actual = self.year_actual_tax_usd.get(obligation_tax_year - 1)
        if prior_year_actual is None:
            return None
        return prior_year_actual * _safe_harbor_prior_year_fraction(self.tax_profile) / 4.0

    def annual_obligation_due(self, *, month_index: int) -> np.ndarray | None:
        """If `month_index` is a year-end marker (offset 11 within its
        tax year), compute the year's actual tax (closing out the
        accumulator) and return `actual - estimated_paid_this_year`.
        Else None."""
        offset_in_year = month_index % MONTHS_PER_YEAR
        if offset_in_year != _YEAR_END_TAX_OFFSET:
            return None
        year = month_index // MONTHS_PER_YEAR
        accumulator = self._year_accumulator(year)
        actual_tax = self._compute_year_actual_tax(accumulator)
        self.year_actual_tax_usd[year] = actual_tax
        return np.maximum(0.0, actual_tax - self.year_estimated_paid_usd[year])

    def record_estimated_paid(self, *, month_index: int, paid_usd: np.ndarray) -> None:
        """Add this month's estimated-tax payment to the appropriate
        year's running estimated-paid total. Q1-Q3 credit the current
        tax year; Q4 (Jan 15 of year N+1) credits tax year N."""
        offset_in_year = month_index % MONTHS_PER_YEAR
        tax_year = month_index // MONTHS_PER_YEAR
        credit_year = tax_year - 1 if offset_in_year == _Q4_OFFSET_IN_NEXT_YEAR and tax_year >= 1 else tax_year
        self._year_accumulator(credit_year)  # ensure initialized
        self.year_estimated_paid_usd[credit_year] = self.year_estimated_paid_usd[credit_year] + paid_usd

    def _compute_year_actual_tax(self, accumulator: _TaxYearAccumulator) -> np.ndarray:
        """Compute the year's actual incremental federal+CA tax from the
        accumulated events. Mirrors `annual_sale_tax_allocation`'s
        year-loop body, but operating on the actor's per-year
        accumulators rather than full per-month matrices."""
        long_term_capital_gain = (
            accumulator.taxable_property_capital_gain_usd
            + accumulator.generic_sp500_sale_gain_usd
            + accumulator.private_equity_sale_taxable_gain_usd
        )
        salt_deduction = np.minimum(accumulator.property_tax_paid_usd, FEDERAL_SALT_CAP_USD)
        average_principal = np.divide(
            accumulator.mortgage_principal_sum_usd,
            accumulator.mortgage_principal_months_active,
            out=np.zeros_like(accumulator.mortgage_principal_sum_usd),
            where=accumulator.mortgage_principal_months_active > 0,
        )
        # `_qualified_residence_interest_deduction_usd` in annual_tax.py
        # takes the per-month balance matrix; we already collapsed to
        # the year's sum/active, so inline the equivalent math here.
        deductible_fraction = np.divide(
            FEDERAL_QUALIFIED_RESIDENCE_INTEREST_PRINCIPAL_CAP_USD,
            np.maximum(average_principal, FEDERAL_QUALIFIED_RESIDENCE_INTEREST_PRINCIPAL_CAP_USD),
            out=np.ones_like(average_principal),
            where=average_principal > 0,
        )
        qualified_interest_deduction = accumulator.mortgage_interest_paid_usd * deductible_fraction
        ordinary_income = np.full(
            self.rollout_count, float(self.tax_profile.annual_ordinary_income_usd), dtype="float64"
        )
        federal_ordinary = np.maximum(
            0.0,
            ordinary_income + accumulator.net_rental_taxable_income_usd - salt_deduction - qualified_interest_deduction,
        )
        california_ordinary = np.maximum(
            0.0, ordinary_income + accumulator.net_rental_taxable_income_usd - qualified_interest_deduction
        )
        federal_tax = np.maximum(
            0.0,
            federal_income_tax_due_usd(
                self.tax_profile,
                ordinary_income_usd=federal_ordinary,
                unrecaptured_1250_gain_usd=accumulator.property_depreciation_recapture_usd,
                long_term_capital_gain_usd=long_term_capital_gain,
            )
            - self._baseline_federal_usd,
        )
        california_tax = np.maximum(
            0.0,
            california_income_tax_due_usd(
                self.tax_profile,
                ordinary_income_usd=california_ordinary,
                capital_income_usd=accumulator.property_depreciation_recapture_usd + long_term_capital_gain,
            )
            - self._baseline_california_usd,
        )
        return federal_tax + california_tax
