"""Shared runtime semantics for simulator engines."""

from __future__ import annotations

from finance.augur.sim.jurisdictions import Jurisdiction, load_jurisdiction
from finance.augur.sim.scenario import Scenario

LONG_TERM_CAPITAL_GAIN = "ltcg"
SHORT_TERM_CAPITAL_GAIN = "stcg"


def load_jurisdictions_for(scenario: Scenario) -> dict[str, Jurisdiction]:
    """Load every jurisdiction referenced by any scenario tax profile."""

    ids = {jurisdiction_id for profile in scenario.tax_profiles for jurisdiction_id in profile.jurisdiction_ids}
    return {jurisdiction_id: load_jurisdiction(jurisdiction_id) for jurisdiction_id in ids}


def mortgage_monthly_payment_usd(principal_usd: float, annual_interest_rate: float, term_months: int) -> float:
    monthly_rate = annual_interest_rate / 12.0
    if monthly_rate == 0:
        return principal_usd / term_months
    return principal_usd * monthly_rate / (1.0 - (1.0 + monthly_rate) ** -term_months)


def is_tax_year_end(month: int) -> bool:
    """Calendar-year-aligned tax years end at month indices 11, 23, 35, ..."""

    return month % 12 == 11


def estimated_tax_quarter(month: int) -> int | None:
    """Return the estimated-tax marker quarter for a zero-based month."""

    month_in_year = month % 12
    if month_in_year == 3:
        return 1
    if month_in_year == 5:
        return 2
    if month_in_year == 8:
        return 3
    if month_in_year == 0 and month > 0:
        return 4
    return None


def capital_gain_classification(month_index: int, purchase_month_index: int) -> str:
    if is_long_term_capital_gain(month_index, purchase_month_index):
        return LONG_TERM_CAPITAL_GAIN
    return SHORT_TERM_CAPITAL_GAIN


def is_long_term_capital_gain(month_index: int, purchase_month_index: int) -> bool:
    return month_index - purchase_month_index >= 12
