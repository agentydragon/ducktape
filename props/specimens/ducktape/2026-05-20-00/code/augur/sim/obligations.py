"""Due-now obligation accruals.

This module emits hard cash demands for the current month. It does
not decide how to liquidate assets or whether a demand is fully paid.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from augur.sim.amounts import amount_by_rollout
from augur.sim.events import EVENT_FRAMES
from augur.sim.locations import Location
from augur.sim.market import MarketContext
from augur.sim.scenario import PropertyTaxPolicy, RecurringObligation, Scenario, ScheduledObligation, TaxProfile
from augur.sim.state import StateCrossSection


@dataclass(frozen=True)
class DueNowObligations:
    """Hard demands and liability-side candidates for this month."""

    obligation_accruals: pl.DataFrame
    mortgage_payments: pl.DataFrame
    tax_settlement_candidates: pl.DataFrame


@dataclass(frozen=True)
class _TaxPaymentObligationEvents:
    obligation_accruals: pl.DataFrame
    settlements: pl.DataFrame


def emit_due_now_obligations(
    *, state: StateCrossSection, scenario: Scenario, market: MarketContext, locations: dict[str, Location], month: int
) -> DueNowObligations:
    active_rollouts = state.rollout_status.filter(pl.col("status") == "active").select("rollout_index")
    mortgage_payments = _emit_mortgage_payments(state, month)
    tax_payment_events = _emit_tax_payment_obligations(state=state, profiles=scenario.tax_profiles, month=month)
    obligations = EVENT_FRAMES.obligation_accruals.concat(
        [
            _emit_configured_obligations(scenario, market, month, active_rollouts),
            _mortgage_payment_obligations(mortgage_payments),
            _emit_property_tax_obligations(state=state, scenario=scenario, locations=locations, month=month),
            tax_payment_events.obligation_accruals,
        ]
    )
    return DueNowObligations(
        obligation_accruals=obligations,
        mortgage_payments=mortgage_payments,
        tax_settlement_candidates=tax_payment_events.settlements,
    )


def _emit_mortgage_payments(state: StateCrossSection, month: int) -> pl.DataFrame:
    liabilities = state.liabilities.filter((pl.col("principal_usd") > 0) & (pl.col("origination_month_index") < month))
    if liabilities.is_empty():
        return EVENT_FRAMES.mortgage_payments.empty()
    monthly_interest = pl.col("principal_usd") * pl.col("annual_interest_rate") / 12.0
    total_payment = pl.min_horizontal(pl.col("monthly_payment_usd"), pl.col("principal_usd") + monthly_interest)
    return (
        liabilities.with_columns(
            _interest_usd=pl.min_horizontal(monthly_interest, total_payment), _total_payment_usd=total_payment
        )
        .with_columns(_principal_usd=pl.max_horizontal(0.0, pl.col("_total_payment_usd") - pl.col("_interest_usd")))
        .with_columns(
            pl.lit(month, dtype=pl.Int64()).alias("month_index"),
            pl.concat_str([pl.col("liability_id"), pl.lit("_payment_m"), pl.lit(str(month))]).alias("cause_id"),
            pl.col("payment_account_id").alias("from_account_id"),
            pl.col("counterparty_account_id").alias("to_account_id"),
            pl.col("_interest_usd").alias("interest_usd"),
            pl.col("_principal_usd").alias("principal_usd"),
            pl.col("_total_payment_usd").alias("total_payment_usd"),
        )
        .pipe(EVENT_FRAMES.mortgage_payments.normalize)
    )


def _mortgage_payment_obligations(mortgage_payments: pl.DataFrame) -> pl.DataFrame:
    if mortgage_payments.is_empty():
        return EVENT_FRAMES.obligation_accruals.empty()
    return mortgage_payments.with_columns(
        pl.col("cause_id").alias("obligation_id"),
        pl.lit("mortgage_payment", dtype=pl.Utf8()).alias("obligation_type"),
        pl.col("counterparty_agent_id").alias("to_agent_id"),
        pl.col("total_payment_usd").alias("amount_due_usd"),
    ).pipe(EVENT_FRAMES.obligation_accruals.normalize)


def _emit_property_tax_obligations(
    *, state: StateCrossSection, scenario: Scenario, locations: dict[str, Location], month: int
) -> pl.DataFrame:
    active = [policy for policy in scenario.property_tax_policies if policy.is_active_at(month)]
    if not active or state.property_state.is_empty():
        return EVENT_FRAMES.obligation_accruals.empty()
    return EVENT_FRAMES.obligation_accruals.concat(
        [_property_tax_obligation_block(state, policy, locations, month) for policy in active]
    )


def _property_tax_obligation_block(
    state: StateCrossSection, policy: PropertyTaxPolicy, locations: dict[str, Location], month: int
) -> pl.DataFrame:
    property_rows = state.property_state.filter(
        (pl.col("property_id") == policy.property_id) & (pl.col("purchase_month_index") < month)
    )
    if property_rows.is_empty():
        return EVENT_FRAMES.obligation_accruals.empty()
    rate_rows = pl.DataFrame(
        {
            "location_id": list(locations),
            "_annual_tax_rate": [location.annual_property_tax_rate for location in locations.values()],
        },
        schema={"location_id": pl.Utf8(), "_annual_tax_rate": pl.Float64()},
    )
    taxed = (
        property_rows.join(rate_rows, on="location_id", how="left")
        .with_columns(
            _annual_tax_rate=pl.lit(policy.annual_tax_rate, dtype=pl.Float64())
            if policy.annual_tax_rate is not None
            else pl.col("_annual_tax_rate")
        )
        .with_columns(amount_usd=pl.col("adjusted_basis_usd") * pl.col("_annual_tax_rate") / 12.0)
        .filter(pl.col("amount_usd") > 0)
    )
    return taxed.with_columns(
        pl.lit(month, dtype=pl.Int64()).alias("month_index"),
        pl.lit(f"{policy.property_id}_property_tax_m{month}", dtype=pl.Utf8()).alias("cause_id"),
        pl.lit(f"{policy.property_id}_property_tax_m{month}", dtype=pl.Utf8()).alias("obligation_id"),
        pl.lit("property_tax", dtype=pl.Utf8()).alias("obligation_type"),
        pl.lit(policy.owner_agent_id, dtype=pl.Utf8()).alias("agent_id"),
        pl.lit(policy.from_account_id, dtype=pl.Utf8()).alias("from_account_id"),
        pl.lit(policy.tax_authority_agent_id, dtype=pl.Utf8()).alias("to_agent_id"),
        pl.lit(policy.tax_authority_account_id, dtype=pl.Utf8()).alias("to_account_id"),
        pl.col("amount_usd").alias("amount_due_usd"),
    ).pipe(EVENT_FRAMES.obligation_accruals.normalize)


def _emit_configured_obligations(
    scenario: Scenario, market: MarketContext, month: int, rollouts: pl.DataFrame
) -> pl.DataFrame:
    active: list[ScheduledObligation | RecurringObligation] = [
        obligation for obligation in scenario.scheduled_obligations if obligation.month == month
    ]
    active.extend(obligation for obligation in scenario.recurring_obligations if obligation.is_active_at(month))
    if not active:
        return EVENT_FRAMES.obligation_accruals.empty()
    return EVENT_FRAMES.obligation_accruals.concat(
        [_configured_obligation_block_per_rollout(obligation, market, rollouts, month) for obligation in active]
    )


def _configured_obligation_block_per_rollout(
    obligation: ScheduledObligation | RecurringObligation, market: MarketContext, rollouts: pl.DataFrame, month: int
) -> pl.DataFrame:
    amounts = amount_by_rollout(
        obligation.amount_due_usd, market=market, rollouts=rollouts, month=month, column_name="amount_due_usd"
    )
    return (
        rollouts.join(amounts, on="rollout_index")
        .with_columns(
            pl.lit(month, dtype=pl.Int64()).alias("month_index"),
            pl.lit(f"{obligation.obligation_id}_m{month}", dtype=pl.Utf8()).alias("cause_id"),
            pl.lit(f"{obligation.obligation_id}_m{month}", dtype=pl.Utf8()).alias("obligation_id"),
            pl.lit(obligation.obligation_type, dtype=pl.Utf8()).alias("obligation_type"),
            pl.lit(obligation.agent_id, dtype=pl.Utf8()).alias("agent_id"),
            pl.lit(obligation.from_account_id, dtype=pl.Utf8()).alias("from_account_id"),
            pl.lit(obligation.to_agent_id, dtype=pl.Utf8()).alias("to_agent_id"),
            pl.lit(obligation.to_account_id, dtype=pl.Utf8()).alias("to_account_id"),
        )
        .pipe(EVENT_FRAMES.obligation_accruals.normalize)
    )


def _emit_tax_payment_obligations(
    *, state: StateCrossSection, profiles: list[TaxProfile], month: int
) -> _TaxPaymentObligationEvents:
    """Emit estimated-tax obligations and liability-settlement candidates."""

    if not profiles:
        return _empty_tax_payment_obligation_events()
    obligation_blocks: list[pl.DataFrame] = []
    settlement_blocks: list[pl.DataFrame] = []
    quarter = _estimated_tax_quarter(month)
    for profile in profiles:
        if quarter in {1, 2, 3}:
            amount = profile.prior_year_tax_usd / 4.0
            if amount <= 0:
                continue
            amounts = state.rollout_status.select("rollout_index").with_columns(
                pl.lit(amount, dtype=pl.Float64()).alias("amount_usd")
            )
            tax_year = month // 12
            obligation_blocks.append(
                _tax_payment_obligation_block(
                    amounts=amounts,
                    profile=profile,
                    month=month,
                    cause_id=f"{profile.agent_id}_estimated_tax_q{quarter}_y{tax_year}",
                    obligation_type="estimated_tax",
                )
            )
        elif quarter == 4:
            tax_year = month // 12 - 1
            if tax_year < 0:
                continue
            final_events = _final_estimated_and_true_up_events(
                state=state, profile=profile, month=month, tax_year=tax_year
            )
            obligation_blocks.append(final_events.obligation_accruals)
            settlement_blocks.append(final_events.settlements)
    return _TaxPaymentObligationEvents(
        obligation_accruals=EVENT_FRAMES.obligation_accruals.concat(obligation_blocks),
        settlements=EVENT_FRAMES.tax_settlements.concat(settlement_blocks),
    )


def _empty_tax_payment_obligation_events() -> _TaxPaymentObligationEvents:
    return _TaxPaymentObligationEvents(
        obligation_accruals=EVENT_FRAMES.obligation_accruals.empty(), settlements=EVENT_FRAMES.tax_settlements.empty()
    )


def _estimated_tax_quarter(month: int) -> int | None:
    """Calendar-month markers in a zero-based monthly simulation."""

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


def _final_estimated_and_true_up_events(
    *, state: StateCrossSection, profile: TaxProfile, month: int, tax_year: int
) -> _TaxPaymentObligationEvents:
    actual = _actual_tax_by_rollout(state.tax_liabilities, profile=profile, tax_year=tax_year)
    if actual.is_empty():
        return _empty_tax_payment_obligation_events()
    safe_harbor_total = pl.min_horizontal(
        pl.lit(profile.prior_year_tax_usd, dtype=pl.Float64()), pl.col("_actual_tax_usd")
    )
    paid_before_q4 = profile.prior_year_tax_usd * 0.75
    payments = actual.with_columns(
        _q4_amount_usd=pl.max_horizontal(pl.lit(0.0), safe_harbor_total - pl.lit(paid_before_q4)),
        _true_up_amount_usd=pl.max_horizontal(pl.lit(0.0), pl.col("_actual_tax_usd") - safe_harbor_total),
    )
    q4 = payments.select("rollout_index", pl.col("_q4_amount_usd").alias("amount_usd"))
    true_up = payments.select("rollout_index", pl.col("_true_up_amount_usd").alias("amount_usd"))
    settlement = payments.select("rollout_index", pl.col("_actual_tax_usd").alias("amount_usd"))
    return _TaxPaymentObligationEvents(
        obligation_accruals=EVENT_FRAMES.obligation_accruals.concat(
            [
                _tax_payment_obligation_block(
                    amounts=q4,
                    profile=profile,
                    month=month,
                    cause_id=f"{profile.agent_id}_estimated_tax_q4_y{tax_year}",
                    obligation_type="estimated_tax",
                ),
                _tax_payment_obligation_block(
                    amounts=true_up,
                    profile=profile,
                    month=month,
                    cause_id=f"{profile.agent_id}_tax_true_up_y{tax_year}",
                    obligation_type="tax_true_up",
                ),
            ]
        ),
        settlements=_tax_settlement_block(
            amounts=settlement,
            profile=profile,
            month=month,
            tax_year=tax_year,
            cause_id=f"{profile.agent_id}_tax_settlement_y{tax_year}",
        ),
    )


def _actual_tax_by_rollout(tax_liabilities: pl.DataFrame, *, profile: TaxProfile, tax_year: int) -> pl.DataFrame:
    tax_year_end_month = tax_year * 12 + 11
    return (
        tax_liabilities.filter(
            (pl.col("agent_id") == profile.agent_id) & (pl.col("tax_year_end_month") == tax_year_end_month)
        )
        .group_by("rollout_index")
        .agg(pl.col("amount_owed_usd").sum().alias("_actual_tax_usd"))
    )


def _tax_payment_obligation_block(
    *, amounts: pl.DataFrame, profile: TaxProfile, month: int, cause_id: str, obligation_type: str
) -> pl.DataFrame:
    return (
        amounts.filter(pl.col("amount_usd") > 0)
        .with_columns(
            pl.lit(month, dtype=pl.Int64()).alias("month_index"),
            pl.lit(cause_id, dtype=pl.Utf8()).alias("cause_id"),
            pl.lit(cause_id, dtype=pl.Utf8()).alias("obligation_id"),
            pl.lit(obligation_type, dtype=pl.Utf8()).alias("obligation_type"),
            pl.lit(profile.agent_id, dtype=pl.Utf8()).alias("agent_id"),
            pl.lit(profile.payment_account_id, dtype=pl.Utf8()).alias("from_account_id"),
            pl.lit(profile.tax_authority_agent_id, dtype=pl.Utf8()).alias("to_agent_id"),
            pl.lit(profile.tax_authority_account_id, dtype=pl.Utf8()).alias("to_account_id"),
            pl.col("amount_usd").alias("amount_due_usd"),
        )
        .pipe(EVENT_FRAMES.obligation_accruals.normalize)
    )


def _tax_settlement_block(
    *, amounts: pl.DataFrame, profile: TaxProfile, month: int, tax_year: int, cause_id: str
) -> pl.DataFrame:
    tax_year_end_month = tax_year * 12 + 11
    return (
        amounts.filter(pl.col("amount_usd") > 0)
        .with_columns(
            pl.lit(month, dtype=pl.Int64()).alias("month_index"),
            pl.lit(cause_id, dtype=pl.Utf8()).alias("cause_id"),
            pl.lit(profile.agent_id, dtype=pl.Utf8()).alias("agent_id"),
            pl.lit(tax_year_end_month, dtype=pl.Int64()).alias("tax_year_end_month"),
        )
        .pipe(EVENT_FRAMES.tax_settlements.normalize)
    )
