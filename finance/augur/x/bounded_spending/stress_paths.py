"""Stipulated up/down/interior price paths for authoring and transport controls, not forecasts."""

import numpy as np

from finance.augur.model.series import InflationKey, SecurityKey
from finance.augur.sim.backend import CompiledRun, compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.study.trinity.replay import EQUITY, build_scenario


def prepare(*, rollout_count: int, horizon_months: int) -> CompiledRun:
    """Repeat three deterministic stress cases over a tax-free, sales-only equity sleeve."""
    prices = np.full((rollout_count, horizon_months + 1), 100.0)
    prices[::3, 12:] = 200.0
    prices[1::3, 12:] = 50.0
    prices[2::3, 12:] = 130.0
    cpi = np.ones_like(prices)
    cpi[:, 12:] = 1.25
    paths = ExternalSeriesContext.from_level_blocks(
        [(SecurityKey(symbol=EQUITY), prices), (InflationKey(), cpi)],
        rollout_count=rollout_count,
        horizon_months=horizon_months,
    )
    scenario = build_scenario(equity_share=1.0, withdrawal_rate=0.04).model_copy(
        update={"scheduled_obligations": [], "horizon_months": horizon_months}
    )
    return compile_run(scenario, rollout_count=rollout_count, external_series=paths, jurisdictions={}, locations={})
