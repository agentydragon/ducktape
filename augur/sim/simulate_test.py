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
from augur.sim.scenario import Agent, InitialAccountBalance, RecurringTransfer, Scenario, ScheduledTransfer
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


def test_recurring_paycheck_accrues_monthly() -> None:
    """Alice receives a $3000 paycheck every month from a payroll
    sink for 12 months. Starting cash $1000; ending cash
    $1000 + 12 × $3000 = $37000. One Transfer event per month on
    the log."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payroll")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1000.0),
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=3000.0,
            )
        ],
        horizon_months=12,
    )

    result = simulate(scenario, rollout_count=1)

    alice_final = (
        result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 12))
        .get_column("balance_usd")
        .item()
    )
    assert alice_final == 1000.0 + 12 * 3000.0

    # Conservation: payroll sink goes negative by the same amount.
    payroll_final = (
        result.cash_balances.filter((pl.col("agent_id") == "payroll") & (pl.col("month_index") == 12))
        .get_column("balance_usd")
        .item()
    )
    assert payroll_final == -12 * 3000.0

    # 12 paycheck events on the log (one per month).
    assert result.events_log.transfers.height == 12
    assert set(result.events_log.transfers.get_column("month_index").to_list()) == set(range(12))
    assert set(result.events_log.transfers.get_column("cause_id").unique().to_list()) == {"alice_paycheck"}


def test_recurring_transfer_bounded_by_end_month() -> None:
    """Recurring transfer with end_month=4 fires months 0-4
    (inclusive), then stops. Asserts the end_month bound is
    honored — no events at month 5+."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="sink")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="sink", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=4,
                cause_id="bounded_pay",
                from_agent_id="sink",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=100.0,
            )
        ],
        horizon_months=10,
    )

    result = simulate(scenario, rollout_count=1)
    assert result.events_log.transfers.height == 5  # months 0..4

    # Alice's balance plateaus at 500.0 from month 5 onward.
    balances = (
        result.cash_balances.filter(pl.col("agent_id") == "alice")
        .sort("month_index")
        .get_column("balance_usd")
        .to_list()
    )
    assert balances == [0.0, 100.0, 200.0, 300.0, 400.0, 500.0, 500.0, 500.0, 500.0, 500.0, 500.0]


def test_combined_one_off_and_recurring() -> None:
    """A scenario with both a recurring monthly paycheck and a
    one-off bonus transfer at month 5. Both fire through the same
    Transfer event path; the log shows both. Tests that the step
    emits both kinds in one call."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="employer")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="employer", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                cause_id="alice_paycheck",
                from_agent_id="employer",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=1000.0,
            )
        ],
        scheduled_transfers=[
            ScheduledTransfer(
                month=5,
                cause_id="alice_bonus",
                from_agent_id="employer",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=5000.0,
            )
        ],
        horizon_months=10,
    )

    result = simulate(scenario, rollout_count=1)

    # 10 paycheck events + 1 bonus = 11.
    assert result.events_log.transfers.height == 11

    # Alice at end-of-horizon: 10 × $1000 paychecks + $5000 bonus = $15000.
    alice_final = (
        result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 10))
        .get_column("balance_usd")
        .item()
    )
    assert alice_final == 15000.0


if __name__ == "__main__":
    pytest_bazel.main()
