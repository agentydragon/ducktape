"""What a filer owes at a year end, and what paying it does to their cash."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np
import polars as pl
import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.product.household import ConfiguredHousehold
from finance.augur.sim.actions import DecisionActions
from finance.augur.sim.books import AccountRef, Book, TaxLiabilityState
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import (
    currency_amount_to_quanta,
    quantity_scale_for_asset,
    quantity_to_quanta,
    round_currency_amount,
)
from finance.augur.sim.ids import AgentId
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedJurisdiction,
    PreparedLot,
    PreparedRecurringTransfer,
    _AllocationPolicy,
    _ScheduledSale,
    _SleeveTarget,
)
from finance.augur.sim.results import Finished, Rollout, UnpaidClaims
from finance.augur.sim.scenario import ORDINARY_INCOME, FilingStatus, TaxProfile
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
ALICE, PAYROLL, IRS, LANDLORD = "alice", "payroll", "irs", "landlord"
CHECKING = "checking"
FEDERAL, CALIFORNIA = "federal_us", "california"
VTI = SecurityKey(symbol=SecuritySymbol("vti"))
IXUS = SecurityKey(symbol=SecuritySymbol("ixus"))


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def usd(quanta: Any) -> float:
    """Currency quanta as dollars; a polars column or row hands the count back untyped."""
    return int(quanta) / 100


def wage(annual: int) -> Decimal:
    """A monthly paycheck, rounded to cents before it enters the engine."""
    return round_currency_amount(Decimal(annual) / 12, quantum=QUANTUM)


def account(agent_id: str, balance: Decimal | int = 0) -> PreparedAccount:
    return PreparedAccount(account=AccountRef(agent_id=agent_id, account_id=CHECKING), opening_balance=money(balance))


def lot(lot_id: str, asset: SecurityKey, *, quantity: float, cost_basis: int, purchase_month: int) -> PreparedLot:
    scale = quantity_scale_for_asset(asset)
    return PreparedLot(
        lot_id=lot_id,
        agent_id=ALICE,
        account_id=CHECKING,
        asset_id=str(asset.symbol),
        purchase_month=purchase_month,
        quantity_scale=scale,
        units=int(quantity_to_quanta(quantity, scale=scale)),
        basis=money(cost_basis),
    )


def sale(cause_id: str, asset: SecurityKey, *, month: int, quantity: float) -> _ScheduledSale:
    scale = quantity_scale_for_asset(asset)
    return _ScheduledSale(
        month=month,
        cause_id=cause_id,
        agent_id=ALICE,
        account_id=CHECKING,
        asset_id=str(asset.symbol),
        units=int(quantity_to_quanta(quantity, scale=scale)),
        proceeds_account_id=CHECKING,
    )


def monthly(
    cause_id: str, payer: str, payee: str, amount: Decimal, *, income: bool, end_month: int | None = 11
) -> PreparedRecurringTransfer:
    return PreparedRecurringTransfer(
        start_month=0,
        end_month=end_month,
        cause_id=cause_id,
        from_account=AccountRef(agent_id=payer, account_id=CHECKING),
        to_account=AccountRef(agent_id=payee, account_id=CHECKING),
        amount=money(amount),
        income_category=ORDINARY_INCOME if income else None,
        deduction_category=None,
    )


def sell_into_cash(asset: SecurityKey) -> _AllocationPolicy:
    """A band with no floor and no ceiling: Alice holds no spare cash and funds her claims by selling."""
    return _AllocationPolicy(
        agent_id=ALICE,
        account_id=CHECKING,
        source_account_ids=(),
        sleeves=(_SleeveTarget(asset_id=str(asset.symbol), weight=1, quantity_scale=quantity_scale_for_asset(asset)),),
        cash_floor=0,
        cash_ceiling=0,
        cause_id_prefix="allocation_sale",
        allow_purchases=False,
        rebalance_tolerance_ppb=None,
    )


@dataclass(frozen=True)
class Situation:
    """Alice's accounts, the wages and rent that move without her asking, and what she holds."""

    horizon_months: int
    accounts: tuple[PreparedAccount, ...]
    jurisdiction_ids: tuple[str, ...] = (FEDERAL, CALIFORNIA)
    prior_year_tax: Decimal | int = 0
    recurring_transfers: tuple[PreparedRecurringTransfer, ...] = ()
    lots: tuple[PreparedLot, ...] = ()
    scheduled_sales: tuple[_ScheduledSale, ...] = ()
    policies: tuple[_AllocationPolicy, ...] = ()
    prices: Mapping[SecurityKey, Sequence[float]] = field(default_factory=dict)


def compose(case: Situation) -> World:
    horizon = case.horizon_months
    series = compile_series(
        ExternalSeriesContext.from_level_blocks(
            [(asset, np.asarray([levels], dtype=np.float64)) for asset, levels in case.prices.items()],
            rollout_count=1,
            horizon_months=horizon,
        ),
        rollout_count=1,
        horizon_months=horizon,
        currency_quantum=QUANTUM,
    )
    jurisdictions = {id_: load_jurisdiction(id_) for id_ in case.jurisdiction_ids}
    world = World(
        MarketPath(series, 0, rollout_count=1),
        horizon_months=horizon,
        income_sources=(ORDINARY_INCOME,),
        jurisdictions=tuple(
            PreparedJurisdiction(jurisdiction_id=id_, level=rules.level) for id_, rules in jurisdictions.items()
        ),
    )
    for opening in case.accounts:
        world.declare_account(opening)
    if case.jurisdiction_ids:
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
                    quantum=QUANTUM,
                )
            )
        )
    for pool in {
        held.asset_id: PreparedHoldingPool(
            agent_id=ALICE, account_id=CHECKING, asset_id=held.asset_id, quantity_scale=held.quantity_scale
        )
        for held in case.lots
    }.values():
        world.declare_pool(pool)
    for held in case.lots:
        world.hold(held)
    world.recurring_transfers = case.recurring_transfers
    return world


def run(case: Situation) -> Rollout:
    """Alice sells on schedule and on her band, then pays each account's claims all or none."""
    household = ConfiguredHousehold(AgentId(ALICE), case.policies, scheduled_sales=case.scheduled_sales)
    session = ActionSession({0: compose(case)}, ALICE)
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
    return one(batch.rollouts)


def book(rollout: Rollout, month: int) -> Book:
    assert rollout.trace is not None
    return one(entry for entry in rollout.trace.books if entry.month == month)


def cash(rollout: Rollout, agent_id: str, month: int) -> float:
    account_ = AccountRef(agent_id=agent_id, account_id=CHECKING)
    return usd(one(row.balance for row in book(rollout, month).balances if row.account == account_))


def owed(rollout: Rollout, month: int) -> list[TaxLiabilityState]:
    return sorted(
        (row for row in book(rollout, month).tax_liabilities if row.active), key=lambda row: row.jurisdiction_id
    )


def ordinary_income(rollout: Rollout, month: int) -> float:
    return usd(one(row.income for row in book(rollout, month).income if row.agent_id == ALICE))


def units_remaining(rollout: Rollout, lot_id: str, month: int) -> float:
    held = one(row for row in book(rollout, month).lots if row.lot_id == lot_id)
    return held.units_remaining / held.quantity_scale


def tax_transfers(rollout: Rollout) -> pl.DataFrame:
    assert rollout.trace is not None
    return rollout.trace.events.transfers.filter(pl.col("cause_id").str.contains("tax")).sort(
        ["month_index", "cause_id"]
    )


def by_jurisdiction(frame: pl.DataFrame) -> dict[str, dict[str, Any]]:
    return {row["jurisdiction_id"]: row for row in frame.iter_rows(named=True)}


def test_year_end_tax_accrual_federal_and_california_single_filer() -> None:
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
            accounts=(account(ALICE), account(PAYROLL), account(IRS)),
            recurring_transfers=(monthly("alice_paycheck", PAYROLL, ALICE, wage(200_000), income=True),),
        )
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


def test_year_end_tax_includes_long_term_capital_gain_under_federal_ltcg_schedule() -> None:
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
            accounts=(account(ALICE), account(PAYROLL), account(IRS)),
            lots=(lot("alice_long_vti", VTI, quantity=100.0, cost_basis=8000, purchase_month=-24),),
            recurring_transfers=(monthly("alice_paycheck", PAYROLL, ALICE, wage(50_000), income=True),),
            scheduled_sales=(sale("alice_long_sale", VTI, month=6, quantity=100.0),),
            prices={VTI: [280.0] * 13},
        )
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


def test_e2e_pinned_ltcg_tax_safe_harbor_and_cash_numerics() -> None:
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
            accounts=(account(ALICE, 1000), account(PAYROLL), account(IRS)),
            prior_year_tax=4000,
            lots=(lot("alice_long_vti", VTI, quantity=100.0, cost_basis=8000, purchase_month=-24),),
            recurring_transfers=(monthly("alice_paycheck", PAYROLL, ALICE, wage(50_000), income=True),),
            scheduled_sales=(sale("alice_long_sale", VTI, month=6, quantity=100.0),),
            prices={VTI: [280.0] * 14},
        )
    )
    assert rollout.trace is not None
    accruals = by_jurisdiction(rollout.trace.events.tax_accruals)
    assert usd(accruals[FEDERAL]["amount_quanta"]) == pytest.approx(5272.26, abs=0.01)
    assert usd(accruals[CALIFORNIA]["amount_quanta"]) == pytest.approx(2712.36, abs=0.01)

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
    assert units_remaining(rollout, "alice_long_vti", 13) == 0.0


def test_e2e_pinned_multi_asset_ltcg_stcg_tax_breakdown_numerics() -> None:
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
            accounts=(account(ALICE), account(PAYROLL), account(IRS)),
            jurisdiction_ids=(FEDERAL,),
            lots=(
                lot("alice_long_vti", VTI, quantity=100.0, cost_basis=10000, purchase_month=-24),
                lot("alice_short_ixus", IXUS, quantity=10.0, cost_basis=500, purchase_month=0),
            ),
            recurring_transfers=(monthly("alice_paycheck", PAYROLL, ALICE, wage(50_000), income=True),),
            scheduled_sales=(
                sale("alice_long_sale", VTI, month=6, quantity=100.0),
                sale("alice_short_sale", IXUS, month=6, quantity=10.0),
            ),
            prices={VTI: [200.0] * 13, IXUS: [200.0] * 13},
        )
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


def test_e2e_pinned_tax_payments_force_asset_liquidation_and_settle_liability() -> None:
    """Pinned obligation e2e: taxes are due-now outflows.

    Alice earns $50k and spends every paycheck on rent, so estimated
    taxes must be funded by selling VTI. Federal tax is $4,016.
    Prior-year safe harbor is $2,000: three $500 estimates in April,
    June, September; then January Q4 $500 plus $2,016 true-up.
    """
    rollout = run(
        Situation(
            horizon_months=13,
            accounts=(account(ALICE), account(PAYROLL), account(LANDLORD), account(IRS)),
            jurisdiction_ids=(FEDERAL,),
            prior_year_tax=2000,
            lots=(lot("alice_vti_seed", VTI, quantity=100.0, cost_basis=10000, purchase_month=-24),),
            recurring_transfers=(
                monthly("alice_paycheck", PAYROLL, ALICE, wage(50_000), income=True),
                monthly("alice_rent", ALICE, LANDLORD, wage(50_000), income=False),
            ),
            policies=(sell_into_cash(VTI),),
            prices={VTI: [100.0] * 14},
        )
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
    assert units_remaining(rollout, "alice_vti_seed", 13) == pytest.approx(59.84, abs=0.02)
    assert usd(sum(row.amount_owed for row in owed(rollout, 13))) == pytest.approx(0.0, abs=0.02)
    assert rollout.stop is None


def test_explicit_empty_tax_profiles_means_no_year_end_accrual() -> None:
    """An explicit no-tax scenario emits no year-end accruals."""
    rollout = run(
        Situation(
            horizon_months=12,
            accounts=(account(ALICE), account(PAYROLL)),
            jurisdiction_ids=(),
            recurring_transfers=(
                monthly("alice_paycheck", PAYROLL, ALICE, Decimal(5000), income=True, end_month=None),
            ),
        )
    )
    assert rollout.trace is not None
    assert rollout.trace.events.tax_accruals.is_empty()
    assert not [row for entry in rollout.trace.books for row in entry.tax_liabilities]


def test_year_end_tax_payment_debits_agent_cash() -> None:
    """The year-end tax accrual is followed by a January true-up
    payment to the tax authority. Alice earns $200k of W-2 income
    across year 0; with no prior-year safe-harbor amount configured,
    the full tax is paid as the month-12 true-up."""
    rollout = run(
        Situation(
            horizon_months=13,
            accounts=(account(ALICE), account(PAYROLL), account(IRS)),
            recurring_transfers=(monthly("alice_paycheck", PAYROLL, ALICE, wage(200_000), income=True),),
        )
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


def test_tax_payment_can_trigger_rollout_failure_when_unfunded() -> None:
    """When the tax-payment true-up exceeds the agent's cash plus
    liquidity-policy sale proceeds, the unpaid claim stops the path.
    The "mandatory obligation that fails the scenario if unpaid" pattern
    works for any cash outflow — taxes here, rent in other tests, later
    mortgages."""
    rollout = run(
        Situation(
            horizon_months=13,
            accounts=(account(ALICE), account(PAYROLL), account(IRS)),
            recurring_transfers=(
                monthly("alice_paycheck", PAYROLL, ALICE, wage(500_000), income=True),
                # Spend it all on rent, with payroll as the sink.
                monthly("alice_rent", ALICE, PAYROLL, wage(500_000), income=False),
            ),
        )
    )
    # Alice has $0 cash after year 0 (income == rent), no assets,
    # but the tax bill arrives in January. Failure fires at month 12.
    assert rollout.trace is not None
    failures = rollout.trace.events.rollout_failures
    assert failures.height == 1
    assert failures.row(0, named=True)["month_index"] == 12
    assert isinstance(rollout.stop, UnpaidClaims)
    assert rollout.stop.month == 12


if __name__ == "__main__":
    pytest_bazel.main()
