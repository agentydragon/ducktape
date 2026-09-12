"""Explicit household funding, full claim payments, and stopped-path successful prefixes."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import partial

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.policy.cash_band import Raise, cash_band
from finance.augur.policy.funding import fund_claims
from finance.augur.policy.sleeves import withdraw
from finance.augur.sim.actions import ClaimId, DecisionActions, PayClaim, Transfer
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Book
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.observations import Decision
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedLot,
    PreparedObligation,
    PreparedRecurringObligation,
    PreparedRecurringTransfer,
    PreparedSeries,
)
from finance.augur.sim.results import (
    Executed,
    Finished,
    InsufficientCash,
    Paid,
    PaymentRejected,
    Rejected,
    RejectedAction,
    Rollout,
)
from finance.augur.sim.scenario import ORDINARY_INCOME
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import World

VTI = SecurityKey(symbol=SecuritySymbol("vti"))
CHECKING_TARGET = {("checking", "vti"): 1}
QUANTUM = Decimal("0.01")
SCALE = quantity_scale_for_asset(VTI)


def ref(agent_id: str, account_id: str = "checking") -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=account_id)


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(amount, quantum=QUANTUM))


def account(agent_id: str, account_id: str = "checking", balance: Decimal | int = 0) -> PreparedAccount:
    return PreparedAccount(account=ref(agent_id, account_id), opening_balance=money(balance))


def pool(agent_id: str, account_id: str) -> PreparedHoldingPool:
    return PreparedHoldingPool(agent_id=agent_id, account_id=account_id, asset_id=str(VTI.symbol), quantity_scale=SCALE)


def lot(
    lot_id: str,
    agent_id: str,
    account_id: str,
    *,
    quantity: Decimal | int,
    cost_basis: Decimal | int,
    purchase_month: int = -24,
) -> PreparedLot:
    return PreparedLot(
        lot_id=lot_id,
        agent_id=agent_id,
        account_id=account_id,
        asset_id=str(VTI.symbol),
        purchase_month=purchase_month,
        quantity_scale=SCALE,
        units=int(quantity_to_quanta(quantity, scale=SCALE)),
        basis=money(cost_basis),
    )


def bill(
    obligation_id: str,
    obligation_type: str,
    payer: AccountRef,
    payee: AccountRef,
    amount_due: Decimal | int,
    *,
    month: int = 0,
) -> PreparedObligation:
    return PreparedObligation(
        month=month,
        obligation_id=obligation_id,
        obligation_type=obligation_type,
        from_account=payer,
        to_account=payee,
        amount_due=money(amount_due),
        property_id=None,
        deduction_category=None,
        deductible_fraction_ppb=1_000_000_000,
    )


def monthly_bill(
    obligation_id: str,
    obligation_type: str,
    payer: AccountRef,
    payee: AccountRef,
    amount_due: Decimal | int,
    *,
    start_month: int = 0,
) -> PreparedRecurringObligation:
    return PreparedRecurringObligation(
        start_month=start_month,
        end_month=None,
        obligation_id=obligation_id,
        obligation_type=obligation_type,
        from_account=payer,
        to_account=payee,
        amount_due=money(amount_due),
        property_id=None,
        deduction_category=None,
        deductible_fraction_ppb=1_000_000_000,
    )


@dataclass
class Situation:
    """The books and claims every path declares; `compose` puts them on one `World` per price path."""

    horizon_months: int
    accounts: list[PreparedAccount] = field(default_factory=list)
    pools: list[PreparedHoldingPool] = field(default_factory=list)
    lots: list[PreparedLot] = field(default_factory=list)
    claims: list[PreparedObligation | PreparedRecurringObligation] = field(default_factory=list)
    recurring_transfers: tuple[PreparedRecurringTransfer, ...] = ()


def compose(case: Situation, rollout_id: int, *, series: tuple[PreparedSeries, ...], rollout_count: int) -> World:
    world = World(
        MarketPath(series, rollout_id, rollout_count=rollout_count),
        horizon_months=case.horizon_months,
        income_sources=(ORDINARY_INCOME,),
    )
    for opening in case.accounts:
        world.declare_account(opening)
    for holding_pool in case.pools:
        world.declare_pool(holding_pool)
    for holding in case.lots:
        world.hold(holding)
    world.recurring_transfers = case.recurring_transfers
    for claim in case.claims:
        world.track(Biller(claim))
    return world


@pytest.fixture
def rent() -> Situation:
    return Situation(
        horizon_months=1,
        accounts=[account("alice", balance=Decimal(100)), account("landlord")],
        pools=[pool("alice", "checking")],
        lots=[lot("alice_vti", "alice", "checking", quantity=10, cost_basis=Decimal(500))],
        claims=[bill("rent_due", "rent", ref("alice"), ref("landlord"), Decimal(500))],
    )


def _run(
    case: Situation,
    policy: Callable[[list[Decision]], list[DecisionActions]],
    *,
    prices: tuple[tuple[int, ...], ...] = ((100, 100),),
) -> tuple[list[Rollout], list[tuple[int, int]]]:
    rollout_count = len(prices)
    paths = ExternalSeriesContext.from_level_blocks(
        [(VTI, np.asarray(prices, dtype=np.float64))], rollout_count=rollout_count, horizon_months=case.horizon_months
    )
    series = compile_series(
        paths, rollout_count=rollout_count, horizon_months=case.horizon_months, currency_quantum=QUANTUM
    )
    session = ActionSession(
        {id_: compose(case, id_, series=series, rollout_count=rollout_count) for id_ in range(rollout_count)},
        "alice",
        capture="forensic",
    )
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


def test_due_now_obligation_sells_assets_and_settles(rent: Situation) -> None:
    [result], calls = _run(rent, partial(fund_claims, targets=CHECKING_TARGET, cash_account_id="checking"))
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


def test_due_now_obligation_failure_aborts_payment(rent: Situation) -> None:
    rent.lots = []
    [result], _ = _run(rent, _pay_claims)
    _assert_rejected_payment(result, action_index=0, claim_index=0, due=50_000, cash=10_000)
    assert result.summary.unpaid_claims[0].cause_id == "rent_due_m0"
    assert result.summary.unpaid_claims[0].obligation_type == "rent"
    assert result.trace is not None
    assert result.trace.events.transfers.is_empty()
    assert [entry.cause_id for entry in result.trace.journal] == ["opening:alice:checking"]
    opening = result.trace.books[0]
    assert result.summary.ending_book.model_copy(update={"month": opening.month, "failed": False}) == opening
    assert [_cash(result.summary.ending_book, actor) for actor in ("alice", "landlord")] == [10_000, 0]


def test_funding_uses_rollout_specific_prices(rent: Situation) -> None:
    rent.accounts[0] = account("alice")
    results, _ = _run(
        rent, partial(fund_claims, targets=CHECKING_TARGET, cash_account_id="checking"), prices=((100, 100), (200, 200))
    )
    assert [result.rollout_id for result in results] == [0, 1]
    for result, units in zip(results, [5, 2.5], strict=True):
        assert result.stop is None
        assert result.trace is not None
        assert result.trace.events.lot_dispositions.select("units_sold", "proceeds_quanta").rows() == [(units, 50_000)]
        [held] = result.summary.ending_book.lots
        assert held.units_remaining * 2 == int((10 - units) * 2) * held.quantity_scale
        assert result.summary.payments[0].receipt.amount_paid == 50_000
        assert _cash(result.summary.ending_book, "alice") == 0


def test_funding_consumes_only_selected_account_fifo_pool(rent: Situation) -> None:
    rent.accounts[0] = account("alice", "taxable")
    rent.claims[0] = replace(rent.claims[0], from_account=ref("alice", "taxable"), amount_due=money(Decimal(400)))
    rent.lots[0] = lot("alice_vti", "alice", "taxable", quantity=5, cost_basis=Decimal(250))
    rent.lots.append(lot("ira_vti", "alice", "ira", quantity=100, cost_basis=Decimal(5000)))
    rent.pools = [pool("alice", "taxable"), pool("alice", "ira")]
    [result], _ = _run(rent, partial(fund_claims, targets={("taxable", "vti"): 1}, cash_account_id="taxable"))
    assert result.stop is None
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.select("lot_id", "units_sold", "proceeds_quanta").rows() == [
        ("alice_vti", 4, 40_000)
    ]
    lots = {held.account_id: held for held in result.summary.ending_book.lots}
    assert lots["taxable"].units_remaining == lots["taxable"].quantity_scale
    assert lots["taxable"].basis_remaining == 5000
    assert lots["ira"].units_remaining == 100 * lots["ira"].quantity_scale
    assert lots["ira"].basis_remaining == 500_000


def test_funding_sells_from_source_account_into_cash_account(rent: Situation) -> None:
    rent.accounts[0] = account("alice")
    rent.lots[0] = lot("alice_vti", "alice", "taxable", quantity=5, cost_basis=Decimal(250))
    rent.pools = [pool("alice", "taxable")]
    rent.claims[0] = replace(rent.claims[0], amount_due=money(Decimal(400)))
    [result], _ = _run(rent, partial(fund_claims, targets={("taxable", "vti"): 1}, cash_account_id="checking"))
    assert result.stop is None
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.select("units_sold", "proceeds_quanta").rows() == [(4, 40_000)]
    [held] = result.summary.ending_book.lots
    assert held.account_id == "taxable"
    assert held.units_remaining == held.quantity_scale
    assert [_cash(result.summary.ending_book, actor) for actor in ("alice", "landlord")] == [0, 40_000]


def test_funding_covers_monthly_spend_deficit(rent: Situation) -> None:
    rent.accounts[0] = account("alice", balance=Decimal(1000))
    rent.lots[0] = lot("alice_vti", "alice", "checking", quantity=200, cost_basis=Decimal(10000), purchase_month=-1)
    rent.claims = [monthly_bill("alice_rent", "rent", ref("alice"), ref("landlord"), Decimal(5000))]
    rent.horizon_months = 3
    [result], calls = _run(
        rent, partial(fund_claims, targets=CHECKING_TARGET, cash_account_id="checking"), prices=((100,) * 4,)
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
    [held] = result.summary.ending_book.lots
    assert held.units_remaining == 60 * held.quantity_scale


def test_policy_without_sales_fails_even_with_assets(rent: Situation) -> None:
    rent.accounts[0] = account("alice")
    [result], _ = _run(rent, _pay_claims)
    _assert_rejected_payment(result, action_index=0, claim_index=0, due=50_000, cash=0)
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.is_empty()
    opening = result.trace.books[0]
    assert result.summary.ending_book.model_copy(update={"month": opening.month, "failed": False}) == opening


@pytest.mark.parametrize(("cash", "ending_cash", "sales"), [(2500, 650_000, [(50, 500_000)]), (3500, 250_000, [])])
def test_cash_band_uses_balance_after_planned_claims(
    rent: Situation, cash: int, ending_cash: int, sales: list[tuple[int, int]]
) -> None:
    rent.accounts[0] = account("alice", balance=Decimal(cash))
    rent.lots[0] = lot("alice_vti", "alice", "checking", quantity=100, cost_basis=Decimal(5000))
    rent.claims[0] = replace(rent.claims[0], amount_due=money(Decimal(1000)))
    [result], _ = _run(rent, _band_funding)
    assert result.stop is None
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.select("units_sold", "proceeds_quanta").rows() == sales
    assert result.summary.payments[0].receipt.amount_paid == 100_000
    assert _cash(result.summary.ending_book, "alice") == ending_cash


def test_unfundable_optional_cash_band_is_not_a_claim(rent: Situation) -> None:
    rent.accounts[0] = account("alice", balance=Decimal(1000))
    rent.lots = []
    rent.claims = []
    [result], _ = _run(rent, _band_funding)
    assert result.stop is None
    assert result.trace is not None
    assert result.trace.receipts == []
    assert result.summary.unpaid_claims == []
    assert _cash(result.summary.ending_book, "alice") == 100_000


def test_first_payment_survives_later_rejection_and_subsequent_action_is_skipped(rent: Situation) -> None:
    """Two $500 claims against $600 are ordered payments, not an all-or-none group."""
    rent.accounts[0] = account("alice", balance=Decimal(600))
    rent.lots = []
    rent.accounts.append(account("utility"))
    rent.claims.append(
        replace(rent.claims[0], obligation_id="utility_due", obligation_type="utility", to_account=ref("utility"))
    )

    def pay_then_transfer(batch: list[Decision]) -> list[DecisionActions]:
        return [
            DecisionActions(
                response.rollout_id,
                response.month,
                [
                    *response.actions,
                    Transfer(
                        cause_id="must-not-run",
                        from_account=AccountRef(agent_id="alice", account_id="checking"),
                        to_account=AccountRef(agent_id="utility", account_id="checking"),
                        amount=1,
                    ),
                ],
            )
            for response in _pay_claims(batch)
        ]

    [result], _ = _run(rent, pay_then_transfer)
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
def exhaustion(rent: Situation) -> Situation:
    rent.accounts[0] = account("alice")
    rent.lots[0] = lot("alice_vti", "alice", "checking", quantity=5, cost_basis=Decimal(400), purchase_month=-1)
    rent.claims = [monthly_bill("alice_rent", "rent", ref("alice"), ref("landlord"), Decimal(1000))]
    return rent


def test_asset_exhaustion_keeps_successful_sale_and_rejects_full_payment(exhaustion: Situation) -> None:
    [result], _ = _run(exhaustion, partial(fund_claims, targets=CHECKING_TARGET, cash_account_id="checking"))
    _assert_rejected_payment(result, action_index=1, claim_index=0, due=100_000, cash=50_000)
    assert result.trace is not None
    assert [receipt.action.kind for receipt in result.trace.receipts] == ["Sell", "PayClaim"]
    assert isinstance(result.trace.receipts[0].outcome, Executed)
    assert result.trace.events.lot_dispositions.select(
        "units_sold", "proceeds_quanta", "cost_basis_consumed_quanta", "realized_gain_quanta"
    ).rows() == [(5, 50_000, 40_000, 10_000)]
    assert result.trace.events.transfers.is_empty()
    [held] = result.summary.ending_book.lots
    assert held.units_remaining == held.basis_remaining == 0
    assert [_cash(result.summary.ending_book, actor) for actor in ("alice", "landlord")] == [50_000, 0]


def test_failed_path_skips_future_transfers_and_policy_calls_while_other_path_continues(exhaustion: Situation) -> None:
    exhaustion.lots[0] = lot("alice_vti", "alice", "checking", quantity=1, cost_basis=Decimal(80), purchase_month=-1)
    exhaustion.accounts.append(account("employer"))
    exhaustion.recurring_transfers = (
        PreparedRecurringTransfer(
            start_month=1,
            end_month=None,
            cause_id="future_paycheck",
            from_account=ref("employer"),
            to_account=ref("alice"),
            amount=money(Decimal(10000)),
            income_category=ORDINARY_INCOME,
            deduction_category=None,
        ),
    )
    exhaustion.horizon_months = 2
    [stopped, continuing], calls = _run(
        exhaustion,
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
