"""`step_emit_events` — pure function that reads state + scenario +
month index and returns the events for that month.

At the current spike the step emits:

  - transfer events for scheduled + recurring transfers active at
    this month, optionally tagged with an `income_category`;
  - lot_disposition events for scheduled asset sales (FIFO across
    the agent's lots);
  - tax_accrual events at the end of each tax year (month_index
    in {11, 23, 35, ...}) — one per (taxed agent, jurisdiction),
    computed by bracket-walking end-of-year ordinary income minus
    the jurisdiction's standard deduction;
  - due-now obligations for configured required payments, mortgage
    payments, property tax, estimated-tax markers, and January
    true-ups after prior year-end accruals are known.

The step does not mutate `state`. The simulate loop calls
`apply_events(state, step_result)` separately. apply_events
processes income transfers before tax accruals so the accrual
amount the step computes is consistent with the YTD that apply
will produce.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from augur.sim.amounts import amount_by_rollout
from augur.sim.events import EVENT_FRAMES, EventLog
from augur.sim.jurisdictions import Jurisdiction
from augur.sim.liquidity import plan_liquidity
from augur.sim.locations import Location
from augur.sim.market import MarketContext
from augur.sim.obligations import emit_due_now_obligations
from augur.sim.scenario import (
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    TaxProfile,
)
from augur.sim.settlement import settle_due_now_demands
from augur.sim.state import StateCrossSection
from augur.sim.tax import apply_brackets, apply_ltcg_brackets


@dataclass(frozen=True)
class _TaxYearEvents:
    accruals: pl.DataFrame
    breakdowns: pl.DataFrame


def step_emit_scheduled_events(
    *,
    state: StateCrossSection,
    scenario: Scenario,
    market: MarketContext,
    jurisdictions: dict[str, Jurisdiction],
    locations: dict[str, Location],
    month: int,
    rollout_count: int,
) -> EventLog:
    """Phase 1 of the month step: scheduled / recurring transfers,
    scheduled asset sales, property purchases, mortgage originations,
    and year-end tax accruals. Pure: does not mutate `state`."""
    transfers = _emit_transfers(scenario, market, month, rollout_count)
    property_purchases = _emit_property_purchases(scenario, month, rollout_count)
    mortgage_originations = _emit_mortgage_originations(scenario, month, rollout_count)
    property_cash_transfers = _property_purchase_transfer_events(scenario, property_purchases)
    dispositions = _emit_lot_dispositions(state, scenario, market, month)
    tax_year_events = _emit_year_end_tax_events(
        state=state,
        scenario=scenario,
        jurisdictions=jurisdictions,
        month=month,
        transfers=transfers,
        dispositions=dispositions,
    )
    return EventLog.from_frames(
        {
            "transfers": EVENT_FRAMES.transfers.concat([transfers, property_cash_transfers]),
            "lot_dispositions": dispositions,
            "tax_accruals": tax_year_events.accruals,
            "tax_breakdowns": tax_year_events.breakdowns,
            "property_purchases": property_purchases,
            "mortgage_originations": mortgage_originations,
        }
    )


def step_emit_policy_events(
    *, state: StateCrossSection, scenario: Scenario, market: MarketContext, locations: dict[str, Location], month: int
) -> EventLog:
    """Phase 2 of the month step: due-now demand accrual, liquidity
    policy sale decisions, and mechanical settlement."""
    due_now = emit_due_now_obligations(state=state, scenario=scenario, market=market, locations=locations, month=month)
    liquidity_plan = plan_liquidity(
        state=state,
        policies=scenario.liquidity_policies,
        hard_demands=due_now.obligation_accruals,
        market=market,
        month=month,
    )
    settlement = settle_due_now_demands(
        state=state,
        obligations=due_now.obligation_accruals,
        planned_dispositions=liquidity_plan.lot_dispositions,
        attempted_sources_by_account=liquidity_plan.attempted_sources_by_account,
        mortgage_payments=due_now.mortgage_payments,
        tax_settlement_candidates=due_now.tax_settlement_candidates,
        month=month,
    )
    return EventLog.from_frames(
        {
            "transfers": settlement.transfers,
            "lot_dispositions": settlement.lot_dispositions,
            "tax_settlements": settlement.tax_settlements,
            "obligation_accruals": settlement.obligation_accruals,
            "obligation_settlements": settlement.obligation_settlements,
            "mortgage_payments": settlement.mortgage_payments,
            "rollout_failures": settlement.rollout_failures,
        }
    )


def _emit_transfers(scenario: Scenario, market: MarketContext, month: int, rollout_count: int) -> pl.DataFrame:
    """Emit Transfer event rows for every scheduled or recurring
    transfer active at this month. Scheduled transfers fire only at
    their configured month; recurring transfers fire every month in
    `[start_month, end_month]` (or through horizon end). One row per
    (transfer, rollout)."""
    active: list[ScheduledTransfer | RecurringTransfer] = [t for t in scenario.scheduled_transfers if t.month == month]
    active.extend(t for t in scenario.recurring_transfers if t.is_active_at(month))
    blocks: list[pl.DataFrame] = []
    if active:
        rollouts = pl.DataFrame({"rollout_index": list(range(rollout_count))}, schema={"rollout_index": pl.Int64()})
        blocks = [_transfer_block_per_rollout(t, market, rollouts, month) for t in active]
    return EVENT_FRAMES.transfers.concat(blocks)


def _transfer_block_per_rollout(
    t: ScheduledTransfer | RecurringTransfer, market: MarketContext, rollouts: pl.DataFrame, month: int
) -> pl.DataFrame:
    """One row per rollout for one transfer config. The rollout
    dimension is expanded vectorized — no Python loop over rollouts.
    Handles both ScheduledTransfer (one-off at a specific month) and
    RecurringTransfer (firing at this active month) — same event
    schema, only the cadence config differs."""
    amounts = amount_by_rollout(t.amount_usd, market=market, rollouts=rollouts, month=month, column_name="amount_usd")
    return (
        rollouts.join(amounts, on="rollout_index")
        .with_columns(
            pl.lit(month, dtype=pl.Int64()).alias("month_index"),
            pl.lit(t.cause_id, dtype=pl.Utf8()).alias("cause_id"),
            pl.lit(t.from_agent_id, dtype=pl.Utf8()).alias("from_agent_id"),
            pl.lit(t.from_account_id, dtype=pl.Utf8()).alias("from_account_id"),
            pl.lit(t.to_agent_id, dtype=pl.Utf8()).alias("to_agent_id"),
            pl.lit(t.to_account_id, dtype=pl.Utf8()).alias("to_account_id"),
            pl.lit(t.income_category, dtype=pl.Utf8()).alias("income_category"),
        )
        .pipe(EVENT_FRAMES.transfers.normalize)
    )


def _emit_property_purchases(scenario: Scenario, month: int, rollout_count: int) -> pl.DataFrame:
    purchases = [purchase for purchase in scenario.scheduled_property_purchases if purchase.month == month]
    if not purchases:
        return EVENT_FRAMES.property_purchases.empty()
    rollouts = pl.DataFrame({"rollout_index": list(range(rollout_count))}, schema={"rollout_index": pl.Int64()})
    return EVENT_FRAMES.property_purchases.concat(
        [_property_purchase_block_per_rollout(purchase, rollouts, month) for purchase in purchases]
    )


def _property_purchase_block_per_rollout(
    purchase: ScheduledPropertyPurchase, rollouts: pl.DataFrame, month: int
) -> pl.DataFrame:
    mortgage_principal = purchase.mortgage.principal_usd if purchase.mortgage is not None else 0.0
    return rollouts.with_columns(
        pl.lit(month, dtype=pl.Int64()).alias("month_index"),
        pl.lit(purchase.cause_id, dtype=pl.Utf8()).alias("cause_id"),
        pl.lit(purchase.property_id, dtype=pl.Utf8()).alias("property_id"),
        pl.lit(purchase.location_id, dtype=pl.Utf8()).alias("location_id"),
        pl.lit(purchase.buyer_agent_id, dtype=pl.Utf8()).alias("buyer_agent_id"),
        pl.lit(purchase.purchase_price_usd, dtype=pl.Float64()).alias("purchase_price_usd"),
        pl.lit(purchase.buyer_closing_cost_usd, dtype=pl.Float64()).alias("closing_cost_usd"),
        pl.lit(purchase.purchase_price_usd + purchase.buyer_closing_cost_usd, dtype=pl.Float64()).alias(
            "adjusted_basis_usd"
        ),
        pl.lit(purchase.ownership_pct, dtype=pl.Float64()).alias("ownership_pct"),
        pl.lit(purchase.down_payment_usd + purchase.buyer_closing_cost_usd, dtype=pl.Float64()).alias(
            "stake_contribution_usd"
        ),
        pl.lit(purchase.purchase_price_usd - mortgage_principal, dtype=pl.Float64()).alias("equity_ledger_usd"),
    ).pipe(EVENT_FRAMES.property_purchases.normalize)


def _emit_mortgage_originations(scenario: Scenario, month: int, rollout_count: int) -> pl.DataFrame:
    purchases = [
        purchase
        for purchase in scenario.scheduled_property_purchases
        if purchase.month == month and purchase.mortgage is not None
    ]
    if not purchases:
        return EVENT_FRAMES.mortgage_originations.empty()
    rollouts = pl.DataFrame({"rollout_index": list(range(rollout_count))}, schema={"rollout_index": pl.Int64()})
    return EVENT_FRAMES.mortgage_originations.concat(
        [_mortgage_origination_block_per_rollout(purchase, rollouts, month) for purchase in purchases]
    )


def _mortgage_origination_block_per_rollout(
    purchase: ScheduledPropertyPurchase, rollouts: pl.DataFrame, month: int
) -> pl.DataFrame:
    mortgage = purchase.mortgage
    if mortgage is None:
        raise ValueError("_mortgage_origination_block_per_rollout requires mortgage terms")
    return rollouts.with_columns(
        pl.lit(month, dtype=pl.Int64()).alias("month_index"),
        pl.lit(f"{purchase.cause_id}_mortgage_origination", dtype=pl.Utf8()).alias("cause_id"),
        pl.lit(mortgage.liability_id, dtype=pl.Utf8()).alias("liability_id"),
        pl.lit(purchase.buyer_agent_id, dtype=pl.Utf8()).alias("agent_id"),
        pl.lit(purchase.buyer_account_id, dtype=pl.Utf8()).alias("payment_account_id"),
        pl.lit(mortgage.lender_agent_id, dtype=pl.Utf8()).alias("counterparty_agent_id"),
        pl.lit(mortgage.lender_account_id, dtype=pl.Utf8()).alias("counterparty_account_id"),
        pl.lit(purchase.property_id, dtype=pl.Utf8()).alias("property_id"),
        pl.lit(mortgage.principal_usd, dtype=pl.Float64()).alias("principal_usd"),
        pl.lit(mortgage.annual_interest_rate, dtype=pl.Float64()).alias("annual_interest_rate"),
        pl.lit(int(mortgage.term_months), dtype=pl.Int64()).alias("term_months"),
        pl.lit(
            _mortgage_monthly_payment_usd(mortgage.principal_usd, mortgage.annual_interest_rate, mortgage.term_months),
            dtype=pl.Float64(),
        ).alias("monthly_payment_usd"),
    ).pipe(EVENT_FRAMES.mortgage_originations.normalize)


def _mortgage_monthly_payment_usd(principal_usd: float, annual_interest_rate: float, term_months: int) -> float:
    monthly_rate = annual_interest_rate / 12.0
    if monthly_rate == 0:
        return principal_usd / term_months
    return principal_usd * monthly_rate / (1.0 - (1.0 + monthly_rate) ** -term_months)


def _property_purchase_transfer_events(scenario: Scenario, purchases: pl.DataFrame) -> pl.DataFrame:
    if purchases.is_empty():
        return EVENT_FRAMES.transfers.empty()
    blocks = []
    for purchase in scenario.scheduled_property_purchases:
        purchase_rows = purchases.filter(pl.col("cause_id") == purchase.cause_id)
        if purchase_rows.is_empty():
            continue
        amount = purchase.down_payment_usd + purchase.buyer_closing_cost_usd
        if amount <= 0:
            continue
        blocks.append(
            purchase_rows.with_columns(
                pl.lit(f"{purchase.cause_id}_buyer_cash", dtype=pl.Utf8()).alias("cause_id"),
                pl.lit(purchase.buyer_agent_id, dtype=pl.Utf8()).alias("from_agent_id"),
                pl.lit(purchase.buyer_account_id, dtype=pl.Utf8()).alias("from_account_id"),
                pl.lit(purchase.seller_agent_id, dtype=pl.Utf8()).alias("to_agent_id"),
                pl.lit(purchase.seller_account_id, dtype=pl.Utf8()).alias("to_account_id"),
                pl.lit(amount, dtype=pl.Float64()).alias("amount_usd"),
                pl.lit(None, dtype=pl.Utf8()).alias("income_category"),
            ).pipe(EVENT_FRAMES.transfers.normalize)
        )
    return EVENT_FRAMES.transfers.concat(blocks)


def _emit_lot_dispositions(
    state: StateCrossSection, scenario: Scenario, market: MarketContext, month: int
) -> pl.DataFrame:
    """Emit `LotDisposition` rows for every scheduled asset sale at
    this month. Each sale is FIFO-resolved against the agent's
    current lots of the asset; the same resolution applies
    per-rollout via polars window functions over `rollout_index`.

    When the sale supplies an explicit `price_per_unit_usd` that
    price applies uniformly across rollouts; otherwise the price
    comes from the exogenous trajectory bundle's per-rollout
    per-month curve."""
    sales = [s for s in scenario.scheduled_asset_sales if s.month == month]
    if not sales:
        return EVENT_FRAMES.lot_dispositions.empty()
    prices_at_month = market.prices_at(month)
    blocks = [_fifo_dispositions_for_sale(state, sale, prices_at_month, month) for sale in sales]
    return EVENT_FRAMES.lot_dispositions.concat(blocks)


def _fifo_dispositions_for_sale(
    state: StateCrossSection, sale: ScheduledAssetSale, prices_at_month: pl.DataFrame, month: int
) -> pl.DataFrame:
    """Vectorized FIFO consumption of one sale across all rollouts.

    Within each rollout the lots of the matching `(agent_id,
    asset_id)` are ordered by `purchase_month_index` ascending; the
    sale eats from the oldest forward. A lot's `units_sold` is
    `clip(sale.quantity - prev_cumulative_remaining, 0,
    remaining_quantity)`. The result is one disposition row per
    consumed lot per rollout.

    Pricing: if `sale.price_per_unit_usd` is set it's used as a
    scalar; otherwise `prices_at_month` is joined by
    `(rollout_index, asset_id)` so each rollout gets its own
    market-derived price."""
    candidates = state.asset_lots.filter(
        (pl.col("agent_id") == sale.agent_id)
        & (pl.col("asset_id") == sale.asset_id)
        & (pl.col("remaining_quantity") > 0)
    )
    if candidates.is_empty():
        return EVENT_FRAMES.lot_dispositions.empty()
    priced = _attach_unit_price(candidates, sale, prices_at_month)
    ordered = priced.sort(["rollout_index", "purchase_month_index", "lot_id"])
    with_cum = ordered.with_columns(
        _prev_cum_remaining=(
            pl.col("remaining_quantity").cum_sum().over("rollout_index") - pl.col("remaining_quantity")
        )
    )
    sized = with_cum.with_columns(
        _units_from_lot=pl.min_horizontal(
            pl.col("remaining_quantity"),
            pl.max_horizontal(pl.lit(0.0), pl.lit(sale.quantity) - pl.col("_prev_cum_remaining")),
        )
    )
    consumed = sized.filter(pl.col("_units_from_lot") > 0)
    if consumed.is_empty():
        return EVENT_FRAMES.lot_dispositions.empty()
    return consumed.with_columns(
        pl.lit(month, dtype=pl.Int64()).alias("month_index"),
        pl.lit(sale.cause_id, dtype=pl.Utf8()).alias("cause_id"),
        pl.col("_units_from_lot").alias("units_sold"),
        (pl.col("_units_from_lot") * pl.col("cost_basis_per_unit_usd")).alias("cost_basis_consumed_usd"),
        (pl.col("_units_from_lot") * pl.col("_unit_price")).alias("proceeds_usd"),
        pl.lit(sale.proceeds_account_id, dtype=pl.Utf8()).alias("proceeds_account_id"),
    ).pipe(EVENT_FRAMES.lot_dispositions.normalize)


def _attach_unit_price(lots: pl.DataFrame, sale: ScheduledAssetSale, prices_at_month: pl.DataFrame) -> pl.DataFrame:
    """Add a `_unit_price` column to the candidate lots. Scalar
    price (configured on the sale) is broadcast across rollouts;
    market-derived price is joined per `(rollout_index, asset_id)`."""
    if sale.price_per_unit_usd is not None:
        return lots.with_columns(pl.lit(sale.price_per_unit_usd, dtype=pl.Float64()).alias("_unit_price"))
    prices_for_asset = prices_at_month.filter(pl.col("asset_id") == sale.asset_id).rename(
        {"price_per_unit_usd": "_unit_price"}
    )
    return lots.join(prices_for_asset.select("rollout_index", "_unit_price"), on="rollout_index", how="left")


def _is_year_end(month: int) -> bool:
    """Tax years are calendar-year-aligned at spike 1: the year
    ends at month index 11, 23, 35, …"""
    return month % 12 == 11


def _emit_year_end_tax_events(
    *,
    state: StateCrossSection,
    scenario: Scenario,
    jurisdictions: dict[str, Jurisdiction],
    month: int,
    transfers: pl.DataFrame,
    dispositions: pl.DataFrame,
) -> _TaxYearEvents:
    """At year-end emit tax accruals plus audit breakdown rows.

    Federal tax = ordinary_bracket_walk (ordinary_income + STCG -
    std_ded) + LTCG_bracket_walk(LTCG stacked above
    ordinary_taxable). California tax = ordinary bracket walk on
    (ordinary_income + LTCG + STCG - std_ded) because CA does not
    have a separate LTCG schedule.

    Like ordinary income, capital gains are summed as `state YTD +
    this-month's dispositions` since `apply_events` will produce
    that same YTD before the year closes."""
    if not _is_year_end(month) or not scenario.tax_profiles:
        return _empty_tax_year_events()
    eoy = _compute_end_of_year_taxable_components(state, transfers, dispositions, scenario.tax_profiles, month)
    events = [_tax_events_for_profile(profile, eoy, jurisdictions, month) for profile in scenario.tax_profiles]
    accrual_blocks = [event.accruals for event in events]
    breakdown_blocks = [event.breakdowns for event in events]
    return _TaxYearEvents(
        accruals=EVENT_FRAMES.tax_accruals.concat(accrual_blocks),
        breakdowns=EVENT_FRAMES.tax_breakdowns.concat(breakdown_blocks),
    )


def _empty_tax_year_events() -> _TaxYearEvents:
    return _TaxYearEvents(accruals=EVENT_FRAMES.tax_accruals.empty(), breakdowns=EVENT_FRAMES.tax_breakdowns.empty())


def _compute_end_of_year_taxable_components(
    state: StateCrossSection,
    transfers: pl.DataFrame,
    dispositions: pl.DataFrame,
    profiles: list[TaxProfile],
    month: int,
) -> pl.DataFrame:
    """Return one row per (rollout, agent) with columns
    `ordinary_income_usd`, `ltcg_usd`, `stcg_usd` — the end-of-year
    totals matching what apply_events will materialize. Computed
    as `pre-month state YTD + this-month's emitted events` so the
    step can compose the tax accrual without round-tripping."""
    taxed_agents = [p.agent_id for p in profiles]
    pre_ord = state.ordinary_income_ytd.filter(pl.col("agent_id").is_in(taxed_agents))
    pre_cg = state.capital_gains_ytd.filter(pl.col("agent_id").is_in(taxed_agents))
    pre_ltcg = pre_cg.filter(pl.col("classification") == "ltcg").select(
        "rollout_index", "agent_id", pl.col("gain_usd").alias("ltcg_usd")
    )
    pre_stcg = pre_cg.filter(pl.col("classification") == "stcg").select(
        "rollout_index", "agent_id", pl.col("gain_usd").alias("stcg_usd")
    )
    this_month_ord = (
        transfers.filter((pl.col("income_category") == "ordinary") & pl.col("to_agent_id").is_in(taxed_agents))
        .group_by(["rollout_index", "to_agent_id"])
        .agg(pl.col("amount_usd").sum().alias("_this_month_ord"))
        .rename({"to_agent_id": "agent_id"})
    )
    classified_dispositions = dispositions.filter(pl.col("agent_id").is_in(taxed_agents)).with_columns(
        gain_usd=pl.col("proceeds_usd") - pl.col("cost_basis_consumed_usd"),
        is_ltcg=(pl.lit(month) - pl.col("purchase_month_index")) >= 12,
    )
    this_month_ltcg = (
        classified_dispositions.filter(pl.col("is_ltcg"))
        .group_by(["rollout_index", "agent_id"])
        .agg(pl.col("gain_usd").sum().alias("_this_month_ltcg"))
    )
    this_month_stcg = (
        classified_dispositions.filter(~pl.col("is_ltcg"))
        .group_by(["rollout_index", "agent_id"])
        .agg(pl.col("gain_usd").sum().alias("_this_month_stcg"))
    )
    return (
        pre_ord.join(this_month_ord, on=["rollout_index", "agent_id"], how="left")
        .with_columns(ordinary_income_usd=pl.col("ordinary_income_usd") + pl.col("_this_month_ord").fill_null(0.0))
        .drop("_this_month_ord")
        .join(pre_ltcg, on=["rollout_index", "agent_id"], how="left")
        .join(this_month_ltcg, on=["rollout_index", "agent_id"], how="left")
        .with_columns(ltcg_usd=pl.col("ltcg_usd").fill_null(0.0) + pl.col("_this_month_ltcg").fill_null(0.0))
        .drop("_this_month_ltcg")
        .join(pre_stcg, on=["rollout_index", "agent_id"], how="left")
        .join(this_month_stcg, on=["rollout_index", "agent_id"], how="left")
        .with_columns(stcg_usd=pl.col("stcg_usd").fill_null(0.0) + pl.col("_this_month_stcg").fill_null(0.0))
        .drop("_this_month_stcg")
    )


def _tax_events_for_profile(
    profile: TaxProfile, eoy: pl.DataFrame, jurisdictions: dict[str, Jurisdiction], month: int
) -> _TaxYearEvents:
    """Compute accrual and breakdown rows for one tax profile."""
    eoy_rows = eoy.filter(pl.col("agent_id") == profile.agent_id).sort("rollout_index")
    if eoy_rows.is_empty():
        return _empty_tax_year_events()
    rollout_idx = eoy_rows.get_column("rollout_index").to_numpy()
    ordinary = eoy_rows.get_column("ordinary_income_usd").to_numpy()
    ltcg = eoy_rows.get_column("ltcg_usd").to_numpy()
    stcg = eoy_rows.get_column("stcg_usd").to_numpy()
    accrual_blocks = []
    breakdown_blocks = []
    for jurisdiction_id in profile.jurisdiction_ids:
        jurisdiction = jurisdictions[jurisdiction_id]
        deduction = jurisdiction.standard_deduction[profile.filing_status]
        ord_brackets = jurisdiction.ordinary_income_brackets[profile.filing_status]
        if jurisdiction.ltcg_brackets is not None:
            ltcg_brackets = jurisdiction.ltcg_brackets[profile.filing_status]
            ordinary_taxable = np.maximum(ordinary + stcg - deduction, 0.0)
            capital_gain_taxable = ltcg
            ordinary_tax = apply_brackets(ordinary_taxable, ord_brackets)
            capital_gain_tax = apply_ltcg_brackets(ltcg, ordinary_taxable, ltcg_brackets)
            tax = ordinary_tax + capital_gain_tax
        else:
            ordinary_taxable = np.maximum(ordinary + ltcg + stcg - deduction, 0.0)
            capital_gain_taxable = np.zeros_like(ordinary)
            ordinary_tax = apply_brackets(ordinary_taxable, ord_brackets)
            capital_gain_tax = np.zeros_like(ordinary)
            tax = ordinary_tax
        cause_id = f"{profile.agent_id}_{jurisdiction_id}_year_end_accrual_m{month}"
        accrual_blocks.append(
            pl.DataFrame(
                {
                    "rollout_index": rollout_idx,
                    "month_index": np.full_like(rollout_idx, month),
                    "cause_id": [cause_id] * len(rollout_idx),
                    "agent_id": [profile.agent_id] * len(rollout_idx),
                    "jurisdiction_id": [jurisdiction_id] * len(rollout_idx),
                    "tax_year_end_month": np.full_like(rollout_idx, month),
                    "amount_usd": tax,
                },
                schema=EVENT_FRAMES.tax_accruals.schema,
            )
        )
        breakdown_blocks.append(
            pl.DataFrame(
                {
                    "rollout_index": rollout_idx,
                    "month_index": np.full_like(rollout_idx, month),
                    "cause_id": [cause_id] * len(rollout_idx),
                    "agent_id": [profile.agent_id] * len(rollout_idx),
                    "jurisdiction_id": [jurisdiction_id] * len(rollout_idx),
                    "tax_year_end_month": np.full_like(rollout_idx, month),
                    "ordinary_income_usd": ordinary,
                    "ltcg_usd": ltcg,
                    "stcg_usd": stcg,
                    "standard_deduction_usd": np.full(len(rollout_idx), deduction, dtype=float),
                    "ordinary_taxable_usd": ordinary_taxable,
                    "capital_gain_taxable_usd": capital_gain_taxable,
                    "ordinary_tax_usd": ordinary_tax,
                    "capital_gain_tax_usd": capital_gain_tax,
                    "total_tax_usd": tax,
                },
                schema=EVENT_FRAMES.tax_breakdowns.schema,
            )
        )
    return _TaxYearEvents(
        accruals=EVENT_FRAMES.tax_accruals.concat(accrual_blocks),
        breakdowns=EVENT_FRAMES.tax_breakdowns.concat(breakdown_blocks),
    )
