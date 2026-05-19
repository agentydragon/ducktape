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
from augur.sim.state import StateCrossSection


def apply_events(state: StateCrossSection, events: EventLog) -> StateCrossSection:
    """Apply all events in `events` to `state`. Returns the new
    cross-section. Pure: does not mutate inputs."""
    cash_balances = state.cash_balances
    if not events.transfers.is_empty():
        cash_balances = _apply_transfers(cash_balances, events.transfers)
    return StateCrossSection(cash_balances=cash_balances)


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
