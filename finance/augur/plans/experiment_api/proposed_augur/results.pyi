"""Trajectory inspection, experiment-owned reductions and forecast scoring.

Provides per-path outcomes and replayable traces, plus reusable accounting metrics
and statistical scores. The experiment chooses success definitions, comparison
grids and any selection criterion. Observer names are supplied by the author,
not an engine registry of every policy's outcomes. Historical-window frequencies,
sampling uncertainty and differences between models remain distinct.

No web cache or retention of every ledger in RAM is required for a result receipt.
Forecast scoring can consume market paths directly, without a financial rollout.
"""

from collections.abc import Callable, Mapping
from typing import Literal

import polars as pl
from proposed_augur.data import NamedSeries
from proposed_augur.markets import Worlds
from proposed_augur.money import FloatArray

type Runs = dict[tuple[str | int | float | bool, ...], Run]

type StudyResult = tuple[pl.DataFrame, Runs]

class Events:
    frame: pl.DataFrame

class Observer:
    """Reduce actor-scoped events to path_id/value rows, joined by key rather than row order."""

    def __init__(self, reduce: Callable[[Events], pl.DataFrame]) -> None: ...

type FinancialMetric = Literal[
    "terminal_wealth_nominal",
    "terminal_wealth_real",
    "terminal_liquid_wealth_real",
    "total_spending_real",
    "minimum_annual_spending_real",
    "final_spending_real",
    "housing_cost_real",
    "tax_paid_real",
    "mortgage_balance_real",
]

def financial_observers(*names: FinancialMetric) -> dict[str, Observer]: ...

class PathResults:
    paths: pl.DataFrame

class Run(PathResults):
    """Convenience-run result retaining its supplied initializers for fresh replay."""

    def trace(self, path_id: str) -> Events: ...

class LogGrowth:
    columns: tuple[str, ...]
    def __init__(self, *, columns: tuple[str, ...]) -> None: ...
    def scale_from(self, observations: NamedSeries) -> FloatArray: ...
    def forecast(self, worlds: Worlds) -> FloatArray: ...
    def observed(self, frame: pl.DataFrame) -> FloatArray: ...

def energy_score(samples: FloatArray, actual: FloatArray) -> float: ...
def variogram_score(samples: FloatArray, actual: FloatArray, *, power: float) -> float: ...

type PolicySelector = Callable[[pl.DataFrame, Mapping[str, Run]], str | None]
