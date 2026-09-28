"""Transfer taxes and the acquisition costs a property's basis carries, against hand arithmetic."""

from dataclasses import replace
from decimal import Decimal

import polars as pl
import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.model.series import LocationId
from finance.augur.policy.funding import ClaimPayer
from finance.augur.sim.actions import DecisionActions
from finance.augur.sim.books import AccountRef
from finance.augur.sim.fixed_point import rate_to_ppb
from finance.augur.sim.ids import AccountId, AgentId, JurisdictionId, PropertyId
from finance.augur.sim.income import ORDINARY_INCOME
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath, Series
from finance.augur.sim.money import USD
from finance.augur.sim.property import Housing, Parcel, ScheduledPurchase, ScheduledSale
from finance.augur.sim.property_tax import PropertyTaxPolicy
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.session import ActionSession
from finance.augur.sim.situs import compile_situs
from finance.augur.sim.testing.situs import START_YEAR, UNTAXED
from finance.augur.sim.world import World

OWNER, SELLER, COUNTY = AgentId("test-owner"), AgentId("test-seller"), AgentId("test-county")
CHECKING = AccountId("checking")
HOME = PropertyId("test-home")
SAN_FRANCISCO = compile_situs(load_jurisdiction(JurisdictionId("san_francisco")), currency=USD)
MAINLAND_VALLEJO = compile_situs(load_jurisdiction(JurisdictionId("solano_tra_007000")), currency=USD)


def dollars(amount: Decimal | int | str) -> int:
    return USD.quanta(Decimal(amount))


@pytest.mark.parametrize(
    ("consideration", "tax"),
    [
        # "More than $100": $100 pays nothing, a cent more one $500 unit at $2.50.
        ("100", "0"),
        ("100.01", "2.50"),
        # $250,000 is the top of $2.50 a $500; a cent more takes $3.40 on all 501 units.
        ("250000", "1250"),
        ("250000.01", "1703.40"),
        ("999999.99", "6800"),
        # "$1,000,000 or more": the edge itself takes the higher rate, on the whole price.
        ("1000000", "7500"),
        ("4999999.99", "37500"),
        ("5000000", "112500"),
        ("9999999.99", "225000"),
        ("10000000", "550000"),
        ("24999999.99", "1375000"),
        ("25000000", "1500000"),
    ],
)
def test_san_francisco_taxes_the_whole_consideration_at_its_bracket_s_rate(consideration: str, tax: str) -> None:
    assert SAN_FRANCISCO.transfer_tax(dollars(consideration)) == dollars(tax)


@pytest.mark.parametrize(
    ("consideration", "tax"),
    [
        # Solano County's $0.55 and Vallejo's $1.65 on each $500 or part: 1,200 units.
        ("600000", "2640"),
        # A cent more is a 1,201st unit for both.
        ("600000.01", "2642.20"),
        ("100", "0"),
    ],
)
def test_mainland_vallejo_pays_the_county_s_and_the_city_s_tax(consideration: str, tax: str) -> None:
    assert MAINLAND_VALLEJO.transfer_tax(dollars(consideration)) == dollars(tax)


def purchase(parcel: Parcel, *, buyer_share: Decimal = Decimal(0), rented: int = 0) -> ScheduledPurchase:
    return ScheduledPurchase(
        month=0,
        cause_id="test-purchase",
        property_id=HOME,
        parcel=parcel,
        market=LocationId("test-market"),
        buyer_agent_id=OWNER,
        buyer_account_id=CHECKING,
        seller_agent_id=SELLER,
        seller_account_id=CHECKING,
        purchase_price=dollars(1_000_000),
        down_payment=dollars(1_000_000),
        buyer_closing_cost=dollars(10_000),
        rented_fraction_ppb=rate_to_ppb(rented),
        land_value_fraction_ppb=rate_to_ppb(Decimal("0.2")),
        mortgage=None,
        buyer_transfer_tax_share_ppb=rate_to_ppb(buyer_share),
    )


# Commission 5% and escrow and title 1% of a $1,500,000 sale.
SALE = ScheduledSale(
    month=3,
    property_id=HOME,
    commission_ppb=rate_to_ppb(Decimal("0.05")),
    escrow_title_ppb=rate_to_ppb(Decimal("0.01")),
)
POLICY = PropertyTaxPolicy(
    property_id=HOME,
    owner_agent_id=OWNER,
    from_account_id=CHECKING,
    tax_authority_agent_id=COUNTY,
    tax_authority_account_id=CHECKING,
    start_year=START_YEAR,
    start_month=0,
    end_month=None,
)


def compose(housing: Housing, policies: tuple[PropertyTaxPolicy, ...] = (POLICY,)) -> World:
    """The home's market is worth 1.5 times the price from month 3."""
    world = World(
        MarketPath(
            (Series(series_id="home_value:test-market", snapshots=5, values=(100, 100, 100, 150, 150)),),
            0,
            rollout_count=1,
        ),
        horizon_months=4,
        income_sources=(ORDINARY_INCOME,),
    )
    for agent in (OWNER, SELLER, COUNTY):
        world.declare_account(
            account=AccountRef(agent_id=agent, account_id=CHECKING), opening_balance=dollars(2_000_000)
        )
    world.declare_housing(housing, policies)
    return world


def run(world: World) -> Rollout:
    household = ClaimPayer(OWNER)
    session = ActionSession({0: world}, OWNER)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(
                [
                    DecisionActions(
                        decision.rollout_id, decision.observation.month, household.decide(decision.observation)
                    )
                    for decision in batch
                ]
            )
    finally:
        session.close()
    rollout = one(batch.rollouts)
    assert rollout.stop is None
    return rollout


def test_the_buyer_s_share_is_basis_and_the_seller_s_reduces_the_amount_realized() -> None:
    """Bought in San Francisco for $1,000,000 with $10,000 of closing costs, the buyer paying half of
    the $7,500 transfer tax: basis 1,000,000 + 10,000 + 3,750 = $1,013,750. Sold for $1,500,000, the
    seller paying all of its $11,250 (3.75 × 3,000): realized 1,500,000 - 75,000 - 15,000 - 11,250 =
    $1,398,750, a gain of $385,000. Both shares are billed to the owner and paid to the county."""
    rollout = run(
        compose(
            Housing(
                purchases=(
                    purchase(Parcel(situs=SAN_FRANCISCO, prior_assessed_value=None), buyer_share=Decimal("0.5")),
                ),
                sales=(SALE,),
            )
        )
    )
    assert rollout.trace is not None
    events = rollout.trace.events
    bought = one(events.property_purchases.to_dicts())
    sold = one(events.property_sale_events.to_dicts())
    transfers = events.obligation_settlements.filter(pl.col("obligation_type") == "transfer_tax").sort("month_index")

    assert (bought["transfer_tax_quanta"], bought["adjusted_basis_quanta"]) == (dollars(3_750), dollars(1_013_750))
    assert transfers.select("month_index", "amount_paid_quanta").rows() == [(0, dollars(3_750)), (3, dollars(11_250))]
    assert (sold["commission_quanta"], sold["escrow_title_quanta"], sold["transfer_tax_quanta"]) == (
        dollars(75_000),
        dollars(15_000),
        dollars(11_250),
    )
    assert sold["realized_gain_quanta"] == dollars(385_000)


def test_a_buyer_paid_sale_leaves_the_seller_nothing_to_pay() -> None:
    """The buyer pays the whole sale's tax: no bill, and realized 1,500,000 - 90,000 = $1,410,000 on a
    basis of $1,010,000."""
    sale = replace(SALE, buyer_transfer_tax_share_ppb=rate_to_ppb(1))
    rollout = run(
        compose(Housing(purchases=(purchase(Parcel(situs=SAN_FRANCISCO, prior_assessed_value=None)),), sales=(sale,)))
    )
    assert rollout.trace is not None
    events = rollout.trace.events

    assert events.obligation_settlements.filter(
        (pl.col("obligation_type") == "transfer_tax") & (pl.col("month_index") == 3)
    ).is_empty()
    assert one(events.property_sale_events.to_dicts())["realized_gain_quanta"] == dollars(400_000)


def test_settlement_costs_are_divided_between_land_and_building() -> None:
    """IRS Publication 527, "Separating cost of land and buildings": the $1,010,000 cost, closing
    costs included, is 20% land, so the depreciable building is $808,000."""
    world = compose(Housing(purchases=(purchase(UNTAXED, rented=1),)), ())
    world.prepare_month(0, {}, {})
    assert world.properties is not None

    assert world.properties.properties[HOME].state.building_basis_initial == dollars(808_000)


def test_a_transfer_tax_the_owner_owes_needs_an_authority_to_charge_it() -> None:
    with pytest.raises(ValueError, match="which no property tax policy charges"):
        compose(
            Housing(purchases=(purchase(Parcel(situs=SAN_FRANCISCO, prior_assessed_value=None)),), sales=(SALE,)), ()
        )


if __name__ == "__main__":
    pytest_bazel.main()
