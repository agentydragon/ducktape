"""Scripted cashflows and conservation through the common Python action session."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.sim.actions import DecisionActions
from finance.augur.sim.books import AccountRef, Book
from finance.augur.sim.fixed_point import currency_amount_to_quanta
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import PreparedAccount, PreparedRecurringTransfer, PreparedTransfer
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.scenario import ORDINARY_INCOME
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")


def checking(agent_id: str) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id="checking")


def quanta(amount: Decimal) -> int:
    return int(currency_amount_to_quanta(amount, quantum=QUANTUM))


def one_off(month: int, cause_id: str, payer: str, payee: str, amount: Decimal) -> PreparedTransfer:
    return PreparedTransfer(
        month=month,
        cause_id=cause_id,
        from_account=checking(payer),
        to_account=checking(payee),
        amount=quanta(amount),
        income_category=None,
        deduction_category=None,
    )


def monthly(
    cause_id: str, payer: str, payee: str, amount: Decimal, *, start_month: int = 0, end_month: int | None = None
) -> PreparedRecurringTransfer:
    return PreparedRecurringTransfer(
        start_month=start_month,
        end_month=end_month,
        cause_id=cause_id,
        from_account=checking(payer),
        to_account=checking(payee),
        amount=quanta(amount),
        income_category=None,
        deduction_category=None,
    )


@dataclass(frozen=True)
class Situation:
    """Opening balances and the scripted cashflow tables; `compose` declares them onto one World per path."""

    horizon_months: int
    balances: Sequence[tuple[str, Decimal]]
    scheduled: tuple[PreparedTransfer, ...] = ()
    recurring: tuple[PreparedRecurringTransfer, ...] = ()


def compose(case: Situation, rollout_id: int, *, rollout_count: int) -> World:
    """Cash accounts and the configured transfer tables; no security or CPI series is supplied."""
    world = World(
        MarketPath((), rollout_id, rollout_count=rollout_count),
        horizon_months=case.horizon_months,
        income_sources=(ORDINARY_INCOME,),
    )
    for agent_id, balance in case.balances:
        world.declare_account(PreparedAccount(account=checking(agent_id), opening_balance=quanta(balance)))
    world.scheduled_transfers = case.scheduled
    world.recurring_transfers = case.recurring
    return world


def _run(case: Situation, *, rollout_count: int = 1) -> list[Rollout]:
    session = ActionSession(
        {id_: compose(case, id_, rollout_count=rollout_count) for id_ in range(rollout_count)},
        "alice",
        capture="forensic",
    )
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


@pytest.fixture
def alice_bob() -> Situation:
    return Situation(
        horizon_months=1,
        balances=(("alice", Decimal(10)), ("bob", Decimal(20))),
        scheduled=(one_off(0, "bob_gives_alice_5", "bob", "alice", Decimal(5)),),
    )


def test_bob_gives_alice_five_dollars_one_rollout(alice_bob: Situation) -> None:
    [result] = _run(alice_bob)
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
    [result] = _run(Situation(horizon_months=5, balances=(("alice", Decimal(100)),)))
    assert result.trace is not None
    assert [book.month for book in result.trace.books] == list(range(6))
    assert [_cash(book, "alice") for book in result.trace.books] == [10000] * 6
    assert result.trace.events.transfers.is_empty()


def test_rejects_zero_rollout_count(alice_bob: Situation) -> None:
    with pytest.raises(ValueError, match="a session needs at least one world"):
        _run(alice_bob, rollout_count=0)


def test_recurring_paycheck_accrues_monthly() -> None:
    case = Situation(
        horizon_months=12,
        balances=(("alice", Decimal(1000)), ("payroll", Decimal(0))),
        recurring=(monthly("alice_paycheck", "payroll", "alice", Decimal(3000)),),
    )
    [result] = _run(case)
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
    case = Situation(
        horizon_months=10,
        balances=(("alice", Decimal(0)), ("sink", Decimal(0))),
        recurring=(monthly("bounded_pay", "sink", "alice", Decimal(100), end_month=4),),
    )
    [result] = _run(case)
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
    case = Situation(
        horizon_months=24,
        balances=(("alice", Decimal(1000)), ("employer", Decimal(0))),
        recurring=(monthly("alice_paycheck", "employer", "alice", Decimal(2000)),),
    )
    results = _run(case, rollout_count=1000)
    assert [result.rollout_id for result in results] == list(range(1000))
    assert [_cash(result.summary.ending_book, "alice") for result in results] == [4900000] * 1000
    for result in results:
        assert result.trace is not None
        assert result.trace.events.transfers.height == 24
        assert [book.month for book in result.trace.books] == list(range(25))
        assert [_cash(book, "alice") + _cash(book, "employer") for book in result.trace.books] == [100000] * 25


def test_combined_one_off_and_recurring() -> None:
    case = Situation(
        horizon_months=10,
        balances=(("alice", Decimal(0)), ("employer", Decimal(0))),
        scheduled=(one_off(5, "alice_bonus", "employer", "alice", Decimal(5000)),),
        recurring=(monthly("alice_paycheck", "employer", "alice", Decimal(1000)),),
    )
    [result] = _run(case)
    assert _cash(result.summary.ending_book, "alice") == 1500000
    assert result.trace is not None
    transfers = result.trace.events.transfers
    assert transfers.height == 11
    assert sorted(
        (txn["cause_id"], txn["amount_quanta"]) for txn in transfers.iter_rows(named=True) if txn["month_index"] == 5
    ) == [("alice_bonus", 500000), ("alice_paycheck", 100000)]


if __name__ == "__main__":
    pytest_bazel.main()
