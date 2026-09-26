"""Hand-calculated Guyton-Klinger transitions, as pure rule arithmetic and through real settlement.

Settled paths open at 100 units of cash, 250 of bonds and 650 of equity, each priced 100 quanta
per whole unit: opening wealth 100_000, w0 = 5%, so year 0 withdraws 5000 from cash and leaves a
95_000 post-withdrawal book. Prices and CPI are per year and held within it. A path with bond
payouts or a prior-year tax also gets the taxed composition's accounts, paid each December or on
the tax authority's schedule; its jurisdictions tax nothing here, since no year's income reaches
a standard deduction.
"""

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from itertools import chain

import pytest
import pytest_bazel

from finance.augur.sim.actions import Action, Buy, Consume, LotSale, PayClaim, Sell, Transfer
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.ids import AssetId, LotId
from finance.augur.sim.jurisdictions import JurisdictionLevel
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedDistribution,
    PreparedDistributionSlice,
    PreparedHoldingPool,
    PreparedJurisdiction,
    PreparedLot,
    PreparedSeries,
)
from finance.augur.sim.results import Executed, Finished, Receipt, Rejected, RejectedAction, Rollout
from finance.augur.sim.runtime import load_jurisdictions_for
from finance.augur.sim.scenario import ORDINARY_INCOME, InterestIncome, TaxProfile
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World
from finance.augur.study.guyton_klinger.panel import Sleeve
from finance.augur.study.guyton_klinger.paths import (
    BROKERAGE,
    CALIFORNIA,
    CHECKING,
    FEDERAL,
    INCOME,
    RETIREE,
    TAX_AUTHORITY,
    TAX_RESERVE,
    WORLD,
)
from finance.augur.study.guyton_klinger.policy import (
    Cell,
    Guardrail,
    Inflation,
    Policy,
    Spending,
    Stage,
    YearRecord,
    apply_rules,
)

OPENING_UNITS = {Sleeve.CASH: 100, Sleeve.BONDS: 250, Sleeve.EQUITY: 650}
FLAT = [100, 100, 100, 100]


def cell(years: int) -> Cell:
    return Cell(initial_rate=Fraction(1, 20), years=years)


@dataclass(frozen=True)
class Path:
    """Per-year levels, one more than the horizon's years."""

    cpi: list[int]
    equity: list[int]
    bonds: list[int]
    # Equity lots as (lot, units), oldest first.
    equity_lots: tuple[tuple[str, int], ...] = (("equity", 650),)
    # Quanta each bond unit pays every December.
    bond_payout: int = 0
    # The tax profile's estimate base: a quarter of it falls due in April, June and September.
    prior_year_tax: int = 0

    @property
    def taxed(self) -> bool:
        return bool(self.bond_payout or self.prior_year_tax)


def monthly(annual: list[int]) -> tuple[int, ...]:
    return tuple(annual[month // 12] for month in range(12 * (len(annual) - 1) + 1))


def world(paths: list[Path], rollout_id: int) -> World:
    years = len(paths[0].cpi) - 1
    levels = {
        "inflation": [path.cpi for path in paths],
        f"security:{Sleeve.CASH}": [FLAT[: years + 1] for _ in paths],
        f"security:{Sleeve.BONDS}": [path.bonds for path in paths],
        f"security:{Sleeve.EQUITY}": [path.equity for path in paths],
    }
    series = [
        PreparedSeries(
            series_id=series_id,
            snapshots=12 * years + 1,
            values=tuple(chain.from_iterable(monthly(row) for row in rows)),
        )
        for series_id, rows in levels.items()
    ]
    path = paths[rollout_id]
    if path.taxed:
        series.append(
            PreparedSeries(
                series_id=f"security_distribution:{Sleeve.BONDS}",
                snapshots=12 * years + 1,
                values=tuple(
                    path.bond_payout * 10**9 if month % 12 == 11 else 0
                    for path in paths
                    for month in range(12 * years + 1)
                ),
            )
        )
    result = World(
        MarketPath(series, rollout_id, rollout_count=len(paths)),
        horizon_months=12 * years,
        income_sources=(ORDINARY_INCOME, InterestIncome(issuer_jurisdiction_id=FEDERAL)) if path.taxed else (),
        jurisdictions=(
            PreparedJurisdiction(jurisdiction_id=FEDERAL, level=JurisdictionLevel.FEDERAL),
            PreparedJurisdiction(jurisdiction_id=CALIFORNIA, level=JurisdictionLevel.STATE),
        )
        if path.taxed
        else (),
    )
    accounts = [
        AccountRef(agent_id=RETIREE, account_id=CHECKING),
        AccountRef(agent_id=WORLD, account_id=CHECKING),
        *(
            AccountRef(agent_id=agent_id, account_id=account_id)
            for agent_id, account_id in (
                (RETIREE, TAX_RESERVE),
                *((RETIREE, account_id) for account_id in INCOME.values()),
                (TAX_AUTHORITY, CHECKING),
            )
            if path.taxed
        ),
    ]
    for account in accounts:
        result.declare_account(PreparedAccount(account=account, opening_balance=0))
    for sleeve in Sleeve:
        result.declare_pool(
            PreparedHoldingPool(agent_id=RETIREE, account_id=BROKERAGE, asset_id=AssetId(sleeve), quantity_scale=1)
        )
    if path.taxed:
        profile = TaxProfile(
            agent_id=RETIREE,
            jurisdiction_ids=[FEDERAL, CALIFORNIA],
            tax_authority_agent_id=TAX_AUTHORITY,
            payment_account_id=TAX_RESERVE,
            tax_authority_account_id=CHECKING,
            prior_year_tax=Decimal(path.prior_year_tax),
        )
        result.track(TaxAuthority(compile_profile(profile, load_jurisdictions_for([profile]), quantum=Decimal(1))))
        result.declare_distribution(
            PreparedDistribution(
                agent_id=RETIREE,
                holding_account_id=BROKERAGE,
                asset_id=AssetId(Sleeve.BONDS),
                to_account_id=INCOME[Sleeve.BONDS],
                tax_character=(
                    PreparedDistributionSlice(
                        fraction_ppb=10**9, income_category=InterestIncome(issuer_jurisdiction_id=FEDERAL)
                    ),
                ),
            )
        )
    lots = [
        (Sleeve.CASH, "cash", OPENING_UNITS[Sleeve.CASH]),
        (Sleeve.BONDS, "bonds", OPENING_UNITS[Sleeve.BONDS]),
        *((Sleeve.EQUITY, lot, units) for lot, units in path.equity_lots),
    ]
    for index, (sleeve, lot, units) in enumerate(lots):
        result.hold(
            PreparedLot(
                lot_id=LotId(lot),
                agent_id=RETIREE,
                account_id=BROKERAGE,
                asset_id=AssetId(sleeve),
                purchase_month=index - len(lots),
                quantity_scale=1,
                units=units,
                basis=100 * units,
            )
        )
    return result


def run(paths: list[Path], rollout_ids: list[int]) -> tuple[list[Rollout], Policy]:
    policy = Policy(cell(len(paths[0].cpi) - 1))
    session = ActionSession({id_: world(paths, id_) for id_ in rollout_ids}, RETIREE, capture="forensic")
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(policy(batch))
        return batch.rollouts, policy
    finally:
        session.close()


def settle(path: Path) -> tuple[Rollout, list[YearRecord]]:
    (rollout,), policy = run([path], [0])
    return rollout, policy.memory[0].records


def paid(rollout: Rollout) -> list[tuple[int, int]]:
    return [(row.month, row.receipt.amount_paid) for row in rollout.summary.payments]


def receipts(rollout: Rollout, month: int) -> list[Receipt]:
    assert rollout.trace is not None
    return [row for row in rollout.trace.receipts if row.month == month]


def actions(rollout: Rollout, month: int) -> list[Action]:
    return [row.action for row in receipts(rollout, month)]


def sold(rollout: Rollout, month: int) -> list[tuple[AssetId, tuple[LotSale, ...]]]:
    return [(action.asset_id, action.lots) for action in actions(rollout, month) if isinstance(action, Sell)]


def sale(lot: str, units: int) -> LotSale:
    return LotSale(account_id=BROKERAGE, lot_id=LotId(lot), units=units)


# Opening wealth 10_000 and w0 = 5%: the guardrails sit at withdrawals of exactly 600 and 400.
@pytest.mark.parametrize(
    ("basis", "guardrail", "withdrawal"),
    [
        (600, Guardrail.NONE, 600),
        (601, Guardrail.CUT, Fraction(5409, 10)),
        (400, Guardrail.NONE, 400),
        (399, Guardrail.RAISE, Fraction(4389, 10)),
    ],
)
def test_guardrails_are_strict(basis: int, guardrail: Guardrail, withdrawal: Fraction) -> None:
    spending = apply_rules(
        cell(30), year=1, basis=Fraction(basis), cpi_ratio=Fraction(1), lost=False, opening_wealth=10_000
    )
    assert (spending.guardrail, spending.withdrawal) == (guardrail, withdrawal)


@pytest.mark.parametrize(
    ("basis", "lost", "inflation", "withdrawal"),
    [
        # 25% inflation: 404 inflates to 505, a rate over w0.
        (404, True, Inflation.FROZEN, 404),
        (404, False, Inflation.APPLIED, 505),
        # 400 inflates to exactly w0 * V: equality does not freeze.
        (400, True, Inflation.APPLIED, 500),
    ],
)
def test_freeze_needs_a_loss_and_a_candidate_rate_over_w0(
    basis: int, lost: bool, inflation: Inflation, withdrawal: int
) -> None:
    spending = apply_rules(
        cell(30), year=1, basis=Fraction(basis), cpi_ratio=Fraction(5, 4), lost=lost, opening_wealth=10_000
    )
    assert spending == Spending(Fraction(basis * 5, 4), inflation, Guardrail.NONE, Fraction(withdrawal))


def test_deflation_lowers_the_withdrawal_even_after_a_loss() -> None:
    spending = apply_rules(
        cell(30), year=1, basis=Fraction(560), cpi_ratio=Fraction(9, 10), lost=True, opening_wealth=10_000
    )
    assert (spending.inflation, spending.withdrawal) == (Inflation.APPLIED, Fraction(504))


def test_guardrail_adjusts_the_inflated_candidate_once() -> None:
    # 560 * 1.1 = 616 > 600 is cut to 554.4; 1000 is 2 * w0 * V and still cut only once, to 900.
    inflated = apply_rules(
        cell(30), year=1, basis=Fraction(560), cpi_ratio=Fraction(11, 10), lost=False, opening_wealth=10_000
    )
    assert (inflated.guardrail, inflated.withdrawal) == (Guardrail.CUT, Fraction(2772, 5))
    rescue = apply_rules(
        cell(30), year=1, basis=Fraction(1000), cpi_ratio=Fraction(1), lost=False, opening_wealth=10_000
    )
    assert (rescue.guardrail, rescue.withdrawal) == (Guardrail.CUT, Fraction(900))
    prosperity = apply_rules(
        cell(30), year=1, basis=Fraction(100), cpi_ratio=Fraction(1), lost=False, opening_wealth=10_000
    )
    assert (prosperity.guardrail, prosperity.withdrawal) == (Guardrail.RAISE, Fraction(110))


# A 17-year horizon's final 15 years are year indices 2..16.
@pytest.mark.parametrize(
    ("year", "basis", "guardrail", "withdrawal"),
    [
        (1, 601, Guardrail.CUT, Fraction(5409, 10)),
        (2, 601, Guardrail.NONE, 601),
        (16, 399, Guardrail.RAISE, Fraction(4389, 10)),
    ],
)
def test_capital_preservation_stops_for_the_final_fifteen_years(
    year: int, basis: int, guardrail: Guardrail, withdrawal: Fraction
) -> None:
    spending = apply_rules(
        cell(17), year=year, basis=Fraction(basis), cpi_ratio=Fraction(1), lost=False, opening_wealth=10_000
    )
    assert (spending.guardrail, spending.withdrawal) == (guardrail, withdrawal)


def test_a_withdrawal_that_lowers_wealth_is_not_an_investment_loss() -> None:
    # Flat prices: year 1 opens at the 95_000 book, below year 0's 100_000 but a 0% return. 4% CPI
    # takes 5000 to 5200, a rate over w0 that a value-decline freeze would have withheld.
    rollout, records = settle(Path(cpi=[100, 104, 104], equity=FLAT[:3], bonds=FLAT[:3]))
    assert [(record.prior_book, record.spending.inflation) for record in records] == [
        (None, Inflation.INITIAL),
        (95_000, Inflation.APPLIED),
    ]
    assert paid(rollout) == [(0, 5000), (12, 5200)]
    # Cash runs out at 5000; the remaining 200 is 2 bond units.
    assert records[1].funding == ((Stage.CASH, 5000), (Stage.BONDS, 200))


def test_a_loss_freezes_the_increase_with_no_later_catch_up() -> None:
    # Equity 100 -> 99 opens year 1 at 94_350 < 95_000. 5200 / 94_350 exceeds w0: frozen at 5000.
    # Year 2 opens at its 89_350 book (0% return) and inflates 5000 by 4%, not by the missed 8.16%.
    rollout, records = settle(Path(cpi=[10_000, 10_400, 10_816, 10_816], equity=[100, 99, 99, 99], bonds=FLAT))
    assert [
        (record.prior_book, record.opening_wealth, record.spending.inflated, record.spending.inflation)
        for record in records
    ] == [
        (None, 100_000, Fraction(5000), Inflation.INITIAL),
        (95_000, 94_350, Fraction(5200), Inflation.FROZEN),
        (89_350, 89_350, Fraction(5200), Inflation.APPLIED),
    ]
    assert paid(rollout) == [(0, 5000), (12, 5000), (24, 5200)]
    # The losing equity sleeve stays untouched while cash and then bonds fund the withdrawals.
    assert sold(rollout, 24) == [(AssetId(Sleeve.BONDS), (sale("bonds", 52),))]


@pytest.mark.parametrize(
    ("equity", "bonds", "funding", "sold_12", "cash_bought"),
    [
        # 109_000 opens year 1: equity 78_000 is 7150 over its 70_850 target and rose. 42 units
        # (5040) fund 5000; the 2110 left of the excess sells as 18 units (2160) and buys 21 cash units.
        (
            [100, 120, 120],
            [100, 104, 104],
            ((Stage.OVERWEIGHT_EQUITY, 5040),),
            [(AssetId(Sleeve.EQUITY), (sale("equity", 42),)), (AssetId(Sleeve.EQUITY), (sale("equity", 18),))],
            21,
        ),
        # 86_750 opens year 1: equity 61_750 is over its 56_387.5 target but fell, so it is neither
        # a funding source before cash nor swept.
        ([100, 95, 95], [100, 80, 80], ((Stage.CASH, 5000),), [(AssetId(Sleeve.CASH), (sale("cash", 50),))], 0),
        # 93_500 opens year 1: bonds 30_000 are 6625 over 23_375 and rose; equity fell. 42 bond units
        # (5040) fund 5000; the 1585 left sells as 14 units (1680) and buys 16 cash units.
        (
            [100, 90, 90],
            [100, 120, 120],
            ((Stage.OVERWEIGHT_BONDS, 5040),),
            [(AssetId(Sleeve.BONDS), (sale("bonds", 42),)), (AssetId(Sleeve.BONDS), (sale("bonds", 14),))],
            16,
        ),
    ],
)
def test_only_rising_overweight_sleeves_fund_first_and_sweep_into_cash(
    equity: list[int],
    bonds: list[int],
    funding: tuple[tuple[Stage, int], ...],
    sold_12: list[tuple[AssetId, tuple[LotSale, ...]]],
    cash_bought: int,
) -> None:
    rollout, records = settle(Path(cpi=[100, 100, 100], equity=equity, bonds=bonds))
    assert rollout.stop is None
    assert records[1].funding == funding
    assert sold(rollout, 12) == sold_12
    assert [action.units for action in actions(rollout, 12) if isinstance(action, Buy)] == (
        [cash_bought] if cash_bought else []
    )
    assert paid(rollout) == [(0, 5000), (12, 5000)]


def test_funding_stages_reserve_each_unit_once() -> None:
    # CPI x10 with capital preservation off (2-year horizon) asks 50_000 of a 104_000 book. Both
    # risky sleeves rose 10% and are over target: equity by 3900, bonds by 1500. Stage by stage:
    # 36 old equity units (3960), 14 bond units (1540), all 50 cash units (5000), the 236 bond
    # units left (25_960), then the 64 old and 60 new equity units left (13_640).
    rollout, records = settle(
        Path(
            cpi=[100, 1000, 1000],
            equity=[100, 110, 110],
            bonds=[100, 110, 110],
            equity_lots=(("old", 100), ("new", 550)),
        )
    )
    assert rollout.stop is None
    assert records[1].funding == (
        (Stage.OVERWEIGHT_EQUITY, 3960),
        (Stage.OVERWEIGHT_BONDS, 1540),
        (Stage.CASH, 5000),
        (Stage.BONDS, 25_960),
        (Stage.EQUITY, 13_640),
    )
    assert sold(rollout, 12) == [
        (AssetId(Sleeve.EQUITY), (sale("old", 36),)),
        (AssetId(Sleeve.BONDS), (sale("bonds", 14),)),
        (AssetId(Sleeve.CASH), (sale("cash", 50),)),
        (AssetId(Sleeve.BONDS), (sale("bonds", 236),)),
        (AssetId(Sleeve.EQUITY), (sale("old", 64), sale("new", 60))),
    ]
    assert paid(rollout) == [(0, 5000), (12, 50_000)]
    assert rollout.summary.cash[0].values[-1] == 100


def test_unfunded_withdrawal_stops_after_its_sales_settle() -> None:
    # CPI x100 asks 500_000 of a 95_000 book: every sleeve sells, then the withdrawal is rejected.
    rollout, records = settle(Path(cpi=[100, 10_000, 10_000], equity=FLAT[:3], bonds=FLAT[:3]))
    assert records[1].requested == 500_000
    assert rollout.stop == RejectedAction(month=12, action_index=3)
    assert [(type(row.action), type(row.outcome)) for row in receipts(rollout, 12)] == [
        (Sell, Executed),
        (Sell, Executed),
        (Sell, Executed),
        (Consume, Rejected),
    ]
    assert paid(rollout) == [(0, 5000), (12, 0)]
    summary = rollout.summary
    assert [series.values[-1] for series in summary.cash] == [95_000]
    assert [series.values[-1] for series in summary.public_holdings] == [0, 0, 0]


def test_a_bond_sleeve_whose_price_fell_is_rising_when_its_coupon_made_the_year_positive() -> None:
    # Bonds 100 -> 99 but pay 3 per unit: 250 * -1 + 750 = +500 over the year, so they count as
    # rising. Year 1 opens at 5000 cash + 24_750 + 750 payouts + 65_000 = 95_500; bonds at 25_500
    # are 1625 over their 23_875 target. The 750 of payouts fund first, then 9 units (891); cash
    # funds the other 3359 as 34 units (3400). On price alone the bonds fell, and cash would fund all.
    rollout, records = settle(Path(cpi=[100, 100, 100], equity=FLAT[:3], bonds=[100, 99, 99], bond_payout=3))
    assert (records[1].opening_wealth, records[1].funding) == (
        95_500,
        ((Stage.OVERWEIGHT_BONDS, 1641), (Stage.CASH, 3400)),
    )
    assert [type(action) for action in actions(rollout, 12)] == [Transfer, Sell, Sell, Consume]
    assert sold(rollout, 12) == [
        (AssetId(Sleeve.BONDS), (sale("bonds", 9),)),
        (AssetId(Sleeve.CASH), (sale("cash", 34),)),
    ]
    assert paid(rollout) == [(0, 5000), (12, 5000)]


def test_estimated_tax_between_reviews_is_advanced_by_the_stages_and_repaid_from_the_next_withdrawal() -> None:
    # A 400 prior-year tax asks 100 in months 3, 5 and 8. Year 0 has no reserve yet, so each is
    # advanced from the portfolio: checking is empty, so one cash unit each. The year's income is
    # nil and its tax 0, so January owes nothing more; the 300 paid stays an unrefunded prepayment.
    rollout, records = settle(Path(cpi=[100, 104, 104], equity=FLAT[:3], bonds=FLAT[:3], prior_year_tax=400))
    assert paid(rollout) == [(0, 5000), (3, 100), (5, 100), (8, 100), (12, 4900), (15, 100), (17, 100), (20, 100)]
    assert [type(action) for action in actions(rollout, 3)] == [Sell, Transfer, PayClaim]
    assert sold(rollout, 3) == [(AssetId(Sleeve.CASH), (sale("cash", 1),))]
    # Year 1 opens at 94_700, below the 95_000 book only by the 300 advanced: not a loss, so the
    # 4% CPI rise applies (a loss would freeze 5200, which exceeds 5% of 94_700). 5200 first repays
    # the 300 and reserves nothing, since year 0's tax was 0; 4900 is spent, from the 47 cash
    # units left and 2 bond units.
    assert (records[1].opening_wealth, records[1].spending.inflation, records[1].requested) == (
        94_700,
        Inflation.APPLIED,
        5200,
    )
    assert (records[1].repaid, records[1].reserved, records[1].funding) == (
        300,
        0,
        ((Stage.CASH, 4700), (Stage.BONDS, 200)),
    )


def test_selected_replay_with_fresh_memory_reproduces_the_path() -> None:
    paths = [
        Path(cpi=[100, 104, 104], equity=FLAT[:3], bonds=FLAT[:3]),
        Path(cpi=[10_000, 10_400, 10_816], equity=[100, 120, 120], bonds=[100, 104, 104]),
    ]
    together, population = run(paths, [0, 1])
    (alone,), replay = run(paths, [1])
    assert alone.summary == together[1].summary
    assert replay.memory[1].records == population.memory[1].records


if __name__ == "__main__":
    pytest_bazel.main()
