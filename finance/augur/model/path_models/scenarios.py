from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from finance.augur.model.series import LevelSeriesKey


def calendar_month_index(month: str) -> int:
    try:
        parsed = date.fromisoformat(f"{month}-01")
    except ValueError as error:
        raise ValueError(f"calendar month must use YYYY-MM; got {month!r}") from error
    if parsed.strftime("%Y-%m") != month:
        raise ValueError(f"calendar month must use YYYY-MM; got {month!r}")
    return parsed.year * 12 + parsed.month


def require_increasing_calendar_months(months: Sequence[str]) -> None:
    month_indices = tuple(calendar_month_index(month) for month in months)
    for earlier, later, earlier_index, later_index in zip(
        months[:-1], months[1:], month_indices[:-1], month_indices[1:], strict=True
    ):
        if later_index <= earlier_index:
            raise ValueError(f"calendar months must be unique and increasing; found {earlier} followed by {later}")


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

    def require_consecutive_months(self) -> None:
        """Require each adjacent row to represent one calendar-month transition."""
        require_increasing_calendar_months(self.months)
        month_indices = tuple(calendar_month_index(month) for month in self.months)
        for earlier, later, earlier_index, later_index in zip(
            self.months[:-1], self.months[1:], month_indices[:-1], month_indices[1:], strict=True
        ):
            if later_index != earlier_index + 1:
                raise ValueError(
                    "HistoricalSeries requires consecutive calendar months for monthly fitting and scoring; "
                    f"found {earlier} followed by {later}"
                )


def historical_log_returns(historical: HistoricalSeries) -> np.ndarray:
    """diff(log(levels), axis=time) → (T, F)."""
    historical.require_consecutive_months()
    return np.diff(np.log(historical.levels), axis=0)
