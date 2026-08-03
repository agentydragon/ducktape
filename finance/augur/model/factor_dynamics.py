"""How a state-space factor's innovations become a path.

Every factor in the state-space basis draws its monthly innovation from one joint
covariance — that shared draw is what makes equities, inflation and rates fall
together in the states where they historically did, and it is the whole reason
rates belong in this basis rather than in a sampler of their own. What differs per
factor is the *recursion* that turns those innovations into a path:

  - a **level** (an index, a price, a rent) compounds: the innovation is a monthly
    log return, the path is `exp(log(level_0) + cumsum(innovations))`, and the
    result is positive by construction and free to wander without bound;
  - a **rate** does neither. A yield may sit at or below zero, so it has no log,
    and over a thirty-year horizon an undamped random walk in yield space wanders
    to absurdity — 20% or -5% — which would make a bond ladder's mark meaningless
    exactly where the ladder is supposed to be doing its job. A rate follows a
    mean-reverting (Ornstein-Uhlenbeck / AR(1)) recursion on its own level.

So `FactorDynamics` is not a free-form setting: it is determined by which
`FactorKey` variant the factor is, and `dynamics_for_factor` is the single place
that mapping lives. Only the *parameters* of mean reversion are fitted data.
"""

from __future__ import annotations

import math
from enum import StrEnum

from pydantic import Field

from finance.augur.model.schemas import FrozenModel
from finance.augur.model.series import MuniRatioKey, NominalYieldKey
from finance.augur.model.state_space_factor import FactorKey


class FactorDynamics(StrEnum):
    GEOMETRIC_RANDOM_WALK = "geometric_random_walk"
    """Innovation is a monthly log return; the path compounds and stays positive."""

    MEAN_REVERTING_RATE = "mean_reverting_rate"
    """Innovation is an additive shock to a rate that is pulled back toward a long-run level."""


class MeanReversionParams(FrozenModel):
    """Monthly Ornstein-Uhlenbeck parameters for a rate factor.

    `rate_t = rate_{t-1} + kappa * (theta - rate_{t-1}) + innovation_t`
    """

    kappa: float = Field(gt=0.0, le=1.0, description="Monthly pull toward theta; 1.0 snaps there in one month.")
    theta: float = Field(description="Long-run level of the rate, as a decimal (0.041 = 4.1%). May be non-positive.")


def dynamics_for_factor(factor: FactorKey) -> FactorDynamics:
    """The recursion a factor's innovations feed, determined by its typed identity."""

    if isinstance(factor, (NominalYieldKey, MuniRatioKey)):
        return FactorDynamics.MEAN_REVERTING_RATE
    return FactorDynamics.GEOMETRIC_RANDOM_WALK


def to_innovation_space(level: float, dynamics: FactorDynamics) -> float:
    """Project an observed factor level into the space its innovations are added in.

    A compounding factor advances in log space; a rate advances in its own units. Every
    start value, filtered state, and path recursion has to agree on this, so it lives here.
    """

    if dynamics is FactorDynamics.MEAN_REVERTING_RATE:
        return level
    return math.log(level)
