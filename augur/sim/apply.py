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
kind-specific apply function. `apply_events` composes them.
"""

from __future__ import annotations

import polars as pl

from augur.sim.events import EventLog
from augur.sim.state import ASSET_LOT_SCHEMA, StateCrossSection


def apply_events(state: StateCrossSection, events: EventLog) -> StateCrossSection:
    """Apply all events in `events` to `state`. Returns the new
    cross-section. Pure: does not mutate inputs."""
    cash_balances = state.cash_balances
    asset_lots = state.asset_lots
    if not events.asset_purchases.is_empty():
        asset_lots = _apply_asset_purchases(asset_lots, events.asset_purchases)
    if not events.lot_dispositions.is_empty():
        asset_lots = _apply_lot_dispositions_to_lots(asset_lots, events.lot_dispositions)
        cash_balances = _apply_lot_dispositions_to_cash(cash_balances, events.lot_dispositions)
    if not events.transfers.is_empty():
        cash_balances = _apply_transfers(cash_balances, events.transfers)
    return StateCrossSection(cash_balances=cash_balances, asset_lots=asset_lots)


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
