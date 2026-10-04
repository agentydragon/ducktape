"""Each month books what the world was declared to hold, with every phase firing together.

Transfers, bills, a security sale, a property purchase and its carrying costs, and mortgage
servicing, on worlds composed from declared facts and driven by a household that pays every
due claim in full, in order.
"""

from decimal import Decimal

import numpy as np
import pytest_bazel

from finance.augur.model.series import SP500_SYMBOL, LocationId, SecurityKey
from finance.augur.policy.funding import ClaimPayer
from finance.augur.sim.actions import LotSale, Sell
from finance.augur.sim.agent import EconomicAgent
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Book
from finance.augur.sim.fixed_point import quantity_scale_for_asset, quantity_to_quanta, rate_to_ppb
from finance.augur.sim.ids import AccountId, AgentId, AssetId, JurisdictionId, LiabilityId, LotId, PropertyId
from finance.augur.sim.income import ORDINARY_INCOME
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath, Series
from finance.augur.sim.money import USD
from finance.augur.sim.property import Housing, MortgageFinancing, ScheduledPurchase
from finance.augur.sim.property_tax import PropertyTaxPolicy
from finance.augur.sim.schedule import Once, Recurring
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tax_indexation import FixedNominalLaw
from finance.augur.sim.tax_profile import FilingStatus, TaxProfile, compile_profile
from finance.augur.sim.testing.scripted import Scripted
from finance.augur.sim.testing.series import level_series
from finance.augur.sim.testing.situs import START_YEAR, flat_parcel
from finance.augur.sim.world import World

IRS = AgentId("irs")

ALICE = AgentId("alice")
CHECKING = AccountId("checking")
FEDERAL = JurisdictionId("federal_us")
SP500 = SecurityKey(symbol=SP500_SYMBOL)
# The home's parcel, taxed a flat 1.2% of its price.
HOME_PARCEL = flat_parcel(Decimal("0.012"))


def ref(agent_id: AgentId, account_id: AccountId = CHECKING) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=account_id)


def account(agent_id: AgentId, balance: Decimal | int = 0) -> tuple[AccountRef, int]:
    """An account and its opening balance."""
    return ref(agent_id), USD.quanta(balance)


def world_for(*accounts: tuple[AccountRef, int], horizon_months: int, series: tuple[Series, ...] = ()) -> World:
    """An empty world holding the declared cash accounts."""
    world = World(
        MarketPath(series, 0, rollout_count=1), horizon_months=horizon_months, income_sources=(ORDINARY_INCOME,)
    )
    for opened, balance in accounts:
        world.declare_account(account=opened, opening_balance=balance)
    return world


def taxed_by(world: World, *jurisdiction_ids: JurisdictionId) -> None:
    world.track(
        TaxAuthority(
            compile_profile(
                TaxProfile(
                    agent_id=ALICE,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=list(jurisdiction_ids),
                    tax_authority_agent_id=IRS,
                ),
                {id_: load_jurisdiction(id_) for id_ in jurisdiction_ids},
                currency=USD,
            ),
            indexation=FixedNominalLaw(),
        )
    )


def run(world: World, *, tracked: EconomicAgent | None = None) -> list[Book]:
    """Every month to the horizon or the stop; the books the caller keeps for itself between steps."""
    world.track(ClaimPayer(AgentId(ALICE)) if tracked is None else tracked)
    books = [world.book()]
    world.start()
    while not world.finished:
        world.step()
        books.append(world.book())
    return books


def cash(books: list[Book], agent_id: AgentId, month: int) -> int:
    [balance] = [
        row.balance
        for row in books[month].balances
        if (row.account.agent_id, row.account.account_id) == (agent_id, CHECKING)
    ]
    return balance


def gain(books: list[Book], agent_id: AgentId, month: int, *, long_term: bool) -> int:
    return sum(
        row.long_term_gain if long_term else row.short_term_gain
        for row in books[month].capital_gains
        if row.agent_id == agent_id
    )


def paycheck(world: World, amount: Decimal | int, *, end_month: int) -> None:
    world.declare_flow(
        schedule=Recurring(start_month=0, end_month=end_month),
        cause_id="paycheck",
        from_account=ref(AgentId("payroll")),
        to_account=ref(ALICE),
        amount=USD.quanta(amount),
        income_category=None,
        deduction_category=None,
    )


def rent(amount: Decimal | int, *, end_month: int) -> Biller:
    return Biller(
        schedule=Recurring(start_month=0, end_month=end_month),
        obligation_id="rent",
        obligation_type="rent",
        from_account=ref(ALICE),
        to_account=ref(AgentId("landlord")),
        amount_due=USD.quanta(amount),
        property_id=None,
        deduction_category=None,
        deductible_fraction_ppb=1_000_000_000,
    )


def test_transfers_only_month_loop() -> None:
    # Recurring paycheck for a year + a one-off gift: transfers and nothing else.
    world = world_for(account(AgentId("payroll")), account(ALICE, 100), account(AgentId("bob"), 500), horizon_months=12)
    paycheck(world, 1000, end_month=11)
    world.declare_flow(
        schedule=Once(month=6),
        cause_id="bob_gifts_alice",
        from_account=ref(AgentId("bob")),
        to_account=ref(ALICE),
        amount=USD.quanta(250),
        income_category=None,
        deduction_category=None,
    )
    books = run(world)

    # alice: 100 opening + 12 paychecks of 1000 + a 250 gift = 12350.
    assert cash(books, ALICE, 12) == 1_235_000
    assert cash(books, AgentId("bob"), 12) == 25_000
    assert cash(books, AgentId("payroll"), 12) == -1_200_000
    # Mid-horizon snapshot: 6 paychecks landed by month 6 (months 0..5), gift not yet (fires at 6).
    assert cash(books, ALICE, 6) == 610_000


def test_declared_bill_settles_beside_the_paycheck() -> None:
    # Paycheck (transfer) + monthly rent (a tracked biller's claim). Always funded, so nothing stops.
    world = world_for(
        account(AgentId("payroll")), account(ALICE, 1000), account(AgentId("landlord")), horizon_months=12
    )
    paycheck(world, 5000, end_month=11)
    world.track(rent(2000, end_month=11))
    books = run(world)

    # alice: 1000 opening + 12 paychecks of 5000 - 12 rents of 2000 = 37000.
    assert cash(books, ALICE, 12) == 3_700_000
    assert cash(books, AgentId("landlord"), 12) == 2_400_000
    assert cash(books, AgentId("payroll"), 12) == -6_000_000


def test_unfundable_bill_stops_the_path_keeping_both_parties_balances() -> None:
    # No income: alice can pay rent in month 0 (1000 -> 400) but not month 1 (needs 600), so the
    # rollout stops at month 1, preserving both parties' actual balances.
    world = world_for(account(ALICE, 1000), account(AgentId("landlord")), horizon_months=12)
    world.track(rent(600, end_month=11))
    books = run(world)

    assert cash(books, ALICE, 1) == 40_000  # after month 0: rent paid (1000 -> 400)
    assert cash(books, AgentId("landlord"), 1) == 60_000  # month 0's rent landed pre-failure
    assert cash(books, ALICE, 2) == 40_000
    assert cash(books, AgentId("landlord"), 2) == 60_000
    assert len(books) == 3  # the stopped month is the last one booked


def test_security_sale_books_proceeds_and_a_long_term_gain() -> None:
    # 100 SP500 units bought 24 months pre-horizon at $80, sold at month 3 for $120 — the lot
    # disposal, the proceeds credit and the holding-period classification, in one run. A flat price
    # series keeps the assertion exact. The horizon ends before any December, so the profile is what
    # makes the gain reportable rather than what assesses it.
    horizon = 6
    scale = quantity_scale_for_asset(SP500)
    units = quantity_to_quanta(100, scale=scale)
    series = level_series({SP500: np.full((1, horizon + 1), 120.0)}, rollout_count=1, horizon_months=horizon)
    world = world_for(account(ALICE), account(IRS), horizon_months=horizon, series=series)
    taxed_by(world, FEDERAL)
    world.declare_pool(
        agent_id=ALICE, account_id=AccountId("brokerage"), asset_id=AssetId(SP500.symbol), quantity_scale=scale
    )
    world.hold_lot(
        lot_id=LotId("alice_sp500"),
        agent_id=ALICE,
        account_id=AccountId("brokerage"),
        asset_id=AssetId(SP500.symbol),
        purchase_month=-24,  # long-term when sold at month 3
        quantity_scale=scale,
        units=units,
        basis=USD.quanta(8000),
    )
    books = run(
        world,
        tracked=Scripted(
            ClaimPayer(AgentId(ALICE)),
            {
                3: (
                    Sell(
                        cause_id="alice_sells_sp500",
                        agent_id=ALICE,
                        proceeds_account_id=CHECKING,
                        asset_id=AssetId(SP500.symbol),
                        lots=(LotSale(account_id=AccountId("brokerage"), lot_id=LotId("alice_sp500"), units=units),),
                    ),
                )
            },
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
    mortgage: MortgageFinancing | None = None,
) -> ScheduledPurchase:
    """Alice buys a $500k SF home, none of it let, so nothing depreciates."""
    return ScheduledPurchase(
        month=month,
        cause_id="alice_buys_home",
        property_id=PropertyId("home"),
        parcel=HOME_PARCEL,
        market=LocationId("sf"),
        buyer_agent_id=ALICE,
        buyer_account_id=CHECKING,
        seller_agent_id=AgentId("seller"),
        seller_account_id=CHECKING,
        purchase_price=USD.quanta(500_000),
        down_payment=USD.quanta(down_payment),
        buyer_closing_cost=USD.quanta(buyer_closing_cost),
        rented_fraction_ppb=0,
        land_value_fraction_ppb=rate_to_ppb(Decimal("0.2")),
        mortgage=mortgage,
    )


def test_cash_property_purchase_moves_the_whole_stake() -> None:
    # All-cash home purchase at month 2: the buyer's down payment + closing cost moves to the seller
    # and the property goes active.
    world = world_for(account(ALICE, 600_000), account(AgentId("seller")), horizon_months=6)
    world.declare_housing(Housing(purchases=(purchase(month=2, down_payment=500_000, buyer_closing_cost=10_000),)), ())
    books = run(world)

    # stake = down payment + closing = 510k, moved buyer -> seller during month 2 (snapshot index 3).
    assert cash(books, ALICE, 2) == 60_000_000  # before purchase
    assert cash(books, ALICE, 3) == 9_000_000
    assert cash(books, AgentId("seller"), 3) == 51_000_000


def test_property_tax_accrues_only_once_the_property_is_held() -> None:
    # Cash home purchase at month 0 + a property-tax policy: the monthly ad-valorem tax
    # (assessed 500k × 1.2% / 12 = $500) is the county's claim, starting the month after purchase.
    world = world_for(account(ALICE, 600_000), account(AgentId("seller")), account(AgentId("county")), horizon_months=4)
    world.declare_housing(
        Housing(purchases=(purchase(month=0, down_payment=500_000),)),
        (
            PropertyTaxPolicy(
                property_id=PropertyId("home"),
                owner_agent_id=ALICE,
                from_account_id=CHECKING,
                tax_authority_agent_id=AgentId("county"),
                tax_authority_account_id=CHECKING,
                start_year=START_YEAR,
                start_month=0,
                end_month=None,
            ),
        ),
    )
    books = run(world)

    # After month 0: 500k purchase, no tax yet (accrues only once owned). Then $500/mo for months 1-3.
    assert cash(books, ALICE, 1) == 10_000_000
    assert cash(books, ALICE, 4) == 9_850_000
    assert cash(books, AgentId("county"), 4) == 150_000


def test_financed_purchase_originates_then_services_the_loan() -> None:
    # Month 0 originates the loan (down payment moves buyer -> seller, liability principal set),
    # then monthly mortgage-payment claims (interest/principal split) settle buyer -> lender from
    # month 1.
    world = world_for(account(ALICE, 300_000), account(AgentId("seller")), account(AgentId("lender")), horizon_months=3)
    world.declare_housing(
        Housing(
            purchases=(
                purchase(
                    month=0,
                    down_payment=100_000,
                    mortgage=MortgageFinancing(
                        liability_id=LiabilityId("alice_mortgage"),
                        lender_agent_id=AgentId("lender"),
                        lender_account_id=CHECKING,
                        principal=USD.quanta(400_000),
                        annual_interest_rate_ppb=rate_to_ppb(Decimal("0.06")),
                        term_months=360,
                    ),
                ),
            )
        ),
        (),
    )
    books = run(world)

    # After month 0: down payment only (mortgage payments start the month after origination).
    assert cash(books, ALICE, 1) == 20_000_000
    # Months 1 & 2 each pay one mortgage bill to the lender; alice's cash nets both off.
    assert cash(books, AgentId("lender"), 3) == 479_640
    assert cash(books, ALICE, 3) == 19_520_360


if __name__ == "__main__":
    pytest_bazel.main()
