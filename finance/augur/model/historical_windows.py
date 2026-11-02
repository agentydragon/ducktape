"""Replay the past: one rollout per historical starting month.

The caller selects historical start dates; each output row follows that date through the
requested horizon. Selection and ordering belong to the experiment, not the batch size.
No parameters, no distributional assumptions, no fit — the paths ARE the data.

**Why this exists next to `structural_macro`.** That model is a fitted Gaussian VAR, and a
fitted Gaussian VAR gets specific things wrong in ways its own diagnostics cannot show: it has
no fat tails, no volatility clustering, no valuation feedback, and — most damaging for a
CPI-indexed spender — equity independent of inflation, so it cannot produce the stagflation
that actually hurt retirees in 1973. Historical replay gets every one of those right for free,
because it never assumes anything about the joint distribution; it just uses the one draw
history handed us.

**And what it gets wrong in exchange, which is severe.** The windows OVERLAP. A century of
aligned monthly data and a 30-year horizon give 840 of them, and consecutive windows share 359
of 360 months — so a rough duration-based estimate is closer to **3 independent observations** than to 840.
A "P[ruin] = 4%" from this is not a probability. It is "34 of the 840 historical starting months
would have failed", and those 34 are almost certainly one contiguous episode counted 34 times.
`window_count` and `independent_window_estimate` are on the result so a caller cannot quietly
forget that.

Use the two together and disagree loudly: the fitted model gives smooth probabilities over
scenarios that never happened, and this gives a handful of scenarios that definitely did. When
they agree, the answer is robust to the modelling choice. When they diverge, the divergence is
the finding — and neither number is the truth.

The INSTRUMENT layer (`bond_fund.constant_maturity_fund_paths`) is shared with
`structural_macro`, deliberately: how a fund responds to a yield change is a claim about the
fund, not about the economy, so the two providers must not differ on it or their outputs are
not comparable. What DOES differ is the yield going in — here it is observed, including
Moody's corporate curves, where the fitted model has only its own three-factor state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Literal

import numpy as np
import polars as pl
from pydantic import model_validator

from finance.augur.model.bond_fund import MONTHS_PER_YEAR, BondFundSpec, YieldCurve
from finance.augur.model.equity import EquitySpec
from finance.augur.model.exogenous import SampledExogenousBundle
from finance.augur.model.market_paths import MarketPaths
from finance.augur.model.product_paths import construct_products
from finance.augur.model.schemas import FrozenModel
from finance.evidence import loading, sources
from finance.evidence.loading import evidence_dir_from_env


@dataclass(frozen=True)
class MacroHistory:
    """The aligned monthly record every window is cut from.

    Every series shares `months`, so a window is a slice and nothing can drift out of
    alignment. A missing source month is a boundary between replayable segments; no replay
    window may cross it. `short_rate` and `term_spread` are annualized decimals; `equity_level` is a
    total-return index and `cpi_level` a price index, both in arbitrary units because only
    their ratios within a window are ever used.

    The month axis is carried rather than derived because a window's identity IS its starting
    month: without it a caller can say 199 windows failed but not which decade they were, and
    a study reproducing a published result cannot cut the record to that study's period.
    """

    months: tuple[date, ...]
    short_rate: np.ndarray
    term_spread: np.ndarray
    # Moody's seasoned corporate yields, annualized decimals. Carried as OBSERVATIONS rather
    # than as a spread over the government curve: a corporate sleeve's yield is a thing that
    # was measured every month back to 1919, and the Aaa/Baa gap is the credit spread widening
    # in every recession — which a constant spread denies.
    corporate_aaa_yield: np.ndarray
    corporate_baa_yield: np.ndarray
    equity_level: np.ndarray
    cpi_level: np.ndarray
    # Ex-US developed equity total-return index in USD, compounded like `equity_level`; `None` for
    # a US-only record. Opt-in (`with_ex_us_equity`) because it starts in 1975, and a record
    # carrying it is cut to that span — half a century shorter for callers that never read it.
    ex_us_equity_level: np.ndarray | None = None

    def __post_init__(self) -> None:
        series = [
            self.months,
            self.short_rate,
            self.term_spread,
            self.corporate_aaa_yield,
            self.corporate_baa_yield,
            self.equity_level,
            self.cpi_level,
        ]
        indices = [self.equity_level, self.cpi_level]
        if self.ex_us_equity_level is not None:
            series.append(self.ex_us_equity_level)
            indices.append(self.ex_us_equity_level)
        lengths = {len(values) for values in series}
        if len(lengths) != 1:
            raise ValueError(f"macro history series have different lengths: {sorted(lengths)}")
        if any(later <= earlier for earlier, later in pairwise(self.months)):
            raise ValueError("macro history months must be strictly increasing")
        if any(np.any(index <= 0.0) for index in indices):
            raise ValueError("equity and CPI levels must be strictly positive to be rebased")

    def restricted_to(self, *, start: date | None, end: date | None) -> MacroHistory:
        """The same record cut to `[start, end]`, either bound `None` for "as far as it goes".

        A reproduction of a published study needs this: run the replay over the whole record
        and the answer is about a different sample than the one being reproduced, with no way
        to tell the disagreement apart from a methodology difference.
        """

        keep = [
            index
            for index, month in enumerate(self.months)
            if (start is None or month >= start) and (end is None or month <= end)
        ]
        if not keep:
            raise ValueError(f"no month of the {self.months[0]}..{self.months[-1]} record falls in {start}..{end}")
        return self._rows(np.asarray(keep))

    def with_ex_us_equity(self, ex_us_equity_level: pl.DataFrame) -> MacroHistory:
        """The record cut to the months in the ex-US `(month, value)` frame, carrying it alongside.

        Every other series keeps its values month for month, so a window or bootstrap block
        draws US and ex-US equity from the same months. Any additional month removed by the
        ex-US overlap becomes another boundary; replay and bootstrap only use adjacent rows
        within a contiguous run.
        """

        record_rows = pl.DataFrame({"month": self.months, "value": np.arange(len(self.months), dtype=np.float64)})
        aligned = loading.align_monthly_frames(
            {"record_row": record_rows, "ex_us_equity_level": ex_us_equity_level}, value_column="value"
        )
        if aligned.is_empty():
            raise ValueError("US and ex-US records have no shared months")
        rows = aligned.get_column("record_row").to_numpy().astype(np.intp)
        cut = self._rows(rows)
        return replace(cut, ex_us_equity_level=aligned.get_column("ex_us_equity_level").to_numpy())

    def _rows(self, rows: np.ndarray) -> MacroHistory:
        return MacroHistory(
            months=tuple(self.months[index] for index in rows),
            short_rate=self.short_rate[rows],
            term_spread=self.term_spread[rows],
            corporate_aaa_yield=self.corporate_aaa_yield[rows],
            corporate_baa_yield=self.corporate_baa_yield[rows],
            equity_level=self.equity_level[rows],
            cpi_level=self.cpi_level[rows],
            ex_us_equity_level=None if self.ex_us_equity_level is None else self.ex_us_equity_level[rows],
        )


@dataclass(frozen=True)
class HistoricalWindowsModel:
    """Materialize named historical windows without a seeded sampler interface."""

    history: MacroHistory
    instruments: tuple[BondFundSpec, ...] = ()
    equity: EquitySpec | None = None
    label: str = "historical_windows"

    def window_count(self, horizon_months: int) -> int:
        """How many starts admit a full calendar-contiguous window."""

        if horizon_months < 0:
            raise ValueError("horizon_months must be non-negative")
        return len(self._eligible_start_indices(horizon_months))

    def window_starts(self, horizon_months: int) -> tuple[date, ...]:
        """All eligible starting months, in record order; callers may select any subset."""

        return tuple(self.history.months[index] for index in self._eligible_start_indices(horizon_months))

    def _eligible_start_indices(self, horizon_months: int) -> tuple[int, ...]:
        if horizon_months < 0:
            raise ValueError("horizon_months must be non-negative")
        return tuple(
            index
            for start, end in loading.contiguous_month_segments(self.history.months)
            for index in range(start, max(start, end - horizon_months))
        )

    def independent_window_estimate(self, horizon_months: int) -> float:
        """Approximate observations at the requested horizon, summed over contiguous runs.

        This duration-based ratio is only a rough dependence caveat, not an exact independent
        sample size: it assumes each run contributes in proportion to its observed months and
        cannot model serial dependence. Runs shorter than the requested horizon contribute no
        full observation, and replayable windows never cross a missing month. For a continuous
        century and a 30-year horizon, the ratio is about three even though there are hundreds
        of overlapping start months.
        """

        if not horizon_months:
            return 0.0
        return sum(
            (end - start) / horizon_months
            for start, end in loading.contiguous_month_segments(self.history.months)
            if end - start > horizon_months
        )

    def materialize(self, *, window_starts: Sequence[date], horizon_months: int) -> SampledExogenousBundle:
        """Construct configured products on exactly the supplied historical dates."""
        return construct_products(
            self.market_paths(window_starts=window_starts, horizon_months=horizon_months),
            equity=self.equity,
            instruments=self.instruments,
        )

    def market_paths(self, *, window_starts: Sequence[date], horizon_months: int) -> MarketPaths:
        """Emit exactly the supplied dates in order, with batch-local rollout indices.

        A date's values do not change when other windows are selected, reordered or split
        into another batch. Duplicate dates reject to prevent silently overweighting history.
        `provenance.window_starts` records the date corresponding to every output row.
        """

        dates = tuple(window_starts)
        rollouts = len(dates)
        months = horizon_months + 1
        available = self.window_count(horizon_months)
        if available <= 0:
            raise ValueError(
                f"history has {len(self.history.months)} months, too few for a {horizon_months}-month window"
            )
        if not dates:
            raise ValueError("window_starts must select at least one historical window")
        if len(set(dates)) != rollouts:
            raise ValueError("window_starts must not repeat historical windows")

        eligible = {self.history.months[index]: index for index in self._eligible_start_indices(horizon_months)}
        invalid = [month for month in dates if month not in eligible]
        if invalid:
            raise ValueError(f"window starts {invalid} are unavailable for a {horizon_months}-month horizon")
        starts = np.asarray([eligible[month] for month in dates])
        windows = starts[:, None] + np.arange(months)[None, :]

        return MarketPaths(
            short_rate=self.history.short_rate[windows],
            term_spread=self.history.term_spread[windows],
            cpi_level=_rebased(self.history.cpi_level[windows], 100.0),
            equity_total_return_index=_rebased(self.history.equity_level[windows], 1.0),
            corporate_yields={
                YieldCurve.CORPORATE_AAA: self.history.corporate_aaa_yield[windows],
                YieldCurve.CORPORATE_BAA: self.history.corporate_baa_yield[windows],
            },
            provenance={
                "exogenous_provider_label": self.label,
                "window_months": horizon_months,
                "record_start": self.history.months[0].isoformat(),
                "record_end": self.history.months[-1].isoformat(),
                "window_starts": tuple(month.isoformat() for month in dates),
                "distinct_windows_available": available,
                "independent_window_estimate": round(self.independent_window_estimate(horizon_months), 2),
                "notes": (
                    "Rollouts are OVERLAPPING historical windows, not independent draws. A "
                    "percentile over them is a count of historical starting months, not a probability. "
                    "The independent-window estimate is a rough duration ratio, not an exact sample size.",
                ),
            },
        )


class HistoricalWindowsProviderConfig(FrozenModel):
    """Experiment configuration for loading history and choosing instruments.

    `evidence_dir` points at a checkout of augur-evidence, the same one the fit pipeline reads;
    `None` falls back to `AUGUR_EVIDENCE_DIR`, which is what the in-cluster deployment sets.
    After realization the experiment selects dates and calls `materialize`; this is not a
    seeded deployment `ProviderConfig`.
    """

    type: Literal["historical_windows"] = "historical_windows"
    evidence_dir: Path | None = None
    # Cut the record to a study period; `None` means "as far as the data goes". A reproduction
    # of a published result has to pin these or it is answering about a different sample.
    record_start: date | None = None
    record_end: date | None = None
    equity: EquitySpec | None = None
    instruments: tuple[BondFundSpec, ...] = ()

    @model_validator(mode="after")
    def _reject_empty_record_span(self) -> HistoricalWindowsProviderConfig:
        if self.record_start is not None and self.record_end is not None and self.record_end < self.record_start:
            raise ValueError(f"record span ends before it starts: {self.record_start}..{self.record_end}")
        return self

    def realize_model(self) -> HistoricalWindowsModel:
        directory = self.evidence_dir if self.evidence_dir is not None else evidence_dir_from_env()
        history = load_macro_history(directory)
        return HistoricalWindowsModel(
            history=history.restricted_to(start=self.record_start, end=self.record_end),
            instruments=self.instruments,
            equity=self.equity,
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


def macro_history_from_frames(frames: dict[str, pl.DataFrame]) -> MacroHistory:
    """Align selected monthly series and convert them into the replay model's arrays.

    Every frame must contain `(month, value)`. Source parsing, column selection, and derived
    series stay in Polars up to this boundary so unrelated columns never enter the join. A
    month absent from a required source becomes a boundary between replayable segments.
    """
    expected = {
        "short_rate_percent",
        "long_rate_percent",
        "corporate_aaa_percent",
        "corporate_baa_percent",
        "equity_level",
        "cpi_level",
    }
    if set(frames) != expected:
        raise ValueError(f"macro history needs exactly these selected series: {sorted(expected)}")
    aligned = loading.align_monthly_frames(frames, value_column="value")
    months = aligned.get_column("month").to_list()

    short = aligned.get_column("short_rate_percent").to_numpy()
    long_rate = aligned.get_column("long_rate_percent").to_numpy()
    aaa = aligned.get_column("corporate_aaa_percent").to_numpy()
    baa = aligned.get_column("corporate_baa_percent").to_numpy()
    equity = aligned.get_column("equity_level").to_numpy()
    cpi = aligned.get_column("cpi_level").to_numpy()
    return MacroHistory(
        months=tuple(months),
        short_rate=short * 0.01,
        term_spread=(long_rate - short) * 0.01,
        corporate_aaa_yield=aaa * 0.01,
        corporate_baa_yield=baa * 0.01,
        equity_level=equity,
        cpi_level=cpi,
    )


# ── Assembling the record ────────────────────────────────────────────────────────────────
#
# Here rather than in `fit` because of what it is, not where it started: `fit` ESTIMATES
# parameters, and this only reads and aligns. Putting it there also made `model` import `fit`
# to realize a provider — a cycle whose only escape was a deferred import, which is the sort of
# workaround that is easier to add than to notice later.──

SEAM_ANCHOR_MONTHS = 12
"""Overlap months averaged to align a spliced series at its seam. See `splice_monthly_frames`."""


def splice_monthly_frames(
    *, early: pl.DataFrame, late: pl.DataFrame, anchor_months: int = SEAM_ANCHOR_MONTHS
) -> pl.DataFrame:
    """Extend `late` backwards with `early`, shifted to meet it at the seam.

    This combines `LTGOVTBD` (the long-term government composite, 1925-2000) with `GS10` (the
    10-year constant-maturity yield, 1953-). They measure different durations, so their
    difference varies: over the 567-month overlap it averages -0.13 percentage points, but
    ranges from -0.63 in the 1970s to +0.33 in the 1990s, with a 0.44-point standard deviation.

    The shift uses the first `anchor_months` of overlap. A whole-overlap mean would carry a
    1990s discrepancy into a 1930s observation. The seam anchor only asserts agreement where
    the two measurements are joined. A 30-year window starting in 1926 crosses this seam in
    1953, so a step there would look like a real rate move.

    The remaining duration mismatch cannot be removed: pre-seam values retain an uncertain
    offset of roughly the overlap's spread. That is acceptable for a term spread feeding a
    duration approximation, not for pricing a specific bond.
    """
    if anchor_months <= 0:
        raise ValueError("anchor_months must be positive")
    overlap = loading.align_monthly_frames({"early": early, "late": late}, value_column="value")
    if overlap.height < anchor_months:
        raise ValueError(f"the two series overlap in {overlap.height} months, fewer than the {anchor_months} anchor")
    anchor = overlap.head(anchor_months)
    shift = anchor.select((pl.col("late") - pl.col("early")).mean()).item()
    seam = overlap.get_column("month")[0]
    shifted_early = early.filter(pl.col("month") < seam).with_columns((pl.col("value") + shift).alias("value"))
    return pl.concat([shifted_early, late], how="vertical").sort("month")


DECIMAL_TO_PERCENT = 100.0

MACRO_HISTORY_SOURCES = (
    sources.FRENCH_FACTORS,
    sources.FRED_LTGOVTBD,
    sources.FRED_GS10,
    sources.FRED_CPI_NSA,
    sources.FRED_AAA,
    sources.FRED_BAA,
)
"""Exactly what `load_macro_history` reads, so a caller materializing an evidence directory
does not keep its own copy of the list. It lives here because adding a series to the record is
the edit that invalidates one — and a caller that missed the edit fails at load, not at fetch."""


def load_macro_history(evidence_dir: Path) -> MacroHistory:
    """Assemble the century-long record the historical-window sampler replays.

    Six selected series, three of them needing a decision the raw data does not make for you:

    - **Equity and the short rate come from ONE file.** Ken French's factors give the CRSP
      total market (`Mkt-RF + RF`) and the one-month T-bill together, monthly from 1926-07, so
      the two are aligned by construction rather than by a join that could slip.
    - **The equity LEVEL is a compounded index**, not a price. `MacroHistory` rebases every
      window to a common start, so only ratios matter and the base is arbitrary.
    - **The long rate is spliced** (`splice_monthly_frames`), which is the only step carrying an
      unquantified error — see that function.
    - **CPI is the NOT-seasonally-adjusted series.** `CPIAUCSL` starts 1947 and would truncate
      the record by two decades; `CPIAUCNS` reaches 1913. Seasonality is irrelevant here
      because every consumer reads a 12-month ratio.
    - **Corporate yields are Moody's own**, not the government curve plus a guessed spread.
      Aaa and Baa both reach 1919 without a gap, so a high-grade sleeve earns what high-grade
      corporates earned, and the credit spread between them is observed rather than assumed.

    The record is the intersection of those six inputs, so its start is whichever series begins
    latest — today French's 1926-07.
    """

    cache = loading.EvidenceFrameCache(evidence_dir)
    factors = cache.frame(sources.FRENCH_FACTORS)
    short_rate = loading.french_risk_free_returns(factors).select(
        "month", (pl.col("risk_free_rate") * MONTHS_PER_YEAR * DECIMAL_TO_PERCENT).alias("value")
    )
    equity = loading.compound_monthly_returns(
        loading.french_market_returns(factors), return_column="market_total_return"
    )
    long_rate = splice_monthly_frames(
        early=cache.monthly_frame(sources.FRED_LTGOVTBD), late=cache.monthly_frame(sources.FRED_GS10)
    )
    return macro_history_from_frames(
        {
            "short_rate_percent": short_rate,
            "long_rate_percent": long_rate,
            "corporate_aaa_percent": cache.monthly_frame(sources.FRED_AAA),
            "corporate_baa_percent": cache.monthly_frame(sources.FRED_BAA),
            "equity_level": equity,
            "cpi_level": cache.monthly_frame(sources.FRED_CPI_NSA),
        }
    )


def load_ex_us_equity_levels(evidence_dir: Path) -> pl.DataFrame:
    """Load the independent ex-US total-return index, compounded from French monthly returns.

    Compose it with a US record using `MacroHistory.with_ex_us_equity` when a consumer needs
    both paths on one month axis. This loader does not read or truncate the US evidence.
    """
    cache = loading.EvidenceFrameCache(evidence_dir)
    ex_us = cache.frame(sources.FRENCH_INTERNATIONAL_INDICES)
    return loading.compound_monthly_returns(ex_us, return_column="market_total_return")
