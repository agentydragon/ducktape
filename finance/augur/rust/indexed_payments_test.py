"""Path-indexed cashflows and explicit claim payments through the common session."""

import json
from decimal import Decimal

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import LocationId, RentKey
from finance.augur.rust.simulator import Action, ActionSession, DecisionActions
from finance.augur.sim.books import Book
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.results import Finished, Paid, Rollout
from finance.augur.sim.scenario import (
    Agent,
    InitialAccountBalance,
    RecurringObligation,
    RecurringTransfer,
    Scenario,
    SeriesIndexedAmount,
)
from finance.augur.sim.testing.case import Case

RENT = RentKey(location_id=LocationId("san_francisco_ca"))


def _rent_scenario(amount: SeriesIndexedAmount, *, horizon_months: int) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=20000),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance=0),
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                obligation_id="outside_rent",
                obligation_type="outside_rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due=amount,
            )
        ],
        tax_profiles=[],
        horizon_months=horizon_months,
    )


def _run(scenario: Scenario, levels: list[list[float]]) -> list[Rollout]:
    case = Case(
        scenario=scenario,
        rollout_count=len(levels),
        paths=ExternalSeriesContext.from_level_blocks(
            [(RENT, np.asarray(levels, dtype=np.float64))], rollout_count=len(levels), horizon_months=len(levels[0]) - 1
        ),
    )
    session = ActionSession(
        json.dumps(case.compiled_run.execution_input), "alice", list(range(case.rollout_count)), capture="forensic"
    )
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(
                [
                    DecisionActions(
                        decision.rollout_id,
                        decision.observation.month,
                        [
                            Action.pay_claim(index, claim.cause_id, claim, claim.from_account, claim.amount_due)
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
        row.balance for row in book.balances if (row.account.agent_id, row.account.account_id) == (agent_id, "checking")
    ]
    return balance


def test_series_indexed_amount_cannot_fire_before_base_month() -> None:
    scenario = _rent_scenario(
        SeriesIndexedAmount(base_amount=1000, series=RENT, base_month_index=1, adjustment_period_months=12),
        horizon_months=2,
    )
    with pytest.raises(ValueError, match="before base month 1"):
        _run(scenario, [[100.0, 110.0, 120.0]])


def test_series_indexed_amount_requires_external_series_coverage() -> None:
    scenario = _rent_scenario(
        SeriesIndexedAmount(base_amount=1000, series=RENT, base_month_index=0, adjustment_period_months=12),
        horizon_months=13,
    )
    with pytest.raises(KeyError, match="missing rollout"):
        _run(scenario, [[100.0] * 12])


def test_series_indexed_amount_rejects_zero_base_level() -> None:
    scenario = _rent_scenario(
        SeriesIndexedAmount(base_amount=1000, series=RENT, base_month_index=0, adjustment_period_months=12),
        horizon_months=1,
    )
    with pytest.raises(ValueError, match="zero base level"):
        _run(scenario, [[0.0, 100.0]])


def test_series_indexed_recurring_rent_obligation_resets_yearly_by_rollout() -> None:
    scenario = _rent_scenario(
        SeriesIndexedAmount(base_amount=1000, series=RENT, base_month_index=0, adjustment_period_months=12),
        horizon_months=13,
    )
    results = _run(scenario, [[100.0] * 12 + [110.0] * 2, [100.0] * 12 + [90.0] * 2])
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
    scenario = Scenario(
        agents=[Agent(agent_id="tenant"), Agent(agent_id="alice")],
        initial_cash=[
            InitialAccountBalance(agent_id="tenant", account_id="checking", balance=20000),
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                cause_id="tenant_rent",
                from_agent_id="tenant",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount=SeriesIndexedAmount(
                    base_amount=1500, series=RENT, base_month_index=0, adjustment_period_months=12
                ),
            )
        ],
        tax_profiles=[],
        horizon_months=13,
    )
    [result] = _run(scenario, [[200.0] * 12 + [240.0] * 2])
    assert result.trace is not None
    assert result.trace.events.transfers.get_column("month_index").to_list() == list(range(13))
    assert result.trace.events.transfers.get_column("amount_quanta").to_list() == [150000] * 12 + [180000]
    assert [_cash(result.summary.ending_book, actor) for actor in ("alice", "tenant")] == [1980000, 20000]


def test_half_quantum_indexing_funds_same_month_claim_without_losing_cash() -> None:
    """One cent times 3/2 and 5/2 rounds half-up to two and three cents, not zero or even."""
    amount = SeriesIndexedAmount(
        base_amount=Decimal("0.01"), series=RENT, base_month_index=0, adjustment_period_months=1
    )
    rent = _rent_scenario(amount, horizon_months=3)
    scenario = rent.model_copy(
        update={
            "agents": [Agent(agent_id=actor) for actor in ("alice", "landlord", "tenant")],
            "initial_cash": [
                InitialAccountBalance(agent_id=actor, account_id="checking", balance=1 if actor == "tenant" else 0)
                for actor in ("alice", "landlord", "tenant")
            ],
            "recurring_transfers": [
                RecurringTransfer(
                    start_month=0,
                    cause_id="tenant_rent",
                    from_agent_id="tenant",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=amount,
                )
            ],
        }
    )
    [result] = _run(scenario, [[2.0, 3.0, 5.0, 5.0]])
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
