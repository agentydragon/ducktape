"""`step_emit_events` — pure function that reads state + scenario +
month index and returns the events for that month.

At spike 1 step 4 the step emits:

  - transfer events for scheduled + recurring transfers active at
    this month;
  - lot_disposition events for scheduled asset sales active at this
    month — FIFO across the agent's lots of the asset, vectorized
    over the rollout dimension.

Initial holdings are seeded into `state.asset_lots` at _initial_state
time, not via in-sim AssetPurchase events. In-sim purchases (a later
layer) will emit AssetPurchase events here. The step does not mutate
`state`. The simulate loop calls `apply_events(state, step_result)`
separately.
"""

from __future__ import annotations

import polars as pl

from augur.sim.events import ASSET_PURCHASE_EVENT_SCHEMA, LOT_DISPOSITION_EVENT_SCHEMA, TRANSFER_EVENT_SCHEMA, EventLog
from augur.sim.scenario import RecurringTransfer, Scenario, ScheduledAssetSale, ScheduledTransfer
from augur.sim.state import StateCrossSection


def step_emit_events(*, state: StateCrossSection, scenario: Scenario, month: int, rollout_count: int) -> EventLog:
    """Return the events to apply at this month. Pure: does not
    mutate `state`."""
    return EventLog(
        transfers=_emit_transfers(scenario, month, rollout_count),
        asset_purchases=pl.DataFrame(schema=ASSET_PURCHASE_EVENT_SCHEMA),
        lot_dispositions=_emit_lot_dispositions(state, scenario, month),
    )


def _emit_transfers(scenario: Scenario, month: int, rollout_count: int) -> pl.DataFrame:
    """Emit Transfer event rows for every scheduled or recurring
    transfer active at this month. Scheduled transfers fire only at
    their configured month; recurring transfers fire every month in
    `[start_month, end_month]` (or through horizon end). One row per
    (transfer, rollout)."""
    scheduled = [t for t in scenario.scheduled_transfers if t.month == month]
    recurring = [t for t in scenario.recurring_transfers if t.is_active_at(month)]
    if not scheduled and not recurring:
        return pl.DataFrame(schema=TRANSFER_EVENT_SCHEMA)
    rollouts = pl.DataFrame({"rollout_index": list(range(rollout_count))}, schema={"rollout_index": pl.Int64()})
    blocks = [_transfer_block_per_rollout(t, rollouts, month) for t in (*scheduled, *recurring)]
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
    )


def _emit_lot_dispositions(state: StateCrossSection, scenario: Scenario, month: int) -> pl.DataFrame:
    """Emit `LotDisposition` rows for every scheduled asset sale at
    this month. Each sale is FIFO-resolved against the agent's
    current lots of the asset; the same resolution applies
    per-rollout via polars window functions over `rollout_index`.

    At spike-1 step 4 part A scenarios have at most one sale per
    `(agent, asset)` per month, so the resolution reads the current
    `state.asset_lots` directly and emits its dispositions without
    chaining."""
    sales = [s for s in scenario.scheduled_asset_sales if s.month == month]
    if not sales:
        return pl.DataFrame(schema=LOT_DISPOSITION_EVENT_SCHEMA)
    blocks = [_fifo_dispositions_for_sale(state, sale, month) for sale in sales]
    blocks = [b for b in blocks if not b.is_empty()]
    if not blocks:
        return pl.DataFrame(schema=LOT_DISPOSITION_EVENT_SCHEMA)
    return pl.concat(blocks).select(list(LOT_DISPOSITION_EVENT_SCHEMA.keys()))


def _fifo_dispositions_for_sale(state: StateCrossSection, sale: ScheduledAssetSale, month: int) -> pl.DataFrame:
    """Vectorized FIFO consumption of one sale across all rollouts.

    Within each rollout the lots of the matching `(agent_id,
    asset_id)` are ordered by `purchase_month_index` ascending; the
    sale eats from the oldest forward. A lot's `units_sold` is
    `clip(sale.quantity - prev_cumulative_remaining, 0,
    remaining_quantity)`. The result is one disposition row per
    consumed lot per rollout."""
    candidates = state.asset_lots.filter(
        (pl.col("agent_id") == sale.agent_id)
        & (pl.col("asset_id") == sale.asset_id)
        & (pl.col("remaining_quantity") > 0)
    )
    if candidates.is_empty():
        return pl.DataFrame(schema=LOT_DISPOSITION_EVENT_SCHEMA)
    ordered = candidates.sort(["rollout_index", "purchase_month_index", "lot_id"])
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
        (pl.col("_units_from_lot") * pl.lit(sale.price_per_unit_usd)).alias("proceeds_usd"),
        pl.lit(sale.proceeds_account_id, dtype=pl.Utf8()).alias("proceeds_account_id"),
    ).select(list(LOT_DISPOSITION_EVENT_SCHEMA.keys()))
