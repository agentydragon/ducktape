"""Explicit household funding, full claim payments, and stopped-path successful prefixes."""

from collections.abc import Callable
from decimal import Decimal
from functools import partial

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.policy.cash_band import Raise, cash_band
from finance.augur.policy.funding import fund_claims
from finance.augur.policy.sleeves import withdraw
from finance.augur.rust.simulator import Action, ActionSession, Decision, DecisionActions
from finance.augur.sim.books import Book
from finance.augur.sim.results import (
    ClaimId,
    Executed,
    Finished,
    InsufficientCash,
    Paid,
    PaymentRejected,
    Rejected,
    RejectedAction,
    Rollout,
)
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Agent,
    HoldingPool,
    InitialAccountBalance,
    InitialLot,
    RecurringObligation,
    RecurringTransfer,
    Scenario,
    ScheduledObligation,
)
from finance.augur.sim.testing.case import Case

VTI = SecurityKey(symbol=SecuritySymbol("vti"))
CHECKING_TARGET = {("checking", "vti"): 1}


@pytest.fixture
def rent_scenario() -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=100),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance=0),
        ],
        holding_pools=[HoldingPool(agent_id="alice", account_id="checking", asset=VTI)],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti", agent_id="alice", asset=VTI, purchase_month_index=-24, quantity=10, cost_basis=500
            )
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=0,
                obligation_id="rent_due",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due=500,
            )
        ],
        tax_profiles=[],
        horizon_months=1,
    )


def _run(
    scenario: Scenario,
    policy: Callable[[list[Decision]], list[DecisionActions]],
    *,
    prices: tuple[tuple[int, ...], ...] = ((100, 100),),
) -> tuple[list[Rollout], list[tuple[int, int]]]:
    case = Case(scenario=scenario, rollout_count=len(prices), series={VTI: np.asarray(prices, dtype=np.float64)})
    session = ActionSession(case.compiled_run, "alice", list(range(len(prices))), capture="forensic")
    calls: list[tuple[int, int]] = []
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            calls.extend((decision.rollout_id, decision.observation.month) for decision in batch)
            batch = session.advance(policy(batch))
        return batch.rollouts, calls
    finally:
        session.close()


def _cash(book: Book, agent_id: str, account_id: str = "checking") -> int:
    [balance] = [
        row.balance for row in book.balances if (row.account.agent_id, row.account.account_id) == (agent_id, account_id)
    ]
    return balance


def _pay_claims(batch: list[Decision]) -> list[DecisionActions]:
    return [
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


def _band_funding(batch: list[Decision]) -> list[DecisionActions]:
    responses = []
    for decision, payment in zip(batch, _pay_claims(batch), strict=True):
        observation = decision.observation
        proposal = cash_band(
            projected_cash=dict(observation.accounts)["checking"]
            - sum(claim.amount_due for claim in observation.claims),
            floor=200_000,
            ceiling=650_000,
        )
        sales = (
            withdraw(
                observation,
                targets=CHECKING_TARGET,
                cash_account_id="checking",
                amount=proposal.amount,
                cause_id="cash-band-raise",
            )
            if isinstance(proposal, Raise)
            else []
        )
        responses.append(DecisionActions(decision.rollout_id, observation.month, sales + payment.actions))
    return responses


def _assert_rejected_payment(result: Rollout, *, action_index: int, claim_index: int, due: int, cash: int) -> None:
    assert result.stop == RejectedAction(month=0, action_index=action_index)
    assert result.summary.ending_mark_month == 0
    [unpaid] = result.summary.unpaid_claims
    assert unpaid.id == ClaimId(month=0, index=claim_index)
    assert unpaid.amount_due == due
    assert unpaid.from_account.agent_id == "alice"
    payment = result.summary.payments[-1]
    assert payment.month == 0
    assert payment.action_index == action_index
    assert payment.receipt.amount_requested == due
    assert payment.receipt.amount_paid == 0
    assert payment.receipt.outcome == PaymentRejected(reason=InsufficientCash(available=cash))
    assert result.trace is not None
    assert isinstance(result.trace.receipts[-1].outcome, Rejected)


def test_due_now_obligation_sells_assets_and_settles(rent_scenario: Scenario) -> None:
    [result], calls = _run(rent_scenario, partial(fund_claims, targets=CHECKING_TARGET, cash_account_id="checking"))
    assert result.stop is None
    assert calls == [(0, 0)]
    assert result.trace is not None
    assert result.trace.events.obligation_accruals.get_column("amount_due_quanta").to_list() == [50_000]
    assert result.trace.events.lot_dispositions.select("units_sold", "proceeds_quanta").rows() == [(4, 40_000)]
    [payment] = result.summary.payments
    assert payment.cause_id == "rent_due_m0"
    assert payment.receipt.amount_paid == 50_000
    assert isinstance(payment.receipt.outcome, Paid)
    assert [receipt.action.kind for receipt in result.trace.receipts] == ["Sell", "PayClaim"]
    assert [_cash(result.summary.ending_book, actor) for actor in ("alice", "landlord")] == [0, 50_000]


def test_due_now_obligation_failure_aborts_payment(rent_scenario: Scenario) -> None:
    [result], _ = _run(rent_scenario.model_copy(update={"initial_lots": []}), _pay_claims)
    _assert_rejected_payment(result, action_index=0, claim_index=0, due=50_000, cash=10_000)
    assert result.summary.unpaid_claims[0].cause_id == "rent_due_m0"
    assert result.summary.unpaid_claims[0].obligation_type == "rent"
    assert result.trace is not None
    assert result.trace.events.transfers.is_empty()
    assert [entry.cause_id for entry in result.trace.journal] == ["opening:alice:checking"]
    opening = result.trace.books[0]
    assert result.summary.ending_book.model_copy(update={"month": opening.month, "failed": False}) == opening
    assert [_cash(result.summary.ending_book, actor) for actor in ("alice", "landlord")] == [10_000, 0]


def test_funding_uses_rollout_specific_prices(rent_scenario: Scenario) -> None:
    rent_scenario.initial_cash[0].balance = Decimal(0)
    results, _ = _run(
        rent_scenario,
        partial(fund_claims, targets=CHECKING_TARGET, cash_account_id="checking"),
        prices=((100, 100), (200, 200)),
    )
    assert [result.rollout_id for result in results] == [0, 1]
    for result, units in zip(results, [5, 2.5], strict=True):
        assert result.stop is None
        assert result.trace is not None
        assert result.trace.events.lot_dispositions.select("units_sold", "proceeds_quanta").rows() == [(units, 50_000)]
        [lot] = result.summary.ending_book.lots
        assert lot.units_remaining * 2 == int((10 - units) * 2) * lot.quantity_scale
        assert result.summary.payments[0].receipt.amount_paid == 50_000
        assert _cash(result.summary.ending_book, "alice") == 0


def test_funding_consumes_only_selected_account_fifo_pool(rent_scenario: Scenario) -> None:
    rent_scenario.initial_cash[0].account_id = "taxable"
    rent_scenario.initial_cash[0].balance = Decimal(0)
    rent_scenario.scheduled_obligations[0].from_account_id = "taxable"
    rent_scenario.scheduled_obligations[0].amount_due = Decimal(400)
    rent_scenario.initial_lots[0].account_id = "taxable"
    rent_scenario.initial_lots[0].quantity = 5
    rent_scenario.initial_lots[0].cost_basis = Decimal(250)
    rent_scenario.initial_lots.append(
        rent_scenario.initial_lots[0].model_copy(
            update={"lot_id": "ira_vti", "account_id": "ira", "quantity": 100, "cost_basis": Decimal(5000)}
        )
    )
    rent_scenario.holding_pools = [
        HoldingPool(agent_id="alice", account_id=account, asset=VTI) for account in ("taxable", "ira")
    ]
    [result], _ = _run(rent_scenario, partial(fund_claims, targets={("taxable", "vti"): 1}, cash_account_id="taxable"))
    assert result.stop is None
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.select("lot_id", "units_sold", "proceeds_quanta").rows() == [
        ("alice_vti", 4, 40_000)
    ]
    lots = {lot.account_id: lot for lot in result.summary.ending_book.lots}
    assert lots["taxable"].units_remaining == lots["taxable"].quantity_scale
    assert lots["taxable"].basis_remaining == 5000
    assert lots["ira"].units_remaining == 100 * lots["ira"].quantity_scale
    assert lots["ira"].basis_remaining == 500_000


def test_funding_sells_from_source_account_into_cash_account(rent_scenario: Scenario) -> None:
    rent_scenario.initial_cash[0].balance = Decimal(0)
    rent_scenario.initial_lots[0].account_id = "taxable"
    rent_scenario.initial_lots[0].quantity = 5
    rent_scenario.initial_lots[0].cost_basis = Decimal(250)
    rent_scenario.holding_pools[0].account_id = "taxable"
    rent_scenario.scheduled_obligations[0].amount_due = Decimal(400)
    [result], _ = _run(rent_scenario, partial(fund_claims, targets={("taxable", "vti"): 1}, cash_account_id="checking"))
    assert result.stop is None
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.select("units_sold", "proceeds_quanta").rows() == [(4, 40_000)]
    [lot] = result.summary.ending_book.lots
    assert lot.account_id == "taxable"
    assert lot.units_remaining == lot.quantity_scale
    assert [_cash(result.summary.ending_book, actor) for actor in ("alice", "landlord")] == [0, 40_000]


def test_funding_covers_monthly_spend_deficit(rent_scenario: Scenario) -> None:
    rent_scenario.initial_cash[0].balance = Decimal(1000)
    rent_scenario.initial_lots[0].quantity = 200
    rent_scenario.initial_lots[0].cost_basis = Decimal(10000)
    rent_scenario.initial_lots[0].purchase_month_index = -1
    rent_scenario.scheduled_obligations = []
    rent_scenario.recurring_obligations = [
        RecurringObligation(
            start_month=0,
            obligation_id="alice_rent",
            obligation_type="rent",
            agent_id="alice",
            from_account_id="checking",
            to_agent_id="landlord",
            to_account_id="checking",
            amount_due=5000,
        )
    ]
    rent_scenario.horizon_months = 3
    [result], calls = _run(
        rent_scenario, partial(fund_claims, targets=CHECKING_TARGET, cash_account_id="checking"), prices=((100,) * 4,)
    )
    assert result.stop is None
    assert calls == [(0, 0), (0, 1), (0, 2)]
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.select("units_sold", "proceeds_quanta").rows() == [
        (40, 400_000),
        (50, 500_000),
        (50, 500_000),
    ]
    assert [payment.receipt.amount_paid for payment in result.summary.payments] == [500_000] * 3
    assert [_cash(book, "alice") for book in result.trace.books] == [100_000, 0, 0, 0]
    [lot] = result.summary.ending_book.lots
    assert lot.units_remaining == 60 * lot.quantity_scale


def test_policy_without_sales_fails_even_with_assets(rent_scenario: Scenario) -> None:
    rent_scenario.initial_cash[0].balance = Decimal(0)
    [result], _ = _run(rent_scenario, _pay_claims)
    _assert_rejected_payment(result, action_index=0, claim_index=0, due=50_000, cash=0)
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.is_empty()
    opening = result.trace.books[0]
    assert result.summary.ending_book.model_copy(update={"month": opening.month, "failed": False}) == opening


@pytest.mark.parametrize(("cash", "ending_cash", "sales"), [(2500, 650_000, [(50, 500_000)]), (3500, 250_000, [])])
def test_cash_band_uses_balance_after_planned_claims(
    rent_scenario: Scenario, cash: int, ending_cash: int, sales: list[tuple[int, int]]
) -> None:
    rent_scenario.initial_cash[0].balance = Decimal(cash)
    rent_scenario.initial_lots[0].quantity = 100
    rent_scenario.initial_lots[0].cost_basis = Decimal(5000)
    rent_scenario.scheduled_obligations[0].amount_due = Decimal(1000)
    [result], _ = _run(rent_scenario, _band_funding)
    assert result.stop is None
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.select("units_sold", "proceeds_quanta").rows() == sales
    assert result.summary.payments[0].receipt.amount_paid == 100_000
    assert _cash(result.summary.ending_book, "alice") == ending_cash


def test_unfundable_optional_cash_band_is_not_a_claim(rent_scenario: Scenario) -> None:
    rent_scenario.initial_cash[0].balance = Decimal(1000)
    rent_scenario.initial_lots = []
    rent_scenario.scheduled_obligations = []
    [result], _ = _run(rent_scenario, _band_funding)
    assert result.stop is None
    assert result.trace is not None
    assert result.trace.receipts == []
    assert result.summary.unpaid_claims == []
    assert _cash(result.summary.ending_book, "alice") == 100_000


def test_first_payment_survives_later_rejection_and_subsequent_action_is_skipped(rent_scenario: Scenario) -> None:
    """Two $500 claims against $600 are ordered payments, not an all-or-none group."""
    rent_scenario.initial_cash[0].balance = Decimal(600)
    rent_scenario.initial_lots = []
    rent_scenario.agents.append(Agent(agent_id="utility"))
    rent_scenario.initial_cash.append(InitialAccountBalance(agent_id="utility", account_id="checking", balance=0))
    rent_scenario.scheduled_obligations.append(
        rent_scenario.scheduled_obligations[0].model_copy(
            update={"obligation_id": "utility_due", "obligation_type": "utility", "to_agent_id": "utility"}
        )
    )

    def pay_then_transfer(batch: list[Decision]) -> list[DecisionActions]:
        return [
            DecisionActions(
                response.rollout_id,
                response.month,
                [*response.actions, Action.transfer("must-not-run", ("alice", "checking"), ("utility", "checking"), 1)],
            )
            for response in _pay_claims(batch)
        ]

    [result], _ = _run(rent_scenario, pay_then_transfer)
    _assert_rejected_payment(result, action_index=1, claim_index=1, due=50_000, cash=10_000)
    assert result.summary.unpaid_claims[0].cause_id == "utility_due_m0"
    assert [payment.receipt.amount_paid for payment in result.summary.payments] == [50_000, 0]
    assert [_cash(result.summary.ending_book, actor) for actor in ("alice", "landlord", "utility")] == [
        10_000,
        50_000,
        0,
    ]
    assert result.trace is not None
    assert len(result.trace.receipts) == 2
    assert isinstance(result.trace.receipts[0].outcome, Executed)
    assert result.trace.events.transfers.get_column("amount_quanta").to_list() == [50_000]


@pytest.fixture
def exhaustion_scenario(rent_scenario: Scenario) -> Scenario:
    rent_scenario.initial_cash[0].balance = Decimal(0)
    rent_scenario.initial_lots[0].quantity = 5
    rent_scenario.initial_lots[0].cost_basis = Decimal(400)
    rent_scenario.initial_lots[0].purchase_month_index = -1
    rent_scenario.scheduled_obligations = []
    rent_scenario.recurring_obligations = [
        RecurringObligation(
            start_month=0,
            obligation_id="alice_rent",
            obligation_type="rent",
            agent_id="alice",
            from_account_id="checking",
            to_agent_id="landlord",
            to_account_id="checking",
            amount_due=1000,
        )
    ]
    return rent_scenario


def test_asset_exhaustion_keeps_successful_sale_and_rejects_full_payment(exhaustion_scenario: Scenario) -> None:
    [result], _ = _run(exhaustion_scenario, partial(fund_claims, targets=CHECKING_TARGET, cash_account_id="checking"))
    _assert_rejected_payment(result, action_index=1, claim_index=0, due=100_000, cash=50_000)
    assert result.trace is not None
    assert [receipt.action.kind for receipt in result.trace.receipts] == ["Sell", "PayClaim"]
    assert isinstance(result.trace.receipts[0].outcome, Executed)
    assert result.trace.events.lot_dispositions.select(
        "units_sold", "proceeds_quanta", "cost_basis_consumed_quanta", "realized_gain_quanta"
    ).rows() == [(5, 50_000, 40_000, 10_000)]
    assert result.trace.events.transfers.is_empty()
    [lot] = result.summary.ending_book.lots
    assert lot.units_remaining == lot.basis_remaining == 0
    assert [_cash(result.summary.ending_book, actor) for actor in ("alice", "landlord")] == [50_000, 0]


def test_failed_path_skips_future_transfers_and_policy_calls_while_other_path_continues(
    exhaustion_scenario: Scenario,
) -> None:
    exhaustion_scenario.initial_lots[0].quantity = 1
    exhaustion_scenario.initial_lots[0].cost_basis = Decimal(80)
    exhaustion_scenario.agents.append(Agent(agent_id="employer"))
    exhaustion_scenario.initial_cash.append(
        InitialAccountBalance(agent_id="employer", account_id="checking", balance=0)
    )
    exhaustion_scenario.recurring_transfers = [
        RecurringTransfer(
            start_month=1,
            cause_id="future_paycheck",
            from_agent_id="employer",
            from_account_id="checking",
            to_agent_id="alice",
            to_account_id="checking",
            amount=10000,
            income_category=ORDINARY_INCOME,
        )
    ]
    exhaustion_scenario.horizon_months = 2
    [stopped, continuing], calls = _run(
        exhaustion_scenario,
        partial(fund_claims, targets=CHECKING_TARGET, cash_account_id="checking"),
        prices=((100, 100, 100), (1000, 1000, 1000)),
    )
    assert [stopped.rollout_id, continuing.rollout_id] == [0, 1]
    assert calls == [(0, 0), (1, 0), (1, 1)]
    _assert_rejected_payment(stopped, action_index=1, claim_index=0, due=100_000, cash=10_000)
    assert stopped.trace is not None
    assert stopped.trace.events.transfers.is_empty()
    assert stopped.trace.events.lot_dispositions.get_column("proceeds_quanta").to_list() == [10_000]
    assert stopped.trace.events.obligation_accruals.get_column("month_index").to_list() == [0]
    assert {receipt.month for receipt in stopped.trace.receipts} == {0}
    assert {entry.month for entry in stopped.trace.journal} == {0}
    assert len(stopped.trace.books) == 2
    assert [_cash(stopped.summary.ending_book, actor) for actor in ("alice", "employer", "landlord")] == [10_000, 0, 0]
    assert continuing.stop is None
    assert continuing.summary.ending_mark_month == 2
    assert [payment.receipt.amount_paid for payment in continuing.summary.payments] == [100_000, 100_000]
    assert [_cash(continuing.summary.ending_book, actor) for actor in ("alice", "employer", "landlord")] == [
        900_000,
        -1_000_000,
        200_000,
    ]


if __name__ == "__main__":
    pytest_bazel.main()
