"""Independent actor mark, lot rounding, current-book and payer-scope controls."""

from dataclasses import replace
from unittest.mock import patch

import pytest
import pytest_bazel

from finance.augur.sim.actions import LotSale, Sell
from finance.augur.sim.claims import Claim, Claims
from finance.augur.sim.observations import Observation
from finance.augur.sim.prepared import CompiledRun, PreparedHoldingPool, PreparedLot, PreparedSeries
from finance.augur.sim.results import Executed
from finance.augur.sim.session import ActionSession
from finance.augur.sim.testing.accounting import (
    CASH,
    EXOGENOUS,
    HOUSEHOLD,
    OTHER,
    RECIPIENT,
    RESERVE,
    WORLD,
    prepared_scenario,
)
from finance.augur.sim.world import World


@pytest.fixture
def scoped() -> CompiledRun:
    base = prepared_scenario()
    lots = tuple(
        PreparedLot(
            lot_id=id_,
            agent_id=actor,
            account_id=account,
            asset_id=asset,
            purchase_month=-24,
            quantity_scale=10,
            units=units,
            basis=0,
        )
        for id_, actor, account, asset, units in (
            ("half-a", HOUSEHOLD, "checking", "stock", 5),
            ("half-b", HOUSEHOLD, "checking", "stock", 5),
            ("second", HOUSEHOLD, "checking", "second", 4),
            ("reserve", HOUSEHOLD, "savings", "stock", 5),
            ("other-actor", OTHER, "checking", "stock", 1000),
        )
    )
    pools = tuple(
        PreparedHoldingPool(agent_id=actor, account_id=account, asset_id=asset, quantity_scale=10)
        for actor, account, asset in (
            (HOUSEHOLD, "checking", "stock"),
            (HOUSEHOLD, "checking", "second"),
            (HOUSEHOLD, "savings", "stock"),
            (OTHER, "checking", "stock"),
        )
    )
    return CompiledRun(
        currency_code="USD",
        currency_quantum="0.01",
        rollout_count=1,
        scenario=replace(
            base,
            horizon_months=3,
            tax_profiles=(),
            initial_lots=lots,
            holding_pools=pools,
            accounts=tuple(
                replace(a, opening_balance={CASH: 100, RESERVE: 900, RECIPIENT: 5000}.get(a.account, 0))
                for a in base.accounts
            ),
        ),
        series=tuple(
            PreparedSeries(series_id=id_, snapshots=4, values=values)
            for id_, values in (
                ("inflation", (1_000_000_000, 1_500_000_000, 2_000_000_000, 3_000_000_000)),
                ("security:stock", (1, 3, 5, 7)),
                ("security:second", (2, 4, 6, 8)),
            )
        ),
    )


def world_for(run: CompiledRun) -> World:
    return World(run, 0, [], capture_mode="forensic", actor=HOUSEHOLD, product_actor=HOUSEHOLD)


def close(world: World) -> None:
    world.close_month(failed=False, shortfall=0, mortgages=[], snapshots=[])


def test_scoped_observations_match_output_at_same_marks_and_round_each_lot(scoped: CompiledRun) -> None:
    world = world_for(scoped)
    for month, value in enumerate((4, 8, 11)):
        world.prepare_month(month, {}, {})
        world.assemble_claims([])
        observed = world.observe(HOUSEHOLD)
        assert (observed.agent_id, observed.month, observed.cash, observed.public_holdings) == (
            HOUSEHOLD,
            month,
            1000,
            value,
        )
        assert observed.accounts == (("checking", 100), ("savings", 900))
        assert observed.cpi == ((1_000_000_000, 1_500_000_000, 2_000_000_000)[month], 1_000_000_000)
        first, second, sleeve, reserve = observed.public_positions
        assert (
            first.lot_id,
            first.account_id,
            first.asset_id,
            first.purchase_month,
            first.units,
            first.quantity_scale,
            first.book_basis,
            first.price,
        ) == ("half-a", "checking", "stock", -24, 5, 10, 0, (1, 3, 5)[month])
        assert [p.value for p in (first, second, sleeve, reserve)] == ([1, 1, 1, 1], [2, 2, 2, 2], [3, 3, 2, 3])[month]
        close(world)
    result = world.finish([])
    assert result.summary is not None
    assert result.financial is not None
    assert [sum(s.values[m] for s in result.summary.public_holdings) for m in range(4)] == [4, 8, 11, 15]
    assert [sum(s.values[m] for s in result.summary.cash) for m in range(4)] == [1000] * 4
    assert len(result.financial.months) == 4


def observations(run: CompiledRun) -> list[Observation]:
    world = world_for(run)
    seen = []
    for month in range(3):
        world.prepare_month(month, {}, {})
        world.assemble_claims([])
        seen.append(world.observe(HOUSEHOLD))
        close(world)
    return seen


def test_actor_books_do_not_read_future_prices_or_cpi(scoped: CompiledRun) -> None:
    changed = replace(
        scoped, series=tuple(replace(s, values=(*s.values[:2], *(v * 2 for v in s.values[2:]))) for s in scoped.series)
    )
    before, after = observations(scoped), observations(changed)
    assert before[:2] == after[:2]
    assert before[2].public_holdings != after[2].public_holdings
    assert before[2].cpi != after[2].cpi


def test_actor_books_follow_partial_sales_and_hide_exhausted_lots(scoped: CompiledRun) -> None:
    run = replace(
        scoped,
        scenario=replace(
            scoped.scenario,
            initial_lots=(replace(scoped.scenario.initial_lots[0], basis=7), *scoped.scenario.initial_lots[1:]),
        ),
    )
    world = world_for(run)
    for month in range(3):
        world.prepare_month(month, {}, {})
        world.assemble_claims([])
        observed = world.observe(HOUSEHOLD)
        if month < 2:
            first = observed.public_positions[0]
            assert (first.lot_id, first.units, first.book_basis) == ("half-a", (5, 3)[month], (7, 4)[month])
            lots = (
                (LotSale(account_id="checking", lot_id="half-a", units=2),)
                if month == 0
                else (
                    LotSale(account_id="checking", lot_id="half-a", units=3),
                    LotSale(account_id="checking", lot_id="half-b", units=5),
                )
            )
            assert isinstance(
                world.apply(
                    HOUSEHOLD,
                    Sell(
                        cause_id=f"sale-{month}",
                        agent_id=HOUSEHOLD,
                        proceeds_account_id="checking",
                        asset_id="stock",
                        lots=lots,
                    ),
                    0,
                ),
                Executed,
            )
        else:
            assert [p.lot_id for p in observed.public_positions] == ["second", "reserve"]
        close(world)
    result = world.finish([]).financial
    assert result is not None
    assert [(d.units, d.basis) for d in result.dispositions] == [(2, 3), (3, 4), (5, 0)]


def test_actor_books_reject_unpriced_public_positions_before_inspection(scoped: CompiledRun) -> None:
    invalid = replace(scoped, series=tuple(s for s in scoped.series if s.series_id != "security:second"))
    with patch("finance.augur.sim.session.World") as world:
        with pytest.raises(ValueError, match="security:second"):
            ActionSession(invalid, HOUSEHOLD, [0])
        world.assert_not_called()
    with pytest.raises(ValueError, match="unknown actor"):
        world_for(scoped).observe("absent-actor")


def test_claim_views_keep_assembled_amount_identity_and_payer_scope(scoped: CompiledRun) -> None:
    world = world_for(scoped)
    world.claims = Claims(
        3,
        [
            Claim("test-rent-m3", "rent", CASH, EXOGENOUS, 700, None),
            Claim("test-tax-m3", "estimated_tax", RESERVE, EXOGENOUS, 300, None),
            Claim("test-other-m3", "rent", RECIPIENT, EXOGENOUS, 9000, None),
        ],
    )
    first, second = world.observe(HOUSEHOLD).claims
    assert (
        first.month,
        first.index,
        first.cause_id,
        first.obligation_type,
        first.from_account,
        first.to_account,
        first.amount_due,
    ) == (3, 0, "test-rent-m3", "rent", CASH, EXOGENOUS, 700)
    assert (second.index, second.from_account, second.amount_due) == (1, RESERVE, 300)
    assert not world.observe(WORLD).claims
    world.claims.entries[0].amount_due = 725
    assert world.observe(HOUSEHOLD).claims[0].amount_due == 725
    world.claims.entries[0].paid = True
    assert [c.index for c in world.observe(HOUSEHOLD).claims] == [1]


if __name__ == "__main__":
    pytest_bazel.main()
