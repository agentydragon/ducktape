from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

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
class CalendarSegment:
    """One run of observed levels with no missing calendar month between rows."""

    start_index: int
    end_index: int
    months: tuple[str, ...]
    levels: np.ndarray

    @property
    def transition_count(self) -> int:
        return len(self.months) - 1


@dataclass(frozen=True)
class CalendarGap:
    """Missing calendar months between two observed level rows."""

    before_month: str
    after_month: str
    missing_months: tuple[str, ...]


@dataclass(frozen=True)
class DatedHistoricalSeries:
    """Observed monthly levels that may contain calendar gaps.

    Unlike `HistoricalSeries`, this type does not imply that adjacent rows are one-month
    transitions. Consumers must use `segments` or `transition_pairs`; the type deliberately
    provides no `diff` helper that could bridge a missing month.
    """

    series_names: tuple[LevelSeriesKey, ...]
    levels: np.ndarray
    months: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.levels.ndim != 2:
            raise ValueError(f"levels must be 2-D (T, F); got shape {self.levels.shape}")
        if self.levels.shape[1] != len(self.series_names):
            raise ValueError(
                f"levels has {self.levels.shape[1]} series columns but series_names has {len(self.series_names)}"
            )
        if self.levels.shape[0] != len(self.months):
            raise ValueError(f"levels has {self.levels.shape[0]} time rows but months has {len(self.months)}")
        if self.levels.shape[0] < 1:
            raise ValueError("DatedHistoricalSeries needs at least one observed level row")
        if not np.all(np.isfinite(self.levels)) or not np.all(self.levels > 0):
            raise ValueError("levels must be finite and strictly positive")
        require_increasing_calendar_months(self.months)

    @property
    def transition_pairs(self) -> tuple[tuple[int, int], ...]:
        """Index pairs whose endpoints are exactly one calendar month apart."""
        month_indices = tuple(calendar_month_index(month) for month in self.months)
        return tuple(
            (index, index + 1) for index, (earlier, later) in enumerate(pairwise(month_indices)) if later == earlier + 1
        )

    @property
    def segments(self) -> tuple[CalendarSegment, ...]:
        """All consecutive observed runs, including single-row runs with no transitions."""
        if not self.months:
            return ()
        month_indices = tuple(calendar_month_index(month) for month in self.months)
        bounds: list[tuple[int, int]] = []
        start = 0
        for index, (earlier, later) in enumerate(pairwise(month_indices)):
            if later != earlier + 1:
                bounds.append((start, index))
                start = index + 1
        bounds.append((start, len(self.months) - 1))
        return tuple(
            CalendarSegment(
                start_index=start,
                end_index=end,
                months=self.months[start : end + 1],
                levels=self.levels[start : end + 1],
            )
            for start, end in bounds
        )

    @property
    def gaps(self) -> tuple[CalendarGap, ...]:
        gaps: list[CalendarGap] = []
        month_indices = tuple(calendar_month_index(month) for month in self.months)
        for index, (earlier, later) in enumerate(pairwise(month_indices)):
            if later <= earlier + 1:
                continue
            missing = tuple(_calendar_month_from_index(value) for value in range(earlier + 1, later))
            gaps.append(CalendarGap(self.months[index], self.months[index + 1], missing))
        return tuple(gaps)

    def prefix_through(self, row_index: int) -> DatedHistoricalSeries:
        """Observed level rows available through `row_index`, retaining gaps as boundaries."""
        if not 0 <= row_index < len(self.months):
            raise IndexError(f"row_index {row_index} outside 0..{len(self.months) - 1}")
        return DatedHistoricalSeries(
            series_names=self.series_names, levels=self.levels[: row_index + 1], months=self.months[: row_index + 1]
        )

    def historical_segments(self) -> tuple[HistoricalSeries, ...]:
        """Return only runs with at least one valid transition as guarded histories."""
        return tuple(
            HistoricalSeries(series_names=self.series_names, levels=segment.levels, months=segment.months)
            for segment in self.segments
            if segment.transition_count > 0
        )


def _calendar_month_from_index(index: int) -> str:
    year, month_zero_based = divmod(index - 1, 12)
    return f"{year:04d}-{month_zero_based + 1:02d}"


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
