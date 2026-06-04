"""Parity test for the jitted `lax.scan` fast path (`run_jax_scan`).

A transfers-only scenario satisfies `scan_supported`, so on the JAX backend it routes through the
scan engine instead of the eager `run_jax`. The autouse `backend` fixture (augur/sim/conftest.py)
runs this under NumPy (reference) and JAX (scan); identical assertions gate scan == reference.
"""

from __future__ import annotations

import polars as pl
import pytest
import pytest_bazel

from augur.sim.scenario import (
    Agent,
    InitialAccountBalance,
    RecurringObligation,
    RecurringTransfer,
    Scenario,
    ScheduledTransfer,
)
from augur.sim.simulate import simulate


def _cash(run, agent_id: str, month_index: int) -> float:
    # `.item()` is typed Any; coerce so the lint aspect's mypy doesn't flag no-any-return.
    return float(
        run.cash_balances.filter(
            (pl.col("agent_id") == agent_id) & (pl.col("month_index") == month_index) & (pl.col("rollout_index") == 0)
        )
        .get_column("balance_usd")
        .item()
    )


def test_transfers_only_scan_parity() -> None:
    # Recurring paycheck for a year + a one-off gift: pure transfers, so JAX runs the lax.scan path.
    scenario = Scenario(
        agents=[Agent(agent_id="payroll"), Agent(agent_id="alice"), Agent(agent_id="bob")],
        initial_cash=[
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100.0),
            InitialAccountBalance(agent_id="bob", account_id="checking", balance_usd=500.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=11,
                cause_id="paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=1_000.0,
            )
        ],
        scheduled_transfers=[
            ScheduledTransfer(
                month=6,
                cause_id="bob_gifts_alice",
                from_agent_id="bob",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=250.0,
            )
        ],
        tax_profiles=[],
        horizon_months=12,
    )
    run = simulate(scenario, rollout_count=4, locations={})

    # alice: 100 opening + 12 paychecks of 1000 + a 250 gift = 12350.
    assert _cash(run, "alice", 12) == pytest.approx(100.0 + 12 * 1_000.0 + 250.0)
    assert _cash(run, "bob", 12) == pytest.approx(500.0 - 250.0)
    assert _cash(run, "payroll", 12) == pytest.approx(-12 * 1_000.0)
    # Mid-horizon snapshot: 6 paychecks landed by month 6 (months 0..5), gift not yet (fires at 6).
    assert _cash(run, "alice", 6) == pytest.approx(100.0 + 6 * 1_000.0)


def test_configured_obligation_scan_parity() -> None:
    # Paycheck (transfer) + monthly rent (CONFIGURED obligation, settled via the funding/settlement
    # cores) — both phases the scan now folds. Always-funded, so no rollout fails.
    scenario = Scenario(
        agents=[Agent(agent_id="payroll"), Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1_000.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=11,
                cause_id="paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=5_000.0,
            )
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                end_month=11,
                obligation_id="rent",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=2_000.0,
            )
        ],
        tax_profiles=[],
        horizon_months=12,
    )
    run = simulate(scenario, rollout_count=4, locations={})

    # alice: 1000 opening + 12 paychecks of 5000 - 12 rents of 2000 = 37000.
    assert _cash(run, "alice", 12) == pytest.approx(1_000.0 + 12 * 5_000.0 - 12 * 2_000.0)
    assert _cash(run, "landlord", 12) == pytest.approx(12 * 2_000.0)
    assert _cash(run, "payroll", 12) == pytest.approx(-12 * 5_000.0)


def test_obligation_failure_scan_parity() -> None:
    # No income: alice can pay rent in month 0 (1000 -> 400) but not month 1 (needs 600), so the
    # rollout fails at month 1. Failure is per-rollout (a whole Monte-Carlo path), so
    # `_zero_failed_state` zeros every account in that rollout's column from the failure month on —
    # including the landlord's received rent. Exercises the scan's settlement failure path.
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1_000.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                end_month=11,
                obligation_id="rent",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=600.0,
            )
        ],
        tax_profiles=[],
        horizon_months=12,
    )
    run = simulate(scenario, rollout_count=4, locations={})

    assert _cash(run, "alice", 1) == pytest.approx(400.0)  # after month 0: rent paid (1000 -> 400)
    assert _cash(run, "landlord", 1) == pytest.approx(600.0)  # month 0's rent landed pre-failure
    assert _cash(run, "alice", 12) == pytest.approx(0.0)  # whole rollout zeroed after month-1 failure
    assert _cash(run, "landlord", 12) == pytest.approx(0.0)  # landlord's column zeroed too


if __name__ == "__main__":
    pytest_bazel.main()
