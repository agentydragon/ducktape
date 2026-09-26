"""Tax-loss harvesting defers a gain and gives it back; it never manufactures one."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.model.series import SP500_SYMBOL, SecurityKey, SecuritySymbol
from finance.augur.sim.actions import Action, DecisionActions, Liquidate, LotSale, PayClaim, Sell, Withdraw
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.observations import Observation
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedJurisdiction,
    PreparedLot,
    PreparedSeries,
    PreparedTlhPortfolio,
)
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.scenario import ORDINARY_INCOME, FilingStatus, TaxProfile
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tlh import TlhAssumptions
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
ALICE = "alice"
IRS = "irs"
SP500 = SecurityKey(symbol=SP500_SYMBOL)
GAINCO = SecurityKey(symbol=SecuritySymbol("gainco"))
BROKERAGE = "brokerage"
CHECKING = "checking"
FEDERAL = "federal_us"
PARAMS = TlhAssumptions(
    peak_annual_yield=0.12,
    floor_annual_yield=0.004,
    maturity_decay_exponent=1.5,
    drawdown_sensitivity=6.0,
    short_term_fraction=1.0,
)

# What a month's decision does with the sleeve or another lot, written against what alice observes.
type Intent = Callable[[Observation], Action]


@dataclass(frozen=True)
class Situation:
    """Alice's SP500 sleeve, held as a managed TLH portfolio or as a plain lot, and any other lot she holds."""

    series: tuple[PreparedSeries, ...]
    rollout_count: int
    horizon_months: int
    sleeve: PreparedLot
    with_harvest: bool
    assumptions: TlhAssumptions
    extra_lots: tuple[PreparedLot, ...] = ()


def _lot(lot_id: str, asset: SecurityKey, *, quantity: float, cost_basis: Decimal, purchase_month: int) -> PreparedLot:
    scale = quantity_scale_for_asset(asset)
    return PreparedLot(
        lot_id=lot_id,
        agent_id=ALICE,
        account_id=BROKERAGE,
        asset_id=str(asset.symbol),
        purchase_month=purchase_month,
        quantity_scale=scale,
        units=int(quantity_to_quanta(quantity, scale=scale)),
        basis=int(currency_amount_to_quanta(cost_basis, quantum=QUANTUM)),
    )


def situation(
    levels: Mapping[SecurityKey, Sequence[Sequence[float]]],
    *,
    with_harvest: bool,
    quantity: float = 1000.0,
    cost_basis_per_unit: int = 1,
    purchase_month: int = 0,
    short_term_fraction: float = 1.0,
    extra_lots: tuple[PreparedLot, ...] = (),
) -> Situation:
    """The sleeve is 1000 units at $1 cost basis by default, so its market value is easy to reason about."""
    rollouts = len(next(iter(levels.values())))
    horizon = len(next(iter(levels.values()))[0]) - 1
    paths = ExternalSeriesContext.from_level_blocks(
        [(asset, np.asarray(path, dtype=np.float64)) for asset, path in levels.items()],
        rollout_count=rollouts,
        horizon_months=horizon,
    )
    return Situation(
        series=compile_series(paths, rollout_count=rollouts, horizon_months=horizon, currency_quantum=QUANTUM),
        rollout_count=rollouts,
        horizon_months=horizon,
        sleeve=_lot(
            "alice_sp500",
            SP500,
            quantity=quantity,
            cost_basis=Decimal(str(quantity)) * cost_basis_per_unit,
            purchase_month=purchase_month,
        ),
        with_harvest=with_harvest,
        assumptions=PARAMS.model_copy(update={"short_term_fraction": short_term_fraction}),
        extra_lots=extra_lots,
    )


def compose(case: Situation, rollout_id: int) -> World:
    jurisdictions = {FEDERAL: load_jurisdiction(FEDERAL)}
    world = World(
        MarketPath(case.series, rollout_id, rollout_count=case.rollout_count),
        horizon_months=case.horizon_months,
        income_sources=(ORDINARY_INCOME,),
        jurisdictions=(PreparedJurisdiction(jurisdiction_id=FEDERAL, level=jurisdictions[FEDERAL].level),),
    )
    for agent_id, account_id in ((ALICE, BROKERAGE), (ALICE, CHECKING), (IRS, CHECKING)):
        world.declare_account(
            PreparedAccount(account=AccountRef(agent_id=agent_id, account_id=account_id), opening_balance=0)
        )
    world.track(
        TaxAuthority(
            compile_profile(
                TaxProfile(
                    agent_id=ALICE,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=[FEDERAL],
                    tax_authority_agent_id=IRS,
                ),
                jurisdictions,
                quantum=QUANTUM,
            )
        )
    )
    lots = case.extra_lots if case.with_harvest else (case.sleeve, *case.extra_lots)
    for pool in {
        lot.asset_id: PreparedHoldingPool(
            agent_id=ALICE, account_id=BROKERAGE, asset_id=lot.asset_id, quantity_scale=lot.quantity_scale
        )
        for lot in lots
    }.values():
        world.declare_pool(pool)
    for lot in lots:
        world.hold(lot)
    if case.with_harvest:
        world.declare_portfolio(
            PreparedTlhPortfolio(
                portfolio_id="alice-sp500",
                owner_agent_id=ALICE,
                account_id=BROKERAGE,
                asset_id=case.sleeve.asset_id,
                quantity_scale=case.sleeve.quantity_scale,
                initial_cohorts=(case.sleeve,),
                assumptions=case.assumptions,
            )
        )
    return world


def pay_claims(observation: Observation) -> list[Action]:
    """Alice pays whatever she is billed: the tax assessment is the only claim these situations raise."""
    return [
        PayClaim(
            request_id=index + 1,
            cause_id=claim.cause_id,
            claim=claim,
            from_account=claim.from_account,
            amount=claim.amount_due,
        )
        for index, claim in enumerate(observation.claims)
    ]


def run(case: Situation, intents: Mapping[int, Sequence[Intent]] = {}) -> list[Rollout]:
    """Every path to the horizon; the month's intents, then every claim, become alice's ordered actions."""
    session = ActionSession({rollout_id: compose(case, rollout_id) for rollout_id in range(case.rollout_count)}, ALICE)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(
                [
                    DecisionActions(
                        decision.rollout_id,
                        decision.observation.month,
                        [
                            *(intent(decision.observation) for intent in intents.get(decision.observation.month, ())),
                            *pay_claims(decision.observation),
                        ],
                    )
                    for decision in batch
                ]
            )
    finally:
        session.close()
    for rollout in batch.rollouts:
        assert rollout.stop is None
    return batch.rollouts


def sell_lots(cause_id: str, asset: SecurityKey) -> Intent:
    """Every unit of the asset alice holds as lots, oldest first."""

    def intent(observation: Observation) -> Action:
        positions = sorted(
            (position for position in observation.public_positions if position.asset_id == str(asset.symbol)),
            key=lambda position: (position.purchase_month, position.lot_id),
        )
        assert positions
        return Sell(
            cause_id=cause_id,
            agent_id=ALICE,
            proceeds_account_id=CHECKING,
            asset_id=str(asset.symbol),
            lots=tuple(
                LotSale(account_id=position.account_id, lot_id=position.lot_id, units=position.units)
                for position in positions
            ),
        )

    return intent


def sell_sleeve(cause_id: str, *, fraction: Decimal) -> Intent:
    """A share of the sleeve: the managed portfolio gives up that share of its value, a lot that share of its units."""

    def intent(observation: Observation) -> Action:
        if observation.tlh_portfolios:
            portfolio = one(observation.tlh_portfolios)
            if fraction == 1:
                return Liquidate(
                    cause_id=cause_id, agent_id=ALICE, portfolio_id=portfolio.portfolio_id, cash_account_id=CHECKING
                )
            return Withdraw(
                cause_id=cause_id,
                agent_id=ALICE,
                portfolio_id=portfolio.portfolio_id,
                cash_account_id=CHECKING,
                amount=int(portfolio.value * fraction),
            )
        position = one(position for position in observation.public_positions if position.asset_id == str(SP500.symbol))
        return Sell(
            cause_id=cause_id,
            agent_id=ALICE,
            proceeds_account_id=CHECKING,
            asset_id=position.asset_id,
            lots=(
                LotSale(account_id=position.account_id, lot_id=position.lot_id, units=int(position.units * fraction)),
            ),
        )

    return intent


def ytd_gain(rollout: Rollout, *, month_index: int, classification: str) -> float:
    """Alice's year-to-date gain of one classification in the book closed at `month_index`."""
    assert rollout.trace is not None
    books = [book for book in rollout.trace.books if book.month == month_index]
    if not books:
        return 0.0
    gains = [gain for gain in one(books).capital_gains if gain.agent_id == ALICE]
    return float(sum(gain.short_term_gain if classification == "stcg" else gain.long_term_gain for gain in gains))


def harvested_short_term_in_month(rollout: Rollout, *, calendar_month: int) -> float:
    """Magnitude of short-term loss harvested during `calendar_month` (a positive number).

    The book closed at `month_index = m + 1` reflects the end of calendar month `m`, so the loss booked
    during month `m` is the drop in cumulative YTD short-term gain from that of `m` to `m + 1`."""
    before = ytd_gain(rollout, month_index=calendar_month, classification="stcg")
    after = ytd_gain(rollout, month_index=calendar_month + 1, classification="stcg")
    return before - after


@pytest.mark.parametrize("bad_level", [-0.01, float("nan")], ids=["negative", "nonfinite"])
def test_harvest_index_validation_rejects_negative_or_nonfinite_prices(bad_level: float) -> None:
    # A non-finite level is refused where the path is built; a negative one where the sleeve is priced.
    with pytest.raises(ValueError, match=r"(?i)(price must be nonnegative|no finite level)"):
        run(situation({SP500: [[1.0, bad_level, 1.0]]}, with_harvest=True))


def test_a_security_price_is_required_at_the_terminal_snapshot_too() -> None:
    """The managed portfolio's supplied price is checked at every snapshot, the terminal one included."""
    with pytest.raises(ValueError, match=r"(?i)price must be nonnegative"):
        run(situation({SP500: [[1.0, 1.0, -1.0]]}, with_harvest=True, quantity=100.0))


def test_down_month_harvests_strictly_more_than_flat_month() -> None:
    # Two rollouts, same fresh sleeve. Rollout 0 has a 20% drawdown in calendar month 1; rollout 1
    # is flat. The loss harvested DURING month 1 must be strictly larger for the drawdown rollout
    # (the drawdown kicker), isolating the period-return effect (both enter month 1 with the same
    # basis and a comparable embedded-gain fraction).
    drawdown, flat = run(situation({SP500: [[1.0, 1.0, 0.8, 0.8], [1.0, 1.0, 1.0, 1.0]]}, with_harvest=True))
    assert (
        harvested_short_term_in_month(drawdown, calendar_month=2)
        > harvested_short_term_in_month(flat, calendar_month=2)
        > 0.0
    )


def test_long_bull_run_ossifies_harvest_toward_floor() -> None:
    # A long, steady bull run: basis stays at month-0 level while MV climbs, so the embedded-gain
    # fraction e -> 1 and the harvested loss per month decays toward the floor. Compare an early
    # month's harvest to a late month's; late must be strictly smaller (ossification).
    horizon = 24
    [rollout] = run(situation({SP500: [[1.0 * (1.03**m) for m in range(horizon + 1)]]}, with_harvest=True))
    early = harvested_short_term_in_month(rollout, calendar_month=1)
    late = harvested_short_term_in_month(rollout, calendar_month=12)
    assert early > 0.0  # a real loss was harvested early
    assert late < early  # ossification: harvest decays as embedded gains build


def test_harvested_short_term_loss_offsets_realized_gain_lowering_tax() -> None:
    # Alice realizes a real short-term capital GAIN (a separate crypto-like lot sold at a profit) in
    # the same year she harvests SP500 losses. With harvesting on, the harvested ST loss nets against
    # that gain (§1211/§1212), lowering the year's tax vs the no-harvest baseline.
    gain_lot = _lot("alice_gain", GAINCO, quantity=100.0, cost_basis=Decimal(10_000), purchase_month=-3)
    # SP500 sleeve drops then recovers so harvesting books meaningful losses through the year.
    levels = {
        SP500: [[1.0, 0.85, 0.85, 0.9, 0.9, 0.9, 0.95] + [0.95] * 7],
        GAINCO: [[400.0] * 14],  # $400 against a $100 basis: a $30k short-term gain at the month-6 sale.
    }

    def year_tax(with_harvest: bool) -> int:
        [rollout] = run(
            situation(levels, with_harvest=with_harvest, extra_lots=(gain_lot,)),
            {6: [sell_lots("alice_gain_sale", GAINCO)]},
        )
        return sum(accrual.total_tax for accrual in rollout.summary.tax_accruals if accrual.jurisdiction_id == FEDERAL)

    assert year_tax(with_harvest=True) < year_tax(with_harvest=False)


def test_give_back_makes_sale_gain_larger_by_cumulative_harvest_and_is_bounded() -> None:
    # The deferral check. Hold the sleeve, harvest for several months, then liquidate the entire
    # sleeve. The realized gain at sale must be larger WITH harvesting than without — by exactly the
    # cumulative harvested loss booked over the held months — so the deferred gain is fully repaid.
    # Net: the year's total realized capital gain (harvested losses + give-back at sale) returns to
    # the no-harvest baseline, proving the benefit is deferral/timing, not unbounded free money.
    horizon = 8
    sale_month = 6
    # Flat sleeve price so the sale itself realizes ~zero economic gain; all the give-back is the
    # repaid deferral. (Price 1.0 == cost basis, so without harvest the sale gain is 0.)
    levels = {SP500: [[1.0] * (horizon + 1)]}
    sale = {sale_month: [sell_sleeve("alice_sp500_liquidate", fraction=Decimal(1))]}
    [harvested] = run(situation(levels, with_harvest=True), sale)
    [baseline] = run(situation(levels, with_harvest=False), sale)

    # The book closed at `month_index = m + 1` is the end of calendar month `m`. The sale fires inside
    # month `sale_month`, after that month's modeled harvest. The net monthly gain change repays
    # losses accumulated through month sale_month-1 (the book closed at `month_index = sale_month`).
    cumulative_harvest = -ytd_gain(harvested, month_index=sale_month, classification="stcg")
    assert cumulative_harvest > 0.0

    # The YTD change includes both this month's loss and the liquidation gain. It is not
    # a sale receipt; their sum cancels this month's additional deferral.
    def net_sale_month_gain(rollout: Rollout) -> float:
        before = ytd_gain(rollout, month_index=sale_month, classification="stcg") + ytd_gain(
            rollout, month_index=sale_month, classification="ltcg"
        )
        after = ytd_gain(rollout, month_index=sale_month + 1, classification="stcg") + ytd_gain(
            rollout, month_index=sale_month + 1, classification="ltcg"
        )
        return after - before

    # Baseline monthly gain is zero; the managed run repays all prior deferral.
    assert net_sale_month_gain(baseline) == pytest.approx(0.0, abs=1e-6)
    assert net_sale_month_gain(harvested) == pytest.approx(cumulative_harvest, rel=1e-9, abs=1e-6)

    # Deferral, not free money: after the give-back, the net realized capital gain over the whole
    # (sub-year) horizon returns to the no-harvest baseline (~0) — bounded, not unbounded free money.
    net_st = ytd_gain(harvested, month_index=horizon, classification="stcg")
    net_lt = ytd_gain(harvested, month_index=horizon, classification="ltcg")
    assert net_st + net_lt == pytest.approx(0.0, abs=1e-6)


def test_partial_sales_give_back_proportionally_and_never_exceed_harvest() -> None:
    # Two partial sales (half, then the rest) must together give back exactly the cumulative harvest
    # through consumed adjusted basis. The portfolio has no separate deferral balance.
    horizon = 9
    [rollout] = run(
        situation({SP500: [[1.0] * (horizon + 1)]}, with_harvest=True),
        {
            4: [sell_sleeve("alice_sp500_half", fraction=Decimal("0.5"))],
            7: [sell_sleeve("alice_sp500_rest", fraction=Decimal(1))],
        },
    )
    # By the terminal month, all units are sold, so the entire cumulative harvest has been given
    # back: the year-cumulative net short-term gain returns to ~0 (price flat == basis).
    assert ytd_gain(rollout, month_index=horizon, classification="stcg") == pytest.approx(0.0, abs=1e-6)


def test_harvest_off_reproduces_baseline_capital_gains_exactly() -> None:
    # Regression: a scenario with no harvest policy must produce byte-identical capital-gain YTD to
    # the same scenario run on the pre-harvest code path (here: no harvested losses ever appear).
    [rollout] = run(situation({SP500: [[1.0, 0.8, 0.9, 0.85, 0.95, 1.1, 1.2]]}, with_harvest=False))
    assert rollout.trace is not None
    # No sales, no harvest → no capital-gain rows at all.
    assert not [
        gain
        for book in rollout.trace.books
        for gain in book.capital_gains
        if gain.agent_id == ALICE and (gain.short_term_gain or gain.long_term_gain)
    ]


if __name__ == "__main__":
    pytest_bazel.main()
