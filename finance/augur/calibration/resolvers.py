"""Resolve prediction markets against augur's per-rollout exogenous output.

The calibration piece consumes the SAME shape augur hands the sim: a
``PrivateEquityBundle`` (from ``SampledExogenousBundle.private_equity``) with
per-issuer, per-rollout channels -- ``mark_usd_per_unit``, ``event_kind_code``,
``regime_code``, ... A :class:`RolloutTrajectory` is one rollout's slice of those
channels. A resolver reads that whole trajectory and returns how a market resolves
IN THAT WORLD: ``YES``, ``NO``, or ``UNRESOLVED`` (the outcome is not determined
within the simulated horizon). Aggregating ``YES / (YES + NO)`` across rollouts
gives ``p_model``.

Only EVENT-based markets map cleanly. augur models going-public
(``PUBLIC_MARKET_OPEN``), collapse (``COLLAPSE`` / ``FORCED_RECOVERY``) and
acquisition (``ACQUISITION_CASHOUT``) as first-class absorbing events, so
"IPO by date" and "collapse/acquired before IPO" are apples-to-apples. With the
opt-in M2 valuation channel an issuer ALSO carries a company market-cap path
``company_valuation_usd`` (``V(t)``), making "valuation >= $X by date" scoreable
(``valuation_by_date``). Issuers without the channel (no ``current_valuation_usd``
anchor) leave it ``None``, and valuation markets stay unscored/surfaced. Revenue
is still unmodeled.

This module is generic over the issuer -- nothing here is specific to any one
company. Pass the issuer through ``trajectories_from_bundle``.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from enum import Enum

import numpy as np
import numpy.typing as npt

from finance.augur.calibration.platform import Direction
from finance.augur.dates import months_between
from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode


def months_after(anchor: date, when: date) -> int:
    """Month offset of `when` from `anchor` (floored). May be negative if `when` precedes `anchor`."""
    return math.floor(months_between(anchor, when))


# An IPO is preempted by any absorbing pre-IPO exit: collapse, forced recovery
# (collapse-flavored), or acquisition cashout.
_FAILURE_EVENTS = (
    PrivateEquityEventKindCode.COLLAPSE,
    PrivateEquityEventKindCode.FORCED_RECOVERY,
    PrivateEquityEventKindCode.ACQUISITION_CASHOUT,
)


class Resolution(Enum):
    YES = "yes"
    NO = "no"
    UNRESOLVED = "unresolved"  # outcome not determined within the simulated horizon


@dataclass(frozen=True)
class RolloutTrajectory:
    """One rollout's slice of the augur PE bundle for a single issuer (months 0..horizon)."""

    mark_usd_per_unit: npt.NDArray[np.float64]  # (horizon+1,) per-UNIT value (NOT a company valuation)
    event_kind_code: npt.NDArray[np.int64]  # (horizon+1,) PrivateEquityEventKindCode values
    regime_code: npt.NDArray[np.int64]  # (horizon+1,) PrivateEquityRegimeCode values
    as_of: date
    # (horizon+1,) company market cap V(t) in USD, or None when the issuer has no
    # opt-in M2 valuation channel. `trajectories_from_bundle` passes None when the
    # bundle's `company_valuation_usd` is all-zeros for the issuer (the
    # channel-off sentinel) — a positive market cap is never all-zeros.
    company_valuation_usd: npt.NDArray[np.float64] | None = None

    @property
    def horizon_months(self) -> int:
        return int(self.mark_usd_per_unit.shape[0]) - 1

    def month_on_or_before(self, when: date) -> int:
        """Largest month index falling on/before `when`. May exceed horizon or be negative."""
        return months_after(self.as_of, when)

    def first_event_month(self, *codes: PrivateEquityEventKindCode) -> int | None:
        hits = np.flatnonzero(np.isin(self.event_kind_code, codes))
        return int(hits[0]) if hits.size else None


def resolve_ipo_by_date(traj: RolloutTrajectory, *, by_month: int) -> Resolution:
    """YES iff a PUBLIC_MARKET_OPEN (going-public) event occurs at month <= by_month."""
    t_ipo = traj.first_event_month(PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN)
    if t_ipo is not None and t_ipo <= by_month:
        return Resolution.YES
    if by_month <= traj.horizon_months:
        return Resolution.NO  # whole window simulated, no IPO
    return Resolution.UNRESOLVED  # deadline beyond the simulated horizon, no IPO yet


def ipo_by_date_bucket_counts(
    trajectories: list[RolloutTrajectory], *, by_dates: list[date]
) -> npt.NDArray[np.int64] | None:
    """Per-bucket rollout counts for cumulative IPO-by-date ladders.

    For dates `[d0, d1, ...]`, buckets are `IPO by d0`, `(d0, d1]`, ...,
    and `not IPO by last date`. The final bucket is only resolved for rollouts
    whose last date is inside the simulated horizon; otherwise still-private
    rollouts are uncounted and the categorical row reports `n_resolved` below
    the rollout count.
    """
    if not trajectories:
        return None
    by_months = [trajectories[0].month_on_or_before(by_date) for by_date in by_dates]
    counts = np.zeros(len(by_months) + 1, dtype=np.int64)
    for traj in trajectories:
        t_ipo = traj.first_event_month(PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN)
        if t_ipo is not None:
            bucket_index = next((i for i, by_month in enumerate(by_months) if t_ipo <= by_month), None)
            if bucket_index is None:
                counts[-1] += 1
            else:
                counts[bucket_index] += 1
        elif by_months[-1] <= traj.horizon_months:
            counts[-1] += 1
    return counts


def resolve_pre_ipo_failure(traj: RolloutTrajectory) -> Resolution:
    """YES iff an absorbing COLLAPSED/ACQUIRED exit occurs before any PUBLIC_MARKET_OPEN."""
    t_fail = traj.first_event_month(*_FAILURE_EVENTS)
    t_ipo = traj.first_event_month(PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN)
    if t_fail is not None and (t_ipo is None or t_fail < t_ipo):
        return Resolution.YES
    if t_ipo is not None and (t_fail is None or t_ipo < t_fail):
        return Resolution.NO  # went public first
    return Resolution.UNRESOLVED  # still private-operating at end of horizon


def resolve_valuation_by_date(traj: RolloutTrajectory, *, threshold_usd: float, by_month: int) -> Resolution:
    """YES iff company valuation V(m) >= ``threshold_usd`` for some month m <= by_month.

    Needs the opt-in M2 valuation channel: an issuer with no ``company_valuation_usd``
    (channel off) is UNRESOLVED. With the channel, YES if the threshold is ever reached
    by month ``min(by_month, horizon)``; otherwise NO when the deadline is within the
    simulated horizon, or UNRESOLVED when the deadline lies beyond it (the threshold
    might still be reached later).
    """
    if traj.company_valuation_usd is None:
        return Resolution.UNRESOLVED  # issuer has no valuation channel — not scoreable
    horizon = traj.horizon_months
    window_end = min(by_month, horizon)
    if window_end >= 0 and np.any(traj.company_valuation_usd[: window_end + 1] >= threshold_usd):
        return Resolution.YES
    if by_month <= horizon:
        return Resolution.NO  # whole window simulated, threshold never reached
    return Resolution.UNRESOLVED  # deadline beyond the simulated horizon, not yet reached


@dataclass(frozen=True)
class ResolutionCounts:
    """Per-rollout YES/NO/UNRESOLVED tally for one market across all rollouts.

    The common currency both the PE per-trajectory loop and the vectorized macro
    resolvers reduce to, so a single row builder turns either into a scored row.
    """

    yes: int
    no: int
    unresolved: int

    @property
    def n_resolved(self) -> int:
        return self.yes + self.no

    @property
    def p_model(self) -> float | None:
        """YES share among resolved rollouts, or None when none resolved."""
        return self.yes / self.n_resolved if self.n_resolved else None

    @classmethod
    def from_resolutions(cls, resolutions: Iterator[Resolution]) -> ResolutionCounts:
        counts = Counter(resolutions)
        return cls(counts[Resolution.YES], counts[Resolution.NO], counts[Resolution.UNRESOLVED])


def level_threshold_counts(
    matrix: npt.NDArray[np.float64], *, threshold: float, direction: Direction, at_month: int, horizon_months: int
) -> ResolutionCounts:
    """Point-in-time threshold on a level series, vectorized over rollouts.

    YES iff the series value AT `at_month` is on `direction`'s side of `threshold`
    (e.g. "S&P 500 >= 7500 on 2026-12-31"). Every rollout resolves when `at_month`
    is within the simulated horizon; an `at_month` beyond the horizon (or before
    month 0) is UNRESOLVED for all rollouts. `matrix` is `(rollout, month)`.
    """
    n = int(matrix.shape[0])
    if at_month < 0 or at_month > horizon_months:
        return ResolutionCounts(yes=0, no=0, unresolved=n)
    column = matrix[:, at_month]
    yes_mask = column >= threshold if direction is Direction.ABOVE else column < threshold
    yes = int(np.count_nonzero(yes_mask))
    return ResolutionCounts(yes=yes, no=n - yes, unresolved=0)


def inflation_yoy_counts(
    matrix: npt.NDArray[np.float64],
    *,
    threshold: float,
    direction: Direction,
    at_month: int,
    horizon_months: int,
    window_months: int = 12,
    history: npt.NDArray[np.float64] | None = None,
) -> ResolutionCounts:
    """Trailing year-over-year change of an index series, vectorized over rollouts.

    `yoy = value[at_month] / value[at_month - window] - 1`, compared to `threshold` (a fraction,
    e.g. 0.03 for 3%). The numerator comes from the sampled path (`at_month` must be within the
    horizon). For the denominator: when `at_month - window` is still within the path it's the
    sampled value; when it falls BEFORE `as_of` (month 0) it's taken from `history` — real index
    values for the months immediately before `as_of`, ordered oldest-first with `history[-1]` the
    month before `as_of` (so `history[lb]` for a negative offset `lb` reads the right month). With
    no history covering that far back, the market is UNRESOLVED.
    """
    n = int(matrix.shape[0])
    if at_month < 0 or at_month > horizon_months:
        return ResolutionCounts(yes=0, no=0, unresolved=n)
    lookback = at_month - window_months
    if lookback >= 0:
        denominator: npt.NDArray[np.float64] | np.float64 = matrix[:, lookback]
    elif history is not None and lookback >= -len(history):
        denominator = history[lookback]  # real index value (deterministic), broadcasts over rollouts
    else:
        return ResolutionCounts(yes=0, no=0, unresolved=n)
    yoy = matrix[:, at_month] / denominator - 1.0
    yes_mask = yoy >= threshold if direction is Direction.ABOVE else yoy < threshold
    yes = int(np.count_nonzero(yes_mask))
    return ResolutionCounts(yes=yes, no=n - yes, unresolved=0)


def inflation_yoy_bucket_counts(
    matrix: npt.NDArray[np.float64],
    *,
    lows: list[float | None],
    highs: list[float | None],
    at_month: int,
    horizon_months: int,
    window_months: int = 12,
    history: npt.NDArray[np.float64] | None = None,
) -> npt.NDArray[np.int64] | None:
    """Per-bucket rollout counts for trailing YoY values at `at_month`.

    This is the distribution-valued sibling of `inflation_yoy_counts`: instead of
    testing one threshold, compute each rollout's YoY rate and count it into
    half-open buckets. Returns `None` when the YoY value is unavailable because the
    requested month is out of horizon or the pre-anchor denominator is not covered
    by `history`.
    """
    if at_month < 0 or at_month > horizon_months:
        return None
    lookback = at_month - window_months
    if lookback >= 0:
        denominator: npt.NDArray[np.float64] | np.float64 = matrix[:, lookback]
    elif history is not None and lookback >= -len(history):
        denominator = history[lookback]
    else:
        return None
    yoy = matrix[:, at_month] / denominator - 1.0
    counts = np.zeros(len(lows), dtype=np.int64)
    for i, (low, high) in enumerate(zip(lows, highs, strict=True)):
        mask = np.ones(yoy.shape, dtype=bool)
        if low is not None:
            mask &= yoy >= low
        if high is not None:
            mask &= yoy < high
        counts[i] = int(np.count_nonzero(mask))
    return counts


def level_by_date_counts(
    matrix: npt.NDArray[np.float64], *, threshold: float, direction: Direction, by_month: int, horizon_months: int
) -> ResolutionCounts:
    """Ever-by-date ("touch") threshold on a level series, vectorized over rollouts.

    YES iff the series EVER reaches `direction`'s side of `threshold` at some month m <= `by_month`
    (e.g. "BTC reaches $150k by 2026-06-30", "S&P hits an all-time high by D"). The level-series
    twin of `valuation_by_date`. A rollout that reaches within the simulated window is YES; when
    `by_month` is within the horizon the rest are NO (the whole window was simulated), and when it
    lies beyond the horizon the un-reached rollouts are UNRESOLVED (they might still cross later).
    """
    n = int(matrix.shape[0])
    if by_month < 0:
        return ResolutionCounts(yes=0, no=0, unresolved=n)
    window = matrix[:, : min(by_month, horizon_months) + 1]
    reached = (
        np.any(window >= threshold, axis=1) if direction is Direction.ABOVE else np.any(window < threshold, axis=1)
    )
    yes = int(np.count_nonzero(reached))
    if by_month <= horizon_months:
        return ResolutionCounts(yes=yes, no=n - yes, unresolved=0)
    return ResolutionCounts(yes=yes, no=0, unresolved=n - yes)


def bucket_model_counts(
    matrix: npt.NDArray[np.float64],
    *,
    lows: list[float | None],
    highs: list[float | None],
    at_month: int,
    horizon_months: int,
) -> npt.NDArray[np.int64] | None:
    """Per-bucket rollout counts at `at_month` for a categorical family, vectorized.

    Bucket `i` is the half-open interval `[lows[i], highs[i])`; `None` means an
    open end (`-inf` / `+inf`). Returns `None` (unscoreable) when `at_month` is
    outside the simulated horizon. For a tiling family the counts sum to the
    rollout count; rollouts outside every bucket are simply uncounted.
    """
    if at_month < 0 or at_month > horizon_months:
        return None
    column = matrix[:, at_month]
    counts = np.zeros(len(lows), dtype=np.int64)
    for i, (low, high) in enumerate(zip(lows, highs, strict=True)):
        mask = np.ones(column.shape, dtype=bool)
        if low is not None:
            mask &= column >= low
        if high is not None:
            mask &= column < high
        counts[i] = int(np.count_nonzero(mask))
    return counts


def trajectories_from_bundle(
    bundle: PrivateEquityBundle, *, issuer: IssuerId | str, rollout_count: int, horizon_months: int, as_of: date
) -> Iterator[RolloutTrajectory]:
    """Slice the augur->sim PE bundle into one RolloutTrajectory per rollout."""
    marks = bundle.issuer_float_matrix(
        issuer, "mark_usd_per_unit", rollout_count=rollout_count, horizon_months=horizon_months
    )
    events = bundle.issuer_int_matrix(
        issuer, "event_kind_code", rollout_count=rollout_count, horizon_months=horizon_months
    )
    regimes = bundle.issuer_int_matrix(
        issuer, "regime_code", rollout_count=rollout_count, horizon_months=horizon_months
    )
    valuations = bundle.issuer_float_matrix(
        issuer, "company_valuation_usd", rollout_count=rollout_count, horizon_months=horizon_months
    )
    # All-zeros across the whole issuer is the channel-off sentinel (a real market
    # cap is strictly positive). Detect once for the issuer, not per-rollout, so a
    # rollout that happens to dip to 0 inside an active channel isn't misread.
    valuation_channel_on = bool(np.any(valuations > 0.0))
    for r in range(rollout_count):
        yield RolloutTrajectory(
            mark_usd_per_unit=marks[r],
            event_kind_code=events[r],
            regime_code=regimes[r],
            as_of=as_of,
            company_valuation_usd=valuations[r] if valuation_channel_on else None,
        )
