"""Plausibility gate: refuse or flag market paths an economy should not produce.

A band bounds one percentile of one measure at one horizon — "the two-year real equity wealth
factor's 99.99th percentile is at most 10" — and cites where the limit comes from. A `REFUSE`
band's breach stops the paths from producing an answer unless the caller records an
`Override`, whose use comes back as `OverriddenRefusal` records and is logged; a `FLAG` band's
breach is reported and the run goes on.
Each limit states what crossing it means: too optimistic, too pessimistic, too wide or too
narrow.

Bands read `MarketPaths` — equity total return, CPI and the rate state, which both historical
replay and the structural model produce — rather than a sampled product bundle, which carries
no rates. A band whose measure the paths lack (a model with no equity) is `Unmodeled`:
reported, never a pass, and blocking when the band refuses, because the gate cannot vouch for
what it did not check. Paths shorter than the bands' horizon are rejected outright.

With `n` rollouts, a percentile above `100 * (1 - 1/n)` (or below `100 / n`) is read off the
sample's extreme: such a band refuses on evidence, but its pass does not establish the tail
probability.

`sample_sanity` serves only the parked calibration endpoint's checks over sampled bundles.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import assert_never

import numpy as np
import yaml
from pydantic import Field, FiniteFloat, PositiveInt, model_validator

from finance.augur.model.bond_fund import government_curve_yield
from finance.augur.model.market_paths import MarketPaths
from finance.augur.model.schemas import FrozenModel

logger = logging.getLogger(__name__)


class Severity(StrEnum):
    REFUSE = "refuse"
    FLAG = "flag"


class BreachDirection(StrEnum):
    TOO_OPTIMISTIC = "too_optimistic"
    TOO_PESSIMISTIC = "too_pessimistic"
    TOO_WIDE = "too_wide"
    TOO_NARROW = "too_narrow"


class Measure(StrEnum):
    """What a band bounds, read at its horizon month of every rollout."""

    # The equity total-return index relative to month 0: what a dollar invested then is worth.
    NOMINAL_EQUITY_WEALTH_FACTOR = "nominal_equity_wealth_factor"
    # The nominal factor divided by `INFLATION_FACTOR` over the same months.
    REAL_EQUITY_WEALTH_FACTOR = "real_equity_wealth_factor"
    # CPI relative to month 0.
    INFLATION_FACTOR = "inflation_factor"
    # Annualized decimal, as the model produced it, before product construction floors it.
    SHORT_RATE = "short_rate"
    # Short rate plus term spread: the government curve at ten years.
    TEN_YEAR_YIELD = "ten_year_yield"


class Limit(FrozenModel):
    value: FiniteFloat
    breach: BreachDirection = Field(description="What a model whose percentile crosses `value` gets wrong.")


class Band(FrozenModel):
    measure: Measure
    horizon_months: PositiveInt = Field(description="Months after the paths' opening observation.")
    percentile: float = Field(ge=0.0, le=100.0)
    lower: Limit | None = None
    upper: Limit | None = None
    severity: Severity
    source: str = Field(min_length=1, description="Where the limits come from, precisely enough to check them.")

    @model_validator(mode="after")
    def _bounded(self) -> Band:
        if self.lower is None and self.upper is None:
            raise ValueError(f"{self.label} has no limit")
        if self.lower is not None and self.upper is not None and self.lower.value > self.upper.value:
            raise ValueError(f"{self.label}: lower limit {self.lower.value:g} is above upper {self.upper.value:g}")
        return self

    @property
    def label(self) -> str:
        return f"{self.measure} p{self.percentile:g} at month {self.horizon_months}"


class BandFile(FrozenModel):
    bands: tuple[Band, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_band_per_statistic(self) -> BandFile:
        repeated = sorted(label for label, count in Counter(band.label for band in self.bands).items() if count > 1)
        if repeated:
            raise ValueError(f"bands repeat a measure, percentile and horizon: {repeated}")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> BandFile:
        """Load a YAML (or JSON) band file."""

        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    @property
    def horizon_months(self) -> int:
        """How far paths must reach for every band to be checked."""

        return max(band.horizon_months for band in self.bands)


class Verdict(StrEnum):
    PASS = "pass"
    FLAG = "flag"
    REFUSE = "refuse"
    UNMODELED = "unmodeled"


@dataclass(frozen=True)
class Passed:
    band: Band
    value: float

    @property
    def verdict(self) -> Verdict:
        return Verdict.PASS

    def __str__(self) -> str:
        return f"{self.verdict} {self.band.label}: {self.value:.4g} within {_interval(self.band)}"


@dataclass(frozen=True)
class Breached:
    band: Band
    value: float
    # The limit `value` crossed; its `breach` is the direction.
    limit: Limit

    @property
    def verdict(self) -> Verdict:
        return Verdict.REFUSE if self.band.severity is Severity.REFUSE else Verdict.FLAG

    def __str__(self) -> str:
        return (
            f"{self.verdict} {self.band.label}: {self.value:.4g} outside {_interval(self.band)}, "
            f"{self.limit.breach} (source: {self.band.source})"
        )


@dataclass(frozen=True)
class Unmodeled:
    """The paths carry no series for the band's measure."""

    band: Band

    @property
    def verdict(self) -> Verdict:
        return Verdict.UNMODELED

    def __str__(self) -> str:
        return f"{self.verdict} {self.band.label}: the paths carry no series for it ({self.band.severity} band)"


type Outcome = Passed | Breached | Unmodeled


def _interval(band: Band) -> str:
    lower = "-inf" if band.lower is None else f"{band.lower.value:g}"
    upper = "inf" if band.upper is None else f"{band.upper.value:g}"
    return f"[{lower}, {upper}]"


@dataclass(frozen=True)
class GateResult:
    model_id: str
    rollout_count: int
    # One per band, in band-file order.
    outcomes: tuple[Outcome, ...]

    @property
    def blocking(self) -> tuple[Breached | Unmodeled, ...]:
        """`REFUSE` bands the paths breached or could not be checked against."""

        return tuple(
            outcome
            for outcome in self.outcomes
            if not isinstance(outcome, Passed) and outcome.band.severity is Severity.REFUSE
        )

    def __str__(self) -> str:
        lines = "".join(f"\n  {outcome}" for outcome in self.outcomes)
        return f"plausibility of {self.model_id} paths ({self.rollout_count} rollouts):{lines}"


def evaluate(paths: MarketPaths, bands: BandFile) -> GateResult:
    """Check every band against `paths`; `require_plausible` decides whether they may answer."""

    if bands.horizon_months > paths.horizon_months:
        raise ValueError(
            f"bands reach month {bands.horizon_months} but {paths.model_id} paths end at month "
            f"{paths.horizon_months}; sample at least the bands' horizon"
        )
    measures = {measure: _measure(paths, measure) for measure in {band.measure for band in bands.bands}}
    return GateResult(
        model_id=paths.model_id,
        rollout_count=paths.rollout_count,
        outcomes=tuple(_check(band, measures[band.measure]) for band in bands.bands),
    )


def _measure(paths: MarketPaths, measure: Measure) -> np.ndarray | None:
    """`(rollout, month)` values of `measure`, or `None` when the paths carry no series for it."""

    match measure:
        case Measure.NOMINAL_EQUITY_WEALTH_FACTOR:
            # `MarketPaths` starts the index at one, so it is the wealth factor itself.
            return paths.equity_total_return_index
        case Measure.REAL_EQUITY_WEALTH_FACTOR:
            equity = paths.equity_total_return_index
            return None if equity is None else np.asarray(equity / _inflation_factor(paths))
        case Measure.INFLATION_FACTOR:
            return _inflation_factor(paths)
        case Measure.SHORT_RATE:
            return paths.short_rate
        case Measure.TEN_YEAR_YIELD:
            return government_curve_yield(paths.short_rate, paths.term_spread, maturity_years=10.0)
        case _:
            assert_never(measure)


def _inflation_factor(paths: MarketPaths) -> np.ndarray:
    return np.asarray(paths.cpi_level / paths.cpi_level[:, :1])


def _check(band: Band, values: np.ndarray | None) -> Outcome:
    if values is None:
        return Unmodeled(band=band)
    value = float(np.percentile(values[:, band.horizon_months], band.percentile))
    if band.lower is not None and value < band.lower.value:
        return Breached(band=band, value=value, limit=band.lower)
    if band.upper is not None and value > band.upper.value:
        return Breached(band=band, value=value, limit=band.upper)
    return Passed(band=band, value=value)


@dataclass(frozen=True)
class Override:
    """A caller's recorded decision to answer despite the blocking bands it names."""

    bands: frozenset[Band]
    reason: str

    def __post_init__(self) -> None:
        if not self.bands or not self.reason.strip():
            raise ValueError("an override names the blocking bands it accepts and says why")


@dataclass(frozen=True)
class OverriddenRefusal:
    """A blocking band an `Override` accepted: the outcome (with value and limit when the band was
    checked) and the caller's reason, for an answer to report beside its numbers."""

    outcome: Breached | Unmodeled
    reason: str

    def __str__(self) -> str:
        return f"overridden ({self.reason}): {self.outcome}"


class RefusalError(Exception):
    """Paths breached a `REFUSE` band, or lack its measure, and no `Override` accepts it."""

    def __init__(self, result: GateResult, refusals: tuple[Breached | Unmodeled, ...]) -> None:
        self.refusals = refusals
        lines = "".join(f"\n  {outcome}" for outcome in refusals)
        super().__init__(f"{result.model_id} paths ({result.rollout_count} rollouts) refused:{lines}")


def require_plausible(result: GateResult, *, override: Override | None = None) -> tuple[OverriddenRefusal, ...]:
    """Raise `RefusalError` for any blocking band `override` does not name.

    Returns a record of each blocking band the override accepted, each also logged at WARNING.
    """

    accepted: frozenset[Band] = frozenset() if override is None else override.bands
    if refusals := tuple(outcome for outcome in result.blocking if outcome.band not in accepted):
        raise RefusalError(result, refusals)
    if override is None:
        return ()
    overridden = tuple(OverriddenRefusal(outcome=outcome, reason=override.reason) for outcome in result.blocking)
    for record in overridden:
        logger.warning("plausibility override for %s: %s", result.model_id, record)
    return overridden
