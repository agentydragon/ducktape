"""A tracked home loan from origination to payoff: the installment, the carry, and the sale that closes it.

Every fact is read from the world the test composed — the books it keeps between months, the
property component's own purchase and sale outcomes, and the app's per-month property mark.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import HomeValueKey, LocationId
from finance.augur.product.household import ConfiguredHousehold
from finance.augur.sim.actions import ClaimId, PayClaim
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Book, JournalEntry
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, rate_to_ppb
from finance.augur.sim.ids import AgentId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedLocation,
    PreparedObligation,
    PreparedSeries,
    _MortgageFinancing,
    _PropertyPurchase,
    _PropertySale,
    _PropertyTax,
)
from finance.augur.sim.product_metrics import product_row
from finance.augur.sim.property import Housing, Purchase, Sale
from finance.augur.sim.results import Rejected
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
ALICE = "alice"
BOB = "bob"
CHECKING = "checking"
SF = PreparedLocation(
    location_id="sf",
    display_name="San Francisco",
    jurisdiction_ids=(),
    annual_property_tax_rate_ppb=rate_to_ppb(0.0118),
    annual_special_assessment=0,
)
SF_HOME = HomeValueKey(location_id=LocationId("sf"))
PROPERTY_VALUE = 3  # product_row's `property_value_quanta` slot.


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def ref(agent_id: str) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=CHECKING)


def account(agent_id: str, balance: Decimal | int = 0) -> PreparedAccount:
    return PreparedAccount(account=ref(agent_id), opening_balance=money(balance))


def home_value(*paths: list[Decimal | int], horizon_months: int) -> tuple[PreparedSeries, ...]:
    return compile_series(
        ExternalSeriesContext.from_level_blocks(
            [(SF_HOME, np.asarray([[float(level) for level in path] for path in paths], dtype=np.float64))],
            rollout_count=len(paths),
            horizon_months=horizon_months,
        ),
        rollout_count=len(paths),
        horizon_months=horizon_months,
        currency_quantum=QUANTUM,
    )


def financing(*, borrower: str, principal: Decimal | int, annual_rate: float, term_months: int) -> _MortgageFinancing:
    return _MortgageFinancing(
        liability_id=f"{borrower}-loan",
        lender_agent_id="bank",
        lender_account_id=CHECKING,
        principal=money(principal),
        annual_interest_rate_ppb=rate_to_ppb(annual_rate),
        term_months=term_months,
    )


def home(
    *,
    buyer: str,
    month: int,
    purchase_price: Decimal | int,
    down_payment: Decimal | int,
    buyer_closing_cost: Decimal | int = 0,
    mortgage: _MortgageFinancing | None,
) -> _PropertyPurchase:
    return _PropertyPurchase(
        month=month,
        cause_id=f"{buyer}-buys-home",
        property_id=f"{buyer}-home",
        location_id=SF.location_id,
        buyer_agent_id=buyer,
        buyer_account_id=CHECKING,
        seller_agent_id="seller",
        seller_account_id=CHECKING,
        purchase_price=money(purchase_price),
        down_payment=money(down_payment),
        buyer_closing_cost=money(buyer_closing_cost),
        rented_fraction_ppb=0,
        land_value_fraction_ppb=rate_to_ppb(0.2),
        mortgage=mortgage,
    )


def compose(
    *accounts: PreparedAccount,
    horizon_months: int,
    housing: Housing,
    tax_policies: tuple[_PropertyTax, ...] = (),
    series: tuple[PreparedSeries, ...] = (),
    rollout_id: int = 0,
    rollout_count: int = 1,
) -> World:
    world = World(MarketPath(series, rollout_id, rollout_count=rollout_count), horizon_months=horizon_months)
    for opening in accounts:
        world.declare_account(opening)
    world.declare_housing(housing, tax_policies, (SF,))
    return world


@dataclass
class Recorded:
    """What the caller keeps between months; the world holds only the current one."""

    books: list[Book]
    rows: list[tuple[int, ...]]
    journal: list[JournalEntry] = field(default_factory=list)
    purchases: list[Purchase] = field(default_factory=list)
    sales: list[Sale] = field(default_factory=list)

    @classmethod
    def opening(cls, world: World) -> Recorded:
        return cls(books=[world.book()], rows=[product_row(world, ALICE)])

    def month(self, world: World) -> None:
        """Read the closed month's outcomes, before the next one clears them."""
        self.journal.extend(world.accounting.journal)
        if world.properties is not None:
            self.purchases.extend(world.properties.purchases)
            self.sales.extend(world.properties.sales)
        self.books.append(world.book())
        self.rows.append(product_row(world, ALICE))


def drive(world: World, *payers: str) -> Recorded:
    """Each named payer settles its own claims in registration order; a rejection stops the path.

    Payer order is the test's: a rejected payment stops the whole rollout, so whoever pays after
    the payer that cannot fund its month never acts.
    """
    recorded = Recorded.opening(world)
    world.start()
    while not world.finished:
        world.begin_actions([])
        for payer in payers:
            for index, unpaid in enumerate(world.unpaid_claims(payer)):
                receipt = world.execute(
                    payer,
                    PayClaim(
                        request_id=index + 1,
                        cause_id=unpaid.cause_id,
                        claim=ClaimId(month=unpaid.id.month, index=unpaid.id.index),
                        from_account=unpaid.from_account,
                        amount=unpaid.amount_due,
                    ),
                )
                if isinstance(receipt.outcome, Rejected):
                    break
            if world.failed:
                break
        world.close_month()
        recorded.month(world)
        if not world.finished:
            world.open_month()
    return recorded


def run(world: World) -> Recorded:
    """One tracked household paying each account's claims all or none, month by month."""
    world.track(ConfiguredHousehold(AgentId(ALICE), ()))
    recorded = Recorded.opening(world)
    world.start()
    while not world.finished:
        world.step()
        recorded.month(world)
    return recorded


def cash(book: Book) -> dict[str, int]:
    return {row.account.agent_id: row.balance for row in book.balances if row.account.account_id == CHECKING}


def balanced(journal: list[JournalEntry]) -> bool:
    return all(sum(posting.amount for posting in entry.postings) == 0 for entry in journal)


def test_financed_purchase_and_first_installment_match_contract() -> None:
    world = compose(
        account(ALICE, 120_000),
        account("seller"),
        account("bank"),
        account("county"),
        horizon_months=2,
        housing=Housing(
            purchases=(
                home(
                    buyer=ALICE,
                    month=0,
                    purchase_price=500_000,
                    down_payment=100_000,
                    buyer_closing_cost=10_000,
                    mortgage=financing(borrower=ALICE, principal=400_000, annual_rate=0.06, term_months=360),
                ),
            )
        ),
        tax_policies=(
            _PropertyTax(
                property_id=f"{ALICE}-home",
                owner_agent_id=ALICE,
                from_account_id=CHECKING,
                tax_authority_agent_id="county",
                tax_authority_account_id=CHECKING,
                annual_tax_rate_ppb=rate_to_ppb(0.012),
                start_month=0,
                end_month=None,
            ),
        ),
    )
    recorded = run(world)

    assert not recorded.books[0].mortgages
    opening, ending = recorded.books[1], recorded.books[2]
    assert opening.properties[0].adjusted_basis == 51_000_000
    assert opening.mortgages[0].monthly_payment == 239_820
    assert opening.mortgages[0].principal == 40_000_000
    assert ending.mortgages[0].principal == 39_960_180
    assert ending.mortgages[0].interest_paid_ytd == 200_000
    assert cash(ending) == {"alice": 710_180, "seller": 11_000_000, "bank": 239_820, "county": 50_000}
    assert balanced(recorded.journal)


@pytest.mark.parametrize("closing_cost_pct", [0, 10])
@pytest.mark.parametrize("financed", [False, True])
def test_sale_pays_off_ledger_principal_before_the_sale_months_installment(
    closing_cost_pct: int, financed: bool
) -> None:
    # Two price paths that meet at the purchase month: the sale is struck against the same
    # purchase anchor on both, so every outcome below is path-independent.
    horizon = 6
    series = home_value([50, 100, 200, 240, 300, 360, 800], [500, 7, 200, 240, 300, 360, 800], horizon_months=horizon)
    housing = Housing(
        purchases=(
            home(
                buyer=ALICE,
                month=2,
                purchase_price=1000,
                down_payment=400 if financed else 1000,
                mortgage=financing(borrower=ALICE, principal=600, annual_rate=0.0, term_months=60)
                if financed
                else None,
            ),
        ),
        sales=(
            _PropertySale(
                month=5,
                property_id=f"{ALICE}-home",
                closing_cost_ppb=rate_to_ppb(float(Decimal(closing_cost_pct) / 100)),
            ),
        ),
    )
    seller_cost = 18_000 if closing_cost_pct else 0
    payoff = 58_000 if financed else 0
    for rollout_id in range(2):
        world = compose(
            account(ALICE, 2000),
            account("seller"),
            account("bank"),
            horizon_months=horizon,
            housing=housing,
            series=series,
            rollout_id=rollout_id,
            rollout_count=2,
        )
        recorded = run(world)
        books = recorded.books
        assert world.failed_month is None
        [purchase_outcome] = recorded.purchases
        assert purchase_outcome.purchase_price == 100_000
        assert books[3].properties[0].adjusted_basis == 100_000
        assert [row[PROPERTY_VALUE] for row in recorded.rows] == [0, 0, 0, 120_000, 150_000, 180_000, 0]
        [sale] = recorded.sales
        assert sale.gross_proceeds == 180_000 - seller_cost
        assert sale.mortgage_payoff == payoff
        assert sale.net_cash_to_owner == 180_000 - seller_cost - payoff
        assert sale.realized_gain == 80_000 - seller_cost
        assert sale.depreciation_recapture == sale.section_121_exclusion == 0
        assert sale.gross_proceeds + seller_cost == recorded.rows[5][PROPERTY_VALUE]
        assert balanced(recorded.journal)
        assert not books[2].mortgages
        if financed:
            assert [book.mortgages[0].principal for book in books[3:]] == [60_000, 59_000, 58_000, 0]
            assert not books[-1].mortgages[0].active
        else:
            assert not any(book.mortgages for book in books[3:])
        assert cash(books[-1])["alice"] == 158_000 + (122_000 if closing_cost_pct == 0 else 104_000)
        # A configured payoff closes the lender's funding control, not its cash account.
        assert cash(books[-1])["bank"] == (2000 if financed else 0)


@pytest.mark.parametrize("fail_year_end", [False, True])
def test_paid_groups_update_entities_but_a_failed_year_end_does_not_reset_interest(fail_year_end: bool) -> None:
    accounts = [account(ALICE, 300_000), account(BOB, 300_000), account("seller"), account("bank")]
    world = compose(
        *accounts,
        horizon_months=12,
        housing=Housing(
            purchases=tuple(
                home(
                    buyer=buyer,
                    month=0,
                    purchase_price=500_000,
                    down_payment=100_000,
                    buyer_closing_cost=10_000,
                    mortgage=financing(borrower=buyer, principal=400_000, annual_rate=0.06, term_months=360),
                )
                for buyer in (ALICE, BOB)
            )
        ),
    )
    if fail_year_end:
        world.track(
            Biller(
                PreparedObligation(
                    month=11,
                    obligation_id="unfundable",
                    obligation_type="cash_spend",
                    from_account=ref(ALICE),
                    to_account=ref("seller"),
                    amount_due=money(1_000_000),
                    property_id=None,
                    deduction_category=None,
                    deductible_fraction_ppb=1_000_000_000,
                )
            )
        )
    books = drive(world, BOB, ALICE).books

    previous, ending = books[-2:]
    before = {loan.liability_id: loan for loan in previous.mortgages}
    after = {loan.liability_id: loan for loan in ending.mortgages}
    assert (world.failed_month is not None) == fail_year_end
    assert after["bob-loan"].principal < before["bob-loan"].principal
    if fail_year_end:
        assert after["alice-loan"].principal == before["alice-loan"].principal
        assert after["alice-loan"].interest_paid_ytd == before["alice-loan"].interest_paid_ytd > 0
        assert after["bob-loan"].interest_paid_ytd > before["bob-loan"].interest_paid_ytd
    else:
        assert all(loan.interest_paid_ytd == 0 for loan in after.values())
    for id_, loan in after.items():
        [liability] = [row.balance for row in ending.balances if row.account.account_id == f"liability:mortgage:{id_}"]
        assert liability == -loan.principal


if __name__ == "__main__":
    pytest_bazel.main()
