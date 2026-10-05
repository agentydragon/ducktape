"""Allocation funding, ordered claim payments and exact purchases in one household's batch.

The tax schedule below is deliberately synthetic: 20% ordinary, 10% long-term,
no deductions. Assertions pin accounting/timing, not statutory fidelity.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import partial

import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.policy.cash_band_household import CashBandHousehold, CpiIndexed, Reinvest, SecuritySleeve
from finance.augur.sim.actions import LotSale, Sell
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Book, SecurityLotState
from finance.augur.sim.capture import FinancialCapture, FinancialOutput
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE, quantity_scale_for_asset
from finance.augur.sim.ids import AccountId, AgentId, AssetId, LotId
from finance.augur.sim.income import ORDINARY_INCOME, InterestIncome, Taxable
from finance.augur.sim.jurisdictions import HYPOTHETICAL_FLAT_TAX, flat_income_tax
from finance.augur.sim.market_path import Amount, IndexedAmount, MarketPath, Series
from finance.augur.sim.money import MAX_COUNT, USD
from finance.augur.sim.schedule import Once, Recurring, Schedule
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tax_indexation import FixedNominalLaw
from finance.augur.sim.tax_profile import TaxProfile, compile_profile
from finance.augur.sim.testing.scripted import Scripted
from finance.augur.sim.world import World

STOCK = SecurityKey(symbol=SecuritySymbol("stock"))
SECOND = SecurityKey(symbol=SecuritySymbol("second"))
SCALE = quantity_scale_for_asset(STOCK)
ALICE = AgentId("alice")
WORLD = AgentId("world")
CHECKING = AccountId("checking")
BROKERAGE = AccountId("brokerage")
TAX = flat_income_tax(HYPOTHETICAL_FLAT_TAX, ordinary_rate=Decimal("0.20"), ltcg_rate=Decimal("0.10"))


def ref(agent_id: AgentId, account_id: AccountId = CHECKING) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=account_id)


def account(agent_id: AgentId, account_id: AccountId = CHECKING, balance: Decimal | int = 0) -> tuple[AccountRef, int]:
    """An account and its opening balance."""
    return ref(agent_id, account_id), USD.quanta(balance)


def flat(asset: SecurityKey, price: Decimal | int, *, snapshots: int) -> Series:
    return Series(series_id=f"security:{asset.symbol}", snapshots=snapshots, values=(USD.quanta(price),) * snapshots)


def distribution_rate(asset: SecurityKey, amount: Decimal | int, *, snapshots: int) -> Series:
    """A per-unit payout, in the prepared rate units: money quanta on the money-factor grid."""
    return Series(
        series_id=f"security_distribution:{asset.symbol}",
        snapshots=snapshots,
        values=(USD.quanta(amount) * MONEY_FACTOR_SCALE,) * snapshots,
    )


@dataclass(frozen=True)
class OpeningLot:
    """Alice's whole shares of `asset` in brokerage, bought two years before the path starts."""

    asset: SecurityKey
    shares: int
    basis: Decimal | int


@dataclass(frozen=True)
class Spending:
    """A cash-spend bill Alice owes the world."""

    obligation_id: str
    schedule: Schedule
    amount_due: Amount


def claim(month: int, amount: Decimal | int, identifier: str = "spending") -> Spending:
    return Spending(identifier, Once(month=month), USD.quanta(amount))


@dataclass(frozen=True)
class Contribution:
    """A one-off payment from the world into Alice's checking, in dollars."""

    cause_id: str
    month: int
    amount: Decimal | int


def allocation(*assets: SecurityKey, purchases: bool, zero_exit: bool) -> partial[CashBandHousehold]:
    """An equal-weight band at zero on `assets`; with `zero_exit`, the first is a zero-weight sleeve drift exits."""
    if zero_exit and not purchases:
        raise ValueError("a drift exit rebalances, which needs purchases")
    return partial(
        CashBandHousehold,
        ALICE,
        cash_account_id=CHECKING,
        floor=0,
        ceiling=0,
        sleeves=tuple(
            SecuritySleeve(asset_id=AssetId(asset.symbol), weight=0 if zero_exit and index == 0 else 1)
            for index, asset in enumerate(assets)
        ),
        source_account_ids=(BROKERAGE,),
        reinvest=Reinvest(rebalance_tolerance_ppb=0 if zero_exit else None) if purchases else None,
        cause_id_prefix="fund",
    )


@dataclass(frozen=True)
class Situation:
    """The books, counterparties and funding household every path declares."""

    horizon_months: int
    series: tuple[Series, ...]
    accounts: tuple[tuple[AccountRef, int], ...]
    # A fresh household per path: it keeps the path's CPI history and purchase identities.
    funding: partial[CashBandHousehold]
    # The securities Alice's brokerage holds a pool of, whether or not she holds any yet.
    pools: tuple[SecurityKey, ...] = ()
    lots: tuple[OpeningLot, ...] = ()
    # The securities whose payouts land in Alice's checking as interest.
    distributions: tuple[SecurityKey, ...] = ()
    claims: tuple[Spending, ...] = ()
    transfers: tuple[Contribution, ...] = ()
    sales: Mapping[int, tuple[Sell, ...]] = field(default_factory=dict)
    interest_sources: tuple[InterestIncome, ...] = ()
    taxed: bool = True
    rollout_count: int = 1


def compose(case: Situation, rollout_id: int) -> World:
    world = World(
        MarketPath(case.series, rollout_id, rollout_count=case.rollout_count),
        horizon_months=case.horizon_months,
        income_sources=(ORDINARY_INCOME, *case.interest_sources),
    )
    for opened, balance in case.accounts:
        world.declare_account(account=opened, opening_balance=balance)
    if case.taxed:
        world.track(
            TaxAuthority(
                compile_profile(
                    TaxProfile(agent_id=ALICE, jurisdiction_ids=[HYPOTHETICAL_FLAT_TAX], tax_authority_agent_id=WORLD),
                    {HYPOTHETICAL_FLAT_TAX: TAX},
                    currency=USD,
                ),
                indexation=FixedNominalLaw(),
            )
        )
    for asset in case.pools:
        world.declare_pool(agent_id=ALICE, account_id=BROKERAGE, asset_id=AssetId(asset.symbol), quantity_scale=SCALE)
    for held in case.lots:
        world.hold_lot(
            lot_id=LotId(f"opening-{held.asset.symbol}"),
            agent_id=ALICE,
            account_id=BROKERAGE,
            asset_id=AssetId(held.asset.symbol),
            purchase_month=-24,
            quantity_scale=SCALE,
            units=held.shares * SCALE,
            basis=USD.quanta(held.basis),
        )
    for asset in case.distributions:
        world.declare_distribution(
            agent_id=ALICE,
            holding_account_id=BROKERAGE,
            asset_id=AssetId(asset.symbol),
            to_account_id=CHECKING,
            tax_character={InterestIncome(character=Taxable()): 1_000_000_000},
        )
    for contribution in case.transfers:
        world.declare_flow(
            schedule=Once(month=contribution.month),
            cause_id=contribution.cause_id,
            from_account=ref(WORLD),
            to_account=ref(ALICE),
            amount=USD.quanta(contribution.amount),
            income_category=None,
            deduction_category=None,
        )
    for bill in case.claims:
        world.track(
            Biller(
                schedule=bill.schedule,
                obligation_id=bill.obligation_id,
                obligation_type="cash_spend",
                from_account=ref(ALICE),
                to_account=ref(WORLD),
                amount_due=bill.amount_due,
                property_id=None,
                deduction_category=None,
                deductible_fraction_ppb=1_000_000_000,
            )
        )
    world.track(Scripted(case.funding(), case.sales))
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
        outputs.append(output)
    return outputs


def book(output: FinancialOutput, month: int) -> Book:
    return one(row for row in output.months if row.month == month)


def cash(output: FinancialOutput, month: int, agent_id: AgentId = ALICE, account_id: AccountId = CHECKING) -> int:
    return one(row.balance for row in book(output, month).balances if row.account == ref(agent_id, account_id))


def lots(output: FinancialOutput, month: int, *, purchased_in: int | None = None) -> list[SecurityLotState]:
    return [row for row in book(output, month).lots if purchased_in is None or row.purchase_month == purchased_in]


def spending_paid(output: FinancialOutput) -> int:
    return sum(row.amount_paid for row in output.obligations if row.obligation_type == "cash_spend")


def base(*, purchases: bool = False, zero_exit: bool = False, single: bool = False) -> Situation:
    """One funding household on a flat $10 market: a $500 bill now, a $50 bill and $100 a year later."""
    assets = (STOCK,) if single else (STOCK, SECOND)
    return Situation(
        horizon_months=13,
        series=tuple(flat(asset, 10, snapshots=14) for asset in assets),
        accounts=(account(ALICE, balance=100), account(WORLD, balance=100)),
        funding=allocation(*assets, purchases=purchases, zero_exit=zero_exit),
        pools=assets,
        lots=tuple(OpeningLot(asset, shares=100 // len(assets), basis=500 // len(assets)) for asset in assets),
        claims=(claim(0, 500), claim(12, 50)),
        transfers=() if zero_exit else (Contribution("contribution", month=12, amount=100),),
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
def test_rejected_payment_does_not_undo_prior_funding_sales_or_payments(spending: int) -> None:
    """$100 cash and $1,000 of stock against $700 rent then `spending`: at $500 the rent is paid first."""
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
    assert [(row.amount_due, row.amount_paid) for row in output.obligations] == [
        (70_000, 70_000),
        (spending * 100, spending * 100 if funded else 0),
    ]
    assert output.failed_month == (None if funded else 0)
    assert output.months[-1].month == 1


def test_indexed_monthly_claims_keep_sales_and_next_year_tax_events() -> None:
    case = base(single=True)
    [output] = run(
        replace(
            case,
            series=(*case.series, Series(series_id="inflation", snapshots=14, values=(1,) * 12 + (2,) * 2)),
            accounts=(account(ALICE), account(WORLD)),
            transfers=(),
            claims=(
                Spending(
                    "indexed",
                    Recurring(start_month=0, end_month=None),
                    IndexedAmount(
                        base_amount=USD.quanta(10),
                        series_id="inflation",
                        base_month_index=0,
                        adjustment_period_months=1,
                    ),
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
            distributions=(STOCK,),
            interest_sources=(InterestIncome(character=Taxable()),),
        )
    )
    first = lots(output, 1)
    assert [row.units_remaining for row in first] == [2 * SCALE]
    assert [row.basis_remaining for row in first] == [20_000]
    # The second month's $2 payout is reinvested, not a first-month entitlement.
    assert [row.basis_remaining for row in lots(output, 2, purchased_in=1)] == [200]


def test_fifo_across_two_purchase_dates_preserves_basis_and_tax_character() -> None:
    case = base(purchases=True, single=True)
    [output] = run(
        replace(
            case,
            series=(
                Series(
                    series_id=f"security:{STOCK.symbol}",
                    snapshots=14,
                    values=(USD.quanta(100), USD.quanta(150)) + (USD.quanta(200),) * 12,
                ),
            ),
            accounts=(account(ALICE, balance=200), account(WORLD, balance=300), account(ALICE, AccountId("proceeds"))),
            lots=(),
            claims=(),
            transfers=(Contribution("later", month=1, amount=300),),
            sales={
                12: (
                    Sell(
                        cause_id="fifo-sale",
                        agent_id=ALICE,
                        proceeds_account_id=AccountId("proceeds"),
                        asset_id=AssetId(STOCK.symbol),
                        lots=(
                            LotSale(account_id=BROKERAGE, lot_id=LotId("fund_buy_s0_0"), units=2 * SCALE),
                            LotSale(account_id=BROKERAGE, lot_id=LotId("fund_buy_s0_1"), units=1 * SCALE),
                        ),
                    ),
                )
            },
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
    return replace(
        case,
        horizon_months=6,
        rollout_count=2,
        series=(
            Series(series_id=f"security:{STOCK.symbol}", snapshots=7, values=(1,) * 14),
            Series(series_id="inflation", snapshots=7, values=(2, 3, 5, 7, 11, 13, 19, 4, 1, 2, 99, 6, 88, 40)),
        ),
        accounts=(account(ALICE), account(WORLD)),
        lots=(OpeningLot(STOCK, shares=100, basis=1),),
        claims=(),
        transfers=(),
        taxed=False,
        funding=partial(
            case.funding,
            floor=CpiIndexed(base_amount=3, adjustment_period_months=2),
            ceiling=CpiIndexed(base_amount=4, adjustment_period_months=2),
        ),
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
    index = CpiIndexed(base_amount=MAX_COUNT, adjustment_period_months=2)
    world = compose(
        replace(
            case,
            accounts=((ref(ALICE), MAX_COUNT), account(WORLD)),
            lots=(),
            funding=partial(case.funding, floor=index, ceiling=index),
        ),
        0,
    )
    world.start()
    with pytest.raises(OverflowError):
        step_to_horizon(world, FinancialCapture(world, capture="forensic"))


if __name__ == "__main__":
    pytest_bazel.main()
