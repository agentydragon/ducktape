"""Path-indexed cashflows and explicit claim payments through the common session."""

from collections.abc import Sequence
from decimal import Decimal

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import LocationId, RentKey
from finance.augur.sim.actions import DecisionActions, PayClaim
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Book
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, rate_to_ppb
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedIndexedAmount,
    PreparedRecurringObligation,
    PreparedRecurringTransfer,
    PreparedSeries,
)
from finance.augur.sim.results import Finished, Paid, Rollout
from finance.augur.sim.scenario import ORDINARY_INCOME, ObligationType
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import World

RENT = RentKey(location_id=LocationId("san_francisco_ca"))
QUANTUM = Decimal("0.01")
CHECKING = "checking"


def _series(levels: list[list[float]], *, horizon_months: int) -> tuple[PreparedSeries, ...]:
    """The authored rent path as integer index levels, one path per rollout."""

    paths = ExternalSeriesContext.from_level_blocks(
        [(RENT, np.asarray(levels, dtype=np.float64))], rollout_count=len(levels), horizon_months=len(levels[0]) - 1
    )
    return compile_series(paths, rollout_count=len(levels), horizon_months=horizon_months, currency_quantum=QUANTUM)


def _indexed(base_amount: Decimal, *, base_month_index: int, adjustment_period_months: int) -> PreparedIndexedAmount:
    return PreparedIndexedAmount(
        base_amount=int(currency_amount_to_quanta(base_amount, quantum=QUANTUM)),
        series_id=RENT.wire_id,
        base_month_index=base_month_index,
        adjustment_period_months=adjustment_period_months,
    )


def _account(agent_id: str, balance: Decimal) -> PreparedAccount:
    return PreparedAccount(
        account=AccountRef(agent_id=agent_id, account_id=CHECKING),
        opening_balance=int(currency_amount_to_quanta(balance, quantum=QUANTUM)),
    )


def _rent_obligation(amount: PreparedIndexedAmount) -> PreparedRecurringObligation:
    """Alice owes the landlord this amount every month of the horizon."""

    return PreparedRecurringObligation(
        start_month=0,
        end_month=None,
        obligation_id="outside_rent",
        obligation_type=ObligationType.OUTSIDE_RENT,
        from_account=AccountRef(agent_id="alice", account_id=CHECKING),
        to_account=AccountRef(agent_id="landlord", account_id=CHECKING),
        amount_due=amount,
        property_id=None,
        deduction_category=None,
        deductible_fraction_ppb=rate_to_ppb(1.0),
    )


def _tenant_rent(amount: PreparedIndexedAmount) -> PreparedRecurringTransfer:
    """The tenant's monthly payment to alice; alice never decides it."""

    return PreparedRecurringTransfer(
        start_month=0,
        end_month=None,
        cause_id="tenant_rent",
        from_account=AccountRef(agent_id="tenant", account_id=CHECKING),
        to_account=AccountRef(agent_id="alice", account_id=CHECKING),
        amount=amount,
        income_category=None,
        deduction_category=None,
    )


def _compose(
    series: tuple[PreparedSeries, ...],
    rollout_id: int,
    *,
    rollout_count: int,
    horizon_months: int,
    accounts: Sequence[PreparedAccount],
    obligation: PreparedRecurringObligation | None = None,
    transfer: PreparedRecurringTransfer | None = None,
) -> World:
    world = World(
        MarketPath(series, rollout_id, rollout_count=rollout_count),
        horizon_months=horizon_months,
        income_sources=(ORDINARY_INCOME,),
    )
    for account in accounts:
        world.declare_account(account)
    if obligation is not None:
        world.track(Biller(obligation))
    if transfer is not None:
        # A counterparty's cashflow table, not an action: the world moves it in `prepare_month`,
        # before this month's claims are assembled.
        world.recurring_transfers = (transfer,)
    return world


def _rent_worlds(amount: PreparedIndexedAmount, levels: list[list[float]], *, horizon_months: int) -> dict[int, World]:
    series = _series(levels, horizon_months=horizon_months)
    accounts = (_account("alice", Decimal(20_000)), _account("landlord", Decimal(0)))
    return {
        rollout_id: _compose(
            series,
            rollout_id,
            rollout_count=len(levels),
            horizon_months=horizon_months,
            accounts=accounts,
            obligation=_rent_obligation(amount),
        )
        for rollout_id in range(len(levels))
    }


def _run(worlds: dict[int, World]) -> list[Rollout]:
    session = ActionSession(worlds, "alice", capture="forensic")
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(
                [
                    DecisionActions(
                        decision.rollout_id,
                        decision.observation.month,
                        [
                            PayClaim(
                                request_id=index,
                                cause_id=claim.cause_id,
                                claim=claim,
                                from_account=claim.from_account,
                                amount=claim.amount_due,
                            )
                            for index, claim in enumerate(decision.observation.claims)
                        ],
                    )
                    for decision in batch
                ]
            )
        assert all(result.stop is None for result in batch.rollouts)
        return batch.rollouts
    finally:
        session.close()


def _cash(book: Book, agent_id: str) -> int:
    [balance] = [
        row.balance for row in book.balances if (row.account.agent_id, row.account.account_id) == (agent_id, CHECKING)
    ]
    return balance


def test_series_indexed_amount_cannot_fire_before_base_month() -> None:
    amount = _indexed(Decimal(1000), base_month_index=1, adjustment_period_months=12)
    with pytest.raises(ValueError, match="indexed payment precedes its base month"):
        _run(_rent_worlds(amount, [[100.0, 110.0, 120.0]], horizon_months=2))


def test_series_indexed_amount_requires_external_series_coverage() -> None:
    amount = _indexed(Decimal(1000), base_month_index=0, adjustment_period_months=12)
    with pytest.raises(ValueError, match="has no finite level at rollout 0, month 12"):
        _run(_rent_worlds(amount, [[100.0] * 12], horizon_months=13))


def test_series_indexed_amount_rejects_zero_base_level() -> None:
    amount = _indexed(Decimal(1000), base_month_index=0, adjustment_period_months=12)
    with pytest.raises(ZeroDivisionError):
        _run(_rent_worlds(amount, [[0.0, 100.0]], horizon_months=1))


def test_series_indexed_recurring_rent_obligation_resets_yearly_by_rollout() -> None:
    amount = _indexed(Decimal(1000), base_month_index=0, adjustment_period_months=12)
    results = _run(_rent_worlds(amount, [[100.0] * 12 + [110.0] * 2, [100.0] * 12 + [90.0] * 2], horizon_months=13))
    assert [result.rollout_id for result in results] == [0, 1]
    for result, reset, ending_cash in zip(
        results, [110000, 90000], [(690000, 1310000), (710000, 1290000)], strict=True
    ):
        assert result.trace is not None
        accruals = result.trace.events.obligation_accruals
        assert accruals.get_column("month_index").to_list() == list(range(13))
        assert accruals.get_column("amount_due_quanta").to_list() == [100000] * 12 + [reset]
        assert [payment.month for payment in result.summary.payments] == list(range(13))
        assert [payment.receipt.amount_requested for payment in result.summary.payments] == [100000] * 12 + [reset]
        assert all(isinstance(payment.receipt.outcome, Paid) for payment in result.summary.payments)
        assert tuple(_cash(result.summary.ending_book, actor) for actor in ("alice", "landlord")) == ending_cash
        assert [_cash(book, "alice") + _cash(book, "landlord") for book in result.trace.books] == [2000000] * 14
        assert result.trace.events.rollout_failures.is_empty()


def test_series_indexed_recurring_transfer_uses_same_amount_schedule() -> None:
    levels = [[200.0] * 12 + [240.0] * 2]
    amount = _indexed(Decimal(1500), base_month_index=0, adjustment_period_months=12)
    [result] = _run(
        {
            0: _compose(
                _series(levels, horizon_months=13),
                0,
                rollout_count=1,
                horizon_months=13,
                accounts=(_account("tenant", Decimal(20_000)), _account("alice", Decimal(0))),
                transfer=_tenant_rent(amount),
            )
        }
    )
    assert result.trace is not None
    assert result.trace.events.transfers.get_column("month_index").to_list() == list(range(13))
    assert result.trace.events.transfers.get_column("amount_quanta").to_list() == [150000] * 12 + [180000]
    assert [_cash(result.summary.ending_book, actor) for actor in ("alice", "tenant")] == [1980000, 20000]


def test_half_quantum_indexing_funds_same_month_claim_without_losing_cash() -> None:
    """One cent times 3/2 and 5/2 rounds half-up to two and three cents, not zero or even."""
    levels = [[2.0, 3.0, 5.0, 5.0]]
    amount = _indexed(Decimal("0.01"), base_month_index=0, adjustment_period_months=1)
    [result] = _run(
        {
            0: _compose(
                _series(levels, horizon_months=3),
                0,
                rollout_count=1,
                horizon_months=3,
                accounts=(
                    _account("alice", Decimal(0)),
                    _account("landlord", Decimal(0)),
                    _account("tenant", Decimal(1)),
                ),
                obligation=_rent_obligation(amount),
                transfer=_tenant_rent(amount),
            )
        }
    )
    assert result.trace is not None
    assert [
        transfer["amount_quanta"]
        for transfer in result.trace.events.transfers.iter_rows(named=True)
        if transfer["from_agent_id"] == "tenant"
    ] == [1, 2, 3]
    assert [payment.receipt.amount_paid for payment in result.summary.payments] == [1, 2, 3]
    assert [_cash(book, "alice") for book in result.trace.books] == [0] * 4
    assert [_cash(book, "tenant") for book in result.trace.books] == [100, 99, 97, 94]
    assert [_cash(book, "landlord") for book in result.trace.books] == [0, 1, 3, 6]
    assert [sum(_cash(book, actor) for actor in ("alice", "tenant", "landlord")) for book in result.trace.books] == [
        100
    ] * 4


if __name__ == "__main__":
    pytest_bazel.main()
