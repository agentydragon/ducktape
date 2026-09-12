"""Each month books what the world was declared to hold, with every phase firing together.

Transfers, bills, a scheduled sale, a property purchase and its carrying costs, mortgage
servicing and the year-end tax pass, on worlds composed from declared facts and driven by a
household that pays each account's claims all or none.
"""

from decimal import Decimal

import numpy as np
import pytest_bazel

from finance.augur.model.series import SP500_SYMBOL, SecurityKey
from finance.augur.policy.configured_household import ConfiguredHousehold
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Book
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import (
    currency_amount_to_quanta,
    quantity_scale_for_asset,
    quantity_to_quanta,
    rate_to_ppb,
)
from finance.augur.sim.ids import AgentId
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedJurisdiction,
    PreparedLocation,
    PreparedLot,
    PreparedRecurringObligation,
    PreparedRecurringTransfer,
    PreparedSeries,
    PreparedTransfer,
    _MortgageFinancing,
    _PropertyPurchase,
    _PropertyTax,
    _ScheduledSale,
)
from finance.augur.sim.property import Housing
from finance.augur.sim.scenario import ORDINARY_INCOME, FilingStatus, TaxProfile
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
ALICE = "alice"
CHECKING = "checking"
FEDERAL = "federal_us"
CALIFORNIA = "california"
SP500 = SecurityKey(symbol=SP500_SYMBOL)
SF = PreparedLocation(
    location_id="sf",
    display_name="SF",
    jurisdiction_ids=(FEDERAL, CALIFORNIA),
    annual_property_tax_rate_ppb=rate_to_ppb(0.0118),
    annual_special_assessment=0,
)


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def ref(agent_id: str, account_id: str = CHECKING) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=account_id)


def account(agent_id: str, balance: Decimal | int = 0) -> PreparedAccount:
    return PreparedAccount(account=ref(agent_id), opening_balance=money(balance))


def world_for(
    *accounts: PreparedAccount,
    horizon_months: int,
    jurisdiction_ids: tuple[str, ...] = (),
    series: tuple[PreparedSeries, ...] = (),
) -> World:
    """An empty world holding the declared cash accounts and the tax vocabulary its taxpayers share."""
    world = World(
        MarketPath(series, 0, rollout_count=1),
        horizon_months=horizon_months,
        income_sources=(ORDINARY_INCOME,),
        jurisdictions=tuple(
            PreparedJurisdiction(jurisdiction_id=id_, level=load_jurisdiction(id_).level) for id_ in jurisdiction_ids
        ),
    )
    for opening in accounts:
        world.declare_account(opening)
    return world


def taxed_by(world: World, *jurisdiction_ids: str, prior_year_tax: Decimal = Decimal(0)) -> None:
    world.track(
        TaxAuthority(
            compile_profile(
                TaxProfile(
                    agent_id=ALICE,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=list(jurisdiction_ids),
                    tax_authority_agent_id="irs",
                    prior_year_tax=prior_year_tax,
                ),
                {id_: load_jurisdiction(id_) for id_ in jurisdiction_ids},
                quantum=QUANTUM,
            )
        )
    )


def run(world: World, *, tracked: ConfiguredHousehold | None = None) -> list[Book]:
    """Every month to the horizon or the stop; the books the caller keeps for itself between steps."""
    world.track(ConfiguredHousehold(AgentId(ALICE), ()) if tracked is None else tracked)
    books = [world.book()]
    world.start()
    while not world.finished:
        world.step()
        books.append(world.book())
    return books


def cash(books: list[Book], agent_id: str, month: int) -> int:
    [balance] = [
        row.balance
        for row in books[month].balances
        if (row.account.agent_id, row.account.account_id) == (agent_id, CHECKING)
    ]
    return balance


def gain(books: list[Book], agent_id: str, month: int, *, long_term: bool) -> int:
    return sum(
        row.long_term_gain if long_term else row.short_term_gain
        for row in books[month].capital_gains
        if row.agent_id == agent_id
    )


def paycheck(amount: Decimal | int, *, end_month: int) -> PreparedRecurringTransfer:
    return PreparedRecurringTransfer(
        start_month=0,
        end_month=end_month,
        cause_id="paycheck",
        from_account=ref("payroll"),
        to_account=ref(ALICE),
        amount=money(amount),
        income_category=None,
        deduction_category=None,
    )


def rent(amount: Decimal | int, *, end_month: int) -> PreparedRecurringObligation:
    return PreparedRecurringObligation(
        start_month=0,
        end_month=end_month,
        obligation_id="rent",
        obligation_type="rent",
        from_account=ref(ALICE),
        to_account=ref("landlord"),
        amount_due=money(amount),
        property_id=None,
        deduction_category=None,
        deductible_fraction_ppb=1_000_000_000,
    )


def test_transfers_only_month_loop() -> None:
    # Recurring paycheck for a year + a one-off gift: transfers and nothing else.
    world = world_for(account("payroll"), account(ALICE, 100), account("bob", 500), horizon_months=12)
    world.recurring_transfers = (paycheck(1000, end_month=11),)
    world.scheduled_transfers = (
        PreparedTransfer(
            month=6,
            cause_id="bob_gifts_alice",
            from_account=ref("bob"),
            to_account=ref(ALICE),
            amount=money(250),
            income_category=None,
            deduction_category=None,
        ),
    )
    books = run(world)

    # alice: 100 opening + 12 paychecks of 1000 + a 250 gift = 12350.
    assert cash(books, ALICE, 12) == 1_235_000
    assert cash(books, "bob", 12) == 25_000
    assert cash(books, "payroll", 12) == -1_200_000
    # Mid-horizon snapshot: 6 paychecks landed by month 6 (months 0..5), gift not yet (fires at 6).
    assert cash(books, ALICE, 6) == 610_000


def test_declared_bill_settles_beside_the_paycheck() -> None:
    # Paycheck (transfer) + monthly rent (a tracked biller's claim). Always funded, so nothing stops.
    world = world_for(account("payroll"), account(ALICE, 1000), account("landlord"), horizon_months=12)
    world.recurring_transfers = (paycheck(5000, end_month=11),)
    world.track(Biller(rent(2000, end_month=11)))
    books = run(world)

    # alice: 1000 opening + 12 paychecks of 5000 - 12 rents of 2000 = 37000.
    assert cash(books, ALICE, 12) == 3_700_000
    assert cash(books, "landlord", 12) == 2_400_000
    assert cash(books, "payroll", 12) == -6_000_000


def test_unfundable_bill_stops_the_path_keeping_both_parties_balances() -> None:
    # No income: alice can pay rent in month 0 (1000 -> 400) but not month 1 (needs 600), so the
    # rollout stops at month 1, preserving both parties' actual balances.
    world = world_for(account(ALICE, 1000), account("landlord"), horizon_months=12)
    world.track(Biller(rent(600, end_month=11)))
    books = run(world)

    assert cash(books, ALICE, 1) == 40_000  # after month 0: rent paid (1000 -> 400)
    assert cash(books, "landlord", 1) == 60_000  # month 0's rent landed pre-failure
    assert cash(books, ALICE, 2) == 40_000
    assert cash(books, "landlord", 2) == 60_000
    assert len(books) == 3  # the stopped month is the last one booked


def test_scheduled_sale_books_proceeds_and_a_long_term_gain() -> None:
    # 100 SP500 units bought 24 months pre-horizon at $80, sold at month 3 for $120 — FIFO lot
    # matching, the proceeds credit and the holding-period classification, in one run. A flat price
    # series keeps the assertion exact. The horizon ends before any December, so the profile is what
    # makes the gain reportable rather than what assesses it.
    horizon = 6
    scale = quantity_scale_for_asset(SP500)
    units = int(quantity_to_quanta(100.0, scale=scale))
    series = compile_series(
        ExternalSeriesContext.from_level_blocks(
            [(SP500, np.full((1, horizon + 1), 120.0, dtype=np.float64))], rollout_count=1, horizon_months=horizon
        ),
        rollout_count=1,
        horizon_months=horizon,
        currency_quantum=QUANTUM,
    )
    world = world_for(
        account(ALICE), account("irs"), horizon_months=horizon, jurisdiction_ids=(FEDERAL,), series=series
    )
    taxed_by(world, FEDERAL)
    world.declare_pool(
        PreparedHoldingPool(agent_id=ALICE, account_id="brokerage", asset_id=str(SP500.symbol), quantity_scale=scale)
    )
    world.hold(
        PreparedLot(
            lot_id="alice_sp500",
            agent_id=ALICE,
            account_id="brokerage",
            asset_id=str(SP500.symbol),
            purchase_month=-24,  # long-term when sold at month 3
            quantity_scale=scale,
            units=units,
            basis=money(8000),
        )
    )
    books = run(
        world,
        tracked=ConfiguredHousehold(
            AgentId(ALICE),
            (),
            scheduled_sales=(
                _ScheduledSale(
                    month=3,
                    cause_id="alice_sells_sp500",
                    agent_id=ALICE,
                    account_id="brokerage",
                    asset_id=str(SP500.symbol),
                    units=units,
                    proceeds_account_id=CHECKING,
                ),
            ),
        ),
    )

    assert cash(books, ALICE, 3) == 0  # before the month-3 sale
    assert cash(books, ALICE, 4) == 1_200_000  # proceeds credited after month 3
    # Long-term realized gain = 100 * (120 - 80) = 4000, held in YTD through the (sub-year) horizon.
    assert gain(books, ALICE, 4, long_term=True) == 400_000
    assert gain(books, ALICE, 4, long_term=False) == 0


def purchase(
    *,
    month: int,
    down_payment: Decimal | int,
    buyer_closing_cost: Decimal | int = 0,
    mortgage: _MortgageFinancing | None = None,
) -> _PropertyPurchase:
    """Alice buys a $500k SF home, none of it let, so nothing depreciates."""
    return _PropertyPurchase(
        month=month,
        cause_id="alice_buys_home",
        property_id="home",
        location_id=SF.location_id,
        buyer_agent_id=ALICE,
        buyer_account_id=CHECKING,
        seller_agent_id="seller",
        seller_account_id=CHECKING,
        purchase_price=money(500_000),
        down_payment=money(down_payment),
        buyer_closing_cost=money(buyer_closing_cost),
        rented_fraction_ppb=0,
        land_value_fraction_ppb=rate_to_ppb(0.2),
        mortgage=mortgage,
    )


def test_cash_property_purchase_moves_the_whole_stake() -> None:
    # All-cash home purchase at month 2: the buyer's down payment + closing cost moves to the seller
    # and the property goes active.
    world = world_for(account(ALICE, 600_000), account("seller"), horizon_months=6)
    world.declare_housing(
        Housing(purchases=(purchase(month=2, down_payment=500_000, buyer_closing_cost=10_000),)), (), (SF,)
    )
    books = run(world)

    # stake = down payment + closing = 510k, moved buyer -> seller during month 2 (snapshot index 3).
    assert cash(books, ALICE, 2) == 60_000_000  # before purchase
    assert cash(books, ALICE, 3) == 9_000_000
    assert cash(books, "seller", 3) == 51_000_000


def test_property_tax_accrues_only_once_the_property_is_held() -> None:
    # Cash home purchase at month 0 + a property-tax policy: the monthly ad-valorem tax
    # (assessed 500k × 1.2% / 12 = $500) is the county's claim, starting the month after purchase.
    world = world_for(account(ALICE, 600_000), account("seller"), account("county"), horizon_months=4)
    world.declare_housing(
        Housing(purchases=(purchase(month=0, down_payment=500_000),)),
        (
            _PropertyTax(
                property_id="home",
                owner_agent_id=ALICE,
                from_account_id=CHECKING,
                tax_authority_agent_id="county",
                tax_authority_account_id=CHECKING,
                annual_tax_rate_ppb=rate_to_ppb(0.012),
                start_month=0,
                end_month=None,
            ),
        ),
        (SF,),
    )
    books = run(world)

    # After month 0: 500k purchase, no tax yet (accrues only once owned). Then $500/mo for months 1-3.
    assert cash(books, ALICE, 1) == 10_000_000
    assert cash(books, ALICE, 4) == 9_850_000
    assert cash(books, "county", 4) == 150_000


def test_financed_purchase_originates_then_services_the_loan() -> None:
    # Month 0 originates the loan (down payment moves buyer -> seller, liability principal set),
    # then monthly mortgage-payment claims (interest/principal split) settle buyer -> lender from
    # month 1.
    world = world_for(account(ALICE, 300_000), account("seller"), account("lender"), horizon_months=3)
    world.declare_housing(
        Housing(
            purchases=(
                purchase(
                    month=0,
                    down_payment=100_000,
                    mortgage=_MortgageFinancing(
                        liability_id="alice_mortgage",
                        lender_agent_id="lender",
                        lender_account_id=CHECKING,
                        principal=money(400_000),
                        annual_interest_rate_ppb=rate_to_ppb(0.06),
                        term_months=360,
                    ),
                ),
            )
        ),
        (),
        (SF,),
    )
    books = run(world)

    # After month 0: down payment only (mortgage payments start the month after origination).
    assert cash(books, ALICE, 1) == 20_000_000
    # Months 1 & 2 each pay one mortgage bill to the lender; alice's cash nets both off.
    assert cash(books, "lender", 3) == 479_640
    assert cash(books, ALICE, 3) == 19_520_360


def test_year_end_tax_accrues_and_the_following_year_settles_it() -> None:
    # Multi-year W-2 income + a tax profile with a prior-year tax: the December year-end pass
    # accrues a federal + CA liability, and the following year's estimated-tax and true-up claims
    # settle it.
    horizon = 36
    world = world_for(
        account("payroll"),
        account(ALICE),
        account("irs"),
        horizon_months=horizon,
        jurisdiction_ids=(FEDERAL, CALIFORNIA),
    )
    world.recurring_transfers = (
        PreparedRecurringTransfer(
            start_month=0,
            end_month=35,
            cause_id="alice_paycheck",
            from_account=ref("payroll"),
            to_account=ref(ALICE),
            amount=money(Decimal(120_000) / Decimal(12)),
            income_category=ORDINARY_INCOME,
            deduction_category=None,
        ),
    )
    # > 0 -> quarterly estimated-tax claims the next year.
    taxed_by(world, FEDERAL, CALIFORNIA, prior_year_tax=Decimal(15_000))
    books = run(world)

    assert any(row.jurisdiction_id == FEDERAL and row.amount_owed > 0 for book in books for row in book.tax_liabilities)
    assert cash(books, "irs", horizon) > 0  # estimated payments and true-ups reached the tax authority


if __name__ == "__main__":
    pytest_bazel.main()
