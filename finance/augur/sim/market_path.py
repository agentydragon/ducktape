"""Read one rollout's supplied exact market marks; no sampling or future-path policy access."""

from finance.augur.sim.money import mul_div
from finance.augur.sim.prepared import CompiledRun, PreparedAmount, PreparedFixedAmount


class MarketPath:
    def __init__(self, run: CompiledRun, rollout_id: int) -> None:
        if not 0 <= rollout_id < run.rollout_count:
            raise ValueError("invalid rollout selection")
        self.rollout_id = rollout_id
        self.series = {series.series_id: series for series in run.series}

    def value(self, series_id: str, month: int) -> int:
        series = self.series[series_id]
        if not 0 <= month < series.snapshots:
            raise ValueError(f"series {series_id!r} has no value at rollout {self.rollout_id} month {month}")
        return series.values[self.rollout_id * series.snapshots + month]

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
