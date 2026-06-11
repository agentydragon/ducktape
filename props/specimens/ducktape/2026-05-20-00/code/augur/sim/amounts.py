"""Coerce legacy scalar amounts before evaluating configured amount schedules."""

from __future__ import annotations

import polars as pl

from augur.sim.market import MarketContext
from augur.sim.scenario import AmountSpec, FixedAmount, MarketIndexedAmount


def amount_by_rollout(
    amount: AmountSpec, *, market: MarketContext, rollouts: pl.DataFrame, month: int, column_name: str
) -> pl.DataFrame:
    """Return `(rollout_index, column_name)` for a configured amount."""

    schedule = (
        amount if isinstance(amount, (FixedAmount, MarketIndexedAmount)) else FixedAmount(amount_usd=float(amount))
    )
    return schedule.amount_by_rollout(market=market, rollouts=rollouts, month=month, column_name=column_name)
