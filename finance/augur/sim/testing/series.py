"""Stipulated level paths as the integer USD series a world reads."""

from collections.abc import Mapping

import numpy as np
import numpy.typing as npt

from finance.augur.model.series import LevelSeriesKey
from finance.augur.sim.external_series import ExternalSeriesContext, compile_series
from finance.augur.sim.market_path import Series
from finance.augur.sim.money import USD


def level_series(
    levels: Mapping[LevelSeriesKey, npt.ArrayLike], *, rollout_count: int, horizon_months: int
) -> tuple[Series, ...]:
    """Each key's authored `(rollout, snapshot)` levels, one path per rollout and dense to the horizon."""
    return compile_series(
        ExternalSeriesContext.from_level_blocks(
            [(key, np.asarray(block, dtype=np.float64)) for key, block in levels.items()],
            rollout_count=rollout_count,
            horizon_months=horizon_months,
        ),
        rollout_count=rollout_count,
        horizon_months=horizon_months,
        currency=USD,
    )
