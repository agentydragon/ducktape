"""Scripted cashflows and conservation through the common Python action session."""

import pytest
import pytest_bazel

from finance.augur.sim.actions import DecisionActions
from finance.augur.sim.books import Book
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.scenario import Agent, InitialAccountBalance, RecurringTransfer, Scenario, ScheduledTransfer
from finance.augur.sim.session import ActionSession
from finance.augur.sim.testing.case import sampled


def _run(scenario: Scenario, *, rollout_count: int = 1) -> list[Rollout]:
    case = sampled(scenario, rollout_count=rollout_count, locations={})
    session = ActionSession(case.compiled_run, "alice", list(range(rollout_count)), capture="forensic")
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(
                [DecisionActions(decision.rollout_id, decision.observation.month, []) for decision in batch]
            )
        assert all(result.stop is None for result in batch.rollouts)
        return batch.rollouts
    finally:
        session.close()


def _cash(book: Book, agent_id: str) -> int:
    [balance] = [
        row.balance for row in book.balances if (row.account.agent_id, row.account.account_id) == (agent_id, "checking")
    ]
    return balance


def test_bob_gives_alice_five_dollars_one_rollout(alice_bob_scenario: Scenario) -> None:
    [result] = _run(alice_bob_scenario)
    assert result.trace is not None
    assert [book.month for book in result.trace.books] == [0, 1]
    assert [[_cash(book, actor) for actor in ("alice", "bob")] for book in result.trace.books] == [
        [1000, 2000],
        [1500, 1500],
    ]
    assert [_cash(book, "alice") + _cash(book, "bob") for book in result.trace.books] == [3000, 3000]
    [txn] = result.trace.events.transfers.iter_rows(named=True)
    assert txn["from_agent_id"] == "bob"
    assert txn["to_agent_id"] == "alice"
    assert txn["amount_quanta"] == 500
    assert txn["month_index"] == 0


def test_no_scheduled_transfers_leaves_balances_unchanged() -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance=100)],
        tax_profiles=[],
        horizon_months=5,
    )
    [result] = _run(scenario)
    assert result.trace is not None
    assert [book.month for book in result.trace.books] == list(range(6))
    assert [_cash(book, "alice") for book in result.trace.books] == [10000] * 6
    assert result.trace.events.transfers.is_empty()


def test_rejects_zero_rollout_count(alice_bob_scenario: Scenario) -> None:
    with pytest.raises(ValueError, match="rollout_count must be positive"):
        _run(alice_bob_scenario, rollout_count=0)


def test_recurring_paycheck_accrues_monthly() -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payroll")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=1000),
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance=0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount=3000,
            )
        ],
        tax_profiles=[],
        horizon_months=12,
    )
    [result] = _run(scenario)
    assert _cash(result.summary.ending_book, "alice") == 3700000
    # Scripted counterparties debit the same cash, even below zero.
    assert _cash(result.summary.ending_book, "payroll") == -3600000
    assert result.trace is not None
    transfers = result.trace.events.transfers
    assert transfers.height == 12
    assert transfers.get_column("month_index").to_list() == list(range(12))
    assert transfers.get_column("amount_quanta").to_list() == [300000] * 12
    assert set(transfers.get_column("cause_id")) == {"alice_paycheck"}


def test_recurring_transfer_bounded_by_end_month() -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="sink")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=0),
            InitialAccountBalance(agent_id="sink", account_id="checking", balance=0),
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
                amount=100,
            )
        ],
        tax_profiles=[],
        horizon_months=10,
    )
    [result] = _run(scenario)
    assert result.trace is not None
    assert result.trace.events.transfers.get_column("month_index").to_list() == list(range(5))
    assert [_cash(book, "alice") for book in result.trace.books] == [
        0,
        10000,
        20000,
        30000,
        40000,
        50000,
        50000,
        50000,
        50000,
        50000,
        50000,
    ]


def test_one_thousand_rollouts_identical_when_inputs_are() -> None:
    """Identical paths conserve cash independently at every month, including the opening mark."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="employer")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=1000),
            InitialAccountBalance(agent_id="employer", account_id="checking", balance=0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                cause_id="alice_paycheck",
                from_agent_id="employer",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount=2000,
            )
        ],
        tax_profiles=[],
        horizon_months=24,
    )
    results = _run(scenario, rollout_count=1000)
    assert [result.rollout_id for result in results] == list(range(1000))
    assert [_cash(result.summary.ending_book, "alice") for result in results] == [4900000] * 1000
    for result in results:
        assert result.trace is not None
        assert result.trace.events.transfers.height == 24
        assert [book.month for book in result.trace.books] == list(range(25))
        assert [_cash(book, "alice") + _cash(book, "employer") for book in result.trace.books] == [100000] * 25


def test_combined_one_off_and_recurring() -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="employer")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=0),
            InitialAccountBalance(agent_id="employer", account_id="checking", balance=0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                cause_id="alice_paycheck",
                from_agent_id="employer",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount=1000,
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
                amount=5000,
            )
        ],
        tax_profiles=[],
        horizon_months=10,
    )
    [result] = _run(scenario)
    assert _cash(result.summary.ending_book, "alice") == 1500000
    assert result.trace is not None
    transfers = result.trace.events.transfers
    assert transfers.height == 11
    assert sorted(
        (txn["cause_id"], txn["amount_quanta"]) for txn in transfers.iter_rows(named=True) if txn["month_index"] == 5
    ) == [("alice_bonus", 500000), ("alice_paycheck", 100000)]


if __name__ == "__main__":
    pytest_bazel.main()
