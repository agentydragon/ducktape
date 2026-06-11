"""Poisson-process boolean event sampler."""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field


class PoissonEvents(BaseModel):
    """Independent Bernoulli draw per month with probability `monthly_lambda`.

    Events are sampled for months 1..horizon_months; month 0 is always False.
    Returns an all-False mask when `horizon_months < min_horizon_months` —
    used to suppress events that require a minimum lookahead (e.g.
    private-equity sale opportunities only meaningful past a 12-month horizon).
    """

    kind: Literal["poisson"] = "poisson"
    monthly_lambda: float = Field(ge=0.0, le=1.0)
    min_horizon_months: int = Field(default=0, ge=0)

    def sample_events(self, *, rollout_seeds: tuple[int, ...], horizon_months: int) -> np.ndarray:
        active = np.zeros((len(rollout_seeds), horizon_months + 1), dtype=np.bool_)
        if horizon_months < self.min_horizon_months:
            return active
        for rollout_index, seed in enumerate(rollout_seeds):
            active[rollout_index, 1:] = np.random.default_rng(seed).random(horizon_months) < self.monthly_lambda
        return active
