"""Shared replay-invariant assertion for scenario tests.

The spike-1 central guarantee:

    state_at(M).event_sourced ==
        apply_events(initial_state, events_log.filter(month <= M))

for every M. `assert_replay_invariant_holds` is the verification of
this guarantee for every month boundary: any scenario test can drop it
in after a successful `simulate()` call to ratify the invariant for
that scenario, without writing scenario-specific replay plumbing.

The helper compares every state frame the engine touches —
`cash_balances`, `asset_lots`, `ordinary_income_ytd`,
`capital_gains_ytd`, `tax_liabilities`, `property_state`,
`property_stakes`, `liabilities`, `rollout_status`. New frames added
in later spikes need new entries here.

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
    """Replay month-by-month from a fresh initial state and check
    every materialized state frame at every month boundary.

    The `rollout_count` argument must match the value passed to
    `simulate()` — it's not stored on `SimulationRun` because the
    rollout dimension lives implicitly in every long-form frame."""
    replayed = _initial_state(scenario, rollout_count)
    horizon = int(scenario.horizon_months)
    for month in range(horizon + 1):
        _check_month(result=result, replayed=replayed, month=month)
        if month == horizon:
            break
        replayed = apply_events(replayed, result.events_log.at_month(month))


def _check_month(*, result: SimulationRun, replayed: StateCrossSection, month: int) -> None:
    _check_frame(
        kind=f"cash_balances/month_{month}",
        incremental=_month_slice(result.cash_balances, month),
        replayed=replayed.cash_balances,
        sort_keys=["rollout_index", "agent_id", "account_id"],
    )
    _check_frame(
        kind=f"asset_lots/month_{month}",
        incremental=_month_slice(result.asset_lots, month),
        replayed=replayed.asset_lots,
        sort_keys=["rollout_index", "lot_id"],
    )
    _check_frame(
        kind=f"ordinary_income_ytd/month_{month}",
        incremental=_month_slice(result.ordinary_income_ytd, month),
        replayed=replayed.ordinary_income_ytd,
        sort_keys=["rollout_index", "agent_id"],
    )
    _check_frame(
        kind=f"capital_gains_ytd/month_{month}",
        incremental=_month_slice(result.capital_gains_ytd, month),
        replayed=replayed.capital_gains_ytd,
        sort_keys=["rollout_index", "agent_id", "classification"],
    )
    _check_frame(
        kind=f"tax_liabilities/month_{month}",
        incremental=_month_slice(result.tax_liabilities, month),
        replayed=replayed.tax_liabilities,
        sort_keys=["rollout_index", "agent_id", "jurisdiction_id", "tax_year_end_month"],
    )
    _check_frame(
        kind=f"property_state/month_{month}",
        incremental=_month_slice(result.property_state, month),
        replayed=replayed.property_state,
        sort_keys=["rollout_index", "property_id"],
    )
    _check_frame(
        kind=f"property_stakes/month_{month}",
        incremental=_month_slice(result.property_stakes, month),
        replayed=replayed.property_stakes,
        sort_keys=["rollout_index", "property_id", "agent_id"],
    )
    _check_frame(
        kind=f"liabilities/month_{month}",
        incremental=_month_slice(result.liabilities, month),
        replayed=replayed.liabilities,
        sort_keys=["rollout_index", "liability_id"],
    )
    _check_frame(
        kind=f"rollout_status/month_{month}",
        incremental=_month_slice(result.rollout_status_history, month),
        replayed=replayed.rollout_status,
        sort_keys=["rollout_index"],
    )


def _month_slice(frame: pl.DataFrame, month: int) -> pl.DataFrame:
    """One-month cross-section view of a per-month long-form
    frame, projected to the schema apply_events produces."""
    return frame.filter(pl.col("month_index") == month).drop("month_index")


# Round float columns before `.equals` so the invariant compares values
# up to nano-cent precision (more than enough for tax accuracy) without
# flagging FP noise.
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
