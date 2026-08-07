"""Replay the past: one rollout per historical starting month.

Rollout `i` is "what if you had started in month `i` of the record and lived through exactly
what followed". No parameters, no distributional assumptions, no fit — the paths ARE the data.

**Why this exists next to `structural_macro`.** That model is a fitted Gaussian VAR, and a
fitted Gaussian VAR gets specific things wrong in ways its own diagnostics cannot show: it has
no fat tails, no volatility clustering, no valuation feedback, and — most damaging for a
CPI-indexed spender — equity independent of inflation, so it cannot produce the stagflation
that actually hurt retirees in 1973. Historical replay gets every one of those right for free,
because it never assumes anything about the joint distribution; it just uses the one draw
history handed us.

**And what it gets wrong in exchange, which is severe.** The windows OVERLAP. With 46 years of
aligned data and a 30-year horizon there are ~199 of them, and consecutive windows share 359 of
360 months — so the effective sample is closer to **1.5 independent observations** than to 199.
A "P[ruin] = 4%" from this is not a probability. It is "8 of the 199 historical starting months
would have failed", and those 8 are almost certainly one contiguous episode counted 8 times.
`window_count` and `independent_window_estimate` are on the result so a caller cannot quietly
forget that.

Use the two together and disagree loudly: the fitted model gives smooth probabilities over
scenarios that never happened, and this gives a handful of scenarios that definitely did. When
they agree, the answer is robust to the modelling choice. When they diverge, the divergence is
the finding — and neither number is the truth.

The INSTRUMENT layer is shared with `structural_macro` (`instrument_paths`), deliberately: how
a fund responds to a yield change is a claim about the fund, not about the economy, so the two
providers must not differ on it or their outputs are not comparable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from finance.augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle, assemble_level_frames
from finance.augur.model.series import InflationKey, IssuerId, LevelSeriesKey, SecurityDistributionKey, SecurityKey
from finance.augur.model.structural_macro import MINIMUM_ANNUAL_YIELD, EquitySpec, InstrumentSpec, instrument_paths


@dataclass(frozen=True)
class MacroHistory:
    """The aligned monthly record every window is cut from.

    All four arrays share one month index, so a window is a slice and nothing can drift out of
    alignment. `short_rate` and `term_spread` are annualized decimals; `equity_level` is a
    total-return index and `cpi_level` a price index, both in arbitrary units because only
    their ratios within a window are ever used.
    """

    short_rate: np.ndarray
    term_spread: np.ndarray
    equity_level: np.ndarray
    cpi_level: np.ndarray

    def __post_init__(self) -> None:
        lengths = {len(self.short_rate), len(self.term_spread), len(self.equity_level), len(self.cpi_level)}
        if len(lengths) != 1:
            raise ValueError(f"macro history series have different lengths: {sorted(lengths)}")
        if np.any(self.equity_level <= 0.0) or np.any(self.cpi_level <= 0.0):
            raise ValueError("equity and CPI levels must be strictly positive to be rebased")

    @property
    def months(self) -> int:
        return len(self.short_rate)


@dataclass(frozen=True)
class HistoricalWindowsModel:
    """A `Sampler` whose rollouts are contiguous slices of the record.

    Implements `Sampler` only. There is nothing to fit and nothing to score: the parameters
    are the past.
    """

    history: MacroHistory
    instruments: tuple[InstrumentSpec, ...] = ()
    equity: EquitySpec | None = None
    label: str = "historical_windows"

    def window_count(self, horizon_months: int) -> int:
        """How many distinct starting months admit a full `horizon_months` window."""

        return max(0, self.history.months - horizon_months)

    def independent_window_estimate(self, horizon_months: int) -> float:
        """Non-overlapping windows the record could supply — the honest sample size.

        Reported alongside `window_count` because the two differ by two orders of magnitude and
        only this one bounds what can be concluded. 199 overlapping 30-year windows drawn from
        46 years of data contain about 1.5 independent 30-year observations.
        """

        return self.history.months / horizon_months if horizon_months else 0.0

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        keys: set[LevelSeriesKey] = {InflationKey()}
        for spec in self.instruments:
            keys.add(SecurityKey(symbol=spec.symbol))
            keys.add(SecurityDistributionKey(symbol=spec.symbol))
        if self.equity is not None:
            keys.add(SecurityKey(symbol=self.equity.symbol))
        return frozenset(keys)

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        return frozenset()

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        """Rollout `i` replays the window starting at month `i`.

        `request.rollout_seeds` is IGNORED, and that is not an oversight — there is no
        randomness here to seed. A rollout's identity is its start month, so asking for the
        same rollout index always returns the same path, which is the property the seeds exist
        to provide everywhere else.
        """

        rollouts = request.rollout_count
        months = request.horizon_months + 1
        available = self.window_count(request.horizon_months)
        if available <= 0:
            raise ValueError(
                f"history has {self.history.months} months, too few for a {request.horizon_months}-month window"
            )
        if rollouts > available:
            raise ValueError(
                f"asked for {rollouts} rollouts but the record supplies only {available} distinct "
                f"{request.horizon_months}-month windows. Cycling would duplicate paths and quietly "
                f"double-count them in every percentile; request at most {available}."
            )

        # Evenly spaced starts rather than the first `rollouts` of them: a caller asking for
        # fewer windows than exist wants the whole record thinned, not its first decade.
        starts = np.linspace(0, available - 1, rollouts).round().astype(int)
        windows = starts[:, None] + np.arange(months)[None, :]

        short_rate = np.maximum(self.history.short_rate[windows], MINIMUM_ANNUAL_YIELD)
        term_spread = self.history.term_spread[windows]

        blocks: list[tuple[LevelSeriesKey, np.ndarray]] = [
            (InflationKey(), _rebased(self.history.cpi_level[windows], 100.0))
        ]
        for spec in self.instruments:
            price, distribution = instrument_paths(spec, short_rate=short_rate, term_spread=term_spread)
            blocks.append((SecurityKey(symbol=spec.symbol), price))
            blocks.append((SecurityDistributionKey(symbol=spec.symbol), distribution))
        if self.equity is not None:
            blocks.append(
                (
                    SecurityKey(symbol=self.equity.symbol),
                    _rebased(self.history.equity_level[windows], self.equity.initial_price_usd),
                )
            )

        return SampledExogenousBundle(
            levels=assemble_level_frames(blocks, rollout_count=rollouts, horizon_months=request.horizon_months),
            model_id=self.label,
            provenance={
                "exogenous_provider_label": self.label,
                "window_months": request.horizon_months,
                "distinct_windows_available": available,
                "independent_window_estimate": round(self.independent_window_estimate(request.horizon_months), 2),
                "notes": (
                    "Rollouts are OVERLAPPING historical windows, not independent draws. A "
                    "percentile over them is a count of historical starting months, not a probability.",
                ),
            },
        )


def _rebased(windows: np.ndarray, initial: float) -> np.ndarray:
    """Scale each window so it starts at `initial`.

    Every window must start at the same level or the sim's month-0 mark would differ per
    rollout, which would make a portfolio's starting value depend on which piece of history it
    was about to live through. Only ratios within a window carry information anyway.
    """

    # Divide FIRST, then scale: `(initial * w) / w[0]` is not exactly `initial` at month 0 in
    # floating point, and month 0 is the value anchoring divides by — so an inexact one turns
    # into a per-rollout scale error in every anchored series.
    return np.asarray(initial * (windows / windows[:, :1]))


def macro_history_from_levels(
    *,
    short_rate_percent: Sequence[tuple[date, float]],
    long_rate_percent: Sequence[tuple[date, float]],
    equity_level: Sequence[tuple[date, float]],
    cpi_level: Sequence[tuple[date, float]],
) -> MacroHistory:
    """Inner-join four `(month, value)` series into one aligned record.

    Inner-joined on month, so the record is exactly the span where ALL FOUR exist — which is
    the binding constraint and worth seeing: rates reach 1954 and CPI 1947, but a total-return
    equity series only reaches 1980, so that is where the usable history starts.
    """

    tables = [dict(series) for series in (short_rate_percent, long_rate_percent, equity_level, cpi_level)]
    months = sorted(set.intersection(*(set(table) for table in tables)))
    if not months:
        raise ValueError("the four series share no months")

    short, long_rate, equity, cpi = ({month: table[month] for month in months} for table in tables)
    return MacroHistory(
        short_rate=np.array([short[m] * 0.01 for m in months]),
        term_spread=np.array([(long_rate[m] - short[m]) * 0.01 for m in months]),
        equity_level=np.array([equity[m] for m in months]),
        cpi_level=np.array([cpi[m] for m in months]),
    )
