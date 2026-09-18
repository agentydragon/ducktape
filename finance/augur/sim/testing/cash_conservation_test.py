"""Money leaves the modeled world only when something recorded says it did.

Sum every modeled agent's cash and a transfer between two of them cancels, so the total moves
only by what crossed the boundary — a wage from an employer nobody models, a sale to a market
nobody models. The rule below is that this move equals what the engine recorded as crossing.

Disposals are why it is worth saying. When a sale credits proceeds with no matching debit, net
worth stays correct — the lot leaves as the cash arrives — so every agent-facing number looks
right while cash is minted from nothing. Each way of turning something into cash therefore
gets its own case: a scheduled asset sale, a target-allocation sale, a private-equity tender,
and a property sale. Each asserts the disposal actually fired, because a sale that never
happened moves nothing and proves nothing.

Every case is minimal on purpose: in the month under test, the disposal is the only thing
crossing the boundary, so the total's move is the whole story. The mortgage payment and the
tax settlement that share the property sale's month are between modeled agents and cancel.

The stronger form of this — that no flow to an unmodeled counterparty can vanish, in any
month — is not stateable over these channels, because the external boundary is not one of
them. The engine's own counterpart is the double-entry journal it validates on every entry.
"""

from collections.abc import Sequence
from decimal import Decimal

import numpy as np
import polars as pl
import pytest_bazel

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
from finance.augur.policy.configured_household import ConfiguredHousehold
from finance.augur.sim.actions import DecisionActions
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
    PreparedSeries,
    _AllocationPolicy,
    _CapitalImprovement,
    _MortgageFinancing,
    _PropertyPurchase,
    _PropertySale,
    _ScheduledSale,
    _SleeveTarget,
    _TenderPolicy,
)
from finance.augur.sim.property import Housing
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.scenario import ORDINARY_INCOME, TaxProfile
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.testing.issuer_protocol import at_month, issuer_protocol
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
QUANTA_PER_UNIT = 100
ALICE = "alice"
CHECKING = "checking"
FEDERAL = "federal_us"
VTI = SecurityKey(symbol=SecuritySymbol("vti"))
VTI_SCALE = quantity_scale_for_asset(VTI)
ISSUER = "acme"
ACME = PrivateEquityAssetKey(issuer_id=IssuerId(ISSUER))
ACME_ASSET_ID = "private_equity:acme"
ACME_SCALE = quantity_scale_for_asset(ACME)

SALE_MONTH = 4
SALE_UNITS, SALE_PRICE = 5_000.0, Decimal(150)
SALE_PROCEEDS_QUANTA = int(SALE_UNITS * int(SALE_PRICE)) * QUANTA_PER_UNIT

RENT_MONTH = 0
RENT = Decimal(5_000)

TENDER_MONTH, TENDER_HORIZON = 12, 24
TENDER_UNITS, TENDER_MARK = 100.0, Decimal(50)
TENDER_PROCEEDS_QUANTA = int(TENDER_UNITS * int(TENDER_MARK)) * QUANTA_PER_UNIT

PROPERTY_HORIZON, PROPERTY_SALE_MONTH, CAPEX_MONTH = 36, 24, 12
PROPERTY_LOCATION_ID = "loc"
PROPERTY_LOCATION = PreparedLocation(
    location_id=PROPERTY_LOCATION_ID,
    display_name="Loc",
    jurisdiction_ids=(FEDERAL,),
    annual_property_tax_rate_ppb=0,
    annual_special_assessment=0,
)


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def account(agent_id: str, balance: Decimal | int = 0) -> PreparedAccount:
    return PreparedAccount(account=AccountRef(agent_id=agent_id, account_id=CHECKING), opening_balance=money(balance))


def ref(agent_id: str) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=CHECKING)


def path(key: LevelSeriesKey, levels: Sequence[Decimal], *, horizon_months: int) -> tuple[PreparedSeries, ...]:
    """One exogenous level series on the single rollout every case here runs."""

    return compile_series(
        ExternalSeriesContext.from_level_blocks(
            [(key, np.asarray([[float(level) for level in levels]], dtype=np.float64))],
            rollout_count=1,
            horizon_months=horizon_months,
        ),
        rollout_count=1,
        horizon_months=horizon_months,
        currency_quantum=QUANTUM,
    )


def vti_lot(lot_id: str, *, quantity: float, cost_basis: Decimal | int, purchase_month: int) -> PreparedLot:
    return PreparedLot(
        lot_id=lot_id,
        agent_id=ALICE,
        account_id=CHECKING,
        asset_id=str(VTI.symbol),
        purchase_month=purchase_month,
        quantity_scale=VTI_SCALE,
        units=int(quantity_to_quanta(quantity, scale=VTI_SCALE)),
        basis=money(cost_basis),
    )


def hold_vti(world: World, lot: PreparedLot) -> None:
    world.declare_pool(
        PreparedHoldingPool(agent_id=ALICE, account_id=CHECKING, asset_id=str(VTI.symbol), quantity_scale=VTI_SCALE)
    )
    world.hold(lot)


def run(
    world: World, *, policies: tuple[_AllocationPolicy, ...] = (), scheduled_sales: tuple[_ScheduledSale, ...] = ()
) -> Rollout:
    """Alice sells on her schedule and her funding policy, then pays each account's claims all or none."""

    household = ConfiguredHousehold(AgentId(ALICE), policies, scheduled_sales=scheduled_sales)
    session = ActionSession({0: world}, ALICE)
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
        [rollout] = batch.rollouts
        assert rollout.stop is None
        return rollout
    finally:
        session.close()


def book(rollout: Rollout, month: int) -> Book:
    assert rollout.trace is not None
    [snapshot] = [snapshot for snapshot in rollout.trace.books if snapshot.month == month]
    return snapshot


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


def scheduled_sale_world() -> World:
    """The reported symptom, minimized: hold $500,000 of an asset, sell it for $750,000.

    Net worth is right either way — the lot leaves as the cash arrives — so the $250,000 gain
    is not what is under test. Crediting $750,000 with nobody debited is.
    """

    world = World(
        MarketPath(path(VTI, [SALE_PRICE] * 7, horizon_months=6), 0, rollout_count=1),
        horizon_months=6,
        income_sources=(ORDINARY_INCOME,),
    )
    world.declare_account(account(ALICE, Decimal(1_000_000)))
    hold_vti(world, vti_lot("bought", quantity=SALE_UNITS, cost_basis=int(SALE_UNITS) * 100, purchase_month=0))
    return world


def target_allocation_world() -> World:
    """Alice cannot cover the rent from cash, so the policy raises it by selling VTI."""

    world = World(
        MarketPath(path(VTI, [Decimal(100)] * 4, horizon_months=3), 0, rollout_count=1),
        horizon_months=3,
        income_sources=(ORDINARY_INCOME,),
    )
    world.declare_account(account(ALICE, Decimal(1_000)))
    world.declare_account(account("landlord"))
    hold_vti(world, vti_lot("alice-vti", quantity=200.0, cost_basis=10_000, purchase_month=-1))
    world.track(
        Biller(
            PreparedRecurringObligation(
                start_month=RENT_MONTH,
                end_month=None,
                obligation_id="alice-rent",
                obligation_type="rent",
                from_account=ref(ALICE),
                to_account=ref("landlord"),
                amount_due=money(RENT),
                property_id=None,
                deduction_category=None,
                deductible_fraction_ppb=rate_to_ppb(1.0),
            )
        )
    )
    return world


VTI_BAND = _AllocationPolicy(
    agent_id=ALICE,
    account_id=CHECKING,
    source_account_ids=(),
    sleeves=(_SleeveTarget(asset_id=str(VTI.symbol), weight=1, quantity_scale=VTI_SCALE),),
    cash_floor=0,
    cash_ceiling=0,
    cause_id_prefix="allocation_sale",
    allow_purchases=False,
    rebalance_tolerance_ppb=None,
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
    world.declare_account(account(ALICE, Decimal(100_000)))
    world.declare_pool(
        PreparedHoldingPool(agent_id=ALICE, account_id=CHECKING, asset_id=ACME_ASSET_ID, quantity_scale=ACME_SCALE)
    )
    world.hold(
        PreparedLot(
            lot_id="acme-lot",
            agent_id=ALICE,
            account_id=CHECKING,
            asset_id=ACME_ASSET_ID,
            purchase_month=-36,
            quantity_scale=ACME_SCALE,
            units=int(quantity_to_quanta(TENDER_UNITS, scale=ACME_SCALE)),
            basis=money(Decimal(str(TENDER_UNITS)) * 10),
        )
    )
    world.declare_tender_policy(
        _TenderPolicy(owner_agent_id=ALICE, proceeds_account_id=CHECKING, liquid_net_worth_floor=money(500_000))
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
        jurisdictions=(PreparedJurisdiction(jurisdiction_id=FEDERAL, level=jurisdictions[FEDERAL].level),),
    )
    for agent_id, balance in ((ALICE, Decimal(1_000_000)), ("seller", 0), ("bank", 0), ("irs", 0)):
        world.declare_account(account(agent_id, balance))
    world.track(
        TaxAuthority(
            compile_profile(
                TaxProfile(agent_id=ALICE, jurisdiction_ids=[FEDERAL], tax_authority_agent_id="irs"),
                jurisdictions,
                quantum=QUANTUM,
            )
        )
    )
    world.declare_housing(
        Housing(
            purchases=(
                _PropertyPurchase(
                    month=0,
                    cause_id="buy-house",
                    property_id="house",
                    location_id=PROPERTY_LOCATION_ID,
                    buyer_agent_id=ALICE,
                    buyer_account_id=CHECKING,
                    seller_agent_id="seller",
                    seller_account_id=CHECKING,
                    purchase_price=money(500_000),
                    down_payment=money(100_000),
                    buyer_closing_cost=0,
                    rented_fraction_ppb=0,
                    land_value_fraction_ppb=rate_to_ppb(0.2),
                    mortgage=_MortgageFinancing(
                        liability_id="house-mortgage",
                        lender_agent_id="bank",
                        lender_account_id=CHECKING,
                        principal=money(400_000),
                        annual_interest_rate_ppb=rate_to_ppb(0.06),
                        term_months=360,
                    ),
                ),
            ),
            sales=(_PropertySale(month=PROPERTY_SALE_MONTH, property_id="house", closing_cost_ppb=rate_to_ppb(0.06)),),
            capital_improvements=(
                _CapitalImprovement(month=CAPEX_MONTH, property_id="house", amount=money(30_000), description="roof"),
            ),
        ),
        (),
        (PROPERTY_LOCATION,),
    )
    return world


def declared(world: World) -> frozenset[AccountRef]:
    return frozenset(world.accounting.declared)


def test_a_scheduled_sale_brings_in_exactly_its_proceeds() -> None:
    world = scheduled_sale_world()
    accounts = declared(world)
    rollout = run(
        world,
        scheduled_sales=(
            _ScheduledSale(
                month=SALE_MONTH,
                cause_id="sell-vti",
                agent_id=ALICE,
                account_id=CHECKING,
                asset_id=str(VTI.symbol),
                units=int(quantity_to_quanta(SALE_UNITS, scale=VTI_SCALE)),
                proceeds_account_id=CHECKING,
            ),
        ),
    )

    assert proceeds(rollout, month=SALE_MONTH) == SALE_PROCEEDS_QUANTA
    assert crossed_the_boundary(rollout, accounts, month=SALE_MONTH) == SALE_PROCEEDS_QUANTA


def test_a_target_allocation_sale_brings_in_exactly_its_proceeds() -> None:
    """The rent it was raised for is between two modeled agents, so it cancels and only
    the sale is left."""

    world = target_allocation_world()
    accounts = declared(world)
    rollout = run(world, policies=(VTI_BAND,))
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
