"""Prepared facts are rejected before any rollout acquires mutable financial state."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import pytest
import pytest_bazel

from finance.augur.sim.prepared import (
    CompiledRun,
    PreparedDistribution,
    PreparedDistributionSlice,
    PreparedSeries,
    PreparedTlhPortfolio,
)
from finance.augur.sim.results import Finished
from finance.augur.sim.scenario import InterestIncome
from finance.augur.sim.session import ActionSession, _Session
from finance.augur.sim.tlh import TlhAssumptions
from finance.augur.sim.validation import validate
from finance.augur.x.monthly_actions.run import prepare


@pytest.fixture
def run() -> CompiledRun:
    return prepare()


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("bad_price", [0, -1])
def test_invalid_terminal_price_precedes_all_world_and_component_construction(
    run: CompiledRun, configured: bool, bad_price: int
) -> None:
    price = run.series[0]
    # The invalid mark is in the last snapshot of an unselected rollout.
    invalid = replace(run, series=(replace(price, values=(*price.values[:-1], bad_price)),))
    before = deepcopy(invalid)
    with (
        patch("finance.augur.sim.session.World") as world,
        patch("finance.augur.sim.session.TlhPortfolio") as portfolio,
    ):
        with pytest.raises(ValueError, match="non-positive value"):
            _Session(invalid, "example-household", [0], capture="forensic", configured=configured)
        world.assert_not_called()
        portfolio.assert_not_called()
    assert invalid == before
    # The original prepared authority remains reusable after the rejected input.
    session = ActionSession(run, "example-household", [0])
    try:
        assert not isinstance(session.start(), Finished)
    finally:
        session.close()


@pytest.mark.parametrize("malformation", ["missing", "duplicate", "short", "snapshots"])
def test_required_series_identity_and_shape(run: CompiledRun, malformation: str) -> None:
    price = run.series[0]
    paths = {
        "missing": (),
        "duplicate": (price, price),
        "short": (replace(price, values=price.values[:-1]),),
        "snapshots": (replace(price, snapshots=price.snapshots - 1),),
    }
    with pytest.raises(ValueError, match="series"):
        validate(replace(run, series=paths[malformation]))


def test_distribution_requires_declared_pool_not_a_cash_account(run: CompiledRun) -> None:
    distribution = PreparedDistribution(
        agent_id="example-household",
        holding_account_id="missing-pool",
        asset_id="example-stock",
        to_account_id="checking",
        tax_character=(PreparedDistributionSlice(fraction_ppb=1_000_000_000, issuer_jurisdiction_id=None),),
    )
    # Keep every unrelated distribution contract valid so only pool admission is tested.
    valid_distribution = replace(distribution, holding_account_id=run.scenario.holding_pools[0].account_id)
    payout = replace(
        run.series[0], series_id="security_distribution:example-stock", values=(0,) * len(run.series[0].values)
    )
    valid = replace(
        run,
        scenario=replace(
            run.scenario,
            distributions=(valid_distribution,),
            income_sources=(*run.scenario.income_sources, InterestIncome(issuer_jurisdiction_id=None)),
        ),
        series=(*run.series, payout),
    )
    validate(valid)
    with pytest.raises(ValueError, match="references no lots for example-household:missing-pool:example-stock"):
        validate(replace(valid, scenario=replace(valid.scenario, distributions=(distribution,))))


def test_zero_price_is_allowed_only_for_exclusively_managed_assets(run: CompiledRun) -> None:
    lot = run.scenario.initial_lots[0]
    portfolio = PreparedTlhPortfolio(
        portfolio_id="managed",
        owner_agent_id=lot.agent_id,
        account_id=lot.account_id,
        asset_id=lot.asset_id,
        quantity_scale=lot.quantity_scale,
        initial_cohorts=(lot,),
        assumptions=TlhAssumptions(
            peak_annual_yield=0,
            floor_annual_yield=0,
            maturity_decay_exponent=1,
            drawdown_sensitivity=0,
            short_term_fraction=1,
        ),
    )
    price = run.series[0]
    worthless = replace(
        run,
        scenario=replace(run.scenario, initial_lots=(), tlh_portfolios=(portfolio,)),
        series=(replace(price, values=(0,) * len(price.values)),),
    )
    session = ActionSession(worthless, lot.agent_id, [0])
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        [decision] = batch
        [observed] = decision.observation.tlh_portfolios
        assert observed.value == 0
        assert observed.reported_tax_basis == lot.basis
    finally:
        session.close()
    # A exclusively managed zero mark is valid without an ordinary pool too.
    validate(replace(worthless, scenario=replace(worthless.scenario, holding_pools=())))
    negative = replace(worthless.series[0], values=(*worthless.series[0].values[:-1], -1))
    with pytest.raises(ValueError, match="non-positive value -1"):
        validate(replace(worthless, series=(negative,)))
    # An ordinary lot or even an empty ordinary purchase pool sharing the quote
    # restores the positive-price requirement. Managed ownership is pool-scoped.
    with pytest.raises(ValueError, match="non-positive value"):
        validate(replace(worthless, scenario=replace(worthless.scenario, initial_lots=(lot,))))
    pool = replace(run.scenario.holding_pools[0], account_id="ordinary")
    with pytest.raises(ValueError, match="non-positive value"):
        validate(
            replace(worthless, scenario=replace(worthless.scenario, holding_pools=(*run.scenario.holding_pools, pool)))
        )


@pytest.mark.parametrize(
    ("channel", "value"), [("mark", -1), ("regime", 0), ("sale_capacity", 1_000_000_001), ("forced_recovery", -1)]
)
def test_imported_private_equity_channels_validate_terminal_values(run: CompiledRun, channel: str, value: int) -> None:
    channels = {
        "mark": 1,
        "regime": 1,
        "event_kind": 0,
        "sale_opportunity": 0,
        "sale_capacity": 1_000_000_000,
        "eligible": 1_000_000_000,
        "forced_sale": 0,
        "liquidity_blocked": 0,
        "forced_recovery": 0,
        "company_valuation": 1,
    }
    snapshots = run.scenario.horizon_months + 1
    private = replace(
        run,
        scenario=replace(
            run.scenario,
            initial_lots=tuple(replace(lot, asset_id="private_equity:issuer") for lot in run.scenario.initial_lots),
            holding_pools=tuple(replace(pool, asset_id="private_equity:issuer") for pool in run.scenario.holding_pools),
        ),
        series=tuple(
            PreparedSeries(
                series_id=f"private_equity_{name}:issuer",
                snapshots=snapshots,
                values=(level,) * (run.rollout_count * snapshots),
            )
            for name, level in channels.items()
        ),
    )
    validate(private)
    invalid = replace(
        private,
        series=tuple(
            replace(path, values=(*path.values[:-1], value))
            if path.series_id == f"private_equity_{channel}:issuer"
            else path
            for path in private.series
        ),
    )
    with pytest.raises(ValueError, match=f"invalid {channel} value"):
        validate(invalid)


if __name__ == "__main__":
    pytest_bazel.main()
