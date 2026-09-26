"""A world refuses a fact it cannot execute where that fact is declared, before it holds anything.

Every rejection here is the declaration's own: the path admits only dense series it can read, a
pool admits only a quote that is a price, a lot needs the pool it sits in, a bond is bought at
par, a distribution pays whoever holds the security once, a purchase names a location and a
funded price and its lifecycle follows it, a cashflow or bill falls inside the horizon on an index
the path carries, and an issuer's protocol path stays in range.
"""

from dataclasses import replace

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
from finance.augur.sim.jurisdictions import JurisdictionLevel
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedBond,
    PreparedDistribution,
    PreparedDistributionSlice,
    PreparedFixedAmount,
    PreparedHoldingPool,
    PreparedIndexedAmount,
    PreparedIndexedCoupon,
    PreparedJurisdiction,
    PreparedLocation,
    PreparedLot,
    PreparedObligation,
    PreparedPropertyCashflow,
    PreparedRecurringTransfer,
    PreparedSeries,
    PreparedTlhPortfolio,
    PreparedTransfer,
    _MortgageFinancing,
    _MortgageInterestDeduction,
    _PrimaryResidence,
    _PrimaryResidenceEvent,
    _PropertyPurchase,
    _PropertySale,
    _PropertyTax,
    _RentedFraction,
)
from finance.augur.sim.property import Housing
from finance.augur.sim.scenario import ORDINARY_INCOME, InterestIncome
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
TAX_HOME = PreparedJurisdiction(jurisdiction_id=JurisdictionId("test-jurisdiction"), level=JurisdictionLevel.STATE)
LOCATION = PreparedLocation(
    location_id=LocationId("test-market"),
    display_name="Test market",
    jurisdiction_ids=(),
    annual_property_tax_rate_ppb=0,
    annual_special_assessment=0,
)
FLAT = TlhAssumptions(
    peak_annual_yield=0, floor_annual_yield=0, maturity_decay_exponent=1, drawdown_sensitivity=0, short_term_fraction=1
)


def ref(agent_id: AgentId, account_id: AccountId = CHECKING) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=account_id)


def prices(*values: int) -> PreparedSeries:
    return PreparedSeries(series_id=f"security:{STOCK}", snapshots=len(values), values=values)


def payouts(*values: int) -> PreparedSeries:
    return PreparedSeries(series_id=f"security_distribution:{STOCK}", snapshots=len(values), values=values)


def composed(*series: PreparedSeries, horizon_months: int = HORIZON) -> World:
    """The holder's cash and brokerage accounts and one counterparty, on a path carrying `series`."""
    world = World(
        MarketPath(series, 0, rollout_count=1),
        horizon_months=horizon_months,
        income_sources=(ORDINARY_INCOME, InterestIncome(issuer_jurisdiction_id=None)),
        jurisdictions=(TAX_HOME,),
    )
    for account in (ref(HOLDER), ref(HOLDER, BROKERAGE), ref(COUNTERPARTY)):
        world.declare_account(PreparedAccount(account=account, opening_balance=0))
    return world


def pool(*, account_id: AccountId = BROKERAGE, quantity_scale: int = SCALE) -> PreparedHoldingPool:
    return PreparedHoldingPool(agent_id=HOLDER, account_id=account_id, asset_id=STOCK, quantity_scale=quantity_scale)


def lot(lot_id: LotId = LOT, *, account_id: AccountId = BROKERAGE, quantity_scale: int = SCALE) -> PreparedLot:
    return PreparedLot(
        lot_id=lot_id,
        agent_id=HOLDER,
        account_id=account_id,
        asset_id=STOCK,
        purchase_month=-2,
        quantity_scale=quantity_scale,
        units=quantity_scale,
        basis=100,
    )


@pytest.mark.parametrize("bad", [0, -1], ids=["zero", "negative"])
def test_a_pool_admits_no_quote_that_is_not_a_price_and_holds_nothing_when_it_refuses(bad: int) -> None:
    composed(prices(100, 100, 100)).declare_pool(pool())
    world = composed(prices(100, 100, bad))  # the unusable mark is in the terminal snapshot
    with pytest.raises(ValueError, match=f"non-positive value {bad}"):
        world.declare_pool(pool())
    # The refusal precedes every holding built on that quote: nothing was admitted.
    assert not world.holdings.pools
    assert world.managed is None
    assert not world.holdings.lots


def test_a_public_pool_needs_its_price_series_on_the_path() -> None:
    with pytest.raises(ValueError, match="missing public security series"):
        composed().declare_pool(pool())


def test_a_path_admits_only_dense_series_named_once() -> None:
    with pytest.raises(ValueError, match="duplicate series"):
        MarketPath((prices(100, 100, 100), prices(1, 1, 1)), 0, rollout_count=1)
    short = PreparedSeries(series_id=f"security:{STOCK}", snapshots=3, values=(100, 100, 100, 100, 100))
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
        world.hold(lot())
    with pytest.raises(ValueError, match="invalid holding pool quantity scale"):
        world.declare_pool(pool(quantity_scale=7))  # units are counted on a power-of-ten grid
    world.declare_pool(pool())
    with pytest.raises(ValueError, match="mixed quantity scale"):
        world.hold(lot(quantity_scale=10))
    world.hold(lot())
    assert [held.spec.lot_id for held in world.holdings.lots] == ["test-lot"]


def test_a_pool_holds_one_opening_lot_per_purchase_month() -> None:
    world = composed(prices(100, 100, 100))
    world.declare_pool(pool())
    world.declare_pool(pool(account_id=CHECKING))
    world.hold(lot(LotId("test-first")))
    # Another month in the pool, or the same month in another pool, leaves FIFO ordered by month.
    world.hold(replace(lot(LotId("test-older")), purchase_month=-3))
    world.hold(lot(LotId("test-elsewhere"), account_id=CHECKING))
    with pytest.raises(ValueError, match=r"'test-first' and 'test-twin' share holding\.purchase_month=-2"):
        world.hold(lot(LotId("test-twin")))


# A par bond paying a fixed semiannual coupon over two whole periods.
BOND = PreparedBond(
    bond_id=BondId("test-bond"),
    agent_id=HOLDER,
    account_id=CHECKING,
    issuer_jurisdiction_id=None,
    face_value=100,
    purchase_price=100,
    coupon=PreparedFixedAmount(amount=3),
    coupon_period_months=6,
    purchase_month_index=-6,
    maturity_month_index=6,
)


@pytest.mark.parametrize(
    ("invalid", "match"),
    [
        (replace(BOND, purchase_price=99), "invalid bond terms"),
        (replace(BOND, coupon=PreparedFixedAmount(amount=-1)), "invalid bond terms"),
        (replace(BOND, coupon_period_months=5), "invalid bond terms"),
        (replace(BOND, coupon=PreparedIndexedCoupon(annual_rate_ppb=50_000_000)), "inflation"),
        (replace(BOND, issuer_jurisdiction_id=JurisdictionId("test-unknown")), "unknown issuer"),
        (replace(BOND, account_id=AccountId("test-undeclared")), "unknown account"),
    ],
    ids=["non-par", "negative-coupon", "part-period", "missing-index", "unknown-issuer", "unknown-account"],
)
def test_a_dated_bond_is_bought_at_par_over_whole_coupon_periods(invalid: PreparedBond, match: str) -> None:
    composed().hold(BOND)
    with pytest.raises(ValueError, match=match):
        composed().hold(invalid)


PURCHASE = _PropertyPurchase(
    month=0,
    cause_id="test-purchase",
    property_id=PropertyId("test-home"),
    location_id=LOCATION.location_id,
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
LOAN = _MortgageFinancing(
    liability_id=LiabilityId("test-loan"),
    lender_agent_id=COUNTERPARTY,
    lender_account_id=CHECKING,
    principal=6,
    annual_interest_rate_ppb=0,
    term_months=60,
)


def housed(purchase: _PropertyPurchase, *locations: PreparedLocation) -> None:
    composed().declare_housing(Housing(purchases=(purchase,)), (), locations)


def test_a_property_purchase_names_a_known_location_and_declared_parties() -> None:
    housed(PURCHASE, LOCATION)
    with pytest.raises(ValueError, match="unknown location"):
        housed(PURCHASE)
    with pytest.raises(ValueError, match="unknown account"):
        housed(replace(PURCHASE, seller_agent_id=AgentId("test-stranger")), LOCATION)


def test_a_purchase_price_is_covered_by_the_down_payment_and_the_loan() -> None:
    housed(replace(PURCHASE, down_payment=4, mortgage=LOAN), LOCATION)
    for gap in (replace(PURCHASE, down_payment=9), replace(PURCHASE, down_payment=3, mortgage=LOAN)):
        with pytest.raises(ValueError, match="invalid property terms"):
            housed(gap, LOCATION)


DISTRIBUTION = PreparedDistribution(
    agent_id=HOLDER,
    holding_account_id=BROKERAGE,
    asset_id=STOCK,
    to_account_id=CHECKING,
    tax_character=(PreparedDistributionSlice(fraction_ppb=1_000_000_000, issuer_jurisdiction_id=None),),
)


def holding_stock(*, payout: PreparedSeries) -> World:
    world = composed(prices(100, 100, 100), payout)
    world.declare_pool(pool())
    world.hold(lot())
    return world


def test_a_distribution_pays_whoever_holds_the_security_not_a_cash_account() -> None:
    holding_stock(payout=payouts(0, 0, 0)).declare_distribution(DISTRIBUTION)
    with pytest.raises(ValueError, match=f"references no lots for {HOLDER}:{CHECKING}:{STOCK}"):
        holding_stock(payout=payouts(0, 0, 0)).declare_distribution(replace(DISTRIBUTION, holding_account_id=CHECKING))
    with pytest.raises(ValueError, match="missing distribution series"):
        composed(prices(100, 100, 100)).declare_distribution(DISTRIBUTION)


@pytest.mark.parametrize(
    ("slices", "match"),
    [
        ((PreparedDistributionSlice(fraction_ppb=400_000_000, issuer_jurisdiction_id=None),), "tax character"),
        (
            (
                PreparedDistributionSlice(
                    fraction_ppb=1_000_000_000, issuer_jurisdiction_id=JurisdictionId("test-unknown")
                ),
            ),
            "unknown",
        ),
        (
            (PreparedDistributionSlice(fraction_ppb=1_000_000_000, issuer_jurisdiction_id=TAX_HOME.jurisdiction_id),),
            "undeclared income",
        ),
    ],
    ids=["incomplete", "unknown-issuer", "undeclared-source"],
)
def test_a_distribution_splits_its_tax_character_across_known_reported_issuers(
    slices: tuple[PreparedDistributionSlice, ...], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        holding_stock(payout=payouts(0, 0, 0)).declare_distribution(replace(DISTRIBUTION, tax_character=slices))


def test_a_zero_payout_is_valid_but_a_negative_one_is_not() -> None:
    holding_stock(payout=payouts(0, 0, 0)).declare_distribution(DISTRIBUTION)
    with pytest.raises(ValueError, match="negative security distribution"):
        holding_stock(payout=payouts(0, 0, -1)).declare_distribution(DISTRIBUTION)


def test_a_zero_mark_is_valid_only_where_the_asset_is_held_exclusively_through_a_manager() -> None:
    written_off = TlhOpeningCohort(value=0, cost_basis=100, purchase_month_index=-2)
    spec = PreparedTlhPortfolio(
        portfolio_id=PortfolioId("test-managed"),
        owner_agent_id=HOLDER,
        account_id=BROKERAGE,
        asset_id=STOCK,
        initial_cohorts=(written_off,),
        assumptions=FLAT,
    )
    managed = composed(prices(0, 0, 0))
    managed.declare_portfolio(spec)
    observed = managed.portfolios[spec.portfolio_id].observe()
    assert (observed.value, observed.reported_tax_basis) == (0, written_off.cost_basis)
    # An ordinary purchase pool sharing the quote restores the positive-price requirement:
    # managed ownership is pool-scoped.
    with pytest.raises(ValueError, match="non-positive value"):
        composed(prices(0, 0, 0)).declare_pool(pool())


def cpi(*levels: int) -> PreparedSeries:
    return PreparedSeries(series_id="inflation", snapshots=len(levels), values=levels)


def indexed(*, series_id: str = "inflation", base_month_index: int = 0) -> PreparedIndexedAmount:
    return PreparedIndexedAmount(
        base_amount=1, series_id=series_id, base_month_index=base_month_index, adjustment_period_months=1
    )


FLOW = PreparedTransfer(
    month=0,
    cause_id="test-transfer",
    from_account=ref(COUNTERPARTY),
    to_account=ref(HOLDER),
    amount=1,
    income_category=None,
    deduction_category=None,
)


@pytest.mark.parametrize(
    ("invalid", "match"),
    [
        (replace(FLOW, from_account=ref(AgentId("test-stranger"))), "unknown declared account"),
        (
            replace(FLOW, income_category=InterestIncome(issuer_jurisdiction_id=TAX_HOME.jurisdiction_id)),
            "undeclared income source",
        ),
        (replace(FLOW, month=HORIZON), "outside the horizon"),
        (
            PreparedRecurringTransfer(
                start_month=1,
                end_month=0,
                cause_id=FLOW.cause_id,
                from_account=FLOW.from_account,
                to_account=FLOW.to_account,
                amount=FLOW.amount,
                income_category=None,
                deduction_category=None,
            ),
            "before start month",
        ),
        (
            PreparedPropertyCashflow(
                month=0,
                property_id=PropertyId("test-unbought"),
                cause_id=FLOW.cause_id,
                from_account=FLOW.from_account,
                to_account=FLOW.to_account,
                amount=FLOW.amount,
                income_category=None,
                deduction_category=None,
            ),
            "undeclared property",
        ),
        (replace(FLOW, amount=indexed(series_id="rent:test-nowhere")), "missing series"),
        (replace(FLOW, amount=indexed(base_month_index=1)), "before base month"),
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
    invalid: PreparedTransfer | PreparedRecurringTransfer, match: str
) -> None:
    world = composed(cpi(1, 1, 1))
    world.declare_flow(FLOW)
    world.start()
    assert world.account_balance(HOLDER, CHECKING) == 1
    with pytest.raises(ValueError, match=match):
        composed(cpi(1, 1, 1)).declare_flow(invalid)


def test_an_indexed_amount_needs_a_nonzero_base_level() -> None:
    world = composed(cpi(1, 2, 2))
    world.declare_flow(replace(FLOW, amount=indexed()))
    with pytest.raises(ValueError, match="zero base level"):
        composed(cpi(0, 1, 1)).declare_flow(replace(FLOW, amount=indexed()))


def rented(month: int, property_id: PropertyId = PURCHASE.property_id) -> _RentedFraction:
    return _RentedFraction(month=month, property_id=property_id, rented_fraction_ppb=500_000_000)


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
                sales=(_PropertySale(month=1, property_id=PURCHASE.property_id, closing_cost_ppb=0),) * 2,
            ),
            "multiple sales",
        ),
        (
            Housing(
                purchases=(PURCHASE,),
                sales=(_PropertySale(month=1, property_id=PURCHASE.property_id, closing_cost_ppb=0),),
                rented_fraction_events=(rented(1),),
            ),
            "frozen after sale",
        ),
        (
            Housing(
                purchases=(PURCHASE,),
                initial_residences=(_PrimaryResidence(agent_id=COUNTERPARTY, property_id=PURCHASE.property_id),),
            ),
            "did not buy it",
        ),
        (
            Housing(
                purchases=(PURCHASE,),
                initial_residences=(_PrimaryResidence(agent_id=HOLDER, property_id=PURCHASE.property_id),) * 2,
            ),
            "multiple initial primary residences",
        ),
        (
            Housing(
                purchases=(PURCHASE,),
                residence_events=(_PrimaryResidenceEvent(month=HORIZON, agent_id=HOLDER, property_id=None),),
            ),
            "outside the horizon",
        ),
        (
            Housing(
                purchases=(PURCHASE,),
                residence_events=(_PrimaryResidenceEvent(month=1, agent_id=HOLDER, property_id=None),) * 2,
            ),
            "multiple primary residence events",
        ),
        (
            Housing(
                purchases=(PURCHASE,),
                residence_events=(
                    _PrimaryResidenceEvent(month=1, agent_id=AgentId("test-stranger"), property_id=None),
                ),
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
            initial_residences=(_PrimaryResidence(agent_id=HOLDER, property_id=PURCHASE.property_id),),
        ),
        (),
        (LOCATION,),
    )
    with pytest.raises(ValueError, match=match):
        composed().declare_housing(housing, (), (LOCATION,))


PROPERTY_TAX = _PropertyTax(
    property_id=PURCHASE.property_id,
    owner_agent_id=HOLDER,
    from_account_id=CHECKING,
    tax_authority_agent_id=COUNTERPARTY,
    tax_authority_account_id=CHECKING,
    annual_tax_rate_ppb=None,
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
def test_one_property_tax_policy_at_a_time_is_owed_by_the_buyer(policies: tuple[_PropertyTax, ...], match: str) -> None:
    composed().declare_housing(Housing(purchases=(PURCHASE,)), (PROPERTY_TAX,), (LOCATION,))
    with pytest.raises(ValueError, match=match):
        composed().declare_housing(Housing(purchases=(PURCHASE,)), policies, (LOCATION,))


def test_a_deduction_is_claimed_by_an_enrolled_taxpayer() -> None:
    with pytest.raises(ValueError, match="names no taxpayer"):
        composed().declare_deduction(
            _MortgageInterestDeduction(
                liability_id=LOAN.liability_id,
                owner_agent_id=HOLDER,
                debt_class="acquisition",
                per_jurisdiction_principal_cap={},
            )
        )


BILL = PreparedObligation(
    month=0,
    obligation_id="test-bill",
    obligation_type="cash_spend",
    from_account=ref(HOLDER),
    to_account=ref(COUNTERPARTY),
    amount_due=1,
    property_id=None,
    deduction_category=None,
    deductible_fraction_ppb=0,
)


def test_a_bill_is_due_inside_the_horizon_on_an_index_the_path_carries() -> None:
    composed().track(Biller(BILL))
    with pytest.raises(ValueError, match="outside the horizon"):
        composed().track(Biller(replace(BILL, month=HORIZON)))
    with pytest.raises(ValueError, match="missing series"):
        composed().track(Biller(replace(BILL, amount_due=indexed())))


def test_a_holding_pays_out_through_one_distribution() -> None:
    world = holding_stock(payout=payouts(0, 0, 0))
    world.declare_distribution(DISTRIBUTION)
    with pytest.raises(ValueError, match="duplicate distribution"):
        world.declare_distribution(DISTRIBUTION)


MANAGED = PreparedTlhPortfolio(
    portfolio_id=PortfolioId("test-managed"),
    owner_agent_id=HOLDER,
    account_id=BROKERAGE,
    asset_id=STOCK,
    initial_cohorts=(TlhOpeningCohort(value=100, cost_basis=100, purchase_month_index=-2),),
    assumptions=FLAT,
)
SOLE_OWNER = "must have exactly one component owner and no ordinary holdings"


def test_a_managed_portfolio_has_a_declared_owner_a_price_path_and_one_manager() -> None:
    world = composed(prices(100, 100, 100))
    world.declare_portfolio(MANAGED)
    with pytest.raises(ValueError, match="duplicate TLH portfolio"):
        world.declare_portfolio(replace(MANAGED, account_id=CHECKING))
    with pytest.raises(ValueError, match=SOLE_OWNER):
        world.declare_portfolio(replace(MANAGED, portfolio_id=PortfolioId("test-second")))
    with pytest.raises(ValueError, match="unknown owner"):
        composed(prices(100, 100, 100)).declare_portfolio(replace(MANAGED, owner_agent_id=AgentId("test-stranger")))
    with pytest.raises(ValueError, match="missing security series"):
        composed().declare_portfolio(MANAGED)
    # Zero is a mark a manager may carry; below zero is not a price at any snapshot, the terminal one included.
    composed(prices(100, 0, 0)).declare_portfolio(MANAGED)
    with pytest.raises(ValueError, match="index price must be nonnegative, got -1 at month 2"):
        composed(prices(100, 100, -1)).declare_portfolio(MANAGED)


def test_a_managed_portfolio_holds_its_pool_alone_whichever_is_declared_first() -> None:
    managed_first = composed(prices(100, 100, 100))
    managed_first.declare_portfolio(MANAGED)
    with pytest.raises(ValueError, match=SOLE_OWNER):
        managed_first.declare_pool(pool())
    with pytest.raises(ValueError, match="references no declared holding pool"):
        managed_first.hold(lot())
    # Ownership is pool-scoped: the same security in another account is an ordinary holding.
    managed_first.declare_pool(pool(account_id=CHECKING))
    managed_first.hold(lot(account_id=CHECKING))

    lot_first = holding_stock(payout=payouts(0, 0, 0))
    with pytest.raises(ValueError, match=SOLE_OWNER):
        lot_first.declare_portfolio(MANAGED)
    assert lot_first.managed is None
    pool_first = composed(prices(100, 100, 100))
    pool_first.declare_pool(pool())
    with pytest.raises(ValueError, match=SOLE_OWNER):
        pool_first.declare_portfolio(MANAGED)


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


def issuer_paths(**overrides: tuple[int, ...]) -> tuple[PreparedSeries, ...]:
    return tuple(
        PreparedSeries(
            series_id=f"private_equity_{channel}:{ISSUER}",
            snapshots=HORIZON + 1,
            values=overrides.get(channel, (level,) * (HORIZON + 1)),
        )
        for channel, level in QUIET.items()
    )


def holding_private(*series: PreparedSeries) -> None:
    world = composed(*series)
    asset_id = AssetId(f"private_equity:{ISSUER}")
    world.declare_pool(PreparedHoldingPool(agent_id=HOLDER, account_id=BROKERAGE, asset_id=asset_id, quantity_scale=1))
    world.hold(replace(lot(), asset_id=asset_id, quantity_scale=1))


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
