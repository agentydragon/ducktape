"""Joint exogenous models, financial bindings and sampled/historical paths.

Fits and conditions on named observations, then produces compatible instrument
prices/cashflows and observable market history. Providers can have different fit
procedures and latent states. Binding connects their coordinates to instruments;
it does not make a total-return proxy adequate for taxed holdings.

The constructors below illustrate providers, not one mandatory model schema.
MarketBinding is specifically a log-index adapter; richer products need richer
adapters. This module can be used for forecast evaluation without any investor,
spending policy or financial simulation.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import polars as pl
from proposed_augur.data import Calendar, History, NamedSeries, ObservationsEncoder
from proposed_augur.instruments import Instrument
from proposed_augur.money import PriceIndex

type Fitters = Mapping[str, Callable[[NamedSeries], MarketModel]]

@dataclass(frozen=True)
class MarketBinding:
    """Encode named observations to model coordinates; decode log levels to named financial outputs."""

    observe: ObservationsEncoder
    total_return_indices: Mapping[Instrument, str]
    price_index: PriceIndex
    inflation_variable: str
    observable_columns: Mapping[str, str]
    encoding: Literal["log_levels"]

class FittedModel:
    def bind(self, binding: MarketBinding) -> MarketModel: ...

def load_vecm(artifact: Path) -> FittedModel: ...
def fit_vecm(
    state: pl.DataFrame,
    *,
    time: str,
    variables: tuple[str, ...],
    lagged_differences: int,
    cointegration_rank: int,
    deterministic: Literal["restricted_constant"],
) -> FittedModel: ...

class Worlds:
    """Immutable keyed paths; rows identify path/date and the relevant instrument or observable.

    Cashflows are per-unit product distributions with their character, not an
    investor's realized proceeds. The financial executor applies actual holdings.
    Prefixing preserves path identity. Policies receive only the observed prefix.
    """

    calendar: Calendar
    price_index: PriceIndex
    prices: pl.DataFrame
    cashflows: pl.DataFrame
    observables: pl.DataFrame
    def prefix(self, *, years: int | None = ..., months: int | None = ...) -> Worlds: ...

class Forecast:
    """A dated conditional distribution. Annual output compounds monthly dynamics, not relabels them."""

    origin: date
    price_index: PriceIndex
    def sample(self, *, years: int, step: Literal["month", "year"], paths: int, seed: int) -> Worlds: ...

class MarketModel:
    """A financially bound provider; conditioning produces a dated forecast before sampling."""

    price_index: PriceIndex
    def condition(self, observations: NamedSeries, *, at: date) -> Forecast: ...

class HistoricalMarket:
    price_index: PriceIndex
    def __init__(
        self,
        *,
        history: History,
        bindings: Mapping[Instrument, str],
        price_index: PriceIndex,
        inflation_column: str,
        observation_period: Literal["year", "month"],
    ) -> None: ...
    @classmethod
    def from_index_levels(
        cls,
        history: pl.DataFrame,
        *,
        bindings: Mapping[Instrument, str],
        price_index: PriceIndex,
        inflation_column: str,
    ) -> HistoricalMarket: ...
    def windows(
        self, *, first_year: int, last_year: int, years: int, stride_years: int, replay_start: date
    ) -> Worlds: ...

class JointBlockBootstrap(MarketModel):
    @classmethod
    def from_index_levels(
        cls,
        history: pl.DataFrame,
        *,
        bindings: Mapping[Instrument, str],
        price_index: PriceIndex,
        inflation_column: str,
        block_months: int,
        incomplete_blocks: Literal["exclude"],
        circular: bool,
    ) -> JointBlockBootstrap: ...

class AnnualJointLognormal:
    @classmethod
    def fit(
        cls,
        history: History,
        *,
        bindings: Mapping[Instrument, str],
        price_index: PriceIndex,
        inflation_column: str,
        moment_space: Literal["arithmetic_gross_returns"],
    ) -> MarketModel: ...

@dataclass(frozen=True)
class AnnualConvention:
    """NoTax study slots, not executor action-order rules.

    Annual price/CPI changes occur at opening of return_month; all other months
    are flat. Withdrawals/rebalances are authored policy actions in named slots.
    The synthetic slots do not represent observed intra-year market history.
    """

    name: str
    withdrawal_month: int
    return_month: int
    rebalance_month: int

def annual_study_grid(worlds: Worlds, *, convention: AnnualConvention) -> Worlds:
    """Expand annual gross index/CPI observations into declared synthetic months.

    Preserve annual endpoints, source dates, original path IDs and horizon. No
    smoothing, invented payouts or tax-aware product approximation. Require
    NoTax-compatible gross indices; annual studies choose this adapter explicitly.
    """
