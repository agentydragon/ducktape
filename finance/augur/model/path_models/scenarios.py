from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from finance.augur.model.series import LevelSeriesKey


@dataclass(frozen=True)
class HistoricalSeries:
    """Observed monthly levels, one column per series — the shared training/scoring input.

    `series_names`, not `factor_names`: these are OBSERVATIONS, and a factor is a private
    implementation concept of the vector-space/correlation models. Only those name their own
    basis, and they are free to fit a different set than they were shown; a per-series
    independent model has no basis at all, and a provider that returns a constant for
    everything is perfectly valid and never encounters the word.
    """

    series_names: tuple[LevelSeriesKey, ...]
    levels: np.ndarray
    months: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.levels.ndim != 2:
            raise ValueError(f"levels must be 2-D (T+1, F); got shape {self.levels.shape}")
        if self.levels.shape[1] != len(self.series_names):
            raise ValueError(
                f"levels has {self.levels.shape[1]} series columns but series_names has {len(self.series_names)}"
            )
        if self.levels.shape[0] != len(self.months):
            raise ValueError(f"levels has {self.levels.shape[0]} time rows but months has {len(self.months)}")
        if self.levels.shape[0] < 2:
            raise ValueError("HistoricalSeries needs at least two time rows")
        if not np.all(self.levels > 0):
            raise ValueError("levels must be strictly positive")


def historical_log_returns(historical: HistoricalSeries) -> np.ndarray:
    """diff(log(levels), axis=time) → (T, F)."""
    return np.diff(np.log(historical.levels), axis=0)
