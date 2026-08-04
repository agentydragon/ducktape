"""Consumer-side external-series context for the simulator.

Production evidence ingestion, model fitting, stochastic sampling, and
provenance belong in `augur/model`; `augur/sim` is a deterministic path
evaluator once it receives those trajectories.

The handoff is the model's own typed `LevelFrames` (one frame per
`LevelSeriesKind`, keyed by a sub-id column, never by a magic-prefix
`series_id` string) plus the typed `PrivateEquityBundle`. The compiler looks
paths up by `LevelSeriesKey`, so nothing on this path re-parses a wire string
that the layer above already had typed. Wire strings reappear only in the
decoded read model (`sim/codec/series.py`), which is a serialization boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from finance.augur.model.exogenous import LevelFrames, SampledExogenousBundle, assemble_level_frames
from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import LevelSeriesKey
from finance.augur.model.series_model import SeriesModelBundle


@dataclass(frozen=True)
class ExternalSeriesContext:
    """The materialized external-series context.

    `levels` carries non-PE level series (asset prices, CPI levels, rent
    levels). `private_equity` carries the typed PE protocol bundle — mark,
    regime, event-kind, fractions, blocked, recovery — per issuer; the sim
    compiler reads it directly by issuer index. PE tender events live on the
    `private_equity.sale_opportunity_active` channel — there is no separate
    exogenous-event frame.
    """

    levels: LevelFrames = field(default_factory=LevelFrames.empty)
    private_equity: PrivateEquityBundle = field(default_factory=PrivateEquityBundle.empty)

    @classmethod
    def from_level_blocks(
        cls,
        blocks: list[tuple[LevelSeriesKey, np.ndarray]],
        *,
        rollout_count: int,
        horizon_months: int,
        private_equity: PrivateEquityBundle | None = None,
    ) -> ExternalSeriesContext:
        """Build a context from `(key, (rollout, month) matrix)` blocks."""

        return cls(
            levels=assemble_level_frames(blocks, rollout_count=rollout_count, horizon_months=horizon_months),
            private_equity=private_equity if private_equity is not None else PrivateEquityBundle.empty(),
        )


def materialize_external_series(
    bundle: SeriesModelBundle, *, rollout_seeds: tuple[int, ...], horizon_months: int
) -> ExternalSeriesContext:
    """Sample a scenario's own series-model bundle into the simulator's series context."""

    return materialize_sampled_exogenous(bundle.sample(rollout_seeds=rollout_seeds, horizon_months=horizon_months))


def materialize_sampled_exogenous(bundle: SampledExogenousBundle) -> ExternalSeriesContext:
    """Adapt a model-owned sampled bundle into the simulator's series context."""

    return ExternalSeriesContext(levels=bundle.levels, private_equity=bundle.private_equity)
