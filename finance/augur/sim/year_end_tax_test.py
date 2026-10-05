"""What a filer owes at a year end, and what paying it does to their cash."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any

import polars as pl
import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.policy.cash_band_household import CashBandHousehold, SecuritySleeve
from finance.augur.policy.funding import ClaimPayer
from finance.augur.sim.actions import LotSale, Sell
from finance.augur.sim.books import AccountRef, TaxLiabilityState
from finance.augur.sim.fixed_point import quantity_scale_for_asset, quantity_to_quanta, round_currency_amount
from finance.augur.sim.ids import AccountId, AgentId, AssetId, JurisdictionId, LotId
from finance.augur.sim.income import (
    ORDINARY_INCOME,
    InterestCharacter,
    InterestIncome,
    Municipal,
    Taxable,
    TransferIncomeCategory,
)
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath, Series
from finance.augur.sim.money import USD
from finance.augur.sim.results import RejectedAction, Rollout
from finance.augur.sim.schedule import Recurring
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tax_indexation import CpiIndexedLaw, FixedNominalLaw, TaxIndexation
from finance.augur.sim.tax_profile import FilingStatus, TaxProfile, compile_profile
from finance.augur.sim.testing.rollouts import book, cash
from finance.augur.sim.testing.scripted import Scripted
from finance.augur.sim.testing.series import level_series
from finance.augur.sim.testing.session import each, finish
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
ALICE, PAYROLL, IRS, LANDLORD = AgentId("alice"), AgentId("payroll"), AgentId("irs"), AgentId("landlord")
CHECKING = AccountId("checking")
FEDERAL, CALIFORNIA = JurisdictionId("federal_us"), JurisdictionId("california")
VTI = SecurityKey(symbol=SecuritySymbol("vti"))
IXUS = SecurityKey(symbol=SecuritySymbol("ixus"))


def usd(quanta: Any) -> float:
    """Currency quanta as dollars; a polars column or row hands the count back untyped."""
    return int(quanta) / 100


def wage(annual: int) -> Decimal:
    """A monthly paycheck, rounded to cents before it enters the engine."""
    return round_currency_amount(Decimal(annual) / 12, quantum=QUANTUM)


@dataclass(frozen=True)
class Checking:
    """A party's checking account and its opening balance in dollars."""

    agent_id: AgentId
    balance: Decimal | int = 0


@dataclass(frozen=True)
class Lot:
    """One of Alice's opening lots, held in checking."""

    lot_id: LotId
    asset: SecurityKey
    quantity: Decimal | int
    cost_basis: int
    purchase_month: int


def sale(cause_id: str, lot_id: LotId, asset: SecurityKey, *, quantity: Decimal | int) -> Sell:
    scale = quantity_scale_for_asset(asset)
    return Sell(
        cause_id=cause_id,
        agent_id=ALICE,
        proceeds_account_id=CHECKING,
        asset_id=AssetId(asset.symbol),
        lots=(LotSale(account_id=CHECKING, lot_id=lot_id, units=quantity_to_quanta(quantity, scale=scale)),),
    )


@dataclass(frozen=True)
class Monthly:
    """A cashflow paid every month from month zero; its income category joins the world's tax vocabulary."""

    cause_id: str
    payer: AgentId
    payee: AgentId
    amount: int
    income_category: TransferIncomeCategory | None
    end_month: int


def monthly(
    cause_id: str, payer: AgentId, payee: AgentId, amount: Decimal, *, income: bool, end_month: int = 11
) -> Monthly:
    return Monthly(cause_id, payer, payee, USD.quanta(amount), ORDINARY_INCOME if income else None, end_month)


def monthly_interest(cause_id: str, character: InterestCharacter, amount: Decimal) -> Monthly:
    """A year of monthly coupons of `character` into Alice's checking."""
    return Monthly(cause_id, PAYROLL, ALICE, USD.quanta(amount), InterestIncome(character=character), 11)


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


@dataclass(frozen=True)
class Situation:
    """Alice's accounts, the wages and rent that move without her asking, and what she holds."""

    horizon_months: int
    accounts: tuple[Checking, ...]
    jurisdiction_ids: tuple[JurisdictionId, ...] = (FEDERAL, CALIFORNIA)
    prior_year_tax: Decimal | int = 0
    recurring_transfers: tuple[Monthly, ...] = ()
    lots: tuple[Lot, ...] = ()
    sales: Mapping[int, tuple[Sell, ...]] = field(default_factory=dict)
    # The holding Alice sells to fund her claims; with none, she only pays them.
    funded_by: SecurityKey | None = None
    prices: Mapping[SecurityKey, Sequence[float]] = field(default_factory=dict)
    # The modeled CPI path; `None` holds CPI flat wherever tax is CPI-indexed and models none otherwise.
    cpi: tuple[int, ...] | None = None


@pytest.fixture
def indexation() -> TaxIndexation:
    """The tables' law-year amounts in nominal dollars; `under_both_indexations` overrides it."""
    return FixedNominalLaw()


# A flat CPI from a start in the tables' law year indexes by exactly 1, so the CPI-indexed law must
# reproduce the nominal figures. The cases that opt in reach every amount the tables index (ordinary
# brackets, standard deduction, long-term gain brackets) and a January true-up.
under_both_indexations = pytest.mark.parametrize(
    "indexation",
    [FixedNominalLaw(), CpiIndexedLaw(start_year=2024, law_year_to_start=Fraction(1))],
    ids=["fixed_nominal", "cpi_indexed_flat"],
)


def compose(case: Situation, indexation: TaxIndexation) -> World:
    horizon = case.horizon_months
    series = level_series(
        {asset: [levels] for asset, levels in case.prices.items()}, rollout_count=1, horizon_months=horizon
    )
    if isinstance(indexation, CpiIndexedLaw):
        cpi = (100,) * (horizon + 1) if case.cpi is None else case.cpi
        series = (*series, Series(series_id="inflation", snapshots=horizon + 1, values=cpi))
    jurisdictions = {id_: load_jurisdiction(id_) for id_ in case.jurisdiction_ids}
    world = World(
        MarketPath(series, 0, rollout_count=1),
        horizon_months=horizon,
        income_sources=tuple(
            dict.fromkeys(
                [
                    ORDINARY_INCOME,
                    *(flow.income_category for flow in case.recurring_transfers if flow.income_category is not None),
                ]
            )
        ),
    )
    for opened in case.accounts:
        world.declare_account(
            account=AccountRef(agent_id=opened.agent_id, account_id=CHECKING),
            opening_balance=USD.quanta(opened.balance),
        )
    world.track(
        TaxAuthority(
            compile_profile(
                TaxProfile(
                    agent_id=ALICE,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=list(case.jurisdiction_ids),
                    tax_authority_agent_id=IRS,
                    prior_year_tax=Decimal(case.prior_year_tax),
                ),
                jurisdictions,
                currency=USD,
            ),
            indexation=indexation,
        )
    )
    for asset in dict.fromkeys(held.asset for held in case.lots):
        world.declare_pool(
            agent_id=ALICE,
            account_id=CHECKING,
            asset_id=AssetId(asset.symbol),
            quantity_scale=quantity_scale_for_asset(asset),
        )
    for held in case.lots:
        scale = quantity_scale_for_asset(held.asset)
        world.hold_lot(
            lot_id=held.lot_id,
            agent_id=ALICE,
            account_id=CHECKING,
            asset_id=AssetId(held.asset.symbol),
            purchase_month=held.purchase_month,
            quantity_scale=scale,
            units=quantity_to_quanta(held.quantity, scale=scale),
            basis=USD.quanta(held.cost_basis),
        )
    for flow in case.recurring_transfers:
        world.declare_flow(
            cause_id=flow.cause_id,
            from_account=AccountRef(agent_id=flow.payer, account_id=CHECKING),
            to_account=AccountRef(agent_id=flow.payee, account_id=CHECKING),
            amount=flow.amount,
            income_category=flow.income_category,
            deduction_category=None,
            schedule=Recurring(start_month=0, end_month=flow.end_month),
        )
    return world


def run(case: Situation, indexation: TaxIndexation) -> Rollout:
    """Alice makes her scripted sales, sells on her band, then pays every due claim in full, in order."""
    household = Scripted(ClaimPayer(ALICE) if case.funded_by is None else sell_into_cash(case.funded_by), case.sales)
    return one(finish(ActionSession({0: compose(case, indexation)}, ALICE), each(household.decide)).rollouts)


def owed(rollout: Rollout, month: int) -> list[TaxLiabilityState]:
    return sorted(
        (row for row in book(rollout, month).tax_liabilities if row.active), key=lambda row: row.jurisdiction_id
    )


def ordinary_income(rollout: Rollout, month: int) -> float:
    return usd(one(row.income for row in book(rollout, month).income if row.agent_id == ALICE))


def units_remaining(rollout: Rollout, lot_id: LotId, month: int) -> float:
    held = one(row for row in book(rollout, month).lots if row.lot_id == lot_id)
    return held.units_remaining / held.quantity_scale


def tax_transfers(rollout: Rollout) -> pl.DataFrame:
    assert rollout.trace is not None
    return rollout.trace.events.transfers.filter(pl.col("cause_id").str.contains("tax")).sort(
        ["month_index", "cause_id"]
    )


def by_jurisdiction(frame: pl.DataFrame) -> dict[str, dict[str, Any]]:
    return {row["jurisdiction_id"]: row for row in frame.iter_rows(named=True)}


def test_year_end_tax_accrual_federal_and_california_single_filer(indexation: TaxIndexation) -> None:
    """L7 — Alice gets $200k of W-2 income in year 0. At month 11
    the engine computes federal + CA tax on (200000 - std_deduction)
    and owes one liability per jurisdiction.

    Monthly paychecks are rounded to cents before entering the engine:
    12 * $16,666.67 = $200,000.04. Federal: $200,000.04 - $14,600
    = $185,400.04 taxable.
      10% × 11600 + 12% × 35550 + 22% × 53375 + 24% × 84875
      = 1160.00 + 4266.00 + 11742.50 + 20370.01 = 37538.51
    California: $200,000.04 - $5,363 = $194,637.04 taxable.
      1% × 10412 + 2% × 14272 + 4% × 14275 + 6% × 15122 + 8% × 14269
      + 9.3% × 126287 = 104.12 + 285.44 + 571.00 + 907.32 + 1141.52
      + 11744.69 = 14754.09
    """
    rollout = run(
        Situation(
            horizon_months=12,
            accounts=(Checking(ALICE), Checking(PAYROLL), Checking(IRS)),
            recurring_transfers=(monthly("alice_paycheck", PAYROLL, ALICE, wage(200_000), income=True),),
        ),
        indexation,
    )
    assert rollout.trace is not None
    events = rollout.trace.events

    # 12 paycheck transfers fired (income_category = "ordinary").
    assert events.transfers.filter(pl.col("income_category") == "ordinary").height == 12

    # Two tax accruals at month 11 — federal + CA.
    assert events.tax_accruals.height == 2
    accruals = by_jurisdiction(events.tax_accruals)
    annual_income = 12 * float(wage(200_000))
    assert usd(accruals[FEDERAL]["amount_quanta"]) == pytest.approx(37538.51, abs=0.01)
    assert usd(accruals[CALIFORNIA]["amount_quanta"]) == pytest.approx(14754.09, abs=0.02)
    assert accruals[FEDERAL]["month_index"] == 11
    assert accruals[FEDERAL]["tax_year_end_month"] == 11
    breakdowns = by_jurisdiction(events.tax_breakdowns)
    assert usd(breakdowns[FEDERAL]["ordinary_income_quanta"]) == pytest.approx(annual_income, abs=0.02)
    assert usd(breakdowns[FEDERAL]["ordinary_taxable_quanta"]) == pytest.approx(185_400.04, abs=0.02)
    assert usd(breakdowns[FEDERAL]["ordinary_tax_quanta"]) == pytest.approx(37_538.51, abs=0.01)
    assert usd(breakdowns[FEDERAL]["total_tax_quanta"]) == pytest.approx(37_538.51, abs=0.01)

    # At the end of the horizon one liability per jurisdiction stands, with matching amounts.
    ending = owed(rollout, 12)
    assert [row.jurisdiction_id for row in ending] == [CALIFORNIA, FEDERAL]
    assert usd(ending[0].amount_owed) == pytest.approx(14754.09, abs=0.02)
    assert usd(ending[1].amount_owed) == pytest.approx(37538.51, abs=0.01)

    # YTD reflects accumulated income across the year; the year-end reset at month 11
    # (visible in the book closed at month 12) drops it back to 0. The book closed at
    # month 11 is after Alice's eleventh paycheck.
    assert ordinary_income(rollout, 11) == pytest.approx(11 * float(wage(200_000)), abs=0.02)
    assert ordinary_income(rollout, 12) == 0.0


@under_both_indexations
def test_year_end_tax_includes_long_term_capital_gain_under_federal_ltcg_schedule(indexation: TaxIndexation) -> None:
    """L8 — Alice gets $50k W-2 wages, plus sells a long-held VTI
    lot (24 months pre-horizon) for a $20k gain at month 6.

    Federal taxable ordinary = 50000.04 - 14600 = 35400.04.
      10% × 11600 + 12% × 23800 = 1160 + 2856 = 4016.
    LTCG stacks above ordinary. The 0% bracket ends at 47025, so
    11624.96 of LTCG falls in 0%; the remaining 8375.04 falls in 15%.
      LTCG tax = 8375.04 × 0.15 = 1256.26.
    Federal total = 4016.00 + 1256.26 = 5272.26.

    California taxes LTCG as ordinary income.
      Total CA taxable = 50000.04 + 20000 - 5363 = 64637.04.
      1% × 10412 + 2% × 14272 + 4% × 14275 + 6% × 15122 + 8% × 10556
      = 104.12 + 285.44 + 571.00 + 907.32 + 844.48 = 2712.36."""
    rollout = run(
        Situation(
            horizon_months=12,
            accounts=(Checking(ALICE), Checking(PAYROLL), Checking(IRS)),
            lots=(Lot(LotId("alice_long_vti"), VTI, quantity=100, cost_basis=8000, purchase_month=-24),),
            recurring_transfers=(monthly("alice_paycheck", PAYROLL, ALICE, wage(50_000), income=True),),
            sales={6: (sale("alice_long_sale", LotId("alice_long_vti"), VTI, quantity=100),)},
            prices={VTI: [280.0] * 13},
        ),
        indexation,
    )
    assert rollout.trace is not None
    accruals = by_jurisdiction(rollout.trace.events.tax_accruals)
    assert usd(accruals[FEDERAL]["amount_quanta"]) == pytest.approx(5272.26, abs=0.01)
    assert usd(accruals[CALIFORNIA]["amount_quanta"]) == pytest.approx(2712.36, abs=0.01)
    breakdowns = by_jurisdiction(rollout.trace.events.tax_breakdowns)
    assert usd(breakdowns[FEDERAL]["ordinary_taxable_quanta"]) == pytest.approx(35_400.04, abs=0.02)
    assert usd(breakdowns[FEDERAL]["capital_gain_taxable_quanta"]) == pytest.approx(20_000.0, abs=0.02)
    assert usd(breakdowns[FEDERAL]["ordinary_tax_quanta"]) == pytest.approx(4_016.0, abs=0.01)
    assert usd(breakdowns[FEDERAL]["capital_gain_tax_quanta"]) == pytest.approx(1_256.26, abs=0.01)
    assert usd(breakdowns[CALIFORNIA]["ordinary_taxable_quanta"]) == pytest.approx(64_637.04, abs=0.02)
    assert usd(breakdowns[CALIFORNIA]["capital_gain_tax_quanta"]) == 0.0

    # YTD captured the LTCG ($20k), and only as a long-term gain, before the year-end reset.
    gain = one(row for row in book(rollout, 11).capital_gains if row.agent_id == ALICE)
    assert gain.short_term_gain == 0
    assert usd(gain.long_term_gain) == pytest.approx(20_000.0, abs=0.02)


@under_both_indexations
def test_niit_taxes_the_magi_excess_but_not_muni_interest_and_settles_in_the_true_up(indexation: TaxIndexation) -> None:
    """$180,000 wages, $30,000 corporate interest and $48,000 California muni interest.

    Muni interest is outside federal AGI and outside net investment income (Form 8960). MAGI is
    $210,000, $10,000 over the single threshold, and NII is the $30,000 of corporate interest,
    so NIIT is 3.8% of the smaller, $10,000: $380.00. Counting the muni coupons would have made
    it 3.8% of $58,000.
    Federal taxable = 210,000 - 14,600 = 195,400:
      10% × 11600 + 12% × 35550 + 22% × 53375 + 24% × 91425 + 32% × 3450
      = 1160 + 4266 + 11742.50 + 21942 + 1104 = 40214.50; with NIIT 40594.50.
    California exempts its own munis and taxes the corporate interest: 210,000 - 5,363 = 204,637:
      104.12 + 285.44 + 571.00 + 907.32 + 1141.52 + 9.3% × 136287 = 15684.09.
    No prior-year tax, so January's true-up is the whole 56278.59.
    """
    rollout = run(
        Situation(
            horizon_months=13,
            accounts=(Checking(ALICE), Checking(PAYROLL), Checking(IRS)),
            recurring_transfers=(
                monthly("alice_paycheck", PAYROLL, ALICE, Decimal(15_000), income=True),
                monthly_interest("alice_corporate_coupon", Taxable(), Decimal(2_500)),
                monthly_interest("alice_muni_coupon", Municipal(state=CALIFORNIA), Decimal(4_000)),
            ),
        ),
        indexation,
    )
    assert rollout.trace is not None
    breakdowns = by_jurisdiction(rollout.trace.events.tax_breakdowns)
    assert usd(breakdowns[FEDERAL]["net_investment_income_tax_quanta"]) == 380.00
    assert usd(breakdowns[FEDERAL]["ordinary_tax_quanta"]) == 40_214.50
    assert usd(breakdowns[FEDERAL]["total_tax_quanta"]) == 40_594.50
    assert usd(breakdowns[CALIFORNIA]["net_investment_income_tax_quanta"]) == 0
    assert usd(breakdowns[CALIFORNIA]["total_tax_quanta"]) == 15_684.09
    assert [usd(row.amount_owed) for row in owed(rollout, 12)] == [15_684.09, 40_594.50]
    assert tax_transfers(rollout).select("month_index", "cause_id", "amount_quanta").to_dicts() == [
        {"month_index": 12, "cause_id": "alice_tax_true_up_y0", "amount_quanta": 5_627_859}
    ]


@pytest.mark.parametrize(
    ("monthly_wage", "surtax", "california_tax"),
    [
        # Taxable income exactly $1,000,000: the surtax starts above it.
        #   104.12 + 285.44 + 571.00 + 907.32 + 1141.52 + 9.3% × 280787 + 10.3% × 69824
        #   + 11.3% × 279310 + 12.3% × 301729 = 104989.16
        (Decimal("83780.25"), 0.00, 104_989.16),
        # $1,000,120: 12.3% × 120 = 14.76 more bracket tax, and 1% × 120 = 1.20 surtax.
        (Decimal("83790.25"), 1.20, 105_005.12),
        # $1,194,637: 1% × 194637 = 1946.37 on top of 128929.51 bracket tax.
        (Decimal(100_000), 1_946.37, 130_875.88),
    ],
)
def test_california_surtax_on_taxable_income_above_a_million(
    monthly_wage: Decimal, surtax: float, california_tax: float, indexation: TaxIndexation
) -> None:
    """California taxable income is wages less its $5,363 standard deduction. Wages alone are
    not net investment income, so none of this high income draws NIIT."""
    rollout = run(
        Situation(
            horizon_months=12,
            accounts=(Checking(ALICE), Checking(PAYROLL), Checking(IRS)),
            recurring_transfers=(monthly("alice_paycheck", PAYROLL, ALICE, monthly_wage, income=True),),
        ),
        indexation,
    )
    assert rollout.trace is not None
    breakdowns = by_jurisdiction(rollout.trace.events.tax_breakdowns)
    assert usd(breakdowns[CALIFORNIA]["taxable_income_surtax_quanta"]) == surtax
    assert usd(breakdowns[CALIFORNIA]["total_tax_quanta"]) == california_tax
    assert usd(breakdowns[FEDERAL]["net_investment_income_tax_quanta"]) == 0
    assert usd(breakdowns[FEDERAL]["taxable_income_surtax_quanta"]) == 0


def test_e2e_pinned_ltcg_tax_safe_harbor_and_cash_numerics(indexation: TaxIndexation) -> None:
    """Pinned deterministic e2e: wages + a long-held asset sale +
    federal/CA year tax + estimated-tax safe harbor + true-up.

    Alice earns $50k, sells a long-held VTI lot for $28k proceeds
    and $20k gain, and has $4k of prior-year tax. The safe-harbor
    quarterlies pay $1k at months 3/5/8/12; the month-12 true-up
    pays the remaining $3,984.62. Ending cash is:

      1000 + 50000.04 + 28000 - 7984.62 = 71015.42.
    """
    rollout = run(
        Situation(
            horizon_months=13,
            accounts=(Checking(ALICE, 1000), Checking(PAYROLL), Checking(IRS)),
            prior_year_tax=4000,
            lots=(Lot(LotId("alice_long_vti"), VTI, quantity=100, cost_basis=8000, purchase_month=-24),),
            recurring_transfers=(monthly("alice_paycheck", PAYROLL, ALICE, wage(50_000), income=True),),
            sales={6: (sale("alice_long_sale", LotId("alice_long_vti"), VTI, quantity=100),)},
            prices={VTI: [280.0] * 14},
        ),
        indexation,
    )
    assert rollout.trace is not None
    payments = tax_transfers(rollout)
    assert payments.select("month_index", "cause_id", "amount_quanta").to_dicts() == [
        {"month_index": 3, "cause_id": "alice_estimated_tax_q1_y0", "amount_quanta": 100_000},
        {"month_index": 5, "cause_id": "alice_estimated_tax_q2_y0", "amount_quanta": 100_000},
        {"month_index": 8, "cause_id": "alice_estimated_tax_q3_y0", "amount_quanta": 100_000},
        {"month_index": 12, "cause_id": "alice_estimated_tax_q4_y0", "amount_quanta": 100_000},
        {"month_index": 12, "cause_id": "alice_tax_true_up_y0", "amount_quanta": pytest.approx(398_462, abs=2)},
    ]
    assert usd(payments.get_column("amount_quanta").sum()) == pytest.approx(7_984.62, abs=0.02)

    settlement = rollout.trace.events.tax_settlements.row(0, named=True)
    assert settlement["month_index"] == 12
    assert settlement["tax_year_end_month"] == 11
    assert usd(settlement["amount_quanta"]) == pytest.approx(7_984.62, abs=0.02)
    assert usd(sum(row.amount_owed for row in owed(rollout, 12))) == pytest.approx(7_984.62, abs=0.02)
    assert usd(sum(row.amount_owed for row in owed(rollout, 13))) == pytest.approx(0.0, abs=0.02)

    assert cash(rollout, ALICE, 13) == pytest.approx(71_015.42, abs=0.02)
    assert units_remaining(rollout, LotId("alice_long_vti"), 13) == 0.0


def test_e2e_pinned_multi_asset_ltcg_stcg_tax_breakdown_numerics(indexation: TaxIndexation) -> None:
    """Pinned tax aggregation e2e: wages plus two asset sales.

    Alice earns $50,000.04 after cent-rounded monthly paychecks, sells one
    long-held lot for $10k LTCG and one short-held lot for $1.5k STCG.
    Federal ordinary taxable income is 50000.04 + 1500 - 14600 = 36900.04,
    producing $4,196 ordinary tax. The
    $10k LTCG still fits under the 0% LTCG bracket after stacking, so
    capital-gain tax is $0.
    """
    rollout = run(
        Situation(
            horizon_months=12,
            accounts=(Checking(ALICE), Checking(PAYROLL), Checking(IRS)),
            jurisdiction_ids=(FEDERAL,),
            lots=(
                Lot(LotId("alice_long_vti"), VTI, quantity=100, cost_basis=10000, purchase_month=-24),
                Lot(LotId("alice_short_ixus"), IXUS, quantity=10, cost_basis=500, purchase_month=0),
            ),
            recurring_transfers=(monthly("alice_paycheck", PAYROLL, ALICE, wage(50_000), income=True),),
            sales={
                6: (
                    sale("alice_long_sale", LotId("alice_long_vti"), VTI, quantity=100),
                    sale("alice_short_sale", LotId("alice_short_ixus"), IXUS, quantity=10),
                )
            },
            prices={VTI: [200.0] * 13, IXUS: [200.0] * 13},
        ),
        indexation,
    )
    assert rollout.trace is not None
    accrual = rollout.trace.events.tax_accruals.row(0, named=True)
    assert usd(accrual["amount_quanta"]) == pytest.approx(4_196.0, abs=0.01)
    breakdown = rollout.trace.events.tax_breakdowns.row(0, named=True)
    assert usd(breakdown["ordinary_income_quanta"]) == pytest.approx(50_000.04, abs=0.02)
    assert usd(breakdown["ltcg_quanta"]) == pytest.approx(10_000.0, abs=0.02)
    assert usd(breakdown["stcg_quanta"]) == pytest.approx(1_500.0, abs=0.02)
    assert usd(breakdown["ordinary_taxable_quanta"]) == pytest.approx(36_900.04, abs=0.02)
    assert usd(breakdown["ordinary_tax_quanta"]) == pytest.approx(4_196.0, abs=0.01)
    assert usd(breakdown["capital_gain_tax_quanta"]) == pytest.approx(0.0, abs=0.02)

    gain = one(row for row in book(rollout, 11).capital_gains if row.agent_id == ALICE)
    assert usd(gain.long_term_gain) == pytest.approx(10_000.0)
    assert usd(gain.short_term_gain) == pytest.approx(1_500.0)


def test_e2e_pinned_tax_payments_force_asset_liquidation_and_settle_liability(indexation: TaxIndexation) -> None:
    """Pinned obligation e2e: taxes are due-now outflows.

    Alice earns $50k and spends every paycheck on rent, so estimated
    taxes must be funded by selling VTI. Federal tax is $4,016.
    Prior-year safe harbor is $2,000: three $500 estimates in April,
    June, September; then January Q4 $500 plus $2,016 true-up.
    """
    rollout = run(
        Situation(
            horizon_months=13,
            accounts=(Checking(ALICE), Checking(PAYROLL), Checking(LANDLORD), Checking(IRS)),
            jurisdiction_ids=(FEDERAL,),
            prior_year_tax=2000,
            lots=(Lot(LotId("alice_vti_seed"), VTI, quantity=100, cost_basis=10000, purchase_month=-24),),
            recurring_transfers=(
                monthly("alice_paycheck", PAYROLL, ALICE, wage(50_000), income=True),
                monthly("alice_rent", ALICE, LANDLORD, wage(50_000), income=False),
            ),
            funded_by=VTI,
            prices={VTI: [100.0] * 14},
        ),
        indexation,
    )
    assert rollout.trace is not None
    payments = tax_transfers(rollout)
    assert payments.select("month_index", "cause_id", "amount_quanta").to_dicts() == [
        {"month_index": 3, "cause_id": "alice_estimated_tax_q1_y0", "amount_quanta": 50_000},
        {"month_index": 5, "cause_id": "alice_estimated_tax_q2_y0", "amount_quanta": 50_000},
        {"month_index": 8, "cause_id": "alice_estimated_tax_q3_y0", "amount_quanta": 50_000},
        {"month_index": 12, "cause_id": "alice_estimated_tax_q4_y0", "amount_quanta": 50_000},
        {"month_index": 12, "cause_id": "alice_tax_true_up_y0", "amount_quanta": 201_600},
    ]
    assert usd(rollout.trace.events.tax_settlements.get_column("amount_quanta").sum()) == pytest.approx(
        4_016.0, abs=0.01
    )

    policy_sales = rollout.trace.events.lot_dispositions.filter(pl.col("cause_id").str.starts_with("allocation_sale"))
    # Fixed-point FIFO sells fractional quanta for whole-unit-scale assets too: month-12 needs exactly
    # $2,516 at $100/unit, so it sells 25.16 units with no excess cash.
    assert policy_sales.sort("month_index").select("month_index", "units_sold", "proceeds_quanta").to_dicts() == [
        {"month_index": 3, "units_sold": pytest.approx(5.0), "proceeds_quanta": 50_000},
        {"month_index": 5, "units_sold": pytest.approx(5.0), "proceeds_quanta": 50_000},
        {"month_index": 8, "units_sold": pytest.approx(5.0), "proceeds_quanta": 50_000},
        {"month_index": 12, "units_sold": pytest.approx(25.16), "proceeds_quanta": 251_600},
    ]

    assert cash(rollout, ALICE, 13) == pytest.approx(0.0, abs=0.02)
    # 100 - (5+5+5+25.16) = 59.84 units remaining.
    assert units_remaining(rollout, LotId("alice_vti_seed"), 13) == pytest.approx(59.84, abs=0.02)
    assert usd(sum(row.amount_owed for row in owed(rollout, 13))) == pytest.approx(0.0, abs=0.02)
    assert rollout.stop is None


def test_year_end_tax_payment_debits_agent_cash(indexation: TaxIndexation) -> None:
    """The year-end tax accrual is followed by a January true-up
    payment to the tax authority. Alice earns $200k of W-2 income
    across year 0; with no prior-year safe-harbor amount configured,
    the full tax is paid as the month-12 true-up."""
    rollout = run(
        Situation(
            horizon_months=13,
            accounts=(Checking(ALICE), Checking(PAYROLL), Checking(IRS)),
            recurring_transfers=(monthly("alice_paycheck", PAYROLL, ALICE, wage(200_000), income=True),),
        ),
        indexation,
    )
    assert rollout.trace is not None

    # Year-end tax: $37,538.51 federal + $14,754.09 CA = $52,292.60.
    payments = tax_transfers(rollout)
    assert payments.height == 1
    assert usd(payments.get_column("amount_quanta").sum()) == pytest.approx(52_292.60, abs=0.02)
    assert payments.row(0, named=True)["cause_id"] == "alice_tax_true_up_y0"
    # Tax true-up fires in January after the year-end accrual.
    assert set(payments.get_column("month_index").to_list()) == {12}
    assert rollout.trace.events.tax_settlements.height == 1
    settlement = rollout.trace.events.tax_settlements.row(0, named=True)
    assert settlement["cause_id"] == "alice_tax_settlement_y0"
    assert usd(settlement["amount_quanta"]) == pytest.approx(52_292.60, abs=0.02)

    assert usd(sum(row.amount_owed for row in owed(rollout, 12))) == pytest.approx(52_292.60, abs=0.02)
    assert usd(sum(row.amount_owed for row in owed(rollout, 13))) == pytest.approx(0.0, abs=0.02)

    # Cash flow: $200,000.04 income - $52,292.60 tax = $147,707.44 at end of horizon.
    assert cash(rollout, ALICE, 13) == pytest.approx(147_707.44, abs=0.02)
    # The IRS sink accumulates the tax inflows.
    assert cash(rollout, IRS, 13) == pytest.approx(52_292.60, abs=0.02)


def test_tax_payment_can_trigger_rollout_failure_when_unfunded(indexation: TaxIndexation) -> None:
    """When the tax-payment true-up exceeds the agent's cash plus
    liquidity-policy sale proceeds, the rejected payment stops the path.
    The "mandatory obligation that fails the scenario if unpaid" pattern
    works for any cash outflow — taxes here, rent in other tests, later
    mortgages."""
    rollout = run(
        Situation(
            horizon_months=13,
            accounts=(Checking(ALICE), Checking(PAYROLL), Checking(IRS)),
            recurring_transfers=(
                monthly("alice_paycheck", PAYROLL, ALICE, wage(500_000), income=True),
                # Spend it all on rent, with payroll as the sink.
                monthly("alice_rent", ALICE, PAYROLL, wage(500_000), income=False),
            ),
        ),
        indexation,
    )
    # Alice has $0 cash after year 0 (income == rent), no assets,
    # but the tax bill arrives in January. Failure fires at month 12.
    assert rollout.trace is not None
    failures = rollout.trace.events.rollout_failures
    assert failures.height == 1
    assert failures.row(0, named=True)["month_index"] == 12
    assert rollout.stop == RejectedAction(month=12, action_index=0)


def test_cpi_indexed_amounts_follow_each_januarys_cpi() -> None:
    """$150,000 of wages in each of two years; CPI is flat through year 0 and half again higher from
    year 1's January, so year 0 applies the 2024 tables and year 1 their indexed amounts at 1.5.

    Year 0: federal 150,000 - 14,600 = 135,400:
      10% × 11600 + 12% × 35550 + 22% × 53375 + 24% × 34875 = 25538.50
    California 150,000 - 5,363 = 144,637:
      104.12 + 285.44 + 571.00 + 907.32 + 1141.52 + 9.3% × 76287 = 10104.091, rounded once: 10104.09
    Year 1: deductions 21,900 and 8,044.50, and every bracket edge × 1.5. Federal 128,100:
      10% × 17400 + 12% × 53325 + 22% × 57375 = 1740 + 6399 + 12622.50 = 20761.50
    California 141,955.50:
      1% × 15618 + 2% × 21408 + 4% × 21412.50 + 6% × 22683 + 8% × 21403.50 + 9.3% × 39430.50
      = 156.18 + 428.16 + 856.50 + 1360.98 + 1712.28 + 3667.0365 = 8181.1365, rounded once: 8181.14
    """
    rollout = run(
        Situation(
            horizon_months=24,
            accounts=(Checking(ALICE), Checking(PAYROLL), Checking(IRS)),
            recurring_transfers=(
                monthly("alice_paycheck", PAYROLL, ALICE, Decimal(12_500), income=True, end_month=23),
            ),
            cpi=(100,) * 12 + (150,) * 13,
        ),
        CpiIndexedLaw(start_year=2024, law_year_to_start=Fraction(1)),
    )
    assert rollout.trace is not None
    breakdowns = {
        (row["jurisdiction_id"], row["month_index"]): row
        for row in rollout.trace.events.tax_breakdowns.iter_rows(named=True)
    }
    assert {key: usd(row["total_tax_quanta"]) for key, row in breakdowns.items()} == {
        (FEDERAL, 11): 25_538.50,
        (CALIFORNIA, 11): 10_104.09,
        (FEDERAL, 23): 20_761.50,
        (CALIFORNIA, 23): 8_181.14,
    }
    assert usd(breakdowns[(FEDERAL, 23)]["standard_deduction_quanta"]) == 21_900
    assert usd(breakdowns[(CALIFORNIA, 23)]["standard_deduction_quanta"]) == 8_044.50


if __name__ == "__main__":
    pytest_bazel.main()
