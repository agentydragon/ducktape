"""A Python policy on composed worlds: the path's CPI, selected replay and an experiment's own claim labels."""

from decimal import Decimal

import numpy as np
import pytest_bazel

from finance.augur.model.series import InflationKey
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.ids import AccountId, AgentId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import PreparedAccount, PreparedObligation
from finance.augur.sim.results import Finished
from finance.augur.sim.scenario import ORDINARY_INCOME
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import World
from finance.augur.x.bounded_spending.python_policy import BatchPolicy, Parameters, SpendingPolicy, consumption, run

HORIZON = 13


def _retiree(market: MarketPath) -> World:
    """A retiree with USD 100 and the world she spends into; nothing taxed."""
    world = World(market, horizon_months=HORIZON, income_sources=(ORDINARY_INCOME,))
    for name, balance in ((AgentId("retiree"), 10_000), (AgentId("world"), 0)):
        world.declare_account(
            PreparedAccount(
                account=AccountRef(agent_id=name, account_id=AccountId("checking")), opening_balance=balance
            )
        )
    return world


def test_each_path_keeps_its_own_cpi_and_selected_replay_matches_the_population() -> None:
    cpi = np.ones((3, HORIZON + 1))
    cpi[:, 12:] = np.asarray([2.0, 1.0, 3.0])[:, None]
    series = compile_series(
        ExternalSeriesContext.from_level_blocks([(InflationKey(), cpi)], rollout_count=3, horizon_months=HORIZON),
        rollout_count=3,
        horizon_months=HORIZON,
        currency_quantum=Decimal("0.01"),
    )

    def compose(rollout_id: int) -> World:
        return _retiree(MarketPath(series, rollout_id, rollout_count=3))

    parameters = Parameters(400, 0, 0)
    baseline = run(compose, SpendingPolicy(BatchPolicy(parameters, 3), {}), [0, 1, 2])
    assert [row[12] for row in consumption(baseline)[1]] == [800, 400, 1200]
    replay = run(compose, SpendingPolicy(BatchPolicy(parameters, 3), {}), [2, 0], capture="forensic")
    assert [row.rollout_id for row in replay.rollouts] == [2, 0]
    assert [row.summary for row in replay.rollouts] == [baseline.rollouts[id_].summary for id_ in [2, 0]]


def test_an_experiment_defined_claim_label_reaches_the_policy() -> None:
    world = _retiree(MarketPath((), 0, rollout_count=1))
    world.track(
        Biller(
            PreparedObligation(
                month=0,
                obligation_id="test-outflow",
                obligation_type="experiment:annual-outflow",
                from_account=AccountRef(agent_id=AgentId("retiree"), account_id=AccountId("checking")),
                to_account=AccountRef(agent_id=AgentId("world"), account_id=AccountId("checking")),
                amount_due=15_000,
                property_id=None,
                deduction_category=None,
                deductible_fraction_ppb=1_000_000_000,
            )
        )
    )
    session = ActionSession({0: world}, AgentId("retiree"))
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        [observed] = batch[0].observation.claims
        assert observed.obligation_type == "experiment:annual-outflow"
        assert observed.amount_due == 15_000
    finally:
        session.close()


if __name__ == "__main__":
    pytest_bazel.main()
