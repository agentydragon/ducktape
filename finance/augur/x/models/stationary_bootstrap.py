"""Resample the replay's record in blocks: a stationary bootstrap (Politis & Romano 1994).

`historical_windows` replays the record whole, so a century offers about three independent
30-year paths and nothing longer than itself. This recombines the same months into as many
paths as asked for, at any horizon, by concatenating blocks of consecutive record months
(Politis & Romano, "The Stationary Bootstrap", JASA 89(428):1303-1313). A block keeps the
record's short-run dependence, and drawing every series from the same months keeps their
cross-section: a 1974 inflation month arrives with 1974's rates and 1974's equity return.

**It creates no information.** Every path month is a month the record contains, so a path can
string the record's worst episodes together but never exceed its worst month, and a percentile
over rollouts describes recombinations of those months, not futures the record lacks.

The conventions a result depends on:

- **A resampled month is a record month after the first**: its rates, and the equity and CPI
  growth INTO it. The opening carries the month before the first block's first month, so a
  block reads exactly as the replay window starting there, rebased the same way.
- **Blocks start at a uniformly drawn month and last a geometric number of months** with mean
  `mean_block_months`: the first path month starts a block, and every later one continues its
  block with probability `1 - 1 / mean_block_months`. A mean of one month is the i.i.d.
  monthly bootstrap.
- **Blocks wrap circularly on a contiguous record**: past the record's last month a block
  continues at its second, the first with growth into it. A source gap forces a block restart
  at the segment edge instead of continuing across the missing month.
- **Rates are resampled as levels, the indices as growth.** A seam therefore moves every rate
  from one historical month's level to another's within a month, which the bond-fund
  construction prices as a rate move of that size; a rollout has about
  `horizon_months / mean_block_months` seams. Resampling rate changes instead would remove the
  step, but let rates random-walk out of the range the record spans.

`mean_block_months` has no default: the block length is a judgement that changes the answer.
Anarkulova, Cederburg, O'Doherty & Sias ("The Safe Withdrawal Rate: Evidence from a Broad Sample
of Developed Markets", JPEF 24(3), 2025) use a 120-month mean and report 12 to 240 months as
their sensitivity range.

Draws come from per-rollout streams (`derive_stream_rollout_seeds`), so a rollout's path
depends only on its seed, never on the batch, and its first months not on the horizon. The same
draws decide block starts at every mean, and a longer mean only drops starts, so a sweep over
the mean compares like with like.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from finance.augur.model.bond_fund import BondFundSpec, YieldCurve
from finance.augur.model.equity import EquitySpec
from finance.augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle
from finance.augur.model.historical_windows import MacroHistory
from finance.augur.model.market_paths import MarketPaths
from finance.augur.model.product_paths import construct_products, product_level_keys, validate_product_symbols
from finance.augur.model.series import IssuerId, LevelSeriesKey
from finance.augur.model.series_model import derive_stream_rollout_seeds
from finance.evidence.loading import contiguous_month_segments


@dataclass(frozen=True)
class StationaryBootstrapModel:
    """`Sampler` resampling `history` in geometric blocks; see the module docstring."""

    history: MacroHistory
    mean_block_months: float
    instruments: tuple[BondFundSpec, ...] = ()
    equity: EquitySpec | None = None
    label: str = "stationary_bootstrap"

    def __post_init__(self) -> None:
        if not len(_transition_sources(self.history.months)):
            raise ValueError(
                "the record has no consecutive month pair; resampling needs at least one, because a "
                "resampled month carries the growth into it from the month before"
            )
        if not (math.isfinite(self.mean_block_months) and self.mean_block_months >= 1.0):
            raise ValueError(
                f"mean_block_months must be a finite number of months, at least one: {self.mean_block_months}"
            )
        validate_product_symbols(equity=self.equity, instruments=self.instruments)

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        return product_level_keys(equity=self.equity, instruments=self.instruments)

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        return frozenset()

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        return construct_products(self.sample_market(request), equity=self.equity, instruments=self.instruments)

    def source_months(self, request: ExogenousSamplingRequest) -> np.ndarray:
        """`(rollout, month)` indices into `history.months`: the record month each path month carries.

        Month 0 is the opening; every later month is a record month after the first, and the runs
        of consecutive ones are the blocks.
        """

        # Each source index starts a real observed transition to the next calendar month. On a
        # gapped record, reaching a segment edge forces a fresh block instead of wrapping into
        # another segment.
        transition_sources = _transition_sources(self.history.months)
        transitions = len(transition_sources)
        draws = max(request.horizon_months, 1)
        starts = np.stack(
            [
                np.random.default_rng(seed).integers(transitions, size=draws)
                for seed in derive_stream_rollout_seeds(request.rollout_seeds, stream_id="stationary_bootstrap:start")
            ]
        )
        breaks = np.stack(
            [
                np.random.default_rng(seed).random(draws) < 1.0 / self.mean_block_months
                for seed in derive_stream_rollout_seeds(request.rollout_seeds, stream_id="stationary_bootstrap:break")
            ]
        )
        breaks[:, 0] = True
        if len(contiguous_month_segments(self.history.months)) == 1:
            # Preserve the original seeded circular sampler exactly for an unbroken record.
            step = np.arange(draws)
            block_start = np.maximum.accumulate(np.where(breaks, step, 0), axis=1)
            transition = (np.take_along_axis(starts, block_start, axis=1) + step - block_start) % transitions
            return np.concatenate([transition[:, :1], transition[:, : request.horizon_months] + 1], axis=1)

        source_to_transition = {int(source): index for index, source in enumerate(transition_sources)}
        transition = np.empty_like(starts)
        for rollout in range(len(starts)):
            for step in range(draws):
                if step == 0 or breaks[rollout, step]:
                    transition[rollout, step] = starts[rollout, step]
                    continue
                previous_source = transition_sources[transition[rollout, step - 1]] + 1
                transition[rollout, step] = source_to_transition.get(previous_source, starts[rollout, step])
        opening = transition_sources[transition[:, :1]]
        subsequent = transition_sources[transition[:, : request.horizon_months]] + 1
        return np.concatenate([opening, subsequent], axis=1)

    def sample_market(self, request: ExogenousSamplingRequest) -> MarketPaths:
        """Sample once for multiple constructions; configured bond choices do not affect these paths."""

        record = self.history
        months = self.source_months(request)
        return MarketPaths(
            short_rate=record.short_rate[months],
            term_spread=record.term_spread[months],
            # The replay's CPI base, so a block reads exactly as its replay window.
            cpi_level=100.0 * _compounded_growth(record.cpi_level, months, record.months),
            equity_total_return_index=_compounded_growth(record.equity_level, months, record.months),
            corporate_yields={
                YieldCurve.CORPORATE_AAA: record.corporate_aaa_yield[months],
                YieldCurve.CORPORATE_BAA: record.corporate_baa_yield[months],
            },
            provenance={
                "exogenous_provider_label": self.label,
                "record_start": record.months[0].isoformat(),
                "record_end": record.months[-1].isoformat(),
                "mean_block_months": self.mean_block_months,
                "rollout_seeds": request.rollout_seeds,
                "notes": (
                    "Rollouts recombine blocks of the record's own months, so the support is bounded by the "
                    "record: a rollout can string its worst episodes together but never exceed its worst month.",
                    "Blocks wrap circularly on a contiguous record; a source gap forces a block restart. "
                    "At each block seam every rate moves from one historical month's level to another's "
                    "within a month.",
                ),
            },
        )


def _compounded_growth(levels: np.ndarray, months: np.ndarray, record_months: Sequence[date]) -> np.ndarray:
    """One at the opening, then the record's growth into each later path month, compounded."""

    current = months[:, 1:]
    valid_sources = set(_transition_sources(record_months))
    invalid = [int(source) for source in np.unique(current - 1) if int(source) not in valid_sources]
    if invalid:
        raise ValueError(f"sampled growth crosses a missing source month at row(s) {invalid}")
    growth = levels[current] / levels[current - 1]
    return np.concatenate([np.ones((len(months), 1)), np.cumprod(growth, axis=1)], axis=1)


def _transition_sources(record_months: Sequence[date]) -> np.ndarray:
    """Source-row indexes whose next row is an observed consecutive calendar month."""

    return np.asarray(
        [index for start, end in contiguous_month_segments(record_months) for index in range(start, end - 1)],
        dtype=np.intp,
    )
