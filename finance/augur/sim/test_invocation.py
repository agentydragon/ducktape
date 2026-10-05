"""A Python policy on composed worlds: the path's CPI and selected replay."""

from collections.abc import Callable

import numpy as np
import pytest_bazel

from finance.augur.model.series import InflationKey
from finance.augur.sim.actions import Action, Consume, DecisionActions
from finance.augur.sim.books import AccountRef
from finance.augur.sim.ids import AccountId, AgentId
from finance.augur.sim.income import ORDINARY_INCOME
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import mul_div
from finance.augur.sim.observations import Decision
from finance.augur.sim.results import Finished
from finance.augur.sim.session import ActionSession
from finance.augur.sim.testing.series import level_series
from finance.augur.sim.testing.session import finish
from finance.augur.sim.world import Capture, World

HORIZON = 13


def _retiree(market: MarketPath) -> World:
    """A retiree with USD 100 and the world she spends into; nothing taxed."""
    world = World(market, horizon_months=HORIZON, income_sources=(ORDINARY_INCOME,))
    for name, balance in ((AgentId("retiree"), 10_000), (AgentId("world"), 0)):
        world.declare_account(
            account=AccountRef(agent_id=name, account_id=AccountId("checking")), opening_balance=balance
        )
    return world


def _spend_indexed(batch: list[Decision]) -> list[DecisionActions]:
    """Each year from month zero, consume USD 4 of month-zero money grown by the path's own CPI."""
    responses = []
    for decision in batch:
        observation = decision.observation
        assert observation.cpi is not None
        current, origin = observation.cpi
        actions: list[Action] = (
            []
            if observation.month % 12
            else [
                Consume(
                    request_id=0,
                    cause_id=f"annual_consumption_m{observation.month}",
                    component_id="annual_consumption",
                    from_account=AccountRef(agent_id=AgentId("retiree"), account_id=AccountId("checking")),
                    to_account=AccountRef(agent_id=AgentId("world"), account_id=AccountId("checking")),
                    amount=mul_div(400, current, origin, "indexed consumption"),
                )
            ]
        )
        responses.append(DecisionActions(decision.rollout_id, observation.month, actions))
    return responses


def _run(compose: Callable[[int], World], rollout_ids: list[int], capture: Capture) -> Finished:
    return finish(
        ActionSession({id_: compose(id_) for id_ in rollout_ids}, AgentId("retiree"), capture=capture), _spend_indexed
    )


def test_each_path_keeps_its_own_cpi_and_selected_replay_matches_the_population() -> None:
    cpi = np.ones((3, HORIZON + 1))
    cpi[:, 12:] = np.asarray([2.0, 1.0, 3.0])[:, None]
    series = level_series({InflationKey(): cpi}, rollout_count=3, horizon_months=HORIZON)

    def compose(rollout_id: int) -> World:
        return _retiree(MarketPath(series, rollout_id, rollout_count=3))

    baseline = _run(compose, [0, 1, 2], "summary")
    assert [
        row.receipt.amount_paid for rollout in baseline.rollouts for row in rollout.summary.payments if row.month == 12
    ] == [800, 400, 1200]
    replay = _run(compose, [2, 0], "forensic")
    assert [row.rollout_id for row in replay.rollouts] == [2, 0]
    assert [row.summary for row in replay.rollouts] == [baseline.rollouts[id_].summary for id_ in [2, 0]]


if __name__ == "__main__":
    pytest_bazel.main()
