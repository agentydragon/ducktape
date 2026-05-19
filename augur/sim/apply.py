"""apply_events — the single state-mutation primitive.

`apply_events(state, events) → state'` is the only function in the
simulator that writes the event-sourced columns of state. The
forward loop calls it once per iteration; tests + an opt-in
`--check-replay` flag use it to validate that incrementally-
maintained state agrees with re-derivation from the cumulative
event log:

    state_at(M).event_sourced ==
        apply_events(initial_state, events_log.filter(month <= M))

for every M. If the invariant ever fails, the bug is here and the
fix is in one place.

Dispatch is by event kind: each kind's frame is consumed by a
kind-specific apply function. `apply_events` composes them in a
fixed order so that downstream kinds (tax accruals) see the
already-updated state from upstream kinds (income transfers).
"""

from __future__ import annotations

import polars as pl

from augur.sim.events import EventLog
from augur.sim.state import ASSET_LOT_SCHEMA, StateCrossSection


def apply_events(state: StateCrossSection, events: EventLog) -> StateCrossSection:
    """Apply all events in `events` to `state`. Returns the new
    cross-section. Pure: does not mutate inputs.

    Order matters: income-carrying transfers increment
    `ordinary_income_ytd` first, asset sales both credit cash and
    bucket capital gains into ltcg/stcg, then year-end tax accruals
    book a liability and zero out the year-to-date totals."""
    cash_balances = state.cash_balances
    asset_lots = state.asset_lots
    ordinary_income_ytd = state.ordinary_income_ytd
    capital_gains_ytd = state.capital_gains_ytd
    tax_liabilities = state.tax_liabilities

    if not events.asset_purchases.is_empty():
        asset_lots = _apply_asset_purchases(asset_lots, events.asset_purchases)
    if not events.lot_dispositions.is_empty():
        asset_lots = _apply_lot_dispositions_to_lots(asset_lots, events.lot_dispositions)
        cash_balances = _apply_lot_dispositions_to_cash(cash_balances, events.lot_dispositions)
        capital_gains_ytd = _apply_dispositions_to_capital_gains_ytd(capital_gains_ytd, events.lot_dispositions)
    if not events.transfers.is_empty():
        cash_balances = _apply_transfers(cash_balances, events.transfers)
        ordinary_income_ytd = _apply_income_to_ytd(ordinary_income_ytd, events.transfers)
    if not events.tax_accruals.is_empty():
        tax_liabilities = _apply_tax_accruals_to_liabilities(tax_liabilities, events.tax_accruals)
        ordinary_income_ytd = _reset_ytd_for_taxed_agents(ordinary_income_ytd, events.tax_accruals)
        capital_gains_ytd = _reset_capital_gains_for_taxed_agents(capital_gains_ytd, events.tax_accruals)

    return StateCrossSection(
        cash_balances=cash_balances,
        asset_lots=asset_lots,
        ordinary_income_ytd=ordinary_income_ytd,
        capital_gains_ytd=capital_gains_ytd,
        tax_liabilities=tax_liabilities,
    )


def _apply_transfers(cash_balances: pl.DataFrame, transfers: pl.DataFrame) -> pl.DataFrame:
    """Apply transfer events to cash_balances. Each transfer debits
    `from_agent`'s `from_account` and credits `to_agent`'s `to_account`.
    Vectorized: aggregates per-(rollout, agent, account) deltas and
    joins them into the cash_balances frame in one expression."""
    outgoing = (
        transfers.group_by(["rollout_index", "from_agent_id", "from_account_id"])
        .agg(pl.col("amount_usd").sum())
        .rename({"from_agent_id": "agent_id", "from_account_id": "account_id", "amount_usd": "_delta_out"})
    )
    incoming = (
        transfers.group_by(["rollout_index", "to_agent_id", "to_account_id"])
        .agg(pl.col("amount_usd").sum())
        .rename({"to_agent_id": "agent_id", "to_account_id": "account_id", "amount_usd": "_delta_in"})
    )
    return (
        cash_balances.join(outgoing, on=["rollout_index", "agent_id", "account_id"], how="left")
        .join(incoming, on=["rollout_index", "agent_id", "account_id"], how="left")
        .with_columns(
            balance_usd=pl.col("balance_usd") - pl.col("_delta_out").fill_null(0.0) + pl.col("_delta_in").fill_null(0.0)
        )
        .drop(["_delta_out", "_delta_in"])
    )


def _apply_income_to_ytd(ordinary_income_ytd: pl.DataFrame, transfers: pl.DataFrame) -> pl.DataFrame:
    """Increment per-(rollout, recipient) ordinary_income_ytd by
    the sum of transfer amounts whose `income_category == "ordinary"`.
    Transfers without that tag don't touch YTD."""
    ordinary_transfers = transfers.filter(pl.col("income_category") == "ordinary")
    if ordinary_transfers.is_empty():
        return ordinary_income_ytd
    deltas = (
        ordinary_transfers.group_by(["rollout_index", "to_agent_id"])
        .agg(pl.col("amount_usd").sum().alias("_delta"))
        .rename({"to_agent_id": "agent_id"})
    )
    return (
        ordinary_income_ytd.join(deltas, on=["rollout_index", "agent_id"], how="left")
        .with_columns(ordinary_income_usd=pl.col("ordinary_income_usd") + pl.col("_delta").fill_null(0.0))
        .drop("_delta")
    )


def _apply_asset_purchases(asset_lots: pl.DataFrame, purchases: pl.DataFrame) -> pl.DataFrame:
    """Append purchase events as new lot rows. Each purchase row
    becomes one lot with `remaining_quantity = quantity`. Lots are
    keyed by `(rollout_index, lot_id)`; the scenario is responsible
    for assigning unique `lot_id` strings."""
    new_lots = purchases.select(
        pl.col("rollout_index"),
        pl.col("lot_id"),
        pl.col("agent_id"),
        pl.col("asset_id"),
        pl.col("month_index").alias("purchase_month_index"),
        pl.col("cost_basis_per_unit_usd"),
        pl.col("quantity").alias("remaining_quantity"),
    ).select(list(ASSET_LOT_SCHEMA.keys()))
    if asset_lots.is_empty():
        return new_lots
    return pl.concat([asset_lots, new_lots])


def _apply_lot_dispositions_to_lots(asset_lots: pl.DataFrame, dispositions: pl.DataFrame) -> pl.DataFrame:
    """Reduce `remaining_quantity` on the lots consumed by each
    disposition. Aggregates `units_sold` per `(rollout_index,
    lot_id)` so multiple dispositions of the same lot (e.g. two
    sales of the same asset in the same month) compose. Vectorized:
    one left-join + one with_columns."""
    deltas = dispositions.group_by(["rollout_index", "lot_id"]).agg(pl.col("units_sold").sum().alias("_units_consumed"))
    return (
        asset_lots.join(deltas, on=["rollout_index", "lot_id"], how="left")
        .with_columns(remaining_quantity=pl.col("remaining_quantity") - pl.col("_units_consumed").fill_null(0.0))
        .drop("_units_consumed")
    )


def _apply_lot_dispositions_to_cash(cash_balances: pl.DataFrame, dispositions: pl.DataFrame) -> pl.DataFrame:
    """Credit sale proceeds to the configured `proceeds_account_id`
    on each disposition. Aggregates per (rollout, agent, account)
    before joining so multi-lot sales fold into a single delta."""
    credits = (
        dispositions.group_by(["rollout_index", "agent_id", "proceeds_account_id"])
        .agg(pl.col("proceeds_usd").sum().alias("_delta_in"))
        .rename({"proceeds_account_id": "account_id"})
    )
    return (
        cash_balances.join(credits, on=["rollout_index", "agent_id", "account_id"], how="left")
        .with_columns(balance_usd=pl.col("balance_usd") + pl.col("_delta_in").fill_null(0.0))
        .drop("_delta_in")
    )


def _apply_tax_accruals_to_liabilities(tax_liabilities: pl.DataFrame, tax_accruals: pl.DataFrame) -> pl.DataFrame:
    """Append each accrual as a new liability row. Liabilities are
    additive — paying them down is a later (step-9) concern that
    reduces `amount_owed_usd` via tax-payment events."""
    new_rows = tax_accruals.select(
        pl.col("rollout_index"),
        pl.col("agent_id"),
        pl.col("jurisdiction_id"),
        pl.col("tax_year_end_month"),
        pl.col("amount_usd").alias("amount_owed_usd"),
    )
    if tax_liabilities.is_empty():
        return new_rows
    return pl.concat([tax_liabilities, new_rows])


def _apply_dispositions_to_capital_gains_ytd(
    capital_gains_ytd: pl.DataFrame, dispositions: pl.DataFrame
) -> pl.DataFrame:
    """Bucket each lot disposition's `gain_usd = proceeds_usd -
    cost_basis_consumed_usd` into LTCG (holding period ≥ 12 months)
    or STCG, aggregate per (rollout, agent, classification), and
    add to the running YTD. Rollouts × agents that have no
    pre-existing row are appended; existing rows accumulate."""
    classified = dispositions.with_columns(
        gain_usd=pl.col("proceeds_usd") - pl.col("cost_basis_consumed_usd"),
        classification=pl.when(pl.col("month_index") - pl.col("purchase_month_index") >= 12)
        .then(pl.lit("ltcg"))
        .otherwise(pl.lit("stcg")),
    )
    deltas = classified.group_by(["rollout_index", "agent_id", "classification"]).agg(
        pl.col("gain_usd").sum().alias("_delta")
    )
    merged = capital_gains_ytd.join(
        deltas, on=["rollout_index", "agent_id", "classification"], how="full", coalesce=True
    )
    return merged.with_columns(gain_usd=pl.col("gain_usd").fill_null(0.0) + pl.col("_delta").fill_null(0.0)).drop(
        "_delta"
    )


def _reset_ytd_for_taxed_agents(ordinary_income_ytd: pl.DataFrame, tax_accruals: pl.DataFrame) -> pl.DataFrame:
    """Zero `ordinary_income_usd` for every (rollout, agent) that
    has a tax accrual fired this month — that's "year closed, start
    counting again from zero". Multiple jurisdictions accruing for
    the same agent collapse to one reset via the `_reset` flag from
    the left join."""
    affected = tax_accruals.select("rollout_index", "agent_id").unique().with_columns(pl.lit(True).alias("_reset"))
    return (
        ordinary_income_ytd.join(affected, on=["rollout_index", "agent_id"], how="left")
        .with_columns(
            ordinary_income_usd=pl.when(pl.col("_reset").fill_null(False))
            .then(0.0)
            .otherwise(pl.col("ordinary_income_usd"))
        )
        .drop("_reset")
    )


def _reset_capital_gains_for_taxed_agents(capital_gains_ytd: pl.DataFrame, tax_accruals: pl.DataFrame) -> pl.DataFrame:
    """Year-end reset of LTCG / STCG running totals — same pattern
    as the ordinary-income reset, keyed by (rollout, agent)."""
    affected = tax_accruals.select("rollout_index", "agent_id").unique().with_columns(pl.lit(True).alias("_reset"))
    return (
        capital_gains_ytd.join(affected, on=["rollout_index", "agent_id"], how="left")
        .with_columns(gain_usd=pl.when(pl.col("_reset").fill_null(False)).then(0.0).otherwise(pl.col("gain_usd")))
        .drop("_reset")
    )
