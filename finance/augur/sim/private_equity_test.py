"""What a private-equity position does when the issuer offers, forces, or blocks a sale.

A PE holding is the one asset whose sales the holder does not initiate. The issuer's protocol
decides whether a sale is possible at all — a tender window, a public-market regime, a forced
redemption — and the holder's policy decides only whether to take it, by whether liquid net
worth has fallen through a floor. So every case here fixes the protocol channels and reads
back what the position and the cash did, which is the only way the two halves can be told
apart: a sale that should have been capacity-limited and one that should never have been
offered both end with units still held.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.asset_key import PrivateEquityAssetKey
from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.sim.actions import DecisionActions, PayClaim
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Book
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.fixed_point import (
    currency_amount_to_quanta,
    quantity_scale_for_asset,
    quantity_to_quanta,
    rate_to_ppb,
)
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedJurisdiction,
    PreparedLot,
    PreparedRecurringObligation,
    PreparedSeries,
    _TenderPolicy,
)
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.scenario import ORDINARY_INCOME, ObligationType, TaxProfile
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.testing.issuer_protocol import Code, Money, Rate, at_month, issuer_protocol
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
QUANTA_PER_UNIT = 100
ALICE = "alice"
SPEND_SINK = "spend_sink"
IRS = "irs"
CHECKING = "checking"
SAVINGS = "savings"
FEDERAL = "federal_us"
ISSUER = "acme"
ASSET_ID = "private_equity:acme"
LOT_ID = "acme_lot_a"
ACME = PrivateEquityAssetKey(issuer_id=IssuerId(ISSUER))
SCALE = quantity_scale_for_asset(ACME)


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def account(agent_id: str, account_id: str = CHECKING, balance: Decimal | int = 0) -> PreparedAccount:
    return PreparedAccount(account=AccountRef(agent_id=agent_id, account_id=account_id), opening_balance=money(balance))


def protocol(
    *,
    horizon_months: int,
    initial_mark: Decimal,
    tender_month: int | None = None,
    tender_mark: Decimal | None = None,
    regime: Code = PrivateEquityRegimeCode.PRIVATE_OPERATING,
    sale_capacity: Rate = 1.0,
    eligible: Rate = 1.0,
    forced_sale: Rate = 0.0,
    liquidity_blocked: Code = 0,
    forced_recovery_usd: Money = Decimal(0),
) -> tuple[PreparedSeries, ...]:
    """A flat mark that steps at `tender_month`, and a tender window open only in that month."""

    snapshots = horizon_months + 1
    marks = [initial_mark] * snapshots
    window = [0] * snapshots
    if tender_month is not None and tender_mark is not None:
        marks[tender_month:] = [tender_mark] * (snapshots - tender_month)
        window[tender_month] = 1
    return issuer_protocol(
        ISSUER,
        horizon_months=horizon_months,
        mark_usd=marks,
        regime=regime,
        event_kind=[
            int(PrivateEquityEventKindCode.TENDER if open_window else PrivateEquityEventKindCode.NONE)
            for open_window in window
        ],
        sale_opportunity=window,
        sale_capacity=sale_capacity,
        eligible=eligible,
        forced_sale=forced_sale,
        liquidity_blocked=liquidity_blocked,
        forced_recovery_usd=forced_recovery_usd,
    )


@dataclass(frozen=True)
class Holder:
    """Alice holds one acme position, spends monthly, and sells PE only to hold a floor.

    No income and no other holding, so liquid net worth is her cash alone and the floor
    shortfall a tender has to close is arithmetic the case can state. `floor` is `None` for an
    owner who gave the issuer no standing answer at all.
    """

    horizon_months: int
    accounts: tuple[PreparedAccount, ...]
    lot: PreparedLot
    monthly_spend: int
    floor: int | None
    taxed: bool


def holder(
    *,
    initial_cash: Decimal | int,
    monthly_spend: Decimal | int,
    pe_units: float,
    pe_cost_basis_per_unit: Decimal | int,
    pe_holding_period_months: int,
    horizon_months: int,
    lnw_floor: Decimal | int | None,
    taxed: bool = False,
) -> Holder:
    return Holder(
        horizon_months=horizon_months,
        accounts=(account(ALICE, balance=initial_cash), account(SPEND_SINK), *((account(IRS),) if taxed else ())),
        lot=PreparedLot(
            lot_id=LOT_ID,
            agent_id=ALICE,
            account_id=CHECKING,
            asset_id=ASSET_ID,
            purchase_month=-pe_holding_period_months,
            quantity_scale=SCALE,
            units=int(quantity_to_quanta(pe_units, scale=SCALE)),
            basis=money(Decimal(str(pe_units)) * pe_cost_basis_per_unit),
        ),
        monthly_spend=money(monthly_spend),
        floor=None if lnw_floor is None else money(lnw_floor),
        taxed=taxed,
    )


def compose(case: Holder, channels: Sequence[PreparedSeries]) -> World:
    jurisdictions = {FEDERAL: load_jurisdiction(FEDERAL)} if case.taxed else {}
    world = World(
        MarketPath(channels, 0, rollout_count=1),
        horizon_months=case.horizon_months,
        income_sources=(ORDINARY_INCOME,),
        jurisdictions=tuple(
            PreparedJurisdiction(jurisdiction_id=id_, level=jurisdiction.level)
            for id_, jurisdiction in jurisdictions.items()
        ),
    )
    for opening in case.accounts:
        world.declare_account(opening)
    if case.taxed:
        world.track(
            TaxAuthority(
                compile_profile(
                    TaxProfile(agent_id=ALICE, jurisdiction_ids=[FEDERAL], tax_authority_agent_id=IRS),
                    jurisdictions,
                    quantum=QUANTUM,
                )
            )
        )
    world.declare_pool(
        PreparedHoldingPool(agent_id=ALICE, account_id=CHECKING, asset_id=ASSET_ID, quantity_scale=SCALE)
    )
    world.hold(case.lot)
    if case.floor is not None:
        world.declare_tender_policy(
            _TenderPolicy(owner_agent_id=ALICE, proceeds_account_id=CHECKING, liquid_net_worth_floor=case.floor)
        )
    world.track(
        Biller(
            PreparedRecurringObligation(
                start_month=0,
                end_month=case.horizon_months - 1,
                obligation_id="monthly_spend",
                obligation_type=ObligationType.CASH_SPEND,
                from_account=AccountRef(agent_id=ALICE, account_id=CHECKING),
                to_account=AccountRef(agent_id=SPEND_SINK, account_id=CHECKING),
                amount_due=case.monthly_spend,
                property_id=None,
                deduction_category=None,
                deductible_fraction_ppb=rate_to_ppb(1.0),
            )
        )
    )
    return world


def run(case: Holder, channels: Sequence[PreparedSeries]) -> Rollout:
    """Alice pays her monthly spend and nothing else; the issuer protocol runs as the month closes."""

    session = ActionSession({0: compose(case, channels)}, ALICE)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(
                [
                    DecisionActions(
                        decision.rollout_id,
                        decision.observation.month,
                        [
                            PayClaim(
                                request_id=index + 1,
                                cause_id=claim.cause_id,
                                claim=claim,
                                from_account=claim.from_account,
                                amount=claim.amount_due,
                            )
                            for index, claim in enumerate(decision.observation.claims)
                        ],
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


def units_held(rollout: Rollout, *, month: int) -> float:
    [lot] = [lot for lot in book(rollout, month).lots if lot.lot_id == LOT_ID]
    return lot.units_remaining / lot.quantity_scale


def balances(rollout: Rollout, case: Holder, *, month: int) -> dict[str, int]:
    """Alice's declared cash accounts; the books also carry the internal ones."""

    declared = {opening.account for opening in case.accounts}
    return {
        row.account.account_id: row.balance
        for row in book(rollout, month).balances
        if row.account in declared and row.account.agent_id == ALICE
    }


def cash(rollout: Rollout, case: Holder, *, month: int) -> float:
    return sum(balances(rollout, case, month=month).values()) / QUANTA_PER_UNIT


def opportunity(rollout: Rollout, *, month: int) -> dict[str, object]:
    assert rollout.trace is not None
    return rollout.trace.events.private_equity_opportunities.filter(pl.col("month_index") == month).row(0, named=True)


def dispositions(rollout: Rollout, *, month: int) -> pl.DataFrame:
    assert rollout.trace is not None
    return rollout.trace.events.lot_dispositions.filter(pl.col("month_index") == month)


def test_a_position_with_no_opportunity_carries_through_untouched() -> None:
    """No tender, no forced sale: a floor far above liquid net worth changes nothing.

    The floor is deliberately unreachable, so this separates "the policy wanted to sell"
    from "the protocol let it" — only the second is missing here.
    """

    horizon = 24
    case = holder(
        initial_cash=100_000,
        monthly_spend=0,
        pe_units=100.0,
        pe_cost_basis_per_unit=10,
        pe_holding_period_months=36,
        horizon_months=horizon,
        lnw_floor=500_000,
    )
    rollout = run(case, protocol(horizon_months=horizon, initial_mark=Decimal(50)))

    assert units_held(rollout, month=horizon) == pytest.approx(100.0)
    assert cash(rollout, case, month=horizon) == pytest.approx(100_000.0)


def test_a_tender_below_the_floor_sells_toward_it() -> None:
    """The whole position goes when it is worth less than the shortfall.

    Cash at the tender is 30k less six months of 1k spend = 24k, against a 50k floor, so
    the shortfall is 26k and the position is worth 100 x $60 = $6k. Selling all of it
    still leaves her under the floor, which is the point: the policy sells what it can,
    it does not fail when that is not enough.
    """

    horizon, tender_month = 12, 5
    case = holder(
        initial_cash=Decimal(30_000),
        monthly_spend=Decimal(1_000),
        pe_units=100.0,
        pe_cost_basis_per_unit=10,
        pe_holding_period_months=36,
        horizon_months=horizon,
        lnw_floor=Decimal(50_000),
    )
    rollout = run(
        case,
        protocol(horizon_months=horizon, initial_mark=Decimal(50), tender_month=tender_month, tender_mark=Decimal(60)),
    )

    assert units_held(rollout, month=tender_month + 1) == pytest.approx(0.0, abs=0.02)
    assert cash(rollout, case, month=tender_month + 1) == pytest.approx(30_000.0, abs=1.0)

    [row] = dispositions(rollout, month=tender_month).iter_rows(named=True)
    assert row["asset_id"] == ASSET_ID
    assert row["units_sold"] == pytest.approx(100.0, abs=0.02)
    assert row["proceeds_quanta"] / QUANTA_PER_UNIT == pytest.approx(6_000.0, abs=1.0)
    assert row["cause_id"] == "pe_tender_m5_acme"

    assert rollout.trace is not None
    [marker] = rollout.trace.events.private_equity_events.filter(pl.col("month_index") == tender_month).iter_rows(
        named=True
    )
    assert marker["event_kind"] == "tender"
    assert marker["asset_id"] == ASSET_ID


def test_a_tender_above_the_floor_passes_without_a_sale() -> None:
    """An open window is not a reason to sell; a shortfall is."""

    horizon = 12
    case = holder(
        initial_cash=200_000,
        monthly_spend=0,
        pe_units=100.0,
        pe_cost_basis_per_unit=10,
        pe_holding_period_months=36,
        horizon_months=horizon,
        lnw_floor=50_000,
    )
    rollout = run(
        case, protocol(horizon_months=horizon, initial_mark=Decimal(50), tender_month=5, tender_mark=Decimal(60))
    )

    assert units_held(rollout, month=6) == pytest.approx(100.0)
    assert cash(rollout, case, month=6) == pytest.approx(200_000.0)
    traced = opportunity(rollout, month=5)
    assert traced["outcome"] == "floor_satisfied"
    assert traced["shortfall_quanta"] == 0


def test_a_zero_floor_never_sells() -> None:
    """Liquid net worth is always at or above a floor of nothing."""

    horizon = 12
    case = holder(
        initial_cash=1_000,
        monthly_spend=0,
        pe_units=100.0,
        pe_cost_basis_per_unit=10,
        pe_holding_period_months=36,
        horizon_months=horizon,
        lnw_floor=0,
    )
    rollout = run(
        case, protocol(horizon_months=horizon, initial_mark=Decimal(50), tender_month=5, tender_mark=Decimal(60))
    )

    assert units_held(rollout, month=6) == pytest.approx(100.0)


def test_a_tender_with_no_policy_is_not_taken() -> None:
    """The opportunity is still traced, so "nobody asked" is distinguishable from "refused"."""

    horizon = 12
    case = holder(
        initial_cash=30_000,
        monthly_spend=1_000,
        pe_units=100.0,
        pe_cost_basis_per_unit=10,
        pe_holding_period_months=36,
        horizon_months=horizon,
        lnw_floor=None,
    )
    rollout = run(
        case, protocol(horizon_months=horizon, initial_mark=Decimal(50), tender_month=5, tender_mark=Decimal(60))
    )

    assert units_held(rollout, month=6) == pytest.approx(100.0)
    assert opportunity(rollout, month=5)["outcome"] == "no_policy"


def unreachable_floor(*, horizon_months: int) -> Holder:
    """A floor no sale can close, so what is sold is whatever the protocol allows."""

    return holder(
        initial_cash=0,
        monthly_spend=0,
        pe_units=100.0,
        pe_cost_basis_per_unit=10,
        pe_holding_period_months=36,
        horizon_months=horizon_months,
        lnw_floor=1_000_000,
    )


def test_the_issuers_capacity_caps_what_a_tender_can_sell() -> None:
    """A quarter of the position is sellable, so a floor that wants all of it gets a quarter."""

    horizon = 12
    case = unreachable_floor(horizon_months=horizon)
    rollout = run(
        case,
        protocol(
            horizon_months=horizon,
            initial_mark=Decimal(100),
            tender_month=5,
            tender_mark=Decimal(100),
            sale_capacity=0.25,
        ),
    )

    assert units_held(rollout, month=6) == pytest.approx(75.0)
    assert cash(rollout, case, month=6) == pytest.approx(2_500.0)
    traced = opportunity(rollout, month=5)
    assert traced["outcome"] == "sold"
    assert traced["sellable_units"] == pytest.approx(25.0)
    assert traced["target_units"] == pytest.approx(25.0)
    assert traced["proceeds_quanta"] == 250_000


def test_zero_capacity_is_traced_as_its_own_outcome() -> None:
    """An open window with no capacity behind it is not the same as a satisfied floor."""

    horizon = 12
    rollout = run(
        unreachable_floor(horizon_months=horizon),
        protocol(
            horizon_months=horizon,
            initial_mark=Decimal(100),
            tender_month=5,
            tender_mark=Decimal(100),
            sale_capacity=0.0,
        ),
    )

    assert units_held(rollout, month=6) == pytest.approx(100.0)
    traced = opportunity(rollout, month=5)
    assert traced["outcome"] == "capacity_zero"
    assert traced["sellable_units"] == pytest.approx(0.0)
    assert traced["target_units"] == pytest.approx(0.0)


def test_the_eligible_fraction_caps_what_a_tender_can_sell() -> None:
    """Eligibility bites the same way capacity does, on a different channel."""

    horizon = 12
    case = unreachable_floor(horizon_months=horizon)
    rollout = run(
        case,
        protocol(
            horizon_months=horizon, initial_mark=Decimal(100), tender_month=5, tender_mark=Decimal(100), eligible=0.4
        ),
    )

    assert units_held(rollout, month=6) == pytest.approx(60.0)
    assert cash(rollout, case, month=6) == pytest.approx(4_000.0)


def test_a_liquidity_block_prevents_the_sale_entirely() -> None:
    """Blocked is its own outcome, and it zeroes the target rather than the proceeds."""

    horizon = 12
    case = unreachable_floor(horizon_months=horizon)
    rollout = run(
        case,
        protocol(
            horizon_months=horizon,
            initial_mark=Decimal(100),
            tender_month=5,
            tender_mark=Decimal(100),
            liquidity_blocked=1,
        ),
    )

    assert units_held(rollout, month=6) == pytest.approx(100.0)
    assert cash(rollout, case, month=6) == pytest.approx(0.0)
    traced = opportunity(rollout, month=5)
    assert traced["outcome"] == "liquidity_blocked"
    assert traced["liquidity_blocked"] is True
    assert traced["target_units"] == pytest.approx(0.0)


def test_a_public_market_regime_lets_the_floor_sell_with_no_tender() -> None:
    """Once the issuer trades publicly the holder no longer needs a window offered to them.

    A $5k floor against no cash sells exactly the 50 units that closes it, and the cause
    names the regime rather than a tender.
    """

    horizon = 12
    case = holder(
        initial_cash=0,
        monthly_spend=0,
        pe_units=100.0,
        pe_cost_basis_per_unit=10,
        pe_holding_period_months=36,
        horizon_months=horizon,
        lnw_floor=5_000,
    )
    rollout = run(
        case,
        protocol(
            horizon_months=horizon,
            initial_mark=Decimal(100),
            regime=at_month(
                int(PrivateEquityRegimeCode.PUBLIC_MARKET),
                month=5,
                default=int(PrivateEquityRegimeCode.PRIVATE_OPERATING),
                snapshots=horizon + 1,
            ),
        ),
    )

    assert units_held(rollout, month=6) == pytest.approx(50.0)
    assert cash(rollout, case, month=6) == pytest.approx(5_000.0)
    [row] = dispositions(rollout, month=5).iter_rows(named=True)
    assert row["cause_id"] == "pe_public_market_m5_acme"


def forced_sale_in_month(*, horizon_months: int, month: int, fraction: float) -> Rate:
    return at_month(fraction, month=month, default=0.0, snapshots=horizon_months + 1)


def test_a_forced_sale_happens_with_no_window_and_no_shortfall() -> None:
    """The issuer can redeem against the holder's wishes: floor satisfied, no tender open."""

    horizon = 12
    case = holder(
        initial_cash=10_000,
        monthly_spend=0,
        pe_units=100.0,
        pe_cost_basis_per_unit=10,
        pe_holding_period_months=36,
        horizon_months=horizon,
        lnw_floor=0,
    )
    rollout = run(
        case,
        protocol(
            horizon_months=horizon,
            initial_mark=Decimal(100),
            forced_sale=forced_sale_in_month(horizon_months=horizon, month=5, fraction=0.3),
        ),
    )

    assert units_held(rollout, month=6) == pytest.approx(70.0)
    assert cash(rollout, case, month=6) == pytest.approx(13_000.0)
    [row] = dispositions(rollout, month=5).iter_rows(named=True)
    assert row["cause_id"] == "pe_forced_sale_m5_acme"


def test_a_forced_sale_still_books_its_capital_gain() -> None:
    """A sale the holder did not choose is taxed like one they did.

    30 units at a $100 mark against a $10 basis is $2,700 of gain, long-term on a lot held
    36 months. Worth pinning separately because a forced sale takes a different path
    through the engine than a tender does, and tax state is what that path could drop.
    """

    horizon = 12
    case = holder(
        initial_cash=0,
        monthly_spend=0,
        pe_units=100.0,
        pe_cost_basis_per_unit=10,
        pe_holding_period_months=36,
        horizon_months=horizon,
        lnw_floor=0,
        taxed=True,
    )
    rollout = run(
        case,
        protocol(
            horizon_months=horizon,
            initial_mark=Decimal(100),
            forced_sale=forced_sale_in_month(horizon_months=horizon, month=5, fraction=0.3),
        ),
    )

    [gain] = [row for row in book(rollout, 6).capital_gains if row.agent_id == ALICE]
    assert gain.long_term_gain / QUANTA_PER_UNIT == pytest.approx(2_700.0)


def test_proceeds_land_in_the_account_the_policy_names() -> None:
    """An owner with two accounts: the policy's account takes the proceeds, the other is untouched."""

    horizon = 12
    case = replace(
        holder(
            initial_cash=0,
            monthly_spend=0,
            pe_units=100.0,
            pe_cost_basis_per_unit=10,
            pe_holding_period_months=36,
            horizon_months=horizon,
            lnw_floor=0,
        ),
        accounts=(account(ALICE, SAVINGS, balance=123), account(ALICE), account(SPEND_SINK)),
    )
    rollout = run(
        case,
        protocol(
            horizon_months=horizon,
            initial_mark=Decimal(100),
            forced_sale=forced_sale_in_month(horizon_months=horizon, month=5, fraction=0.3),
        ),
    )

    assert balances(rollout, case, month=6) == {CHECKING: 300_000, SAVINGS: 12_300}
    assert rollout.trace is not None
    [row] = rollout.trace.events.lot_dispositions.filter(pl.col("cause_id") == "pe_forced_sale_m5_acme").iter_rows(
        named=True
    )
    assert row["proceeds_account_id"] == CHECKING
    assert row["source_account_id"] == CHECKING


def test_a_recovery_cashout_takes_the_rest_of_the_position_for_a_stated_amount() -> None:
    """A wind-up pays what it pays: all remaining units go for $100 regardless of the mark.

    The position is marked at $100/unit x 100 units = $10,000, and the holder receives
    $100. That gap is the point — a recovery is not priced off the mark.
    """

    horizon = 12
    case = holder(
        initial_cash=0,
        monthly_spend=0,
        pe_units=100.0,
        pe_cost_basis_per_unit=10,
        pe_holding_period_months=36,
        horizon_months=horizon,
        lnw_floor=0,
    )
    rollout = run(
        case,
        protocol(
            horizon_months=horizon,
            initial_mark=Decimal(100),
            forced_recovery_usd=at_month(Decimal(100), month=5, default=Decimal(0), snapshots=horizon + 1),
        ),
    )

    assert units_held(rollout, month=6) == pytest.approx(0.0)
    assert cash(rollout, case, month=6) == pytest.approx(100.0)
    [row] = dispositions(rollout, month=5).iter_rows(named=True)
    assert row["units_sold"] == pytest.approx(100.0)
    assert row["proceeds_quanta"] / QUANTA_PER_UNIT == pytest.approx(100.0)
    assert row["cause_id"] == "pe_forced_recovery_m5_acme"


@pytest.mark.parametrize("cashout_quanta", [1, 2, 5])
def test_tiny_total_recovery_is_not_rounded_through_a_unit_price(cashout_quanta: int) -> None:
    case = holder(
        initial_cash=0,
        monthly_spend=0,
        pe_units=3.0,
        pe_cost_basis_per_unit=1,
        pe_holding_period_months=36,
        horizon_months=1,
        lnw_floor=0,
    )
    rollout = run(
        case,
        protocol(
            horizon_months=1,
            initial_mark=Decimal(100),
            forced_recovery_usd=at_month(
                Decimal(cashout_quanta) / QUANTA_PER_UNIT, month=0, default=Decimal(0), snapshots=2
            ),
        ),
    )

    [sale] = dispositions(rollout, month=0).iter_rows(named=True)
    assert sale["proceeds_quanta"] == cashout_quanta
    assert sale["cost_basis_consumed_quanta"] == 300
    assert units_held(rollout, month=1) == 0
    assert cash(rollout, case, month=1) == pytest.approx(cashout_quanta / QUANTA_PER_UNIT)


def test_a_disposition_carries_the_lot_it_consumed() -> None:
    """All 200 units at an $80 mark against a $20 basis: $16,000 out, $4,000 of basis gone."""

    horizon, tender_month = 12, 3
    case = holder(
        initial_cash=10_000,
        monthly_spend=0,
        pe_units=200.0,
        pe_cost_basis_per_unit=20,
        pe_holding_period_months=24,
        horizon_months=horizon,
        lnw_floor=100_000,
    )
    rollout = run(
        case,
        protocol(horizon_months=horizon, initial_mark=Decimal(50), tender_month=tender_month, tender_mark=Decimal(80)),
    )

    [row] = dispositions(rollout, month=tender_month).iter_rows(named=True)
    assert row["asset_id"] == ASSET_ID
    assert row["lot_id"] == LOT_ID
    assert row["agent_id"] == ALICE
    assert row["units_sold"] == pytest.approx(200.0, abs=0.02)
    assert row["cost_basis_consumed_quanta"] / QUANTA_PER_UNIT == pytest.approx(4_000.0, abs=1.0)


def test_a_pe_lot_with_no_protocol_for_its_issuer_is_refused() -> None:
    """A position whose issuer has no channels cannot be priced, marked, or sold.

    Answering it would mean inventing a mark, so holding it fails instead.
    """

    with pytest.raises(ValueError, match=r"missing private-equity mark series for issuer 'acme'"):
        compose(unreachable_floor(horizon_months=12), ())


if __name__ == "__main__":
    pytest_bazel.main()
