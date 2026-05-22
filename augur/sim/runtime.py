"""Shared runtime semantics for simulator engines."""

from __future__ import annotations

import polars as pl

from augur.sim.jurisdictions import Jurisdiction, load_jurisdiction
from augur.sim.locations import Location, load_location
from augur.sim.scenario import Scenario

LONG_TERM_CAPITAL_GAIN = "ltcg"
SHORT_TERM_CAPITAL_GAIN = "stcg"


def load_jurisdictions_for(scenario: Scenario) -> dict[str, Jurisdiction]:
    """Load every jurisdiction referenced by any scenario tax profile."""

    ids = {jurisdiction_id for profile in scenario.tax_profiles for jurisdiction_id in profile.jurisdiction_ids}
    return {jurisdiction_id: load_jurisdiction(jurisdiction_id) for jurisdiction_id in ids}


def load_locations_for(scenario: Scenario) -> dict[str, Location]:
    """Load locations needed to resolve configured property-tax policies."""

    tax_policy_properties = {
        policy.property_id for policy in scenario.property_tax_policies if policy.annual_tax_rate is None
    }
    ids = {
        purchase.location_id
        for purchase in scenario.scheduled_property_purchases
        if purchase.property_id in tax_policy_properties
    }
    return {location_id: load_location(location_id) for location_id in ids}


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


def long_term_capital_gain_expr(month_index: int, purchase_month_column: str = "purchase_month_index") -> pl.Expr:
    return (pl.lit(month_index) - pl.col(purchase_month_column)) >= 12


def capital_gain_classification_expr(
    month_index_column: str = "month_index", purchase_month_column: str = "purchase_month_index"
) -> pl.Expr:
    return (
        pl.when(pl.col(month_index_column) - pl.col(purchase_month_column) >= 12)
        .then(pl.lit(LONG_TERM_CAPITAL_GAIN))
        .otherwise(pl.lit(SHORT_TERM_CAPITAL_GAIN))
    )
