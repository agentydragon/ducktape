"""`step_emit_events` — pure function that reads state + scenario +
month index and returns the events for that month.

At spike 1 step 7 the step emits:

  - transfer events for scheduled + recurring transfers active at
    this month, optionally tagged with an `income_category`;
  - lot_disposition events for scheduled asset sales (FIFO across
    the agent's lots);
  - tax_accrual events at the end of each tax year (month_index
    in {11, 23, 35, ...}) — one per (taxed agent, jurisdiction),
    computed by bracket-walking end-of-year ordinary income minus
    the jurisdiction's standard deduction.

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

from augur.sim.events import (
    ASSET_PURCHASE_EVENT_SCHEMA,
    LOT_DISPOSITION_EVENT_SCHEMA,
    ROLLOUT_FAILURE_EVENT_SCHEMA,
    TAX_ACCRUAL_EVENT_SCHEMA,
    TRANSFER_EVENT_SCHEMA,
    EventLog,
)
from augur.sim.jurisdictions import Jurisdiction
from augur.sim.market import MarketContext
from augur.sim.scenario import (
    FloorTriggeredSalePolicy,
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    ScheduledTransfer,
    TaxProfile,
)
from augur.sim.state import StateCrossSection
from augur.sim.tax import apply_brackets, apply_ltcg_brackets


def step_emit_scheduled_events(
    *,
    state: StateCrossSection,
    scenario: Scenario,
    market: MarketContext,
    jurisdictions: dict[str, Jurisdiction],
    month: int,
    rollout_count: int,
) -> EventLog:
    """Phase 1 of the month step: scheduled / recurring transfers,
    scheduled asset sales, and year-end tax accruals. Pure: does
    not mutate `state`."""
    transfers = _emit_transfers(scenario, month, rollout_count)
    dispositions = _emit_lot_dispositions(state, scenario, market, month)
    tax_accruals = _emit_year_end_tax_accruals(
        state=state,
        scenario=scenario,
        jurisdictions=jurisdictions,
        month=month,
        transfers=transfers,
        dispositions=dispositions,
    )
    return EventLog(
        transfers=transfers,
        asset_purchases=pl.DataFrame(schema=ASSET_PURCHASE_EVENT_SCHEMA),
        lot_dispositions=dispositions,
        tax_accruals=tax_accruals,
        rollout_failures=pl.DataFrame(schema=ROLLOUT_FAILURE_EVENT_SCHEMA),
    )


def step_emit_policy_events(
    *, state: StateCrossSection, scenario: Scenario, market: MarketContext, month: int
) -> EventLog:
    """Phase 2 of the month step: discretionary policies +
    failure detection. Runs on the post-phase-1 state so the floor
    check sees cash after all scheduled events have applied."""
    dispositions = _emit_floor_triggered_sales(state, scenario, market, month)
    failures = _emit_rollout_failures(state, scenario, dispositions, market, month)
    return EventLog(
        transfers=pl.DataFrame(schema=TRANSFER_EVENT_SCHEMA),
        asset_purchases=pl.DataFrame(schema=ASSET_PURCHASE_EVENT_SCHEMA),
        lot_dispositions=dispositions,
        tax_accruals=pl.DataFrame(schema=TAX_ACCRUAL_EVENT_SCHEMA),
        rollout_failures=failures,
    )


def _emit_transfers(scenario: Scenario, month: int, rollout_count: int) -> pl.DataFrame:
    """Emit Transfer event rows for every scheduled or recurring
    transfer active at this month. Scheduled transfers fire only at
    their configured month; recurring transfers fire every month in
    `[start_month, end_month]` (or through horizon end). One row per
    (transfer, rollout)."""
    active: list[ScheduledTransfer | RecurringTransfer] = [t for t in scenario.scheduled_transfers if t.month == month]
    active.extend(t for t in scenario.recurring_transfers if t.is_active_at(month))
    if not active:
        return pl.DataFrame(schema=TRANSFER_EVENT_SCHEMA)
    rollouts = pl.DataFrame({"rollout_index": list(range(rollout_count))}, schema={"rollout_index": pl.Int64()})
    blocks = [_transfer_block_per_rollout(t, rollouts, month) for t in active]
    return pl.concat(blocks).select(list(TRANSFER_EVENT_SCHEMA.keys()))


def _transfer_block_per_rollout(
    t: ScheduledTransfer | RecurringTransfer, rollouts: pl.DataFrame, month: int
) -> pl.DataFrame:
    """One row per rollout for one transfer config. The rollout
    dimension is expanded vectorized — no Python loop over rollouts.
    Handles both ScheduledTransfer (one-off at a specific month) and
    RecurringTransfer (firing at this active month) — same event
    schema, only the cadence config differs."""
    return rollouts.with_columns(
        pl.lit(month, dtype=pl.Int64()).alias("month_index"),
        pl.lit(t.cause_id, dtype=pl.Utf8()).alias("cause_id"),
        pl.lit(t.from_agent_id, dtype=pl.Utf8()).alias("from_agent_id"),
        pl.lit(t.from_account_id, dtype=pl.Utf8()).alias("from_account_id"),
        pl.lit(t.to_agent_id, dtype=pl.Utf8()).alias("to_agent_id"),
        pl.lit(t.to_account_id, dtype=pl.Utf8()).alias("to_account_id"),
        pl.lit(t.amount_usd, dtype=pl.Float64()).alias("amount_usd"),
        pl.lit(t.income_category, dtype=pl.Utf8()).alias("income_category"),
    )


def _emit_lot_dispositions(
    state: StateCrossSection, scenario: Scenario, market: MarketContext, month: int
) -> pl.DataFrame:
    """Emit `LotDisposition` rows for every scheduled asset sale at
    this month. Each sale is FIFO-resolved against the agent's
    current lots of the asset; the same resolution applies
    per-rollout via polars window functions over `rollout_index`.

    When the sale supplies an explicit `price_per_unit_usd` that
    price applies uniformly across rollouts; otherwise the price
    comes from the market bundle's per-rollout per-month curve."""
    sales = [s for s in scenario.scheduled_asset_sales if s.month == month]
    if not sales:
        return pl.DataFrame(schema=LOT_DISPOSITION_EVENT_SCHEMA)
    prices_at_month = market.prices_at(month)
    blocks = [_fifo_dispositions_for_sale(state, sale, prices_at_month, month) for sale in sales]
    blocks = [b for b in blocks if not b.is_empty()]
    if not blocks:
        return pl.DataFrame(schema=LOT_DISPOSITION_EVENT_SCHEMA)
    return pl.concat(blocks).select(list(LOT_DISPOSITION_EVENT_SCHEMA.keys()))


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
        return pl.DataFrame(schema=LOT_DISPOSITION_EVENT_SCHEMA)
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
        return pl.DataFrame(schema=LOT_DISPOSITION_EVENT_SCHEMA)
    return consumed.with_columns(
        pl.lit(month, dtype=pl.Int64()).alias("month_index"),
        pl.lit(sale.cause_id, dtype=pl.Utf8()).alias("cause_id"),
        pl.col("_units_from_lot").alias("units_sold"),
        (pl.col("_units_from_lot") * pl.col("cost_basis_per_unit_usd")).alias("cost_basis_consumed_usd"),
        (pl.col("_units_from_lot") * pl.col("_unit_price")).alias("proceeds_usd"),
        pl.lit(sale.proceeds_account_id, dtype=pl.Utf8()).alias("proceeds_account_id"),
    ).select(list(LOT_DISPOSITION_EVENT_SCHEMA.keys()))


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


def _emit_floor_triggered_sales(
    state: StateCrossSection, scenario: Scenario, market: MarketContext, month: int
) -> pl.DataFrame:
    """For every `FloorTriggeredSalePolicy`, find rollouts whose
    monitored account is below the floor and emit FIFO lot
    dispositions walking the asset preference chain until either
    the deficit is covered or the chain is exhausted. Vectorized
    over rollouts; Python-loops only over asset slots (typically
    3-5 entries)."""
    if not scenario.floor_triggered_sale_policies:
        return pl.DataFrame(schema=LOT_DISPOSITION_EVENT_SCHEMA)
    prices = market.prices_at(month)
    active_rollouts = state.rollout_status.filter(pl.col("status") == "active").select("rollout_index")
    blocks: list[pl.DataFrame] = []
    for policy in scenario.floor_triggered_sale_policies:
        blocks.extend(_dispositions_for_policy(state, policy, prices, active_rollouts, month))
    blocks = [b for b in blocks if not b.is_empty()]
    if not blocks:
        return pl.DataFrame(schema=LOT_DISPOSITION_EVENT_SCHEMA)
    return pl.concat(blocks).select(list(LOT_DISPOSITION_EVENT_SCHEMA.keys()))


def _dispositions_for_policy(
    state: StateCrossSection,
    policy: FloorTriggeredSalePolicy,
    prices: pl.DataFrame,
    active_rollouts: pl.DataFrame,
    month: int,
) -> list[pl.DataFrame]:
    """Resolve one policy across the rollout column. Returns a list
    of disposition blocks (one per asset slot consumed)."""
    cash = state.cash_balances.filter(
        (pl.col("agent_id") == policy.agent_id) & (pl.col("account_id") == policy.account_id)
    ).select("rollout_index", pl.col("balance_usd").alias("_balance_usd"))
    deficit = (
        cash.join(active_rollouts, on="rollout_index", how="inner")
        .with_columns(
            _remaining_deficit_usd=pl.lit(policy.floor_usd + policy.replenish_buffer_usd) - pl.col("_balance_usd")
        )
        .filter(pl.col("_remaining_deficit_usd") > 0)
        .select("rollout_index", "_remaining_deficit_usd")
    )
    if deficit.is_empty():
        return []
    blocks: list[pl.DataFrame] = []
    for slot_index, asset_id in enumerate(policy.asset_preference_chain):
        if deficit.is_empty():
            break
        cause_id = f"{policy.cause_id_prefix}_m{month}_{asset_id}"
        result = _consume_asset_for_policy(
            state=state,
            policy=policy,
            asset_id=asset_id,
            slot_index=slot_index,
            prices=prices,
            deficit=deficit,
            cause_id=cause_id,
            month=month,
        )
        if result.dispositions is not None and not result.dispositions.is_empty():
            blocks.append(result.dispositions)
        deficit = result.remaining_deficit
    return blocks


@dataclass(frozen=True)
class _PolicyAssetResult:
    """Output of consuming one asset within a policy: the lot
    dispositions emitted for the asset plus the residual deficit
    after this asset's sale (to feed the next asset slot)."""

    dispositions: pl.DataFrame | None
    remaining_deficit: pl.DataFrame


def _consume_asset_for_policy(
    *,
    state: StateCrossSection,
    policy: FloorTriggeredSalePolicy,
    asset_id: str,
    slot_index: int,
    prices: pl.DataFrame,
    deficit: pl.DataFrame,
    cause_id: str,
    month: int,
) -> _PolicyAssetResult:
    """Walk the agent's lots of `asset_id` in FIFO order to cover
    as much of each rollout's remaining deficit as the asset
    supports. Decrement the deficit by the dollars actually
    realized. Returns the new disposition frame and an updated
    deficit frame for the next asset slot."""
    asset_price = prices.filter(pl.col("asset_id") == asset_id).select(
        "rollout_index", pl.col("price_per_unit_usd").alias("_unit_price")
    )
    lots = (
        state.asset_lots.filter(
            (pl.col("agent_id") == policy.agent_id)
            & (pl.col("asset_id") == asset_id)
            & (pl.col("remaining_quantity") > 0)
        )
        .join(asset_price, on="rollout_index", how="left")
        .join(deficit, on="rollout_index", how="inner")
    )
    if lots.is_empty():
        return _PolicyAssetResult(dispositions=None, remaining_deficit=deficit)
    ordered = lots.sort(["rollout_index", "purchase_month_index", "lot_id"])
    with_cum = ordered.with_columns(
        _prev_cum_dollars=(
            (pl.col("remaining_quantity") * pl.col("_unit_price")).cum_sum().over("rollout_index")
            - (pl.col("remaining_quantity") * pl.col("_unit_price"))
        )
    )
    sized = with_cum.with_columns(
        _dollars_from_lot=pl.min_horizontal(
            pl.col("remaining_quantity") * pl.col("_unit_price"),
            pl.max_horizontal(pl.lit(0.0), pl.col("_remaining_deficit_usd") - pl.col("_prev_cum_dollars")),
        )
    )
    consumed = sized.filter(pl.col("_dollars_from_lot") > 0).with_columns(
        _units_from_lot=pl.col("_dollars_from_lot") / pl.col("_unit_price")
    )
    if consumed.is_empty():
        return _PolicyAssetResult(dispositions=None, remaining_deficit=deficit)
    dispositions = consumed.with_columns(
        pl.lit(month, dtype=pl.Int64()).alias("month_index"),
        pl.lit(cause_id, dtype=pl.Utf8()).alias("cause_id"),
        pl.col("_units_from_lot").alias("units_sold"),
        (pl.col("_units_from_lot") * pl.col("cost_basis_per_unit_usd")).alias("cost_basis_consumed_usd"),
        pl.col("_dollars_from_lot").alias("proceeds_usd"),
        pl.lit(policy.account_id, dtype=pl.Utf8()).alias("proceeds_account_id"),
    ).select(list(LOT_DISPOSITION_EVENT_SCHEMA.keys()))
    _ = slot_index  # reserved for future use (e.g. partial-fill accounting)
    realized_per_rollout = consumed.group_by("rollout_index").agg(
        pl.col("_dollars_from_lot").sum().alias("_realized_usd")
    )
    new_deficit = (
        deficit.join(realized_per_rollout, on="rollout_index", how="left")
        .with_columns(_remaining_deficit_usd=pl.col("_remaining_deficit_usd") - pl.col("_realized_usd").fill_null(0.0))
        .filter(pl.col("_remaining_deficit_usd") > 0)
        .select("rollout_index", "_remaining_deficit_usd")
    )
    return _PolicyAssetResult(dispositions=dispositions, remaining_deficit=new_deficit)


def _emit_rollout_failures(
    state: StateCrossSection, scenario: Scenario, policy_dispositions: pl.DataFrame, market: MarketContext, month: int
) -> pl.DataFrame:
    """Flag any active rollout whose monitored cash account is
    still below 0 after the floor-triggered sales have fired. Cash
    on the post-phase-1 state is in `state.cash_balances`; the
    policy dispositions emitted this phase haven't been applied
    yet, so we credit them in projection before checking the
    floor."""
    _ = market
    if not scenario.floor_triggered_sale_policies:
        return pl.DataFrame(schema=ROLLOUT_FAILURE_EVENT_SCHEMA)
    active_rollouts = state.rollout_status.filter(pl.col("status") == "active").select("rollout_index")
    blocks: list[pl.DataFrame] = []
    for policy in scenario.floor_triggered_sale_policies:
        policy_proceeds = (
            policy_dispositions.filter(
                (pl.col("agent_id") == policy.agent_id) & (pl.col("proceeds_account_id") == policy.account_id)
            )
            .group_by("rollout_index")
            .agg(pl.col("proceeds_usd").sum().alias("_proceeds"))
        )
        pre_failure_cash = (
            state.cash_balances.filter(
                (pl.col("agent_id") == policy.agent_id) & (pl.col("account_id") == policy.account_id)
            )
            .select("rollout_index", pl.col("balance_usd").alias("_balance"))
            .join(policy_proceeds, on="rollout_index", how="left")
            .with_columns(_projected=pl.col("_balance") + pl.col("_proceeds").fill_null(0.0))
            .join(active_rollouts, on="rollout_index", how="inner")
            .filter(pl.col("_projected") < 0)
        )
        if pre_failure_cash.is_empty():
            continue
        blocks.append(
            pre_failure_cash.with_columns(
                pl.lit(month, dtype=pl.Int64()).alias("month_index"),
                pl.lit(f"{policy.cause_id_prefix}_failure_m{month}", dtype=pl.Utf8()).alias("cause_id"),
                pl.lit(policy.agent_id, dtype=pl.Utf8()).alias("agent_id"),
                deficit_usd=-pl.col("_projected"),
            ).select(list(ROLLOUT_FAILURE_EVENT_SCHEMA.keys()))
        )
    if not blocks:
        return pl.DataFrame(schema=ROLLOUT_FAILURE_EVENT_SCHEMA)
    return pl.concat(blocks)


def _is_year_end(month: int) -> bool:
    """Tax years are calendar-year-aligned at spike 1: the year
    ends at month index 11, 23, 35, …"""
    return month % 12 == 11


def _emit_year_end_tax_accruals(
    *,
    state: StateCrossSection,
    scenario: Scenario,
    jurisdictions: dict[str, Jurisdiction],
    month: int,
    transfers: pl.DataFrame,
    dispositions: pl.DataFrame,
) -> pl.DataFrame:
    """At year-end emit one `tax_accrual` row per (taxed agent,
    jurisdiction, rollout). Federal tax = ordinary_bracket_walk
    (ordinary_income + STCG  - std_ded) + LTCG_bracket_walk(LTCG
    stacked above ordinary_taxable). California tax = ordinary
    bracket walk on (ordinary_income + LTCG + STCG  - std_ded) —
    CA does not have a separate LTCG schedule.

    Like ordinary income, capital gains are summed as `state YTD +
    this-month's dispositions` since `apply_events` will produce
    that same YTD before the year closes."""
    if not _is_year_end(month) or not scenario.tax_profiles:
        return pl.DataFrame(schema=TAX_ACCRUAL_EVENT_SCHEMA)
    eoy = _compute_end_of_year_taxable_components(state, transfers, dispositions, scenario.tax_profiles, month)
    blocks = [_tax_accruals_for_profile(profile, eoy, jurisdictions, month) for profile in scenario.tax_profiles]
    blocks = [b for b in blocks if not b.is_empty()]
    if not blocks:
        return pl.DataFrame(schema=TAX_ACCRUAL_EVENT_SCHEMA)
    return pl.concat(blocks).select(list(TAX_ACCRUAL_EVENT_SCHEMA.keys()))


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


def _tax_accruals_for_profile(
    profile: TaxProfile, eoy: pl.DataFrame, jurisdictions: dict[str, Jurisdiction], month: int
) -> pl.DataFrame:
    """Compute one row per jurisdiction in the profile. Federal vs
    California is distinguished by whether the jurisdiction has its
    own LTCG schedule: federal stacks LTCG above ordinary+STCG;
    California flattens everything into ordinary brackets."""
    eoy_rows = eoy.filter(pl.col("agent_id") == profile.agent_id).sort("rollout_index")
    if eoy_rows.is_empty():
        return pl.DataFrame(schema=TAX_ACCRUAL_EVENT_SCHEMA)
    rollout_idx = eoy_rows.get_column("rollout_index").to_numpy()
    ordinary = eoy_rows.get_column("ordinary_income_usd").to_numpy()
    ltcg = eoy_rows.get_column("ltcg_usd").to_numpy()
    stcg = eoy_rows.get_column("stcg_usd").to_numpy()
    blocks = []
    for jurisdiction_id in profile.jurisdiction_ids:
        jurisdiction = jurisdictions[jurisdiction_id]
        deduction = jurisdiction.standard_deduction[profile.filing_status]
        ord_brackets = jurisdiction.ordinary_income_brackets[profile.filing_status]
        if jurisdiction.ltcg_brackets is not None:
            ltcg_brackets = jurisdiction.ltcg_brackets[profile.filing_status]
            ordinary_taxable = np.maximum(ordinary + stcg - deduction, 0.0)
            tax = apply_brackets(ordinary_taxable, ord_brackets) + apply_ltcg_brackets(
                ltcg, ordinary_taxable, ltcg_brackets
            )
        else:
            total_taxable = np.maximum(ordinary + ltcg + stcg - deduction, 0.0)
            tax = apply_brackets(total_taxable, ord_brackets)
        cause_id = f"{profile.agent_id}_{jurisdiction_id}_year_end_accrual_m{month}"
        blocks.append(
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
                schema=TAX_ACCRUAL_EVENT_SCHEMA,
            )
        )
    return pl.concat(blocks)
