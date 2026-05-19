"""`step_emit_events` — pure function that reads state + scenario +
month index and returns the events for that month.

At spike 1 the step is small: it only emits scheduled transfers
that the scenario configured. Later layers add scheduled property
events, recurring obligation accruals, tax-marker accruals,
obligation settlement (with funding chain), discretionary policies,
and end-of-month accruals.

The step does not mutate `state`. The simulate loop calls
`apply_events(state, step_result)` separately.
"""

from __future__ import annotations

import polars as pl

from augur.sim.events import TRANSFER_EVENT_SCHEMA, EventLog
from augur.sim.scenario import Scenario, ScheduledTransfer
from augur.sim.state import StateCrossSection


def step_emit_events(*, state: StateCrossSection, scenario: Scenario, month: int, rollout_count: int) -> EventLog:
    """Return the events to apply at this month. Pure: does not
    mutate `state`."""
    _ = state  # spike 1 step doesn't read state yet; later layers will
    return EventLog(transfers=_emit_scheduled_transfers(scenario, month, rollout_count))


def _emit_scheduled_transfers(scenario: Scenario, month: int, rollout_count: int) -> pl.DataFrame:
    """Emit one Transfer row per (scheduled_transfer scheduled at
    `month`, rollout). Same amount across rollouts at spike 1 —
    scheduled transfers are scenario-fixed inputs."""
    matching = [t for t in scenario.scheduled_transfers if t.month == month]
    if not matching:
        return pl.DataFrame(schema=TRANSFER_EVENT_SCHEMA)
    rollouts = pl.DataFrame({"rollout_index": list(range(rollout_count))}, schema={"rollout_index": pl.Int64()})
    blocks = [_transfer_block_per_rollout(t, rollouts, month) for t in matching]
    return pl.concat(blocks).select(list(TRANSFER_EVENT_SCHEMA.keys()))


def _transfer_block_per_rollout(t: ScheduledTransfer, rollouts: pl.DataFrame, month: int) -> pl.DataFrame:
    """One row per rollout for one scheduled transfer. The rollout
    dimension is expanded vectorized — no Python loop over rollouts."""
    return rollouts.with_columns(
        pl.lit(month, dtype=pl.Int64()).alias("month_index"),
        pl.lit(t.cause_id, dtype=pl.Utf8()).alias("cause_id"),
        pl.lit(t.from_agent_id, dtype=pl.Utf8()).alias("from_agent_id"),
        pl.lit(t.from_account_id, dtype=pl.Utf8()).alias("from_account_id"),
        pl.lit(t.to_agent_id, dtype=pl.Utf8()).alias("to_agent_id"),
        pl.lit(t.to_account_id, dtype=pl.Utf8()).alias("to_account_id"),
        pl.lit(t.amount_usd, dtype=pl.Float64()).alias("amount_usd"),
    )
