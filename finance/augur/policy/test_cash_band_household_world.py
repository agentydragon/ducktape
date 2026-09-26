"""The household against a world: what `check` refuses before it is tracked, and purchases sized
from the cash its own batch leaves.

The guards are on the household's knobs against the world it would act on, not on financial
execution; the financial behaviour of the same households lives in
<../sim/allocation_household_test.py> and <../sim/target_allocation_test.py>.
"""

from dataclasses import dataclass
from typing import Any

import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.policy.cash_band_household import (
    BandBound,
    CashBandHousehold,
    CpiIndexed,
    ManagedSleeve,
    Reinvest,
    SecuritySleeve,
    Sleeve,
)
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Book, SecurityLotState
from finance.augur.sim.capture import FinancialCapture, FinancialOutput
from finance.augur.sim.ids import AccountId, AgentId, AssetId, LotId, PortfolioId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedLot,
    PreparedObligation,
    PreparedSeries,
    PreparedTlhPortfolio,
)
from finance.augur.sim.tlh import TlhAssumptions, TlhOpeningCohort
from finance.augur.sim.world import World

ALICE = AgentId("alice")
CREDITOR = AgentId("creditor")
CASH = AccountId("cash-account")
HOLDINGS = AccountId("holdings-account")
# No modeled harvest: the managed sleeve's value moves only with contributions here.
QUIET = TlhAssumptions(
    peak_annual_yield=0, floor_annual_yield=0, maturity_decay_exponent=1, drawdown_sensitivity=0, short_term_fraction=1
)
GUARDED = AgentId("test-alice")
STOCK = AssetId("test-stock")
CHECKING = AccountId("checking")
BROKERAGE = AccountId("brokerage")
SCALE = 1_000_000
HORIZON = 13
INDEX = CpiIndexed(base_amount=0, adjustment_period_months=12)
OPENING_LOT = LotId("opening-stock")
STOCK_SLEEVE = SecuritySleeve(asset_id=STOCK, weight=1)
REINVEST = Reinvest(rebalance_tolerance_ppb=None)


@dataclass
class Situation:
    """One household's books, the household and the claims raised on it."""

    prices: dict[str, int]
    household: CashBandHousehold
    opening_cash: int = 0
    lots: tuple[PreparedLot, ...] = ()
    portfolios: tuple[PreparedTlhPortfolio, ...] = ()
    claims: tuple[PreparedObligation, ...] = ()
    horizon_months: int = 1


def reinvesting(*sleeves: Sleeve, ceiling: int, tolerance: int | None) -> CashBandHousehold:
    return CashBandHousehold(
        ALICE,
        cash_account_id=CASH,
        floor=0,
        ceiling=ceiling,
        sleeves=sleeves,
        source_account_ids=(HOLDINGS,),
        reinvest=Reinvest(rebalance_tolerance_ppb=tolerance),
        cause_id_prefix="fund",
    )


def lot(lot_id: LotId, asset_id: AssetId, *, units: int, basis: int) -> PreparedLot:
    return PreparedLot(
        lot_id=lot_id,
        agent_id=ALICE,
        account_id=HOLDINGS,
        asset_id=asset_id,
        purchase_month=-24,
        quantity_scale=1,
        units=units,
        basis=basis,
    )


def claim(amount: int) -> PreparedObligation:
    return PreparedObligation(
        month=0,
        obligation_id="upkeep",
        obligation_type="cash_spend",
        from_account=AccountRef(agent_id=ALICE, account_id=CASH),
        to_account=AccountRef(agent_id=CREDITOR, account_id=CASH),
        amount_due=amount,
        property_id=None,
        deduction_category=None,
        deductible_fraction_ppb=1_000_000_000,
    )


def run(case: Situation) -> FinancialOutput:
    snapshots = case.horizon_months + 1
    world = World(
        MarketPath(
            [
                PreparedSeries(series_id=f"security:{asset_id}", snapshots=snapshots, values=(price,) * snapshots)
                for asset_id, price in case.prices.items()
            ],
            0,
            rollout_count=1,
        ),
        horizon_months=case.horizon_months,
    )
    for agent_id, account_id, opening in ((ALICE, CASH, case.opening_cash), (ALICE, HOLDINGS, 0), (CREDITOR, CASH, 0)):
        world.declare_account(
            PreparedAccount(account=AccountRef(agent_id=agent_id, account_id=account_id), opening_balance=opening)
        )
    # Every security sleeve gets a pool: the household reads its quotes off the positions it observes.
    for target in case.household.sleeves:
        if isinstance(target, SecuritySleeve):
            world.declare_pool(
                PreparedHoldingPool(agent_id=ALICE, account_id=HOLDINGS, asset_id=target.asset_id, quantity_scale=1)
            )
    for holding in case.lots:
        world.hold(holding)
    for spec in case.portfolios:
        world.declare_portfolio(spec)
    for obligation in case.claims:
        world.track(Biller(obligation))
    case.household.check(world)
    world.track(case.household)
    recorder = FinancialCapture(world, capture="forensic")
    world.start()
    while not world.finished:
        world.step()
        recorder.record()
    output = recorder.financial()
    assert output.failed_month is None
    return output


def balance(book: Book, agent_id: AgentId, account_id: AccountId = CASH) -> int:
    return one(
        row.balance for row in book.balances if (row.account.agent_id, row.account.account_id) == (agent_id, account_id)
    )


def holding(book: Book, lot_id: LotId) -> SecurityLotState:
    return one(row for row in book.lots if row.lot_id == lot_id)


def test_a_purchase_is_sized_to_what_the_months_claim_payment_leaves() -> None:
    """A quiet drift month: the trim raises less than the buy side wants, and the claim takes 50 more.

    The sleeves are $500 of `coarse` (5 units at $100) against $102 of `fine` (102 units at $1)
    on equal weights, so the rebalance moves $199 each way. The trim can only give up whole
    $100 units, so it raises $100, while $199 buys 199 units of `fine`. Against $50 of opening
    cash and a $50 claim the account holds $100 when the purchase settles, so the exact order
    is 100 units — and an order sized from the pre-payment cash would be rejected and stop the
    path instead.
    """
    output = run(
        Situation(
            prices={"coarse": 100, "fine": 1},
            household=reinvesting(
                SecuritySleeve(asset_id=AssetId("coarse"), weight=1),
                SecuritySleeve(asset_id=AssetId("fine"), weight=1),
                ceiling=1_000,
                tolerance=0,
            ),
            opening_cash=50,
            lots=(
                lot(LotId("opening-coarse"), AssetId("coarse"), units=5, basis=500),
                lot(LotId("opening-fine"), AssetId("fine"), units=102, basis=102),
            ),
            claims=(claim(50),),
        )
    )
    closed = output.months[1]
    bought = holding(closed, LotId("fund_buy_s1_0"))
    assert (bought.units_remaining, bought.basis_remaining, bought.purchase_month) == (100, 100, 0)
    assert holding(closed, LotId("opening-coarse")).units_remaining == 4
    assert (balance(closed, ALICE), balance(closed, CREDITOR)) == (0, 50)
    # Exactness is the point: nothing this batch requested was trimmed or refused.
    assert [disposition.units for disposition in output.dispositions] == [1]


def managed_portfolio(opening: TlhOpeningCohort) -> PreparedTlhPortfolio:
    return PreparedTlhPortfolio(
        portfolio_id=PortfolioId("managed"),
        owner_agent_id=ALICE,
        account_id=HOLDINGS,
        asset_id=AssetId("index"),
        initial_cohorts=(opening,),
        assumptions=QUIET,
    )


def test_a_projected_purchase_into_a_managed_sleeve_contributes_what_is_left() -> None:
    """The sleeve names a TLH portfolio, so the order is an opaque contribution.

    $1,000 of cash against a $400 claim and a zero band invests $600, all of it: at a price of 7
    no whole number of units is worth $600, and none is needed. No household-visible lot is created,
    and no pool quotes the index: the sleeve has no unit price to read.
    """
    output = run(
        Situation(
            prices={"index": 7},
            household=reinvesting(
                ManagedSleeve(portfolio_id=PortfolioId("managed"), weight=1), ceiling=0, tolerance=None
            ),
            opening_cash=1_000,
            portfolios=(managed_portfolio(TlhOpeningCohort(value=100, cost_basis=100, purchase_month_index=-24)),),
            claims=(claim(400),),
        )
    )
    closed = output.months[1]
    assert closed.tlh_portfolios is not None
    assert one(closed.tlh_portfolios).value == 700  # The $100 opening mark plus the $600 contribution.
    assert closed.lots == []
    assert (balance(closed, ALICE), balance(closed, CREDITOR)) == (0, 400)


def test_a_portfolio_at_a_zero_index_mark_is_not_offered_the_surplus() -> None:
    """At a zero mark the portfolio refuses money, which the world would answer by stopping the path.

    Its written-off cohort shows value 0 like an empty portfolio would, so only the statement's
    flag keeps the household from contributing the $600 surplus; the path runs to the end with
    the cash left idle.
    """
    output = run(
        Situation(
            prices={"index": 0},
            household=reinvesting(
                ManagedSleeve(portfolio_id=PortfolioId("managed"), weight=1), ceiling=0, tolerance=None
            ),
            opening_cash=1_000,
            portfolios=(managed_portfolio(TlhOpeningCohort(value=0, cost_basis=100, purchase_month_index=-24)),),
            claims=(claim(400),),
            horizon_months=2,
        )
    )
    closed = output.months[-1]
    assert closed.tlh_portfolios is not None
    assert one(closed.tlh_portfolios).value == 0
    assert (balance(closed, ALICE), balance(closed, CREDITOR)) == (600, 400)
    assert output.tlh_financial_effects is not None
    assert [effect.operation for effect in output.tlh_financial_effects] == ["modeled_realization"] * 2


def stock_world(
    *, lot_id: LotId = OPENING_LOT, inflation: tuple[int, ...] | None = None, second_grid: int | None = None
) -> World:
    """Alice's cash and 100 shares held in brokerage, on a flat price path and optionally a CPI path.

    With `second_grid`, a second brokerage account declares a pool of the same stock on that grid.
    """
    series = [PreparedSeries(series_id=f"security:{STOCK}", snapshots=HORIZON + 1, values=(1_000,) * (HORIZON + 1))]
    if inflation is not None:
        series.append(PreparedSeries(series_id="inflation", snapshots=HORIZON + 1, values=inflation))
    world = World(MarketPath(series, 0, rollout_count=1), horizon_months=HORIZON)
    world.declare_account(
        PreparedAccount(account=AccountRef(agent_id=GUARDED, account_id=CHECKING), opening_balance=10_000)
    )
    world.declare_pool(
        PreparedHoldingPool(agent_id=GUARDED, account_id=BROKERAGE, asset_id=STOCK, quantity_scale=SCALE)
    )
    if second_grid is not None:
        world.declare_pool(
            PreparedHoldingPool(
                agent_id=GUARDED, account_id=AccountId("brokerage-2"), asset_id=STOCK, quantity_scale=second_grid
            )
        )
    world.hold(
        PreparedLot(
            lot_id=lot_id,
            agent_id=GUARDED,
            account_id=BROKERAGE,
            asset_id=STOCK,
            purchase_month=-24,
            quantity_scale=SCALE,
            units=100 * SCALE,
            basis=50_000,
        )
    )
    return world


def check(
    world: World,
    *,
    cash_account_id: AccountId = CHECKING,
    floor: BandBound = 0,
    ceiling: BandBound = 0,
    sleeves: tuple[Sleeve, ...] = (STOCK_SLEEVE,),
    source_account_ids: tuple[AccountId, ...] = (BROKERAGE,),
    reinvest: Reinvest | None = REINVEST,
    cause_id_prefix: str = "fund",
) -> None:
    """Build a household differing from a valid reinvesting one in the knobs given, and check it."""
    CashBandHousehold(
        GUARDED,
        cash_account_id=cash_account_id,
        floor=floor,
        ceiling=ceiling,
        sleeves=sleeves,
        source_account_ids=source_account_ids,
        reinvest=reinvest,
        cause_id_prefix=cause_id_prefix,
    ).check(world)


def test_generated_purchase_namespace_is_reserved() -> None:
    reserved = stock_world(lot_id=LotId("fund_buy_s0_1000000"))
    with pytest.raises(ValueError, match="reserved purchase identity"):
        check(reserved)
    check(reserved, reinvest=None)
    check(stock_world(lot_id=LotId("fund_buy_s0_1000000x")))


@pytest.mark.parametrize(
    ("knobs", "error"),
    [
        ({"source_account_ids": (BROKERAGE, BROKERAGE)}, "source accounts must be unique"),
        ({"source_account_ids": (AccountId("undeclared"),)}, "purchase pool is not declared"),
        ({"cash_account_id": AccountId("undeclared")}, "declared funding account"),
        ({"cause_id_prefix": " "}, "nonempty cause"),
        ({"sleeves": (STOCK_SLEEVE,) * 2}, "duplicate funding sleeve"),
        ({"sleeves": ()}, "nonempty values"),
        ({"sleeves": (SecuritySleeve(asset_id=STOCK, weight=0),)}, "positive target"),
        ({"sleeves": (SecuritySleeve(asset_id=STOCK, weight=-1),)}, "nonnegative"),
        ({"floor": 1}, "must not exceed"),
        ({"floor": -1}, "nonnegative"),
        (
            {
                "floor": CpiIndexed(base_amount=5, adjustment_period_months=1),
                "ceiling": CpiIndexed(base_amount=4, adjustment_period_months=1),
            },
            "must not exceed",
        ),
        ({"ceiling": CpiIndexed(base_amount=0, adjustment_period_months=0)}, "invalid reset period"),
        ({"ceiling": INDEX}, "missing series"),
    ],
    ids=[
        "sources",
        "purchase_pool",
        "funding",
        "cause",
        "duplicate_sleeve",
        "no_sleeves",
        "zero_weights",
        "negative_weight",
        "band",
        "negative_floor",
        "indexed_band",
        "period",
        "missing_index",
    ],
)
def test_a_malformed_household_is_refused_before_it_is_tracked(knobs: dict[str, Any], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        check(stock_world(), **knobs)


def test_source_pools_on_different_grids_are_refused() -> None:
    with pytest.raises(ValueError, match="disagree on the quantity grid"):
        check(stock_world(second_grid=10), source_account_ids=(BROKERAGE, AccountId("brokerage-2")))


def test_an_indexed_bound_needs_a_positive_level_at_every_reset_it_reads() -> None:
    with pytest.raises(ValueError, match="positive index levels"):
        check(stock_world(inflation=(10**9,) * 12 + (0, 0)), ceiling=INDEX)


def test_exact_integer_indices_and_a_sales_only_scope_are_valid() -> None:
    # An exact i64 index above 2**53 is valid; absent purchase destinations remain valid for sales-only rules.
    check(
        stock_world(inflation=(2**53 + 1,) * (HORIZON + 1)),
        reinvest=None,
        source_account_ids=(AccountId("unused-holdings"),),
        ceiling=INDEX,
    )


if __name__ == "__main__":
    pytest_bazel.main()
