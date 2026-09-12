"""Stipulated up/down/interior price paths for authoring and transport controls, not forecasts."""

from collections.abc import Callable
from functools import partial

import numpy as np

from finance.augur.model.series import InflationKey, SecurityKey
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.world import World
from finance.augur.study.trinity.replay import EQUITY, situation
from finance.augur.x.bounded_spending.situation import compose


def sample(*, rollout_count: int, horizon_months: int) -> ExternalSeriesContext:
    """Repeat three deterministic price/CPI cases, not independent probability samples."""
    prices = np.full((rollout_count, horizon_months + 1), 100.0)
    prices[::3, 12:] = 200.0
    prices[1::3, 12:] = 50.0
    prices[2::3, 12:] = 130.0
    cpi = np.ones_like(prices)
    cpi[:, 12:] = 1.25
    return ExternalSeriesContext.from_level_blocks(
        [(SecurityKey(symbol=EQUITY), prices), (InflationKey(), cpi)],
        rollout_count=rollout_count,
        horizon_months=horizon_months,
    )


def equity_only(*, rollout_count: int, horizon_months: int) -> Callable[[int], World]:
    """A tax-free all-equity portfolio on the stipulated paths, composed per path id."""
    case = situation(
        sample(rollout_count=rollout_count, horizon_months=horizon_months),
        rollout_count=rollout_count,
        horizon_months=horizon_months,
    )
    return partial(compose, case, equity_share=1.0)
