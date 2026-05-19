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
from augur.sim.scenario import RecurringTransfer, Scenario, ScheduledTransfer
from augur.sim.state import StateCrossSection


def step_emit_events(*, state: StateCrossSection, scenario: Scenario, month: int, rollout_count: int) -> EventLog:
    """Return the events to apply at this month. Pure: does not
    mutate `state`."""
    _ = state  # spike 1 step doesn't read state yet; later layers will
    return EventLog(transfers=_emit_transfers(scenario, month, rollout_count))


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
