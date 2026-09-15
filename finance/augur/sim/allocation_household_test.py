"""Allocation funding, grouped claim settlement and exact purchases in one household's batch.

The tax schedule below is deliberately synthetic: 20% ordinary, 10% long-term,
no deductions. Assertions pin accounting/timing, not statutory fidelity.
"""

from dataclasses import dataclass, replace
from decimal import Decimal

import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.policy.configured_household import ConfiguredHousehold
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Book, SecurityLotState
from finance.augur.sim.capture import FinancialCapture, FinancialOutput
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE, currency_amount_to_quanta, quantity_scale_for_asset
from finance.augur.sim.ids import AgentId
from finance.augur.sim.jurisdictions import Jurisdiction, JurisdictionLevel, TaxBracket
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import MAX_COUNT
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedDistribution,
    PreparedDistributionSlice,
    PreparedHoldingPool,
    PreparedIndexedAmount,
    PreparedJurisdiction,
    PreparedLot,
    PreparedObligation,
    PreparedRecurringObligation,
    PreparedSeries,
    PreparedTransfer,
    _AllocationPolicy,
    _ScheduledSale,
    _SleeveTarget,
)
from finance.augur.sim.scenario import ORDINARY_INCOME, InterestIncome, TaxProfile
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
STOCK = SecurityKey(symbol=SecuritySymbol("stock"))
SECOND = SecurityKey(symbol=SecuritySymbol("second"))
SCALE = quantity_scale_for_asset(STOCK)
ALICE = "alice"
WORLD = "world"
CHECKING = "checking"
BROKERAGE = "brokerage"
SYNTHETIC = "synthetic"
TAX = Jurisdiction(
    jurisdiction_id=SYNTHETIC,
    level=JurisdictionLevel.FEDERAL,
    ordinary_income_brackets={"single": [TaxBracket(upper="Infinity", rate=0.2)]},
    ltcg_brackets={"single": [TaxBracket(upper="Infinity", rate=0.1)]},
    standard_deduction={"single": 0},
    max_capital_loss_ordinary_offset={"single": 0},
)


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def ref(agent_id: str, account_id: str = CHECKING) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=account_id)


def account(agent_id: str, account_id: str = CHECKING, balance: Decimal | int = 0) -> PreparedAccount:
    return PreparedAccount(account=ref(agent_id, account_id), opening_balance=money(balance))


def flat(asset: SecurityKey, price: Decimal | int, *, snapshots: int) -> PreparedSeries:
    return PreparedSeries(series_id=f"security:{asset.symbol}", snapshots=snapshots, values=(money(price),) * snapshots)


def distribution_rate(asset: SecurityKey, amount: Decimal | int, *, snapshots: int) -> PreparedSeries:
    """A per-unit payout, in the prepared rate units: money quanta on the money-factor grid."""
    return PreparedSeries(
        series_id=f"security_distribution:{asset.symbol}",
        snapshots=snapshots,
        values=(money(amount) * MONEY_FACTOR_SCALE,) * snapshots,
    )


def pool(asset: SecurityKey) -> PreparedHoldingPool:
    return PreparedHoldingPool(agent_id=ALICE, account_id=BROKERAGE, asset_id=str(asset.symbol), quantity_scale=SCALE)


def lot(asset: SecurityKey, *, units: float, basis: Decimal | int, purchase_month: int = -24) -> PreparedLot:
    return PreparedLot(
        lot_id=f"opening-{asset.symbol}",
        agent_id=ALICE,
        account_id=BROKERAGE,
        asset_id=str(asset.symbol),
        purchase_month=purchase_month,
        quantity_scale=SCALE,
        units=int(units * SCALE),
        basis=money(basis),
    )


def claim(month: int, amount: Decimal | int, identifier: str = "spending") -> PreparedObligation:
    return PreparedObligation(
        month=month,
        obligation_id=identifier,
        obligation_type="cash_spend",
        from_account=ref(ALICE),
        to_account=ref(WORLD),
        amount_due=money(amount),
        property_id=None,
        deduction_category=None,
        deductible_fraction_ppb=1_000_000_000,
    )


def allocation(
    *assets: SecurityKey,
    account_id: str = CHECKING,
    prefix: str = "fund",
    purchases: bool = False,
    zero_exit: bool = False,
    floor: int = 0,
    ceiling: int = 0,
) -> _AllocationPolicy:
    return _AllocationPolicy(
        agent_id=ALICE,
        account_id=account_id,
        source_account_ids=(BROKERAGE,),
        sleeves=tuple(
            _SleeveTarget(asset_id=str(asset.symbol), weight=0 if zero_exit and index == 0 else 1, quantity_scale=SCALE)
            for index, asset in enumerate(assets)
        ),
        cash_floor=floor,
        cash_ceiling=ceiling,
        cause_id_prefix=prefix,
        allow_purchases=purchases,
        rebalance_tolerance_ppb=0 if zero_exit else None,
    )


@dataclass(frozen=True)
class Situation:
    """The books, counterparties and funding policies every path declares."""

    horizon_months: int
    series: tuple[PreparedSeries, ...]
    accounts: tuple[PreparedAccount, ...]
    policies: tuple[_AllocationPolicy, ...]
    pools: tuple[PreparedHoldingPool, ...] = ()
    lots: tuple[PreparedLot, ...] = ()
    distributions: tuple[PreparedDistribution, ...] = ()
    claims: tuple[PreparedObligation | PreparedRecurringObligation, ...] = ()
    transfers: tuple[PreparedTransfer, ...] = ()
    scheduled_sales: tuple[_ScheduledSale, ...] = ()
    interest_sources: tuple[InterestIncome, ...] = ()
    taxed: bool = True
    rollout_count: int = 1


def compose(case: Situation, rollout_id: int) -> World:
    world = World(
        MarketPath(case.series, rollout_id, rollout_count=case.rollout_count),
        horizon_months=case.horizon_months,
        income_sources=(ORDINARY_INCOME, *case.interest_sources),
        jurisdictions=(PreparedJurisdiction(jurisdiction_id=SYNTHETIC, level=TAX.level),) if case.taxed else (),
    )
    for opening in case.accounts:
        world.declare_account(opening)
    if case.taxed:
        world.track(
            TaxAuthority(
                compile_profile(
                    TaxProfile(agent_id=ALICE, jurisdiction_ids=[SYNTHETIC], tax_authority_agent_id=WORLD),
                    {SYNTHETIC: TAX},
                    quantum=QUANTUM,
                )
            )
        )
    for holding_pool in case.pools:
        world.declare_pool(holding_pool)
    for holding in case.lots:
        world.hold(holding)
    for distribution in case.distributions:
        world.declare_distribution(distribution)
    world.scheduled_transfers = case.transfers
    for obligation in case.claims:
        world.track(Biller(obligation))
    world.track(ConfiguredHousehold(AgentId(ALICE), case.policies, scheduled_sales=case.scheduled_sales))
    return world


def step_to_horizon(world: World, recorder: FinancialCapture) -> None:
    while not world.finished:
        world.step()
        recorder.record()


def run(case: Situation) -> list[FinancialOutput]:
    outputs = []
    for rollout_id in range(case.rollout_count):
        world = compose(case, rollout_id)
        recorder = FinancialCapture(world, capture="forensic")
        world.start()
        step_to_horizon(world, recorder)
        output = recorder.financial()
        assert output is not None
        outputs.append(output)
    return outputs


def book(output: FinancialOutput, month: int) -> Book:
    return one(row for row in output.months if row.month == month)


def cash(output: FinancialOutput, month: int, agent_id: str = ALICE, account_id: str = CHECKING) -> int:
    return one(row.balance for row in book(output, month).balances if row.account == ref(agent_id, account_id))


def lots(output: FinancialOutput, month: int, *, purchased_in: int | None = None) -> list[SecurityLotState]:
    return [row for row in book(output, month).lots if purchased_in is None or row.purchase_month == purchased_in]


def spending_paid(output: FinancialOutput) -> int:
    return sum(row.amount_paid for row in output.obligations if row.obligation_type == "cash_spend")


def base(*, purchases: bool = False, zero_exit: bool = False, single: bool = False) -> Situation:
    """One funding policy on a flat $10 market: a $500 bill now, a $50 bill and $100 a year later."""
    assets = (STOCK,) if single else (STOCK, SECOND)
    return Situation(
        horizon_months=13,
        series=tuple(flat(asset, 10, snapshots=14) for asset in assets),
        accounts=(account(ALICE, balance=100), account(WORLD, balance=100)),
        policies=(allocation(*assets, purchases=purchases, zero_exit=zero_exit),),
        pools=tuple(pool(asset) for asset in assets),
        lots=tuple(lot(asset, units=100 // len(assets), basis=500 // len(assets)) for asset in assets),
        claims=(claim(0, 500), claim(12, 50)),
        transfers=()
        if zero_exit
        else (
            PreparedTransfer(
                month=12,
                cause_id="contribution",
                from_account=ref(WORLD),
                to_account=ref(ALICE),
                amount=money(100),
                income_category=None,
                deduction_category=None,
            ),
        ),
    )


@pytest.mark.parametrize("purchases", [False, True])
@pytest.mark.parametrize("single", [False, True])
def test_configured_funding_preserves_tax_year_claims_and_surplus(purchases: bool, single: bool) -> None:
    [output] = run(base(purchases=purchases, single=single))
    assert [(row.month, row.total_tax) for row in output.tax_accruals] == [(11, 2000)]
    assert [(row.month, row.amount) for row in output.tax_settlements] == [(12, 2000)]
    assert sum(row.proceeds for row in output.dispositions) == 40_000
    assert sum(row.basis for row in output.dispositions) == 20_000
    assert spending_paid(output) == 55_000
    assert cash(output, 13) == (0 if purchases else 3000)
    bought = lots(output, 13, purchased_in=12)
    assert sum(row.basis_remaining for row in bought) == (3000 if purchases else 0)
    if single and purchases:
        assert [row.units_remaining for row in bought] == [3 * SCALE]
    assert output.rollout_failures == []


def test_zero_target_partial_raise_then_full_exit_and_later_tax_funding() -> None:
    [output] = run(base(purchases=True, zero_exit=True))
    assert [(row.month, row.proceeds) for row in output.dispositions] == [(0, 40_000), (1, 10_000), (12, 7500)]
    assert [row.units_remaining for row in lots(output, 1) if row.lot_id == "opening-stock"] == [10 * SCALE]
    assert [row.units_remaining for row in lots(output, 2) if row.lot_id == "opening-stock"] == [0]
    assert [(row.month, row.amount) for row in output.tax_settlements] == [(12, 2500)]
    assert spending_paid(output) == 55_000
    assert output.rollout_failures == []


@pytest.mark.parametrize("spending", [300, 500])
def test_group_failure_does_not_undo_prior_funding_sales(spending: int) -> None:
    case = base(single=True)
    [output] = run(
        replace(
            case,
            horizon_months=1,
            series=(flat(STOCK, 10, snapshots=2),),
            transfers=(),
            claims=(claim(0, 700, "rent"), claim(0, spending)),
            taxed=False,
        )
    )
    funded = spending == 300
    assert sum(row.proceeds for row in output.dispositions) == (90_000 if funded else 100_000)
    assert sum(row.amount_paid for row in output.obligations) == (100_000 if funded else 0)
    assert (output.rollout_failures == []) == funded
    assert output.months[-1].month == 1


def test_indexed_monthly_claims_keep_sales_and_next_year_tax_events() -> None:
    case = base(single=True)
    [output] = run(
        replace(
            case,
            series=(*case.series, PreparedSeries(series_id="inflation", snapshots=14, values=(1,) * 12 + (2,) * 2)),
            accounts=(account(ALICE), account(WORLD)),
            transfers=(),
            claims=(
                PreparedRecurringObligation(
                    start_month=0,
                    end_month=None,
                    obligation_id="indexed",
                    obligation_type="cash_spend",
                    from_account=ref(ALICE),
                    to_account=ref(WORLD),
                    amount_due=PreparedIndexedAmount(
                        base_amount=money(10), series_id="inflation", base_month_index=0, adjustment_period_months=1
                    ),
                    property_id=None,
                    deduction_category=None,
                    deductible_fraction_ppb=1_000_000_000,
                ),
            ),
        )
    )
    paid = sorted((row for row in output.obligations if row.obligation_type == "cash_spend"), key=lambda row: row.month)
    assert [row.amount_paid for row in paid] == [1000] * 12 + [2000]
    assert [row.total_tax for row in output.tax_accruals] == [600]
    assert [(row.month, row.amount) for row in output.tax_settlements] == [(12, 600)]
    assert output.rollout_failures == []


def test_empty_buyable_pool_pays_coupon_only_after_first_purchase() -> None:
    case = base(purchases=True, single=True)
    [output] = run(
        replace(
            case,
            horizon_months=2,
            series=(flat(STOCK, 100, snapshots=3), distribution_rate(STOCK, 1, snapshots=3)),
            accounts=(account(ALICE, balance=200), account(WORLD)),
            lots=(),
            transfers=(),
            claims=(),
            taxed=False,
            distributions=(
                PreparedDistribution(
                    agent_id=ALICE,
                    holding_account_id=BROKERAGE,
                    asset_id=str(STOCK.symbol),
                    to_account_id=CHECKING,
                    tax_character=(PreparedDistributionSlice(fraction_ppb=1_000_000_000, issuer_jurisdiction_id=None),),
                ),
            ),
            interest_sources=(InterestIncome(issuer_jurisdiction_id=None),),
        )
    )
    first = lots(output, 1)
    assert [row.units_remaining for row in first] == [2 * SCALE]
    assert [row.basis_remaining for row in first] == [20_000]
    # The second month's $2 payout is reinvested, not a first-month entitlement.
    assert [row.basis_remaining for row in lots(output, 2, purchased_in=1)] == [200]


def test_fifo_across_two_policy_purchase_dates_preserves_basis_and_tax_character() -> None:
    case = base(purchases=True, single=True)
    [output] = run(
        replace(
            case,
            series=(
                PreparedSeries(
                    series_id=f"security:{STOCK.symbol}",
                    snapshots=14,
                    values=(money(100), money(150)) + (money(200),) * 12,
                ),
            ),
            accounts=(
                account(ALICE),
                account(WORLD, balance=300),
                account(ALICE, "early-cash", balance=200),
                account(ALICE, "proceeds"),
            ),
            lots=(),
            claims=(),
            policies=(
                allocation(STOCK, purchases=True),
                allocation(STOCK, account_id="early-cash", prefix="early", purchases=True),
            ),
            transfers=(
                PreparedTransfer(
                    month=1,
                    cause_id="later",
                    from_account=ref(WORLD),
                    to_account=ref(ALICE),
                    amount=money(300),
                    income_category=None,
                    deduction_category=None,
                ),
            ),
            scheduled_sales=(
                _ScheduledSale(
                    month=12,
                    cause_id="fifo-sale",
                    agent_id=ALICE,
                    account_id=BROKERAGE,
                    asset_id=str(STOCK.symbol),
                    units=3 * SCALE,
                    proceeds_account_id="proceeds",
                ),
            ),
        )
    )
    assert [(row.purchase_month, row.units, row.basis, row.proceeds) for row in output.dispositions] == [
        (0, 2 * SCALE, 20_000, 40_000),
        (1, 1 * SCALE, 15_000, 20_000),
    ]
    gains = one(row for row in book(output, 13).capital_gains if row.agent_id == ALICE)
    assert (gains.long_term_gain, gains.short_term_gain) == (20_000, 5000)


def indexed_bounds() -> Situation:
    """One-quantum shares and a three/four-quantum band, so half ties are hand-checkable."""
    case = base(purchases=True, single=True)
    floor = PreparedIndexedAmount(base_amount=3, series_id="inflation", base_month_index=0, adjustment_period_months=2)
    return replace(
        case,
        horizon_months=6,
        rollout_count=2,
        series=(
            PreparedSeries(series_id=f"security:{STOCK.symbol}", snapshots=7, values=(1,) * 14),
            PreparedSeries(series_id="inflation", snapshots=7, values=(2, 3, 5, 7, 11, 13, 19, 4, 1, 2, 99, 6, 88, 40)),
        ),
        accounts=(account(ALICE), account(WORLD)),
        lots=(lot(STOCK, units=100, basis=1),),
        claims=(),
        transfers=(),
        taxed=False,
        policies=(replace(case.policies[0], cash_floor=floor, cash_ceiling=replace(floor, base_amount=4)),),
    )


def test_indexed_bounds_keep_exact_cash_across_path_specific_resets() -> None:
    # Between-reset index changes must not affect either path's funding decisions.
    outputs = run(indexed_bounds())
    expected_cash = ([0, 4, 4, 10, 10, 22, 22], [0, 4, 4, 2, 2, 6, 6])
    for output, expected in zip(outputs, expected_cash, strict=True):
        assert output.failed_month is None
        assert [cash(output, snapshot.month) for snapshot in output.months] == expected
        for snapshot, balance in zip(output.months, expected, strict=True):
            assert balance + sum(row.units_remaining // row.quantity_scale for row in snapshot.lots) == 100
        assert all(sum(posting.amount for posting in entry.postings) == 0 for entry in output.journal)


def test_indexed_bound_overflow_remains_an_error_not_a_financial_stop() -> None:
    case = indexed_bounds()
    index = PreparedIndexedAmount(
        base_amount=MAX_COUNT, series_id="inflation", base_month_index=0, adjustment_period_months=2
    )
    world = compose(
        replace(
            case,
            accounts=(PreparedAccount(account=ref(ALICE), opening_balance=MAX_COUNT), account(WORLD)),
            lots=(),
            policies=(replace(case.policies[0], cash_floor=index, cash_ceiling=index),),
        ),
        0,
    )
    world.start()
    with pytest.raises(OverflowError, match=r"overflow|signed 64"):
        step_to_horizon(world, FinancialCapture(world, capture="forensic"))


if __name__ == "__main__":
    pytest_bazel.main()
