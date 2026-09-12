"""A world refuses a fact it cannot execute where that fact is declared, before it holds anything.

Every rejection here is the declaration's own: the path admits only dense series it can read, a
pool admits only a quote that is a price, a lot needs the pool it sits in, a bond is bought at
par, a distribution pays whoever holds the security, and a purchase names a location and a
funded price.
"""

from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.sim.books import AccountRef
from finance.augur.sim.jurisdictions import JurisdictionLevel
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedBond,
    PreparedDistribution,
    PreparedDistributionSlice,
    PreparedFixedAmount,
    PreparedHoldingPool,
    PreparedIndexedCoupon,
    PreparedJurisdiction,
    PreparedLocation,
    PreparedLot,
    PreparedSeries,
    PreparedTlhPortfolio,
    PreparedTransfer,
    _MortgageFinancing,
    _PropertyPurchase,
)
from finance.augur.sim.property import Housing
from finance.augur.sim.scenario import ORDINARY_INCOME, InterestIncome
from finance.augur.sim.tlh import TlhAssumptions
from finance.augur.sim.world import World

HOLDER = "test-holder"
COUNTERPARTY = "test-counterparty"
CHECKING = "checking"
BROKERAGE = "brokerage"
STOCK = "test-stock"
SCALE = 1000
HORIZON = 2
TAX_HOME = PreparedJurisdiction(jurisdiction_id="test-jurisdiction", level=JurisdictionLevel.STATE)
LOCATION = PreparedLocation(
    location_id="test-market",
    display_name="Test market",
    jurisdiction_ids=(),
    annual_property_tax_rate_ppb=0,
    annual_special_assessment=0,
)
FLAT = TlhAssumptions(
    peak_annual_yield=0, floor_annual_yield=0, maturity_decay_exponent=1, drawdown_sensitivity=0, short_term_fraction=1
)


def ref(agent_id: str, account_id: str = CHECKING) -> AccountRef:
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


def pool(*, account_id: str = BROKERAGE, quantity_scale: int = SCALE) -> PreparedHoldingPool:
    return PreparedHoldingPool(agent_id=HOLDER, account_id=account_id, asset_id=STOCK, quantity_scale=quantity_scale)


def lot(lot_id: str = "test-lot", *, account_id: str = BROKERAGE, quantity_scale: int = SCALE) -> PreparedLot:
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


# A par bond paying a fixed semiannual coupon over two whole periods.
BOND = PreparedBond(
    bond_id="test-bond",
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
        (replace(BOND, issuer_jurisdiction_id="test-unknown"), "unknown issuer"),
        (replace(BOND, account_id="test-undeclared"), "unknown account"),
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
    property_id="test-home",
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
    liability_id="test-loan",
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
        housed(replace(PURCHASE, seller_agent_id="test-stranger"), LOCATION)


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
        ((PreparedDistributionSlice(fraction_ppb=1_000_000_000, issuer_jurisdiction_id="test-unknown"),), "unknown"),
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
    cohort = lot("test-cohort")
    spec = PreparedTlhPortfolio(
        portfolio_id="test-managed",
        owner_agent_id=HOLDER,
        account_id=BROKERAGE,
        asset_id=STOCK,
        quantity_scale=SCALE,
        initial_cohorts=(cohort,),
        assumptions=FLAT,
    )
    managed = composed(prices(0, 0, 0))
    managed.declare_portfolio(spec)
    observed = managed.portfolios[spec.portfolio_id].observe()
    assert (observed.value, observed.reported_tax_basis) == (0, cohort.basis)
    # An ordinary purchase pool sharing the quote restores the positive-price requirement:
    # managed ownership is pool-scoped.
    with pytest.raises(ValueError, match="non-positive value"):
        composed(prices(0, 0, 0)).declare_pool(pool())


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
        (replace(FLOW, from_account=ref("test-stranger")), "unknown declared account"),
        (
            replace(FLOW, income_category=InterestIncome(issuer_jurisdiction_id=TAX_HOME.jurisdiction_id)),
            "undeclared income source",
        ),
    ],
    ids=["unknown-account", "undeclared-income"],
)
def test_a_scheduled_flow_moves_declared_cash_from_a_declared_income_source(
    invalid: PreparedTransfer, match: str
) -> None:
    world = composed()
    world.scheduled_transfers = (FLOW,)
    world.start()
    assert world.account_balance(HOLDER, CHECKING) == 1
    stopped = composed()
    stopped.scheduled_transfers = (invalid,)
    with pytest.raises(ValueError, match=match):
        stopped.start()


if __name__ == "__main__":
    pytest_bazel.main()
