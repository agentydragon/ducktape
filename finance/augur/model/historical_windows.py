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

**And what it gets wrong in exchange, which is severe.** The windows OVERLAP. A century of
aligned monthly data and a 30-year horizon give 840 of them, and consecutive windows share 359
of 360 months — so the effective sample is closer to **3 independent observations** than to 840.
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
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Literal, assert_never

import numpy as np
from pydantic import model_validator

from finance.augur.model.bond_fund import YieldCurve, constant_maturity_fund_paths
from finance.augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle, assemble_level_frames
from finance.augur.model.schemas import FrozenModel
from finance.augur.model.series import InflationKey, IssuerId, LevelSeriesKey, SecurityDistributionKey, SecurityKey
from finance.augur.model.structural_macro import MINIMUM_ANNUAL_YIELD, MONTHS_PER_YEAR, EquitySpec, InstrumentSpec
from finance.evidence import loading, sources
from finance.evidence.loading import MonthlyLevel, evidence_dir_from_env


@dataclass(frozen=True)
class MacroHistory:
    """The aligned monthly record every window is cut from.

    All four series share `months`, so a window is a slice and nothing can drift out of
    alignment. `short_rate` and `term_spread` are annualized decimals; `equity_level` is a
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

    def __post_init__(self) -> None:
        lengths = {
            len(self.months),
            len(self.short_rate),
            len(self.term_spread),
            len(self.corporate_aaa_yield),
            len(self.corporate_baa_yield),
            len(self.equity_level),
            len(self.cpi_level),
        }
        if len(lengths) != 1:
            raise ValueError(f"macro history series have different lengths: {sorted(lengths)}")
        if any(later <= earlier for earlier, later in pairwise(self.months)):
            raise ValueError("macro history months must be strictly increasing")
        if np.any(self.equity_level <= 0.0) or np.any(self.cpi_level <= 0.0):
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
        rows = np.asarray(keep)
        return MacroHistory(
            months=tuple(self.months[index] for index in keep),
            short_rate=self.short_rate[rows],
            term_spread=self.term_spread[rows],
            corporate_aaa_yield=self.corporate_aaa_yield[rows],
            corporate_baa_yield=self.corporate_baa_yield[rows],
            equity_level=self.equity_level[rows],
            cpi_level=self.cpi_level[rows],
        )


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

        return max(0, len(self.history.months) - horizon_months)

    def independent_window_estimate(self, horizon_months: int) -> float:
        """Non-overlapping windows the record could supply — the honest sample size.

        Reported alongside `window_count` because the two differ by two orders of magnitude and
        only this one bounds what can be concluded. 840 overlapping 30-year windows drawn from a
        century of data contain about 3 independent 30-year observations.
        """

        return len(self.history.months) / horizon_months if horizon_months else 0.0

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
                f"history has {len(self.history.months)} months, too few for a {request.horizon_months}-month window"
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

        def market_yield(spec: InstrumentSpec) -> np.ndarray:
            """The fund's own yield over each window — observed, not derived from a spread."""

            match spec.yield_curve:
                case YieldCurve.GOVERNMENT:
                    # Still interpolated off the two-point government curve; see SPEC.md gap 8.
                    curve = short_rate + min(spec.maturity_years / 10.0, 1.0) * term_spread
                case YieldCurve.CORPORATE_AAA:
                    curve = self.history.corporate_aaa_yield[windows]
                case YieldCurve.CORPORATE_BAA:
                    curve = self.history.corporate_baa_yield[windows]
                case _ as unreachable:
                    assert_never(unreachable)
            return np.asarray(np.maximum(curve + spec.spread, MINIMUM_ANNUAL_YIELD))

        blocks: list[tuple[LevelSeriesKey, np.ndarray]] = [
            (InflationKey(), _rebased(self.history.cpi_level[windows], 100.0))
        ]
        for spec in self.instruments:
            price, distribution = constant_maturity_fund_paths(
                market_yield(spec), maturity_years=spec.maturity_years, initial_price_usd=spec.initial_price_usd
            )
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
                "record_start": self.history.months[0].isoformat(),
                "record_end": self.history.months[-1].isoformat(),
                "first_window_start": self.history.months[starts[0]].isoformat(),
                "last_window_start": self.history.months[starts[-1]].isoformat(),
                "distinct_windows_available": available,
                "independent_window_estimate": round(self.independent_window_estimate(request.horizon_months), 2),
                "notes": (
                    "Rollouts are OVERLAPPING historical windows, not independent draws. A "
                    "percentile over them is a count of historical starting months, not a probability.",
                ),
            },
        )


class HistoricalWindowsProviderConfig(FrozenModel):
    """YAML config for the replay provider. See the module docstring.

    `evidence_dir` points at a checkout of augur-evidence, the same one the fit pipeline reads;
    `None` falls back to `AUGUR_EVIDENCE_DIR`, which is what the in-cluster deployment sets.
    Unlike every other provider here the parameters are not in the config — they are the past —
    so the only thing to configure is where to find it and what instruments to price.
    """

    type: Literal["historical_windows"] = "historical_windows"
    evidence_dir: Path | None = None
    # Cut the record to a study period; `None` means "as far as the data goes". A reproduction
    # of a published result has to pin these or it is answering about a different sample.
    record_start: date | None = None
    record_end: date | None = None
    equity: EquitySpec | None = None
    instruments: tuple[InstrumentSpec, ...] = ()

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


def macro_history_from_levels(
    *,
    short_rate_percent: Sequence[tuple[date, float]],
    long_rate_percent: Sequence[tuple[date, float]],
    corporate_aaa_percent: Sequence[tuple[date, float]],
    corporate_baa_percent: Sequence[tuple[date, float]],
    equity_level: Sequence[tuple[date, float]],
    cpi_level: Sequence[tuple[date, float]],
) -> MacroHistory:
    """Inner-join four `(month, value)` series into one aligned record.

    Inner-joined on month, so the record is exactly the span where ALL FOUR exist, and its
    start is whichever series begins latest — see `load_macro_history` for which that is.
    """

    tables = [
        dict(series)
        for series in (
            short_rate_percent,
            long_rate_percent,
            corporate_aaa_percent,
            corporate_baa_percent,
            equity_level,
            cpi_level,
        )
    ]
    months = sorted(set.intersection(*(set(table) for table in tables)))
    if not months:
        raise ValueError("the series share no months")

    short, long_rate, aaa, baa, equity, cpi = ({month: table[month] for month in months} for table in tables)
    return MacroHistory(
        months=tuple(months),
        short_rate=np.array([short[m] * 0.01 for m in months]),
        term_spread=np.array([(long_rate[m] - short[m]) * 0.01 for m in months]),
        corporate_aaa_yield=np.array([aaa[m] * 0.01 for m in months]),
        corporate_baa_yield=np.array([baa[m] * 0.01 for m in months]),
        equity_level=np.array([equity[m] for m in months]),
        cpi_level=np.array([cpi[m] for m in months]),
    )


# ── Assembling the record ────────────────────────────────────────────────────────────────
#
# Here rather than in `fit` because of what it is, not where it started: `fit` ESTIMATES
# parameters, and this only reads and aligns. Putting it there also made `model` import `fit`
# to realize a provider — a cycle whose only escape was a deferred import, which is the sort of
# workaround that is easier to add than to notice later.──

SEAM_ANCHOR_MONTHS = 12
"""Overlap months averaged to align a spliced series at its seam. See `splice_at_seam`."""


def splice_at_seam(
    *, early: Sequence[MonthlyLevel], late: Sequence[MonthlyLevel], anchor_months: int = SEAM_ANCHOR_MONTHS
) -> list[MonthlyLevel]:
    """Extend `late` backwards with `early`, level-shifted to meet it at the seam.

    Built for `LTGOVTBD` (long-term government composite, 1925-2000) under `GS10` (10-year
    constant maturity, 1953-). They measure different durations, so they do not agree: over
    their 567-month overlap the difference averages -0.13pp but swings from -0.63pp in the
    1970s to +0.33pp in the 1990s, sd 0.44pp.

    That time variation is why the shift is computed from the FIRST `anchor_months` of overlap
    rather than from the whole of it. A whole-overlap mean would import a 1990s discrepancy
    into a 1930s observation; a seam anchor only claims the two series agree where they are
    joined, which is the one place continuity actually matters — a 30-year window starting in
    1926 ends in 1956 and crosses the seam, so a step there would read as a real rate move.

    The residual error is not removed and cannot be: pre-seam values carry an unknown
    duration-mismatch offset of roughly the overlap's spread. Fine for a term SPREAD feeding a
    duration approximation; not fine for pricing a specific bond.
    """

    late_by_month = {level.month: level.value for level in late}
    early_by_month = {level.month: level.value for level in early}
    overlap = sorted(set(late_by_month) & set(early_by_month))
    if len(overlap) < anchor_months:
        raise ValueError(f"the two series overlap in {len(overlap)} months, fewer than the {anchor_months} anchor")

    anchor = overlap[:anchor_months]
    shift = float(np.mean([late_by_month[m] - early_by_month[m] for m in anchor]))
    seam = overlap[0]

    spliced = [MonthlyLevel(month=m, value=v + shift) for m, v in sorted(early_by_month.items()) if m < seam]
    spliced.extend(MonthlyLevel(month=m, value=late_by_month[m]) for m in sorted(late_by_month))
    return spliced


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

    Four series, three of them needing a decision the raw data does not make for you:

    - **Equity and the short rate come from ONE file.** Ken French's factors give the CRSP
      total market (`Mkt-RF + RF`) and the one-month T-bill together, monthly from 1926-07, so
      the two are aligned by construction rather than by a join that could slip.
    - **The equity LEVEL is a compounded index**, not a price. `MacroHistory` rebases every
      window to a common start, so only ratios matter and the base is arbitrary.
    - **The long rate is spliced** (`splice_at_seam`), which is the only step carrying an
      unquantified error — see that function.
    - **CPI is the NOT-seasonally-adjusted series.** `CPIAUCSL` starts 1947 and would truncate
      the record by two decades; `CPIAUCNS` reaches 1913. Seasonality is irrelevant here
      because every consumer reads a 12-month ratio.
    - **Corporate yields are Moody's own**, not the government curve plus a guessed spread.
      Aaa and Baa both reach 1919 without a gap, so a high-grade sleeve earns what high-grade
      corporates earned, and the credit spread between them is observed rather than assumed.

    The record is the intersection, so its start is whichever series begins latest — today
    French's 1926-07.
    """

    factors = loading.french_factors_frame(
        loading.source_bytes(evidence_dir, sources.FRENCH_FACTORS), sources.FRENCH_FACTORS
    )
    months = factors.get_column("month").to_list()
    equity_index = np.cumprod(1.0 + factors.get_column("market_total_return").to_numpy())
    # The T-bill is a monthly simple return; the rest of the model speaks ANNUALIZED PERCENT.
    short_rate_percent = factors.get_column("risk_free_rate").to_numpy() * MONTHS_PER_YEAR * DECIMAL_TO_PERCENT

    long_rate = splice_at_seam(
        early=loading.read_monthly_levels(evidence_dir, sources.FRED_LTGOVTBD),
        late=loading.read_monthly_levels(evidence_dir, sources.FRED_GS10),
    )

    return macro_history_from_levels(
        short_rate_percent=list(zip(months, short_rate_percent.tolist(), strict=True)),
        long_rate_percent=[(level.month, level.value) for level in long_rate],
        corporate_aaa_percent=[
            (level.month, level.value) for level in loading.read_monthly_levels(evidence_dir, sources.FRED_AAA)
        ],
        corporate_baa_percent=[
            (level.month, level.value) for level in loading.read_monthly_levels(evidence_dir, sources.FRED_BAA)
        ],
        equity_level=list(zip(months, equity_index.tolist(), strict=True)),
        cpi_level=[
            (level.month, level.value) for level in loading.read_monthly_levels(evidence_dir, sources.FRED_CPI_NSA)
        ],
    )
