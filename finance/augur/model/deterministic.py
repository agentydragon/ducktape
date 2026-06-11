"""Deterministic scalar exogenous models."""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import BaseModel


class Deterministic(BaseModel):
    """A fixed per-month level curve.

    `levels` runs from month 0 through `horizon_months`
    inclusive; sampling validates that its length matches the
    requested horizon.
    """

    kind: Literal["deterministic"] = "deterministic"
    levels: list[float]

    def sample_levels(self, *, rollout_seeds: tuple[int, ...], horizon_months: int) -> np.ndarray:
        expected = horizon_months + 1
        if len(self.levels) != expected:
            msg = f"Deterministic model has {len(self.levels)} levels; need {expected}"
            raise ValueError(msg)
        levels = np.asarray(self.levels, dtype=np.float64)
        return np.tile(levels, (len(rollout_seeds), 1))


class Constant(BaseModel):
    """A constant level shared across every rollout and month."""

    kind: Literal["constant"] = "constant"
    value: float

    def sample_levels(self, *, rollout_seeds: tuple[int, ...], horizon_months: int) -> np.ndarray:
        return np.full((len(rollout_seeds), horizon_months + 1), self.value, dtype=np.float64)
