"""Public lot controls through explicit monthly actions and exact common books."""

from collections.abc import Callable
from decimal import Decimal
from fractions import Fraction

import pytest_bazel

from finance.augur.model.gbm import GeometricBrownian
from finance.augur.model.level_series_groups import AssetPriceGroups
from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.policy import sleeves
from finance.augur.sim.actions import Action, DecisionActions, LotSale, Sell
from finance.augur.sim.books import SecurityLotState
from finance.augur.sim.observations import Observation
from finance.augur.sim.results import Executed, Finished, Rejected, RejectedAction, Rollout
from finance.augur.sim.scenario import InitialLot, TaxProfile
from finance.augur.sim.session import ActionSession
from finance.augur.sim.testing.case import Case, levels, sampled, scenario
from finance.augur.sim.testing.fixtures import VTI, checking, taxed

QQQ = SecurityKey(symbol=SecuritySymbol("qqq"))
BTC = SecurityKey(symbol=SecuritySymbol("btc"))


def _lot(
    lot_id: str,
    quantity: float,
    basis: Decimal,
    purchase_month: int,
    *,
    asset: SecurityKey = VTI,
    account: str = "checking",
) -> InitialLot:
    return InitialLot(
        lot_id=lot_id,
        agent_id="alice",
        account_id=account,
        asset=asset,
        purchase_month_index=purchase_month,
        quantity=quantity,
        cost_basis=basis,
    )


def _case(
    lots: list[InitialLot],
    prices: dict[SecurityKey, list[Decimal]],
    *,
    rollouts: int = 1,
    tax_profiles: list[TaxProfile] | None = None,
) -> Case:
    profiles = [] if tax_profiles is None else tax_profiles
    balances = checking(("alice", Decimal(0)))
    if profiles:
        balances.extend(checking(("irs", Decimal(0))))
    return Case(
        scenario=scenario(
            balances, initial_lots=lots, tax_profiles=profiles, horizon_months=len(next(iter(prices.values()))) - 1
        ),
        rollout_count=rollouts,
        series={asset: levels([path] * rollouts) for asset, path in prices.items()},
    )


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
    return Sell(cause_id=cause, agent_id="alice", proceeds_account_id="checking", asset_id=asset, lots=tuple(lots))


def _run(case: Case, propose: Callable[[Observation], list[Action]]) -> list[Rollout]:
    session = ActionSession(case.compiled_run, "alice", list(range(case.rollout_count)))
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
    case = _case([_lot("seed", 100, Decimal(8000), -24)], {VTI: [Decimal(120)] * 7})
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
    case = _case([_lot("seed", 100, Decimal(9000), -12)], {VTI: [Decimal(150)] * 4})
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
    case = _case([_lot("seed", 50, Decimal(5000), 0)], {VTI: [Decimal(110)] * 3}, rollouts=100)
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
    case = _case([_lot("old", 100, Decimal(8000), -6), _lot("young", 50, Decimal(5000), 2)], {VTI: [Decimal(200)] * 11})
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
    case = _case(
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
    case = _case(
        [_lot("long", 2, Decimal(40000), -12, asset=BTC), _lot("short", 1, Decimal(40000), 2, asset=BTC)],
        {BTC: [Decimal(60000)] * 8},
        tax_profiles=[taxed("alice", "federal_us")],
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
    case = _case(
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
    case = _case(
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
    case = _case([_lot("seed", 5, Decimal(400), -12, account="taxable")], {VTI: [Decimal(100)] * 3})

    def propose(obs: Observation) -> list[Action]:
        if obs.month != 1:
            return []
        [lot] = obs.public_positions
        return [
            Sell(
                cause_id="oversell",
                agent_id="alice",
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
    case = _case(
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
    authored = scenario(
        checking(("alice", Decimal(0))),
        initial_lots=[_lot("seed", 5, Decimal(500), 0)],
        external_series=SeriesModelBundle.independent(
            asset_prices=AssetPriceGroups(
                security={
                    VTI.symbol: GeometricBrownian(
                        initial_value=100.0, monthly_log_return_mu=0.005, monthly_log_return_sigma=0.05
                    )
                }
            )
        ),
        tax_profiles=[],
        horizon_months=6,
    )

    def propose(obs: Observation) -> list[Action]:
        return [_sale(obs, Fraction(5))] if obs.month == 3 else []

    first = _run(sampled(authored, rollout_count=200), propose)
    second = _run(sampled(authored, rollout_count=200), propose)
    assert all(rollout.stop is None for rollout in [*first, *second])
    assert [rollout.summary.cash for rollout in first] == [rollout.summary.cash for rollout in second]
    assert len({rollout.summary.cash[0].values[-1] for rollout in first}) > 100


def test_awkward_thirds_consume_exactly_the_whole_lot_basis() -> None:
    case = _case([_lot("seed", 2.5, Decimal("83.33"), -24)], {VTI: [Decimal(50)] * 7})

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
