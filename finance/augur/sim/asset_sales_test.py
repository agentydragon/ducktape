"""Public lot controls through explicit monthly actions and exact common books."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest_bazel

from finance.augur.model.gbm import GeometricBrownian
from finance.augur.model.level_series_groups import AssetPriceGroups
from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.policy import sleeves
from finance.augur.sim.actions import Action, DecisionActions, LotSale, Sell
from finance.augur.sim.books import AccountRef, SecurityLotState
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.external_series import ExternalSeriesContext, materialize_external_series
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
)
from finance.augur.sim.results import Executed, Finished, Rejected, RejectedAction, Rollout
from finance.augur.sim.scenario import ORDINARY_INCOME, TaxProfile
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World

VTI = SecurityKey(symbol=SecuritySymbol("vti"))
QQQ = SecurityKey(symbol=SecuritySymbol("qqq"))
BTC = SecurityKey(symbol=SecuritySymbol("btc"))
QUANTUM = Decimal("0.01")
ALICE = "alice"


@dataclass(frozen=True)
class Situation:
    """What every path shares: the compiled paths, the lots each world opens holding, and who files tax."""

    series: tuple[PreparedSeries, ...]
    rollout_count: int
    horizon_months: int
    lots: tuple[PreparedLot, ...]
    tax_profiles: tuple[TaxProfile, ...]


def _lot(
    lot_id: str,
    quantity: float,
    basis: Decimal,
    purchase_month: int,
    *,
    asset: SecurityKey = VTI,
    account: str = "checking",
) -> PreparedLot:
    scale = quantity_scale_for_asset(asset)
    return PreparedLot(
        lot_id=lot_id,
        agent_id=ALICE,
        account_id=account,
        asset_id=str(asset.symbol),
        purchase_month=purchase_month,
        quantity_scale=scale,
        units=int(quantity_to_quanta(quantity, scale=scale)),
        basis=int(currency_amount_to_quanta(basis, quantum=QUANTUM)),
    )


def _taxed(*jurisdiction_ids: str) -> TaxProfile:
    """One single filer paying from `checking` to the `irs` agent; the ids are all it says about tax law."""
    return TaxProfile(
        agent_id=ALICE, jurisdiction_ids=list(jurisdiction_ids), tax_authority_agent_id="irs", prior_year_tax=Decimal(0)
    )


def _situation(
    lots: Sequence[PreparedLot],
    prices: Mapping[SecurityKey, Sequence[Decimal]],
    *,
    rollouts: int = 1,
    tax_profiles: Sequence[TaxProfile] = (),
) -> Situation:
    """One stipulated price path per asset, repeated across every rollout that shares it."""
    horizon = len(next(iter(prices.values()))) - 1
    paths = ExternalSeriesContext.from_level_blocks(
        [
            (asset, np.asarray([[float(value) for value in path]] * rollouts, dtype=np.float64))
            for asset, path in prices.items()
        ],
        rollout_count=rollouts,
        horizon_months=horizon,
    )
    return Situation(
        series=compile_series(paths, rollout_count=rollouts, horizon_months=horizon, currency_quantum=QUANTUM),
        rollout_count=rollouts,
        horizon_months=horizon,
        lots=tuple(lots),
        tax_profiles=tuple(tax_profiles),
    )


def _compose(case: Situation, rollout_id: int) -> World:
    """Alice opens with no cash, a pool per account each lot sits in, and the `irs` any profile files with."""
    jurisdictions = {
        jurisdiction_id: load_jurisdiction(jurisdiction_id)
        for profile in case.tax_profiles
        for jurisdiction_id in profile.jurisdiction_ids
    }
    world = World(
        MarketPath(case.series, rollout_id, rollout_count=case.rollout_count),
        horizon_months=case.horizon_months,
        income_sources=(ORDINARY_INCOME,),
        jurisdictions=tuple(
            PreparedJurisdiction(jurisdiction_id=jurisdiction_id, level=jurisdiction.level)
            for jurisdiction_id, jurisdiction in sorted(jurisdictions.items())
        ),
    )
    for agent_id in (ALICE, *(("irs",) if case.tax_profiles else ())):
        world.declare_account(
            PreparedAccount(account=AccountRef(agent_id=agent_id, account_id="checking"), opening_balance=0)
        )
    for profile in case.tax_profiles:
        world.track(TaxAuthority(compile_profile(profile, jurisdictions, quantum=QUANTUM)))
    for pool in {
        (lot.account_id, lot.asset_id): PreparedHoldingPool(
            agent_id=ALICE, account_id=lot.account_id, asset_id=lot.asset_id, quantity_scale=lot.quantity_scale
        )
        for lot in case.lots
    }.values():
        world.declare_pool(pool)
    for lot in case.lots:
        world.hold(lot)
    return world


def _sale(
    observation: Observation, quantity: Fraction, *, cause: str = "sale", asset: str = "vti", account: str = "checking"
) -> Sell:
    lots, _ = sleeves._sale_lots(
        sorted(
            (lot for lot in observation.public_positions if (lot.account_id, lot.asset_id) == (account, asset)),
            key=lambda lot: (lot.purchase_month, lot.lot_id),
        ),
        0,
        unit_target=quantity,
    )
    return Sell(cause_id=cause, agent_id=ALICE, proceeds_account_id="checking", asset_id=asset, lots=tuple(lots))


def _run(case: Situation, propose: Callable[[Observation], list[Action]]) -> list[Rollout]:
    session = ActionSession({id_: _compose(case, id_) for id_ in range(case.rollout_count)}, ALICE)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(
                [
                    DecisionActions(decision.rollout_id, decision.observation.month, propose(decision.observation))
                    for decision in batch
                ]
            )
        return batch.rollouts
    finally:
        session.close()


def _remaining(lot: SecurityLotState) -> Fraction:
    return Fraction(lot.units_remaining, lot.quantity_scale)


def test_partial_sale_retains_exact_basis_and_cash_trajectory() -> None:
    case = _situation([_lot("seed", 100, Decimal(8000), -24)], {VTI: [Decimal(120)] * 7})
    [rollout] = _run(case, lambda obs: [_sale(obs, Fraction(30))] if obs.month == 3 else [])
    assert rollout.stop is None
    assert rollout.summary.cash[0].values == [0, 0, 0, 0, 360_000, 360_000, 360_000]
    assert rollout.trace is not None
    assert [_remaining(book.lots[0]) for book in rollout.trace.books] == [100, 100, 100, 100, 70, 70, 70]
    assert [book.lots[0].basis_remaining for book in rollout.trace.books] == [800_000] * 4 + [560_000] * 3
    [receipt] = rollout.trace.receipts
    assert isinstance(receipt.outcome, Executed)
    assert receipt.month == 3
    [disposition] = rollout.trace.events.lot_dispositions.to_dicts()
    assert disposition["lot_id"] == "seed"
    assert disposition["cause_id"] == "sale"
    assert disposition["month_index"] == 3
    assert disposition["purchase_month_index"] == -24
    assert disposition["units_sold"] == 30
    assert disposition["cost_basis_consumed_quanta"] == 240_000
    assert disposition["proceeds_quanta"] == 360_000


def test_full_sale_preserves_exhausted_lot_with_zero_basis() -> None:
    case = _situation([_lot("seed", 100, Decimal(9000), -12)], {VTI: [Decimal(150)] * 4})
    [rollout] = _run(case, lambda obs: [_sale(obs, Fraction(100))] if obs.month == 2 else [])
    assert rollout.stop is None
    [lot] = rollout.summary.ending_book.lots
    assert lot.units_remaining == lot.basis_remaining == 0
    assert rollout.summary.cash[0].values == [0, 0, 0, 1_500_000]
    assert rollout.trace is not None
    assert rollout.trace.events.lot_dispositions.select(
        "units_sold", "proceeds_quanta", "cost_basis_consumed_quanta"
    ).rows() == [(100, 1_500_000, 900_000)]


def test_deterministic_sale_scales_across_one_hundred_rollouts() -> None:
    case = _situation([_lot("seed", 50, Decimal(5000), 0)], {VTI: [Decimal(110)] * 3}, rollouts=100)
    rollouts = _run(case, lambda obs: [_sale(obs, Fraction(20))] if obs.month == 1 else [])
    assert [rollout.rollout_id for rollout in rollouts] == list(range(100))
    for rollout in rollouts:
        assert rollout.stop is None
        [lot] = rollout.summary.ending_book.lots
        assert _remaining(lot) == 30
        assert lot.basis_remaining == 300_000
        assert rollout.summary.cash[0].values == [0, 0, 220_000]
        assert rollout.trace is not None
        assert rollout.trace.events.lot_dispositions.height == 1


def test_fifo_crosses_two_lots_and_preserves_each_basis() -> None:
    case = _situation(
        [_lot("old", 100, Decimal(8000), -6), _lot("young", 50, Decimal(5000), 2)], {VTI: [Decimal(200)] * 11}
    )
    [rollout] = _run(case, lambda obs: [_sale(obs, Fraction(120))] if obs.month == 8 else [])
    assert rollout.stop is None
    assert rollout.trace is not None
    assert rollout.trace.events.lot_dispositions.sort("purchase_month_index").select(
        "lot_id", "units_sold", "cost_basis_consumed_quanta", "proceeds_quanta"
    ).rows() == [("old", Fraction(100), 800_000, 2_000_000), ("young", Fraction(20), 200_000, 400_000)]
    assert [(lot.lot_id, _remaining(lot), lot.basis_remaining) for lot in rollout.trace.books[9].lots] == [
        ("old", Fraction(0), 0),
        ("young", Fraction(30), 300_000),
    ]
    assert rollout.summary.cash[0].values == [0] * 9 + [2_400_000] * 2


def test_same_month_sales_consume_lots_sequentially() -> None:
    case = _situation(
        [_lot("old", 100, Decimal(8000), -24), _lot("new", 100, Decimal(10000), -6)], {VTI: [Decimal(150)] * 3}
    )

    def propose(obs: Observation) -> list[Action]:
        if obs.month != 1:
            return []
        first = _sale(obs, Fraction(70), cause="first")
        # A batch shares one observation: reserve the first sale before proposing the next.
        reserved = {lot.lot_id: lot.units for lot in first.lots}
        remaining = obs.model_copy(
            update={
                "public_positions": tuple(
                    lot.model_copy(update={"units": lot.units - reserved.get(lot.lot_id, 0)})
                    for lot in obs.public_positions
                )
            }
        )
        return [first, _sale(remaining, Fraction(70), cause="second")]

    [rollout] = _run(case, propose)
    assert rollout.stop is None
    assert rollout.trace is not None
    assert rollout.trace.events.lot_dispositions.sort(["cause_id", "purchase_month_index"]).select(
        "cause_id", "lot_id", "units_sold", "cost_basis_consumed_quanta", "proceeds_quanta"
    ).rows() == [
        ("first", "old", Fraction(70), 560_000, 1_050_000),
        ("second", "old", Fraction(30), 240_000, 450_000),
        ("second", "new", Fraction(40), 400_000, 600_000),
    ]
    assert [(lot.lot_id, _remaining(lot), lot.basis_remaining) for lot in rollout.summary.ending_book.lots] == [
        ("old", Fraction(0), 0),
        ("new", Fraction(60), 600_000),
    ]
    assert rollout.summary.cash[0].values[-1] == 2_100_000
    assert [(receipt.month, receipt.action_index) for receipt in rollout.trace.receipts] == [(1, 0), (1, 1)]
    assert all(isinstance(receipt.outcome, Executed) for receipt in rollout.trace.receipts)


def test_fifo_holding_period_classifies_each_disposition() -> None:
    case = _situation(
        [_lot("long", 2, Decimal(40000), -12, asset=BTC), _lot("short", 1, Decimal(40000), 2, asset=BTC)],
        {BTC: [Decimal(60000)] * 8},
        tax_profiles=[_taxed("federal_us")],
    )
    [rollout] = _run(case, lambda obs: [_sale(obs, Fraction(5, 2), asset="btc")] if obs.month == 6 else [])
    assert rollout.stop is None
    assert rollout.trace is not None
    long, short = rollout.trace.events.lot_dispositions.sort("purchase_month_index").to_dicts()
    assert (long["lot_id"], long["month_index"] - long["purchase_month_index"], long["units_sold"]) == ("long", 18, 2)
    assert (short["lot_id"], short["month_index"] - short["purchase_month_index"], short["units_sold"]) == (
        "short",
        4,
        0.5,
    )
    [gains] = rollout.summary.ending_book.capital_gains
    assert gains.long_term_gain == 8_000_000  # 2 * $60k - $40k basis.
    assert gains.short_term_gain == 1_000_000  # 0.5 * $60k - $20k basis.
    assert rollout.summary.cash[0].values[-1] == 15_000_000


def test_sales_of_different_assets_do_not_consume_each_others_lots() -> None:
    case = _situation(
        [_lot("vti", 10, Decimal(1000), 0), _lot("qqq", 10, Decimal(2000), 0, asset=QQQ)],
        {VTI: [Decimal(150)] * 7, QQQ: [Decimal(250)] * 7},
    )

    def propose(obs: Observation) -> list[Action]:
        match obs.month:
            case 2:
                return [_sale(obs, Fraction(4))]
            case 5:
                return [_sale(obs, Fraction(3), asset="qqq")]
            case _:
                return []

    [rollout] = _run(case, propose)
    assert rollout.stop is None
    assert {lot.lot_id: (_remaining(lot), lot.basis_remaining) for lot in rollout.summary.ending_book.lots} == {
        "vti": (Fraction(6), 60_000),
        "qqq": (Fraction(7), 140_000),
    }
    assert rollout.summary.cash[0].values == [0, 0, 0, 60_000, 60_000, 60_000, 135_000]
    assert rollout.trace is not None
    assert rollout.trace.events.lot_dispositions.height == 2


def test_sale_consumes_only_source_account_fifo_pool() -> None:
    case = _situation(
        [_lot("taxable", 10, Decimal(800), -12, account="taxable"), _lot("ira", 10, Decimal(700), -12, account="ira")],
        {VTI: [Decimal(100)] * 3},
    )
    [rollout] = _run(case, lambda obs: [_sale(obs, Fraction(8), account="taxable")] if obs.month == 1 else [])
    assert rollout.stop is None
    assert {lot.account_id: (_remaining(lot), lot.basis_remaining) for lot in rollout.summary.ending_book.lots} == {
        "taxable": (Fraction(2), 16_000),
        "ira": (Fraction(10), 70_000),
    }
    assert rollout.trace is not None
    assert rollout.trace.events.lot_dispositions.select("source_account_id", "lot_id", "units_sold").rows() == [
        ("taxable", "taxable", 8)
    ]
    assert rollout.summary.cash[0].values[-1] == 80_000


def test_oversell_rejects_atomically_and_leaves_no_disposition() -> None:
    case = _situation([_lot("seed", 5, Decimal(400), -12, account="taxable")], {VTI: [Decimal(100)] * 3})

    def propose(obs: Observation) -> list[Action]:
        if obs.month != 1:
            return []
        [lot] = obs.public_positions
        return [
            Sell(
                cause_id="oversell",
                agent_id=ALICE,
                proceeds_account_id="checking",
                asset_id="vti",
                lots=(LotSale(account_id="taxable", lot_id="seed", units=6 * lot.quantity_scale),),
            ),
            _sale(obs, Fraction(1), account="taxable", cause="unattempted"),
        ]

    [rollout] = _run(case, propose)
    assert rollout.stop == RejectedAction(month=1, action_index=0)
    [receipt] = rollout.summary.last_receipts
    assert isinstance(receipt.outcome, Rejected)
    assert receipt.action.cause_id == "oversell"
    [lot] = rollout.summary.ending_book.lots
    assert _remaining(lot) == 5
    assert lot.basis_remaining == 40_000
    assert rollout.summary.cash[0].values == [0, 0, 0]
    assert rollout.trace is not None
    assert rollout.trace.events.lot_dispositions.is_empty()
    assert len(rollout.trace.books) == 3


def test_sale_reads_current_month_of_deterministic_curve() -> None:
    case = _situation(
        [_lot("seed", 10, Decimal(900), -3)], {VTI: [Decimal(price) for price in (100, 110, 120, 130, 150, 160, 170)]}
    )
    [rollout] = _run(case, lambda obs: [_sale(obs, Fraction(4))] if obs.month == 4 else [])
    assert rollout.stop is None
    assert rollout.trace is not None
    assert rollout.trace.events.lot_dispositions.select("month_index", "units_sold", "proceeds_quanta").rows() == [
        (4, 4, 60_000)
    ]
    assert rollout.summary.cash[0].values == [0] * 5 + [60_000] * 2


def test_gbm_sales_diverge_and_same_seed_reproduces_all_cash() -> None:
    bundle = SeriesModelBundle.independent(
        asset_prices=AssetPriceGroups(
            security={
                VTI.symbol: GeometricBrownian(
                    initial_value=100.0, monthly_log_return_mu=0.005, monthly_log_return_sigma=0.05
                )
            }
        )
    )

    def drawn(rollout_count: int) -> Situation:
        """One seed per path, drawn from the situation's own series model rather than a stipulated curve."""
        paths = materialize_external_series(bundle, rollout_seeds=tuple(range(rollout_count)), horizon_months=6)
        return Situation(
            series=compile_series(paths, rollout_count=rollout_count, horizon_months=6, currency_quantum=QUANTUM),
            rollout_count=rollout_count,
            horizon_months=6,
            lots=(_lot("seed", 5, Decimal(500), 0),),
            tax_profiles=(),
        )

    def propose(obs: Observation) -> list[Action]:
        return [_sale(obs, Fraction(5))] if obs.month == 3 else []

    first = _run(drawn(200), propose)
    second = _run(drawn(200), propose)
    assert all(rollout.stop is None for rollout in [*first, *second])
    assert [rollout.summary.cash for rollout in first] == [rollout.summary.cash for rollout in second]
    assert len({rollout.summary.cash[0].values[-1] for rollout in first}) > 100


def test_awkward_thirds_consume_exactly_the_whole_lot_basis() -> None:
    case = _situation([_lot("seed", 2.5, Decimal("83.33"), -24)], {VTI: [Decimal(50)] * 7})

    def propose(obs: Observation) -> list[Action]:
        if obs.month not in (1, 2, 3):
            return []
        return [_sale(obs, Fraction(833334 if obs.month == 3 else 833333, 1000000))]

    [rollout] = _run(case, propose)
    assert rollout.stop is None
    assert rollout.trace is not None
    dispositions = rollout.trace.events.lot_dispositions
    assert dispositions.height == 3
    assert dispositions["cost_basis_consumed_quanta"].sum() == 8333
    assert dispositions["cost_basis_consumed_quanta"].to_list() == [2778, 2777, 2778]
    [lot] = rollout.summary.ending_book.lots
    assert lot.units_remaining == lot.basis_remaining == 0
    # Each $41.66665/$41.66670 proceeds rounds independently to $41.67.
    assert rollout.summary.cash[0].values[-1] == 12_501


if __name__ == "__main__":
    pytest_bazel.main()
