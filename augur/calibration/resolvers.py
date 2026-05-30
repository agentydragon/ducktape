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
"IPO by date" and "collapse/acquired before IPO" are apples-to-apples. augur does
NOT model company valuation (the mark is per-UNIT; there is no shares or market-cap
concept) nor revenue, so markets needing those are surfaced for interpretation, not
scored against the model.

This module is generic over the issuer -- nothing here is specific to any one
company. Pass the issuer through ``trajectories_from_bundle``.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from enum import Enum

import numpy as np
import numpy.typing as npt

from augur.model.private_equity_bundle import PrivateEquityBundle
from augur.model.series import IssuerId, PrivateEquityEventKindCode

_DAYS_PER_MONTH = 365.2425 / 12


def months_after(anchor: date, when: date) -> int:
    """Month offset of `when` from `anchor` (floored). May be negative if `when` precedes `anchor`."""
    return math.floor((when - anchor).days / _DAYS_PER_MONTH)


_PUBLIC_MARKET_OPEN = int(PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN)
# An IPO is preempted by any absorbing pre-IPO exit: collapse, forced recovery
# (collapse-flavored), or acquisition cashout.
_FAILURE_EVENTS = (
    int(PrivateEquityEventKindCode.COLLAPSE),
    int(PrivateEquityEventKindCode.FORCED_RECOVERY),
    int(PrivateEquityEventKindCode.ACQUISITION_CASHOUT),
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

    @property
    def horizon_months(self) -> int:
        return int(self.mark_usd_per_unit.shape[0]) - 1

    def month_on_or_before(self, when: date) -> int:
        """Largest month index falling on/before `when`. May exceed horizon or be negative."""
        return months_after(self.as_of, when)

    def first_event_month(self, *codes: int) -> int | None:
        hits = np.flatnonzero(np.isin(self.event_kind_code, codes))
        return int(hits[0]) if hits.size else None


def resolve_ipo_by_date(traj: RolloutTrajectory, *, by_month: int) -> Resolution:
    """YES iff a PUBLIC_MARKET_OPEN (going-public) event occurs at month <= by_month."""
    t_ipo = traj.first_event_month(_PUBLIC_MARKET_OPEN)
    if t_ipo is not None and t_ipo <= by_month:
        return Resolution.YES
    if by_month <= traj.horizon_months:
        return Resolution.NO  # whole window simulated, no IPO
    return Resolution.UNRESOLVED  # deadline beyond the simulated horizon, no IPO yet


def resolve_pre_ipo_failure(traj: RolloutTrajectory) -> Resolution:
    """YES iff an absorbing COLLAPSED/ACQUIRED exit occurs before any PUBLIC_MARKET_OPEN."""
    t_fail = traj.first_event_month(*_FAILURE_EVENTS)
    t_ipo = traj.first_event_month(_PUBLIC_MARKET_OPEN)
    if t_fail is not None and (t_ipo is None or t_fail < t_ipo):
        return Resolution.YES
    if t_ipo is not None and (t_fail is None or t_ipo < t_fail):
        return Resolution.NO  # went public first
    return Resolution.UNRESOLVED  # still private-operating at end of horizon


def resolve_market(traj: RolloutTrajectory, *, mapping_kind: str, params: dict[str, object]) -> Resolution:
    """Dispatch a cleanly-mappable (`exact`) catalog entry to its resolver.

    Only event-based kinds are handled; valuation/revenue markets are not scored
    (augur does not model those quantities) and must be surfaced, not resolved here.
    """
    match mapping_kind:
        case "ipo_by_date":
            return resolve_ipo_by_date(
                traj, by_month=traj.month_on_or_before(date.fromisoformat(str(params["by_date"])))
            )
        case "pre_ipo_failure":
            return resolve_pre_ipo_failure(traj)
    raise ValueError(f"mapping_kind {mapping_kind!r} is not cleanly resolvable against augur (surface it instead)")


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
    for r in range(rollout_count):
        yield RolloutTrajectory(
            mark_usd_per_unit=marks[r], event_kind_code=events[r], regime_code=regimes[r], as_of=as_of
        )
