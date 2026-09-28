"""Money leaves the modeled world only when something recorded says it did.

Sum every modeled agent's cash and a transfer between two of them cancels, so the total moves
only by what crossed the boundary — a wage from an employer nobody models, a sale to a market
nobody models. The rule below is that this move equals what the engine recorded as crossing.

Disposals are why it is worth saying. When a sale credits proceeds with no matching debit, net
worth stays correct — the lot leaves as the cash arrives — so every agent-facing number looks
right while cash is minted from nothing. Each way of turning something into cash therefore
gets its own case: an explicit asset sale, a target-allocation sale, a private-equity tender,
and a property sale. Each asserts the disposal actually fired, because a sale that never
happened moves nothing and proves nothing.

Every case is minimal on purpose: in the month under test, the disposal is the only thing
crossing the boundary, so the total's move is the whole story. The mortgage payment and the
tax settlement that share the property sale's month are between modeled agents and cancel.

The stronger form of this — that no flow to an unmodeled counterparty can vanish, in any
month — is not stateable over these channels, because the external boundary is not one of
them. The engine's own counterpart is the double-entry journal it validates on every entry.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal

import polars as pl
import pytest_bazel
from more_itertools import one

from finance.augur.model.asset_key import PrivateEquityAssetKey
from finance.augur.model.series import (
    HomeValueKey,
    IssuerId,
    LevelSeriesKey,
    LocationId,
    PrivateEquityEventKindCode,
    SecurityKey,
    SecuritySymbol,
)
from finance.augur.policy.cash_band_household import CashBandHousehold, SecuritySleeve
from finance.augur.policy.funding import ClaimPayer
from finance.augur.sim.actions import Action, LotSale, Sell
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef
from finance.augur.sim.fixed_point import quantity_scale_for_asset, quantity_to_quanta, rate_to_ppb
from finance.augur.sim.ids import AccountId, AgentId, AssetId, JurisdictionId, LiabilityId, LotId, PropertyId
from finance.augur.sim.income import ORDINARY_INCOME
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath, Series
from finance.augur.sim.money import USD
from finance.augur.sim.private_equity import TenderPolicy
from finance.augur.sim.property import (
    CapitalImprovement,
    Housing,
    MortgageFinancing,
    Parcel,
    ScheduledPurchase,
    ScheduledSale,
)
from finance.augur.sim.property_tax import PropertyTaxPolicy
from finance.augur.sim.results import Rollout
from finance.augur.sim.schedule import Recurring
from finance.augur.sim.session import ActionSession
from finance.augur.sim.situs import compile_situs
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tax_indexation import FixedNominalLaw
from finance.augur.sim.tax_profile import TaxProfile, compile_profile
from finance.augur.sim.testing.issuer_protocol import at_month, issuer_protocol
from finance.augur.sim.testing.rollouts import book
from finance.augur.sim.testing.scripted import Scripted
from finance.augur.sim.testing.series import level_series
from finance.augur.sim.testing.session import each, finish
from finance.augur.sim.testing.situs import START_YEAR
from finance.augur.sim.world import World

QUANTA_PER_UNIT = 100
ALICE = AgentId("alice")
CHECKING = AccountId("checking")
FEDERAL = JurisdictionId("federal_us")
VTI = SecurityKey(symbol=SecuritySymbol("vti"))
VTI_SCALE = quantity_scale_for_asset(VTI)
ISSUER = IssuerId("acme")
ACME = PrivateEquityAssetKey(issuer_id=IssuerId(ISSUER))
ACME_ASSET_ID = AssetId("private_equity:acme")
ACME_SCALE = quantity_scale_for_asset(ACME)

SALE_MONTH = 4
SALE_UNITS, SALE_PRICE = 5_000, Decimal(150)
SALE_PROCEEDS_QUANTA = SALE_UNITS * int(SALE_PRICE) * QUANTA_PER_UNIT

RENT_MONTH = 0
RENT = Decimal(5_000)

TENDER_MONTH, TENDER_HORIZON = 12, 24
TENDER_UNITS, TENDER_MARK = 100, Decimal(50)
TENDER_PROCEEDS_QUANTA = TENDER_UNITS * int(TENDER_MARK) * QUANTA_PER_UNIT

PROPERTY_HORIZON, PROPERTY_SALE_MONTH, CAPEX_MONTH = 36, 24, 12
PROPERTY_LOCATION_ID = LocationId("loc")
COUNTY = AgentId("county")


def account(world: World, agent_id: AgentId, balance: Decimal | int = 0) -> None:
    world.declare_account(
        account=AccountRef(agent_id=agent_id, account_id=CHECKING), opening_balance=USD.quanta(balance)
    )


def ref(agent_id: AgentId) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=CHECKING)


def path(key: LevelSeriesKey, levels: Sequence[Decimal], *, horizon_months: int) -> tuple[Series, ...]:
    """One exogenous level series on the single rollout every case here runs."""

    return level_series({key: [[float(level) for level in levels]]}, rollout_count=1, horizon_months=horizon_months)


def hold_vti(
    world: World, lot_id: LotId, *, quantity: Decimal | int, cost_basis: Decimal | int, purchase_month: int
) -> None:
    """Alice's VTI pool in checking, holding one lot."""
    world.declare_pool(agent_id=ALICE, account_id=CHECKING, asset_id=AssetId(VTI.symbol), quantity_scale=VTI_SCALE)
    world.hold_lot(
        lot_id=lot_id,
        agent_id=ALICE,
        account_id=CHECKING,
        asset_id=AssetId(VTI.symbol),
        purchase_month=purchase_month,
        quantity_scale=VTI_SCALE,
        units=quantity_to_quanta(quantity, scale=VTI_SCALE),
        basis=USD.quanta(cost_basis),
    )


def run(world: World, *, funded_by: SecurityKey | None = None, script: Mapping[int, Sequence[Action]] = {}) -> Rollout:
    """Alice makes her scripted trades, sells `funded_by` as her claims need, then pays every due claim in full, in order."""

    household = Scripted(ClaimPayer(ALICE) if funded_by is None else sell_into_cash(funded_by), script)
    [rollout] = finish(ActionSession({0: world}, ALICE), each(household.decide)).rollouts
    assert rollout.stop is None
    return rollout


def crossed_the_boundary(rollout: Rollout, declared: frozenset[AccountRef], *, month: int) -> int:
    """What the modeled world gained in one month.

    Every modeled agent's declared cash, summed: money moving between two of them cancels, so
    what is left is what entered from outside. Snapshot `m` is the state entering month `m`,
    so month `m`'s move is the difference between snapshots `m + 1` and `m`.
    """

    def total(snapshot: int) -> int:
        return sum(row.balance for row in book(rollout, snapshot).balances if row.account in declared)

    return total(month + 1) - total(month)


def proceeds(rollout: Rollout, *, month: int) -> int:
    """What the dispositions in one month say they brought in."""

    assert rollout.trace is not None
    return int(
        rollout.trace.events.lot_dispositions.filter(pl.col("month_index") == month).get_column("proceeds_quanta").sum()
    )


def security_sale_world() -> World:
    """The reported symptom, minimized: hold $500,000 of an asset, sell it for $750,000.

    Net worth is right either way — the lot leaves as the cash arrives — so the $250,000 gain
    is not what is under test. Crediting $750,000 with nobody debited is.
    """

    world = World(
        MarketPath(path(VTI, [SALE_PRICE] * 7, horizon_months=6), 0, rollout_count=1),
        horizon_months=6,
        income_sources=(ORDINARY_INCOME,),
    )
    account(world, ALICE, Decimal(1_000_000))
    hold_vti(world, LotId("bought"), quantity=SALE_UNITS, cost_basis=SALE_UNITS * 100, purchase_month=0)
    return world


def target_allocation_world() -> World:
    """Alice cannot cover the rent from cash, so the policy raises it by selling VTI."""

    world = World(
        MarketPath(path(VTI, [Decimal(100)] * 4, horizon_months=3), 0, rollout_count=1),
        horizon_months=3,
        income_sources=(ORDINARY_INCOME,),
    )
    account(world, ALICE, Decimal(1_000))
    account(world, AgentId("landlord"))
    hold_vti(world, LotId("alice-vti"), quantity=200, cost_basis=10_000, purchase_month=-1)
    world.track(
        Biller(
            schedule=Recurring(start_month=RENT_MONTH, end_month=None),
            obligation_id="alice-rent",
            obligation_type="rent",
            from_account=ref(ALICE),
            to_account=ref(AgentId("landlord")),
            amount_due=USD.quanta(RENT),
            property_id=None,
            deduction_category=None,
            deductible_fraction_ppb=rate_to_ppb(1),
        )
    )
    return world


def sell_into_cash(asset: SecurityKey) -> CashBandHousehold:
    """A band with no floor and no ceiling: Alice holds no spare cash and funds her claims by selling."""
    return CashBandHousehold(
        ALICE,
        cash_account_id=CHECKING,
        floor=0,
        ceiling=0,
        sleeves=(SecuritySleeve(asset_id=AssetId(asset.symbol), weight=1),),
        source_account_ids=(CHECKING,),
        reinvest=None,
        cause_id_prefix="allocation_sale",
    )


def private_equity_tender_world() -> World:
    """Alice holds illiquid PE and sits under her floor, so the tender sells the whole stake."""

    snapshots = TENDER_HORIZON + 1
    world = World(
        MarketPath(
            issuer_protocol(
                ISSUER,
                horizon_months=TENDER_HORIZON,
                mark_usd=TENDER_MARK,
                event_kind=at_month(
                    int(PrivateEquityEventKindCode.TENDER), month=TENDER_MONTH, default=0, snapshots=snapshots
                ),
                sale_opportunity=at_month(1, month=TENDER_MONTH, default=0, snapshots=snapshots),
            ),
            0,
            rollout_count=1,
        ),
        horizon_months=TENDER_HORIZON,
        income_sources=(ORDINARY_INCOME,),
    )
    account(world, ALICE, Decimal(100_000))
    world.declare_pool(agent_id=ALICE, account_id=CHECKING, asset_id=ACME_ASSET_ID, quantity_scale=ACME_SCALE)
    world.hold_lot(
        lot_id=LotId("acme-lot"),
        agent_id=ALICE,
        account_id=CHECKING,
        asset_id=ACME_ASSET_ID,
        purchase_month=-36,
        quantity_scale=ACME_SCALE,
        units=quantity_to_quanta(TENDER_UNITS, scale=ACME_SCALE),
        basis=USD.quanta(Decimal(TENDER_UNITS) * 10),
    )
    world.declare_tender_policy(
        TenderPolicy(owner_agent_id=ALICE, proceeds_account_id=CHECKING, liquid_net_worth_floor=USD.quanta(500_000))
    )
    return world


def property_sale_world() -> World:
    """A mortgaged house, a roof paid for out of pocket, and a sale that pays the loan off.

    Both non-cash-neutral halves of the property lifecycle are here: the capital improvement
    (cash out to a contractor nobody models) and the sale (cash in from a buyer nobody models,
    net of a payoff that extinguishes a liability rather than moving cash).
    """

    home_values = [Decimal(1)] * PROPERTY_SALE_MONTH + [Decimal("1.5")] * (PROPERTY_HORIZON + 1 - PROPERTY_SALE_MONTH)
    jurisdictions = {FEDERAL: load_jurisdiction(FEDERAL)}
    world = World(
        MarketPath(
            path(
                HomeValueKey(location_id=LocationId(PROPERTY_LOCATION_ID)), home_values, horizon_months=PROPERTY_HORIZON
            ),
            0,
            rollout_count=1,
        ),
        horizon_months=PROPERTY_HORIZON,
        income_sources=(ORDINARY_INCOME,),
    )
    for agent_id, balance in (
        (ALICE, Decimal(1_000_000)),
        (AgentId("seller"), 0),
        (AgentId("bank"), 0),
        (AgentId("irs"), 0),
        (COUNTY, 0),
    ):
        account(world, agent_id, balance)
    world.track(
        TaxAuthority(
            compile_profile(
                TaxProfile(agent_id=ALICE, jurisdiction_ids=[FEDERAL], tax_authority_agent_id=AgentId("irs")),
                jurisdictions,
                currency=USD,
            ),
            indexation=FixedNominalLaw(),
        )
    )
    world.declare_housing(
        Housing(
            purchases=(
                ScheduledPurchase(
                    month=0,
                    cause_id="buy-house",
                    property_id=PropertyId("house"),
                    parcel=Parcel(
                        situs=compile_situs(load_jurisdiction(JurisdictionId("san_francisco")), currency=USD)
                    ),
                    market=PROPERTY_LOCATION_ID,
                    buyer_agent_id=ALICE,
                    buyer_account_id=CHECKING,
                    seller_agent_id=AgentId("seller"),
                    seller_account_id=CHECKING,
                    purchase_price=USD.quanta(500_000),
                    down_payment=USD.quanta(100_000),
                    buyer_closing_cost=0,
                    rented_fraction_ppb=0,
                    land_value_fraction_ppb=rate_to_ppb(Decimal("0.2")),
                    mortgage=MortgageFinancing(
                        liability_id=LiabilityId("house-mortgage"),
                        lender_agent_id=AgentId("bank"),
                        lender_account_id=CHECKING,
                        principal=USD.quanta(400_000),
                        annual_interest_rate_ppb=rate_to_ppb(Decimal("0.06")),
                        term_months=360,
                    ),
                ),
            ),
            sales=(
                ScheduledSale(
                    month=PROPERTY_SALE_MONTH,
                    property_id=PropertyId("house"),
                    closing_cost_ppb=rate_to_ppb(Decimal("0.06")),
                ),
            ),
            capital_improvements=(
                CapitalImprovement(
                    month=CAPEX_MONTH, property_id=PropertyId("house"), amount=USD.quanta(30_000), description="roof"
                ),
            ),
        ),
        (
            PropertyTaxPolicy(
                property_id=PropertyId("house"),
                owner_agent_id=ALICE,
                from_account_id=CHECKING,
                tax_authority_agent_id=COUNTY,
                tax_authority_account_id=CHECKING,
                start_year=START_YEAR,
                start_month=0,
                end_month=None,
            ),
        ),
    )
    return world


def declared(world: World) -> frozenset[AccountRef]:
    return frozenset(world.accounting.declared)


def test_a_security_sale_brings_in_exactly_its_proceeds() -> None:
    world = security_sale_world()
    accounts = declared(world)
    rollout = run(
        world,
        script={
            SALE_MONTH: (
                Sell(
                    cause_id="sell-vti",
                    agent_id=ALICE,
                    proceeds_account_id=CHECKING,
                    asset_id=AssetId(VTI.symbol),
                    lots=(
                        LotSale(
                            account_id=CHECKING,
                            lot_id=LotId("bought"),
                            units=quantity_to_quanta(SALE_UNITS, scale=VTI_SCALE),
                        ),
                    ),
                ),
            )
        },
    )

    assert proceeds(rollout, month=SALE_MONTH) == SALE_PROCEEDS_QUANTA
    assert crossed_the_boundary(rollout, accounts, month=SALE_MONTH) == SALE_PROCEEDS_QUANTA


def test_a_target_allocation_sale_brings_in_exactly_its_proceeds() -> None:
    """The rent it was raised for is between two modeled agents, so it cancels and only
    the sale is left."""

    world = target_allocation_world()
    accounts = declared(world)
    rollout = run(world, funded_by=VTI)
    raised = proceeds(rollout, month=RENT_MONTH)

    assert raised > 0
    assert crossed_the_boundary(rollout, accounts, month=RENT_MONTH) == raised


def test_a_private_equity_tender_brings_in_exactly_its_proceeds() -> None:
    """The whole 100-unit stake tenders at $50: a $5,000 credit with nothing to debit."""

    world = private_equity_tender_world()
    accounts = declared(world)
    rollout = run(world)

    assert proceeds(rollout, month=TENDER_MONTH) == TENDER_PROCEEDS_QUANTA
    assert crossed_the_boundary(rollout, accounts, month=TENDER_MONTH) == TENDER_PROCEEDS_QUANTA


def test_a_property_sale_brings_in_its_net_and_not_its_gross() -> None:
    """The mortgage payoff never leaves the modeled world — it extinguishes a liability —
    and the closing costs never arrive, so only the owner's net crosses the boundary."""

    world = property_sale_world()
    accounts = declared(world)
    rollout = run(world)
    assert rollout.trace is not None
    sale = rollout.trace.events.property_sale_events.row(0, named=True)

    assert sale["mortgage_payoff_quanta"] > 0, "a payoff of nothing would not tell gross from net"
    assert sale["net_cash_to_owner_quanta"] > 0
    assert crossed_the_boundary(rollout, accounts, month=PROPERTY_SALE_MONTH) == sale["net_cash_to_owner_quanta"]


def test_property_tax_moves_cash_only_to_the_modeled_county() -> None:
    """A month whose only flows are the installment and the tax bill: both reach a modeled agent."""

    world = property_sale_world()
    accounts = declared(world)
    rollout = run(world)
    county = AccountRef(agent_id=COUNTY, account_id=CHECKING)

    assert crossed_the_boundary(rollout, accounts, month=1) == 0
    assert one(row.balance for row in book(rollout, 2).balances if row.account == county) > 0


def test_a_capital_improvement_takes_cash_out_of_the_modeled_world() -> None:
    """The other direction, and the anti-vacuity check for the case above: a boundary that
    only ever credits would pass the sale assertion while losing every outflow."""

    world = property_sale_world()
    accounts = declared(world)
    rollout = run(world)
    assert rollout.trace is not None
    capex = rollout.trace.events.capital_improvement_events

    assert capex.height == 1
    assert crossed_the_boundary(rollout, accounts, month=CAPEX_MONTH) == -int(capex.get_column("amount_quanta").sum())


if __name__ == "__main__":
    pytest_bazel.main()
