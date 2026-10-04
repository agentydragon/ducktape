"""A world refuses a fact it cannot execute where that fact is declared, before it holds anything.

Every rejection here is the declaration's own: the path admits only dense series it can read, a
pool admits only a quote that is a price and is declared once, a lot needs the pool it sits in, a
bond is bought at par, a distribution pays whoever holds the security once, a purchase names a
location and a funded price and its lifecycle follows it, a cashflow or bill falls inside the
horizon on an index the path carries, and an issuer's protocol path stays in range.
"""

from collections.abc import Callable, Mapping
from dataclasses import replace
from fractions import Fraction
from functools import partial

import pytest
import pytest_bazel

from finance.augur.model.series import LocationId
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef
from finance.augur.sim.ids import (
    AccountId,
    AgentId,
    AssetId,
    BondId,
    JurisdictionId,
    LiabilityId,
    LotId,
    PortfolioId,
    PropertyId,
)
from finance.augur.sim.income import (
    ORDINARY_INCOME,
    InterestCharacter,
    InterestIncome,
    Municipal,
    Taxable,
    TransferIncomeCategory,
    Treasury,
)
from finance.augur.sim.market_path import Amount, IndexedAmount, MarketPath, Series
from finance.augur.sim.observations import FixedCoupon, IndexedCoupon
from finance.augur.sim.property import (
    Housing,
    MortgageFinancing,
    PrimaryResidence,
    PrimaryResidenceEvent,
    RentedFraction,
    ScheduledPurchase,
    ScheduledSale,
)
from finance.augur.sim.property_tax import PropertyTaxPolicy
from finance.augur.sim.schedule import Once, Recurring, Schedule
from finance.augur.sim.tax_authority import MortgageInterestDeduction
from finance.augur.sim.testing.situs import START_YEAR, UNTAXED
from finance.augur.sim.tlh import TlhAssumptions, TlhOpeningCohort
from finance.augur.sim.world import World

HOLDER = AgentId("test-holder")
COUNTERPARTY = AgentId("test-counterparty")
CHECKING = AccountId("checking")
BROKERAGE = AccountId("brokerage")
STOCK = AssetId("test-stock")
LOT = LotId("test-lot")
SCALE = 1000
HORIZON = 2
TAXABLE = Taxable()
# A payout that is all taxable interest.
WHOLLY_INTEREST: Mapping[TransferIncomeCategory, int] = {InterestIncome(character=TAXABLE): 1_000_000_000}
FLAT = TlhAssumptions(
    peak_annual_yield=0, floor_annual_yield=0, maturity_decay_exponent=1, drawdown_sensitivity=0, short_term_fraction=1
)


def ref(agent_id: AgentId, account_id: AccountId = CHECKING) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=account_id)


def prices(*values: int) -> Series:
    return Series(series_id=f"security:{STOCK}", snapshots=len(values), values=values)


def payouts(*values: int) -> Series:
    return Series(series_id=f"security_distribution:{STOCK}", snapshots=len(values), values=values)


def composed(*series: Series, horizon_months: int = HORIZON) -> World:
    """The holder's cash and brokerage accounts and one counterparty, on a path carrying `series`."""
    world = World(
        MarketPath(series, 0, rollout_count=1),
        horizon_months=horizon_months,
        income_sources=(ORDINARY_INCOME, InterestIncome(character=Taxable())),
    )
    for account in (ref(HOLDER), ref(HOLDER, BROKERAGE), ref(COUNTERPARTY)):
        world.declare_account(account=account, opening_balance=0)
    return world


def pool(
    world: World, *, account_id: AccountId = BROKERAGE, asset_id: AssetId = STOCK, quantity_scale: int = SCALE
) -> None:
    world.declare_pool(agent_id=HOLDER, account_id=account_id, asset_id=asset_id, quantity_scale=quantity_scale)


def lot(
    world: World,
    lot_id: LotId = LOT,
    *,
    account_id: AccountId = BROKERAGE,
    asset_id: AssetId = STOCK,
    purchase_month: int = -2,
    quantity_scale: int = SCALE,
) -> None:
    world.hold_lot(
        lot_id=lot_id,
        agent_id=HOLDER,
        account_id=account_id,
        asset_id=asset_id,
        purchase_month=purchase_month,
        quantity_scale=quantity_scale,
        units=quantity_scale,
        basis=100,
    )


@pytest.mark.parametrize("bad", [0, -1], ids=["zero", "negative"])
def test_a_pool_admits_no_quote_that_is_not_a_price_and_holds_nothing_when_it_refuses(bad: int) -> None:
    pool(composed(prices(100, 100, 100)))
    world = composed(prices(100, 100, bad))  # the unusable mark is in the terminal snapshot
    with pytest.raises(ValueError, match=f"non-positive value {bad}"):
        pool(world)
    # The refusal precedes every holding built on that quote: nothing was admitted.
    assert not world.holdings.pools
    assert world.managed is None
    assert not world.holdings.lots


def test_a_public_pool_needs_its_price_series_on_the_path() -> None:
    with pytest.raises(ValueError, match="missing public security series"):
        pool(composed())


def test_a_pool_is_declared_once() -> None:
    world = composed(prices(100, 100, 100))
    pool(world)
    with pytest.raises(ValueError, match="duplicate holding pool declaration"):
        pool(world)


def test_a_path_admits_only_dense_series_named_once() -> None:
    with pytest.raises(ValueError, match="duplicate series"):
        MarketPath((prices(100, 100, 100), prices(1, 1, 1)), 0, rollout_count=1)
    short = Series(series_id=f"security:{STOCK}", snapshots=3, values=(100, 100, 100, 100, 100))
    with pytest.raises(ValueError, match="invalid shape"):
        MarketPath((short,), 0, rollout_count=2)
    with pytest.raises(ValueError, match="invalid rollout selection"):
        MarketPath((), 0, rollout_count=0)


def test_a_world_needs_a_positive_horizon_every_series_covers() -> None:
    with pytest.raises(ValueError, match="horizon must be positive"):
        World(MarketPath((), 0, rollout_count=1), horizon_months=0)
    with pytest.raises(ValueError, match="snapshots"):
        World(MarketPath((prices(100, 100),), 0, rollout_count=1), horizon_months=HORIZON)


def test_a_lot_needs_a_declared_pool_on_its_own_quantity_scale() -> None:
    world = composed(prices(100, 100, 100))
    with pytest.raises(ValueError, match="references no declared holding pool"):
        lot(world)
    with pytest.raises(ValueError, match="invalid holding pool quantity scale"):
        pool(world, quantity_scale=7)  # units are counted on a power-of-ten grid
    pool(world)
    with pytest.raises(ValueError, match="mixed quantity scale"):
        lot(world, quantity_scale=10)
    lot(world)
    assert [held.lot_id for held in world.holdings.lots] == ["test-lot"]


def test_a_pool_holds_one_opening_lot_per_purchase_month() -> None:
    world = composed(prices(100, 100, 100))
    pool(world)
    pool(world, account_id=CHECKING)
    lot(world, LotId("test-first"))
    # Another month in the pool, or the same month in another pool, leaves FIFO ordered by month.
    lot(world, LotId("test-older"), purchase_month=-3)
    lot(world, LotId("test-elsewhere"), account_id=CHECKING)
    with pytest.raises(ValueError, match="'test-first' and 'test-twin' share purchase_month=-2"):
        lot(world, LotId("test-twin"))


# A par bond paying a fixed semiannual coupon over two whole periods.
COUPON = FixedCoupon(amount=3)


def bond(
    world: World,
    *,
    account_id: AccountId = CHECKING,
    character: InterestCharacter = TAXABLE,
    purchase_price: int = 100,
    coupon: FixedCoupon | IndexedCoupon = COUPON,
    coupon_period_months: int = 6,
    maturity_month_index: int = 6,
) -> None:
    world.hold_bond(
        bond_id=BondId("test-bond"),
        agent_id=HOLDER,
        account_id=account_id,
        character=character,
        face_value=100,
        purchase_price=purchase_price,
        coupon=coupon,
        coupon_period_months=coupon_period_months,
        purchase_month_index=-6,
        maturity_month_index=maturity_month_index,
    )


@pytest.mark.parametrize(
    ("invalid", "match"),
    [
        (partial(bond, purchase_price=99), "invalid bond terms"),
        (partial(bond, coupon=FixedCoupon(amount=-1)), "invalid bond terms"),
        (partial(bond, coupon_period_months=5), "invalid bond terms"),
        (partial(bond, maturity_month_index=-6), "invalid bond terms"),
        (partial(bond, coupon=IndexedCoupon(annual_rate_ppb=50_000_000)), "inflation"),
        (partial(bond, character=Municipal(state=JurisdictionId("test-state"))), "undeclared income source"),
        (partial(bond, account_id=AccountId("test-undeclared")), "unknown account"),
    ],
    ids=[
        "non-par",
        "negative-coupon",
        "part-period",
        "matures-at-purchase",
        "missing-index",
        "undeclared-income",
        "unknown-account",
    ],
)
def test_a_dated_bond_is_bought_at_par_over_whole_coupon_periods(invalid: Callable[[World], None], match: str) -> None:
    bond(composed())
    with pytest.raises(ValueError, match=match):
        invalid(composed())


PURCHASE = ScheduledPurchase(
    month=0,
    cause_id="test-purchase",
    property_id=PropertyId("test-home"),
    parcel=UNTAXED,
    market=LocationId("test-market"),
    buyer_agent_id=HOLDER,
    buyer_account_id=CHECKING,
    seller_agent_id=COUNTERPARTY,
    seller_account_id=CHECKING,
    purchase_price=10,
    down_payment=10,
    buyer_closing_cost=0,
    rented_fraction_ppb=0,
    land_value_fraction_ppb=200_000_000,
    mortgage=None,
)
LOAN = MortgageFinancing(
    liability_id=LiabilityId("test-loan"),
    lender_agent_id=COUNTERPARTY,
    lender_account_id=CHECKING,
    principal=6,
    annual_interest_rate_ppb=0,
    term_months=60,
)


def housed(purchase: ScheduledPurchase) -> None:
    composed().declare_housing(Housing(purchases=(purchase,)), ())


def test_a_property_purchase_names_declared_parties() -> None:
    housed(PURCHASE)
    with pytest.raises(ValueError, match="unknown account"):
        housed(replace(PURCHASE, seller_agent_id=AgentId("test-stranger")))


def test_a_purchase_price_is_covered_by_the_down_payment_and_the_loan() -> None:
    housed(replace(PURCHASE, down_payment=4, mortgage=LOAN))
    for gap in (replace(PURCHASE, down_payment=9), replace(PURCHASE, down_payment=3, mortgage=LOAN)):
        with pytest.raises(ValueError, match="invalid property terms"):
            housed(gap)


def distribution(
    world: World,
    *,
    holding_account_id: AccountId = BROKERAGE,
    tax_character: Mapping[TransferIncomeCategory, int] = WHOLLY_INTEREST,
) -> None:
    world.declare_distribution(
        agent_id=HOLDER,
        holding_account_id=holding_account_id,
        asset_id=STOCK,
        to_account_id=CHECKING,
        tax_character=tax_character,
    )


def holding_stock(*, payout: Series) -> World:
    world = composed(prices(100, 100, 100), payout)
    pool(world)
    lot(world)
    return world


def test_a_distribution_pays_whoever_holds_the_security_not_a_cash_account() -> None:
    distribution(holding_stock(payout=payouts(0, 0, 0)))
    with pytest.raises(ValueError, match=f"references no lots for {HOLDER}:{CHECKING}:{STOCK}"):
        distribution(holding_stock(payout=payouts(0, 0, 0)), holding_account_id=CHECKING)
    with pytest.raises(ValueError, match="missing distribution series"):
        distribution(composed(prices(100, 100, 100)))


@pytest.mark.parametrize(
    ("tax_character", "match"),
    [
        ({InterestIncome(character=Taxable()): 400_000_000}, "tax character"),
        ({InterestIncome(character=Treasury()): 1_000_000_000}, "undeclared income"),
    ],
    ids=["incomplete", "undeclared-source"],
)
def test_a_distribution_splits_its_whole_payout_across_declared_income_sources(
    tax_character: Mapping[TransferIncomeCategory, int], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        distribution(holding_stock(payout=payouts(0, 0, 0)), tax_character=tax_character)


def test_a_zero_payout_is_valid_but_a_negative_one_is_not() -> None:
    distribution(holding_stock(payout=payouts(0, 0, 0)))
    with pytest.raises(ValueError, match="negative security distribution"):
        distribution(holding_stock(payout=payouts(0, 0, -1)))


def test_a_zero_mark_is_valid_only_where_the_asset_is_held_exclusively_through_a_manager() -> None:
    written_off = TlhOpeningCohort(value=0, cost_basis=100, purchase_month_index=-2)
    managed = composed(prices(0, 0, 0))
    portfolio(managed, cohort=written_off)
    observed = managed.portfolios[MANAGED].observe()
    assert (observed.value, observed.reported_tax_basis) == (0, written_off.cost_basis)
    # An ordinary purchase pool sharing the quote restores the positive-price requirement:
    # managed ownership is pool-scoped.
    with pytest.raises(ValueError, match="non-positive value"):
        pool(composed(prices(0, 0, 0)))


def cpi(*levels: int) -> Series:
    return Series(series_id="inflation", snapshots=len(levels), values=levels)


def indexed(*, series_id: str = "inflation", base_month_index: int = 0) -> IndexedAmount:
    return IndexedAmount(
        base_amount=1, series_id=series_id, base_month_index=base_month_index, adjustment_period_months=1
    )


PAYER = ref(COUNTERPARTY)
MONTH_ZERO = Once(month=0)


def flow(
    world: World,
    *,
    from_account: AccountRef = PAYER,
    amount: Amount = 1,
    income_category: TransferIncomeCategory | None = None,
    schedule: Schedule = MONTH_ZERO,
    property_id: PropertyId | None = None,
) -> None:
    world.declare_flow(
        cause_id="test-transfer",
        from_account=from_account,
        to_account=ref(HOLDER),
        amount=amount,
        income_category=income_category,
        deduction_category=None,
        schedule=schedule,
        property_id=property_id,
    )


@pytest.mark.parametrize(
    ("invalid", "match"),
    [
        (partial(flow, from_account=ref(AgentId("test-stranger"))), "unknown declared account"),
        (partial(flow, income_category=InterestIncome(character=Treasury())), "undeclared income source"),
        (partial(flow, schedule=Once(month=HORIZON)), "outside the horizon"),
        (partial(flow, schedule=Recurring(start_month=1, end_month=0)), "before start month"),
        (partial(flow, property_id=PropertyId("test-unbought")), "undeclared property"),
        (partial(flow, amount=indexed(series_id="rent:test-nowhere")), "missing series"),
        (partial(flow, amount=indexed(base_month_index=1)), "before base month"),
    ],
    ids=[
        "unknown-account",
        "undeclared-income",
        "outside-horizon",
        "ends-before-it-starts",
        "unbought-property",
        "unsampled-index",
        "before-base-month",
    ],
)
def test_a_standing_flow_moves_declared_cash_from_a_declared_income_source_inside_the_horizon(
    invalid: Callable[[World], None], match: str
) -> None:
    world = composed(cpi(1, 1, 1))
    flow(world)
    world.start()
    assert world.account_balance(HOLDER, CHECKING) == 1
    with pytest.raises(ValueError, match=match):
        invalid(composed(cpi(1, 1, 1)))


def test_an_indexed_amount_needs_a_nonzero_base_level() -> None:
    flow(composed(cpi(1, 2, 2)), amount=indexed())
    with pytest.raises(ValueError, match="zero base level"):
        flow(composed(cpi(0, 1, 1)), amount=indexed())


def rented(month: int, property_id: PropertyId = PURCHASE.property_id) -> RentedFraction:
    return RentedFraction(month=month, property_id=property_id, rented_fraction_ppb=500_000_000)


@pytest.mark.parametrize(
    ("housing", "match"),
    [
        (Housing(purchases=(PURCHASE, PURCHASE)), "duplicate property purchase"),
        (Housing(purchases=(replace(PURCHASE, month=HORIZON),)), "outside the horizon"),
        (
            Housing(purchases=(PURCHASE,), rented_fraction_events=(rented(1, PropertyId("test-unbought")),)),
            "unknown property",
        ),
        (Housing(purchases=(PURCHASE,), rented_fraction_events=(rented(0),)), "strictly after its purchase"),
        (
            Housing(
                purchases=(PURCHASE,),
                sales=(ScheduledSale(month=1, property_id=PURCHASE.property_id, commission_ppb=0, escrow_title_ppb=0),)
                * 2,
            ),
            "multiple sales",
        ),
        (
            Housing(
                purchases=(PURCHASE,),
                sales=(ScheduledSale(month=1, property_id=PURCHASE.property_id, commission_ppb=0, escrow_title_ppb=0),),
                rented_fraction_events=(rented(1),),
            ),
            "frozen after sale",
        ),
        (
            Housing(
                purchases=(PURCHASE,),
                initial_residences=(PrimaryResidence(agent_id=COUNTERPARTY, property_id=PURCHASE.property_id),),
            ),
            "did not buy it",
        ),
        (
            Housing(
                purchases=(PURCHASE,),
                initial_residences=(PrimaryResidence(agent_id=HOLDER, property_id=PURCHASE.property_id),) * 2,
            ),
            "multiple initial primary residences",
        ),
        (
            Housing(
                purchases=(PURCHASE,),
                residence_events=(PrimaryResidenceEvent(month=HORIZON, agent_id=HOLDER, property_id=None),),
            ),
            "outside the horizon",
        ),
        (
            Housing(
                purchases=(PURCHASE,),
                residence_events=(PrimaryResidenceEvent(month=1, agent_id=HOLDER, property_id=None),) * 2,
            ),
            "multiple primary residence events",
        ),
        (
            Housing(
                purchases=(PURCHASE,),
                residence_events=(PrimaryResidenceEvent(month=1, agent_id=AgentId("test-stranger"), property_id=None),),
            ),
            "unknown agent",
        ),
    ],
    ids=[
        "duplicate-purchase",
        "purchase-outside-horizon",
        "event-on-unbought-property",
        "event-at-purchase",
        "two-sales",
        "event-at-sale",
        "residence-not-bought",
        "two-initial-residences",
        "residence-outside-horizon",
        "two-residence-changes",
        "unknown-resident",
    ],
)
def test_housing_is_bought_inside_the_horizon_and_its_lifecycle_follows_the_purchase(
    housing: Housing, match: str
) -> None:
    composed().declare_housing(
        Housing(
            purchases=(PURCHASE,),
            rented_fraction_events=(rented(1),),
            initial_residences=(PrimaryResidence(agent_id=HOLDER, property_id=PURCHASE.property_id),),
        ),
        (),
    )
    with pytest.raises(ValueError, match=match):
        composed().declare_housing(housing, ())


PROPERTY_TAX = PropertyTaxPolicy(
    property_id=PURCHASE.property_id,
    owner_agent_id=HOLDER,
    from_account_id=CHECKING,
    tax_authority_agent_id=COUNTERPARTY,
    tax_authority_account_id=CHECKING,
    start_year=START_YEAR,
    start_month=0,
    end_month=None,
)


@pytest.mark.parametrize(
    ("policies", "match"),
    [
        ((replace(PROPERTY_TAX, property_id=PropertyId("test-unbought")),), "unknown property"),
        ((replace(PROPERTY_TAX, owner_agent_id=COUNTERPARTY),), "not owed by the property's buyer"),
        ((replace(PROPERTY_TAX, start_month=1, end_month=0),), "ends before it starts"),
        ((PROPERTY_TAX, replace(PROPERTY_TAX, start_month=1)), "overlapping property tax policies"),
    ],
    ids=["unbought", "not-the-buyer", "ends-before-it-starts", "overlapping"],
)
def test_one_property_tax_policy_at_a_time_is_owed_by_the_buyer(
    policies: tuple[PropertyTaxPolicy, ...], match: str
) -> None:
    composed().declare_housing(Housing(purchases=(PURCHASE,)), (PROPERTY_TAX,))
    with pytest.raises(ValueError, match=match):
        composed().declare_housing(Housing(purchases=(PURCHASE,)), policies)


def test_a_taxed_parcel_s_rate_area_publishes_its_first_billed_fiscal_year() -> None:
    """Month 1 is in fiscal year START_YEAR - 1, which a rate area first publishing START_YEAR cannot bill."""
    later = replace(UNTAXED.situs, debt_rates={START_YEAR: Fraction(0)})
    with pytest.raises(ValueError, match="publishes no debt rate as early as fiscal year"):
        composed().declare_housing(
            Housing(purchases=(replace(PURCHASE, parcel=replace(PURCHASE.parcel, situs=later)),)), (PROPERTY_TAX,)
        )


def test_a_deduction_is_claimed_by_an_enrolled_taxpayer() -> None:
    with pytest.raises(ValueError, match="names no taxpayer"):
        composed().declare_deduction(
            MortgageInterestDeduction(
                liability_id=LOAN.liability_id,
                owner_agent_id=HOLDER,
                debt_class="acquisition",
                per_jurisdiction_principal_cap={},
            )
        )


def bill(*, amount_due: Amount = 1, schedule: Schedule = MONTH_ZERO) -> Biller:
    return Biller(
        obligation_id="test-bill",
        obligation_type="cash_spend",
        from_account=ref(HOLDER),
        to_account=ref(COUNTERPARTY),
        amount_due=amount_due,
        property_id=None,
        deduction_category=None,
        deductible_fraction_ppb=0,
        schedule=schedule,
    )


def test_a_bill_is_due_inside_the_horizon_on_an_index_the_path_carries() -> None:
    composed().track(bill())
    with pytest.raises(ValueError, match="outside the horizon"):
        composed().track(bill(schedule=Once(month=HORIZON)))
    with pytest.raises(ValueError, match="missing series"):
        composed().track(bill(amount_due=indexed()))


def test_a_holding_pays_out_through_one_distribution() -> None:
    world = holding_stock(payout=payouts(0, 0, 0))
    distribution(world)
    with pytest.raises(ValueError, match="duplicate distribution"):
        distribution(world)


MANAGED = PortfolioId("test-managed")
OPENING_COHORT = TlhOpeningCohort(value=100, cost_basis=100, purchase_month_index=-2)


def portfolio(
    world: World,
    *,
    portfolio_id: PortfolioId = MANAGED,
    owner_agent_id: AgentId = HOLDER,
    account_id: AccountId = BROKERAGE,
    cohort: TlhOpeningCohort = OPENING_COHORT,
) -> None:
    world.declare_portfolio(
        portfolio_id=portfolio_id,
        owner_agent_id=owner_agent_id,
        account_id=account_id,
        asset_id=STOCK,
        initial_cohorts=(cohort,),
        assumptions=FLAT,
    )


SOLE_OWNER = "must have exactly one component owner and no ordinary holdings"


def test_a_managed_portfolio_has_a_declared_owner_a_price_path_and_one_manager() -> None:
    world = composed(prices(100, 100, 100))
    portfolio(world)
    with pytest.raises(ValueError, match="duplicate TLH portfolio"):
        portfolio(world, account_id=CHECKING)
    with pytest.raises(ValueError, match=SOLE_OWNER):
        portfolio(world, portfolio_id=PortfolioId("test-second"))
    with pytest.raises(ValueError, match="unknown owner"):
        portfolio(composed(prices(100, 100, 100)), owner_agent_id=AgentId("test-stranger"))
    with pytest.raises(ValueError, match="missing security series"):
        portfolio(composed())
    # Zero is a mark a manager may carry; below zero is not a price at any snapshot, the terminal one included.
    portfolio(composed(prices(100, 0, 0)))
    with pytest.raises(ValueError, match="index price must be nonnegative, got -1 at month 2"):
        portfolio(composed(prices(100, 100, -1)))


def test_a_managed_portfolio_holds_its_pool_alone_whichever_is_declared_first() -> None:
    managed_first = composed(prices(100, 100, 100))
    portfolio(managed_first)
    with pytest.raises(ValueError, match=SOLE_OWNER):
        pool(managed_first)
    with pytest.raises(ValueError, match="references no declared holding pool"):
        lot(managed_first)
    # Ownership is pool-scoped: the same security in another account is an ordinary holding.
    pool(managed_first, account_id=CHECKING)
    lot(managed_first, account_id=CHECKING)

    lot_first = holding_stock(payout=payouts(0, 0, 0))
    with pytest.raises(ValueError, match=SOLE_OWNER):
        portfolio(lot_first)
    assert lot_first.managed is None
    pool_first = composed(prices(100, 100, 100))
    pool(pool_first)
    with pytest.raises(ValueError, match=SOLE_OWNER):
        portfolio(pool_first)


ISSUER = "test-issuer"
# A channel's level when nothing about the issuer is meant to happen.
QUIET = {
    "mark": 1,
    "regime": 1,
    "event_kind": 0,
    "sale_opportunity": 0,
    "sale_capacity": 0,
    "eligible": 0,
    "forced_sale": 0,
    "liquidity_blocked": 0,
    "forced_recovery": 0,
    "company_valuation": 1,
}


def issuer_paths(**overrides: tuple[int, ...]) -> tuple[Series, ...]:
    return tuple(
        Series(
            series_id=f"private_equity_{channel}:{ISSUER}",
            snapshots=HORIZON + 1,
            values=overrides.get(channel, (level,) * (HORIZON + 1)),
        )
        for channel, level in QUIET.items()
    )


def holding_private(*series: Series) -> None:
    world = composed(*series)
    asset_id = AssetId(f"private_equity:{ISSUER}")
    pool(world, asset_id=asset_id, quantity_scale=1)
    lot(world, asset_id=asset_id, quantity_scale=1)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"regime": (1, 5, 1)}, "invalid regime value 5 at month 1"),
        ({"sale_capacity": (0, 0, 1_000_000_001)}, "invalid sale_capacity value"),
        ({"event_kind": (0, 1, 0)}, "tender event and a sale opportunity in different months"),
    ],
    ids=["unknown-regime", "capacity-above-one", "tender-without-opportunity"],
)
def test_an_issuer_protocol_path_stays_in_range_and_tenders_only_with_an_opportunity(
    overrides: dict[str, tuple[int, ...]], match: str
) -> None:
    holding_private(*issuer_paths())
    holding_private(*issuer_paths(event_kind=(0, 1, 0), sale_opportunity=(0, 1, 0)))
    with pytest.raises(ValueError, match=match):
        holding_private(*issuer_paths(**overrides))


if __name__ == "__main__":
    pytest_bazel.main()
