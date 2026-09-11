"""Exact selections, residual basis and all-or-none cash/lot/tax trade accounting."""

from copy import deepcopy
from dataclasses import dataclass, replace

import pytest
import pytest_bazel

from finance.augur.sim.accounting import Accounting
from finance.augur.sim.actions import Buy, LotSale, Sell
from finance.augur.sim.books import AccountRef, JournalEntry, Posting
from finance.augur.sim.holdings import Holdings
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import MAX_COUNT
from finance.augur.sim.prepared import (
    CompiledRun,
    PreparedAccount,
    PreparedHoldingPool,
    PreparedLot,
    PreparedScenario,
    PreparedSeries,
    _ScheduledSale,
)
from finance.augur.sim.testing.accounting import CASH, EXOGENOUS, HOUSEHOLD, prepared_scenario

BROKERAGE = AccountRef(agent_id=HOUSEHOLD, account_id="brokerage")


@dataclass
class Books:
    scenario: PreparedScenario
    accounting: Accounting
    holdings: Holdings
    market: MarketPath

    def snapshot(self) -> tuple[object, ...]:
        return (
            dict(self.accounting.ledger.balances),
            deepcopy(self.accounting.tax.years),
            list(self.accounting.journal),
            self.accounting.journal_entry_count,
            deepcopy(self.holdings.lots),
            list(self.holdings.dispositions),
            self.holdings.disposition_count,
        )


@pytest.fixture
def books() -> Books:
    base = prepared_scenario()
    scenario = replace(
        base,
        accounts=(*base.accounts, PreparedAccount(account=BROKERAGE, opening_balance=0)),
        tax_profiles=(base.tax_profiles[0],),
        holding_pools=(
            PreparedHoldingPool(agent_id=HOUSEHOLD, account_id="brokerage", asset_id="test_fund", quantity_scale=10),
        ),
        initial_lots=tuple(
            PreparedLot(
                lot_id=id_,
                agent_id=HOUSEHOLD,
                account_id="brokerage",
                asset_id="test_fund",
                purchase_month=month,
                quantity_scale=10,
                units=10,
                basis=basis,
            )
            for month, id_, basis in [(-12, "old", 17), (0, "new", 32)]
        ),
    )
    accounting = Accounting(scenario.accounts, scenario.tax_profiles, scenario.income_sources, capture="forensic")
    holdings = Holdings(scenario, accounting)
    market = MarketPath(
        CompiledRun(
            currency_code="USD",
            currency_quantum="0.01",
            rollout_count=1,
            scenario=scenario,
            series=(PreparedSeries(series_id="security:test_fund", snapshots=26, values=(10,) * 26),),
        ),
        0,
    )
    return Books(scenario, accounting, holdings, market)


def sale(lot: str, units: int) -> Sell:
    return Sell(
        cause_id="sale",
        agent_id=HOUSEHOLD,
        proceeds_account_id="checking",
        asset_id="test_fund",
        lots=(LotSale(account_id="brokerage", lot_id=lot, units=units),),
    )


def both_lots() -> Sell:
    return sale("old", 10).model_copy(update={"lots": (*sale("old", 10).lots, *sale("new", 10).lots)})


def purchase() -> Buy:
    return Buy(
        cause_id="purchase",
        agent_id=HOUSEHOLD,
        cash_account_id="checking",
        holding_account_id="brokerage",
        asset_id="test_fund",
        lot_id="bought",
        quantity_scale=10,
        units=15,
    )


def scheduled(units: int) -> _ScheduledSale:
    return _ScheduledSale(
        month=0,
        cause_id="sale",
        agent_id=HOUSEHOLD,
        account_id="brokerage",
        asset_id="test_fund",
        units=units,
        proceeds_account_id="checking",
    )


def test_exact_selection_is_not_fifo_and_full_lot_basis_reconciles(books: Books) -> None:
    books.holdings.sell(books.accounting, 0, sale("new", 3), price=10)
    assert books.holdings.lots[0].units_remaining == 10
    assert books.holdings.dispositions[0].basis == 10
    books.holdings.sell(books.accounting, 0, sale("new", 7), price=10)
    assert books.holdings.lots[1].units_remaining == books.holdings.lots[1].basis_remaining == 0
    assert books.holdings.dispositions[1].basis == 22
    assert sum(row.proceeds for row in books.holdings.dispositions) == 10
    assert books.accounting.tax.years[HOUSEHOLD].short_term_gain == -22
    assert books.accounting.ledger.trial_balance() == 0
    assert all(row.source_account_id == "brokerage" for row in books.holdings.dispositions)


def test_total_proceeds_use_the_same_basis_and_tax_commit(books: Books) -> None:
    books.holdings.cashout(books.accounting, 0, both_lots(), total=2)
    assert books.accounting.ledger.balance(CASH) == 102
    assert all(lot.units_remaining == lot.basis_remaining == 0 for lot in books.holdings.lots)
    assert [(row.basis, row.proceeds, row.realized_gain) for row in books.holdings.dispositions] == [
        (17, 1, -16),
        (32, 1, -31),
    ]
    assert books.accounting.tax.years[HOUSEHOLD].long_term_gain == -16
    assert books.accounting.tax.years[HOUSEHOLD].short_term_gain == -31
    assert books.accounting.ledger.trial_balance() == 0


@pytest.mark.parametrize("case", range(8))
def test_rejected_total_cashouts_leave_lots_cash_tax_and_capture_unchanged(books: Books, case: int) -> None:
    request = both_lots()
    total = 100
    if case == 0:
        total = -1
    elif case == 1:
        request = request.model_copy(
            update={"lots": (request.lots[0], request.lots[1].model_copy(update={"lot_id": "missing"}))}
        )
    elif case == 2:
        request = request.model_copy(
            update={"lots": (request.lots[0], request.lots[1].model_copy(update={"units": 11}))}
        )
    elif case == 3:
        request = request.model_copy(update={"proceeds_account_id": "missing"})
    elif case == 4:
        books.holdings.disposition_count = (1 << 64) - 2
    elif case == 5:
        books.accounting.journal_entry_count = (1 << 64) - 1
    elif case == 6:
        books.accounting.tax.years[HOUSEHOLD].long_term_gain = MAX_COUNT
    else:
        total = MAX_COUNT
    before = books.snapshot()
    with pytest.raises(
        (ValueError, OverflowError), match=r"sale needs|unknown lot|invalid quantity|unknown declared|overflow"
    ):
        books.holdings.cashout(books.accounting, 0, request, total=total)
    assert books.snapshot() == before


def test_fifo_scheduled_sale_matches_the_same_explicit_selection(books: Books) -> None:
    other = deepcopy(books)
    selected = books.holdings.fifo([1, 0], 13)
    assert selected[0].lot_id == "old"
    books.holdings.sell(books.accounting, 0, sale("old", 13).model_copy(update={"lots": selected}), price=10)
    other.holdings.scheduled_sale(other.accounting, other.market, scheduled(13))
    assert books.snapshot() == other.snapshot()
    assert [(row.lot_id, row.units, row.basis) for row in books.holdings.dispositions] == [
        ("old", 10, 17),
        ("new", 3, 10),
    ]
    assert [(lot.units_remaining, lot.basis_remaining) for lot in books.holdings.lots] == [(0, 0), (7, 22)]
    assert books.accounting.ledger.trial_balance() == 0


@pytest.mark.parametrize("case", range(10))
def test_invalid_exact_lot_requests_leave_every_book_unchanged(books: Books, case: int) -> None:
    request = sale("old", 3)
    selection = request.lots[0]
    changes: list[dict[str, object]] = [
        {"lots": (selection.model_copy(update={"lot_id": "absent"}),)},
        {"lots": (selection.model_copy(update={"account_id": "checking"}),)},
        {"agent_id": "test_intruder"},
        {"asset_id": "test_other_fund"},
        {"lots": (selection.model_copy(update={"units": 0}),)},
        {"lots": (selection.model_copy(update={"units": -1}),)},
        {"lots": (selection.model_copy(update={"units": 11}),)},
        {"lots": (selection, selection)},
        {"proceeds_account_id": "undeclared"},
        {"lots": ()},
    ]
    before = books.snapshot()
    with pytest.raises(ValueError, match=r"unknown|does not belong|invalid quantity|duplicate lot|sale needs"):
        books.holdings.sell(books.accounting, 0, request.model_copy(update=changes[case]), price=10)
    assert books.snapshot() == before


@pytest.mark.parametrize("case", range(5))
def test_overflow_after_first_lot_or_jurisdiction_cannot_partially_commit(books: Books, case: int) -> None:
    if case == 0:
        books.accounting.tax.years[HOUSEHOLD].long_term_gain = MAX_COUNT
    elif case == 1:
        books.holdings.disposition_count = (1 << 64) - 2
    elif case == 2:
        books.accounting.journal_entry_count = (1 << 64) - 1
    elif case == 3:
        books.accounting.ledger.apply(
            JournalEntry(
                month=0,
                cause_id="large cash",
                postings=[
                    Posting(account=CASH, amount=MAX_COUNT - 100),
                    Posting(account=EXOGENOUS, amount=-(MAX_COUNT - 100)),
                ],
            )
        )
    before = books.snapshot()
    with pytest.raises(OverflowError):
        books.holdings.sell(books.accounting, 0, both_lots(), price=MAX_COUNT if case == 4 else 20)
    assert books.snapshot() == before


def test_rejected_scheduled_sale_preserves_every_book(books: Books) -> None:
    books.accounting.journal_entry_count = (1 << 64) - 1
    before = books.snapshot()
    with pytest.raises(OverflowError):
        books.holdings.scheduled_sale(books.accounting, books.market, scheduled(3))
    assert books.snapshot() == before


def test_purchase_posts_cash_and_basis_then_joins_future_exact_sales(books: Books) -> None:
    books.holdings.buy(books.scenario, books.accounting, 0, purchase(), price=10)
    lot = books.holdings.lots[2]
    assert lot.units_remaining == lot.basis_remaining == 15
    assert lot.spec.purchase_month == 0
    assert lot.snapshot().asset_id == "security:test_fund"
    assert books.accounting.ledger.balance(CASH) == 85
    books.holdings.sell(books.accounting, 0, sale("bought", 15), price=20)
    assert books.holdings.dispositions[0].realized_gain == 15
    assert books.accounting.ledger.trial_balance() == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"lot_id": "old"},
        {"units": 101},
        {"units": 0},
        {"units": -1},
        {"quantity_scale": 100},
        {"holding_account_id": "other"},
        {"agent_id": "test_intruder"},
        {"cash_account_id": "other"},
        {},
    ],
)
def test_invalid_or_unfunded_purchase_does_not_create_lot_or_debit_cash(
    books: Books, changes: dict[str, object]
) -> None:
    if not changes:
        books.accounting.journal_entry_count = (1 << 64) - 1
    before = books.snapshot()
    with pytest.raises((ValueError, OverflowError), match=r"purchase|holding pool|unknown declared|overflow"):
        books.holdings.buy(books.scenario, books.accounting, 0, purchase().model_copy(update=changes), price=10)
    assert books.snapshot() == before


def test_oversell_is_rejected_before_any_disposition(books: Books) -> None:
    before = books.snapshot()
    with pytest.raises(ValueError, match="exceeds available"):
        books.holdings.scheduled_sale(books.accounting, books.market, scheduled(21))
    assert books.snapshot() == before


if __name__ == "__main__":
    pytest_bazel.main()
