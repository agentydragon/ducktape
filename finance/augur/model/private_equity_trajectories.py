"""Pre-sampled private-equity trajectories: ingest + Sampler overlay.

A separate trainer produces a
JSONL artifact with sampled per-rollout tender-event paths per issuer. This module
parses the artifact and exposes a `Sampler` wrapper that bolts the PE trajectories
onto an underlying exogenous bundle (VECM, independent, …) so the sim sees a unified
view: stocks/inflation/housing from the underlying provider, PE marks and tender
events from the artifact.

Artifact schema (one event per JSONL line):

```jsonl
{"issuer_id": "private_company_a", "trajectory_index": 0, "month_index": 5, "event_type": "tender", "price_per_share_usd": 250.0}
{"issuer_id": "private_company_a", "trajectory_index": 0, "month_index": 18, "event_type": "tender", "price_per_share_usd": 290.0}
{"issuer_id": "private_company_a", "trajectory_index": 1, "month_index": 12, "event_type": "tender", "price_per_share_usd": 220.0}
```

Only `event_type == "tender"` is consumed today. Other event types (`ipo`,
`public_mark`, `acquisition`) are tolerated but ignored — they're produced by the
upstream model for the full regime ladder (public-market / acquired / collapsed),
which this pre-sampled trajectories path scopes out (it consumes only `tender`
events). The structured `private_equity_risk` provider, by contrast, emits those
regime transitions on the bundle's `regime_code` channel.

Trajectory selection per (rollout, issuer) is deterministic:
`trajectory_index = rollout_seed % len(trajectories_for_issuer)`.

The initial mark (sim t=0 valuation) comes from `initial_marks` (typically sourced
from the portfolio config's `unit_value_usd`), not from the artifact, so the user's
known-current-mark anchors the rollout. The artifact must still supply modeled
future trajectories for every issuer; missing issuers fail loudly instead of
falling back to flat marks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from finance.augur.model.exogenous import (
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    Sampler,
    validate_sample_satisfies_request,
)
from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.private_equity_protocol import neutral_private_equity_issuer_bundle
from finance.augur.model.series import IssuerId, LevelSeriesKey


@dataclass(frozen=True)
class TenderEvent:
    """One tender opportunity within a trajectory."""

    month_index: int
    price_per_share_usd: float


@dataclass(frozen=True)
class PrivateEquityTrajectorySet:
    """All sampled trajectories for one PE issuer + the user-known starting mark."""

    issuer_id: str
    initial_mark_usd: float
    trajectories: tuple[tuple[TenderEvent, ...], ...]


def read_private_equity_trajectories_jsonl(
    path: Path, *, initial_marks: dict[str, float]
) -> dict[str, PrivateEquityTrajectorySet]:
    """Parse a JSONL artifact into per-issuer trajectory sets."""

    by_issuer: dict[str, dict[int, list[TenderEvent]]] = {}
    with path.open("r", encoding="utf-8") as f:
        for line_number, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            row = json.loads(line)
            event_type = row.get("event_type")
            if event_type != "tender":
                continue
            issuer = row["issuer_id"]
            trajectory = int(row["trajectory_index"])
            try:
                month_index = int(row["month_index"])
                price = float(row["price_per_share_usd"])
            except (KeyError, ValueError) as error:
                raise ValueError(f"PE trajectory artifact {path} line {line_number}: {error}") from error
            by_issuer.setdefault(issuer, {}).setdefault(trajectory, []).append(
                TenderEvent(month_index=month_index, price_per_share_usd=price)
            )
    result: dict[str, PrivateEquityTrajectorySet] = {}
    for issuer, by_traj in by_issuer.items():
        if issuer not in initial_marks:
            raise ValueError(
                f"PE trajectory artifact {path} references issuer {issuer!r} but no initial mark provided "
                f"(known issuers: {sorted(initial_marks)})"
            )
        ordered_indices = sorted(by_traj.keys())
        ordered_trajectories = tuple(
            tuple(sorted(by_traj[idx], key=lambda event: event.month_index)) for idx in ordered_indices
        )
        result[issuer] = PrivateEquityTrajectorySet(
            issuer_id=issuer, initial_mark_usd=float(initial_marks[issuer]), trajectories=ordered_trajectories
        )
    missing = sorted(set(initial_marks) - set(result))
    if missing:
        raise ValueError(
            f"PE trajectory artifact {path} has no modeled trajectories for issuer(s) {missing}; "
            "train a private-equity model or remove the private-equity holding"
        )
    return result


@dataclass(frozen=True)
class PreSampledPrivateEquitySampler:
    """Sampler overlay: underlying provider + per-issuer PE trajectories from an artifact.

    `sample()` calls the underlying provider without PE requirements, strips out any
    pre-existing PE protocol level/event series (the artifact is the source of truth
    for PE), then appends our materialized PE levels and tender events for every
    configured issuer.
    """

    underlying: Sampler
    trajectories_by_issuer: dict[str, PrivateEquityTrajectorySet]

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        # Level series come straight from the underlying provider; this overlay only adds PE.
        return self.underlying.emittable_level_keys()

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        # The overlay overrides PE wholesale (it strips the underlying's PE bundle first), so
        # the only PE issuers this sampler emits are the artifact's.
        return frozenset(IssuerId(issuer) for issuer in self.trajectories_by_issuer)

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        # Forward the non-PE level-series channels straight to the underlying provider; this
        # overlay only adds PE trajectories on top. `replace` rather than a field-by-field
        # rebuild, so a level role added later is forwarded instead of silently dropped.
        underlying_request = replace(request, required_private_equity_issuers=frozenset())
        bundle = self.underlying.sample(underlying_request)
        if not self.trajectories_by_issuer:
            validate_sample_satisfies_request(request, bundle)
            return bundle

        rollout_count = request.rollout_count
        horizon_months = request.horizon_months

        pe_bundle_parts: list[PrivateEquityBundle] = []
        for issuer, trajectory_set in self.trajectories_by_issuer.items():
            levels = _materialize_pe_levels(
                trajectory_set, rollout_seeds=request.rollout_seeds, horizon_months=horizon_months
            )
            events = _materialize_pe_events(
                trajectory_set, rollout_seeds=request.rollout_seeds, horizon_months=horizon_months
            )
            pe_bundle_parts.append(
                neutral_private_equity_issuer_bundle(
                    issuer,
                    observed_mark=levels,
                    tender_events=events,
                    rollout_count=rollout_count,
                    horizon_months=horizon_months,
                )
            )

        sampled = SampledExogenousBundle(
            levels=bundle.levels,
            private_equity=PrivateEquityBundle.combine(pe_bundle_parts),
            private_equity_prices_usd=bundle.private_equity_prices_usd,
            model_id=bundle.model_id,
            provenance={**bundle.provenance, "private_equity_issuers": tuple(sorted(self.trajectories_by_issuer))},
        )
        validate_sample_satisfies_request(request, sampled)
        return sampled


def _materialize_pe_levels(
    trajectory_set: PrivateEquityTrajectorySet, *, rollout_seeds: tuple[int, ...], horizon_months: int
) -> np.ndarray:
    """Build a (rollout, horizon+1) float matrix of piecewise-constant marks per rollout.

    Each rollout starts at the initial mark; at each tender month within its trajectory the
    mark steps to the tender's realized price and carries forward until the next tender (or
    the end of the horizon).
    """

    rollout_count = len(rollout_seeds)
    levels = np.full((rollout_count, horizon_months + 1), trajectory_set.initial_mark_usd, dtype=np.float64)
    if not trajectory_set.trajectories:
        raise ValueError(
            f"private-equity issuer {trajectory_set.issuer_id!r} has no modeled trajectories; "
            "flat private-equity fallbacks are not supported"
        )
    trajectory_count = len(trajectory_set.trajectories)
    for rollout_idx, seed in enumerate(rollout_seeds):
        trajectory = trajectory_set.trajectories[seed % trajectory_count]
        current_mark = trajectory_set.initial_mark_usd
        last_set_index = 0  # next index to fill is `last_set_index`
        for event in trajectory:
            if event.month_index < 0 or event.month_index > horizon_months:
                continue
            levels[rollout_idx, last_set_index : event.month_index] = current_mark
            current_mark = event.price_per_share_usd
            levels[rollout_idx, event.month_index] = current_mark
            last_set_index = event.month_index + 1
        levels[rollout_idx, last_set_index : horizon_months + 1] = current_mark
    return levels


def _materialize_pe_events(
    trajectory_set: PrivateEquityTrajectorySet, *, rollout_seeds: tuple[int, ...], horizon_months: int
) -> np.ndarray:
    rollout_count = len(rollout_seeds)
    events = np.zeros((rollout_count, horizon_months + 1), dtype=np.bool_)
    if not trajectory_set.trajectories:
        raise ValueError(
            f"private-equity issuer {trajectory_set.issuer_id!r} has no modeled trajectories; "
            "flat private-equity fallbacks are not supported"
        )
    trajectory_count = len(trajectory_set.trajectories)
    for rollout_idx, seed in enumerate(rollout_seeds):
        trajectory = trajectory_set.trajectories[seed % trajectory_count]
        for event in trajectory:
            if 0 <= event.month_index <= horizon_months:
                events[rollout_idx, event.month_index] = True
    return events
