"""Read one rollout's supplied exact market marks; no sampling or future-path policy access."""

from __future__ import annotations

from collections.abc import Iterable

from finance.augur.sim.actor import Statement
from finance.augur.sim.money import mul_div
from finance.augur.sim.prepared import CompiledRun, PreparedAmount, PreparedFixedAmount, PreparedSeries


class MarketStatement(Statement):
    """Current/origin CPI, absent only when inflation is unmodeled."""

    cpi: tuple[int, int] | None


class MarketPath:
    """One rollout's view of the supplied series populations."""

    def __init__(self, series: Iterable[PreparedSeries], rollout_id: int, *, rollout_count: int) -> None:
        if not 0 <= rollout_id < rollout_count:
            raise ValueError("invalid rollout selection")
        self.rollout_id = rollout_id
        self.rollout_count = rollout_count
        self.series: dict[str, PreparedSeries] = {}
        for row in series:
            if row.series_id in self.series:
                raise ValueError(f"duplicate series {row.series_id!r}")
            if len(row.values) != rollout_count * row.snapshots:
                raise ValueError(
                    f"series {row.series_id!r} has invalid shape; expected {rollout_count} x {row.snapshots}"
                )
            self.series[row.series_id] = row

    @classmethod
    def from_run(cls, run: CompiledRun, rollout_id: int) -> MarketPath:
        return cls(run.series, rollout_id, rollout_count=run.rollout_count)

    def value(self, series_id: str, month: int) -> int:
        series = self.series[series_id]
        if not 0 <= month < series.snapshots:
            raise ValueError(f"series {series_id!r} has no value at rollout {self.rollout_id} month {month}")
        return series.values[self.rollout_id * series.snapshots + month]

    def path(self, series_id: str) -> list[int]:
        """Every snapshot this rollout reads of one series, the terminal one included."""
        return [self.value(series_id, month) for month in range(self.series[series_id].snapshots)]

    def require_prices(self, series_id: str) -> None:
        """A price series is a price at every snapshot: zero is refused, not read as worthless."""
        for month, value in enumerate(self.path(series_id)):
            if value <= 0:
                raise ValueError(f"series {series_id!r} has non-positive value {value} at month {month}")

    def statement(self, month: int) -> MarketStatement:
        return MarketStatement(
            month=month,
            cpi=(self.value("inflation", month), self.value("inflation", 0)) if "inflation" in self.series else None,
        )

    def amount(self, amount: PreparedAmount, month: int) -> int:
        if isinstance(amount, int):
            return amount
        if isinstance(amount, PreparedFixedAmount):
            return amount.amount
        elapsed = month - amount.base_month_index
        if elapsed < 0:
            raise ValueError("indexed payment precedes its base month")
        reset = amount.base_month_index + elapsed // amount.adjustment_period_months * amount.adjustment_period_months
        return mul_div(
            amount.base_amount,
            self.value(amount.series_id, reset),
            self.value(amount.series_id, amount.base_month_index),
            "series-indexed amount",
        )
