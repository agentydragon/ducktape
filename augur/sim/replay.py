"""Shared replay-invariant assertion for scenario tests.

The spike-1 central guarantee:

    state_at(M).event_sourced ==
        apply_events(initial_state, events_log.filter(month <= M))

for every M. `assert_replay_invariant_holds` is the verification of
this guarantee at the end-of-horizon cross-section: any scenario test
can drop it in after a successful `simulate()` call to ratify the
invariant for that scenario, without writing scenario-specific replay
plumbing.

The helper compares every state frame the engine touches —
`cash_balances`, `asset_lots`, `ordinary_income_ytd`,
`capital_gains_ytd`, `tax_liabilities`, `rollout_status`. New frames
added in later spikes need new entries here.

Also intended as the verification hook for an opt-in
`--check-replay` flag in production (per `DESIGN.md`); not yet wired.
"""

from __future__ import annotations

import polars as pl

from augur.sim.apply import apply_events
from augur.sim.run import SimulationRun
from augur.sim.scenario import Scenario
from augur.sim.simulate import _initial_state
from augur.sim.state import StateCrossSection


def assert_replay_invariant_holds(scenario: Scenario, result: SimulationRun, *, rollout_count: int) -> None:
    """Re-derive the end-state by applying the full event log to a
    fresh initial state, then check it matches the incremental
    result frame-by-frame.

    The `rollout_count` argument must match the value passed to
    `simulate()` — it's not stored on `SimulationRun` because the
    rollout dimension lives implicitly in every long-form frame."""
    initial = _initial_state(scenario, rollout_count)
    replayed = apply_events(initial, result.events_log)
    horizon = int(scenario.horizon_months)
    _check_frame(
        kind="cash_balances",
        incremental=_horizon_slice(result.cash_balances, horizon),
        replayed=replayed.cash_balances,
        sort_keys=["rollout_index", "agent_id", "account_id"],
    )
    _check_frame(
        kind="asset_lots",
        incremental=_horizon_slice(result.asset_lots, horizon),
        replayed=replayed.asset_lots,
        sort_keys=["rollout_index", "lot_id"],
    )
    _check_frame(
        kind="ordinary_income_ytd",
        incremental=_horizon_slice(result.ordinary_income_ytd, horizon),
        replayed=replayed.ordinary_income_ytd,
        sort_keys=["rollout_index", "agent_id"],
    )
    _check_frame(
        kind="capital_gains_ytd",
        incremental=_horizon_slice(result.capital_gains_ytd, horizon),
        replayed=replayed.capital_gains_ytd,
        sort_keys=["rollout_index", "agent_id", "classification"],
    )
    _check_frame(
        kind="tax_liabilities",
        incremental=_horizon_slice(result.tax_liabilities, horizon),
        replayed=replayed.tax_liabilities,
        sort_keys=["rollout_index", "agent_id", "jurisdiction_id", "tax_year_end_month"],
    )
    _check_rollout_status(incremental=result.rollout_status, replayed=replayed)


def _horizon_slice(frame: pl.DataFrame, horizon: int) -> pl.DataFrame:
    """End-of-horizon cross-section view of a per-month long-form
    frame, projected to the schema apply_events produces."""
    return frame.filter(pl.col("month_index") == horizon).drop("month_index")


# Cumulative replay sums all months' transfers in one polars group_by;
# the incremental loop sums per month and accumulates. Float addition
# is non-associative, so a many-paycheck-plus-tax-payment scenario can
# land a few ULPs apart on the two paths. Round float columns before
# `.equals` so the invariant compares values up to nano-cent precision
# (more than enough for tax accuracy) without flagging FP noise.
_FLOAT_COMPARISON_DECIMALS: int = 6


def _check_frame(*, kind: str, incremental: pl.DataFrame, replayed: pl.DataFrame, sort_keys: list[str]) -> None:
    inc_sorted = _round_floats(incremental.sort(sort_keys))
    rep_sorted = _round_floats(replayed.sort(sort_keys))
    if not inc_sorted.equals(rep_sorted):
        msg = f"replay invariant violated for {kind!r}:\n  incremental:\n{inc_sorted}\n  replayed:\n{rep_sorted}"
        raise AssertionError(msg)


def _round_floats(frame: pl.DataFrame) -> pl.DataFrame:
    """Round every float column to `_FLOAT_COMPARISON_DECIMALS` to
    swallow FP-associativity noise between the per-month and
    cumulative apply paths."""
    return frame.with_columns(
        pl.col(name).round(_FLOAT_COMPARISON_DECIMALS) for name, dtype in frame.schema.items() if dtype.is_float()
    )


def _check_rollout_status(*, incremental: pl.DataFrame, replayed: StateCrossSection) -> None:
    """`rollout_status` is already a single cross-section on
    `SimulationRun`; no horizon slicing needed."""
    _check_frame(
        kind="rollout_status", incremental=incremental, replayed=replayed.rollout_status, sort_keys=["rollout_index"]
    )
