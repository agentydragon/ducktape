"""Independent-per-series exogenous provider configured from YAML.

The provider enumerates every external series the simulator may request as a
typed, per-kind group (inflation/sp500 singletons; crypto/home_value/rent keyed
by their sub-id), each mapped to a scalar level model (Constant / Deterministic
/ GBM). Private-equity marks are a separate `private_equity_marks` map keyed by
issuer id — they are not level series and travel via the typed PE bundle /
metadata, not the `levels` frame. There is no prefix dispatch: config keys are
already typed, so the level-vs-PE split is structural rather than parsed.

The model is the only source of price for any series it covers (including the
per-issuer current PE price), exposed both as the month-0 level and as the
`private_equity_prices_usd` metadata dict on the sampled bundle.
"""

from __future__ import annotations

from typing import Literal, assert_never

import jax.numpy as jnp
import numpy as np
from numpyro import distributions as dist
from pydantic import Field

from augur.frames import concat_frames
from augur.model.deterministic import Constant, Deterministic
from augur.model.exogenous import (
    SERIES_LEVELS_SCHEMA,
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    series_levels_frame,
)
from augur.model.gbm import GeometricBrownian
from augur.model.level_series_groups import LevelSeriesGroups
from augur.model.path_models.scenarios import HistoricalSeries
from augur.model.schemas import FrozenModel
from augur.model.series import IssuerId, LevelSeriesKey
from augur.model.series_model import ScalarSeriesSpec, derive_stream_rollout_seeds


class IndependentExogenousProviderConfig(LevelSeriesGroups[ScalarSeriesSpec]):
    """YAML provider that enumerates every level series and PE mark explicitly.

    Level series are the per-kind fields inherited from `LevelSeriesGroups`
    (`inflation`/`sp500` singletons; `crypto`/`home_value`/`rent` keyed by
    sub-id). `private_equity_marks` carries per-issuer mark specs separately —
    PE marks are not level series. `extra="forbid"` (from `FrozenModel`) rejects
    stray top-level keys, including legacy `"crypto:btc"`-style wire ids.
    """

    type: Literal["independent"] = "independent"
    private_equity_marks: dict[IssuerId, ScalarSeriesSpec] = Field(default_factory=dict)

    def realize_model(self) -> IndependentExogenousModel:
        return IndependentExogenousModel(level_series=self.by_level_key(), pe_marks=dict(self.private_equity_marks))


class IndependentExogenousModel(FrozenModel):
    """Runtime exogenous model built from an `IndependentExogenousProviderConfig`.

    Implements `Sampler` (the runtime sampling contract) and `Scorable` (the
    metric battery contract). No `Fittable` — params are YAML-set, not fit.

    `level_series` is keyed by typed `LevelSeriesKey`; `pe_marks` by typed
    `IssuerId`. The split is structural (it came from typed config), so this
    model never parses a prefix.
    """

    label: str = "independent_exogenous_model"
    level_series: dict[LevelSeriesKey, ScalarSeriesSpec]
    pe_marks: dict[IssuerId, ScalarSeriesSpec] = Field(default_factory=dict)

    @property
    def factor_names(self) -> tuple[str, ...]:
        """Wire ids of the level series, in iteration order.

        `factor_names` is the wire-string view consumed by `predictive` (which
        is itself driven by `HistoricalSeries.factor_names` wire ids). PE marks
        are not factors here — they are not level series.
        """

        return tuple(key.wire_id for key in self.level_series)

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        # PE marks travel via the typed PE bundle / metadata, never the `levels`
        # frame — `level_series` already excludes them by construction.
        level_blocks = [
            series_levels_frame(
                level_key,
                model.sample_levels(
                    # Seed substreams stay keyed on the stable wire id so a series'
                    # path is identical regardless of config-dict ordering.
                    rollout_seeds=derive_stream_rollout_seeds(request.rollout_seeds, stream_id=level_key.wire_id),
                    horizon_months=request.horizon_months,
                ),
                rollout_count=request.rollout_count,
                horizon_months=request.horizon_months,
            )
            for level_key, model in self.level_series.items()
        ]
        return SampledExogenousBundle(
            levels=concat_frames(level_blocks, SERIES_LEVELS_SCHEMA),
            metadata={"model_id": self.label, "private_equity_prices_usd": self._private_equity_prices_usd()},
        )

    def predictive(self, historical: HistoricalSeries, t: int, *, horizon: int = 1) -> dist.Distribution | None:
        """Joint predictive over the cumulative `horizon`-step log-return at
        origin t for the factor list `historical.factor_names`.

        Under per-series independence (the whole point of this provider) the
        joint is a `MultivariateNormal` with diagonal covariance. The
        marginal for factor i is `N(horizon · μ_i, horizon · σ_i²)` — h
        independent N(μ, σ²) increments cumulate to N(hμ, hσ²).

        Returns `None` if any factor in `historical.factor_names` isn't
        backed by a GBM scalar in the provider config — Constant /
        Deterministic factors have zero predictive variance and the
        density is degenerate.
        """
        if horizon < 1:
            raise ValueError(f"horizon must be >= 1; got {horizon}")
        n_steps = historical.levels.shape[0] - 1
        if t + horizon > n_steps:
            return None

        # `historical.factor_names` are wire ids; match them against the wire-id
        # view of the level series. This is frame/historical readback, not config.
        spec_by_wire_id = {key.wire_id: spec for key, spec in self.level_series.items()}
        mus: list[float] = []
        sigmas: list[float] = []
        for factor in historical.factor_names:
            spec = spec_by_wire_id.get(factor)
            if not isinstance(spec, GeometricBrownian):
                return None
            mus.append(float(spec.monthly_log_return_mu) * horizon)
            sigmas.append(float(spec.monthly_log_return_sigma) * np.sqrt(horizon))
        mean_arr = jnp.asarray(np.asarray(mus, dtype=np.float32))
        sd_arr = jnp.asarray(np.asarray(sigmas, dtype=np.float32))
        cov_arr = jnp.diag(sd_arr**2)
        return dist.MultivariateNormal(mean_arr, covariance_matrix=cov_arr)

    def _private_equity_prices_usd(self) -> dict[str, float]:
        return {str(issuer_id): _month_zero_level(spec) for issuer_id, spec in self.pe_marks.items()}


def _month_zero_level(spec: ScalarSeriesSpec) -> float:
    if isinstance(spec, Constant):
        return float(spec.value)
    if isinstance(spec, Deterministic):
        return float(spec.levels[0])
    if isinstance(spec, GeometricBrownian):
        return float(spec.initial_value)
    assert_never(spec)
