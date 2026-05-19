"""End-to-end tests for the spike-1 simulator.

L1 baseline: Alice gives Bob $5, single rollout, one-month horizon.
The simulator advances state via the apply_events pipeline, records
the transfer on the event log, and produces a long-form
state-over-time frame that satisfies the conservation invariant.
"""

from __future__ import annotations

import polars as pl
import pytest
import pytest_bazel

from augur.sim.apply import apply_events
from augur.sim.scenario import Agent, InitialAccountBalance, Scenario, ScheduledTransfer
from augur.sim.simulate import _initial_state, simulate


def _alice_bob_scenario() -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="bob")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=10.0),
            InitialAccountBalance(agent_id="bob", account_id="checking", balance_usd=20.0),
        ],
        scheduled_transfers=[
            ScheduledTransfer(
                month=0,
                cause_id="bob_gives_alice_5",
                from_agent_id="bob",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=5.0,
            )
        ],
        horizon_months=1,
    )


def test_alice_gives_bob_five_dollars_one_rollout() -> None:
    """One scheduled transfer at month 0 moves $5 from Bob to Alice.
    After month 0: Alice $15, Bob $15. The transfer is on the log;
    the post-step cross-section reflects it; total cash in the
    system is conserved at every month."""
    result = simulate(_alice_bob_scenario(), rollout_count=1)

    initial = result.cash_balances.filter(pl.col("month_index") == 0).sort("agent_id")
    assert initial.get_column("balance_usd").to_list() == [10.0, 20.0]

    post = result.cash_balances.filter(pl.col("month_index") == 1).sort("agent_id")
    assert post.get_column("balance_usd").to_list() == [15.0, 15.0]

    # Conservation invariant: total cash unchanged at every month.
    totals = (
        result.cash_balances.group_by("month_index").agg(pl.col("balance_usd").sum().alias("total")).sort("month_index")
    )
    assert totals.get_column("total").to_list() == [30.0, 30.0]

    # The transfer is on the log.
    assert result.events_log.transfers.height == 1
    txn = result.events_log.transfers.row(0, named=True)
    assert txn["from_agent_id"] == "bob"
    assert txn["to_agent_id"] == "alice"
    assert txn["amount_usd"] == 5.0
    assert txn["month_index"] == 0


def test_apply_events_is_only_mutation_replays_from_log() -> None:
    """Replay invariant: re-applying the event log to the initial
    state from scratch produces the same final cross-section as the
    incrementally-maintained one. If apply_events ever drifts from
    the log this test catches it."""
    scenario = _alice_bob_scenario()
    rollout_count = 1
    result = simulate(scenario, rollout_count=rollout_count)

    final_incremental = result.cash_balances.filter(pl.col("month_index") == int(scenario.horizon_months)).sort(
        ["rollout_index", "agent_id", "account_id"]
    )

    # Re-derive: apply the entire event log to a fresh initial state.
    initial = _initial_state(scenario, rollout_count)
    from_log = apply_events(initial, result.events_log).cash_balances.sort(["rollout_index", "agent_id", "account_id"])

    assert from_log.equals(final_incremental.drop("month_index"))


def test_no_scheduled_transfers_leaves_balances_unchanged() -> None:
    """Multi-month horizon with no events should carry initial cash
    forward unchanged. Exercises the empty-event-log path through
    the loop."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100.0)],
        horizon_months=5,
    )

    result = simulate(scenario, rollout_count=1)

    # Six rows: initial month 0 through end-of-horizon month 5.
    assert result.cash_balances.height == 6
    assert result.cash_balances.get_column("balance_usd").to_list() == [100.0] * 6
    assert result.events_log.transfers.is_empty()


def test_rejects_zero_rollout_count() -> None:
    with pytest.raises(ValueError, match="rollout_count"):
        simulate(_alice_bob_scenario(), rollout_count=0)


if __name__ == "__main__":
    pytest_bazel.main()
