"""The configured allocation household against canonical settlement and recorded books.

The shared proposal arithmetic has unit controls in `policy/configured_allocation_test.py`.
These flat-price worlds exercise the household's whole monthly batch: pre-claim sales, the
all-or-none claim payments, and the purchases sized from what both leave.
"""

from dataclasses import dataclass, replace
from decimal import Decimal

import numpy as np
import pytest_bazel
from more_itertools import one

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.policy.configured_household import ConfiguredHousehold
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Book
from finance.augur.sim.capture import FinancialCapture, FinancialOutput
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.holdings import Disposition
from finance.augur.sim.ids import AgentId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedLot,
    PreparedRecurringObligation,
    PreparedRecurringTransfer,
    _AllocationPolicy,
    _SleeveTarget,
)
from finance.augur.sim.world import World

VTI = SecurityKey(symbol=SecuritySymbol("vti"))
BND = SecurityKey(symbol=SecuritySymbol("bnd"))
QUANTUM = Decimal("0.01")
HORIZON = 4
PRICE = Decimal(100)
ALICE = "alice"
LANDLORD = "landlord"
CHECKING = "checking"
# Weights default equal against a 9:1 holding, so stock is the overweight sleeve and every
# raise has to come out of it first.
STOCK_UNITS, BOND_UNITS = 900.0, 100.0
QUANTA_PER_UNIT = 100
FULL_DRIFT = 250_000_000  # A 25% drift band, in parts per billion.


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def ref(agent_id: str) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=CHECKING)


@dataclass(frozen=True)
class Situation:
    """One agent holding two sleeves against an (s,S) cash band.

    `rent` is an outflow the band must fund and `income` an inflow it must invest. The rent is
    a claim on alice, so it is in the month's projection; the income is a transfer landing when
    its month opens, so the band sees it in that month's decision.
    """

    opening_cash: Decimal | int
    floor: Decimal | int
    ceiling: Decimal | int
    stock_units: float = STOCK_UNITS
    bond_units: float = BOND_UNITS
    rent: Decimal | int = 0
    income: Decimal | int = 0
    rent_months: tuple[int, int | None] = (1, None)
    income_months: tuple[int, int | None] = (1, None)
    allow_purchases: bool = False
    tolerance_ppb: int | None = None
    weights: tuple[int, int] = (1, 1)


def lot(lot_id: str, asset: SecurityKey, quantity: float) -> PreparedLot:
    scale = quantity_scale_for_asset(asset)
    return PreparedLot(
        lot_id=lot_id,
        agent_id=ALICE,
        account_id=CHECKING,
        asset_id=str(asset.symbol),
        purchase_month=0,
        quantity_scale=scale,
        units=int(quantity_to_quanta(quantity, scale=scale)),
        basis=money(Decimal(str(quantity)) * PRICE),
    )


def compose(case: Situation) -> World:
    paths = ExternalSeriesContext.from_level_blocks(
        [(asset, np.full((1, HORIZON + 1), float(PRICE))) for asset in (VTI, BND)],
        rollout_count=1,
        horizon_months=HORIZON,
    )
    world = World(
        MarketPath(
            compile_series(paths, rollout_count=1, horizon_months=HORIZON, currency_quantum=QUANTUM), 0, rollout_count=1
        ),
        horizon_months=HORIZON,
    )
    world.declare_account(PreparedAccount(account=ref(ALICE), opening_balance=money(case.opening_cash)))
    # Funded only for what it owes, so an unfunded counterparty can never fail a rollout.
    world.declare_account(
        PreparedAccount(account=ref(LANDLORD), opening_balance=money(Decimal(case.income) * (HORIZON + 1)))
    )
    lots = (lot("stock", VTI, case.stock_units), lot("bond", BND, case.bond_units))
    for holding in lots:
        world.declare_pool(
            PreparedHoldingPool(
                agent_id=ALICE, account_id=CHECKING, asset_id=holding.asset_id, quantity_scale=holding.quantity_scale
            )
        )
        world.hold(holding)
    if case.rent:
        start, end = case.rent_months
        world.track(
            Biller(
                PreparedRecurringObligation(
                    start_month=start,
                    end_month=end,
                    obligation_id="rent",
                    obligation_type="rent",
                    from_account=ref(ALICE),
                    to_account=ref(LANDLORD),
                    amount_due=money(case.rent),
                    property_id=None,
                    deduction_category=None,
                    deductible_fraction_ppb=1_000_000_000,
                )
            )
        )
    if case.income:
        start, end = case.income_months
        world.recurring_transfers = (
            PreparedRecurringTransfer(
                start_month=start,
                end_month=end,
                cause_id="income",
                from_account=ref(LANDLORD),
                to_account=ref(ALICE),
                amount=money(case.income),
                income_category=None,
                deduction_category=None,
            ),
        )
    world.track(
        ConfiguredHousehold(
            AgentId(ALICE),
            (
                _AllocationPolicy(
                    agent_id=ALICE,
                    account_id=CHECKING,
                    source_account_ids=(),
                    sleeves=tuple(
                        _SleeveTarget(
                            asset_id=str(asset.symbol), weight=weight, quantity_scale=quantity_scale_for_asset(asset)
                        )
                        for asset, weight in zip((VTI, BND), case.weights, strict=True)
                    ),
                    cash_floor=money(case.floor),
                    cash_ceiling=money(case.ceiling),
                    cause_id_prefix="allocation_sale",
                    allow_purchases=case.allow_purchases,
                    rebalance_tolerance_ppb=case.tolerance_ppb,
                ),
            ),
        )
    )
    return world


def run(case: Situation) -> FinancialOutput:
    world = compose(case)
    recorder = FinancialCapture(world, capture="forensic")
    world.start()
    while not world.finished:
        world.step()
        recorder.record()
    output = recorder.financial()
    assert output is not None
    return output


def units(output: FinancialOutput, *, month: int) -> dict[str, float]:
    """Every lot's remaining units as of one snapshot, by lot id."""
    return {row.lot_id: row.units_remaining / row.quantity_scale for row in book(output, month).lots}


def book(output: FinancialOutput, month: int) -> Book:
    return one(row for row in output.months if row.month == month)


def alice_cash(output: FinancialOutput) -> list[int]:
    return [one(row.balance for row in snapshot.balances if row.account == ref(ALICE)) for snapshot in output.months]


def sales(output: FinancialOutput) -> list[Disposition]:
    return [row for row in output.dispositions if row.agent_id == ALICE]


def test_a_zero_target_sleeve_is_exited_whole_and_its_proceeds_reinvested() -> None:
    output = run(
        Situation(opening_cash=0, floor=0, ceiling=0, weights=(0, 1), allow_purchases=True, tolerance_ppb=FULL_DRIFT)
    )
    assert units(output, month=1) == {"stock": 0.0, "bond": BOND_UNITS, "allocation_sale_buy_p0_s1_0": STOCK_UNITS}
    assert alice_cash(output)[-1] == 0
    [sale] = sales(output)
    assert sale.units / sale.quantity_scale == STOCK_UNITS
    assert sale.basis == 9_000_000


def test_a_month_inside_the_band_sells_nothing() -> None:
    """Drift alone never triggers a trade. The portfolio is 9:1 against a 1:1 target — as
    far from target as this scenario gets — and the policy still does nothing while cash
    sits inside the band. Rebalancing rides cashflow only."""

    output = run(Situation(opening_cash=50_000, floor=10_000, ceiling=90_000))

    assert units(output, month=HORIZON) == {"stock": STOCK_UNITS, "bond": BOND_UNITS}
    assert alice_cash(output)[-1] == 5_000_000


def test_landing_exactly_on_a_bound_is_inside_the_band() -> None:
    """The band is closed at both ends. Cash exactly at the floor has not crossed it, so
    nothing is sold — an off-by-one here would trade every month the balance came to rest
    on its trigger, which is the balance a refill leaves it at.
    """

    output = run(Situation(opening_cash=10_000, floor=10_000, ceiling=90_000))

    assert units(output, month=HORIZON) == {"stock": STOCK_UNITS, "bond": BOND_UNITS}
    assert alice_cash(output)[-1] == 1_000_000


def test_crossing_the_floor_refills_to_the_ceiling() -> None:
    """(s,S), through the engine. Cash below the floor is raised to the CEILING, not back
    to the floor — refilling to the floor would put the agent back at its trigger next
    month, making it a forced seller into every dip.

    $5,000 with a $10,000 floor and a $40,000 ceiling raises $35,000, which at $100/unit is
    350 units out of the overweight stock sleeve.
    """

    output = run(Situation(opening_cash=5_000, floor=10_000, ceiling=40_000))

    assert alice_cash(output)[1] == 4_000_000
    assert units(output, month=1) == {"stock": 550.0, "bond": BOND_UNITS}


def test_the_raise_comes_out_of_the_overweight_sleeve() -> None:
    """Water-filling, observed end to end. Stock is worth $90,000 and bonds $10,000 against
    equal weights, so the first $80,000 of any raise comes entirely from stock — the level
    where the two sleeves meet. The bond sleeve is untouched, which is what "don't sell the
    underweight sleeve" means when it is not a slogan."""

    output = run(Situation(opening_cash=0, floor=1_000, ceiling=30_000))

    assert units(output, month=1) == {"stock": 600.0, "bond": BOND_UNITS}


def test_the_band_is_measured_after_the_months_obligations() -> None:
    """The decision is made against the balance the month will END at, not the balance
    sitting there before the bills — which is what lets funding happen once a month like a
    person.

    Month 0 has no rent and $12,000 sits inside the band, so nothing happens. Month 1
    brings $5,000 of rent: a policy reading the CURRENT balance sees $12,000, above the
    $10,000 floor, and sells nothing — leaving $7,000 after the rent, below the floor it
    was supposed to hold. Reading the PROJECTED balance sees $7,000 and raises to the
    $30,000 ceiling, so $23,000 is sold (230 units of the overweight stock) and the rent
    settles out of the refilled account.

    Asserted exactly rather than as "sold something": the wrong reading also sells on later
    months, so an inequality would pass against the defect this test exists to catch.
    """

    output = run(Situation(opening_cash=12_000, floor=10_000, ceiling=30_000, rent=5_000))

    # Index N is the state ENTERING month N, so month 1's sale shows at index 2.
    assert units(output, month=1) == {"stock": STOCK_UNITS, "bond": BOND_UNITS}
    assert units(output, month=2) == {"stock": 670.0, "bond": BOND_UNITS}
    assert alice_cash(output)[2] == 3_000_000
    assert output.failed_month is None


def test_a_raise_the_sleeves_cannot_cover_drains_them_and_stops() -> None:
    """Asking for more than the portfolio holds sells all of it and does not go negative."""

    output = run(Situation(opening_cash=0, floor=1_000, ceiling=10_000_000))

    assert units(output, month=1) == {"stock": 0.0, "bond": 0.0}
    assert alice_cash(output)[1] == (STOCK_UNITS + BOND_UNITS) * int(PRICE) * QUANTA_PER_UNIT


def test_a_sale_shows_up_as_a_lot_disposition() -> None:
    """A sale the ledger records but the disposition frame does not is a sale nobody can
    audit: cash and lots move, and the row explaining WHY is missing. This asserts the row
    exists, is attributed to the selling agent and the sleeve's asset, and reconciles
    against the lots the run actually gave up.
    """

    rows = sales(run(Situation(opening_cash=5_000, floor=10_000, ceiling=40_000)))

    assert [row.lot_id for row in rows] == ["stock"]
    assert rows[0].asset_id == "security:vti"
    assert rows[0].units / rows[0].quantity_scale == 350.0
    assert rows[0].proceeds == 3_500_000
    assert rows[0].basis == 3_500_000
    # The bond sleeve was never touched, so it must not appear at all — an over-broad
    # decode would emit a zero-unit row for it, and the equality above refuses that.


def test_enabling_purchases_does_not_create_lots_without_a_buy() -> None:
    """Enabling the buy side must not change a cash raise or create empty future holdings."""

    case = Situation(opening_cash=5_000, floor=10_000, ceiling=40_000)
    without = run(case)
    enabled = run(replace(case, allow_purchases=True))

    assert units(enabled, month=1) == units(without, month=1)
    assert alice_cash(enabled) == alice_cash(without)


def test_surplus_above_the_ceiling_is_invested_into_the_underweight_sleeve() -> None:
    """The buy side, end to end. $100,000 against a $10,000 floor and a $20,000 ceiling
    invests $90,000 — down to the FLOOR, not to the ceiling, for the same (s,S) reason a
    raise goes to the far edge.

    Where it goes is water-filling in reverse: stock is worth $90,000 and bonds $10,000
    against equal weights, so the deposit levels them at $95,000 each — $85,000 into bonds
    and $5,000 into stock. That is the asymmetry with the sale side made visible: a raise
    came entirely out of stock, and the deposit goes overwhelmingly the other way.
    """

    held = units(run(Situation(opening_cash=100_000, floor=10_000, ceiling=20_000, allow_purchases=True)), month=1)

    assert held["allocation_sale_buy_p0_s0_0"] == 50.0
    assert held["allocation_sale_buy_p0_s1_0"] == 850.0
    # The holdings it started with are untouched: this month bought, it did not rebalance.
    assert held["stock"] == STOCK_UNITS
    assert held["bond"] == BOND_UNITS


def test_a_purchase_leaves_exactly_the_floor() -> None:
    """A quantum of overshoot would show as the floor minus the overshoot, which is the
    band spending money it promised to keep."""

    output = run(Situation(opening_cash=100_000, floor=10_000, ceiling=20_000, allow_purchases=True))

    assert alice_cash(output)[1] == 1_000_000


def test_a_purchase_records_the_price_its_rollout_paid() -> None:
    """Basis comes from whatever the rollout paid the month it crossed the band. Reading a static
    column would report 0, making the whole proceeds a gain on the eventual sale."""

    output = run(Situation(opening_cash=100_000, floor=10_000, ceiling=20_000, allow_purchases=True))
    bought = one(row for row in book(output, 1).lots if row.lot_id == "allocation_sale_buy_p0_s1_0")

    assert bought.basis_remaining == 85_000 * QUANTA_PER_UNIT


def test_successive_purchases_create_separate_lots() -> None:
    """Repeated purchases need separate acquisition dates and bases, without a configured count.

    The income lands as each month opens and the band invests it in that month's decision, so
    the policy buys in months 2 and 3. Both go to bonds: at $10,000 against stock's $90,000,
    the bond sleeve is still underweight after both deposits.
    """

    output = run(
        Situation(opening_cash=0, floor=0, ceiling=1_000, income=30_000, income_months=(2, None), allow_purchases=True)
    )
    rows = sorted(
        (row for row in book(output, HORIZON).lots if row.lot_id.startswith("allocation_sale_buy_p0_s1_")),
        key=lambda row: row.lot_id,
    )

    assert [row.units_remaining / row.quantity_scale for row in rows] == [300.0, 300.0]
    assert [row.purchase_month for row in rows] == [2, 3]


def test_a_runtime_purchase_keeps_its_month_when_later_sold() -> None:
    """A disposition retains the lot's acquisition date, not the world's start date."""

    # The month-0 holdings sell first, so the raise must reach the subsequent purchase.
    output = run(
        Situation(
            opening_cash=0,
            floor=0,
            ceiling=1_000,
            stock_units=1.0,
            bond_units=1.0,
            income=30_000,
            income_months=(2, 2),
            rent=10_000,
            rent_months=(3, 3),
            allow_purchases=True,
        )
    )
    rows = [row for row in sales(output) if row.lot_id.startswith("allocation_sale_buy_")]

    assert rows
    assert {row.purchase_month for row in rows} == {2}


def test_sales_only_keeps_surplus_cash_without_buying() -> None:
    output = run(Situation(opening_cash=0, floor=0, ceiling=1_000, income=30_000, allow_purchases=False))

    assert units(output, month=HORIZON) == {"stock": STOCK_UNITS, "bond": BOND_UNITS}
    assert alice_cash(output)[-1] == 9_000_000


def test_a_drifted_portfolio_is_rebalanced_in_a_quiet_month() -> None:
    """The mechanism neither side of the band can express. Cash sits at $50,000 inside a
    [$10,000, $90,000] band, so nothing is being funded and nothing is being invested — and
    yet the portfolio is 9:1 against a 1:1 target.

    Without a tolerance this is exactly `test_a_month_inside_the_band_sells_nothing`. With
    one, $40,000 crosses: 400 units of stock sold and 400 units of bonds bought, landing
    both sleeves on $50,000. The sale and the purchase are two independent legs of one
    batch — the sell is stated before the buy and the buy is sized from what it leaves — so
    this also pins that they meet in the same month.
    """

    output = run(
        Situation(opening_cash=50_000, floor=10_000, ceiling=90_000, allow_purchases=True, tolerance_ppb=FULL_DRIFT)
    )
    held = units(output, month=1)

    assert held["stock"] == 500.0
    assert held["allocation_sale_buy_p0_s1_0"] == 400.0
    # Untouched: the bond sleeve was the underweight one, so the trim never reaches it.
    assert held["bond"] == BOND_UNITS
    assert "allocation_sale_buy_p0_s0_0" not in held
    # Cash-neutral to the cent. A rebalance is a portfolio operation, not a funding one.
    assert alice_cash(output)[1] == 5_000_000


def test_a_rebalanced_portfolio_then_sits_still() -> None:
    """One trigger, not one per month. Once both sleeves are on target the drift is zero,
    so a flat price path produces exactly one rebalance over the horizon."""

    output = run(
        Situation(opening_cash=50_000, floor=10_000, ceiling=90_000, allow_purchases=True, tolerance_ppb=FULL_DRIFT)
    )

    assert [(row.lot_id, row.units / row.quantity_scale) for row in sales(output)] == [("stock", 400.0)]
    assert units(output, month=HORIZON) == units(output, month=1)


def test_a_tolerance_wider_than_the_drift_changes_nothing() -> None:
    """Configuring a rebalance is not asking for one. The fixture is 80% off target, so a
    100% tolerance leaves it exactly where an unconfigured policy would."""

    case = Situation(opening_cash=50_000, floor=10_000, ceiling=90_000, allow_purchases=True)
    with_tolerance = run(replace(case, tolerance_ppb=1_000_000_000))
    without = run(case)

    assert units(with_tolerance, month=HORIZON) == units(without, month=HORIZON)
    assert alice_cash(with_tolerance) == alice_cash(without)


if __name__ == "__main__":
    pytest_bazel.main()
